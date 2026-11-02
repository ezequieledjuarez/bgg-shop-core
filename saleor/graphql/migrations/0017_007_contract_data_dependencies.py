from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0016_coupon_constraints")]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE promotion "
                "ADD COLUMN product_bgg_id integer REFERENCES product_boardgame(bgg_id), "
                "ADD CONSTRAINT promotion_discount_range "
                "CHECK (discount_pct >= 0 AND discount_pct <= 50) NOT VALID;"
            ),
            reverse_sql=(
                "ALTER TABLE promotion "
                "DROP CONSTRAINT promotion_discount_range, "
                "DROP COLUMN product_bgg_id;"
            ),
        ),
        migrations.RunSQL(
            sql=(
                "ALTER TABLE product_boardgame "
                "ADD CONSTRAINT product_price_positive "
                "CHECK (price_amount > 0) NOT VALID;"
            ),
            reverse_sql=(
                "ALTER TABLE product_boardgame "
                "DROP CONSTRAINT product_price_positive;"
            ),
        ),
        migrations.RunSQL(
            sql=(
                "ALTER TABLE promotion "
                "VALIDATE CONSTRAINT promotion_discount_range;"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=(
                "ALTER TABLE product_boardgame "
                "VALIDATE CONSTRAINT product_price_positive;"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX unique_active_promotion_product "
                "ON promotion (product_bgg_id) "
                "WHERE active AND product_bgg_id IS NOT NULL;"
            ),
            reverse_sql="DROP INDEX unique_active_promotion_product;",
        ),
    ]
