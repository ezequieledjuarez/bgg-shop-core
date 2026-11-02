"""Sync promotions migration state with SQL applied by graphql 0014-0017.

State-only: the database changes already live in the RunSQL migrations.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("promotions", "0001_initial"),
        ("graphql", "0017_007_contract_data_dependencies"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="Product",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        ("bgg_id", models.IntegerField(unique=True)),
                        ("name", models.CharField(max_length=250, unique=True)),
                        ("price_amount", models.DecimalField(decimal_places=2, max_digits=12)),
                        ("image_url", models.CharField(max_length=500)),
                        ("image_filename", models.CharField(max_length=255)),
                        ("quantity", models.IntegerField(default=0)),
                    ],
                    options={"db_table": "product_boardgame", "managed": False},
                ),
                migrations.AddConstraint(
                    model_name="product",
                    constraint=models.CheckConstraint(
                        check=models.Q(price_amount__gt=0), name="product_price_positive"
                    ),
                ),
                migrations.AddConstraint(
                    model_name="product",
                    constraint=models.CheckConstraint(
                        check=models.Q(quantity__gte=0), name="product_boardgame_quantity_check"
                    ),
                ),
                migrations.AlterField(
                    model_name="auditlog",
                    name="id",
                    field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                migrations.AlterField(
                    model_name="stockreservation",
                    name="id",
                    field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                migrations.AlterField(
                    model_name="stockreservation",
                    name="bgg_id",
                    field=models.ForeignKey(
                        db_column="bgg_id",
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        to="promotions.product",
                        to_field="bgg_id",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="stockreservation",
                    constraint=models.CheckConstraint(
                        check=models.Q(reserved_qty__gte=0),
                        name="stock_reservation_reserved_qty_check",
                    ),
                ),
                migrations.AddField(
                    model_name="coupon",
                    name="active",
                    field=models.BooleanField(default=False),
                ),
                migrations.AddField(
                    model_name="coupon",
                    name="starts_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="coupon",
                    name="ends_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.AddConstraint(
                    model_name="coupon",
                    constraint=models.CheckConstraint(
                        check=models.Q(remaining__gte=0), name="coupon_remaining_nonnegative"
                    ),
                ),
                migrations.AddConstraint(
                    model_name="coupon",
                    constraint=models.CheckConstraint(
                        check=models.Q(starts_at__isnull=True)
                        | models.Q(ends_at__isnull=True)
                        | models.Q(starts_at__lt=models.F("ends_at")),
                        name="coupon_valid_period",
                    ),
                ),
                migrations.AddField(
                    model_name="promotion",
                    name="active",
                    field=models.BooleanField(default=False),
                ),
                migrations.AddField(
                    model_name="promotion",
                    name="product",
                    field=models.ForeignKey(
                        blank=True,
                        db_column="product_bgg_id",
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="promotions",
                        to="promotions.product",
                        to_field="bgg_id",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="promotion",
                    constraint=models.CheckConstraint(
                        check=models.Q(discount_pct__gte=0, discount_pct__lte=50),
                        name="promotion_discount_range",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="promotion",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(active=True, product__isnull=False),
                        fields=("product",),
                        name="unique_active_promotion_product",
                    ),
                ),
            ],
        )
    ]
