"""Payment webhook tests, including DEMO SCENARIO 2: payment sync within seconds."""
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from app import config
from app.clients import TIMELINE_COLLECTION, get_mongo_db
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import Applicant, Application, OutboxEvent, Payment
from app.payments import sign
from app.timeline import deliver_pending

APPLICANT = {
    "first_name": "Test", "middle_name": None, "last_name": "Dummy",
    "birthdate": "1990-01-01", "birthplace": "Test City",
    "mother_maiden_name": "Test Mother", "email": "test@example.com",
    "purpose": "EMPLOYMENT",
}


# ------------------------------------------------------------------ helpers
def make_client() -> TestClient:
    # Phase 8: every endpoint needs an API key. The applicant role may book.
    return TestClient(fastapi_app, raise_server_exceptions=False,
                      headers={"X-API-Key": config.API_KEY_APPLICANT})


def app_id_of(ref: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Application.id).where(Application.reference_no == ref))


def start_payment(client, ref: str) -> dict:
    resp = client.post(f"/applications/{ref}/payments")
    assert resp.status_code == 201, resp.text
    return resp.json()


def webhook_body(payment_ref: str, event_id: str | None = None,
                 status: str = "PAID", amount: str = "130.00") -> dict:
    return {"event_id": event_id or "EVT-" + uuid.uuid4().hex[:10],
            "payment_ref": payment_ref, "status": status, "amount": amount}


def send_webhook(client, body: dict, signature: str | None = None):
    """Send exactly the bytes we sign, like a real gateway would."""
    raw = json.dumps(body).encode()
    headers = {"X-Signature": sign(raw) if signature is None else signature,
               "Content-Type": "application/json"}
    return client.post("/webhooks/payment", content=raw, headers=headers)


def mongo_event_types(app_id: int) -> list[str]:
    doc = get_mongo_db()[TIMELINE_COLLECTION].find_one({"_id": app_id})
    return [e["type"] for e in doc["events"]] if doc else []


