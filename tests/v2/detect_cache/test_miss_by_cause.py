"""``cache.miss-by-cause``: one hand-computed fixture per cause (to the nano), cause precedence,
Appendix A.9 thresholds, the D28 effort exemption, allowance labeling and ``min_usd``."""

from __future__ import annotations

from typing import Any

import pytest

from tokenbill.core.labels import Basis, Calibration, Evidence
from tokenbill.core.records import LaneKind, RequestParams
from tokenbill.detect.cache_miss import MissByCause

from .helpers import (
    OPUS55,
    SONNET5,
    attribution,
    ctx,
    diag,
    event,
    lane,
    nano,
    only,
)

MIN_10C = {"min_usd": "0.10"}
BASE = (0, 0, 100_000, 0, 0, 500)   # request 0: a 100k 5m write on Opus 5.5

# kind: (request 1 row, per_request overrides of request 1, events, cost nano, premium nano)
# Rates (nano/token), Opus 5.5: u 4,000, r 200, w5 5,000; fast: r 400, w5 10,000;
# Sonnet 5: r 200, w5 2,500. E = min(P0 = 100k, T1); M = E − R1; mw = min(M, W1);
# mu = min(M − mw, U1); cost = mw·w + mu·u; premium = mw·(w − r) + mu·(u − r).
CASES: dict[str, tuple[Any, dict[str, Any], list[Any], int, int | None]] = {
    # 420 s > 300 s: TTL expiry; mw = 100k at w5
    "ttl-expiry": ((420, 0, 102_000, 0, 0, 500), {}, [], 500_000_000, 480_000_000),
    # Sonnet 5 serves request 1: rewrite at Sonnet's w5
    "model-switch": ((30, 0, 102_000, 0, 0, 500), {"model": SONNET5}, [], 250_000_000,
                     230_000_000),
    # fast mode on request 1: Opus 5.5 fast rates
    "param-change": ((30, 0, 102_000, 0, 0, 500), {"speed": "fast"}, [], 1_000_000_000,
                     960_000_000),
    # a COMPACTION event in (t0, t1]: E = min(100k, 30k) = 30k; no recoverable
    "compaction": ((30, 0, 30_000, 0, 0, 500), {}, ["compaction"], 150_000_000, None),
    "tools-changed": ((30, 0, 102_000, 0, 0, 500), {"diagnostics": diag("tools_changed")}, [],
                      500_000_000, 480_000_000),
    "system-changed": ((30, 0, 102_000, 0, 0, 500), {"diagnostics": diag("system_changed")}, [],
                       500_000_000, 480_000_000),
    "messages-changed": ((30, 0, 102_000, 0, 0, 500),
                         {"diagnostics": diag("messages_changed")}, [], 500_000_000,
                         480_000_000),
    # T1 = 80k < 0.9·100k: E = 80k, M = 60k = W1
    "context-shrank": ((30, 20_000, 60_000, 0, 0, 500), {}, [], 300_000_000, 288_000_000),
    # M = 100k: mw = 60k written, mu = 40k uncached: 60k·5,000 + 40k·4,000; premium
    # 60k·4,800 + 40k·3,800
    "unexplained": ((30, 0, 60_000, 0, 42_000, 500), {}, [], 460_000_000, 440_000_000),
}


def _case_lane(cause: str, key: str = "L1", **kw: Any):
    row, over, events, _cost, _prem = CASES[cause]
    evs = [event(key, 20, "compaction", trigger="auto", pre_tokens=100_000, post_tokens=25_000,
                 duration_ms=5_000, dropped_tokens=None)] if "compaction" in events else []
    return lane(key, [BASE, row], events=evs, per_request={1: over}, **kw)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_one_hand_computed_fixture_per_cause(kind: str) -> None:
    findings = MissByCause().detect([_case_lane(kind)], ctx(thresholds=MIN_10C))
    f = only(findings, kind)
    _row, _over, _ev, cost, premium = CASES[kind]
    assert f.cost_observed.nano == cost
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.cost_observed.basis is Basis.LIST
    assert nano(f.recoverable) == premium
    if premium is not None:
        assert f.recoverable is not None
        assert f.recoverable.evidence is Evidence.ESTIMATED
        assert f.recoverable.upper_bound
        assert f.recoverable.calibration is Calibration.UNCALIBRATED
    assert f.n_events == 1 and f.n_lanes == 1 and f.n_users == 1
    assert f.scope.dims == (("lane_kind", "main"), ("team", "payments"))
    assert f.audience == "org"
    assert f.fix is not None and f.fix.text
    assert set(f.references) >= {"cc-miss-taxonomy-ground-truth", "cc-usage-likely-cause"}


