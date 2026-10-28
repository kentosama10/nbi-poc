"""NBI Clearance PoC dashboard (SYNTHETIC DATA ONLY).

Run from the project root:   streamlit run dashboard/streamlit_app.py
The API must be running too:  uvicorn app.main:app --reload
"""
import sys
from datetime import datetime
from pathlib import Path

# Make the project root importable (Streamlit only adds this file's folder)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from dashboard import client, scenarios  # noqa: E402
from dashboard.client import call  # noqa: E402

st.set_page_config(page_title="NBI Clearance PoC (synthetic)", layout="wide")

PAGES = ["Applicant status timeline", "Book appointment",
         "Staff review queue", "Analytics"]
CUSTOMER_ROLES = ("applicant", "frontdesk", "admin")
STAFF_ROLES = ("reviewer", "admin")


# ------------------------------------------------------------------ helpers
def fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(iso)


def pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def sec(v) -> str:
    return "n/a" if v is None else f"{v:.1f} s"


def warn_if_role_not_allowed(role: str, allowed: tuple, what: str) -> None:
    if role not in allowed:
        st.warning(f"Your current role is '{role}', which is not allowed to {what}. "
                   "Pressing a button will show the API refusing it (and the audit "
                   "log recording the attempt). Switch role in the sidebar to continue.")


def run_scenario(state_key: str, fn, *args) -> None:
    """Run a scenario, keep its result in session_state, show errors nicely."""
    try:
        with st.spinner("Running..."):
            st.session_state[state_key] = fn(*args)
    except Exception as exc:
        st.session_state.pop(state_key, None)
        st.error(f"{type(exc).__name__}: {exc}")


