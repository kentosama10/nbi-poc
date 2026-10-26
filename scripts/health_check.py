"""Health check: connects to all four databases and reports OK or FAIL.

Run from the project root:  python scripts/health_check.py
"""
import os
import sys

from dotenv import load_dotenv

# Read the settings in the .env file into environment variables
load_dotenv()


def env(name: str) -> str:
    """Get a setting from .env, with a clear error if it is missing."""
    value = os.getenv(name)
    if value is None:
        raise RuntimeError(f"Missing setting '{name}' in .env")
    return value


def check_postgres() -> str:
    import psycopg2

    conn = psycopg2.connect(
        host="localhost",
        port=int(env("POSTGRES_PORT")),
        user=env("POSTGRES_USER"),
        password=env("POSTGRES_PASSWORD"),
        dbname=env("POSTGRES_DB"),
        connect_timeout=5,
    )
    cur = conn.cursor()
    cur.execute("SELECT version();")
    version = cur.fetchone()[0]
    conn.close()
    return version.split(",")[0]  # e.g. "PostgreSQL 16.4 (Debian ...)"


def check_redis() -> str:
    import redis

    client = redis.Redis(
        host="localhost",
        port=int(env("REDIS_PORT")),
        password=env("REDIS_PASSWORD"),
        socket_connect_timeout=5,
    )
    client.ping()  # raises an error if Redis does not answer
    return "Redis " + client.info()["redis_version"]


def check_mongo() -> str:
    from pymongo import MongoClient

    uri = (
        f"mongodb://{env('MONGO_USER')}:{env('MONGO_PASSWORD')}"
        f"@localhost:{env('MONGO_PORT')}/?authSource=admin"
    )
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return "MongoDB " + client.server_info()["version"]


def check_elasticsearch() -> str:
    from elasticsearch import Elasticsearch

    es = Elasticsearch(f"http://localhost:{env('ES_PORT')}", request_timeout=5)
    version = es.info()["version"]["number"]
    status = es.cluster.health()["status"]
    return f"Elasticsearch {version} (cluster status: {status})"


CHECKS = [
    ("PostgreSQL", check_postgres),
    ("Redis", check_redis),
    ("MongoDB", check_mongo),
    ("Elasticsearch", check_elasticsearch),
]


def main() -> int:
    failures = 0
    for name, check in CHECKS:
        try:
            detail = check()
            print(f"[OK]   {name:<14} {detail}")
        except Exception as exc:  # we want any error shown, not a crash
            failures += 1
            print(f"[FAIL] {name:<14} {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"{failures} service(s) failed. See the troubleshooting list.")
        return 1
    print("All 4 databases are healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())