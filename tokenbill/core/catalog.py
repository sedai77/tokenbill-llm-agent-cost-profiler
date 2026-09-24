"""Lever catalog, settings allowlist, model lifecycle and SKU rules (SPEC §3.20, §11.1, §11.4,
§6.7).

Data only (D29): no wave-2 package depends on another wave-2 package's module for these tables.

* :data:`LEVERS` is the SPEC §11.1 table. Every ``grid`` entry is a complete policy spec (SPEC §9.5
  grammar) that ``core.policy.parse_policy`` accepts; clauses that take a selector carry the lever's
  selector explicitly (the table's ``@…``). Clauses without a selector in
  :class:`~tokenbill.core.types.Policy` (``compact-window``, ``cold-resume``, ``fast``, ``geo``,
  ``regional``, ``batch``, ``repair``, ``breakpoints``) carry none.
* :data:`ALLOWLIST` is the SPEC §11.4 table, built from ``core/facts.json`` ``settings_keys`` (the
  verified flags, minimum versions and domains there override the SPEC prose, Appendix E).
* Lifecycle (:data:`SUCCESSORS`, :data:`RETIREMENTS`, :data:`PROMOTIONS`,
  :data:`ANNOUNCED_UNPRICED`) and :data:`SKU_RULES` come from ``core/facts.json``; successors exist
  only for same-tier, same-tokenizer pairs (SPEC §6.7).

GitHub Copilot (CORE-AMENDMENTS K-1 … K-3; addendum §10.4, §11.1, §11.3, §11.4) lives in
**separate** tables so the SPEC tables above stay exactly as pinned by the wave-1 tests:

* :data:`COPILOT_LEVERS` (addendum §11.1; aggregate levers have ``replay="aggregate"`` and
  ``grid=()``, their candidates are in :data:`AGGREGATE_GRIDS` in the aggregate grammar
  ``copilot:<param>=<value>[@<scope>]`` — :func:`parse_aggregate_spec` / :func:`to_aggregate_spec`);
  :func:`lever` searches :data:`LEVERS` then :data:`COPILOT_LEVERS`; :func:`levers_for_kind` takes
  ``family="copilot"``.
* :data:`COPILOT_ALLOWLIST` (from ``facts.copilot.settings_keys``, target ``github-copilot``) and
  :data:`ADMIN_ACTIONS` (checklist items and REST request templates, never executed).
* Copilot data accessors over ``core.facts`` (promotions, retirements, allowances, SKUs, workflow
  paths, categories, remaps, runner rates, editor families), :func:`fix_for` (Copilot fix texts),
  :data:`FAMILY_EXCLUSIONS` (generic detectors that do not run on Copilot lanes) and
  :data:`COUNT_SOURCE` (what ``core.kanon.scope_counter`` counts people over, ruling R-E16).
"""

from __future__ import annotations

import datetime as _dt
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from tokenbill.core.errors import UsageError
from tokenbill.core.facts import CopilotRunnerRateFact
from tokenbill.core.facts import load as _load_facts
from tokenbill.core.records import (
    ACTIVITY_FLAGS,
    ACTIVITY_KEYS,
    CONFIG_KEYS,
    EDITOR_FAMILIES,
    GITHUB_COST_TYPES,
)
from tokenbill.core.types import ADMIN_ACTION_WHERE, Fix

__all__ = [
    "ACTIVITY_FLAGS",
    "ACTIVITY_KEYS",
    "ADMIN_ACTIONS",
    "AGGREGATE_GRIDS",
    "AGGREGATE_PARAMS",
    "AGGREGATE_SCOPES",
    "ALLOWLIST",
    "ANNOUNCED_UNPRICED",
    "CONFIG_KEYS",
    "COPILOT_ALLOWLIST",
    "COPILOT_LEVERS",
    "COPILOT_PROMOTIONS",
    "COPILOT_RETIREMENTS",
    "COUNT_SOURCE",
    "COUNT_SOURCES",
    "EDITOR_FAMILIES",
    "FAMILIES",
    "FAMILY_EXCLUSIONS",
    "LEVERS",
    "LEVER_CLASSES",
    "PROMOTIONS",
    "RETIREMENTS",
    "RR_PRIORS",
    "SKU_RULES",
    "SUCCESSORS",
    "AdminActionDef",
    "AggregateSpec",
    "AllowedKey",
    "LeverDef",
    "Promotion",
    "SkuRule",
    "agent_family",
    "allowed",
    "copilot_allowance",
    "copilot_allowed",
    "copilot_category",
    "copilot_cost_type",
    "copilot_promotion_for",
    "copilot_remap",
    "copilot_seat_plan",
    "copilot_workload",
    "count_source",
    "editor_family",
    "fix_for",
    "lever",
    "levers_for_kind",
    "map_sku",
    "parse_aggregate_spec",
    "promotion_for",
    "retiring_within",
    "runner_rate",
    "successor",
    "to_aggregate_spec",
]

#: Lever classes (SPEC §11.2 realization priors).
LEVER_CLASSES = ("rate", "cache_transform", "trajectory", "behavioral")
#: ``LeverDef.replay`` values; ``"aggregate"`` only in :data:`COPILOT_LEVERS` (K-1).
_REPLAY_KINDS = ("usage", "block", "none", "aggregate")


# ---------------------------------------------------------------------------------------------
# lever catalog (SPEC §11.1)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeverDef:
    """One lever of the catalog (SPEC §3.20)."""

    lever_id: str
    lever_class: str                         # rate | cache_transform | trajectory | behavioral
    grid: tuple[str, ...]                    # Policy spec fragments, one per candidate value (§9.5)
    selector: str                            # lanes the lever touches (core.policy.lane_matches)
    replay: str                              # "usage" | "block" | "none" | "aggregate" (Copilot)
    needs_eval: bool
    upper_bound: bool
    tradeoff: bool
    # ALLOWLIST keys (delivery through managed settings); Copilot levers: COPILOT_ALLOWLIST keys or
    # ADMIN_ACTIONS ids
    patch_keys: tuple[str, ...]
    finding_kinds: tuple[str, ...]           # detector kinds that link to it


_CC_MAIN = "agent_product:claude_code,lane_kind:main"
_CC_SUB = "agent_product:claude_code,lane_kind:subagent"
_CC_WF = "agent_product:claude_code,lane_kind:workflow_agent"
_TTL_KINDS = ("ttl-expiry", "ttl-1h-recommended", "ttl-5m-recommended", "ttl-heterogeneous",
              "oversized-ttl")


def _ttl_grid(*selectors: str) -> tuple[str, ...]:
    return tuple(";".join(f"ttl={ttl}@{sel}" for sel in selectors) for ttl in ("5m", "1h"))


def _model_grid(models: tuple[str, ...], *selectors: str) -> tuple[str, ...]:
    return tuple(";".join(f"model={m}@{sel}" for sel in selectors) for m in models)


def _successor_grid() -> tuple[str, ...]:
    pairs = sorted(_load_facts().successors.items())
    if not pairs:  # pragma: no cover - facts.json always carries successors
        return ()
    return (";".join(f"model={new}@model:{old}" for old, new in pairs),)


