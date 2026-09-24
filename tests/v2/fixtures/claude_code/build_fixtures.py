"""Build the synthetic Claude Code fixtures (CC package; run as a script).

    uv run python tests/v2/fixtures/claude_code/build_fixtures.py [OUT_DIR]

Everything here is synthetic and schema-true per SPEC §19.4 (observed Claude Code v2.1.2xx
transcript fields; Agent SDK message shapes). Nothing comes from a real transcript. The content
canary (``core.builders.CANARY`` / ``CANARY_EMAIL``) is planted in message text, thinking, tool
inputs, tool results, ``cwd``, ``gitBranch``, file paths inside ``toolUseResult``, attachment
content, summaries, headless stream content and an email-shaped string.

The module doubles as the builder library of ``tests/v2/claude_code`` (loaded by path): ``Tx``
writes transcripts, ``Hx`` writes headless SDK streams, ``usage`` builds usage objects and
``synthetic_transcript`` writes the perf-test file. Output is deterministic (fixed ids, fixed
timestamps, sorted JSON keys), so the checked-in files equal a fresh build.
"""

from __future__ import annotations

import datetime as _dt
import json
import random
import sys
import uuid as _uuid
from pathlib import Path
from typing import Any

from tokenbill.core.builders import CANARY, CANARY_EMAIL

HERE = Path(__file__).resolve().parent
T0_MS = 1_790_067_600_000  # 2026-09-22T09:00:00Z (Opus 5.5 is priced from 2026-09-22)
VERSION = "2.1.270"
LONG = (" lorem ipsum " * 12).strip()  # > 64 bytes of text around each canary


def iso(ms: int) -> str:
    """``2026-09-22T09:00:00.000Z`` for epoch milliseconds."""
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def usage(inp: int = 0, read: int = 0, w5: int = 0, w1: int = 0, out: int = 0, *,
          total_write: int | None = None, iterations: list[dict[str, Any]] | None = None,
          tier: str = "standard", geo: str | None = "not_available", speed: str | None = None,
          split: bool = True, **extra: Any) -> dict[str, Any]:
    """An Anthropic ``usage`` object (``input_tokens`` excludes cache)."""
    u: dict[str, Any] = {
        "input_tokens": inp,
        "cache_creation_input_tokens": w5 + w1 if total_write is None else total_write,
        "cache_read_input_tokens": read,
        "output_tokens": out,
        "service_tier": tier,
    }
    if split:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}
    if geo is not None:
        u["inference_geo"] = geo
    if speed is not None:
        u["speed"] = speed
    if iterations is not None:
        u["iterations"] = iterations
    u.update(extra)
    return u


def iteration(kind: str, model: str | None = None, **kw: Any) -> dict[str, Any]:
    """One ``usage.iterations[]`` element."""
    u = usage(**kw)
    u.pop("service_tier", None)
    u.pop("inference_geo", None)
    u["type"] = kind
    if model is not None:
        u["model"] = model
    return u


def _buckets(u: dict[str, Any]) -> dict[str, int]:
    """Canonical buckets of a usage object (the anthropic.messages mapping, §3.11)."""
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
    w1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
    total = u.get("cache_creation_input_tokens")
    unknown = max(0, (total or 0) - w5 - w1) if total is not None else 0
    return {"uncached_input": u.get("input_tokens", 0) or 0,
            "cache_read": u.get("cache_read_input_tokens", 0) or 0,
            "cache_write_5m": w5, "cache_write_1h": w1, "cache_write_unknown": unknown,
            "output": u.get("output_tokens", 0) or 0}


def _add(acc: dict[str, int], b: dict[str, int]) -> None:
    for k, v in b.items():
        acc[k] = acc.get(k, 0) + v


