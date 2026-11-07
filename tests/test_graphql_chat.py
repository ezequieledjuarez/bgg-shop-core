"""Tests for the approved Core chat identity and conversation contract."""

import hashlib
import os
import re
import sys
import time
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import django
import jwt
import pytest
from django.conf import settings
from django.utils import timezone
from django.test import RequestFactory, override_settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connections

from saleor.graphql.api import schema


_CHAT_ASSERTION_KEY = (
    "test-only-chat-assertion-key-" + "x" * 40 + "-0123456789ABCDEF"
)
_OTHER_ASSERTION_KEY = "different-test-only-chat-key-" + "y" * 40
_POSTGRES_DSN = os.environ.get("CHAT_CONVERSATION_TEST_POSTGRES_DSN")
_MUTATION = """
mutation CreateConversation {
  createChatConversation {
    conversation { id messages { role content } }
    userErrors { code }
  }
}
"""
_RECORD_MUTATION = """
mutation RecordExchange(
  $conversationId: ID!
  $userMessage: String!
  $assistantMessage: String!
) {
  recordChatExchange(
    conversationId: $conversationId
    userMessage: $userMessage
    assistantMessage: $assistantMessage
  ) {
    conversation { id messages { role content } }
    userErrors { code field message }
  }
}
"""
_CONVERSATION_QUERY = """
query Conversation($id: ID!) {
  chatConversation(id: $id) {
    conversation { id messages { role content } }
    userErrors { code }
  }
}
"""
_DELETE_CONVERSATION_MUTATION = """
mutation DeleteConversation($conversationId: ID!) {
  deleteChatConversation(conversationId: $conversationId) {
    success
    userErrors { code message }
  }
}
"""


def _set_chat_last_activity(database, conversation_id, last_activity):
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE chat_conversation SET created_at = %s, updated_at = %s "
            "WHERE id = %s",
            [
                last_activity,
                last_activity,
                uuid.UUID(conversation_id),
            ],
        )


def _create_chat_exchange(assertion, user_message, assistant_message):
    created = _execute(_MUTATION, assertion=assertion)
    assert created.errors is None, created.errors
    payload = created.data["createChatConversation"]
    assert payload["userErrors"] == []
    conversation_id = payload["conversation"]["id"]

    recorded = _execute(
        _RECORD_MUTATION,
        assertion=assertion,
        variables={
            "conversationId": conversation_id,
            "userMessage": user_message,
            "assistantMessage": assistant_message,
        },
    )
    assert recorded.errors is None, recorded.errors
    assert recorded.data["recordChatExchange"]["userErrors"] == []
    return conversation_id


def _claims(sub, **overrides):
    now = int(time.time())
    claims = {
        "iss": "bgg-shop-bff",
        "aud": "bgg-shop-core",
        "sub": sub,
        "scope": "chat:conversations",
        "iat": now,
        "exp": now + 60,
    }
    claims.update(overrides)
    return claims


def _assertion(
    sub="a" * 64,
    *,
    key=_CHAT_ASSERTION_KEY,
    claims=None,
    typ="chat-assertion",
    algorithm="HS256",
):
    return jwt.encode(
        _claims(sub) if claims is None else claims,
        key,
        algorithm=algorithm,
        headers={"typ": typ},
    )


