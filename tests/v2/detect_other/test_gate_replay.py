"""Gate (merge gate 1): the replay-based figures with REPLAY's real ``UsageReplayer`` on
hand-computed lanes — delegation and the same-tier upgrade (the direct arithmetic equals the
replay), default effort, batch eligibility, the keepalive/TTL choice of a scheduled cadence and
the Appendix A.6 compaction window (a negative saving is never recommended)."""

from __future__ import annotations

from typing import Any

import pytest

from tokenbill.core.labels import Evidence
from tokenbill.core.policy import parse_policy
from tokenbill.core.records import LaneKind, WorkloadClass
from tokenbill.detect.automation import Automation
from tokenbill.detect.context import CompactionWindow
from tokenbill.detect.model import Routing

from .helpers import OPUS5, OPUS55, PRICER, RULES, SONNET5, by_kind, ctx, lane, one

pytestmark = pytest.mark.gate
usage_replay = pytest.importorskip("tokenbill.sim.usage_replay")


def _ctx(**kw: Any):
    return ctx(replayer=usage_replay.UsageReplayer(), **kw)


def test_delegation_opus5_subagent_to_sonnet5() -> None:
    # Opus 5: 145,025,000 + 48,775,000; Sonnet 5 (same tokenizer, no band): 58,010,000 +
    # 19,510,000 → saving 116,280,000
    sub = lane("L-sub", [(0, 0, 20_000, 0, 5, 800), (20, 20_000, 3_000, 0, 5, 800)],
               kind=LaneKind.SUBAGENT, model=OPUS5)
    f = one(Routing().detect([sub], _ctx(thresholds={"min_usd": "0.01"})), "delegation-routing")
    assert f.cost_observed.nano == 193_800_000
    assert f.recoverable.nano == 116_280_000 and f.recoverable.upper_bound
    assert f.recoverable.evidence is Evidence.ESTIMATED


def test_same_tier_arithmetic_equals_the_replay() -> None:
    ln = lane("L-o5", [(0, 0, 40_000, 0, 5, 800), (30, 40_000, 5_000, 0, 5, 800),
                       (60, 45_000, 5_000, 0, 5, 800)], model=OPUS5)
    f = one(Routing().detect([ln], _ctx(thresholds={"min_usd": "0.01"})), "same-tier-upgrade")
    result = usage_replay.UsageReplayer().replay(
        [ln], parse_policy(f"model={OPUS55}@model:{OPUS5}"), mode="documented", pricer=PRICER,
        rules=RULES, calibration=None)
    assert f.recoverable.nano == result.saving.nano


def test_default_effort_halves_thinking() -> None:
    # effort high, output 1,000 of which 400 reasoning: scale 0.5 cuts 200 output tokens per
    # request at $20/M on Opus 5.5
    lanes = [lane(f"L-{i}", [(0, 0, 30_000, 0, 5, 1_000), (30, 30_000, 4_000, 0, 5, 1_000)],
                  per_request={0: {"effort": "high", "reasoning": 400},
                               1: {"effort": "high", "reasoning": 400}}) for i in range(2)]
    f = one(Routing().detect(lanes, _ctx(thresholds={"min_usd": "0.001"})), "default-effort")
    assert f.recoverable.nano == 4 * 200 * 20_000
    assert f.recoverable.upper_bound


def test_batch_eligible_halves_single_calls() -> None:
    singles = [lane(f"L-b{i}", [(i * 10, 0, 0, 0, 10_000, 1_000)], model=SONNET5,
                    kind=LaneKind.API_RUN, product="api", workload=WorkloadClass.SERVICE)
               for i in range(3)]
    f = one(Automation().detect(singles, _ctx(thresholds={"min_usd": "0.01"})), "batch-eligible")
    assert f.cost_observed.nano == 90_000_000 and f.recoverable.nano == 45_000_000


def test_scheduled_cadence_prefers_keepalive_for_an_sdk_agent() -> None:
    rows = [(t, 0, 50_000, 0, 0, 100) for t in (0, 600, 1_205, 1_800, 2_400)]
    sdk = lane("L-cron", rows, kind=LaneKind.API_RUN, product="agent_sdk",
               workload=WorkloadClass.SCHEDULED)
    f = one(Automation().detect([sdk], _ctx(thresholds={"min_usd": "0.10"})),
            "scheduled-cadence")
    # keepalive: 2 pings per gap reading 50,000 tokens + a warm read, instead of a 50,000-token
    # rewrite at $5/M: 4 × (250,000,000 − 3 × 10,000,000) = 880,000,000
    assert f.cost_observed.nano == 1_000_000_000
    assert f.recoverable.nano == 880_000_000 and "keepalive" in f.summary


def test_a6_compaction_window_is_not_recommended() -> None:
    # Appendix A.6 (S_c = 20,000): on this short Sonnet 5 lane a 400k window costs $0.569 more
    a6 = lane("L-a6", [(0, 0, 300_000, 0, 0, 1_000), (30, 300_000, 150_000, 0, 0, 1_000),
                       (60, 450_000, 50_000, 0, 0, 1_000)], model=SONNET5)
    result = usage_replay.UsageReplayer().replay(
        [a6], parse_policy("compact-window=400000,post=20000"), mode="documented", pricer=PRICER,
        rules=RULES, calibration=None)
    assert result.saving.nano == -569_000_000
    post = {"min_usd": "0", "context.compaction-window.post_tokens": "20000"}
    assert by_kind(CompactionWindow().detect([a6], _ctx(thresholds=post)),
                   "compaction-window") == []
