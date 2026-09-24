"""SPEC §11.2 candidates, billing classes, grid choice, guards, headline and headroom (D26, A-7)."""

from __future__ import annotations

import dataclasses

import pytest

from tokenbill.core.labels import Basis, Calibration, Evidence, Figure
from tokenbill.core.records import LaneKind
from tokenbill.core.shards import plan_shards
from tokenbill.core.testing import FakeReplayer
from tokenbill.core.types import CalibrationReport, Fix, Policy
from tokenbill.plan.action_plan import build_action_plan, touches
from tokenbill.plan.realization import crosses_zero

from .helpers import (
    MAIN_SEL,
    Loader,
    a7_findings,
    a7_replayer,
    ctx,
    est,
    finding,
    index_of,
    lane,
)


def _plan(lanes, findings, replayer, *, context=None, **kw):
    index = index_of(lanes)
    return build_action_plan(findings, index, Loader(lanes), plan_shards(index),
                             context or ctx(replayer), window_days=kw.pop("window_days", 30),
                             **kw)


def _get(plan, lever_id, basis=None):
    got = [r for r in plan.levers if r.lever_id == lever_id
           and (basis is None or r.basis is basis)]
    assert len(got) == 1, [r.lever_id for r in plan.levers]
    return got[0]


def _mixed(pol: Policy, ln) -> int:
    """TTL 1h on main lanes saves 1,000; fast=off 400; effort=medium 900 and effort=high 300 on
    every lane; 5m saves nothing."""
    total = 0
    if any(ttl == "1h" and touches((sel,), ln) for sel, ttl in pol.ttl):
        total += 1_000
    if pol.fast_off:
        total += 400
    for _sel, level, _scale in pol.effort:
        total += {"medium": 900, "high": 300}.get(level, 0)
    return total


MIXED = FakeReplayer.from_function(lambda ln, pol: _mixed(pol, ln))


def test_grid_choice_takes_the_largest_saving_and_drops_levers_without_one() -> None:
    plan = _plan([lane("m1")], [], MIXED)
    ttl = _get(plan, "cc.prompt_cache_ttl.main")
    assert ttl.params == f"ttl=1h@{MAIN_SEL}"
    assert ttl.lever_class == "cache_transform" and ttl.shapley.nano == 1_000
    ids = {r.lever_id for r in plan.levers}
    assert "geo.global" not in ids and "sdk.ttl" not in ids   # defaults without a saving
    assert "cc.max_effort" not in ids                         # not linked, not a default


def test_tradeoff_levers_are_shown_but_excluded_unless_requested() -> None:
    findings = [finding("effort-mix", ["cc.max_effort"], lever_class="trajectory")]
    off = _plan([lane("m1")], findings, MIXED)
    max_effort = _get(off, "cc.max_effort")
    assert max_effort.group == "trade-off" and max_effort.params == "effort=medium,scale=0.5"
    assert max_effort.shapley.nano is None and "trade-off" in max_effort.shapley.note
    assert max_effort.standalone.nano == 900
    assert max_effort.projected_monthly.nano == 450 and crosses_zero(max_effort.projected_monthly)
    assert off.joint_saving.nano == 1_400
    assert off.headline_monthly.nano == 900 + 400          # TTL 1000 × 0.9 + fast 400 × 1.0
    assert all(g != "trade-off" for g, _ in off.groups)

    on = _plan([lane("m1")], findings, MIXED, include_tradeoffs=True)
    max_effort = _get(on, "cc.max_effort")
    assert max_effort.group.startswith("billed:g") and max_effort.shapley.nano == 900
    proj = max_effort.projected_monthly
    assert (proj.nano, proj.low_nano, proj.high_nano) == (450, -180, 900)
    assert "range crosses zero" in proj.note and "design judgment" in proj.note
    assert on.joint_saving.nano == 2_300
    head = on.headline_monthly
    assert head.nano == 900 + 400 + 450
    assert (head.low_nano, head.high_nano) == (800 + 400 - 180, 1_000 + 400 + 900)
    assert head.evidence is Evidence.ESTIMATED and head.upper_bound


