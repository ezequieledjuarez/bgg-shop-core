from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from opkeys.models import OpSession


class Command(BaseCommand):
    help = "Report or remove operation-key sessions expired beyond the retention period."

    def add_arguments(self, parser):
        parser.add_argument("--retention-days", type=int, default=30)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        days = options["retention_days"]
        if days < 0:
            raise CommandError("Retention days cannot be negative.")
        sessions = OpSession.objects.filter(expires_at__lt=timezone.now() - timedelta(days=days))
        if options["apply"]:
            deleted, _ = sessions.delete()
            self.stdout.write(f"Deleted {deleted} expired session records.")
        else:
            self.stdout.write(f"Dry run: {sessions.count()} expired sessions eligible for removal.")