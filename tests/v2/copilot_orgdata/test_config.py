"""github-copilot-config: budgets, user-states, cost centers and org Copilot settings (§5.4)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tokenbill.adapters.github_config import SEAT_MANAGEMENT_SETTINGS, CopilotConfigAdapter
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.records import CONFIG_KEYS, Attribution, ContentTier
from tokenbill.core.testing import assert_adapter_conforms, conformance_ingest_options

from .helpers import FIXTURES, assert_no_identity, attrs, blob, h, opts

CONFIG = FIXTURES / "config"
ADAPTER = CopilotConfigAdapter()


def _read(name: str, **kw: object):  # type: ignore[no-untyped-def]
    return ADAPTER.read(CONFIG / name, opts(**kw))


def _by_entity(result):  # type: ignore[no-untyped-def]
    return {(c.kind, c.entity_id): c for c in result.config}


def test_budgets_both_sku_spellings_three_types_and_amounts() -> None:
    res = _read("budgets.json")
    got = {c.entity_id: attrs(c) for c in res.config}
    assert len(got) == 8 and res.capabilities == frozenset({"config"})
    first = got["budget:b010000-0000-4000-8000-000000000001"]
    assert first["amount_nano"] == 1_000_000_000_000        # 1000.0 whole dollars
    assert first["sku"] == "actions" and first["type"] == "ProductPricing"
    assert first["target"] == "enterprise" and first["n_recipients"] == 2
    org = got["budget:b020000-0000-4000-8000-000000000002"]
    assert org["sku"] == "copilot_ai_credit" and org["type"] == "SkuPricing"  # singular spelling
    assert org["target"] == "org:acme-eng" and org["amount_nano"] == 500_000_000_000
    cc = got["budget:b030000-0000-4000-8000-000000000003"]
    assert cc["type"] == "BundlePricing" and cc["target"] == "cc:cc-platform"
    assert {a["type"] for a in got.values()} == {"BundlePricing", "ProductPricing", "SkuPricing"}
    assert all(c.source_kind == "github.budgets" and c.kind == "budget" for c in res.config)


def test_user_scope_zero_budget_keeps_team_only() -> None:
    res = _read("budgets.json")
    zero = attrs(_by_entity(res)[("budget", "budget:b040000-0000-4000-8000-000000000004")])
    assert zero["amount_nano"] == 0 and zero["scope"] == "user"
    assert zero["team"] == "platform" and zero["cost_center"] == "cc-platform"
    assert zero["expires_at"] == "2026-12-31" and zero["consumed_nano"] == 0
    assert "target" not in zero
    other = attrs(_by_entity(res)[("budget", "budget:b050000-0000-4000-8000-000000000005")])
    assert other["team"] == "payments" and other["consumed_nano"] == 12_340_000_000
    assert set(zero) <= set(CONFIG_KEYS["budget"])
    text = blob(res)
    assert not re.search(r"p_[0-9a-f]{20}", text)            # R14: never a person
    assert res.source.principal_key_id is None
    assert_no_identity(res, "dev-07")


def test_unmapped_user_budget_has_no_team() -> None:
    res = ADAPTER.read(CONFIG / "budgets.json", opts(team_map=(), cost_center_map=()))
    zero = attrs(_by_entity(res)[("budget", "budget:b040000-0000-4000-8000-000000000004")])
    assert "team" not in zero and "cost_center" not in zero and zero["scope"] == "user"


def test_repository_target_is_hashed_and_license_amount_not_dollars() -> None:
    res = _read("budgets.json")
    repo = attrs(_by_entity(res)[("budget", "budget:b060000-0000-4000-8000-000000000006")])
    assert repo["target"].startswith("repo:h_") and repo["n_recipients"] == 1
    assert repo["target"] == "repo:" + h("acme-eng/secret-TB-CANARY-7f3a91")
    seats = attrs(_by_entity(res)[("budget", "budget:b080000-0000-4000-8000-000000000008")])
    assert "amount_nano" not in seats and seats["sku"] == "copilot_for_business"
    assert res.stats["license_budget_amounts_not_stored"] == 1
    multi = attrs(_by_entity(res)[("budget", "budget:b070000-0000-4000-8000-000000000007")])
    assert multi["target"] == "cc:cc-data" and "team" not in multi


def test_user_filtered_multi_user_page(tmp_path: Path) -> None:
    page = {"budgets": [{"id": "m1", "budget_type": "BundlePricing", "budget_scope":
                         "multi_user_customer", "budget_product_skus": ["ai_credits"],
                         "budget_amount": 100, "consumed_amount": 7, "prevent_further_usage": True,
                         "budget_alerting": {"will_alert": False, "alert_recipients": []}},
                        {"id": "e1", "budget_type": "ProductPricing", "budget_scope": "enterprise",
                         "budget_product_sku": "actions", "budget_amount": 5,
                         "consumed_amount": 3, "prevent_further_usage": False}],
            "user": CANARY_LOGIN, "effective_budget": {"id": "m1", "budget_amount": 100,
                                                      "consumed_amount": 7}}
    path = tmp_path / "b.json"
    path.write_text(json.dumps(page), encoding="utf-8")
    res = ADAPTER.read(path, opts())
    got = {c.entity_id: attrs(c) for c in res.config}
    assert got["budget:m1"]["team"] == "platform" and got["budget:m1"]["consumed_nano"] == 7 * 10**9
    assert got["budget:m1"]["target"] == "enterprise"
    assert "consumed_nano" not in got["budget:e1"] and "team" not in got["budget:e1"]
    assert_no_identity(res)


def test_user_states_quantiles_only_with_k_users() -> None:
    three = attrs(_read("budget_user_states_3.json").config[0])
    assert three == {"n_users": 3, "n_at_or_over_target": 1}
    res7 = _read("budget_user_states_7.json")
    seven = res7.config[0]
    assert seven.kind == "budget_users" and seven.entity_id.startswith("budget:b07")
    assert attrs(seven) == {"n_users": 7, "n_at_or_over_target": 1,
                            "consumed_p50_nano": 4_000_000_000,
                            "consumed_p90_nano": 100_000_000_000}
    assert_no_identity(res7, "dev-02")
    lower_k = attrs(_read("budget_user_states_3.json", k_anonymity=3).config[0])
    assert lower_k["consumed_p50_nano"] == 20_500_000_000
    assert lower_k["consumed_p90_nano"] == 300 * 10**9


def test_user_states_pages_aggregate_per_budget_and_need_the_path(tmp_path: Path) -> None:
    base = json.loads((CONFIG / "budget_user_states_7.json").read_text(encoding="utf-8"))
    second = json.loads(json.dumps(base))
    second["response"]["user_states"] = second["response"]["user_states"][:2]
    second["response"]["total_count"] = 12
    path = tmp_path / "pages.jsonl"
    path.write_text(json.dumps(base) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    res = ADAPTER.read(path, opts())
    assert attrs(res.config[0])["n_users"] == 9 and res.stats["user_states_not_read"] == 3
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(base["response"]), encoding="utf-8")
    res = ADAPTER.read(bare, opts())
    assert res.config == [] and [q.reason for q in res.quarantined] == ["missing:budget_id"]
    assert [n.code for n in res.notes] == ["dq.copilot_budget_users_unattributed"]


def test_cost_centers_counts_not_names() -> None:
    res = _read("cost_centers.json")
    got = {c.entity_id: attrs(c) for c in res.config}
    assert set(got) == {"cc:cc-platform", "cc:cc-data", "cc:cc-retired"}
    platform = got["cc:cc-platform"]
    assert platform == {"cost_center_id": "2eeb8ffe-6903-11ee-8c99-0242ac120002",
                        "state": "active", "pool_enabled": True, "pool_target_credits": 21000,
                        "pool_current_credits": "7250.5", "azure": True, "n_users": 1,
                        "n_teams": 1, "n_orgs": 0, "n_repos": 1}
    assert got["cc:cc-data"]["n_orgs"] == 1 and got["cc:cc-data"]["azure"] is False
    assert "pool_target_credits" not in got["cc:cc-data"]
    assert got["cc:cc-retired"]["state"] == "deleted"
    assert all(c.source_kind == "github.cost_centers" for c in res.config)
    assert_no_identity(res, "dev-07", "hello", "enterprise-team")


def test_single_cost_center_and_budget_objects(tmp_path: Path) -> None:
    cc = json.loads((CONFIG / "cost_centers.json").read_text(encoding="utf-8"))
    one = {"request": {"path": "/enterprises/acme/settings/billing/cost-centers/x"},
           "response": cc["response"]["costCenters"][1]}
    budget = json.loads((CONFIG / "budgets_bare.json").read_text(encoding="utf-8"))["budgets"][0]
    path = tmp_path / "mixed.jsonl"
    path.write_text(json.dumps(one) + "\n" + json.dumps(budget) + "\n", encoding="utf-8")
    res = ADAPTER.read(path, opts())
    assert sorted(c.kind for c in res.config) == ["budget", "cost_center"]


def test_org_settings_all_four_settings_and_plan_type() -> None:
    res = _read("org_billing.jsonl")
    got = {c.entity_id: attrs(c) for c in res.config}
    assert {a["seat_management_setting"] for a in got.values()} == set(SEAT_MANAGEMENT_SETTINGS)
    assert got["org:org-assign-selected"]["plan_type"] == "enterprise"
    assert got["org:org-assign-all"]["plan_type"] == "business"
    assert "plan_type" not in got["org:org-unconfigured"]
    first = got["org:org-assign-all"]
    assert first["seats_total"] == 12 and first["seats_pending_cancellation"] == 1
    assert first["ide_chat"] == "enabled" and first["cli"] == "unconfigured"
    assert "public_code_suggestions" not in first
    assert all(c.source_kind == "github.org_copilot_settings" and c.kind == "org_settings"
               for c in res.config)


def test_org_settings_needs_an_org() -> None:
    res = _read("org_billing_bare.json")
    assert res.config == [] and res.quarantined[0].reason == "missing:org"
    assert "dq.copilot_org_unknown" in {n.code for n in res.notes}
    named = ADAPTER.read(CONFIG / "org_billing_bare.json",
                         opts(attribution=Attribution(workspace_id="acme-eng")))
    assert [c.entity_id for c in named.config] == ["org:acme-eng"]


def test_unknown_enum_values_are_dropped(tmp_path: Path) -> None:
    body = {"seat_breakdown": {"total": 3}, "seat_management_setting": "assign_by_magic",
            "plan_type": "platinum", "ide_chat": "maybe"}
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"request": {"path": "/orgs/x/copilot/billing"},
                                "response": body}), encoding="utf-8")
    res = ADAPTER.read(path, opts())
    assert attrs(res.config[0]) == {"seats_total": 3}
    assert res.stats["unknown_enum_values"] == 3


def test_bad_records_are_quarantined(tmp_path: Path) -> None:
    page = {"budgets": [{"id": "ok", "budget_scope": "enterprise", "budget_type": "SkuPricing",
                         "budget_amount": 1},
                        {"budget_scope": "enterprise", "budget_amount": 1},
                        {"id": "neg", "budget_scope": "user", "budget_amount": -5},
                        {"id": "x", "budget_scope": "enterprise", "budget_amount": "12"},
                        {"id": "exp", "budget_scope": "enterprise", "expires_at": "soon"},
                        "not an object"]}
    path = tmp_path / "b.json"
    path.write_text(json.dumps(page), encoding="utf-8")
    res = ADAPTER.read(path, opts())
    assert sorted(c.entity_id for c in res.config) == ["budget:exp", "budget:ok"]
    assert sorted(q.reason for q in res.quarantined) == [
        "bad_type:budget_amount", "bad_type:budget_amount", "missing:id", "not_object"]
    assert res.stats["bad_expires_at"] == 1
    with pytest.raises(SourceError):
        ADAPTER.read(path, opts(lenient=False))


def test_latest_fetch_wins_for_one_budget(tmp_path: Path) -> None:
    doc = json.loads((CONFIG / "budgets.json").read_text(encoding="utf-8"))
    later = json.loads(json.dumps(doc))
    later["fetched_ms"] += 3_600_000
    later["response"]["budgets"][0]["budget_amount"] = 1500
    path = tmp_path / "two.jsonl"
    path.write_text(json.dumps(later) + "\n" + json.dumps(doc) + "\n", encoding="utf-8")
    res = ADAPTER.read(path, opts())
    first = [c for c in res.config if c.entity_id.endswith("000000000001")]
    assert len(first) == 1 and attrs(first[0])["amount_nano"] == 1_500_000_000_000


def test_sniff() -> None:
    for name in ("budgets.json", "budgets_bare.json", "budget_user_states_7.json",
                 "cost_centers.json", "org_billing.jsonl", "org_billing_bare.json"):
        path = CONFIG / name
        assert ADAPTER.sniff(path, path.read_bytes()[:65536]), name
    for other in (FIXTURES / "seats" / "enterprise_seats.json",
                  FIXTURES / "metrics" / "users-1-day_example.ndjson"):
        assert not ADAPTER.sniff(other, other.read_bytes()[:65536])


def test_options_are_checked() -> None:
    with pytest.raises(UsageError):
        ADAPTER.read(CONFIG / "budgets.json", opts(content_tier=ContentTier.FULL))
    with pytest.raises(UsageError):
        ADAPTER.read(CONFIG / "budgets.json", opts(name_key=b""))
    with pytest.raises(UsageError):
        ADAPTER.read(CONFIG / "budgets.json", object())  # type: ignore[arg-type]
    with pytest.raises(SourceError):
        ADAPTER.read(CONFIG / "missing.json", opts())


@pytest.mark.parametrize("name", ["budgets.json", "budget_user_states_7.json", "cost_centers.json",
                                  "org_billing.jsonl"])
def test_conforms(name: str) -> None:
    result = assert_adapter_conforms(ADAPTER, CONFIG / name, expect_capabilities={"config"},
                                     opts=conformance_ingest_options(
                                         team_map=(("tb-canary-login-7f3a91", "platform"),)))
    assert result.config


def test_directory_read_is_one_source() -> None:
    res = ADAPTER.read(CONFIG, opts())
    kinds = sorted({c.kind for c in res.config})
    assert kinds == ["budget", "budget_users", "cost_center", "org_settings"]
    assert res.stats["files"] == 7
