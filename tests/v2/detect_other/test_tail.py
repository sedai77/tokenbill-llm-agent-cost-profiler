"""``tail.runaway``: cohort statistics (p95 / p99, nearest rank), break-glass semantics (team only
unless ``ctx.break_glass``) and idle loops."""

from __future__ import annotations

import json

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind, to_json
from tokenbill.detect.tail import Runaway

from .helpers import CAPS, SONNET5, by_kind, ctx, event, evidence, lane, one


def _small(n: int = 120, **kw):
    # $0.03 each: 10,000 × 2,000 + 1,000 × 10,000
    return [lane(f"L-s{i:03d}", [(i * 7_200, 0, 0, 0, 10_000, 1_000)], model=SONNET5,
                 team="ops", principal=f"r_dev{i % 7}", **kw) for i in range(n)]


def _loop(**kw):
    # fifteen Opus 5.5 calls of 1,000,000 uncached tokens ($4 each), 240 s apart: $60 in an hour
    rows = [(1_000_000 + 240 * i, 0, 0, 0, 1_000_000, 0) for i in range(15)]
    return lane("L-loop", rows, team="ops", principal="r_loop", session_key="s_runaway", **kw)


def test_runaway_session_team_only() -> None:
    f = one(Runaway().detect(_small() + [_loop()], ctx()), "runaway-session")
    # limit = max($50, 5 × p99): p99 (rank 120 of 121 hourly peaks) = $0.03 → $50
    assert f.cost_observed.nano == 60_000_000_000 - 30_000_000       # above the p95 ($0.03)
    assert f.cost_observed.evidence is Evidence.EXACT and f.recoverable is None
    assert f.scope.dims == (("team", "ops"),)
    cohort = evidence(f, "tail:cohort")
    assert (cohort["sessions"], cohort["p95_session_nano"], cohort["p99_hourly_nano"],
            cohort["threshold_nano"]) == (121, 30_000_000, 30_000_000, 50_000_000_000)
    item = evidence(f, "tail:runaway-session:1")
    assert (item["requests"], item["rolling_1h_max_nano"]) == (15, 60_000_000_000)
    text = json.dumps(to_json(f))
    assert "s_runaway" not in text and "L-loop" not in text
    assert "--break-glass" in f.summary and f.n_users == 1


def test_runaway_break_glass_names_the_session() -> None:
    f = one(Runaway().detect(_small() + [_loop()], ctx(break_glass="incident 42")),
            "runaway-session")
    assert f.scope.dims == (("session", "s_runaway"), ("team", "ops"))
    assert evidence(f, "s_runaway")["nano"] == 59_970_000_000


def test_runaway_limits() -> None:
    assert by_kind(Runaway().detect(_small(), ctx(thresholds={"min_usd": "0"})),
                   "runaway-session") == []
    # a small cohort: the loop is its own p99, so 5 × p99 exceeds its hour
    assert by_kind(Runaway().detect(_small(20) + [_loop()], ctx()), "runaway-session") == []
    lowered = ctx(thresholds={"tail.runaway.min_hourly_usd": "10"})
    assert by_kind(Runaway().detect(_small() + [_loop()], lowered), "runaway-session")
    raised = ctx(thresholds={"tail.runaway.min_hourly_usd": "70"})
    assert by_kind(Runaway().detect(_small() + [_loop()], raised), "runaway-session") == []


def test_runaway_cohorts_scope_lane_kind_and_billing_class() -> None:
    sub = _small(kind=LaneKind.SUBAGENT) + [_loop(kind=LaneKind.SUBAGENT)]
    f = one(Runaway().detect(sub, ctx()), "runaway-session")
    assert f.scope.dims == (("lane_kind", "subagent"), ("team", "ops"))
    seat = _small(billing_path="subscription") + [_loop(billing_path="subscription")]
    f = one(Runaway().detect(seat, ctx()), "runaway-session")
    assert f.scope.dims == (("billing_class", "allowance"), ("team", "ops"))
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    assert f.title.startswith("Allowance headroom:")


def _idle(events=True, middle=False):
    rows = [(2_000_000 + 70 * i, 0, 0, 0, 10_000, 1_000) for i in range(60)]
    evs = [event("L-idle", 2_000_000, "human_prompt")] if events else []
    if middle:
        evs.append(event("L-idle", 2_000_000 + 70 * 30, "human_prompt"))
    return lane("L-idle", rows, model=SONNET5, team="ops", principal="r_bot", events=evs,
                session_key="s_idle")


def test_idle_loop_needs_human_prompts() -> None:
    f = one(Runaway().detect(_small() + [_idle()], ctx()), "idle-loop")
    # sixty $0.03 calls over 69 minutes with no human prompt after the first
    assert f.cost_observed.nano == 60 * 30_000_000 - 30_000_000
    assert evidence(f, "tail:idle-loop:1")["idle_requests"] == 60
    assert by_kind(Runaway().detect(_small() + [_idle()], ctx()), "runaway-session") == []
    assert by_kind(Runaway().detect(_small() + [_idle(middle=True)], ctx()), "idle-loop") == []
    no_prompts = ctx(capabilities=CAPS - {"human_prompts"})
    assert by_kind(Runaway().detect(_small() + [_idle()], no_prompts), "idle-loop") == []
    notes = Runaway().detect([], no_prompts)
    assert [dict(n.scope.dims)["kind"] for n in notes] == ["idle-loop"]


def test_unpriced_requests_count_zero_and_are_disclosed() -> None:
    loop = _loop()
    odd = lane("L-odd", [(1_000_000 + 3_000, 0, 0, 0, 1_000, 0)], model="claude-foo-9",
               team="ops", session_key="s_runaway")
    f = one(Runaway().detect(_small() + [loop, odd], ctx()), "runaway-session")
    assert f.cost_observed.nano == 59_970_000_000
    assert "had no priced rate" in f.summary
