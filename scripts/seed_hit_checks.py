"""Run the HIT check on the first N unchecked applications (DEMO data only).

    python -m scripts.seed_hit_checks              (200 applications)
    python -m scripts.seed_hit_checks --limit 500

It calls the same pipeline as the API, never reads ground_truth.csv, and records
NO decisions (only a human reviewer does that).
"""
import sys

from sqlalchemy import select

from app.analytics import build_summary
from app.db import SessionLocal
from app.hit_check import HitCheckError, run_hit_check
from app.models import Application
from app.timeline import deliver_pending


def main() -> None:
    limit = 200
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    with SessionLocal() as s:
        refs = list(s.scalars(
            select(Application.reference_no)
            .where(Application.status == "SUBMITTED")
            .order_by(Application.id).limit(limit)))
    print(f"Running the HIT check on {len(refs)} applications...")

    done = 0
    for ref in refs:
        with SessionLocal() as s:          # one short session per application
            try:
                run_hit_check(s, ref)
                done += 1
            except HitCheckError as exc:
                sys.exit(f"{ref}: {exc.message}")
        if done % 50 == 0:
            print(f"  {done} done...")

    # Copy the new status events to the MongoDB timelines (the worker would do this)
    delivered = 0
    while True:
        result = deliver_pending(limit=500)
        delivered += result["delivered"]
        if result["delivered"] == 0:
            break
    print(f"Delivered {delivered} timeline events to MongoDB.")

    with SessionLocal() as s:
        h = build_summary(s)["hit"]
    print()
    print(f"HIT-checked applications : {h['checked_applications']}")
    print(f"Flagged by name (baseline manual reviews): {h['flagged_by_name']}")
    print(f"Still need a human with the AI           : {h['manual_reviews_with_ai']}")
    print(f"Manual reviews avoided                   : {h['manual_reviews_avoided']}"
          f"  (auto-clear rate {h['auto_clear_rate']})")


if __name__ == "__main__":
    main()