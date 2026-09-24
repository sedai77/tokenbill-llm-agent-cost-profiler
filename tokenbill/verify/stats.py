"""Statistics for verification (SPEC §13.3, D15): stdlib only, floats inside.

Everything here is plain numerical code; callers convert results to int nano and label them
(``verify.label_policy``). Randomness only through :func:`tokenbill.common.rng`, so the same seed
gives the same bootstrap draws in every process.

* :func:`bootstrap_ci` — percentile bootstrap of a statistic over i.i.d. values.
* :func:`cluster_bootstrap_ci` — percentile bootstrap resampling whole clusters (optionally within
  strata, e.g. by arm); :func:`resample_counts` exposes the draws as per-unit multiplicities so an
  estimator can reweight instead of copying data.
* :func:`cuped` — CUPED covariate adjustment ``Ỹ = Y − θ(X − mean X)``, ``θ = cov(Y, X)/var(X)``.
* :func:`srm_pvalue` — sample-ratio-mismatch χ² goodness of fit; the tail probability uses the
  regularized upper incomplete gamma function (:func:`gammaincc`).
* :func:`wilson` — Wilson score interval for a proportion.
* :func:`ols` and :func:`newey_west_se` — least squares with Newey–West (Bartlett, HAC) standard
  errors.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from statistics import NormalDist
from typing import TypeVar

from tokenbill.common import rng
from tokenbill.core.errors import UsageError

__all__ = [
    "Z95",
    "bootstrap_ci",
    "chi2_sf",
    "cluster_bootstrap_ci",
    "cuped",
    "gammaincc",
    "mat_inv",
    "mean",
    "newey_west_se",
    "normal_quantile",
    "ols",
    "percentile",
    "resample_counts",
    "srm_pvalue",
    "weighted_mean",
    "wilson",
]

T = TypeVar("T")

#: Two-sided 95% normal critical value.
Z95 = NormalDist().inv_cdf(0.975)

_EPS = 1e-300
_GAMMA_ITMAX = 10_000
_GAMMA_TOL = 1e-15


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean (``UsageError`` when empty)."""
    if not values:
        raise UsageError("mean of an empty sequence")
    return math.fsum(values) / len(values)


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    """``Σ w·v / Σ w`` (``UsageError`` when the weights sum to zero or lengths differ)."""
    if len(values) != len(weights):
        raise UsageError("weighted_mean: values and weights differ in length")
    total = math.fsum(weights)
    if total <= 0:
        raise UsageError("weighted_mean: weights sum to zero")
    return math.fsum(v * w for v, w in zip(values, weights, strict=True)) / total


def normal_quantile(p: float) -> float:
    """The standard normal quantile of *p* (``0 < p < 1``)."""
    return NormalDist().inv_cdf(p)


def percentile(values: Sequence[float], q: float) -> float:
    """The *q*-quantile (``0 ≤ q ≤ 1``) with linear interpolation between order statistics
    (Hyndman–Fan type 7, numpy's default)."""
    if not values:
        raise UsageError("percentile of an empty sequence")
    if not 0.0 <= q <= 1.0:
        raise UsageError("percentile: q must be in [0, 1]")
    xs = sorted(values)
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def _level_bounds(draws: Sequence[float], level: float) -> tuple[float, float]:
    if not 0.0 < level < 1.0:
        raise UsageError("confidence level must be in (0, 1)")
    tail = (1.0 - level) / 2.0
    return percentile(draws, tail), percentile(draws, 1.0 - tail)


def _check_boot(boot: int) -> None:
    if type(boot) is not int or boot < 1:
        raise UsageError("boot must be a positive int")


def bootstrap_ci(values: Sequence[float], stat: Callable[[Sequence[float]], float] = mean, *,
                 boot: int = 2000, seed: int = 0, level: float = 0.95,
                 scope: str = "bootstrap") -> tuple[float, float, float]:
    """Percentile bootstrap of ``stat(values)`` over i.i.d. *values*: ``(point, low, high)``.

    Draws come from ``common.rng(seed, "verify.stats", scope)``.
    """
    if not values:
        raise UsageError("bootstrap_ci of an empty sequence")
    _check_boot(boot)
    point = stat(values)
    rnd = rng(seed, "verify.stats", scope)
    n = len(values)
    draws = []
    for _ in range(boot):
        sample = [values[rnd.randrange(n)] for _ in range(n)]
        draws.append(stat(sample))
    lo, hi = _level_bounds(draws, level)
    return point, lo, hi


def resample_counts(strata: Sequence[str], rnd: random.Random) -> list[int]:
    """One cluster-bootstrap draw as per-unit multiplicities: within each stratum, draw as many
    units as the stratum has, with replacement. Strata are visited in sorted order so the draw is
    independent of how the caller ordered its units."""
    groups: dict[str, list[int]] = {}
    for i, s in enumerate(strata):
        groups.setdefault(s, []).append(i)
    counts = [0] * len(strata)
    for key in sorted(groups):
        members = groups[key]
        m = len(members)
        for _ in range(m):
            counts[members[rnd.randrange(m)]] += 1
    return counts


