from django.db import migrations, models
import django.utils.timezone

class Migration(migrations.Migration):
    dependencies = [("opkeys", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="OpKeyAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_id", models.UUIDField()),
                ("subject_user_id", models.BigIntegerField(blank=True, null=True)),
                ("actor_user_id", models.BigIntegerField(blank=True, null=True)),
                ("actor_source", models.CharField(default="INTERNAL_SERVICE", max_length=32)),
                ("action", models.CharField(max_length=32)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ],
            options={"db_table": "opkeys_opkeyaudit"},
        ),
    ]