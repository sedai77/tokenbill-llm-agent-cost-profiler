"""SPEC §11.3 / §11.4: policy packs — allowlist, VERIFY keys, trade-offs, merge and rollback
patches, cohorts, OTEL tags, the hook, LiteLLM, snippets, rendering and the content canary."""

from __future__ import annotations

import json
import types
from decimal import Decimal

import pytest

from tokenbill.core import catalog
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure
from tokenbill.core.types import (
    ActionPlan,
    ContractOverlay,
    EvidenceItem,
    Fix,
    LeverResult,
    PolicyPack,
)
from tokenbill.plan.litellm import validate_litellm_fragment
from tokenbill.plan.policy_pack import (
    HOOK_COMMAND,
    HOOK_PATH,
    LITELLM_FILE,
    apply_merge_patch,
    build_policy_packs,
    canonical_json,
    make_merge_patch,
    render_pack,
    snippet,
)

from .helpers import MAIN_SEL, est, finding

WF = "agent_product:claude_code,lane_kind:workflow_agent"
SUB = "agent_product:claude_code,lane_kind:subagent"


def _fig(nano: int, basis: Basis = Basis.LIST, **kw) -> Figure:
    return Figure(nano=nano, evidence=Evidence.ESTIMATED, basis=basis,
                  calibration=Calibration.UNCALIBRATED, note="test", **kw)


