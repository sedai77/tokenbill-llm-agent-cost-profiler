"""Gate tests (merge gate 1; ``importorskip``-guarded): CP-BILL / CP-ORGDATA fixture files through
the real adapters and the real ``RateCard`` (synthetic verdicts), and RECON's ``merge_reports``
combining a Copilot report with a RECON report for ``anthropic_api`` (decisions kept)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.copilot import recon
from tokenbill.core import testing as kit
from tokenbill.core.builders import make_aggregate, make_cost_line, make_usage
from tokenbill.core.ids import key_id
from tokenbill.core.records import to_json
from tokenbill.core.types import RECON_DECISION_PREFIXES, IngestOptions, ReconciliationReport

from . import world as w

pytestmark = pytest.mark.gate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NAME_KEY = bytes(range(51, 83))


def _opts() -> IngestOptions:
    return IngestOptions(identity_mode="central-ingest", principal_key=w.ORG,
                         principal_key_id=key_id(w.ORG), name_key=NAME_KEY,
                         name_key_id=key_id(NAME_KEY), now_ms=w.day_ms("2026-10-20"))


def _rate_card():
    engine = pytest.importorskip("tokenbill.rates.engine")
    schema = pytest.importorskip("tokenbill.rates.schema")
    card = engine.RateCard([schema.load_builtin()])
    ctx = w.list_ctx("Claude Sonnet 5")
    if card.resolve(ctx, ts_ms=w.day_ms("2026-09-10")) is None:
        pytest.skip("the RATES card carries no Copilot rows yet")
    return card


def _ingest_dir(directory: Path, adapters, store, rs) -> int:
    n = 0
    for path in sorted(directory.glob("*")):
        if path.suffix not in (".csv", ".json", ".jsonl"):
            continue
        head = path.read_bytes()[:4096]
        for adapter in adapters:
            if adapter.sniff(path, head):
                result = adapter.read(path, _opts())
                store.ingest(result)
                if result.licenses or result.activity or result.config:
                    rs.put(result, principal_key_id=key_id(w.ORG))
                n += 1
                break
    return n


def test_cp_bill_and_orgdata_fixtures_reconcile_through_the_real_adapters() -> None:
    billing = pytest.importorskip("tokenbill.adapters.github_billing")
    bill_dir = FIXTURES / "copilot_bill"
    if not bill_dir.is_dir():
        pytest.skip("CP-BILL fixtures not merged yet")
    card = _rate_card()
    store = kit.MemoryStore(org_key=w.ORG, name_key_id=key_id(NAME_KEY), pricer=card)
    rs = kit.MemoryRecordStore(store)
    adapters = [billing.AiUsageReportAdapter(), billing.MeteredUsageAdapter(),
                billing.BillingApiAdapter()]
    assert _ingest_dir(bill_dir, adapters, store, rs) > 0
    org_dir = FIXTURES / "copilot_orgdata"
    config_mod = None
    try:
        import tokenbill.adapters.github_config as config_mod  # noqa: PLC0415
    except ImportError:
        pass
    if config_mod is not None and org_dir.is_dir():
        _ingest_dir(org_dir, [config_mod.CopilotConfigAdapter()], store, rs)
    lines = store.cost_lines(since_ms=0, until_ms=2**53)
    months = sorted({c.date_utc[:7] for c in lines if c.channel in recon.CHANNELS})
    assert months
    first = f"{months[0]}-01"
    report = recon.reconcile_copilot(store, [rs], card, since_ms=w.day_ms(first),
                                     until_ms=2**45, tolerance_pct="0.5", unexplained_pct="1.0",
                                     closed_only=False, today="2026-10-20")
    assert isinstance(report, ReconciliationReport)
    assert all(not v.mapping_verified for v in report.channels)          # synthetic
    for v in report.channels:
        if v.verdict != "insufficient_data":
            assert "schema unverified" in recon.verdict_label(report, v.channel)
    keys = [k for k, _ in report.decisions]
    assert all(k.startswith(RECON_DECISION_PREFIXES) for k in keys)
    if any(c.source_kind == recon.REPORT_KIND for c in lines):
        assert any(k.startswith("convention:") for k in keys)
    again = recon.reconcile_copilot(store, [rs], card, since_ms=w.day_ms(first), until_ms=2**45,
                                    tolerance_pct="0.5", unexplained_pct="1.0",
                                    closed_only=False, today="2026-10-20")
    assert json.dumps(to_json(again), sort_keys=True) == json.dumps(to_json(report),
                                                                     sort_keys=True)


def test_p1_world_reconciles_with_the_real_rate_card() -> None:
    card = _rate_card()
    store, rs = w.p1_world()
    report = recon.reconcile_copilot(store, [rs], card, since_ms=w.day_ms("2026-09-01"),
                                     until_ms=w.day_ms("2026-10-01"), tolerance_pct="0.5",
                                     unexplained_pct="1.0", closed_only=False,
                                     today="2026-10-20")
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert w.decisions(report)["convention:s_p1"] == "excl"


def test_recon_merge_reports_keeps_the_decisions() -> None:
    reconcile_mod = pytest.importorskip("tokenbill.recon.reconcile")
    if not hasattr(reconcile_mod, "merge_reports"):
        pytest.skip("RECON merge_reports not merged yet")
    pricer = kit.FakePricer()
    usage = make_usage(uncached_input=1_000_000, output=100_000)
    agg = make_aggregate(usage, bucket_start_ms=w.day_ms("2026-09-10"),
                         bucket_end_ms=w.day_ms("2026-09-11"), finality="final")
    line = make_cost_line(1, date_utc="2026-09-10", finality="final")
    anthropic = reconcile_mod.reconcile([], [agg], [line], pricer, today="2026-10-20")
    store, rs = w.p1_world()
    copilot = w.reconcile(store, rs)
    merged = reconcile_mod.merge_reports([anthropic, copilot])
    assert isinstance(merged, ReconciliationReport)
    assert dict(merged.decisions) == {**dict(anthropic.decisions), **dict(copilot.decisions)}
    channels = {v.channel for v in merged.channels}
    assert set(recon.CHANNELS) <= channels
    assert {v.channel: v.verdict for v in merged.channels if v.channel in recon.CHANNELS} == \
        w.verdicts(copilot)
