"""Hypothesis properties of the aggregate plan (Shapley efficiency, the pool rule's split of a
credit saving, seat-saving bounds, order independence) and fuzz of the parsers CP-PLAN owns (the
seat-count reader of finding evidence, the plan over hostile findings): only ``TokenbillError``
escapes."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.copilot.plan import plan_copilot, seat_counts
from tokenbill.core.errors import TokenbillError
from tokenbill.core.labels import Basis
from tokenbill.core.types import EvidenceItem

from .worlds import PRICER, USD, C, World, finding, idle, lever, plan_for

FAST = "copilot.fast_mode_off"
SEAT = "copilot.seat_reclaim"
AUTO = "copilot.default_model_auto"

SETTINGS = settings(max_examples=40, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


def _world(business: int, enterprise: int, use: int, fast_units: int) -> World:
    """Seats, *use* pooled Sonnet credits and *fast_units* × 1M fast-mode Opus 4.8 output tokens
    (each $50 fast, $25 standard: a 2,500-credit premium)."""
    w = World().seats("business", business)
    if enterprise:
        w.seats("enterprise", enterprise)
    if use:
        w.usage(use, users=2)
    if fast_units:
        w.usage(5_000 * fast_units, model="Claude Opus 4.8 (fast mode)",
                output_tokens=1_000_000 * fast_units)
    return w.ide("t1", {"ide:vscode": 3, "ide:intellij": 1})


@SETTINGS
@given(business=st.integers(1, 400), enterprise=st.integers(0, 100),
       use=st.integers(0, 2_000_000), fast_units=st.integers(1, 40))
def test_invoice_plus_headroom_is_the_credit_saving(business: int, enterprise: int, use: int,
                                                    fast_units: int) -> None:
    plan = plan_for(_world(business, enterprise, use, fast_units),
                    [finding("fast-mode", team="t1")])
    inv, head = lever(plan, FAST), lever(plan, FAST, Basis.LIST_EQUIVALENT)
    premium = 2_500 * fast_units * C
    assert inv.standalone.nano + head.standalone.nano == premium
    assert 0 <= inv.standalone.nano <= premium
    assert inv.shapley.nano == plan.joint_saving.nano             # one player: φ = v(all)


@SETTINGS
@given(business=st.integers(10, 400), use=st.integers(0, 1_500_000),
       removable=st.integers(1, 10), fast_units=st.integers(0, 20),
       with_auto=st.booleans())
def test_shapley_is_efficient_and_seat_savings_are_bounded(business: int, use: int,
                                                           removable: int, fast_units: int,
                                                           with_auto: bool) -> None:
    fs = [idle(removable)]
    if fast_units:
        fs.append(finding("fast-mode", team="t1"))
    if with_auto:
        fs.append(finding("auto-adoption", team="t1"))
    plan = plan_for(_world(business, 0, use, fast_units), fs)
    listed = [lv for lv in plan.levers if lv.basis is Basis.LIST]
    assert sum(lv.shapley.nano for lv in listed) == plan.joint_saving.nano
    heads = [lv for lv in plan.levers if lv.basis is Basis.LIST_EQUIVALENT]
    if heads:
        assert {lv.params for lv in heads} == {lv.params for lv in listed}
    seat = lever(plan, SEAT)
    # a removal saves at most its fees and never costs money outside the promo months
    assert 0 <= seat.standalone.nano <= removable * 19 * USD
    for lv in listed:
        fig = lv.shapley
        if fig.low_nano is not None:
            assert fig.low_nano <= fig.nano <= fig.high_nano


@SETTINGS
@given(business=st.integers(10, 400), use=st.integers(0, 1_500_000), n=st.integers(1, 8))
def test_more_idle_seats_never_save_less(business: int, use: int, n: int) -> None:
    w = _world(business, 0, use, 0)
    fewer = lever(plan_for(w, [idle(n)]), SEAT).standalone.nano
    more = lever(plan_for(w, [idle(n + 1)]), SEAT).standalone.nano
    assert more >= fewer


@SETTINGS
@given(order=st.permutations(list(range(7))), seed=st.integers(0, 3))
def test_order_of_findings_and_cells_never_changes_the_plan(order: list[int], seed: int) -> None:
    w = _world(100, 20, 150_000 + 10_000 * seed, 3).usage(30_000, team="t2")
    fs = [idle(3), idle(2, team="t2"), finding("fast-mode", team="t1"),
          finding("auto-adoption", team="t1"), finding("auto-adoption", team="t2"),
          finding("fast-mode", team="t2"), idle(1, team="t3", unknown=1)]
    cells, pools = w.cells(), w.pools()
    base = plan_copilot(cells, pools, fs, PRICER, lines=w.lines, activity=w.activity,
                        month=w.month)
    shuffled = [fs[i] for i in order]
    got = plan_copilot(list(reversed(cells)), list(reversed(pools)), shuffled, PRICER,
                       lines=list(reversed(w.lines)), activity=list(reversed(w.activity)),
                       month=w.month)
    assert got == base


# ---------- fuzz: the parsers CP-PLAN owns ----------

_KEYS = st.one_of(
    st.sampled_from(["removable", "n_removable", "team_assigned_seats", "auto", "idle", "n",
                     "count", "seats", "unknown", "assignment", "class", "plan", "bucket",
                     "enterprise", "downgradable", "kind", "team", "Removable-Seats"]),
    st.text(min_size=0, max_size=12))
_VALUES = st.one_of(st.integers(-5, 2 * 10**9), st.booleans(),
                    st.sampled_from(["business", "enterprise", "unknown", "removable",
                                     "team_assigned", "none_90d", "31-90", ""]),
                    st.text(max_size=10))
_ATTRS = st.lists(st.tuples(_KEYS, _VALUES), max_size=8).map(tuple)


@SETTINGS
@given(items=st.lists(_ATTRS, max_size=5),
       dims=st.dictionaries(st.sampled_from(["plan", "bucket", "team", "org", "entity"]),
                            st.sampled_from(["business", "enterprise", "unknown", "31-90",
                                             "none_90d", "t1", "org-a", "enterprise"]),
                            max_size=4))
def test_fuzz_seat_counts_only_tokenbill_errors(items: list, dims: dict) -> None:
    try:
        f = finding("idle-seat", detector="copilot.seats-budgets",
                    evidence=tuple(EvidenceItem("aggregate", f"r{i}", a)
                                   for i, a in enumerate(items)), **dims)
    except TokenbillError:
        return
    try:
        got = seat_counts(f)
    except TokenbillError:
        return
    for name in ("removable", "team_assigned", "auto_assigned", "unknown", "total",
                 "enterprise"):
        assert getattr(got, name) >= 0
    assert got.plan in (None, "business", "enterprise", "unknown")


_KINDS = st.sampled_from(["idle-seat", "seat-auto-assign", "plan-mix", "auto-adoption",
                          "fast-mode", "premium-model-share", "larger-runner", "plan-status",
                          "review-cost", "unknown-kind"])


@SETTINGS
@given(specs=st.lists(st.tuples(_KINDS, _ATTRS,
                                 st.dictionaries(
                                     st.sampled_from(["plan", "bucket", "team", "org", "entity",
                                                      "model", "plan_scenario"]),
                                     st.sampled_from(["business", "enterprise", "unknown",
                                                      "none_90d", "t1", "org-a", "enterprise",
                                                      "org:org-a", "claude-opus-4-8",
                                                      "gpt-5.5", "cc:x"]),
                                     max_size=4)),
                      max_size=6),
       tradeoffs=st.booleans())
def test_fuzz_plan_over_hostile_findings(specs: list, tradeoffs: bool) -> None:
    w = _world(50, 10, 120_000, 2).usage(9_000, model="Claude Opus 4.8",
                                         output_tokens=3_000_000)
    w.actions("50")
    fs = []
    for kind, attrs, dims in specs:
        try:
            fs.append(finding(kind, detector="copilot.seats-budgets",
                              evidence=(EvidenceItem("aggregate", "x", attrs),), **dims))
        except TokenbillError:
            continue
    try:
        plan = plan_for(w, fs, include_tradeoffs=tradeoffs)
    except TokenbillError:
        return
    listed = [lv for lv in plan.levers if lv.basis is Basis.LIST]
    assert sum(lv.shapley.nano for lv in listed) == plan.joint_saving.nano
    assert all(lv.lever_id != AUTO or lv.needs_eval for lv in listed)
