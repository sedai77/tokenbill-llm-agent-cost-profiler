"""Schema-true source files for the synthetic fleet (SPEC §18, §19.4; SYNTH-FLEET).

Each writer serialises canonical records of a :class:`~tokenbill.synth.fleet.FleetWorld` into the
format one adapter family reads, so the gate test can check that the real adapter reproduces the
canonical token totals:

* :func:`write_cc_transcripts` — a Claude Code ``~/.claude/projects`` tree (main and subagent
  files, split assistant lines, iterations, ``compact_boundary``, ``model_refusal_fallback``, a
  placeholder-usage call) for the infra sample; CANARY in every content field;
* :func:`write_headless_streams` — claude-code-action ``execution_file`` JSON arrays for CI runs
  (placeholder per-step outputs, ``result`` with ``modelUsage``); CANARY in content fields;
* :func:`write_otlp` — an OpenTelemetry Collector ``file`` exporter log (``claude_code.api_request``
  events, int64 as JSON strings) for the core team;
* :func:`write_trace_v2_fingerprint` — a ``tokenbill/trace@2`` ``fingerprint`` profile file (the
  recorder's output) for the agents team, canonical JSON, owner-only;
* :func:`write_admin_pages` — Admin ``usage_report/messages`` and ``cost_report`` pages and
  Claude Code Analytics day pages (shapes from the API reference, checked 2026-09-23);
* :func:`write_cur_csv` — an AWS CUR 2.0 CSV for the Bedrock team.

Only transcripts and headless streams carry content (and the canary); every other file is
content-free. Output bytes are a pure function of the world (no clocks, sorted where order is free).
Money never passes through ``float``: provider USD fields that are JSON numbers are spliced in as
exact decimal literals.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core.builders import CANARY
from tokenbill.core.ids import key_id, stable_id
from tokenbill.core.jsonl import write_jsonl
from tokenbill.core.records import (
    Attribution,
    InferenceKind,
    Lane,
    LaneEventKind,
    LaneKind,
    Request,
    UsageSource,
    to_json,
)

__all__ = [
    "write_admin_pages",
    "write_all",
    "write_cc_transcripts",
    "write_cur_csv",
    "write_headless_streams",
    "write_otlp",
    "write_trace_v2_fingerprint",
]

_DEC_RE = re.compile(r'"@@dec:(-?[0-9]+(?:\.[0-9]+)?)@@"')
_TOOLS = ("Read", "Bash", "Grep", "Edit", "Glob")


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def _iso(ts_ms: int) -> str:
    from tokenbill.synth.fleet import date_of

    ms = ts_ms % 1000
    sec = ts_ms // 1000
    day = date_of(ts_ms)
    rem = sec % 86_400
    return f"{day}T{rem // 3600:02d}:{rem % 3600 // 60:02d}:{rem % 60:02d}.{ms:03d}Z"


def _dec_literal(value: Decimal) -> str:
    """A marker that :func:`_dumps` turns into an exact JSON number literal."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"@@dec:{text or '0'}@@"


def _dumps(obj: Any, **kw: Any) -> str:
    return _DEC_RE.sub(r"\1", json.dumps(obj, ensure_ascii=False, **kw))


def _write(out_dir: Path, rel: str, text: str) -> tuple[str, Path]:
    path = out_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return rel, path


