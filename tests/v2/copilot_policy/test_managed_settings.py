"""Managed-settings merge patch, rollback, allowlist, unverified and trade-off keys (addendum §11.3,
§11.4; brief acceptance 1)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from tokenbill.copilot import policy
from tokenbill.copilot.policy import (
    apply_merge_patch,
    build_copilot_packs,
    load_current,
    managed_settings_patch,
)
from tokenbill.core import catalog
from tokenbill.core.errors import UsageError
from tokenbill.core.types import Fix

from .helpers import finding, plan, result


def _pack(**kw: object):
    args: dict[str, object] = dict(plans_by_scenario=(("known", plan(result(
        "copilot.default_model_auto", 120))),), findings=[], actions=[],
        current={"model": "gpt-5.4"}, cohort_by=None, include_tradeoffs=False, teams=None,
        budgets=None)
    args.update(kw)
    return build_copilot_packs(**args)[0]  # type: ignore[arg-type]


def _hook(pack, path: str) -> str:
    return dict(pack.hooks)[path]


def test_merge_patch_sets_auto_and_rollback_restores_the_previous_model() -> None:
    pack = _pack()
    patch = json.loads(pack.merge_patch_json)
    rollback = json.loads(pack.rollback_patch_json)
    assert patch == {"model": "auto"}
    assert rollback == {"model": "gpt-5.4"}
    assert json.loads(_hook(pack, "copilot/managed-settings.patch.json")) == patch
    assert json.loads(_hook(pack, "copilot/rollback.patch.json")) == rollback
    current = {"model": "gpt-5.4", "deniedMcpServers": ["x"]}
    applied = apply_merge_patch(current, patch)
    assert applied == {"model": "auto", "deniedMcpServers": ["x"]}
    assert apply_merge_patch(applied, rollback) == current


def test_absent_keys_roll_back_to_null_and_unchanged_keys_are_not_patched() -> None:
    patch, rollback = managed_settings_patch({"copilot.managed.model": "auto"}, {})
    assert (patch, rollback) == ({"model": "auto"}, {"model": None})
    patch, rollback = managed_settings_patch({"copilot.managed.model": "auto"}, {"model": "auto"})
    assert (patch, rollback) == ({}, {})


def test_nested_telemetry_keys_and_a_non_object_previous_value() -> None:
    changes = {"copilot.managed.telemetry.enabled": True,
               "copilot.managed.telemetry.captureContent": False}
    patch, rollback = managed_settings_patch(changes, {"telemetry": {"enabled": False,
                                                                     "headers": {"a": "b"}}})
    assert patch == {"telemetry": {"captureContent": False, "enabled": True}}
    assert rollback == {"telemetry": {"captureContent": None, "enabled": False}}
    patch, rollback = managed_settings_patch(changes, {"telemetry": "off"})
    assert apply_merge_patch({"telemetry": "off"}, patch) == {
        "telemetry": {"captureContent": False, "enabled": True}}
    assert rollback == {"telemetry": "off"}


@pytest.mark.parametrize("key", ["copilot.managed.unknownKey", "promptCacheTtl", "model",
                                 "copilot.repo.effortLevel", "copilot.managed..model"])
def test_unknown_or_non_managed_keys_raise(key: str) -> None:
    with pytest.raises(UsageError):
        managed_settings_patch({key: "x"}, {})


def test_a_copilot_fix_with_an_unknown_key_raises() -> None:
    bad = Fix(text="t", config_patch=(("copilot.managed.bogus", '"x"'),),
              target="github-copilot", doc_url="https://docs.github.com")
    with pytest.raises(UsageError):
        _pack(findings=[finding("auto-adoption", team="t1", fix=bad)])
    bad_json = Fix(text="t", config_patch=(("copilot.managed.model", "not json"),),
                   target="github-copilot", doc_url="https://docs.github.com")
    with pytest.raises(UsageError):
        _pack(findings=[finding("auto-adoption", team="t1", fix=bad_json)])


def test_non_copilot_fix_targets_are_ignored() -> None:
    other = Fix(text="t", config_patch=(("promptCacheTtl", '"1h"'),),
                target="claude-code-managed-settings", doc_url=None)
    pack = _pack(findings=[finding("auto-adoption", team="t1", fix=other)])
    assert json.loads(pack.merge_patch_json) == {"model": "auto"}


def test_an_unverified_key_appears_only_as_a_readme_comment(monkeypatch) -> None:
    table = dict(catalog.COPILOT_ALLOWLIST)
    table["copilot.managed.model"] = dataclasses.replace(table["copilot.managed.model"],
                                                         verified=False)
    monkeypatch.setattr(catalog, "COPILOT_ALLOWLIST", table)
    pack = _pack()
    assert json.loads(pack.merge_patch_json) == {}
    assert json.loads(pack.rollback_patch_json) == {}
    for path, text in pack.hooks:
        if path.endswith(".json"):
            assert '"model"' not in text, path
    assert '// "model": "auto"   VERIFY against the managed-settings reference' in pack.readme_md
    entry = next(e for e in pack.entries if e.key == "copilot.managed.model")
    assert entry.verified_key is False


def test_fix_patch_keys_route_to_managed_repo_and_tradeoff_sections() -> None:
    effort = Fix(text="t", config_patch=(("copilot.repo.effortLevel", '"medium"'),),
                 target="github-copilot", doc_url="https://docs.github.com")
    tier = Fix(text="t", config_patch=(("copilot.repo.contextTier", '"default"'),),
               target="github-copilot", doc_url="https://docs.github.com")
    fs = [finding("effort-mix", detector="model.routing", fix=effort, team="t1"),
          finding("context-heavy-cli", fix=tier, team="t1")]
    pack = _pack(findings=fs)
    repo = json.loads(_hook(pack, "repo/.github/copilot/settings.patch.json"))
    assert repo == {"effortLevel": "medium"}
    assert "`copilot.repo.contextTier`" in pack.readme_md and "--include-tradeoffs" in \
        pack.readme_md
    assert "trusted directories" in pack.readme_md
    with_tradeoffs = _pack(findings=fs, include_tradeoffs=True)
    repo = json.loads(_hook(with_tradeoffs, "repo/.github/copilot/settings.patch.json"))
    assert repo == {"contextTier": "default", "effortLevel": "medium"}
    # the managed patch never carries a repository key
    assert set(json.loads(with_tradeoffs.merge_patch_json)) == {"model"}


def test_telemetry_keeps_the_default_service_name_unless_one_is_given() -> None:
    cache = finding("cache-health", team="t1")
    pack = _pack(findings=[cache])
    tel = json.loads(pack.merge_patch_json)["telemetry"]
    assert tel == {"captureContent": False, "enabled": True, "lockCaptureContent": True,
                   "serviceName": "github-copilot"}
    named = _pack(findings=[], otel_service_name="copilot-acme=copilot_jetbrains")
    assert json.loads(named.merge_patch_json)["telemetry"]["serviceName"] == "copilot-acme"
    assert "JetBrains support is documented inconsistently" in named.readme_md
    with pytest.raises(UsageError):
        _pack(otel_service_name="  ")


def test_teams_cohort_writes_overridable_auto_and_team_files() -> None:
    teams = {"Data Platform": {"n_people": 6, "ide:vscode": 10},
             "tiny": {"n_people": 2, "ide:vscode": 10}}
    packs = build_copilot_packs(
        (("known", plan(result("copilot.default_model_auto", 50))),), [], [],
        current={"model": "gpt-5.4"}, cohort_by="team", include_tradeoffs=False, teams=teams,
        budgets=None)
    main = packs[0]
    assert json.loads(main.merge_patch_json) == {"model": {"overridable": "auto"}}
    files = dict(main.hooks)
    team_files = [p for p in files if p.startswith("copilot/teams/")]
    assert len(team_files) == 1 and "data-platform" in team_files[0]
    assert json.loads(files[team_files[0]]) == {"model": "auto"}
    mapping = json.loads(files["copilot/team-mappings.patch.json"])
    assert list(mapping["teams"]) == ["Data Platform"]
    assert "tiny" not in json.dumps(mapping)
    assert [p.cohort for p in packs] == ["all", "Data Platform"]
    assert json.loads(packs[1].merge_patch_json) == {"model": "auto"}
    assert json.loads(packs[1].rollback_patch_json) == {"model": None}
    assert packs[1].otel_resource_attributes == \
        "tokenbill.arm=copilot.default_model_auto,tokenbill.wave=1"
    with pytest.raises(UsageError):
        build_copilot_packs((), [], [], current=None, cohort_by="mdm-group",
                            include_tradeoffs=False, teams=None, budgets=None)


def test_waves_spread_teams_and_team_files_carry_telemetry_attributes() -> None:
    teams = {f"team{i}": {"n_people": 5, "ide:intellij" if i < 2 else "ide:vscode": 10}
             for i in range(6)}
    packs = build_copilot_packs(
        (("known", plan(result("copilot.default_model_auto", 50))),),
        [finding("cache-health", team="t1")], [], current=None, cohort_by="team",
        include_tradeoffs=False, teams=teams, budgets=None)
    waves = sorted(int(p.otel_resource_attributes.rsplit("=", 1)[1]) for p in packs[1:])
    assert waves == [1, 1, 2, 2, 3, 3]
    team_file = json.loads(dict(packs[1].hooks)[packs[1].hooks[0][0]])
    assert team_file["telemetry"]["resourceAttributes"]["tokenbill.arm"] == \
        "copilot.default_model_auto"


def test_load_current_parses_objects_and_refuses_everything_else() -> None:
    assert load_current(None) == {} and load_current("  ") == {}
    assert load_current('﻿{"model": "gpt-5.4"}') == {"model": "gpt-5.4"}
    assert load_current(b'{"a": [1, 2]}') == {"a": [1, 2]}
    for bad in ("[1]", "{", '{"a": NaN}', b"\xff\xfe", "x" * (1 << 20 + 1), 5,
                "[" * 100_000):
        with pytest.raises(UsageError):
            load_current(bad)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        load_current(b"{}" + b" " * (1 << 20))


def test_current_must_be_json_data() -> None:
    with pytest.raises(UsageError):
        _pack(current={"model": object()})
    with pytest.raises(UsageError):
        _pack(current=["model"])


def test_apply_merge_patch_follows_rfc_7386() -> None:
    assert apply_merge_patch({"a": "b"}, {"a": "c"}) == {"a": "c"}
    assert apply_merge_patch({"a": "b"}, {"b": "c"}) == {"a": "b", "b": "c"}
    assert apply_merge_patch({"a": "b"}, {"a": None}) == {}
    assert apply_merge_patch({"a": [{"b": "c"}]}, {"a": [1]}) == {"a": [1]}
    assert apply_merge_patch(["a"], {"a": {"b": None}}) == {"a": {}}
    assert apply_merge_patch({"a": "b"}, ["c"]) == ["c"]


def test_entries_carry_reach_projection_and_versions() -> None:
    pack = _pack()
    entry = next(e for e in pack.entries if e.key == "copilot.managed.model")
    assert entry.projection is not None and entry.projection.nano == 120 * 10**9
    assert "NOT JetBrains" in entry.note and "$120.00/month" in entry.note
    assert entry.lever_id == "copilot.default_model_auto" and entry.verified_key
    assert pack.otel_resource_attributes == \
        "tokenbill.arm=copilot.default_model_auto,tokenbill.wave=0"
    assert policy.TARGET == pack.target == "github-copilot"
