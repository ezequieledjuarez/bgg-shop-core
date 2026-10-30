"""Contract and transactional tests for credit checkout."""

import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import django
import pytest
from graphql import build_schema

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
django.setup()

from saleor.graphql.api import schema
from saleor.graphql import credit
from django.db import connections, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings


MUTATION = """
mutation Checkout($input: CheckoutWithCreditInput!) {
  checkoutWithCredit(input: $input) {
    credit { balance currency }
    order {
      id status total currency shippingMode recipient street city postalCode phone
      paymentStatus paymentStatusDetail createdAt paidAt
      lines { bggId name unitPrice quantity subtotal }
      invoice { number issuedAt total }
    }
    userErrors { field code message }
  }
}
"""
USER = SimpleNamespace(pk=1)
CART = "b67f84b0-c5f6-43d3-8412-c5a8a30d0982"


@pytest.fixture
def database(monkeypatch):
    alias = f"isolated_checkout_{uuid.uuid4().hex}"
    connections.databases[alias] = {
        **connections.databases["default"],
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": {},
    }
    database = connections[alias]
    definitions = (
        "CREATE TABLE credit_account (user_id integer PRIMARY KEY, balance numeric, currency text)",
        "CREATE TABLE credit_transaction (id integer PRIMARY KEY, user_id integer, gift_card_id text, "
        "order_id text UNIQUE, amount numeric, currency text)",
        "CREATE TABLE credit_payment (order_id text PRIMARY KEY, user_id integer, transaction_id integer UNIQUE, "
        "amount numeric, currency text)",
        "CREATE TABLE credit_retry_guard (order_id text PRIMARY KEY, user_id integer, credit_order_id text)",
        "CREATE TABLE credit_retry_attempt (order_id text PRIMARY KEY, user_id integer, preference_id text)",
        "CREATE TABLE credit_payment_review (order_id text, payment_id text, PRIMARY KEY(order_id, payment_id))",
        "CREATE TABLE credit_payment_review_release (order_id text, payment_id text, PRIMARY KEY(order_id, payment_id))",
        "CREATE TABLE credit_idempotency (user_id integer, operation text, idempotency_key text, "
        "fingerprint blob, balance_snapshot numeric, error_code text, order_snapshot text, "
        "created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(user_id, operation, idempotency_key))",
        "CREATE TABLE shopping_cart (cart_id text PRIMARY KEY, user_id integer UNIQUE)",
        "CREATE TABLE shopping_cart_line (cart_id text, bgg_id integer, quantity integer, "
        "PRIMARY KEY(cart_id, bgg_id))",
        "CREATE TABLE product_boardgame (bgg_id integer PRIMARY KEY, name text, price_amount numeric, quantity integer)",
        "CREATE TABLE purchase_order (id text PRIMARY KEY, user_id integer, status text, total numeric, currency text, "
        "shipping_mode text, recipient text, street text, city text, postal_code text, phone text, "
        "mp_preference_id text, mp_payment_id text, payment_status text, payment_status_detail text, "
        "created_at timestamp DEFAULT CURRENT_TIMESTAMP, paid_at timestamp)",
        "CREATE TABLE purchase_order_line (order_id text, bgg_id integer, name text, "
        "unit_price numeric, quantity integer, subtotal numeric)",
        "CREATE TABLE invoice (order_id text PRIMARY KEY, number integer DEFAULT 1, issued_at timestamp, total numeric)",
        "CREATE TABLE wishlist_entry (user_id integer, bgg_id integer)",
    )
    with database.cursor() as cursor:
        for definition in definitions:
            cursor.execute(definition)
        cursor.execute("INSERT INTO credit_account VALUES (1, 30, 'ARS')")
        cursor.execute("INSERT INTO shopping_cart VALUES (%s, 1)", [CART])
        cursor.execute("INSERT INTO product_boardgame VALUES (7, 'Game', 12.50, 5)")
        cursor.execute("INSERT INTO shopping_cart_line VALUES (%s, 7, 2)", [CART])
        cursor.execute("INSERT INTO wishlist_entry VALUES (1, 7), (2, 7), (1, 8)")
    atomic = transaction.atomic
    monkeypatch.setattr(credit, "connection", database)
    monkeypatch.setattr(credit, "_lock_clause", lambda: "")
    monkeypatch.setattr(credit.transaction, "atomic", lambda: atomic(using=alias))
    yield database
    database.close()


