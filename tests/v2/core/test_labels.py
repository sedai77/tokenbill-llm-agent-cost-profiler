"""SPEC §3.4 Figure: construction rules, combination rules, billed eligibility."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import (
    STRENGTH,
    Basis,
    Calibration,
    Evidence,
    Figure,
    Finality,
    add,
    estimated,
    exact,
    scale,
    sub,
    unpriced,
    zero,
)

L = Basis.LIST


def meas(nano: int, low: int, high: int, **kw: object) -> Figure:
    return Figure(
        nano=nano,
        evidence=Evidence.MEASURED,
        basis=L,
        low_nano=low,
        high_nano=high,
        ci_level_pct=95,
        **kw,
    )  # type: ignore[arg-type]


def test_enum_values() -> None:
    assert [e.value for e in Evidence] == ["exact", "estimated", "measured", "verified"]
    assert [b.value for b in Basis] == [
        "list",
        "contract",
        "invoice",
        "provider_estimate",
        "list_equivalent",
    ]
    assert [f.value for f in Finality] == ["provisional", "final", "n/a"]
    assert [c.value for c in Calibration] == ["calibrated", "uncalibrated", "n/a"]
    assert STRENGTH == {
        Evidence.EXACT: 3,
        Evidence.VERIFIED: 2,
        Evidence.MEASURED: 1,
        Evidence.ESTIMATED: 0,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        # unpriced needs an "unpriced:" note
        dict(nano=None, evidence=Evidence.EXACT, basis=L),
        dict(nano=None, evidence=Evidence.EXACT, basis=L, note="missing"),
        # low/high both or neither; low <= nano <= high
        dict(nano=5, evidence=Evidence.ESTIMATED, basis=L, low_nano=1, note="x"),
        dict(nano=5, evidence=Evidence.ESTIMATED, basis=L, high_nano=9, note="x"),
        dict(nano=5, evidence=Evidence.ESTIMATED, basis=L, low_nano=6, high_nano=9, note="x"),
        dict(nano=5, evidence=Evidence.ESTIMATED, basis=L, low_nano=1, high_nano=4, note="x"),
        dict(
            nano=None,
            evidence=Evidence.ESTIMATED,
            basis=L,
            low_nano=9,
            high_nano=1,
            note="unpriced: x",
        ),
        # EXACT: no range, calibration n/a, never an upper bound
        dict(nano=5, evidence=Evidence.EXACT, basis=L, low_nano=5, high_nano=5),
        dict(nano=5, evidence=Evidence.EXACT, basis=L, calibration=Calibration.CALIBRATED),
        dict(nano=5, evidence=Evidence.EXACT, basis=L, upper_bound=True),
        # ESTIMATED: calibration declared or a note naming the assumption
        dict(nano=5, evidence=Evidence.ESTIMATED, basis=L),
        # MEASURED / VERIFIED need a range and a CI level
        dict(nano=5, evidence=Evidence.MEASURED, basis=L, ci_level_pct=95),
        dict(nano=5, evidence=Evidence.VERIFIED, basis=L, low_nano=1, high_nano=9),
        # INVOICE basis requires EXACT
        dict(nano=5, evidence=Evidence.ESTIMATED, basis=Basis.INVOICE, note="x"),
        # types
        dict(nano=5.0, evidence=Evidence.EXACT, basis=L),
        dict(nano=True, evidence=Evidence.EXACT, basis=L),
        dict(nano=5, evidence="certain", basis=L),
        dict(nano=5, evidence=Evidence.EXACT, basis="guess"),
        dict(nano=5, evidence=Evidence.EXACT, basis=L, upper_bound=1),
        dict(nano=5, evidence=Evidence.EXACT, basis=L, note=None),
        dict(nano=5, evidence=Evidence.EXACT, basis=L, provenance="row"),
        dict(nano=5, evidence=Evidence.EXACT, basis=L, provenance=(1,)),
        dict(
            nano=5, evidence=Evidence.MEASURED, basis=L, low_nano=1, high_nano=9, ci_level_pct=100
        ),
    ],
)
def test_construction_rules_raise(kwargs: dict) -> None:
    with pytest.raises(ContractViolation):
        Figure(**kwargs)


def test_valid_constructions_and_coercion() -> None:
    f = Figure(nano=5, evidence="estimated", basis="list", note="assumed", provenance=["b", "a"])
    assert f.evidence is Evidence.ESTIMATED and f.basis is Basis.LIST and f.provenance == ("b", "a")
    assert Figure(
        nano=5, evidence=Evidence.ESTIMATED, basis=L, calibration=Calibration.UNCALIBRATED
    )
    assert meas(5, 1, 9).ci_level_pct == 95
    assert Figure(nano=5, evidence=Evidence.EXACT, basis=Basis.INVOICE).is_billed_eligible
    assert Figure(
        nano=None,
        evidence=Evidence.ESTIMATED,
        basis=L,
        low_nano=1,
        high_nano=2,
        note="unpriced: partial",
    )


def test_usd_and_billed_eligibility() -> None:
    assert exact(1_500_000, L).usd == Decimal("0.0015")
    assert unpriced("no rate row").usd is None
    assert exact(5, L).is_billed_eligible
    assert exact(5, Basis.CONTRACT).is_billed_eligible
    assert not exact(5, Basis.LIST_EQUIVALENT).is_billed_eligible
    assert not exact(5, Basis.PROVIDER_ESTIMATE).is_billed_eligible
    assert not estimated(5, L).is_billed_eligible
    assert not meas(5, 1, 9).is_billed_eligible


def test_helpers() -> None:
    assert exact(7, L, provenance=["r1"], finality=Finality.FINAL) == Figure(
        nano=7, evidence=Evidence.EXACT, basis=L, finality=Finality.FINAL, provenance=("r1",)
    )
    e = estimated(5, L, low=1, high=9, note="n", provenance=("x",))
    assert (e.low_nano, e.high_nano, e.calibration, e.note) == (1, 9, Calibration.UNCALIBRATED, "n")
    assert estimated(None, L, note="unpriced: x").nano is None
    u = unpriced("no rate row")
    assert u.nano is None and u.note == "unpriced: no rate row" and u.basis is L
    assert unpriced("unpriced: capabilities missing").note == "unpriced: capabilities missing"
    assert unpriced("x", Basis.LIST_EQUIVALENT).basis is Basis.LIST_EQUIVALENT
    z = zero(Basis.CONTRACT)
    assert z.nano == 0 and z.evidence is Evidence.EXACT and z.basis is Basis.CONTRACT


def test_add_rules() -> None:
    a, b = exact(10, L, provenance=["r2"]), exact(5, L, provenance=["r1", "r2"])
    s = add(a, b)
    assert s.nano == 15 and s.evidence is Evidence.EXACT and s.low_nano is None
    assert s.provenance == ("r1", "r2")
    e = estimated(3, L, low=1, high=8, note="ttl")
    t = add(a, e)
    assert (t.nano, t.low_nano, t.high_nano, t.evidence) == (13, 11, 18, Evidence.ESTIMATED)
    assert t.calibration is Calibration.UNCALIBRATED and t.note == "ttl"
    m = add(meas(5, 1, 9), exact(10, L))
    assert (m.evidence, m.low_nano, m.high_nano, m.ci_level_pct) == (Evidence.MEASURED, 11, 19, 95)
    v = Figure(
        nano=2,
        evidence=Evidence.VERIFIED,
        basis=L,
        low_nano=1,
        high_nano=3,
        ci_level_pct=90,
        calibration=Calibration.CALIBRATED,
    )
    mv = add(meas(5, 1, 9, calibration=Calibration.CALIBRATED), v)
    assert mv.evidence is Evidence.MEASURED and mv.ci_level_pct == 90
    assert mv.calibration is Calibration.CALIBRATED
    assert add(v, estimated(1, L, calibration=Calibration.CALIBRATED)).ci_level_pct is None
    with pytest.raises(ContractViolation):
        add(exact(1, L), exact(1, Basis.CONTRACT))
    with pytest.raises(ContractViolation):
        add(exact(1, L), exact(1, Basis.LIST_EQUIVALENT))
    with pytest.raises(ContractViolation):
        add(exact(1, L), 1)  # type: ignore[arg-type]


def test_add_propagates_unpriced() -> None:
    u = unpriced("no rate row")
    s = add(exact(5, L), u)
    assert s.nano is None and s.note == "unpriced: no rate row" and s.evidence is Evidence.EXACT
    s = add(u, estimated(5, L, low=1, high=9, note="x"))
    assert s.nano is None and s.low_nano is None and s.evidence is Evidence.ESTIMATED
    s = add(meas(5, 1, 9), u)
    assert s.nano is None and s.evidence is Evidence.ESTIMATED and s.ci_level_pct is None


def test_combination_metadata() -> None:
    p = exact(1, L, finality=Finality.PROVISIONAL)
    f = exact(1, L, finality=Finality.FINAL)
    assert add(p, f).finality is Finality.PROVISIONAL
    assert add(f, f).finality is Finality.FINAL
    assert add(f, exact(1, L)).finality is Finality.NA
    c = estimated(1, L, calibration=Calibration.CALIBRATED)
    assert add(c, exact(1, L)).calibration is Calibration.CALIBRATED
    assert add(c, estimated(1, L)).calibration is Calibration.UNCALIBRATED
    assert add(estimated(1, L, note="a; b"), estimated(1, L, note="b; c")).note == "a; b; c"
    assert add(estimated(1, L, note=""), estimated(1, L, note="b")).note == "b"
    assert add(estimated(1, L, upper_bound=True), exact(1, L)).upper_bound


def test_sub_crosswise() -> None:
    a = estimated(100, L, low=90, high=120, note="a")
    b = estimated(40, L, low=30, high=45, note="b")
    d = sub(a, b)
    assert (d.nano, d.low_nano, d.high_nano) == (60, 45, 90)
    d2 = sub(exact(10, L), exact(25, L))
    assert d2.nano == -15 and d2.evidence is Evidence.EXACT and d2.low_nano is None
    assert sub(exact(1, L), unpriced("x")).nano is None


def test_scale() -> None:
    f = estimated(100, L, low=50, high=150, note="x")
    s = scale(f, 30, 7)
    assert (s.nano, s.low_nano, s.high_nano) == (429, 214, 643)  # 428.57, 214.29, 642.86
    assert scale(exact(5, L), 1, 2).nano == 2  # 2.5 → 2 (half-even)
    assert scale(exact(7, L), 1, 2).nano == 4  # 3.5 → 4
    neg = scale(f, -1, 1)
    assert (neg.nano, neg.low_nano, neg.high_nano) == (-100, -150, -50)
    u = unpriced("x")
    assert scale(u, 3, 1) is u
    with pytest.raises(ContractViolation):
        scale(f, 1, 0)
    with pytest.raises(ContractViolation):
        scale(f, 1.5, 1)  # type: ignore[arg-type]


figs = st.builds(
    lambda n, w, v: estimated(n, L, low=n - w, high=n + v, note="x"),
    st.integers(-(10**12), 10**12),
    st.integers(0, 10**6),
    st.integers(0, 10**6),
)


@given(figs, figs, figs)
@settings(max_examples=100, deadline=None)
def test_add_is_associative_and_commutative(a: Figure, b: Figure, c: Figure) -> None:
    assert add(add(a, b), c) == add(a, add(b, c))
    assert add(a, b) == add(b, a)
    d = sub(a, b)
    assert d.low_nano <= d.nano <= d.high_nano  # type: ignore[operator]
