"""Governance tests: roles, masking, audit log. Test 1 is DEMO SCENARIO 5.
Run first: seed_users, load_data, train_model."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import config
from app.clients import TIMELINE_COLLECTION, get_mongo_db
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import (
    Applicant, Application, Appointment, AuditLog, HitCase, OutboxEvent, Payment,
    User, WatchlistRecord,
)
from app.payments import sign
from app.views import mask_email, mask_name

APPLICANT = {"X-API-Key": config.API_KEY_APPLICANT}
FRONTDESK = {"X-API-Key": config.API_KEY_FRONTDESK}
REVIEWER = {"X-API-Key": config.API_KEY_REVIEWER}
ADMIN = {"X-API-Key": config.API_KEY_ADMIN}

BASE = {
    "first_name": "Zelda", "middle_name": None, "last_name": "Dummy",
    "birthdate": "1990-01-01", "birthplace": "Test City",
    "mother_maiden_name": "Test Mother", "email": "test@example.com",
    "purpose": "EMPLOYMENT",
}


# ------------------------------------------------------------------ helpers
@pytest.fixture(scope="module", autouse=True)
def require_seeded_users():
    with SessionLocal() as s:
        n = s.scalar(select(func.count()).select_from(User).where(
            User.username.in_(["applicant1", "frontdesk1", "reviewer1", "admin1"])))
    if n < 4:
        pytest.fail("Demo users are missing. Run first: python -m scripts.seed_users")


def audit_count(username=None, action=None, outcome=None, resource_id=None) -> int:
    conditions = []
    if username:
        conditions.append(AuditLog.username == username)
    if action:
        conditions.append(AuditLog.action == action)
    if outcome:
        conditions.append(AuditLog.outcome == outcome)
    if resource_id:
        conditions.append(AuditLog.resource_id == resource_id)
    query = select(func.count()).select_from(AuditLog)
    if conditions:
        query = query.where(*conditions)
    with SessionLocal() as s:
        return s.scalar(query)


def clean_up(ref: str) -> None:
    """Delete an application and everything hanging off it (NOT the audit log)."""
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
def test_scenario5_frontdesk_is_masked_reviewer_sees_all_and_both_are_logged(make_app):
    """DEMO SCENARIO 5: access control + audit trail."""
    client, make = make_app
    ref = make()

    front = client.get(f"/applications/{ref}", headers=FRONTDESK)
    review = client.get(f"/applications/{ref}", headers=REVIEWER)
    assert front.status_code == 200 and review.status_code == 200

    # Front desk: masked name, no birthdate, no evidence of any kind
    masked = front.json()
    assert masked["view"] == "masked"
    assert masked["applicant"]["name"] == "Z*** D***"
    for secret in ("Zelda", "Dummy", "1990-01-01", "Test City", "Test Mother",
                   "test@example.com"):
        assert secret not in front.text
    assert "birthdate" not in front.text and "hit_cases" not in front.text
    assert "watchlist" not in front.text.lower()

    # Reviewer: full details
    full = review.json()
    assert full["view"] == "full"
    assert full["applicant"]["last_name"] == "Dummy"
    assert full["applicant"]["birthdate"] == "1990-01-01"
    assert "hit_cases" in full

    # Front desk cannot reach the evidence endpoints at all
    denied_before = audit_count("frontdesk1", "READ_QUEUE", "DENIED")
    assert client.get("/review-queue", headers=FRONTDESK).status_code == 403
    assert client.get("/hit-cases/1", headers=FRONTDESK).status_code == 403
    assert audit_count("frontdesk1", "READ_QUEUE", "DENIED") == denied_before + 1

    # BOTH successful reads are in the audit log: who, role, action, resource, time
    assert audit_count("frontdesk1", "READ_DETAIL", "ALLOWED", ref) == 1
    assert audit_count("reviewer1", "READ_DETAIL", "ALLOWED", ref) == 1
    with SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(
            AuditLog.username == "frontdesk1", AuditLog.resource_id == ref)).one()
    assert row.role == "frontdesk" and row.resource_type == "application"
    assert row.occurred_at is not None


def test_missing_or_wrong_keys_are_rejected_and_logged():
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    calls = [("GET", "/review-queue"), ("GET", "/slots"),
             ("GET", "/applications/NBI-000001"),
             ("GET", "/applications/NBI-000001/status"),
             ("GET", "/audit-log"),
             ("POST", "/applications/NBI-000001/hit-check")]
    anon_before = audit_count("anonymous", "AUTHENTICATE", "DENIED")
    unknown_before = audit_count("unknown", "AUTHENTICATE", "DENIED")

    for method, url in calls:
        assert client.request(method, url).status_code == 401
        assert client.request(method, url,
                              headers={"X-API-Key": "WrongKey999"}).status_code == 401

    assert audit_count("anonymous", "AUTHENTICATE", "DENIED") == anon_before + len(calls)
    assert audit_count("unknown", "AUTHENTICATE", "DENIED") == unknown_before + len(calls)
    assert client.get("/health").status_code == 200     # health stays open


def test_every_guarded_endpoint_writes_exactly_one_audit_row(make_app):
    """Audit coverage: one call = one row, for every audited endpoint."""
    client, make = make_app

    before = audit_count("applicant1", "CREATE")
    ref = make()                                          # POST /applicants
    assert audit_count("applicant1", "CREATE") == before + 1

    calls = [  # (method, url, headers, username, action, json body)
        ("POST", "/appointments", APPLICANT, "applicant1", "BOOK",
         {"application_ref": ref, "slot_key": "slot:MNL:20000101T0800"}),   # 404 after the gate
        ("POST", f"/applications/{ref}/payments", APPLICANT, "applicant1",
         "REQUEST_PAYMENT", None),
        ("GET", f"/applications/{ref}/status", APPLICANT, "applicant1",
         "READ_STATUS", None),
        ("GET", f"/applications/{ref}", FRONTDESK, "frontdesk1", "READ_DETAIL", None),
        ("POST", f"/applications/{ref}/hit-check", REVIEWER, "reviewer1",
         "HIT_CHECK", None),
        ("GET", "/review-queue", REVIEWER, "reviewer1", "READ_QUEUE", None),
        ("GET", "/hit-cases/999999999", REVIEWER, "reviewer1", "READ_CASE", None),
        ("POST", "/review-queue/999999999/decision", REVIEWER, "reviewer1", "DECIDE",
         {"decision": "NOT_A_MATCH"}),
        ("GET", "/audit-log", ADMIN, "admin1", "READ_AUDIT", None),
    ]
    for method, url, headers, username, action, body in calls:
        before = audit_count(username, action)
        resp = client.request(method, url, headers=headers, json=body)
        assert resp.status_code not in (401, 403), f"{action}: {resp.status_code}"
        assert audit_count(username, action) == before + 1, action


def test_audit_log_endpoint_is_admin_only_and_returns_newest_first():
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    before = audit_count("reviewer1", "READ_AUDIT", "DENIED")
    assert client.get("/audit-log", headers=REVIEWER).status_code == 403
    assert client.get("/audit-log", headers=FRONTDESK).status_code == 403
    assert audit_count("reviewer1", "READ_AUDIT", "DENIED") == before + 1

    resp = client.get("/audit-log", params={"limit": 5}, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["returned"] == 5 and body["total_matching"] >= 5
    ids = [i["id"] for i in body["items"]]
    assert ids == sorted(ids, reverse=True)               # newest first
    assert set(body["items"][0]) == {"id", "occurred_at", "username", "role", "action",
                                     "resource_type", "resource_id", "outcome", "detail"}
    denied = client.get("/audit-log", params={"outcome": "DENIED", "limit": 20},
                        headers=ADMIN).json()
    assert denied["items"] and all(i["outcome"] == "DENIED" for i in denied["items"])


def test_audit_log_contains_no_personal_data_and_no_api_keys(make_app):
    client, make = make_app
    ref = make(first_name="Zzyzx", last_name="Quillfeather")
    client.get(f"/applications/{ref}", headers=REVIEWER)
    client.get(f"/applications/{ref}/status", headers=APPLICANT)
    client.get("/review-queue", params={"application_ref": ref}, headers=REVIEWER)

    with SessionLocal() as s:
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).all()
    blob = json.dumps([[r.username, r.role, r.action, r.resource_type,
                        r.resource_id, r.detail] for r in rows], default=str)
    for secret in ("Zzyzx", "Quillfeather", "1990-01-01", "Test City", "Test Mother",
                   "test@example.com", config.API_KEY_APPLICANT, config.API_KEY_FRONTDESK,
                   config.API_KEY_REVIEWER, config.API_KEY_ADMIN, "WrongKey999"):
        assert secret not in blob


def test_applicants_and_frontdesk_get_a_public_status_staff_get_the_internal_one(make_app):
    client, make = make_app
    with SessionLocal() as s:
        rec = s.scalars(select(WatchlistRecord).order_by(WatchlistRecord.id)).first()
        fields = {"first_name": rec.first_name, "middle_name": rec.middle_name,
                  "last_name": rec.last_name, "birthdate": rec.birthdate.isoformat(),
                  "birthplace": rec.birthplace,
                  "mother_maiden_name": rec.mother_maiden_name}
    ref = make(**fields)                                   # identical to a watchlist person
    resp = client.post(f"/applications/{ref}/hit-check", headers=REVIEWER)
    assert resp.status_code == 200 and resp.json()["status"] == "ESCALATED"

    public = client.get(f"/applications/{ref}/status", headers=APPLICANT).json()
    front = client.get(f"/applications/{ref}/status", headers=FRONTDESK).json()
    staff = client.get(f"/applications/{ref}/status", headers=REVIEWER).json()
    assert public["view"] == "public" and front["view"] == "public"
    assert public["status"] == front["status"] == "VERIFICATION_IN_PROGRESS"
    assert staff["view"] == "internal" and staff["status"] == "ESCALATED"


def test_webhook_is_audited_when_accepted_and_when_rejected(make_app):
    client, make = make_app
    ref = make()
    pay = client.post(f"/applications/{ref}/payments", headers=APPLICANT).json()
    body = {"event_id": "EVT-" + uuid.uuid4().hex[:10], "payment_ref": pay["payment_ref"],
            "status": "PAID", "amount": "130.00"}
    raw = json.dumps(body).encode()
    json_header = {"Content-Type": "application/json"}

    denied_before = audit_count("payment-gateway", "WEBHOOK", "DENIED")
    bad = client.post("/webhooks/payment", content=raw,
                      headers={**json_header, "X-Signature": "0" * 64})
    assert bad.status_code == 401
    assert audit_count("payment-gateway", "WEBHOOK", "DENIED") == denied_before + 1

    good = client.post("/webhooks/payment", content=raw,
                       headers={**json_header, "X-Signature": sign(raw)})
    assert good.status_code == 200
    assert audit_count("payment-gateway", "WEBHOOK", "ALLOWED", pay["payment_ref"]) == 1


def test_masking_helpers():
    assert mask_name("Jose", "Dela Cruz") == "J*** D*** C***"
    assert mask_name("", "") == ""
    assert mask_email("jose.santos@example.com") == "j***@example.com"