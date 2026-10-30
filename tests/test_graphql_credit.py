import os
import sys
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import django
import pytest
from graphql import build_schema, parse, print_ast
from django.test import override_settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
django.setup()

from saleor.graphql.api import schema
from saleor.graphql import credit
from django.db import connections, transaction
from django.db.migrations.loader import MigrationLoader


BALANCE_QUERY = "{ myCredit { credit { balance currency } userErrors { code message } } }"
REDEEM_MUTATION = """
mutation Redeem($input: RedeemGiftCardInput!) {
  redeemGiftCard(input: $input) {
    credit { balance currency }
    userErrors { code message }
  }
}
"""


@pytest.fixture
def database(monkeypatch):
    alias = f"isolated_credit_test_{uuid.uuid4().hex}"
    connections.databases[alias] = {
        **connections.databases["default"],
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": {},
    }
    database = connections[alias]
    with database.cursor() as cursor:
        cursor.execute("CREATE TABLE gift_card (id text PRIMARY KEY, code_hash blob UNIQUE, "
                       "amount numeric, currency text, redeemed_at timestamp, redeemed_by integer)")
        cursor.execute("CREATE TABLE credit_account (user_id integer PRIMARY KEY, "
                       "balance numeric NOT NULL CHECK(balance >= 0), currency text NOT NULL)")
        cursor.execute("CREATE TABLE credit_transaction (id integer PRIMARY KEY, "
                       "user_id integer NOT NULL, gift_card_id text NOT NULL UNIQUE, "
                       "amount numeric NOT NULL, currency text NOT NULL)")
        cursor.execute("CREATE TABLE credit_idempotency (id integer PRIMARY KEY, "
                       "user_id integer NOT NULL, operation text NOT NULL, idempotency_key text NOT NULL, "
                       "fingerprint blob NOT NULL, balance_snapshot numeric, error_code text, "
                       "created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                       "UNIQUE(user_id, operation, idempotency_key))")
    atomic = transaction.atomic
    monkeypatch.setattr(credit, "connection", database)
    monkeypatch.setattr(credit, "_lock_clause", lambda: "")
    monkeypatch.setattr(credit.transaction, "atomic", lambda: atomic(using=alias))
    yield database
    database.close()


def issue(database, code, amount="12.50", currency="ARS"):
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO gift_card (id, code_hash, amount, currency) VALUES (%s, %s, %s, %s)",
            [str(uuid.uuid4()), hashlib.sha256(code.encode()).digest(), amount, currency],
        )


def execute(query, user, variables=None):
    with patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(query, variable_values=variables, context_value=SimpleNamespace(headers={}))
    assert result.errors is None
    return next(iter(result.data.values()))


def redeem(user, code, key):
    return execute(REDEEM_MUTATION, user, {"input": {"code": code, "idempotencyKey": str(key)}})


def test_credit_signatures_match_approved_contract():
    document = str(schema)
    assert "myCredit: CreditBalancePayload!" in document
    assert "redeemGiftCard(input: RedeemGiftCardInput!): CreditBalancePayload!" in document
    assert "idempotencyKey: String!" in document


def test_additive_migration_is_discoverable_and_irreversible():
    migration = MigrationLoader(None).disk_migrations[("graphql", "0001_credit_accounts")]
    assert all(not operation.reversible for operation in migration.operations)


def test_anonymous_balance_is_not_zero():
    with patch("saleor.graphql.api._authenticated_user", return_value=None):
        result = schema.execute(
            "{ myCredit { credit { balance currency } userErrors { code } } }",
            context_value=SimpleNamespace(headers={}),
        )
    assert result.errors is None
    assert result.data["myCredit"]["credit"] is None
    assert result.data["myCredit"]["userErrors"][0]["code"] == "UNAUTHENTICATED"


def test_invalid_token_is_not_zero():
    result = schema.execute(BALANCE_QUERY, context_value=SimpleNamespace(headers={"Authorization": "Bearer invalid"}))
    assert result.errors is None
    assert result.data["myCredit"]["credit"] is None
    assert result.data["myCredit"]["userErrors"][0]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("query,field,variables", [
    (BALANCE_QUERY, "myCredit", None),
    (REDEEM_MUTATION, "redeemGiftCard", {
        "input": {"code": "a" * 64, "idempotencyKey": str(uuid.uuid4())},
    }),
])
def test_authentication_exception_is_sanitized(query, field, variables):
    secret = "synthetic-auth-secret-token"
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError(secret)):
        result = schema.execute(
            query, variable_values=variables,
            context_value=SimpleNamespace(headers={"Authorization": f"Bearer {secret}"}),
        )
    assert result.errors is None
    assert secret not in str(result.data)
    assert secret not in str(result.errors)
    assert result.data[field] == {
        "credit": None,
        "userErrors": [{"code": "SERVER_ERROR", "message": "Credit service is unavailable."}],
    }