LEVERS: tuple[LeverDef, ...] = (
    LeverDef("cc.prompt_cache_ttl.main", "cache_transform", _ttl_grid(_CC_MAIN), _CC_MAIN,
             "usage", False, False, False, ("promptCacheTtl",), _TTL_KINDS + ("cold-resume",)),
    LeverDef("cc.prompt_cache_ttl.subagent", "cache_transform", _ttl_grid(_CC_SUB, _CC_WF),
             _CC_SUB, "usage", False, False, False, ("subagentPromptCacheTtl",), _TTL_KINDS),
    LeverDef("sdk.ttl", "cache_transform", _ttl_grid("lane_kind:api_run"), "lane_kind:api_run",
             "usage", False, False, False, (), _TTL_KINDS + ("scheduled-cadence",)),
    LeverDef("sdk.keepalive", "cache_transform",
             ("keepalive=240s,max=3600s@agent_product:agent_sdk",), "agent_product:agent_sdk",
             "usage", False, False, False, (),
             ("keepalive-recommended", "scheduled-cadence")),
    LeverDef("cc.autocompact_window", "trajectory",
             tuple(f"compact-window={w}" for w in (200000, 300000, 400000, 500000, 700000)),
             _CC_MAIN, "usage", True, True, True,
             ("autoCompactWindow", "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
             ("compaction-window", "context-tax")),
    LeverDef("cc.compact_on_resume", "trajectory",
             ("cold-resume=compact,min=200000", "cold-resume=clear,min=200000"), _CC_MAIN,
             "usage", True, True, True, ("hooks.SessionStart",), ("cold-resume",)),
    LeverDef("cc.cold_resume_hook", "behavioral", (), _CC_MAIN, "none", False, False, False,
             ("hooks.SessionStart",), ("cold-resume", "compaction-cold")),
    LeverDef("cc.default_model", "trajectory", _model_grid(("claude-sonnet-5",), _CC_MAIN),
             _CC_MAIN, "usage", True, True, True,
             ("model", "availableModels", "enforceAvailableModels"), ("default-model",)),
    LeverDef("cc.default_effort", "trajectory",
             (f"effort=medium,scale=0.5@{_CC_MAIN}", f"effort=low,scale=0.25@{_CC_MAIN}"),
             _CC_MAIN, "usage", True, True, True, ("effortLevel", "maxEffortLevel"),
             ("default-effort",)),
    LeverDef("cc.subagent_model", "trajectory",
             _model_grid(("claude-sonnet-5", "claude-haiku-4-5"), "lane_kind:subagent",
                         "lane_kind:workflow_agent"),
             "lane_kind:subagent", "usage", True, True, True,
             ("env.CLAUDE_CODE_SUBAGENT_MODEL",), ("delegation-routing",)),
    LeverDef("model.same_tier_upgrade", "trajectory", _successor_grid(), "all", "usage", True,
             True, True,
             ("env.ANTHROPIC_DEFAULT_OPUS_MODEL", "env.ANTHROPIC_DEFAULT_SONNET_MODEL",
              "env.ANTHROPIC_DEFAULT_HAIKU_MODEL"), ("same-tier-upgrade",)),
    LeverDef("cc.max_effort", "trajectory", ("effort=high", "effort=medium"), "all", "usage",
             True, True, True, ("maxEffortLevel",), ("effort-mix",)),
    LeverDef("cc.fast_mode_opt_in", "rate", ("fast=off",), "all", "usage", False, False, False,
             ("fastModePerSessionOptIn",), ("fast-premium", "fast-toggle", "param-change")),
    LeverDef("geo.global", "rate", ("geo=global",), "all", "usage", False, False, False, (),
             ("geo-premium",)),
    LeverDef("endpoint.global", "rate", ("regional=global",), "all", "usage", False, False, False,
             (), ("regional-premium",)),
    LeverDef("batch.eligible", "rate", ("batch=eligible",), "all", "usage", False, False, False,
             (), ("batch-eligible", "batch-share")),
    LeverDef("fanout.stagger", "cache_transform", ("repair=stagger_fanout",), "all", "usage",
             False, True, False, (), ("cold-fanout", "fanout")),
    LeverDef("retry.single_owner", "cache_transform", ("repair=retry_backoff_cap",), "all",
             "usage", False, False, False, (), ("cold-retry", "retry-storm")),
    LeverDef("fallback.credit", "cache_transform", ("repair=fallback_credit",), "all", "usage",
             False, False, False, (), ("refusal-fallback-no-credit", "model-switch")),
    LeverDef("gateway.restore_caching", "cache_transform", ("repair=restore_caching",), "all",
             "usage", False, False, False, (), ("no-cache", "beta-header-dropped")),
    LeverDef("ci.shared_prefix", "cache_transform", ("repair=shared_ci_prefix",), "workload:ci",
             "usage", False, True, False, (), ("ci-cross-run",)),
    LeverDef("blocks.breakpoints", "cache_transform",
             ("breakpoints=static_plus_end", "breakpoints=every_15"), "all", "block", False, False,
             False, (),
             ("breakpoint-placement", "missing-breakpoint", "lookback-overflow",
              "write-never-read")),
    # tool-defs-bloat projections are upper bounds (SPEC §10.2: band × tokens, `upper_bound`)
    LeverDef("cc.tool_search", "cache_transform", (), "agent_product:claude_code", "none", False,
             True, False, ("env.ENABLE_TOOL_SEARCH",),
             ("tool-defs-bloat", "static-prefix", "tool-search-disabled")),
    LeverDef("sdk.defer_loading", "cache_transform", (), "all", "none", False, True, False, (),
             ("tool-defs-bloat",)),
)

_LEVERS_BY_ID: Mapping[str, LeverDef] = types.MappingProxyType({lv.lever_id: lv for lv in LEVERS})


def lever(lever_id: str) -> LeverDef:
    """The catalog entry for *lever_id*, searching :data:`LEVERS` then :data:`COPILOT_LEVERS`
    (unknown id → ``UsageError``)."""
    try:
        return _LEVERS_BY_ID[lever_id]
    except KeyError:
        pass
    try:
        return _COPILOT_LEVERS_BY_ID[lever_id]
    except KeyError:
        raise UsageError(f"unknown lever {lever_id!r}") from None


#: Product families of lanes and findings (``core.findings.product_family``).
FAMILIES = ("default", "copilot")


def levers_for_kind(kind: str, *, family: str = "default") -> tuple[LeverDef, ...]:
    """Levers a finding of *kind* links to, in catalog order (empty when none): from
    :data:`LEVERS` for family ``"default"`` (unchanged), from :data:`COPILOT_LEVERS` for
    ``"copilot"``; another family → ``UsageError``."""
    if family == "default":
        table = LEVERS
    elif family == "copilot":
        table = COPILOT_LEVERS
    else:
        raise UsageError(f"unknown product family {family!r}")
    return tuple(lv for lv in table if kind in lv.finding_kinds)


# ---------------------------------------------------------------------------------------------
# settings allowlist (SPEC §11.4; verified flags from facts.json)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllowedKey:
    """A managed-settings key Token Bill may emit (SPEC §3.20)."""

    key: str
    target: str
    domain: str
    min_version: str | None
    verified: bool
    lever_id: str | None
    tradeoff: bool
    source: str


def _allowlist(facts_keys: Mapping | None = None) -> Mapping[str, AllowedKey]:
    out: dict[str, AllowedKey] = {}
    keys = _load_facts().settings_keys if facts_keys is None else facts_keys
    for key, fact in keys.items():
        out[key] = AllowedKey(
            key=key,
            target=fact.target,
            domain=fact.domain,
            min_version=fact.min_version,
            verified=fact.verified,
            lever_id=fact.lever_id,
            tradeoff=fact.tradeoff,
            source=fact.source,
        )
    return types.MappingProxyType(out)


ALLOWLIST: Mapping[str, AllowedKey] = _allowlist()


def allowed(key: str) -> AllowedKey:
    """The allowlist entry for *key*, searching :data:`ALLOWLIST` then :data:`COPILOT_ALLOWLIST`
    (a key outside both → ``UsageError``)."""
    try:
        return ALLOWLIST[key]
    except (KeyError, TypeError):
        pass
    try:
        return COPILOT_ALLOWLIST[key]
    except (KeyError, TypeError):
        raise UsageError(f"settings key {key!r} is not in the allowlist") from None


# ---------------------------------------------------------------------------------------------
# lifecycle (SPEC §6.7)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Promotion:
    """A promotional price window: valid from ``start`` at least through ``not_before_end``."""

    promotion_id: str
    model: str
    channel: str
    start: str
    not_before_end: str
    source: str


def _lifecycle() -> tuple[Mapping[str, str], Mapping[str, str], tuple[Promotion, ...],
                          tuple[str, ...]]:
    facts = _load_facts()
    successors = types.MappingProxyType(dict(sorted(facts.successors.items())))
    # Retired models (a hard retirement date) and retirement floors ("not sooner than").
    retire: dict[str, str] = dict(facts.retired)
    retire.update(facts.retirements)
    retirements = types.MappingProxyType(dict(sorted(retire.items())))
    promotions = tuple(
        Promotion(
            promotion_id=p.promotion_id,
            model=p.model,
            channel=p.channel,
            start=p.start,
            not_before_end=p.not_before_end,
            source=p.source,
        )
        for p in sorted(facts.promotions, key=lambda p: p.promotion_id)
    )
    announced = tuple(sorted(a.model for a in facts.announced))
    return successors, retirements, promotions, announced


SUCCESSORS: Mapping[str, str]
RETIREMENTS: Mapping[str, str]
PROMOTIONS: tuple[Promotion, ...]
ANNOUNCED_UNPRICED: tuple[str, ...]
SUCCESSORS, RETIREMENTS, PROMOTIONS, ANNOUNCED_UNPRICED = _lifecycle()


def successor(model: str) -> str | None:
    """The documented same-tier, same-tokenizer successor of *model*, else None."""
    return SUCCESSORS.get(model)


_EPOCH = _dt.date(1970, 1, 1)
_DAY_MS = 86_400_000


def retiring_within(model: str, ts_ms: int, days: int) -> str | None:
    """The retirement (floor) date of *model* when it falls on or before the UTC date of *ts_ms*
    plus *days* days — including dates already past — else None. Day arithmetic is on integers,
    so any ``ts_ms`` / ``days`` (however large) is answered without an overflow."""
    floor = RETIREMENTS.get(model)
    if floor is None:
        return None
    floor_day = (_dt.date.fromisoformat(floor) - _EPOCH).days
    return floor if floor_day <= ts_ms // _DAY_MS + days else None


def promotion_for(model: str, channel: str, date: str) -> Promotion | None:
    """The promotion covering *model* on *channel* on *date* (``start ≤ date ≤ not_before_end``),
    searching :data:`PROMOTIONS` then :data:`COPILOT_PROMOTIONS`."""
    for promo in (*PROMOTIONS, *COPILOT_PROMOTIONS):
        if promo.model == model and promo.channel == channel and (
            promo.start <= date <= promo.not_before_end
        ):
            return promo
    return None


# ---------------------------------------------------------------------------------------------
# CUR usage-type / GCP SKU rules (SPEC §5.13, §12; from facts.json)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkuRule:
    """CUR usage type / GCP SKU → canonical dims. ``pattern`` is a regular expression matched
    against the whole provider code; ``unit_tokens`` is the number of tokens per usage unit."""

    source_kind: str
    pattern: str
    model: str | None
    bucket: str
    endpoint_scope: str | None
    service_tier: str | None
    unit_tokens: int
    verified: bool
    source: str


SKU_RULES: tuple[SkuRule, ...] = tuple(
    SkuRule(
        source_kind=s.source_kind,
        pattern=s.pattern,
        model=s.model,
        bucket=s.bucket,
        endpoint_scope=s.endpoint_scope,
        service_tier=s.service_tier,
        unit_tokens=s.unit_tokens,
        verified=s.verified,
        source=s.source,
    )
    for s in _load_facts().sku_rules
)
_SKU_PATTERNS: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str]:
    compiled = _SKU_PATTERNS.get(pattern)
    if compiled is None:
        compiled = _SKU_PATTERNS[pattern] = re.compile(pattern)
    return compiled


