"""core.catalog: lever catalog, settings allowlist, lifecycle, SKU rules (SPEC §3.20, §11.1, §11.4,
§6.7; F-KIT acceptance). The parse-with-``core.policy`` check is a gate-F test
(``test_gate_f.py``)."""

from __future__ import annotations

import dataclasses
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import catalog
from tokenbill.core.errors import UsageError
from tokenbill.core.facts import load as load_facts

#: SPEC §11.1, in table order.
SPEC_LEVERS = (
    "cc.prompt_cache_ttl.main", "cc.prompt_cache_ttl.subagent", "sdk.ttl", "sdk.keepalive",
    "cc.autocompact_window", "cc.compact_on_resume", "cc.cold_resume_hook", "cc.default_model",
    "cc.default_effort", "cc.subagent_model", "model.same_tier_upgrade", "cc.max_effort",
    "cc.fast_mode_opt_in", "geo.global", "endpoint.global", "batch.eligible", "fanout.stagger",
    "retry.single_owner", "fallback.credit", "gateway.restore_caching", "ci.shared_prefix",
    "blocks.breakpoints", "cc.tool_search", "sdk.defer_loading",
)
#: needs_eval / tradeoff column of SPEC §11.1.
TRADEOFFS = {"cc.autocompact_window", "cc.compact_on_resume", "cc.default_model",
             "cc.default_effort", "cc.subagent_model", "model.same_tier_upgrade", "cc.max_effort"}
_CLAUSE = re.compile(
    r"(ttl=(5m|1h)|keepalive=\d+s,max=\d+s|compact-window=\d+(,post=\d+)?"
    r"|cold-resume=(compact|clear)(,min=\d+)?|model=[a-z0-9.\-]+|effort=[a-z]+(,scale=[0-9.]+)?"
    r"|fast=off|geo=global|regional=global|batch=eligible|repair=[a-z_]+"
    r"|breakpoints=[a-z_0-9]+)(@[a-z_]+:[a-z0-9_.\-]+(,[a-z_]+:[a-z0-9_.\-]+)*)?\Z")


def test_levers_are_the_spec_table_in_order() -> None:
    assert tuple(lv.lever_id for lv in catalog.LEVERS) == SPEC_LEVERS
    for lv in catalog.LEVERS:
        assert lv.lever_class in catalog.LEVER_CLASSES
        assert lv.replay in ("usage", "block", "none")
        assert (lv.needs_eval, lv.tradeoff) == ((True, True) if lv.lever_id in TRADEOFFS
                                                 else (False, False))
        if lv.lever_class == "trajectory":
            assert lv.upper_bound and lv.needs_eval  # SPEC §9.1 #4
        if lv.replay == "none":
            assert lv.grid == ()
        else:
            assert lv.grid, lv.lever_id
    assert catalog.lever("cc.cold_resume_hook").lever_class == "behavioral"
    assert catalog.lever("blocks.breakpoints").replay == "block"
    assert catalog.lever("fanout.stagger").upper_bound
    assert catalog.lever("ci.shared_prefix").selector == "workload:ci"


def test_grid_strings_follow_the_clause_grammar() -> None:
    for lv in catalog.LEVERS:
        for spec in lv.grid:
            for clause in spec.split(";"):
                assert _CLAUSE.match(clause), (lv.lever_id, clause)
            selectors = [c.partition("@")[2] for c in spec.split(";") if "@" in c]
            for sel in selectors:
                terms = sel.split(",")
                assert terms == sorted(terms, key=lambda t: t.split(":")[0])
    assert catalog.lever("cc.prompt_cache_ttl.main").grid == (
        "ttl=5m@agent_product:claude_code,lane_kind:main",
        "ttl=1h@agent_product:claude_code,lane_kind:main")
    assert catalog.lever("cc.autocompact_window").grid == tuple(
        f"compact-window={w}" for w in (200000, 300000, 400000, 500000, 700000))
    assert catalog.lever("model.same_tier_upgrade").grid == (
        "model=claude-fable-5-1@model:claude-fable-5;model=claude-mythos-5-1@model:claude-mythos-5;"
        "model=claude-opus-5-5@model:claude-opus-5",)


def test_patch_keys_are_allowlisted() -> None:
    for lv in catalog.LEVERS:
        for key in lv.patch_keys:
            assert key in catalog.ALLOWLIST, (lv.lever_id, key)
    assert catalog.lever("cc.autocompact_window").patch_keys == (
        "autoCompactWindow", "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW")


def test_lever_lookup() -> None:
    with pytest.raises(UsageError):
        catalog.lever("no.such.lever")
    ttl = {lv.lever_id for lv in catalog.levers_for_kind("ttl-expiry")}
    assert ttl == {"cc.prompt_cache_ttl.main", "cc.prompt_cache_ttl.subagent", "sdk.ttl"}
    assert [lv.lever_id for lv in catalog.levers_for_kind("cold-resume")] == [
        "cc.prompt_cache_ttl.main", "cc.compact_on_resume", "cc.cold_resume_hook"]
    assert catalog.levers_for_kind("context-shrank") == ()


def test_allowlist_mirrors_facts() -> None:
    facts = load_facts().settings_keys
    assert list(catalog.ALLOWLIST) == list(facts)
    assert len(catalog.ALLOWLIST) == 31
    for key, entry in catalog.ALLOWLIST.items():
        fact = facts[key]
        assert (entry.key, entry.target, entry.domain, entry.min_version, entry.verified,
                entry.lever_id, entry.tradeoff, entry.source) == (
            key, fact.target, fact.domain, fact.min_version, fact.verified, fact.lever_id,
            fact.tradeoff, fact.source)
    assert catalog.allowed("maxEffortLevel").min_version == "2.1.267"
    assert catalog.allowed("promptCacheTtl").verified  # verified by the wave-0 facts task
    with pytest.raises(UsageError):
        catalog.allowed("dangerouslySkipPermissions")
    with pytest.raises(TypeError):
        catalog.ALLOWLIST["x"] = catalog.allowed("model")  # type: ignore[index]


