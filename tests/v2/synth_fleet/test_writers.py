"""Written source files parse as their formats, carry the documented fields (SPEC §19.4, §4.2),
contain CANARY exactly where content lives (transcripts, headless streams) and nowhere else, and
reproduce the canonical records when read back with small independent readers written here from
the documented rules (the real adapters are exercised by the gate test)."""

from __future__ import annotations

import csv
import json
import re
import stat
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY
from tokenbill.core.ids import request_id_for
from tokenbill.core.money import cents_to_nano, usd_str_to_nano
from tokenbill.core.records import (
    EXTRA_KEYS,
    BlockRef,
    ContentFingerprint,
    Request,
    from_json,
    to_json,
)
from tokenbill.synth import fleet as F
from tokenbill.synth import truth as T
from tokenbill.synth import writers as W

HEX20 = re.compile(r"(?:h|p|c)_[0-9a-f]{20}\Z")


def _family(world: F.FleetWorld, prefix: str) -> list[Path]:
    return [p for k, p in world.source_files.items() if k.startswith(prefix)]


def test_every_family_written_and_keys_resolve(world: F.FleetWorld, fleet_dir: Path) -> None:
    families = Counter(k.split("/")[0] for k in world.source_files)
    assert set(families) == {"admin", "claude-code", "claude-code-headless", "cur", "otlp",
                             "trace2"}
    for key, path in world.source_files.items():
        assert path == fleet_dir / key and path.is_file() and path.stat().st_size > 0
    assert list(world.source_files) == sorted(world.source_files)
    for source in world.truth.sources:
        assert source.keys and set(source.keys) <= set(world.source_files), source.family
    assert {s.family for s in world.truth.sources} == {
        "claude-code", "claude-code-headless", "otlp", "trace@2", "anthropic-usage-report",
        "anthropic-cost-report", "anthropic-cc-analytics", "aws-cur"}
    assert world.truth.source("otlp").ttl_split is False


def test_canary_only_in_content_sources(world: F.FleetWorld) -> None:
    for key, path in world.source_files.items():
        data = path.read_bytes()
        if key.startswith(("claude-code/", "claude-code-headless/")):
            if key.endswith((".jsonl", ".json")):
                assert CANARY.encode() in data, key
        else:
            assert CANARY.encode() not in data, key
        assert CANARY not in key


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def test_cc_transcripts_parse_and_reproduce_totals(world: F.FleetWorld) -> None:
    paths = _family(world, "claude-code/")
    assert any("/subagents/agent-" in str(p) for p in paths)
    by_msg: dict[str, dict] = {}
    naive = 0
    subtypes: Counter = Counter()
    for path in paths:
        if path.name.endswith(".meta.json"):
            meta = json.loads(path.read_text())
            assert {"agentType", "model", "spawnDepth", "toolUseId"} <= set(meta)
            continue
        uuids = set()
        for line in _lines(path):
            assert {"uuid", "parentUuid", "sessionId", "timestamp", "type", "cwd",
                    "version", "gitBranch", "isSidechain"} <= set(line)
            assert line["uuid"] not in uuids
            uuids.add(line["uuid"])
            assert CANARY in line["cwd"] and CANARY in line["gitBranch"]
            if line["type"] == "system":
                subtypes[line["subtype"]] += 1
            if line["type"] != "assistant":
                continue
            msg = line["message"]
            assert msg["id"].startswith("msg_") and line["requestId"].startswith("req_")
            usage = msg["usage"]
            naive += usage["output_tokens"]
            prev = by_msg.get(msg["id"])
            if prev is None or usage["output_tokens"] >= prev["usage"]["output_tokens"]:
                by_msg[msg["id"]] = msg
    assert subtypes == {"compact_boundary": 1, "model_refusal_fallback": 2}
    got = T.TokenTotals()
    with_iterations = 0
    for msg in by_msg.values():
        u = msg["usage"]
        if "iterations" in u:
            with_iterations += 1
            parts = u["iterations"]
            assert [it["type"] for it in parts] == ["message", "fallback_message"]
        else:
            parts = [u]
        for it in parts:
            got = got + T.TokenTotals(it["input_tokens"], it["cache_read_input_tokens"],
                                      it["cache_creation"]["ephemeral_5m_input_tokens"],
                                      it["cache_creation"]["ephemeral_1h_input_tokens"], 0,
                                      it["output_tokens"])
    assert with_iterations == 2
    assert naive > got.output                   # split lines repeat usage: the naive-sum trap
    source = world.truth.source("claude-code")
    expected = dict(source.totals)["infra"]
    # + the importer's ESTIMATED compaction call (input read warm, output = summary)
    comp = [e for e in world.events if e.kind.value == "compaction"]
    pre = sum(dict(e.attrs)["pre_tokens"] for e in comp)
    post = sum(dict(e.attrs)["post_tokens"] for e in comp)
    assert got + T.TokenTotals(0, pre, 0, 0, 0, post) == expected
    # every canonical request of the sample appears under its message id
    sample = set(source.session_keys)
    for req in world.requests:
        if req.session_key in sample and req.final_attempt.provider_message_id:
            msg_id = req.final_attempt.provider_message_id
            assert msg_id in by_msg
            assert req.request_id == request_id_for("anthropic", msg_id, "", "")


