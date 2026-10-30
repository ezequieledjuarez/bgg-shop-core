import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from ... import payments


class Command(BaseCommand):
    help = "Inspect or explicitly search for late sandbox payments on credit-guarded orders."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--after")
        parser.add_argument("--max-orders", type=int, default=10)
        parser.add_argument("--max-pages", type=int, default=10)
        parser.add_argument("--max-lookups", type=int, default=100)

    def handle(self, *args, **options):
        max_orders, max_pages, max_lookups = options["max_orders"], options["max_pages"], options["max_lookups"]
        if connection.vendor != "postgresql":
            raise CommandError("Payment discovery requires PostgreSQL.")
        if not 1 <= max_orders <= 100 or not 1 <= max_pages <= 100:
            raise CommandError("Order and page limits must be between 1 and 100.")
        if not 1 <= max_lookups <= 1000:
            raise CommandError("Payment lookup limit must be between 1 and 1000.")
        try:
            after = uuid.UUID(options["after"]) if options["after"] else None
        except (ValueError, TypeError, AttributeError):
            raise CommandError("Invalid order cursor.") from None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT order_id FROM credit_retry_guard "
                "WHERE (%s::uuid IS NULL OR order_id > %s::uuid) "
                "ORDER BY order_id LIMIT %s", [after, after, max_orders + 1],
            )
            rows = cursor.fetchall()
        orders = [str(row[0]) for row in rows[:max_orders]]
        if not options["apply"]:
            self.stdout.write(f"Guarded orders in batch: {len(orders)}. No provider calls or changes made.")
            return
        if payments.configuration_error():
            raise CommandError("Sandbox payment configuration is required.")
        detected = 0
        remaining_lookups = [max_lookups]
        for order_id in orders:
            try:
                payment_ids = self._verified_payments(order_id, max_pages, remaining_lookups)
                with transaction.atomic(), connection.cursor() as cursor:
                    for payment_id in payment_ids:
                        cursor.execute(
                            "INSERT INTO credit_payment_review (order_id, payment_id) "
                            "VALUES (%s, %s) ON CONFLICT (order_id, payment_id) DO NOTHING",
                            [order_id, payment_id],
                        )
                        detected += cursor.rowcount
            except Exception:
                raise CommandError("Payment discovery failed; earlier orders may have been recorded. Retry the batch.") from None
        next_cursor = orders[-1] if len(rows) > max_orders else "none"
        self.stdout.write(
            f"Scanned {len(orders)} guarded orders; added {detected} review records. "
            f"Next cursor: {next_cursor}. Provider search does not prove absence of older payments."
        )

    def _verified_payments(self, order_id, max_pages, remaining_lookups):
        verified = set()
        offset = 0
        limit = 50
        for _page in range(max_pages):
            result = payments.search_payments(order_id, offset, limit)
            if not isinstance(result, dict) or result.get("status") != 200:
                raise ValueError
            response = result.get("response")
            if not isinstance(response, dict):
                raise ValueError
            paging, items = response.get("paging"), response.get("results")
            if not isinstance(paging, dict) or not isinstance(items, list):
                raise ValueError
            total, page_offset, page_limit = paging.get("total"), paging.get("offset"), paging.get("limit")
            if (type(total) is not int or total < 0 or type(page_offset) is not int or page_offset != offset
                    or type(page_limit) is not int or not 1 <= page_limit <= limit
                    or len(items) > page_limit or offset + len(items) > total):
                raise ValueError
            for item in items:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                    raise ValueError
                payment_id = str(item["id"])
                if len(payment_id) > 128:
                    raise ValueError
                if remaining_lookups[0] == 0:
                    raise ValueError
                remaining_lookups[0] -= 1
                fetched = payments.fetch_payment(payment_id, bounded=True)
                if not isinstance(fetched, dict) or fetched.get("status") != 200:
                    raise ValueError
                payment = fetched.get("response")
                if (not isinstance(payment, dict) or type(payment.get("id")) is not int
                    or payment["id"] != item["id"]
                    or payment.get("external_reference") != order_id
                    or not payments.is_sandbox_payment(payment)):
                    raise ValueError
                if payment.get("status") == "approved":
                    verified.add(payment_id)
            offset += len(items)
            if offset == total:
                return sorted(verified)
            if not items or len(items) < page_limit or offset > 10000:
                raise ValueError
        raise ValueError