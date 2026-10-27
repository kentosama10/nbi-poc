"""Booking tests, including DEMO SCENARIO 1: 50 simultaneous requests, 1 seat."""
import threading

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from app import config
from app.booking import reconcile_slots
from app.clients import get_redis
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import Applicant, Application, Appointment, OutboxEvent
from app.slots import DEMO_POINTER_KEY


# ------------------------------------------------------------------ helpers
def make_client() -> TestClient:
    # Phase 8: every endpoint needs an API key. The applicant role may book.
    return TestClient(fastapi_app, raise_server_exceptions=False,
                      headers={"X-API-Key": config.API_KEY_APPLICANT})


def free_application_refs(n: int) -> list[str]:
    """n applications (highest ids first) that have no BOOKED appointment."""
    booked_ids = select(Appointment.application_id).where(Appointment.status == "BOOKED")
    with SessionLocal() as s:
        return list(s.scalars(
            select(Application.reference_no)
            .where(~Application.id.in_(booked_ids))
            .order_by(Application.id.desc()).limit(n)))


def clean_up(refs: list[str]) -> None:
    """Delete the appointments and outbox rows a test created, then rebuild the
    Redis counters from PostgreSQL (this also exercises reconcile_slots)."""
    with SessionLocal() as s:
        ids = list(s.scalars(select(Application.id)
                             .where(Application.reference_no.in_(refs))))
        s.execute(delete(Appointment).where(Appointment.application_id.in_(ids)))
        s.execute(delete(OutboxEvent).where(OutboxEvent.application_id.in_(ids)))
        s.commit()
        reconcile_slots(s, fix=True)


def pick_regular_slot_key(client: TestClient) -> str:
    """A normal (5-seat), completely free, non-demo slot."""
    slots = client.get("/slots", params={"limit": 300}).json()
    return next(s["key"] for s in slots
                if not s["demo"] and s["available"] == s["capacity"])


