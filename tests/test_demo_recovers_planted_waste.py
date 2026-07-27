"""Flagship test: the analyzer recovers every waste pattern planted in the demo.

Expectations are DERIVED from demo_traces' documented usage derivation (see
that module's docstring), not copied from analyzer output:

    tok(s)  = int(len(s) / 3.7)                       (floor)
    total_i = tok(R_i)     R_i = canonical rendering (tools -> system -> messages)
    well-behaved:  reads_i = total_{i-1}, writes_i = total_i - reads_i, uncached 0
    timestamp / tool-churn / no-cache:  uncached_i = total_i, reads = writes = 0

The rendering is reconstructed here independently with ``common.canonical_json``
per the SPEC's documented segment order — not via ``trace.render_segments`` —
so these tests cross-check the byte-comparison substrate instead of assuming it.
Pricing constants below are claude-sonnet-5 rows from the verified pricing table.
"""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
import sys

import pytest

from tokenbill import analyzer, breakers, simulator
from tokenbill.breakers import STABLE_PLACEHOLDER
from tokenbill.common import canonical_json
from tokenbill.demo_traces import all_scenarios, scenario
from tokenbill.trace import Call, Run

SEED = 7
CHARS_PER_TOKEN = 3.7
IN_RATE = 3.00  # claude-sonnet-5 $/MTok input
OUT_RATE = 15.00  # claude-sonnet-5 $/MTok output
READ_MULT = 0.10
WRITE_MULT = 1.25
MIN_CACHEABLE = 1_024  # claude-sonnet-5 minimum cacheable prefix, approx tokens
TTL = 300.0
MTOK = 1_000_000


def rendered(call: Call) -> str:
    """Independent reconstruction of the canonical rendering (SPEC order)."""
    parts = [canonical_json(list(call.tools)), call.system]
    parts.extend(canonical_json(message) for message in call.messages)
    return "".join(parts)


