"""Operational analytics (pain point #5). AGGREGATES ONLY: no names, birthdates,
or watchlist evidence ever appear here.

Everything is a snapshot of the PostgreSQL system of record (plus Redis for
seat counts). It never uses ground-truth labels, so it cannot say whether an
auto-clear was CORRECT. Precision and recall come from the evaluation (Phase 11).
"""
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.booking import CODE_BY_NAME
from app.clients import get_redis
from app.hit_check import FINISHED_STATUSES
from app.models import Application, Appointment, HitCase, OutboxEvent, Payment
from app.slots import PH_TZ, SLOT_INDEX_KEY, slot_key
from app.timeline import MAX_ATTEMPTS

ALL_STATUSES = ("SUBMITTED", "PAID", "SCHEDULED", "UNDER_REVIEW",
                "ESCALATED", "CLEARED", "HIT_CONFIRMED")


def _ratio(part: int, whole: int) -> float | None:
    """part / whole, or None when there is nothing to divide by (never a fake 0)."""
    return round(part / whole, 4) if whole else None


def _round(value) -> float | None:
    return None if value is None else round(float(value), 3)


# ------------------------------------------------------------- applications
def application_stats(session: Session) -> dict:
    counts = {s: 0 for s in ALL_STATUSES}
    for status, n in session.execute(
            select(Application.status, func.count()).group_by(Application.status)):
        counts[status] = int(n)
    return {"total": sum(counts.values()), "by_status": counts}


# --------------------------------------------------------------- HIT funnel
def hit_stats(session: Session, checked: int) -> dict:
    # One row per application that has candidates; count how many of its
    # candidate pairs are NOT auto-cleared.
    per_app = (
        select(HitCase.application_id.label("app_id"),
               func.count().filter(HitCase.decision_band != "AUTO_CLEAR")
               .label("n_review_worthy"))
        .group_by(HitCase.application_id)
        .subquery()
    )
    flagged, need_review = session.execute(
        select(func.count(),
               func.count().filter(per_app.c.n_review_worthy > 0))
        .select_from(per_app)
    ).one()
    flagged, need_review = int(flagged), int(need_review)
    avoided = flagged - need_review

    pairs = {"AUTO_CLEAR": 0, "REVIEW": 0, "ESCALATE": 0}
    for band, n in session.execute(
            select(HitCase.decision_band, func.count()).group_by(HitCase.decision_band)):
        pairs[band] = int(n)
    pairs_total = sum(pairs.values())

    return {
        "checked_applications": checked,
        "flagged_by_name": flagged,
        "hit_rate": _ratio(flagged, checked),
        "manual_reviews_baseline": flagged,        # AS-IS: every flagged case goes to a person
        "manual_reviews_with_ai": need_review,
        "manual_reviews_avoided": avoided,
        "auto_clear_rate": _ratio(avoided, flagged),                 # per application
        "pairs": {"total": pairs_total, "auto_clear": pairs["AUTO_CLEAR"],
                  "review": pairs["REVIEW"], "escalate": pairs["ESCALATE"]},
        "auto_clear_rate_pairs": _ratio(pairs["AUTO_CLEAR"], pairs_total),
    }


# -------------------------------------------------------------------- queue
def queue_stats(session: Session) -> dict:
    by_band = {"ESCALATE": 0, "REVIEW": 0}
    for band, n in session.execute(
            select(HitCase.decision_band, func.count())
            .where(HitCase.status == "OPEN").group_by(HitCase.decision_band)):
        by_band[band] = int(n)

    open_cases = session.scalar(
        select(func.count()).select_from(HitCase).where(HitCase.status == "OPEN"))
    open_apps = session.scalar(
        select(func.count(func.distinct(HitCase.application_id)))
        .where(HitCase.status == "OPEN"))
    oldest = session.scalar(
        select(func.min(HitCase.created_at)).where(HitCase.status == "OPEN"))
    age = None if oldest is None else round(
        (datetime.now(timezone.utc) - oldest).total_seconds(), 1)
    return {"open_cases": int(open_cases), "open_applications": int(open_apps),
            "by_band": by_band, "oldest_open_age_seconds": age}


