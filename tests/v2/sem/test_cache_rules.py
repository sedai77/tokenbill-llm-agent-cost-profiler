"""SPEC §3.14 and Appendix A.13: the cache-rule table and the D28 effort exemption."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import cache_rules as cr
from tokenbill.core.errors import UsageError
from tokenbill.core.protocols import CacheRulesProvider

BETA = "mid-conversation-output-config-2026-07-01"

# SPEC Appendix A.13, verbatim: (agent_product, model, channel, client_version, betas, keeps)
A13 = [
    ("claude_code", "claude-opus-5-5", "anthropic_api", "2.1.270", (), True),
    ("claude_code", "claude-opus-5-5", "bedrock", "2.1.270", (), False),
    ("claude_code", "claude-opus-5", "anthropic_api", "2.1.270", (), False),
    ("claude_code", "claude-fable-5-1", "anthropic_api", "2.1.250", (), False),
    ("agent_sdk", "claude-opus-5-5", "anthropic_api", None, (), False),
    ("agent_sdk", "claude-opus-5", "anthropic_api", None, (BETA,), True),
    ("agent_sdk", "claude-sonnet-5", "anthropic_api", None, (BETA,), False),
]


@pytest.mark.parametrize(("product", "model", "channel", "version", "betas", "keeps"), A13)
def test_appendix_a13(product: str, model: str, channel: str, version: str | None,
                      betas: tuple[str, ...], keeps: bool) -> None:
    assert cr.effort_change_keeps_cache(agent_product=product, model=model, channel=channel,
                                        client_version=version, betas=betas) is keeps


def keeps(**kw: object) -> bool:
    base: dict[str, object] = {"agent_product": "claude_code", "model": "claude-opus-5-5",
                               "channel": "anthropic_api", "client_version": None, "betas": ()}
    base.update(kw)
    return cr.effort_change_keeps_cache(**base)  # type: ignore[arg-type]


def test_claude_code_branch_details() -> None:
    assert keeps()                                           # unknown version counts as recent
    assert keeps(model="claude-fable-5-1", client_version="2.1.260")
    assert not keeps(model="claude-fable-5-1", client_version="2.1.259")
    assert keeps(client_version="2.1.1000")                  # numeric, not lexicographic
    assert not keeps(client_version="2.1.26")                # "2.1.26" < "2.1.260" numerically
    assert keeps(client_version="2.2")                        # missing components count as 0
    assert keeps(client_version="v2.1.270 (Claude Code)")
    assert keeps(client_version="unknown")                   # no leading digits → unknown
    assert not keeps(channel="vertex")
    assert keeps(channel="foundry") and keeps(channel="claude_platform_aws")
    assert keeps(model="claude-opus-5-5[1m]")                # normalized before comparison
    assert keeps(model="claude-opus-5-5-20260922")
    assert not keeps(agent_product="agent_sdk")
    assert not keeps(agent_product=None)


def test_per_message_beta_branch_details() -> None:
    for model in cr.PER_MESSAGE_EFFORT_MODELS:
        assert keeps(agent_product="api", model=model, betas=(BETA,))
        assert not keeps(agent_product="api", model=model, betas=())
    assert keeps(agent_product="api", model="claude-mythos-5-1", betas=("x", BETA),
                 channel="vertex")
    assert keeps(agent_product="api", model="claude-opus-5", betas=(BETA,),
                 channel="claude_platform_aws")
    # primary source: the beta is not available on Bedrock or Microsoft Foundry
    assert not keeps(agent_product="api", model="claude-opus-5", betas=(BETA,), channel="bedrock")
    assert not keeps(agent_product="api", model="claude-opus-5", betas=(BETA,), channel="foundry")
    assert not keeps(agent_product="api", model="claude-fable-5", betas=(BETA,))
    # Claude Code on a model outside its own list can still use the beta
    assert keeps(model="claude-opus-5", betas=(BETA,))


def test_constants_match_the_spec() -> None:
    assert cr.EFFORT_KEEPS_CACHE_CLAUDE_CODE == ("claude-opus-5-5", "claude-fable-5-1")
    assert cr.PER_MESSAGE_EFFORT_BETA == BETA
    assert cr.PER_MESSAGE_EFFORT_MODELS == ("claude-fable-5-1", "claude-mythos-5-1",
                                            "claude-opus-5", "claude-opus-5-5")


def test_version_helpers() -> None:
    assert cr.parse_version("2.1.270") == (2, 1, 270)
    assert cr.parse_version(" v3 ") == (3,)
    assert cr.parse_version("beta") is None and cr.parse_version(None) is None
    assert cr.version_at_least("2.1.260", "2.1.260")
    assert not cr.version_at_least("2.0.999", "2.1")
    with pytest.raises(UsageError):
        cr.version_at_least("x", "2.1")


@given(st.tuples(st.integers(0, 999), st.integers(0, 999), st.integers(0, 999)),
       st.tuples(st.integers(0, 999), st.integers(0, 999), st.integers(0, 999)))
def test_version_comparison_is_numeric(a: tuple[int, int, int], b: tuple[int, int, int]) -> None:
    sa, sb = ".".join(map(str, a)), ".".join(map(str, b))
    assert cr.version_at_least(sa, sb) is (a >= b)


# ---------------------------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------------------------

TABLE = cr.RulesTable()


def test_rules_table_is_a_cache_rules_provider() -> None:
    assert isinstance(TABLE, CacheRulesProvider)
    assert set(TABLE.channels()) == {"anthropic_api", "claude_platform_aws", "foundry", "bedrock",
                                     "vertex", "openai_api", "azure_openai"}


@pytest.mark.parametrize(("channel", "scope"), [
    ("anthropic_api", "workspace"),
    ("claude_platform_aws", "workspace"),
    ("foundry", "workspace"),
    ("bedrock", "organization"),
    ("vertex", "organization"),
])
def test_anthropic_rows(channel: str, scope: str) -> None:
    rules = TABLE.rules_for("anthropic", channel, "claude-opus-5-5")
    assert rules.provider == "anthropic" and rules.channel == channel
    assert rules.scope == scope
    assert rules.ttl_options_s == (300, 3600)
    assert rules.ttl_measured_from == "request_start"
    assert rules.refresh_on_read is True
    assert rules.visible_from == "first_token"
    assert rules.lookback_positions == 20
    assert rules.collapse_tool_runs is (channel == "anthropic_api")
    assert rules.max_breakpoints == 4
    assert rules.tier_params == (
        ("tools", ("model", "tool_defs")),
        ("system", ("speed", "web_search_enabled", "citations_enabled")),
        ("messages", ("tool_choice", "disable_parallel_tool_use", "has_images", "thinking",
                      "effort", "output_format")),
    )
    assert rules.effort_invalidates_all_tiers_models == ()
    assert rules.sources and all(s.startswith("https://") for s in rules.sources)


def test_openai_row() -> None:
    rules = TABLE.rules_for("openai", "openai_api", "gpt-5.6-sol")
    assert rules.scope == "organization"
    assert rules.ttl_options_s == (1800,)
    assert rules.ttl_measured_from == "last_use"
    assert rules.visible_from == "response_end"
    assert rules.lookback_positions is None and rules.collapse_tool_runs is False
    assert rules.max_breakpoints == 4
    assert TABLE.rules_for("openai", "openai_api", "gpt-5.7").ttl_options_s == (1800,)
    assert TABLE.rules_for("openai", "openai_api", "gpt-6").ttl_options_s == (1800,)
    assert TABLE.rules_for("openai", "openai_api", "gpt-5.4").ttl_options_s == ()
    assert TABLE.rules_for("openai", "openai_api", "o3").ttl_options_s == ()


def test_azure_scope_is_subscription() -> None:
    rules = TABLE.rules_for("openai", "azure_openai", "gpt-5.6")
    assert rules.scope == "subscription"
    assert rules.ttl_options_s == ()    # Azure's 5.6+ TTL is unverified
    assert any("learn.microsoft.com" in s for s in rules.sources)


def test_fallback_rows() -> None:
    anthropic = TABLE.rules_for("anthropic", "gateway-x", "claude-opus-5-5")
    assert anthropic.channel == "gateway-x" and anthropic.scope == "workspace"
    assert anthropic.ttl_options_s == (300, 3600) and anthropic.collapse_tool_runs is False
    openai = TABLE.rules_for("openai", "openrouter", "gpt-5.6")
    assert openai.channel == "openrouter" and openai.ttl_options_s == (1800,)
    other = TABLE.rules_for("google", "vertex-gemini", "gemini-3")
    assert other.provider == "google" and other.ttl_options_s == () and \
        other.scope == "organization"


def test_rules_for_is_cached_and_deterministic() -> None:
    a = TABLE.rules_for("anthropic", "anthropic_api", "claude-sonnet-5")
    assert TABLE.rules_for("anthropic", "anthropic_api", "claude-sonnet-5") is a
    assert cr.RulesTable().rules_for("anthropic", "anthropic_api", "claude-sonnet-5") == a


# ---------------------------------------------------------------------------------------------
# hostile inputs (client versions come from transcripts, model ids from provider payloads)
# ---------------------------------------------------------------------------------------------


def test_huge_digit_runs_are_unknown_not_a_crash() -> None:
    # int() of more than sys.get_int_max_str_digits() digits raises ValueError on 3.10.7+
    huge = "2." + "1" * 5000
    assert cr.parse_version(huge) is None
    assert cr.parse_version("1" * 18) == (10**18 // 9,)          # 18 digits still parse
    assert cr.parse_version("1" * 19) is None
    assert keeps(client_version=huge)                              # unknown version → recent
    with pytest.raises(UsageError):
        cr.version_at_least(huge, "2.1.260")
    rules = TABLE.rules_for("openai", "openai_api", "gpt-" + "9" * 5000)
    assert rules.ttl_options_s == ()
    assert TABLE.rules_for("openai", "openai_api", "gpt-5.6" + "0" * 10).ttl_options_s == ()


def test_betas_match_by_exact_value() -> None:
    header = f"interleaved-thinking-2025-05-14, {BETA}"
    assert keeps(agent_product="api", model="claude-opus-5", betas=header)
    assert not keeps(agent_product="api", model="claude-opus-5", betas=f"x{BETA}y")
    assert not keeps(agent_product="api", model="claude-opus-5", betas=(f"x{BETA}y",))
    assert not keeps(agent_product="api", model="claude-opus-5", betas=None)
    assert keeps(betas=None)                         # the Claude Code branch needs no beta


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.none(), st.text(max_size=40),
                 st.text("0123456789.v ", max_size=6000)),
       st.text(max_size=40), st.sampled_from(["anthropic_api", "vertex", "bedrock", "x"]))
def test_fuzz_effort_predicate_and_rules_never_crash(version: str | None, model: str,
                                                     channel: str) -> None:
    for product in ("claude_code", "agent_sdk", None):
        result = cr.effort_change_keeps_cache(agent_product=product, model=model,
                                              channel=channel, client_version=version,
                                              betas=(BETA,))
        assert type(result) is bool
    for provider in ("anthropic", "openai", "other"):
        assert TABLE.rules_for(provider, channel, model).channel == channel
        assert TABLE.rules_for(provider, "openai_api", "gpt-" + (version or "")).provider
