from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("graphql", "0013_stock_movement_details"),
        ("promotions", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE promotion ADD COLUMN active boolean NOT NULL DEFAULT false;"
        ),
    ]