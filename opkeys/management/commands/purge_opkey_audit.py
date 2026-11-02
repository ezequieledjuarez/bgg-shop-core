from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from opkeys.models import OpKeyAudit


class Command(BaseCommand):
    help = "Report or remove operation-key audit records older than seven days."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        records = OpKeyAudit.objects.filter(created_at__lt=timezone.now() - timedelta(days=7))
        if options["apply"]:
            deleted, _ = records.delete()
            self.stdout.write(f"Deleted {deleted} expired audit records.")
        else:
            self.stdout.write(f"Dry run: {records.count()} audit records eligible for removal.")