def _first_verified(rules: tuple[SkuRule, ...], source_kind: str, sku: str) -> SkuRule | None:
    for rule in rules:
        if rule.verified and rule.source_kind == source_kind and _compiled(rule.pattern).fullmatch(
            sku
        ):
            return rule
    return None


def map_sku(source_kind: str, sku: str) -> SkuRule | None:
    """The first VERIFIED rule of :data:`SKU_RULES` for *source_kind* whose pattern matches the
    whole *sku*; None otherwise (unverified rules never map: the row falls to
    ``unmapped_cost_type`` / ``dq.unmapped_sku``)."""
    if not isinstance(sku, str) or not isinstance(source_kind, str):
        return None
    return _first_verified(SKU_RULES, source_kind, sku)


# =============================================================================================
# GitHub Copilot (CORE-AMENDMENTS K-1 … K-3) — separate tables; the SPEC tables above are pinned
# =============================================================================================

_DOCS = "https://docs.github.com/en"

# ---------------------------------------------------------------------------------------------
# aggregate grammar  copilot:<param>=<value>[@<scope>]  (K-1, addendum CA-35)
# ---------------------------------------------------------------------------------------------

_MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9.\-]{0,63}\Z")
_DAYS_RE = re.compile(r"[1-9][0-9]{0,3}d\Z")
_POS_INT_RE = re.compile(r"[1-9][0-9]{0,8}\Z")
_SCOPE_VALUE_RE = re.compile(r"[^\x00-\x20@;\x7f]{1,128}\Z")
_ENTITY_RE = re.compile(r"(?:enterprise|(?:org|cc):[^\x00-\x20@;\x7f]{1,125})\Z")


def _runner_value_ok(value: str) -> bool:
    return runner_rate(value) is not None


#: Aggregate-grammar parameters → a description of their value domain (checked by
#: :func:`parse_aggregate_spec`): closed sets, or ``model id`` / ``<n>d`` / ``int > 0`` / runner
#: SKU.
AGGREGATE_PARAMS: Mapping[str, str] = types.MappingProxyType({
    "auto": "on | off",
    "auto_tier": "efficiency | balance | intelligence",
    "remap": "target model id",
    "fast": "off | on",
    "seats_idle": "<days>d (idle threshold)",
    "seats_team": "<days>d (idle threshold, team-assigned seats)",
    "seat_policy": "assign_selected | disabled",
    "plan": "business | enterprise",
    "runner": "runner SKU (core.catalog.runner_rate)",
    "context_tier": "default | long_context",
    "mcp": "trim | off",
    "aw_cap": "AI credits per run (int > 0)",
})
_PARAM_CHOICES: Mapping[str, frozenset[str]] = {
    "auto": frozenset({"on", "off"}),
    "auto_tier": frozenset({"efficiency", "balance", "intelligence"}),
    "fast": frozenset({"off", "on"}),
    "seat_policy": frozenset({"assign_selected", "disabled"}),
    "plan": frozenset({"business", "enterprise"}),
    "context_tier": frozenset({"default", "long_context"}),
    "mcp": frozenset({"trim", "off"}),
}
#: Aggregate-grammar scope kinds (``all`` has no value).
AGGREGATE_SCOPES = ("all", "team", "entity", "model", "org")


def _value_ok(param: str, value: str) -> bool:
    choices = _PARAM_CHOICES.get(param)
    if choices is not None:
        return value in choices
    if param == "remap":
        return bool(_MODEL_ID_RE.match(value))
    if param in ("seats_idle", "seats_team"):
        return bool(_DAYS_RE.match(value))
    if param == "aw_cap":
        return bool(_POS_INT_RE.match(value))
    return param == "runner" and _runner_value_ok(value)


def _scope_ok(scope: str) -> bool:
    if scope == "all":
        return True
    kind, sep, value = scope.partition(":")
    if not sep or kind not in AGGREGATE_SCOPES or kind == "all":
        return False
    if kind == "entity":
        return bool(_ENTITY_RE.match(value))
    if kind == "model":
        return bool(_MODEL_ID_RE.match(value))
    return bool(_SCOPE_VALUE_RE.match(value))


@dataclass(frozen=True, slots=True)
class AggregateSpec:
    """One candidate of an aggregate (Copilot cell-model) lever: ``param`` = ``value`` over
    ``scope`` (``"all"`` or ``"<kind>:<value>"`` with kind ∈ team | entity | model | org).
    Invalid parts → ``UsageError`` (the grammar is closed)."""

    param: str
    value: str
    scope: str = "all"

    def __post_init__(self) -> None:
        for name in ("param", "value", "scope"):
            if not isinstance(getattr(self, name), str):
                raise UsageError(f"aggregate spec: {name} must be a string")
        if self.param not in AGGREGATE_PARAMS:
            raise UsageError(f"aggregate spec: unknown parameter {self.param!r}")
        if not _value_ok(self.param, self.value):
            raise UsageError(f"aggregate spec: bad value for {self.param}: {self.value!r}")
        if not _scope_ok(self.scope):
            raise UsageError(f"aggregate spec: bad scope {self.scope!r}")


_AGG_PREFIX = "copilot:"


def parse_aggregate_spec(spec: str) -> AggregateSpec:
    """Parse ``copilot:<param>=<value>[@<scope>]`` (scope defaults to ``all``); anything else —
    unknown parameter, bad value or scope, whitespace, a second ``=`` or ``@`` — → ``UsageError``.
    :func:`to_aggregate_spec` is its exact inverse on canonical strings."""
    if not isinstance(spec, str) or not spec.startswith(_AGG_PREFIX):
        raise UsageError("aggregate spec must start with 'copilot:'")
    body = spec[len(_AGG_PREFIX):]
    if body.count("@") > 1:
        raise UsageError("aggregate spec: at most one '@<scope>'")
    head, at, scope = body.partition("@")
    if at and not scope:
        raise UsageError("aggregate spec: empty scope")
    param, eq, value = head.partition("=")
    if not eq or "=" in value or not param or not value:
        raise UsageError("aggregate spec: expected <param>=<value>")
    return AggregateSpec(param=param, value=value, scope=scope if at else "all")


def to_aggregate_spec(spec: AggregateSpec) -> str:
    """The canonical string of *spec*: ``copilot:<param>=<value>@<scope>`` (the scope is always
    written, ``@all`` included, as in :data:`AGGREGATE_GRIDS`)."""
    if not isinstance(spec, AggregateSpec):
        raise UsageError("to_aggregate_spec expects an AggregateSpec")
    return f"{_AGG_PREFIX}{spec.param}={spec.value}@{spec.scope}"


# ---------------------------------------------------------------------------------------------
# Copilot allowlist and admin actions (K-2; addendum §11.3, §11.4, §19.4)
# ---------------------------------------------------------------------------------------------

#: The Copilot settings allowlist (addendum §11.4), built from ``facts.copilot.settings_keys``
#: (every key targets ``github-copilot``). :data:`ALLOWLIST` is unchanged.
COPILOT_ALLOWLIST: Mapping[str, AllowedKey] = _allowlist(_load_facts().copilot.settings_keys)


def copilot_allowed(key: str) -> AllowedKey:
    """The Copilot allowlist entry for *key* (a key outside :data:`COPILOT_ALLOWLIST` →
    ``UsageError``)."""
    try:
        return COPILOT_ALLOWLIST[key]
    except (KeyError, TypeError):
        raise UsageError(f"settings key {key!r} is not in the Copilot allowlist") from None


