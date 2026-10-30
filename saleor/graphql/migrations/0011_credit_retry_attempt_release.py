from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0010_credit_retry_attempt_observation")]

    operations = [
        migrations.RunSQL("""
            ALTER TABLE credit_retry_attempt_observation
                ADD CONSTRAINT credit_attempt_observation_identity_key
                UNIQUE (id, attempt_id, order_id, user_id);
            CREATE TABLE credit_retry_attempt_release (
                attempt_id uuid PRIMARY KEY,
                order_id uuid NOT NULL,
                user_id integer NOT NULL,
                observation_id uuid NOT NULL,
                actor_user_id integer NOT NULL REFERENCES auth_user(id) ON DELETE RESTRICT,
                preference_id varchar(128),
                reason varchar(1000) NOT NULL CHECK (char_length(btrim(reason)) > 0),
                risk_acknowledged boolean NOT NULL CHECK (risk_acknowledged),
                released_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id, user_id)
                    REFERENCES purchase_order(id, user_id) ON DELETE RESTRICT,
                FOREIGN KEY (observation_id, attempt_id, order_id, user_id)
                    REFERENCES credit_retry_attempt_observation(id, attempt_id, order_id, user_id)
                    ON DELETE RESTRICT
            );
            CREATE TABLE credit_retry_attempt_release_response (
                attempt_id uuid PRIMARY KEY
                    REFERENCES credit_retry_attempt_release(attempt_id) ON DELETE RESTRICT,
                preference_id varchar(128) NOT NULL CHECK (char_length(btrim(preference_id)) > 0),
                recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """),
    ]