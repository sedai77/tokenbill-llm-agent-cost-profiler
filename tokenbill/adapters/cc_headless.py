"""Claude Code headless, CI and Agent SDK streams (SPEC §5.12, D33).

``ClaudeCodeHeadlessAdapter`` (registry name ``claude-code-headless``) reads the traffic that never
reaches a laptop's ``~/.claude/projects``:

* the claude-code-action ``execution_file`` (``$RUNNER_TEMP/claude-execution-output.json``: a JSON
  array of SDK messages, or JSONL),
* ``claude -p --output-format stream-json`` (JSONL of SDK messages),
* ``claude -p --output-format json`` (one ``result`` object), and
* Agent SDK message logs (the same message shapes).

Rules (``ci-headless-ingest``, ``sdk-accounting-pitfalls``):

1. assistant messages are de-duplicated by ``message.id`` keeping the max ``output_tokens``
   (parallel tool calls repeat one id with identical usage); ``<synthetic>`` is skipped;
2. one MAIN lane per ``session_id``; messages with a ``parent_tool_use_id`` go to a SUBAGENT lane
   keyed by that id; without timestamps ``ts = base + file order × 1 ms`` (base: ``opts.now_ms``,
   else the file's mtime), ``lane_exact = False`` and the ``timing`` capability is dropped;
3. per-step ``output_tokens`` is a placeholder: steps keep their logged output (FINAL, exact on the
   logged tokens) and the session's last ``result`` supplies the exact output — per model in
   ``modelUsage`` (subagents included) the residual ``outputTokens − Σ logged step outputs`` becomes
   an ``OUTPUT_RESIDUAL`` inference on a synthetic request ``<main lane>#output-residual`` (no
   serving inference, so it never enters transitions); without ``modelUsage`` the main lane's
   residual comes from ``result.usage``; negative residuals are dropped
   (``dq.headless_output_residual``);
4. resumed sessions (result input totals > 1% above the stream's step inputs) and zeroed crash
   results skip the residual (``dq.headless_resumed_totals`` / ``dq.headless_zeroed_result``);
5. ``total_cost_usd`` is only a ``COST_STATE`` event (a client estimate, never billed); a file with
   a result and no steps yields only ``UsageAggregate(source_kind="claude_code.headless_result")``
   rows per model;
6. attribution comes from ``opts.attribution`` (the CLI collector reads the CI environment), never
   from the file.

Field names follow the Agent SDK cost-tracking and headless docs (verified 2026-09-23); the
execution-file container shape is research-grade (SPEC §19.8 #16, see the area README).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.adapters.claude_code import (
    _Run,
    build_result,
    check_options,
    decimal_usd_to_nano,
    loads_decimal,
    mcp_server_of,
    parse_ts_ms,
    safe_usage_json,
    sha256_file,
    source_id_for,
    source_info,
    token,
)
from tokenbill.core.conventions import (
    ANTHROPIC_MESSAGES,
    BadUsageError,
    anthropic_inferences,
    normalize_anthropic_messages,
)
from tokenbill.core.errors import ContractViolation, SourceError
from tokenbill.core.ids import request_id_for, stable_id
from tokenbill.core.records import (
    MAX_TOKENS,
    Attempt,
    Fidelity,
    Inference,
    InferenceKind,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Outcome,
    Request,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageSource,
    WorkloadClass,
)
from tokenbill.core.types import IngestOptions, IngestResult, QuarantineItem

__all__ = ["ADAPTER_NAME", "CAPABILITIES", "ClaudeCodeHeadlessAdapter", "iter_messages",
           "load_messages"]

logger = logging.getLogger("tokenbill.adapters.cc_headless")

ADAPTER_NAME = "claude-code-headless"
SOURCE_PRIORITY = 38
RESULT_SOURCE_KIND = "claude_code.headless_result"
CAPABILITIES = frozenset({"usage_sequence", "timing", "ttl_split", "iterations", "params",
                          "workload"})
#: Resumed-session threshold: result input totals above the steps' by more than 1% (§5.12.4).
RESUMED_NUM, RESUMED_DEN = 101, 100
#: Largest headless file read into memory (execution files of long CI runs are a few MB).
MAX_FILE_BYTES = 512 * 2**20
_MESSAGE_TYPES = frozenset({"system", "assistant", "user", "result", "stream_event"})
_QUERY_SOURCE = {LaneKind.MAIN: "main", LaneKind.SUBAGENT: "subagent"}


# ---------------------------------------------------------------------------------------------
# loading (array, JSONL or a single result object)
# ---------------------------------------------------------------------------------------------

def _read_text(path: Path) -> bytes:
    try:
        if path.suffix.lower() == ".gz":
            with gzip.open(path, "rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)
        else:
            with open(path, "rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None
    if len(data) > MAX_FILE_BYTES:
        raise SourceError(f"{path.name}: larger than {MAX_FILE_BYTES} bytes")
    return data


def _loads(raw: bytes) -> Any:
    try:
        return loads_decimal(raw)
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None


@dataclass
class _Loaded:
    messages: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (locator, msg)
    bad: list[tuple[str, str]] = field(default_factory=list)                 # (locator, reason)
    lines: int = 0


def load_messages(data: bytes) -> _Loaded:
    """Split a headless file into SDK message objects: a JSON array (execution file), JSONL
    (stream-json / execution file / SDK log), or one JSON object (``--output-format json``)."""
    out = _Loaded()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    stripped = data.lstrip()
    if stripped[:1] == b"[":
        _load_array(stripped, out)
        return out
    offset = 0
    first = True
    for raw in data.split(b"\n"):
        start = offset
        offset += len(raw) + 1
        line = raw.strip()
        if not line:
            continue
        out.lines += 1
        obj = _loads(line)
        if first and obj is None:
            whole = _loads(stripped)
            if isinstance(whole, dict):
                out.messages = [("offset:0", whole)]
                out.bad = []
                out.lines = 1
                return out
        first = False
        if isinstance(obj, dict):
            out.messages.append((f"offset:{start}", obj))
        else:
            out.bad.append((f"offset:{start}", "bad_json" if obj is None else "not_object"))
    return out


def _refuse_constant(tok: str) -> Any:
    raise ValueError("non-finite number")


_DECODER = json.JSONDecoder(parse_float=Decimal,
                            parse_constant=_refuse_constant)


def _load_array(data: bytes, out: _Loaded) -> None:
    """Decode a JSON array element by element, so a truncated execution file keeps every complete
    message (the damaged tail is quarantined once)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        out.bad.append(("index:0", "bad_json"))
        return
    pos, n, i = 1, len(text), 0
    while True:
        while pos < n and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= n or text[pos] == "]":
            return
        out.lines += 1
        try:
            item, pos = _DECODER.raw_decode(text, pos)
        except (ValueError, RecursionError):
            out.bad.append((f"index:{i}", "bad_json"))
            return
        if isinstance(item, dict):
            out.messages.append((f"index:{i}", item))
        else:
            out.bad.append((f"index:{i}", "not_object"))
        i += 1


