from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest

from src.cleaning import clean
from src.validation import validator
from src.validation.rules import RULES, Action
from src.validation.schemas import SCHEMAS
from src.validation.validator import validate_entity

SCHEMA_DIR = Path(validator.__file__).parent / "schemas"
RULE_SOURCES = [*SCHEMA_DIR.glob("*.py"), Path(clean.__file__)]
# Duplicate rules are built per entity (f"{entity}.duplicate"), so they never appear as literals.
DROP_RULES = {rule_id for rule_id, rule in RULES.items() if rule.action is Action.DROP}


def _rule_ids_in_schema_code() -> set[str]:
    ids = set()
    for path in RULE_SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.fullmatch(r"[a-z_]+\.[a-z_]+", node.value) and node.value.split(".")[0] in SCHEMAS:
                    ids.add(node.value)
    return ids


def test_every_rule_used_by_a_schema_is_registered():
    assert _rule_ids_in_schema_code() - set(RULES) == set()


def test_every_registered_rule_is_used_by_a_schema():
    assert set(RULES) - DROP_RULES - _rule_ids_in_schema_code() == set()


def test_every_entity_has_a_duplicate_rule():
    assert DROP_RULES == {f"{entity}.duplicate" for entity in SCHEMAS}


def test_rule_entities_match_schemas():
    assert {rule.entity for rule in RULES.values()} == set(SCHEMAS)


def test_unregistered_event_is_a_programming_error():
    with pytest.raises(KeyError, match="unregistered"):
        validator._split_events(["products.not_a_rule"])


def test_events_are_split_by_action_and_deduplicated():
    fixes, flags = validator._split_events(
        ["products.currency", "products.invalid_price", "products.currency"]
    )
    assert fixes == ["products.currency"] and flags == ["products.invalid_price"]
    assert RULES["products.currency"].action is Action.FIX


def test_validate_entity_splits_rows_and_counts():
    bronze = [
        {"_source_file": "f", "_source_row": 1, "_record_hash": "h1",
         "record": {"vendor_id": "VND-001", "contact": {"primary_name": "A", "email": "a@b.co", "phone": "+1-555-0101"}}},
        {"_source_file": "f", "_source_row": 2, "_record_hash": "h2", "record": {"vendor_id": "nope"}},
        {"_source_file": "f", "_source_row": 3, "_record_hash": "h3", "record": {"vendor_id": "VND-002"}},
    ]
    valid, quarantine, metrics = validate_entity("vendors", bronze, {"as_of": date(2025, 1, 1)})

    assert (metrics.total, metrics.valid, metrics.quarantined) == (3, 2, 1)
    assert list(valid["_source_row"]) == [1, 3]
    assert quarantine.loc[0, "rejection_rules"] == "vendors.invalid_id"
    assert '"vendor_id": "nope"' in quarantine.loc[0, "raw_record"]
    assert valid.loc[1, "dq_flags"] == ["vendors.missing_reliability", "vendors.missing_contact"]
    assert metrics.flags["vendors.missing_contact"] == 1


def test_empty_quarantine_still_has_columns():
    _, quarantine, _ = validate_entity("vendors", [], {})
    assert list(quarantine.columns)[:6] == list(validator.LINEAGE_COLUMNS)
