"""PostgreSQL tables (the system of record) for the NBI PoC.

All data is SYNTHETIC. The hidden ground-truth label is NOT stored here; it
lives only in the CSV files and is used by the evaluation script (Phase 11).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Applicant(Base):
    """A person applying for clearance (synthetic). Sensitive personal data."""

    __tablename__ = "applicants"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable ID that also appears in the CSV files (used to join ground truth later)
    reference_no: Mapped[str] = mapped_column(String(20), unique=True)
    first_name: Mapped[str] = mapped_column(String(80))
    middle_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), index=True)
    birthdate: Mapped[date] = mapped_column(Date)
    birthplace: Mapped[str] = mapped_column(String(120))
    mother_maiden_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WatchlistRecord(Base):
    """A synthetic watchlist entry that applicants are compared against."""

    __tablename__ = "watchlist_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_no: Mapped[str] = mapped_column(String(20), unique=True)
    first_name: Mapped[str] = mapped_column(String(80))
    middle_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), index=True)
    birthdate: Mapped[date] = mapped_column(Date)
    birthplace: Mapped[str] = mapped_column(String(120))
    mother_maiden_name: Mapped[str] = mapped_column(String(120))
    case_reference: Mapped[str] = mapped_column(String(40))      # fake case number
    offense_category: Mapped[str] = mapped_column(String(60))    # generic synthetic label
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Application(Base):
    """One clearance request. Links applicant, payment, appointment, HIT check."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference_no: Mapped[str] = mapped_column(String(30), unique=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicants.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(40))  # e.g. EMPLOYMENT, TRAVEL
    status: Mapped[str] = mapped_column(String(20), server_default="SUBMITTED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('SUBMITTED','PAID','SCHEDULED','UNDER_REVIEW',"
            "'ESCALATED','CLEARED','HIT_CONFIRMED')",
            name="ck_applications_status",
        ),
    )


class Payment(Base):
    """Payment for an application. Updated by the payment webhook (Phase 6)."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), index=True
    )
    # Our own reference, sent to the (simulated) gateway and echoed back in the webhook
    payment_ref: Mapped[str] = mapped_column(String(40), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), server_default="PHP")
    status: Mapped[str] = mapped_column(String(10), server_default="PENDING")
    # ID of the last webhook event applied. UNIQUE, so the same event can never
    # be applied twice (idempotency). NULL values are allowed to repeat.
    gateway_event_id: Mapped[str | None] = mapped_column(String(60), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PAID','FAILED')", name="ck_payments_status"
        ),
    )


class Appointment(Base):
    """A booked slot. Redis does the fast atomic counting; this is the permanent record."""

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    site: Mapped[str] = mapped_column(String(60))
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), server_default="BOOKED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('BOOKED','CANCELLED')", name="ck_appointments_status"
        ),
        # Safety net: an application may have only ONE active (BOOKED) appointment.
        # Cancelled ones do not count, so people can rebook.
        Index(
            "uq_one_booked_per_application",
            "application_id",
            unique=True,
            postgresql_where=text("status = 'BOOKED'"),
        ),
        # Fast lookup of "how many bookings in this slot?" (used by reconciliation)
        Index("ix_appointments_site_slot", "site", "slot_start"),
    )


class User(Base):
    """Staff/applicant accounts. Identified by an API key; we store only its hash."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    # SHA-256 hash of the API key. If this table leaks, the keys are not exposed.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('applicant','frontdesk','reviewer','admin')",
            name="ck_users_role",
        ),
    )


class HitCase(Base):
    """One applicant-vs-watchlist candidate pair that the AI scored (Phase 7).

    The AI fills the score, band, and explanation. ONLY a human fills
    final_decision (enforced by a database rule below).
    """

    __tablename__ = "hit_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), index=True
    )
    watchlist_record_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_records.id")
    )
    es_score: Mapped[float | None]                      # Elasticsearch relevance score
    match_probability: Mapped[float]                    # logistic regression output, 0 to 1
    decision_band: Mapped[str] = mapped_column(String(10))   # AUTO_CLEAR / REVIEW / ESCALATE
    explanation: Mapped[dict] = mapped_column(JSONB)    # feature values and contributions
    model_version: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(15), server_default="OPEN")
    final_decision: Mapped[str | None] = mapped_column(String(20))   # human only
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # One row per application + candidate pair
        UniqueConstraint(
            "application_id", "watchlist_record_id", name="uq_hitcase_pair"
        ),
        CheckConstraint(
            "decision_band IN ('AUTO_CLEAR','REVIEW','ESCALATE')",
            name="ck_hitcases_band",
        ),
        CheckConstraint(
            "status IN ('OPEN','AUTO_CLEARED','DECIDED')", name="ck_hitcases_status"
        ),
        CheckConstraint(
            "final_decision IS NULL OR final_decision IN "
            "('NOT_A_MATCH','CONFIRMED_MATCH')",
            name="ck_hitcases_final_decision",
        ),
        # HUMAN-IN-THE-LOOP RULE: a case is DECIDED if and only if a final decision
        # AND the deciding user are both recorded. The AI cannot set these.
        CheckConstraint(
            "(status = 'DECIDED') = "
            "(final_decision IS NOT NULL AND decided_by_user_id IS NOT NULL)",
            name="ck_hitcases_human_decides",
        ),
    )


class AuditLog(Base):
    """Who did what to which resource, and when. Append-only (see init_db.py)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Plain text (not a foreign key) so even unknown or denied callers can be logged
    username: Mapped[str] = mapped_column(String(50))
    role: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(40))          # e.g. READ, CREATE, DECIDE
    resource_type: Mapped[str] = mapped_column(String(40))   # e.g. hit_case
    resource_id: Mapped[str | None] = mapped_column(String(60))
    outcome: Mapped[str] = mapped_column(String(10))         # ALLOWED or DENIED
    # Extra context. NEVER put personal data (names, birthdates) in here.
    detail: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("outcome IN ('ALLOWED','DENIED')", name="ck_audit_outcome"),
    )


class OutboxEvent(Base):
    """Outbox pattern (explained in Phase 6): an event saved in the SAME
    transaction as the business change, then delivered to MongoDB afterwards.
    If delivery fails, the row stays here and is retried, so nothing is lost."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        # Small, fast index containing only events not yet delivered
        Index(
            "ix_outbox_unprocessed",
            "id",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )