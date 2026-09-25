"""Hypothesis fuzz of the CP-LOCAL parsers: the ``events.jsonl`` reader (line recovery and every
handler), the store row mapper and the three convention normalizers. Only ``TokenbillError``
subclasses may escape (SPEC §21 #5); results are deterministic and serializable.
``TB_LOCAL_FUZZ_EXAMPLES`` raises the example count (default 60 per property)."""

from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.adapters import copilot_conventions as cc
from tokenbill.adapters.copilot_cli import (
    CopilotCliAdapter,
    map_store_row,
    parse_event_line,
    parse_ts_ms,
)
from tokenbill.adapters.copilot_collect import CopilotCollectorState, collect_incremental_copilot
from tokenbill.core.builders import CANARY
from tokenbill.core.errors import TokenbillError
from tokenbill.core.records import to_json

from .helpers import G11_DETAILS, iso, opts

NOW = 1_789_898_400_000 + 10**7

EXAMPLES = int(os.environ.get("TB_LOCAL_FUZZ_EXAMPLES", "60"))
FUZZ = settings(max_examples=EXAMPLES, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

TYPES = ("session.start", "session.resume", "session.context_changed",
         "session.session_limits_changed", "session.model_change", "session.auto_mode_resolved",
         "session.truncation", "session.error", "session.usage_checkpoint",
         "session.compaction_complete", "session.shutdown", "tool.execution_start",
         "tool.execution_complete", "user.message", "assistant.message", "assistant.turn_end",
         "brand.new_type", "")
KEYS = ("sessionId", "copilotVersion", "selectedModel", "contextTier", "reasoningEffort",
        "sessionLimits", "maxAiCredits", "context", "cwd", "repository", "newModel",
        "previousModel", "cause", "source", "tokensRemovedDuringTruncation", "errorType",
        "statusCode", "totalNanoAiu", "modelCacheState", "modelId", "cacheTtlSeconds", "success",
        "trigger", "preCompactionTokens", "postCompactionTokens", "tokensRemoved",
        "compactionTokensUsed", "copilotUsage", "tokenDetails", "tokenType", "tokenCount",
        "batchSize", "costPerBatch", "model", "duration", "modelMetrics", "usage", "inputTokens",
        "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens",
        "shutdownType", "toolCallId", "toolName", "result", "content", "apiCallId", "chunkIndex",
        "chunkCount", "requestId", "turnId", "sessionLimits")
SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(min_value=-2**60, max_value=2**60),
    st.decimals(allow_nan=False, allow_infinity=False, places=2),
    st.sampled_from(["claude-sonnet-4.5", "auto", "gpt-5.4", "long_context", "default",
                     "threshold", "manual", "input", "cache_read", "cache_write", "output",
                     "refusal_fallback", "automatic", "error", "0", "1", "chatcmpl-1", CANARY,
                     "2026-09-20T10:00:00Z", ""]),
    st.text(max_size=10))
JSONISH = st.recursive(
    SCALARS,
    lambda inner: st.one_of(st.lists(inner, max_size=4),
                            st.dictionaries(st.sampled_from(KEYS), inner, max_size=6)),
    max_leaves=30)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


@st.composite
def event(draw) -> dict[str, Any]:
    obj: dict[str, Any] = {"type": draw(st.sampled_from(TYPES)),
                           "data": draw(st.dictionaries(st.sampled_from(KEYS), JSONISH,
                                                        max_size=8)),
                           "id": draw(st.sampled_from(["e1", "e2", "e3", None])),
                           "timestamp": iso(1_789_898_400_000 + draw(st.integers(0, 10**7)))}
    if draw(st.booleans()):
        obj["agentId"] = draw(st.sampled_from(["a1", "a2", CANARY, ""]))
    if draw(st.integers(0, 9)) == 0:
        obj["timestamp"] = draw(SCALARS)
    if draw(st.integers(0, 9)) == 0:
        obj["data"] = draw(SCALARS)
    return obj


LINES = st.lists(st.one_of(
    event().map(lambda o: json.dumps(_jsonable(o)).encode()),
    st.binary(max_size=40),
    st.tuples(st.binary(max_size=12), event()).map(
        lambda t: t[0] + json.dumps(_jsonable(t[1])).encode())), max_size=14)


@FUZZ
@given(LINES)
def test_events_reader_never_crashes(lines: list[bytes]) -> None:
    adapter = CopilotCliAdapter()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "session-state" / "fz" / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\n".join(line.replace(b"\n", b" ") for line in lines))
        try:
            first = adapter.read(path, opts())
        except TokenbillError:
            return
        second = adapter.read(path, opts())
        assert to_json(first) == to_json(second)
        json.dumps(to_json(first))
        try:
            adapter.read(path, opts(lenient=False))
        except TokenbillError:
            pass
        # the collector's resumable path over the same bytes: never anything but TokenbillError
        state = CopilotCollectorState()
        try:
            list(collect_incremental_copilot(Path(tmp), state, opts(), now_ms=NOW))
            list(collect_incremental_copilot(Path(tmp), state, opts(), now_ms=NOW))
        except TokenbillError:
            pass


@FUZZ
@given(st.binary(max_size=200))
def test_parse_event_line_never_crashes(raw: bytes) -> None:
    objs, recovered = parse_event_line(raw)
    assert isinstance(objs, list) and isinstance(recovered, bool)
    parse_ts_ms(raw.decode("latin-1"))


ROW_VALUES = st.one_of(SCALARS, st.floats(allow_nan=True, allow_infinity=True),
                       st.binary(max_size=8))
COLUMNS = ("id", "session_id", "model", "input_tokens", "cache_read_tokens",
           "cache_write_tokens", "created_at", "turn_index", "copilot_usage_model",
           "output_tokens", "reasoning_tokens", "total_nano_aiu", "duration_ms", "initiator",
           "request_multiplier")


@FUZZ
@given(st.dictionaries(st.sampled_from(COLUMNS), ROW_VALUES, max_size=15),
       st.booleans())
def test_row_mapper_only_raises_tokenbill_errors(values: dict[str, Any], valid_base: bool
                                                 ) -> None:
    if valid_base:
        values = {"id": 1, "session_id": "s", "model": "gpt-5.4", "input_tokens": 10,
                  "cache_read_tokens": 2, "cache_write_tokens": 1,
                  "created_at": "2026-09-20T10:00:00Z", **values}
    try:
        mapped = map_store_row(values)
    except TokenbillError:
        return
    assert mapped.usage.total_input >= 0 and json.loads(mapped.raw_json) is not None


@FUZZ
@given(st.dictionaries(st.sampled_from(KEYS), JSONISH, max_size=6),
       st.lists(st.dictionaries(st.sampled_from(("tokenType", "tokenCount", "batchSize",
                                                 "costPerBatch", "model")), SCALARS, max_size=5),
                max_size=5))
def test_normalizers_only_raise_tokenbill_errors(raw: dict[str, Any],
                                                 details: list[dict[str, Any]]) -> None:
    for fn, arg in ((cc.normalize_shutdown_rollup, raw), (cc.normalize_session_store, raw),
                    (cc.normalize_token_details, {"tokenDetails": details,
                                                  "totalNanoAiu": raw.get("totalNanoAiu")}),
                    (cc.normalize_token_details, {"tokenDetails": G11_DETAILS + details})):
        try:
            buckets, notes = fn(arg)
        except TokenbillError:
            continue
        assert buckets.total_input >= 0 and all(n.startswith("dq.") for n in notes)
