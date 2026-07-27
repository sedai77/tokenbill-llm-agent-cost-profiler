"""analyzer.py: waterfalls, segment attribution, and the redundancy formula.

Expected redundancy values are derived in-test from trace.py's own rendering
primitives (common_prefix_chars / rendered_text), so the assertions check the
analyzer's arithmetic — not a hardcoded guess about canonical rendering.
"""

from __future__ import annotations

import pytest

trace = pytest.importorskip("tokenbill.trace", reason="Module A's trace.py not present yet")
analyzer = pytest.importorskip("tokenbill.analyzer")

from tokenbill.pricing import cost_breakdown  # noqa: E402

SYSTEM = "You are a careful coding agent. Read before you write; keep diffs minimal."
TOOLS = (
    {
        "name": "read_file",
        "description": "Read a file from the workspace.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "run_tests",
        "description": "Run the test suite.",
        "input_schema": {"type": "object"},
    },
)
BASE_MESSAGES = ({"role": "user", "content": "Fix the failing test in tests/test_io.py."},)
EXTRA_MESSAGES = (
    {"role": "assistant", "content": "Reading the test file first."},
    {"role": "user", "content": "Go ahead."},
)


def usage(
    input_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    output: int = 0,
) -> object:
    return trace.Usage(
        input_tokens=input_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        output_tokens=output,
    )


def make_call(
    index: int,
    usage_obj: object,
    *,
    messages: tuple = BASE_MESSAGES,
    model: str = "claude-sonnet-5",
    run_id: str = "run-a",
    cache_breakpoints: int = 1,
) -> object:
    return trace.Call(
        run_id=run_id,
        index=index,
        ts=1_700_000_000.0 + 10.0 * index,
        model=model,
        system=SYSTEM,
        tools=TOOLS,
        messages=messages,
        cache_breakpoints=cache_breakpoints,
        usage=usage_obj,
        stop_reason="end_turn",
    )


def make_run(*calls: object, run_id: str = "run-a") -> object:
    return trace.Run(run_id=run_id, calls=tuple(calls))


def test_first_call_has_zero_repeated_prefix() -> None:
    run = make_run(make_call(0, usage(input_tokens=1000, output=50)))
    profile = analyzer.profile_run(run)
    assert profile.calls[0].repeated_prefix_chars == 0
    assert profile.calls[0].repeated_fraction_of_input == 0.0
    assert profile.totals.redundancy_fraction == 0.0


def test_identical_calls_no_cache_is_half_redundant() -> None:
    c0 = make_call(0, usage(input_tokens=1000, output=50))
    c1 = make_call(1, usage(input_tokens=1000, output=50))
    profile = analyzer.profile_run(make_run(c0, c1))

    rendered_len = len(trace.rendered_text(c1))
    assert profile.calls[1].repeated_prefix_chars == rendered_len
    assert profile.calls[1].repeated_fraction_of_input == pytest.approx(1.0)
    # Call 1 re-sends its entire input uncached: wasted 1000 of 2000 billed.
    assert profile.totals.redundancy_fraction == pytest.approx(0.5)


def test_extension_redundancy_matches_docstring_formula() -> None:
    u0 = usage(input_tokens=900, output=40)
    u1 = usage(input_tokens=700, cache_read=300, output=60)
    c0 = make_call(0, u0)
    c1 = make_call(1, u1, messages=BASE_MESSAGES + EXTRA_MESSAGES)
    profile = analyzer.profile_run(make_run(c0, c1))

    lcp = trace.common_prefix_chars(c0, c1)
    fraction = lcp / len(trace.rendered_text(c1))
    total_input_1 = 700 + 300  # billed total_input of call 1
    wasted = max(0.0, total_input_1 * fraction - 300)  # reads subtracted
    expected = wasted / (900 + total_input_1)

    assert 0 < lcp < len(trace.rendered_text(c1))
    assert profile.calls[1].repeated_prefix_chars == lcp
    assert profile.calls[1].repeated_fraction_of_input == pytest.approx(fraction)
    assert profile.totals.redundancy_fraction == pytest.approx(expected)


