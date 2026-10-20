import datetime
import re
import unicodedata

import graphene
import jwt
from django.db import IntegrityError, connection


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
    review_summary = graphene.Field(ReviewSummary, required=True)
    approved_reviews = graphene.List(graphene.NonNull(PublicReview), required=True)


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
    }


def _product_by_slug(cursor, slug):
    cursor.execute(
        "SELECT bgg_id, name, release_year, min_players, max_players, "
        "playing_time_minutes, min_age, weight, category, price_amount, "
        "publisher, image_url FROM product_boardgame"
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
                f"min_age, weight, category, price_amount, publisher, image_url "
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
                category=[item.strip() for item in (row["category"] or "").split(",") if item.strip()],
            ) for row in rows],
            page=page, page_size=page_size, total=total,
            total_pages=(total + page_size - 1) // page_size,
        )


class RegisterInput(graphene.InputObjectType):
    email = graphene.String(required=True)
    password = graphene.String(required=True)
    first_name = graphene.String(required=True)
    last_name = graphene.String(required=True)


class LoginInput(graphene.InputObjectType):
    email = graphene.String(required=True)
    password = graphene.String(required=True)


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
        return LoginPayload(user=_auth_user(user), tokens=_auth_tokens(user.pk), errors=[])


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
    create_review = CreateReviewMutation.Field(required=True)


schema = graphene.Schema(query=Query, mutation=Mutation)
