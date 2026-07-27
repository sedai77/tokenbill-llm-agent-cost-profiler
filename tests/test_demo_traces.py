"""Tests for tokenbill.demo_traces: determinism, realism bounds, planted waste.

The usage assertions re-derive every number from the documented scheme in the
demo_traces module docstring (tok = floor(chars / 3.7) over the canonical
rendering), so a drift between the generator and its contract fails here
before it can confuse the flagship analyzer tests.
"""

from __future__ import annotations

import pytest

from tokenbill.common import TokenbillError, canonical_json
from tokenbill.demo_traces import SCENARIOS, ScenarioSpec, all_scenarios, scenario
from tokenbill.trace import (
    CHARS_PER_TOKEN,
    common_prefix_chars,
    diverging_segment,
    read_trace,
    rendered_text,
    write_trace,
)

ALL_NAMES = ("well-behaved", "timestamp", "tool-churn", "no-cache")


def tok(text: str) -> int:
    """The documented rounding: floor of chars / 3.7."""
    return int(len(text) / CHARS_PER_TOKEN)


# -- registry and API -------------------------------------------------------


def test_registry_has_exactly_the_four_scenarios() -> None:
    assert set(SCENARIOS) == set(ALL_NAMES)
    for name, spec in SCENARIOS.items():
        assert spec == ScenarioSpec(name=name, description=spec.description)
        assert spec.seed == 7
        assert spec.n_calls == 14
        assert spec.model == "claude-sonnet-5"
        assert spec.description


def test_all_scenarios_matches_scenario_by_name() -> None:
    everything = all_scenarios(seed=7)
    assert set(everything) == set(ALL_NAMES)
    for name, calls in everything.items():
        assert calls == scenario(name, seed=7)


def test_unknown_scenario_name_lists_known_names() -> None:
    with pytest.raises(TokenbillError, match=r"unknown demo scenario 'nope'") as excinfo:
        scenario("nope")
    for name in ALL_NAMES:
        assert name in str(excinfo.value)


# -- determinism ------------------------------------------------------------


def test_deterministic_per_seed_and_seed_sensitive() -> None:
    for name in ALL_NAMES:
        assert scenario(name, seed=7) == scenario(name, seed=7)
    assert scenario("well-behaved", seed=7) != scenario("well-behaved", seed=11)


# -- shape and realism bounds -----------------------------------------------


@pytest.mark.parametrize("name", ALL_NAMES)
def test_agent_loop_shape(name: str) -> None:
    calls = scenario(name)
    assert len(calls) == 14
    for i, call in enumerate(calls):
        assert call.run_id == f"demo-{name}-seed7"
        assert call.index == i
        assert call.model == "claude-sonnet-5"
        assert len(call.tools) == 4
        assert len(call.messages) == 1 + 2 * i  # task + (assistant, user) per turn
        assert call.messages[0]["role"] == "user"
        assert call.stop_reason in ("tool_use", "end_turn")
    assert calls[-1].stop_reason == "end_turn"  # run ends on a text summary
    # timestamps: strictly increasing, 15-45 s apart, well inside the 5-min TTL
    gaps = [b.ts - a.ts for a, b in zip(calls, calls[1:], strict=False)]
    assert all(15.0 <= gap <= 45.0 for gap in gaps)


