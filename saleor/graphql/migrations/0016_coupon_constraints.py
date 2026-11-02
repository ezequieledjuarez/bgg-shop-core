from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0015_coupon_availability")]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE coupon "
            "ADD CONSTRAINT coupon_remaining_nonnegative CHECK (remaining >= 0), "
            "ADD CONSTRAINT coupon_valid_period CHECK "
            "(starts_at IS NULL OR ends_at IS NULL OR starts_at < ends_at);",
            reverse_sql=(
                "ALTER TABLE coupon DROP CONSTRAINT coupon_remaining_nonnegative, "
                "DROP CONSTRAINT coupon_valid_period;"
            ),
        ),
    ]