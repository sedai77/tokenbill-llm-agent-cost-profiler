"""Addendum §17 budget: the aggregate plan (≤ 6 levers, 64 joint evaluations per state, ≤ 50k
monthly cells) ≤ 10 s. Full size under the ``perf`` marker (nightly); the PR variant runs a tenth
of the cells against a tenth of the budget plus a fixed allowance for pricing-cache warm-up.
Budgets are process CPU time (best of three for the PR variant, so parallel builds on a shared
machine do not flake) and are not measured under coverage or a tracer (the F-POOL convention)."""

from __future__ import annotations

import sys
import time

import pytest

from tokenbill.copilot.plan import plan_copilot
from tokenbill.core import builders as b
from tokenbill.core.labels import Basis
from tokenbill.core.pool import Cell
from tokenbill.core.records import UsageBuckets

from .worlds import PRICER, finding, idle

_MODELS = (("claude-sonnet-5", "standard"), ("claude-opus-4-8", "standard"),
           ("claude-opus-4-8", "fast"), ("gpt-5.5", "standard"), ("claude-opus-4-7", "standard"),
           ("claude-haiku-4-5", "standard"))


def _cells(n: int) -> list[Cell]:
    """*n* month-grain cells over 200 teams, 6 model / speed variants, two routings and a few
    SKUs — a large enterprise's month."""
    out = []
    for i in range(n):
        model, speed = _MODELS[i % len(_MODELS)]
        tokens = 1_000 + i % 997
        credits = str(20 + i % 50)
        gross = int(credits) * 10**7
        out.append(Cell(
            month="2026-09", date_utc=None, entity_id="enterprise", team=f"team-{i % 200}",
            cost_center=None, org=f"org-{i % 3}", sku=f"sku-{i % 7}",
            cost_type="ai_credit.user" if i % 11 else "ai_credit.direct", model=model,
            routing="auto" if i % 5 == 0 else "direct", speed=speed, pseudo=None, workload=None,
            usage=UsageBuckets(uncached_input=tokens, cache_read=10 * tokens,
                               output=tokens // 4),
            credits=credits, gross_nano=gross, discount_nano=0, net_nano=gross, n_users=1,
            final=True))
    return out


def _run(n: int) -> float:
    cells = _cells(n)
    consumed = sum(int(c.credits) for c in cells if c.cost_type == "ai_credit.user") * 10**7
    pm = b.make_pool_month(seats={"business": "5000"}, consumed_report_nano=consumed)
    activity = [b.make_activity(b.make_principal(f"a{t}"), team=f"team-{t}",
                                counts={"ide:intellij": t % 4, "ide:vscode": 3})
                for t in range(0, 200, 2)]           # half the teams without activity: a range
    fs = [finding("auto-adoption", team="team-1"), finding("fast-mode", team="team-2"),
          finding("premium-model-share", model="claude-opus-4-8"),
          finding("premium-model-share", model="gpt-5.5"),
          finding("premium-model-share", model="claude-opus-4-7"), idle(40)]
    start = time.process_time()
    plan = plan_copilot(cells, [pm], fs, PRICER, lines=[], activity=activity,
                        month="2026-09", include_tradeoffs=True)
    elapsed = time.process_time() - start
    listed = [lv for lv in plan.levers if lv.basis is Basis.LIST]
    assert len(listed) == 6 and plan.method == "shapley-exact"
    assert sum(lv.shapley.nano for lv in listed) == plan.joint_saving.nano
    return elapsed


def _traced() -> bool:
    """True under a tracer or coverage measurement, where CPU budgets are not meaningful."""
    if sys.gettrace() is not None:
        return True
    cov = sys.modules.get("coverage")
    current = getattr(getattr(cov, "Coverage", None), "current", None)
    return bool(current and current() is not None)


def test_large_plan_is_efficient_and_uses_exact_shapley() -> None:
    _run(2_000)          # the assertions inside _run, without a budget


@pytest.mark.perf
def test_aggregate_plan_50k_cells_within_10s() -> None:
    if _traced():
        pytest.skip("CPU budgets are not measured under coverage or a tracer")
    assert _run(50_000) <= 10


def test_aggregate_plan_pr_variant_5k_cells() -> None:
    if _traced():
        pytest.skip("CPU budgets are not measured under coverage or a tracer")
    assert min(_run(5_000) for _ in range(3)) <= 1 + 1
