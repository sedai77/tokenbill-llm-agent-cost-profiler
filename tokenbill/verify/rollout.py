"""Randomized rollout plans at cache-isolation units, and the ITS design for org-wide changes
(SPEC §13.2).

:func:`plan` assigns clusters (workspaces, MDM groups, or IdP groups via the Claude apps gateway —
never individuals inside a shared workspace) to a 10–25% never-treated holdback and to seeded
waves (the order comes from ``common.rng``, never from spend), computes the washout, the per-wave
MDM payload names and ``OTEL_RESOURCE_ATTRIBUTES`` arm/wave tags, an assignment log (JSON lines)
and its SHA-256, the MDE from 200 A/A re-randomizations of the pre-period panel under the planned
design (``MDE = 2.8 × SD``), and a pre-registration with its SHA-256.

Units: the MDE and ``projection`` are in nano-USD **per active developer-day** (the metric's unit;
:func:`projection_per_dev_day` converts a monthly projection). ``projection`` is the projected
*saving* (positive = cheaper); the check is ``MDE ≤ 0.8 × |projection|``.

Extensions beyond the SPEC signature (keyword-only, with defaults): ``change_date`` (the
pre-registered change date of an ITS design; default: the day after the pre-period panel) and
``rate_card_sha256`` (recorded in the pre-registration).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from tokenbill.common import rng
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Figure, scale
from tokenbill.core.money import RATIO_CTX, nano_to_usd_str
from tokenbill.core.records import to_json
from tokenbill.core.types import MeasurePlan, PanelRow
from tokenbill.verify.estimators import point_estimate
from tokenbill.verify.its import MIN_SIDE_DAYS, its_point
from tokenbill.verify.panel import CLUSTER_FIELDS, org_series

__all__ = [
    "AA_DRAWS",
    "CONTROL_ARM",
    "DESIGNS",
    "ESTIMATORS",
    "HOLDBACK_RANGE",
    "METRIC",
    "ORG_WIDE_RANDOMIZABLE",
    "ORG_WIDE_WARNING",
    "arms_for",
    "assignment_log_text",
    "canonical_json",
    "is_randomized",
    "payload_names",
    "plan",
    "preregistration",
    "projection_per_dev_day",
    "spend_targeting",
    "verify_assignment",
]

DESIGNS = ("cluster_rct", "stepped_wedge", "its")
ESTIMATORS: Mapping[str, str] = {"cluster_rct": "cuped_cluster_dim",
                                 "stepped_wedge": "imputation_did",
                                 "its": "event_study_its"}
METRIC = "cost per active developer-day"
HOLDBACK_RANGE = (Decimal("0.10"), Decimal("0.25"))
AA_DRAWS = 200
MDE_FACTOR = Decimal("2.8")
#: MDE must be ≤ MDE_RATIO_NUM/MDE_RATIO_DEN × |projection| (0.8).
MDE_RATIO_NUM, MDE_RATIO_DEN = 4, 5
SPEND_PERMUTATIONS = 2000
SPEND_ALPHA = 0.05
CONTROL_ARM = "control"
#: Settings refresh interval by delivery (minutes): MDM 30 min, server-managed / gateway 1 h.
REFRESH_MINUTES: Mapping[str, int] = {"mdm_group": 30, "gateway": 60, "workspace": 60,
                                      "team": 60}
#: Cluster kinds through which an org-wide (server-managed) setting can still be randomized.
ORG_WIDE_RANDOMIZABLE = frozenset({"mdm_group", "gateway"})
ORG_WIDE_WARNING = ("org-wide server-managed settings: randomize via MDM groups or the Claude apps "
                    "gateway per IdP group, else design its (MEASURED at best)")
_CONTROL_LABELS = frozenset({"control", "holdback", "0"})
_LEVER_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def canonical_json(obj: object) -> str:
    """Sorted keys, compact separators, UTF-8 text (the pre-registration and log encoding)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _date(value: str, name: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise UsageError(f"{name} must be a YYYY-MM-DD date") from None


def _dec(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise UsageError(f"{name} must be a Decimal or decimal string, not a float")
    try:
        d = Decimal(value) if isinstance(value, (str, int)) else value
    except InvalidOperation:
        raise UsageError(f"{name} is not a decimal") from None
    if not isinstance(d, Decimal) or not d.is_finite():
        raise UsageError(f"{name} is not a finite decimal")
    return d


def _share(num: int, den: int) -> str:
    if den <= 0:
        return "0"
    return str(RATIO_CTX.divide(Decimal(num), Decimal(den)).quantize(Decimal("0.000001")))


def projection_per_dev_day(monthly: Figure, dev_days_per_month: int) -> Figure:
    """A monthly projected saving expressed per active developer-day (exact rational scaling)."""
    if type(dev_days_per_month) is not int or dev_days_per_month <= 0:
        raise UsageError("dev_days_per_month must be a positive int")
    return scale(monthly, 1, dev_days_per_month)


# ---------------------------------------------------------------------------------------------
# spend targeting (regression to the mean)
# ---------------------------------------------------------------------------------------------


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    sa = math.fsum((x - ma) ** 2 for x in a)
    sb = math.fsum((y - mb) ** 2 for y in b)
    if sa <= 0 or sb <= 0:
        return 0.0
    return math.fsum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / math.sqrt(sa * sb)


def spend_targeting(pre_panel: Sequence[PanelRow], assignment: Mapping[str, int], *,
                    seed: int = 0) -> tuple[float, float] | None:
    """Spearman correlation between each cluster's pre-period spend and its treatment priority
    (wave number; the holdback ranks last) with a seeded two-sided permutation p-value:
    ``(rho, p)``, or None with fewer than 4 clusters that have pre-period spend. A negative rho
    means the biggest spenders were treated first."""
    spend: dict[str, int] = {}
    for r in pre_panel:
        if r.cluster_id in assignment:
            spend[r.cluster_id] = spend.get(r.cluster_id, 0) + r.cost_baseline_nano
    clusters = sorted(spend)
    if len(clusters) < 4:
        return None
    s = _ranks([float(spend[c]) for c in clusters])
    p = _ranks([float(assignment[c]) for c in clusters])
    rho = _pearson(s, p)
    rnd = rng(seed, "verify.rollout.spend_targeting")
    extreme = 0
    perm = list(p)
    for _ in range(SPEND_PERMUTATIONS):
        rnd.shuffle(perm)
        if abs(_pearson(s, perm)) >= abs(rho) - 1e-12:
            extreme += 1
    return rho, (extreme + 1) / (SPEND_PERMUTATIONS + 1)


# ---------------------------------------------------------------------------------------------
# A/A re-randomizations
# ---------------------------------------------------------------------------------------------


def _assign(clusters: Sequence[str], n_hold: int, waves: int, rnd) -> tuple[list[str],
                                                                           list[list[str]]]:
    order = sorted(clusters)
    rnd.shuffle(order)
    hold = order[:n_hold]
    treat = order[n_hold:]
    groups: list[list[str]] = [[] for _ in range(waves)]
    for i, c in enumerate(treat):
        groups[i * waves // len(treat)].append(c)
    return hold, groups


def _aa_randomized(pre_panel: Sequence[PanelRow], clusters: Sequence[str], *, design: str,
                   n_hold: int, waves: int, seed: int, lever_id: str) -> list[float]:
    present = sorted({r.cluster_id for r in pre_panel} & set(clusters))
    if len(present) < 2:
        return []
    dates = sorted({r.date_utc for r in pre_panel})
    if len(dates) < 4:
        return []
    k_hold = min(max(1, n_hold * len(present) // max(1, len(clusters))), len(present) - 1)
    w = max(1, min(waves, len(present) - k_hold))
    rows = [r for r in pre_panel if r.cluster_id in set(present)]
    estimates = []
    for draw in range(AA_DRAWS):
        rnd = rng(seed, "verify.rollout.aa", lever_id, design, draw)
        _, groups = _assign(present, k_hold, w, rnd)
        if design == "cluster_rct":
            mid = dates[len(dates) // 2]
            start = {c: mid for g in groups for c in g}
            pre_until: str | None = mid
        else:
            start = {c: dates[(k + 1) * len(dates) // (w + 1)]
                     for k, g in enumerate(groups) for c in g}
            pre_until = None
        fake = [PanelRow(cluster_id=r.cluster_id, date_utc=r.date_utc,
                         cost_baseline_nano=r.cost_baseline_nano,
                         cost_actual_nano=r.cost_actual_nano, active_dev_days=r.active_dev_days,
                         arm=None, wave=None,
                         treated=r.cluster_id in start and r.date_utc >= start[r.cluster_id])
                for r in rows]
        try:
            estimates.append(point_estimate(fake, design=design, pre_until=pre_until))
        except UsageError:
            continue
    return estimates


def _aa_its(pre_panel: Sequence[PanelRow], *, seed: int, lever_id: str) -> list[float]:
    series = org_series(pre_panel)
    dates = [d for d, _, n in series if n > 0]
    lo, hi = MIN_SIDE_DAYS, len(dates) - MIN_SIDE_DAYS
    if hi <= lo:
        return []
    rnd = rng(seed, "verify.rollout.aa", lever_id, "its")
    estimates = []
    for _ in range(AA_DRAWS):
        change = dates[rnd.randrange(lo, hi)]
        try:
            estimates.append(its_point(series, change_date=change))
        except UsageError:
            continue
    return estimates


def _mde(estimates: Sequence[float]) -> int | None:
    if len(estimates) < 20:
        return None
    sd = statistics.stdev(estimates)
    return int((MDE_FACTOR * Decimal(repr(sd))).to_integral_value(ROUND_HALF_UP))


# ---------------------------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------------------------


def _figure_json(fig: Figure | None) -> dict[str, object] | None:
    if fig is None:
        return None
    return to_json(fig)


def _log_lines(*, lever_id: str, design: str, cluster_kind: str, seed: int | None, source: str,
               holdback: Sequence[str], waves: Sequence[tuple[int, Sequence[str]]]) -> str:
    entries = [(c, CONTROL_ARM, 0) for c in holdback]
    entries += [(c, lever_id, n) for n, members in waves for c in members]
    lines = [canonical_json({"arm": arm, "cluster": c, "cluster_kind": cluster_kind,
                             "design": design, "lever": lever_id, "position": i, "seed": seed,
                             "source": source, "wave": n})
             for i, (c, arm, n) in enumerate(entries)]
    return "".join(line + "\n" for line in lines)


def _otel(lever_id: str, holdback: Sequence[str], waves: Sequence[tuple[int, Sequence[str]]]
          ) -> tuple[tuple[str, str], ...]:
    tags = {c: f"tokenbill.arm={CONTROL_ARM},tokenbill.wave=0" for c in holdback}
    for n, members in waves:
        for c in members:
            tags[c] = f"tokenbill.arm={lever_id},tokenbill.wave={n}"
    return tuple(sorted(tags.items()))


def _payloads(lever_id: str, waves: Sequence[tuple[int, Sequence[str]]]) -> list[list[object]]:
    return [[n, f"tokenbill-{lever_id}-wave{n}"] for n, _ in waves]


def _user_assignment(clusters: Sequence[str], treated: Mapping[str, str]
                     ) -> tuple[list[str], list[tuple[int, tuple[str, ...]]]]:
    unknown = sorted(set(treated) - set(clusters))
    if unknown:
        raise UsageError("treated names clusters that are not in the plan")
    missing = sorted(set(clusters) - set(treated))
    if missing:
        raise UsageError("treated must assign every cluster (a wave number or 'control')")
    hold: list[str] = []
    by_wave: dict[int, list[str]] = {}
    for c in sorted(clusters):
        label = str(treated[c]).strip().lower()
        if label in _CONTROL_LABELS:
            hold.append(c)
            continue
        try:
            n = int(label.removeprefix("wave"))
        except ValueError:
            raise UsageError("treated values must be wave numbers or 'control'") from None
        if n < 1:
            raise UsageError("wave numbers start at 1")
        by_wave.setdefault(n, []).append(c)
    if not by_wave:
        raise UsageError("treated assigns no cluster to a wave")
    return hold, [(i + 1, tuple(by_wave[n])) for i, n in enumerate(sorted(by_wave))]


def plan(clusters: Sequence[str], *, lever_id: str, cluster_kind: str, design: str, waves: int,
         holdback: Decimal, seed: int, pre_panel: Sequence[PanelRow] | None,
         projection: Figure | None, washout_hours: int, looks: Sequence[str],
         treated: Mapping[str, str] | None = None, org_wide_delivery: bool = False,
         change_date: str | None = None, rate_card_sha256: str | None = None) -> MeasurePlan:
    """A measurement plan (SPEC §13.2). ``treated`` (cluster → wave number or ``"control"``) is a
    user-supplied assignment: it has no logged seed (MEASURED at best) and is checked for
    correlation with pre-period spend."""
    names = list(clusters)
    if not names or any(not isinstance(c, str) or not c or not _encodable(c) for c in names):
        raise UsageError("clusters must be non-empty UTF-8 strings")
    if len(set(names)) != len(names):
        raise UsageError("clusters must be unique")
    if not isinstance(lever_id, str) or not _LEVER_RE.match(lever_id):
        raise UsageError("lever_id must match [A-Za-z0-9._:-]{1,128}")
    if cluster_kind not in CLUSTER_FIELDS:
        raise UsageError(f"unknown cluster kind {cluster_kind!r}")
    if design not in DESIGNS:
        raise UsageError(f"design must be one of {', '.join(DESIGNS)}")
    if type(waves) is not int or waves < 1:
        raise UsageError("waves must be an int ≥ 1")
    if type(seed) is not int:
        raise UsageError("seed must be an int")
    if type(washout_hours) is not int or washout_hours < 0:
        raise UsageError("washout_hours must be an int ≥ 0")
    hb = _dec(holdback, "holdback")
    look_list = sorted({_date(v, "look").isoformat() for v in looks})
    if projection is not None and not isinstance(projection, Figure):
        raise UsageError("projection must be a Figure")
    names.sort()
    warnings: list[str] = []
    if org_wide_delivery:
        warnings.append(ORG_WIDE_WARNING)
        if cluster_kind not in ORG_WIDE_RANDOMIZABLE:
            design = "its"
    refresh_h = math.ceil(REFRESH_MINUTES.get(cluster_kind, 60) / 60)
    washout = max(washout_hours, 1, refresh_h)
    prereg: dict[str, object] = {
        "lever": lever_id, "metric": METRIC, "estimator": ESTIMATORS[design],
        "looks": look_list, "rate_card_sha256": rate_card_sha256, "projection":
            _figure_json(projection), "design": design, "cluster_kind": cluster_kind,
        "washout_hours": washout, "unit": "nano per active developer-day",
    }
    verification = True
    clusters_needed: int | None = None
    spend_flag: bool | None = None
    if design == "its":
        if treated is not None:
            warnings.append("design its ignores the supplied assignment")
        hold: list[str] = []
        wave_list: list[tuple[int, tuple[str, ...]]] = [(1, tuple(names))]
        source, log_seed = "org_wide", None
        verification = False
        warnings.append("design its: no comparison group, MEASURED at best (placebo date "
                        "pre-registered)")
        pre_dates = sorted({r.date_utc for r in pre_panel}) if pre_panel else []
        if change_date is None and pre_dates:
            change_date = (_date(pre_dates[-1], "date") + _dt.timedelta(days=1)).isoformat()
        placebo = pre_dates[len(pre_dates) // 2] if len(pre_dates) >= 2 * MIN_SIDE_DAYS else None
        if change_date is not None:
            _date(change_date, "change_date")
        else:
            warnings.append("no change date pre-registered")
        if placebo is None:
            warnings.append(f"no placebo date: the pre-period needs ≥ {2 * MIN_SIDE_DAYS} days")
        prereg.update(change_date=change_date, placebo_date=placebo)
        estimates = _aa_its(pre_panel, seed=seed, lever_id=lever_id) if pre_panel else []
    else:
        lo_hb, hi_hb = HOLDBACK_RANGE
        if not lo_hb <= hb <= hi_hb:
            raise UsageError("holdback must be between 0.10 and 0.25")
        if len(names) < 2:
            raise UsageError("a randomized design needs ≥ 2 clusters (use design its)")
        n_hold = int((hb * len(names)).to_integral_value(ROUND_HALF_UP))
        n_hold = min(max(1, n_hold), len(names) - 1)
        if design == "cluster_rct" and waves != 1:
            warnings.append("cluster_rct treats every non-holdback cluster in one wave")
            waves = 1
        waves = min(waves, len(names) - n_hold)
        if treated is not None:
            hold, wave_list = _user_assignment(names, treated)
            source, log_seed = "user", None
            n_hold = len(hold)
            verification = False
            warnings.append("user-supplied assignment has no logged seed: MEASURED at best")
            if pre_panel:
                priority = {c: 0 for c in names}
                last = len(wave_list) + 1
                for n, members in wave_list:
                    for c in members:
                        priority[c] = n
                for c in hold:
                    priority[c] = last
                check = spend_targeting(pre_panel, priority, seed=seed)
                if check is None:
                    warnings.append("too few clusters with pre-period spend to check the "
                                    "assignment for spend targeting")
                else:
                    rho, p = check
                    spend_flag = p < SPEND_ALPHA
                    prereg["spend_rho"] = f"{rho:.4f}"
                    if spend_flag:
                        warnings.append("assignment correlates with pre-period spend (spend-"
                                        "targeted waves): regression to the mean inflates "
                                        "savings; the label is capped at MEASURED")
            else:
                warnings.append("no pre-period panel: the assignment cannot be checked for "
                                "spend targeting")
        else:
            rnd = rng(seed, "verify.rollout.assign", lever_id, design)
            hold, groups = _assign(names, n_hold, waves, rnd)
            wave_list = [(i + 1, tuple(g)) for i, g in enumerate(groups)]
            source, log_seed = "seeded", seed
        estimates = (_aa_randomized(pre_panel, names, design=design, n_hold=n_hold,
                                    waves=len(wave_list), seed=seed, lever_id=lever_id)
                     if pre_panel else [])
        dev = {c: 0 for c in names}
        for r in pre_panel or ():
            if r.cluster_id in dev:
                dev[r.cluster_id] += r.active_dev_days
        total = sum(dev.values())
        if total > 0:
            ctrl = sum(dev[c] for c in hold)
            prereg["arm_shares"] = {CONTROL_ARM: _share(ctrl, total),
                                    "treatment": _share(total - ctrl, total)}
        else:
            prereg["arm_shares"] = {CONTROL_ARM: _share(len(hold), len(names)),
                                    "treatment": _share(len(names) - len(hold), len(names))}
        prereg["holdback"] = str(hb)
    mde = _mde(estimates)
    if pre_panel and mde is None:
        warnings.append("the pre-period panel is too small for 200 A/A re-randomizations")
    if projection is not None and projection.nano is None:
        warnings.append("the projection is unpriced: the MDE guard cannot pass")
        verification = False
    elif projection is not None:
        target = abs(projection.nano)
        if mde is None:
            verification = False
            warnings.append("no pre-period panel: MDE unknown, verification not designed")
        elif MDE_RATIO_DEN * mde > MDE_RATIO_NUM * target:
            verification = False
            if design != "its":
                ratio = (MDE_RATIO_DEN * mde) / max(1, MDE_RATIO_NUM * target)
                clusters_needed = math.ceil(len(names) * ratio * ratio)
                warnings.append(
                    f"MDE ${nano_to_usd_str(mde)} per active developer-day exceeds 0.8 × "
                    f"|projection| (${nano_to_usd_str(target)}): verification needs about "
                    f"{clusters_needed} clusters or a pre-period and window about "
                    f"{math.ceil(ratio * ratio)}× longer")
            else:
                warnings.append(
                    f"MDE ${nano_to_usd_str(mde)} per active developer-day exceeds 0.8 × "
                    f"|projection| (${nano_to_usd_str(target)})")
    else:
        warnings.append("no projection: the MDE guard is not applicable")
    if spend_flag:
        verification = False
    log = _log_lines(lever_id=lever_id, design=design, cluster_kind=cluster_kind, seed=log_seed,
                     source=source, holdback=hold, waves=wave_list)
    log_sha = _sha(log)
    prereg.update(mde=mde, seed=log_seed, assignment=source, assignment_log_sha256=log_sha,
                  spend_targeted=spend_flag, waves=len(wave_list),
                  payloads=_payloads(lever_id, wave_list))
    prereg_json = canonical_json(prereg)
    return MeasurePlan(
        lever_id=lever_id, design=design, cluster_kind=cluster_kind, waves=tuple(wave_list),
        holdback=tuple(hold), washout_hours=washout, looks=tuple(look_list), mde_nano=mde,
        projection=projection, verification_design=verification,
        clusters_needed=clusters_needed, assignment_log_sha256=log_sha,
        preregistration_sha256=_sha(prereg_json), preregistration_json=prereg_json,
        otel_tags=_otel(lever_id, hold, wave_list), warnings=tuple(warnings))


# ---------------------------------------------------------------------------------------------
# plan helpers
# ---------------------------------------------------------------------------------------------


def preregistration(plan_: MeasurePlan) -> dict[str, object]:
    """The parsed pre-registration (``UsageError`` when it does not match its SHA-256)."""
    if _sha(plan_.preregistration_json) != plan_.preregistration_sha256:
        raise UsageError("the pre-registration does not match its SHA-256")
    try:
        data = json.loads(plan_.preregistration_json)
    except ValueError:
        raise UsageError("the pre-registration is not JSON") from None
    if not isinstance(data, dict):
        raise UsageError("the pre-registration is not a JSON object")
    return data


def assignment_log_text(plan_: MeasurePlan) -> str:
    """The assignment log (JSON lines) reconstructed from the plan and its pre-registration."""
    pre = preregistration(plan_)
    seed = pre.get("seed")
    return _log_lines(lever_id=plan_.lever_id, design=plan_.design,
                      cluster_kind=plan_.cluster_kind,
                      seed=seed if isinstance(seed, int) else None,
                      source=str(pre.get("assignment", "")), holdback=plan_.holdback,
                      waves=plan_.waves)


def verify_assignment(plan_: MeasurePlan, log_text: str | None = None) -> bool:
    """True iff the assignment log (given, else reconstructed from the plan) hashes to the
    pre-registered SHA-256 and the pre-registration matches its own SHA-256."""
    try:
        pre = preregistration(plan_)
    except UsageError:
        return False
    text = log_text if log_text is not None else assignment_log_text(plan_)
    digest = _sha(text)
    return digest == plan_.assignment_log_sha256 == pre.get("assignment_log_sha256")


def is_randomized(plan_: MeasurePlan) -> bool:
    """True for a seeded randomized assignment (cluster RCT or stepped wedge)."""
    try:
        pre = preregistration(plan_)
    except UsageError:
        return False
    return plan_.design != "its" and pre.get("assignment") == "seeded" \
        and isinstance(pre.get("seed"), int)


def arms_for(plan_: MeasurePlan, starts: Mapping[int, str] | None = None) -> dict[str, str]:
    """The ``arms`` mapping for ``verify.panel.build_panel``: holdback → ``"control"``, wave *n*
    → the lever id, with ``@<date>`` when *starts* gives wave *n*'s adoption date."""
    out = {c: CONTROL_ARM for c in plan_.holdback}
    for n, members in plan_.waves:
        start = (starts or {}).get(n)
        if start is not None:
            _date(start, "wave start")
        for c in members:
            out[c] = f"{plan_.lever_id}@{start}" if start is not None else plan_.lever_id
    return out


def payload_names(plan_: MeasurePlan) -> tuple[tuple[int, str], ...]:
    """Per-wave MDM payload names, e.g. ``(1, "tokenbill-<lever>-wave1")``."""
    return tuple((n, f"tokenbill-{plan_.lever_id}-wave{n}") for n, _ in plan_.waves)
