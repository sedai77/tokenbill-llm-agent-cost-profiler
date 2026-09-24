"""core.catalog, GitHub Copilot tables (CORE-AMENDMENTS K-1 … K-3; addendum §10.4, §11.1, §11.3,
§11.4; F-KIT-C acceptance). The SPEC tables stay exactly as ``test_catalog.py`` pins them."""

from __future__ import annotations

import dataclasses
import re
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import catalog, records
from tokenbill.core import registry as reg
from tokenbill.core.errors import UsageError
from tokenbill.core.facts import load as load_facts
from tokenbill.core.types import ADMIN_ACTION_WHERE, Fix

#: Addendum §11.1, in table order.
COPILOT_LEVER_IDS = (
    "copilot.default_model_auto", "copilot.model_policy", "copilot.fast_mode_off",
    "copilot.seat_reclaim", "copilot.seat_policy_selected", "copilot.seat_downgrade",
    "copilot.agent_runner_standard", "copilot.seat_reclaim_team", "copilot.auto_tier",
    "copilot.review_effort_lite", "copilot.review_triggers", "copilot.review_mcp_off",
    "copilot.review_instructions_trim", "copilot.review_unlicensed_off", "copilot.session_limits",
    "copilot.agentic_workflow_caps", "copilot.budget_plan", "copilot.mcp_trim",
    "copilot.context_default", "copilot.telemetry_on", "copilot.vscode_traces_optin",
)
AGGREGATE_LEVERS = COPILOT_LEVER_IDS[:7]
#: needs_eval / tradeoff yes/yes rows of §11.1.
TRADEOFFS = {"copilot.model_policy", "copilot.seat_policy_selected", "copilot.seat_downgrade",
             "copilot.review_effort_lite", "copilot.context_default"}
#: Every id of the F-KIT-C brief.
BRIEF_ADMIN_ACTIONS = {
    "admin:model_policy", "admin:model_policy_fast", "admin:org_seat_policy",
    "admin:seat_plan_change", "admin:runner_type", "admin:team_membership_review",
    "admin:communicate_auto_tier", "admin:review_effort_default",
    "admin:communicate_personal_review_settings", "admin:review_triggers",
    "admin:repo_review_mcp_off", "admin:review_instructions", "admin:review_unlicensed_policy",
    "admin:ci_limits_snippet", "admin:aw_triggers", "admin:org_cli_billing_policy",
    "admin:paid_usage_policy", "admin:cost_center_pool", "admin:budget_stop",
    "admin:plan_confirm", "admin:vscode_db_exporter_optin", "rest:org_selected_users_delete",
    "rest:org_selected_teams_delete", "rest:budget_create", "rest:cost_center_create",
    "rest:cost_center_patch", "rest:coding_agent_policy",
}
#: SPEC §10.2 kinds of the detectors named at kind level in FAMILY_EXCLUSIONS (used while their
#: modules are not merged; the declared ``kinds`` win when the class is importable).
SPEC_KINDS = {
    "premium.modifiers": {"fast-premium", "geo-premium", "regional-premium", "tier-premium"},
    "automation": {"ci-cross-run", "scheduled-cadence", "batch-eligible", "ci-run-cost"},
    "context.static-prefix": {"static-prefix", "tool-defs-bloat"},
    "model.routing": {"delegation-routing", "same-tier-upgrade", "default-model",
                      "default-effort", "effort-mix", "rebaseline"},
}


# ---------- K-1: levers and the aggregate grammar ----------


def test_copilot_levers_are_the_addendum_table() -> None:
    assert tuple(lv.lever_id for lv in catalog.COPILOT_LEVERS) == COPILOT_LEVER_IDS
    for lv in catalog.COPILOT_LEVERS:
        assert lv.lever_class in catalog.LEVER_CLASSES, lv.lever_id
        assert lv.grid == (), lv.lever_id  # candidates live in AGGREGATE_GRIDS
        assert lv.replay == ("aggregate" if lv.lever_id in AGGREGATE_LEVERS else "none")
        if lv.lever_id in TRADEOFFS:
            assert lv.needs_eval and lv.tradeoff, lv.lever_id
        if lv.lever_class == "trajectory":
            assert lv.upper_bound and lv.needs_eval, lv.lever_id
        for key in lv.patch_keys:
            assert key in catalog.COPILOT_ALLOWLIST or key in catalog.ADMIN_ACTIONS, (
                lv.lever_id, key)
    assert "aggregate" in catalog._REPLAY_KINDS
    assert all(lv.replay != "aggregate" for lv in catalog.LEVERS)  # SPEC table untouched
    assert catalog.lever("copilot.default_model_auto").patch_keys == ("copilot.managed.model",)
    telemetry = catalog.lever("copilot.telemetry_on").patch_keys
    assert len(telemetry) == 8 and all(k.startswith("copilot.managed.telemetry.")
                                       for k in telemetry)
    assert catalog.lever("copilot.vscode_traces_optin").patch_keys == (
        "admin:vscode_db_exporter_optin",)
    assert catalog.lever("copilot.mcp_trim").upper_bound


