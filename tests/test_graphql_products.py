import os
import sqlite3
import sys
from importlib import import_module
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import django
import pytest
from graphql import build_schema
from graphql.utilities import introspection_from_schema

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from saleor.graphql.api import schema


def _normalize_schema(value):
    if isinstance(value, dict):
        return {key: _normalize_schema(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        normalized = [_normalize_schema(item) for item in value]
        if all(isinstance(item, dict) and "name" in item for item in normalized):
            return sorted(normalized, key=lambda item: item["name"])
        return normalized
    return value


def test_generated_schema_matches_reference_sdl():
    schema_path = Path(__file__).resolve().parents[1] / "specs" / "schema.graphql"
    reference_schema = build_schema(schema_path.read_text())

    assert _normalize_schema(introspection_from_schema(schema.graphql_schema)) == (
        _normalize_schema(introspection_from_schema(reference_schema))
    )


def test_coupon_validity_and_promotion_product_fields_are_exposed():
    coupon_fields = schema.graphql_schema.get_type("Coupon").fields
    promotion_fields = schema.graphql_schema.get_type("Promotion").fields
    create_fields = schema.graphql_schema.get_type("CreatePromotionInput").fields
    update_fields = schema.graphql_schema.get_type("UpdatePromotionInput").fields

    assert {"active", "startsAt", "endsAt"} <= set(coupon_fields)
    assert "productBggId" in promotion_fields
    assert "productBggId" in create_fields
    assert "productBggId" in update_fields


def test_coupon_query_returns_validity_and_product_linkage():
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    starts_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    ends_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cursor = MagicMock()
    cursor.fetchone.return_value = (
        7,
        "SAVE",
        3,
        2,
        created_at,
        "Game sale",
        False,
        15,
        starts_at,
        ends_at,
        created_at,
        True,
        123,
        True,
        starts_at,
        ends_at,
    )
    cursor.description = [
        (name,)
        for name in (
            "id", "code", "promotion_id", "remaining", "coupon_created_at",
            "name", "stackable", "discount_pct", "promotion_starts_at",
            "promotion_ends_at", "promotion_created_at", "promotion_active",
            "product_bgg_id", "coupon_active", "coupon_starts_at",
            "coupon_ends_at",
        )
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    query = (
        '{ couponByCode(code: "SAVE") { active startsAt endsAt '
        'promotion { productBggId } } }'
    )

    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute(query)

    assert result.errors is None
    coupon = result.data["couponByCode"]
    assert coupon["active"] is True
    assert coupon["startsAt"] == starts_at.isoformat()
    assert coupon["endsAt"] == ends_at.isoformat()
    assert coupon["promotion"]["productBggId"] == 123


@pytest.mark.parametrize(
    "promotion_input",
    [
        {"name": "Sale", "discountPercent": 10, "active": True},
        {"name": "Sale", "discountPercent": 10, "active": True, "productBggId": None},
    ],
)
def test_promotion_creation_accepts_omitted_or_null_product_as_global(promotion_input):
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    promotion_row = (9, "Sale", False, 10, None, None, created_at, True, None)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = promotion_row
    mutation = (
        "mutation ($input: CreatePromotionInput!) { "
        "createPromotion(input: $input) { promotion { productBggId } userErrors { code field } } }"
    )

    with patch("saleor.graphql.api.connection", connection), patch(
        "saleor.graphql.api._authenticated_user", return_value=user
    ), patch("saleor.graphql.api.transaction"), patch(
        "saleor.graphql.api.record_catalog_event"
    ):
        result = schema.execute(mutation, variable_values={"input": promotion_input})

    assert result.errors is None
    assert result.data["createPromotion"]["promotion"]["productBggId"] is None
    assert result.data["createPromotion"]["userErrors"] == []
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO promotion" in statement for statement in statements)
    assert not any("SELECT bgg_id FROM product_boardgame" in statement for statement in statements)


def test_promotion_creation_and_update_persist_product_linkage():
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    promotion_row = (9, "Sale", False, 10, None, None, created_at, True, 123)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [(123,), promotion_row, (123,), promotion_row]
    create_mutation = (
        "mutation ($input: CreatePromotionInput!) { createPromotion(input: $input) "
        "{ promotion { productBggId } userErrors { code } } }"
    )
    update_mutation = (
        'mutation ($input: UpdatePromotionInput!) { updatePromotion(id: "9", input: $input) '
        "{ promotion { productBggId } userErrors { code } } }"
    )
    with patch("saleor.graphql.api.connection", connection), patch(
        "saleor.graphql.api._authenticated_user", return_value=user
    ), patch("saleor.graphql.api.transaction"), patch(
        "saleor.graphql.api.record_catalog_event"
    ):
        created = schema.execute(
            create_mutation,
            variable_values={
                "input": {
                    "name": "Sale",
                    "discountPercent": 10,
                    "active": True,
                    "productBggId": 123,
                }
            },
        )
        updated = schema.execute(
            update_mutation,
            variable_values={"input": {"productBggId": 123}},
        )

    assert created.errors is None
    assert created.data["createPromotion"]["promotion"]["productBggId"] == 123
    assert created.data["createPromotion"]["userErrors"] == []
    assert updated.errors is None
    assert updated.data["updatePromotion"]["promotion"]["productBggId"] == 123
    assert updated.data["updatePromotion"]["userErrors"] == []
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO promotion" in statement and "product_bgg_id" in statement for statement in statements)
    assert any("product_bgg_id = %s" in statement for statement in statements)


def test_product_promotions_exclude_productless_coupon_promotions():
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (9, "Game sale", False, 10, None, None, datetime(2026, 1, 1, tzinfo=timezone.utc), True, 123),
        (10, "Global sale", False, 20, None, None, datetime(2026, 1, 1, tzinfo=timezone.utc), True, None),
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute("{ promotions { id productBggId } }")

    assert result.errors is None
    assert result.data["promotions"] == [
        {"id": "9", "productBggId": 123},
        {"id": "10", "productBggId": None},
    ]
    query = cursor.execute.call_args.args[0]
    assert "NOT EXISTS" in query
    assert "FROM coupon" in query


def test_promotion_update_can_clear_product_link():
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    promotion_row = (9, "Sale", False, 10, None, None, created_at, True, None)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = promotion_row
    mutation = (
        "mutation ($input: UpdatePromotionInput!) { "
        'updatePromotion(id: "9", input: $input) { promotion { productBggId } userErrors { code } } }'
    )

    with patch("saleor.graphql.api.connection", connection), patch(
        "saleor.graphql.api._authenticated_user", return_value=user
    ), patch("saleor.graphql.api.transaction"), patch(
        "saleor.graphql.api.record_catalog_event"
    ):
        result = schema.execute(mutation, variable_values={"input": {"productBggId": None}})

    assert result.errors is None
    assert result.data["updatePromotion"]["promotion"]["productBggId"] is None
    assert result.data["updatePromotion"]["userErrors"] == []
    update_call = next(
        call for call in cursor.execute.call_args_list
        if "UPDATE promotion AS p" in call.args[0]
    )
    assert "product_bgg_id = %s" in update_call.args[0]
    assert update_call.args[1][0] is None


def test_stock_movement_status_contract():
    reference = build_schema((Path(__file__).resolve().parents[1] / "specs" / "schema.graphql").read_text())
    assert "adminStockMovementStatus" in reference.query_type.fields
    assert set(reference.get_type("StockMovementStatus").values) == {"IN_PROGRESS", "APPLIED", "REJECTED"}


def test_stock_movement_status_migration_enforces_unique_id_and_status():
    migration = import_module("saleor.graphql.migrations.0012_stock_movement_status").Migration
    database = sqlite3.connect(":memory:")
    try:
        database.executescript(migration.operations[0].sql)
        movement_id = "12345678-1234-4234-8234-123456789abc"
        database.execute("INSERT INTO stock_movement_status VALUES (?, ?)", (movement_id, "APPLIED"))
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO stock_movement_status VALUES (?, ?)", (movement_id, "REJECTED"))
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO stock_movement_status VALUES (?, ?)", ("another-id", "INVALID"))
    finally:
        database.close()


def test_stock_movement_details_migration_enforces_identity():
    status_migration = import_module("saleor.graphql.migrations.0012_stock_movement_status").Migration
    details_migration = import_module("saleor.graphql.migrations.0013_stock_movement_details").Migration
    database = sqlite3.connect(":memory:")
    try:
        database.execute("PRAGMA foreign_keys = ON")
        database.executescript(status_migration.operations[0].sql)
        database.executescript(details_migration.operations[0].sql)
        movement_id = "12345678-1234-4234-8234-123456789abc"
        database.execute("INSERT INTO stock_movement_status VALUES (?, ?)", (movement_id, "IN_PROGRESS"))
        database.execute("INSERT INTO stock_movement_details (movement_id, product_id, direction, requested_quantity, actor_id) VALUES (?, ?, ?, ?, ?)", (movement_id, 123, "IN", 20, 1))
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO stock_movement_details (movement_id, product_id, direction, requested_quantity, actor_id) VALUES (?, ?, ?, ?, ?)", (movement_id, 123, "IN", 20, 1))
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO stock_movement_details (movement_id, product_id, direction, requested_quantity, actor_id) VALUES (?, ?, ?, ?, ?)", ("different", 123, "INVALID", 0, 1))
    finally:
        database.close()


@pytest.fixture
def movement_status_database():
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE stock_movement_status (movement_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    cursor = database.cursor()
    mocked_cursor = MagicMock()
    mocked_cursor.execute.side_effect = lambda sql, parameters: cursor.execute(sql.replace("%s", "?"), parameters)
    mocked_cursor.fetchone.side_effect = cursor.fetchone
    mocked_connection = MagicMock()
    mocked_connection.cursor.return_value.__enter__.return_value = mocked_cursor
    yield database, mocked_connection, mocked_cursor
    database.close()


def _stock_movement_status(connection, user, movement_id):
    query = "query ($id: ID!) { adminStockMovementStatus(movementId: $id) { movementId status userErrors { code message field } } }"
    with patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api._authenticated_user", return_value=user):
        return schema.execute(query, variable_values={"id": movement_id})


@pytest.mark.parametrize("status", ["IN_PROGRESS", "APPLIED", "REJECTED"])
def test_stock_movement_status_reads_record(movement_status_database, status):
    database, connection, _ = movement_status_database
    movement_id = "12345678-1234-4234-8234-123456789abc"
    database.execute("INSERT INTO stock_movement_status VALUES (?, ?)", (movement_id, status))
    result = _stock_movement_status(connection, SimpleNamespace(is_active=True, is_staff=True), movement_id)
    assert result.errors is None
    assert result.data["adminStockMovementStatus"] == {"movementId": movement_id, "status": status, "userErrors": []}


@pytest.mark.parametrize("user,code", [
    (None, "UNAUTHENTICATED"),
    (SimpleNamespace(is_active=False, is_staff=True), "UNAUTHENTICATED"),
    (SimpleNamespace(is_active=True, is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_stock_movement_status_denies_unauthorized_before_database(movement_status_database, user, code):
    _, connection, cursor = movement_status_database
    result = _stock_movement_status(connection, user, "12345678-1234-4234-8234-123456789abc")
    assert result.errors is None
    assert result.data["adminStockMovementStatus"]["userErrors"][0]["code"] == code
    cursor.execute.assert_not_called()


@pytest.mark.parametrize("movement_id,code", [("invalid", "INVALID"), ("12345678-1234-4234-8234-123456789abc", "NOT_FOUND")])
def test_stock_movement_status_reports_invalid_or_unknown_id(movement_status_database, movement_id, code):
    _, connection, _ = movement_status_database
    result = _stock_movement_status(connection, SimpleNamespace(is_active=True, is_superuser=True), movement_id)
    assert result.errors is None
    payload = result.data["adminStockMovementStatus"]
    assert payload["movementId"] == movement_id
    assert payload["status"] is None
    assert payload["userErrors"][0]["code"] == code


def test_stock_movement_status_sanitizes_database_failure():
    connection = MagicMock()
    connection.cursor.side_effect = RuntimeError("private database details")
    result = _stock_movement_status(connection, SimpleNamespace(is_active=True, is_staff=True), "12345678-1234-4234-8234-123456789abc")
    assert result.errors is None
    assert result.data["adminStockMovementStatus"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database details" not in str(result.data)


def test_stock_movement_status_rejects_invalid_stored_status():
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = ("private invalid status",)
    result = _stock_movement_status(connection, SimpleNamespace(is_active=True, is_staff=True), "12345678-1234-4234-8234-123456789abc")
    assert result.errors is None
    assert result.data["adminStockMovementStatus"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private invalid status" not in str(result.data)


def test_products_graphql_default_matches_contract():
    query = schema.graphql_schema.query_type
    assert query.fields["products"].args["pageSize"].default_value == 50


@pytest.mark.parametrize("quantity", [0, 5, None])
def test_product_update_rejects_quantity_before_database(quantity):
    user = SimpleNamespace(is_active=True, is_staff=True, is_superuser=False)
    connection = MagicMock()
    mutation = "mutation ($input: UpdateProductInput!) { updateProduct(id: \"123\", input: $input) { product { bggId } userErrors { code } } }"
    with patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(mutation, variable_values={"input": {"name": "Game", "quantity": quantity}})
    assert result.errors
    assert "quantity" in str(result.errors[0])
    connection.cursor.assert_not_called()


def test_product_update_other_fields_preserve_stock():
    user = SimpleNamespace(is_active=True, is_staff=True, is_superuser=False)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (123, "Updated", 10, 7)
    mutation = "mutation ($input: UpdateProductInput!) { updateProduct(id: \"123\", input: $input) { product { name quantity } userErrors { code } } }"
    with patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api._authenticated_user", return_value=user), patch("saleor.graphql.api.transaction"):
        result = schema.execute(mutation, variable_values={"input": {"name": "Updated"}})
    assert result.errors is None
    assert result.data["updateProduct"] == {"product": {"name": "Updated", "quantity": 7}, "userErrors": []}
    assert "quantity =" not in cursor.execute.call_args_list[0].args[0]
    assert "catalog_event_outbox" in cursor.execute.call_args.args[0]


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("discount,accepted", [(50, True), (50.01, False), (0, False)])
def test_promotion_discount_limit_is_enforced_before_database(operation, discount, accepted):
    user = SimpleNamespace(is_active=True, is_staff=True, is_superuser=False)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    promotion_row = (
        1, "Sale", False, 50, None, None, datetime(2026, 1, 1, tzinfo=timezone.utc),
        True, 123,
    )
    if operation == "create":
        mutation = "mutation ($input: CreatePromotionInput!) { createProductPromotion: createPromotion(input: $input) { promotion { id } userErrors { field code } } }"
        variables = {
            "input": {
                "name": "Sale",
                "discountPercent": discount,
                "active": True,
                "productBggId": 123,
            }
        }
        cursor.fetchone.side_effect = [(123,), promotion_row]
    else:
        mutation = "mutation ($input: UpdatePromotionInput!) { createProductPromotion: updatePromotion(id: \"1\", input: $input) { promotion { id } userErrors { field code } } }"
        variables = {"input": {"discountPercent": discount}}
        cursor.fetchone.return_value = promotion_row
    with patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api._authenticated_user", return_value=user), patch("saleor.graphql.api.transaction"):
        result = schema.execute(mutation, variable_values=variables)
    assert result.errors is None
    payload = result.data["createProductPromotion"]
    if accepted:
        assert payload["userErrors"] == []
        connection.cursor.assert_called_once()
    else:
        assert payload["promotion"] is None
        assert payload["userErrors"][0]["field"] == ["discountPercent"]
        connection.cursor.assert_not_called()


@pytest.fixture
def stock_database():
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE product_boardgame (bgg_id INTEGER PRIMARY KEY, quantity INTEGER NOT NULL)")
    database.execute("INSERT INTO product_boardgame VALUES (123, 10)")
    cursor = database.cursor()
    mocked_cursor = MagicMock()
    mocked_cursor.execute.side_effect = lambda sql, parameters: cursor.execute(sql.replace("%s", "?"), parameters)
    mocked_cursor.fetchone.side_effect = cursor.fetchone
    mocked_connection = MagicMock()
    mocked_connection.cursor.return_value.__enter__.return_value = mocked_cursor
    yield database, mocked_connection, mocked_cursor
    database.close()


def _add_product_stock(connection, user, product_id="123", incoming=20):
    with patch("saleor.graphql.api.connection", connection), patch(
        "saleor.graphql.api._authenticated_user", return_value=user
    ):
        return schema.execute(
            "mutation ($id: ID!, $amount: Int!) { addProductStock(productId: $id, newStock: $amount) { success errors { code message field } } }",
            variable_values={"id": product_id, "amount": incoming},
        )


def test_legacy_add_stock_cannot_bypass_movement_id(stock_database):
    _, connection, cursor = stock_database
    result = _add_product_stock(connection, SimpleNamespace(is_active=True, is_staff=True), incoming=1)
    assert result.errors is None
    assert result.data["addProductStock"]["errors"][0]["code"] == "MIGRATION_REQUIRED"
    cursor.execute.assert_not_called()


@pytest.mark.parametrize("user,code", [
    (None, "UNAUTHENTICATED"),
    (SimpleNamespace(is_active=False, is_staff=True), "UNAUTHENTICATED"),
    (SimpleNamespace(is_active=True, is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_adjust_stock_rejects_unauthorized_before_database(user, code):
    connection = MagicMock()
    query = 'mutation { adjustProductStock(productId: "123", movementId: "12345678-1234-4234-8234-123456789abc", direction: IN, quantity: 1) { errors { code } } }'
    with patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(query)
    assert result.errors is None
    assert result.data["adjustProductStock"]["errors"][0]["code"] == code
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("product_id,movement_id,quantity,field", [
    ("0", "12345678-1234-4234-8234-123456789abc", 1, "productId"),
    ("2147483648", "12345678-1234-4234-8234-123456789abc", 1, "productId"),
    ("123", "invalid", 1, "movementId"),
    ("123", "12345678-1234-4234-8234-123456789abc", 0, "quantity"),
    ("123", "12345678-1234-4234-8234-123456789abc", -1, "quantity"),
])
def test_adjust_stock_rejects_invalid_input_before_database(product_id, movement_id, quantity, field):
    connection = MagicMock()
    query = 'mutation ($product: ID!, $movement: ID!, $quantity: Int!) { adjustProductStock(productId: $product, movementId: $movement, direction: IN, quantity: $quantity) { errors { code field } } }'
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(query, variable_values={"product": product_id, "movement": movement_id, "quantity": quantity})
    assert result.errors is None
    assert result.data["adjustProductStock"]["errors"][0] == {"code": "INVALID", "field": [field]}
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/admin/products/124/stock"),
    ("GET", "/api/v1/admin/products/123/stock"),
    ("POST", "/api/v1/admin/products/123/stock/extra"),
])
def test_adjust_stock_rejects_mismatched_delegation_before_database(method, path):
    query = 'mutation { adjustProductStock(productId: "123", movementId: "12345678-1234-4234-8234-123456789abc", direction: IN, quantity: 1) { errors { code } } }'
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    request = SimpleNamespace(opkey_delegated=True, headers={"X-Op-Method": method, "X-Op-Path": path})
    connection = MagicMock()
    with patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(query, context_value=request)
    assert result.errors is None
    assert result.data["adjustProductStock"]["errors"][0]["code"] == "FORBIDDEN"
    connection.cursor.assert_not_called()


def test_previous_replacement_stock_mutation_is_not_exposed():
    assert "adjustVariantStock" not in schema.graphql_schema.mutation_type.fields


def test_create_product_sanitizes_database_exception_logs(capsys, caplog):
    from types import SimpleNamespace
    from saleor.graphql.api import CreateProductMutation

    secret = "synthetic-private-database-detail"
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError(secret)
    admin = SimpleNamespace(is_staff=True, is_superuser=False)
    product = SimpleNamespace(name="Game", price=1, quantity=1, bggId=123)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = CreateProductMutation.mutate(None, None, product)
    assert result.userErrors[0].code == "SERVER_ERROR"
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err + caplog.text + str(result.userErrors)


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
        ("quantity",),
    ]
    cursor.fetchall.return_value = [
        (1, "Game", 2020, 2, 4, 60, 10, 2.5, "Strategy, Family", 12.5, "Pub", "/game.jpg", 10)
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute(
            """
            { products(page: 2, pageSize: 1) {
              items { bggId name category price imageUrl quantity }
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
            "quantity": 10,
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
        ("quantity",),
    ]
    cursor.fetchall.return_value = [
        (7, "Game", 2020, 2, 4, 60, 10, 2.5, "Juegos de Estrategia", 12.5, "Pub", "/game.jpg", 10)
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


def test_product_categories_are_distinct_trimmed_and_sorted():
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("Juegos Party,Juegos de Estrategia",),
        (" Juegos Party , Juegos Familiares ",),
        (None,),
        ("",),
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute("{ productCategories }")

    assert result.errors is None
    assert result.data["productCategories"] == [
        "Juegos Familiares", "Juegos Party", "Juegos de Estrategia"
    ]


def test_products_query_filters_exact_category_name():
    cursor = MagicMock()
    cursor.fetchone.return_value = (0,)
    cursor.description = []
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute('{ products(categoryName: "Juegos Party") { total } }')

    assert result.errors is None
    assert result.data["products"]["total"] == 0
    sql, parameters = cursor.execute.call_args_list[0].args
    assert "regexp_split_to_table" in sql
    assert parameters == ["Juegos Party"]


def test_products_query_rejects_inverted_price_range():
    result = schema.execute(
        "{ products(priceMin: 80, priceMax: 10) { total } }"
    )

    assert result.data["products"] is None
    assert "priceMin" in str(result.errors[0])


def test_product_by_slug_exposes_public_bgg_id():
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    product = {
        "bgg_id": 13,
        "name": "Catan",
        "release_year": 1995,
        "min_players": 3,
        "max_players": 4,
        "playing_time_minutes": 90,
        "min_age": 10,
        "weight": 2.3,
        "category": "Strategy",
        "price_amount": 49.99,
        "publisher": "Kosmos",
        "image_url": "/catan.jpg",
        "quantity": 8,
    }

    with patch("saleor.graphql.api.connection", connection), patch(
        "saleor.graphql.api._product_by_slug", return_value=product
    ):
        result = schema.execute(
            '{ productBySlug(slug: "catan") { bggId } }'
        )

    assert result.errors is None
    assert result.data["productBySlug"]["bggId"] == 13


@pytest.mark.parametrize("found", [True, False])
def test_product_by_bgg_id_queries_exact_product_and_approved_reviews(found):
    cursor = MagicMock()
    columns = ("bgg_id", "name", "release_year", "min_players", "max_players", "playing_time_minutes", "min_age", "weight", "category", "price_amount", "publisher", "image_url", "quantity")
    cursor.description = [(name,) for name in columns]
    cursor.fetchone.return_value = (13, "Catan", 1995, 3, 4, 90, 10, 2.3, "Strategy", 49.99, "Kosmos", "/catan.jpg", 8) if found else None
    cursor.fetchall.return_value = [(5, "Excellent", datetime(2026, 1, 2, tzinfo=timezone.utc))]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    with patch("saleor.graphql.api.connection", connection):
        result = schema.execute('{ productByBggId(bggId: 13) { bggId name approvedReviews { rating } } }')
    assert result.errors is None
    assert result.data["productByBggId"] == ({"bggId": 13, "name": "Catan", "approvedReviews": [{"rating": 5}]} if found else None)
    query, parameters = cursor.execute.call_args_list[0].args
    assert "WHERE bgg_id = %s" in query
    assert parameters == [13]
    if found:
        assert "status = 'ACCEPTED'" in cursor.execute.call_args_list[1].args[0]


def test_product_detail_aggregates_only_approved_reviews():
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (5, "Excellent", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        (4, "Good", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    product = {
        "bgg_id": 13, "name": "Catan", "release_year": 1995, "min_players": 3,
        "max_players": 4, "playing_time_minutes": 90, "min_age": 10, "weight": 2.3,
        "category": "Strategy", "price_amount": 49.99, "publisher": "Kosmos",
        "image_url": "/catan.jpg", "quantity": 8,
    }

    with patch("saleor.graphql.api.connection", connection), patch(
        "saleor.graphql.api._product_by_slug", return_value=product
    ):
        result = schema.execute('''
            { productBySlug(slug: "catan") {
                reviewSummary { approvedReviewCount averageRating }
                approvedReviews { rating body }
            } }
        ''')

    assert result.errors is None
    detail = result.data["productBySlug"]
    assert detail["reviewSummary"] == {"approvedReviewCount": 2, "averageRating": 4.5}
    assert detail["approvedReviews"] == [
        {"rating": 5, "body": "Excellent"}, {"rating": 4, "body": "Good"}
    ]
    sql, parameters = cursor.execute.call_args.args
    assert "status = 'ACCEPTED'" in sql
    assert parameters == [13]
