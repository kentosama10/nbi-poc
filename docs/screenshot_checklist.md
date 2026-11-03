# Screenshot checklist

Save PNGs in `docs/screenshots/` using the file names below. Everything on screen is synthetic.

**Before you start:** run `python -m scripts.bootstrap --reload --demo-data`, start the API and the dashboard, and run Scenario 4 once so the analytics show a review time.
**Never capture:** your `.env` file, or any terminal line showing a real password or API key.

| # | File name | What to capture | Must be visible | Report section |
|---|---|---|---|---|
| 1 | `01_containers_healthy.png` | `docker compose ps` | four containers, all `(healthy)` | Setup |
| 2 | `02_tests_passed.png` | `pytest -v` final lines | `81 passed` | Testing |
| 3 | `03_architecture_diagram.png` | The Mermaid diagram rendered in the README | four stores, API, worker, dashboard | Architecture |
| 4 | `04_scenario1_concurrency.png` | Book appointment, Scenario 1 result | green banner; 50 sent, 1 succeeded, 49 full, 1 booking in PostgreSQL, 0 seats left | Pain point 2 |
| 5 | `05_scenario2_payment_sync.png` | Status timeline, Scenario 2 result | green banner with delay in seconds; `applied` then `duplicate`; three timeline events | Pain points 3 and 4 |
| 6 | `06_review_queue_case.png` | Queue tab with one case open | side-by-side evidence table, explanation table, contribution chart, decision form | Pain point 1 |
| 7 | `07_scenario3_common_name.png` | Demo 3 tab result | candidates, auto-cleared count, public status, explanation | Pain point 1 |
| 8 | `08_scenario4_human_decision.png` | Demo 4 tab after recording a decision | ESCALATE case, "Decision recorded" message | Human in the loop |
| 9 | `09_analytics_operations.png` | Analytics, Operations tab | queue size, HIT rate, auto-clear rate, reviews avoided, baseline vs AI chart, review time | Pain point 5 |
| 10 | `10_scenario5_access_control.png` | Analytics, Governance tab after Scenario 5 | masked front-desk view, full reviewer view, HTTP 403 for the queue, audit rows | Pain point 6 |
| 11 | `11_frontdesk_403.png` | Role set to frontdesk, queue page, after pressing Load | the `HTTP 403` error | Pain point 6 |
| 12 | `12_audit_append_only.png` | `docker exec ... psql ... "DELETE FROM audit_log;"` | `audit_log is append-only` error | Pain point 6 |
| 13 | `13_results_headline.png` | `results.md` preview (Ctrl+Shift+V), headline table | every row PASS | Results |
| 14 | `14_results_baselines.png` | `results.md`, baseline comparison table | baseline A, baseline B, AI rows with recall and precision | Results |
| 15 | `15_swagger_docs.png` | <http://127.0.0.1:8000/docs> | the endpoint list | Appendix |

**Tips:** use the same browser zoom for all dashboard shots, crop out your username and folder path if they show personal information, and add a one-line caption under each image in your report ("Figure 4: ...").