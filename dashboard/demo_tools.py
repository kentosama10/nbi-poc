"""DEMO-ONLY helpers that touch the databases directly (the API has no such
endpoints on purpose). Real requests in the demo still go through the API."""
from datetime import datetime

from sqlalchemy import delete, func, select

from app.clients import get_redis
from app.db import SessionLocal
from app.models import Application, Appointment, WatchlistRecord
from app.slots import DEMO_POINTER_KEY


def reset_demo_slot() -> str:
    """Free the one-seat demo slot again: delete its bookings in PostgreSQL and
    put the seat back in Redis. Returns the slot key."""
    r = get_redis()
    key = r.get(DEMO_POINTER_KEY)
    if not key:
        raise RuntimeError("Demo slot not found. Run: python -m scripts.load_data --reload")
    slot = r.hgetall(key)
    start = datetime.fromisoformat(slot["start"])
    with SessionLocal() as s:
        s.execute(delete(Appointment).where(Appointment.site == slot["site"],
                                            Appointment.slot_start == start))
        s.commit()
    r.hset(key, "available", slot["capacity"])
    return key


def free_application_refs(n: int) -> list[str]:
    """n applications (highest ids first) that have no active booking."""
    booked_ids = select(Appointment.application_id).where(Appointment.status == "BOOKED")
    with SessionLocal() as s:
        return list(s.scalars(
            select(Application.reference_no)
            .where(~Application.id.in_(booked_ids))
            .order_by(Application.id.desc()).limit(n)))


def booked_count(refs: list[str]) -> int:
    """How many ACTIVE bookings these applications hold in PostgreSQL (the truth)."""
    with SessionLocal() as s:
        ids = select(Application.id).where(Application.reference_no.in_(refs))
        return int(s.scalar(
            select(func.count()).select_from(Appointment)
            .where(Appointment.application_id.in_(ids), Appointment.status == "BOOKED")))


def common_name_record() -> dict:
    """A watchlist record that has the most common surname in the watchlist."""
    with SessionLocal() as s:
        surname = s.execute(
            select(WatchlistRecord.last_name, func.count().label("n"))
            .group_by(WatchlistRecord.last_name)
            .order_by(func.count().desc())).first()[0]
        r = s.scalars(select(WatchlistRecord)
                      .where(WatchlistRecord.last_name == surname)
                      .order_by(WatchlistRecord.id)).first()
        return {"first_name": r.first_name, "middle_name": r.middle_name,
                "last_name": r.last_name, "birthdate": r.birthdate.isoformat(),
                "birthplace": r.birthplace,
                "mother_maiden_name": r.mother_maiden_name}