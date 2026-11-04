"""B2B invoice header and invoice line schemas."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import ValidationInfo, field_validator, model_validator

from src.cleaning import standardize as std
from src.validation.base import RecordModel, close, id_parser, note, reject, require, standardized
from src.validation.schemas.catalog import foreign_key

INVOICE_ID = id_parser(r"INV-[0-9A-F]{8}")
_NUMBER_YEAR = re.compile(r"-(\d{4})-")
_PAID_STATUSES = {"paid", "partial"}

# Fallback when no context is supplied (e.g. ad-hoc use); the pipeline passes config values.
DEFAULT_TERMS_DAYS = {"NET15": 15, "NET30": 30, "NET45": 45, "NET60": 60}


def _is_iso(value: Any) -> bool:
    return isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}", value.strip()))


class Invoice(RecordModel):
    invoice_id: str
    invoice_number: str | None
    vendor_id: str | None
    vendor_name: str | None
    invoice_date: date
    src_due_date: date | None
    due_date: date | None = None
    payment_terms: str | None
    po_number: str | None
    line_item_count: int | None
    lines_in_json: int | None = None
    lines_total: float | None = None
    subtotal: float | None
    tax_rate: float | None
    tax_amount: float | None
    shipping_handling: float | None
    total_amount: float | None
    is_credit_note: bool = False
    currency: str | None
    payment_status: str | None
    amount_paid: float | None
    balance_due: float | None
    payment_date: date | None
    payment_method: str | None
    approved_by: str | None
    notes: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @model_validator(mode="before")
    @classmethod
    def _prepare(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict):
            return data
        raw_dates = (data.get(k) for k in ("invoice_date", "due_date", "payment_date"))
        if any(not std.is_null(v) and not _is_iso(v) for v in raw_dates):
            note(info, "invoices.date_format")
        lines_in_json = lines_total = None
        try:
            items = json.loads(data.get("line_items_json") or "")
        except (json.JSONDecodeError, TypeError):
            items = None
        if isinstance(items, list):
            lines_in_json = len(items)
            totals = [std.parse_float(i.get("line_total")) for i in items if isinstance(i, dict)]
            lines_total = round(sum(t for t in totals if t is not None), 2)
        return {
            **data,
            "src_due_date": data.get("due_date"),
            "due_date": None,
            "lines_in_json": lines_in_json,
            "lines_total": lines_total,
        }

    @field_validator("invoice_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return require("invoices.invalid_id", INVOICE_ID, v)

    @field_validator("vendor_id", mode="before")
    @classmethod
    def _fk(cls, v: Any) -> str | None:
        return foreign_key(v)

    @field_validator("invoice_number", "vendor_name", "po_number", "approved_by", "notes", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return None if std.is_null(v) else std.normalize_text(v)

    @field_validator("invoice_date", mode="before")
    @classmethod
    def _invoice_date(cls, v: Any) -> date:
        return require("invoices.invalid_date", std.parse_date, v)

    @field_validator("src_due_date", "payment_date", mode="before")
    @classmethod
    def _dates(cls, v: Any) -> date | None:
        return std.parse_date(v)

    @field_validator("payment_terms", mode="before")
    @classmethod
    def _terms(cls, v: Any) -> str | None:
        return None if std.is_null(v) else str(v).strip().upper()

    @field_validator("line_item_count", mode="before")
    @classmethod
    def _count(cls, v: Any) -> int | None:
        return std.parse_int(v)

    @field_validator(
        "subtotal", "tax_rate", "tax_amount", "shipping_handling", "total_amount",
        "amount_paid", "balance_due", mode="before",
    )
    @classmethod
    def _money(cls, v: Any) -> float | None:
        return std.parse_float(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any, info: ValidationInfo) -> str | None:
        return standardized(info, "invoices.currency", v, std.normalize_currency(v))

    @field_validator("payment_status", "payment_method", mode="before")
    @classmethod
    def _token(cls, v: Any) -> str | None:
        return std.normalize_token(v)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _timestamps(cls, v: Any) -> datetime | None:
        return std.parse_datetime(v)

    @model_validator(mode="after")
    def _cross_field(self, info: ValidationInfo) -> Invoice:
        terms = ((info.context or {}).get("payment_terms_days")) or DEFAULT_TERMS_DAYS
        if self.payment_terms in terms:
            self.due_date = self.invoice_date + timedelta(days=terms[self.payment_terms])
            if self.src_due_date != self.due_date:
                note(info, "invoices.due_date_from_terms")
        else:
            note(info, "invoices.unknown_terms")
            self.due_date = self.src_due_date

        if self.total_amount is not None and self.total_amount < 0:
            self.is_credit_note = True
            note(info, "invoices.credit_note")

        if self.lines_in_json is None:
            note(info, "invoices.unparseable_line_items")
        else:
            if self.line_item_count is not None and self.line_item_count != self.lines_in_json:
                note(info, "invoices.line_count_mismatch")
            if not close(self.subtotal, self.lines_total):
                note(info, "invoices.subtotal_mismatch")

        if self.subtotal is not None and self.tax_rate is not None and not close(
            round(self.subtotal * self.tax_rate, 2), self.tax_amount
        ):
            note(info, "invoices.tax_mismatch")
        parts = (self.subtotal, self.tax_amount, self.shipping_handling)
        if all(p is not None for p in parts) and not close(sum(parts), self.total_amount):
            note(info, "invoices.total_mismatch")
        if self.total_amount is not None and self.amount_paid is not None and not close(
            self.total_amount - self.amount_paid, self.balance_due
        ):
            note(info, "invoices.balance_mismatch")

        if self.payment_date and self.payment_date < self.invoice_date:
            note(info, "invoices.paid_before_invoiced")
        if (self.payment_status in _PAID_STATUSES) != (self.payment_date is not None):
            note(info, "invoices.status_payment_conflict")

        year = _NUMBER_YEAR.search(self.invoice_number or "")
        if year and int(year[1]) != self.invoice_date.year:
            note(info, "invoices.number_year_mismatch")
        return self


class InvoiceLine(RecordModel):
    invoice_id: str
    line_number: int
    product_id: str | None
    description: str | None
    quantity: int
    is_credit: bool = False
    unit_cost: float | None
    line_total: float | None

    @field_validator("invoice_id", mode="before")
    @classmethod
    def _invoice(cls, v: Any) -> str:
        return require("invoice_lines.invalid_key", INVOICE_ID, v)

    @field_validator("line_number", mode="before")
    @classmethod
    def _line(cls, v: Any) -> int:
        return require("invoice_lines.invalid_key", std.parse_int, v)

    @field_validator("product_id", mode="before")
    @classmethod
    def _product(cls, v: Any, info: ValidationInfo) -> str | None:
        product = foreign_key(v)
        if product is None:
            note(info, "invoice_lines.missing_product")
        return product

    @field_validator("description", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return None if std.is_null(v) else std.normalize_text(v)

    @field_validator("quantity", mode="before")
    @classmethod
    def _quantity(cls, v: Any) -> int:
        quantity = std.parse_int(v)
        if quantity is None:
            raise ValueError("quantity missing")
        if quantity == 0:
            raise reject("invoice_lines.zero_quantity", "quantity is 0")
        return quantity

    @field_validator("unit_cost", "line_total", mode="before")
    @classmethod
    def _money(cls, v: Any) -> float | None:
        return std.parse_float(v)

    @model_validator(mode="after")
    def _cross_field(self, info: ValidationInfo) -> InvoiceLine:
        if self.quantity < 0:
            self.is_credit = True
            note(info, "invoice_lines.credit")
        if self.unit_cost is not None and not close(round(self.quantity * self.unit_cost, 2), self.line_total):
            note(info, "invoice_lines.line_total_mismatch")
        return self
