#!/usr/bin/env python
"""Exploratory profiling of the eight raw source datasets.

Reads the raw files read-only and reports what is in them: structure, column statistics,
data-quality findings, cross-dataset references and the evidence behind the pipeline's
cleaning decisions. It is not part of the ETL: nothing is cleaned, dropped or rewritten,
and no pipeline stage needs to have run.

    python scripts/data_profile.py [--config PATH] [--out-dir DIR]

Outputs ``raw_data_profile.json`` (every statistic and finding) and ``raw_data_profile.md``
(a ~3-page summary of what matters), by default in ``outputs/profiling``.
Both are deterministic: same input files and config, same bytes.

Every finding is classified as a confirmed **violation**, a suspicious **anomaly** or a
legitimate **business exception**, given a severity, and linked to the pipeline rule
(``src/validation/rules.py``) that handles it, where one exists.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python scripts/data_profile.py` from anywhere
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from src.cleaning.standardize import (  # noqa: E402
    is_null,
    normalize_currency,
    normalize_email,
    parse_bool,
    parse_date,
    parse_datetime,
    parse_float,
    parse_hms,
)
from src.ingestion.loaders import file_sha256, read_records  # noqa: E402
from src.utils.config import PipelineConfig, load_config  # noqa: E402
from src.utils.io import dumps  # noqa: E402
from src.utils.log import new_run_id, setup_logging  # noqa: E402
from src.validation.rules import RULES  # noqa: E402
from src.validation.schemas.sales import _JUNK_NOTE  # noqa: E402  (same definition as the pipeline)

REPORT_NAME = "raw_data_profile"
INVOICE_LINES = "invoice_lines"
MONEY_TOLERANCE = 0.02
TOP_VALUES = 5
CATEGORICAL_MAX_UNIQUE = 50

VIOLATION, ANOMALY, BUSINESS = "violation", "anomaly", "business_exception"
KIND_LABEL = {VIOLATION: "Confirmed violation", ANOMALY: "Suspicious anomaly", BUSINESS: "Business exception"}
SEVERITIES = ("Critical", "High", "Medium", "Low")
FACT_TABLES = {"transactions", "invoices", INVOICE_LINES}

# Format families for date-like strings. Order matters: first match wins.
DATE_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iso_datetime", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}[T ]\d{1,2}:\d{2}")),
    ("iso_date", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")),
    ("yyyy/mm/dd", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")),
    ("nn/nn/yyyy", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("nn-nn-yyyy", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("dd-Mon-yyyy", re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{4}$")),
    ("epoch_seconds", re.compile(r"^\d{9,10}$")),
)
_MOJIBAKE = re.compile(r"Ã[\x80-\xbf]|â€|Â[\x80-\xbf]")
_TRAILING_ID = re.compile(r"[0-9A-F]+$")

# Fields a record is not usable without, and the pipeline rule that handles a gap.
REQUIRED: dict[str, dict[str, str | None]] = {
    "products": {"product_id": "products.invalid_id", "product_name": "products.missing_name",
                 "category": "products.missing_category", "price": "products.invalid_price",
                 "vendor_id": None},
    "customers": {"customer_id": "customers.invalid_id", "email": "customers.missing_email",
                  "registration_date": None},
    "transactions": {"transaction_id": "transactions.invalid_id", "customer_id": None, "product_id": None,
                     "transaction_date": "transactions.invalid_date", "quantity": None, "unit_price": None,
                     "total_amount": None},
    "vendors": {"vendor_id": "vendors.invalid_id", "vendor_name": None, "status": None},
    "invoices": {"invoice_id": "invoices.invalid_id", "vendor_id": None, "invoice_date": "invoices.invalid_date",
                 "total_amount": None},
    INVOICE_LINES: {"invoice_id": "invoice_lines.invalid_key", "line_number": "invoice_lines.invalid_key",
                    "quantity": None, "unit_cost": None},
    "support_tickets": {"ticket_id": "support_tickets.invalid_id", "customer_id": None, "status": None,
                        "created_at": "support_tickets.invalid_created_at"},
    "product_reviews": {"review_id": "product_reviews.invalid_id", "product_id": None, "customer_id": None,
                        "rating": "product_reviews.missing_rating"},
    "call_transcripts": {"call_id": "call_transcripts.invalid_id", "customer_id": None,
                         "call_start": "call_transcripts.invalid_start", "call_end": None},
}


@dataclass(frozen=True)
class Relation:
    child: str
    column: str
    parent: str
    rule: str


RELATIONS: tuple[Relation, ...] = (
    Relation("products", "vendor_id", "vendors", "products.unknown_vendor"),
    Relation("transactions", "customer_id", "customers", "transactions.unknown_customer"),
    Relation("transactions", "product_id", "products", "transactions.unknown_product"),
    Relation("invoices", "vendor_id", "vendors", "invoices.unknown_vendor"),
    Relation(INVOICE_LINES, "invoice_id", "invoices", "invoice_lines.unknown_invoice"),
    Relation(INVOICE_LINES, "product_id", "products", "invoice_lines.unknown_product"),
    Relation("support_tickets", "customer_id", "customers", "support_tickets.unknown_customer"),
    Relation("support_tickets", "product_id", "products", "support_tickets.unknown_product"),
    Relation("product_reviews", "customer_id", "customers", "product_reviews.unknown_customer"),
    Relation("product_reviews", "product_id", "products", "product_reviews.unknown_product"),
    Relation("call_transcripts", "ticket_id", "support_tickets", "call_transcripts.unknown_ticket"),
    Relation("call_transcripts", "customer_id", "customers", "call_transcripts.unknown_customer"),
)


# ── data model ───────────────────────────────────────────────────────────────────


@dataclass
class RawDataset:
    name: str
    file: str
    format: str
    key: tuple[str, ...]
    records: list[dict[str, Any]] = field(default_factory=list)
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    envelope: dict[str, Any] = field(default_factory=dict)
    file_bytes: int | None = None
    sha256: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Finding:
    dataset: str
    check: str
    severity: str
    kind: str
    count: int
    total: int
    description: str
    recommendation: str
    rule: str | None = None
    samples: list[str] = field(default_factory=list)

    @property
    def pct(self) -> float:
        return round(100 * self.count / self.total, 2) if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "pct": self.pct}


# ── loading ──────────────────────────────────────────────────────────────────────


def load_dataset(name: str, config: PipelineConfig) -> RawDataset:
    """Read one source as raw records (CSV values stay strings) plus a flattened frame."""
    source = config.sources[name]
    ds = RawDataset(name=name, file=source.file, format=source.format, key=source.primary_key)
    path = config.source_path(name)
    try:
        ds.file_bytes, ds.sha256 = path.stat().st_size, file_sha256(path)
        ds.records, ds.envelope = read_records(path, source)
    except Exception as exc:  # a missing or malformed file is reported, not fatal
        ds.error = f"{type(exc).__name__}: {exc}"
        logger.error("Could not read source", source=name, error=ds.error)
        return ds
    ds.frame = pd.json_normalize(ds.records, sep=".") if ds.records else pd.DataFrame()
    return ds


def explode_invoice_lines(invoices: RawDataset) -> tuple[RawDataset, list[int | None]]:
    """Flatten ``line_items_json`` into a line dataset; also return lines per invoice (None = unparseable)."""
    lines: list[dict[str, Any]] = []
    per_invoice: list[int | None] = []
    for record in invoices.records:
        try:
            items = json.loads(record.get("line_items_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            per_invoice.append(None)
            continue
        items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        per_invoice.append(len(items))
        lines += [{"invoice_id": record.get("invoice_id"), **item} for item in items]
    ds = RawDataset(
        name=INVOICE_LINES, file=f"{invoices.file} (line_items_json)", format="nested json",
        key=("invoice_id", "line_number"), records=lines, frame=pd.json_normalize(lines) if lines else pd.DataFrame(),
        error=invoices.error,
    )
    return ds, per_invoice


def load_all(config: PipelineConfig) -> tuple[dict[str, RawDataset], list[int | None]]:
    data = {name: load_dataset(name, config) for name in config.sources}
    per_invoice: list[int | None] = []
    if "invoices" in data:
        data[INVOICE_LINES], per_invoice = explode_invoice_lines(data["invoices"])
    return data, per_invoice


# ── value helpers ────────────────────────────────────────────────────────────────


def value_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _try(parser: Any, value: Any) -> Any:
    try:
        return parser(value)
    except (ValueError, TypeError, OverflowError):
        return None


def to_float(value: Any) -> float:
    number = _try(parse_float, value)
    return math.nan if number is None or isinstance(value, bool) else float(number)


def classify_date_format(value: Any) -> str | None:
    text = str(value).strip()
    return next((name for name, pattern in DATE_FORMATS if pattern.match(text)), None)


def is_bool_token(value: Any) -> bool:
    return isinstance(value, bool) or _try(parse_bool, value) is not None


def id_shape(value: Any) -> str:
    """``PRD-000123`` -> ``PRD-<6>``: identifiers of one entity should share a shape."""
    text = str(value).strip().upper()
    return _TRAILING_ID.sub(lambda m: f"<{len(m.group())}>", text)


def _hashable(value: Any) -> tuple[str, Any]:
    return (value_kind(value), dumps(value) if isinstance(value, (list, dict)) else value)


def infer_type(present: list[Any]) -> tuple[str, float]:
    """Inferred type of the non-missing values and the share of values that fit it."""
    if not present:
        return "empty", 0.0
    kinds = Counter(value_kind(v) for v in present)
    if len(kinds) == 1 and "string" not in kinds:
        return next(iter(kinds)), 1.0
    if set(kinds) <= {"integer", "float"}:
        return "float", 1.0
    strings = [str(v) for v in present]
    tests = (
        ("boolean_string", is_bool_token),
        ("numeric_string", lambda s: not math.isnan(to_float(s))),
        ("date_string", lambda s: classify_date_format(s) is not None),
    )
    for name, test in tests:
        rate = sum(map(test, strings)) / len(strings)
        if rate >= 0.9:
            return (name if set(kinds) == {"string"} else f"mixed:{name}"), round(rate, 4)
    return ("string" if set(kinds) == {"string"} else "mixed"), 1.0


def _r(value: float | None) -> float | None:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else round(float(value), 4)


def numeric_stats(numbers: list[float]) -> dict[str, Any]:
    s = pd.Series(numbers, dtype="float64").dropna()
    if s.empty:
        return {"count": 0}
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    outliers = int(((s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)).sum()) if iqr > 0 else 0
    return {
        "count": int(s.size), "min": _r(s.min()), "max": _r(s.max()), "mean": _r(s.mean()),
        "median": _r(s.median()), "std": _r(s.std()) if s.size > 1 else None,
        "negatives": int((s < 0).sum()), "zeros": int((s == 0).sum()), "outliers_iqr": outliers,
    }


def date_stats(strings: list[str], as_of: date) -> dict[str, Any]:
    formats: Counter[str] = Counter()
    dates: list[date] = []
    impossible: list[str] = []
    for text in strings:
        fmt = classify_date_format(text) or "unrecognised"
        formats[fmt] += 1
        parsed = _try(parse_date, text)
        if parsed is None:
            impossible.append(text)
        else:
            dates.append(parsed)
    return {
        "formats": dict(sorted(formats.items())),
        "min": min(dates).isoformat() if dates else None,
        "max": max(dates).isoformat() if dates else None,
        "unparseable": len(impossible),
        "unparseable_samples": sorted(set(impossible))[:5],
        "after_as_of": sum(d > as_of for d in dates),
        "before_1900": sum(d.year < 1900 for d in dates),
    }


def text_issues(strings: list[str]) -> dict[str, int]:
    issues = {
        "non_ascii": sum(any(ord(c) > 127 for c in s) for s in strings),
        "mojibake": sum(bool(_MOJIBAKE.search(s)) for s in strings),
        "replacement_char": sum("\ufffd" in s for s in strings),
        "control_chars": sum(any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in s) for s in strings),
        "untrimmed": sum(s != s.strip() for s in strings),
        "not_nfc": sum(unicodedata.normalize("NFC", s) != s for s in strings),
    }
    return {k: v for k, v in issues.items() if v}


def spelling_variants(strings: list[str]) -> dict[str, dict[str, int]]:
    """Group values that differ only in case, spaces, ``_`` or ``-``; keep groups with >1 spelling."""
    groups: dict[str, Counter[str]] = {}
    for s in strings:
        groups.setdefault(re.sub(r"[\s_\-]+", "", s.strip().lower()), Counter())[s] += 1
    return {k: dict(sorted(c.items())) for k, c in sorted(groups.items()) if len(c) > 1}


def profile_column(series: pd.Series, as_of: date) -> dict[str, Any]:
    values = series.tolist()
    total = len(values)
    nulls = sum(v is None or (isinstance(v, float) and math.isnan(v)) for v in values)
    placeholders = sum(isinstance(v, str) and is_null(v) for v in values)
    present = [v for v in values if not is_null(v)]
    inferred, fit = infer_type(present)
    unique = len({_hashable(v) for v in present})
    out: dict[str, Any] = {
        "inferred_type": inferred,
        "type_fit": fit,
        "python_types": dict(sorted(Counter(value_kind(v) for v in present).items())),
        "nulls": nulls,
        "placeholders": placeholders,
        "missing_pct": round(100 * (nulls + placeholders) / total, 2) if total else 0.0,
        "unique": unique,
        "cardinality": round(unique / len(present), 4) if present else 0.0,
    }
    base = inferred.removeprefix("mixed:")
    if base in ("integer", "float", "numeric_string"):
        out["numeric"] = numeric_stats([to_float(v) for v in present])
    if base == "date_string":
        out["dates"] = date_stats([str(v) for v in present], as_of)
    if base in ("boolean", "boolean_string"):
        out["boolean_spellings"] = dict(sorted(Counter(repr(v) for v in present).items()))
        out["invalid_booleans"] = sum(not is_bool_token(v) for v in present)
    strings = [v for v in present if isinstance(v, str)]
    if strings:
        if issues := text_issues(strings):
            out["text_issues"] = issues
        if base == "string" and unique <= CATEGORICAL_MAX_UNIQUE:
            out["top_values"] = [[str(k[1]), n] for k, n in sorted(
                Counter(_hashable(v) for v in present).items(), key=lambda kv: (-kv[1], str(kv[0][1]))
            )[:TOP_VALUES]]
            if variants := spelling_variants(strings):
                out["spelling_variants"] = variants
    return out


# ── accessors used by the checks ─────────────────────────────────────────────────


def col(ds: RawDataset, name: str) -> pd.Series:
    if name in ds.frame.columns:
        return ds.frame[name]
    return pd.Series([None] * len(ds.frame), index=ds.frame.index, dtype=object)


def num(ds: RawDataset, name: str) -> pd.Series:
    return col(ds, name).map(to_float).astype("float64")


def ids(ds: RawDataset, name: str) -> pd.Series:
    return col(ds, name).map(lambda v: None if is_null(v) else str(v).strip().upper())


def dates(ds: RawDataset, name: str) -> pd.Series:
    return col(ds, name).map(lambda v: _try(parse_date, v))


def datetimes(ds: RawDataset, name: str) -> pd.Series:
    return pd.to_datetime(col(ds, name).map(lambda v: _try(parse_datetime, v)), errors="coerce")


def token(ds: RawDataset, name: str) -> pd.Series:
    return col(ds, name).map(lambda v: None if is_null(v) else re.sub(r"[\s\-]+", "_", str(v).strip().lower()))


def missing(ds: RawDataset, name: str) -> pd.Series:
    return col(ds, name).map(is_null).astype(bool)


def record_ids(ds: RawDataset) -> pd.Series:
    parts = [ids(ds, k).fillna("∅") for k in ds.key]
    return parts[0] if len(parts) == 1 else parts[0].str.cat(parts[1:], sep="|")


class Checks:
    """Collects findings for one dataset; samples default to the record's key."""

    def __init__(self, ds: RawDataset):
        self.ds, self.ids, self.findings = ds, record_ids(ds), []

    def add(
        self, check: str, mask: pd.Series, severity: str, kind: str, description: str, recommendation: str,
        rule: str | None = None, samples: pd.Series | None = None,
    ) -> None:
        mask = mask.fillna(False).astype(bool)
        source = self.ids if samples is None else samples
        picked = sorted({str(v) for v in source[mask] if v is not None and not pd.isna(v)})[:5]
        self.findings.append(Finding(self.ds.name, check, severity, kind, int(mask.sum()), len(mask),
                                     description, recommendation, rule, picked))


