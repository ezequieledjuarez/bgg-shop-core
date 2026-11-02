"""Opt-in admin audit integration tests for a disposable local PostgreSQL DB."""

import json
import os
import re
import uuid
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
django.setup()

from django.db import connections

from saleor.graphql.api import schema


DSN = os.environ.get("ADMIN_AUDIT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="ADMIN_AUDIT_TEST_POSTGRES_DSN is not set"
)

DDL = """
CREATE TABLE product_boardgame (
    bgg_id integer PRIMARY KEY,
    name varchar(250) NOT NULL,
    price_amount numeric(12, 2) NOT NULL,
    quantity integer NOT NULL DEFAULT 0,
    publisher varchar(250) NOT NULL DEFAULT '',
    image_url varchar(500) NOT NULL DEFAULT '',
    image_filename varchar(250) NOT NULL DEFAULT '',
    release_year integer NOT NULL DEFAULT 0,
    min_players integer NOT NULL DEFAULT 1,
    max_players integer NOT NULL DEFAULT 1,
    playing_time_minutes integer NOT NULL DEFAULT 0,
    min_age integer NOT NULL DEFAULT 0,
    max_age integer NOT NULL DEFAULT 0,
    weight numeric(6, 2) NOT NULL DEFAULT 0,
    category text NOT NULL DEFAULT ''
);
CREATE TABLE promotion (
    id serial PRIMARY KEY,
    name varchar(250) NOT NULL,
    active boolean NOT NULL DEFAULT false,
    stackable boolean NOT NULL DEFAULT false,
    discount_pct numeric(5, 2) NOT NULL DEFAULT 0
        CHECK (discount_pct >= 0 AND discount_pct <= 50),
    starts_at timestamptz,
    ends_at timestamptz,
    product_bgg_id integer REFERENCES product_boardgame(bgg_id),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE coupon (
    id serial PRIMARY KEY,
    code varchar(64) NOT NULL UNIQUE,
    promotion_id integer REFERENCES promotion(id) ON DELETE SET NULL,
    remaining bigint NOT NULL CHECK (remaining >= 0),
    active boolean NOT NULL DEFAULT false,
    starts_at timestamptz,
    ends_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE audit_log (
    id serial PRIMARY KEY,
    actor varchar(150),
    action varchar(100) NOT NULL,
    target_table varchar(150),
    target_id varchar(64),
    diff jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE catalog_event_outbox (
    event_id uuid PRIMARY KEY,
    event_type varchar(100) NOT NULL,
    occurred_at timestamptz NOT NULL,
    aggregate_type varchar(50) NOT NULL,
    aggregate_id varchar(64) NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL,
    delivered_at timestamptz
);
CREATE TABLE stock_movement_status (
    movement_id uuid PRIMARY KEY,
    status varchar(20) NOT NULL
);
CREATE TABLE stock_movement_details (
    movement_id uuid PRIMARY KEY REFERENCES stock_movement_status(movement_id),
    product_id integer NOT NULL,
    direction varchar(3) NOT NULL,
    requested_quantity integer NOT NULL,
    actor_id integer NOT NULL,
    applied_quantity integer,
    quantity_before integer,
    quantity_after integer
);
"""

AUDIT_QUERY = """
query Audit($entityType: String, $entityId: String, $actorId: String, $action: String,
            $createdFrom: String, $createdTo: String, $cursor: String, $limit: Int) {
  adminAuditLogs(entityType: $entityType, entityId: $entityId, actorId: $actorId,
                 action: $action, createdFrom: $createdFrom, createdTo: $createdTo,
                 cursor: $cursor, limit: $limit) {
    records { id actorId action entityType entityId createdAt before after }
    nextCursor
  }
}
"""
CREATE_PRODUCT = """
mutation ($input: CreateProductInput!) {
  createProduct(input: $input) { product { bggId name } userErrors { code } }
}
"""
UPDATE_PRODUCT = """
mutation ($id: ID!, $input: UpdateProductInput!) {
  updateProduct(id: $id, input: $input) { product { bggId name } userErrors { code } }
}
"""
CREATE_PROMOTION = """
mutation ($input: CreatePromotionInput!) {
  createPromotion(input: $input) { promotion { id } userErrors { code } }
}
"""
UPDATE_PROMOTION = """
mutation ($id: ID!, $input: UpdatePromotionInput!) {
  updatePromotion(id: $id, input: $input) { promotion { id } userErrors { code } }
}
"""
CREATE_COUPON = """
mutation ($input: AdminCreateCouponInput!) {
  createAdminCoupon(input: $input) { coupon { id } userErrors { code } }
}
"""
ADJUST_STOCK = """
mutation ($movementId: ID!) {
  adjustProductStock(productId: "7", movementId: $movementId, direction: IN, quantity: 3) {
    status quantityBefore quantityAfter errors { code }
  }
}
"""


