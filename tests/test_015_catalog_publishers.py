from unittest.mock import MagicMock, patch

import pytest

from saleor.graphql.api import schema


def _connection(cursor):
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection


def test_product_publishers_returns_complete_sorted_global_distinct_game_counts():
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("A Publisher", 4),
        ("a Publisher", 2),
        ("B Publisher", 1),
    ]

    with patch("saleor.graphql.api.connection", _connection(cursor)):
        result = schema.execute(
            "{ productPublishers { publisher productCount } }"
        )

    assert result.errors is None
    assert result.data["productPublishers"] == [
        {"publisher": "A Publisher", "productCount": 4},
        {"publisher": "a Publisher", "productCount": 2},
        {"publisher": "B Publisher", "productCount": 1},
    ]
    sql = cursor.execute.call_args.args[0]
    normalized_sql = " ".join(sql.split()).lower()
    assert "count(distinct bgg_id)" in normalized_sql
    assert "btrim(publisher)" in normalized_sql
    assert "group by btrim(publisher)" in normalized_sql
    assert "order by lower(btrim(publisher)), btrim(publisher)" in normalized_sql
    assert "quantity" not in normalized_sql
    assert "limit" not in normalized_sql
    assert len(cursor.execute.call_args.args) == 1


def test_products_combines_exact_trimmed_publisher_with_existing_filters():
    cursor = MagicMock()
    cursor.fetchone.return_value = (1,)
    cursor.description = [
        ("bgg_id",), ("name",), ("release_year",), ("min_players",),
        ("max_players",), ("playing_time_minutes",), ("min_age",),
        ("weight",), ("category",), ("price_amount",), ("publisher",),
        ("image_url",), ("quantity",),
    ]
    cursor.fetchall.return_value = [
        (7, "Game", 2020, 2, 4, 60, 10, 2.5, "Strategy", 12.5,
         "A Publisher", "/game.jpg", 0)
    ]

    with patch("saleor.graphql.api.connection", _connection(cursor)):
        result = schema.execute(
            """
            query ($publisher: String!, $search: String!, $category: String!) {
              products(publisher: $publisher, search: $search, categoryName: $category) {
                items { bggId }
                total
              }
            }
            """,
            variable_values={
                "publisher": "  A Publisher  ",
                "search": "game",
                "category": "Strategy",
            },
        )

    assert result.errors is None
    assert result.data["products"]["items"] == [{"bggId": 7}]
    sql, parameters = cursor.execute.call_args_list[0].args
    normalized_sql = " ".join(sql.split()).lower()
    assert "btrim(publisher) = %s" in normalized_sql
    assert "lower(name) like lower(%s)" in normalized_sql
    assert "regexp_split_to_table" in normalized_sql
    assert parameters == ["%game%", "%game%", "A Publisher", "Strategy"]


@pytest.mark.parametrize("publisher", ["", "  ", "x" * 251])
def test_products_rejects_blank_or_overlong_publisher_without_querying(publisher):
    cursor = MagicMock()
    with patch("saleor.graphql.api.connection", _connection(cursor)):
        result = schema.execute(
            "query ($publisher: String!) { products(publisher: $publisher) { total } }",
            variable_values={"publisher": publisher},
        )

    assert result.data["products"] is None
    assert "publisher" in str(result.errors[0]).lower()
    cursor.execute.assert_not_called()


def test_products_returns_empty_page_for_unknown_valid_publisher():
    cursor = MagicMock()
    cursor.fetchone.return_value = (0,)
    cursor.description = []
    cursor.fetchall.return_value = []

    with patch("saleor.graphql.api.connection", _connection(cursor)):
        result = schema.execute(
            '{ products(publisher: "Unknown Publisher") { items { bggId } total } }'
        )

    assert result.errors is None
    assert result.data["products"] == {"items": [], "total": 0}
    assert cursor.execute.call_args_list[0].args[1] == ["Unknown Publisher"]
