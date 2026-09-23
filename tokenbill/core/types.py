"""Shared result types (SPEC §3.5).

Plain frozen dataclasses transcribed from the SPEC. Construction-time invariants that the SPEC
states (``PublishedAggregate``'s publish token) are enforced; behavior owned by F-SEM
(``Policy.spec()``, ``Policy.combine()``) is delegated lazily to ``tokenbill.core.policy``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import Basis, Calibration, Figure, Finality
from tokenbill.core.money import scaled_to_nano
from tokenbill.core.records import (
    Attribution,
    ContentTier,
    CostLine,
    Inference,
    LaneEvent,
    OutcomeAggregate,
    Request,
    Session,
    UsageAggregate,
    UsageBuckets,
)

if TYPE_CHECKING:
    from tokenbill.core.protocols import CacheRulesProvider, Pricer, Replayer

__all__ = [
    "AbResult", "ActionPlan", "AggRow", "AnalysisContext", "BillSummary", "CalibrationPartial",
    "CalibrationReport", "ChannelVerdict", "CheckResult", "CheckViolation", "ClusterDay",
    "ContractOverlay", "DataQualityNote", "Discrepancy", "EvidenceItem", "Finding", "Fix",
    "GuardResult", "IngestOptions", "IngestResult", "LaneIndexRow", "LedgerCostRow", "LeverResult",
    "MeasurePlan", "MeasurementResult", "Modifier", "PanelRow", "Policy", "PolicyEntry",
    "PolicyPack", "PricedInference", "PricedLine", "PricedTotal", "PricingReport", "PrivacyInfo",
    "PublishedAggregate",
    "QuarantineItem", "RateCardInfo", "RateLayer", "RateRow", "RawAggregate", "ReceiptRow",
    "ReconRow", "ReconciliationReport", "ReplayRequestOutcome", "ReplayResult", "ResolvedRates",
    "RunResult", "Scope", "ShardKey", "SourceCitation", "SourceInfo", "Transition", "UnitRates",
]


# ---------- ingest ----------
@dataclass(frozen=True, slots=True)
class DataQualityNote:
    code: str            # dotted, stable: see §5.1 list, e.g. "dq.message_start_only"
    severity: str        # "info" | "warn" | "error"
    count: int
    detail: str          # content-free, ≤ 256 chars
    # token magnitude where relevant (adapters report tokens, never dollars)
    tokens: int | None = None
    figure: Figure | None = None      # set only by the pipeline after pricing (adapters leave None)


@dataclass(frozen=True, slots=True)
class QuarantineItem:
    source_id: str
    locator: str
    reason: str      # never the raw line


@dataclass(frozen=True, slots=True)
class SourceInfo:
    source_id: str
    adapter: str
    name_hmac: str
    sha256: str
    bytes: int
    name_key_id: str | None          # key id of every h_ value in the result (None: no h_ values)
    # key id of every p_/c_ value in the result (None: none or only r_ refs)
    principal_key_id: str | None


@dataclass(frozen=True, slots=True)
class IngestOptions:
    content_tier: ContentTier = ContentTier.NONE
    identity_mode: str = "install"   # "install" | "central" | "two-stage" | "central-ingest" (§5.1)
    # HMACs MCP/skill/plugin/tool names, cwd, repo, api keys, workspaces (h_)
    name_key: bytes = b""
    name_key_id: str = ""
    # install key (install), collection key (two-stage), org key (central-ingest); None in central
    # collectors
    principal_key: bytes | None = None
    principal_key_id: str | None = None
    principal_ref: str | None = None # opaque employee/device id supplied by MDM (collector modes)
    # defaults from --attr / OTEL_RESOURCE_ATTRIBUTES / MDM
    attribution: Attribution = Attribution()
    # raw actor ref → team, applied at ingest then discarded
    team_map: tuple[tuple[str, str], ...] = ()
    k_anonymity: int = 5             # used by adapters that aggregate people at ingest (§5.11)
    # skill/MCP/plugin/tool names allowed in clear text
    name_allowlist: frozenset[str] = frozenset()
    since_ms: int | None = None
    until_ms: int | None = None
    # quarantine bad records; False = first bad record raises SourceError
    lenient: bool = True
    # trace@2: rebuild inferences from raw_usage with current conventions
    renormalize: bool = False
    now_ms: int = 0                  # injected clock (determinism)


@dataclass
class IngestResult:
    source: SourceInfo
    requests: list[Request]
    # lanes may carry no requests; core/lanes.py assembles final Lanes
    sessions: list[Session]
    events: list[LaneEvent]
    aggregates: list[UsageAggregate]
    cost_lines: list[CostLine]
    outcomes: list[OutcomeAggregate]
    quarantined: list[QuarantineItem]
    notes: list[DataQualityNote]
    stats: dict[str, int]            # "lines", "records", "requests", "duplicate_lines", …
    capabilities: frozenset[str]     # capabilities actually present (§5.1)
    naive_usage: dict[str, UsageBuckets] = field(default_factory=dict)
                                     # Claude Code only: Σ usage over every assistant line, per
                                     # model


# ---------- pricing registry format (frozen contract; RATES implements load/validate) ----------
@dataclass(frozen=True, slots=True)
class SourceCitation:
    url: str
    retrieved: str
    finding: str | None


@dataclass(frozen=True, slots=True)
class RateRow:
    row_id: str                      # "<provider>/<channel>/<model>/<effective_from>"
    provider: str
    channel: str
    model: str
    aliases: tuple[str, ...]
    generation: str                  # e.g. "5.5", "4.6"; compared numerically by modifiers
    effective_from: str              # "YYYY-MM-DD" inclusive
    effective_to: str | None         # exclusive; None = open
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_mult: Decimal | None  # None → provider has no cache reads
    cache_write_5m_mult: Decimal | None
    cache_write_1h_mult: Decimal | None
    cache_write_other_mult: Decimal | None      # OpenAI 5.6+: 1.25 (30m)
    cache_write_other_ttl_s: int | None
    # bucket → USD/MTok as published (validator)
    published_absolute: tuple[tuple[str, Decimal], ...]
    min_cacheable_tokens: int | None
    tokenizer_family: str            # "claude-4.7+" | "claude-legacy" | "openai-o200k" | …
    per_request_usd: tuple[tuple[str, Decimal], ...]      # ("web_search", Decimal("0.01"))
    long_context_threshold: int | None
    # bucket → rate for the whole request
    long_context_usd_per_mtok: tuple[tuple[str, Decimal], ...]
    # "fast_mode","inference_geo","1m_context","batch","keepalive", …
    supports: tuple[str, ...]
    # False = VERIFY row: loaded, never priced ("unverified rate row")
    enabled: bool
    verified_on: str
    sources: tuple[SourceCitation, ...]
    # promotion id (core.catalog.PROMOTIONS) when the row is promotional
    promotion: str | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class Modifier:
    modifier_id: str                 # "anthropic.batch", "anthropic.inference_geo.us", …
    kind: str                        # "multiply" | "replace_base"
    factor: Decimal | None           # multiply
    base_usd_per_mtok: tuple[tuple[str, Decimal], ...]   # replace_base: ("input", …), ("output", …)
    applies_to: tuple[str, ...]      # bucket names or ("*",)
    # sorted predicates: service_tier, speed, inference_geo, endpoint_scope, channel_in (comma
    # list), model_in (comma list), generation_gte
    when: tuple[tuple[str, str], ...]
    stacking: str                    # "documented" | "assumed"
    sources: tuple[SourceCitation, ...]


@dataclass(frozen=True, slots=True)
class RateLayer:
    # "builtin@2026-09-23" | "user:<file>" | "model-price" | "contract:<name>"
    name: str
    schema: str                      # "tokenbill/rates@1"
    as_of: str
    rows: tuple[RateRow, ...]
    modifiers: tuple[Modifier, ...]
    sha256: str                      # of the canonical JSON of the layer


@dataclass(frozen=True, slots=True)
class ContractOverlay:
    name: str
    multiplier: Decimal | None       # applied to every bucket after list resolution
    # model → ((bucket, usd_per_mtok), …)
    overrides: tuple[tuple[str, tuple[tuple[str, Decimal], ...]], ...]
    effective_from: str
    effective_to: str | None
    derived: bool                    # True when produced by reconcile --suggest-contract
    # e.g. ("cache_write_1h",) when derived from modelPricing cacheWrite
    assumed_fields: tuple[str, ...]
    channels: tuple[str, ...] = ()   # channels it applies to (empty = all)
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedRates:
    row_id: str
    channel: str
    model: str
    input: Decimal
    output: Decimal
    cache_read: Decimal | None
    cache_write_5m: Decimal | None
    cache_write_1h: Decimal | None
    cache_write_other: Decimal | None
    per_request: tuple[tuple[str, Decimal], ...]
    modifier_ids: tuple[str, ...]
    stacking_assumed: bool
    layer: str                       # "builtin" | "user" | "contract"
    min_cacheable_tokens: int | None
    tokenizer_family: str
    long_context_band: bool          # True when these are band rates
    # high-side rates when endpoint_scope is unknown (§6.2)
    scope_range: ResolvedRates | None = None


_UNIT_BUCKETS = {
    "uncached_input": "uncached",
    "uncached": "uncached",
    "input": "uncached",
    "cache_read": "cache_read",
    "cache_write_5m": "cache_write_5m",
    "cache_write_1h": "cache_write_1h",
    "cache_write_other": "cache_write_other",
    "cache_write_unknown": "cache_write_5m",   # unit rates are the point (low) rates (§6.4, R5)
    "output": "output",
}


@dataclass(frozen=True, slots=True)
class UnitRates:
    """Exact integer rates for replay hot loops: each bucket rate = numerator × 10^-scale_exp
    USD/token."""

    # ≤ 24; chosen as the smallest exponent making every bucket integral
    scale_exp: int
    uncached: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_other: int
    output: int
    web_search_nano: int
    row_id: str

    def __post_init__(self) -> None:
        if type(self.scale_exp) is not int or not 0 <= self.scale_exp <= 24:
            raise ContractViolation("UnitRates.scale_exp: must be an int in [0, 24]")
        for name in ("uncached", "cache_read", "cache_write_5m", "cache_write_1h",
                     "cache_write_other", "output", "web_search_nano"):
            if type(getattr(self, name)) is not int:
                raise ContractViolation(f"UnitRates.{name}: must be an int")

    def bucket_nano(self, bucket: str, tokens: int) -> int:
        """``scaled_to_nano(tokens × rate, scale_exp)`` for a PricedLine bucket name;
        ``web_search`` is
        per request (``tokens`` = requests × ``web_search_nano``). ``cache_write_unknown`` uses the
        5m
        (low/point) rate."""
        if bucket == "web_search":
            return tokens * self.web_search_nano
        attr = _UNIT_BUCKETS.get(bucket)
        if attr is None:
            raise ContractViolation("UnitRates.bucket_nano: unknown bucket")
        return scaled_to_nano(tokens * getattr(self, attr), self.scale_exp)


@dataclass(frozen=True, slots=True)
class PricedLine:
    bucket: str                      # "uncached_input"|"cache_read"|…|"output"|"web_search"|…
    quantity: int
    unit_usd_per_mtok: str           # decimal string after modifiers (per request for server tools)
    amount_nano: int                 # point, rounded once
    # range lines only (unknown TTL / scope, uncertain billing, placeholder)
    low_nano: int | None
    high_nano: int | None
    exact: bool                      # True iff billed tokens × sourced rate with no range (R9)
    rate_row_id: str
    modifier_ids: tuple[str, ...]
    layer: str


@dataclass(frozen=True, slots=True)
class PricedInference:
    inference_id: str | None
    lines: tuple[PricedLine, ...]
    figure: Figure                   # whole inference: EXACT iff every line is exact
    exact_nano: int                  # Σ amount of exact lines (0 when unpriced)
    # Σ of range lines (ESTIMATED, same basis), None when every line exact
    estimated: Figure | None
    unpriced_reason: str | None


@dataclass(frozen=True, slots=True)
class PricedTotal:
    # Σ exact lines on billed bases (LIST/CONTRACT): the billed-eligible number
    exact: Figure
    # Σ range lines on billed bases; shown beside, never inside, the bill
    estimated: Figure | None
    # Σ every line on basis LIST_EQUIVALENT (EXACT iff all its lines are)
    allowance: Figure | None
    priced_inferences: int
    unpriced_inferences: int
    unpriced_tokens: int
    coverage: str                    # decimal string: priced billable tokens / all billable tokens


@dataclass(frozen=True, slots=True)
class Discrepancy:
    row_id: str
    field: str
    ours: str
    theirs: str
    source: str
    # False for LiteLLM/OpenRouter cross-check feeds (warnings only)
    authoritative: bool


@dataclass(frozen=True, slots=True)
class PricingReport:
    kind: str                        # "show" | "verify" | "diff"
    rows: tuple[RateRow, ...]
    modifiers: tuple[Modifier, ...]
    discrepancies: tuple[Discrepancy, ...]
    stale_rows: tuple[str, ...]
    ok: bool


# ---------- simulation ----------
def _policy_module():  # lazy import of F-SEM's module (it does not exist before wave 1)
    from tokenbill.core import policy  # type: ignore[attr-defined]

    return policy


@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    # (selector, "5m"|"1h"), e.g. (("lane_kind:main","1h"),)
    ttl: tuple[tuple[str, str], ...] = ()
    keepalive: tuple[str, int, int] | None = None  # (selector, interval_s, max_idle_s)
    # (window_tokens, summary_tokens | None)
    compaction_window: tuple[int, int | None] | None = None
    cold_resume: tuple[str, int] | None = None     # ("compact"|"clear", min_context_tokens)
    model_remap: tuple[tuple[str, str], ...] = ()  # (selector, target model id)
    # (selector, max_level, thinking_scale decimal string)
    effort: tuple[tuple[str, str, str], ...] = ()
    fast_off: bool = False
    geo_global: bool = False
    regional_to_global: bool = False
    batch: str | None = None                       # predicate id: "eligible"
    # "restore_caching","stagger_fanout","retry_backoff_cap", "fallback_credit","shared_ci_prefix"
    repairs: tuple[str, ...] = ()
    # block-level only: "observed"|"end"|"static_plus_end"|"every_15"
    breakpoint_policy: str | None = None

    @classmethod
    def observed(cls) -> Policy:
        """The observed (identity) policy: every field at its default, name ``"observed"``."""
        return cls(name="observed")

    def is_observed(self) -> bool:
        """True when every field except ``name`` is at its default (the policy changes nothing)."""
        return dataclasses.replace(self, name="observed") == Policy(name="observed")

    def combine(self, other: Policy) -> Policy:
        """Union of two policies; conflicting scalar fields raise ContractViolation
        (core.policy.combine)."""
        return _policy_module().combine(self, other)

    def spec(self) -> str:
        """Canonical grammar string (SPEC §9.5; core.policy.to_spec)."""
        return _policy_module().to_spec(self)


# Selector grammar: "all" | "lane_kind:<LaneKind>" | "agent_type:<name>" | "team:<team>" |
# "agent_product:<name>" | "billing_path:<path>" | "workload:<class>" | "model:<id>";
#                   comma = AND (§9.5).


@dataclass(frozen=True, slots=True)
class Transition:
    request_id: str
    lane_key: str
    index: int                   # index ≥ 1 within the lane
    gap_ms: int                  # ts_start_i − ts_start_{i−1}
    total: int                   # T_i
    reads: int                   # R_i
    prev_prefix: int             # P_{i−1}
    expected_reuse: int          # E_i
    missed: int                  # M_i
    is_miss_event: bool
    cause: str                   # §3.15 cause slugs; "hit" when not a miss event
    sub_cause: str | None
    ttl_s: int | None            # τ_i in seconds; None when unknown
    ambiguous: bool              # |gap − τ| ≤ 10 s
    predicted_hit: bool | None   # documented one-step-ahead prediction (None: excluded)
    diag_reason: str | None      # canonical CacheDiagnostic.reason


@dataclass(frozen=True, slots=True)
class ReplayRequestOutcome:
    request_id: str
    usage: UsageBuckets          # serving-inference usage under the policy
    extra: tuple[Inference, ...] # keepalive pings, inserted compaction/summary calls
    # POINT cost of every billable inference of the request, policy applied
    cost_nano: int | None
    low_nano: int | None
    high_nano: int | None
    changed: bool                # False ⇒ usage and cost identical to the priced ledger


@dataclass(frozen=True, slots=True)
class ReplayResult:
    policy: Policy
    mode: str                    # "documented" | "calibrated"
    baseline: Figure             # observed point cost of the replayed lanes; basis of the lanes
    # policy cost (ESTIMATED unless the policy is observed, then == baseline)
    cost: Figure
    # baseline − cost, per request then summed (ESTIMATED; ranges crosswise)
    saving: Figure
    per_lane: tuple[tuple[str, int], ...]   # lane_key → policy point nano
    outcomes: tuple[ReplayRequestOutcome, ...] | None
    assumptions: tuple[str, ...]
    calibration: Calibration
    added_calls: int
    keepalive_pings: int
    lanes_skipped: tuple[tuple[str, str], ...]   # lane_key → reason
    n_lanes: int = 0
    n_requests: int = 0
# Precondition: every replayed lane has the same billing class (billed | allowance); mixed input
# raises
# UsageError. core.shards.merge_replay adds results of disjoint lane sets.


@dataclass(frozen=True, slots=True)
class CalibrationPartial:
    """Mergeable sums for the two-pass streaming model gate (§9.6); merge = field-wise addition."""

    granularity: str
    period_billed: tuple[tuple[str, int], ...]                 # period → billed point nano
    # period → predicted nano (documented)
    period_documented: tuple[tuple[str, int], ...]
    # period → out-of-fold calibrated nano
    period_calibrated: tuple[tuple[str, int], ...]
    rho_counts: tuple[tuple[int, str, int, int], ...]          # (fold, gap band, hits, trials)
    # (predicted cause, canonical server reason, n)
    confusion: tuple[tuple[str, str, int], ...]
    ttl_corroboration: tuple[int, int]
    unlabeled: int
    no_comparison_labels: int


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    granularity: str             # "day" | "month"
    n_periods: int
    status: str                  # "pass" | "fail" | "insufficient_data"
    mode_used: str | None        # "documented" | "calibrated" | None
    nmbe_pct: str | None         # decimal strings, documented mode
    cvrmse_pct: str | None
    nmbe_pct_calibrated: str | None
    cvrmse_pct_calibrated: str | None
    thresholds: tuple[str, str]  # ("5","15") monthly or ("10","30") daily
    # (gap band, hits, trials, wilson_low, wilson_high)
    rho: tuple[tuple[str, int, int, str, str], ...]
    diag_confusion: tuple[tuple[str, str, int], ...]  # (predicted cause, server reason, count)
    diag_precision_recall: tuple[tuple[str, str, str], ...]  # (class, precision, recall)
    unlabeled: int
    no_comparison_labels: int    # previous_message_not_found + unavailable
    ttl_corroboration: tuple[int, int]
    notes: tuple[str, ...]

    def calibration(self) -> Calibration:
        """CALIBRATED iff ``status == "pass"``, else UNCALIBRATED."""
        return Calibration.CALIBRATED if self.status == "pass" else Calibration.UNCALIBRATED


# ---------- shards (D30) ----------
@dataclass(frozen=True, slots=True)
class ShardKey:
    team: str | None             # None = unattributed requests
    lane_kind: str | None        # None = every lane kind of the team (team below the shard cap)


@dataclass(frozen=True, slots=True)
class LaneIndexRow:
    lane_key: str
    team: str | None
    lane_kind: str
    billing_class: str
    requests: int
    point_nano: int              # for shard planning and stratified sampling


# ---------- findings ----------
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    # "transition"|"event"|"block_divergence"|"attempt_chain"|"aggregate"
    kind: str
    ref: str                     # request/lane/event id (pseudonymous)
    attrs: tuple[tuple[str, str | int], ...]


@dataclass(frozen=True, slots=True)
class Fix:
    text: str
    # flattened key path → JSON value string (ALLOWLIST keys)
    config_patch: tuple[tuple[str, str], ...] | None
    # "claude-code-managed-settings"|"litellm"|"code"|"gateway"|"ci"|"sdk"
    target: str | None
    doc_url: str | None
    gates: tuple[str, ...] = ()  # applicability, e.g. "claude-code>=2.1.267"


@dataclass(frozen=True, slots=True)
class Scope:
    # sorted; team/repo/lane_kind/model/agent_type/workspace/cohort/…
    dims: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Finding:
    # core.findings.finding_id(detector_id, kind, scope) — shard-independent
    finding_id: str
    detector_id: str             # class id, e.g. "cache.miss-by-cause"
    kind: str                    # emitted kind, e.g. "ttl-expiry" (§10 tables)
    detector_version: str
    # "breaker"|"lever"|"premium"|"failure"|"attribution"|"data-quality"|"aggregate"
    category: str
    # "rate"|"cache_transform"|"trajectory"|"behavioral"|"hygiene"|"none"
    lever_class: str
    audience: str                # "org" | "self"
    title: str                   # ≤ 120 chars, generated, never content
    summary: str                 # ≤ 400 chars
    scope: Scope
    n_events: int
    n_lanes: int
    n_users: int
    first_seen_ms: int
    # what the pattern cost in the window (EXACT where it is billed arithmetic)
    cost_observed: Figure
    # standalone counterfactual (None = no mechanical repair / attribution only)
    recoverable: Figure | None
    recoverable_shapley: Figure | None = None
    projected_monthly: Figure | None = None
    lever_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()   # ≤ 20, sorted by (−magnitude, ref)
    fix: Fix | None = None
    confidence: str = "medium"   # "high"|"medium"|"low"
    # e.g. "cache_miss_reason previous_message_not_found: 128/128"
    validated_against: str | None = None
    needs_eval: bool = False
    references: tuple[str, ...] = ()       # research finding ids


@dataclass(frozen=True)
class AnalysisContext:
    pricer: Pricer
    rules: CacheRulesProvider
    replayer: Replayer | None
    calibration: CalibrationReport | None
    window: tuple[int, int]      # [start_ms, end_ms)
    capabilities: frozenset[str]
    # detector overrides (decimal strings)
    thresholds: Mapping[str, str] = field(default_factory=dict)
    k_anonymity: int = 5
    self_principal: str | None = None
    # reason; enables session+team naming for tail findings only
    break_glass: str | None = None
    now_ms: int = 0
    static_prefix_floor: Mapping[tuple[str, str], int] = field(default_factory=dict)
        # (scope, model) → S
    aggregates: tuple[UsageAggregate, ...] = ()   # for aggregate-level detectors (org scan)
    cost_lines: tuple[CostLine, ...] = ()
    shard: ShardKey | None = None                 # informational; detectors must not depend on it


@dataclass(frozen=True, slots=True)
class PolicyEntry:
    key: str                     # e.g. "promptCacheTtl" or "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    value_json: str
    projection: Figure | None
    lever_id: str
    needs_eval: bool
    verified_key: bool           # False → emitted only as a comment (VERIFY key)
    min_version: str | None
    note: str


@dataclass(frozen=True, slots=True)
class PolicyPack:
    target: str                  # "claude-code" | "litellm" | "sdk"
    cohort: str                  # "all" or cohort id (team / MDM group)
    merge_patch_json: str        # RFC 7386 merge patch against --current (canonical JSON)
    rollback_patch_json: str     # merge patch restoring the previous values (null for absent keys)
    entries: tuple[PolicyEntry, ...]
    otel_resource_attributes: str   # "tokenbill.arm=<lever>,tokenbill.wave=<n>"
    readme_md: str
    hooks: tuple[tuple[str, str], ...]   # (relative path, file text) e.g. SessionStart hook


@dataclass(frozen=True, slots=True)
class LeverResult:
    lever_id: str
    lever_class: str
    params: str                  # canonical Policy spec fragment
    basis: Basis                 # billed basis (LIST/CONTRACT) or LIST_EQUIVALENT
    standalone: Figure           # never summed across levers
    shapley: Figure
    projected_monthly: Figure    # shapley × RR interval, monthly
    needs_eval: bool
    upper_bound: bool
    group: str
    finding_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActionPlan:
    # full-scope joint replay of the selected billed-basis set (window)
    joint_saving: Figure
    # Σ shapley_i × RR_i(p50) over billed-basis levers, p10/p90 range
    headline_monthly: Figure
    # same for LIST_EQUIVALENT cohorts (never added to headline)
    allowance_headroom_monthly: Figure | None
    levers: tuple[LeverResult, ...]
    groups: tuple[tuple[str, tuple[str, ...]], ...]       # group id → lever ids
    method: str                           # "shapley-exact" | "shapley-mc"
    shapley_se: tuple[tuple[str, int], ...]               # lever → SE nano (mc only)
    # e.g. "shapley on 20000/1481203 lanes (seed 7), scaled to …"
    sample: str
    # lever class → (observed mean RR, n receipts)
    observed_rr: tuple[tuple[str, str, int], ...] = ()


# ---------- reconciliation ----------
@dataclass(frozen=True, slots=True)
class ReconRow:
    # channel, date, workspace, model, token_type/bucket, service_tier
    key: tuple[tuple[str, str], ...]
    ledger_tokens: int | None
    provider_tokens: int | None
    ledger_nano: int | None               # our rate card on OUR ledger (exact + estimated points)
    priced_provider_nano: int | None      # our rate card on PROVIDER usage
    invoice_nano: int | None              # provider cost report / CUR / billing export
    rate_card_error_pct: str | None       # (priced_provider − invoice)/invoice
    coverage_pct: str | None              # ledger / invoice
    # "match"|"within_tolerance"|"over"|"under"|"explained" |"unexplained"|"provisional"
    status: str
    residual_code: str | None             # §12.3


@dataclass(frozen=True, slots=True)
class ChannelVerdict:
    channel: str
    verdict: str                          # "reconciled" | "not_reconciled" | "insufficient_data"
    invoice_sources: tuple[str, ...]      # e.g. ("anthropic.cost_report",) or ("aws.cur2",)
    # False when the cost-type/SKU map rows used are VERIFY rows
    mapping_verified: bool


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    window: tuple[str, str]
    tolerance_pct: str
    unexplained_tolerance_pct: str
    rows: tuple[ReconRow, ...]
    token_coverage_pct: str | None
    dollar_coverage_pct: str | None
    rate_card_error: tuple[str, str, str] | None   # p50, p95, max |%| over model-days
    over_count_rows: int
    effective_discount: tuple[tuple[str, str], ...]   # "channel:model:bucket" → 1 − invoice/list
    residuals: tuple[tuple[str, int], ...]             # residual code → nano
    unexplained_nano: int
    channels: tuple[ChannelVerdict, ...]
    # overall: "reconciled" | "not_reconciled" | "insufficient_data"
    verdict: str
    finality: Finality
    suggested_contract: ContractOverlay | None
    rerun_verdict: str | None             # verdict after applying the suggested contract


# ---------- verification ----------
@dataclass(frozen=True, slots=True)
class GuardResult:
    name: str
    passed: bool
    value: str
    threshold: str


@dataclass(frozen=True, slots=True)
class PanelRow:
    cluster_id: str
    date_utc: str
    cost_baseline_nano: int          # repriced at the pre-registered baseline rate card (R8)
    cost_actual_nano: int            # at the actual rate card (rate variance)
    active_dev_days: int
    arm: str | None
    wave: str | None
    treated: bool
    outcome_prs: int | None = None   # team-level merged PRs (quality guardrail), None when absent


@dataclass(frozen=True, slots=True)
class MeasurePlan:
    lever_id: str
    design: str                      # "cluster_rct" | "stepped_wedge" | "its"
    cluster_kind: str
    # wave → clusters (seeded order, never by spend)
    waves: tuple[tuple[int, tuple[str, ...]], ...]
    holdback: tuple[str, ...]
    washout_hours: int
    looks: tuple[str, ...]
    mde_nano: int | None
    projection: Figure | None
    verification_design: bool        # False when MDE > 0.8 × |projection| (or design "its")
    clusters_needed: int | None
    assignment_log_sha256: str
    preregistration_sha256: str
    preregistration_json: str
    otel_tags: tuple[tuple[str, str], ...]           # cluster → OTEL_RESOURCE_ATTRIBUTES value
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MeasurementResult:
    lever_id: str
    design: str                  # "ab" | "cluster_rct" | "stepped_wedge" | "its"
    # "cost per active developer-day" | "cost per task" | "cost per success"
    unit: str
    estimate: Figure             # MEASURED or VERIFIED, with CI range
    projected: Figure | None
    realization_rate: tuple[str, str, str] | None   # point, lo, hi (decimal strings)
    guards: tuple[GuardResult, ...]
    scope: tuple[tuple[str, int], ...]              # clusters, units, treated unit-days
    scope_label: str             # "fleet:<window>" or "lab:<task-set>"
    window: tuple[tuple[str, str], ...]
    rate_card_sha256: str
    assignment_log_sha256: str | None
    preregistration_sha256: str | None
    adjustments: tuple[str, ...]
    rate_variance: Figure | None # EXACT price effect, reported separately (R8)
    signable: bool               # False when ESTIMATED, unreconciled or projection uncalibrated


@dataclass(frozen=True, slots=True)
class AbResult:
    verdict: str                 # "cheaper" | "no-difference" | "costlier"
    scope_label: str             # "lab:<sha256 of task ids>[:12]"
    n_tasks: int
    trials_per_arm: tuple[int, int]
    randomized_order: bool
    cost_per_success: tuple[Figure, Figure]     # baseline, candidate (MEASURED/VERIFIED with CI)
    paired_difference: Figure                   # candidate − baseline per task (CI)
    token_delta_pct: str
    turn_delta_pct: str
    read_delta_pct: str
    success_delta_pct: str
    measurement: MeasurementResult


@dataclass(frozen=True, slots=True)
class ReceiptRow:
    receipt_id: str
    lever_id: str
    lever_class: str
    label: str
    realization_rate: str | None
    created_ms: int
    json: str
    dsse: str | None


# ---------- privacy / aggregates ----------
@dataclass(frozen=True, slots=True)
class AggRow:
    dims: tuple[tuple[str, str | None], ...]
    n_users: int
    n_requests: int
    usage: UsageBuckets
    priced: PricedTotal


@dataclass(frozen=True, slots=True)
class RawAggregate:           # NOT renderable; must pass core.kanon.publish()
    group_by: tuple[str, ...]
    rows: tuple[AggRow, ...]
    window: tuple[int, int]


class _PublishToken:
    """The construction guard of :class:`PublishedAggregate` (a module-private singleton)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<publish token>"


