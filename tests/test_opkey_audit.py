import json
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

import django
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connections
from django.test import RequestFactory, override_settings
from django.utils import timezone

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
django.setup()

from django.contrib.auth.models import User
from opkeys import models, views
from opkeys.utils import key_hash


INTERNAL_SECRET = "synthetic-internal-secret-0123456789-abcdef"


@pytest.fixture
def isolated_audit_database():
    audit_model = models.OpKeyAudit
    alias = f"isolated_opkey_audit_{uuid.uuid4().hex}"
    connections.databases[alias] = {
        **connections.databases["default"], "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:", "OPTIONS": {},
    }
    database = connections[alias]
    try:
        with database.schema_editor() as editor:
            editor.create_model(User)
            editor.create_model(models.OpSession)
            editor.create_model(audit_model)
        user = User.objects.db_manager(alias).create_user(username="synthetic-admin", is_staff=True)
        sessions = models.OpSession.objects.db_manager(alias)
        audits = audit_model.objects.db_manager(alias)
        with override_settings(SECRET_KEY="synthetic-test-signing-key"), patch.object(
            models.OpSession, "objects", sessions
        ), patch.object(audit_model, "objects", audits):
            session = sessions.create(user=user, key_hash=key_hash("synthetic-operation-key"), ops=[],
                                      expires_at=timezone.now() + timedelta(minutes=30))
            yield user, session, sessions, audits
    finally:
        database.close()
        del connections.databases[alias]


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_logout_records_owner_as_inferred_actor_without_storing_key(isolated_audit_database):
    user, session, sessions, audits = isolated_audit_database
    request = RequestFactory().post(
        "/internal/op-revoke", data=json.dumps({"op_key": "synthetic-operation-key", "reason": "logout"}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    response = views.op_revoke(request)
    assert response.status_code == 200
    assert sessions.get(pk=session.pk).revoked is True
    event = audits.get()
    assert str(event.session_id) == str(session.pk)
    assert event.subject_user_id == user.pk and event.actor_user_id == user.pk
    assert event.actor_source == "KEY_HOLDER"
    assert event.action == "LOGOUT"
    assert "synthetic-operation-key" not in str(event.__dict__)


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_internal_issuance_records_service_actor(isolated_audit_database):
    user, _session, _sessions, audits = isolated_audit_database
    request = RequestFactory().post(
        "/internal/op-create", data=json.dumps({"user_id": user.pk, "ops": ["GET:/api/v1/admin/products"]}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    response = views.op_create(request)
    assert response.status_code == 200
    event = audits.get()
    assert event.action == "ISSUE" and event.subject_user_id == user.pk
    assert event.actor_user_id is None and event.actor_source == "INTERNAL_SERVICE"
    assert json.loads(response.content)["raw_key"] not in str(event.__dict__)


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_issuance_rolls_back_if_audit_fails(isolated_audit_database):
    user, _session, sessions, audits = isolated_audit_database
    request = RequestFactory().post(
        "/internal/op-create", data=json.dumps({"user_id": user.pk, "ops": []}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    with patch.object(audits, "create", side_effect=DatabaseError("synthetic-private-error")):
        response = views.op_create(request)
    assert response.status_code == 503
    assert sessions.count() == 1
    assert b"synthetic-private-error" not in response.content


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET, OPKEYS_ISSUE_ON_LOGIN=True)
def test_login_issuance_records_authenticated_actor(isolated_audit_database):
    from saleor.graphql.api import LoginMutation
    from types import SimpleNamespace

    user, _session, _sessions, audits = isolated_audit_database
    credentials = SimpleNamespace(email="admin@example.test", password="synthetic-password", guest_cart_id=None)
    with patch("django.contrib.auth.authenticate", return_value=user):
        result = LoginMutation.mutate(None, SimpleNamespace(context=None), credentials)
    assert result.tokens.op_key
    event = audits.get()
    assert event.action == "ISSUE" and event.subject_user_id == user.pk
    assert event.actor_user_id == user.pk and event.actor_source == "AUTHENTICATED_USER"
    assert result.tokens.op_key not in str(event.__dict__)


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET, OPKEYS_ISSUE_ON_LOGIN=True)
def test_manual_rotation_keeps_new_session_valid_after_old_session_is_revoked(isolated_audit_database):
    from saleor.graphql.api import LoginMutation, schema
    from types import SimpleNamespace

    user, _session, sessions, audits = isolated_audit_database
    credentials = SimpleNamespace(email="admin@example.test", password="synthetic-password", guest_cart_id=None)
    with patch("django.contrib.auth.authenticate", return_value=user):
        previous = LoginMutation.mutate(None, SimpleNamespace(context=None), credentials).tokens.op_key
        replacement = LoginMutation.mutate(None, SimpleNamespace(context=None), credentials).tokens.op_key
    previous_session = sessions.get(key_hash=key_hash(previous))
    replacement_session = sessions.get(key_hash=key_hash(replacement))
    assert previous_session.is_valid() and replacement_session.is_valid()

    with patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(
            'mutation($id: ID!) { manageOpSession(sessionId: $id, action: "REVOKE") '
            '{ success userErrors { code } } }', variable_values={"id": str(previous_session.pk)},
        )
    assert not result.errors and result.data["manageOpSession"]["success"] is True
    previous_session.refresh_from_db(using=sessions.db)
    replacement_session.refresh_from_db(using=sessions.db)
    assert not previous_session.is_valid() and replacement_session.is_valid()
    assert audits.filter(session_id=previous_session.pk, action="REVOKE", actor_user_id=user.pk).exists()



@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_audit_failure_rolls_back_revocation(isolated_audit_database):
    _user, session, sessions, audits = isolated_audit_database
    request = RequestFactory().post(
        "/internal/op-revoke", data=json.dumps({"op_key": "synthetic-operation-key", "reason": "logout"}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    with patch.object(audits, "create", side_effect=DatabaseError("synthetic-private-error")):
        response = views.op_revoke(request)
    assert response.status_code == 503
    assert b"synthetic-private-error" not in response.content
    assert sessions.get(pk=session.pk).revoked is False


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_expiration_is_audited_once_without_storing_the_key(isolated_audit_database):
    user, session, sessions, audits = isolated_audit_database
    sessions.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    for _ in range(2):
        request = RequestFactory().post(
            "/internal/op-validate", data=json.dumps({"op_key": "synthetic-operation-key", "path": "/test"}),
            content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        )
        response = views.op_validate(request)
        assert json.loads(response.content)["valid"] is False
    event = audits.get()
    assert event.action == "EXPIRED" and event.session_id == session.pk
    assert event.actor_user_id == user.pk and event.actor_source == "KEY_HOLDER"
    assert "synthetic-operation-key" not in str(event.__dict__)


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_expiration_audit_failure_denies_validation_without_exposing_error(isolated_audit_database):
    _user, session, sessions, audits = isolated_audit_database
    sessions.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    request = RequestFactory().post(
        "/internal/op-validate", data=json.dumps({"op_key": "synthetic-operation-key", "path": "/test"}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    with patch.object(audits, "create", side_effect=DatabaseError("synthetic-private-error")):
        response = views.op_validate(request)
    assert response.status_code == 503
    assert b"synthetic-private-error" not in response.content
    assert audits.count() == 0


def test_audit_retention_is_seven_days_and_dry_run_by_default(isolated_audit_database):
    user, session, _sessions, audits = isolated_audit_database
    now = timezone.now()
    audits.create(session_id=session.pk, subject_user_id=user.pk, actor_user_id=user.pk,
                  action="LOGOUT", created_at=now - timedelta(days=8))
    audits.create(session_id=session.pk, subject_user_id=user.pk, actor_user_id=user.pk,
                  action="LOGOUT", created_at=now - timedelta(days=6))
    call_command("purge_opkey_audit", stdout=StringIO())
    assert audits.count() == 2
    call_command("purge_opkey_audit", apply=True, stdout=StringIO())
    assert audits.count() == 1


def test_retention_worker_runs_a_bounded_cleanup():
    with patch("opkeys.management.commands.run_opkey_audit_retention.call_command") as cleanup:
        call_command("run_opkey_audit_retention", once=True, stdout=StringIO())
    cleanup.assert_called_once()
    assert cleanup.call_args.args == ("purge_opkey_audit",)
    assert cleanup.call_args.kwargs["apply"] is True


@pytest.mark.parametrize("action", ["REVOKE", "EXPIRE"])
def test_administrative_change_records_authenticated_actor(isolated_audit_database, action):
    from saleor.graphql.api import schema

    user, session, _sessions, audits = isolated_audit_database
    with patch("saleor.graphql.api._authenticated_user", return_value=user):
        result = schema.execute(
            'mutation($id: ID!, $action: String!) { manageOpSession(sessionId: $id, action: $action) '
            '{ success userErrors { code } } }', variable_values={"id": str(session.pk), "action": action},
        )
    assert not result.errors and result.data["manageOpSession"]["success"] is True
    event = audits.get()
    assert event.session_id == session.pk and event.subject_user_id == user.pk
    assert event.actor_user_id == user.pk and event.actor_source == "ADMIN"
    assert event.action == action


def test_admin_audit_failure_rolls_back_session_change(isolated_audit_database):
    from saleor.graphql.api import schema

    user, session, sessions, audits = isolated_audit_database
    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch.object(
        audits, "create", side_effect=DatabaseError("synthetic-private-error")
    ):
        result = schema.execute(
            'mutation($id: ID!) { manageOpSession(sessionId: $id, action: "REVOKE") '
            '{ success userErrors { code message } } }', variable_values={"id": str(session.pk)},
        )
    assert not result.errors and result.data["manageOpSession"]["success"] is False
    assert "synthetic-private-error" not in str(result.data)
    assert sessions.get(pk=session.pk).revoked is False