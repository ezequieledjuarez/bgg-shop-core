"""Mercado Pago Checkout Pro integration."""

from decimal import Decimal
import re
from django.conf import settings
import requests


def currency_id():
    if settings.MP_CURRENCY_ID:
        return settings.MP_CURRENCY_ID
    countries = {
        "AR": "ARS",
        "BR": "BRL",
        "CL": "CLP",
        "CO": "COP",
        "MX": "MXN",
        "UY": "UYU",
        "US": "USD",
    }
    code = settings.COUNTRY_MONEY_CODE.upper()
    # COUNTRY_MONEY_CODE may hold either a country or a currency code.
    return countries.get(code) or (code if code in countries.values() else None)


def configuration_error():
    if not settings.MP_SANDBOX or not settings.MP_ACCESS_TOKEN:
        return "Checkout is available only with Mercado Pago sandbox credentials."
    if not currency_id():
        return "Configure a supported Mercado Pago currency."
    return None


def is_sandbox_payment(payment):
    mode = payment.get("live_mode")
    if mode is False:
        return True
    if mode is not True or not settings.MP_SANDBOX or not settings.MP_ACCESS_TOKEN:
        return False
    collector_id = payment.get("collector_id")
    if type(collector_id) is not int or collector_id <= 0:
        return False
    try:
        response = requests.get(
            "https://api.mercadopago.com/users/me",
            headers={"Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}"},
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        identity = response.json()
        return (isinstance(identity, dict) and type(identity.get("id")) is int
                and identity["id"] == collector_id
                and isinstance(identity.get("tags"), list)
                and "test_user" in identity["tags"])
    except (requests.RequestException, ValueError, TypeError):
        return False


def verified_preference(payment, order_id):
    merchant_order = payment.get("order")
    payment_id = payment.get("id")
    collector_id = payment.get("collector_id")
    if (not isinstance(merchant_order, dict)
            or merchant_order.get("type") not in ("mercadopago", "merchant_order")
            or isinstance(payment_id, bool) or not isinstance(payment_id, (str, int))
            or type(collector_id) is not int or collector_id <= 0
            or payment.get("external_reference") != order_id
            or not settings.MP_ACCESS_TOKEN):
        return None
    merchant_id = merchant_order.get("id")
    if (isinstance(merchant_id, bool) or not isinstance(merchant_id, (str, int))
            or not re.fullmatch(r"[0-9]{1,30}", str(merchant_id))):
        return None
    try:
        response = requests.get(
            f"https://api.mercadopago.com/merchant_orders/{merchant_id}",
            headers={"Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}"},
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return None
        merchant = response.json()
        if not isinstance(merchant, dict):
            return None
        collector = merchant.get("collector")
        entries = merchant.get("payments")
        preference_id = merchant.get("preference_id")
        if (str(merchant.get("id")) != str(merchant_id)
                or not isinstance(collector, dict) or type(collector.get("id")) is not int
                or collector["id"] != collector_id
                or merchant.get("external_reference") != order_id
                or not isinstance(entries, list)
                or not any(isinstance(entry, dict) and not isinstance(entry.get("id"), bool)
                           and str(entry.get("id")) == str(payment_id) for entry in entries)
                or not isinstance(preference_id, str) or not 1 <= len(preference_id) <= 128):
            return None
        return preference_id
    except (requests.RequestException, ValueError, TypeError):
        return None


def create_preference(order_id, lines):
    import mercadopago

    base_url = settings.FRONTEND_URL.rstrip("/") + "/compra/resultado"
    preference = {
        "items": [
            {
                "id": str(line["bgg_id"]),
                "title": line["name"],
                "quantity": line["quantity"],
                "unit_price": float(Decimal(str(line["unit_price"]))),
                "currency_id": currency_id(),
            }
            for line in lines
        ],
        "external_reference": str(order_id),
        "back_urls": {"success": base_url, "failure": base_url, "pending": base_url},
    }
    sdk = mercadopago.SDK(settings.MP_ACCESS_TOKEN)
    result = sdk.preference(request_options=_bounded_request_options()).create(preference)
    return result


def _bounded_request_options():
    from mercadopago.config import RequestOptions

    return RequestOptions(access_token=settings.MP_ACCESS_TOKEN, connection_timeout=10.0, max_retries=0)


def fetch_payment(payment_id, bounded=False):
    import mercadopago

    if bounded:
        return mercadopago.SDK(settings.MP_ACCESS_TOKEN).payment().get(
            payment_id, request_options=_bounded_request_options(),
        )
    return mercadopago.SDK(settings.MP_ACCESS_TOKEN).payment().get(payment_id)


def search_payments(external_reference, offset, limit):
    import mercadopago

    return mercadopago.SDK(settings.MP_ACCESS_TOKEN).payment().search({
        "external_reference": external_reference,
        "status": "approved",
        "sort": "id",
        "criteria": "asc",
        "offset": offset,
        "limit": limit,
    }, request_options=_bounded_request_options())
