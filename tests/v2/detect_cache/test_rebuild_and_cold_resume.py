"""``cache.rebuild`` (compaction-cold, edit-churn with Appendix A.10) and ``cache.cold-resume``
(Appendix A.5)."""

from __future__ import annotations

from fractions import Fraction

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind
from tokenbill.detect.cache_miss import MissByCause, RebuildEvents
from tokenbill.detect.cache_ttl import ColdResume

from .helpers import CAPS, SONNET5, ctx, event, lane, lane_a5, lane_a10, only

MIN_10C = {"min_usd": "0.10"}


# ---------------------------------------------------------------------------------------------
# edit churn (Appendix A.10)
# ---------------------------------------------------------------------------------------------


def test_appendix_a10_edit_churn() -> None:
    f = only(RebuildEvents().detect([lane_a10()], ctx(thresholds=MIN_10C)), "edit-churn")
    assert f.cost_observed.nano == 310_000_000                 # $0.31 = 62,000 × $5/M
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable is not None
    assert f.recoverable.nano == 257_600_000                   # $0.2976 − $0.04
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert f.n_events == 1
    event_item = next(e for e in f.evidence if e.kind == "event")
    attrs = dict(event_item.attrs)
    assert attrs["kstar"] == "37.2" and attrs["k_rem"] == 5 and attrs["cleared"] == 40_000
    assert attrs["rewritten"] == 62_000
    dist = dict(next(e for e in f.evidence if e.kind == "aggregate").attrs)
    assert dist["kstar_p50"] == "37.2" and dist["edits_not_paying_back"] == 1
    assert "37.2" in f.summary and f.fix is not None and "clear_at_least" in f.fix.text
    assert f.lever_class == "hygiene"
    assert set(f.references) >= {"context-editing-cost", "anth-context-editing-not-savings"}


def test_edit_churn_below_min_usd_by_default() -> None:
    assert RebuildEvents().detect([lane_a10()], ctx()) == []     # $0.2576 < $1.00


def test_edit_that_pays_back_loses_nothing() -> None:
    """With K_rem ≥ K*, the loss is 0: counted as an event, no finding above min_usd."""
    rows = [(0, 0, 100_000, 0, 0, 500), (30, 100_000, 20_000, 0, 0, 500),
            (60, 118_000, 2_000, 0, 0, 500)]       # the edit rewrites only 2,000 tokens
    prefix = 120_000
    for k in range(10):
        rows.append((90 + 30 * k, prefix, 1_000, 0, 0, 500))
        prefix += 1_000
    lane_ = lane("PB", rows, kind=LaneKind.API_RUN, product="agent_sdk",
                 per_request={2: {"applied_edits": (("clear_tool_uses_20250919", 40_000),)}})
    # K* = 2,000·4,800/(40,000·200) = 1.2 < K_rem = 10
    assert RebuildEvents().detect([lane_], ctx(thresholds={"min_usd": "0.000001"})) == []


def test_edit_while_cold_is_not_churn() -> None:
    """An edit after a gap longer than the TTL is a cold rebuild, not edit churn."""
    rows = [(0, 0, 100_000, 0, 0, 500), (30, 100_000, 20_000, 0, 0, 500),
            (400, 0, 82_000, 0, 0, 500), (430, 82_000, 2_000, 0, 0, 500)]
    lane_ = lane("CE", rows, kind=LaneKind.API_RUN, product="agent_sdk",
                 per_request={2: {"applied_edits": (("clear_tool_uses_20250919", 40_000),)}})
    assert RebuildEvents().detect([lane_], ctx(thresholds={"min_usd": "0"})) == []


def test_edit_churn_from_context_edit_events() -> None:
    """Without applied_edits the CONTEXT_EDIT events of the transition give X; K_rem stops at
    the next reset event."""
    key = "EV"
    rows = [(0, 0, 100_000, 0, 0, 500), (30, 100_000, 20_000, 0, 0, 500),
            (60, 20_000, 62_000, 0, 0, 500), (90, 82_000, 2_000, 0, 0, 500),
            (120, 84_000, 2_000, 0, 0, 500), (150, 0, 30_000, 0, 0, 500)]
    evs = [event(key, 59, "context_edit", edit_type="clear_tool_uses_20250919",
                 cleared_input_tokens=40_000),
           event(key, 140, "clear")]
    lane_ = lane(key, rows, kind=LaneKind.API_RUN, product="agent_sdk", events=evs)
    f = only(RebuildEvents().detect([lane_], ctx(thresholds=MIN_10C)), "edit-churn")
    attrs = dict(next(e for e in f.evidence if e.kind == "event").attrs)
    assert attrs["k_rem"] == 2 and attrs["cleared"] == 40_000
    # loss = 62,000·(5,000 − 200) − 40,000·200·2
    assert f.recoverable is not None and f.recoverable.nano == 297_600_000 - 16_000_000


def test_edit_churn_needs_events_or_attempts() -> None:
    caps = CAPS - {"events", "attempts"}
    assert RebuildEvents().detect([lane_a10()], ctx(thresholds=MIN_10C, caps=caps)) == []
    only(RebuildEvents().detect([lane_a10()], ctx(thresholds=MIN_10C, caps=caps | {"attempts"})),
         "edit-churn")


def test_kstar_is_the_payback_formula() -> None:
    """K* = S(α − β)/(Xβ) with α = w/u, β = r/u equals S(w − r)/(X·r)."""
    s, x, w, r, u = 62_000, 40_000, Fraction(5), Fraction(1, 5), Fraction(4)
    alpha, beta = w / u, r / u
    assert s * (alpha - beta) / (x * beta) == s * (w - r) / (x * r) == Fraction(372, 10)