def test_default_model_and_effort_are_tradeoffs() -> None:
    def fn(ln, pol: Policy) -> int:
        return 700 if pol.model_remap else (500 if pol.effort else 0)

    findings = [finding("default-model", ["cc.default_model"], lever_class="trajectory"),
                finding("default-effort", ["cc.default_effort"], lever_class="trajectory")]
    plan = _plan([lane("m1")], findings, FakeReplayer.from_function(fn))
    for lid in ("cc.default_model", "cc.default_effort"):
        assert _get(plan, lid).group == "trade-off"
    assert plan.headline_monthly.nano == 0
    assert _get(plan, "cc.default_model").params == f"model=claude-sonnet-5@{MAIN_SEL}"
    assert _get(plan, "cc.default_effort").params == f"effort=medium,scale=0.5@{MAIN_SEL}"
    plan = _plan([lane("m1")], findings, FakeReplayer.from_function(fn), include_tradeoffs=True)
    # interacting: v(model) 700, v(effort) 500, v(both) 700 → credits 450 / 250, Σ = 700
    assert _get(plan, "cc.default_model").shapley.nano == 450
    assert _get(plan, "cc.default_effort").shapley.nano == 250
    assert plan.joint_saving.nano == 700
    assert plan.headline_monthly.nano == (450 + 250) // 2


def test_behavioral_and_ceiling_only_levers_never_enter_the_headline() -> None:
    def fn(ln, pol: Policy) -> int:
        return 5_000 if pol.cold_resume else (1_000 if pol.fast_off else 0)

    findings = [finding("cold-resume", ["cc.cold_resume_hook", "cc.compact_on_resume"],
                        detector="cache.cold-resume", lever_class="behavioral")]
    for include in (False, True):
        plan = _plan([lane("m1")], findings, FakeReplayer.from_function(fn),
                     include_tradeoffs=include)
        hook = _get(plan, "cc.cold_resume_hook")
        assert hook.group == "behavioral" and hook.projected_monthly.nano is None
        assert "never projected" in hook.projected_monthly.note
        assert hook.finding_ids == (findings[0].finding_id,)
        ceiling = _get(plan, "cc.compact_on_resume")
        assert ceiling.group == "ceiling-only" and ceiling.standalone.nano == 5_000
        assert ceiling.projected_monthly.nano is None and ceiling.shapley.nano is None
        assert plan.headline_monthly.nano == 1_000 and plan.joint_saving.nano == 1_000


def test_allowance_levers_go_only_to_the_allowance_headroom() -> None:
    lanes = [lane("b1"), lane("s1", billing_path="subscription"),
             lane("s2", billing_path="subscription")]
    plan = _plan(lanes, [], MIXED)
    billed = _get(plan, "cc.prompt_cache_ttl.main", Basis.LIST)
    allowance = _get(plan, "cc.prompt_cache_ttl.main", Basis.LIST_EQUIVALENT)
    assert billed.shapley.nano == 1_000 and allowance.shapley.nano == 2_000
    assert allowance.group.startswith("allowance:")
    assert plan.headline_monthly.nano == 900 + 400
    assert plan.headline_monthly.basis is Basis.LIST
    head = plan.allowance_headroom_monthly
    assert head is not None and head.basis is Basis.LIST_EQUIVALENT
    assert head.nano == 1_800 + 800
    assert plan.joint_saving.nano == 1_400                 # billed set only
    assert plan.pool_headroom_monthly is None
    only_allowance = _plan(lanes[1:], [], MIXED)
    assert only_allowance.headline_monthly.nano == 0
    assert only_allowance.allowance_headroom_monthly.nano == 2_600


def test_pool_lanes_go_to_the_pool_headroom_and_aggregate_levers_are_skipped() -> None:
    lanes = [lane("c1", product="copilot_cli", billing_path="copilot_pool")]
    findings = [finding("idle-seat", ["copilot.seat_reclaim"], billing_class="pool",
                        basis=Basis.LIST_EQUIVALENT),
                finding("x", ["no.such.lever"])]
    plan = _plan(lanes, findings, MIXED)
    assert all(r.lever_id != "copilot.seat_reclaim" for r in plan.levers)
    fast = _get(plan, "cc.fast_mode_opt_in")
    assert fast.basis is Basis.LIST_EQUIVALENT and fast.group.startswith("pool:")
    assert plan.pool_headroom_monthly is not None and plan.pool_headroom_monthly.nano == 400
    assert plan.pool_headroom_monthly.basis is Basis.LIST_EQUIVALENT
    assert plan.headline_monthly.nano == 0 and plan.allowance_headroom_monthly is None


