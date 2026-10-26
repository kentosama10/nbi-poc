"""NBI PoC API (synthetic data only). Run with:  uvicorn app.main:app --reload

Roles and the audit log arrive in Phase 8. Until then these endpoints are open,
so keep the server on your own PC (Uvicorn listens on 127.0.0.1 by default).
"""
import uuid
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from app.booking import BookingError, book_appointment, list_slots
from app.db import get_session
from app.models import Applicant, Application
from app.outbox import enqueue
from app.schemas import ApplicantIn, BookingIn

app = FastAPI(title="NBI Clearance PoC (synthetic data)", version="0.5.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/applicants", status_code=201)
def create_applicant(body: ApplicantIn, session: Session = Depends(get_session)):
    """Create an applicant and a new application. Returns ONLY the reference
    number, never the personal data (data minimization)."""
    token = uuid.uuid4().hex[:8].upper()
    applicant = Applicant(
        reference_no=f"APP-{token}",
        first_name=body.first_name,
        middle_name=body.middle_name or None,
        last_name=body.last_name,
        birthdate=body.birthdate,
        birthplace=body.birthplace,
        mother_maiden_name=body.mother_maiden_name,
        email=body.email,
    )
    session.add(applicant)
    session.flush()                      # gives us applicant.id (not committed yet)
    application = Application(
        reference_no=f"NBI-{token}", applicant_id=applicant.id,
        purpose=body.purpose, status="SUBMITTED",
    )
    session.add(application)
    session.flush()
    enqueue(session, application.id, "APPLICATION_SUBMITTED",
            "Application received", source="applicant-api")
    session.commit()                     # applicant + application + outbox: all or nothing
    return {"application_ref": application.reference_no, "status": "SUBMITTED"}


@app.get("/slots")
def get_slots(
    site_code: str | None = Query(default=None, description="MNL, QC or CEB"),
    day: date | None = Query(default=None, alias="date", description="YYYY-MM-DD"),
    include_full: bool = False,
    limit: int = Query(default=100, ge=1, le=300),
):
    return list_slots(site_code, day, include_full, limit)


@app.post("/appointments", status_code=201)
def create_appointment(body: BookingIn, session: Session = Depends(get_session)):
    try:
        return book_appointment(session, body.application_ref, body.slot_key)
    except BookingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)