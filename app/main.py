"""NBI PoC API (synthetic data only). Run with:  uvicorn app.main:app --reload

Roles and the audit log arrive in Phase 8. Until then these endpoints are open,
so keep the server on your own PC (Uvicorn listens on 127.0.0.1 by default).
"""
import uuid
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.booking import BookingError, book_appointment, list_slots
from app.db import get_session
from app.models import Applicant, Application
from app.outbox import enqueue
from app.payments import (
    PaymentError, PaymentWebhookIn, create_payment, handle_payment_event, verify,
)
from app.schemas import ApplicantIn, BookingIn
from app.status import get_status
from app.timeline import deliver_pending, pending_count

app = FastAPI(title="NBI Clearance PoC (synthetic data)", version="0.6.0")


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


# ------------------------------------------------------------ Phase 6: payments
@app.post("/applications/{application_ref}/payments", status_code=201)
def request_payment(application_ref: str, session: Session = Depends(get_session)):
    """Ask for a payment reference (the simulated gateway echoes it in the webhook)."""
    try:
        return create_payment(session, application_ref)
    except PaymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


def _sync_timeline(session: Session, application_id: int) -> bool:
    """After a commit: try to copy outbox rows to MongoDB. Never raises.
    Returns True if this application's timeline is fully up to date."""
    try:
        deliver_pending(limit=50)
    except Exception:
        pass            # the outbox keeps the events; the worker will retry
    return pending_count(session, application_id) == 0


@app.post("/webhooks/payment")
async def payment_webhook(
    request: Request,
    x_signature: str | None = Header(default=None),   # header "X-Signature"
    session: Session = Depends(get_session),
):
    """Receive a payment event from the (simulated) gateway.
    We read the RAW bytes first, because the signature covers the exact bytes."""
    raw = await request.body()
    if not x_signature or not verify(raw, x_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        event = PaymentWebhookIn.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid payload")

    try:
        result = await run_in_threadpool(handle_payment_event, session, event)
    except PaymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    app_id = result.pop("_application_id")
    result["timeline_synced"] = await run_in_threadpool(_sync_timeline, session, app_id)
    return result       # 200 even if MongoDB lagged: PostgreSQL has the payment


@app.get("/applications/{application_ref}/status")
def application_status(application_ref: str, session: Session = Depends(get_session)):
    status = get_status(session, application_ref)
    if status is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return status