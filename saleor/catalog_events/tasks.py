import hashlib
import hmac
import json
import logging
from datetime import timedelta, timezone as datetime_timezone

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import CatalogEventOutbox


logger = logging.getLogger(__name__)


def _event_body(event):
    occurred_at = event.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=datetime_timezone.utc)
    else:
        occurred_at = occurred_at.astimezone(datetime_timezone.utc)
    return json.dumps(
        {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _retry(event, now):
    event.attempts += 1
    delay = min(2 ** min(event.attempts - 1, 9), 300)
    event.next_attempt_at = now + timedelta(seconds=delay)
    event.save(update_fields=["attempts", "next_attempt_at"])
    logger.error("Catalog event delivery failed; retry scheduled.")


@shared_task
def dispatch_due_catalog_events(batch_size=100):
    delivered = 0
    for _ in range(batch_size):
        now = timezone.now()
        with transaction.atomic():
            event = (
                CatalogEventOutbox.objects.filter(
                    delivered_at__isnull=True,
                    next_attempt_at__lte=now,
                )
                .order_by("occurred_at", "event_id")
                .select_for_update(skip_locked=True)
                .first()
            )
            if event is None:
                break

            secret = settings.CATALOG_EVENTS_HMAC_SECRET
            if not secret:
                _retry(event, now)
                continue

            body = _event_body(event)
            signature = "sha256=" + hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            try:
                response = requests.post(
                    settings.CATALOG_EVENTS_BFF_URL,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Catalog-Event-Signature": signature,
                    },
                    timeout=5,
                    allow_redirects=False,
                )
                succeeded = 200 <= response.status_code < 300
            except requests.RequestException:
                succeeded = False

            if succeeded:
                event.delivered_at = timezone.now()
                event.save(update_fields=["delivered_at"])
                delivered += 1
            else:
                _retry(event, now)

    return delivered
