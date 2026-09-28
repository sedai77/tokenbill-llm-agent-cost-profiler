"""Incremental extraction, retention awareness and the state file (CP-VSCODE brief, Build 2;
acceptance "Incremental: …")."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from tokenbill.adapters import copilot_vscode_collect as cvc

from .helpers import NOW_MS, codes, collect, db_files, file_state, mk, read_extract

HOUR = 3_600_000
DAY = 24 * HOUR
POSIX = os.name != "nt"


def _span_ids(res: cvc.CollectResult) -> list[str]:
    return [s["span_id"] for f in db_files(res) for s in read_extract(f)["spans"]]


def _add(db: Path, spans: list) -> None:
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        mk.insert_spans(conn, spans)
    finally:
        conn.close()


def test_second_run_without_new_spans_writes_nothing(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    res1, state = collect(tmp_path, dbs=[db])
    assert len(db_files(res1)) == 1
    res2, state = collect(tmp_path, dbs=[db], state=state)
    assert res2.files == () and res2.notes == ()
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [res1.files[0].name]


def test_new_spans_after_the_mark_are_extracted_once(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    res1, state = collect(tmp_path, dbs=[db])
    _add(db, mk.conversation(50, "c", mk.BASE_MS + HOUR))
    res2, state = collect(tmp_path, dbs=[db], state=state)
    res3, state = collect(tmp_path, dbs=[db], state=state)
    first, second = _span_ids(res1), _span_ids(res2)
    assert len(first) == 5 and len(second) == 5
    assert not set(first) & set(second)
    assert res3.files == ()
    cur = next(iter(state.databases.values()))
    assert cur.mark == (mk.BASE_MS + HOUR + 1960, mk.hex_id(54))


def test_ties_on_start_time_are_ordered_by_span_id(tmp_path: Path) -> None:
    t = mk.BASE_MS
    trace = mk.hex_id(7, 32)
    spans = [mk.chat_span(n, trace, t, conv="tie") for n in (0x30, 0x10, 0x20)]
    db = mk.write_db(tmp_path / "agent-traces.db", spans)
    res1, state = collect(tmp_path, dbs=[db])
    assert _span_ids(res1) == [mk.hex_id(0x10), mk.hex_id(0x20), mk.hex_id(0x30)]
    assert next(iter(state.databases.values())).mark == (t, mk.hex_id(0x30))
    # same start, larger span id → after the mark; smaller span id → a late insert (read once)
    _add(db, [mk.chat_span(0x40, trace, t, conv="tie"), mk.chat_span(0x18, trace, t, conv="tie")])
    res2, state = collect(tmp_path, dbs=[db], state=state)
    assert _span_ids(res2) == [mk.hex_id(0x18), mk.hex_id(0x40)]
    assert res2.stats["late_spans"] == 1
    res3, _ = collect(tmp_path, dbs=[db], state=state)
    assert res3.files == ()


def test_late_inserted_root_span_is_read_once(tmp_path: Path) -> None:
    """VS Code inserts a span when it ends: the invoke_agent root arrives after its children."""
    spans = mk.conversation(1, "c", mk.BASE_MS)
    root, children = spans[0], spans[1:]
    db = mk.write_db(tmp_path / "agent-traces.db", children)
    res1, state = collect(tmp_path, dbs=[db])
    assert root.span_id not in _span_ids(res1)
    _add(db, [root])
    res2, state = collect(tmp_path, dbs=[db], state=state)
    assert _span_ids(res2) == [root.span_id]
    res3, _ = collect(tmp_path, dbs=[db], state=state)
    assert res3.files == ()


def test_late_span_outside_the_window_is_not_read(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS + 10 * HOUR))
    _, state = collect(tmp_path, dbs=[db])
    old = mk.chat_span(0x99, mk.hex_id(0x99, 32), mk.BASE_MS + 10 * HOUR - cvc.LATE_WINDOW_MS - 1,
                       conv="c")
    _add(db, [old])
    res, _ = collect(tmp_path, dbs=[db], state=state)
    assert res.files == ()  # documented limitation (README)


def test_pruned_range_is_a_gap(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    _, state = collect(tmp_path, dbs=[db], now_ms=mk.BASE_MS + HOUR)
    mk.delete_spans(db, "start_time_ms < ?", (mk.BASE_MS + 5 * HOUR,))
    _add(db, mk.conversation(50, "d", mk.BASE_MS + 10 * HOUR))
    res, _ = collect(tmp_path, dbs=[db], state=state, now_ms=mk.BASE_MS + 11 * HOUR)
    [gap] = [n for n in res.notes if n.code == cvc.DQ_GAP]
    assert gap.severity == "warn" and "10 hours" in gap.detail
    assert res.stats["gap_hours"] == 10
    assert len(_span_ids(res)) == 5


def test_no_gap_while_the_mark_span_is_present(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    _, state = collect(tmp_path, dbs=[db])
    mk.delete_spans(db, "start_time_ms < ?", (mk.BASE_MS + 1000,))  # older spans pruned only
    _add(db, mk.conversation(50, "c", mk.BASE_MS + 3 * DAY))
    res, _ = collect(tmp_path, dbs=[db], state=state, now_ms=mk.BASE_MS + 3 * DAY + HOUR)
    assert cvc.DQ_GAP not in codes(res)


def test_unread_spans_aged_six_days_are_a_retention_risk(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    res, _ = collect(tmp_path, dbs=[db], now_ms=mk.BASE_MS + 6 * DAY)
    [risk] = [n for n in res.notes if n.code == cvc.DQ_RETENTION_RISK]
    assert risk.severity == "warn" and "6 days" in risk.detail and "daily" in risk.detail
    res_fresh, _ = collect(tmp_path, dbs=[db], out="fresh", now_ms=mk.BASE_MS + 4 * DAY)
    assert cvc.DQ_RETENTION_RISK not in codes(res_fresh)


def test_recreated_database_restarts_from_its_first_span(tmp_path: Path) -> None:
    db = tmp_path / "agent-traces.db"
    mk.write_db(db, mk.conversation(1, "c", mk.BASE_MS + 5 * HOUR))
    _, state = collect(tmp_path, dbs=[db])
    for p in tmp_path.glob("agent-traces.db*"):
        p.unlink()
    # a different database at the same path, holding spans older than the old head and mark
    mk.write_db(db, mk.conversation(300, "r", mk.BASE_MS + HOUR)
                + mk.conversation(400, "r", mk.BASE_MS + 5 * HOUR + 500))
    res, state = collect(tmp_path, dbs=[db], state=state)
    assert res.stats["databases_restarted"] == 1
    assert len(_span_ids(res)) == 10
    assert cvc.DQ_GAP not in codes(res)
    res2, _ = collect(tmp_path, dbs=[db], state=state)
    assert res2.files == ()


def test_recreated_database_with_newer_spans_needs_no_restart(tmp_path: Path) -> None:
    db = tmp_path / "agent-traces.db"
    mk.write_db(db, mk.conversation(1, "c", mk.BASE_MS))
    _, state = collect(tmp_path, dbs=[db])
    for p in tmp_path.glob("agent-traces.db*"):
        p.unlink()
    mk.write_db(db, mk.conversation(300, "r", mk.BASE_MS + 2 * HOUR))
    res, _ = collect(tmp_path, dbs=[db], state=state)
    assert "databases_restarted" not in res.stats
    assert len(_span_ids(res)) == 5


def test_session_cap_eviction_of_the_mark_is_not_a_recreation(tmp_path: Path) -> None:
    """VS Code's 100-session cap may delete the newest-read session while older spans of a
    longer-running session stay: the head is unchanged, so this is the same database."""
    long_running = mk.conversation(1, "long", mk.BASE_MS, chats=1)
    evicted = mk.conversation(50, "evicted", mk.BASE_MS + HOUR, chats=1)
    db = mk.write_db(tmp_path / "agent-traces.db", long_running + evicted)
    _, state = collect(tmp_path, dbs=[db])
    mk.delete_spans(db, "conversation_id = ?", ("evicted",))
    _add(db, mk.conversation(90, "long", mk.BASE_MS + 2 * HOUR, chats=1))
    res, _ = collect(tmp_path, dbs=[db], state=state)
    assert "databases_restarted" not in res.stats
    assert _span_ids(res) == [mk.hex_id(n) for n in (90, 91, 92)]


def test_source_file_is_never_written(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    before = file_state(db)
    _, state = collect(tmp_path, dbs=[db])
    collect(tmp_path, dbs=[db], state=state)
    assert file_state(db) == before


def test_state_file_round_trip_and_mode(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    otel = tmp_path / "otel.jsonl"
    otel.write_bytes(b"".join(mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS))))
    _, state = collect(tmp_path, dbs=[db], outfiles=[otel])
    path = tmp_path / "state" / "vscode-collector.json"
    state.save(path)
    if POSIX:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = cvc.VsCodeCollectorState.load(path)
    assert loaded == state
    doc = json.loads(path.read_text())
    assert doc["schema"] == cvc.STATE_SCHEMA and doc["last_run_ms"] == NOW_MS
    raw = path.read_bytes()
    assert b"agent-traces" not in raw and str(tmp_path).encode() not in raw  # h_ ids only
    res, _ = collect(tmp_path, dbs=[db], outfiles=[otel], state=loaded)
    assert res.files == ()


@pytest.mark.parametrize("content", [
    b"", b"{not json", b"[]", b'{"schema": "other"}', b"\xff\xfe",
    b'{"schema": "tokenbill/vscode-collector@1", "databases": [], "outfiles": 3}',
])
def test_corrupt_state_starts_fresh(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(content)
    assert cvc.VsCodeCollectorState.load(path) == cvc.VsCodeCollectorState()


def test_missing_and_unreadable_state(tmp_path: Path) -> None:
    assert cvc.VsCodeCollectorState.load(tmp_path / "none.json") == cvc.VsCodeCollectorState()
    folder = tmp_path / "dir.json"
    folder.mkdir()
    assert cvc.VsCodeCollectorState.load(folder) == cvc.VsCodeCollectorState()


def test_bad_cursor_entries_are_dropped(tmp_path: Path) -> None:
    good_db = {"mark_start_ms": 5, "mark_span_id": "a", "head_start_ms": 1, "head_sha": "x",
               "recent": [[5, "a"], [4, "b"], [5, "a"]]}
    doc = {"schema": cvc.STATE_SCHEMA, "last_run_ms": "x", "otel_seq": -3,
           "databases": {"h_ok": good_db,
                         "h_half": {"mark_start_ms": 5},
                         "h_types": {"mark_start_ms": "5", "mark_span_id": "a"},
                         "h_head": {"head_start_ms": 1, "head_sha": 2},
                         "h_recent": {"recent": "no"},
                         "h_pair": {"recent": [[1]]},
                         "h_list": [1]},
           "outfiles": {"h_o": {"offset": 10, "head_sha": "s", "head_len": 10},
                        "h_neg": {"offset": -1, "head_sha": "s", "head_len": 0},
                        "h_big": {"offset": 1, "head_sha": "s", "head_len": 10**6},
                        "h_bad": "x"}}
    path = tmp_path / "s.json"
    path.write_text(json.dumps(doc))
    st = cvc.VsCodeCollectorState.load(path)
    assert list(st.databases) == ["h_ok"] and list(st.outfiles) == ["h_o"]
    assert st.databases["h_ok"].recent == [(4, "b"), (5, "a")]
    assert st.last_run_ms is None and st.otel_seq == 0
    assert st.databases["h_ok"].mark == (5, "a")
    assert cvc.DatabaseCursor().mark is None


def test_recent_ids_stay_inside_the_late_window(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    _, state = collect(tmp_path, dbs=[db])
    _add(db, mk.conversation(50, "c", mk.BASE_MS + cvc.LATE_WINDOW_MS + DAY))
    _, state = collect(tmp_path, dbs=[db], state=state, now_ms=mk.BASE_MS + 3 * DAY)
    cur = next(iter(state.databases.values()))
    assert {sid for _, sid in cur.recent} == {mk.hex_id(n) for n in range(50, 55)}