def checkout(user=USER, key=None, **changes):
    data = {"shippingMode": "PICKUP", "idempotencyKey": str(key or uuid.uuid4())}
    data.update(changes)
    with patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(
            MUTATION, variable_values={"input": data},
            context_value=SimpleNamespace(headers={}),
        )
    assert result.errors is None
    return result.data["checkoutWithCredit"]


def scalar(database, sql, params=None):
    with database.cursor() as cursor:
        cursor.execute(sql, params or [])
        return cursor.fetchone()[0]


def test_pending_provider_order_blocks_checkout_without_financial_changes(database):
    pending_id = str(uuid.uuid4())
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO purchase_order (id, user_id, status) VALUES (%s, %s, %s)",
            [pending_id, USER.pk, "pending_payment"],
        )
    key = uuid.uuid4()
    result = checkout(key=key)
    assert result["userErrors"][0]["code"] == "PENDING_PAYMENT"
    assert result["order"] is None and result["credit"] is None
    assert scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1") == 30
    assert scalar(database, "SELECT quantity FROM product_boardgame WHERE bgg_id = 7") == 5
    assert scalar(database, "SELECT quantity FROM shopping_cart_line WHERE cart_id = %s", [CART]) == 2
    assert scalar(database, "SELECT count(*) FROM purchase_order") == 1
    assert scalar(database, "SELECT count(*) FROM invoice") == 0
    assert scalar(database, "SELECT count(*) FROM credit_payment") == 0
    with database.cursor() as cursor:
        cursor.execute("UPDATE purchase_order SET status = %s WHERE id = %s", ["paid", pending_id])
    assert checkout(key=key) == result
    assert checkout()["userErrors"] == []


def test_credit_checkout_guards_only_existing_rejected_orders_for_buyer(database):
    rejected, other, later = (str(uuid.uuid4()) for _ in range(3))
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO purchase_order (id, user_id, status) VALUES (%s, 1, 'rejected'), (%s, 2, 'rejected')",
            [rejected, other],
        )
    result = checkout()
    assert result["userErrors"] == []
    with database.cursor() as cursor:
        cursor.execute("INSERT INTO purchase_order (id, user_id, status) VALUES (%s, 1, 'rejected')", [later])
        cursor.execute("SELECT order_id, user_id, credit_order_id FROM credit_retry_guard")
        assert cursor.fetchall() == [(rejected, USER.pk, result["order"]["id"])]


def test_uncertain_retry_blocks_credit_even_if_order_was_rejected_again(database):
    order_id = str(uuid.uuid4())
    with database.cursor() as cursor:
        cursor.execute("INSERT INTO purchase_order (id, user_id, status) VALUES (%s, 1, 'rejected')", [order_id])
        cursor.execute("INSERT INTO credit_retry_attempt (order_id, user_id) VALUES (%s, 1)", [order_id])
    assert checkout()["userErrors"][0]["code"] == "PENDING_PAYMENT"
    assert scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1") == 30
    assert scalar(database, "SELECT count(*) FROM credit_retry_guard") == 0


def test_failed_credit_checkout_does_not_guard_rejected_orders(database):
    with database.cursor() as cursor:
        cursor.execute("INSERT INTO purchase_order (id, user_id, status) VALUES (%s, 1, 'rejected')", [str(uuid.uuid4())])
        cursor.execute("UPDATE credit_account SET balance = 0 WHERE user_id = 1")
    assert checkout()["userErrors"][0]["code"] == "INSUFFICIENT_CREDIT"
    assert scalar(database, "SELECT count(*) FROM credit_retry_guard") == 0


@pytest.mark.parametrize(("owner", "status"), [
    (2, "pending_payment"), (1, "paid"), (1, "rejected"), (1, "cancelled"),
])
def test_other_orders_do_not_block_credit_checkout(database, owner, status):
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO purchase_order (id, user_id, status) VALUES (%s, %s, %s)",
            [str(uuid.uuid4()), owner, status],
        )
    assert checkout()["userErrors"] == []


