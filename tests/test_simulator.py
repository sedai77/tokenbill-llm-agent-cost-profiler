"""Unit tests for the cache-scenario replay engine (tokenbill.simulator)."""

from __future__ import annotations

import re

import pytest

from tokenbill.simulator import SCENARIO_NAMES, simulate
from tokenbill.trace import Call, Run, Usage, rendered_text

MODEL = "claude-sonnet-5"  # $3/MTok in, $15/MTok out, min cacheable 1024 approx tokens
MTOK = 1_000_000

# ~5,180 chars -> ~1,400 approx tokens: comfortably over the 1,024-token
# min-cacheable gate for claude-sonnet-5 from the very first call.
BIG_SYSTEM = "You are a meticulous release engineer for the walcache project. " * 80

TINY_SYSTEM = "Be terse."  # far below the min-cacheable gate


def history(n: int) -> tuple[dict, ...]:
    """A deterministic, append-only message history of *n* user turns."""
    return tuple(
        {"role": "user", "content": f"message {i}: please continue the code review."}
        for i in range(n)
    )


def make_call(
    index: int,
    *,
    ts: float | None = None,
    model: str = MODEL,
    system: str = BIG_SYSTEM,
    tools: tuple[dict, ...] = (),
    messages: tuple[dict, ...] | None = None,
    breakpoints: int = 1,
    usage: Usage | None = None,
) -> Call:
    if messages is None:
        messages = history(index + 1)
    if usage is None:
        usage = Usage(
            input_tokens=1_000 + 100 * index,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=50,
        )
    return Call(
        run_id="r",
        index=index,
        ts=1_000_000.0 + 10.0 * index if ts is None else ts,
        model=model,
        system=system,
        tools=tools,
        messages=messages,
        cache_breakpoints=breakpoints,
        usage=usage,
        stop_reason="end_turn",
    )


def run_of(*calls: Call) -> Run:
    return Run(run_id=calls[0].run_id, calls=calls)


def by_name(results: list) -> dict[str, object]:
    return {r.name: r for r in results}


def test_scenario_names_and_order() -> None:
    results = simulate(run_of(make_call(0), make_call(1)))
    assert [r.name for r in results] == list(SCENARIO_NAMES)


def test_as_billed_is_exact_billed_usage_times_rates() -> None:
    usage0 = Usage(
        input_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=900,
        output_tokens=40,
    )
    usage1 = Usage(
        input_tokens=50, cache_read_input_tokens=900, cache_creation_input_tokens=200,
        output_tokens=60,
    )
    run = run_of(make_call(0, usage=usage0), make_call(1, usage=usage1))
    as_billed = by_name(simulate(run))["as-billed"]
    assert as_billed.tokens == {
        "uncached": 150,
        "cache_write": 1_100,
        "cache_read": 900,
        "output": 100,
        "total_input": 2_150,
    }
    # 150 uncached x $3 + 1,100 writes x $3 x 1.25 + 900 reads x $3 x 0.10
    # + 100 output x $15, all per MTok.
    expected = (150 * 3.0 + 1_100 * 3.0 * 1.25 + 900 * 3.0 * 0.10 + 100 * 15.0) / MTOK
    assert as_billed.dollars == pytest.approx(expected, rel=1e-12)
    assert "exact" in as_billed.note


def test_no_cache_reprices_all_input_at_full_rate() -> None:
    usage = Usage(
        input_tokens=100, cache_read_input_tokens=800, cache_creation_input_tokens=100,
        output_tokens=25,
    )
    run = run_of(make_call(0, usage=usage))
    no_cache = by_name(simulate(run))["no-cache"]
    assert no_cache.tokens == {
        "uncached": 1_000,
        "cache_write": 0,
        "cache_read": 0,
        "output": 25,
        "total_input": 1_000,
    }
    # All 1,000 input tokens at the full $3/MTok rate — no write premium, no reads.
    assert no_cache.dollars == pytest.approx((1_000 * 3.0 + 25 * 15.0) / MTOK, rel=1e-12)


def test_optimal_replay_reads_prefix_and_writes_extension() -> None:
    total0, total1 = 2_000, 2_600
    call0 = make_call(0, usage=Usage(total0, 0, 0, 40))
    call1 = make_call(1, usage=Usage(total1, 0, 0, 40))  # extends call0 by one message
    len0, len1 = len(rendered_text(call0)), len(rendered_text(call1))
    assert rendered_text(call1).startswith(rendered_text(call0))

    optimal = by_name(simulate(run_of(call0, call1)))["optimal-cache"]
    # Call 0: no entry yet -> the whole billed total (2,000) is written and
    # read back by call 1, so the write premium is productive. Call 1: the
    # matched prefix is call 0's full rendering, so reads = round(2,600 x
    # len0/len1); its extension is never read again (nothing follows), so the
    # retrospective accounting bills it as plain uncached input, not a write.
    read1 = round(total1 * len0 / len1)
    write1 = total1 - read1
    assert optimal.tokens == {
        "uncached": write1,
        "cache_write": total0,
        "cache_read": read1,
        "output": 80,
        "total_input": total0 + total1,
    }
    expected = (
        total0 * 3.0 * 1.25 + write1 * 3.0 + read1 * 3.0 * 0.10 + 80 * 15.0
    ) / MTOK
    assert optimal.dollars == pytest.approx(expected, rel=1e-9)
    assert "approx" in optimal.note


