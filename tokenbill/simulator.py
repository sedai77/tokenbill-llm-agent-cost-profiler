"""Cache-scenario replay engine: four price tags for one run.

Scenario semantics (see docs/SPEC.md; assumptions are also documented in
DESIGN.md):

- ``as-billed`` — real billed usage priced at published rates. Exact; the
  ground truth every other scenario is compared against.
- ``no-cache`` — every billed input token repriced at the full uncached input
  rate (no cache reads, no write premium). Counterfactual.
- ``optimal-cache`` — replays the calls in ``ts`` order under the documented
  provider cache rules applied to the canonical rendered text. A cache entry
  matches when it (a) was written by a call on the *same model* (prompt caches
  are per-model), (b) is a byte-identical prefix of the current call's
  rendering, (c) is within :data:`~tokenbill.pricing.CACHE_TTL_SECONDS` of its
  last use (sliding refresh per :data:`~tokenbill.pricing.TTL_REFRESH_ON_READ`),
  and (d) meets the model's minimum cacheable prefix length in approx tokens.
  The matched prefix is charged at the read rate. The remainder is charged at
  the write-premium rate ONLY when the written entry is actually read by a
  later call in the replay (retrospective accounting): the replay knows the
  whole run, and a truly optimal policy never pays the 1.25x premium for a
  write that will never be read back — such remainders (including the always
  final, never-read write of the last call) are charged at the plain uncached
  rate instead. As a guarantee, the scenario is clamped at the no-cache price:
  not caching at all is always available to an optimal policy, so optimal can
  never cost more than no-cache. A single breakpoint is assumed at the end of
  messages on every call — optimal placement — so the
  :data:`~tokenbill.pricing.MAX_BREAKPOINTS` cap (4) is never binding for this
  policy. Below the minimum cacheable length nothing is cached and the
  remainder is charged uncached.
- ``fixed-cache`` — the optimal replay over breaker-repaired calls
  (``breakers.repaired_calls``); identical to ``optimal-cache`` when no
  repaired calls are supplied.

Honesty rules: simulated token splits are char-fraction approximations scaled
so each call's input total matches its billed ``total_input`` exactly; the
scenario notes label them approx. Only ``as-billed`` is exact. When billed
usage shows real cache activity, the ``optimal-cache`` note reports the
agreement between billed and simulated cache reads — the validation check the
README cites.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from tokenbill.pricing import (
    CACHE_TTL_SECONDS,
    PRICING,
    TTL_REFRESH_ON_READ,
    ModelPricing,
    price_usd,
)
from tokenbill.trace import Call, Run, approx_tokens, rendered_text

_MTOK = 1_000_000

# Fallback limits for models without a pricing entry: dollars are reported as
# None upstream, but the replay still needs a min-cacheable gate — use the
# dataclass defaults (the rate fields are never read for unknown models).
_DEFAULT_LIMITS = ModelPricing(input_per_mtok=0.0, output_per_mtok=0.0)

#: Scenario names in the order :func:`simulate` returns them.
SCENARIO_NAMES = ("as-billed", "no-cache", "optimal-cache", "fixed-cache")

_TOKEN_KEYS = ("uncached", "cache_write", "cache_read", "output", "total_input")


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's token totals, price tag, and honesty note.

    ``tokens`` uses the analyzer's category keys: ``uncached``,
    ``cache_write``, ``cache_read``, ``output``, ``total_input`` (the three
    input categories always sum to ``total_input``). ``dollars`` is ``None``
    when any call's model has no pricing entry. ``note`` is a one-line honest
    description of the scenario's assumptions.
    """

    name: str
    tokens: dict[str, int]
    dollars: float | None
    note: str


@dataclass
class _CacheEntry:
    """A simulated cache entry: model, exact rendered prefix, last-use time.

    ``last_writer`` is the replay position of the call whose write premium
    created (or, after TTL expiry, re-created) this entry — the retrospective
    accounting marks that position productive when the entry is later read.
    """

    model: str
    text: str
    alive_ts: float
    last_writer: int


def _zero_tokens() -> dict[str, int]:
    return dict.fromkeys(_TOKEN_KEYS, 0)


