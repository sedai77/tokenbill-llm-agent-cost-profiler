"""Tier salts and the invalidation hierarchy (SPEC §9.7 #1, §19.3, D28, Appendix A.13)."""

from __future__ import annotations

import dataclasses

import pytest

from tests.v2.blocksim.fp import fingerprint
from tests.v2.blocksim.helpers import blk, context, lane, req, size, system, tool, usage
from tokenbill.core.cache_rules import (
    PER_MESSAGE_EFFORT_BETA,
    RulesTable,
    effort_change_keeps_cache,
)
from tokenbill.core.records import Request
from tokenbill.core.testing import FakePricer
from tokenbill.core.transitions import classify_transitions
from tokenbill.detect.block import BlockBreakers
from tokenbill.sim.block_replay import BlockReplayer, chain_hashes, first_divergence, salt_diff

TOOLS = (tool("t1"), tool("t2"))
SYS = (system("sys"),)
MSGS = (blk("m0"), blk("m1"))
BLOCKS = (*TOOLS, *SYS, *MSGS)          # tools 0-1, system 2, messages 3-4


def _req(seq: int, *, blocks=BLOCKS, model: str = "claude-opus-5-5", params=None,
         attribution=None, **ctx) -> Request:
    return req("L", seq, 30 * seq, blocks, usage(w5=4000), model=model, params=params,
               attribution=attribution, **ctx)


def _tiers(a: Request, b: Request) -> tuple[bool, bool, bool]:
    """Whether the tools / system / messages chain hashes of two requests are all equal."""
    ha, hb = chain_hashes(a), chain_hashes(b)
    return (ha[:2] == hb[:2], ha[2:3] == hb[2:3], ha[3:] == hb[3:])


def test_identical_requests_share_every_tier() -> None:
    assert _tiers(_req(0), _req(1)) == (True, True, True)
    assert first_divergence(_req(0), _req(1)) is None


def test_effort_change_on_sdk_lane_without_beta_invalidates_only_messages() -> None:
    sdk = {"agent_product": "agent_sdk"}
    a = _req(0, params={"effort": "high"}, attribution=sdk)
    b = _req(1, params={"effort": "low"}, attribution=sdk)
    assert _tiers(a, b) == (True, True, False)
    assert first_divergence(a, b) == ("messages", 3, "param")
    assert salt_diff(a, b) == (("messages", "effort"),)


def test_effort_change_on_claude_code_opus_5_5_invalidates_nothing() -> None:
    cc = {"agent_product": "claude_code", "client_version": "2.1.270"}
    a = _req(0, params={"effort": "high"}, attribution=cc)
    b = _req(1, params={"effort": "low"}, attribution=cc)
    assert _tiers(a, b) == (True, True, True)
    assert first_divergence(a, b) is None
    assert salt_diff(a, b) == ()


def test_thinking_change_follows_the_same_exemption() -> None:
    sdk = {"agent_product": "agent_sdk"}
    a = _req(0, params={"thinking": "adaptive"}, attribution=sdk)
    b = _req(1, params={"thinking": "off"}, attribution=sdk)
    assert _tiers(a, b) == (True, True, False)
    cc = {"agent_product": "claude_code"}
    a = _req(0, params={"thinking": "adaptive"}, attribution=cc)
    b = _req(1, params={"thinking": "off"}, attribution=cc)
    assert _tiers(a, b) == (True, True, True)


# SPEC Appendix A.13 rows: (agent_product, model, channel, client_version, betas, keeps cache)
A13 = [
    ("claude_code", "claude-opus-5-5", "anthropic_api", "2.1.270", (), True),
    ("claude_code", "claude-opus-5-5", "bedrock", "2.1.270", (), False),
    ("claude_code", "claude-opus-5", "anthropic_api", "2.1.270", (), False),
    ("claude_code", "claude-fable-5-1", "anthropic_api", "2.1.250", (), False),
    ("agent_sdk", "claude-opus-5-5", "anthropic_api", None, (), False),
    ("agent_sdk", "claude-opus-5", "anthropic_api", None, (PER_MESSAGE_EFFORT_BETA,), True),
    ("agent_sdk", "claude-sonnet-5", "anthropic_api", None, (PER_MESSAGE_EFFORT_BETA,), False),
]


