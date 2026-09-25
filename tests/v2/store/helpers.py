"""Builders for the STORE tests (synthetic records only; imported only within this area).

Every record is built with the foundation builders (``core.builders``); keys are fixed byte
strings; timestamps sit on 2026-09-23 (Opus 5.5 and Sonnet 5 are priced by ``FakePricer`` that day).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tokenbill.core.builders import make_request
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.records import (
    Attribution,
    CostLine,
    Fidelity,
    Lane,
    LaneEvent,
    LaneKind,
    OutcomeAggregate,
    Request,
    Session,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    to_json,
)
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.core.types import IngestResult, SourceInfo
from tokenbill.store import schema
from tokenbill.store.db import SqliteStore

ORG_KEY = bytes(range(64, 96))
NAME_KEY = bytes(range(96, 128))
OTHER_NAME_KEY = bytes(range(128, 160))
DAY = "2026-09-23"
EPOCH = _dt.date(1970, 1, 1)
DAY_MS = 86_400_000
T0 = (_dt.date.fromisoformat(DAY) - EPOCH).days * DAY_MS + 9 * 3_600_000
FOREVER = {"since_ms": 0, "until_ms": 2**53}
NOW = T0 + DAY_MS


def ts_of(date: str, hour: int = 9) -> int:
    """Epoch ms of *date* at *hour*:00 UTC."""
    return (_dt.date.fromisoformat(date) - EPOCH).days * DAY_MS + hour * 3_600_000


def h(value: str, key: bytes = NAME_KEY) -> str:
    """An ``h_`` name hash under *key*."""
    return pseudonym(key, "h", value)


def p(ref: str, key: bytes = ORG_KEY) -> str:
    """The ``p_`` pseudonym the store makes of ``r_<ref>`` under *key*."""
    return pseudonym(key, "p", ref)


def src(source_id: str, adapter: str, *, name_key: bytes | None = NAME_KEY,
        principal_key: bytes | None = ORG_KEY, sha: str | None = None) -> SourceInfo:
    """A ``SourceInfo`` (sha256 of the source id unless *sha* is given)."""
    return SourceInfo(source_id=source_id, adapter=adapter, name_hmac=h(source_id),
                      sha256=sha or hashlib.sha256(source_id.encode()).hexdigest(), bytes=100,
                      name_key_id=key_id(name_key) if name_key is not None else None,
                      principal_key_id=key_id(principal_key) if principal_key else None)


def ref(adapter: str, source_id: str, fidelity: Fidelity, priority: int, n: int) -> SourceRef:
    return SourceRef(adapter=adapter, source_id=source_id, locator=f"line:{n}",
                     fidelity=fidelity, priority=priority)


def result(source: SourceInfo, requests: Sequence[Request] = (), *,
           sessions: Sequence[Session] = (), events: Sequence[LaneEvent] = (),
           aggregates: Sequence[UsageAggregate] = (), cost_lines: Sequence[CostLine] = (),
           outcomes: Sequence[OutcomeAggregate] = (),
           stats: Mapping[str, int] | None = None) -> IngestResult:
    return IngestResult(source=source, requests=list(requests), sessions=list(sessions),
                        events=list(events), aggregates=list(aggregates),
                        cost_lines=list(cost_lines), outcomes=list(outcomes), quarantined=[],
                        notes=[], stats=dict(stats or {"records": len(requests)}),
                        capabilities=frozenset({"usage_sequence", "timing"}))


def shell(lane_key: str, session_key: str, kind: LaneKind | str, scope: str = "ws:w1",
          parent: str | None = None) -> Session:
    lane = Lane(lane_key=lane_key, session_key=session_key, kind=kind, parent_lane_key=parent,
                cache_scope_key=scope, requests=())
    return Session(session_key=session_key, source_kind="test", attribution=Attribution(),
                   lanes=(lane,), started_ms=0, ended_ms=0)


def with_hint(req: Request, rq: str) -> Request:
    """*req* with provider request id *rq* on its first attempt."""
    att = dataclasses.replace(req.attempts[0], provider_request_id=rq)
    return dataclasses.replace(req, attempts=(att, *req.attempts[1:]))


# ---------------------------------------------------------------------------------------------
# the five acceptance sources (transcript-, OTel-, trace@2-, trace@1- and responses-shaped)
# ---------------------------------------------------------------------------------------------

CC_ATTR = {"principal": "r_alice", "team": "payments", "agent_product": "claude_code",
           "cwd_key": h("/repo/a"), "workload_class": "interactive", "billing_path": "api_key"}


def transcript_source() -> IngestResult:
    s = src("s_transcript", "claude-code")

    def cc(seq: int, ts: int, usage: dict[str, int], msg: str, rq: str, lane: str = "L-main",
           model: str = "claude-opus-5-5", attr: Mapping[str, Any] = CC_ATTR) -> Request:
        req = make_request(lane, seq, ts, usage, model, session_key="S-cc",
                           request_id=stable_id("rq", "anthropic", msg), message_id=msg,
                           attribution=dict(attr),
                           source=ref("claude-code", "s_transcript", Fidelity.FULL, 40, seq),
                           billing_path="api_key")
        return with_hint(req, rq) if rq else req

    reqs = [
        cc(0, T0, {"cache_write_5m": 100_000, "output": 500}, "msg_m0", "req_q0"),
        cc(1, T0 + 420_000, {"cache_write_5m": 102_000, "output": 500}, "msg_m1", "req_q1"),
        # split entry: the same message logged twice, the second with the final output
        cc(2, T0 + 450_000, {"cache_read": 102_000, "cache_write_1h": 2000, "output": 10},
           "msg_m2", "req_q2"),
        cc(2, T0 + 450_000, {"cache_read": 102_000, "cache_write_1h": 2000, "output": 500},
           "msg_m2", "req_q2"),
        # one requestId seen with two message ids: never a join key
        cc(3, T0 + 480_000, {"cache_read": 104_000, "output": 300}, "msg_m5", "req_dup"),
        cc(4, T0 + 510_000, {"cache_read": 104_000, "output": 200}, "msg_m6", "req_dup"),
        cc(0, T0 + 60_000, {"cache_write_5m": 20_000, "output": 100}, "msg_sub0", "",
           lane="L-sub", model="claude-sonnet-5", attr={**CC_ATTR, "principal": "r_bob"}),
    ]
    return result(s, reqs, sessions=[shell("L-main", "S-cc", LaneKind.MAIN),
                                     shell("L-sub", "S-cc", LaneKind.SUBAGENT,
                                           parent="L-main")],
                  events=[LaneEvent(lane_key="L-main", ts_ms=T0 + 400_000, kind="human_prompt")])


OTEL_ATTR = {"principal": "r_alice", "team": "payments", "cost_center": "cc-1",
             "extra": (("mdm_group", "g1"), ("task_id", "t-42"))}


def otel_source() -> IngestResult:
    s = src("s_otel", "otlp")

    def otel(n: int, ts: int, rq: str) -> Request:
        req = make_request("L-otel", n, ts, {"cache_read": 50_000, "cache_write_unknown": 52_000,
                                             "output": 500}, "claude-opus-5-5",
                           session_key="S-otel", request_id=stable_id("rq", "s_otel", n),
                           attribution=dict(OTEL_ATTR),
                           source=ref("otlp", "s_otel", Fidelity.NO_TTL_SPLIT, 20, n))
        return with_hint(req, rq)

    return result(s, [otel(1, T0 + 420_000, "req_q1"), otel(7, T0 + 480_000, "req_dup"),
                      otel(9, T0 + 900_000, "req_q9")],
                  sessions=[shell("L-otel", "S-otel", LaneKind.UNKNOWN, "unknown")])


SDK_ATTR = {"principal": "r_carol", "team": "platform", "agent_product": "agent_sdk",
            "workload_class": "ci", "repo": h("repo-x"), "extra": (("mdm_group", "g2"),)}


def _sdk(n: int, ts: int, usage: dict[str, int], adapter: str, source_id: str, priority: int,
         attr: Mapping[str, Any] = SDK_ATTR) -> Request:
    return make_request("L-sdk", n, ts, usage, "claude-sonnet-5", session_key="S-sdk",
                        request_id=stable_id("rq", "anthropic", f"msg_s{n}"),
                        message_id=f"msg_s{n}", attribution=dict(attr),
                        source=ref(adapter, source_id, Fidelity.FULL, priority, n))


def aggregate_record(fetched_ms: int = 0, finality: str = "provisional",
                     output: int = 5) -> UsageAggregate:
    start = ts_of(DAY, 0)
    return UsageAggregate(agg_id="ag_1", source_kind="anthropic.usage_report",
                          bucket_start_ms=start, bucket_end_ms=start + DAY_MS,
                          dims=(("api_key_id", h("key-1")), ("channel", "anthropic_api"),
                                ("model", "claude-opus-5-5")),
                          usage=UsageBuckets(uncached_input=10, output=output),
                          finality=finality, fetched_ms=fetched_ms)


def cost_line_record(amount: int = 1_000_000, **kw: Any) -> CostLine:
    fields: dict[str, Any] = {
        "line_id": "cl_1", "source_kind": "anthropic.cost_report", "date_utc": DAY,
        "channel": "anthropic_api", "workspace_id": None,
        "description": "Claude Opus 5.5 output", "model": "claude-opus-5-5",
        "cost_type": "tokens", "token_type": "output", "sku": None, "service_tier": None,
        "inference_geo": None, "endpoint_scope": None, "amount_nano": amount}
    fields.update(kw)
    return CostLine(**fields)


def trace2_source() -> IngestResult:
    s = src("s_trace2", "trace@2")
    return result(s, [
        _sdk(0, T0 + 1000, {"cache_write_5m": 30_000, "output": 800}, "trace@2", "s_trace2", 50),
        _sdk(1, T0 + 31_000, {"cache_read": 30_000, "cache_write_5m": 1000, "output": 800},
             "trace@2", "s_trace2", 50)],
        sessions=[shell("L-sdk", "S-sdk", LaneKind.API_RUN)],
        aggregates=[aggregate_record()], cost_lines=[cost_line_record()],
        outcomes=[OutcomeAggregate(date_utc=DAY, team="platform", n_users=6, sessions=10,
                                   commits=3, pull_requests=2, lines_added=100, lines_removed=20,
                                   edits_accepted=5, edits_rejected=1)])


def trace1_source(source_id: str = "s_trace1", *, name_key: bytes = OTHER_NAME_KEY,
                  run_id: str = "run-1") -> IngestResult:
    """A trace@1-shaped source: ``session_key = stable_id("ses", source_id, run_id)``."""
    s = src(source_id, "trace@1", name_key=name_key)
    session = stable_id("ses", source_id, run_id)
    lane = stable_id("ln", session, run_id)
    attr = {"principal": "p_" + "0" * 20, "team": "data", "repo": h("repo-y", name_key),
            "workload_class": "batch"}
    reqs = [make_request(lane, n, T0 + 5000 * n, {"uncached_input": 3000, "output": 100},
                         "claude-haiku-4-5", session_key=session,
                         request_id=stable_id("rq", source_id, f"line:{n}"), attribution=attr,
                         source=ref("trace@1", source_id, Fidelity.FULL, 10, n))
            for n in range(2)]
    return result(s, reqs, sessions=[shell(lane, session, LaneKind.API_RUN, "unknown")])


def responses_source() -> IngestResult:
    s = src("s_responses", "anthropic-responses")
    resp_attr = {"principal": "r_carol", "team": "platform", "agent_product": "agent_sdk",
                 "project": "proj-1"}
    return result(s, [
        _sdk(0, T0 + 1000, {"cache_write_5m": 30_000, "output": 700}, "anthropic-responses",
             "s_responses", 35, resp_attr),
        make_request("L-allow", 0, T0 + 7000, {"uncached_input": 1000, "output": 100},
                     "claude-opus-5-5", session_key="S-allow",
                     request_id=stable_id("rq", "anthropic", "msg_a0"), message_id="msg_a0",
                     attribution={"principal": "r_alice", "team": "payments",
                                  "billing_path": "subscription"},
                     billing_path="subscription",
                     source=ref("anthropic-responses", "s_responses", Fidelity.FULL, 35, 9)),
    ], sessions=[shell("L-allow", "S-allow", LaneKind.MAIN)])


def five_sources() -> list[IngestResult]:
    """Transcript-, OTel-, trace@2-, trace@1- and responses-shaped sources."""
    return [transcript_source(), otel_source(), trace2_source(), trace1_source(),
            responses_source()]


# ---------------------------------------------------------------------------------------------
# stores and dumps
# ---------------------------------------------------------------------------------------------

class Stores:
    """Opens fresh ledgers under one directory (``stores.open(**kw)``)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.n = 0
        self.opened: list[SqliteStore] = []

    def open(self, **kw: Any) -> SqliteStore:
        self.n += 1
        kw.setdefault("now_ms", NOW)
        store = SqliteStore(self.root / f"s{self.n}" / "tokenbill.db", **kw)
        self.opened.append(store)
        return store

    def close(self) -> None:
        for store in self.opened:
            store.close()


