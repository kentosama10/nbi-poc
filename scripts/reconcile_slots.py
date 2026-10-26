"""Compare Redis seat counters with PostgreSQL bookings.

    python -m scripts.reconcile_slots          (report only)
    python -m scripts.reconcile_slots --fix    (repair Redis from PostgreSQL)
"""
import sys

from app.booking import reconcile_slots
from app.db import SessionLocal


def main() -> None:
    fix = "--fix" in sys.argv
    with SessionLocal() as session:
        mismatches = reconcile_slots(session, fix=fix)
    if not mismatches:
        print("Redis seat counters match PostgreSQL bookings (0 mismatches).")
        return
    for m in mismatches:
        print(f"{m['key']}: Redis says {m['actual']} seats left, "
              f"PostgreSQL implies {m['expected']}")
    print("Repaired." if fix else "Run again with --fix to repair Redis.")


if __name__ == "__main__":
    main()