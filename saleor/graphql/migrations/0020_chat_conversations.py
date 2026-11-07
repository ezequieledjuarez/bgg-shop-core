from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("graphql", "0019_review_status_compatibility"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE TABLE chat_conversation (
                    id uuid PRIMARY KEY,
                    owner_type varchar(16) NOT NULL,
                    owner_id varchar(64) NOT NULL,
                    owner_user_id integer
                        REFERENCES auth_user(id) ON DELETE CASCADE,
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT chat_conversation_owner_type
                        CHECK (owner_type IN ('USER', 'ANONYMOUS')),
                    CONSTRAINT chat_conversation_owner_identity
                        CHECK (
                            (owner_type = 'USER'
                                AND owner_user_id IS NOT NULL
                                AND owner_id = owner_user_id::text)
                            OR
                            (owner_type = 'ANONYMOUS'
                                AND owner_user_id IS NULL
                                AND owner_id ~ '^[0-9a-f]{64}$')
                        ),
                    CONSTRAINT chat_conversation_activity_order
                        CHECK (updated_at >= created_at)
                );
                CREATE INDEX chat_conversation_owner_activity_idx
                    ON chat_conversation (owner_type, owner_id, updated_at DESC);
                CREATE TABLE chat_message (
                    id uuid PRIMARY KEY,
                    conversation_id uuid NOT NULL
                        REFERENCES chat_conversation(id) ON DELETE CASCADE,
                    role varchar(16) NOT NULL
                        CHECK (role IN ('USER', 'ASSISTANT')),
                    content text NOT NULL
                        CHECK (char_length(btrim(content)) > 0),
                    sources jsonb NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(sources) = 'array'),
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX chat_message_conversation_order_idx
                    ON chat_message (conversation_id, created_at, id);
            """,
            reverse_sql="""
                DROP TABLE chat_message;
                DROP TABLE chat_conversation;
            """,
        ),
    ]
