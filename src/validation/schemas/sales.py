"""B2C sales transaction schema."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import ValidationInfo, field_validator, model_validator

from src.cleaning import standardize as std
from src.validation.base import RecordModel, close, id_parser, note, reject, require, standardized
from src.validation.schemas.catalog import foreign_key

TRANSACTION_ID = id_parser(r"TXN-[0-9A-F]{8}")
_JUNK_NOTE = re.compile(r"^(special instructions:\s*)?(.)\2{9,}$", re.IGNORECASE)


class Transaction(RecordModel):
    transaction_id: str
    order_id: str | None
    customer_id: str | None
    product_id: str | None
    transaction_ts: datetime
    transaction_date: date
    quantity: int
    is_return: bool = False
    unit_price: float | None
    subtotal: float | None
    discount_percent: float | None
    discount_amount: float | None
    tax_rate: float | None
    tax_amount: float | None
    shipping_cost: float | None
    total_amount: float | None
    currency: str | None
    payment_method: str | None
    payment_status: str | None
    order_status: str | None
    shipping_method: str | None
    channel: str | None
    region: str | None
    is_gift: bool | None
    notes: str | None

    @model_validator(mode="before")
    @classmethod
    def _timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict):
            ts = data.get("transaction_timestamp")
            data = {**data, "transaction_ts": data.get("transaction_date") if std.is_null(ts) else ts}
        return data

    @field_validator("transaction_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return require("transactions.invalid_id", TRANSACTION_ID, v)

    @field_validator("order_id", "customer_id", "product_id", mode="before")
    @classmethod
    def _fk(cls, v: Any) -> str | None:
        return foreign_key(v)

    @field_validator("transaction_ts", mode="before")
    @classmethod
    def _ts(cls, v: Any) -> datetime:
        return require("transactions.invalid_date", std.parse_datetime, v)

    @field_validator("transaction_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> date | None:
        try:
            return std.parse_date(v)
        except ValueError:
            return None  # re-derived from transaction_ts below

    @field_validator("quantity", mode="before")
    @classmethod
    def _quantity(cls, v: Any) -> int:
        quantity = std.parse_int(v)
        if quantity is None:
            raise ValueError("quantity missing")
        if quantity == 0:
            raise reject("transactions.zero_quantity", "quantity is 0")
        return quantity

    @field_validator(
        "unit_price", "subtotal", "discount_percent", "discount_amount", "tax_rate",
        "tax_amount", "shipping_cost", "total_amount", mode="before",
    )
    @classmethod
    def _money(cls, v: Any) -> float | None:
        return std.parse_float(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any, info: ValidationInfo) -> str | None:
        return standardized(info, "transactions.currency", v, std.normalize_currency(v))

    @field_validator("payment_method", "payment_status", "order_status", "shipping_method", "channel", mode="before")
    @classmethod
    def _token(cls, v: Any) -> str | None:
        return std.normalize_token(v)

    @field_validator("region", mode="before")
    @classmethod
    def _region(cls, v: Any) -> str | None:
        return None if std.is_null(v) else std.normalize_text(v)

    @field_validator("is_gift", mode="before")
    @classmethod
    def _gift(cls, v: Any, info: ValidationInfo) -> bool | None:
        return standardized(info, "transactions.boolean", v, std.parse_bool(v))

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v: Any, info: ValidationInfo) -> str | None:
        text = std.normalize_text(v) if not std.is_null(v) else None
        if text and _JUNK_NOTE.match(text):
            note(info, "transactions.junk_notes")
            return None
        return text

    @model_validator(mode="after")
    def _cross_field(self, info: ValidationInfo) -> Transaction:
        if self.transaction_date is None or self.transaction_date != self.transaction_ts.date():
            self.transaction_date = self.transaction_ts.date()

        if self.quantity < 0:
            self.is_return = True
            note(info, "transactions.return")
        elif self.shipping_cost is not None and self.shipping_cost < 0:
            note(info, "transactions.negative_shipping")

        # Returns reverse the merchandise amounts; shipping carries its own sign.
        parts = (self.subtotal, self.discount_amount, self.tax_amount, self.shipping_cost)
        if all(p is not None for p in parts):
            sign = -1 if self.is_return else 1
            expected = sign * (self.subtotal - self.discount_amount + self.tax_amount) + self.shipping_cost
            if not close(expected, self.total_amount):
                note(info, "transactions.amount_mismatch")
        return self
