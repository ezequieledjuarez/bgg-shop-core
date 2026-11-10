"""Hermetic unit tests for registration email verification.

Run with DJANGO_SETTINGS_MODULE=email_verification_settings so no .env is read.
"""
import json
import logging
import os
import smtplib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "email_verification_settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from django.test.utils import setup_test_environment

setup_test_environment()

from django.core import mail
from django.db import DatabaseError
from django.test import RequestFactory
from graphql import build_schema

from saleor import email_verification as ev
from saleor.graphql import api
from saleor.graphql.api import schema
from saleor.graphql.views import SanitizedGraphQLView

SECRET = "test-internal-secret-0123456789abcdef0123456789"
CALLER = "a" * 64
TOKEN = "T" * 43
EMAIL = "player@example.test"
CANONICAL = Path(__file__).resolve().parents[1] / "specs" / "schema.graphql"


def context(secret=SECRET, caller=CALLER):
    headers = {}
    if secret is not None:
        headers["X-Internal-Secret"] = secret
    if caller is not None:
        headers["X-Verification-Caller"] = caller
    return SimpleNamespace(headers=headers)


def run(document, variables=None, ctx=None):
    return schema.execute(document, variables=variables, context_value=ctx or context())


CONFIRM = """
mutation C($input: ConfirmEmailVerificationInput!) {
  confirmEmailVerification(input: $input) {
    verified message errors { code message field retryAfterSeconds }
  }
}"""
RESEND = """
mutation R($input: ResendEmailVerificationInput!) {
  resendEmailVerification(input: $input) {
    message errors { code message field retryAfterSeconds }
  }
}"""
REGISTER = """
mutation G($input: RegisterInput!) {
  register(input: $input) {
    user { id email isActive } tokens { token refreshToken }
    verification { state deliveryStatus message }
    errors { code message field }
  }
}"""
LOGIN = """
mutation L($input: LoginInput!) {
  login(input: $input) { user { id } tokens { token } errors { code message } }
}"""


def test_tests_never_import_application_settings():
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "email_verification_settings"
    assert "saleor.settings" not in sys.modules


def test_schema_matches_canonical_contract():
    canonical = build_schema(CANONICAL.read_text())
    actual = schema.graphql_schema
    for name in (
        "RegisterPayload", "LoginPayload", "RegistrationVerification",
        "EmailVerificationError", "ConfirmEmailVerificationPayload",
        "ResendEmailVerificationPayload", "ConfirmEmailVerificationInput",
        "ResendEmailVerificationInput",
    ):
        expected_fields = canonical.type_map[name].fields
        got_fields = actual.type_map[name].fields
        assert {k: str(v.type) for k, v in got_fields.items()} == {
            k: str(v.type) for k, v in expected_fields.items()
        }, name
    for name in ("RegistrationVerificationState", "VerificationDeliveryStatus"):
        assert set(actual.type_map[name].values) == set(canonical.type_map[name].values)
    for field in ("confirmEmailVerification", "resendEmailVerification", "register", "login"):
        assert str(actual.mutation_type.fields[field].type) == str(
            canonical.mutation_type.fields[field].type
        )
    assert "verification" not in actual.type_map["LoginPayload"].fields


def test_token_helpers_are_bounded_and_digest_only():
    token = ev.new_token()
    assert ev.is_valid_token(token) and len(token) == 43
    assert ev.token_digest(token) != token and len(ev.token_digest(token)) == 64
    for bad in (None, "", "short", "x" * 44, "x" * 42 + "!", 5, {"a": 1}, ["x" * 43]):
        assert not ev.is_valid_token(bad)
    assert ev.is_valid_caller(CALLER)
    for bad in (None, "", "A" * 64, "a" * 63, "g" * 64):
        assert not ev.is_valid_caller(bad)
    assert ev.is_valid_email(EMAIL)
    assert not ev.is_valid_email("a" * 250 + "@x.test")
    assert not ev.is_valid_email("no-at-sign")


