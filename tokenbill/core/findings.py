"""Shared detector helpers (SPEC §3.22, §10.1; F-SEM, Copilot additions F-SEM-C).

Cohorts, stable finding ids, scopes, a validating :class:`~tokenbill.core.types.Finding` builder,
the billed-rewrite split of a miss, thresholds, the bytes-per-token fit of the carry detector,
evidence ordering, figure sums and per-bucket rate arithmetic. Money is int nano; no floats.

GitHub Copilot (CORE-AMENDMENTS S-1 … S-3, ruling R-E20): :func:`product_family` names a lane's
product family (``"copilot"`` for pooled-credit lanes); :func:`make_scope` gives a ``pool`` cohort
the ``product: copilot`` dim; :func:`build_finding` validates Copilot scopes with the R-E20
per-field basis domains, labels pool cohorts as list-equivalent AI-credit value and swaps
Claude Code fixes for Copilot ones; :func:`min_usd_gate` is the ``min_usd`` test for every scope.
``cohort_key`` is unchanged: billing class ``pool`` already separates Copilot cohorts.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable
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
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, Fix, Scope, Transition

__all__ = [
    "AUDIENCES",
    "CATEGORIES",
    "CONFIDENCES",
    "COPILOT_CHANNEL",
    "COPILOT_FAMILY",
    "COPILOT_FIX_TARGET",
    "COPILOT_SUMMARY_PHRASE",
    "COPILOT_TITLE_PREFIX",
    "CPT_MIN_SAMPLES",
    "DEFAULT_FAMILY",
    "LEVER_CLASSES",
    "MAX_EVIDENCE",
    "MAX_SUMMARY",
    "MAX_TITLE",
    "NO_COPILOT_SETTING",
    "build_finding",
    "cohort_key",
    "evidence_magnitude",
    "finding_id",
    "fit_cpt",
    "make_scope",
    "min_usd_gate",
    "min_usd_nano",
    "miss_waste",
    "product_family",
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

#: Product family of every lane that is not a Copilot lane (and of findings without a product dim).
DEFAULT_FAMILY = "default"
#: Product family (and ``product`` scope-dim value) of GitHub Copilot lanes and findings (DC14).
COPILOT_FAMILY = "copilot"
#: The pricing channel of Copilot AI-credit usage.
COPILOT_CHANNEL = "github_copilot"
#: The billing class of GitHub Copilot's pooled AI credits (``core.records.billing_class``).
_POOL_CLASS = "pool"
#: Title prefix of pool-cohort findings (list-equivalent credit value, never invoice dollars).
COPILOT_TITLE_PREFIX = "Copilot credits: "
#: The labelling phrase every pool-cohort summary carries.
COPILOT_SUMMARY_PHRASE = "list-equivalent AI-credit value"
_COPILOT_SUMMARY_TAIL = f"({COPILOT_SUMMARY_PHRASE})"
#: ``Fix.target`` of Copilot fixes (``core.catalog.fix_for(…, "copilot")``).
COPILOT_FIX_TARGET = "github-copilot"
#: Appended to a kept fix text on a Copilot scope when the catalog has no Copilot fix.
NO_COPILOT_SETTING = " (no Copilot setting known)"


def cohort_key(lane: Lane) -> tuple[str | None, str, str]:
    """``(team, lane_kind, billing_class)`` of *lane*: the boundary of every cross-lane
    computation, so per-shard and whole runs agree. An empty team is unattributed (None).

    Copilot lanes need no fourth element: only the two Copilot billing paths map to billing class
    ``pool``, so a Copilot lane never shares a cohort with a Claude Code lane of the same team."""
    return (lane.team or None, lane.kind.value, lane.billing_class)


def product_family(lane: Lane) -> str:
    """``"copilot"`` when *lane* is a GitHub Copilot lane, else ``"default"`` (S-1, DC14).

    A Copilot lane has billing class ``pool`` or its first request is priced on channel
    ``github_copilot`` (the serving inference's pricing context, else the first inference of the
    request's attempts). ``core.registry.run_detectors`` filters the lanes a detector sees by this
    family (detector ``families`` and ``core.catalog.FAMILY_EXCLUSIONS``).
    """
    if lane.billing_class == _POOL_CLASS:
        return COPILOT_FAMILY
    if lane.requests:
        first = lane.requests[0]
        inf = first.serving_inference
        if inf is None:
            inf = next((i for att in first.attempts for i in att.inferences), None)
        if inf is not None and inf.pricing.channel == COPILOT_CHANNEL:
            return COPILOT_FAMILY
    return DEFAULT_FAMILY


def finding_id(detector_id: str, kind: str, scope: Scope) -> str:
    """``stable_id("fd", detector_id, kind, "k=v"…)`` over the scope dims in sorted order —
    independent of evidence, shard and emission order."""
    dims = sorted(scope.dims)
    return stable_id("fd", detector_id, kind, *(f"{k}={v}" for k, v in dims))


def make_scope(**dims: str | None) -> Scope:
    """A :class:`Scope` from keyword dims: ``None`` values dropped, enums by value, sorted.

    A ``billing_class="pool"`` scope without a (non-None) ``product`` dim gets
    ``product="copilot"`` (S-1), so generic detectors' pool cohorts are Copilot findings."""
    out: list[tuple[str, str]] = []
    for key, value in dims.items():
        if value is None:
            continue
        if isinstance(value, Enum):
            value = value.value
        if not isinstance(value, str):
            raise ContractViolation(f"make_scope: dim {key} must be a str")
        out.append((key, value))
    if ("billing_class", _POOL_CLASS) in out and not any(k == "product" for k, _ in out):
        out.append(("product", COPILOT_FAMILY))
    return Scope(dims=tuple(sorted(out)))


def _is_copilot_scope(scope: Scope) -> bool:
    """R-E20: a scope with ``product=copilot`` or ``billing_class=pool``."""
    dims = scope.dims
    return ("product", COPILOT_FAMILY) in dims or ("billing_class", _POOL_CLASS) in dims


def _figures(finding: Finding) -> list[Figure]:
    figs = [finding.cost_observed, finding.recoverable, finding.recoverable_shapley,
            finding.projected_monthly]
    return [f for f in figs if f is not None]


#: R-E20 basis domain of each money field of a Copilot-scope finding (CONTRACT is in none;
#: PROVIDER_ESTIMATE is added for data-quality findings, except on ``headroom``).
_COPILOT_DOMAINS: tuple[tuple[str, frozenset[Basis]], ...] = (
    ("cost_observed", frozenset({Basis.LIST_EQUIVALENT, Basis.LIST, Basis.INVOICE})),
    ("recoverable", frozenset({Basis.LIST, Basis.LIST_EQUIVALENT})),
    ("recoverable_shapley", frozenset({Basis.LIST, Basis.LIST_EQUIVALENT})),
    ("projected_monthly", frozenset({Basis.LIST, Basis.LIST_EQUIVALENT})),
    ("headroom", frozenset({Basis.LIST_EQUIVALENT})),
)
_MONEY_FIELDS = tuple(name for name, _ in _COPILOT_DOMAINS)


def build_finding(**fields: object) -> Finding:
    """A validated :class:`Finding`.

    ``finding_id`` defaults to :func:`finding_id` of (detector_id, kind, scope) and, when given,
    must equal it; list-valued ``lever_ids`` / ``evidence`` / ``references`` become tuples.
    Validates: title non-empty and ≤ 120 chars, summary ≤ 400, references non-empty, evidence ≤ 20,
    category / lever_class / audience / confidence in their SPEC value sets, non-negative counts,
    and figure bases — every figure is a :class:`Figure` on one common basis, ``PROVIDER_ESTIMATE``
    only on data-quality findings (R4, in any cohort), otherwise ``LIST_EQUIVALENT`` exactly when
    the scope's ``billing_class`` is ``allowance`` (D26); ``headroom`` must be None. Violations
    raise :class:`ContractViolation`.

    **Copilot scopes** (a ``product=copilot`` or ``billing_class=pool`` dim; ruling R-E20) replace
    the one-basis rule by per-field domains: ``cost_observed`` ∈ {LIST_EQUIVALENT, LIST,
    INVOICE}; ``recoverable``, ``recoverable_shapley``, ``projected_monthly`` ∈ {LIST,
    LIST_EQUIVALENT}; ``headroom`` LIST_EQUIVALENT; CONTRACT never; PROVIDER_ESTIMATE only in
    data-quality findings (R4) and never as ``headroom``. Before validation the finding is
    normalized, never rejected (S-2):

    - a ``billing_class=pool`` cohort with a LIST_EQUIVALENT figure gets the title prefix
      ``"Copilot credits: "`` and the summary phrase ``"(list-equivalent AI-credit value)"``
      when absent; the text before them is cut at a word boundary (with "…") so the title stays
      ≤ 120 and the summary ≤ 400 chars and the labels always survive;
    - a fix that does not already target ``github-copilot`` is replaced by
      ``core.catalog.fix_for(detector_id, kind, "copilot")`` (resolved lazily) when that returns
      a ``github-copilot`` fix; otherwise its text is kept with ``" (no Copilot setting known)"``
      appended and its config patch, target and applicability gates are dropped (the gates
      qualify the dropped patch), so no Claude Code key reaches a Copilot fix (DC14). A None fix
      stays None.
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
    if _is_copilot_scope(scope):
        _normalize_copilot(data, scope, detector, kind)
    try:
        finding = Finding(**data)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContractViolation(f"build_finding: {exc}") from None
    _validate(finding)
    return finding


def _cut(text: str, limit: int) -> str:
    """*text* within *limit* chars: cut at a word boundary with "…" (a scope value is never left
    half-cut in generated text, which ``core.kanon`` scrubs as whole tokens)."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def _pool_labels(data: dict[str, object]) -> None:
    """S-2 title prefix and summary phrase of a pool cohort (idempotent)."""
    title, summary = data.get("title"), data.get("summary")
    if isinstance(title, str) and title:
        body = title[len(COPILOT_TITLE_PREFIX):] if title.startswith(COPILOT_TITLE_PREFIX) \
            else title
        data["title"] = COPILOT_TITLE_PREFIX + _cut(body, MAX_TITLE - len(COPILOT_TITLE_PREFIX))
    if isinstance(summary, str) and COPILOT_SUMMARY_PHRASE not in summary:
        if not summary:
            data["summary"] = _COPILOT_SUMMARY_TAIL
        else:
            tail = " " + _COPILOT_SUMMARY_TAIL
            data["summary"] = _cut(summary, MAX_SUMMARY - len(tail)) + tail


def _catalog_fix(detector_id: str, kind: str) -> Fix | None:
    """``core.catalog.fix_for(detector_id, kind, "copilot")`` (F-KIT-C), resolved lazily: a
    missing module or accessor, or an answer that is not a ``github-copilot`` :class:`Fix`,
    means "no Copilot fix known"."""
    try:
        from tokenbill.core import catalog
    except ImportError:  # pragma: no cover - core.catalog ships with the foundation
        return None
    fix_for = getattr(catalog, "fix_for", None)
    if not callable(fix_for):
        return None
    fix = fix_for(detector_id, kind, COPILOT_FAMILY)
    if isinstance(fix, Fix) and fix.target == COPILOT_FIX_TARGET:
        return fix
    return None


def _copilot_fix(fix: Fix, detector_id: str, kind: str) -> Fix:
    """S-2 fix substitution for a Copilot scope (idempotent)."""
    if fix.target == COPILOT_FIX_TARGET:
        return fix
    replacement = _catalog_fix(detector_id, kind)
    if replacement is not None:
        return replacement
    text = fix.text if isinstance(fix.text, str) else ""
    if not text.endswith(NO_COPILOT_SETTING):
        text += NO_COPILOT_SETTING
    return Fix(text=text, config_patch=None, target=None, doc_url=fix.doc_url, gates=())


def _normalize_copilot(data: dict[str, object], scope: Scope, detector_id: str,
                       kind: str) -> None:
    """Pool-cohort labels and the Copilot fix (S-2) on the raw fields of a Copilot finding."""
    if ("billing_class", _POOL_CLASS) in scope.dims and any(
            isinstance(fig, Figure) and fig.basis is Basis.LIST_EQUIVALENT
            for fig in (data.get(name) for name in _MONEY_FIELDS)):
        _pool_labels(data)
    fix = data.get("fix")
    if isinstance(fix, Fix):
        data["fix"] = _copilot_fix(fix, detector_id, kind)


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
    for name in ("recoverable", "recoverable_shapley", "projected_monthly", "headroom"):
        value = getattr(f, name)
        if value is not None and not isinstance(value, Figure):
            raise fail(f"{name} must be a Figure or None")
    if _is_copilot_scope(f.scope):
        _validate_copilot_bases(f, fail)
        return
    if f.headroom is not None:
        raise fail("headroom appears only on Copilot scopes (R-E20)")
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


def _validate_copilot_bases(f: Finding, fail: Callable[[str], ContractViolation]) -> None:
    """R-E20 per-field basis domains of a Copilot-scope finding (figures are never summed across
    fields, so no common basis is required)."""
    data_quality = f.category == "data-quality"
    for name, domain in _COPILOT_DOMAINS:
        fig: Figure | None = getattr(f, name)
        if fig is None:
            continue
        basis = fig.basis
        if basis is Basis.PROVIDER_ESTIMATE:
            if not data_quality:
                raise fail("provider estimates appear only in data-quality findings (R4)")
            if name != "headroom":
                continue
        if basis not in domain:
            allowed = ", ".join(sorted(b.value for b in domain))
            raise fail(f"{name} on a Copilot scope must have basis in {{{allowed}}} (R-E20)")


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


def _point(fig: Figure | None) -> int | None:
    return fig.nano if fig is not None else None


def min_usd_gate(finding: Finding, ctx: AnalysisContext) -> bool:
    """True when *finding* reaches the ``min_usd`` threshold (S-3, SPEC §10.1).

    The compared point is the recoverable point; on a Copilot scope (``product=copilot`` or
    ``billing_class=pool``) the larger of the recoverable and ``headroom`` points (credits and
    dollars are compared only as numbers of nano for this gate, never added). When none of those
    is present and priced, the ``cost_observed`` point is compared (triage / info kinds). An
    unpriced value never passes (unknown is never zero, R2).
    """
    points = [_point(finding.recoverable)]
    if _is_copilot_scope(finding.scope):
        points.append(_point(finding.headroom))
    known = [p for p in points if p is not None]
    value = max(known) if known else _point(finding.cost_observed)
    return value is not None and value >= min_usd_nano(ctx)


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

