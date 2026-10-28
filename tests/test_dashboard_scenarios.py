"""Runs the dashboard's demo-scenario code in-process (same code as the buttons).
Run first: seed_users, load_data, train_model."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.clients import TIMELINE_COLLECTION, get_mongo_db
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import Applicant, Application, Appointment, HitCase, OutboxEvent, Payment
from dashboard import client as dash_client
from dashboard import demo_tools, scenarios


@pytest.fixture(autouse=True)
def in_process_api(monkeypatch):
    """Send the dashboard's HTTP calls straight into the FastAPI app."""
    monkeypatch.setattr(dash_client, "make_http",
                        lambda: TestClient(fastapi_app, raise_server_exceptions=False))


def clean_up(ref: str) -> None:
    """Delete a scenario's application and everything attached (NOT the audit log)."""
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


def test_demo1_button_50_requests_one_winner():
    try:
        r = scenarios.scenario1_concurrency()
        assert r["requests"] == 50
        assert r["succeeded"] == 1 and r["slot_full"] == 49 and r["other"] == []
        assert r["booked_in_postgres"] == 1          # zero double-bookings
        assert r["seats_left"] == 0
    finally:
        demo_tools.reset_demo_slot()                 # leave the demo slot free again


def test_demo1_can_be_run_twice_in_a_row():
    try:
        for _ in range(2):
            r = scenarios.scenario1_concurrency()
            assert r["succeeded"] == 1 and r["booked_in_postgres"] == 1
    finally:
        demo_tools.reset_demo_slot()


def test_demo2_button_payment_sync_and_duplicate_webhook():
    r = scenarios.scenario2_payment_sync()
    try:
        assert r["webhook"]["result"] == "applied"
        assert r["duplicate"]["result"] == "duplicate"
        assert r["delay_seconds"] is not None and r["delay_seconds"] < 5
        assert r["status"]["payment"]["status"] == "PAID"
        assert "PAYMENT_RECEIVED" in [e["type"] for e in r["status"]["timeline"]]
    finally:
        clean_up(r["application_ref"])


def test_demo3_button_common_name_is_scored_and_never_decided_by_ai():
    r = scenarios.scenario3_common_name()
    try:
        assert r["check"]["candidates_found"] >= 1            # flagged by name
        assert r["outcome"] in ("AUTO_CLEARED", "QUEUED")
        assert r["top_case"]["final_decision"] is None        # the AI never decides
        assert r["top_case"]["explanation"]["features"]
        assert r["public_status"]["view"] == "public"
    finally:
        clean_up(r["application_ref"])


def test_demo4_button_true_match_escalated_then_human_decides():
    r = scenarios.scenario4_true_match()
    try:
        case = r["case"]
        assert case["band"] == "ESCALATE" and case["final_decision"] is None
        assert case["explanation"]["features"]
        res = dash_client.call("POST", f"/review-queue/{case['hit_case_id']}/decision",
                               "reviewer", json_body={"decision": "CONFIRMED_MATCH",
                                                      "note": "synthetic dashboard test"})
        assert res.ok and res.data["application_status"] == "HIT_CONFIRMED"
        # A front desk key can never record a decision
        denied = dash_client.call("POST", f"/review-queue/{case['hit_case_id']}/decision",
                                  "frontdesk", json_body={"decision": "NOT_A_MATCH"})
        assert denied.status == 403
    finally:
        clean_up(r["application_ref"])


def test_demo5_button_masking_and_audit_trail():
    r = scenarios.scenario5_access_control("NBI-000001")
    assert r["frontdesk_view"].ok and r["frontdesk_view"].data["view"] == "masked"
    assert "birthdate" not in str(r["frontdesk_view"].data)
    assert r["reviewer_view"].ok and r["reviewer_view"].data["view"] == "full"
    assert r["frontdesk_queue"].status == 403
    assert r["audit"].ok
    seen = {(i["username"], i["action"], i["outcome"]) for i in r["audit"].data["items"]}
    assert ("frontdesk1", "READ_DETAIL", "ALLOWED") in seen
    assert ("reviewer1", "READ_DETAIL", "ALLOWED") in seen
    assert ("frontdesk1", "READ_QUEUE", "DENIED") in seen


def test_client_reports_an_unreachable_api(monkeypatch):
    monkeypatch.setattr(dash_client, "make_http",
                        lambda: httpx.Client(base_url="http://127.0.0.1:9", timeout=2.0))
    res = dash_client.call("GET", "/health")
    assert res.status == 0 and "uvicorn" in res.error.lower()