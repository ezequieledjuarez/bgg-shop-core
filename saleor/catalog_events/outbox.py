import uuid
from datetime import datetime, timezone as datetime_timezone


_EVENT_TYPES = {
    "product.changed": "product",
    "stock.changed": "stock",
    "promotion.changed": "promotion",
}


def record_catalog_event(cursor, event_type, aggregate_id):
    aggregate_type = _EVENT_TYPES.get(event_type)
    if aggregate_type is None:
        raise ValueError("Unsupported catalog event type")

    event_id = uuid.uuid4()
    occurred_at = datetime.now(datetime_timezone.utc).replace(tzinfo=None)
    cursor.execute(
        "INSERT INTO catalog_event_outbox "
        "(event_id, event_type, occurred_at, aggregate_type, aggregate_id, "
        "attempts, next_attempt_at, delivered_at) "
        "VALUES (%s, %s, %s, %s, %s, 0, %s, NULL)",
        [
            str(event_id),
            event_type,
            occurred_at,
            aggregate_type,
            str(aggregate_id),
            occurred_at,
        ],
    )
    return event_id
