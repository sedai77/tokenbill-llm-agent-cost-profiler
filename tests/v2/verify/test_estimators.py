"""verify.estimators: imputation DiD, CUPED cluster DIM, placebos, quality bound (SPEC §13.3)."""

from __future__ import annotations

import dataclasses
import statistics

import pytest

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.types import PanelRow
from tokenbill.verify import estimators as E
from tokenbill.verify.panel import rate_variance

from .panelgen import rollout_panel


def _check_recovery(seeds: range, boot: int) -> None:
    covered = 0
    for seed in seeds:
        g = rollout_panel(seed=seed)
        att, lo, hi = E.imputation_did(g.rows, boot=boot, seed=seed)
        assert abs(att - g.truth_att) <= 0.05 * abs(g.truth_att), seed
        assert att / g.truth_base == pytest.approx(-0.25, abs=0.0125), seed
        covered += lo <= g.truth_att <= hi
    assert covered >= 0.9 * len(seeds)


def test_stepped_wedge_25pct_effect_recovered_10_seeds() -> None:
    """Acceptance (PR CI): known 25% effect, estimate within ±5% of truth, 95% CI coverage ≥ 90%."""
    _check_recovery(range(10), boot=300)


@pytest.mark.slow
def test_stepped_wedge_25pct_effect_recovered_50_seeds() -> None:
    """Acceptance (slow): the same over 50 seeds."""
    _check_recovery(range(50), boot=300)


def test_imputation_is_deterministic_and_seed_scoped() -> None:
    g = rollout_panel(seed=3)
    a = E.imputation_did(g.rows, boot=50, seed=1)
    assert a == E.imputation_did(list(reversed(g.rows)), boot=50, seed=1)
    b = E.imputation_did(g.rows, boot=50, seed=2)
    assert a[0] == b[0] and a[1:] != b[1:]
    assert a[1] <= a[0] <= a[2]


def test_washout_days_are_excluded() -> None:
    g = rollout_panel(seed=4, washout_days=3)
    res0 = E._did(g.rows, metric="cost", washout_days=0, boot=0, seed=0, scope="t")
    res3 = E._did(g.rows, metric="cost", washout_days=3, boot=0, seed=0, scope="t")
    assert res3.n_treated_cells < res0.n_treated_cells
    assert abs(res3.att - g.truth_att) <= 0.05 * abs(g.truth_att)
    with pytest.raises(UsageError):
        E.imputation_did(g.rows, washout_days=-1)


def test_simultaneous_price_cut_leaves_the_constant_price_estimate_unchanged() -> None:
    """Acceptance: a 20% price cut at the first adoption changes only cost_actual; the estimate
    at the baseline card is identical and the price effect is an EXACT rate variance."""
    plain = rollout_panel(seed=5)
    cut = rollout_panel(seed=5, price_cut=0.20)
    assert [r.cost_baseline_nano for r in plain.rows] == [r.cost_baseline_nano for r in cut.rows]
    assert E.imputation_did(plain.rows, boot=100, seed=0) == \
        E.imputation_did(cut.rows, boot=100, seed=0)
    rv = rate_variance(cut.rows)
    assert rv.evidence is Evidence.EXACT and rv.basis is Basis.LIST
    post = sum(r.cost_baseline_nano for r in cut.rows if r.date_utc >= cut.first_start)
    assert rv.nano == pytest.approx(-0.20 * post, rel=1e-6)
    assert rate_variance(plain.rows).nano == 0


def test_imputation_errors() -> None:
    g = rollout_panel(seed=6)
    untreated = [dataclasses.replace(r, treated=False) for r in g.rows]
    with pytest.raises(UsageError):
        E.imputation_did(untreated)
    all_treated = [dataclasses.replace(r, treated=True) for r in g.rows]
    with pytest.raises(UsageError):
        E.imputation_did(all_treated)
    with pytest.raises(UsageError):
        E.imputation_did(g.rows, boot=0)
    with pytest.raises(UsageError):
        E.imputation_did([object()])  # type: ignore[list-item]
    # treated from the first day: no untreated day to identify its fixed effect
    first_day = [dataclasses.replace(r, treated=r.cluster_id != "c00" or r.treated)
                 for r in g.rows if r.cluster_id in ("c00",)]
    with pytest.raises(UsageError):
        E.imputation_did(first_day)


