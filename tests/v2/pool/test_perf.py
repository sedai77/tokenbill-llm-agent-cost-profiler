"""Performance budget (addendum §17): cells + pool months + forecast over 10⁶ cost lines ≤ 20 s.
The full-size test carries the ``perf`` marker (nightly); the PR variant runs at 1/10 size with a
1/10 budget. Budgets are checked on process CPU time (best of three runs for the PR variant) so
parallel builds on a shared machine do not flake."""

from __future__ import annotations

import dataclasses
import sys
import time
from collections.abc import Callable

import pytest

from tokenbill.core import builders as b
from tokenbill.core.pool import build_cells, pool_months
from tokenbill.core.records import CostLine, UsageAggregate


def _world(n: int) -> tuple[list[CostLine], list[UsageAggregate]]:
    """*n* AI usage report rows of an open month (500 users, 40 teams, 4 models, 28 days) and one
    token aggregate per (day, team, model)."""
    line0, agg0 = b.make_ai_usage_row(date_utc="2026-09-01", credits="12.5",
                                      discount_credits="10", input_tokens=1_000)
    users = [b.make_principal(i) for i in range(500)]
    models = ("claude-opus-5-5", "claude-sonnet-5", "gpt-5.5", "gemini-3.8-flash")
    lines = [dataclasses.replace(line0, line_id=f"cl_{i}", date_utc=f"2026-09-{i % 28 + 1:02d}",
                                 principal=users[i % 500], team=f"t{i % 40}",
                                 model=models[i % 4]) for i in range(n)]
    day_ms = 86_400_000
    aggs = []
    for day in range(28):
        for team in range(40):
            for model in models:
                dims = dict(agg0.dims) | {"team": f"t{team}", "model": model}
                aggs.append(dataclasses.replace(
                    agg0, agg_id=f"ag_{day}_{team}_{model}", dims=tuple(sorted(dims.items())),
                    bucket_start_ms=agg0.bucket_start_ms + day * day_ms,
                    bucket_end_ms=agg0.bucket_end_ms + day * day_ms))
    return lines, aggs


def _traced() -> bool:
    """True under a tracer or coverage measurement, where CPU budgets are not meaningful."""
    if sys.gettrace() is not None:
        return True
    cov = sys.modules.get("coverage")
    current = getattr(getattr(cov, "Coverage", None), "current", None)
    return bool(current and current() is not None)


def _cpu(fn: Callable[[], object], repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.process_time()
        fn()
        best = min(best, time.process_time() - start)
    return best


def _run(lines: list[CostLine], aggs: list[UsageAggregate]) -> None:
    cells, _ = build_cells(aggs, lines)
    months = pool_months(cells, lines, [], [], today="2026-09-23")
    assert [pm.plan_scenario for pm in months] == ["business", "enterprise"]
    assert all(pm.forecast is not None for pm in months)


def _budget(n: int, budget_s: float, repeat: int) -> None:
    if _traced():
        pytest.skip("CPU budgets are not measured under coverage or a tracer")
    lines, aggs = _world(n)
    elapsed = _cpu(lambda: _run(lines, aggs), repeat=repeat)
    assert elapsed <= budget_s, f"cells + pool months over {n} cost lines took {elapsed:.1f} s"


def test_pr_budget_tenth_size() -> None:
    _budget(100_000, 2.0, repeat=3)


@pytest.mark.perf
def test_full_budget() -> None:
    _budget(1_000_000, 20.0, repeat=1)
