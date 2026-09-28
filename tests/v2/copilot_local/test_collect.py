"""Incremental collector acceptance (CP-LOCAL brief): VS Code coverage skips, shared keys, byte
cursors, rotation, in-flight responses, the private state file, untouched Copilot files and the
experimental store high-water mark."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest

from tokenbill.adapters import copilot_cli
from tokenbill.adapters.copilot_cli import CopilotCliAdapter
from tokenbill.adapters.copilot_collect import (
    STATE_SCHEMA,
    CopilotCollectorState,
    CopilotFileCursor,
    collect_incremental_copilot,
    session_ids,
)
from tokenbill.core.builders import assert_no_canary
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import copilot_lane_key, copilot_session_key
from tokenbill.core.records import LaneEventKind, to_json

from .helpers import (
    SID_A,
    SID_B,
    T0,
    Events,
    blob,
    make_store,
    notes_by_code,
    opts,
    row,
    session_file,
    usage,
)

NOW = int(time.time() * 1000)
FLAG = frozenset({"copilot-store"})


def collect(home: Path, state: CopilotCollectorState, *, now: int = NOW, **kw):
    return list(collect_incremental_copilot(home, state, opts(**kw.pop("o", {})), now_ms=now,
                                            **kw))


def _requests(results):
    return [r for res in results for r in res.requests]


def _events(results, kind=None):
    return [e for res in results for e in res.events if kind is None or e.kind is kind]


def _age(path: Path, minutes: int) -> None:
    old = time.time() - minutes * 60
    os.utime(path, (old, old))


def _two_sessions(home: Path) -> None:
    from tests.v2.fixtures.copilot_local.build_fixtures import session_a, session_b

    session_a().write(session_file(home, SID_A))
    session_b().write(session_file(home, SID_B))


def test_skip_session_ids_keep_lane_events_only(tmp_path: Path) -> None:
    _two_sessions(tmp_path)
    assert session_ids(tmp_path) == frozenset({SID_A, SID_B})
    state = CopilotCollectorState()
    results = collect(tmp_path, state, skip_session_ids=frozenset({SID_A}))
    sk_a, sk_b = copilot_session_key(SID_A), copilot_session_key(SID_B)
    reqs = _requests(results)
    assert reqs and {r.session_key for r in reqs} == {sk_b}
    aggs = [a for res in results for a in res.aggregates]
    assert {dict(a.dims)["model"] for a in aggs} == {"claude-haiku-4-5"}
    events_a = [e for e in _events(results) if e.lane_key in {
        copilot_lane_key(sk_a, "main", None), copilot_lane_key(sk_a, "subagent", "agent-1")}]
    kinds = {e.kind for e in events_a}
    assert LaneEventKind.MODEL_SWITCH_USER in kinds and LaneEventKind.CONTEXT_EDIT in kinds
    assert LaneEventKind.COST_STATE not in kinds                          # no money
    (meta,) = [e for e in events_a if e.kind is LaneEventKind.SESSION_META]
    assert "credit_limit_nano" not in dict(meta.attrs)
    covered = [notes_by_code(res).get("dq.copilot_session_covered_by_vscode", 0)
               for res in results]
    assert sum(covered) == 1
    for res in results:
        assert_no_canary(blob(res))


def test_keys_match_core_ids_and_one_shot_read(tmp_path: Path) -> None:
    _two_sessions(tmp_path)
    results = collect(tmp_path, CopilotCollectorState())
    one_shot = CopilotCliAdapter().read(tmp_path, opts())
    assert sorted(r.request_id for r in _requests(results)) == sorted(
        r.request_id for r in one_shot.requests)
    def canon(reqs):   # the source ref names the file (collector) or the home (one-shot)
        return sorted(json.dumps(to_json(dataclasses.replace(r, source=None)), sort_keys=True)
                      for r in reqs)

    assert canon(_requests(results)) == canon(one_shot.requests)
    sk = copilot_session_key(SID_A)
    lanes = {lane.lane_key for res in results for s in res.sessions for lane in s.lanes
             if s.session_key == sk}
    assert lanes == {copilot_lane_key(sk, "main", None),
                     copilot_lane_key(sk, "subagent", "agent-1")}
    assert all(res.source.adapter == "copilot-cli" for res in results)


def test_second_run_emits_only_new_data(tmp_path: Path) -> None:
    ev = Events()
    ev.start("inc-1")
    ev.message("c1", 10)
    path = ev.write(session_file(tmp_path, "inc-1"))
    state = CopilotCollectorState()
    first = collect(tmp_path, state)
    assert [r.final_attempt.provider_message_id for r in _requests(first)] == ["c1"]
    assert collect(tmp_path, state) == []                                 # nothing changed
    ev.message("c2", 20)
    ev.add("session.truncation", {"tokensRemovedDuringTruncation": 7})
    ev.shutdown({"claude-sonnet-4.5": usage(100, 10, 5, 30)})
    ev.write(path)
    second = collect(tmp_path, state)
    assert [r.final_attempt.provider_message_id for r in _requests(second)] == ["c2"]
    assert [e.kind for e in _events(second)] == [LaneEventKind.CONTEXT_EDIT]
    assert len([a for res in second for a in res.aggregates]) == 1
    (req,) = _requests(second)
    assert req.seq == 1                                                   # seq resumes
    assert collect(tmp_path, state) == []
    cur = state.cursors[next(iter(state.cursors))]
    assert cur.offset == path.stat().st_size


def test_rotated_file_is_reread(tmp_path: Path) -> None:
    ev = Events()
    ev.start("rot")
    ev.message("c1", 10)
    ev.message("c2", 10)
    path = ev.write(session_file(tmp_path, "rot"))
    state = CopilotCollectorState()
    collect(tmp_path, state)
    other = Events(T0 + 10_000_000, prefix="r")
    other.start("rot")
    other.message("n1", 5)
    other.message("n2", 6)
    other.message("n3", 7)
    other.write(path)                                                     # new head, larger file
    again = collect(tmp_path, state)
    assert [r.final_attempt.provider_message_id for r in _requests(again)] == ["n1", "n2", "n3"]
    shorter = Events(prefix="s")
    shorter.start("rot")
    shorter.write(path)                                                   # truncated
    third = collect(tmp_path, state)
    assert _requests(third) == [] and len(_events(third, LaneEventKind.SESSION_META)) == 1


def test_open_chunked_response_and_unterminated_line_are_held(tmp_path: Path) -> None:
    ev = Events()
    ev.start("fly")
    ev.message("c1", 10)
    ev.message("c2", 40, chunkIndex=0, chunkCount=2)
    path = ev.write(session_file(tmp_path, "fly"))
    state = CopilotCollectorState()
    first = collect(tmp_path, state)
    assert [r.final_attempt.provider_message_id for r in _requests(first)] == ["c1"]
    ev.message("c2", 45, chunkIndex=1, chunkCount=2)
    ev.message("c3", 5)
    body = b"\n".join(ev.lines())
    path.write_bytes(body[:-7])                                           # c3 half written
    second = collect(tmp_path, state)
    reqs = _requests(second)
    assert [(r.final_attempt.provider_message_id, r.final_attempt.inferences[0].usage.output)
            for r in reqs] == [("c2", 45)]
    assert second[0].quarantined == []
    path.write_bytes(body + b"\n")
    third = collect(tmp_path, state)
    assert [r.final_attempt.provider_message_id for r in _requests(third)] == ["c3"]


def test_quiescent_file_flushes_open_response(tmp_path: Path) -> None:
    ev = Events()
    ev.start("idle")
    ev.message("c1", 40, chunkIndex=0, chunkCount=3)
    path = ev.write(session_file(tmp_path, "idle"), trailing_newline=False)
    state = CopilotCollectorState()
    assert _requests(collect(tmp_path, state)) == []
    _age(path, 30)
    flushed = collect(tmp_path, state)
    assert [r.final_attempt.inferences[0].usage.output for r in _requests(flushed)] == [40]
    assert collect(tmp_path, state) == []
    # an mtime far after the injected clock is final too
    ev2 = Events()
    ev2.start("future")
    ev2.message("f1", 9, chunkIndex=0, chunkCount=2)
    ev2.write(session_file(tmp_path, "future"))
    res = collect(tmp_path, CopilotCollectorState(), now=T0)
    assert "f1" in [r.final_attempt.provider_message_id for r in _requests(res)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_state_file_private_and_roundtrip(tmp_path: Path) -> None:
    _two_sessions(tmp_path)
    state = CopilotCollectorState()
    collect(tmp_path, state)
    target = tmp_path / "state" / "copilot-collector.json"
    state.save(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    text = target.read_text()
    assert "TB-CANARY" not in text and "acme" not in text
    loaded = CopilotCollectorState.load(target)
    assert loaded.cursors == state.cursors and loaded.contexts == json.loads(text)["contexts"]
    assert collect(tmp_path, loaded) == []


def test_copilot_files_untouched(tmp_path: Path) -> None:
    _two_sessions(tmp_path)
    make_store(tmp_path / "session-store.db", [row(1, SID_A, T0 + 3_000)])
    watched = [session_file(tmp_path, SID_A), session_file(tmp_path, SID_B),
               tmp_path / "session-store.db"]
    for p in watched:
        _age(p, 1)
    before = {p: p.stat().st_mtime_ns for p in watched}
    listing = sorted(os.listdir(tmp_path))
    state = CopilotCollectorState()
    collect(tmp_path, state, o={"experimental": FLAG})
    CopilotCliAdapter().read(tmp_path, opts(experimental=FLAG))
    assert {p: p.stat().st_mtime_ns for p in watched} == before
    assert sorted(os.listdir(tmp_path)) == listing                        # no -wal / -shm files


def test_state_load_edge_cases(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    assert CopilotCollectorState.load(path).cursors == {}
    path.write_text("{not json")
    assert CopilotCollectorState.load(path).cursors == {}
    path.write_text(json.dumps({"schema": "other"}))
    assert CopilotCollectorState.load(path).cursors == {}
    good = CopilotFileCursor("k", 10, "h", 10, 5).to_json()
    path.write_text(json.dumps({"schema": STATE_SCHEMA,
                                "cursors": {"k": good, "bad": {"offset": "x"}, "x": good},
                                "contexts": {"k": {"dir_id": "d"}, "zz": {}},
                                "store": {"source": "s", "hwm": 3, "done": [4, 5]}}))
    loaded = CopilotCollectorState.load(path)
    assert set(loaded.cursors) == {"k"} and set(loaded.contexts) == {"k"}
    assert loaded.store == {"source": "s", "hwm": 3, "done": [4, 5]}
    assert CopilotFileCursor.from_json([1]) is None
    assert CopilotFileCursor.from_json(dict(good, size=-1)) is None
    path.write_text(json.dumps({"schema": STATE_SCHEMA, "store": {"hwm": "x"}}))
    assert CopilotCollectorState.load(path).store == {}
    (tmp_path / "dir.json").mkdir()
    assert CopilotCollectorState.load(tmp_path / "dir.json").cursors == {}


def test_damaged_context_rereads_and_deleted_files_pruned(tmp_path: Path) -> None:
    ev = Events()
    ev.start("dmg")
    ev.message("c1", 10)
    ev.write(session_file(tmp_path, "dmg"))
    state = CopilotCollectorState()
    collect(tmp_path, state)
    key = next(iter(state.contexts))
    state.contexts[key] = {"dir_id": 5}
    ev.message("c2", 10)
    ev.write(session_file(tmp_path, "dmg"))
    again = collect(tmp_path, state)
    assert [r.final_attempt.provider_message_id for r in _requests(again)] == ["c1", "c2"]
    session_file(tmp_path, "dmg").unlink()
    assert collect(tmp_path, state) == [] and state.cursors == {} and state.contexts == {}


def test_content_tier_refused(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        collect(tmp_path, CopilotCollectorState(), o={"content_tier": "full"})


# ----------------------------------------------------------------------------------- store


def _store_session(home: Path, *, shutdown: bool) -> Events:
    ev = Events(T0)
    ev.start("st-1")
    ev.message("c1", 11)
    ev.message("c2", 22)
    if shutdown:
        ev.shutdown({"claude-sonnet-4.5": usage(20_000, 16_000, 2_000, 33)})
    ev.write(session_file(home, "st-1"))
    return ev


def test_store_mode_holds_open_legs_then_joins(tmp_path: Path) -> None:
    ev = _store_session(tmp_path, shutdown=False)
    store = make_store(tmp_path / "session-store.db", [
        row(1, "st-1", T0 + 2_500), row(2, "st-1", T0 + 3_400)])
    state = CopilotCollectorState()
    first = collect(tmp_path, state, o={"experimental": FLAG})
    assert _requests(first) == []                                         # leg still open
    assert state.store["hwm"] == 0 and state.store["done"] == []
    ctx = state.contexts[next(iter(state.contexts))]
    assert len(ctx["pending"]) == 2
    ev.shutdown({"claude-sonnet-4.5": usage(20_000, 16_000, 2_000, 33)})
    ev.write(session_file(tmp_path, "st-1"))
    second = collect(tmp_path, state, o={"experimental": FLAG})
    reqs = _requests(second)
    assert sorted((r.final_attempt.provider_message_id,
                   r.final_attempt.inferences[0].usage.cache_read) for r in reqs) == [
        ("c1", 8_000), ("c2", 8_000)]
    assert all("usage_sequence" in res.capabilities for res in second if res.requests)
    assert [a for res in second for a in res.aggregates] == []           # rows replace rollup
    assert state.store["hwm"] == 2
    conn = sqlite3.connect(store)
    conn.execute("INSERT INTO assistant_usage_events (session_id, model, input_tokens, "
                 "cache_read_tokens, cache_write_tokens, output_tokens, created_at) VALUES "
                 "('gone-session', 'gpt-5.4', 50, 0, 0, 5, '2026-09-01T00:00:00Z')")
    conn.commit()
    conn.close()
    third = collect(tmp_path, state, o={"experimental": FLAG})
    assert [r.session_key for r in _requests(third)] == [copilot_session_key("gone-session")]
    assert state.store["hwm"] == 3
    assert collect(tmp_path, state, o={"experimental": FLAG}) == []


def test_store_mode_quiescent_settles_and_residuals(tmp_path: Path) -> None:
    _store_session(tmp_path, shutdown=False)
    make_store(tmp_path / "session-store.db", [row(1, "st-1", T0 + 2_500)])
    _age(session_file(tmp_path, "st-1"), 30)
    state = CopilotCollectorState()
    results = collect(tmp_path, state, o={"experimental": FLAG})
    kinds = sorted((r.final_attempt.provider_message_id, r.final_attempt.inferences[0].kind.value)
                   for r in _requests(results))
    assert kinds == [("c1", "message"), ("c2", "output_residual")]
    assert state.store["hwm"] == 1


def test_store_mode_fresh_store_only_rows_held(tmp_path: Path) -> None:
    make_store(tmp_path / "session-store.db", [row(1, "elsewhere", NOW - 1_000),
                                               row(2, "covered", NOW - 10**9),
                                               row(3, "elsewhere", NOW - 10**9)])
    (tmp_path / "session-state").mkdir()
    state = CopilotCollectorState()
    results = collect(tmp_path, state, o={"experimental": FLAG},
                      skip_session_ids=frozenset({"covered"}))
    reqs = _requests(results)
    assert [r.seq for r in reqs] == [3]                                    # row 1 too fresh
    assert notes_by_code(results[-1])["dq.copilot_session_covered_by_vscode"] == 1
    assert state.store["hwm"] == 0 and state.store["done"] == [2, 3]
    again = collect(tmp_path, state, o={"experimental": FLAG}, now=NOW + 3_600_000)
    assert [r.seq for r in _requests(again)] == [1] and state.store["hwm"] == 3


def test_store_locked_skips_the_run(tmp_path: Path, monkeypatch) -> None:
    _store_session(tmp_path, shutdown=True)
    make_store(tmp_path / "session-store.db", [row(1, "st-1", T0 + 2_500)])
    monkeypatch.setattr(copilot_cli, "STORE_TIMEOUT_S", 0)
    writer = sqlite3.connect(tmp_path / "session-store.db")
    writer.execute("BEGIN EXCLUSIVE")
    state = CopilotCollectorState()
    try:
        results = collect(tmp_path, state, o={"experimental": FLAG})
    finally:
        writer.rollback()
        writer.close()
    assert len(results) == 1 and results[0].requests == []
    assert notes_by_code(results[0])["dq.copilot_store_unavailable"] == 1
    assert state.cursors == {} and state.store == {}                     # nothing committed
    after = collect(tmp_path, state, o={"experimental": FLAG})
    assert [r.final_attempt.provider_message_id for r in _requests(after)] == ["c1", "c2"]


def test_store_schema_mismatch_falls_back_to_events(tmp_path: Path) -> None:
    _store_session(tmp_path, shutdown=True)
    make_store(tmp_path / "session-store.db", [], without_table=True)
    results = collect(tmp_path, CopilotCollectorState(), o={"experimental": FLAG})
    assert [a for res in results for a in res.aggregates]                  # rollup kept
    assert any(notes_by_code(res).get("dq.copilot_store_schema") for res in results)
