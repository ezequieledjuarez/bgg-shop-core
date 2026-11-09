from threading import Event

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError


class Command(BaseCommand):
    help = "Run chat conversation retention every minute."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = Event()
        while True:
            try:
                call_command(
                    "purge_chat_conversations", apply=True, stdout=self.stdout
                )
            except DatabaseError:
                self.stderr.write("Chat conversation retention failed.")
                if options["once"]:
                    raise CommandError(
                        "Chat conversation retention failed."
                    ) from None
            if options["once"]:
                return
            stop.wait(60)
