import datetime
import hashlib
import json
import re
import secrets
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation

import graphene
import jwt
import logging
from django.db import IntegrityError, connection, transaction
from graphql import GraphQLError

from saleor.catalog_events import record_catalog_event
from . import wishlist
from .credit import (
    CheckoutWithCreditMutation, CreditBalancePayload, RedeemGiftCardMutation,
    _error, balance_for_user,
)
from .orders import (
    CheckoutMutation, ConfirmPaymentMutation, CouponPreview as BaseCouponPreview,
    OrderUserError, PurchaseOrder,
    RetryPaymentMutation, _lock_credit_account, preview_coupon,
)


class CouponPreview(BaseCouponPreview):
    class Meta:
        name = "CouponPreview"
        description = (
            "Read-only estimate for the authenticated user's current cart. The estimate\n"
            "does not reserve or consume a use. A discount above 50% of the undiscounted\n"
            "cart subtotal is rejected, not capped. Preserve decimal precision while\n"
            "calculating each alternative, then round each total upward once to whole\n"
            "pesos. Select the greater saving; ties favor the store promotion."
        )

    reason = graphene.String(
        description="Null when the coupon is eligible for the current cart."
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
    price = graphene.Float(
        required=True, description="The stored base price is strictly greater than zero."
    )
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
    price = graphene.Float(required=True, description="Positive base price.")
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


class ReviewModerationStatus(graphene.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ReviewModerationAction(graphene.Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    DELETE = "DELETE"


class AdminReview(graphene.ObjectType):
    id = graphene.ID(required=True)
    game_bgg_id = graphene.Int(required=True)
    game_name = graphene.String(required=True)
    rating = graphene.Int(required=True)
    body = graphene.String(required=True)
    created_at = graphene.String(required=True)
    status = graphene.Field(ReviewModerationStatus, required=True)
    deleted = graphene.Boolean(required=True)
    publicly_visible = graphene.Boolean(required=True)


class AdminReviewPage(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(AdminReview), required=True)
    page = graphene.Int(required=True)
    page_size = graphene.Int(required=True)
    total = graphene.Int(required=True)
    total_pages = graphene.Int(required=True)


class ReviewModerationError(graphene.ObjectType):
    field = graphene.List(graphene.NonNull(graphene.String))
    code = graphene.String(required=True)
    message = graphene.String(required=True)


class ModerateReviewPayload(graphene.ObjectType):
    success = graphene.Boolean(required=True)
    review = graphene.Field(AdminReview)
    user_errors = graphene.List(graphene.NonNull(ReviewModerationError), required=True)


class CreateReviewInput(graphene.InputObjectType):
    product_slug = graphene.String(required=True)
    rating = graphene.Int(required=True)
    body = graphene.String(required=True)


class Promotion(graphene.ObjectType):
    id = graphene.ID(required=True)
    name = graphene.String(required=True)
    active = graphene.Boolean(
        required=True,
        description=(
            "Manual activation state, independent of validity dates; admin changes "
            "require an active staff or superuser account."
        ),
    )
    stackable = graphene.Boolean(
        required=True,
        description=(
            "Store promotions do not stack; an eligible product-specific offer "
            "wins over global offers, otherwise the highest eligible global "
            "percentage applies. Coupon-linked promotion rows are coupon offers, "
            "not global store promotions."
        ),
    )
    discount_pct = graphene.Float(
        required=True,
        description=(
            "Percentage discount from zero through 50, inclusive; at most one "
            "active promotion may target a product."
        ),
    )
    product_bgg_id = graphene.Int(
        description=(
            "BGG identifier of the product this promotion applies to; null means a "
            "global store offer. An eligible product-specific offer wins for its "
            "product; otherwise the highest eligible global percentage applies. "
            "Store promotions do not stack, and coupon-linked rows are excluded "
            "from store offers."
        )
    )
    starts_at = graphene.String()
    ends_at = graphene.String()
    created_at = graphene.String(required=True)


def _promotion_from_row(row):
    (
        identifier, name, stackable, discount, starts_at, ends_at, created_at,
        active, product_bgg_id,
    ) = row
    return Promotion(
        id=identifier, name=name, stackable=stackable, discount_pct=float(discount), active=active,
        starts_at=starts_at.isoformat() if starts_at else None,
        ends_at=ends_at.isoformat() if ends_at else None,
        created_at=created_at.isoformat() if created_at else None,
        product_bgg_id=product_bgg_id,
    )


class Coupon(graphene.ObjectType):
    id = graphene.ID(required=True)
    code = graphene.String(
        required=True,
        description="Unique by exact, case-sensitive equality; code is not normalized.",
    )
    promotion = graphene.Field(Promotion)
    remaining = graphene.BigInt(
        required=True, description="Number of uses remaining; never negative."
    )
    active = graphene.Boolean(required=True)
    starts_at = graphene.String(description="UTC validity start, inclusive.")
    ends_at = graphene.String(
        description=(
            "UTC validity end, exclusive; when both boundaries exist, startsAt "
            "precedes endsAt."
        )
    )
    created_at = graphene.String(required=True)


class AdminCoupon(graphene.ObjectType):
    class Meta:
        description = (
            "Admin representation of a coupon; the discount is flattened from "
            "its product-less promotion."
        )

    id = graphene.Int(required=True)
    code = graphene.String(required=True)
    discount_percent = graphene.Float(required=True)
    remaining = graphene.BigInt(required=True)
    active = graphene.Boolean(required=True)
    starts_at = graphene.String()
    ends_at = graphene.String()
    created_at = graphene.String(required=True)


def _admin_coupon_from_row(row):
    starts_at = _coupon_utc_datetime(row["starts_at"])
    ends_at = _coupon_utc_datetime(row["ends_at"])
    created_at = _coupon_utc_datetime(row["created_at"])
    return AdminCoupon(
        id=row["id"],
        code=row["code"],
        discount_percent=float(row["discount_pct"]),
        remaining=row["remaining"],
        active=row["active"],
        starts_at=starts_at.isoformat() if starts_at else None,
        ends_at=ends_at.isoformat() if ends_at else None,
        created_at=created_at.isoformat(),
    )


def _coupon_utc_datetime(value):
    if value is None:
        return None
    if value.utcoffset() is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


class AvailableCoupon(graphene.ObjectType):
    code = graphene.String(required=True)
    starts_at = graphene.String(required=True)
    ends_at = graphene.String(required=True)


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


def _product_by_bgg_id(cursor, bgg_id):
    cursor.execute(
        "SELECT bgg_id, name, release_year, min_players, max_players, "
        "playing_time_minutes, min_age, weight, category, price_amount, "
        "publisher, image_url, quantity FROM product_boardgame WHERE bgg_id = %s",
        [bgg_id],
    )
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row)) if row else None


def _product_detail(cursor, product):
    if product is None:
        return None
    cursor.execute(
        "SELECT rating, body, created_at FROM product_review "
        "WHERE bgg_id = %s AND status = 'ACCEPTED' AND is_deleted = FALSE "
        "ORDER BY created_at DESC",
        [product["bgg_id"]],
    )
    public_reviews = [
        PublicReview(rating=rating, body=body, created_at=created_at.isoformat())
        for rating, body, created_at in cursor.fetchall()
    ]
    average = sum(review.rating for review in public_reviews) / len(public_reviews) if public_reviews else None
    return ProductDetail(
        **_product_from_row(product),
        review_summary=ReviewSummary(approved_review_count=len(public_reviews), average_rating=average),
        approved_reviews=public_reviews,
    )


def _review_error(code, message, field=None):
    return ReviewUserError(field=field, code=code, message=message)


def _moderation_error(code, message, field=None):
    return ReviewModerationError(field=field, code=code, message=message)


def _admin_review_from_row(row):
    review_id, bgg_id, game_name, rating, body, created_at, status, deleted = row
    return AdminReview(
        id=str(review_id),
        game_bgg_id=bgg_id,
        game_name=game_name,
        rating=rating,
        body=body,
        created_at=created_at.isoformat(),
        status=status,
        deleted=deleted,
        publicly_visible=status == "ACCEPTED" and not deleted,
    )


def _moderation_user_error(info):
    user = _authenticated_user(info)
    if user is None:
        return _moderation_error("UNAUTHENTICATED", "Authentication is required.")
    if not getattr(user, "is_active", False) or not (
        getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
    ):
        return _moderation_error("FORBIDDEN", "Administrator permission is required.")
    return None


def _authenticated_user(info):
    from django.conf import settings
    from django.contrib.auth.models import User

    request = info.context
    header = request.headers.get("Authorization", "")
    if not header and getattr(request, "opkey_delegated", False):
        user = getattr(request, "user", None)
        if getattr(user, "is_active", False) and (
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        ):
            return user
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


class OperationSession(graphene.ObjectType):
    id = graphene.ID(required=True)
    user_id = graphene.ID()
    ops = graphene.List(graphene.NonNull(graphene.String), required=True)
    issued_at = graphene.String(required=True)
    expires_at = graphene.String()
    last_used = graphene.String()
    revoked = graphene.Boolean(required=True)


class OperationSessionPage(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(OperationSession), required=True)
    has_next = graphene.Boolean(required=True)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class ManageOperationSessionPayload(graphene.ObjectType):
    success = graphene.Boolean(required=True)
    user_errors = graphene.List(graphene.NonNull(OrderUserError), required=True)


class TopProduct(graphene.ObjectType):
    bgg_id = graphene.Int(required=True)
    name = graphene.String(required=True)
    units = graphene.Int(required=True)
    revenue = graphene.Float(required=True)


class RevenueByDay(graphene.ObjectType):
    date = graphene.String(required=True)
    revenue = graphene.Float(required=True)


class StatusCount(graphene.ObjectType):
    status = graphene.String(required=True)
    count = graphene.Int(required=True)


class SalesMetrics(graphene.ObjectType):
    revenue_total = graphene.Float(required=True)
    orders_count = graphene.Int(required=True)
    top_products = graphene.List(graphene.NonNull(TopProduct), required=True)
    orders_by_dispatch_status = graphene.List(
        graphene.NonNull(StatusCount), required=True
    )
    orders_by_payment_status = graphene.List(
        graphene.NonNull(StatusCount), required=True
    )
    revenue_by_day = graphene.List(graphene.NonNull(RevenueByDay), required=True)


def _sales_metrics_invalid(field):
    return GraphQLError(
        f"{field} is invalid", extensions={"code": "INVALID", "field": field}
    )


def _sales_metrics_date(value, field):
    if not isinstance(value, str):
        raise _sales_metrics_invalid(field)
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        raise _sales_metrics_invalid(field) from None
    if parsed.utcoffset() is None:
        raise _sales_metrics_invalid(field)
    return parsed.astimezone(datetime.timezone.utc)


class Query(graphene.ObjectType):
    hello = graphene.String(default_value="Saleor API activa y respondiendo OK")
    me = graphene.Field(lambda: AuthUser)
    product_by_slug = graphene.Field(
        ProductDetail,
        slug=graphene.String(required=True),
    )
    product_by_bgg_id = graphene.Field(ProductDetail, bgg_id=graphene.Int(required=True))
    product_categories = graphene.List(graphene.NonNull(graphene.String), required=True)
    products = graphene.Field(
        ProductPage,
        page=graphene.Int(default_value=1),
        page_size=graphene.Int(default_value=50),
        search=graphene.String(),
        year_published=graphene.Int(),
        min_players=graphene.Int(),
        max_players=graphene.Int(),
        category_id=graphene.Int(),
        category_name=graphene.String(),
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
    admin_op_sessions = graphene.Field(
        OperationSessionPage, required=True, user_id=graphene.ID(),
        page=graphene.Int(default_value=1), page_size=graphene.Int(default_value=50),
    )
    admin_payment_reviews = graphene.Field(
        PaymentReviewPage, required=True,
        page=graphene.Int(default_value=1), page_size=graphene.Int(default_value=50),
    )
    admin_reviews = graphene.Field(
        AdminReviewPage,
        required=True,
        status=ReviewModerationStatus(),
        page=graphene.Int(default_value=1),
        page_size=graphene.Int(default_value=50),
        description=(
            "Requires an authenticated active staff or superuser identity. An "
            "operation key without an active user identity is insufficient. "
            "Lists non-deleted reviews filtered by an optional single status. "
            "page is one-based and pageSize defaults to 50 (maximum 100)."
        ),
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
    admin_stock_movement_status = graphene.Field(
        lambda: StockMovementStatusPayload, required=True,
        movement_id=graphene.ID(required=True),
        description=(
            "Requires an active staff or superuser account; an unknown UUID does "
            "not prove rejection."
        ),
    )
    promotions = graphene.List(
        graphene.NonNull(Promotion),
        required=True,
        description=(
            "Requires an active staff or superuser account. Lists global and "
            "product-specific store promotions but excludes coupon-linked "
            "promotion rows. For each product, an eligible specific offer wins "
            "over global offers; otherwise the highest eligible global percentage "
            "applies. Store promotions do not stack."
        ),
    )
    admin_coupons = graphene.List(
        graphene.NonNull(AdminCoupon),
        required=True,
        description="Requires an active staff or superuser account.",
    )
    admin_audit_logs = graphene.Field(
        lambda: AdminAuditPage,
        required=True,
        entity_type=graphene.String(),
        entity_id=graphene.String(),
        actor_id=graphene.String(),
        action=graphene.String(),
        created_from=graphene.String(),
        created_to=graphene.String(),
        cursor=graphene.String(),
        limit=graphene.Int(default_value=50),
        description=(
            "Requires an active staff or superuser account. Optional exact filters by "
            "entity type/ID, actor ID and action; createdFrom/createdTo are inclusive "
            "timezone-aware date-times normalized to UTC. Ordered by createdAt DESC, id DESC "
            "with an opaque signed cursor; limit defaults to 50 (maximum 100). Invalid "
            "filters, limits or cursors fail with extensions.code INVALID and "
            "extensions.field; authorization failures use UNAUTHENTICATED or FORBIDDEN."
        ),
    )
    admin_sales_metrics = graphene.Field(
        SalesMetrics,
        from_=graphene.String(name="from"),
        to=graphene.String(),
        categories=graphene.List(graphene.NonNull(graphene.String)),
        product_ids=graphene.List(graphene.NonNull(graphene.Int), name="productIds"),
        required=True,
        description=(
            "Requires an active staff or superuser account. from and to are individually\n"
            "optional timezone-aware ISO instants normalized to UTC and define the\n"
            "half-open range [from, to). Callers must provide both bounds or omit both;\n"
            "if exactly one is supplied, INVALID identifies the missing bound in\n"
            "extensions.field. If both are omitted, the range is the last 30 days. The\n"
            "maximum range is 366 days. Revenue and ordersCount use paid orders by paid_at;\n"
            "payment and dispatch status counts use orders by created_at. Gross revenue\n"
            "sums purchase-order line subtotals and may differ from the order total. Exact\n"
            "category and product filters include matching lines only; each matching\n"
            "order is counted once. Dispatch status is derived at query time from paid_at:\n"
            "Pendiente for pending_payment, En preparación before 24 hours, Enviado from\n"
            "24 to less than 72 hours, and Entregado at 72 hours or later. pickup orders\n"
            "follow the same rule; rejected, cancelled and paid_stock_conflict orders have\n"
            "no dispatch status. Invalid input fails with extensions.code INVALID and\n"
            "extensions.field; authorization failures use UNAUTHENTICATED or FORBIDDEN."
        ),
    )
    admin_sales_categories = graphene.List(
        graphene.NonNull(graphene.String),
        required=True,
        description=(
            "Requires an active staff or superuser account; returns distinct stored "
            "category strings for the sales-metrics filter. Authorization failures "
            "use UNAUTHENTICATED or FORBIDDEN."
        ),
    )
    coupon_by_code = graphene.Field(Coupon, code=graphene.String(required=True))
    available_coupons = graphene.List(graphene.NonNull(AvailableCoupon), required=True)
    preview_coupon = graphene.Field(
        CouponPreview,
        required=True,
        code=graphene.String(required=True),
        description=(
            "Requires an authenticated user; previews the current cart without "
            "reserving or consuming a use."
        ),
    )
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

    def resolve_admin_stock_movement_status(self, info, movement_id):
        try:
            user = _authenticated_user(info)
            if user is None or not getattr(user, "is_active", False):
                return StockMovementStatusPayload(movement_id=movement_id, user_errors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
            if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
                return StockMovementStatusPayload(movement_id=movement_id, user_errors=[_auth_error("Administrator permission required", "FORBIDDEN")])
            try:
                normalized_id = str(uuid.UUID(movement_id))
            except (ValueError, TypeError, AttributeError):
                return StockMovementStatusPayload(movement_id=movement_id, user_errors=[_auth_error("Invalid movement identifier", "INVALID", ["movementId"])])
            with connection.cursor() as cursor:
                cursor.execute("SELECT status FROM stock_movement_status WHERE movement_id = %s", [normalized_id])
                row = cursor.fetchone()
            if row is None:
                return StockMovementStatusPayload(movement_id=movement_id, user_errors=[_auth_error("Movement not found", "NOT_FOUND")])
            if row[0] not in ("IN_PROGRESS", "APPLIED", "REJECTED"):
                raise ValueError("Invalid movement status")
            return StockMovementStatusPayload(movement_id=normalized_id, status=row[0], user_errors=[])
        except Exception:
            return StockMovementStatusPayload(movement_id=movement_id, user_errors=[_auth_error("Could not load movement status", "SERVER_ERROR")])

    def resolve_admin_op_sessions(self, info, user_id=None, page=1, page_size=50):
        try:
            user = _authenticated_user(info)
            if user is None:
                return OperationSessionPage(items=[], has_next=False, user_errors=[OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")])
            if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
                return OperationSessionPage(items=[], has_next=False, user_errors=[OrderUserError(code="FORBIDDEN", message="Administrator permission required.")])
            if page is None or page_size is None or not 1 <= page <= 10000 or not 1 <= page_size <= 100:
                raise ValueError("Invalid pagination")
            owner = int(user_id) if user_id is not None else None
            if owner is not None and owner < 1:
                raise ValueError("Invalid user")
            from opkeys.models import OpSession
            from opkeys.utils import utc_isoformat
            sessions = OpSession.objects.all()
            if owner is not None:
                sessions = sessions.filter(user_id=owner)
            offset = (page - 1) * page_size
            rows = list(sessions.order_by("-issued_at", "-id")[offset:offset + page_size + 1])
            return OperationSessionPage(items=[OperationSession(
                id=str(row.id), user_id=str(row.user_id) if row.user_id else None, ops=row.ops or [],
                issued_at=utc_isoformat(row.issued_at), expires_at=utc_isoformat(row.expires_at),
                last_used=utc_isoformat(row.last_used), revoked=row.revoked,
            ) for row in rows[:page_size]], has_next=len(rows) > page_size, user_errors=[])
        except (TypeError, ValueError):
            return OperationSessionPage(items=[], has_next=False, user_errors=[OrderUserError(code="INVALID", message="Invalid request.")])
        except Exception:
            return OperationSessionPage(items=[], has_next=False, user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not load operation sessions.")])

    def resolve_admin_reviews(self, info, status=None, page=1, page_size=50):
        user = _authenticated_user(info)
        if user is None:
            raise GraphQLError(
                "Authentication is required.",
                extensions={"code": "UNAUTHENTICATED"},
            )
        if not getattr(user, "is_active", False) or not (
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        ):
            raise GraphQLError(
                "Administrator permission is required.",
                extensions={"code": "FORBIDDEN"},
            )
        if (
            page is None
            or page_size is None
            or not 1 <= page <= 10000
            or not 1 <= page_size <= 100
        ):
            raise GraphQLError(
                "Invalid pagination.",
                extensions={"code": "INVALID"},
            )

        status_value = getattr(status, "value", status)
        if status_value not in (None, "PENDING", "ACCEPTED", "REJECTED"):
            raise GraphQLError(
                "Invalid review status.",
                extensions={"code": "INVALID"},
            )
        conditions = ["review.is_deleted = FALSE"]
        params = []
        if status_value is not None:
            conditions.append("review.status = %s")
            params.append(status_value)
        where_clause = " AND ".join(conditions)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM product_review review WHERE " + where_clause,
                params,
            )
            total = cursor.fetchone()[0]
            cursor.execute(
                "SELECT review.id, review.bgg_id, game.name, review.rating, "
                "review.body, review.created_at, review.status, review.is_deleted "
                "FROM product_review review "
                "JOIN product_boardgame game ON game.bgg_id = review.bgg_id "
                "WHERE " + where_clause +
                " ORDER BY review.created_at DESC, review.id DESC "
                "LIMIT %s OFFSET %s",
                params + [page_size, (page - 1) * page_size],
            )
            rows = cursor.fetchall()
        return AdminReviewPage(
            items=[_admin_review_from_row(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=(total + page_size - 1) // page_size,
        )

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
            return _product_detail(cursor, _product_by_slug(cursor, slug))

    def resolve_product_by_bgg_id(self, info, bgg_id):
        with connection.cursor() as cursor:
            return _product_detail(cursor, _product_by_bgg_id(cursor, bgg_id))

    def resolve_product_categories(self, info):
        with connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT category FROM product_boardgame WHERE category IS NOT NULL")
            return sorted({name.strip() for (value,) in cursor.fetchall() for name in (value or "").split(",") if name.strip()})

    def resolve_products(self, info, page=1, page_size=50, search=None,
                         year_published=None, min_players=None, max_players=None,
                         category_id=None, category_name=None, price_min=None, price_max=None):
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
            if category_name is not None:
                if not category_name.strip():
                    raise ValueError("categoryName must not be blank")
                conditions.append(
                    "EXISTS (SELECT 1 FROM regexp_split_to_table(category, ',') AS category_part "
                    "WHERE trim(category_part) = %s)"
                )
                parameters.append(category_name.strip())
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
                "SELECT id, name, stackable, discount_pct, starts_at, ends_at, "
                "created_at, active, product_bgg_id FROM promotion "
                "WHERE NOT EXISTS (SELECT 1 FROM coupon c WHERE c.promotion_id = promotion.id)"
            )
            rows = cursor.fetchall()
        return [_promotion_from_row(row) for row in rows]

    def resolve_admin_coupons(self, info):
        _, error = _coupon_admin_user(info)
        if error:
            raise GraphQLError(
                error.message,
                extensions={"code": error.code},
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.id, c.code, COALESCE(p.discount_pct, 0) AS discount_pct, "
                "c.remaining, c.active, c.starts_at AT TIME ZONE 'UTC' AS starts_at, "
                "c.ends_at AT TIME ZONE 'UTC' AS ends_at, "
                "c.created_at AT TIME ZONE 'UTC' AS created_at "
                "FROM coupon c LEFT JOIN promotion p ON p.id = c.promotion_id "
                "WHERE p.product_bgg_id IS NULL ORDER BY c.id"
            )
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return [_admin_coupon_from_row(row) for row in rows]

    def resolve_admin_audit_logs(
        self, info, entity_type=None, entity_id=None, actor_id=None, action=None,
        created_from=None, created_to=None, cursor=None, limit=50,
    ):
        from django.core import signing

        _, error = _coupon_admin_user(info)
        if error:
            raise GraphQLError(error.message, extensions={"code": error.code})
        if limit is None or not 1 <= limit <= 100:
            raise _audit_invalid("limit")
        clauses, params = [], []
        filters = {
            "entity_type": (entity_type, "target_table"),
            "entity_id": (entity_id, "target_id"),
            "actor_id": (actor_id, "actor"),
            "action": (action, "action"),
        }
        for name, (value, column) in filters.items():
            if value is None:
                continue
            if not value or len(value) > _AUDIT_FILTER_LIMITS[name]:
                raise _audit_invalid(name)
            clauses.append(f"{column} = %s")
            params.append(value)
        bounds = []
        for name, value, operator in (
            ("created_from", created_from, ">="), ("created_to", created_to, "<="),
        ):
            if value is None:
                continue
            try:
                parsed = _coupon_date(value, name)
            except ValueError:
                raise _audit_invalid(name) from None
            bounds.append(parsed)
            clauses.append(f"created_at {operator} %s")
            params.append(parsed)
        if len(bounds) == 2 and bounds[0] > bounds[1]:
            raise _audit_invalid("created_to")
        if cursor is not None:
            cursor_time, cursor_id = _audit_cursor_decode(cursor)
            clauses.append("(created_at, id) < (%s, %s)")
            params.extend([cursor_time, cursor_id])
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        try:
            with connection.cursor() as db:
                db.execute(
                    "SELECT id, actor, action, target_table, target_id, diff, "
                    "created_at AT TIME ZONE 'UTC' AS created_at FROM audit_log "
                    + where + "ORDER BY created_at DESC, id DESC LIMIT %s",
                    [*params, limit + 1],
                )
                rows = db.fetchall()
        except Exception:
            logging.getLogger(__name__).error("Audit lookup failed.")
            raise GraphQLError(
                "Audit lookup failed", extensions={"code": "SERVER_ERROR"}
            ) from None
        page = rows[:limit]
        records = []
        for row_id, actor, row_action, table, target, diff, created_at in page:
            before, after = _audit_changes(diff)
            records.append(AdminAuditRecord(
                id=row_id, actor_id=actor, action=row_action, entity_type=table,
                entity_id=target,
                created_at=_coupon_utc_datetime(created_at).isoformat(),
                before=before, after=after,
            ))
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = signing.dumps(
                {"c": _coupon_utc_datetime(last[6]).isoformat(), "i": last[0]},
                salt=_AUDIT_CURSOR_SALT, compress=False,
            )
        return AdminAuditPage(records=records, next_cursor=next_cursor)

    def resolve_admin_sales_metrics(
        self, info, from_=None, to=None, categories=None, product_ids=None
    ):
        user = _authenticated_user(info)
        if user is None or not getattr(user, "is_active", False):
            raise GraphQLError(
                "Authentication is required.",
                extensions={"code": "UNAUTHENTICATED"},
            )
        if not (
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
        ):
            raise GraphQLError(
                "Administrator permission is required.",
                extensions={"code": "FORBIDDEN"},
            )

        if (from_ is None) != (to is None):
            raise _sales_metrics_invalid("to" if from_ is not None else "from")
        if from_ is None:
            range_to = datetime.datetime.now(datetime.timezone.utc)
            range_from = range_to - datetime.timedelta(days=30)
        else:
            range_from = _sales_metrics_date(from_, "from")
            range_to = _sales_metrics_date(to, "to")
        if range_from >= range_to:
            raise _sales_metrics_invalid("to")
        if range_to - range_from > datetime.timedelta(days=366):
            raise _sales_metrics_invalid("to")

        filters = []
        filter_parameters = []
        if categories is not None:
            filters.append("p.category = ANY(%s)")
            filter_parameters.append(categories)
        if product_ids is not None:
            filters.append("p.bgg_id = ANY(%s)")
            filter_parameters.append(product_ids)
        filter_clause = " AND " + " AND ".join(filters) if filters else ""
        query_time = datetime.datetime.now(datetime.timezone.utc)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(l.subtotal), 0), COUNT(DISTINCT o.id) "
                "FROM purchase_order o "
                "LEFT JOIN purchase_order_line l ON l.order_id = o.id "
                "LEFT JOIN product_boardgame p ON p.bgg_id = l.bgg_id "
                "WHERE o.status = 'paid' AND o.paid_at >= %s AND o.paid_at < %s"
                + filter_clause,
                [range_from, range_to, *filter_parameters],
            )
            revenue_total, orders_count = cursor.fetchone()

            cursor.execute(
                "SELECT p.bgg_id, p.name, SUM(l.quantity), SUM(l.subtotal) "
                "FROM purchase_order o "
                "JOIN purchase_order_line l ON l.order_id = o.id "
                "JOIN product_boardgame p ON p.bgg_id = l.bgg_id "
                "WHERE o.status = 'paid' AND o.paid_at >= %s AND o.paid_at < %s"
                + filter_clause
                + " GROUP BY p.bgg_id, p.name "
                "ORDER BY SUM(l.quantity) DESC, SUM(l.subtotal) DESC, p.bgg_id ASC "
                "LIMIT 10",
                [range_from, range_to, *filter_parameters],
            )
            top_products = [
                TopProduct(bgg_id=row[0], name=row[1], units=row[2], revenue=row[3])
                for row in cursor.fetchall()
            ]

            cursor.execute(
                "SELECT (o.paid_at AT TIME ZONE 'UTC')::date, SUM(l.subtotal) "
                "FROM purchase_order o "
                "JOIN purchase_order_line l ON l.order_id = o.id "
                "JOIN product_boardgame p ON p.bgg_id = l.bgg_id "
                "WHERE o.status = 'paid' AND o.paid_at >= %s AND o.paid_at < %s"
                + filter_clause
                + " GROUP BY (o.paid_at AT TIME ZONE 'UTC')::date "
                "ORDER BY (o.paid_at AT TIME ZONE 'UTC')::date ASC",
                [range_from, range_to, *filter_parameters],
            )
            revenue_by_day = [
                RevenueByDay(date=row[0].isoformat(), revenue=row[1])
                for row in cursor.fetchall()
            ]

            cursor.execute(
                "SELECT COALESCE(o.payment_status, o.status), COUNT(DISTINCT o.id) "
                "FROM purchase_order o "
                "LEFT JOIN purchase_order_line l ON l.order_id = o.id "
                "LEFT JOIN product_boardgame p ON p.bgg_id = l.bgg_id "
                "WHERE o.created_at >= %s AND o.created_at < %s"
                + filter_clause
                + " GROUP BY COALESCE(o.payment_status, o.status) "
                "ORDER BY COALESCE(o.payment_status, o.status)",
                [range_from, range_to, *filter_parameters],
            )
            payment_statuses = [
                StatusCount(status=row[0], count=row[1])
                for row in cursor.fetchall()
            ]

            cursor.execute(
                "SELECT CASE "
                "WHEN o.status = 'pending_payment' THEN 'Pendiente' "
                "WHEN o.paid_at > %s - INTERVAL '24 hours' THEN 'En preparación' "
                "WHEN o.paid_at > %s - INTERVAL '72 hours' THEN 'Enviado' "
                "ELSE 'Entregado' END, COUNT(DISTINCT o.id) "
                "FROM purchase_order o "
                "LEFT JOIN purchase_order_line l ON l.order_id = o.id "
                "LEFT JOIN product_boardgame p ON p.bgg_id = l.bgg_id "
                "WHERE o.created_at >= %s AND o.created_at < %s "
                "AND (o.status = 'pending_payment' OR "
                "(o.status = 'paid' AND o.paid_at IS NOT NULL))"
                + filter_clause
                + " GROUP BY 1 ORDER BY 1",
                [query_time, query_time, range_from, range_to, *filter_parameters],
            )
            dispatch_statuses = [
                StatusCount(status=row[0], count=row[1])
                for row in cursor.fetchall()
            ]

        return SalesMetrics(
            revenue_total=revenue_total,
            orders_count=orders_count,
            top_products=top_products,
            orders_by_dispatch_status=dispatch_statuses,
            orders_by_payment_status=payment_statuses,
            revenue_by_day=revenue_by_day,
        )

    def resolve_admin_sales_categories(self, info):
        user = _authenticated_user(info)
        if user is None or not getattr(user, "is_active", False):
            raise GraphQLError(
                "Authentication is required.",
                extensions={"code": "UNAUTHENTICATED"},
            )
        if not (
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
        ):
            raise GraphQLError(
                "Administrator permission is required.",
                extensions={"code": "FORBIDDEN"},
            )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT category FROM product_boardgame "
                "ORDER BY category"
            )
            return [row[0] for row in cursor.fetchall()]

    def resolve_coupon_by_code(self, info, code):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.id, c.code, c.promotion_id, c.remaining, "
                "c.created_at AT TIME ZONE 'UTC' AS coupon_created_at, "
                "p.name, p.stackable, p.discount_pct, "
                "p.starts_at AS promotion_starts_at, "
                "p.ends_at AS promotion_ends_at, "
                "p.created_at AS promotion_created_at, "
                "p.active AS promotion_active, p.product_bgg_id, "
                "c.active AS coupon_active, "
                "c.starts_at AT TIME ZONE 'UTC' AS coupon_starts_at, "
                "c.ends_at AT TIME ZONE 'UTC' AS coupon_ends_at "
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
            promotion = _promotion_from_row((
                r["promotion_id"], r["name"], r["stackable"], r["discount_pct"],
                r["promotion_starts_at"], r["promotion_ends_at"],
                r["promotion_created_at"], r["promotion_active"],
                r["product_bgg_id"],
            ))
        return Coupon(
            id=r["id"],
            code=r["code"],
            promotion=promotion,
            remaining=r["remaining"],
            active=r["coupon_active"],
            starts_at=(
                _coupon_utc_datetime(r["coupon_starts_at"]).isoformat()
                if r["coupon_starts_at"] else None
            ),
            ends_at=(
                _coupon_utc_datetime(r["coupon_ends_at"]).isoformat()
                if r["coupon_ends_at"] else None
            ),
            created_at=_coupon_utc_datetime(r["coupon_created_at"]).isoformat(),
        )

    def resolve_available_coupons(self, info):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT code, starts_at AT TIME ZONE 'UTC', ends_at AT TIME ZONE 'UTC' FROM coupon "
                "WHERE active = TRUE AND remaining > 0 "
                "AND starts_at <= CURRENT_TIMESTAMP AND ends_at > CURRENT_TIMESTAMP "
                "ORDER BY code"
            )
            rows = cursor.fetchall()
        return [AvailableCoupon(code=code, starts_at=starts_at.replace(tzinfo=datetime.timezone.utc).isoformat(),
                    ends_at=ends_at.replace(tzinfo=datetime.timezone.utc).isoformat())
                for code, starts_at, ends_at in rows]

    def resolve_preview_coupon(self, info, code):
        return preview_coupon(info, code)

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


class AdminCouponPayload(graphene.ObjectType):
    coupon = graphene.Field(AdminCoupon)
    user_errors = graphene.List(graphene.NonNull(UserError), required=True)


class AdminAuditRecord(graphene.ObjectType):
    id = graphene.BigInt(required=True)
    actor_id = graphene.String()
    action = graphene.String(required=True)
    entity_type = graphene.String()
    entity_id = graphene.String()
    created_at = graphene.String(required=True)
    before = graphene.JSONString()
    after = graphene.JSONString()

    class Meta:
        description = (
            "Redacted administrative change record. `before` and `after` are JSON "
            "objects (or null when absent). Values under keys naming credentials, "
            "tokens, passwords, secrets, authorization data, or payment/card data "
            'are replaced with "[REDACTED]" before the record is returned.'
        )


class AdminAuditPage(graphene.ObjectType):
    records = graphene.List(graphene.NonNull(AdminAuditRecord), required=True)
    next_cursor = graphene.String()

    class Meta:
        description = "One page of audit records ordered by createdAt DESC, id DESC."


class StockMovementStatus(graphene.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class StockMovementStatusPayload(graphene.ObjectType):
    movement_id = graphene.ID(required=True)
    status = graphene.Field(StockMovementStatus)
    user_errors = graphene.List(graphene.NonNull(UserError), required=True)


class StockMovementDirection(graphene.Enum):
    IN = "IN"
    OUT = "OUT"


class AdjustProductStockPayload(graphene.ObjectType):
    movement_id = graphene.ID(required=True)
    status = graphene.Field(StockMovementStatus)
    requested_quantity = graphene.Int()
    applied_quantity = graphene.Int()
    quantity_before = graphene.Int()
    quantity_after = graphene.Int()
    errors = graphene.List(graphene.NonNull(UserError), required=True)


class RegisterPayload(graphene.ObjectType):
    user = graphene.Field(AuthUser)
    tokens = graphene.Field(AuthToken)
    errors = graphene.List(graphene.NonNull(UserError), required=True)


class LoginPayload(RegisterPayload):
    pass


def _auth_error(message, code, field=None):
    return UserError(field=field, code=code, message=message)


def _coupon_admin_user(info):
    user = _authenticated_user(info)
    if user is None or not getattr(user, "is_active", False):
        return None, _auth_error("Authentication required", "UNAUTHENTICATED")
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        return None, _auth_error("Administrator permission required", "FORBIDDEN")
    return user, None


def _coupon_date(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UTC date-time or null")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be a UTC date-time or null") from None
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def _coupon_dates_valid(starts_at, ends_at):
    return starts_at is None or ends_at is None or starts_at < ends_at


def _coupon_audit_values(row):
    return {
        "code": row["code"],
        "discount_percent": float(row["discount_pct"]),
        "remaining": row["remaining"],
        "active": row["active"],
        "starts_at": (
            _coupon_utc_datetime(row["starts_at"]).isoformat()
            if row["starts_at"] else None
        ),
        "ends_at": (
            _coupon_utc_datetime(row["ends_at"]).isoformat()
            if row["ends_at"] else None
        ),
    }


_AUDIT_SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token|credential|authorization|api[_-]?key|card|cvv|cvc|payment|iban",
    re.IGNORECASE,
)
_AUDIT_SECRET_VALUE = re.compile(
    r"^(?:Bearer\s+\S+|[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,})$"
)
_PRODUCT_AUDIT_COLUMNS = (
    "bgg_id, name, price_amount, quantity, publisher, image_url, image_filename, "
    "release_year, min_players, max_players, playing_time_minutes, min_age, max_age, "
    "weight, category"
)
_PROMOTION_COLUMNS = (
    "id, name, stackable, discount_pct, starts_at, ends_at, created_at, active, "
    "product_bgg_id"
)


def _prefixed(columns, alias):
    return ", ".join(f"{alias}.{column.strip()}" for column in columns.split(","))

_AUDIT_INSERT = (
    "INSERT INTO audit_log (actor, action, target_table, target_id, diff, created_at) "
    "SELECT %s, '{action}', '{table}', {target}::text, "
    "jsonb_build_object('previous', {previous}, 'new', to_jsonb({new}) - 'previous'), CURRENT_TIMESTAMP "
)
_AUDIT_CURSOR_SALT = "admin-audit-cursor"
_AUDIT_FILTER_LIMITS = {
    "entity_type": 150, "entity_id": 64, "actor_id": 150, "action": 100,
}


def _audit_redact(value, depth=0):
    if depth > 12:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _AUDIT_SENSITIVE_KEY.search(str(key))
            else _audit_redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_audit_redact(item, depth + 1) for item in value]
    if isinstance(value, str) and _AUDIT_SECRET_VALUE.match(value):
        return "[REDACTED]"
    return value


def _audit_changes(diff):
    if isinstance(diff, str):
        try:
            diff = json.loads(diff)
        except ValueError:
            return None, None
    if not isinstance(diff, dict):
        return None, None
    if "previous" in diff:
        before, after = diff.get("previous"), diff.get("new")
    elif "old" in diff:
        before, after = {"quantity": diff.get("old")}, {"quantity": diff.get("new")}
    else:
        return None, None
    return (
        _audit_redact(before) if isinstance(before, dict) else None,
        _audit_redact(after) if isinstance(after, dict) else None,
    )


def _audit_actor(user):
    pk = getattr(user, "pk", None)
    return None if pk is None else str(pk)


def _audit_invalid(field):
    return GraphQLError(
        f"{field} is invalid", extensions={"code": "INVALID", "field": field}
    )


def _audit_cursor_decode(value):
    from django.core import signing

    if len(value) > 512:
        raise _audit_invalid("cursor")
    try:
        data = signing.loads(value, salt=_AUDIT_CURSOR_SALT, max_age=None)
        created_at = datetime.datetime.fromisoformat(data["c"])
        row_id = data["i"]
        if (
            created_at.utcoffset() is None
            or type(row_id) is not int
            or row_id < 1
        ):
            raise ValueError
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise _audit_invalid("cursor") from None
    return created_at, row_id


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
        from opkeys.utils import generate_raw_key, key_hash, default_expiry, internal_secret_configured, utc_isoformat
        from opkeys.models import OpKeyAudit, OpSession

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
            if getattr(dj_settings, "OPKEYS_ISSUE_ON_LOGIN", False) and internal_secret_configured() and (
                getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
            ):
                raw = generate_raw_key()
                h = key_hash(raw)
                # default ops: admin endpoints wildcard for main HTTP methods
                methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
                ops_list = [f"{m}:/api/v1/admin/*" for m in methods]
                with transaction.atomic(using=OpSession.objects.db):
                    sess = OpSession.objects.create(user=user, key_hash=h, ops=ops_list, expires_at=default_expiry())
                    OpKeyAudit.objects.create(
                        session_id=sess.id, subject_user_id=user.pk, actor_user_id=user.pk,
                        actor_source="AUTHENTICATED_USER", action="ISSUE",
                    )
                # expose raw op_key only once in the login payload
                tokens.op_key = raw
                tokens.op_key_expires_at = utc_isoformat(sess.expires_at)
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


class ModerateReviewMutation(graphene.Mutation):
    class Arguments:
        review_id = graphene.ID(required=True)
        action = graphene.Argument(ReviewModerationAction, required=True)

    Output = ModerateReviewPayload

    @staticmethod
    def mutate(_root, info, review_id, action):
        auth_error = _moderation_user_error(info)
        if auth_error is not None:
            return ModerateReviewPayload(
                success=False,
                user_errors=[auth_error],
            )

        action_value = getattr(action, "value", action)
        if action_value != "ACCEPT":
            return ModerateReviewPayload(
                success=False,
                user_errors=[_moderation_error(
                    "INVALID_TRANSITION",
                    "Only pending reviews can be accepted by this operation.",
                )],
            )
        try:
            identifier = int(review_id)
            if identifier < 1:
                raise ValueError
        except (TypeError, ValueError):
            return ModerateReviewPayload(
                success=False,
                user_errors=[_moderation_error(
                    "REVIEW_NOT_FOUND", "Review not found.", ["reviewId"]
                )],
            )

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT review.id, review.bgg_id, game.name, review.rating, "
                    "review.body, review.created_at, review.status, review.is_deleted "
                    "FROM product_review review "
                    "JOIN product_boardgame game ON game.bgg_id = review.bgg_id "
                    "WHERE review.id = %s AND review.is_deleted = FALSE "
                    "FOR UPDATE OF review",
                    [identifier],
                )
                row = cursor.fetchone()
                if row is None:
                    return ModerateReviewPayload(
                        success=False,
                        user_errors=[_moderation_error(
                            "REVIEW_NOT_FOUND", "Review not found.", ["reviewId"]
                        )],
                    )

                if row[6] == "PENDING":
                    cursor.execute(
                        "UPDATE product_review SET status = 'ACCEPTED', "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = %s AND status = 'PENDING' "
                        "AND is_deleted = FALSE",
                        [identifier],
                    )
                    cursor.execute(
                        "SELECT review.id, review.bgg_id, game.name, review.rating, "
                        "review.body, review.created_at, review.status, "
                        "review.is_deleted FROM product_review review "
                        "JOIN product_boardgame game ON game.bgg_id = review.bgg_id "
                        "WHERE review.id = %s AND review.is_deleted = FALSE",
                        [identifier],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return ModerateReviewPayload(
                            success=False,
                            user_errors=[_moderation_error(
                                "REVIEW_NOT_FOUND",
                                "Review not found.",
                                ["reviewId"],
                            )],
                        )
                elif row[6] != "ACCEPTED":
                    return ModerateReviewPayload(
                        success=False,
                        user_errors=[_moderation_error(
                            "INVALID_TRANSITION",
                            "Only pending reviews can be accepted by this operation.",
                        )],
                    )

        return ModerateReviewPayload(
            success=True,
            review=_admin_review_from_row(row),
            user_errors=[],
        )


class CreateProductInput(graphene.InputObjectType):
    class Meta:
        description = (
            "Create a product with a positive base price and exactly one .webp image\n"
            "reference in imageUrl or imageFileName and a nonnegative initial quantity.\n"
            "Product administration requires an active staff or superuser account."
        )

    name = graphene.String(required=True)
    price = graphene.Float(
        required=True, description="Strictly greater than zero."
    )
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
            with transaction.atomic(), connection.cursor() as cursor:
                # Use provided bggId if present, otherwise generate next sequential bgg_id
                provided_bgg = getattr(input, 'bggId', None)
                if provided_bgg is None:
                    cursor.execute("SELECT COALESCE(MAX(bgg_id), 0) + 1 FROM product_boardgame")
                    provided_bgg = cursor.fetchone()[0]

                cursor.execute(
                    "WITH ins AS (INSERT INTO product_boardgame (bgg_id, name, price_amount, quantity, publisher, image_url, image_filename, release_year, min_players, max_players, playing_time_minutes, min_age, max_age, weight, category) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING " + _PRODUCT_AUDIT_COLUMNS + "), "
                    "aud AS (" + _AUDIT_INSERT.format(action="product_created", table="product_boardgame", target="ins.bgg_id", previous="'null'::jsonb", new="ins") + "FROM ins RETURNING id) "
                    "SELECT bgg_id, name FROM ins",
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
                        _audit_actor(user),
                    ],
                )
                row = cursor.fetchone()
                record_catalog_event(cursor, "product.changed", row[0])
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
    class Meta:
        description = (
            "Partial product update; BGG ID and quantity are immutable through this input.\n"
            "When changing the image, provide exactly one .webp imageUrl or imageFileName."
        )

    name = graphene.String()
    price = graphene.Float(description="Must be strictly greater than zero.")
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
            with transaction.atomic(), connection.cursor() as cursor:
                # Build update statement dynamically
                fields = []
                values = []
                if input.name is not None:
                    fields.append("name = %s")
                    values.append(input.name.strip())
                if input.price is not None:
                    fields.append("price_amount = %s")
                    values.append(input.price)
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
                values = [*values, id, _audit_actor(user)]
                sql = (
                    f"WITH upd AS (UPDATE product_boardgame AS p SET {', '.join(fields)} "
                    f"FROM (SELECT {_PRODUCT_AUDIT_COLUMNS} FROM product_boardgame WHERE bgg_id = %s FOR UPDATE) AS o "
                    f"WHERE p.bgg_id = o.bgg_id RETURNING {_prefixed(_PRODUCT_AUDIT_COLUMNS, 'p')}, to_jsonb(o) AS previous), "
                    "aud AS (" + _AUDIT_INSERT.format(action="product_updated", table="product_boardgame", target="upd.bgg_id", previous="upd.previous", new="upd")
                    + "FROM upd RETURNING id) "
                    "SELECT bgg_id, name, price_amount, quantity FROM upd"
                )
                cursor.execute(sql, values)
                row = cursor.fetchone()
                if not row:
                    return UpdateProductPayload(product=None, userErrors=[_auth_error("Product not found", "PRODUCT_NOT_FOUND")])
                record_catalog_event(cursor, "product.changed", row[0])
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


class AddProductStockPayload(graphene.ObjectType):
    success = graphene.Boolean()
    errors = graphene.List(graphene.NonNull(UserError), required=True)


class AddProductStockMutation(graphene.Mutation):
    class Arguments:
        productId = graphene.ID(required=True)
        newStock = graphene.Int(required=True)

    Output = AddProductStockPayload

    @staticmethod
    def mutate(_root, info, productId, newStock):
        user = _authenticated_user(info)
        if user is None or not getattr(user, "is_active", False):
            return AddProductStockPayload(success=False, errors=[_auth_error("Authentication required", "UNAUTHENTICATED")])
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return AddProductStockPayload(success=False, errors=[_auth_error("Administrator access required", "FORBIDDEN")])
        return AddProductStockPayload(success=False, errors=[_auth_error("Use a UUID-bound stock movement", "MIGRATION_REQUIRED")])


class AdjustProductStockMutation(graphene.Mutation):
    class Arguments:
        product_id = graphene.ID(required=True)
        movement_id = graphene.ID(required=True)
        direction = StockMovementDirection(required=True)
        quantity = graphene.Int(required=True)

    Output = AdjustProductStockPayload

    @staticmethod
    def mutate(_root, info, product_id, movement_id, direction, quantity):
        def failure(code, message, field=None):
            return AdjustProductStockPayload(movement_id=movement_id, errors=[_auth_error(message, code, field)])

        try:
            user = _authenticated_user(info)
            if user is None or not getattr(user, "is_active", False):
                return failure("UNAUTHENTICATED", "Authentication required")
            if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
                return failure("FORBIDDEN", "Administrator access required")
            normalized_product = product_id.lstrip("0")
            if (not product_id.isascii() or not product_id.isdecimal() or not normalized_product
                    or len(normalized_product) > 10 or int(normalized_product) > 2147483647):
                return failure("INVALID", "Invalid product identifier", ["productId"])
            try:
                normalized_movement = str(uuid.UUID(movement_id))
            except (ValueError, TypeError, AttributeError):
                return failure("INVALID", "Invalid movement identifier", ["movementId"])
            if quantity <= 0:
                return failure("INVALID", "Quantity must be positive", ["quantity"])
            actor_id = int(user.pk)
            if actor_id <= 0:
                return failure("UNAUTHENTICATED", "Authentication required")
            direction_value = direction.value
            product_number = int(normalized_product)
            request = info.context
            if getattr(request, "opkey_delegated", False) and not request.headers.get("Authorization", ""):
                path = request.headers.get("X-Op-Path", "")
                match = re.fullmatch(r"/api/v1/admin/products/([0-9]+)/stock", path)
                if (request.headers.get("X-Op-Method", "").upper() != "POST" or not match
                        or match.group(1).lstrip("0") != normalized_product):
                    return failure("FORBIDDEN", "Administrator access required")
            with transaction.atomic(durable=True):
                with connection.cursor() as cursor:
                    cursor.execute("INSERT INTO stock_movement_status (movement_id, status) VALUES (%s, 'IN_PROGRESS') ON CONFLICT (movement_id) DO NOTHING RETURNING movement_id", [normalized_movement])
                    created = cursor.fetchone() is not None
                    if created:
                        cursor.execute("INSERT INTO stock_movement_details (movement_id, product_id, direction, requested_quantity, actor_id) VALUES (%s, %s, %s, %s, %s)", [normalized_movement, product_number, direction_value, quantity, actor_id])
                    else:
                        cursor.execute("SELECT status FROM stock_movement_status WHERE movement_id = %s", [normalized_movement])
                        status_row = cursor.fetchone()
                        cursor.execute("SELECT product_id, direction, requested_quantity, applied_quantity, quantity_before, quantity_after FROM stock_movement_details WHERE movement_id = %s", [normalized_movement])
                        detail = cursor.fetchone()
                        if not status_row or not detail:
                            return failure("SERVER_ERROR", "Movement details unavailable")
                        if (detail[0], detail[1], detail[2]) != (product_number, direction_value, quantity):
                            return failure("IDEMPOTENCY_CONFLICT", "Movement identifier already used with different data")
                        if status_row[0] == "APPLIED":
                            if any(value is None for value in detail[3:]):
                                return failure("SERVER_ERROR", "Movement details unavailable")
                            return AdjustProductStockPayload(movement_id=normalized_movement, status="APPLIED", requested_quantity=quantity, applied_quantity=detail[3], quantity_before=detail[4], quantity_after=detail[5], errors=[])
                        if status_row[0] not in ("IN_PROGRESS", "REJECTED"):
                            return failure("SERVER_ERROR", "Movement details unavailable")
            rolled_back = False
            with transaction.atomic(durable=True):
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT status FROM stock_movement_status WHERE movement_id = %s FOR UPDATE SKIP LOCKED", [normalized_movement])
                        locked_status = cursor.fetchone()
                        if locked_status is None:
                            return AdjustProductStockPayload(movement_id=normalized_movement, status="IN_PROGRESS", errors=[])
                        if locked_status[0] == "APPLIED":
                            cursor.execute("SELECT applied_quantity, quantity_before, quantity_after FROM stock_movement_details WHERE movement_id = %s", [normalized_movement])
                            applied_details = cursor.fetchone()
                            if not applied_details or any(value is None for value in applied_details):
                                return failure("SERVER_ERROR", "Movement details unavailable")
                            return AdjustProductStockPayload(movement_id=normalized_movement, status="APPLIED", requested_quantity=quantity, applied_quantity=applied_details[0], quantity_before=applied_details[1], quantity_after=applied_details[2], errors=[])
                        if locked_status[0] == "REJECTED":
                            cursor.execute("UPDATE stock_movement_status SET status = 'IN_PROGRESS' WHERE movement_id = %s", [normalized_movement])
                            cursor.execute("UPDATE stock_movement_details SET actor_id = %s WHERE movement_id = %s", [actor_id, normalized_movement])
                        elif locked_status[0] != "IN_PROGRESS":
                            return failure("SERVER_ERROR", "Movement details unavailable")
                        cursor.execute("SELECT quantity FROM product_boardgame WHERE bgg_id = %s FOR UPDATE", [product_number])
                        row = cursor.fetchone()
                        if row is None:
                            cursor.execute("UPDATE stock_movement_status SET status = 'REJECTED' WHERE movement_id = %s", [normalized_movement])
                            return failure("NOT_FOUND", "Product not found")
                        before = row[0]
                        if before < 0:
                            raise ValueError("Invalid stored stock")
                        if direction_value == "IN":
                            applied = quantity
                            after = before + quantity
                            if after > 2147483647:
                                cursor.execute("UPDATE stock_movement_status SET status = 'REJECTED' WHERE movement_id = %s", [normalized_movement])
                                return failure("INVALID", "Stock exceeds the supported limit", ["quantity"])
                        else:
                            applied = min(before, quantity)
                            after = before - applied
                            if not applied:
                                cursor.execute("UPDATE stock_movement_status SET status = 'REJECTED' WHERE movement_id = %s", [normalized_movement])
                                return failure("NO_STOCK", "Product has no stock")
                        cursor.execute("UPDATE product_boardgame SET quantity = %s WHERE bgg_id = %s", [after, product_number])
                        cursor.execute("UPDATE stock_movement_details SET applied_quantity = %s, quantity_before = %s, quantity_after = %s WHERE movement_id = %s", [applied, before, after, normalized_movement])
                        diff = json.dumps({"movement_id": normalized_movement, "direction": direction_value, "requested_quantity": quantity, "applied_quantity": applied, "old": before, "new": after})
                        cursor.execute("INSERT INTO audit_log (actor, action, target_table, target_id, diff, created_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)", [str(actor_id), "stock_" + direction_value.lower(), "product_boardgame", str(product_number), diff])
                        record_catalog_event(cursor, "stock.changed", product_number)
                        cursor.execute("UPDATE stock_movement_status SET status = 'APPLIED' WHERE movement_id = %s", [normalized_movement])
                except Exception:
                    transaction.set_rollback(True)
                    rolled_back = True
            if rolled_back:
                with transaction.atomic(durable=True), connection.cursor() as cursor:
                    cursor.execute("UPDATE stock_movement_status SET status = 'REJECTED' WHERE movement_id = %s AND status = 'IN_PROGRESS'", [normalized_movement])
                return failure("SERVER_ERROR", "Stock movement failed")
            return AdjustProductStockPayload(movement_id=normalized_movement, status="APPLIED", requested_quantity=quantity, applied_quantity=applied, quantity_before=before, quantity_after=after, errors=[])
        except Exception:
            return failure("SERVER_ERROR", "Stock movement failed")


class CreatePromotionInput(graphene.InputObjectType):
    name = graphene.String(required=True)
    discount_percent = graphene.Float(
        required=True,
        description="Percentage discount from zero through 50, inclusive.",
    )
    active = graphene.Boolean(required=True)
    product_bgg_id = graphene.Int(
        description="Optional product BGG identifier; omitted or null creates a global store offer."
    )


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
        if not 0 < input.discount_percent <= 50:
            return CreatePromotionPayload(promotion=None, userErrors=[_auth_error("Discount must be greater than 0 and at most 50", "INVALID", ["discountPercent"])])
        product_bgg_id = getattr(input, "product_bgg_id", None)
        if product_bgg_id is not None and product_bgg_id <= 0:
            return CreatePromotionPayload(
                promotion=None,
                userErrors=[
                    _auth_error(
                        "A product identifier must be positive.",
                        "INVALID",
                        ["productBggId"],
                    )
                ],
            )
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                if product_bgg_id is not None:
                    cursor.execute(
                        "SELECT bgg_id FROM product_boardgame WHERE bgg_id = %s",
                        [product_bgg_id],
                    )
                    if cursor.fetchone() is None:
                        return CreatePromotionPayload(
                            promotion=None,
                            userErrors=[
                                _auth_error(
                                    "Product not found.",
                                    "NOT_FOUND",
                                    ["productBggId"],
                                )
                            ],
                        )
                cursor.execute(
                    "WITH ins AS (INSERT INTO promotion "
                    "(name, discount_pct, starts_at, ends_at, stackable, active, "
                    "product_bgg_id, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) RETURNING "
                    + _PROMOTION_COLUMNS + "), "
                    "aud AS (" + _AUDIT_INSERT.format(action="promotion_created", table="promotion", target="ins.id", previous="'null'::jsonb", new="ins") + "FROM ins RETURNING id) "
                    "SELECT " + _PROMOTION_COLUMNS + " FROM ins",
                    [
                        name, input.discount_percent, None, None, False,
                        input.active, product_bgg_id, _audit_actor(user),
                    ],
                )
                row = cursor.fetchone()
                record_catalog_event(cursor, "promotion.changed", row[0])
            promo = _promotion_from_row(row)
            return CreatePromotionPayload(promotion=promo, userErrors=[])
        except IntegrityError:
            return CreatePromotionPayload(
                promotion=None,
                userErrors=[
                    _auth_error(
                        "An active promotion already exists for this product.",
                        "CONFLICT",
                        ["productBggId"],
                    )
                ],
            )
        except Exception:
            return CreatePromotionPayload(promotion=None, userErrors=[_auth_error("Create promotion failed", "SERVER_ERROR")])


class UpdatePromotionInput(graphene.InputObjectType):
    name = graphene.String()
    discount_percent = graphene.Float(
        description="Percentage discount from zero through 50, inclusive."
    )
    active = graphene.Boolean()
    product_bgg_id = graphene.Int(
        description="Optional product BGG identifier; null clears the product link and creates a global store offer."
    )


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
        if input.discount_percent is not None and not 0 < input.discount_percent <= 50:
            return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("Discount must be greater than 0 and at most 50", "INVALID", ["discountPercent"])])
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                fields = []
                values = []
                if "product_bgg_id" in input:
                    product_bgg_id = input["product_bgg_id"]
                    if product_bgg_id is not None and product_bgg_id <= 0:
                        return UpdatePromotionPayload(
                            promotion=None,
                            userErrors=[
                                _auth_error(
                                    "A product identifier must be positive.",
                                    "INVALID",
                                    ["productBggId"],
                                )
                            ],
                        )
                    if product_bgg_id is not None:
                        cursor.execute(
                            "SELECT bgg_id FROM product_boardgame WHERE bgg_id = %s",
                            [product_bgg_id],
                        )
                        if cursor.fetchone() is None:
                            return UpdatePromotionPayload(
                                promotion=None,
                                userErrors=[
                                    _auth_error(
                                        "Product not found.",
                                        "NOT_FOUND",
                                        ["productBggId"],
                                    )
                                ],
                            )
                    fields.append("product_bgg_id = %s")
                    values.append(product_bgg_id)
                if input.name is not None:
                    fields.append("name = %s")
                    values.append(input.name)
                if input.discount_percent is not None:
                    fields.append("discount_pct = %s")
                    values.append(input.discount_percent)
                if input.active is not None:
                    fields.append("active = %s")
                    values.append(input.active)
                if not fields:
                    return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("No fields to update", "NO_OP")])
                values = [*values, id, _audit_actor(user)]
                sql = (
                    f"WITH upd AS (UPDATE promotion AS p SET {', '.join(fields)} "
                    f"FROM (SELECT {_PROMOTION_COLUMNS} FROM promotion WHERE id = %s FOR UPDATE) AS o "
                    f"WHERE p.id = o.id RETURNING {_prefixed(_PROMOTION_COLUMNS, 'p')}, to_jsonb(o) AS previous), "
                    "aud AS (" + _AUDIT_INSERT.format(action="promotion_updated", table="promotion", target="upd.id", previous="upd.previous", new="upd")
                    + "FROM upd RETURNING id) "
                    f"SELECT {_PROMOTION_COLUMNS} FROM upd"
                )
                cursor.execute(sql, values)
                row = cursor.fetchone()
                if not row:
                    return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("Promotion not found", "NOT_FOUND")])
                record_catalog_event(cursor, "promotion.changed", row[0])
            promo = _promotion_from_row(row)
            return UpdatePromotionPayload(promotion=promo, userErrors=[])
        except IntegrityError:
            return UpdatePromotionPayload(
                promotion=None,
                userErrors=[
                    _auth_error(
                        "An active promotion already exists for this product.",
                        "CONFLICT",
                        ["productBggId"],
                    )
                ],
            )
        except Exception:
            return UpdatePromotionPayload(promotion=None, userErrors=[_auth_error("Update promotion failed", "SERVER_ERROR")])


