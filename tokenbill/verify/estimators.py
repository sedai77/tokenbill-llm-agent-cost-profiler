"""Measurement estimators on a cluster-day panel (SPEC §13.3, D15): floats inside, int nano out.

The metric is **cost per active developer-day** at the pre-registered baseline rate card
(``PanelRow.cost_baseline_nano / PanelRow.active_dev_days``), intention-to-treat, per cluster-day.
Rows with no active developer-days carry no weight and are skipped; duplicate ``(cluster, date)``
rows are summed. Treatment is absorbing: a cluster is treated from its first ``treated`` row on.

* :func:`imputation_did` — imputation difference-in-differences for staggered / stepped-wedge
  rollouts (Borusyak–Jaravel–Spiess): fit ``Y_ct = α_c + λ_t`` on untreated cells by alternating
  projections (dev-day weighted; tolerance 1e-9 relative to the metric's scale; ≤ 10,000
  iterations), impute ``Ŷ0`` for treated cells (washout days after each cluster's adoption are
  excluded), ATT = dev-day-weighted mean ``Y − Ŷ0``; cluster bootstrap stratified by adoption
  cohort.
* :func:`cuped_cluster_dim` — CUPED cluster difference in means for a cluster RCT: pooled
  ``θ = cov(Y_post, X_pre)/var(X_pre)`` over clusters (a missing-pre indicator joins the
  regression when some clusters are new); ATT = dev-day-weighted mean ``Ỹ(treated) − Ỹ(control)``;
  cluster bootstrap stratified by arm.
* :func:`placebo_did` / :func:`placebo_cuped` — the same estimators on a fake adoption at the
  pre-period midpoint (the placebo guard, SPEC §13.4).
* :func:`quality_lower_bound` — the one-sided lower bound of the relative change in merged pull
  requests per active developer-day (quality non-inferiority guard).
* :func:`point_estimate` — the ATT without a bootstrap (A/A re-randomizations for the MDE).

All estimates are ``(ATT, ci_low, ci_high)`` in int nano per active developer-day; negative = the
treated arm got cheaper. There is deliberately no TWFE and no naive pre/post estimator.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from tokenbill.common import rng
from tokenbill.core.errors import UsageError
from tokenbill.core.types import PanelRow
from tokenbill.verify.stats import percentile, resample_counts

__all__ = [
    "AP_MAX_ITER",
    "AP_TOL",
    "DESIGN_ESTIMATORS",
    "BootResult",
    "cuped_cluster_dim",
    "first_treated_date",
    "imputation_did",
    "placebo_cuped",
    "placebo_did",
    "point_estimate",
    "pre_midpoint",
    "quality_lower_bound",
    "to_nano_triple",
]

#: Alternating-projection tolerance (relative to the metric's scale) and iteration cap.
AP_TOL = 1e-9
AP_MAX_ITER = 10_000

#: Estimator per randomized design (table-driven; ITS lives in ``verify.its``).
DESIGN_ESTIMATORS: Mapping[str, str] = {
    "stepped_wedge": "imputation_did",
    "cluster_rct": "cuped_cluster_dim",
}

_NEVER = "never"


# ---------------------------------------------------------------------------------------------
# panel preparation
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Cell:
    cluster: str
    date: str
    cost: int
    dev_days: int
    treated: bool
    prs: int | None


def _merged_rows(panel: Sequence[PanelRow]) -> list[_Cell]:
    """One cell per (cluster, date): costs, dev-days and PRs summed, ``treated`` OR-ed."""
    cells: dict[tuple[str, str], _Cell] = {}
    for row in panel:
        if not isinstance(row, PanelRow):
            raise UsageError("panel rows must be PanelRow")
        key = (row.cluster_id, row.date_utc)
        cell = cells.get(key)
        if cell is None:
            cells[key] = _Cell(row.cluster_id, row.date_utc, row.cost_baseline_nano,
                               row.active_dev_days, bool(row.treated), row.outcome_prs)
        else:
            cell.cost += row.cost_baseline_nano
            cell.dev_days += row.active_dev_days
            cell.treated = cell.treated or bool(row.treated)
            if row.outcome_prs is not None:
                cell.prs = (cell.prs or 0) + row.outcome_prs
    return [cells[k] for k in sorted(cells)]


def _value(cell: _Cell, metric: str) -> float | None:
    if cell.dev_days <= 0:
        return None
    if metric == "cost":
        return cell.cost / cell.dev_days
    if cell.prs is None:
        return None
    return cell.prs / cell.dev_days


def first_treated_date(panel: Sequence[PanelRow]) -> str | None:
    """The earliest date of any treated row (None when nothing is treated)."""
    dates = [r.date_utc for r in panel if r.treated]
    return min(dates) if dates else None


def _add_days(date: str, days: int) -> str:
    return (_dt.date.fromisoformat(date) + _dt.timedelta(days=days)).isoformat()


def _cluster_starts(cells: Sequence[_Cell]) -> dict[str, str | None]:
    starts: dict[str, str | None] = {}
    for c in cells:
        if c.treated:
            prev = starts.get(c.cluster)
            starts[c.cluster] = c.date if prev is None else min(prev, c.date)
        else:
            starts.setdefault(c.cluster, None)
    return starts


def to_nano_triple(att: float, lo: float, hi: float) -> tuple[int, int, int]:
    """Round an ``(estimate, low, high)`` float triple to int nano, widening the interval so it
    always contains the point (a percentile interval can miss a skewed point estimate)."""
    for v in (att, lo, hi):
        if not math.isfinite(v):
            raise UsageError("estimate is not finite")
    p, a, b = round(att), round(lo), round(hi)
    return p, min(a, p), max(b, p)


@dataclass(frozen=True, slots=True)
class BootResult:
    """An estimate with its bootstrap draws: ``att`` and ``base`` (the counterfactual level the
    ATT is relative to) for the full sample, and one ``(att, base)`` pair per replicate."""

    att: float
    base: float
    draws: tuple[tuple[float, float], ...]
    n_units: int
    n_treated_cells: int

    def ci(self, level: float = 0.95) -> tuple[float, float]:
        """Percentile interval of the ATT draws."""
        tail = (1.0 - level) / 2.0
        vals = [a for a, _ in self.draws]
        return percentile(vals, tail), percentile(vals, 1.0 - tail)

    def relative_lower(self, level: float = 0.90) -> float:
        """One-sided lower bound (at *level*) of ``att / base`` over the replicates."""
        vals = [a / b for a, b in self.draws if b != 0]
        if not vals:
            raise UsageError("relative bound undefined: zero counterfactual level")
        return percentile(vals, 1.0 - level)


# ---------------------------------------------------------------------------------------------
# imputation DiD
# ---------------------------------------------------------------------------------------------


class _TwoWay:
    """Untreated cells of a panel and the α_c + λ_t fit by alternating projections."""

    def __init__(self, cells: Sequence[_Cell], metric: str, washout_days: int) -> None:
        self.clusters = sorted({c.cluster for c in cells})
        self.dates = sorted({c.date for c in cells})
        cidx = {c: i for i, c in enumerate(self.clusters)}
        tidx = {d: i for i, d in enumerate(self.dates)}
        starts = _cluster_starts(cells)
        self.starts = starts
        nc, nt = len(self.clusters), len(self.dates)
        # untreated cells, by cluster and by time
        self.c_t: list[list[int]] = [[] for _ in range(nc)]
        self.c_w: list[list[float]] = [[] for _ in range(nc)]
        self.c_sw = [0.0] * nc
        self.c_swy = [0.0] * nc
        self.t_c: list[list[int]] = [[] for _ in range(nt)]
        self.t_w: list[list[float]] = [[] for _ in range(nt)]
        self.t_wy: list[list[float]] = [[] for _ in range(nt)]
        # treated cells after washout: (cluster idx, time idx, y, w)
        self.treated: list[tuple[int, int, float, float]] = []
        scale = 0.0
        for cell in cells:
            y = _value(cell, metric)
            if y is None:
                continue
            w = float(cell.dev_days)
            ci, ti = cidx[cell.cluster], tidx[cell.date]
            start = starts.get(cell.cluster)
            if start is not None and cell.date >= start:
                if washout_days > 0 and cell.date < _add_days(start, washout_days):
                    continue
                self.treated.append((ci, ti, y, w))
                continue
            scale = max(scale, abs(y))
            self.c_t[ci].append(ti)
            self.c_w[ci].append(w)
            self.c_sw[ci] += w
            self.c_swy[ci] += w * y
            self.t_c[ti].append(ci)
            self.t_w[ti].append(w)
            self.t_wy[ti].append(w * y)
        self.tol = AP_TOL * max(1.0, scale)
        self.strata = [str(starts.get(c)) if starts.get(c) is not None else _NEVER
                       for c in self.clusters]
        self.iterations = 0

    def fit(self, mult: Sequence[int] | None = None,
            warm: tuple[list[float], list[float]] | None = None
            ) -> tuple[list[float | None], list[float | None]]:
        """``(α, λ)`` fitted on the untreated cells with cluster multiplicities *mult*
        (None entries are unidentified)."""
        nc, nt = len(self.clusters), len(self.dates)
        m = mult if mult is not None else [1] * nc
        alive_c = [m[c] > 0 and self.c_sw[c] > 0 for c in range(nc)]
        # per-time weights under the multiplicities
        tw: list[list[float]] = []
        tsw = [0.0] * nt
        tswy = [0.0] * nt
        for t in range(nt):
            ws = [m[c] * w for c, w in zip(self.t_c[t], self.t_w[t], strict=True)]
            tw.append(ws)
            tsw[t] = math.fsum(ws)
            tswy[t] = math.fsum(m[c] * wy for c, wy in zip(self.t_c[t], self.t_wy[t], strict=True))
        alive_t = [s > 0 for s in tsw]
        alpha = list(warm[0]) if warm is not None else [0.0] * nc
        lam = list(warm[1]) if warm is not None else [0.0] * nt
        tol = self.tol
        self.iterations = AP_MAX_ITER
        for it in range(1, AP_MAX_ITER + 1):
            delta = 0.0
            get_l = lam.__getitem__
            for c in range(nc):
                if not alive_c[c]:
                    continue
                s = self.c_swy[c] - math.fsum(map(float.__mul__, self.c_w[c],
                                                  map(get_l, self.c_t[c])))
                new = s / self.c_sw[c]
                d = abs(new - alpha[c])
                if d > delta:
                    delta = d
                alpha[c] = new
            get_a = alpha.__getitem__
            for t in range(nt):
                if not alive_t[t]:
                    continue
                s = tswy[t] - math.fsum(map(float.__mul__, tw[t], map(get_a, self.t_c[t])))
                new = s / tsw[t]
                d = abs(new - lam[t])
                if d > delta:
                    delta = d
                lam[t] = new
            if delta <= tol:
                self.iterations = it
                break
        return ([a if alive_c[c] else None for c, a in enumerate(alpha)],
                [v if alive_t[t] else None for t, v in enumerate(lam)])

    def att(self, alpha: Sequence[float | None], lam: Sequence[float | None],
            mult: Sequence[int] | None = None) -> tuple[float, float, int]:
        """``(ATT, mean Ŷ0, imputable treated cells)`` over treated cells with identified
        fixed effects."""
        num = den = base = 0.0
        n = 0
        for c, t, y, w in self.treated:
            k = mult[c] if mult is not None else 1
            if k == 0:
                continue
            a, v = alpha[c], lam[t]
            if a is None or v is None:
                continue
            y0 = a + v
            ww = k * w
            num += ww * (y - y0)
            base += ww * y0
            den += ww
            n += 1
        if den <= 0:
            raise UsageError("no treated cell can be imputed (every treated cluster needs "
                             "untreated days and every treated day needs untreated clusters)")
        return num / den, base / den, n


def _did(panel: Sequence[PanelRow], *, metric: str, washout_days: int, boot: int,
         seed: int, scope: str) -> BootResult:
    if type(washout_days) is not int or washout_days < 0:
        raise UsageError("washout_days must be an int ≥ 0")
    if type(boot) is not int or boot < 0:
        raise UsageError("boot must be an int ≥ 0")
    cells = _merged_rows(panel)
    tw = _TwoWay(cells, metric, washout_days)
    if not tw.treated:
        raise UsageError("imputation_did: the panel has no treated cluster-days")
    if not any(tw.c_t):
        raise UsageError("imputation_did: the panel has no untreated cluster-days")
    alpha, lam = tw.fit()
    att, base, n_cells = tw.att(alpha, lam)
    warm = ([a if a is not None else 0.0 for a in alpha], [v if v is not None else 0.0
                                                          for v in lam])
    draws: list[tuple[float, float]] = []
    rnd = rng(seed, "verify.imputation_did", scope)
    for _ in range(boot):
        counts = resample_counts(tw.strata, rnd)
        a_b, l_b = tw.fit(counts, warm)
        try:
            att_b, base_b, _ = tw.att(a_b, l_b, counts)
        except UsageError:
            continue  # a replicate without imputable treated cells carries no information
        draws.append((att_b, base_b))
    if boot and not draws:
        raise UsageError("imputation_did: no bootstrap replicate could be imputed")
    return BootResult(att=att, base=base, draws=tuple(draws), n_units=len(tw.clusters),
                      n_treated_cells=n_cells)


def imputation_did(panel: Sequence[PanelRow], *, washout_days: int = 0, boot: int = 2000,
                   seed: int = 0) -> tuple[int, int, int]:
    """Imputation DiD on a stepped-wedge / staggered panel: ``(ATT nano per active dev-day,
    ci_low, ci_high)`` with a seeded cluster-bootstrap percentile 95% CI (B = *boot*)."""
    if type(boot) is not int or boot < 1:
        raise UsageError("boot must be a positive int")
    res = _did(panel, metric="cost", washout_days=washout_days, boot=boot, seed=seed,
               scope="estimate")
    lo, hi = res.ci()
    return to_nano_triple(res.att, lo, hi)


# ---------------------------------------------------------------------------------------------
# CUPED cluster difference in means
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Unit:
    cluster: str
    x: float | None      # pre-period metric (None: new cluster)
    y: float             # post-period metric
    w: float             # post-period dev-days
    treated: bool


def _cuped_units(panel: Sequence[PanelRow], pre_until: str, metric: str) -> list[_Unit]:
    cells = _merged_rows(panel)
    pre: dict[str, list[float]] = {}
    post: dict[str, list[float]] = {}
    treated: dict[str, bool] = {}
    for cell in cells:
        v = _value(cell, metric)
        if v is None:
            continue
        num = v * cell.dev_days
        bucket = pre if cell.date < pre_until else post
        acc = bucket.setdefault(cell.cluster, [0.0, 0.0])
        acc[0] += num
        acc[1] += cell.dev_days
        if cell.date >= pre_until:
            treated[cell.cluster] = treated.get(cell.cluster, False) or cell.treated
    units = []
    for cluster in sorted(post):
        num, w = post[cluster]
        if w <= 0:
            continue
        p = pre.get(cluster)
        x = p[0] / p[1] if p is not None and p[1] > 0 else None
        units.append(_Unit(cluster, x, num / w, w, treated[cluster]))
    return units


def _cuped_theta(units: Sequence[_Unit], mult: Sequence[int]
                 ) -> tuple[float, float, float, float, float]:
    """``(θ_x, θ_m, mean X_f, mean M, fill)`` pooled over clusters (weights = multiplicities);
    ``fill`` is the mean pre-period metric that stands in for a new cluster's missing ``X``."""
    have = [(u, k) for u, k in zip(units, mult, strict=True) if k > 0]
    n = sum(k for _, k in have)
    xs = [(u.x, k) for u, k in have if u.x is not None]
    nx = sum(k for _, k in xs)
    if nx == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    mean_x_present = math.fsum(x * k for x, k in xs) / nx
    xf = [(u.x if u.x is not None else mean_x_present, 1.0 if u.x is None else 0.0, u.y, k)
          for u, k in have]
    mx = math.fsum(x * k for x, _, _, k in xf) / n
    mm = math.fsum(mi * k for _, mi, _, k in xf) / n
    my = math.fsum(y * k for _, _, y, k in xf) / n
    sxx = math.fsum(k * (x - mx) ** 2 for x, _, _, k in xf)
    smm = math.fsum(k * (mi - mm) ** 2 for _, mi, _, k in xf)
    sxm = math.fsum(k * (x - mx) * (mi - mm) for x, mi, _, k in xf)
    sxy = math.fsum(k * (x - mx) * (y - my) for x, _, y, k in xf)
    smy = math.fsum(k * (mi - mm) * (y - my) for _, mi, y, k in xf)
    if smm > 0:
        det = sxx * smm - sxm * sxm
        if det > 1e-12 * max(sxx * smm, 1e-300):
            return ((sxy * smm - smy * sxm) / det, (smy * sxx - sxy * sxm) / det, mx, mm,
                    mean_x_present)
        return 0.0, smy / smm, mx, mm, mean_x_present
    theta = sxy / sxx if sxx > 0 else 0.0
    return theta, 0.0, mx, mm, mean_x_present


