"""Central settings, read from the .env file. Never hard-code secrets."""
import os

from dotenv import load_dotenv

# Finds the .env file in the project root and loads it into environment variables
load_dotenv()


def _get(name: str) -> str:
    """Return a setting, or stop with a clear message if it is missing."""
    value = os.getenv(name)
    if value is None:
        raise RuntimeError(f"Missing setting '{name}' in .env")
    return value


# PostgreSQL
POSTGRES_HOST = "localhost"
POSTGRES_PORT = int(_get("POSTGRES_PORT"))
POSTGRES_USER = _get("POSTGRES_USER")
POSTGRES_PASSWORD = _get("POSTGRES_PASSWORD")
POSTGRES_DB = _get("POSTGRES_DB")

# Redis
REDIS_HOST = "localhost"
REDIS_PORT = int(_get("REDIS_PORT"))
REDIS_PASSWORD = _get("REDIS_PASSWORD")

# MongoDB
MONGO_URI = (
    f"mongodb://{_get('MONGO_USER')}:{_get('MONGO_PASSWORD')}"
    f"@localhost:{_get('MONGO_PORT')}/?authSource=admin"
)

# Elasticsearch
ES_URL = f"http://localhost:{_get('ES_PORT')}"

# Shared secret between us and the (simulated) payment gateway
WEBHOOK_SECRET = _get("WEBHOOK_SECRET")

# Demo API keys, one per role (synthetic prototype only)
API_KEY_APPLICANT = _get("API_KEY_APPLICANT")
API_KEY_FRONTDESK = _get("API_KEY_FRONTDESK")
API_KEY_REVIEWER = _get("API_KEY_REVIEWER")
API_KEY_ADMIN = _get("API_KEY_ADMIN")