def lever_result(lever_id: str, params: str, *, nano: int = 1_000_000_000,
                 group: str = "billed:g1", basis: Basis = Basis.LIST,
                 finding_ids: tuple[str, ...] = ()) -> LeverResult:
    lv = catalog.lever(lever_id)
    fig = _fig(nano, basis, low_nano=nano // 2, high_nano=nano)
    return LeverResult(lever_id=lever_id, lever_class=lv.lever_class, params=params, basis=basis,
                       standalone=_fig(nano, basis), shapley=_fig(nano, basis),
                       projected_monthly=fig, needs_eval=lv.needs_eval,
                       upper_bound=lv.upper_bound, group=group, finding_ids=finding_ids)


def plan_of(*levers: LeverResult) -> ActionPlan:
    zero = Figure(nano=0, evidence=Evidence.ESTIMATED, basis=Basis.LIST, note="test")
    return ActionPlan(joint_saving=zero, headline_monthly=zero, allowance_headroom_monthly=None,
                      levers=tuple(levers), groups=(), method="shapley-exact", shapley_se=(),
                      sample="test")


TTL = lever_result("cc.prompt_cache_ttl.main", f"ttl=1h@{MAIN_SEL}")
COMPACT = lever_result("cc.autocompact_window", "compact-window=400000,post=20283",
                       group="trade-off")
MODEL = lever_result("cc.default_model", f"model=claude-sonnet-5@{MAIN_SEL}", group="trade-off")
HOOK = lever_result("cc.cold_resume_hook", "", group="behavioral")
FAST = lever_result("cc.fast_mode_opt_in", "fast=off")


def _packs(plan, findings=(), **kw):
    args = dict(target="claude-code", current=None, cohort_by=None, include_tradeoffs=False,
                contract=None)
    args.update(kw)
    return build_policy_packs(plan, list(findings), **args)


def _patch(pack: PolicyPack) -> dict:
    return json.loads(pack.merge_patch_json)


def _unverified(monkeypatch, *keys: str) -> None:
    table = dict(catalog.ALLOWLIST)
    for key in keys:
        entry = table[key]
        table[key] = catalog.AllowedKey(
            key=entry.key, target=entry.target, domain=entry.domain,
            min_version=entry.min_version, verified=False, lever_id=entry.lever_id,
            tradeoff=entry.tradeoff, source=entry.source)
    monkeypatch.setattr(catalog, "ALLOWLIST", types.MappingProxyType(table))


# ---------------------------------------------------------------------------------------------
# keys, VERIFY and trade-offs
# ---------------------------------------------------------------------------------------------


def test_compaction_lever_pairs_the_setting_with_the_env_variable() -> None:
    (pack,) = _packs(plan_of(COMPACT), include_tradeoffs=True)
    patch = _patch(pack)
    assert patch["autoCompactWindow"] == 400000
    assert patch["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"
    keys = {e.key for e in pack.entries}
    assert {"autoCompactWindow", "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"} <= keys
    entry = next(e for e in pack.entries if e.key == "autoCompactWindow")
    assert entry.needs_eval and entry.verified_key and entry.lever_id == "cc.autocompact_window"
    assert entry.projection == COMPACT.projected_monthly
    assert "trade-off" in entry.note and "measure plan --design its" in entry.note
    (off,) = _packs(plan_of(COMPACT))
    assert "autoCompactWindow" not in _patch(off) and "env" not in _patch(off)
    assert '// "autoCompactWindow": 400000' in off.readme_md
    assert '// "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"' in off.readme_md
    assert "`ab` or `measure`" in off.readme_md


def test_ttl_lever_goes_to_json_when_verified_and_to_the_readme_otherwise(monkeypatch) -> None:
    (pack,) = _packs(plan_of(TTL))
    assert _patch(pack)["promptCacheTtl"] == "1h"
    entry = next(e for e in pack.entries if e.key == "promptCacheTtl")
    assert entry.verified_key and entry.min_version == "2.1.242" and entry.value_json == '"1h"'
    assert "tokenbill policy check-effect --lever cc.prompt_cache_ttl.main" in pack.readme_md
    assert "tokenbill measure plan --lever cc.prompt_cache_ttl.main" in pack.readme_md
    _unverified(monkeypatch, "promptCacheTtl")
    (pack,) = _packs(plan_of(TTL))
    assert "promptCacheTtl" not in _patch(pack)
    entry = next(e for e in pack.entries if e.key == "promptCacheTtl")
    assert not entry.verified_key
    assert "VERIFY against settings-reference before applying" in entry.note
    assert '// "promptCacheTtl": "1h"' in pack.readme_md
    assert "VERIFY against settings-reference before applying" in pack.readme_md


def test_subagent_ttl_and_other_lever_keys() -> None:
    sub = lever_result("cc.prompt_cache_ttl.subagent", f"ttl=1h@{SUB};ttl=1h@{WF}")
    tool = lever_result("cc.tool_search", "", group="projection-only")
    same = lever_result("model.same_tier_upgrade",
                        "model=claude-fable-5-1@model:claude-fable-5;"
                        "model=claude-opus-5-5@model:claude-opus-5", group="trade-off")
    subm = lever_result("cc.subagent_model", "model=claude-haiku-4-5@lane_kind:subagent",
                        group="trade-off")
    eff = lever_result("cc.default_effort", f"effort=low,scale=0.25@{MAIN_SEL}",
                       group="trade-off")
    mx = lever_result("cc.max_effort", "effort=medium,scale=0.5", group="trade-off")
    (pack,) = _packs(plan_of(sub, tool, same, subm, eff, mx, FAST), include_tradeoffs=True)
    patch = _patch(pack)
    assert patch["subagentPromptCacheTtl"] == "1h"
    assert patch["fastModePerSessionOptIn"] is True
    assert patch["effortLevel"] == "low" and patch["maxEffortLevel"] == "medium"
    env = patch["env"]
    assert env["ENABLE_TOOL_SEARCH"] == "true"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-5-5"
    assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in env        # no managed key for that tier
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-haiku-4-5"
    assert next(e for e in pack.entries if e.key == "maxEffortLevel").min_version == "2.1.267"


def test_default_model_is_a_readme_comment_and_available_models_needs_tradeoffs(
        monkeypatch) -> None:
    (off,) = _packs(plan_of(MODEL))
    assert "model" not in _patch(off) and "availableModels" not in _patch(off)
    assert '// "model": "claude-sonnet-5"' in off.readme_md
    assert "availableModels" not in off.readme_md
    assert all(e.key not in ("model", "availableModels") for e in off.entries)
    (on,) = _packs(plan_of(MODEL), include_tradeoffs=True)
    patch = _patch(on)
    assert patch["model"] == "claude-sonnet-5"                 # verified in facts.json
    assert patch["availableModels"] == ["claude-sonnet-5"]
    assert patch["enforceAvailableModels"] is True
    _unverified(monkeypatch, "model")
    (on,) = _packs(plan_of(MODEL), include_tradeoffs=True,
                   current={"availableModels": ["claude-opus-5-5"]})
    patch = _patch(on)
    assert "model" not in patch
    assert patch["availableModels"] == ["claude-opus-5-5", "claude-sonnet-5"]
    assert '// "model": "claude-sonnet-5"' in on.readme_md
    (off,) = _packs(plan_of(MODEL))
    assert "availableModels" not in off.readme_md and '"model"' in off.readme_md


def test_unknown_keys_and_bad_values_raise() -> None:
    bad_key = finding("x", ["cc.prompt_cache_ttl.main"], fix=Fix(
        text="t", config_patch=(("notARealSetting", "true"),),
        target="claude-code-managed-settings", doc_url=None))
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), [bad_key])
    copilot_key = next(iter(catalog.COPILOT_ALLOWLIST))
    other_target = finding("x", [], fix=Fix(text="t", config_patch=((copilot_key, '"x"'),),
                                            target=None, doc_url=None))
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), [other_target])
    for key, value in (("promptCacheTtl", '"2h"'), ("autoCompactWindow", "5"),
                       ("model", '"bad model\\u0007"'), ("promptCacheTtl", "not json"),
                       ("bashOutputMaxChars", "NaN"), ("env.MAX_MCP_OUTPUT_TOKENS", "null"),
                       ("claudeMdExcludes", "[1]")):
        f = finding("x", [], fix=Fix(text="t", config_patch=((key, value),),
                                     target="claude-code-managed-settings", doc_url=None))
        with pytest.raises(UsageError):
            _packs(plan_of(), [f])


