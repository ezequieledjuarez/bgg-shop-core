"""Tests for purchase order checkout and payment confirmation."""

import os
import sys
import uuid
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import django
import pytest
from django.test import RequestFactory, override_settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from saleor.graphql import orders

USER = SimpleNamespace(pk=41)
ORDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000123")
CART_ID = uuid.UUID("00000000-0000-0000-0000-000000000456")


class OrderCursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = 0
        self.description = []

    def execute(self, sql, params=None):
        params = params or []
        sql = " ".join(sql.split())
        self.rows = []
        if sql.startswith("SELECT p.bgg_id, p.name, p.price_amount"):
            self.rows = list(self.db["cart_lines"])
        elif sql.startswith("INSERT INTO purchase_order ("):
            oid, uid, status, total, currency, mode, *address = params
            self.db["order"] = dict(
                id=oid,
                user_id=uid,
                status=status,
                total=total,
                currency=currency,
                shipping_mode=mode,
                recipient=address[0],
                street=address[1],
                city=address[2],
                postal_code=address[3],
                phone=address[4],
                mp_preference_id=None,
                mp_payment_id=None,
                payment_status=None,
                payment_status_detail=None,
                created_at=self.db["created_at"],
                paid_at=None,
            )
        elif sql.startswith("INSERT INTO purchase_order_line"):
            self.db["lines"].append(tuple(params[1:]))
        elif sql.startswith("UPDATE purchase_order SET mp_preference_id = %s WHERE"):
            self.db["order"]["mp_preference_id"] = params[0]
        elif sql.startswith("UPDATE purchase_order SET mp_preference_id"):
            if self.db["order"]["status"] in ("pending_payment", "rejected"):
                self.rowcount = 1
                self.db["order"]["mp_preference_id"] = params[0]
                self.db["order"]["status"] = params[1]
                self.db["order"]["payment_status"] = None
                self.db["order"]["payment_status_detail"] = None
            else:
                self.rowcount = 0
        elif sql.startswith("SELECT id, user_id, status, total"):
            order = self.db.get("order")
            owner_id = params[-1] if "AND user_id = %s" in sql else None
            if (
                order
                and order["id"] == params[0]
                and (owner_id is None or owner_id == order["user_id"])
            ):
                keys = [
                    "id",
                    "user_id",
                    "status",
                    "total",
                    "currency",
                    "shipping_mode",
                    "recipient",
                    "street",
                    "city",
                    "postal_code",
                    "phone",
                    "mp_preference_id",
                    "mp_payment_id",
                    "payment_status",
                    "payment_status_detail",
                    "created_at",
                    "paid_at",
                ]
                self.rows = [tuple(order[key] for key in keys)]
                self.description = [(key,) for key in keys]
        elif sql.startswith("SELECT bgg_id, name, unit_price, quantity, subtotal"):
            self.rows = list(self.db["lines"])
        elif sql.startswith("SELECT number, issued_at, total FROM invoice"):
            self.rows = [self.db["invoice"]] if self.db.get("invoice") else []
        elif sql.startswith("SELECT bgg_id, quantity FROM purchase_order_line"):
            self.rows = [(line[0], line[3]) for line in self.db["lines"]]
        elif sql.startswith("SELECT p.bgg_id, p.quantity FROM product_boardgame"):
            self.rows = sorted(self.db["products"].items())
        elif sql.startswith("UPDATE product_boardgame SET quantity = quantity -"):
            self.db["products"][params[1]] -= params[0]
        elif sql.startswith("UPDATE purchase_order SET status"):
            status = params[0]
            self.db["order"]["status"] = status
            self.db["order"]["mp_payment_id"] = params[1]
            self.db["order"]["payment_status"] = params[2]
            self.db["order"]["payment_status_detail"] = params[3]
            if "paid_at = %s" in sql:
                self.db["order"]["paid_at"] = params[4]
        elif sql.startswith("UPDATE purchase_order SET payment_status"):
            self.db["order"]["payment_status"] = params[0]
            self.db["order"]["payment_status_detail"] = params[1]
            self.db["order"]["mp_payment_id"] = params[2]
        elif sql.startswith("INSERT INTO invoice"):
            self.db["invoice"] = (1, params[1], params[2])
        elif sql.startswith("DELETE FROM shopping_cart_line"):
            bgg_id, quantity = params[1], params[2]
            self.db["cart_lines"] = [
                line
                for line in self.db["cart_lines"]
                if not (line[0] == bgg_id and line[3] <= quantity)
            ]
        elif sql.startswith("UPDATE shopping_cart_line SET quantity = quantity -"):
            bought, bgg_id, quantity = params[0], params[2], params[3]
            self.db["cart_lines"] = [
                (*line[:3], line[3] - bought, *line[4:])
                if line[0] == bgg_id and line[3] > quantity
                else line
                for line in self.db["cart_lines"]
            ]
        elif sql.startswith("SELECT quantity FROM product_boardgame WHERE"):
            value = self.db["products"].get(params[0])
            self.rows = [(value,)] if value is not None else []
        elif sql.startswith("SELECT o.id, o.status, o.total"):
            order = self.db.get("order")
            if (
                order
                and order["id"] == params[0]
                and order["user_id"] == params[1]
                and self.db.get("invoice")
            ):
                self.rows = [
                    (
                        order["id"],
                        order["status"],
                        order["total"],
                        order["currency"],
                        order["created_at"],
                        self.db["invoice"][0],
                        self.db["invoice"][1],
                    )
                ]
        elif sql.startswith("SELECT name, unit_price, quantity, subtotal"):
            self.rows = [
                (line[1], line[2], line[3], line[4]) for line in self.db["lines"]
            ]
        self.last_sql = sql

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        result, self.rows = self.rows, []
        return result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class OrderConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return OrderCursor(self.db)


