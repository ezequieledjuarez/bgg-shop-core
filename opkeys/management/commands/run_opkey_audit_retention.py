from threading import Event

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError


class Command(BaseCommand):
    help = "Run operation-key audit retention every minute."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = Event()
        while True:
            try:
                call_command("purge_opkey_audit", apply=True, stdout=self.stdout)
            except DatabaseError:
                self.stderr.write("Operation-key audit retention failed.")
                if options["once"]:
                    raise CommandError("Operation-key audit retention failed.") from None
            if options["once"]:
                return
            stop.wait(60)