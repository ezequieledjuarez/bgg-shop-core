"""Tests for authenticated, persistent Wishlist GraphQL operations."""

import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import django
import jwt
import pytest
from django.conf import settings
from django.test import RequestFactory

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from django.contrib.auth.models import User

from saleor.graphql.api import schema


PRODUCT_COLUMNS = (
    "bgg_id",
    "name",
    "release_year",
    "min_players",
    "max_players",
    "playing_time_minutes",
    "min_age",
    "weight",
    "category",
    "price_amount",
    "publisher",
    "image_url",
    "quantity",
)
PRODUCT = (
    7,
    "Board Game",
    2020,
    2,
    4,
    60,
    10,
    2.5,
    "Strategy, Family",
    12.5,
    "Publisher",
    "/game.jpg",
    8,
)
OTHER_PRODUCT = (
    8,
    "Other Game",
    2021,
    1,
    5,
    45,
    8,
    1.7,
    "Family",
    9.5,
    "Publisher",
    "/other.jpg",
    4,
)


class WishlistDatabase:
    def __init__(self):
        self.products = {PRODUCT[0]: PRODUCT, OTHER_PRODUCT[0]: OTHER_PRODUCT}
        self.entries = set()
        self.statements = []

    def cursor(self):
        return WishlistCursor(self)


class WishlistCursor:
    def __init__(self, database):
        self.database = database
        self.rows = []
        self.description = []

    def execute(self, sql, params=None):
        params = params or []
        normalized = " ".join(sql.split())
        self.database.statements.append((normalized, list(params)))
        self.rows = []
        if normalized.startswith("SELECT bgg_id, name, release_year"):
            product = self.database.products.get(params[0])
            if product is not None:
                self.rows = [product]
                self.description = [(column,) for column in PRODUCT_COLUMNS]
        elif normalized.startswith("SELECT p.bgg_id, p.name, p.release_year"):
            user_id = params[0]
            self.rows = [
                self.database.products[bgg_id]
                for owner_id, bgg_id in sorted(self.database.entries)
                if owner_id == user_id
            ]
            self.description = [(column,) for column in PRODUCT_COLUMNS]
        elif normalized.startswith("INSERT INTO wishlist_entry"):
            self.database.entries.add((params[0], params[1]))
        elif normalized.startswith("DELETE FROM wishlist_entry"):
            self.database.entries.discard((params[0], params[1]))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture
def wishlist_database():
    return WishlistDatabase()


def execute(query, user_id=None, database=None):
    request = RequestFactory().get("/graphql/")
    user = SimpleNamespace(pk=user_id) if user_id is not None else None
    with patch("saleor.graphql.api._authenticated_user", return_value=user):
        with patch("saleor.graphql.wishlist.connection", database):
            with patch("saleor.graphql.wishlist.transaction.atomic", nullcontext):
                return schema.execute(query, context=request)


def test_wishlist_operations_are_exposed_without_client_user_id():
    document = str(schema)

    assert "myWishlist: [Product!]!" in document
    assert "addToWishlist(bggId: Int!): Product!" in document
    assert "removeFromWishlist(bggId: Int!): Product!" in document
    assert "userId" not in document


