"""Unit tests for cache-breaker detection, classification, and repair."""

from __future__ import annotations

import pytest

from tokenbill.breakers import (
    STABLE_PLACEHOLDER,
    VOLATILE_PATTERNS,
    detect,
    repaired_calls,
)
from tokenbill.trace import Call, Run, Usage

MODEL = "claude-sonnet-5"  # min cacheable prefix: 1,024 approx tokens

# ~5,180 chars -> ~1,400 approx tokens, over the min-cacheable gate on its own.
BIG_SYSTEM = "You are a meticulous release engineer for the walcache project. " * 80

TOOL_A = {"name": "read_file", "description": "Read a file from the repository. " * 8}
TOOL_B = {"name": "run_tests", "description": "Run the pytest suite quietly. " * 8}


def history(n: int) -> tuple[dict, ...]:
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
    tools: tuple[dict, ...] = (TOOL_A, TOOL_B),
    messages: tuple[dict, ...] | None = None,
    breakpoints: int = 1,
    usage: Usage | None = None,
) -> Call:
    if messages is None:
        messages = history(index + 1)
    if usage is None:
        usage = Usage(
            input_tokens=1_500 + 100 * index,
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


# ---------------------------------------------------------------------------
# The volatile-regex list is a tested module constant (SPEC).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "2026-07-26",  # ISO date
        "14:03:05",  # clock time with seconds
        "09:15",  # clock time without seconds
        "2026-07-26T14:03:05Z",  # ISO datetime, T separator
        "2026-07-26 14:03:05",  # ISO datetime, space separator
        "1721990000",  # unix timestamp (seconds)
        "123e4567-e89b-12d3-a456-426614174000",  # UUID
        "attempt 3",  # monotonic counters
        "seq=42",
        "turn #7",
    ],
)
def test_volatile_patterns_match(text: str) -> None:
    assert any(pattern.search(text) for pattern in VOLATILE_PATTERNS)


@pytest.mark.parametrize(
    "text",
    [
        "version 2.0 of the walcache library",
        "pi is 3.14159",
        "the answer is 42",
        "read the file src/walcache/store.py",
        "ttl=0.1 keeps entries alive briefly",
        "a 12345678 digit account number",  # too short for a unix timestamp
    ],
)
def test_volatile_patterns_ignore_stable_text(text: str) -> None:
    assert not any(pattern.search(text) for pattern in VOLATILE_PATTERNS)


# ---------------------------------------------------------------------------
# Classification, one rule at a time.
# ---------------------------------------------------------------------------

def test_well_behaved_run_has_no_breakers() -> None:
    run = run_of(make_call(0), make_call(1), make_call(2))
    assert detect(run) == []


def test_single_call_run_has_no_breakers() -> None:
    assert detect(run_of(make_call(0))) == []


def test_model_switch_wins_over_other_divergence() -> None:
    # The pair both switches model and reorders tools: rule 1 must win.
    run = run_of(
        make_call(0),
        make_call(1, model="claude-haiku-4-5", tools=(TOOL_B, TOOL_A)),
    )
    found = detect(run)
    assert [b.kind for b in found] == ["model-switch"]
    assert found[0].first_call_index == 1
    assert "claude-haiku-4-5" in found[0].evidence


def test_tool_churn_detected_with_order_evidence() -> None:
    run = run_of(
        make_call(0),
        make_call(1),
        make_call(2, tools=(TOOL_B, TOOL_A)),
    )
    found = detect(run)
    assert [b.kind for b in found] == ["tool-churn"]
    breaker = found[0]
    assert breaker.first_call_index == 2
    assert "['read_file', 'run_tests']" in breaker.evidence  # first-seen order
    assert "['run_tests', 'read_file']" in breaker.evidence  # churned order
    assert "fixed order" in breaker.fix


def test_volatile_system_detected_with_span_evidence() -> None:
    calls = [
        make_call(i, system=f"{BIG_SYSTEM}\nSession started 2026-07-26 14:03:{i:02d}\n")
        for i in range(3)
    ]
    found = detect(run_of(*calls))
    assert [b.kind for b in found] == ["volatile-system"]
    breaker = found[0]
    assert breaker.first_call_index == 1
    assert "system chars [" in breaker.evidence  # char offsets present
    assert "14:03" in breaker.evidence  # the volatile stamp is shown
    assert "system prompt" in breaker.fix
    assert breaker.est_recovered_usd is not None
    assert breaker.est_recovered_usd > 0  # stabilizing the prefix recovers money