def _request(assertion=None, bearer=None):
    headers = {}
    if assertion is not None:
        headers["HTTP_X_CHAT_ASSERTION"] = assertion
    if bearer is not None:
        headers["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return RequestFactory().post("/graphql/", **headers)


def _execute(query, *, assertion=None, bearer=None, variables=None):
    return schema.execute(
        query,
        variable_values=variables,
        context_value=_request(assertion=assertion, bearer=bearer),
    )


@pytest.fixture
def chat_postgres_database():
    """Run migrations only in a unique, loopback-only disposable test database."""
    if not _POSTGRES_DSN:
        pytest.skip("CHAT_CONVERSATION_TEST_POSTGRES_DSN is not set")

    try:
        parsed = urlsplit(_POSTGRES_DSN)
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
        or not re.fullmatch(r"chat_conversation_test_[0-9a-f]{32}", name)
        or user != name
    ):
        pytest.fail(
            "DSN must use loopback, an explicit port, and a unique "
            "chat_conversation_test_<uuid> role/database",
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
    schema_name = "chat_conversation_" + uuid.uuid4().hex
    created = False
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            assert cursor.fetchone() == (name, user)
            cursor.execute(f'CREATE SCHEMA "{schema_name}"')
            created = True

        database.close()
        database_settings["OPTIONS"] = {
            "connect_timeout": 3,
            "options": (
                f"-c search_path={schema_name},pg_catalog "
                "-c statement_timeout=10000 -c lock_timeout=5000"
            ),
        }
        # The legacy catalog tables reference auth_user, so establish that
        # prerequisite first; the subsequent unrestricted migrate applies the
        # complete migration graph in its normal dependency order.
        call_command(
            "migrate", "auth", database="default", interactive=False, verbosity=0
        )
        # Supply only the pre-migration legacy relations required by 0001-0019.
        # They are the relevant subset of import_catalog.sql, not application data.
        with database.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE gift_card (
                    id uuid PRIMARY KEY
                );
                CREATE TABLE purchase_order (
                    id uuid PRIMARY KEY,
                    user_id integer NOT NULL REFERENCES auth_user(id) ON DELETE RESTRICT
                );
                CREATE TABLE product_boardgame (
                    id serial PRIMARY KEY,
                    bgg_id integer UNIQUE NOT NULL,
                    price_amount numeric(12, 2) NOT NULL
                );
                CREATE TABLE product_review (
                    id bigserial PRIMARY KEY,
                    bgg_id integer NOT NULL
                        REFERENCES product_boardgame(bgg_id) ON DELETE CASCADE,
                    user_id integer NOT NULL
                        REFERENCES auth_user(id) ON DELETE CASCADE,
                    status varchar(20) NOT NULL DEFAULT 'PENDING',
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT chk_product_review_status
                        CHECK (status IN ('PENDING', 'APPROVED', 'HIDDEN', 'DELETED'))
                );
                CREATE INDEX idx_product_review_approved_game
                    ON product_review (bgg_id, created_at)
                    WHERE status = 'APPROVED';
            """)
        call_command("migrate", database="default", interactive=False, verbosity=0)
        with database.cursor() as cursor:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1
                    FROM django_migrations
                    WHERE app = 'graphql'
                      AND name = '0020_chat_conversations'
                ),
                to_regclass('chat_conversation'),
                to_regclass('chat_message')
            """)
            migration_applied, conversation_table, message_table = cursor.fetchone()
            assert migration_applied
            assert conversation_table is not None
            assert message_table is not None
        yield database
    finally:
        database.close()
        if created:
            database_settings["OPTIONS"] = {"connect_timeout": 3}
            with database.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
        database.close()
        del connections["default"]
        connections.databases["default"] = original


def test_chat_graphql_contract_has_no_owner_or_assertion_arguments():
    graphql_schema = schema.graphql_schema
    query_fields = graphql_schema.query_type.fields
    mutation_fields = graphql_schema.mutation_type.fields

    assert set(query_fields["chatConversation"].args) == {"id"}
    assert set(mutation_fields["createChatConversation"].args) == set()
    assert set(mutation_fields["recordChatExchange"].args) == {
        "conversationId",
        "userMessage",
        "assistantMessage",
        "sources",
    }
    assert "ownerId" not in mutation_fields["createChatConversation"].args
    assert "ownerId" not in mutation_fields["recordChatExchange"].args
    assert "assertion" not in mutation_fields["createChatConversation"].args
    assert "assertion" not in mutation_fields["recordChatExchange"].args


@pytest.mark.parametrize(
    "case",
    (
        "wrong_key",
        "wrong_signature",
        "wrong_issuer",
        "wrong_audience",
        "wrong_type",
        "wrong_scope",
        "missing_expiration",
        "expired",
        "unsupported_algorithm",
    ),
)
def test_anonymous_chat_rejects_invalid_assertions(case):
    claims = _claims("b" * 64)
    key = _CHAT_ASSERTION_KEY
    typ = "chat-assertion"
    algorithm = "HS256"

    if case == "wrong_key":
        key = _OTHER_ASSERTION_KEY
    elif case == "wrong_signature":
        valid = _assertion(claims=claims)
        signing_input, signature = valid.rsplit(".", 1)
        replacement = "A" * len(signature)
        if replacement == signature:
            replacement = "B" * len(signature)
        token = f"{signing_input}.{replacement}"
    elif case == "wrong_issuer":
        claims["iss"] = "untrusted-issuer"
    elif case == "wrong_audience":
        claims["aud"] = "another-service"
    elif case == "wrong_type":
        typ = "JWT"
    elif case == "wrong_scope":
        claims["scope"] = "chat:other"
    elif case == "missing_expiration":
        claims.pop("exp")
    elif case == "expired":
        claims["exp"] = int(time.time()) - 1
    elif case == "unsupported_algorithm":
        algorithm = "HS384"

    if case != "wrong_signature":
        token = _assertion(
            key=key, claims=claims, typ=typ, algorithm=algorithm
        )

    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        result = _execute(_MUTATION, assertion=token)

    assert result.errors is None, result.errors
    assert result.data["createChatConversation"]["conversation"] is None
    assert result.data["createChatConversation"]["userErrors"][0]["code"] == (
        "UNAUTHENTICATED"
    )


