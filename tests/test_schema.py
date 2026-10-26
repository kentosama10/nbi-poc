"""Schema tests: the database must reject data that breaks our rules."""
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import SessionLocal, engine
from app.models import (
    Applicant,
    Application,
    Appointment,
    AuditLog,
    HitCase,
    Payment,
    User,
    WatchlistRecord,
)

EXPECTED_TABLES = {
    "applicants", "applications", "appointments", "audit_log", "hit_cases",
    "outbox_events", "payments", "users", "watchlist_records",
}


@pytest.fixture()
def session():
    """A session that always rolls back, so tests leave the database clean."""
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def uid() -> str:
    return uuid.uuid4().hex[:12]


def make_application(s) -> Application:
    """Helper: insert a synthetic applicant + application and return the application."""
    applicant = Applicant(
        reference_no=f"T-{uid()}", first_name="Test", last_name="Person",
        birthdate=date(1990, 1, 1), birthplace="Test City",
        mother_maiden_name="Test Mother", email="test@example.com",
    )
    s.add(applicant)
    s.flush()  # sends the INSERT now (not committed) so we get applicant.id
    application = Application(
        reference_no=f"T-{uid()}", applicant_id=applicant.id, purpose="EMPLOYMENT"
    )
    s.add(application)
    s.flush()
    return application


def test_all_tables_exist():
    assert EXPECTED_TABLES.issubset(set(inspect(engine).get_table_names()))


def test_invalid_role_rejected(session):
    session.add(User(username=f"u-{uid()}", role="superhero", api_key_hash="a" * 64))
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_booked_appointment_per_application(session):
    application = make_application(session)
    now = datetime.now(timezone.utc)
    session.add(Appointment(application_id=application.id, site="Test", slot_start=now, status="BOOKED"))
    session.flush()
    session.add(Appointment(application_id=application.id, site="Test", slot_start=now, status="BOOKED"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_cancelled_appointment_does_not_block_rebooking(session):
    application = make_application(session)
    now = datetime.now(timezone.utc)
    session.add(Appointment(application_id=application.id, site="Test", slot_start=now, status="CANCELLED"))
    session.add(Appointment(application_id=application.id, site="Test", slot_start=now, status="BOOKED"))
    session.flush()  # must succeed: no exception expected


def test_payment_ref_must_be_unique(session):
    application = make_application(session)
    ref = f"PAY-{uid()}"
    session.add(Payment(application_id=application.id, payment_ref=ref, amount=130))
    session.flush()
    session.add(Payment(application_id=application.id, payment_ref=ref, amount=130))
    with pytest.raises(IntegrityError):
        session.flush()


def test_final_decision_requires_a_human(session):
    """A case cannot be DECIDED unless a user id is recorded (the AI has no user id)."""
    application = make_application(session)
    record = WatchlistRecord(
        record_no=f"T-{uid()}", first_name="Test", last_name="Record",
        birthdate=date(1985, 5, 5), birthplace="Test City",
        mother_maiden_name="Test Mother", case_reference="SYNTH-CASE",
        offense_category="SYNTHETIC",
    )
    session.add(record)
    session.flush()
    session.add(HitCase(
        application_id=application.id, watchlist_record_id=record.id,
        match_probability=0.5, decision_band="REVIEW", explanation={},
        model_version="test", status="DECIDED", final_decision="NOT_A_MATCH",
        decided_by_user_id=None,  # no human recorded -> must be rejected
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_audit_log_is_append_only(session):
    row = AuditLog(
        username="tester", role="admin", action="TEST",
        resource_type="test", outcome="ALLOWED",
    )
    session.add(row)
    session.flush()  # inserting is allowed
    with pytest.raises(DBAPIError):  # deleting must be blocked by the trigger
        session.delete(row)
        session.flush()