class AdminCreateCouponInput(graphene.InputObjectType):
    code = graphene.String(
        required=True,
        description="Exact-case unique code; it is not normalized.",
    )
    discount_percent = graphene.Float(
        required=True,
        description="Whole-purchase discount percentage from zero through 50, inclusive.",
    )
    remaining = graphene.BigInt(
        required=True, description="Remaining uses; must be non-negative."
    )
    active = graphene.Boolean(required=True)
    starts_at = graphene.String(
        description="UTC validity start, inclusive; null means no lower bound."
    )
    ends_at = graphene.String(
        description=(
            "UTC validity end, exclusive; when both boundaries exist, start "
            "precedes end."
        )
    )


class AdminUpdateCouponInput(graphene.InputObjectType):
    discount_percent = graphene.Float(
        description="Whole-purchase discount percentage from zero through 50, inclusive."
    )
    remaining = graphene.BigInt(
        description="Remaining uses; must be non-negative."
    )
    active = graphene.Boolean()
    starts_at = graphene.String(
        description="UTC validity start, inclusive; null clears the lower bound."
    )
    ends_at = graphene.String(
        description="UTC validity end, exclusive; null clears the upper bound."
    )


def _coupon_field_error(field, message):
    return AdminCouponPayload(
        coupon=None,
        user_errors=[_auth_error(message, "INVALID", [field])],
    )