def test_finding_fixes_are_delivered_and_other_targets_ignored() -> None:
    carry = finding("tool-output-carry", [], detector="attrib.carry", fix=Fix(
        text="t", config_patch=(("bashOutputMaxChars", "20000"),
                                ("skillListingBudgetFraction", "0.005")),
        target="claude-code-managed-settings", doc_url=None))
    sdk = finding("keepalive-recommended", ["sdk.keepalive"], fix=Fix(
        text="t", config_patch=(("notAKey", "1"),), target="sdk", doc_url=None))
    (pack,) = _packs(plan_of(), [carry, sdk])
    patch = _patch(pack)
    assert patch["bashOutputMaxChars"] == 20000
    assert patch["skillListingBudgetFraction"] == 0.005
    assert '"skillListingBudgetFraction":0.005' in pack.merge_patch_json
    entry = next(e for e in pack.entries if e.key == "bashOutputMaxChars")
    assert entry.lever_id == "fix:attrib.carry" and entry.min_version == "2.1.261"
    assert "sdk.keepalive" in pack.readme_md          # the SDK snippet, not the bogus key


# ---------------------------------------------------------------------------------------------
# merge patch and rollback
# ---------------------------------------------------------------------------------------------


CURRENT = {
    "permissions": {"deny": ["Bash(rm:*)"]},
    "promptCacheTtl": "5m",
    "env": {"FOO": "1", "OTEL_RESOURCE_ATTRIBUTES": "team.id=payments,tokenbill.arm=old"},
    "hooks": {"SessionStart": [{"matcher": "startup",
                                "hooks": [{"type": "command", "command": "echo hi"}]}]},
    "cleanupPeriodDays": 30,
}