def test_projection_only_levers_come_from_their_findings() -> None:
    tool = finding("tool-defs-bloat", ["cc.tool_search"], detector="context.static-prefix",
                   lever_class="cache_transform",
                   recoverable=est(1_000, low=500, high=1_700, upper_bound=True))
    breaker = finding("breakpoint-placement", ["blocks.breakpoints"], detector="block.breakers",
                      recoverable=est(300))
    plan = _plan([lane("m1")], [tool, breaker], MIXED)
    lever = _get(plan, "cc.tool_search")
    assert lever.group == "projection-only" and lever.shapley.nano is None
    assert lever.standalone.nano == 1_000 and lever.upper_bound
    assert (lever.projected_monthly.nano, lever.projected_monthly.low_nano,
            lever.projected_monthly.high_nano) == (900, 400, 1_700)
    assert "outside the Shapley plan" in lever.projected_monthly.note
    assert _get(plan, "blocks.breakpoints").group == "projection-only"
    assert plan.headline_monthly.nano == 900 + 400          # projections never added
    no_fig = finding("tool-defs-bloat", ["sdk.defer_loading"], detector="context.static-prefix")
    plan = _plan([lane("m1")], [no_fig], MIXED)
    assert all(r.lever_id != "sdk.defer_loading" for r in plan.levers)


def test_observed_realization_rates_are_shown_after_three_receipts() -> None:
    plan = _plan([lane("m1")], [], MIXED,
                 observed_rr={"cache_transform": ("0.72", 3), "rate": ("1.0", 2)})
    assert plan.observed_rr == (("cache_transform", "0.72", 3),)
    ttl = _get(plan, "cc.prompt_cache_ttl.main")
    assert ttl.projected_monthly.nano == 900   # priors are never replaced by observed rates


class _Spy(FakeReplayer):
    def __init__(self, fn):
        super().__init__(fn=fn)
        self.modes: list[str] = []

    def replay(self, lanes, policy, **kw):
        self.modes.append(kw["mode"])
        return super().replay(lanes, policy, **kw)


def _report(status: str) -> CalibrationReport:
    return CalibrationReport(
        granularity="day", n_periods=12, status=status, mode_used="calibrated", nmbe_pct="1",
        cvrmse_pct="2", nmbe_pct_calibrated="1", cvrmse_pct_calibrated="2",
        thresholds=("10", "30"), rho=(), diag_confusion=(), diag_precision_recall=(),
        unlabeled=0, no_comparison_labels=0, ttl_corroboration=(0, 0), notes=())


def test_calibrated_mode_when_the_model_gate_passed() -> None:
    spy = _Spy(lambda ln, pol: _mixed(pol, ln))
    plan = _plan([lane("m1")], [], spy, context=ctx(spy, calibration=_report("pass")))
    assert "documented" in spy.modes and "calibrated" in spy.modes   # grid on documented
    ttl = _get(plan, "cc.prompt_cache_ttl.main")
    assert ttl.shapley.calibration is Calibration.CALIBRATED
    assert plan.headline_monthly.calibration is Calibration.CALIBRATED
    spy = _Spy(lambda ln, pol: _mixed(pol, ln))
    plan = _plan([lane("m1")], [], spy, context=ctx(spy, calibration=_report("fail")))
    assert set(spy.modes) == {"documented"}
    assert plan.headline_monthly.calibration is Calibration.UNCALIBRATED


