"""Purchase order GraphQL types and operations."""

import hashlib
import json
import re
import uuid
from decimal import Decimal, ROUND_CEILING

import graphene
from django.db import connection, transaction
from django.utils import timezone
from graphql import GraphQLError

from saleor.catalog_events import record_catalog_event
from . import payments


_ADDRESS_LIMITS = {"recipient": 250, "street": 500, "city": 150, "postal_code": 32, "phone": 50}


def _peso_savings(amount):
    return Decimal(amount).quantize(Decimal("1"), rounding=ROUND_CEILING)


def _select_savings(promotion_savings, coupon_savings):
    promotions = _peso_savings(sum(promotion_savings, Decimal("0")))
    coupon = _peso_savings(coupon_savings)
    if promotions >= coupon:
        return promotions, False
    return coupon, True


def _calculate_savings(cart_lines, promotions, coupon_discount_pct):
    subtotal = sum(
        (Decimal(str(line[1])) * int(line[2]) for line in cart_lines), Decimal("0")
    )
    global_rate = Decimal("0")
    promotion_rates = {}
    for product_id, discount_pct in promotions:
        rate = Decimal(str(discount_pct))
        if product_id is None:
            global_rate = max(global_rate, rate)
        else:
            product_id = int(product_id)
            promotion_rates[product_id] = max(promotion_rates.get(product_id, Decimal("0")), rate)
    promotion_savings = [
        Decimal(str(price)) * int(quantity)
        * promotion_rates.get(int(product_id), global_rate) / 100
        for product_id, price, quantity in cart_lines
    ]
    coupon_percent = Decimal(str(coupon_discount_pct))
    if coupon_percent > 50:
        raise ValueError("DISCOUNT_LIMIT_EXCEEDED")
    coupon_saving = subtotal * coupon_percent / 100
    return subtotal, *_select_savings(promotion_savings, coupon_saving)


def _coupon_row(cursor, code, lock=False):
    sql = (
        "SELECT c.id, c.active, c.starts_at, c.ends_at, c.remaining, "
        "p.discount_pct, p.active, p.starts_at, p.ends_at "
        "FROM coupon c LEFT JOIN promotion p ON p.id = c.promotion_id "
        "WHERE c.code = %s"
    )
    if lock:
        sql += " FOR UPDATE OF c"
    cursor.execute(sql, [code])
    return cursor.fetchone()


def _coupon_eligibility(coupon, now):
    coupon_id, active, starts_at, ends_at, remaining, discount, promo_active, promo_start, promo_end = coupon
    if not active or not promo_active or discount is None:
        return "INACTIVE"
    if starts_at and now < starts_at:
        return "NOT_YET_VALID"
    if promo_start and now < promo_start:
        return "NOT_YET_VALID"
    if ends_at and now >= ends_at:
        return "EXPIRED"
    if promo_end and now >= promo_end:
        return "EXPIRED"
    if remaining <= 0:
        return "EXHAUSTED"
    if Decimal(str(discount)) > 50:
        return "DISCOUNT_LIMIT_EXCEEDED"
    return None


def _promotion_rows(cursor, bgg_ids, now):
    if not bgg_ids:
        return []
    cursor.execute(
        "SELECT p.product_bgg_id, p.discount_pct FROM promotion p "
        "WHERE p.active = TRUE "
        "AND (p.product_bgg_id = ANY(%s) OR p.product_bgg_id IS NULL) "
        "AND (p.starts_at IS NULL OR p.starts_at <= %s) "
        "AND (p.ends_at IS NULL OR p.ends_at > %s) "
        "AND NOT EXISTS (SELECT 1 FROM coupon c WHERE c.promotion_id = p.id)",
        [list(bgg_ids), now, now],
    )
    return cursor.fetchall()


