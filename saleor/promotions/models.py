from django.db import models


class Promotion(models.Model):
    name = models.CharField(max_length=250)
    stackable = models.BooleanField(default=False)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "promotion"


class Coupon(models.Model):
    code = models.CharField(max_length=64, unique=True)
    promotion = models.ForeignKey(
        Promotion, null=True, blank=True, on_delete=models.SET_NULL, db_column="promotion_id"
    )
    remaining = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "coupon"


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
    bgg_id = models.IntegerField()
    reserved_qty = models.IntegerField()
    reason = models.CharField(max_length=250, null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_reservation"
