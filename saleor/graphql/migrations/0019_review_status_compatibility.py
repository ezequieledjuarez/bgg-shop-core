from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0018_audit_log_cursor_index")]

    operations = [
        migrations.RunSQL(
            sql=[
                (
                    "ALTER TABLE product_review "
                    "ADD COLUMN is_deleted boolean NOT NULL DEFAULT FALSE;"
                ),
                "DROP INDEX idx_product_review_approved_game;",
                (
                    "ALTER TABLE product_review "
                    "DROP CONSTRAINT chk_product_review_status;"
                ),
                (
                    "UPDATE product_review "
                    "SET is_deleted = (status = 'DELETED'), "
                    "status = CASE status "
                    "WHEN 'PENDING' THEN 'PENDING' "
                    "WHEN 'APPROVED' THEN 'ACCEPTED' "
                    "WHEN 'HIDDEN' THEN 'REJECTED' "
                    "WHEN 'DELETED' THEN 'REJECTED' "
                    "END;"
                ),
                (
                    "ALTER TABLE product_review "
                    "ADD CONSTRAINT chk_product_review_status "
                    "CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED'));"
                ),
                (
                    "CREATE INDEX idx_product_review_public_game "
                    "ON product_review (bgg_id, created_at) "
                    "WHERE status = 'ACCEPTED' AND is_deleted = FALSE;"
                ),
            ],
            reverse_sql=[
                "DROP INDEX idx_product_review_public_game;",
                (
                    "ALTER TABLE product_review "
                    "DROP CONSTRAINT chk_product_review_status;"
                ),
                (
                    "UPDATE product_review "
                    "SET status = CASE "
                    "WHEN is_deleted THEN 'DELETED' "
                    "WHEN status = 'PENDING' THEN 'PENDING' "
                    "WHEN status = 'ACCEPTED' THEN 'APPROVED' "
                    "WHEN status = 'REJECTED' THEN 'HIDDEN' "
                    "END;"
                ),
                "ALTER TABLE product_review DROP COLUMN is_deleted;",
                (
                    "ALTER TABLE product_review "
                    "ADD CONSTRAINT chk_product_review_status "
                    "CHECK (status IN ('PENDING', 'APPROVED', 'HIDDEN', 'DELETED'));"
                ),
                (
                    "CREATE INDEX idx_product_review_approved_game "
                    "ON product_review (bgg_id, created_at) "
                    "WHERE status = 'APPROVED';"
                ),
            ],
        ),
    ]
