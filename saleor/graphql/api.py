import datetime
import re
import unicodedata
import uuid

import graphene
import jwt
from django.db import IntegrityError, connection, transaction
from graphql import GraphQLError

from . import wishlist
from .orders import (
    CheckoutMutation, ConfirmPaymentMutation, PurchaseOrder, RetryPaymentMutation,
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


class Query(graphene.ObjectType):
    hello = graphene.String(default_value="Saleor API activa y respondiendo OK")
    product_by_slug = graphene.Field(
        ProductDetail,
        slug=graphene.String(required=True),
    )
    products = graphene.Field(
        ProductPage,
        page=graphene.Int(default_value=1),
        page_size=graphene.Int(default_value=20),
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

    def resolve_hello(self, info):
        return "Saleor API activa y respondiendo OK"

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

    def resolve_products(self, info, page=1, page_size=20, search=None,
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


class AuthToken(graphene.ObjectType):
    token = graphene.String()
    refresh_token = graphene.String()
    csrf_token = graphene.String()


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
    return AuthUser(id=user.pk, email=user.email, first_name=user.first_name,
                    last_name=user.last_name, is_active=user.is_active)


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

        user = authenticate(
            request=info.context, username=input.email.strip().casefold(), password=input.password
        )
        if user is None or not user.is_active:
            return LoginPayload(errors=[_auth_error(
                "Invalid email or password.", "INVALID_CREDENTIALS"
            )])
        if input.guest_cart_id:
            _merge_guest_cart(input.guest_cart_id, user)
        return LoginPayload(user=_auth_user(user), tokens=_auth_tokens(user.pk), errors=[])


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


class Mutation(graphene.ObjectType):
    register = RegisterMutation.Field(required=True)
    login = LoginMutation.Field(required=True)
    add_to_cart = AddToCartMutation.Field(required=True)
    update_cart_quantity = UpdateCartQuantityMutation.Field(required=True)
    remove_from_cart = RemoveFromCartMutation.Field(required=True)
    add_to_wishlist = AddToWishlistMutation.Field(required=True)
    remove_from_wishlist = RemoveFromWishlistMutation.Field(required=True)
    create_review = CreateReviewMutation.Field(required=True)
    checkout = CheckoutMutation.Field(required=True)
    retry_payment = RetryPaymentMutation.Field(required=True)
    confirm_payment = ConfirmPaymentMutation.Field(required=True)


schema = graphene.Schema(query=Query, mutation=Mutation)
