# 5-minute demo script

All data is synthetic. Total time: **5:00**. Rehearse it twice with a timer.

## Before the demo (2 minutes ahead)

```powershell
python -m scripts.bootstrap --reload --demo-data   # fresh data and a filled review queue
uvicorn app.main:app                               # terminal 1
python -m scripts.outbox_worker                    # terminal 2 (optional)
streamlit run dashboard/streamlit_app.py           # terminal 3
```
Open <http://localhost:8501>. Leave the sidebar role on **admin**. Open `results.md` in another tab.

## Script

| Time | Where | Do | Say |
|---|---|---|---|
| 0:00 to 0:30 | Slide or README | Show the architecture diagram | "NBI flags applicants by name, so common names go to manual review. In our synthetic test, 94% of applicants were flagged but only 5% were real matches. Four databases, one job each, plus an AI scorer. The AI only scores and explains. A human always decides." |
| 0:30 to 1:15 | **Book appointment**, expander **Scenario 1** | Press **Run scenario 1** | "50 people request the last seat at the same instant. Exactly one wins, 49 are told the slot is full, and PostgreSQL shows one booking. Redis takes the seat in one atomic step, and PostgreSQL's unique index is the safety net." Point at 1 / 49 / 1 / 0. |
| 1:15 to 2:00 | **Applicant status timeline**, expander **Scenario 2** | Press **Run scenario 2** | "A signed payment webhook updates PostgreSQL and the MongoDB timeline in under a second. We resend the same webhook and nothing changes. If MongoDB were down, an outbox row would retry it later." Point at the green banner and `duplicate`. |
| 2:00 to 3:00 | **Staff review queue**, tab **Demo 3** | Press **Run scenario 3** | "A different person with the same name as a watchlist person. Search flags it by name, the model scores it, and it is auto-cleared or queued with an explanation. Each feature's value, weight, and contribution is shown." Show the explanation table. |
| 3:00 to 3:45 | Tab **Demo 4** | Press **Run scenario 4**, choose a decision, press **Record my decision** | "An exact copy of a watchlist person is escalated. The AI does not decide. I am the reviewer, and my user id is stored with the decision. The database would refuse it without one." |
| 3:45 to 4:30 | **Analytics**, tab **Governance and audit (Demo 5)** | Press **Run scenario 5** | "Front desk sees a masked name and no birthdate or evidence, and is refused the queue with a 403. Reviewer sees everything. The admin audit log shows every attempt, allowed or denied." |
| 4:30 to 5:00 | **Analytics**, tab **Operations**, then `results.md` | Press **Refresh numbers**, then show the headline table | "Baseline sends 2,813 of 3,000 test applicants to a human. With the AI, 288, so 89.8% avoided. Recall on true matches is 100%, 150 of 150, and 0 wrongly cleared. These are synthetic results, indicative, not predictive." |

## If something goes wrong

| Problem | Quick fix |
|---|---|
| Dashboard says it cannot reach the API | Start `uvicorn app.main:app` in its own terminal |
| Scenario 1 says too few free applications | `python -m scripts.bootstrap --reload` |
| Review queue is empty | `python -m scripts.seed_hit_checks`, then press **Load / refresh queue** |
| Analytics shows n/a for review time | Expected until a human decides a case (do Scenario 4 first) |
| `401` errors | `python -m scripts.seed_users` |

## Questions to expect

- **"Why not clear more automatically?"** Safety first. We tune for zero wrongly cleared true matches, then count the reviews avoided.
- **"Is 100% recall real?"** On synthetic data, with 0 misses in 150, the 95% upper bound on the true miss rate is about 2%. Real data is messier.
- **"Why a baseline B?"** Exact-name matching is cheaper but misses mistyped names. The recall column in `results.md` shows it.
- **"Does the AI decide?"** No. It scores, ranks, and explains. The decision endpoint requires a human reviewer, and the database enforces it.