def _close(a: pd.Series, b: pd.Series, tolerance: float = MONEY_TOLERANCE) -> pd.Series:
    """True where both are present and differ by more than ``tolerance``."""
    return (a - b).abs().gt(tolerance) & a.notna() & b.notna()


# ── generic checks (every dataset) ───────────────────────────────────────────────


def generic_checks(ds: RawDataset, columns: dict[str, dict[str, Any]]) -> list[Finding]:
    c = Checks(ds)
    content = pd.Series([dumps(r) for r in ds.records], index=ds.frame.index, dtype=object)
    c.add("exact_duplicate_records", content.duplicated(), "Critical" if ds.name in FACT_TABLES else "High",
          VIOLATION, "record identical to an earlier one (all fields)",
          "Drop repeats, keep the first; log what was removed", f"{ds.name}.duplicate")
    key = c.ids
    conflicting = key.duplicated(keep=False) & ~content.duplicated(keep=False) & ~missing(ds, ds.key[0])
    c.add("conflicting_duplicate_keys", conflicting, "Critical", VIOLATION,
          "same primary key, different content", "Keep the latest version by updated_at", f"{ds.name}.duplicate")

    raw_key = ids(ds, ds.key[0])
    shapes = raw_key.dropna().map(id_shape)
    if not shapes.empty and len(ds.key) == 1:  # missing keys are reported under missing_required
        modal = shapes.value_counts().sort_index().idxmax()
        c.add("malformed_primary_key", raw_key.notna() & raw_key.map(id_shape).ne(modal), "High", VIOLATION,
              f"primary key not in the dominant shape {modal}", "Quarantine: the record cannot be referenced",
              REQUIRED.get(ds.name, {}).get(ds.key[0]))

    for name, rule in REQUIRED.get(ds.name, {}).items():
        severity = "Critical" if name in ds.key else "High"
        c.add(f"missing_required:{name}", missing(ds, name), severity, VIOLATION, f"required field `{name}` missing",
              "Quarantine if it is the key; otherwise null and flag", rule)

    for name, profile in columns.items():
        if name not in ds.frame.columns:
            continue
        values = col(ds, name)
        # CSV cannot express null, so an empty cell there is just "missing", not a placeholder.
        empty_is_null = ds.format == "csv"
        placeholder = values.map(lambda v: isinstance(v, str) and is_null(v) and not (empty_is_null and not v.strip()))
        c.add(f"placeholder_nulls:{name}", placeholder, "Low", VIOLATION,
              "missing value encoded as text ('N/A', 'Unknown', 'UNLISTED', or '' in JSON)",
              "Map placeholder tokens to null on read", samples=values)
        if dates_ := profile.get("dates"):
            fmts = dates_["formats"]
            if len(fmts) > 1:
                dominant = max(sorted(fmts), key=fmts.get)
                fmt = values.map(lambda v: None if is_null(v) else classify_date_format(v) or "unrecognised")
                c.add(f"mixed_date_formats:{name}", fmt.notna() & fmt.ne(dominant), "Medium", VIOLATION,
                      f"{len(fmts)} date formats ({', '.join(sorted(fmts))}); dominant {dominant}",
                      "Parse with explicit per-format rules, never format inference", samples=values)
            unparseable = values.map(lambda v: not is_null(v) and _try(parse_date, v) is None)
            c.add(f"impossible_dates:{name}", unparseable, "High", VIOLATION,
                  "date that cannot exist or cannot be parsed", "Null and flag", samples=values)
        if profile.get("invalid_booleans"):
            c.add(f"invalid_booleans:{name}", values.map(lambda v: not is_null(v) and not is_bool_token(v)),
                  "High", VIOLATION, "value is not a recognisable boolean", "Null and flag", samples=values)
        if len(profile.get("boolean_spellings", {})) > 2:
            canonical = values.map(lambda v: isinstance(v, bool) or str(v) in ("true", "false"))
            c.add(f"boolean_spellings:{name}", ~canonical & ~values.map(is_null), "Medium", VIOLATION,
                  f"{len(profile['boolean_spellings'])} spellings of true/false",
                  "Map tokens to true/false", f"{ds.name}.boolean" if f"{ds.name}.boolean" in RULES else None,
                  samples=values)
        if variants := profile.get("spelling_variants"):
            minority = {s for group in variants.values() for s in sorted(group, key=lambda k: (-group[k], k))[1:]}
            c.add(f"inconsistent_categories:{name}", values.map(lambda v: isinstance(v, str) and v in minority),
                  "Medium", VIOLATION, f"{len(variants)} value(s) spelled several ways (case/space/underscore)",
                  "Normalise to lower snake_case", samples=values)
        if issues := profile.get("text_issues"):
            bad = {k: v for k, v in issues.items() if k != "non_ascii"}
            if bad:
                broken = values.map(lambda v: isinstance(v, str) and bool(text_issues([v]).keys() - {"non_ascii"}))
                c.add(f"text_encoding:{name}", broken, "Low", VIOLATION,
                      "encoding / whitespace problems: " + ", ".join(f"{k}={v}" for k, v in sorted(bad.items())),
                      "Trim and NFC-normalise text", samples=values)
        if name.lower().endswith("email"):
            invalid = values.map(lambda v: not is_null(v) and _try(normalize_email, v) is None)
            c.add(f"invalid_email:{name}", invalid, "High", VIOLATION, "not a syntactically valid email address",
                  "Null and flag", "customers.invalid_email" if ds.name == "customers" else None, samples=values)

    expected = ds.envelope.get("total_count")
    if isinstance(expected, int) and expected > 0:
        c.findings.append(Finding(ds.name, "partial_extract", "Low", ANOMALY, max(expected - len(ds.records), 0),
                                  expected, f"envelope total_count={expected}, file holds {len(ds.records)} records",
                                  "Report completeness; confirm with the source whether this is a sample"))
    return c.findings