def test_every_aggregate_grid_entry_parses_and_round_trips() -> None:
    assert set(catalog.AGGREGATE_GRIDS) == set(AGGREGATE_LEVERS)
    parsed = 0
    for lever_id, grid in catalog.AGGREGATE_GRIDS.items():
        assert grid, lever_id
        assert catalog.lever(lever_id).replay == "aggregate"
        for spec in grid:
            agg = catalog.parse_aggregate_spec(spec)
            assert catalog.to_aggregate_spec(agg) == spec
            assert catalog.parse_aggregate_spec(catalog.to_aggregate_spec(agg)) == agg
            parsed += 1
    remaps = load_facts().copilot.remaps
    assert catalog.AGGREGATE_GRIDS["copilot.model_policy"] == tuple(
        f"copilot:remap={fact.target}@model:{model}" for model, fact in sorted(remaps.items()))
    assert catalog.AGGREGATE_GRIDS["copilot.seat_reclaim"] == (
        "copilot:seats_idle=30d@all", "copilot:seats_idle=60d@all")
    assert parsed >= 16


@pytest.mark.parametrize("spec,expected", [
    ("copilot:auto=on", ("auto", "on", "all")),
    ("copilot:auto=on@team:platform", ("auto", "on", "team:platform")),
    ("copilot:auto_tier=efficiency@org:acme", ("auto_tier", "efficiency", "org:acme")),
    ("copilot:remap=claude-sonnet-5@model:claude-opus-4-7",
     ("remap", "claude-sonnet-5", "model:claude-opus-4-7")),
    ("copilot:seat_policy=assign_selected@org:org-b",
     ("seat_policy", "assign_selected", "org:org-b")),
    ("copilot:seats_team=90d@entity:cc:platform", ("seats_team", "90d", "entity:cc:platform")),
    ("copilot:plan=business@entity:enterprise", ("plan", "business", "entity:enterprise")),
    ("copilot:runner=linux_16_core@all", ("runner", "linux_16_core", "all")),
    ("copilot:runner=actions_linux_16_core@all", ("runner", "actions_linux_16_core", "all")),
    ("copilot:context_tier=default@team:data", ("context_tier", "default", "team:data")),
    ("copilot:mcp=trim@all", ("mcp", "trim", "all")),
    ("copilot:aw_cap=1500@org:acme", ("aw_cap", "1500", "org:acme")),
    ("copilot:fast=off@entity:org:acme", ("fast", "off", "entity:org:acme")),
])
def test_parse_aggregate_spec(spec: str, expected: tuple[str, str, str]) -> None:
    agg = catalog.parse_aggregate_spec(spec)
    assert (agg.param, agg.value, agg.scope) == expected
    canonical = catalog.to_aggregate_spec(agg)
    assert canonical.endswith("@" + expected[2])
    assert catalog.parse_aggregate_spec(canonical) == agg


@pytest.mark.parametrize("spec", [
    "", "auto=on", "copilot:", "copilot:auto", "copilot:auto=", "copilot:=on",
    "copilot:auto=maybe", "copilot:unknown=on", "copilot:auto=on@", "copilot:auto=on@all@all",
    "copilot:auto=on=off", "copilot:auto=on@galaxy:x", "copilot:auto=on@team:",
    "copilot:auto=on@team:a b", "copilot:auto=on@entity:planet", "copilot:auto=on@model:Opus!",
    "copilot:seats_idle=30@all", "copilot:seats_idle=0d@all", "copilot:aw_cap=0@all",
    "copilot:aw_cap=-5@all", "copilot:remap=Claude Opus@all", "copilot:runner=quantum@all",
    "copilot:plan=unknown@all", "copilot:plan=mixed@all", "copilot:auto=on@all:x",
    "Copilot:auto=on", "copilot:auto=on@team:x\n",
])
def test_parse_aggregate_spec_rejects(spec: str) -> None:
    with pytest.raises(UsageError):
        catalog.parse_aggregate_spec(spec)


