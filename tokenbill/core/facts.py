"""Loader of ``core/facts.json``, the single transcription of verified facts (SPEC §3.24, D37).

``load()`` parses the package data once (cached) into typed, immutable accessors; no other module
parses the JSON. Numbers that are money or multipliers are decimal strings in the file and
``Decimal`` here — a JSON float anywhere in the file is a load error. Every entry carries
``source``, ``finding``, ``verified_on`` and ``verification`` (``"primary"`` | ``"research"``).

The top-level ``copilot`` object (CORE-AMENDMENTS C-28) holds every GitHub Copilot fact — rate rows
of channel ``github_copilot``, modifiers, promotions, settings keys and the other Copilot tables —
apart from the top-level sections, which stay unchanged (pinned by wave-0/1 tests). It is read only
through :attr:`Facts.copilot` and the ``copilot_*`` accessors; every entry carries
``verification: "research"`` until the Copilot release gate (ruling R-E19).
"""

from __future__ import annotations

import copy
import functools
import importlib.resources
import json
import types
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tokenbill.core.errors import ContractViolation
from tokenbill.core.money import EXACT_CTX, usd
from tokenbill.core.records import (
    COPILOT_WORKLOADS,
    EDITOR_FAMILIES,
    GITHUB_COST_TYPES,
    LICENSE_PLANS,
)
from tokenbill.core.types import Modifier, RateRow, SourceCitation

__all__ = [
    "FACTS_SCHEMA",
    "META_KEYS",
    "AnnouncedFact",
    "CopilotBandRuleFact",
    "CopilotCreditFact",
    "CopilotEditorFamilyFact",
    "CopilotFacts",
    "CopilotIncludedMinutesFact",
    "CopilotPlanFact",
    "CopilotQuotaFact",
    "CopilotRemapFact",
    "CopilotRetirementFact",
    "CopilotReviewEstimateFact",
    "CopilotRunnerRateFact",
    "CopilotSkuFact",
    "CopilotVsCodeTracesFact",
    "CopilotWorkflowPathFact",
    "CopilotWriteRuleFact",
    "EvidenceFact",
    "Facts",
    "FocusColumn",
    "FocusSpec",
    "HeadlessFieldGroup",
    "PromotionFact",
    "SettingsKeyFact",
    "SkuRuleFact",
    "copilot_dates",
    "copilot_editor_families",
    "copilot_modifiers",
    "copilot_plan_quota_map",
    "copilot_plans",
    "copilot_rates",
    "copilot_remaps",
    "copilot_report_lag_days",
    "copilot_retirements",
    "copilot_runner_rates",
    "copilot_settings_keys",
    "copilot_skus",
    "copilot_vscode_traces",
    "copilot_workflow_paths",
    "load",
    "parse",
]

FACTS_SCHEMA = "tokenbill/facts@1"
META_KEYS = ("source", "finding", "verified_on", "verification")
VERIFICATIONS = frozenset({"primary", "research"})
_RATE_BUCKETS = ("cache_read", "cache_write_5m", "cache_write_1h", "cache_write_other")


@dataclass(frozen=True, slots=True)
class SettingsKeyFact:
    key: str
    target: str
    domain: str
    min_version: str | None
    verified: bool
    lever_id: str | None
    tradeoff: bool
    managed_only: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    name: str
    type: str  # "int" | "decimal" | "decimal_pair" | "formula" | "str"
    value: Decimal | int | str | tuple[Decimal, Decimal]
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class PromotionFact:
    promotion_id: str
    model: str
    channel: str
    start: str
    not_before_end: str
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class AnnouncedFact:
    model: str
    announced_on: str
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class FocusColumn:
    name: str
    feature_level: str  # "Mandatory" | "Conditional" | "Recommended"
    allows_nulls: bool
    data_type: str
    column_type: str
    introduced: str


@dataclass(frozen=True, slots=True)
class FocusSpec:
    version: str
    ratified: str
    dataset: str
    columns: tuple[FocusColumn, ...]
    removed: tuple[tuple[str, tuple[str, ...]], ...]  # removed column → replacements
    custom_column_prefix: str
    service_category_ai: str
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""

    def column(self, name: str) -> FocusColumn:
        """The column named *name* (KeyError when FOCUS 1.4 has no such column)."""
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(name)

    @property
    def mandatory(self) -> tuple[str, ...]:
        """Names of the Mandatory columns, in specification order."""
        return tuple(c.name for c in self.columns if c.feature_level == "Mandatory")