def test_placeholder_call_is_written_as_message_start_only(world: F.FleetWorld) -> None:
    placeholder = [q for q in world.requests if q.serving_inference
                   and q.serving_inference.usage_source.value == "message_start_only"]
    assert len(placeholder) == 1
    msg_id = placeholder[0].final_attempt.provider_message_id
    lines = [ln for p in _family(world, "claude-code/") if p.suffix == ".jsonl"
             for ln in _lines(p)]
    idx = [i for i, ln in enumerate(lines) if ln.get("message", {}).get("id") == msg_id]
    entries = [lines[i]["message"] for i in idx]
    assert all(m["stop_reason"] is None and m["usage"]["output_tokens"] <= 20 for m in entries)
    tool_ids = {b["id"] for m in entries for b in m["content"] if b["type"] == "tool_use"}
    nxt = next(ln for ln in lines[idx[-1] + 1:] if ln["type"] != "assistant")
    assert nxt["type"] == "user"
    assert {b["tool_use_id"] for b in nxt["message"]["content"]} & tool_ids


def test_headless_execution_files(world: F.FleetWorld) -> None:
    paths = _family(world, "claude-code-headless/")
    assert paths and all(p.name == "claude-execution-output.json" for p in paths)
    got = T.TokenTotals()
    for path in paths:
        text = path.read_text("utf-8")
        assert '"@@dec' not in text
        msgs = json.loads(text, parse_float=Decimal)
        assert msgs[0]["type"] == "system" and msgs[0]["subtype"] == "init"
        result = msgs[-1]
        assert result["type"] == "result" and isinstance(result["total_cost_usd"], Decimal)
        steps: dict[str, dict] = {}
        for m in msgs:
            if m["type"] == "assistant":
                assert m["parent_tool_use_id"] is None and m["session_id"] == msgs[0]["session_id"]
                steps[m["message"]["id"]] = m["message"]["usage"]
        logged = sum(u["output_tokens"] for u in steps.values())
        assert all(u["output_tokens"] <= 20 for u in steps.values())   # placeholders
        (model, mu), = result["modelUsage"].items()
        assert mu["outputTokens"] >= logged
        assert mu["inputTokens"] == sum(u["input_tokens"] for u in steps.values())
        assert mu["costUSD"] == result["total_cost_usd"]
        for u in steps.values():
            got = got + T.TokenTotals(u["input_tokens"], u["cache_read_input_tokens"],
                                      u["cache_creation"]["ephemeral_5m_input_tokens"],
                                      u["cache_creation"]["ephemeral_1h_input_tokens"], 0,
                                      u["output_tokens"])
        got = got + T.TokenTotals(output=mu["outputTokens"] - logged)       # the residual
    assert got == dict(world.truth.source("claude-code-headless").totals)["ci-bots"]


def test_otlp_file(world: F.FleetWorld) -> None:
    (path,) = _family(world, "otlp/")
    got = T.TokenTotals()
    sessions = set()
    for line in _lines(path):
        (rl,) = line["resourceLogs"]
        res = {a["key"]: a["value"] for a in rl["resource"]["attributes"]}
        assert res["team.id"] == {"stringValue": "core"}
        for rec in rl["scopeLogs"][0]["logRecords"]:
            assert isinstance(rec["timeUnixNano"], str) and rec["timeUnixNano"].isdigit()
            assert rec["body"] == {"stringValue": "claude_code.api_request"}
            attrs = {a["key"]: a["value"] for a in rec["attributes"]}
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "duration_ms"):
                assert isinstance(attrs[k]["intValue"], str)
            assert "doubleValue" in attrs["cost_usd"]
            sessions.add(attrs["session.id"]["stringValue"])
            got = got + T.TokenTotals(int(attrs["input_tokens"]["intValue"]),
                                      int(attrs["cache_read_tokens"]["intValue"]), 0, 0,
                                      int(attrs["cache_creation_tokens"]["intValue"]),
                                      int(attrs["output_tokens"]["intValue"]))
    source = world.truth.source("otlp")
    assert len(sessions) == len(source.session_keys)
    assert got == dict(source.totals)["core"]