def cluster_bootstrap_ci(units: Sequence[T], stat: Callable[[Sequence[T]], float], *,
                         strata: Sequence[str] | None = None, boot: int = 2000, seed: int = 0,
                         level: float = 0.95,
                         scope: str = "cluster") -> tuple[float, float, float]:
    """Percentile cluster bootstrap: ``(stat(units), low, high)``.

    Each replicate resamples whole *units* (clusters) with replacement — within *strata* when
    given (e.g. the arm of each cluster), so every replicate keeps each stratum's size — and
    evaluates *stat* on the resampled list (a unit drawn twice appears twice).
    """
    if not units:
        raise UsageError("cluster_bootstrap_ci needs at least one unit")
    _check_boot(boot)
    labels = list(strata) if strata is not None else ["all"] * len(units)
    if len(labels) != len(units):
        raise UsageError("cluster_bootstrap_ci: strata and units differ in length")
    point = stat(units)
    rnd = rng(seed, "verify.stats", scope)
    draws = []
    for _ in range(boot):
        counts = resample_counts(labels, rnd)
        sample = [u for u, c in zip(units, counts, strict=True) for _ in range(c)]
        draws.append(stat(sample))
    lo, hi = _level_bounds(draws, level)
    return point, lo, hi


def cuped(y: Sequence[float], x: Sequence[float]) -> tuple[list[float], float]:
    """CUPED adjustment: ``(Ỹ, θ)`` with ``θ = cov(Y, X)/var(X)`` and
    ``Ỹ_i = Y_i − θ (X_i − mean X)``. ``θ = 0`` when ``X`` is constant."""
    if len(y) != len(x):
        raise UsageError("cuped: y and x differ in length")
    if not y:
        return [], 0.0
    mx, my = mean(x), mean(y)
    var = math.fsum((xi - mx) ** 2 for xi in x)
    cov = math.fsum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True))
    theta = cov / var if var > 0 else 0.0
    return [yi - theta * (xi - mx) for xi, yi in zip(x, y, strict=True)], theta


# ---------------------------------------------------------------------------------------------
# χ² tail via the regularized incomplete gamma function
# ---------------------------------------------------------------------------------------------


