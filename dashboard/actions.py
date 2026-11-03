"""Plain functions behind the 'New application and payment' page (no Streamlit).
The page calls them, and tests/test_dashboard_actions.py runs the same code.
ALL DATA IS SYNTHETIC."""
from dashboard import client
from dashboard.client import ApiResult, call

PURPOSES = ["EMPLOYMENT", "TRAVEL_ABROAD", "BUSINESS", "SCHOLARSHIP", "PERSONAL"]


def create_application(fields: dict, role: str) -> ApiResult:
    """POST /applicants. Trims text, turns a date into ISO text, blank middle name -> None."""
    body = {}
    for key, value in fields.items():
        if hasattr(value, "isoformat"):          # a date object -> "1990-01-01"
            value = value.isoformat()
        elif isinstance(value, str):
            value = value.strip()
        body[key] = value
    body["middle_name"] = body.get("middle_name") or None
    return call("POST", "/applicants", role, json_body=body)


def request_payment(application_ref: str, role: str) -> ApiResult:
    """POST /applications/{ref}/payments (reuses an open payment if one exists)."""
    return call("POST", f"/applications/{application_ref.strip()}/payments", role)


def gateway_send(payment_ref: str, status: str, amount: str = "130.00",
                 event_id: str | None = None) -> ApiResult:
    """Play the payment gateway: send a signed webhook (PAID or FAILED)."""
    return client.send_payment_webhook(payment_ref, status=status, amount=amount,
                                       event_id=event_id)


def get_status(application_ref: str, role: str) -> ApiResult:
    return call("GET", f"/applications/{application_ref.strip()}/status", role)


def explain_error(res: ApiResult) -> str:
    """Turn an API error into a sentence a person can act on."""
    if res.ok:
        return ""
    if res.status == 422 and isinstance(res.data, dict) \
            and isinstance(res.data.get("detail"), list):
        parts = []
        for d in res.data["detail"]:
            where = ".".join(str(x) for x in d.get("loc", []) if x != "body")
            parts.append(f"{where}: {d.get('msg')}")
        return "Please fix: " + "; ".join(parts)
    return res.error