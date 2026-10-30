import datetime
import hashlib
import re
import secrets
import unicodedata
import uuid
from decimal import Decimal

import graphene
import jwt
import logging
from django.db import IntegrityError, connection, transaction
from graphql import GraphQLError

from . import wishlist
from .credit import (
    CheckoutWithCreditMutation, CreditBalancePayload, RedeemGiftCardMutation,
    _error, balance_for_user,
)
from .orders import (
    CheckoutMutation, ConfirmPaymentMutation, OrderUserError, PurchaseOrder, RetryPaymentMutation,
    _lock_credit_account,
)


class Product(graphene.ObjectType):
    bgg_id = graphene.Int(required=True)
    name = graphene.String(required=True)
    release_year = graphene.Int(required=True)
    min_players = graphene.Int(required=True)
    max_players = graphene.Int(required=True)
    playing_time_minutes = graphene.Int(required=True)
    min_age = graphene.Int(required=True)
    weight = graphene.Float(required=True)
    category = graphene.List(graphene.NonNull(graphene.String), required=True)
    price = graphene.Float(required=True)
    publisher = graphene.String(required=True)
    image_url = graphene.String(required=True)
    quantity = graphene.Int(required=True)


class ProductPage(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(Product), required=True)
    page = graphene.Int(required=True)
    page_size = graphene.Int(required=True)
    total = graphene.Int(required=True)
    total_pages = graphene.Int(required=True)


class PublicReview(graphene.ObjectType):
    rating = graphene.Int(required=True)
    body = graphene.String(required=True)
    created_at = graphene.String(required=True)


class ReviewSummary(graphene.ObjectType):
    approved_review_count = graphene.Int(required=True)
    average_rating = graphene.Float()


class ProductDetail(graphene.ObjectType):
    bgg_id = graphene.Int(required=True)
    name = graphene.String(required=True)
    release_year = graphene.Int(required=True)
    min_players = graphene.Int(required=True)
    max_players = graphene.Int(required=True)
    playing_time_minutes = graphene.Int(required=True)
    min_age = graphene.Int(required=True)
    weight = graphene.Float(required=True)
    category = graphene.List(graphene.NonNull(graphene.String), required=True)
    price = graphene.Float(required=True)
    publisher = graphene.String(required=True)
    image_url = graphene.String(required=True)
    quantity = graphene.Int(required=True)
    review_summary = graphene.Field(ReviewSummary, required=True)
    approved_reviews = graphene.List(graphene.NonNull(PublicReview), required=True)


class CartLine(graphene.ObjectType):
    bgg_id = graphene.Int(required=True)
    name = graphene.String(required=True)
    quantity = graphene.Int(required=True)
    inventory_quantity = graphene.Int(required=True)
    unit_price = graphene.Float(required=True)
    subtotal = graphene.Float(required=True)


class Cart(graphene.ObjectType):
    cart_id = graphene.ID(required=True)
    lines = graphene.List(graphene.NonNull(CartLine), required=True)
    subtotal = graphene.Float(required=True)


class CartUserError(graphene.ObjectType):
    field = graphene.List(graphene.NonNull(graphene.String))
    code = graphene.String(required=True)
    message = graphene.String(required=True)


class CartPayload(graphene.ObjectType):
    cart = graphene.Field(Cart, required=True)
    user_errors = graphene.List(graphene.NonNull(CartUserError), required=True)


class ReviewUserError(graphene.ObjectType):
    field = graphene.List(graphene.NonNull(graphene.String))
    code = graphene.String(required=True)
    message = graphene.String(required=True)


class CreateReviewPayload(graphene.ObjectType):
    created = graphene.Boolean(required=True)
    status = graphene.String()
    created_at = graphene.String()
    user_errors = graphene.List(graphene.NonNull(ReviewUserError), required=True)


class CreateReviewInput(graphene.InputObjectType):
    product_slug = graphene.String(required=True)
    rating = graphene.Int(required=True)
    body = graphene.String(required=True)


class Promotion(graphene.ObjectType):
    id = graphene.ID(required=True)
    name = graphene.String(required=True)
    stackable = graphene.Boolean(required=True)
    discount_pct = graphene.Float(required=True)
    starts_at = graphene.String()
    ends_at = graphene.String()
    created_at = graphene.String(required=True)


class Coupon(graphene.ObjectType):
    id = graphene.ID(required=True)
    code = graphene.String(required=True)
    promotion = graphene.Field(Promotion)
    remaining = graphene.BigInt(required=True)
    created_at = graphene.String(required=True)


class StockReservation(graphene.ObjectType):
    id = graphene.ID(required=True)
    bgg_id = graphene.Int(required=True)
    reserved_qty = graphene.Int(required=True)
    reason = graphene.String()
    expires_at = graphene.String()
    created_at = graphene.String(required=True)


def _slugify(value):
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", normalized.casefold())).strip("-")


def _product_from_row(row):
    return {
        "bgg_id": row["bgg_id"],
        "name": row["name"],
        "release_year": row["release_year"],
        "min_players": row["min_players"],
        "max_players": row["max_players"],
        "playing_time_minutes": row["playing_time_minutes"],
        "min_age": row["min_age"],
        "weight": row["weight"],
        "category": [item.strip() for item in (row["category"] or "").split(",") if item.strip()],
        "price": row["price_amount"],
        "publisher": row["publisher"],
        "image_url": row["image_url"],
        "quantity": row["quantity"],
    }


def _product_by_slug(cursor, slug):
    cursor.execute(
        "SELECT bgg_id, name, release_year, min_players, max_players, "
        "playing_time_minutes, min_age, weight, category, price_amount, "
        "publisher, image_url, quantity FROM product_boardgame"
    )
    columns = [column[0] for column in cursor.description]
    for row in cursor.fetchall():
        product = dict(zip(columns, row))
        if _slugify(product["name"]) == slug:
            return product
    return None


def _review_error(code, message, field=None):
    return ReviewUserError(field=field, code=code, message=message)


def _authenticated_user(info):
    from django.conf import settings
    from django.contrib.auth.models import User

    request = info.context
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    try:
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if claims.get("token_type") != "access":
            return None
        return User.objects.get(pk=int(claims["sub"]), is_active=True)
    except (KeyError, TypeError, ValueError, OverflowError, jwt.PyJWTError, User.DoesNotExist):
        return None


def _cart_error(code, message, field=None):
    return CartUserError(field=field, code=code, message=message)


