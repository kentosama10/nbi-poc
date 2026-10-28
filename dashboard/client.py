"""How the dashboard talks to the FastAPI service (over HTTP, with a role's API key).

The dashboard reads personal data ONLY through the API, so roles, masking and the
audit log apply to it too. The few demo-only shortcuts are isolated in demo_tools.py.
"""
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app import config

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")

ROLES = ("applicant", "frontdesk", "reviewer", "admin")
ROLE_KEYS = {
    "applicant": config.API_KEY_APPLICANT,
    "frontdesk": config.API_KEY_FRONTDESK,
    "reviewer": config.API_KEY_REVIEWER,
    "admin": config.API_KEY_ADMIN,
}


@dataclass
class ApiResult:
    """What came back: the HTTP status (0 = could not connect) and the parsed body."""
    status: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error(self) -> str:
        if self.ok:
            return ""
        if self.status == 0:
            return str(self.data)
        if isinstance(self.data, dict) and "detail" in self.data:
            return f"HTTP {self.status}: {self.data['detail']}"
        return f"HTTP {self.status}"


def make_http() -> httpx.Client:
    """One HTTP connection. The tests replace this function with an in-process one."""
    return httpx.Client(base_url=API_URL, timeout=60.0)


def _send(method: str, path: str, headers: dict, **kwargs) -> ApiResult:
    http = make_http()
    try:
        resp = http.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        return ApiResult(0, f"Cannot reach the API at {API_URL} "
                            f"({type(exc).__name__}). Is uvicorn running?")
    finally:
        http.close()
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return ApiResult(resp.status_code, data)


def call(method: str, path: str, role: str | None = None,
         json_body: dict | None = None, params: dict | None = None) -> ApiResult:
    """Call the API as the given role (None = no key, for /health)."""
    headers = {"X-API-Key": ROLE_KEYS[role]} if role else {}
    return _send(method, path, headers, json=json_body, params=params)


# ---------------------------------------------------- simulated payment gateway
def sign(raw: bytes) -> str:
    """HMAC-SHA256 signature, the same recipe the API uses to verify webhooks."""
    return hmac.new(config.WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def send_payment_webhook(payment_ref: str, status: str = "PAID",
                         amount: str = "130.00",
                         event_id: str | None = None) -> ApiResult:
    """Play the payment gateway: send a signed webhook to the API."""
    body = {"event_id": event_id or "EVT-" + uuid.uuid4().hex[:10],
            "payment_ref": payment_ref, "status": status, "amount": amount}
    raw = json.dumps(body).encode()
    return _send("POST", "/webhooks/payment",
                 {"X-Signature": sign(raw), "Content-Type": "application/json"},
                 content=raw)