"""Cache-rule table (SPEC §3.14; F-SEM): data with sources, and the conditional effort exemption.

:class:`RulesTable` implements :class:`~tokenbill.core.protocols.CacheRulesProvider` with one row
per channel: the Anthropic channels (``anthropic_api``, ``claude_platform_aws``, ``foundry``:
workspace scope; ``bedrock``, ``vertex``: organization scope), ``openai_api`` (organization) and
``azure_openai`` (subscription, D43). Minimum cacheable tokens are **not** here: they come from the
rate row (``Pricer.min_cacheable_tokens``).

:func:`effort_change_keeps_cache` is the single D28 predicate. The usage-level engine
(``core.transitions``) and the block-level engine (``sim.block_replay``) both call it, so the two
engines agree on whether an effort (or thinking) change breaks the messages cache.

Facts re-checked against the primary sources on 2026-09-23 (``VERIFIED_ON``): TTLs, TTL clock,
visibility, 20-position lookback with collapsed tool runs, 4 breakpoints, isolation scopes, the
invalidation hierarchy (Anthropic prompt-caching docs); Claude Code effort behavior (Claude Code
prompt-caching docs); the per-message effort beta and its models and platforms (Anthropic
mid-conversation system messages docs); OpenAI 30-minute TTL from last use, organization scope and
the parameters that break the cache (OpenAI prompt-caching guide); Azure OpenAI subscription
isolation (Microsoft Learn). The per-message effort beta is documented for the Claude API and Google
Cloud only ("not Amazon Bedrock or Microsoft Foundry"), so ``foundry`` is **not** in
:data:`PER_MESSAGE_EFFORT_CHANNELS` although SPEC §3.14 lists it (see
``tests/v2/sem/CONTRACT-CHANGE-F-SEM-1.md``). The models on which thinking/effort also invalidate
the tools and system tiers are "model-specific" in the docs without a list, so
``effort_invalidates_all_tiers_models`` stays empty (§19.8 #14, unverified).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from dataclasses import dataclass

from tokenbill.core.errors import UsageError
from tokenbill.core.models import normalize_model

__all__ = [
    "ANTHROPIC_CHANNELS",
    "CLAUDE_CODE_EFFORT_MIN_VERSION",
    "EFFORT_KEEPS_CACHE_CLAUDE_CODE",
    "EFFORT_KEEPS_CACHE_EXCLUDED_CHANNELS",
    "PER_MESSAGE_EFFORT_BETA",
    "PER_MESSAGE_EFFORT_CHANNELS",
    "PER_MESSAGE_EFFORT_MODELS",
    "VERIFIED_ON",
    "CacheRules",
    "RulesTable",
    "effort_change_keeps_cache",
    "parse_version",
    "version_at_least",
]

VERIFIED_ON = "2026-09-23"

EFFORT_KEEPS_CACHE_CLAUDE_CODE = ("claude-opus-5-5", "claude-fable-5-1")
PER_MESSAGE_EFFORT_BETA = "mid-conversation-output-config-2026-07-01"
PER_MESSAGE_EFFORT_MODELS = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5",
                             "claude-opus-5-5")
#: Claude Code keeps the cache on an effort change from this version on (cc-cache-breakers).
CLAUDE_CODE_EFFORT_MIN_VERSION = "2.1.260"
#: Channels where Claude Code's effort change still invalidates the cache (D28 (a)).
EFFORT_KEEPS_CACHE_EXCLUDED_CHANNELS = frozenset({"bedrock", "vertex"})
#: Channels where the per-message effort beta is available (D28 (b), primary-source verified).
PER_MESSAGE_EFFORT_CHANNELS = frozenset({"anthropic_api", "claude_platform_aws", "vertex"})

ANTHROPIC_CHANNELS = ("anthropic_api", "claude_platform_aws", "foundry", "bedrock", "vertex")

_SRC_CACHING = "https://platform.claude.com/docs/en/build-with-claude/prompt-caching"
_SRC_MIDCONV = "https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages"
_SRC_CC_CACHE = "https://code.claude.com/docs/en/prompt-caching"
_SRC_BEDROCK = "https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html"
_SRC_VERTEX = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
_SRC_OPENAI = "https://developers.openai.com/api/docs/guides/prompt-caching"
_SRC_AZURE = "https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching"


@dataclass(frozen=True, slots=True)
class CacheRules:
    """Cache behavior of one (provider, channel[, model family]) — SPEC §3.14."""

    provider: str
    channel: str
    ttl_options_s: tuple[int, ...]        # Anthropic (300, 3600); OpenAI 5.6+ (1800,)
    ttl_measured_from: str                # "request_start" (Anthropic) | "last_use" (OpenAI)
    refresh_on_read: bool                 # True
    visible_from: str                     # "first_token" (Anthropic) | "response_end" (OpenAI)
    lookback_positions: int | None        # 20 (Anthropic); None (OpenAI)
    collapse_tool_runs: bool              # True on Anthropic 1P
    max_breakpoints: int                  # 4
    scope: str                            # "workspace" | "organization" | "subscription"
    # tier → params salted into that tier's hash (the invalidation hierarchy)
    tier_params: tuple[tuple[str, tuple[str, ...]], ...]
    # models on which thinking/effort also salt tools/system; empty until verified (§19.8 #14)
    effort_invalidates_all_tiers_models: tuple[str, ...]
    sources: tuple[str, ...]


_ANTHROPIC_TIER_PARAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tools", ("model", "tool_defs")),
    ("system", ("speed", "web_search_enabled", "citations_enabled")),
    ("messages", ("tool_choice", "disable_parallel_tool_use", "has_images", "thinking", "effort",
                  "output_format")),
)
# OpenAI has no tier hierarchy: model, tools, parallel_tool_calls, text.format, reasoning.effort,
# text.verbosity and context_management each break the whole prefix, so they salt the first tier.
_OPENAI_TIER_PARAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tools", ("model", "tool_defs", "disable_parallel_tool_use", "output_format", "effort",
               "context_management")),
    ("system", ()),
    ("messages", ()),
)
_ANTHROPIC_SCOPE = {
    "anthropic_api": "workspace",
    "claude_platform_aws": "workspace",
    "foundry": "workspace",
    "bedrock": "organization",
    "vertex": "organization",
}
_ANTHROPIC_EXTRA_SOURCES = {
    "anthropic_api": (_SRC_MIDCONV,),
    "claude_platform_aws": (_SRC_MIDCONV,),
    "foundry": (_SRC_MIDCONV,),
    "bedrock": (_SRC_BEDROCK,),
    "vertex": (_SRC_VERTEX, _SRC_MIDCONV),
}


def _anthropic_rules(channel: str, scope: str, extra_sources: tuple[str, ...]) -> CacheRules:
    return CacheRules(
        provider="anthropic",
        channel=channel,
        ttl_options_s=(300, 3600),
        ttl_measured_from="request_start",
        refresh_on_read=True,
        visible_from="first_token",
        lookback_positions=20,
        collapse_tool_runs=channel == "anthropic_api",
        max_breakpoints=4,
        scope=scope,
        tier_params=_ANTHROPIC_TIER_PARAMS,
        effort_invalidates_all_tiers_models=(),
        sources=(_SRC_CACHING, _SRC_CC_CACHE, *extra_sources),
    )


def _openai_rules(channel: str, *, ttl_options_s: tuple[int, ...]) -> CacheRules:
    azure = channel == "azure_openai"
    return CacheRules(
        provider="openai",
        channel=channel,
        ttl_options_s=ttl_options_s,
        ttl_measured_from="last_use",
        refresh_on_read=True,
        visible_from="response_end",
        lookback_positions=None,
        collapse_tool_runs=False,
        max_breakpoints=4,
        scope="subscription" if azure else "organization",
        tier_params=_OPENAI_TIER_PARAMS,
        effort_invalidates_all_tiers_models=(),
        sources=(_SRC_AZURE, _SRC_OPENAI) if azure else (_SRC_OPENAI,),
    )


# Bounded digit runs: model ids come from provider payloads, and ``int()`` of a digit string longer
# than ``sys.get_int_max_str_digits()`` raises ValueError (a garbage id must not crash rules_for).
_GPT_VERSION_RE = re.compile(r"\Agpt-(\d{1,6})(?:\.(\d{1,6}))?(?!\d)")


def _openai_explicit_ttl(model: str) -> tuple[int, ...]:
    """GPT-5.6 and later cache for 30 minutes from the last write or reuse; earlier models keep
    prefixes for an unspecified in-memory time (TTL unknown → ``()``). An id whose version is
    not a short dotted number (e.g. an implausibly long digit run) has no TTL option."""
    m = _GPT_VERSION_RE.match(model if isinstance(model, str) else "")
    if m is None:
        return ()
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    return (1800,) if (major, minor) >= (5, 6) else ()


class RulesTable:
    """The built-in cache-rule table (implements ``CacheRulesProvider``).

    ``rules_for`` is keyed by channel; the model matters only for OpenAI TTLs (GPT-5.6+ → 1800 s on
    ``openai_api``; Azure's TTL for 5.6+ is unverified, so Azure rows carry no TTL option). An
    unknown channel falls back by provider: Anthropic → the ``anthropic_api`` semantics
    (workspace scope), OpenAI → the ``openai_api`` semantics; any other provider gets a
    conservative row (no TTL option, organization scope, visibility at response end).
    """

    def __init__(self) -> None:
        self._anthropic = {
            channel: _anthropic_rules(channel, scope, _ANTHROPIC_EXTRA_SOURCES[channel])
            for channel, scope in _ANTHROPIC_SCOPE.items()
        }
        self._cache: dict[tuple[str, str, str], CacheRules] = {}

    def channels(self) -> tuple[str, ...]:
        """The channels with a dedicated row."""
        return (*ANTHROPIC_CHANNELS, "openai_api", "azure_openai")

    def rules_for(self, provider: str, channel: str, model: str) -> CacheRules:
        """The cache rules for a request served on *channel* by *provider* (``model`` is the
        normalized model id)."""
        key = (provider, channel, model)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        rules = self._build(provider, channel, model)
        if len(self._cache) < 4096:
            self._cache[key] = rules
        return rules

    def _build(self, provider: str, channel: str, model: str) -> CacheRules:
        row = self._anthropic.get(channel)
        if row is not None:
            return row
        if channel == "openai_api":
            return _openai_rules(channel, ttl_options_s=_openai_explicit_ttl(model))
        if channel == "azure_openai":
            return _openai_rules(channel, ttl_options_s=())
        if provider == "anthropic":
            return dataclasses.replace(self._anthropic["anthropic_api"], channel=channel,
                                       collapse_tool_runs=False)
        if provider == "openai":
            rules = _openai_rules("openai_api", ttl_options_s=_openai_explicit_ttl(model))
            return dataclasses.replace(rules, channel=channel)
        return CacheRules(
            provider=provider,
            channel=channel,
            ttl_options_s=(),
            ttl_measured_from="request_start",
            refresh_on_read=True,
            visible_from="response_end",
            lookback_positions=None,
            collapse_tool_runs=False,
            max_breakpoints=4,
            scope="organization",
            tier_params=_ANTHROPIC_TIER_PARAMS,
            effort_invalidates_all_tiers_models=(),
            sources=("default: provider without a verified cache-rule row",),
        )


_VERSION_RE = re.compile(r"\A\s*v?(\d+(?:\.\d+)*)")
#: Longest numeric component accepted; longer digit runs are not versions (and ``int()`` of a
#: digit string above ``sys.get_int_max_str_digits()`` would raise ValueError).
_MAX_VERSION_DIGITS = 18


def parse_version(version: str | None) -> tuple[int, ...] | None:
    """Leading numeric dotted components of *version* (``"2.1.270 (Claude Code)"`` →
    ``(2, 1, 270)``), or None when there are none or a component has more than 18 digits
    (client versions come from transcripts: garbage is "unknown", never a crash)."""
    if not isinstance(version, str):
        return None
    m = _VERSION_RE.match(version)
    if m is None:
        return None
    parts = m.group(1).split(".")
    if any(len(part) > _MAX_VERSION_DIGITS for part in parts):
        return None
    return tuple(int(part) for part in parts)


def version_at_least(version: str, minimum: str) -> bool:
    """Numeric dotted comparison (``"2.1.26" < "2.1.260"``); missing components count as 0."""
    a, b = parse_version(version), parse_version(minimum)
    if a is None or b is None:
        raise UsageError("not a dotted version")
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


def _canonical_model(model: str, known: Sequence[str]) -> str:
    if model in known or not model:
        return model
    return normalize_model(model).model


def effort_change_keeps_cache(*, agent_product: str | None, model: str, channel: str,
                              client_version: str | None, betas: Sequence[str]) -> bool:
    """D28: True iff a top-level effort (or thinking) change keeps the messages cache.

    (a) ``agent_product == "claude_code"``, *model* in :data:`EFFORT_KEEPS_CACHE_CLAUDE_CODE`,
        *channel* not Bedrock/Vertex, and *client_version* unknown or ≥ 2.1.260 (numeric dotted
        comparison; a version string without leading digits counts as unknown); or
    (b) :data:`PER_MESSAGE_EFFORT_BETA` in *betas*, *model* in :data:`PER_MESSAGE_EFFORT_MODELS` and
        *channel* in :data:`PER_MESSAGE_EFFORT_CHANNELS` (Claude API, Claude Platform on AWS,
        Vertex).

    Otherwise an effort or thinking change invalidates the messages tier
    (``anth-invalidation-hierarchy``, ``anth-effort-rerun-budgets``).

    *betas* is matched by exact value: a raw ``anthropic-beta`` header string is split on commas
    (never a substring test) and None counts as no betas.
    """
    if betas is None:
        betas = ()
    elif isinstance(betas, str):
        betas = tuple(part.strip() for part in betas.split(","))
    if agent_product == "claude_code":
        cc_model = _canonical_model(model, EFFORT_KEEPS_CACHE_CLAUDE_CODE)
        if cc_model in EFFORT_KEEPS_CACHE_CLAUDE_CODE and \
                channel not in EFFORT_KEEPS_CACHE_EXCLUDED_CHANNELS:
            parsed = parse_version(client_version)
            if parsed is None or version_at_least(client_version or "",
                                                  CLAUDE_CODE_EFFORT_MIN_VERSION):
                return True
    if PER_MESSAGE_EFFORT_BETA in betas:
        pm_model = _canonical_model(model, PER_MESSAGE_EFFORT_MODELS)
        if pm_model in PER_MESSAGE_EFFORT_MODELS and channel in PER_MESSAGE_EFFORT_CHANNELS:
            return True
    return False