def _text(seed: str, n_bytes: int) -> str:
    """Synthetic text of exactly *n_bytes* UTF-8 bytes (≥ the canary), ending with the canary."""
    tail = f" {CANARY}"
    body_len = max(0, n_bytes - len(tail))
    base = f"synthetic {seed} "
    body = (base * (body_len // len(base) + 1))[:body_len]
    return body + tail


def _lanes_by_session(world: Any, sessions: Iterable[str]) -> dict[str, list[Lane]]:
    wanted = set(sessions)
    reqs = [q for q in world.requests if q.session_key in wanted]
    lane_keys = {q.lane_key for q in reqs}
    events = [e for e in world.events if e.lane_key in lane_keys]
    shells = [s for s in world.sessions if s.session_key in wanted]
    from tokenbill.core.lanes import group_lanes

    out: dict[str, list[Lane]] = {}
    for lane in group_lanes(reqs, events, shells):
        out.setdefault(lane.session_key, []).append(lane)
    return out


def _usage_obj(inf_usage: Any, output: int, *, speed: str | None = None,
               tier: str = "standard") -> dict[str, Any]:
    u = inf_usage
    obj: dict[str, Any] = {
        "input_tokens": u.uncached_input,
        "cache_creation_input_tokens": u.cache_write_5m + u.cache_write_1h,
        "cache_read_input_tokens": u.cache_read,
        "cache_creation": {"ephemeral_5m_input_tokens": u.cache_write_5m,
                           "ephemeral_1h_input_tokens": u.cache_write_1h},
        "output_tokens": output,
        "service_tier": tier,
    }
    if speed is not None:
        obj["speed"] = speed
    return obj


def _placeholder(request_id: str, output: int) -> int:
    """A deterministic streaming placeholder in [1, 20], never above the true output."""
    return min(output, 1 + int(stable_id("ph", request_id)[3:9], 16) % 20)


# ---------------------------------------------------------------------------------------------
# Claude Code transcripts
# ---------------------------------------------------------------------------------------------


def write_cc_transcripts(world: Any, out_dir: Path,
                         session_keys: Sequence[str] | None = None) -> dict[str, Path]:
    """A ``projects/<slug>/<sessionId>.jsonl`` tree (plus ``subagents/agent-<id>.jsonl`` and
    ``.meta.json``) for *session_keys* (default: the infra transcript sample). Keys are
    ``claude-code/projects/...``."""
    hints = world.hints
    sessions = tuple(session_keys) if session_keys is not None else hints.transcript_sessions
    files: dict[str, Path] = {}
    for sk, lanes in sorted(_lanes_by_session(world, sessions).items()):
        dev = hints.devs[hints.session_dev[sk]]
        native = hints.session_native[sk]
        cwd_clean = dev.cwd.replace(f" {CANARY}", "")
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd_clean)
        main = next((ln for ln in lanes if ln.kind is LaneKind.MAIN), None)
        for lane in lanes:
            if lane.kind is LaneKind.COMPACTION or not lane.requests:
                continue
            agent = hints.lane_agent.get(lane.lane_key, "main")
            lines = _transcript_lines(lane, native, dev, agent, main)
            if lane.kind is LaneKind.MAIN:
                rel = f"claude-code/projects/{slug}/{native}.jsonl"
            else:
                rel = f"claude-code/projects/{slug}/{native}/subagents/agent-{agent}.jsonl"
                meta = _session_meta(lane)
                k, p = _write(out_dir, rel[:-len(".jsonl")] + ".meta.json",
                              json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
                files[k] = p
            k, p = _write(out_dir, rel, "".join(_dumps(line) + "\n" for line in lines))
            files[k] = p
    return files


def _session_meta(lane: Lane) -> dict[str, Any]:
    meta = next((e for e in lane.events if e.kind is LaneEventKind.SESSION_META), None)
    attrs = dict(meta.attrs) if meta is not None else {}
    return {"agentType": attrs.get("agent_type") or "general-purpose",
            "description": f"Investigate the failing module {CANARY}",
            "model": attrs.get("model_alias") or "sonnet",
            "spawnDepth": attrs.get("spawn_depth") or 1,
            "toolUseId": "toolu_01" + stable_id("tu", lane.lane_key)[3:25]}


def _transcript_lines(lane: Lane, native: str, dev: Any, agent: str,
                      main: Lane | None) -> list[dict[str, Any]]:
    sidechain = lane.kind is not LaneKind.MAIN
    version = lane.requests[0].attribution.client_version or "2.1.270"
    effort = lane.requests[0].params.session_effort
    common = {"isSidechain": sidechain, "userType": "external", "cwd": dev.cwd,
              "sessionId": native, "version": version,
              "gitBranch": f"feature/{dev.ref} {CANARY}", "entrypoint": "cli"}
    if sidechain:
        common["agentId"] = agent
    lines: list[dict[str, Any]] = []
    parent: str | None = None

    def emit(entry: dict[str, Any], ts: int, key: str) -> None:
        nonlocal parent
        uid = stable_id("u", lane.lane_key, key)[2:]
        uid = f"{uid[:8]}-{uid[8:12]}-4{uid[13:16]}-8{uid[17:20]}-{uid[20:24]}00000000"
        line = {"parentUuid": parent, **common, **entry, "uuid": uid, "timestamp": _iso(ts)}
        lines.append(line)
        parent = uid

    humans = {e.ts_ms for e in lane.events if e.kind is LaneEventKind.HUMAN_PROMPT}
    fallbacks = {e.ts_ms: dict(e.attrs) for e in lane.events
                 if e.kind is LaneEventKind.MODEL_FALLBACK}
    compactions = sorted((e.ts_ms, dict(e.attrs)) for e in lane.events
                         if e.kind is LaneEventKind.COMPACTION)
    reqs = [q for q in lane.requests if q.serving_inference is not None]
    tool_ids: list[str] = []
    for i, req in enumerate(reqs):
        ts = req.ts_start_ms
        for c_ts, attrs in compactions:
            if (reqs[i - 1].ts_start_ms if i else -1) < c_ts <= ts:
                emit({"type": "system", "subtype": "compact_boundary",
                      "content": "Conversation compacted", "level": "info",
                      "compactMetadata": {"trigger": attrs["trigger"],
                                          "preTokens": attrs["pre_tokens"],
                                          "postTokens": attrs["post_tokens"],
                                          "durationMs": attrs["duration_ms"],
                                          "cumulativeDroppedTokens": attrs.get("dropped_tokens")}},
                     c_ts, f"cb{c_ts}")
                emit({"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
                      "message": {"role": "user",
                                  "content": _text(f"summary {i}", 600)}}, c_ts, f"cs{c_ts}")
        item = req.appended[0] if req.appended else None
        if i == 0 or ts in humans:
            n_bytes = item.n_bytes if item is not None and item.kind == "user_text" else 200
            entry: dict[str, Any] = {"type": "user", "message": {
                "role": "user", "content": _text(f"prompt {req.request_id}", n_bytes)}}
            if lane.kind is LaneKind.MAIN:
                entry["origin"] = {"kind": "human"}
            emit(entry, ts, f"t{i}")
        else:
            n_bytes = item.n_bytes if item is not None else 300
            answer = tool_ids[-1] if tool_ids else "toolu_01none"
            emit({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": answer,
                 "content": _text(f"result {req.request_id}", n_bytes), "is_error": False}]},
                "toolUseResult": {"stdout": _text(f"stdout {i}", 120), "stderr": "",
                                  "filePath": f"{dev.cwd}/src/module_{i}.py"}}, ts, f"t{i}")
        if ts in fallbacks:
            fb = fallbacks[ts]
            emit({"type": "system", "subtype": "model_refusal_fallback",
                  "originalModel": fb["from_model"], "fallbackModel": fb["to_model"],
                  "content": f"Switched model after a refusal {CANARY}", "level": "warning"},
                 ts, f"fb{i}")
        next_item = reqs[i + 1].appended[0] if i + 1 < len(reqs) and reqs[i + 1].appended \
            else None
        tool_name = next_item.name if next_item is not None and next_item.name in _TOOLS \
            else "Read"
        lines_for = _assistant_lines(req, tool_name, effort)
        att = req.final_attempt
        for j, (entry, tool_id) in enumerate(lines_for):
            dur = att.duration_ms or 1000
            emit(entry, ts + dur * (j + 1) // len(lines_for), f"a{i}.{j}")
            if tool_id:
                tool_ids.append(tool_id)
    return lines


def _assistant_lines(req: Request, tool_name: str, effort: str | None
                     ) -> list[tuple[dict[str, Any], str | None]]:
    att = req.final_attempt
    serving = req.serving_inference
    assert serving is not None
    u = serving.usage
    placeholder = serving.usage_source is UsageSource.MESSAGE_START_ONLY
    stop = att.stop_reason
    speed = "fast" if serving.pricing.speed == "fast" else None
    tier = serving.pricing.service_tier
    base = _usage_obj(u, u.output, speed=speed, tier=tier)
    declined = [inf for inf in att.inferences if inf.kind is InferenceKind.FALLBACK_DECLINED]
    if declined:
        d = declined[0]
        iterations = [
            {"type": "message", "model": d.pricing.model_raw,
             **{k: v for k, v in _usage_obj(d.usage, d.usage.output).items()
                if k != "service_tier"}},
            {"type": "fallback_message", "model": serving.pricing.model_raw,
             **{k: v for k, v in _usage_obj(u, u.output).items() if k != "service_tier"}},
        ]
        base["iterations"] = iterations
    tool_id = "toolu_01" + stable_id("tu", req.request_id)[3:25]
    blocks: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": f"Considering the next step {CANARY}",
         "signature": "sig_" + stable_id("sg", req.request_id)[3:27]},
        {"type": "text", "text": f"Working on it {CANARY}"},
    ]
    if stop in ("tool_use", None):
        blocks.append({"type": "tool_use", "id": tool_id, "name": tool_name,
                       "input": {"file_path": f"src/file_{req.seq}.py {CANARY}",
                                 "command": f"ls {CANARY}"}})
    out: list[tuple[dict[str, Any], str | None]] = []
    n = len(blocks)
    for j, block in enumerate(blocks):
        last = j == n - 1
        if placeholder:
            output = u.output
        else:
            output = u.output if last else min(u.output, 1 + j)
        usage = dict(base)
        usage["output_tokens"] = output
        if "iterations" in usage:
            its = [dict(it) for it in usage["iterations"]]
            its[-1]["output_tokens"] = output
            usage["iterations"] = its
        message = {"id": att.provider_message_id, "type": "message", "role": "assistant",
                   "model": serving.pricing.model_raw, "content": [block],
                   "stop_reason": stop if last else None, "stop_sequence": None,
                   "usage": usage}
        entry: dict[str, Any] = {"type": "assistant", "message": message,
                                 "requestId": att.provider_request_id}
        if effort is not None:
            entry["effort"] = effort
        out.append((entry, tool_id if block["type"] == "tool_use" else None))
    return out


