"""``SqliteStore``: the SQLite ledger (SPEC §7.2) implementing ``core.protocols.LedgerStore`` and
``core.protocols.LedgerStats``.

One file per ledger, created ``0600`` in a ``0700`` directory, WAL journal. Money is int nano-USD in
INTEGER columns, so every SQL ``SUM`` is exact. Semantics follow the executable specification
``core.testing.MemoryStore``:

* **Privacy at write time** (§7.2, §7.6, R-E21): ``r_`` / ``c_`` principals become ``p_`` with the
  org key before anything is written (``PrivacyError`` without one); ``p_`` values are kept only
  under the store's org key id (or the adopted Copilot export key id) and ``h_`` names only under
  its name key id, otherwise they are nulled (``dq.principal_key_mismatch`` /
  ``dq.name_key_mismatch``).
* **Merge** (§7.3, ``store.merge``): a request seen by several sources is one row, recomputed from
  the set of its contributions whenever a new one joins, so ingest is idempotent and
  order-independent. Requests that only one source ever saw take a fast path (no JSON, no canonical
  form).
* **Pricing** (§7.2): each ingest prices the usage sets it stores with its pricer (else the
  constructor's): point, exact, estimated and per-bucket columns plus the priced lines;
  ``reprice`` recomputes a window.
* **Reading**: lanes stream lane by lane (memory bounded by the largest lane); filters are
  parameterized SQL over whitelisted dimensions; ``aggregate`` and ``count_users`` never return
  identities and refuse person dimensions (``PrivacyError``).

``read_only=True`` opens the file read-only (shard worker processes); every write raises
``UsageError``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import time
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.ids import key_id, stable_id
from tokenbill.core.jsonl import open_private
from tokenbill.core.kanon import PERSON_DIMS
from tokenbill.core.labels import Basis, Evidence, Figure, exact
from tokenbill.core.lanes import _ttl_observed
from tokenbill.core.money import ratio
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    COPILOT_BILLING_PATHS,
    AppendedItem,
    Attempt,
    Attribution,
    CacheDiagnostic,
    ContentFingerprint,
    CostLine,
    Fidelity,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Outcome,
    OutcomeAggregate,
    PricingContext,
    Request,
    RequestParams,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageRecord,
    UsageSource,
    WorkloadClass,
    from_json,
    to_json,
)
from tokenbill.core.types import (
    AggRow,
    ClusterDay,
    Finding,
    IngestResult,
    LaneIndexRow,
    LedgerCostRow,
    PricedInference,
    PricedTotal,
    RawAggregate,
    ReceiptRow,
    SourceInfo,
)
from tokenbill.store import retention, rollups, schema
from tokenbill.store.merge import (
    Contribution,
    MergedGroup,
    canonical,
    choose_version,
    merge_contributions,
    shell_rank,
    sources_mask,
)
from tokenbill.store.pseudonym import Pseudonymizer, is_name_hash

__all__ = ["ADOPTABLE_ADAPTER", "BATCH_ROWS", "SqliteStore"]

logger = logging.getLogger(__name__)

#: Rows per ingest transaction (SPEC §7.2).
BATCH_ROWS = 5_000
#: The only adapter whose key ids a store opened with ``adopt_key_ids=True`` adopts (R-E21).
ADOPTABLE_ADAPTER = "copilot-export"
_FOREVER_MS = 2**53
_IN = 400                                # keys per IN (…) list (old SQLite limit is 999)
_CACHE_MAX = 50_000
_DQ_NAME = "dq.name_key_mismatch"
_DQ_PRINCIPAL = "dq.principal_key_mismatch"
_DQ_COLLISION = "dq.request_id_collision"
_DQ_MISMATCH = "dq.cross_source_usage_mismatch"
_HASHED_ATTR = ("repo", "workspace_id", "api_key_id", "skill", "mcp_server", "plugin", "cwd_key")
_COST_LINE_HASHED = ("workspace_id", "repo", "workflow")
_COPILOT_SQL = "('copilot_pool', 'copilot_direct')"
_BILLING_CLASS_SQL = ("CASE WHEN {0} = 'subscription' THEN 'allowance' WHEN {0} IN "
                      + _COPILOT_SQL + " THEN 'pool' ELSE 'billed' END")

# ---------------------------------------------------------------------------------------------
# dimensions (the MemoryStore vocabulary)
# ---------------------------------------------------------------------------------------------

#: ``aggregate`` group-by dimensions (SPEC §7.2 whitelist).
AGG_DIMS = ("date", "team", "cost_center", "workspace_id", "workload_class", "lane_kind", "model",
            "agent_type", "agent_product", "repo", "arm", "wave", "skill", "mcp_server",
            "billing_path", "channel")
#: ``cost_rows`` group-by dimensions (SPEC §3.6).
COST_DIMS = ("date", "provider", "channel", "model", "team", "cost_center", "project",
             "workspace_id", "lane_kind", "workload_class", "agent_product", "billing_path")
#: ``where`` keys of every request filter.
WHERE_KEYS = frozenset(AGG_DIMS) | {"billing_class", "provider", "project"}
#: ``count_users(source="cost_lines")`` filter keys (C-27).
COST_LINE_WHERE_KEYS = frozenset({"team", "cost_center", "channel", "model", "sku",
                                  "workspace_id", "cost_type"})
#: Lane-level ``iter_lanes`` filter keys (denormalized on ``lanes``).
_LANE_KEYS = frozenset({"team", "lane_kind", "billing_class"})
_CLUSTER_KINDS = tuple(rollups.CLUSTER_KINDS)

_LANE_KIND_SQL = "COALESCE(l.kind, 'unknown')"
#: Request-level SQL expression per dimension (alias ``r`` = requests, ``l`` = lanes).
_REQ_DIM_SQL = {
    "date": "r.date_utc", "team": "r.team", "cost_center": "r.cost_center",
    "project": "r.project", "workspace_id": "r.workspace_id", "workload_class": "r.workload_class",
    "lane_kind": _LANE_KIND_SQL, "model": "r.req_model", "agent_type": "r.agent_type",
    "agent_product": "r.agent_product", "repo": "r.repo", "arm": "r.arm", "wave": "r.wave",
    "skill": "r.skill", "mcp_server": "r.mcp_server", "billing_path": "r.eff_billing_path",
    "channel": "r.serving_channel", "provider": "r.serving_provider",
    "billing_class": _BILLING_CLASS_SQL.format("r.eff_billing_path"),
}
#: Inference-level SQL expression per dimension (alias ``i`` = inferences).
_INF_DIM_SQL = dict(_REQ_DIM_SQL, date="i.date_utc", model="NULLIF(i.model, '')",
                    billing_path="i.billing_path", channel="i.channel", provider="i.provider",
                    billing_class=_BILLING_CLASS_SQL.format("i.billing_path"))
_LANE_DIM_SQL = {"team": "l.team", "lane_kind": _LANE_KIND_SQL, "billing_class": "l.billing_class"}

# ---------------------------------------------------------------------------------------------
# column layouts
# ---------------------------------------------------------------------------------------------

_REQ_COLS = ("request_id", "lane_key", "session_key", "seq", "ts_start_ms", "date_utc", "fidelity",
             "source_priority", "adapter", "sources_mask", "principal", "team", "cost_center",
             "project", "repo", "workspace_id", "api_key_id", "agent_product", "agent_type",
             "query_source", "skill", "mcp_server", "plugin", "workload_class", "entrypoint",
             "client_version", "billing_path", "cwd_key", "arm", "wave", "attr_extra_json",
             "attr_prio_json", "params_json", "appended_json", "fp_json", "source_json",
             "req_model", "eff_billing_path", "serving_channel", "serving_provider",
             "serving_cache_read")
_ATTR_SLICE = slice(10, 31)          # principal … attr_extra_json
_ATT_COLS = ("attempt_id", "request_id", "attempt_no", "ts_start_ms", "ttft_ms", "duration_ms",
             "outcome", "http_status", "error_type", "retry_layer", "retry_after_ms",
             "should_retry", "sdk_retry_count", "provider_request_id", "provider_message_id",
             "model_served", "stop_reason", "diag_reason", "diag_provider_reason",
             "diag_missed_tokens", "diag_source", "applied_edits_json", "thinking_dropped",
             "raw_usage_json", "convention_id", "ord")
_INF_COLS = ("inference_id", "attempt_id", "request_id", "lane_key", "lane_kind", "ts_ms",
             "date_utc", "kind", "usage_source", "billable", "billing_rule_id", "output_upper",
             "provider", "channel", "model", "model_raw", "service_tier", "speed", "inference_geo",
             "endpoint_scope", "write_ttl_hint", "billing_path", "routing", "compliance",
             "context_tier", "uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
             "cache_write_other", "cache_write_other_ttl_s", "cache_write_unknown", "output",
             "output_reasoning", "web_search_requests", "web_fetch_requests", "provider_cost_nano",
             "provider_cost_basis", "team", "principal", "workspace_id", "workload_class",
             "agent_product", "ord")
_PRICED_COLS = ("priced_nano", "exact_nano", "est_nano", "est_low_nano", "est_high_nano",
                "evidence", "basis", "unpriced_reason", "nano_uncached", "nano_read", "nano_w5m",
                "nano_w1h", "nano_wother", "nano_wunknown", "nano_output", "nano_server_tools",
                "exact_mask", "rate_card_sha", "lines_json")
_BUCKET_ORDER = ("nano_uncached", "nano_read", "nano_w5m", "nano_w1h", "nano_wother",
                 "nano_wunknown", "nano_output", "nano_server_tools")
_NOT_BILLABLE = (None, 0, 0, 0, 0, None, None, None) + (None,) * 8 + (None, None, None)
_CL_COLS = ("line_id", "source_kind", "date_utc", "channel", "workspace_id", "description",
            "model", "cost_type", "token_type", "sku", "service_tier", "inference_geo",
            "endpoint_scope", "amount_nano", "list_amount_nano", "currency", "finality",
            "principal", "fetched_ms", "quantity", "unit", "cost_center", "team", "repo",
            "workload", "workflow", "routing", "speed", "pseudo")
_AGG_COLS = ("agg_id", "source_kind", "bucket_start_ms", "bucket_end_ms", "dims_json",
             "usage_json", "reported_cost_nano", "reported_cost_basis", "list_cost_nano",
             "finality", "fetched_ms")
_OUT_COLS = ("date_utc", "team", "source_kind", "n_users", "sessions", "commits",
             "pull_requests", "lines_added", "lines_removed", "edits_accepted", "edits_rejected",
             "extra_json")
_LANE_COLS = ("lane_key", "session_key", "kind", "parent_lane_key", "cache_scope_key",
              "ttl_observed", "lane_exact", "team", "billing_class")
_MEMBER_COLS = ("request_id", "member_key", "orig_request_id", "adapter", "source_id",
                "fidelity", "priority", "principal", "ts_start_ms", "doc")


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _insert_sql(table: str, cols: Sequence[str], verb: str = "INSERT") -> str:
    return f"{verb} INTO {table}({', '.join(cols)}) VALUES ({_marks(len(cols))})"


_REQ_INSERT = _insert_sql("requests", _REQ_COLS)
_ATT_INSERT = _insert_sql("attempts", _ATT_COLS)
_INF_INSERT = _insert_sql("inferences", _INF_COLS + _PRICED_COLS)
_MEMBER_INSERT = _insert_sql("merge_members", _MEMBER_COLS)
_REQ_SELECT = ", ".join(f"r.{c}" for c in _REQ_COLS)
_ATT_SELECT = ", ".join(_ATT_COLS)
_INF_SELECT = ", ".join(_INF_COLS)


def _chunks(items: Sequence[Any], n: int = _IN) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _json_or_none(value: Any) -> str | None:
    return canonical(value) if value else None


def _bool_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _int_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _canonical_record(obj: Any) -> str:
    return canonical(to_json(obj))


# ---------------------------------------------------------------------------------------------
# priced columns
# ---------------------------------------------------------------------------------------------


def _priced_tuple(p: PricedInference, sha: str | None) -> tuple[Any, ...]:
    """The priced columns of one inference (``_PRICED_COLS`` order)."""
    fig = p.figure
    if fig.nano is None:
        reason = p.unpriced_reason or fig.note.removeprefix("unpriced:").strip() or "unpriced"
        return ((None, 0, 0, 0, 0, fig.evidence.value, fig.basis.value, reason)
                + (None,) * 8 + (None, sha, None))
    est = p.estimated
    est_nano = est.nano if est is not None and est.nano is not None else 0
    est_low = est.low_nano if est is not None and est.low_nano is not None else 0
    est_high = est.high_nano if est is not None and est.high_nano is not None else 0
    per: dict[str, int] = dict.fromkeys(_BUCKET_ORDER, 0)
    inexact: set[str] = set()
    seen: set[str] = set()
    lines = []
    for ln in p.lines:
        col = schema.BUCKET_COLUMNS.get(ln.bucket)
        if col is not None:
            per[col] += ln.amount_nano
            seen.add(col)
            if not ln.exact:
                inexact.add(col)
        lines.append([ln.bucket, ln.quantity, ln.amount_nano, ln.low_nano, ln.high_nano,
                      1 if ln.exact else 0, ln.rate_row_id])
    mask = 0
    for col in seen - inexact:
        mask |= schema.EXACT_MASK_BITS[col]
    return ((fig.nano, p.exact_nano, est_nano, est_low, est_high, fig.evidence.value,
             fig.basis.value, None) + tuple(per[c] for c in _BUCKET_ORDER)
            + (mask, sha, json.dumps(lines, separators=(",", ":"))))


def _no_pricer_tuple() -> tuple[Any, ...]:
    return ((None, 0, 0, 0, 0, Evidence.EXACT.value, Basis.LIST.value, "no pricer")
            + (None,) * 8 + (None, None, None))


# ---------------------------------------------------------------------------------------------
# the row codec (records ⇄ rows), with small caches for the values lanes repeat
# ---------------------------------------------------------------------------------------------


class _Codec:
    """Encodes records to rows and decodes rows to records (lossless: a single-contribution
    request decodes to exactly the request that was stored)."""

    def __init__(self) -> None:
        self._params: dict[Any, Any] = {}
        self._params_text: dict[str, RequestParams] = {}
        self._attr: dict[tuple[Any, ...], Attribution] = {}
        self._ctx: dict[tuple[Any, ...], PricingContext] = {}

    def _cached(self, cache: dict, key: Any, build: Any) -> Any:
        value = cache.get(key)
        if value is None:
            if len(cache) > _CACHE_MAX:
                cache.clear()
            value = cache[key] = build()
        return value

    # ---- encode ----

    def params_json(self, params: RequestParams) -> str:
        return self._cached(self._params, params, lambda: _canonical_record(params))

    @staticmethod
    def source_json(ref: SourceRef | None) -> str | None:
        if ref is None:
            return None
        return json.dumps([ref.adapter, ref.source_id, ref.locator, int(ref.fidelity),
                           ref.priority], separators=(",", ":"), ensure_ascii=False)

    def request_row(self, req: Request, *, fidelity: int, priority: int, adapter: str, mask: int,
                    prio: Mapping[str, int] | None) -> tuple[Any, ...]:
        a = req.attribution
        si = req.serving_inference
        ctx = si.pricing if si is not None else None
        eff = a.billing_path or (ctx.billing_path if ctx is not None else None)
        ts = req.ts_start_ms
        return (req.request_id, req.lane_key, req.session_key, req.seq, ts, rollups.date_of(ts),
                fidelity, priority, adapter, mask, a.principal, a.team, a.cost_center, a.project,
                a.repo, a.workspace_id, a.api_key_id, a.agent_product, a.agent_type,
                a.query_source, a.skill, a.mcp_server, a.plugin, a.workload_class.value,
                a.entrypoint, a.client_version, a.billing_path, a.cwd_key, a.arm, a.wave,
                _json_or_none([list(p) for p in a.extra]),
                canonical(dict(prio)) if prio else None,
                self.params_json(req.params),
                _json_or_none([to_json(x) for x in req.appended]),
                _canonical_record(req.fingerprint) if req.fingerprint is not None else None,
                self.source_json(req.source), req.model, eff,
                ctx.channel if ctx is not None else None,
                ctx.provider if ctx is not None else None,
                si.usage.cache_read if si is not None else None)

    @staticmethod
    def attempt_row(att: Attempt, request_id: str, ord_: int) -> tuple[Any, ...]:
        d = att.diagnostics
        return (att.attempt_id, request_id, att.attempt_no, att.ts_start_ms, att.ttft_ms,
                att.duration_ms, att.outcome.value, att.http_status, att.error_type,
                att.retry_layer, att.retry_after_ms, _bool_int(att.should_retry),
                att.sdk_retry_count, att.provider_request_id, att.provider_message_id,
                att.model_served, att.stop_reason,
                d.reason if d is not None else None,
                d.provider_reason if d is not None else None,
                d.missed_input_tokens_estimate if d is not None else None,
                d.source if d is not None else None,
                _json_or_none([list(e) for e in att.applied_edits]), att.thinking_dropped,
                att.raw_usage_json, att.convention_id, ord_)

    @staticmethod
    def inference_row(inf: Inference, att: Attempt, req: Request, lane_kind: str,
                      ord_: int) -> tuple[Any, ...]:
        u = inf.usage
        c = inf.pricing
        a = req.attribution
        return (inf.inference_id, att.attempt_id, req.request_id, req.lane_key, lane_kind,
                att.ts_start_ms, rollups.date_of(att.ts_start_ms), inf.kind.value,
                inf.usage_source.value, _bool_int(inf.billable), inf.billing_rule_id,
                inf.output_upper, c.provider, c.channel, c.model, c.model_raw, c.service_tier,
                c.speed, c.inference_geo, c.endpoint_scope, c.write_ttl_hint, c.billing_path,
                c.routing, c.compliance, c.context_tier, u.uncached_input, u.cache_read,
                u.cache_write_5m, u.cache_write_1h, u.cache_write_other,
                u.cache_write_other_ttl_s, u.cache_write_unknown, u.output, u.output_reasoning,
                u.web_search_requests, u.web_fetch_requests, inf.provider_reported_cost_nano,
                inf.provider_reported_cost_basis, a.team, a.principal, a.workspace_id,
                a.workload_class.value, a.agent_product, ord_)

    # ---- decode ----

    def params(self, text: str) -> RequestParams:
        return self._cached(self._params_text, text,
                            lambda: from_json(RequestParams, json.loads(text)))

    def attribution(self, vals: tuple[Any, ...]) -> Attribution:
        def build() -> Attribution:
            (principal, team, cost_center, project, repo, workspace_id, api_key_id,
             agent_product, agent_type, query_source, skill, mcp_server, plugin, workload,
             entrypoint, client_version, billing_path, cwd_key, arm, wave, extra) = vals
            return Attribution(
                principal=principal, team=team, cost_center=cost_center, project=project,
                repo=repo, workspace_id=workspace_id, api_key_id=api_key_id,
                agent_product=agent_product, agent_type=agent_type, query_source=query_source,
                skill=skill, mcp_server=mcp_server, plugin=plugin,
                workload_class=WorkloadClass(workload), entrypoint=entrypoint,
                client_version=client_version, billing_path=billing_path, cwd_key=cwd_key,
                arm=arm, wave=wave,
                extra=tuple(tuple(p) for p in json.loads(extra)) if extra else ())
        return self._cached(self._attr, vals, build)

    @staticmethod
    def source(text: str | None) -> SourceRef | None:
        if text is None:
            return None
        adapter, source_id, locator, fidelity, priority = json.loads(text)
        return SourceRef(adapter=adapter, source_id=source_id, locator=locator,
                         fidelity=Fidelity(fidelity), priority=priority)

    def context(self, row: Sequence[Any], at: int) -> PricingContext:
        vals = tuple(row[at:at + 13])

        def build() -> PricingContext:
            (provider, channel, model, model_raw, tier, speed, geo, scope, hint, path, routing,
             compliance, context_tier) = vals
            return PricingContext(provider=provider, channel=channel, model=model,
                                  model_raw=model_raw, service_tier=tier, speed=speed,
                                  inference_geo=geo, endpoint_scope=scope, write_ttl_hint=hint,
                                  billing_path=path, routing=routing, compliance=compliance,
                                  context_tier=context_tier)
        return self._cached(self._ctx, vals, build)

    def inference(self, row: Sequence[Any]) -> Inference:
        """An ``Inference`` from a row in ``_INF_COLS`` order."""
        usage = UsageBuckets(uncached_input=row[25], cache_read=row[26], cache_write_5m=row[27],
                             cache_write_1h=row[28], cache_write_other=row[29],
                             cache_write_other_ttl_s=row[30], cache_write_unknown=row[31],
                             output=row[32], output_reasoning=row[33],
                             web_search_requests=row[34], web_fetch_requests=row[35])
        return Inference(inference_id=row[0], kind=InferenceKind(row[7]), usage=usage,
                         pricing=self.context(row, 12), usage_source=UsageSource(row[8]),
                         billable=_int_bool(row[9]), billing_rule_id=row[10],
                         output_upper=row[11], provider_reported_cost_nano=row[36],
                         provider_reported_cost_basis=row[37])

    @staticmethod
    def attempt(row: Sequence[Any], inferences: tuple[Inference, ...]) -> Attempt:
        diag = None
        if row[17] is not None:
            diag = CacheDiagnostic(reason=row[17], provider_reason=row[18],
                                   missed_input_tokens_estimate=row[19], source=row[20])
        edits = tuple((e[0], e[1]) for e in json.loads(row[21])) if row[21] else ()
        return Attempt(attempt_id=row[0], attempt_no=row[2], ts_start_ms=row[3], ttft_ms=row[4],
                       duration_ms=row[5], outcome=Outcome(row[6]), http_status=row[7],
                       error_type=row[8], retry_layer=row[9], retry_after_ms=row[10],
                       should_retry=_int_bool(row[11]), provider_request_id=row[13],
                       provider_message_id=row[14], model_served=row[15], stop_reason=row[16],
                       inferences=inferences, diagnostics=diag, applied_edits=edits,
                       thinking_dropped=row[22], sdk_retry_count=row[12],
                       raw_usage_json=row[23], convention_id=row[24])

    def request(self, row: Sequence[Any], attempts: tuple[Attempt, ...]) -> Request:
        """A ``Request`` from a row in ``_REQ_COLS`` order and its attempts."""
        appended = (tuple(from_json(AppendedItem, x) for x in json.loads(row[33]))
                    if row[33] else ())
        fp = from_json(ContentFingerprint, json.loads(row[34])) if row[34] else None
        return Request(request_id=row[0], session_key=row[2], lane_key=row[1], seq=row[3],
                       attribution=self.attribution(tuple(row[_ATTR_SLICE])),
                       params=self.params(row[32]), attempts=attempts, fingerprint=fp,
                       appended=appended, source=self.source(row[35]))


# ---------------------------------------------------------------------------------------------
# provider-side record codecs
# ---------------------------------------------------------------------------------------------


def _cost_line_row(c: CostLine) -> tuple[Any, ...]:
    return (c.line_id, c.source_kind, c.date_utc, c.channel, c.workspace_id, c.description,
            c.model, c.cost_type, c.token_type, c.sku, c.service_tier, c.inference_geo,
            c.endpoint_scope, c.amount_nano, c.list_amount_nano, c.currency, c.finality,
            c.principal, c.fetched_ms, c.quantity, c.unit, c.cost_center, c.team, c.repo,
            c.workload, c.workflow, c.routing, c.speed, c.pseudo)


def _cost_line_of(row: Sequence[Any]) -> CostLine:
    return CostLine(**dict(zip(_CL_COLS, row, strict=True)))


def _aggregate_row(a: UsageAggregate) -> tuple[Any, ...]:
    return (a.agg_id, a.source_kind, a.bucket_start_ms, a.bucket_end_ms,
            canonical([list(p) for p in a.dims]), _canonical_record(a.usage),
            a.reported_cost_nano, a.reported_cost_basis, a.list_cost_nano, a.finality,
            a.fetched_ms)


def _aggregate_of(row: Sequence[Any]) -> UsageAggregate:
    return UsageAggregate(agg_id=row[0], source_kind=row[1], bucket_start_ms=row[2],
                          bucket_end_ms=row[3], dims=tuple(tuple(p) for p in json.loads(row[4])),
                          usage=from_json(UsageBuckets, json.loads(row[5])),
                          reported_cost_nano=row[6], reported_cost_basis=row[7],
                          list_cost_nano=row[8], finality=row[9], fetched_ms=row[10])


def _outcome_row(o: OutcomeAggregate) -> tuple[Any, ...]:
    return (o.date_utc, o.team, o.source_kind, o.n_users, o.sessions, o.commits, o.pull_requests,
            o.lines_added, o.lines_removed, o.edits_accepted, o.edits_rejected,
            _json_or_none([list(p) for p in o.extra]))


def _outcome_of(row: Sequence[Any]) -> OutcomeAggregate:
    extra = tuple(tuple(p) for p in json.loads(row[11])) if row[11] else ()
    return OutcomeAggregate(date_utc=row[0], team=row[1], n_users=row[3], sessions=row[4],
                            commits=row[5], pull_requests=row[6], lines_added=row[7],
                            lines_removed=row[8], edits_accepted=row[9], edits_rejected=row[10],
                            source_kind=row[2], extra=extra)


def _shell_row(shell: Lane) -> tuple[Any, ...]:
    return (shell.lane_key, shell.session_key, shell.kind.value, shell.parent_lane_key,
            shell.cache_scope_key, shell.ttl_observed, int(shell.lane_exact))


def _shell_of(row: Sequence[Any]) -> Lane | None:
    """The stored shell of a lanes row (None for a placeholder: requests without a shell)."""
    if row[2] is None:
        return None
    return Lane(lane_key=row[0], session_key=row[1], kind=LaneKind(row[2]),
                parent_lane_key=row[3], cache_scope_key=row[4], requests=(),
                ttl_observed=row[5], lane_exact=bool(row[6]))


# ---------------------------------------------------------------------------------------------
# PricedTotal accumulation (the fake_price_total rules, on stored or fresh prices)
# ---------------------------------------------------------------------------------------------


class _Total:
    """Accumulates the priced columns of billable inferences into a ``PricedTotal``."""

    __slots__ = ("allow", "all_tokens", "est", "est_high", "est_low", "est_point", "exact",
                 "pool", "priced", "shas", "unpriced", "unpriced_tokens")

    def __init__(self) -> None:
        self.exact = self.est_point = self.est_low = self.est_high = 0
        self.est = False
        self.priced = self.unpriced = self.unpriced_tokens = self.all_tokens = 0
        self.allow = [0, 0, 0, 0, True]    # n, point, low, high, all exact
        self.pool = [0, 0, 0, 0, True]
        self.shas: set[str] = set()

    def add(self, tokens: int, billing_path: str, priced: int | None, exact_n: int, est_n: int,
            est_lo: int, est_hi: int, evidence: str | None, basis: str | None,
            sha: str | None) -> None:
        self.all_tokens += tokens
        if sha is not None:
            self.shas.add(sha)
        if priced is None:
            self.unpriced += 1
            self.unpriced_tokens += tokens
            return
        self.priced += 1
        if basis == Basis.LIST_EQUIVALENT.value:
            slot = self.pool if billing_path in COPILOT_BILLING_PATHS else self.allow
            ranged = evidence == Evidence.ESTIMATED.value
            slot[0] += 1
            slot[1] += priced
            slot[2] += exact_n + est_lo if ranged else priced
            slot[3] += exact_n + est_hi if ranged else priced
            slot[4] = slot[4] and evidence == Evidence.EXACT.value
            return
        self.exact += exact_n
        if evidence == Evidence.ESTIMATED.value:
            self.est = True
            self.est_point += est_n
            self.est_low += est_lo
            self.est_high += est_hi

    @staticmethod
    def _le(slot: list[Any]) -> Figure | None:
        n, point, low, high, all_exact = slot
        if n == 0:
            return None
        if all_exact:
            return exact(point, Basis.LIST_EQUIVALENT)
        return Figure(nano=point, evidence=Evidence.ESTIMATED, basis=Basis.LIST_EQUIVALENT,
                      low_nano=low, high_nano=high, note="range lines")

    def total(self, billed_basis: Basis) -> PricedTotal:
        est = (Figure(nano=self.est_point, evidence=Evidence.ESTIMATED, basis=billed_basis,
                      low_nano=self.est_low, high_nano=self.est_high, note="range lines")
               if self.est else None)
        priced_tokens = self.all_tokens - self.unpriced_tokens
        if self.all_tokens <= 0:
            coverage = "1"
        else:
            value = ratio(max(priced_tokens, 0), self.all_tokens)
            coverage = format(value.normalize(), "f") if value is not None else "1"
        return PricedTotal(exact=exact(self.exact, billed_basis), estimated=est,
                           allowance=self._le(self.allow), priced_inferences=self.priced,
                           unpriced_inferences=self.unpriced, unpriced_tokens=self.unpriced_tokens,
                           coverage=coverage, pool=self._le(self.pool))


def _usage_sum(rows: list[tuple[int, ...]]) -> UsageBuckets:
    """Σ of usage tuples ``(uncached, read, w5m, w1h, wother, ttl, wunknown, output, reasoning,
    web_search, web_fetch)`` (other-write TTL: the shortest; reasoning only when every row has
    it)."""
    if not rows:
        return UsageBuckets()
    sums = [0] * 11
    ttls = [r[5] for r in rows if r[5] is not None]
    reasoning = [r[8] for r in rows]
    for r in rows:
        for k in (0, 1, 2, 3, 4, 6, 7, 9, 10):
            sums[k] += r[k]
    return UsageBuckets(uncached_input=sums[0], cache_read=sums[1], cache_write_5m=sums[2],
                        cache_write_1h=sums[3], cache_write_other=sums[4],
                        cache_write_other_ttl_s=min(ttls) if ttls else None,
                        cache_write_unknown=sums[6], output=sums[7],
                        output_reasoning=(sum(reasoning) if all(x is not None for x in reasoning)
                                          else None),
                        web_search_requests=sums[9], web_fetch_requests=sums[10])


def _group_sort_key(key: tuple[tuple[str, Any], ...]) -> tuple[tuple[bool, str], ...]:
    return tuple((v is None, v or "") for _, v in key)


# ---------------------------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------------------------


class SqliteStore:
    """SQLite ``LedgerStore`` (SPEC §7.2) and ``LedgerStats`` (C-27).

    ``SqliteStore(path, *, create=True, org_key=None, name_key_id=None, pricer=None,
    read_only=False, adopt_key_ids=False, now_ms=None)``. *now_ms* fixes the clock of ``meta``,
    ``sources`` and ``audit`` rows (tests and deterministic runs); by default the wall clock.

    Key-id adoption (R-E21, amendment A-2): with ``adopt_key_ids=True`` the store adopts the
    principal and name key ids of the first ``copilot-export`` bundle it ingests (``meta``
    ``adopted_key_id`` / ``adopted_name_key_id``; a store without its own org key also records them
    as ``org_key_id`` / ``name_key_id`` with ``org_key_mode = "adopted"``) and writes an audit row;
    a second bundle under another key id raises ``UsageError``; ``r_`` / ``c_`` principals still
    need the store's own org key.
    """

    def __init__(self, path: str | os.PathLike[str], *, create: bool = True,
                 org_key: bytes | None = None, name_key_id: str | None = None,
                 pricer: Pricer | None = None, read_only: bool = False,
                 adopt_key_ids: bool = False, now_ms: int | None = None) -> None:
        self.path = Path(path)
        self.read_only = bool(read_only)
        self._fixed_now = now_ms
        self._org_key = bytes(org_key) if org_key else None
        self._pricer = pricer
        self._adopt = bool(adopt_key_ids)
        self._codec = _Codec()
        self._basis_seen: dict[str, str] = {}
        exists = self.path.exists()
        if not exists and (read_only or not create):
            raise UsageError("ledger file does not exist")
        if not exists:
            with open_private(self.path, "ab"):
                pass
        if self.read_only:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                                         check_same_thread=False)
        else:
            self._conn = sqlite3.connect(str(self.path), isolation_level=None,
                                         check_same_thread=False)
        try:
            self._open(name_key_id)
        except BaseException:
            self._conn.close()
            raise
        self._pseudo = self._make_pseudonymizer()

    # ---------- lifecycle ----------

    def _open(self, name_key_id: str | None) -> None:
        conn = self._conn
        schema.apply_pragmas(conn, read_only=self.read_only)
        version = schema.schema_version(conn)
        if version is None:
            if self.read_only:
                raise UsageError("not a Token Bill ledger")
            has_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
            if has_tables and conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='requests'").fetchone()[0]:
                raise UsageError("not a Token Bill ledger (no schema_version)")
            schema.create_schema(conn)
            self._init_meta(name_key_id)
        else:
            if not self.read_only:
                schema.migrate(conn)
            elif version != schema.SCHEMA_VERSION:
                raise UsageError("the ledger needs a migration; open it writable once")
            self._check_meta(name_key_id)

    def _init_meta(self, name_key_id: str | None) -> None:
        own = key_id(self._org_key) if self._org_key else ""
        values = {"org_key_id": own, "name_key_id": name_key_id or "", "content_tier": "none",
                  "created_ms": str(self._now()), "org_key_mode": "own" if own else "none",
                  "adopted_key_id": "", "adopted_name_key_id": ""}
        with self._tx():
            for k, v in values.items():
                self._meta_set(k, v)

    def _check_meta(self, name_key_id: str | None) -> None:
        meta = self._meta_all()
        if self._org_key:
            own = key_id(self._org_key)
            stored = meta.get("org_key_id", "")
            if stored and stored != own:
                raise UsageError("the org key does not match this ledger (key id differs)")
            if not stored and not self.read_only:
                with self._tx():
                    self._meta_set("org_key_id", own)
                    self._meta_set("org_key_mode", "own")
        if name_key_id:
            stored = meta.get("name_key_id", "")
            if stored and stored != name_key_id:
                raise UsageError("the name key id does not match this ledger")
            if not stored and not self.read_only:
                with self._tx():
                    self._meta_set("name_key_id", name_key_id)

    def _make_pseudonymizer(self) -> Pseudonymizer:
        meta = self._meta_all()
        accepted = [meta.get("org_key_id", ""), meta.get("adopted_key_id", "")]
        return Pseudonymizer(self._org_key, accepted_key_ids=[k for k in accepted if k])

    def close(self) -> None:
        """Close the connection (idempotent)."""
        conn = getattr(self, "_conn", None)
        if conn is not None:
            conn.close()
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection (for ``store.rollups`` / ``store.retention`` callers)."""
        if self._conn is None:
            raise UsageError("the store is closed")
        return self._conn

    def _now(self) -> int:
        return self._fixed_now if self._fixed_now is not None else _now_ms()

    def _writable(self) -> sqlite3.Connection:
        if self.read_only:
            raise UsageError("this store was opened read-only")
        return self.connection

    class _Tx:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def __enter__(self) -> sqlite3.Connection:
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type: object, *rest: object) -> None:
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")

    def _tx(self) -> SqliteStore._Tx:
        return SqliteStore._Tx(self._writable())

    # ---------- meta ----------

    def _meta_all(self) -> dict[str, str]:
        return dict(self.connection.execute("SELECT key, value FROM meta").fetchall())

    def _meta_get(self, key: str) -> str:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row is not None else ""

    def _meta_set(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value", (key, value))

    def _meta_add(self, key: str, n: int) -> None:
        if n:
            current = self._meta_get(key)
            self._meta_set(key, str((int(current) if current else 0) + n))

    def meta(self) -> dict[str, str]:
        """``schema_version``, ``org_key_id``, ``name_key_id``, ``content_tier``, ``created_ms``,
        ``org_key_mode``, ``adopted_key_id`` and ``adopted_name_key_id`` (``""`` when none)."""
        meta = self._meta_all()
        keys = ("schema_version", "org_key_id", "name_key_id", "content_tier", "created_ms",
                "org_key_mode", "adopted_key_id", "adopted_name_key_id")
        return {k: meta.get(k, "") for k in keys}

    def dq_counts(self) -> dict[str, int]:
        """Store-wide data-quality counts: key mismatches (running totals), request-id collisions
        and cross-source usage mismatches (current state)."""
        conn = self.connection
        collisions = conn.execute(
            "SELECT COUNT(*) FROM request_index WHERE collision=1").fetchone()[0]
        mismatches = 0
        current: str | None = None
        usages: set[str] = set()
        for rid, doc in conn.execute("SELECT request_id, doc FROM merge_members "
                                     "ORDER BY request_id"):
            if rid != current:
                mismatches += len(usages) > 1
                current, usages = rid, set()
            req = from_json(Request, json.loads(doc))
            si = req.serving_inference
            if si is not None:
                usages.add(_canonical_record(si.usage))
        mismatches += len(usages) > 1
        meta = self._meta_all()
        return {_DQ_NAME: int(meta.get(_DQ_NAME) or 0), _DQ_COLLISION: collisions,
                _DQ_MISMATCH: mismatches, _DQ_PRINCIPAL: int(meta.get(_DQ_PRINCIPAL) or 0)}

    def source_stats(self, *, adapter: str | None = None) -> dict[str, int]:
        """Σ of the integer ``sources.stats_json`` values by key, over every source or one
        adapter's (``LedgerStats``, C-27); sorted by key."""
        sql = "SELECT stats_json FROM sources"
        params: tuple[Any, ...] = ()
        if adapter is not None:
            sql += " WHERE adapter=?"
            params = (adapter,)
        totals: dict[str, int] = {}
        for (text,) in self.connection.execute(sql, params):
            for key, value in (json.loads(text) if text else {}).items():
                if type(value) is int:
                    totals[key] = totals.get(key, 0) + value
        return dict(sorted(totals.items()))

    # ---------- ingest: write-time privacy ----------

    def _adoption(self, src: SourceInfo, meta: Mapping[str, str]) -> bool:
        if not self._adopt or src.adapter != ADOPTABLE_ADAPTER or not src.principal_key_id:
            return False
        if src.principal_key_id in (meta.get("org_key_id"), meta.get("adopted_key_id")):
            return False
        if meta.get("adopted_key_id"):
            raise UsageError("this store already adopted another export key id; use the same "
                             "export key every month")
        return True

    def _adopt_from(self, src: SourceInfo, meta: dict[str, str]) -> None:
        key = src.principal_key_id or ""
        name = src.name_key_id or ""
        self._meta_set("adopted_key_id", key)
        self._meta_set("adopted_name_key_id", name)
        if not meta.get("org_key_id"):
            self._meta_set("org_key_id", key)
            self._meta_set("org_key_mode", "adopted")
            if not meta.get("name_key_id") and name:
                self._meta_set("name_key_id", name)
        self._audit_row("store", "adopt_key_id", {"key_id": key, "name_key_id": name,
                                                   "adapter": src.adapter})

    def _clean_principal(self, value: str | None, principals_ok: bool,
                         counts: dict[str, int]) -> str | None:
        if value is None:
            return None
        if value.startswith(("r_", "c_")):
            counts["principals_pseudonymized"] += 1
            return self._pseudo.principal(value)
        if principals_ok:
            return value
        counts["principals_nulled"] += 1
        return None

    def _clean_request(self, req: Request, names_ok: bool, principals_ok: bool,
                       counts: dict[str, int]) -> Request:
        attr = req.attribution
        changes: dict[str, Any] = {}
        principal = self._clean_principal(attr.principal, principals_ok, counts)
        if principal != attr.principal:
            changes["principal"] = principal
        if not names_ok:
            for name in _HASHED_ATTR:
                if is_name_hash(getattr(attr, name)):
                    changes[name] = None
                    counts["names_nulled"] += 1
            hashed_extra = [k for k, v in attr.extra if is_name_hash(v)]
            if hashed_extra:
                changes["extra"] = tuple((k, v) for k, v in attr.extra if not is_name_hash(v))
                counts["names_nulled"] += len(hashed_extra)
        appended = req.appended
        if not names_ok and any(is_name_hash(a.name) for a in appended):
            counts["names_nulled"] += sum(1 for a in appended if is_name_hash(a.name))
            appended = tuple(dataclasses.replace(a, name=None) if is_name_hash(a.name) else a
                             for a in appended)
        for att in req.attempts:
            rollups.date_of(att.ts_start_ms)  # UsageError past 9999-12-31, before any write
        if not changes and appended is req.appended:
            return req
        return dataclasses.replace(req, attribution=dataclasses.replace(attr, **changes),
                                   appended=appended)

    # ---------- ingest ----------

    def ingest(self, result: IngestResult, *, pricer: Pricer | None = None) -> dict[str, int]:
        """Store *result* (SPEC §7.2). Re-ingesting a source with the same ``(source_id,
        sha256)`` is a no-op (``skipped = 1``). Returns counts: ``requests``, ``events``,
        ``sessions``, ``aggregates``, ``cost_lines``, ``outcomes``, ``skipped``,
        ``principals_pseudonymized``, ``principals_nulled``, ``names_nulled``,
        ``dq.name_key_mismatch``, ``dq.principal_key_mismatch``, ``dq.request_id_collision``
        (new collisions) and ``dq.cross_source_usage_mismatch`` (newly mismatched requests)."""
        if not isinstance(result, IngestResult) or not isinstance(result.source, SourceInfo):
            raise UsageError("ingest expects an IngestResult")
        conn = self._writable()
        src = result.source
        counts = dict.fromkeys(("requests", "events", "sessions", "aggregates", "cost_lines",
                                "outcomes", "skipped", "principals_pseudonymized",
                                "principals_nulled", "names_nulled"), 0)
        counts.update({_DQ_NAME: 0, _DQ_PRINCIPAL: 0, _DQ_COLLISION: 0, _DQ_MISMATCH: 0})
        row = conn.execute("SELECT sha256 FROM sources WHERE source_id=?",
                           (src.source_id,)).fetchone()
        if row is not None and row[0] == src.sha256:
            counts["skipped"] = 1
            return counts
        meta = self._meta_all()
        adopting = self._adoption(src, meta)
        keyless_adopt = self._adopt and self._org_key is None
        store_name = (meta.get("name_key_id", "") if keyless_adopt
                      else meta.get("name_key_id", "") or src.name_key_id or "")
        name_ids = {store_name, meta.get("adopted_name_key_id", "")} - {""}
        principal_ids = {meta.get("org_key_id", ""), meta.get("adopted_key_id", "")} - {""}
        if adopting:
            name_ids.add(src.name_key_id or "")
            principal_ids.add(src.principal_key_id or "")
        names_ok = src.name_key_id is not None and src.name_key_id in name_ids
        principals_ok = src.principal_key_id is not None and src.principal_key_id in principal_ids
        # --- write-time privacy: everything is cleaned before the first write ---
        loose = list(result.requests)
        events = list(result.events)
        shells: list[Lane] = []
        for session in result.sessions:
            for lane in session.lanes:
                loose.extend(lane.requests)
                events.extend(lane.events)
                shells.append(dataclasses.replace(lane, requests=(), events=()))
        cleaned = [self._clean_request(r, names_ok, principals_ok, counts) for r in loose]
        cost_lines = [self._clean_cost_line(c, names_ok, principals_ok, counts)
                      for c in result.cost_lines]
        aggregates = []
        for agg in result.aggregates:
            if not names_ok and any(is_name_hash(v) for _, v in agg.dims):
                counts["names_nulled"] += sum(1 for _, v in agg.dims if is_name_hash(v))
                agg = dataclasses.replace(agg, dims=tuple((k, v) for k, v in agg.dims
                                                          if not is_name_hash(v)))
            aggregates.append(agg)
        for ev in events:
            rollups.date_of(ev.ts_ms)
        contribs = [Contribution.of(r, default_adapter=src.adapter, source_id=src.source_id)
                    for r in cleaned]
        ingest_pricer = pricer if pricer is not None else self._pricer
        # --- commit phase ---
        work = _Work(self, ingest_pricer, counts)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if adopting:
                self._adopt_from(src, meta)
                self._pseudo = Pseudonymizer(
                    self._org_key, accepted_key_ids=[*self._pseudo.accepted_key_ids,
                                                     src.principal_key_id or ""])
            if not meta.get("name_key_id") and src.name_key_id and not keyless_adopt:
                self._meta_set("name_key_id", src.name_key_id)
            work.shells(shells)
            work.sessions(result.sessions)
            for n, chunk in enumerate(_chunks(contribs, BATCH_ROWS)):
                if n:
                    work.finish()
                    conn.execute("COMMIT")
                    conn.execute("BEGIN IMMEDIATE")
                work.merge_chunk(list(chunk))
            work.events(events)
            work.provider_records(aggregates, cost_lines, list(result.outcomes))
            work.finish()
            counts.update(requests=len(cleaned), events=len(events),
                          sessions=len(result.sessions), aggregates=len(aggregates),
                          cost_lines=len(cost_lines), outcomes=len(result.outcomes))
            counts[_DQ_NAME] = counts["names_nulled"] + counts["principals_nulled"]
            counts[_DQ_PRINCIPAL] = counts["principals_nulled"]
            self._meta_add(_DQ_NAME, counts[_DQ_NAME])
            self._meta_add(_DQ_PRINCIPAL, counts[_DQ_PRINCIPAL])
            stats = {k: v for k, v in result.stats.items() if type(v) is int}
            conn.execute(_insert_sql("sources", (
                "source_id", "adapter", "name_hmac", "sha256", "name_key_id", "principal_key_id",
                "ingested_ms", "records", "quarantined", "stats_json"), "INSERT OR REPLACE"),
                (src.source_id, src.adapter, src.name_hmac, src.sha256, src.name_key_id,
                 src.principal_key_id, self._now(), len(cleaned), len(result.quarantined),
                 canonical(dict(sorted(stats.items())))))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return counts

    def _clean_cost_line(self, line: CostLine, names_ok: bool, principals_ok: bool,
                         counts: dict[str, int]) -> CostLine:
        changes: dict[str, Any] = {}
        principal = self._clean_principal(line.principal, principals_ok, counts)
        if principal != line.principal:
            changes["principal"] = principal
        if not names_ok:
            for name in _COST_LINE_HASHED:
                if is_name_hash(getattr(line, name)):
                    changes[name] = None
                    counts["names_nulled"] += 1
        return dataclasses.replace(line, **changes) if changes else line

    def _record_basis(self, pricer: Pricer) -> None:
        sha = pricer.rate_card_sha256
        basis = Basis(pricer.basis).value
        if self._basis_seen.get(sha) != basis:
            self._meta_set(f"rate_card_basis:{sha}", basis)
            self._basis_seen[sha] = basis

    def _price(self, req: Request, pricer: Pricer | None) -> dict[str, tuple[Any, ...]]:
        """Priced columns of every inference of *req* (by inference id)."""
        out: dict[str, tuple[Any, ...]] = {}
        if pricer is not None:
            self._record_basis(pricer)
        for att in req.attempts:
            for inf in att.inferences:
                if inf.billable is False:
                    out[inf.inference_id] = _NOT_BILLABLE
                elif pricer is None:
                    out[inf.inference_id] = _no_pricer_tuple()
                else:
                    out[inf.inference_id] = _priced_tuple(
                        pricer.price_inference(inf, ts_ms=att.ts_start_ms),
                        pricer.rate_card_sha256)
        return out

    # ---------- reading helpers ----------

    def _in_rows(self, sql: str, keys: Sequence[Any],
                 params: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
        """Rows of *sql* (with one ``{}`` for an IN list) over *keys* in chunks."""
        conn = self.connection
        for chunk in _chunks(list(keys)):
            yield from conn.execute(sql.format(_marks(len(chunk))), (*chunk, *params))

    def _children(self, request_ids: Sequence[str]) -> dict[str, tuple[Attempt, ...]]:
        """Attempts (with inferences) of *request_ids*."""
        infs: dict[str, list[Inference]] = {}
        for row in self._in_rows(f"SELECT {_INF_SELECT} FROM inferences WHERE request_id IN ({{}}) "
                                 "ORDER BY attempt_id, ord", request_ids):
            infs.setdefault(row[1], []).append(self._codec.inference(row))
        out: dict[str, list[Attempt]] = {}
        for row in self._in_rows(f"SELECT {_ATT_SELECT} FROM attempts WHERE request_id IN ({{}}) "
                                 "ORDER BY request_id, ord", request_ids):
            out.setdefault(row[1], []).append(
                self._codec.attempt(row, tuple(infs.get(row[0], ()))))
        return {rid: tuple(atts) for rid, atts in out.items()}

    def _requests_of(self, rows: Sequence[Sequence[Any]]) -> list[Request]:
        kids = self._children([r[0] for r in rows])
        return [self._codec.request(r, kids.get(r[0], ())) for r in rows]

    @staticmethod
    def _check_where(where: Mapping[str, str] | None) -> dict[str, str]:
        clause = dict(where or {})
        person = sorted(set(clause) & PERSON_DIMS)
        if person:
            raise PrivacyError(f"filtering by {', '.join(person)} is not allowed")
        unknown = sorted(set(clause) - WHERE_KEYS)
        if unknown:
            raise UsageError(f"unknown filter key(s): {', '.join(unknown)}")
        for value in clause.values():
            if not isinstance(value, str):
                raise UsageError("filter values must be strings ('' = missing)")
        return clause

    @staticmethod
    def _filter_sql(clause: Mapping[str, str], dims: Mapping[str, str]) -> tuple[str, list[Any]]:
        parts: list[str] = []
        params: list[Any] = []
        for key in sorted(clause):
            expr = dims[key]
            if clause[key] == "":
                parts.append(f"{expr} IS NULL")
            else:
                parts.append(f"{expr} = ?")
                params.append(clause[key])
        return "".join(f" AND {p}" for p in parts), params

    @staticmethod
    def _window(since_ms: int | None, until_ms: int | None) -> tuple[int, int]:
        lo = since_ms if since_ms is not None else 0
        hi = until_ms if until_ms is not None else _FOREVER_MS
        if type(lo) is not int or type(hi) is not int:
            raise UsageError("window bounds are int milliseconds")
        return lo, hi

    # ---------- protocol: lanes and requests ----------

    def iter_lanes(self, *, since_ms: int | None = None, until_ms: int | None = None,
                   where: Mapping[str, str] | None = None,
                   lane_keys: Collection[str] | None = None) -> Iterator[Lane]:
        """Lanes ordered by lane key, assembled from the requests starting in ``[since, until)``
        and their events in the window, streamed lane by lane. ``team``, ``lane_kind`` and
        ``billing_class`` select lanes (denormalized on ``lanes`` from the lane's first request);
        other keys select requests; *lane_keys* restricts to a sample."""
        clause = self._check_where(where)
        lo, hi = self._window(since_ms, until_ms)
        lane_clause = {k: v for k, v in clause.items() if k in _LANE_KEYS}
        req_clause = {k: v for k, v in clause.items() if k not in _LANE_KEYS}
        req_sql, req_params = self._filter_sql(req_clause, _REQ_DIM_SQL)
        needs_lane_join = "l." in req_sql
        base = (f"SELECT {_REQ_SELECT} FROM requests r"
                + (" LEFT JOIN lanes l ON l.lane_key = r.lane_key" if needs_lane_join else "")
                + f" WHERE r.ts_start_ms >= ? AND r.ts_start_ms < ?{req_sql}")
        if lane_clause or lane_keys is not None:
            keys = self._selected_lanes(lane_clause, lane_keys)
            rows = self._rows_for_lanes(base, keys, [lo, hi, *req_params])
        else:
            cur = self.connection.execute(
                base + " ORDER BY r.lane_key, r.ts_start_ms, r.seq, r.request_id",
                (lo, hi, *req_params))
            rows = _fetch_iter(cur)
        yield from self._assemble_lanes(rows, lo, hi)

    def _selected_lanes(self, lane_clause: Mapping[str, str],
                        lane_keys: Collection[str] | None) -> list[str]:
        sql, params = self._filter_sql(lane_clause, _LANE_DIM_SQL)
        if lane_keys is not None:
            wanted = sorted(set(lane_keys))
            if not lane_clause:
                return wanted
            got: list[str] = []
            for chunk in _chunks(wanted):
                got.extend(k for (k,) in self.connection.execute(
                    f"SELECT l.lane_key FROM lanes l WHERE l.lane_key IN ({_marks(len(chunk))})"
                    + sql, (*chunk, *params)))
            return sorted(got)
        return [k for (k,) in self.connection.execute(
            "SELECT l.lane_key FROM lanes l WHERE 1=1" + sql + " ORDER BY l.lane_key", params)]

    def _rows_for_lanes(self, base: str, keys: Sequence[str],
                        params: list[Any]) -> Iterator[tuple[Any, ...]]:
        for chunk in _chunks(keys, 200):
            sql = (base + f" AND r.lane_key IN ({_marks(len(chunk))})"
                   " ORDER BY r.lane_key, r.ts_start_ms, r.seq, r.request_id")
            yield from self.connection.execute(sql, (*params, *chunk))

    def _assemble_lanes(self, rows: Iterable[tuple[Any, ...]], lo: int,
                        hi: int) -> Iterator[Lane]:
        """Group request rows (ordered by lane) into Lanes, a chunk of requests at a time."""
        pending: list[tuple[str, list[Request]]] = []     # completed lanes awaiting events
        current_key: str | None = None
        current: list[Request] = []
        buffer: list[tuple[Any, ...]] = []

        def flush_rows() -> None:
            nonlocal current_key, current
            for req in self._requests_of(buffer):
                if req.lane_key != current_key:
                    if current_key is not None:
                        pending.append((current_key, current))
                    current_key, current = req.lane_key, []
                current.append(req)
            buffer.clear()

        for row in rows:
            buffer.append(row)
            if len(buffer) >= _IN:
                flush_rows()
                if len(pending) >= 64:
                    yield from self._finish_lanes(pending, lo, hi)
                    pending = []
        flush_rows()
        if current_key is not None:
            pending.append((current_key, current))
        yield from self._finish_lanes(pending, lo, hi)

    def _finish_lanes(self, lanes: list[tuple[str, list[Request]]], lo: int,
                      hi: int) -> Iterator[Lane]:
        if not lanes:
            return
        keys = [k for k, _ in lanes]
        shells: dict[str, tuple[Any, ...]] = {}
        for row in self._in_rows(f"SELECT {', '.join(_LANE_COLS)} FROM lanes "
                                 "WHERE lane_key IN ({})", keys):
            shells[row[0]] = row
        events: dict[str, list[LaneEvent]] = {}
        for lane_key, ts, kind, attrs in self._in_rows(
                "SELECT lane_key, ts_ms, kind, attrs_json FROM events WHERE lane_key IN ({}) "
                "AND ts_ms >= ? AND ts_ms < ?", keys, (lo, hi)):
            events.setdefault(lane_key, []).append(_event_of(lane_key, ts, kind, attrs))
        for key, reqs in lanes:
            row = shells.get(key)
            shell = _shell_of(row) if row is not None else None
            yield Lane(
                lane_key=key,
                session_key=shell.session_key if shell is not None else min(
                    r.session_key for r in reqs),
                kind=shell.kind if shell is not None else LaneKind.UNKNOWN,
                parent_lane_key=shell.parent_lane_key if shell is not None else None,
                cache_scope_key=shell.cache_scope_key if shell is not None else "unknown",
                requests=tuple(reqs), events=tuple(events.get(key, ())),
                ttl_observed=_ttl_observed(reqs),
                lane_exact=shell.lane_exact if shell is not None else True)

    def iter_requests(self, *, since_ms: int | None = None, until_ms: int | None = None,
                      where: Mapping[str, str] | None = None) -> Iterator[Request]:
        """Requests starting in ``[since, until)`` matching *where*, ordered by (ts, lane, seq,
        id)."""
        clause = self._check_where(where)
        lo, hi = self._window(since_ms, until_ms)
        sql, params = self._filter_sql(clause, _REQ_DIM_SQL)
        cur = self.connection.execute(
            f"SELECT {_REQ_SELECT} FROM requests r LEFT JOIN lanes l ON l.lane_key = r.lane_key "
            f"WHERE r.ts_start_ms >= ? AND r.ts_start_ms < ?{sql} "
            "ORDER BY r.ts_start_ms, r.lane_key, r.seq, r.request_id", (lo, hi, *params))
        while True:
            rows = cur.fetchmany(_IN)
            if not rows:
                return
            yield from self._requests_of(rows)

    def iter_usage_records(self, *, since_ms: int | None = None,
                           until_ms: int | None = None) -> Iterator[UsageRecord]:
        """One ``UsageRecord`` per billable inference (``billable`` not False) whose attempt starts
        in the window, ordered by request (ts, lane, seq, id) then attempt and inference order."""
        lo, hi = self._window(since_ms, until_ms)
        cols = ", ".join(f"i.{c}" for c in _INF_COLS)
        cur = self.connection.execute(
            f"SELECT {cols}, r.session_key, r.fidelity, {_LANE_KIND_SQL}, "
            + ", ".join(f"r.{c}" for c in _REQ_COLS[_ATTR_SLICE]) +
            " FROM inferences i JOIN requests r ON r.request_id = i.request_id"
            " JOIN attempts a ON a.attempt_id = i.attempt_id"
            " LEFT JOIN lanes l ON l.lane_key = r.lane_key"
            " WHERE i.billable IS NOT 0 AND i.ts_ms >= ? AND i.ts_ms < ?"
            " ORDER BY r.ts_start_ms, r.lane_key, r.seq, r.request_id, a.ord, i.ord", (lo, hi))
        n = len(_INF_COLS)
        codec = self._codec
        for row in _fetch_iter(cur):
            inf = codec.inference(row)
            yield UsageRecord(
                inference_id=inf.inference_id, request_id=row[2], attempt_id=row[1],
                session_key=row[n], lane_key=row[3], lane_kind=LaneKind(row[n + 2]),
                ts_ms=row[5], date_utc=row[6], kind=inf.kind, usage_source=inf.usage_source,
                billable=inf.billable, billing_rule_id=inf.billing_rule_id, pricing=inf.pricing,
                usage=inf.usage, attribution=codec.attribution(tuple(row[n + 3:])),
                fidelity=Fidelity(row[n + 1]))

    def lane_index(self, *, since_ms: int, until_ms: int) -> Iterator[LaneIndexRow]:
        """One row per lane with requests in the window: team, kind and billing class (of the
        lane), request count and the point priced nano of its billable inferences (unpriced counts
        0). One SQL query."""
        lo, hi = self._window(since_ms, until_ms)
        cur = self.connection.execute(
            f"SELECT r.lane_key, l.team, {_LANE_KIND_SQL}, l.billing_class, COUNT(*),"
            " SUM(COALESCE((SELECT SUM(i.priced_nano) FROM inferences i"
            "   WHERE i.request_id = r.request_id AND i.billable IS NOT 0), 0))"
            " FROM requests r LEFT JOIN lanes l ON l.lane_key = r.lane_key"
            " WHERE r.ts_start_ms >= ? AND r.ts_start_ms < ?"
            " GROUP BY r.lane_key ORDER BY r.lane_key", (lo, hi))
        for key, team, kind, bclass, n, point in _fetch_iter(cur):
            yield LaneIndexRow(lane_key=key, team=team, lane_kind=kind,
                               billing_class=bclass or "billed", requests=n,
                               point_nano=point or 0)

    def lane_first_reads(self, *, since_ms: int,
                         until_ms: int) -> Iterator[tuple[str, str, int]]:
        """``(cache_scope_key, model, R)`` of each lane's first request (in the window) with a
        serving inference, by lane key. One SQL query."""
        lo, hi = self._window(since_ms, until_ms)
        cur = self.connection.execute(
            "SELECT scope, model, reads FROM (SELECT r.lane_key AS lk,"
            " COALESCE(l.cache_scope_key, 'unknown') AS scope, r.req_model AS model,"
            " r.serving_cache_read AS reads, ROW_NUMBER() OVER (PARTITION BY r.lane_key"
            " ORDER BY r.ts_start_ms, r.seq, r.request_id) AS rn"
            " FROM requests r LEFT JOIN lanes l ON l.lane_key = r.lane_key"
            " WHERE r.ts_start_ms >= ? AND r.ts_start_ms < ? AND r.serving_cache_read IS NOT NULL)"
            " WHERE rn = 1 ORDER BY lk", (lo, hi))
        yield from _fetch_iter(cur)

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    source: str = "requests") -> int:
        """Exact ``COUNT(DISTINCT principal)`` over requests (or, with ``source="cost_lines"``,
        stored cost lines dated in the window) matching *where*; never returns ids (§8.4)."""
        lo, hi = self._window(since_ms, until_ms)
        if source == "cost_lines":
            clause = dict(where or {})
            person = sorted(set(clause) & PERSON_DIMS)
            if person:
                raise PrivacyError(f"filtering by {', '.join(person)} is not allowed")
            unknown = sorted(set(clause) - COST_LINE_WHERE_KEYS)
            if unknown:
                raise UsageError(f"unknown cost-line filter key(s): {', '.join(unknown)}")
            sql, params = self._filter_sql(clause, {k: f"c.{k}" for k in COST_LINE_WHERE_KEYS})
            first, last = _date_bounds(lo, hi)
            if first is None:
                return 0
            return self.connection.execute(
                "SELECT COUNT(DISTINCT c.principal) FROM cost_lines c WHERE c.date_utc >= ? AND "
                f"c.date_utc <= ?{sql}", (first, last, *params)).fetchone()[0]
        if source != "requests":
            raise UsageError(f"unknown count source {source!r} (requests | cost_lines)")
        clause = self._check_where(where)
        sql, params = self._filter_sql(clause, _REQ_DIM_SQL)
        join = " LEFT JOIN lanes l ON l.lane_key = r.lane_key" if "l." in sql else ""
        return self.connection.execute(
            f"SELECT COUNT(DISTINCT r.principal) FROM requests r{join} "
            f"WHERE r.ts_start_ms >= ? AND r.ts_start_ms < ?{sql}", (lo, hi, *params)).fetchone()[0]

    # ---------- protocol: provider-side records ----------

    @staticmethod
    def _record_window(window: Mapping[str, int]) -> tuple[int, int]:
        unknown = set(window) - {"since_ms", "until_ms"}
        if unknown:
            raise UsageError(f"unknown window key(s): {', '.join(sorted(unknown))}")
        return SqliteStore._window(window.get("since_ms"), window.get("until_ms"))

    def aggregates(self, source_kind: str | None = None, **window: int) -> list[UsageAggregate]:
        """Stored aggregates (one version per ``agg_id``) whose bucket starts in the window."""
        lo, hi = self._record_window(window)
        sql = (f"SELECT {', '.join(_AGG_COLS)} FROM aggregates WHERE bucket_start_ms >= ? AND "
               "bucket_start_ms < ?")
        params: list[Any] = [lo, hi]
        if source_kind is not None:
            sql += " AND source_kind = ?"
            params.append(source_kind)
        rows = self.connection.execute(sql + " ORDER BY bucket_start_ms, agg_id", params)
        return [_aggregate_of(r) for r in rows]

    def cost_lines(self, source_kind: str | None = None, **window: int) -> list[CostLine]:
        """Stored cost lines (one version per ``line_id``) dated inside the window."""
        lo, hi = self._record_window(window)
        first, last = _date_bounds(lo, hi)
        if first is None:
            return []
        sql = (f"SELECT {', '.join(_CL_COLS)} FROM cost_lines WHERE date_utc >= ? AND "
               "date_utc <= ?")
        params: list[Any] = [first, last]
        if source_kind is not None:
            sql += " AND source_kind = ?"
            params.append(source_kind)
        rows = self.connection.execute(sql + " ORDER BY date_utc, line_id", params)
        return [_cost_line_of(r) for r in rows]

    def outcomes(self, **window: int) -> list[OutcomeAggregate]:
        """Stored outcome aggregates dated inside the window."""
        lo, hi = self._record_window(window)
        first, last = _date_bounds(lo, hi)
        if first is None:
            return []
        rows = self.connection.execute(
            f"SELECT {', '.join(_OUT_COLS)} FROM outcomes WHERE date_utc >= ? AND date_utc <= ? "
            "ORDER BY date_utc, team, source_kind", (first, last))
        return [_outcome_of(r) for r in rows]

    # ---------- protocol: aggregates for publication ----------

    def _billed_basis(self, shas: Iterable[str]) -> Basis:
        bases = set()
        meta = None
        for sha in shas:
            basis = self._basis_seen.get(sha)
            if basis is None:
                meta = meta if meta is not None else self._meta_all()
                basis = meta.get(f"rate_card_basis:{sha}", Basis.LIST.value)
            bases.add(basis)
        return Basis.CONTRACT if bases == {Basis.CONTRACT.value} else Basis.LIST

    def aggregate(self, *, since_ms: int, until_ms: int, group_by: Sequence[str],
                  where: Mapping[str, str] | None = None,
                  pricer: Pricer | None = None) -> RawAggregate:
        """Group billable inferences of the requests starting in the window by whitelisted
        dimensions (``n_users`` = distinct principals, ``n_requests`` = distinct requests); the
        stored prices, or *pricer*'s when given. ``principal`` / ``session`` / ``session_key`` in
        *group_by* → ``PrivacyError``; unknown dimensions → ``UsageError``."""
        dims_by = tuple(group_by)
        person = sorted(set(dims_by) & PERSON_DIMS)
        if person:
            raise PrivacyError(f"group by {', '.join(person)} is not allowed")
        unknown = [d for d in dims_by if d not in AGG_DIMS]
        if unknown:
            raise UsageError(f"unknown group-by dimension(s): {', '.join(unknown)}")
        clause = self._check_where(where)
        lo, hi = self._window(since_ms, until_ms)
        wsql, wparams = self._filter_sql(clause, _INF_DIM_SQL)
        dim_sql = ", ".join(_INF_DIM_SQL[d] for d in dims_by)
        usage_cols = ("i.uncached_input, i.cache_read, i.cache_write_5m, i.cache_write_1h, "
                      "i.cache_write_other, i.cache_write_other_ttl_s, i.cache_write_unknown, "
                      "i.output, i.output_reasoning, i.web_search_requests, "
                      "i.web_fetch_requests")
        extra = (", " + ", ".join(f"i.{c}" for c in _INF_COLS)) if pricer is not None else ""
        sql = (f"SELECT r.principal, i.request_id, {usage_cols}, i.billing_path, i.priced_nano,"
               " i.exact_nano, i.est_nano, i.est_low_nano, i.est_high_nano, i.evidence, i.basis,"
               f" i.rate_card_sha{', ' + dim_sql if dim_sql else ''}{extra}"
               " FROM inferences i JOIN requests r ON r.request_id = i.request_id"
               " LEFT JOIN lanes l ON l.lane_key = r.lane_key"
               f" WHERE i.billable IS NOT 0 AND r.ts_start_ms >= ? AND r.ts_start_ms < ?{wsql}")
        nd = len(dims_by)
        groups: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        for row in _fetch_iter(self.connection.execute(sql, (lo, hi, *wparams))):
            key = tuple(zip(dims_by, row[22:22 + nd], strict=True))
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"users": set(), "requests": set(), "usage": [],
                                   "total": _Total()}
            if row[0] is not None:
                g["users"].add(row[0])
            g["requests"].add(row[1])
            usage = row[2:13]
            g["usage"].append(usage)
            tokens = (usage[0] + usage[1] + usage[2] + usage[3] + usage[4] + usage[6]
                      + usage[7])
            if pricer is None:
                g["total"].add(tokens, row[13], *row[14:22])
            else:
                inf = self._codec.inference(row[22 + nd:])
                p = pricer.price_inference(inf, ts_ms=row[22 + nd + 5])
                cols = _priced_tuple(p, pricer.rate_card_sha256)
                g["total"].add(tokens, row[13], cols[0], cols[1], cols[2], cols[3], cols[4],
                               cols[5], cols[6], pricer.rate_card_sha256)
        rows = []
        for key in sorted(groups, key=_group_sort_key):
            g = groups[key]
            if pricer is not None:
                basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) \
                    else Basis.LIST
            else:
                basis = self._billed_basis(g["total"].shas)
            rows.append(AggRow(dims=key, n_users=len(g["users"]), n_requests=len(g["requests"]),
                               usage=_usage_sum(g["usage"]), priced=g["total"].total(basis)))
        return RawAggregate(group_by=dims_by, rows=tuple(rows), window=(since_ms, until_ms))

    def cluster_days(self, *, cluster_kind: str, since: str, until: str) -> list[ClusterDay]:
        """Per (date, cluster, arm, wave) over the dates ``[since, until)``, from the
        ``cluster_day`` rollup (stale dates are refreshed first, or computed on the fly by a
        read-only store): active users, requests, billed exact nano, list-equivalent nano of the
        subscription path (``allowance_nano``) and of the Copilot paths (``pool_nano``). Kinds:
        ``team``, ``workspace``, ``mdm_group``, ``gateway`` (R-E28)."""
        if cluster_kind not in _CLUSTER_KINDS:
            raise UsageError(f"unknown cluster kind {cluster_kind!r}")
        rollups.date_start_ms(since)
        rollups.date_start_ms(until)
        conn = self.connection
        stale = {d for d in rollups.dirty_dates(conn) if since <= d < until}
        fresh: list[tuple[Any, ...]] = []
        if stale and not self.read_only:
            rollups.refresh_rollups(conn, sorted(stale))
            stale = set()
        elif stale:
            fresh = [r for r in rollups.cluster_rows(conn, stale) if r[1] == cluster_kind]
        rows = [r for r in conn.execute(
            "SELECT date_utc, cluster_kind, cluster_id, arm, wave, active_users, requests, "
            "exact_nano, allowance_nano, pool_nano FROM cluster_day WHERE cluster_kind = ? AND "
            "date_utc >= ? AND date_utc < ?", (cluster_kind, since, until))
            if r[0] not in stale]
        out = [ClusterDay(date_utc=r[0], cluster_kind=r[1], cluster_id=r[2], arm=r[3], wave=r[4],
                          active_users=r[5], requests=r[6], exact_nano=r[7],
                          allowance_nano=r[8], pool_nano=r[9]) for r in rows + fresh]
        out.sort(key=lambda c: (c.date_utc, c.cluster_id, c.arm or "", c.wave or ""))
        return out

    def cost_rows(self, *, since_ms: int, until_ms: int,
                  group_by: Sequence[str]) -> list[LedgerCostRow]:
        """One row per group × bucket × basis of the priced lines of billable inferences (exact
        per-bucket sums of the stored lines); ungrouped dimensions are ``""`` (string fields) or
        None; ``principal`` → ``PrivacyError``."""
        dims_by = tuple(group_by)
        person = sorted(set(dims_by) & PERSON_DIMS)
        if person:
            raise PrivacyError(f"group by {', '.join(person)} is not allowed")
        unknown = [d for d in dims_by if d not in COST_DIMS]
        if unknown:
            raise UsageError(f"unknown cost-row dimension(s): {', '.join(unknown)}")
        lo, hi = self._window(since_ms, until_ms)
        dim_sql = "".join(", " + _INF_DIM_SQL[d] for d in dims_by)
        cur = self.connection.execute(
            f"SELECT r.principal, i.basis, i.lines_json{dim_sql}"
            " FROM inferences i JOIN requests r ON r.request_id = i.request_id"
            " LEFT JOIN lanes l ON l.lane_key = r.lane_key"
            " WHERE i.billable IS NOT 0 AND i.priced_nano IS NOT NULL"
            " AND r.ts_start_ms >= ? AND r.ts_start_ms < ?", (lo, hi))
        cells: dict[tuple[Any, ...], list[Any]] = {}
        for row in _fetch_iter(cur):
            principal, basis, lines_json = row[0], row[1], row[2]
            gkey = tuple(row[3:])
            for line in json.loads(lines_json or "[]"):
                bucket, qty, amount, low, high, is_exact, rate_row = line
                c = cells.get((gkey, bucket, basis))
                if c is None:
                    c = cells[(gkey, bucket, basis)] = [0, 0, 0, 0, set(), set()]
                c[0] += qty
                if is_exact:
                    c[1] += amount
                else:
                    c[2] += low or 0
                    c[3] += high or 0
                if principal is not None:
                    c[4].add(principal)
                c[5].add(rate_row)
        out = []
        for (gkey, bucket, basis), c in sorted(
                cells.items(), key=lambda kv: (tuple(v or "" for v in kv[0][0]), kv[0][1],
                                               kv[0][2])):
            vals = dict(zip(dims_by, gkey, strict=True))
            out.append(LedgerCostRow(
                date_utc=vals.get("date") or "", provider=vals.get("provider") or "",
                channel=vals.get("channel") or "", model=vals.get("model") or "", bucket=bucket,
                team=vals.get("team"), cost_center=vals.get("cost_center"),
                project=vals.get("project"), workspace_id=vals.get("workspace_id"),
                lane_kind=vals.get("lane_kind") or "",
                workload_class=vals.get("workload_class") or "",
                agent_product=vals.get("agent_product"),
                billing_path=vals.get("billing_path") or "", quantity=c[0], priced_nano=c[1],
                estimated_low_nano=c[2], estimated_high_nano=c[3], basis=Basis(basis),
                rate_row_id=min(c[5]) if len(c[5]) == 1 else None, n_users=len(c[4])))
        return out

    # ---------- repricing ----------

    def reprice(self, pricer: Pricer, *, since_ms: int | None = None,
                until_ms: int | None = None) -> int:
        """Re-price the billable inferences of the requests starting in ``[since_ms, until_ms)``
        (default: all) with *pricer* (later ingests keep using their own pricer); returns the
        number of billable inferences re-priced. Streams in ``BATCH_ROWS`` transactions; rollups
        of the touched dates become stale."""
        conn = self._writable()
        lo, hi = self._window(since_ms, until_ms)
        cols = ", ".join(f"i.{c}" for c in _INF_COLS)
        setter = ", ".join(f"{c}=?" for c in _PRICED_COLS)
        total = 0
        last = -1
        while True:
            rows = conn.execute(
                f"SELECT i.rowid, r.date_utc, {cols} FROM inferences i JOIN requests r"
                " ON r.request_id = i.request_id WHERE i.rowid > ? AND i.billable IS NOT 0"
                " AND r.ts_start_ms >= ? AND r.ts_start_ms < ? ORDER BY i.rowid LIMIT ?",
                (last, lo, hi, BATCH_ROWS)).fetchall()
            if not rows:
                return total
            last = rows[-1][0]
            updates = []
            for row in rows:
                inf = self._codec.inference(row[2:])
                p = pricer.price_inference(inf, ts_ms=row[2 + 5])
                updates.append((*_priced_tuple(p, pricer.rate_card_sha256), inf.inference_id))
            with self._tx():
                self._record_basis(pricer)
                conn.executemany(f"UPDATE inferences SET {setter} WHERE inference_id=?", updates)
                rollups.mark_dirty(conn, {row[1] for row in rows})
            total += len(updates)

    # ---------- protocol: cursors, findings, receipts ----------

    def get_cursor(self, source_id: str, unit_hmac: str) -> tuple[int, str, int, int] | None:
        """``(byte_offset, head_sha, size, mtime_ns)`` or None."""
        row = self.connection.execute(
            "SELECT byte_offset, head_sha, size, mtime_ns FROM cursors WHERE source_id=? AND "
            "unit_hmac=?", (source_id, unit_hmac)).fetchone()
        return tuple(row) if row is not None else None  # type: ignore[return-value]

    def set_cursor(self, source_id: str, unit_hmac: str, *, byte_offset: int, head_sha: str,
                   size: int, mtime_ns: int) -> None:
        """Record the incremental-collection cursor of one unit of a source."""
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO cursors(source_id, unit_hmac, byte_offset, "
                         "head_sha, size, mtime_ns) VALUES (?, ?, ?, ?, ?, ?)",
                         (source_id, unit_hmac, byte_offset, head_sha, size, mtime_ns))

    def put_findings(self, run_id: str, findings: Sequence[Finding]) -> None:
        """Persist *findings* under *run_id* (canonical JSON of ``core.records.to_json``); a
        later put of an id replaces it; the run becomes the most recent one."""
        with self._tx() as conn:
            old = conn.execute("SELECT finding_id, json FROM findings WHERE run_id=? "
                               "ORDER BY rowid", (run_id,)).fetchall()
            run = dict(old)
            for f in findings:
                if not isinstance(f, Finding):
                    raise UsageError("put_findings expects Finding objects")
                run[f.finding_id] = _canonical_record(f)
            conn.execute("DELETE FROM findings WHERE run_id=?", (run_id,))
            now = self._now()
            conn.executemany("INSERT INTO findings(run_id, finding_id, created_ms, json) "
                             "VALUES (?, ?, ?, ?)",
                             [(run_id, fid, now, text) for fid, text in run.items()])

    def findings(self, run_id: str | None = None) -> list[Finding]:
        """Findings of *run_id*, or of the most recently written run."""
        conn = self.connection
        if run_id is None:
            row = conn.execute("SELECT run_id FROM findings ORDER BY rowid DESC "
                               "LIMIT 1").fetchone()
            if row is None:
                return []
            run_id = row[0]
        return [from_json(Finding, json.loads(text)) for (text,) in conn.execute(
            "SELECT json FROM findings WHERE run_id=? ORDER BY rowid", (run_id,))]

    def put_receipt(self, row: ReceiptRow) -> None:
        """Store a receipt row (by ``receipt_id``)."""
        if not isinstance(row, ReceiptRow):
            raise UsageError("put_receipt expects a ReceiptRow")
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO receipts(receipt_id, lever_id, lever_class, "
                         "label, realization_rate, created_ms, json, dsse) VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?)",
                         (row.receipt_id, row.lever_id, row.lever_class, row.label,
                          row.realization_rate, row.created_ms, row.json, row.dsse))

    def receipts(self, *, lever_class: str | None = None) -> list[ReceiptRow]:
        """Receipts (optionally of one lever class) ordered by (created_ms, receipt_id)."""
        sql = ("SELECT receipt_id, lever_id, lever_class, label, realization_rate, created_ms, "
               "json, dsse FROM receipts")
        params: tuple[Any, ...] = ()
        if lever_class is not None:
            sql += " WHERE lever_class=?"
            params = (lever_class,)
        return [ReceiptRow(*r) for r in self.connection.execute(
            sql + " ORDER BY created_ms, receipt_id", params)]

    # ---------- protocol: purge, audit ----------

    def purge(self, *, principal: str | None = None, before_ms: int | None = None,
              actor: str) -> int:
        """Delete a person's requests (every contribution of a request any contribution of which
        names *principal*; their cost lines) or everything before *before_ms*; ``VACUUM``; an
        audit row without the identity. Returns the number of merged requests deleted."""
        conn = self._writable()
        n = retention.purge(conn, principal=principal, before_ms=before_ms, actor=actor,
                            now_ms=self._now(), regroup=self._regroup_after_purge)
        self._codec = _Codec()
        return n

    def _regroup_after_purge(self, conn: sqlite3.Connection, request_ids: Sequence[str],
                             drop: Any) -> int:
        """Re-merge the groups *request_ids* without the members for which *drop(member)* is
        true (used by ``store.retention.purge``); returns the number of groups deleted."""
        work = _Work(self, self._pricer, None)
        return work.regroup(list(request_ids), drop)

    def apply_retention(self, now_ms: int, **days: int) -> dict[str, int]:
        """``store.retention.apply_retention`` on this ledger (``identity_days``,
        ``request_days``, ``event_days``, ``rollup_days`` keywords)."""
        counts = retention.apply_retention(self._writable(), now_ms, **days)
        self._codec = _Codec()
        return counts

    def rotate_key(self, old: bytes, new: bytes, kind: str, *, actor: str = "rotate") -> int:
        """``store.retention.rotate`` on this ledger; the store uses the new key afterwards."""
        n = retention.rotate(self._writable(), old, new, kind, actor=actor, now_ms=self._now())
        if kind == "org":
            self._org_key = bytes(new)
        self._codec = _Codec()
        self._pseudo = self._make_pseudonymizer()
        return n

    def audit(self, actor: str, action: str, detail: Mapping[str, object]) -> None:
        """Append an audit row (``detail`` stored as canonical JSON)."""
        with self._tx():
            self._audit_row(actor, action, detail)

    def _audit_row(self, actor: str, action: str, detail: Mapping[str, object]) -> None:
        self.connection.execute(
            "INSERT INTO audit(ts_ms, actor, action, detail_json) VALUES (?, ?, ?, ?)",
            (self._now(), actor, action, canonical(dict(detail))))

    def audit_log(self) -> list[tuple[int, str, str, str]]:
        """The audit rows ``(ts_ms, actor, action, detail_json)`` in insertion order."""
        return [tuple(r) for r in self.connection.execute(  # type: ignore[misc]
            "SELECT ts_ms, actor, action, detail_json FROM audit ORDER BY rowid")]


