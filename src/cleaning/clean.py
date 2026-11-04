"""Dataset-level cleaning: Silver ``validated`` -> Silver ``clean``.

Validation judges each record on its own. This stage applies the rules that need other
records or other tables, in this order:

1. duplicates – one row per primary key; the latest ``updated_at`` (else the first seen)
   is kept, the others are logged to ``outputs/quarantine/_duplicates.csv``;
2. references – foreign keys that match no record are flagged, never dropped or nulled:
   revenue stays complete and the graph simply draws no edge;
3. consistency – values that must be unique across records (email, SKU, invoice number)
   and claims that another table contradicts (verified purchases);
4. evidence-based fixes – currency labels contradicted by catalog prices, missing
   categories implied by the product name.

Every change is a registered rule appended to the row's ``dq_fixes`` / ``dq_flags``.
The functions take and return DataFrames; only :func:`run_cleaning` touches files.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd
from loguru import logger

from src.ingestion.loaders import INVOICE_LINES, INVOICE_LINES_KEY
from src.utils.config import PipelineConfig
from src.utils.io import write_csv, write_parquet
from src.validation.rules import RULES, Action
from src.validation.validator import LINEAGE_COLUMNS, METRICS_FILE as VALIDATION_METRICS_FILE, VALIDATED_SUBDIR

CLEAN_SUBDIR = "clean"
METRICS_FILE = "_cleaning_metrics.json"
DUPLICATES_FILE = "_duplicates.csv"
MONEY_TOLERANCE = 0.01

Tables = dict[str, pd.DataFrame]


@dataclass(frozen=True)
class ForeignKey:
    entity: str
    column: str  # same column name in the parent, where it is the primary key
    parent: str
    rule: str


FOREIGN_KEYS: tuple[ForeignKey, ...] = (
    ForeignKey("products", "vendor_id", "vendors", "products.unknown_vendor"),
    ForeignKey("transactions", "customer_id", "customers", "transactions.unknown_customer"),
    ForeignKey("transactions", "product_id", "products", "transactions.unknown_product"),
    ForeignKey("invoices", "vendor_id", "vendors", "invoices.unknown_vendor"),
    ForeignKey("invoice_lines", "invoice_id", "invoices", "invoice_lines.unknown_invoice"),
    ForeignKey("invoice_lines", "product_id", "products", "invoice_lines.unknown_product"),
    ForeignKey("support_tickets", "customer_id", "customers", "support_tickets.unknown_customer"),
    ForeignKey("support_tickets", "product_id", "products", "support_tickets.unknown_product"),
    ForeignKey("product_reviews", "customer_id", "customers", "product_reviews.unknown_customer"),
    ForeignKey("product_reviews", "product_id", "products", "product_reviews.unknown_product"),
    ForeignKey("call_transcripts", "customer_id", "customers", "call_transcripts.unknown_customer"),
    ForeignKey("call_transcripts", "ticket_id", "support_tickets", "call_transcripts.unknown_ticket"),
)

# (entity, column, rule): values that identify one real-world thing and must not repeat.
UNIQUE_VALUES: tuple[tuple[str, str, str], ...] = (
    ("customers", "email", "customers.shared_email"),
    ("products", "sku", "products.sku_conflict"),
    ("invoices", "invoice_number", "invoices.duplicate_number"),
)


@dataclass
class CleaningMetrics:
    entity: str
    rows_in: int = 0
    rows_out: int = 0
    records_fixed: int = 0
    records_flagged: int = 0
    removals: Counter = field(default_factory=Counter)
    fixes: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("removals", "fixes", "flags"):
            data[key] = dict(getattr(self, key).most_common())
        return data


def mark(df: pd.DataFrame, mask: pd.Series, rule_id: str, metrics: CleaningMetrics) -> None:
    """Append ``rule_id`` to ``dq_fixes`` or ``dq_flags`` of every row where ``mask`` is true."""
    rule = RULES[rule_id]
    if rule.action not in (Action.FIX, Action.FLAG):
        raise ValueError(f"{rule_id} is a {rule.action.value} rule; only fix/flag rules mark rows")
    mask = mask.fillna(False).astype(bool)
    if not mask.any():
        return
    column, counter = ("dq_fixes", metrics.fixes) if rule.action is Action.FIX else ("dq_flags", metrics.flags)
    df[column] = [[*events, rule_id] if hit else events for events, hit in zip(df[column], mask)]
    counter[rule_id] += int(mask.sum())


# ── 1. duplicates ────────────────────────────────────────────────────────────────


def deduplicate(entity: str, df: pd.DataFrame, key: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep one row per key: the latest ``updated_at`` if the entity has one, else the first seen.

    Returns the kept rows (original order) and a log of removed rows, each pointing at the
    row that was kept and saying whether it was an exact copy of it.
    """
    ranked = df
    if "updated_at" in df.columns:
        ranked = df.sort_values("updated_at", ascending=False, kind="stable", na_position="last")
    repeated = ranked.duplicated(list(key), keep="first")
    kept, removed = ranked[~repeated].sort_index(), ranked[repeated]

    survivors = kept[[*key, "_source_row", "_record_hash"]].rename(
        columns={"_source_row": "kept_source_row", "_record_hash": "kept_record_hash"}
    )
    log = removed[[*key, *LINEAGE_COLUMNS]].merge(survivors, on=list(key), how="left")
    log["identical_to_kept"] = log["_record_hash"] == log.pop("kept_record_hash")
    log.insert(0, "rule", f"{entity}.duplicate")
    log.insert(0, "entity", entity)
    return kept.reset_index(drop=True), log.drop(columns=list(key))