def test_min_cacheable_gate_disables_caching() -> None:
    calls = [
        make_call(i, system=TINY_SYSTEM, usage=Usage(500, 0, 0, 10)) for i in range(3)
    ]
    results = by_name(simulate(run_of(*calls)))
    optimal, no_cache = results["optimal-cache"], results["no-cache"]
    # The whole rendering is far below 1,024 approx tokens, so nothing may be
    # cached: the optimal scenario degenerates to no-cache.
    assert optimal.tokens == no_cache.tokens
    assert optimal.dollars == pytest.approx(no_cache.dollars, rel=1e-12)
    assert optimal.tokens["cache_read"] == 0
    assert optimal.tokens["cache_write"] == 0


def test_ttl_expiry_prevents_reads() -> None:
    call0 = make_call(0, ts=0.0, usage=Usage(2_000, 0, 0, 10))
    call1 = make_call(1, ts=400.0, usage=Usage(2_500, 0, 0, 10))  # 400s > 300s TTL
    optimal = by_name(simulate(run_of(call0, call1)))["optimal-cache"]
    assert optimal.tokens["cache_read"] == 0
    # Neither entry is ever read, so neither write premium is charged: the
    # optimal policy would simply not cache, and everything stays uncached.
    assert optimal.tokens["cache_write"] == 0
    assert optimal.tokens["uncached"] == 4_500


def test_ttl_slides_on_read() -> None:
    # Three byte-identical calls at t=0, 200, 400. The entry written at t=0
    # would be dead at t=400 (400 > 300), but the read at t=200 refreshes the
    # TTL (sliding expiry), so the t=400 call still hits.
    shared = history(1)
    call0 = make_call(0, ts=0.0, messages=shared, usage=Usage(2_000, 0, 0, 10))
    call1 = make_call(1, ts=200.0, messages=shared, usage=Usage(2_000, 0, 0, 10))
    call2 = make_call(2, ts=400.0, messages=shared, usage=Usage(2_000, 0, 0, 10))
    optimal = by_name(simulate(run_of(call0, call1, call2)))["optimal-cache"]
    assert optimal.tokens["cache_write"] == 2_000  # written once, at t=0
    assert optimal.tokens["cache_read"] == 4_000  # full hits at t=200 and t=400


def test_every_scenario_preserves_billed_input_totals() -> None:
    calls = [
        make_call(0, usage=Usage(1_234, 0, 0, 77)),
        make_call(1, usage=Usage(17, 2_000, 333, 88)),
        make_call(2, usage=Usage(2_951, 0, 0, 99)),
    ]
    billed_input = sum(c.usage.total_input for c in calls)
    for result in simulate(run_of(*calls)):
        assert result.tokens["total_input"] == billed_input
        split = (
            result.tokens["uncached"]
            + result.tokens["cache_write"]
            + result.tokens["cache_read"]
        )
        assert split == billed_input
        assert result.tokens["output"] == 264


def test_unknown_model_reports_tokens_without_dollars() -> None:
    calls = [make_call(i, model="mystery-9") for i in range(2)]
    for result in simulate(run_of(*calls)):
        assert result.dollars is None
        assert result.tokens["total_input"] == 2_100
        assert "dollars omitted" in result.note


def test_fixed_cache_uses_repaired_calls() -> None:
    # Broken run: the system prompt differs on every call, so the optimal
    # replay never finds a byte-identical prefix long enough to read.
    broken = [
        make_call(i, system=f"{BIG_SYSTEM} variant {i}", usage=Usage(2_000, 0, 0, 10))
        for i in range(3)
    ]
    repaired = [
        make_call(i, system=BIG_SYSTEM, usage=Usage(2_000, 0, 0, 10)) for i in range(3)
    ]
    results = by_name(simulate(run_of(*broken), fixed_calls=repaired))
    assert results["optimal-cache"].tokens["cache_read"] == 0
    assert results["fixed-cache"].tokens["cache_read"] > 0
    assert results["fixed-cache"].dollars < results["optimal-cache"].dollars
    assert "repair" in results["fixed-cache"].note