@pytest.mark.parametrize("secret,caller", [
    (None, CALLER), ("wrong-secret", CALLER), (SECRET, None), (SECRET, "nothex"),
])
def test_new_mutations_reject_untrusted_service_context(secret, caller):
    with patch.object(ev, "confirm") as confirm, patch.object(ev, "resend") as resend:
        confirmed = run(CONFIRM, {"input": {"token": TOKEN}}, context(secret, caller))
        resent = run(RESEND, {"input": {"email": EMAIL}}, context(secret, caller))
    confirm.assert_not_called()
    resend.assert_not_called()
    assert confirmed.data["confirmEmailVerification"]["verified"] is False
    assert confirmed.data["confirmEmailVerification"]["errors"][0]["code"] == "FORBIDDEN"
    assert resent.data["resendEmailVerification"]["errors"][0]["code"] == "FORBIDDEN"


def test_confirm_success_invalid_rate_limited_and_unavailable():
    with patch.object(ev, "confirm", return_value=ev.ConfirmResult(verified=True)) as confirm:
        ok = run(CONFIRM, {"input": {"token": TOKEN}}).data["confirmEmailVerification"]
    confirm.assert_called_once_with(TOKEN, CALLER)
    assert ok["verified"] is True and ok["errors"] == []

    with patch.object(ev, "confirm", return_value=ev.ConfirmResult()):
        bad = run(CONFIRM, {"input": {"token": TOKEN}}).data["confirmEmailVerification"]
    assert bad["verified"] is False
    assert [e["code"] for e in bad["errors"]] == ["INVALID_VERIFICATION_TOKEN"]

    limited = ev.ConfirmResult(rate_limited=True, retry_after=17)
    with patch.object(ev, "confirm", return_value=limited):
        out = run(CONFIRM, {"input": {"token": TOKEN}}).data["confirmEmailVerification"]
    assert out["errors"][0]["code"] == "RATE_LIMITED"
    assert out["errors"][0]["retryAfterSeconds"] == 17

    with patch.object(ev, "confirm", side_effect=DatabaseError("secret-host down")):
        out = run(CONFIRM, {"input": {"token": TOKEN}}).data["confirmEmailVerification"]
    assert out["errors"][0]["code"] == "VERIFICATION_UNAVAILABLE"
    assert "secret-host" not in json.dumps(out)


def test_resend_neutral_invalid_limited_unavailable_and_sends_only_when_issued():
    with patch.object(ev, "resend", return_value=ev.ResendResult()), \
            patch.object(ev, "deliver") as deliver:
        out = run(RESEND, {"input": {"email": EMAIL}}).data["resendEmailVerification"]
    assert out["message"] == ev.RESEND_MESSAGE and out["errors"] == []
    deliver.assert_not_called()

    issued = ev.ResendResult(issued=ev.Registration(user=7, token=TOKEN))
    with patch.object(ev, "resend", return_value=issued), \
            patch.object(ev, "deliver") as deliver:
        out = run(RESEND, {"input": {"email": "  PLAYER@Example.test "}}).data[
            "resendEmailVerification"]
    assert out["message"] == ev.RESEND_MESSAGE
    deliver.assert_called_once_with(7, EMAIL, TOKEN)

    with patch.object(ev, "resend", return_value=ev.ResendResult(invalid=True)):
        out = run(RESEND, {"input": {"email": "x"}}).data["resendEmailVerification"]
    assert out["errors"][0]["code"] == "INVALID" and out["errors"][0]["field"] == ["email"]

    with patch.object(ev, "resend", return_value=ev.ResendResult(rate_limited=True, retry_after=3)):
        out = run(RESEND, {"input": {"email": EMAIL}}).data["resendEmailVerification"]
    assert out["errors"][0]["code"] == "RATE_LIMITED" and out["errors"][0]["retryAfterSeconds"] == 3

    with patch.object(ev, "resend", side_effect=DatabaseError("boom")):
        out = run(RESEND, {"input": {"email": EMAIL}}).data["resendEmailVerification"]
    assert out["errors"][0]["code"] == "VERIFICATION_UNAVAILABLE"


REGISTER_INPUT = {"input": {
    "email": EMAIL, "password": "Synthetic-password-1!", "firstName": "A", "lastName": "B",
}}


