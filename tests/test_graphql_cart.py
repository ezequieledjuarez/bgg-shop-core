"""CU-C04 cart behavior with an in-memory cursor; no live PostgreSQL required."""
import os
import sys
import uuid
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from saleor.graphql import api
from saleor.graphql.api import schema


ADD_MUTATION = """
mutation($cartId: ID, $bggId: Int!, $quantity: Int!) {
  addToCart(cartId: $cartId, bggId: $bggId, quantity: $quantity) {
    cart { cartId subtotal lines { bggId quantity inventoryQuantity unitPrice subtotal } }
    userErrors { code field message }
  }
}
"""
UPDATE_MUTATION = """
mutation($cartId: ID!, $bggId: Int!, $quantity: Int!) {
  updateCartQuantity(cartId: $cartId, bggId: $bggId, quantity: $quantity) {
    cart { cartId subtotal lines { bggId quantity subtotal } }
    userErrors { code field message }
  }
}
"""
REMOVE_MUTATION = """
mutation($cartId: ID!, $bggId: Int!) {
  removeFromCart(cartId: $cartId, bggId: $bggId) {
    cart { cartId subtotal lines { bggId quantity } }
    userErrors { code }
  }
}
"""
LOGIN_MUTATION = """
mutation($input: LoginInput!) {
  login(input: $input) { user { id } errors { code } }
}
"""