def render_status(data: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", data["status"])
    c2.metric("View", data["view"])
    pay = data["payment"]
    c3.metric("Payment", pay["status"] if pay else "none")
    c4.metric("Events waiting to sync", data["pending_sync"])
    appt = data["appointment"]
    st.write(f"Appointment: {appt['site']} at {fmt_time(appt['slot_start'])}"
             if appt else "No appointment booked yet.")
    if data["timeline_status"] != "ok":
        st.warning("The timeline store (MongoDB) is unavailable. Showing PostgreSQL facts only.")
    rows = [{"Time (UTC)": fmt_time(e["at"]), "Event": e["type"],
             "Detail": e["detail"], "Source": e["source"]} for e in data["timeline"]]
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No timeline events yet.")


def render_case(item: dict, role: str, prefix: str) -> None:
    """Full evidence, the AI explanation, and the HUMAN decision form."""
    case_id = item["hit_case_id"]
    exp = item["explanation"]
    st.subheader(f"Case #{case_id}: {item['band']} (probability {item['probability']:.3f})")
    w = item["watchlist"]
    st.caption(f"Application {item['application_ref']} compared with watchlist record "
               f"{w['record_no']} | model {item['model_version']} | "
               f"synthetic case ref {w['case_reference']} ({w['offense_category']})")

    a = item["applicant"]
    fields = [("first_name", "First name"), ("middle_name", "Middle name"),
              ("last_name", "Last name"), ("birthdate", "Birthdate"),
              ("birthplace", "Birthplace"), ("mother_maiden_name", "Mother's maiden name")]
    rows = []
    for key, label in fields:
        av, wv = a.get(key) or "", w.get(key) or ""
        rows.append({"Field": label, "Applicant": av, "Watchlist record": wv,
                     "Same?": "same" if av.lower() == wv.lower() else "different"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("**Why the AI gave this score.** Contribution = weight x value. "
                "The contributions plus the intercept add up to the log-odds.")
    feats = pd.DataFrame(exp["features"])[["name", "value", "weight", "contribution"]]
    left, right = st.columns(2)
    left.dataframe(feats, use_container_width=True, hide_index=True)
    right.bar_chart(feats.set_index("name")["contribution"])
    st.write(f"Intercept {exp['intercept']} | log-odds {exp['log_odds']} | "
             f"probability {exp['probability']} | band {exp['band']} "
             f"(auto-clear below {exp['thresholds']['auto_clear']}, "
             f"escalate above {exp['thresholds']['escalate']})")
    if exp["supports_match"]:
        st.write("Evidence supporting a match: " + ", ".join(exp["supports_match"]))
    if exp["missing_evidence"]:
        st.write("Missing or weak evidence: " + ", ".join(exp["missing_evidence"]))

    # The decision is ALWAYS a human's. The API records who made it.
    done_key = f"decided_{case_id}"
    if st.session_state.get(done_key):
        d = st.session_state[done_key]
        st.success(f"Decision recorded: {d['final_decision']} "
                   f"(application is now {d['application_status']}).")
        return
    if item.get("status") != "OPEN":
        st.info("This case is not open (already decided or auto-cleared).")
        return
    with st.form(f"{prefix}_decision_{case_id}"):
        decision = st.radio("Final decision (made by a human)",
                            ["NOT_A_MATCH", "CONFIRMED_MATCH"], horizontal=True)
        note = st.text_input("Note (optional; never put personal data here)", max_chars=500)
        submitted = st.form_submit_button("Record my decision")
    if submitted:
        res = call("POST", f"/review-queue/{case_id}/decision", role,
                   json_body={"decision": decision, "note": note or None})
        if res.ok:
            st.session_state[done_key] = res.data
            st.session_state.pop("queue", None)      # the queue is now out of date
            st.success(f"Decision recorded: {res.data['final_decision']}. "
                       "Reload the queue to see the change.")
        else:
            st.error(res.error)


# -------------------------------------------------------- page 1: status
def page_status(role: str) -> None:
    st.header("Applicant status timeline")
    st.caption("PostgreSQL facts plus the MongoDB timeline. Applicants and front desk "
               "see a PUBLIC status; staff see the internal one.")
    ref = st.text_input("Application reference", value="NBI-000001", key="status_ref")
    if st.button("Look up status"):
        st.session_state["status_res"] = call("GET", f"/applications/{ref.strip()}/status", role)
    res = st.session_state.get("status_res")
    if res is not None:
        render_status(res.data) if res.ok else st.error(res.error)

    st.divider()
    with st.expander("Demo scenario 2: payment webhook updates PostgreSQL and the timeline"):
        st.write("Creates a new synthetic application, requests a payment, plays the payment "
                 "gateway by sending a signed webhook, then measures how long the MongoDB "
                 "timeline takes to show it. It resends the same webhook to prove that "
                 "duplicates change nothing.")
        if st.button("Run scenario 2", key="run_s2"):
            run_scenario("s2", scenarios.scenario2_payment_sync)
        s2 = st.session_state.get("s2")
        if s2:
            ok = s2["delay_seconds"] is not None and s2["delay_seconds"] < 5
            (st.success if ok else st.error)(
                f"Timeline showed the payment after {s2['delay_seconds']} seconds (target: under 5).")
            m1, m2, m3 = st.columns(3)
            m1.metric("Webhook result", s2["webhook"]["result"])
            m2.metric("Same webhook sent again", s2["duplicate"]["result"])
            m3.metric("Application", s2["application_ref"])
            st.caption("Final status as seen by an applicant:")
            render_status(s2["status"])


# ---------------------------------------------------- page 2: book appointment
def page_book(role: str) -> None:
    st.header("Book an appointment")
    st.caption("Seats are counted in Redis by an atomic script. PostgreSQL keeps the permanent record.")
    warn_if_role_not_allowed(role, CUSTOMER_ROLES, "book appointments")

    site_names = {"MNL": "NBI Main - Manila", "QC": "NBI Quezon City", "CEB": "NBI Cebu City"}
    left, right = st.columns(2)
    site = left.selectbox("Site", list(site_names),
                          format_func=lambda c: f"{c} - {site_names[c]}")
    res = call("GET", "/slots", role, params={"site_code": site, "limit": 300})
    if not res.ok:
        st.error(res.error)
    slots = res.data if res.ok else []

    days = sorted({s["start"][:10] for s in slots})
    if days:
        day = right.selectbox("Date", days)
        day_slots = [s for s in slots if s["start"][:10] == day]
        st.dataframe(pd.DataFrame([{
            "Time": s["start"][11:16], "Seats left": s["available"],
            "Capacity": s["capacity"], "Note": "demo: 1-seat slot" if s["demo"] else ""}
            for s in day_slots]), use_container_width=True, hide_index=True)
        by_key = {s["key"]: s for s in day_slots}
        key = st.selectbox("Slot", list(by_key), format_func=lambda k: (
            f"{by_key[k]['start'][11:16]} - {by_key[k]['available']} of "
            f"{by_key[k]['capacity']} seats left"))
        ref = st.text_input("Your application reference", value="NBI-000001", key="book_ref")
        if st.button("Book this slot"):
            r = call("POST", "/appointments", role,
                     json_body={"application_ref": ref.strip(), "slot_key": key})
            if r.ok:
                st.success(f"Booked at {r.data['site']} on {fmt_time(r.data['slot_start'])}. "
                           f"Seats left in that slot: {r.data['seats_left']}.")
            else:
                st.error(r.error)
    elif res.ok:
        st.info("No free slots for this site.")

    st.divider()
    with st.expander("Demo scenario 1: 50 simultaneous requests for the last slot"):
        st.write("Resets the special one-seat slot, then 50 different applications request it "
                 "at the same instant through the real API. Exactly one should win, the other "
                 "49 should be told 'Slot is full', and PostgreSQL should show one booking.")
        if st.button("Run scenario 1", key="run_s1"):
            run_scenario("s1", scenarios.scenario1_concurrency)
        s1 = st.session_state.get("s1")
        if s1:
            ok = (s1["succeeded"] == 1 and s1["booked_in_postgres"] == 1
                  and s1["slot_full"] == s1["requests"] - 1 and not s1["other"])
            (st.success if ok else st.error)(
                "Exactly one booking, zero double-bookings." if ok
                else "Unexpected result. Paste these numbers into the chat.")
            c = st.columns(5)
            c[0].metric("Requests sent", s1["requests"])
            c[1].metric("Succeeded (201)", s1["succeeded"])
            c[2].metric("Slot full (409)", s1["slot_full"])
            c[3].metric("Bookings in PostgreSQL", s1["booked_in_postgres"])
            c[4].metric("Seats left in Redis", s1["seats_left"])
            st.caption(f"Slot {s1['slot_key']} | took {s1['seconds']} s | other responses: {s1['other'] or 'none'}")


# ------------------------------------------------------ page 3: review queue
def page_review(role: str) -> None:
    st.header("Staff review queue")
    st.caption("The AI scores, ranks and explains. A human always makes the final decision.")
    warn_if_role_not_allowed(role, STAFF_ROLES, "see watchlist evidence or decide cases")
    tab_q, tab_h, tab_3, tab_4 = st.tabs(
        ["Queue", "Run a HIT check", "Demo 3: common name", "Demo 4: true match"])

    with tab_q:
        c1, c2 = st.columns(2)
        band = c1.selectbox("Band", ["All", "ESCALATE", "REVIEW"])
        limit = c2.slider("Cases to load", 5, 50, 20)
        if st.button("Load / refresh queue"):
            params = {"limit": limit, **({} if band == "All" else {"band": band})}
            st.session_state["queue"] = call("GET", "/review-queue", role, params=params)
        res = st.session_state.get("queue")
        if res is None:
            st.info("Press 'Load / refresh queue'. Each load is written to the audit log, "
                    "so the page never refreshes by itself.")
        elif not res.ok:
            st.error(res.error)
        else:
            q = res.data
            m = st.columns(3)
            m[0].metric("Open cases", q["open_total"])
            m[1].metric("Escalated", q["by_band"]["ESCALATE"])
            m[2].metric("Needs review", q["by_band"]["REVIEW"])
            items = q["items"]
            if not items:
                st.success("The queue is empty.")
            else:
                st.dataframe(pd.DataFrame([{
                    "Case": i["hit_case_id"], "Application": i["application_ref"],
                    "Band": i["band"], "Probability": i["probability"],
                    "ES score": i["es_score"]} for i in items]),
                    use_container_width=True, hide_index=True)
                by_id = {i["hit_case_id"]: i for i in items}
                chosen = st.selectbox("Open a case", list(by_id), format_func=lambda cid: (
                    f"#{cid} | {by_id[cid]['application_ref']} | {by_id[cid]['band']} | "
                    f"p={by_id[cid]['probability']}"))
                render_case(by_id[chosen], role, "queue")

    with tab_h:
        ref = st.text_input("Application to check", value="NBI-000250", key="hit_ref")
        if st.button("Run HIT check", key="run_hit"):
            st.session_state["hit_res"] = call("POST", f"/applications/{ref.strip()}/hit-check", role)
        res = st.session_state.get("hit_res")
        if res is not None:
            if not res.ok:
                st.error(res.error)
            else:
                d = res.data
                if d["already_checked"]:
                    st.info("This application was already checked (the check is idempotent).")
                c = st.columns(5)
                c[0].metric("Status", d["status"])
                c[1].metric("Candidates", d["candidates_found"])
                c[2].metric("Auto-cleared", d["auto_cleared"])
                c[3].metric("Needs review", d["needs_review"])
                c[4].metric("Escalated", d["escalated"])
                if d["cases"]:
                    st.dataframe(pd.DataFrame(d["cases"]), use_container_width=True, hide_index=True)

    with tab_3:
        st.write("A new synthetic applicant shares the first and last name of a watchlist person "
                 "but has a different birthdate, birthplace and mother. The pipeline flags it by "
                 "name, scores it, and either auto-clears it or queues it with an explanation.")
        if st.button("Run scenario 3", key="run_s3"):
            run_scenario("s3", scenarios.scenario3_common_name)
        s3 = st.session_state.get("s3")
        if s3:
            chk = s3["check"]
            (st.success if s3["outcome"] == "AUTO_CLEARED" else st.warning)(
                f"Flagged by name against '{s3['watchlist_name']}'. Result: "
                + ("every candidate was auto-cleared as a likely namesake."
                   if s3["outcome"] == "AUTO_CLEARED" else "queued for human review."))
            c = st.columns(5)
            c[0].metric("Candidates", chk["candidates_found"])
            c[1].metric("Auto-cleared", chk["auto_cleared"])
            c[2].metric("Needs review", chk["needs_review"])
            c[3].metric("Escalated", chk["escalated"])
            c[4].metric("Public status", s3["public_status"]["status"])
            top = s3["top_case"]
            if top:
                st.markdown("**Highest-scoring candidate and its explanation**")
                st.write(f"Probability {top['probability']} | band {top['band']} | "
                         f"final decision: {top['final_decision'] or 'none (the AI never decides)'}")
                st.dataframe(pd.DataFrame(top["explanation"]["features"]),
                             use_container_width=True, hide_index=True)

    with tab_4:
        st.write("A new synthetic applicant is an exact copy of a watchlist person. The AI "
                 "escalates it. Then YOU act as the human reviewer and record the final decision.")
        if st.button("Run scenario 4", key="run_s4"):
            run_scenario("s4", scenarios.scenario4_true_match)
        s4 = st.session_state.get("s4")
        if s4:
            chk = s4["check"]
            st.warning(f"Application {s4['application_ref']} is {chk['status']}: "
                       f"{chk['escalated']} escalated, {chk['needs_review']} for review.")
            render_case(s4["case"], role, "s4")


# --------------------------------------------------------- page 4: analytics
def render_summary(s: dict) -> None:
    h, q, t = s["hit"], s["queue"], s["turnaround"]["human_review"]
    c = st.columns(4)
    c[0].metric("Queue size (open cases)", q["open_cases"])
    c[1].metric("HIT rate (flagged by name)", pct(h["hit_rate"]))
    c[2].metric("Auto-clear rate", pct(h["auto_clear_rate"]))
    c[3].metric("Manual reviews avoided", h["manual_reviews_avoided"])
    c = st.columns(4)
    c[0].metric("Manual reviews: baseline (AS-IS)", h["manual_reviews_baseline"])
    c[1].metric("Manual reviews: with AI", h["manual_reviews_with_ai"])
    c[2].metric("Avg human review time", sec(t["avg_seconds"]))
    c[3].metric("Median human review time", sec(t["median_seconds"]))
    st.bar_chart(pd.DataFrame({"Manual reviews": [h["manual_reviews_baseline"],
                                                  h["manual_reviews_with_ai"]]},
                              index=["Baseline (AS-IS)", "With AI"]))
    st.caption(f"{h['checked_applications']} applications HIT-checked. "
               f"Snapshot taken {s['generated_at']}. Human review time counts only "
               f"{t['decided_cases']} decided cases (auto-cleared cases have no human wait).")
    a, b = st.columns(2)
    a.markdown("**Applications by status**")
    a.bar_chart(pd.Series(s["applications"]["by_status"]))
    b.markdown("**Open queue by band**")
    b.bar_chart(pd.Series(q["by_band"]))

    bk, sy = s["bookings"], s["sync"]
    na = lambda v: "n/a" if v is None else v
    c = st.columns(5)
    c[0].metric("Booked appointments", bk["booked_appointments"])
    c[1].metric("Apps with 2+ bookings", bk["applications_with_multiple_bookings"])
    c[2].metric("Overbooked slots", na(bk["overbooked_slots"]))
    c[3].metric("Outbox pending", sy["outbox_pending"])
    c[4].metric("Dead letters", sy["dead_letters"])


def render_audit(items: list) -> None:
    rows = [{"ID": i["id"], "Time": fmt_time(i["occurred_at"]), "User": i["username"],
             "Role": i["role"], "Action": i["action"], "Resource": i["resource_type"],
             "Resource ID": i["resource_id"], "Outcome": i["outcome"]} for i in items]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def page_analytics(role: str) -> None:
    st.header("Analytics")
    tab_ops, tab_gov = st.tabs(["Operations", "Governance and audit (Demo 5)"])

    with tab_ops:
        warn_if_role_not_allowed(role, STAFF_ROLES, "read analytics")
        if st.button("Refresh numbers"):
            st.session_state["analytics"] = call("GET", "/analytics/summary", role)
        res = st.session_state.get("analytics")
        if res is None:
            st.info("Press 'Refresh numbers'. Tip: run `python -m scripts.seed_hit_checks` "
                    "first so there is something to show.")
        elif not res.ok:
            st.error(res.error)
        else:
            render_summary(res.data)

    with tab_gov:
        st.markdown("**Demo scenario 5: access control and the audit trail**")
        st.write("The same application is read as front desk and as reviewer. Front desk gets "
                 "a masked view with no birthdate and no watchlist evidence, and is refused the "
                 "review queue. Then the audit log (admin only) shows every attempt.")
        ref = st.text_input("Application reference", value="NBI-000001", key="s5_ref")
        if st.button("Run scenario 5", key="run_s5"):
            run_scenario("s5", scenarios.scenario5_access_control, ref.strip())
        s5 = st.session_state.get("s5")
        if s5:
            c1, c2, c3 = st.columns(3)
            c1.markdown(f"**Front desk** reads the application (HTTP {s5['frontdesk_view'].status})")
            c2.markdown(f"**Reviewer** reads the application (HTTP {s5['reviewer_view'].status})")
            c3.markdown(f"**Front desk** opens the review queue (HTTP {s5['frontdesk_queue'].status})")
            for col, res in ((c1, s5["frontdesk_view"]), (c2, s5["reviewer_view"]),
                             (c3, s5["frontdesk_queue"])):
                if res.ok:
                    col.json(res.data)
                else:
                    col.error(res.error)
            st.markdown("**Newest audit rows (read by an admin):**")
            if s5["audit"].ok:
                render_audit(s5["audit"].data["items"])
            else:
                st.error(s5["audit"].error)

        st.divider()
        st.markdown("**Audit log viewer** (needs the admin role in the sidebar)")
        if st.button("Load the latest 20 audit rows"):
            st.session_state["audit_res"] = call("GET", "/audit-log", role, params={"limit": 20})
        res = st.session_state.get("audit_res")
        if res is not None:
            render_audit(res.data["items"]) if res.ok else st.error(res.error)


# -------------------------------------------------------------------- main
st.sidebar.title("NBI Clearance PoC")
st.sidebar.caption("SYNTHETIC DATA ONLY. A prototype, not NBI's real system.")
page = st.sidebar.radio("Page", PAGES)
role = st.sidebar.selectbox("Signed in as (demo role switcher)", client.ROLES, index=3)
st.sidebar.caption(f"API: {client.API_URL}")

health = call("GET", "/health")        # open endpoint, not audited
if not health.ok:
    st.error(health.error)
    st.stop()

if page == PAGES[0]:
    page_status(role)
elif page == PAGES[1]:
    page_book(role)
elif page == PAGES[2]:
    page_review(role)
else:
    page_analytics(role)