@pytest.mark.parametrize("password", [
    "abcdef1!",
    "ABCDEF1!",
    "Abcdefg!",
    "Abcdefg1",
    "Abcdefg1 ",
    "Abcdefg1\t",
    "Abcdefg1\n",
    "Ábcdef1!",
    "ABCDEF1!ñ",
    "Abcdefg!١",
    "Abcdef1😀",
])
def test_register_rejects_missing_ascii_password_categories_before_side_effects(password):
    with patch.object(ev, "register_pending") as register_pending, \
            patch.object(ev, "deliver") as deliver:
        result = run(REGISTER, {"input": {**REGISTER_INPUT["input"], "password": password}})

    register_pending.assert_not_called()
    deliver.assert_not_called()
    payload = result.data["register"]
    assert payload["user"] is None
    assert payload["verification"] is None
    assert payload["errors"][0]["code"] == "INVALID"
    assert payload["errors"][0]["field"] == ["password"]


def test_register_passes_password_without_trimming():
    password = " Abcdef1! "
    user = SimpleNamespace(pk=5, email=EMAIL, first_name="A", last_name="B",
                           is_active=True, is_staff=False, is_superuser=False)
    with patch.object(ev, "register_pending", return_value=ev.Registration(user, TOKEN)) as register_pending, \
            patch.object(ev, "deliver", return_value=ev.DELIVERY_SUBMITTED):
        run(REGISTER, {"input": {**REGISTER_INPUT["input"], "password": password}})

    register_pending.assert_called_once_with(EMAIL, password, "A", "B")


def test_register_returns_pending_metadata_and_no_tokens():
    user = SimpleNamespace(pk=5, email=EMAIL, first_name="A", last_name="B",
                           is_active=True, is_staff=False, is_superuser=False)
    for delivered, expected in ((ev.DELIVERY_SUBMITTED, "SUBMITTED"),
                                (ev.DELIVERY_UNCONFIRMED, "UNCONFIRMED")):
        with patch.object(ev, "register_pending", return_value=ev.Registration(user, TOKEN)), \
                patch.object(ev, "deliver", return_value=delivered) as deliver:
            out = run(REGISTER, REGISTER_INPUT).data["register"]
        deliver.assert_called_once_with(5, EMAIL, TOKEN)
        assert out["tokens"] is None and out["errors"] == []
        assert out["verification"]["state"] == "PENDING"
        assert out["verification"]["deliveryStatus"] == expected
        assert TOKEN not in json.dumps(out)


def test_register_duplicate_never_sends():
    with patch.object(ev, "register_pending", side_effect=ev.DuplicateEmail), \
            patch.object(ev, "deliver") as deliver:
        out = run(REGISTER, REGISTER_INPUT).data["register"]
    deliver.assert_not_called()
    assert out["errors"][0]["code"] == "DUPLICATE_EMAIL" and out["verification"] is None


def login_user(active=True):
    return SimpleNamespace(pk=9, email=EMAIL, first_name="", last_name="",
                           is_active=active, is_staff=False, is_superuser=False)


LOGIN_INPUT = {"input": {"email": EMAIL, "password": "synthetic-password-1",
                         "guestCartId": "00000000-0000-0000-0000-000000000001"}}


def test_login_gates_pending_before_cart_merge_and_tokens():
    with patch("django.contrib.auth.authenticate", return_value=login_user()), \
            patch.object(ev, "is_eligible", return_value=False), \
            patch.object(ev, "is_pending", return_value=True), \
            patch.object(api, "_merge_guest_cart") as merge, \
            patch.object(api, "_auth_tokens") as tokens:
        out = run(LOGIN, LOGIN_INPUT).data["login"]
    merge.assert_not_called()
    tokens.assert_not_called()
    assert out["user"] is None and out["tokens"] is None
    assert out["errors"][0]["code"] == "EMAIL_NOT_VERIFIED"


@pytest.mark.parametrize("user", [None, login_user(active=False)])
def test_login_bad_or_inactive_credentials_stay_generic(user):
    with patch("django.contrib.auth.authenticate", return_value=user), \
            patch.object(ev, "is_eligible") as eligible, \
            patch.object(api, "_merge_guest_cart") as merge:
        out = run(LOGIN, LOGIN_INPUT).data["login"]
    eligible.assert_not_called()
    merge.assert_not_called()
    assert out["errors"][0]["code"] == "INVALID_CREDENTIALS"