@pytest.fixture
def database():
    try:
        parsed = urlsplit(DSN)
        name = unquote(parsed.path.removeprefix("/"))
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        port = parsed.port
    except (ValueError, AttributeError):
        pytest.fail("Invalid isolated PostgreSQL DSN", pytrace=False)
    if (
        parsed.scheme not in ("postgres", "postgresql")
        or parsed.hostname != "127.0.0.1"
        or not port
        or not password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"admin_audit_test_[0-9a-f]{32}", name)
        or user != name
    ):
        pytest.fail(
            "DSN must use loopback, an explicit port, and a unique admin_audit_test_<uuid> role/database",
            pytrace=False,
        )

    original = connections.databases["default"]
    connections.close_all()
    if hasattr(connections._connections, "default"):
        del connections["default"]
    settings = {
        **original,
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": user,
        "PASSWORD": password,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "OPTIONS": {"connect_timeout": 3},
    }
    connections.databases["default"] = settings
    db = connections["default"]
    schema_name = "admin_audit_" + uuid.uuid4().hex
    created = False
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            assert cursor.fetchone() == (name, user)
            cursor.execute(f'CREATE SCHEMA "{schema_name}"')
            created = True
        db.close()
        settings["OPTIONS"] = {
            "connect_timeout": 3,
            "options": f"-c search_path={schema_name},pg_catalog -c statement_timeout=10000 -c lock_timeout=5000",
        }
        with db.cursor() as cursor:
            cursor.execute(DDL)
            cursor.execute(
                "INSERT INTO product_boardgame (bgg_id, name, price_amount, quantity) "
                "VALUES (7, 'Seed', 10, 5)"
            )
        yield db
    finally:
        db.close()
        if created:
            settings["OPTIONS"] = {"connect_timeout": 3}
            with db.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
        db.close()
        del connections["default"]
        connections.databases["default"] = original


def _context(**overrides):
    user = {"pk": 31, "is_active": True, "is_staff": True, "is_superuser": False}
    user.update(overrides)
    return SimpleNamespace(user=SimpleNamespace(**user))


def _execute(query, variables=None, context=None):
    with patch(
        "saleor.graphql.api._authenticated_user",
        side_effect=lambda info: info.context.user,
    ):
        return schema.execute(
            query, variable_values=variables, context_value=context or _context()
        )


def _audit(variables=None, context=None):
    result = _execute(AUDIT_QUERY, variables or {}, context)
    assert result.errors is None, result.errors
    page = result.data["adminAuditLogs"]
    for record in page["records"]:
        for key in ("before", "after"):
            record[key] = None if record[key] is None else json.loads(record[key])
    return page


def _fetch(database, query, params=()):
    with database.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def _insert_audit(database, rows):
    with database.cursor() as cursor:
        for actor, action, table, target, diff, created_at in rows:
            cursor.execute(
                "INSERT INTO audit_log (actor, action, target_table, target_id, diff, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [actor, action, table, target, json.dumps(diff), created_at],
            )


def _break_audit_inserts(database):
    with database.cursor() as cursor:
        cursor.execute(
            "CREATE FUNCTION fail_audit() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'private audit failure'; END $$"
        )
        cursor.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION fail_audit()"
        )


