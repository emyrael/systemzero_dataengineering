from __future__ import annotations

import pytest

from src.reporting.dashboard import (_bar_path, axis_money, compact_money, hbar_chart, money, nice_ticks,
                                     segmented_bar)


@pytest.mark.parametrize(("top", "expected"), [(121_678, [0, 50_000, 100_000, 150_000]), (339, [0, 100, 200, 300, 400]),
                                               (0, [0.0, 1.0])])
def test_nice_ticks_cover_the_maximum_with_round_steps(top, expected):
    assert nice_ticks(top) == expected


def test_money_formats():
    assert (money(2_739_162.03), money(None)) == ("$2,739,162", "–")
    assert (compact_money(2_739_162), compact_money(80_612), compact_money(641)) == ("$2.74M", "$80.6K", "$641")
    assert (axis_money(150_000), axis_money(1_500_000), axis_money(0)) == ("$150K", "$1.5M", "$0")


def test_bar_path_has_a_rounded_end_and_a_square_base():
    d = _bar_path(10, 0, 100, 16, horizontal=True)
    assert d.startswith("M10.0,0.0H106.0Q110.0,0.0") and d.endswith("H10.0Z")


def test_segmented_bar_skips_empty_parts_and_labels_only_wide_ones():
    html = segmented_bar([("A", 90), ("B", 0), ("C", 5), ("D", 5)], 100, tall=True)
    assert html.count('class="seg s') == 3 and ">90%<" in html and ">5%<" not in html
    assert 'class="seg s3"' in html  # colour follows the part's position, not its rank among the drawn ones


def test_labels_are_escaped():
    assert "&lt;b&gt;" in hbar_chart([("<b>", 1.0, "1")]) and "<b>" not in hbar_chart([("<b>", 1.0, "1")])