def _get_cart_id(cursor, user, cart_id=None):
    if user is not None:
        cursor.execute(
            "SELECT cart_id FROM shopping_cart WHERE user_id = %s FOR UPDATE",
            [user.pk],
        )
        row = cursor.fetchone()
        if row:
            return str(row[0])
        identifier = uuid.uuid4()
        cursor.execute(
            "INSERT INTO shopping_cart (cart_id, user_id) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            [identifier, user.pk],
        )
        cursor.execute(
            "SELECT cart_id FROM shopping_cart WHERE user_id = %s FOR UPDATE",
            [user.pk],
        )
        return str(cursor.fetchone()[0])

    if cart_id is not None:
        try:
            identifier = uuid.UUID(str(cart_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise GraphQLError("Invalid cart identifier.") from error
        cursor.execute(
            "SELECT cart_id FROM shopping_cart "
            "WHERE cart_id = %s AND user_id IS NULL FOR UPDATE",
            [identifier],
        )
        row = cursor.fetchone()
        if row is None:
            raise GraphQLError("Invalid cart identifier.")
        return str(row[0])

    identifier = uuid.uuid4()
    cursor.execute(
        "INSERT INTO shopping_cart (cart_id) VALUES (%s)",
        [identifier],
    )
    return str(identifier)


def _cart_details(cursor, cart_id):
    cursor.execute(
        "SELECT p.bgg_id, p.name, line.quantity, p.quantity, p.price_amount "
        "FROM shopping_cart cart "
        "LEFT JOIN shopping_cart_line line ON line.cart_id = cart.cart_id "
        "LEFT JOIN product_boardgame p ON p.bgg_id = line.bgg_id "
        "WHERE cart.cart_id = %s ORDER BY p.name",
        [uuid.UUID(str(cart_id))],
    )
    lines = []
    for bgg_id, name, quantity, inventory_quantity, unit_price in cursor.fetchall():
        if bgg_id is None:
            continue
        price = float(unit_price)
        lines.append(CartLine(
            bgg_id=bgg_id,
            name=name,
            quantity=quantity,
            inventory_quantity=inventory_quantity,
            unit_price=price,
            subtotal=price * quantity,
        ))
    return Cart(
        cart_id=str(cart_id),
        lines=lines,
        subtotal=sum(line.subtotal for line in lines),
    )


def _cart_mutation(info, cart_id, bgg_id, quantity, action):
    user = _authenticated_user(info)
    with transaction.atomic(), connection.cursor() as cursor:
        identifier = _get_cart_id(cursor, user, cart_id)
        errors = []
        if action != "remove":
            if quantity is None or quantity < 1:
                errors.append(_cart_error(
                    "INVALID_QUANTITY", "Quantity must be a positive integer.", ["quantity"]
                ))
            else:
                cursor.execute(
                    "SELECT quantity FROM product_boardgame WHERE bgg_id = %s",
                    [bgg_id],
                )
                product = cursor.fetchone()
                if product is None:
                    errors.append(_cart_error(
                        "PRODUCT_NOT_FOUND", "The requested game does not exist.", ["bggId"]
                    ))
                else:
                    inventory_quantity = product[0]
                    cursor.execute(
                        "SELECT quantity FROM shopping_cart_line "
                        "WHERE cart_id = %s AND bgg_id = %s",
                        [uuid.UUID(identifier), bgg_id],
                    )
                    existing = cursor.fetchone()
                    if action == "update" and existing is None:
                        errors.append(_cart_error(
                            "CART_LINE_NOT_FOUND", "The game is not in this cart.", ["bggId"]
                        ))
                    else:
                        requested = quantity + (existing[0] if existing and action == "add" else 0)
                        if requested > inventory_quantity:
                            errors.append(_cart_error(
                                "INSUFFICIENT_STOCK",
                                "Requested quantity exceeds current inventory; adjusted to the available stock.",
                                ["quantity"],
                            ))
                            requested = inventory_quantity
                        if requested > 0:
                            cursor.execute(
                                "INSERT INTO shopping_cart_line (cart_id, bgg_id, quantity) "
                                "VALUES (%s, %s, %s) "
                                "ON CONFLICT (cart_id, bgg_id) "
                                "DO UPDATE SET quantity = EXCLUDED.quantity",
                                [uuid.UUID(identifier), bgg_id, requested],
                            )
        if action == "remove":
            cursor.execute(
                "DELETE FROM shopping_cart_line WHERE cart_id = %s AND bgg_id = %s",
                [uuid.UUID(identifier), bgg_id],
            )
        cart = _cart_details(cursor, identifier)
    return CartPayload(cart=cart, user_errors=errors)


def _merge_guest_cart(guest_cart_id, user):
    if not guest_cart_id:
        return
    try:
        guest_id = uuid.UUID(str(guest_cart_id))
    except (AttributeError, TypeError, ValueError):
        return
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT cart_id FROM shopping_cart "
            "WHERE cart_id = %s AND user_id IS NULL FOR UPDATE",
            [guest_id],
        )
        if cursor.fetchone() is None:
            return
        account_id = uuid.UUID(_get_cart_id(cursor, user))
        cursor.execute(
            "SELECT bgg_id, quantity FROM shopping_cart_line "
            "WHERE cart_id = %s FOR UPDATE",
            [guest_id],
        )
        guest_lines = cursor.fetchall()
        for bgg_id, guest_quantity in guest_lines:
            cursor.execute(
                "SELECT quantity FROM product_boardgame WHERE bgg_id = %s",
                [bgg_id],
            )
            product = cursor.fetchone()
            if product is None:
                continue
            inventory_quantity = product[0]
            cursor.execute(
                "SELECT quantity FROM shopping_cart_line "
                "WHERE cart_id = %s AND bgg_id = %s FOR UPDATE",
                [account_id, bgg_id],
            )
            account_line = cursor.fetchone()
            merged_quantity = min(
                inventory_quantity,
                guest_quantity + (account_line[0] if account_line else 0),
            )
            if merged_quantity:
                cursor.execute(
                    "INSERT INTO shopping_cart_line (cart_id, bgg_id, quantity) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (cart_id, bgg_id) "
                    "DO UPDATE SET quantity = EXCLUDED.quantity",
                    [account_id, bgg_id, merged_quantity],
                )
            else:
                cursor.execute(
                    "DELETE FROM shopping_cart_line "
                    "WHERE cart_id = %s AND bgg_id = %s",
                    [account_id, bgg_id],
                )
        cursor.execute("DELETE FROM shopping_cart WHERE cart_id = %s", [guest_id])


class PaymentReviewResolution(graphene.ObjectType):
    actor_id = graphene.ID(required=True)
    action = graphene.String(required=True)
    reason = graphene.String(required=True)
    resolved_at = graphene.String(required=True)


class PaymentReviewRelease(graphene.ObjectType):
    actor_id = graphene.ID(required=True)
    reason = graphene.String(required=True)
    released_at = graphene.String(required=True)


class PaymentReview(graphene.ObjectType):
    order_id = graphene.ID(required=True)
    payment_id = graphene.ID(required=True)
    user_id = graphene.ID(required=True)
    credit_order_id = graphene.ID()
    detected_at = graphene.String(required=True)
    resolution = graphene.Field(PaymentReviewResolution)
    release = graphene.Field(PaymentReviewRelease)


class PaymentReviewPage(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(PaymentReview), required=True)
    has_next = graphene.Boolean(required=True)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class PaymentAttempt(graphene.ObjectType):
    attempt_id = graphene.ID(required=True)
    order_id = graphene.ID(required=True)
    user_id = graphene.ID(required=True)
    order_status = graphene.String(required=True)
    preference_id = graphene.ID()


class PaymentAttemptPage(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(PaymentAttempt), required=True)
    has_next = graphene.Boolean(required=True)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class PaymentAttemptObservation(graphene.ObjectType):
    id = graphene.ID(required=True)
    attempt_id = graphene.ID(required=True)
    order_id = graphene.ID(required=True)
    actor_id = graphene.ID(required=True)
    preference_id = graphene.ID()
    statement = graphene.String(required=True)
    recorded_at = graphene.String(required=True)


class PaymentAttemptObservationPage(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(PaymentAttemptObservation), required=True)
    has_next = graphene.Boolean(required=True)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class PaymentAttemptRelease(graphene.ObjectType):
    attempt_id = graphene.ID(required=True)
    order_id = graphene.ID(required=True)
    observation_id = graphene.ID(required=True)
    actor_id = graphene.ID(required=True)
    preference_id = graphene.ID()
    reason = graphene.String(required=True)
    risk_acknowledged = graphene.Boolean(required=True)
    released_at = graphene.String(required=True)


class PaymentAttemptProviderResponse(graphene.ObjectType):
    preference_id = graphene.ID(required=True)
    recorded_at = graphene.String(required=True)


class PaymentAttemptReleasePayload(graphene.ObjectType):
    release = graphene.Field(PaymentAttemptRelease)
    provider_response = graphene.Field(PaymentAttemptProviderResponse)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


def _payment_attempt_release(attempt_id, record, provider_response=None):
    order_id, observation_id, actor_id, reason, preference_id, risk_acknowledged, released_at = record
    return PaymentAttemptReleasePayload(release=PaymentAttemptRelease(
        attempt_id=str(attempt_id), order_id=str(order_id), observation_id=str(observation_id),
        actor_id=str(actor_id), preference_id=preference_id, reason=reason,
        risk_acknowledged=risk_acknowledged, released_at=released_at.isoformat(),
    ), provider_response=(PaymentAttemptProviderResponse(preference_id=provider_response[0],
                            recorded_at=provider_response[1].isoformat()) if provider_response is not None else None),
        user_errors=[])


class Query(graphene.ObjectType):
    hello = graphene.String(default_value="Saleor API activa y respondiendo OK")
    me = graphene.Field(lambda: AuthUser)
    product_by_slug = graphene.Field(
        ProductDetail,
        slug=graphene.String(required=True),
    )
    products = graphene.Field(
        ProductPage,
        page=graphene.Int(default_value=1),
        page_size=graphene.Int(default_value=50),
        search=graphene.String(),
        year_published=graphene.Int(),
        min_players=graphene.Int(),
        max_players=graphene.Int(),
        category_id=graphene.Int(),
        price_min=graphene.Float(),
        price_max=graphene.Float(),
    )
    cart = graphene.Field(Cart, required=True, cart_id=graphene.ID())
    my_wishlist = graphene.List(graphene.NonNull(Product), required=True)
    my_orders = graphene.List(graphene.NonNull(PurchaseOrder), required=True)
    my_order = graphene.Field(PurchaseOrder, id=graphene.ID(required=True))
    my_credit = graphene.Field(
        CreditBalancePayload, required=True,
        description="Returns the authenticated buyer's ARS balance, or zero if no credit exists.",
    )
    admin_payment_reviews = graphene.Field(
        PaymentReviewPage, required=True,
        page=graphene.Int(default_value=1), page_size=graphene.Int(default_value=50),
    )
    admin_payment_attempts = graphene.Field(
        PaymentAttemptPage, required=True,
        page=graphene.Int(default_value=1), page_size=graphene.Int(default_value=50),
    )
    admin_payment_attempt_observations = graphene.Field(
        PaymentAttemptObservationPage, required=True, attempt_id=graphene.ID(required=True),
        page=graphene.Int(default_value=1), page_size=graphene.Int(default_value=50),
    )
    admin_payment_attempt_release = graphene.Field(
        PaymentAttemptReleasePayload, required=True, attempt_id=graphene.ID(required=True),
    )
    promotions = graphene.List(graphene.NonNull(Promotion), required=True)
    coupon_by_code = graphene.Field(Coupon, code=graphene.String(required=True))
    stock_reservations = graphene.List(graphene.NonNull(StockReservation), bgg_id=graphene.Int(), required=True)

    def resolve_hello(self, info):
        return "Saleor API activa y respondiendo OK"

    def resolve_me(self, info):
        user = _authenticated_user(info)
        if user is None:
            return None
        return _auth_user(user)

    def resolve_my_credit(self, info):
        try:
            return balance_for_user(_authenticated_user(info))
        except Exception:
            return _error("SERVER_ERROR")

    def resolve_admin_payment_attempt_release(self, info, attempt_id):
        try:
            user = _authenticated_user(info)
        except Exception:
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not load payment attempt release.")])
        if user is None:
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="FORBIDDEN", message="Administrator permission required.")])
        try:
            identifier = uuid.UUID(str(attempt_id))
        except (ValueError, TypeError, AttributeError):
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="INVALID", message="Invalid attempt identifier.")])
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT order_id, observation_id, actor_user_id, reason, preference_id, risk_acknowledged, released_at "
                    "FROM credit_retry_attempt_release WHERE attempt_id = %s", [identifier],
                )
                record = cursor.fetchone()
                provider_response = None
                if record is not None:
                    cursor.execute(
                        "SELECT preference_id, recorded_at FROM credit_retry_attempt_release_response WHERE attempt_id = %s",
                        [identifier],
                    )
                    provider_response = cursor.fetchone()
            if record is None:
                return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment attempt release not found.")])
            return _payment_attempt_release(identifier, record, provider_response)
        except Exception:
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not load payment attempt release.")])

    def resolve_admin_payment_attempt_observations(self, info, attempt_id, page=1, page_size=50):
        try:
            user = _authenticated_user(info)
        except Exception:
            return PaymentAttemptObservationPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="SERVER_ERROR", message="Could not load payment attempt observations.")
            ])
        if user is None:
            return PaymentAttemptObservationPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")
            ])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return PaymentAttemptObservationPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="FORBIDDEN", message="Administrator permission required.")
            ])
        try:
            identifier = uuid.UUID(str(attempt_id))
            if page is None or page_size is None or not 1 <= page <= 10000 or not 1 <= page_size <= 100:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return PaymentAttemptObservationPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="INVALID", message="Invalid observation query.")
            ])
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, attempt_id, order_id, actor_user_id, preference_id, statement, recorded_at "
                    "FROM credit_retry_attempt_observation WHERE attempt_id = %s "
                    "ORDER BY recorded_at DESC, id DESC LIMIT %s OFFSET %s",
                    [identifier, page_size + 1, (page - 1) * page_size],
                )
                rows = cursor.fetchall()
            return PaymentAttemptObservationPage(
                items=[PaymentAttemptObservation(id=str(record_id), attempt_id=str(attempt), order_id=str(order_id),
                       actor_id=str(actor_id), preference_id=preference_id, statement=statement,
                       recorded_at=recorded_at.isoformat())
                       for record_id, attempt, order_id, actor_id, preference_id, statement, recorded_at in rows[:page_size]],
                has_next=len(rows) > page_size, user_errors=[],
            )
        except Exception:
            return PaymentAttemptObservationPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="SERVER_ERROR", message="Could not load payment attempt observations.")
            ])

    def resolve_admin_payment_attempts(self, info, page=1, page_size=50):
        try:
            user = _authenticated_user(info)
        except Exception:
            return PaymentAttemptPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="SERVER_ERROR", message="Could not load payment attempts.")
            ])
        if user is None:
            return PaymentAttemptPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")
            ])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return PaymentAttemptPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="FORBIDDEN", message="Administrator permission required.")
            ])
        if page is None or page_size is None or not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            return PaymentAttemptPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="INVALID", message="Invalid pagination.")
            ])
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT attempt.attempt_id, attempt.order_id, attempt.user_id, purchase.status, attempt.preference_id "
                    "FROM credit_retry_attempt attempt JOIN purchase_order purchase "
                    "ON purchase.id = attempt.order_id AND purchase.user_id = attempt.user_id "
                    "ORDER BY attempt.order_id LIMIT %s OFFSET %s",
                    [page_size + 1, (page - 1) * page_size],
                )
                rows = cursor.fetchall()
            return PaymentAttemptPage(
                  items=[PaymentAttempt(attempt_id=str(attempt_id), order_id=str(order_id), user_id=str(user_id),
                                      order_status=status, preference_id=preference_id)
                      for attempt_id, order_id, user_id, status, preference_id in rows[:page_size]],
                has_next=len(rows) > page_size, user_errors=[],
            )
        except Exception:
            return PaymentAttemptPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="SERVER_ERROR", message="Could not load payment attempts.")
            ])

    def resolve_admin_payment_reviews(self, info, page=1, page_size=50):
        try:
            user = _authenticated_user(info)
        except Exception:
            return PaymentReviewPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="SERVER_ERROR", message="Could not load payment reviews.")
            ])
        if user is None:
            return PaymentReviewPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")
            ])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return PaymentReviewPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="FORBIDDEN", message="Administrator permission required.")
            ])
        if page is None or page_size is None or not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            return PaymentReviewPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="INVALID", message="Invalid pagination.")
            ])
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT review.order_id, review.payment_id, purchase.user_id, "
                    "guard.credit_order_id, review.detected_at, resolution.actor_user_id, "
                    "resolution.action, resolution.reason, resolution.resolved_at, "
                    "release.actor_user_id, release.reason, release.released_at "
                    "FROM credit_payment_review review "
                    "JOIN purchase_order purchase ON purchase.id = review.order_id "
                    "LEFT JOIN credit_retry_guard guard ON guard.order_id = review.order_id "
                    "LEFT JOIN credit_payment_review_resolution resolution "
                    "ON resolution.order_id = review.order_id AND resolution.payment_id = review.payment_id "
                    "LEFT JOIN credit_payment_review_release release "
                    "ON release.order_id = review.order_id AND release.payment_id = review.payment_id "
                    "ORDER BY review.detected_at DESC, review.order_id DESC, review.payment_id DESC "
                    "LIMIT %s OFFSET %s",
                    [page_size + 1, (page - 1) * page_size],
                )
                rows = cursor.fetchall()
            return PaymentReviewPage(
                items=[PaymentReview(order_id=str(order_id), payment_id=payment_id, user_id=str(user_id),
                                     credit_order_id=str(credit_order_id) if credit_order_id is not None else None, detected_at=detected_at.isoformat(),
                                     resolution=(PaymentReviewResolution(actor_id=str(actor_id), action=action,
                                                 reason=reason, resolved_at=resolved_at.isoformat())
                                                 if actor_id is not None else None),
                                     release=(PaymentReviewRelease(actor_id=str(release_actor_id), reason=release_reason,
                                              released_at=released_at.isoformat()) if release_actor_id is not None else None))
                       for order_id, payment_id, user_id, credit_order_id, detected_at, actor_id,
                           action, reason, resolved_at, release_actor_id, release_reason, released_at in rows[:page_size]],
                has_next=len(rows) > page_size, user_errors=[],
            )
        except Exception:
            return PaymentReviewPage(items=[], has_next=False, user_errors=[
                OrderUserError(code="SERVER_ERROR", message="Could not load payment reviews.")
            ])

    def resolve_product_by_slug(self, info, slug):
        with connection.cursor() as cursor:
            product = _product_by_slug(cursor, slug)
            if product is None:
                return None
            cursor.execute(
                "SELECT rating, body, created_at FROM product_review "
                "WHERE bgg_id = %s AND status = 'APPROVED' ORDER BY created_at DESC",
                [product["bgg_id"]],
            )
            reviews = cursor.fetchall()
        public_reviews = [
            PublicReview(rating=rating, body=body, created_at=created_at.isoformat())
            for rating, body, created_at in reviews
        ]
        average = (
            sum(review.rating for review in public_reviews) / len(public_reviews)
            if public_reviews else None
        )
        return ProductDetail(
            **_product_from_row(product),
            review_summary=ReviewSummary(
                approved_review_count=len(public_reviews),
                average_rating=average,
            ),
            approved_reviews=public_reviews,
        )

    def resolve_products(self, info, page=1, page_size=50, search=None,
                         year_published=None, min_players=None, max_players=None,
                         category_id=None, price_min=None, price_max=None):
        if page < 1 or page_size < 1:
            raise ValueError("page and pageSize must be positive integers")
        if min_players is not None and max_players is not None and min_players > max_players:
            raise ValueError("minPlayers must be less than or equal to maxPlayers")
        if price_min is not None and price_max is not None and price_min > price_max:
            raise ValueError("priceMin must be less than or equal to priceMax")
        with connection.cursor() as cursor:
            conditions, parameters = [], []
            if search:
                conditions.append("(LOWER(name) LIKE LOWER(%s) OR LOWER(publisher) LIKE LOWER(%s))")
                parameters.extend([f"%{search}%", f"%{search}%"])
            for value, column, minimum, maximum in (
                (year_published, "release_year", 0, 9999),
                (min_players, "max_players", 1, 999),
                (max_players, "min_players", 1, 999),
            ):
                if value is not None:
                    conditions.append(f"{column} {'>=' if column == 'max_players' else '<=' if column == 'min_players' else '='} %s")
                    parameters.append(value)
            if category_id is not None:
                names = {1: "Juegos de Estrategia", 2: "Juegos Temáticos", 3: "Juegos Familiares"}
                category = names.get(category_id)
                if category:
                    conditions.append(
                        "(category = %s OR category LIKE %s OR category LIKE %s OR category LIKE %s)"
                    )
                    parameters.extend([
                        category, f"{category},%", f"%, {category}", f"%, {category},%"
                    ])
                else:
                    conditions.append("1 = 0")
            if price_min is not None:
                conditions.append("price_amount >= %s")
                parameters.append(price_min)
            if price_max is not None:
                conditions.append("price_amount <= %s")
                parameters.append(price_max)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            cursor.execute(f"SELECT COUNT(*) FROM product_boardgame {where}", parameters)
            total = cursor.fetchone()[0]
            cursor.execute(
                f"SELECT bgg_id, name, release_year, min_players, max_players, playing_time_minutes, "
                f"min_age, weight, category, price_amount, publisher, image_url, quantity "
                f"FROM product_boardgame {where} ORDER BY id LIMIT %s OFFSET %s",
                [*parameters, page_size, (page - 1) * page_size],
            )
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return ProductPage(
            items=[Product(
                bgg_id=row["bgg_id"], name=row["name"], release_year=row["release_year"],
                min_players=row["min_players"], max_players=row["max_players"],
                playing_time_minutes=row["playing_time_minutes"], min_age=row["min_age"],
                weight=row["weight"], price=row["price_amount"], publisher=row["publisher"],
                image_url=row["image_url"],
                quantity=row["quantity"],
                category=[item.strip() for item in (row["category"] or "").split(",") if item.strip()],
            ) for row in rows],
            page=page, page_size=page_size, total=total,
            total_pages=(total + page_size - 1) // page_size,
        )

    def resolve_cart(self, info, cart_id=None):
        user = _authenticated_user(info)
        with transaction.atomic(), connection.cursor() as cursor:
            identifier = _get_cart_id(cursor, user, cart_id)
            return _cart_details(cursor, identifier)

    def resolve_my_wishlist(self, info):
        return wishlist.list_products(info)

    def resolve_my_orders(self, info):
        user = _authenticated_user(info)
        if user is None:
            raise GraphQLError("Authentication is required.")
        from .orders import _load_order, _order_payload

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM purchase_order WHERE user_id = %s ORDER BY created_at DESC",
                [user.pk],
            )
            identifiers = [row[0] for row in cursor.fetchall()]
            return [
                _order_payload(cursor, _load_order(cursor, identifier, user.pk))
                for identifier in identifiers
            ]

    def resolve_my_order(self, info, id):
        user = _authenticated_user(info)
        if user is None:
            raise GraphQLError("Authentication is required.")
        from .orders import _order_by_id

        return _order_by_id(info, id, user)

    def resolve_promotions(self, info):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, stackable, discount_pct, starts_at, ends_at, created_at FROM promotion"
            )
            rows = cursor.fetchall()
            columns = [c[0] for c in cursor.description]
        result = []
        for row in rows:
            r = dict(zip(columns, row))
            result.append(Promotion(
                id=r["id"], name=r["name"], stackable=r["stackable"],
                discount_pct=float(r["discount_pct"]),
                starts_at=r["starts_at"].isoformat() if r["starts_at"] else None,
                ends_at=r["ends_at"].isoformat() if r["ends_at"] else None,
                created_at=r["created_at"].isoformat(),
            ))
        return result

    def resolve_coupon_by_code(self, info, code):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.id, c.code, c.promotion_id, c.remaining, c.created_at, p.name, p.stackable, p.discount_pct, p.starts_at, p.ends_at, p.created_at as p_created "
                "FROM coupon c LEFT JOIN promotion p ON p.id = c.promotion_id WHERE c.code = %s",
                [code],
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cursor.description]
            r = dict(zip(cols, row))
        promotion = None
        if r.get("promotion_id"):
            promotion = Promotion(
                id=r.get("promotion_id"), name=r.get("name"), stackable=r.get("stackable"),
                discount_pct=float(r.get("discount_pct") or 0),
                starts_at=r.get("starts_at").isoformat() if r.get("starts_at") else None,
                ends_at=r.get("ends_at").isoformat() if r.get("ends_at") else None,
                created_at=r.get("p_created").isoformat() if r.get("p_created") else None,
            )
        return Coupon(id=r["id"], code=r["code"], promotion=promotion, remaining=r["remaining"], created_at=r["created_at"].isoformat())

    def resolve_stock_reservations(self, info, bgg_id=None):
        with connection.cursor() as cursor:
            if bgg_id is None:
                cursor.execute(
                    "SELECT id, bgg_id, reserved_qty, reason, expires_at, created_at FROM stock_reservation ORDER BY created_at DESC"
                )
                rows = cursor.fetchall()
            else:
                cursor.execute(
                    "SELECT id, bgg_id, reserved_qty, reason, expires_at, created_at FROM stock_reservation WHERE bgg_id = %s ORDER BY created_at DESC",
                    [bgg_id],
                )
                rows = cursor.fetchall()
            cols = [c[0] for c in cursor.description]
        result = []
        for row in rows:
            r = dict(zip(cols, row))
            result.append(StockReservation(
                id=r["id"], bgg_id=r["bgg_id"], reserved_qty=r["reserved_qty"],
                reason=r.get("reason"),
                expires_at=r.get("expires_at").isoformat() if r.get("expires_at") else None,
                created_at=r.get("created_at").isoformat(),
            ))
        return result


