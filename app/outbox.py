"""Outbox helper: queue an event to be delivered to the MongoDB timeline.

The event is added to the SAME database transaction as the business change.
If the transaction commits, the event exists; if it rolls back, the event
vanishes with it. Phase 6 adds the code that delivers these rows to MongoDB.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import OutboxEvent


def enqueue(session: Session, application_id: int, event_type: str,
            detail: str, source: str = "api") -> None:
    """Add an outbox row. It does NOT commit: the caller commits everything together."""
    session.add(OutboxEvent(
        application_id=application_id,
        event_type=event_type,
        payload={
            "detail": detail,
            "source": source,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    ))