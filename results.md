# PoC evaluation results (SYNTHETIC DATA ONLY)

Generated 2026-10-28T08:37:33+00:00 | data seed 42 | model logreg-v1 (trained 2026-10-27T03:04:41+00:00) | a prototype, not NBI's real system.

**Overall: every checked target passed.**

Targets were set by this PoC, not by NBI. Results are indicative, not predictive (see Limitations).

## Headline results

| Metric | Result | Target | Status |
|---|---|---|---|
| Test applications scored (held-out split) | 3,000 | test split only | info |
| Flagged by name, baseline A (fuzzy search) | 2,813 | - | info |
| Applications still needing a human, with AI | 288 | - | info |
| Auto-clear rate, per application | 89.8% | - | info |
| Auto-clear rate, per candidate pair | 99.6% | - | info |
| Manual reviews avoided vs baseline A | 2,525 of 2,813 (89.8%) | >= 50% | PASS |
| Manual reviews avoided vs baseline B (exact first+last name) | 823 of 1,111 (74.1%) | - | info |
| Precision (applications sent to a human that are true matches) | 52.1% | no target (trade-off) | info |
| Recall on true matches | 100.0% (150 of 150) | 100% | PASS |
| True matches wrongly cleared | 0 | 0 | PASS |
| Double-booking count | 0 | 0 | PASS |
| Average status-update delay | 0.060 s | < 5 s | PASS |
| Audit-log coverage | 100.0% (24 of 24 calls) | 100% | PASS |

## Findings (for the report)

| Pain point | Test | Target | Result |
|---|---|---|---|
| 1. HIT verification bottleneck | Held-out test split: 3,000 applications, 150 true matches. AI scorer vs fuzzy-name baseline | At least 50% fewer manual reviews; 0 true matches wrongly cleared | 2,525 of 2,813 reviews avoided (89.8%); wrongly cleared 0 of 150 |
| 2. Slot scarcity and double-booking | 5 rounds of 50 simultaneous requests for a 1-seat slot | Exactly 1 winner per round; 0 double-bookings | 5 of 5 rounds clean; double-bookings 0 |
| 3. Poor status visibility | 20 simulated payment webhooks; time until the timeline shows the payment | Under 5 s | Average 0.060 s, max 0.231 s; 20 of 20 within 5 s |
| 4. Data consistency across services | The same 20 webhooks, each resent once (duplicate delivery) | Applied exactly once; PostgreSQL and MongoDB agree; nothing left pending | 20 of 20 runs consistent |
| 5. Limited operational visibility | GET /analytics/summary with a reviewer key | Returns queue size, HIT rate, auto-clear rate, reviews avoided, turnaround | All required fields present |
| 6. Privacy and security (RA 10173) | 24 API calls (allowed, denied, unauthenticated) plus a front-desk masking probe | 100% audit coverage; front desk sees no birthdate or evidence; no personal data or keys in audit rows | Coverage 100.0%; masking OK; personal data or keys found in audit rows: 0 |

## HIT matching on the held-out test split

The model was trained on the train split only (7,000 applicants). Here it scored 3,000 test applicants it never saw, against 2,000 watchlist records. Scoring used no labels; the answer key was read only afterwards, to grade.

| Approach | Manual reviews | True matches caught | Recall | Precision |
|---|---|---|---|---|
| Baseline A: every fuzzy-name candidate goes to a human | 2,813 | 150 of 150 | 100.0% | 5.3% |
| Baseline B: only an exact first+last name match goes to a human | 1,111 | 51 of 150 | 34.0% | 4.6% |
| AI scorer (a human decides every queued case) | 288 | 150 of 150 | 100.0% | 52.1% |

Baseline B sends fewer cases to humans than A, but it can only catch a true match whose first and last name are spelled exactly the same as the watchlist entry, so compare the recall column. A true match is 'caught' when its own watchlist record is retrieved and not auto-cleared.

### By applicant category (hidden label, used for grading only)

| Category | Applicants | Flagged by name | Needs a human with AI | Fully auto-cleared |
|---|---|---|---|---|
| TRUE_MATCH | 150 | 150 | 150 | 0 |
| NAMESAKE | 750 | 750 | 130 | 620 |
| UNRELATED | 2100 | 1913 | 8 | 1905 |

### Safety check

- True matches in the test split: **150**
- Caught (own record retrieved and not auto-cleared): **150** (150 escalated, 0 sent to review)
- Wrongly cleared: **0** (0 auto-cleared by the scorer + 0 not retrieved by the search)
- Lowest probability the model gave any retrieved true match: 0.9886
- Auto-clear threshold: 0.2 (escalate above 0.8)
- Highest probability among auto-cleared pairs: 0.1838
- 95% upper bound on the true miss rate: 1.98% (zero misses in a finite sample does not prove zero risk)
- Candidate pairs scored: 67,236 (66,948 auto-clear, 86 review, 202 escalate)
- Pair-level precision: 52.1%