def _gammainc_series(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x) by its series (x < a + 1)."""
    term = 1.0 / a
    total = term
    ap = a
    for _ in range(_GAMMA_ITMAX):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * _GAMMA_TOL:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gammaincc_cf(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x) by Lentz's continued fraction (x ≥ a + 1)."""
    b = x + 1.0 - a
    c = 1.0 / _EPS
    d = 1.0 / b
    h = d
    for i in range(1, _GAMMA_ITMAX + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _EPS:
            d = _EPS
        c = b + an / c
        if abs(c) < _EPS:
            c = _EPS
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _GAMMA_TOL:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gammaincc(a: float, x: float) -> float:
    """The regularized upper incomplete gamma function ``Q(a, x) = Γ(a, x) / Γ(a)``."""
    if a <= 0:
        raise UsageError("gammaincc: a must be > 0")
    if x < 0:
        raise UsageError("gammaincc: x must be ≥ 0")
    if x == 0:
        return 1.0
    if math.isinf(x):
        return 0.0
    if x < a + 1.0:
        return max(0.0, min(1.0, 1.0 - _gammainc_series(a, x)))
    return max(0.0, min(1.0, _gammaincc_cf(a, x)))


def chi2_sf(stat: float, df: int) -> float:
    """Survival function of the χ² distribution: ``P(χ²_df ≥ stat) = Q(df/2, stat/2)``."""
    if type(df) is not int or df < 1:
        raise UsageError("chi2_sf: df must be a positive int")
    if stat <= 0:
        return 1.0
    return gammaincc(df / 2.0, stat / 2.0)


def srm_pvalue(observed: Sequence[float], expected: Sequence[float]) -> float:
    """Sample-ratio-mismatch p-value: χ² goodness of fit of *observed* counts (e.g. active
    developer-days per arm) against the planned allocation *expected* (shares or counts; they are
    normalized to the observed total). Groups with an expected share of 0 must observe 0."""
    if len(observed) != len(expected) or len(observed) < 2:
        raise UsageError("srm_pvalue needs ≥ 2 groups of equal length")
    if any(o < 0 for o in observed) or any(e < 0 for e in expected):
        raise UsageError("srm_pvalue: counts and shares must be ≥ 0")
    total = math.fsum(observed)
    share_total = math.fsum(expected)
    if total <= 0 or share_total <= 0:
        raise UsageError("srm_pvalue: nothing observed or nothing planned")
    stat = 0.0
    df = -1
    for o, e in zip(observed, expected, strict=True):
        exp_count = total * e / share_total
        if exp_count == 0:
            if o > 0:
                return 0.0
            continue
        df += 1
        stat += (o - exp_count) ** 2 / exp_count
    if df < 1:
        return 1.0
    return chi2_sf(stat, df)


def wilson(successes: int, n: int, *, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval ``(low, high)`` for ``successes / n`` (``(0, 1)`` when ``n == 0``)."""
    if type(successes) is not int or type(n) is not int or n < 0 or not 0 <= successes <= n:
        raise UsageError("wilson: need ints with 0 ≤ successes ≤ n")
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4 * n * n)) / denom
    lo = 0.0 if successes == 0 else max(0.0, centre - half)
    hi = 1.0 if successes == n else min(1.0, centre + half)
    return lo, hi


# ---------------------------------------------------------------------------------------------
# least squares and HAC standard errors
# ---------------------------------------------------------------------------------------------


def mat_inv(m: Sequence[Sequence[float]]) -> list[list[float]]:
    """Inverse of a small square matrix by Gauss–Jordan elimination with partial pivoting
    (``UsageError`` when singular)."""
    n = len(m)
    if any(len(row) != n for row in m):
        raise UsageError("mat_inv: matrix is not square")
    scale = max((abs(v) for row in m for v in row), default=0.0)
    if scale == 0:
        raise UsageError("singular design matrix")
    a = [list(map(float, row)) + [1.0 if i == j else 0.0 for j in range(n)]
         for i, row in enumerate(m)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) <= 1e-12 * scale:
            raise UsageError("singular design matrix")
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        row = a[col] = [v / pv for v in a[col]]
        for r in range(n):
            if r != col:
                f = a[r][col]
                if f != 0.0:
                    ar = a[r]
                    a[r] = [x - f * y for x, y in zip(ar, row, strict=True)]
    return [row[n:] for row in a]


def _xtx(x: Sequence[Sequence[float]]) -> list[list[float]]:
    k = len(x[0])
    out = [[0.0] * k for _ in range(k)]
    for row in x:
        for i in range(k):
            ri = row[i]
            if ri == 0.0:
                continue
            oi = out[i]
            for j in range(k):
                oi[j] += ri * row[j]
    return out


def ols(x: Sequence[Sequence[float]], y: Sequence[float]
        ) -> tuple[list[float], list[float], list[list[float]]]:
    """Ordinary least squares: ``(beta, residuals, (X'X)^-1)``."""
    if not x or len(x) != len(y):
        raise UsageError("ols: X and y must be non-empty and of equal length")
    k = len(x[0])
    if len(x) <= k:
        raise UsageError("ols: more parameters than observations")
    inv = mat_inv(_xtx(x))
    xty = [math.fsum(row[j] * yi for row, yi in zip(x, y, strict=True)) for j in range(k)]
    beta = [math.fsum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    resid = [yi - math.fsum(b * v for b, v in zip(beta, row, strict=True))
             for row, yi in zip(x, y, strict=True)]
    return beta, resid, inv


def newey_west_se(x: Sequence[Sequence[float]], resid: Sequence[float], *, lag: int = 7,
                  xtx_inv: Sequence[Sequence[float]] | None = None) -> list[float]:
    """Newey–West heteroskedasticity- and autocorrelation-consistent standard errors of OLS
    coefficients: ``V = (X'X)^-1 S (X'X)^-1`` with the Bartlett-kernel meat
    ``S = Σ e_t² x_t x_t' + Σ_{l=1..L} (1 − l/(L+1)) Σ_t e_t e_{t−l}
    (x_t x_{t−l}' + x_{t−l} x_t')``. Rows of *x* must be in time order."""
    if type(lag) is not int or lag < 0:
        raise UsageError("newey_west_se: lag must be an int ≥ 0")
    n = len(x)
    if n == 0 or n != len(resid):
        raise UsageError("newey_west_se: X and residuals must be non-empty and of equal length")
    k = len(x[0])
    inv = [list(r) for r in xtx_inv] if xtx_inv is not None else mat_inv(_xtx(x))
    # score contributions x_t e_t
    u = [[v * e for v in row] for row, e in zip(x, resid, strict=True)]
    meat = [[0.0] * k for _ in range(k)]
    for ut in u:
        for i in range(k):
            mi = meat[i]
            ui = ut[i]
            for j in range(k):
                mi[j] += ui * ut[j]
    for lag_l in range(1, min(lag, n - 1) + 1):
        w = 1.0 - lag_l / (lag + 1.0)
        for t in range(lag_l, n):
            a, b = u[t], u[t - lag_l]
            for i in range(k):
                mi = meat[i]
                ai, bi = a[i], b[i]
                for j in range(k):
                    mi[j] += w * (ai * b[j] + bi * a[j])
    tmp = [[math.fsum(inv[i][m] * meat[m][j] for m in range(k)) for j in range(k)]
           for i in range(k)]
    cov = [[math.fsum(tmp[i][m] * inv[m][j] for m in range(k)) for j in range(k)]
           for i in range(k)]
    return [math.sqrt(max(cov[i][i], 0.0)) for i in range(k)]
