"""CU-C03 createReview: no live DB; cursor/connection and auth are mocked."""
import datetime
import importlib
import os
import re
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlsplit
import uuid

import django
import jwt
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import RequestFactory

from saleor.graphql import api
from saleor.graphql.api import schema

MUTATION = """
mutation($input: CreateReviewInput!) {
  createReview(input: $input) {
    created status createdAt
    userErrors { code field message }
  }
}
"""
CREATED_AT = datetime.datetime(2026, 1, 2, 3, 4, 5)
REVIEW_STATUS_MIGRATION = "0019_review_status_compatibility"
REVIEW_STATUS_TARGET = ("graphql", REVIEW_STATUS_MIGRATION)
REVIEW_STATUS_PREDECESSOR = ("graphql", "0018_audit_log_cursor_index")
CREDIT_TEST_POSTGRES_DSN = os.environ.get("CREDIT_TEST_POSTGRES_DSN")


@pytest.fixture
def review_migration_database():
    """Use one disposable, loopback-only PostgreSQL schema and always drop it."""
    if not CREDIT_TEST_POSTGRES_DSN:
        pytest.skip("CREDIT_TEST_POSTGRES_DSN is not set")

    try:
        parsed = urlsplit(CREDIT_TEST_POSTGRES_DSN)
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
        or not re.fullmatch(r"credit_test_[0-9a-f]{32}", name)
        or user != name
    ):
        pytest.fail(
            "DSN must use loopback, an explicit port, and matching exclusive "
            "credit_test_<uuid> user/database",
            pytrace=False,
        )

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
    database = connections["default"]
    schema_name = "review_migration_" + uuid.uuid4().hex
    created = False
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            assert cursor.fetchone() == (name, user)
            cursor.execute("CREATE SCHEMA " + database.ops.quote_name(schema_name))
            created = True

        database.close()
        database_settings["OPTIONS"] = {
            "connect_timeout": 3,
            "options": f"-c search_path={schema_name},pg_catalog "
            "-c statement_timeout=10000 -c lock_timeout=5000",
        }
        with database.cursor() as cursor:
            cursor.execute("CREATE TABLE auth_user (id integer PRIMARY KEY)")
            cursor.execute("CREATE TABLE product_boardgame (bgg_id integer PRIMARY KEY)")
            cursor.execute(
                """
                CREATE TABLE product_review (
                    id bigint PRIMARY KEY,
                    bgg_id integer NOT NULL REFERENCES product_boardgame(bgg_id)
                        ON DELETE CASCADE,
                    user_id integer NOT NULL REFERENCES auth_user(id) ON DELETE CASCADE,
                    rating smallint NOT NULL,
                    body varchar(2000) NOT NULL,
                    status varchar(20) NOT NULL DEFAULT 'PENDING',
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT chk_product_review_status
                        CHECK (status IN ('PENDING', 'APPROVED', 'HIDDEN', 'DELETED'))
                )
                """
            )
            cursor.execute(
                "CREATE INDEX idx_product_review_approved_game "
                "ON product_review (bgg_id, created_at) WHERE status = 'APPROVED'"
            )
            cursor.execute(
                "CREATE INDEX idx_product_review_user ON product_review (user_id)"
            )
            cursor.execute("INSERT INTO auth_user VALUES (1), (2), (3), (4)")
            cursor.execute("INSERT INTO product_boardgame VALUES (7)")
            cursor.executemany(
                "INSERT INTO product_review (id, bgg_id, user_id, rating, body, status) "
                "VALUES (%s, 7, %s, 5, %s, %s)",
                [
                    (1, 1, "Pending", "PENDING"),
                    (2, 2, "Approved", "APPROVED"),
                    (3, 3, "Hidden", "HIDDEN"),
                    (4, 4, "Deleted", "DELETED"),
                ],
            )

        yield database
    finally:
        database.close()
        if created:
            database_settings["OPTIONS"] = {"connect_timeout": 3}
            with database.cursor() as cursor:
                cursor.execute(
                    "DROP SCHEMA " + database.ops.quote_name(schema_name) + " CASCADE"
                )
        database.close()
        del connections["default"]
        connections.databases["default"] = original


