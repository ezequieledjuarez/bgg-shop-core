from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0012_stock_movement_status")]

    operations = [
        migrations.RunSQL("""
            CREATE TABLE stock_movement_details (
                movement_id uuid PRIMARY KEY REFERENCES stock_movement_status(movement_id) ON DELETE RESTRICT,
                product_id integer NOT NULL CHECK (product_id > 0),
                direction varchar(3) NOT NULL CHECK (direction IN ('IN', 'OUT')),
                requested_quantity integer NOT NULL CHECK (requested_quantity > 0),
                actor_id integer NOT NULL CHECK (actor_id > 0),
                applied_quantity integer CHECK (applied_quantity >= 0),
                quantity_before integer CHECK (quantity_before >= 0),
                quantity_after integer CHECK (quantity_after >= 0),
                CHECK (
                    (applied_quantity IS NULL AND quantity_before IS NULL AND quantity_after IS NULL)
                    OR (applied_quantity IS NOT NULL AND quantity_before IS NOT NULL AND quantity_after IS NOT NULL)
                )
            );
        """),
    ]