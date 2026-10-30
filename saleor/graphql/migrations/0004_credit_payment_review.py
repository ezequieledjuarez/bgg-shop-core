from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0003_credit_retry_guard")]

    operations = [
        migrations.RunSQL("""
            CREATE TABLE credit_payment_review (
                order_id uuid NOT NULL REFERENCES credit_retry_guard(order_id) ON DELETE RESTRICT,
                payment_id varchar(128) NOT NULL,
                detected_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (order_id, payment_id)
            );
        """),
    ]