class RegisterInput(graphene.InputObjectType):
    email = graphene.String(required=True)
    password = graphene.String(required=True)
    first_name = graphene.String(required=True)
    last_name = graphene.String(required=True)


class LoginInput(graphene.InputObjectType):
    email = graphene.String(required=True)
    password = graphene.String(required=True)
    guest_cart_id = graphene.ID()


class AuthUser(graphene.ObjectType):
    class Meta:
        name = "User"

    id = graphene.ID(required=True)
    email = graphene.String(required=True)
    first_name = graphene.String(required=True)
    last_name = graphene.String(required=True)
    is_active = graphene.Boolean(required=True)
    is_staff = graphene.Boolean(required=True)
    is_superuser = graphene.Boolean(required=True)


class AuthToken(graphene.ObjectType):
    token = graphene.String()
    refresh_token = graphene.String()
    csrf_token = graphene.String()
    op_key = graphene.String()
    op_key_expires_at = graphene.String()
    ops = graphene.List(graphene.String)


class UserError(graphene.ObjectType):
    field = graphene.List(graphene.NonNull(graphene.String))
    code = graphene.String(required=True)
    message = graphene.String(required=True)


class RegisterPayload(graphene.ObjectType):
    user = graphene.Field(AuthUser)
    tokens = graphene.Field(AuthToken)
    errors = graphene.List(graphene.NonNull(UserError), required=True)


