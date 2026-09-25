"""``subagent-share`` (SUBAGENT lane spend by ``agent_type``) and ``ci-uncapped`` (Copilot CI
lanes: CLI sessions without ``credit_limit_nano``; gh-aw runs against the 1,000 AIC default cap)."""

from __future__ import annotations

import json

from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind, to_json
from tokenbill.core.registry import run_detectors
from tokenbill.detect.copilot_lanes import default_run_cap_nano

from .helpers import (
    CLI,
    CREDIT,
    EVENTS_ONLY_CAPS,
    GH_AW,
    OPUS48,
    SONNET5,
    attr,
    attrs,
    ctx,
    event,
    lane,
    one,
    only,
    req,
    run,
)
from .test_compaction import events_only_cli_lane

# per request: Opus 4.8 u=100,000 o=2,000 → 550,000,000; Sonnet 5 → 220,000,000
OPUS_REQ = dict(u=100_000, o=2_000, model=OPUS48)
SONNET_REQ = dict(u=100_000, o=2_000, model=SONNET5)


# ---- subagent-share --------------------------------------------------------------------------


def _subagent_lanes() -> list:
    explore = attr(agent_type="Explore")
    general = attr(agent_type="general-purpose")
    custom = attr(agent_type=f"my helper {CANARY}")
    return [
        lane("sa-1", [req("sa-1", 0, 0, a=explore, **OPUS_REQ),
                      req("sa-1", 1, 60, a=explore, **OPUS_REQ)], kind=LaneKind.SUBAGENT),
        lane("sa-2", [req("sa-2", 0, 0, a=general, **SONNET_REQ)], kind=LaneKind.SUBAGENT),
        lane("sa-3", [req("sa-3", 0, 0, a=custom, u=10_000, model=SONNET5)],
             kind=LaneKind.SUBAGENT),
        lane("sa-4", [req("sa-4", 0, 0, u=10_000, model=SONNET5)], kind=LaneKind.SUBAGENT,
             events=[event("sa-4", 0, "session_meta", agent_type="workflow-subagent")]),
        lane("main-1", [req("main-1", 0, 0, **OPUS_REQ)]),
    ]


def test_subagent_share_by_agent_type() -> None:
    found = run(_subagent_lanes())
    f = one(found, "subagent-share")
    assert dict(f.scope.dims)["lane_kind"] == "subagent"
    total = 1_100_000_000 + 220_000_000 + 20_000_000 + 20_000_000
    assert f.cost_observed.nano == total and f.cost_observed.evidence is Evidence.EXACT
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT and f.recoverable is None
    assert attrs(f, "agent_type:Explore") == {"nano": 1_100_000_000, "share_pct": "80.9",
                                              "lanes": 1, "requests": 2}
    assert attrs(f, "agent_type:general-purpose")["share_pct"] == "16.2"
    assert attrs(f, "agent_type:custom")["nano"] == 20_000_000
    assert attrs(f, "agent_type:workflow-subagent")["nano"] == 20_000_000
    assert f.category == "aggregate" and f.lever_ids == () and f.lever_class == "none"
    assert "Explore carries 80.9%" in f.summary
    assert (f.n_events, f.n_lanes) == (5, 4)
    assert f.fix is not None and "/subagents" in f.fix.text
    # the MAIN lane is its own cohort: no subagent-share there
    assert only(found, "subagent-share", lane_kind="main") == []


def test_free_text_agent_types_never_reach_a_finding() -> None:
    for f in run(_subagent_lanes(), ctx(min_usd="0")):
        assert_no_canary(json.dumps(to_json(f)), f.title, f.summary)


def test_subagent_share_below_min_usd_and_unknown_type() -> None:
    small = lane("sa-9", [req("sa-9", 0, 0, u=1_000, model=SONNET5)], kind=LaneKind.SUBAGENT)
    assert only(run([small]), "subagent-share") == []
    f = one(run([small], ctx(min_usd="0")), "subagent-share")
    assert attrs(f, "agent_type:unknown")["share_pct"] == "100.0"


# ---- ci-uncapped: CLI ------------------------------------------------------------------------


def _cli_ci(key: str, n: int, *, limit_credits: int | None = None, team: str = "agents") -> object:
    a = attr(team=team, product=CLI, workload="ci", billing_path="copilot_direct")
    events = []
    if limit_credits is not None:
        events.append(event(key, 0, "session_meta", credit_limit_nano=limit_credits * CREDIT))
    return lane(key, [req(key, i, 60 * i, a=a, **SONNET_REQ) for i in range(n)], events=events)


