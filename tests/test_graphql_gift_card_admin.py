import hashlib
import os
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
django.setup()

from saleor.graphql.api import schema


MUTATION = """
mutation IssueGiftCard($amount: String!) {
  createGiftCard(amount: $amount) {
    giftCard { id code amount currency }
    userErrors { code message }
  }
}
"""


def execute(amount, user, connection):
    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch(
        "saleor.graphql.api.connection", connection
    ):
        return schema.execute(MUTATION, variable_values={"amount": amount})


def test_admin_issues_card_with_hash_only():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    card_id = uuid.uuid4()
    cursor.fetchone.return_value = (card_id,)
    user = SimpleNamespace(is_staff=True, is_superuser=False)
    result = execute("20000.00", user, connection)

    assert result.errors is None
    payload = result.data["createGiftCard"]
    assert payload["userErrors"] == []
    card = payload["giftCard"]
    assert card["id"] == str(card_id)
    assert card["amount"] == "20000.00"
    assert card["currency"] == "ARS"
    assert len(card["code"]) == 64
    parameters = cursor.execute.call_args.args[1]
    assert hashlib.sha256(card["code"].encode()).digest() in parameters
    assert card["code"] not in parameters


def test_superuser_can_issue_card():
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (uuid.uuid4(),)
    result = execute("1.00", SimpleNamespace(is_staff=False, is_superuser=True), connection)
    assert result.errors is None
    assert result.data["createGiftCard"]["userErrors"] == []


