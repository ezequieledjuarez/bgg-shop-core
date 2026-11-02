from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("promotions", "0003_stock_reservation_constraints")]

    operations = [
        migrations.RunSQL(
            """
            CREATE TABLE coupon_checkout_reservation (
                order_id uuid PRIMARY KEY
                    REFERENCES purchase_order(id) ON DELETE RESTRICT,
                coupon_id integer NOT NULL
                    REFERENCES coupon(id) ON DELETE RESTRICT,
                operation_key_hash char(64) NOT NULL UNIQUE,
                request_hash char(64) NOT NULL,
                state varchar(16) NOT NULL
                    CHECK (state IN ('RESERVED', 'CONSUMED', 'RELEASED', 'NOT_APPLIED', 'REVIEW')),
                init_point text,
                created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """,
            reverse_sql=migrations.RunSQL.noop,
        )
    ]
