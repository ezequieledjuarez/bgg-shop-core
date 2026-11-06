"""Opt-in user-permission integration tests for a disposable local PostgreSQL DB."""

import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from django.conf import settings
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connections, transaction
from django.db import close_old_connections
from django.db.utils import OperationalError

from saleor.graphql import api
from saleor.graphql.api import schema


DSN = os.environ.get("USER_PERMISSIONS_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="USER_PERMISSIONS_TEST_POSTGRES_DSN is not set"
)

USER_FIELDS = "id email firstName lastName isActive isStaff isSuperuser"
ADMIN_USERS = f"""
query AdminUsers {{
  adminUsers(limit: 100) {{
    items {{ {USER_FIELDS} }}
    nextCursor
  }}
}}
"""
ADMIN_USERS_WITH_INPUTS = f"""
query AdminUsersWithInputs(
  $search: String, $cursor: String, $limit: Int
) {{
  adminUsers(search: $search, cursor: $cursor, limit: $limit) {{
    items {{ {USER_FIELDS} }}
    nextCursor
  }}
}}
"""
ADMIN_USERS_DEFAULT_LIMIT = f"""
query AdminUsersDefaultLimit {{
  adminUsers {{
    items {{ {USER_FIELDS} }}
    nextCursor
  }}
}}
"""
ADMIN_UPDATE_USER = f"""
mutation UpdateUserPrivilege(
  $userId: ID!, $privilege: AdminUserPrivilege!, $enabled: Boolean!
) {{
  adminUpdateUserPrivilege(
    userId: $userId, privilege: $privilege, enabled: $enabled
  ) {{
    user {{ {USER_FIELDS} }}
    userErrors {{ code field message }}
  }}
}}
"""
REGISTER = """
mutation Register($input: RegisterInput!) {
  register(input: $input) {
    user { id email isStaff isSuperuser }
    errors { code }
  }
}
"""


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
        or not re.fullmatch(r"user_permissions_test_[0-9a-f]{32}", name)
        or user != name
    ):
        pytest.fail(
            "DSN must use loopback, an explicit port, and a matching "
            "user_permissions_test_<32-hex> role/database",
            pytrace=False,
        )
    return name, user, password, port


@pytest.fixture
def database():
    # Validate before touching a connection; a present bad DSN is never skipped
    # or replaced by Django's configured/default database.
    name, user, password, port = _validated_dsn()
    original = connections.databases["default"]
    connections.close_all()
    if hasattr(connections._connections, "default"):
        del connections["default"]

    database_settings = {
        **original,
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": user,
        "PASSWORD": password,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "OPTIONS": {"connect_timeout": 3},
    }
    connections.databases["default"] = database_settings
    db = connections["default"]
    schema_name = "user_permissions_" + uuid.uuid4().hex
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


def _new_user(*, is_active=True, is_staff=False, is_superuser=False):
    unique = uuid.uuid4().hex
    email = f"user-{unique}@example.test"
    return User.objects.create(
        username=email,
        email=email,
        password="not-a-valid-login-hash",
        first_name="Synthetic",
        last_name=unique[:8],
        is_active=is_active,
        is_staff=is_staff,
        is_superuser=is_superuser,
    )