# ---------------------------------------------------------------------------------------------
# claude-code-action execution files
# ---------------------------------------------------------------------------------------------


def write_headless_streams(world: Any, out_dir: Path,
                           session_keys: Sequence[str] | None = None) -> dict[str, Path]:
    """One claude-code-action ``execution_file`` (a JSON array of SDK messages) per CI run in
    *session_keys* (default: the ci-bots runs of the first three days). Per-step
    ``output_tokens`` are streaming placeholders; the ``result`` message's ``modelUsage`` carries
    the exact totals. Keys are ``claude-code-headless/<run>/claude-execution-output.json``."""
    from tokenbill.synth.truth import Coster

    hints = world.hints
    coster = Coster()
    sessions = tuple(session_keys) if session_keys is not None else hints.headless_sessions
    files: dict[str, Path] = {}
    for sk, lanes in sorted(_lanes_by_session(world, sessions).items()):
        native = hints.session_native[sk]
        dev = hints.devs[hints.session_dev[sk]]
        lane = lanes[0]
        reqs = [q for q in lane.requests if q.serving_inference is not None]
        model = reqs[0].model
        version = reqs[0].attribution.client_version or "2.1.270"
        msgs: list[dict[str, Any]] = [{
            "type": "system", "subtype": "init", "session_id": native, "cwd": dev.cwd,
            "model": model, "tools": list(_TOOLS), "claude_code_version": version,
            "permissionMode": "default", "apiKeySource": "ANTHROPIC_API_KEY"}]
        totals = [0, 0, 0, 0]   # uncached, read, writes, output
        cost = 0
        prev_tool = None
        for i, req in enumerate(reqs):
            inf = req.serving_inference
            assert inf is not None
            u = inf.usage
            if i == 0:
                content: Any = [{"type": "text", "text": _text(f"task {native}", 400)}]
            else:
                content = [{"type": "tool_result", "tool_use_id": prev_tool,
                            "content": _text(f"output {req.request_id}", 500)}]
            msgs.append({"type": "user", "message": {"role": "user", "content": content},
                         "parent_tool_use_id": None, "session_id": native,
                         "timestamp": _iso(req.ts_start_ms)})
            tool_id = "toolu_01" + stable_id("tu", req.request_id)[3:25]
            att = req.final_attempt
            blocks: list[dict[str, Any]] = [{"type": "text", "text": f"Reviewing {CANARY}"}]
            if att.stop_reason != "end_turn":
                blocks.append({"type": "tool_use", "id": tool_id, "name": "Bash",
                               "input": {"command": f"git diff {CANARY}"}})
            msgs.append({"type": "assistant", "message": {
                "id": att.provider_message_id, "type": "message", "role": "assistant",
                "model": inf.pricing.model_raw, "content": blocks,
                "stop_reason": att.stop_reason, "stop_sequence": None,
                "usage": _usage_obj(u, _placeholder(req.request_id, u.output))},
                "parent_tool_use_id": None, "session_id": native,
                "timestamp": _iso(req.ts_start_ms + (att.duration_ms or 1000))})
            prev_tool = tool_id
            totals[0] += u.uncached_input
            totals[1] += u.cache_read
            totals[2] += u.cache_write
            totals[3] += u.output
            cost += coster.inference(inf, req.ts_start_ms) or 0
        end = reqs[-1].ts_start_ms + (reqs[-1].final_attempt.duration_ms or 1000)
        usd = _dec_literal(Decimal(cost).scaleb(-9))
        msgs.append({
            "type": "result", "subtype": "success", "is_error": False,
            "duration_ms": end - reqs[0].ts_start_ms, "duration_api_ms": sum(
                q.final_attempt.duration_ms or 0 for q in reqs),
            "num_turns": len(reqs), "result": f"Review posted {CANARY}", "session_id": native,
            "total_cost_usd": usd,
            "usage": {"input_tokens": totals[0], "cache_creation_input_tokens": totals[2],
                      "cache_read_input_tokens": totals[1], "output_tokens": totals[3],
                      "server_tool_use": {"web_search_requests": 0},
                      "service_tier": "standard"},
            "modelUsage": {model: {"inputTokens": totals[0], "outputTokens": totals[3],
                                   "cacheReadInputTokens": totals[1],
                                   "cacheCreationInputTokens": totals[2],
                                   "webSearchRequests": 0, "costUSD": usd}}})
        repo, workflow = hints.ci_runs[sk]
        run = stable_id("run", sk)[4:16]
        slug = re.sub(r"[^A-Za-z0-9]", "-", repo)
        rel = f"claude-code-headless/{slug}/{workflow}/{run}/claude-execution-output.json"
        k, p = _write(out_dir, rel, _dumps(msgs, indent=2) + "\n")
        files[k] = p
    return files


