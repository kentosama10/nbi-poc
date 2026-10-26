"""Load the synthetic CSV files into PostgreSQL, Elasticsearch, MongoDB, Redis.

Run from the project root:
    python -m scripts.load_data            (loads only if the database is empty)
    python -m scripts.load_data --reload   (wipes the loaded data, then reloads)

NOTE: ground_truth.csv is deliberately NOT loaded anywhere. Only the evaluation
script (Phase 11) reads it, so the scorer can never see the answers.
"""
import csv
import random
import sys
import time
from datetime import date, datetime, timezone

from elasticsearch import helpers
from sqlalchemy import func, insert, inspect, select, text

import app.models  # noqa: F401  (registers the tables)
from app.clients import (
    ES_INDEX, TIMELINE_COLLECTION, get_es, get_mongo_db, get_redis,
)
from app.db import SessionLocal, engine
from app.models import Applicant, Application, WatchlistRecord
from app.slots import DEMO_POINTER_KEY, SLOT_INDEX_KEY, build_slots
from scripts.generate_data import DATA_DIR

PURPOSES = ["EMPLOYMENT", "TRAVEL_ABROAD", "BUSINESS", "SCHOLARSHIP", "PERSONAL"]

# Elasticsearch gets NAMES ONLY (data minimization, privacy rule #6)
ES_SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0}
ES_MAPPINGS = {
    "properties": {
        "pg_id": {"type": "integer"},       # the row id in PostgreSQL
        "record_no": {"type": "keyword"},   # exact-match text, e.g. WL-00001
        "first_name": {"type": "text"},     # "text" = searchable, typo-tolerant
        "middle_name": {"type": "text"},
        "last_name": {"type": "text"},
        "full_name": {"type": "text"},
    }
}


def read_csv(name: str) -> list[dict]:
    path = DATA_DIR / name
    if not path.exists():
        sys.exit(f"Missing {path}. Run first: python -m scripts.generate_data")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- PostgreSQL
def reset_postgres() -> None:
    """Empty the data tables. CASCADE also empties tables that point at them
    (applications, payments, appointments, hit_cases, outbox_events).
    users and audit_log are NOT touched."""
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE watchlist_records, applicants RESTART IDENTITY CASCADE"
        ))


def load_postgres(watchlist_rows, applicant_rows):
    """Bulk-insert everything in ONE transaction: all or nothing."""
    rng = random.Random(42)
    with SessionLocal() as s:
        s.execute(insert(WatchlistRecord), [{
            "record_no": w["record_no"],
            "first_name": w["first_name"],
            "middle_name": w["middle_name"] or None,   # empty text -> NULL
            "last_name": w["last_name"],
            "birthdate": date.fromisoformat(w["birthdate"]),
            "birthplace": w["birthplace"],
            "mother_maiden_name": w["mother_maiden_name"],
            "case_reference": w["case_reference"],
            "offense_category": w["offense_category"],
        } for w in watchlist_rows])

        s.execute(insert(Applicant), [{
            "reference_no": a["reference_no"],
            "first_name": a["first_name"],
            "middle_name": a["middle_name"] or None,
            "last_name": a["last_name"],
            "birthdate": date.fromisoformat(a["birthdate"]),
            "birthplace": a["birthplace"],
            "mother_maiden_name": a["mother_maiden_name"],
            "email": a["email"],
        } for a in applicant_rows])

        # One application per applicant: APP-000001 -> NBI-000001
        id_of = {ref: pk for ref, pk in
                 s.execute(select(Applicant.reference_no, Applicant.id)).all()}
        s.execute(insert(Application), [{
            "reference_no": "NBI-" + a["reference_no"].split("-")[1],
            "applicant_id": id_of[a["reference_no"]],
            "purpose": rng.choice(PURPOSES),
            "status": "SUBMITTED",
        } for a in applicant_rows])
        s.commit()

        # Return (id, reference_no) of every application, for MongoDB
        return s.execute(
            select(Application.id, Application.reference_no)
        ).all()


def already_loaded() -> bool:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(Applicant)) > 0


# ------------------------------------------------------------- Elasticsearch
def load_elasticsearch(watchlist_rows) -> int:
    es = get_es()
    es.indices.delete(index=ES_INDEX, ignore_unavailable=True)  # start fresh
    es.indices.create(index=ES_INDEX, settings=ES_SETTINGS, mappings=ES_MAPPINGS)

    with SessionLocal() as s:
        pg_id = {rn: pk for rn, pk in
                 s.execute(select(WatchlistRecord.record_no, WatchlistRecord.id)).all()}

    def actions():
        for w in watchlist_rows:
            parts = [w["first_name"], w["middle_name"], w["last_name"]]
            yield {
                "_index": ES_INDEX,
                "_id": w["record_no"],      # same id every time = safe to re-run
                "_source": {
                    "pg_id": pg_id[w["record_no"]],
                    "record_no": w["record_no"],
                    "first_name": w["first_name"],
                    "middle_name": w["middle_name"] or "",
                    "last_name": w["last_name"],
                    "full_name": " ".join(p for p in parts if p),
                },
            }

    ok, _ = helpers.bulk(es, actions())
    es.indices.refresh(index=ES_INDEX)   # make the new documents searchable now
    return ok


