from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.cleaning import standardize as std


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2023-08-23", date(2023, 8, 23)),
        ("2024-02-11T00:00:00", date(2024, 2, 11)),
        ("2023/08/23", date(2023, 8, 23)),
        ("23-Aug-2023", date(2023, 8, 23)),
        # slash → day-first by convention; dash → month-first by convention
        ("05/10/2021", date(2021, 10, 5)),
        ("05-10-2021", date(2021, 5, 10)),
        # an unambiguous part overrides the convention
        ("25/03/2025", date(2025, 3, 25)),
        ("20-03-2025", date(2025, 3, 20)),
        ("08-16-2024", date(2024, 8, 16)),
        ("1548370800", date(2019, 1, 24)),  # epoch seconds, read as UTC
        (None, None),
        ("N/A", None),
    ],
)
def test_parse_date(raw, expected):
    assert std.parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["2023-13-45", "yesterday", "12-Foo-2020", "1/2"])
def test_parse_date_rejects_garbage(raw):
    with pytest.raises(ValueError):
        std.parse_date(raw)


def test_parse_datetime_normalises_timezones_to_naive_utc():
    assert std.parse_datetime("2020-03-15T10:00:00Z") == datetime(2020, 3, 15, 10, 0)
    assert std.parse_datetime("2020-03-15T12:00:00+02:00") == datetime(2020, 3, 15, 10, 0)
    assert std.parse_datetime("20-03-2025") == datetime(2025, 3, 20)


@pytest.mark.parametrize(
    "raw, expected",
    [("yes", True), ("Y", True), ("1", True), (1, True), ("True", True), (True, True),
     ("no", False), ("0", False), ("false", False), (False, False), ("", None), (None, None)],
)
def test_parse_bool(raw, expected):
    assert std.parse_bool(raw) is expected


def test_parse_bool_rejects_unknown_token():
    with pytest.raises(ValueError):
        std.parse_bool("maybe")


@pytest.mark.parametrize("raw", ["USD", "usd", "Usd", "$", "US$", "US Dollar", "US Dollars"])
def test_currency_aliases_map_to_usd(raw):
    assert std.normalize_currency(raw) == "USD"


def test_unknown_currency_raises():
    with pytest.raises(ValueError):
        std.normalize_currency("doubloons")


@pytest.mark.parametrize(
    "raw", ["687.883.3415", "(687) 883-3415", "6878833415", "+1-687-883-3415"]
)
def test_phone_formats_converge_to_e164(raw):
    assert std.normalize_phone(raw) == "+16878833415"


def test_phone_null_tokens_and_garbage():
    assert std.normalize_phone("UNLISTED") is None
    with pytest.raises(ValueError):
        std.normalize_phone("12")


def test_email_normalisation():
    assert std.normalize_email("  Jane.Doe@Example.COM ") == "jane.doe@example.com"
    assert std.normalize_email("N/A") is None
    with pytest.raises(ValueError):
        std.normalize_email("JessicaJohnson")


def test_name_casing_only_touches_single_case_names():
    assert std.normalize_name("compact bluetooth speaker") == "Compact Bluetooth Speaker"
    assert std.normalize_name("SMART WOOL SWEATER") == "Smart Wool Sweater"
    assert std.normalize_name("Webcam HD") == "Webcam HD"


def test_text_normalisation_keeps_unicode_but_composes_it():
    assert std.normalize_text("  Café   au  lait ") == "Café au lait"
    assert std.normalize_text("   ") is None


def test_tokens_and_tags():
    assert std.normalize_token("Prefer not to say") == "prefer_not_to_say"
    assert std.split_tags("electronics,Smart Watches, electronics") == ["electronics", "smart_watches"]
    assert std.split_tags(None) == []


def test_numbers():
    assert std.parse_int("7") == 7 and std.parse_int(7.0) == 7
    assert std.parse_float("") is None
    with pytest.raises(ValueError):
        std.parse_int("7.5")
    with pytest.raises(ValueError):
        std.parse_float(True)


def test_parse_hms():
    assert std.parse_hms("00:03:43") == timedelta(minutes=3, seconds=43)
    with pytest.raises(ValueError):
        std.parse_hms("3:43")
