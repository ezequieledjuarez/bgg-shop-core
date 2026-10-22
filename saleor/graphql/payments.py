"""Mercado Pago Checkout Pro integration."""

from decimal import Decimal
from django.conf import settings


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
    return mercadopago.SDK(settings.MP_ACCESS_TOKEN).preference().create(preference)


def fetch_payment(payment_id):
    import mercadopago

    return mercadopago.SDK(settings.MP_ACCESS_TOKEN).payment().get(payment_id)