@dataclass(frozen=True, slots=True)
class AdminActionDef:
    """One admin checklist item or REST request template of the Copilot policy pack (addendum
    §11.3): nothing is ever executed. ``where`` ∈ ``core.types.ADMIN_ACTION_WHERE``; ``template``
    is content-free checklist text (≤ 400 chars; REST paths keep GitHub's ``{org}`` /
    ``{enterprise}`` / ``{cost_center_id}`` placeholders); ``rest_method`` / ``rest_path`` are None
    for UI and communication items; ``auth_note`` names the role / token the change needs
    (addendum §19.4)."""

    action_id: str
    where: str
    doc_url: str
    template: str
    rest_method: str | None
    rest_path: str | None
    auth_note: str | None


_AUTH_ENT_OWNER = "enterprise owner (enterprise settings)"
_AUTH_ORG_OWNER = "organization owner (organization settings)"
_AUTH_SEATS_REST = ("organization owner; classic PAT manage_billing:copilot or admin:org (addendum "
                    "§19.4)")
_AUTH_BILLING_REST = ("enterprise admin or billing manager; GitHub App 'Enterprise billing: read "
                      "and write'; no classic PAT scope documented (roles only)")
_URL_MODELS = f"{_DOCS}/copilot/how-tos/administer-copilot/manage-for-enterprise/" \
              "manage-availability-of-default-models"
_URL_CODE_REVIEW = f"{_DOCS}/copilot/concepts/agents/code-review"
_URL_BUDGETS = f"{_DOCS}/copilot/concepts/billing-and-usage/organizations-and-enterprises/budgets"
_URL_COST_CENTERS = f"{_DOCS}/billing/concepts/cost-centers"
_URL_SEATS_REST = f"{_DOCS}/rest/copilot/copilot-user-management"
_URL_AGENTIC = f"{_DOCS}/copilot/concepts/agents/about-github-agentic-workflows"
_URL_MANAGED = f"{_DOCS}/copilot/reference/enterprise-administrators/enterprise-managed-settings"
_URL_SESSION_LIMIT = f"{_DOCS}/copilot/how-tos/copilot-cli/use-copilot-cli/set-session-limit"
_URL_CLI_ACTIONS = f"{_DOCS}/copilot/concepts/agents/copilot-cli/copilot-cli-in-github-actions"
_URL_ORG_BILLING = f"{_DOCS}/copilot/concepts/billing-and-usage/organizations-and-enterprises/" \
                   "billing"
_URL_LICENSES = f"{_DOCS}/copilot/how-tos/administer-copilot/manage-for-enterprise/manage-access/" \
                "view-license-usage"
_URL_CLI_CONFIG = f"{_DOCS}/copilot/reference/copilot-cli-reference/cli-config-dir-reference"
_URL_CONTEXT = f"{_DOCS}/copilot/concepts/agents/copilot-cli/context-management"
_URL_OPTIMIZE = f"{_DOCS}/copilot/tutorials/optimize-ai-usage"
_URL_MODELS_LIST = f"{_DOCS}/copilot/reference/ai-models/supported-models"


def _admin(action_id: str, where: str, doc_url: str, template: str, auth_note: str | None, *,
           rest: tuple[str, str] | None = None) -> AdminActionDef:
    return AdminActionDef(action_id=action_id, where=where, doc_url=doc_url, template=template,
                          rest_method=rest[0] if rest else None,
                          rest_path=rest[1] if rest else None, auth_note=auth_note)


_ADMIN_ROWS: tuple[AdminActionDef, ...] = (
    _admin("admin:model_policy", "enterprise settings", _URL_MODELS,
           "Model policies: restrict premium models per organization or cohort and review "
           "'Default availability for released models'. Server-side, so it reaches every editor "
           "incl. JetBrains.", _AUTH_ENT_OWNER),
    _admin("admin:model_policy_fast", "enterprise settings", _URL_MODELS,
           "Model policies: disable the fast-mode model (Claude Opus 4.8 fast mode) for the "
           "cohorts that do not need it.", _AUTH_ENT_OWNER),
    _admin("admin:org_seat_policy", "organization settings",
           f"{_DOCS}/copilot/how-tos/administer-copilot/manage-for-organization/manage-access/"
           "grant-access",
           "Seat policy: switch the organization from 'all members' to 'selected members', then "
           "unassign idle seats (the effect on existing seats is unverified).", _AUTH_ORG_OWNER),
    _admin("admin:seat_plan_change", "enterprise settings",
           f"{_DOCS}/copilot/how-tos/administer-copilot/manage-for-enterprise/manage-plan/"
           "downgrade-subscription",
           "Change the selected users' seats from Copilot Enterprise to Copilot Business "
           "(trade-off: Enterprise-only features).", _AUTH_ENT_OWNER),
    _admin("admin:runner_type", "organization settings",
           f"{_DOCS}/copilot/how-tos/copilot-on-github/set-up-copilot/configure-runners",
           "Runner type: use standard GitHub-hosted runners for Copilot code review and the cloud "
           "agent unless larger runners are needed (larger runners never use included minutes).",
           _AUTH_ORG_OWNER),
    _admin("admin:team_membership_review", "organization settings",
           f"{_DOCS}/copilot/reference/copilot-billing/seat-assignment",
           "Team-assigned idle seats: review membership of the teams that grant Copilot; a seat "
           "held through team membership stays assigned until the member leaves the team.",
           _AUTH_ORG_OWNER),
    _admin("admin:communicate_auto_tier", "personal settings (communicate)",
           f"{_DOCS}/copilot/concepts/models/auto-model-selection",
           "Communicate Auto model selection: Efficiency for routine work; usage is charged at "
           "the model Auto selects, whatever the tier (10% Auto discount on paid plans).", None),
    _admin("admin:review_effort_default", "enterprise settings", _URL_CODE_REVIEW,
           "Code review: set the default effort to Lite explicitly at enterprise or organization "
           "level (the default becomes Balanced on 2026-09-28 unless Lite is chosen).",
           _AUTH_ENT_OWNER),
    _admin("admin:communicate_personal_review_settings", "personal settings (communicate)",
           "https://github.blog/changelog/2026-09-23-copilot-code-review-more-ways-to-request-and-"
           "configure-reviews",
           "Tell developers that their personal default review effort and automatic review of new "
           "pushes and drafts apply to the reviews they request, beside the org default.", None),
    _admin("admin:review_triggers", "repository settings",
           f"{_DOCS}/copilot/how-tos/copilot-on-github/set-up-copilot/configure-code-review",
           "Code review triggers: review once after the pull request is ready instead of on every "
           "push and draft.", "repository admin"),
    _admin("admin:repo_review_mcp_off", "repository settings", _URL_CODE_REVIEW,
           "Repository setting 'Allow Copilot to use MCP tools when reviewing pull requests' is on "
           "by default: turn it off where reviews do not need MCP tools.", "repository admin"),
    _admin("admin:review_instructions", "repository settings",
           f"{_DOCS}/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/"
           "add-repository-instructions",
           "Trim repository custom instructions: review consumption grows with pull request size "
           "and instruction length.", "repository admin"),
    _admin("admin:review_unlicensed_policy", "organization settings", _URL_CODE_REVIEW,
           "Review the policy for Copilot code review of pull requests by unlicensed members and "
           "bots: those reviews are billed directly to the organization.", _AUTH_ORG_OWNER),
    _admin("admin:ci_limits_snippet", "repository settings", _URL_SESSION_LIMIT,
           "CI workflows: run 'copilot -p ... --max-ai-credits N' (minimum 30, a soft limit) and "
           "collect the session with 'tokenbill collect copilot-cli --ci'.", "repository admin"),
    _admin("admin:aw_triggers", "workflow frontmatter", _URL_AGENTIC,
           "Agentic workflows: set 'max-ai-credits: N' in the frontmatter (default cap 1,000 AIC "
           "per run) and review 'on:' schedules and triggers.", "repository admin"),
    _admin("admin:org_cli_billing_policy", "organization settings", _URL_CLI_ACTIONS,
           "Review the policy 'Allow use of Copilot CLI billed to the organization' (on by default "
           "when Copilot CLI is enabled): GITHUB_TOKEN runs are metered to the organization.",
           _AUTH_ORG_OWNER),
    _admin("admin:paid_usage_policy", "enterprise settings", _URL_ORG_BILLING,
           "Review the 'AI credits paid usage' policy (on by default): it allows metered usage "
           "once the pooled credits are exhausted.", _AUTH_ENT_OWNER),
    _admin("admin:cost_center_pool", "enterprise settings", _URL_COST_CENTERS,
           "Cost center AI credit pool: enable it and choose whether members are blocked or "
           "continue as paid overage at the cap (metered billing only).", _AUTH_ENT_OWNER),
    _admin("admin:budget_stop", "enterprise settings", _URL_BUDGETS,
           "Budgets: configure alerts, then 'Stop usage when budget limit is reached' where "
           "acceptable; user-level budgets always hard-stop.", _AUTH_ENT_OWNER),
    _admin("admin:plan_confirm", "enterprise settings", _URL_LICENSES,
           "Confirm the Copilot plan (Business or Enterprise) on the Licensing page, the seats API "
           "plan_type or the seat SKU of the detailed usage report, and record it in answers.json "
           "(or pass --plan).", "enterprise owner or billing manager"),
    _admin("admin:vscode_db_exporter_optin", "personal settings (communicate)",
           "https://github.com/microsoft/vscode/tree/main/extensions/copilot/src/platform/otel/"
           "node/sqlite",
           "Developer opt-in (user setting, cannot be enforced): enable "
           "github.copilot.chat.otel.dbSpanExporter.enabled and run 'tokenbill copilot collect "
           "--source vscode' daily; token counts only, no prompts or file names.", None),
    _admin("rest:org_selected_users_delete", "REST", _URL_SEATS_REST,
           "Remove idle directly assigned seats: body selected_usernames = the logins from GET "
           "/orgs/{org}/copilot/billing/seats with last_activity_at before the cut-off and "
           "assigning_team null (seats end at the close of the billing cycle).", _AUTH_SEATS_REST,
           rest=("DELETE", "/orgs/{org}/copilot/billing/selected_users")),
    _admin("rest:org_selected_teams_delete", "REST", _URL_SEATS_REST,
           "Remove a team whose every seat is idle from the Copilot subscription (body "
           "selected_teams).", _AUTH_SEATS_REST,
           rest=("DELETE", "/orgs/{org}/copilot/billing/selected_teams")),
    _admin("rest:budget_create", "REST", f"{_DOCS}/rest/billing/budgets",
           "Create a budget: budget_amount in whole dollars, prevent_further_usage, "
           "budget_alerting, budget_scope, budget_type BundlePricing, budget_product_sku "
           "ai_credits, budget_entity_name.", _AUTH_BILLING_REST,
           rest=("POST", "/enterprises/{enterprise}/settings/billing/budgets")),
    _admin("rest:cost_center_create", "REST",
           "https://docs.github.com/en/enterprise-cloud@latest/rest/billing/cost-centers",
           "Create a cost center with ai_credit_pool_enabled true.", _AUTH_BILLING_REST,
           rest=("POST", "/enterprises/{enterprise}/settings/billing/cost-centers")),
    _admin("rest:cost_center_patch", "REST",
           "https://docs.github.com/en/enterprise-cloud@latest/rest/billing/cost-centers",
           "Update a cost center (e.g. enable its AI credit pool).", _AUTH_BILLING_REST,
           rest=("PATCH", "/enterprises/{enterprise}/settings/billing/cost-centers/"
                          "{cost_center_id}")),
    _admin("rest:coding_agent_policy", "REST",
           f"{_DOCS}/rest/copilot/copilot-coding-agent-management",
           "Set the enterprise Copilot coding agent policy.",
           "enterprise owner; classic PAT admin:enterprise or manage_billing:copilot",
           rest=("PUT", "/enterprises/{enterprise}/copilot/policies/coding_agent")),
)
#: Every admin checklist item and REST request template of the Copilot policy pack (K-2).
ADMIN_ACTIONS: Mapping[str, AdminActionDef] = types.MappingProxyType(
    {a.action_id: a for a in _ADMIN_ROWS})