@pytest.mark.parametrize(
    "secret_overrides",
    (
        {"CHAT_ASSERTION_SECRET": ""},
        {"CHAT_ASSERTION_SECRET": "x" * 64},
        {
            "CHAT_ASSERTION_SECRET": _CHAT_ASSERTION_KEY,
            "SECRET_KEY": _CHAT_ASSERTION_KEY,
        },
        {
            "CHAT_ASSERTION_SECRET": _CHAT_ASSERTION_KEY,
            "OPKEY_INTERNAL_SECRET": _CHAT_ASSERTION_KEY,
        },
    ),
)
def test_anonymous_chat_fails_closed_for_unset_weak_or_reused_secret(
    secret_overrides,
):
    token = _assertion("c" * 64)
    with override_settings(**secret_overrides):
        result = _execute(_MUTATION, assertion=token)

    assert result.errors is None, result.errors
    assert result.data["createChatConversation"]["conversation"] is None
    assert result.data["createChatConversation"]["userErrors"][0]["code"] == (
        "UNAUTHENTICATED"
    )


def test_invalid_bearer_does_not_fall_back_to_valid_anonymous_assertion():
    token = _assertion("d" * 64)
    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        result = _execute(
            _MUTATION,
            assertion=token,
            bearer="not-a-valid-access-token",
        )

    assert result.errors is None, result.errors
    assert result.data["createChatConversation"]["conversation"] is None
    assert result.data["createChatConversation"]["userErrors"][0]["code"] == (
        "UNAUTHENTICATED"
    )


def test_valid_assertion_owns_created_conversation_and_recorded_exchange(
    chat_postgres_database,
):
    session = "synthetic-browser-chat-session"
    owner_subject = hashlib.sha256(session.encode("utf-8")).hexdigest()
    token = _assertion(owner_subject)

    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        created = _execute(_MUTATION, assertion=token)
        assert created.errors is None, created.errors
        payload = created.data["createChatConversation"]
        assert payload["userErrors"] == []
        conversation_id = payload["conversation"]["id"]
        assert payload["conversation"]["messages"] == []

        recorded = _execute(
            _RECORD_MUTATION,
            assertion=token,
            variables={
                "conversationId": conversation_id,
                "userMessage": "How many players can join?",
                "assistantMessage": "The published player count is two to four.",
            },
        )

    assert recorded.errors is None, recorded.errors
    exchange = recorded.data["recordChatExchange"]
    assert exchange["userErrors"] == []
    assert exchange["conversation"]["id"] == conversation_id
    assert exchange["conversation"]["messages"] == [
        {"role": "USER", "content": "How many players can join?"},
        {
            "role": "ASSISTANT",
            "content": "The published player count is two to four.",
        },
    ]


def test_record_exchange_rejects_oversized_user_message_without_database():
    oversized_message = "sensitive-long-user-message-" + "x" * 2000

    with (
        patch(
            "saleor.graphql.api._chat_execution_owner",
            return_value=("ANONYMOUS", "a" * 64, None),
        ),
        patch(
            "saleor.graphql.api.connection.cursor",
            side_effect=AssertionError("database cursor must not be used"),
        ),
        patch(
            "saleor.graphql.api.transaction.atomic",
            side_effect=AssertionError("database transaction must not be used"),
        ),
    ):
        result = _execute(
            _RECORD_MUTATION,
            variables={
                "conversationId": str(uuid.uuid4()),
                "userMessage": oversized_message,
                "assistantMessage": "A concise answer.",
            },
        )

    assert result.errors is None, result.errors
    payload = result.data["recordChatExchange"]
    assert payload["conversation"] is None
    assert payload["userErrors"][0]["code"] == "INVALID"
    assert payload["userErrors"][0]["field"] == ["userMessage"]
    assert "sensitive-long-user-message" not in payload["userErrors"][0]["message"]


