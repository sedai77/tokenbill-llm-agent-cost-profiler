"""CORE-AMENDMENTS S-4 (cache rules): ``CacheRules.ttl_semantics_known`` and the GitHub Copilot
row of ``RulesTable`` (unknown TTL semantics, context tier salted into the messages tier)."""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import cache_rules as cr
from tokenbill.core.protocols import CacheRulesProvider

TABLE = cr.RulesTable()
ANTHROPIC_TIERS = (
    ("tools", ("model", "tool_defs")),
    ("system", ("speed", "web_search_enabled", "citations_enabled")),
    ("messages", ("tool_choice", "disable_parallel_tool_use", "has_images", "thinking", "effort",
                  "output_format")),
)


def test_ttl_semantics_known_is_an_appended_defaulted_field() -> None:
    names = [f.name for f in dataclasses.fields(cr.CacheRules)]
    assert names[-1] == "ttl_semantics_known"
    assert names[:-1] == ["provider", "channel", "ttl_options_s", "ttl_measured_from",
                          "refresh_on_read", "visible_from", "lookback_positions",
                          "collapse_tool_runs", "max_breakpoints", "scope", "tier_params",
                          "effort_invalidates_all_tiers_models", "sources"]
    row = cr.CacheRules("p", "c", (), "request_start", True, "response_end", None, False, 4,
                        "organization", (), (), ("x",))
    assert row.ttl_semantics_known is True
    assert not hasattr(row, "__dict__")                       # still a slots dataclass
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.ttl_semantics_known = False  # type: ignore[misc]


@pytest.mark.parametrize("provider", ["github", "anthropic", "openai", "other"])
@pytest.mark.parametrize("model", ["claude-opus-5-5", "gpt-5.6-sol", "gemini-3-pro", ""])
def test_the_copilot_row(provider: str, model: str) -> None:
    rules = TABLE.rules_for(provider, "github_copilot", model)
    assert rules.provider == "github" and rules.channel == "github_copilot"
    assert rules.ttl_options_s == ()
    assert rules.ttl_measured_from == "request_start"
    assert rules.refresh_on_read is True
    assert rules.visible_from == "response_end"
    assert rules.lookback_positions is None
    assert rules.collapse_tool_runs is False
    assert rules.max_breakpoints == 4
    assert rules.scope == "organization"
    assert rules.tier_params == (
        ANTHROPIC_TIERS[0], ANTHROPIC_TIERS[1],
        ("messages", (*ANTHROPIC_TIERS[2][1], "context_tier")),
    )
    assert rules.effort_invalidates_all_tiers_models == ()
    assert rules.sources == (
        "https://docs.github.com/en/copilot/tutorials/optimize-ai-usage",
        "https://github.com/microsoft/vscode/blob/main/extensions/copilot/src/platform/"
        "networking/common/anthropic.ts",
    )
    assert rules.ttl_semantics_known is False


def test_context_tier_only_in_the_messages_tier() -> None:
    tiers = dict(TABLE.rules_for("github", "github_copilot", "claude-opus-5-5").tier_params)
    assert [t for t, params in tiers.items() if "context_tier" in params] == ["messages"]
    assert dict(TABLE.rules_for("anthropic", "anthropic_api", "claude-opus-5-5").tier_params) == \
        dict(ANTHROPIC_TIERS)


def test_channels_are_unchanged() -> None:
    assert TABLE.channels() == ("anthropic_api", "claude_platform_aws", "foundry", "bedrock",
                                "vertex", "openai_api", "azure_openai")
    assert "github_copilot" not in TABLE.channels()
    assert isinstance(TABLE, CacheRulesProvider)


@pytest.mark.parametrize(("provider", "channel", "model"), [
    ("anthropic", "anthropic_api", "claude-opus-5-5"),
    ("anthropic", "claude_platform_aws", "claude-opus-5-5"),
    ("anthropic", "foundry", "claude-sonnet-5"),
    ("anthropic", "bedrock", "claude-sonnet-5"),
    ("anthropic", "vertex", "claude-sonnet-5"),
    ("openai", "openai_api", "gpt-5.6-sol"),
    ("openai", "openai_api", "gpt-5.4"),
    ("openai", "azure_openai", "gpt-5.6"),
    ("anthropic", "gateway-x", "claude-opus-5-5"),
    ("openai", "openrouter", "gpt-5.6"),
    ("github", "github_actions", "x"),
    ("github", "github_sandbox", "x"),
    ("google", "vertex-gemini", "gemini-3"),
])
def test_every_other_row_has_known_semantics(provider: str, channel: str, model: str) -> None:
    rules = TABLE.rules_for(provider, channel, model)
    assert rules.ttl_semantics_known is True
    assert rules.channel == channel


def test_the_copilot_row_is_cached_and_shared() -> None:
    a = TABLE.rules_for("github", "github_copilot", "claude-sonnet-5")
    assert TABLE.rules_for("github", "github_copilot", "claude-sonnet-5") is a
    assert cr.RulesTable().rules_for("anthropic", "github_copilot", "x") == a
    assert cr.COPILOT_CACHE_CHANNEL == "github_copilot"


def test_effort_changes_invalidate_on_copilot() -> None:
    # the per-message effort beta is not available on GitHub's proxy (D28 (b) channels), so a
    # Copilot agent's effort change breaks the messages cache
    for product in ("copilot_cli", "copilot_vscode", None):
        for model in cr.PER_MESSAGE_EFFORT_MODELS:
            assert cr.effort_change_keeps_cache(
                agent_product=product, model=model, channel="github_copilot",
                client_version=None, betas=(cr.PER_MESSAGE_EFFORT_BETA,)) is False


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=30), st.text(max_size=60))
def test_any_provider_and_model_on_copilot_get_the_copilot_row(provider: str,
                                                               model: str) -> None:
    rules = TABLE.rules_for(provider, "github_copilot", model)
    assert rules.provider == "github" and rules.ttl_semantics_known is False
    assert rules.ttl_options_s == ()