def test_levers_per_cause() -> None:
    ttl = only(MissByCause().detect([_case_lane("ttl-expiry")], ctx(thresholds=MIN_10C)),
               "ttl-expiry")
    # Claude Code main lane: the main TTL lever only (never the subagent or SDK TTL levers)
    assert ttl.lever_ids == ("cc.prompt_cache_ttl.main",)
    fast = only(MissByCause().detect([_case_lane("param-change")], ctx(thresholds=MIN_10C)),
                "param-change")
    assert fast.lever_ids == ("cc.fast_mode_opt_in",)
    switch = only(MissByCause().detect([_case_lane("model-switch")], ctx(thresholds=MIN_10C)),
                  "model-switch")
    assert switch.lever_ids == ()   # a user switch: fallback credit does not apply
    sdk = _case_lane("ttl-expiry", kind=LaneKind.API_RUN, product="agent_sdk")
    f = only(MissByCause().detect([sdk], ctx(thresholds=MIN_10C)), "ttl-expiry")
    assert f.lever_ids == ("sdk.ttl",)


def test_precedence_compaction_beats_idle_gap() -> None:
    """A COMPACTION event and a gap longer than the TTL: the cause is compaction."""
    key = "P1"
    lane_ = lane(key, [BASE, (900, 0, 30_000, 0, 0, 500)],
                 events=[event(key, 600, "compaction", trigger="auto", pre_tokens=100_000,
                               post_tokens=25_000, duration_ms=5_000, dropped_tokens=None)])
    kinds = [f.kind for f in MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0"}))]
    assert kinds == ["compaction"]


def test_precedence_switch_beats_idle_gap() -> None:
    lane_ = lane("P2", [BASE, (900, 0, 102_000, 0, 0, 500)], per_request={1: {"model": SONNET5}})
    kinds = [f.kind for f in MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0"}))]
    assert kinds == ["model-switch"]


@pytest.mark.parametrize(("prefix", "reads", "is_miss"), [
    (100_000, 98_000, False),    # M = 2,000 ≤ 5% of 100,000
    (100_000, 94_999, True),     # M = 5,001 > 5,000
    (30_000, 28_001, False),     # M = 1,999 < 2,000
    (30_000, 28_000, True),      # M = 2,000 > 1,500 and ≥ 2,000 (never 2,048)
])
def test_appendix_a9_thresholds(prefix: int, reads: int, is_miss: bool) -> None:
    lane_ = lane("A9", [(0, 0, prefix, 0, 0, 500), (30, reads, prefix - reads, 0, 0, 500)])
    findings = MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0"}))
    assert bool(findings) is is_miss
    if is_miss:
        f = only(findings, "unexplained")
        assert f.cost_observed.nano == (prefix - reads) * 5_000


def _effort_lane(key: str, product: str, kind: LaneKind) -> Any:
    rows = [BASE, (30, 0, 102_000, 0, 0, 500)]
    return lane(key, rows, kind=kind, product=product,
                per_request={0: {"params": RequestParams(model_requested=OPUS55, effort="high")},
                             1: {"params": RequestParams(model_requested=OPUS55, effort="low")}})


def test_effort_change_claude_code_opus55_is_not_a_miss_cause() -> None:
    cc_lane = _effort_lane("E-cc", "claude_code", LaneKind.MAIN)
    kinds = [f.kind for f in MissByCause().detect([cc_lane], ctx(thresholds=MIN_10C))]
    assert kinds == ["unexplained"]
    sdk_lane = _effort_lane("E-sdk", "agent_sdk", LaneKind.API_RUN)
    f = only(MissByCause().detect([sdk_lane], ctx(thresholds=MIN_10C)), "param-change")
    assert ("sub_effort-change", 1) in f.evidence[0].attrs


def test_allowance_cohort_is_list_equivalent() -> None:
    lane_ = _case_lane("ttl-expiry", billing_path="subscription")
    f = only(MissByCause().detect([lane_], ctx(thresholds=MIN_10C)), "ttl-expiry")
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    assert f.recoverable is not None and f.recoverable.basis is Basis.LIST_EQUIVALENT
    assert f.title.startswith("Allowance headroom:")
    assert "list-equivalent, not invoice dollars" in f.summary
    assert ("billing_class", "allowance") in f.scope.dims
    assert not f.cost_observed.is_billed_eligible


def test_min_usd_gates_on_cost_observed() -> None:
    lane_ = _case_lane("ttl-expiry")        # $0.50 billed
    assert MissByCause().detect([lane_], ctx()) == []                     # default $1.00
    assert MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0.50"}))
    assert MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0.51"})) == []


def test_aggregates_per_cohort_and_counts_users() -> None:
    lanes = [_case_lane("ttl-expiry", key=f"L{i}", principal=f"r_dev{i % 2}") for i in range(3)]
    lanes.append(_case_lane("ttl-expiry", key="X", team="search"))
    findings = MissByCause().detect(lanes, ctx(thresholds=MIN_10C))
    by_team = {dict(f.scope.dims)["team"]: f for f in findings}
    assert by_team["payments"].n_events == 3 and by_team["payments"].n_lanes == 3
    assert by_team["payments"].n_users == 2
    assert by_team["payments"].cost_observed.nano == 3 * 500_000_000
    assert by_team["search"].n_events == 1
    # sorted by recoverable, largest first
    assert [f.recoverable.nano for f in findings if f.recoverable] == sorted(
        (f.recoverable.nano for f in findings if f.recoverable), reverse=True)


def test_diagnostics_validate_the_cause() -> None:
    row = (420, 0, 102_000, 0, 0, 500)
    lanes = [lane(f"V{i}", [BASE, row],
                  per_request={1: {"diagnostics": diag("previous_message_not_found")}})
             for i in range(3)]
    f = only(MissByCause().detect(lanes, ctx(thresholds=MIN_10C)), "ttl-expiry")
    assert f.validated_against == "cache_miss_reason previous_message_not_found: 3/3"


def test_unknown_ttl_writes_are_a_range() -> None:
    """A miss rewritten into unknown-TTL writes (no TTL split) is priced [5m, 1h] (R5)."""
    from tokenbill.core.builders import make_lane, make_request

    attr = attribution()
    r0 = make_request("U", 0, 1_790_121_600_000, {"cache_write_unknown": 100_000, "output": 5},
                      OPUS55, attribution=attr, billing_path="api_key", write_ttl_hint="5m")
    r1 = make_request("U", 1, 1_790_121_600_000 + 420_000,
                      {"cache_write_unknown": 102_000, "output": 5}, OPUS55, attribution=attr,
                      billing_path="api_key", write_ttl_hint="5m")
    lane_ = make_lane([r0, r1], lane_key="U")
    f = only(MissByCause().detect([lane_], ctx(thresholds=MIN_10C)), "ttl-expiry")
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert (f.cost_observed.low_nano, f.cost_observed.nano, f.cost_observed.high_nano) == (
        500_000_000, 500_000_000, 800_000_000)
    assert f.recoverable is not None and f.recoverable.low_nano == 480_000_000


def test_unpriced_model_is_never_zero() -> None:
    lane_ = lane("Z", [BASE, (420, 0, 102_000, 0, 0, 500)], model="claude-sonnet-5-5")
    assert MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0"})) == []


def test_self_view_audience_and_filter() -> None:
    mine = _case_lane("ttl-expiry", key="M", principal="r_me")
    other = _case_lane("ttl-expiry", key="O", principal="r_other")
    findings = MissByCause().detect([mine, other],
                                    ctx(thresholds=MIN_10C, self_principal="r_me"))
    f = only(findings, "ttl-expiry")
    assert f.audience == "self" and f.n_lanes == 1
