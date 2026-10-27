"""Role-based views of an application: masked for front desk, full for reviewers."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Applicant, Application, HitCase
from app.status import public_status

FULL_VIEW_ROLES = ("reviewer", "admin")


def mask_word(word: str) -> str:
    """Keep the first letter only. A fixed '***' hides the real length too."""
    return word[0] + "***" if word else ""


def mask_name(first_name: str, last_name: str) -> str:
    """'Jose Dela Cruz' -> 'J*** D*** C***'. Middle name is left out entirely."""
    return " ".join(mask_word(p) for p in f"{first_name} {last_name}".split())


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def get_application_view(session: Session, application_ref: str,
                         role: str) -> dict | None:
    row = session.execute(
        select(Application, Applicant)
        .join(Applicant, Applicant.id == Application.applicant_id)
        .where(Application.reference_no == application_ref)).first()
    if row is None:
        return None
    application, applicant = row

    if role in FULL_VIEW_ROLES:
        cases = session.execute(
            select(HitCase.id, HitCase.decision_band,
                   HitCase.match_probability, HitCase.status)
            .where(HitCase.application_id == application.id)
            .order_by(HitCase.match_probability.desc()).limit(20)).all()
        return {
            "view": "full",
            "application_ref": application.reference_no,
            "purpose": application.purpose,
            "status": application.status,                   # internal status
            "applicant": {
                "first_name": applicant.first_name,
                "middle_name": applicant.middle_name,
                "last_name": applicant.last_name,
                "birthdate": applicant.birthdate.isoformat(),
                "birthplace": applicant.birthplace,
                "mother_maiden_name": applicant.mother_maiden_name,
                "email": applicant.email,
            },
            "hit_cases": [{"hit_case_id": c.id, "band": c.decision_band,
                           "probability": round(c.match_probability, 4),
                           "status": c.status} for c in cases],
        }

    # Front desk: masked identity, no birthdate, no watchlist evidence at all
    return {
        "view": "masked",
        "application_ref": application.reference_no,
        "purpose": application.purpose,
        "status": public_status(application.status),
        "applicant": {
            "name": mask_name(applicant.first_name, applicant.last_name),
            "email": mask_email(applicant.email),
        },
    }