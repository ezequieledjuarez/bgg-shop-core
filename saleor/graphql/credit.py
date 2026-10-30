"""Buyer credit balances and single-use gift card redemptions."""

import hashlib
import hmac
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal, localcontext

import graphene
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .orders import (
    OrderUserError, PurchaseOrder, ShippingAddressInput, ShippingMode,
    _ADDRESS_LIMITS, _load_order, _order_payload,
)


MAX_BALANCE = Decimal("9999999999999999999999999999.99")
REJECTION_RETENTION = timedelta(days=30)
ERRORS = {
    "UNAUTHENTICATED": "Authentication required.",
    "INVALID_KEY": "A valid idempotency key is required.",
    "IDEMPOTENCY_CONFLICT": "The idempotency key was used for another request.",
    "INVALID_GIFT_CARD": "Gift card cannot be redeemed.",
    "INVALID_CURRENCY": "Credit currency must be ARS.",
    "INVALID_AMOUNT": "Credit amount is invalid or exceeds the supported precision.",
    "SERVER_ERROR": "Credit service is unavailable.",
    "ADDRESS_REQUIRED": "A valid shipping address is required.",
    "EMPTY_CART": "The cart is empty.",
    "INSUFFICIENT_STOCK": "One or more items have insufficient stock.",
    "INSUFFICIENT_CREDIT": "There is insufficient credit for this order.",
    "PENDING_PAYMENT": "A pending payment must be resolved before using credit.",
}


class CreditBalance(graphene.ObjectType):
    balance = graphene.String(required=True, description="Available credit expressed as a decimal string.")
    currency = graphene.String(required=True, description="The credit currency is ARS.")


class CreditBalancePayload(graphene.ObjectType):
    credit = graphene.Field(CreditBalance)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class RedeemGiftCardInput(graphene.InputObjectType):
    code = graphene.String(required=True)
    idempotency_key = graphene.String(
        required=True, description="A UUID scoped to the authenticated buyer and redemption operation."
    )


class CheckoutWithCreditInput(graphene.InputObjectType):
    shipping_mode = ShippingMode(required=True)
    address = graphene.InputField(ShippingAddressInput)
    idempotency_key = graphene.String(
        required=True, description="A UUID scoped to the authenticated buyer and credit checkout operation."
    )


class CreditCheckoutPayload(graphene.ObjectType):
    order = graphene.Field(PurchaseOrder)
    credit = graphene.Field(CreditBalance)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class CheckoutWithCreditMutation(graphene.Mutation):
    class Arguments:
        input = CheckoutWithCreditInput(required=True)

    Output = CreditCheckoutPayload

    @staticmethod
    def mutate(_root, info, input):
        from .api import _authenticated_user

        try:
            return checkout_for_user(_authenticated_user(info), input)
        except Exception:
            return _checkout_error("SERVER_ERROR")


def _checkout_error(code):
    return CreditCheckoutPayload(
        order=None, credit=None,
        user_errors=[OrderUserError(code=code, message=ERRORS[code])],
    )


def _checkout_fingerprint(request, key, secret):
    return hmac.new(
        secret.encode("utf-8"),
        b"credit-checkout\x00" + key.bytes + request,
        hashlib.sha256,
    ).digest()


def _purge_expired_rejections(cursor, user_id):
    cursor.execute(
        "DELETE FROM credit_idempotency WHERE user_id = %s AND error_code IS NOT NULL "
        "AND created_at <= %s",
        [user_id, timezone.now() - REJECTION_RETENTION],
    )


def _checkout_snapshot(cursor, order_id, user_id):
    order = _order_payload(cursor, _load_order(cursor, order_id, user_id))
    fields = (
        "id", "status", "total", "currency", "shipping_mode", "recipient", "street",
        "city", "postal_code", "phone", "payment_status", "payment_status_detail",
        "created_at", "paid_at",
    )
    snapshot = {field: getattr(order, field) for field in fields}
    snapshot["id"] = str(snapshot["id"])
    snapshot["total"] = float(snapshot["total"])
    snapshot["lines"] = [
        {field: getattr(line, field) for field in
         ("bgg_id", "name", "unit_price", "quantity", "subtotal")}
        for line in order.lines
    ]
    snapshot["invoice"] = {
        field: getattr(order.invoice, field) for field in ("number", "issued_at", "total")
    }
    return snapshot


def _checkout_result(snapshot, balance):
    return CreditCheckoutPayload(
        order=snapshot,
        credit=CreditBalance(balance=format(Decimal(str(balance)), ".2f"), currency="ARS"),
        user_errors=[],
    )


