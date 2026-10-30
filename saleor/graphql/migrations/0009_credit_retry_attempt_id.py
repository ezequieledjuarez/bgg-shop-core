from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0008_payment_review_release")]

    operations = [
        migrations.RunSQL("""
            ALTER TABLE credit_retry_attempt
                ADD COLUMN attempt_id uuid NOT NULL DEFAULT gen_random_uuid(),
                ADD CONSTRAINT credit_retry_attempt_attempt_id_key UNIQUE (attempt_id);
        """),
    ]