class CartCursor:
    """Minimal SQL behavior for exercising cart resolver persistence paths."""

    def __init__(self):
        self.carts = {}
        self.lines = {}
        self.products = {12: ("Game", 10, 12.5), 13: ("Out of stock", 0, 8.0)}
        self.result = None
        self.results = []

    @staticmethod
    def _id(value):
        return str(value)

    def execute(self, sql, params=None):
        params = params or []
        statement = " ".join(sql.split())
        self.result = None
        self.results = []

        if statement.startswith("SELECT cart_id FROM shopping_cart WHERE user_id"):
            user_id = params[0]
            self.result = next(
                ((cart_id,) for cart_id, owner in self.carts.items() if owner == user_id),
                None,
            )
        elif statement.startswith("INSERT INTO shopping_cart ("):
            cart_id = self._id(params[0])
            owner = params[1] if len(params) > 1 else None
            if owner is None or not any(value == owner for value in self.carts.values()):
                self.carts[cart_id] = owner
        elif statement.startswith("SELECT cart_id FROM shopping_cart WHERE cart_id"):
            cart_id = self._id(params[0])
            if self.carts.get(cart_id, "missing") is None:
                self.result = (cart_id,)
        elif statement.startswith("SELECT p.bgg_id, p.name"):
            cart_id = self._id(params[0])
            for (line_cart_id, bgg_id), quantity in self.lines.items():
                if line_cart_id == cart_id and bgg_id in self.products:
                    name, stock, price = self.products[bgg_id]
                    self.results.append((bgg_id, name, quantity, stock, price))
        elif statement.startswith("SELECT quantity FROM product_boardgame"):
            product = self.products.get(params[0])
            self.result = (product[1],) if product else None
        elif statement.startswith("SELECT quantity FROM shopping_cart_line"):
            line = self.lines.get((self._id(params[0]), params[1]))
            self.result = (line,) if line is not None else None
        elif statement.startswith("INSERT INTO shopping_cart_line"):
            self.lines[(self._id(params[0]), params[1])] = params[2]
        elif statement.startswith("DELETE FROM shopping_cart_line"):
            self.lines.pop((self._id(params[0]), params[1]), None)
        elif statement.startswith("DELETE FROM shopping_cart WHERE"):
            cart_id = self._id(params[0])
            self.carts.pop(cart_id, None)
            self.lines = {
                key: value for key, value in self.lines.items() if key[0] != cart_id
            }
        elif statement.startswith("SELECT bgg_id, quantity FROM shopping_cart_line"):
            cart_id = self._id(params[0])
            self.results = [
                (bgg_id, quantity)
                for (line_cart_id, bgg_id), quantity in self.lines.items()
                if line_cart_id == cart_id
            ]
        else:
            raise AssertionError(f"Unexpected SQL in cart test: {statement}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.results


def _execute(query, cursor, variables, user=None):
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    with patch.object(api, "connection", connection), \
         patch.object(api.transaction, "atomic", return_value=nullcontext()), \
         patch.object(api, "_authenticated_user", return_value=user):
        result = schema.execute(
            query,
            variable_values=variables,
            context_value=MagicMock(),
        )
    assert result.errors is None
    return result.data


def _codes(payload):
    return [error["code"] for error in payload["userErrors"]]


def test_cart_contract_exposes_inventory_and_cart_operations():
    document = str(schema)
    assert "quantity: Int!" in document
    assert "cart(cartId: ID): Cart!" in document
    assert "addToCart(bggId: Int!, cartId: ID, quantity: Int!): CartPayload!" in document
    assert "updateCartQuantity(bggId: Int!, cartId: ID, quantity: Int!): CartPayload!" in document
    assert "removeFromCart(bggId: Int!, cartId: ID): CartPayload!" in document
    assert "guestCartId: ID" in document


def test_anonymous_add_creates_opaque_cart_and_calculates_current_price_total():
    cursor = CartCursor()
    data = _execute(
        ADD_MUTATION,
        cursor,
        {"cartId": None, "bggId": 12, "quantity": 2},
    )["addToCart"]

    assert uuid.UUID(data["cart"]["cartId"])
    assert data["cart"]["subtotal"] == 25.0
    assert data["cart"]["lines"] == [{
        "bggId": 12,
        "quantity": 2,
        "inventoryQuantity": 10,
        "unitPrice": 12.5,
        "subtotal": 25.0,
    }]
    assert data["userErrors"] == []


def test_stock_excess_is_capped_with_warning_then_remove_recalculates_total():
    cursor = CartCursor()
    created = _execute(
        ADD_MUTATION, cursor, {"cartId": None, "bggId": 12, "quantity": 3}
    )["addToCart"]
    cart_id = created["cart"]["cartId"]

    capped = _execute(
        ADD_MUTATION, cursor, {"cartId": cart_id, "bggId": 12, "quantity": 8}
    )["addToCart"]
    assert _codes(capped) == ["INSUFFICIENT_STOCK"]
    assert capped["cart"]["lines"][0]["quantity"] == 10

    updated = _execute(
        UPDATE_MUTATION, cursor, {"cartId": cart_id, "bggId": 12, "quantity": 4}
    )["updateCartQuantity"]
    assert updated["cart"]["subtotal"] == 50.0
    assert updated["userErrors"] == []

    removed = _execute(
        REMOVE_MUTATION, cursor, {"cartId": cart_id, "bggId": 12}
    )["removeFromCart"]
    assert removed["cart"]["lines"] == []
    assert removed["cart"]["subtotal"] == 0.0


def test_account_cart_uses_validated_user_not_supplied_cart_id():
    cursor = CartCursor()
    user = MagicMock(pk=42)
    supplied_cart_id = str(uuid.uuid4())
    data = _execute(
        ADD_MUTATION,
        cursor,
        {"cartId": supplied_cart_id, "bggId": 12, "quantity": 1},
        user=user,
    )["addToCart"]

    account_cart_id = data["cart"]["cartId"]
    assert cursor.carts[account_cart_id] == 42
    assert account_cart_id != supplied_cart_id
    assert len(cursor.carts) == 1


def test_guest_cart_merge_sums_account_quantity_and_caps_to_inventory():
    cursor = CartCursor()
    guest_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    cursor.carts[guest_id] = None
    cursor.carts[account_id] = 42
    cursor.lines[(guest_id, 12)] = 8
    cursor.lines[(account_id, 12)] = 4
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch.object(api, "connection", connection), \
         patch.object(api.transaction, "atomic", return_value=nullcontext()):
        api._merge_guest_cart(guest_id, MagicMock(pk=42))

    assert cursor.lines[(account_id, 12)] == 10
    assert guest_id not in cursor.carts
    assert (guest_id, 12) not in cursor.lines


def test_guest_merge_removes_account_line_when_current_inventory_is_zero():
    cursor = CartCursor()
    guest_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    cursor.carts[guest_id] = None
    cursor.carts[account_id] = 42
    cursor.lines[(guest_id, 13)] = 1
    cursor.lines[(account_id, 13)] = 1
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    with patch.object(api, "connection", connection), \
         patch.object(api.transaction, "atomic", return_value=nullcontext()):
        api._merge_guest_cart(guest_id, MagicMock(pk=42))

    assert (account_id, 13) not in cursor.lines


def test_login_merges_guest_cart_only_after_account_authentication():
    user = MagicMock(
        pk=42,
        email="buyer@example.test",
        first_name="Buyer",
        last_name="",
        is_active=True,
    )
    guest_cart_id = str(uuid.uuid4())
    with patch(
        "django.contrib.auth.authenticate",
        return_value=user,
    ), patch.object(api, "_merge_guest_cart") as merge:
        result = schema.execute(
            LOGIN_MUTATION,
            variable_values={
                "input": {
                    "email": "buyer@example.test",
                    "password": "password",
                    "guestCartId": guest_cart_id,
                }
            },
            context_value=MagicMock(),
        )

    assert result.errors is None
    assert result.data["login"]["user"]["id"] == "42"
    merge.assert_called_once_with(guest_cart_id, user)