def test_anonymous_session_cannot_read_or_record_another_scope_conversation(
    chat_postgres_database,
):
    first_session = "first-synthetic-session"
    first_subject = hashlib.sha256(first_session.encode("utf-8")).hexdigest()
    first_assertion = _assertion(first_subject)
    other_session = "second-synthetic-session"
    other_subject = hashlib.sha256(other_session.encode("utf-8")).hexdigest()
    other_assertion = _assertion(other_subject)

    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        created = _execute(_MUTATION, assertion=first_assertion)
        assert created.errors is None, created.errors
        conversation_id = created.data["createChatConversation"]["conversation"]["id"]

        read = _execute(
            _CONVERSATION_QUERY,
            assertion=other_assertion,
            variables={"id": conversation_id},
        )
        recorded = _execute(
            _RECORD_MUTATION,
            assertion=other_assertion,
            variables={
                "conversationId": conversation_id,
                "userMessage": "A message from another scope",
                "assistantMessage": "This must not be stored.",
            },
        )
        unchanged = _execute(
            _CONVERSATION_QUERY,
            assertion=first_assertion,
            variables={"id": conversation_id},
        )

    assert read.errors is None, read.errors
    assert read.data["chatConversation"]["conversation"] is None
    assert read.data["chatConversation"]["userErrors"][0]["code"] == "NOT_FOUND"
    assert recorded.errors is None, recorded.errors
    assert recorded.data["recordChatExchange"]["conversation"] is None
    assert recorded.data["recordChatExchange"]["userErrors"][0]["code"] == (
        "NOT_FOUND"
    )
    assert unchanged.errors is None, unchanged.errors
    assert unchanged.data["chatConversation"]["conversation"]["messages"] == []


