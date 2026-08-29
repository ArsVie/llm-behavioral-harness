"""WS-D spend report — totals + by lane/model/day-window, cache savings.

Run from the worktree::

    python -m harness.spend <companion.db> [--days N] [--day-start D]
        [--day-end D] [--lane L] [--model M] [--out results/spend-report-<ts>.txt]
        [--pricing-json pricing.json]

Reads the ``llm_calls`` ledger (v8 columns; legacy NULL rows count as
calls with no tokens/dollars), prices each row with the tiered cached-input
formula (``harness.pricing``), and renders a plain-text report: grand
totals, per-lane (product | research | NULL/unknown), per-model, per-day
window, plus cache-hit rate and cache savings (what the calls would have
cost fully-uncached minus the actual tiered cost). While
``harness.pricing.PRICING_PENDING`` is True the report prints a
"PRICING PENDING" banner and dollar figures are labeled as placeholder
math — the user must fill real rates first.

Aggregation is pure and separately testable (``aggregate`` /
``aggregate_by`` / ``render_report``); the CLI only reads the store.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from harness.pricing import (
    MODELS,
    PRICING_PENDING,
    price_for,
    rates_from_json,
)
from harness.store import SQLiteStore

LANE_LABELS = {"product": "product (Lily live bot)", "research": "research (judges/experiments)"}


@dataclass
class GroupStats:
    """Aggregated spend of a group of llm_calls rows (WS-D).

    Rows with NULL token columns (legacy pre-v8) contribute calls only.
    ``cost_usd`` uses the tiered formula: cached at the cheap cached rate,
    cache-miss at the fresh input rate, completion at the output rate.
    ``uncached_cost_usd`` is the hypothetical fully-uncached bill of the
    same tokens; ``savings_usd`` = uncached - actual = cached tokens times
    the cached-rate discount. ``unpriced_calls`` counts rows whose model
    is absent (no rate table entry — no dollars for them).
    """

    calls: int = 0
    priced_calls: int = 0
    unpriced_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_miss_tokens: int = 0
    cost_usd: float = 0.0
    uncached_cost_usd: float = 0.0
    savings_usd: float = 0.0
    raw_cost_usd: float = 0.0

    @property
    def cache_hit_rate(self) -> float | None:
        """Fraction of input tokens served from cache (None when no input)."""
        inp = self.cached_tokens + self.cache_miss_tokens
        if inp <= 0:
            return None
        return self.cached_tokens / inp

    def add(self, row: dict, rates: dict[str, float] | None) -> None:
        """Fold one llm_calls row into this group (rates=None => unpriced)."""
        self.calls += 1
        pt = row.get("prompt_tokens") or 0
        ct = row.get("completion_tokens") or 0
        tt = row.get("total_tokens") or 0
        cached = row.get("cached_tokens") or 0
        miss = row.get("cache_miss_tokens") or 0
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.total_tokens += tt
        self.cached_tokens += cached
        self.cache_miss_tokens += miss
        rc = row.get("raw_cost")
        if isinstance(rc, (int, float)) and not isinstance(rc, bool):
            self.raw_cost_usd += float(rc)
        if rates is None:
            self.unpriced_calls += 1
            return
        self.priced_calls += 1
        in_rate = rates["input_per_mtok"]
        cached_rate = rates["cached_input_per_mtok"]
        out_rate = rates["output_per_mtok"]
        actual = (
            cached / 1e6 * cached_rate
            + miss / 1e6 * in_rate
            + ct / 1e6 * out_rate
        )
        uncached = (
            (cached + miss) / 1e6 * in_rate + ct / 1e6 * out_rate
        )
        self.cost_usd += actual
        self.uncached_cost_usd += uncached
        self.savings_usd += uncached - actual


def aggregate(rows: Iterable[dict], pricing: dict[str, dict[str, float]] | None = None) -> GroupStats:
    """Grand-total stats over an iterable of llm_calls rows.

    ``pricing`` overrides the module-level ``MODELS`` table for this call
    only (no global side effect); ``None`` uses the built-in rates.
    """
    out = GroupStats()
    for row in rows:
        out.add(row, price_for(row.get("model"), pricing))
    return out


def aggregate_by(
    rows: Iterable[dict],
    key: Callable[[dict], str],
    pricing: dict[str, dict[str, float]] | None = None,
) -> dict[str, GroupStats]:
    """Stats grouped by ``key(row)``; groups appear in first-seen order.

    ``pricing`` overrides the module-level ``MODELS`` table for this call
    only (no global side effect); ``None`` uses the built-in rates.
    """
    groups: dict[str, GroupStats] = {}
    for row in rows:
        k = key(row)
        if k not in groups:
            groups[k] = GroupStats()
        groups[k].add(row, price_for(row.get("model"), pricing))
    return groups


def _fmt_usd(v: float) -> str:
    return f"${v:,.4f}"


def _fmt_rate(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def render_report(
    total: GroupStats,
    by_lane: dict[str, GroupStats],
    by_model: dict[str, GroupStats],
    by_day: dict[str, GroupStats],
    *,
    window_desc: str,
    pricing_pending: bool = PRICING_PENDING,
) -> str:
    """Render the plain-text spend report (deterministic, table-shaped)."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("LLM SPEND REPORT (WS-D)")
    lines.append(f"window: {window_desc}")
    if pricing_pending:
        lines.append("PRICING PENDING — rates in harness/pricing.py are placeholders;")
        lines.append("dollar figures below are placeholder math until the user fills")
        lines.append("real $/1M rates and sets PRICING_PENDING = False.")
    lines.append("=" * 78)

    def _group_block(title: str, groups: dict[str, GroupStats]) -> None:
        lines.append("")
        lines.append(f"-- {title} --")
        lines.append(
            f"{'group':<34}{'calls':>6}{'in':>10}{'out':>10}{'cached':>9}"
            f"{'miss':>9}{'hit%':>8}{'cost':>12}{'saved':>12}"
        )
        for name, g in groups.items():
            label = LANE_LABELS.get(name, name)
            lines.append(
                f"{label:<34}{g.calls:>6}{g.prompt_tokens:>10,}"
                f"{g.completion_tokens:>10,}{g.cached_tokens:>9,}"
                f"{g.cache_miss_tokens:>9,}{_fmt_rate(g.cache_hit_rate):>8}"
                f"{_fmt_usd(g.cost_usd):>12}{_fmt_usd(g.savings_usd):>12}"
            )
        if not groups:
            lines.append(f"{'<no calls>':<34}")

    _group_block("TOTAL", {"total": total})
    _group_block("BY LANE", by_lane)
    _group_block("BY MODEL", by_model)
    _group_block("BY DAY", by_day)

    rate = total.cache_hit_rate
    lines.append("")
    lines.append("-" * 78)
    lines.append("Summary")
    lines.append(f"  calls logged        : {total.calls:,}  (priced {total.priced_calls:,}, "
                 f"unpriced {total.unpriced_calls:,})")
    lines.append(f"  input tokens        : {total.prompt_tokens:,}  "
                 f"(cached {total.cached_tokens:,} / miss {total.cache_miss_tokens:,})")
    lines.append(f"  completion tokens   : {total.completion_tokens:,}")
    lines.append(f"  total tokens        : {total.total_tokens:,}")
    lines.append(f"  cache-hit rate      : {_fmt_rate(rate)}")
    lines.append(f"  tiered cost         : {_fmt_usd(total.cost_usd)}"
                 + ("  (placeholder)" if pricing_pending else ""))
    lines.append(f"  uncached cost       : {_fmt_usd(total.uncached_cost_usd)}"
                 + ("  (placeholder)" if pricing_pending else ""))
    lines.append(f"  cache savings       : {_fmt_usd(total.savings_usd)}"
                 + ("  (placeholder)" if pricing_pending else ""))
    if total.raw_cost_usd:
        lines.append(
            f"  gateway-reported cost: {_fmt_usd(total.raw_cost_usd)} "
            f"(sum of raw_cost; cross-check vs tiered cost)"
        )
    lines.append("-" * 78)
    if pricing_pending:
        lines.append(
            "USER ACTION: fill real $/1M rates in harness/pricing.py "
            "(input / cached input / output per model) and set "
            "PRICING_PENDING = False."
        )
    return "\n".join(lines) + "\n"