def _coupon_record(cursor, coupon_id):
    cursor.execute(
        "SELECT c.id, c.code, COALESCE(p.discount_pct, 0) AS discount_pct, "
        "c.remaining, c.active, c.starts_at AT TIME ZONE 'UTC' AS starts_at, "
        "c.ends_at AT TIME ZONE 'UTC' AS ends_at, "
        "c.created_at AT TIME ZONE 'UTC' AS created_at "
        "FROM coupon c LEFT JOIN promotion p ON p.id = c.promotion_id "
        "WHERE c.id = %s",
        [coupon_id],
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip((column[0] for column in cursor.description), row))


def _coupon_admin_result(cursor, coupon_id, user, action, previous):
    row = _coupon_record(cursor, coupon_id)
    if row is None:
        raise RuntimeError("Coupon disappeared during its transaction")
    from saleor.promotions.models import AuditLog

    AuditLog.objects.create(
        actor=str(user.pk),
        action=action,
        target_table="coupon",
        target_id=str(coupon_id),
        diff={"previous": previous, "new": _coupon_audit_values(row)},
    )
    return _admin_coupon_from_row(row)


def _coupon_code_conflict(exc):
    cause = getattr(exc, "__cause__", None)
    diagnostic = getattr(cause, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == "coupon_code_key"


class AdminCreateCouponMutation(graphene.Mutation):
    class Arguments:
        input = AdminCreateCouponInput(required=True)

    Output = AdminCouponPayload

    @staticmethod
    def mutate(_root, info, input):
        user, auth_error = _coupon_admin_user(info)
        if auth_error:
            return AdminCouponPayload(coupon=None, user_errors=[auth_error])

        code = input.get("code")
        discount = input.get("discount_percent")
        remaining = input.get("remaining")
        active = input.get("active")
        if not isinstance(code, str) or not code or len(code) > 64:
            return _coupon_field_error("code", "Code must contain 1 to 64 characters.")
        if isinstance(discount, bool) or not isinstance(discount, (int, float)):
            return _coupon_field_error("discountPercent", "Discount must be a number from 0 to 50.")
        try:
            discount = Decimal(str(discount))
        except (InvalidOperation, ValueError):
            return _coupon_field_error("discountPercent", "Discount must be a number from 0 to 50.")
        if not discount.is_finite() or not Decimal("0") <= discount <= Decimal("50"):
            return _coupon_field_error("discountPercent", "Discount must be a number from 0 to 50.")
        if type(remaining) is not int or not 0 <= remaining <= 9223372036854775807:
            return _coupon_field_error("remaining", "Remaining uses must be a non-negative integer.")
        if type(active) is not bool:
            return _coupon_field_error("active", "Active must be a boolean.")
        try:
            starts_at = _coupon_date(input.get("starts_at"), "startsAt")
            ends_at = _coupon_date(input.get("ends_at"), "endsAt")
        except ValueError as exc:
            field = "startsAt" if "startsAt" in str(exc) else "endsAt"
            return _coupon_field_error(field, str(exc))
        if not _coupon_dates_valid(starts_at, ends_at):
            return _coupon_field_error("endsAt", "End date must be later than start date.")

        previous = None
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO promotion "
                        "(name, active, stackable, discount_pct, starts_at, ends_at, created_at) "
                        "VALUES (%s, TRUE, FALSE, %s, NULL, NULL, CURRENT_TIMESTAMP) "
                        "RETURNING id",
                        [f"Coupon {code}", discount],
                    )
                    promotion_id = cursor.fetchone()[0]
                    cursor.execute(
                        "INSERT INTO coupon "
                        "(code, promotion_id, remaining, active, starts_at, ends_at, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP) "
                        "RETURNING id",
                        [code, promotion_id, remaining, active, starts_at, ends_at],
                    )
                    coupon_id = cursor.fetchone()[0]
                    coupon = _coupon_admin_result(
                        cursor, coupon_id, user, "coupon_created", previous
                    )
            return AdminCouponPayload(coupon=coupon, user_errors=[])
        except IntegrityError as exc:
            if _coupon_code_conflict(exc):
                return AdminCouponPayload(
                    coupon=None,
                    user_errors=[
                        _auth_error("A coupon with this code already exists.", "CODE_EXISTS", ["code"])
                    ],
                )
            return AdminCouponPayload(
                coupon=None,
                user_errors=[_auth_error("Could not create coupon.", "SERVER_ERROR")],
            )
        except Exception:
            return AdminCouponPayload(
                coupon=None,
                user_errors=[_auth_error("Could not create coupon.", "SERVER_ERROR")],
            )