def test_successors_are_same_tier_same_tokenizer_only() -> None:
    assert catalog.successor("claude-opus-4-8") is None
    assert catalog.successor("claude-sonnet-4-6") is None
    assert catalog.successor("claude-opus-5") == "claude-opus-5-5"
    assert catalog.successor("claude-fable-5") == "claude-fable-5-1"
    assert catalog.successor("claude-mythos-5") == "claude-mythos-5-1"
    rows = {r.model: r.tokenizer_family for r in load_facts().rate_rows}
    for old, new in catalog.SUCCESSORS.items():
        if old in rows and new in rows:
            assert rows[old] == rows[new]


def test_retirements_and_announcements() -> None:
    assert catalog.RETIREMENTS["claude-sonnet-4-5"] == "2026-09-29"
    assert catalog.RETIREMENTS["claude-haiku-4-5"] == "2026-10-15"
    assert catalog.RETIREMENTS["claude-opus-4-5"] == "2026-11-24"
    assert catalog.RETIREMENTS["claude-opus-4-1"] == "2026-08-05"  # already retired
    sept_23 = (catalog._dt.date(2026, 9, 23) - catalog._EPOCH).days * 86_400_000
    assert catalog.retiring_within("claude-haiku-4-5", sept_23 - 365 * 86_400_000, 30) is None
    assert catalog.retiring_within("claude-haiku-4-5", sept_23, 30) == "2026-10-15"
    assert catalog.retiring_within("claude-haiku-4-5", sept_23, 21) is None
    assert catalog.retiring_within("claude-opus-4-1", sept_23, 0) == "2026-08-05"
    assert catalog.retiring_within("claude-opus-5-5", sept_23, 30) is None
    assert catalog.retiring_within("not-a-model", sept_23, 9999) is None
    assert catalog.ANNOUNCED_UNPRICED == ("claude-haiku-5-5", "claude-sonnet-5-5")


@settings(max_examples=200, deadline=None)
@given(ts_ms=st.integers(-(2**80), 2**80), days=st.integers(-(10**12), 10**12))
def test_retiring_within_never_overflows(ts_ms: int, days: int) -> None:
    got = catalog.retiring_within("claude-haiku-4-5", ts_ms, days)
    horizon = ts_ms // 86_400_000 + days
    floor = (catalog._dt.date(2026, 10, 15) - catalog._EPOCH).days
    assert got == ("2026-10-15" if floor <= horizon else None)


def test_promotions() -> None:
    promo = catalog.promotion_for("gpt-5.6-sol", "openai_api", "2026-10-01")
    assert promo is not None and promo.promotion_id == "openai.gpt-5.6-sol.2026-08"
    assert (promo.start, promo.not_before_end) == ("2026-08-21", "2026-11-21")
    assert catalog.promotion_for("gpt-5.6-sol", "openai_api", "2026-11-21") == promo
    assert catalog.promotion_for("gpt-5.6-sol", "openai_api", "2026-11-22") is None
    assert catalog.promotion_for("gpt-5.6-sol", "openai_api", "2026-08-20") is None
    assert catalog.promotion_for("gpt-5.6-sol", "azure_openai", "2026-10-01") is None
    assert catalog.PROMOTIONS == (promo,)
    rows = [r for r in load_facts().rate_rows if r.promotion is not None]
    assert {r.promotion for r in rows} <= {p.promotion_id for p in catalog.PROMOTIONS}


def test_map_sku_ignores_unverified_rules() -> None:
    assert catalog.SKU_RULES and not any(r.verified for r in catalog.SKU_RULES)
    sku = "USE1-MP:USE1_InputTokenCount_Global-Units"
    assert any(re.fullmatch(r.pattern, sku) for r in catalog.SKU_RULES)
    assert catalog.map_sku("aws.cur2", sku) is None
    assert catalog.map_sku("aws.cur2", None) is None  # type: ignore[arg-type]


def test_map_sku_uses_the_first_verified_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    verified = tuple(dataclasses.replace(r, verified=True) for r in catalog.SKU_RULES)
    monkeypatch.setattr(catalog, "SKU_RULES", verified)
    rule = catalog.map_sku("aws.cur2", "USE1-MP:USE1_InputTokenCount_Global-Units")
    assert rule is not None and (rule.bucket, rule.endpoint_scope) == ("uncached_input", "global")
    regional = catalog.map_sku("aws.cur2", "USE1-MP:USE1_CacheWrite1hInputTokenCount-Units")
    assert regional is not None and (regional.bucket, regional.endpoint_scope) == (
        "cache_write_1h", "regional")
    assert catalog.map_sku("gcp.billing_export", "USE1-MP:USE1_InputTokenCount-Units") is None
    assert catalog.map_sku("aws.cur2", "prefix USE1-MP:USE1_InputTokenCount-Units") is None


@settings(max_examples=300, deadline=None)
@given(source_kind=st.sampled_from(["aws.cur2", "gcp.billing_export", "x"]), sku=st.text())
def test_fuzz_map_sku(source_kind: str, sku: str) -> None:
    assert catalog.map_sku(source_kind, sku) is None or isinstance(
        catalog.map_sku(source_kind, sku), catalog.SkuRule)