def test_successful_retry_replays_even_when_provider_order_becomes_pending(database):
    key = uuid.uuid4()
    original = checkout(key=key)
    assert original["userErrors"] == []
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO purchase_order (id, user_id, status) VALUES (%s, %s, %s)",
            [str(uuid.uuid4()), USER.pk, "pending_payment"],
        )
    assert checkout(key=key) == original


def test_checkout_credit_matches_approved_schema():
    reference = build_schema(
        (Path(__file__).resolve().parents[1] / "specs" / "schema.graphql").read_text()
    )
    for type_name in ("CheckoutWithCreditInput", "CreditCheckoutPayload", "PurchaseOrder"):
        actual = schema.graphql_schema.get_type(type_name)
        expected = reference.get_type(type_name)
        assert actual is not None
        assert {name: str(field.type) for name, field in actual.fields.items()} == {
            name: str(field.type) for name, field in expected.fields.items()
        }
    actual = schema.graphql_schema.mutation_type.fields["checkoutWithCredit"]
    expected = reference.mutation_type.fields["checkoutWithCredit"]
    assert str(actual.type) == str(expected.type)
    assert {name: str(argument.type) for name, argument in actual.args.items()} == {
        name: str(argument.type) for name, argument in expected.args.items()
    }
    assert actual.description == expected.description
    assert (
        schema.graphql_schema.get_type("CheckoutWithCreditInput").fields["idempotencyKey"].description
        == reference.get_type("CheckoutWithCreditInput").fields["idempotencyKey"].description
    )
    for name in ("checkout", "retryPayment", "confirmPayment"):
        actual = schema.graphql_schema.mutation_type.fields[name]
        expected = reference.mutation_type.fields[name]
        assert str(actual.type) == str(expected.type)
        assert {key: str(argument.type) for key, argument in actual.args.items()} == {
            key: str(argument.type) for key, argument in expected.args.items()
        }


def test_checkout_migration_is_additive_and_irreversible():
    migration = MigrationLoader(None).disk_migrations[("graphql", "0002_credit_checkout")]
    assert ("graphql", "0001_credit_accounts") in migration.dependencies
    assert all(not operation.reversible for operation in migration.operations)
    sql = " ".join(operation.sql for operation in migration.operations)
    assert "CREATE TABLE credit_payment" in sql
    assert "FOREIGN KEY (transaction_id, user_id, order_id)" in sql
    assert "ADD COLUMN order_snapshot jsonb" in sql
    assert "DROP TABLE" not in sql


def test_retry_guard_migration_is_additive_and_irreversible():
    migration = MigrationLoader(None).disk_migrations[("graphql", "0003_credit_retry_guard")]
    assert ("graphql", "0002_credit_checkout") in migration.dependencies
    assert all(not operation.reversible for operation in migration.operations)
    sql = " ".join(operation.sql for operation in migration.operations)
    assert "CREATE TABLE credit_retry_guard" in sql
    assert "CREATE TABLE credit_retry_attempt" in sql
    assert "DROP TABLE" not in sql


@pytest.mark.parametrize("balance,remaining", [(30, "5.00"), (25, "0.00")])
def test_success_and_retry_return_original_snapshot(database, balance, remaining):
    with database.cursor() as cursor:
        cursor.execute("UPDATE credit_account SET balance = %s", [balance])
    key = uuid.uuid4()
    with patch("saleor.graphql.orders.payments.create_preference") as preference, \
            patch("saleor.graphql.orders.payments.fetch_payment") as payment:
        first = checkout(key=key)
        assert first["userErrors"] == []
        assert first["credit"] == {"balance": remaining, "currency": "ARS"}
        assert first["order"]["status"] == "paid"
        assert first["order"]["total"] == 25.0
        assert first["order"]["lines"][0]["unitPrice"] == 12.5
        assert first["order"]["invoice"]["total"] == 25.0
        with database.cursor() as cursor:
            cursor.execute("UPDATE credit_account SET balance = 100 WHERE user_id = 1")
            cursor.execute("UPDATE product_boardgame SET price_amount = 99 WHERE bgg_id = 7")
            cursor.execute("INSERT INTO shopping_cart_line VALUES (%s, 7, 1)", [CART])
            cursor.execute("UPDATE purchase_order SET recipient = 'Changed' WHERE user_id = 1")
        assert checkout(key=key) == first
        preference.assert_not_called()
        payment.assert_not_called()
    assert scalar(database, "SELECT quantity FROM product_boardgame WHERE bgg_id = 7") == 3
    assert scalar(database, "SELECT quantity FROM shopping_cart_line WHERE cart_id = %s", [CART]) == 1
    for table in ("purchase_order", "invoice", "credit_transaction", "credit_payment"):
        assert scalar(database, f"SELECT count(*) FROM {table}") == 1
    assert scalar(database, "SELECT count(*) FROM wishlist_entry WHERE user_id = 1 AND bgg_id = 7") == 0
    assert scalar(database, "SELECT count(*) FROM wishlist_entry WHERE user_id = 2 AND bgg_id = 7") == 1