class _Compaction(FakeReplayer):
    """Saves 1,000 at 200k, 800 at 300k, 500 at 400k; 300k adds 9 compactions."""

    def __init__(self):
        super().__init__(fn=self._fn)
        self.windows: list[tuple[int, int | None]] = []

    @staticmethod
    def _fn(ln, pol: Policy) -> int:
        if pol.compaction_window is None:
            return 0
        return {200_000: 1_000, 300_000: 800, 400_000: 500}.get(pol.compaction_window[0], 0)

    def replay(self, lanes, policy, **kw):
        result = super().replay(lanes, policy, **kw)
        if policy.compaction_window is not None:
            self.windows.append(policy.compaction_window)
            if policy.compaction_window[0] == 300_000:
                result = dataclasses.replace(result, added_calls=9)
        return result


def test_compaction_guards_and_the_org_median_summary() -> None:
    rep = _Compaction()
    plan = _plan([lane("m1")], [], rep, include_tradeoffs=True)
    cw = _get(plan, "cc.autocompact_window")
    assert cw.params == "compact-window=400000"      # 200k below the minimum, 300k too many
    assert cw.shapley.nano == 500 and cw.upper_bound and cw.needs_eval
    rep = _Compaction()
    plan = _plan([lane("m1")], [], rep, include_tradeoffs=True,
                 context=ctx(rep, thresholds={
                     "context.compaction-window.min_compaction_window": "100000",
                     "context.compaction-window.post_tokens": "20283"}))
    assert _get(plan, "cc.autocompact_window").params == "compact-window=200000,post=20283"
    assert all(post == 20283 for _, post in rep.windows)
    rep = _Compaction()
    plan = _plan([lane("m1")], [], rep, include_tradeoffs=True,
                 context=ctx(rep, thresholds={
                     "context.compaction-window.max_extra_compactions": "10"}))
    assert _get(plan, "cc.autocompact_window").params == "compact-window=300000"
    with pytest.raises(Exception, match="threshold"):
        _plan([lane("m1")], [], rep, context=ctx(rep, thresholds={
            "context.compaction-window.post_tokens": "abc"}))


def _unpriced_geo(inner: FakeReplayer):
    class _Wrap(FakeReplayer):
        def replay(self, lanes, policy, **kw):
            result = inner.replay(lanes, policy, **kw)
            if policy.geo_global:
                saving = Figure(nano=None, evidence=Evidence.ESTIMATED, basis=Basis.LIST,
                                calibration=Calibration.UNCALIBRATED,
                                note="unpriced: a changed request has no rate row")
                result = dataclasses.replace(result, saving=saving)
            return result

    return _Wrap()


def test_unpriced_replays_make_credits_and_the_headline_unpriced() -> None:
    rep = _unpriced_geo(a7_replayer())
    plan = _plan([lane("l1")], a7_findings(), rep)
    # geo.global's standalone is unpriced, so its grid value cannot be chosen: dropped
    assert {r.lever_id for r in plan.levers} == {"cc.fast_mode_opt_in", "endpoint.global"}

    class _JointUnpriced(FakeReplayer):
        def replay(self, lanes, policy, **kw):
            result = a7_replayer().replay(lanes, policy, **kw)
            if policy.fast_off and policy.regional_to_global:
                result = dataclasses.replace(result, saving=Figure(
                    nano=None, evidence=Evidence.ESTIMATED, basis=Basis.LIST,
                    note="unpriced: joint"))
            return result

    plan = _plan([lane("l1")], a7_findings()[:2], _JointUnpriced())
    assert all(r.shapley.nano is None for r in plan.levers if ":" in r.group)
    assert plan.headline_monthly.nano is None
    assert plan.headline_monthly.note.startswith("unpriced:")
    assert plan.joint_saving.nano is None


def test_without_a_replayer_only_finding_projections_are_listed() -> None:
    tool = finding("tool-defs-bloat", ["cc.tool_search"], detector="context.static-prefix",
                   recoverable=est(600, upper_bound=True))
    hook = finding("cold-resume", ["cc.cold_resume_hook"], detector="cache.cold-resume")
    plan = _plan([lane("m1")], [tool, hook], None)
    assert {r.lever_id for r in plan.levers} == {"cc.tool_search", "cc.cold_resume_hook"}
    assert plan.sample.startswith("no replayer")
    assert plan.headline_monthly.nano == 0 and plan.joint_saving.nano == 0
    assert plan.method == "shapley-exact" and plan.groups == ()


