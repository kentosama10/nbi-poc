"""Evaluation: checks the PoC against its targets and writes results.md.

    python -m scripts.evaluate        (takes about 3 to 8 minutes)

Part A  live system checks (in-process, real databases): double-booking,
        payment-sync delay, audit-log coverage, analytics endpoint.
Part B  HIT matching: score the held-out TEST applicants WITHOUT labels,
        then grade them with ground_truth.csv.
ALL DATA IS SYNTHETIC.
"""
import csv
import json
import math
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.analytics import build_summary
from app.candidates import search_candidates
from app.clients import ES_INDEX, TIMELINE_COLLECTION, get_es, get_mongo_db, get_redis
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.models import (
    Applicant, Application, Appointment, AuditLog, HitCase, OutboxEvent, Payment, User,
)
from app.slots import DEMO_POINTER_KEY
from dashboard import client as dash_client
from dashboard import demo_tools, scenarios
from ml.features import compute_features
from ml.model import ARTIFACT_PATH, load_model
from scripts.generate_data import DATA_DIR, SEED

ROOT = Path(__file__).resolve().parent.parent
RESULTS_MD = ROOT / "results.md"
RESULTS_JSON = ROOT / "results.json"

CATEGORIES = ("TRUE_MATCH", "NAMESAKE", "UNRELATED")
REDUCTION_TARGET = 0.50     # our own target: at least 50% fewer manual reviews
DELAY_TARGET_S = 5.0        # our own target: timeline shows a payment within 5 s
CONCURRENCY_ROUNDS = 5
PAYMENT_RUNS = 20
COUNT_KEYS = ["applications", "flagged", "need_review", "escalated_apps", "pairs",
              "pairs_auto_clear", "pairs_review", "pairs_escalate", "true_total",
              "true_retrieved", "true_caught", "true_cleared", "true_not_found",
              "true_escalated", "true_review", "b_flagged", "b_caught"]


# ================================================================ small helpers
def api_client() -> TestClient:
    return TestClient(fastapi_app, raise_server_exceptions=False)


def ratio(part, whole):
    """part / whole, or None when there is nothing to divide by (never a fake 0)."""
    return round(part / whole, 4) if whole else None


def pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def secs(v) -> str:
    return "n/a" if v is None else f"{v:.3f} s"


def percentile(sorted_values, q):
    if not sorted_values:
        return None
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def miss_rate_upper_bound(misses: int, n: int, z: float = 1.645):
    """One-sided 95% upper bound on the true miss rate.
    0 misses: exact formula 1 - 0.05^(1/n) (about 3/n, the 'rule of three').
    Otherwise: the Wilson score bound."""
    if n <= 0:
        return None
    if misses == 0:
        return 1 - 0.05 ** (1 / n)
    p = misses / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre + margin) / denom


def read_csv(name: str) -> list[dict]:
    path = DATA_DIR / name
    if not path.exists():
        sys.exit(f"Missing {path}. Run first: python -m scripts.generate_data")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def delete_application(ref: str) -> None:
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


def audit_total() -> int:
    with SessionLocal() as s:
        return int(s.scalar(select(func.count()).select_from(AuditLog)))


def preflight() -> None:
    problems = []
    if not ARTIFACT_PATH.exists():
        problems.append("Model file missing. Run: python -m scripts.train_model")
    try:
        if get_es().count(index=ES_INDEX)["count"] == 0:
            problems.append("Elasticsearch is empty. Run: python -m scripts.load_data --reload")
    except Exception as exc:
        problems.append(f"Elasticsearch unreachable ({type(exc).__name__}). Run: docker compose ps")
    try:
        with SessionLocal() as s:
            n_users = s.scalar(select(func.count()).select_from(User).where(
                User.username.in_(["applicant1", "frontdesk1", "reviewer1", "admin1"])))
            n_apps = s.scalar(select(func.count()).select_from(Application))
        if n_users < 4:
            problems.append("Demo users missing. Run: python -m scripts.seed_users")
        if n_apps < 1000:
            problems.append("Applications not loaded. Run: python -m scripts.load_data --reload")
    except Exception as exc:
        problems.append(f"PostgreSQL problem ({type(exc).__name__}). Run: docker compose ps")
    try:
        if not get_redis().get(DEMO_POINTER_KEY):
            problems.append("Redis slots missing. Run: python -m scripts.load_data --reload")
    except Exception as exc:
        problems.append(f"Redis unreachable ({type(exc).__name__}). Run: docker compose ps")
    if problems:
        sys.exit("Cannot start the evaluation:\n  - " + "\n  - ".join(problems))