# ---------------------------------------------------------------------------------------------
# Copilot lever catalog (K-1; addendum §11.1)
# ---------------------------------------------------------------------------------------------

_TELEMETRY_KEYS = tuple(k for k in COPILOT_ALLOWLIST if k.startswith("copilot.managed.telemetry."))


def _copilot_lever(lever_id: str, lever_class: str, replay: str, needs_eval: bool,
                   upper_bound: bool, tradeoff: bool, patch_keys: tuple[str, ...],
                   finding_kinds: tuple[str, ...]) -> LeverDef:
    return LeverDef(lever_id=lever_id, lever_class=lever_class, grid=(), selector="all",
                    replay=replay, needs_eval=needs_eval, upper_bound=upper_bound,
                    tradeoff=tradeoff, patch_keys=patch_keys, finding_kinds=finding_kinds)


#: The addendum §11.1 table in order. Aggregate levers (``replay="aggregate"``) have ``grid=()``;
#: their candidates are :data:`AGGREGATE_GRIDS`. Levers without a grid are never replayed
#: (``replay="none"``). ``selector`` is ``all`` (cell levers touch no lane). ``upper_bound`` is True
#: for trajectory levers (SPEC §9.1 #4) and for the ``static-overhead`` projection of
#: ``copilot.mcp_trim``.
COPILOT_LEVERS: tuple[LeverDef, ...] = (
    _copilot_lever("copilot.default_model_auto", "trajectory", "aggregate", True, True, False,
                   ("copilot.managed.model",), ("auto-adoption",)),
    _copilot_lever("copilot.model_policy", "trajectory", "aggregate", True, True, True,
                   ("admin:model_policy", "copilot.repo.allowed_models"),
                   ("premium-model-share",)),
    _copilot_lever("copilot.fast_mode_off", "rate", "aggregate", False, False, False,
                   ("admin:model_policy_fast",), ("fast-mode",)),
    _copilot_lever("copilot.seat_reclaim", "rate", "aggregate", False, False, False,
                   ("rest:org_selected_users_delete",), ("idle-seat",)),
    _copilot_lever("copilot.seat_policy_selected", "rate", "aggregate", True, False, True,
                   ("admin:org_seat_policy",), ("seat-auto-assign",)),
    _copilot_lever("copilot.seat_downgrade", "rate", "aggregate", True, False, True,
                   ("admin:seat_plan_change",), ("plan-mix",)),
    _copilot_lever("copilot.agent_runner_standard", "rate", "aggregate", False, False, True,
                   ("admin:runner_type",), ("larger-runner",)),
    # linked from idle-seat findings by id (team-assigned seats; not projected)
    _copilot_lever("copilot.seat_reclaim_team", "behavioral", "none", False, False, False,
                   ("admin:team_membership_review", "rest:org_selected_teams_delete"), ()),
    _copilot_lever("copilot.auto_tier", "behavioral", "none", False, False, False,
                   ("admin:communicate_auto_tier",), ("auto-adoption",)),
    _copilot_lever("copilot.review_effort_lite", "trajectory", "none", True, True, True,
                   ("admin:review_effort_default", "admin:communicate_personal_review_settings"),
                   ("review-default-balanced", "review-cost")),
    _copilot_lever("copilot.review_triggers", "behavioral", "none", False, False, False,
                   ("admin:review_triggers", "admin:communicate_personal_review_settings"),
                   ("review-drivers", "review-cost")),
    _copilot_lever("copilot.review_mcp_off", "behavioral", "none", False, False, False,
                   ("admin:repo_review_mcp_off",), ("review-drivers",)),
    _copilot_lever("copilot.review_instructions_trim", "behavioral", "none", False, False, False,
                   ("admin:review_instructions",), ("review-drivers",)),
    _copilot_lever("copilot.review_unlicensed_off", "behavioral", "none", False, False, False,
                   ("admin:review_unlicensed_policy",), ("direct-org-usage",)),
    _copilot_lever("copilot.session_limits", "behavioral", "none", False, False, False,
                   ("copilot.ci.max_ai_credits", "admin:ci_limits_snippet"),
                   ("ci-uncapped", "direct-org-usage")),
    _copilot_lever("copilot.agentic_workflow_caps", "behavioral", "none", False, False, False,
                   ("copilot.aw.max_ai_credits", "admin:aw_triggers",
                    "admin:org_cli_billing_policy"), ("agentic-workflow-cost", "ci-uncapped")),
    _copilot_lever("copilot.budget_plan", "behavioral", "none", False, False, False,
                   ("rest:budget_create", "rest:cost_center_create", "rest:cost_center_patch"),
                   ("overage-forecast", "budget-paid-usage-uncapped",
                    "budget-no-cost-center-pool")),
    _copilot_lever("copilot.mcp_trim", "cache_transform", "none", False, True, False,
                   ("copilot.managed.deniedMcpServers", "copilot.managed.allowedMcpServers"),
                   ("static-overhead", "mcp-sprawl")),
    _copilot_lever("copilot.context_default", "trajectory", "none", True, True, True,
                   ("copilot.repo.contextTier",), ("long-context-band", "context-heavy-cli")),
    _copilot_lever("copilot.telemetry_on", "behavioral", "none", False, False, False,
                   _TELEMETRY_KEYS, ("cache-health",)),
    _copilot_lever("copilot.vscode_traces_optin", "behavioral", "none", False, False, False,
                   ("admin:vscode_db_exporter_optin",), ("cache-health",)),
)
_COPILOT_LEVERS_BY_ID: Mapping[str, LeverDef] = types.MappingProxyType(
    {lv.lever_id: lv for lv in COPILOT_LEVERS})


def _remap_grid() -> tuple[str, ...]:
    return tuple(to_aggregate_spec(AggregateSpec("remap", fact.target, f"model:{model}"))
                 for model, fact in sorted(_load_facts().copilot.remaps.items()))