def keyed(stores: Stores, **kw: Any) -> SqliteStore:
    """A store with the org key, the name key id fixed up front and ``FakePricer``."""
    kw.setdefault("org_key", ORG_KEY)
    kw.setdefault("name_key_id", key_id(NAME_KEY))
    kw.setdefault("pricer", FakePricer())
    return stores.open(**kw)


def memory(**kw: Any) -> MemoryStore:
    kw.setdefault("org_key", ORG_KEY)
    kw.setdefault("name_key_id", key_id(NAME_KEY))
    kw.setdefault("pricer", FakePricer())
    kw.setdefault("now_ms", NOW)
    return MemoryStore(**kw)


def data_dump(store: SqliteStore, tables: Iterable[str] = schema.DATA_TABLES) -> list[str]:
    """The ``iterdump()`` INSERT statements of the data tables, sorted (SQLite dumps rows in
    rowid order, i.e. insertion order; the content is what order independence is about)."""
    wanted = {f'INSERT INTO "{t}"' for t in tables}
    return sorted(line for line in store.connection.iterdump()
                  if line.split(" VALUES")[0] in wanted)


def canon(obj: Any) -> str:
    return json.dumps(to_json(obj) if dataclasses.is_dataclass(obj) else obj, sort_keys=True,
                      separators=(",", ":"))


def api_dump(store: Any) -> dict[str, list[str]]:
    """Everything the protocol returns over all time, canonically encoded (comparable between a
    ``SqliteStore`` and a ``MemoryStore``)."""
    return {
        "requests": [canon(r) for r in store.iter_requests(**FOREVER)],
        "records": [canon(r) for r in store.iter_usage_records(**FOREVER)],
        "lanes": [canon(lane) for lane in store.iter_lanes(**FOREVER)],
        "lane_index": [canon(r) for r in store.lane_index(**FOREVER)],
        "first_reads": [canon(list(t)) for t in store.lane_first_reads(**FOREVER)],
        "aggregates": [canon(a) for a in store.aggregates(**FOREVER)],
        "cost_lines": [canon(c) for c in store.cost_lines(**FOREVER)],
        "outcomes": [canon(o) for o in store.outcomes(**FOREVER)],
    }
