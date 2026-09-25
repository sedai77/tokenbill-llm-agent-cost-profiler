"""Privacy scan before publishing (CP-VSCODE brief, Build 5): canary / secret hits abort the file,
random identifiers do not; oversize values are never copied."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path

import pytest

from tokenbill.adapters import copilot_vscode_collect as cvc
from tokenbill.core.builders import CANARY, CANARY_LOGIN

from .helpers import codes, collect, db_files, mk, otel_files, read_extract

AWS = "AKIA" + "Z7QW3RT5YU8IOP2A"
ANTHROPIC = "sk-ant-api03-" + "Xy7Kq2Lm9Np4Rs6Tv8Wz" * 2
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
RANDOM_B64 = "Qm9vbXRvd24gcm9ja3MhIFRoaXMgaXMgcmFuZG9tIGRhdGEgZm9yIGVudHJvcHk"


@pytest.mark.parametrize(("key", "value"), [
    ("gen_ai.request.model", ANTHROPIC),
    ("gen_ai.response.model", AWS),
    ("copilot_chat.location", JWT),
    ("copilot_chat.api_type", CANARY),
    ("gen_ai.provider.name", CANARY_LOGIN),
    ("copilot_chat.endpoint_type", RANDOM_B64),  # high entropy in a non-identifier field
    ("gen_ai.response.id", ANTHROPIC),  # specific detectors apply to identifiers too
])
def test_hit_in_an_allowlisted_field_aborts_the_extract(tmp_path: Path, key: str,
                                                        value: str) -> None:
    span = mk.chat_span(1, mk.hex_id(1, 32), mk.BASE_MS, conv="c", canary=False)
    span.attributes[key] = value
    db = mk.write_db(tmp_path / "agent-traces.db", [span])
    res, state = collect(tmp_path, dbs=[db])
    assert res.files == () and codes(res) == [cvc.DQ_ABORTED]
    assert res.notes[0].severity == "error"
    assert value not in res.notes[0].detail and CANARY not in repr(res)
    assert state.databases == {}  # nothing advanced: retried next run
    assert list((tmp_path / "out").iterdir()) == []  # no temporary file left
    res2, _ = collect(tmp_path, dbs=[db], state=state)
    assert codes(res2) == [cvc.DQ_ABORTED]


def test_random_identifiers_do_not_abort(tmp_path: Path) -> None:
    rnd = random.Random(7)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    spans = []
    for n in range(1, 41):
        trace = hashlib.sha256(f"t{n}".encode()).hexdigest()[:32]
        conv = (f"{rnd.getrandbits(32):08x}-{rnd.getrandbits(16):04x}-4{rnd.getrandbits(12):03x}"
                f"-a{rnd.getrandbits(12):03x}-{rnd.getrandbits(48):012x}")
        resp = "chatcmpl-" + "".join(rnd.choice(alphabet) for _ in range(29))
        s = mk.chat_span(n, trace, mk.BASE_MS + n, conv=conv, response_id=resp, canary=False)
        s.span_id = hashlib.sha256(f"s{n}".encode()).hexdigest()[:16]
        spans.append(s)
    # a trace id holding every hex digit exactly twice has Shannon entropy 4.0 bits/char
    spans[0].trace_id = "0123456789abcdef" * 2
    db = mk.write_db(tmp_path / "agent-traces.db", spans)
    path = tmp_path / "otel.jsonl"
    path.write_bytes(b"".join(mk.outfile_lines(spans)))
    res, _ = collect(tmp_path, dbs=[db], outfiles=[path])
    assert res.notes == ()
    assert len(read_extract(db_files(res)[0])["spans"]) == 40
    assert len(otel_files(res)) == 1


def test_outfile_hit_aborts_and_keeps_the_cursor(tmp_path: Path) -> None:
    span = mk.chat_span(1, mk.hex_id(1, 32), mk.BASE_MS, conv="c", canary=False)
    span.attributes["gen_ai.request.model"] = ANTHROPIC
    path = tmp_path / "otel.jsonl"
    path.write_bytes(b"".join(mk.outfile_lines([span])))
    res, state = collect(tmp_path, outfiles=[path])
    assert res.files == () and codes(res) == [cvc.DQ_ABORTED]
    assert state.outfiles == {} and state.otel_seq == 0
    assert list((tmp_path / "out").iterdir()) == []


def test_scan_rejects_canary_bytes_anywhere(tmp_path: Path) -> None:
    for needle in (CANARY, CANARY_LOGIN, f"canary.{CANARY}@example.com"):
        f = tmp_path / "blob"
        f.write_bytes(b"\x00\x01" + needle.encode() + b"\x02")
        with pytest.raises(cvc._Abort):
            cvc._scan_file(f, ())
    f.write_bytes(b"\x00" + ANTHROPIC.encode() + b"\x00ok")
    with pytest.raises(cvc._Abort):
        cvc._scan_file(f, ())
    f.write_bytes(b"\x00" + b"eyJ" + b"just-an-id\x00" + b"sk-short\x00AKIA\x00")
    cvc._scan_file(f, ["claude-sonnet-4.5", "panel"])
    with pytest.raises(cvc._Abort):
        cvc._scan_file(f, [RANDOM_B64])
    with pytest.raises(cvc._Abort):
        cvc._scan_file(tmp_path / "missing", ())


def test_oversize_values_are_never_copied(tmp_path: Path) -> None:
    long_model = mk.chat_span(1, mk.hex_id(1, 32), mk.BASE_MS, conv="c", canary=False,
                              model="m" * 300)
    long_attr = mk.chat_span(2, mk.hex_id(2, 32), mk.BASE_MS + 1, conv="c", canary=False)
    long_attr.attributes["copilot_chat.location"] = "x" * 300
    long_trace = mk.chat_span(3, "a" * 300, mk.BASE_MS + 2, conv="c", canary=False)
    db = mk.write_db(tmp_path / "agent-traces.db", [long_model, long_attr, long_trace])
    res, state = collect(tmp_path, dbs=[db])
    x = read_extract(db_files(res)[0])
    assert [s["span_id"] for s in x["spans"]] == [mk.hex_id(1), mk.hex_id(2)]
    assert x["spans"][0]["request_model"] is None
    assert not any(v == "x" * 300 for _, _, v in x["attrs"])
    # request/response model columns and attributes, the location attribute, the skipped span
    assert res.stats["oversize_values_dropped"] == 6
    assert x["meta"]["spans"] == "2"
    assert next(iter(state.databases.values())).mark == (mk.BASE_MS + 2, mk.hex_id(3))


def test_only_oversize_spans_write_no_file(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db",
                     [mk.chat_span(3, "a" * 300, mk.BASE_MS, conv="c", canary=False)])
    res, state = collect(tmp_path, dbs=[db])
    assert res.files == () and state.databases


def test_bad_rows_are_skipped(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db",
                     [mk.chat_span(1, mk.hex_id(1, 32), mk.BASE_MS, conv="c", canary=False)])
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("INSERT INTO spans (span_id, trace_id, name, start_time_ms, end_time_ms) "
                 "VALUES (?, ?, 'chat', 'yesterday', 0)", (mk.hex_id(9), mk.hex_id(9, 32)))
    conn.execute("INSERT INTO spans (span_id, trace_id, name, start_time_ms, end_time_ms) "
                 "VALUES (X'0102', ?, 'chat', ?, 0)", (mk.hex_id(8, 32), mk.BASE_MS + 5))
    conn.close()
    res, _ = collect(tmp_path, dbs=[db])
    assert res.stats["bad_rows"] == 2
    assert [s["span_id"] for s in read_extract(db_files(res)[0])["spans"]] == [mk.hex_id(1)]


def test_notes_and_stats_are_content_free(tmp_path: Path) -> None:
    db = mk.write_db(tmp_path / "agent-traces.db", mk.conversation(1, "c", mk.BASE_MS))
    path = tmp_path / "otel.jsonl"
    path.write_bytes(b"".join(mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS))))
    res, state = collect(tmp_path, dbs=[db, tmp_path / "missing.db"], outfiles=[path],
                         now_ms=mk.BASE_MS + 9 * 86_400_000)
    text = repr(res) + json.dumps(dict(res.stats)) + repr(state)
    assert CANARY not in text and CANARY_LOGIN not in text
    assert str(tmp_path) not in "".join(n.detail for n in res.notes)
    assert all(len(n.detail) <= 256 for n in res.notes)