# ── 2. references ────────────────────────────────────────────────────────────────


def check_references(tables: Tables, metrics: dict[str, CleaningMetrics]) -> None:
    for fk in FOREIGN_KEYS:
        if fk.entity not in tables or fk.parent not in tables:
            continue
        child, parent_keys = tables[fk.entity], set(tables[fk.parent][fk.column])
        # A missing value is a validation concern (already flagged there), not a broken link.
        mark(child, child[fk.column].notna() & ~child[fk.column].isin(parent_keys), fk.rule, metrics[fk.entity])

    if {"invoice_lines", "invoices", "products"} <= tables.keys():
        lines = tables["invoice_lines"]
        product_vendor = lines["product_id"].map(tables["products"].set_index("product_id")["vendor_id"])
        invoice_vendor = lines["invoice_id"].map(tables["invoices"].set_index("invoice_id")["vendor_id"])
        mismatch = product_vendor.notna() & invoice_vendor.notna() & product_vendor.ne(invoice_vendor)
        mark(lines, mismatch, "invoice_lines.vendor_mismatch", metrics["invoice_lines"])


# ── 3. consistency ───────────────────────────────────────────────────────────────


def check_consistency(tables: Tables, metrics: dict[str, CleaningMetrics]) -> None:
    for entity, column, rule in UNIQUE_VALUES:
        if entity in tables:
            df = tables[entity]
            mark(df, df[column].notna() & df[column].duplicated(keep=False), rule, metrics[entity])

    if {"product_reviews", "transactions"} <= tables.keys():
        reviews, tx = tables["product_reviews"], tables["transactions"]
        bought = set(zip(tx["customer_id"], tx["product_id"]))
        no_purchase = pd.Series(
            [pair not in bought for pair in zip(reviews["customer_id"], reviews["product_id"])], index=reviews.index
        )
        claimed = reviews["verified_purchase"].fillna(False).astype(bool)
        mark(reviews, claimed & no_purchase, "product_reviews.unverified_purchase", metrics["product_reviews"])


# ── 4. evidence-based fixes ──────────────────────────────────────────────────────


def _relabel_currency(
    df: pd.DataFrame, confirmed: pd.Series, base: str, rules: tuple[str, str], metrics: CleaningMetrics
) -> None:
    """Set non-base labels to ``base``, keeping the source label in ``src_currency``.

    Rows whose amounts were checked get a fix; the rest a flag. Both are set to ``base``:
    the evidence that labels are meaningless is dataset-wide, not per row.
    """
    df.insert(df.columns.get_loc("currency") + 1, "src_currency", df["currency"])
    labelled = df["currency"].notna() & df["currency"].ne(base)
    fix_rule, flag_rule = rules
    mark(df, labelled & confirmed, fix_rule, metrics)
    mark(df, labelled & ~confirmed, flag_rule, metrics)
    df.loc[labelled, "currency"] = base


