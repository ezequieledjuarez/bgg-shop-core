from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0011_credit_retry_attempt_release")]

    operations = [
        migrations.RunSQL("""
            CREATE TABLE stock_movement_status (
                movement_id uuid PRIMARY KEY,
                status varchar(16) NOT NULL CHECK (status IN ('IN_PROGRESS', 'APPLIED', 'REJECTED'))
            );
        """),
    ]