def _bearer_context(user):
    import jwt

    token = jwt.encode(
        {"sub": str(user.pk), "token_type": "access"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    return SimpleNamespace(headers={"Authorization": f"Bearer {token}"}), token


def _context(token=None, *, opkey_user=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(
        headers=headers,
        opkey_delegated=opkey_user is not None,
        user=opkey_user,
    )


def _execute(query, *, variables=None, context=None):
    return schema.execute(
        query,
        variable_values=variables,
        context_value=context or SimpleNamespace(headers={}),
    )


def _error_codes(result):
    codes = set()
    for error in result.errors or ():
        extensions = getattr(error, "extensions", None) or {}
        if extensions.get("code"):
            codes.add(extensions["code"])
    return codes


def _assert_sanitized_public_messages(messages):
    assert messages
    forbidden_details = re.compile(
        r"traceback|[a-z][a-z0-9+.-]*://|credential|password|secret|"
        r"\bdsn\b|\b(?:dbname|host|user)=",
        re.IGNORECASE,
    )
    for message in messages:
        assert not forbidden_details.search(message), (
            "public error exposed internal or sensitive details"
        )


def _assert_query_denied(result, expected_code):
    assert result.errors, "unauthorized query unexpectedly succeeded"
    assert expected_code in _error_codes(result)
    assert result.data is None


def _update(user_id, privilege, enabled, context):
    return _execute(
        ADMIN_UPDATE_USER,
        variables={
            "userId": str(user_id),
            "privilege": privilege,
            "enabled": enabled,
        },
        context=context,
    )


def _update_payload(result):
    assert result.errors is None, result.errors
    assert result.data is not None
    return result.data["adminUpdateUserPrivilege"]


def _assert_update_denied(result, expected_code):
    if result.errors:
        assert expected_code in _error_codes(result)
        assert result.data is None
        return
    payload = result.data["adminUpdateUserPrivilege"]
    assert payload["user"] is None
    assert expected_code in {error["code"] for error in payload["userErrors"]}


def test_user_list_requires_active_superuser_not_staff(database):
    superadmin = _new_user(is_active=True, is_superuser=True)
    staff = _new_user(is_active=True, is_staff=True)
    inactive_superadmin = _new_user(is_active=False, is_superuser=True)
    target = _new_user()
    superadmin_context, token = _bearer_context(superadmin)

    listed = _execute(ADMIN_USERS, context=superadmin_context)
    assert listed.errors is None, listed.errors
    listed_emails = {item["email"] for item in listed.data["adminUsers"]["items"]}
    assert target.email in listed_emails
    assert inactive_superadmin.email in listed_emails

    introspection = _execute(
        '{ __type(name: "User") { fields { name } } }',
        context=_context(token),
    )
    assert introspection.errors is None, introspection.errors
    assert {
        field["name"] for field in introspection.data["__type"]["fields"]
    } == {
        "id",
        "email",
        "firstName",
        "lastName",
        "isActive",
        "isStaff",
        "isSuperuser",
    }
    target_projection = next(
        item for item in listed.data["adminUsers"]["items"] if item["id"] == str(target.pk)
    )
    assert set(target_projection) == {
        "id",
        "email",
        "firstName",
        "lastName",
        "isActive",
        "isStaff",
        "isSuperuser",
    }
    assert "password" not in repr(listed.data).lower()

    staff_context, _ = _bearer_context(staff)
    inactive_context, _ = _bearer_context(inactive_superadmin)
    for context, code in (
        (staff_context, "FORBIDDEN"),
        (inactive_context, "FORBIDDEN"),
        (_context(), "UNAUTHENTICATED"),
    ):
        denied_list = _execute(ADMIN_USERS, context=context)
        _assert_query_denied(denied_list, code)
        denied_update = _update(target.pk, "is_staff", True, context)
        _assert_update_denied(denied_update, code)
        target.refresh_from_db()
        assert target.is_staff is False


def test_user_list_search_is_case_insensitive_substring_across_user_fields(database):
    actor = _new_user(is_active=True, is_superuser=True)
    email_user = _new_user()
    email_user.email = f"synthetic-MiXeD-{uuid.uuid4().hex}@example.test"
    email_user.username = email_user.email
    email_user.save(update_fields=["email", "username"])
    first_name_user = _new_user()
    first_name_user.first_name = "PhoEnIx-Name"
    first_name_user.save(update_fields=["first_name"])
    last_name_user = _new_user()
    last_name_user.last_name = "RiVeR-Name"
    last_name_user.save(update_fields=["last_name"])
    context, _ = _bearer_context(actor)

    for search, expected_user in (
        ("mIxEd", email_user),
        ("pHoEnIx", first_name_user),
        ("rIvEr", last_name_user),
    ):
        result = _execute(
            ADMIN_USERS_WITH_INPUTS,
            variables={"search": search, "limit": 100},
            context=context,
        )

        assert result.errors is None, result.errors
        items = result.data["adminUsers"]["items"]
        assert [item["id"] for item in items] == [str(expected_user.pk)]


def test_user_list_cursor_paginates_ascending_ids_with_default_and_max_limits(
    database,
):
    actor = _new_user(is_active=True, is_superuser=True)
    users = [_new_user() for _ in range(102)]
    expected_ids = [
        str(user.pk) for user in sorted([actor, *users], key=lambda user: user.pk)
    ]
    context, _ = _bearer_context(actor)

    first_page = _execute(ADMIN_USERS_DEFAULT_LIMIT, context=context)
    assert first_page.errors is None, first_page.errors
    first_data = first_page.data["adminUsers"]
    first_ids = [item["id"] for item in first_data["items"]]
    assert first_ids == expected_ids[:50]
    assert first_data["nextCursor"]

    second_page = _execute(
        ADMIN_USERS_WITH_INPUTS,
        variables={"cursor": first_data["nextCursor"], "limit": 50},
        context=context,
    )
    assert second_page.errors is None, second_page.errors
    second_data = second_page.data["adminUsers"]
    second_ids = [item["id"] for item in second_data["items"]]
    assert second_ids == expected_ids[50:100]
    assert second_data["nextCursor"]

    final_page = _execute(
        ADMIN_USERS_WITH_INPUTS,
        variables={"cursor": second_data["nextCursor"], "limit": 100},
        context=context,
    )
    assert final_page.errors is None, final_page.errors
    final_data = final_page.data["adminUsers"]
    final_ids = [item["id"] for item in final_data["items"]]
    assert final_ids == expected_ids[100:]
    assert final_data["nextCursor"] is None
    assert first_ids + second_ids + final_ids == expected_ids

    max_limit_page = _execute(
        ADMIN_USERS_WITH_INPUTS,
        variables={"limit": 100},
        context=context,
    )
    assert max_limit_page.errors is None, max_limit_page.errors
    max_limit_data = max_limit_page.data["adminUsers"]
    assert [item["id"] for item in max_limit_data["items"]] == expected_ids[:100]
    assert max_limit_data["nextCursor"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("search", ""),
        ("search", "x" * 201),
        ("limit", 0),
        ("limit", 101),
        ("cursor", "not-a-signed-cursor"),
        ("cursor", "x" * 513),
    ],
)
def test_user_list_invalid_search_limit_and_cursor_include_invalid_field(
    database, field, value
):
    actor = _new_user(is_active=True, is_superuser=True)
    context, _ = _bearer_context(actor)

    result = _execute(
        ADMIN_USERS_WITH_INPUTS,
        variables={field: value},
        context=context,
    )

    assert result.errors
    invalid_errors = [
        error
        for error in result.errors
        if (getattr(error, "extensions", None) or {}).get("code") == "INVALID"
    ]
    assert invalid_errors
    assert any(
        (getattr(error, "extensions", None) or {}).get("field") == field
        for error in invalid_errors
    )


def test_user_list_and_last_superuser_errors_are_sanitized(database):
    unauthorized = _execute(ADMIN_USERS_DEFAULT_LIMIT, context=_context())
    _assert_query_denied(unauthorized, "UNAUTHENTICATED")
    _assert_sanitized_public_messages(
        [error.message for error in unauthorized.errors]
    )

    staff = _new_user(is_active=True, is_staff=True)
    staff_context, _ = _bearer_context(staff)
    forbidden = _execute(ADMIN_USERS_DEFAULT_LIMIT, context=staff_context)
    _assert_query_denied(forbidden, "FORBIDDEN")
    _assert_sanitized_public_messages([error.message for error in forbidden.errors])

    only_superadmin = _new_user(is_active=True, is_superuser=True)
    context, _ = _bearer_context(only_superadmin)
    denied_update = _update(only_superadmin.pk, "is_superuser", False, context)
    _assert_update_denied(denied_update, "LAST_ACTIVE_SUPERUSER")
    if denied_update.errors:
        public_messages = [error.message for error in denied_update.errors]
    else:
        payload = denied_update.data["adminUpdateUserPrivilege"]
        public_messages = [error["message"] for error in payload["userErrors"]]
    _assert_sanitized_public_messages(public_messages)
    only_superadmin.refresh_from_db()
    assert only_superadmin.is_superuser is True


def test_operation_key_only_cannot_list_or_update_users(database):
    delegated_superadmin = _new_user(is_active=True, is_superuser=True)
    target = _new_user()
    delegated_context = _context(opkey_user=delegated_superadmin)

    listed = _execute(ADMIN_USERS, context=delegated_context)
    _assert_query_denied(listed, "FORBIDDEN")
    updated = _update(target.pk, "is_staff", True, delegated_context)
    _assert_update_denied(updated, "FORBIDDEN")
    target.refresh_from_db()
    assert target.is_staff is False


_DATABASE_ERROR_MARKER = "SYNTHETIC_T012_INTERNAL_DB_FAILURE"
_DATABASE_ERROR_DETAIL = (
    f"{_DATABASE_ERROR_MARKER}: Traceback (synthetic only); "
    "https://internal.example.test/database; "
    "password=synthetic-password secret=synthetic-secret "
    "DSN=postgresql://synthetic:synthetic-password@internal.example.test/core"
)


def _assert_database_error_is_public_safe(result, *, mutation=False):
    if result.errors:
        # Both selected root fields are non-null: preserve GraphQL error bubbling.
        assert result.data is None
        errors = [
            {"message": error.message, "extensions": error.extensions or {}}
            for error in result.errors
        ]
        codes = [error["extensions"].get("code") for error in errors]
    else:
        assert mutation, "a failed listing must not masquerade as an empty page"
        payload = _update_payload(result)
        assert payload["user"] is None
        errors = payload["userErrors"]
        codes = [error["code"] for error in errors]

    assert errors, "database failure must have an explicit public error result"
    public_result = json.dumps({"data": result.data, "errors": errors})
    for internal_detail in (
        _DATABASE_ERROR_MARKER,
        "internal.example.test",
        "synthetic-password",
        "synthetic-secret",
    ):
        assert internal_detail not in public_result, (
            "public error exposed synthetic internal database details"
        )
    _assert_sanitized_public_messages([public_result])
    assert all(error["message"] for error in errors)
    # The approved contract does not prescribe a database-failure code, but an
    # unclassified GraphQL exception is not an explicit, safe error result.
    assert codes and all(isinstance(code, str) and code for code in codes)


def test_user_list_operational_error_is_sanitized(database):
    actor = _new_user(is_active=True, is_superuser=True)
    context, _ = _bearer_context(actor)
    injected = []

    def fail_listing(execute, sql, params, many, execution_context):
        assert execution_context["connection"] is database
        # Bearer authorization uses a PK lookup; fail the ordered list SELECT,
        # not authentication, and do not replace the list/error contract.
        if (
            sql.lstrip().upper().startswith("SELECT")
            and '"auth_user"' in sql
            and "ORDER BY" in sql.upper()
        ):
            injected.append(True)
            raise OperationalError(_DATABASE_ERROR_DETAIL)
        return execute(sql, params, many, execution_context)

    with database.execute_wrapper(fail_listing):
        result = _execute(ADMIN_USERS_DEFAULT_LIMIT, context=context)

    assert injected == [True], "the user-list database path was not exercised"
    _assert_database_error_is_public_safe(result)


def test_privilege_update_operational_error_is_sanitized(database):
    actor = _new_user(is_active=True, is_superuser=True)
    target = _new_user()
    context, _ = _bearer_context(actor)
    injected = []

    def fail_update(execute, sql, params, many, execution_context):
        assert execution_context["connection"] is database
        if sql.lstrip().upper().startswith('UPDATE "AUTH_USER"'):
            injected.append(True)
            raise OperationalError(_DATABASE_ERROR_DETAIL)
        return execute(sql, params, many, execution_context)

    with database.execute_wrapper(fail_update):
        result = _update(target.pk, "is_staff", True, context)

    assert injected == [True], "the privilege-write database path was not exercised"
    target.refresh_from_db()
    assert target.is_staff is False
    assert target.is_superuser is False
    _assert_database_error_is_public_safe(result, mutation=True)


@pytest.mark.parametrize(
    ("privilege", "enabled", "initial_staff", "initial_superuser", "expected_staff", "expected_superuser"),
    [
        ("is_staff", True, False, False, True, False),
        ("is_staff", False, True, True, False, True),
        ("is_superuser", True, False, False, False, True),
        ("is_superuser", False, True, True, True, False),
    ],
)
def test_privilege_update_changes_only_requested_flag(
    database,
    privilege,
    enabled,
    initial_staff,
    initial_superuser,
    expected_staff,
    expected_superuser,
):
    actor = _new_user(is_active=True, is_superuser=True)
    target = _new_user(
        is_active=True,
        is_staff=initial_staff,
        is_superuser=initial_superuser,
    )
    context, _ = _bearer_context(actor)
    original_password = target.password
    original_email = target.email

    result = _update(target.pk, privilege, enabled, context)

    payload = _update_payload(result)
    assert payload["userErrors"] == []
    assert payload["user"]["isStaff"] is expected_staff
    assert payload["user"]["isSuperuser"] is expected_superuser
    target.refresh_from_db()
    assert target.is_staff is expected_staff
    assert target.is_superuser is expected_superuser
    assert target.password == original_password
    assert target.email == original_email


def test_superuser_without_staff_can_list_and_update_privileges(database):
    actor = _new_user(is_active=True, is_staff=False, is_superuser=True)
    target = _new_user()
    context, _ = _bearer_context(actor)

    listed = _execute(ADMIN_USERS, context=context)
    assert listed.errors is None, listed.errors
    assert target.email in {item["email"] for item in listed.data["adminUsers"]["items"]}

    result = _update(target.pk, "is_superuser", True, context)
    payload = _update_payload(result)
    assert payload["userErrors"] == []
    assert payload["user"]["isStaff"] is False
    assert payload["user"]["isSuperuser"] is True
    target.refresh_from_db()
    assert target.is_staff is False
    assert target.is_superuser is True


def test_last_active_superuser_revocation_is_denied_without_partial_update(database):
    only_superadmin = _new_user(is_active=True, is_staff=True, is_superuser=True)
    context, _ = _bearer_context(only_superadmin)

    result = _update(only_superadmin.pk, "is_superuser", False, context)

    _assert_update_denied(result, "LAST_ACTIVE_SUPERUSER")
    only_superadmin.refresh_from_db()
    assert only_superadmin.is_active is True
    assert only_superadmin.is_staff is True
    assert only_superadmin.is_superuser is True


def test_last_superuser_self_demotion_requires_another_active_superuser(database):
    actor = _new_user(is_active=True, is_staff=False, is_superuser=True)
    other_superadmin = _new_user(is_active=True, is_superuser=True)
    context, _ = _bearer_context(actor)

    result = _update(actor.pk, "is_superuser", False, context)

    payload = _update_payload(result)
    assert payload["userErrors"] == []
    assert payload["user"]["isSuperuser"] is False
    actor.refresh_from_db()
    other_superadmin.refresh_from_db()
    assert actor.is_superuser is False
    assert actor.is_staff is False
    assert other_superadmin.is_active is True
    assert other_superadmin.is_superuser is True


def test_concurrent_superuser_revocations_preserve_one_active_superuser(database):
    actor_a = _new_user(is_active=True, is_superuser=True)
    actor_b = _new_user(is_active=True, is_staff=True, is_superuser=True)
    _, token_a = _bearer_context(actor_a)
    _, token_b = _bearer_context(actor_b)
    start = threading.Barrier(2)
    authenticated = threading.Barrier(2)
    auth_local = threading.local()
    backend_pids = []
    pids_lock = threading.Lock()
    real_authenticated_user = api._authenticated_user

    def synchronize_after_auth(info):
        user = real_authenticated_user(info)
        if not getattr(auth_local, "waited", False):
            auth_local.waited = True
            assert user is not None
            authenticated.wait(timeout=10)
        return user

    def revoke_self(token):
        close_old_connections()
        db = connections["default"]
        try:
            with db.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pid = cursor.fetchone()[0]
            with pids_lock:
                backend_pids.append(backend_pid)
            start.wait(timeout=10)
            return _update(
                actor_a.pk if token == token_a else actor_b.pk,
                "is_superuser",
                False,
                _context(token),
            )
        finally:
            db.close()

    with patch.object(api, "_authenticated_user", side_effect=synchronize_after_auth):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(revoke_self, (token_a, token_b)))

    assert len(set(backend_pids)) == 2
    assert all(result.errors is None for result in results), [
        result.errors for result in results
    ]
    payloads = [result.data["adminUpdateUserPrivilege"] for result in results]
    successes = [payload for payload in payloads if payload["user"] is not None]
    denials = [payload for payload in payloads if payload["user"] is None]
    assert len(successes) == 1
    assert successes[0]["userErrors"] == []
    assert len(denials) == 1
    assert "LAST_ACTIVE_SUPERUSER" in {
        error["code"] for error in denials[0]["userErrors"]
    }
    actor_a.refresh_from_db()
    actor_b.refresh_from_db()
    assert sum(
        user.is_active and user.is_superuser for user in (actor_a, actor_b)
    ) == 1


def test_promotion_then_concurrent_actor_revocation_refreshes_qualifying_set(database):
    actor = _new_user(is_active=True, is_superuser=True)
    target = _new_user(is_active=True, is_superuser=False)
    _, token = _bearer_context(actor)
    with database.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid(), current_schema()")
        blocker_pid, fixture_schema = cursor.fetchone()

    pids = {"blocker": blocker_pid}
    ready = {name: threading.Event() for name in ("promotion", "revocation")}
    poll_interval = threading.Event()

    def update_on_backend(name, user_id, enabled):
        close_old_connections()
        db = connections["default"]
        try:
            with db.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid(), current_schema()")
                backend_pid, current_schema = cursor.fetchone()
                assert current_schema == fixture_schema
                assert db.settings_dict["NAME"] == database.settings_dict["NAME"]
                # Bound both SQL execution and lock acquisition, including cleanup
                # if a coordinating assertion fails before a blocker is released.
                cursor.execute("SET statement_timeout = 10000")
                cursor.execute("SET lock_timeout = 10000")
            pids[name] = backend_pid
            ready[name].set()
            return _update(user_id, "is_superuser", enabled, _context(token))
        finally:
            db.close()

    def wait_until_blocked(waiter, blocker, future):
        assert ready[waiter].wait(timeout=3), "worker backend was not ready"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if future.done():
                future.result(timeout=0)  # Surface worker setup failures directly.
                pytest.fail(f"{waiter} finished before the required blocked phase")
            with database.cursor() as cursor:
                # The observer also holds the target-row transaction. Clear its
                # cached statistics snapshot before each lock-state observation.
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(
                    "SELECT pg_blocking_pids(pid), wait_event_type, query "
                    "FROM pg_stat_activity WHERE pid = %s",
                    [pids[waiter]],
                )
                row = cursor.fetchone()
            if row and pids[blocker] in row[0] and row[1] == "Lock":
                return row[2]
            # Throttle polling, never use elapsed time as proof of the race phase.
            poll_interval.wait(timeout=0.01)
        pytest.fail(f"{waiter} did not block on {blocker} within the deadline")

    with ThreadPoolExecutor(max_workers=2) as executor:
        # This is the only deliberately held row lock, on the fixture-schema
        # target, not a catalog, shared table, or application-default database.
        with transaction.atomic(using=database.alias):
            User.objects.select_for_update().get(pk=target.pk)
            promotion = executor.submit(
                update_on_backend, "promotion", target.pk, True
            )
            blocked_query = wait_until_blocked("promotion", "blocker", promotion)
            # In the current implementation the target SELECT follows complete
            # materialization/locking of the old active-superuser set. Observing
            # this statement blocked proves promotion has passed that first step.
            assert "FOR UPDATE" in blocked_query.upper()
            target_predicate = blocked_query.split(" WHERE ", 1)[1]
            assert '"auth_user"."id" =' in target_predicate
            assert "is_superuser" not in target_predicate

            revocation = executor.submit(
                update_on_backend, "revocation", actor.pk, False
            )
            wait_until_blocked("revocation", "promotion", revocation)
            assert len(set(pids.values())) == 3
        # Release only after revocation is demonstrably waiting on promotion.
        promoted = promotion.result(timeout=15)
        revoked = revocation.result(timeout=15)

    promotion_payload = _update_payload(promoted)
    assert promotion_payload["userErrors"] == []
    assert promotion_payload["user"]["id"] == str(target.pk)
    assert promotion_payload["user"]["isSuperuser"] is True
    revocation_payload = _update_payload(revoked)
    assert revocation_payload["userErrors"] == [], (
        "revocation must see the newly committed active Superadmin, "
        "not incorrectly return LAST_ACTIVE_SUPERUSER from a stale qualifying set"
    )
    assert revocation_payload["user"]["id"] == str(actor.pk)
    assert revocation_payload["user"]["isSuperuser"] is False
    actor.refresh_from_db()
    target.refresh_from_db()
    assert actor.is_active is True and actor.is_superuser is False
    assert target.is_active is True and target.is_superuser is True
    assert User.objects.filter(is_active=True, is_superuser=True).count() == 1


def test_existing_bearer_token_uses_updated_privileges_on_next_request(database):
    actor = _new_user(is_active=True, is_superuser=True)
    remaining_superadmin = _new_user(is_active=True, is_superuser=True)
    context, token = _bearer_context(actor)

    updated = _update(actor.pk, "is_superuser", False, context)
    assert _update_payload(updated)["user"]["isSuperuser"] is False

    next_request = _execute(ADMIN_USERS, context=_context(token))
    _assert_query_denied(next_request, "FORBIDDEN")
    actor.refresh_from_db()
    remaining_superadmin.refresh_from_db()
    assert actor.is_superuser is False
    assert remaining_superadmin.is_active is True
    assert remaining_superadmin.is_superuser is True


def test_registration_does_not_assign_staff_or_superuser(database):
    email = f"buyer-{uuid.uuid4().hex}@example.test"
    result = _execute(
        REGISTER,
        variables={
            "input": {
                "email": email,
                "password": "Synthetic-test-password-2026",
                "firstName": "New",
                "lastName": "Buyer",
            }
        },
    )

    assert result.errors is None, result.errors
    registration = result.data["register"]
    assert registration["errors"] == []
    assert registration["user"]["isStaff"] is False
    assert registration["user"]["isSuperuser"] is False
    user = User.objects.get(email=email)
    assert user.is_staff is False
    assert user.is_superuser is False