_PUBLISH_TOKEN: object = _PublishToken()


@dataclass(frozen=True, slots=True)
class PublishedAggregate:     # the only aggregate type renderers/exporters accept
    group_by: tuple[str, ...]
    rows: tuple[AggRow, ...]
    window: tuple[int, int]
    k: int
    suppressed_rows: int
    suppressed_users: int
    # must be core.types._PUBLISH_TOKEN (else ContractViolation); only core.kanon.publish and
    # core.testing.published_for_tests pass it
    token: object = field(repr=False)

    def __post_init__(self) -> None:
        if self.token is not _PUBLISH_TOKEN:
            raise ContractViolation("PublishedAggregate: construct it through core.kanon.publish")


# ---------- run-level results (what pipeline returns and outputs/* render) ----------
@dataclass(frozen=True, slots=True)
class LedgerCostRow:
    """Per-bucket ledger cost at export grain; built by the store with exact per-bucket SUMs."""

    date_utc: str
    provider: str
    channel: str
    model: str
    bucket: str                              # a PricedLine bucket name
    team: str | None
    cost_center: str | None
    project: str | None
    workspace_id: str | None
    lane_kind: str
    workload_class: str
    agent_product: str | None
    billing_path: str
    quantity: int                            # tokens (or requests for server tools)
    priced_nano: int                         # Σ exact lines (0 for pure-range buckets)
    estimated_low_nano: int
    estimated_high_nano: int                 # Σ range lines (0 when none)
    basis: Basis                             # LIST | CONTRACT | LIST_EQUIVALENT
    rate_row_id: str | None
    n_users: int


