"""KPI dashboard: one self-contained HTML page built from the Gold tables.

* **Aggregates only.** No customer id, name or contact detail leaves this module, so the
  page can be shared or hosted without exposing personal data.
* **Same numbers as everywhere else.** Revenue comes from ``recognised_sales``, the one
  revenue definition the features and the graph use.
* **Self-contained and deterministic.** Charts are inline SVG drawn here, the little
  JavaScript only adds tooltips; no CDN, no network, no timestamp. A rerun on the same
  data writes the same file, and the page works offline or as a static site.
"""

from __future__ import annotations

import json
import math
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.cleaning.clean import CLEAN_SUBDIR
from src.cleaning.clean import METRICS_FILE as CLEANING_METRICS_FILE
from src.features.build import recognised_sales
from src.reporting.quality_report import merge_metrics
from src.utils.config import PipelineConfig
from src.validation.rules import RULES
from src.validation.validator import METRICS_FILE, VALIDATED_SUBDIR

GOLD_TABLES = ("customer_features", "product_features", "vendor_features", "invoice_features")
RFM_ORDER = ("Champions", "Loyal", "Promising", "Needs attention", "At risk", "Hibernating", "No purchases")
TIER_ORDER = ("Low", "Medium", "High", "Premium")
PAYMENT_ORDER = ("paid", "refunded", "failed", "pending")
RECENCY_BUCKETS = ((0, 90, "0–90 days"), (91, 180, "91–180 days"), (181, 365, "181–365 days"),
                   (366, None, "Over a year"))
QUALITY_PARTS = ("No issues", "Flagged", "Duplicate removed", "Quarantined")
TOP_PRODUCTS = 10
TOP_RULES = 10


# ── data ─────────────────────────────────────────────────────────────────────────


def _r(value: float, digits: int = 2) -> float:
    return round(float(value), digits) + 0.0  # + 0.0 turns -0.0 into 0.0


