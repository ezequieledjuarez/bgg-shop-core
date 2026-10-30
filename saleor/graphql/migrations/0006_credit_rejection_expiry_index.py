from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0005_credit_payment_review_resolution")]

    operations = [
        migrations.RunSQL(
            "CREATE INDEX credit_idempotency_rejection_expiry_idx "
            "ON credit_idempotency (user_id, created_at, id) WHERE error_code IS NOT NULL",
            reverse_sql="DROP INDEX credit_idempotency_rejection_expiry_idx",
        ),
    ]