def test_merge_patch_keeps_unrelated_keys_and_rollback_restores_the_original() -> None:
    plan = plan_of(TTL, COMPACT, HOOK, FAST)
    (pack,) = _packs(plan, include_tradeoffs=True, current=CURRENT)
    patch, rollback = _patch(pack), json.loads(pack.rollback_patch_json)
    assert "permissions" not in patch and "cleanupPeriodDays" not in patch   # only changes
    patched = apply_merge_patch(CURRENT, patch)
    assert patched["permissions"] == CURRENT["permissions"]
    assert patched["env"]["FOO"] == "1"
    assert patched["promptCacheTtl"] == "1h"
    assert patched["hooks"]["SessionStart"][0] == CURRENT["hooks"]["SessionStart"][0]
    assert patched["hooks"]["SessionStart"][1]["hooks"][0]["command"] == HOOK_COMMAND
    attrs = patched["env"]["OTEL_RESOURCE_ATTRIBUTES"]
    assert attrs.startswith("team.id=payments,tokenbill.arm=") and "arm=old" not in attrs
    assert attrs.endswith(",tokenbill.wave=1")
    assert apply_merge_patch(patched, rollback) == CURRENT
    assert rollback["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] is None
    assert rollback["promptCacheTtl"] == "5m"
    assert "rollback: restore `promptCacheTtl` to `\"5m\"`" in pack.readme_md
    assert "rollback: remove `autoCompactWindow` (it was not set)" in pack.readme_md


def test_rollback_of_a_fresh_install_removes_whole_sections() -> None:
    (pack,) = _packs(plan_of(TTL, HOOK))
    patch, rollback = _patch(pack), json.loads(pack.rollback_patch_json)
    patched = apply_merge_patch({}, patch)
    assert apply_merge_patch(patched, rollback) == {}
    assert rollback["env"] is None and rollback["hooks"] is None


def test_merge_patch_helpers_follow_rfc_7386() -> None:
    assert apply_merge_patch({"a": "b"}, {"a": "c"}) == {"a": "c"}
    assert apply_merge_patch({"a": "b"}, {"b": "c"}) == {"a": "b", "b": "c"}
    assert apply_merge_patch({"a": "b"}, {"a": None}) == {}
    assert apply_merge_patch({"a": ["b"]}, {"a": "c"}) == {"a": "c"}
    assert apply_merge_patch({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}) == {"a": {"b": "d"}}
    assert apply_merge_patch(["a"], {"a": "b"}) == {"a": "b"}
    assert apply_merge_patch({"a": "b"}, ["c"]) == ["c"]
    assert make_merge_patch({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 3}}) == {"b": {"c": 3}}
    assert make_merge_patch({"a": 1}, {}) == {"a": None}
    assert make_merge_patch([1], {"x": 1}) == {"x": 1}
    assert canonical_json({"b": [Decimal("0.50"), 2, True, None], "a": "é"}) == \
        '{"a":"é","b":[0.5,2,true,null]}'
    assert canonical_json({"x": {}}, indent=2) == '{\n  "x": {}\n}'


@pytest.mark.parametrize("current", [
    {"env": "not-an-object"}, {"hooks": []}, {"a": None}, {1: "x"}, {"a": float("nan")},
    "not a mapping", {"a": object()},
])
def test_invalid_current_settings_raise(current) -> None:
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), current=current)


def test_float_values_in_current_are_kept_without_floats() -> None:
    (pack,) = _packs(plan_of(TTL), current={"skillListingBudgetFraction": 0.25})
    assert "skillListingBudgetFraction" not in _patch(pack)
    assert json.loads(pack.rollback_patch_json)["promptCacheTtl"] is None


def test_bad_otel_attributes_in_current_raise() -> None:
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), current={"env": {"OTEL_RESOURCE_ATTRIBUTES": "a b c"}})


# ---------------------------------------------------------------------------------------------
# cohorts
# ---------------------------------------------------------------------------------------------


def _ttl_fix(ttl: str) -> Fix:
    return Fix(text="t", config_patch=(("promptCacheTtl", f'"{ttl}"'),),
               target="claude-code-managed-settings", doc_url=None)


def test_heterogeneity_findings_force_per_cohort_packs() -> None:
    hetero = finding("ttl-heterogeneous", ["cc.prompt_cache_ttl.main"], team="payments",
                     detector="cache.ttl-advisor", lever_class="cache_transform",
                     fix=_ttl_fix("1h"), projected=est(4_000_000_000), lane_kind="main")
    other = finding("ttl-5m-recommended", ["cc.prompt_cache_ttl.main"], team="search",
                    detector="cache.ttl-advisor", lever_class="cache_transform",
                    fix=_ttl_fix("5m"), lane_kind="main")
    packs = _packs(plan_of(TTL, FAST), [hetero, other])
    assert [p.cohort for p in packs] == ["all", "payments", "search"]
    everyone, payments, search = packs
    assert "promptCacheTtl" not in _patch(everyone)
    assert _patch(everyone)["fastModePerSessionOptIn"] is True
    assert _patch(payments)["promptCacheTtl"] == "1h"
    assert _patch(search)["promptCacheTtl"] == "5m"
    entry = next(e for e in payments.entries if e.key == "promptCacheTtl")
    assert entry.projection == hetero.projected_monthly
    assert "heterogeneous" in entry.note and "deliver to this cohort only" in entry.note
    assert "--cohort payments" in payments.readme_md
    assert hetero.finding_id in payments.readme_md
    s_entry = next(e for e in search.entries if e.key == "promptCacheTtl")
    assert s_entry.projection is None
    assert "not projected for this cohort" in search.readme_md