def dashboard_data(gold: dict[str, pd.DataFrame], transactions: pd.DataFrame,
                   quality: dict[str, dict], as_of: str) -> dict[str, Any]:
    """Every number on the page, as plain JSON-ready values (no record-level data)."""
    sales = recognised_sales(transactions)
    purchases, returns = sales[~sales["is_return"]], sales[sales["is_return"]]
    gross = purchases["net_revenue"].sum()
    customers, products = gold["customer_features"], gold["product_features"]
    vendors, invoices = gold["vendor_features"], gold["invoice_features"]

    monthly = sales.set_index("transaction_date")["net_revenue"].resample("MS").sum()
    buyers = customers["total_orders"].fillna(0) > 0
    recency = customers["days_since_last_purchase"]
    rfm = customers.groupby("rfm_segment").agg(n=("customer_id", "size"), clv=("customer_lifetime_value", "sum"))
    by_category = products.groupby("category")["revenue_contribution"].sum().sort_values(ascending=False)
    tiers = products.groupby("price_tier").agg(revenue=("revenue_contribution", "sum"), n=("product_id", "size"))
    top = products.sort_values(["revenue_contribution", "product_id"], ascending=[False, True]).head(TOP_PRODUCTS)
    overdue = invoices[invoices["days_overdue"].fillna(0) > 0].groupby("vendor_id").size()

    issues: list[tuple[str, str, int]] = []
    for metrics in quality.values():
        for key, label in (("rejections", "Quarantined"), ("removals", "Removed"), ("flags", "Flagged")):
            issues += [(rule, label, n) for rule, n in metrics[key].items()]
    issues.sort(key=lambda item: (-item[2], item[0]))

    return {
        "as_of": as_of,
        "window": [sales["transaction_date"].min().date().isoformat(),
                   sales["transaction_date"].max().date().isoformat()],
        "kpis": {
            "net_revenue": _r(sales["net_revenue"].sum()),
            "gross_sales": _r(gross),
            "returns_value": _r(-returns["net_revenue"].sum()),
            "return_rate_pct": _r(100 * -returns["net_revenue"].sum() / gross, 1) if gross else None,
            "orders": int(len(purchases)),
            "returns": int(len(returns)),
            "transactions": int(len(transactions)),
            "average_order_value": _r(purchases["net_revenue"].mean()) if len(purchases) else None,
            "customers": int(len(customers)),
            "buying_customers": int(buyers.sum()),
            "active_12m": int((recency <= 365).sum()),
        },
        "monthly": [[ts.strftime("%Y-%m"), _r(v)] for ts, v in monthly.items()],
        "payment": [[s, int((transactions["payment_status"] == s).sum())] for s in PAYMENT_ORDER],
        "recognised": int(len(sales)),
        "paid_but_cancelled": int(((transactions["payment_status"] == "paid") & ~transactions.index.isin(sales.index)).sum()),
        "rfm": [[seg, int(rfm.loc[seg, "n"]), _r(rfm.loc[seg, "clv"])] for seg in RFM_ORDER if seg in rfm.index],
        "recency": [[label, int(recency.between(lo, hi if hi is not None else math.inf).sum())]
                    for lo, hi, label in RECENCY_BUCKETS] + [["No purchase", int(recency.isna().sum())]],
        "categories": [[c, _r(v)] for c, v in by_category.items()],
        "unlisted_product_revenue": _r(sales["net_revenue"].sum() - products["revenue_contribution"].sum()),
        "tiers": [[t, _r(tiers.loc[t, "revenue"]), int(tiers.loc[t, "n"])] for t in TIER_ORDER if t in tiers.index],
        "top_products": [[r.product_name, r.category, int(r.units_sold), _r(r.revenue_contribution)]
                         for r in top.itertuples()],
        "vendors": [
            {"name": v.vendor_name, "status": v.status, "revenue": _r(v.revenue_generated),
             "reliability": None if pd.isna(v.reliability_score) else _r(v.reliability_score, 1),
             "payment_rate": None if pd.isna(v.invoice_payment_rate) else _r(v.invoice_payment_rate, 1),
             "outstanding": _r(v.total_outstanding_balance), "overdue": int(overdue.get(v.vendor_id, 0))}
            for v in vendors.sort_values(["revenue_generated", "vendor_id"], ascending=[False, True]).itertuples()
        ],
        "quality": [
            {"source": name, "total": m["total"], "parts": [m["rows_out"] - m["records_flagged"], m["records_flagged"],
                                                             m["removed"], m["quarantined"]]}
            for name, m in quality.items()
        ],
        "top_rules": [[rule, label, n, RULES[rule].description if rule in RULES else ""]
                      for rule, label, n in issues[:TOP_RULES]],
    }


# ── formatting ───────────────────────────────────────────────────────────────────


def money(v: float | None) -> str:
    return "–" if v is None else f"${v:,.0f}"


def compact_money(v: float) -> str:
    for scale, suffix in ((1e6, "M"), (1e3, "K")):
        if abs(v) >= scale:
            return f"${v / scale:.{2 if scale == 1e6 else 1}f}{suffix}"
    return f"${v:,.0f}"


def nice_ticks(top: float, count: int = 4) -> list[float]:
    """Round axis ticks from 0 to at least ``top``."""
    if top <= 0:
        return [0.0, 1.0]
    raw = top / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return [i * step for i in range(math.ceil(top / step) + 1)]


def axis_money(v: float) -> str:
    """Axis ticks: $0, $50K, $1.5M (no trailing .0)."""
    for scale, suffix in ((1e6, "M"), (1e3, "K")):
        if abs(v) >= scale:
            return f"${v / scale:g}{suffix}"
    return f"${v:g}"


def _month_label(ym: str) -> str:
    return pd.Timestamp(ym + "-01").strftime("%b %Y")


# ── SVG charts ───────────────────────────────────────────────────────────────────


def _tip(value: str, label: str) -> str:
    return f'data-value="{escape(value)}" data-label="{escape(label)}" tabindex="0"'


