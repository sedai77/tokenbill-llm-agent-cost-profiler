"""SPEC §3.24 facts.json / facts.py and §3.17 evidence constants."""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import evidence
from tokenbill.core.errors import ContractViolation
from tokenbill.core.facts import FACTS_SCHEMA, META_KEYS, load, parse

FAKEPRICER_ANTHROPIC = (
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-fable-5-1",
    "claude-mythos-5-1",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)
SETTINGS_KEYS_11_4 = (
    "autoCompactWindow",
    "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "env.CLAUDE_CODE_DISABLE_1M_CONTEXT",
    "promptCacheTtl",
    "subagentPromptCacheTtl",
    "env.CLAUDE_CODE_PROMPT_CACHE_TTL",
    "env.CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
    "env.ENABLE_PROMPT_CACHING_1H",
    "env.FORCE_PROMPT_CACHING_5M",
    "model",
    "effortLevel",
    "env.CLAUDE_CODE_SUBAGENT_MODEL",
    "env.ANTHROPIC_DEFAULT_OPUS_MODEL",
    "env.ANTHROPIC_DEFAULT_SONNET_MODEL",
    "env.ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "availableModels",
    "enforceAvailableModels",
    "maxEffortLevel",
    "fastModePerSessionOptIn",
    "env.CLAUDE_CODE_DISABLE_FAST_MODE",
    "modelPricing",
    "bashOutputMaxChars",
    "env.MAX_MCP_OUTPUT_TOKENS",
    "env.ENABLE_TOOL_SEARCH",
    "skillListingBudgetFraction",
    "claudeMdExcludes",
    "cleanupPeriodDays",
    "env.CLAUDE_CODE_ENABLE_TELEMETRY",
    "env.OTEL_RESOURCE_ATTRIBUTES",
    "env.OTEL_METRICS_INCLUDE_ENTRYPOINT",
    "hooks.SessionStart",
)
EVIDENCE_NAMES = (
    "MISS_MIN_TOKENS",
    "MISS_MIN_FRACTION",
    "CACHE_READ_SHARE_MEDIAN",
    "CACHE_READ_SHARE_TOP_DECILE",
    "CACHE_READ_SHARE_INVESTIGATE_BELOW",
    "TTL_RULE_GAP_SHARE_5_60MIN",
    "KEEPALIVE_INTERVAL_S",
    "KEEPALIVE_MAX_IDLE_S",
    "KEEPALIVE_BREAK_EVEN",
    "CC_AUTOCOMPACT_DEFAULT_TOKENS",
    "COMPACTION_SUMMARY_TOKENS_DEFAULT",
    "THINKING_SHARE_PRIOR",
    "TOKENIZER_BAND",
    "BATCH_CACHE_HIT_BAND",
    "CC_FLEET_USD_PER_ACTIVE_DAY",
    "CC_FLEET_USD_PER_ACTIVE_DAY_P90_UNDER",
    "REFUSAL_AMBIGUOUS_MAX_OUTPUT",
    "CPT_DEFAULT_47PLUS_TOOL_OUTPUT",
    "CPT_DEFAULT_LEGACY",
    "COLD_RESUME_MIN_CONTEXT",
    "FEMP_MONTHLY",
    "FEMP_HOURLY_DAILY",
    "MIN_CALIBRATION_PERIODS",
    "MAX_TOKENS_AGENTIC_RECOMMENDED",
    "MAX_TOKENS_AGENTIC_RECOMMENDED_XHIGH",
    "TOOL_DEFS_DEFER_THRESHOLD_TOKENS",
    "TOOL_SEARCH_REDUCTION_BAND",
    "EDIT_PAYBACK_FORMULA",
    "CODE_REVIEW_USD_PER_REVIEW",
    "STALE_PROMPT_OUTPUT_DELTA",
)


def _raw() -> dict:
    text = resources.files("tokenbill.core").joinpath("facts.json").read_text(encoding="utf-8")
    return json.loads(text)


def test_facts_load_and_every_entry_has_metadata() -> None:
    facts = load()
    assert facts is load()  # cached
    assert facts.schema == FACTS_SCHEMA == "tokenbill/facts@1"
    entries = list(facts.entries())
    assert len(entries) > 60
    for section, entry in entries:
        for key in META_KEYS:
            assert isinstance(entry.get(key), str) and entry[key], (section, key)
        assert entry["verification"] in ("primary", "research")
        assert entry["verified_on"] == "2026-09-23"


def test_no_floats_in_facts_json() -> None:
    def walk(value: object) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(_raw())


