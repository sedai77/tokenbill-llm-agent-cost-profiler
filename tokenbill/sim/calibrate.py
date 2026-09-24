"""The model gate: one-step-ahead predictive calibration (SPEC §9.6, D7, D30; package REPLAY).

The gate never reads the predicted transition's own billed reads: for every transition ``i ≥ 1``
with a documented prediction (``Transition.predicted_hit is not None``, SPEC §3.15) the predicted
reads are ``R̂ = E_i`` on a predicted hit, else the static-prefix floor ``S``; predicted writes are
the rest of the prefix in the observed write bucket of request ``i`` (or the bucket of ``τ_i`` when
it wrote nothing); uncached input and output are the billed ones. Predicted and billed serving
costs (points) are summed per period (UTC day or month) and compared with NMBE and CV(RMSE)
against the FEMP thresholds (ASHRAE Guideline 14; the daily row reuses the hourly thresholds).

``calibrate`` makes two streaming passes over ``lane_batches()``: pass 1 collects billed and
documented sums, ρ counts per (fold, gap band), the diagnostics confusion matrix and the TTL
corroboration; ρ is then fitted per fold on the *other* folds (days are assigned to folds by
``day_ordinal mod folds``) and pass 2 sums the out-of-fold calibrated predictions
``ρ·cost(R̂=E) + (1 − ρ)·cost(R̂=S)`` (rounded once per transition). Partials merge by addition, so
the report is identical for any batching. The status is ``pass`` if the documented variant passes
(mode ``documented``), else if the calibrated one does (mode ``calibrated``), else ``fail``; fewer
than 12 periods give ``insufficient_data``. Only a passing report makes figures CALIBRATED.

Everything is integer / Fraction / Decimal arithmetic: no floats, deterministic decimal strings.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction
from typing import Any

from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import UsageError
from tokenbill.core.evidence import FEMP_HOURLY_DAILY, FEMP_MONTHLY, MIN_CALIBRATION_PERIODS
from tokenbill.core.protocols import CacheRulesProvider, Pricer
from tokenbill.core.records import Lane, LaneEventKind, Request
from tokenbill.core.transitions import DIAG_CAUSES, classify_transitions
from tokenbill.core.types import CalibrationPartial, CalibrationReport, Transition
from tokenbill.sim.usage_replay import (
    _PREF_5M,
    _R,
    _U,
    GAP_BANDS,
    _dominant,
    _pref_of_ttl,
    _PriceBook,
    _round_half_even,
    _total_input,
    _tup,
    _with_split,
    gap_band,
)

__all__ = [
    "GRANULARITIES",
    "LaneBatches",
    "PR_CLASSES",
    "calibrate",
    "calibrate_lanes",
    "calibrate_pass1",
    "calibrate_pass2",
    "finish_calibration",
    "fit_rho_by_fold",
    "merge_partials",
]

LaneBatches = Callable[[], Iterable[Sequence[Lane]]]   # re-iterable: called once per pass
GRANULARITIES = ("day", "month")
#: Precision/recall classes: (canonical server reason, Token Bill cause) (§9.6 #7).
PR_CLASSES: tuple[tuple[str, str], ...] = (
    ("model_changed", "model-switch"),
    ("tools_changed", "tools-changed"),
    ("system_changed", "system-changed"),
    ("messages_changed", "messages-changed"),
    ("param_changed", "param-change"),
)
_NO_COMPARISON = frozenset({"previous_message_not_found", "unavailable"})
_NOT_COMPARED = _NO_COMPARISON | {"key_changed", "compacted"}
_REBUILD_LABELS = frozenset({"messages_changed", "system_changed"})
_MIN_BAND_TRIALS = 30
_DAY_MS = 86_400_000
_CTX = Context(prec=40, rounding=ROUND_HALF_EVEN)
_Q4 = Decimal("0.0001")
_Z = Decimal("1.96")
_MIN_PERIODS: int = MIN_CALIBRATION_PERIODS.value  # type: ignore[assignment]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def _check(granularity: str, folds: int) -> None:
    if granularity not in GRANULARITIES:
        raise UsageError("calibrate: granularity must be 'day' or 'month'")
    if type(folds) is not int or folds < 2:
        raise UsageError("calibrate: folds must be an int >= 2")


def _period(ts_ms: int, granularity: str) -> str:
    day = _dt.date(1970, 1, 1) + _dt.timedelta(days=ts_ms // _DAY_MS)
    return day.isoformat() if granularity == "day" else day.isoformat()[:7]


def _dec(value: Decimal) -> str:
    """A Decimal rounded half-even to 4 places, as a plain string without trailing zeros."""
    q = value.quantize(_Q4, context=_CTX)
    if q == 0:
        return "0"
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _ratio(num: int, den: int) -> str:
    return _dec(_CTX.divide(Decimal(num), Decimal(den))) if den else "n/a"


def _provider_of(source: str, fallback: str) -> str:
    head = source.split(".", 1)[0] if isinstance(source, str) and source else ""
    return head or fallback or "unknown"


def _rule1_resets(lane: Lane, steps: Sequence[Request]) -> list[bool]:
    """§3.15 rule 1 per step: a COMPACTION / CLEAR / CONTEXT_EDIT event in ``(ts_{i−1}, ts_i]``,
    applied context edits or dropped thinking blocks on request ``i``."""
    out = []
    events = lane.events
    prev_ts: int | None = None
    for req in steps:
        ts = req.ts_start_ms
        reset = any(att.applied_edits or att.thinking_dropped > 0 for att in req.attempts)
        if not reset and prev_ts is not None:
            reset = any(prev_ts < ev.ts_ms <= ts and ev.kind in (
                LaneEventKind.COMPACTION, LaneEventKind.CLEAR, LaneEventKind.CONTEXT_EDIT)
                for ev in events)
        out.append(reset)
        prev_ts = ts
    return out


def _undiagnosed_cause(t: Transition, reset: bool, prev_total: int) -> str:
    """Token Bill's cause of *t* without its diagnostic label (§3.15 rules 1–4, 6, 7): the rule-5
    mapping of the label itself is undone so a label is never compared with itself."""
    if not t.is_miss_event:
        return "hit"
    reason = t.diag_reason
    if reason in DIAG_CAUSES and (t.cause, t.sub_cause) == DIAG_CAUSES[reason]:
        if reason == "compacted" and reset:
            return "compaction"   # rule 1 fired before rule 5
        return "context-shrank" if 10 * t.total < 9 * prev_total else "unexplained"
    return t.cause


class _Walker:
    """Per-transition quantities shared by both passes."""

    def __init__(self, *, pricer: Pricer, rules: CacheRulesProvider | None, granularity: str,
                 folds: int, static_prefix_floor: Mapping[tuple[str, str], int] | None) -> None:
        _check(granularity, folds)
        self.pricer = pricer
        self.rules: CacheRulesProvider = rules if rules is not None else RulesTable()
        self.granularity = granularity
        self.folds = folds
        self.floor = dict(static_prefix_floor) if static_prefix_floor else {}
        self.book = _PriceBook(pricer)

    def walk(self, lanes: Sequence[Lane], *, with_labels: bool
             ) -> Iterable[tuple[Transition, dict[str, Any]]]:
        """Yield every transition of *lanes* with its participation data (``None`` values when
        it does not participate in the cost comparison)."""
        for lane in lanes:
            if not isinstance(lane, Lane):
                raise UsageError("calibrate: lanes must be Lane records")
            transitions = classify_transitions(lane, pricer=self.pricer, rules=self.rules,
                                               static_prefix_floor=self.floor)
            if not transitions:
                continue
            steps = [req for req in lane.requests if req.serving_inference is not None]
            resets: list[bool] | None = None
            for t in transitions:
                req = steps[t.index]
                info: dict[str, Any] = {"req": req}
                if with_labels and t.diag_reason is not None:
                    if resets is None:
                        resets = _rule1_resets(lane, steps)
                    prev_inf = steps[t.index - 1].serving_inference
                    assert prev_inf is not None
                    info["cause"] = _undiagnosed_cause(t, resets[t.index],
                                                       prev_inf.usage.total_input)
                if t.predicted_hit is not None:
                    info.update(self._costs(lane, req, t))
                yield t, info

    def _costs(self, lane: Lane, req: Request, t: Transition) -> dict[str, Any]:
        inf = req.serving_inference
        assert inf is not None
        ts = req.attempts[-1].ts_start_ms
        obs = _tup(inf.usage)
        billed = self.book.price(obs, inf.pricing, ts, inf.billable, inf.usage_source,
                                 inf.output_upper)
        if billed is None:
            return {}
        prefix = _total_input(obs) - obs[_U]
        pref = _dominant(obs) or (_pref_of_ttl(t.ttl_s) if t.ttl_s else None) or _PREF_5M
        floor_s = self.floor.get((lane.cache_scope_key, req.model), 0)

        def cost(reads: int) -> int | None:
            reads = max(0, min(reads, prefix))
            if reads == obs[_R]:
                return billed[0]
            usage = _with_split(obs, obs[_U], reads, prefix - reads, pref)
            got = self.book.price(usage, inf.pricing, ts, inf.billable, inf.usage_source,
                                  inf.output_upper)
            return got[0] if got is not None else None

        cost_e = cost(t.expected_reuse)
        cost_s = cost(floor_s)
        if cost_e is None or cost_s is None:
            return {}
        day = req.ts_start_ms // _DAY_MS
        return {"period": _period(req.ts_start_ms, self.granularity),
                "fold": day % self.folds, "band": gap_band(t.gap_ms), "billed": billed[0],
                "cost_e": cost_e, "cost_s": cost_s}


def _sorted_pairs(d: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(d.items()))


# ---------------------------------------------------------------------------------------------
# passes
# ---------------------------------------------------------------------------------------------


def calibrate_pass1(lanes: Sequence[Lane], *, pricer: Pricer, rules: CacheRulesProvider | None,
                    granularity: str = "day", folds: int = 5, seed: int = 0,
                    static_prefix_floor: Mapping[tuple[str, str], int] | None = None
                    ) -> CalibrationPartial:
    """Pass 1 over one batch of lanes: billed and documented sums per period, ρ counts per
    (fold, gap band), the confusion matrix, TTL corroboration and label counts.

    Confusion entries carry the provider-qualified reason ``"<provider>:<reason>"`` (so precision
    and recall can be reported per provider after merging); :func:`finish_calibration` strips
    it."""
    w = _Walker(pricer=pricer, rules=rules, granularity=granularity, folds=folds,
                static_prefix_floor=static_prefix_floor)
    billed: dict[str, int] = {}
    documented: dict[str, int] = {}
    rho: dict[tuple[int, str], list[int]] = {}
    confusion: dict[tuple[str, str], int] = {}
    corroborated = ttl_labeled = unlabeled = no_comparison = 0
    for t, info in w.walk(lanes, with_labels=True):
        if "period" in info:
            period = info["period"]
            billed[period] = billed.get(period, 0) + info["billed"]
            predicted = info["cost_e"] if t.predicted_hit else info["cost_s"]
            documented[period] = documented.get(period, 0) + predicted
            if t.predicted_hit and not t.ambiguous:
                slot = rho.setdefault((info["fold"], info["band"]), [0, 0])
                slot[0] += 0 if t.is_miss_event else 1
                slot[1] += 1
        if t.diag_reason is None:
            unlabeled += 1
            continue
        req: Request = info["req"]
        diag = req.final_attempt.diagnostics
        assert diag is not None
        si = req.serving_inference
        provider = _provider_of(diag.source, si.pricing.provider if si is not None else "")
        cause = info["cause"]
        key = (cause, f"{provider}:{t.diag_reason}")
        confusion[key] = confusion.get(key, 0) + 1
        if t.diag_reason in _NO_COMPARISON:
            no_comparison += 1
        if cause == "ttl-expiry":
            ttl_labeled += 1
            if t.diag_reason == "previous_message_not_found":
                corroborated += 1
    return CalibrationPartial(
        granularity=granularity,
        period_billed=_sorted_pairs(billed),
        period_documented=_sorted_pairs(documented),
        period_calibrated=(),
        rho_counts=tuple(sorted((fold, band, h, n) for (fold, band), (h, n) in rho.items())),
        confusion=tuple(sorted((c, r, n) for (c, r), n in confusion.items())),
        ttl_corroboration=(corroborated, ttl_labeled),
        unlabeled=unlabeled,
        no_comparison_labels=no_comparison,
    )


def calibrate_pass2(lanes: Sequence[Lane], rho_by_fold: Mapping[int, Mapping[str, Decimal]], *,
                    pricer: Pricer, rules: CacheRulesProvider | None, granularity: str = "day",
                    folds: int = 5, seed: int = 0,
                    static_prefix_floor: Mapping[tuple[str, str], int] | None = None
                    ) -> CalibrationPartial:
    """Pass 2 over one batch: out-of-fold calibrated sums per period, ``ρ·cost(R̂=E) +
    (1 − ρ)·cost(R̂=S)`` for predicted hits (ρ of the transition's fold and gap band, fitted on
    the other folds; rounded half-even once per transition), ``cost(R̂=S)`` for predicted
    misses."""
    w = _Walker(pricer=pricer, rules=rules, granularity=granularity, folds=folds,
                static_prefix_floor=static_prefix_floor)
    calibrated: dict[str, int] = {}
    for t, info in w.walk(lanes, with_labels=False):
        if "period" not in info:
            continue
        if t.predicted_hit:
            rho = Fraction(rho_by_fold.get(info["fold"], {}).get(info["band"], Decimal(1)))
            num, den = rho.numerator, rho.denominator
            value = _round_half_even(num * info["cost_e"] + (den - num) * info["cost_s"], den)
        else:
            value = info["cost_s"]
        period = info["period"]
        calibrated[period] = calibrated.get(period, 0) + value
    return CalibrationPartial(
        granularity=granularity, period_billed=(), period_documented=(),
        period_calibrated=_sorted_pairs(calibrated), rho_counts=(), confusion=(),
        ttl_corroboration=(0, 0), unlabeled=0, no_comparison_labels=0)


def merge_partials(parts: Sequence[CalibrationPartial]) -> CalibrationPartial:
    """Field-wise sum of partials of one granularity (the merge is associative and
    commutative, so any batching gives the same result)."""
    parts = list(parts)
    if not parts:
        raise UsageError("merge_partials: no partials")
    granularity = parts[0].granularity
    billed: dict[str, int] = {}
    documented: dict[str, int] = {}
    calibrated: dict[str, int] = {}
    rho: dict[tuple[int, str], list[int]] = {}
    confusion: dict[tuple[str, str], int] = {}
    corr = [0, 0]
    unlabeled = no_comparison = 0
    for part in parts:
        if part.granularity != granularity:
            raise UsageError("merge_partials: partials of different granularities")
        for target, pairs in ((billed, part.period_billed), (documented, part.period_documented),
                              (calibrated, part.period_calibrated)):
            for period, value in pairs:
                target[period] = target.get(period, 0) + value
        for fold, band, h, n in part.rho_counts:
            slot = rho.setdefault((fold, band), [0, 0])
            slot[0] += h
            slot[1] += n
        for cause, reason, n in part.confusion:
            confusion[(cause, reason)] = confusion.get((cause, reason), 0) + n
        corr[0] += part.ttl_corroboration[0]
        corr[1] += part.ttl_corroboration[1]
        unlabeled += part.unlabeled
        no_comparison += part.no_comparison_labels
    return CalibrationPartial(
        granularity=granularity,
        period_billed=_sorted_pairs(billed),
        period_documented=_sorted_pairs(documented),
        period_calibrated=_sorted_pairs(calibrated),
        rho_counts=tuple(sorted((f, b, h, n) for (f, b), (h, n) in rho.items())),
        confusion=tuple(sorted((c, r, n) for (c, r), n in confusion.items())),
        ttl_corroboration=(corr[0], corr[1]),
        unlabeled=unlabeled,
        no_comparison_labels=no_comparison,
    )


def _band_rho(counts: Mapping[str, tuple[int, int]]) -> dict[str, Decimal]:
    hits = sum(h for h, _n in counts.values())
    trials = sum(n for _h, n in counts.values())
    pooled = _CTX.divide(Decimal(hits), Decimal(trials)) if trials else Decimal(1)
    out = {}
    for label, _lo, _hi in GAP_BANDS:
        h, n = counts.get(label, (0, 0))
        out[label] = _CTX.divide(Decimal(h), Decimal(n)) if n >= _MIN_BAND_TRIALS else pooled
    return out


def fit_rho_by_fold(parts1: Sequence[CalibrationPartial], *, folds: int
                    ) -> dict[int, dict[str, Decimal]]:
    """fold → gap band → ρ fitted on the other folds' pass-1 counts (bands with fewer than 30
    trials use the pooled ρ of those folds; 1 when there is nothing to fit)."""
    if type(folds) is not int or folds < 2:
        raise UsageError("calibrate: folds must be an int >= 2")
    merged = merge_partials(parts1) if parts1 else None
    counts = merged.rho_counts if merged is not None else ()
    out: dict[int, dict[str, Decimal]] = {}
    for k in range(folds):
        acc: dict[str, list[int]] = {}
        for fold, band, h, n in counts:
            if fold != k:
                slot = acc.setdefault(band, [0, 0])
                slot[0] += h
                slot[1] += n
        out[k] = _band_rho({b: (v[0], v[1]) for b, v in acc.items()})
    return out


def _metrics(billed: Mapping[str, int], predicted: Mapping[str, int]
             ) -> tuple[Decimal, Decimal] | None:
    """(NMBE %, CV(RMSE) %) with p = 0 over the periods of *billed*; None when Σ billed is 0."""
    n = len(billed)
    total = sum(billed.values())
    if n == 0 or total == 0:
        return None
    diff = sq = 0
    for period, b in billed.items():
        d = b - predicted.get(period, 0)
        diff += d
        sq += d * d
    nmbe = _CTX.divide(Decimal(100 * diff), Decimal(total))
    rmse = _CTX.divide(Decimal(sq), Decimal(n)).sqrt(_CTX)
    cv = _CTX.divide(_CTX.multiply(rmse, Decimal(100 * n)), Decimal(total))
    return nmbe, cv


def _wilson(hits: int, trials: int) -> tuple[str, str]:
    """95% Wilson score interval of ``hits / trials`` as 4-place decimal strings."""
    if trials <= 0:
        return "0", "1"
    n = Decimal(trials)
    p = _CTX.divide(Decimal(hits), n)
    z2 = _Z * _Z
    denom = 1 + _CTX.divide(z2, n)
    center = _CTX.divide(p + _CTX.divide(z2, 2 * n), denom)
    spread = _CTX.divide(p * (1 - p), n) + _CTX.divide(z2, 4 * n * n)
    half = _CTX.divide(_Z * spread.sqrt(_CTX), denom)
    return _dec(max(Decimal(0), center - half)), _dec(min(Decimal(1), center + half))


def _precision_recall(confusion: Mapping[tuple[str, str], int]) -> tuple[tuple[str, str, str], ...]:
    by_provider: dict[str, dict[tuple[str, str], int]] = {}
    for (cause, qualified), n in confusion.items():
        provider, _sep, reason = qualified.partition(":")
        by_provider.setdefault(provider, {})
        key = (cause, reason)
        by_provider[provider][key] = by_provider[provider].get(key, 0) + n
    providers = sorted(by_provider)
    out: list[tuple[str, str, str]] = []
    for provider in providers:
        cells = {k: n for k, n in by_provider[provider].items()
                 if k[1] not in _NOT_COMPARED
                 and not (k[0] == "compaction" and k[1] in _REBUILD_LABELS)}
        for label, cause in PR_CLASSES:
            predicted = sum(n for (c, _r), n in cells.items() if c == cause)
            actual = sum(n for (_c, r), n in cells.items() if r == label)
            if predicted == 0 and actual == 0:
                continue
            tp = cells.get((cause, label), 0)
            name = f"{provider}:{label}" if len(providers) > 1 else label
            out.append((name, _ratio(tp, predicted), _ratio(tp, actual)))
    return tuple(out)


def finish_calibration(parts1: Sequence[CalibrationPartial], parts2: Sequence[CalibrationPartial],
                       *, granularity: str, folds: int) -> CalibrationReport:
    """Merge the partials of both passes and evaluate the gate (§9.6 #3–#7)."""
    _check(granularity, folds)
    for part in list(parts1) + list(parts2):
        if part.granularity != granularity:
            raise UsageError("finish_calibration: partials of another granularity")
    empty = CalibrationPartial(granularity=granularity, period_billed=(), period_documented=(),
                               period_calibrated=(), rho_counts=(), confusion=(),
                               ttl_corroboration=(0, 0), unlabeled=0, no_comparison_labels=0)
    one = merge_partials([empty, *parts1])
    two = merge_partials([empty, *parts2])
    billed = dict(one.period_billed)
    documented = dict(one.period_documented)
    calibrated = dict(two.period_calibrated)
    n = len(billed)
    femp = FEMP_HOURLY_DAILY if granularity == "day" else FEMP_MONTHLY
    t_nmbe, t_cv = femp.value  # type: ignore[misc]
    thresholds = (str(t_nmbe), str(t_cv))
    notes = ["NMBE and CV(RMSE) with p = 0 on serving-inference cost points per period",
             "thresholds: FEMP Table 4-2 (ASHRAE Guideline 14)"
             + ("; the daily row reuses the hourly thresholds" if granularity == "day" else ""),
             f"calibrated variant: ρ fitted out of fold (day ordinal mod {folds})",
             f"gap bands with fewer than {_MIN_BAND_TRIALS} trials use the pooled ρ"]
    doc = _metrics(billed, documented)
    cal = _metrics(billed, calibrated)

    def passes(m: tuple[Decimal, Decimal] | None) -> bool:
        return m is not None and abs(m[0]) <= t_nmbe and m[1] <= t_cv

    if n < _MIN_PERIODS:
        status, mode = "insufficient_data", None
        notes.append(f"{n} periods < {_MIN_PERIODS}: projections UNCALIBRATED")
    elif doc is None:
        status, mode = "insufficient_data", None
        notes.append("no billed cost in the comparison: projections UNCALIBRATED")
    elif passes(doc):
        status, mode = "pass", "documented"
    elif passes(cal):
        status, mode = "pass", "calibrated"
    else:
        status, mode = "fail", None
    band_counts: dict[str, list[int]] = {}
    for _fold, band, h, t in one.rho_counts:
        slot = band_counts.setdefault(band, [0, 0])
        slot[0] += h
        slot[1] += t
    pooled_h = sum(v[0] for v in band_counts.values())
    pooled_t = sum(v[1] for v in band_counts.values())
    rho_rows = []
    for label, _lo, _hi in GAP_BANDS:
        h, t = band_counts.get(label, [0, 0])
        low, high = _wilson(h, t) if t >= _MIN_BAND_TRIALS else _wilson(pooled_h, pooled_t)
        rho_rows.append((label, h, t, low, high))
    plain: dict[tuple[str, str], int] = {}
    qualified: dict[tuple[str, str], int] = {}
    providers = set()
    for cause, reason, count in one.confusion:
        provider, _sep, bare = reason.partition(":")
        if not _sep:
            provider, bare = "unknown", reason
        providers.add(provider)
        plain[(cause, bare)] = plain.get((cause, bare), 0) + count
        qualified[(cause, f"{provider}:{bare}")] = \
            qualified.get((cause, f"{provider}:{bare}"), 0) + count
    if len(providers) > 1:
        notes.append("precision/recall per provider: " + ", ".join(sorted(providers)))
    if any(r == "key_changed" for _c, r in plain):
        notes.append("key_changed labels have no Token Bill equivalent (reported, not scored)")
    return CalibrationReport(
        granularity=granularity,
        n_periods=n,
        status=status,
        mode_used=mode,
        nmbe_pct=_dec(doc[0]) if doc is not None else None,
        cvrmse_pct=_dec(doc[1]) if doc is not None else None,
        nmbe_pct_calibrated=_dec(cal[0]) if cal is not None and calibrated else None,
        cvrmse_pct_calibrated=_dec(cal[1]) if cal is not None and calibrated else None,
        thresholds=thresholds,
        rho=tuple(rho_rows),
        diag_confusion=tuple(sorted((c, r, k) for (c, r), k in plain.items())),
        diag_precision_recall=_precision_recall(qualified),
        unlabeled=one.unlabeled,
        no_comparison_labels=one.no_comparison_labels,
        ttl_corroboration=one.ttl_corroboration,
        notes=tuple(notes),
    )


def calibrate(lane_batches: LaneBatches, *, pricer: Pricer, rules: CacheRulesProvider | None,
              granularity: str = "day", folds: int = 5, seed: int = 0,
              static_prefix_floor: Mapping[tuple[str, str], int] | None = None
              ) -> CalibrationReport:
    """The model gate over two streaming passes of ``lane_batches()`` (the pipeline passes store
    shards; tests pass ``lambda: [lanes]``). *seed* is accepted for API stability: the fold
    assignment is deterministic (day ordinal mod *folds*) and nothing else is random."""
    _check(granularity, folds)
    if not callable(lane_batches):
        raise UsageError("calibrate: lane_batches must be a callable returning lane batches")
    kw: dict[str, Any] = {"pricer": pricer, "rules": rules, "granularity": granularity,
                          "folds": folds, "seed": seed,
                          "static_prefix_floor": static_prefix_floor}
    parts1 = [calibrate_pass1(batch, **kw) for batch in lane_batches()]
    rho = fit_rho_by_fold(parts1, folds=folds)
    parts2 = [calibrate_pass2(batch, rho, **kw) for batch in lane_batches()]
    return finish_calibration(parts1, parts2, granularity=granularity, folds=folds)


def calibrate_lanes(lanes: Sequence[Lane], **kw: Any) -> CalibrationReport:
    """``calibrate(lambda: [lanes], **kw)``."""
    lanes = list(lanes)
    return calibrate(lambda: [lanes], **kw)
