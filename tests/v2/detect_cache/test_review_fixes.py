"""Adversarial-review regressions: evidence kinds from the SPEC value set, the §10.1 threshold
overrides, keepalive never replayed on Claude Code lanes, the allowance statement surviving long
summaries and k-anonymous re-scoping, unpriced replays and events disclosed instead of dropped,
and the linear-time ``K_rem`` agreeing with its definition."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import kanon
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, unpriced
from tokenbill.core.records import LaneKind, Request
from tokenbill.core.registry import run_detectors
from tokenbill.core.types import Policy
from tokenbill.detect import cache_miss as cm
from tokenbill.detect.cache_miss import MissByCause, RebuildEvents
from tokenbill.detect.cache_structure import ColdFanout, GatewayDisabled, UnreadWrite
from tokenbill.detect.cache_ttl import ColdResume, TtlAdvisor, keepalive_spec, ttl_spec

from .helpers import (
    ALL_DETECTORS,
    CWD,
    PRICER,
    ctx,
    diag,
    event,
    fleet_saving,
    fn_replayer,
    lane,
    lane_a1,
    lane_a2,
    lane_a5,
    lane_a10,
    mixed_fleet,
    only,
    table_replayer,
)

EVIDENCE_KINDS = {"transition", "event", "block_divergence", "attempt_chain", "aggregate"}
ALLOWANCE_PHRASE = "list-equivalent, not invoice dollars"


# ---------------------------------------------------------------------------------------------
# SPEC value sets
# ---------------------------------------------------------------------------------------------


def test_evidence_kinds_are_the_spec_value_set() -> None:
    """``EvidenceItem.kind`` is one of the five SPEC §3.5 values (per-lane items are
    ``aggregate``), for every kind of every cache detector on the mixed fleet."""
    c = ctx(replayer=fn_replayer(fleet_saving),
            thresholds={"min_usd": "0.01", "policy.ttl.ops": "1h"})
    findings = run_detectors(mixed_fleet(), c, only=list(ALL_DETECTORS))
    kinds = {f.kind for f in findings}
    assert {"no-cache", "oversized-ttl", "tail-writes"} <= kinds
    for f in findings:
        for item in f.evidence:
            assert item.kind in EVIDENCE_KINDS, (f.kind, item.kind)


def test_billing_classes_are_table_driven() -> None:
    """Only ``billed`` is left out of the scope, so an additive class (GitHub Copilot's ``pool``,
    R-E20) never shares a finding id with the billed cohort of the same team and lane kind; the
    list-equivalent classes are a table."""
    billed = cm.Cohort(team="t", lane_kind="main", billing_class="billed", lanes=())
    allowance = cm.Cohort(team="t", lane_kind="main", billing_class="allowance", lanes=())
    pool = cm.Cohort(team="t", lane_kind="main", billing_class="pool", lanes=())
    assert billed.scope_dims()["billing_class"] is None
    assert allowance.scope_dims()["billing_class"] == "allowance"
    assert pool.scope_dims()["billing_class"] == "pool"
    assert billed.basis(PRICER) is Basis.LIST
    assert allowance.basis(PRICER) is Basis.LIST_EQUIVALENT
    assert pool.basis(PRICER) is Basis.LIST_EQUIVALENT
    assert allowance.allowance and not pool.allowance    # "Allowance headroom:" is D26's only
    assert cm.LIST_EQUIVALENT_CLASSES >= {"allowance", "pool"}


# ---------------------------------------------------------------------------------------------
# §10.1: every threshold is overridable by ctx.thresholds["<detector id>.<name>"]
# ---------------------------------------------------------------------------------------------


def test_cold_resume_write_share_override() -> None:
    partial = lane("PA", [(0, 0, 0, 200_000, 0, 500), (7200, 110_000, 0, 92_000, 0, 500)])
    t = {"min_usd": "0"}
    assert ColdResume().detect([partial], ctx(thresholds=t)) == []      # 92k < 0.5 × 200k
    f = only(ColdResume().detect([partial], ctx(thresholds={
        **t, "cache.cold-resume.min_write_share": "0.4"})), "cold-resume")
    assert f.cost_observed.nano == 92_000 * 8_000


def _no_cache_rows(n: int, tokens: int) -> list[tuple[int, int, int, int, int, int]]:
    return [(i * 40, 0, 0, 0, tokens, 100) for i in range(n)]


def test_no_cache_thresholds_override() -> None:
    t = {"min_usd": "0"}
    four = lane("N4", _no_cache_rows(4, 20_000), team="platform")
    assert GatewayDisabled().detect([four], ctx(thresholds=t)) == []
    only(GatewayDisabled().detect([four], ctx(thresholds={
        **t, "cache.gateway-disabled.min_requests": "4"})), "no-cache")
    small = lane("SM", _no_cache_rows(6, 4_000), team="platform")
    assert GatewayDisabled().detect([small], ctx(thresholds=t)) == []
    only(GatewayDisabled().detect([small], ctx(thresholds={
        **t, "cache.gateway-disabled.min_prompt_tokens": "4000"})), "no-cache")
    # the pricer's minimum cacheable prompt still applies below the override
    tiny = lane("TY", _no_cache_rows(6, 400), team="platform")
    assert GatewayDisabled().detect([tiny], ctx(thresholds={
        **t, "cache.gateway-disabled.min_prompt_tokens": "0"})) == []


def test_unread_write_read_share_override() -> None:
    t = {"min_usd": "0"}
    full = lane("WR", [(0, 0, 50_000, 0, 0, 200), (30, 47_500, 3_000, 0, 0, 200)])
    assert UnreadWrite().detect([full], ctx(thresholds=t)) == []        # 95% read
    f = only(UnreadWrite().detect([full], ctx(thresholds={
        **t, "cache.unread-write.read_share": "0.99"})), "write-never-read")
    assert f.cost_observed.nano == 2_500 * 1_000                         # w5 − u on 2,500


def _fanout(offsets: tuple[int, ...], w: int = 20_000, r: int = 0) -> list[Any]:
    return [lane(f"FO{i}", [(off, r, w, 0, 0, 300)], kind=LaneKind.SUBAGENT, cwd_key=CWD)
            for i, off in enumerate(offsets)]


def test_cold_fanout_thresholds_override() -> None:
    t = {"min_usd": "0"}
    spread = _fanout((0, 15))
    assert ColdFanout().detect(spread, ctx(thresholds=t)) == []
    f = only(ColdFanout().detect(spread, ctx(thresholds={
        **t, "cache.cold-fanout.window_s": "20"})), "cold-fanout")
    assert "within 20 s" in f.summary
    tiny = _fanout((0, 1), w=1_000)
    assert ColdFanout().detect(tiny, ctx(thresholds=t)) == []
    only(ColdFanout().detect(tiny, ctx(thresholds={
        **t, "cache.cold-fanout.min_write_tokens": "1000"})), "cold-fanout")
    warm = _fanout((0, 1), w=2_000, r=15_000)                              # R ≥ 0.5·T
    assert ColdFanout().detect(warm, ctx(thresholds=t)) == []
    only(ColdFanout().detect(warm, ctx(thresholds={
        **t, "cache.cold-fanout.max_read_share": "0.9"})), "cold-fanout")


@pytest.mark.parametrize(("detector", "key", "value"), [
    (GatewayDisabled, "cache.gateway-disabled.min_requests", "-1"),
    (ColdResume, "cache.cold-resume.min_write_share", "1.5"),
    (UnreadWrite, "cache.unread-write.read_share", "-0.1"),
    (ColdFanout, "cache.cold-fanout.max_read_share", "2"),
    (TtlAdvisor, "cache.ttl-advisor.spend_share", "1.01"),
    (ColdFanout, "cache.cold-fanout.window_s", "soon"),
])
def test_invalid_threshold_overrides_raise_usage_error(detector: Any, key: str,
                                                        value: str) -> None:
    lanes = [lane_a1(), *_fanout((0, 2)), lane("NC", _no_cache_rows(6, 20_000))]
    c = ctx(replayer=table_replayer({("A1", ttl_spec("main", "1h")): 2_000_000_000}),
            thresholds={key: value})
    with pytest.raises(UsageError):
        detector().detect(lanes, c)


# ---------------------------------------------------------------------------------------------
# keepalive: Claude Code lanes never enter the replay
# ---------------------------------------------------------------------------------------------


def test_keepalive_replay_excludes_claude_code_lanes_of_a_mixed_api_run_cohort() -> None:
    """An API_RUN cohort with a Claude Code lane and an SDK lane: the keepalive saving is the
    SDK lane's alone, even when the replayer (wrongly) prices a ping saving for Claude Code."""
    keepalive_lanes: list[str] = []

    def fn(lane_: Any, policy: Policy) -> int:
        if policy.keepalive is not None:
            keepalive_lanes.append(lane_.lane_key)
            return 9_000_000_000 if lane_.lane_key == "CC" else 1_407_600_000
        return 0

    lanes = [lane_a1("CC", kind=LaneKind.API_RUN, product="claude_code", team="agents"),
             lane_a1("SD", kind=LaneKind.API_RUN, product="agent_sdk", team="agents")]
    f = only(TtlAdvisor().detect(lanes, ctx(replayer=fn_replayer(fn))), "keepalive-recommended")
    assert keepalive_lanes and set(keepalive_lanes) == {"SD"}
    assert f.recoverable is not None and f.recoverable.nano == 1_407_600_000
    assert keepalive_spec("api_run") == "keepalive=240s,max=3600s@lane_kind:api_run"