def test_fakepricer_rate_rows() -> None:
    facts = load()
    for model in FAKEPRICER_ANTHROPIC:
        rows = facts.rows_for(model)
        assert len(rows) == 1 and rows[0].enabled, model
        row = rows[0]
        assert row.provider == "anthropic" and row.channel == "anthropic_api"
        assert row.cache_write_5m_mult == Decimal("1.25") and row.cache_write_1h_mult == 2
        assert dict(row.per_request_usd) == {"web_search": Decimal("0.01")}
        assert row.row_id == f"anthropic/anthropic_api/{model}/{row.effective_from}"
    opus55 = facts.rows_for("claude-opus-5-5")[0]
    assert (opus55.input_usd_per_mtok, opus55.output_usd_per_mtok, opus55.cache_read_mult) == (
        Decimal("4.00"),
        Decimal("20.00"),
        Decimal("0.05"),
    )
    assert opus55.effective_from == "2026-09-22" and opus55.min_cacheable_tokens == 512
    assert "per_message_effort" in opus55.supports and "fast_mode" in opus55.supports
    fable51 = facts.rows_for("claude-fable-5-1")[0]
    assert fable51.effective_from == "2026-09-01" and fable51.cache_read_mult == Decimal("0.025")
    assert facts.rows_for("claude-mythos-5-1")[0].effective_from == "2026-09-01"
    assert facts.rows_for("claude-fable-5")[0].cache_read_mult == Decimal("0.1")
    assert facts.rows_for("claude-sonnet-5")[0].input_usd_per_mtok == Decimal("2.00")
    haiku = facts.rows_for("claude-haiku-4-5")[0]
    assert haiku.min_cacheable_tokens == 4096 and haiku.tokenizer_family == "claude-legacy"
    assert facts.rows_for("claude-opus-4-8")[0].min_cacheable_tokens == 1024
    bedrock = facts.rows_for("claude-opus-5", channel="bedrock")
    assert len(bedrock) == 1 and bedrock[0].input_usd_per_mtok == Decimal("5.00")
    assert facts.rate_row("anthropic/bedrock/claude-opus-5/2026-07-24") is bedrock[0]
    with pytest.raises(KeyError):
        facts.rate_row("nope")


def test_gpt_56_sol_rows() -> None:
    facts = load()
    launch, promo = facts.rows_for("gpt-5.6-sol", channel="openai_api")
    assert (launch.effective_from, launch.effective_to, launch.enabled) == (
        "2026-07-09",
        "2026-08-21",
        False,
    )
    assert (promo.effective_from, promo.effective_to, promo.enabled) == (
        "2026-08-21",
        "2026-11-22",
        True,
    )
    assert promo.promotion == "openai.gpt-5.6-sol.2026-08"
    assert (promo.input_usd_per_mtok, promo.output_usd_per_mtok) == (
        Decimal("4.00"),
        Decimal("20.00"),
    )
    assert promo.cache_read_mult == Decimal("0.1") and promo.cache_write_other_mult == Decimal(
        "1.25"
    )
    assert promo.cache_write_other_ttl_s == 1800 and promo.cache_write_5m_mult is None
    assert promo.long_context_threshold == 272000
    assert dict(promo.long_context_usd_per_mtok) == {
        "input": Decimal("8.00"),
        "cache_read": Decimal("0.80"),
        "cache_write_other": Decimal("10.00"),
        "output": Decimal("30.00"),
    }


def test_modifiers() -> None:
    facts = load()
    ids = {m.modifier_id for m in facts.modifiers}
    assert ids == {
        "anthropic.batch",
        "anthropic.inference_geo.us",
        "anthropic.fast.opus-5-5",
        "anthropic.fast.opus-5",
        "bedrock.endpoint.regional",
    }
    batch = facts.modifier("anthropic.batch")
    assert (
        batch.kind == "multiply" and batch.factor == Decimal("0.5") and batch.applies_to == ("*",)
    )
    assert dict(batch.when)["service_tier"] == "batch"
    geo = facts.modifier("anthropic.inference_geo.us")
    assert geo.factor == Decimal("1.1") and dict(geo.when)["generation_gte"] == "4.6"
    fast = facts.modifier("anthropic.fast.opus-5-5")
    assert fast.kind == "replace_base" and dict(fast.base_usd_per_mtok) == {
        "input": Decimal("8.00"),
        "output": Decimal("40.00"),
    }
    assert dict(facts.modifier("anthropic.fast.opus-5").base_usd_per_mtok)["output"] == Decimal(
        "50.00"
    )
    regional = facts.modifier("bedrock.endpoint.regional")
    assert regional.factor == Decimal("1.1") and dict(regional.when)["endpoint_scope"] == "regional"
    for m in facts.modifiers:
        assert list(m.when) == sorted(m.when)
        assert set(dict(m.when)) <= {
            "service_tier",
            "speed",
            "inference_geo",
            "endpoint_scope",
            "channel_in",
            "model_in",
            "generation_gte",
        }
    with pytest.raises(KeyError):
        facts.modifier("nope")
    assert facts.rate_rows_json()[0]["row_id"] == facts.rate_rows[0].row_id
    assert facts.modifiers_json()[0]["modifier_id"] == facts.modifiers[0].modifier_id


