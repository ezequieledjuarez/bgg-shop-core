"""Opt-in sales metrics integration tests for a disposable local PostgreSQL DB."""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
django.setup()

from django.db import connections

from saleor.graphql.api import schema


DSN = os.environ.get("SALES_METRICS_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="SALES_METRICS_TEST_POSTGRES_DSN is not set"
)

DDL = """
CREATE TABLE product_boardgame (
    bgg_id integer PRIMARY KEY,
    name varchar(250) NOT NULL,
    category text NOT NULL DEFAULT ''
);
CREATE TABLE purchase_order (
    id uuid PRIMARY KEY,
    status varchar(32) NOT NULL,
    total numeric(12, 2) NOT NULL,
    payment_status varchar(32),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at timestamptz
);
CREATE TABLE purchase_order_line (
    order_id uuid NOT NULL REFERENCES purchase_order(id),
    bgg_id integer NOT NULL REFERENCES product_boardgame(bgg_id),
    name varchar(250) NOT NULL,
    unit_price numeric(12, 2) NOT NULL,
    quantity integer NOT NULL,
    subtotal numeric(12, 2) NOT NULL,
    PRIMARY KEY (order_id, bgg_id)
);
"""

METRICS_QUERY = """
query SalesMetrics($from: String, $to: String, $categories: [String!], $productIds: [Int!]) {
  adminSalesMetrics(from: $from, to: $to, categories: $categories, productIds: $productIds) {
    revenueTotal
    ordersCount
    topProducts { bggId name units revenue }
    ordersByDispatchStatus { status count }
    ordersByPaymentStatus { status count }
    revenueByDay { date revenue }
  }
}
"""

CATEGORIES_QUERY = "{ adminSalesCategories }"
DEFAULT_FROM = "2026-01-01T00:00:00Z"
DEFAULT_TO = "2026-01-10T00:00:00Z"


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
        or not re.fullmatch(r"sales_metrics_test_[0-9a-f]{32}", name)
        or user != name
    ):
        pytest.fail(
            "DSN must use loopback, an explicit port, and a unique sales_metrics_test_<uuid> role/database",
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
    schema_name = "sales_metrics_" + uuid.uuid4().hex
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


def _metrics(database, *, variables=None, context=None):
    result = _execute(
        METRICS_QUERY,
        {"from": DEFAULT_FROM, "to": DEFAULT_TO, **(variables or {})},
        context,
    )
    assert result.errors is None, result.errors
    return result.data["adminSalesMetrics"]


def _insert_product(database, bgg_id, category="Strategy", name=None):
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO product_boardgame (bgg_id, name, category) VALUES (%s, %s, %s)",
            [bgg_id, name or f"Game {bgg_id}", category],
        )


def _insert_order(
    database,
    *,
    status="paid",
    payment_status="approved",
    total="0.00",
    created_at="2026-01-02T00:00:00Z",
    paid_at="2026-01-02T00:00:00Z",
    lines=(),
):
    order_id = uuid.uuid4()
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO purchase_order "
            "(id, status, total, payment_status, created_at, paid_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [order_id, status, total, payment_status, created_at, paid_at],
        )
        for bgg_id, quantity, subtotal in lines:
            unit_price = Decimal(subtotal) / quantity
            cursor.execute(
                "INSERT INTO purchase_order_line "
                "(order_id, bgg_id, name, unit_price, quantity, subtotal) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [order_id, bgg_id, f"Game {bgg_id}", unit_price, quantity, subtotal],
            )
    return order_id


def _counts(rows):
    return {row["status"]: row["count"] for row in rows}


def test_paid_revenue_uses_half_open_paid_at_and_historical_line_subtotals(database):
    for bgg_id in (1, 2, 3, 4):
        _insert_product(database, bgg_id)
    _insert_order(
        database,
        total="999.00",
        paid_at="2026-01-01T00:00:00Z",
        lines=[(1, 1, "12.50")],
    )
    _insert_order(
        database,
        total="1.00",
        paid_at="2026-01-09T23:59:59Z",
        lines=[(2, 1, "23.25")],
    )
    _insert_order(
        database,
        total="50.00",
        paid_at="2026-01-10T00:00:00Z",
        lines=[(3, 1, "50.00")],
    )
    _insert_order(
        database,
        status="pending_payment",
        total="40.00",
        payment_status="pending",
        paid_at="2026-01-05T00:00:00Z",
        lines=[(4, 1, "40.00")],
    )

    metrics = _metrics(database)

    assert metrics["revenueTotal"] == 35.75
    assert metrics["ordersCount"] == 2