def _bar_path(x0: float, y: float, length: float, thickness: float, horizontal: bool) -> str:
    """A bar square at the baseline with a 4px rounded data end."""
    r = min(4.0, length / 2, thickness / 2)
    if horizontal:
        x1, y1 = x0 + length, y + thickness
        return (f"M{x0:.1f},{y:.1f}H{x1 - r:.1f}Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f}V{y1 - r:.1f}"
                f"Q{x1:.1f},{y1:.1f} {x1 - r:.1f},{y1:.1f}H{x0:.1f}Z")
    # vertical: x0 is the left edge, y the baseline, length grows upwards
    top, x1 = y - length, x0 + thickness
    return (f"M{x0:.1f},{y:.1f}V{top + r:.1f}Q{x0:.1f},{top:.1f} {x0 + r:.1f},{top:.1f}H{x1 - r:.1f}"
            f"Q{x1:.1f},{top:.1f} {x1:.1f},{top + r:.1f}V{y:.1f}Z")


def hbar_chart(rows: list[tuple[str, float, str]], label_width: int = 150, width: int = 560) -> str:
    """Ranked horizontal bars, one hue. ``rows``: (label, value, formatted value)."""
    row_h, bar_h, value_w = 30, 16, 76
    plot_w = width - label_width - value_w
    top = max((v for _, v, _ in rows), default=0) or 1
    parts = []
    for i, (label, v, shown) in enumerate(rows):
        y = i * row_h
        length = max(v, 0) / top * plot_w
        bar = (f'<path class="bar" d="{_bar_path(label_width, y + (row_h - bar_h) / 2, length, bar_h, True)}"/>'
               if length >= 1 else "")
        parts.append(
            f'<g class="hit" {_tip(shown, label)}><rect class="hit-area" x="0" y="{y}" width="{width}" height="{row_h}"/>'
            f'<text class="label" x="{label_width - 10}" y="{y + row_h / 2}" text-anchor="end" dominant-baseline="central">'
            f'{escape(label)}</text>{bar}'
            f'<text class="value" x="{label_width + length + 6:.1f}" y="{y + row_h / 2}" dominant-baseline="central">'
            f'{escape(shown)}</text></g>'
        )
    height = row_h * len(rows)
    wide = " wide" if width > 800 else ""
    return f'<div class="chart-box"><svg class="chart{wide}" viewBox="0 0 {width} {height}" role="img">{"".join(parts)}</svg></div>'


def column_chart(rows: list[tuple[str, float, str]], width: int = 560, height: int = 220) -> str:
    """Columns on one baseline, value on the cap. ``rows``: (label, value, formatted value)."""
    base, top_pad, col_w = height - 30, 22, 24
    band = width / max(len(rows), 1)
    top = max((v for _, v, _ in rows), default=0) or 1
    parts = [f'<line class="axis" x1="0" x2="{width}" y1="{base}" y2="{base}"/>']
    for i, (label, v, shown) in enumerate(rows):
        cx = band * i + band / 2
        length = max(v, 0) / top * (base - top_pad)
        col = (f'<path class="bar" d="{_bar_path(cx - col_w / 2, base, length, col_w, False)}"/>'
               if length >= 1 else "")
        parts.append(
            f'<g class="hit" {_tip(shown, label)}><rect class="hit-area" x="{band * i:.1f}" y="0" width="{band:.1f}" '
            f'height="{height}"/>{col}'
            f'<text class="value" x="{cx:.1f}" y="{base - length - 6:.1f}" text-anchor="middle">{escape(shown)}</text>'
            f'<text class="label" x="{cx:.1f}" y="{base + 18}" text-anchor="middle">{escape(label)}</text></g>'
        )
    return f'<div class="chart-box"><svg class="chart" viewBox="0 0 {width} {height}" role="img">{"".join(parts)}</svg></div>'