def _migrate_review_status(database, target):
    """Mark the isolated schema's migration ancestors as already provisioned."""
    executor = MigrationExecutor(database)
    graph = executor.loader.graph
    recorder = MigrationRecorder(database)
    for migration_key in graph.forwards_plan(target):
        if migration_key != target:
            recorder.record_applied(*migration_key)
    MigrationExecutor(database).migrate([target])


def test_review_status_compatibility_migration_is_registered():
    migration = importlib.import_module(
        f"saleor.graphql.migrations.{REVIEW_STATUS_MIGRATION}"
    )
    assert migration.Migration


def test_review_status_migration_maps_all_legacy_states(review_migration_database):
    database = review_migration_database
    _migrate_review_status(database, REVIEW_STATUS_TARGET)

    with database.cursor() as cursor:
        cursor.execute(
            "SELECT id, status, is_deleted FROM product_review ORDER BY id"
        )
        assert cursor.fetchall() == [
            (1, "PENDING", False),
            (2, "ACCEPTED", False),
            (3, "REJECTED", False),
            (4, "REJECTED", True),
        ]


def test_review_status_migration_rollback_is_deterministic(
    review_migration_database,
):
    database = review_migration_database
    _migrate_review_status(database, REVIEW_STATUS_TARGET)
    with database.cursor() as cursor:
        # A newly soft-deleted accepted row must roll back to legacy DELETED.
        cursor.execute("UPDATE product_review SET is_deleted = TRUE WHERE id = 2")

    MigrationExecutor(database).migrate([REVIEW_STATUS_PREDECESSOR])
    with database.cursor() as cursor:
        cursor.execute("SELECT id, status FROM product_review ORDER BY id")
        assert cursor.fetchall() == [
            (1, "PENDING"),
            (2, "DELETED"),
            (3, "HIDDEN"),
            (4, "DELETED"),
        ]


def test_public_review_eligibility_requires_accepted_and_not_deleted(
    review_migration_database,
):
    database = review_migration_database
    _migrate_review_status(database, REVIEW_STATUS_TARGET)
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = 'product_review'"
        )
        index_definitions = [row[0].lower() for row in cursor.fetchall()]
        assert any(
            "where" in definition
            and "accepted" in definition
            and "is_deleted" in definition
            for definition in index_definitions
        )

        cursor.execute(
            "SELECT id FROM product_review "
            "WHERE status = 'ACCEPTED' AND is_deleted = FALSE ORDER BY id"
        )
        assert cursor.fetchall() == [(2,)]

        cursor.execute("UPDATE product_review SET is_deleted = TRUE WHERE id = 2")
        cursor.execute(
            "SELECT id FROM product_review "
            "WHERE status = 'ACCEPTED' AND is_deleted = FALSE ORDER BY id"
        )
        assert cursor.fetchall() == []


def _variables(**overrides):
    data = {"productSlug": "catan", "rating": 5, "body": "Great game"}
    data.update(overrides)
    return {"input": data}