def test_system_edit_variant_for_non_volatile_change() -> None:
    run = run_of(
        make_call(0),
        make_call(1, system=BIG_SYSTEM + "\nBe extra terse in your replies.\n"),
    )
    found = detect(run)
    assert [b.kind for b in found] == ["system-edit"]
    assert "Be extra terse" in found[0].evidence
    assert "byte-stable" in found[0].fix


def test_edit_touching_a_stable_date_is_not_volatile() -> None:
    # Regression: the overlap test was closed-interval, so a volatile match
    # merely TOUCHING the changed span (a never-changing ISO date right
    # before an edited punctuation mark) misclassified a real system edit as
    # volatile-system — pointing the user at the wrong fix and repairing the
    # date while leaving the actual divergence in place.
    calls = [
        make_call(0, system=f"{BIG_SYSTEM}\nOffer valid on 2026-07-26. Be terse.\n"),
        make_call(1, system=f"{BIG_SYSTEM}\nOffer valid on 2026-07-26! Be terse.\n"),
        make_call(2, system=f"{BIG_SYSTEM}\nOffer valid on 2026-07-26! Be terse.\n"),
    ]
    found = detect(run_of(*calls))
    assert [b.kind for b in found] == ["system-edit"]
    # The system-edit repair pins the system text, so the repaired run is
    # byte-stable and the estimate prices the real fix.
    repaired = repaired_calls(run_of(*calls), found)
    assert len({call.system for call in repaired}) == 1


def test_mixed_volatile_and_real_edit_classifies_as_system_edit() -> None:
    # A change that strictly overlaps a volatile stamp but ALSO edits stable
    # text is not fully explained by volatility: stabilizing would leave the
    # run diverging, so the honest classification (and working fix) is
    # system-edit.
    calls = [
        make_call(0, system=f"{BIG_SYSTEM}\nnow 14:03:01 be verbose\n"),
        make_call(1, system=f"{BIG_SYSTEM}\nnow 14:03:02 be terse\n"),
    ]
    found = detect(run_of(*calls))
    assert [b.kind for b in found] == ["system-edit"]
    repaired = repaired_calls(run_of(*calls), found)
    assert len({call.system for call in repaired}) == 1


def test_history_rewrite_detected() -> None:
    rewritten = (
        {"role": "user", "content": "message 0: REWRITTEN summary of earlier context."},
        {"role": "user", "content": "message 1: please continue the code review."},
    )
    run = run_of(make_call(0), make_call(1, messages=rewritten))
    found = detect(run)
    assert [b.kind for b in found] == ["history-rewrite"]
    assert found[0].first_call_index == 1
    assert "messages[0]" in found[0].evidence
    assert "append" in found[0].fix


def test_missing_breakpoint_detected() -> None:
    run = run_of(make_call(0, breakpoints=0), make_call(1, breakpoints=0))
    found = detect(run)
    assert [b.kind for b in found] == ["missing-breakpoint"]
    breaker = found[0]
    assert breaker.first_call_index == 1
    assert "cache_breakpoints=0" in breaker.evidence
    assert "cache_control" in breaker.fix
    assert breaker.est_recovered_usd is not None
    assert breaker.est_recovered_usd > 0


def test_no_missing_breakpoint_when_breakpoint_present() -> None:
    assert detect(run_of(make_call(0), make_call(1))) == []


def test_no_missing_breakpoint_when_cache_activity_billed() -> None:
    cached = Usage(
        input_tokens=0, cache_read_input_tokens=1_500, cache_creation_input_tokens=100,
        output_tokens=50,
    )
    run = run_of(make_call(0, breakpoints=0), make_call(1, breakpoints=0, usage=cached))
    assert detect(run) == []


def test_no_missing_breakpoint_below_min_cacheable_prefix() -> None:
    # "Be terse." plus a couple of short messages is nowhere near 1,024 tokens.
    run = run_of(
        make_call(0, system="Be terse.", tools=(), breakpoints=0),
        make_call(1, system="Be terse.", tools=(), breakpoints=0),
    )
    assert detect(run) == []


def test_recurring_cause_collapses_to_first_occurrence() -> None:
    calls = [
        make_call(i, system=f"{BIG_SYSTEM}\nnow={1_784_037_780 + i}\n") for i in range(5)
    ]
    found = detect(run_of(*calls))  # unix timestamp changes on every call
    assert [b.kind for b in found] == ["volatile-system"]
    assert found[0].first_call_index == 1


