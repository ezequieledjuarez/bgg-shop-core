from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0007_payment_review_order")]

    operations = [
        migrations.RunSQL("""
            CREATE TABLE credit_payment_review_release (
                order_id uuid NOT NULL,
                payment_id varchar(128) NOT NULL,
                actor_user_id integer NOT NULL REFERENCES auth_user(id) ON DELETE RESTRICT,
                reason varchar(1000) NOT NULL CHECK (char_length(btrim(reason)) > 0),
                released_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (order_id, payment_id),
                FOREIGN KEY (order_id, payment_id)
                    REFERENCES credit_payment_review_resolution(order_id, payment_id) ON DELETE RESTRICT
            );
        """),
    ]