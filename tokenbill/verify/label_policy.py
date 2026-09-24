"""Guards and labels: when a measurement is MEASURED, VERIFIED or neither (SPEC §13.4, §1.2).

Guards (every one is recorded, in :data:`GUARD_NAMES` order; a guard that does not apply to the
design passes with a value starting ``"n/a"``):

``srm``
    χ² of active developer-days by arm (holdback vs treated waves) against the pre-registered arm
    shares; fails when p < 0.001.
``placebo``
    fake adoption at the pre-period midpoint (or the ITS placebo date): the CI must include 0.
``mde``
    the plan's MDE ≤ 0.8 × |projection|.
``reconciliation``
    every channel the panel covers is ``reconciled`` for the window (per-channel verdicts).
``cluster_cache_scope``
    for cache-touching levers, no cache scope spans two clusters.
``quality_noninferiority``
    with team-level outcome data, the one-sided 90% lower bound of Δ merged PRs per active
    developer-day is above −5%.
``registered_looks``
    every analysis date (look) was pre-registered.
``washout``
    the estimator excluded at least ``ceil(washout_hours / 24)`` days after each adoption.
``assignment_not_spend_targeted``
    the assignment is not correlated with pre-period spend (regression to the mean).

``result_inputs`` keys read by :func:`guards` (all optional; a missing input fails the guard that
needs it): ``panel`` (``Sequence[PanelRow]``), ``placebo`` (``(att, lo, hi)``), ``placebo_passed``
(ITS), ``pre_until``, ``channels``, ``window`` (``(since, until)``), ``cache_scopes`` (cache scope →
clusters), ``look`` / ``looks_taken``, ``washout_days``, ``quality_lower`` (float), ``boot``,
``seed``. Without ``placebo`` / ``quality_lower`` the guard computes them from ``panel``.

:func:`decide` returns VERIFIED iff the design is randomized with a logged seed whose assignment
hash matches the pre-registration, every guard passes and the CI excludes 0; MEASURED iff a
comparison group exists (or design ``its`` with its placebo passing) and a CI was computed;
otherwise ESTIMATED — "not a measurement" (e.g. an ITS whose placebo failed), which is never
signable. Nothing here emits EXACT.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

from tokenbill.core.catalog import lever as catalog_lever
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure
from tokenbill.core.money import RATIO_CTX
from tokenbill.core.types import (
    GuardResult,
    MeasurementResult,
    MeasurePlan,
    PanelRow,
    ReconciliationReport,
)
from tokenbill.verify.estimators import (
    cuped_cluster_dim,
    first_treated_date,
    imputation_did,
    placebo_cuped,
    placebo_did,
    pre_midpoint,
    quality_lower_bound,
    to_nano_triple,
)
from tokenbill.verify.its import event_study_its
from tokenbill.verify.panel import org_series, panel_window, rate_variance
from tokenbill.verify.rollout import (
    CONTROL_ARM,
    METRIC,
    is_randomized,
    preregistration,
    verify_assignment,
)
from tokenbill.verify.stats import srm_pvalue

__all__ = [
    "CACHE_TOUCHING_CLASSES",
    "COMPARISON_DESIGNS",
    "GUARD_NAMES",
    "PLACEBO_DESIGNS",
    "QUALITY_MARGIN",
    "SRM_ALPHA",
    "decide",
    "guards",
    "measure",
    "signable",
]

GUARD_NAMES = ("srm", "placebo", "mde", "reconciliation", "cluster_cache_scope",
               "quality_noninferiority", "registered_looks", "washout",
               "assignment_not_spend_targeted")
SRM_ALPHA = 0.001
QUALITY_MARGIN = -0.05
QUALITY_LEVEL = 0.90
#: Designs with a (randomized) comparison group.
COMPARISON_DESIGNS = frozenset({"cluster_rct", "stepped_wedge", "ab"})
#: Designs without a comparison group that are MEASURED only when their placebo passes.
PLACEBO_DESIGNS = frozenset({"its"})
#: Lever classes whose changes touch the prompt cache (cluster ≥ cache scope guard). A lever that
#: is not in the catalog is treated as cache-touching.
CACHE_TOUCHING_CLASSES = frozenset({"cache_transform", "trajectory"})
_LABELS = frozenset({Evidence.MEASURED, Evidence.VERIFIED})


def _g(name: str, passed: bool, value: str, threshold: str) -> GuardResult:
    return GuardResult(name=name, passed=bool(passed), value=value, threshold=threshold)


def _panel(result_inputs: Mapping[str, object]) -> list[PanelRow] | None:
    panel = result_inputs.get("panel")
    if panel is None:
        return None
    rows = list(panel)  # type: ignore[call-overload]
    if any(not isinstance(r, PanelRow) for r in rows):
        raise UsageError("result_inputs['panel'] must hold PanelRow values")
    return rows


def _int_opt(result_inputs: Mapping[str, object], key: str, default: int) -> int:
    v = result_inputs.get(key, default)
    if type(v) is not int:
        raise UsageError(f"result_inputs[{key!r}] must be an int")
    return v


def _srm(panel: list[PanelRow] | None, plan: MeasurePlan) -> GuardResult:
    threshold = f"p ≥ {SRM_ALPHA}"
    if plan.design not in COMPARISON_DESIGNS:
        return _g("srm", True, "n/a: no randomized arms", threshold)
    if panel is None:
        return _g("srm", False, "missing input: panel", threshold)
    shares = preregistration(plan).get("arm_shares")
    if not isinstance(shares, dict):
        return _g("srm", False, "missing input: pre-registered arm shares", threshold)
    control = set(plan.holdback)
    treatment = {c for _, members in plan.waves for c in members}
    obs = {CONTROL_ARM: 0, "treatment": 0}
    for r in panel:
        if r.cluster_id in control:
            obs[CONTROL_ARM] += r.active_dev_days
        elif r.cluster_id in treatment:
            obs["treatment"] += r.active_dev_days
    try:
        expected = [float(Decimal(str(shares[CONTROL_ARM]))),
                    float(Decimal(str(shares["treatment"])))]
        p = srm_pvalue([obs[CONTROL_ARM], obs["treatment"]], expected)
    except (KeyError, ArithmeticError, UsageError):
        return _g("srm", False, "not computable", threshold)
    return _g("srm", p >= SRM_ALPHA,
              f"p={p:.3g} (dev-days control {obs[CONTROL_ARM]}, treatment {obs['treatment']})",
              threshold)


def _placebo(result_inputs: Mapping[str, object], panel: list[PanelRow] | None,
             plan: MeasurePlan) -> GuardResult:
    threshold = "placebo CI includes 0"
    if plan.design in PLACEBO_DESIGNS:
        ok = result_inputs.get("placebo_passed")
        if not isinstance(ok, bool):
            return _g("placebo", False, "missing input: placebo_passed", threshold)
        return _g("placebo", ok, "includes 0" if ok else "excludes 0", threshold)
    tri = result_inputs.get("placebo")
    if tri is None:
        if panel is None:
            return _g("placebo", False, "missing input: panel", threshold)
        boot = _int_opt(result_inputs, "boot", 2000)
        seed = _int_opt(result_inputs, "seed", 0)
        try:
            if plan.design == "cluster_rct":
                pre_until = result_inputs.get("pre_until") or first_treated_date(panel)
                if not isinstance(pre_until, str):
                    raise UsageError("no adoption date")
                tri = placebo_cuped(panel, pre_until=pre_until, boot=boot, seed=seed)
            else:
                tri = placebo_did(panel, boot=boot, seed=seed)
        except UsageError:
            return _g("placebo", False, "not computable (pre-period too short)", threshold)
    if not isinstance(tri, (tuple, list)) or len(tri) != 3:
        raise UsageError("result_inputs['placebo'] must be (att, ci_low, ci_high)")
    _, lo, hi = tri
    return _g("placebo", lo <= 0 <= hi, f"CI [{lo}, {hi}] nano per active developer-day",
              threshold)


def _mde(plan: MeasurePlan, projection: Figure | None) -> GuardResult:
    if projection is None:
        return _g("mde", True, "n/a: no projection", "MDE ≤ 0.8 × |projection|")
    if projection.nano is None:
        return _g("mde", False, "projection unpriced", "MDE ≤ 0.8 × |projection|")
    target = abs(projection.nano)
    threshold = f"MDE ≤ 0.8 × {target} nano per active developer-day"
    if plan.mde_nano is None:
        return _g("mde", False, "MDE unknown", threshold)
    return _g("mde", 5 * plan.mde_nano <= 4 * target, f"MDE {plan.mde_nano}", threshold)


def _reconciliation(result_inputs: Mapping[str, object],
                    reconciliation: ReconciliationReport) -> GuardResult:
    threshold = "every covered channel reconciled"
    if not isinstance(reconciliation, ReconciliationReport):
        raise UsageError("reconciliation must be a ReconciliationReport")
    window = result_inputs.get("window")
    if window is not None:
        since, until = window  # type: ignore[misc]
        r_since, r_until = reconciliation.window
        if not (r_since <= since and until <= r_until):
            return _g("reconciliation", False, "reconciliation window does not cover the panel",
                      threshold)
    channels = result_inputs.get("channels")
    if channels is None:
        ok = reconciliation.verdict == "reconciled"
        return _g("reconciliation", ok, f"overall {reconciliation.verdict}", threshold)
    chans = sorted(set(channels))  # type: ignore[call-overload]
    if not chans:
        return _g("reconciliation", False, "no channel covered", threshold)
    verdicts = {cv.channel: cv.verdict for cv in reconciliation.channels}
    bad = [c for c in chans if verdicts.get(c) != "reconciled"]
    if bad:
        return _g("reconciliation", False, "not reconciled: " + ", ".join(bad), threshold)
    return _g("reconciliation", True, "reconciled: " + ", ".join(chans), threshold)


def _cache_touching(lever_id: str) -> bool:
    try:
        return catalog_lever(lever_id).lever_class in CACHE_TOUCHING_CLASSES
    except UsageError:
        return True


def _cache_scope(result_inputs: Mapping[str, object], plan: MeasurePlan) -> GuardResult:
    threshold = "no cache scope spans two clusters"
    if plan.design not in COMPARISON_DESIGNS:
        return _g("cluster_cache_scope", True, "n/a: org-wide design", threshold)
    if not _cache_touching(plan.lever_id):
        return _g("cluster_cache_scope", True, "n/a: lever does not touch the cache", threshold)
    scopes = result_inputs.get("cache_scopes")
    if scopes is None:
        return _g("cluster_cache_scope", False, "missing input: cache_scopes", threshold)
    if not isinstance(scopes, Mapping):
        raise UsageError("result_inputs['cache_scopes'] must map cache scope → clusters")
    spanning = sorted(s for s, cl in scopes.items() if len(set(cl)) > 1)
    return _g("cluster_cache_scope", not spanning,
              f"{len(spanning)} of {len(scopes)} cache scopes span clusters", threshold)


def _quality(result_inputs: Mapping[str, object], panel: list[PanelRow] | None,
             plan: MeasurePlan) -> GuardResult:
    threshold = f"one-sided {int(QUALITY_LEVEL * 100)}% lower bound > {QUALITY_MARGIN:.0%}"
    bound = result_inputs.get("quality_lower")
    if bound is None:
        if panel is None or not any(r.outcome_prs is not None for r in panel):
            return _g("quality_noninferiority", True, "n/a: no team-level outcome data",
                      threshold)
        if plan.design not in COMPARISON_DESIGNS:
            return _g("quality_noninferiority", True, "n/a: org-wide design", threshold)
        try:
            bound = quality_lower_bound(
                panel, design=plan.design, pre_until=result_inputs.get("pre_until")
                or first_treated_date(panel),  # type: ignore[arg-type]
                washout_days=_int_opt(result_inputs, "washout_days", 0),
                boot=_int_opt(result_inputs, "boot", 2000),
                seed=_int_opt(result_inputs, "seed", 0), level=QUALITY_LEVEL)
        except UsageError:
            return _g("quality_noninferiority", False, "not computable", threshold)
        if bound is None:  # pragma: no cover - guarded by the outcome check above
            return _g("quality_noninferiority", True, "n/a: no team-level outcome data",
                      threshold)
    if not isinstance(bound, (int, float)) or not math.isfinite(bound):
        raise UsageError("result_inputs['quality_lower'] must be a finite number")
    return _g("quality_noninferiority", bound > QUALITY_MARGIN,
              f"lower bound {bound:+.2%} merged PRs per active developer-day", threshold)


def _looks(result_inputs: Mapping[str, object], plan: MeasurePlan) -> GuardResult:
    threshold = "every look pre-registered"
    taken = result_inputs.get("looks_taken")
    if taken is None:
        look = result_inputs.get("look")
        taken = [look] if look is not None else []
    looks = [str(v) for v in taken]  # type: ignore[attr-defined]
    if not looks:
        return _g("registered_looks", False, "missing input: look", threshold)
    bad = [v for v in looks if v not in plan.looks]
    return _g("registered_looks", not bad,
              f"{len(looks)} look(s), {len(bad)} unregistered", threshold)


def _washout(result_inputs: Mapping[str, object], plan: MeasurePlan) -> GuardResult:
    need = math.ceil(plan.washout_hours / 24)
    threshold = f"≥ {need} day(s) excluded after adoption ({plan.washout_hours} h)"
    if plan.design not in COMPARISON_DESIGNS:
        return _g("washout", True, "n/a: org-wide design", threshold)
    used = result_inputs.get("washout_days")
    if type(used) is not int:
        return _g("washout", False, "missing input: washout_days", threshold)
    return _g("washout", used >= need, f"{used} day(s)", threshold)


def _spend(plan: MeasurePlan) -> GuardResult:
    threshold = "assignment not correlated with pre-period spend"
    flag = preregistration(plan).get("spend_targeted")
    if flag is True:
        return _g("assignment_not_spend_targeted", False,
                  "spend-targeted (regression to the mean)", threshold)
    if flag is False:
        return _g("assignment_not_spend_targeted", True, "not spend-targeted", threshold)
    return _g("assignment_not_spend_targeted", True, "n/a: seeded or org-wide assignment",
              threshold)


def guards(result_inputs: Mapping[str, object], *, plan: MeasurePlan,
           reconciliation: ReconciliationReport,
           projection: Figure | None) -> tuple[GuardResult, ...]:
    """Every guard of SPEC §13.4, in :data:`GUARD_NAMES` order (see the module docstring for the
    ``result_inputs`` keys)."""
    if not isinstance(plan, MeasurePlan):
        raise UsageError("plan must be a MeasurePlan")
    panel = _panel(result_inputs)
    return (
        _srm(panel, plan),
        _placebo(result_inputs, panel, plan),
        _mde(plan, projection),
        _reconciliation(result_inputs, reconciliation),
        _cache_scope(result_inputs, plan),
        _quality(result_inputs, panel, plan),
        _looks(result_inputs, plan),
        _washout(result_inputs, plan),
        _spend(plan),
    )


def decide(*, design: str, randomized: bool, assignment_hash_matches: bool,
           guards: Sequence[GuardResult], ci: tuple[int, int]) -> Evidence:
    """The label of a measurement (SPEC §13.4): VERIFIED, MEASURED, or ESTIMATED ("not a
    measurement"); never EXACT."""
    if design not in COMPARISON_DESIGNS and design not in PLACEBO_DESIGNS:
        raise UsageError(f"unknown design {design!r}")
    computed = (isinstance(ci, (tuple, list)) and len(ci) == 2
                and all(type(v) is int for v in ci) and ci[0] <= ci[1])
    if not computed:
        return Evidence.ESTIMATED
    excludes_zero = ci[0] > 0 or ci[1] < 0
    if (design in COMPARISON_DESIGNS and randomized and assignment_hash_matches
            and all(g.passed for g in guards) and excludes_zero):
        return Evidence.VERIFIED
    if design in COMPARISON_DESIGNS:
        return Evidence.MEASURED
    placebo = [g for g in guards if g.name == "placebo"]
    if placebo and all(g.passed for g in placebo):
        return Evidence.MEASURED
    return Evidence.ESTIMATED


def signable(label: Evidence, *, reconciled: bool, projection: Figure | None) -> bool:
    """``label ∈ {MEASURED, VERIFIED}`` and reconciliation passed and (no projection or the
    projection is CALIBRATED)."""
    return (Evidence(label) in _LABELS and bool(reconciled)
            and (projection is None or projection.calibration is Calibration.CALIBRATED))


# ---------------------------------------------------------------------------------------------
# the whole measurement
# ---------------------------------------------------------------------------------------------


def _add_days(date: str, days: int) -> str:
    return (_dt.date.fromisoformat(date) + _dt.timedelta(days=days)).isoformat()


def _rr(saving: int, lo: int, hi: int, projection: Figure | None
        ) -> tuple[str, str, str] | None:
    if projection is None or not projection.nano:
        return None
    den = Decimal(projection.nano)
    q = Decimal("0.000001")
    vals = [RATIO_CTX.divide(Decimal(v), den).quantize(q) for v in (saving, lo, hi)]
    a, b = sorted(vals[1:])
    return str(vals[0]), str(a), str(b)


def measure(panel: Sequence[PanelRow], *, plan: MeasurePlan,
            reconciliation: ReconciliationReport, look: str, rate_card_sha256: str,
            channels: Sequence[str] | None = None,
            cache_scopes: Mapping[str, Sequence[str]] | None = None,
            assignment_log: str | None = None, basis: Basis = Basis.LIST, boot: int = 2000,
            seed: int = 0, looks_taken: Sequence[str] | None = None,
            washout_days: int | None = None) -> MeasurementResult:
    """Run the plan's estimator on *panel* (built at the pre-registered baseline card), the
    guards, :func:`decide` and :func:`signable`; returns the :class:`MeasurementResult`.

    ``estimate`` is the realized **saving** per active developer-day (``−ATT``: positive when the
    treated arm got cheaper) with its CI; the rate variance is reported separately (EXACT, R8).
    Allowance panels (``basis=LIST_EQUIVALENT``) are measured but never signable (D26).
    """
    rows = list(panel)
    if not rows:
        raise UsageError("empty panel")
    design = plan.design
    since, until = panel_window(rows)
    need = math.ceil(plan.washout_hours / 24)
    w = need if washout_days is None else washout_days
    first = first_treated_date(rows)
    inputs: dict[str, object] = {"panel": rows, "look": look, "window": (since, until),
                                 "boot": boot, "seed": seed, "washout_days": w}
    if looks_taken is not None:
        inputs["looks_taken"] = list(looks_taken)
    if channels is not None:
        inputs["channels"] = list(channels)
    if cache_scopes is not None:
        inputs["cache_scopes"] = dict(cache_scopes)
    adjustments = [f"constant prices at the pre-registered rate card {rate_card_sha256[:12]}",
                   "rate variance reported separately (EXACT)"]
    post_from = first
    if design == "its":
        pre = preregistration(plan)
        change = pre.get("change_date") or first
        placebo_date = pre.get("placebo_date")
        if not isinstance(change, str):
            raise UsageError("design its needs a pre-registered change date")
        if not isinstance(placebo_date, str):
            placebo_date = pre_midpoint([r.date_utc for r in rows if r.date_utc < change])
        att, lo, hi, placebo_ok = event_study_its(org_series(rows), change_date=change,
                                                  placebo_date=placebo_date)
        inputs["placebo_passed"] = placebo_ok
        post_from = change
        adjustments.append("event-study ITS: linear trend, day-of-week effects, HAC lag 7")
    else:
        if first is None:
            raise UsageError("the panel has no treated cluster-days")
        if design == "stepped_wedge":
            att, lo, hi = imputation_did(rows, washout_days=w, boot=boot, seed=seed)
            try:
                inputs["placebo"] = placebo_did(rows, boot=boot, seed=seed)
            except UsageError:
                pass
            adjustments.append("imputation DiD: cluster and day fixed effects on untreated "
                               "cluster-days")
        elif design == "cluster_rct":
            cut = _add_days(first, w)
            post = [r for r in rows if not first <= r.date_utc < cut]
            att, lo, hi = cuped_cluster_dim(post, pre_until=first, boot=boot, seed=seed)
            inputs["pre_until"] = first
            try:
                inputs["placebo"] = placebo_cuped(rows, pre_until=first, boot=boot, seed=seed)
            except UsageError:
                pass
            adjustments.append("CUPED: pre-period cost per active developer-day covariate")
        else:
            raise UsageError(f"unknown design {design!r}")
        if w:
            adjustments.append(f"washout: {w} day(s) after adoption excluded")
    results = guards(inputs, plan=plan, reconciliation=reconciliation,
                     projection=plan.projection)
    label = decide(design=design, randomized=is_randomized(plan),
                   assignment_hash_matches=verify_assignment(plan, assignment_log),
                   guards=results, ci=(lo, hi))
    saving, s_lo, s_hi = to_nano_triple(-att, -hi, -lo)
    failing = [g.name for g in results if not g.passed]
    if label in _LABELS:
        estimate = Figure(nano=saving, evidence=label, basis=basis, low_nano=s_lo,
                          high_nano=s_hi, ci_level_pct=95,
                          note="failing guards: " + ", ".join(failing) if failing else "")
    else:
        estimate = Figure(nano=saving, evidence=Evidence.ESTIMATED, basis=basis, low_nano=s_lo,
                          high_nano=s_hi,
                          note="not a measurement: " + (", ".join(failing) or "no CI"))
    reconciled = next(g.passed for g in results if g.name == "reconciliation")
    ok = (signable(label, reconciled=reconciled, projection=plan.projection)
          and basis is not Basis.LIST_EQUIVALENT)
    clusters = sorted({r.cluster_id for r in rows})
    treated_days = sum(r.active_dev_days for r in rows if r.treated)
    return MeasurementResult(
        lever_id=plan.lever_id, design=design, unit=METRIC, estimate=estimate,
        projected=plan.projection, realization_rate=_rr(saving, s_lo, s_hi, plan.projection),
        guards=results,
        scope=(("clusters", len(clusters)),
               ("unit_days", sum(r.active_dev_days for r in rows)),
               ("treated_unit_days", treated_days)),
        scope_label=f"fleet:{since}..{until}", window=(("since", since), ("until", until)),
        rate_card_sha256=rate_card_sha256,
        assignment_log_sha256=plan.assignment_log_sha256 if design != "its" else None,
        preregistration_sha256=plan.preregistration_sha256, adjustments=tuple(adjustments),
        rate_variance=rate_variance(rows, post_from=post_from or "", basis=basis),
        signable=ok)