def fix_currencies(tables: Tables, base: str, metrics: dict[str, CleaningMetrics]) -> None:
    if "products" not in tables:
        return
    catalog = tables["products"].set_index("product_id")

    if "transactions" in tables:
        tx = tables["transactions"]
        price = tx["product_id"].map(catalog["price"])
        confirmed = (tx["unit_price"].abs() - price).abs() <= MONEY_TOLERANCE
        rules = ("transactions.currency_relabelled", "transactions.currency_unverified")
        _relabel_currency(tx, confirmed, base, rules, metrics["transactions"])

    if {"invoices", "invoice_lines"} <= tables.keys():
        invoices, lines = tables["invoices"], tables["invoice_lines"]
        cost = lines["product_id"].map(catalog["cost"])
        checked = cost.notna()
        # Credit lines carry the negated cost, hence abs().
        wrong = checked & ((lines["unit_cost"].abs() - cost).abs() > MONEY_TOLERANCE)
        per_invoice = pd.DataFrame({"checked": checked, "wrong": wrong}).groupby(lines["invoice_id"]).sum()
        ok_ids = per_invoice.index[(per_invoice["checked"] > 0) & (per_invoice["wrong"] == 0)]
        rules = ("invoices.currency_relabelled", "invoices.currency_unverified")
        _relabel_currency(invoices, invoices["invoice_id"].isin(ok_ids), base, rules, metrics["invoices"])


def infer_categories(products: pd.DataFrame, metrics: CleaningMetrics) -> None:
    """Fill a missing category from the product name minus its leading adjective.

    "Ultra Power Bank" -> "Power Bank"; used only where every catalogued product with that
    name has the same category. Subcategory is not inferred: it varies within a name.
    """
    noun = products["product_name"].str.split(n=1).str[1]
    known = products.loc[products["category"].notna(), "category"].groupby(noun)
    lookup = known.first()[known.nunique() == 1]
    inferred = noun.map(lookup)
    fill = products["category"].isna() & inferred.notna()
    mark(products, fill, "products.category_from_name", metrics)
    products.loc[fill, "category"] = inferred[fill]


# ── stage ────────────────────────────────────────────────────────────────────────


def primary_key(config: PipelineConfig, entity: str) -> tuple[str, ...]:
    return INVOICE_LINES_KEY if entity == INVOICE_LINES else config.sources[entity].primary_key


def clean_tables(
    tables: Tables, config: PipelineConfig
) -> tuple[Tables, pd.DataFrame, dict[str, CleaningMetrics]]:
    """Apply every dataset-level rule. Rules whose tables are missing are skipped."""
    metrics = {name: CleaningMetrics(entity=name, rows_in=len(df)) for name, df in tables.items()}
    cleaned: Tables = {}
    removed = []
    for name, df in tables.items():
        df = df.copy()
        for column in ("dq_fixes", "dq_flags"):
            df[column] = df[column].map(list)
        cleaned[name], log = deduplicate(name, df, primary_key(config, name))
        metrics[name].removals.update(log["rule"])
        removed.append(log)

    check_references(cleaned, metrics)
    check_consistency(cleaned, metrics)
    fix_currencies(cleaned, config.reference.base_currency, metrics)
    if "products" in cleaned:
        infer_categories(cleaned["products"], metrics["products"])

    for name, df in cleaned.items():
        m = metrics[name]
        m.rows_out = len(df)
        m.records_fixed = int(df["dq_fixes"].map(bool).sum())
        m.records_flagged = int(df["dq_flags"].map(bool).sum())
    return cleaned, pd.concat(removed, ignore_index=True), metrics


def run_cleaning(config: PipelineConfig) -> dict[str, CleaningMetrics]:
    validated_dir = config.paths.silver_dir / VALIDATED_SUBDIR
    out_dir = config.paths.silver_dir / CLEAN_SUBDIR
    validated = json.loads((validated_dir / VALIDATION_METRICS_FILE).read_text())
    tables = {name: pd.read_parquet(validated_dir / f"{name}.parquet") for name in validated["entities"]}

    cleaned, removed, metrics = clean_tables(tables, config)

    for name, df in cleaned.items():
        write_parquet(out_dir / f"{name}.parquet", df)
        m = metrics[name]
        logger.bind(stage="clean", source=name).info(
            "Cleaned", rows_in=m.rows_in, removed=sum(m.removals.values()), rows_out=m.rows_out,
            fixes=sum(m.fixes.values()), flags=sum(m.flags.values()),
        )
    write_csv(config.paths.quarantine_dir / DUPLICATES_FILE, removed)
    payload = {"entities": {name: m.as_dict() for name, m in metrics.items()}}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / METRICS_FILE).write_text(json.dumps(payload, indent=2))
    return metrics