# ---------------------------------------------------------------------------------------------
# OTLP/JSON (Claude Code OpenTelemetry events)
# ---------------------------------------------------------------------------------------------


def _kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, str) and value.startswith("@@dec:"):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def write_otlp(world: Any, out_dir: Path, team: str = "core") -> dict[str, Path]:
    """An OpenTelemetry Collector ``file`` exporter log (one ``ExportLogsServiceRequest`` per line,
    one line per session) with a ``claude_code.api_request`` event per request of *team*. Key:
    ``otlp/<team>-claude-code-logs.jsonl``."""
    from tokenbill.synth.truth import Coster

    hints = world.hints
    coster = Coster()
    sessions = sorted(sk for sk, t in hints.session_team.items() if t == team)
    lines = []
    for sk, lanes in sorted(_lanes_by_session(world, sessions).items()):
        native = hints.session_native[sk]
        dev = hints.devs[hints.session_dev[sk]]
        records = []
        for lane in lanes:
            for req in lane.requests:
                inf = req.serving_inference
                if inf is None:
                    continue
                att = req.final_attempt
                u = inf.usage
                end_ms = req.ts_start_ms + (att.duration_ms or 0)
                cost = coster.inference(inf, req.ts_start_ms) or 0
                attrs = [
                    _kv("event.name", "api_request"), _kv("event.timestamp", _iso(end_ms)),
                    _kv("session.id", native), _kv("organization.id", "fleet-example-org"),
                    _kv("user.id", dev.ref), _kv("terminal.type", "vscode"),
                    _kv("model", inf.pricing.model_raw),
                    _kv("request_id", att.provider_request_id or ""),
                    _kv("duration_ms", att.duration_ms or 0),
                    _kv("input_tokens", u.uncached_input), _kv("output_tokens", u.output),
                    _kv("cache_read_tokens", u.cache_read),
                    _kv("cache_creation_tokens", u.cache_write),
                    _kv("cost_usd", _dec_literal(Decimal(cost).scaleb(-9))),
                    _kv("speed", inf.pricing.speed), _kv("effort", req.params.effort or ""),
                    _kv("query_source", "main" if lane.kind is LaneKind.MAIN else "subagent"),
                ]
                records.append({"timeUnixNano": str(end_ms * 1_000_000),
                                "observedTimeUnixNano": str(end_ms * 1_000_000),
                                "severityNumber": 9, "severityText": "INFO",
                                "body": {"stringValue": "claude_code.api_request"},
                                "attributes": attrs})
        records.sort(key=lambda r: (int(r["timeUnixNano"]), json.dumps(r["attributes"])))
        resource = [_kv("service.name", "claude-code"), _kv("service.version", "2.1.270"),
                    _kv("team.id", team), _kv("cost_center", f"cc-{team}"),
                    _kv("os.type", "darwin"), _kv("host.arch", "arm64")]
        lines.append({"resourceLogs": [{"resource": {"attributes": resource}, "scopeLogs": [{
            "scope": {"name": "com.anthropic.claude_code.events", "version": "2.1.270"},
            "logRecords": records}]}]})
    rel = f"otlp/{team}-claude-code-logs.jsonl"
    k, p = _write(out_dir, rel, "".join(_dumps(line, separators=(",", ":")) + "\n"
                                        for line in lines))
    return {k: p}