def _cuped_att(units: Sequence[_Unit], mult: Sequence[int], adjust: bool = True
               ) -> tuple[float, float]:
    """``(ATT, control mean Ỹ)`` for multiplicities *mult*."""
    if adjust:
        tx, tm, mx, mm, fill = _cuped_theta(units, mult)
    sums = {True: [0.0, 0.0], False: [0.0, 0.0]}
    for u, k in zip(units, mult, strict=True):
        if k == 0:
            continue
        y = u.y
        if adjust:
            x = u.x if u.x is not None else fill
            y = y - tx * (x - mx) - tm * ((1.0 if u.x is None else 0.0) - mm)
        acc = sums[u.treated]
        acc[0] += k * u.w * y
        acc[1] += k * u.w
    if sums[True][1] <= 0 or sums[False][1] <= 0:
        raise UsageError("cuped_cluster_dim needs treated and control clusters with post-period "
                         "developer-days")
    ctrl = sums[False][0] / sums[False][1]
    return sums[True][0] / sums[True][1] - ctrl, ctrl


def _cuped(panel: Sequence[PanelRow], *, pre_until: str, metric: str, boot: int, seed: int,
           scope: str, adjust: bool = True) -> BootResult:
    _check_date(pre_until, "pre_until")
    if type(boot) is not int or boot < 0:
        raise UsageError("boot must be an int ≥ 0")
    units = _cuped_units(panel, pre_until, metric)
    ones = [1] * len(units)
    att, base = _cuped_att(units, ones, adjust)
    strata = ["treated" if u.treated else "control" for u in units]
    rnd = rng(seed, "verify.cuped_cluster_dim", scope)
    draws = []
    for _ in range(boot):
        counts = resample_counts(strata, rnd)
        draws.append(_cuped_att(units, counts, adjust))
    return BootResult(att=att, base=base, draws=tuple(draws), n_units=len(units),
                      n_treated_cells=sum(1 for u in units if u.treated))


