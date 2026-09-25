"""Gate tests (merge gate 1): OUT's renderers include the Copilot section through
``core.extensions.render_sections``; ``write_focus`` on a real ``SqliteStore`` with Copilot
collector lanes and report rows emits no ledger rows on Copilot channels and its Copilot totals
equal GitHub's lines."""

from __future__ import annotations

import csv
import datetime as _dt
import io
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.core import builders as b
from tokenbill.core import extensions
from tokenbill.core import testing as kit
from tokenbill.core.builders import CANARY
from tokenbill.core.ids import key_id
from tokenbill.core.records import COPILOT_CHANNELS
from tokenbill.core.types import IngestResult, SourceInfo

from .worlds import action_plan, admin_action, p1_world, p13_world, result_of

pytestmark = pytest.mark.gate

ORG = b"cp-out-gate-org-key-0123456789abc"
EPOCH = _dt.date(1970, 1, 1)
DAY = 86_400_000
SINCE = (_dt.date(2026, 9, 1) - EPOCH).days * DAY
UNTIL = SINCE + 30 * DAY
GROUP_BY = ("date", "provider", "channel", "model", "team", "cost_center", "project",
            "workspace_id", "lane_kind", "workload_class", "agent_product", "billing_path")


def test_out_terminal_html_and_json_include_the_copilot_section() -> None:
    terminal = pytest.importorskip("tokenbill.outputs.terminal")
    html = pytest.importorskip("tokenbill.outputs.html")
    rj = pytest.importorskip("tokenbill.outputs.result_json")
    for summary in (p1_world().summary(plans_by_scenario=(("known", action_plan()),),
                                       actions=[admin_action()]),
                    p13_world().summary(plans_by_scenario=(("business", action_plan()),
                                                           ("enterprise", action_plan())))):
        result = result_of(summary)
        text = terminal.render_terminal(result)
        assert "COPILOT BILL" in text and "PLAN" in text
        page = html.render_html(result)
        assert '<section id="copilot">' in page
        doc = rj.to_result_json(result, deterministic=True)
        assert "copilot" in doc and rj.validate_result_json(doc) == []
        assert rj.rule_violations(doc["copilot"]) == []
        assert CANARY not in text + page + rj.dumps_result(result)


def _requests() -> list:
    out = []
    for i in range(6):
        ts = SINCE + (4 + i) * DAY + 3_600_000
        out.append(b.make_request(
            f"L-copilot-{i}", 0, ts, {"uncached_input": 2_000, "cache_read": 10_000,
                                      "output": 500}, "claude-sonnet-4-6",
            attribution={"principal": f"r_dev{i}", "team": "alpha",
                         "billing_path": "copilot_pool"},
            provider="github", channel="github_copilot", billing_path="copilot_pool",
            message_id=f"msg_cp_{i}"))
    return out


def test_write_focus_on_sqlite_drops_copilot_ledger_rows_and_matches_report_lines(
        tmp_path: Path) -> None:
    db = pytest.importorskip("tokenbill.store.db")
    focus = pytest.importorskip("tokenbill.outputs.focus")
    w = p1_world()
    store = db.SqliteStore(tmp_path / "ledger.db", org_key=ORG)
    src = SourceInfo(source_id="s_cp", adapter="github-ai-usage", name_hmac="h_" + "3" * 20,
                     sha256="s_cp", bytes=1, name_key_id=None, principal_key_id=key_id(ORG))
    store.ingest(IngestResult(source=src, requests=_requests(), sessions=[], events=[],
                              aggregates=list(w.aggs), cost_lines=list(w.lines), outcomes=[],
                              quarantined=[], notes=[], stats={},
                              capabilities=frozenset({"cost", "aggregates", "usage_sequence"})),
                 pricer=kit.FakePricer())
    ledger_rows = store.cost_rows(since_ms=SINCE, until_ms=UNTIL, group_by=GROUP_BY)
    assert any(r.channel == "github_copilot" for r in ledger_rows)   # collector lanes exist
    reconciled = frozenset(COPILOT_CHANNELS)
    rows, owned = extensions.focus_rows(store, [], since_ms=SINCE, until_ms=UNTIL,
                                        reconciled_channels=reconciled, k=5,
                                        allow_unreconciled=False, role="primary")
    assert owned == reconciled and rows
    buf = io.StringIO()
    n = focus.write_focus(ledger_rows, buf, reconciled_channels=reconciled,
                          allow_unreconciled=False, rate_card_sha="ab" * 32, extra_rows=rows,
                          owned_channels=owned)
    out = list(csv.DictReader(io.StringIO(buf.getvalue())))
    assert n == len(out) == len(rows)
    copilot = [r for r in out if r["x_Channel"] in COPILOT_CHANNELS]
    assert copilot and all(r["x_DiscountUnclassified"] != "" for r in copilot)
    billed = sum(Decimal(r["BilledCost"]) for r in copilot)
    expected = Decimal(sum(c.amount_nano for c in w.lines)) / Decimal(10**9)
    assert billed == expected
    listed = sum(Decimal(r["ListCost"]) for r in copilot)
    gross = sum(c.list_amount_nano if c.list_amount_nano is not None else c.amount_nano
                for c in w.lines)
    assert listed == Decimal(gross) / Decimal(10**9)
    assert CANARY not in buf.getvalue()