#: Candidates of every aggregate lever (canonical aggregate specs). Per-team / per-org variants
#: (``@team:<t>``, ``@org:<o>``) are built by CP-PLAN with :func:`to_aggregate_spec`.
AGGREGATE_GRIDS: Mapping[str, tuple[str, ...]] = types.MappingProxyType({
    "copilot.default_model_auto": ("copilot:auto=on@all",),
    "copilot.model_policy": _remap_grid(),
    "copilot.fast_mode_off": ("copilot:fast=off@all",),
    "copilot.seat_reclaim": ("copilot:seats_idle=30d@all", "copilot:seats_idle=60d@all"),
    "copilot.seat_policy_selected": ("copilot:seat_policy=assign_selected@all",),
    "copilot.seat_downgrade": ("copilot:plan=business@all",),
    "copilot.agent_runner_standard": ("copilot:runner=actions_linux@all",),
})

# ---------------------------------------------------------------------------------------------
# Copilot data accessors (K-3; over core.facts.copilot)
# ---------------------------------------------------------------------------------------------

_COPILOT = _load_facts().copilot

#: Copilot promotions (``github_copilot`` channel); :data:`PROMOTIONS` keeps the SPEC's OpenAI one.
COPILOT_PROMOTIONS: tuple[Promotion, ...] = tuple(
    Promotion(promotion_id=p.promotion_id, model=p.model, channel=p.channel, start=p.start,
              not_before_end=p.not_before_end, source=p.source)
    for p in sorted(_COPILOT.promotions, key=lambda p: p.promotion_id))


def copilot_promotion_for(model: str, date: str) -> Promotion | None:
    """The Copilot promotion covering *model* on *date* (``start ≤ date ≤ not_before_end``); the
    hard end is the promotional rate row's ``effective_to``."""
    for promo in COPILOT_PROMOTIONS:
        if promo.model == model and promo.start <= date <= promo.not_before_end:
            return promo
    return None


#: Copilot model retirements: model → (retirement date, suggested successor or None when already
#: retired).
COPILOT_RETIREMENTS: Mapping[str, tuple[str, str | None]] = types.MappingProxyType({
    model: (fact.retire_on, fact.successor)
    for model, fact in sorted(_COPILOT.retirements.items())})

#: Realization-rate priors by lever class (SPEC §11.2 #8): (p10, p50, p90), None = not projected.
RR_PRIORS: Mapping[str, tuple[Decimal, Decimal, Decimal] | None] = types.MappingProxyType({
    "rate": (Decimal("1.0"), Decimal("1.0"), Decimal("1.0")),
    "cache_transform": (Decimal("0.8"), Decimal("0.9"), Decimal("1.0")),
    "trajectory": (Decimal("-0.2"), Decimal("0.5"), Decimal("1.0")),
    "behavioral": None,
})

_MONTH_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")


def copilot_allowance(plan: str, month: str, *,
                      promo_eligible: bool = True) -> tuple[Decimal, str | None]:
    """Included AI credits per seat of *plan* (``business`` | ``enterprise``) in *month*
    (``YYYY-MM``) and the promotion label when the existing-customer promotion applies (June–August
    2026, ``promo_eligible``); ``unknown`` / ``mixed`` or any other plan → ``UsageError`` (callers
    expand plan scenarios first, ruling R-E22)."""
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise UsageError(f"month must be YYYY-MM, got {month!r}")
    fact = _COPILOT.plans.get(plan) if isinstance(plan, str) else None
    if fact is None:
        raise UsageError(f"copilot_allowance needs plan business or enterprise, got {plan!r}; "
                         "expand the plan scenarios first")
    if (promo_eligible and fact.promo_credits is not None and fact.promo_from is not None
            and fact.promo_to is not None
            and fact.promo_from[:7] <= month < fact.promo_to[:7]):
        return Decimal(fact.promo_credits), f"copilot.promo.{plan}.{fact.promo_from[:7]}"
    return Decimal(fact.included_credits), None


def copilot_cost_type(sku: str | None, *, username_present: bool) -> str:
    """``GITHUB_COST_TYPES`` value of a GitHub billing SKU: AI-credit SKUs are ``ai_credit.user``
    with a username, ``ai_credit.direct`` without; seat, Code Quality, sandbox and legacy-PRU SKUs
    by ``facts.copilot.skus``; Actions runner SKUs (both spellings) ``actions``; anything else
    ``other``."""
    if not isinstance(sku, str) or not sku:
        return "other"
    fact = _COPILOT.skus.get(sku)
    if fact is not None:
        value = fact.cost_type if username_present else fact.cost_type_unattributed
    elif runner_rate(sku) is not None:
        value = "actions"
    else:
        value = "other"
    return value if value in GITHUB_COST_TYPES else "other"


def copilot_seat_plan(sku: str | None) -> str | None:
    """The seat plan a seat SKU bills (``copilot_for_business`` → business, ``copilot_enterprise``
    → enterprise, ``copilot_standalone`` → business, VERIFY); None for other SKUs."""
    fact = _COPILOT.skus.get(sku) if isinstance(sku, str) else None
    return fact.plan if fact is not None else None


def _glob_re(pattern: str) -> re.Pattern[str]:
    """A glob where ``*`` matches within one path segment."""
    parts = [re.escape(p) for p in pattern.split("*")]
    return re.compile("[^/]*".join(parts) + r"\Z")


_WORKFLOW_RULES: tuple[tuple[str, re.Pattern[str] | None, str, str], ...] = tuple(
    (w.pattern, _glob_re(w.pattern) if w.match == "glob" else None, w.match, w.workload)
    for w in _COPILOT.workflow_paths)


def copilot_workload(workflow_path: str | None) -> str | None:
    """``COPILOT_WORKLOADS`` value of a billing ``workflow_path``: the dynamic Copilot workflow
    paths (cloud agent, code review, Code Quality) and ``.github/workflows/*.lock.yml`` (agentic
    workflows; ``*`` within one segment) — all VERIFY in facts; None otherwise."""
    if not isinstance(workflow_path, str) or not workflow_path:
        return None
    for pattern, rx, match, workload in _WORKFLOW_RULES:
        if (match == "exact" and workflow_path == pattern) or (
                rx is not None and rx.match(workflow_path)):
            return workload
    return None


def copilot_category(model: str | None) -> str | None:
    """GitHub's model category (``Lightweight`` | ``Versatile`` | ``Powerful``) of a normalized
    Copilot model id, None when unlisted."""
    return _COPILOT.model_categories.get(model) if isinstance(model, str) else None


def copilot_remap(model: str | None) -> tuple[str, bool] | None:
    """The same-vendor model-policy remap target of *model* and whether both share a tokenizer
    (``(target, tokenizer_same)``), None when there is none."""
    fact = _COPILOT.remaps.get(model) if isinstance(model, str) else None
    return (fact.target, fact.tokenizer_same) if fact is not None else None


def runner_rate(sku: str | None) -> CopilotRunnerRateFact | None:
    """The Actions runner rate of *sku* in either spelling (``linux_16_core`` or
    ``actions_linux_16_core``; ``actions_linux`` also as ``linux``), None when unknown."""
    if not isinstance(sku, str) or not sku:
        return None
    rates = _COPILOT.runner_rates
    for candidate in (sku, sku[len("actions_"):] if sku.startswith("actions_") else None,
                      f"actions_{sku}"):
        if candidate and candidate in rates:
            return rates[candidate]
    return None


_EDITOR_EXACT: Mapping[str, str] = {
    f.pattern.lower(): f.family for f in _COPILOT.editor_families if f.match == "exact"}
_EDITOR_PREFIX: tuple[tuple[str, str], ...] = tuple(sorted(
    ((f.pattern.lower(), f.family) for f in _COPILOT.editor_families if f.match == "prefix"),
    key=lambda pf: (-len(pf[0]), pf[0])))


def editor_family(raw: str | None) -> str:
    """The ``EDITOR_FAMILIES`` value of a seat ``last_activity_editor`` string (e.g.
    ``vscode/1.77.3/copilot/1.86.82``), an activity-report ``last_surface_used`` (e.g. ``VS Code
    1.89.1``), a metrics IDE key (``intellij``, also as ``ide:intellij``) or a family name itself;
    matching is case-insensitive, longest prefix first; unknown, empty or None → ``"other"``. The
    JetBrains and most non-VS Code patterns are ``verified: false`` in facts."""
    if not isinstance(raw, str):
        return "other"
    text = raw.strip()
    if text.lower().startswith("ide:"):
        text = text[4:]
    low = text.lower()
    if not low:
        return "other"
    if low in EDITOR_FAMILIES:
        return low
    exact_family = _EDITOR_EXACT.get(low)
    if exact_family is not None:
        return exact_family
    for prefix, family in _EDITOR_PREFIX:
        if low.startswith(prefix):
            return family
    return "other"


