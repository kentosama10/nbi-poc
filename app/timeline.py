"""Copy outbox rows into the MongoDB status timeline, and read the timeline back."""
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients import TIMELINE_COLLECTION, get_mongo_db
from app.db import SessionLocal
from app.models import Application, OutboxEvent

MAX_ATTEMPTS = 10   # after this many failures an event waits for a human ("dead letter")


def _push_to_mongo(coll, ev: OutboxEvent, application_ref: str) -> None:
    """Append one event to the application's timeline document.

    Idempotent: the filter says "only if this outbox_id is not already in the
    events array". If it is, MongoDB tries to insert a document with the same
    _id, fails with DuplicateKeyError, and we treat that as "already delivered".
    """
    event = {
        "type": ev.event_type,
        "at": datetime.fromisoformat(ev.payload["at"]),
        "detail": ev.payload["detail"],
        "source": ev.payload["source"],
        "outbox_id": ev.id,
    }
    try:
        coll.update_one(
            {"_id": ev.application_id, "events.outbox_id": {"$ne": ev.id}},
            {
                "$push": {"events": event},
                "$set": {"updated_at": datetime.now(timezone.utc)},
                "$setOnInsert": {"application_ref": application_ref},
            },
            upsert=True,        # create the timeline if it does not exist yet
        )
    except DuplicateKeyError:
        pass                    # already delivered earlier


def deliver_pending(limit: int = 50, max_failures: int = 3) -> dict:
    """Deliver unprocessed outbox rows to MongoDB, oldest first.

    Each row gets its own short transaction. SKIP LOCKED means two workers
    never fight over the same row. A failed row has its attempts counter
    raised and will be retried on the next run.
    """
    coll = get_mongo_db()[TIMELINE_COLLECTION]
    delivered = failed = 0
    tried: set[int] = set()     # do not retry a failing row within this run

    while delivered + failed < limit and failed < max_failures:
        conditions = [OutboxEvent.processed_at.is_(None),
                      OutboxEvent.attempts < MAX_ATTEMPTS]
        if tried:
            conditions.append(OutboxEvent.id.notin_(tried))
        with SessionLocal() as s:
            row = s.execute(
                select(OutboxEvent, Application.reference_no)
                .join(Application, Application.id == OutboxEvent.application_id)
                .where(*conditions)
                .order_by(OutboxEvent.id).limit(1)
                .with_for_update(skip_locked=True, of=OutboxEvent)
            ).first()
            if row is None:
                break
            ev, ref = row
            tried.add(ev.id)
            try:
                _push_to_mongo(coll, ev, ref)
                ev.processed_at = datetime.now(timezone.utc)
                delivered += 1
            except Exception:
                ev.attempts += 1
                failed += 1
            s.commit()
    return {"delivered": delivered, "failed": failed}


def pending_count(session: Session, application_id: int) -> int:
    """Outbox rows for this application that MongoDB has not received yet."""
    return session.scalar(
        select(func.count()).select_from(OutboxEvent)
        .where(OutboxEvent.application_id == application_id,
               OutboxEvent.processed_at.is_(None))) or 0


def get_timeline(application_id: int) -> list[dict]:
    """Read the timeline from MongoDB, oldest event first."""
    doc = get_mongo_db()[TIMELINE_COLLECTION].find_one({"_id": application_id})
    if not doc:
        return []
    events = []
    for e in doc["events"]:
        at = e["at"]
        if at.tzinfo is None:               # MongoDB returns UTC without a label
            at = at.replace(tzinfo=timezone.utc)
        events.append((at, {"type": e["type"], "detail": e["detail"],
                            "source": e["source"]}))
    events.sort(key=lambda pair: pair[0])   # sort by real time, not by text
    return [{**data, "at": at.isoformat()} for at, data in events]