class Tx:
    """Writes one Claude Code transcript file (entries in order, deterministic ids)."""

    def __init__(self, session_id: str, *, cwd: str = f"/home/dev/alpha {CANARY}",
                 branch: str = f"feature/{CANARY}", version: str = VERSION,
                 entrypoint: str = "cli", t0_ms: int = T0_MS, agent_id: str | None = None,
                 effort: str | None = "medium", seed: int = 0) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.branch = branch
        self.version = version
        self.entrypoint = entrypoint
        self.t = t0_ms
        self.agent_id = agent_id
        self.effort = effort
        self.lines: list[str] = []
        self.parent: str | None = None
        self._n = seed * 1_000_000
        self.messages: dict[str, tuple[int, dict[str, Any]]] = {}   # mid → (max out, usage)
        self.naive: dict[str, int] = {}
        self.assistant_lines = 0

    # --- ids and clock ---
    def uuid(self) -> str:
        self._n += 1
        return str(_uuid.UUID(int=(hash_int(self.session_id) << 64) + self._n))

    def tick(self, dt_ms: int) -> str:
        self.t += dt_ms
        return iso(self.t)

    def base(self, etype: str, dt_ms: int, **extra: Any) -> dict[str, Any]:
        u = self.uuid()
        e: dict[str, Any] = {"parentUuid": self.parent, "isSidechain": self.agent_id is not None,
                             "userType": "external", "cwd": self.cwd,
                             "sessionId": self.session_id, "version": self.version,
                             "gitBranch": self.branch, "entrypoint": self.entrypoint,
                             "type": etype, "uuid": u, "timestamp": self.tick(dt_ms)}
        if self.agent_id is not None:
            e["agentId"] = self.agent_id
        e.update(extra)
        self.parent = u
        return e

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.lines.append(_dumps_raw(entry))
        return entry

    def raw(self, line: str) -> None:
        self.lines.append(line)

    def duplicate_last(self) -> None:
        """Re-write the previous line verbatim (same uuid): must be dropped."""
        self.lines.append(self.lines[-1])

    # --- entries ---
    def human(self, text: str, dt_ms: int = 5_000, *, origin: bool = True,
              **extra: Any) -> dict[str, Any]:
        e = self.base("user", dt_ms, message={"role": "user", "content": text}, **extra)
        if origin:
            e["origin"] = {"kind": "human"}
        return self.add(e)

    def meta_user(self, text: str, dt_ms: int = 100, **extra: Any) -> dict[str, Any]:
        return self.add(self.base("user", dt_ms, isMeta=True,
                                  message={"role": "user", "content": text}, **extra))

    def tool_result(self, tool_use_id: str, text: str, dt_ms: int = 2_000, *,
                    is_error: bool = False, images: int = 0, total_tokens: int | None = None,
                    **extra: Any) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        parts += [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                               "data": "iVBORw0KGgo" + "A" * 40}}
                  for _ in range(images)]
        content: Any = text if not images else parts
        tur: dict[str, Any] = {"stdout": text, "filePath": f"/home/dev/{CANARY}/src/app.py"}
        if total_tokens is not None:
            tur["totalTokens"] = total_tokens
        return self.add(self.base(
            "user", dt_ms, message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content,
                 "is_error": is_error}]}, toolUseResult=tur, **extra))

    def attachment(self, att_type: str, content: Any, dt_ms: int = 50,
                   **extra: Any) -> dict[str, Any]:
        return self.add(self.base("attachment", dt_ms,
                                  attachment={"type": att_type, "content": content}, **extra))

    def system(self, subtype: str, dt_ms: int = 100, **fields: Any) -> dict[str, Any]:
        return self.add(self.base("system", dt_ms, subtype=subtype, level="info",
                                  content=f"system note {CANARY}", **fields))

    def assistant_line(self, mid: str, model: str, u: dict[str, Any], block: dict[str, Any] | None,
                       *, stop: str | None = None, dt_ms: int = 700, request_id: str | None = None,
                       **extra: Any) -> dict[str, Any]:
        msg = {"id": mid, "type": "message", "role": "assistant", "model": model,
               "content": [block] if block is not None else [], "stop_reason": stop,
               "stop_sequence": None, "usage": u}
        if self.effort is not None and "effort" not in extra:
            extra["effort"] = self.effort
        e = self.base("assistant", dt_ms, message=msg,
                      requestId=request_id or ("req_" + mid[4:]), **extra)
        self.assistant_lines += 1
        if model != "<synthetic>":
            prev = self.messages.get(mid)
            if prev is None or u["output_tokens"] >= prev[0]:
                self.messages[mid] = (u["output_tokens"], u)
            _add(self.naive, _buckets(u))
        return self.add(e)

    def call(self, mid: str, model: str, *, inp: int = 0, read: int = 0, w5: int = 0,
             w1: int = 0, outputs: tuple[int, ...] = (3, 250, 470), stop: str | None = "tool_use",
             blocks: list[dict[str, Any]] | None = None, dt_ms: int = 1_500,
             line_extra: dict[str, Any] | None = None, request_id: str | None = None,
             **usage_kw: Any) -> list[dict[str, Any]]:
        """One API response written as ``len(outputs)`` split lines (one content block each);
        ``stop`` goes on the last line only."""
        if blocks is None:
            blocks = [{"type": "thinking", "thinking": f"thinking {CANARY} {LONG}",
                       "signature": "sig"},
                      {"type": "text", "text": f"answer {CANARY} {LONG}"},
                      {"type": "tool_use", "id": "toolu_" + mid[4:], "name": "Bash",
                       "input": {"command": f"ls {CANARY} {LONG}"}}][-len(outputs):]
        out = []
        for i, o in enumerate(outputs):
            u = usage(inp, read, w5, w1, o, **usage_kw)
            last = i == len(outputs) - 1
            out.append(self.assistant_line(
                mid, model, u, blocks[i] if i < len(blocks) else None,
                stop=stop if last else None, dt_ms=dt_ms if i == 0 else 300,
                request_id=request_id, **(line_extra or {})))
        return out

    # --- output ---
    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text(), encoding="utf-8")
        return path

    def expected(self) -> dict[str, Any]:
        """De-duplicated usage (max output per message id, synthetic skipped) and naive sums."""
        dedup: dict[str, int] = {}
        inference: dict[str, int] = {}
        for _out, u in self.messages.values():
            _add(dedup, _buckets(u))
            its = u.get("iterations")
            if its:
                for element in its:
                    _add(inference, _buckets(element))
            else:
                _add(inference, _buckets(u))
        return {"messages": len(self.messages), "assistant_lines": self.assistant_lines,
                "dedup_usage": dict(sorted(dedup.items())),
                "inference_usage": dict(sorted(inference.items())),
                "naive_usage": dict(sorted(self.naive.items()))}