def test_exact_line_filters_count_each_order_once_and_sum_only_matching_lines(database):
    for bgg_id, category in (
        (10, "Strategy"),
        (11, "Strategy"),
        (12, "strategy"),
        (13, "Strategy Deluxe"),
    ):
        _insert_product(database, bgg_id, category)
    _insert_order(
        database,
        total="100.00",
        lines=[(10, 2, "18.00"), (11, 1, "40.00")],
    )
    _insert_order(database, lines=[(12, 1, "7.00")])
    _insert_order(database, lines=[(13, 1, "8.00")])

    category_metrics = _metrics(database, variables={"categories": ["Strategy"]})
    assert category_metrics["ordersCount"] == 1
    assert category_metrics["revenueTotal"] == 58.00

    product_metrics = _metrics(database, variables={"productIds": [10]})
    assert product_metrics["ordersCount"] == 1
    assert product_metrics["revenueTotal"] == 18.00

    metrics = _metrics(
        database,
        variables={"categories": ["Strategy"], "productIds": [10]},
    )

    assert metrics["ordersCount"] == 1
    assert metrics["revenueTotal"] == 18.00
    assert metrics["topProducts"] == [
        {"bggId": 10, "name": "Game 10", "units": 2, "revenue": 18.00}
    ]


def test_top_ten_orders_by_units_then_revenue_then_bgg_id(database):
    for bgg_id in (10, 11, 12, 13, 20, 21, 22, 23, 24, 25, 26, 27):
        _insert_product(database, bgg_id)
    products = [
        (10, 5, "50.00"),
        (11, 5, "60.00"),
        (12, 5, "60.00"),
        (13, 5, "40.00"),
        *((bgg_id, 4, "100.00") for bgg_id in range(20, 28)),
    ]
    for bgg_id, quantity, subtotal in products:
        _insert_order(database, lines=[(bgg_id, quantity, subtotal)])

    top = _metrics(database)["topProducts"]

    assert [product["bggId"] for product in top] == [
        11, 12, 10, 13, 20, 21, 22, 23, 24, 25
    ]


def test_revenue_by_day_uses_utc_calendar_dates_without_deciding_gap_filling(database):
    """The contract leaves zero-filled versus omitted empty days unresolved."""
    _insert_product(database, 30)
    _insert_order(
        database,
        created_at="2026-01-02T00:00:00Z",
        paid_at="2026-01-02T00:30:00+02:00",
        lines=[(30, 1, "5.00")],
    )
    _insert_order(
        database,
        created_at="2026-01-04T00:00:00Z",
        paid_at="2026-01-04T00:30:00Z",
        lines=[(30, 1, "7.00")],
    )

    days = _metrics(database)["revenueByDay"]

    active_days = {day["date"]: day["revenue"] for day in days if day["revenue"]}
    assert active_days == {"2026-01-01": 5.00, "2026-01-04": 7.00}


def test_dispatch_boundaries_pending_and_excluded_order_states(database):
    _insert_product(database, 40)
    query_time = datetime.now(timezone.utc)
    offsets = (1439, 1440, 4319, 4320)
    for minute in offsets:
        _insert_order(
            database,
            created_at="2026-01-02T12:00:00Z",
            paid_at=(query_time - timedelta(minutes=minute)).isoformat(),
            lines=[(40, 1, "1.00")],
        )
    _insert_order(
        database,
        status="pending_payment",
        payment_status="pending",
        created_at="2026-01-02T12:00:00Z",
        paid_at=None,
    )
    for status in ("rejected", "cancelled", "paid_stock_conflict"):
        _insert_order(
            database,
            status=status,
            payment_status=status,
            created_at="2026-01-02T12:00:00Z",
            paid_at="2026-01-02T00:00:00Z",
        )

    metrics = _metrics(database)

    assert _counts(metrics["ordersByDispatchStatus"]) == {
        "En preparación": 1,
        "Enviado": 2,
        "Entregado": 1,
        "Pendiente": 1,
    }


