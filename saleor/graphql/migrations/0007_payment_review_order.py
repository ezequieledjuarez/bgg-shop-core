from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("graphql", "0006_credit_rejection_expiry_index")]

    operations = [
        migrations.RunSQL("""
            ALTER TABLE credit_payment_review
                DROP CONSTRAINT credit_payment_review_order_id_fkey;
            ALTER TABLE credit_payment_review
                ADD CONSTRAINT credit_payment_review_order_id_fkey
                FOREIGN KEY (order_id) REFERENCES purchase_order(id) ON DELETE RESTRICT;
        """),
    ]