def test_aggregate_spec_type_is_closed() -> None:
    with pytest.raises(UsageError):
        catalog.AggregateSpec("auto", "on", "team:")
    with pytest.raises(UsageError):
        catalog.AggregateSpec("auto", 1, "all")  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        catalog.to_aggregate_spec("copilot:auto=on")  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        catalog.parse_aggregate_spec(None)  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.AggregateSpec("auto", "on").value = "off"  # type: ignore[misc]


_VALUES = {
    "auto": st.sampled_from(["on", "off"]),
    "auto_tier": st.sampled_from(["efficiency", "balance", "intelligence"]),
    "remap": st.from_regex(r"[a-z0-9][a-z0-9.\-]{0,20}", fullmatch=True),
    "fast": st.sampled_from(["on", "off"]),
    "seats_idle": st.integers(1, 9999).map(lambda n: f"{n}d"),
    "seats_team": st.integers(1, 9999).map(lambda n: f"{n}d"),
    "seat_policy": st.sampled_from(["assign_selected", "disabled"]),
    "plan": st.sampled_from(["business", "enterprise"]),
    "runner": st.sampled_from(sorted(load_facts().copilot.runner_rates)),
    "context_tier": st.sampled_from(["default", "long_context"]),
    "mcp": st.sampled_from(["trim", "off"]),
    "aw_cap": st.integers(1, 10**6).map(str),
}
_SCOPES = st.one_of(
    st.just("all"),
    st.tuples(st.sampled_from(["team", "org"]),
              st.from_regex(r"[A-Za-z0-9._/:()-]{1,40}", fullmatch=True)).map(":".join),
    st.from_regex(r"[a-z0-9][a-z0-9.\-]{0,30}", fullmatch=True).map(lambda m: f"model:{m}"),
    st.one_of(st.just("enterprise"),
              st.from_regex(r"(org|cc):[A-Za-z0-9._-]{1,30}", fullmatch=True)).map(
        lambda e: f"entity:{e}"),
)


@settings(max_examples=300, deadline=None)
@given(st.sampled_from(sorted(_VALUES)).flatmap(
    lambda p: st.tuples(st.just(p), _VALUES[p])), _SCOPES)
def test_aggregate_grammar_round_trips(param_value: tuple[str, str], scope: str) -> None:
    param, value = param_value
    spec = catalog.AggregateSpec(param, value, scope)
    text = catalog.to_aggregate_spec(spec)
    assert catalog.parse_aggregate_spec(text) == spec
    assert catalog.to_aggregate_spec(catalog.parse_aggregate_spec(text)) == text


@settings(max_examples=500, deadline=None)
@given(st.text(max_size=80))
def test_aggregate_parser_fuzz_only_raises_usage_error(text: str) -> None:
    for candidate in (text, "copilot:" + text):
        try:
            spec = catalog.parse_aggregate_spec(candidate)
        except UsageError:
            continue
        assert catalog.parse_aggregate_spec(catalog.to_aggregate_spec(spec)) == spec


def test_lever_lookup_searches_both_tables() -> None:
    assert catalog.lever("copilot.seat_reclaim").lever_id == "copilot.seat_reclaim"
    assert catalog.lever("cc.prompt_cache_ttl.main").replay == "usage"  # SPEC first
    with pytest.raises(UsageError):
        catalog.lever("copilot.nope")
    assert catalog.levers_for_kind("idle-seat") == ()
    assert [lv.lever_id for lv in catalog.levers_for_kind("idle-seat", family="copilot")] == [
        "copilot.seat_reclaim"]
    assert catalog.levers_for_kind("ttl-expiry", family="copilot") == ()
    assert catalog.levers_for_kind("ttl-expiry") == catalog.levers_for_kind(
        "ttl-expiry", family="default")
    assert [lv.lever_id for lv in catalog.levers_for_kind("auto-adoption", family="copilot")] == [
        "copilot.default_model_auto", "copilot.auto_tier"]
    with pytest.raises(UsageError):
        catalog.levers_for_kind("idle-seat", family="cursor")
    linked = {k for lv in catalog.COPILOT_LEVERS for k in lv.finding_kinds}
    copilot_kinds = {k for (det, k) in catalog.COUNT_SOURCE if det.startswith("copilot.")}
    assert linked <= copilot_kinds  # every linked kind is a Copilot detector kind