# ---------------------------------------------------------------------------------------------
# the allowance statement (D26) survives long summaries and re-scoping
# ---------------------------------------------------------------------------------------------


def _long_team_allowance_lanes(team: str) -> list[Any]:
    kw = {"team": team, "billing_path": "subscription"}
    lanes = [lane_a1(f"P{i}", principal=f"r_dev{i}", **kw) for i in range(5)]
    lanes += [lane_a5("C5", principal="r_dev0", **kw), lane_a2("B2", hour=True, **kw),
              lane_a10("E10", principal="r_dev1", **kw),
              lane("NC", _no_cache_rows(6, 20_000), principal="r_dev2", **kw),
              lane("OS", [(0, 0, 60_000, 0, 10, 200)], kind=LaneKind.API_RUN, product="api",
                   principal="r_dev3", **kw)]
    lanes += [lane(f"FO{i}", [(off, 0, 30_000, 0, 0, 300)], kind=LaneKind.SUBAGENT,
                   principal="r_dev4", **kw) for i, off in enumerate((0, 2, 4))]
    return lanes


def test_allowance_statement_survives_long_summaries_and_rescoping() -> None:
    team = "t" * 40
    spec = ttl_spec("main", "1h")
    # 2 of 5 principals gain: the longest summary (ttl-heterogeneous)

    def fn(lane_: Any, policy: Policy) -> int:
        if policy.spec() == spec:
            return 3_000_000_000 if lane_.lane_key in ("P0", "P1") else -100_000_000
        return fleet_saving(lane_, policy)

    c = ctx(replayer=fn_replayer(fn), thresholds={"min_usd": "0.01"})
    findings = run_detectors(_long_team_allowance_lanes(team), c, only=list(ALL_DETECTORS))
    kinds = {f.kind for f in findings}
    assert {"ttl-heterogeneous", "cold-resume", "edit-churn", "no-cache", "tail-writes",
            "cold-fanout", "ttl-expiry"} <= kinds
    for f in findings:
        assert ("billing_class", "allowance") in f.scope.dims
        assert len(f.summary) <= cm.SUMMARY_BUDGET
        assert f.summary.endswith(ALLOWANCE_PHRASE + "."), f.summary
    # below k at (team, lane kind), enough at team level: every finding is re-scoped, merged and
    # prefixed by core.kanon, and the allowance statement is still there
    published = kanon.rescope_findings(
        findings, k=50,
        count_users=lambda scope: 1 if "lane_kind" in dict(scope.dims) else 100)
    assert published
    for f in published:
        assert f.summary.startswith("[re-scoped for k-anonymity")
        assert ALLOWANCE_PHRASE in f.summary, f.summary