def wait_for_event(app_id: int, event_type: str, timeout: float = 5.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if event_type in mongo_event_types(app_id):
            return True
        time.sleep(0.05)
    return False


def clean_up(ref: str) -> None:
    with SessionLocal() as s:
        application = s.scalar(select(Application).where(Application.reference_no == ref))
        if application is None:
            return
        app_id, applicant_id = application.id, application.applicant_id
        s.execute(delete(OutboxEvent).where(OutboxEvent.application_id == app_id))
        s.execute(delete(Payment).where(Payment.application_id == app_id))
        s.execute(delete(Application).where(Application.id == app_id))
        s.execute(delete(Applicant).where(Applicant.id == applicant_id))
        s.commit()
    get_mongo_db()[TIMELINE_COLLECTION].delete_one({"_id": app_id})


@pytest.fixture()
def fresh():
    """A brand-new synthetic application, deleted again after the test."""
    client = make_client()
    resp = client.post("/applicants", json=APPLICANT)
    assert resp.status_code == 201
    ref = resp.json()["application_ref"]
    try:
        yield client, ref
    finally:
        clean_up(ref)


# -------------------------------------------------------------------- tests
def test_payment_webhook_updates_postgres_and_mongodb_within_seconds(fresh):
    client, ref = fresh
    pay = start_payment(client, ref)
    app_id = app_id_of(ref)

    started = time.perf_counter()
    resp = send_webhook(client, webhook_body(pay["payment_ref"]))
    assert resp.status_code == 200 and resp.json()["result"] == "applied"
    assert wait_for_event(app_id, "PAYMENT_RECEIVED", timeout=5.0)
    delay = time.perf_counter() - started
    assert delay < 5.0, f"timeline took {delay:.2f}s"

    status = client.get(f"/applications/{ref}/status").json()
    assert status["status"] == "PAID"                      # PostgreSQL side
    assert status["payment"]["status"] == "PAID"
    assert status["payment"]["paid_at"] is not None
    assert status["pending_sync"] == 0
    assert status["timeline_status"] == "ok"
    assert [e["type"] for e in status["timeline"]] == [    # MongoDB side
        "APPLICATION_SUBMITTED", "PAYMENT_REQUESTED", "PAYMENT_RECEIVED"]


def test_duplicate_webhook_is_applied_only_once(fresh):
    client, ref = fresh
    pay = start_payment(client, ref)
    body = webhook_body(pay["payment_ref"])

    first = send_webhook(client, body).json()
    second = send_webhook(client, body).json()      # the gateway resends the same event
    assert first["result"] == "applied"
    assert second["result"] == "duplicate"

    deliver_pending()
    assert mongo_event_types(app_id_of(ref)).count("PAYMENT_RECEIVED") == 1


def test_bad_or_missing_signature_is_rejected(fresh):
    client, ref = fresh
    pay = start_payment(client, ref)
    body = webhook_body(pay["payment_ref"])

    assert send_webhook(client, body, signature="0" * 64).status_code == 401
    no_header = client.post("/webhooks/payment", content=json.dumps(body).encode(),
                            headers={"Content-Type": "application/json"})
    assert no_header.status_code == 401

    status = client.get(f"/applications/{ref}/status").json()
    assert status["payment"]["status"] == "PENDING"        # nothing changed


def test_wrong_amount_is_rejected(fresh):
    client, ref = fresh
    pay = start_payment(client, ref)
    resp = send_webhook(client, webhook_body(pay["payment_ref"], amount="1.00"))
    assert resp.status_code == 422
    assert client.get(f"/applications/{ref}/status").json()["payment"]["status"] == "PENDING"


def test_unknown_payment_reference_returns_404():
    client = make_client()
    resp = send_webhook(client, webhook_body("PAY-DOES-NOT-EXIST"))
    assert resp.status_code == 404


def test_10_simultaneous_copies_of_one_event_apply_exactly_once(fresh):
    client, ref = fresh
    pay = start_payment(client, ref)
    body = webhook_body(pay["payment_ref"])

    barrier = threading.Barrier(10)
    lock = threading.Lock()
    results: list = []

    def worker() -> None:
        c = make_client()
        try:
            barrier.wait(timeout=30)
            resp = send_webhook(c, body)
            outcome = resp.json().get("result") if resp.status_code == 200 else resp.status_code
        except Exception:
            outcome = "error"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("applied") == 1, results
    assert results.count("duplicate") == 9, results
    deliver_pending()
    assert mongo_event_types(app_id_of(ref)).count("PAYMENT_RECEIVED") == 1


def test_mongodb_outage_keeps_event_in_outbox_and_retry_delivers_it(fresh, monkeypatch):
    client, ref = fresh
    pay = start_payment(client, ref)
    app_id = app_id_of(ref)

    def mongo_down(*args, **kwargs):
        raise ConnectionError("simulated MongoDB outage")

    monkeypatch.setattr("app.timeline._push_to_mongo", mongo_down)
    resp = send_webhook(client, webhook_body(pay["payment_ref"]))
    assert resp.status_code == 200                         # PostgreSQL still succeeded
    assert resp.json()["result"] == "applied"
    assert resp.json()["timeline_synced"] is False         # honest about the lag

    status = client.get(f"/applications/{ref}/status").json()
    assert status["payment"]["status"] == "PAID"           # the truth is already correct
    assert status["pending_sync"] >= 1                     # visible to staff
    assert "PAYMENT_RECEIVED" not in mongo_event_types(app_id)

    monkeypatch.undo()                                     # "MongoDB is back"
    result = deliver_pending()
    assert result["delivered"] >= 1 and result["failed"] == 0
    assert "PAYMENT_RECEIVED" in mongo_event_types(app_id)
    assert client.get(f"/applications/{ref}/status").json()["pending_sync"] == 0


def test_payment_request_is_reused_and_unknown_application_returns_404(fresh):
    client, ref = fresh
    first = start_payment(client, ref)
    second = start_payment(client, ref)
    assert first["payment_ref"] == second["payment_ref"]   # no duplicate pending payment
    assert first["amount"] == "130.00"

    assert client.post("/applications/NBI-NOPE/payments").status_code == 404
    assert client.get("/applications/NBI-NOPE/status").status_code == 404