def test_fixed_cache_without_repairs_equals_optimal() -> None:
    run = run_of(make_call(0), make_call(1))
    results = by_name(simulate(run))
    assert results["fixed-cache"].tokens == results["optimal-cache"].tokens
    assert results["fixed-cache"].dollars == pytest.approx(
        results["optimal-cache"].dollars, rel=1e-12
    )
    assert "identical to optimal-cache" in results["fixed-cache"].note


def test_agreement_note_on_cache_active_run() -> None:
    # Billed usage constructed AS IF caching worked: call 1 reads exactly what
    # call 0 wrote. The optimal replay must predict (almost) the same reads.
    call0 = make_call(0, usage=Usage(0, 0, 0, 10))
    call1 = make_call(1, usage=Usage(0, 0, 0, 10))
    len0, len1 = len(rendered_text(call0)), len(rendered_text(call1))
    total0, total1 = int(len0 / 3.7), int(len1 / 3.7)
    call0 = make_call(0, usage=Usage(0, 0, total0, 10))
    call1 = make_call(1, usage=Usage(0, total0, total1 - total0, 10))
    results = by_name(simulate(run_of(call0, call1)))
    optimal = results["optimal-cache"]
    assert abs(optimal.tokens["cache_read"] - total0) <= 2  # integer rounding only
    assert "validation: billed cache reads" in optimal.note
    match = re.search(r"agreement (\d\.\d{3})", optimal.note)
    assert match is not None and float(match.group(1)) >= 0.99


def test_no_agreement_note_without_billed_cache_activity() -> None:
    results = by_name(simulate(run_of(make_call(0), make_call(1))))
    assert "validation" not in results["optimal-cache"].note


def test_optimal_never_exceeds_no_cache_on_byte_unstable_run() -> None:
    # Regression: the replay used to charge the 1.25x write premium on every
    # unmatched remainder even when the entry could never be read back, so a
    # volatile run priced "optimal caching" ABOVE doing nothing at all.
    calls = [
        make_call(i, system=f"{BIG_SYSTEM} stamp {i}", usage=Usage(2_000, 0, 0, 10))
        for i in range(3)
    ]
    results = by_name(simulate(run_of(*calls)))
    optimal, no_cache = results["optimal-cache"], results["no-cache"]
    # No entry is ever a prefix of a later call, so nothing is cached: the
    # optimal policy degenerates to no-cache instead of paying dead premiums.
    assert optimal.tokens["cache_read"] == 0
    assert optimal.tokens["cache_write"] == 0
    assert optimal.dollars == pytest.approx(no_cache.dollars, rel=1e-12)


def test_final_never_read_write_is_not_charged_a_premium() -> None:
    # Regression: the last call's extension can never be read back; billing
    # it at the write premium made est_recovered_usd go negative on short
    # history-rewrite runs ("recovers ~$-0.0045").
    calls = [make_call(i, usage=Usage(2_000, 0, 0, 10)) for i in range(3)]
    results = by_name(simulate(run_of(*calls)))
    optimal, no_cache = results["optimal-cache"], results["no-cache"]
    assert optimal.dollars is not None and no_cache.dollars is not None
    assert optimal.dollars <= no_cache.dollars
    # The two productive writes (calls 0 and 1, each read by the next call)
    # are premium-billed; call 2's extension is plain uncached input.
    assert optimal.tokens["cache_write"] > 0
    assert optimal.tokens["uncached"] > 0


def test_cache_entries_are_per_model() -> None:
    # Regression: entries were keyed by text only, so a call could "read" a
    # cache entry written under a different model. Prompt caches are
    # per-model; identical bytes on another model must be a cold cache.
    shared = history(3)
    call0 = make_call(0, messages=shared, usage=Usage(2_000, 0, 0, 10))
    call1 = make_call(
        1, model="claude-opus-4-8", messages=shared, usage=Usage(2_000, 0, 0, 10)
    )
    optimal = by_name(simulate(run_of(call0, call1)))["optimal-cache"]
    assert optimal.tokens["cache_read"] == 0
    assert optimal.tokens["cache_write"] == 0  # neither write is ever read
    assert optimal.tokens["uncached"] == 4_000


def test_same_model_entries_still_match_across_model_interleaving() -> None:
    # Sanity for the per-model keying: an interleaved other-model call must
    # not prevent the original model from hitting its own entry.
    shared = history(3)
    call0 = make_call(0, messages=shared, usage=Usage(2_000, 0, 0, 10))
    call1 = make_call(
        1, model="claude-opus-4-8", messages=shared, usage=Usage(2_000, 0, 0, 10)
    )
    call2 = make_call(2, messages=shared, usage=Usage(2_000, 0, 0, 10))
    optimal = by_name(simulate(run_of(call0, call1, call2)))["optimal-cache"]
    # call 2 reads call 0's same-model entry in full.
    assert optimal.tokens["cache_read"] == 2_000
    assert optimal.tokens["cache_write"] == 2_000  # call 0's productive write
