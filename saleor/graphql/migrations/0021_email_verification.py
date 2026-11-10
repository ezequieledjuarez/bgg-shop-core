from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("graphql", "0020_chat_conversations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    # Existing accounts are exempt without a fabricated verification timestamp.
    operations = [
        migrations.RunSQL(
            sql="""
                CREATE TABLE account_email_verification (
                    user_id integer PRIMARY KEY
                        REFERENCES auth_user(id) ON DELETE CASCADE,
                    state varchar(16) NOT NULL,
                    verified_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    CONSTRAINT account_email_verification_state
                        CHECK (state IN ('LEGACY_EXEMPT', 'PENDING', 'VERIFIED')),
                    CONSTRAINT account_email_verification_verified_at
                        CHECK ((state = 'VERIFIED') = (verified_at IS NOT NULL))
                );
                INSERT INTO account_email_verification (user_id, state)
                    SELECT id, 'LEGACY_EXEMPT' FROM auth_user
                    ON CONFLICT DO NOTHING;
                CREATE TABLE email_verification_challenge (
                    user_id integer PRIMARY KEY
                        REFERENCES auth_user(id) ON DELETE CASCADE,
                    token_digest char(64) NOT NULL UNIQUE,
                    email_normalized varchar(254) NOT NULL,
                    issued_at timestamptz NOT NULL,
                    expires_at timestamptz NOT NULL,
                    consumed_at timestamptz,
                    delivery_status varchar(16) NOT NULL DEFAULT 'UNCONFIRMED',
                    delivery_finished_at timestamptz,
                    CONSTRAINT email_verification_challenge_digest
                        CHECK (token_digest ~ '^[0-9a-f]{64}$'),
                    CONSTRAINT email_verification_challenge_lifetime
                        CHECK (expires_at = issued_at + interval '3600 seconds'),
                    CONSTRAINT email_verification_challenge_consumed
                        CHECK (consumed_at IS NULL
                            OR (consumed_at >= issued_at AND consumed_at < expires_at)),
                    CONSTRAINT email_verification_challenge_delivery
                        CHECK (delivery_status IN ('UNCONFIRMED', 'SUBMITTED', 'FAILED'))
                );
                CREATE TABLE email_verification_limit_bucket (
                    scope varchar(24) NOT NULL,
                    subject_digest char(64) NOT NULL,
                    PRIMARY KEY (scope, subject_digest),
                    CONSTRAINT email_verification_limit_scope
                        CHECK (scope IN ('RESEND_CALLER', 'RESEND_RECIPIENT',
                                         'INVALID_CONFIRM_CALLER')),
                    CONSTRAINT email_verification_limit_subject
                        CHECK (subject_digest ~ '^[0-9a-f]{64}$')
                );
                CREATE TABLE email_verification_limit_event (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    scope varchar(24) NOT NULL,
                    subject_digest char(64) NOT NULL,
                    occurred_at timestamptz NOT NULL,
                    FOREIGN KEY (scope, subject_digest)
                        REFERENCES email_verification_limit_bucket (scope, subject_digest)
                        ON DELETE CASCADE
                );
                CREATE INDEX email_verification_limit_event_window_idx
                    ON email_verification_limit_event
                    (scope, subject_digest, occurred_at, id);
            """,
            reverse_sql="""
                DROP TABLE IF EXISTS email_verification_limit_event;
                DROP TABLE IF EXISTS email_verification_limit_bucket;
                DROP TABLE IF EXISTS email_verification_challenge;
                DROP TABLE IF EXISTS account_email_verification;
            """,
        ),
    ]
