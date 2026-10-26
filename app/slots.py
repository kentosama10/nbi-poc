"""Appointment slot inventory design (synthetic).

Shared by the loader (Phase 4) and the booking API (Phase 5).

Redis layout:
  slot:<SITE>:<YYYYMMDDTHHMM>   a HASH with the fields:
        site_code, site, start (ISO time), capacity, available, demo
  slots:index                   a SORTED SET of slot keys ordered by start time
  slots:demo_key                the key of the special 1-seat slot used in the
                                "last slot" concurrency demo
"""
from datetime import datetime, time, timedelta, timezone

# The Philippines uses UTC+8 all year (no daylight saving), so a fixed offset is
# correct and avoids needing a timezone database on Windows.
PH_TZ = timezone(timedelta(hours=8))

SITES = {
    "MNL": "NBI Main - Manila",
    "QC": "NBI Quezon City",
    "CEB": "NBI Cebu City",
}
BOOKING_DAYS = 10          # next 10 weekdays are bookable
FIRST_HOUR = 8             # slots start at 8:00 ...
LAST_HOUR = 15             # ... and the last regular one starts at 15:00
CAPACITY_PER_SLOT = 5      # seats per slot

SLOT_INDEX_KEY = "slots:index"
DEMO_POINTER_KEY = "slots:demo_key"


def slot_key(site_code: str, start: datetime) -> str:
    """Example: slot:MNL:20261027T0800"""
    return f"slot:{site_code}:{start:%Y%m%dT%H%M}"


def build_slots(today=None) -> list[dict]:
    """Return every slot as a dict. Dates are relative to today, so reloading
    always gives you bookable dates."""
    today = today or datetime.now(PH_TZ).date()
    slots = []
    day, days_added = today, 0
    while days_added < BOOKING_DAYS:
        day += timedelta(days=1)
        if day.weekday() >= 5:          # 5 = Saturday, 6 = Sunday: skip
            continue
        days_added += 1
        for code, name in SITES.items():
            for hour in range(FIRST_HOUR, LAST_HOUR + 1):
                start = datetime.combine(day, time(hour, 0), tzinfo=PH_TZ)
                slots.append({
                    "key": slot_key(code, start), "site_code": code,
                    "site": name, "start": start,
                    "capacity": CAPACITY_PER_SLOT,
                    "available": CAPACITY_PER_SLOT, "demo": False,
                })

    # One special slot with exactly ONE seat: "the last slot" for demo scenario 1
    first_day = slots[0]["start"].date()
    demo_start = datetime.combine(first_day, time(16, 0), tzinfo=PH_TZ)
    slots.append({
        "key": slot_key("MNL", demo_start), "site_code": "MNL",
        "site": SITES["MNL"], "start": demo_start,
        "capacity": 1, "available": 1, "demo": True,
    })
    return slots