def test_registered_bearer_identity_overrides_present_anonymous_assertion(
    chat_postgres_database,
):
    user = User.objects.create(username=f"chat-test-{uuid.uuid4().hex}")
    bearer = jwt.encode(
        {
            "sub": str(user.pk),
            "token_type": "access",
            "exp": int(time.time()) + 300,
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    anonymous_session = "conflicting-anonymous-session"
    anonymous_subject = hashlib.sha256(anonymous_session.encode("utf-8")).hexdigest()
    assertion = _assertion(anonymous_subject)

    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        created = _execute(
            _MUTATION,
            assertion=assertion,
            bearer=bearer,
        )
        assert created.errors is None, created.errors
        conversation_id = created.data["createChatConversation"]["conversation"]["id"]

        registered_read = _execute(
            _CONVERSATION_QUERY,
            assertion=assertion,
            bearer=bearer,
            variables={"id": conversation_id},
        )
        anonymous_read = _execute(
            _CONVERSATION_QUERY,
            assertion=assertion,
            variables={"id": conversation_id},
        )

    assert registered_read.errors is None, registered_read.errors
    assert registered_read.data["chatConversation"]["conversation"]["id"] == (
        conversation_id
    )
    assert anonymous_read.errors is None, anonymous_read.errors
    assert anonymous_read.data["chatConversation"]["conversation"] is None
    assert anonymous_read.data["chatConversation"]["userErrors"][0]["code"] == (
        "NOT_FOUND"
    )


def test_chat_conversation_delete_removes_owner_conversation_and_messages(
    chat_postgres_database,
):
    session = "owner-authorized-delete-session"
    subject = hashlib.sha256(session.encode("utf-8")).hexdigest()
    assertion = _assertion(subject)
    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        conversation_id = _create_chat_exchange(
            assertion,
            "private buyer question",
            "private assistant response",
        )
        deleted = _execute(
            _DELETE_CONVERSATION_MUTATION,
            assertion=assertion,
            variables={"conversationId": conversation_id},
        )

    assert deleted.errors is None, deleted.errors
    payload = deleted.data["deleteChatConversation"]
    assert payload["success"] is True
    assert payload["userErrors"] == []
    with chat_postgres_database.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM chat_conversation WHERE id = %s",
            [uuid.UUID(conversation_id)],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM chat_message WHERE conversation_id = %s",
            [uuid.UUID(conversation_id)],
        )
        assert cursor.fetchone()[0] == 0


def test_chat_conversation_delete_denies_other_owner_without_leaking_data(
    chat_postgres_database,
):
    owner_subject = hashlib.sha256(b"conversation-owner-session").hexdigest()
    other_subject = hashlib.sha256(b"different-delete-session").hexdigest()
    owner_assertion = _assertion(owner_subject)
    other_assertion = _assertion(other_subject)
    private_message = "owner-only conversation content"

    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        conversation_id = _create_chat_exchange(
            owner_assertion,
            private_message,
            "owner-only response",
        )
        denied = _execute(
            _DELETE_CONVERSATION_MUTATION,
            assertion=other_assertion,
            variables={"conversationId": conversation_id},
        )
        owner_read = _execute(
            _CONVERSATION_QUERY,
            assertion=owner_assertion,
            variables={"id": conversation_id},
        )

    assert denied.errors is None, denied.errors
    payload = denied.data["deleteChatConversation"]
    assert payload["success"] is False
    assert len(payload["userErrors"]) == 1
    assert payload["userErrors"][0]["code"] == "NOT_FOUND"
    assert private_message not in str(payload)
    assert conversation_id not in str(payload)
    assert owner_read.errors is None, owner_read.errors
    assert owner_read.data["chatConversation"]["conversation"]["messages"] == [
        {"role": "USER", "content": private_message},
        {"role": "ASSISTANT", "content": "owner-only response"},
    ]


def test_chat_conversation_expires_after_30_days_of_inactivity(
    chat_postgres_database,
):
    session = "thirty-day-inactivity-session"
    subject = hashlib.sha256(session.encode("utf-8")).hexdigest()
    assertion = _assertion(subject)
    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        conversation_id = _create_chat_exchange(
            assertion,
            "message that will expire",
            "response that will expire",
        )
        _set_chat_last_activity(
            chat_postgres_database,
            conversation_id,
            timezone.now() - timedelta(days=30, seconds=1),
        )
        expired = _execute(
            _CONVERSATION_QUERY,
            assertion=assertion,
            variables={"id": conversation_id},
        )

    assert expired.errors is None, expired.errors
    payload = expired.data["chatConversation"]
    assert payload["conversation"] is None
    assert payload["userErrors"][0]["code"] == "NOT_FOUND"
    assert "message that will expire" not in str(payload)
    assert conversation_id not in str(payload)


def test_chat_retention_command_physically_purges_only_inactive_conversations(
    chat_postgres_database,
):
    inactive_subject = hashlib.sha256(b"inactive-retention-session").hexdigest()
    active_subject = hashlib.sha256(b"active-retention-session").hexdigest()
    inactive_assertion = _assertion(inactive_subject)
    active_assertion = _assertion(active_subject)
    with override_settings(CHAT_ASSERTION_SECRET=_CHAT_ASSERTION_KEY):
        inactive_id = _create_chat_exchange(
            inactive_assertion, "expired question", "expired answer"
        )
        active_id = _create_chat_exchange(
            active_assertion, "recent question", "recent answer"
        )
        now = timezone.now()
        _set_chat_last_activity(
            chat_postgres_database,
            inactive_id,
            now - timedelta(days=30, seconds=1),
        )
        _set_chat_last_activity(
            chat_postgres_database,
            active_id,
            now - timedelta(days=29),
        )

    # This represents the independent, BFF-owned ChromaDB FAQ store. Core
    # conversation cleanup must not alter its reviewed documents.
    separate_chroma_store = {"approved-faq-1": "reviewed game return policy"}
    expected_chroma_documents = separate_chroma_store.copy()
    call_command("purge_chat_conversations", apply=True, stdout=StringIO())

    with chat_postgres_database.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM chat_conversation ORDER BY id"
        )
        assert {str(row[0]) for row in cursor.fetchall()} == {active_id}
        cursor.execute(
            "SELECT content FROM chat_message WHERE conversation_id = %s",
            [uuid.UUID(active_id)],
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "recent question",
            "recent answer",
        ]
    assert separate_chroma_store == expected_chroma_documents


def test_chat_retention_runner_once_invokes_one_physical_purge():
    with patch(
        "saleor.graphql.management.commands.run_chat_conversation_retention.call_command"
    ) as purge:
        call_command(
            "run_chat_conversation_retention",
            once=True,
            stdout=StringIO(),
        )

    purge.assert_called_once()
    assert purge.call_args.args == ("purge_chat_conversations",)
    assert purge.call_args.kwargs["apply"] is True


def test_chat_retention_runner_repeats_cleanup_on_60_second_intervals():
    class StopRunner(Exception):
        pass

    with (
        patch(
            "saleor.graphql.management.commands.run_chat_conversation_retention.call_command"
        ) as purge,
        patch(
            "saleor.graphql.management.commands.run_chat_conversation_retention.Event"
        ) as event,
    ):
        event.return_value.wait.side_effect = [None, StopRunner]
        with pytest.raises(StopRunner):
            call_command(
                "run_chat_conversation_retention",
                stdout=StringIO(),
            )

    assert purge.call_count == 2
    assert all(call.args == ("purge_chat_conversations",) for call in purge.call_args_list)
    assert all(call.kwargs["apply"] is True for call in purge.call_args_list)
    assert [call.args for call in event.return_value.wait.call_args_list] == [
        (60,),
        (60,),
    ]
