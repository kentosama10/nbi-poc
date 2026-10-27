"""HIT matching tests, including DEMO SCENARIO 3 (common name) and 4 (true match).
Run first: seed_users, load_data, train_model."""
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import config
from app.candidates import search_candidates
from app.clients import TIMELINE_COLLECTION, get_mongo_db
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import Applicant, Application, HitCase, OutboxEvent, User, WatchlistRecord
from ml.features import FEATURE_NAMES, compute_features
from ml.model import get_model

REVIEWER = {"X-API-Key": config.API_KEY_REVIEWER}
FRONTDESK = {"X-API-Key": config.API_KEY_FRONTDESK}
APPLICANT = {"X-API-Key": config.API_KEY_APPLICANT}

BASE_APPLICANT = {
    "first_name": "Test", "middle_name": None, "last_name": "Dummy",
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


def reviewer_id() -> int:
    with SessionLocal() as s:
        return s.scalar(select(User.id).where(User.username == "reviewer1"))


def app_id_of(ref: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Application.id).where(Application.reference_no == ref))


def record_dict(r: WatchlistRecord) -> dict:
    return {"id": r.id, "first_name": r.first_name, "middle_name": r.middle_name,
            "last_name": r.last_name, "birthdate": r.birthdate,
            "birthplace": r.birthplace, "mother_maiden_name": r.mother_maiden_name}


def common_name_record() -> dict:
    """A watchlist record that has the most common surname in the watchlist."""
    with SessionLocal() as s:
        surname = s.execute(
            select(WatchlistRecord.last_name, func.count().label("n"))
            .group_by(WatchlistRecord.last_name)
            .order_by(func.count().desc())).first()[0]
        record = s.scalars(select(WatchlistRecord)
                           .where(WatchlistRecord.last_name == surname)
                           .order_by(WatchlistRecord.id)).first()
        return record_dict(record)


def clean_up(ref: str) -> None:
    with SessionLocal() as s:
        application = s.scalar(select(Application).where(Application.reference_no == ref))
        if application is None:
            return
        app_id, applicant_id = application.id, application.applicant_id
        s.execute(delete(HitCase).where(HitCase.application_id == app_id))
        s.execute(delete(OutboxEvent).where(OutboxEvent.application_id == app_id))
        s.execute(delete(Application).where(Application.id == app_id))
        s.execute(delete(Applicant).where(Applicant.id == applicant_id))
        s.commit()
    get_mongo_db()[TIMELINE_COLLECTION].delete_one({"_id": app_id})


@pytest.fixture()
def make_applicant():
    """Returns (client, make). make(**fields) creates a synthetic application and
    remembers it so it is deleted after the test."""
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    created: list[str] = []

    def make(**fields) -> str:
        resp = client.post("/applicants", json={**BASE_APPLICANT, **fields},
                           headers=APPLICANT)
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["application_ref"])
        return created[-1]

    yield client, make
    for ref in created:
        clean_up(ref)


def exact_copy_fields(rec: dict) -> dict:
    return {"first_name": rec["first_name"], "middle_name": rec["middle_name"],
            "last_name": rec["last_name"], "birthdate": rec["birthdate"].isoformat(),
            "birthplace": rec["birthplace"],
            "mother_maiden_name": rec["mother_maiden_name"]}


def make_escalated_case(client, make):
    """An applicant identical to a watchlist record -> run the HIT check.
    Returns (ref, record, response_body, hit_case_id)."""
    rec = common_name_record()
    ref = make(**exact_copy_fields(rec))
    resp = client.post(f"/applications/{ref}/hit-check", headers=REVIEWER)
    assert resp.status_code == 200, resp.text
    with SessionLocal() as s:
        case_id = s.scalar(select(HitCase.id).where(
            HitCase.application_id == app_id_of(ref),
            HitCase.watchlist_record_id == rec["id"]))
    assert case_id is not None
    return ref, rec, resp.json(), case_id


# -------------------------------------------------------------------- tests
def test_identical_people_have_perfect_features():
    p = {"first_name": "Jose", "middle_name": "Reyes", "last_name": "Santos",
         "birthdate": date(1990, 5, 17), "birthplace": "Manila",
         "mother_maiden_name": "Ana Reyes"}
    f = compute_features(p, p)
    assert set(f) == set(FEATURE_NAMES)
    for name in ("full_name_jw", "surname_jw", "first_name_jw", "birthdate_exact",
                 "birthplace_jw", "mother_maiden_jw"):
        assert f[name] == pytest.approx(1.0)
    assert f["birthdate_close"] == 0.0 and f["birth_year_gap"] == 0.0


