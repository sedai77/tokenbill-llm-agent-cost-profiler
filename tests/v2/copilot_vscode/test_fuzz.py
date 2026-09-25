"""Hypothesis fuzz of every parser this package owns (SPEC §21 #5: only ``TokenbillError`` may
escape): the outfile line parser, the agent-traces.db reader, the state loader, the path expander
and the identity validator. Planted canaries never reach an extract."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.adapters import copilot_vscode_collect as cvc
from tokenbill.common import TokenbillError
from tokenbill.core import facts
from tokenbill.core.builders import CANARY

from .helpers import NAME_KEY, collect, db_files, mk, otel_files, read_extract

FACT = facts.copilot_vscode_traces()
EXAMPLES = int(os.environ.get("TB_CP_VSCODE_FUZZ_EXAMPLES", "40"))
SETTINGS = settings(max_examples=EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])

ALLOW = list(FACT.attribute_allowlist)
CONTENT = list(FACT.content_keys) + list(FACT.identity_keys)
KEYS = st.sampled_from(ALLOW + CONTENT + ["x.other", "tokenbill.principal", ""])
scalars = st.one_of(st.none(), st.booleans(), st.integers(-2**70, 2**70),
                    st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=40),
                    st.sampled_from(["chat", "invoke_agent", "execute_tool", "0af7651916cd43dd",
                                     "c_" + "a" * 20]))
json_values = st.recursive(scalars, lambda c: st.lists(c, max_size=3)
                           | st.dictionaries(st.text(max_size=8), c, max_size=3), max_leaves=12)
hr = st.one_of(st.lists(st.integers(-5, 2**40), min_size=0, max_size=3), json_values)


def _planted(attrs: dict[str, Any]) -> dict[str, Any]:
    """Canary in every non-allowlisted string value (content keys, identity keys, others)."""
    return {k: (f"{v} {CANARY}" if isinstance(v, str) and k not in ALLOW else v)
            for k, v in attrs.items()}


span_objects = st.fixed_dictionaries(
    {"traceId": st.one_of(st.sampled_from(["0af7651916cd43dd8448eb211c80319c", "00ff"]),
                          st.text(max_size=5)),
     "spanId": st.one_of(st.sampled_from(["b7ad6b7169203331", "01"]), st.integers()),
     "name": st.one_of(st.sampled_from(["chat", "invoke_agent", f"chat {CANARY}"]),
                       json_values),
     "startTime": hr, "endTime": hr},
    optional={"attributes": st.dictionaries(KEYS, json_values, max_size=8).map(_planted),
              "parentSpanContext": st.one_of(json_values, st.fixed_dictionaries(
                  {"traceId": st.just("0af7651916cd43dd8448eb211c80319c"),
                   "spanId": st.text(max_size=18)})),
              "status": st.one_of(json_values, st.fixed_dictionaries(
                  {"code": st.integers(0, 2), "message": st.just(CANARY)})),
              "events": st.lists(st.fixed_dictionaries(
                  {"name": st.sampled_from(["github.copilot.session.truncation", "exception",
                                            CANARY]),
                   "time": hr,
                   "attributes": st.dictionaries(KEYS, scalars, max_size=3).map(_planted)}),
                  max_size=3),
              "resource": st.one_of(json_values, st.fixed_dictionaries({"attributes": st.dictionaries(
                  st.sampled_from(["service.name", "host.name", "user.name", "tokenbill.team"]),
                  st.just(f"x {CANARY}"), max_size=3)})),
              "instrumentationScope": json_values, "kind": json_values, "duration": hr,
              "ended": json_values, "traceFlags": json_values})

lines = st.one_of(
    st.binary(max_size=80),
    span_objects.map(lambda o: json.dumps(o).encode("utf-8")),
    json_values.map(lambda o: json.dumps(o).encode("utf-8")),
)


@SETTINGS
@given(st.lists(lines, max_size=12), st.booleans())
def test_fuzz_outfile_lines(raw_lines: list[bytes], terminated: bool) -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        path = tmp / "otel.jsonl"
        blob = b"\n".join(raw_lines) + (b"\n" if terminated else b"")
        path.write_bytes(blob)
        try:
            res, state = collect(tmp, outfiles=[path])
        except TokenbillError:
            return
        for f in otel_files(res):
            data = f.read_bytes()
            assert CANARY.encode() not in data
            for line in data.splitlines():
                rec = json.loads(line)
                assert set(rec["attributes"]) <= set(ALLOW)
                assert rec["resource"]["attributes"]["tokenbill.collector"] == cvc.COLLECTOR
        assert all(n.code.startswith("dq.") for n in res.notes)
        cur = next(iter(state.outfiles.values()), None)
        assert cur is None or cur.offset <= len(blob)


column_values = st.one_of(st.none(), st.integers(-2**63, 2**63 - 1), st.text(max_size=30),
                          st.binary(max_size=8), st.floats(allow_nan=False, allow_infinity=False))


@st.composite
def db_rows(draw: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows = []
    for n in range(draw(st.integers(0, 6))):
        cols = {c: draw(column_values) for c in FACT.span_columns}
        cols["span_id"] = draw(st.one_of(st.just(mk.hex_id(n + 1)), st.text(max_size=20)))
        cols["trace_id"] = draw(st.one_of(st.just(mk.hex_id(n % 2, 32)), st.text(max_size=5)))
        cols["name"] = draw(st.sampled_from(["chat", "invoke_agent", f"x {CANARY}", ""]))
        cols["start_time_ms"] = draw(st.one_of(st.integers(0, 2**45), st.text(max_size=3)))
        cols["end_time_ms"] = draw(st.integers(0, 2**45))
        cols["status_code"] = draw(st.integers(0, 2))
        attrs = _planted(draw(st.dictionaries(KEYS, st.one_of(st.text(max_size=30),
                                                              st.integers()), max_size=6)))
        rows.append((cols, attrs))
    return rows


@SETTINGS
@given(db_rows(), st.booleans())
def test_fuzz_database_reader(rows: list[tuple[dict[str, Any], dict[str, Any]]],
                              wal: bool) -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        db = tmp / "agent-traces.db"
        conn = mk.create_db(db, wal=wal)
        try:
            for cols, attrs in rows:
                names = list(cols) + ["status_message", "tool_name"]
                try:
                    conn.execute(f"INSERT INTO spans ({', '.join(names)}) VALUES "
                                 f"({', '.join('?' * len(names))})",
                                 [*cols.values(), CANARY, CANARY])
                    conn.executemany("INSERT OR REPLACE INTO span_attributes VALUES (?, ?, ?)",
                                     [(cols["span_id"], k, str(v)) for k, v in attrs.items()])
                    conn.execute("INSERT INTO span_events (span_id, name, timestamp_ms, "
                                 "attributes) VALUES (?, ?, 0, ?)", (cols["span_id"], CANARY,
                                                                     CANARY))
                except sqlite3.IntegrityError:
                    continue
        finally:
            conn.close()
        try:
            res, state = collect(tmp, dbs=[db])
            res2, _ = collect(tmp, dbs=[db], state=state)
        except TokenbillError:
            return
        for f in db_files(res) + db_files(res2):
            x = read_extract(f)
            assert {k for _, k, _ in x["attrs"]} <= set(ALLOW)
            assert x["columns"] == list(FACT.span_columns)
            assert CANARY.encode() not in f.read_bytes()
        ids1 = {s["span_id"] for f in db_files(res) for s in read_extract(f)["spans"]}
        ids2 = {s["span_id"] for f in db_files(res2) for s in read_extract(f)["spans"]}
        assert not ids1 & ids2  # nothing extracted twice


@SETTINGS
@given(json_values)
def test_fuzz_state_loader(doc: Any) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        payload = doc if not isinstance(doc, dict) else {"schema": cvc.STATE_SCHEMA, **doc}
        path.write_text(json.dumps(payload))
        st_ = cvc.VsCodeCollectorState.load(path)
        assert isinstance(st_, cvc.VsCodeCollectorState)
        st_.save(path)
        assert cvc.VsCodeCollectorState.load(path) == st_


cursor_docs = st.fixed_dictionaries({}, optional={
    "mark_start_ms": st.one_of(st.integers(), st.text(max_size=3), st.none()),
    "mark_span_id": st.one_of(st.text(max_size=4), st.integers(), st.none()),
    "head_start_ms": st.one_of(st.integers(), st.none()),
    "head_sha": st.one_of(st.text(max_size=4), st.none(), st.integers()),
    "recent": st.one_of(json_values, st.lists(st.lists(st.one_of(st.integers(), st.text(
        max_size=3)), max_size=3), max_size=3))})


@SETTINGS
@given(st.dictionaries(st.text(max_size=5), cursor_docs, max_size=3),
       st.dictionaries(st.text(max_size=5), st.dictionaries(
           st.sampled_from(["offset", "head_sha", "head_len"]), json_values), max_size=3))
def test_fuzz_state_cursors(dbs: dict, outs: dict) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        path.write_text(json.dumps({"schema": cvc.STATE_SCHEMA, "databases": dbs,
                                    "outfiles": outs}))
        state = cvc.VsCodeCollectorState.load(path)
        for cur in state.databases.values():
            assert (cur.mark_start_ms is None) == (cur.mark_span_id is None)
        for out in state.outfiles.values():
            assert out.offset >= 0 and 0 <= out.head_len <= cvc.HEAD_SHA_BYTES


@SETTINGS
@given(st.sampled_from(["darwin", "linux", "win32", "cygwin", "sunos5", "MACOS", ""]),
       st.dictionaries(st.sampled_from(["APPDATA", "XDG_CONFIG_HOME", "TMPDIR", "TMP", "TEMP",
                                        "SystemRoot", "windir"]), st.text(max_size=12)),
       st.text(max_size=12))
def test_fuzz_vscode_paths(platform: str, env: dict, home: str) -> None:
    paths = cvc.vscode_paths(platform=platform, env=env, home=Path(home or "/h"))
    assert paths and all(isinstance(p, Path) for p in paths)
    assert str(paths[-1]).endswith(FACT.tmp_fallback_file)
    assert len({str(p) for p in paths}) == len(paths)


@SETTINGS
@given(st.one_of(st.none(), st.text(max_size=30), st.integers()),
       st.one_of(st.none(), st.text(max_size=20)), st.one_of(st.none(), st.text(max_size=30)))
def test_fuzz_identity(principal: Any, kid: Any, team: Any) -> None:
    try:
        ident = cvc.CollectorIdentity(principal, kid, team, NAME_KEY, "k_0123456789ab")
    except TokenbillError:
        return
    assert ident.principal is None or ident.principal[:2] in ("r_", "c_")