# ====================================================== PART A: live checks
def live_concurrency(rounds: int = CONCURRENCY_ROUNDS) -> dict:
    """Several rounds of 50 simultaneous requests for the one-seat demo slot."""
    results = []
    try:
        for i in range(rounds):
            r = scenarios.scenario1_concurrency()
            results.append(r)
            print(f"    round {i + 1}: {r['succeeded']} won, {r['slot_full']} told 'full', "
                  f"{r['booked_in_postgres']} booking in PostgreSQL")
        with SessionLocal() as s:
            bk = build_summary(s)["bookings"]      # independent double-booking checks
    finally:
        demo_tools.reset_demo_slot()               # leave the demo slot free again

    over = sum(max(r["booked_in_postgres"] - 1, 0) for r in results)   # capacity is 1
    clean = sum(1 for r in results if r["succeeded"] == 1 and r["slot_full"] == 49
                and not r["other"] and r["booked_in_postgres"] == 1)
    double = over + bk["applications_with_multiple_bookings"] + (bk["overbooked_slots"] or 0)
    return {
        "rounds": [{k: r[k] for k in ("succeeded", "slot_full", "other",
                                      "booked_in_postgres", "seconds")} for r in results],
        "overbooked_seats": over,
        "apps_multi": bk["applications_with_multiple_bookings"],
        "overbooked_slots": bk["overbooked_slots"],
        "double_bookings": double,
        "clean_rounds": clean,
    }


def live_payment_sync(runs: int = PAYMENT_RUNS) -> dict:
    """Simulated payment webhooks: how fast does the MongoDB timeline show them,
    and is the duplicate resend harmless?"""
    delays, failures, refs, consistent = [], [], [], 0
    try:
        for i in range(runs):
            try:
                r = scenarios.scenario2_payment_sync()
            except Exception as exc:
                failures.append(f"run {i + 1}: {type(exc).__name__}: {exc}")
                continue
            ref = r["application_ref"]
            refs.append(ref)
            delays.append(r["delay_seconds"])
            again = dash_client.call("GET", f"/applications/{ref}/status", "applicant")
            types = [e["type"] for e in again.data["timeline"]] if again.ok else []
            good = (r["webhook"]["result"] == "applied"
                    and r["duplicate"]["result"] == "duplicate"
                    and r["delay_seconds"] is not None
                    and again.ok and again.data["payment"]["status"] == "PAID"
                    and types.count("PAYMENT_RECEIVED") == 1
                    and again.data["pending_sync"] == 0)
            consistent += good
            if not good:
                failures.append(f"run {i + 1}: inconsistent ({ref})")
    finally:
        for ref in refs:
            delete_application(ref)
    measured = sorted(d for d in delays if d is not None)
    return {
        "runs": runs, "delays": measured,
        "avg": round(sum(measured) / len(measured), 3) if measured else None,
        "median": percentile(measured, 0.5), "p95": percentile(measured, 0.95),
        "max": measured[-1] if measured else None,
        "within_5s": sum(1 for d in measured if d < DELAY_TARGET_S),
        "consistent": consistent, "failures": failures,
    }