def database(stock=9):
    return {
        "cart_lines": [(7, "Board Game", Decimal("12.50"), 2, stock)],
        "lines": [],
        "products": {7: stock},
        "order": None,
        "invoice": None,
        "created_at": __import__("datetime").datetime(2026, 1, 1),
    }


def auth(user=USER):
    return patch("saleor.graphql.api._authenticated_user", return_value=user)


def setup_order(db, monkeypatch, status="pending_payment"):
    db["lines"] = [(7, "Board Game", Decimal("12.50"), 2, Decimal("25.00"))]
    db["order"] = dict(
        id=ORDER_ID,
        user_id=USER.pk,
        status=status,
        total=Decimal("25.00"),
        currency="ARS",
        shipping_mode="pickup",
        recipient=None,
        street=None,
        city=None,
        postal_code=None,
        phone=None,
        mp_preference_id="pref-1",
        mp_payment_id=None,
        payment_status=None,
        payment_status_detail=None,
        created_at=db["created_at"],
        paid_at=None,
    )
    monkeypatch.setattr(orders, "connection", OrderConnection(db))
    monkeypatch.setattr(orders.transaction, "atomic", nullcontext)


def payment(status="approved", **updates):
    value = {
        "external_reference": str(ORDER_ID),
        "transaction_amount": 25.0,
        "currency_id": "ARS",
        "status": status,
        "status_detail": "accredited",
    }
    value.update(updates)
    return {"status": 200, "response": value}


def confirm(db, monkeypatch, response=None, user=USER):
    setup_order(db, monkeypatch)
    monkeypatch.setattr(
        orders.payments, "fetch_payment", lambda _payment_id: response or payment()
    )
    with auth(user):
        return orders.confirm_payment(SimpleNamespace(), ORDER_ID, "mp-9")


def test_checkout_creates_order_snapshot_and_preference(monkeypatch):
    db = database()
    monkeypatch.setattr(orders, "connection", OrderConnection(db))
    monkeypatch.setattr(orders.transaction, "atomic", nullcontext)
    monkeypatch.setattr("saleor.graphql.api._get_cart_id", lambda *_args: str(CART_ID))
    monkeypatch.setattr(orders.payments, "configuration_error", lambda: None)
    monkeypatch.setattr(orders.payments, "currency_id", lambda: "ARS")
    monkeypatch.setattr(
        orders, "_preference", lambda *_args: ("pref-1", "https://mp.test/pay")
    )
    with auth():
        result = orders.checkout(SimpleNamespace(), "pickup")
    assert result.user_errors == []
    assert result.order.total == 25.0
    assert db["order"]["status"] == "pending_payment"
    assert db["products"][7] == 9
    assert db["lines"][0][1:4] == ("Board Game", Decimal("12.50"), 2)