def test_cohort_by_mdm_group_and_explicit_team() -> None:
    f = finding("ttl-1h-recommended", ["cc.prompt_cache_ttl.main"], team="t1",
                detector="cache.ttl-advisor", fix=_ttl_fix("1h"),
                extra_dims={"mdm_group": "eng-laptops"})
    packs = _packs(plan_of(TTL), [f], cohort_by="mdm-group")
    assert [p.cohort for p in packs] == ["eng-laptops"]
    packs = _packs(plan_of(TTL), [f], cohort_by="team")
    assert [p.cohort for p in packs] == ["t1"]
    packs = _packs(plan_of(TTL), [f])
    assert [p.cohort for p in packs] == ["all"]
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), [f], cohort_by="department")
    bad = finding("ttl-1h-recommended", ["cc.prompt_cache_ttl.main"], team="a/b",
                  detector="cache.ttl-advisor", fix=_ttl_fix("1h"))
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), [bad], cohort_by="team")


def test_conflicting_values_keep_the_plan_value_and_are_listed() -> None:
    f = finding("ttl-5m-recommended", ["cc.prompt_cache_ttl.main"], detector="cache.ttl-advisor",
                fix=_ttl_fix("5m"))
    (pack,) = _packs(plan_of(TTL), [f])
    assert _patch(pack)["promptCacheTtl"] == "1h"
    assert "## Conflicts" in pack.readme_md


# ---------------------------------------------------------------------------------------------
# OTEL tags, hook, LiteLLM, snippets, model pricing, versions
# ---------------------------------------------------------------------------------------------


def test_otel_tags_name_the_arms_and_the_wave() -> None:
    (pack,) = _packs(plan_of(TTL, FAST))
    assert pack.otel_resource_attributes == (
        "tokenbill.arm=cc.fast_mode_opt_in+cc.prompt_cache_ttl.main,tokenbill.wave=1")
    assert _patch(pack)["env"]["OTEL_RESOURCE_ATTRIBUTES"] == pack.otel_resource_attributes
    otel = next(e for e in pack.entries if e.key == "env.OTEL_RESOURCE_ATTRIBUTES")
    assert otel.lever_id == "verification" and otel.projection is None


def test_hook_is_shipped_with_the_cold_resume_levers() -> None:
    (pack,) = _packs(plan_of(HOOK))
    paths = dict(pack.hooks)
    assert HOOK_PATH in paths and "def main()" in paths[HOOK_PATH]
    assert _patch(pack)["hooks"]["SessionStart"] == [
        {"matcher": "resume", "hooks": [{"type": "command", "command": HOOK_COMMAND}]}]
    assert "SessionStart hook" in pack.readme_md
    ceiling = lever_result("cc.compact_on_resume", "cold-resume=compact,min=200000",
                           group="ceiling-only")
    (pack,) = _packs(plan_of(ceiling, HOOK))
    assert HOOK_PATH in dict(pack.hooks)
    packs = _packs(plan_of(ceiling))
    assert all(HOOK_PATH not in dict(p.hooks) for p in packs)
    (again,) = _packs(plan_of(HOOK), current={"hooks": {"SessionStart": _patch(pack)["hooks"][
        "SessionStart"]}})
    assert "hooks" not in _patch(again)      # already installed: nothing to change


def test_gateway_lever_ships_a_valid_litellm_fragment() -> None:
    gw = lever_result("gateway.restore_caching", "repair=restore_caching")
    nocache = finding("no-cache", ["gateway.restore_caching"], detector="cache.gateway-disabled",
                      model="claude-opus-5-5")
    (pack,) = _packs(plan_of(gw, TTL), [nocache])
    text = dict(pack.hooks)[LITELLM_FILE]
    validate_litellm_fragment(text)
    assert '"claude-opus-5-5"' in text and '# ttl: "1h"' in text
    assert "gateway.restore_caching" in pack.readme_md
    (lite,) = _packs(plan_of(gw), [nocache], target="litellm")
    assert lite.target == "litellm" and lite.merge_patch_json == "{}"
    assert dict(lite.hooks)[LITELLM_FILE].count("model_name") == 1
    (anon,) = _packs(plan_of(gw), target="litellm")
    validate_litellm_fragment(dict(anon.hooks)[LITELLM_FILE])
    assert _packs(plan_of(TTL), target="litellm") == []