@dataclass(frozen=True, slots=True)
class ClusterDay:
    date_utc: str
    cluster_kind: str
    cluster_id: str
    arm: str | None
    wave: str | None
    active_users: int
    requests: int
    exact_nano: int
    allowance_nano: int


@dataclass(frozen=True, slots=True)
class PrivacyInfo:
    content_tier: ContentTier
    key_id: str | None
    identity_mode: str
    k: int
    suppressed_groups: int


@dataclass(frozen=True, slots=True)
class RateCardInfo:
    sha256: str
    layers: tuple[str, ...]
    stale_rows: tuple[str, ...]
    contract: str | None
    basis: Basis


@dataclass(frozen=True, slots=True)
class BillSummary:
    total: PricedTotal
    # Effective Token Savings Rate, exact ratio string (§14.1)
    esr: str | None
    breakdowns: tuple[tuple[str, PublishedAggregate], ...]   # key = comma-joined dimension list
    # Claude Code naive line-sum ÷ de-duplicated (priced), §5.3
    naive_ratio: str | None = None
    # e.g. trace@1 5m-write footnote, placeholder-output range
    footnotes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckViolation:
    # "TB-CACHE-SHARE" | "TB-NEW-BREAKER" | "TB-COST-REGRESSION" | "TB-SERIALIZATION-CHURN"
    rule_id: str
    level: str         # "error" | "warning"
    message: str       # content-free
    location: str      # "<file name>#run=<run>#call=<index>" (no content)


