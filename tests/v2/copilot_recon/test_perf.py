"""Performance: reconciling one month of an AI usage report fits the ``copilot scan`` budget
(addendum §17: 5,000 seats, one month ≤ 3 min for the whole scan). Nightly ``perf`` variant with
600,000 rows; PR variant with 1/30 of it (CPU seconds, generous for a shared machine)."""

from __future__ import annotations

import time

import pytest

from tokenbill.core.builders import make_ai_usage_row
from tokenbill.core.ids import natural_id
from tokenbill.core.records import UsageAggregate, UsageBuckets

from . import world as w

MODELS = ("Claude Sonnet 5", "GPT-5.5", "Auto: Claude Haiku 4.5")


def _month(users: int, teams: int = 40):
    lines, aggs = [], []
    for date in w.days("2026-09"):
        tokens: dict[tuple[str, str], list[int]] = {}
        for u in range(users):
            model, team = MODELS[u % 3], f"team{u % teams}"
            line, agg = make_ai_usage_row(date_utc=date, model=model, credits="12.5",
                                          principal=w.p(f"u{u}"), team=team,
                                          input_tokens=1000, output_tokens=100,
                                          cache_read_tokens=5000)
            lines.append(line)
            t = tokens.setdefault((team, model), [0, 0, 0, dict(agg.dims)])
            t[0] += 1000
            t[1] += 100
            t[2] += 5000
        for (team, model), (inp, out, read, dims) in tokens.items():
            start = w.day_ms(date)
            aggs.append(UsageAggregate(
                agg_id=natural_id("ag", "github.ai_usage_report", date, team, model),
                source_kind="github.ai_usage_report", bucket_start_ms=start,
                bucket_end_ms=start + w.DAY_MS, dims=tuple(sorted(dims.items())),
                usage=UsageBuckets(uncached_input=inp, cache_read=read, output=out),
                finality="final"))
    return lines, aggs


def _run(users: int, budget_s: float) -> None:
    lines, aggs = _month(users)
    store, rs = w.world()
    w.ingest(store, lines=lines, aggs=aggs)
    start = time.process_time()
    report = w.reconcile(store, rs)
    elapsed = time.process_time() - start
    assert report.channels[0].channel == "github_copilot"
    assert elapsed <= budget_s, f"{len(lines)} rows reconciled in {elapsed:.1f} s CPU"


def test_reconcile_pr_variant() -> None:
    _run(users=667, budget_s=15)           # 20,010 rows


@pytest.mark.perf
def test_reconcile_nightly_600k_rows() -> None:
    _run(users=20_000, budget_s=60)        # 600,000 rows