#: ``Attribution.agent_product`` values of Copilot lanes (VS Code, JetBrains, CLI, gh-aw, others).
COPILOT_AGENT_PRODUCTS = ("copilot_vscode", "copilot_jetbrains", "copilot_cli", "copilot_gh_aw",
                          "copilot_other")


def agent_family(agent_product: str | None) -> str:
    """The product family (:data:`FAMILIES`) of an ``agent_product``: ``"copilot"`` for the
    Copilot agent products (and any ``copilot_*`` value), else ``"default"``."""
    if isinstance(agent_product, str) and (
            agent_product in COPILOT_AGENT_PRODUCTS or agent_product.startswith("copilot_")):
        return "copilot"
    return "default"


# ---------------------------------------------------------------------------------------------
# Copilot fixes (addendum §10.1–§10.4), family exclusions (§10.4) and count sources (§8.2)
# ---------------------------------------------------------------------------------------------


def _fix(text: str, doc_url: str, patch: tuple[tuple[str, str], ...] | None = None) -> Fix:
    return Fix(text=text, config_patch=patch, target="github-copilot", doc_url=doc_url)


_LANES_MODEL_SWITCH = _fix(
    "Auto switches models only at cache boundaries; switch models at /new or via a subagent; keep "
    "effort and context tier fixed mid-session.", _URL_OPTIMIZE)
_CONTEXT_DEFAULT_PATCH = (("copilot.repo.contextTier", '"default"'),)

#: Copilot fix per ``(detector_id, kind or None)`` (None: every kind of the detector); read through
#: :func:`fix_for`.
_COPILOT_FIXES: Mapping[tuple[str, str | None], Fix] = types.MappingProxyType({
    # generic detectors kept on Copilot lanes (addendum §10.4)
    ("cache.miss-by-cause", None): _LANES_MODEL_SWITCH,
    ("cache.switch-churn", None): _LANES_MODEL_SWITCH,
    ("cache.rebuild", "compaction-cold"): _fix(
        "/compact while the session is warm, /new for a new task.", _URL_CONTEXT),
    ("cache.cold-resume", None): _fix(
        "/compact before stepping away; resume with /new for a new task.", _URL_CONTEXT),
    ("attrib.carry", None): _fix(
        "Large tool output is saved to a file above COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES; trim "
        "MCP tools (deniedMcpServers / allowedMcpServers).",
        f"{_DOCS}/copilot/reference/copilot-cli-reference/cli-command-reference"),
    ("tail.runaway", None): _fix(
        "Cap sessions with --max-ai-credits N or /limits set max-ai-credits N (minimum 30, a "
        "soft limit).", _URL_SESSION_LIMIT),
    ("model.routing", "delegation-routing"): _fix(
        "Assign cheaper subagent models with /subagents (overlaps the premium-model-share "
        "finding; never add the two).", _URL_OPTIMIZE),
    ("model.routing", "same-tier-upgrade"): _fix(
        "Choose the successor model deliberately; see the retirement dates.", _URL_MODELS_LIST),
    ("model.routing", "effort-mix"): _fix(
        "Keep the default effort at medium; set effortLevel in the repository's "
        ".github/copilot/settings.json for the CLI (trusted directories only).", _URL_CLI_CONFIG,
        (("copilot.repo.effortLevel", '"medium"'),)),
    # copilot.seats-budgets (addendum §10.1)
    ("copilot.seats-budgets", "plan-status"): _fix(
        "Plan unknown or conflicting: read it from the seats API plan_type, the seat SKU of the "
        "detailed usage report (copilot_for_business / copilot_enterprise) or Enterprise settings "
        "> Licensing, then pass --plan or answers.json.", _URL_LICENSES),
    ("copilot.seats-budgets", "pool-regime"): _fix(
        "Act by regime: in overage, credit savings cut the invoice and seat removals do not; in "
        "slack, seat removals cut the invoice and credit savings only free pool headroom.",
        _URL_ORG_BILLING),
    ("copilot.seats-budgets", "overage-forecast"): _fix(
        "Set budgets (alerts, then 'Stop usage when budget limit is reached' where acceptable), "
        "cost-center AI credit pools (metered billing only) and model policies.", _URL_BUDGETS),
    ("copilot.seats-budgets", "promo-cliff"): _fix(
        "Re-size user-level and enterprise budgets for the standard pool: the promotional "
        "credits ended on 2026-09-01.", _URL_BUDGETS),
    ("copilot.seats-budgets", "idle-seat"): _fix(
        "Remove idle directly assigned seats with DELETE /orgs/{org}/copilot/billing/"
        "selected_users (list from the seats API filtered on last_activity_at and assigning_team "
        "null); for team-assigned seats remove the member from the Copilot-granting team.",
        _URL_SEATS_REST),
    ("copilot.seats-budgets", "seat-auto-assign"): _fix(
        "Switch the organization to 'selected members', then unassign idle seats (the effect on "
        "existing seats is unverified).",
        f"{_DOCS}/copilot/how-tos/administer-copilot/manage-for-organization/manage-access/"
        "grant-access"),
    ("copilot.seats-budgets", "completions-only-seat"): _fix(
        "Where applicable, a Copilot Business seat instead of Enterprise for completions-only "
        "users (the seat still feeds the pool).",
        f"{_DOCS}/copilot/tutorials/roll-out-at-scale/assign-licenses/choose-enterprise-plan"),
    ("copilot.seats-budgets", "plan-mix"): _fix(
        "Downgrade the selected users from Copilot Enterprise to Copilot Business (trade-off: "
        "Enterprise-only features).",
        f"{_DOCS}/copilot/how-tos/administer-copilot/manage-for-enterprise/manage-plan/"
        "downgrade-subscription"),
    ("copilot.seats-budgets", "duplicate-seat"): _fix(
        "Assign each user's seat through one organization or cost center (a multi-organization "
        "seat is billed once, via a random organization).",
        f"{_DOCS}/copilot/reference/copilot-billing/seat-assignment"),
    ("copilot.seats-budgets", "budget-paid-usage-uncapped"): _fix(
        "Create metered budgets with 'Stop usage when budget limit is reached' where acceptable "
        "(request file: POST /enterprises/{enterprise}/settings/billing/budgets).", _URL_BUDGETS),
    ("copilot.seats-budgets", "budget-stop-usage-off"): _fix(
        "Configure alerts first, then enable 'Stop usage when budget limit is reached' where "
        "acceptable.", _URL_BUDGETS),
    ("copilot.seats-budgets", "budget-zero-user-budget"): _fix(
        "Review user-level budgets set to $0: user-level budgets always hard-stop and cover pool "
        "and metered usage.", _URL_BUDGETS),
    ("copilot.seats-budgets", "budget-ulb-gap"): _fix(
        "Apply GitHub's sizing check: the maximum metered spend is the sum of user-level caps "
        "minus the pool; size metered budgets to it.",
        f"{_DOCS}/copilot/tutorials/budgets/optimizing-your-budget-configuration"),
    ("copilot.seats-budgets", "budget-org-multi-org-seats"): _fix(
        "Budget at enterprise or cost-center level: users hold seats through several "
        "organizations.", _URL_BUDGETS),
    ("copilot.seats-budgets", "budget-no-cost-center-pool"): _fix(
        "Enable the cost center's AI credit pool and choose whether its members are blocked or "
        "continue as paid overage at the cap.", _URL_COST_CENTERS),
    ("copilot.seats-budgets", "budget-enterprise-misread"): _fix(
        "The enterprise budget governs metered usage after the pool, not the total bill; seat "
        "fees are outside it.", _URL_BUDGETS),
    # copilot.org-scan (addendum §10.2)
    ("copilot.org-scan", "premium-model-share"): _fix(
        "Set model policies per organization or cohort (server-side, every editor) and a managed "
        "default model; price-only estimate, evaluate quality first.", _URL_MODELS),
    ("copilot.org-scan", "fast-mode"): _fix(
        "Disable the fast-mode model in the model policies.", _URL_MODELS),
    ("copilot.org-scan", "auto-adoption"): _fix(
        "Set the managed default model to Auto (per-team files for waves); managed model does "
        "not reach JetBrains, use the server-side model policy there; Auto tier Efficiency for "
        "routine work.", _URL_MANAGED, (("copilot.managed.model", '"auto"'),)),
    ("copilot.org-scan", "forced-migration"): _fix(
        "Choose the successor model deliberately before the retirement date.", _URL_MODELS_LIST),
    ("copilot.org-scan", "compliance-uplift"): _fix(
        "Prefer cheaper compliant models; the compliance policy itself is not a cost lever.",
        "https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/"
        "github-copilot-with-data-residency"),
    ("copilot.org-scan", "cache-health"): _fix(
        "Install collectors or OpenTelemetry export (managed telemetry, the VS Code agent-traces "
        "opt-in) to see the causes.", f"{_DOCS}/copilot/concepts/enterprise/opentelemetry"),
    ("copilot.org-scan", "review-cost"): _fix(
        "Review the code-review checklist: default effort Lite, review triggers, the MCP tools "
        "setting and repository custom instructions.", _URL_CODE_REVIEW),
    ("copilot.org-scan", "review-default-balanced"): _fix(
        "Set the code-review default effort to Lite explicitly at enterprise or organization "
        "level before 2026-09-28; personal default efforts still apply to reviews users request.",
        _URL_CODE_REVIEW),
    ("copilot.org-scan", "review-drivers"): _fix(
        "Review the repository setting 'Allow Copilot to use MCP tools when reviewing pull "
        "requests' (on by default), custom instruction size and personal automatic review of new "
        "pushes and drafts.", _URL_CODE_REVIEW),
    ("copilot.org-scan", "direct-org-usage"): _fix(
        "Review the unlicensed-member review policy, --max-ai-credits for GITHUB_TOKEN CLI runs, "
        "agentic-workflow caps and cost-center budgets.", _URL_CLI_ACTIONS),
    ("copilot.org-scan", "agentic-workflow-cost"): _fix(
        "Set max-ai-credits in each workflow's frontmatter near p99 x 1.5, review on: schedules "
        "and the policy 'Allow use of Copilot CLI billed to the organization'.", _URL_AGENTIC),
    ("copilot.org-scan", "larger-runner"): _fix(
        "Set the organization's runner type for code review and the cloud agent to standard "
        "runners.", f"{_DOCS}/copilot/how-tos/copilot-on-github/set-up-copilot/configure-runners"),
    ("copilot.org-scan", "cloud-agent-cost"): _fix(
        "Review the cloud agent's setup steps, runner and session limits.",
        f"{_DOCS}/copilot/concepts/agents/cloud-agent/about-cloud-agent"),
    ("copilot.org-scan", "agent-failed-sessions"): _fix(
        "Scope agent tasks smaller; failed, timed-out and cancelled sessions still consume "
        "credits.", f"{_DOCS}/copilot/concepts/agents/cloud-agent/about-cloud-agent"),
    ("copilot.org-scan", "unattributed-spend"): _fix(
        "Assign users, organizations and repositories to cost centers.",
        f"{_DOCS}/billing/how-tos/products/use-cost-centers"),
    ("copilot.org-scan", "mcp-sprawl"): _fix(
        "Restrict MCP servers with the managed deniedMcpServers / allowedMcpServers lists; use "
        "tool search.", _URL_MANAGED),
    ("copilot.org-scan", "context-heavy-cli"): _fix(
        "/compact at task boundaries and the default context tier (repository contextTier, CLI "
        "trusted directories only).", _URL_CLI_CONFIG, _CONTEXT_DEFAULT_PATCH),
    ("copilot.org-scan", "editor-mix"): _fix(
        "JetBrains-heavy teams: server-side model policy first (managed model does not reach "
        "JetBrains); VS Code teams: managed model \"auto\".", _URL_MANAGED),
    # copilot.lanes (addendum §10.3)
    ("copilot.lanes", "long-context-band"): _fix(
        "Keep the default context tier (repository contextTier, CLI trusted directories only); "
        "/compact at task boundaries.", _URL_CLI_CONFIG, _CONTEXT_DEFAULT_PATCH),
    ("copilot.lanes", "compaction-cost"): _fix(
        "Smaller tool output, an earlier /compact and the default context tier.", _URL_CONTEXT),
    ("copilot.lanes", "static-overhead"): _fix(
        "Trim MCP tools (managed deniedMcpServers), use tool search and trim instructions.",
        _URL_MANAGED),
    ("copilot.lanes", "subagent-share"): _fix(
        "Assign cheaper subagent models (/subagents).", _URL_OPTIMIZE),
    ("copilot.lanes", "ci-uncapped"): _fix(
        "Cap CI sessions: copilot -p ... --max-ai-credits N; agentic workflows: max-ai-credits: N "
        "in the frontmatter (N near p99 x 1.5).", _URL_SESSION_LIMIT),
})


