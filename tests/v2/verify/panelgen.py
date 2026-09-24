"""Local seeded generators of synthetic verification panels (VERIFY tests only).

Every value is synthetic: cluster levels, day effects, developer-days and noise come from
``common.rng(seed, …)``. The generators return the panel together with its **truth** (the
in-sample ATT computed from the generator's own counterfactual), so tests can check estimates and
interval coverage.

Model (additive, matching the imputation estimator's ``α_c + λ_t``):
``Y0_ct = a_c + b_t + ε_ct`` nano per active developer-day, ``ε ~ N(0, noise · a_c / √devdays)``;
treated cells (from the cluster's adoption date) get ``Y1 = Y0 − effect · (a_c + b_t)``. A planted
pre-trend adds ``pre_trend · a_c · t / n_days`` to every ever-treated cluster (a differential
trend the placebo must catch).
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass

from tokenbill.common import rng
from tokenbill.core.types import PanelRow

START = _dt.date(2026, 6, 1)      # a Monday
LEVEL_NANO = 20 * 10**9           # $20 per active developer-day
_DOW = (0.02, 0.03, 0.01, 0.0, -0.02, -0.10, -0.12)


def day(i: int) -> str:
    """The ISO date *i* days after :data:`START`."""
    return (START + _dt.timedelta(days=i)).isoformat()


@dataclass(frozen=True)
class GenPanel:
    rows: list[PanelRow]
    truth_att: float                 # in-sample ATT, nano per active developer-day
    truth_base: float                # mean counterfactual level over the same treated cells
    starts: dict[str, str | None]    # cluster → adoption date (None: holdback)
    first_start: str
    holdback: tuple[str, ...]


def _binomial(rnd, n: int, p: float) -> int:
    return sum(1 for _ in range(n) if rnd.random() < p)


def rollout_panel(*, seed: int, clusters: int = 24, weeks: int = 10, waves: int = 4,
                  holdback: float = 0.25, effect: float = 0.25, noise: float = 0.05,
                  pre_weeks: int = 2, pre_trend: float = 0.0, price_cut: float = 0.0,
                  washout_days: int = 0, design: str = "stepped_wedge", prs: bool = False,
                  pr_effect: float = 0.0, targeted: bool = False,
                  lever: str = "cc.prompt_cache_ttl.main") -> GenPanel:
    """A stepped-wedge (or, with ``design="cluster_rct"``, a single-wave) panel."""
    rnd = rng(seed, "tests.verify.panelgen", design)
    names = [f"c{i:02d}" for i in range(clusters)]
    size = {c: rnd.randint(8, 40) for c in names}
    level = {c: LEVEL_NANO * math.exp(rnd.gauss(0.0, 0.3)) for c in names}
    n_days = weeks * 7
    shock = [LEVEL_NANO * (_DOW[t % 7] + 0.02 * math.sin(2 * math.pi * t / 30.0))
             for t in range(n_days)]
    order = list(names)
    if targeted:
        order.sort(key=lambda c: -level[c] * size[c])  # biggest spenders first
    else:
        rnd.shuffle(order)
    n_hold = max(1, round(holdback * clusters))
    hold = order[len(order) - n_hold:] if targeted else order[:n_hold]
    treat = [c for c in order if c not in hold]
    pre_days = pre_weeks * 7
    starts: dict[str, str | None] = {c: None for c in hold}
    start_idx: dict[str, int | None] = {c: None for c in hold}
    if design == "cluster_rct":
        for c in treat:
            start_idx[c], starts[c] = pre_days, day(pre_days)
    else:
        step = max(1, (n_days - pre_days - 14) // waves)
        for i, c in enumerate(treat):
            k = i * waves // len(treat)
            idx = pre_days + k * step
            start_idx[c], starts[c] = idx, day(idx)
    wave_of = {}
    for c in treat:
        wave_of[c] = str(1 + sorted({v for v in start_idx.values() if v is not None})
                         .index(start_idx[c]))
    rows: list[PanelRow] = []
    num = den = base = 0.0
    first = min(v for v in start_idx.values() if v is not None)
    for c in sorted(names):
        for t in range(n_days):
            p = 0.85 if t % 7 < 5 else 0.25
            devs = _binomial(rnd, size[c], p)
            eps = rnd.gauss(0.0, 1.0)
            if devs == 0:
                continue
            mu = level[c] + shock[t]
            y0 = mu + eps * noise * level[c] / math.sqrt(devs)
            if pre_trend and c in treat:
                y0 += pre_trend * level[c] * t / n_days
            s = start_idx[c]
            treated = s is not None and t >= s
            y = y0 - effect * mu if treated else y0
            cost = max(0, round(y * devs))
            actual = cost
            if price_cut and t >= first:
                actual = round(cost * (1.0 - price_cut))
            if treated and (washout_days == 0 or t >= s + washout_days):
                num += devs * (y - y0)
                base += devs * y0
                den += devs
            pr = None
            if prs:
                rate = 0.25 * (1.0 + pr_effect) if treated else 0.25
                pr = _binomial(rnd, 2 * devs, rate)
            rows.append(PanelRow(
                cluster_id=c, date_utc=day(t), cost_baseline_nano=cost,
                cost_actual_nano=actual, active_dev_days=devs,
                arm="control" if c in hold else lever,
                wave=None if c in hold else wave_of[c], treated=treated, outcome_prs=pr))
    return GenPanel(rows=rows, truth_att=num / den, truth_base=base / den, starts=starts,
                    first_start=day(first), holdback=tuple(sorted(hold)))


def org_series(*, seed: int, days: int = 150, change_day: int = 90, shift: float = -0.20,
               noise: float = 0.005, trend: float = 0.0,
               pre_ramp: tuple[int, int, float] | None = None, ar: float = 0.3
               ) -> tuple[list[tuple[str, int, int]], float]:
    """A daily org-level series ``(date, cost nano, active dev-days)`` with a level shift of
    ``shift × level`` at *change_day*, day-of-week effects, a linear *trend* (fraction of the level
    per day), AR(1) noise and an optional planted pre-trend ``pre_ramp = (start day, ramp days,
    fraction)``: from *start day* the pre-period drifts linearly to ``fraction × level`` over
    *ramp days* (a gradual pre-change drift the linear trend cannot absorb). Returns
    ``(series, truth δ)`` with δ in nano per active developer-day."""
    rnd = rng(seed, "tests.verify.panelgen", "its")
    level = float(LEVEL_NANO)
    out = []
    e = 0.0
    for t in range(days):
        e = ar * e + rnd.gauss(0.0, noise)
        y = level * (1.0 + _DOW[t % 7] + trend * t + e)
        if pre_ramp is not None and pre_ramp[0] <= t < change_day:
            start, width, frac = pre_ramp
            y += level * frac * min(1.0, (t - start + 1) / width)
        if t >= change_day:
            y += shift * level
        devs = 400 + rnd.randint(-20, 20) if t % 7 < 5 else 120 + rnd.randint(-10, 10)
        out.append((day(t), round(y * devs), devs))
    return out, shift * level
