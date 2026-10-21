"""Authenticated Wishlist persistence and product lookup operations."""

from django.db import connection, transaction
from graphql import GraphQLError


_PRODUCT_SELECT = (
    "SELECT bgg_id, name, release_year, min_players, max_players, "
    "playing_time_minutes, min_age, weight, category, price_amount, "
    "publisher, image_url, quantity FROM product_boardgame"
)


def _wishlist_user(info):
    from .api import _authenticated_user

    user = _authenticated_user(info)
    if user is None:
        raise GraphQLError("Authentication is required.")
    return user


def _product(cursor, bgg_id):
    cursor.execute(f"{_PRODUCT_SELECT} WHERE bgg_id = %s", [bgg_id])
    row = cursor.fetchone()
    if row is None:
        raise GraphQLError("Product not found.")

    columns = [column[0] for column in cursor.description]
    product = dict(zip(columns, row))
    from .api import _product_from_row

    return {"bgg_id": product["bgg_id"], **_product_from_row(product)}


def list_products(info):
    user = _wishlist_user(info)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.bgg_id, p.name, p.release_year, p.min_players, "
            "p.max_players, p.playing_time_minutes, p.min_age, p.weight, "
            "p.category, p.price_amount, p.publisher, p.image_url, p.quantity "
            "FROM wishlist_entry w "
            "JOIN product_boardgame p ON p.bgg_id = w.bgg_id "
            "WHERE w.user_id = %s ORDER BY p.name, p.bgg_id",
            [user.pk],
        )
        columns = [column[0] for column in cursor.description]
        products = [dict(zip(columns, row)) for row in cursor.fetchall()]

    from .api import _product_from_row

    return [
        {"bgg_id": product["bgg_id"], **_product_from_row(product)}
        for product in products
    ]


def add_product(info, bgg_id):
    user = _wishlist_user(info)
    with transaction.atomic(), connection.cursor() as cursor:
        product = _product(cursor, bgg_id)
        cursor.execute(
            "INSERT INTO wishlist_entry (user_id, bgg_id) VALUES (%s, %s) "
            "ON CONFLICT (user_id, bgg_id) DO NOTHING",
            [user.pk, bgg_id],
        )
    return product


def remove_product(info, bgg_id):
    user = _wishlist_user(info)
    with transaction.atomic(), connection.cursor() as cursor:
        product = _product(cursor, bgg_id)
        cursor.execute(
            "DELETE FROM wishlist_entry WHERE user_id = %s AND bgg_id = %s",
            [user.pk, bgg_id],
        )
    return product