class LoginPayload(RegisterPayload):
    pass


def _auth_error(message, code, field=None):
    return UserError(field=field, code=code, message=message)


def _auth_user(user):
    return AuthUser(
        id=user.pk,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
        is_staff=getattr(user, "is_staff", False),
        is_superuser=getattr(user, "is_superuser", False),
    )


def _auth_tokens(user_id):
    from django.conf import settings

    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {"sub": str(user_id), "iat": now}
    return AuthToken(
        token=jwt.encode({**claims, "exp": now + datetime.timedelta(minutes=15),
                          "token_type": "access"}, settings.SECRET_KEY, algorithm="HS256"),
        refresh_token=jwt.encode({**claims, "exp": now + datetime.timedelta(days=30),
                                  "token_type": "refresh"}, settings.SECRET_KEY, algorithm="HS256"),
    )


class RegisterMutation(graphene.Mutation):
    class Arguments:
        input = RegisterInput(required=True)

    Output = RegisterPayload

    @staticmethod
    def mutate(_root, _info, input):
        from django.contrib.auth.models import User

        email = input.email.strip().casefold()
        if User.objects.filter(email__iexact=email).exists():
            return RegisterPayload(errors=[_auth_error(
                "An account with this email already exists.", "DUPLICATE_EMAIL", ["email"]
            )])
        user = User.objects.create_user(
            username=email, email=email, password=input.password,
            first_name=input.first_name.strip(), last_name=input.last_name.strip(),
        )
        return RegisterPayload(user=_auth_user(user), errors=[])