def test_product_create_and_update_write_atomic_audit_records(database):
    created = _execute(
        CREATE_PRODUCT, {"input": {"name": "Catan", "price": 25.5, "quantity": 4}}
    )
    assert created.errors is None
    assert created.data["createProduct"]["userErrors"] == []
    bgg_id = created.data["createProduct"]["product"]["bggId"]

    updated = _execute(
        UPDATE_PRODUCT, {"id": str(bgg_id), "input": {"name": "Catan 2", "price": 30}}
    )
    assert updated.errors is None
    assert updated.data["updateProduct"]["userErrors"] == []

    records = _audit({"entityType": "product_boardgame", "entityId": str(bgg_id)})["records"]
    assert [record["action"] for record in records] == ["product_updated", "product_created"]
    update, create = records
    assert create["actorId"] == "31" and create["before"] is None
    assert create["after"]["name"] == "Catan" and create["after"]["quantity"] == 4
    assert update["before"]["name"] == "Catan" and update["after"]["name"] == "Catan 2"
    assert float(update["before"]["price_amount"]) == 25.5
    assert float(update["after"]["price_amount"]) == 30
    assert update["after"]["quantity"] == 4


def test_product_audit_failure_rolls_back_create_and_update(database):
    _break_audit_inserts(database)
    created = _execute(
        CREATE_PRODUCT, {"input": {"name": "Ghost", "price": 5, "quantity": 1}}
    )
    assert created.data["createProduct"]["product"] is None
    assert created.data["createProduct"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private audit failure" not in str(created.data)
    assert _fetch(database, "SELECT count(*) FROM product_boardgame WHERE name = 'Ghost'") == [(0,)]

    updated = _execute(UPDATE_PRODUCT, {"id": "7", "input": {"name": "Changed"}})
    assert updated.data["updateProduct"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert _fetch(database, "SELECT name FROM product_boardgame WHERE bgg_id = 7") == [("Seed",)]


def test_promotion_create_and_update_write_atomic_audit_records(database):
    created = _execute(
        CREATE_PROMOTION, {"input": {"name": "Sale", "discountPercent": 10, "active": True}}
    )
    assert created.errors is None
    promotion_id = created.data["createPromotion"]["promotion"]["id"]
    updated = _execute(
        UPDATE_PROMOTION,
        {"id": str(promotion_id), "input": {"discountPercent": 20, "active": False}},
    )
    assert updated.errors is None
    assert updated.data["updatePromotion"]["userErrors"] == []

    records = _audit({"entityType": "promotion", "entityId": str(promotion_id)})["records"]
    assert [record["action"] for record in records] == ["promotion_updated", "promotion_created"]
    update, create = records
    assert create["before"] is None and create["after"]["name"] == "Sale"
    assert float(update["before"]["discount_pct"]) == 10 and update["before"]["active"] is True
    assert float(update["after"]["discount_pct"]) == 20 and update["after"]["active"] is False


def test_promotion_audit_failure_rolls_back_create_and_update(database):
    created = _execute(
        CREATE_PROMOTION, {"input": {"name": "Keep", "discountPercent": 10, "active": True}}
    )
    promotion_id = created.data["createPromotion"]["promotion"]["id"]
    _break_audit_inserts(database)
    failed_create = _execute(
        CREATE_PROMOTION, {"input": {"name": "Ghost", "discountPercent": 10, "active": True}}
    )
    assert failed_create.data["createPromotion"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert _fetch(database, "SELECT count(*) FROM promotion WHERE name = 'Ghost'") == [(0,)]
    failed_update = _execute(
        UPDATE_PROMOTION, {"id": str(promotion_id), "input": {"discountPercent": 40}}
    )
    assert failed_update.data["updatePromotion"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert _fetch(database, "SELECT discount_pct FROM promotion WHERE id = %s", [promotion_id])[0][0] == 10


def test_stock_and_coupon_changes_are_readable_in_audit_history(database):
    stock = _execute(ADJUST_STOCK, {"movementId": str(uuid.uuid4())})
    assert stock.errors is None
    assert stock.data["adjustProductStock"]["status"] == "APPLIED"
    coupon = _execute(
        CREATE_COUPON,
        {"input": {"code": "SAVE10", "discountPercent": 10, "remaining": 2, "active": True}},
    )
    assert coupon.data["createAdminCoupon"]["userErrors"] == []

    records = _audit()["records"]
    by_action = {record["action"]: record for record in records}
    assert by_action["stock_in"]["entityType"] == "product_boardgame"
    assert by_action["stock_in"]["before"] == {"quantity": 5}
    assert by_action["stock_in"]["after"] == {"quantity": 8}
    assert by_action["coupon_created"]["entityType"] == "coupon"
    assert by_action["coupon_created"]["after"]["code"] == "SAVE10"


def test_audit_cursor_index_migration_is_idempotent_and_preserves_cursor_order(database):
    from importlib import import_module

    migration = import_module(
        "saleor.graphql.migrations.0018_audit_log_cursor_index"
    ).Migration("0018_audit_log_cursor_index", "graphql")
    operation = migration.operations[0]

    with database.schema_editor() as editor:
        operation.database_forwards("graphql", editor, None, None)
        operation.database_forwards("graphql", editor, None, None)

    index_definition = _fetch(
        database,
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = current_schema() AND tablename = 'audit_log' "
        "AND indexname = 'audit_log_created_id_desc_idx'",
    )[0][0]
    assert index_definition.endswith("USING btree (created_at DESC, id DESC)")

    with database.schema_editor() as editor:
        operation.database_backwards("graphql", editor, None, None)
    assert _fetch(database, "SELECT to_regclass('audit_log') IS NOT NULL") == [(True,)]
    assert _fetch(
        database,
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = current_schema() AND indexname = 'audit_log_created_id_desc_idx'",
    ) == []

    with database.schema_editor() as editor:
        operation.database_forwards("graphql", editor, None, None)
    tie = "2026-01-02T00:00:00Z"
    _insert_audit(database, [
        ("1", "first", "coupon", "1", {}, tie),
        ("1", "middle", "coupon", "2", {}, tie),
        ("1", "last", "coupon", "3", {}, tie),
    ])
    first = _audit({"limit": 1})
    second = _audit({"limit": 1, "cursor": first["nextCursor"]})
    third = _audit({"limit": 1, "cursor": second["nextCursor"]})
    assert [page["records"][0]["action"] for page in (first, second, third)] == [
        "last",
        "middle",
        "first",
    ]


def test_audit_filters_cursor_order_and_ties_are_stable(database):
    tie = "2026-01-02T00:00:00Z"
    _insert_audit(database, [
        ("1", "a", "coupon", "1", {"previous": None, "new": {"n": 1}}, "2026-01-01T00:00:00Z"),
        ("2", "b", "promotion", "2", {"previous": None, "new": {"n": 2}}, tie),
        ("1", "c", "promotion", "3", {"previous": None, "new": {"n": 3}}, tie),
        ("1", "d", "promotion", "4", {"previous": None, "new": {"n": 4}}, tie),
        ("1", "e", "coupon", "5", {"previous": None, "new": {"n": 5}}, "2026-01-03T00:00:00Z"),
    ])
    first = _audit({"limit": 2})
    assert [record["action"] for record in first["records"]] == ["e", "d"]
    second = _audit({"limit": 2, "cursor": first["nextCursor"]})
    assert [record["action"] for record in second["records"]] == ["c", "b"]
    third = _audit({"limit": 2, "cursor": second["nextCursor"]})
    assert [record["action"] for record in third["records"]] == ["a"]
    assert third["nextCursor"] is None

    assert [r["action"] for r in _audit({"entityType": "promotion"})["records"]] == ["d", "c", "b"]
    assert [r["action"] for r in _audit({"entityId": "5"})["records"]] == ["e"]
    assert [r["action"] for r in _audit({"actorId": "2"})["records"]] == ["b"]
    assert [r["action"] for r in _audit({"action": "a"})["records"]] == ["a"]
    ranged = _audit({"createdFrom": "2026-01-02T00:00:00Z", "createdTo": "2026-01-02T23:59:59Z"})
    assert [r["action"] for r in ranged["records"]] == ["d", "c", "b"]
    assert _audit({"createdFrom": "2026-01-02T03:00:00+03:00", "createdTo": "2026-01-02T03:00:00+03:00"})["records"]


def test_audit_validation_rejects_bad_filters_limits_and_cursors(database):
    good = _audit({"limit": 1})
    assert good["records"] == [] and good["nextCursor"] is None
    cases = [
        ({"limit": 0}, "limit"), ({"limit": 101}, "limit"),
        ({"cursor": "not-a-signed-cursor"}, "cursor"), ({"cursor": "x" * 513}, "cursor"),
        ({"entityType": ""}, "entity_type"), ({"entityId": "x" * 65}, "entity_id"),
        ({"createdFrom": "2026-01-01"}, "created_from"),
        ({"createdFrom": "2026-01-02T00:00:00Z", "createdTo": "2026-01-01T00:00:00Z"}, "created_to"),
    ]
    for variables, field in cases:
        result = _execute(AUDIT_QUERY, variables)
        assert result.errors, variables
        assert result.errors[0].extensions == {"code": "INVALID", "field": field}


def test_audit_cursor_tampering_is_rejected(database):
    _insert_audit(database, [
        ("1", "x", "coupon", str(i), {"previous": None, "new": {}}, f"2026-01-0{i}T00:00:00Z")
        for i in range(1, 4)
    ])
    cursor = _audit({"limit": 1})["nextCursor"]
    tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    result = _execute(AUDIT_QUERY, {"cursor": tampered})
    assert result.errors and result.errors[0].extensions["code"] == "INVALID"


def test_audit_filters_are_parameterized(database):
    _insert_audit(database, [("1", "x", "coupon", "1", {"previous": None, "new": {}}, "2026-01-01T00:00:00Z")])
    payload = "coupon' OR '1'='1'; DROP TABLE audit_log; --"
    assert _audit({"entityType": payload})["records"] == []
    assert len(_audit()["records"]) == 1


def test_audit_redacts_secrets_recursively(database):
    _insert_audit(database, [(
        "1", "coupon_updated", "coupon", "1",
        {
            "previous": {"code": "A", "password": "hunter2", "nested": [{"Access_Token": "abc"}]},
            "new": {
                "payment": {"card_number": "4111111111111111"},
                "mp_payment_id": "pay-1",
                "note": "Bearer abc.def.ghi",
                "jwt": "aaaaaaaaaa.bbbbbbbbbb.cccccccccc",
                "safe": "kept",
            },
        },
        "2026-01-01T00:00:00Z",
    )])
    record = _audit()["records"][0]
    assert record["before"] == {"code": "A", "password": "[REDACTED]", "nested": [{"Access_Token": "[REDACTED]"}]}
    assert record["after"] == {
        "payment": "[REDACTED]", "mp_payment_id": "[REDACTED]", "note": "[REDACTED]",
        "jwt": "[REDACTED]", "safe": "kept",
    }
    assert "hunter2" not in json.dumps(record) and "4111" not in json.dumps(record)


@pytest.mark.parametrize(
    "overrides,code",
    [({"is_active": False}, "UNAUTHENTICATED"), ({"is_staff": False}, "FORBIDDEN")],
)
def test_audit_read_requires_active_staff_or_superuser(database, overrides, code):
    result = _execute(AUDIT_QUERY, {}, _context(**overrides))
    assert result.errors and result.errors[0].extensions == {"code": code}
    assert _audit({}, _context(is_staff=False, is_superuser=True))["records"] == []


def test_audit_database_failure_is_sanitized(database):
    with database.cursor() as cursor:
        cursor.execute("ALTER TABLE audit_log RENAME TO audit_log_hidden")
    result = _execute(AUDIT_QUERY, {})
    assert result.errors[0].extensions == {"code": "SERVER_ERROR"}
    assert "audit_log" not in result.errors[0].message
