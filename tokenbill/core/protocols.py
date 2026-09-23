"""Structural interfaces every implementation package codes against (SPEC §3.6).

The protocols are ``runtime_checkable`` so conformance helpers can ``isinstance``-check an
implementation's surface; behavior is pinned by the ``core.testing`` conformance suites (F-KIT).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tokenbill.core.labels import Basis
from tokenbill.core.records import (
    CostLine,
    Inference,
    Lane,
    OutcomeAggregate,
    PricingContext,
    Request,
    UsageAggregate,
    UsageBuckets,
    UsageRecord,
    UsageSource,
)
from tokenbill.core.types import (
    AnalysisContext,
    CalibrationReport,
    ClusterDay,
    Finding,
    IngestOptions,
    IngestResult,
    LaneIndexRow,
    LedgerCostRow,
    Policy,
    PricedInference,
    RawAggregate,
    ReceiptRow,
    ReplayResult,
    ResolvedRates,
    UnitRates,
)

if TYPE_CHECKING:
    from tokenbill.core.cache_rules import CacheRules  # F-SEM (wave 1)

__all__ = ["Adapter", "CacheRulesProvider", "Detector", "LedgerStore", "Pricer", "Replayer"]


@runtime_checkable
class Pricer(Protocol):
    rate_card_sha256: str
    # LIST or CONTRACT (inferences on billing_path "subscription" are priced on basis
    # LIST_EQUIVALENT regardless, D26)
    basis: Basis

    def resolve(self, ctx: PricingContext, *, ts_ms: int) -> ResolvedRates | None: ...

    def price_inference(self, inf: Inference, *, ts_ms: int) -> PricedInference: ...

    def price_usage(self, usage: UsageBuckets, ctx: PricingContext, *, ts_ms: int,
                    billable: bool | None = True, usage_source: UsageSource = UsageSource.FINAL,
                    output_upper: int | None = None) -> PricedInference: ...

    def unit_rates(self, ctx: PricingContext, *, ts_ms: int) -> UnitRates | None: ...

    def min_cacheable_tokens(self, ctx: PricingContext, *, ts_ms: int) -> int | None: ...

    def supports(self, ctx: PricingContext, feature: str, *, ts_ms: int) -> bool: ...

    def tokenizer_family(self, ctx: PricingContext, *, ts_ms: int) -> str | None: ...


@runtime_checkable
class Adapter(Protocol):
    name: str                             # registry key, e.g. "claude-code"
    capabilities: frozenset[str]          # declared maximum capabilities (§5.1)

    def sniff(self, path: Path, head: bytes) -> bool: ...

    def read(self, path: Path, opts: IngestOptions) -> IngestResult: ...


@runtime_checkable
class CacheRulesProvider(Protocol):
    def rules_for(self, provider: str, channel: str, model: str) -> CacheRules: ...


@runtime_checkable
class Replayer(Protocol):
    def replay(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
               rules: CacheRulesProvider, calibration: CalibrationReport | None,
               static_prefix_floor: Mapping[tuple[str, str], int] | None = None,
               keep_outcomes: bool = False) -> ReplayResult: ...


@runtime_checkable
class Detector(Protocol):
    id: str                               # class id (registry key)
    version: str
    kinds: tuple[str, ...]                # finding kinds it may emit
    requires: frozenset[str]              # capabilities needed

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]: ...
    # Contract: cross-lane logic is confined to cohorts core.findings.cohort_key(lane) =
    # (team, lane_kind, billing_class) (plus finer keys inside a cohort), so running per shard and
    # concatenating equals running on all lanes (§3.21). Aggregate detectors read ctx.aggregates /
    # ctx.cost_lines and ignore lanes.


@runtime_checkable
class LedgerStore(Protocol):
    def ingest(self, result: IngestResult, *, pricer: Pricer | None = None) -> dict[str, int]: ...

    def reprice(self, pricer: Pricer, *, since_ms: int | None = None,
                until_ms: int | None = None) -> int: ...

    def iter_lanes(self, *, since_ms: int | None = None, until_ms: int | None = None,
                   # keys: team, lane_kind, billing_class, workspace_id, agent_product,
                   #       workload_class
                   where: Mapping[str, str] | None = None,
                   lane_keys: Collection[str] | None = None) -> Iterator[Lane]: ...

    def iter_requests(self, *, since_ms: int | None = None, until_ms: int | None = None,
                      where: Mapping[str, str] | None = None) -> Iterator[Request]: ...

    def iter_usage_records(self, *, since_ms: int | None = None,
                           until_ms: int | None = None) -> Iterator[UsageRecord]: ...

    def lane_index(self, *, since_ms: int, until_ms: int) -> Iterator[LaneIndexRow]: ...

    def lane_first_reads(self, *, since_ms: int,
                         until_ms: int) -> Iterator[tuple[str, str, int]]: ...
        # (cache_scope_key, model, R of the lane's first request) — for
        # core.transitions.static_prefix_floor

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str]) -> int: ...
        # COUNT(DISTINCT principal) over requests matching `where`; never returns ids (§8.4)

    def aggregates(self, source_kind: str | None = None, **window: int) -> list[UsageAggregate]: ...

    def cost_lines(self, source_kind: str | None = None, **window: int) -> list[CostLine]: ...

    def outcomes(self, **window: int) -> list[OutcomeAggregate]: ...

    def aggregate(self, *, since_ms: int, until_ms: int, group_by: Sequence[str],
                  where: Mapping[str, str] | None = None,
                  pricer: Pricer | None = None) -> RawAggregate: ...

    def cluster_days(self, *, cluster_kind: str, since: str, until: str) -> list[ClusterDay]: ...

    def cost_rows(self, *, since_ms: int, until_ms: int,
                  group_by: Sequence[str]) -> list[LedgerCostRow]: ...
        # group_by ⊆ {date, provider, channel, model, team, cost_center, project, workspace_id,
        # lane_kind, workload_class, agent_product, billing_path}; one row per group × bucket ×
        # basis; principal never allowed

    def get_cursor(self, source_id: str, unit_hmac: str) -> tuple[int, str, int, int] | None: ...
        # (byte_offset, head_sha, size, mtime_ns)

    def set_cursor(self, source_id: str, unit_hmac: str, *, byte_offset: int, head_sha: str,
                   size: int, mtime_ns: int) -> None: ...

    def put_findings(self, run_id: str, findings: Sequence[Finding]) -> None: ...

    def findings(self, run_id: str | None = None) -> list[Finding]: ...     # latest run when None

    def put_receipt(self, row: ReceiptRow) -> None: ...

    def receipts(self, *, lever_class: str | None = None) -> list[ReceiptRow]: ...

    def purge(self, *, principal: str | None = None, before_ms: int | None = None,
              actor: str) -> int: ...

    def audit(self, actor: str, action: str, detail: Mapping[str, object]) -> None: ...

    def meta(self) -> dict[str, str]: ...
