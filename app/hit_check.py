"""The HIT pipeline: search -> features -> score -> band -> store -> review queue.

The AI only scores, ranks and explains. A case becomes DECIDED only through
decide_case(), which needs a real user id (a human).
"""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.candidates import search_candidates
from app.models import Applicant, Application, HitCase, WatchlistRecord
from app.outbox import enqueue
from ml.features import compute_features
from ml.model import get_model

# Once an application is in one of these states, its HIT check has been run
FINISHED_STATUSES = ("CLEARED", "UNDER_REVIEW", "ESCALATED", "HIT_CONFIRMED")


class HitCheckError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class DecisionIn(BaseModel):
    decision: Literal["NOT_A_MATCH", "CONFIRMED_MATCH"]
    note: str | None = Field(default=None, max_length=500)


def _person(obj) -> dict:
    """The six fields the feature code needs, from an ORM row."""
    return {"first_name": obj.first_name, "middle_name": obj.middle_name,
            "last_name": obj.last_name, "birthdate": obj.birthdate,
            "birthplace": obj.birthplace,
            "mother_maiden_name": obj.mother_maiden_name}


def recompute_status(session: Session, application_id: int) -> str:
    """Application status implied by its HIT cases."""
    rows = session.execute(
        select(HitCase.status, HitCase.decision_band, HitCase.final_decision)
        .where(HitCase.application_id == application_id)).all()
    if any(r.final_decision == "CONFIRMED_MATCH" for r in rows):
        return "HIT_CONFIRMED"
    open_rows = [r for r in rows if r.status == "OPEN"]
    if any(r.decision_band == "ESCALATE" for r in open_rows):
        return "ESCALATED"
    if open_rows:
        return "UNDER_REVIEW"
    return "CLEARED"


def _summary(session: Session, app_id: int, ref: str, status: str,
             already: bool) -> dict:
    rows = session.execute(
        select(HitCase.id, HitCase.decision_band, HitCase.match_probability)
        .where(HitCase.application_id == app_id)
        .order_by(HitCase.match_probability.desc())).all()
    bands = [r.decision_band for r in rows]
    return {
        "application_ref": ref, "status": status, "already_checked": already,
        "candidates_found": len(rows),
        "auto_cleared": bands.count("AUTO_CLEAR"),
        "needs_review": bands.count("REVIEW"),
        "escalated": bands.count("ESCALATE"),
        "cases": [{"hit_case_id": r.id, "band": r.decision_band,
                   "probability": round(r.match_probability, 4)} for r in rows[:10]],
        "_application_id": app_id,
    }


# ------------------------------------------------------------------ the check
def run_hit_check(session: Session, application_ref: str) -> dict:
    # Lock the application row: two simultaneous checks take turns
    application = session.scalar(select(Application).where(
        Application.reference_no == application_ref).with_for_update())
    if application is None:
        raise HitCheckError(404, "Application not found")
    app_id, ref, current = application.id, application.reference_no, application.status

    if current in FINISHED_STATUSES:                  # idempotent: do not re-run
        session.rollback()
        return _summary(session, app_id, ref, current, already=True)

    try:
        model = get_model()
    except FileNotFoundError as exc:
        session.rollback()
        raise HitCheckError(503, str(exc))

    applicant = session.get(Applicant, application.applicant_id)
    person = _person(applicant)
    try:
        candidates = search_candidates(applicant.first_name, applicant.middle_name,
                                       applicant.last_name)
    except Exception:
        session.rollback()                            # nothing was written
        raise HitCheckError(503, "Candidate search is unavailable. Nothing was changed.")

    ids = [c["pg_id"] for c in candidates]
    records = {}
    if ids:
        records = {r.id: r for r in session.scalars(
            select(WatchlistRecord).where(WatchlistRecord.id.in_(ids)))}

    for c in candidates:
        record = records.get(c["pg_id"])
        if record is None:                            # index and database out of sync
            continue
        explanation = model.explain(compute_features(person, _person(record)))
        band = explanation["band"]
        session.add(HitCase(
            application_id=app_id, watchlist_record_id=record.id,
            es_score=c["es_score"], match_probability=explanation["probability"],
            decision_band=band, explanation=explanation, model_version=model.version,
            status="AUTO_CLEARED" if band == "AUTO_CLEAR" else "OPEN",
        ))                                            # final_decision stays NULL
    session.flush()

    new_status = recompute_status(session, app_id)
    application.status = new_status
    # The applicant sees this timeline, so the wording is deliberately generic.
    enqueue(session, app_id, "VERIFICATION_UPDATE",
            "Verification completed" if new_status == "CLEARED"
            else "Verification in progress", source="hit-check")
    session.commit()          # cases + status + outbox row: ONE transaction
    return _summary(session, app_id, ref, new_status, already=False)


