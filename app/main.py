"""NBI PoC API (synthetic data only). Run with:  uvicorn app.main:app --reload

Phase 8: every endpoint except /health and the signed payment webhook requires
an X-API-Key. Sensitive endpoints write an audit row at the gate.
"""
import uuid
from datetime import date
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.audit import list_audit, log_event
from app.analytics import build_summary
from app.auth import get_current_user, guard
from app.booking import BookingError, book_appointment, list_slots
from app.db import get_session
from app.hit_check import (
    DecisionIn, HitCheckError, decide_case, get_case_view, list_queue, run_hit_check,
)
from app.models import Applicant, Application, User
from app.outbox import enqueue
from app.payments import (
    PaymentError, PaymentWebhookIn, create_payment, handle_payment_event, verify,
)
from app.schemas import ApplicantIn, BookingIn
from app.status import get_status
from app.timeline import deliver_pending, pending_count
from app.views import get_application_view

app = FastAPI(title="NBI Clearance PoC (synthetic data)", version="0.8.0")

# Who may do what (least privilege)
ALL_ROLES = ("applicant", "frontdesk", "reviewer", "admin")
CUSTOMER_ROLES = ("applicant", "frontdesk", "admin")   # create, book, pay
STAFF_ROLES = ("reviewer", "admin")                    # evidence and decisions
DETAIL_ROLES = ("frontdesk", "reviewer", "admin")      # application detail (masked or full)
ADMIN_ONLY = ("admin",)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/applicants", status_code=201)
def create_applicant(
    body: ApplicantIn,
    session: Session = Depends(get_session),
    user: User = Depends(guard("CREATE", "applicant", CUSTOMER_ROLES)),
):
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
    user: User = Depends(get_current_user),       # any valid key; not audited (not sensitive)
):
    return list_slots(site_code, day, include_full, limit)


@app.post("/appointments", status_code=201)
def create_appointment(
    body: BookingIn,
    session: Session = Depends(get_session),
    user: User = Depends(guard("BOOK", "appointment", CUSTOMER_ROLES)),
):
    try:
        return book_appointment(session, body.application_ref, body.slot_key)
    except BookingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ------------------------------------------------------------ Phase 6: payments
@app.post("/applications/{application_ref}/payments", status_code=201)
def request_payment(
    application_ref: str,
    session: Session = Depends(get_session),
    user: User = Depends(guard("REQUEST_PAYMENT", "application", CUSTOMER_ROLES,
                               id_param="application_ref")),
):
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
    Authenticated by HMAC signature, not by API key. Audited as 'payment-gateway'."""
    raw = await request.body()
    if not x_signature or not verify(raw, x_signature):
        await run_in_threadpool(
            log_event, "payment-gateway", "system", "WEBHOOK", "payment",
            outcome="DENIED", detail={"reason": "bad_signature"})
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        event = PaymentWebhookIn.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid payload")

    # Fail closed: record the (verified) event before applying it
    await run_in_threadpool(
        log_event, "payment-gateway", "system", "WEBHOOK", "payment",
        event.payment_ref, "ALLOWED", {"event_id": event.event_id})

    try:
        result = await run_in_threadpool(handle_payment_event, session, event)
    except PaymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    app_id = result.pop("_application_id")
    result["timeline_synced"] = await run_in_threadpool(_sync_timeline, session, app_id)
    return result       # 200 even if MongoDB lagged: PostgreSQL has the payment


@app.get("/applications/{application_ref}/status")
def application_status(
    application_ref: str,
    session: Session = Depends(get_session),
    user: User = Depends(guard("READ_STATUS", "application", ALL_ROLES,
                               id_param="application_ref")),
):
    """Staff see the internal status; applicants and front desk see a public one."""
    status = get_status(session, application_ref, internal=user.role in STAFF_ROLES)
    if status is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return status


# ------------------------------------------------- Phase 8: role-based detail view
@app.get("/applications/{application_ref}")
def application_detail(
    application_ref: str,
    session: Session = Depends(get_session),
    user: User = Depends(guard("READ_DETAIL", "application", DETAIL_ROLES,
                               id_param="application_ref")),
):
    """Front desk gets a MASKED view; reviewers and admins get the FULL view."""
    view = get_application_view(session, application_ref, user.role)
    if view is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return view


# ---------------------------------------------------------- Phase 7: HIT matching
@app.post("/applications/{application_ref}/hit-check")
def hit_check(
    application_ref: str,
    session: Session = Depends(get_session),
    user: User = Depends(guard("HIT_CHECK", "application", STAFF_ROLES,
                               id_param="application_ref")),
):
    """Run the matching pipeline. The AI scores and explains; it never decides."""
    try:
        result = run_hit_check(session, application_ref)
    except HitCheckError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    _sync_timeline(session, result.pop("_application_id"))
    return result


@app.get("/review-queue")
def review_queue(
    band: Literal["REVIEW", "ESCALATE"] | None = None,
    application_ref: str | None = None,
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: User = Depends(guard("READ_QUEUE", "hit_case", STAFF_ROLES)),
):
    """Open cases, escalated first, then by probability. Full evidence."""
    return list_queue(session, band, application_ref, limit, offset)


@app.get("/hit-cases/{hit_case_id}")
def hit_case_detail(
    hit_case_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(guard("READ_CASE", "hit_case", STAFF_ROLES,
                               id_param="hit_case_id")),
):
    """One case with its explanation (including auto-cleared ones, for audit)."""
    try:
        return get_case_view(session, hit_case_id)
    except HitCheckError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/review-queue/{hit_case_id}/decision")
def review_decision(
    hit_case_id: int,
    body: DecisionIn,
    session: Session = Depends(get_session),
    user: User = Depends(guard("DECIDE", "hit_case", STAFF_ROLES,
                               id_param="hit_case_id")),
):
    """The human final decision. The caller's user id is recorded with it."""
    try:
        result = decide_case(session, hit_case_id, user.id, body.decision, body.note)
    except HitCheckError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    _sync_timeline(session, result.pop("_application_id"))
    return result


# ------------------------------------------------------------ Phase 8: audit log
@app.get("/audit-log")
def audit_log(
    limit: int = Query(default=25, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=40),
    outcome: Literal["ALLOWED", "DENIED"] | None = None,
    username: str | None = Query(default=None, max_length=50),
    session: Session = Depends(get_session),
    user: User = Depends(guard("READ_AUDIT", "audit_log", ADMIN_ONLY)),
):
    """Admin only. Reading the audit log is itself audited."""
    return list_audit(session, limit, offset, action, outcome, username)

# ------------------------------------------------------- Phase 9: analytics
@app.get("/analytics/summary")
def analytics_summary(
    session: Session = Depends(get_session),
    user: User = Depends(guard("READ_ANALYTICS", "analytics", STAFF_ROLES)),
):
    """Aggregate operational numbers. Reviewer and admin only; every call is audited."""
    return build_summary(session)