def _checkout_validate(cursor, user, mode, address, account):
    if account[1] != "ARS":
        return "INVALID_CURRENCY", None, None, None
    if any(value is not None and (not isinstance(value, str) or len(value) > _ADDRESS_LIMITS[key])
           for key, value in address.items()):
        return "ADDRESS_REQUIRED", None, None, None
    if mode == "standard" and any(not (address.get(key) or "").strip()
                                  for key in ("recipient", "street", "city", "postal_code")):
        return "ADDRESS_REQUIRED", None, None, None
    cursor.execute("SELECT 1 FROM credit_retry_attempt WHERE user_id = %s LIMIT 1", [user.pk])
    if cursor.fetchone() is not None:
        return "PENDING_PAYMENT", None, None, None
    cursor.execute(
        "SELECT 1 FROM credit_payment_review review "
        "JOIN purchase_order purchase ON purchase.id = review.order_id "
        "WHERE purchase.user_id = %s AND NOT EXISTS "
        "(SELECT 1 FROM credit_retry_guard guard WHERE guard.order_id = review.order_id) "
        "AND NOT EXISTS (SELECT 1 FROM credit_payment_review_release release "
        "WHERE release.order_id = review.order_id AND release.payment_id = review.payment_id) LIMIT 1",
        [user.pk],
    )
    if cursor.fetchone() is not None:
        return "PENDING_PAYMENT", None, None, None
    cursor.execute(
        "SELECT cart_id FROM shopping_cart WHERE user_id = %s" + _lock_clause(), [user.pk]
    )
    cart = cursor.fetchone()
    cursor.execute(
        "SELECT 1 FROM purchase_order WHERE user_id = %s AND status = %s LIMIT 1",
        [user.pk, "pending_payment"],
    )
    if cursor.fetchone() is not None:
        return "PENDING_PAYMENT", None, None, None
    if cart is None:
        return "EMPTY_CART", None, None, None
    cursor.execute(
        "SELECT p.bgg_id, p.name, p.price_amount, line.quantity, p.quantity "
        "FROM shopping_cart_line line JOIN product_boardgame p ON p.bgg_id = line.bgg_id "
        "WHERE line.cart_id = %s ORDER BY p.bgg_id" + _lock_clause(), [str(cart[0])]
    )
    lines = cursor.fetchall()
    if not lines:
        return "EMPTY_CART", None, None, None
    balance = Decimal(str(account[0]))
    if (not balance.is_finite() or balance < 0 or balance > MAX_BALANCE
            or balance.as_tuple().exponent < -2):
        return "INVALID_AMOUNT", None, None, None
    total = Decimal("0")
    with localcontext() as context:
        context.prec = 50
        for _bgg_id, _name, raw_price, quantity, stock in lines:
            price = Decimal(str(raw_price))
            if (not price.is_finite() or price < 0 or price.as_tuple().exponent < -2
                    or price > Decimal("9999999999.99") or quantity <= 0):
                return "INVALID_AMOUNT", None, None, None
            if stock < quantity:
                return "INSUFFICIENT_STOCK", None, None, None
            total += price * quantity
            if total > Decimal("9999999999.99"):
                return "INVALID_AMOUNT", None, None, None
        if balance < total:
            return "INSUFFICIENT_CREDIT", None, None, None
        remaining = balance - total
    return None, (str(cart[0]), lines), total, remaining


def _checkout_purchase(cursor, user, mode, address, cart, total, remaining):
    cart_id, lines = cart
    order_id = str(uuid.uuid4())
    now = timezone.now()
    cursor.execute(
        "UPDATE credit_account SET balance = %s WHERE user_id = %s", [remaining, user.pk]
    )
    cursor.execute(
        "INSERT INTO purchase_order (id, user_id, status, total, currency, shipping_mode, "
        "recipient, street, city, postal_code, phone, payment_status, payment_status_detail, paid_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [order_id, user.pk, "paid", total, "ARS", mode,
         *[address.get(key) for key in ("recipient", "street", "city", "postal_code", "phone")],
         "approved", "credit", now],
    )
    for bgg_id, name, raw_price, quantity, _stock in lines:
        price = Decimal(str(raw_price))
        cursor.execute(
            "INSERT INTO purchase_order_line (order_id, bgg_id, name, unit_price, quantity, subtotal) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [order_id, bgg_id, name, price, quantity, price * quantity],
        )
        cursor.execute("UPDATE product_boardgame SET quantity = quantity - %s WHERE bgg_id = %s",
                       [quantity, bgg_id])
    cursor.execute(
        "INSERT INTO credit_transaction (user_id, order_id, amount, currency) "
        "VALUES (%s, %s, %s, %s) RETURNING id", [user.pk, order_id, total, "ARS"]
    )
    transaction_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO credit_payment (order_id, user_id, transaction_id, amount, currency) "
        "VALUES (%s, %s, %s, %s, %s)", [order_id, user.pk, transaction_id, total, "ARS"]
    )
    cursor.execute("INSERT INTO invoice (order_id, issued_at, total) VALUES (%s, %s, %s)",
                   [order_id, now, total])
    for bgg_id, _name, _price, quantity, _stock in lines:
        cursor.execute("DELETE FROM wishlist_entry WHERE user_id = %s AND bgg_id = %s",
                       [user.pk, bgg_id])
        cursor.execute(
            "DELETE FROM shopping_cart_line WHERE cart_id = %s AND bgg_id = %s AND quantity <= %s",
            [cart_id, bgg_id, quantity],
        )
        cursor.execute(
            "UPDATE shopping_cart_line SET quantity = quantity - %s "
            "WHERE cart_id = %s AND bgg_id = %s AND quantity > %s",
            [quantity, cart_id, bgg_id, quantity],
        )
    return _checkout_snapshot(cursor, order_id, user.pk)