def test_introspection_matches_credit_contract():
    query = schema.graphql_schema.query_type.fields["myCredit"]
    mutation = schema.graphql_schema.mutation_type.fields["redeemGiftCard"]
    assert str(query.type) == "CreditBalancePayload!"
    assert str(mutation.type) == "CreditBalancePayload!"
    assert str(mutation.args["input"].type) == "RedeemGiftCardInput!"
    fields = schema.graphql_schema.get_type("RedeemGiftCardInput").fields
    assert str(fields["code"].type) == "String!"
    assert str(fields["idempotencyKey"].type) == "String!"


def test_credit_types_match_reference_sdl():
    reference = (Path(__file__).resolve().parents[1] / "specs" / "schema.graphql").read_text()

    def fields(document, name):
        definition = next(item for item in parse(document).definitions if item.name.value == name)
        return {field.name.value: print_ast(field.type) for field in definition.fields}

    for name in ("CreditBalance", "CreditBalancePayload", "RedeemGiftCardInput", "OrderUserError"):
        assert fields(str(schema), name) == fields(reference, name)

    expected = build_schema(reference)
    for type_name, field_name in (
        ("Query", "myCredit"), ("Mutation", "redeemGiftCard"),
        ("CreditBalance", "balance"), ("CreditBalance", "currency"),
        ("RedeemGiftCardInput", "idempotencyKey"),
    ):
        actual_field = schema.graphql_schema.get_type(type_name).fields[field_name]
        expected_field = expected.get_type(type_name).fields[field_name]
        assert actual_field.description == expected_field.description


def test_initial_balance_and_authentication(database):
    assert execute(BALANCE_QUERY, SimpleNamespace(pk=1))["credit"] == {"balance": "0.00", "currency": "ARS"}
    assert redeem(None, "a" * 64, uuid.uuid4())["userErrors"][0]["code"] == "UNAUTHENTICATED"
    assert redeem(SimpleNamespace(pk=1), "a" * 64, "invalid")["userErrors"][0]["code"] == "INVALID_KEY"