def _discounted_payment_lines(cart_lines, savings):
    gross = [Decimal(str(line[2])) * int(line[3]) for line in cart_lines]
    savings_cents = int((Decimal(savings) * 100).to_integral_value())
    gross_cents = [int((amount * 100).to_integral_value()) for amount in gross]
    gross_total = sum(gross_cents)
    if not gross_total or not savings_cents:
        return [
            {"bgg_id": bgg_id, "name": name, "unit_price": price, "quantity": quantity}
            for bgg_id, name, price, quantity in cart_lines
        ]
    allocations = [savings_cents * amount // gross_total for amount in gross_cents]
    for index in range(savings_cents - sum(allocations)):
        allocations[index % len(allocations)] += 1
    result = []
    for (bgg_id, name, _price, quantity), amount, discount in zip(
        cart_lines, gross_cents, allocations
    ):
        adjusted = max(0, amount - discount)
        if adjusted:
            result.append({
                "bgg_id": bgg_id,
                "name": f"{name} (quantity {quantity})",
                "unit_price": Decimal(adjusted) / 100,
                "quantity": 1,
            })
    return result


def _coupon_operation_hash(user_id, checkout_key):
    return hashlib.sha256(f"{user_id}:{checkout_key}".encode("utf-8")).hexdigest()


def _checkout_payload_hash(user_id, mode, address, coupon_code):
    encoded = json.dumps(
        {
            "user_id": user_id,
            "shipping_mode": mode,
            "address": address,
            "coupon_code": coupon_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkout_replay_error(prior, payload_hash):
    if prior[0] != payload_hash:
        return _error(
            "IDEMPOTENCY_CONFLICT",
            "X-Op-Key was already used for a different checkout.",
            ["checkoutKey"],
        )
    if prior[1] is None:
        return _error(
            "PENDING_PAYMENT",
            "The original checkout is still being reconciled.",
        )
    return None


def _reserve_coupon(cursor, coupon_id, order_id, operation_hash, payload_hash, use_coupon):
    if use_coupon:
        cursor.execute(
            "UPDATE coupon SET remaining = remaining - 1 "
            "WHERE id = %s AND remaining > 0 RETURNING remaining",
            [coupon_id],
        )
        if cursor.fetchone() is None:
            raise ValueError("EXHAUSTED")
    cursor.execute(
        "INSERT INTO coupon_checkout_reservation "
        "(order_id, coupon_id, operation_key_hash, request_hash, state) "
        "VALUES (%s, %s, %s, %s, %s)",
        [
            order_id,
            coupon_id,
            operation_hash,
            payload_hash,
            "RESERVED" if use_coupon else "NOT_APPLIED",
        ],
    )


def _release_coupon_reservation(cursor, order_id):
    cursor.execute(
        "UPDATE coupon_checkout_reservation SET state = 'RELEASED', updated_at = CURRENT_TIMESTAMP "
        "WHERE order_id = %s AND state IN ('RESERVED', 'REVIEW') RETURNING coupon_id",
        [order_id],
    )
    row = cursor.fetchone()
    if row:
        cursor.execute(
            "UPDATE coupon SET remaining = remaining + 1 WHERE id = %s",
            [row[0]],
        )


def _set_coupon_reservation_state(cursor, order_id, state):
    cursor.execute(
        "UPDATE coupon_checkout_reservation SET state = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE order_id = %s AND state IN ('RESERVED', 'REVIEW')",
        [state, order_id],
    )


def _rereserve_released_coupon(cursor, order_id, now):
    cursor.execute(
        "SELECT coupon_id, state FROM coupon_checkout_reservation "
        "WHERE order_id = %s FOR UPDATE",
        [order_id],
    )
    reservation = cursor.fetchone()
    if not reservation or reservation[1] != "RELEASED":
        return None
    cursor.execute(
        "SELECT code FROM coupon WHERE id = %s",
        [reservation[0]],
    )
    row = cursor.fetchone()
    coupon = _coupon_row(cursor, row[0], lock=True) if row else None
    if coupon is None:
        return "COUPON_NOT_FOUND"
    reason = _coupon_eligibility(coupon, now)
    if reason:
        return reason
    cursor.execute(
        "UPDATE coupon SET remaining = remaining - 1 "
        "WHERE id = %s AND remaining > 0 RETURNING remaining",
        [coupon[0]],
    )
    if cursor.fetchone() is None:
        return "EXHAUSTED"
    cursor.execute(
        "UPDATE coupon_checkout_reservation SET state = 'RESERVED', "
        "updated_at = CURRENT_TIMESTAMP WHERE order_id = %s AND state = 'RELEASED'",
        [order_id],
    )
    return None


def preview_coupon(info, code):
    from .api import _authenticated_user

    user = _authenticated_user(info)
    if user is None:
        raise GraphQLError(
            "Authentication is required.",
            extensions={"code": "UNAUTHENTICATED"},
        )
    try:
        now = timezone.now()
        with connection.cursor() as cursor:
            coupon = _coupon_row(cursor, code)
            if coupon is None:
                raise GraphQLError(
                    "Coupon not found.",
                    extensions={"code": "NOT_FOUND"},
                )
            cursor.execute(
                "SELECT cart_id FROM shopping_cart WHERE user_id = %s",
                [user.pk],
            )
            cart = cursor.fetchone()
            if cart is None:
                return CouponPreview(
                    valid=False, coupon_code=code, discount_amount=0,
                    new_total=0, reason="EMPTY_CART",
                )
            cursor.execute(
                "SELECT p.bgg_id, p.price_amount, line.quantity "
                "FROM shopping_cart_line line "
                "JOIN product_boardgame p ON p.bgg_id = line.bgg_id "
                "WHERE line.cart_id = %s ORDER BY p.bgg_id",
                [cart[0]],
            )
            cart_lines = cursor.fetchall()
            if not cart_lines:
                return CouponPreview(
                    valid=False, coupon_code=code, discount_amount=0,
                    new_total=0, reason="EMPTY_CART",
                )
            subtotal = sum(
                (Decimal(str(row[1])) * row[2] for row in cart_lines),
                Decimal("0"),
            )
            reason = _coupon_eligibility(coupon, now)
            if reason:
                return CouponPreview(
                    valid=False, coupon_code=code, discount_amount=0,
                    new_total=float(subtotal), reason=reason,
                )
            promotions = _promotion_rows(
                cursor, (row[0] for row in cart_lines), now
            )
            subtotal, savings, _coupon_wins = _calculate_savings(
                cart_lines, promotions, coupon[5]
            )
        return CouponPreview(
            valid=True,
            coupon_code=code,
            discount_amount=float(savings),
            new_total=float(max(Decimal("0"), subtotal - savings)),
            reason=None,
        )
    except GraphQLError:
        raise
    except Exception:
        raise GraphQLError(
            "Coupon preview could not be completed.",
            extensions={"code": "SERVER_ERROR"},
        ) from None


class ShippingMode(graphene.Enum):
    STANDARD = "standard"
    PICKUP = "pickup"


class ShippingAddressInput(graphene.InputObjectType):
    recipient = graphene.String()
    street = graphene.String()
    city = graphene.String()
    postal_code = graphene.String()
    phone = graphene.String()


class OrderLine(graphene.ObjectType):
    bgg_id = graphene.Int(required=True)
    name = graphene.String(required=True)
    unit_price = graphene.Float(required=True)
    quantity = graphene.Int(required=True)
    subtotal = graphene.Float(required=True)


class Invoice(graphene.ObjectType):
    number = graphene.BigInt(required=True)
    issued_at = graphene.String(required=True)
    total = graphene.Float(required=True)


class PurchaseOrder(graphene.ObjectType):
    id = graphene.ID(required=True)
    status = graphene.String(required=True)
    total = graphene.Float(required=True)
    currency = graphene.String(required=True)
    shipping_mode = graphene.String(required=True)
    recipient = graphene.String()
    street = graphene.String()
    city = graphene.String()
    postal_code = graphene.String()
    phone = graphene.String()
    payment_status = graphene.String()
    payment_status_detail = graphene.String()
    created_at = graphene.String(required=True)
    paid_at = graphene.String()
    lines = graphene.List(graphene.NonNull(OrderLine), required=True)
    invoice = graphene.Field(Invoice)


class OrderUserError(graphene.ObjectType):
    field = graphene.List(graphene.NonNull(graphene.String))
    code = graphene.String(required=True)
    message = graphene.String(required=True)


class CheckoutPayload(graphene.ObjectType):
    order = graphene.Field(PurchaseOrder)
    init_point = graphene.String()
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class CouponPreview(graphene.ObjectType):
    valid = graphene.Boolean(required=True)
    coupon_code = graphene.String(required=True)
    discount_amount = graphene.Float(required=True)
    new_total = graphene.Float(required=True)
    reason = graphene.String()


class ConfirmPaymentPayload(graphene.ObjectType):
    order = graphene.Field(PurchaseOrder)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


def _error(code, message, field=None):
    return OrderUserError(code=code, message=message, field=field)


def _row(cursor, values):
    return dict(zip((column[0] for column in cursor.description), values))


def _load_order(cursor, order_id, user_id=None, lock=False):
    sql = (
        "SELECT id, user_id, status, total, currency, shipping_mode, recipient, "
        "street, city, postal_code, phone, mp_preference_id, mp_payment_id, "
        "payment_status, payment_status_detail, created_at, paid_at "
        "FROM purchase_order WHERE id = %s"
    )
    params = [order_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    if lock:
        sql += " FOR UPDATE"
    cursor.execute(sql, params)
    values = cursor.fetchone()
    return _row(cursor, values) if values else None


def _order_payload(cursor, order):
    cursor.execute(
        "SELECT bgg_id, name, unit_price, quantity, subtotal "
        "FROM purchase_order_line WHERE order_id = %s ORDER BY bgg_id",
        [order["id"]],
    )
    lines = [
        OrderLine(
            bgg_id=row[0],
            name=row[1],
            unit_price=float(row[2]),
            quantity=row[3],
            subtotal=float(row[4]),
        )
        for row in cursor.fetchall()
    ]
    cursor.execute(
        "SELECT number, issued_at, total FROM invoice WHERE order_id = %s",
        [order["id"]],
    )
    row = cursor.fetchone()
    invoice = (
        Invoice(number=row[0], issued_at=row[1].isoformat(), total=float(row[2]))
        if row
        else None
    )
    for key in ("created_at", "paid_at"):
        if order[key] is not None:
            order[key] = order[key].isoformat()
    fields = (
        "id",
        "status",
        "total",
        "currency",
        "shipping_mode",
        "recipient",
        "street",
        "city",
        "postal_code",
        "phone",
        "payment_status",
        "payment_status_detail",
        "created_at",
        "paid_at",
    )
    data = {key: order[key] for key in fields}
    data.update(lines=lines, invoice=invoice)
    return PurchaseOrder(**data)


def _order_by_id(info, order_id, user=None):
    from .api import _authenticated_user

    if user is None:
        user = _authenticated_user(info)
    if user is None:
        return None
    try:
        identifier = uuid.UUID(str(order_id))
    except (ValueError, TypeError, AttributeError):
        return None
    with connection.cursor() as cursor:
        order = _load_order(cursor, identifier, user.pk)
        return _order_payload(cursor, order) if order else None


def _preference(order_id, lines):
    try:
        result = payments.create_preference(order_id, lines)
    except Exception:
        return None, None
    if not isinstance(result, dict) or result.get("status") not in (200, 201):
        return None, None
    response = result.get("response", {})
    if not isinstance(response, dict):
        return None, None
    preference_id = response.get("id")
    if (isinstance(preference_id, bool) or not isinstance(preference_id, (str, int))
            or (isinstance(preference_id, int) and preference_id <= 0)):
        return None, None
    preference_id = str(preference_id)
    if (not 1 <= len(preference_id) <= 128 or preference_id != preference_id.strip()
            or not preference_id.isprintable()):
        return None, None
    return preference_id, response.get("init_point")


def _lock_credit_account(cursor, user_id):
    cursor.execute(
        "INSERT INTO credit_account (user_id, balance, currency) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id) DO NOTHING", [user_id, Decimal("0.00"), "ARS"]
    )
    cursor.execute(
        "SELECT user_id FROM credit_account WHERE user_id = %s FOR UPDATE", [user_id]
    )


def checkout(info, shipping_mode, address=None, cart_id=None, coupon_code=None, checkout_key=None):
    from .api import _authenticated_user, _get_cart_id

    user = _authenticated_user(info)
    if user is None:
        return CheckoutPayload(
            user_errors=[_error("UNAUTHENTICATED", "Authentication is required.")]
        )
    if coupon_code is not None and (
        not isinstance(coupon_code, str) or not coupon_code
    ):
        return CheckoutPayload(
            user_errors=[_error("INVALID", "Invalid coupon code.", ["couponCode"])]
        )
    if coupon_code is not None and (
        not isinstance(checkout_key, str) or not checkout_key or len(checkout_key) > 512
    ):
        return CheckoutPayload(
            user_errors=[
                _error(
                    "INVALID",
                    "X-Op-Key is required for coupon checkout.",
                    ["checkoutKey"],
                )
            ]
        )
    issue = payments.configuration_error()
    if issue:
        return CheckoutPayload(user_errors=[_error("PAYMENTS_DISABLED", issue)])
    mode = str(getattr(shipping_mode, "value", shipping_mode)).lower()
    if mode not in ("standard", "pickup"):
        return CheckoutPayload(
            user_errors=[
                _error(
                    "INVALID_SHIPPING_MODE", "Invalid shipping mode.", ["shippingMode"]
                )
            ]
        )
    address = address or {}
    values = {
        key: (
            address.get(key)
            if isinstance(address, dict)
            else getattr(address, key, None)
        )
        for key in ("recipient", "street", "city", "postal_code", "phone")
    }
    invalid_lengths = any(
        value is not None and (not isinstance(value, str) or len(value) > _ADDRESS_LIMITS[key])
        for key, value in values.items()
    )
    if invalid_lengths or (mode == "standard" and any(
        not (values[key] or "").strip() for key in ("recipient", "street", "city", "postal_code")
    )):
        return CheckoutPayload(
            user_errors=[
                _error(
                    "ADDRESS_REQUIRED",
                    "A complete shipping address is required.",
                    ["address"],
                )
            ]
        )
    operation_hash = (
        _coupon_operation_hash(user.pk, checkout_key)
        if coupon_code is not None else None
    )
    payload_hash = (
        _checkout_payload_hash(user.pk, mode, values, coupon_code)
        if coupon_code is not None else None
    )
    with transaction.atomic(durable=True):
        with connection.cursor() as cursor:
            if operation_hash:
                lock_number = int.from_bytes(
                    bytes.fromhex(operation_hash[:16]), "big", signed=True
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s::bigint)",
                    [lock_number],
                )
                cursor.execute(
                    # Read-only replay lookup avoids reversing preference-completion lock order.
                    "SELECT reservation.request_hash, reservation.init_point, order_row.id "
                    "FROM coupon_checkout_reservation reservation "
                    "JOIN purchase_order order_row ON order_row.id = reservation.order_id "
                    "WHERE reservation.operation_key_hash = %s "
                    "AND order_row.user_id = %s",
                    [operation_hash, user.pk],
                )
                prior = cursor.fetchone()
                if prior:
                    replay_error = _checkout_replay_error(prior, payload_hash)
                    if replay_error:
                        return CheckoutPayload(user_errors=[replay_error])
                    order = _load_order(cursor, prior[2], user.pk)
                    return CheckoutPayload(
                        order=_order_payload(cursor, order),
                        init_point=prior[1],
                        user_errors=[],
                    )
            _lock_credit_account(cursor, user.pk)
            cart_identifier = _get_cart_id(cursor, user, cart_id)
            cursor.execute(
                "SELECT p.bgg_id, p.name, p.price_amount, line.quantity, p.quantity "
                "FROM shopping_cart_line line JOIN product_boardgame p "
                "ON p.bgg_id = line.bgg_id WHERE line.cart_id = %s "
                "ORDER BY p.bgg_id FOR UPDATE OF p",
                [uuid.UUID(cart_identifier)],
            )
            cart_lines = cursor.fetchall()
            if not cart_lines:
                return CheckoutPayload(
                    user_errors=[_error("EMPTY_CART", "The cart is empty.", ["cartId"])]
                )
            if any(row[3] > row[4] for row in cart_lines):
                return CheckoutPayload(
                    user_errors=[
                        _error(
                            "INSUFFICIENT_STOCK",
                            "One or more items have insufficient stock.",
                            ["cartId"],
                        )
                    ]
                )
            subtotal = sum(
                (Decimal(str(row[2])) * row[3] for row in cart_lines), Decimal("0")
            )
            now = timezone.now()
            coupon = None
            if coupon_code is not None:
                coupon = _coupon_row(cursor, coupon_code, lock=True)
                if coupon is None:
                    return CheckoutPayload(
                        user_errors=[
                            _error(
                                "COUPON_NOT_FOUND",
                                "Coupon not found.",
                                ["couponCode"],
                            )
                        ]
                    )
                reason = _coupon_eligibility(coupon, now)
                if reason:
                    return CheckoutPayload(
                        user_errors=[
                            _error(
                                reason,
                                "Coupon is not eligible for this checkout.",
                                ["couponCode"],
                            )
                        ]
                    )
            promotions = _promotion_rows(
                cursor, (row[0] for row in cart_lines), now
            )
            try:
                subtotal, savings, coupon_wins = _calculate_savings(
                    [(row[0], row[2], row[3]) for row in cart_lines],
                    promotions,
                    coupon[5] if coupon else Decimal("0"),
                )
            except ValueError:
                return CheckoutPayload(
                    user_errors=[
                        _error(
                            "DISCOUNT_LIMIT_EXCEEDED",
                            "Coupon discount exceeds the 50% limit.",
                            ["couponCode"],
                        )
                    ]
                )
            total = max(Decimal("0"), subtotal - savings)
            order_id = uuid.uuid4()
            cursor.execute(
                "INSERT INTO purchase_order (id, user_id, status, total, currency, "
                "shipping_mode, recipient, street, city, postal_code, phone) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    order_id,
                    user.pk,
                    "pending_payment",
                    total,
                    payments.currency_id(),
                    mode,
                    *values.values(),
                ],
            )
            lines = []
            for bgg_id, name, price, quantity, _stock in cart_lines:
                cursor.execute(
                    "INSERT INTO purchase_order_line "
                    "(order_id, bgg_id, name, unit_price, quantity, subtotal) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        order_id,
                        bgg_id,
                        name,
                        price,
                        quantity,
                        Decimal(str(price)) * quantity,
                    ],
                )
                lines.append(
                    {
                        "bgg_id": bgg_id,
                        "name": name,
                        "unit_price": price,
                        "quantity": quantity,
                    }
                )
            if coupon is not None:
                try:
                    _reserve_coupon(
                        cursor,
                        coupon[0],
                        order_id,
                        operation_hash,
                        payload_hash,
                        coupon_wins,
                    )
                except ValueError:
                    return CheckoutPayload(
                        user_errors=[
                            _error(
                                "COUPON_EXHAUSTED",
                                "Coupon has no remaining uses.",
                                ["couponCode"],
                            )
                        ]
                    )
            cursor.execute(
                "INSERT INTO credit_retry_attempt (order_id, user_id) VALUES (%s, %s) "
                "RETURNING attempt_id", [order_id, user.pk]
            )
            attempt_id = cursor.fetchone()[0]
            provider_lines = _discounted_payment_lines(
                [(row[0], row[1], row[2], row[3]) for row in cart_lines],
                savings,
            )
    preference_id, init_point = _preference(order_id, provider_lines)
    if not preference_id:
        with transaction.atomic(), connection.cursor() as cursor:
            _set_coupon_reservation_state(cursor, order_id, "REVIEW")
        return CheckoutPayload(
            user_errors=[
                _error(
                    "PAYMENT_INITIALIZATION_FAILED",
                    "Mercado Pago could not initialize payment.",
                )
            ]
        )
    return _complete_preference_attempt(
        user, order_id, attempt_id, preference_id, init_point
    )


def retry_payment(info, order_id):
    from .api import _authenticated_user

    user = _authenticated_user(info)
    if user is None:
        return CheckoutPayload(
            user_errors=[_error("UNAUTHENTICATED", "Authentication is required.")]
        )
    issue = payments.configuration_error()
    if issue:
        return CheckoutPayload(user_errors=[_error("PAYMENTS_DISABLED", issue)])
    try:
        identifier = uuid.UUID(str(order_id))
    except (ValueError, TypeError, AttributeError):
        return CheckoutPayload(
            user_errors=[_error("ORDER_NOT_FOUND", "Order not found.")]
        )
    with transaction.atomic(durable=True), connection.cursor() as cursor:
        _lock_credit_account(cursor, user.pk)
        order = _load_order(cursor, identifier, user.pk, lock=True)
        if not order:
            return CheckoutPayload(
                user_errors=[_error("ORDER_NOT_FOUND", "Order not found.")]
            )
        if order["status"] not in ("pending_payment", "rejected"):
            return CheckoutPayload(
                user_errors=[
                    _error("ORDER_NOT_PAYABLE", "This order cannot be paid again.")
                ]
            )
        cursor.execute(
            "SELECT 1 FROM credit_retry_guard WHERE order_id = %s AND user_id = %s",
            [identifier, user.pk],
        )
        if cursor.fetchone():
            return CheckoutPayload(
                user_errors=[_error("ORDER_NOT_PAYABLE", "This order cannot be paid again.")]
            )
        cursor.execute(
            "SELECT 1 FROM credit_payment_review review WHERE review.order_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM credit_payment_review_release release "
            "WHERE release.order_id = review.order_id AND release.payment_id = review.payment_id) LIMIT 1",
            [identifier],
        )
        if cursor.fetchone():
            return CheckoutPayload(
                user_errors=[_error("PAYMENT_REQUIRES_REVIEW", "Payment requires manual review.")]
            )
        coupon_retry_error = _rereserve_released_coupon(cursor, identifier, timezone.now())
        if coupon_retry_error:
            return CheckoutPayload(
                user_errors=[
                    _error(
                        f"COUPON_{coupon_retry_error}",
                        "Coupon is no longer eligible for this checkout.",
                        ["couponCode"],
                    )
                ]
            )
        cursor.execute(
            "INSERT INTO credit_retry_attempt (order_id, user_id) VALUES (%s, %s) "
            "ON CONFLICT (order_id) DO NOTHING RETURNING attempt_id", [identifier, user.pk]
        )
        created_attempt = cursor.fetchone()
        if created_attempt is None:
            return CheckoutPayload(
                user_errors=[_error("PENDING_PAYMENT", "A payment attempt requires reconciliation.")]
            )
        attempt_id = created_attempt[0]
        cursor.execute(
            "UPDATE purchase_order SET status = %s WHERE id = %s "
            "AND user_id = %s AND status IN ('pending_payment', 'rejected')",
            ["pending_payment", identifier, user.pk],
        )
        if cursor.rowcount == 0:
            return CheckoutPayload(
                user_errors=[_error("ORDER_NOT_PAYABLE", "This order cannot be paid again.")]
            )
        cursor.execute(
            "SELECT bgg_id, name, unit_price, quantity FROM purchase_order_line "
            "WHERE order_id = %s ORDER BY bgg_id",
            [identifier],
        )
        lines = [
            {"bgg_id": row[0], "name": row[1], "unit_price": row[2], "quantity": row[3]}
            for row in cursor.fetchall()
        ]
        subtotal = sum(
            (Decimal(str(line["unit_price"])) * line["quantity"] for line in lines),
            Decimal("0"),
        )
        savings = max(Decimal("0"), subtotal - Decimal(str(order["total"])))
        provider_lines = _discounted_payment_lines(
            [
                (line["bgg_id"], line["name"], line["unit_price"], line["quantity"])
                for line in lines
            ],
            savings,
        )
    preference_id, init_point = _preference(identifier, provider_lines)
    if not preference_id:
        return CheckoutPayload(
            user_errors=[
                _error(
                    "PAYMENT_INITIALIZATION_FAILED",
                    "Mercado Pago could not initialize payment.",
                )
            ]
        )
    return _complete_preference_attempt(user, identifier, attempt_id, preference_id, init_point)


def _complete_preference_attempt(user, identifier, attempt_id, preference_id, init_point):
    with transaction.atomic(), connection.cursor() as cursor:
        _lock_credit_account(cursor, user.pk)
        order = _load_order(cursor, identifier, user.pk, lock=True)
        if not order:
            return CheckoutPayload(
                user_errors=[_error("ORDER_NOT_FOUND", "Order not found.")]
            )
        cursor.execute(
            "SELECT attempt_id FROM credit_retry_attempt WHERE order_id = %s AND user_id = %s FOR UPDATE",
            [identifier, user.pk],
        )
        current_attempt = cursor.fetchone()
        if current_attempt is None or current_attempt[0] != attempt_id:
            cursor.execute(
                "INSERT INTO credit_retry_attempt_release_response (attempt_id, preference_id) "
                "SELECT attempt_id, %s FROM credit_retry_attempt_release WHERE attempt_id = %s "
                "ON CONFLICT (attempt_id) DO NOTHING",
                [preference_id, attempt_id],
            )
            return CheckoutPayload(
                user_errors=[_error("PENDING_PAYMENT", "A payment attempt requires reconciliation.")]
            )
        # Conditional update: a concurrent confirmation must never be overwritten.
        cursor.execute(
            "UPDATE purchase_order SET mp_preference_id = %s, status = %s, "
            "payment_status = NULL, payment_status_detail = NULL "
            "WHERE id = %s AND status IN ('pending_payment', 'rejected')",
            [preference_id, "pending_payment", identifier],
        )
        order_updated = cursor.rowcount != 0
        cursor.execute(
            "UPDATE credit_retry_attempt SET preference_id = %s WHERE order_id = %s AND attempt_id = %s",
            [preference_id, identifier, attempt_id],
        )
        cursor.execute(
            "UPDATE coupon_checkout_reservation SET init_point = %s, "
            "updated_at = CURRENT_TIMESTAMP WHERE order_id = %s",
            [init_point, identifier],
        )
        if not order_updated:
            return CheckoutPayload(
                user_errors=[
                    _error("ORDER_NOT_PAYABLE", "This order cannot be paid again.")
                ]
            )
        order = _load_order(cursor, identifier, user.pk)
        result_order = _order_payload(cursor, order)
    return CheckoutPayload(order=result_order, init_point=init_point, user_errors=[])


def confirm_payment(info, order_id, payment_id):
    from .api import _authenticated_user, _get_cart_id

    user = _authenticated_user(info)
    if user is None:
        return ConfirmPaymentPayload(
            user_errors=[_error("UNAUTHENTICATED", "Authentication is required.")]
        )
    if not isinstance(payment_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", payment_id):
        return ConfirmPaymentPayload(
            user_errors=[_error("PAYMENT_NOT_VERIFIED", "Mercado Pago payment could not be verified.")]
        )
    try:
        identifier = uuid.UUID(str(order_id))
    except (ValueError, TypeError, AttributeError):
        return ConfirmPaymentPayload(
            user_errors=[_error("ORDER_NOT_FOUND", "Order not found.")]
        )
    with connection.cursor() as cursor:
        order = _load_order(cursor, identifier, user.pk)
    if not order:
        return ConfirmPaymentPayload(
            user_errors=[_error("ORDER_NOT_FOUND", "Order not found.")]
        )
    try:
        result = payments.fetch_payment(payment_id)
        payment = result.get("response", {})
        returned_id = payment.get("id")
        if (result.get("status") != 200 or not payments.is_sandbox_payment(payment)
            or isinstance(returned_id, bool) or returned_id is None
            or str(returned_id) != payment_id):
            raise ValueError
        external_reference = str(payment.get("external_reference", ""))
        amount = Decimal(str(payment.get("transaction_amount")))
    except Exception:
        return ConfirmPaymentPayload(
            user_errors=[
                _error(
                    "PAYMENT_NOT_VERIFIED",
                    "Mercado Pago payment could not be verified.",
                )
            ]
        )
    if external_reference != str(identifier):
        return ConfirmPaymentPayload(
            user_errors=[
                _error(
                    "PAYMENT_ORDER_MISMATCH", "Payment does not belong to this order."
                )
            ]
        )
    status, detail = payment.get("status"), payment.get("status_detail")
    preference_id = payments.verified_preference(payment, str(identifier)) if status == "approved" else None
    with transaction.atomic():
        with connection.cursor() as cursor:
            _lock_credit_account(cursor, user.pk)
            order = _load_order(cursor, identifier, user.pk, lock=True)
            if not order:
                return ConfirmPaymentPayload(
                    user_errors=[_error("ORDER_NOT_FOUND", "Order not found.")]
                )
            cursor.execute(
                "SELECT 1 FROM credit_retry_guard WHERE order_id = %s AND user_id = %s",
                [identifier, user.pk],
            )
            if cursor.fetchone():
                if status == "approved":
                    cursor.execute(
                        "INSERT INTO credit_payment_review (order_id, payment_id) "
                        "VALUES (%s, %s) ON CONFLICT (order_id, payment_id) DO NOTHING",
                        [identifier, str(payment_id)],
                    )
                    return ConfirmPaymentPayload(
                        user_errors=[_error("PAYMENT_REQUIRES_REVIEW", "Payment requires manual review.")]
                    )
                return ConfirmPaymentPayload(
                    user_errors=[_error("ORDER_NOT_PAYABLE", "This order cannot be paid again.")]
                )
            cursor.execute(
                "SELECT 1 FROM credit_payment_review WHERE order_id = %s AND payment_id = %s",
                [identifier, str(payment_id)],
            )
            if cursor.fetchone():
                return ConfirmPaymentPayload(
                    user_errors=[_error("PAYMENT_REQUIRES_REVIEW", "Payment requires manual review.")]
                )
            cursor.execute(
                "SELECT 1 FROM credit_payment_review review WHERE review.order_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM credit_payment_review_release release "
                "WHERE release.order_id = review.order_id AND release.payment_id = review.payment_id) LIMIT 1",
                [identifier],
            )
            reviewed = cursor.fetchone() is not None
            if status == "approved" and (reviewed or not preference_id or preference_id != order["mp_preference_id"]):
                cursor.execute(
                    "INSERT INTO credit_payment_review (order_id, payment_id) "
                    "VALUES (%s, %s) ON CONFLICT (order_id, payment_id) DO NOTHING",
                    [identifier, str(payment_id)],
                )
                return ConfirmPaymentPayload(
                    user_errors=[_error("PAYMENT_REQUIRES_REVIEW", "Payment requires manual review.")]
                )
            if reviewed:
                return ConfirmPaymentPayload(
                    user_errors=[_error("PAYMENT_REQUIRES_REVIEW", "Payment requires manual review.")]
                )
            if (
                amount != Decimal(str(order["total"]))
                or payment.get("currency_id") != order["currency"]
            ):
                return ConfirmPaymentPayload(
                    user_errors=[
                        _error(
                            "PAYMENT_AMOUNT_MISMATCH",
                            "Payment amount or currency does not match the order.",
                        )
                    ]
                )
            if order["status"] not in ("paid", "paid_stock_conflict", "cancelled"):
                if status == "approved":
                    _get_cart_id(cursor, user)
                    cursor.execute(
                        "SELECT bgg_id, quantity FROM purchase_order_line "
                        "WHERE order_id = %s ORDER BY bgg_id",
                        [identifier],
                    )
                    lines = cursor.fetchall()
                    cursor.execute(
                        "SELECT p.bgg_id, p.quantity FROM product_boardgame p "
                        "JOIN purchase_order_line l ON l.bgg_id = p.bgg_id "
                        "WHERE l.order_id = %s ORDER BY p.bgg_id FOR UPDATE OF p",
                        [identifier],
                    )
                    stocks = dict(cursor.fetchall())
                    coupon_retry_error = _rereserve_released_coupon(
                        cursor, identifier, timezone.now()
                    )
                    if coupon_retry_error:
                        cursor.execute(
                            "INSERT INTO credit_payment_review (order_id, payment_id) "
                            "VALUES (%s, %s) ON CONFLICT (order_id, payment_id) DO NOTHING",
                            [identifier, str(payment_id)],
                        )
                        return ConfirmPaymentPayload(
                            user_errors=[
                                _error(
                                    "PAYMENT_REQUIRES_REVIEW",
                                    "Approved payment requires manual review.",
                                )
                            ]
                        )
                    if any(
                        stocks.get(bgg_id, -1) < quantity for bgg_id, quantity in lines
                    ):
                        cursor.execute(
                            "UPDATE purchase_order SET status = %s, mp_payment_id = %s, "
                            "payment_status = %s, payment_status_detail = %s WHERE id = %s",
                            [
                                "paid_stock_conflict",
                                str(payment_id),
                                status,
                                "insufficient_stock",
                                identifier,
                            ],
                        )
                    else:
                        for bgg_id, quantity in lines:
                            cursor.execute(
                                "UPDATE product_boardgame SET quantity = quantity - %s "
                                "WHERE bgg_id = %s",
                                [quantity, bgg_id],
                            )
                            record_catalog_event(cursor, "stock.changed", bgg_id)
                        now = timezone.now()
                        cursor.execute(
                            "UPDATE purchase_order SET status = %s, mp_payment_id = %s, "
                            "payment_status = %s, payment_status_detail = %s, paid_at = %s WHERE id = %s",
                            ["paid", str(payment_id), status, detail, now, identifier],
                        )
                        cursor.execute(
                            "INSERT INTO invoice (order_id, issued_at, total) "
                            "VALUES (%s, %s, %s) ON CONFLICT (order_id) DO NOTHING",
                            [identifier, now, order["total"]],
                        )
                        # Remove only this buyer's purchased Wishlist products.
                        for bgg_id, _quantity in lines:
                            cursor.execute(
                                "DELETE FROM wishlist_entry "
                                "WHERE user_id = %s AND bgg_id = %s",
                                [user.pk, bgg_id],
                            )
                        # Remove only what was purchased; keep later cart edits.
                        for bgg_id, quantity in lines:
                            cart_filter = (
                                "cart_id = (SELECT cart_id FROM shopping_cart "
                                "WHERE user_id = %s) AND bgg_id = %s"
                            )
                            cursor.execute(
                                f"DELETE FROM shopping_cart_line WHERE {cart_filter} "
                                "AND quantity <= %s",
                                [user.pk, bgg_id, quantity],
                            )
                            cursor.execute(
                                "UPDATE shopping_cart_line SET quantity = quantity - %s "
                                f"WHERE {cart_filter} AND quantity > %s",
                                [quantity, user.pk, bgg_id, quantity],
                            )
                elif status == "rejected":
                    cursor.execute(
                        "UPDATE purchase_order SET status = %s, mp_payment_id = %s, "
                        "payment_status = %s, payment_status_detail = %s WHERE id = %s",
                        [
                            "rejected",
                            str(payment_id),
                            status,
                            detail or "rejected",
                            identifier,
                        ],
                    )
                    _release_coupon_reservation(cursor, identifier)
                elif status in ("cancelled", "canceled"):
                    cursor.execute(
                        "UPDATE purchase_order SET status = %s, mp_payment_id = %s, "
                        "payment_status = %s, payment_status_detail = %s WHERE id = %s",
                        ["cancelled", str(payment_id), status, detail or status, identifier],
                    )
                    _release_coupon_reservation(cursor, identifier)
                else:
                    cursor.execute(
                        "UPDATE purchase_order SET payment_status = %s, "
                        "payment_status_detail = %s, mp_payment_id = %s WHERE id = %s",
                        [status, detail, str(payment_id), identifier],
                    )
            if status == "approved":
                _set_coupon_reservation_state(cursor, identifier, "CONSUMED")
            if status == "approved" and preference_id == order["mp_preference_id"]:
                cursor.execute(
                    "DELETE FROM credit_retry_attempt WHERE order_id = %s AND preference_id = %s",
                    [identifier, preference_id],
                )
            order = _load_order(cursor, identifier, user.pk)
            result_order = _order_payload(cursor, order)
    return ConfirmPaymentPayload(order=result_order, user_errors=[])


class CheckoutMutation(graphene.Mutation):
    class Arguments:
        shipping_mode = ShippingMode(required=True)
        address = ShippingAddressInput()
        cart_id = graphene.ID()
        coupon_code = graphene.String()
        checkout_key = graphene.String()

    Output = CheckoutPayload

    @staticmethod
    def mutate(
        _root, info, shipping_mode, address=None, cart_id=None,
        coupon_code=None, checkout_key=None,
    ):
        return checkout(
            info, shipping_mode, address, cart_id, coupon_code, checkout_key
        )


class RetryPaymentMutation(graphene.Mutation):
    class Arguments:
        order_id = graphene.ID(required=True)

    Output = CheckoutPayload

    @staticmethod
    def mutate(_root, info, order_id):
        return retry_payment(info, order_id)


class ConfirmPaymentMutation(graphene.Mutation):
    class Arguments:
        order_id = graphene.ID(required=True)
        payment_id = graphene.ID(required=True)

    Output = ConfirmPaymentPayload

    @staticmethod
    def mutate(_root, info, order_id, payment_id):
        return confirm_payment(info, order_id, payment_id)


def invoice_pdf(request, order_id):
    from types import SimpleNamespace
    from django.http import HttpResponse, HttpResponseNotFound
    from .api import _authenticated_user

    user = _authenticated_user(SimpleNamespace(context=request))
    if user is None:
        return HttpResponseNotFound()
    try:
        identifier = uuid.UUID(str(order_id))
    except (ValueError, TypeError, AttributeError):
        return HttpResponseNotFound()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT o.id, o.status, o.total, o.currency, o.created_at, "
            "i.number, i.issued_at FROM purchase_order o JOIN invoice i "
            "ON i.order_id = o.id WHERE o.id = %s AND o.user_id = %s",
            [identifier, user.pk],
        )
        invoice_data = cursor.fetchone()
        if not invoice_data or invoice_data[1] != "paid":
            return HttpResponseNotFound()
        cursor.execute(
            "SELECT name, unit_price, quantity, subtotal FROM purchase_order_line "
            "WHERE order_id = %s ORDER BY bgg_id",
            [identifier],
        )
        lines = cursor.fetchall()
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    content = BytesIO()
    pdf = canvas.Canvas(content, pagesize=A4)
    pdf.setTitle("Purchase invoice")
    pdf.drawString(48, 790, "Purchase invoice")
    pdf.drawString(48, 768, "Invoice number: " + str(invoice_data[5]))
    pdf.drawString(48, 748, "Order: " + str(invoice_data[0]))
    pdf.drawString(48, 728, "Issued at: " + invoice_data[6].isoformat())
    y = 690
    for name, unit_price, quantity, subtotal in lines:
        pdf.drawString(48, y, str(name)[:70])
        pdf.drawString(330, y, str(quantity) + " x " + str(unit_price))
        pdf.drawRightString(540, y, str(subtotal))
        y -= 20
        if y < 80:
            pdf.showPage()
            y = 790
    pdf.drawString(48, y - 16, "Total: " + str(invoice_data[2]) + " " + invoice_data[3])
    pdf.save()
    response = HttpResponse(content.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = (
        "attachment; filename=invoice-" + str(invoice_data[5]) + ".pdf"
    )
    return response