class AdminUpdateCouponMutation(graphene.Mutation):
    class Arguments:
        id = graphene.Int(required=True)
        input = AdminUpdateCouponInput(required=True)

    Output = AdminCouponPayload

    @staticmethod
    def mutate(_root, info, id, input):
        user, auth_error = _coupon_admin_user(info)
        if auth_error:
            return AdminCouponPayload(coupon=None, user_errors=[auth_error])
        present = set(input.keys())
        if not present:
            return AdminCouponPayload(
                coupon=None,
                user_errors=[_auth_error("At least one field must be provided.", "NO_OP")],
            )

        updates = {}
        if "discount_percent" in present:
            discount = input.get("discount_percent")
            if isinstance(discount, bool) or not isinstance(discount, (int, float)):
                return _coupon_field_error("discountPercent", "Discount must be a number from 0 to 50.")
            try:
                discount = Decimal(str(discount))
            except (InvalidOperation, ValueError):
                return _coupon_field_error("discountPercent", "Discount must be a number from 0 to 50.")
            if not discount.is_finite() or not Decimal("0") <= discount <= Decimal("50"):
                return _coupon_field_error("discountPercent", "Discount must be a number from 0 to 50.")
            updates["discount_percent"] = discount
        if "remaining" in present:
            remaining = input.get("remaining")
            if type(remaining) is not int or not 0 <= remaining <= 9223372036854775807:
                return _coupon_field_error("remaining", "Remaining uses must be a non-negative integer.")
            updates["remaining"] = remaining
        if "active" in present:
            active = input.get("active")
            if type(active) is not bool:
                return _coupon_field_error("active", "Active must be a boolean.")
            updates["active"] = active
        for field in ("starts_at", "ends_at"):
            if field in present:
                graph_field = "startsAt" if field == "starts_at" else "endsAt"
                try:
                    updates[field] = _coupon_date(input.get(field), graph_field)
                except ValueError as exc:
                    return _coupon_field_error(graph_field, str(exc))

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, code, promotion_id, remaining, active, "
                        "starts_at AT TIME ZONE 'UTC' AS starts_at, "
                        "ends_at AT TIME ZONE 'UTC' AS ends_at, created_at "
                        "FROM coupon WHERE id = %s FOR UPDATE",
                        [id],
                    )
                    raw = cursor.fetchone()
                    if raw is None:
                        return AdminCouponPayload(
                            coupon=None,
                            user_errors=[_auth_error("Coupon not found.", "NOT_FOUND")],
                        )
                    previous_coupon = dict(
                        zip((column[0] for column in cursor.description), raw)
                    )
                    previous_coupon["starts_at"] = _coupon_utc_datetime(
                        previous_coupon["starts_at"]
                    )
                    previous_coupon["ends_at"] = _coupon_utc_datetime(
                        previous_coupon["ends_at"]
                    )
                    promotion_id = previous_coupon["promotion_id"]
                    previous_discount = Decimal("0")
                    if promotion_id is not None:
                        cursor.execute(
                            "SELECT discount_pct, product_bgg_id FROM promotion "
                            "WHERE id = %s FOR UPDATE",
                            [promotion_id],
                        )
                        promotion = cursor.fetchone()
                        if promotion is None or promotion[1] is not None:
                            return AdminCouponPayload(
                                coupon=None,
                                user_errors=[
                                    _auth_error(
                                        "Coupon does not have a product-less promotion.",
                                        "PROMOTION_NOT_FOUND",
                                    )
                                ],
                            )
                        previous_discount = Decimal(str(promotion[0]))
                    else:
                        return AdminCouponPayload(
                            coupon=None,
                            user_errors=[
                                _auth_error(
                                    "Coupon does not have a product-less promotion.",
                                    "PROMOTION_NOT_FOUND",
                                )
                            ],
                        )

                    starts_at = updates.get("starts_at", previous_coupon["starts_at"])
                    ends_at = updates.get("ends_at", previous_coupon["ends_at"])
                    if not _coupon_dates_valid(starts_at, ends_at):
                        return _coupon_field_error(
                            "endsAt", "End date must be later than start date."
                        )
                    previous = {
                        "code": previous_coupon["code"],
                        "discount_percent": float(previous_discount),
                        "remaining": previous_coupon["remaining"],
                        "active": previous_coupon["active"],
                        "starts_at": (
                            previous_coupon["starts_at"].isoformat()
                            if previous_coupon["starts_at"] else None
                        ),
                        "ends_at": (
                            previous_coupon["ends_at"].isoformat()
                            if previous_coupon["ends_at"] else None
                        ),
                    }
                    if "discount_percent" in updates:
                        cursor.execute(
                            "UPDATE promotion SET discount_pct = %s WHERE id = %s",
                            [updates["discount_percent"], promotion_id],
                        )
                    coupon_updates = {
                        field: updates[field]
                        for field in ("remaining", "active", "starts_at", "ends_at")
                        if field in updates
                    }
                    if coupon_updates:
                        assignments = ", ".join(
                            f"{field} = %s" for field in coupon_updates
                        )
                        cursor.execute(
                            f"UPDATE coupon SET {assignments} WHERE id = %s",
                            [*coupon_updates.values(), id],
                        )
                    coupon = _coupon_admin_result(
                        cursor,
                        id,
                        user,
                        "coupon_updated",
                        previous,
                    )
            return AdminCouponPayload(coupon=coupon, user_errors=[])
        except Exception:
            return AdminCouponPayload(
                coupon=None,
                user_errors=[_auth_error("Could not update coupon.", "SERVER_ERROR")],
            )


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