def test_summary_budget_keeps_the_note_and_the_allowance_tail() -> None:
    cohort = cm.Cohort(team="x", lane_kind="main", billing_class="allowance", lanes=())
    text = cohort.summary("y" * 1_000, " note.")
    assert len(text) == cm.SUMMARY_BUDGET
    assert text.endswith("… note." + cm.ALLOWANCE_SUMMARY)
    billed = cm.Cohort(team="x", lane_kind="main", billing_class="billed", lanes=())
    assert billed.summary("short") == "short"


# ---------------------------------------------------------------------------------------------
# unknown is never zero, and never silently dropped
# ---------------------------------------------------------------------------------------------


class UnpricedSaving:
    """Wraps a replayer and makes every saving unpriced (a changed request without a rate row,
    REPLAY's R2)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def replay(self, *args: Any, **kwargs: Any) -> Any:
        res = self.inner.replay(*args, **kwargs)
        if res.policy.is_observed():
            return res
        return dataclasses.replace(
            res, saving=unpriced("a request the policy changes has no priced rate row",
                                 res.saving.basis))


def test_unpriced_replay_saving_keeps_the_finding() -> None:
    """A cohort with a large exact cost whose repair replay is unpriced keeps its finding, with
    the recoverable shown as unpriced (gated on cost_observed), and publishes through kanon."""
    no_cache = lane("NC", _no_cache_rows(6, 20_000), team="platform")
    inner = table_replayer({("NC", "repair=restore_caching"): 420_000_000})
    f = only(GatewayDisabled().detect([no_cache], ctx(replayer=UnpricedSaving(inner),
                                                      thresholds={"min_usd": "0.10"})),
             "no-cache")
    assert f.cost_observed.nano == 6 * 20_000 * 4_000
    assert f.recoverable is not None and f.recoverable.nano is None
    assert f.recoverable.note.startswith("unpriced:")
    published = kanon.rescope_findings([f], k=5, count_users=lambda scope: 10)
    assert published and published[0].recoverable is not None
    assert published[0].recoverable.nano is None
    # below min_usd on cost_observed: still gated
    assert GatewayDisabled().detect([no_cache], ctx(replayer=UnpricedSaving(inner),
                                                    thresholds={"min_usd": "1.00"})) == []


def test_unpriced_events_are_disclosed_in_the_summary() -> None:
    priced = lane_a1("PR")
    unknown = lane_a1("UN", model="claude-sonnet-5-5")          # announced, unpriced
    f = only(MissByCause().detect([priced, unknown], ctx()), "ttl-expiry")
    assert f.n_events == 6 and f.n_lanes == 2
    assert f.cost_observed.nano == (100_000 + 102_000 + 104_000) * 5_000
    assert "3 of the 6 events had no priced rate" in f.summary
    agg = dict(next(e for e in f.evidence if e.kind == "aggregate").attrs)
    assert agg["unpriced_events"] == 3
    clean = only(MissByCause().detect([priced], ctx()), "ttl-expiry")
    assert "no priced rate" not in clean.summary


# ---------------------------------------------------------------------------------------------
# K_rem: one backward pass equals the definition
# ---------------------------------------------------------------------------------------------


def _k_rem_reference(lane_: Any, steps: list[Request], i: int) -> int:
    """The definition: later requests before the next reset event after step i or the next
    request carrying edits or dropped thinking blocks."""
    ts = steps[i].ts_start_ms
    resets = sorted(ev.ts_ms for ev in lane_.events if ev.kind in cm.RESET_EVENTS)
    count = 0
    for later in steps[i + 1:]:
        if any(att.applied_edits or att.thinking_dropped for att in later.attempts):
            break
        if any(ts < r <= later.ts_start_ms for r in resets):
            break
        count += 1
    return count


@settings(max_examples=80, deadline=None)
@given(gaps=st.lists(st.integers(0, 400), min_size=1, max_size=12),
       edits=st.sets(st.integers(0, 11), max_size=4),
       resets=st.lists(st.tuples(st.integers(0, 5_000),
                                 st.sampled_from(("compaction", "clear", "context_edit",
                                                  "human_prompt"))), max_size=5))
def test_remaining_counts_match_the_definition(gaps: list[int], edits: set[int],
                                               resets: list[tuple[int, str]]) -> None:
    rows = []
    ts = 0
    for i, gap in enumerate([0, *gaps]):
        ts += gap
        rows.append((ts, 0, 10_000 + i, 0, 0, 5))
    per = {i: {"applied_edits": (("clear", 1_000),)} for i in edits if 0 < i < len(rows)}
    evs = []
    for at, kind in resets:
        attrs: dict[str, Any] = {}
        if kind == "compaction":
            attrs = {"trigger": "auto", "pre_tokens": 1, "post_tokens": 1, "duration_ms": 1,
                     "dropped_tokens": None}
        elif kind == "context_edit":
            attrs = {"edit_type": "clear", "cleared_input_tokens": 5}
        evs.append(event("KR", at, kind, **attrs))
    lane_ = lane("KR", rows, events=evs, per_request=per)
    steps = cm.serving_steps(lane_)
    got = cm._remaining_counts(cm.EventIndex(lane_), steps)
    assert got == [_k_rem_reference(lane_, steps, i) for i in range(len(steps))]


def test_edit_churn_is_linear_on_a_long_lane() -> None:
    """4,000 requests, an edit every 10 and a reset event every 50: K_rem per edit is found
    without rescanning the lane (the definition's result, not a timing assertion)."""
    rows = [(0, 0, 20_000, 0, 0, 100)]
    prefix = 20_000
    per: dict[int, dict[str, Any]] = {}
    evs = []
    for i in range(1, 4_000):
        rows.append((i * 20, prefix, 500, 0, 0, 100))
        prefix += 500
        if i % 10 == 5:
            per[i] = {"applied_edits": (("clear_tool_uses_20250919", 5_000),)}
        if i % 50 == 0:
            evs.append(event("LONG", i * 20 - 1, "clear"))
    lane_ = lane("LONG", rows, kind=LaneKind.API_RUN, product="agent_sdk", events=evs,
                 per_request=per)
    f = only(RebuildEvents().detect([lane_], ctx(thresholds={"min_usd": "0"})), "edit-churn")
    assert f.n_events == 400
    dist = dict(next(e for e in f.evidence if e.ref == "edit-churn:distribution").attrs)
    # edits at …5 and …15 … …45 of every 50: the next break is the next edit (10 later → 9
    # requests) except the edit at …45, whose next break is the clear before …50 (4 requests)
    assert dist["k_rem_p10"] == 4 and dist["k_rem_p90"] == 9


# ---------------------------------------------------------------------------------------------
# a team name is never cut in half (core.kanon scrubs whole tokens only)
# ---------------------------------------------------------------------------------------------


def _long_titles_fleet(team: str) -> list[Any]:
    kw: dict[str, Any] = {"team": team, "billing_path": "subscription",
                          "kind": LaneKind.WORKFLOW_AGENT}
    lanes = [lane_a1(f"P{i}", principal=f"r_dev{i}", **kw) for i in range(5)]
    lanes.append(lane_a2("B2", hour=True, **kw))
    compaction = [event("CPX", 20, "compaction", trigger="auto", pre_tokens=100_000,
                        post_tokens=25_000, duration_ms=5_000, dropped_tokens=None)]
    lanes.append(lane("CPX", [(0, 0, 100_000, 0, 0, 500), (30, 0, 30_000, 0, 0, 500)],
                      events=compaction, **kw))
    rows = [(0, 0, 30_000, 0, 0, 200)]
    over: dict[int, dict[str, Any]] = {}
    prefix = 30_000
    for i in range(1, 30):
        if i in (5, 15, 25):
            rows.append((i * 30, 0, prefix + 1_000, 0, 0, 200))
            over[i] = {"diagnostics": diag("tools_changed")}
        else:
            rows.append((i * 30, prefix, 1_000, 0, 0, 200))
        prefix = rows[-1][1] + rows[-1][2]
    lanes.append(lane("GW", rows, gateway="corp-gateway", per_request=over, **kw))
    return lanes


@pytest.mark.parametrize("team", ["abcdefghij" * 4, "unattributed-" + "x" * 27])
def test_titles_and_summaries_never_cut_a_team_name(team: str) -> None:
    spec = ttl_spec("workflow_agent", "1h")

    def fn(lane_: Any, policy: Policy) -> int:
        if policy.spec() == spec:
            return 3_000_000_000 if lane_.lane_key in ("P0", "P1") else -100_000_000
        return fleet_saving(lane_, policy)

    c = ctx(replayer=fn_replayer(fn), thresholds={"min_usd": "0"})
    findings = run_detectors(_long_titles_fleet(team), c, only=list(ALL_DETECTORS))
    kinds = {f.kind for f in findings}
    assert {"compaction", "tool-search-disabled", "oversized-ttl", "ttl-heterogeneous",
            "cold-fanout"} <= kinds, kinds
    fragment = team[:10]
    for f in findings:
        assert len(f.title) <= 120 and len(f.summary) <= cm.SUMMARY_BUDGET
        for text in (f.title, f.summary):
            assert team in text or fragment not in text, text
        assert f.title.startswith("Allowance headroom: ")