def _run(user=MagicMock(pk=7), product={"bgg_id": 13}, insert_error=None, **overrides):
    """Execute createReview; returns (data, cursor)."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (CREATED_AT,)
    if insert_error:
        cursor.execute.side_effect = insert_error
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    with patch.object(api, "connection", connection), \
         patch.object(api, "_authenticated_user", return_value=user), \
         patch.object(api, "_product_by_slug", return_value=product):
        result = schema.execute(MUTATION, variable_values=_variables(**overrides),
                                context_value=MagicMock())
    assert result.errors is None
    return result.data["createReview"], cursor


def _codes(payload):
    return [error["code"] for error in payload["userErrors"]]


def test_unauthenticated_user_is_rejected_without_touching_db():
    payload, cursor = _run(user=None)
    assert payload["created"] is False
    assert _codes(payload) == ["UNAUTHENTICATED"]
    cursor.execute.assert_not_called()


def test_valid_review_is_created_as_pending_with_insert_params():
    payload, cursor = _run(rating=4, body="  Muy bueno  ")
    assert payload["created"] is True
    assert payload["status"] == "PENDING"
    assert payload["createdAt"] == CREATED_AT.isoformat()
    assert payload["userErrors"] == []
    sql, params = cursor.execute.call_args.args
    assert "'PENDING'" in sql
    assert params == [13, 7, 4, "Muy bueno"]  # body trimmed, user from token


@pytest.mark.parametrize("rating", [1, 5])
def test_rating_boundaries_are_accepted(rating):
    payload, _ = _run(rating=rating)
    assert payload["created"] is True


@pytest.mark.parametrize("rating", [0, 6, -1])
def test_rating_out_of_range_is_rejected(rating):
    payload, cursor = _run(rating=rating)
    assert payload["created"] is False
    assert _codes(payload) == ["RATING_OUT_OF_RANGE"]
    assert payload["userErrors"][0]["field"] == ["rating"]
    cursor.execute.assert_not_called()


def test_body_of_exactly_2000_chars_is_accepted():
    payload, _ = _run(body="a" * 2000)
    assert payload["created"] is True


def test_body_over_2000_chars_is_rejected():
    payload, cursor = _run(body="a" * 2001)
    assert _codes(payload) == ["BODY_TOO_LONG"]
    cursor.execute.assert_not_called()


@pytest.mark.parametrize("body", ["", "   \n\t"])
def test_blank_body_is_rejected(body):
    payload, _ = _run(body=body)
    assert _codes(payload) == ["BODY_REQUIRED"]


def test_unknown_product_is_rejected():
    payload, cursor = _run(product=None)
    assert _codes(payload) == ["PRODUCT_NOT_FOUND"]
    cursor.execute.assert_not_called()


def test_duplicate_review_per_user_and_product_is_reported():
    payload, _ = _run(insert_error=IntegrityError("unique violation"))
    assert payload["created"] is False
    assert _codes(payload) == ["REVIEW_ALREADY_EXISTS"]


def _token(token_type, user_id=7):
    now = datetime.datetime.now(datetime.timezone.utc)
    return jwt.encode({"sub": str(user_id), "token_type": token_type,
                       "exp": now + datetime.timedelta(minutes=5)},
                      settings.SECRET_KEY, algorithm="HS256")


@pytest.mark.parametrize("header", ["", "Basic abc", "Bearer not-a-jwt"])
def test_authenticated_user_rejects_missing_or_malformed_header(header):
    info = SimpleNamespace(context=RequestFactory().get("/graphql/", HTTP_AUTHORIZATION=header))
    assert api._authenticated_user(info) is None


def test_authenticated_user_rejects_refresh_token():
    info = MagicMock()
    info.context.headers = {"Authorization": f"Bearer {_token('refresh')}"}
    assert api._authenticated_user(info) is None

ADMIN_REVIEWS_QUERY = """
query AdminReviews($status: ReviewModerationStatus, $page: Int!, $pageSize: Int!) {
  adminReviews(status: $status, page: $page, pageSize: $pageSize) {
    items {
      id gameBggId gameName rating body createdAt status deleted publiclyVisible
    }
    page pageSize total totalPages
  }
}
"""

ACCEPT_REVIEW_MUTATION = """
mutation AcceptReview($reviewId: ID!) {
  moderateReview(reviewId: $reviewId, action: ACCEPT) {
    success
    review { id gameBggId gameName rating body createdAt status deleted publiclyVisible }
    userErrors { code field message }
  }
}
"""

MODERATE_REVIEW_MUTATION = """
mutation ModerateReview($reviewId: ID!, $action: ReviewModerationAction!) {
  moderateReview(reviewId: $reviewId, action: $action) {
    success
    review { id status publiclyVisible }
    userErrors { code field message }
  }
}
"""

DELETE_REVIEW_MUTATION = """
mutation DeleteReview($reviewId: ID!) {
  moderateReview(reviewId: $reviewId, action: DELETE) {
    success
    review { id status deleted publiclyVisible }
    userErrors { code field message }
  }
}
"""


def _moderator(is_staff=False, is_superuser=False, is_active=True):
    return SimpleNamespace(
        pk=42,
        is_active=is_active,
        is_staff=is_staff,
        is_superuser=is_superuser,
    )


def _review_cursor(rows=(), total=0):
    cursor = MagicMock()
    cursor.fetchone.return_value = (total,)
    cursor.fetchall.return_value = list(rows)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


def _run_admin_reviews(user, rows=(), total=0, status="PENDING",
                       page=1, page_size=50, context=None):
    connection, cursor = _review_cursor(rows=rows, total=total)
    if context is None:
        context = SimpleNamespace(headers={})
    with patch.object(api, "connection", connection), \
         patch.object(api, "_authenticated_user", return_value=user):
        result = schema.execute(
            ADMIN_REVIEWS_QUERY,
            variable_values={
                "status": status,
                "page": page,
                "pageSize": page_size,
            },
            context_value=context,
        )
    return result, cursor


def _assert_graphql_error_code(result, code):
    assert result.errors
    assert result.errors[0].extensions["code"] == code


@pytest.mark.parametrize(
    "user",
    [
        _moderator(is_staff=True),
        _moderator(is_superuser=True),
    ],
    ids=["active-staff", "active-superuser"],
)
def test_admin_review_queue_allows_active_staff_and_superuser(user):
    result, _ = _run_admin_reviews(user)

    assert result.errors is None
    assert result.data["adminReviews"]["items"] == []


def test_admin_review_queue_rejects_unauthenticated_user():
    result, cursor = _run_admin_reviews(user=None)

    _assert_graphql_error_code(result, "UNAUTHENTICATED")
    cursor.execute.assert_not_called()


@pytest.mark.parametrize(
    "user",
    [
        _moderator(is_staff=True, is_active=False),
        _moderator(),
    ],
    ids=["inactive-staff", "active-non-admin"],
)
def test_admin_review_queue_rejects_inactive_or_non_admin_user(user):
    result, cursor = _run_admin_reviews(user=user)

    _assert_graphql_error_code(result, "FORBIDDEN")
    cursor.execute.assert_not_called()


def test_admin_review_queue_rejects_operation_key_context_without_user():
    context = RequestFactory().get("/graphql/")
    context.opkey_delegated = True
    context.user = None
    connection, cursor = _review_cursor()
    with patch.object(api, "connection", connection):
        result = schema.execute(
            ADMIN_REVIEWS_QUERY,
            variable_values={"status": None, "page": 1, "pageSize": 50},
            context_value=context,
        )

    _assert_graphql_error_code(result, "UNAUTHENTICATED")
    cursor.execute.assert_not_called()


def test_admin_review_queue_filters_status_and_returns_stable_pagination_context():
    created_at = datetime.datetime(2026, 1, 2, 3, 4, 5)
    rows = [
        (91, 7, "Catan", 5, "Great game", created_at, "PENDING", False),
        (89, 8, "Azul", 4, "Lovely tiles", created_at, "PENDING", False),
    ]
    result, cursor = _run_admin_reviews(
        user=_moderator(is_staff=True),
        rows=rows,
        total=5,
        status="PENDING",
        page=2,
        page_size=2,
    )

    assert result.errors is None
    queue = result.data["adminReviews"]
    assert queue["page"] == 2
    assert queue["pageSize"] == 2
    assert queue["total"] == 5
    assert queue["totalPages"] == 3
    assert queue["items"] == [
        {
            "id": "91",
            "gameBggId": 7,
            "gameName": "Catan",
            "rating": 5,
            "body": "Great game",
            "createdAt": created_at.isoformat(),
            "status": "PENDING",
            "deleted": False,
            "publiclyVisible": False,
        },
        {
            "id": "89",
            "gameBggId": 8,
            "gameName": "Azul",
            "rating": 4,
            "body": "Lovely tiles",
            "createdAt": created_at.isoformat(),
            "status": "PENDING",
            "deleted": False,
            "publiclyVisible": False,
        },
    ]

    statements = [
        (
            call.args[0].lower(),
            tuple(call.args[1]) if len(call.args) > 1 and call.args[1] else (),
        )
        for call in cursor.execute.call_args_list
    ]
    page_sql = [sql for sql, _ in statements if "select" in sql and "created_at" in sql]
    assert page_sql
    assert any("status" in sql and "is_deleted" in sql for sql in page_sql)
    assert any(
        "order by" in sql
        and "created_at desc" in sql
        and "id desc" in sql
        and "limit" in sql
        and "offset" in sql
        for sql in page_sql
    )
    assert any("pending" in sql or "PENDING" in params for sql, params in statements)


@pytest.mark.parametrize(
    ("page", "page_size"),
    [(0, 2), (1, 0), (1, 101), (10001, 1)],
)
def test_admin_review_queue_rejects_out_of_bounds_pagination(page, page_size):
    result, cursor = _run_admin_reviews(
        user=_moderator(is_staff=True),
        status=None,
        page=page,
        page_size=page_size,
    )

    _assert_graphql_error_code(result, "INVALID")
    cursor.execute.assert_not_called()


def test_admin_review_queue_returns_empty_page_for_no_matches():
    result, _ = _run_admin_reviews(
        user=_moderator(is_superuser=True),
        rows=[],
        total=0,
        status="REJECTED",
        page=1,
        page_size=10,
    )

    assert result.errors is None
    assert result.data["adminReviews"] == {
        "items": [],
        "page": 1,
        "pageSize": 10,
        "total": 0,
        "totalPages": 0,
    }


def test_pending_review_can_be_accepted():
    pending = (91, 7, "Catan", 5, "Great game", CREATED_AT, "PENDING", False)
    accepted = (91, 7, "Catan", 5, "Great game", CREATED_AT, "ACCEPTED", False)
    connection, cursor = _review_cursor()
    cursor.fetchone.side_effect = [pending, accepted]
    user = _moderator(is_staff=True)

    with patch.object(api, "connection", connection), \
         patch.object(api, "_authenticated_user", return_value=user), \
         patch.object(api.transaction, "atomic"):
        result = schema.execute(
            ACCEPT_REVIEW_MUTATION,
            variable_values={"reviewId": "91"},
            context_value=SimpleNamespace(headers={}),
        )

    assert result.errors is None
    payload = result.data["moderateReview"]
    assert payload["success"] is True
    assert payload["userErrors"] == []
    assert payload["review"]["id"] == "91"
    assert payload["review"]["status"] == "ACCEPTED"
    assert payload["review"]["publiclyVisible"] is True
    assert any(
        "for update" in call.args[0].lower()
        for call in cursor.execute.call_args_list
    )


def _moderate_review(action, initial_status, resulting_status=None):
    initial = (91, 7, "Catan", 5, "Great game", CREATED_AT, initial_status, False)
    if resulting_status is None:
        rows = [initial]
    else:
        resulting = (
            91, 7, "Catan", 5, "Great game", CREATED_AT, resulting_status, False
        )
        rows = [initial, resulting]
    connection, cursor = _review_cursor()
    cursor.fetchone.side_effect = rows

    with patch.object(api, "connection", connection), \
         patch.object(
             api, "_authenticated_user", return_value=_moderator(is_staff=True)
         ), \
         patch.object(api.transaction, "atomic"):
        result = schema.execute(
            MODERATE_REVIEW_MUTATION,
            variable_values={"reviewId": "91", "action": action},
            context_value=SimpleNamespace(headers={}),
        )

    return result, cursor


@pytest.mark.parametrize(
    ("initial_status", "action", "expected_status"),
    [
        ("PENDING", "REJECT", "REJECTED"),
        ("ACCEPTED", "REJECT", "REJECTED"),
        ("REJECTED", "ACCEPT", "ACCEPTED"),
    ],
    ids=["pending-to-rejected", "accepted-to-rejected", "rejected-to-accepted"],
)
def test_review_moderation_transitions(initial_status, action, expected_status):
    result, cursor = _moderate_review(
        action, initial_status, resulting_status=expected_status
    )

    assert result.errors is None
    payload = result.data["moderateReview"]
    assert payload["success"] is True
    assert payload["userErrors"] == []
    assert payload["review"]["status"] == expected_status
    assert payload["review"]["publiclyVisible"] is (
        expected_status == "ACCEPTED"
    )
    assert any(
        "for update" in call.args[0].lower()
        for call in cursor.execute.call_args_list
    )


@pytest.mark.parametrize(
    ("status", "action"),
    [("ACCEPTED", "ACCEPT"), ("REJECTED", "REJECT")],
    ids=["accepted-again", "rejected-again"],
)
def test_review_moderation_is_idempotent_for_same_state(status, action):
    result, _ = _moderate_review(action, status)

    assert result.errors is None
    payload = result.data["moderateReview"]
    assert payload["success"] is True
    assert payload["userErrors"] == []
    assert payload["review"]["status"] == status
    assert payload["review"]["publiclyVisible"] is (status == "ACCEPTED")


def test_delete_accepted_review_reports_soft_deleted_and_not_public():
    accepted = (91, 7, "Catan", 5, "Great game", CREATED_AT, "ACCEPTED", False)
    deleted = (91, 7, "Catan", 5, "Great game", CREATED_AT, "ACCEPTED", True)
    connection, cursor = _review_cursor()
    cursor.fetchone.side_effect = [accepted, deleted]

    with patch.object(api, "connection", connection), \
         patch.object(
             api, "_authenticated_user", return_value=_moderator(is_staff=True)
         ), \
         patch.object(api.transaction, "atomic"):
        result = schema.execute(
            DELETE_REVIEW_MUTATION,
            variable_values={"reviewId": "91"},
            context_value=SimpleNamespace(headers={}),
        )

    assert result.errors is None
    payload = result.data["moderateReview"]
    assert payload["userErrors"] == []
    assert payload["success"] is True
    assert payload["review"] == {
        "id": "91",
        "status": "ACCEPTED",
        "deleted": True,
        "publiclyVisible": False,
    }


def test_pending_is_not_a_valid_moderation_action():
    connection, cursor = _review_cursor()
    with patch.object(api, "connection", connection), \
         patch.object(
             api, "_authenticated_user", return_value=_moderator(is_staff=True)
         ):
        result = schema.execute(
            MODERATE_REVIEW_MUTATION,
            variable_values={"reviewId": "91", "action": "PENDING"},
            context_value=SimpleNamespace(headers={}),
        )

    assert result.errors
    assert "PENDING" in str(result.errors[0])
    cursor.execute.assert_not_called()


def test_moderation_returns_not_found_for_missing_or_deleted_reviews():
    connection, cursor = _review_cursor()
    cursor.fetchone.return_value = None

    with patch.object(api, "connection", connection), \
         patch.object(
             api, "_authenticated_user", return_value=_moderator(is_staff=True)
         ), \
         patch.object(api.transaction, "atomic"):
        result = schema.execute(
            MODERATE_REVIEW_MUTATION,
            variable_values={"reviewId": "91", "action": "REJECT"},
            context_value=SimpleNamespace(headers={}),
        )

    assert result.errors is None
    payload = result.data["moderateReview"]
    assert payload["success"] is False
    assert payload["userErrors"][0]["code"] == "REVIEW_NOT_FOUND"
    query = cursor.execute.call_args.args[0].lower()
    assert "review.is_deleted = false" in query


def test_concurrent_moderation_last_serialized_action_wins(review_migration_database):
    database = review_migration_database
    _migrate_review_status(database, REVIEW_STATUS_TARGET)
    with database.cursor() as cursor:
        cursor.execute("ALTER TABLE product_boardgame ADD COLUMN name varchar(200)")
        cursor.execute("UPDATE product_boardgame SET name = 'Catan' WHERE bgg_id = 7")
        cursor.execute(
            "INSERT INTO product_review "
            "(id, bgg_id, user_id, rating, body, status) "
            "VALUES (5, 7, 1, 5, 'Concurrent review', 'PENDING')"
        )

    ready = {name: threading.Event() for name in ("accept", "reject")}
    finished = {name: threading.Event() for name in ("accept", "reject")}
    backend_pids = {}
    results = {}

    def moderate(name, action):
        thread_connection = connections["default"]
        try:
            with thread_connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pids[name] = cursor.fetchone()[0]
            ready[name].set()
            results[name] = schema.execute(
                MODERATE_REVIEW_MUTATION,
                variable_values={"reviewId": "5", "action": action},
                context_value=SimpleNamespace(headers={}),
            )
        finally:
            thread_connection.close()
            finished[name].set()

    def wait_for_row_lock(name):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if finished[name].is_set():
                assert results[name].errors is None, (
                    f"{name} moderation failed before acquiring the row lock: "
                    f"{results[name].errors}"
                )
                pytest.fail(f"{name} moderation did not wait for the row lock")
            with database.cursor() as cursor:
                cursor.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    [backend_pids[name]],
                )
                activity = cursor.fetchone()
            if activity and activity[0] == "Lock":
                return
            time.sleep(0.05)
        pytest.fail(f"{name} moderation did not acquire the queued row lock")

    lock_transaction = transaction.atomic()
    lock_transaction.__enter__()
    threads = []
    moderator_patch = patch.object(
        api, "_authenticated_user", return_value=_moderator(is_staff=True)
    )
    moderator_patch.start()
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT id FROM product_review WHERE id = 5 FOR UPDATE")

        for name, action in (("accept", "ACCEPT"), ("reject", "REJECT")):
            thread = threading.Thread(target=moderate, args=(name, action))
            threads.append(thread)
            thread.start()
            assert ready[name].wait(timeout=5)
            if name == "accept":
                wait_for_row_lock(name)
    finally:
        try:
            lock_transaction.__exit__(None, None, None)
        finally:
            for thread in threads:
                thread.join()
            moderator_patch.stop()

    assert all(not thread.is_alive() for thread in threads)
    assert results["accept"].errors is None
    assert results["reject"].errors is None
    assert results["accept"].data["moderateReview"]["review"]["status"] == "ACCEPTED"
    assert results["reject"].data["moderateReview"]["review"]["status"] == "REJECTED"
    with database.cursor() as cursor:
        cursor.execute("SELECT status FROM product_review WHERE id = 5")
        assert cursor.fetchone() == ("REJECTED",)


def test_public_detail_includes_only_accepted_non_deleted_reviews_and_matching_summary():
    accepted_rows = [
        (5, "Excellent strategy", CREATED_AT),
        (3, "Fun with friends", CREATED_AT - datetime.timedelta(days=1)),
    ]
    cursor = MagicMock()
    cursor.fetchall.return_value = accepted_rows
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    product = {
        "bgg_id": 7,
        "name": "Catan",
        "release_year": 1995,
        "min_players": 3,
        "max_players": 4,
        "playing_time_minutes": 60,
        "min_age": 10,
        "weight": 2.3,
        "category": "Strategy",
        "price_amount": 49.99,
        "publisher": "Kosmos",
        "image_url": "https://example.test/catan.jpg",
        "quantity": 4,
    }
    query = """
    {
      productBySlug(slug: "catan") {
        reviewSummary { approvedReviewCount averageRating }
        approvedReviews { rating body createdAt }
      }
    }
    """
    with patch.object(api, "connection", connection), \
         patch.object(api, "_product_by_slug", return_value=product):
        result = schema.execute(query)

    assert result.errors is None
    detail = result.data["productBySlug"]
    assert detail["reviewSummary"] == {
        "approvedReviewCount": 2,
        "averageRating": 4.0,
    }
    assert [review["rating"] for review in detail["approvedReviews"]] == [5, 3]
    assert [review["body"] for review in detail["approvedReviews"]] == [
        "Excellent strategy",
        "Fun with friends",
    ]
    public_sql = cursor.execute.call_args.args[0].lower()
    assert "status = 'accepted'" in public_sql
    assert "is_deleted = false" in public_sql


def test_deleting_last_accepted_review_retains_row_and_removes_public_visibility(
    review_migration_database,
):
    database = review_migration_database
    _migrate_review_status(database, REVIEW_STATUS_TARGET)
    with database.cursor() as cursor:
        cursor.execute("ALTER TABLE product_boardgame ADD COLUMN name varchar(200)")
        cursor.execute("UPDATE product_boardgame SET name = 'Catan' WHERE bgg_id = 7")

    with patch.object(
        api, "_authenticated_user", return_value=_moderator(is_staff=True)
    ):
        delete_result = schema.execute(
            DELETE_REVIEW_MUTATION,
            variable_values={"reviewId": "2"},
            context_value=SimpleNamespace(headers={}),
        )

    assert delete_result.errors is None
    delete_payload = delete_result.data["moderateReview"]
    assert delete_payload["success"] is True
    assert delete_payload["userErrors"] == []
    assert delete_payload["review"]["status"] == "ACCEPTED"
    assert delete_payload["review"]["deleted"] is True
    assert delete_payload["review"]["publiclyVisible"] is False

    with database.cursor() as cursor:
        cursor.execute(
            "SELECT status, is_deleted FROM product_review WHERE id = 2"
        )
        assert cursor.fetchone() == ("ACCEPTED", True)

    with patch.object(
        api, "_authenticated_user", return_value=_moderator(is_staff=True)
    ):
        admin_result = schema.execute(
            ADMIN_REVIEWS_QUERY,
            variable_values={"status": "ACCEPTED", "page": 1, "pageSize": 50},
            context_value=SimpleNamespace(headers={}),
        )

    assert admin_result.errors is None
    assert admin_result.data["adminReviews"]["items"] == []

    product = {
        "bgg_id": 7,
        "name": "Catan",
        "release_year": 1995,
        "min_players": 3,
        "max_players": 4,
        "playing_time_minutes": 60,
        "min_age": 10,
        "weight": 2.3,
        "category": "Strategy",
        "price_amount": 49.99,
        "publisher": "Kosmos",
        "image_url": "https://example.test/catan.jpg",
        "quantity": 4,
    }
    with patch.object(api, "_product_by_slug", return_value=product):
        public_result = schema.execute(
            """
            {
              productBySlug(slug: "catan") {
                reviewSummary { approvedReviewCount averageRating }
                approvedReviews { rating body }
              }
            }
            """
        )

    assert public_result.errors is None
    detail = public_result.data["productBySlug"]
    assert detail["reviewSummary"] == {
        "approvedReviewCount": 0,
        "averageRating": None,
    }
    assert detail["approvedReviews"] == []