def line_chart(points: list[list[Any]], width: int = 1120, height: int = 280) -> str:
    """Monthly line with an area wash, a crosshair tooltip and the last value labelled."""
    left, right, top_pad, bottom = 56, 64, 12, 28
    plot_w, plot_h = width - left - right, height - top_pad - bottom
    values = [v for _, v in points]
    ticks = nice_ticks(max(values, default=0))
    y_max = ticks[-1]
    step = plot_w / max(len(points) - 1, 1)
    xs = [left + i * step for i in range(len(points))]
    ys = [top_pad + plot_h * (1 - v / y_max) for v in values]
    base = top_pad + plot_h

    grid = "".join(
        f'<line class="grid" x1="{left}" x2="{left + plot_w}" y1="{top_pad + plot_h * (1 - t / y_max):.1f}" '
        f'y2="{top_pad + plot_h * (1 - t / y_max):.1f}"/>'
        f'<text class="tick" x="{left - 8}" y="{top_pad + plot_h * (1 - t / y_max):.1f}" text-anchor="end" '
        f'dominant-baseline="central">{escape(axis_money(t))}</text>'
        for t in ticks
    )
    years = "".join(
        f'<text class="tick" x="{xs[i]:.1f}" y="{height - 8}" text-anchor="middle">{ym[:4]}</text>'
        f'<line class="axis" x1="{xs[i]:.1f}" x2="{xs[i]:.1f}" y1="{base}" y2="{base + 4}"/>'
        for i, (ym, _) in enumerate(points) if ym.endswith("-01")
    )
    path = "".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    area = f"{path}L{xs[-1]:.1f},{base}L{xs[0]:.1f},{base}Z" if points else ""
    last = (f'<circle class="dot" cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4"/>'
            f'<text class="value" x="{xs[-1] + 8:.1f}" y="{ys[-1]:.1f}" dominant-baseline="central">'
            f'{escape(compact_money(values[-1]))}</text>') if points else ""
    data = json.dumps({"x": [round(x, 1) for x in xs], "y": [round(y, 1) for y in ys],
                       "label": [_month_label(ym) for ym, _ in points], "value": [money(v) for v in values]})
    return (
        f'<div class="chart-box"><svg class="chart line" viewBox="0 0 {width} {height}" role="img" tabindex="0" '
        f"data-series='{escape(data)}'>{grid}"
        f'<line class="axis" x1="{left}" x2="{left + plot_w}" y1="{base}" y2="{base}"/>{years}'
        f'<path class="area" d="{area}"/><path class="line" d="{path}"/>{last}'
        f'<line class="crosshair" x1="0" x2="0" y1="{top_pad}" y2="{base}" visibility="hidden"/>'
        f'<circle class="dot cursor" r="4" visibility="hidden"/>'
        f'<rect class="hit-area" x="{left}" y="{top_pad}" width="{plot_w}" height="{plot_h}"/></svg></div>'
    )


def segmented_bar(parts: list[tuple[str, int]], total: int, tall: bool) -> str:
    """One whole split into parts, in categorical slot order; tall bars print the share inside."""
    blocks = []
    for slot, (name, n) in enumerate(parts, start=1):
        if n <= 0:
            continue
        share = 100 * n / total if total else 0
        text = f"{share:.0f}%" if tall and share >= 8 else ""
        blocks.append(f'<span class="seg s{slot}" style="flex-grow:{n}" {_tip(f"{n:,} ({share:.1f}%)", name)}>'
                      f"{escape(text)}</span>")
    return f'<div class="segbar {"tall" if tall else "thin"}">{"".join(blocks)}</div>'


def legend(names: list[str]) -> str:
    items = "".join(f'<span class="key"><span class="swatch s{i}"></span>{escape(n)}</span>'
                    for i, n in enumerate(names, start=1))
    return f'<div class="legend">{items}</div>'