def hash_int(text: str) -> int:
    """A small deterministic integer from text (uuid namespacing)."""
    h = 0
    for ch in text.encode():
        h = (h * 131 + ch) % (1 << 60)
    return h


class Hx:
    """Writes one headless SDK message stream (``claude -p --output-format stream-json`` shape)."""

    def __init__(self, session_id: str, *, timestamps: bool = False, t0_ms: int = T0_MS) -> None:
        self.session_id = session_id
        self.messages: list[dict[str, Any]] = []
        self.timestamps = timestamps
        self.t = t0_ms
        self._n = 0

    def _msg(self, mtype: str, **fields: Any) -> dict[str, Any]:
        self._n += 1
        m: dict[str, Any] = {"type": mtype, "session_id": self.session_id,
                             "uuid": str(_uuid.UUID(int=(hash_int(self.session_id) << 64)
                                                    + self._n))}
        if self.timestamps:
            self.t += 1_000
            m["timestamp"] = iso(self.t)
        m.update(fields)
        self.messages.append(m)
        return m

    def init(self, model: str = "claude-opus-5-5") -> dict[str, Any]:
        return self._msg("system", subtype="init", model=model, cwd=f"/runner/work/{CANARY}",
                         tools=["Bash", "Read", "mcp__github__list_issues"],
                         mcp_servers=[{"name": "github", "status": "connected"}],
                         permissionMode="default", apiKeySource="ANTHROPIC_API_KEY")

    def assistant(self, mid: str, model: str, u: dict[str, Any],
                  blocks: list[dict[str, Any]] | None = None, *, parent: str | None = None,
                  stop: str | None = None) -> dict[str, Any]:
        msg = {"id": mid, "type": "message", "role": "assistant", "model": model,
               "content": blocks if blocks is not None else [
                   {"type": "text", "text": f"step {CANARY} {LONG}"}],
               "stop_reason": stop, "stop_sequence": None, "usage": u}
        return self._msg("assistant", message=msg, parent_tool_use_id=parent)

    def tool_result(self, tool_use_id: str, text: str, *, parent: str | None = None
                    ) -> dict[str, Any]:
        return self._msg("user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]},
            parent_tool_use_id=parent)

    def result(self, model_usage: dict[str, dict[str, Any]] | None, *, out: int = 0,
               inp: int = 0, read: int = 0, write: int = 0, cost: str | None = "0.4210",
               subtype: str = "success", is_error: bool = False) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "subtype": subtype, "is_error": is_error, "duration_ms": 42_000,
            "duration_api_ms": 39_000, "num_turns": 4, "result": f"done {CANARY} {LONG}",
            "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                      "cache_creation_input_tokens": write, "output_tokens": out,
                      "server_tool_use": {"web_search_requests": 0}},
            "permission_denials": []}
        if cost is not None:
            fields["total_cost_usd"] = _Raw(cost)
        if model_usage is not None:
            fields["modelUsage"] = model_usage
        return self._msg("result", **fields)

    def jsonl(self) -> str:
        return "".join(_dumps_raw(m) + "\n" for m in self.messages)

    def array(self) -> str:
        return "[\n" + ",\n".join(_dumps_raw(m) for m in self.messages) + "\n]\n"


