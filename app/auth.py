"""API-key authentication, role checks, and audit logging at the gate."""
import hashlib

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import log_event
from app.db import get_session
from app.models import User


def hash_key(key: str) -> str:
    """SHA-256 hash of an API key. Only the hash is stored in the database."""
    return hashlib.sha256(key.encode()).hexdigest()


def _label(request: Request) -> str:
    return f"{request.method} {request.url.path}"


def _authenticate(request: Request, x_api_key: str | None,
                  session: Session) -> User:
    """Return the active user for this key, or log the failure and raise 401.
    The key itself is never logged."""
    if not x_api_key:
        log_event("anonymous", "none", "AUTHENTICATE", "endpoint",
                  _label(request), "DENIED", {"reason": "missing_key"})
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    user = session.scalar(select(User).where(
        User.api_key_hash == hash_key(x_api_key), User.is_active.is_(True)))
    session.commit()          # ends the read transaction: the connection goes back to the pool
    if user is None:
        log_event("unknown", "none", "AUTHENTICATE", "endpoint",
                  _label(request), "DENIED", {"reason": "invalid_key"})
        raise HTTPException(status_code=401, detail="Invalid API key")
    session.expunge(user)     # detach, so a later rollback cannot expire its fields
    return user


def get_current_user(
    request: Request,
    x_api_key: str | None = Header(default=None),      # header "X-API-Key"
    session: Session = Depends(get_session),
) -> User:
    """Any valid key is enough (used for non-sensitive endpoints like /slots)."""
    return _authenticate(request, x_api_key, session)


def guard(action: str, resource_type: str, roles: tuple[str, ...],
          id_param: str | None = None):
    """Build a dependency that: authenticates, checks the role, writes ONE audit
    row (ALLOWED or DENIED), then lets the request through or returns 403.

    id_param: name of the path parameter to record as the resource id
    (for example 'application_ref' or 'hit_case_id')."""
    def dependency(
        request: Request,
        x_api_key: str | None = Header(default=None),
        session: Session = Depends(get_session),
    ) -> User:
        user = _authenticate(request, x_api_key, session)
        resource_id = request.path_params.get(id_param) if id_param else None
        detail = ({"query": {k: v[:100] for k, v in request.query_params.items()}}
                  if request.query_params else None)
        allowed = user.role in roles
        # Fail closed: if this write fails, the request fails and no data is returned.
        log_event(user.username, user.role, action, resource_type, resource_id,
                  "ALLOWED" if allowed else "DENIED", detail)
        if not allowed:
            raise HTTPException(status_code=403,
                                detail="Your role is not allowed to do this")
        return user
    return dependency