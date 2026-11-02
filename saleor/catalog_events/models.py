import uuid

from django.db import models
from django.utils import timezone


class CatalogEventOutbox(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=32)
    occurred_at = models.DateTimeField(default=timezone.now)
    aggregate_type = models.CharField(max_length=32)
    aggregate_id = models.CharField(max_length=128)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "catalog_event_outbox"
        indexes = [
            models.Index(
                fields=["next_attempt_at", "occurred_at"],
                name="catalog_event_due_idx",
            ),
        ]