@dataclass(frozen=True, slots=True)
class CheckResult:
    passed: bool
    violations: tuple[CheckViolation, ...]
    runs: int
    cache_read_share: str | None             # decimal string
    median_cost_nano: int | None
    baseline_median_cost_nano: int | None
    breaker_kinds: tuple[str, ...]
    summary_md: str


@dataclass(frozen=True)
class RunResult:
    command: str
    window: tuple[int, int]
    inputs: tuple[tuple[SourceInfo, int, int], ...]   # (source, records, quarantined)
    privacy: PrivacyInfo
    rate_card: RateCardInfo | None
    bill: BillSummary | None = None
    data_quality: tuple[DataQualityNote, ...] = ()
    reconciliation: ReconciliationReport | None = None
    calibration: CalibrationReport | None = None
    findings: tuple[Finding, ...] = ()
    action_plan: ActionPlan | None = None
    policy_packs: tuple[PolicyPack, ...] = ()
    replays: tuple[ReplayResult, ...] = ()           # `whatif`
    measure_plan: MeasurePlan | None = None
    measurements: tuple[MeasurementResult, ...] = ()
    ab: AbResult | None = None
    check: CheckResult | None = None
    pricing: PricingReport | None = None
    receipts: tuple[str, ...] = ()           # receipt ids
    synthetic: bool = False                  # demo-data banner
    notes: tuple[str, ...] = ()

