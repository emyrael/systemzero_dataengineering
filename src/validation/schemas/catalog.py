"""Schemas for the product catalog and vendors."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import ValidationInfo, field_validator, model_validator

from src.cleaning import standardize as std
from src.validation.base import (
    RecordModel,
    check_range,
    id_parser,
    note,
    require,
    soft,
    standardized,
)

PRODUCT_ID = id_parser(r"PRD-\d{6}")
VENDOR_ID = id_parser(r"VND-\d{3}")


def foreign_key(value: Any) -> str | None:
    """Reference to another entity; existence is checked in the cleaning stage."""
    return None if std.is_null(value) else str(value).strip().upper()


class Product(RecordModel):
    product_id: str
    sku: str | None
    product_name: str
    description: str | None
    category: str | None
    subcategory: str | None
    vendor_id: str | None
    price: float | None
    cost: float | None
    currency: str | None
    weight_kg: float | None
    stock_quantity: int | None
    reorder_level: int | None
    rating: float | None
    review_count: int | None
    is_active: bool | None
    created_date: date | None
    last_updated: datetime | None
    tags: list[str]

    @field_validator("product_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return require("products.invalid_id", PRODUCT_ID, v)

    @field_validator("product_name", mode="before")
    @classmethod
    def _name(cls, v: Any, info: ValidationInfo) -> str:
        name = require("products.missing_name", std.normalize_name, v)
        return standardized(info, "products.name_case", std.normalize_text(v), name)

    @field_validator("sku", "description", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return std.normalize_text(v)

    @field_validator("category", "subcategory", mode="before")
    @classmethod
    def _category(cls, v: Any, info: ValidationInfo) -> str | None:
        value = None if std.is_null(v) else std.normalize_text(v)
        if value is None and info.field_name == "category":
            note(info, "products.missing_category")
        return value

    @field_validator("vendor_id", mode="before")
    @classmethod
    def _vendor(cls, v: Any) -> str | None:
        return foreign_key(v)

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, v: Any, info: ValidationInfo) -> float | None:
        price = soft(info, "products.invalid_price", std.parse_float, v)
        return check_range(info, "products.invalid_price", price, low=0.01)

    @field_validator("cost", mode="before")
    @classmethod
    def _cost(cls, v: Any, info: ValidationInfo) -> float | None:
        cost = std.parse_float(v)
        if cost is None:
            note(info, "products.missing_cost")
        return cost

    @field_validator("weight_kg", mode="before")
    @classmethod
    def _weight(cls, v: Any, info: ValidationInfo) -> float | None:
        weight = soft(info, "products.invalid_weight", std.parse_float, v)
        return check_range(info, "products.invalid_weight", weight, low=0.001)

    @field_validator("stock_quantity", mode="before")
    @classmethod
    def _stock(cls, v: Any, info: ValidationInfo) -> int | None:
        stock = std.parse_int(v)
        if stock is not None and stock < 0:
            note(info, "products.negative_stock")
        return stock

    @field_validator("reorder_level", "review_count", mode="before")
    @classmethod
    def _count(cls, v: Any) -> int | None:
        return std.parse_int(v)

    @field_validator("rating", mode="before")
    @classmethod
    def _rating(cls, v: Any, info: ValidationInfo) -> float | None:
        rating = soft(info, "products.invalid_rating", std.parse_float, v)
        return check_range(info, "products.invalid_rating", rating, low=0, high=5)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any, info: ValidationInfo) -> str | None:
        return standardized(info, "products.currency", v, std.normalize_currency(v))

    @field_validator("is_active", mode="before")
    @classmethod
    def _active(cls, v: Any, info: ValidationInfo) -> bool | None:
        return standardized(info, "products.boolean", v, std.parse_bool(v))

    @field_validator("created_date", mode="before")
    @classmethod
    def _created(cls, v: Any, info: ValidationInfo) -> date | None:
        return standardized(info, "products.date_format", v, soft(info, "products.invalid_date", std.parse_date, v))

    @field_validator("last_updated", mode="before")
    @classmethod
    def _updated(cls, v: Any) -> datetime | None:
        return std.parse_datetime(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> list[str]:
        return std.split_tags(v)

    @model_validator(mode="after")
    def _dates_in_order(self, info: ValidationInfo) -> Product:
        if self.created_date and self.last_updated and self.created_date > self.last_updated.date():
            note(info, "products.created_after_updated")
        return self


class Vendor(RecordModel):
    vendor_id: str
    vendor_name: str | None
    vendor_code: str | None
    country: str | None
    country_code: str | None
    region: str | None
    status: str | None
    reliability_score: float | None
    lead_time_days: int | None
    payment_terms: str | None
    currency: str | None
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None
    address_street: str | None
    address_city: str | None
    address_state: str | None
    address_postal_code: str | None
    address_country: str | None
    categories: list[str]
    certifications: list[str]
    created_at: datetime | None
    updated_at: datetime | None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        contact = data.get("contact") or {}
        address = data.get("address") or {}
        return {
            **data,
            "contact_name": contact.get("primary_name"),
            "contact_email": contact.get("email"),
            "contact_phone": contact.get("phone"),
            **{f"address_{k}": address.get(k) for k in ("street", "city", "state", "postal_code", "country")},
        }

    @field_validator("vendor_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return require("vendors.invalid_id", VENDOR_ID, v)

    @field_validator(
        "vendor_name", "vendor_code", "country", "country_code", "region", "contact_name",
        "address_street", "address_city", "address_state", "address_postal_code", "address_country",
        mode="before",
    )
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return None if std.is_null(v) else std.normalize_text(v)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> str | None:
        return std.normalize_token(v)

    @field_validator("payment_terms", mode="before")
    @classmethod
    def _terms(cls, v: Any) -> str | None:
        return None if std.is_null(v) else str(v).strip().upper()

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any) -> str | None:
        return std.normalize_currency(v)

    @field_validator("reliability_score", mode="before")
    @classmethod
    def _reliability(cls, v: Any, info: ValidationInfo) -> float | None:
        score = std.parse_float(v)
        if score is None:
            note(info, "vendors.missing_reliability")
        return check_range(info, "vendors.invalid_reliability", score, low=0, high=100)

    @field_validator("lead_time_days", mode="before")
    @classmethod
    def _lead_time(cls, v: Any) -> int | None:
        return std.parse_int(v)

    @field_validator("contact_email", mode="before")
    @classmethod
    def _email(cls, v: Any, info: ValidationInfo) -> str | None:
        return soft(info, "vendors.incomplete_contact", std.normalize_email, v)

    @field_validator("contact_phone", mode="before")
    @classmethod
    def _phone(cls, v: Any, info: ValidationInfo) -> str | None:
        phone = soft(info, "vendors.incomplete_contact", std.normalize_phone, v)
        return standardized(info, "vendors.phone_format", v, phone)

    @field_validator("categories", "certifications", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return std.split_tags(v)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _timestamps(cls, v: Any) -> datetime | None:
        return std.parse_datetime(v)

    @model_validator(mode="after")
    def _contact_completeness(self, info: ValidationInfo) -> Vendor:
        present = [self.contact_name, self.contact_email, self.contact_phone]
        if not any(present):
            note(info, "vendors.missing_contact")
        elif not all(present):
            note(info, "vendors.incomplete_contact")
        return self
