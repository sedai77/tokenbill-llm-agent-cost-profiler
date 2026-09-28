"""The ``tokenbill check`` CI gate (SPEC §15.2; package CLI-SAVINGS).

:func:`run_check` judges the lanes of a recorded smoke test (trace@1 / trace@2 files read by the
TRACE adapters, or a ledger) against four rules and returns a
:class:`~tokenbill.core.types.CheckResult`:

1. ``TB-CACHE-SHARE`` — the cache-read share of the input tokens of every serving inference at
   turn index ≥ ``after_turn`` (0-based position in its run; the warm-up turns are skipped) must be
   at least ``min_cache_read_share``. No qualifying request → the check is skipped with a
   warning.
2. ``TB-NEW-BREAKER`` — no cache-breaker kind that the baseline does not have. Fingerprinted
   lanes run the block-level breaker suite (``block.breakers``, dollar floor 0 so every kind is
   seen); usage-only lanes compare the causes of their miss events (``core.transitions``) as
   ``miss:<cause>`` kinds, except ``ttl-expiry``, ``compaction`` and ``directory-change`` (idle
   time and working-directory changes are not regressions of the agent's prompt). Without a
   baseline every kind found is new.
3. ``TB-COST-REGRESSION`` — the median exact cost per run must not exceed the baseline median by
   more than ``max_cost_regression_pct``. Medians need at least ``min_runs`` runs on both sides;
   fewer → the check is skipped with a warning (never a failure).
4. ``TB-SERIALIZATION-CHURN`` — the ``serialization-churn`` breaker kind (fingerprinted lanes):
   an unchanged prefix serialized differently between calls. Reported whenever present.

A run is a session (trace@1: one ``run_id``). ``fail_on`` selects which rule families fail the
gate (``share``, ``breaker`` — new breakers and churn —, ``regression``, or ``any``); violations of
the other families are downgraded to warnings. The gate passes when no violation has level
``error``. Locations are ``<file name>#run=<session>#call=<index>`` (content-free).

The baseline is a previous ``check --format json`` document (``tokenbill/result@2`` with a
``check`` object) or that ``check`` object alone; :func:`parse_baseline` validates it and raises
:class:`~tokenbill.core.errors.UsageError` for anything else.

Money is int nano; the share and percentages are exact decimal strings (no floats).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.jsonl import load_json_exact
from tokenbill.core.money import fmt_usd
from tokenbill.core.protocols import CacheRulesProvider, Pricer
from tokenbill.core.records import Lane
from tokenbill.core.types import AnalysisContext, CheckResult, CheckViolation

__all__ = [
    "BLOCK_DETECTOR",
    "CHURN_KIND",
    "FAIL_ON",
    "MISS_PREFIX",
    "RULE_CACHE_SHARE",
    "RULE_CHURN",
    "RULE_COST",
    "RULE_NEW_BREAKER",
    "USAGE_EXCLUDED_CAUSES",
    "Baseline",
    "CheckOptions",
    "load_baseline",
    "parse_baseline",
    "run_check",
]

RULE_CACHE_SHARE = "TB-CACHE-SHARE"
RULE_NEW_BREAKER = "TB-NEW-BREAKER"
RULE_COST = "TB-COST-REGRESSION"
RULE_CHURN = "TB-SERIALIZATION-CHURN"
#: ``--fail-on`` values.
FAIL_ON = ("breaker", "regression", "share", "any")
#: Registry id of the block-level breaker suite (BLOCK).
BLOCK_DETECTOR = "block.breakers"
#: The breaker kind reported under ``TB-SERIALIZATION-CHURN``.
CHURN_KIND = "serialization-churn"
#: Prefix of usage-level (miss-cause) breaker kinds.
MISS_PREFIX = "miss:"
#: Miss causes never compared on usage-only inputs (SPEC §15.2).
USAGE_EXCLUDED_CAUSES = frozenset({"ttl-expiry", "compaction", "directory-change"})

_FAMILY = {RULE_CACHE_SHARE: "share", RULE_NEW_BREAKER: "breaker", RULE_CHURN: "breaker",
           RULE_COST: "regression"}
_KIND_RE = re.compile(r"[a-z0-9][a-z0-9:._-]{0,63}\Z")
_BASELINE_MAX_BYTES = 64 << 20
_MAX_RUNS = 2**31
_SHARE_Q = Decimal("0.0001")
_PCT_MAX = Decimal(1_000_000)


@dataclass(frozen=True)
class CheckOptions:
    """The thresholds of one ``check`` run (SPEC §15 flags; defaults as documented)."""

    min_cache_read_share: Decimal = Decimal("0.80")
    after_turn: int = 2
    max_cost_regression_pct: Decimal = Decimal("15")
    min_runs: int = 3
    fail_on: str = "any"

    def __post_init__(self) -> None:
        share = _decimal(self.min_cache_read_share, "--min-cache-read-share")
        if not Decimal(0) <= share <= Decimal(1):
            raise UsageError("--min-cache-read-share must be between 0 and 1")
        pct = _decimal(self.max_cost_regression_pct, "--max-cost-regression-pct")
        if not Decimal(0) <= pct <= _PCT_MAX:
            raise UsageError("--max-cost-regression-pct must be between 0 and 1000000")
        object.__setattr__(self, "min_cache_read_share", share)
        object.__setattr__(self, "max_cost_regression_pct", pct)
        if type(self.after_turn) is not int or not 0 <= self.after_turn <= 1_000_000:
            raise UsageError("--after-turn must be an int between 0 and 1000000")
        if type(self.min_runs) is not int or not 1 <= self.min_runs <= 1_000_000:
            raise UsageError("--min-runs must be an int between 1 and 1000000")
        if self.fail_on not in FAIL_ON:
            raise UsageError(f"--fail-on must be one of {', '.join(FAIL_ON)}")


def _decimal(value: object, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise UsageError(f"{what} must be a decimal number")
    try:
        out = Decimal(value.strip() if isinstance(value, str) else value)
    except (InvalidOperation, ValueError):
        raise UsageError(f"{what} must be a decimal number") from None
    if not out.is_finite():
        raise UsageError(f"{what} must be a finite decimal number")
    return out


# ---------------------------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    """What a previous ``check`` recorded: its breaker kinds, run count and median cost."""

    breaker_kinds: frozenset[str]
    runs: int
    median_cost_nano: int | None


def _bad_baseline(why: str) -> UsageError:
    return UsageError(f"baseline: not a tokenbill check baseline ({why})")


def parse_baseline(doc: object) -> Baseline:
    """The :class:`Baseline` of a ``check --format json`` document (or its ``check`` object).
    Anything else raises ``UsageError`` (content-free: the message never echoes values)."""
    if not isinstance(doc, Mapping):
        raise _bad_baseline("not a JSON object")
    check = doc.get("check", doc) if "check" in doc else doc
    if not isinstance(check, Mapping):
        raise _bad_baseline("no check object")
    kinds = check.get("breaker_kinds")
    if not isinstance(kinds, list) or len(kinds) > 10_000:
        raise _bad_baseline("breaker_kinds must be a list")
    if not all(isinstance(k, str) and _KIND_RE.match(k) for k in kinds):
        raise _bad_baseline("breaker_kinds must hold kind names")
    runs = check.get("runs")
    if type(runs) is not int or not 0 <= runs <= _MAX_RUNS:
        raise _bad_baseline("runs must be a non-negative int")
    median = check.get("median_cost")
    nano: int | None = None
    if median is not None:
        if not isinstance(median, Mapping):
            raise _bad_baseline("median_cost must be a money object")
        value = median.get("nano")
        if type(value) is not int or value < 0 or value > 2**63:
            raise _bad_baseline("median_cost.nano must be a non-negative int")
        nano = value
    return Baseline(breaker_kinds=frozenset(kinds), runs=runs, median_cost_nano=nano)


def load_baseline(path: Path) -> Baseline:
    """Read and parse a baseline file (JSON, at most 64 MiB); errors are ``UsageError``."""
    path = Path(path)
    try:
        doc = load_json_exact(path, max_bytes=_BASELINE_MAX_BYTES)
    except SourceError as exc:
        raise UsageError(f"baseline {exc}") from None
    return parse_baseline(doc)


# ---------------------------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------------------------


@dataclass
class _Where:
    """Request id → location, built from the lanes."""

    by_request: dict[str, str]
    first: str

    @classmethod
    def of(cls, lanes: Sequence[Lane], files: Mapping[str, str]) -> _Where:
        where: dict[str, str] = {}
        first = ""
        for lane in lanes:
            name = files.get(lane.session_key, "ledger")
            for i, req in enumerate(lane.requests):
                loc = f"{name}#run={lane.session_key}#call={i}"
                where.setdefault(req.request_id, loc)
                if not first:
                    first = f"{name}#run={lane.session_key}"
        return cls(where, first or "ledger")

    def at(self, request_id: str | None) -> str:
        if request_id is None:
            return self.first
        return self.by_request.get(request_id, self.first)


def _input_tokens(usage: object) -> tuple[int, int]:
    reads = usage.cache_read  # type: ignore[attr-defined]
    total = (usage.uncached_input + reads + usage.cache_write_5m  # type: ignore[attr-defined]
             + usage.cache_write_1h + usage.cache_write_other  # type: ignore[attr-defined]
             + usage.cache_write_unknown)  # type: ignore[attr-defined]
    return reads, total


def _share(lanes: Sequence[Lane], after_turn: int) -> str | None:
    reads = total = 0
    for lane in lanes:
        for i, req in enumerate(lane.requests):
            if i < after_turn:
                continue
            inf = req.serving_inference
            if inf is None or inf.billable is False:
                continue
            r, t = _input_tokens(inf.usage)
            reads += r
            total += t
    if total <= 0:
        return None
    value = (Decimal(reads) / Decimal(total)).quantize(_SHARE_Q, rounding=ROUND_HALF_EVEN)
    return format(value, "f")


def _run_costs(lanes: Sequence[Lane], pricer: Pricer) -> tuple[dict[str, int], int]:
    """Σ exact nano per run (session) and the number of unpriced billable inferences."""
    costs: dict[str, int] = {}
    unpriced = 0
    for lane in lanes:
        total = costs.setdefault(lane.session_key, 0)
        for req in lane.requests:
            for att in req.attempts:
                for inf in att.inferences:
                    if inf.billable is False:
                        continue
                    priced = pricer.price_inference(inf, ts_ms=att.ts_start_ms)
                    if priced.figure.nano is None:
                        unpriced += 1
                    total += priced.exact_nano
        costs[lane.session_key] = total
    return costs, unpriced


def _median(values: Sequence[int]) -> int | None:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    if n % 2:
        return ordered[n // 2]
    q, r = divmod(ordered[n // 2 - 1] + ordered[n // 2], 2)
    if r and q % 2:
        q += 1
    return q


def _fingerprinted(lanes: Sequence[Lane]) -> bool:
    return any(req.fingerprint is not None for lane in lanes for req in lane.requests)


def _block_kinds(lanes: Sequence[Lane], pricer: Pricer, rules: CacheRulesProvider,
                 window: tuple[int, int]) -> list[tuple[str, str | None]]:
    """``(kind, first request id)`` of every block-level breaker finding."""
    from tokenbill.core.registry import run_detectors  # lazy: BLOCK is imported only here

    ctx = AnalysisContext(pricer=pricer, rules=rules, replayer=None, calibration=None,
                          window=window, capabilities=frozenset({"blocks", "usage_sequence",
                                                                 "timing", "params"}),
                          thresholds={"min_usd": "0"}, now_ms=window[1])
    out: list[tuple[str, str | None]] = []
    for f in run_detectors(lanes, ctx, only=[BLOCK_DETECTOR], emit_missing=False):
        refs = sorted(item.ref for item in f.evidence if item.kind != "aggregate")
        out.append((f.kind, refs[0] if refs else None))
    return out


def _miss_kinds(lanes: Sequence[Lane], pricer: Pricer,
                rules: CacheRulesProvider) -> list[tuple[str, str | None]]:
    from tokenbill.core.transitions import classify_transitions

    out: list[tuple[str, str | None]] = []
    for lane in lanes:
        for t in classify_transitions(lane, pricer=pricer, rules=rules):
            if t.is_miss_event and t.cause not in USAGE_EXCLUDED_CAUSES:
                out.append((MISS_PREFIX + t.cause, t.request_id))
    return out


def _window(lanes: Sequence[Lane]) -> tuple[int, int]:
    starts = [req.ts_start_ms for lane in lanes for req in lane.requests]
    if not starts:
        return (0, 1)
    return (min(starts), max(starts) + 1)


def _usd(nano: int) -> str:
    return fmt_usd(nano, 4)


def _pct_text(value: Fraction) -> str:
    q = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_EVEN)
    return format(q, "f")


def run_check(lanes: Sequence[Lane], *, pricer: Pricer, rules: CacheRulesProvider,
              options: CheckOptions | None = None, baseline: Baseline | None = None,
              files: Mapping[str, str] | None = None,
              notes: Sequence[str] = ()) -> CheckResult:
    """Judge *lanes* against the four rules of the module docstring.

    *files* maps a lane's ``session_key`` to the name of the input file it came from (for
    locations); *notes* are extra warnings for the Markdown summary (e.g. quarantined lines).
    Returns the :class:`CheckResult`; ``passed`` is False when any violation has level
    ``error``."""
    opts = options if options is not None else CheckOptions()
    if not isinstance(opts, CheckOptions):
        raise UsageError("options must be CheckOptions")
    lanes = list(lanes)
    where = _Where.of(lanes, files or {})
    fails = set(FAIL_ON[:3]) if opts.fail_on == "any" else {opts.fail_on}
    found: list[CheckViolation] = []
    lines: list[str] = []

    def violate(rule: str, message: str, location: str, *, warning: bool = False) -> None:
        level = "error" if not warning and _FAMILY[rule] in fails else "warning"
        found.append(CheckViolation(rule_id=rule, level=level, message=message[:400],
                                    location=location[:256]))

    runs = len({lane.session_key for lane in lanes})
    # (1) cache-read share
    share = _share(lanes, opts.after_turn)
    threshold = format(opts.min_cache_read_share, "f")
    if share is None:
        violate(RULE_CACHE_SHARE, f"cache-read share not checked: no request after turn "
                f"{opts.after_turn}", where.first, warning=True)
        lines.append(f"| cache-read share | skipped | no request after turn {opts.after_turn} |")
    elif Decimal(share) < opts.min_cache_read_share:
        violate(RULE_CACHE_SHARE, f"cache-read share {share} after turn {opts.after_turn} is "
                f"below {threshold}", where.first)
        lines.append(f"| cache-read share | FAIL | {share} < {threshold} |")
    else:
        lines.append(f"| cache-read share | pass | {share} >= {threshold} |")
    # (2) + (4) breakers
    if _fingerprinted(lanes):
        breakers = _block_kinds(lanes, pricer, rules, _window(lanes))
    else:
        breakers = _miss_kinds(lanes, pricer, rules)
    first_at: dict[str, str | None] = {}
    for kind, ref in breakers:
        first_at.setdefault(kind, ref)
    kinds = tuple(sorted(first_at))
    known = baseline.breaker_kinds if baseline is not None else frozenset()
    new = [k for k in kinds if k not in known and k != CHURN_KIND]
    for kind in new:
        violate(RULE_NEW_BREAKER, f"new cache-breaker kind {kind} (not in the baseline)",
                where.at(first_at[kind]))
    if CHURN_KIND in first_at:
        violate(RULE_CHURN, "serialization churn: an unchanged prefix is serialized differently "
                "between calls", where.at(first_at[CHURN_KIND]))
    lines.append(f"| cache breakers | {'FAIL' if new else 'pass'} | "
                 f"{', '.join(kinds) if kinds else 'none'}"
                 f"{' (new: ' + ', '.join(new) + ')' if new else ''} |")
    if CHURN_KIND in first_at:
        lines.append("| serialization churn | FAIL | serialization-churn |")
    # (3) cost per run
    costs, unpriced = _run_costs(lanes, pricer)
    median = _median(list(costs.values()))
    base_median = baseline.median_cost_nano if baseline is not None else None
    delta_line = ""
    if baseline is None or base_median is None:
        lines.append("| median cost per run | skipped | no baseline median |")
    elif runs < opts.min_runs or baseline.runs < opts.min_runs:
        violate(RULE_COST, f"cost check skipped: {min(runs, baseline.runs)} run(s) < --min-runs "
                f"{opts.min_runs}", where.first, warning=True)
        lines.append(f"| median cost per run | skipped | fewer than {opts.min_runs} runs |")
    else:
        assert median is not None
        limit = Fraction(base_median) * (1 + Fraction(opts.max_cost_regression_pct) / 100)
        delta = (median - base_median) * 1000
        delta_line = f"Δ$ per 1,000 runs: {'+' if delta >= 0 else '-'}{_usd(abs(delta))}"
        if median > limit:
            pct = (Fraction(median - base_median) * 100 / base_median) if base_median else None
            grew = f"+{_pct_text(pct)}%" if pct is not None else "from $0"
            violate(RULE_COST, f"median cost per run {_usd(median)} is {grew} over the baseline "
                    f"{_usd(base_median)} (limit +{opts.max_cost_regression_pct}%)", where.first)
            lines.append(f"| median cost per run | FAIL | {_usd(median)} vs {_usd(base_median)} |")
        else:
            lines.append(f"| median cost per run | pass | {_usd(median)} vs {_usd(base_median)} |")
    warnings = list(notes)
    if unpriced:
        warnings.append(f"{unpriced} billable inference(s) unpriced (excluded from run costs)")
    passed = not any(v.level == "error" for v in found)
    md = ["## tokenbill check: " + ("passed" if passed else "FAILED"), "",
          f"{runs} run(s); median exact cost per run "
          + (_usd(median) if median is not None else "n/a")
          + (f" (baseline {_usd(base_median)})" if base_median is not None else ""), "",
          "| check | result | detail |", "|---|---|---|", *lines]
    if delta_line:
        md += ["", delta_line]
    if warnings:
        md += ["", *(f"- warning: {w}" for w in warnings)]
    found.sort(key=lambda v: (v.rule_id, v.location, v.message))
    return CheckResult(passed=passed, violations=tuple(found), runs=runs,
                       cache_read_share=share, median_cost_nano=median,
                       baseline_median_cost_nano=base_median, breaker_kinds=kinds,
                       summary_md="\n".join(md) + "\n")
