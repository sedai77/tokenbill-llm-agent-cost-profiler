"""verify.stats: bootstrap, CUPED, χ² via the regularized gamma function, Wilson, OLS + HAC."""

from __future__ import annotations

import math
import statistics

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.common import rng
from tokenbill.core.errors import UsageError
from tokenbill.verify import stats as S


def test_mean_weighted_percentile() -> None:
    assert S.mean([1.0, 2.0, 3.0]) == 2.0
    assert S.weighted_mean([1.0, 3.0], [3.0, 1.0]) == 1.5
    assert S.percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.5
    assert S.percentile([1.0], 0.9) == 1.0
    assert S.percentile([0.0, 10.0], 0.25) == 2.5
    for bad in (lambda: S.mean([]), lambda: S.weighted_mean([1.0], [0.0]),
                lambda: S.weighted_mean([1.0], [1.0, 2.0]), lambda: S.percentile([], 0.5),
                lambda: S.percentile([1.0], 1.5)):
        with pytest.raises(UsageError):
            bad()
    assert S.normal_quantile(0.975) == pytest.approx(1.959964, abs=1e-6)


def test_bootstrap_ci_is_seeded_and_covers_the_mean() -> None:
    rnd = rng(1, "t")
    xs = [rnd.gauss(10.0, 2.0) for _ in range(200)]
    a = S.bootstrap_ci(xs, boot=500, seed=3)
    assert a == S.bootstrap_ci(xs, boot=500, seed=3)
    assert a != S.bootstrap_ci(xs, boot=500, seed=4)
    point, lo, hi = a
    assert lo < point < hi
    se = statistics.stdev(xs) / math.sqrt(len(xs))
    assert hi - lo == pytest.approx(2 * 1.96 * se, rel=0.2)
    with pytest.raises(UsageError):
        S.bootstrap_ci([], boot=10)
    with pytest.raises(UsageError):
        S.bootstrap_ci([1.0], boot=0)
    with pytest.raises(UsageError):
        S.bootstrap_ci([1.0, 2.0], boot=5, level=1.0)


def test_cluster_bootstrap_keeps_strata_sizes() -> None:
    units = [("t", 1.0), ("t", 2.0), ("c", 5.0), ("c", 7.0), ("c", 9.0)]
    seen: list[tuple[int, int]] = []

    def stat(sample):
        seen.append((sum(1 for s, _ in sample if s == "t"), sum(1 for s, _ in sample if s == "c")))
        return sum(v for _, v in sample)

    point, lo, hi = S.cluster_bootstrap_ci(units, stat, strata=[s for s, _ in units], boot=50,
                                           seed=0)
    assert point == 24.0 and lo <= hi
    assert set(seen) == {(2, 3)}
    counts = S.resample_counts(["b", "a", "b"], rng(0, "x"))
    assert sum(counts) == 3 and counts[1] == 1
    with pytest.raises(UsageError):
        S.cluster_bootstrap_ci([], stat)
    with pytest.raises(UsageError):
        S.cluster_bootstrap_ci(units, stat, strata=["a"])


def test_cuped_reduces_variance_on_a_correlated_covariate() -> None:
    rnd = rng(2, "cuped")
    x = [rnd.gauss(100.0, 20.0) for _ in range(500)]
    y = [0.9 * xi + rnd.gauss(0.0, 5.0) for xi in x]
    adj, theta = S.cuped(y, x)
    assert theta == pytest.approx(0.9, abs=0.05)
    assert statistics.pvariance(adj) < 0.2 * statistics.pvariance(y)
    assert S.mean(adj) == pytest.approx(S.mean(y))
    assert S.cuped([1.0, 2.0], [3.0, 3.0]) == ([1.0, 2.0], 0.0)
    assert S.cuped([], []) == ([], 0.0)
    with pytest.raises(UsageError):
        S.cuped([1.0], [])


def test_regularized_gamma_and_chi2() -> None:
    for x in (0.1, 1.0, 3.0, 20.0):
        assert S.gammaincc(1.0, x) == pytest.approx(math.exp(-x), rel=1e-10)
        assert S.gammaincc(0.5, x) == pytest.approx(math.erfc(math.sqrt(x)), rel=1e-9)
    assert S.gammaincc(2.0, 0.0) == 1.0
    assert S.gammaincc(2.0, math.inf) == 0.0
    assert S.chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-9)
    assert S.chi2_sf(5.991464547107979, 2) == pytest.approx(0.05, abs=1e-9)
    assert S.chi2_sf(10.828, 1) == pytest.approx(0.001, rel=1e-3)
    assert S.chi2_sf(0.0, 3) == 1.0
    for bad in (lambda: S.gammaincc(0.0, 1.0), lambda: S.gammaincc(1.0, -1.0),
                lambda: S.chi2_sf(1.0, 0)):
        with pytest.raises(UsageError):
            bad()