# ── domain checks ────────────────────────────────────────────────────────────────


def check_products(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    p = data["products"]
    c = Checks(p)
    first = ~c.ids.duplicated()
    sku = ids(p, "sku")
    shared = first & sku.notna() & sku.map(sku[first].value_counts()).gt(1)
    c.add("duplicate_sku", shared, "Medium", ANOMALY, "SKU shared by different product_ids",
          "Keep product_id as the key; flag, do not merge", "products.sku_conflict", sku)
    price, cost = num(p, "price"), num(p, "cost")
    c.add("invalid_price", ~missing(p, "price") & (price.isna() | price.le(0)), "High", VIOLATION,
          "price is not a positive number", "Null and flag; do not guess", "products.invalid_price")
    c.add("cost_above_price", cost.gt(price) & price.gt(0), "Medium", ANOMALY, "unit cost higher than list price",
          "Flag for review")
    c.add("negative_weight", num(p, "weight_kg").lt(0), "High", VIOLATION, "weight_kg < 0", "Null and flag",
          "products.invalid_weight")
    c.add("negative_stock", num(p, "stock_quantity").lt(0), "High", VIOLATION, "stock_quantity < 0",
          "Null and flag", "products.negative_stock")
    rating = num(p, "rating")
    c.add("rating_out_of_range", rating.lt(0) | rating.gt(5), "Medium", VIOLATION, "rating outside 0–5",
          "Null and flag", "products.invalid_rating")
    currency = col(p, "currency")
    c.add("currency_not_iso", currency.map(lambda v: not is_null(v) and not re.fullmatch(r"[A-Z]{3}", str(v))),
          "Medium", VIOLATION, "currency written as an alias ('$', 'usd', 'US Dollar')", "Map aliases to ISO 4217",
          "products.currency", currency)
    created, updated = dates(p, "created_date"), dates(p, "last_updated")
    c.add("created_after_updated", pd.Series([bool(a and b and a > b) for a, b in zip(created, updated)], index=p.frame.index),
          "Medium", VIOLATION, "created_date later than last_updated", "Flag", "products.created_after_updated")
    return c.findings


def check_customers(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    cu = data["customers"]
    c = Checks(cu)
    first = ~c.ids.duplicated()
    email = col(cu, "email").map(lambda v: _try(normalize_email, v))
    shared = first & email.notna() & email.map(email[first].value_counts()).gt(1)
    c.add("shared_email", shared, "Medium", ANOMALY, "email used by more than one customer_id",
          "Flag; do not merge (names, phones and dates differ)", "customers.shared_email", email)
    for name, rule in (("registration_date", "customers.future_registration"),
                       ("last_purchase_date", "customers.future_last_purchase")):
        future = dates(cu, name).map(lambda d: d is not None and d > as_of)
        c.add(f"future_{name}", future, "High", VIOLATION, f"{name} after as_of_date {as_of}", "Null and flag", rule)
    age = num(cu, "age")
    c.add("age_out_of_range", age.lt(0) | age.gt(120), "High", VIOLATION, "age outside 0–120",
          "Recompute from date_of_birth; else null and flag", "customers.invalid_age")
    c.add("negative_total_spend", num(cu, "total_spend").lt(0), "Medium", VIOLATION, "total_spend < 0",
          "Flag; recompute aggregates from transactions", "customers.negative_total_spend")
    reg, created = dates(cu, "registration_date"), dates(cu, "metadata.created_at")
    disagree = pd.Series([bool(a and b and a != b) for a, b in zip(reg, created)], index=cu.frame.index)
    c.add("registration_vs_metadata", disagree, "Medium", ANOMALY,
          "registration_date differs from metadata.created_at (see date-convention evidence)",
          "Take the ISO metadata timestamp", "customers.registration_from_metadata", col(cu, "registration_date"))
    return c.findings


def _catalog(data: dict[str, RawDataset], column: str) -> pd.Series:
    p = data.get("products")
    if p is None or not p.ok:
        return pd.Series(dtype="float64")
    values = pd.Series(num(p, column).values, index=ids(p, "product_id").values)
    return values[~values.index.duplicated()]


def check_transactions(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    t = data["transactions"]
    c = Checks(t)
    qty = num(t, "quantity")
    c.add("zero_quantity", qty.eq(0), "High", VIOLATION, "quantity is 0 (shipping-only line, nothing sold)",
          "Quarantine", "transactions.zero_quantity")
    c.add("returns", qty.lt(0), "Low", BUSINESS, "negative quantity: a return, not an error",
          "Keep; mark is_return and treat amounts as refunds", "transactions.return")
    sign = qty.map(lambda q: -1.0 if q < 0 else 1.0)
    expected = sign * (num(t, "subtotal") - num(t, "discount_amount") + num(t, "tax_amount")) + num(t, "shipping_cost")
    c.add("amount_mismatch", _close(num(t, "total_amount"), expected), "Critical", VIOLATION,
          "total_amount != sign × (subtotal − discount + tax) + shipping", "Flag", "transactions.amount_mismatch")
    c.add("negative_shipping_on_sale", num(t, "shipping_cost").lt(0) & qty.gt(0), "Medium", VIOLATION,
          "negative shipping cost on a sale", "Flag", "transactions.negative_shipping")
    notes = col(t, "notes")
    c.add("junk_notes", notes.map(lambda v: isinstance(v, str) and bool(_JUNK_NOTE.match(v.strip()))), "Low",
          VIOLATION, "placeholder notes ('xxxxxxxxxx')", "Null and flag", "transactions.junk_notes")
    c.add("future_transaction_date", dates(t, "transaction_date").map(lambda d: d is not None and d > as_of),
          "High", VIOLATION, f"transaction after as_of_date {as_of}", "Flag")
    currency = col(t, "currency").map(lambda v: _try(normalize_currency, v))
    c.add("currency_alias", col(t, "currency").map(lambda v: not is_null(v) and not re.fullmatch(r"[A-Z]{3}", str(v))),
          "Medium", VIOLATION, "currency written as an alias", "Map to ISO 4217", "transactions.currency",
          col(t, "currency"))
    price = ids(t, "product_id").map(_catalog(data, "price"))
    matches = (num(t, "unit_price").abs() - price).abs().le(0.01)
    c.add("currency_label_contradicted", currency.notna() & currency.ne("USD") & matches, "Critical", ANOMALY,
          "non-USD label, but unit_price equals the USD catalog price",
          "Treat amounts as USD; keep source label; never FX-convert", "transactions.currency_relabelled", currency)
    return c.findings


def check_vendors(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    v = data["vendors"]
    c = Checks(v)
    status = token(v, "status")
    products = data.get("products")
    supplied = Counter(ids(products, "vendor_id").dropna()) if products and products.ok else Counter()
    sales: Counter[str] = Counter()
    if products and products.ok and data.get("transactions") and data["transactions"].ok:
        owner = dict(zip(ids(products, "product_id"), ids(products, "vendor_id")))
        sales = Counter(owner.get(pid) for pid in ids(data["transactions"], "product_id"))
    label = pd.Series([f"{vid} ({s}: {supplied[vid]} products, {sales[vid]} sales)"
                       for vid, s in zip(c.ids, status)], index=v.frame.index)
    c.add("non_active_vendor_supplying", status.ne("active") & status.notna() & c.ids.map(lambda i: supplied[i] > 0),
          "Medium", BUSINESS, "pending/inactive vendor still has catalogue products and sales",
          "Keep; surface as a vendor-risk feature", samples=label)
    contact = pd.concat([missing(v, f"contact.{f}") for f in ("primary_name", "email", "phone")], axis=1)
    c.add("missing_contact", contact.all(axis=1), "High", VIOLATION, "no contact name, email or phone",
          "Flag", "vendors.missing_contact")
    c.add("incomplete_contact", contact.any(axis=1) & ~contact.all(axis=1), "Medium", VIOLATION,
          "contact details partially missing", "Flag", "vendors.incomplete_contact")
    reliability = num(v, "reliability_score")
    c.add("reliability_out_of_range", reliability.lt(0) | reliability.gt(100), "Medium", VIOLATION,
          "reliability_score outside 0–100", "Null and flag", "vendors.invalid_reliability")
    c.add("missing_reliability", missing(v, "reliability_score"), "Low", VIOLATION, "reliability_score missing",
          "Flag", "vendors.missing_reliability")
    return c.findings


def check_invoices(data: dict[str, RawDataset], as_of: date, per_invoice: list[int | None],
                   terms_days: dict[str, int]) -> list[Finding]:
    inv, lines = data["invoices"], data[INVOICE_LINES]
    c = Checks(inv)
    idx = inv.frame.index
    first = ~c.ids.duplicated()
    number = ids(inv, "invoice_number")
    c.add("duplicate_invoice_number", first & number.notna() & number.map(number[first].value_counts()).gt(1),
          "Medium", ANOMALY, "invoice_number shared by different invoice_ids (numbers are unique per vendor only)",
          "Key on invoice_id; flag", "invoices.duplicate_number", number)
    n_lines = pd.Series(per_invoice, index=idx, dtype="float64")
    c.add("unparseable_line_items", n_lines.isna(), "High", VIOLATION, "line_items_json is not valid JSON",
          "Flag; keep header", "invoices.unparseable_line_items")
    c.add("line_count_mismatch", _close(num(inv, "line_item_count"), n_lines, 0), "High", VIOLATION,
          "line_item_count != number of lines in line_items_json", "Flag", "invoices.line_count_mismatch")
    # Duplicate invoice rows repeat their lines; count each (invoice_id, line_number) once.
    once = ~record_ids(lines).duplicated()
    line_sum = num(lines, "line_total")[once].groupby(ids(lines, "invoice_id")[once]).sum()
    c.add("subtotal_vs_lines", _close(num(inv, "subtotal"), c.ids.map(line_sum)), "Critical", VIOLATION,
          "subtotal != sum of line totals", "Flag", "invoices.subtotal_mismatch")
    expected = num(inv, "subtotal") + num(inv, "tax_amount") + num(inv, "shipping_handling")
    c.add("total_mismatch", _close(num(inv, "total_amount"), expected), "Critical", VIOLATION,
          "total_amount != subtotal + tax + shipping", "Flag", "invoices.total_mismatch")
    c.add("balance_mismatch", _close(num(inv, "balance_due"), num(inv, "total_amount") - num(inv, "amount_paid")),
          "High", VIOLATION, "balance_due != total − amount_paid", "Flag", "invoices.balance_mismatch")
    invoiced, due, paid = dates(inv, "invoice_date"), dates(inv, "due_date"), dates(inv, "payment_date")
    days = col(inv, "payment_terms").map(lambda t: terms_days.get(str(t).strip().upper()))
    same_day = pd.Series([bool(a and b and a == b and (d or 0) > 0) for a, b, d in zip(invoiced, due, days)], index=idx)
    c.add("due_date_ignores_terms", same_day, "High", ANOMALY, "due_date equals invoice_date despite NET terms",
          "Derive due_date = invoice_date + NET days; keep source as src_due_date", "invoices.due_date_from_terms")
    c.add("paid_before_invoiced", pd.Series([bool(a and b and b < a) for a, b in zip(invoiced, paid)], index=idx),
          "High", VIOLATION, "payment_date earlier than invoice_date", "Flag", "invoices.paid_before_invoiced")
    status = token(inv, "payment_status")
    has_payment = ~missing(inv, "payment_date")
    conflict = (status.eq("paid") & ~has_payment) | (status.isin(["pending", "overdue"]) & has_payment)
    c.add("status_payment_conflict", conflict, "Medium", VIOLATION, "payment_status contradicts payment_date",
          "Flag", "invoices.status_payment_conflict")
    year = number.map(lambda n: int(m.group()) if n and (m := re.search(r"(19|20)\d{2}", n)) else None)
    mismatch = pd.Series([bool(y and d and y != d.year) for y, d in zip(year, invoiced)], index=idx)
    c.add("number_year_mismatch", mismatch, "Low", ANOMALY, "year embedded in invoice_number != invoice year",
          "Flag; do not derive dates from the number", "invoices.number_year_mismatch", number)
    # Currency: do line costs equal USD catalog costs whatever the label says?
    if len(lines.frame):
        cost = ids(lines, "product_id").map(_catalog(data, "cost"))
        checked = cost.notna()
        wrong = checked & (num(lines, "unit_cost").abs() - cost).abs().gt(0.01)
        per = pd.DataFrame({"checked": checked, "wrong": wrong}).groupby(ids(lines, "invoice_id")).sum()
        confirmed_ids = set(per.index[(per["checked"] > 0) & (per["wrong"] == 0)])
        currency = col(inv, "currency").map(lambda v: _try(normalize_currency, v))
        c.add("currency_label_contradicted", currency.notna() & currency.ne("USD") & c.ids.isin(confirmed_ids),
              "Critical", ANOMALY, "non-USD label, but every line cost equals the USD catalog cost",
              "Treat amounts as USD; keep source label; never FX-convert", "invoices.currency_relabelled", currency)
    return c.findings


def check_invoice_lines(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    lines = data[INVOICE_LINES]
    c = Checks(lines)
    qty, cost = num(lines, "quantity"), num(lines, "unit_cost")
    c.add("line_total_mismatch", _close(num(lines, "line_total"), qty * cost), "High", VIOLATION,
          "line_total != quantity × unit_cost", "Flag", "invoice_lines.line_total_mismatch")
    c.add("zero_quantity", qty.eq(0), "High", VIOLATION, "quantity is 0", "Flag", "invoice_lines.zero_quantity")
    c.add("missing_product", missing(lines, "product_id"), "Medium", ANOMALY,
          "line has no product_id: a non-catalogue item (service, freight) or a gap", "Keep; flag",
          "invoice_lines.missing_product")
    c.add("credit_lines", qty.lt(0), "Low", BUSINESS, "negative quantity: a credit line", "Keep; mark is_credit",
          "invoice_lines.credit")
    products, invoices = data.get("products"), data.get("invoices")
    if products and products.ok and invoices and invoices.ok:
        owner = dict(zip(ids(products, "product_id"), ids(products, "vendor_id")))
        billed_by = dict(zip(ids(invoices, "invoice_id"), ids(invoices, "vendor_id")))
        pv, iv = ids(lines, "product_id").map(owner), ids(lines, "invoice_id").map(billed_by)
        c.add("vendor_mismatch", pv.notna() & iv.notna() & pv.ne(iv), "Medium", ANOMALY,
              "product belongs to a different vendor than the invoice", "Flag", "invoice_lines.vendor_mismatch")
    return c.findings


def check_support_tickets(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    t = data["support_tickets"]
    c = Checks(t)
    score = num(t, "satisfaction_score")
    c.add("satisfaction_out_of_range", score.lt(1) | score.gt(5), "High", VIOLATION, "satisfaction_score outside 1–5",
          "Null and flag", "support_tickets.invalid_satisfaction")
    status = token(t, "status")
    done = status.isin(["resolved", "closed"])
    c.add("resolved_without_resolved_at", done & missing(t, "resolved_at"), "Medium", VIOLATION,
          "resolved/closed ticket has no resolved_at", "Flag", "support_tickets.missing_resolved_at")
    c.add("resolved_without_score", done & score.isna(), "Low", VIOLATION, "resolved/closed ticket has no score",
          "Flag", "support_tickets.missing_satisfaction")
    c.add("open_with_resolved_at", ~done & status.notna() & ~missing(t, "resolved_at"), "Medium", ANOMALY,
          "open ticket already has resolved_at", "Flag")
    created, resolved = datetimes(t, "created_at"), datetimes(t, "resolved_at")
    c.add("resolved_before_created", resolved.lt(created), "High", VIOLATION, "resolved_at earlier than created_at",
          "Flag", "support_tickets.resolved_before_created")
    return c.findings


def check_product_reviews(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    r = data["product_reviews"]
    c = Checks(r)
    rating = num(r, "rating")
    c.add("rating_out_of_range", rating.lt(1) | rating.gt(5), "High", VIOLATION, "rating outside 1–5",
          "Null and flag", "product_reviews.invalid_rating")
    c.add("missing_sentiment", missing(r, "sentiment"), "Low", VIOLATION, "sentiment missing", "Flag",
          "product_reviews.missing_sentiment")
    sentiment = token(r, "sentiment")
    c.add("rating_contradicts_sentiment", (rating.le(2) & sentiment.eq("positive")) | (rating.ge(4) & sentiment.eq("negative")),
          "Low", ANOMALY, "low rating with positive sentiment or the reverse", "Flag for review")
    tx = data.get("transactions")
    if tx and tx.ok:
        bought = set(zip(ids(tx, "customer_id"), ids(tx, "product_id")))
        claimed = col(r, "verified_purchase").map(lambda v: _try(parse_bool, v) is True)
        unsupported = [pair not in bought for pair in zip(ids(r, "customer_id"), ids(r, "product_id"))]
        c.add("unverified_purchase_claim", claimed & pd.Series(unsupported, index=r.frame.index), "Medium", ANOMALY,
              "verified_purchase is true but the customer never bought the product", "Flag",
              "product_reviews.unverified_purchase")
    return c.findings


def check_call_transcripts(data: dict[str, RawDataset], as_of: date) -> list[Finding]:
    calls = data["call_transcripts"]
    c = Checks(calls)
    start, end = datetimes(calls, "call_start"), datetimes(calls, "call_end")
    actual = (end - start).dt.total_seconds()
    c.add("negative_duration", actual.lt(0), "High", VIOLATION, "call_end earlier than call_start", "Flag",
          "call_transcripts.invalid_duration")
    c.add("duration_mismatch", _close(num(calls, "duration_seconds"), actual, 1), "Medium", VIOLATION,
          "duration_seconds != call_end − call_start", "Recompute from timestamps", "call_transcripts.duration_recomputed")
    c.add("missing_ticket_reference", missing(calls, "ticket_id"), "Medium", VIOLATION, "call has no ticket_id",
          "Flag", "call_transcripts.missing_ticket_ref")
    c.add("missing_agent", missing(calls, "agent_id"), "Low", VIOLATION, "call has no agent_id", "Flag",
          "call_transcripts.missing_agent")
    quality = num(calls, "quality_score")
    c.add("quality_out_of_range", quality.lt(0) | quality.gt(100), "Medium", VIOLATION, "quality_score outside 0–100",
          "Null and flag", "call_transcripts.invalid_quality_score")

    empty, bad_ts, unordered, beyond, incomplete, third_party = [], [], [], [], [], []
    for utterances, length in zip(col(calls, "utterances"), actual):
        items = utterances if isinstance(utterances, list) else []
        offsets = [_try(parse_hms, u.get("timestamp")) for u in items if isinstance(u, dict)]
        seconds = [o.total_seconds() for o in offsets if o is not None]
        empty.append(not items)
        bad_ts.append(any(o is None for o in offsets))
        unordered.append(seconds != sorted(seconds))
        beyond.append(bool(seconds) and not math.isnan(length) and max(seconds) > length)
        incomplete.append(any(not isinstance(u, dict) or is_null(u.get("text")) or is_null(u.get("speaker"))
                              for u in items))
        third_party.append(any(isinstance(u, dict) and not is_null(u.get("speaker"))
                               and str(u["speaker"]).strip().lower() not in ("agent", "customer") for u in items))
    for check, flags, severity, kind, text, rule in (
        ("missing_utterances", empty, "Medium", VIOLATION, "no utterances", "call_transcripts.missing_utterances"),
        ("invalid_utterance_timestamp", bad_ts, "Medium", VIOLATION, "utterance timestamp not HH:MM:SS", None),
        ("utterances_out_of_order", unordered, "Medium", VIOLATION, "utterance timestamps not ascending",
         "call_transcripts.utterances_sorted"),
        ("utterance_after_call_end", beyond, "Low", ANOMALY, "utterance offset beyond call duration", None),
        ("incomplete_speaker_info", incomplete, "Medium", VIOLATION, "utterance without a speaker or text", None),
        ("third_party_speakers", third_party, "Low", BUSINESS,
         "utterances by a speaker other than agent/customer (system prompts, supervisors)", None),
    ):
        c.add(check, pd.Series(flags, index=calls.frame.index, dtype=bool), severity, kind, text, "Flag", rule)
    return c.findings


DOMAIN_CHECKS = {
    "products": check_products,
    "customers": check_customers,
    "transactions": check_transactions,
    "vendors": check_vendors,
    INVOICE_LINES: check_invoice_lines,
    "support_tickets": check_support_tickets,
    "product_reviews": check_product_reviews,
    "call_transcripts": check_call_transcripts,
}


# ── cross-dataset ────────────────────────────────────────────────────────────────


def check_relationship(rel: Relation, data: dict[str, RawDataset]) -> tuple[dict[str, Any], Finding] | None:
    child, parent = data.get(rel.child), data.get(rel.parent)
    if not (child and parent and child.ok and parent.ok):
        return None
    fk = ids(child, rel.column)
    known = set(ids(parent, rel.column).dropna())
    orphan = fk.notna() & ~fk.isin(known)
    patterns = Counter(fk[orphan].map(id_shape))
    summary = {
        "child": rel.child, "column": rel.column, "parent": rel.parent, "rule": rel.rule,
        "rows": len(fk), "null_references": int(fk.isna().sum()), "orphans": int(orphan.sum()),
        "orphan_pct": round(100 * orphan.sum() / len(fk), 2) if len(fk) else 0.0,
        "distinct_orphan_ids": int(fk[orphan].nunique()),
        "orphan_patterns": dict(sorted(patterns.items(), key=lambda kv: (-kv[1], kv[0]))[:5]),
        "samples": sorted(set(fk[orphan]))[:5],
    }
    # Orphan sales distort revenue attribution; orphan invoices/lines distort vendor spend.
    severity = "Critical" if rel.child == "transactions" else "High" if rel.child in FACT_TABLES else "Medium"
    finding = Finding(rel.child, f"orphan:{rel.column}->{rel.parent}", severity, VIOLATION, summary["orphans"],
                      len(fk), f"{rel.column} not found in {rel.parent}",
                      "Keep and flag (revenue stays complete); the graph draws no edge", rel.rule, summary["samples"])
    return summary, finding


# ── evidence for transformation decisions ────────────────────────────────────────


def date_convention_evidence(data: dict[str, RawDataset]) -> dict[str, Any]:
    """Customers carry each registration date twice: free-form and ISO in metadata.created_at.

    Agreement per format family shows how ambiguous ``nn/nn`` and ``nn-nn`` dates must be read.
    """
    cu = data.get("customers")
    if not cu or not cu.ok:
        return {}
    out: dict[str, dict[str, int]] = {}
    for raw, created in zip(col(cu, "registration_date"), dates(cu, "metadata.created_at")):
        if is_null(raw) or created is None:
            continue
        fmt = classify_date_format(raw) or "unrecognised"
        bucket = out.setdefault(fmt, {"values": 0, "agree": 0, "agree_if_day_first": 0, "agree_if_month_first": 0})
        bucket["values"] += 1
        bucket["agree"] += _try(parse_date, raw) == created
        parts = re.split(r"[/-]", str(raw).strip())
        if fmt in ("nn/nn/yyyy", "nn-nn-yyyy"):
            a, b, y = map(int, parts)
            bucket["agree_if_day_first"] += _safe_date(y, b, a) == created
            bucket["agree_if_month_first"] += _safe_date(y, a, b) == created
    return dict(sorted(out.items()))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def currency_evidence(data: dict[str, RawDataset]) -> dict[str, Any]:
    """Share of amounts equal to the USD catalog value, per source currency label."""
    out: dict[str, Any] = {}
    t = data.get("transactions")
    price = _catalog(data, "price")
    if t and t.ok and not price.empty:
        label = col(t, "currency").map(lambda v: _try(normalize_currency, v) or "unknown")
        ref = ids(t, "product_id").map(price)
        match = (num(t, "unit_price").abs() - ref).abs().le(0.01)
        frame = pd.DataFrame({"label": label, "checked": ref.notna(), "match": match & ref.notna()})
        out["transactions_unit_price_vs_catalog_price"] = {
            k: {"checked": int(g["checked"].sum()), "match": int(g["match"].sum())}
            for k, g in sorted(frame.groupby("label"))
        }
    lines, inv, cost = data.get(INVOICE_LINES), data.get("invoices"), _catalog(data, "cost")
    if lines and inv and inv.ok and len(lines.frame) and not cost.empty:
        labels = dict(zip(ids(inv, "invoice_id"), col(inv, "currency").map(lambda v: _try(normalize_currency, v) or "unknown")))
        ref = ids(lines, "product_id").map(cost)
        match = (num(lines, "unit_cost").abs() - ref).abs().le(0.01)
        frame = pd.DataFrame({"label": ids(lines, "invoice_id").map(labels), "checked": ref.notna(), "match": match & ref.notna()})
        out["invoice_line_unit_cost_vs_catalog_cost"] = {
            k: {"checked": int(g["checked"].sum()), "match": int(g["match"].sum())}
            for k, g in sorted(frame.groupby("label"))
        }
    return out


# ── profile assembly ─────────────────────────────────────────────────────────────


def profile_dataset(ds: RawDataset, as_of: date) -> dict[str, Any]:
    info: dict[str, Any] = {
        "file": ds.file, "format": ds.format, "status": "ok" if ds.ok else "failed", "error": ds.error,
        "file_bytes": ds.file_bytes, "sha256": ds.sha256, "primary_key": list(ds.key),
        "rows": len(ds.frame), "columns": len(ds.frame.columns),
        "memory_bytes": int(ds.frame.memory_usage(deep=True).sum()) if len(ds.frame.columns) else 0,
        "envelope": {k: v for k, v in sorted(ds.envelope.items()) if not isinstance(v, (list, dict))},
    }
    if ds.ok:
        info["column_profiles"] = {name: profile_column(ds.frame[name], as_of) for name in ds.frame.columns}
    return info


def build_profile(config: PipelineConfig) -> dict[str, Any]:
    as_of = config.run.as_of_date
    data, per_invoice = load_all(config)
    datasets, findings, relationships = {}, [], []
    for name, ds in data.items():
        log = logger.bind(stage="profile", source=name)
        datasets[name] = profile_dataset(ds, as_of)
        if not ds.ok:
            continue
        findings += generic_checks(ds, datasets[name]["column_profiles"])
        if name == "invoices":
            findings += check_invoices(data, as_of, per_invoice, config.reference.payment_terms_days)
        elif name in DOMAIN_CHECKS:
            findings += DOMAIN_CHECKS[name](data, as_of)
        log.info("Profiled", rows=datasets[name]["rows"], columns=datasets[name]["columns"])
    for rel in RELATIONS:
        if result := check_relationship(rel, data):
            relationships.append(result[0])
            findings.append(result[1])

    issues = [f for f in findings if f.count]
    return {
        "as_of_date": as_of.isoformat(),
        "datasets": datasets,
        "relationships": relationships,
        "evidence": {"date_convention": date_convention_evidence(data), "currency_labels": currency_evidence(data)},
        "findings": [f.as_dict() for f in issues],
        "checks_without_issues": sorted(f"{f.dataset}:{f.check}" for f in findings if not f.count),
        "summary": {
            "datasets": len(datasets),
            "records": sum(d["rows"] for n, d in datasets.items() if n != INVOICE_LINES),
            "invoice_lines": datasets.get(INVOICE_LINES, {}).get("rows", 0),
            "checks_run": len(findings),
            "checks_with_issues": len(issues),
            "by_severity": {s: sum(f.severity == s for f in issues) for s in SEVERITIES},
            "by_kind": {k: sum(f.kind == k for f in issues) for k in KIND_LABEL},
        },
    }


# ── Markdown ─────────────────────────────────────────────────────────────────────


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    def cell(v: Any) -> str:
        return "–" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

    return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|",
            *("| " + " | ".join(cell(c) for c in row) + " |" for row in rows), ""]


def _sorted_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(findings, key=lambda f: (rank[f["severity"]], -f["count"], f["dataset"], f["check"]))


def render_markdown(profile: dict[str, Any]) -> str:
    """Short summary (about three printed pages). Full detail is in the JSON report."""
    s, datasets, findings = profile["summary"], profile["datasets"], _sorted_findings(profile["findings"])
    sources = [n for n in datasets if n != INVOICE_LINES]
    out = [
        "# Raw Data Profile", "",
        f"Raw files profiled read-only before any cleaning; reference date **{profile['as_of_date']}**. "
        f"**{len(sources)} files, {s['records']:,} records** (+{s['invoice_lines']:,} invoice lines nested in "
        f"`invoices.csv`). **{s['checks_run']} checks, {s['checks_with_issues']} with findings**: "
        + ", ".join(f"{n} {sev}" for sev, n in s["by_severity"].items())
        + f"; {s['by_kind'][VIOLATION]} violations, {s['by_kind'][ANOMALY]} anomalies, "
        f"{s['by_kind'][BUSINESS]} business exceptions. Every figure, column statistic and sample id is in "
        f"`{REPORT_NAME}.json`.", "",
        "## Datasets", "",
    ]
    out += _table(
        ["Dataset", "Rows", "Cols", "Critical", "High", "Medium/Low"],
        [[f"`{n}`" + ("" if d["status"] == "ok" else " (failed)"), d["rows"], d["columns"],
          *(sum(f["severity"] == sev for f in findings if f["dataset"] == n) for sev in ("Critical", "High")),
          sum(f["severity"] in ("Medium", "Low") for f in findings if f["dataset"] == n)]
         for n, d in datasets.items()],
    )

    out += ["## Critical and high findings", ""]
    out += _table(
        ["Sev.", "Dataset", "Finding", "Records", "Pipeline rule"],
        [[f["severity"][:4], f["dataset"], f"{f['description']}" + (" *(anomaly)*" if f["kind"] == ANOMALY else ""),
          f"{f['count']:,} ({f['pct']:g}%)", f"`{f['rule']}`" if f["rule"] else "–"]
         for f in findings if f["severity"] in ("Critical", "High")],
    )

    orphans = [r for r in profile["relationships"] if r["orphans"]]
    if orphans:
        out += ["## Broken references", ""]
        out += _table(
            ["Reference", "Orphans", "Example ids"],
            [[f"{r['child']}.{r['column']} → {r['parent']}", f"{r['orphans']:,} ({r['orphan_pct']:g}%)",
              ", ".join(f"`{x}`" for x in r["samples"][:2])] for r in orphans],
        )
        clean = [f"{r['child']}.{r['column']}" for r in profile["relationships"] if not r["orphans"]]
        if clean:
            out += [f"No orphans: {', '.join(clean)}.", ""]

    out += _evidence_section(profile["evidence"])

    business = [f for f in findings if f["kind"] == BUSINESS]
    if business:
        out += ["## Kept as business events, not errors", ""]
        out += [f"- {f['dataset']}: {f['description']} ({f['count']:,})" for f in business]
        out += [""]
    medium = sorted((f for f in findings if f["severity"] in ("Medium", "Low") and f["kind"] != BUSINESS),
                    key=lambda f: (-f["pct"], f["dataset"], f["check"]))
    out += ["## Medium and low", "",
            f"{len(medium)} findings, mostly representations to standardise. Largest by share: "
            + "; ".join(f"{f['dataset']} `{f['check']}` {f['pct']:g}%" for f in medium[:10]) + ".", ""]
    return "\n".join(out)


def _evidence_section(evidence: dict[str, Any]) -> list[str]:
    out = ["## Evidence behind key decisions", ""]
    if conv := evidence.get("date_convention"):
        ambiguous = {f: b for f, b in conv.items() if f.startswith("nn")}
        out += ["**Ambiguous dates** vs the ISO `metadata.created_at` copy in customers:", ""]
        out += _table(["Format", "Values", "Day-first agrees", "Month-first agrees"],
                      [[f, b["values"], b["agree_if_day_first"], b["agree_if_month_first"]] for f, b in ambiguous.items()])
        if epoch := conv.get("epoch_seconds"):
            out += [f"Epoch values agree {epoch['agree']}/{epoch['values']} (one day early): metadata wins.", ""]
    labels = evidence.get("currency_labels", {})
    tx = labels.get("transactions_unit_price_vs_catalog_price", {})
    lines = labels.get("invoice_line_unit_cost_vs_catalog_cost", {})
    if tx or lines:
        def share(v: dict[str, int] | None) -> str:
            return f"{v['match']:,}/{v['checked']:,}" if v else "–"

        out += ["**Currency labels**: amounts equal to the USD catalog value, by label. Labels carry no "
                "meaning, so everything is treated as USD and nothing is FX-converted.", ""]
        out += _table(["Label", "Transactions", "Invoice lines"],
                      [[k, share(tx.get(k)), share(lines.get(k))] for k in sorted(set(tx) | set(lines))])
    return out


# ── entry point ──────────────────────────────────────────────────────────────────


def write_reports(profile: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / f"{REPORT_NAME}.json", out_dir / f"{REPORT_NAME}.md"
    json_path.write_text(json.dumps(profile, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n")
    md_path.write_text(render_markdown(profile))
    return md_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Profile the raw source datasets (read-only).")
    parser.add_argument("--config", type=Path, help="pipeline config (default: config/pipeline_config.yaml)")
    parser.add_argument("--out-dir", type=Path, help="output directory (default: <output_dir>/profiling)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging(new_run_id(), level=config.run.log_level)
    profile = build_profile(config)
    md_path, json_path = write_reports(profile, args.out_dir or config.paths.output_dir / "profiling")
    logger.bind(stage="profile").info("Reports written", markdown=str(md_path), json=str(json_path),
                                      checks_with_issues=profile["summary"]["checks_with_issues"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