def table(headers: list[str], rows: list[list[Any]], numeric_from: int = 1) -> str:
    head = "".join(f'<th{" class=num" if i >= numeric_from else ""}>{escape(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td{" class=num" if i >= numeric_from else ""}>{escape(str(c))}</td>'
                         for i, c in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def table_view(headers: list[str], rows: list[list[Any]], numeric_from: int = 1) -> str:
    return f"<details><summary>Table view</summary>{table(headers, rows, numeric_from)}</details>"


def card(title: str, subtitle: str, body: str, wide: bool = False) -> str:
    return (f'<section class="card{" wide" if wide else ""}"><h3>{escape(title)}</h3>'
            f'<p class="sub">{escape(subtitle)}</p>{body}</section>')


# ── page ─────────────────────────────────────────────────────────────────────────


def _kpi(label: str, value: str, note: str) -> str:
    return (f'<div class="kpi"><div class="kpi-label">{escape(label)}</div>'
            f'<div class="kpi-value">{escape(value)}</div><div class="kpi-note">{escape(note)}</div></div>')


def render_dashboard(d: dict[str, Any]) -> str:
    k = d["kpis"]
    start, end = (f"{pd.Timestamp(x).day} {pd.Timestamp(x):%b %Y}" for x in d["window"])
    pct = lambda part, whole: f"{100 * part / whole:.0f}%" if whole else "–"  # noqa: E731

    overview = (
        f'<div class="hero"><div class="kpi-label">Net revenue</div>'
        f'<div class="hero-value">{escape(compact_money(k["net_revenue"]))}</div>'
        f'<div class="kpi-note">{escape(money(k["net_revenue"]))}, {escape(start)} to {escape(end)}</div></div>'
        '<div class="kpis">'
        + _kpi("Orders", f'{k["orders"]:,}', f'paid, not cancelled; with {k["returns"]:,} returns, '
               f'{d["recognised"]:,} of {k["transactions"]:,} transactions count')
        + _kpi("Average order value", money(k["average_order_value"]), "recognised purchases, before returns")
        + _kpi("Return rate", "–" if k["return_rate_pct"] is None else f'{k["return_rate_pct"]:.1f}%', f'{money(k["returns_value"])} returned of '
               f'{money(k["gross_sales"])} sold')
        + _kpi("Buying customers", f'{k["buying_customers"]:,}', f'of {k["customers"]:,}; '
               f'{k["active_12m"]:,} bought in the last 12 months')
        + "</div>"
    )

    monthly = d["monthly"]
    trend = card("Monthly net revenue", "Recognised revenue per month, net of discounts and returns.",
                 line_chart(monthly) + table_view(["Month", "Net revenue"],
                                                  [[_month_label(m), money(v)] for m, v in monthly]), wide=True)

    pay_total = sum(n for _, n in d["payment"])
    payment = card(
        "Why most transactions are not revenue",
        f"Payment status of all {pay_total:,} transactions. Revenue counts paid orders that were not cancelled: "
        f"{d['recognised']:,} ({pct(d['recognised'], pay_total)}); {d['paid_but_cancelled']:,} paid orders were "
        "cancelled.",
        segmented_bar([(s.capitalize(), n) for s, n in d["payment"]], pay_total, tall=True)
        + legend([s.capitalize() for s, _ in d["payment"]])
        + table_view(["Payment status", "Transactions", "Share"],
                     [[s.capitalize(), f"{n:,}", pct(n, pay_total)] for s, n in d["payment"]]),
        wide=True,
    )

    rfm = card("Customers by RFM segment", "Recency, frequency and monetary scores (Gold customer features).",
               hbar_chart([(s, n, f"{n:,}") for s, n, _ in d["rfm"]])
               + table_view(["Segment", "Customers", "Lifetime value (sum)"],
                            [[s, f"{n:,}", money(v)] for s, n, v in d["rfm"]]))
    recency = card("Days since last purchase", f"Customers by recency on {d['as_of']}.",
                   column_chart([(label, n, f"{n:,}") for label, n in d["recency"]])
                   + table_view(["Recency", "Customers"], [[label, f"{n:,}"] for label, n in d["recency"]]))

    categories = card(
        "Revenue by category",
        f"Catalog products only; {money(d['unlisted_product_revenue'])} was sold on product ids not in the catalog.",
        hbar_chart([(c, v, compact_money(v)) for c, v in d["categories"]], label_width=170)
        + table_view(["Category", "Revenue"], [[c, money(v)] for c, v in d["categories"]]))
    tiers = card("Revenue by price tier", "Price quartiles of the catalog.",
                 column_chart([(t, v, compact_money(v)) for t, v, _ in d["tiers"]])
                 + table_view(["Price tier", "Revenue", "Products"], [[t, money(v), n] for t, v, n in d["tiers"]]))
    products = card("Top products", f"The {len(d['top_products'])} products with the most recognised revenue.",
                    table(["Product", "Category", "Units", "Revenue"],
                          [[n, c, f"{u:,}", money(v)] for n, c, u, v in d["top_products"]], numeric_from=2),
                    wide=True)

    vendors = card(
        "Revenue and invoice health",
        "Revenue from their products, reliability, and how their invoices are paid. Paid on time = invoices "
        "due by the reference date that were fully paid by their due date; outstanding = unpaid balance.",
        table(["Vendor", "Status", "Revenue", "Reliability", "Paid on time", "Outstanding", "Overdue invoices"],
              [[v["name"], v["status"], money(v["revenue"]), "–" if v["reliability"] is None else v["reliability"],
                "–" if v["payment_rate"] is None else f'{v["payment_rate"]:.1f}%', money(v["outstanding"]),
                v["overdue"]] for v in d["vendors"]], numeric_from=2),
        wide=True,
    )

    quality_rows = "".join(
        f'<tr><td>{escape(q["source"])}</td><td class="num">{q["total"]:,}</td>'
        + "".join(f'<td class="num">{n:,}</td>' for n in q["parts"])
        + f'<td class="bar-cell">{segmented_bar(list(zip(QUALITY_PARTS, q["parts"])), q["total"], tall=False)}</td></tr>'
        for q in d["quality"]
    )
    quality = card(
        "Data quality by source",
        "Every record read is accounted for: kept without issues, kept and flagged, removed as a duplicate, "
        "or quarantined. Details in quality_report.md.",
        legend(list(QUALITY_PARTS))
        + '<div class="table-wrap"><table><thead><tr><th>Source</th><th class="num">Records</th>'
        + "".join(f'<th class="num">{escape(p)}</th>' for p in QUALITY_PARTS)
        + f'<th>Split</th></tr></thead><tbody>{quality_rows}</tbody></table></div>',
        wide=True,
    )
    rules = card("Most frequent issues", "Rules that flagged, removed or quarantined the most records.",
                 hbar_chart([(rule, n, f"{n:,}") for rule, _, n, _ in d["top_rules"]], label_width=280, width=1120)
                 + table_view(["Rule", "Action", "Records", "Meaning"],
                              [[rule, action, f"{n:,}", desc] for rule, action, n, desc in d["top_rules"]],
                              numeric_from=2),
                 wide=True)

    sections = [
        ("Overview", overview + '<div class="cards">' + trend + payment + "</div>"),
        ("Customers", f'<div class="cards">{rfm}{recency}</div>'),
        ("Products", f'<div class="cards">{categories}{tiers}{products}</div>'),
        ("Vendors", f'<div class="cards">{vendors}</div>'),
        ("Data quality", f'<div class="cards">{quality}{rules}</div>'),
    ]
    nav = "".join(f'<a href="#{escape(t.lower().replace(" ", "-"))}">{escape(t)}</a>' for t, _ in sections)
    body = "".join(f'<h2 id="{escape(t.lower().replace(" ", "-"))}">{escape(t)}</h2>{s}' for t, s in sections)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>E-commerce KPIs</title><style>{_CSS}</style></head><body>"
        '<header><div class="wrap"><h1>E-commerce KPIs</h1>'
        f'<p class="sub">{escape(start)} to {escape(end)} · reference date {escape(d["as_of"])} · '
        "generated by <code>python run_pipeline.py</code> from the Gold tables · aggregates only, no customer data"
        f'</p><nav>{nav}</nav></div></header><main class="wrap">{body}'
        '<p class="foot">Revenue = paid orders that were not cancelled, net of discounts, returns negative '
        "(the same rule as the feature tables and the graph). Sources: <code>outputs/processed/gold/</code>, "
        "Silver transactions for the monthly series, and the validation and cleaning metrics.</p></main>"
        f'<div id="tip" role="status" hidden></div><script>{_JS}</script></body></html>\n'
    )


