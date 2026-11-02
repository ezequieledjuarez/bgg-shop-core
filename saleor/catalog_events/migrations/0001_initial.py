import uuid

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="CatalogEventOutbox",
            fields=[
                (
                    "event_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("event_type", models.CharField(max_length=32)),
                ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("aggregate_type", models.CharField(max_length=32)),
                ("aggregate_id", models.CharField(max_length=128)),
                ("attempts", models.PositiveIntegerField(default=0)),
                (
                    "next_attempt_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "catalog_event_outbox",
                "indexes": [
                    models.Index(
                        fields=["next_attempt_at", "occurred_at"],
                        name="catalog_event_due_idx",
                    )
                ],
            },
        ),
    ]