class LoginMutation(graphene.Mutation):
    class Arguments:
        input = LoginInput(required=True)

    Output = LoginPayload

    @staticmethod
    def mutate(_root, info, input):
        from django.contrib.auth import authenticate
        # OpKeys: create a one-time OpSession for privileged users
        from opkeys.utils import generate_raw_key, key_hash, default_expiry
        from opkeys.models import OpSession

        user = authenticate(
            request=info.context, username=input.email.strip().casefold(), password=input.password
        )
        if user is None or not user.is_active:
            return LoginPayload(errors=[_auth_error(
                "Invalid email or password.", "INVALID_CREDENTIALS"
            )])
        if input.guest_cart_id:
            _merge_guest_cart(input.guest_cart_id, user)
        tokens = _auth_tokens(user.pk)

        # Issue op_key only when enabled in settings and for staff/superusers
        from django.conf import settings as dj_settings
        try:
            if getattr(dj_settings, "OPKEYS_ISSUE_ON_LOGIN", False) and (
                getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
            ):
                raw = generate_raw_key()
                h = key_hash(raw)
                # default ops: admin endpoints wildcard for main HTTP methods
                methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
                ops_list = [f"{m}:/api/v1/admin/*" for m in methods]
                sess = OpSession.objects.create(user=user, key_hash=h, ops=ops_list, expires_at=default_expiry())
                # expose raw op_key only once in the login payload
                tokens.op_key = raw
                tokens.op_key_expires_at = sess.expires_at.isoformat() if sess.expires_at else None
                tokens.ops = sess.ops
        except Exception:
            # don't break login if op session creation fails
            pass

        return LoginPayload(user=_auth_user(user), tokens=tokens, errors=[])


class AddToCartMutation(graphene.Mutation):
    class Arguments:
        cart_id = graphene.ID()
        bgg_id = graphene.Int(required=True)
        quantity = graphene.Int(required=True)

    Output = CartPayload

    @staticmethod
    def mutate(_root, info, bgg_id, quantity, cart_id=None):
        return _cart_mutation(info, cart_id, bgg_id, quantity, "add")


class UpdateCartQuantityMutation(graphene.Mutation):
    class Arguments:
        cart_id = graphene.ID()
        bgg_id = graphene.Int(required=True)
        quantity = graphene.Int(required=True)

    Output = CartPayload

    @staticmethod
    def mutate(_root, info, bgg_id, quantity, cart_id=None):
        return _cart_mutation(info, cart_id, bgg_id, quantity, "update")


class RemoveFromCartMutation(graphene.Mutation):
    class Arguments:
        cart_id = graphene.ID()
        bgg_id = graphene.Int(required=True)

    Output = CartPayload

    @staticmethod
    def mutate(_root, info, bgg_id, cart_id=None):
        return _cart_mutation(info, cart_id, bgg_id, None, "remove")


class AddToWishlistMutation(graphene.Mutation):
    class Arguments:
        bgg_id = graphene.Int(required=True)

    Output = Product

    @staticmethod
    def mutate(_root, info, bgg_id):
        return wishlist.add_product(info, bgg_id)


class RemoveFromWishlistMutation(graphene.Mutation):
    class Arguments:
        bgg_id = graphene.Int(required=True)

    Output = Product

    @staticmethod
    def mutate(_root, info, bgg_id):
        return wishlist.remove_product(info, bgg_id)


class CreateReviewMutation(graphene.Mutation):
    class Arguments:
        input = CreateReviewInput(required=True)

    Output = CreateReviewPayload

    @staticmethod
    def mutate(_root, info, input):
        user = _authenticated_user(info)
        if user is None:
            return CreateReviewPayload(
                created=False,
                user_errors=[_review_error(
                    "UNAUTHENTICATED", "Authentication is required."
                )],
            )
        body = input.body.strip()
        if not 1 <= input.rating <= 5:
            return CreateReviewPayload(
                created=False,
                user_errors=[_review_error(
                    "RATING_OUT_OF_RANGE", "Rating must be between 1 and 5.", ["rating"]
                )],
            )
        if not body:
            return CreateReviewPayload(
                created=False,
                user_errors=[_review_error(
                    "BODY_REQUIRED", "Review text is required.", ["body"]
                )],
            )
        if len(body) > 2000:
            return CreateReviewPayload(
                created=False,
                user_errors=[_review_error(
                    "BODY_TOO_LONG", "Review text must not exceed 2000 characters.", ["body"]
                )],
            )

        with connection.cursor() as cursor:
            product = _product_by_slug(cursor, input.product_slug)
            if product is None:
                return CreateReviewPayload(
                    created=False,
                    user_errors=[_review_error(
                        "PRODUCT_NOT_FOUND", "The requested game does not exist.", ["productSlug"]
                    )],
                )
            try:
                cursor.execute(
                    "INSERT INTO product_review (bgg_id, user_id, rating, body, status) "
                    "VALUES (%s, %s, %s, %s, 'PENDING') "
                    "RETURNING created_at",
                    [product["bgg_id"], user.pk, input.rating, body],
                )
                created_at = cursor.fetchone()[0]
            except IntegrityError:
                return CreateReviewPayload(
                    created=False,
                    user_errors=[_review_error(
                        "REVIEW_ALREADY_EXISTS",
                        "You already reviewed this game.",
                    )],
                )
        return CreateReviewPayload(
            created=True,
            status="PENDING",
            created_at=created_at.isoformat(),
            user_errors=[],
        )


class CreateProductInput(graphene.InputObjectType):
    name = graphene.String(required=True)
    price = graphene.Float(required=True)
    quantity = graphene.Int(required=True)
    # Business id provided by client
    bggId = graphene.Int()
    publisher = graphene.String()
    # Contract: accept camelCase fields only
    imageUrl = graphene.String()
    imageFileName = graphene.String()
    releaseYear = graphene.Int()
    minPlayers = graphene.Int()
    maxPlayers = graphene.Int()
    playingTimeMinutes = graphene.Int()
    minAge = graphene.Int()
    maxAge = graphene.Int()
    weight = graphene.Float()
    category = graphene.List(graphene.String)


class CreateProductPayload(graphene.ObjectType):
    product = graphene.Field(Product)
    userErrors = graphene.List(graphene.NonNull(UserError), required=True)