@dataclass(frozen=True, slots=True)
class HeadlessFieldGroup:
    group: str
    fields: Mapping[str, str]
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class SkuRuleFact:
    source_kind: str
    pattern: str  # regular expression over the provider usage type / SKU id
    model: str | None  # None: the model comes from the product, not the usage type
    bucket: str
    endpoint_scope: str | None
    service_tier: str | None
    unit_tokens: int
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


# ---------------------------------------------------------------------------------------------
# GitHub Copilot facts (C-28); every entry also carries the META_KEYS and optional notes
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CopilotCreditFact:
    usd_per_credit: Decimal          # 0.01
    nano_usd_per_credit: int         # 10,000,000
    nano_aiu_per_credit: int         # 10**9 (runtime units)
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotPlanFact:
    plan: str                        # "business" | "enterprise"
    seat_usd_per_month: Decimal      # list price per seat-month
    included_credits: int            # pooled AI credits per seat-month
    promo_credits: int | None        # promotional credits per seat-month for existing customers
    promo_from: str | None           # inclusive
    promo_to: str | None             # exclusive
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotWriteRuleFact:
    model_prefix: str                # "claude-"
    multiplier_of_input: Decimal     # 2: the assumed 1-hour write price = 2 × input (VERIFY)
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotBandRuleFact:
    model: str
    threshold: int                   # input tokens above which the long-context band applies
    measure: str                     # "request_input_tokens" (hypothesis A)
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotSkuFact:
    sku: str
    product: str
    cost_type: str                   # GITHUB_COST_TYPES, rows with a username
    cost_type_unattributed: str      # GITHUB_COST_TYPES, rows without a username
    plan: str | None                 # seat SKUs: "business" | "enterprise"
    workload: str | None             # COPILOT_WORKLOADS
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotQuotaFact:
    quota: int                       # AI usage report total_monthly_quota
    plan: str                        # "business" | "enterprise"
    months: tuple[str, ...]          # "YYYY-MM" months it applies to; () = every month
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotWorkflowPathFact:
    pattern: str
    match: str                       # "exact" | "glob"
    workload: str                    # COPILOT_WORKLOADS
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotRetirementFact:
    model: str
    retire_on: str                   # YYYY-MM-DD
    successor: str | None
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotRemapFact:
    model: str
    target: str                      # same-vendor candidate
    tokenizer_same: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotRunnerRateFact:
    sku: str                         # the runner-pricing spelling (billing rows may add "actions_")
    label: str
    arch: str
    runner_class: str                # "standard" | "larger" | "gpu"
    usd_per_minute: Decimal
    included_minutes_apply: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotIncludedMinutesFact:
    plan: str                        # "ghec"
    minutes_per_month: int
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotReviewEstimateFact:
    effort: str                      # "lite" | "balanced"
    low_usd: Decimal
    high_usd: Decimal
    display_only: bool               # quoted context, never projected (R13)
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotEditorFamilyFact:
    origin: str                      # "seat_editor" | "activity_surface" | "metrics_ide"
    match: str                       # "prefix" (case-insensitive) | "exact"
    pattern: str
    family: str                      # core.records.EDITOR_FAMILIES
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CopilotVsCodeTracesFact:
    extension_id: str
    global_storage_dir: str
    db_file: str
    tmp_fallback_file: str
    enable_setting: str
    outfile_setting: str
    span_columns: tuple[str, ...]
    attribute_allowlist: tuple[str, ...]   # the only span_attributes keys ever selected
    content_keys: tuple[str, ...]          # never read (canary targets in fixtures)
    identity_keys: tuple[str, ...]
    retention_days: int
    retention_sessions: int
    paths: Mapping[str, tuple[str, ...]]   # platform ("darwin" | "linux" | "win32") → templates
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True)
class CopilotFacts:
    """Typed view of the ``copilot`` object of ``facts.json`` (read-only mappings)."""

    credit: CopilotCreditFact
    plans: Mapping[str, CopilotPlanFact]
    rate_rows: tuple[RateRow, ...]
    modifiers: tuple[Modifier, ...]
    write_1h_rule: CopilotWriteRuleFact
    band_rules: Mapping[str, CopilotBandRuleFact]
    promotions: tuple[PromotionFact, ...]
    skus: Mapping[str, CopilotSkuFact]
    plan_quota_map: Mapping[int, CopilotQuotaFact]
    workflow_paths: tuple[CopilotWorkflowPathFact, ...]
    utility_models: frozenset[str]
    model_categories: Mapping[str, str]
    retirements: Mapping[str, CopilotRetirementFact]
    remaps: Mapping[str, CopilotRemapFact]
    runner_rates: Mapping[str, CopilotRunnerRateFact]
    included_minutes: Mapping[str, CopilotIncludedMinutesFact]
    review_estimates: Mapping[str, CopilotReviewEstimateFact]
    dates: Mapping[str, str]
    settings_keys: Mapping[str, SettingsKeyFact]
    editor_families: tuple[CopilotEditorFamilyFact, ...]
    vscode_traces: CopilotVsCodeTracesFact
    cache_ttl_statement: str
    report_lag_days: int
    aic_default_cap_per_run: int


