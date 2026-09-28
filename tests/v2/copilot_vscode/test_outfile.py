"""OTel outfile tail (CP-VSCODE brief, Build 3; acceptance "Outfile: …")."""

from __future__ import annotations

import gzip
import json
import os
import stat
from decimal import Decimal
from pathlib import Path

from tokenbill.adapters import copilot_vscode_collect as cvc
from tokenbill.core import facts, jsonl
from tokenbill.core.builders import CANARY, CANARY_LOGIN

from .helpers import C_PRINCIPAL, codes, collect, file_state, identity, mk, otel_files

FACT = facts.copilot_vscode_traces()
POSIX = os.name != "nt"


def _records(res: cvc.CollectResult) -> list[dict]:
    out = []
    for f in otel_files(res):
        for line in f.read_bytes().splitlines():
            rec = jsonl.parse_json_line(line, exact_numbers=True)
            assert rec is not None
            out.append(rec)
    return out


def _outfile(tmp_path: Path, *chunks: bytes, name: str = "copilot-otel.jsonl") -> Path:
    path = tmp_path / name
    path.write_bytes(b"".join(chunks))
    return path


def test_content_and_identity_removed_numbers_kept(tmp_path: Path) -> None:
    spans = mk.conversation(1, "conv-a", mk.BASE_MS)
    path = _outfile(tmp_path, *mk.outfile_lines(spans), mk.log_line(), mk.metric_line(),
                    b"{broken json\n", b"[1, 2]\n")
    before = file_state(path)
    res, state = collect(tmp_path, outfiles=[path])
    assert file_state(path) == before
    [out] = otel_files(res)
    blob = out.read_bytes()
    assert CANARY.encode() not in blob and CANARY_LOGIN.encode() not in blob
    recs = _records(res)
    assert len(recs) == 5
    for rec in recs:
        assert set(rec["attributes"]) <= set(FACT.attribute_allowlist)
        assert rec["name"] in ("chat", "invoke_agent", "execute_tool", "other")
        assert "links" not in rec and "traceState" not in rec
        assert set(rec["status"]) == {"code"}
        res_attrs = rec["resource"]["attributes"]
        assert res_attrs["tokenbill.collector"] == "copilot-vscode-collect@1"
        assert res_attrs["tokenbill.principal"] == C_PRINCIPAL
        assert res_attrs["tokenbill.principal_key_id"] == identity().principal_key_id
        assert res_attrs["tokenbill.team"] == "platform"
        assert res_attrs["service.name"] == "copilot-chat"
        assert "host.name" not in res_attrs and "process.executable.path" not in res_attrs
        assert rec["instrumentationScope"] == {"name": "copilot-chat", "version": "0.68.0"}
    chat = [r for r in recs if r["attributes"].get("gen_ai.operation.name") == "chat"]
    assert len(chat) == 2
    a = chat[0]["attributes"]
    assert a["gen_ai.usage.input_tokens"] == 1200 and a["gen_ai.usage.output_tokens"] == 300
    assert a["gen_ai.usage.cache_read.input_tokens"] == 800
    assert a["gen_ai.usage.cache_creation.input_tokens"] == 100
    assert a["copilot_chat.copilot_usage_nano_aiu"] == 23_284_800_000
    assert chat[0]["startTime"] == [mk.BASE_MS // 1000, 10_000_000]
    assert chat[0]["parentSpanContext"]["spanId"] == mk.hex_id(1)
    root = next(r for r in recs if r["name"] == "invoke_agent")
    assert root["events"] == [{"name": "github.copilot.session.compaction_start",
                               "time": [mk.BASE_MS // 1000, 7_000_000], "attributes": {}}]
    s = res.stats
    assert s["otel_lines"] == 9 and s["otel_non_span_lines"] == 2
    assert s["otel_invalid_lines"] == 2 and s["otel_spans_extracted"] == 5
    assert s["content_attributes_removed"] > 0 and s["identity_attributes_removed"] > 0
    assert state.otel_seq == 1
    assert out.name.startswith("vscode-otel-") and out.name.endswith("-1.jsonl")
    if POSIX:
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    # canonical: sorted keys, no spaces
    for line in blob.splitlines():
        assert line == json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False).encode("utf-8")


def test_partial_last_line_waits_for_the_next_run(tmp_path: Path) -> None:
    lines = mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS, chats=1))
    third = lines[2]
    path = _outfile(tmp_path, lines[0], lines[1], third[:40])
    res1, state = collect(tmp_path, outfiles=[path])
    assert len(_records(res1)) == 2
    cur = next(iter(state.outfiles.values()))
    assert cur.offset == len(lines[0]) + len(lines[1])
    with open(path, "ab") as f:
        f.write(third[40:])
    res2, state = collect(tmp_path, outfiles=[path], state=state)
    assert [r["spanId"] for r in _records(res2)] == [mk.hex_id(3)]
    res3, _ = collect(tmp_path, outfiles=[path], state=state)
    assert res3.files == ()


def test_crlf_and_blank_lines(tmp_path: Path) -> None:
    lines = [ln.replace(b"\n", b"\r\n") for ln in mk.outfile_lines(mk.conversation(1, "c",
                                                                                   mk.BASE_MS))]
    path = _outfile(tmp_path, lines[0], b"\r\n\n", *lines[1:])
    res, state = collect(tmp_path, outfiles=[path])
    assert len(_records(res)) == 5
    assert next(iter(state.outfiles.values())).offset == path.stat().st_size


def test_rotation_truncated_file_is_reread_from_zero(tmp_path: Path) -> None:
    lines = mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS))
    path = _outfile(tmp_path, *lines)
    _, state = collect(tmp_path, outfiles=[path])
    new = mk.outfile_lines(mk.conversation(70, "d", mk.BASE_MS + 5000, chats=1))
    path.write_bytes(new[0])  # truncated and rewritten (smaller than the cursor)
    res, state = collect(tmp_path, outfiles=[path], state=state)
    assert res.stats["outfile_rotations"] == 1
    assert [r["spanId"] for r in _records(res)] == [mk.hex_id(70)]