def test_golden_arithmetic_from_facts() -> None:
    """SPEC §6.9 cases 1, 2, 3, 4, 18 recomputed from facts.json rows and modifiers (Decimal)."""
    from tokenbill.core.money import token_nano

    facts = load()
    row = facts.rows_for("claude-opus-5-5")[0]
    inp, out = row.input_usd_per_mtok, row.output_usd_per_mtok
    lines = [
        (1000, inp),
        (100_000, inp * row.cache_read_mult),
        (2000, inp * row.cache_write_5m_mult),
        (3000, inp * row.cache_write_1h_mult),
        (500, out),
    ]
    case1 = sum(token_nano(q, r) for q, r in lines)
    assert case1 == 68_000_000
    geo = facts.modifier("anthropic.inference_geo.us").factor
    batch = facts.modifier("anthropic.batch").factor
    assert sum(token_nano(q, r, geo) for q, r in lines) == 74_800_000
    assert sum(token_nano(q, r, batch) for q, r in lines) == 34_000_000
    assert sum(token_nano(q, r, batch, geo) for q, r in lines) == 37_400_000
    fast = dict(facts.modifier("anthropic.fast.opus-5-5").base_usd_per_mtok)
    fin, fout = fast["input"], fast["output"]
    fast_lines = [
        (1000, fin),
        (100_000, fin * row.cache_read_mult),
        (2000, fin * row.cache_write_5m_mult),
        (3000, fin * row.cache_write_1h_mult),
        (500, fout),
    ]
    assert sum(token_nano(q, r) for q, r in fast_lines) == 136_000_000
    assert token_nano(1_000_000, inp * row.cache_write_1h_mult, batch, geo) == 4_400_000_000


def test_settings_keys() -> None:
    keys = load().settings_keys
    assert set(SETTINGS_KEYS_11_4) == set(keys)
    ttl = keys["promptCacheTtl"]
    assert (
        ttl.verified
        and ttl.min_version == "2.1.242"
        and '"5m"' in ttl.domain
        and '"1h"' in ttl.domain
    )
    assert keys["maxEffortLevel"].min_version == "2.1.267"
    assert keys["modelPricing"].managed_only and "cacheWrite" in keys["modelPricing"].domain
    assert keys["effortLevel"].verified and "max" not in keys["effortLevel"].domain
    assert keys["model"].lever_id == "cc.default_model" and keys["model"].tradeoff
    assert keys["hooks.SessionStart"].lever_id == "cc.cold_resume_hook"
    assert keys["env.CLAUDE_CODE_DISABLE_1M_CONTEXT"].lever_id is None
    for k in keys.values():
        assert k.target == "claude-code" and isinstance(k.verified, bool)


def test_evidence_lifecycle_focus_headless_sku() -> None:
    facts = load()
    assert set(facts.evidence) == set(EVIDENCE_NAMES)
    assert facts.successors == {
        "claude-fable-5": "claude-fable-5-1",
        "claude-opus-5": "claude-opus-5-5",
        "claude-mythos-5": "claude-mythos-5-1",
    }
    assert "claude-opus-4-8" not in facts.successors and "claude-sonnet-4-6" not in facts.successors
    assert facts.retirements["claude-sonnet-4-5"] == "2026-09-29"
    assert facts.retirements["claude-haiku-4-5"] == "2026-10-15"
    assert facts.retirements["claude-opus-4-5"] == "2026-11-24"
    assert facts.retired["claude-opus-4-1"] == "2026-08-05"
    (promo,) = facts.promotions
    assert (promo.promotion_id, promo.model, promo.channel, promo.start, promo.not_before_end) == (
        "openai.gpt-5.6-sol.2026-08",
        "gpt-5.6-sol",
        "openai_api",
        "2026-08-21",
        "2026-11-21",
    )
    assert {a.model for a in facts.announced} == {"claude-sonnet-5-5", "claude-haiku-5-5"}
    focus = facts.focus
    assert focus.version == "1.4" and len(focus.columns) == 65
    names = [c.name for c in focus.columns]
    for required in (
        "BilledCost",
        "EffectiveCost",
        "ListCost",
        "ContractedCost",
        "ChargePeriodStart",
        "ServiceProviderName",
        "HostProviderName",
        "InvoiceIssuerName",
        "SkuId",
        "Tags",
        "AllocatedMethodId",
        "ConsumedQuantity",
        "PricingQuantity",
        "ServiceCategory",
    ):
        assert required in names
    assert "ProviderName" not in names and "PublisherName" not in names
    assert dict(focus.removed)["ProviderName"] == ("ServiceProviderName", "HostProviderName")
    assert focus.column("BilledCost").feature_level == "Mandatory"
    assert focus.column("BilledCost").allows_nulls is False
    assert "BilledCost" in focus.mandatory and "Tags" not in focus.mandatory
    with pytest.raises(KeyError):
        focus.column("ProviderName")
    assert (
        focus.custom_column_prefix == "x_"
        and focus.service_category_ai == "AI and Machine Learning"
    )
    assert (
        facts.headless_fields["result_message"]
        .fields["total_cost_usd"]
        .startswith("includes subagents")
    )
    assert facts.headless_fields["stream_json"].fields["subagent_link"] == "parent_tool_use_id"
    assert facts.sku_rules and all(not r.verified for r in facts.sku_rules)
    assert all(r.source_kind == "aws.cur2" and r.unit_tokens == 1_000_000 for r in facts.sku_rules)


