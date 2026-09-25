"""SPEC §3.22 / §10.1: the shared detector helpers."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from tokenbill.core.builders import FlatRates, make_ctx
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import ContractViolation, PricingError, UsageError
from tokenbill.core.findings import (
    build_finding,
    cohort_key,
    evidence_magnitude,
    finding_id,
    fit_cpt,
    make_scope,
    min_usd_nano,
    miss_waste,
    rate_nano,
    sum_figures,
    threshold,
    top_evidence,
)
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, Finality, estimated, exact
from tokenbill.core.records import AppendedItem, Attribution, LaneKind
from tokenbill.core.transitions import classify_transitions
from tokenbill.core.types import AnalysisContext, EvidenceItem, Scope

from .helpers import event, lane, req, usage

PRICER = FlatRates()
RULES = RulesTable()


def ctx(**thresholds: str) -> AnalysisContext:
    return AnalysisContext(pricer=PRICER, rules=RULES, replayer=None, calibration=None,
                           window=(0, 10**12), capabilities=frozenset(), thresholds=thresholds)


# ---------------------------------------------------------------------------------------------
# cohorts, scopes, ids
# ---------------------------------------------------------------------------------------------


def test_cohort_key() -> None:
    billed = lane([req("L", 0, 0, usage(w5=1), attribution=Attribution(team="payments"))])
    assert cohort_key(billed) == ("payments", "main", "billed")
    seat = lane([req("L", 0, 0, usage(w5=1), billing_path="subscription")],
                kind=LaneKind.SUBAGENT)
    assert cohort_key(seat) == (None, "subagent", "allowance")
    blank = lane([req("L", 0, 0, usage(w5=1), attribution=Attribution(team=""))])
    assert cohort_key(blank)[0] is None
    assert cohort_key(lane([])) == (None, "main", "billed")


def test_make_scope() -> None:
    scope = make_scope(team="payments", lane_kind=LaneKind.MAIN, model=None,
                       billing_class="billed")
    assert scope == Scope(dims=(("billing_class", "billed"), ("lane_kind", "main"),
                                ("team", "payments")))
    assert make_scope() == Scope(dims=())
    with pytest.raises(ContractViolation):
        make_scope(team=5)  # type: ignore[arg-type]


def test_finding_id_is_stable_and_scope_order_independent() -> None:
    scope = make_scope(team="payments", lane_kind="main")
    fid = finding_id("cache.miss-by-cause", "ttl-expiry", scope)
    assert fid == stable_id("fd", "cache.miss-by-cause", "ttl-expiry", "lane_kind=main",
                            "team=payments")
    unsorted = Scope(dims=(("team", "payments"), ("lane_kind", "main")))
    assert finding_id("cache.miss-by-cause", "ttl-expiry", unsorted) == fid
    assert finding_id("cache.miss-by-cause", "ttl-expiry", make_scope(team="search")) != fid
    assert finding_id("cache.miss-by-cause", "model-switch", scope) != fid


# ---------------------------------------------------------------------------------------------
# build_finding
# ---------------------------------------------------------------------------------------------

EV = [EvidenceItem(kind="transition", ref=f"rq_{i}", attrs=(("nano", i * 10),)) for i in range(5)]


def fields(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        detector_id="cache.miss-by-cause", kind="ttl-expiry", detector_version="1.0",
        category="breaker", lever_class="cache_transform", audience="org",
        title="TTL-expiry rewrites in payments", summary="Cache writes repeated after idle gaps.",
        scope=make_scope(team="payments", lane_kind="main"), n_events=3, n_lanes=1, n_users=5,
        first_seen_ms=1000, cost_observed=exact(2_100_000_000, Basis.LIST),
        recoverable=estimated(1_150_800_000, Basis.LIST, upper_bound=True),
        evidence=list(EV), references=["cc-miss-taxonomy-ground-truth"], lever_ids=["x"])
    base.update(over)
    return base


def test_build_finding_happy_path() -> None:
    f = build_finding(**fields())
    assert f.finding_id == finding_id("cache.miss-by-cause", "ttl-expiry", f.scope)
    assert isinstance(f.evidence, tuple) and isinstance(f.references, tuple)
    assert f.lever_ids == ("x",)
    again = build_finding(**fields(finding_id=f.finding_id))
    assert again == f


def test_finding_id_independent_of_evidence_order() -> None:
    a = build_finding(**fields(evidence=EV))
    b = build_finding(**fields(evidence=list(reversed(EV))))
    assert a.finding_id == b.finding_id and a.evidence != b.evidence


@pytest.mark.parametrize("over", [
    {"finding_id": "fd_wrong"},
    {"title": ""}, {"title": "x" * 121}, {"summary": "y" * 401},
    {"references": []}, {"references": [""]},
    {"evidence": [EV[0]] * 21}, {"evidence": ["not evidence"]},
    {"lever_ids": [1]},
    {"category": "misc"}, {"lever_class": "magic"}, {"audience": "everyone"},
    {"confidence": "certain"}, {"detector_version": ""},
    {"n_events": -1}, {"n_users": 1.5}, {"first_seen_ms": None}, {"needs_eval": "yes"},
    {"cost_observed": 5}, {"recoverable": 5},
    {"recoverable": estimated(1, Basis.CONTRACT, note="x")},
    {"scope": "team=x"}, {"detector_id": ""}, {"kind": None},
])
def test_build_finding_rejects(over: dict[str, Any]) -> None:
    with pytest.raises(ContractViolation):
        build_finding(**fields(**over))


def test_build_finding_requires_every_field() -> None:
    data = fields()
    del data["n_lanes"]
    with pytest.raises(ContractViolation):
        build_finding(**data)


def test_allowance_basis_rule() -> None:
    allowance_scope = make_scope(team="payments", lane_kind="main", billing_class="allowance")
    ok = build_finding(**fields(
        scope=allowance_scope, title="Allowance headroom: TTL",
        summary="list-equivalent, not invoice dollars",
        cost_observed=exact(10, Basis.LIST_EQUIVALENT),
        recoverable=estimated(5, Basis.LIST_EQUIVALENT, note="formula")))
    assert ok.cost_observed.basis is Basis.LIST_EQUIVALENT
    with pytest.raises(ContractViolation):   # allowance cohort priced on a billed basis
        build_finding(**fields(scope=allowance_scope))
    with pytest.raises(ContractViolation):   # list-equivalent outside an allowance cohort
        build_finding(**fields(cost_observed=exact(10, Basis.LIST_EQUIVALENT),
                               recoverable=None))


def test_provider_estimates_only_in_data_quality() -> None:
    pe = Figure(nano=10, evidence=Evidence.EXACT, basis=Basis.PROVIDER_ESTIMATE)
    with pytest.raises(ContractViolation):
        build_finding(**fields(cost_observed=pe, recoverable=None))
    dq = build_finding(**fields(cost_observed=pe, recoverable=None, category="data-quality",
                                lever_class="none", kind="cost-state-drift"))
    assert dq.category == "data-quality"


# ---------------------------------------------------------------------------------------------
# miss_waste
# ---------------------------------------------------------------------------------------------


def test_miss_waste_on_hand_fixtures() -> None:
    # Appendix A.1 transition 1: M = 100,000, W = 102,000, U = 0 → all rewritten as writes
    a1 = lane([req("L", 0, 0, usage(w5=100_000)), req("L", 1, 420, usage(w5=102_000))])
    (t,) = classify_transitions(a1, pricer=PRICER, rules=RULES)
    assert miss_waste(t, a1.requests[1]) == (100_000, 0)
    # M = 50,000 re-sent as 30,000 writes and 40,000 uncached → (30,000, 20,000)
    mixed = lane([req("L", 0, 0, usage(w5=50_000)), req("L", 1, 30, usage(w5=30_000, u=40_000))])
    (t,) = classify_transitions(mixed, pricer=PRICER, rules=RULES)
    assert t.missed == 50_000 and miss_waste(t, mixed.requests[1]) == (30_000, 20_000)
    # the buckets bound the split: M = 50,000 but only 10,000 + 5,000 were billed
    t_small = dataclasses.replace(t, missed=50_000)
    tiny = req("L", 1, 30, usage(w1=10_000, u=5_000))
    assert miss_waste(t_small, tiny) == (10_000, 5_000)
    residual = req("L", 1, 30, usage(o=10), kind="output_residual")
    assert miss_waste(t, residual) == (0, 0)
    assert miss_waste(dataclasses.replace(t, missed=0), mixed.requests[1]) == (0, 0)


# ---------------------------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------------------------


def test_min_usd_and_threshold() -> None:
    assert min_usd_nano(ctx()) == 1_000_000_000
    assert min_usd_nano(ctx(min_usd="0.10")) == 100_000_000
    assert min_usd_nano(ctx(min_usd="0.0000000004")) == 0
    assert threshold(ctx(), "cache.ttl-advisor.share", "0.02") == Decimal("0.02")
    assert threshold(ctx(**{"cache.ttl-advisor.share": "0.05"}), "cache.ttl-advisor.share",
                     "0.02") == Decimal("0.05")
    for bad in ("abc", "NaN", "Infinity", ""):
        with pytest.raises(UsageError):
            min_usd_nano(ctx(min_usd=bad))
    with pytest.raises(UsageError):
        min_usd_nano(ctx(min_usd="1e5000"))
    with pytest.raises(UsageError):
        threshold(AnalysisContext(pricer=PRICER, rules=RULES, replayer=None, calibration=None,
                                  window=(0, 1), capabilities=frozenset(),
                                  thresholds={"k": 0.5}), "k", "1")  # type: ignore[dict-item]
    with pytest.raises(UsageError):
        threshold(ctx(), "k", True)  # type: ignore[arg-type]
    assert threshold(ctx(), "k", 3) == Decimal(3)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# fit_cpt
# ---------------------------------------------------------------------------------------------


def carry_lane(n: int, *, bytes_per_token: int = 3, key: str = "L", assistant: bool = True,
               images: bool = False, model_switch_at: int | None = None,
               edit_at: int | None = None) -> Any:
    """Each request appends ``k`` tokens worth ``k·bytes_per_token`` bytes of tool output."""
    reqs = []
    total = 10_000
    prev_out = 200
    for i in range(n):
        items = []
        if i > 0:
            k = 1_000 + 37 * i
            items.append(AppendedItem(kind="tool_result", name="Bash", n_bytes=k * bytes_per_token,
                                      images=1 if images else 0))
            if assistant:
                items.append(AppendedItem(kind="assistant", name=None,
                                          n_bytes=prev_out * bytes_per_token))
            total += k + prev_out
        model = "claude-sonnet-5" if model_switch_at is not None and i >= model_switch_at \
            else "claude-opus-5-5"
        edits = [("clear_tool_uses_20250919", 50)] if i == edit_at else []
        reqs.append(req(key, i, 10 * i, usage(r=total - 100, w5=100, o=prev_out), model,
                        appended=items, applied_edits=edits))
    return lane(reqs, lane_key=key)


def test_fit_cpt_falls_back_below_30_samples() -> None:
    few = [carry_lane(30)]                     # 29 samples
    assert fit_cpt(few, "claude-4.7+") == (Decimal("2.5"), 29)
    assert fit_cpt(few, "claude-legacy") == (Decimal("3.3"), 29)
    assert fit_cpt([], "openai-o200k") == (Decimal("2.5"), 0)


def test_fit_cpt_fits_bytes_per_token() -> None:
    cpt, n = fit_cpt([carry_lane(31)], "claude-legacy")
    assert (cpt, n) == (Decimal(3), 30)
    cpt, n = fit_cpt([carry_lane(20, bytes_per_token=4, key="A"),
                      carry_lane(20, bytes_per_token=4, key="B")], "claude-4.7+")
    assert (cpt, n) == (Decimal(4), 38)


def test_fit_cpt_without_assistant_items_subtracts_the_previous_output() -> None:
    lane_obj = carry_lane(31, assistant=False)
    # bytes cover the tool output only; the previous output (200 tokens) is subtracted
    cpt, n = fit_cpt([lane_obj], "claude-4.7+")
    assert n == 30 and cpt == Decimal(3)


def test_fit_cpt_excludes_unusable_samples() -> None:
    assert fit_cpt([carry_lane(40, images=True)], "claude-4.7+")[1] == 0
    assert fit_cpt([carry_lane(40, model_switch_at=20)], "claude-4.7+")[1] == 38
    assert fit_cpt([carry_lane(40, bytes_per_token=20)], "claude-4.7+")[1] == 0  # implausible
    base = carry_lane(3)
    with_reset = lane(list(base.requests), events=[event("clear", 15)])
    assert fit_cpt([with_reset], "claude-4.7+")[1] == 1
    assert fit_cpt([carry_lane(3, edit_at=1)], "claude-4.7+")[1] == 1
    residual = req("L", 5, 5, usage(o=9), kind="output_residual")
    assert fit_cpt([lane([base.requests[0], residual, base.requests[1]])],
                   "claude-4.7+")[1] == 1


# ---------------------------------------------------------------------------------------------
# evidence, figures, rates
# ---------------------------------------------------------------------------------------------


def test_top_evidence() -> None:
    items = [EvidenceItem("transition", "b", (("nano", 5),)),
             EvidenceItem("transition", "a", (("nano", 5),)),
             EvidenceItem("event", "c", (("magnitude", 9), ("nano", 1))),
             EvidenceItem("aggregate", "d", (("tokens", 7),)),
             EvidenceItem("aggregate", "e", (("label", "x"),))]
    assert [e.ref for e in top_evidence(items)] == ["c", "d", "a", "b", "e"]
    assert [e.ref for e in top_evidence(items, 2)] == ["c", "d"]
    assert top_evidence(items, 0) == ()
    assert evidence_magnitude(items[2]) == 9 and evidence_magnitude(items[4]) == 0
    assert len(top_evidence([items[0]] * 50)) == 20


def test_sum_figures() -> None:
    assert sum_figures([], Basis.LIST) == exact(0, Basis.LIST)
    final = exact(5, Basis.LIST, finality=Finality.FINAL)
    assert sum_figures([final], Basis.LIST) == final
    total = sum_figures([exact(5, Basis.LIST),
                         estimated(10, Basis.LIST, low=8, high=12, note="x")], Basis.LIST)
    assert (total.nano, total.low_nano, total.high_nano) == (15, 13, 17)
    assert total.evidence is Evidence.ESTIMATED and total.calibration is Calibration.UNCALIBRATED
    with pytest.raises(ContractViolation):
        sum_figures([exact(5, Basis.LIST_EQUIVALENT)], Basis.LIST)


class NoUnitRates(FlatRates):
    def unit_rates(self, ctx: Any, *, ts_ms: int) -> None:
        return None


OPUS = make_ctx("claude-opus-5-5")


@pytest.mark.parametrize(("bucket", "tokens", "nano"), [   # FlatRates: $1 / $5 per MTok
    ("uncached_input", 1000, 1_000_000), ("uncached", 1000, 1_000_000),
    ("input", 1000, 1_000_000), ("cache_read", 1000, 100_000),
    ("cache_write_5m", 1000, 1_250_000), ("cache_write_1h", 1000, 2_000_000),
    ("cache_write_other", 1000, 1_250_000), ("output", 1000, 5_000_000),
    ("cache_write_unknown", 1000, 1_250_000), ("web_search", 2, 20_000_000),
    ("cache_read", 3, 300), ("cache_read", 1, 100), ("output", -1000, -5_000_000),
    ("output", 0, 0),
])
def test_rate_nano(bucket: str, tokens: int, nano: int) -> None:
    assert rate_nano(PRICER, OPUS, 0, bucket, tokens) == nano
    assert rate_nano(NoUnitRates(), OPUS, 0, bucket, tokens) == nano


def test_rate_nano_uses_the_ttl_hint_for_unknown_writes() -> None:
    hinted = make_ctx("claude-opus-5-5", write_ttl_hint="1h")
    assert rate_nano(PRICER, hinted, 0, "cache_write_unknown", 1000) == 2_000_000


def test_rate_nano_errors() -> None:
    with pytest.raises(PricingError):
        rate_nano(PRICER, make_ctx(""), 0, "output", 10)
    with pytest.raises(UsageError):
        rate_nano(PRICER, OPUS, 0, "cache_write", 10)
    with pytest.raises(UsageError):
        rate_nano(PRICER, OPUS, 0, "output", 1.5)  # type: ignore[arg-type]


def test_provider_estimate_data_quality_finding_in_an_allowance_cohort() -> None:
    """R4 and D26 together: a provider estimate is neither billed nor list-equivalent, so a
    data-quality finding may carry it in an allowance cohort; other categories still may not."""
    pe = Figure(nano=10, evidence=Evidence.EXACT, basis=Basis.PROVIDER_ESTIMATE)
    allowance_scope = make_scope(team="payments", lane_kind="main", billing_class="allowance")
    dq = build_finding(**fields(scope=allowance_scope, cost_observed=pe, recoverable=None,
                                category="data-quality", lever_class="none",
                                kind="cost-state-drift"))
    assert dq.cost_observed.basis is Basis.PROVIDER_ESTIMATE
    with pytest.raises(ContractViolation):
        build_finding(**fields(scope=allowance_scope, cost_observed=pe, recoverable=None))


def test_fit_cpt_reset_window_is_half_open() -> None:
    base = carry_lane(3)                       # requests at 0, 10, 20 s → two samples
    at_prev = lane(list(base.requests), events=[event("clear", 10)])     # in (0, 10] only
    assert fit_cpt([at_prev], "claude-4.7+")[1] == 1
    at_start = lane(list(base.requests), events=[event("compaction", 0, trigger="auto",
                                                       pre_tokens=1, post_tokens=1,
                                                       duration_ms=1, dropped_tokens=None)])
    assert fit_cpt([at_start], "claude-4.7+")[1] == 2               # ts == first request: none
    many = lane(list(base.requests),
                events=[event("human_prompt", 1 + i / 1000) for i in range(2000)]
                + [event("context_edit", 20, edit_type="clear_tool_uses_20250919",
                         cleared_input_tokens=5)])
    assert fit_cpt([many], "claude-4.7+")[1] == 1                   # only (10, 20] is reset