_TRACE_KEYS = {
    "header": {"rec", "schema", "trace_id", "profile", "name_key_id", "principal_key_id",
               "fp_key_id", "identity_mode", "producer", "created_ms", "attribution"},
    "session": {"rec", "schema", "session_key", "source_kind", "attribution", "started_ms",
                "ended_ms"},
    "lane": {"rec", "schema", "lane_key", "session_key", "kind", "parent_lane_key",
             "cache_scope_key", "ttl_observed", "lane_exact"},
    "blocks": {"rec", "schema", "key_id", "blocks"},
    "request": {"rec", "schema", "request_id", "session_key", "lane_key", "seq", "attribution",
                "params", "appended", "source", "fp", "attempts"},
    "event": {"rec", "schema", "lane_key", "ts_ms", "kind", "attrs"},
}
_ATTEMPT_KEYS = {"attempt_id", "attempt_no", "ts_start_ms", "ttft_ms", "duration_ms", "outcome",
                 "http_status", "error_type", "retry_layer", "retry_after_ms", "should_retry",
                 "provider_request_id", "provider_message_id", "model_served", "stop_reason",
                 "diagnostics", "applied_edits", "thinking_dropped", "sdk_retry_count",
                 "convention_id", "raw_usage", "inferences"}


def _walk(value: object) -> list[object]:
    out = [value]
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_walk(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_walk(v))
    return out


def test_trace_v2_fingerprint_file_round_trips(world: F.FleetWorld) -> None:
    (path,) = _family(world, "trace2/")
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text("utf-8").splitlines()
    records = [json.loads(line) for line in raw]
    for line, rec in zip(raw, records, strict=True):
        assert line == json.dumps(rec, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False)
        assert set(rec) == _TRACE_KEYS[rec["rec"]], rec["rec"]
        assert rec["schema"] == "tokenbill/trace@2"
        for v in _walk(rec):
            assert not isinstance(v, float)
            assert not isinstance(v, str) or len(v) <= 256
            if isinstance(v, int) and not isinstance(v, bool):
                assert 0 <= v <= 2**53
    header = records[0]
    assert header["rec"] == "header" and header["profile"] == "fingerprint"
    blocks: dict[str, BlockRef] = {}
    rebuilt: dict[str, Request] = {}
    lists: dict[str, tuple[BlockRef, ...]] = {}
    for rec in records[1:]:
        if rec["rec"] == "blocks":
            assert rec["key_id"] == header["fp_key_id"]
            for b in rec["blocks"]:
                blocks[b["h"]] = from_json(BlockRef, b)
        elif rec["rec"] == "request":
            for att in rec["attempts"]:
                assert set(att) == _ATTEMPT_KEYS
            fp = rec["fp"]
            parent = lists[fp["parent"]] if fp["parent"] else ()
            seq = parent[:fp["keep"]] + tuple(blocks[h] for h in fp["append"])
            lists[rec["request_id"]] = seq
            fingerprint = ContentFingerprint(key_id=header["fp_key_id"], blocks=seq,
                                             tier_end=tuple(fp["tier_end"]))
            assert [list(m) for m in fp["markers"]] == [[len(seq) - 1, "5m"]]
            attempts = []
            for att in rec["attempts"]:
                att = dict(att)
                raw_usage = att.pop("raw_usage")
                assert raw_usage["cache_creation"]["ephemeral_5m_input_tokens"] == \
                    att["inferences"][-1]["usage"]["cache_write_5m"]
                attempts.append({**att, "raw_usage_json": None})
            body = {k: v for k, v in rec.items() if k not in ("rec", "schema", "fp")}
            req = from_json(Request, {**body, "attempts": attempts,
                                      "fingerprint": to_json(fingerprint)})
            rebuilt[req.request_id] = req
    for rec in records:
        if rec["rec"] in ("request", "session"):
            attr = rec["attribution"]
            assert set(dict(attr.get("extra", []))) <= set(EXTRA_KEYS)
            for key in ("principal", "cwd_key", "repo", "api_key_id"):
                assert attr.get(key) is None or HEX20.match(attr[key])
    canonical = {q.request_id: q for q in world.requests if q.attribution.team == "agents"}
    assert set(rebuilt) == set(canonical)
    for rid, req in rebuilt.items():
        assert to_json(req) == to_json(canonical[rid]), rid


