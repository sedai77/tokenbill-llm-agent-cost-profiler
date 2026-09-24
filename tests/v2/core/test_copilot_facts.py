"""GitHub Copilot facts (CORE-AMENDMENTS C-28): the top-level ``copilot`` section of facts.json,
its provenance, the rate rows replayed from the pricing YAML (checked against addendum §19.2 and
the Appendix C golden arithmetic), and the typed accessors."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from importlib import resources

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import facts as facts_mod
from tokenbill.core.errors import ContractViolation
from tokenbill.core.facts import META_KEYS, load, parse
from tokenbill.core.money import EXACT_CTX, token_nano
from tokenbill.core.records import (
    COPILOT_WORKLOADS,
    EDITOR_FAMILIES,
    GITHUB_COST_TYPES,
)
from tokenbill.core.types import RateRow


def _raw() -> dict:
    text = resources.files("tokenbill.core").joinpath("facts.json").read_text(encoding="utf-8")
    return json.loads(text)


def _row_at(model: str, date: str) -> RateRow | None:
    for row in load().copilot_rates():
        if row.model == model and row.effective_from <= date and (
                row.effective_to is None or date < row.effective_to):
            return row
    return None


# ---------- provenance and separation ----------


def test_copilot_entries_carry_provenance() -> None:
    entries = [(s, e) for s, e in load().entries() if s.startswith("copilot.")]
    parts = {s.split(".", 1)[1] for s, _ in entries}
    assert parts == set(facts_mod._COPILOT_PARTS)
    assert len(entries) > 250
    for section, entry in entries:
        for key in META_KEYS:
            assert isinstance(entry.get(key), str) and entry[key], (section, key)
        assert entry["verification"] == "research", section  # R-E19
        assert entry["verified_on"] == "2026-09-23"


def test_top_level_sections_unchanged() -> None:
    f = load()
    assert len(f.rate_rows) == 12 and len(f.modifiers) == 5 and len(f.settings_keys) == 31
    assert len(f.promotions) == 1 and f.promotions[0].promotion_id == "openai.gpt-5.6-sol.2026-08"
    assert all(k.target == "claude-code" for k in f.settings_keys.values())
    assert len(f.rows_for("claude-opus-5-5")) == 1  # Copilot rows only via copilot_rates()
    assert f.rows_for("claude-opus-5-5", "github_copilot") == ()
    assert not any(r.channel == "github_copilot" for r in f.rate_rows)
    assert list(_raw())[-1] == "copilot"


def test_no_float_anywhere() -> None:
    def walk(value: object) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(_raw()["copilot"])
    text = resources.files("tokenbill.core").joinpath("facts.json").read_text(encoding="utf-8")
    json.loads(text, parse_float=lambda tok: pytest.fail(f"float {tok}"))


# ---------- rate rows (§19.2) ----------

CURRENT_192 = {  # model → (input, cached, write or None, output, effective_from) on 2026-09-23
    "gpt-5-mini": ("0.25", "0.025", None, "2.00", "2026-06-01"),
    "gpt-5.3-codex": ("1.75", "0.175", None, "14.00", "2026-06-01"),
    "gpt-5.4": ("2.50", "0.25", None, "15.00", "2026-06-04"),
    "gpt-5.4-mini": ("0.75", "0.075", None, "4.50", "2026-06-01"),
    "gpt-5.4-nano": ("0.20", "0.02", None, "1.25", "2026-06-01"),
    "gpt-5.5": ("5.00", "0.50", None, "30.00", "2026-06-04"),
    "gpt-5.6-luna": ("0.20", "0.02", "0.25", "1.20", "2026-08-03"),
    "gpt-5.6-sol": ("4.00", "0.40", "5.00", "20.00", "2026-09-04"),
    "gpt-5.6-terra": ("2.00", "0.20", "2.50", "12.00", "2026-08-03"),
    "gpt-6-astra": ("10.00", "1.00", "12.50", "50.00", "2026-09-04"),
    "gpt-6-luna": ("0.10", "0.01", "0.125", "0.50", "2026-09-22"),
    "gpt-6-sol": ("2.00", "0.20", "2.50", "10.00", "2026-09-22"),
    "claude-haiku-4-5": ("1.00", "0.10", "1.25", "5.00", "2026-06-01"),
    "claude-sonnet-4": ("3.00", "0.30", "3.75", "15.00", "2026-06-01"),
    "claude-sonnet-4-6": ("3.00", "0.30", "3.75", "15.00", "2026-06-01"),
    "claude-opus-4-7": ("5.00", "0.50", "6.25", "25.00", "2026-06-01"),
    "claude-opus-4-8": ("5.00", "0.50", "6.25", "25.00", "2026-06-01"),
    "claude-opus-5": ("5.00", "0.50", "6.25", "25.00", "2026-07-24"),
    "claude-opus-5-5": ("4.00", "0.20", "5.00", "20.00", "2026-09-22"),
    "claude-sonnet-5": ("2.00", "0.20", "2.50", "10.00", "2026-06-30"),
    "claude-fable-5": ("10.00", "1.00", "12.50", "50.00", "2026-06-09"),
    "claude-fable-5-1": ("10.00", "0.25", "12.50", "50.00", "2026-09-01"),
    "gemini-3.5-flash": ("1.50", "0.15", None, "9.00", "2026-06-01"),
    "gemini-3.6-flash": ("0.75", "0.075", None, "3.75", "2026-08-13"),
    "gemini-3.7-flash": ("0.75", "0.075", None, "3.75", "2026-08-13"),
    "gemini-3.8-flash": ("0.75", "0.075", None, "3.75", "2026-09-03"),
    "grok-4.5": ("2.00", "0.50", None, "6.00", "2026-07-28"),
    "grok-4.6": ("2.00", "0.50", None, "6.00", "2026-08-14"),
    "grok-4.7": ("2.00", "0.50", None, "6.00", "2026-09-21"),
    "mai-code-1.1-flash": ("0.20", "0.02", None, "1.20", "2026-08-11"),
    "kimi-k2.7-code": ("0.95", "0.19", None, "4.00", "2026-07-01"),
    "kimi-k3": ("3.00", "0.30", None, "15.00", "2026-08-07"),
}


def test_current_rows_match_section_19_2() -> None:
    for model, (inp, cached, write, out, frm) in CURRENT_192.items():
        row = _row_at(model, "2026-09-23")
        assert row is not None, model
        assert row.effective_from == frm, model
        assert row.input_usd_per_mtok == Decimal(inp) and row.output_usd_per_mtok == Decimal(out)
        assert EXACT_CTX.multiply(row.input_usd_per_mtok, row.cache_read_mult) == Decimal(cached)
        if write is None:
            assert row.cache_write_5m_mult is None and row.cache_write_1h_mult is None, model
        else:
            assert EXACT_CTX.multiply(row.input_usd_per_mtok, row.cache_write_5m_mult) == Decimal(
                write)
            one_hour = EXACT_CTX.multiply(row.input_usd_per_mtok, row.cache_write_1h_mult)
            if model.startswith("claude-"):
                assert one_hour == 2 * row.input_usd_per_mtok  # DC6 assumption (VERIFY)
            else:
                assert one_hour == Decimal(write)  # one published write price, both classes
        assert row.provider == "github" and row.channel == "github_copilot" and row.enabled
        assert "date_source=" in row.notes and "category " in row.notes
    assert _row_at("claude-opus-4-8", "2026-09-23").supports == ("fast_mode",)


def test_history_rows_and_intervals() -> None:
    rows = load().copilot_rates()
    by_model: dict[str, list[RateRow]] = {}
    for row in rows:
        by_model.setdefault(row.model, []).append(row)
    for model, group in by_model.items():
        group.sort(key=lambda r: r.effective_from)
        for a, b in zip(group, group[1:], strict=False):
            assert a.effective_to is not None and a.effective_to <= b.effective_from, model
    sol = [(r.effective_from, r.effective_to, str(r.input_usd_per_mtok), r.promotion)
           for r in by_model["gpt-5.6-sol"]]
    assert sol == [
        ("2026-07-09", "2026-08-03", "5.00", None),
        ("2026-08-03", "2026-08-20", "5.00", None),
        ("2026-08-20", "2026-08-21", "2.50", "github.gpt-5.6-sol.2026-08"),
        ("2026-08-21", "2026-09-04", "2.00", "github.gpt-5.6-sol.2026-08"),
        ("2026-09-04", None, "4.00", None),
    ]
    assert by_model["gpt-5.6-sol"][0].cache_write_5m_mult is None  # no write listed before 08-03
    assert [r.effective_to for r in by_model["claude-opus-4-5"]] == ["2026-09-04"]
    assert [r.effective_to for r in by_model["gemini-3.8-flash"]] == ["2027-01-01"]
    assert [str(r.input_usd_per_mtok) for r in by_model["gemini-3.6-flash"]] == ["1.50", "0.75"]
    assert [str(r.input_usd_per_mtok) for r in by_model["gpt-5.6-luna"]] == ["1.00", "0.20",
                                                                              "0.20"]
    bands = {r.model: r.long_context_threshold for r in rows if r.long_context_threshold}
    assert bands["gpt-5.6-luna"] == 200000 and bands["grok-4.7"] == 200000
    assert bands["gpt-5.5"] == 272000 and "claude-opus-5-5" not in bands
    assert [r.long_context_threshold for r in by_model["gpt-5.4"]] == [None, 272000]
    promos = {p.promotion_id for p in load().copilot.promotions}
    assert all(r.promotion is None or r.promotion in promos for r in rows)
    assert len(rows) == 46


def _price(row: RateRow, *, uncached: int = 0, read: int = 0, write: int = 0, out: int = 0,
           band: bool = False, write_mult: Decimal | None = None) -> int:
    rates = dict(row.long_context_usd_per_mtok) if band else {}
    inp = rates.get("input", row.input_usd_per_mtok)
    total = token_nano(uncached, inp)
    total += token_nano(read, rates.get("cache_read") or EXACT_CTX.multiply(
        row.input_usd_per_mtok, row.cache_read_mult))
    if write:
        total += token_nano(write, row.input_usd_per_mtok, write_mult or row.cache_write_5m_mult)
    total += token_nano(out, rates.get("output", row.output_usd_per_mtok))
    return total


def test_appendix_c_golden_arithmetic() -> None:
    opus55 = _row_at("claude-opus-5-5", "2026-09-23")
    g1 = _price(opus55, uncached=12_000, read=180_000, write=6_000, out=3_000)
    assert g1 == 174_000_000  # G1 point (write at the published 5m price)
    assert _price(opus55, uncached=12_000, read=180_000, write=6_000, out=3_000,
                  write_mult=opus55.cache_write_1h_mult) == 192_000_000  # G1 high / 1h hint
    gpt55 = _row_at("gpt-5.5", "2026-09-10")
    assert _price(gpt55, uncached=20_000, read=280_000, out=4_000, band=True) == 660_000_000
    assert _price(gpt55, uncached=20_000, read=250_000, out=4_000) == 345_000_000  # G5
    assert _price(gpt55, uncached=20_000, read=250_000, out=4_000, band=True) == 630_000_000
    for date, expect in (("2026-09-10", 40_400_000), ("2026-08-25", 20_200_000),
                         ("2026-08-20", 27_750_000)):  # G6, G7
        sol = _row_at("gpt-5.6-sol", date)
        assert _price(sol, uncached=2_000, read=6_000, write=2_000, out=1_000) == expect
    gemini = _row_at("gemini-3.8-flash", "2026-09-23")
    assert _price(gemini, uncached=100_000, read=400_000, out=10_000) == 142_500_000  # G9
    assert _row_at("gemini-3.8-flash", "2027-01-01") is None
    grok = _row_at("grok-4.7", "2026-09-23")
    assert _price(grok, uncached=50_000, read=160_000, out=5_000, band=True) == 420_000_000
    opus47 = _row_at("claude-opus-4-7", "2026-09-23")
    assert _price(opus47, uncached=6, read=127_386, write=2_220, out=6_210) == 232_848_000  # G11
    kimi = _row_at("kimi-k2.7-code", "2026-09-23")
    assert _price(kimi, read=1_000_000) == 190_000_000  # G14
    assert _row_at("claude-opus-5-5", "2026-09-21") is None  # G16: C-dated boundary
    opus48 = _row_at("claude-opus-4-8", "2026-09-23")
    fast = load().copilot.modifiers[2]
    base = dict(fast.base_usd_per_mtok)
    fast_total = (token_nano(10_000, base["input"])
                  + token_nano(90_000, base["input"], opus48.cache_read_mult)
                  + token_nano(2_000, base["output"]))
    assert fast_total == 290_000_000
    assert _price(opus48, uncached=10_000, read=90_000, out=2_000) == 145_000_000  # G8


# ---------- the other Copilot tables ----------


def test_copilot_tables() -> None:
    c = load().copilot
    assert c.credit.usd_per_credit == Decimal("0.01") and c.credit.nano_aiu_per_credit == 10**9
    assert c.plans["business"].seat_usd_per_month == Decimal("19")
    assert (c.plans["enterprise"].included_credits, c.plans["enterprise"].promo_credits) == (3900,
                                                                                             7000)
    assert (c.plans["business"].promo_from, c.plans["business"].promo_to) == ("2026-06-01",
                                                                              "2026-09-01")
    mods = {m.modifier_id: m for m in c.modifiers}
    assert mods["github.auto"].factor == Decimal("0.9")
    assert dict(mods["github.auto"].when) == {"channel_in": "github_copilot", "routing": "auto"}
    assert mods["github.compliance"].factor == Decimal("1.1")
    assert dict(mods["github.compliance"].when)["compliance_in"] == "data_residency,fedramp"
    assert mods["github.fast.opus-4-8"].kind == "replace_base"
    assert c.write_1h_rule.multiplier_of_input == 2 and not c.write_1h_rule.verified
    assert c.band_rules["gpt-6-sol"].threshold == 272000
    assert {p.promotion_id for p in c.promotions} == {
        "github.gpt-5.6-sol.2026-08", "github.gemini-3.6-flash.2026",
        "github.gemini-3.7-flash.2026", "github.gemini-3.8-flash.2026"}
    assert (c.skus["copilot_for_business"].plan, c.skus["copilot_enterprise"].plan) == (
        "business", "enterprise")
    assert c.skus["copilot_standalone"].plan == "business" and not c.skus[
        "copilot_standalone"].verified
    assert c.skus["copilot_ai_credit"].cost_type_unattributed == "ai_credit.direct"
    assert c.skus["coding_agent_ai_credit"].workload == "copilot_cloud_agent"
    assert all(s.cost_type in GITHUB_COST_TYPES for s in c.skus.values())
    assert {q: f.plan for q, f in c.plan_quota_map.items()} == {
        1900: "business", 3900: "enterprise", 3000: "business", 7000: "enterprise"}
    assert c.plan_quota_map[7000].months == ("2026-06", "2026-07", "2026-08")
    assert c.plan_quota_map[1900].months == ()
    assert {w.workload for w in c.workflow_paths} <= set(COPILOT_WORKLOADS)
    assert c.workflow_paths[-1].match == "glob"
    assert c.utility_models == {"gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-5.4-nano"}
    assert c.model_categories["claude-opus-5-5"] == "Powerful"
    assert c.retirements["gpt-5.5"].successor == "gpt-5.6-sol"
    assert c.retirements["claude-opus-4-7"].retire_on == "2026-10-02"
    assert c.remaps["claude-opus-4-8"].target == "claude-sonnet-5"
    assert c.remaps["claude-opus-4-8"].tokenizer_same
    assert c.runner_rates["actions_linux"].usd_per_minute == Decimal("0.006")
    assert c.runner_rates["linux_16_core"].usd_per_minute == Decimal("0.042")
    assert not c.runner_rates["linux_16_core"].included_minutes_apply
    assert c.runner_rates["windows_96_core"].usd_per_minute == Decimal("0.552")
    assert c.included_minutes["ghec"].minutes_per_month == 50_000
    assert c.review_estimates["balanced"].high_usd == Decimal("5")
    assert c.dates["promo_end"] == "2026-09-01" and c.dates["review_default_balanced"] == (
        "2026-09-28")
    assert c.cache_ttl_statement.startswith("24 hours") and c.report_lag_days == 3
    assert c.aic_default_cap_per_run == 1000


def test_copilot_settings_keys() -> None:
    keys = load().copilot_settings_keys()
    assert len(keys) == 19 and all(k.target == "github-copilot" for k in keys.values())
    assert keys["copilot.managed.model"].lever_id == "copilot.default_model_auto"
    assert keys["copilot.managed.model"].managed_only
    assert keys["copilot.ci.max_ai_credits"].min_version == "1.0.67"
    assert sum(k.startswith("copilot.managed.telemetry.") for k in keys) == 8
    assert not set(keys) & set(load().settings_keys)


def test_editor_families_and_vscode_traces() -> None:
    fams = load().copilot_editor_families()
    assert {f.family for f in fams} <= set(EDITOR_FAMILIES)
    assert {f.origin for f in fams} == {"seat_editor", "activity_surface", "metrics_ide"}
    seat = {f.pattern: f.family for f in fams if f.origin == "seat_editor"}
    assert seat["vscode/"] == "vscode" and seat["intellij"] == "jetbrains"
    surface = {f.pattern: f for f in fams if f.origin == "activity_surface"}
    assert surface["VS Code"].verified and not surface["IntelliJ"].verified
    ide = {f.pattern: f.family for f in fams if f.origin == "metrics_ide"}
    assert ide["intellij"] == "jetbrains" and ide["visualstudio"] == "visual_studio"
    v = load().copilot_vscode_traces()
    assert len(v.attribute_allowlist) == 18 and "copilot_chat.copilot_usage_nano_aiu" in (
        v.attribute_allowlist)
    assert "gen_ai.input.messages" in v.content_keys
    assert not set(v.attribute_allowlist) & set(v.content_keys)
    assert (v.retention_days, v.retention_sessions) == (7, 100)
    assert v.db_file == "agent-traces.db" and not v.verified
    assert set(v.paths) == {"darwin", "linux", "win32"}
    assert "span_events" not in v.span_columns and v.span_columns[0] == "span_id"


def test_module_level_accessors() -> None:
    f = load()
    pairs = [
        (facts_mod.copilot_rates, f.copilot_rates), (facts_mod.copilot_modifiers,
                                                     f.copilot_modifiers),
        (facts_mod.copilot_plans, f.copilot_plans), (facts_mod.copilot_dates, f.copilot_dates),
        (facts_mod.copilot_skus, f.copilot_skus),
        (facts_mod.copilot_plan_quota_map, f.copilot_plan_quota_map),
        (facts_mod.copilot_workflow_paths, f.copilot_workflow_paths),
        (facts_mod.copilot_remaps, f.copilot_remaps),
        (facts_mod.copilot_retirements, f.copilot_retirements),
        (facts_mod.copilot_runner_rates, f.copilot_runner_rates),
        (facts_mod.copilot_settings_keys, f.copilot_settings_keys),
        (facts_mod.copilot_editor_families, f.copilot_editor_families),
        (facts_mod.copilot_vscode_traces, f.copilot_vscode_traces),
        (facts_mod.copilot_report_lag_days, f.copilot_report_lag_days),
    ]
    for module_fn, method in pairs:
        assert module_fn() == method()
    raw_rows = f.copilot_rate_rows_json()
    raw_rows[0]["model"] = "mutated"
    assert f.copilot_rate_rows_json()[0]["model"] != "mutated"  # deep copies
    with pytest.raises(TypeError):
        f.copilot_skus()["x"] = None  # type: ignore[index]


# ---------- validation of the section ----------


def _mutated(mutate: object) -> str:
    raw = _raw()
    mutate(raw["copilot"])  # type: ignore[operator]
    return json.dumps(raw)


def _set(path: str, value: object):  # noqa: ANN202
    def mutate(c: dict) -> None:
        *head, last = path.split("/")
        target: object = c
        for part in head:
            target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
        if isinstance(target, list):
            target[int(last)] = value
        elif value is ...:
            del target[last]  # type: ignore[union-attr]
        else:
            target[last] = value  # type: ignore[index]
    return mutate


@pytest.mark.parametrize(
    "mutation",
    [
        _set("credit", ...),
        _set("unexpected", {}),
        _set("rates/0/channel", "anthropic_api"),
        _set("rates/0/provider", "openai"),
        _set("rates/0/promotion", "github.nope"),
        _set("rates/0/usd_per_mtok/input", 0.25),
        _set("rates/1/row_id", "github/github_copilot/gpt-5-mini/2026-06-01"),
        _set("modifiers/0/when", {"routing": "auto"}),
        _set("plans/0/plan", "pro"),
        _set("skus/0/cost_type", "tokens"),
        _set("skus/0/plan", "unknown"),
        _set("skus/1/sku", "copilot_ai_credit"),
        _set("skus/1/workload", "ci"),
        _set("plan_quota_map/0/plan", "mixed"),
        _set("plan_quota_map/0/quota", "1900"),
        _set("workflow_paths/0/match", "regex"),
        _set("retirements/0/retire_on", "Oct 2"),
        _set("dates/1/name", "usage_based_billing_start"),
        _set("settings_keys/0/target", "claude-code"),
        _set("editor_families/0/family", "vs"),
        _set("vscode_traces/content_keys", ["gen_ai.response.id"]),
        _set("report_lag_days/value", "3"),
        _set("cache_ttl_statement/text", 3),
        _set("plans/0/source", ""),
        _set("dates/0/verification", "guess"),
        _set("skus/0/verified", "false"),
        _set("vscode_traces/verified", 0),
    ],
)
def test_parse_rejects_malformed_copilot_sections(mutation: object) -> None:
    with pytest.raises(ContractViolation):
        parse(_mutated(mutation))


_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=6),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=6), c, max_size=3),
    max_leaves=8,
)


@given(st.sampled_from(sorted(_raw()["copilot"])), _json_values, st.data())
@settings(max_examples=150, deadline=None)
def test_parse_fuzz_copilot_only_contract_violations(part: str, value: object,
                                                    data: st.DataObject) -> None:
    raw = _raw()
    section = raw["copilot"][part]
    if isinstance(section, list) and section and data.draw(st.booleans()):
        entry = copy.deepcopy(section[data.draw(st.integers(0, len(section) - 1))])
        key = data.draw(st.sampled_from(sorted(entry)))
        entry[key] = value
        section.append(entry)
    else:
        raw["copilot"][part] = value
    try:
        parse(json.dumps(raw))
    except ContractViolation:
        pass
