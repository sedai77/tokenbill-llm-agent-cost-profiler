"""The shared miss definition (SPEC §3.15; F-SEM).

The single definition of a transition, a miss event and its cause, used by the detectors, the
usage-level replay, the model gate (calibration) and the oracle. Everything here is integer
arithmetic on billed tokens (no floats).

For a lane with requests in ``(ts_start_ms, seq)`` order, requests **without a serving inference**
(e.g. an OUTPUT_RESIDUAL-only request) are skipped; ``i`` below (and ``Transition.index``) is the
position among the remaining requests. From the serving inference of request ``i``: ``R`` reads,
``W`` every write bucket, ``U`` uncached input, ``T = R + W + U``, ``P = R + W``.

For ``i ≥ 1``: ``gap = ts_start_i − ts_start_{i−1}``; ``E = min(P_{i−1}, T_i)``;
``M = max(0, E − R_i)``; a **miss event** iff ``M > 0.05·E`` and ``M ≥ 2,000``. ``τ`` is the TTL
of the most recent billed write before ``i`` (the semantics of ``core.lanes.ttl_of_last_write``),
else the ``write_ttl_hint`` of request ``i``'s serving inference, else unknown. ``ambiguous`` iff
``τ`` is known and ``|gap − τ| ≤ 10 s``.

**Unknown TTL semantics** (CORE-AMENDMENTS S-4; GitHub Copilot): when the cache-rule row of
request ``i``'s serving inference has ``ttl_semantics_known=False``, nobody documents where the
TTL clock starts (request start or response end) or whether reads refresh it, so the windows widen
by ``d`` = the duration of request ``i−1`` (from its first attempt's start to the latest attempt
end ``ts_start + duration_ms``; an unknown ``duration_ms`` adds nothing): rule 3 applies only when
``gap > τ + d + 10 s`` (true under every reading) and ``ambiguous`` iff ``|gap − τ| ≤ d + 10 s``.
With ``τ`` unknown there is never a ``ttl-expiry``. Every other row is unchanged.

Cause precedence (first match; slugs are contract): ``compaction`` (COMPACTION / CLEAR /
CONTEXT_EDIT event in ``(ts_{i−1}, ts_i]``, applied context edits or dropped thinking blocks on
request ``i``) → ``model-switch`` (sub-causes ``refusal-fallback``, ``availability-fallback``,
``plan-toggle``, ``ping-pong``, ``user``) → ``ttl-expiry`` → ``param-change`` (``fast-toggle``,
``effort-change`` unless :func:`~tokenbill.core.cache_rules.effort_change_keeps_cache`,
``context-tier-change`` (the serving inferences' ``pricing.context_tier`` differ; S-4),
``client-upgrade``, ``directory-change``) → the canonical diagnostic reason → ``context-shrank``
(``T_i < 0.9·T_{i−1}``) → ``unexplained``. A parameter "differs" only when both requests report it
(``None`` means not observed, never a change); the served speed always counts.

The documented prediction (``predicted_hit``, used by the model gate) never reads ``R_i``: True iff
same serving model, no reset, no invalidating parameter change, ``τ`` known, ``gap ≤ τ`` and
``E ≥ min_cacheable_tokens(model)`` (same cache scope holds within a lane); None when ``τ`` is
unknown. Callers compute predicted reads as ``E`` on a predicted hit, else the static-prefix floor.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from fractions import Fraction

from tokenbill.core.cache_rules import RulesTable, effort_change_keeps_cache
from tokenbill.core.errors import ContractViolation
from tokenbill.core.evidence import MISS_MIN_FRACTION as _MISS_MIN_FRACTION_FACT
from tokenbill.core.evidence import MISS_MIN_TOKENS as _MISS_MIN_TOKENS_FACT
from tokenbill.core.protocols import CacheRulesProvider, Pricer
from tokenbill.core.records import (
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Request,
)
from tokenbill.core.types import Transition

__all__ = [
    "AMBIGUITY_MS",
    "CAUSES",
    "DIAG_CAUSES",
    "HIT",
    "MISS_MIN_FRACTION",
    "MISS_MIN_TOKENS",
    "MODEL_SWITCH_SUB_CAUSES",
    "PARAM_CHANGE_SUB_CAUSES",
    "classify_transitions",
    "is_miss_event",
    "lane_first_reads_of",
    "static_prefix_floor",
]

MISS_MIN_TOKENS: int = _MISS_MIN_TOKENS_FACT.value  # type: ignore[assignment]
MISS_MIN_FRACTION: Decimal = _MISS_MIN_FRACTION_FACT.value  # type: ignore[assignment]
if type(MISS_MIN_TOKENS) is not int or not isinstance(MISS_MIN_FRACTION, Decimal):
    raise ContractViolation("facts.json: MISS_MIN_TOKENS must be an int and MISS_MIN_FRACTION a "
                            "decimal")  # pragma: no cover
AMBIGUITY_MS = 10_000

HIT = "hit"
#: Cause slugs in precedence order (``hit`` is used for non-miss transitions).
CAUSES = ("compaction", "model-switch", "ttl-expiry", "param-change", "tools-changed",
          "system-changed", "messages-changed", "context-shrank", "unexplained")
MODEL_SWITCH_SUB_CAUSES = ("refusal-fallback", "availability-fallback", "plan-toggle", "ping-pong",
                           "user", "diag")
PARAM_CHANGE_SUB_CAUSES = ("fast-toggle", "effort-change", "context-tier-change", "client-upgrade",
                           "directory-change", "diag")
#: Canonical ``CacheDiagnostic.reason`` → (cause, sub_cause) for rule 5.
#: ``previous_message_not_found`` and ``unavailable`` carry no comparison and fall through.
DIAG_CAUSES: Mapping[str, tuple[str, str | None]] = {
    "tools_changed": ("tools-changed", None),
    "system_changed": ("system-changed", None),
    "messages_changed": ("messages-changed", None),
    "param_changed": ("param-change", "diag"),
    "key_changed": ("unexplained", "cache-key"),
    "compacted": ("compaction", None),
    "model_changed": ("model-switch", "diag"),
}

_FRACTION = Fraction(MISS_MIN_FRACTION)
_RESET_EVENTS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR,
                           LaneEventKind.CONTEXT_EDIT})
_FALLBACK_KINDS = frozenset({InferenceKind.FALLBACK, InferenceKind.FALLBACK_DECLINED})
_HINT_TTL_S = {"5m": 300, "1h": 3600}
_OVERLOADED = frozenset({"overloaded", "overloaded_error"})
_DAY_MS = 86_400_000
#: Rules used when a caller passes no rules provider (``rules=None``).
_DEFAULT_RULES = RulesTable()


def is_miss_event(missed: int, expected: int) -> bool:
    """The miss rule (D21): ``missed > 0.05·expected`` **and** ``missed ≥ 2,000`` (never 2,048)."""
    return missed >= MISS_MIN_TOKENS and missed * _FRACTION.denominator > \
        _FRACTION.numerator * expected


def _write_ttl(inf: Inference) -> int | None:
    """TTL in seconds of one billed write (shortest class when several), or None when unknown —
    exactly ``core.lanes.ttl_of_last_write`` for a single inference."""
    usage = inf.usage
    candidates: list[int] = []
    if usage.cache_write_5m:
        candidates.append(300)
    if usage.cache_write_1h:
        candidates.append(3600)
    if usage.cache_write_other and usage.cache_write_other_ttl_s is not None:
        candidates.append(usage.cache_write_other_ttl_s)
    if usage.cache_write_unknown and inf.pricing.write_ttl_hint in _HINT_TTL_S:
        candidates.append(_HINT_TTL_S[inf.pricing.write_ttl_hint])
    return min(candidates) if candidates else None


def _family(model: str) -> str | None:
    if "opus" in model:
        return "opus"
    if "sonnet" in model:
        return "sonnet"
    return None


def _opus_sonnet_switches(models: list[str]) -> int:
    """Number of Opus↔Sonnet family switches along the lane (other families ignored)."""
    switches = 0
    last: str | None = None
    for model in models:
        fam = _family(model)
        if fam is None:
            continue
        if last is not None and fam != last:
            switches += 1
        last = fam
    return switches


def _differs(a: object, b: object) -> bool:
    return a is not None and b is not None and a != b


def _attr(event: LaneEvent, key: str) -> object:
    for k, v in event.attrs:
        if k == key:
            return v
    return None


def _duration_ms(req: Request) -> int:
    """Wall time of *req*: from its first attempt's start to the latest known attempt end
    (``ts_start_ms + duration_ms``; an attempt without a duration ends at its start)."""
    start = req.ts_start_ms
    end = max(att.ts_start_ms + (att.duration_ms or 0) for att in req.attempts)
    return max(0, end - start)


class _Step:
    """One request with a serving inference, and what the classification needs from it."""

    __slots__ = ("inf", "model", "req", "tau")

    def __init__(self, req: Request, inf: Inference, tau: int | None) -> None:
        self.req = req
        self.inf = inf
        self.model = req.model
        self.tau = tau


class _Semantics:
    """``ttl_semantics_known`` of the cache-rule row of each serving pricing context, memoized
    per (provider, channel, model); a row without the attribute counts as known."""

    __slots__ = ("_memo", "_rules")

    def __init__(self, rules: CacheRulesProvider | None) -> None:
        self._rules = rules if rules is not None else _DEFAULT_RULES
        self._memo: dict[tuple[str, str, str], bool] = {}

    def known(self, step: _Step) -> bool:
        pricing = step.inf.pricing
        key = (pricing.provider, pricing.channel, pricing.model or step.model)
        value = self._memo.get(key)
        if value is None:
            row = self._rules.rules_for(*key)
            value = getattr(row, "ttl_semantics_known", True) is not False
            self._memo[key] = value
        return value


def _param_change(cur: _Step, prev: _Step) -> str | None:
    """The first invalidating parameter change of rule 4, or None."""
    if cur.inf.pricing.speed != prev.inf.pricing.speed:
        return "fast-toggle"
    p, pp = cur.req.params, prev.req.params
    if (_differs(p.effort, pp.effort) or _differs(p.thinking, pp.thinking)) and \
            not effort_change_keeps_cache(
                agent_product=cur.req.attribution.agent_product,
                model=cur.inf.pricing.model or cur.model,
                channel=cur.inf.pricing.channel,
                client_version=cur.req.attribution.client_version,
                betas=p.betas):
        return "effort-change"
    if _differs(cur.inf.pricing.context_tier, prev.inf.pricing.context_tier):
        return "context-tier-change"
    a, pa = cur.req.attribution, prev.req.attribution
    if _differs(a.client_version, pa.client_version):
        return "client-upgrade"
    if _differs(a.cwd_key, pa.cwd_key):
        return "directory-change"
    return None


def classify_transitions(lane: Lane, *, pricer: Pricer, rules: CacheRulesProvider,
                         static_prefix_floor: Mapping[tuple[str, str], int] | None = None
                         ) -> list[Transition]:
    """Every transition ``i ≥ 1`` of *lane* (see the module docstring for the rules).

    *pricer* supplies ``min_cacheable_tokens`` for the documented prediction. *rules* supplies
    ``CacheRules.ttl_semantics_known`` of each serving context (the unknown-TTL rule, S-4;
    ``None`` uses the built-in :class:`~tokenbill.core.cache_rules.RulesTable`).
    *static_prefix_floor* is accepted for protocol symmetry with the replay engines (predicted
    reads on a predicted miss are the caller's ``static_prefix_floor[(scope, model)]``).
    O(requests + events) per lane.
    """
    steps: list[_Step] = []
    last_ttl: int | None = None
    for req in lane.requests:
        inf = req.serving_inference
        if inf is not None:
            tau = last_ttl if last_ttl is not None else _HINT_TTL_S.get(
                inf.pricing.write_ttl_hint or "")
            steps.append(_Step(req, inf, tau))
        for billed in req.billable_inferences:
            if billed.usage.cache_write > 0:
                last_ttl = _write_ttl(billed)
    if len(steps) < 2:
        return []

    events = lane.events
    event_ts = [ev.ts_ms for ev in events]
    alternating = _opus_sonnet_switches([s.model for s in steps]) >= 2
    semantics = _Semantics(rules)
    min_cache_memo: dict[tuple[object, int], int] = {}

    def min_cacheable(step: _Step) -> int:
        key = (step.inf.pricing, step.req.ts_start_ms // _DAY_MS)
        value = min_cache_memo.get(key)
        if value is None:
            got = pricer.min_cacheable_tokens(step.inf.pricing, ts_ms=step.req.ts_start_ms)
            value = got if got is not None else 0
            min_cache_memo[key] = value
        return value

    out: list[Transition] = []
    for i in range(1, len(steps)):
        cur, prev = steps[i], steps[i - 1]
        ts, prev_ts = cur.req.ts_start_ms, prev.req.ts_start_ms
        gap = ts - prev_ts
        usage, prev_usage = cur.inf.usage, prev.inf.usage
        reads = usage.cache_read
        total = reads + usage.cache_write + usage.uncached_input
        prev_prefix = prev_usage.cache_read + prev_usage.cache_write
        prev_total = prev_prefix + prev_usage.uncached_input
        expected = min(prev_prefix, total)
        missed = max(0, expected - reads)
        miss = is_miss_event(missed, expected)
        tau = cur.tau
        tau_ms = tau * 1000 if tau is not None else None
        expired = ambiguous = False
        if tau_ms is not None:
            slack, limit = AMBIGUITY_MS, tau_ms
            if not semantics.known(cur):
                # S-4: unknown TTL semantics widen both windows by the previous request's duration
                slack += _duration_ms(prev.req)
                limit = tau_ms + slack
            ambiguous = abs(gap - tau_ms) <= slack
            expired = gap > limit
        diag = cur.req.final_attempt.diagnostics
        diag_reason = diag.reason if diag is not None else None
        window = events[bisect_right(event_ts, prev_ts):bisect_right(event_ts, ts)]

        reset = any(ev.kind in _RESET_EVENTS for ev in window) or any(
            att.applied_edits or att.thinking_dropped > 0 for att in cur.req.attempts)
        model_switch = cur.model != prev.model
        param_sub = _param_change(cur, prev)

        cause, sub_cause = HIT, None
        if miss:
            if reset:
                cause = "compaction"
            elif model_switch:
                cause = "model-switch"
                sub_cause = _switch_sub_cause(lane, steps, i, window, gap, tau_ms, alternating)
            elif expired:
                cause = "ttl-expiry"
            elif param_sub is not None:
                cause, sub_cause = "param-change", param_sub
            elif diag_reason in DIAG_CAUSES:
                cause, sub_cause = DIAG_CAUSES[diag_reason]
            elif 10 * total < 9 * prev_total:
                cause = "context-shrank"
            else:
                cause = "unexplained"

        predicted: bool | None = None
        if tau_ms is not None:
            predicted = (not model_switch and not reset and param_sub is None
                         and gap <= tau_ms and expected >= min_cacheable(cur))

        out.append(Transition(
            request_id=cur.req.request_id,
            lane_key=lane.lane_key,
            index=i,
            gap_ms=gap,
            total=total,
            reads=reads,
            prev_prefix=prev_prefix,
            expected_reuse=expected,
            missed=missed,
            is_miss_event=miss,
            cause=cause,
            sub_cause=sub_cause,
            ttl_s=tau,
            ambiguous=ambiguous,
            predicted_hit=predicted,
            diag_reason=diag_reason,
        ))
    return out


def _switch_sub_cause(lane: Lane, steps: list[_Step], i: int, window: list[LaneEvent], gap: int,
                      tau_ms: int | None, alternating: bool) -> str:
    """Rule 2 sub-cause, first match."""
    cur = steps[i]
    kinds = {inf.kind for att in cur.req.attempts for inf in att.inferences}
    fallback_triggers = {_attr(ev, "trigger") for ev in window
                         if ev.kind is LaneEventKind.MODEL_FALLBACK}
    if kinds & _FALLBACK_KINDS or "refusal" in fallback_triggers:
        return "refusal-fallback"
    if "availability" in fallback_triggers and any(
            ev.kind is LaneEventKind.API_ERROR
            and (_attr(ev, "status") == 529 or _attr(ev, "error_type") in _OVERLOADED)
            for ev in window):
        return "availability-fallback"
    if (lane.kind is LaneKind.MAIN and cur.req.attribution.agent_product == "claude_code"
            and alternating
            and {_family(cur.model), _family(steps[i - 1].model)} == {"opus", "sonnet"}
            and any(ev.kind is LaneEventKind.HUMAN_PROMPT for ev in window)):
        return "plan-toggle"
    if i >= 2 and steps[i - 2].model == cur.model and tau_ms is not None and gap <= tau_ms:
        return "ping-pong"
    return "user"


def lane_first_reads_of(lanes: Iterable[Lane]) -> Iterator[tuple[str, str, int]]:
    """``(cache_scope_key, model, R)`` of each lane's first request with a serving inference (the
    in-memory equivalent of ``LedgerStore.lane_first_reads``); lanes without one are skipped."""
    for lane in lanes:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is not None:
                yield (lane.cache_scope_key, req.model, inf.usage.cache_read)
                break


def static_prefix_floor(first_reads: Iterable[tuple[str, str, int]], *, min_lanes: int = 5
                        ) -> dict[tuple[str, str], int]:
    """``(scope, model) → S``: the median first-request read over entries with ``R > 0``, only for
    keys with at least *min_lanes* such entries (the static harness prefix every lane of that scope
    and model re-reads). An even count takes the floor of the mean of the two middle values."""
    groups: dict[tuple[str, str], list[int]] = {}
    for scope, model, reads in first_reads:
        if reads > 0:
            groups.setdefault((scope, model), []).append(reads)
    out: dict[tuple[str, str], int] = {}
    for key in sorted(groups):
        values = groups[key]
        if len(values) < min_lanes:
            continue
        values.sort()
        mid = len(values) // 2
        out[key] = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) // 2
    return out
