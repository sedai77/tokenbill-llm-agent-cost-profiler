"""Structural interfaces every implementation package codes against (SPEC §3.6).

The protocols are ``runtime_checkable`` so conformance helpers can ``isinstance``-check an
implementation's surface; behavior is pinned by the ``core.testing`` conformance suites (F-KIT).

Channel-extension protocols (GitHub Copilot, CORE-AMENDMENTS C-27): ``ChannelReconciler``,
``SectionRenderer``, ``ExtRecordStore`` and ``LedgerStats`` (``source_stats``) — a separate
protocol, so ``LedgerStore`` gains no member (``isinstance(MemoryStore(), LedgerStore)`` stays
true); ``LedgerStore.count_users`` gains the ``source`` keyword.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tokenbill.core.labels import Basis
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    Inference,
    Lane,
    LicenseSnapshot,
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
    ReconciliationReport,
    ReplayResult,
    ResolvedRates,
    RunResult,
    UnitRates,
)

if TYPE_CHECKING:
    from tokenbill.core.cache_rules import CacheRules  # F-SEM (wave 1)

__all__ = [
    "Adapter",
    "CacheRulesProvider",
    "ChannelReconciler",
    "Detector",
    "ExtRecordStore",
    "LedgerStats",
    "LedgerStore",
    "Pricer",
    "Replayer",
    "SectionRenderer",
]


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
    # Optional class attributes read by core.registry.run_detectors with getattr defaults (C-25):
    #   extension: str | None = None   (runs only with f"ext:{extension}" in ctx.capabilities)
    #   aggregate: bool = False        (True: once per run, run_detectors(aggregates_only=True))
    #   families: frozenset[str] | None = None   (product families whose lanes it sees)


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

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    source: str = "requests") -> int: ...
        # COUNT(DISTINCT principal) over requests matching `where`; never returns ids (§8.4).
        # source "cost_lines" (C-27) counts DISTINCT CostLine.principal; `where` keys for cost
        # lines: team, cost_center, channel, model, sku, workspace_id, cost_type

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


# ---------------------------------------------------------------------------------------------
# channel extensions (GitHub Copilot, C-27)
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class LedgerStats(Protocol):
    """Optional ledger capability (not a ``LedgerStore`` member): Σ of the integer
    ``sources.stats_json`` values by key, over every source or one adapter's (the Copilot
    reconciler's ``rounding_remainders``). Callers check ``isinstance(store, LedgerStats)``."""

    def source_stats(self, *, adapter: str | None = None) -> dict[str, int]: ...


@runtime_checkable
class ExtRecordStore(Protocol):
    """An extension's record store for ``LicenseSnapshot`` / ``ActivityDay`` / ``ConfigSnapshot``
    (same database file as the ledger, own tables). ``put`` accepts ``p_`` values only under the key
    ids recorded in the ledger's ``meta`` (its own org key id or the adopted one, R-E21)."""

    name: str

    def put(self, result: IngestResult, *, principal_key_id: str | None) -> dict[str, int]: ...

    def licenses(self, **window: int) -> list[LicenseSnapshot]: ...

    def activity(self, **window: int) -> list[ActivityDay]: ...

    def config(self, **window: int) -> list[ConfigSnapshot]: ...

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    source: str) -> int: ...
        # source "licenses" | "activity"; COUNT(DISTINCT principal), never ids (§8.4)

    def retain(self, *, identity_before_ms: int) -> int: ...

    def purge(self, *, principal: str | None, before_ms: int | None, actor: str) -> int: ...


@runtime_checkable
class ChannelReconciler(Protocol):
    """A channel extension's reconciler (``ExtensionSpec.reconciler``): reads what it needs through
    the ledger and record stores and returns one report whose ``decisions`` carry its reusable
    per-source / per-entity-month decisions (C-17)."""

    def __call__(self, ledger: LedgerStore, record_stores: Sequence[ExtRecordStore], pricer: Pricer,
                 *, since_ms: int, until_ms: int, tolerance_pct: str, unexplained_pct: str,
                 closed_only: bool, today: str,
                 rounding_remainders: Mapping[str, Decimal] | None = None
                 ) -> ReconciliationReport: ...


@runtime_checkable
class SectionRenderer(Protocol):
    """Renders an extension's ``RunResult`` slot (e.g. ``RunResult.copilot``) as one output
    section: terminal text (sanitized, ≤ *width* columns), an escaped HTML ``<section>`` without
    scripts, and a JSON object (MONEY via ``core.labels.figure_json``) or None."""

    name: str

    def terminal(self, result: RunResult, *, width: int) -> str: ...

    def html(self, result: RunResult) -> str: ...

    def json(self, result: RunResult) -> dict | None: ...
