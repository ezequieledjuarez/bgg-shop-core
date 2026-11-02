from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("promotions", "0002_007_state_sync")]

    operations = [
        migrations.RunSQL(
            sql="""
                DO $migration$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conrelid = 'stock_reservation'::regclass
                          AND conname = 'stock_reservation_bgg_id_fkey'
                    ) THEN
                        ALTER TABLE stock_reservation
                        ADD CONSTRAINT stock_reservation_bgg_id_fkey
                        FOREIGN KEY (bgg_id)
                        REFERENCES product_boardgame(bgg_id)
                        NOT VALID;
                    END IF;
                END
                $migration$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="ALTER TABLE stock_reservation VALIDATE CONSTRAINT stock_reservation_bgg_id_fkey;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
                DO $migration$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conrelid = 'stock_reservation'::regclass
                          AND conname = 'stock_reservation_reserved_qty_check'
                    ) THEN
                        ALTER TABLE stock_reservation
                        ADD CONSTRAINT stock_reservation_reserved_qty_check
                        CHECK (reserved_qty >= 0)
                        NOT VALID;
                    END IF;
                END
                $migration$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="ALTER TABLE stock_reservation VALIDATE CONSTRAINT stock_reservation_reserved_qty_check;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
