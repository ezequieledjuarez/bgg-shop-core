"""Opt-in email-verification tests against a disposable local PostgreSQL DB.

EMAIL_VERIFICATION_TEST_POSTGRES_DSN must name a loopback database and role
called email_verification_test_<32 hex>; the configured application database is
never used. Run with DJANGO_SETTINGS_MODULE=email_verification_settings.
"""
import datetime
import importlib
import logging
import os
import re
import smtplib
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "email_verification_settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import modify_settings

from saleor import email_verification as ev

DSN = os.environ.get("EMAIL_VERIFICATION_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="EMAIL_VERIFICATION_TEST_POSTGRES_DSN is not set"
)

CALLER_A = "a" * 64
CALLER_B = "b" * 64
PASSWORD = "synthetic-password-1"
T0 = datetime.datetime(2030, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
MICRO = datetime.timedelta(microseconds=1)


def _validated_dsn():
    try:
        parsed = urlsplit(DSN)
        name = unquote(parsed.path.removeprefix("/"))
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        port = parsed.port
    except (AttributeError, ValueError):
        pytest.fail("Invalid isolated PostgreSQL DSN", pytrace=False)
    if (
        parsed.scheme not in ("postgres", "postgresql")
        or parsed.hostname != "127.0.0.1"
        or not port
        or not password
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/{name}"
        or not re.fullmatch(r"email_verification_test_[0-9a-f]{32}", name)
        or user != name
    ):
        pytest.fail(
            "DSN must use loopback, an explicit port, and a matching "
            "email_verification_test_<32-hex> role/database",
            pytrace=False,
        )
    return name, user, password, port


def _apply_migration(db):
    migration = importlib.import_module(
        "saleor.graphql.migrations.0021_email_verification"
    ).Migration
    sql = migration.operations[0].sql
    with db.cursor() as cursor:
        cursor.execute(sql)


@pytest.fixture
def base_database():
    name, user, password, port = _validated_dsn()
    original = connections.databases["default"]
    connections.close_all()
    if hasattr(connections._connections, "default"):
        del connections["default"]
    database_settings = {
        **original,
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name, "USER": user, "PASSWORD": password,
        "HOST": "127.0.0.1", "PORT": str(port),
        "OPTIONS": {"connect_timeout": 3},
    }
    connections.databases["default"] = database_settings
    db = connections["default"]
    schema_name = "email_verification_" + uuid.uuid4().hex
    created = False
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            assert cursor.fetchone() == (name, user)
            cursor.execute(f'CREATE SCHEMA "{schema_name}"')
            created = True
        db.close()
        database_settings["OPTIONS"] = {
            "connect_timeout": 3,
            "options": (
                f"-c search_path={schema_name},pg_catalog "
                "-c statement_timeout=10000 -c lock_timeout=10000"
            ),
        }
        with db.schema_editor() as editor:
            editor.create_model(ContentType)
            editor.create_model(Permission)
            editor.create_model(Group)
            editor.create_model(User)
        with db.cursor() as cursor:
            cursor.execute("SELECT current_schema()")
            assert cursor.fetchone()[0] == schema_name
        yield db
    finally:
        db.close()
        try:
            if created:
                database_settings["OPTIONS"] = {"connect_timeout": 3}
                with db.cursor() as cursor:
                    cursor.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
        finally:
            db.close()
            del connections["default"]
            connections.databases["default"] = original


@pytest.fixture
def database(base_database):
    _apply_migration(base_database)
    return base_database


def reg(email=None):
    email = email or f"user-{uuid.uuid4().hex}@example.test"
    return ev.register_pending(email, PASSWORD, "Synthetic", "Player")


def query(sql, params=None):
    with connections["default"].cursor() as cursor:
        cursor.execute(sql, params or [])
        return cursor.fetchall() if cursor.description else []


def state_of(user_id):
    return query("SELECT state, verified_at FROM account_email_verification WHERE user_id = %s",
                 [user_id])[0]


def challenge_of(user_id):
    return query(
        "SELECT issued_at, expires_at, consumed_at, delivery_status, token_digest "
        "FROM email_verification_challenge WHERE user_id = %s", [user_id])[0]


def at(moment):
    return patch.object(ev, "_db_now", return_value=moment)


def concurrently(*functions):
    barrier = threading.Barrier(len(functions))

    def wrap(function):
        def run():
            try:
                barrier.wait(timeout=5)
                return function()
            finally:
                connections.close_all()
        return run

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        futures = [pool.submit(wrap(f)) for f in functions]
        return [f.result(timeout=30) for f in futures]


def test_migration_backfills_legacy_accounts_without_timestamps(base_database):
    active = User.objects.create_user("legacy@example.test", "legacy@example.test", PASSWORD)
    disabled = User.objects.create_user("off@example.test", "off@example.test", PASSWORD,
                                        is_active=False)
    staff = User.objects.create_superuser("root@example.test", "root@example.test", PASSWORD)
    _apply_migration(base_database)
    for user in (active, disabled, staff):
        assert state_of(user.pk) == ("LEGACY_EXEMPT", None)
    assert ev.is_eligible(active.pk) and ev.is_eligible(staff.pk)
    assert not ev.is_eligible(disabled.pk)
    assert query("SELECT count(*) FROM email_verification_challenge") == [(0,)]


def test_migration_reverse_drops_only_feature_tables(database):
    migration = importlib.import_module(
        "saleor.graphql.migrations.0021_email_verification").Migration
    with database.cursor() as cursor:
        cursor.execute(migration.operations[0].reverse_sql)
    names = {row[0] for row in query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    assert "auth_user" in names
    assert not any(n.startswith(("account_email", "email_verification")) for n in names)


@modify_settings(INSTALLED_APPS={"append": "saleor.promotions"})
def test_migration_executor_applies_records_and_reverses_feature(base_database):
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.recorder import MigrationRecorder

    previous = ("graphql", "0020_chat_conversations")
    target = ("graphql", "0021_email_verification")
    executor = MigrationExecutor(base_database)
    executor.loader.graph.validate_consistency()
    parents = {node.key for node in executor.loader.graph.node_map[target].parents}
    assert previous in parents
    assert any(app == "auth" for app, _name in parents)
    # Auth tables are created by the isolated fixture; the pre-feature commerce
    # baseline is not provisioned here. Fake only ancestors, never migration 0021.
    executor.migrate([previous], fake=True)
    active = User.objects.create_user("graph@example.test", "graph@example.test", PASSWORD)
    disabled = User.objects.create_user(
        "graph-disabled@example.test", "graph-disabled@example.test", PASSWORD, is_active=False,
    )
    executor = MigrationExecutor(base_database)
    assert [(migration.app_label, migration.name, backwards)
            for migration, backwards in executor.migration_plan([target])] == [
                ("graphql", "0021_email_verification", False)]
    executor.migrate([target])
    assert target in MigrationRecorder(base_database).applied_migrations()
    assert state_of(active.pk) == state_of(disabled.pk) == ("LEGACY_EXEMPT", None)
    assert ev.is_eligible(active.pk) and not ev.is_eligible(disabled.pk)
    assert state_of(reg().user.pk) == ("PENDING", None)
    assert MigrationExecutor(base_database).migration_plan([target]) == []

    MigrationExecutor(base_database).migrate([previous])
    assert target not in MigrationRecorder(base_database).applied_migrations()
    assert query("SELECT to_regclass('account_email_verification')") == [(None,)]
    assert User.objects.filter(pk=active.pk).exists()


def test_registration_creates_pending_user_with_digest_only_challenge(database):
    registration = reg("Mixed.Case@Example.test")
    user = registration.user
    assert user.email == "mixed.case@example.test" and user.is_active
    assert state_of(user.pk) == ("PENDING", None)
    issued, expires, consumed, delivery, digest = challenge_of(user.pk)
    assert expires - issued == datetime.timedelta(seconds=3600) and consumed is None
    assert delivery == "UNCONFIRMED"
    assert digest == ev.token_digest(registration.token) != registration.token
    assert not ev.is_eligible(user.pk) and ev.is_pending(user.pk)
    for table in ("account_email_verification", "email_verification_challenge",
                  "email_verification_limit_bucket", "email_verification_limit_event",
                  "auth_user"):
        dump = str(query(f"SELECT t::text FROM {table} t"))
        assert registration.token not in dump


def test_duplicate_registration_is_rejected_case_insensitively(database):
    reg("dup@example.test")
    with pytest.raises(ev.DuplicateEmail):
        reg("DUP@example.TEST")
    assert query("SELECT count(*) FROM auth_user") == [(1,)]


def test_concurrent_registration_creates_exactly_one_account(database):
    def attempt():
        try:
            return reg("race@example.test")
        except ev.DuplicateEmail:
            return None

    results = concurrently(attempt, attempt, attempt)
    assert sum(r is not None for r in results) == 1
    assert query("SELECT count(*) FROM auth_user") == [(1,)]
    assert query("SELECT count(*) FROM account_email_verification") == [(1,)]
    assert query("SELECT count(*) FROM email_verification_challenge") == [(1,)]


def test_confirmation_is_single_use_and_does_not_authenticate(database):
    registration = reg()
    result = ev.confirm(registration.token, CALLER_A)
    assert result.verified
    assert state_of(registration.user.pk)[0] == "VERIFIED"
    assert state_of(registration.user.pk)[1] is not None
    assert ev.is_eligible(registration.user.pk)
    again = ev.confirm(registration.token, CALLER_A)
    assert not again.verified and not again.rate_limited


@pytest.mark.parametrize("token", [None, "", "short", "x" * 44, "!" * 43, "z" * 43,
                                   {"a": 1}, ["x" * 43], 7])
def test_unusable_tokens_are_uniformly_invalid_without_state_change(database, token):
    registration = reg()
    result = ev.confirm(token, CALLER_A)
    assert result == ev.ConfirmResult()
    assert state_of(registration.user.pk)[0] == "PENDING"


def test_expiry_boundary_uses_one_authoritative_deadline(database):
    for offset, expected in ((-MICRO, True), (datetime.timedelta(0), False), (MICRO, False)):
        registration = reg()
        expires = challenge_of(registration.user.pk)[1]
        with at(expires + offset):
            assert ev.confirm(registration.token, CALLER_A).verified is expected
        assert state_of(registration.user.pk)[0] == ("VERIFIED" if expected else "PENDING")
        query("DELETE FROM email_verification_limit_event")


def test_concurrent_confirmations_have_exactly_one_winner(database):
    registration = reg()
    results = concurrently(
        lambda: ev.confirm(registration.token, CALLER_A),
        lambda: ev.confirm(registration.token, CALLER_B),
        lambda: ev.confirm(registration.token, "c" * 64),
    )
    assert sum(r.verified for r in results) == 1
    assert state_of(registration.user.pk)[0] == "VERIFIED"
    assert query("SELECT count(*) FROM email_verification_challenge "
                 "WHERE consumed_at IS NOT NULL") == [(1,)]


def test_confirmation_cannot_verify_another_account(database):
    first, second = reg(), reg()
    assert ev.confirm(first.token, CALLER_A).verified
    assert state_of(second.user.pk)[0] == "PENDING"


def test_confirmation_versus_replacement_is_serialized(database):
    for _ in range(4):
        registration = reg()
        email = registration.user.email
        query("DELETE FROM email_verification_limit_event")
        later = challenge_of(registration.user.pk)[0] + datetime.timedelta(seconds=300)
        with at(later):
            confirm_result, resend_result = concurrently(
                lambda: ev.confirm(registration.token, CALLER_A),
                lambda: ev.resend(email, CALLER_B),
            )
            row = state_of(registration.user.pk)[0]
            if confirm_result.verified:
                # Confirmation won: the replacement must not create a new proof.
                assert row == "VERIFIED" and resend_result.issued is None
            else:
                # Replacement won: the old proof is dead and the new one works.
                assert row == "PENDING" and resend_result.issued is not None
                assert ev.confirm(registration.token, CALLER_A).verified is False
                assert ev.confirm(resend_result.issued.token, CALLER_B).verified
                assert state_of(registration.user.pk)[0] == "VERIFIED"


def test_parallel_resends_issue_one_effective_token(database):
    registration = reg()
    email = registration.user.email
    later = challenge_of(registration.user.pk)[0] + datetime.timedelta(seconds=300)
    with at(later):
        results = concurrently(
            lambda: ev.resend(email, CALLER_A), lambda: ev.resend(email, CALLER_B),
            lambda: ev.resend(email, "c" * 64),
        )
    issued = [r.issued for r in results if r.issued]
    assert len(issued) == 1
    assert query("SELECT count(*) FROM email_verification_limit_event "
                 "WHERE scope = 'RESEND_RECIPIENT'") == [(1,)]
    assert challenge_of(registration.user.pk)[4] == ev.token_digest(issued[0].token)


def test_resend_cooldown_boundary_is_inclusive_at_120_seconds(database):
    registration = reg()
    email = registration.user.email
    issued_at = challenge_of(registration.user.pk)[0]
    cooldown = datetime.timedelta(seconds=120)
    with at(issued_at + cooldown - MICRO):
        assert ev.resend(email, CALLER_A).issued is None
    with at(issued_at + cooldown):
        assert ev.resend(email, CALLER_A).issued is not None
    assert query("SELECT count(*) FROM email_verification_limit_event "
                 "WHERE scope = 'RESEND_RECIPIENT'") == [(1,)]


def test_initial_issue_starts_cooldown_but_is_not_a_resend(database):
    registration = reg()
    assert query("SELECT count(*) FROM email_verification_limit_event") == [(0,)]
    issued_at = challenge_of(registration.user.pk)[0]
    with at(issued_at + datetime.timedelta(seconds=1)):
        assert ev.resend(registration.user.email, CALLER_A).issued is None


def test_recipient_quota_is_two_per_rolling_hour(database):
    registration = reg()
    email = registration.user.email
    base = challenge_of(registration.user.pk)[0]
    step = datetime.timedelta(seconds=120)
    first = base + step
    with at(first):
        assert ev.resend(email, CALLER_A).issued is not None
    with at(first + step):
        assert ev.resend(email, CALLER_A).issued is not None
    window = datetime.timedelta(seconds=3600)
    with at(first + window - MICRO):
        assert ev.resend(email, CALLER_A).issued is None
    with at(first + window):
        assert ev.resend(email, CALLER_A).issued is not None


def test_recipient_exhaustion_is_neutral_not_rate_limited(database):
    registration = reg()
    email = registration.user.email
    base = challenge_of(registration.user.pk)[0]
    for index in (1, 2):
        with at(base + datetime.timedelta(seconds=120 * index)):
            ev.resend(email, CALLER_A)
    with at(base + datetime.timedelta(seconds=400)):
        result = ev.resend(email, CALLER_A)
    assert result == ev.ResendResult()


def test_caller_resend_quota_is_ten_per_rolling_hour_with_ceiling_retry(database):
    start = T0
    for index in range(10):
        with at(start + datetime.timedelta(seconds=index)):
            assert ev.resend(f"nobody{index}@example.test", CALLER_A) == ev.ResendResult()
    with at(start + datetime.timedelta(seconds=10, milliseconds=500)):
        limited = ev.resend("nobody@example.test", CALLER_A)
    assert limited.rate_limited and limited.retry_after == 3590
    with at(start + datetime.timedelta(seconds=3600) - MICRO):
        assert ev.resend("nobody@example.test", CALLER_A).rate_limited
    with at(start + datetime.timedelta(seconds=3600)):
        assert not ev.resend("nobody@example.test", CALLER_A).rate_limited
    with at(start + datetime.timedelta(seconds=11)):
        assert not ev.resend("other@example.test", CALLER_B).rate_limited


def test_invalid_resend_email_is_validated_before_counting(database):
    assert ev.resend("not-an-email", CALLER_A).invalid
    assert query("SELECT count(*) FROM email_verification_limit_event") == [(0,)]


@pytest.mark.parametrize("variant", ["pending", "verified", "exempt", "absent", "inactive"])
def test_resend_sends_only_for_eligible_pending_accounts(database, variant):
    registration = reg()
    email = registration.user.email
    if variant == "verified":
        assert ev.confirm(registration.token, CALLER_A).verified
    elif variant == "exempt":
        query("UPDATE account_email_verification SET state='LEGACY_EXEMPT', verified_at=NULL")
    elif variant == "absent":
        email = "absent@example.test"
    elif variant == "inactive":
        User.objects.filter(pk=registration.user.pk).update(is_active=False)
    later = challenge_of(registration.user.pk)[0] + datetime.timedelta(seconds=500)
    with at(later):
        result = ev.resend(email, CALLER_B)
    assert (result.issued is not None) is (variant == "pending")
    assert not result.rate_limited and not result.invalid


def test_invalid_confirmation_limit_is_ten_per_caller_per_900_seconds(database):
    registration = reg()
    for index in range(10):
        with at(T0 + datetime.timedelta(seconds=index)):
            assert ev.confirm("x" * 43, CALLER_A) == ev.ConfirmResult()
    with at(T0 + datetime.timedelta(seconds=10, milliseconds=1)):
        limited = ev.confirm("x" * 43, CALLER_A)
        assert limited.rate_limited and limited.retry_after == 890
    # An exhausted caller never consumes a valid proof; another caller still can.
    with patch.object(ev, "_db_now", return_value=T0 + datetime.timedelta(seconds=11)):
        query("UPDATE email_verification_challenge SET issued_at = %s, expires_at = %s",
              [T0, T0 + datetime.timedelta(seconds=3600)])
        assert ev.confirm(registration.token, CALLER_A).rate_limited
        assert state_of(registration.user.pk)[0] == "PENDING"
        assert ev.confirm(registration.token, CALLER_B).verified
    registration = reg()
    query("UPDATE email_verification_challenge SET issued_at = %s, expires_at = %s "
          "WHERE user_id = %s", [T0, T0 + datetime.timedelta(seconds=3600), registration.user.pk])
    with at(T0 + datetime.timedelta(seconds=900) - MICRO):
        assert ev.confirm(registration.token, CALLER_A).rate_limited
    with at(T0 + datetime.timedelta(seconds=900)):
        assert ev.confirm(registration.token, CALLER_A).verified


def test_bff_malformed_confirmations_use_authoritative_core_quota(database):
    """Run the real BFF adapter through GraphQL against the isolated PostgreSQL store."""
    proxy_path = Path(__file__).resolve().parents[2] / "bgg-shop-bff/src/proxy.py"
    spec = importlib.util.spec_from_file_location("verification_test_bff_proxy", proxy_path)
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    from django.conf import settings
    from saleor.graphql.api import schema

    def upstream(_url, *, json, headers, **_kwargs):
        assert json["variables"]["input"]["token"] == ""
        result = schema.execute(
            json["query"], variable_values=json["variables"],
            context_value=SimpleNamespace(headers=headers),
        )
        assert result.errors is None
        return SimpleNamespace(status_code=200, json=lambda: {"data": result.data})

    malformed = [None, 7, [], {}, "short", "A" * 100000, "!" * 43, "", True, "A" * 44]
    with patch.dict(os.environ, {"OPKEY_INTERNAL_SECRET": settings.OPKEY_INTERNAL_SECRET}), \
            patch.object(proxy.requests, "post", side_effect=upstream), at(T0):
        registration = reg()
        for token in malformed:
            with pytest.raises(proxy.VerificationError) as error:
                proxy.confirm_email_verification(token, CALLER_A)
            assert error.value.code == "INVALID_VERIFICATION_TOKEN"
        assert query("SELECT count(*) FROM email_verification_limit_event "
                     "WHERE scope = %s AND subject_digest = %s",
                     [ev.SCOPE_INVALID_CONFIRM, CALLER_A]) == [(10,)]
        with pytest.raises(proxy.VerificationError) as error:
            proxy.confirm_email_verification(None, CALLER_A)
        assert error.value.code == "RATE_LIMITED"
        assert error.value.retry_after == 900
        assert ev.confirm(registration.token, CALLER_A).rate_limited
        assert state_of(registration.user.pk) == ("PENDING", None)
        assert query("SELECT count(*) FROM email_verification_limit_event") == [(10,)]
        assert not ev.confirm("", CALLER_B).rate_limited
    with at(T0 + datetime.timedelta(seconds=900) - MICRO):
        assert ev.confirm(registration.token, CALLER_A).rate_limited
    with at(T0 + datetime.timedelta(seconds=900)):
        assert ev.confirm(registration.token, CALLER_A).verified


def test_disabled_and_legacy_accounts(database):
    registration = reg()
    User.objects.filter(pk=registration.user.pk).update(is_active=False)
    assert not ev.confirm(registration.token, CALLER_A).verified
    assert state_of(registration.user.pk)[0] == "PENDING"
    assert not User.objects.get(pk=registration.user.pk).is_active
    legacy = User.objects.create_user("l@example.test", "l@example.test", PASSWORD)
    assert not ev.is_eligible(legacy.pk)  # no sidecar row: fail closed
    query("INSERT INTO account_email_verification (user_id, state) VALUES (%s, 'LEGACY_EXEMPT')",
          [legacy.pk])
    assert ev.is_eligible(legacy.pk)
    User.objects.filter(pk=legacy.pk).update(is_active=False)
    assert not ev.is_eligible(legacy.pk)
    assert not ev.is_eligible("not-a-number") and not ev.is_eligible(None)


def test_eligibility_fails_closed_when_storage_is_unavailable(database):
    registration = reg()
    ev.confirm(registration.token, CALLER_A)
    query("DROP TABLE account_email_verification")
    assert ev.is_eligible(registration.user.pk) is False
    assert ev.is_pending(registration.user.pk) is False


def test_delivery_failure_preserves_pending_and_leaks_nothing(database, caplog):
    registration = reg()
    caplog.set_level(logging.DEBUG)
    with patch.object(ev, "send_mail", side_effect=smtplib.SMTPServerDisconnected("detail")):
        status = ev.deliver(registration.user.pk, registration.user.email, registration.token)
    assert status == ev.DELIVERY_UNCONFIRMED
    assert challenge_of(registration.user.pk)[3] == "FAILED"
    assert state_of(registration.user.pk)[0] == "PENDING"
    for forbidden in (registration.token, registration.user.email, "detail"):
        assert forbidden not in caplog.text
    # The proof is still usable: failed delivery never restores or rotates tokens.
    assert ev.confirm(registration.token, CALLER_A).verified


def test_delivery_success_is_recorded_only_for_the_current_proof(database):
    registration = reg()
    later = challenge_of(registration.user.pk)[0] + datetime.timedelta(seconds=300)
    with at(later):
        replacement = ev.resend(registration.user.email, CALLER_A).issued
    ev.deliver(registration.user.pk, registration.user.email, registration.token)
    assert challenge_of(registration.user.pk)[3] == "UNCONFIRMED"
    ev.deliver(registration.user.pk, registration.user.email, replacement.token)
    assert challenge_of(registration.user.pk)[3] == "SUBMITTED"


def test_database_constraints_reject_inconsistent_rows(database):
    registration = reg()
    from django.db import IntegrityError, transaction
    with pytest.raises(IntegrityError), transaction.atomic():
        query("UPDATE email_verification_challenge SET expires_at = expires_at + interval '1 second'")
    with pytest.raises(IntegrityError), transaction.atomic():
        query("UPDATE account_email_verification SET state = 'VERIFIED' WHERE user_id = %s",
              [registration.user.pk])
