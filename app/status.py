"""Build the status view of one application (PostgreSQL facts + MongoDB timeline).

It returns NO personal data. Role-based masking arrives in Phase 8.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Appointment, Payment
from app.timeline import get_timeline, pending_count


def get_status(session: Session, application_ref: str) -> dict | None:
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
        "status": application.status,
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