# ---------------------------------------------------------------------------------------------
# helpers used by the store
# ---------------------------------------------------------------------------------------------


def _fetch_iter(cur: sqlite3.Cursor, n: int = 1000) -> Iterator[tuple[Any, ...]]:
    while True:
        rows = cur.fetchmany(n)
        if not rows:
            return
        yield from rows


def _date_bounds(lo: int, hi: int) -> tuple[str | None, str]:
    """First and last date overlapping ``[lo, hi)`` (None when the window is empty)."""
    lo = max(lo, 0)
    hi = min(hi, rollups.MAX_MS + 1)
    if hi <= lo:
        return None, ""
    return rollups.date_of(lo), rollups.date_of(hi - 1)


def _event_of(lane_key: str, ts: int, kind: str, attrs: str | None) -> LaneEvent:
    pairs = tuple((k, v) for k, v in json.loads(attrs)) if attrs else ()
    return LaneEvent(lane_key=lane_key, ts_ms=ts, kind=LaneEventKind(kind), attrs=pairs)


# ---------------------------------------------------------------------------------------------
# the write path: shells, sessions, the merge engine, events, provider records
# ---------------------------------------------------------------------------------------------


class _Work:
    """One ingest's (or purge's) writes, inside the store's open transaction."""

    def __init__(self, store: SqliteStore, pricer: Pricer | None,
                 counts: dict[str, int] | None) -> None:
        self.store = store
        self.conn = store.connection
        self.codec = store._codec
        self.pricer = pricer
        self.counts = counts if counts is not None else {}
        self.touched_lanes: set[str] = set()
        self.dirty: set[str] = set()
        self._lane_kinds: dict[str, str] = {}

    # ---- shells and sessions ----

    def shells(self, shells: Sequence[Lane]) -> None:
        best: dict[str, Lane] = {}
        for shell in shells:
            have = best.get(shell.lane_key)
            if have is None or shell_rank(shell) < shell_rank(have):
                best[shell.lane_key] = shell
        if not best:
            return
        stored = {row[0]: row for row in self.store._in_rows(
            f"SELECT {', '.join(_LANE_COLS)} FROM lanes WHERE lane_key IN ({{}})",
            sorted(best))}
        for key, shell in sorted(best.items()):
            row = stored.get(key)
            old = _shell_of(row) if row is not None else None
            if old is not None and shell_rank(old) <= shell_rank(shell):
                continue
            if row is None:
                self.conn.execute(
                    "INSERT INTO lanes(lane_key, session_key, kind, parent_lane_key, "
                    "cache_scope_key, ttl_observed, lane_exact) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    _shell_row(shell))
            else:
                self.conn.execute(
                    "UPDATE lanes SET session_key=?, kind=?, parent_lane_key=?, "
                    "cache_scope_key=?, ttl_observed=?, lane_exact=? WHERE lane_key=?",
                    (*_shell_row(shell)[1:], key))
            if old is None or old.kind is not shell.kind:
                self.conn.execute("UPDATE inferences SET lane_kind=? WHERE lane_key=?",
                                  (shell.kind.value, key))
            self._lane_kinds[key] = shell.kind.value
            self.touched_lanes.add(key)

    def sessions(self, sessions: Sequence[Any]) -> None:
        rows = [(s.session_key, s.source_kind, s.started_ms, s.ended_ms,
                 _canonical_record(s.attribution)) for s in sessions]
        self.conn.executemany(
            "INSERT INTO sessions(session_key, source_kind, started_ms, ended_ms, "
            "attribution_json) VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET "
            "source_kind=MIN(source_kind, excluded.source_kind), "
            "started_ms=MIN(started_ms, excluded.started_ms), "
            "ended_ms=MAX(ended_ms, excluded.ended_ms), "
            "attribution_json=MIN(attribution_json, excluded.attribution_json)", rows)

    def _lane_kind(self, lane_key: str) -> str:
        kind = self._lane_kinds.get(lane_key)
        if kind is None:
            row = self.conn.execute("SELECT kind FROM lanes WHERE lane_key=?",
                                    (lane_key,)).fetchone()
            kind = self._lane_kinds[lane_key] = (row[0] if row is not None and row[0]
                                                 else "unknown")
        return kind

    def _load_lane_kinds(self, keys: Iterable[str]) -> None:
        missing = sorted(set(keys) - set(self._lane_kinds))
        for key, kind in self.store._in_rows(
                "SELECT lane_key, kind FROM lanes WHERE lane_key IN ({})", missing):
            self._lane_kinds[key] = kind or "unknown"
        for key in missing:
            self._lane_kinds.setdefault(key, "unknown")

    # ---- group members ----

    def members(self, gid: str, cache: dict[str, list[Contribution]]) -> list[Contribution]:
        """The contributions of the stored merged request *gid*."""
        got = cache.get(gid)
        if got is not None:
            return got
        rows = self.conn.execute(
            "SELECT member_key, adapter, source_id, fidelity, priority, doc FROM merge_members "
            "WHERE request_id=? ORDER BY member_key", (gid,)).fetchall()
        if rows:
            got = [Contribution(from_json(Request, json.loads(doc)), adapter, Fidelity(fid),
                                prio, source_id, _canon=doc)
                   for _, adapter, source_id, fid, prio, doc in rows]
        else:
            row = self.conn.execute(f"SELECT {_REQ_SELECT} FROM requests r WHERE "
                                    "r.request_id=?", (gid,)).fetchone()
            if row is None:
                got = []
            else:
                req = self.store._requests_of([row])[0]
                src = req.source
                got = [Contribution(req, row[8], Fidelity(row[6]), row[7],
                                    src.source_id if src is not None else "")]
        cache[gid] = got
        return got

    def _stored_prices(self, gid: str) -> dict[str, tuple[Any, ...]]:
        cols = ", ".join(_PRICED_COLS)
        return {row[0]: tuple(row[1:]) for row in self.conn.execute(
            f"SELECT inference_id, {cols} FROM inferences WHERE request_id=?", (gid,))}

    # ---- the merge engine ----

    def merge_chunk(self, contribs: list[Contribution]) -> None:
        """Merge one chunk of incoming contributions into the ledger (SPEC §7.3)."""
        store = self.store
        cache: dict[str, list[Contribution]] = {}
        ids = sorted({c.request.request_id for c in contribs})
        msgs = sorted({m for c in contribs for m in c.message_ids()})
        hints = sorted({h for c in contribs for h in c.request_hints()})
        rix = {h: (rid, coll) for h, rid, coll in store._in_rows(
            "SELECT provider_request_id, request_id, collision FROM request_index "
            "WHERE provider_request_id IN ({})", hints)}
        collided = {h for h, (_, coll) in rix.items() if coll}
        pairs: dict[str, set[str]] = {}
        for c in contribs:
            for h, m in c.hint_pairs():
                pairs.setdefault(h, set()).add(m)
        affected: set[str] = set()
        for h in sorted(pairs):
            if h in collided:
                continue
            seen = set(pairs[h])
            gid = rix.get(h, (None, 0))[0]
            if gid is not None:
                for member in self.members(gid, cache):
                    seen |= {m for q, m in member.hint_pairs() if q == h}
            if len(seen) > 1:
                collided.add(h)
                self.counts[_DQ_COLLISION] = self.counts.get(_DQ_COLLISION, 0) + 1
                self.conn.execute(
                    "INSERT INTO request_index(provider_request_id, request_id, collision) "
                    "VALUES (?, NULL, 1) ON CONFLICT(provider_request_id) DO UPDATE SET "
                    "request_id=NULL, collision=1", (h,))
                if gid is not None:
                    affected.add(gid)
        # --- existing groups each incoming key reaches ---
        by_key: dict[tuple[str, str], str] = {}
        for (rid,) in store._in_rows("SELECT request_id FROM requests WHERE request_id "
                                     "IN ({})", ids):
            by_key[("id", rid)] = rid
        for orig, rid in store._in_rows("SELECT orig_request_id, request_id FROM merge_members "
                                        "WHERE orig_request_id IN ({})", ids):
            by_key[("id", orig)] = rid
        for msg, rid in store._in_rows("SELECT provider_message_id, request_id FROM "
                                       "message_index WHERE provider_message_id IN ({})", msgs):
            by_key[("msg", msg)] = rid
        for h, (rid, coll) in rix.items():
            if rid is not None and not coll and h not in collided:
                by_key[("rq", h)] = rid
        # --- components over incoming contributions and existing groups ---
        n = len(contribs)
        node_of: dict[str, int] = {}
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        def group_node(gid: str) -> int:
            node = node_of.get(gid)
            if node is None:
                node = node_of[gid] = len(parent)
                parent.append(node)
            return node

        first: dict[tuple, int] = {}
        for i, c in enumerate(contribs):
            for key in c.join_keys(collided):
                if key in first:
                    union(first[key], i)
                else:
                    first[key] = i
                gid = by_key.get(key)
                if gid is not None:
                    union(i, group_node(gid))
        for gid in sorted(affected):
            group_node(gid)
        comps: dict[int, tuple[list[Contribution], list[str]]] = {}
        for i, c in enumerate(contribs):
            comps.setdefault(find(i), ([], []))[0].append(c)
        for gid, node in node_of.items():
            comps.setdefault(find(node), ([], []))[1].append(gid)
        fast: list[Contribution] = []
        for root in sorted(comps):
            cs, gids = comps[root]
            if not gids and len(cs) == 1:
                fast.append(cs[0])
            else:
                self._remerge(sorted(gids), cs, cache, affected)
        self._insert_fast(fast)

    def _collided_among(self, members: Iterable[Contribution]) -> set[str]:
        hints = sorted({h for c in members for h in c.request_hints()})
        return {h for (h,) in self.store._in_rows(
            "SELECT provider_request_id FROM request_index WHERE collision=1 AND "
            "provider_request_id IN ({})", hints)}

    def _remerge(self, gids: list[str], incoming: list[Contribution],
                 cache: dict[str, list[Contribution]], affected: set[str]) -> None:
        old: list[Contribution] = []
        old_keys: set[str] = set()
        prices: dict[str, dict[str, tuple[Any, ...]]] = {}
        old_mismatch = 0
        for gid in gids:
            members = self.members(gid, cache)
            if not members:
                continue
            old.extend(members)
            old_keys |= {m.key for m in members}
            winner = min(members, key=lambda m: m.rank())
            prices[winner.key] = self._stored_prices(gid)
            usages = {_canonical_record(m.request.serving_inference.usage) for m in members
                      if m.request.serving_inference is not None}
            old_mismatch += len(usages) > 1
        new = [c for c in incoming if c.key not in old_keys]
        if not new and not (set(gids) & affected):
            return
        everything = old + new
        merged = merge_contributions(everything, self._collided_among(everything))
        self.delete_groups(gids, old)
        new_mismatch = sum(1 for mg in merged if mg.usage_mismatch)
        if new_mismatch > old_mismatch:
            self.counts[_DQ_MISMATCH] = self.counts.get(_DQ_MISMATCH, 0) + (
                new_mismatch - old_mismatch)
        for mg in merged:
            self.write_group(mg, prices.get(mg.winner.key))

    def delete_groups(self, gids: Sequence[str], members: Sequence[Contribution]) -> None:
        """Delete the merged requests *gids* (children cascade) with their member rows and the
        index rows of *members* that point at them."""
        if not gids:
            return
        conn = self.conn
        for chunk in _chunks(list(gids)):
            marks = _marks(len(chunk))
            for row in conn.execute(f"SELECT lane_key, date_utc FROM requests WHERE request_id "
                                    f"IN ({marks})", tuple(chunk)):
                self.touched_lanes.add(row[0])
                self.dirty.add(row[1])
            conn.execute(f"DELETE FROM inferences WHERE request_id IN ({marks})", tuple(chunk))
            conn.execute(f"DELETE FROM requests WHERE request_id IN ({marks})", tuple(chunk))
            conn.execute(f"DELETE FROM merge_members WHERE request_id IN ({marks})", tuple(chunk))
        gid_set = set(gids)
        msg_rows = sorted({(m, g) for c in members for m in c.message_ids() for g in gid_set})
        conn.executemany("DELETE FROM message_index WHERE provider_message_id=? AND request_id=?",
                         msg_rows)
        hint_rows = sorted({(h, g) for c in members for h in c.request_hints() for g in gid_set})
        conn.executemany("DELETE FROM request_index WHERE provider_request_id=? AND request_id=? "
                         "AND collision=0", hint_rows)

    def write_group(self, mg: MergedGroup, stored: Mapping[str, tuple[Any, ...]] | None) -> None:
        """Insert one merged request (rows, index entries, member rows when merged)."""
        req = mg.request
        infs = [inf.inference_id for att in req.attempts for inf in att.inferences]
        if stored is not None and all(i in stored for i in infs):
            priced = dict(stored)
        else:
            priced = self.store._price(req, self.pricer)
        multi = len(mg.members) > 1
        self._write_rows([(req, int(mg.fidelity), mg.priority, mg.adapter, mg.sources_mask,
                           mg.attr_prio if multi else None, priced)])
        conn = self.conn
        if multi:
            conn.executemany(_MEMBER_INSERT, [
                (req.request_id, m.key, m.request.request_id, m.adapter, m.source_id,
                 int(m.fidelity), m.priority, m.request.attribution.principal,
                 m.request.ts_start_ms, m.canon) for m in mg.members])
        msgs = sorted({msg for m in mg.members for msg in m.message_ids()})
        conn.executemany("INSERT OR REPLACE INTO message_index(provider_message_id, request_id) "
                         "VALUES (?, ?)", [(msg, req.request_id) for msg in msgs])
        hints = sorted({h for m in mg.members for h in m.request_hints()})
        conn.executemany(
            "INSERT INTO request_index(provider_request_id, request_id, collision) VALUES "
            "(?, ?, 0) ON CONFLICT(provider_request_id) DO UPDATE SET "
            "request_id=excluded.request_id WHERE collision=0",
            [(h, req.request_id) for h in hints])

    def _insert_fast(self, contribs: Sequence[Contribution]) -> None:
        if not contribs:
            return
        entries = []
        for c in contribs:
            entries.append((c.request, int(c.fidelity), c.priority, c.adapter,
                            sources_mask([c.adapter]), None,
                            self.store._price(c.request, self.pricer)))
        self._write_rows(entries)
        conn = self.conn
        conn.executemany("INSERT OR REPLACE INTO message_index(provider_message_id, request_id) "
                         "VALUES (?, ?)", [(m, c.request.request_id) for c in contribs
                                           for m in sorted(c.message_ids())])
        conn.executemany("INSERT OR IGNORE INTO request_index(provider_request_id, request_id, "
                         "collision) VALUES (?, ?, 0)",
                         [(h, c.request.request_id) for c in contribs
                          for h in sorted(c.request_hints())])

    def _write_rows(self, entries: Sequence[tuple[Any, ...]]) -> None:
        codec = self.codec
        self._load_lane_kinds(e[0].lane_key for e in entries)
        req_rows, att_rows, inf_rows = [], [], []
        for req, fidelity, priority, adapter, mask, prio, priced in entries:
            req_rows.append(codec.request_row(req, fidelity=fidelity, priority=priority,
                                              adapter=adapter, mask=mask, prio=prio))
            kind = self._lane_kind(req.lane_key)
            for a_ord, att in enumerate(req.attempts):
                att_rows.append(codec.attempt_row(att, req.request_id, a_ord))
                for i_ord, inf in enumerate(att.inferences):
                    inf_rows.append(codec.inference_row(inf, att, req, kind, i_ord)
                                    + priced[inf.inference_id])
            self.touched_lanes.add(req.lane_key)
            self.dirty.add(req_rows[-1][5])
        conn = self.conn
        try:
            conn.executemany(_REQ_INSERT, req_rows)
            conn.executemany(_ATT_INSERT, att_rows)
            conn.executemany(_INF_INSERT, inf_rows)
        except sqlite3.IntegrityError as exc:
            raise ContractViolation(
                "a request, attempt or inference id is already stored under another request "
                "(ids must be unique per logical request)") from exc

    # ---- regroup after a purge ----

    def regroup(self, gids: list[str], drop: Any) -> int:
        """Re-merge the stored groups *gids* without the members *drop* selects."""
        cache: dict[str, list[Contribution]] = {}
        deleted = 0
        for chunk in _chunks(sorted(set(gids)), 200):
            old: list[Contribution] = []
            prices: dict[str, dict[str, tuple[Any, ...]]] = {}
            for gid in chunk:
                members = self.members(gid, cache)
                old.extend(members)
                if members:
                    winner = min(members, key=lambda m: m.rank())
                    prices[winner.key] = self._stored_prices(gid)
            keep = [m for m in old if not drop(m)]
            self.delete_groups(list(chunk), old)
            deleted += len(chunk)
            merged = merge_contributions(keep, self._collided_among(keep)) if keep else []
            for mg in merged:
                self.write_group(mg, prices.get(mg.winner.key))
            deleted -= len(merged)
        self.finish()
        return deleted

    # ---- events and provider records ----

    def events(self, events: Sequence[LaneEvent]) -> None:
        rows = []
        for ev in events:
            doc = canonical(to_json(ev))
            rows.append((stable_id("ev", doc), ev.lane_key, ev.ts_ms, rollups.date_of(ev.ts_ms),
                         ev.kind.value, _json_or_none([list(p) for p in ev.attrs])))
        self.conn.executemany("INSERT OR IGNORE INTO events(event_id, lane_key, ts_ms, date_utc, "
                              "kind, attrs_json) VALUES (?, ?, ?, ?, ?, ?)", rows)

    def _upsert(self, table: str, cols: Sequence[str], key_cols: Sequence[str],
                records: Sequence[Any], row_of: Any, rec_of: Any) -> None:
        if not records:
            return
        store = self.store
        best: dict[tuple[Any, ...], Any] = {}
        for rec in records:
            row = row_of(rec)
            key = tuple(row[cols.index(k)] for k in key_cols)
            have = best.get(key)
            best[key] = rec if have is None else choose_version(have, rec)
        existing: dict[tuple[Any, ...], Any] = {}
        if len(key_cols) == 1:
            for row in store._in_rows(f"SELECT {', '.join(cols)} FROM {table} WHERE "
                                      f"{key_cols[0]} IN ({{}})", [k[0] for k in best]):
                existing[(row[cols.index(key_cols[0])],)] = rec_of(row)
        else:
            where = " AND ".join(f"{k}=?" for k in key_cols)
            for key in best:
                row = self.conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}",
                                        key).fetchone()
                if row is not None:
                    existing[key] = rec_of(row)
        writes = []
        for key, rec in best.items():
            have = existing.get(key)
            chosen = rec if have is None else choose_version(have, rec)
            if have is None or chosen is not have:
                writes.append(row_of(chosen))
        self.conn.executemany(_insert_sql(table, cols, "INSERT OR REPLACE"), writes)

    def provider_records(self, aggregates: Sequence[UsageAggregate],
                         cost_lines: Sequence[CostLine],
                         outcomes: Sequence[OutcomeAggregate]) -> None:
        self._upsert("aggregates", _AGG_COLS, ("agg_id",), aggregates, _aggregate_row,
                     _aggregate_of)
        self._upsert("cost_lines", _CL_COLS, ("line_id",), cost_lines, _cost_line_row,
                     _cost_line_of)
        self._upsert("outcomes", _OUT_COLS, ("date_utc", "team", "source_kind"), outcomes,
                     _outcome_row, _outcome_of)

    # ---- lanes and rollup bookkeeping ----

    def finish(self) -> None:
        """Refresh the denormalized lane columns of every touched lane and mark stale dates."""
        refresh_lanes(self.conn, self.touched_lanes)
        rollups.mark_dirty(self.conn, self.dirty)
        self.touched_lanes.clear()
        self.dirty.clear()


