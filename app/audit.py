"""Audit log: write and read. The table itself is append-only (Phase 2 trigger).

log_event() uses its OWN session and commits immediately. That matters:
a denied request often rolls back or aborts its main transaction, but the
record of the attempt must survive.

NEVER pass personal data (names, birthdates...) or API keys into this file.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AuditLog


def log_event(username: str, role: str, action: str, resource_type: str,
              resource_id: str | None = None, outcome: str = "ALLOWED",
              detail: dict | None = None) -> None:
    """Write one audit row. Raises if it cannot, so callers fail closed."""
    with SessionLocal() as s:
        s.add(AuditLog(
            username=username[:50], role=role[:20], action=action[:40],
            resource_type=resource_type[:40],
            resource_id=None if resource_id is None else str(resource_id)[:60],
            outcome=outcome, detail=detail,
        ))
        s.commit()


def list_audit(session: Session, limit: int, offset: int, action: str | None,
               outcome: str | None, username: str | None) -> dict:
    """Newest first, with optional filters."""
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if outcome:
        conditions.append(AuditLog.outcome == outcome)
    if username:
        conditions.append(AuditLog.username == username)

    query = select(AuditLog).order_by(AuditLog.id.desc())
    count = select(func.count()).select_from(AuditLog)
    if conditions:
        query, count = query.where(*conditions), count.where(*conditions)

    rows = session.scalars(query.limit(limit).offset(offset)).all()
    return {
        "total_matching": session.scalar(count),
        "returned": len(rows),
        "items": [{
            "id": r.id, "occurred_at": r.occurred_at.isoformat(),
            "username": r.username, "role": r.role, "action": r.action,
            "resource_type": r.resource_type, "resource_id": r.resource_id,
            "outcome": r.outcome, "detail": r.detail,
        } for r in rows],
    }