def test_content_size_targets() -> None:
    calls = scenario("well-behaved")
    assert 3000 <= len(calls[0].system) <= 3400  # ~3,200 chars per the spec
    assert 2200 <= len(canonical_json(list(calls[0].tools))) <= 2700  # ~2,400 chars
    # tool results are 300-2,500 chars; the last call's history holds them all
    result_lengths = [
        len(block["content"])
        for message in calls[-1].messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert result_lengths, "expected tool_result turns in the demo history"
    assert all(300 <= n <= 2500 for n in result_lengths)
    # the first call alone clears claude-sonnet-5's 1,024-token minimum
    # cacheable prefix, so cache legality never bites in the demo
    assert calls[0].usage.total_input >= 1024


# -- the documented usage derivation ----------------------------------------


@pytest.mark.parametrize("name", ALL_NAMES)
def test_usage_total_matches_rendering_for_every_call(name: str) -> None:
    for call in scenario(name):
        assert call.usage.total_input == tok(rendered_text(call))


@pytest.mark.parametrize("name", ALL_NAMES)
def test_output_tokens_match_next_calls_assistant_reply(name: str) -> None:
    calls = scenario(name)
    for i in range(len(calls) - 1):
        reply = calls[i + 1].messages[1 + 2 * i]
        assert reply["role"] == "assistant"
        assert calls[i].usage.output_tokens == tok(canonical_json(reply))
    assert calls[-1].usage.output_tokens > 0


# -- planted waste patterns -------------------------------------------------


def test_well_behaved_is_the_cached_control() -> None:
    calls = scenario("well-behaved")
    for prev, cur in zip(calls, calls[1:], strict=False):
        assert diverging_segment(prev, cur) is None  # byte-stable, append-only
        assert common_prefix_chars(prev, cur) == len(rendered_text(prev))
    reads = [c.usage.cache_read_input_tokens for c in calls]
    assert calls[0].usage.cache_read_input_tokens == 0
    assert reads[1:] == sorted(reads[1:]) and reads[1] > 0  # reads grow
    for i, call in enumerate(calls):
        assert call.cache_breakpoints == 1
        assert call.usage.input_tokens == 0
        expected_reads = calls[i - 1].usage.total_input if i else 0
        assert call.usage.cache_read_input_tokens == expected_reads
        assert (
            call.usage.cache_creation_input_tokens
            == call.usage.total_input - expected_reads
        )


def test_timestamp_plants_a_volatile_system_prompt() -> None:
    calls = scenario("timestamp")
    for i, call in enumerate(calls):
        assert f"[session 2026-07-26 14:03:{i:02d}]" in call.system
        assert call.cache_breakpoints == 1
        assert call.usage.cache_read_input_tokens == 0
        assert call.usage.cache_creation_input_tokens == 0
        assert call.usage.input_tokens == call.usage.total_input
    for prev, cur in zip(calls, calls[1:], strict=False):
        assert diverging_segment(prev, cur) == (1, "system")
        # the divergence really is inside the system segment: past tools,
        # before the first message
        tools_len = len(canonical_json(list(cur.tools)))
        prefix = common_prefix_chars(prev, cur)
        assert tools_len < prefix < tools_len + len(cur.system)


def test_tool_churn_rotates_every_fourth_call() -> None:
    calls = scenario("tool-churn")
    for prev, cur in zip(calls, calls[1:], strict=False):
        expected = (0, "tools") if cur.index in (4, 8, 12) else None
        assert diverging_segment(prev, cur) == expected
    names_first = [t["name"] for t in calls[0].tools]
    names_at_4 = [t["name"] for t in calls[4].tools]
    assert sorted(names_first) == sorted(names_at_4)  # same tools ...
    assert names_first != names_at_4  # ... different order
    for call in calls:
        assert call.cache_breakpoints == 1
        assert call.usage.cache_read_input_tokens == 0
        assert call.usage.input_tokens == call.usage.total_input


def test_no_cache_is_byte_identical_to_well_behaved_but_uncached() -> None:
    cached = scenario("well-behaved")
    uncached = scenario("no-cache")
    for good, bad in zip(cached, uncached, strict=True):
        assert rendered_text(good) == rendered_text(bad)  # identical content
        assert bad.cache_breakpoints == 0
        assert bad.usage.cache_read_input_tokens == 0
        assert bad.usage.cache_creation_input_tokens == 0
        assert bad.usage.input_tokens == bad.usage.total_input
        assert bad.usage.total_input == good.usage.total_input
    for prev, cur in zip(uncached, uncached[1:], strict=False):
        assert diverging_segment(prev, cur) is None


# -- integration with trace IO ----------------------------------------------


def test_demo_scenarios_round_trip_through_jsonl(tmp_path) -> None:
    for name in ALL_NAMES:
        calls = scenario(name)
        path = tmp_path / f"{name}.jsonl"
        write_trace(path, calls)
        (run,) = read_trace(path)
        assert run.run_id == f"demo-{name}-seed7"
        assert list(run.calls) == calls
