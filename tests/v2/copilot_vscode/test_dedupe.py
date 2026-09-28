"""Dedupe against the in-VS Code CLI agent (CP-VSCODE brief, Build 4; acceptance "Dedupe: …")."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tokenbill.adapters import copilot_vscode_collect as cvc

from .helpers import collect, db_files, mk, otel_files, read_extract

CLI_SESSION = "0b8c3d52-6f1e-4c2a-9a57-3e2f7c1d9e40"


def _synth(n: int, trace: str, start: int, conv: str = CLI_SESSION) -> mk.Span:
    """A chat span VS Code synthesizes for the CLI agent host: no response id, no cache writes."""
    return mk.chat_span(n, trace, start, conv=conv, response_id=None, name="chat")


def test_synthesized_only_conversation_is_kept_and_covered(tmp_path: Path) -> None:
    trace = mk.hex_id(1, 32)
    root = mk.agent_span(1, trace, mk.BASE_MS, conv=CLI_SESSION)
    spans = [root, _synth(2, trace, mk.BASE_MS + 10), _synth(3, trace, mk.BASE_MS + 2000)]
    other = mk.conversation(50, "vscode-panel-conv", mk.BASE_MS + 5000)
    db = mk.write_db(tmp_path / "agent-traces.db", spans + other)
    res, _ = collect(tmp_path, dbs=[db], cli=frozenset({CLI_SESSION, "not-in-vscode"}))
    assert res.covered_session_ids == frozenset({CLI_SESSION})
    x = read_extract(db_files(res)[0])
    kept = {s["span_id"] for s in x["spans"]}
    assert {mk.hex_id(2), mk.hex_id(3)} <= kept
    assert x["meta"]["dropped_synthesized"] == "0"
    synth_attrs = {k for sid, k, _ in x["attrs"] if sid == mk.hex_id(2)}
    assert "gen_ai.response.id" not in synth_attrs
    assert "copilot_chat.copilot_usage_nano_aiu" in synth_attrs


def test_native_and_synthesized_spans_of_one_call(tmp_path: Path) -> None:
    trace = mk.hex_id(1, 32)
    native = mk.chat_span(2, trace, mk.BASE_MS + 10, conv=CLI_SESSION, name="chat")
    synth = _synth(3, trace, mk.BASE_MS + 11)
    other_trace = _synth(4, mk.hex_id(2, 32), mk.BASE_MS + 12)  # no native in its trace
    other_conv = _synth(5, trace, mk.BASE_MS + 13, conv="another-conversation")
    db = mk.write_db(tmp_path / "agent-traces.db", [native, synth, other_trace, other_conv])
    res, _ = collect(tmp_path, dbs=[db], cli=frozenset({CLI_SESSION}))
    x = read_extract(db_files(res)[0])
    assert [s["span_id"] for s in x["spans"]] == [mk.hex_id(2), mk.hex_id(4), mk.hex_id(5)]
    assert x["meta"]["dropped_synthesized"] == "1"
    assert res.stats["spans_dropped_synthesized"] == 1
    assert res.covered_session_ids == frozenset({CLI_SESSION})


def test_synthesized_span_arriving_after_its_native_span(tmp_path: Path) -> None:
    trace = mk.hex_id(1, 32)
    db = mk.write_db(tmp_path / "agent-traces.db",
                     [mk.chat_span(2, trace, mk.BASE_MS, conv=CLI_SESSION, name="chat")])
    _, state = collect(tmp_path, dbs=[db])
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        mk.insert_spans(conn, [_synth(3, trace, mk.BASE_MS + 50)])
    finally:
        conn.close()
    res, state = collect(tmp_path, dbs=[db], state=state)
    assert res.files == () and res.stats["spans_dropped_synthesized"] == 1
    res2, _ = collect(tmp_path, dbs=[db], state=state)
    assert "spans_dropped_synthesized" not in res2.stats  # counted once


def test_covered_by_chat_session_id_and_only_chat_spans(tmp_path: Path) -> None:
    trace = mk.hex_id(1, 32)
    chat = mk.chat_span(2, trace, mk.BASE_MS, conv="vs-conv")
    chat.attributes["copilot_chat.chat_session_id"] = CLI_SESSION
    agent_only = mk.agent_span(3, mk.hex_id(3, 32), mk.BASE_MS + 10, conv="agent-only-session")
    db = mk.write_db(tmp_path / "agent-traces.db", [chat, agent_only])
    res, _ = collect(tmp_path, dbs=[db],
                     cli=frozenset({CLI_SESSION, "agent-only-session"}))
    assert res.covered_session_ids == frozenset({CLI_SESSION})


def test_covered_includes_conversations_read_in_earlier_runs(tmp_path: Path) -> None:
    trace = mk.hex_id(1, 32)
    db = mk.write_db(tmp_path / "agent-traces.db", [_synth(2, trace, mk.BASE_MS)])
    _, state = collect(tmp_path, dbs=[db])
    res, _ = collect(tmp_path, dbs=[db], state=state, cli=frozenset({CLI_SESSION}))
    assert res.files == () and res.covered_session_ids == frozenset({CLI_SESSION})


def test_no_cli_sessions_no_coverage(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", [_synth(2, mk.hex_id(1, 32), mk.BASE_MS)])
    res, _ = collect(tmp_path, dbs=[db])
    assert res.covered_session_ids == frozenset()


def test_outfile_dedupe_and_coverage(tmp_path: Path) -> None:
    trace = mk.hex_id(1, 32)
    native = mk.chat_span(2, trace, mk.BASE_MS + 10, conv=CLI_SESSION, name="chat")
    synth = _synth(3, trace, mk.BASE_MS + 11)
    lone = _synth(4, mk.hex_id(9, 32), mk.BASE_MS + 12, conv="lone-cli-session")
    path = tmp_path / "otel.jsonl"
    path.write_bytes(b"".join(mk.outfile_lines([native], service="github-copilot")
                              + mk.outfile_lines([synth, lone])))
    res, _ = collect(tmp_path, outfiles=[path],
                     cli=frozenset({CLI_SESSION, "lone-cli-session", "absent"}))
    [out] = otel_files(res)
    ids = [line.split(b'"spanId":"')[1][:16].decode() for line in out.read_bytes().splitlines()]
    assert ids == [mk.hex_id(2), mk.hex_id(4)]
    assert res.stats["otel_dropped_synthesized"] == 1
    assert res.covered_session_ids == frozenset({CLI_SESSION, "lone-cli-session"})


def test_aborted_extract_covers_nothing(tmp_path: Path) -> None:
    span = _synth(2, mk.hex_id(1, 32), mk.BASE_MS)
    span.attributes["gen_ai.request.model"] = "sk-ant-api03-" + "A1b2C3d4E5" * 3
    db = mk.write_db(tmp_path / "agent-traces.db", [span])
    res, state = collect(tmp_path, dbs=[db], cli=frozenset({CLI_SESSION}))
    assert res.files == () and res.covered_session_ids == frozenset()
    assert [n.code for n in res.notes] == [cvc.DQ_ABORTED]
    assert state.databases == {}