def test_admin_pages(world: F.FleetWorld) -> None:
    usage_page = json.loads((world.source_files["admin/usage_report_messages.json"]).read_text())
    assert usage_page["has_more"] is False and usage_page["next_page"] is None
    assert len(usage_page["data"]) == world.days
    got = T.TokenTotals()
    for bucket in usage_page["data"]:
        assert bucket["starting_at"].endswith("T00:00:00Z")
        for r in bucket["results"]:
            assert {"uncached_input_tokens", "cache_read_input_tokens", "cache_creation",
                    "output_tokens", "server_tool_use", "service_tier", "workspace_id",
                    "model", "inference_geo", "speed", "api_key_id", "context_window"} <= set(r)
            assert r["workspace_id"].startswith("wrkspc_01")
            got = got + T.TokenTotals(r["uncached_input_tokens"], r["cache_read_input_tokens"],
                                      r["cache_creation"]["ephemeral_5m_input_tokens"],
                                      r["cache_creation"]["ephemeral_1h_input_tokens"], 0,
                                      r["output_tokens"])
    assert got == dict(world.truth.source("anthropic-usage-report").totals)["all"]
    cost_page = json.loads(world.source_files["admin/cost_report.json"].read_text())
    total = 0
    token_types = set()
    for bucket in cost_page["data"]:
        for r in bucket["results"]:
            assert isinstance(r["amount"], str) and r["currency"] == "USD"
            assert r["cost_type"] == "tokens" and r["service_tier"] == "standard"
            token_types.add(r["token_type"])
            total += cents_to_nano(r["amount"])[0]
    assert token_types <= set(F.TOKEN_TYPES.values())
    assert total == world.truth.source("anthropic-cost-report").cost_nano
    analytics = T.TokenTotals()
    actors = set()
    for path in _family(world, "admin/claude_code/"):
        page = json.loads(path.read_text())
        for row in page["data"]:
            assert row["actor"]["type"] in ("user_actor", "api_actor")
            actors.add(json.dumps(row["actor"], sort_keys=True))
            assert {"num_sessions", "lines_of_code", "commits_by_claude_code",
                    "pull_requests_by_claude_code"} <= set(row["core_metrics"])
            for mb in row["model_breakdown"]:
                t = mb["tokens"]
                assert isinstance(mb["estimated_cost"]["amount"], int)
                analytics = analytics + T.TokenTotals(t["input"], t["cache_read"], 0, 0,
                                                      t["cache_creation"], t["output"])
    # per-user rows cover every Claude Code developer on the first-party API; the canonical
    # aggregates (k = 5 at ingest) may drop small groups, never add
    assert analytics.output >= dict(world.truth.source("anthropic-cc-analytics").totals)[
        "all"].output
    team_map = dict(world.truth.team_map)
    assert all(team_map[json.loads(a).get("email_address") or json.loads(a)["api_key_name"]]
               for a in actors)


def test_cur_csv(world: F.FleetWorld) -> None:
    path = world.source_files["cur/bedrock-cur2.csv"]
    rows = list(csv.DictReader(path.read_text().splitlines()))
    assert rows
    tokens = net = 0
    for r in rows:
        assert r["line_item_product_code"] == "AmazonBedrock"
        assert re.fullmatch(r"[A-Z0-9]+-MP:[A-Z0-9]+_[A-Za-z0-9]+-Units", r["line_item_usage_type"])
        assert r["pricing_unit"] == "1M tokens"
        unblended = Decimal(r["line_item_unblended_cost"])
        assert Decimal(r["line_item_net_unblended_cost"]) == unblended * Decimal("0.9")
        assert unblended == Decimal(r["line_item_usage_amount"]) * Decimal(
            r["line_item_unblended_rate"])
        tokens += int(Decimal(r["line_item_usage_amount"]) * 1_000_000)
        net += usd_str_to_nano(r["line_item_net_unblended_cost"])[0]
        assert r["line_item_iam_principal"] in dict(world.truth.team_map)
    source = world.truth.source("aws-cur")
    assert net == source.cost_nano
    ops = T.token_totals([q for q in world.requests if q.attribution.team == "ops"],
                         provider=True)
    assert tokens == sum(ops.as_tuple()) == dict(source.totals)["all"].uncached_input


def test_writers_accept_explicit_sessions(world: F.FleetWorld, tmp_path: Path) -> None:
    one = world.hints.transcript_sessions[:1]
    files = W.write_cc_transcripts(world, tmp_path, one)
    assert files and all(k.startswith("claude-code/") for k in files)
    assert W.write_headless_streams(world, tmp_path, ()) == {}
    again = W.write_all(world, tmp_path / "b")
    assert {k: p.read_bytes() for k, p in again.items()} == {
        k: p.read_bytes() for k, p in world.source_files.items()}


@pytest.mark.parametrize("value, expected", [
    (Decimal("0.123400"), "0.1234"), (Decimal("5"), "5"), (Decimal("0E-9"), "0")])
def test_decimal_literals(value: Decimal, expected: str) -> None:
    assert W._dumps({"x": W._dec_literal(value)}) == '{"x": ' + expected + "}"