class Facts:
    """Typed, read-only view of ``facts.json``."""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._raw = raw
        self.schema: str = raw["schema"]
        self.as_of: str = raw["as_of"]
        self.rate_rows: tuple[RateRow, ...] = tuple(_rate_row(r) for r in raw["rates"])
        self.modifiers: tuple[Modifier, ...] = tuple(_modifier(m) for m in raw["modifiers"])
        self.settings_keys: Mapping[str, SettingsKeyFact] = types.MappingProxyType(
            {k["key"]: SettingsKeyFact(**k) for k in raw["settings_keys"]}
        )
        self.evidence: Mapping[str, EvidenceFact] = types.MappingProxyType(
            {e["name"]: _evidence(e) for e in raw["evidence"]}
        )
        life = raw["lifecycle"]
        self.successors: Mapping[str, str] = types.MappingProxyType(
            {s["model"]: s["successor"] for s in life["successors"]}
        )
        self.retirements: Mapping[str, str] = types.MappingProxyType(
            {r["model"]: r["retirement_floor"] for r in life["retirements"]}
        )
        self.retired: Mapping[str, str] = types.MappingProxyType(
            {r["model"]: r["retired_on"] for r in life["retired"]}
        )
        self.promotions: tuple[PromotionFact, ...] = tuple(
            PromotionFact(**p) for p in life["promotions"]
        )
        self.announced: tuple[AnnouncedFact, ...] = tuple(
            AnnouncedFact(**a) for a in life["announced"]
        )
        self.focus: FocusSpec = _focus(raw["focus_columns"])
        self.headless_fields: Mapping[str, HeadlessFieldGroup] = types.MappingProxyType(
            {
                g["group"]: HeadlessFieldGroup(
                    **{**g, "fields": types.MappingProxyType(dict(g["fields"]))}
                )
                for g in raw["headless_fields"]
            }
        )
        self.sku_rules: tuple[SkuRuleFact, ...] = tuple(SkuRuleFact(**s) for s in raw["sku_rules"])
        self.copilot: CopilotFacts = _copilot(raw["copilot"])

    def rate_row(self, row_id: str) -> RateRow:
        """The rate row with *row_id* (KeyError when absent)."""
        for row in self.rate_rows:
            if row.row_id == row_id:
                return row
        raise KeyError(row_id)

    def rows_for(self, model: str, channel: str = "anthropic_api") -> tuple[RateRow, ...]:
        """Every row (enabled or not) for ``(channel, model)``, ordered by ``effective_from``."""
        rows = [r for r in self.rate_rows if r.model == model and r.channel == channel]
        return tuple(sorted(rows, key=lambda r: r.effective_from))

    def modifier(self, modifier_id: str) -> Modifier:
        """The modifier with *modifier_id* (KeyError when absent)."""
        for m in self.modifiers:
            if m.modifier_id == modifier_id:
                return m
        raise KeyError(modifier_id)

    def rate_rows_json(self) -> list[dict[str, Any]]:
        """Deep copies of the raw ``tokenbill/rates@1``-shaped rows (for registry parity tests)."""
        return copy.deepcopy(list(self._raw["rates"]))

    def modifiers_json(self) -> list[dict[str, Any]]:
        """Deep copies of the raw modifier objects."""
        return copy.deepcopy(list(self._raw["modifiers"]))

    # ---- GitHub Copilot accessors (C-28); Copilot rows are never reached through rows_for()

    def copilot_rates(self) -> tuple[RateRow, ...]:
        """Every Copilot rate row (channel ``github_copilot``, current and history, §19.2)."""
        return self.copilot.rate_rows

    def copilot_rate_rows_json(self) -> list[dict[str, Any]]:
        """Deep copies of the raw Copilot rate rows (for rate-file parity tests)."""
        return copy.deepcopy(list(self._raw["copilot"]["rates"]))

    def copilot_modifiers(self) -> tuple[Modifier, ...]:
        """The Copilot modifiers (``github.auto``, ``github.compliance``, ``github.fast.*``)."""
        return self.copilot.modifiers

    def copilot_plans(self) -> Mapping[str, CopilotPlanFact]:
        """Plan → seat price, included credits and promotional credits."""
        return self.copilot.plans

    def copilot_dates(self) -> Mapping[str, str]:
        """Named Copilot dates (billing start, promo end, exogenous events) → ``YYYY-MM-DD``."""
        return self.copilot.dates

    def copilot_skus(self) -> Mapping[str, CopilotSkuFact]:
        """SKU → cost type (with / without a username), product, seat plan and workload."""
        return self.copilot.skus

    def copilot_plan_quota_map(self) -> Mapping[int, CopilotQuotaFact]:
        """AI usage report ``total_monthly_quota`` → plan (plan evidence only, VERIFY)."""
        return self.copilot.plan_quota_map

    def copilot_workflow_paths(self) -> tuple[CopilotWorkflowPathFact, ...]:
        """Billing ``workflow_path`` patterns → Copilot workload (exact paths, then globs)."""
        return self.copilot.workflow_paths

    def copilot_remaps(self) -> Mapping[str, CopilotRemapFact]:
        """Model → same-vendor remap candidate (model-policy lever)."""
        return self.copilot.remaps

    def copilot_retirements(self) -> Mapping[str, CopilotRetirementFact]:
        """Model → retirement date and suggested successor."""
        return self.copilot.retirements

    def copilot_runner_rates(self) -> Mapping[str, CopilotRunnerRateFact]:
        """Actions runner SKU (runner-pricing spelling) → per-minute rate and class."""
        return self.copilot.runner_rates

    def copilot_settings_keys(self) -> Mapping[str, SettingsKeyFact]:
        """Copilot settings allowlist (target ``github-copilot``), in file order."""
        return self.copilot.settings_keys

    def copilot_editor_families(self) -> tuple[CopilotEditorFamilyFact, ...]:
        """Editor / surface string patterns → ``core.records.EDITOR_FAMILIES``."""
        return self.copilot.editor_families

    def copilot_vscode_traces(self) -> CopilotVsCodeTracesFact:
        """VS Code ``agent-traces.db`` layout: columns, attribute allowlist, retention, paths."""
        return self.copilot.vscode_traces

    def copilot_report_lag_days(self) -> int:
        """Days the GitHub reports lag (open-month days within the lag are provisional)."""
        return self.copilot.report_lag_days

    def entries(self) -> Iterator[tuple[str, Mapping[str, Any]]]:
        """``(section, raw entry)`` for every fact entry (each carries the META_KEYS); Copilot
        entries are yielded as ``("copilot.<part>", entry)``."""
        raw = self._raw
        for section in (
            "rates",
            "modifiers",
            "settings_keys",
            "evidence",
            "headless_fields",
            "sku_rules",
        ):
            for entry in raw[section]:
                yield section, entry
        for part, items in raw["lifecycle"].items():
            for entry in items:
                yield f"lifecycle.{part}", entry
        yield "focus_columns", raw["focus_columns"]
        for part, value in raw["copilot"].items():
            if isinstance(value, list):
                for entry in value:
                    yield f"copilot.{part}", entry
            else:
                yield f"copilot.{part}", value