# ------------------------------------------------------------------- MongoDB
def load_mongo(applications) -> int:
    """One timeline document per application. _id = the PostgreSQL application id."""
    coll = get_mongo_db()[TIMELINE_COLLECTION]
    coll.delete_many({})
    now = datetime.now(timezone.utc)
    docs = [{
        "_id": app_id,
        "application_ref": ref,
        "events": [{
            "type": "APPLICATION_SUBMITTED",
            "at": now,
            "detail": "Application received",
            "source": "loader",
        }],
        "updated_at": now,
    } for app_id, ref in applications]
    coll.insert_many(docs, ordered=False)
    coll.create_index("application_ref", unique=True)
    return len(docs)


# --------------------------------------------------------------------- Redis
def load_redis() -> tuple[int, int]:
    r = get_redis()
    # Remove only OUR keys (never FLUSHALL)
    old = list(r.scan_iter(match="slot:*", count=500))
    old += [SLOT_INDEX_KEY, DEMO_POINTER_KEY]
    r.delete(*old)

    slots = build_slots()
    pipe = r.pipeline()
    for s in slots:
        pipe.hset(s["key"], mapping={
            "site_code": s["site_code"],
            "site": s["site"],
            "start": s["start"].isoformat(),
            "capacity": s["capacity"],
            "available": s["available"],
            "demo": "1" if s["demo"] else "0",
        })
        pipe.zadd(SLOT_INDEX_KEY, {s["key"]: s["start"].timestamp()})
        if s["demo"]:
            pipe.set(DEMO_POINTER_KEY, s["key"])
    pipe.execute()
    return len(slots), sum(s["capacity"] for s in slots)


# ------------------------------------------------------------- cross-check
def cross_check() -> bool:
    """Compare record counts across the stores (pain point #4: consistency)."""
    with SessionLocal() as s:
        pg_wl = s.scalar(select(func.count()).select_from(WatchlistRecord))
        pg_app = s.scalar(select(func.count()).select_from(Applicant))
        pg_apn = s.scalar(select(func.count()).select_from(Application))
    es_n = get_es().count(index=ES_INDEX)["count"]
    mongo_n = get_mongo_db()[TIMELINE_COLLECTION].count_documents({})
    redis_n = get_redis().zcard(SLOT_INDEX_KEY)

    print("\nCross-check (store: count)")
    print(f"  PostgreSQL watchlist_records : {pg_wl}")
    print(f"  Elasticsearch watchlist_names: {es_n}")
    print(f"  PostgreSQL applicants        : {pg_app}")
    print(f"  PostgreSQL applications      : {pg_apn}")
    print(f"  MongoDB status_timeline      : {mongo_n}")
    print(f"  Redis slots                  : {redis_n}")
    ok = (pg_wl == es_n) and (pg_apn == mongo_n == pg_app)
    print("  " + ("All stores are consistent." if ok else "MISMATCH! Run with --reload."))
    return ok


def sample_fuzzy_search() -> None:
    """Show Elasticsearch finding names despite a typo."""
    es = get_es()
    query = "Jose Santoz"   # typo on purpose
    hits = es.search(index=ES_INDEX, size=3, query={
        "match": {"full_name": {"query": query, "fuzziness": "AUTO"}}
    })["hits"]["hits"]
    print(f"\nSample fuzzy search for '{query}' (typo on purpose), top 3:")
    for h in hits:
        print(f"  {h['_source']['record_no']}  {h['_source']['full_name']:<32}"
              f" score {h['_score']:.2f}")


# ---------------------------------------------------------------------- main
def main() -> None:
    reload_ = "--reload" in sys.argv

    if "watchlist_records" not in inspect(engine).get_table_names():
        sys.exit("Tables are missing. Run first: python -m scripts.init_db")
    if already_loaded() and not reload_:
        print("Data is already loaded. To wipe and reload, run:")
        print("    python -m scripts.load_data --reload")
        return

    started = time.perf_counter()
    print("Reading CSV files...")
    watchlist_rows = read_csv("watchlist.csv")
    applicant_rows = read_csv("applicants.csv")

    if reload_:
        print("--reload: wiping existing data first...")
        reset_postgres()

    applications = load_postgres(watchlist_rows, applicant_rows)
    print(f"PostgreSQL   : loaded {len(watchlist_rows)} watchlist records, "
          f"{len(applicant_rows)} applicants, {len(applications)} applications")

    n = load_elasticsearch(watchlist_rows)
    print(f"Elasticsearch: indexed {n} watchlist names into '{ES_INDEX}'")

    n = load_mongo(applications)
    print(f"MongoDB      : created {n} status timelines")

    n_slots, n_seats = load_redis()
    print(f"Redis        : created {n_slots} slots ({n_seats} seats)")

    cross_check()
    sample_fuzzy_search()
    print(f"\nDone in {time.perf_counter() - started:.1f} seconds.")


if __name__ == "__main__":
    main()