"""Shared machinery for record schemas: rule events, soft parsing and the base model.

Schemas receive a validation context ``{"events": [...], "as_of": date}``. Validators
append rule ids to ``events`` (fixes and flags) or raise :func:`reject` (quarantine).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationInfo, model_validator
from pydantic_core import PydanticCustomError

from src.cleaning.standardize import is_null

T = TypeVar("T")


class RecordModel(BaseModel):
    """Base for all source schemas. Unknown source columns are ignored.

    Field validators parse single values; ``mode="after"`` model validators apply
    cross-field rules and may overwrite derived fields, so models are not frozen.
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _absent_keys_are_null(cls, data: Any) -> Any:
        """Treat a key missing from the source like an explicit null.

        Field validators then decide what a missing value means (quarantine, flag or
        nothing), instead of Pydantic rejecting the record for a missing optional key.
        """
        if not isinstance(data, dict):
            return data
        required = {name: None for name, field in cls.model_fields.items() if field.is_required()}
        return {**required, **data}


def note(info: ValidationInfo, rule_id: str) -> None:
    """Record that a fix or flag rule fired for the current record."""
    if info.context is not None:
        info.context.setdefault("events", []).append(rule_id)


def as_of(info: ValidationInfo) -> date:
    return (info.context or {}).get("as_of") or date.today()


def reject(rule_id: str, message: str) -> PydanticCustomError:
    """Build a quarantine error whose ``type`` is the rule id (raise the result)."""
    return PydanticCustomError(rule_id, message)


def require(rule_id: str, parser: Callable[[Any], T | None], value: Any) -> T:
    """Parse a value the record cannot live without; failure or absence quarantines."""
    try:
        parsed = parser(value)
    except ValueError as exc:
        raise reject(rule_id, str(exc)) from exc
    if parsed is None:
        raise reject(rule_id, "required value is missing")
    return parsed


def soft(info: ValidationInfo, flag_rule: str, parser: Callable[[Any], T | None], value: Any) -> T | None:
    """Parse an attribute; if it is invalid, null it and flag the record instead of rejecting."""
    try:
        return parser(value)
    except (ValueError, TypeError):
        note(info, flag_rule)
        return None


def standardized(info: ValidationInfo, fix_rule: str, raw: Any, value: Any) -> Any:
    """Return ``value``, noting a fix when its canonical form differs from the raw input."""
    if value is not None and not is_null(raw) and _canonical(raw) != _canonical(value):
        note(info, fix_rule)
    return value


def id_parser(pattern: str) -> Callable[[Any], str | None]:
    """Parser for identifiers: trimmed, upper-cased, must fully match ``pattern``."""
    compiled = re.compile(pattern)

    def parse(value: Any) -> str | None:
        if is_null(value):
            return None
        text = str(value).strip().upper()
        if not compiled.fullmatch(text):
            raise ValueError(f"{value!r} does not match {pattern}")
        return text

    return parse


def _canonical(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def check_range(
    info: ValidationInfo, flag_rule: str, value: float | None, low: float | None = None, high: float | None = None
) -> float | None:
    """Null and flag numbers outside ``[low, high]``."""
    if value is None:
        return None
    if (low is not None and value < low) or (high is not None and value > high):
        note(info, flag_rule)
        return None
    return value


def close(a: float | None, b: float | None, tolerance: float = 0.02) -> bool:
    """Monetary equality with rounding tolerance; missing values never count as a mismatch."""
    return a is None or b is None or abs(a - b) <= tolerance