def _as_billed(calls: Sequence[Call]) -> ScenarioResult:
    tokens = _zero_tokens()
    dollars: float | None = 0.0
    for call in calls:
        usage = call.usage
        tokens["uncached"] += usage.input_tokens
        tokens["cache_write"] += usage.cache_creation_input_tokens
        tokens["cache_read"] += usage.cache_read_input_tokens
        tokens["output"] += usage.output_tokens
        tokens["total_input"] += usage.total_input
        if dollars is not None:
            call_usd = price_usd(call.model, usage)
            dollars = None if call_usd is None else dollars + call_usd
    note = "exact: real billed usage priced at published rates (ground truth)"
    return ScenarioResult("as-billed", tokens, dollars, _flag_unpriced(note, dollars))


def _no_cache(calls: Sequence[Call]) -> ScenarioResult:
    tokens = _zero_tokens()
    dollars: float | None = 0.0
    for call in calls:
        usage = call.usage
        tokens["uncached"] += usage.total_input
        tokens["output"] += usage.output_tokens
        tokens["total_input"] += usage.total_input
        pricing = PRICING.get(call.model)
        if pricing is None:
            dollars = None
        elif dollars is not None:
            dollars += (
                usage.total_input * pricing.input_per_mtok
                + usage.output_tokens * pricing.output_per_mtok
            ) / _MTOK
    note = (
        "counterfactual: every billed input token repriced at the full uncached "
        "rate (no cache reads, no write premium)"
    )
    return ScenarioResult("no-cache", tokens, dollars, _flag_unpriced(note, dollars))


def _replay(calls: Sequence[Call], name: str, note: str) -> ScenarioResult:
    """Replay *calls* in ``ts`` order under the documented cache rules.

    Two passes. The matching pass resolves reads exactly as the provider
    would (per-model entries, TTL with sliding refresh, min-cacheable gate)
    and records which write events are later read. The billing pass then
    charges each call's remainder at the write premium only when its write
    was productive — an optimal policy with knowledge of the whole run never
    caches an entry nothing will read — and at the plain uncached rate
    otherwise. Never-read entries never served a read, so skipping their
    writes cannot change the matching pass's outcome. Finally the dollar
    total is clamped at the no-cache price (not caching is always available
    to an optimal policy).
    """
    entries: list[_CacheEntry] = []
    tokens = _zero_tokens()
    #: per call: (pricing, read, remainder, output_tokens, cacheable, writer_pos)
    billing: list[tuple[ModelPricing | None, int, int, int, bool, int | None]] = []
    productive: set[int] = set()  # write positions whose entry a later call read
    for pos, call in enumerate(sorted(calls, key=lambda c: (c.ts, c.index))):
        pricing = PRICING.get(call.model)
        limits = pricing if pricing is not None else _DEFAULT_LIMITS
        text = rendered_text(call)
        chars = len(text)
        billed_input = call.usage.total_input

        # Longest previously-written, same-model, still-alive, byte-identical
        # prefix that meets the min-cacheable gate (approx tokens).
        best: _CacheEntry | None = None
        for entry in entries:
            if (
                entry.model == call.model  # prompt caches are per-model
                and len(entry.text) <= chars
                and text.startswith(entry.text)
                and call.ts - entry.alive_ts <= CACHE_TTL_SECONDS
                and approx_tokens(entry.text) >= limits.min_cacheable_prefix_tokens
                and (best is None or len(entry.text) > len(best.text))
            ):
                best = entry
        matched_chars = len(best.text) if best is not None else 0
        if best is not None:
            productive.add(best.last_writer)  # that write premium paid off
            if TTL_REFRESH_ON_READ:
                best.alive_ts = call.ts  # sliding expiry: a read refreshes the TTL

        # Char-fraction token split, scaled so the three input categories sum
        # to this call's billed total_input exactly (approx by construction).
        read = round(billed_input * matched_chars / chars) if chars else 0
        remainder = billed_input - read
        cacheable = chars > 0 and approx_tokens(text) >= limits.min_cacheable_prefix_tokens
        writer_pos: int | None = None
        if cacheable:
            # Breakpoint at end of messages: the remainder is the newly-cached
            # extension; whether its premium is billed is decided after the
            # replay, once we know if anything reads this entry.
            existing = next(
                (e for e in entries if e.model == call.model and e.text == text), None
            )
            if existing is not None:
                existing.alive_ts = call.ts
                if remainder > 0:  # stale entry re-written by this call
                    existing.last_writer = pos
                    writer_pos = pos
            elif matched_chars < chars:
                entries.append(_CacheEntry(call.model, text, call.ts, pos))
                writer_pos = pos
        billing.append(
            (pricing, read, remainder, call.usage.output_tokens, cacheable, writer_pos)
        )

    dollars: float | None = 0.0
    no_cache_floor: float | None = 0.0
    for pricing, read, remainder, output, cacheable, writer_pos in billing:
        # Retrospective write accounting: premium only for extensions a later
        # call actually read; never-read extensions stay plain uncached input.
        if cacheable and writer_pos is not None and writer_pos in productive:
            uncached, write = 0, remainder
        else:
            uncached, write = remainder, 0
        tokens["uncached"] += uncached
        tokens["cache_write"] += write
        tokens["cache_read"] += read
        tokens["output"] += output
        tokens["total_input"] += read + remainder

        if pricing is None:
            dollars = None
            no_cache_floor = None
        elif dollars is not None and no_cache_floor is not None:
            dollars += (
                uncached * pricing.input_per_mtok
                + write * pricing.input_per_mtok * pricing.cache_write_multiplier
                + read * pricing.input_per_mtok * pricing.cache_read_multiplier
                + output * pricing.output_per_mtok
            ) / _MTOK
            no_cache_floor += (
                (read + remainder) * pricing.input_per_mtok + output * pricing.output_per_mtok
            ) / _MTOK
    if dollars is not None and no_cache_floor is not None and dollars > no_cache_floor:
        # Safety net for pathological billed-token distributions: an optimal
        # policy can always decline to cache, so it never beats no-cache.
        dollars = no_cache_floor
        note += "; clamped at the no-cache floor (write premiums exceeded read savings)"
    return ScenarioResult(name, tokens, dollars, _flag_unpriced(note, dollars))