## Live system checks

### Concurrency: 50 simultaneous requests for 1 seat

| Round | Won (201) | Slot full (409) | Other | Bookings in PostgreSQL | Seconds |
|---|---|---|---|---|---|
| 1 | 1 | 49 | none | 1 | 4.37 |
| 2 | 1 | 49 | none | 1 | 2.24 |
| 3 | 1 | 49 | none | 1 | 2.22 |
| 4 | 1 | 49 | none | 1 | 2.24 |
| 5 | 1 | 49 | none | 1 | 2.25 |

- Seats booked beyond capacity: 0
- Applications with more than one active booking: 0
- Slots over capacity (analytics check): 0
- **Double-booking count: 0**

### Payment webhook to timeline

- Runs: 20 | consistent after a duplicate resend: 20
- Delay until the MongoDB timeline showed the payment: average 0.060 s, median 0.050 s, p95 0.066 s, max 0.231 s
- Within 5 s: 20 of 20

### Audit-log coverage

Each call below was made once. The check counts the audit rows it added.

| Call | Rows expected | Rows written | HTTP | OK |
|---|---|---|---|---|
| POST /applicants (applicant) | 1 | 1 | 201 | yes |
| POST /applications/{ref}/payments (applicant) | 1 | 1 | 201 | yes |
| POST /webhooks/payment, valid signature | 1 | 1 | 200 | yes |
| POST /webhooks/payment, forged signature | 1 | 1 | 401 | yes |
| GET /applications/{ref}/status (applicant) | 1 | 1 | 200 | yes |
| GET /applications/{ref}/status (frontdesk) | 1 | 1 | 200 | yes |
| GET /applications/{ref}/status (reviewer) | 1 | 1 | 200 | yes |
| GET /applications/{ref} (frontdesk, masked) | 1 | 1 | 200 | yes |
| GET /applications/{ref} (reviewer, full) | 1 | 1 | 200 | yes |
| POST /applications/{ref}/hit-check (reviewer) | 1 | 1 | 200 | yes |
| GET /review-queue (reviewer) | 1 | 1 | 200 | yes |
| GET /hit-cases/999999999 (reviewer) | 1 | 1 | 404 | yes |
| POST /review-queue/999999999/decision (reviewer) | 1 | 1 | 404 | yes |
| GET /analytics/summary (reviewer) | 1 | 1 | 200 | yes |
| GET /audit-log (admin) | 1 | 1 | 200 | yes |
| POST /appointments, unknown slot (applicant) | 1 | 1 | 404 | yes |
| GET /review-queue (frontdesk) -> 403 | 1 | 1 | 403 | yes |
| POST /applications/{ref}/hit-check (applicant) -> 403 | 1 | 1 | 403 | yes |
| GET /analytics/summary (frontdesk) -> 403 | 1 | 1 | 403 | yes |
| GET /audit-log (reviewer) -> 403 | 1 | 1 | 403 | yes |
| GET /review-queue (no key) -> 401 | 1 | 1 | 401 | yes |
| GET /audit-log (wrong key) -> 401 | 1 | 1 | 401 | yes |
| GET /health (open, not audited) | 0 | 0 | 200 | yes |
| GET /slots (any key, not audited) | 0 | 0 | 200 | yes |

- **Coverage: 100.0%**
- Denied or unauthenticated attempts recorded: 7
- Front-desk masking probe: OK (no name, birthdate, birthplace, mother or e-mail; no evidence)
- Personal data or API keys found in the audit rows written during this run: none

### Analytics endpoint

- GET /analytics/summary returned every required section and field: applications, bookings, generated_at, hit, payments, queue, sync, turnaround

## Method

- Data: synthetic, seed fixed; 10,000 applicants, 2,000 watchlist records; 30% of each category held out as the test split.
- The scorer never sees `ground_truth.csv`. The scoring function takes no labels; the key is joined only in the grading step.
- The offline scoring calls the same search, feature, and model code as the live API.
- Live checks run in-process against the real databases (no network between caller and API) and clean up the applications they create. Audit rows are never deleted.
- Timing: live checks 16 s, scoring 57 s.

## Limitations

- Synthetic names do not reflect real Filipino name distributions; true matches always keep at least one exact identifier. Real data is messier, so 100% recall here is indicative, not predictive.
- The auto-clear threshold is the 0.20 cap from the project spec. The model's scores for true matches sit far above it, which shows how easily separable synthetic data is.
- Baselines A and B are stand-ins; NBI's real matching rule is not public.
- Precision is deliberately modest: the system errs toward sending doubtful cases to a human.
- Timing and concurrency were measured in-process on one PC, without a real network, payment gateway, or production load.
- No integration with NBI systems, payment gateways, or biometric hardware.