def test_sdk_target_ships_snippets() -> None:
    ttl = lever_result("sdk.ttl", "ttl=1h@lane_kind:api_run")
    ka = lever_result("sdk.keepalive", "keepalive=240s,max=3600s@agent_product:agent_sdk")
    (pack,) = _packs(plan_of(ttl, ka), target="sdk")
    files = dict(pack.hooks)
    assert set(files) == {"snippets/sdk.ttl.md", "snippets/sdk.keepalive.md"}
    assert '"ttl": "1h"' in files["snippets/sdk.ttl.md"]
    assert pack.entries == () and pack.merge_patch_json == "{}"
    assert _packs(plan_of(TTL), target="sdk") == []
    assert snippet("no.such") is None and snippet("../etc") is None
    for lid in ("geo.global", "endpoint.global", "batch.eligible", "fanout.stagger",
                "retry.single_owner", "fallback.credit", "gateway.restore_caching",
                "ci.shared_prefix", "blocks.breakpoints", "sdk.defer_loading"):
        assert snippet(lid), lid


def test_model_pricing_from_the_contract() -> None:
    overlay = ContractOverlay(name="acme", multiplier=Decimal("0.85"), overrides=(),
                              effective_from="2026-09-01", effective_to=None, derived=False,
                              assumed_fields=())

    def emitter(contract):
        return {"multiplier": "0.85", "overrides": {"claude-opus-5-5": {"input": 3.4}}}

    (pack,) = _packs(plan_of(TTL), contract=overlay, model_pricing_emitter=emitter)
    patch = _patch(pack)
    assert patch["modelPricing"] == {"multiplier": 0.85,
                                     "overrides": {"claude-opus-5-5": {"input": 3.4}}}
    entry = next(e for e in pack.entries if e.key == "modelPricing")
    assert entry.lever_id == "reporting.model_pricing" and entry.min_version == "2.1.242"
    assert "managed settings only" in entry.note
    (pack,) = _packs(plan_of(TTL), contract=overlay)
    assert "modelPricing" not in _patch(pack)
    assert "no model-pricing emitter" in pack.readme_md
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), contract=overlay, model_pricing_emitter=lambda c: ["x"])
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), contract=overlay,
               model_pricing_emitter=lambda c: {"multiplier": 50})
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), contract="acme")


def test_min_client_version_is_noted() -> None:
    (pack,) = _packs(plan_of(TTL), min_client_version="2.1.200")
    entry = next(e for e in pack.entries if e.key == "promptCacheTtl")
    assert "clients below 2.1.242 ignore this key (fleet minimum 2.1.200)" in entry.note
    (pack,) = _packs(plan_of(TTL), min_client_version="2.1.250")
    assert "ignore this key" not in next(e for e in pack.entries
                                         if e.key == "promptCacheTtl").note
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), min_client_version="latest")


def test_targets_and_arguments_are_checked() -> None:
    with pytest.raises(UsageError, match="Copilot extension"):
        _packs(plan_of(TTL), target="github-copilot")
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), target="vim")
    with pytest.raises(UsageError):
        build_policy_packs("plan", [], target="claude-code", current=None, cohort_by=None,
                           include_tradeoffs=False, contract=None)
    with pytest.raises(UsageError):
        _packs(plan_of(TTL), ["finding"])
    assert _packs(plan_of()) == []


def test_allowance_projections_are_labeled_list_equivalent() -> None:
    allow = lever_result("cc.prompt_cache_ttl.main", f"ttl=1h@{MAIN_SEL}",
                         basis=Basis.LIST_EQUIVALENT, group="allowance:g1")
    (pack,) = _packs(plan_of(allow))
    assert "list-equivalent allowance headroom, not invoice dollars" in pack.readme_md
    unpriced_projection = LeverResult(
        lever_id=TTL.lever_id, lever_class=TTL.lever_class, params=TTL.params, basis=Basis.LIST,
        standalone=TTL.standalone, shapley=TTL.shapley,
        projected_monthly=Figure(nano=None, evidence=Evidence.ESTIMATED, basis=Basis.LIST,
                                 note="unpriced: a replay"),
        needs_eval=False, upper_bound=False, group="billed:g1", finding_ids=())
    (pack,) = _packs(plan_of(unpriced_projection))
    assert "unpriced (a replay)" in pack.readme_md


