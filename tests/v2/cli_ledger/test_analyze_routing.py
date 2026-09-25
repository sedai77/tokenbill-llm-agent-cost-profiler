"""SPEC §15.1 / D2 / D38: ``analyze`` engine routing, run-id namespacing and the v2 engine."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from tokenbill import cli
from tokenbill.commands import analyze as analyze_cmd

from .helpers import run_main

NOTE_RE = re.compile(r"^note: using the v2 engine because (.+) \(--engine v1 forces the legacy "
                     r"engine\)$")


def _calls(name: str = "well-behaved") -> list:
    from tokenbill.demo_traces import scenario

    return list(scenario(name, 7))


def _write(path: Path, calls: list) -> Path:
    from tokenbill.trace import write_trace

    write_trace(path, calls)
    return path


def _mark_last_message(call: object, ttl: str | None = None) -> object:
    """A ``cache_control`` marker on the last message only (the common agent-loop pattern: the
    marker moves forward every call)."""
    messages = [dict(m) for m in call.messages]  # type: ignore[attr-defined]
    last = messages[-1]
    content = last["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [
        dict(b) for b in content]
    marker = {"type": "ephemeral"} if ttl is None else {"type": "ephemeral", "ttl": ttl}
    blocks[-1] = {**blocks[-1], "cache_control": marker}
    last["content"] = blocks
    return dataclasses.replace(call, messages=tuple(messages))  # type: ignore[type-var]


def _mark_first_message_1h(call: object) -> object:
    """A static ``ttl: "1h"`` marker on the first message of every call (it never moves)."""
    messages = [dict(m) for m in call.messages]  # type: ignore[attr-defined]
    first = messages[0]
    content = first["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [
        dict(b) for b in content]
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    first["content"] = blocks
    return dataclasses.replace(call, messages=tuple(messages))  # type: ignore[type-var]


def _note(err: str) -> str:
    lines = [line for line in err.splitlines() if line.startswith("note:")]
    assert len(lines) == 1, err
    match = NOTE_RE.match(lines[0])
    assert match is not None, lines[0]
    return match.group(1)


@pytest.fixture
def moving(tmp_path: Path) -> Path:
    return _write(tmp_path / "moving.jsonl", [_mark_last_message(c) for c in _calls()])


def test_moving_markers_switch_to_v2_with_one_note(moving: Path) -> None:
    code, out, err = run_main(["analyze", str(moving)])
    assert code == 0
    assert _note(err) == analyze_cmd.REASON_MOVING
    assert "TOKEN BILL · analyze" in out and "exact" in out


def test_one_hour_marker_switches_to_v2(tmp_path: Path) -> None:
    path = _write(tmp_path / "ttl1h.jsonl", [_mark_first_message_1h(c) for c in _calls()])
    code, out, err = run_main(["analyze", str(path)])
    assert code == 0
    assert _note(err) == analyze_cmd.REASON_1H
    assert "TOKEN BILL" in out


def test_interleaved_models_switch_to_v2(tmp_path: Path) -> None:
    calls = _calls()
    models = ["claude-sonnet-5", "claude-opus-5-5"]
    mixed = [dataclasses.replace(c, model=models[i % 2]) for i, c in enumerate(calls)]
    path = _write(tmp_path / "mixed.jsonl", mixed)
    code, _out, err = run_main(["analyze", str(path)])
    assert code == 0
    assert _note(err) == analyze_cmd.REASON_MODELS


def test_a_single_model_change_stays_on_v1(tmp_path: Path) -> None:
    calls = _calls()
    half = len(calls) // 2
    one = [dataclasses.replace(c, model="claude-opus-5-5") if i >= half else c
           for i, c in enumerate(calls)]
    path = _write(tmp_path / "switch.jsonl", one)
    code, out, err = run_main(["analyze", str(path)])
    assert code == 0 and err == ""
    assert out.startswith("~")


def test_unknown_model_alone_stays_on_v1_unavailable(tmp_path: Path) -> None:
    calls = [dataclasses.replace(c, model="acme-llm-1") for c in _calls()]
    path = _write(tmp_path / "unknown.jsonl", calls)
    code, out, err = run_main(["analyze", str(path)])
    assert code == 0 and "note:" not in err  # v0.1 logs its unknown-model warning, no routing
    assert "unavailable" in out


def test_unknown_model_with_engine_v2_is_unpriced_not_zero(tmp_path: Path) -> None:
    calls = [dataclasses.replace(c, model="acme-llm-1") for c in _calls()]
    path = _write(tmp_path / "unknown.jsonl", calls)
    code, out, err = run_main(["analyze", "--engine", "v2", str(path)])
    assert code == 0 and "note:" not in err
    assert re.search(r"unpriced \(\d+ inferences\)", out), out


def test_model_price_prices_the_unknown_model_in_v2(tmp_path: Path) -> None:
    calls = [dataclasses.replace(c, model="acme-llm-2") for c in _calls()]
    path = _write(tmp_path / "unknown.jsonl", calls)
    try:
        code, out, _err = run_main(["analyze", "--engine", "v2", "--format", "json",
                                    "--model-price", "acme-llm-2=3,15", str(path)])
    finally:
        from tokenbill import pricing

        pricing.PRICING.pop("acme-llm-2", None)
    assert code == 0
    doc = json.loads(out)
    assert doc["bill"]["coverage"]["unpriced_inferences"] == 0
    assert int(doc["bill"]["exact"]["nano"]) > 0


def test_surrogate_model_exits_1_with_the_012_message_before_routing(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_mark_last_message(c) for c in _calls()])
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"claude-sonnet-5"', '"claude\\ud800model"'), encoding="utf-8")
    code, out, err = run_main(["analyze", str(path)])
    assert code == 1 and out == ""
    assert err.startswith("tokenbill: error:") and "unpaired surrogate" in err
    assert len(err.strip().splitlines()) == 1


def test_engine_v1_forces_the_legacy_engine(moving: Path) -> None:
    code, out, err = run_main(["analyze", "--engine", "v1", str(moving)])
    assert code == 0 and err == ""
    assert out.startswith("~")


def test_engine_v1_with_json_is_a_usage_error(moving: Path) -> None:
    code, _out, err = run_main(["analyze", "--engine", "v1", "--format", "json", str(moving)])
    assert code == 2 and "v2 engine" in err


def test_json_format_routes_to_v2_and_validates(tmp_path: Path) -> None:
    from tokenbill.outputs.result_json import validate_result_json

    path = _write(tmp_path / "wb.jsonl", _calls())
    code, out, err = run_main(["analyze", "--format", "json", "--deterministic", str(path)])
    assert code == 0
    assert "--format json has no v1 renderer" in err
    doc = json.loads(out)
    assert doc["command"] == "analyze" and "generated_ms" not in doc
    assert validate_result_json(doc) == []


def test_v2_writes_the_html_report(moving: Path, tmp_path: Path) -> None:
    report = tmp_path / "r.html"
    code, out, _err = run_main(["analyze", str(moving), "-o", str(report)])
    assert code == 0
    assert f"Report written to {report}" in out
    assert report.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_v2_missing_output_directory_fails_before_analysis(moving: Path) -> None:
    code, out, err = run_main(["analyze", "--engine", "v2", str(moving), "-o",
                               "missing_dir/r.html"])
    assert code == 1 and out == ""
    assert "does not exist" in err


def _dollars(out: str) -> list[str]:
    return re.findall(r"billed dollars\s+(\$[0-9.,]+)", out)


def test_two_files_sharing_a_run_id_add_up_in_v1(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.jsonl", _calls())
    b = _write(tmp_path / "b.jsonl", _calls())
    code, single, _ = run_main(["analyze", str(a)])
    assert code == 0
    code, both, err = run_main(["analyze", str(a), str(b)])
    assert code == 0 and err == ""
    assert "Run a.jsonl:demo-well-behaved-seed7" in both
    assert "Run b.jsonl:demo-well-behaved-seed7" in both
    assert "| 2 runs |" in both
    assert _dollars(both) == _dollars(single) * 2  # $22, not $40: each run priced once


def test_namespace_runs_renames_only_colliding_ids(tmp_path: Path) -> None:
    from tokenbill.trace import Run

    calls = _calls()
    run = Run(run_id="r1", calls=tuple(dataclasses.replace(c, run_id="r1") for c in calls))
    other = Run(run_id="r2", calls=tuple(dataclasses.replace(c, run_id="r2") for c in calls))
    out = cli.namespace_runs([(Path("x/a.jsonl"), [run, other]), (Path("y/b.jsonl"), [run])])
    assert [r.run_id for r in out] == ["a.jsonl:r1", "r2", "b.jsonl:r1"]
    assert {c.run_id for c in out[0].calls} == {"a.jsonl:r1"}
    same = cli.namespace_runs([(Path("x/a.jsonl"), [run]), (Path("y/a.jsonl"), [run])])
    assert [r.run_id for r in same] == [f"{Path('x/a.jsonl')}:r1", f"{Path('y/a.jsonl')}:r1"]


def test_no_runs_is_a_runtime_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    code, _out, err = run_main(["analyze", str(empty)])
    assert code == 1 and "no runs found" in err


def test_unsafe_reason_unit() -> None:
    calls = _calls()
    from tokenbill.trace import Run

    assert analyze_cmd.unsafe_reason([Run(run_id="r", calls=tuple(calls))]) is None
    marked = tuple(_mark_last_message(c) for c in calls)
    assert analyze_cmd.unsafe_reason([Run(run_id="r", calls=marked)]) == \
        analyze_cmd.REASON_MOVING
    static = tuple(_mark_first_message_1h(c) for c in calls)
    assert analyze_cmd.unsafe_reason([Run(run_id="r", calls=static)]) == analyze_cmd.REASON_1H
    deep: dict = {"a": 1}
    for _ in range(80):
        deep = {"x": deep}
    assert not analyze_cmd._has_1h(deep) and not analyze_cmd._has_marker(deep)
    assert analyze_cmd._strip([1, (2, {"cache_control": 1, "k": 3})]) == [1, (2, {"k": 3})]