# ---------- K-2: allowlist and admin actions ----------


def test_copilot_allowlist_mirrors_facts() -> None:
    facts = load_facts().copilot.settings_keys
    assert list(catalog.COPILOT_ALLOWLIST) == list(facts) and len(facts) == 19
    for key, entry in catalog.COPILOT_ALLOWLIST.items():
        fact = facts[key]
        assert entry.target == "github-copilot" and key.startswith("copilot.")
        assert (entry.domain, entry.min_version, entry.verified, entry.lever_id,
                entry.tradeoff, entry.source) == (fact.domain, fact.min_version, fact.verified,
                                                  fact.lever_id, fact.tradeoff, fact.source)
        if entry.lever_id is not None:
            assert entry.lever_id in {lv.lever_id for lv in catalog.COPILOT_LEVERS}
    assert catalog.copilot_allowed("copilot.ci.max_ai_credits").min_version == "1.0.67"
    assert catalog.allowed("copilot.managed.model") is catalog.COPILOT_ALLOWLIST[
        "copilot.managed.model"]
    assert catalog.allowed("promptCacheTtl") is catalog.ALLOWLIST["promptCacheTtl"]
    for bad in ("model", "copilot.managed.dangerous", None):
        with pytest.raises(UsageError):
            catalog.copilot_allowed(bad)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        catalog.allowed("copilot.nope")
    assert not set(catalog.ALLOWLIST) & set(catalog.COPILOT_ALLOWLIST)
    with pytest.raises(TypeError):
        catalog.COPILOT_ALLOWLIST["x"] = catalog.copilot_allowed(  # type: ignore[index]
            "copilot.managed.model")


def test_admin_actions_are_complete_and_well_formed() -> None:
    assert set(catalog.ADMIN_ACTIONS) == BRIEF_ADMIN_ACTIONS
    rest_path = re.compile(r"/(orgs|enterprises)/\{(org|enterprise)\}/[a-z_/{}\-]+\Z")
    for action_id, a in catalog.ADMIN_ACTIONS.items():
        assert a.action_id == action_id
        assert a.where in ADMIN_ACTION_WHERE
        assert a.doc_url.startswith("https://") and " " not in a.doc_url
        assert 0 < len(a.template) <= 400 and "p_" not in a.template
        if action_id.startswith("rest:"):
            assert a.where == "REST" and a.rest_method in ("GET", "POST", "PATCH", "PUT",
                                                             "DELETE")
            assert a.rest_path is not None and rest_path.match(a.rest_path), a.rest_path
            assert a.auth_note  # §19.4: every emitted request names its auth
        else:
            assert a.rest_method is None and a.rest_path is None
    seats = catalog.ADMIN_ACTIONS["rest:org_selected_users_delete"]
    assert (seats.rest_method, seats.rest_path) == ("DELETE",
                                                    "/orgs/{org}/copilot/billing/selected_users")
    assert "manage_billing:copilot" in (seats.auth_note or "")
    budgets = catalog.ADMIN_ACTIONS["rest:budget_create"]
    assert budgets.rest_path == "/enterprises/{enterprise}/settings/billing/budgets"
    assert catalog.ADMIN_ACTIONS["admin:plan_confirm"].where == "enterprise settings"


# ---------- K-3: data accessors ----------


def test_promotions_are_a_separate_table() -> None:
    assert [p.promotion_id for p in catalog.PROMOTIONS] == ["openai.gpt-5.6-sol.2026-08"]
    assert [p.promotion_id for p in catalog.COPILOT_PROMOTIONS] == [
        "github.gemini-3.6-flash.2026", "github.gemini-3.7-flash.2026",
        "github.gemini-3.8-flash.2026", "github.gpt-5.6-sol.2026-08"]
    assert all(p.channel == "github_copilot" for p in catalog.COPILOT_PROMOTIONS)
    promo = catalog.copilot_promotion_for("gemini-3.8-flash", "2026-12-31")
    assert promo is not None and promo.promotion_id == "github.gemini-3.8-flash.2026"
    assert catalog.copilot_promotion_for("gemini-3.8-flash", "2026-09-02") is None
    assert catalog.copilot_promotion_for("gpt-5.6-sol", "2026-09-03") is not None
    assert catalog.copilot_promotion_for("gpt-5.6-sol", "2026-09-04") is None
    assert catalog.promotion_for("gpt-5.6-sol", "github_copilot", "2026-08-25") is not None
    assert catalog.promotion_for("gpt-5.6-sol", "openai_api", "2026-09-01").promotion_id == (
        "openai.gpt-5.6-sol.2026-08")