@pytest.mark.parametrize("product,model,channel,version,betas,keeps", A13)
def test_appendix_a13_rows(product, model, channel, version, betas, keeps) -> None:
    assert effort_change_keeps_cache(agent_product=product, model=model, channel=channel,
                                     client_version=version, betas=betas) is keeps
    attribution = {"agent_product": product, "client_version": version}
    a = _req(0, model=model, channel=channel, attribution=attribution,
             params={"effort": "high", "betas": betas})
    b = _req(1, model=model, channel=channel, attribution=attribution,
             params={"effort": "medium", "betas": betas})
    tools_eq, system_eq, messages_eq = _tiers(a, b)
    assert tools_eq and system_eq
    assert messages_eq is keeps
    assert (first_divergence(a, b) is None) is keeps


@pytest.mark.parametrize("prev_kw,cur_kw", [
    # Claude Code upgraded mid-session past 2.1.260 on Opus 5.5: the exemption turns on
    ({"attribution": {"agent_product": "claude_code", "client_version": "2.1.250"}},
     {"attribution": {"agent_product": "claude_code", "client_version": "2.1.270"}}),
    # ... and the reverse (a downgrade)
    ({"attribution": {"agent_product": "claude_code", "client_version": "2.1.270"}},
     {"attribution": {"agent_product": "claude_code", "client_version": "2.1.250"}}),
    # an SDK lane starts sending the per-message effort beta on Opus 5
    ({"attribution": {"agent_product": "agent_sdk"}, "model": "claude-opus-5"},
     {"attribution": {"agent_product": "agent_sdk"}, "model": "claude-opus-5",
      "betas": (PER_MESSAGE_EFFORT_BETA,)}),
])
def test_an_exemption_flip_with_unchanged_effort_is_no_change(prev_kw, cur_kw) -> None:
    """``core.transitions`` evaluates ``effort_change_keeps_cache`` for the later request only,
    so a flip of the exemption itself with the same effort (and thinking unreported on both) is
    not a parameter change; the block engine compares both salts under that same exemption."""
    def make(seq, kw, blocks):
        kw = dict(kw)
        params = {"effort": "high", "betas": kw.pop("betas", ())}
        return req("FL", seq, 30 * seq, blocks, usage(w5=size(blocks)), params=params, **kw)

    a = make(0, prev_kw, (*SYS, MSGS[0]))
    b = make(1, cur_kw, (*SYS, *MSGS))
    assert first_divergence(a, b) is None
    assert salt_diff(a, b) == ()
    ln = lane([a, b])
    t = classify_transitions(ln, pricer=FakePricer(), rules=RulesTable())[0]
    assert t.sub_cause != "effort-change"
    assert BlockBreakers().detect([ln], context(min_usd="0")) == []
    # the chain carries on: request 1 reads request 0's entry
    outs = BlockReplayer().predict([ln], pricer=FakePricer())
    assert outs[1].usage.cache_read == size((*SYS, MSGS[0]))


def test_an_effort_change_is_judged_by_the_later_requests_exemption() -> None:
    """High → low while the exemption turns on: no change (the later request is exempt); low →
    high while it turns off: a messages-tier change — exactly core.transitions' effort-change."""
    old = {"agent_product": "claude_code", "client_version": "2.1.250"}
    new = {"agent_product": "claude_code", "client_version": "2.1.270"}
    a = _req(0, params={"effort": "high"}, attribution=old)
    b = _req(1, params={"effort": "low"}, attribution=new)
    assert first_divergence(a, b) is None
    c = _req(2, params={"effort": "high"}, attribution=old)
    assert first_divergence(b, c) == ("messages", 3, "param")
    assert salt_diff(b, c) == (("messages", "effort"),)


def test_tool_definition_change_invalidates_every_tier() -> None:
    a = _req(0)
    b = _req(1, blocks=(tool("t1"), tool("t2-changed"), *SYS, *MSGS))
    ha, hb = chain_hashes(a), chain_hashes(b)
    assert ha[0] == hb[0]
    assert all(x != y for x, y in zip(ha[1:], hb[1:], strict=True))
    assert first_divergence(a, b) == ("tools", 1, "tool-definition")


