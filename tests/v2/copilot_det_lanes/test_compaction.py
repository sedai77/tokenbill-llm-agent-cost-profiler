"""``compaction-cost`` (addendum §10.3): exact compaction credits by ``copilot_trigger`` and the
forced share; events-only CLI sessions qualify (no ``usage_sequence`` needed)."""

from __future__ import annotations

import dataclasses

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind
from tokenbill.detect.copilot_lanes import FORCED_TRIGGERS, _join_compactions

from .helpers import (
    CLI,
    EVENTS_ONLY_CAPS,
    SONNET5,
    T0,
    attr,
    attrs,
    compaction_event,
    compaction_inf,
    ctx,
    lane,
    one,
    only,
    req,
    run,
)

# Claude Sonnet 5 on Copilot: input 2,000 nano/token, output 10,000 nano/token.
A = compaction_inf("cmp-a", u=100_000, o=2_000)     # 220,000,000 — context_limit_retry
B = compaction_inf("cmp-b", u=50_000, o=1_000)      # 110,000,000 — memory_pressure
C = compaction_inf("cmp-c", u=150_000, o=3_000)     # 330,000,000 — threshold


def events_only_cli_lane(key: str = "cli-ev", *, workload: str = "interactive") -> object:
    """An events-only CLI session: output-only requests, exact COMPACTION inferences next to
    their ``session.compaction_complete`` events, one manual compaction without an inference."""
    a = attr(product=CLI, workload=workload)
    return lane(key, [
        req(key, 0, 0, o=500, model=SONNET5, a=a),
        req(key, 1, 100, o=500, model=SONNET5, a=a, extra=(A,)),
        req(key, 2, 200, o=500, model=SONNET5, a=a, extra=(B,)),
        req(key, 3, 300, o=500, model=SONNET5, a=a, extra=(C,)),
    ], events=[
        compaction_event(key, 100, "context_limit_retry"),
        compaction_event(key, 200, "memory_pressure"),
        compaction_event(key, 300, "threshold"),
        compaction_event(key, 400, "manual"),
    ])


def test_forced_compactions_counted_and_priced_exactly() -> None:
    f = one(run([events_only_cli_lane()], ctx(min_usd="0.10", caps=EVENTS_ONLY_CAPS)),
            "compaction-cost")
    assert f.cost_observed.nano == 660_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT and f.recoverable is None
    forced = attrs(f, "forced")
    assert (forced["compactions"], forced["total"], forced["share_pct"], forced["nano"]) == \
        (2, 4, "50.0", 330_000_000)
    assert attrs(f, "trigger:context_limit_retry") == {"compactions": 1, "priced": 1,
                                                       "nano": 220_000_000}
    assert attrs(f, "trigger:memory_pressure")["nano"] == 110_000_000
    assert attrs(f, "trigger:threshold")["nano"] == 330_000_000
    assert attrs(f, "trigger:manual") == {"compactions": 1, "priced": 0, "nano": 0}
    assert (f.n_events, f.n_lanes, f.n_users) == (4, 1, 1)
    assert f.category == "lever" and f.lever_class == "none" and f.lever_ids == ()
    assert "50.0% forced" in f.title and f.confidence == "high"
    assert f.fix is not None and f.fix.target == "github-copilot"
    assert f.first_seen_ms == T0 + 100_000


def test_forced_trigger_vocabulary() -> None:
    assert frozenset({"context_limit_retry", "memory_pressure"}) == FORCED_TRIGGERS


def test_min_usd_gates_on_the_compaction_credits() -> None:
    assert only(run([events_only_cli_lane()], ctx(caps=EVENTS_ONLY_CAPS)), "compaction-cost") == []


def test_events_without_inferences_carry_no_money() -> None:
    key = "vs-ev"
    vs = lane(key, [req(key, 0, 0, u=1_000, o=100, model=SONNET5)],
              events=[compaction_event(key, 10, "threshold")])
    assert only(run([vs], ctx(min_usd="0")), "compaction-cost") == []


def test_join_nearest_event_within_window() -> None:
    key = "L"
    infs = [(T0 + 100_000, A), (T0 + 200_000, B), (T0 + 900_000, C)]
    events = [compaction_event(key, 170, "memory_pressure"),     # 70 s from A, 30 s from B
              compaction_event(key, 95, "context_limit_retry"),
              compaction_event(key, 290, "threshold")]    # 90 s after B: too far
    pairs = _join_compactions(infs, events)
    got = [(name, pair[1].inference_id if pair else None) for name, pair in pairs]
    assert got == [("context_limit_retry", "cmp-a"), ("memory_pressure", "cmp-b"),
                   ("unknown", "cmp-c"), ("threshold", None)]


def test_join_ties_take_the_earlier_event_and_fallback_trigger() -> None:
    key = "L"
    ev_late = compaction_event(key, 110, "threshold")
    ev_early = compaction_event(key, 90, "manual")
    no_copilot = compaction_event(key, 500, None)          # copilot_trigger None → trigger
    free_text = compaction_event(key, 600, "some free text")
    pairs = _join_compactions([(T0 + 100_000, A)], [ev_late, ev_early, no_copilot, free_text])
    names = [name for name, _ in pairs]
    assert names == ["manual", "threshold", "auto", "custom"]


def test_unmatched_inference_is_unknown_and_compaction_lanes_have_their_cohort() -> None:
    a = attr(product=CLI)
    key = "cli-cmp"
    comp_lane = lane(key, [req(key, 0, 0, u=100_000, o=2_000, model=SONNET5, a=a,
                               kind="compaction")], kind=LaneKind.COMPACTION)
    f = one(run([comp_lane], ctx(min_usd="0.10")), "compaction-cost")
    assert dict(f.scope.dims)["lane_kind"] == "compaction"
    assert attrs(f, "trigger:unknown")["nano"] == 220_000_000
    assert attrs(f, "forced")["share_pct"] == "0.0"


def test_unbillable_compaction_is_ignored_and_unpriced_disclosed() -> None:
    key = "cli-u"
    a = attr(product=CLI)
    free = dataclasses.replace(compaction_inf("cmp-free", u=10_000, o=100), billable=False)
    unpriced = compaction_inf("cmp-x", u=100_000, o=2_000, model="claude-unknown-9")
    ln = lane(key, [req(key, 0, 0, o=10, model=SONNET5, a=a, extra=(free,)),
                    req(key, 1, 50, o=10, model=SONNET5, a=a, extra=(A,)),
                    req(key, 2, 90, o=10, model=SONNET5, a=a, extra=(unpriced,))])
    f = one(run([ln], ctx(min_usd="0.10")), "compaction-cost")
    assert f.cost_observed.nano == 220_000_000 and f.n_events == 2
    assert "1 inferences had no priced rate" in f.summary


def test_billing_uncertain_compaction_is_an_estimated_range() -> None:
    key = "cli-r"
    a = attr(product=CLI)
    maybe = dataclasses.replace(compaction_inf("cmp-m", u=100_000, o=2_000), billable=None)
    ln = lane(key, [req(key, 0, 0, o=10, model=SONNET5, a=a, extra=(maybe, A))])
    f = one(run([ln], ctx(min_usd="0.10")), "compaction-cost")
    assert f.cost_observed.evidence is Evidence.ESTIMATED and f.confidence == "medium"
    assert (f.cost_observed.low_nano, f.cost_observed.nano, f.cost_observed.high_nano) == \
        (220_000_000, 440_000_000, 440_000_000)