class ManageOperationSessionMutation(graphene.Mutation):
    class Arguments:
        session_id = graphene.ID(required=True)
        action = graphene.String(required=True)

    Output = ManageOperationSessionPayload

    @staticmethod
    def mutate(_root, info, session_id, action):
        try:
            user = _authenticated_user(info)
            if user is None:
                return ManageOperationSessionPayload(success=False, user_errors=[OrderUserError(code="UNAUTHENTICATED", message="Authentication required.")])
            if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
                return ManageOperationSessionPayload(success=False, user_errors=[OrderUserError(code="FORBIDDEN", message="Administrator permission required.")])
            identifier = uuid.UUID(str(session_id))
            if action not in ("REVOKE", "EXPIRE"):
                raise ValueError("Invalid action")
            from django.utils import timezone
            from opkeys.models import OpKeyAudit, OpSession
            changes = {"revoked": True} if action == "REVOKE" else {"expires_at": timezone.now()}
            with transaction.atomic(using=OpSession.objects.db):
                session = OpSession.objects.select_for_update().filter(pk=identifier).first()
                if session is None:
                    return ManageOperationSessionPayload(success=False, user_errors=[OrderUserError(code="NOT_FOUND", message="Operation session not found.")])
                for field, value in changes.items():
                    setattr(session, field, value)
                session.save(update_fields=list(changes))
                OpKeyAudit.objects.create(
                    session_id=session.id, subject_user_id=session.user_id, actor_user_id=user.pk,
                    actor_source="ADMIN", action=action,
                )
            reference = hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:16]
            logging.getLogger("opkeys.audit").info("OpKey administrative action=%s actor_id=%s reference=%s", action, user.pk, reference)
            return ManageOperationSessionPayload(success=True, user_errors=[])
        except (TypeError, ValueError):
            return ManageOperationSessionPayload(success=False, user_errors=[OrderUserError(code="INVALID", message="Invalid request.")])
        except Exception:
            return ManageOperationSessionPayload(success=False, user_errors=[OrderUserError(code="SERVER_ERROR", message="Could not update operation session.")])


