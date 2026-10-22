"""Initial migration for promotions app

Generated manually to match existing DDL in import_catalog.sql
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Promotion",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=250)),
                ("stackable", models.BooleanField(default=False)),
                ("discount_pct", models.DecimalField(max_digits=5, decimal_places=2, default=0)),
                ("starts_at", models.DateTimeField(blank=True, null=True)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "promotion"},
        ),
        migrations.CreateModel(
            name="Coupon",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=64, unique=True)),
                (
                    "promotion",
                    models.ForeignKey(
                        to="saleor.promotions.Promotion",
                        null=True,
                        blank=True,
                        on_delete=models.SET_NULL,
                        db_column="promotion_id",
                    ),
                ),
                ("remaining", models.BigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "coupon"},
        ),
        migrations.CreateModel(
            name="CouponCounter",
            fields=[
                (
                    "coupon",
                    models.OneToOneField(
                        to="saleor.promotions.Coupon",
                        on_delete=models.CASCADE,
                        primary_key=True,
                        serialize=False,
                        db_column="coupon_id",
                    ),
                ),
                ("counter", models.BigIntegerField(default=0)),
            ],
            options={"db_table": "coupon_counter"},
        ),
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("actor", models.CharField(blank=True, max_length=150, null=True)),
                ("action", models.CharField(max_length=100)),
                ("target_table", models.CharField(blank=True, max_length=150, null=True)),
                ("target_id", models.CharField(blank=True, max_length=64, null=True)),
                ("diff", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "audit_log"},
        ),
        migrations.CreateModel(
            name="StockReservation",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("bgg_id", models.IntegerField()),
                ("reserved_qty", models.IntegerField()),
                ("reason", models.CharField(blank=True, max_length=250, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "stock_reservation"},
        ),
    ]
