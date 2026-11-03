# Architecture

All data is synthetic. This is a prototype, not NBI's real system.

## 1. Components

```mermaid
flowchart LR
  subgraph clients["Clients"]
    ST["Streamlit dashboard"]
    GW["Payment gateway - simulated"]
  end
  API["FastAPI service<br/>API-key roles, masking, audit"]
  ML["AI scorer<br/>rapidfuzz + logistic regression"]
  WK["Outbox worker"]
  subgraph stores["Four stores, one job each"]
    PG[("PostgreSQL 16<br/>source of truth")]
    RD[("Redis 7<br/>atomic slot counters")]
    MG[("MongoDB 7<br/>status timeline")]
    ES[("Elasticsearch 8<br/>fuzzy name search")]
  end
  ST -->|"HTTP + API key"| API
  GW -->|"signed webhook"| API
  API -->|"ACID transactions + audit_log"| PG
  API -->|"Lua script: take a seat"| RD
  API -->|"candidate search"| ES
  API --> ML
  API -->|"read timeline"| MG
  PG -->|"outbox rows"| WK
  WK -->|"append events"| MG
```

## 2. Payment webhook: keeping PostgreSQL and MongoDB consistent

```mermaid
sequenceDiagram
  participant G as Gateway
  participant A as API
  participant P as PostgreSQL
  participant M as MongoDB
  G->>A: POST /webhooks/payment with HMAC signature
  A->>A: verify the signature, reject forgeries with 401
  A->>P: lock the payment row
  A->>P: ONE transaction: payment PAID, application PAID, outbox row
  P-->>A: commit
  A->>M: deliver the outbox event, idempotent by outbox_id
  alt MongoDB is down
    A-->>G: 200 with timeline_synced false
    Note over P,M: the outbox row stays and the worker retries every 2 s
  else MongoDB is up
    A-->>G: 200 with timeline_synced true
  end
```

## 3. Booking the last seat

```mermaid
sequenceDiagram
  participant C as 50 simultaneous clients
  participant A as API
  participant R as Redis
  participant P as PostgreSQL
  C->>A: POST /appointments for the same slot
  A->>R: Lua script: if seats left then take one
  R-->>A: one request gets the seat, 49 get slot full
  A->>P: ONE transaction: appointment + outbox row
  alt PostgreSQL write fails
    A->>R: give the seat back, compensation
  end
  Note over P: a partial unique index allows only one BOOKED appointment per application
```

## 4. HIT pipeline

```mermaid
flowchart TD
  A["Application submitted"] --> B["Elasticsearch fuzzy surname search<br/>top 50 candidates"]
  B --> C["8 similarity features per candidate"]
  C --> D["Logistic regression gives a probability"]
  D --> E{"Probability"}
  E -->|"below 0.20"| F["AUTO_CLEAR<br/>stored with its explanation"]
  E -->|"0.20 to 0.80"| G["REVIEW queue with evidence"]
  E -->|"above 0.80"| H["ESCALATE to senior reviewer"]
  G --> I["A human records the final decision"]
  H --> I
```

## 5. Failure handling

| What fails | What happens | Where it is tested |
|---|---|---|
| PostgreSQL write fails after Redis took a seat | The seat is released immediately | `tests/test_booking.py` |
| Server crashes between the Redis step and the commit | A seat is lost in Redis. `reconcile_slots` repairs it from PostgreSQL | `tests/test_booking.py` |
| MongoDB is down during a webhook | PostgreSQL stays correct, the outbox row stays, the worker retries, status shows `pending_sync` | `tests/test_payment_webhook.py` |
| The gateway resends a webhook, even 10 at once | Applied exactly once (event id is unique, row is locked) | `tests/test_payment_webhook.py` |
| Elasticsearch is down during a HIT check | The check returns 503 and nothing is written | `app/hit_check.py` |
| The audit row cannot be written | The request fails and no data is returned (fail closed) | `tests/test_governance.py` |
| Someone tries to edit the audit log | A database trigger refuses the UPDATE or DELETE | `tests/test_schema.py` |
| A decision is recorded without a human user | A database CHECK constraint rejects it | `tests/test_schema.py` |

## 6. Design decisions worth defending

- **PostgreSQL is the truth.** Redis counters and MongoDB timelines are rebuildable working copies.
- **Outbox pattern.** The business change and the event are saved in one transaction, so an event is never lost and never invented.
- **Human in the loop, enforced three ways.** The AI code path never writes a final decision, the decision endpoint needs a reviewer or admin key, and the database rejects a decision without a user id.
- **Privacy by design.** Elasticsearch holds names only, applicants and front desk get a public status, front desk gets a masked view, API keys are stored hashed, and the audit log holds no personal data.
- **The ground-truth answer key lives only in a CSV.** It is never loaded into any database and is joined only when grading.