@pytest.mark.parametrize("change,code", [
    ("UPDATE credit_account SET balance = 24", "INSUFFICIENT_CREDIT"),
    ("UPDATE product_boardgame SET quantity = 1", "INSUFFICIENT_STOCK"),
    ("DELETE FROM shopping_cart_line", "EMPTY_CART"),
    ("UPDATE credit_account SET currency = 'USD'", "INVALID_CURRENCY"),
    ("UPDATE product_boardgame SET price_amount = -1", "INVALID_AMOUNT"),
    ("UPDATE product_boardgame SET price_amount = 'NaN'", "INVALID_AMOUNT"),
    ("UPDATE product_boardgame SET price_amount = 'Infinity'", "INVALID_AMOUNT"),
    ("UPDATE product_boardgame SET price_amount = 1.001", "INVALID_AMOUNT"),
    ("UPDATE product_boardgame SET price_amount = 9999999999.99", "INVALID_AMOUNT"),
    ("UPDATE shopping_cart SET user_id = 2", "EMPTY_CART"),
])
def test_rejections_have_no_financial_effect(database, change, code):
    with database.cursor() as cursor:
        cursor.execute(change)
    before = scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1")
    result = checkout()
    assert result["userErrors"][0]["code"] == code
    assert result["order"] is None
    assert scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1") == before
    for table in ("purchase_order", "invoice", "credit_transaction", "credit_payment"):
        assert scalar(database, f"SELECT count(*) FROM {table}") == 0


