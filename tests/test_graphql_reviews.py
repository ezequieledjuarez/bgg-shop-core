"""CU-C03 createReview: no live DB; cursor/connection and auth are mocked."""
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

import django
import jwt
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from django.conf import settings
from django.db import IntegrityError

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
    info = MagicMock()
    info.context.headers = {"Authorization": header}
    assert api._authenticated_user(info) is None


def test_authenticated_user_rejects_refresh_token():
    info = MagicMock()
    info.context.headers = {"Authorization": f"Bearer {_token('refresh')}"}
    assert api._authenticated_user(info) is None
