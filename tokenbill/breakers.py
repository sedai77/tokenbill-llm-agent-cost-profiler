"""Cache-breaker detection: divergence classification, fixes, and repair.

For each pair of consecutive calls the first matching rule wins (SPEC priority
order):

1. ``model`` differs from the previous call → ``model-switch``.
2. diverging segment is tools → ``tool-churn``.
3. diverging segment is system → ``volatile-system`` when the changed span
   strictly overlaps a :data:`VOLATILE_PATTERNS` match (ISO dates/times, unix
   timestamps, UUIDs, monotonic counters) AND substituting every volatile
   match makes the two system prompts byte-identical — i.e. the volatile
   values fully explain the divergence. Otherwise ``system-edit`` — the
   non-volatile variant with its own fix sentence (whose repair, pinning the
   system text, also covers mixed volatile-plus-edit changes).
4. diverging segment is a message that already existed in the previous call →
   ``history-rewrite``.
5. no divergence, shared prefix at least the model's minimum cacheable length
   (approx tokens), but ``cache_breakpoints == 0`` and billed cache activity
   is zero → ``missing-breakpoint``.

:func:`detect` reports one :class:`Breaker` per distinct cause with the first
call index where it bites (the demo's ``timestamp`` scenario diverges on every
call but yields exactly one ``volatile-system`` breaker at call 1).
``est_recovered_usd`` isolates each cause: the run is re-simulated with only
that one breaker repaired, and the value is as-billed minus fixed-cache
dollars (positive = money recovered by the fix; approx, since the fixed-cache
scenario is simulated). Causes with no mechanical repair (``model-switch``,
``history-rewrite``) get ``None`` — a billed-minus-fixed number there would
price the optimal replay of the still-broken run, which is not attributable
to the displayed fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from tokenbill.common import canonical_json
from tokenbill.pricing import PRICING, ModelPricing
from tokenbill.simulator import simulate
from tokenbill.trace import Call, Run, approx_tokens, diverging_segment, rendered_text

# Heuristic patterns for content that legitimately changes every call and
# therefore must not live in the cached prefix. Tested as a module constant
# (SPEC). Order matters for :func:`repaired_calls`: the combined ISO datetime
# comes first so one substitution covers the whole stamp.
VOLATILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ISO datetime, e.g. "2026-07-26T14:03:05Z" or "2026-07-26 14:03:05".
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
    # ISO date, e.g. "2026-07-26".
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    # Clock time, e.g. "14:03" or "14:03:05".
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    # Unix timestamp in seconds (~2017..2051 range guard against arbitrary
    # 10-digit numbers).
    re.compile(r"\b(?:1[5-9]|2[0-5])\d{8}\b"),
    # UUID.
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # Monotonic counters, e.g. "attempt 3", "seq=42", "turn #7".
    re.compile(
        r"\b(?:seq|counter|attempt|turn|step|iteration|request|call|run)[ _#:=-]{1,3}\d+\b",
        re.IGNORECASE,
    ),
)

#: What repaired volatile spans are replaced with (stable across all calls).
STABLE_PLACEHOLDER = "<volatile>"

_DEFAULT_LIMITS = ModelPricing(input_per_mtok=0.0, output_per_mtok=0.0)

_FIXES: dict[str, str] = {
    "model-switch": (
        "prompt caches are per-model: keep one model for the whole run, or expect a "
        "cold cache after every switch"
    ),
    "tool-churn": (
        "send tool definitions in one fixed order on every call (sort them once at "
        "startup); reordering rewrites the cached prefix"
    ),
    "volatile-system": (
        "move the volatile value (timestamp/UUID/counter) out of the system prompt — "
        "inject it in the latest user message instead"
    ),
    "system-edit": (
        "keep the system prompt byte-stable for the whole run; put per-turn context in "
        "the latest user message instead of editing the system text"
    ),
    "history-rewrite": (
        "append new messages instead of rewriting earlier ones; any edit above the "
        "cache breakpoint invalidates the cached prefix"
    ),
    "missing-breakpoint": (
        "add a cache_control breakpoint (for example on the last message); the stable "
        "prefix already meets the minimum cacheable length"
    ),
}

# SPEC rule order, used only to order breakers that first bite at the same call.
_KIND_PRIORITY = {
    "model-switch": 0,
    "tool-churn": 1,
    "volatile-system": 2,
    "system-edit": 2,
    "history-rewrite": 3,
    "missing-breakpoint": 4,
}


@dataclass(frozen=True)
class Breaker:
    """One detected cache-breaking cause with a concrete, priced fix.

    ``kind`` is one of ``model-switch``, ``tool-churn``, ``volatile-system``,
    ``system-edit`` (the non-volatile system-divergence variant),
    ``history-rewrite``, ``missing-breakpoint``. ``evidence`` shows the exact
    changed span, truncated, with char offsets. ``est_recovered_usd`` is
    as-billed minus fixed-cache dollars with only this breaker repaired
    (positive = recovered; ``None`` when pricing is unknown or when the kind
    has no mechanical repair — ``model-switch`` and ``history-rewrite`` —
    so no honest per-fix dollar figure exists).
    """

    kind: str
    first_call_index: int
    evidence: str
    fix: str
    est_recovered_usd: float | None


def _truncate(text: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    keep = (limit - 3) // 2
    return f"{text[:keep]}...{text[-keep:]}"


def _changed_window(prev: str, cur: str) -> tuple[int, int, int]:
    """``(lo, hi_prev, hi_cur)``: minimal spans ``prev[lo:hi_prev]`` vs ``cur[lo:hi_cur]``."""
    lo = 0
    limit = min(len(prev), len(cur))
    while lo < limit and prev[lo] == cur[lo]:
        lo += 1
    hi_prev, hi_cur = len(prev), len(cur)
    while hi_prev > lo and hi_cur > lo and prev[hi_prev - 1] == cur[hi_cur - 1]:
        hi_prev -= 1
        hi_cur -= 1
    return lo, hi_prev, hi_cur


def _volatile_overlap(text: str, lo: int, hi: int) -> re.Match[str] | None:
    """First volatile-pattern match strictly overlapping ``text[lo:hi]``.

    Strict means at least one shared character: a match that merely touches
    the changed span (e.g. a stable date immediately before an edited
    punctuation mark) must not classify the change as volatile.
    """
    for pattern in VOLATILE_PATTERNS:
        for match in pattern.finditer(text):
            if match.start() < hi and match.end() > lo:
                return match
    return None


def _excerpt(text: str, lo: int, hi: int, context: int = 24) -> str:
    start = max(0, lo - context)
    end = min(len(text), hi + context)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + _truncate(text[start:end]) + suffix


def _tool_names(call: Call) -> list[str]:
    return [str(tool.get("name", "?")) for tool in call.tools]


def _classify_system(prev: Call, cur: Call) -> tuple[str, str]:
    lo, hi_prev, hi_cur = _changed_window(prev.system, cur.system)
    match_cur = _volatile_overlap(cur.system, lo, hi_cur)
    match_prev = _volatile_overlap(prev.system, lo, hi_prev)
    # Volatile only when the volatile values fully explain the divergence:
    # after substituting every volatile match the systems must be identical.
    # Otherwise the volatile repair would leave the run still diverging and
    # the honest classification (and fix) is system-edit.
    volatile = (match_cur is not None or match_prev is not None) and _stabilized(
        prev.system
    ) == _stabilized(cur.system)
    if volatile:
        # Widen each excerpt to cover the volatile token so the evidence shows
        # the full stamp, not just the digits that changed.
        prev_lo = min(lo, match_prev.start()) if match_prev else lo
        prev_hi = max(hi_prev, match_prev.end()) if match_prev else hi_prev
        cur_lo = min(lo, match_cur.start()) if match_cur else lo
        cur_hi = max(hi_cur, match_cur.end()) if match_cur else hi_cur
        evidence = (
            f"system chars [{cur_lo}:{cur_hi}] at call {cur.index}: "
            f"{_excerpt(prev.system, prev_lo, prev_hi)!r} -> "
            f"{_excerpt(cur.system, cur_lo, cur_hi)!r}"
        )
        return "volatile-system", evidence
    evidence = (
        f"system chars [{lo}:{hi_cur}] at call {cur.index}: "
        f"{_excerpt(prev.system, lo, hi_prev)!r} -> {_excerpt(cur.system, lo, hi_cur)!r}"
    )
    return "system-edit", evidence


def _classify_pair(prev: Call, cur: Call, first_seen_tools: list[str]) -> tuple[str, str] | None:
    """Classify one consecutive pair per the SPEC priority order."""
    if cur.model != prev.model:
        return "model-switch", (
            f"model changed at call {cur.index}: {prev.model!r} -> {cur.model!r}"
        )
    divergence = diverging_segment(prev, cur)
    if divergence is not None:
        seg_index, seg_kind = divergence
        if seg_kind == "tools":
            return "tool-churn", (
                f"tool order changed at call {cur.index}: first seen "
                f"{first_seen_tools} -> {_tool_names(cur)}"
            )
        if seg_kind == "system":
            return _classify_system(prev, cur)
        # Message segments start after the tools and system segments.
        msg_index = seg_index - 2
        if 0 <= msg_index < len(prev.messages):
            prev_text = canonical_json(prev.messages[msg_index])
            cur_text = (
                canonical_json(cur.messages[msg_index]) if msg_index < len(cur.messages) else ""
            )
            lo, hi_prev, hi_cur = _changed_window(prev_text, cur_text)
            return "history-rewrite", (
                f"messages[{msg_index}] rewritten at call {cur.index}, chars "
                f"[{lo}:{hi_cur}]: {_excerpt(prev_text, lo, hi_prev)!r} -> "
                f"{_excerpt(cur_text, lo, hi_cur)!r}"
            )
        return None
    # Rule 5: byte-stable prefix that was never cached.
    usage = cur.usage
    if (
        cur.cache_breakpoints == 0
        and usage.cache_read_input_tokens == 0
        and usage.cache_creation_input_tokens == 0
    ):
        limits = PRICING.get(cur.model) or _DEFAULT_LIMITS
        shared_chars = min(len(rendered_text(prev)), len(rendered_text(cur)))
        prefix_tokens = approx_tokens(rendered_text(prev)[:shared_chars])
        if prefix_tokens >= limits.min_cacheable_prefix_tokens:
            return "missing-breakpoint", (
                f"calls {prev.index}->{cur.index} share a byte-stable prefix of "
                f"~{prefix_tokens:.0f} approx tokens (min cacheable "
                f"{limits.min_cacheable_prefix_tokens}) but cache_breakpoints=0 "
                "and billed cache activity is 0"
            )
    return None


def detect(run: Run) -> list[Breaker]:
    """Detect cache breakers in *run*: one :class:`Breaker` per distinct cause.

    Consecutive call pairs are classified per the SPEC priority order; events
    of the same kind are collapsed to the first call index where the cause
    bites. Each breaker's ``est_recovered_usd`` is computed by re-simulating
    the run with only that breaker repaired (see :func:`repaired_calls`), so
    the dollar estimate isolates each cause; kinds with no mechanical repair
    report ``None`` instead of a number the fix cannot claim.
    """
    calls = run.calls
    if len(calls) < 2:
        return []
    first_seen_tools = _tool_names(calls[0])
    events: dict[str, tuple[int, str]] = {}
    for i in range(1, len(calls)):
        classified = _classify_pair(calls[i - 1], calls[i], first_seen_tools)
        if classified is None:
            continue
        kind, evidence = classified
        events.setdefault(kind, (i, evidence))

    ordered = sorted(events.items(), key=lambda item: (item[1][0], _KIND_PRIORITY[item[0]]))
    breakers: list[Breaker] = []
    for kind, (index, evidence) in ordered:
        breaker = Breaker(
            kind=kind,
            first_call_index=index,
            evidence=evidence,
            fix=_FIXES[kind],
            est_recovered_usd=None,
        )
        breakers.append(replace(breaker, est_recovered_usd=_estimate(run, breaker)))
    return breakers


def _estimate(run: Run, breaker: Breaker) -> float | None:
    """As-billed minus fixed-cache dollars with only *breaker* repaired.

    ``None`` when the repair left the calls untouched (no mechanical repair
    exists — model-switch, history-rewrite): billed minus fixed would then be
    the optimal replay of the still-broken run, a number the displayed fix
    cannot claim. ``None`` also when any call's model is unpriced.
    """
    repaired = repaired_calls(run, [breaker])
    if repaired == list(run.calls):
        return None
    results = {r.name: r for r in simulate(run, repaired)}
    billed = results["as-billed"].dollars
    fixed = results["fixed-cache"].dollars
    if billed is None or fixed is None:
        return None
    return billed - fixed


def _stabilized(system: str) -> str:
    for pattern in VOLATILE_PATTERNS:
        system = pattern.sub(STABLE_PLACEHOLDER, system)
    return system


def repaired_calls(run: Run, breakers: list[Breaker]) -> list[Call]:
    """Return *run*'s calls with the given breakers neutralized for simulation.

    Repairs (SPEC): volatile spans in the system prompt are replaced by the
    stable :data:`STABLE_PLACEHOLDER`; a mid-run system edit is pinned to the
    first call's system text; tool order is restored to first-seen order;
    ``cache_breakpoints=1`` where it was 0. ``model-switch`` and
    ``history-rewrite`` have no mechanical repair (rewriting content or
    models would change semantics) and leave the calls untouched. Content
    semantics are never mutated otherwise; billed ``usage`` is preserved so
    the fixed-cache simulation still scales to real billed totals.
    """
    kinds = {breaker.kind for breaker in breakers}
    calls = list(run.calls)
    if "tool-churn" in kinds:
        first_seen: dict[str, int] = {}
        for call in calls:
            for tool in call.tools:
                first_seen.setdefault(canonical_json(tool), len(first_seen))
        calls = [
            replace(
                call,
                tools=tuple(sorted(call.tools, key=lambda t: first_seen[canonical_json(t)])),
            )
            for call in calls
        ]
    if "volatile-system" in kinds:
        calls = [replace(call, system=_stabilized(call.system)) for call in calls]
    if "system-edit" in kinds and calls:
        calls = [replace(call, system=calls[0].system) for call in calls]
    if "missing-breakpoint" in kinds:
        calls = [
            replace(call, cache_breakpoints=1) if call.cache_breakpoints == 0 else call
            for call in calls
        ]
    return calls