def test_features_reward_typos_and_detect_swapped_dates():
    a = {"first_name": "Jose", "middle_name": None, "last_name": "Santos",
         "birthdate": "1990-03-04", "birthplace": "Manila", "mother_maiden_name": ""}
    b = {"first_name": "Jose", "middle_name": "Reyes", "last_name": "Santoz",
         "birthdate": "1990-04-03", "birthplace": "Manlia",
         "mother_maiden_name": "Ana Reyes"}
    f = compute_features(a, b)
    assert 0.9 < f["surname_jw"] < 1.0                    # one typo is still very similar
    assert f["birthdate_exact"] == 0.0 and f["birthdate_close"] == 1.0   # day/month swapped
    assert f["birth_year_gap"] == 0.0
    assert f["mother_maiden_jw"] == 0.0                   # missing data = no evidence


def test_model_file_is_valid():
    m = get_model()
    assert m.feature_names == FEATURE_NAMES
    assert len(m.coefficients) == len(FEATURE_NAMES)
    assert 0 < m.auto_clear <= 0.20
    assert m.escalate == 0.80


def test_decision_bands_follow_the_thresholds():
    m = get_model()
    assert m.band(0.0) == "AUTO_CLEAR"
    assert m.band(m.auto_clear - 1e-6) == "AUTO_CLEAR"
    assert m.band(m.auto_clear) == "REVIEW"
    assert m.band(0.80) == "REVIEW"
    assert m.band(0.8001) == "ESCALATE"
    assert m.band(1.0) == "ESCALATE"


def test_explanation_adds_up_to_the_score():
    m = get_model()
    a = {"first_name": "Jose", "middle_name": "Reyes", "last_name": "Santos",
         "birthdate": "1990-05-17", "birthplace": "Manila",
         "mother_maiden_name": "Ana Reyes"}
    b = {**a, "first_name": "Josie", "birthdate": "1991-05-17"}
    feats = compute_features(a, b)
    exp = m.explain(feats)
    total = exp["intercept"] + sum(r["contribution"] for r in exp["features"])
    assert abs(total - exp["log_odds"]) < 1e-3            # contributions add up exactly
    assert abs(m.predict(feats) - exp["probability"]) < 1e-4
    assert exp["band"] == m.band(exp["probability"])
    assert {r["name"] for r in exp["features"]} == set(FEATURE_NAMES)


def test_candidate_search_finds_a_record_despite_a_surname_typo():
    with SessionLocal() as s:
        rec = record_dict(s.scalars(
            select(WatchlistRecord)
            .where(func.length(WatchlistRecord.last_name) >= 6,
                   ~WatchlistRecord.last_name.contains(" "))
            .order_by(WatchlistRecord.id)).first())
    typo = rec["last_name"][:3] + rec["last_name"][4:]    # delete one letter
    found = search_candidates(rec["first_name"], rec["middle_name"], typo)
    assert rec["id"] in [c["pg_id"] for c in found]


def test_hit_check_endpoints_need_a_staff_key():
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    url = "/applications/NBI-000001/hit-check"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=FRONTDESK).status_code == 403
    assert client.post(url, headers=APPLICANT).status_code == 403
    assert client.get("/review-queue").status_code == 401
    assert client.get("/review-queue", headers=FRONTDESK).status_code == 403