class CreateProductMutation(graphene.Mutation):
    class Arguments:
        input = CreateProductInput(required=True)

    Output = CreateProductPayload

    @staticmethod
    def mutate(_root, info, input):
        user = _authenticated_user(info)
        if user is None or (not getattr(user, "is_staff", False) and not getattr(user, "is_superuser", False)):
            return CreateProductPayload(product=None, userErrors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
        name = (input.name or "").strip()
        if not name:
            return CreateProductPayload(product=None, userErrors=[_auth_error("Name is required", "NAME_REQUIRED", ["name"])])
        try:
            with connection.cursor() as cursor:
                # Use provided bggId if present, otherwise generate next sequential bgg_id
                provided_bgg = getattr(input, 'bggId', None)
                if provided_bgg is None:
                    cursor.execute("SELECT COALESCE(MAX(bgg_id), 0) + 1 FROM product_boardgame")
                    provided_bgg = cursor.fetchone()[0]

                cursor.execute(
                    "INSERT INTO product_boardgame (bgg_id, name, price_amount, quantity, publisher, image_url, image_filename, release_year, min_players, max_players, playing_time_minutes, min_age, max_age, weight, category) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING bgg_id, name",
                    [
                        provided_bgg,
                        name,
                        input.price,
                        input.quantity,
                        (getattr(input, 'publisher', None) or ''),
                        # imageUrl (camelCase) per contract
                        (getattr(input, 'imageUrl', None) or ''),
                        (getattr(input, 'imageFileName', None) or ''),
                        (getattr(input, 'releaseYear', None) or 0),
                        (getattr(input, 'minPlayers', None) or 1),
                        (getattr(input, 'maxPlayers', None) or 1),
                        (getattr(input, 'playingTimeMinutes', None) or 0),
                        (getattr(input, 'minAge', None) or 0),
                        (getattr(input, 'maxAge', None) or 0),
                        (getattr(input, 'weight', None) or 0.0),
                        # category stored as comma-separated string internally
                        (','.join(getattr(input, 'category', []) or []) if (getattr(input, 'category', None) is not None) else ''),
                    ],
                )
                row = cursor.fetchone()
            product = Product(
                bgg_id=row[0],
                name=row[1],
                release_year=0,
                min_players=1,
                max_players=1,
                playing_time_minutes=0,
                min_age=0,
                weight=0.0,
                category=[],
                price=float(input.price),
                publisher=input.publisher or "",
                image_url=input.imageUrl or "",
                quantity=int(input.quantity),
            )
            return CreateProductPayload(product=product, userErrors=[])
        except IntegrityError:
            return CreateProductPayload(product=None, userErrors=[_auth_error("Conflict creating product", "DUPLICATE")])
        except Exception:
            logging.error("Create product failed.")
            return CreateProductPayload(product=None, userErrors=[_auth_error("Create product failed", "SERVER_ERROR")])


class UpdateProductInput(graphene.InputObjectType):
    name = graphene.String()
    price = graphene.Float()
    quantity = graphene.Int()
    publisher = graphene.String()
    # Contract: accept camelCase fields only
    imageUrl = graphene.String()
    imageFileName = graphene.String()
    releaseYear = graphene.Int()
    minPlayers = graphene.Int()
    maxPlayers = graphene.Int()
    playingTimeMinutes = graphene.Int()
    minAge = graphene.Int()
    maxAge = graphene.Int()
    weight = graphene.Float()
    category = graphene.List(graphene.String)


class UpdateProductPayload(graphene.ObjectType):
    product = graphene.Field(Product)
    userErrors = graphene.List(graphene.NonNull(UserError), required=True)


class UpdateProductMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        input = UpdateProductInput(required=True)

    Output = UpdateProductPayload

    @staticmethod
    def mutate(_root, info, id, input):
        user = _authenticated_user(info)
        if user is None or (not getattr(user, "is_staff", False) and not getattr(user, "is_superuser", False)):
            return UpdateProductPayload(product=None, userErrors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
        try:
            with connection.cursor() as cursor:
                # Build update statement dynamically
                fields = []
                values = []
                if input.name is not None:
                    fields.append("name = %s")
                    values.append(input.name.strip())
                if input.price is not None:
                    fields.append("price_amount = %s")
                    values.append(input.price)
                if input.quantity is not None:
                    fields.append("quantity = %s")
                    values.append(input.quantity)
                if input.publisher is not None:
                    fields.append("publisher = %s")
                    values.append(input.publisher)
                # Accept camelCase and snake_case variants for image and other fields
                if getattr(input, 'imageUrl', None) is not None or getattr(input, 'image_url', None) is not None:
                    fields.append("image_url = %s")
                    values.append(getattr(input, 'imageUrl', None) or getattr(input, 'image_url', None))
                if getattr(input, 'imageFileName', None) is not None or getattr(input, 'image_file_name', None) is not None:
                    fields.append("image_filename = %s")
                    values.append(getattr(input, 'imageFileName', None) or getattr(input, 'image_file_name', None))
                if getattr(input, 'releaseYear', None) is not None:
                    fields.append("release_year = %s")
                    values.append(getattr(input, 'releaseYear', None))
                if getattr(input, 'minPlayers', None) is not None:
                    fields.append("min_players = %s")
                    values.append(getattr(input, 'minPlayers', None))
                if getattr(input, 'maxPlayers', None) is not None:
                    fields.append("max_players = %s")
                    values.append(getattr(input, 'maxPlayers', None))
                if getattr(input, 'playingTimeMinutes', None) is not None:
                    fields.append("playing_time_minutes = %s")
                    values.append(getattr(input, 'playingTimeMinutes', None))
                if getattr(input, 'minAge', None) is not None:
                    fields.append("min_age = %s")
                    values.append(getattr(input, 'minAge', None))
                if getattr(input, 'maxAge', None) is not None:
                    fields.append("max_age = %s")
                    values.append(getattr(input, 'maxAge', None))
                if getattr(input, 'weight', None) is not None:
                    fields.append("weight = %s")
                    values.append(getattr(input, 'weight', None))
                if getattr(input, 'category', None) is not None:
                    fields.append("category = %s")
                    values.append(','.join(input.category) if isinstance(input.category, (list, tuple)) else input.category)
                if not fields:
                    return UpdateProductPayload(product=None, userErrors=[_auth_error("No fields to update", "NO_OP")])
                values.append(id)
                sql = f"UPDATE product_boardgame SET {', '.join(fields)} WHERE bgg_id = %s RETURNING bgg_id, name, price_amount, quantity"
                cursor.execute(sql, values)
                row = cursor.fetchone()
                if not row:
                    return UpdateProductPayload(product=None, userErrors=[_auth_error("Product not found", "PRODUCT_NOT_FOUND")])
            product = Product(
                bgg_id=row[0],
                name=row[1],
                release_year=0,
                min_players=1,
                max_players=1,
                playing_time_minutes=0,
                min_age=0,
                weight=0.0,
                category=[],
                price=float(row[2]),
                publisher="",
                image_url="",
                quantity=int(row[3]),
            )
            return UpdateProductPayload(product=product, userErrors=[])
        except Exception:
            return UpdateProductPayload(product=None, userErrors=[_auth_error("Update failed", "SERVER_ERROR")])


class AdjustVariantStockPayload(graphene.ObjectType):
    success = graphene.Boolean()
    errors = graphene.List(graphene.NonNull(UserError), required=True)


class AdjustVariantStockMutation(graphene.Mutation):
    class Arguments:
        productId = graphene.ID(required=True)
        variantId = graphene.ID(required=True)
        newStock = graphene.Int(required=True)

    Output = AdjustVariantStockPayload

    @staticmethod
    def mutate(_root, info, productId, variantId, newStock):
        user = _authenticated_user(info)
        if user is None or (not getattr(user, "is_staff", False) and not getattr(user, "is_superuser", False)):
            return AdjustVariantStockPayload(success=False, errors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
        try:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE product_boardgame SET quantity = %s WHERE bgg_id = %s RETURNING bgg_id", [newStock, productId])
                row = cursor.fetchone()
                if not row:
                    return AdjustVariantStockPayload(success=False, errors=[_auth_error("Product or variant not found", "NOT_FOUND")])
            return AdjustVariantStockPayload(success=True, errors=[])
        except Exception:
            return AdjustVariantStockPayload(success=False, errors=[_auth_error("Adjust stock failed", "SERVER_ERROR")])


class CreatePromotionInput(graphene.InputObjectType):
    name = graphene.String(required=True)
    discount_percent = graphene.Float(required=True)
    active = graphene.Boolean(required=True)


class CreatePromotionPayload(graphene.ObjectType):
    promotion = graphene.Field(lambda: Promotion)
    userErrors = graphene.List(graphene.NonNull(UserError), required=True)


class CreatePromotionMutation(graphene.Mutation):
    class Arguments:
        input = CreatePromotionInput(required=True)

    Output = CreatePromotionPayload

    @staticmethod
    def mutate(_root, info, input):
        user = _authenticated_user(info)
        if user is None or (not getattr(user, "is_staff", False) and not getattr(user, "is_superuser", False)):
            return CreatePromotionPayload(promotion=None, userErrors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
        name = (input.name or "").strip()
        if not name:
            return CreatePromotionPayload(promotion=None, userErrors=[_auth_error("Name is required", "NAME_REQUIRED", ["name"])])
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO promotion (name, discount_pct, starts_at, ends_at, stackable, created_at) VALUES (%s, %s, %s, %s, %s, now()) RETURNING id, name",
                    [name, input.discount_percent, None, None, False],
                )
                row = cursor.fetchone()
            promo = Promotion(id=row[0], name=row[1], stackable=False, discount_pct=float(input.discount_percent), starts_at=None, ends_at=None, created_at="")
            return CreatePromotionPayload(promotion=promo, userErrors=[])
        except Exception:
            return CreatePromotionPayload(promotion=None, userErrors=[_auth_error("Create promotion failed", "SERVER_ERROR")])


class UpdatePromotionInput(graphene.InputObjectType):
    name = graphene.String()
    discount_percent = graphene.Float()
    active = graphene.Boolean()


class UpdatePromotionPayload(graphene.ObjectType):
    promotion = graphene.Field(lambda: Promotion)
    userErrors = graphene.List(graphene.NonNull(UserError), required=True)


class UpdatePromotionMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        input = UpdatePromotionInput(required=True)

    Output = UpdatePromotionPayload

    @staticmethod
    def mutate(_root, info, id, input):
        user = _authenticated_user(info)
        if user is None or (not getattr(user, "is_staff", False) and not getattr(user, "is_superuser", False)):
            return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
        try:
            with connection.cursor() as cursor:
                fields = []
                values = []
                if input.name is not None:
                    fields.append("name = %s")
                    values.append(input.name)
                if input.discount_percent is not None:
                    fields.append("discount_pct = %s")
                    values.append(input.discount_percent)
                if not fields:
                    return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("No fields to update", "NO_OP")])
                values.append(id)
                sql = f"UPDATE promotion SET {', '.join(fields)} WHERE id = %s RETURNING id, name"
                cursor.execute(sql, values)
                row = cursor.fetchone()
                if not row:
                    return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("Promotion not found", "NOT_FOUND")])
            promo = Promotion(id=row[0], name=row[1], stackable=False, discount_pct=float(input.discount_percent or 0), starts_at=None, ends_at=None, created_at="")
            return UpdatePromotionPayload(promotion=promo, userErrors=[])
        except Exception:
            return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("Update promotion failed", "SERVER_ERROR")])