def live_audit_coverage() -> dict:
    """Make one call per audited operation (allowed, denied, unauthenticated) and
    check that each wrote exactly the expected number of audit rows."""
    api = api_client()
    key = {role: {"X-API-Key": k} for role, k in dash_client.ROLE_KEYS.items()}
    marker = uuid.uuid4().hex[:6].capitalize()
    person = {"first_name": "Evalia", "middle_name": None, "last_name": f"Evalq{marker}",
              "birthdate": "1991-02-03", "birthplace": "Evalburg",
              "mother_maiden_name": "Evalina Motherq", "email": "evalia@example.com",
              "purpose": "EMPLOYMENT"}
    state, texts = {}, {}

    def get(path, role):
        return api.get(path, headers=key[role])

    def create():
        resp = api.post("/applicants", json=person, headers=key["applicant"])
        if resp.status_code == 201:
            state["ref"] = resp.json()["application_ref"]
        return resp

    def pay():
        resp = api.post(f"/applications/{state['ref']}/payments", headers=key["applicant"])
        if resp.status_code == 201:
            state["pay"] = resp.json()["payment_ref"]
        return resp

    def hook(valid_signature):
        body = {"event_id": "EVT-EVAL-" + uuid.uuid4().hex[:8],
                "payment_ref": state["pay"], "status": "PAID", "amount": "130.00"}
        raw = json.dumps(body).encode()
        sig = dash_client.sign(raw) if valid_signature else "0" * 64
        return api.post("/webhooks/payment", content=raw,
                        headers={"X-Signature": sig, "Content-Type": "application/json"})

    def detail(role):
        resp = get(f"/applications/{state['ref']}", role)
        texts[role] = resp.text
        return resp

    def ref_url(suffix=""):
        return f"/applications/{state['ref']}{suffix}"

    steps = [
        ("POST /applicants (applicant)", 1, create),
        ("POST /applications/{ref}/payments (applicant)", 1, pay),
        ("POST /webhooks/payment, valid signature", 1, lambda: hook(True)),
        ("POST /webhooks/payment, forged signature", 1, lambda: hook(False)),
        ("GET /applications/{ref}/status (applicant)", 1,
         lambda: get(ref_url("/status"), "applicant")),
        ("GET /applications/{ref}/status (frontdesk)", 1,
         lambda: get(ref_url("/status"), "frontdesk")),
        ("GET /applications/{ref}/status (reviewer)", 1,
         lambda: get(ref_url("/status"), "reviewer")),
        ("GET /applications/{ref} (frontdesk, masked)", 1, lambda: detail("frontdesk")),
        ("GET /applications/{ref} (reviewer, full)", 1, lambda: detail("reviewer")),
        ("POST /applications/{ref}/hit-check (reviewer)", 1,
         lambda: api.post(ref_url("/hit-check"), headers=key["reviewer"])),
        ("GET /review-queue (reviewer)", 1, lambda: get("/review-queue", "reviewer")),
        ("GET /hit-cases/999999999 (reviewer)", 1,
         lambda: get("/hit-cases/999999999", "reviewer")),
        ("POST /review-queue/999999999/decision (reviewer)", 1,
         lambda: api.post("/review-queue/999999999/decision",
                          json={"decision": "NOT_A_MATCH"}, headers=key["reviewer"])),
        ("GET /analytics/summary (reviewer)", 1,
         lambda: get("/analytics/summary", "reviewer")),
        ("GET /audit-log (admin)", 1, lambda: get("/audit-log", "admin")),
        ("POST /appointments, unknown slot (applicant)", 1,
         lambda: api.post("/appointments", headers=key["applicant"],
                          json={"application_ref": state["ref"],
                                "slot_key": "slot:MNL:20000101T0800"})),
        ("GET /review-queue (frontdesk) -> 403", 1, lambda: get("/review-queue", "frontdesk")),
        ("POST /applications/{ref}/hit-check (applicant) -> 403", 1,
         lambda: api.post(ref_url("/hit-check"), headers=key["applicant"])),
        ("GET /analytics/summary (frontdesk) -> 403", 1,
         lambda: get("/analytics/summary", "frontdesk")),
        ("GET /audit-log (reviewer) -> 403", 1, lambda: get("/audit-log", "reviewer")),
        ("GET /review-queue (no key) -> 401", 1, lambda: api.get("/review-queue")),
        ("GET /audit-log (wrong key) -> 401", 1,
         lambda: api.get("/audit-log", headers={"X-API-Key": "WrongKey999"})),
        ("GET /health (open, not audited)", 0, lambda: api.get("/health")),
        ("GET /slots (any key, not audited)", 0, lambda: get("/slots", "applicant")),
    ]

    with SessionLocal() as s:
        start_id = s.scalar(select(func.max(AuditLog.id))) or 0

    results = []
    try:
        for label, expected, fn in steps:
            before = audit_total()
            try:
                status = fn().status_code
            except Exception:
                status = -1
            observed = audit_total() - before
            results.append({"label": label, "expected": expected, "observed": observed,
                            "status": status, "ok": observed == expected and status != -1})

        with SessionLocal() as s:
            rows = s.scalars(select(AuditLog).where(AuditLog.id > start_id)).all()
            blob = json.dumps([[r.username, r.role, r.action, r.resource_type,
                                r.resource_id, r.detail] for r in rows], default=str)
            denied_rows = sum(1 for r in rows if r.outcome == "DENIED")
    finally:
        if state.get("ref"):
            delete_application(state["ref"])

    forbidden = [person["last_name"], person["birthdate"], person["birthplace"],
                 person["mother_maiden_name"], person["email"], "WrongKey999",
                 *dash_client.ROLE_KEYS.values()]
    front, review = texts.get("frontdesk", ""), texts.get("reviewer", "")
    masking_ok = bool(front) and bool(review) and person["last_name"] in review and not any(
        secret in front for secret in (person["first_name"], person["last_name"],
                                       person["birthdate"], person["birthplace"],
                                       "Evalina", person["email"])
    ) and "hit_cases" not in front and "watchlist" not in front.lower()
    return {
        "steps": results,
        "coverage": ratio(sum(1 for r in results if r["ok"]), len(results)),
        "masking_ok": masking_ok,
        "pii_found": [f for f in forbidden if f in blob],
        "denied_rows": denied_rows,
    }