def test_scenario3_common_name_applicant_is_flagged_scored_and_explained(make_applicant):
    """DEMO SCENARIO 3: same name as a watchlist person, but a different person."""
    client, make = make_applicant
    rec = common_name_record()
    other_date = date(1999, 12, 31) if rec["birthdate"].year != 1999 else date(1980, 6, 15)
    ref = make(first_name=rec["first_name"], last_name=rec["last_name"],
               middle_name=None, birthdate=other_date.isoformat(),
               birthplace="Unlisted Town", mother_maiden_name="Maria Unrelated")

    resp = client.post(f"/applications/{ref}/hit-check", headers=REVIEWER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["candidates_found"] >= 1                  # flagged by name

    with SessionLocal() as s:
        cases = list(s.scalars(select(HitCase).where(HitCase.application_id == app_id_of(ref))))
    mine = [c for c in cases if c.watchlist_record_id == rec["id"]]
    assert len(mine) == 1                                 # the same-name record was scored
    assert mine[0].match_probability < 0.80               # a namesake is not escalated
    assert mine[0].decision_band in ("AUTO_CLEAR", "REVIEW")

    detail = client.get(f"/hit-cases/{mine[0].id}", headers=REVIEWER).json()
    assert {r["name"] for r in detail["explanation"]["features"]} == set(FEATURE_NAMES)
    assert detail["explanation"]["probability"] == pytest.approx(mine[0].match_probability)

    # The AI never records a final decision
    assert all(c.final_decision is None and c.status != "DECIDED" for c in cases)
    # Status is consistent: queued if any case is open, otherwise cleared
    any_open = any(c.status == "OPEN" for c in cases)
    assert body["status"] in (("UNDER_REVIEW", "ESCALATED") if any_open else ("CLEARED",))
    if mine[0].status == "OPEN":
        queue = client.get("/review-queue", params={"application_ref": ref},
                           headers=REVIEWER).json()
        assert any(i["hit_case_id"] == mine[0].id for i in queue["items"])


def test_hit_check_is_idempotent(make_applicant):
    client, make = make_applicant
    rec = common_name_record()
    ref = make(first_name=rec["first_name"], last_name=rec["last_name"])
    first = client.post(f"/applications/{ref}/hit-check", headers=REVIEWER).json()
    second = client.post(f"/applications/{ref}/hit-check", headers=REVIEWER).json()
    assert first["already_checked"] is False
    assert second["already_checked"] is True
    assert second["candidates_found"] == first["candidates_found"]
    with SessionLocal() as s:
        n = s.scalar(select(func.count()).select_from(HitCase)
                     .where(HitCase.application_id == app_id_of(ref)))
    assert n == first["candidates_found"]                 # no duplicate rows


def test_scenario4_true_match_is_escalated_and_a_human_decides(make_applicant):
    """DEMO SCENARIO 4: high-confidence match -> reviewer records the final decision."""
    client, make = make_applicant
    ref, rec, body, case_id = make_escalated_case(client, make)

    with SessionLocal() as s:
        case = s.get(HitCase, case_id)
        assert case.decision_band == "ESCALATE" and case.match_probability > 0.80
        assert case.status == "OPEN" and case.final_decision is None   # AI did not decide
    assert body["status"] == "ESCALATED"

    queue = client.get("/review-queue", params={"application_ref": ref},
                       headers=REVIEWER).json()
    item = next(i for i in queue["items"] if i["hit_case_id"] == case_id)
    assert item["band"] == "ESCALATE"
    assert item["explanation"]["features"] and item["watchlist"]["record_no"]

    resp = client.post(f"/review-queue/{case_id}/decision", headers=REVIEWER,
                       json={"decision": "CONFIRMED_MATCH", "note": "synthetic test"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["application_status"] == "HIT_CONFIRMED"
    with SessionLocal() as s:
        case = s.get(HitCase, case_id)
        assert case.status == "DECIDED" and case.final_decision == "CONFIRMED_MATCH"
        assert case.decided_by_user_id == reviewer_id() and case.decided_at is not None

    # The applicant-facing timeline updates, and reveals nothing about watchlists
    deadline, st = time.perf_counter() + 5, {}
    while time.perf_counter() < deadline:
        st = client.get(f"/applications/{ref}/status", headers=APPLICANT).json()
        if "VERIFICATION_COMPLETED" in [e["type"] for e in st["timeline"]]:
            break
        time.sleep(0.1)
    assert "VERIFICATION_COMPLETED" in [e["type"] for e in st["timeline"]]
    assert not any(word in e["detail"].lower()
                   for e in st["timeline"] for word in ("match", "watchlist", "hit"))


def test_only_reviewers_can_decide_and_only_once(make_applicant):
    client, make = make_applicant
    _, _, _, case_id = make_escalated_case(client, make)
    url = f"/review-queue/{case_id}/decision"
    body = {"decision": "NOT_A_MATCH", "note": "synthetic test"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=FRONTDESK).status_code == 403
    assert client.post(url, json=body, headers=APPLICANT).status_code == 403
    assert client.post(url, json={"decision": "MAYBE"}, headers=REVIEWER).status_code == 422
    assert client.post(url, json=body, headers=REVIEWER).status_code == 200
    assert client.post(url, json=body, headers=REVIEWER).status_code == 409   # already decided