"""Integration tests: are all four stores loaded correctly and consistently?
Run `python -m scripts.load_data` first."""
from sqlalchemy import func, inspect, select

from app.clients import ES_INDEX, TIMELINE_COLLECTION, get_es, get_mongo_db, get_redis
from app.db import SessionLocal, engine
from app.models import Applicant, Application, WatchlistRecord
from app.slots import DEMO_POINTER_KEY, SLOT_INDEX_KEY, build_slots


def count(session, model, *conditions):
    return session.scalar(select(func.count()).select_from(model).where(*conditions))


def test_postgres_row_counts():
    with SessionLocal() as s:
        assert count(s, WatchlistRecord) == 2000
        assert count(s, Applicant) == 10000
        assert count(s, Application) == 10000
        # every applicant has exactly one application
        distinct = s.scalar(select(func.count(func.distinct(Application.applicant_id))))
        assert distinct == 10000


def test_applicants_table_has_no_label_columns():
    """The hidden answer key must never be stored where the scorer can read it."""
    columns = {c["name"] for c in inspect(engine).get_columns("applicants")}
    for forbidden in ("category", "is_true_match", "linked_record_no", "ground_truth", "split"):
        assert forbidden not in columns


def test_blank_middle_names_stored_as_null():
    with SessionLocal() as s:
        assert count(s, Applicant, Applicant.middle_name == "") == 0
        assert count(s, Applicant, Applicant.middle_name.is_(None)) > 0


def test_every_application_has_a_mongo_timeline():
    coll = get_mongo_db()[TIMELINE_COLLECTION]
    with SessionLocal() as s:
        assert coll.count_documents({}) == count(s, Application)
    doc = coll.find_one({})
    assert len(doc["events"]) == 1
    assert doc["events"][0]["type"] == "APPLICATION_SUBMITTED"
    # the unique index guarantees one timeline per application reference
    info = coll.index_information()
    assert any(v.get("unique") and v["key"] == [("application_ref", 1)]
               for v in info.values())


def test_elasticsearch_matches_postgres_and_stores_names_only():
    es = get_es()
    with SessionLocal() as s:
        assert es.count(index=ES_INDEX)["count"] == count(s, WatchlistRecord)
    source = es.get(index=ES_INDEX, id="WL-00001")["_source"]
    assert set(source) == {"pg_id", "record_no", "first_name", "middle_name",
                           "last_name", "full_name"}   # no birthdate etc.


def test_elasticsearch_fuzzy_search_tolerates_a_typo():
    with SessionLocal() as s:
        record = s.scalars(
            select(WatchlistRecord)
            .where(func.length(WatchlistRecord.last_name) >= 6,
                   ~WatchlistRecord.last_name.contains(" "))
            .order_by(WatchlistRecord.id)
        ).first()
    surname = record.last_name
    typo_surname = surname[:3] + surname[4:]     # delete one letter
    query = f"{record.first_name} {record.middle_name or ''} {typo_surname}"
    hits = get_es().search(index=ES_INDEX, size=200, query={
        "match": {"full_name": {"query": query, "fuzziness": "AUTO"}}
    })["hits"]["hits"]
    assert record.record_no in [h["_source"]["record_no"] for h in hits]


def test_redis_slot_inventory_is_consistent():
    r = get_redis()
    keys = r.zrange(SLOT_INDEX_KEY, 0, -1)
    assert len(keys) == len(build_slots())
    assert len(list(r.scan_iter("slot:*"))) == len(keys)
    for key in keys:
        slot = r.hgetall(key)
        assert 0 <= int(slot["available"]) <= int(slot["capacity"])
    demo_key = r.get(DEMO_POINTER_KEY)
    assert demo_key in keys
    assert r.hget(demo_key, "capacity") == "1"     # the one-seat demo slot