def test_login_inactive_without_verification_row_fails_closed_as_generic():
    with patch("django.contrib.auth.authenticate", return_value=login_user()), \
            patch.object(ev, "is_eligible", return_value=False), \
            patch.object(ev, "is_pending", return_value=False), \
            patch.object(api, "_merge_guest_cart") as merge:
        out = run(LOGIN, LOGIN_INPUT).data["login"]
    merge.assert_not_called()
    assert out["errors"][0]["code"] == "INVALID_CREDENTIALS"


def test_login_eligible_still_merges_and_issues_tokens():
    with patch("django.contrib.auth.authenticate", return_value=login_user()), \
            patch.object(ev, "is_eligible", return_value=True), \
            patch.object(api, "_merge_guest_cart") as merge:
        out = run(LOGIN, LOGIN_INPUT).data["login"]
    merge.assert_called_once()
    assert out["errors"] == [] and out["tokens"]["token"]


def bearer_context(user_id, token_type="access"):
    import datetime
    import jwt
    from django.conf import settings
    now = datetime.datetime.now(datetime.timezone.utc)
    token = jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + datetime.timedelta(minutes=5),
         "token_type": token_type}, settings.SECRET_KEY, algorithm="HS256")
    return SimpleNamespace(headers={"Authorization": f"Bearer {token}"})


def test_bearer_identity_requires_verification_eligibility():
    from django.contrib.auth.models import User
    info = SimpleNamespace(context=bearer_context(4))
    user = login_user()
    with patch.object(User.objects, "get", return_value=user):
        with patch.object(ev, "is_eligible", return_value=False):
            assert api._authenticated_user(info) is None
        with patch.object(ev, "is_eligible", return_value=True):
            assert api._authenticated_user(info) is user


def test_delegated_opkey_identity_requires_verification_eligibility():
    user = SimpleNamespace(pk=3, is_active=True, is_staff=True, is_superuser=False)
    request = SimpleNamespace(headers={}, opkey_delegated=True, user=user)
    with patch.object(ev, "is_eligible", return_value=False):
        assert api._authenticated_user(SimpleNamespace(context=request)) is None
    with patch.object(ev, "is_eligible", return_value=True):
        assert api._authenticated_user(SimpleNamespace(context=request)) is user


@pytest.fixture
def fake_db():
    with patch.object(ev, "connection", MagicMock()), patch.object(ev, "_db_now"):
        yield


def test_delivery_sends_fragment_link_and_reports_submitted(fake_db):
    mail.outbox.clear()
    assert ev.deliver(1, EMAIL, TOKEN) == ev.DELIVERY_SUBMITTED
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [EMAIL]
    assert f"http://frontend.example.test/verify-email#token={TOKEN}" in message.body
    assert "?token" not in message.body and TOKEN not in message.subject


