"""Customer profile schema."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import ValidationInfo, field_validator, model_validator

from src.cleaning import standardize as std
from src.validation.base import RecordModel, as_of, id_parser, note, reject, soft, standardized

CUSTOMER_ID = id_parser(r"CUS-\d{8}")

_GENDERS = {
    "m": "male", "male": "male",
    "f": "female", "female": "female",
    "other": "other",
    "prefer_not_to_say": "prefer_not_to_say",
}
SEGMENTS = ("bronze", "silver", "gold", "platinum", "vip")


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


class Customer(RecordModel):
    customer_id: str
    first_name: str | None
    last_name: str | None
    full_name: str
    email: str | None
    phone: str | None
    gender: str
    age: int | None
    date_of_birth: date | None
    segment: str | None
    src_total_orders: int | None
    src_total_spend: float | None
    src_average_order_value: float | None
    registration_date: date | None
    last_purchase_date: date | None
    is_active: bool | None
    email_verified: bool | None
    phone_verified: bool | None
    address_street: str | None
    address_city: str | None
    address_state: str | None
    address_postal_code: str | None
    address_country: str | None
    newsletter_opt_in: bool | None
    sms_opt_in: bool | None
    preferred_language: str | None
    preferred_currency: str | None
    acquisition_source: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        address = data.get("address") or {}
        prefs = data.get("preferences") or {}
        meta = data.get("metadata") or {}
        first, last = data.get("first_name"), data.get("last_name")
        full = data.get("full_name")
        if std.is_null(full) and not (std.is_null(first) and std.is_null(last)):
            full = " ".join(p for p in (first, last) if not std.is_null(p))
        return {
            **data,
            "full_name": full,
            "src_total_orders": data.get("total_orders"),
            "src_total_spend": data.get("total_spend"),
            "src_average_order_value": data.get("average_order_value"),
            **{f"address_{k}": address.get(k) for k in ("street", "city", "state", "postal_code", "country")},
            "newsletter_opt_in": prefs.get("newsletter"),
            "sms_opt_in": prefs.get("sms_notifications"),
            "preferred_language": prefs.get("preferred_language"),
            "preferred_currency": prefs.get("preferred_currency"),
            "acquisition_source": meta.get("source"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
        }

    @field_validator("customer_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        parsed = CUSTOMER_ID(v) if not std.is_null(v) else None
        if parsed is None:
            raise reject("customers.invalid_id", f"bad customer_id {v!r}")
        return parsed

    @field_validator("full_name", mode="before")
    @classmethod
    def _full_name(cls, v: Any) -> str:
        if std.is_null(v):
            raise reject("customers.missing_name", "no name on record")
        return std.normalize_text(v)

    @field_validator(
        "first_name", "last_name", "address_street", "address_city", "address_state",
        "address_postal_code", "address_country", mode="before",
    )
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return None if std.is_null(v) else std.normalize_text(v)

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, v: Any, info: ValidationInfo) -> str | None:
        if std.is_null(v):
            note(info, "customers.missing_email")
            return None
        email = soft(info, "customers.invalid_email", std.normalize_email, v)
        return standardized(info, "customers.email_format", v, email)

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v: Any, info: ValidationInfo) -> str | None:
        # Every number in the file is NANP-formatted regardless of the customer's country,
        # so numbers without a country code are read as US/Canada.
        phone = soft(info, "customers.invalid_phone", std.normalize_phone, v)
        return standardized(info, "customers.phone_format", v, phone)

    @field_validator("gender", mode="before")
    @classmethod
    def _gender(cls, v: Any, info: ValidationInfo) -> str:
        token = std.normalize_token(v)
        gender = _GENDERS.get(token, "unknown") if token else "unknown"
        return standardized(info, "customers.gender", v, gender)

    @field_validator("segment", mode="before")
    @classmethod
    def _segment(cls, v: Any, info: ValidationInfo) -> str | None:
        token = std.normalize_token(v)
        if token is not None and token not in SEGMENTS:
            raise ValueError(f"unknown segment {v!r}")
        display = "VIP" if token == "vip" else (token or "").title()
        if token is not None and str(v).strip() != display:
            note(info, "customers.segment")
        return token

    @field_validator("age", mode="before")
    @classmethod
    def _age(cls, v: Any) -> int | None:
        try:
            age = std.parse_int(v)
        except ValueError:
            return None
        return age if age is not None and 0 <= age <= 120 else None

    @field_validator("date_of_birth", "last_purchase_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> date | None:
        return std.parse_date(v)

    @field_validator("registration_date", mode="before")
    @classmethod
    def _registration(cls, v: Any) -> date | None:
        return std.parse_date(v)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _timestamps(cls, v: Any) -> datetime | None:
        return std.parse_datetime(v)

    @field_validator(
        "is_active", "email_verified", "phone_verified", "newsletter_opt_in", "sms_opt_in", mode="before"
    )
    @classmethod
    def _bool(cls, v: Any, info: ValidationInfo) -> bool | None:
        return standardized(info, "customers.boolean", v, std.parse_bool(v))

    @field_validator("src_total_orders", mode="before")
    @classmethod
    def _orders(cls, v: Any) -> int | None:
        return std.parse_int(v)

    @field_validator("src_total_spend", "src_average_order_value", mode="before")
    @classmethod
    def _money(cls, v: Any) -> float | None:
        return std.parse_float(v)

    @field_validator("preferred_language", "acquisition_source", mode="before")
    @classmethod
    def _token(cls, v: Any) -> str | None:
        return std.normalize_token(v)

    @field_validator("preferred_currency", mode="before")
    @classmethod
    def _currency(cls, v: Any, info: ValidationInfo) -> str | None:
        return soft(info, "customers.invalid_currency_pref", std.normalize_currency, v)

    @model_validator(mode="after")
    def _cross_field(self, info: ValidationInfo) -> Customer:
        today = as_of(info)

        # metadata.created_at is ISO and agrees with every unambiguous registration_date,
        # so it is the source of truth; it also corrects UTC+1 epoch values.
        if self.created_at and self.created_at.date() != self.registration_date:
            if self.registration_date is not None:
                note(info, "customers.registration_from_metadata")
            self.registration_date = self.created_at.date()

        if self.registration_date and self.registration_date > today:
            note(info, "customers.future_registration")
            self.registration_date = None
        if self.last_purchase_date and self.last_purchase_date > today:
            note(info, "customers.future_last_purchase")
            self.last_purchase_date = None

        if self.age is None:
            if self.date_of_birth:
                self.age = age_on(self.date_of_birth, today)
                note(info, "customers.age_from_dob")
            else:
                note(info, "customers.invalid_age")

        if self.src_total_spend is not None and self.src_total_spend < 0:
            note(info, "customers.negative_total_spend")
        return self
