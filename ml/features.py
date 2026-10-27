"""Turn an (applicant, watchlist record) pair into 8 similarity numbers.

Inputs are dicts with: first_name, middle_name, last_name, birthdate,
birthplace, mother_maiden_name. Birthdates may be date objects or ISO text.
Nothing here ever sees a ground-truth label.
"""
import re
from datetime import date

from rapidfuzz.distance import JaroWinkler

FEATURE_NAMES = [
    "full_name_jw",      # Jaro-Winkler on first + middle + last name
    "surname_jw",
    "first_name_jw",
    "birthdate_exact",   # 1 if identical, else 0
    "birthdate_close",   # 1 if day/month swapped, or one year off with a similar day
    "birth_year_gap",    # years apart, capped at 10, scaled to 0..1 (0 = same year)
    "birthplace_jw",
    "mother_maiden_jw",
]


def _norm(text: str | None) -> str:
    """Lowercase, drop punctuation (so 'Ma.' becomes 'ma'), collapse spaces."""
    text = re.sub(r"[^a-z0-9 ]", "", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _jw(a: str | None, b: str | None) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:          # nothing to compare = no evidence
        return 0.0
    return float(JaroWinkler.similarity(a, b))


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _full_name(person: dict) -> str:
    parts = (person["first_name"], person.get("middle_name"), person["last_name"])
    return " ".join(p for p in parts if p)


def compute_features(applicant: dict, record: dict) -> dict:
    da, db = _as_date(applicant["birthdate"]), _as_date(record["birthdate"])
    exact = da == db
    swapped = da.year == db.year and da.month == db.day and da.day == db.month
    one_year = (abs(da.year - db.year) == 1 and da.month == db.month
                and abs(da.day - db.day) <= 3)
    return {
        "full_name_jw": _jw(_full_name(applicant), _full_name(record)),
        "surname_jw": _jw(applicant["last_name"], record["last_name"]),
        "first_name_jw": _jw(applicant["first_name"], record["first_name"]),
        "birthdate_exact": 1.0 if exact else 0.0,
        "birthdate_close": 1.0 if (not exact and (swapped or one_year)) else 0.0,
        "birth_year_gap": min(abs(da.year - db.year), 10) / 10,
        "birthplace_jw": _jw(applicant["birthplace"], record["birthplace"]),
        "mother_maiden_jw": _jw(applicant["mother_maiden_name"],
                                record["mother_maiden_name"]),
    }