def lcp_chars(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def scenario_run(name: str) -> Run:
    calls = scenario(name, seed=SEED)
    return Run(run_id=calls[0].run_id, calls=tuple(calls))


def expected_redundancy(calls: list[Call], texts: list[str]) -> float:
    """The SPEC redundancy formula, re-derived: for call i>0 the re-sent prefix's
    token value is billed total_input scaled by the LCP char fraction, minus the
    part already served from cache, clamped at 0; summed and divided by billed
    input."""
    wasted = 0.0
    for i in range(1, len(calls)):
        fraction = lcp_chars(texts[i - 1], texts[i]) / len(texts[i])
        wasted += max(
            0.0,
            calls[i].usage.total_input * fraction - calls[i].usage.cache_read_input_tokens,
        )
    return wasted / sum(call.usage.total_input for call in calls)


def stable_replay_usd(calls: list[Call], texts: list[str]) -> float:
    """Straight-line re-derivation of the optimal/fixed replay price for a
    byte-stable append-only run, under the replay's retrospective write
    accounting (a write premium is only charged when the written entry is
    read by a later call — never for the final call, whose entry nothing can
    ever read):

        call 0:       everything written:  total_0 tokens at IN_RATE x 1.25
                      (productive: call 1 reads it)
        call 0<i<n-1: prefix (= call i-1's whole rendering) read:
                          read_i  = round(total_i x len_{i-1} / len_i)  at IN_RATE x 0.10
                      extension written (productive: call i+1 reads it):
                          write_i = total_i - read_i                    at IN_RATE x 1.25
        call n-1:     prefix read as above; the extension is NEVER read, so
                      it is billed plain uncached at IN_RATE x 1.0
        outputs at OUT_RATE throughout.

    Guards assert the demo-documented preconditions that make this closed form
    valid: append-only bytes, first call over the min-cacheable gate, ts gaps
    inside the TTL.
    """
    for a, b in zip(texts, texts[1:], strict=False):
        assert b.startswith(a), "closed form only valid for append-only byte-stable runs"
    assert len(texts[0]) / CHARS_PER_TOKEN >= MIN_CACHEABLE
    for a, b in zip(calls, calls[1:], strict=False):
        assert 0 < b.ts - a.ts <= TTL
    usd = 0.0
    last = len(calls) - 1
    for i, call in enumerate(calls):
        matched = len(texts[i - 1]) if i else 0
        read = round(call.usage.total_input * matched / len(texts[i]))
        extension = call.usage.total_input - read
        extension_rate = IN_RATE if i == last else IN_RATE * WRITE_MULT
        usd += (
            extension * extension_rate
            + read * IN_RATE * READ_MULT
            + call.usage.output_tokens * OUT_RATE
        ) / MTOK
    return usd


def assert_billed_all_uncached(calls: list[Call], texts: list[str]) -> None:
    """The documented derivation for the three broken scenarios, checked exactly."""
    for call, text in zip(calls, texts, strict=False):
        assert call.usage.input_tokens == int(len(text) / CHARS_PER_TOKEN)
        assert call.usage.cache_read_input_tokens == 0
        assert call.usage.cache_creation_input_tokens == 0


# ---------------------------------------------------------------------------
# well-behaved: the control — zero breakers, simulator agrees with the bill.
# ---------------------------------------------------------------------------

def test_well_behaved_zero_breakers_and_simulator_agreement() -> None:
    run = scenario_run("well-behaved")
    calls = list(run.calls)
    texts = [rendered(call) for call in calls]
    profile = analyzer.profile_run(run)
    found = breakers.detect(run)
    results = {r.name: r for r in simulator.simulate(run)}

    # Planted truth: nothing to fix.
    assert found == []

    # Billed usage follows the documented cached derivation exactly:
    # reads_i = total_{i-1} (P_i = len(R_{i-1}) because the run is append-only).
    for i in range(1, len(calls)):
        assert texts[i].startswith(texts[i - 1])
        assert calls[i].usage.cache_read_input_tokens == calls[i - 1].usage.total_input
        assert calls[i].usage.input_tokens == 0

    # Redundancy ~ 0 because cache reads are subtracted. Per call i>0 the
    # repeated-prefix token value is total_i x P_i / L_i <= P_i / 3.7, and the
    # billed reads are int(P_i / 3.7) — so each call contributes < 1 token of
    # "waste"; 13 pairs over a run of tens of thousands of billed input tokens
    # is far below 1%.
    assert profile.totals.redundancy_fraction == pytest.approx(
        expected_redundancy(calls, texts), rel=1e-9
    )
    assert 0.0 <= profile.totals.redundancy_fraction < 0.01

    # Simulator validation (the honesty check the README cites): billed reads
    # were constructed as int(P_i / 3.7) and the replay predicts
    # round(total_i x P_i / L_i); both approximate P_i / 3.7 within 1 token,
    # so they agree within 2 tokens per pair — never looser.
    billed_reads = results["as-billed"].tokens["cache_read"]
    simulated_reads = results["optimal-cache"].tokens["cache_read"]
    n_pairs = len(calls) - 1
    assert billed_reads > 10_000  # the tolerance below is not vacuous
    assert abs(billed_reads - simulated_reads) <= 2 * n_pairs
    assert "validation: billed cache reads" in results["optimal-cache"].note

    # Dollar agreement with one derived, deliberate gap: the real bill pays
    # the write premium on the FINAL call's extension (the provider cannot
    # know the run is over), while the replay's retrospective accounting
    # bills that never-read extension plain uncached. So billed - optimal ==
    # 0.25x the final call's billed write, up to the <= 2 tokens/pair
    # rounding repriced across the widest rate gap (1.25x vs 0.10x).
    billed_usd = results["as-billed"].dollars
    optimal_usd = results["optimal-cache"].dollars
    assert billed_usd is not None and optimal_usd is not None
    final_write_gap = (
        calls[-1].usage.cache_creation_input_tokens * (WRITE_MULT - 1.0) * IN_RATE / MTOK
    )
    rounding = 2 * n_pairs * (WRITE_MULT - READ_MULT) * IN_RATE / MTOK
    assert abs((billed_usd - optimal_usd) - final_write_gap) <= rounding

    # No repairs -> fixed-cache degenerates to optimal-cache.
    assert results["fixed-cache"].tokens == results["optimal-cache"].tokens


# ---------------------------------------------------------------------------
# timestamp: volatile system prompt — the planted "volatile-system" waste.
# ---------------------------------------------------------------------------

def test_timestamp_recovers_planted_volatile_system() -> None:
    run = scenario_run("timestamp")
    calls = list(run.calls)
    texts = [rendered(call) for call in calls]
    profile = analyzer.profile_run(run)
    found = breakers.detect(run)

    # Planted truth: exactly one volatile-system breaker, first biting at call
    # 1, with the "[session 2026-07-26 14:03:SS]" stamp visible in the evidence.
    assert [b.kind for b in found] == ["volatile-system"]
    breaker = found[0]
    assert breaker.first_call_index == 1
    assert "14:03" in breaker.evidence
    assert "system" in breaker.evidence
    assert "user message" in breaker.fix

    # The documented billing for this scenario: everything uncached.
    assert_billed_all_uncached(calls, texts)

    # Redundancy band, derived: the LCP of consecutive calls is the ~2.4k-char
    # tools segment plus the stable head of the system prompt (up to the
    # session stamp), over renderings that grow from ~7k to ~25k chars — a
    # material but partial fraction. The analyzer must match the re-derived
    # value exactly and sit inside the structural band.
    expected = expected_redundancy(calls, texts)
    tools_chars = len(canonical_json(list(calls[0].tools)))
    total_billed = sum(call.usage.total_input for call in calls)
    floor = (
        sum(
            calls[i].usage.total_input * tools_chars / len(texts[i])
            for i in range(1, len(calls))
        )
        / total_billed
    )
    assert profile.totals.redundancy_fraction == pytest.approx(expected, rel=1e-9)
    assert expected >= floor > 0.03  # LCP always covers at least the tools segment
    assert expected < 0.60  # ...but never the growing history tail

    # Repair and re-simulate: the placeholder makes the run byte-stable, so the
    # fixed-cache price obeys the closed form in stable_replay_usd.
    repaired = breakers.repaired_calls(run, found)
    repaired_texts = [rendered(call) for call in repaired]
    assert len({call.system for call in repaired}) == 1
    assert STABLE_PLACEHOLDER in repaired[0].system
    expected_fixed = stable_replay_usd(repaired, repaired_texts)

    results = {r.name: r for r in simulator.simulate(run, repaired)}
    billed_usd = results["as-billed"].dollars
    fixed_usd = results["fixed-cache"].dollars
    assert billed_usd is not None and fixed_usd is not None
    # As billed: every input token uncached at $3/MTok plus outputs at $15/MTok.
    expected_billed = sum(
        (call.usage.total_input * IN_RATE + call.usage.output_tokens * OUT_RATE) / MTOK
        for call in calls
    )
    assert billed_usd == pytest.approx(expected_billed, abs=1e-9)
    assert fixed_usd == pytest.approx(expected_fixed, abs=1e-9)
    assert fixed_usd < billed_usd

    # The breaker's price tag isolates exactly this cause.
    assert breaker.est_recovered_usd == pytest.approx(billed_usd - expected_fixed, abs=1e-9)
    # Consistent with construction: most input is re-sent prefix, so reads at
    # 0.10x (minus the 0.25x write premium on extensions) must recover a large
    # share of the bill.
    assert breaker.est_recovered_usd > 0.25 * billed_usd


# ---------------------------------------------------------------------------
# tool-churn: rotating tool order — the planted "tool-churn" waste.
# ---------------------------------------------------------------------------

def test_tool_churn_recovers_planted_rotation() -> None:
    run = scenario_run("tool-churn")
    calls = list(run.calls)
    texts = [rendered(call) for call in calls]
    analyzer.profile_run(run)  # profiling must run cleanly on this scenario too
    found = breakers.detect(run)

    # Rotation calls derived from the data, not assumed.
    rotations = [
        i
        for i in range(1, len(calls))
        if [t["name"] for t in calls[i].tools] != [t["name"] for t in calls[i - 1].tools]
    ]
    assert rotations, "the demo must actually rotate tools"

    # Planted truth: tool-churn at the first rotation call, and no
    # volatile-system false positive (SPEC).
    assert [b.kind for b in found] == ["tool-churn"]
    breaker = found[0]
    assert breaker.first_call_index == rotations[0]
    assert "read_file" in breaker.evidence  # first-seen vs current order shown
    assert_billed_all_uncached(calls, texts)

    # Restoring first-seen order makes the run byte-stable, so the fixed-cache
    # price obeys the closed form and recovers real dollars.
    repaired = breakers.repaired_calls(run, found)
    repaired_texts = [rendered(call) for call in repaired]
    expected_fixed = stable_replay_usd(repaired, repaired_texts)
    results = {r.name: r for r in simulator.simulate(run, repaired)}
    assert results["fixed-cache"].dollars == pytest.approx(expected_fixed, abs=1e-9)
    billed_usd = results["as-billed"].dollars
    assert billed_usd is not None
    assert breaker.est_recovered_usd == pytest.approx(billed_usd - expected_fixed, abs=1e-9)
    assert breaker.est_recovered_usd > 0.25 * billed_usd


# ---------------------------------------------------------------------------
# no-cache: stable prefix, no breakpoint — the planted one-line fix.
# ---------------------------------------------------------------------------

def test_no_cache_recovers_planted_missing_breakpoint() -> None:
    run = scenario_run("no-cache")
    calls = list(run.calls)
    texts = [rendered(call) for call in calls]
    profile = analyzer.profile_run(run)
    found = breakers.detect(run)

    # Planted truth: a missing-breakpoint breaker (and nothing else — the run
    # is byte-stable).
    assert [b.kind for b in found] == ["missing-breakpoint"]
    breaker = found[0]
    assert breaker.first_call_index == 1
    assert "cache_breakpoints=0" in breaker.evidence
    assert_billed_all_uncached(calls, texts)

    # Redundancy is the headline here: nearly everything after call 0 is
    # re-sent bytes with zero cache reads to subtract. Derived value must
    # match, and be large — the prefix is the whole previous rendering, so the
    # fraction approaches sum(total_{i-1}) / sum(total_i), well over half for
    # a growing 14-call history.
    expected = expected_redundancy(calls, texts)
    assert profile.totals.redundancy_fraction == pytest.approx(expected, rel=1e-9)
    assert expected > 0.5

    repaired = breakers.repaired_calls(run, found)
    assert all(call.cache_breakpoints == 1 for call in repaired)
    assert [rendered(call) for call in repaired] == texts  # rendering untouched

    results = {r.name: r for r in simulator.simulate(run, repaired)}
    as_billed = results["as-billed"]
    no_cache = results["no-cache"]
    optimal = results["optimal-cache"]
    fixed = results["fixed-cache"]
    assert as_billed.dollars is not None

    # SPEC: fixed-cache ~ optimal-cache < as-billed ~ no-cache.
    # as-billed == no-cache because everything was already billed uncached.
    assert no_cache.tokens == as_billed.tokens
    assert no_cache.dollars == pytest.approx(as_billed.dollars, abs=1e-9)
    # The repair only flips the breakpoint flag, which the optimal replay
    # already assumes, so fixed and optimal are the same replay.
    assert fixed.tokens == optimal.tokens
    assert fixed.dollars == pytest.approx(optimal.dollars, abs=1e-12)
    # And the replay obeys the closed form: one line of config buys the whole
    # optimal-cache price.
    expected_optimal = stable_replay_usd(calls, texts)
    assert optimal.dollars == pytest.approx(expected_optimal, abs=1e-9)
    assert optimal.dollars < as_billed.dollars
    assert breaker.est_recovered_usd == pytest.approx(
        as_billed.dollars - expected_optimal, abs=1e-9
    )
    assert breaker.est_recovered_usd > 0.25 * as_billed.dollars


# ---------------------------------------------------------------------------
# Determinism: identical bytes across independent processes.
# ---------------------------------------------------------------------------

def _digest_all_scenarios(seed: int) -> str:
    digest = hashlib.sha256()
    scenarios = all_scenarios(seed)
    for name in sorted(scenarios):
        for call in scenarios[name]:
            digest.update(canonical_json(dataclasses.asdict(call)).encode())
    return digest.hexdigest()


def test_demo_traces_are_byte_deterministic_across_processes() -> None:
    # A fresh interpreter has a different string-hash seed; identical digests
    # prove the demo generator never leans on process-dependent ordering.
    script = (
        "import dataclasses, hashlib\n"
        "from tokenbill.common import canonical_json\n"
        "from tokenbill.demo_traces import all_scenarios\n"
        f"scenarios = all_scenarios({SEED})\n"
        "digest = hashlib.sha256()\n"
        "for name in sorted(scenarios):\n"
        "    for call in scenarios[name]:\n"
        "        digest.update(canonical_json(dataclasses.asdict(call)).encode())\n"
        "print(digest.hexdigest())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == _digest_all_scenarios(SEED)