# ── stage ────────────────────────────────────────────────────────────────────────


def write_dashboard(config: PipelineConfig) -> Path:
    gold = {n: pd.read_parquet(config.paths.gold_dir / f"{n}.parquet") for n in GOLD_TABLES}
    transactions = pd.read_parquet(config.paths.silver_dir / CLEAN_SUBDIR / "transactions.parquet")
    validation = json.loads((config.paths.silver_dir / VALIDATED_SUBDIR / METRICS_FILE).read_text())
    cleaning = json.loads((config.paths.silver_dir / CLEAN_SUBDIR / CLEANING_METRICS_FILE).read_text())
    quality = merge_metrics(validation["entities"], cleaning["entities"])
    data = dashboard_data(gold, transactions, quality, config.run.as_of_date.isoformat())
    path = config.paths.dashboard
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dashboard(data))
    logger.bind(stage="dashboard").info("Dashboard written", path=str(path),
                                        net_revenue=data["kpis"]["net_revenue"], orders=data["kpis"]["orders"])
    return path


# Colours: the validated reference palette (categorical slots 1–4), light and dark steps.
_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;
--on-s4:#0b0b0b;--wash:rgba(42,120,214,.10)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--ink:#f0efec;--ink-2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--on-s4:#ffffff;--wash:rgba(57,135,229,.10)}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink-2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;
--on-s4:#ffffff;--wash:rgba(57,135,229,.10)}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:0 16px}
header{border-bottom:1px solid var(--ring);background:var(--surface);padding:20px 0 0;position:sticky;top:0;z-index:2}
@media (max-width:700px){header{position:static}h2{scroll-margin-top:8px}}
h1{margin:0;font-size:22px;font-weight:600}
h2{font-size:17px;font-weight:600;margin:32px 0 12px;scroll-margin-top:110px}
h3{font-size:15px;font-weight:600;margin:0}
.sub{color:var(--ink-2);margin:4px 0 12px;font-size:13px}
nav{display:flex;gap:4px;overflow-x:auto;margin-top:8px;scrollbar-width:none}
nav a{color:var(--ink-2);text-decoration:none;padding:8px 10px;font-size:14px;white-space:nowrap;border-bottom:2px solid transparent}
nav a:hover{color:var(--ink);border-bottom-color:var(--axis)}
code{font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr));gap:16px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:16px 18px;min-width:0}
.card.wide{grid-column:1/-1}
.hero{margin:4px 0 16px}
.hero-value{font-size:52px;font-weight:600;line-height:1.1}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:16px;margin-bottom:16px}
.kpi{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px 16px}
.kpi-label{color:var(--ink-2);font-size:13px}
.kpi-value{font-size:26px;font-weight:600;margin:2px 0}
.kpi-note{color:var(--muted);font-size:12px}
.chart-box{overflow-x:auto}
svg.chart{width:100%;height:auto;display:block;overflow:visible;min-width:500px}
svg.chart.line,svg.chart.wide{min-width:760px}
svg text{font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
.label,.tick{fill:var(--ink-2)} .tick{fill:var(--muted);font-variant-numeric:tabular-nums}
.value{fill:var(--ink);font-weight:600}
.bar{fill:var(--s1)} .hit-area{fill:transparent}
.hit{outline:none} .hit:hover .bar,.hit:focus .bar{fill-opacity:.75}
.hit:focus-visible .hit-area{stroke:var(--axis)}
line.grid{stroke:var(--grid);stroke-width:1} line.axis{stroke:var(--axis);stroke-width:1}
path.line{fill:none;stroke:var(--s1);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
path.area{fill:var(--wash)}
.dot{fill:var(--s1);stroke:var(--surface);stroke-width:2}
.crosshair{stroke:var(--axis);stroke-width:1}
svg.line{outline:none}
.segbar{display:flex;gap:2px;width:100%}
.segbar.tall{height:28px;margin:8px 0 4px}
.segbar.thin{height:8px;min-width:120px}
.segbar.thin .seg:first-child{border-radius:4px 0 0 4px}.segbar.thin .seg:last-child{border-radius:0 4px 4px 0}
.segbar.thin .seg:only-child{border-radius:4px}
.seg{display:flex;align-items:center;padding-left:8px;color:#fff;font-size:12px;font-weight:600;min-width:2px;outline:none}
.segbar.tall .seg{border-radius:4px}
.seg:hover,.seg:focus{filter:brightness(1.1)}
.s1{background:var(--s1)}.s2{background:var(--s2)}.s3{background:var(--s3)}.s4{background:var(--s4);color:var(--on-s4)}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin:8px 0;font-size:13px;color:var(--ink-2)}
.key{display:inline-flex;align-items:center;gap:6px}
.swatch{width:10px;height:10px;border-radius:2px;display:inline-block}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--grid);white-space:nowrap}
th{color:var(--ink-2);font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums}
td.bar-cell{width:30%}
details{margin-top:10px;font-size:13px} summary{cursor:pointer;color:var(--ink-2)}
.foot{color:var(--muted);font-size:12px;margin:32px 0}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--ring);
border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);z-index:10;max-width:280px}
#tip strong{display:block;font-size:14px} #tip span{color:var(--ink-2)}
"""

# Tooltips only: every value is also printed on the chart or in its table view.
_JS = """
(function(){
const tip=document.getElementById('tip');
function show(value,label,x,y){tip.replaceChildren();const v=document.createElement('strong');v.textContent=value;
const l=document.createElement('span');l.textContent=label;tip.append(v,l);tip.hidden=false;
const w=tip.offsetWidth,h=tip.offsetHeight;tip.style.left=Math.min(x+12,innerWidth-w-8)+'px';
tip.style.top=Math.max(8,y-h-12)+'px';}
function hide(){tip.hidden=true;}
document.querySelectorAll('[data-value]').forEach(function(el){
el.addEventListener('pointermove',function(e){show(el.dataset.value,el.dataset.label,e.clientX,e.clientY);});
el.addEventListener('pointerleave',hide);el.addEventListener('blur',hide);
el.addEventListener('focus',function(){const r=el.getBoundingClientRect();show(el.dataset.value,el.dataset.label,r.left+r.width/2,r.top);});});
document.querySelectorAll('svg.line').forEach(function(svg){
const s=JSON.parse(svg.dataset.series),cross=svg.querySelector('.crosshair'),dot=svg.querySelector('.cursor');let idx=s.x.length-1;
function at(i,cx,cy){idx=Math.max(0,Math.min(s.x.length-1,i));cross.setAttribute('x1',s.x[idx]);cross.setAttribute('x2',s.x[idx]);
dot.setAttribute('cx',s.x[idx]);dot.setAttribute('cy',s.y[idx]);cross.setAttribute('visibility','visible');dot.setAttribute('visibility','visible');
if(cx===undefined){const r=svg.getBoundingClientRect(),k=r.width/svg.viewBox.baseVal.width;cx=r.left+s.x[idx]*k;cy=r.top+s.y[idx]*k;}
show(s.value[idx],s.label[idx],cx,cy);}
function off(){cross.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');hide();}
svg.addEventListener('pointermove',function(e){const r=svg.getBoundingClientRect(),vx=(e.clientX-r.left)*svg.viewBox.baseVal.width/r.width;
let best=0;s.x.forEach(function(x,i){if(Math.abs(x-vx)<Math.abs(s.x[best]-vx))best=i;});at(best,e.clientX,e.clientY);});
svg.addEventListener('pointerleave',off);svg.addEventListener('blur',off);svg.addEventListener('focus',function(){at(idx);});
svg.addEventListener('keydown',function(e){if(e.key==='ArrowLeft'){at(idx-1);e.preventDefault();}if(e.key==='ArrowRight'){at(idx+1);e.preventDefault();}});});
})();
"""