class Mutation(graphene.ObjectType):
    register = RegisterMutation.Field(required=True)
    login = LoginMutation.Field(required=True)
    manage_op_session = ManageOperationSessionMutation.Field(required=True)
    add_to_cart = AddToCartMutation.Field(required=True)
    update_cart_quantity = UpdateCartQuantityMutation.Field(required=True)
    remove_from_cart = RemoveFromCartMutation.Field(required=True)
    add_to_wishlist = AddToWishlistMutation.Field(required=True)
    remove_from_wishlist = RemoveFromWishlistMutation.Field(required=True)
    create_review = CreateReviewMutation.Field(required=True)
    moderate_review = ModerateReviewMutation.Field(required=True)
    # Admin mutations for BFF
    create_product = CreateProductMutation.Field(
        required=True,
        description="Requires an active staff or superuser account.",
    )
    update_product = UpdateProductMutation.Field(
        required=True,
        description="Requires an active staff or superuser account.",
    )
    add_product_stock = AddProductStockMutation.Field(required=True)
    adjust_product_stock = AdjustProductStockMutation.Field(
        required=True,
        description=(
            "UUID-bound stock movement. Retries with the same product, direction and\n"
            "quantity do not duplicate applied work; failed, rejected or no-change\n"
            "operations may retry the exact payload indefinitely. Reusing the UUID with\n"
            "different movement data rejects. OUT over available stock applies partially\n"
            "to zero and reports requested and applied quantities. Requires an active\n"
            "staff or superuser account."
        ),
    )
    create_promotion = CreatePromotionMutation.Field(
        required=True,
        description="Requires an active staff or superuser account.",
    )
    update_promotion = UpdatePromotionMutation.Field(
        required=True,
        description="Requires an active staff or superuser account.",
    )
    create_admin_coupon = AdminCreateCouponMutation.Field(
        required=True,
        description=(
            "Requires an active staff or superuser account. Coupon creation and "
            "its audit\nrecord commit atomically; the percentage is stored on a product-less promotion."
        ),
    )
    update_admin_coupon = AdminUpdateCouponMutation.Field(
        required=True,
        description=(
            "Requires an active staff or superuser account. Code is immutable. Coupon\n"
            "updates and their audit records commit atomically; deactivation replaces deletion."
        ),
    )
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
    checkout = CheckoutMutation.Field(
        required=True,
        description=(
            "A supplied coupon is reserved atomically during checkout. Release only after\n"
            "confirmed failure or cancellation; uncertain provider outcomes retain the\n"
            "reservation for reconciliation and unresolved outcomes require manual review.\n"
            "A timeout alone does not release a reservation. Coupon codes are matched by\n"
            "exact, case-sensitive equality, without normalization.\n"
            "Coupon checkouts require checkoutKey. Retrying the same key with the same\n"
            "payload returns the original order; using it for a different payload conflicts."
        ),
    )
    retry_payment = RetryPaymentMutation.Field(required=True)
    confirm_payment = ConfirmPaymentMutation.Field(required=True)
    resolve_payment_review = ResolvePaymentReviewMutation.Field(required=True)
    release_payment_review = ReleasePaymentReviewMutation.Field(required=True)
    record_payment_attempt_observation = RecordPaymentAttemptObservationMutation.Field(required=True)
    release_payment_attempt = ReleasePaymentAttemptMutation.Field(required=True)


schema = graphene.Schema(query=Query, mutation=Mutation)