# ---------------------------------------------------------------------------------------------
# compaction-cold
# ---------------------------------------------------------------------------------------------


def _compaction_lane(idle_s: int, key: str = "CC"):
    rows = [(0, 0, 150_000, 0, 0, 500), (30, 150_000, 10_000, 0, 0, 500),
            (30 + idle_s + 60, 0, 25_000, 0, 0, 500)]
    ev = event(key, 30 + idle_s, "compaction", trigger="manual", pre_tokens=160_000,
               post_tokens=20_000, duration_ms=30_000, dropped_tokens=None)
    return lane(key, rows, events=[ev])


def test_compaction_cold() -> None:
    """A compaction 900 s after the last request (TTL 300 s) re-reads 160,000 tokens cold:
    160,000 × 5,000 = $0.80 at the write rate; premium over a warm read 160,000 × 4,800."""
    f = only(RebuildEvents().detect([_compaction_lane(900)], ctx(thresholds=MIN_10C)),
             "compaction-cold")
    assert f.cost_observed.nano == 800_000_000
    assert f.cost_observed.evidence is Evidence.ESTIMATED    # the compaction input is a line
    assert f.recoverable is not None and f.recoverable.nano == 768_000_000
    assert f.lever_ids == ("cc.cold_resume_hook",)
    assert dict(f.evidence[0].attrs)["idle_ms"] == 900_000


def test_compaction_while_warm_is_fine() -> None:
    assert RebuildEvents().detect([_compaction_lane(30)], ctx(thresholds={"min_usd": "0"})) == []


def test_compaction_cold_needs_events_capability() -> None:
    caps = CAPS - {"events"}
    assert RebuildEvents().detect([_compaction_lane(900)],
                                  ctx(thresholds=MIN_10C, caps=caps)) == []


def test_compaction_cold_on_1h_lane_uses_1h_rate_and_ttl() -> None:
    key = "CH"
    rows = [(0, 0, 0, 150_000, 0, 500), (30, 150_000, 0, 10_000, 0, 500),
            (5000, 0, 0, 25_000, 0, 500)]
    evs = [event(key, 2000, "compaction", trigger="auto", pre_tokens=160_000, post_tokens=20_000,
                 duration_ms=30_000, dropped_tokens=None),        # 1,970 s < 3,600 s: warm
           event(key, 4900, "compaction", trigger="auto", pre_tokens=160_000,
                 post_tokens=20_000, duration_ms=30_000, dropped_tokens=None)]  # cold
    f = only(RebuildEvents().detect([lane(key, rows, events=evs)], ctx(thresholds=MIN_10C)),
             "compaction-cold")
    assert f.n_events == 1 and f.cost_observed.nano == 160_000 * 8_000


# ---------------------------------------------------------------------------------------------
# cold resume (Appendix A.5)
# ---------------------------------------------------------------------------------------------


def test_appendix_a5_cold_resume() -> None:
    f = only(ColdResume().detect([lane_a5()], ctx()), "cold-resume")
    assert f.n_events == 1
    assert f.cost_observed.nano == 4_000_000_000              # $4.00 EXACT
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable is not None and f.recoverable.nano == 3_900_000_000   # $3.90
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert set(f.lever_ids) == {"cc.prompt_cache_ttl.main", "cc.compact_on_resume",
                                "cc.cold_resume_hook"}
    assert f.fix is not None and f.fix.config_patch is not None
    assert f.fix.config_patch[0][0] == "hooks.SessionStart"
    assert "/compact" in f.fix.text and "/clear" in f.fix.text


def test_cold_resume_allowance_cohort() -> None:
    f = only(ColdResume().detect([lane_a5(billing_path="subscription")], ctx()), "cold-resume")
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    assert f.title.startswith("Allowance headroom:")
    assert ("billing_class", "allowance") in f.scope.dims


def test_cold_resume_thresholds() -> None:
    small = lane("SM", [(0, 0, 0, 90_000, 0, 500), (7200, 0, 0, 92_000, 0, 500)])
    assert ColdResume().detect([small], ctx(thresholds={"min_usd": "0"})) == []   # P < 100k
    partial = lane("PA", [(0, 0, 0, 200_000, 0, 500), (7200, 110_000, 0, 92_000, 0, 500)])
    # W = 92,000 < 0.5 × 200,000: a partial rebuild, not a cold resume
    assert ColdResume().detect([partial], ctx(thresholds={"min_usd": "0"})) == []
    override = ctx(thresholds={"min_usd": "0", "cache.cold-resume.min_context": "50000"})
    assert ColdResume().detect([small], override)


def test_cold_resume_main_lanes_only() -> None:
    sub = lane_a5(kind=LaneKind.SUBAGENT)
    assert ColdResume().detect([sub], ctx()) == []


def test_cold_resume_sdk_main_lane_has_no_claude_code_patch() -> None:
    f = only(ColdResume().detect([lane_a5(product="agent_sdk")], ctx()), "cold-resume")
    assert f.fix is not None and f.fix.config_patch is None
    assert f.lever_ids == ()


def test_miss_by_cause_sees_the_same_resume_as_ttl_expiry() -> None:
    f = only(MissByCause().detect([lane_a5()], ctx()), "ttl-expiry")
    assert f.cost_observed.nano == 4_000_000_000


def test_sonnet_rates() -> None:
    f = only(ColdResume().detect([lane_a5(model=SONNET5)], ctx()), "cold-resume")
    assert f.cost_observed.nano == 500_000 * 4_000
    assert f.recoverable is not None and f.recoverable.nano == 500_000 * 3_800
