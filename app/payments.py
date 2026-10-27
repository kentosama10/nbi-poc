"""Payments: request a payment, verify webhook signatures, apply webhook events.

Write order for a webhook (and why):
  1. Verify the signature (reject forgeries).
  2. Lock the payment row, so two copies of the same event take turns.
  3. In ONE PostgreSQL transaction: update payment + application, and add an
     outbox row. All three succeed together or none do.
  4. AFTER the commit, the caller copies outbox rows to MongoDB (timeline.py).
If step 4 fails, PostgreSQL is still correct and the outbox row is retried.
"""
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.models import Application, Payment
from app.outbox import enqueue

CLEARANCE_FEE = Decimal("130.00")   # synthetic fee in PHP


class PaymentError(Exception):
    """A payment problem the caller should see, with an HTTP status code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class PaymentWebhookIn(BaseModel):
    """What the gateway sends us."""
    event_id: str = Field(min_length=1, max_length=60)     # idempotency key
    payment_ref: str = Field(min_length=1, max_length=40)
    status: Literal["PAID", "FAILED"]
    amount: Decimal


# ---------------------------------------------------------------- signatures
def sign(raw_body: bytes) -> str:
    """HMAC-SHA256 of the exact bytes of the body, as lowercase hex text."""
    return hmac.new(config.WEBHOOK_SECRET.encode(), raw_body,
                    hashlib.sha256).hexdigest()


def verify(raw_body: bytes, signature: str) -> bool:
    """compare_digest takes the same time however many characters match,
    which prevents timing attacks."""
    return hmac.compare_digest(sign(raw_body).encode(), signature.encode())


# ------------------------------------------------------------ request payment
def create_payment(session: Session, application_ref: str) -> dict:
    application = session.scalar(
        select(Application).where(Application.reference_no == application_ref))
    if application is None:
        raise PaymentError(404, "Application not found")
    app_id = application.id

    existing = session.scalar(
        select(Payment).where(Payment.application_id == app_id,
                              Payment.status.in_(["PENDING", "PAID"]))
        .order_by(Payment.id.desc()))
    if existing is not None:
        if existing.status == "PAID":
            raise PaymentError(409, "This application is already paid")
        payment = existing               # reuse the open payment, do not create a second
    else:
        payment = Payment(
            application_id=app_id,
            payment_ref="PAY-" + uuid.uuid4().hex[:12].upper(),
            amount=CLEARANCE_FEE, status="PENDING",
        )
        session.add(payment)
        session.flush()
        enqueue(session, app_id, "PAYMENT_REQUESTED",
                f"Payment of PHP {CLEARANCE_FEE} requested", source="payment-api")
        session.commit()
    return {"payment_ref": payment.payment_ref, "amount": str(payment.amount),
            "currency": "PHP", "status": payment.status}


# ------------------------------------------------------------- apply webhook
def handle_payment_event(session: Session, ev: PaymentWebhookIn) -> dict:
    """Apply one gateway event. Safe to call many times with the same event."""
    # FOR UPDATE: concurrent copies of this event wait here, one at a time
    payment = session.scalar(
        select(Payment).where(Payment.payment_ref == ev.payment_ref).with_for_update())
    if payment is None:
        raise PaymentError(404, "Unknown payment_ref")
    app_id = payment.application_id

    if payment.gateway_event_id == ev.event_id:
        session.rollback()                               # release the lock
        return {"result": "duplicate", "payment_status": payment.status,
                "_application_id": app_id}

    if ev.amount != payment.amount:
        session.rollback()
        raise PaymentError(422, "Amount does not match the payment request")

    if payment.status == "PAID":
        session.rollback()                               # a paid payment never changes
        return {"result": "ignored_already_paid", "payment_status": "PAID",
                "_application_id": app_id}

    payment.status = ev.status
    payment.gateway_event_id = ev.event_id
    if ev.status == "PAID":
        payment.paid_at = datetime.now(timezone.utc)
        application = session.get(Application, app_id)
        if application.status == "SUBMITTED":
            application.status = "PAID"
        enqueue(session, app_id, "PAYMENT_RECEIVED",
                f"Payment of PHP {payment.amount} received", source="payment-webhook")
    else:
        enqueue(session, app_id, "PAYMENT_FAILED",
                "Payment failed. Please try again.", source="payment-webhook")

    try:
        session.commit()      # payment + application + outbox row: ONE transaction
    except IntegrityError:
        session.rollback()    # this event_id was already used for another payment
        raise PaymentError(409, "event_id already used")
    return {"result": "applied", "payment_status": ev.status,
            "_application_id": app_id}