def refresh_lanes(conn: sqlite3.Connection, lane_keys: Iterable[str]) -> None:
    """Placeholder rows for lanes that have requests but no shell, the denormalized ``team`` /
    ``billing_class`` of the lanes' first requests, and no placeholder for a lane without
    requests (inside the caller's transaction)."""
    keys = sorted(set(lane_keys))
    bclass = _BILLING_CLASS_SQL.format("r.eff_billing_path")
    for chunk in _chunks(keys):
        marks = _marks(len(chunk))
        params = tuple(chunk)
        conn.execute(
            "INSERT INTO lanes(lane_key, session_key) SELECT r.lane_key, MIN(r.session_key) "
            f"FROM requests r WHERE r.lane_key IN ({marks}) AND NOT EXISTS (SELECT 1 FROM "
            "lanes x WHERE x.lane_key = r.lane_key) GROUP BY r.lane_key", params)
        conn.execute(
            "UPDATE lanes SET team=(SELECT r.team FROM requests r WHERE r.lane_key = "
            "lanes.lane_key ORDER BY r.ts_start_ms, r.seq, r.request_id LIMIT 1), "
            f"billing_class=(SELECT {bclass} FROM requests r WHERE r.lane_key = "
            "lanes.lane_key ORDER BY r.ts_start_ms, r.seq, r.request_id LIMIT 1) "
            f"WHERE lane_key IN ({marks})", params)
        conn.execute(
            "UPDATE lanes SET session_key=(SELECT MIN(r.session_key) FROM requests r WHERE "
            f"r.lane_key = lanes.lane_key) WHERE kind IS NULL AND lane_key IN ({marks}) AND "
            "EXISTS (SELECT 1 FROM requests r WHERE r.lane_key = lanes.lane_key)", params)
        conn.execute(
            f"DELETE FROM lanes WHERE kind IS NULL AND lane_key IN ({marks}) AND NOT EXISTS "
            "(SELECT 1 FROM requests r WHERE r.lane_key = lanes.lane_key)", params)


