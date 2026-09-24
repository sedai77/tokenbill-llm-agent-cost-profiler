"""Incremental collection (SPEC §5.3 "Incremental collection", §5.4; brief CC)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.adapters.cc_collect import (
    QUIESCENT_MS,
    RECENT_UUIDS,
    STATE_SCHEMA,
    CollectorState,
    FileCursor,
    collect_incremental,
    retention_check,
)
from tokenbill.core.builders import assert_no_canary
from tokenbill.core.records import to_json
from tokenbill.core.testing import MemoryStore

from .helpers import CC, bf, by_message, canonical, note, opts, set_mtime, store_dump

OPUS = "claude-opus-5-5"
NOW = bf.T0_MS + 3_600_000


def _file(tmp: Path) -> Path:
    d = tmp / "projects" / "-home-dev-inc"
    d.mkdir(parents=True, exist_ok=True)
    return d / "33333333-0000-4000-8000-000000000003.jsonl"


def _write(path: Path, data: bytes, mtime_ms: int) -> None:
    path.write_bytes(data)
    set_mtime(path, mtime_ms)


def _store(results: list) -> MemoryStore:
    store = MemoryStore(name_key_id=opts().name_key_id)
    for r in results:
        store.ingest(r)
    return store


def _one_shot(path: Path) -> MemoryStore:
    return _store([CC.read(path, opts())])


def _collect(root: Path, state: CollectorState, now: int) -> list:
    return list(collect_incremental(root, state, opts(), now_ms=now))


def _reappear_transcript() -> tuple[bytes, int]:
    """A session whose message id reappears (higher output) after a tool_result, with the cut
    point (bytes) after the first appearance."""
    t = bf.Tx("33333333-0000-4000-8000-000000000003")
    t.human("go")
    t.call("msg_r1", OPUS, inp=10, w5=4_000, outputs=(3, 250), stop="tool_use")
    t.tool_result("toolu_r1", "ok")
    t.human("more")
    cut = len(t.text().encode())
    t.assistant_line("msg_r1", OPUS, bf.usage(10, 0, 4_000, 0, 470),
                     {"type": "text", "text": "late"}, stop="end_turn")
    t.call("msg_r2", OPUS, inp=12, read=4_000, w5=300, outputs=(3, 80), stop="tool_use")
    t.tool_result("toolu_r2", "ok")
    t.call("msg_r3", OPUS, inp=3, read=4_300, outputs=(3,), stop=None,
           blocks=[{"type": "tool_use", "id": "toolu_r3", "name": "Bash", "input": {}}])
    t.tool_result("toolu_r3", "ok")
    t.call("msg_r4", OPUS, inp=3, read=4_300, outputs=(3, 90), stop="end_turn")
    return t.text().encode(), cut


def test_half_then_rest_equals_one_shot_including_a_reappearing_id(tmp_path: Path) -> None:
    data, cut = _reappear_transcript()
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, data[:cut], NOW)
    first = _collect(tmp_path, state, NOW)
    assert by_message(first[0])["msg_r1"].attempts[0].inferences[0].usage.output == 250
    _write(path, data, NOW + 1_000)
    second = _collect(tmp_path, state, NOW + 1_000)
    assert by_message(second[0])["msg_r1"].attempts[0].inferences[0].usage.output == 470
    assert "msg_r4" not in by_message(second[0])      # trailing: withheld until closed/quiescent
    third = _collect(tmp_path, state, NOW + 1_000 + QUIESCENT_MS)
    assert set(by_message(third[0])) == {"msg_r4"}
    assert store_dump(_store(first + second + third)) == store_dump(_one_shot(path))


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cuts=st.lists(st.integers(min_value=1, max_value=10**6), min_size=1, max_size=4))
def test_any_sequence_of_cuts_equals_one_shot(tmp_path_factory: pytest.TempPathFactory,
                                              cuts: list[int]) -> None:
    """Appending the alpha transcript in arbitrary pieces (mid-line cuts included), then letting
    it go quiescent, merges to exactly the one-shot import."""
    tmp = tmp_path_factory.mktemp("cuts")
    data = bf.alpha_main().text().encode()
    points = sorted({c % len(data) for c in cuts} - {0}) + [len(data)]
    path = _file(tmp)
    state = CollectorState()
    results: list = []
    clock = NOW
    for p in points:
        clock += 1_000
        _write(path, data[:p], clock)
        results += _collect(tmp, state, clock)
    results += _collect(tmp, state, clock + QUIESCENT_MS)
    assert store_dump(_store(results)) == store_dump(_one_shot(path))


def test_in_flight_last_group_is_reread_next_time(tmp_path: Path) -> None:
    t = bf.Tx("33333333-0000-4000-8000-000000000003")
    t.human("go")
    t.call("msg_a", OPUS, inp=5, w5=1_000, outputs=(3, 60), stop="tool_use")
    t.tool_result("toolu_a", "ok")
    t.assistant_line("msg_b", OPUS, bf.usage(5, 1_000, 200, 0, 3), {"type": "text",
                                                                     "text": "x"})
    partial = t.text().encode()
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, partial, NOW)
    [r1] = _collect(tmp_path, state, NOW)
    assert set(by_message(r1)) == {"msg_a"}                       # msg_b is in flight
    cur = next(iter(state.cursors.values()))
    last_line = partial.rstrip(b"\n").split(b"\n")[-1]
    assert cur.offset == len(partial) - len(last_line) - 1           # the start of msg_b
    t.assistant_line("msg_b", OPUS, bf.usage(5, 1_000, 200, 0, 500), {"type": "text",
                                                                       "text": "y"},
                     stop="end_turn")
    t.human("thanks")                                                  # closes msg_b
    _write(path, t.text().encode(), NOW + 1_000)
    [r2] = _collect(tmp_path, state, NOW + 1_000)
    assert by_message(r2)["msg_b"].attempts[0].inferences[0].usage.output == 500
    assert "msg_a" not in by_message(r2)
    assert next(iter(state.cursors.values())).offset == len(t.text().encode())


def test_quiescent_file_emits_its_in_flight_group(tmp_path: Path) -> None:
    t = bf.Tx("33333333-0000-4000-8000-000000000003")
    t.human("go")
    t.assistant_line("msg_dead", OPUS, bf.usage(5, out=3), {"type": "text", "text": "x"})
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, t.text().encode(), NOW)
    [r0] = _collect(tmp_path, state, NOW)                        # the prompt; msg_dead withheld
    assert r0.requests == [] and [e.kind.value for e in r0.events] == ["human_prompt"]
    assert _collect(tmp_path, state, NOW + 5_000) == []          # unchanged, not quiescent
    [r] = _collect(tmp_path, state, NOW + QUIESCENT_MS)           # quiescent: emitted as is
    assert set(by_message(r)) == {"msg_dead"}
    assert _collect(tmp_path, state, NOW + 2 * QUIESCENT_MS) == []   # fully consumed


def test_unterminated_last_line_waits_for_its_newline(tmp_path: Path) -> None:
    t = bf.Tx("33333333-0000-4000-8000-000000000003")
    t.human("go")
    t.call("msg_u", OPUS, inp=5, outputs=(3, 40), stop="end_turn")
    t.human("next")
    data = t.text().encode()
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, data[:-10], NOW)                                  # last line cut mid-write
    [r1] = _collect(tmp_path, state, NOW)
    assert r1.quarantined == [] and r1.requests == []              # msg_u not closed yet
    _write(path, data, NOW + 1_000)
    [r2] = _collect(tmp_path, state, NOW + 1_000)
    assert r2.quarantined == [] and set(by_message(r2)) == {"msg_u"}
    assert len([e for e in r1.events + r2.events if e.kind.value == "human_prompt"]) == 2


def test_head_hash_change_triggers_a_full_reread(tmp_path: Path) -> None:
    data = bf.alpha_main().text().encode()
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, data, NOW)
    [r1] = _collect(tmp_path, state, NOW + QUIESCENT_MS)
    assert len(r1.requests) == 8
    other = bf.beta_subscription().text().encode()
    _write(path, other + b"\n" * (len(data) - len(other)) if len(other) < len(data) else other,
           NOW + 1_000)
    [r2] = _collect(tmp_path, state, NOW + 1_000 + QUIESCENT_MS)
    assert set(by_message(r2)) == {"msg_31BetaAllowance31", "msg_32BetaOverage032",
                                   "msg_33BetaOverage033"}


def test_truncation_below_the_offset_triggers_a_full_reread(tmp_path: Path) -> None:
    data = bf.alpha_main().text().encode()
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, data, NOW)
    _collect(tmp_path, state, NOW)
    lines = data.split(b"\n")
    short = b"\n".join(lines[:6]) + b"\n"
    _write(path, short, NOW + 1_000)
    [r] = _collect(tmp_path, state, NOW + QUIESCENT_MS * 2)
    assert "msg_01AlphaSplit0001" in by_message(r)


def test_small_files_growing_are_not_mistaken_for_rotation(tmp_path: Path) -> None:
    t = bf.Tx("33333333-0000-4000-8000-000000000003")
    t.human("hi")
    t.call("msg_s1", OPUS, inp=5, outputs=(9,), stop="end_turn")
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, t.text().encode(), NOW)
    [r1] = _collect(tmp_path, state, NOW)
    assert len(t.text().encode()) < 4096 and r1.requests == []     # msg_s1 withheld (trailing)
    t.human("again")
    t.call("msg_s2", OPUS, inp=5, outputs=(9,), stop="end_turn")
    _write(path, t.text().encode(), NOW + 1_000)
    [r2] = _collect(tmp_path, state, NOW + 1_000)
    assert set(by_message(r2)) == {"msg_s1"}                       # resumed, not re-read
    assert len([e for e in r2.events if e.kind.value == "human_prompt"]) == 1


def test_duplicate_uuids_across_runs_use_the_recent_set(tmp_path: Path) -> None:
    t = bf.Tx("33333333-0000-4000-8000-000000000003")
    t.human("hi")
    t.call("msg_d1", OPUS, inp=5, outputs=(9,), stop="end_turn")
    path = _file(tmp_path)
    state = CollectorState()
    t.human("next")
    _write(path, t.text().encode(), NOW)
    _collect(tmp_path, state, NOW)
    t.duplicate_last()
    t.human("again")
    _write(path, t.text().encode(), NOW + 1_000)
    [r2] = _collect(tmp_path, state, NOW + 1_000)
    assert note(r2, "dq.duplicate_uuid_lines").count == 1
    cur = next(iter(state.cursors.values()))
    assert len(cur.recent_uuids) <= RECENT_UUIDS


def test_state_round_trips_through_a_private_file(tmp_path: Path) -> None:
    data = bf.alpha_main().text().encode()
    path = _file(tmp_path)
    state = CollectorState()
    _write(path, data[: len(data) // 2], NOW)
    first = _collect(tmp_path, state, NOW)
    state_file = tmp_path / "state" / "cc.json"
    state.save(state_file)
    if os.name != "nt":
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    raw = state_file.read_bytes()
    assert_no_canary(raw)
    assert b"/home/dev" not in raw and b"feature/" not in raw
    loaded = CollectorState.load(state_file)
    assert loaded == state
    _write(path, data, NOW + 1_000)
    second = _collect(tmp_path, loaded, NOW + 1_000 + QUIESCENT_MS)
    assert store_dump(_store(first + second)) == store_dump(_one_shot(path))


def test_load_tolerates_missing_corrupt_and_foreign_state(tmp_path: Path) -> None:
    assert CollectorState.load(tmp_path / "none.json") == CollectorState()
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert CollectorState.load(bad) == CollectorState()
    bad.write_text(json.dumps({"schema": "other"}))
    assert CollectorState.load(bad) == CollectorState()
    bad.write_text(json.dumps({"schema": STATE_SCHEMA, "cursors": {
        "s_a": {"path_hmac": "s_a", "offset": -1, "head_sha": "x", "size": 1, "mtime_ns": 1},
        "s_b": {"path_hmac": "s_b", "offset": 1, "head_sha": "x", "size": 1, "mtime_ns": 1,
                "last_trigger_ts_ms": None, "recent_uuids": ["u"]},
        "s_c": "junk"}, "contexts": {"s_b": {"lanes": {}}, "s_z": {}}}))
    loaded = CollectorState.load(bad)
    assert set(loaded.cursors) == {"s_b"} and set(loaded.contexts) == {"s_b"}
    assert FileCursor.from_json({"path_hmac": 1}) is None
    unreadable = tmp_path / "dir.json"
    unreadable.mkdir()
    assert CollectorState.load(unreadable) == CollectorState()


def test_outputs_are_content_free(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    state = CollectorState()
    results = list(collect_incremental(root / "projects", state, opts(),
                                       now_ms=NOW + 30 * 86_400_000))
    assert len(results) == 4
    for r in results:
        assert_no_canary(repr(r), canonical(r))
    for ctx in state.contexts.values():
        assert_no_canary(json.dumps(ctx))


def test_retention_note_goes_with_the_first_result(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    for p in (root / "projects").rglob("*.jsonl"):
        set_mtime(p, NOW - 40 * 86_400_000)
    results = list(collect_incremental(root / "projects", CollectorState(), opts(),
                                       now_ms=NOW))
    assert note(results[0], "dq.retention_warning") is not None
    assert all(note(r, "dq.retention_warning") is None for r in results[1:])
    assert retention_check(root / "projects", NOW).count == 4
    assert retention_check(root / "projects", 0) is None


def test_collector_results_match_the_adapter_records(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    results = list(collect_incremental(root / "projects", CollectorState(), opts(),
                                       now_ms=NOW + 30 * 86_400_000))
    for r in results:
        path = next(p for p in (root / "projects").rglob("*.jsonl")
                    if CC.read(p, opts()).source.source_id == r.source.source_id)
        one = CC.read(path, opts())
        assert [to_json(q) for q in r.requests] == [to_json(q) for q in one.requests]
        assert [to_json(e) for e in r.events] == [to_json(e) for e in one.events]


_STEPS = st.lists(st.sampled_from(
    ["human", "call", "call_nostop", "tool_result", "attachment", "api_error", "compact",
     "quota_over", "quota_ok", "version", "reappear", "fallback", "meta", "dup", "sidechain",
     "cost", "snapshot", "big_call"]), min_size=3, max_size=30)


def _random_session(steps: list[str]) -> bytes:
    """A random but schema-true session built from *steps*."""
    t = bf.Tx("55555555-0000-4000-8000-000000000005")
    n = 0
    last_call: str | None = None
    version = 270
    quota: dict | None = None
    for step in steps:
        n += 1
        extra = {"quotaLimits": quota} if quota else {}
        if step == "human":
            t.human(f"prompt {n}", origin=n % 2 == 0)
        elif step in ("call", "call_nostop"):
            last_call = f"msg_rnd{n:03d}"
            outs = (3, 40 + n) if step == "call" else (3,)
            t.call(last_call, OPUS, inp=n, read=100 * n, w5=10 * n, outputs=outs,
                   stop="tool_use" if step == "call" else None, line_extra=extra)
        elif step == "tool_result" and last_call:
            t.tool_result("toolu_" + last_call[4:], f"result {n}")
        elif step == "attachment":
            t.attachment("todo", [n])
        elif step == "api_error":
            t.system("api_error", error={"status": 529})
        elif step == "compact":
            t.system("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": n * 7,
                                                          "postTokens": n})
        elif step == "quota_over":
            quota = {"status": "allowed_warning", "isUsingOverage": True}
        elif step == "quota_ok":
            quota = {"status": "allowed", "isUsingOverage": False}
        elif step == "version":
            version += 1
            t.version = f"2.1.{version}"
        elif step == "reappear" and last_call:
            t.assistant_line(last_call, OPUS, bf.usage(1, 0, 0, 0, 900 + n), None,
                             stop="end_turn")
        elif step == "fallback":
            t.system("model_refusal_fallback", originalModel="claude-fable-5",
                     fallbackModel=OPUS)
        elif step == "meta":
            t.meta_user(f"meta {n}")
        elif step == "dup" and t.lines:
            t.duplicate_last()
        elif step == "sidechain":
            t.agent_id = None if t.agent_id else f"side{n}"
        elif step == "cost":
            t.add(t.base("cost-state", 10, totalCostUSD=n))
        elif step == "snapshot":
            t.add({"type": "file-history-snapshot", "messageId": "x", "snapshot": {}})
        elif step == "big_call":
            last_call = f"msg_rnd{n:03d}"
            t.call(last_call, OPUS, inp=n, w1=5 * n, outputs=(3, 90 + n, 400 + n),
                   stop="tool_use", line_extra=extra)
    return t.text().encode()


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(steps=_STEPS, cuts=st.lists(st.floats(0.0, 1.0), min_size=1, max_size=4))
def test_random_sessions_collected_in_pieces_equal_one_shot(
        tmp_path_factory: pytest.TempPathFactory, steps: list[str], cuts: list[float]) -> None:
    tmp = tmp_path_factory.mktemp("rnd")
    data = _random_session(steps)
    points = sorted({int(c * len(data)) for c in cuts} - {0}) + [len(data)]
    path = _file(tmp)
    sub = opts(billing_path="subscription")
    state = CollectorState()
    results: list = []
    clock = NOW
    for p in points:
        clock += 1_000
        _write(path, data[:p], clock)
        results += list(collect_incremental(tmp, state, sub, now_ms=clock))
    results += list(collect_incremental(tmp, state, sub, now_ms=clock + QUIESCENT_MS))
    one = CC.read(path, sub)
    assert store_dump(_store(results)) == store_dump(_store([one]))
    assert len(one.requests) == len({r for res in results for r in
                                     (q.request_id for q in res.requests)})


def test_final_flag_and_injected_clock_behind_the_file(tmp_path: Path) -> None:
    t = bf.Tx("33333333-0000-4000-8000-000000000003")
    t.human("go")
    t.call("msg_f", OPUS, inp=5, outputs=(3, 9), stop="end_turn")
    path = _file(tmp_path)
    _write(path, t.text().encode(), NOW)
    assert _collect(tmp_path, CollectorState(), NOW)[0].requests == []       # trailing: withheld
    [r] = list(collect_incremental(tmp_path, CollectorState(), opts(), now_ms=NOW, final=True))
    assert set(by_message(r)) == {"msg_f"}
    [r] = _collect(tmp_path, CollectorState(), NOW - 3_600_000)     # clock behind the file
    assert set(by_message(r)) == {"msg_f"}
