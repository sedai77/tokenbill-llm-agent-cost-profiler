"""Experimental ``session-store.db`` reader (flag ``copilot-store``): joins, conventions, schema
probe, locked databases and the never-open guarantee."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tokenbill.adapters import copilot_cli
from tokenbill.adapters.copilot_cli import CopilotCliAdapter, map_store_row, read_store
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.errors import SourceError, TokenbillError
from tokenbill.core.ids import copilot_lane_key, copilot_session_key, pseudonym, request_id_for
from tokenbill.core.records import InferenceKind
from tokenbill.core.testing import CONFORMANCE_NAME_KEY

from .helpers import (
    T0,
    Events,
    blob,
    iso,
    make_store,
    notes_by_code,
    opts,
    row,
    session_file,
    usage,
)

ADAPTER = CopilotCliAdapter()
FLAG = frozenset({"copilot-store"})
SID = "store-session-1"


def _home(tmp_path: Path, rows, *, drop=(), sessions=None, without_table=False) -> Path:
    ev = Events()
    ev.start(SID, selectedModel="claude-sonnet-4.5")
    ev.user()
    ev.message("c1", 111, turnId="0")                      # T0 + 3 s
    ev.message("c2", 222, turnId="1")                      # T0 + 4 s
    ev.message("c3", 333, model="gpt-5.4", agent="sub-1")  # T0 + 5 s (no row: residual)
    ev.compaction()                                        # T0 + 6 s
    ev.shutdown({"claude-sonnet-4.5": usage(20_000, 16_000, 2_000, 333)})
    ev.write(session_file(tmp_path, SID))
    if sessions is None:
        sessions = [{"id": SID, "cwd": f"/home/{CANARY}", "repository": f"acme/{CANARY}"}]
    make_store(tmp_path / "session-store.db", rows, drop=drop, sessions=sessions,
               without_table=without_table)
    return tmp_path


def _rows() -> list[dict]:
    return [
        row(1, SID, T0 + 3_500, turn=0),                                  # joins c1
        row(2, SID, T0 + 4_200, turn=1, out=222, nano=7_000_000),         # joins c2
        row(3, SID, T0 + 60_000, out=50),                                 # no message: own request
        row(4, SID, T0 + 61_000, inp=5, read=10, write=0),                # negative uncached
        row(5, SID, T0 + 6_500, model="claude-opus-4.7", initiator="compaction"),
        row(6, "other-session", T0 + 70_000, model="gpt-4o-mini", nano=0, out=12,
            inp=900, read=0, write=0),                                    # store-only, utility
    ]


def test_rows_joined_decomposed_and_residuals(tmp_path: Path) -> None:
    home = _home(tmp_path, _rows())
    result = ADAPTER.read(home, opts(experimental=FLAG))
    by_msg = {r.final_attempt.provider_message_id: r for r in result.requests}
    c1 = by_msg["c1"]
    inf = c1.final_attempt.inferences[0]
    assert c1.request_id == request_id_for("github", "c1", "", "")          # same id as events-only
    assert inf.kind is InferenceKind.MESSAGE
    # inclusive input 10,000 = 1,000 uncached + 8,000 read + 1,000 write
    assert (inf.usage.uncached_input, inf.usage.cache_read, inf.usage.cache_write_unknown,
            inf.usage.output) == (1_000, 8_000, 1_000, 120)
    assert inf.provider_reported_cost_nano == 50_000
    assert c1.final_attempt.convention_id == "github_copilot.session_store"
    assert c1.final_attempt.raw_usage_json is not None and c1.final_attempt.duration_ms == 900
    assert by_msg["c2"].final_attempt.inferences[0].usage.output == 222
    residual = by_msg["c3"].final_attempt.inferences[0]
    assert residual.kind is InferenceKind.OUTPUT_RESIDUAL and residual.usage.output == 333
    sk = copilot_session_key(SID)
    assert by_msg["c3"].lane_key == copilot_lane_key(sk, "subagent", "sub-1")
    lonely = [r for r in result.requests if r.final_attempt.provider_message_id is None
              and r.session_key == sk
              and r.final_attempt.inferences[0].kind is InferenceKind.MESSAGE]
    assert [r.final_attempt.inferences[0].usage.output for r in lonely] == [50]
    # the compaction row is covered by the exact events inference (no double count)
    comps = [r for r in result.requests
             if r.final_attempt.inferences[0].kind is InferenceKind.COMPACTION]
    assert len(comps) == 1 and comps[0].final_attempt.convention_id == \
        "github_copilot.token_details"
    assert result.stats["store_compaction_rows_covered"] == 1
    # negative uncached → quarantine and a convention note
    assert [(q.locator, q.reason) for q in result.quarantined] == [("store:row:4", "bad_usage")]
    notes = notes_by_code(result)
    assert notes["dq.convention_mismatch"] == 1
    # store rows supply per-request input: no rollup aggregate, usage_sequence present
    assert result.aggregates == []
    assert "usage_sequence" in result.capabilities
    assert_no_canary(blob(result))


def test_store_only_session_and_g12_utility(tmp_path: Path) -> None:
    home = _home(tmp_path, _rows(), sessions=[
        {"id": SID, "cwd": f"/home/{CANARY}", "repository": f"acme/{CANARY}"},
        {"id": "other-session", "cwd": "/tmp/x", "repository": "acme/other"}])
    result = ADAPTER.read(home, opts(experimental=FLAG))
    other = [r for r in result.requests if r.session_key == copilot_session_key("other-session")]
    (req,) = other
    inf = req.final_attempt.inferences[0]
    assert inf.billable is False and inf.billing_rule_id == "github.copilot.utility_unbilled"
    assert inf.provider_reported_cost_nano == 0
    assert req.attribution.repo == pseudonym(CONFORMANCE_NAME_KEY, "h", "acme/other")
    assert req.lane_key == copilot_lane_key(copilot_session_key("other-session"), "main", None)
    sessions = {s.session_key for s in result.sessions}
    assert copilot_session_key("other-session") in sessions
    assert_no_canary(blob(result))


def test_missing_optional_columns_dq_and_residual_outputs(tmp_path: Path) -> None:
    rows = [row(1, SID, T0 + 3_500, turn=0), row(2, SID, T0 + 9_000)]
    home = _home(tmp_path, rows, drop=("total_nano_aiu", "output_tokens"))
    result = ADAPTER.read(home, opts(experimental=FLAG))
    assert notes_by_code(result)["dq.copilot_store_schema"] == 1
    by_msg = {r.final_attempt.provider_message_id: r for r in result.requests}
    joined = by_msg["c1"].final_attempt
    assert joined.inferences[0].usage.output == 111            # output from the message
    assert joined.inferences[0].provider_reported_cost_nano is None
    assert joined.raw_usage_json is None                       # no single raw object
    kinds = {k: by_msg[k].final_attempt.inferences[0].kind for k in ("c2", "c3")}
    assert kinds == {"c2": InferenceKind.OUTPUT_RESIDUAL, "c3": InferenceKind.OUTPUT_RESIDUAL}
    lonely = [r for r in result.requests if r.final_attempt.provider_message_id is None
              and r.final_attempt.inferences[0].kind is InferenceKind.MESSAGE]
    assert [r.final_attempt.inferences[0].usage.output for r in lonely] == [0]


def test_missing_required_columns_or_table(tmp_path: Path) -> None:
    home = _home(tmp_path, [row(1, SID, T0 + 3_500)], drop=("cache_write_tokens",))
    result = ADAPTER.read(home, opts(experimental=FLAG))
    assert notes_by_code(result)["dq.copilot_store_schema"] == 1
    assert {r.final_attempt.inferences[0].kind for r in result.requests} >= {
        InferenceKind.MESSAGE}
    assert len(result.aggregates) == 1                 # events-only fallback keeps the rollup
    other = tmp_path / "other"
    home2 = _home(other, [], without_table=True)
    result2 = ADAPTER.read(home2, opts(experimental=FLAG))
    assert notes_by_code(result2)["dq.copilot_store_schema"] == 1


def test_locked_database_retried_then_skipped(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path, _rows())
    monkeypatch.setattr(copilot_cli, "STORE_TIMEOUT_S", 0)
    calls = []
    real = copilot_cli._store_connect

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(copilot_cli, "_store_connect", counting)
    writer = sqlite3.connect(home / "session-store.db")
    writer.execute("BEGIN EXCLUSIVE")
    try:
        result = ADAPTER.read(home, opts(experimental=FLAG))
    finally:
        writer.rollback()
        writer.close()
    assert len(calls) == 2
    assert result.stats["store_retries"] == 1
    assert notes_by_code(result)["dq.copilot_store_unavailable"] == 1
    assert result.aggregates                          # events-only fallback for this read
    with pytest.raises(SourceError):
        writer = sqlite3.connect(home / "session-store.db")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            ADAPTER.read(home, opts(experimental=FLAG, lenient=False))
        finally:
            writer.rollback()
            writer.close()


def test_corrupt_store_skipped(tmp_path: Path) -> None:
    home = _home(tmp_path, [])
    (home / "session-store.db").write_bytes(b"this is not a database " * 100)
    result = ADAPTER.read(home, opts(experimental=FLAG))
    assert notes_by_code(result)["dq.copilot_store_unavailable"] == 1


def test_without_flag_the_store_is_never_opened(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path, _rows())

    def refuse(*args, **kwargs):
        raise AssertionError("session-store.db opened without the copilot-store flag")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    real_open = open
    store = str(home / "session-store.db")

    def guarded_open(file, *args, **kwargs):
        assert str(file) != store, "session-store.db opened without the flag"
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    result = ADAPTER.read(home, opts())
    assert result.aggregates and "usage_sequence" not in result.capabilities
    assert all(r.final_attempt.convention_id is None for r in result.requests
               if r.final_attempt.inferences[0].kind is InferenceKind.MESSAGE)


def test_read_store_file_directly(tmp_path: Path) -> None:
    home = _home(tmp_path, _rows())
    result = ADAPTER.read(home / "session-store.db", opts(experimental=FLAG))
    assert result.requests and all(r.final_attempt.provider_message_id is None
                                   for r in result.requests)


def test_read_store_absent_and_after_id(tmp_path: Path) -> None:
    run = copilot_cli._Run(opts(experimental=FLAG), "s_x")
    assert read_store(tmp_path / "none.db", run).status == "absent"
    home = _home(tmp_path, _rows())
    read = read_store(home / "session-store.db", run, after_id=4)
    assert [r.row_id for r in read.rows] == [5, 6] and read.max_id == 6
    assert read.missing_optional == ()


def test_map_store_row_edge_cases() -> None:
    base = row(7, "s", T0, model="auto", reasoning=5)
    base["copilot_usage_model"] = "claude-haiku-4.5"
    mapped = map_store_row(base)
    assert mapped.model == "claude-haiku-4.5" and mapped.usage.output_reasoning == 5
    assert map_store_row(dict(base, created_at=T0 // 1000)).created_ms == T0
    assert map_store_row(dict(base, created_at=1.789898400e9)).created_ms == T0
    assert map_store_row(dict(base, total_nano_aiu=None)).nano_aiu is None
    assert map_store_row(dict(base, initiator="Sub Agent!")).initiator is None
    assert map_store_row(dict(base, created_at=iso(T0))).created_ms == T0
    for key, value, reason in (("id", "7", "bad_type:id"), ("session_id", None,
                                                            "bad_type:session_id"),
                               ("created_at", "soon", "bad_type:created_at"),
                               ("turn_index", -1, "bad_type:turn_index"),
                               ("total_nano_aiu", "x", "bad_type:total_nano_aiu")):
        with pytest.raises(SourceError, match=reason):
            map_store_row(dict(base, **{key: value}))
    with pytest.raises(SourceError, match="bad_type:model"):
        map_store_row(dict(base, model=None, copilot_usage_model=None))
    with pytest.raises(SourceError):
        map_store_row([1, 2])  # type: ignore[arg-type]
    with pytest.raises(TokenbillError):
        map_store_row(dict(base, input_tokens="12"))
