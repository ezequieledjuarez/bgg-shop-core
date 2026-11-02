"""Opt-in coupon administration integration tests for a disposable local PostgreSQL DB."""

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
from django.utils import timezone

from saleor.graphql.api import schema
from saleor.graphql.orders import _promotion_rows


DSN = os.environ.get("COUPON_ADMIN_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="COUPON_ADMIN_TEST_POSTGRES_DSN is not set"
)

COUPON_DDL = """
CREATE TABLE product_boardgame (bgg_id integer PRIMARY KEY);
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
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (starts_at IS NULL OR ends_at IS NULL OR starts_at < ends_at)
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
"""

CREATE_COUPON = """
mutation CreateAdminCoupon($input: AdminCreateCouponInput!) {
  createAdminCoupon(input: $input) {
    coupon { id code discountPercent remaining active startsAt endsAt createdAt }
    userErrors { field code message }
  }
}
"""
UPDATE_COUPON = """
mutation UpdateAdminCoupon($id: Int!, $input: AdminUpdateCouponInput!) {
  updateAdminCoupon(id: $id, input: $input) {
    coupon { id code discountPercent remaining active startsAt endsAt createdAt }
    userErrors { field code message }
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
        or not re.fullmatch(r"coupon_admin_test_[0-9a-f]{32}", name)
        or user != name
    ):
        pytest.fail(
            "DSN must use loopback, an explicit port, and a unique coupon_admin_test_<uuid> role/database",
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
    schema_name = "coupon_admin_" + uuid.uuid4().hex
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
            cursor.execute(COUPON_DDL)
            cursor.execute("INSERT INTO product_boardgame VALUES (7)")
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


def _admin_context():
    return SimpleNamespace(
        user=SimpleNamespace(
            pk=31, is_active=True, is_staff=True, is_superuser=False
        )
    )


def _execute(query, variables=None, context=None):
    with patch(
        "saleor.graphql.api._authenticated_user",
        side_effect=lambda info: info.context.user,
    ):
        return schema.execute(
            query,
            variable_values=variables,
            context_value=context or _admin_context(),
        )


def _fetch(database, query, params=()):
    with database.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def _coupon_input(code="SAVE10"):
    return {
        "code": code,
        "discountPercent": 12.5,
        "remaining": 4,
        "active": True,
        "startsAt": "2026-11-01T00:00:00Z",
        "endsAt": "2026-12-01T00:00:00Z",
    }


def test_admin_coupon_create_list_and_partial_update_are_atomic(database):
    created = _execute(CREATE_COUPON, {"input": _coupon_input()})
    assert created.errors is None
    result = created.data["createAdminCoupon"]
    assert result["userErrors"] == []
    coupon = result["coupon"]
    assert coupon["id"] == 1
    assert coupon["code"] == "SAVE10"
    assert coupon["discountPercent"] == 12.5
    assert coupon["remaining"] == 4
    assert coupon["active"] is True
    assert coupon["startsAt"].startswith("2026-11-01T00:00:00")
    assert coupon["endsAt"].startswith("2026-12-01T00:00:00")

    promotion = _fetch(
        database,
        "SELECT product_bgg_id, discount_pct, active, starts_at, ends_at "
        "FROM promotion WHERE id = 1",
    )[0]
    assert promotion == (None, 12.5, True, None, None)
    audit = _fetch(
        database,
        "SELECT actor, action, target_table, target_id, diff "
        "FROM audit_log WHERE target_id = '1'",
    )[0]
    assert audit[:4] == ("31", "coupon_created", "coupon", "1")
    audit_values = audit[4] if isinstance(audit[4], dict) else json.loads(audit[4])
    assert audit_values == {
        "previous": None,
        "new": {
            "code": "SAVE10",
            "discount_percent": 12.5,
            "remaining": 4,
            "active": True,
            "starts_at": "2026-11-01T00:00:00+00:00",
            "ends_at": "2026-12-01T00:00:00+00:00",
        },
    }

    listed = _execute(
        "{ adminCoupons { id code discountPercent remaining active startsAt endsAt } }"
    )
    assert listed.errors is None
    assert listed.data["adminCoupons"][0]["discountPercent"] == 12.5

    updated = _execute(
        UPDATE_COUPON,
        {"id": coupon["id"], "input": {"discountPercent": 0, "active": False}},
    )
    assert updated.errors is None
    update_result = updated.data["updateAdminCoupon"]
    assert update_result["userErrors"] == []
    assert update_result["coupon"]["discountPercent"] == 0
    assert update_result["coupon"]["active"] is False
    assert update_result["coupon"]["remaining"] == 4
    assert _fetch(database, "SELECT discount_pct FROM promotion WHERE id = 1") == [(0,)]
    audit = _fetch(
        database,
        "SELECT actor, action, target_table, target_id, diff "
        "FROM audit_log WHERE action = 'coupon_updated'",
    )[0]
    assert audit[:4] == ("31", "coupon_updated", "coupon", "1")
    audit_values = audit[4] if isinstance(audit[4], dict) else json.loads(audit[4])
    assert audit_values["previous"]["discount_percent"] == 12.5
    assert audit_values["new"]["discount_percent"] == 0
    assert audit_values["new"]["active"] is False


def test_admin_coupon_exact_case_dates_immutability_and_product_discount_isolation(database):
    created = _execute(CREATE_COUPON, {"input": _coupon_input()})
    assert created.errors is None
    assert created.data["createAdminCoupon"]["coupon"]["code"] == "SAVE10"

    duplicate = _execute(CREATE_COUPON, {"input": _coupon_input()})
    assert duplicate.errors is None
    assert duplicate.data["createAdminCoupon"]["userErrors"][0]["code"] == "CODE_EXISTS"
    lowercase = _execute(CREATE_COUPON, {"input": _coupon_input("save10")})
    assert lowercase.errors is None
    assert lowercase.data["createAdminCoupon"]["userErrors"] == []

    invalid_dates = _execute(
        CREATE_COUPON,
        {
            "input": {
                **_coupon_input("BAD-DATES"),
                "startsAt": "2026-12-01T00:00:00Z",
                "endsAt": "2026-12-01T00:00:00Z",
            }
        },
    )
    assert invalid_dates.errors is None
    assert invalid_dates.data["createAdminCoupon"]["userErrors"][0]["code"] == "INVALID"

    immutable = _execute(
        "mutation ($id: Int!, $input: AdminUpdateCouponInput!) { "
        "updateAdminCoupon(id: $id, input: $input) { coupon { id } } }",
        {"id": 1, "input": {"code": "OTHER"}},
    )
    assert immutable.errors

    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO promotion (name, active, discount_pct, product_bgg_id) "
            "VALUES ('Product promotion', TRUE, 20, 7)"
        )
        discounts = _promotion_rows(cursor, [7], timezone.now())
    assert discounts == [(7, 20)]


def test_admin_coupon_auth_and_audit_failure_roll_back_both_records(database):
    buyer_context = SimpleNamespace(
        user=SimpleNamespace(
            pk=8, is_active=True, is_staff=False, is_superuser=False
        )
    )
    denied = _execute(CREATE_COUPON, {"input": _coupon_input()}, buyer_context)
    assert denied.errors is None
    assert denied.data["createAdminCoupon"]["userErrors"][0]["code"] == "FORBIDDEN"

    with database.cursor() as cursor:
        cursor.execute(
            """
            CREATE FUNCTION reject_coupon_audit() RETURNS trigger AS $$
            BEGIN
                IF NEW.action = 'coupon_created' THEN
                    RAISE EXCEPTION 'test audit rejection';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        cursor.execute(
            "CREATE TRIGGER reject_coupon_audit BEFORE INSERT ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION reject_coupon_audit()"
        )

    failed = _execute(CREATE_COUPON, {"input": _coupon_input("ROLLBACK")})
    assert failed.errors is None
    assert failed.data["createAdminCoupon"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert _fetch(database, "SELECT count(*) FROM coupon")[0][0] == 0
    assert _fetch(database, "SELECT count(*) FROM promotion")[0][0] == 0
    assert _fetch(database, "SELECT count(*) FROM audit_log")[0][0] == 0


def test_admin_coupon_update_rolls_back_when_audit_insert_fails(database):
    created = _execute(CREATE_COUPON, {"input": _coupon_input()})
    assert created.errors is None
    assert created.data["createAdminCoupon"]["userErrors"] == []

    with database.cursor() as cursor:
        cursor.execute(
            """
            CREATE FUNCTION reject_coupon_update_audit() RETURNS trigger AS $$
            BEGIN
                IF NEW.action = 'coupon_updated' THEN
                    RAISE EXCEPTION 'test audit rejection';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        cursor.execute(
            "CREATE TRIGGER reject_coupon_update_audit BEFORE INSERT ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION reject_coupon_update_audit()"
        )

    failed = _execute(
        UPDATE_COUPON,
        {"id": 1, "input": {"discountPercent": 40, "active": False}},
    )
    assert failed.errors is None
    assert failed.data["updateAdminCoupon"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert _fetch(database, "SELECT discount_pct FROM promotion WHERE id = 1") == [
        (12.5,)
    ]
    assert _fetch(database, "SELECT active FROM coupon WHERE id = 1") == [(True,)]
    assert _fetch(database, "SELECT count(*) FROM audit_log")[0][0] == 1