# ---------------------------------------------------------------------------------------------
# trace@2 fingerprint profile (recorder output)
# ---------------------------------------------------------------------------------------------

_SCHEMA = "tokenbill/trace@2"


def _raw_usage(u: Any) -> dict[str, Any]:
    return _usage_obj(u, u.output)


def write_trace_v2_fingerprint(world: Any, out_dir: Path, team: str = "agents"
                               ) -> dict[str, Path]:
    """The recorder's ``tokenbill/trace@2`` file (profile ``fingerprint``) for *team*: header,
    sessions, lanes, ``blocks`` records before first use and requests with delta-encoded
    fingerprints (SPEC §4.2); canonical JSON, owner-only. Key: ``trace2/<team>-recorder.jsonl``."""
    from tokenbill.synth.fleet import FLEET_FP_KEY, FLEET_NAME_KEY, FLEET_ORG_KEY

    hints = world.hints
    sessions = sorted(sk for sk, t in hints.session_team.items() if t == team)
    by_session = _lanes_by_session(world, sessions)
    shells = {s.session_key: s for s in world.sessions if s.session_key in by_session}
    header = {"rec": "header", "schema": _SCHEMA,
              "trace_id": "t_" + stable_id("t", "fleet-recorder", world.seed, team)[2:],
              "profile": "fingerprint", "name_key_id": key_id(FLEET_NAME_KEY),
              "principal_key_id": key_id(FLEET_ORG_KEY), "fp_key_id": key_id(FLEET_FP_KEY),
              "identity_mode": "install",
              "producer": {"name": "tokenbill", "version": "0.2.0", "adapter": "recorder"},
              "created_ms": world.until_ms,
              "attribution": to_json(Attribution(team=team, agent_product="agent_sdk",
                                                 billing_path="api_key"))}
    records: list[dict[str, Any]] = [header]
    emitted: set[str] = set()
    for sk in sorted(by_session):
        shell = shells[sk]
        records.append({"rec": "session", "schema": _SCHEMA, "session_key": sk,
                        "source_kind": shell.source_kind,
                        "attribution": to_json(shell.attribution),
                        "started_ms": shell.started_ms, "ended_ms": shell.ended_ms})
        for lane in by_session[sk]:
            records.append({"rec": "lane", "schema": _SCHEMA, "lane_key": lane.lane_key,
                            "session_key": sk, "kind": lane.kind.value,
                            "parent_lane_key": lane.parent_lane_key,
                            "cache_scope_key": lane.cache_scope_key,
                            "ttl_observed": lane.ttl_observed, "lane_exact": lane.lane_exact})
            parent: Request | None = None
            for req in lane.requests:
                fp = req.fingerprint
                fresh = []
                if fp is not None:
                    for bl in fp.blocks:
                        if bl.h not in emitted:
                            emitted.add(bl.h)
                            fresh.append(to_json(bl))
                if fresh:
                    records.append({"rec": "blocks", "schema": _SCHEMA,
                                    "key_id": key_id(FLEET_FP_KEY), "blocks": fresh})
                records.append(_request_record(req, parent))
                parent = req
            for ev in lane.events:
                records.append({"rec": "event", "schema": _SCHEMA, "lane_key": ev.lane_key,
                                "ts_ms": ev.ts_ms, "kind": ev.kind.value,
                                "attrs": dict(ev.attrs)})
    path = out_dir / f"trace2/{team}-recorder.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(path, records)
    return {f"trace2/{team}-recorder.jsonl": path}


