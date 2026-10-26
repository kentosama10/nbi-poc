"""Request shapes (Pydantic). FastAPI rejects anything that does not fit with a 422."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ApplicantIn(BaseModel):
    """Data for a new application. Use SYNTHETIC values only, never real people."""
    first_name: str = Field(min_length=1, max_length=80)
    middle_name: str | None = Field(default=None, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    birthdate: date
    birthplace: str = Field(min_length=1, max_length=120)
    mother_maiden_name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    purpose: Literal["EMPLOYMENT", "TRAVEL_ABROAD", "BUSINESS",
                     "SCHOLARSHIP", "PERSONAL"]

    @field_validator("birthdate")
    @classmethod
    def birthdate_must_be_in_the_past(cls, value: date) -> date:
        if value >= date.today():
            raise ValueError("birthdate must be in the past")
        return value


class BookingIn(BaseModel):
    application_ref: str = Field(min_length=1, max_length=30)   # e.g. NBI-000001
    # Slot keys look like  slot:MNL:20261027T0800
    slot_key: str = Field(pattern=r"^slot:[A-Z]+:\d{8}T\d{4}$")