def test_rejected_key_is_reserved_and_changed_input_conflicts(database):
    key = uuid.uuid4()
    with database.cursor() as cursor:
        cursor.execute("UPDATE credit_account SET balance = 0")
    first = checkout(key=key)
    with database.cursor() as cursor:
        cursor.execute("UPDATE credit_account SET balance = 100")
    assert checkout(key=key) == first
    assert checkout(key=key, address={"city": "Other"})["userErrors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    assert scalar(database, "SELECT count(*) FROM purchase_order") == 0


def test_address_rejection_is_replayed_and_requires_new_key_for_changed_input(database):
    key = uuid.uuid4()
    invalid_address = {"recipient": "Buyer"}
    valid_address = {"recipient": "Buyer", "street": "Street 1", "city": "City", "postalCode": "1234"}
    first = checkout(key=key, shippingMode="STANDARD", address=invalid_address)
    assert first["userErrors"][0]["code"] == "ADDRESS_REQUIRED"
    assert checkout(key=key, shippingMode="STANDARD", address=invalid_address) == first
    assert checkout(key=key, shippingMode="STANDARD", address=valid_address)["userErrors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    assert checkout(shippingMode="STANDARD", address=valid_address)["userErrors"] == []
    assert scalar(database, "SELECT count(*) FROM credit_payment") == 1


@pytest.mark.parametrize("field,limit", [("recipient", 250), ("street", 500), ("city", 150), ("postalCode", 32), ("phone", 50)])
def test_credit_checkout_validates_address_field_boundaries(database, field, limit):
    invalid = checkout(address={field: "x" * (limit + 1)})
    assert invalid["userErrors"][0]["code"] == "ADDRESS_REQUIRED"
    assert scalar(database, "SELECT count(*) FROM credit_payment") == 0
    valid = checkout(address={field: "x" * limit})
    assert valid["userErrors"] == []
    assert scalar(database, "SELECT count(*) FROM credit_payment") == 1


def test_changed_input_conflicts_after_success(database):
    key = uuid.uuid4()
    assert checkout(key=key)["userErrors"] == []
    assert checkout(key=key, address={"city": "Other"})["userErrors"][0]["code"] == "IDEMPOTENCY_CONFLICT"


def test_shipping_address_is_in_the_original_snapshot(database):
    key = uuid.uuid4()
    address = {"recipient": "Buyer", "street": "Street 1", "city": "City", "postalCode": "1234"}
    first = checkout(key=key, shippingMode="STANDARD", address=address)
    assert first["userErrors"] == []
    assert first["order"]["recipient"] == "Buyer"
    assert first["order"]["postalCode"] == "1234"
    assert checkout(key=key, shippingMode="STANDARD", address=address) == first


def test_added_cart_quantity_is_not_removed(database):
    with database.cursor() as cursor:
        cursor.execute(
            "CREATE TRIGGER add_cart_quantity AFTER UPDATE ON product_boardgame "
            "BEGIN UPDATE shopping_cart_line SET quantity = quantity + 3; END"
        )
    assert checkout()["userErrors"] == []
    assert scalar(database, "SELECT quantity FROM shopping_cart_line WHERE cart_id = %s", [CART]) == 3


def test_cart_update_failure_rolls_back_everything(database):
    with database.cursor() as cursor:
        cursor.execute(
            "CREATE TRIGGER add_quantity AFTER INSERT ON invoice "
            "BEGIN UPDATE shopping_cart_line SET quantity = quantity + 3; END"
        )
        cursor.execute(
            "CREATE TRIGGER fail_cart_update BEFORE UPDATE ON shopping_cart_line "
            "WHEN NEW.quantity < OLD.quantity "
            "BEGIN SELECT RAISE(ABORT, 'private detail'); END"
        )
    assert checkout()["userErrors"][0]["code"] == "SERVER_ERROR"
    assert scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1") == 30
    assert scalar(database, "SELECT quantity FROM product_boardgame WHERE bgg_id = 7") == 5
    assert scalar(database, "SELECT quantity FROM shopping_cart_line WHERE cart_id = %s", [CART]) == 2
    for target in ("purchase_order", "invoice", "credit_transaction", "credit_payment", "credit_idempotency"):
        assert scalar(database, f"SELECT count(*) FROM {target}") == 0


def test_retry_after_rolled_back_database_error(database):
    key = uuid.uuid4()
    with database.cursor() as cursor:
        cursor.execute("CREATE TRIGGER fail_invoice BEFORE INSERT ON invoice "
                       "BEGIN SELECT RAISE(ABORT, 'private detail'); END")
    assert checkout(key=key)["userErrors"][0]["code"] == "SERVER_ERROR"
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER fail_invoice")
    assert checkout(key=key)["userErrors"] == []
    assert scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1") == 5
    assert scalar(database, "SELECT count(*) FROM purchase_order") == 1


def test_zero_total_is_supported(database):
    with database.cursor() as cursor:
        cursor.execute("UPDATE product_boardgame SET price_amount = 0")
        cursor.execute("UPDATE credit_account SET balance = 0")
    result = checkout()
    assert result["userErrors"] == []
    assert result["order"]["total"] == 0
    assert result["credit"]["balance"] == "0.00"
    assert scalar(database, "SELECT amount FROM credit_transaction") == 0


def test_invalid_address_and_authentication_are_sanitized(database):
    assert checkout(user=None)["userErrors"][0]["code"] == "UNAUTHENTICATED"
    assert checkout(shippingMode="STANDARD", address={"city": " "})["userErrors"][0]["code"] == "ADDRESS_REQUIRED"
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError("secret-token")):
        result = schema.execute(MUTATION, variable_values={"input": {
            "shippingMode": "PICKUP", "idempotencyKey": str(uuid.uuid4()),
        }}, context_value=SimpleNamespace(headers={"Authorization": "Bearer secret-token"}))
    assert result.errors is None
    assert "secret-token" not in str(result.data)
    assert result.data["checkoutWithCredit"]["userErrors"][0]["code"] == "SERVER_ERROR"


def test_invalid_key_and_invalid_token(database):
    assert checkout(key="invalid")["userErrors"][0]["code"] == "INVALID_KEY"
    result = schema.execute(MUTATION, variable_values={"input": {
        "shippingMode": "PICKUP", "idempotencyKey": str(uuid.uuid4()),
    }}, context_value=SimpleNamespace(headers={"Authorization": "Bearer invalid"}))
    assert result.errors is None
    assert result.data["checkoutWithCredit"]["userErrors"][0]["code"] == "UNAUTHENTICATED"


def test_retry_after_secret_rotation_uses_fallback(database):
    key = uuid.uuid4()
    with override_settings(SECRET_KEY="old-key", SECRET_KEY_FALLBACKS=[]):
        first = checkout(key=key)
    with override_settings(SECRET_KEY="new-key", SECRET_KEY_FALLBACKS=["old-key"]):
        assert checkout(key=key) == first
        assert checkout(key=key, address={"city": "Other"})["userErrors"][0]["code"] == "IDEMPOTENCY_CONFLICT"


def test_rejected_key_is_scoped_to_buyer(database):
    key = uuid.uuid4()
    with database.cursor() as cursor:
        cursor.execute("UPDATE credit_account SET balance = 0")
        cursor.execute("INSERT INTO credit_account VALUES (2, 0, 'ARS')")
    assert checkout(key=key)["userErrors"][0]["code"] == "INSUFFICIENT_CREDIT"
    assert checkout(user=SimpleNamespace(pk=2), key=key, address={"city": "Other"})["userErrors"][0]["code"] == "EMPTY_CART"


def test_checkout_fails_closed_without_postgresql_row_locks(database, monkeypatch):
    monkeypatch.setattr(credit, "_lock_clause", lambda: (_ for _ in ()).throw(RuntimeError("private detail")))
    result = checkout()
    assert result["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private detail" not in str(result)
    assert scalar(database, "SELECT count(*) FROM credit_idempotency") == 0


@pytest.mark.parametrize("table", [
    "purchase_order", "purchase_order_line", "credit_transaction", "credit_payment", "invoice", "credit_idempotency",
])
def test_database_failure_rolls_back_every_stage(database, table):
    with database.cursor() as cursor:
        cursor.execute(f"CREATE TRIGGER fail_checkout BEFORE INSERT ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'private detail'); END")
    result = checkout()
    assert result["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private detail" not in str(result)
    assert Decimal(str(scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1"))) == Decimal("30")
    assert scalar(database, "SELECT quantity FROM product_boardgame WHERE bgg_id = 7") == 5
    assert scalar(database, "SELECT quantity FROM shopping_cart_line WHERE cart_id = %s", [CART]) == 2
    for target in ("purchase_order", "purchase_order_line", "invoice", "credit_transaction", "credit_payment"):
        assert scalar(database, f"SELECT count(*) FROM {target}") == 0
    assert scalar(database, "SELECT count(*) FROM credit_idempotency") == 0
    assert scalar(database, "SELECT count(*) FROM wishlist_entry WHERE user_id = 1 AND bgg_id = 7") == 1


@pytest.mark.parametrize("action,table", [
    ("UPDATE", "credit_account"),
    ("UPDATE", "product_boardgame"),
    ("DELETE", "wishlist_entry"),
    ("DELETE", "shopping_cart_line"),
])
def test_update_and_delete_failures_roll_back_everything(database, action, table):
    with database.cursor() as cursor:
        cursor.execute(f"CREATE TRIGGER fail_checkout BEFORE {action} ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'private detail'); END")
    result = checkout()
    assert result["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private detail" not in str(result)
    assert scalar(database, "SELECT balance FROM credit_account WHERE user_id = 1") == 30
    assert scalar(database, "SELECT quantity FROM product_boardgame WHERE bgg_id = 7") == 5
    assert scalar(database, "SELECT quantity FROM shopping_cart_line WHERE cart_id = %s", [CART]) == 2
    assert scalar(database, "SELECT count(*) FROM wishlist_entry WHERE user_id = 1 AND bgg_id = 7") == 1
    for target in ("purchase_order", "invoice", "credit_transaction", "credit_payment", "credit_idempotency"):
        assert scalar(database, f"SELECT count(*) FROM {target}") == 0