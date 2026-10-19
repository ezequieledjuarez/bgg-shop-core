import os
import sys
from unittest.mock import MagicMock, patch

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from saleor.graphql.api import schema


def test_products_query_maps_rows_and_paginates():
    cursor = MagicMock()
    cursor.fetchone.return_value = (2,)
    cursor.description = [
        ("bgg_id",),
        ("name",),
        ("release_year",),
        ("min_players",),
        ("max_players",),
        ("playing_time_minutes",),
        ("min_age",),
        ("weight",),
        ("category",),
        ("price_amount",),
        ("publisher",),
        ("image_url",),
    ]
    cursor.fetchall.return_value = [
        (1, "Game", 2020, 2, 4, 60, 10, 2.5, "Strategy, Family", 12.5, "Pub", "/game.jpg")
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute(
            """
            { products(page: 2, pageSize: 1) {
              items { bggId name category price imageUrl }
              page pageSize total totalPages
            } }
            """
        )

    assert result.errors is None
    assert result.data["products"] == {
        "items": [{
            "bggId": 1,
            "name": "Game",
            "category": ["Strategy", "Family"],
            "price": 12.5,
            "imageUrl": "/game.jpg",
        }],
        "page": 2,
        "pageSize": 1,
        "total": 2,
        "totalPages": 2,
    }


def test_products_query_applies_filters_before_pagination():
    cursor = MagicMock()
    cursor.fetchone.return_value = (1,)
    cursor.description = [
        ("bgg_id",), ("name",), ("release_year",), ("min_players",),
        ("max_players",), ("playing_time_minutes",), ("min_age",),
        ("weight",), ("category",), ("price_amount",), ("publisher",),
        ("image_url",),
    ]
    cursor.fetchall.return_value = [
        (7, "Game", 2020, 2, 4, 60, 10, 2.5, "Juegos de Estrategia", 12.5, "Pub", "/game.jpg")
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute(
            """
            { products(
              search: "Game", yearPublished: 2020, minPlayers: 2,
              maxPlayers: 4, categoryId: 1, priceMin: 10, priceMax: 20
            ) { items { bggId } total totalPages } }
            """
        )

    assert result.errors is None
    assert result.data["products"]["total"] == 1
    assert result.data["products"]["items"][0]["bggId"] == 7
    count_sql = cursor.execute.call_args_list[0].args[0]
    count_parameters = cursor.execute.call_args_list[0].args[1]
    assert "LOWER(name)" in count_sql
    assert "release_year = %s" in count_sql
    assert "price_amount >= %s" in count_sql
    assert count_parameters == [
        "%Game%", "%Game%", 2020, 2, 4,
        "Juegos de Estrategia", "Juegos de Estrategia,%",
        "%, Juegos de Estrategia", "%, Juegos de Estrategia,%",
        10, 20,
    ]


def test_products_query_rejects_inverted_ranges():
    result = schema.execute(
        "{ products(minPlayers: 4, maxPlayers: 2) { total } }"
    )

    assert result.data["products"] is None
    assert "minPlayers" in str(result.errors[0])


def test_products_query_rejects_inverted_price_range():
    result = schema.execute(
        "{ products(priceMin: 80, priceMax: 10) { total } }"
    )

    assert result.data["products"] is None
    assert "priceMin" in str(result.errors[0])
