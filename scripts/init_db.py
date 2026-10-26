"""Create all PostgreSQL tables (and the audit-log protection trigger).

Run from the project root:
    python -m scripts.init_db            (create what is missing)
    python -m scripts.init_db --reset    (DELETE all tables, then recreate)
"""
import sys

from sqlalchemy import inspect, text

import app.models  # noqa: F401  (importing registers every table on Base)
from app.db import Base, engine

# The audit log must be append-only: any UPDATE or DELETE raises an error.
# (Plain SQL, run once. CREATE OR REPLACE / DROP IF EXISTS make it safe to repeat.)
AUDIT_PROTECTION_SQL = [
    """
    CREATE OR REPLACE FUNCTION block_audit_changes() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only: no updates or deletes';
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;",
    """
    CREATE TRIGGER audit_log_no_change
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION block_audit_changes();
    """,
]


def main() -> None:
    if "--reset" in sys.argv:
        print("Dropping all tables...")
        Base.metadata.drop_all(engine)

    # Creates only the tables that do not exist yet
    Base.metadata.create_all(engine)

    # engine.begin() opens a transaction and commits it at the end
    with engine.begin() as conn:
        for statement in AUDIT_PROTECTION_SQL:
            conn.execute(text(statement))

    inspector = inspect(engine)
    print("Tables in PostgreSQL:")
    for table in sorted(inspector.get_table_names()):
        print(f"  {table:<20} {len(inspector.get_columns(table))} columns")


if __name__ == "__main__":
    main()