def cuped_cluster_dim(panel: Sequence[PanelRow], *, pre_until: str, boot: int = 2000,
                      seed: int = 0) -> tuple[int, int, int]:
    """CUPED cluster difference in means (cluster RCT): ``(ATT nano per active dev-day, ci_low,
    ci_high)``; rows before *pre_until* are the covariate period, rows from it on the outcome
    period; a cluster is treated when any outcome-period row is. Seeded cluster bootstrap
    stratified by arm, percentile 95% CI."""
    if type(boot) is not int or boot < 1:
        raise UsageError("boot must be a positive int")
    res = _cuped(panel, pre_until=pre_until, metric="cost", boot=boot, seed=seed,
                 scope="estimate")
    lo, hi = res.ci()
    return to_nano_triple(res.att, lo, hi)


# ---------------------------------------------------------------------------------------------
# placebo, quality and point estimates
# ---------------------------------------------------------------------------------------------


def _check_date(value: str, name: str) -> None:
    try:
        _dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise UsageError(f"{name} must be a YYYY-MM-DD date") from None


def pre_midpoint(dates: Sequence[str]) -> str:
    """The midpoint date of a pre-period (the fake adoption date of the placebo)."""
    ds = sorted(set(dates))
    if len(ds) < 4:
        raise UsageError("the pre-period needs at least 4 days for a placebo")
    return ds[len(ds) // 2]


def _fake(rows: Sequence[PanelRow], treated_clusters: set[str], start: str) -> list[PanelRow]:
    return [replace(r, treated=r.cluster_id in treated_clusters and r.date_utc >= start)
            for r in rows]


def placebo_did(panel: Sequence[PanelRow], *, boot: int = 2000, seed: int = 0
                ) -> tuple[int, int, int]:
    """Imputation DiD on the pre-period (before the first adoption) with a fake adoption of every
    ever-treated cluster at the pre-period midpoint. A CI that excludes 0 means differential
    pre-trends (the placebo guard fails)."""
    first = first_treated_date(panel)
    if first is None:
        raise UsageError("placebo_did: the panel has no treated rows")
    pre = [r for r in panel if r.date_utc < first]
    mid = pre_midpoint([r.date_utc for r in pre])
    ever = {r.cluster_id for r in panel if r.treated}
    return imputation_did(_fake(pre, ever, mid), washout_days=0, boot=boot, seed=seed)


def placebo_cuped(panel: Sequence[PanelRow], *, pre_until: str, boot: int = 2000,
                  seed: int = 0) -> tuple[int, int, int]:
    """CUPED DIM inside the covariate period: the first half is the covariate, the second the
    outcome, the real assignment the (fake) arm."""
    _check_date(pre_until, "pre_until")
    pre = [r for r in panel if r.date_utc < pre_until]
    mid = pre_midpoint([r.date_utc for r in pre])
    ever = {r.cluster_id for r in panel if r.treated and r.date_utc >= pre_until}
    return cuped_cluster_dim(_fake(pre, ever, mid), pre_until=mid, boot=boot, seed=seed)


def quality_lower_bound(panel: Sequence[PanelRow], *, design: str, pre_until: str | None = None,
                        washout_days: int = 0, boot: int = 2000, seed: int = 0,
                        level: float = 0.90) -> float | None:
    """One-sided lower bound (*level*) of the relative change in merged pull requests per active
    developer-day (``Δ / counterfactual level``), or None when the panel has no outcome data.

    The non-inferiority guard passes when the bound is above −5%.
    """
    if not any(r.outcome_prs is not None for r in panel):
        return None
    rows = [r for r in panel if r.outcome_prs is not None]
    if design == "stepped_wedge":
        res = _did(rows, metric="prs", washout_days=washout_days, boot=boot, seed=seed,
                   scope="quality")
    elif design == "cluster_rct":
        if pre_until is None:
            raise UsageError("quality_lower_bound: cluster_rct needs pre_until")
        res = _cuped(rows, pre_until=pre_until, metric="prs", boot=boot, seed=seed,
                     scope="quality")
    else:
        raise UsageError(f"no comparison-group estimator for design {design!r}")
    return res.relative_lower(level)


_POINT: Mapping[str, Callable[..., float]] = {
    "stepped_wedge": lambda panel, *, pre_until, washout_days: _did(
        panel, metric="cost", washout_days=washout_days, boot=0, seed=0, scope="point").att,
    "cluster_rct": lambda panel, *, pre_until, washout_days: _cuped(
        panel, pre_until=pre_until, metric="cost", boot=0, seed=0, scope="point").att,
}


def point_estimate(panel: Sequence[PanelRow], *, design: str, pre_until: str | None = None,
                   washout_days: int = 0) -> float:
    """The design's ATT (float nano per active dev-day) without a bootstrap."""
    fn = _POINT.get(design)
    if fn is None:
        raise UsageError(f"no comparison-group estimator for design {design!r}")
    if design == "cluster_rct" and pre_until is None:
        raise UsageError("cluster_rct needs pre_until")
    return fn(panel, pre_until=pre_until, washout_days=washout_days)
