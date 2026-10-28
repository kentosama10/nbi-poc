"""Analytics tests. Run first: seed_users, load_data, train_model."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app import config
from app.clients import TIMELINE_COLLECTION, get_mongo_db
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import (
    Applicant, Application, Appointment, AuditLog, HitCase, OutboxEvent, Payment,
    User, WatchlistRecord,
)

APPLICANT = {"X-API-Key": config.API_KEY_APPLICANT}
FRONTDESK = {"X-API-Key": config.API_KEY_FRONTDESK}
REVIEWER = {"X-API-Key": config.API_KEY_REVIEWER}
ADMIN = {"X-API-Key": config.API_KEY_ADMIN}

BASE = {
    "first_name": "Test", "middle_name": None, "last_name": "Dummy",
    "birthdate": "1990-01-01", "birthplace": "Test City",
    "mother_maiden_name": "Test Mother", "email": "test@example.com",
    "purpose": "EMPLOYMENT",
}
URL = "/analytics/summary"
STATUSES = {"SUBMITTED", "PAID", "SCHEDULED", "UNDER_REVIEW", "ESCALATED",
            "CLEARED", "HIT_CONFIRMED"}


# ------------------------------------------------------------------ helpers
@pytest.fixture(scope="module", autouse=True)
def require_seeded_users():
    with SessionLocal() as s:
        n = s.scalar(select(func.count()).select_from(User).where(
            User.username.in_(["applicant1", "frontdesk1", "reviewer1", "admin1"])))
    if n < 4:
        pytest.fail("Demo users are missing. Run first: python -m scripts.seed_users")


def summary(client) -> dict:
    resp = client.get(URL, headers=REVIEWER)
    assert resp.status_code == 200, resp.text
    return resp.json()


def audit_count(username: str, action: str, outcome: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.username == username, AuditLog.action == action,
            AuditLog.outcome == outcome))


def app_id_of(ref: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Application.id).where(Application.reference_no == ref))


def common_name_record() -> dict:
    """A watchlist record with the most common surname in the watchlist."""
    with SessionLocal() as s:
        surname = s.execute(
            select(WatchlistRecord.last_name, func.count().label("n"))
            .group_by(WatchlistRecord.last_name)
            .order_by(func.count().desc())).first()[0]
        r = s.scalars(select(WatchlistRecord).where(WatchlistRecord.last_name == surname)
                      .order_by(WatchlistRecord.id)).first()
        return {"first_name": r.first_name, "last_name": r.last_name,
                "birthdate": r.birthdate}


def first_record_fields() -> dict:
    """The fields of the first watchlist record (to create an identical applicant)."""
    with SessionLocal() as s:
        r = s.scalars(select(WatchlistRecord).order_by(WatchlistRecord.id)).first()
        return {"first_name": r.first_name, "middle_name": r.middle_name,
                "last_name": r.last_name, "birthdate": r.birthdate.isoformat(),
                "birthplace": r.birthplace,
                "mother_maiden_name": r.mother_maiden_name}


def clean_up(ref: str) -> None:
    """Delete an application and everything attached (NOT the audit log)."""
    with SessionLocal() as s:
        application = s.scalar(select(Application).where(Application.reference_no == ref))
        if application is None:
            return
        app_id, applicant_id = application.id, application.applicant_id
        for model in (HitCase, Payment, Appointment, OutboxEvent):
            s.execute(delete(model).where(model.application_id == app_id))
        s.execute(delete(Application).where(Application.id == app_id))
        s.execute(delete(Applicant).where(Applicant.id == applicant_id))
        s.commit()
    get_mongo_db()[TIMELINE_COLLECTION].delete_one({"_id": app_id})


@pytest.fixture()
def make_app():
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    created: list[str] = []

    def make(**fields) -> str:
        resp = client.post("/applicants", json={**BASE, **fields}, headers=APPLICANT)
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["application_ref"])
        return created[-1]

    yield client, make
    for ref in created:
        clean_up(ref)


# -------------------------------------------------------------------- tests
def test_analytics_is_staff_only_and_audited():
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    denied_before = audit_count("frontdesk1", "READ_ANALYTICS", "DENIED")
    allowed_before = audit_count("reviewer1", "READ_ANALYTICS", "ALLOWED")
    admin_before = audit_count("admin1", "READ_ANALYTICS", "ALLOWED")

    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=APPLICANT).status_code == 403
    assert client.get(URL, headers=FRONTDESK).status_code == 403
    assert client.get(URL, headers=REVIEWER).status_code == 200
    assert client.get(URL, headers=ADMIN).status_code == 200

    assert audit_count("frontdesk1", "READ_ANALYTICS", "DENIED") == denied_before + 1
    assert audit_count("reviewer1", "READ_ANALYTICS", "ALLOWED") == allowed_before + 1
    assert audit_count("admin1", "READ_ANALYTICS", "ALLOWED") == admin_before + 1


def test_summary_is_internally_consistent_and_has_no_personal_data():
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    resp = client.get(URL, headers=REVIEWER)
    assert resp.status_code == 200
    s = resp.json()
    apps, hit, queue = s["applications"], s["hit"], s["queue"]

    assert set(apps["by_status"]) == STATUSES
    assert apps["total"] == sum(apps["by_status"].values())
    assert hit["flagged_by_name"] <= hit["checked_applications"]
    assert hit["manual_reviews_with_ai"] <= hit["flagged_by_name"]
    assert hit["manual_reviews_baseline"] == hit["flagged_by_name"]
    assert hit["manual_reviews_avoided"] == (
        hit["flagged_by_name"] - hit["manual_reviews_with_ai"])
    pairs = hit["pairs"]
    assert pairs["total"] == pairs["auto_clear"] + pairs["review"] + pairs["escalate"]
    assert queue["open_cases"] == queue["by_band"]["ESCALATE"] + queue["by_band"]["REVIEW"]
    assert queue["open_applications"] <= queue["open_cases"]
    for rate in (hit["hit_rate"], hit["auto_clear_rate"], hit["auto_clear_rate_pairs"]):
        assert rate is None or 0 <= rate <= 1

    # Double-booking check (needed for the Phase 11 report): must be zero
    assert s["bookings"]["applications_with_multiple_bookings"] == 0
    assert s["bookings"]["overbooked_slots"] in (0, None)

    # Aggregates only
    for word in ("first_name", "last_name", "birthdate", "mother_maiden", "watchlist"):
        assert word not in resp.text


def test_summary_matches_independent_sql():
    """Recompute the headline numbers with different SQL and compare."""
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    s = summary(client)
    with SessionLocal() as db:
        def one(sql: str) -> int:
            return int(db.scalar(text(sql)))
        total = one("SELECT count(*) FROM applications")
        flagged = one("SELECT count(DISTINCT application_id) FROM hit_cases")
        need = one("SELECT count(DISTINCT application_id) FROM hit_cases "
                   "WHERE decision_band <> 'AUTO_CLEAR'")
        open_cases = one("SELECT count(*) FROM hit_cases WHERE status = 'OPEN'")
        decided = one("SELECT count(*) FROM hit_cases WHERE status = 'DECIDED'")
        paid = one("SELECT count(*) FROM payments WHERE status = 'PAID'")
        booked = one("SELECT count(*) FROM appointments WHERE status = 'BOOKED'")
    assert s["applications"]["total"] == total
    assert s["hit"]["flagged_by_name"] == flagged
    assert s["hit"]["manual_reviews_with_ai"] == need
    assert s["queue"]["open_cases"] == open_cases
    assert s["turnaround"]["human_review"]["decided_cases"] == decided
    assert s["payments"]["PAID"] == paid
    assert s["bookings"]["booked_appointments"] == booked


def test_auto_cleared_namesake_raises_manual_reviews_avoided(make_app):
    client, make = make_app
    rec = common_name_record()
    other_date = date(1999, 12, 31) if rec["birthdate"].year != 1999 else date(1980, 6, 15)
    ref = make(first_name=rec["first_name"], last_name=rec["last_name"],
               birthdate=other_date.isoformat(), birthplace="Unlisted Town",
               mother_maiden_name="Maria Unrelated")
    before = summary(client)

    body = client.post(f"/applications/{ref}/hit-check", headers=REVIEWER).json()
    assert body["candidates_found"] >= 1                  # flagged by name
    after = summary(client)

    b, a = before["hit"], after["hit"]
    needs_review = (body["needs_review"] + body["escalated"]) > 0
    assert a["checked_applications"] == b["checked_applications"] + 1
    assert a["flagged_by_name"] == b["flagged_by_name"] + 1
    assert a["manual_reviews_baseline"] == b["manual_reviews_baseline"] + 1
    assert a["manual_reviews_with_ai"] == b["manual_reviews_with_ai"] + (1 if needs_review else 0)
    assert a["manual_reviews_avoided"] == b["manual_reviews_avoided"] + (0 if needs_review else 1)
    assert a["pairs"]["total"] == b["pairs"]["total"] + body["candidates_found"]


def test_escalated_case_enters_queue_and_a_human_decision_updates_turnaround(make_app):
    client, make = make_app
    ref = make(**first_record_fields())                    # identical to a watchlist person
    before = summary(client)

    body = client.post(f"/applications/{ref}/hit-check", headers=REVIEWER).json()
    assert body["escalated"] >= 1
    mid = summary(client)
    assert mid["queue"]["by_band"]["ESCALATE"] >= before["queue"]["by_band"]["ESCALATE"] + 1
    assert mid["queue"]["open_cases"] >= before["queue"]["open_cases"] + 1
    assert mid["queue"]["oldest_open_age_seconds"] is not None
    assert mid["applications"]["by_status"]["ESCALATED"] == (
        before["applications"]["by_status"]["ESCALATED"] + 1)

    with SessionLocal() as s:
        case_id = s.scalar(select(HitCase.id).where(
            HitCase.application_id == app_id_of(ref), HitCase.decision_band == "ESCALATE")
            .order_by(HitCase.match_probability.desc()))
    resp = client.post(f"/review-queue/{case_id}/decision", headers=REVIEWER,
                       json={"decision": "CONFIRMED_MATCH", "note": "synthetic analytics test"})
    assert resp.status_code == 200, resp.text

    after = summary(client)
    h_before = before["turnaround"]["human_review"]
    h_after = after["turnaround"]["human_review"]
    assert h_after["decided_cases"] == h_before["decided_cases"] + 1
    assert h_after["avg_seconds"] is not None and h_after["avg_seconds"] >= 0
    assert h_after["median_seconds"] is not None
    assert after["queue"]["by_band"]["ESCALATE"] <= mid["queue"]["by_band"]["ESCALATE"] - 1
    assert after["applications"]["by_status"]["HIT_CONFIRMED"] == (
        before["applications"]["by_status"]["HIT_CONFIRMED"] + 1)