# ------------------------------------------------------------- reading cases
def _case_view(hc: HitCase, application_ref: str, applicant: Applicant,
               record: WatchlistRecord) -> dict:
    """FULL evidence. Only reviewers/admins may receive this (Phase 8 adds masking
    for other roles)."""
    return {
        "hit_case_id": hc.id, "application_ref": application_ref,
        "status": hc.status, "band": hc.decision_band,
        "probability": round(hc.match_probability, 4), "es_score": hc.es_score,
        "model_version": hc.model_version,
        "applicant": {
            "first_name": applicant.first_name, "middle_name": applicant.middle_name,
            "last_name": applicant.last_name,
            "birthdate": applicant.birthdate.isoformat(),
            "birthplace": applicant.birthplace,
            "mother_maiden_name": applicant.mother_maiden_name},
        "watchlist": {
            "record_no": record.record_no, "first_name": record.first_name,
            "middle_name": record.middle_name, "last_name": record.last_name,
            "birthdate": record.birthdate.isoformat(),
            "birthplace": record.birthplace,
            "mother_maiden_name": record.mother_maiden_name,
            "case_reference": record.case_reference,
            "offense_category": record.offense_category},
        "explanation": hc.explanation,
        "final_decision": hc.final_decision,
        "decided_at": hc.decided_at.isoformat() if hc.decided_at else None,
        "decision_note": hc.decision_note,
    }


def _joined():
    return (select(HitCase, Application.reference_no, Applicant, WatchlistRecord)
            .join(Application, Application.id == HitCase.application_id)
            .join(Applicant, Applicant.id == Application.applicant_id)
            .join(WatchlistRecord, WatchlistRecord.id == HitCase.watchlist_record_id))


def list_queue(session: Session, band: str | None, application_ref: str | None,
               limit: int, offset: int) -> dict:
    conditions = [HitCase.status == "OPEN"]
    if band:
        conditions.append(HitCase.decision_band == band)
    if application_ref:
        conditions.append(Application.reference_no == application_ref)
    rows = session.execute(
        _joined().where(*conditions)
        .order_by(case((HitCase.decision_band == "ESCALATE", 0), else_=1),
                  HitCase.match_probability.desc(), HitCase.id)
        .limit(limit).offset(offset)).all()
    counts = dict(session.execute(
        select(HitCase.decision_band, func.count())
        .where(HitCase.status == "OPEN").group_by(HitCase.decision_band)).all())
    return {
        "open_total": sum(counts.values()),
        "by_band": {"ESCALATE": counts.get("ESCALATE", 0),
                    "REVIEW": counts.get("REVIEW", 0)},
        "returned": len(rows),
        "items": [_case_view(*row) for row in rows],
    }


def get_case_view(session: Session, hit_case_id: int) -> dict:
    row = session.execute(_joined().where(HitCase.id == hit_case_id)).first()
    if row is None:
        raise HitCheckError(404, "Case not found")
    return _case_view(*row)


# --------------------------------------------------------- the human decision
def decide_case(session: Session, hit_case_id: int, user_id: int,
                decision: str, note: str | None) -> dict:
    hc = session.scalar(select(HitCase).where(HitCase.id == hit_case_id)
                        .with_for_update())
    if hc is None:
        session.rollback()
        raise HitCheckError(404, "Case not found")
    if hc.status != "OPEN":
        session.rollback()
        raise HitCheckError(409, "Case is not open (already decided or auto-cleared)")
    app_id = hc.application_id

    # Lock the application too, so two reviewers on the same application take turns
    application = session.scalar(select(Application)
                                 .where(Application.id == app_id).with_for_update())

    hc.final_decision = decision
    hc.decided_by_user_id = user_id           # the database requires a human here
    hc.decided_at = datetime.now(timezone.utc)
    hc.decision_note = note
    hc.status = "DECIDED"
    session.flush()

    new_status = recompute_status(session, app_id)
    application.status = new_status
    if new_status in ("CLEARED", "HIT_CONFIRMED"):
        enqueue(session, app_id, "VERIFICATION_COMPLETED",
                "Verification completed", source="review")
    session.commit()
    return {"hit_case_id": hit_case_id, "final_decision": decision,
            "application_status": new_status, "decided_by_user_id": user_id,
            "_application_id": app_id}