# ---------------------------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------------------------

class _Step:
    __slots__ = ("best", "first", "first_ts", "lane", "last_ts", "locator", "mid", "out", "sid",
                 "stop", "ts_start", "mcp", "served")

    def __init__(self, mid: str, sid: str, lane: tuple[str, LaneKind, str | None], first: int,
                 locator: str, ts: int, ts_start: int, mcp: str | None) -> None:
        self.mid = mid
        self.sid = sid
        self.lane = lane
        self.first = first
        self.locator = locator
        self.first_ts = ts
        self.last_ts = ts
        self.ts_start = ts_start
        self.best: dict[str, Any] | None = None
        self.out = -1
        self.stop: str | None = None
        self.mcp = mcp
        #: (normalized model, service tier, speed, inference geo) of the built step
        self.served: tuple[str, str, str, str | None] | None = None


def _uint(value: object) -> int:
    return value if type(value) is int and 0 <= value <= MAX_TOKENS else 0


@dataclass
class ClaudeCodeHeadlessAdapter:
    """Headless / CI / Agent SDK stream adapter (registry name ``claude-code-headless``)."""

    name: str = ADAPTER_NAME
    capabilities: frozenset[str] = field(default=CAPABILITIES)

    def sniff(self, path: Path, head: bytes) -> bool:
        """SDK message streams: a JSON array or JSONL/JSON objects carrying ``session_id`` and an
        SDK message ``type`` (``system``/``assistant``/``user``/``result``)."""
        if not isinstance(head, (bytes, bytearray)):
            return False
        head = bytes(head)
        if head.startswith(b"\xef\xbb\xbf"):
            head = head[3:]
        text = head.lstrip()
        if text[:1] == b"[":
            return b'"session_id"' in text and b'"type"' in text and (
                b'"assistant"' in text or b'"result"' in text or b'"system"' in text)
        if text[:1] != b"{":
            return False
        for raw in text.split(b"\n")[:16]:
            raw = raw.strip()
            if not raw:
                continue
            obj = _loads(raw)
            if isinstance(obj, dict):
                return "session_id" in obj and obj.get("type") in _MESSAGE_TYPES
            break
        whole = _loads(text)
        if isinstance(whole, dict):
            return "session_id" in whole and whole.get("type") in _MESSAGE_TYPES
        return (b'"session_id"' in text and b'"type"' in text and b'"result"' in text
                and b'"modelUsage"' in text)

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Read one headless file (SPEC §5.12)."""
        check_options(opts, ADAPTER_NAME)
        path = Path(path)
        if not path.is_file():
            raise SourceError(f"{path.name}: not a file")
        digest, n_bytes = sha256_file(path)
        loaded = load_messages(_read_text(path))
        source_id = source_id_for(opts, path, ADAPTER_NAME)
        run = _Run(opts, source_id)
        try:
            base_ms = opts.now_ms if opts.now_ms > 0 else os.stat(path).st_mtime_ns // 1_000_000
        except OSError:
            base_ms = 0
        return _Reader(run, loaded, base_ms, path).result(digest, n_bytes)


class _Reader:
    """One headless file → an :class:`IngestResult`."""

    def __init__(self, run: _Run, loaded: _Loaded, base_ms: int, path: Path) -> None:
        self.run = run
        self.loaded = loaded
        self.path = path
        self.base_ms = base_ms
        needs_ts = [m for _, m in loaded.messages
                    if m.get("type") in ("assistant", "user", "result")]
        self.timed = bool(needs_ts) and all(parse_ts_ms(m.get("timestamp")) is not None
                                            for m in needs_ts)
        self.steps: dict[str, _Step] = {}
        self.results: dict[str, tuple[int, str, dict[str, Any]]] = {}
        self.sessions: list[str] = []
        self.triggers: dict[str, int] = {}
        self.tool_names: dict[str, str] = {}
        self.pending_mcp: dict[str, set[str]] = {}
        self.saw_split = False
        self.aggregates: list[UsageAggregate] = []

    # ---------- helpers ----------
    def ts(self, index: int, msg: Mapping[str, Any]) -> int:
        if self.timed:
            ts = parse_ts_ms(msg.get("timestamp"))
            if ts is not None:
                return ts
        return min(self.base_ms + index, MAX_TOKENS)

    def lane(self, sid: str, parent: object) -> tuple[str, LaneKind, str | None]:
        main = stable_id("ln", sid, "main")
        session_key = stable_id("ses", "claude-code", sid)
        exact = self.timed
        self.run.shell(main, session_key, LaneKind.MAIN, None, exact)
        if isinstance(parent, str) and parent:
            key = stable_id("ln", sid, "tool:" + parent)
            self.run.shell(key, session_key, LaneKind.SUBAGENT, main, exact)
            return key, LaneKind.SUBAGENT, main
        return main, LaneKind.MAIN, None

    def billing(self) -> str:
        path = self.run.billing_path
        return "unknown" if path is None else path

    # ---------- pass over the messages ----------
    def scan(self) -> None:
        run = self.run
        for locator, reason in self.loaded.bad:
            self._quarantine(locator, reason)
        last_sid = "unknown"
        for index, (locator, msg) in enumerate(self.loaded.messages):
            run.stats["records"] += 1
            sid = msg.get("session_id")
            sid = sid if isinstance(sid, str) and sid else last_sid
            last_sid = sid
            if sid not in self.sessions:
                self.sessions.append(sid)
            try:
                self.message(index, locator, sid, msg)
            except (ContractViolation, BadUsageError, TypeError, ValueError, KeyError,
                    AttributeError, IndexError, OverflowError, RecursionError) as exc:
                self._quarantine(locator, "bad_usage" if isinstance(exc, BadUsageError)
                                 else "bad_type:message")

    def _quarantine(self, locator: str, reason: str) -> None:
        run = self.run
        if not run.opts.lenient:
            raise SourceError(f"{self.path.name}: {locator}: {reason}")
        run.quarantined.append(QuarantineItem(source_id=run.source_id, locator=locator,
                                              reason=reason))
        run.dq["dq.quarantined"] += 1

    def message(self, index: int, locator: str, sid: str, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "assistant":
            self.assistant(index, locator, sid, msg)
        elif mtype == "user":
            lane_key, _kind, _p = self.lane(sid, msg.get("parent_tool_use_id"))
            self.triggers[lane_key] = self.ts(index, msg)
            inner = msg.get("message")
            content = inner.get("content") if isinstance(inner, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        server = mcp_server_of(self.tool_names.get(block.get("tool_use_id")))
                        if server is not None:
                            self.pending_mcp.setdefault(lane_key, set()).add(server)
        elif mtype == "result":
            self.results[sid] = (index, locator, msg)
        elif mtype not in _MESSAGE_TYPES:
            self.run.unknown_types[token(mtype, 32) or "(invalid)"] += 1
            self.run.dq["dq.unknown_entry_type"] += 1

    def assistant(self, index: int, locator: str, sid: str, msg: dict[str, Any]) -> None:
        """Group assistant messages by ``message.id`` (max output wins; rule 1)."""
        run = self.run
        run.stats["assistant_lines"] += 1
        inner = msg.get("message")
        if not isinstance(inner, dict):
            self._quarantine(locator, "missing:message")
            return
        mid = inner.get("id")
        if not isinstance(mid, str) or not mid or len(mid) > 128:
            self._quarantine(locator, "missing:message.id")
            return
        if inner.get("model") == "<synthetic>":
            run.dq["dq.synthetic_skipped"] += 1
            return
        usage = inner.get("usage")
        if not isinstance(usage, dict):
            self._quarantine(locator, "missing:message.usage")
            return
        buckets, _ = normalize_anthropic_messages(usage)
        if isinstance(usage.get("cache_creation"), dict):
            self.saw_split = True
        ts = self.ts(index, msg)
        lane = self.lane(sid, msg.get("parent_tool_use_id"))
        content = inner.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid, name = block.get("id"), block.get("name")
                    if isinstance(tid, str) and isinstance(name, str) and len(self.tool_names) \
                            < 100_000:
                        self.tool_names[tid] = name
        step = self.steps.get(mid)
        if step is None:
            start = self.triggers.get(lane[0], ts)
            pending = self.pending_mcp.pop(lane[0], set())
            step = self.steps[mid] = _Step(mid, sid, lane, index, locator, ts,
                                           start if start <= ts else ts,
                                           min(pending) if pending else None)
        if ts > step.last_ts:
            step.last_ts = ts
        if buckets.output >= step.out:
            step.out = buckets.output
            step.best = msg
        stop = inner.get("stop_reason")
        if stop is not None:
            step.stop = token(stop, 32) or "other"

    # ---------- building records ----------
    def build_steps(self) -> None:
        billing = self.billing()
        for step in sorted(self.steps.values(), key=lambda s: s.first):
            try:
                self.build_step(step, billing)
            except (ContractViolation, BadUsageError, TypeError, ValueError, KeyError,
                    AttributeError, IndexError, OverflowError, RecursionError) as exc:
                self._quarantine(step.locator, "bad_usage" if isinstance(exc, BadUsageError)
                                 else "bad_type:message")

    def build_step(self, step: _Step, billing: str) -> None:
        run = self.run
        msg = step.best
        if msg is None:
            return
        inner = msg["message"]
        usage = inner["usage"]
        model_raw = token(inner.get("model")) or ""
        tier = token(usage.get("service_tier"), 32) or "standard"
        speed = token(usage.get("speed"), 32) or "standard"
        geo = token(usage.get("inference_geo"), 32)
        if geo in ("not_available", ""):
            geo = None
        ctx = run.pricing(model_raw, tier, speed, geo, billing)
        step.served = (ctx.model, tier, speed, geo)
        request_id = request_id_for("anthropic", step.mid, run.source_id, step.locator)
        infs, codes = anthropic_inferences(usage, message_model=model_raw, ctx=ctx,
                                           id_prefix=request_id)
        for code in codes:
            run.dq[code] += 1
        if not ctx.model:
            run.dq["dq.unpriced_model"] += 1
        lane_key, kind, _parent = step.lane
        duration = step.last_ts - step.ts_start if self.timed else None
        attempt = Attempt(
            attempt_id=stable_id("at", request_id, 0), attempt_no=0, ts_start_ms=step.ts_start,
            ttft_ms=None, duration_ms=duration, outcome=Outcome.OK, http_status=None,
            error_type=None, retry_layer=None, retry_after_ms=None, should_retry=None,
            provider_request_id=None, provider_message_id=step.mid,
            model_served=model_raw or None, stop_reason=step.stop, inferences=tuple(infs),
            raw_usage_json=safe_usage_json(usage), convention_id=ANTHROPIC_MESSAGES)
        attr = run.attribution(query_source=_QUERY_SOURCE[kind],
                               billing_path=None if billing == "unknown" else billing,
                               mcp_server=run.names.name(step.mcp) if step.mcp else None)
        session_key = stable_id("ses", "claude-code", step.sid)
        req = Request(request_id=request_id, session_key=session_key, lane_key=lane_key,
                      seq=step.first, attribution=attr,
                      params=run.params(model_raw, None, None, None), attempts=(attempt,),
                      source=self.source_ref(step.locator))
        run.emit_request(req)
        run.touch_session(session_key, step.ts_start)
        if billing == "subscription":
            run.dq["dq.subscription_allowance"] += 1

    def source_ref(self, locator: str, fidelity: Fidelity = Fidelity.FULL) -> SourceRef:
        return SourceRef(adapter=ADAPTER_NAME, source_id=self.run.source_id, locator=locator,
                         fidelity=fidelity, priority=SOURCE_PRIORITY)

    def build_results(self) -> None:
        for sid in self.sessions:
            entry = self.results.get(sid)
            if entry is None:
                continue
            index, locator, msg = entry
            try:
                self.build_result(sid, index, locator, msg)
            except (ContractViolation, TypeError, ValueError, KeyError, AttributeError,
                    IndexError, OverflowError, RecursionError):
                self._quarantine(locator, "bad_type:result")

    def build_result(self, sid: str, index: int, locator: str, msg: dict[str, Any]) -> None:
        """The session's last ``result``: COST_STATE, the per-model output residual, or the
        headless-result aggregates when the file has no steps (rules 3-5)."""
        run = self.run
        ts = self.ts(index, msg)
        steps = [s for s in self.steps.values() if s.sid == sid and s.best is not None]
        per_model = _model_usage(msg.get("modelUsage"))
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        billing = self.billing()
        session_key = stable_id("ses", "claude-code", sid)
        cost_nano = decimal_usd_to_nano(msg.get("total_cost_usd"))
        if not steps:
            self.result_aggregates(sid, ts, per_model, usage, cost_nano, billing)
            return
        main_key, _k, _p = self.lane(sid, None)
        step_usage = {s.mid: _step_buckets(s) for s in steps}
        logged: dict[str, int] = {}
        step_in: dict[str, int] = {}
        for s in steps:
            model = run.model_id(token(s.best["message"].get("model")) or "").model
            u = step_usage[s.mid]
            logged[model] = logged.get(model, 0) + u.output
            step_in[model] = step_in.get(model, 0) + u.total_input
        main_steps = [s for s in steps if s.lane[1] is LaneKind.MAIN]
        if per_model:
            res_out = {m: v[3] for m, v in per_model.items()}
            res_in_total = sum(v[0] + v[1] + v[2] for m, v in per_model.items() if m in step_in)
            steps_in_total = sum(step_in.values())
            zero = all(not any(v) for v in per_model.values())
        else:
            main_model = run.model_id(
                token(main_steps[-1].best["message"].get("model")) or "").model \
                if main_steps else ""
            u_out = _uint(usage.get("output_tokens"))
            res_out = {main_model: u_out}
            res_in_total = (_uint(usage.get("input_tokens"))
                            + _uint(usage.get("cache_read_input_tokens"))
                            + _uint(usage.get("cache_creation_input_tokens")))
            steps_in_total = sum(step_usage[s.mid].total_input for s in main_steps)
            logged = {main_model: sum(step_usage[s.mid].output for s in main_steps)}
            zero = res_in_total == 0 and u_out == 0
        steps_nonzero = any(u.total_input or u.output for u in step_usage.values())
        if zero and steps_nonzero:
            run.dq["dq.headless_zeroed_result"] += 1
            return
        if cost_nano is not None:  # a provider estimate: an event, never a billed number
            run.events.append(LaneEvent(
                lane_key=main_key, ts_ms=ts, kind=LaneEventKind.COST_STATE,
                attrs=(("reported_total_nano", cost_nano), ("reporter", "claude_code.headless"))))
            run.touch_session(session_key, ts)
        if res_in_total * RESUMED_DEN > steps_in_total * RESUMED_NUM:
            run.dq["dq.headless_resumed_totals"] += 1
            return
        served: dict[str, tuple[str, str, str | None]] = {}
        for s in sorted(steps, key=lambda s: s.first):
            if s.served is not None:
                served[s.served[0]] = s.served[1:]
        residual_infs: list[Inference] = []
        request_id = stable_id("rq", "claude-code-headless", main_key + "#output-residual")
        for i, model in enumerate(sorted(res_out)):
            residual = res_out[model] - logged.get(model, 0)
            if residual < 0:
                run.dq["dq.headless_output_residual"] += 1
                run.dq_tokens["dq.headless_output_residual"] += -residual
                continue
            if residual == 0:
                continue
            raw = per_model_raw(msg.get("modelUsage"), model, run) or model
            # the residual is output of that model's steps: priced at the served tier, speed and
            # geo of its last step (standard when the stream has no step on the model)
            tier, speed, geo = served.get(model, ("standard", "standard", None))
            ctx = run.pricing(raw, tier, speed, geo, billing)
            residual_infs.append(Inference(
                inference_id=stable_id("inf", request_id, i), kind=InferenceKind.OUTPUT_RESIDUAL,
                usage=UsageBuckets(output=residual), pricing=ctx, usage_source=UsageSource.FINAL,
                billable=True))
        if not residual_infs:
            return
        attempt = Attempt(
            attempt_id=stable_id("at", request_id, 0), attempt_no=0, ts_start_ms=ts,
            ttft_ms=None, duration_ms=None, outcome=Outcome.OK, http_status=None, error_type=None,
            retry_layer=None, retry_after_ms=None, should_retry=None, provider_request_id=None,
            provider_message_id=None, model_served=None, stop_reason=None,
            inferences=tuple(residual_infs))
        attr = run.attribution(query_source="main",
                               billing_path=None if billing == "unknown" else billing)
        req = Request(request_id=request_id, session_key=session_key, lane_key=main_key,
                      seq=index, attribution=attr, params=run.params("", None, None, None),
                      attempts=(attempt,), source=self.source_ref(locator))
        run.emit_request(req)
        run.touch_session(session_key, ts)
        if billing == "subscription":
            run.dq["dq.subscription_allowance"] += 1

    def result_aggregates(self, sid: str, ts: int, per_model: dict[str, list[int]],
                          usage: Mapping[str, Any], cost_nano: int | None, billing: str) -> None:
        run = self.run
        rows: list[tuple[str, UsageBuckets, int | None]] = []
        if per_model:
            for model, v in sorted(per_model.items()):
                rows.append((model, UsageBuckets(uncached_input=v[0], cache_read=v[1],
                                                 cache_write_unknown=v[2], output=v[3],
                                                 web_search_requests=v[4]), v[5]))
        else:
            buckets, _ = normalize_anthropic_messages(usage)
            rows.append(("", buckets, cost_nano))
        channel = run.pricing("", "standard", "standard", None, billing).channel
        for model, buckets, cost in rows:
            dims = [("channel", channel)]
            if model:
                dims.append(("model", model))
            self.aggregates.append(UsageAggregate(
                agg_id=stable_id("ag", RESULT_SOURCE_KIND, sid, model),
                source_kind=RESULT_SOURCE_KIND, bucket_start_ms=ts, bucket_end_ms=ts,
                dims=tuple(dims), usage=buckets, reported_cost_nano=cost,
                reported_cost_basis="provider_estimate" if cost is not None else None,
                finality="final", fetched_ms=max(run.opts.now_ms, 0)))

    def result(self, digest: str, n_bytes: int) -> IngestResult:
        run = self.run
        run.stats["lines"] = self.loaded.lines
        self.scan()
        self.build_steps()
        self.build_results()
        caps: set[str] = set()
        if run.requests:
            caps |= {"usage_sequence", "iterations", "params"}
            if self.timed:
                caps.add("timing")
            if self.saw_split:
                caps.add("ttl_split")
            if run.opts.attribution.workload_class is not WorkloadClass.UNKNOWN:
                caps.add("workload")
        info = source_info(run, run.source_id, self.path, digest, n_bytes, ADAPTER_NAME)
        return build_result(run, info, capabilities=frozenset(caps), source_kind=ADAPTER_NAME,
                            aggregates=self.aggregates)


def _step_buckets(step: _Step) -> UsageBuckets:
    best = step.best or {"message": {"usage": {}}}
    buckets, _ = normalize_anthropic_messages(best["message"]["usage"])
    return buckets


def _model_usage(mu: object) -> dict[str, list[int]]:
    """``modelUsage`` → normalized model → [input, cache read, cache creation, output, web search,
    cost nano or None] (entries whose key normalizes to the same model are summed)."""
    from tokenbill.core.models import normalize_model

    out: dict[str, list[Any]] = {}
    if not isinstance(mu, dict):
        return out
    for key, v in sorted(mu.items(), key=lambda kv: str(kv[0])):
        if not isinstance(key, str) or not isinstance(v, dict):
            continue
        model = normalize_model(token(key) or "").model
        row = out.setdefault(model, [0, 0, 0, 0, 0, None])
        row[0] += _uint(v.get("inputTokens"))
        row[1] += _uint(v.get("cacheReadInputTokens"))
        row[2] += _uint(v.get("cacheCreationInputTokens"))
        row[3] += _uint(v.get("outputTokens"))
        row[4] += _uint(v.get("webSearchRequests"))
        cost = decimal_usd_to_nano(v.get("costUSD"))
        if cost is not None:
            row[5] = (row[5] or 0) + cost
    return out


def per_model_raw(mu: object, model: str, run: _Run) -> str | None:
    """The first raw ``modelUsage`` key that normalizes to *model* (kept as ``model_raw``)."""
    if not isinstance(mu, dict):
        return None
    for key in sorted(k for k in mu if isinstance(k, str)):
        raw = token(key)
        if raw is not None and run.model_id(raw).model == model:
            return raw
    return None


def iter_messages(path: Path) -> Iterator[dict[str, Any]]:
    """The SDK message objects of a headless file (for tests and tooling; content stays local)."""
    yield from (m for _, m in load_messages(_read_text(Path(path))).messages)

