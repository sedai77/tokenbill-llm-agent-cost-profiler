"""verify.its: event-study ITS with HAC errors and a placebo date (SPEC §13.3)."""

from __future__ import annotations

import pytest

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Evidence
from tokenbill.core.types import GuardResult
from tokenbill.verify import its as I
from tokenbill.verify.label_policy import decide

from .panelgen import day, org_series


# Fixed seeds: the placebo is a 5%-level test, so some seeds fail it by chance (seeds 2 and 4 do,
# even with i.i.d. noise); its calibration is pinned separately below.
@pytest.mark.parametrize("seed", [0, 1, 3, 5, 6])
def test_org_wide_20pct_level_shift(seed: int) -> None:
    """Acceptance: estimate within ±5% of the true shift, placebo passes, label MEASURED (never
    VERIFIED, even when claimed randomized)."""
    series, truth = org_series(seed=seed)
    delta, lo, hi, placebo = I.event_study_its(series, change_date=day(90),
                                               placebo_date=day(45))
    assert abs(delta - truth) <= 0.05 * abs(truth)
    assert lo <= delta <= hi < 0
    assert placebo
    guards = (GuardResult("placebo", placebo, "includes 0", "placebo CI includes 0"),)
    label = decide(design="its", randomized=True, assignment_hash_matches=True, guards=guards,
                   ci=(lo, hi))
    assert label is Evidence.MEASURED


def test_placebo_and_interval_calibration_over_100_series() -> None:
    """Under AR(1) 0.3 noise the placebo falsely fails ~9% of series (nominal 5%; small-sample
    HAC, README) and the 95% CI on the shift covers the truth ≥ 90% of the time."""
    false_fail = covered = 0
    for seed in range(100):
        series, truth = org_series(seed=seed)
        _, lo, hi, placebo = I.event_study_its(series, change_date=day(90),
                                               placebo_date=day(45))
        false_fail += not placebo
        covered += lo <= truth <= hi
    assert false_fail <= 12
    assert covered >= 90


def test_planted_pre_trend_fails_the_placebo() -> None:
    """Acceptance: a gradual pre-change drift fails the placebo; the label is then not a
    measurement (never VERIFIED)."""
    for seed in range(5):
        series, _ = org_series(seed=seed, pre_ramp=(40, 10, -0.06))
        _, lo, hi, placebo = I.event_study_its(series, change_date=day(90),
                                               placebo_date=day(45))
        assert not placebo
        guards = (GuardResult("placebo", placebo, "excludes 0", "placebo CI includes 0"),)
        assert decide(design="its", randomized=True, assignment_hash_matches=True,
                      guards=guards, ci=(lo, hi)) is Evidence.ESTIMATED


def test_linear_trend_is_modelled_not_a_placebo_failure() -> None:
    series, truth = org_series(seed=7, trend=0.0005)
    delta, _, _, placebo = I.event_study_its(series, change_date=day(90), placebo_date=day(45))
    assert placebo
    assert abs(delta - truth) <= 0.05 * abs(truth)


def test_no_change_gives_an_interval_around_zero() -> None:
    series, _ = org_series(seed=8, shift=0.0)
    _, lo, hi, _ = I.event_study_its(series, change_date=day(90), placebo_date=day(45))
    assert lo <= 0 <= hi


def test_fixed_b_critical_value() -> None:
    assert I.fixed_b_critical_value(0.0) == pytest.approx(1.96)
    assert I.fixed_b_critical_value(0.1) == pytest.approx(1.96 + 0.29694 + 0.00416 - 0.0005324)
    with pytest.raises(UsageError):
        I.fixed_b_critical_value(1.5)


def test_series_aggregation_and_point() -> None:
    series, _ = org_series(seed=9)
    doubled = [(d, c // 2, n // 2) for d, c, n in series] + \
        [(d, c - c // 2, n - n // 2) for d, c, n in series] + [("2026-01-01", 5, 0)]
    assert I.series_dates(doubled) == I.series_dates(series)
    a = I.its_point(series, change_date=day(90))
    b = I.its_point(doubled, change_date=day(90))
    assert a == pytest.approx(b, rel=1e-6)
    with pytest.raises(UsageError):
        I.its_point([], change_date=day(90))


def test_input_validation() -> None:
    series, _ = org_series(seed=10)
    bad_calls = [
        lambda: I.event_study_its(series, change_date=day(90), placebo_date=day(95)),
        lambda: I.event_study_its(series, change_date=day(3), placebo_date=day(1)),
        lambda: I.event_study_its(series, change_date=day(90), placebo_date=day(2)),
        lambda: I.event_study_its(series, change_date="x", placebo_date=day(2)),
        lambda: I.event_study_its(series, change_date=day(90), placebo_date="y"),
        lambda: I.event_study_its(series, change_date=day(90), placebo_date=day(45), hac_lag=-1),
        lambda: I.event_study_its([], change_date=day(90), placebo_date=day(45)),
        lambda: I.event_study_its([("2026-06-01", 1.5, 3)], change_date=day(90),  # type: ignore
                                  placebo_date=day(45)),
        lambda: I.event_study_its([("bad", 1, 3)], change_date=day(90), placebo_date=day(45)),
        lambda: I.event_study_its([("2026-06-01", 1)], change_date=day(90),  # type: ignore
                                  placebo_date=day(45)),
        lambda: I.event_study_its(series[:20], change_date=day(12), placebo_date=day(6)),
    ]
    for call in bad_calls:
        with pytest.raises(UsageError):
            call()