def checkout_for_user(user, input):
    if user is None:
        return _checkout_error("UNAUTHENTICATED")
    try:
        key = uuid.UUID(input.idempotency_key)
    except (ValueError, TypeError, AttributeError):
        return _checkout_error("INVALID_KEY")
    mode = str(getattr(input.shipping_mode, "value", input.shipping_mode)).lower()
    request = {"shipping_mode": mode}
    if "address" in input:
        request["address"] = dict(input.address) if input.address is not None else None
    request_bytes = json.dumps(request, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("utf-8")
    fingerprint = _checkout_fingerprint(request_bytes, key, settings.SECRET_KEY)
    address = request.get("address") or {}
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO credit_account (user_id, balance, currency) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO NOTHING", [user.pk, Decimal("0.00"), "ARS"]
            )
            cursor.execute("SELECT balance, currency FROM credit_account WHERE user_id = %s" + _lock_clause(),
                           [user.pk])
            account = cursor.fetchone()
            _purge_expired_rejections(cursor, user.pk)
            cursor.execute(
                "SELECT fingerprint, balance_snapshot, error_code, order_snapshot FROM credit_idempotency "
                "WHERE user_id = %s AND operation = %s AND idempotency_key = %s",
                [user.pk, "CHECKOUT", str(key)],
            )
            previous = cursor.fetchone()
            if previous is not None:
                keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
                if not any(hmac.compare_digest(bytes(previous[0]), _checkout_fingerprint(request_bytes, key, secret))
                           for secret in keys):
                    return _checkout_error("IDEMPOTENCY_CONFLICT")
                if previous[2]:
                    return _checkout_error(previous[2])
                snapshot = json.loads(previous[3]) if isinstance(previous[3], str) else previous[3]
                return _checkout_result(snapshot, previous[1])
            cursor.execute(
                "SELECT id, status FROM purchase_order WHERE user_id = %s "
                "AND status IN (%s, %s) ORDER BY id" + _lock_clause(),
                [user.pk, "pending_payment", "rejected"],
            )
            rejected_ids = [row[0] for row in cursor.fetchall() if row[1] == "rejected"]
            error, cart, total, remaining = _checkout_validate(cursor, user, mode, address, account)
            snapshot = None if error else _checkout_purchase(cursor, user, mode, address, cart, total, remaining)
            if snapshot is not None:
                for rejected_id in rejected_ids:
                    cursor.execute(
                        "INSERT INTO credit_retry_guard (order_id, user_id, credit_order_id) "
                        "VALUES (%s, %s, %s) ON CONFLICT (order_id) DO NOTHING",
                        [rejected_id, user.pk, snapshot["id"]],
                    )
            cursor.execute(
                "INSERT INTO credit_idempotency (user_id, operation, idempotency_key, fingerprint, "
                "balance_snapshot, error_code, order_snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [user.pk, "CHECKOUT", str(key), fingerprint, remaining, error,
                 json.dumps(snapshot) if snapshot is not None else None],
            )
            return _checkout_error(error) if error else _checkout_result(snapshot, remaining)
    except Exception:
        return _checkout_error("SERVER_ERROR")


def _error(code):
    return CreditBalancePayload(
        credit=None, user_errors=[OrderUserError(code=code, message=ERRORS[code])]
    )


def _balance(value):
    value = Decimal(str(value))
    if (not value.is_finite() or value < 0 or value > MAX_BALANCE
            or value.as_tuple().exponent < -2):
        raise ValueError("Invalid balance")
    return CreditBalancePayload(
        credit=CreditBalance(balance=format(value, ".2f"), currency="ARS"),
        user_errors=[],
    )


def _lock_clause():
    if connection.vendor != "postgresql":
        raise RuntimeError("Credit redemption requires PostgreSQL row locks")
    return " FOR UPDATE"


