"""Shared detector helpers (SPEC §3.22, §10.1; F-SEM).

Cohorts, stable finding ids, scopes, a validating :class:`~tokenbill.core.types.Finding` builder,
the billed-rewrite split of a miss, thresholds, the bytes-per-token fit of the carry detector,
evidence ordering, figure sums and per-bucket rate arithmetic. Money is int nano; no floats.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from enum import Enum

from tokenbill.core.errors import ContractViolation, PricingError, UsageError
from tokenbill.core.evidence import CPT_DEFAULT_47PLUS_TOOL_OUTPUT, CPT_DEFAULT_LEGACY
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis, Figure, add, zero
from tokenbill.core.money import RATIO_CTX, decimal_to_nano, scaled_to_nano, usd
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    Lane,
    LaneEventKind,
    PricingContext,
    Request,
    UsageBuckets,
)
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, Scope, Transition

__all__ = [
    "AUDIENCES",
    "CATEGORIES",
    "CONFIDENCES",
    "CPT_MIN_SAMPLES",
    "LEVER_CLASSES",
    "MAX_EVIDENCE",
    "MAX_SUMMARY",
    "MAX_TITLE",
    "build_finding",
    "cohort_key",
    "evidence_magnitude",
    "finding_id",
    "fit_cpt",
    "make_scope",
    "min_usd_nano",
    "miss_waste",
    "rate_nano",
    "sum_figures",
    "threshold",
    "top_evidence",
]

MAX_TITLE = 120
MAX_SUMMARY = 400
MAX_EVIDENCE = 20
CPT_MIN_SAMPLES = 30
CATEGORIES = frozenset({"breaker", "lever", "premium", "failure", "attribution", "data-quality",
                        "aggregate"})
LEVER_CLASSES = frozenset({"rate", "cache_transform", "trajectory", "behavioral", "hygiene",
                           "none"})
AUDIENCES = frozenset({"org", "self"})
CONFIDENCES = frozenset({"high", "medium", "low"})
_MIN_USD_DEFAULT = "1.00"
_MAGNITUDE_KEYS = ("magnitude", "nano", "tokens")


def cohort_key(lane: Lane) -> tuple[str | None, str, str]:
    """``(team, lane_kind, billing_class)`` of *lane*: the boundary of every cross-lane
    computation, so per-shard and whole runs agree. An empty team is unattributed (None)."""
    return (lane.team or None, lane.kind.value, lane.billing_class)


def finding_id(detector_id: str, kind: str, scope: Scope) -> str:
    """``stable_id("fd", detector_id, kind, "k=v"…)`` over the scope dims in sorted order —
    independent of evidence, shard and emission order."""
    dims = sorted(scope.dims)
    return stable_id("fd", detector_id, kind, *(f"{k}={v}" for k, v in dims))


def make_scope(**dims: str | None) -> Scope:
    """A :class:`Scope` from keyword dims: ``None`` values dropped, enums by value, sorted."""
    out: list[tuple[str, str]] = []
    for key, value in dims.items():
        if value is None:
            continue
        if isinstance(value, Enum):
            value = value.value
        if not isinstance(value, str):
            raise ContractViolation(f"make_scope: dim {key} must be a str")
        out.append((key, value))
    return Scope(dims=tuple(sorted(out)))


def _figures(finding: Finding) -> list[Figure]:
    figs = [finding.cost_observed, finding.recoverable, finding.recoverable_shapley,
            finding.projected_monthly]
    return [f for f in figs if f is not None]


def build_finding(**fields: object) -> Finding:
    """A validated :class:`Finding`.

    ``finding_id`` defaults to :func:`finding_id` of (detector_id, kind, scope) and, when given,
    must equal it; list-valued ``lever_ids`` / ``evidence`` / ``references`` become tuples.
    Validates: title non-empty and ≤ 120 chars, summary ≤ 400, references non-empty, evidence ≤ 20,
    category / lever_class / audience / confidence in their SPEC value sets, non-negative counts,
    and figure bases — every figure is a :class:`Figure` on one common basis, ``PROVIDER_ESTIMATE``
    only on data-quality findings (R4, in any cohort), otherwise ``LIST_EQUIVALENT`` exactly when
    the scope's ``billing_class`` is ``allowance`` (D26). Violations raise
    :class:`ContractViolation`.
    """
    data = dict(fields)
    for name in ("lever_ids", "evidence", "references"):
        if isinstance(data.get(name), list):
            data[name] = tuple(data[name])  # type: ignore[arg-type]
    scope = data.get("scope")
    if not isinstance(scope, Scope):
        raise ContractViolation("build_finding: scope must be a Scope")
    detector, kind = data.get("detector_id"), data.get("kind")
    if not isinstance(detector, str) or not detector or not isinstance(kind, str) or not kind:
        raise ContractViolation("build_finding: detector_id and kind must be non-empty str")
    expected = finding_id(detector, kind, scope)
    given = data.setdefault("finding_id", expected)
    if given != expected:
        raise ContractViolation("build_finding: finding_id does not match detector/kind/scope")
    try:
        finding = Finding(**data)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContractViolation(f"build_finding: {exc}") from None
    _validate(finding)
    return finding


def _validate(f: Finding) -> None:
    def fail(why: str) -> ContractViolation:
        return ContractViolation(f"build_finding ({f.detector_id}/{f.kind}): {why}")

    if not isinstance(f.title, str) or not f.title or len(f.title) > MAX_TITLE:
        raise fail(f"title must be 1..{MAX_TITLE} chars")
    if not isinstance(f.summary, str) or len(f.summary) > MAX_SUMMARY:
        raise fail(f"summary must be at most {MAX_SUMMARY} chars")
    if not isinstance(f.references, tuple) or not f.references or \
            not all(isinstance(r, str) and r for r in f.references):
        raise fail("references must be a non-empty tuple of str")
    if not isinstance(f.evidence, tuple) or len(f.evidence) > MAX_EVIDENCE or \
            not all(isinstance(e, EvidenceItem) for e in f.evidence):
        raise fail(f"evidence must be a tuple of at most {MAX_EVIDENCE} EvidenceItem")
    if not isinstance(f.lever_ids, tuple) or not all(isinstance(x, str) for x in f.lever_ids):
        raise fail("lever_ids must be a tuple of str")
    if f.category not in CATEGORIES:
        raise fail("unknown category")
    if f.lever_class not in LEVER_CLASSES:
        raise fail("unknown lever_class")
    if f.audience not in AUDIENCES:
        raise fail("unknown audience")
    if f.confidence not in CONFIDENCES:
        raise fail("unknown confidence")
    if not isinstance(f.detector_version, str) or not f.detector_version:
        raise fail("detector_version must be a non-empty str")
    for name in ("n_events", "n_lanes", "n_users", "first_seen_ms"):
        value = getattr(f, name)
        if type(value) is not int or value < 0:
            raise fail(f"{name} must be a non-negative int")
    if type(f.needs_eval) is not bool:
        raise fail("needs_eval must be a bool")
    if not isinstance(f.cost_observed, Figure):
        raise fail("cost_observed must be a Figure")
    for name in ("recoverable", "recoverable_shapley", "projected_monthly"):
        value = getattr(f, name)
        if value is not None and not isinstance(value, Figure):
            raise fail(f"{name} must be a Figure or None")
    bases = {fig.basis for fig in _figures(f)}
    if len(bases) != 1:
        raise fail("figures must share one basis")
    basis = bases.pop()
    if basis is Basis.PROVIDER_ESTIMATE:
        # R4: a provider estimate is never a billed number, so it is neither a billed nor a
        # list-equivalent figure; it may appear (in any cohort) only in data-quality findings.
        if f.category != "data-quality":
            raise fail("provider estimates appear only in data-quality findings (R4)")
        return
    allowance = ("billing_class", "allowance") in f.scope.dims
    if allowance != (basis is Basis.LIST_EQUIVALENT):
        raise fail("allowance cohorts use basis list_equivalent and only they do (D26)")


def miss_waste(t: Transition, request: Request) -> tuple[int, int]:
    """Split a miss's ``M`` into billed rewrite tokens: ``mw = min(M, W_i)`` written,
    ``mu = min(M − mw, U_i)`` sent uncached (from *request*'s serving inference; ``(0, 0)`` when it
    has none)."""
    inf = request.serving_inference
    if inf is None:
        return (0, 0)
    missed = max(0, t.missed)
    mw = min(missed, inf.usage.cache_write)
    mu = min(missed - mw, inf.usage.uncached_input)
    return (mw, mu)


def threshold(ctx: AnalysisContext, key: str, default: str) -> Decimal:
    """``ctx.thresholds[key]`` (else *default*) as a finite Decimal; an invalid value raises
    :class:`UsageError` naming the key."""
    raw = ctx.thresholds.get(key, default) if ctx.thresholds is not None else default
    if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
        raise UsageError(f"threshold {key}: must be a decimal string")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise UsageError(f"threshold {key}: not a decimal") from None
    if not value.is_finite():
        raise UsageError(f"threshold {key}: not finite")
    return value


def min_usd_nano(ctx: AnalysisContext) -> int:
    """The ``min_usd`` threshold (default ``"1.00"`` over the window) in int nano-USD."""
    value = threshold(ctx, "min_usd", _MIN_USD_DEFAULT)
    try:
        return decimal_to_nano(usd(value))
    except (ValueError, TypeError):
        raise UsageError("threshold min_usd: out of range") from None


_RESET_EVENTS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR,
                           LaneEventKind.CONTEXT_EDIT})
_TEXT_KINDS = frozenset({"tool_result", "user_text", "attachment", "assistant"})
_CPT_LOW, _CPT_HIGH = 1, 8   # plausible bytes per token of one sample


def _cpt_default(family: str) -> Decimal:
    fact = CPT_DEFAULT_LEGACY if family == "claude-legacy" else CPT_DEFAULT_47PLUS_TOOL_OUTPUT
    value = fact.value
    if not isinstance(value, Decimal):  # pragma: no cover - facts.json guarantees a decimal
        raise ContractViolation("CPT defaults must be decimals")
    return value


def fit_cpt(lanes: Iterable[Lane], family: str) -> tuple[Decimal, int]:
    """Bytes-per-token fit for the carry detector (§10.2 ``attrib.carry``): ``(cpt, n samples)``.

    *lanes* are the lanes of one tokenizer *family* (the caller partitions with
    ``Pricer.tokenizer_family``). A sample is a request whose appended items are all text-like
    (tool results, user text, attachments, assistant turns; no images) on the same serving model as
    the previous request with no reset in between: ``bytes = Σ n_bytes`` and ``tokens = T_i −
    T_{i−1}`` (minus the previous output when no assistant item was reported). Samples outside 1–8
    bytes per token are dropped as mismatched turns. The fit is ``Σ bytes / Σ tokens``; with fewer
    than 30 samples the published default is returned (2.5 for ``claude-4.7+``, 3.3 for
    ``claude-legacy``; other families use 2.5).
    """
    total_bytes = total_tokens = n = 0
    for lane in lanes:
        prev: tuple[Request, UsageBuckets] | None = None
        # sorted timestamps of the lane's reset events (Lane sorts events by ts): O(log m) per
        # sample instead of a scan of every event
        resets = [ev.ts_ms for ev in lane.events if ev.kind in _RESET_EVENTS]
        for req in lane.requests:
            inf = req.serving_inference
            if inf is None:
                continue
            current = (req, inf.usage)
            if prev is not None and req.appended:
                sample = _cpt_sample(prev, current, resets)
                if sample is not None:
                    total_bytes += sample[0]
                    total_tokens += sample[1]
                    n += 1
            prev = current
    if n < CPT_MIN_SAMPLES or total_tokens <= 0:
        return (_cpt_default(family), n)
    return (RATIO_CTX.divide(Decimal(total_bytes), Decimal(total_tokens)), n)


def _cpt_sample(prev: tuple[Request, UsageBuckets], cur: tuple[Request, UsageBuckets],
                resets: list[int]) -> tuple[int, int] | None:
    prev_req, prev_usage = prev
    req, usage = cur
    if req.model != prev_req.model:
        return None
    if any(item.kind not in _TEXT_KINDS or item.images for item in req.appended):
        return None
    if any(att.applied_edits or att.thinking_dropped for att in req.attempts):
        return None
    lo, hi = prev_req.ts_start_ms, req.ts_start_ms
    first_after = bisect_right(resets, lo)
    if first_after < len(resets) and resets[first_after] <= hi:
        return None     # a COMPACTION / CLEAR / CONTEXT_EDIT event in (lo, hi]
    n_bytes = sum(item.n_bytes for item in req.appended)
    tokens = usage.total_input - prev_usage.total_input
    if not any(item.kind == "assistant" for item in req.appended):
        tokens -= prev_usage.output
    if n_bytes <= 0 or tokens <= 0:
        return None
    if not _CPT_LOW * tokens <= n_bytes <= _CPT_HIGH * tokens:
        return None
    return (n_bytes, tokens)


def evidence_magnitude(item: EvidenceItem) -> int:
    """The sort magnitude of an evidence item: the first int among its ``magnitude``, ``nano`` or
    ``tokens`` attrs (in that order), else 0."""
    attrs = dict(item.attrs)
    for key in _MAGNITUDE_KEYS:
        value = attrs.get(key)
        if type(value) is int:
            return value
    return 0


def top_evidence(items: Iterable[EvidenceItem], n: int = MAX_EVIDENCE) -> tuple[EvidenceItem, ...]:
    """The *n* largest evidence items sorted by ``(−magnitude, ref)`` (then kind and attrs, for a
    total order); see :func:`evidence_magnitude`."""
    if n <= 0:
        return ()
    ranked = sorted(items, key=lambda e: (-evidence_magnitude(e), e.ref, e.kind,
                                          tuple((k, repr(v)) for k, v in e.attrs)))
    return tuple(ranked[:n])


def sum_figures(figs: Iterable[Figure], basis: Basis) -> Figure:
    """``core.labels.add`` fold of *figs* (all on *basis*); empty → ``zero(basis)``."""
    total: Figure | None = None
    for fig in figs:
        if fig.basis is not basis:
            raise ContractViolation("sum_figures: basis mismatch")
        total = fig if total is None else add(total, fig)
    return total if total is not None else zero(basis)


_USAGE_FIELD = {
    "uncached_input": "uncached_input", "uncached": "uncached_input", "input": "uncached_input",
    "cache_read": "cache_read", "cache_write_5m": "cache_write_5m",
    "cache_write_1h": "cache_write_1h", "cache_write_other": "cache_write_other",
    "cache_write_unknown": "cache_write_unknown", "output": "output",
    "web_search": "web_search_requests",
}


def rate_nano(pricer: Pricer, ctx: PricingContext, ts_ms: int, bucket: str, tokens: int) -> int:
    """Nano-USD for *tokens* of *bucket* at *ctx*'s rates (``r``, ``w5``, ``w1``, ``u``, ``o`` of
    §10.1 formulas): exact integer unit rates when the pricer has them, else the point of
    ``Pricer.price_usage`` (e.g. unknown endpoint scope). ``cache_write_other`` uses the
    context's other-TTL write rate; ``cache_write_unknown`` the point (hint or 5m) rate;
    ``web_search`` counts requests. Negative *tokens* give the negated amount. An unpriceable
    context raises :class:`PricingError` (unknown is never zero).
    """
    field = _USAGE_FIELD.get(bucket)
    if field is None:
        raise UsageError(f"rate_nano: unknown bucket {bucket!r}")
    if type(tokens) is not int:
        raise UsageError("rate_nano: tokens must be an int")
    if tokens == 0:
        return 0
    if tokens < 0:
        return -rate_nano(pricer, ctx, ts_ms, bucket, -tokens)
    unit = pricer.unit_rates(ctx, ts_ms=ts_ms)
    if unit is not None and bucket != "cache_write_unknown":
        if bucket == "web_search":
            return tokens * unit.web_search_nano
        attr = {"uncached_input": "uncached"}.get(field, field)
        return scaled_to_nano(tokens * getattr(unit, attr), unit.scale_exp)
    kwargs: dict[str, int] = {field: tokens}
    if field == "cache_write_other":
        kwargs["cache_write_other_ttl_s"] = 1800  # informational: the rate is per bucket
    priced = pricer.price_usage(UsageBuckets(**kwargs), ctx, ts_ms=ts_ms)  # type: ignore[arg-type]
    if priced.unpriced_reason is not None or priced.figure.nano is None:
        raise PricingError("rate_nano: context is not priceable")
    for line in priced.lines:
        if line.bucket == bucket or (bucket in ("uncached", "input")
                                     and line.bucket == "uncached_input"):
            return line.amount_nano
    return sum(line.amount_nano for line in priced.lines)