def test_duplicate_rows_are_summed_and_zero_devday_rows_skipped() -> None:
    g = rollout_panel(seed=7)
    split = []
    for r in g.rows:
        half = r.cost_baseline_nano // 2
        split.append(dataclasses.replace(r, cost_baseline_nano=half,
                                         active_dev_days=r.active_dev_days // 2))
        split.append(dataclasses.replace(r, cost_baseline_nano=r.cost_baseline_nano - half,
                                         active_dev_days=r.active_dev_days
                                         - r.active_dev_days // 2))
    split.append(PanelRow("c00", "2026-06-01", 999, 999, 0, None, None, False))
    a = E._did(g.rows, metric="cost", washout_days=0, boot=0, seed=0, scope="t").att
    b = E._did(split, metric="cost", washout_days=0, boot=0, seed=0, scope="t").att
    assert b == pytest.approx(a, rel=1e-9)


def test_alternating_projections_converge() -> None:
    g = rollout_panel(seed=8)
    tw = E._TwoWay(E._merged_rows(g.rows), "cost", 0)
    alpha, lam = tw.fit()
    assert 1 < tw.iterations < E.AP_MAX_ITER
    # the normal equations hold on untreated cells: weighted residuals sum to ~0 per cluster
    for c, ts in enumerate(tw.c_t):
        if not ts:
            continue
        fitted = sum(w * (alpha[c] + lam[t]) for t, w in zip(ts, tw.c_w[c], strict=True))
        assert abs(tw.c_swy[c] - fitted) <= 1e-6 * tw.c_swy[c]


def test_cuped_reduces_variance_on_a_correlated_pre_period() -> None:
    """Acceptance: CUPED's across-seed spread is far below the unadjusted difference in means."""
    adjusted, raw = [], []
    for seed in range(12):
        g = rollout_panel(seed=seed, design="cluster_rct", clusters=40)
        kw = {"pre_until": g.first_start, "metric": "cost", "boot": 0, "seed": 0, "scope": "t"}
        adjusted.append(E._cuped(g.rows, **kw).att / g.truth_base)
        raw.append(E._cuped(g.rows, adjust=False, **kw).att / g.truth_base)
    assert statistics.pstdev(adjusted) < 0.5 * statistics.pstdev(raw)
    assert statistics.fmean(adjusted) == pytest.approx(-0.25, abs=0.05)


def test_cuped_cluster_dim_ci_and_errors() -> None:
    g = rollout_panel(seed=9, design="cluster_rct", clusters=40)
    att, lo, hi = E.cuped_cluster_dim(g.rows, pre_until=g.first_start, boot=300, seed=1)
    assert lo <= att <= hi < 0
    # CUPED targets the population ATT; with an effect proportional to cluster level the
    # in-sample truth can sit just outside its interval, so check accuracy instead
    assert att / g.truth_base == pytest.approx(-0.25, abs=0.05)
    assert (att, lo, hi) == E.cuped_cluster_dim(g.rows, pre_until=g.first_start, boot=300, seed=1)
    with pytest.raises(UsageError):
        E.cuped_cluster_dim(g.rows, pre_until="not-a-date")
    with pytest.raises(UsageError):
        E.cuped_cluster_dim(g.rows, pre_until=g.first_start, boot=0)
    only_control = [r for r in g.rows if r.cluster_id in g.holdback]
    with pytest.raises(UsageError):
        E.cuped_cluster_dim(only_control, pre_until=g.first_start, boot=10)


def test_cuped_with_new_clusters_uses_a_missing_pre_indicator() -> None:
    g = rollout_panel(seed=10, design="cluster_rct", clusters=40)
    new = {"c01", "c02", "c03", "c04", "c05"}
    rows = [r for r in g.rows if not (r.cluster_id in new and r.date_utc < g.first_start)]
    att, lo, hi = E.cuped_cluster_dim(rows, pre_until=g.first_start, boot=200, seed=0)
    assert lo <= att <= hi
    assert att / g.truth_base == pytest.approx(-0.25, abs=0.08)
    units = E._cuped_units(rows, g.first_start, "cost")
    assert sum(1 for u in units if u.x is None) == len(new)
    # every cluster new: no covariate at all, a plain difference in means
    post = [r for r in g.rows if r.date_utc >= g.first_start]
    assert E._cuped_theta(E._cuped_units(post, g.first_start, "cost"),
                          [1] * 40)[:2] == (0.0, 0.0)


def test_placebo_passes_on_clean_and_fails_on_planted_pre_trend() -> None:
    """Acceptance: placebo with a planted pre-trend fails (CI excludes 0)."""
    clean = rollout_panel(seed=11, pre_weeks=4, weeks=12)
    _, lo, hi = E.placebo_did(clean.rows, boot=300, seed=0)
    assert lo <= 0 <= hi
    trend = rollout_panel(seed=11, pre_weeks=4, weeks=12, pre_trend=0.6)
    _, lo, hi = E.placebo_did(trend.rows, boot=300, seed=0)
    assert not lo <= 0 <= hi
    rct = rollout_panel(seed=12, design="cluster_rct", clusters=40, pre_weeks=4)
    _, lo, hi = E.placebo_cuped(rct.rows, pre_until=rct.first_start, boot=300, seed=0)
    assert lo <= 0 <= hi
    rct_trend = rollout_panel(seed=12, design="cluster_rct", clusters=40, pre_weeks=4,
                              pre_trend=0.8)
    _, lo, hi = E.placebo_cuped(rct_trend.rows, pre_until=rct.first_start, boot=300, seed=0)
    assert not lo <= 0 <= hi


def test_placebo_errors() -> None:
    g = rollout_panel(seed=13)
    with pytest.raises(UsageError):
        E.placebo_did([dataclasses.replace(r, treated=False) for r in g.rows])
    short = [r for r in g.rows if r.date_utc >= "2026-06-12"]
    with pytest.raises(UsageError):
        E.placebo_did(short, boot=10)
    with pytest.raises(UsageError):
        E.pre_midpoint(["2026-06-01", "2026-06-02"])


def test_quality_lower_bound() -> None:
    none = rollout_panel(seed=14)
    assert E.quality_lower_bound(none.rows, design="stepped_wedge") is None
    ok = rollout_panel(seed=14, prs=True, pr_effect=0.0)
    bound = E.quality_lower_bound(ok.rows, design="stepped_wedge", boot=200, seed=0)
    assert bound is not None and bound > -0.05
    bad = rollout_panel(seed=14, prs=True, pr_effect=-0.20)
    bound = E.quality_lower_bound(bad.rows, design="stepped_wedge", boot=200, seed=0)
    assert bound is not None and bound < -0.05
    rct = rollout_panel(seed=15, design="cluster_rct", clusters=40, prs=True, pr_effect=-0.25)
    bound = E.quality_lower_bound(rct.rows, design="cluster_rct", pre_until=rct.first_start,
                                  boot=200, seed=0)
    assert bound is not None and bound < -0.05
    with pytest.raises(UsageError):
        E.quality_lower_bound(rct.rows, design="cluster_rct")
    with pytest.raises(UsageError):
        E.quality_lower_bound(rct.rows, design="its")


def test_point_estimate_and_helpers() -> None:
    g = rollout_panel(seed=16)
    p = E.point_estimate(g.rows, design="stepped_wedge")
    assert round(p) == E.imputation_did(g.rows, boot=10)[0]
    rct = rollout_panel(seed=16, design="cluster_rct")
    assert E.point_estimate(rct.rows, design="cluster_rct", pre_until=rct.first_start) < 0
    with pytest.raises(UsageError):
        E.point_estimate(rct.rows, design="cluster_rct")
    with pytest.raises(UsageError):
        E.point_estimate(rct.rows, design="its")
    assert E.first_treated_date(g.rows) == g.first_start
    assert E.first_treated_date([dataclasses.replace(r, treated=False) for r in g.rows]) is None
    assert E.to_nano_triple(5.4, 6.0, 9.0) == (5, 5, 9)
    with pytest.raises(UsageError):
        E.to_nano_triple(float("nan"), 0.0, 1.0)
    res = E.BootResult(att=1.0, base=0.0, draws=((1.0, 0.0),), n_units=1, n_treated_cells=1)
    with pytest.raises(UsageError):
        res.relative_lower()