def test_srm_pvalue() -> None:
    # 60/40 realized vs 50/50 planned over 10,000 developer-days: a glaring mismatch
    assert S.srm_pvalue([6000, 4000], [1, 1]) < 1e-80
    assert S.srm_pvalue([5020, 4980], [0.5, 0.5]) > 0.5
    assert S.srm_pvalue([250, 750], [0.25, 0.75]) == pytest.approx(1.0)
    assert S.srm_pvalue([0, 10], [0.0, 1.0]) == 1.0
    assert S.srm_pvalue([1, 10], [0.0, 1.0]) == 0.0
    for bad in (lambda: S.srm_pvalue([1], [1]), lambda: S.srm_pvalue([1, -1], [1, 1]),
                lambda: S.srm_pvalue([0, 0], [1, 1]), lambda: S.srm_pvalue([1, 2], [1])):
        with pytest.raises(UsageError):
            bad()


def test_wilson() -> None:
    lo, hi = S.wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4) and hi == pytest.approx(0.9433, abs=1e-4)
    assert S.wilson(0, 0) == (0.0, 1.0)
    lo, hi = S.wilson(0, 20)
    assert lo == 0.0 and 0.0 < hi < 0.2
    assert S.wilson(20, 20)[1] == 1.0
    with pytest.raises(UsageError):
        S.wilson(3, 2)


def test_ols_and_hac() -> None:
    x = [[1.0, float(t)] for t in range(20)]
    y = [3.0 + 2.0 * t for t in range(20)]
    beta, resid, inv = S.ols(x, y)
    assert beta == pytest.approx([3.0, 2.0])
    assert max(abs(r) for r in resid) < 1e-9
    rnd = rng(5, "hac")
    y2 = [3.0 + 2.0 * t + rnd.gauss(0, 1) for t in range(20)]
    b2, r2, inv2 = S.ols(x, y2)
    hc0 = S.newey_west_se(x, r2, lag=0, xtx_inv=inv2)
    # lag 0 is White's HC0: (X'X)^-1 Σ e² x x' (X'X)^-1
    meat = [[sum(e * e * row[i] * row[j] for row, e in zip(x, r2, strict=True))
             for j in range(2)] for i in range(2)]
    v00 = sum(inv2[0][a] * meat[a][b] * inv2[b][0] for a in range(2) for b in range(2))
    assert hc0[0] == pytest.approx(math.sqrt(v00))
    assert len(S.newey_west_se(x, r2, lag=7)) == 2
    with pytest.raises(UsageError):
        S.ols([[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]], [1.0, 2.0, 3.0])   # singular
    with pytest.raises(UsageError):
        S.ols([[1.0, 2.0]], [1.0])
    with pytest.raises(UsageError):
        S.newey_west_se(x, r2, lag=-1)
    with pytest.raises(UsageError):
        S.newey_west_se([], [], lag=1)
    with pytest.raises(UsageError):
        S.mat_inv([[1.0, 2.0]])
    with pytest.raises(UsageError):
        S.mat_inv([[0.0, 0.0], [0.0, 0.0]])
    assert S.mat_inv([[2.0, 0.0], [0.0, 4.0]]) == [[0.5, 0.0], [0.0, 0.25]]


@settings(max_examples=150, deadline=None)
@given(st.lists(st.integers(0, 10_000), min_size=2, max_size=6),
       st.lists(st.integers(1, 100), min_size=6, max_size=6))
def test_srm_pvalue_is_a_probability(observed: list[int], shares: list[int]) -> None:
    try:
        p = S.srm_pvalue(observed, shares[:len(observed)])
    except UsageError:
        return
    assert 0.0 <= p <= 1.0


@settings(max_examples=150, deadline=None)
@given(st.floats(0.05, 50.0), st.floats(0.0, 200.0), st.floats(0.0, 200.0))
def test_gammaincc_is_monotone_in_x(a: float, x1: float, x2: float) -> None:
    lo, hi = sorted((x1, x2))
    q_lo, q_hi = S.gammaincc(a, lo), S.gammaincc(a, hi)
    assert 0.0 <= q_hi <= q_lo + 1e-12 <= 1.0 + 1e-12


@settings(max_examples=150, deadline=None)
@given(st.integers(0, 500), st.integers(0, 500))
def test_wilson_contains_the_proportion(k: int, extra: int) -> None:
    n = k + extra
    lo, hi = S.wilson(k, n)
    assert 0.0 <= lo <= hi <= 1.0
    if n:
        assert lo - 1e-12 <= k / n <= hi + 1e-12