def test_multiple_causes_reported_in_first_bite_order() -> None:
    calls = [
        make_call(0, system=f"{BIG_SYSTEM}\nstamp 14:03:00\n"),
        make_call(1, system=f"{BIG_SYSTEM}\nstamp 14:03:01\n"),
        make_call(2, system=f"{BIG_SYSTEM}\nstamp 14:03:02\n", tools=(TOOL_B, TOOL_A)),
    ]
    found = detect(run_of(*calls))
    assert [b.kind for b in found] == ["volatile-system", "tool-churn"]
    assert [b.first_call_index for b in found] == [1, 2]


def test_volatile_stamp_first_appearing_mid_run_is_a_system_edit() -> None:
    # A volatile-looking stamp ADDED mid-run is a system edit, not volatility:
    # stabilizing the stamp cannot make the run byte-stable (the first call
    # has no stamp line at all) — only pinning the system text can.
    calls = [
        make_call(0),
        make_call(1, system=f"{BIG_SYSTEM}\nstamp 14:03:01\n"),
        make_call(2, system=f"{BIG_SYSTEM}\nstamp 14:03:02\n"),
    ]
    found = detect(run_of(*calls))
    assert "system-edit" in [b.kind for b in found]
    assert found[0].kind == "system-edit"


def test_est_recovered_is_none_for_unknown_model() -> None:
    calls = [
        make_call(i, model="mystery-9", system=f"{BIG_SYSTEM}\nstamp 14:03:{i:02d}\n")
        for i in range(3)
    ]
    found = detect(run_of(*calls))
    assert [b.kind for b in found] == ["volatile-system"]
    assert found[0].est_recovered_usd is None


def test_est_recovered_is_none_for_model_switch() -> None:
    # Regression: model-switch has no mechanical repair, so repaired_calls
    # returns the run untouched and billed-minus-fixed priced the optimal
    # replay of the STILL-BROKEN run — a bogus "recovered" figure the
    # displayed fix could never deliver. The honest number is no number.
    run = run_of(make_call(0), make_call(1, model="claude-haiku-4-5"))
    found = detect(run)
    assert [b.kind for b in found] == ["model-switch"]
    assert found[0].est_recovered_usd is None


def test_est_recovered_is_none_for_history_rewrite() -> None:
    # Same reasoning as model-switch: rewriting content back would change
    # semantics, so there is no repair and no honest dollar estimate — the
    # old code could even print negative "recovers ~$-0.00xx" values here.
    rewritten = (
        {"role": "user", "content": "message 0: REWRITTEN summary of earlier context."},
        {"role": "user", "content": "message 1: please continue the code review."},
    )
    run = run_of(make_call(0), make_call(1, messages=rewritten))
    found = detect(run)
    assert [b.kind for b in found] == ["history-rewrite"]
    assert found[0].est_recovered_usd is None


# ---------------------------------------------------------------------------
# repaired_calls neutralizes exactly the named causes.
# ---------------------------------------------------------------------------

def test_repair_volatile_system_stabilizes_bytes() -> None:
    calls = [
        make_call(i, system=f"{BIG_SYSTEM}\nSession started 2026-07-26 14:03:{i:02d}\n")
        for i in range(3)
    ]
    run = run_of(*calls)
    repaired = repaired_calls(run, detect(run))
    assert len({call.system for call in repaired}) == 1  # byte-identical now
    assert STABLE_PLACEHOLDER in repaired[0].system
    # Nothing else is touched: usage, messages, tools, breakpoints survive.
    for before, after in zip(calls, repaired, strict=False):
        assert after.usage == before.usage
        assert after.messages == before.messages
        assert after.tools == before.tools
        assert after.cache_breakpoints == before.cache_breakpoints


def test_repair_tool_churn_restores_first_seen_order() -> None:
    run = run_of(
        make_call(0),
        make_call(1, tools=(TOOL_B, TOOL_A)),
        make_call(2, tools=(TOOL_B, TOOL_A)),
    )
    repaired = repaired_calls(run, detect(run))
    for call in repaired:
        assert call.tools == (TOOL_A, TOOL_B)


def test_repair_missing_breakpoint_sets_one() -> None:
    run = run_of(make_call(0, breakpoints=0), make_call(1, breakpoints=0))
    repaired = repaired_calls(run, detect(run))
    assert [call.cache_breakpoints for call in repaired] == [1, 1]


def test_repair_leaves_unrepairable_kinds_untouched() -> None:
    run = run_of(make_call(0), make_call(1, model="claude-haiku-4-5"))
    found = detect(run)
    assert [b.kind for b in found] == ["model-switch"]
    assert repaired_calls(run, found) == list(run.calls)


def test_repair_with_no_breakers_is_identity() -> None:
    run = run_of(make_call(0), make_call(1))
    assert repaired_calls(run, []) == list(run.calls)
