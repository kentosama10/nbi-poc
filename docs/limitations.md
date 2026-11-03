# Limitations (stated honestly)

## Data
- **Synthetic data does not reflect real Filipino name distributions.** Common surnames were over-sampled to create the HIT problem, but real naming patterns are richer and messier.
- Every synthetic true match keeps **at least one exact identifier** (birthdate, birthplace, or mother's maiden name). Real records can be worse, so 100% recall here is indicative, not predictive.
- Names, birthplaces, and dates are generated, so the model has seen only the kinds of variation the generator produces (typos, spelling variants, swapped day and month).

## Model and evaluation
- Zero misses in 150 test true matches means the true miss rate is **probably under about 2%** (95% upper bound), not zero.
- The auto-clear threshold is the spec's **0.20 cap**. True matches score far above it, which shows the synthetic data is easy to separate, not that the model is excellent.
- Probabilities are not calibrated (the training uses class weighting).
- Precision is modest (52.1%) by design: the system errs toward sending doubtful cases to a human.
- Baselines A and B are stand-ins. **NBI's real matching rule is not public.**
- The 50% reduction and 5-second targets were set by this PoC, not by NBI.

## Architecture
- PostgreSQL does not itself enforce per-slot capacity. Redis does, and reconciliation repairs a lost seat (run it when the system is quiet).
- Timeline order is by event time, not delivery order. Events that fail 10 times become dead letters that need a human.
- The webhook delivers the outbox inline. A high-volume system would use a message queue.
- A real gateway would also send a timestamp that we would check to block replay attacks.
- Booking does not require payment in this PoC.

## Security and privacy
- API keys belong to **roles, not people**. Any applicant key can read any application's public status.
- The dashboard's role switcher **is not a login**.
- Keys travel as plain headers over HTTP on localhost. Production needs HTTPS, key rotation, per-person accounts, and rate limiting.
- Demo passwords and keys are committed in `.env.example` on purpose (fake, local only). Never reuse them.
- The database owner could still `TRUNCATE` the audit table. Production would revoke that and ship logs to write-once storage.
- Elasticsearch runs with **security disabled** (local demo only).
- The PoC applies privacy-by-design ideas relevant to RA 10173 but is **not** a legal or compliance assessment.

## Measurement
- Timing and concurrency were measured in-process on one PC, with no real network, payment gateway, or production load.
- Analytics are point-in-time snapshots without date filters, and they scan whole tables (fine at 10,000 rows).
- Test runs leave audit rows behind on purpose, because the log is append-only.

## Scope
- No integration with NBI systems, payment gateways, or biometric hardware.
- Results are **indicative, not predictive**.