def test_payment_and_dispatch_status_counts_use_created_at(database):
    _insert_product(database, 41)
    _insert_order(
        database,
        payment_status="created-in-range",
        created_at="2026-01-02T12:00:00Z",
        paid_at="2025-12-31T23:00:00Z",
        lines=[(41, 1, "1.00")],
    )
    _insert_order(
        database,
        payment_status="created-outside-range",
        created_at="2025-12-31T23:59:59Z",
        paid_at="2026-01-02T12:00:00Z",
        lines=[(41, 1, "2.00")],
    )

    metrics = _metrics(database)

    assert _counts(metrics["ordersByPaymentStatus"]) == {"created-in-range": 1}
    assert _counts(metrics["ordersByDispatchStatus"]) == {"Entregado": 1}
    assert metrics["ordersCount"] == 1
    assert metrics["revenueTotal"] == 2.00


def test_empty_range_returns_zero_totals_and_empty_collections(database):
    metrics = _metrics(database)

    assert metrics == {
        "revenueTotal": 0,
        "ordersCount": 0,
        "topProducts": [],
        "ordersByDispatchStatus": [],
        "ordersByPaymentStatus": [],
        "revenueByDay": [],
    }


@pytest.mark.parametrize(
    "variables,field",
    [
        ({"from": "2026-01-01"}, "from"),
        ({"from": "2026-01-01T00:00:00"}, "from"),
        (
            {"from": "2026-01-03T00:00:00Z", "to": "2026-01-02T00:00:00Z"},
            "to",
        ),
        (
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
            "to",
        ),
        (
            {"from": "2025-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z"},
            "to",
        ),
    ],
)
def test_invalid_timezone_or_range_returns_field_specific_invalid_error(
    database, variables, field
):
    result = _execute(
        METRICS_QUERY,
        {"from": DEFAULT_FROM, "to": DEFAULT_TO, **variables},
    )

    assert result.errors
    assert result.errors[0].extensions == {"code": "INVALID", "field": field}


@pytest.mark.parametrize(
    "variables,missing_field",
    [
        ({"from": DEFAULT_FROM, "to": None}, "to"),
        ({"from": None, "to": DEFAULT_TO}, "from"),
    ],
)
def test_one_sided_date_range_returns_invalid_for_missing_bound(
    database, variables, missing_field
):
    result = _execute(METRICS_QUERY, variables)

    assert result.errors
    assert result.errors[0].extensions == {
        "code": "INVALID",
        "field": missing_field,
    }


@pytest.mark.parametrize(
    "context,code",
    [
        (SimpleNamespace(user=None), "UNAUTHENTICATED"),
        (_context(is_staff=False), "FORBIDDEN"),
    ],
)
def test_metrics_requires_active_staff_or_superuser(database, context, code):
    result = _execute(
        METRICS_QUERY,
        {"from": DEFAULT_FROM, "to": DEFAULT_TO},
        context,
    )

    assert result.errors and result.errors[0].extensions["code"] == code


def test_metrics_allows_superuser(database):
    result = _execute(
        METRICS_QUERY,
        {"from": DEFAULT_FROM, "to": DEFAULT_TO},
        _context(is_staff=False, is_superuser=True),
    )

    assert result.errors is None, result.errors
    assert result.data["adminSalesMetrics"]["revenueTotal"] == 0


def test_categories_returns_distinct_stored_category_strings(database):
    for bgg_id, category in (
        (50, "Strategy"),
        (51, "Strategy"),
        (52, "strategy"),
        (53, " Strategy "),
    ):
        _insert_product(database, bgg_id, category)

    result = _execute(CATEGORIES_QUERY)

    assert result.errors is None, result.errors
    categories = result.data["adminSalesCategories"]
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT category FROM product_boardgame ORDER BY category"
        )
        expected_categories = [row[0] for row in cursor.fetchall()]
    assert categories == expected_categories
    assert set(categories) == {
        "Strategy",
        "strategy",
        " Strategy ",
    }


@pytest.mark.parametrize(
    "context,code",
    [
        (SimpleNamespace(user=None), "UNAUTHENTICATED"),
        (_context(is_active=False), "UNAUTHENTICATED"),
        (_context(is_staff=False), "FORBIDDEN"),
    ],
)
def test_categories_requires_active_staff_or_superuser(database, context, code):
    result = _execute(CATEGORIES_QUERY, context=context)

    assert result.errors and result.errors[0].extensions["code"] == code


def test_categories_allows_superuser(database):
    result = _execute(
        CATEGORIES_QUERY,
        context=_context(is_staff=False, is_superuser=True),
    )

    assert result.errors is None, result.errors
    assert result.data["adminSalesCategories"] == []