def live_analytics() -> dict:
    resp = api_client().get("/analytics/summary",
                            headers={"X-API-Key": dash_client.ROLE_KEYS["reviewer"]})
    if resp.status_code != 200:
        return {"ok": False, "missing": [f"HTTP {resp.status_code}"], "sections": []}
    data = resp.json()
    need = {"applications", "hit", "queue", "turnaround", "payments", "bookings", "sync"}
    missing = sorted(need - set(data))
    if "hit" in data:
        want = {"hit_rate", "auto_clear_rate", "manual_reviews_baseline",
                "manual_reviews_with_ai", "manual_reviews_avoided"}
        missing += [f"hit.{k}" for k in sorted(want - set(data["hit"]))]
    if "queue" in data and "open_cases" not in data["queue"]:
        missing.append("queue.open_cases")
    if "turnaround" in data and "human_review" not in data["turnaround"]:
        missing.append("turnaround.human_review")
    return {"ok": not missing, "missing": missing, "sections": sorted(data)}


# ================================================= PART B: HIT matching quality
def name_key(row: dict) -> tuple:
    return (row["first_name"].strip().lower(), row["last_name"].strip().lower())


def build_name_index(watchlist_rows: list[dict]) -> dict:
    """(first, last) -> record numbers. Used only for baseline B (exact names)."""
    index: dict = {}
    for w in watchlist_rows:
        index.setdefault(name_key(w), set()).add(w["record_no"])
    return index


def score_applicants(applicants: list[dict], wl_by_no: dict, model) -> dict:
    """Run the real pipeline (search, features, model, bands) for every applicant.
    Takes NO labels: the scorer cannot see the answers."""
    scores = {}
    for n, a in enumerate(applicants, start=1):
        pairs = []
        for c in search_candidates(a["first_name"], a["middle_name"], a["last_name"]):
            p = model.predict(compute_features(a, wl_by_no[c["record_no"]]))
            pairs.append({"record_no": c["record_no"], "p": p, "band": model.band(p)})
        scores[a["reference_no"]] = pairs
        if n % 500 == 0:
            print(f"    scored {n} of {len(applicants)} test applicants...")
    return scores


