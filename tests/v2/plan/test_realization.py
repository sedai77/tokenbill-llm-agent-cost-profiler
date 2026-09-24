"""SPEC §11.2 steps 7–8: realization-rate priors, monthly projections, observed RR (D15)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokenbill.core.catalog import RR_PRIORS
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, estimated, exact
from tokenbill.core.types import ReceiptRow
from tokenbill.plan.realization import (
    MIN_RECEIPTS,
    PRIORS,
    TRAJECTORY_P50_NOTE,
    crosses_zero,
    monthly_nano,
    observed_rr,
    parse_observed_rr,
    prior_note,
    project,
    round_half_even,
)


def test_priors_are_the_spec_table_from_the_catalog() -> None:
    assert dict(PRIORS) == {
        "rate": (Decimal("1.0"), Decimal("1.0"), Decimal("1.0")),
        "cache_transform": (Decimal("0.8"), Decimal("0.9"), Decimal("1.0")),
        "trajectory": (Decimal("-0.2"), Decimal("0.5"), Decimal("1.0")),
    }
    assert RR_PRIORS["behavioral"] is None and "behavioral" not in PRIORS


def test_cache_transform_projection_point_and_range() -> None:
    fig = project(estimated(1_000_000, Basis.LIST, note="x"), "cache_transform", window_days=15,
                  upper_bound=False, calibration=Calibration.UNCALIBRATED)
    assert fig is not None
    # monthly = 2,000,000; × 0.8 / 0.9 / 1.0
    assert (fig.nano, fig.low_nano, fig.high_nano) == (1_800_000, 1_600_000, 2_000_000)
    assert fig.evidence is Evidence.ESTIMATED and fig.basis is Basis.LIST
    assert fig.calibration is Calibration.UNCALIBRATED and not fig.upper_bound
    assert "0.8/0.9/1" in fig.note and "monthly" in fig.note


def test_trajectory_projection_crosses_zero_and_is_labeled() -> None:
    fig = project(estimated(3_000, Basis.LIST, note="x"), "trajectory", window_days=30,
                  upper_bound=True, calibration=Calibration.CALIBRATED)
    assert fig is not None
    assert (fig.nano, fig.low_nano, fig.high_nano) == (1_500, -600, 3_000)
    assert crosses_zero(fig)
    assert "range crosses zero" in fig.note and TRAJECTORY_P50_NOTE in fig.note
    assert fig.upper_bound and fig.calibration is Calibration.CALIBRATED


def test_negative_credit_swaps_bounds() -> None:
    fig = project(estimated(-1_000, Basis.LIST, note="x"), "trajectory", window_days=30,
                  upper_bound=False, calibration=Calibration.UNCALIBRATED)
    assert fig is not None
    assert (fig.nano, fig.low_nano, fig.high_nano) == (-500, -1_000, 200)


def test_behavioral_is_never_projected_and_unknown_classes_raise() -> None:
    fig = estimated(1_000, Basis.LIST, note="x")
    assert project(fig, "behavioral", window_days=30, upper_bound=False,
                   calibration=Calibration.NA) is None
    with pytest.raises(UsageError):
        project(fig, "hygiene", window_days=30, upper_bound=False, calibration=Calibration.NA)
    with pytest.raises(UsageError):
        project(fig, "rate", window_days=0, upper_bound=False, calibration=Calibration.NA)
    with pytest.raises(UsageError):
        project(1_000, "rate", window_days=30, upper_bound=False,  # type: ignore[arg-type]
                calibration=Calibration.NA)
    with pytest.raises(UsageError):
        project(fig, "rate", window_days=30, upper_bound=False,
                calibration="bogus")  # type: ignore[arg-type]
    got = project(fig, "rate", window_days=30, upper_bound=False,
                  calibration="calibrated")  # type: ignore[arg-type]
    assert got is not None and got.calibration is Calibration.CALIBRATED


def test_unpriced_credit_gives_an_unpriced_projection() -> None:
    fig = estimated(None, Basis.LIST_EQUIVALENT, note="unpriced: some request")
    got = project(fig, "cache_transform", window_days=7, upper_bound=False,
                  calibration=Calibration.UNCALIBRATED)
    assert got is not None and got.nano is None and got.note.startswith("unpriced:")
    assert got.basis is Basis.LIST_EQUIVALENT
    plain = project(exact(5, Basis.LIST), "rate", window_days=30, upper_bound=False,
                    calibration=Calibration.NA)
    assert plain is not None and plain.nano == 5 and plain.evidence is Evidence.ESTIMATED


def test_a_ranged_input_widens_the_projection() -> None:
    fig = estimated(1_000, Basis.LIST, low=500, high=1_700, note="band", upper_bound=True)
    got = project(fig, "cache_transform", window_days=30, upper_bound=False,
                  calibration=Calibration.UNCALIBRATED)
    assert got is not None
    assert (got.nano, got.low_nano, got.high_nano) == (900, 400, 1_700)
    assert got.upper_bound


def test_rounding_is_half_even_and_exact() -> None:
    from fractions import Fraction

    assert round_half_even(Fraction(5, 2)) == 2
    assert round_half_even(Fraction(7, 2)) == 4
    assert round_half_even(Fraction(-5, 2)) == -2
    assert round_half_even(Fraction(-7, 2)) == -4
    assert monthly_nano(7, 3) == Fraction(70)
    with pytest.raises(UsageError):
        monthly_nano(7, -1)
    assert prior_note("behavioral") == "behavioral lever: not projected"


def _receipt(i: int, cls: str, rr: str | None) -> ReceiptRow:
    return ReceiptRow(receipt_id=f"r{i}", lever_id="cc.prompt_cache_ttl.main", lever_class=cls,
                      label="measured", realization_rate=rr, created_ms=i, json="{}", dsse=None)


def test_observed_rr_needs_three_receipts_per_class() -> None:
    rows = [_receipt(1, "cache_transform", "0.70"), _receipt(2, "cache_transform", "0.8"),
            _receipt(3, "cache_transform", "0.95"), _receipt(4, "rate", "1.0"),
            _receipt(5, "rate", "0.9"), _receipt(6, "rate", None),
            _receipt(7, "trajectory", "bogus"), _receipt(8, "cache_transform", "1e3")]
    assert observed_rr(rows) == {"cache_transform": ("0.8167", 3)}
    assert observed_rr(rows, min_receipts=2) == {"cache_transform": ("0.8167", 3),
                                                 "rate": ("0.95", 2)}
    assert MIN_RECEIPTS == 3
    with pytest.raises(UsageError):
        observed_rr(rows, min_receipts=0)
    with pytest.raises(UsageError):
        observed_rr(["x"])  # type: ignore[list-item]


def test_parse_observed_rr_keeps_classes_with_three_receipts() -> None:
    assert parse_observed_rr(None) == ()
    assert parse_observed_rr({"rate": ("1.0", 2), "cache_transform": ("0.72", 3),
                              "trajectory": ("-0.1", 4)}) == (
        ("cache_transform", "0.72", 3), ("trajectory", "-0.1", 4))
    for bad in (["x"], {"rate": "1"}, {"rate": ("x", 3)}, {"rate": ("1", -1)},
                {"": ("1", 3)}, {"rate": ("1", True)}):
        with pytest.raises(UsageError):
            parse_observed_rr(bad)  # type: ignore[arg-type]
