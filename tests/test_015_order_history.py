from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saleor.graphql.api import schema


@pytest.mark.parametrize(
    "is_staff,is_superuser",
    [(False, False), (True, False), (True, True)],
)
def test_authenticated_empty_order_history_returns_empty_list_for_own_user(
    is_staff, is_superuser
):
    user = SimpleNamespace(
        pk=41,
        is_active=True,
        is_staff=is_staff,
        is_superuser=is_superuser,
    )
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch("saleor.graphql.api._authenticated_user", return_value=user), patch(
        "saleor.graphql.api.connection", connection
    ):
        result = schema.execute("{ myOrders { id } }")

    assert result.errors is None
    assert result.data["myOrders"] == []
    sql, parameters = cursor.execute.call_args.args
    assert "WHERE user_id = %s" in sql
    assert "ORDER BY created_at DESC" in sql
    assert parameters == [user.pk]