def test_packs_are_deterministic() -> None:
    plan = plan_of(TTL, COMPACT, HOOK, FAST, MODEL)
    assert _packs(plan, include_tradeoffs=True, current=CURRENT) == \
        _packs(plan, include_tradeoffs=True, current=CURRENT)


# ---------------------------------------------------------------------------------------------
# rendering and the canary
# ---------------------------------------------------------------------------------------------


def test_render_pack_writes_every_file(tmp_path) -> None:
    gw = lever_result("gateway.restore_caching", "repair=restore_caching")
    packs = _packs(plan_of(TTL, HOOK, gw))
    written = render_pack(packs[0], tmp_path)
    names = sorted(p.relative_to(tmp_path).as_posix() for p in written)
    assert names == ["all/README.md", "all/hooks/tokenbill_session_start.py",
                     "all/litellm-config.patch.yaml", "all/managed-settings.patch.json",
                     "all/rollback.patch.json"]
    patch = json.loads((tmp_path / "all" / "managed-settings.patch.json").read_text())
    assert patch == json.loads(packs[0].merge_patch_json)
    assert (tmp_path / "all" / "managed-settings.patch.json").read_text().startswith("{\n  ")
    hook = tmp_path / "all" / HOOK_PATH
    assert hook.stat().st_mode & 0o111
    lite = _packs(plan_of(gw), target="litellm")[0]
    assert [p.name for p in render_pack(lite, tmp_path / "lite")] == [
        "README.md", "litellm-config.patch.yaml"]


def test_render_pack_uses_safe_directory_names_and_refuses_unsafe_paths(tmp_path) -> None:
    f = finding("ttl-1h-recommended", ["cc.prompt_cache_ttl.main"], team="Team One:α",
                detector="cache.ttl-advisor", fix=_ttl_fix("1h"))
    (pack,) = _packs(plan_of(TTL), [f], cohort_by="team")
    written = render_pack(pack, tmp_path)
    assert {p.relative_to(tmp_path).parts[0] for p in written} == {"Team_One__"}
    for bad in ("../x", "/etc/passwd", "a\\b", "c:x", ""):
        evil = PolicyPack(target="sdk", cohort="all", merge_patch_json="{}",
                          rollback_patch_json="{}", entries=(), otel_resource_attributes="",
                          readme_md="x", hooks=((bad, "x"),))
        with pytest.raises(UsageError):
            render_pack(evil, tmp_path)
    with pytest.raises(UsageError):
        render_pack("pack", tmp_path)
    broken = PolicyPack(target="claude-code", cohort="..", merge_patch_json="{",
                        rollback_patch_json="{}", entries=(), otel_resource_attributes="",
                        readme_md="x", hooks=())
    with pytest.raises(UsageError):
        render_pack(broken, tmp_path)


def test_canary_never_reaches_a_pack(tmp_path) -> None:
    tainted = [
        finding("ttl-heterogeneous", ["cc.prompt_cache_ttl.main"], team="payments",
                detector="cache.ttl-advisor", fix=Fix(
                    text=f"fix {CANARY}", config_patch=(("promptCacheTtl", '"1h"'),),
                    target="claude-code-managed-settings", doc_url=f"https://x/{CANARY}")),
        finding("no-cache", ["gateway.restore_caching"], detector="cache.gateway-disabled"),
    ]
    tainted = [f.__class__(**{**{s: getattr(f, s) for s in f.__slots__},
                              "title": f"{CANARY} title", "summary": f"{CANARY} summary",
                              "evidence": (EvidenceItem(kind="aggregate", ref=CANARY,
                                                        attrs=(("x", CANARY),)),)})
               for f in tainted]
    gw = lever_result("gateway.restore_caching", "repair=restore_caching")
    packs = _packs(plan_of(TTL, HOOK, gw, COMPACT), tainted, include_tradeoffs=True,
                   current={"env": {"FOO": "1"}})
    assert packs
    for pack in packs:
        assert_no_canary(repr(pack), pack.readme_md, pack.merge_patch_json,
                         pack.rollback_patch_json, *(t for _, t in pack.hooks))
        for path in render_pack(pack, tmp_path):
            assert_no_canary(path.read_bytes())
