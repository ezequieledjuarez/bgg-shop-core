"""Record conservative retry blocks and uncertain provider attempts.

A credit purchase blocks every currently rejected order for the same buyer,
including unrelated historical orders. Previously issued preferences remain
payable at the provider and require separate reconciliation. Retry attempts
block credit until an approved payment is verified against the same preference.
Rejected or ambiguous attempts require manual provider-side reconciliation.
If the provider does not return that association, manual reconciliation is needed.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0002_credit_checkout")]

    operations = [
        migrations.RunSQL("""
            CREATE TABLE credit_retry_guard (
                order_id uuid PRIMARY KEY,
                user_id integer NOT NULL,
                credit_order_id uuid NOT NULL,
                FOREIGN KEY (order_id, user_id)
                    REFERENCES purchase_order (id, user_id) ON DELETE RESTRICT,
                FOREIGN KEY (credit_order_id, user_id)
                    REFERENCES purchase_order (id, user_id) ON DELETE RESTRICT
            );
            CREATE TABLE credit_retry_attempt (
                order_id uuid PRIMARY KEY,
                user_id integer NOT NULL,
                preference_id varchar(128),
                FOREIGN KEY (order_id, user_id)
                    REFERENCES purchase_order (id, user_id) ON DELETE RESTRICT
            );
            CREATE INDEX credit_retry_attempt_user_idx ON credit_retry_attempt (user_id);
        """),
    ]