def balance_for_user(user):
    if user is None:
        return _error("UNAUTHENTICATED")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT balance, currency FROM credit_account WHERE user_id = %s",
                [user.pk],
            )
            row = cursor.fetchone()
        if row is None:
            return _balance(Decimal("0.00"))
        if row[1] != "ARS":
            return _error("INVALID_CURRENCY")
        return _balance(row[0])
    except Exception:
        return _error("SERVER_ERROR")


def _fingerprint(code_hash, key, secret):
    return hmac.new(
        secret.encode("utf-8"),
        b"gift-card-redemption\x00" + key.bytes + code_hash,
        hashlib.sha256,
    ).digest()


def redeem_for_user(user, code, idempotency_key):
    if user is None:
        return _error("UNAUTHENTICATED")
    try:
        key = uuid.UUID(idempotency_key)
    except (ValueError, TypeError, AttributeError):
        return _error("INVALID_KEY")

    if len(code) > 64:
        return _error("INVALID_GIFT_CARD")

    code_hash = hashlib.sha256(code.encode("utf-8", errors="surrogatepass")).digest()
    fingerprint = _fingerprint(code_hash, key, settings.SECRET_KEY)
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO credit_account (user_id, balance, currency) "
                "VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                [user.pk, Decimal("0.00"), "ARS"],
            )
            cursor.execute(
                "SELECT balance, currency FROM credit_account WHERE user_id = %s"
                + _lock_clause(), [user.pk],
            )
            account = cursor.fetchone()
            _purge_expired_rejections(cursor, user.pk)
            cursor.execute(
                "SELECT fingerprint, balance_snapshot, error_code "
                "FROM credit_idempotency WHERE user_id = %s AND operation = %s "
                "AND idempotency_key = %s",
                [user.pk, "REDEEM", str(key)],
            )
            previous = cursor.fetchone()
            if previous is not None:
                keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
                if not any(hmac.compare_digest(bytes(previous[0]), _fingerprint(code_hash, key, secret))
                           for secret in keys):
                    return _error("IDEMPOTENCY_CONFLICT")
                return _error(previous[2]) if previous[2] else _balance(previous[1])

            error_code = None
            new_balance = None
            if account[1] != "ARS":
                error_code = "INVALID_CURRENCY"
            elif not re.fullmatch(r"[0-9a-f]{64}", code):
                error_code = "INVALID_GIFT_CARD"
            else:
                cursor.execute(
                    "SELECT id, amount, currency, redeemed_at, redeemed_by "
                    "FROM gift_card WHERE code_hash = %s" + _lock_clause(),
                    [code_hash],
                )
                card = cursor.fetchone()
                if card is None or card[3] is not None or card[4] is not None:
                    error_code = "INVALID_GIFT_CARD"
                elif card[2] != "ARS":
                    error_code = "INVALID_GIFT_CARD"
                else:
                    amount = Decimal(str(card[1]))
                    current = Decimal(str(account[0]))
                    if (not amount.is_finite() or not current.is_finite()
                            or amount <= 0 or current < 0
                            or amount.as_tuple().exponent < -2
                            or current.as_tuple().exponent < -2):
                        error_code = "INVALID_AMOUNT"
                    else:
                        with localcontext() as context:
                            context.prec = 40
                            new_balance = current + amount
                        if new_balance > MAX_BALANCE:
                            error_code = "INVALID_AMOUNT"
                            new_balance = None
                    if error_code is None:
                        cursor.execute(
                            "UPDATE gift_card SET redeemed_at = CURRENT_TIMESTAMP, "
                            "redeemed_by = %s WHERE id = %s", [user.pk, card[0]],
                        )
                        cursor.execute(
                            "UPDATE credit_account SET balance = %s WHERE user_id = %s",
                            [new_balance, user.pk],
                        )
                        cursor.execute(
                            "INSERT INTO credit_transaction "
                            "(user_id, gift_card_id, amount, currency) VALUES (%s, %s, %s, %s)",
                            [user.pk, card[0], amount, "ARS"],
                        )
            cursor.execute(
                "INSERT INTO credit_idempotency "
                "(user_id, operation, idempotency_key, fingerprint, balance_snapshot, error_code) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [user.pk, "REDEEM", str(key), fingerprint, new_balance, error_code],
            )
            return _error(error_code) if error_code else _balance(new_balance)
    except Exception:
        return _error("SERVER_ERROR")


class RedeemGiftCardMutation(graphene.Mutation):
    class Arguments:
        input = RedeemGiftCardInput(required=True)

    Output = CreditBalancePayload

    @staticmethod
    def mutate(_root, info, input):
        from .api import _authenticated_user

        try:
            return redeem_for_user(_authenticated_user(info), input.code, input.idempotency_key)
        except Exception:
            return _error("SERVER_ERROR")