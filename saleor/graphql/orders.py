"""Purchase order GraphQL types and operations."""

import uuid
from decimal import Decimal

import graphene
from django.db import connection, transaction
from django.utils import timezone

from . import payments


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
    if not response.get("id"):
        return None, None
    return str(response["id"]), response.get("init_point")


def checkout(info, shipping_mode, address=None, cart_id=None):
    from .api import _authenticated_user, _get_cart_id

    user = _authenticated_user(info)
    if user is None:
        return CheckoutPayload(
            user_errors=[_error("UNAUTHENTICATED", "Authentication is required.")]
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
    if mode == "standard" and any(
        not values[key] for key in ("recipient", "street", "city", "postal_code")
    ):
        return CheckoutPayload(
            user_errors=[
                _error(
                    "ADDRESS_REQUIRED",
                    "A complete shipping address is required.",
                    ["address"],
                )
            ]
        )
    with transaction.atomic():
        with connection.cursor() as cursor:
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
            total = sum(
                (Decimal(str(row[2])) * row[3] for row in cart_lines), Decimal("0")
            )
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
    preference_id, init_point = _preference(order_id, lines)
    if not preference_id:
        return CheckoutPayload(
            user_errors=[
                _error(
                    "PAYMENT_INITIALIZATION_FAILED",
                    "Mercado Pago could not initialize payment.",
                )
            ]
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE purchase_order SET mp_preference_id = %s WHERE id = %s",
            [preference_id, order_id],
        )
        order = _load_order(cursor, order_id, user.pk)
        result_order = _order_payload(cursor, order)
    return CheckoutPayload(order=result_order, init_point=init_point, user_errors=[])


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
    with connection.cursor() as cursor:
        order = _load_order(cursor, identifier, user.pk)
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
            "SELECT bgg_id, name, unit_price, quantity FROM purchase_order_line "
            "WHERE order_id = %s ORDER BY bgg_id",
            [identifier],
        )
        lines = [
            {"bgg_id": row[0], "name": row[1], "unit_price": row[2], "quantity": row[3]}
            for row in cursor.fetchall()
        ]
    preference_id, init_point = _preference(identifier, lines)
    if not preference_id:
        return CheckoutPayload(
            user_errors=[
                _error(
                    "PAYMENT_INITIALIZATION_FAILED",
                    "Mercado Pago could not initialize payment.",
                )
            ]
        )
    with connection.cursor() as cursor:
        # Conditional update: a concurrent confirmation must never be overwritten.
        cursor.execute(
            "UPDATE purchase_order SET mp_preference_id = %s, status = %s, "
            "payment_status = NULL, payment_status_detail = NULL "
            "WHERE id = %s AND status IN ('pending_payment', 'rejected')",
            [preference_id, "pending_payment", identifier],
        )
        if cursor.rowcount == 0:
            return CheckoutPayload(
                user_errors=[
                    _error("ORDER_NOT_PAYABLE", "This order cannot be paid again.")
                ]
            )
        order = _load_order(cursor, identifier, user.pk)
        result_order = _order_payload(cursor, order)
    return CheckoutPayload(order=result_order, init_point=init_point, user_errors=[])


def confirm_payment(info, order_id, payment_id):
    from .api import _authenticated_user

    user = _authenticated_user(info)
    if user is None:
        return ConfirmPaymentPayload(
            user_errors=[_error("UNAUTHENTICATED", "Authentication is required.")]
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
        if result.get("status") != 200 or payment.get("live_mode") is True:
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
    status, detail = payment.get("status"), payment.get("status_detail")
    with transaction.atomic():
        with connection.cursor() as cursor:
            order = _load_order(cursor, identifier, user.pk, lock=True)
            if order["status"] not in ("paid", "paid_stock_conflict", "cancelled"):
                if status == "approved":
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
                else:
                    cursor.execute(
                        "UPDATE purchase_order SET payment_status = %s, "
                        "payment_status_detail = %s, mp_payment_id = %s WHERE id = %s",
                        [status, detail, str(payment_id), identifier],
                    )
            order = _load_order(cursor, identifier, user.pk)
            result_order = _order_payload(cursor, order)
    return ConfirmPaymentPayload(order=result_order, user_errors=[])


class CheckoutMutation(graphene.Mutation):
    class Arguments:
        shipping_mode = ShippingMode(required=True)
        address = ShippingAddressInput()
        cart_id = graphene.ID()

    Output = CheckoutPayload

    @staticmethod
    def mutate(_root, info, shipping_mode, address=None, cart_id=None):
        return checkout(info, shipping_mode, address, cart_id)


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
