import hashlib
import hmac
import json
import os
import sys
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import django
import pytest
import requests

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
django.setup()

from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor
from saleor import app as celery_app
from saleor.catalog_events import tasks
from saleor.catalog_events.outbox import record_catalog_event
from saleor.graphql import api, credit, orders


USER = SimpleNamespace(
    pk=7, is_active=True, is_staff=True, is_superuser=False
)
UTC_NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def api_database(monkeypatch):
    cursor = MagicMock()
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    monkeypatch.setattr(api, "connection", connection)
    monkeypatch.setattr(api.transaction, "atomic", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(api, "_authenticated_user", lambda _info: USER)
    monkeypatch.setattr(api, "record_catalog_event", MagicMock())
    return cursor, api.record_catalog_event


def _promotion_row(identifier=11):
    return (
        identifier,
        "Sale",
        False,
        10,
        None,
        None,
        UTC_NOW,
        True,
    )


def test_product_create_and_update_write_events_inside_mutation_transactions(api_database):
    cursor, record_event = api_database
    cursor.fetchone.return_value = (101, "New game")
    created = api.CreateProductMutation.mutate(
        None,
        None,
        SimpleNamespace(
            name="New game",
            price=25,
            quantity=3,
            bggId=101,
            publisher="",
            imageUrl="",
            category=None,
        ),
    )
    assert created.product.bgg_id == 101
    record_event.assert_called_once_with(cursor, "product.changed", 101)

    record_event.reset_mock()
    cursor.fetchone.return_value = (101, "Updated game", 30, 3)
    updated = api.UpdateProductMutation.mutate(
        None,
        None,
        "101",
        SimpleNamespace(
            name="Updated game",
            price=None,
            publisher=None,
            imageUrl=None,
            imageFileName=None,
            releaseYear=None,
            minPlayers=None,
            maxPlayers=None,
            playingTimeMinutes=None,
            minAge=None,
            maxAge=None,
            weight=None,
            category=None,
        ),
    )
    assert updated.product.name == "Updated game"
    record_event.assert_called_once_with(cursor, "product.changed", 101)


def test_product_mutation_does_not_emit_event_for_no_op_or_missing_product(api_database):
    cursor, record_event = api_database
    empty_update = SimpleNamespace(
        name=None,
        price=None,
        publisher=None,
        imageUrl=None,
        imageFileName=None,
        releaseYear=None,
        minPlayers=None,
        maxPlayers=None,
        playingTimeMinutes=None,
        minAge=None,
        maxAge=None,
        weight=None,
        category=None,
    )
    no_op = api.UpdateProductMutation.mutate(None, None, "101", empty_update)
    assert no_op.userErrors[0].code == "NO_OP"
    record_event.assert_not_called()

    cursor.fetchone.return_value = None
    missing = api.UpdateProductMutation.mutate(
        None,
        None,
        "101",
        SimpleNamespace(**{**empty_update.__dict__, "name": "Updated game"}),
    )
    assert missing.userErrors[0].code == "PRODUCT_NOT_FOUND"
    record_event.assert_not_called()


def test_stock_adjustment_writes_event_with_stock_change(api_database):
    cursor, record_event = api_database
    movement_id = str(uuid.uuid4())
    cursor.fetchone.side_effect = [(movement_id,), ("IN_PROGRESS",), (5,)]
    info = SimpleNamespace(
        context=SimpleNamespace(headers={}, opkey_delegated=False)
    )

    result = api.AdjustProductStockMutation.mutate(
        None,
        info,
        "42",
        movement_id,
        SimpleNamespace(value="IN"),
        3,
    )

    assert result.status == "APPLIED"
    record_event.assert_called_once_with(cursor, "stock.changed", 42)


def test_promotion_create_and_update_write_events(api_database):
    cursor, record_event = api_database
    cursor.fetchone.return_value = _promotion_row()
    created = api.CreatePromotionMutation.mutate(
        None,
        None,
        SimpleNamespace(name="Sale", discount_percent=10, active=True),
    )
    assert created.promotion.id == 11
    record_event.assert_called_once_with(cursor, "promotion.changed", 11)

    record_event.reset_mock()
    cursor.fetchone.return_value = _promotion_row()
    updated = api.UpdatePromotionMutation.mutate(
        None,
        None,
        "11",
        SimpleNamespace(name=None, discount_percent=None, active=False),
    )
    assert updated.promotion.id == 11
    record_event.assert_called_once_with(cursor, "promotion.changed", 11)


def test_confirmed_checkout_stock_decrement_writes_transactional_event(monkeypatch):
    cursor = MagicMock()
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    monkeypatch.setattr(orders, "connection", connection)
    monkeypatch.setattr(orders.transaction, "atomic", lambda: nullcontext())
    monkeypatch.setattr(api, "_authenticated_user", lambda _info: USER)
    monkeypatch.setattr(api, "_get_cart_id", lambda *_args: "cart")
    monkeypatch.setattr(orders, "_lock_credit_account", lambda *_args: None)
    monkeypatch.setattr(orders, "_load_order", lambda *_args, **_kwargs: {
        "mp_preference_id": "preference-1",
        "total": "12.50",
        "currency": "ARS",
        "status": "pending_payment",
    })
    monkeypatch.setattr(orders, "_order_payload", lambda *_args: {"status": "paid"})
    monkeypatch.setattr(orders, "_rereserve_released_coupon", lambda *_args: None)
    monkeypatch.setattr(orders, "_set_coupon_reservation_state", lambda *_args: None)
    record_event = MagicMock()
    monkeypatch.setattr(orders, "record_catalog_event", record_event)
    cursor.fetchone.side_effect = [None, None, None]
    cursor.fetchall.side_effect = [[(42, 2)], [(42, 5)]]
    monkeypatch.setattr(
        orders.payments,
        "fetch_payment",
        lambda _payment_id: {
            "status": 200,
            "response": {
                "id": "payment-1",
                "external_reference": "770fa8d4-8e04-41ea-9332-43fa8ce96711",
                "transaction_amount": "12.50",
                "currency_id": "ARS",
                "status": "approved",
                "status_detail": "accredited",
            },
        },
    )
    monkeypatch.setattr(orders.payments, "is_sandbox_payment", lambda _payment: True)
    monkeypatch.setattr(
        orders.payments, "verified_preference", lambda *_args: "preference-1"
    )
    info = SimpleNamespace(context=SimpleNamespace(headers={}))

    result = orders.confirm_payment(
        info,
        "770fa8d4-8e04-41ea-9332-43fa8ce96711",
        "payment-1",
    )

    assert result.order == {"status": "paid"}
    record_event.assert_called_once_with(cursor, "stock.changed", 42)


def test_store_credit_checkout_commits_stock_and_outbox_event_together(monkeypatch):
    alias = f"credit_catalog_events_{uuid.uuid4().hex}"
    connections.databases[alias] = {
        **connections.databases["default"],
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": {},
    }
    database = connections[alias]
    try:
        MigrationExecutor(database).migrate([("catalog_events", "0001_initial")])
        definitions = (
            "CREATE TABLE credit_account (user_id integer PRIMARY KEY, balance numeric)",
            "CREATE TABLE purchase_order (id text PRIMARY KEY, user_id integer, status text, total numeric, "
            "currency text, shipping_mode text, recipient text, street text, city text, postal_code text, "
            "phone text, payment_status text, payment_status_detail text, paid_at timestamp)",
            "CREATE TABLE product_boardgame (bgg_id integer PRIMARY KEY, quantity integer)",
            "CREATE TABLE purchase_order_line (order_id text, bgg_id integer, name text, "
            "unit_price numeric, quantity integer, subtotal numeric)",
            "CREATE TABLE credit_transaction (id integer PRIMARY KEY AUTOINCREMENT, user_id integer, "
            "order_id text, amount numeric, currency text)",
            "CREATE TABLE credit_payment (order_id text PRIMARY KEY, user_id integer, "
            "transaction_id integer, amount numeric, currency text)",
            "CREATE TABLE invoice (order_id text PRIMARY KEY, issued_at timestamp, total numeric)",
            "CREATE TABLE wishlist_entry (user_id integer, bgg_id integer)",
            "CREATE TABLE shopping_cart_line (cart_id text, bgg_id integer, quantity integer)",
        )
        with database.cursor() as cursor:
            for definition in definitions:
                cursor.execute(definition)
            cursor.execute("INSERT INTO credit_account VALUES (7, 100)")
            cursor.execute("INSERT INTO product_boardgame VALUES (42, 5)")

        monkeypatch.setattr(credit, "connection", database)
        monkeypatch.setattr(
            credit,
            "_checkout_snapshot",
            lambda _cursor, order_id, _user_id: {"id": order_id},
        )
        user = SimpleNamespace(pk=7)
        with transaction.atomic(using=alias), database.cursor() as cursor:
            credit._checkout_purchase(
                cursor,
                user,
                "pickup",
                {},
                ("cart-7", [(42, "Game", Decimal("10"), 2, 5)]),
                Decimal("20"),
                Decimal("80"),
            )

        with database.cursor() as cursor:
            cursor.execute("SELECT quantity FROM product_boardgame WHERE bgg_id = 42")
            stock = cursor.fetchone()[0]
            cursor.execute(
                "SELECT event_type, aggregate_type, aggregate_id, delivered_at "
                "FROM catalog_event_outbox"
            )
            events = cursor.fetchall()

        assert stock == 3
        assert events == [("stock.changed", "stock", "42", None)]
    finally:
        database.close()
        connections.databases.pop(alias, None)


def test_initial_outbox_migration_and_event_rows_are_local_safe():
    alias = f"catalog_events_{uuid.uuid4().hex}"
    connections.databases[alias] = {
        **connections.databases["default"],
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": {},
    }
    database = connections[alias]
    try:
        MigrationExecutor(database).migrate([("catalog_events", "0001_initial")])

        class CursorAdapter:
            def __init__(self, cursor):
                self.cursor = cursor

            def execute(self, sql, parameters):
                return self.cursor.execute(sql.replace("%s", "?"), parameters)

        database.ensure_connection()
        cursor = database.connection.cursor()
        try:
            adapted = CursorAdapter(cursor)
            for event_type, aggregate_id in (
                ("product.changed", 42),
                ("stock.changed", 42),
                ("promotion.changed", 11),
            ):
                record_catalog_event(adapted, event_type, aggregate_id)
            cursor.execute(
                "SELECT event_type, aggregate_type, aggregate_id, attempts, delivered_at "
                "FROM catalog_event_outbox ORDER BY event_type"
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
        assert rows == [
            ("product.changed", "product", "42", 0, None),
            ("promotion.changed", "promotion", "11", 0, None),
            ("stock.changed", "stock", "42", 0, None),
        ]
    finally:
        database.close()
        connections.databases.pop(alias, None)


class FakeOutboxQuery:
    def __init__(self, event):
        self.event = event

    def filter(self, **_filters):
        return self

    def order_by(self, *_fields):
        return self

    def select_for_update(self, **_options):
        return self

    def first(self):
        return self.event


class FakeOutboxManager:
    def __init__(self, event):
        self.event = event

    def filter(self, **_filters):
        return FakeOutboxQuery(self.event)


def _outbox_event(attempts=0):
    return SimpleNamespace(
        event_id=uuid.UUID("2f2cdbd3-83b3-4f4a-9df4-04299f640352"),
        event_type="stock.changed",
        occurred_at=UTC_NOW.replace(tzinfo=None),
        aggregate_type="stock",
        aggregate_id="42",
        attempts=attempts,
        next_attempt_at=UTC_NOW,
        delivered_at=None,
        save=MagicMock(),
    )


def _run_dispatch(
    monkeypatch, event, response=None, request_error=None, secret="shared-secret"
):
    monkeypatch.setattr(
        tasks.CatalogEventOutbox, "objects", FakeOutboxManager(event)
    )
    monkeypatch.setattr(tasks.transaction, "atomic", lambda: nullcontext())
    monkeypatch.setattr(tasks.timezone, "now", lambda: UTC_NOW)
    monkeypatch.setattr(tasks.settings, "CATALOG_EVENTS_HMAC_SECRET", secret, raising=False)
    monkeypatch.setattr(
        tasks.settings,
        "CATALOG_EVENTS_BFF_URL",
        "http://bff:5000/internal/v1/catalog-events",
        raising=False,
    )
    post = MagicMock(side_effect=request_error) if request_error else MagicMock(
        return_value=response
    )
    monkeypatch.setattr(tasks.requests, "post", post)
    return tasks.dispatch_due_catalog_events.run(batch_size=1), post


def test_dispatcher_signs_exact_minimal_body_and_marks_only_success(monkeypatch):
    event = _outbox_event()
    response = SimpleNamespace(status_code=204)
    result, post = _run_dispatch(monkeypatch, event, response=response)
    assert result == 1

    kwargs = post.call_args.kwargs
    body = kwargs["data"]
    signature = "sha256=" + hmac.new(
        b"shared-secret", body, hashlib.sha256
    ).hexdigest()
    assert kwargs["headers"]["X-Catalog-Event-Signature"] == signature
    assert set(json.loads(body)) == {
        "event_id",
        "event_type",
        "occurred_at",
        "aggregate_type",
        "aggregate_id",
    }
    assert json.loads(body)["occurred_at"].endswith("Z")
    assert kwargs["timeout"] == 5
    assert event.delivered_at == UTC_NOW
    event.save.assert_called_once_with(update_fields=["delivered_at"])


@pytest.mark.parametrize(
    "response,request_error",
    [
        (SimpleNamespace(status_code=503), None),
        (SimpleNamespace(status_code=302), None),
        (None, requests.Timeout("timeout")),
    ],
)
def test_dispatcher_retries_failures_without_marking_delivered(
    monkeypatch, response, request_error
):
    event = _outbox_event()
    _run_dispatch(
        monkeypatch,
        event,
        response=response,
        request_error=request_error,
    )

    assert event.delivered_at is None
    assert event.attempts == 1
    assert event.next_attempt_at == UTC_NOW + timedelta(seconds=1)
    event.save.assert_called_once_with(
        update_fields=["attempts", "next_attempt_at"]
    )


def test_dispatcher_retries_when_shared_secret_is_missing(monkeypatch):
    event = _outbox_event()
    _result, post = _run_dispatch(monkeypatch, event, secret="")

    post.assert_not_called()
    assert event.delivered_at is None
    assert event.attempts == 1


def test_retry_delay_is_exponential_and_capped_at_five_minutes():
    event = _outbox_event(attempts=10)
    tasks._retry(event, UTC_NOW)

    assert event.attempts == 11
    assert event.next_attempt_at == UTC_NOW + timedelta(seconds=300)


def test_compose_celery_app_autodiscovers_and_schedules_outbox_dispatch():
    celery_app.autodiscover_tasks(force=True)

    assert (
        celery_app.conf.beat_schedule["dispatch-catalog-events"]["task"]
        == "saleor.catalog_events.tasks.dispatch_due_catalog_events"
    )
    assert "saleor.catalog_events.tasks.dispatch_due_catalog_events" in celery_app.tasks
