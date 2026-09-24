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
"""

from __future__ import annotations

import datetime as _dt
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass

from tokenbill.core.errors import UsageError
from tokenbill.core.facts import load as _load_facts

__all__ = [
    "ALLOWLIST",
    "ANNOUNCED_UNPRICED",
    "LEVERS",
    "LEVER_CLASSES",
    "PROMOTIONS",
    "RETIREMENTS",
    "SKU_RULES",
    "SUCCESSORS",
    "AllowedKey",
    "LeverDef",
    "Promotion",
    "SkuRule",
    "allowed",
    "lever",
    "levers_for_kind",
    "map_sku",
    "promotion_for",
    "retiring_within",
    "successor",
]

#: Lever classes (SPEC §11.2 realization priors).
LEVER_CLASSES = ("rate", "cache_transform", "trajectory", "behavioral")
_REPLAY_KINDS = ("usage", "block", "none")


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
    replay: str                              # "usage" | "block" | "none"
    needs_eval: bool
    upper_bound: bool
    tradeoff: bool
    patch_keys: tuple[str, ...]              # ALLOWLIST keys (delivery through managed settings)
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
    """The catalog entry for *lever_id* (unknown id → ``UsageError``)."""
    try:
        return _LEVERS_BY_ID[lever_id]
    except KeyError:
        raise UsageError(f"unknown lever {lever_id!r}") from None


def levers_for_kind(kind: str) -> tuple[LeverDef, ...]:
    """Levers a finding of *kind* links to, in catalog order (empty when none)."""
    return tuple(lv for lv in LEVERS if kind in lv.finding_kinds)


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


def _allowlist() -> Mapping[str, AllowedKey]:
    out: dict[str, AllowedKey] = {}
    for key, fact in _load_facts().settings_keys.items():
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
    """The allowlist entry for *key* (a key outside the allowlist → ``UsageError``)."""
    try:
        return ALLOWLIST[key]
    except KeyError:
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
    """The promotion covering *model* on *channel* on *date* (``start ≤ date ≤ not_before_end``)."""
    for promo in PROMOTIONS:
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