def fix_for(detector_id: str, kind: str, family: str) -> Fix | None:
    """The fix a finding of (*detector_id*, *kind*) carries on product family *family*: for
    ``"copilot"`` the Copilot fix (target ``github-copilot``, a docs URL and only
    :data:`COPILOT_ALLOWLIST` keys in its ``config_patch``) by kind, else by detector; None when
    none is known or for any other family (the detector's own fix applies)."""
    if family != "copilot":
        return None
    return _COPILOT_FIXES.get((detector_id, kind)) or _COPILOT_FIXES.get((detector_id, None))


_COPILOT_ONLY = frozenset({"copilot"})

#: Generic detectors (and kinds) that do not run on a product family (addendum §10.4):
#: ``(detector_id, kind or None for the whole detector)`` → excluded families. Read by
#: ``core.registry.run_detectors``.
FAMILY_EXCLUSIONS: Mapping[tuple[str, str | None], frozenset[str]] = types.MappingProxyType({
    ("cache.ttl-advisor", None): _COPILOT_ONLY,       # TTL not configurable on Copilot
    ("cache.gateway-disabled", None): _COPILOT_ONLY,  # no gateway
    ("cache.cold-fanout", None): _COPILOT_ONLY,       # the fan-out repair is SDK-only
    ("premium.sticky-escalation", None): _COPILOT_ONLY,  # Claude Code settings
    ("aggregate.org-scan", None): _COPILOT_ONLY,      # channels delegated → copilot.org-scan
    ("block.breakers", None): _COPILOT_ONLY,          # no fingerprints
    ("premium.modifiers", "fast-premium"): _COPILOT_ONLY,   # → copilot.org-scan fast-mode
    ("automation", "ci-run-cost"): _COPILOT_ONLY,           # → copilot.lanes ci-uncapped
    ("context.static-prefix", "static-prefix"): _COPILOT_ONLY,  # → copilot.lanes static-overhead
    ("model.routing", "default-model"): _COPILOT_ONLY,      # Claude Code managed keys
    ("model.routing", "default-effort"): _COPILOT_ONLY,
})

#: What ``core.kanon.scope_counter`` counts distinct people over (addendum §8.2, ruling R-E16).
COUNT_SOURCES = ("requests", "cost_lines", "licenses", "activity", "entity")
_SEATS, _ORG, _LANES = "copilot.seats-budgets", "copilot.org-scan", "copilot.lanes"

#: Count source per ``(detector_id, kind)``; absent pairs count ``"requests"``
#: (:func:`count_source`).
COUNT_SOURCE: Mapping[tuple[str, str], str] = types.MappingProxyType({
    **{(_SEATS, k): "entity" for k in (
        "plan-status", "pool-regime", "overage-forecast", "promo-cliff",
        "budget-paid-usage-uncapped", "budget-stop-usage-off", "budget-ulb-gap",
        "budget-org-multi-org-seats", "budget-enterprise-misread", "dq.skipped-kinds")},
    **{(_SEATS, k): "licenses" for k in (
        "idle-seat", "seat-auto-assign", "completions-only-seat", "plan-mix", "duplicate-seat",
        "budget-zero-user-budget")},
    (_SEATS, "budget-no-cost-center-pool"): "cost_lines",
    **{(_ORG, k): "cost_lines" for k in (
        "premium-model-share", "fast-mode", "auto-adoption", "cache-health")},
    **{(_ORG, k): "entity" for k in (
        "forced-migration", "compliance-uplift", "review-cost", "review-default-balanced",
        "review-drivers", "direct-org-usage", "agentic-workflow-cost", "larger-runner",
        "cloud-agent-cost", "agent-failed-sessions", "unattributed-spend", "dq.skipped-kinds")},
    **{(_ORG, k): "activity" for k in ("mcp-sprawl", "context-heavy-cli", "editor-mix")},
    **{(_LANES, k): "requests" for k in (
        "long-context-band", "compaction-cost", "static-overhead", "subagent-share",
        "ci-uncapped")},
})


def count_source(detector_id: str, kind: str) -> str:
    """:data:`COUNT_SOURCE` of (*detector_id*, *kind*), ``"requests"`` when not listed."""
    return COUNT_SOURCE.get((detector_id, kind), "requests")


def _check_tables() -> None:
    """Import-time consistency of the Copilot tables (a broken table is a build error)."""
    for a in ADMIN_ACTIONS.values():
        if a.where not in ADMIN_ACTION_WHERE or len(a.template) > 400:  # pragma: no cover
            raise UsageError(f"admin action {a.action_id}: bad where or template")
    for lv in COPILOT_LEVERS:
        for key in lv.patch_keys:
            if key not in COPILOT_ALLOWLIST and key not in ADMIN_ACTIONS:  # pragma: no cover
                raise UsageError(f"lever {lv.lever_id}: patch key {key!r} unresolved")
    for lever_id, grid in AGGREGATE_GRIDS.items():
        for spec in grid:
            if to_aggregate_spec(parse_aggregate_spec(spec)) != spec:  # pragma: no cover
                raise UsageError(f"lever {lever_id}: grid entry {spec!r} is not canonical")


_check_tables()