@pytest.mark.parametrize("accepted", [True, False])
def test_django_smtp_transport_uses_only_owned_loopback_capture(fake_db, caplog, accepted):
    import socketserver
    import threading
    from django.test import override_settings

    messages = []

    class Capture(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 loopback capture\r\n")
            while line := self.rfile.readline():
                command = line.split(b" ", 1)[0].strip().upper()
                if command == b"DATA":
                    self.wfile.write(b"354 send message\r\n")
                    body = []
                    while (part := self.rfile.readline()) not in (b".\r\n", b""):
                        body.append(part)
                    messages.append(b"".join(body))
                    self.wfile.write(b"250 captured\r\n" if accepted else b"451 not accepted\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 closing\r\n")
                    return
                else:
                    self.wfile.write(b"250 loopback\r\n")

    with socketserver.TCPServer(("127.0.0.1", 0), Capture) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            with override_settings(
                EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                EMAIL_HOST="127.0.0.1", EMAIL_PORT=server.server_address[1],
                EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD="",
                EMAIL_USE_TLS=False, EMAIL_USE_SSL=False, EMAIL_TIMEOUT=2,
            ):
                result = ev.deliver(1, EMAIL, TOKEN)
        finally:
            server.shutdown()
            thread.join(timeout=3)
        assert not thread.is_alive()
    assert result == (ev.DELIVERY_SUBMITTED if accepted else ev.DELIVERY_UNCONFIRMED)
    assert len(messages) == 1
    assert EMAIL.encode() in messages[0] and TOKEN.encode() in messages[0]
    assert "Message-ID:" in messages[0].decode()
    assert TOKEN not in caplog.text and EMAIL not in caplog.text


@pytest.mark.parametrize("failure", [
    smtplib.SMTPServerDisconnected("server detail"), TimeoutError("timeout detail"),
    ConnectionRefusedError("refused detail"), smtplib.SMTPAuthenticationError(535, b"cred detail"),
])
def test_delivery_failure_is_logged_without_secrets(fake_db, caplog, failure):
    caplog.set_level(logging.DEBUG)
    with patch.object(ev, "send_mail", side_effect=failure):
        assert ev.deliver(1, EMAIL, TOKEN) == ev.DELIVERY_UNCONFIRMED
    text = caplog.text
    assert "delivery failed" in text
    for forbidden in (TOKEN, EMAIL, "detail", "Traceback", "535"):
        assert forbidden not in text
    assert not any(r.exc_info for r in caplog.records)


@pytest.mark.parametrize("origin", ["", "frontend.example.test", "ftp://x.test",
                                    "http://x.test/path", "http://x.test?q=1"])
def test_delivery_fails_closed_on_untrusted_origin(fake_db, settings, origin, caplog):
    settings.EMAIL_VERIFICATION_FRONTEND_ORIGIN = origin
    mail.outbox.clear()
    assert ev.deliver(1, EMAIL, TOKEN) == ev.DELIVERY_UNCONFIRMED
    assert mail.outbox == [] and TOKEN not in caplog.text


@pytest.fixture
def settings():
    from django.conf import settings as django_settings
    original = getattr(django_settings, "EMAIL_VERIFICATION_FRONTEND_ORIGIN")
    yield django_settings
    django_settings.EMAIL_VERIFICATION_FRONTEND_ORIGIN = original


def view_response(body, **extra):
    request = RequestFactory().post("/graphql/", data=json.dumps(body),
                                    content_type="application/json", **extra)
    request.headers  # noqa: B018 - exercised by the view
    return SanitizedGraphQLView.as_view(schema=schema)(request)


@pytest.mark.parametrize("variables", [
    {"input": {"token": {"nested": "LEAK-ME"}}},
    {"input": {"token": ["LEAK-ME"]}},
    {"input": {"token": 12345, "extra": "LEAK-ME"}},
    {"input": "LEAK-ME"},
    {"input": None, "LEAK-ME": 1},
])
def test_view_sanitizes_malformed_verification_variables(variables):
    response = view_response({"query": CONFIRM, "variables": variables})
    body = response.content.decode()
    assert "LEAK-ME" not in body and "12345" not in body
    assert response.status_code == 400
    assert json.loads(body)["errors"]


def test_view_sanitizes_aliased_and_multi_operation_documents():
    document = """
    query Other { __typename }
    mutation A($input: ConfirmEmailVerificationInput!) {
      first: confirmEmailVerification(input: $input) { verified }
      second: confirmEmailVerification(input: $input) { verified }
    }"""
    response = view_response({"query": document, "operationName": "A",
                              "variables": {"input": {"token": {"x": "LEAK-ME-TOO"}}}})
    assert "LEAK-ME-TOO" not in response.content.decode()
    assert response.status_code == 400


def test_view_sanitizes_unexpected_resolver_failure():
    with patch.object(ev, "confirm", side_effect=RuntimeError("provider detail LEAK")):
        response = view_response(
            {"query": CONFIRM, "variables": {"input": {"token": TOKEN}}},
            HTTP_X_INTERNAL_SECRET=SECRET, HTTP_X_VERIFICATION_CALLER=CALLER,
        )
    body = response.content.decode()
    assert "LEAK" not in body and TOKEN not in body


def test_view_leaves_unrelated_errors_unchanged():
    response = view_response({"query": "{ doesNotExist }"})
    assert "doesNotExist" in response.content.decode()