def grade(scores: dict, applicants: list[dict], truth: dict, name_index: dict) -> dict:
    """Compare the scores with the hidden answer key. Pure function (no databases)."""
    r = Counter()
    by_cat = {c: Counter() for c in CATEGORIES}
    true_ps, cleared_ps = [], []

    for a in applicants:
        ref = a["reference_no"]
        t = truth[ref]
        cat = t["category"]
        is_true = t["is_true_match"] == "1"
        pairs = scores[ref]
        nonclear = [p for p in pairs if p["band"] != "AUTO_CLEAR"]

        r["applications"] += 1
        r["flagged"] += bool(pairs)                  # AS-IS baseline A: flagged -> human
        r["need_review"] += bool(nonclear)           # with AI: only non-cleared -> human
        r["escalated_apps"] += any(p["band"] == "ESCALATE" for p in pairs)
        by_cat[cat]["applicants"] += 1
        by_cat[cat]["flagged"] += bool(pairs)
        by_cat[cat]["need_review"] += bool(nonclear)

        for p in pairs:
            r["pairs"] += 1
            r["pairs_" + p["band"].lower()] += 1
            if p["band"] == "AUTO_CLEAR":
                cleared_ps.append(p["p"])

        hits = name_index.get(name_key(a), set())    # baseline B: exact first+last name
        r["b_flagged"] += bool(hits)

        if is_true:
            linked = t["linked_record_no"]
            r["true_total"] += 1
            r["b_caught"] += linked in hits
            tp = next((p for p in pairs if p["record_no"] == linked), None)
            if tp is None:
                r["true_not_found"] += 1             # search never retrieved the record
            else:
                r["true_retrieved"] += 1
                true_ps.append(tp["p"])
                if tp["band"] == "AUTO_CLEAR":
                    r["true_cleared"] += 1           # the scorer cleared a true match
                else:
                    r["true_caught"] += 1
                    r["true_" + ("escalated" if tp["band"] == "ESCALATE" else "review")] += 1

    flagged, need = r["flagged"], r["need_review"]
    out = {k: int(r[k]) for k in COUNT_KEYS}
    out.update({
        "avoided": flagged - need,
        "auto_clear_rate": ratio(flagged - need, flagged),
        "pair_auto_clear_rate": ratio(r["pairs_auto_clear"], r["pairs"]),
        "wrongly_cleared": r["true_cleared"] + r["true_not_found"],
        "recall": ratio(r["true_caught"], r["true_total"]),
        "precision": ratio(r["true_caught"], need),
        "pair_precision": ratio(r["true_caught"], r["pairs_review"] + r["pairs_escalate"]),
        "baseline_a_recall": ratio(r["true_retrieved"], r["true_total"]),
        "baseline_a_precision": ratio(r["true_retrieved"], flagged),
        "baseline_b_recall": ratio(r["b_caught"], r["true_total"]),
        "baseline_b_precision": ratio(r["b_caught"], r["b_flagged"]),
        "avoided_vs_b": r["b_flagged"] - need,
        "avoided_vs_b_rate": ratio(r["b_flagged"] - need, r["b_flagged"]),
        "lowest_true_p": min(true_ps) if true_ps else None,
        "highest_cleared_p": max(cleared_ps) if cleared_ps else None,
        "by_category": {c: {k: int(by_cat[c][k]) for k in ("applicants", "flagged", "need_review")}
                        for c in CATEGORIES},
    })
    return out


# ============================================================= report writing
def md_table(headers, rows) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return out


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def headline_rows(res: dict) -> list[tuple]:
    g, c, p, a = res["offline"], res["concurrency"], res["payment"], res["audit"]
    delay_ok = (p["avg"] is not None and p["avg"] < DELAY_TARGET_S
                and p["within_5s"] == p["runs"])
    audit_ok = a["coverage"] == 1.0 and a["masking_ok"] and not a["pii_found"]
    n_calls = len(a["steps"])
    n_ok = sum(1 for s in a["steps"] if s["ok"])
    reduction_ok = g["auto_clear_rate"] is not None and g["auto_clear_rate"] >= REDUCTION_TARGET
    return [
        ("Test applications scored (held-out split)", f"{g['applications']:,}", "test split only", "info"),
        ("Flagged by name, baseline A (fuzzy search)", f"{g['flagged']:,}", "-", "info"),
        ("Applications still needing a human, with AI", f"{g['need_review']:,}", "-", "info"),
        ("Auto-clear rate, per application", pct(g["auto_clear_rate"]), "-", "info"),
        ("Auto-clear rate, per candidate pair", pct(g["pair_auto_clear_rate"]), "-", "info"),
        ("Manual reviews avoided vs baseline A",
         f"{g['avoided']:,} of {g['flagged']:,} ({pct(g['auto_clear_rate'])})",
         f">= {REDUCTION_TARGET:.0%}", verdict(reduction_ok)),
        ("Manual reviews avoided vs baseline B (exact first+last name)",
         f"{g['avoided_vs_b']:,} of {g['b_flagged']:,} ({pct(g['avoided_vs_b_rate'])})", "-", "info"),
        ("Precision (applications sent to a human that are true matches)",
         pct(g["precision"]), "no target (trade-off)", "info"),
        ("Recall on true matches", f"{pct(g['recall'])} ({g['true_caught']} of {g['true_total']})",
         "100%", verdict(g["recall"] == 1.0)),
        ("True matches wrongly cleared", str(g["wrongly_cleared"]), "0",
         verdict(g["wrongly_cleared"] == 0)),
        ("Double-booking count", str(c["double_bookings"]), "0",
         verdict(c["double_bookings"] == 0 and c["clean_rounds"] == len(c["rounds"]))),
        ("Average status-update delay", secs(p["avg"]), f"< {DELAY_TARGET_S:.0f} s", verdict(delay_ok)),
        ("Audit-log coverage", f"{pct(a['coverage'])} ({n_ok} of {n_calls} calls)", "100%",
         verdict(audit_ok)),
    ]