def test_retirements_and_priors() -> None:
    assert catalog.COPILOT_RETIREMENTS["claude-opus-4-7"] == ("2026-10-02", "claude-opus-5")
    assert catalog.COPILOT_RETIREMENTS["gpt-5.5"] == ("2026-10-19", "gpt-5.6-sol")
    assert catalog.COPILOT_RETIREMENTS["claude-opus-4-5"] == ("2026-09-01", None)
    assert len(catalog.COPILOT_RETIREMENTS) == 13
    assert "claude-opus-4-7" not in catalog.RETIREMENTS or catalog.RETIREMENTS[
        "claude-opus-4-7"] != "2026-10-02"  # the SPEC lifecycle table is separate
    assert catalog.RR_PRIORS["rate"] == (Decimal(1), Decimal(1), Decimal(1))
    assert catalog.RR_PRIORS["trajectory"] == (Decimal("-0.2"), Decimal("0.5"), Decimal(1))
    assert catalog.RR_PRIORS["behavioral"] is None
    assert set(catalog.RR_PRIORS) == {"rate", "cache_transform", "trajectory", "behavioral"}


def test_copilot_allowance() -> None:
    assert catalog.copilot_allowance("business", "2026-09") == (Decimal(1900), None)
    assert catalog.copilot_allowance("enterprise", "2026-10") == (Decimal(3900), None)
    assert catalog.copilot_allowance("business", "2026-06") == (
        Decimal(3000), "copilot.promo.business.2026-06")
    assert catalog.copilot_allowance("enterprise", "2026-08") == (
        Decimal(7000), "copilot.promo.enterprise.2026-06")
    assert catalog.copilot_allowance("enterprise", "2026-08", promo_eligible=False) == (
        Decimal(3900), None)
    assert catalog.copilot_allowance("business", "2026-05")[1] is None
    for plan in ("unknown", "mixed", "pro", None):
        with pytest.raises(UsageError):
            catalog.copilot_allowance(plan, "2026-09")  # type: ignore[arg-type]
    for month in ("2026-13", "2026-9", "2026-09-01", None):
        with pytest.raises(UsageError):
            catalog.copilot_allowance("business", month)  # type: ignore[arg-type]


def test_sku_accessors() -> None:
    cost = catalog.copilot_cost_type
    assert cost("copilot_ai_credit", username_present=True) == "ai_credit.user"
    assert cost("copilot_ai_credit", username_present=False) == "ai_credit.direct"
    assert cost("coding_agent_ai_credit", username_present=False) == "ai_credit.direct"
    assert cost("copilot_premium_request", username_present=True) == "ai_credit.legacy_pru"
    assert cost("copilot_enterprise", username_present=True) == "seat"
    assert cost("code_quality_licenses", username_present=False) == "code_quality.license"
    assert cost("sandbox_memory", username_present=False) == "sandbox"
    assert cost("actions_linux", username_present=False) == "actions"
    assert cost("linux_16_core", username_present=False) == "actions"
    assert cost("actions_linux_16_core", username_present=False) == "actions"
    assert cost("mystery_sku", username_present=True) == "other"
    assert cost(None, username_present=True) == "other"
    assert cost("spark_ai_credits", username_present=True) == cost(
        "spark_ai_credit", username_present=True) == "ai_credit.user"  # both spellings (VERIFY)
    assert all(cost(sku, username_present=b) in records.GITHUB_COST_TYPES
               for sku in load_facts().copilot.skus for b in (True, False))
    assert catalog.copilot_seat_plan("copilot_for_business") == "business"
    assert catalog.copilot_seat_plan("copilot_enterprise") == "enterprise"
    assert catalog.copilot_seat_plan("copilot_standalone") == "business"  # VERIFY in facts
    assert catalog.copilot_seat_plan("copilot_ai_credit") is None
    assert catalog.copilot_seat_plan(None) is None