def test_rotation_same_size_different_head(tmp_path: Path) -> None:
    lines = mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS))
    path = _outfile(tmp_path, *lines)
    _, state = collect(tmp_path, outfiles=[path])
    other = mk.outfile_lines(mk.conversation(0x51, "c", mk.BASE_MS))  # same lengths, new ids
    path.write_bytes(b"".join(other) + other[0])
    res, _ = collect(tmp_path, outfiles=[path], state=state)
    assert res.stats["outfile_rotations"] == 1 and len(_records(res)) == 6


def test_growing_small_file_is_not_a_rotation(tmp_path: Path) -> None:
    """The head hash covers only the bytes that existed when the cursor was saved."""
    lines = mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS))
    path = _outfile(tmp_path, lines[0])
    assert len(lines[0]) < 4096
    _, state = collect(tmp_path, outfiles=[path])
    with open(path, "ab") as f:
        f.write(b"".join(lines[1:]))
    res, _ = collect(tmp_path, outfiles=[path], state=state)
    assert "outfile_rotations" not in res.stats and len(_records(res)) == 4


def test_exact_decimals_and_spoofed_collector_attributes(tmp_path: Path) -> None:
    span = mk.chat_span(1, mk.hex_id(1, 32), mk.BASE_MS, conv="c", canary=False)
    obj = mk.readable_span_json(span, extra_resource={"tokenbill.principal": "c_" + "f" * 20,
                                                      "tokenbill.collector": "forged"})
    obj["attributes"]["copilot_chat.copilot_usage_nano_aiu"] = "PLACEHOLDER"
    raw = json.dumps(obj).replace('"PLACEHOLDER"', "23284800000.25").encode() + b"\n"
    path = _outfile(tmp_path, raw)
    res, _ = collect(tmp_path, outfiles=[path], ident=identity(principal=None, team=None))
    [out] = otel_files(res)
    assert b'"copilot_chat.copilot_usage_nano_aiu":23284800000.25' in out.read_bytes()
    [rec] = _records(res)
    assert rec["attributes"]["copilot_chat.copilot_usage_nano_aiu"] == Decimal("23284800000.25")
    assert rec["resource"]["attributes"] == {
        "service.name": "copilot-chat", "service.version": "0.68.0",
        "telemetry.sdk.language": "nodejs", "telemetry.sdk.name": "opentelemetry",
        "telemetry.sdk.version": "2.0.1", "tokenbill.collector": "copilot-vscode-collect@1"}


