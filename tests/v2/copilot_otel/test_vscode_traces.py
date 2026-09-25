"""``copilot-vscode-traces`` acceptance tests (addendum §5.13; CP-OTEL brief): the allowlisted,
read-only ``agent-traces.db`` reader and CP-VSCODE-shaped extracts."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tokenbill.adapters.copilot_otel import CopilotOtelAdapter
from tokenbill.adapters.copilot_vscode import SPAN_COLUMNS, VsCodeAgentTracesAdapter
from tokenbill.core.errors import SourceError
from tokenbill.core.facts import load as load_facts
from tokenbill.core.ids import copilot_lane_key, copilot_session_key
from tokenbill.core.records import Fidelity

from .helpers import (
    PRINCIPAL_KEY_ID,
    T0_MS,
    VSCODE_DUMP,
    DbSpan,
    build_db,
    by_message,
    chat_span,
    codes,
    extract_meta,
    inference,
    no_leak,
    opts,
    sql_trace,
    standard_spans,
)

ADAPTER = VsCodeAgentTracesAdapter()
ALLOWLIST = load_facts().copilot.vscode_traces.attribute_allowlist


def _db(tmp_path: Path, **kw: object) -> Path:
    return build_db(tmp_path / "agent-traces.db", standard_spans(), **kw)


def _usage(result) -> dict[str | None, tuple[int, ...]]:
    out = {}
    for mid, req in by_message(result).items():
        inf = inference(req)
        u = inf.usage
        out[mid] = (u.uncached_input, u.cache_read, u.cache_write_unknown, u.output,
                    inf.provider_reported_cost_nano)
    return out


def test_database_with_canary_content_yields_requests_through_the_allowlist(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _db(tmp_path)
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    with sql_trace(monkeypatch) as statements:
        result = ADAPTER.read(path, opts())
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before     # never written
    assert _usage(result) == {"resp_db_001": (2000, 15000, 3000, 700, 123_456_000),
                              "resp_db_002": (2000, 6000, 0, 400, 10_000_000)}
    reqs = by_message(result)
    assert inference(reqs["resp_db_002"]).pricing.routing == "auto"
    assert reqs["resp_db_002"].params.model_requested == "auto"
    session = copilot_session_key("conv-db-1")
    assert {r.session_key for r in result.requests} == {session}
    assert {r.lane_key for r in result.requests} == {copilot_lane_key(session, "main", None)}
    for req in result.requests:
        assert req.attribution.agent_product == "copilot_vscode"
        assert inference(req).pricing.billing_path == "copilot_pool"
        assert req.source.priority == 22 and req.source.fidelity is Fidelity.NO_TTL_SPLIT
        assert req.attempts[0].ttft_ms == 420
    assert result.capabilities == {"usage_sequence", "timing", "params", "credits"}
    no_leak(result)
    # the SQL trace: span_events never touched, one parameterized IN-list attribute query,
    # no tool_* column selected
    joined = "\n".join(statements)
    assert "span_events" not in joined
    attr_queries = [s for s in statements if "span_attributes" in s and "SELECT" in s.upper()
                    and "PRAGMA" not in s.upper()]
    assert len(attr_queries) == 1
    (query,) = attr_queries
    assert "WHERE key IN (" in query
    for key in ALLOWLIST:
        assert f"'{key}'" in query                       # bound parameters, expanded by trace
    assert "gen_ai.input.messages" not in query and "hook_input" not in query
    span_queries = [s for s in statements if "FROM spans" in s]
    assert len(span_queries) == 1 and "tool_" not in span_queries[0]
    assert "status_message" not in span_queries[0]


def test_extract_with_meta_principal(tmp_path: Path) -> None:
    raw = ADAPTER.read(_db(tmp_path), opts())
    extract_path = build_db(tmp_path / "vscode-extract.db", standard_spans(), content=False,
                            meta=extract_meta())
    extract = ADAPTER.read(extract_path, opts())
    assert _usage(extract) == _usage(raw)
    assert [r.request_id for r in extract.requests] == [r.request_id for r in raw.requests]
    assert {r.attribution.principal for r in extract.requests} == {"c_0123456789abcdef0123"}
    assert {r.attribution.team for r in extract.requests} == {"payments"}
    assert extract.source.principal_key_id == PRINCIPAL_KEY_ID
    assert extract.stats["collector_extract"] == 1
    assert "dq.copilot_collector_principal_invalid" not in codes(extract)


@pytest.mark.parametrize("principal", ["alice@example.com", "p_0123456789abcdef0123", "c_XYZ"])
def test_extract_invalid_principal_is_dropped(tmp_path: Path, principal: str) -> None:
    path = build_db(tmp_path / "x.db", standard_spans(), content=False,
                    meta=extract_meta(principal))
    result = ADAPTER.read(path, opts())
    assert {r.attribution.principal for r in result.requests} == {None}
    assert codes(result)["dq.copilot_collector_principal_invalid"] == 2
    assert "alice" not in repr(result)


def test_extract_r_principal_needs_no_key_and_c_principal_needs_one(tmp_path: Path) -> None:
    ref = ADAPTER.read(build_db(tmp_path / "r.db", standard_spans(), content=False,
                                meta=extract_meta("r_mdm.device-9", principal_key_id=None,
                                                  team=None)), opts())
    assert {r.attribution.principal for r in ref.requests} == {"r_mdm.device-9"}
    assert ref.source.principal_key_id is None
    assert {r.attribution.team for r in ref.requests} == {None}
    keyless = ADAPTER.read(build_db(tmp_path / "c.db", standard_spans(), content=False,
                                    meta=extract_meta(principal_key_id=None)), opts())
    assert {r.attribution.principal for r in keyless.requests} == {None}


def test_meta_keys_ignored_without_the_schema_marker(tmp_path: Path) -> None:
    path = build_db(tmp_path / "m.db", standard_spans(), content=False,
                    meta=extract_meta(schema="something/else@1"))
    result = ADAPTER.read(path, opts())
    assert {r.attribution.principal for r in result.requests} == {None}
    assert result.source.principal_key_id is None
    assert "collector_extract" not in result.stats


def test_same_requests_as_the_otel_dump_rules(tmp_path: Path) -> None:
    """The DB and the OTel file go through the one mapping: equal buckets for equal spans."""
    otel = CopilotOtelAdapter().read(VSCODE_DUMP, opts())
    db = ADAPTER.read(_db(tmp_path), opts())
    assert sorted(v[:4] for v in _usage(otel).values()) == sorted(v[:4]
                                                                   for v in _usage(db).values())


def test_synthesized_db_span_skipped_when_a_native_span_exists(tmp_path: Path) -> None:
    trace = "c" * 32
    spans = [
        chat_span("c1", trace, None, conv="k1", start=T0_MS, model="claude-sonnet-4.5",
                  resp="resp_n", inp=100, read=50, create=0, out=10, nano=100, turn=0),
        chat_span("c2", trace, None, conv="k1", start=T0_MS + 10, model="claude-sonnet-4.5",
                  resp=None, inp=100, read=50, create=0, out=10, nano=100, turn=0),
        chat_span("c3", "d" * 32, None, conv="k2", start=T0_MS + 20, model="gpt-5-mini",
                  resp=None, inp=10, read=0, create=0, out=1, nano=10, turn=0),
    ]
    result = ADAPTER.read(build_db(tmp_path / "s.db", spans), opts())
    assert set(by_message(result)) == {"resp_n", None}
    assert codes(result)["dq.copilot_synthesized_span_skipped"] == 1
    assert codes(result)["dq.copilot_synthesized_span_kept"] == 1


def test_typed_columns_fill_missing_attribute_rows(tmp_path: Path) -> None:
    span = DbSpan("e1", "e" * 32, None, "chat", T0_MS, T0_MS + 500, {},
                  {"operation_name": "chat", "request_model": "gpt-5-mini",
                   "response_model": "gpt-5-mini", "input_tokens": 900, "output_tokens": 40,
                   "cached_tokens": 600, "reasoning_tokens": 10, "chat_session_id": "chat-7",
                   "turn_index": 3}, status=2)
    result = ADAPTER.read(build_db(tmp_path / "t.db", [span], content=False), opts())
    (req,) = result.requests
    usage = inference(req).usage
    assert (usage.uncached_input, usage.cache_read, usage.output, usage.output_reasoning) == (
        300, 600, 40, 10)
    assert req.session_key == copilot_session_key("chat-7")
    assert req.attempts[0].error_type == "error"


def test_subagent_lane_from_nested_invoke_agent(tmp_path: Path) -> None:
    trace = "f" * 32
    root = DbSpan("f1", trace, None, "invoke_agent", T0_MS, T0_MS + 9000,
                  {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": "k9"})
    tool = DbSpan("f2", trace, "f1", "execute_tool", T0_MS + 10, T0_MS + 8000,
                  {"gen_ai.operation.name": "execute_tool"})
    sub = DbSpan("f3", trace, "f2", "invoke_agent", T0_MS + 20, T0_MS + 7000,
                 {"gen_ai.operation.name": "invoke_agent"}, {"agent_name": "Plan"})
    chat = chat_span("f4", trace, "f3", conv="k9", start=T0_MS + 30, model="gpt-5-mini",
                     resp="resp_sub", inp=10, read=0, create=0, out=1, nano=1, turn=0)
    result = ADAPTER.read(build_db(tmp_path / "sub.db", [root, tool, sub, chat]), opts())
    (req,) = result.requests
    session = copilot_session_key("k9")
    assert req.lane_key == copilot_lane_key(session, "subagent", "Plan")
    assert req.attribution.query_source == "subagent"


def test_empty_schema_and_unreadable_databases(tmp_path: Path) -> None:
    empty = ADAPTER.read(build_db(tmp_path / "empty.db", []), opts())
    assert empty.requests == [] and codes(empty)["dq.copilot_vscode_no_spans"] == 1
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("CREATE TABLE spans (span_id TEXT); "
                       "CREATE TABLE span_attributes (span_id TEXT, key TEXT, value TEXT);")
    conn.close()
    result = ADAPTER.read(old, opts())
    assert codes(result)["dq.copilot_vscode_schema"] == 1 and result.requests == []
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200 + b"span_attributes")
    with pytest.raises(SourceError):
        ADAPTER.read(junk, opts())
    with pytest.raises(SourceError):
        ADAPTER.read(tmp_path, opts())
    with pytest.raises(SourceError):
        ADAPTER.read(tmp_path / "missing.db", opts())


def test_sniff(tmp_path: Path) -> None:
    path = _db(tmp_path)
    assert ADAPTER.sniff(path, path.read_bytes()[:65536]) is True
    assert ADAPTER.sniff(path, b"SQLite format 3\x00 no tables") is False
    assert ADAPTER.sniff(VSCODE_DUMP, VSCODE_DUMP.read_bytes()) is False


def test_window_and_bad_rows(tmp_path: Path) -> None:
    spans = standard_spans()
    spans[3].attrs["gen_ai.usage.input_tokens"] = "-5"
    result = ADAPTER.read(build_db(tmp_path / "w.db", spans), opts(until_ms=T0_MS + 50))
    assert result.requests == [] and result.stats["out_of_window"] == 2
    result = ADAPTER.read(build_db(tmp_path / "w2.db", spans), opts())
    assert set(by_message(result)) == {"resp_db_001"}
    assert [q.reason for q in result.quarantined] == ["bad_usage"]


def test_span_columns_never_include_tool_columns() -> None:
    assert not any(c.startswith("tool_") for c in SPAN_COLUMNS)
    assert "span_id" in SPAN_COLUMNS and "chat_session_id" in SPAN_COLUMNS


def test_raw_database_and_its_extract_merge_in_a_store(tmp_path: Path) -> None:
    """One conversation seen through the raw database and a CP-VSCODE extract merges (the same
    request ids and provider message ids): no request is doubled."""
    from tokenbill.core.testing import FakePricer, MemoryStore

    store = MemoryStore(org_key=bytes(range(200, 232)), pricer=FakePricer())
    store.ingest(ADAPTER.read(_db(tmp_path), opts()))
    extract = build_db(tmp_path / "extract.db", standard_spans(), content=False,
                       meta=extract_meta())
    store.ingest(ADAPTER.read(extract, opts()))
    merged = [r for lane in store.iter_lanes() for r in lane.requests]
    assert sorted(r.attempts[0].provider_message_id for r in merged) == ["resp_db_001",
                                                                        "resp_db_002"]
