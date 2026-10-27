"""Create (or refresh) the four demo users. Safe to run repeatedly.

    python -m scripts.seed_users
"""
from sqlalchemy import select

from app import config
from app.auth import hash_key
from app.db import SessionLocal
from app.models import User

DEMO_USERS = [
    ("applicant1", "applicant", config.API_KEY_APPLICANT),
    ("frontdesk1", "frontdesk", config.API_KEY_FRONTDESK),
    ("reviewer1", "reviewer", config.API_KEY_REVIEWER),
    ("admin1", "admin", config.API_KEY_ADMIN),
]


def main() -> None:
    with SessionLocal() as s:
        for username, role, key in DEMO_USERS:
            user = s.scalar(select(User).where(User.username == username))
            if user is None:
                s.add(User(username=username, role=role, api_key_hash=hash_key(key)))
            else:  # refresh, in case .env changed
                user.role, user.api_key_hash, user.is_active = role, hash_key(key), True
        s.commit()
    for username, role, _ in DEMO_USERS:
        print(f"user ready: {username:<11} role={role}")


if __name__ == "__main__":
    main()