def build_findings(res: dict) -> list[tuple]:
    g, c, p, a, an = (res[k] for k in ("offline", "concurrency", "payment", "audit", "analytics"))
    rounds = len(c["rounds"])
    return [
        ("1. HIT verification bottleneck",
         f"Held-out test split: {g['applications']:,} applications, {g['true_total']} true matches. "
         "AI scorer vs fuzzy-name baseline",
         f"At least {REDUCTION_TARGET:.0%} fewer manual reviews; 0 true matches wrongly cleared",
         f"{g['avoided']:,} of {g['flagged']:,} reviews avoided ({pct(g['auto_clear_rate'])}); "
         f"wrongly cleared {g['wrongly_cleared']} of {g['true_total']}"),
        ("2. Slot scarcity and double-booking",
         f"{rounds} rounds of 50 simultaneous requests for a 1-seat slot",
         "Exactly 1 winner per round; 0 double-bookings",
         f"{c['clean_rounds']} of {rounds} rounds clean; double-bookings {c['double_bookings']}"),
        ("3. Poor status visibility",
         f"{p['runs']} simulated payment webhooks; time until the timeline shows the payment",
         f"Under {DELAY_TARGET_S:.0f} s",
         f"Average {secs(p['avg'])}, max {secs(p['max'])}; "
         f"{p['within_5s']} of {p['runs']} within {DELAY_TARGET_S:.0f} s"),
        ("4. Data consistency across services",
         f"The same {p['runs']} webhooks, each resent once (duplicate delivery)",
         "Applied exactly once; PostgreSQL and MongoDB agree; nothing left pending",
         f"{p['consistent']} of {p['runs']} runs consistent"),
        ("5. Limited operational visibility",
         "GET /analytics/summary with a reviewer key",
         "Returns queue size, HIT rate, auto-clear rate, reviews avoided, turnaround",
         "All required fields present" if an["ok"] else "Missing: " + ", ".join(an["missing"])),
        ("6. Privacy and security (RA 10173)",
         f"{len(a['steps'])} API calls (allowed, denied, unauthenticated) plus a front-desk masking probe",
         "100% audit coverage; front desk sees no birthdate or evidence; no personal data or keys in audit rows",
         f"Coverage {pct(a['coverage'])}; masking {'OK' if a['masking_ok'] else 'FAILED'}; "
         f"personal data or keys found in audit rows: {len(a['pii_found'])}"),
    ]