def test_checkout_refuses_sandbox_disabled(monkeypatch):
    monkeypatch.setattr(
        orders.payments, "configuration_error", lambda: "sandbox disabled"
    )
    with auth():
        result = orders.checkout(SimpleNamespace(), "pickup")
    assert result.user_errors[0].code == "PAYMENTS_DISABLED"


def test_checkout_rejects_empty_cart_and_insufficient_stock(monkeypatch):
    db = database(stock=1)
    monkeypatch.setattr(orders, "connection", OrderConnection(db))
    monkeypatch.setattr(orders.transaction, "atomic", nullcontext)
    monkeypatch.setattr("saleor.graphql.api._get_cart_id", lambda *_args: str(CART_ID))
    monkeypatch.setattr(orders.payments, "configuration_error", lambda: None)
    with auth():
        result = orders.checkout(SimpleNamespace(), "pickup")
    assert result.user_errors[0].code == "INSUFFICIENT_STOCK"
    db["cart_lines"] = []
    with auth():
        result = orders.checkout(SimpleNamespace(), "pickup")
    assert result.user_errors[0].code == "EMPTY_CART"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("approved", "paid"), ("rejected", "rejected"), ("pending", "pending_payment")],
)
def test_confirm_payment_statuses(monkeypatch, status, expected):
    db = database()
    result = confirm(db, monkeypatch, payment(status))
    assert result.user_errors == []
    assert result.order.status == expected
    assert db["products"][7] == (7 if status == "approved" else 9)
    assert bool(db["invoice"]) is (status == "approved")
    if status == "rejected":
        assert result.order.payment_status_detail == "accredited"


def test_double_confirmation_decrements_stock_once(monkeypatch):
    db = database()
    confirm(db, monkeypatch)
    monkeypatch.setattr(orders.payments, "fetch_payment", lambda _payment_id: payment())
    with auth():
        result = orders.confirm_payment(SimpleNamespace(), ORDER_ID, "mp-9")
    assert db["products"][7] == 7
    assert result.order.status == "paid"


def test_approved_payment_with_stock_conflict_has_no_invoice(monkeypatch):
    db = database(stock=1)
    result = confirm(db, monkeypatch)
    assert result.order.status == "paid_stock_conflict"
    assert db["products"][7] == 1
    assert db["invoice"] is None


def test_confirm_rejects_owner_and_anonymous(monkeypatch):
    db = database()
    setup_order(db, monkeypatch)
    with auth(None):
        anonymous = orders.confirm_payment(SimpleNamespace(), ORDER_ID, "mp-9")
    with auth(SimpleNamespace(pk=42)):
        wrong_owner = orders.confirm_payment(SimpleNamespace(), ORDER_ID, "mp-9")
    assert anonymous.user_errors[0].code == "UNAUTHENTICATED"
    assert wrong_owner.user_errors[0].code == "ORDER_NOT_FOUND"


def test_confirm_rejects_payment_amount_mismatch(monkeypatch):
    db = database()
    result = confirm(db, monkeypatch, payment(transaction_amount=99))
    assert result.user_errors[0].code == "PAYMENT_AMOUNT_MISMATCH"
    assert db["products"][7] == 9


def test_invoice_pdf_generates_pdf_and_hides_other_owners(monkeypatch):
    db = database()
    setup_order(db, monkeypatch, "paid")
    db["invoice"] = (123, db["created_at"], Decimal("25.00"))
    monkeypatch.setattr(orders, "connection", OrderConnection(db))
    request = RequestFactory().get("/invoices/" + str(ORDER_ID) + "/")
    with auth():
        response = orders.invoice_pdf(request, ORDER_ID)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    with auth(SimpleNamespace(pk=42)):
        missing = orders.invoice_pdf(request, ORDER_ID)
    assert missing.status_code == 404


def test_checkout_rejects_anonymous_user(monkeypatch):
    with auth(None):
        result = orders.checkout(SimpleNamespace(), "pickup")
    assert result.user_errors[0].code == "UNAUTHENTICATED"


