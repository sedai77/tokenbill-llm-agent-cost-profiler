"""Event-study interrupted time series for org-wide changes (SPEC §13.3, D15): floats inside.

For changes that apply to the whole organization at once (a server-managed default model or effort)
there is no comparison group. The design regresses the daily org-level **cost per active developer-
day** (baseline-repriced) on a level, a linear trend, day-of-week effects and a level shift at the
pre-registered change date::

    Y_t = a + b·t + Σ dow_t + δ·1[t ≥ change]

with Newey–West (HAC, Bartlett, lag 7) standard errors and a 95% CI on δ. A placebo level shift at
the pre-registered placebo date, fitted on the pre-period only, must have a CI that includes 0. The
result is **MEASURED at most**, never VERIFIED (no randomization); SDID with donor pools is deferred
(SPEC §20).

Small-sample refinement of the 95% interval (a normal critical value with plain Newey–West SEs
over-rejects at ~90 days: ~15% false placebo failures at AR(1) 0.3 in the VERIFY tests): the HAC
variance gets the degrees-of-freedom factor ``n/(n − k)`` and the critical value is the
Kiefer–Vogelsang (2005) fixed-b 0.975 quantile for the Bartlett kernel,
``q(b) = 1.9600 + 2.9694 b + 0.4160 b² − 0.5324 b³`` with ``b = (lag + 1)/n`` (verified 2026-09-23
against https://www.york.ac.uk/media/economics/documents/discussionpapers/2015/1515.pdf, which
quotes KV 2005). Both reduce to the normal 1.96 interval as ``n`` grows.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Sequence

from tokenbill.core.errors import UsageError
from tokenbill.verify.stats import iso_date, newey_west_se, ols

__all__ = [
    "MIN_SIDE_DAYS",
    "fixed_b_critical_value",
    "event_study_its",
    "its_point",
    "series_dates",
]

#: Minimum number of observed days on each side of a change (and of the placebo date).
MIN_SIDE_DAYS = 7


def fixed_b_critical_value(b: float) -> float:
    """Kiefer–Vogelsang fixed-b 0.975 quantile for the Bartlett kernel (two-sided 95%)."""
    if not 0.0 <= b <= 1.0:
        raise UsageError("fixed-b bandwidth ratio must be in [0, 1]")
    return 1.9600 + 2.9694 * b + 0.4160 * b * b - 0.5324 * b * b * b


def _parse(series: Sequence[tuple[str, int, int]]) -> list[tuple[_dt.date, float]]:
    """Aggregate ``(date, cost nano, dev-days)`` rows per date → ``(date, cost per dev-day)``,
    days without developer-days dropped, in date order."""
    acc: dict[str, list[int]] = {}
    try:
        items = list(series)
    except TypeError:
        raise UsageError("a series is a sequence of (date, cost_nano, active_dev_days)") from None
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise UsageError("series items must be (date, cost_nano, active_dev_days)")
        date, cost, devs = item
        if type(cost) is not int or type(devs) is not int or devs < 0:
            raise UsageError("series cost and dev-days must be ints (dev-days ≥ 0)")
        if not isinstance(date, str) or date not in acc:
            iso_date(date, "series dates")
        a = acc.setdefault(date, [0, 0])
        a[0] += cost
        a[1] += devs
    return [(_dt.date.fromisoformat(d), c / n) for d, (c, n) in sorted(acc.items()) if n > 0]


def series_dates(series: Sequence[tuple[str, int, int]]) -> list[str]:
    """The dates of *series* that carry developer-days, in order."""
    return [d.isoformat() for d, _ in _parse(series)]


def _design(points: Sequence[tuple[_dt.date, float]], step: _dt.date
            ) -> tuple[list[list[float]], list[float], int]:
    """Rows ``[1, t, dow dummies…, 1[t ≥ step]]``; returns (X, y, index of the step column)."""
    t0 = points[0][0]
    span = max(1, (points[-1][0] - t0).days)
    dows = sorted({d.weekday() for d, _ in points})
    ref = dows[0]
    others = [w for w in dows if w != ref]
    x, y = [], []
    for d, v in points:
        row = [1.0, (d - t0).days / span]
        row.extend(1.0 if d.weekday() == w else 0.0 for w in others)
        row.append(1.0 if d >= step else 0.0)
        x.append(row)
        y.append(v)
    return x, y, len(x[0]) - 1


def _shift(points: Sequence[tuple[_dt.date, float]], step: _dt.date, hac_lag: int
           ) -> tuple[float, float, float]:
    """``(δ, HAC standard error, critical value)`` of a level shift at *step*."""
    before = sum(1 for d, _ in points if d < step)
    after = len(points) - before
    if before < MIN_SIDE_DAYS or after < MIN_SIDE_DAYS:
        raise UsageError(f"a level shift needs ≥ {MIN_SIDE_DAYS} observed days on each side")
    x, y, j = _design(points, step)
    if len(x) <= len(x[0]) + 1:
        raise UsageError("too few days for the event-study regression")
    beta, resid, inv = ols(x, y)
    se = newey_west_se(x, resid, lag=hac_lag, xtx_inv=inv)
    n, k = len(x), len(x[0])
    crit = fixed_b_critical_value(min(1.0, (hac_lag + 1) / n))
    return beta[j], se[j] * math.sqrt(n / (n - k)), crit


def _date(value: str, name: str) -> _dt.date:
    return iso_date(value, name)


def its_point(series: Sequence[tuple[str, int, int]], *, change_date: str,
              hac_lag: int = 7) -> float:
    """The level shift δ (float nano per active dev-day) without the placebo (A/A draws)."""
    points = _parse(series)
    if not points:
        raise UsageError("empty series")
    return _shift(points, _date(change_date, "change_date"), hac_lag)[0]


def event_study_its(series: Sequence[tuple[str, int, int]], *, change_date: str,
                    placebo_date: str, hac_lag: int = 7) -> tuple[int, int, int, bool]:
    """Event-study ITS on a daily org series of ``(date, cost nano, active dev-days)``:
    ``(level shift nano per dev-day, ci_low, ci_high, placebo_passed)``.

    The placebo date must fall inside the pre-period with ≥ 7 observed days on each side of it
    (and of the change date); otherwise ``UsageError``.
    """
    if type(hac_lag) is not int or hac_lag < 0:
        raise UsageError("hac_lag must be an int ≥ 0")
    points = _parse(series)
    if not points:
        raise UsageError("empty series")
    change = _date(change_date, "change_date")
    placebo = _date(placebo_date, "placebo_date")
    if not placebo < change:
        raise UsageError("the placebo date must precede the change date")
    delta, se, crit = _shift(points, change, hac_lag)
    pre = [p for p in points if p[0] < change]
    p_delta, p_se, p_crit = _shift(pre, placebo, hac_lag)
    lo, hi = delta - crit * se, delta + crit * se
    p_lo, p_hi = p_delta - p_crit * p_se, p_delta + p_crit * p_se
    for v in (delta, lo, hi):
        if not math.isfinite(v):
            raise UsageError("the ITS estimate is not finite")
    return round(delta), round(lo), round(hi), bool(p_lo <= 0.0 <= p_hi)
