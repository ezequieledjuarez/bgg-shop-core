from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from ...credit import REJECTION_RETENTION


class Command(BaseCommand):
    help = "Inspect or explicitly remove expired failed credit idempotency records."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--max-batches", type=int, default=10)

    def handle(self, *args, **options):
        batch_size, max_batches = options["batch_size"], options["max_batches"]
        if not 1 <= batch_size <= 1000 or not 1 <= max_batches <= 1000:
            raise CommandError("Batch size and maximum batches must be between 1 and 1000.")
        if connection.vendor != "postgresql":
            raise CommandError("Credit rejection cleanup requires PostgreSQL.")
        cutoff = timezone.now() - REJECTION_RETENTION
        if not options["apply"]:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM credit_idempotency "
                    "WHERE error_code IS NOT NULL AND created_at <= %s", [cutoff],
                )
                count = cursor.fetchone()[0]
            self.stdout.write(f"Eligible rejected records: {count}. No changes made.")
            return
        removed = 0
        for _batch in range(max_batches):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT account.user_id FROM credit_account account WHERE EXISTS ("
                    "SELECT 1 FROM credit_idempotency record WHERE record.user_id = account.user_id "
                    "AND record.error_code IS NOT NULL AND record.created_at <= %s) "
                    "ORDER BY account.user_id LIMIT 1 FOR UPDATE OF account SKIP LOCKED", [cutoff],
                )
                account = cursor.fetchone()
                if account is None:
                    break
                cursor.execute(
                    "DELETE FROM credit_idempotency WHERE id IN ("
                    "SELECT id FROM credit_idempotency WHERE user_id = %s "
                    "AND error_code IS NOT NULL AND created_at <= %s "
                    "ORDER BY created_at, id LIMIT %s)", [account[0], cutoff, batch_size],
                )
                removed += cursor.rowcount
        self.stdout.write(f"Removed {removed} expired rejected records; more may remain.")