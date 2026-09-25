"""Gate tests (merge gate 1): CP-ORGDATA against sibling Copilot packages, each skipped until the
sibling module exists (``pytest.importorskip``).

* CP-HANDOFF ``pseudonym_of`` must produce the ``p_`` the org-data adapters produce for the same
  login (the admin answers erasure requests with it) — including a login spelled with capitals
  (GitHub logins are case-insensitive; ``github_config.login_key``).
* A CP-HANDOFF export of the CP-ORGDATA fixtures passes its leak gate and reads back unchanged.
* CP-BILL's AI usage report ``username`` must pseudonymize like seats / metrics logins, so seats ×
  activity × credits join per person (addendum §4 "Identity").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.adapters.github_metrics import CopilotMetricsAdapter
from tokenbill.adapters.github_seats import CopilotSeatsAdapter
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.records import to_json

from .helpers import CC_MAP, FIXTURES, NOW_MS, PRINCIPAL_KEY, TEAM_MAP, opts, p_of

pytestmark = pytest.mark.gate


def test_gate_handoff_pseudonym_matches_the_adapters() -> None:
    handoff = pytest.importorskip("tokenbill.copilot.handoff")
    seats = CopilotSeatsAdapter().read(FIXTURES / "seats" / "enterprise_seats.json", opts())
    assert handoff.pseudonym_of(CANARY_LOGIN, key=PRINCIPAL_KEY) in {
        s.principal for s in seats.licenses}
    assert handoff.pseudonym_of("Dev-02", key=PRINCIPAL_KEY) == p_of("dev-02")


def test_gate_handoff_export_of_orgdata_passes_the_leak_gate(tmp_path: Path) -> None:
    handoff = pytest.importorskip("tokenbill.copilot.handoff")
    files = [FIXTURES / "config" / "budgets.json", FIXTURES / "config" / "cost_centers.json",
             FIXTURES / "config" / "org_billing.jsonl",
             FIXTURES / "seats" / "enterprise_seats.json",
             FIXTURES / "metrics" / "users-1-day_12x3.ndjson",
             FIXTURES / "metrics" / "enterprise-1-day.ndjson",
             FIXTURES / "metrics" / "users-28-day_dashboard.ndjson"]
    out = tmp_path / "export.tbx"
    handoff.export_from_files(files, out, key=PRINCIPAL_KEY, team_map=TEAM_MAP,
                              cost_center_map=CC_MAP, answers=(), k=5, aggregate_only=False,
                              since=None, until=None, now_ms=NOW_MS, tool_version="gate")
    _, result = handoff.read_bundle(out)
    direct = CopilotMetricsAdapter().read(FIXTURES / "metrics" / "users-1-day_12x3.ndjson",
                                          opts())
    daily = [a for a in result.activity if a.source_kind == "github.copilot_metrics"]
    assert sorted(to_json(a)["counts"] for a in daily) == sorted(
        to_json(a)["counts"] for a in direct.activity)
    assert len(result.licenses) == 10 and result.config


def test_gate_ai_usage_report_logins_join_seats(tmp_path: Path) -> None:
    billing = pytest.importorskip("tokenbill.adapters.github_billing")
    header = ("date,product,sku,quantity,unit_type,applied_cost_per_quantity,gross_amount,"
              "discount_amount,net_amount,username,organization,repository,cost_center_name,"
              "model,input,output,cache_read,cache_write")
    row = ("2026-09-20,copilot,copilot_ai_credit,10,ai-credits,0.01,0.1,0,0.1,Dev-02,acme-eng,,"
           ",Claude Sonnet 4.6,1000,100,0,0")
    path = tmp_path / "ai_usage.csv"
    path.write_text(header + "\n" + row + "\n", encoding="utf-8")
    result = billing.AiUsageReportAdapter().read(path, opts())
    principals = {c.principal for c in result.cost_lines if c.principal is not None}
    assert principals == {p_of("dev-02")}