class _Raw:
    """A JSON number literal written verbatim (money is a decimal literal, never a float)."""

    def __init__(self, literal: str) -> None:
        self.literal = literal


def _dumps_raw(obj: Any) -> str:
    """``dumps`` that writes :class:`_Raw` values as bare number literals."""
    marks: dict[str, str] = {}

    def swap(v: Any) -> Any:
        if isinstance(v, _Raw):
            key = f"__RAW{len(marks)}__"
            marks[key] = v.literal
            return key
        if isinstance(v, dict):
            return {k: swap(x) for k, x in v.items()}
        if isinstance(v, list):
            return [swap(x) for x in v]
        return v

    text = dumps(swap(obj))
    for key, literal in marks.items():
        text = text.replace(f'"{key}"', literal)
    return text


def model_usage(model: str, inp: int, out: int, read: int = 0, write: int = 0,
                cost: str = "0.1000") -> dict[str, Any]:
    """One ``modelUsage`` entry (camelCase keys, Agent SDK cost tracking)."""
    return {model: {"inputTokens": inp, "outputTokens": out, "cacheReadInputTokens": read,
                    "cacheCreationInputTokens": write, "webSearchRequests": 0,
                    "costUSD": _Raw(cost), "contextWindow": 200000, "costBasis": "list"}}


# ---------------------------------------------------------------------------------------------
# the checked-in fixture tree
# ---------------------------------------------------------------------------------------------

SID_ALPHA = "5b7c1f0e-0000-4000-8000-00000000a11a"
SID_BETA = "5b7c1f0e-0000-4000-8000-00000000be7a"
SID_HEADLESS = "9e0d2c3b-0000-4000-8000-0000000c1c1c"