def test_cli_ci_sessions_without_credit_limit_counted() -> None:
    lanes = [_cli_ci("ci-1", 1), _cli_ci("ci-2", 3), _cli_ci("ci-3", 2, limit_credits=50)]
    f = one(run(lanes, ctx(min_usd="0.10")), "ci-uncapped")
    assert dict(f.scope.dims) == {"team": "agents", "lane_kind": "main", "billing_class": "pool",
                                  "product": "copilot", "workload_class": "ci",
                                  "agent_product": CLI}
    assert f.cost_observed.nano == 880_000_000 and f.cost_observed.evidence is Evidence.EXACT
    sessions = attrs(f, "ci:sessions")
    assert (sessions["sessions"], sessions["uncapped"], sessions["uncapped_share_pct"]) == \
        (3, 2, "66.7")
    assert (sessions["p50_nano"], sessions["p90_nano"]) == (440_000_000, 660_000_000)
    cap = attrs(f, "ci:cap")
    assert (cap["cap_source"], cap["p99_nano"], cap["over_cap"],
            cap["suggested_max_ai_credits"]) == ("session_limit", 660_000_000, 0, 99)
    assert f.lever_ids == ("copilot.session_limits",) and f.lever_class == "behavioral"
    assert f.category == "aggregate" and f.recoverable is None and f.n_events == 2
    assert "--max-ai-credits" in f.fix.text


def test_capped_cli_sessions_give_no_finding_and_over_cap_counted() -> None:
    lanes = [_cli_ci("ci-1", 1, limit_credits=10), _cli_ci("ci-2", 3, limit_credits=10)]
    assert only(run(lanes, ctx(min_usd="0")), "ci-uncapped") == []
    lanes.append(_cli_ci("ci-3", 1))
    f = one(run(lanes, ctx(min_usd="0")), "ci-uncapped")
    assert attrs(f, "ci:cap")["over_cap"] == 2          # 220M and 660M ≥ 10 credits (100M)


def test_interactive_lanes_are_not_ci() -> None:
    a = attr(team="agents", product=CLI)
    ln = lane("cli-int", [req("cli-int", 0, 0, a=a, **SONNET_REQ)])
    assert only(run([ln], ctx(min_usd="0")), "ci-uncapped") == []


# ---- ci-uncapped: gh-aw ----------------------------------------------------------------------


def _gh_aw(key: str, *, u: int = 100_000, o: int = 2_000, model: str = SONNET5,
           limit_credits: int | None = None) -> object:
    a = attr(team="ci", principal=None, product=GH_AW, workload="ci",
             billing_path="copilot_direct")
    events = []
    if limit_credits is not None:
        events.append(event(key, 0, "session_meta", credit_limit_nano=limit_credits * CREDIT))
    return lane(key, [req(key, 0, 0, a=a, u=u, o=o, model=model)], events=events)


def test_gh_aw_runs_compared_with_the_1000_aic_default_cap() -> None:
    assert default_run_cap_nano() == 1_000 * CREDIT == 10_000_000_000
    lanes = [_gh_aw("aw-1"), _gh_aw("aw-2", u=200_000, o=4_000),
             _gh_aw("aw-3", u=300_000, o=6_000),
             _gh_aw("aw-4", u=2_000_000, o=80_000, model=OPUS48),       # 12,000,000,000
             _gh_aw("aw-5", limit_credits=200)]
    f = one(run(lanes), "ci-uncapped")
    assert dict(f.scope.dims)["agent_product"] == GH_AW
    assert f.cost_observed.nano == 220_000_000 + 440_000_000 + 660_000_000 + 12_000_000_000
    cap = attrs(f, "ci:cap")
    assert (cap["cap_nano"], cap["cap_credits"], cap["cap_source"]) == \
        (10_000_000_000, "1000", "default")
    assert (cap["p99_nano"], cap["over_cap"], cap["suggested_max_ai_credits"]) == \
        (12_000_000_000, 1, 1800)
    sessions = attrs(f, "ci:sessions")
    assert (sessions["sessions"], sessions["uncapped"]) == (5, 4)
    assert f.lever_ids == ("copilot.agentic_workflow_caps",)
    assert "default cap of 1000 AI credits" in f.summary and "p99 of 1200" in f.summary
    assert f.n_users == 0       # gh-aw runs carry no principal


def test_events_only_cli_and_gh_aw_lanes_without_usage_sequence() -> None:
    """Acceptance: capabilities without ``usage_sequence`` (events-only CLI, gh-aw) still yield
    ``compaction-cost`` and ``ci-uncapped`` through the registry."""
    cli = events_only_cli_lane("cli-ci", workload="ci")
    lanes = [cli, _gh_aw("aw-1"), _gh_aw("aw-2", u=200_000)]
    c = ctx(min_usd="0.10", caps=EVENTS_ONLY_CAPS)
    found = run_detectors(lanes, c, only=["copilot.lanes"], aggregates_only=False)
    kinds = {(f.kind, dict(f.scope.dims).get("agent_product")) for f in found}
    assert ("compaction-cost", None) in kinds
    assert ("ci-uncapped", CLI) in kinds and ("ci-uncapped", GH_AW) in kinds
    assert "usage_sequence" not in c.capabilities