def render_markdown(res: dict) -> str:
    g, c, p, a, an = (res[k] for k in ("offline", "concurrency", "payment", "audit", "analytics"))
    m, d = res["model"], res["data"]
    rows = headline_rows(res)
    failed = [r[0] for r in rows if r[3] == "FAIL"]

    L = ["# PoC evaluation results (SYNTHETIC DATA ONLY)", "",
         f"Generated {res['generated_at']} | data seed {res['seed']} | model {m['version']} "
         f"(trained {m['trained_at']}) | a prototype, not NBI's real system.", ""]
    L.append("**Overall: every checked target passed.**" if not failed
             else "**Overall: NOT every target passed. Failed: " + "; ".join(failed) + ".**")
    L += ["", "Targets were set by this PoC, not by NBI. Results are indicative, not predictive "
          "(see Limitations).", ""]

    L += ["## Headline results", ""]
    L += md_table(["Metric", "Result", "Target", "Status"], rows) + [""]

    L += ["## Findings (for the report)", ""]
    L += md_table(["Pain point", "Test", "Target", "Result"], build_findings(res)) + [""]

    # ------------------------------------------------------------ HIT matching
    L += ["## HIT matching on the held-out test split", "",
          f"The model was trained on the train split only ({m['train_applicants']:,} applicants). "
          f"Here it scored {g['applications']:,} test applicants it never saw, against "
          f"{d['watchlist']:,} watchlist records. Scoring used no labels; the answer key was read "
          "only afterwards, to grade.", ""]
    L += md_table(["Approach", "Manual reviews", "True matches caught", "Recall", "Precision"], [
        ("Baseline A: every fuzzy-name candidate goes to a human", f"{g['flagged']:,}",
         f"{g['true_retrieved']} of {g['true_total']}", pct(g["baseline_a_recall"]),
         pct(g["baseline_a_precision"])),
        ("Baseline B: only an exact first+last name match goes to a human", f"{g['b_flagged']:,}",
         f"{g['b_caught']} of {g['true_total']}", pct(g["baseline_b_recall"]),
         pct(g["baseline_b_precision"])),
        ("AI scorer (a human decides every queued case)", f"{g['need_review']:,}",
         f"{g['true_caught']} of {g['true_total']}", pct(g["recall"]), pct(g["precision"])),
    ]) + [""]
    L += ["Baseline B sends fewer cases to humans than A, but it can only catch a true match whose "
          "first and last name are spelled exactly the same as the watchlist entry, so compare the "
          "recall column. A true match is 'caught' when its own watchlist record is retrieved and "
          "not auto-cleared.", ""]

    L += ["### By applicant category (hidden label, used for grading only)", ""]
    L += md_table(["Category", "Applicants", "Flagged by name", "Needs a human with AI",
                   "Fully auto-cleared"],
                  [(cat, f["applicants"], f["flagged"], f["need_review"], f["flagged"] - f["need_review"])
                   for cat, f in g["by_category"].items()]) + [""]

    ub = g["miss_rate_upper_bound"]
    L += ["### Safety check", "",
          f"- True matches in the test split: **{g['true_total']}**",
          f"- Caught (own record retrieved and not auto-cleared): **{g['true_caught']}** "
          f"({g['true_escalated']} escalated, {g['true_review']} sent to review)",
          f"- Wrongly cleared: **{g['wrongly_cleared']}** "
          f"({g['true_cleared']} auto-cleared by the scorer + {g['true_not_found']} not retrieved by the search)",
          f"- Lowest probability the model gave any retrieved true match: "
          f"{'n/a' if g['lowest_true_p'] is None else format(g['lowest_true_p'], '.4f')}",
          f"- Auto-clear threshold: {m['auto_clear']} (escalate above {m['escalate']})",
          f"- Highest probability among auto-cleared pairs: "
          f"{'n/a' if g['highest_cleared_p'] is None else format(g['highest_cleared_p'], '.4f')}",
          f"- 95% upper bound on the true miss rate: "
          f"{'n/a' if ub is None else format(ub * 100, '.2f') + '%'} "
          "(zero misses in a finite sample does not prove zero risk)",
          f"- Candidate pairs scored: {g['pairs']:,} "
          f"({g['pairs_auto_clear']:,} auto-clear, {g['pairs_review']:,} review, "
          f"{g['pairs_escalate']:,} escalate)",
          f"- Pair-level precision: {pct(g['pair_precision'])}", ""]

    # --------------------------------------------------------------- live checks
    L += ["## Live system checks", "", "### Concurrency: 50 simultaneous requests for 1 seat", ""]
    L += md_table(["Round", "Won (201)", "Slot full (409)", "Other", "Bookings in PostgreSQL", "Seconds"],
                  [(i + 1, r["succeeded"], r["slot_full"], r["other"] or "none",
                    r["booked_in_postgres"], r["seconds"]) for i, r in enumerate(c["rounds"])]) + [""]
    L += [f"- Seats booked beyond capacity: {c['overbooked_seats']}",
          f"- Applications with more than one active booking: {c['apps_multi']}",
          "- Slots over capacity (analytics check): "
          f"{'n/a (Redis unavailable)' if c['overbooked_slots'] is None else c['overbooked_slots']}",
          f"- **Double-booking count: {c['double_bookings']}**", ""]

    L += ["### Payment webhook to timeline", "",
          f"- Runs: {p['runs']} | consistent after a duplicate resend: {p['consistent']}",
          f"- Delay until the MongoDB timeline showed the payment: average {secs(p['avg'])}, "
          f"median {secs(p['median'])}, p95 {secs(p['p95'])}, max {secs(p['max'])}",
          f"- Within {DELAY_TARGET_S:.0f} s: {p['within_5s']} of {p['runs']}"]
    for f in p["failures"][:5]:
        L.append(f"- Problem: {f}")
    L.append("")

    L += ["### Audit-log coverage", "",
          "Each call below was made once. The check counts the audit rows it added.", ""]
    L += md_table(["Call", "Rows expected", "Rows written", "HTTP", "OK"],
                  [(s["label"], s["expected"], s["observed"], s["status"], "yes" if s["ok"] else "NO")
                   for s in a["steps"]]) + [""]
    L += [f"- **Coverage: {pct(a['coverage'])}**",
          f"- Denied or unauthenticated attempts recorded: {a['denied_rows']}",
          f"- Front-desk masking probe: {'OK (no name, birthdate, birthplace, mother or e-mail; no evidence)' if a['masking_ok'] else 'FAILED'}",
          "- Personal data or API keys found in the audit rows written during this run: "
          + ("none" if not a["pii_found"] else ", ".join(a["pii_found"])), ""]

    L += ["### Analytics endpoint", "",
          "- " + ("GET /analytics/summary returned every required section and field: "
                  + ", ".join(an["sections"]) if an["ok"]
                  else "Missing: " + ", ".join(an["missing"])), ""]

    L += ["## Method", "",
          "- Data: synthetic, seed fixed; 10,000 applicants, 2,000 watchlist records; "
          "30% of each category held out as the test split.",
          "- The scorer never sees `ground_truth.csv`. The scoring function takes no labels; "
          "the key is joined only in the grading step.",
          "- The offline scoring calls the same search, feature, and model code as the live API.",
          "- Live checks run in-process against the real databases (no network between caller "
          "and API) and clean up the applications they create. Audit rows are never deleted.",
          f"- Timing: live checks {res['live_seconds']:.0f} s, scoring {res['offline_seconds']:.0f} s.", "",
          "## Limitations", "",
          "- Synthetic names do not reflect real Filipino name distributions; true matches always "
          "keep at least one exact identifier. Real data is messier, so 100% recall here is indicative, not predictive.",
          "- The auto-clear threshold is the 0.20 cap from the project spec. The model's scores for "
          "true matches sit far above it, which shows how easily separable synthetic data is.",
          "- Baselines A and B are stand-ins; NBI's real matching rule is not public.",
          "- Precision is deliberately modest: the system errs toward sending doubtful cases to a human.",
          "- Timing and concurrency were measured in-process on one PC, without a real network, "
          "payment gateway, or production load.",
          "- No integration with NBI systems, payment gateways, or biometric hardware.", ""]
    return "\n".join(L)