def alpha_main() -> Tx:
    """A rich main-lane session: split entries, duplicates, synthetic, MSO, iterations, TTL split,
    effort, attribution names, attachments, compaction, fallback, API error, upgrade, diagnostics,
    thinking_dropped, a requestId collision and a cost-state entry."""
    tx = Tx(SID_ALPHA)
    tx.add({"type": "summary", "summary": f"Session summary {CANARY} {LONG}",
            "leafUuid": "00000000-0000-4000-8000-000000000000"})
    tx.human(f"Please fix the failing test {CANARY} and mail {CANARY_EMAIL} {LONG}")
    tx.attachment("new_file", {"filePath": f"/home/dev/{CANARY}/a.py",
                               "content": f"print('{CANARY}') {LONG}"})
    tx.call("msg_01AlphaSplit0001", "claude-opus-5-5", inp=12, w5=2_000, w1=3_000,
            outputs=(3, 250, 470), line_extra={"perTurnEffort": "high",
                                               "attributionSkill": "pdf-tools",
                                               "attributionMcpServer": "github"})
    tx.duplicate_last()
    tx.tool_result("toolu_01AlphaSplit0001", f"total 42 {CANARY} {LONG}", total_tokens=9_999)
    # message_start-only placeholder answered by a tool_result (MSO)
    tx.call("msg_02AlphaPlaceho02", "claude-opus-5-5", inp=3, read=5_000, w5=400, outputs=(3,),
            stop=None, blocks=[{"type": "tool_use", "id": "toolu_02AlphaPlaceho02",
                                "name": "mcp__github__list_issues",
                                "input": {"repo": f"acme/{CANARY}", "q": LONG}}])
    tx.tool_result("toolu_02AlphaPlaceho02", f"issues {CANARY} {LONG}", images=1)
    # no stop, large output: FINAL
    tx.call("msg_03AlphaNoStop003", "claude-opus-5-5", inp=5, read=5_400, w5=900,
            outputs=(900,), stop=None,
            blocks=[{"type": "text", "text": f"long answer {CANARY} {LONG}"}])
    tx.attachment("todo", [{"content": f"step one {CANARY}", "status": "pending"}])
    tx.human(f"now check the logs {CANARY}", origin=False)
    # 1h + 5m split with a residual (6,000 total → 1,000 unknown)
    tx.call("msg_04AlphaTtlResid4", "claude-opus-5-5", inp=7, read=6_300, w5=2_000, w1=3_000,
            total_write=6_000, outputs=(3, 120), stop="end_turn")
    # iterations: declined (0 output) on claude-fable-5 + fallback on claude-opus-4-8
    tx.human("try the other model")
    its = [iteration("message", "claude-fable-5", inp=900, out=0),
           iteration("fallback_message", "claude-opus-4-8", inp=900, out=210)]
    tx.call("msg_05AlphaFallbck05", "claude-fable-5", inp=900, outputs=(210,), stop="end_turn",
            iterations=its, blocks=[{"type": "fallback", "originalModel": "claude-fable-5",
                                     "fallbackModel": "claude-opus-4-8"}])
    tx.system("model_refusal_fallback", originalModel="claude-fable-5",
              fallbackModel="claude-opus-4-8")
    tx.system("api_error", error={"status": 529, "message": f"overloaded {CANARY}"},
              retryAttempt=1, maxRetries=10, retryInMs=2_000)
    tx.meta_user(f"<command-name>/compact</command-name> {CANARY}")
    tx.system("compact_boundary", compactMetadata={"trigger": "manual", "preTokens": 18_000,
                                                   "postTokens": 2_100, "durationMs": 31_000,
                                                   "cumulativeDroppedTokens": 15_900})
    tx.add(tx.base("user", 50, isCompactSummary=True,
                   message={"role": "user", "content": f"Summary {CANARY} {LONG}"}))
    tx.version = "2.1.271"
    tx.human("continue")
    tx.call("msg_06AlphaDiagnos06", "claude-opus-5-5", inp=11, read=2_000, w5=150,
            outputs=(3, 60), stop="end_turn", request_id="req_shared_collision")
    tx.lines[-1] = dumps(_patch_message(json.loads(tx.lines[-1]), {
        "diagnostics": {"cache_miss_reason": {"type": "system_changed",
                                              "cache_missed_input_tokens": 2_100}},
        "input_transformations": [{"type": "thinking_dropped"}, {"type": "thinking_dropped"}],
        "context_management": {"applied_edits": [{"type": "clear_tool_uses_20250919",
                                                  "cleared_input_tokens": 1_200}]}}))
    tx.human("and one more")
    tx.call("msg_07AlphaCollide07", "claude-opus-5-5", inp=9, read=2_150, w5=80,
            outputs=(40,), stop="end_turn", request_id="req_shared_collision")
    tx.assistant_line("msg_08AlphaSynthet08", "<synthetic>", usage(), {
        "type": "text", "text": f"API Error: 500 {CANARY}"}, stop="stop_sequence",
        isApiErrorMessage=True, apiErrorStatus=500)
    tx.add(tx.base("cost-state", 10, totalCostUSD=_RawCost("0.123456789")))
    tx.add({"type": "file-history-snapshot", "messageId": "x", "snapshot": {
        "trackedFileBackups": {f"/home/dev/{CANARY}/a.py": {"backupFileName": "a@v1"}}},
        "isSnapshotUpdate": False})
    return tx


class _RawCost(_Raw):
    pass


