"""github-metered-usage (detailed / summarized CSV, addendum §5.2) and github-billing-api
(recorded REST pages, addendum §5.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.adapters.github_billing import BillingApiAdapter, MeteredUsageAdapter
from tokenbill.core import catalog
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import SourceError
from tokenbill.core.ids import pseudonym
from tokenbill.core.pool import detect_plans

from .helpers import ENTRIES, NAME_KEY, PRINCIPAL_KEY, dump, notes, opts, read, write

DETAILED = "detailed_2026-09.csv"
PAGES = "rest/pull_pages_2026-09.jsonl"


def _by(result, **kw):  # noqa: ANN001, ANN202
    return [c for c in result.cost_lines if all(getattr(c, k) == v for k, v in kw.items())]


def test_detailed_report() -> None:
    r = read(DETAILED)
    exp = ENTRIES[DETAILED]["expect"]
    assert len(r.cost_lines) == exp["cost_lines"] and not r.quarantined
    assert sum(c.amount_nano for c in r.cost_lines) == exp["net_nano"]
    assert sum(c.list_amount_nano for c in r.cost_lines) == exp["gross_nano"]
    for key in ("user_workflows_dropped", "actions_unattributed_dropped", "rows_other_products"):
        assert r.stats[key] == exp[key] == 1
    seats = _by(r, cost_type="seat")
    assert len(seats) == exp["seat_lines"] and {c.channel for c in seats} == {"github_copilot"}
    assert {c.sku for c in seats} == {"copilot_for_business", "copilot_enterprise"}
    assert {catalog.copilot_seat_plan(c.sku) for c in seats} == {"business", "enterprise"}
    assert sorted(c.quantity for c in seats if c.principal) == ["0.5333333", "1", "1", "1"]
    plans = {p.entity_id: p.plan for p in detect_plans(r.cost_lines, [], [], month="2026-09",
                                                       entity_mode="org")}
    assert plans == {"org:acme-a": "business", "org:acme-b": "enterprise"}
    actions = _by(r, cost_type="actions")
    assert len(actions) == exp["actions_lines"] and {c.channel for c in actions} == {
        "github_actions"}
    assert sorted(c.workload for c in actions) == sorted([
        "copilot_cloud_agent", "copilot_cloud_agent", "copilot_code_review",
        "copilot_code_review", "code_quality", "agentic_workflow"])
    review_skus = {c.sku for c in actions if c.workload == "copilot_code_review"}
    assert review_skus == {"linux_16_core", "actions_linux_16_core"}      # both spellings
    assert all(catalog.runner_rate(s) is not None for s in review_skus)
    (aw,) = [c for c in actions if c.workload == "agentic_workflow"]
    assert aw.workflow == pseudonym(NAME_KEY, "h", ".github/workflows/issue-triage.lock.yml")
    assert [c.workflow for c in actions if c.workload != "agentic_workflow"] == [None] * 5
    public = [c for c in actions if c.amount_nano == 0]
    assert len(public) == 1 and public[0].list_amount_nano == 720_000_000   # discount kept
    sandbox = _by(r, cost_type="sandbox")
    assert len(sandbox) == 2 and {c.channel for c in sandbox} == {"github_sandbox"}
    assert [c.sku for c in _by(r, cost_type="code_quality.license")] == ["code_quality_licenses"]
    assert [c.sku for c in _by(r, cost_type="metered.ai_credit")] == ["copilot_ai_credit"]
    text = dump(r)
    for leaked in (CANARY_LOGIN, "octo-", ".github/workflows", "web-app", "ci.yml"):
        assert leaked not in text
    assert {c.team for c in seats if c.principal == pseudonym(PRINCIPAL_KEY, "p", "octo-alice")
            } == {"platform"}
    for c in r.cost_lines:                                          # August closed, Sept open
        assert c.finality == ("final" if c.date_utc == "2026-08-30" else "provisional")
    assert r.capabilities == frozenset({"cost", "copilot_billing"})


def test_summarized_report() -> None:
    r = read("summarized_2026-09.csv")
    exp = ENTRIES["summarized_2026-09.csv"]["expect"]
    assert len(r.cost_lines) == 5 and r.stats["actions_unattributed_dropped"] == 1
    assert all(c.principal is None for c in r.cost_lines) and r.stats["detailed"] == 0
    assert sum(c.amount_nano for c in r.cost_lines) == exp["net_nano"]
    assert {c.cost_type for c in r.cost_lines} == {"seat", "sandbox", "metered.ai_credit",
                                                    "code_quality.license"}


def test_metered_unknown_skus_and_bad_rows(tmp_path: Path) -> None:
    head = "date,product,sku,quantity,unit_type,gross_amount,discount_amount,net_amount\n"
    path = write(tmp_path, "m.csv", head + "2026-09-01,copilot,copilot_mystery,1,x,1,0,1\n"
                 "2026-09-01,sandbox,sandbox_gpu,1,minutes,1,0,1\n"
                 "not-a-date,copilot,copilot_enterprise,1,x,1,0,1\n"
                 "2026-09-01,copilot,,1,x,1,0,1\n"
                 "2026-09-01,copilot,copilot_enterprise,1,x,1,0,2\n")
    r = MeteredUsageAdapter().read(path, opts())
    assert sorted(q.reason for q in r.quarantined) == ["bad_type:date", "missing:sku"]
    assert sorted(c.cost_type for c in r.cost_lines) == ["other", "sandbox", "seat"]
    assert notes(r)["dq.unmapped_sku"] == 2 and notes(r)["dq.copilot_row_identity"] == 1
    wrong = write(tmp_path, "w.csv", "date,sku\n2026-09-01,x\n")
    assert [q.reason for q in MeteredUsageAdapter().read(wrong, opts()).quarantined] == [
        "missing:product"]


@pytest.mark.parametrize(("rel", "sku", "unit", "cost_type", "channel"), [
    ("rest/oas_ai_credit_enterprise.json", "copilot_ai_credit", "ai-credits", "rest.ai_credit",
     "github_copilot"),
    ("rest/oas_ai_credit_org.json", "copilot_ai_credit", "ai-credits", "rest.ai_credit",
     "github_copilot"),
    ("rest/oas_ai_credit_user.json", "copilot_ai_credit", "ai-credits", "rest.ai_credit",
     "github_copilot"),
    ("rest/oas_premium_request.json", "copilot_premium_request", "requests", "rest.ai_credit",
     "github_copilot"),
    ("rest/oas_usage_summary.json", "actions_linux", "minutes", "rest.summary",
     "github_actions"),
    ("rest/oas_usage.json", "actions_linux", "minutes", "rest.usage", "github_actions")])
def test_oas_examples(rel: str, sku: str, unit: str, cost_type: str, channel: str) -> None:
    r = read(rel)
    (line,) = r.cost_lines
    assert (line.sku, line.unit, line.cost_type, line.channel) == (sku, unit, cost_type, channel)
    assert line.amount_nano == ENTRIES[rel]["expect"]["net_nano"]           # exact decimals
    assert line.finality == "final" and line.source_kind == "github.billing_api"
    if "ai_credit" in rel:
        assert (line.model, line.quantity, line.date_utc) == ("gpt-5", "100", "2025-01-01")
    if rel.endswith("_user.json"):
        assert line.principal == pseudonym(PRINCIPAL_KEY, "p", "monalisa")
        assert "monalisa" not in dump(r)
    if rel.endswith("_org.json"):
        assert line.workspace_id == "GitHub"
    if rel.endswith("oas_usage.json"):
        assert line.repo == pseudonym(NAME_KEY, "h", "github/example")
        assert (line.date_utc, line.quantity) == ("2023-08-01", "100")


def test_report_export_envelopes_yield_nothing() -> None:
    r = read("rest/oas_report_exports.json")
    assert r.cost_lines == [] and r.stats["export_envelopes"] == 1 and not r.quarantined
    assert "https://" not in dump(r) and "monalisa" not in dump(r)


def test_pull_pages() -> None:
    r = read(PAGES)
    exp = ENTRIES[PAGES]["expect"]
    assert len(r.cost_lines) == exp["cost_lines"] and r.stats["pages"] == 4
    assert r.stats["export_envelopes"] == 1 and r.stats["items_other_products"] == 1
    assert sum(c.amount_nano for c in r.cost_lines) == exp["net_nano"]
    ai = _by(r, cost_type="rest.ai_credit")
    assert {c.cost_center for c in ai} == {"Platform CC"}
    assert {(c.model, c.routing, c.pseudo) for c in ai} == {
        ("claude-sonnet-5", "direct", None), ("claude-haiku-4-5", "auto", None),
        (None, "direct", "code_review")}
    assert {c.quantity for c in ai} == {"0"} and {c.date_utc for c in ai} == {"2026-09-01"}
    assert {c.list_amount_nano for c in ai} == {123_456_789_000, 50_000_000_000, 8_005_000_000}
    assert {c.finality for c in ai} == {"provisional"}
    summary = _by(r, cost_type="rest.summary")
    assert {(c.sku, c.finality, c.date_utc) for c in summary} == {
        ("copilot_ai_credit", "final", "2026-08-01"), ("copilot_for_business", "final",
                                                       "2026-08-01")}
    usage = _by(r, cost_type="rest.usage")
    assert {(c.sku, c.quantity) for c in usage} == {("copilot_ai_credit", None),
                                                   ("actions_linux", "100")}
    assert notes(r)["dq.copilot_integer_quantity"] == 1
    assert "signed" not in dump(r) and "https://" not in dump(r)


def test_rest_malformed_pages(tmp_path: Path) -> None:
    lines = [
        "[1, 2]", "not json", '{"response": 5}',
        '{"response": {"timePeriod": {"year": 2026}}}',
        '{"response": {"usageItems": {}}}',
        '{"response": {"timePeriod": {"year": 2026, "month": 13}, "usageItems": []}}',
        '{"response": {"timePeriod": {"year": "2026"}, "usageItems": []}}',
        json.dumps({"request": {"path": "/orgs/o/settings/billing/usage/summary"},
                    "response": {"timePeriod": {"year": 2026, "month": 9}, "usageItems": [
                        5, {"sku": "copilot_ai_credit", "product": "Copilot",
                            "grossAmount": "x", "discountAmount": 0, "netAmount": 0},
                        {"sku": 7}, {"product": "Copilot"},
                        {"sku": "copilot_ai_credit", "product": "Copilot", "grossAmount": 1,
                         "discountAmount": 0},
                        {"sku": "Copilot Business Seats", "product": "Copilot",
                         "grossAmount": True, "discountAmount": 0, "netAmount": 0},
                        {"sku": "Copilot for Business", "product": "Copilot", "grossAmount": 19,
                         "discountAmount": 0, "netAmount": 19, "netQuantity": 1,
                         "unitType": "User Months"}]}}),
        json.dumps({"response": {"usageItems": [{"sku": "Actions Linux", "product": "Actions",
                                                 "grossAmount": 1, "discountAmount": 0,
                                                 "netAmount": 1, "date": "someday"}]}})]
    path = write(tmp_path, "bad.jsonl", "\n".join(lines) + "\n")
    r = BillingApiAdapter().read(path, opts())
    reasons = sorted(q.reason for q in r.quarantined)
    assert reasons == sorted([
        "bad_json", "bad_json", "not_object", "missing:usageItems", "bad_type:usageItems",
        "bad_type:timePeriod", "bad_type:timePeriod", "not_object", "bad_type:grossAmount",
        "bad_type:sku", "missing:sku", "missing:netAmount", "bad_type:grossAmount",
        "bad_type:date"])
    (seat,) = r.cost_lines
    assert (seat.sku, seat.unit, seat.cost_type) == ("copilot_for_business", "user months",
                                                     "rest.summary")
    with pytest.raises(SourceError):
        BillingApiAdapter().read(path, opts(lenient=False))
    single = write(tmp_path, "one.json", '{"usageItems": [], "timePeriod": {"year": 2026}}')
    assert BillingApiAdapter().read(single, opts()).cost_lines == []