# --------------------------------------------------------------- turnaround
def turnaround_stats(session: Session) -> dict:
    """Human review time: from the case entering the queue to the decision.
    Auto-cleared cases have no human wait, so they are not part of this number."""
    row = session.execute(text("""
        SELECT count(*) AS decided,
               avg(extract(epoch FROM (decided_at - created_at))) AS avg_s,
               percentile_cont(0.5) WITHIN GROUP
                   (ORDER BY extract(epoch FROM (decided_at - created_at))) AS median_s
        FROM hit_cases
        WHERE status = 'DECIDED'
    """)).one()
    return {"human_review": {"decided_cases": int(row.decided),
                             "avg_seconds": _round(row.avg_s),
                             "median_seconds": _round(row.median_s)}}


# ----------------------------------------------------- payments and sync
def payment_stats(session: Session) -> dict:
    out = {"PENDING": 0, "PAID": 0, "FAILED": 0}
    for status, n in session.execute(
            select(Payment.status, func.count()).group_by(Payment.status)):
        out[status] = int(n)
    return out


def sync_stats(session: Session) -> dict:
    """Is the MongoDB timeline keeping up with PostgreSQL? (pain point #4)"""
    pending = session.scalar(select(func.count()).select_from(OutboxEvent)
                             .where(OutboxEvent.processed_at.is_(None)))
    dead = session.scalar(select(func.count()).select_from(OutboxEvent)
                          .where(OutboxEvent.processed_at.is_(None),
                                 OutboxEvent.attempts >= MAX_ATTEMPTS))
    return {"outbox_pending": int(pending), "dead_letters": int(dead)}


# ----------------------------------------------------------------- bookings
def booking_stats(session: Session) -> dict:
    booked_filter = Appointment.status == "BOOKED"
    booked_total = session.scalar(
        select(func.count()).select_from(Appointment).where(booked_filter))

    # Double-booking check 1: an application with more than one active booking
    multi_sub = (select(Appointment.application_id).where(booked_filter)
                 .group_by(Appointment.application_id)
                 .having(func.count() > 1).subquery())
    multi = session.scalar(select(func.count()).select_from(multi_sub))

    out = {"booked_appointments": int(booked_total),
           "applications_with_multiple_bookings": int(multi),
           "slots": None, "overbooked_slots": None}

    # Double-booking check 2: a slot with more bookings than seats (needs Redis
    # for the capacity). If Redis is down we say "None" instead of guessing.
    try:
        r = get_redis()
        keys = r.zrange(SLOT_INDEX_KEY, 0, -1)
        pipe = r.pipeline()
        for key in keys:
            pipe.hmget(key, "capacity", "available")
        capacity_of, seats, available, full = {}, 0, 0, 0
        for key, (cap, avail) in zip(keys, pipe.execute()):
            if cap is None or avail is None:
                continue
            capacity_of[key] = int(cap)
            seats += int(cap)
            available += int(avail)
            full += int(avail) <= 0

        overbooked = 0
        for site, start, n in session.execute(
                select(Appointment.site, Appointment.slot_start, func.count())
                .where(booked_filter).group_by(Appointment.site, Appointment.slot_start)):
            code = CODE_BY_NAME.get(site)
            if code is None:
                continue
            key = slot_key(code, start.astimezone(PH_TZ))
            if n > capacity_of.get(key, n):       # unknown slot: do not flag
                overbooked += 1
        out["slots"] = {"total": len(keys), "seats_total": seats,
                        "seats_available": available, "full_slots": full}
        out["overbooked_slots"] = overbooked
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ summary
def build_summary(session: Session) -> dict:
    apps = application_stats(session)
    checked = sum(apps["by_status"][s] for s in FINISHED_STATUSES)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "applications": apps,
        "hit": hit_stats(session, checked),
        "queue": queue_stats(session),
        "turnaround": turnaround_stats(session),
        "payments": payment_stats(session),
        "bookings": booking_stats(session),
        "sync": sync_stats(session),
    }