def test_wishlist_uses_the_verified_access_token_owner(wishlist_database):
    token = jwt.encode(
        {"sub": "11", "token_type": "access"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    request = RequestFactory().get(
        "/graphql/", HTTP_AUTHORIZATION=f"Bearer {token}"
    )
    verified_user = SimpleNamespace(pk=11)
    with patch.object(User.objects, "get", return_value=verified_user) as user_lookup:
        with patch("saleor.graphql.wishlist.connection", wishlist_database):
            with patch("saleor.graphql.wishlist.transaction.atomic", nullcontext):
                result = schema.execute(
                    "{ myWishlist { bggId } }", context=request
                )

    assert result.errors is None
    assert user_lookup.call_args.kwargs == {"pk": 11, "is_active": True}
    wishlist_query = next(
        (sql, params)
        for sql, params in wishlist_database.statements
        if "FROM wishlist_entry w" in sql
    )
    assert "WHERE w.user_id = %s" in wishlist_query[0]
    assert wishlist_query[1] == [11]


@pytest.mark.parametrize(
    "query",
    (
        "{ myWishlist { bggId } }",
        "mutation { addToWishlist(bggId: 7) { bggId } }",
        "mutation { removeFromWishlist(bggId: 7) { bggId } }",
    ),
)
def test_wishlist_rejects_anonymous_access(query, wishlist_database):
    request = RequestFactory().get("/graphql/")
    with patch("saleor.graphql.api._authenticated_user", return_value=None):
        with patch("saleor.graphql.wishlist.connection", wishlist_database):
            with patch(
                "saleor.graphql.wishlist.transaction.atomic", nullcontext
            ):
                result = schema.execute(query, context=request)

    assert result.errors
    assert "Authentication is required." in str(result.errors[0])


def test_wishlist_persists_adds_and_lists_only_the_authenticated_owner(
    wishlist_database,
):
    added = execute(
        "mutation { addToWishlist(bggId: 7) { bggId name price } }",
        user_id=11,
        database=wishlist_database,
    )
    repeated = execute(
        "mutation { addToWishlist(bggId: 7) { bggId name } }",
        user_id=11,
        database=wishlist_database,
    )
    other_user = execute(
        "mutation { addToWishlist(bggId: 8) { bggId } }",
        user_id=12,
        database=wishlist_database,
    )
    owner_list = execute(
        "{ myWishlist { bggId name releaseYear category price } }",
        user_id=11,
        database=wishlist_database,
    )
    other_list = execute(
        "{ myWishlist { bggId } }",
        user_id=12,
        database=wishlist_database,
    )

    assert added.errors is None
    assert added.data["addToWishlist"] == {
        "bggId": 7,
        "name": "Board Game",
        "price": 12.5,
    }
    assert repeated.errors is None
    assert other_user.errors is None
    assert owner_list.data["myWishlist"] == [{
        "bggId": 7,
        "name": "Board Game",
        "releaseYear": 2020,
        "category": ["Strategy", "Family"],
        "price": 12.5,
    }]
    assert other_list.data["myWishlist"] == [{"bggId": 8}]
    assert wishlist_database.entries == {(11, 7), (12, 8)}


def test_wishlist_remove_is_idempotent_and_cannot_remove_another_users_entry(
    wishlist_database,
):
    wishlist_database.entries.update({(11, 7), (12, 7)})

    other_user_remove = execute(
        "mutation { removeFromWishlist(bggId: 7) { bggId } }",
        user_id=11,
        database=wishlist_database,
    )
    repeated_remove = execute(
        "mutation { removeFromWishlist(bggId: 7) { bggId } }",
        user_id=11,
        database=wishlist_database,
    )
    owner_list = execute(
        "{ myWishlist { bggId } }",
        user_id=12,
        database=wishlist_database,
    )

    assert other_user_remove.errors is None
    assert repeated_remove.errors is None
    assert owner_list.data["myWishlist"] == [{"bggId": 7}]
    assert wishlist_database.entries == {(12, 7)}
    deletes = [
        params
        for sql, params in wishlist_database.statements
        if sql.startswith("DELETE FROM wishlist_entry")
    ]
    assert deletes == [[11, 7], [11, 7]]


@pytest.mark.parametrize(
    "query",
    (
        "mutation { addToWishlist(bggId: 999) { bggId } }",
        "mutation { removeFromWishlist(bggId: 999) { bggId } }",
    ),
)
def test_wishlist_rejects_unknown_products(query, wishlist_database):
    result = execute(query, user_id=11, database=wishlist_database)

    assert result.errors
    assert "Product not found." in str(result.errors[0])
