"""agent-traces.db extract: allowlist, SQL trace hook, canary, ``tokenbill_meta``, read-only source
(CP-VSCODE brief, Build 1 and 5; acceptance "Fixture databases …")."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path

import pytest

import tokenbill
from tokenbill.adapters import copilot_vscode_collect as cvc
from tokenbill.core import facts
from tokenbill.core.builders import CANARY, CANARY_LOGIN
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id, pseudonym

from .helpers import (
    NAME_KEY,
    collect,
    db_files,
    file_state,
    identity,
    mk,
    read_extract,
    traced,
)

FACT = facts.copilot_vscode_traces()
POSIX = os.name != "nt"


def _two_conversations(tmp_path: Path) -> Path:
    spans = (mk.conversation(1, "conv-a", mk.BASE_MS)
             + mk.conversation(100, "conv-b", mk.BASE_MS + 60_000))
    return mk.write_db(tmp_path / "agent-traces.db", spans)


def test_fixture_constants_match_core() -> None:
    assert mk.CANARY == CANARY
    assert mk.CANARY_LOGIN == CANARY_LOGIN


def test_extract_ddl_matches_the_primary_source() -> None:
    """The column types of ``_COLUMN_DDL`` are those of ``DDL.sql`` (``otelSqliteStore.ts``)."""
    ddl = (mk.DDL_PATH).read_text(encoding="utf-8")
    body = re.search(r"CREATE TABLE IF NOT EXISTS spans \((.*?)\n\);", ddl, re.S)
    assert body is not None
    parts = [p.strip() for p in body.group(1).replace("\n", " ").split(",") if p.strip()]
    source = {p.split()[0]: " ".join(p.split()[1:]) for p in parts}
    assert source == dict(cvc._COLUMN_DDL)
    assert set(FACT.span_columns) <= set(source)
    stmts = cvc.extract_ddl()
    assert stmts[-1] == cvc.VSCODE_META_TABLE_DDL
    assert not any("tool_" in s or "status_message" in s for s in stmts)


def test_extract_is_allowlisted_canary_free_and_traced(tmp_path: Path, monkeypatch) -> None:
    db = _two_conversations(tmp_path)
    before = file_state(db)
    with traced(monkeypatch) as statements:
        res, state = collect(tmp_path, dbs=[db])
    assert file_state(db) == before  # bytes and mtime unchanged
    [extract] = db_files(res)
    x = read_extract(extract)
    assert x["tables"] == ["span_attributes", "spans", "tokenbill_meta"]
    assert x["columns"] == list(FACT.span_columns)
    assert len(x["spans"]) == 10
    keys = {k for _, k, _ in x["attrs"]}
    assert keys <= set(FACT.attribute_allowlist)
    assert not keys & set(FACT.content_keys)
    blob = extract.read_bytes()
    assert CANARY.encode() not in blob and CANARY_LOGIN.encode() not in blob
    assert {s["name"] for s in x["spans"]} == {"invoke_agent", "other"}
    # every chat span keeps tokens, cache creation and nano-AIU
    chat = [s for s in x["spans"] if s["operation_name"] == "chat"]
    assert len(chat) == 4
    by_span: dict[str, dict[str, str]] = {}
    for sid, k, v in x["attrs"]:
        by_span.setdefault(sid, {})[k] = v
    for s in chat:
        a = by_span[s["span_id"]]
        assert a["gen_ai.usage.cache_creation.input_tokens"] == "100"
        assert a["copilot_chat.copilot_usage_nano_aiu"] == "23284800000"
        assert s["input_tokens"] == 1200 and s["cached_tokens"] == 800
    # SQL trace hook: span_events never touched, attributes only through the IN list
    assert statements
    assert not any("span_events" in s for s in statements)
    assert not any(re.search(r"\btool_(name|call_id|type)\b|status_message", s)
                   for s in statements)
    attr_queries = [s for s in statements
                    if "span_attributes" in s and not s.startswith("PRAGMA")]
    assert attr_queries
    for s in attr_queries:
        m = re.search(r"key IN \(([^)]*)\)", s)
        assert m is not None, s
        assert set(re.findall(r"'([^']*)'", m.group(1))) == set(FACT.attribute_allowlist)
    assert res.stats["spans_extracted"] == 10 and res.stats["files_written"] == 1
    assert state.databases


def test_meta_table_exact(tmp_path: Path) -> None:
    db = _two_conversations(tmp_path)
    res, _ = collect(tmp_path, dbs=[db])
    [extract] = db_files(res)
    meta = read_extract(extract)["meta"]
    ident = identity()
    assert meta == {
        "schema": "tokenbill/vscode-extract@1",
        "collector_version": tokenbill.__version__,
        "principal": ident.principal,
        "principal_key_id": ident.principal_key_id,
        "team": "platform",
        "name_key_id": key_id(NAME_KEY),
        "source_db": pseudonym(NAME_KEY, "h", os.path.abspath(db)),
        "window_start_ms": str(mk.BASE_MS),
        "window_end_ms": str(mk.BASE_MS + 61_960),
        "spans": "10",
        "dropped_synthesized": "0",
    }
    name = extract.name
    assert re.fullmatch(r"vscode-[0-9a-f]{12}-%d-%d\.db" % (mk.BASE_MS, mk.BASE_MS + 61_960),
                        name)


def test_meta_without_principal_and_team(tmp_path: Path) -> None:
    db = _two_conversations(tmp_path)
    res, _ = collect(tmp_path, dbs=[db], ident=identity(principal=None, team=None))
    meta = read_extract(db_files(res)[0])["meta"]
    assert "principal" not in meta and "principal_key_id" not in meta and "team" not in meta
    r_res, _ = collect(tmp_path, dbs=[db], out="out-r",
                       ident=cvc.CollectorIdentity("r_emp.4711", None, None, NAME_KEY,
                                                   key_id(NAME_KEY)))
    assert read_extract(db_files(r_res)[0])["meta"]["principal"] == "r_emp.4711"


def test_extract_head_is_sniffable_and_private(tmp_path: Path) -> None:
    db = _two_conversations(tmp_path)
    res, _ = collect(tmp_path, dbs=[db])
    [extract] = db_files(res)
    head = extract.read_bytes()[:4096]
    assert head.startswith(b"SQLite format 3\x00")
    assert b"span_attributes" in head and b"tokenbill_meta" in head
    assert not list(extract.parent.glob(".*.tmp"))
    assert not list(extract.parent.glob("*-journal"))
    if POSIX:
        assert stat.S_IMODE(os.stat(extract).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(extract.parent).st_mode) == 0o700


def test_extract_is_deterministic(tmp_path: Path) -> None:
    db = _two_conversations(tmp_path)
    a, _ = collect(tmp_path, dbs=[db], out="a")
    b, _ = collect(tmp_path, dbs=[db], out="b")
    [fa], [fb] = db_files(a), db_files(b)
    assert fa.name == fb.name
    assert fa.read_bytes() == fb.read_bytes()


def test_rollback_journal_database(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS), wal=False)
    before = file_state(db)
    res, _ = collect(tmp_path, dbs=[db])
    assert len(read_extract(db_files(res)[0])["spans"]) == 5
    assert file_state(db) == before


def test_wal_writer_is_never_blocked(tmp_path: Path) -> None:
    """VS Code keeps the database open (WAL) and may be mid-transaction: the collector reads the
    committed snapshot and the writer commits right after."""
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    writer = sqlite3.connect(str(db), isolation_level=None, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO spans (span_id, trace_id, name, start_time_ms, end_time_ms) "
                       "VALUES ('00000000000000ff', 'ab', 'chat', ?, ?)",
                       (mk.BASE_MS + 5, mk.BASE_MS + 6))
        res, _ = collect(tmp_path, dbs=[db])
        assert len(read_extract(db_files(res)[0])["spans"]) == 5  # uncommitted row not seen
        writer.execute("COMMIT")  # never blocked by the reader
    finally:
        writer.close()


def test_missing_empty_and_foreign_files(tmp_path: Path) -> None:
    empty = mk.write_db(tmp_path / "empty.db", [])
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all " * 300)
    folder = tmp_path / "folder.db"
    folder.mkdir()
    res, state = collect(tmp_path, dbs=[tmp_path / "missing.db", empty, junk, folder])
    assert res.files == ()
    assert [n.code for n in res.notes].count(cvc.DQ_NO_SPANS) == 3
    assert [n.code for n in res.notes].count(cvc.DQ_SCHEMA) == 1
    assert res.stats["databases_missing"] == 2 and res.stats["databases_skipped"] == 1
    assert all(n.severity in ("info", "warn") for n in res.notes)
    assert list(state.databases) == [pseudonym(NAME_KEY, "h", os.path.abspath(empty))]


def test_schema_without_required_column_is_skipped(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE spans (span_id TEXT PRIMARY KEY, trace_id TEXT, name TEXT, "
                 "start_time_ms INTEGER, end_time_ms INTEGER)")
    conn.execute("CREATE TABLE span_attributes (span_id TEXT, key TEXT, value TEXT)")
    conn.close()
    res, state = collect(tmp_path, dbs=[db])
    assert [n.code for n in res.notes] == [cvc.DQ_SCHEMA]
    assert "required column" in res.notes[0].detail
    assert res.files == () and state.databases == {}


def test_busy_database_is_retried(tmp_path: Path, monkeypatch) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS), wal=False)
    monkeypatch.setattr(cvc, "BUSY_TIMEOUT_S", 0)
    locker = sqlite3.connect(str(db), isolation_level=None)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        res, state = collect(tmp_path, dbs=[db])
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert [n.code for n in res.notes] == [cvc.DQ_BUSY]
    assert res.files == () and state.databases == {}
    res2, _ = collect(tmp_path, dbs=[db], state=state)
    assert len(db_files(res2)) == 1


def test_duplicate_source_paths_are_read_once(tmp_path: Path) -> None:
    db = _two_conversations(tmp_path)
    res, _ = collect(tmp_path, dbs=[db, tmp_path / "." / "agent-traces.db"])
    assert len(db_files(res)) == 1


def test_existing_extract_is_never_overwritten(tmp_path: Path) -> None:
    db = _two_conversations(tmp_path)
    res, _ = collect(tmp_path, dbs=[db])
    [first] = db_files(res)
    res2, _ = collect(tmp_path, dbs=[db])  # fresh state: same window, same name
    [second] = db_files(res2)
    assert second != first and second.name == first.name.replace(".db", "-2.db")
    assert first.exists()


def test_unwritable_output_directory(tmp_path: Path) -> None:
    db = _two_conversations(tmp_path)
    (tmp_path / "out").write_text("a file, not a directory")
    with pytest.raises(UsageError, match="output directory"):
        collect(tmp_path, dbs=[db])


def test_argument_validation(tmp_path: Path) -> None:
    state = cvc.VsCodeCollectorState()
    ident = identity()
    with pytest.raises(UsageError):
        cvc.collect_vscode_extracts("x", state, out_dir=tmp_path, identity=ident,  # type: ignore
                                    now_ms=0)
    with pytest.raises(UsageError):
        cvc.collect_vscode_extracts(cvc.VsCodeSources(), {}, out_dir=tmp_path,  # type: ignore
                                    identity=ident, now_ms=0)
    with pytest.raises(UsageError):
        cvc.collect_vscode_extracts(cvc.VsCodeSources(), state, out_dir=tmp_path,
                                    identity="me", now_ms=0)  # type: ignore[arg-type]
    for bad in (-1, True, "1"):
        with pytest.raises(UsageError):
            cvc.collect_vscode_extracts(cvc.VsCodeSources(), state, out_dir=tmp_path,
                                        identity=ident, now_ms=bad)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        cvc.VsCodeSources(traces_dbs="agent-traces.db")  # type: ignore[arg-type]
    res = cvc.collect_vscode_extracts(cvc.VsCodeSources(traces_dbs=[]), state, out_dir=tmp_path,
                                      identity=ident, now_ms=5)
    assert res.files == () and state.last_run_ms == 5


@pytest.mark.parametrize("kw", [
    {"principal": "p_" + "0" * 20},
    {"principal": "c_short", "principal_key_id": "k_1"},
    {"principal": "r_has@sign"},
    {"principal": "c_" + "a" * 20, "principal_key_id": None},
    {"principal_key_id": "bad key id"},
    {"team": ""},
    {"team": "tab\tteam"},
    {"team": "x" * 200},
    {"name_key": b""},
    {"name_key_id": ""},
])
def test_identity_validation(kw: dict) -> None:
    base = {"principal": None, "principal_key_id": None, "team": None, "name_key": NAME_KEY,
            "name_key_id": key_id(NAME_KEY)}
    base.update(kw)
    with pytest.raises(UsageError):
        cvc.CollectorIdentity(**base)


def test_identity_repr_hides_the_key() -> None:
    assert NAME_KEY.hex() not in repr(identity()) and repr(NAME_KEY) not in repr(identity())
