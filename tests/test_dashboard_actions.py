"""Tests for the 'New application and payment' actions (in-process, same code as the page).
Run first: seed_users, load_data."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.clients import TIMELINE_COLLECTION, get_mongo_db
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import Applicant, Application, Appointment, HitCase, OutboxEvent, Payment
from dashboard import actions
from dashboard import client as dash_client

FIELDS = {"first_name": "Dina", "middle_name": "", "last_name": "Testperson",
          "birthdate": date(1990, 1, 1), "birthplace": "Test City",
          "mother_maiden_name": "Test Mother", "email": "dina@example.com",
          "purpose": "EMPLOYMENT"}


@pytest.fixture(autouse=True)
def in_process_api(monkeypatch):
    monkeypatch.setattr(dash_client, "make_http",
                        lambda: TestClient(fastapi_app, raise_server_exceptions=False))


def clean_up(ref: str) -> None:
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


def test_apply_pay_and_gateway_confirms_payment():
    res = actions.create_application(FIELDS, "applicant")
    assert res.status == 201
    ref = res.data["application_ref"]
    try:
        assert ref.startswith("NBI-") and res.data["status"] == "SUBMITTED"
        pay = actions.request_payment(ref, "applicant")
        assert pay.status == 201 and pay.data["amount"] == "130.00"
        assert pay.data["status"] == "PENDING"
        again = actions.request_payment(ref, "applicant")          # no second open payment
        assert again.data["payment_ref"] == pay.data["payment_ref"]

        hook = actions.gateway_send(pay.data["payment_ref"], "PAID", event_id="EVT-ACT-1")
        assert hook.ok and hook.data["result"] == "applied"
        dup = actions.gateway_send(pay.data["payment_ref"], "PAID", event_id="EVT-ACT-1")
        assert dup.ok and dup.data["result"] == "duplicate"

        status = actions.get_status(ref, "applicant")
        assert status.ok and status.data["status"] == "PAID"
        assert status.data["payment"]["status"] == "PAID"
        types = [e["type"] for e in status.data["timeline"]]
        for expected in ("APPLICATION_SUBMITTED", "PAYMENT_REQUESTED", "PAYMENT_RECEIVED"):
            assert expected in types
        assert types.count("PAYMENT_RECEIVED") == 1
    finally:
        clean_up(ref)


def test_failed_payment_can_be_retried_with_a_new_payment():
    ref = actions.create_application(FIELDS, "frontdesk").data["application_ref"]
    try:
        first = actions.request_payment(ref, "frontdesk").data
        failed = actions.gateway_send(first["payment_ref"], "FAILED")
        assert failed.ok and failed.data["payment_status"] == "FAILED"

        second = actions.request_payment(ref, "frontdesk").data
        assert second["payment_ref"] != first["payment_ref"]       # a fresh attempt
        paid = actions.gateway_send(second["payment_ref"], "PAID")
        assert paid.ok and paid.data["result"] == "applied"
        assert actions.get_status(ref, "applicant").data["status"] == "PAID"
    finally:
        clean_up(ref)


def test_reviewer_cannot_create_applications_or_request_payments():
    assert actions.create_application(FIELDS, "reviewer").status == 403
    assert actions.request_payment("NBI-000001", "reviewer").status == 403


def test_invalid_input_gives_a_readable_message():
    bad_purpose = actions.create_application({**FIELDS, "purpose": "NOPE"}, "applicant")
    assert bad_purpose.status == 422
    assert "purpose" in actions.explain_error(bad_purpose)
    future = actions.create_application(
        {**FIELDS, "birthdate": date.today() + timedelta(days=1)}, "applicant")
    assert future.status == 422 and "birthdate" in actions.explain_error(future)


def test_gateway_amount_mismatch_is_rejected_and_payment_stays_pending():
    ref = actions.create_application(FIELDS, "applicant").data["application_ref"]
    try:
        pay = actions.request_payment(ref, "applicant").data
        bad = actions.gateway_send(pay["payment_ref"], "PAID", amount="1.00")
        assert bad.status == 422
        assert actions.get_status(ref, "applicant").data["payment"]["status"] == "PENDING"
    finally:
        clean_up(ref)