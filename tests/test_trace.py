"""Tests for tokenbill.trace: schema IO, canonical rendering, prefix math."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.common import TraceError, canonical_json
from tokenbill.trace import (
    CHARS_PER_TOKEN,
    SCHEMA,
    Call,
    Usage,
    approx_tokens,
    common_prefix_chars,
    diverging_segment,
    read_trace,
    render_segments,
    rendered_text,
    write_trace,
)

TOOLS = (
    {
        "name": "search_code",
        "description": "Search the repo.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
)
MESSAGES = (
    {"role": "user", "content": "Fix the failing TTL test."},
    {"role": "assistant", "content": [{"type": "text", "text": "Reading store.py first."}]},
)


def make_call(**overrides) -> Call:
    fields = {
        "run_id": "run-a",
        "index": 0,
        "ts": 1_784_000_000.0,
        "model": "claude-sonnet-5",
        "system": "You are a careful coding agent.",
        "tools": TOOLS,
        "messages": MESSAGES,
        "cache_breakpoints": 1,
        "usage": Usage(
            input_tokens=12,
            cache_read_input_tokens=340,
            cache_creation_input_tokens=56,
            output_tokens=78,
        ),
        "stop_reason": "end_turn",
    }
    fields.update(overrides)
    return Call(**fields)


def valid_record(tmp_path: Path) -> dict:
    """One known-good JSONL record, produced by write_trace itself."""
    path = tmp_path / "seed.jsonl"
    write_trace(path, [make_call()])
    return json.loads(path.read_text(encoding="utf-8").strip())


def write_lines(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# -- dataclasses ------------------------------------------------------------


def test_usage_total_input_sums_the_three_input_components() -> None:
    usage = Usage(
        input_tokens=5, cache_read_input_tokens=7, cache_creation_input_tokens=11, output_tokens=99
    )
    assert usage.total_input == 23


def test_approx_tokens_uses_documented_constant() -> None:
    assert CHARS_PER_TOKEN == 3.7
    assert approx_tokens("") == 0.0
    assert approx_tokens("x" * 37) == pytest.approx(10.0)
    assert approx_tokens("abc") == pytest.approx(3 / 3.7)


# -- canonical rendering ----------------------------------------------------


def test_render_segments_order_is_tools_system_messages() -> None:
    call = make_call()
    segments = render_segments(call)
    assert [s.kind for s in segments] == ["tools", "system", "message", "message"]
    assert segments[0].text == canonical_json(list(TOOLS))
    assert segments[1].text == call.system
    assert segments[2].text == canonical_json(MESSAGES[0])
    assert segments[3].text == canonical_json(MESSAGES[1])
    assert [s.label for s in segments] == ["tools", "system", "messages[0]", "messages[1]"]


def test_rendered_text_is_concatenation_of_segments() -> None:
    call = make_call()
    assert rendered_text(call) == "".join(s.text for s in render_segments(call))


def test_empty_tools_and_system_still_occupy_positions() -> None:
    call = make_call(tools=(), system="")
    segments = render_segments(call)
    assert [s.kind for s in segments][:2] == ["tools", "system"]
    assert segments[0].text == "[]"
    assert segments[1].text == ""


def test_dict_key_order_never_looks_like_a_prompt_change() -> None:
    # Same values, shuffled key insertion order everywhere.
    tool_a = {
        "name": "search_code",
        "description": "Search the repo.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    tool_b = {
        "input_schema": {"properties": {"query": {"type": "string"}}, "type": "object"},
        "description": "Search the repo.",
        "name": "search_code",
    }
    msg_a = {"role": "user", "content": "Fix the failing TTL test."}
    msg_b = {"content": "Fix the failing TTL test.", "role": "user"}
    a = make_call(tools=(tool_a,), messages=(msg_a,))
    b = make_call(tools=(tool_b,), messages=(msg_b,))
    assert rendered_text(a) == rendered_text(b)
    assert common_prefix_chars(a, b) == len(rendered_text(a))
    assert diverging_segment(a, b) is None
    assert diverging_segment(b, a) is None


def test_common_prefix_chars_extension_and_mid_segment_divergence() -> None:
    base = make_call()
    extended = make_call(messages=MESSAGES + ({"role": "user", "content": "continue"},))
    assert common_prefix_chars(base, extended) == len(rendered_text(base))

    # Divergence inside the system segment: prefix = tools segment + shared chars.
    a = make_call(system="stable-prefix AAA")
    b = make_call(system="stable-prefix BBB")
    tools_len = len(render_segments(a)[0].text)
    assert common_prefix_chars(a, b) == tools_len + len("stable-prefix ")

    identical = make_call()
    assert common_prefix_chars(base, identical) == len(rendered_text(base))


def test_diverging_segment_cases() -> None:
    base = make_call()
    assert diverging_segment(base, make_call()) is None  # identical
    extended = make_call(messages=MESSAGES + ({"role": "user", "content": "go on"},))
    assert diverging_segment(base, extended) is None  # pure extension

    other_tools = ({"name": "read_file", "description": "x", "input_schema": {}},)
    assert diverging_segment(base, make_call(tools=other_tools)) == (0, "tools")
    assert diverging_segment(base, make_call(system="changed")) == (1, "system")

    rewritten = make_call(messages=({"role": "user", "content": "REWRITTEN"},) + MESSAGES[1:])
    assert diverging_segment(base, rewritten) == (2, "message")

    truncated = make_call(messages=MESSAGES[:1])
    assert diverging_segment(base, truncated) == (3, "message")


# -- JSONL round-trip -------------------------------------------------------


def test_jsonl_round_trip_groups_interleaved_runs(tmp_path: Path) -> None:
    a0 = make_call(run_id="run-a", index=0)
    b0 = make_call(run_id="run-b", index=0, ts=1_784_000_100.5, model="claude-haiku-4-5")
    a1 = make_call(
        run_id="run-a",
        index=1,
        messages=MESSAGES + ({"role": "user", "content": "next"},),
        stop_reason="tool_use",
    )
    path = tmp_path / "trace.jsonl"
    write_trace(path, [a0, b0, a1])

    runs = read_trace(path)
    assert [run.run_id for run in runs] == ["run-a", "run-b"]  # first-appearance order
    assert runs[0].calls == (a0, a1)
    assert runs[1].calls == (b0,)
    assert runs[1].calls[0].ts == 1_784_000_100.5


def test_write_trace_empty_and_blank_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    write_trace(path, [])
    assert read_trace(path) == []

    record = valid_record(tmp_path)
    path2 = write_lines(tmp_path, ["", json.dumps(record), "   ", ""])
    (run,) = read_trace(path2)
    assert run.calls[0] == make_call()


# -- precise TraceError messages --------------------------------------------


def test_missing_file_names_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jsonl"
    with pytest.raises(TraceError, match="nope.jsonl"):
        read_trace(missing)


def test_invalid_json_names_line(tmp_path: Path) -> None:
    record = valid_record(tmp_path)
    path = write_lines(tmp_path, [json.dumps(record), "{not json"])
    with pytest.raises(TraceError, match=r"line 2: invalid JSON"):
        read_trace(path)


def test_non_object_line_rejected(tmp_path: Path) -> None:
    path = write_lines(tmp_path, ["[1, 2, 3]"])
    with pytest.raises(TraceError, match=r"line 1: expected a JSON object, got list"):
        read_trace(path)


def test_missing_schema_field(tmp_path: Path) -> None:
    record = valid_record(tmp_path)
    del record["schema"]
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"line 1: missing field 'schema'"):
        read_trace(path)


def test_unknown_schema_names_found_and_supported(tmp_path: Path) -> None:
    record = valid_record(tmp_path)
    record["schema"] = "tokenbill/trace@9"
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"unsupported schema 'tokenbill/trace@9'") as excinfo:
        read_trace(path)
    assert SCHEMA in str(excinfo.value)


def test_missing_field_named_with_line(tmp_path: Path) -> None:
    record = valid_record(tmp_path)
    del record["usage"]
    path = write_lines(tmp_path, [json.dumps(valid_record(tmp_path)), json.dumps(record)])
    with pytest.raises(TraceError, match=r"line 2: missing field 'usage'"):
        read_trace(path)


def test_missing_nested_usage_field(tmp_path: Path) -> None:
    record = valid_record(tmp_path)
    del record["usage"]["output_tokens"]
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"missing field 'usage.output_tokens'"):
        read_trace(path)


def test_negative_usage_rejected(tmp_path: Path) -> None:
    record = valid_record(tmp_path)
    record["usage"]["input_tokens"] = -5
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(
        TraceError, match=r"field 'usage.input_tokens' must be non-negative, got -5"
    ):
        read_trace(path)


def test_wrong_types_are_named(tmp_path: Path) -> None:
    record = valid_record(tmp_path)
    record["index"] = "0"
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"field 'index' must be an int, got str"):
        read_trace(path)

    record = valid_record(tmp_path)
    record["ts"] = True  # bool is not a number here
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"field 'ts' must be a number, got bool"):
        read_trace(path)

    record = valid_record(tmp_path)
    record["tools"] = ["not-a-dict"]
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"field 'tools\[0\]' must be an object, got str"):
        read_trace(path)


def test_non_monotonic_index_rejected(tmp_path: Path) -> None:
    first = valid_record(tmp_path)
    second = valid_record(tmp_path)  # same index 0 for the same run
    path = write_lines(tmp_path, [json.dumps(first), json.dumps(second)])
    with pytest.raises(TraceError, match=r"line 2: non-monotonic index 0 for run 'run-a'"):
        read_trace(path)

    third = valid_record(tmp_path)
    third["index"] = 2
    fourth = valid_record(tmp_path)
    fourth["index"] = 1
    path = write_lines(tmp_path, [json.dumps(third), json.dumps(fourth)])
    with pytest.raises(TraceError, match=r"non-monotonic index 1 .*previous call had index 2"):
        read_trace(path)