class IssuedGiftCard(graphene.ObjectType):
    id = graphene.ID(required=True)
    code = graphene.String(required=True)
    amount = graphene.String(required=True)
    currency = graphene.String(required=True)


class CreateGiftCardPayload(graphene.ObjectType):
    gift_card = graphene.Field(IssuedGiftCard)
    user_errors = graphene.List(graphene.NonNull(UserError), required=True)


class CreateGiftCardMutation(graphene.Mutation):
    class Arguments:
        amount = graphene.String(required=True)

    Output = CreateGiftCardPayload

    @staticmethod
    def mutate(_root, info, amount):
        user = _authenticated_user(info)
        if user is None:
            return CreateGiftCardPayload(user_errors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return CreateGiftCardPayload(user_errors=[_auth_error("Administrator permission required", "FORBIDDEN")])
        if not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", amount) or Decimal(amount) <= 0:
            return CreateGiftCardPayload(user_errors=[_auth_error("Invalid amount", "INVALID_AMOUNT", ["amount"])])
        value = Decimal(amount).quantize(Decimal("0.01"))
        code = secrets.token_hex(32)
        code_hash = hashlib.sha256(code.encode("utf-8")).digest()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO gift_card (code_hash, amount, currency) VALUES (%s, %s, %s) RETURNING id",
                    [code_hash, value, "ARS"],
                )
                card_id = cursor.fetchone()[0]
        except Exception:
            return CreateGiftCardPayload(user_errors=[_auth_error("Could not issue gift card", "SERVER_ERROR")])
        return CreateGiftCardPayload(
            gift_card=IssuedGiftCard(id=str(card_id), code=code, amount=str(value), currency="ARS"),
            user_errors=[],
        )


class ResolvePaymentReviewPayload(graphene.ObjectType):
    resolution = graphene.Field(PaymentReviewResolution)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class ResolvePaymentReviewMutation(graphene.Mutation):
    class Arguments:
        order_id = graphene.ID(required=True)
        payment_id = graphene.ID(required=True)
        action = graphene.String(required=True)
        reason = graphene.String(required=True)

    Output = ResolvePaymentReviewPayload

    @staticmethod
    def mutate(_root, info, order_id, payment_id, action, reason):
        try:
            user = _authenticated_user(info)
        except Exception:
            return ResolvePaymentReviewPayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not resolve payment review.")])
        if user is None:
            return ResolvePaymentReviewPayload(user_errors=[OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return ResolvePaymentReviewPayload(user_errors=[OrderUserError(code="FORBIDDEN", message="Administrator permission required.")])
        try:
            identifier = uuid.UUID(str(order_id))
            action, reason = action.strip(), reason.strip()
            if (not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", payment_id)
                    or not action or len(action) > 200 or not action.isprintable()
                    or not reason or len(reason) > 1000 or not reason.isprintable()):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return ResolvePaymentReviewPayload(user_errors=[OrderUserError(code="INVALID", message="Invalid review details.")])
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM credit_payment_review WHERE order_id = %s AND payment_id = %s FOR UPDATE",
                    [identifier, payment_id],
                )
                if cursor.fetchone() is None:
                    return ResolvePaymentReviewPayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment review not found.")])
                cursor.execute(
                    "SELECT actor_user_id, action, reason, resolved_at FROM credit_payment_review_resolution "
                    "WHERE order_id = %s AND payment_id = %s", [identifier, payment_id],
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[:3] != (user.pk, action, reason):
                        return ResolvePaymentReviewPayload(user_errors=[OrderUserError(code="ALREADY_RESOLVED", message="Payment review is already resolved.")])
                    resolved_at = existing[3]
                else:
                    cursor.execute(
                        "INSERT INTO credit_payment_review_resolution "
                        "(order_id, payment_id, actor_user_id, action, reason) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING resolved_at",
                        [identifier, payment_id, user.pk, action, reason],
                    )
                    resolved_at = cursor.fetchone()[0]
            return ResolvePaymentReviewPayload(
                resolution=PaymentReviewResolution(actor_id=str(user.pk), action=action, reason=reason,
                                                   resolved_at=resolved_at.isoformat()), user_errors=[],
            )
        except Exception:
            return ResolvePaymentReviewPayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not resolve payment review.")])


class ReleasePaymentReviewPayload(graphene.ObjectType):
    release = graphene.Field(PaymentReviewRelease)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class ReleasePaymentReviewMutation(graphene.Mutation):
    class Arguments:
        order_id = graphene.ID(required=True)
        payment_id = graphene.ID(required=True)
        reason = graphene.String(required=True)

    Output = ReleasePaymentReviewPayload

    @staticmethod
    def mutate(_root, info, order_id, payment_id, reason):
        try:
            user = _authenticated_user(info)
        except Exception:
            return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not release payment review.")])
        if user is None:
            return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="FORBIDDEN", message="Administrator permission required.")])
        try:
            identifier = uuid.UUID(str(order_id))
            reason = reason.strip()
            if (not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", payment_id)
                    or not reason or len(reason) > 1000 or not reason.isprintable()):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="INVALID", message="Invalid release details.")])
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT purchase.user_id, EXISTS (SELECT 1 FROM credit_payment_review_resolution resolution "
                    "WHERE resolution.order_id = review.order_id AND resolution.payment_id = review.payment_id) "
                    "FROM purchase_order purchase JOIN credit_payment_review review ON review.order_id = purchase.id "
                    "WHERE purchase.id = %s AND review.payment_id = %s",
                    [identifier, payment_id],
                )
                owner = cursor.fetchone()
                if owner is None:
                    return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment review not found.")])
                if not owner[1]:
                    return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="UNRESOLVED", message="Payment review must be resolved first.")])
                _lock_credit_account(cursor, owner[0])
                cursor.execute("SELECT user_id FROM purchase_order WHERE id = %s FOR UPDATE", [identifier])
                if cursor.fetchone() != (owner[0],):
                    return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment review not found.")])
                cursor.execute("SELECT 1 FROM credit_payment_review WHERE order_id = %s AND payment_id = %s FOR UPDATE", [identifier, payment_id])
                if cursor.fetchone() is None:
                    return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment review not found.")])
                cursor.execute("SELECT actor_user_id, reason, released_at FROM credit_payment_review_release WHERE order_id = %s AND payment_id = %s", [identifier, payment_id])
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[:2] != (user.pk, reason):
                        return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="ALREADY_RELEASED", message="Payment review is already released.")])
                    released_at = existing[2]
                else:
                    cursor.execute("SELECT 1 FROM credit_payment_review_resolution WHERE order_id = %s AND payment_id = %s", [identifier, payment_id])
                    if cursor.fetchone() is None:
                        return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="UNRESOLVED", message="Payment review must be resolved first.")])
                    cursor.execute("INSERT INTO credit_payment_review_release (order_id, payment_id, actor_user_id, reason) VALUES (%s, %s, %s, %s) RETURNING released_at", [identifier, payment_id, user.pk, reason])
                    released_at = cursor.fetchone()[0]
            return ReleasePaymentReviewPayload(release=PaymentReviewRelease(actor_id=str(user.pk), reason=reason, released_at=released_at.isoformat()), user_errors=[])
        except Exception:
            return ReleasePaymentReviewPayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not release payment review.")])


