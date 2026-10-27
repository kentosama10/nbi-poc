"""Minimal API-key authentication. Phase 8 adds masking and the audit log."""
import hashlib

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import User


def hash_key(key: str) -> str:
    """SHA-256 hash of an API key. Only the hash is stored in the database."""
    return hashlib.sha256(key.encode()).hexdigest()


def get_current_user(
    x_api_key: str | None = Header(default=None),    # header "X-API-Key"
    session: Session = Depends(get_session),
) -> User:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    user = session.scalar(select(User).where(
        User.api_key_hash == hash_key(x_api_key), User.is_active.is_(True)))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


def require_roles(*allowed: str):
    """Build a dependency that only lets the listed roles through (else 403)."""
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403,
                                detail="Your role is not allowed to do this")
        return user
    return checker