def test_redemption_retry_snapshot_and_ownership(database):
    user = SimpleNamespace(pk=1)
    other = SimpleNamespace(pk=2)
    first_code, second_code = "a" * 64, "b" * 64
    issue(database, first_code)
    issue(database, second_code, "7.00")
    key = uuid.uuid4()
    assert redeem(user, first_code, key)["credit"]["balance"] == "12.50"
    assert redeem(user, second_code, uuid.uuid4())["credit"]["balance"] == "19.50"
    assert redeem(user, first_code, key)["credit"]["balance"] == "12.50"
    assert redeem(user, second_code, key)["userErrors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    assert redeem(user, first_code, uuid.uuid4())["userErrors"][0]["code"] == "INVALID_GIFT_CARD"
    used = redeem(other, first_code, uuid.uuid4())
    invalid = redeem(other, "c" * 64, uuid.uuid4())
    assert used == invalid
    assert used["userErrors"][0]["code"] == "INVALID_GIFT_CARD"
    assert execute(BALANCE_QUERY, other)["credit"]["balance"] == "0.00"
    with database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM credit_transaction")
        assert cursor.fetchone()[0] == 2
        cursor.execute("SELECT fingerprint FROM credit_idempotency")
        assert all(first_code.encode() not in bytes(row[0]) for row in cursor.fetchall())


def test_rejected_request_reserves_key(database):
    user = SimpleNamespace(pk=1)
    key = uuid.uuid4()
    first = redeem(user, "missing", key)
    assert first == redeem(user, "missing", key)
    issue(database, "a" * 64)
    assert redeem(user, "a" * 64, key)["userErrors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    assert redeem(user, "a" * 64, uuid.uuid4())["credit"]["balance"] == "12.50"


def test_idempotency_key_is_scoped_to_buyer(database):
    issue(database, "a" * 64)
    issue(database, "b" * 64)
    key = uuid.uuid4()
    assert redeem(SimpleNamespace(pk=1), "a" * 64, key)["credit"]["balance"] == "12.50"
    assert redeem(SimpleNamespace(pk=2), "b" * 64, key)["credit"]["balance"] == "12.50"


def test_retry_survives_secret_rotation_with_fallback(database):
    user = SimpleNamespace(pk=1)
    code = "a" * 64
    key = uuid.uuid4()
    issue(database, code)
    with override_settings(SECRET_KEY="old-key", SECRET_KEY_FALLBACKS=[]):
        assert redeem(user, code, key)["credit"]["balance"] == "12.50"
    with override_settings(SECRET_KEY="new-key", SECRET_KEY_FALLBACKS=["old-key"]):
        assert redeem(user, code, key)["credit"]["balance"] == "12.50"
        assert redeem(user, "b" * 64, key)["userErrors"][0]["code"] == "IDEMPOTENCY_CONFLICT"


def test_wrong_currency_is_rejected_without_redeeming(database):
    user = SimpleNamespace(pk=1)
    code = "a" * 64
    issue(database, code, currency="USD")
    result = redeem(user, code, uuid.uuid4())
    assert result == redeem(user, "b" * 64, uuid.uuid4())
    assert result["userErrors"][0]["code"] == "INVALID_GIFT_CARD"
    with database.cursor() as cursor:
        cursor.execute("SELECT redeemed_at, redeemed_by FROM gift_card")
        assert cursor.fetchone() == (None, None)
        cursor.execute("SELECT balance FROM credit_account WHERE user_id = %s", [user.pk])
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT COUNT(*) FROM credit_transaction")
        assert cursor.fetchone()[0] == 0


def test_oversized_code_is_rejected_before_hashing(database):
    user = SimpleNamespace(pk=1)
    code = "sensitive-code-" * 10000
    with patch.object(credit.hashlib, "sha256", side_effect=RuntimeError("private hash detail")) as hash_code:
        result = redeem(user, code, uuid.uuid4())
    hash_code.assert_not_called()
    assert result == {
        "credit": None,
        "userErrors": [{"code": "INVALID_GIFT_CARD", "message": "Gift card cannot be redeemed."}],
    }
    assert code not in str(result)
    with database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM credit_idempotency")
        assert cursor.fetchone()[0] == 0


def test_non_ars_account_is_rejected(database):
    user = SimpleNamespace(pk=1)
    with database.cursor() as cursor:
        cursor.execute("INSERT INTO credit_account VALUES (%s, %s, %s)", [user.pk, 5, "USD"])
    assert execute(BALANCE_QUERY, user)["userErrors"][0]["code"] == "INVALID_CURRENCY"
    assert redeem(user, "a" * 64, uuid.uuid4())["userErrors"][0]["code"] == "INVALID_CURRENCY"


def test_failure_rolls_back_all_financial_changes(database):
    user = SimpleNamespace(pk=1)
    code = "a" * 64
    key = uuid.uuid4()
    issue(database, code)
    with database.cursor() as cursor:
        cursor.execute("CREATE TRIGGER fail_credit BEFORE INSERT ON credit_transaction "
                       "BEGIN SELECT RAISE(ABORT, 'private database detail'); END")
    result = redeem(user, code, key)
    assert result["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database detail" not in str(result)
    with database.cursor() as cursor:
        cursor.execute("SELECT redeemed_at, redeemed_by FROM gift_card")
        assert cursor.fetchone() == (None, None)
        for table in ("credit_account", "credit_transaction", "credit_idempotency"):
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            assert cursor.fetchone()[0] == 0
        cursor.execute("DROP TRIGGER fail_credit")
    assert redeem(user, code, key)["credit"]["balance"] == "12.50"


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0", "1.001"])
def test_invalid_amount_is_rejected(database, amount):
    user = SimpleNamespace(pk=1)
    code = "a" * 64
    issue(database, code, amount=amount)
    assert redeem(user, code, uuid.uuid4())["userErrors"][0]["code"] == "INVALID_AMOUNT"
    with database.cursor() as cursor:
        cursor.execute("SELECT redeemed_at, redeemed_by FROM gift_card")
        assert cursor.fetchone() == (None, None)


def test_precision_overflow_is_rejected(database, monkeypatch):
    monkeypatch.setattr(credit, "MAX_BALANCE", Decimal("10.00"))
    user = SimpleNamespace(pk=1)
    code = "a" * 64
    issue(database, code, amount="12.50")
    assert redeem(user, code, uuid.uuid4())["userErrors"][0]["code"] == "INVALID_AMOUNT"
    assert execute(BALANCE_QUERY, user)["credit"]["balance"] == "0.00"


def test_redemption_fails_closed_without_postgres_row_locks(database, monkeypatch):
    monkeypatch.undo()
    with patch.object(credit, "connection", database):
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            credit._lock_clause()