"""Value-level parsers that turn messy source representations into canonical values.

Each parser returns the canonical value, returns ``None`` for explicit "no value" tokens,
and raises ``ValueError`` for anything it cannot interpret. Callers decide whether a
``ValueError`` means quarantine or "null and flag".
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

import phonenumbers

NULL_TOKENS = frozenset({"", "n/a", "na", "null", "none", "nan", "unknown", "unlisted", "-"})

_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "0"})

_CURRENCY_ALIASES = {
    "usd": "USD", "us$": "USD", "$": "USD", "us dollar": "USD", "us dollars": "USD",
    "eur": "EUR", "€": "EUR", "euro": "EUR",
    "gbp": "GBP", "£": "GBP",
    "aud": "AUD", "cad": "CAD", "jpy": "JPY",
}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}

# Date conventions, established from the data (see PLAN.md, finding 1): customer
# registration dates checked against their ISO metadata.created_at copy matched 203/203
# for dash-separated values as month-first and 185/185 for slash-separated values as
# day-first. Unambiguous values (a part > 12) always override the convention.
_ISO = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ].*)?$")
_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DASH = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_MON = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_EPOCH = re.compile(r"^\d{9,10}$")


def is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return isinstance(value, str) and value.strip().lower() in NULL_TOKENS


def parse_bool(value: Any) -> bool | None:
    if is_null(value):
        return None
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _day_month(first: int, second: int, *, day_first: bool) -> tuple[int, int]:
    if first > 12:
        return first, second
    if second > 12:
        return second, first
    return (first, second) if day_first else (second, first)


def parse_date(value: Any) -> date | None:
    """Parse any of the date formats found in the sources into a ``date``."""
    if is_null(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if m := _ISO.match(text):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := _SLASH.match(text):
        day, month = _day_month(int(m[1]), int(m[2]), day_first=True)
        return date(int(m[3]), month, day)
    if m := _DASH.match(text):
        day, month = _day_month(int(m[1]), int(m[2]), day_first=False)
        return date(int(m[3]), month, day)
    if m := _MON.match(text):
        month = _MONTHS.get(m[2].lower())
        if month is None:
            raise ValueError(f"unknown month in {value!r}")
        return date(int(m[3]), month, int(m[1]))
    if _EPOCH.match(text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc).date()
    raise ValueError(f"unrecognised date format: {value!r}")


def parse_datetime(value: Any) -> datetime | None:
    """Parse ISO timestamps; date-only values (any supported format) become midnight."""
    if is_null(value):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.combine(parse_date(text), datetime.min.time())
    # Store naive UTC so comparisons across sources are consistent.
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def parse_hms(value: Any) -> timedelta | None:
    """Parse an ``HH:MM:SS`` offset (call utterance timestamps)."""
    if is_null(value):
        return None
    parts = str(value).strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not an HH:MM:SS offset: {value!r}")
    hours, minutes, seconds = map(int, parts)
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def parse_float(value: Any) -> float | None:
    if is_null(value):
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    return float(value)


def parse_int(value: Any) -> int | None:
    number = parse_float(value)
    if number is None:
        return None
    if not number.is_integer():
        raise ValueError(f"not a whole number: {value!r}")
    return int(number)


def normalize_currency(value: Any) -> str | None:
    if is_null(value):
        return None
    code = _CURRENCY_ALIASES.get(str(value).strip().lower())
    if code is None:
        raise ValueError(f"unknown currency: {value!r}")
    return code


def normalize_text(value: Any) -> str | None:
    """Unicode NFC, trimmed, internal whitespace collapsed. Empty becomes ``None``."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value))).strip()
    return text or None


def normalize_name(value: Any) -> str | None:
    """Consistent display casing for all-lower/ALL-UPPER names; mixed case is kept."""
    text = normalize_text(value)
    if text is None or not (text.islower() or text.isupper()):
        return text
    return " ".join(word.capitalize() for word in text.split(" "))


def normalize_token(value: Any) -> str | None:
    """Canonical enum token: lower snake_case (``'Prefer not to say'`` → ``prefer_not_to_say``)."""
    if is_null(value):
        return None
    return re.sub(r"[\s\-]+", "_", str(value).strip().lower())


_EMAIL = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")


def normalize_email(value: Any) -> str | None:
    if is_null(value):
        return None
    email = str(value).strip().lower()
    if not _EMAIL.match(email):
        raise ValueError(f"invalid email: {value!r}")
    return email


def normalize_phone(value: Any, default_region: str = "US") -> str | None:
    """E.164 formatting. Numbers without a country code are read in ``default_region``."""
    if is_null(value):
        return None
    try:
        number = phonenumbers.parse(str(value), default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"unparseable phone: {value!r}") from exc
    if not phonenumbers.is_possible_number(number):
        raise ValueError(f"impossible phone number: {value!r}")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def split_tags(value: Any) -> list[str]:
    """Comma-separated or list tags → de-duplicated lower-case list, order preserved."""
    if is_null(value):
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    tags = (normalize_token(item) for item in items)
    return list(dict.fromkeys(t for t in tags if t))