def delete_requests(conn: sqlite3.Connection, where_sql: str, params: Sequence[Any]) -> int:
    """Delete every merged request matching ``requests`` filter *where_sql* (alias-free SQL on
    the ``requests`` columns) with its attempts, inferences, member rows and index entries;
    refreshes the touched lanes and marks the touched dates stale. Returns the number deleted
    (inside the caller's transaction)."""
    sub = f"SELECT request_id FROM requests WHERE {where_sql}"
    lanes: set[str] = set()
    dates: set[str] = set()
    for lane_key, date in conn.execute(f"SELECT DISTINCT lane_key, date_utc FROM requests "
                                       f"WHERE {where_sql}", tuple(params)):
        lanes.add(lane_key)
        dates.add(date)
    conn.execute(f"DELETE FROM message_index WHERE request_id IN ({sub})", tuple(params))
    conn.execute(f"DELETE FROM request_index WHERE collision=0 AND request_id IN ({sub})",
                 tuple(params))
    conn.execute(f"DELETE FROM merge_members WHERE request_id IN ({sub})", tuple(params))
    conn.execute(f"DELETE FROM inferences WHERE request_id IN ({sub})", tuple(params))
    conn.execute(f"DELETE FROM attempts WHERE request_id IN ({sub})", tuple(params))
    n = conn.execute(f"DELETE FROM requests WHERE {where_sql}", tuple(params)).rowcount
    refresh_lanes(conn, lanes)
    rollups.mark_dirty(conn, dates)
    return n