def test_cache_reads_clamp_contribution_at_zero() -> None:
    # Call 1 extends call 0, and its entire input is served from cache: the
    # repeated-prefix value (< total_input) minus reads goes negative and must
    # clamp to 0 — cache reads are cheap, not waste.
    c0 = make_call(0, usage(input_tokens=1000, output=40))
    c1 = make_call(
        1,
        usage(input_tokens=0, cache_read=1500, output=40),
        messages=BASE_MESSAGES + EXTRA_MESSAGES,
    )
    profile = analyzer.profile_run(make_run(c0, c1))
    assert profile.totals.redundancy_fraction == 0.0


def test_segment_shares_sum_to_billed_input() -> None:
    call = make_call(0, usage(input_tokens=800, cache_write=200, output=30))
    profile = analyzer.profile_run(make_run(call))
    shares = profile.calls[0].segments

    assert shares[0].kind == "tools"
    assert shares[1].kind == "system"
    assert [s.kind for s in shares[2:]] == ["message"] * len(call.messages)
    assert sum(s.approx_tokens for s in shares) == pytest.approx(call.usage.total_input)
    assert sum(s.char_fraction for s in shares) == pytest.approx(1.0)
    for share in shares:
        assert 0.0 <= share.char_fraction <= 1.0
        assert share.chars >= 0


def test_totals_tokens_are_exact_sums() -> None:
    c0 = make_call(0, usage(input_tokens=1000, cache_write=200, output=50))
    c1 = make_call(1, usage(input_tokens=100, cache_read=1100, cache_write=30, output=60))
    profile = analyzer.profile_run(make_run(c0, c1))
    tokens = profile.totals.tokens
    assert tokens["uncached"] == 1100
    assert tokens["cache_write"] == 230
    assert tokens["cache_read"] == 1100
    assert tokens["output"] == 110
    assert tokens["total_input"] == 2430


def test_totals_dollars_sum_call_breakdowns() -> None:
    u0 = usage(input_tokens=1000, cache_write=200, output=50)
    u1 = usage(input_tokens=100, cache_read=1100, output=60)
    profile = analyzer.profile_run(make_run(make_call(0, u0), make_call(1, u1)))
    dollars = profile.totals.dollars
    assert dollars is not None
    assert set(dollars) == {"uncached", "write", "read", "output", "total"}
    b0 = cost_breakdown("claude-sonnet-5", u0)
    b1 = cost_breakdown("claude-sonnet-5", u1)
    assert b0 is not None and b1 is not None
    for key in ("uncached", "write", "read", "output"):
        assert dollars[key] == pytest.approx(b0[key] + b1[key])
    assert dollars["total"] == pytest.approx(
        sum(dollars[k] for k in ("uncached", "write", "read", "output"))
    )


def test_unknown_model_yields_none_dollars_but_exact_tokens() -> None:
    known = make_call(0, usage(input_tokens=500, output=10))
    unknown = make_call(1, usage(input_tokens=500, output=10), model="self-hosted-llama")
    profile = analyzer.profile_run(make_run(known, unknown))
    assert profile.calls[0].dollars is not None
    assert profile.calls[1].dollars is None
    assert profile.totals.dollars is None  # partial dollar totals would mislead
    assert profile.totals.tokens["total_input"] == 1000
    assert 0.0 <= profile.totals.redundancy_fraction <= 1.0


def test_redundancy_is_labeled_approximate_and_bounded() -> None:
    calls = [make_call(i, usage(input_tokens=400, output=20)) for i in range(4)]
    profile = analyzer.profile_run(make_run(*calls))
    assert profile.totals.redundancy_is_approx is True
    assert 0.0 <= profile.totals.redundancy_fraction <= 1.0


def test_profile_trace_preserves_run_order() -> None:
    run_a = make_run(make_call(0, usage(input_tokens=100, output=5)))
    run_b = make_run(
        make_call(0, usage(input_tokens=200, output=5), run_id="run-b"), run_id="run-b"
    )
    profiles = analyzer.profile_trace([run_a, run_b])
    assert [p.run.run_id for p in profiles] == ["run-a", "run-b"]
    assert all(isinstance(p, analyzer.RunProfile) for p in profiles)