def _request_record(req: Request, parent: Request | None) -> dict[str, Any]:
    fp_obj = None
    fp = req.fingerprint
    if fp is not None:
        keep = 0
        if parent is not None and parent.fingerprint is not None:
            prev = parent.fingerprint.blocks
            while keep < min(len(prev), len(fp.blocks)) and prev[keep].h == fp.blocks[keep].h:
                keep += 1
        fp_obj = {"parent": parent.request_id if parent is not None else None, "keep": keep,
                  "append": [bl.h for bl in fp.blocks[keep:]],
                  "markers": [[bp.block_index, bp.ttl] for bp in req.params.breakpoints],
                  "tier_end": list(fp.tier_end)}
    attempts = []
    for att in req.attempts:
        obj = to_json(att)
        obj.pop("raw_usage_json", None)
        serving = [inf for inf in att.inferences if inf.kind is not InferenceKind.KEEPALIVE]
        obj["raw_usage"] = _raw_usage(serving[-1].usage) if serving else None
        attempts.append(obj)
    return {"rec": "request", "schema": _SCHEMA, "request_id": req.request_id,
            "session_key": req.session_key, "lane_key": req.lane_key, "seq": req.seq,
            "attribution": to_json(req.attribution), "params": to_json(req.params),
            "appended": [to_json(a) for a in req.appended],
            "source": to_json(req.source) if req.source is not None else None,
            "fp": fp_obj, "attempts": attempts}