def test_database_error_does_not_reveal_details():
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("private database detail")
    result = execute("1.00", SimpleNamespace(is_staff=True, is_superuser=False), connection)
    assert result.errors is None
    assert result.data["createGiftCard"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database detail" not in str(result.data)


@pytest.mark.parametrize("user", [None, SimpleNamespace(is_staff=False, is_superuser=False)])
def test_non_admin_cannot_issue_card(user):
    connection = MagicMock()
    result = execute("20000.00", user, connection)
    assert result.errors is None
    assert result.data["createGiftCard"]["giftCard"] is None
    assert result.data["createGiftCard"]["userErrors"][0]["code"] in {"UNAUTHENTICATED", "FORBIDDEN"}
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("amount", ["0", "-1", "1.001", "NaN", "10000000000", ""])
def test_invalid_amount_is_rejected(amount):
    connection = MagicMock()
    result = execute(amount, SimpleNamespace(is_staff=True, is_superuser=False), connection)
    assert result.errors is None
    assert result.data["createGiftCard"]["userErrors"][0]["code"] == "INVALID_AMOUNT"
    connection.cursor.assert_not_called()


REVIEW_QUERY = """
query Reviews($page: Int, $pageSize: Int) {
  adminPaymentReviews(page: $page, pageSize: $pageSize) {
    items { orderId paymentId userId creditOrderId detectedAt resolution { actorId action reason resolvedAt } release { actorId reason releasedAt } }
    hasNext
    userErrors { code message }
  }
}
"""


ATTEMPTS_QUERY = """
query Attempts($page: Int, $pageSize: Int) {
    adminPaymentAttempts(page: $page, pageSize: $pageSize) {
        items { attemptId orderId userId orderStatus preferenceId }
        hasNext
        userErrors { code message }
    }
}
"""


@pytest.mark.parametrize("user,code", [
        (None, "UNAUTHENTICATED"),
        (SimpleNamespace(is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_payment_attempt_list_requires_admin(user, code):
        connection = MagicMock()
        with patch("saleor.graphql.api._authenticated_user", return_value=user), patch("saleor.graphql.api.connection", connection):
                result = schema.execute(ATTEMPTS_QUERY)
        assert result.errors is None
        assert result.data["adminPaymentAttempts"]["userErrors"][0]["code"] == code
        assert result.data["adminPaymentAttempts"]["items"] == []
        connection.cursor.assert_not_called()


def test_payment_attempt_list_paginates_and_sanitizes_errors():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    order_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cursor.fetchall.return_value = [(attempt_id, order_id, 42, "pending_payment", None),
                                    (uuid.uuid4(), uuid.uuid4(), 43, "rejected", "pref-2")]
    admin = SimpleNamespace(is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(ATTEMPTS_QUERY, variable_values={"page": 2, "pageSize": 1})
        assert result.errors is None
        payload = result.data["adminPaymentAttempts"]
        assert payload["userErrors"] == []
        assert payload["hasNext"] is True
        assert payload["items"] == [{"attemptId": str(attempt_id), "orderId": str(order_id), "userId": "42", "orderStatus": "pending_payment", "preferenceId": None}]
        assert cursor.execute.call_args.args[1] == [2, 1]
        cursor.execute.side_effect = RuntimeError("private provider detail")
        failed = schema.execute(ATTEMPTS_QUERY)
    assert failed.errors is None
    assert failed.data["adminPaymentAttempts"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private provider detail" not in str(failed.data)


@pytest.mark.parametrize("variables", [{"page": 0}, {"pageSize": 0}, {"pageSize": 101}, {"page": 10001}])
def test_payment_attempt_list_rejects_invalid_pagination(variables):
    connection = MagicMock()
    admin = SimpleNamespace(is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(ATTEMPTS_QUERY, variable_values=variables)
    assert result.errors is None
    assert result.data["adminPaymentAttempts"]["userErrors"][0]["code"] == "INVALID"
    connection.cursor.assert_not_called()


def test_payment_attempt_list_sanitizes_authentication_errors():
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError("private token")):
        result = schema.execute(ATTEMPTS_QUERY)
    assert result.errors is None
    assert result.data["adminPaymentAttempts"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private token" not in str(result.data)


def execute_reviews(user, connection, variables=None):
    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch(
        "saleor.graphql.api.connection", connection
    ):
        return schema.execute(REVIEW_QUERY, variable_values=variables or {})


@pytest.mark.parametrize("user,code", [
    (None, "UNAUTHENTICATED"),
    (SimpleNamespace(is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_payment_review_list_requires_admin(user, code):
    connection = MagicMock()
    result = execute_reviews(user, connection)
    assert result.errors is None
    assert result.data["adminPaymentReviews"]["userErrors"][0]["code"] == code
    assert result.data["adminPaymentReviews"]["items"] == []
    connection.cursor.assert_not_called()


def test_admin_can_list_payment_reviews_without_leaking_database_errors():
    import datetime

    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    order_id, credit_order_id = uuid.uuid4(), uuid.uuid4()
    cursor.fetchall.return_value = [(
        order_id, "payment-1", 42, credit_order_id, datetime.datetime(2026, 1, 1),
        7, "Reviewed", "Contacted buyer", datetime.datetime(2026, 1, 2),
        8, "Verified", datetime.datetime(2026, 1, 3),
    )]
    admin = SimpleNamespace(is_staff=True, is_superuser=False)
    result = execute_reviews(admin, connection, {"page": 2, "pageSize": 1})
    assert result.errors is None
    payload = result.data["adminPaymentReviews"]
    assert payload["userErrors"] == []
    assert payload["items"][0]["orderId"] == str(order_id)
    assert payload["items"][0]["creditOrderId"] == str(credit_order_id)
    assert payload["items"][0]["resolution"]["actorId"] == "7"
    assert payload["items"][0]["resolution"]["reason"] == "Contacted buyer"
    assert payload["items"][0]["release"]["actorId"] == "8"
    assert payload["items"][0]["release"]["reason"] == "Verified"
    assert cursor.execute.call_args.args[1] == [2, 1]

    cursor.execute.side_effect = RuntimeError("private database detail")
    result = execute_reviews(admin, connection)
    assert result.errors is None
    assert result.data["adminPaymentReviews"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database detail" not in str(result.data)


def test_payment_review_list_sanitizes_authentication_errors():
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError("private token")):
        result = schema.execute(REVIEW_QUERY)
    assert result.errors is None
    assert result.data["adminPaymentReviews"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private token" not in str(result.data)


@pytest.mark.parametrize("variables", [{"page": 0}, {"pageSize": 0}, {"pageSize": 101}, {"page": 10001}])
def test_payment_review_list_rejects_invalid_pagination(variables):
    connection = MagicMock()
    result = execute_reviews(SimpleNamespace(is_staff=True, is_superuser=False), connection, variables)
    assert result.errors is None
    assert result.data["adminPaymentReviews"]["userErrors"][0]["code"] == "INVALID"
    connection.cursor.assert_not_called()


RESOLVE_REVIEW = """
mutation Resolve($order: ID!, $payment: ID!, $action: String!, $reason: String!) {
  resolvePaymentReview(orderId: $order, paymentId: $payment, action: $action, reason: $reason) {
    resolution { actorId action reason resolvedAt }
    userErrors { code message }
  }
}
"""


@pytest.mark.parametrize("user,code", [
    (None, "UNAUTHENTICATED"),
    (SimpleNamespace(pk=2, is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_resolve_payment_review_requires_admin(user, code):
    connection = MagicMock()
    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(RESOLVE_REVIEW, variable_values={
            "order": str(uuid.uuid4()), "payment": "payment-1", "action": "Reviewed", "reason": "Resolved manually",
        })
    assert result.errors is None
    assert result.data["resolvePaymentReview"]["userErrors"][0]["code"] == code
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"order": "bad"}, {"payment": "bad/id"}, {"action": " "}, {"reason": " "},
    {"action": "a" * 201}, {"reason": "b" * 1001}, {"reason": "contains\ncontrol"},
])
def test_resolve_payment_review_rejects_invalid_input(changes):
    connection = MagicMock()
    values = {"order": str(uuid.uuid4()), "payment": "payment-1", "action": "Reviewed", "reason": "Resolved manually"}
    values.update(changes)
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(RESOLVE_REVIEW, variable_values=values)
    assert result.errors is None
    assert result.data["resolvePaymentReview"]["userErrors"][0]["code"] == "INVALID"
    connection.cursor.assert_not_called()


def test_resolve_payment_review_sanitizes_internal_errors_and_missing_case():
    values = {"order": str(uuid.uuid4()), "payment": "payment-1", "action": "Reviewed", "reason": "Resolved manually"}
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api.transaction.atomic", return_value=nullcontext()):
        missing = schema.execute(RESOLVE_REVIEW, variable_values=values)
        cursor.execute.side_effect = RuntimeError("private database detail")
        failed = schema.execute(RESOLVE_REVIEW, variable_values=values)
    assert missing.errors is None and missing.data["resolvePaymentReview"]["userErrors"][0]["code"] == "NOT_FOUND"
    assert failed.errors is None and failed.data["resolvePaymentReview"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database detail" not in str(failed.data)
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError("private auth detail")):
        failed_auth = schema.execute(RESOLVE_REVIEW, variable_values=values)
    assert failed_auth.errors is None and failed_auth.data["resolvePaymentReview"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private auth detail" not in str(failed_auth.data)


RELEASE_REVIEW = """
mutation Release($order: ID!, $payment: ID!, $reason: String!) {
  releasePaymentReview(orderId: $order, paymentId: $payment, reason: $reason) {
    release { actorId reason releasedAt }
    userErrors { code message }
  }
}
"""


@pytest.mark.parametrize("user,code", [
    (None, "UNAUTHENTICATED"),
    (SimpleNamespace(pk=2, is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_release_payment_review_requires_admin(user, code):
    connection = MagicMock()
    values = {"order": str(uuid.uuid4()), "payment": "payment-1", "reason": "Verified"}
    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(RELEASE_REVIEW, variable_values=values)
    assert result.errors is None
    assert result.data["releasePaymentReview"]["userErrors"][0]["code"] == code
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"order": "bad"}, {"payment": "bad/id"}, {"reason": " "},
    {"reason": "b" * 1001}, {"reason": "contains\ncontrol"},
])
def test_release_payment_review_rejects_invalid_input(changes):
    connection = MagicMock()
    values = {"order": str(uuid.uuid4()), "payment": "payment-1", "reason": "Verified"}
    values.update(changes)
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(RELEASE_REVIEW, variable_values=values)
    assert result.errors is None
    assert result.data["releasePaymentReview"]["userErrors"][0]["code"] == "INVALID"
    connection.cursor.assert_not_called()


def test_release_payment_review_sanitizes_internal_errors_and_missing_case():
    values = {"order": str(uuid.uuid4()), "payment": "payment-1", "reason": "Verified"}
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api.transaction.atomic", return_value=nullcontext()):
        missing = schema.execute(RELEASE_REVIEW, variable_values=values)
        cursor.execute.side_effect = RuntimeError("private database detail")
        failed = schema.execute(RELEASE_REVIEW, variable_values=values)
    assert missing.errors is None and missing.data["releasePaymentReview"]["userErrors"][0]["code"] == "NOT_FOUND"
    assert failed.errors is None and failed.data["releasePaymentReview"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database detail" not in str(failed.data)
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError("private auth detail")):
        failed_auth = schema.execute(RELEASE_REVIEW, variable_values=values)
    assert failed_auth.errors is None and failed_auth.data["releasePaymentReview"]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private auth detail" not in str(failed_auth.data)


OBSERVE_ATTEMPT = """
mutation Observe($order: ID!, $attempt: ID!, $request: ID!, $statement: String!) {
  recordPaymentAttemptObservation(orderId: $order, attemptId: $attempt, requestId: $request, statement: $statement) {
    observation { id } userErrors { code message }
  }
}
"""

ATTEMPT_HISTORY = """
query History($attempt: ID!, $page: Int, $pageSize: Int) {
  adminPaymentAttemptObservations(attemptId: $attempt, page: $page, pageSize: $pageSize) {
    items { id } hasNext userErrors { code message }
  }
}
"""


@pytest.mark.parametrize("operation,field", [
    (OBSERVE_ATTEMPT, "recordPaymentAttemptObservation"),
    (ATTEMPT_HISTORY, "adminPaymentAttemptObservations"),
])
@pytest.mark.parametrize("user,code", [
    (None, "UNAUTHENTICATED"),
    (SimpleNamespace(pk=2, is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_attempt_audit_requires_admin(operation, field, user, code):
    connection = MagicMock()
    values = {"order": str(uuid.uuid4()), "attempt": str(uuid.uuid4()), "request": str(uuid.uuid4()), "statement": "Investigated"}
    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(operation, variable_values=values)
    assert result.errors is None
    assert result.data[field]["userErrors"][0]["code"] == code
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"order": "bad"}, {"attempt": "bad"}, {"request": "bad"},
    {"statement": " "}, {"statement": "a" * 1001}, {"statement": "line\nbreak"},
])
def test_attempt_observation_rejects_invalid_input(changes):
    connection = MagicMock()
    values = {"order": str(uuid.uuid4()), "attempt": str(uuid.uuid4()), "request": str(uuid.uuid4()), "statement": "Investigated"}
    values.update(changes)
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(OBSERVE_ATTEMPT, variable_values=values)
    assert result.errors is None
    assert result.data["recordPaymentAttemptObservation"]["userErrors"][0]["code"] == "INVALID"
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("changes", [{"attempt": "bad"}, {"page": 0}, {"pageSize": 101}])
def test_attempt_observation_history_rejects_invalid_input(changes):
    connection = MagicMock()
    values = {"attempt": str(uuid.uuid4()), **changes}
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(ATTEMPT_HISTORY, variable_values=values)
    assert result.errors is None
    assert result.data["adminPaymentAttemptObservations"]["userErrors"][0]["code"] == "INVALID"
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("operation,field", [
    (OBSERVE_ATTEMPT, "recordPaymentAttemptObservation"),
    (ATTEMPT_HISTORY, "adminPaymentAttemptObservations"),
])
def test_attempt_audit_sanitizes_internal_errors(operation, field):
    values = {"order": str(uuid.uuid4()), "attempt": str(uuid.uuid4()), "request": str(uuid.uuid4()), "statement": "Investigated"}
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("private database detail")
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api.transaction.atomic", return_value=nullcontext()):
        result = schema.execute(operation, variable_values=values)
    assert result.errors is None and result.data[field]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database detail" not in str(result.data)
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError("private auth detail")):
        failed_auth = schema.execute(operation, variable_values=values)
    assert failed_auth.errors is None and failed_auth.data[field]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private auth detail" not in str(failed_auth.data)


def test_attempt_observation_history_paginates():
    import datetime

    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    attempt_id, first_id, second_id, order_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cursor.fetchall.return_value = [
        (first_id, attempt_id, order_id, 1, None, "First", datetime.datetime(2026, 1, 2)),
        (second_id, attempt_id, order_id, 1, "pref-1", "Second", datetime.datetime(2026, 1, 1)),
    ]
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(ATTEMPT_HISTORY, variable_values={"attempt": str(attempt_id), "page": 2, "pageSize": 1})
    assert result.errors is None
    payload = result.data["adminPaymentAttemptObservations"]
    assert payload["userErrors"] == []
    assert payload["hasNext"] is True
    assert payload["items"] == [{"id": str(first_id)}]
    assert cursor.execute.call_args.args[1] == [attempt_id, 2, 1]


RELEASE_ATTEMPT = """
mutation Release($order: ID!, $attempt: ID!, $observation: ID!, $reason: String!, $risk: Boolean!) {
  releasePaymentAttempt(orderId: $order, attemptId: $attempt, observationId: $observation, reason: $reason, acknowledgeRisk: $risk) {
    release { attemptId } userErrors { code message }
  }
}
"""

ATTEMPT_RELEASE_QUERY = """
query Release($attempt: ID!) {
  adminPaymentAttemptRelease(attemptId: $attempt) {
    release { attemptId } providerResponse { preferenceId } userErrors { code message }
  }
}
"""


@pytest.mark.parametrize("operation,field", [
    (RELEASE_ATTEMPT, "releasePaymentAttempt"),
    (ATTEMPT_RELEASE_QUERY, "adminPaymentAttemptRelease"),
])
@pytest.mark.parametrize("user,code", [
    (None, "UNAUTHENTICATED"),
    (SimpleNamespace(pk=2, is_staff=False, is_superuser=False), "FORBIDDEN"),
])
def test_attempt_release_requires_admin(operation, field, user, code):
    connection = MagicMock()
    values = {"order": str(uuid.uuid4()), "attempt": str(uuid.uuid4()), "observation": str(uuid.uuid4()), "reason": "Investigated", "risk": True}
    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(operation, variable_values=values)
    assert result.errors is None
    assert result.data[field]["userErrors"][0]["code"] == code
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"order": "bad"}, {"attempt": "bad"}, {"observation": "bad"}, {"risk": False},
    {"reason": " "}, {"reason": "a" * 1001}, {"reason": "line\nbreak"},
])
def test_attempt_release_rejects_invalid_input(changes):
    connection = MagicMock()
    values = {"order": str(uuid.uuid4()), "attempt": str(uuid.uuid4()), "observation": str(uuid.uuid4()), "reason": "Investigated", "risk": True}
    values.update(changes)
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection):
        result = schema.execute(RELEASE_ATTEMPT, variable_values=values)
    assert result.errors is None
    assert result.data["releasePaymentAttempt"]["userErrors"][0]["code"] == "INVALID"
    connection.cursor.assert_not_called()


@pytest.mark.parametrize("operation,field", [
    (RELEASE_ATTEMPT, "releasePaymentAttempt"),
    (ATTEMPT_RELEASE_QUERY, "adminPaymentAttemptRelease"),
])
def test_attempt_release_sanitizes_internal_errors(operation, field):
    values = {"order": str(uuid.uuid4()), "attempt": str(uuid.uuid4()), "observation": str(uuid.uuid4()), "reason": "Investigated", "risk": True}
    admin = SimpleNamespace(pk=1, is_staff=True, is_superuser=False)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("private database detail")
    with patch("saleor.graphql.api._authenticated_user", return_value=admin), patch("saleor.graphql.api.connection", connection), patch("saleor.graphql.api.transaction.atomic", return_value=nullcontext()):
        result = schema.execute(operation, variable_values=values)
    assert result.errors is None and result.data[field]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private database detail" not in str(result.data)
    with patch("saleor.graphql.api._authenticated_user", side_effect=RuntimeError("private auth detail")):
        failed_auth = schema.execute(operation, variable_values=values)
    assert failed_auth.errors is None and failed_auth.data[field]["userErrors"][0]["code"] == "SERVER_ERROR"
    assert "private auth detail" not in str(failed_auth.data)