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


# -- IO hardening: surrogates, non-finite numbers, implausible ints ----------


def test_unpaired_surrogate_rejected_in_every_display_field(tmp_path: Path) -> None:
    # Regression: "\ud800" is a legal JSON escape, so json.loads decodes it —
    # but the resulting string cannot be encoded back to UTF-8, which used to
    # crash the terminal summary / HTML write AFTER all analysis had run.
    for field in ("run_id", "model", "system", "stop_reason"):
        record = valid_record(tmp_path)
        line = json.dumps(record)
        # Splice the raw escape into the JSON text (json.dumps of a decoded
        # surrogate would fail in the test itself).
        line = line.replace(json.dumps(record[field]), f'"bad-\\ud800-{field}"')
        path = write_lines(tmp_path, [line])
        with pytest.raises(TraceError, match=rf"field '{field}' contains an unpaired surrogate"):
            read_trace(path)


def test_nan_and_infinity_literals_rejected(tmp_path: Path) -> None:
    # Regression: bare NaN/Infinity are invalid JSON but json.loads accepts
    # them by default; a NaN/inf ts silently disabled every TTL comparison,
    # collapsing optimal-cache to the no-cache price with no warning.
    for literal in ("NaN", "Infinity", "-Infinity"):
        record = valid_record(tmp_path)
        line = json.dumps(record).replace(json.dumps(record["ts"]), literal)
        path = write_lines(tmp_path, [line])
        with pytest.raises(TraceError, match=r"could not parse JSON .*not valid JSON"):
            read_trace(path)
    # Anywhere in the payload, not just ts.
    record = valid_record(tmp_path)
    record["messages"] = [{"role": "user", "content": 0}]
    line = json.dumps(record).replace('"content": 0', '"content": NaN')
    path = write_lines(tmp_path, [line])
    with pytest.raises(TraceError, match=r"could not parse JSON"):
        read_trace(path)


def test_write_trace_refuses_non_finite_numbers(tmp_path: Path) -> None:
    # write_trace must never emit a file read_trace rejects.
    call = make_call(ts=float("nan"))
    with pytest.raises(TraceError, match=r"non-finite number"):
        write_trace(tmp_path / "out.jsonl", [call])


def test_implausibly_large_ints_rejected(tmp_path: Path) -> None:
    # Regression: usage ints of unbounded size passed validation, then crashed
    # the analyzer with OverflowError when multiplied by floats.
    record = valid_record(tmp_path)
    record["usage"]["input_tokens"] = 10**308
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"'usage.input_tokens' is implausibly large"):
        read_trace(path)

    record = valid_record(tmp_path)
    record["index"] = 2**53 + 1
    path = write_lines(tmp_path, [json.dumps(record)])
    with pytest.raises(TraceError, match=r"'index' is implausibly large"):
        read_trace(path)


def test_deeply_nested_json_raises_trace_error_not_recursion_error(tmp_path: Path) -> None:
    # Error-contract regression: pathological nesting used to escape as a raw
    # RecursionError traceback instead of the promised TraceError.
    depth = 100_000
    line = '{"schema": "tokenbill/trace@1", "messages": ' + "[" * depth + "]" * depth + "}"
    path = write_lines(tmp_path, [line])
    with pytest.raises(TraceError, match=r"line 1"):
        read_trace(path)


# -- render caching ----------------------------------------------------------


def test_rendering_is_cached_per_call_instance() -> None:
    call = make_call()
    assert rendered_text(call) is rendered_text(call)  # one render per Call
    first = render_segments(call)
    second = render_segments(call)
    assert first == second
    assert first is not second  # fresh list: callers may mutate their copy
    assert first[0] is second[0]  # ... but the segments themselves are shared


def test_replaced_call_reuses_component_rendering() -> None:
    # breakers.repaired_calls swaps single fields via dataclasses.replace,
    # sharing the tools/messages tuples: their rendering must be reused so a
    # repaired replay costs only its actually-changed bytes.
    import dataclasses

    call = make_call()
    rendered_text(call)  # populate the cache
    repaired = dataclasses.replace(call, cache_breakpoints=0)
    a, b = render_segments(call), render_segments(repaired)
    assert a[0].text is b[0].text  # tools text shared
    assert a[2] is b[2]  # message segments shared
    assert rendered_text(repaired) == rendered_text(call)


def test_common_prefix_chars_matches_character_reference_at_gallop_edges() -> None:
    # The gallop+binary-search implementation must agree with a plain
    # character walk everywhere, especially around power-of-two probe edges.
    def reference(a: Call, b: Call) -> int:
        ta, tb = rendered_text(a), rendered_text(b)
        n = min(len(ta), len(tb))
        i = 0
        while i < n and ta[i] == tb[i]:
            i += 1
        return i

    base_system = "S" * 700
    for cut in (0, 1, 2, 3, 15, 16, 17, 63, 64, 65, 255, 256, 257, 699):
        a = make_call(system=base_system)
        b = make_call(system=base_system[:cut] + "X" + base_system[cut + 1 :])
        assert common_prefix_chars(a, b) == reference(a, b), f"cut={cut}"
    # Zero prefix, pure extension, and identical calls.
    a = make_call(tools=(), system="A")
    b = make_call(tools=(), system="B")
    assert common_prefix_chars(a, b) == reference(a, b) == len(canonical_json([]))
    ext = make_call(messages=MESSAGES + ({"role": "user", "content": "more"},))
    assert common_prefix_chars(make_call(), ext) == reference(make_call(), ext)
    assert common_prefix_chars(make_call(), make_call()) == len(rendered_text(make_call()))


def test_empty_tools_and_empty_messages_render_without_cache_collision() -> None:
    # Regression: () is an interned singleton, so an empty tools tuple and an
    # empty messages tuple share the same id(); a shared component cache slot
    # made rendering return the tools *text* where message segments belonged.
    call = make_call(tools=(), messages=())
    segments = render_segments(call)
    assert [s.kind for s in segments] == ["tools", "system"]
    assert segments[0].text == canonical_json([])
    assert rendered_text(call) == canonical_json([]) + call.system
    # A second empty-payload call must render identically, via the cache.
    again = make_call(tools=(), messages=(), index=1)
    assert rendered_text(again) == rendered_text(call)