def _patch_message(entry: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    entry["message"].update(fields)
    return entry


def alpha_subagent() -> tuple[Tx, dict[str, Any]]:
    """A subagent transcript (``agent-a1b2c3.jsonl``) and its meta.json."""
    tx = Tx(SID_ALPHA, agent_id="a1b2c3", t0_ms=T0_MS + 60_000, seed=1)
    tx.human(f"Explore the repository for {CANARY}", origin=False)
    tx.call("msg_11SubagentCall11", "claude-sonnet-5", inp=4, w5=8_000, outputs=(3, 180),
            stop="tool_use")
    tx.tool_result("toolu_11SubagentCall11", f"file list {CANARY} {LONG}")
    tx.call("msg_12SubagentCall12", "claude-sonnet-5", inp=6, read=8_000, w5=600,
            outputs=(3, 95), stop="end_turn")
    meta = {"agentType": "Explore", "description": f"explore {CANARY} {LONG}",
            "model": "sonnet", "parentAgentId": None, "spawnDepth": 1,
            "toolUseId": "toolu_01AlphaSplit0001", "worktreePath": f"/tmp/{CANARY}"}
    return tx, meta


def alpha_workflow() -> tuple[Tx, dict[str, Any]]:
    """A workflow agent transcript and its meta.json."""
    tx = Tx(SID_ALPHA, agent_id="wf01", t0_ms=T0_MS + 120_000, seed=2)
    tx.human(f"Workflow phase prompt {CANARY}", origin=False)
    tx.call("msg_21WorkflowCall21", "claude-haiku-4-5", inp=20, w5=4_500, outputs=(3, 70),
            stop="end_turn")
    meta = {"agentType": "workflow-subagent", "spawnDepth": 2, "workflowPhase": "review"}
    return tx, meta


def beta_subscription() -> Tx:
    """A subscription session whose quota switches to overage half way (quotaLimits)."""
    tx = Tx(SID_BETA, cwd=f"/home/dev/beta {CANARY}", t0_ms=T0_MS + 3_600_000, seed=3)
    q_ok = {"status": "allowed", "rateLimitType": "five_hour", "isUsingOverage": False,
            "overageStatus": "allowed", "resetsAt": 1_790_085_600}
    q_over = dict(q_ok, status="allowed_warning", isUsingOverage=True)
    tx.human(f"refactor {CANARY}")
    tx.call("msg_31BetaAllowance31", "claude-opus-5-5", inp=10, w1=12_000, outputs=(3, 300),
            stop="tool_use", line_extra={"quotaLimits": q_ok})
    tx.tool_result("toolu_31BetaAllowance31", f"ok {CANARY}")
    tx.call("msg_32BetaOverage032", "claude-opus-5-5", inp=12, read=12_000, w1=500,
            outputs=(3, 200), stop="tool_use", line_extra={"quotaLimits": q_over})
    tx.tool_result("toolu_32BetaOverage032", f"ok {CANARY}")
    tx.call("msg_33BetaOverage033", "claude-opus-5-5", inp=14, read=12_500, w1=300,
            outputs=(3, 150), stop="end_turn")
    return tx


def truncated_case() -> Tx:
    """A transcript whose last line is cut mid-write."""
    tx = Tx("5b7c1f0e-0000-4000-8000-0000000cafe0", seed=4)
    tx.human("hello")
    tx.call("msg_41Truncated0041", "claude-opus-5-5", inp=10, w5=1_000, outputs=(3, 50),
            stop="end_turn")
    tx.human("again")
    tx.call("msg_42Truncated0042", "claude-opus-5-5", inp=10, read=1_000, w5=30,
            outputs=(3,), stop=None)
    last = tx.lines.pop()
    tx.raw(last[: len(last) // 2])
    _, lost = tx.messages.pop("msg_42Truncated0042")
    _add(tx.naive, {k: -v for k, v in _buckets(lost).items()})   # the cut line never parses
    tx.assistant_lines -= 1
    return tx


def headless_stream() -> Hx:
    """stream-json: parallel tool calls sharing one id, a subagent, placeholders, a result with
    modelUsage (subagents included)."""
    hx = Hx(SID_HEADLESS)
    hx.init()
    u1 = usage(6, 0, 9_000, 0, 2)
    tool_a = {"type": "tool_use", "id": "toolu_h1a", "name": "Bash",
              "input": {"command": f"make test {CANARY} {LONG}"}}
    tool_b = {"type": "tool_use", "id": "toolu_h1b", "name": "mcp__github__list_issues",
              "input": {"q": f"{CANARY} {LONG}"}}
    hx.assistant("msg_h01Parallel", "claude-opus-5-5", u1, [tool_a])
    hx.assistant("msg_h01Parallel", "claude-opus-5-5", u1, [tool_b])  # parallel: same id/usage
    hx.tool_result("toolu_h1a", f"ok {CANARY} {LONG}")
    hx.tool_result("toolu_h1b", f"issues {CANARY_EMAIL} {LONG}")
    hx.assistant("msg_h02Agent", "claude-opus-5-5", usage(8, 9_000, 700, 0, 1),
                 [{"type": "tool_use", "id": "toolu_h2", "name": "Agent",
                   "input": {"prompt": f"review {CANARY} {LONG}"}}])
    hx._msg("user", message={"role": "user", "content": f"review {CANARY} {LONG}"},
            parent_tool_use_id="toolu_h2")
    hx.assistant("msg_h03Sub", "claude-sonnet-5", usage(5, 0, 3_000, 0, 1),
                 parent="toolu_h2")
    hx.tool_result("toolu_h2", f"review done {CANARY}")
    hx.assistant("msg_h04Final", "claude-opus-5-5", usage(4, 9_700, 250, 0, 1),
                 stop="end_turn")
    mu = model_usage("claude-opus-5-5", 18, 1_400, read=18_700, write=9_950, cost="0.3210")
    mu.update(model_usage("claude-sonnet-5", 5, 600, write=3_000, cost="0.1000"))
    hx.result(mu, out=1_100, inp=18, read=18_700, write=9_950, cost="0.4210")
    return hx


def headless_result_only() -> dict[str, Any]:
    """``claude -p --output-format json``: one result object, no steps."""
    hx = Hx("9e0d2c3b-0000-4000-8000-0000000c2c2c")
    mu = model_usage("claude-opus-5-5", 30, 800, read=40_000, write=5_000, cost="0.2345")
    return hx.result(mu, out=800, inp=30, read=40_000, write=5_000, cost="0.2345")


def headless_execution_file() -> Hx:
    """claude-code-action execution file (JSON array), with timestamps absent."""
    hx = Hx("9e0d2c3b-0000-4000-8000-0000000c3c3c")
    hx.init()
    hx.assistant("msg_e01Step", "claude-opus-5-5", usage(3, 0, 6_000, 0, 2),
                 [{"type": "tool_use", "id": "toolu_e1", "name": "Read",
                   "input": {"file_path": f"/runner/{CANARY}/x.py"}}])
    hx.tool_result("toolu_e1", f"content {CANARY} {LONG}")
    hx.assistant("msg_e02Step", "claude-opus-5-5", usage(4, 6_000, 300, 0, 1), stop="end_turn")
    hx.result(model_usage("claude-opus-5-5", 7, 520, read=6_000, write=6_300, cost="0.0512"),
              out=520, inp=7, read=6_000, write=6_300, cost="0.0512")
    return hx


def build(out: Path = HERE) -> dict[str, Any]:
    """Write every fixture under *out* and return the manifest (also written)."""
    manifest: dict[str, Any] = {"schema": "tokenbill/cc-fixtures@1",
                                "note": "synthetic, schema-true per SPEC §19.4; never real "
                                        "transcripts; CANARY planted in content fields",
                                "files": []}
    proj = out / "projects"
    alpha = proj / "-home-dev-alpha"
    beta = proj / "-home-dev-beta"

    def record(path: Path, kind: str, adapter: str | None, description: str,
               expected: dict[str, Any] | None = None) -> None:
        manifest["files"].append({"path": path.relative_to(out).as_posix(), "kind": kind,
                                  "adapter": adapter, "description": description,
                                  "expected": expected or {}})

    main = alpha_main()
    p = main.write(alpha / f"{SID_ALPHA}.jsonl")
    exp = main.expected()
    exp.update({"requests": exp["messages"] + 1, "compaction_estimated": {"pre": 18_000,
                                                                         "post": 2_100}})
    record(p, "transcript", "claude-code", "main lane: every §5.3 mechanism", exp)
    sub, sub_meta = alpha_subagent()
    p = sub.write(alpha / SID_ALPHA / "subagents" / "agent-a1b2c3.jsonl")
    record(p, "transcript", "claude-code", "subagent lane (Explore)",
           dict(sub.expected(), requests=len(sub.messages)))
    p = alpha / SID_ALPHA / "subagents" / "agent-a1b2c3.meta.json"
    p.write_text(dumps(sub_meta) + "\n", encoding="utf-8")
    record(p, "meta", None, "subagent meta (read: agentType, model, parentAgentId, spawnDepth, "
                            "toolUseId)")
    wf, wf_meta = alpha_workflow()
    p = wf.write(alpha / SID_ALPHA / "workflows" / "run-1" / "agent-wf01.jsonl")
    record(p, "transcript", "claude-code", "workflow agent lane",
           dict(wf.expected(), requests=len(wf.messages)))
    p = alpha / SID_ALPHA / "workflows" / "run-1" / "agent-wf01.meta.json"
    p.write_text(dumps(wf_meta) + "\n", encoding="utf-8")
    record(p, "meta", None, "workflow agent meta")
    p = alpha / "journal.jsonl"
    p.write_text(dumps({"type": "journal", "note": f"skip me {CANARY}"}) + "\n", encoding="utf-8")
    record(p, "journal", None, "journal.jsonl is always skipped")
    b = beta_subscription()
    p = b.write(beta / f"{SID_BETA}.jsonl")
    record(p, "transcript", "claude-code",
           "subscription session: quotaLimits.isUsingOverage flips to true on the second call",
           dict(b.expected(), requests=len(b.messages)))
    t = truncated_case()
    p = t.write(out / "cases" / "truncated-last-line.jsonl")
    record(p, "transcript", "claude-code", "last line cut mid-write (quarantined)",
           dict(t.expected(), requests=len(t.messages), quarantined=1))
    hs = headless_stream()
    p = out / "headless" / "stream.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(hs.jsonl(), encoding="utf-8")
    record(p, "headless", "claude-code-headless",
           "stream-json: parallel tool calls, subagent lane, placeholders, modelUsage result",
           {"steps": 4, "logged_output": {"claude-opus-5-5": 4, "claude-sonnet-5": 1},
            "residual_output": {"claude-opus-5-5": 1_396, "claude-sonnet-5": 599},
            "total_cost_nano": 421_000_000})
    p = out / "headless" / "claude-execution-output.json"
    p.write_text(headless_execution_file().array(), encoding="utf-8")
    record(p, "headless", "claude-code-headless", "execution file (JSON array, no timestamps)",
           {"steps": 2, "residual_output": {"claude-opus-5-5": 517}})
    p = out / "headless" / "result-only.json"
    p.write_text(_dumps_raw(headless_result_only()) + "\n", encoding="utf-8")
    record(p, "headless", "claude-code-headless", "--output-format json: aggregates only",
           {"steps": 0, "aggregates": 1})
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                       encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------------------------
# perf file
# ---------------------------------------------------------------------------------------------

def synthetic_transcript(path: Path, n_lines: int, seed: int = 7) -> dict[str, int]:
    """A realistic long session of about *n_lines* lines (~60% assistant split lines, tool
    results, occasional prompts/attachments), written directly (streaming) for the perf test."""
    rnd = random.Random(seed)
    tx = Tx("5b7c1f0e-0000-4000-8000-0000000fffff", seed=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"lines": 0, "assistant_lines": 0, "messages": 0}
    ctx = 20_000
    with open(path, "w", encoding="utf-8") as f:
        n = 0
        while counts["lines"] < n_lines:
            if n % 25 == 0:
                tx.human(f"next task {n} {LONG}")
            n += 1
            mid = f"msg_perf{n:010d}"
            k = rnd.choice((1, 2, 2, 3, 3, 3))
            outs = tuple(sorted(rnd.sample(range(3, 900), k)))
            tx.call(mid, "claude-opus-5-5", inp=rnd.randint(1, 20), read=ctx,
                    w5=rnd.randint(50, 2_000), outputs=outs, stop="tool_use")
            ctx += rnd.randint(100, 1_500)
            tx.tool_result("toolu_" + mid[4:], f"result {n} {LONG}")
            counts["assistant_lines"] += k
            counts["messages"] += 1
            for line in tx.lines:
                f.write(line + "\n")
            counts["lines"] += len(tx.lines)
            tx.lines.clear()
            tx.messages.clear()
            tx.naive.clear()
    return counts


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else HERE
    manifest = build(out)
    print(f"wrote {len(manifest['files'])} fixture files under {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