def _flag_unpriced(note: str, dollars: float | None) -> str:
    if dollars is None:
        return note + "; dollars omitted (model missing from the pricing table)"
    return note


def simulate(run: Run, fixed_calls: list[Call] | None = None) -> list[ScenarioResult]:
    """Price *run* under the four scenarios, in :data:`SCENARIO_NAMES` order.

    ``fixed_calls`` is the breaker-repaired rendering from
    ``breakers.repaired_calls``; when omitted, ``fixed-cache`` degenerates to
    ``optimal-cache``. Validation hook: when the run's billed usage shows
    nonzero cache activity, the ``optimal-cache`` note reports billed vs
    simulated cache reads and their agreement ratio (1.0 = perfect) — on a
    well-behaved trace the two must agree within per-call integer rounding.
    """
    calls = list(run.calls)
    as_billed = _as_billed(calls)
    no_cache = _no_cache(calls)

    optimal_note = (
        f"simulated (approx): documented cache rules — {CACHE_TTL_SECONDS}s TTL "
        "sliding on read, min-cacheable gate, one breakpoint at end of messages; "
        "char-based token split scaled to billed totals"
    )
    optimal = _replay(calls, "optimal-cache", optimal_note)
    if as_billed.tokens["cache_read"] or as_billed.tokens["cache_write"]:
        billed_reads = as_billed.tokens["cache_read"]
        predicted_reads = optimal.tokens["cache_read"]
        top = max(billed_reads, predicted_reads)
        agreement = (min(billed_reads, predicted_reads) / top) if top else 1.0
        optimal = replace(
            optimal,
            note=optimal.note
            + (
                f"; validation: billed cache reads {billed_reads} vs simulated "
                f"{predicted_reads} (agreement {agreement:.3f})"
            ),
        )

    if fixed_calls is not None:
        fixed = _replay(
            fixed_calls,
            "fixed-cache",
            "simulated (approx): optimal-cache rules over the breaker-repaired "
            "rendering; billed usage totals reused for the token split",
        )
    else:
        fixed = _replay(
            calls,
            "fixed-cache",
            "simulated (approx): no repairs supplied — identical to optimal-cache",
        )
    return [as_billed, no_cache, optimal, fixed]
