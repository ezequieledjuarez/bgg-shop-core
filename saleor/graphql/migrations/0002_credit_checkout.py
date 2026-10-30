"""Add credit checkout records without removing existing redemption data.

This migration is irreversible. Stop writers and retain verified backups of credit
and order records before planning any separately reviewed rollback or archival.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0001_credit_accounts")]

    operations = [
        migrations.RunSQL("""
            ALTER TABLE purchase_order
                ADD CONSTRAINT purchase_order_id_user_unique UNIQUE (id, user_id);
            ALTER TABLE credit_transaction ALTER COLUMN gift_card_id DROP NOT NULL;
            ALTER TABLE credit_transaction
                ADD COLUMN order_id uuid UNIQUE,
                ADD CONSTRAINT credit_transaction_order_owner_fk
                    FOREIGN KEY (order_id, user_id)
                    REFERENCES purchase_order (id, user_id) ON DELETE RESTRICT,
                ADD CONSTRAINT credit_transaction_source_check
                    CHECK ((gift_card_id IS NULL) <> (order_id IS NULL)),
                ADD CONSTRAINT credit_transaction_payment_unique
                    UNIQUE (id, user_id, order_id);
            ALTER TABLE credit_transaction DROP CONSTRAINT credit_transaction_amount_check;
            ALTER TABLE credit_transaction ADD CONSTRAINT credit_transaction_amount_check
                CHECK (amount >= 0 AND amount <= 9999999999999999999999999999.99
                       AND (gift_card_id IS NULL OR amount > 0));
            CREATE TABLE credit_payment (
                order_id uuid PRIMARY KEY,
                user_id integer NOT NULL REFERENCES credit_account(user_id) ON DELETE RESTRICT,
                transaction_id bigint NOT NULL UNIQUE,
                amount numeric(12, 2) NOT NULL CHECK (amount >= 0),
                currency varchar(3) NOT NULL CHECK (currency = 'ARS'),
                CONSTRAINT credit_payment_order_owner_fk FOREIGN KEY (order_id, user_id)
                    REFERENCES purchase_order (id, user_id) ON DELETE RESTRICT,
                CONSTRAINT credit_payment_transaction_fk FOREIGN KEY (transaction_id, user_id, order_id)
                    REFERENCES credit_transaction (id, user_id, order_id) ON DELETE RESTRICT
            );
            ALTER TABLE credit_idempotency DROP CONSTRAINT credit_idempotency_operation_check;
            ALTER TABLE credit_idempotency ADD CONSTRAINT credit_idempotency_operation_check
                CHECK (operation IN ('REDEEM', 'CHECKOUT'));
            ALTER TABLE credit_idempotency ADD COLUMN order_snapshot jsonb;
            ALTER TABLE credit_idempotency ADD CONSTRAINT credit_idempotency_order_snapshot_check
                CHECK ((operation = 'REDEEM' AND order_snapshot IS NULL) OR
                       (operation = 'CHECKOUT' AND
                        ((error_code IS NULL AND order_snapshot IS NOT NULL) OR
                         (error_code IS NOT NULL AND order_snapshot IS NULL))));
        """),
    ]