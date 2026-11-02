from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0014_promotion_manual_activation")]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE coupon ADD COLUMN active boolean NOT NULL DEFAULT false, "
            "ADD COLUMN starts_at timestamptz, ADD COLUMN ends_at timestamptz;"
        ),
    ]