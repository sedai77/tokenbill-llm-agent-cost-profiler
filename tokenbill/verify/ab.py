"""Paired lab comparison of two agent configurations (SPEC §13.5).

Inputs: two usage sets (baseline and candidate requests, e.g. from trace@2 files or ledgers) whose
requests carry ``Attribution.extra["task_id"]``, and an outcomes table with one row per run:
``task_id`` (str), ``arm`` (``"baseline"`` | ``"candidate"``), ``trial`` (int), ``success``
(bool or 0/1) and ``order`` (int: the run's position within its trial, 0 = ran first; optional).

Per task and arm the cost of all its requests (priced with *pricer*, point values) is divided by
the arm's trial count; the per-task paired difference is ``candidate − baseline`` per trial. Cost
per success is ``Σ cost / Σ successes`` per arm (failures stay in the numerator). The
task-clustered bootstrap (B = 10,000, seeded) resamples tasks with both arms together; the verdict
comes from the 95% CI of the difference in cost per success: ``costlier`` (CI above 0),
``cheaper`` (below 0) or ``no-difference``.

Label: VERIFIED only at the lab scope ``lab:<sha256 of the sorted task ids>[:12]``, when arm order
was randomized per task (every trial has one run per arm with distinct ``order`` values, and each
arm runs first in some trials), every task-arm has ≥ 5 trials and the CI excludes 0; otherwise
MEASURED. A lab result is never signable as invoice savings, and any fleet projection derived from
it remains ESTIMATED.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tokenbill.common import rng
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Figure
from tokenbill.core.money import RATIO_CTX
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import Request
from tokenbill.core.types import AbResult, GuardResult, MeasurementResult
from tokenbill.verify.estimators import to_nano_triple
from tokenbill.verify.label_policy import decide
from tokenbill.verify.stats import percentile

__all__ = ["ARMS", "MIN_TRIALS", "lab_scope_label", "paired_ab"]

ARMS = ("baseline", "candidate")
MIN_TRIALS = 5
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)


@dataclass(slots=True)
class _Arm:
    cost: int = 0
    tokens: int = 0
    turns: int = 0
    reads: int = 0
    trials: int = 0
    successes: int = 0


def lab_scope_label(task_ids: Sequence[str]) -> str:
    """``"lab:" + sha256("\\n".join(sorted(unique task ids)))[:12]``."""
    text = "\n".join(sorted(set(task_ids)))
    return "lab:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _task_of(req: Request) -> str:
    task = dict(req.attribution.extra).get("task_id")
    if not task:
        raise UsageError("every A/B request needs attribution extra task_id")
    return task


def _usage(requests: Sequence[Request], pricer: Pricer, arm: str, stats: dict[str, _Arm],
           bases: set[Basis], days: list[int]) -> None:
    for req in requests:
        if not isinstance(req, Request):
            raise UsageError("A/B usage sets must hold Request records")
        a = stats.setdefault(_task_of(req), _Arm())
        a.turns += 1
        days.append(req.ts_start_ms // _DAY_MS)
        for att in req.attempts:
            for inf in att.inferences:
                if inf.billable is False:
                    continue
                priced = pricer.price_inference(inf, ts_ms=att.ts_start_ms)
                if priced.figure.nano is None:
                    raise UsageError(f"{arm} usage has an unpriced inference; A/B needs every "
                                     "inference priced")
                bases.add(priced.figure.basis)
                a.cost += priced.figure.nano
                a.tokens += inf.usage.total_input + inf.usage.output
                a.reads += inf.usage.cache_read


def _outcomes(rows: Sequence[Mapping[str, object]]
              ) -> tuple[dict[tuple[str, str], list[bool]], dict[tuple[str, int], dict[str, int]],
                         bool]:
    runs: dict[tuple[str, str], dict[int, bool]] = {}
    orders: dict[tuple[str, int], dict[str, int]] = {}
    has_order = True
    for row in rows:
        if not isinstance(row, Mapping):
            raise UsageError("outcome rows must be mappings")
        task, arm, trial, success = (row.get("task_id"), row.get("arm"), row.get("trial"),
                                     row.get("success"))
        if not isinstance(task, str) or not task:
            raise UsageError("outcome task_id must be a non-empty string")
        if arm not in ARMS:
            raise UsageError("outcome arm must be 'baseline' or 'candidate'")
        if type(trial) is not int or trial < 0:
            raise UsageError("outcome trial must be an int ≥ 0")
        if success not in (True, False, 0, 1):
            raise UsageError("outcome success must be a bool or 0/1")
        trials = runs.setdefault((task, arm), {})
        if trial in trials:
            raise UsageError("duplicate outcome row for a task, arm and trial")
        trials[trial] = bool(success)
        order = row.get("order")
        if type(order) is not int:
            has_order = False
        else:
            orders.setdefault((task, trial), {})[arm] = order  # type: ignore[index]
    flat = {k: [v[t] for t in sorted(v)] for k, v in runs.items()}
    return flat, orders, has_order


def _randomized(orders: Mapping[tuple[str, int], Mapping[str, int]], has_order: bool) -> bool:
    if not has_order or not orders:
        return False
    first: set[str] = set()
    for pair in orders.values():
        if set(pair) != set(ARMS) or pair["baseline"] == pair["candidate"]:
            return False
        first.add(min(pair, key=lambda a: pair[a]))
    return first == set(ARMS)


def _pct(new: Decimal, old: Decimal) -> str:
    if old == 0:
        return "0.00" if new == 0 else "n/a"
    return str((RATIO_CTX.divide(new - old, old) * 100).quantize(Decimal("0.01")))


def _per_trial(total: int, trials: int) -> Decimal:
    return RATIO_CTX.divide(Decimal(total), Decimal(trials))


def paired_ab(baseline: Sequence[Request], candidate: Sequence[Request],
              outcomes: Sequence[Mapping[str, object]], *, pricer: Pricer, boot: int = 10_000,
              seed: int = 0) -> AbResult:
    """Paired lab A/B (see the module docstring)."""
    if type(boot) is not int or boot < 1:
        raise UsageError("boot must be a positive int")
    stats: dict[str, dict[str, _Arm]] = {arm: {} for arm in ARMS}
    bases: set[Basis] = set()
    days: list[int] = []
    _usage(baseline, pricer, "baseline", stats["baseline"], bases, days)
    _usage(candidate, pricer, "candidate", stats["candidate"], bases, days)
    if len(bases) > 1:
        raise UsageError("A/B usage mixes pricing bases (billed and allowance)")
    basis = bases.pop() if bases else pricer.basis
    runs, orders, has_order = _outcomes(outcomes)
    tasks = sorted({t for t, _ in runs})
    if not tasks:
        raise UsageError("no outcome rows")
    for arm in ARMS:
        missing = set(stats[arm]) - {t for t, a in runs if a == arm}
        if missing:
            raise UsageError(f"{arm} requests carry task ids without outcome rows")
        for t in tasks:
            if (t, arm) not in runs:
                raise UsageError("every task needs outcome rows for both arms")
            a = stats[arm].setdefault(t, _Arm())
            a.trials = len(runs[(t, arm)])
            a.successes = sum(runs[(t, arm)])
    b = [stats["baseline"][t] for t in tasks]
    c = [stats["candidate"][t] for t in tasks]

    def cps(arms: Sequence[_Arm], counts: Sequence[int]) -> float | None:
        cost = sum(k * a.cost for a, k in zip(arms, counts, strict=True))
        succ = sum(k * a.successes for a, k in zip(arms, counts, strict=True))
        return cost / succ if succ else None

    def paired(counts: Sequence[int]) -> float:
        n = sum(counts)
        return sum(k * (y.cost / y.trials - x.cost / x.trials)
                   for x, y, k in zip(b, c, counts, strict=True)) / n

    ones = [1] * len(tasks)
    cps_b, cps_c = cps(b, ones), cps(c, ones)
    if cps_b is None or cps_c is None:
        raise UsageError("an arm has no successes: cost per success is undefined")
    point_pd = paired(ones)
    rnd = rng(seed, "verify.ab", lab_scope_label(tasks))
    d_b, d_c, d_diff, d_pd = [], [], [], []
    n = len(tasks)
    for _ in range(boot):
        counts = [0] * n
        for _ in range(n):
            counts[rnd.randrange(n)] += 1
        xb, xc = cps(b, counts), cps(c, counts)
        d_pd.append(paired(counts))
        if xb is None or xc is None:
            continue
        d_b.append(xb)
        d_c.append(xc)
        d_diff.append(xc - xb)
    if not d_diff:
        raise UsageError("no bootstrap replicate has successes in both arms")

    def ci(draws: Sequence[float]) -> tuple[float, float]:
        return percentile(draws, 0.025), percentile(draws, 0.975)

    diff, lo, hi = to_nano_triple(cps_c - cps_b, *ci(d_diff))
    verdict = "costlier" if lo > 0 else "cheaper" if hi < 0 else "no-difference"
    randomized = _randomized(orders, has_order)
    trials = (min(a.trials for a in b), min(a.trials for a in c))
    guard_list = (
        GuardResult("randomized_order", randomized,
                    "arm order randomized per task" if randomized else "fixed or unknown order",
                    "each trial runs both arms in a random order"),
        GuardResult("trials_per_task_arm", min(trials) >= MIN_TRIALS,
                    f"min {min(trials)} (baseline {trials[0]}, candidate {trials[1]})",
                    f"≥ {MIN_TRIALS}"),
        GuardResult("bootstrap_replicates", len(d_diff) == boot, f"{len(d_diff)} of {boot}",
                    "every replicate has successes in both arms"),
    )
    label = decide(design="ab", randomized=randomized, assignment_hash_matches=True,
                   guards=guard_list, ci=(lo, hi))

    def fig(point: float, draws: Sequence[float]) -> Figure:
        p, a, z = to_nano_triple(point, *ci(draws))
        return Figure(nano=p, evidence=label, basis=basis, low_nano=a, high_nano=z,
                      ci_level_pct=95)

    scope = lab_scope_label(tasks)
    saving, s_lo, s_hi = -diff, -hi, -lo
    measurement = MeasurementResult(
        lever_id="unspecified", design="ab", unit="cost per success",
        estimate=Figure(nano=saving, evidence=label, basis=basis, low_nano=s_lo, high_nano=s_hi,
                        ci_level_pct=95, note="saving per success: baseline − candidate"),
        projected=None, realization_rate=None, guards=guard_list,
        scope=(("tasks", n), ("trials", sum(a.trials for a in b) + sum(a.trials for a in c))),
        scope_label=scope,
        window=(("since", (_EPOCH + _dt.timedelta(days=min(days))).isoformat()),
                ("until", (_EPOCH + _dt.timedelta(days=max(days))).isoformat()))
        if days else (),
        rate_card_sha256=pricer.rate_card_sha256, assignment_log_sha256=None,
        preregistration_sha256=None,
        adjustments=(f"task-clustered bootstrap B={boot}",
                     "failures counted in the cost-per-success numerator",
                     "lab scope: fleet projections from this result remain ESTIMATED"),
        rate_variance=None, signable=False)

    def totals(arms: Sequence[_Arm], attr: str) -> Decimal:
        return _per_trial(sum(getattr(a, attr) for a in arms), sum(a.trials for a in arms))

    return AbResult(
        verdict=verdict, scope_label=scope, n_tasks=n, trials_per_arm=trials,
        randomized_order=randomized,
        cost_per_success=(fig(cps_b, d_b), fig(cps_c, d_c)),
        paired_difference=fig(point_pd, d_pd),
        token_delta_pct=_pct(totals(c, "tokens"), totals(b, "tokens")),
        turn_delta_pct=_pct(totals(c, "turns"), totals(b, "turns")),
        read_delta_pct=_pct(totals(c, "reads"), totals(b, "reads")),
        success_delta_pct=_pct(totals(c, "successes"), totals(b, "successes")),
        measurement=measurement)