def test_workloads_categories_remaps_runners() -> None:
    wl = catalog.copilot_workload
    assert wl("dynamic/copilot-swe-agent/copilot") == "copilot_cloud_agent"
    assert wl("dynamic/agents/copilot-pull-request-reviewer") == "copilot_code_review"
    assert wl("dynamic/github-code-quality/codeql") == "code_quality"
    assert wl(".github/workflows/triage.lock.yml") == "agentic_workflow"
    assert wl(".github/workflows/sub/triage.lock.yml") is None  # * stays within one segment
    assert wl(".github/workflows/triage.yml") is None
    assert wl("") is None and wl(None) is None
    assert catalog.copilot_category("claude-opus-5-5") == "Powerful"
    assert catalog.copilot_category("gpt-5-mini") == "Lightweight"
    assert catalog.copilot_category("no-such-model") is None
    assert catalog.copilot_category(None) is None
    assert catalog.copilot_remap("claude-opus-4-7") == ("claude-sonnet-5", True)
    assert catalog.copilot_remap("gpt-5.5") == ("gpt-5.6-terra", False)
    assert catalog.copilot_remap("claude-sonnet-5") is None
    assert catalog.copilot_remap(None) is None
    rate = catalog.runner_rate("actions_linux_16_core")
    assert rate is not None and rate.usd_per_minute == Decimal("0.042")
    assert catalog.runner_rate("linux_16_core") is rate
    assert catalog.runner_rate("actions_linux").runner_class == "standard"  # type: ignore[union-attr]
    assert catalog.runner_rate("linux") is catalog.runner_rate("actions_linux")
    assert catalog.runner_rate("quantum_runner") is None
    assert catalog.runner_rate(None) is None and catalog.runner_rate("") is None


@pytest.mark.parametrize("raw,family", [
    ("vscode/1.77.3/copilot/1.86.82", "vscode"),
    ("VS Code 1.89.1", "vscode"),
    ("Visual Studio Code 1.99", "vscode"),
    ("Visual Studio 17.10", "visual_studio"),
    ("visualstudio/17.10/copilot/1.2", "visual_studio"),
    ("JetBrains-IC/241.14494", "jetbrains"),
    ("IntelliJ IDEA 2024.2", "jetbrains"),
    ("PyCharm 2024.1", "jetbrains"),
    ("intellij", "jetbrains"),
    ("ide:intellij", "jetbrains"),
    ("xcode/15.0", "xcode"),
    ("eclipse/4.31", "eclipse"),
    ("neovim/0.9.5", "neovim"),
    ("vim/9.1", "neovim"),
    ("Copilot CLI 1.0.70", "cli"),
    ("copilot-cli/1.0.70", "cli"),
    ("Copilot Chat", "github_com"),
    ("github.com", "github_com"),
    ("GitHub Copilot app", "copilot_app"),
    ("GitHub Mobile 1.170", "mobile"),
    ("jetbrains", "jetbrains"),
    ("copilot_app", "copilot_app"),
    ("Unspecified", "other"),
    ("  ", "other"),
    ("", "other"),
    (None, "other"),
    ("emacs/29", "other"),
])
def test_editor_family(raw: str | None, family: str) -> None:
    assert catalog.editor_family(raw) == family
    assert family in catalog.EDITOR_FAMILIES


def test_agent_family_and_reexports() -> None:
    for product in ("copilot_vscode", "copilot_jetbrains", "copilot_cli", "copilot_gh_aw",
                    "copilot_other", "copilot_future"):
        assert catalog.agent_family(product) == "copilot"
    for product in ("claude_code", "agent_sdk", "", None):
        assert catalog.agent_family(product) == "default"
    assert catalog.FAMILIES == ("default", "copilot")
    assert catalog.ACTIVITY_KEYS is records.ACTIVITY_KEYS
    assert catalog.ACTIVITY_FLAGS is records.ACTIVITY_FLAGS
    assert catalog.CONFIG_KEYS is records.CONFIG_KEYS
    assert catalog.EDITOR_FAMILIES is records.EDITOR_FAMILIES


# ---------- fixes, exclusions, count sources ----------


