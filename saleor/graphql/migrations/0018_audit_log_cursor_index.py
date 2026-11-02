from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0017_007_contract_data_dependencies")]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS audit_log_created_id_desc_idx "
                "ON audit_log (created_at DESC, id DESC);"
            ),
            reverse_sql="DROP INDEX IF EXISTS audit_log_created_id_desc_idx;",
        ),
    ]