def _dec(value: Any, what: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractViolation(f"facts: {what} must be a decimal string")
    return usd(value)


def _dec_pairs(obj: Mapping[str, Any], what: str) -> tuple[tuple[str, Decimal], ...]:
    return tuple(sorted((k, _dec(v, what)) for k, v in obj.items()))


def _citations(items: Any) -> tuple[SourceCitation, ...]:
    return tuple(
        SourceCitation(url=s["url"], retrieved=s["retrieved"], finding=s.get("finding"))
        for s in items
    )


def _rate_row(r: Mapping[str, Any]) -> RateRow:
    mult = r.get("multipliers", {})
    lc = r.get("long_context")
    row = RateRow(
        row_id=r["row_id"],
        provider=r["provider"],
        channel=r["channel"],
        model=r["model"],
        aliases=tuple(r["aliases"]),
        generation=r["generation"],
        effective_from=r["effective_from"],
        effective_to=r["effective_to"],
        input_usd_per_mtok=_dec(r["usd_per_mtok"]["input"], "input"),
        output_usd_per_mtok=_dec(r["usd_per_mtok"]["output"], "output"),
        cache_read_mult=_dec(mult["cache_read"], "multiplier") if "cache_read" in mult else None,
        cache_write_5m_mult=_dec(mult["cache_write_5m"], "multiplier")
        if "cache_write_5m" in mult
        else None,
        cache_write_1h_mult=_dec(mult["cache_write_1h"], "multiplier")
        if "cache_write_1h" in mult
        else None,
        cache_write_other_mult=(
            _dec(mult["cache_write_other"], "multiplier") if "cache_write_other" in mult else None
        ),
        cache_write_other_ttl_s=r.get("cache_write_other_ttl_s"),
        published_absolute=_dec_pairs(r.get("published_absolute", {}), "published_absolute"),
        min_cacheable_tokens=r["min_cacheable_tokens"],
        tokenizer_family=r["tokenizer_family"],
        per_request_usd=_dec_pairs(r.get("per_request_usd", {}), "per_request_usd"),
        long_context_threshold=lc["threshold"] if lc else None,
        long_context_usd_per_mtok=_dec_pairs(lc["usd_per_mtok"], "long_context") if lc else (),
        supports=tuple(r["supports"]),
        enabled=r["enabled"],
        verified_on=r["verified_on"],
        sources=_citations(r["sources"]),
        promotion=r.get("promotion"),
        notes=r.get("notes", ""),
    )
    # Published absolute cache prices must equal input × multiplier exactly (the RATES load rule).
    for bucket, published in row.published_absolute:
        m = {
            "cache_read": row.cache_read_mult,
            "cache_write_5m": row.cache_write_5m_mult,
            "cache_write_1h": row.cache_write_1h_mult,
            "cache_write_other": row.cache_write_other_mult,
        }.get(bucket)
        if m is None or EXACT_CTX.multiply(row.input_usd_per_mtok, m) != published:
            raise ContractViolation(f"facts: {row.row_id} published {bucket} != input x multiplier")
    return row


def _modifier(m: Mapping[str, Any]) -> Modifier:
    return Modifier(
        modifier_id=m["modifier_id"],
        kind=m["kind"],
        factor=_dec(m["factor"], "factor") if m.get("factor") is not None else None,
        base_usd_per_mtok=_dec_pairs(m.get("base_usd_per_mtok", {}), "base_usd_per_mtok"),
        applies_to=tuple(m["applies_to"]),
        when=tuple(sorted((k, str(v)) for k, v in m["when"].items())),
        stacking=m["stacking"],
        sources=_citations(m["sources"]),
    )


def _evidence(e: Mapping[str, Any]) -> EvidenceFact:
    typ, raw = e["type"], e["value"]
    value: Decimal | int | str | tuple[Decimal, Decimal]
    if typ == "int":
        if type(raw) is not int:
            raise ContractViolation(f"facts: evidence {e['name']} must be an int")
        value = raw
    elif typ == "decimal":
        value = _dec(raw, e["name"])
    elif typ == "decimal_pair":
        if not isinstance(raw, list) or len(raw) != 2:
            raise ContractViolation(f"facts: evidence {e['name']} must be a pair")
        value = (_dec(raw[0], e["name"]), _dec(raw[1], e["name"]))
    elif typ in ("formula", "str"):
        if not isinstance(raw, str):
            raise ContractViolation(f"facts: evidence {e['name']} must be a string")
        value = raw
    else:
        raise ContractViolation(f"facts: evidence {e['name']} has unknown type")
    return EvidenceFact(
        name=e["name"],
        type=typ,
        value=value,
        source=e["source"],
        finding=e["finding"],
        verified_on=e["verified_on"],
        verification=e["verification"],
        notes=e.get("notes", ""),
    )


def _focus(f: Mapping[str, Any]) -> FocusSpec:
    return FocusSpec(
        version=f["version"],
        ratified=f["ratified"],
        dataset=f["dataset"],
        columns=tuple(FocusColumn(**c) for c in f["columns"]),
        removed=tuple((r["name"], tuple(r["replaced_by"])) for r in f["removed_in_1_4"]),
        custom_column_prefix=f["custom_column_prefix"],
        service_category_ai=f["service_category_ai"],
        source=f["source"],
        finding=f["finding"],
        verified_on=f["verified_on"],
        verification=f["verification"],
        notes=f.get("notes", ""),
    )


_COPILOT_PARTS = (
    "credit", "plans", "rates", "modifiers", "write_1h_rule", "band_rules", "promotions", "skus",
    "plan_quota_map", "workflow_paths", "utility_models", "model_categories", "retirements",
    "remaps", "runner_rates", "included_minutes", "review_estimates", "dates", "settings_keys",
    "editor_families", "vscode_traces", "cache_ttl_statement", "report_lag_days",
    "aic_default_cap_per_run",
)
_META_FIELDS = ("source", "finding", "verified_on", "verification")


def _meta(e: Mapping[str, Any]) -> dict[str, Any]:
    return {k: e[k] for k in _META_FIELDS} | {"notes": e.get("notes", "")}


def _check(ok: bool, what: str) -> None:
    if not ok:
        raise ContractViolation(f"facts: copilot {what}")


def _int_value(value: Any, what: str) -> int:
    _check(type(value) is int, f"{what} must be an int")
    return value


def _date_str(value: Any, what: str) -> str:
    _check(isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-",
           f"{what} must be a YYYY-MM-DD string")
    return value


def _unique(items: list[Any], key: str, what: str) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for item in items:
        k = getattr(item, key)
        _check(k not in out, f"{what}: duplicate {key}")
        out[k] = item
    return out


def _copilot(c: Mapping[str, Any]) -> CopilotFacts:
    """Parse and validate the ``copilot`` object (every Copilot fact; C-28)."""
    _check(isinstance(c, Mapping) and set(c) == set(_COPILOT_PARTS), "section has the wrong parts")
    cr = c["credit"]
    credit = CopilotCreditFact(usd_per_credit=_dec(cr["usd_per_credit"], "usd_per_credit"),
                               nano_usd_per_credit=_int_value(cr["nano_usd_per_credit"], "credit"),
                               nano_aiu_per_credit=_int_value(cr["nano_aiu_per_credit"], "credit"),
                               **_meta(cr))
    plans = [CopilotPlanFact(plan=p["plan"],
                             seat_usd_per_month=_dec(p["seat_usd_per_month"], "seat price"),
                             included_credits=_int_value(p["included_credits"], "credits"),
                             promo_credits=p["promo_credits"], promo_from=p["promo_from"],
                             promo_to=p["promo_to"], **_meta(p)) for p in c["plans"]]
    for plan in plans:
        _check(plan.plan in ("business", "enterprise"), "plans: unknown plan")
    rows = tuple(_rate_row(r) for r in c["rates"])
    _check(all(r.provider == "github" and r.channel == "github_copilot" for r in rows),
           "rates must be provider github, channel github_copilot")
    _unique(list(rows), "row_id", "rates")
    modifiers = tuple(_modifier(m) for m in c["modifiers"])
    _check(all(dict(m.when).get("channel_in") == "github_copilot" for m in modifiers),
           "modifiers must be limited to channel github_copilot")
    promotions = tuple(PromotionFact(**p) for p in c["promotions"])
    promo_ids = {p.promotion_id for p in promotions}
    _check(all(r.promotion is None or r.promotion in promo_ids for r in rows),
           "a rate row names an unknown promotion")
    wr = c["write_1h_rule"]
    write_rule = CopilotWriteRuleFact(model_prefix=wr["model_prefix"],
                                      multiplier_of_input=_dec(wr["multiplier_of_input"], "rule"),
                                      verified=bool(wr["verified"]), **_meta(wr))
    bands = [CopilotBandRuleFact(model=b["model"], threshold=_int_value(b["threshold"], "band"),
                                 measure=b["measure"], verified=bool(b["verified"]), **_meta(b))
             for b in c["band_rules"]]
    skus = [CopilotSkuFact(sku=k["sku"], product=k["product"], cost_type=k["cost_type"],
                           cost_type_unattributed=k["cost_type_unattributed"], plan=k["plan"],
                           workload=k["workload"], verified=bool(k["verified"]), **_meta(k))
            for k in c["skus"]]
    for k in skus:
        _check(k.cost_type in GITHUB_COST_TYPES and k.cost_type_unattributed in GITHUB_COST_TYPES,
               "skus: cost type not in GITHUB_COST_TYPES")
        _check(k.plan is None or k.plan in LICENSE_PLANS[:2], "skus: unknown plan")
        _check(k.workload is None or k.workload in COPILOT_WORKLOADS, "skus: unknown workload")
    quotas = [CopilotQuotaFact(quota=_int_value(q["quota"], "quota"), plan=q["plan"],
                               months=tuple(q["months"]), verified=bool(q["verified"]), **_meta(q))
              for q in c["plan_quota_map"]]
    _check(all(q.plan in LICENSE_PLANS[:2] for q in quotas), "plan_quota_map: unknown plan")
    paths = tuple(CopilotWorkflowPathFact(pattern=w["pattern"], match=w["match"],
                                          workload=w["workload"], verified=bool(w["verified"]),
                                          **_meta(w)) for w in c["workflow_paths"])
    _check(all(w.match in ("exact", "glob") and w.workload in COPILOT_WORKLOADS for w in paths),
           "workflow_paths: bad match or workload")
    retirements = [CopilotRetirementFact(model=r["model"],
                                         retire_on=_date_str(r["retire_on"], "retire_on"),
                                         successor=r["successor"], **_meta(r))
                   for r in c["retirements"]]
    remaps = [CopilotRemapFact(model=r["model"], target=r["target"],
                               tokenizer_same=bool(r["tokenizer_same"]), **_meta(r))
              for r in c["remaps"]]
    runners = [CopilotRunnerRateFact(sku=r["sku"], label=r["label"], arch=r["arch"],
                                     runner_class=r["runner_class"],
                                     usd_per_minute=_dec(r["usd_per_minute"], "runner rate"),
                                     included_minutes_apply=bool(r["included_minutes_apply"]),
                                     **_meta(r)) for r in c["runner_rates"]]
    included = [CopilotIncludedMinutesFact(plan=m["plan"],
                                           minutes_per_month=_int_value(m["minutes_per_month"],
                                                                        "minutes"), **_meta(m))
                for m in c["included_minutes"]]
    reviews = [CopilotReviewEstimateFact(effort=r["effort"], low_usd=_dec(r["low_usd"], "review"),
                                         high_usd=_dec(r["high_usd"], "review"),
                                         display_only=bool(r["display_only"]), **_meta(r))
               for r in c["review_estimates"]]
    dates = {d["name"]: _date_str(d["date"], "dates") for d in c["dates"]}
    _check(len(dates) == len(c["dates"]), "dates: duplicate name")
    keys = [SettingsKeyFact(**k) for k in c["settings_keys"]]
    _check(all(k.target == "github-copilot" for k in keys),
           "settings_keys: target must be github-copilot")
    families = tuple(CopilotEditorFamilyFact(origin=e["origin"], match=e["match"],
                                             pattern=e["pattern"], family=e["family"],
                                             verified=bool(e["verified"]), **_meta(e))
                     for e in c["editor_families"])
    _check(all(e.family in EDITOR_FAMILIES and e.match in ("prefix", "exact") for e in families),
           "editor_families: bad family or match")
    v = c["vscode_traces"]
    traces = CopilotVsCodeTracesFact(
        extension_id=v["extension_id"], global_storage_dir=v["global_storage_dir"],
        db_file=v["db_file"], tmp_fallback_file=v["tmp_fallback_file"],
        enable_setting=v["enable_setting"], outfile_setting=v["outfile_setting"],
        span_columns=tuple(v["span_columns"]), attribute_allowlist=tuple(v["attribute_allowlist"]),
        content_keys=tuple(v["content_keys"]), identity_keys=tuple(v["identity_keys"]),
        retention_days=_int_value(v["retention_days"], "retention"),
        retention_sessions=_int_value(v["retention_sessions"], "retention"),
        paths=types.MappingProxyType({k: tuple(p) for k, p in v["paths"].items()}),
        verified=bool(v["verified"]), **_meta(v))
    _check(not set(traces.attribute_allowlist) & set(traces.content_keys),
           "vscode_traces: a content key is allowlisted")
    ttl, lag, cap = c["cache_ttl_statement"], c["report_lag_days"], c["aic_default_cap_per_run"]
    _check(isinstance(ttl["text"], str), "cache_ttl_statement: text must be a str")
    proxy = types.MappingProxyType
    return CopilotFacts(
        credit=credit,
        plans=proxy(_unique(plans, "plan", "plans")),
        rate_rows=rows,
        modifiers=modifiers,
        write_1h_rule=write_rule,
        band_rules=proxy(_unique(bands, "model", "band_rules")),
        promotions=promotions,
        skus=proxy(_unique(skus, "sku", "skus")),
        plan_quota_map=proxy(_unique(quotas, "quota", "plan_quota_map")),
        workflow_paths=paths,
        utility_models=frozenset(u["model"] for u in c["utility_models"]),
        model_categories=proxy({m["model"]: m["category"] for m in c["model_categories"]}),
        retirements=proxy(_unique(retirements, "model", "retirements")),
        remaps=proxy(_unique(remaps, "model", "remaps")),
        runner_rates=proxy(_unique(runners, "sku", "runner_rates")),
        included_minutes=proxy(_unique(included, "plan", "included_minutes")),
        review_estimates=proxy(_unique(reviews, "effort", "review_estimates")),
        dates=proxy(dates),
        settings_keys=proxy(_unique(keys, "key", "settings_keys")),
        editor_families=families,
        vscode_traces=traces,
        cache_ttl_statement=ttl["text"],
        report_lag_days=_int_value(lag["value"], "report_lag_days"),
        aic_default_cap_per_run=_int_value(cap["value"], "aic_default_cap_per_run"),
    )


def _reject_float(token: str) -> Any:
    raise ContractViolation("facts: JSON floats are not allowed (use decimal strings)")


def parse(text: str) -> Facts:
    """Parse and validate a facts document (the text of ``facts.json``)."""
    try:
        raw = json.loads(text, parse_float=_reject_float, parse_constant=_reject_float)
    except ValueError as exc:
        raise ContractViolation(f"facts: invalid JSON ({type(exc).__name__})") from None
    if not isinstance(raw, dict) or raw.get("schema") != FACTS_SCHEMA:
        raise ContractViolation(f"facts: schema must be {FACTS_SCHEMA}")
    try:
        facts = Facts(raw)
        entries = list(facts.entries())
        for section, entry in entries:
            for key in META_KEYS:
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ContractViolation(f"facts: an entry of {section} lacks {key}")
            if entry["verification"] not in VERIFICATIONS:
                raise ContractViolation(f"facts: an entry of {section} has an unknown verification")
    except (KeyError, TypeError, AttributeError, IndexError, ValueError, ArithmeticError) as exc:
        raise ContractViolation(f"facts: malformed document ({type(exc).__name__})") from None
    return facts


@functools.lru_cache(maxsize=1)
def load() -> Facts:
    """The packaged ``tokenbill/core/facts.json`` (parsed once per process)."""
    text = (
        importlib.resources.files("tokenbill.core")
        .joinpath("facts.json")
        .read_text(encoding="utf-8")
    )
    return parse(text)


# ---- module-level Copilot accessors (the packaged facts; C-28) ---------------------------------


def copilot_rates() -> tuple[RateRow, ...]:
    """``load().copilot_rates()``: every Copilot rate row."""
    return load().copilot_rates()


def copilot_modifiers() -> tuple[Modifier, ...]:
    """``load().copilot_modifiers()``."""
    return load().copilot_modifiers()


def copilot_plans() -> Mapping[str, CopilotPlanFact]:
    """``load().copilot_plans()``."""
    return load().copilot_plans()


def copilot_dates() -> Mapping[str, str]:
    """``load().copilot_dates()``."""
    return load().copilot_dates()


def copilot_skus() -> Mapping[str, CopilotSkuFact]:
    """``load().copilot_skus()``."""
    return load().copilot_skus()


def copilot_plan_quota_map() -> Mapping[int, CopilotQuotaFact]:
    """``load().copilot_plan_quota_map()``."""
    return load().copilot_plan_quota_map()


def copilot_workflow_paths() -> tuple[CopilotWorkflowPathFact, ...]:
    """``load().copilot_workflow_paths()``."""
    return load().copilot_workflow_paths()


def copilot_remaps() -> Mapping[str, CopilotRemapFact]:
    """``load().copilot_remaps()``."""
    return load().copilot_remaps()


def copilot_retirements() -> Mapping[str, CopilotRetirementFact]:
    """``load().copilot_retirements()``."""
    return load().copilot_retirements()


def copilot_runner_rates() -> Mapping[str, CopilotRunnerRateFact]:
    """``load().copilot_runner_rates()``."""
    return load().copilot_runner_rates()


def copilot_settings_keys() -> Mapping[str, SettingsKeyFact]:
    """``load().copilot_settings_keys()``."""
    return load().copilot_settings_keys()


def copilot_editor_families() -> tuple[CopilotEditorFamilyFact, ...]:
    """``load().copilot_editor_families()``."""
    return load().copilot_editor_families()


def copilot_vscode_traces() -> CopilotVsCodeTracesFact:
    """``load().copilot_vscode_traces()``."""
    return load().copilot_vscode_traces()


def copilot_report_lag_days() -> int:
    """``load().copilot_report_lag_days()``."""
    return load().copilot_report_lag_days()
