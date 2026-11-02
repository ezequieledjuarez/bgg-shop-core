from django.db import models


class Product(models.Model):
    id = models.AutoField(primary_key=True)
    bgg_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=250, unique=True)
    price_amount = models.DecimalField(max_digits=12, decimal_places=2)
    image_url = models.CharField(max_length=500)
    image_filename = models.CharField(max_length=255)
    quantity = models.IntegerField(default=0)

    class Meta:
        db_table = "product_boardgame"
        managed = False
        constraints = [
            models.CheckConstraint(
                check=models.Q(price_amount__gt=0),
                name="product_price_positive",
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gte=0),
                name="product_boardgame_quantity_check",
            ),
        ]


class Promotion(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=250)
    active = models.BooleanField(default=False)
    stackable = models.BooleanField(default=False)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        to_field="bgg_id",
        db_column="product_bgg_id",
        on_delete=models.DO_NOTHING,
        related_name="promotions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "promotion"
        constraints = [
            models.CheckConstraint(
                check=models.Q(discount_pct__gte=0, discount_pct__lte=50),
                name="promotion_discount_range",
            ),
            models.UniqueConstraint(
                fields=("product",),
                condition=models.Q(active=True, product__isnull=False),
                name="unique_active_promotion_product",
            ),
        ]


class Coupon(models.Model):
    id = models.AutoField(primary_key=True)
    code = models.CharField(max_length=64, unique=True)
    promotion = models.ForeignKey(
        Promotion, null=True, blank=True, on_delete=models.SET_NULL, db_column="promotion_id"
    )
    remaining = models.BigIntegerField(default=0)
    active = models.BooleanField(default=False)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "coupon"
        constraints = [
            models.CheckConstraint(check=models.Q(remaining__gte=0), name="coupon_remaining_nonnegative"),
            models.CheckConstraint(
                check=models.Q(starts_at__isnull=True) | models.Q(ends_at__isnull=True) | models.Q(starts_at__lt=models.F("ends_at")),
                name="coupon_valid_period",
            ),
        ]


class CouponCounter(models.Model):
    coupon = models.OneToOneField(Coupon, on_delete=models.CASCADE, primary_key=True, db_column="coupon_id")
    counter = models.BigIntegerField(default=0)

    class Meta:
        db_table = "coupon_counter"


class AuditLog(models.Model):
    actor = models.CharField(max_length=150, null=True, blank=True)
    action = models.CharField(max_length=100)
    target_table = models.CharField(max_length=150, null=True, blank=True)
    target_id = models.CharField(max_length=64, null=True, blank=True)
    diff = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"


class StockReservation(models.Model):
    bgg_id = models.ForeignKey(
        Product,
        to_field="bgg_id",
        db_column="bgg_id",
        on_delete=models.DO_NOTHING,
    )
    reserved_qty = models.IntegerField()
    reason = models.CharField(max_length=250, null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_reservation"
        constraints = [
            models.CheckConstraint(
                check=models.Q(reserved_qty__gte=0),
                name="stock_reservation_reserved_qty_check",
            ),
        ]