def test_model_change_invalidates_every_tier() -> None:
    a, b = _req(0), _req(1, model="claude-opus-5")
    assert all(x != y for x, y in zip(chain_hashes(a), chain_hashes(b), strict=True))
    assert first_divergence(a, b) == ("tools", 0, "model")


def test_speed_toggle_invalidates_system_and_messages() -> None:
    a = _req(0, speed="standard")
    b = _req(1, speed="fast")
    assert _tiers(a, b) == (True, False, False)
    assert first_divergence(a, b) == ("system", 2, "param")
    assert salt_diff(a, b) == (("system", "speed"),)


@pytest.mark.parametrize("name,first,second", [
    ("web_search_enabled", True, False), ("citations_enabled", False, True)])
def test_system_tier_params(name, first, second) -> None:
    a, b = _req(0, params={name: first}), _req(1, params={name: second})
    assert _tiers(a, b) == (True, False, False)


@pytest.mark.parametrize("name,first,second", [
    ("tool_choice", "auto", "any"), ("disable_parallel_tool_use", False, True),
    ("has_images", False, True), ("output_format", "set", "h_other")])
def test_messages_tier_params(name, first, second) -> None:
    a, b = _req(0, params={name: first}), _req(1, params={name: second})
    assert _tiers(a, b) == (True, True, False)
    assert first_divergence(a, b) == ("messages", 3, "param")


def test_an_unreported_parameter_is_never_a_change() -> None:
    sdk = {"agent_product": "agent_sdk"}
    a = _req(0, params={"effort": "high"}, attribution=sdk)
    b = _req(1, params={"effort": None}, attribution=sdk)
    assert first_divergence(a, b) is None
    assert salt_diff(a, b) == ()


def test_salt_change_in_an_empty_tier_changes_nothing() -> None:
    blocks = (*TOOLS, *SYS)                                 # no messages
    a = _req(0, blocks=blocks, params={"tool_choice": "auto"})
    b = _req(1, blocks=blocks, params={"tool_choice": "any"})
    assert chain_hashes(a) == chain_hashes(b)
    assert first_divergence(a, b) is None


def test_models_listed_as_invalidating_all_tiers_salt_tools_and_system() -> None:
    table = RulesTable()
    base = table.rules_for("anthropic", "anthropic_api", "claude-opus-5-5")
    rules = dataclasses.replace(base, effort_invalidates_all_tiers_models=("claude-opus-5-5",))
    sdk = {"agent_product": "agent_sdk"}
    a = _req(0, params={"effort": "high"}, attribution=sdk)
    b = _req(1, params={"effort": "low"}, attribution=sdk)
    ha, hb = chain_hashes(a, rules=rules), chain_hashes(b, rules=rules)
    assert all(x != y for x, y in zip(ha, hb, strict=True))
    assert first_divergence(a, b, rules=rules) == ("tools", 0, "param")


def test_moving_a_cache_control_marker_changes_no_hash() -> None:
    msgs = [{"role": "user", "content": [{"type": "text", "text": f"turn number {i}"}]}
            for i in range(6)]
    with_marker = [dict(m, content=[dict(m["content"][0])]) for m in msgs]
    with_marker[3]["content"][0]["cache_control"] = {"type": "ephemeral"}
    moved = [dict(m, content=[dict(m["content"][0])]) for m in msgs]
    moved[4]["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    fa, bpa = fingerprint([], "system text", with_marker)
    fb, bpb = fingerprint([], "system text", moved)
    assert [b.h for b in fa.blocks] == [b.h for b in fb.blocks]
    assert (bpa[0].block_index, bpa[0].ttl) == (4, "5m")
    assert (bpb[0].block_index, bpb[0].ttl) == (5, "1h")


def test_chain_hashes_empty_without_fingerprint() -> None:
    a = dataclasses.replace(_req(0), fingerprint=None)
    assert chain_hashes(a) == ()
    assert first_divergence(a, _req(1)) is None
