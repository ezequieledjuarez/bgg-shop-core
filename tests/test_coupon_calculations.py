import os
import sys
from decimal import Decimal
from datetime import datetime, timezone as datetime_timezone

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from saleor.graphql.orders import (
    _calculate_savings,
    _coupon_eligibility,
    _coupon_operation_hash,
    _checkout_payload_hash,
    _checkout_replay_error,
    _discounted_payment_lines,
    _promotion_rows,
    _select_savings,
)


def test_savings_preserve_decimal_precision_and_round_each_alternative_up_once():
    savings, coupon_applied = _select_savings(
        [Decimal("0.20"), Decimal("0.20")], Decimal("0.39")
    )

    assert savings == Decimal("1")
    assert coupon_applied is False


def test_coupon_wins_only_when_rounded_savings_exceed_store_promotions():
    savings, coupon_applied = _select_savings(
        [Decimal("1.01")], Decimal("2.01")
    )

    assert savings == Decimal("3")
    assert coupon_applied is True


def test_zero_savings_remain_zero():
    assert _select_savings([], Decimal("0")) == (Decimal("0"), False)


def test_cart_promotion_savings_are_summed_before_rounding():
    subtotal, savings, coupon_applied = _calculate_savings(
        [(1, Decimal("10.00"), 1), (2, Decimal("10.00"), 1)],
        [(1, Decimal("1.01")), (2, Decimal("1.01"))],
        Decimal("1.99"),
    )

    assert subtotal == Decimal("20.00")
    assert savings == Decimal("1")
    assert coupon_applied is False


def test_global_store_promotion_applies_when_no_specific_promotion_exists():
    subtotal, savings, coupon_applied = _calculate_savings(
        [(7, Decimal("100.00"), 1)],
        [(None, Decimal("20")), (None, Decimal("35"))],
        Decimal("0"),
    )

    assert subtotal == Decimal("100.00")
    assert savings == Decimal("35")
    assert coupon_applied is False


def test_product_specific_store_promotion_wins_over_higher_global_percentage():
    subtotal, savings, coupon_applied = _calculate_savings(
        [(7, Decimal("100.00"), 1)],
        [(None, Decimal("45")), (7, Decimal("10"))],
        Decimal("0"),
    )

    assert subtotal == Decimal("100.00")
    assert savings == Decimal("10")
    assert coupon_applied is False


def test_coupon_comparison_uses_selected_global_promotion_without_changing_rounding():
    subtotal, savings, coupon_applied = _calculate_savings(
        [(7, Decimal("100.00"), 1)],
        [(None, Decimal("20")), (None, Decimal("35"))],
        Decimal("36"),
    )

    assert subtotal == Decimal("100.00")
    assert savings == Decimal("36")
    assert coupon_applied is True


def test_promotion_selector_includes_global_offers_and_excludes_coupon_rows():
    from unittest.mock import Mock

    cursor = Mock()
    cursor.fetchall.return_value = [(None, Decimal("35")), (7, Decimal("10"))]
    now = datetime(2026, 1, 1, tzinfo=datetime_timezone.utc)

    selected = _promotion_rows(cursor, [7], now)

    assert selected == [(None, Decimal("35")), (7, Decimal("10"))]
    query, parameters = cursor.execute.call_args.args
    assert "product_bgg_id IS NULL" in query
    assert "NOT EXISTS" in query and "FROM coupon" in query
    assert parameters == [[7], now, now]


def test_coupon_over_fifty_percent_is_rejected_by_calculation():
    with pytest.raises(ValueError, match="DISCOUNT_LIMIT_EXCEEDED"):
        _calculate_savings([(1, Decimal("10.00"), 1)], [], Decimal("50.01"))


@pytest.mark.parametrize(
    ("coupon", "now", "reason"),
    [
        ((1, False, None, None, 2, Decimal("10"), True, None, None), datetime(2026, 1, 1, tzinfo=datetime_timezone.utc), "INACTIVE"),
        ((1, True, datetime(2026, 1, 2, tzinfo=datetime_timezone.utc), None, 2, Decimal("10"), True, None, None), datetime(2026, 1, 1, tzinfo=datetime_timezone.utc), "NOT_YET_VALID"),
        ((1, True, None, datetime(2026, 1, 1, tzinfo=datetime_timezone.utc), 2, Decimal("10"), True, None, None), datetime(2026, 1, 1, tzinfo=datetime_timezone.utc), "EXPIRED"),
        ((1, True, None, None, 0, Decimal("10"), True, None, None), datetime(2026, 1, 1, tzinfo=datetime_timezone.utc), "EXHAUSTED"),
    ],
)
def test_coupon_eligibility_reasons(coupon, now, reason):
    assert _coupon_eligibility(coupon, now) == reason


def test_checkout_key_hash_is_scoped_and_payload_changes_conflict():
    assert _coupon_operation_hash(1, "same-key") != _coupon_operation_hash(2, "same-key")
    assert _checkout_payload_hash(1, "pickup", {}, "SAVE") != _checkout_payload_hash(
        1, "pickup", {}, "OTHER"
    )


def test_coupon_checkout_replay_never_returns_success_before_payment_url_exists():
    pending = _checkout_replay_error(("payload-hash", None), "payload-hash")
    conflict = _checkout_replay_error(("other-hash", "https://pay.test"), "payload-hash")

    assert pending.code == "PENDING_PAYMENT"
    assert conflict.code == "IDEMPOTENCY_CONFLICT"


def test_discounted_provider_lines_equal_the_order_total():
    lines = _discounted_payment_lines(
        [(1, "One", Decimal("12.50"), 2), (2, "Two", Decimal("7.50"), 1)],
        Decimal("3"),
    )

    assert sum(line["unit_price"] * line["quantity"] for line in lines) == Decimal("29.5")


def test_runtime_preview_fields_match_the_required_public_contract():
    from saleor.graphql.api import schema

    fields = schema.graphql_schema.get_type("CouponPreview").fields

    assert str(fields["couponCode"].type) == "String!"
    assert str(fields["discountAmount"].type) == "Float!"
    assert str(fields["newTotal"].type) == "Float!"