def test_empty_scope_gives_an_empty_plan() -> None:
    plan = build_action_plan([], [], Loader([]), [], ctx(MIXED), window_days=7)
    assert plan.levers == () and plan.groups == ()
    assert plan.headline_monthly.nano == 0 and plan.headline_monthly.evidence is not \
        Evidence.EXACT
    assert plan.allowance_headroom_monthly is None


def test_linked_levers_carry_their_class_finding_ids() -> None:
    lanes = [lane("b1"), lane("s1", billing_path="subscription")]
    billed = finding("ttl-1h-recommended", ["cc.prompt_cache_ttl.main"],
                     detector="cache.ttl-advisor", lever_class="cache_transform",
                     lane_kind="main")
    allowance = finding("ttl-1h-recommended", ["cc.prompt_cache_ttl.main"],
                        detector="cache.ttl-advisor", lever_class="cache_transform",
                        lane_kind="main", billing_class="allowance", team="t2",
                        basis=Basis.LIST_EQUIVALENT)
    plan = _plan(lanes, [billed, allowance], MIXED)
    assert _get(plan, "cc.prompt_cache_ttl.main", Basis.LIST).finding_ids == (billed.finding_id,)
    assert _get(plan, "cc.prompt_cache_ttl.main", Basis.LIST_EQUIVALENT).finding_ids == (
        allowance.finding_id,)


def test_evaluated_levers_are_scaled_like_the_credits() -> None:
    lanes = [lane(f"m{i}") for i in range(10)]
    findings = [finding("effort-mix", ["cc.max_effort"], lever_class="trajectory")]
    plan = _plan(lanes, findings, MIXED, sample_lanes=4)
    ttl = _get(plan, "cc.prompt_cache_ttl.main")
    assert ttl.shapley.nano == 10_000                    # full-scope joint, scaled from 4 lanes
    assert _get(plan, "cc.max_effort").standalone.nano == 9_000
    assert "sample-scaled" in _get(plan, "cc.max_effort").standalone.note


def test_finding_fix_patches_do_not_change_the_plan_values() -> None:
    f = finding("ttl-1h-recommended", ["cc.prompt_cache_ttl.main"], detector="cache.ttl-advisor",
                fix=Fix(text="t", config_patch=(("promptCacheTtl", '"5m"'),),
                        target="claude-code-managed-settings", doc_url=None))
    with_fix = _get(_plan([lane("m1")], [f], MIXED), "cc.prompt_cache_ttl.main")
    without = _get(_plan([lane("m1")], [], MIXED), "cc.prompt_cache_ttl.main")
    assert (with_fix.params, with_fix.shapley) == (without.params, without.shapley)
    assert with_fix.finding_ids == (f.finding_id,) and without.finding_ids == ()


def test_team_less_lanes_and_subagent_lanes() -> None:
    lanes = [lane("s1", team=None, kind=LaneKind.SUBAGENT),
             lane("w1", team=None, kind=LaneKind.WORKFLOW_AGENT)]

    def fn(ln, pol: Policy) -> int:
        return 300 if any(ttl == "1h" and touches((sel,), ln) for sel, ttl in pol.ttl) else 0

    plan = _plan(lanes, [], FakeReplayer.from_function(fn))
    sub = _get(plan, "cc.prompt_cache_ttl.subagent")
    assert sub.shapley.nano == 600 and plan.joint_saving.nano == 600


def test_findings_with_only_aggregate_levers_open_no_billing_class() -> None:
    f = finding("idle-seat", ["copilot.seat_reclaim"], billing_class="pool",
                basis=Basis.LIST_EQUIVALENT)
    plan = _plan([lane("m1")], [f], MIXED)
    assert plan.pool_headroom_monthly is None and plan.allowance_headroom_monthly is None
    hook = finding("cold-resume", ["cc.cold_resume_hook"], billing_class="allowance",
                   basis=Basis.LIST_EQUIVALENT)
    plan = _plan([lane("m1")], [hook], MIXED)
    assert plan.allowance_headroom_monthly is not None
    assert plan.allowance_headroom_monthly.nano == 0
    assert _get(plan, "cc.cold_resume_hook").basis is Basis.LIST_EQUIVALENT