# ====================================================================== main
def main() -> None:
    started = time.perf_counter()
    preflight()
    dash_client.make_http = api_client     # dashboard scenario code, run in-process

    print("Part A: live system checks (about a minute)...")
    t0 = time.perf_counter()
    print("  A1: concurrency, 50 simultaneous requests per round")
    concurrency = live_concurrency()
    print(f"  A2: payment webhook sync, {PAYMENT_RUNS} runs")
    payment = live_payment_sync()
    print("  A3: audit-log coverage")
    audit = live_audit_coverage()
    print("  A4: analytics endpoint")
    analytics = live_analytics()
    live_seconds = time.perf_counter() - t0

    print("Part B: HIT matching on the held-out test split (a few minutes)...")
    t1 = time.perf_counter()
    model = load_model()
    wl_rows = read_csv("watchlist.csv")
    wl_by_no = {w["record_no"]: w for w in wl_rows}
    truth = {t["applicant_reference_no"]: t for t in read_csv("ground_truth.csv")}
    applicants = read_csv("applicants.csv")
    test = [a for a in applicants if truth[a["reference_no"]]["split"] == "test"]
    print(f"  {len(test)} test applicants (the model was trained on the other "
          f"{len(applicants) - len(test)})")
    scores = score_applicants(test, wl_by_no, model)            # no labels in here
    g = grade(scores, test, truth, build_name_index(wl_rows))   # labels used only here
    g["miss_rate_upper_bound"] = miss_rate_upper_bound(g["wrongly_cleared"], g["true_total"])
    offline_seconds = time.perf_counter() - t1

    res = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": SEED,
        "model": {"version": model.version, "trained_at": model.data.get("trained_at", "unknown"),
                  "auto_clear": model.auto_clear, "escalate": model.escalate,
                  "train_applicants": model.data.get("training", {}).get("applicants", 0)},
        "data": {"watchlist": len(wl_rows), "applicants": len(applicants),
                 "test_applicants": len(test)},
        "offline": g, "concurrency": concurrency, "payment": payment,
        "audit": audit, "analytics": analytics,
        "live_seconds": live_seconds, "offline_seconds": offline_seconds,
    }
    RESULTS_MD.write_text(render_markdown(res), encoding="utf-8")
    RESULTS_JSON.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")

    rows = headline_rows(res)
    print("\nHeadline results")
    for metric, result, target, status in rows:
        print(f"  [{status:<4}] {metric}: {result}   (target: {target})")
    print(f"\nWrote {RESULTS_MD.name} and {RESULTS_JSON.name} "
          f"({time.perf_counter() - started:.0f} seconds).")
    if any(r[3] == "FAIL" for r in rows):
        print("Some targets FAILED. Open results.md and paste the failing rows into the chat.")
        sys.exit(1)
    print("Every checked target passed.")


if __name__ == "__main__":
    main()