def test_malformed_spans_are_counted(tmp_path: Path) -> None:
    good = mk.readable_span_json(mk.chat_span(1, mk.hex_id(1, 32), mk.BASE_MS, conv="c"))
    bad_ids = dict(good, traceId="not hex!")
    bad_time = dict(good, startTime=[1, 2, 3])
    odd = dict(good, parentSpanContext={"traceId": 5}, status="x", events=["x", {"name": 3}],
               attributes=["not", "a", "dict"], resource="x", instrumentationScope={"name": 1},
               kind="client", ended="yes", duration=None, traceFlags="1")
    odd["resource"] = {"attributes": {"service.name": 7}}
    path = _outfile(tmp_path, *[(json.dumps(o) + "\n").encode()
                                for o in (bad_ids, bad_time, odd)])
    res, _ = collect(tmp_path, outfiles=[path])
    assert res.stats["otel_invalid_spans"] == 2
    [rec] = _records(res)
    assert rec["attributes"] == {} and rec["status"] == {"code": 0} and rec["events"] == []
    assert "parentSpanContext" not in rec and "instrumentationScope" not in rec
    assert "kind" not in rec and "ended" not in rec and "duration" not in rec
    assert "traceFlags" not in rec
    assert rec["resource"]["attributes"]["tokenbill.collector"] == cvc.COLLECTOR


def test_oversize_lines_and_values(tmp_path: Path, monkeypatch) -> None:
    span = mk.chat_span(1, mk.hex_id(1, 32), mk.BASE_MS, conv="c", canary=False,
                        model="m" * 300)
    line = (json.dumps(mk.readable_span_json(span)) + "\n").encode()
    small = mk.outfile_lines([mk.chat_span(2, mk.hex_id(2, 32), mk.BASE_MS + 1, conv="c",
                                           canary=False)])[0]
    path = _outfile(tmp_path, line, small)
    res, _ = collect(tmp_path, outfiles=[path])
    recs = _records(res)
    assert "gen_ai.request.model" not in recs[0]["attributes"]  # > 256 bytes: never copied
    assert recs[1]["attributes"]["gen_ai.request.model"] == "claude-sonnet-4.5"
    monkeypatch.setattr(jsonl, "MAX_LINE_BYTES", len(small) + 10)
    res2, _ = collect(tmp_path, outfiles=[path], out="out2")
    assert res2.stats["otel_oversize_lines"] == 1 and len(_records(res2)) == 1


def test_missing_outfile_and_nothing_new(tmp_path: Path) -> None:
    res, state = collect(tmp_path, outfiles=[tmp_path / "none.jsonl"])
    assert codes(res) == [cvc.DQ_NO_SPANS] and res.files == ()
    empty = _outfile(tmp_path, b"")
    res2, state = collect(tmp_path, outfiles=[empty], state=state)
    assert res2.files == () and res2.notes == ()
    assert next(iter(state.outfiles.values())).offset == 0


def test_gzip_outfile_is_tailed_by_decompressed_offset(tmp_path: Path) -> None:
    lines = mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS))
    path = tmp_path / "otel.jsonl.gz"
    path.write_bytes(gzip.compress(b"".join(lines[:3]), mtime=0))
    res1, state = collect(tmp_path, outfiles=[path])
    assert len(_records(res1)) == 3
    with gzip.open(path, "ab") as f:  # a second gzip member
        f.write(b"".join(lines[3:]))
    res2, _ = collect(tmp_path, outfiles=[path], state=state)
    assert [r["spanId"] for r in _records(res2)] == [mk.hex_id(4), mk.hex_id(5)]


def test_unreadable_outfile_is_retried(tmp_path: Path) -> None:
    path = tmp_path / "otel.jsonl.gz"
    path.write_bytes(b"\x1f\x8b\x08\x00garbage-not-gzip")
    res, state = collect(tmp_path, outfiles=[path])
    assert codes(res) == [cvc.DQ_SCHEMA] and state.outfiles == {}


def test_outfile_sequence_numbers_increase(tmp_path: Path) -> None:
    lines = mk.outfile_lines(mk.conversation(1, "c", mk.BASE_MS))
    path = _outfile(tmp_path, lines[0])
    res1, state = collect(tmp_path, outfiles=[path])
    with open(path, "ab") as f:
        f.write(lines[1])
    res2, state = collect(tmp_path, outfiles=[path], state=state)
    assert otel_files(res1)[0].name.endswith("-1.jsonl")
    assert otel_files(res2)[0].name.endswith("-2.jsonl")
    assert state.otel_seq == 2
