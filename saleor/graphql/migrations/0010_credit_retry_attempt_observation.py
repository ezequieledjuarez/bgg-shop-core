from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0009_credit_retry_attempt_id")]

    operations = [
        migrations.RunSQL("""
            CREATE TABLE credit_retry_attempt_observation (
                id uuid PRIMARY KEY,
                attempt_id uuid NOT NULL,
                order_id uuid NOT NULL,
                user_id integer NOT NULL,
                preference_id varchar(128),
                actor_user_id integer NOT NULL REFERENCES auth_user(id) ON DELETE RESTRICT,
                statement varchar(1000) NOT NULL CHECK (char_length(btrim(statement)) > 0),
                recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id, user_id)
                    REFERENCES purchase_order(id, user_id) ON DELETE RESTRICT
            );
            CREATE INDEX credit_retry_attempt_observation_attempt_idx
                ON credit_retry_attempt_observation(attempt_id, recorded_at, id);
        """),
    ]