def _mutated(**changes: object) -> str:
    raw = _raw()
    for path, value in changes.items():
        target = raw
        keys = path.split("__")
        for k in keys[:-1]:
            target = target[int(k)] if isinstance(target, list) else target[k]
        last = keys[-1]
        if value is ...:
            del target[last]
        else:
            target[last] = value
    return json.dumps(raw)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"schema": "tokenbill/facts@0"}),
        '{"schema": "tokenbill/facts@1", "x": 1.5}',
        _mutated(rates__0__source=...),
        _mutated(evidence__0__verification="guess"),
        _mutated(rates__0__usd_per_mtok={"input": 4, "output": "20"}),
        _mutated(rates__0__published_absolute={"cache_read": "0.21"}),
        _mutated(rates__0__published_absolute={"cache_write_other": "1"}),
        _mutated(evidence__0__type="complex"),
        _mutated(evidence__0__value="2000"),
        _mutated(evidence__12__value=["1.00"]),
        _mutated(evidence__8__value=7),
        _mutated(lifecycle__successors__0__successor=...),
        _mutated(focus_columns__finding=""),
    ],
)
def test_parse_rejects_malformed_documents(text: str) -> None:
    with pytest.raises(ContractViolation):
        parse(text)


def test_evidence_constants() -> None:
    assert evidence.MISS_MIN_TOKENS.value == 2000
    assert evidence.MISS_MIN_FRACTION.value == Decimal("0.05")
    value, source_url, finding_id, checked_on = evidence.MISS_MIN_TOKENS
    assert (
        source_url.startswith("https://code.claude.com") and finding_id == "cc-usage-likely-cause"
    )
    assert checked_on == "2026-09-23"
    assert evidence.CACHE_READ_SHARE_MEDIAN.value == Decimal("0.84")
    assert evidence.TOKENIZER_BAND.value == (Decimal("1.00"), Decimal("1.35"))
    assert evidence.FEMP_MONTHLY.value == (Decimal("5"), Decimal("15"))
    assert (
        evidence.KEEPALIVE_INTERVAL_S.value == 240 and evidence.KEEPALIVE_MAX_IDLE_S.value == 3600
    )
    assert evidence.CC_AUTOCOMPACT_DEFAULT_TOKENS.value == 967000
    assert evidence.REFUSAL_AMBIGUOUS_MAX_OUTPUT.value == 16
    assert evidence.EDIT_PAYBACK_FORMULA.value == "K* = S(α − β)/(Xβ)"
    assert set(evidence.TABLE) == set(EVIDENCE_NAMES)
    assert evidence.get("MIN_CALIBRATION_PERIODS").value == 12
    with pytest.raises(ContractViolation):
        evidence.get("NOPE")


def test_keepalive_break_even_appendix_a12() -> None:
    w = Decimal("1.25")
    assert evidence.keepalive_break_even_s(w, Decimal("0.1")) == 2760  # 46 min
    assert evidence.keepalive_break_even_s(w, Decimal("0.05")) == 5760  # 96 min (Opus 5.5)
    assert evidence.keepalive_break_even_s(w, Decimal("0.025")) == 11760  # 196 min (Fable 5.1)
    assert (
        evidence.keepalive_break_even_s(w, Decimal("0.1"), interval_s=300) == 3450
    )  # 57.5 min with τ
    with pytest.raises(ContractViolation):
        evidence.keepalive_break_even_s(w, Decimal("0"))


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=8), c, max_size=4),
    max_leaves=12,
)


@given(json_values, st.sampled_from(sorted(_raw())))
@settings(max_examples=200, deadline=None)
def test_parse_fuzz_only_contract_violations(value: object, key: str) -> None:
    for doc in (value, {"schema": FACTS_SCHEMA, key: value}, {**_raw(), key: value}):
        try:
            parse(json.dumps(doc))
        except ContractViolation:
            pass