# ---------------------------------------------------------------------------------------------
# Admin pages and Claude Code Analytics
# ---------------------------------------------------------------------------------------------


def write_admin_pages(world: Any, out_dir: Path) -> dict[str, Path]:
    """``admin/usage_report_messages.json`` (1d buckets grouped by workspace, model, service tier,
    speed and inference geo), ``admin/cost_report.json`` (grouped by workspace and description;
    cents as decimal strings) and ``admin/claude_code/<date>.json`` (one Claude Code Analytics
    page per day)."""
    from tokenbill.synth.fleet import _date_add

    pages = world.hints.pages
    days = [_date_add(world.window_start, d) for d in range(world.days)]
    files: dict[str, Path] = {}
    usage_by_day: dict[str, list[dict[str, Any]]] = {d: [] for d in days}
    for row in pages.usage_rows:
        usage_by_day[row["date"]].append({
            "account_id": None, "api_key_id": None,
            "cache_creation": {"ephemeral_1h_input_tokens": row["cache_write_1h"],
                               "ephemeral_5m_input_tokens": row["cache_write_5m"]},
            "cache_read_input_tokens": row["cache_read"], "context_window": None,
            "inference_geo": row["inference_geo"], "model": row["model"],
            "output_tokens": row["output"], "server_tool_use": {"web_search_requests": 0},
            "service_account_id": None, "service_tier": row["service_tier"],
            "speed": row["speed"], "uncached_input_tokens": row["uncached_input"],
            "workspace_id": row["workspace_id"]})
    usage_page = {"data": [{"ending_at": _date_add(d, 1) + "T00:00:00Z",
                            "results": usage_by_day[d], "starting_at": d + "T00:00:00Z"}
                           for d in days], "has_more": False, "next_page": None}
    k, p = _write(out_dir, "admin/usage_report_messages.json",
                  json.dumps(usage_page, indent=2) + "\n")
    files[k] = p
    cost_by_day: dict[str, list[dict[str, Any]]] = {d: [] for d in days}
    for row in pages.cost_rows:
        cost_by_day[row["date"]].append({
            "amount": row["amount"], "context_window": None, "cost_type": "tokens",
            "currency": "USD", "description": row["description"],
            "inference_geo": row["inference_geo"], "model": row["model"],
            "service_tier": row["service_tier"], "token_type": row["token_type"],
            "workspace_id": row["workspace_id"]})
    cost_page = {"data": [{"ending_at": _date_add(d, 1) + "T00:00:00Z",
                           "results": cost_by_day[d], "starting_at": d + "T00:00:00Z"}
                          for d in days], "has_more": False, "next_page": None}
    k, p = _write(out_dir, "admin/cost_report.json", json.dumps(cost_page, indent=2) + "\n")
    files[k] = p
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in pages.analytics_rows:
        by_day.setdefault(row["date"][:10], []).append(row)
    for day in days:
        page = {"data": by_day.get(day, []), "has_more": False, "next_page": None}
        k, p = _write(out_dir, f"admin/claude_code/{day}.json", json.dumps(page, indent=2) + "\n")
        files[k] = p
    return files


# ---------------------------------------------------------------------------------------------
# AWS CUR 2.0
# ---------------------------------------------------------------------------------------------


def write_cur_csv(world: Any, out_dir: Path) -> dict[str, Path]:
    """The Bedrock team's AWS CUR 2.0 export as CSV (one row per day, usage type and IAM
    principal; unblended cost at list, net unblended 10% below). Key: ``cur/bedrock-cur2.csv``."""
    rows: Sequence[Mapping[str, str]] = world.hints.pages.cur_rows
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    k, p = _write(out_dir, "cur/bedrock-cur2.csv", buf.getvalue())
    return {k: p}


def write_all(world: Any, out_dir: Path) -> dict[str, Path]:
    """Every source family (keys sorted)."""
    files: dict[str, Path] = {}
    files.update(write_cc_transcripts(world, out_dir))
    files.update(write_headless_streams(world, out_dir))
    files.update(write_otlp(world, out_dir))
    files.update(write_trace_v2_fingerprint(world, out_dir))
    files.update(write_admin_pages(world, out_dir))
    files.update(write_cur_csv(world, out_dir))
    return dict(sorted(files.items()))