def test_fix_for_targets_github_copilot() -> None:
    table = catalog._COPILOT_FIXES
    assert len(table) == 48
    for (detector_id, kind), fix in table.items():
        assert isinstance(fix, Fix)
        assert fix.target == "github-copilot", (detector_id, kind)
        assert fix.doc_url is not None and fix.doc_url.startswith("https://")
        assert 0 < len(fix.text) <= 400 and "p_" not in fix.text
        for key, value in fix.config_patch or ():
            assert key in catalog.COPILOT_ALLOWLIST, (detector_id, kind, key)
            assert value.startswith('"') and value.endswith('"')  # JSON strings
        assert detector_id in reg.BUILTIN_DETECTORS, detector_id
        got = catalog.fix_for(detector_id, kind or "any-kind", "copilot")
        assert got is not None
    miss = catalog.fix_for("cache.miss-by-cause", "model-switch", "copilot")
    assert miss is catalog.fix_for("cache.miss-by-cause", "param-change", "copilot")
    assert miss is not None and "/new" in miss.text and "CLAUDE_CODE" not in miss.text
    auto = catalog.fix_for("copilot.org-scan", "auto-adoption", "copilot")
    assert auto is not None and auto.config_patch == (("copilot.managed.model", '"auto"'),)
    assert catalog.fix_for("model.routing", "effort-mix", "copilot").config_patch == (  # type: ignore[union-attr]
        ("copilot.repo.effortLevel", '"medium"'),)
    assert catalog.fix_for("cache.miss-by-cause", "model-switch", "default") is None
    assert catalog.fix_for("model.routing", "rebaseline", "copilot") is None  # no Copilot text
    assert catalog.fix_for("failure.path", "retry-storm", "copilot") is None
    for kind in ("idle-seat", "plan-status", "budget-zero-user-budget", "pool-regime"):
        assert catalog.fix_for("copilot.seats-budgets", kind, "copilot") is not None
    for kind in ("long-context-band", "compaction-cost", "static-overhead", "subagent-share",
                 "ci-uncapped"):
        assert catalog.fix_for("copilot.lanes", kind, "copilot") is not None


def _declared_kinds(detector_id: str) -> set[str]:
    try:
        cls = reg.load(reg.BUILTIN_DETECTORS[detector_id])
    except ImportError:
        return SPEC_KINDS[detector_id]
    return set(cls.kinds)


def test_family_exclusions_name_registered_detectors_and_kinds() -> None:
    assert len(catalog.FAMILY_EXCLUSIONS) == 11
    for (detector_id, kind), families in catalog.FAMILY_EXCLUSIONS.items():
        assert detector_id in reg.BUILTIN_DETECTORS, detector_id
        assert families == frozenset({"copilot"})
        assert families <= set(catalog.FAMILIES)
        if kind is not None:
            assert kind in _declared_kinds(detector_id), (detector_id, kind)
    assert ("cache.ttl-advisor", None) in catalog.FAMILY_EXCLUSIONS
    assert ("premium.modifiers", "fast-premium") in catalog.FAMILY_EXCLUSIONS
    assert reg._family_exclusions() is catalog.FAMILY_EXCLUSIONS  # what run_detectors reads


def test_count_sources() -> None:
    assert set(catalog.COUNT_SOURCE.values()) <= set(catalog.COUNT_SOURCES)
    for detector_id, _kind in catalog.COUNT_SOURCE:
        assert detector_id in reg.BUILTIN_DETECTORS
    assert catalog.count_source("copilot.seats-budgets", "plan-status") == "entity"
    assert catalog.count_source("copilot.seats-budgets", "dq.skipped-kinds") == "entity"
    assert catalog.count_source("copilot.org-scan", "dq.skipped-kinds") == "entity"
    assert catalog.count_source("copilot.seats-budgets", "idle-seat") == "licenses"
    assert catalog.count_source("copilot.seats-budgets", "budget-zero-user-budget") == "licenses"
    assert catalog.count_source("copilot.org-scan", "premium-model-share") == "cost_lines"
    assert catalog.count_source("copilot.org-scan", "editor-mix") == "activity"
    assert catalog.count_source("copilot.lanes", "ci-uncapped") == "requests"
    assert catalog.count_source("cache.miss-by-cause", "ttl-expiry") == "requests"  # default
    seat_kinds = {"idle-seat", "seat-auto-assign", "completions-only-seat", "plan-mix",
                  "duplicate-seat"}
    assert all(catalog.COUNT_SOURCE[("copilot.seats-budgets", k)] == "licenses"
               for k in seat_kinds)
