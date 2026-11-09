import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Inspect or remove chat conversations inactive for 30 days."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("Chat conversation cleanup requires PostgreSQL.")

        current_time = timezone.now()
        if timezone.is_naive(current_time):
            current_time = timezone.make_aware(
                current_time, timezone.get_default_timezone()
            )
        cutoff = current_time - datetime.timedelta(days=30)
        if not options["apply"]:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM chat_conversation "
                    "WHERE updated_at <= %s",
                    [cutoff],
                )
                count = cursor.fetchone()[0]
            self.stdout.write(
                f"Eligible inactive conversations: {count}. No changes made."
            )
            return

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM chat_conversation WHERE updated_at <= %s",
                [cutoff],
            )
            deleted = cursor.rowcount
        self.stdout.write(f"Deleted {deleted} inactive conversations.")