class RecordPaymentAttemptObservationPayload(graphene.ObjectType):
    observation = graphene.Field(PaymentAttemptObservation)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class RecordPaymentAttemptObservationMutation(graphene.Mutation):
    class Arguments:
        order_id = graphene.ID(required=True)
        attempt_id = graphene.ID(required=True)
        request_id = graphene.ID(required=True)
        statement = graphene.String(required=True)

    Output = RecordPaymentAttemptObservationPayload

    @staticmethod
    def mutate(_root, info, order_id, attempt_id, request_id, statement):
        try:
            user = _authenticated_user(info)
        except Exception:
            return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not record payment attempt observation.")])
        if user is None:
            return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="FORBIDDEN", message="Administrator permission required.")])
        try:
            order_identifier = uuid.UUID(str(order_id))
            attempt_identifier = uuid.UUID(str(attempt_id))
            request_identifier = uuid.UUID(str(request_id))
            statement = statement.strip()
            if not statement or len(statement) > 1000 or not statement.isprintable():
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="INVALID", message="Invalid observation details.")])
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT order_id, attempt_id, actor_user_id, statement, preference_id, recorded_at "
                    "FROM credit_retry_attempt_observation WHERE id = %s", [request_identifier],
                )
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        "SELECT purchase.user_id FROM purchase_order purchase JOIN credit_retry_attempt attempt "
                        "ON attempt.order_id = purchase.id AND attempt.user_id = purchase.user_id "
                        "WHERE purchase.id = %s AND attempt.attempt_id = %s",
                        [order_identifier, attempt_identifier],
                    )
                    owner = cursor.fetchone()
                    if owner is None:
                        return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment attempt not found.")])
                    _lock_credit_account(cursor, owner[0])
                    cursor.execute("SELECT user_id FROM purchase_order WHERE id = %s FOR UPDATE", [order_identifier])
                    if cursor.fetchone() != owner:
                        return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment attempt not found.")])
                    cursor.execute(
                        "SELECT preference_id FROM credit_retry_attempt "
                        "WHERE order_id = %s AND user_id = %s AND attempt_id = %s FOR UPDATE",
                        [order_identifier, owner[0], attempt_identifier],
                    )
                    attempt = cursor.fetchone()
                    if attempt is None:
                        return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment attempt not found.")])
                    cursor.execute(
                        "INSERT INTO credit_retry_attempt_observation "
                        "(id, attempt_id, order_id, user_id, preference_id, actor_user_id, statement) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        [request_identifier, attempt_identifier, order_identifier, owner[0], attempt[0], user.pk, statement],
                    )
                    cursor.execute(
                        "SELECT order_id, attempt_id, actor_user_id, statement, preference_id, recorded_at "
                        "FROM credit_retry_attempt_observation WHERE id = %s", [request_identifier],
                    )
                    existing = cursor.fetchone()
                if existing is None or existing[:4] != (order_identifier, attempt_identifier, user.pk, statement):
                    return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="IDEMPOTENCY_CONFLICT", message="Observation request conflicts with an existing record.")])
                preference_id, recorded_at = existing[4], existing[5]
            return RecordPaymentAttemptObservationPayload(
                observation=PaymentAttemptObservation(id=str(request_identifier), attempt_id=str(attempt_identifier),
                    order_id=str(order_identifier), actor_id=str(user.pk), preference_id=preference_id,
                    statement=statement, recorded_at=recorded_at.isoformat()), user_errors=[],
            )
        except Exception:
            return RecordPaymentAttemptObservationPayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not record payment attempt observation.")])


class ReleasePaymentAttemptMutation(graphene.Mutation):
    class Arguments:
        order_id = graphene.ID(required=True)
        attempt_id = graphene.ID(required=True)
        observation_id = graphene.ID(required=True)
        reason = graphene.String(required=True)
        acknowledge_risk = graphene.Boolean(required=True)

    Output = PaymentAttemptReleasePayload

    @staticmethod
    def mutate(_root, info, order_id, attempt_id, observation_id, reason, acknowledge_risk):
        try:
            user = _authenticated_user(info)
        except Exception:
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not release payment attempt.")])
        if user is None:
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="FORBIDDEN", message="Administrator permission required.")])
        try:
            order_identifier = uuid.UUID(str(order_id))
            attempt_identifier = uuid.UUID(str(attempt_id))
            observation_identifier = uuid.UUID(str(observation_id))
            reason = reason.strip()
            if acknowledge_risk is not True or not reason or len(reason) > 1000 or not reason.isprintable():
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="INVALID", message="Invalid release details.")])
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                release_sql = (
                    "SELECT order_id, observation_id, actor_user_id, reason, preference_id, risk_acknowledged, released_at "
                    "FROM credit_retry_attempt_release WHERE attempt_id = %s"
                )
                cursor.execute(release_sql, [attempt_identifier])
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        "SELECT purchase.user_id FROM purchase_order purchase JOIN credit_retry_attempt attempt "
                        "ON attempt.order_id = purchase.id AND attempt.user_id = purchase.user_id "
                        "WHERE purchase.id = %s AND attempt.attempt_id = %s",
                        [order_identifier, attempt_identifier],
                    )
                    owner = cursor.fetchone()
                    if owner is None:
                        return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment attempt not found.")])
                    cursor.execute(
                        "SELECT 1 FROM credit_retry_attempt_observation WHERE id = %s AND attempt_id = %s "
                        "AND order_id = %s AND user_id = %s",
                        [observation_identifier, attempt_identifier, order_identifier, owner[0]],
                    )
                    if cursor.fetchone() is None:
                        return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="EVIDENCE_REQUIRED", message="An observation for this attempt is required.")])
                    _lock_credit_account(cursor, owner[0])
                    cursor.execute("SELECT user_id FROM purchase_order WHERE id = %s FOR UPDATE", [order_identifier])
                    if cursor.fetchone() != owner:
                        return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment attempt not found.")])
                    cursor.execute(release_sql, [attempt_identifier])
                    existing = cursor.fetchone()
                    if existing is None:
                        cursor.execute(
                            "SELECT preference_id FROM credit_retry_attempt "
                            "WHERE order_id = %s AND user_id = %s AND attempt_id = %s FOR UPDATE",
                            [order_identifier, owner[0], attempt_identifier],
                        )
                        attempt = cursor.fetchone()
                        if attempt is None:
                            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="NOT_FOUND", message="Payment attempt not found.")])
                        cursor.execute(
                            "SELECT preference_id FROM credit_retry_attempt_observation "
                            "WHERE id = %s AND attempt_id = %s AND order_id = %s AND user_id = %s",
                            [observation_identifier, attempt_identifier, order_identifier, owner[0]],
                        )
                        observation = cursor.fetchone()
                        if observation is None:
                            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="EVIDENCE_REQUIRED", message="An observation for this attempt is required.")])
                        if observation[0] != attempt[0]:
                            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="STALE_EVIDENCE", message="The payment attempt changed after the observation.")])
                        cursor.execute(
                            "INSERT INTO credit_retry_attempt_release "
                            "(attempt_id, order_id, user_id, observation_id, actor_user_id, preference_id, reason, risk_acknowledged) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING released_at",
                            [attempt_identifier, order_identifier, owner[0], observation_identifier, user.pk, attempt[0], reason, True],
                        )
                        released_at = cursor.fetchone()[0]
                        cursor.execute(
                            "DELETE FROM credit_retry_attempt WHERE order_id = %s AND user_id = %s AND attempt_id = %s",
                            [order_identifier, owner[0], attempt_identifier],
                        )
                        if cursor.rowcount != 1:
                            raise ValueError
                        existing = (order_identifier, observation_identifier, user.pk, reason, attempt[0], True, released_at)
                if existing[:4] != (order_identifier, observation_identifier, user.pk, reason):
                    return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="ALREADY_RELEASED", message="Payment attempt is already released.")])
            return _payment_attempt_release(attempt_identifier, existing)
        except Exception:
            return PaymentAttemptReleasePayload(user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not release payment attempt.")])


class Mutation(graphene.ObjectType):
    register = RegisterMutation.Field(required=True)
    login = LoginMutation.Field(required=True)
    add_to_cart = AddToCartMutation.Field(required=True)
    update_cart_quantity = UpdateCartQuantityMutation.Field(required=True)
    remove_from_cart = RemoveFromCartMutation.Field(required=True)
    add_to_wishlist = AddToWishlistMutation.Field(required=True)
    remove_from_wishlist = RemoveFromWishlistMutation.Field(required=True)
    create_review = CreateReviewMutation.Field(required=True)
    # Admin mutations for BFF
    create_product = CreateProductMutation.Field(required=True)
    update_product = UpdateProductMutation.Field(required=True)
    adjust_variant_stock = AdjustVariantStockMutation.Field(required=True)
    create_promotion = CreatePromotionMutation.Field(required=True)
    update_promotion = UpdatePromotionMutation.Field(required=True)
    create_gift_card = CreateGiftCardMutation.Field(required=True)
    redeem_gift_card = RedeemGiftCardMutation.Field(
        required=True,
        description=(
            "Redeems an unused gift card atomically. Successful results do not expire.\n"
            "Recorded rejections expire after 30 days and their keys can be reused.\n"
            "While a record exists, a different request with that key conflicts."
        ),
    )
    checkout_with_credit = CheckoutWithCreditMutation.Field(
        required=True,
        description=(
            "Pays the server-calculated order total atomically with account credit.\n"
            "Insufficient credit or stock rejects the purchase without a partial debit.\n"
            "A pending Mercado Pago order owned by the buyer blocks a new credit checkout.\n"
            "Successful results do not expire. Recorded rejections expire after 30 days\n"
            "and their keys can be reused. While a record exists, a different request\n"
            "with that key conflicts."
        ),
    )
    checkout = CheckoutMutation.Field(required=True)
    retry_payment = RetryPaymentMutation.Field(required=True)
    confirm_payment = ConfirmPaymentMutation.Field(required=True)
    resolve_payment_review = ResolvePaymentReviewMutation.Field(required=True)
    release_payment_review = ReleasePaymentReviewMutation.Field(required=True)
    record_payment_attempt_observation = RecordPaymentAttemptObservationMutation.Field(required=True)
    release_payment_attempt = ReleasePaymentAttemptMutation.Field(required=True)


schema = graphene.Schema(query=Query, mutation=Mutation)