def _rows_for_window(store, args) -> list[dict]:
    """llm_calls rows honoring the CLI's window/model/lane filters."""
    where: list[str] = []
    params: list = []
    if args.day_start is not None:
        where.append("day >= ?")
        params.append(args.day_start)
    if args.day_end is not None:
        where.append("day <= ?")
        params.append(args.day_end)
    if args.days is not None:
        where.append("day >= (SELECT COALESCE(MAX(day), 0) - ? + 1 FROM llm_calls)")
        params.append(args.days)
    if args.lane is not None:
        where.append("lane = ?")
        params.append(args.lane)
    if args.model is not None:
        where.append("model = ?")
        params.append(args.model)
    sql = "SELECT * FROM llm_calls"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY day, t_h, id"
    return [dict(r) for r in store.conn.execute(sql, params).fetchall()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.spend",
                                 description="WS-D spend report over the llm_calls ledger.")
    ap.add_argument("db", help="path to the harness SQLite database")
    ap.add_argument("--days", type=int, default=None,
                    help="only the last N days (by llm_calls.day)")
    ap.add_argument("--day-start", type=int, default=None)
    ap.add_argument("--day-end", type=int, default=None)
    ap.add_argument("--lane", choices=("product", "research"), default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=None,
                    help="report file path (default results/spend-report-<date>.txt)")
    ap.add_argument("--pricing-json", default=None,
                    help="override pricing table from a JSON file "
                         "{model: {input_per_mtok, cached_input_per_mtok, output_per_mtok}}")
    args = ap.parse_args(argv)

    pricing_override: dict[str, dict[str, float]] | None = None
    if args.pricing_json:
        payload = json.loads(Path(args.pricing_json).read_text())
        pricing_override = {**MODELS, **rates_from_json(payload)}

    store = SQLiteStore(args.db)
    try:
        rows = _rows_for_window(store, args)
    finally:
        store.close()

    total = aggregate(rows, pricing=pricing_override)
    by_lane = aggregate_by(rows, lambda r: r.get("lane") or "unknown", pricing=pricing_override)
    by_model = aggregate_by(rows, lambda r: r.get("model") or "unknown", pricing=pricing_override)
    by_day = aggregate_by(rows, lambda r: f"day {r['day']}", pricing=pricing_override)

    window_desc = "all time"
    if args.days is not None:
        window_desc = f"last {args.days} day(s)"
    if args.day_start is not None or args.day_end is not None:
        window_desc += f" [day {args.day_start}..{args.day_end}]" if args.day_start is not None and args.day_end is not None \
            else f" [day >= {args.day_start}]" if args.day_start is not None else f" [day <= {args.day_end}]"

    report = render_report(total, by_lane, by_model, by_day,
                           window_desc=window_desc,
                           pricing_pending=PRICING_PENDING)

    out_path = Path(args.out) if args.out else (
        Path("results") / f"spend-report-{date.today().isoformat()}.txt"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(report)
    print(f"[spend] report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
