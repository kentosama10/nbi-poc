"""The five demo scenarios as plain Python functions (no Streamlit in here).
The dashboard buttons call them, and tests/test_dashboard_scenarios.py runs the
same code. ALL DATA IS SYNTHETIC."""
import threading
import time
import uuid

from dashboard import client, demo_tools
from dashboard.client import call


class ScenarioError(Exception):
    """A scenario could not run; the message is shown to the user."""


DEMO_APPLICANT = {
    "first_name": "Demo", "middle_name": None, "last_name": "Person",
    "birthdate": "1990-01-01", "birthplace": "Demo City",
    "mother_maiden_name": "Demo Mother", "email": "demo@example.com",
    "purpose": "EMPLOYMENT",
}


def _ok(res):
    """Return the response body, or stop the scenario with the API's error."""
    if not res.ok:
        raise ScenarioError(res.error)
    return res.data


def _new_application(**fields) -> str:
    data = _ok(call("POST", "/applicants", "applicant", {**DEMO_APPLICANT, **fields}))
    return data["application_ref"]


# ---------------------------------------------------------------- scenario 1
def scenario1_concurrency(n: int = 50) -> dict:
    """n simultaneous booking requests for the one-seat demo slot."""
    key = demo_tools.reset_demo_slot()
    refs = demo_tools.free_application_refs(n)
    if len(refs) < n:
        raise ScenarioError(f"Only {len(refs)} free applications found. "
                            "Run: python -m scripts.load_data --reload")

    barrier = threading.Barrier(n)       # everyone waits here, then all go at once
    lock = threading.Lock()
    results: list[int] = []

    def worker(ref: str) -> None:
        code, http = -1, None
        try:
            http = client.make_http()    # connect first, so the race starts together
            headers = {"X-API-Key": client.ROLE_KEYS["applicant"]}
            try:
                barrier.wait(timeout=60)
            except threading.BrokenBarrierError:
                pass
            code = http.post("/appointments", headers=headers,
                             json={"application_ref": ref, "slot_key": key}).status_code
        except Exception:
            code = -1
        finally:
            if http is not None:
                http.close()
        with lock:
            results.append(code)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(ref,)) for ref in refs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seconds = time.perf_counter() - started

    return {
        "slot_key": key, "requests": n,
        "succeeded": results.count(201), "slot_full": results.count(409),
        "other": sorted(c for c in results if c not in (201, 409)),
        "booked_in_postgres": demo_tools.booked_count(refs),   # the truth
        "seats_left": int(demo_tools.get_redis().hget(key, "available")),
        "seconds": round(seconds, 2),
    }


# ---------------------------------------------------------------- scenario 2
def scenario2_payment_sync() -> dict:
    """New application -> payment request -> signed webhook -> timeline check."""
    ref = _new_application()
    pay = _ok(call("POST", f"/applications/{ref}/payments", "applicant"))
    event_id = "EVT-DEMO-" + uuid.uuid4().hex[:8]

    started = time.perf_counter()
    hook = _ok(client.send_payment_webhook(pay["payment_ref"], event_id=event_id))

    delay, status = None, None
    while time.perf_counter() - started < 10:
        status = _ok(call("GET", f"/applications/{ref}/status", "applicant"))
        if any(e["type"] == "PAYMENT_RECEIVED" for e in status["timeline"]):
            delay = time.perf_counter() - started
            break
        time.sleep(0.1)

    # The gateway resends the same event: it must change nothing
    duplicate = _ok(client.send_payment_webhook(pay["payment_ref"], event_id=event_id))
    return {"application_ref": ref, "payment_ref": pay["payment_ref"],
            "webhook": hook, "duplicate": duplicate,
            "delay_seconds": None if delay is None else round(delay, 3),
            "status": status}


# ---------------------------------------------------------------- scenario 3
def scenario3_common_name() -> dict:
    """Same first and last name as a watchlist person, but a different person."""
    rec = demo_tools.common_name_record()
    other_date = "1999-12-31" if rec["birthdate"][:4] != "1999" else "1980-06-15"
    ref = _new_application(first_name=rec["first_name"], last_name=rec["last_name"],
                           middle_name=None, birthdate=other_date,
                           birthplace="Unlisted Town", mother_maiden_name="Maria Unrelated")
    check = _ok(call("POST", f"/applications/{ref}/hit-check", "reviewer"))
    top = check["cases"][0] if check["cases"] else None
    detail = (_ok(call("GET", f"/hit-cases/{top['hit_case_id']}", "reviewer"))
              if top else None)
    public = _ok(call("GET", f"/applications/{ref}/status", "applicant"))
    queued = (check["needs_review"] + check["escalated"]) > 0
    return {"application_ref": ref,
            "watchlist_name": f"{rec['first_name']} {rec['last_name']}",
            "check": check, "top_case": detail, "public_status": public,
            "outcome": "QUEUED" if queued else "AUTO_CLEARED"}


# ---------------------------------------------------------------- scenario 4
def scenario4_true_match() -> dict:
    """An applicant identical to a watchlist person -> escalated. The human
    decision is made afterwards, in the dashboard (or by a test)."""
    rec = demo_tools.common_name_record()
    ref = _new_application(**rec)
    check = _ok(call("POST", f"/applications/{ref}/hit-check", "reviewer"))
    queue = _ok(call("GET", "/review-queue", "reviewer",
                     params={"application_ref": ref}))
    if not queue["items"] or queue["items"][0]["band"] != "ESCALATE":
        raise ScenarioError("The exact copy was not escalated. Paste the model "
                            "weights into the chat so we can diagnose it.")
    return {"application_ref": ref, "check": check, "case": queue["items"][0]}


# ---------------------------------------------------------------- scenario 5
def scenario5_access_control(ref: str = "NBI-000001") -> dict:
    """Front desk vs reviewer on the same application, then read the audit log."""
    front = call("GET", f"/applications/{ref}", "frontdesk")
    review = call("GET", f"/applications/{ref}", "reviewer")
    front_queue = call("GET", "/review-queue", "frontdesk")      # must be refused
    audit = call("GET", "/audit-log", "admin", params={"limit": 10})
    return {"application_ref": ref, "frontdesk_view": front,
            "reviewer_view": review, "frontdesk_queue": front_queue, "audit": audit}