# -------------------------------------------------------------------- tests
def test_50_simultaneous_requests_for_last_slot_exactly_one_wins():
    r = get_redis()
    demo_key = r.get(DEMO_POINTER_KEY)
    r.hset(demo_key, "available", 1)             # make sure the demo slot has its 1 seat
    refs = free_application_refs(50)
    assert len(refs) == 50

    barrier = threading.Barrier(50)              # everyone waits here, then all go at once
    lock = threading.Lock()
    results: list[int] = []

    def worker(ref: str) -> None:
        client = make_client()
        try:
            barrier.wait(timeout=30)
            resp = client.post("/appointments",
                               json={"application_ref": ref, "slot_key": demo_key})
            code = resp.status_code
        except Exception:
            code = -1
        with lock:
            results.append(code)

    try:
        threads = [threading.Thread(target=worker, args=(ref,)) for ref in refs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with SessionLocal() as s:
            ids = list(s.scalars(select(Application.id)
                                 .where(Application.reference_no.in_(refs))))
            booked = s.scalar(
                select(func.count()).select_from(Appointment)
                .where(Appointment.application_id.in_(ids),
                       Appointment.status == "BOOKED"))

        assert len(results) == 50
        assert results.count(201) == 1, f"winners: {results.count(201)}  all: {results}"
        assert results.count(409) == 49, f"all results: {results}"
        assert booked == 1                       # ZERO double-bookings in PostgreSQL
        assert r.hget(demo_key, "available") == "0"
    finally:
        clean_up(refs)


def test_same_application_cannot_book_twice():
    client = make_client()
    r = get_redis()
    key = pick_regular_slot_key(client)
    ref = free_application_refs(1)[0]
    before = int(r.hget(key, "available"))
    body = {"application_ref": ref, "slot_key": key}
    try:
        assert client.post("/appointments", json=body).status_code == 201
        assert client.post("/appointments", json=body).status_code == 409
        assert int(r.hget(key, "available")) == before - 1   # only ONE seat used
    finally:
        clean_up([ref])


def test_failed_database_write_gives_the_seat_back(monkeypatch):
    """Simulate a crash AFTER Redis took a seat but BEFORE PostgreSQL commits."""
    client = make_client()
    r = get_redis()
    key = pick_regular_slot_key(client)
    ref = free_application_refs(1)[0]
    before = int(r.hget(key, "available"))

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure while saving")

    monkeypatch.setattr("app.booking.enqueue", boom)
    try:
        resp = client.post("/appointments", json={"application_ref": ref, "slot_key": key})
        assert resp.status_code == 500
        assert int(r.hget(key, "available")) == before       # seat was released
        with SessionLocal() as s:
            assert s.scalar(select(func.count()).select_from(Appointment)
                            .join(Application, Application.id == Appointment.application_id)
                            .where(Application.reference_no == ref)) == 0
    finally:
        clean_up([ref])


def test_reconciliation_detects_and_repairs_a_lost_seat():
    client = make_client()
    r = get_redis()
    key = pick_regular_slot_key(client)
    r.hincrby(key, "available", -1)              # simulate: seat taken, crash before saving
    try:
        with SessionLocal() as s:
            found = reconcile_slots(s)
            assert any(m["key"] == key and m["expected"] == m["actual"] + 1
                       for m in found)
            reconcile_slots(s, fix=True)
            assert reconcile_slots(s) == []
    finally:
        with SessionLocal() as s:
            reconcile_slots(s, fix=True)


def test_get_slots_filters_by_site():
    client = make_client()
    resp = client.get("/slots", params={"site_code": "CEB", "limit": 300})
    assert resp.status_code == 200
    slots = resp.json()
    assert len(slots) == 80                      # 10 weekdays x 8 hourly slots
    assert all(s["site_code"] == "CEB" for s in slots)
    starts = [s["start"] for s in slots]
    assert starts == sorted(starts)              # earliest first


def test_create_applicant_returns_reference_only_and_validates_input():
    client = make_client()
    payload = {
        "first_name": "Test", "middle_name": None, "last_name": "Dummy",
        "birthdate": "1990-01-01", "birthplace": "Test City",
        "mother_maiden_name": "Test Mother", "email": "test@example.com",
        "purpose": "EMPLOYMENT",
    }
    resp = client.post("/applicants", json=payload)
    assert resp.status_code == 201
    ref = resp.json()["application_ref"]
    try:
        assert ref.startswith("NBI-")
        assert "birthdate" not in resp.text and "Dummy" not in resp.text
        with SessionLocal() as s:
            application = s.scalar(select(Application)
                                   .where(Application.reference_no == ref))
            events = list(s.scalars(select(OutboxEvent.event_type)
                                    .where(OutboxEvent.application_id == application.id)))
        assert events == ["APPLICATION_SUBMITTED"]    # outbox row saved in same transaction
    finally:
        with SessionLocal() as s:
            application = s.scalar(select(Application)
                                   .where(Application.reference_no == ref))
            s.execute(delete(OutboxEvent)
                      .where(OutboxEvent.application_id == application.id))
            s.execute(delete(Application).where(Application.id == application.id))
            s.execute(delete(Applicant).where(Applicant.id == application.applicant_id))
            s.commit()

    bad = client.post("/applicants", json={**payload, "purpose": "NOT_A_PURPOSE"})
    assert bad.status_code == 422


def test_unknown_application_or_slot_returns_404():
    client = make_client()
    key = pick_regular_slot_key(client)
    ref = free_application_refs(1)[0]
    r1 = client.post("/appointments", json={"application_ref": "NBI-DOES-NOT-EXIST",
                                            "slot_key": key})
    r2 = client.post("/appointments", json={"application_ref": ref,
                                            "slot_key": "slot:MNL:20000101T0800"})
    assert r1.status_code == 404
    assert r2.status_code == 404