def test_retry_payment_creates_new_preference_for_rejected_order(monkeypatch):
    db = database()
    setup_order(db, monkeypatch, "rejected")
    monkeypatch.setattr(orders.payments, "configuration_error", lambda: None)
    monkeypatch.setattr(
        orders, "_preference", lambda *_args: ("pref-2", "https://mp.test/retry")
    )
    with auth():
        result = orders.retry_payment(SimpleNamespace(), ORDER_ID)
    assert result.user_errors == []
    assert result.init_point == "https://mp.test/retry"
    assert db["order"]["mp_preference_id"] == "pref-2"
    assert db["order"]["status"] == "pending_payment"


def test_retry_payment_does_not_revive_paid_order(monkeypatch):
    db = database()
    setup_order(db, monkeypatch, "paid")
    monkeypatch.setattr(orders.payments, "configuration_error", lambda: None)
    monkeypatch.setattr(
        orders, "_preference", lambda *_args: ("pref-2", "https://mp.test/retry")
    )
    # The status check passes on a stale read; the conditional UPDATE must still refuse.
    monkeypatch.setattr(
        orders,
        "_load_order",
        lambda *_args, **_kwargs: {**db["order"], "status": "rejected"},
    )
    with auth():
        result = orders.retry_payment(SimpleNamespace(), ORDER_ID)
    assert result.user_errors[0].code == "ORDER_NOT_PAYABLE"
    assert db["order"]["status"] == "paid"
    assert db["order"]["mp_preference_id"] == "pref-1"


def test_confirm_rejects_live_mode_payment(monkeypatch):
    db = database()
    result = confirm(db, monkeypatch, payment(live_mode=True))
    assert result.user_errors[0].code == "PAYMENT_NOT_VERIFIED"
    assert db["products"][7] == 9


def test_paid_confirmation_keeps_cart_changes_made_after_checkout(monkeypatch):
    db = database()
    db["cart_lines"] = [(7, "Board Game", Decimal("12.50"), 5, 9)]
    confirm(db, monkeypatch)
    assert db["cart_lines"] == [(7, "Board Game", Decimal("12.50"), 3, 9)]


def test_payment_sdk_preference_uses_sandbox_currency_and_return_urls(monkeypatch):
    import mercadopago

    fake_sdk = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    fake_sdk.preference.return_value.create.return_value = {
        "status": 201,
        "response": {"id": "p-1"},
    }
    monkeypatch.setattr(mercadopago, "SDK", lambda token: fake_sdk)
    with override_settings(
        MP_ACCESS_TOKEN="test-token",
        MP_CURRENCY_ID="",
        COUNTRY_MONEY_CODE="AR",
        FRONTEND_URL="http://localhost:3000/",
    ):
        orders.payments.create_preference(
            ORDER_ID,
            [
                {
                    "bgg_id": 7,
                    "name": "Game",
                    "unit_price": Decimal("12.50"),
                    "quantity": 2,
                }
            ],
        )
    data = fake_sdk.preference.return_value.create.call_args.args[0]
    assert data["items"][0]["currency_id"] == "ARS"
    assert data["external_reference"] == str(ORDER_ID)
    assert data["back_urls"] == {
        "success": "http://localhost:3000/compra/resultado",
        "failure": "http://localhost:3000/compra/resultado",
        "pending": "http://localhost:3000/compra/resultado",
    }
    assert "auto_return" not in data


def test_confirm_rejects_payment_with_wrong_external_reference(monkeypatch):
    db = database()
    result = confirm(db, monkeypatch, payment(external_reference=str(uuid.uuid4())))
    assert result.user_errors[0].code == "PAYMENT_ORDER_MISMATCH"
    assert db["products"][7] == 9


def test_cancelled_order_is_not_revived_by_approved_payment(monkeypatch):
    db = database()
    setup_order(db, monkeypatch, "cancelled")
    monkeypatch.setattr(orders.payments, "fetch_payment", lambda _payment_id: payment())
    with auth():
        result = orders.confirm_payment(SimpleNamespace(), ORDER_ID, "mp-9")
    assert result.order.status == "cancelled"
    assert db["products"][7] == 9
    assert db["invoice"] is None
