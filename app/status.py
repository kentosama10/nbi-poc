"""Build the status view of one application (PostgreSQL facts + MongoDB timeline).

It returns NO personal data. Applicants and front desk get a PUBLIC status;
reviewers and admins get the INTERNAL one.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Appointment, Payment
from app.timeline import get_timeline, pending_count

# Internal status -> what an applicant or front desk may see.
# DESIGN CHOICE: a confirmed HIT shows as "VERIFICATION_COMPLETED", the same as a
# cleared case, so the system never tips anyone off. Staff follow up outside this PoC.
PUBLIC_STATUS = {
    "SUBMITTED": "SUBMITTED",
    "PAID": "PAID",
    "SCHEDULED": "SCHEDULED",
    "UNDER_REVIEW": "VERIFICATION_IN_PROGRESS",
    "ESCALATED": "VERIFICATION_IN_PROGRESS",
    "CLEARED": "VERIFICATION_COMPLETED",
    "HIT_CONFIRMED": "VERIFICATION_COMPLETED",
}


def public_status(internal: str) -> str:
    return PUBLIC_STATUS.get(internal, "IN_PROGRESS")


def get_status(session: Session, application_ref: str,
               internal: bool = False) -> dict | None:
    application = session.scalar(
        select(Application).where(Application.reference_no == application_ref))
    if application is None:
        return None
    app_id = application.id

    payment = session.scalar(select(Payment).where(Payment.application_id == app_id)
                             .order_by(Payment.id.desc()))
    appointment = session.scalar(
        select(Appointment).where(Appointment.application_id == app_id,
                                  Appointment.status == "BOOKED"))

    # The timeline comes from MongoDB. If MongoDB is down we still answer from
    # PostgreSQL and say honestly that the timeline is unavailable.
    try:
        timeline, timeline_status = get_timeline(app_id), "ok"
    except Exception:
        timeline, timeline_status = [], "unavailable"

    return {
        "application_ref": application.reference_no,
        "view": "internal" if internal else "public",
        "status": application.status if internal else public_status(application.status),
        "payment": None if payment is None else {
            "payment_ref": payment.payment_ref,
            "status": payment.status,
            "amount": str(payment.amount),
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        },
        "appointment": None if appointment is None else {
            "site": appointment.site,
            "slot_start": appointment.slot_start.isoformat(),
        },
        "pending_sync": pending_count(session, app_id),   # events not yet in MongoDB
        "timeline_status": timeline_status,
        "timeline": timeline,
    }