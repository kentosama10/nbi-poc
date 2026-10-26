"""Slot listing, atomic booking, and Redis-vs-PostgreSQL reconciliation.

Booking order (and why):
  1. Check the application exists and has no active booking (PostgreSQL).
  2. Take ONE seat in Redis with an atomic Lua script (fast, race-free).
  3. Write the appointment + an outbox event in ONE PostgreSQL transaction.
  4. If step 3 fails, give the seat back (compensation).
If the whole server crashes between steps 2 and 3, a seat is "lost" in Redis.
reconcile_slots() finds and repairs that, because PostgreSQL is the truth.
"""
from datetime import date, datetime
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients import get_redis
from app.models import Application, Appointment
from app.outbox import enqueue
from app.slots import PH_TZ, SITES, SLOT_INDEX_KEY, slot_key

# Reverse lookup: "NBI Main - Manila" -> "MNL"
CODE_BY_NAME = {name: code for code, name in SITES.items()}

# Lua runs INSIDE Redis as one uninterruptible step. No other command can run
# between the check and the decrement, so two requests can never both take the
# last seat.
RESERVE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return -2                                   -- slot does not exist
end
local available = tonumber(redis.call('HGET', KEYS[1], 'available'))
if available <= 0 then
    return -1                                   -- slot is full
end
return redis.call('HINCRBY', KEYS[1], 'available', -1)   -- seats left after ours
"""

# Give one seat back, but never go above the slot's capacity.
RELEASE_LUA = """
local available = tonumber(redis.call('HGET', KEYS[1], 'available'))
local capacity = tonumber(redis.call('HGET', KEYS[1], 'capacity'))
if available < capacity then
    return redis.call('HINCRBY', KEYS[1], 'available', 1)
end
return available
"""


class BookingError(Exception):
    """A booking problem the caller should see, with an HTTP status code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@lru_cache(maxsize=1)
def _scripts():
    """Register both Lua scripts once. redis-py then runs them by their hash."""
    r = get_redis()
    return r.register_script(RESERVE_LUA), r.register_script(RELEASE_LUA)


def _release_seat(key: str) -> None:
    _scripts()[1](keys=[key])


# ------------------------------------------------------------------ listing
def list_slots(site_code: str | None = None, day: date | None = None,
               include_full: bool = False, limit: int = 100) -> list[dict]:
    """Read slots from Redis (earliest first), with optional filters."""
    r = get_redis()
    keys = r.zrange(SLOT_INDEX_KEY, 0, -1)
    pipe = r.pipeline()                 # one round trip for all 241 slots
    for key in keys:
        pipe.hgetall(key)
    out = []
    for key, h in zip(keys, pipe.execute()):
        if not h:
            continue
        available = int(h["available"])
        start = datetime.fromisoformat(h["start"])
        if site_code and h["site_code"] != site_code.upper():
            continue
        if day and start.date() != day:
            continue
        if available <= 0 and not include_full:
            continue
        out.append({
            "key": key, "site_code": h["site_code"], "site": h["site"],
            "start": h["start"], "capacity": int(h["capacity"]),
            "available": available, "demo": h["demo"] == "1",
        })
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ booking
def book_appointment(session: Session, application_ref: str, key: str) -> dict:
    r = get_redis()

    # Step 1: checks in PostgreSQL
    application = session.scalar(
        select(Application).where(Application.reference_no == application_ref))
    if application is None:
        raise BookingError(404, "Application not found")
    app_id = application.id             # save now: a rollback would expire the object

    if session.scalar(select(Appointment.id).where(
            Appointment.application_id == app_id, Appointment.status == "BOOKED")):
        raise BookingError(409, "This application already has a booked appointment")

    slot = r.hgetall(key)
    if not slot:
        raise BookingError(404, "Slot not found")

    # Step 2: take one seat, atomically
    seats_left = _scripts()[0](keys=[key])
    if seats_left == -2:
        raise BookingError(404, "Slot not found")
    if seats_left == -1:
        raise BookingError(409, "Slot is full")

    # Step 3: permanent record + outbox event, in ONE transaction
    try:
        appointment = Appointment(
            application_id=app_id, site=slot["site"],
            slot_start=datetime.fromisoformat(slot["start"]), status="BOOKED",
        )
        session.add(appointment)
        session.flush()                 # sends the INSERT; the unique index is checked here
        enqueue(session, app_id, "APPOINTMENT_BOOKED",
                f"Appointment booked at {slot['site']}", source="booking-api")
        session.commit()
    except IntegrityError:
        # Safety net fired: this application already holds a BOOKED appointment
        session.rollback()
        _release_seat(key)
        raise BookingError(409, "This application already has a booked appointment")
    except Exception:
        # Anything else: undo the Redis seat, then let the error surface as a 500
        session.rollback()
        _release_seat(key)
        raise

    return {
        "appointment_id": appointment.id,
        "application_ref": application_ref,
        "slot_key": key,
        "site": slot["site"],
        "slot_start": slot["start"],
        "seats_left": int(seats_left),
    }


# ----------------------------------------------------------- reconciliation
def reconcile_slots(session: Session, fix: bool = False) -> list[dict]:
    """Compare every Redis counter with PostgreSQL's real bookings.
    expected = capacity - BOOKED appointments. Returns the mismatches found.
    With fix=True, overwrites the Redis counter with the expected value.
    Run it when the system is quiet (a booking in flight can look like a mismatch)."""
    r = get_redis()
    rows = session.execute(
        select(Appointment.site, Appointment.slot_start, func.count())
        .where(Appointment.status == "BOOKED")
        .group_by(Appointment.site, Appointment.slot_start)
    ).all()
    booked = {}
    for site, start, n in rows:
        code = CODE_BY_NAME.get(site)
        if code:
            booked[slot_key(code, start.astimezone(PH_TZ))] = n

    keys = r.zrange(SLOT_INDEX_KEY, 0, -1)
    pipe = r.pipeline()
    for key in keys:
        pipe.hmget(key, "capacity", "available")
    mismatches = []
    for key, (capacity, available) in zip(keys, pipe.execute()):
        expected = max(int(capacity) - booked.get(key, 0), 0)
        if int(available) != expected:
            mismatches.append({"key": key, "expected": expected,
                               "actual": int(available)})
            if fix:
                r.hset(key, "available", expected)
    return mismatches