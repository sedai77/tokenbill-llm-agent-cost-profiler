"""Report-level contracts: the ``ChannelReconciler`` registration, decisions through the extension
host, rounding remainders (R-E44), residual order, determinism, privacy and property tests."""

from __future__ import annotations

import dataclasses
import json
import random
import re
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.copilot import recon
from tokenbill.core import extensions
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import ContractViolation, TokenbillError, UsageError
from tokenbill.core.labels import Finality
from tokenbill.core.protocols import ChannelReconciler
from tokenbill.core.records import to_json
from tokenbill.core.types import RECON_DECISION_PREFIXES, ReconciliationReport

from . import world as w

VERDICTS = {"reconciled", "not_reconciled", "insufficient_data"}
STATUSES = {"match", "within_tolerance", "over", "under", "explained", "unexplained",
            "provisional"}


def _recon_like(decisions=()) -> ReconciliationReport:
    """A report of the SPEC RECON shape for ``anthropic_api`` (no decisions of its own)."""
    return ReconciliationReport(
        window=("2026-09-01", "2026-10-01"), tolerance_pct="0.5", unexplained_tolerance_pct="1.0",
        rows=(), token_coverage_pct=None, dollar_coverage_pct=None, rate_card_error=None,
        over_count_rows=0, effective_discount=(), residuals=(), unexplained_nano=0, channels=(),
        verdict="insufficient_data", finality=Finality.PROVISIONAL, suggested_contract=None,
        rerun_verdict=None, decisions=tuple(decisions))


def test_reconcile_copilot_is_the_registered_channel_reconciler() -> None:
    assert isinstance(recon.reconcile_copilot, ChannelReconciler)
    spec = {s.name: s for s in extensions.extensions()}["copilot"]
    assert spec.reconciler == "tokenbill.copilot.recon:reconcile_copilot"
    assert spec.panel_builder == "tokenbill.copilot.panel:build_copilot_panel"
    assert set(spec.channels) == set(recon.CHANNELS)


def test_run_reconcilers_passes_rounding_remainders_by_adapter_name() -> None:
    store, rs = w.p1_world()
    w.ingest(store, lines=[], adapter="github-ai-usage", source_id="rem",
             stats={"rounding_remainder_e18": 7_000_000_000})          # 7e-9 USD → 7 nano
    notes: list = []
    reports = extensions.run_reconcilers(
        store, [rs], w.PRICER, since_ms=w.day_ms("2026-09-01"), until_ms=w.day_ms("2026-10-01"),
        tolerance_pct="0.5", unexplained_pct="1.0", closed_only=False, today="2026-10-20",
        notes=notes)
    assert notes == [] and len(reports) == 1
    report = reports[0]
    assert w.residuals(report)["copilot_rounding"] == 7
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    merged = extensions.recon_decisions_of([report, _recon_like()])
    assert merged == report.decisions
    assert all(k.startswith(RECON_DECISION_PREFIXES) for k, _ in merged)
    with pytest.raises(ContractViolation):
        extensions.recon_decisions_of([report, _recon_like(
            [("gross_is_list:enterprise:2026-09", "false")])])


def test_rounding_remainders_legacy_key_and_absence() -> None:
    store, rs = w.p1_world()
    report = w.reconcile(store, rs, rounding_remainders={"github.ai_usage_report":
                                                         Decimal("-2E-9")})
    assert w.residuals(report)["copilot_rounding"] == -2
    report = w.reconcile(store, rs, rounding_remainders={"anthropic-cost": Decimal("1")})
    assert "copilot_rounding" not in w.residuals(report)


def test_residuals_follow_the_classifier_order() -> None:
    store, rs = w.p1_world()
    w.ingest(store, requests=[w.request("l", 0, "2026-09-10", {"uncached_input": 10,
                                                                "output": 10},
                                        provider_nano=1)], adapter="copilot-otel", source_id="o")
    report = w.reconcile(store, rs)
    order = [*recon.RESIDUAL_ORDER, *recon.L0_CODES]
    codes = [c for c, _ in report.residuals]
    assert codes == sorted(codes, key=order.index)
    assert codes[-1] == "copilot_rate_mismatch"


def test_verdict_label_and_notes() -> None:
    store, rs = w.world()
    report = w.reconcile(store, rs)
    assert recon.verdict_label(report, "github_actions") == "insufficient_data"
    assert recon.report_notes(report) == ()
    with pytest.raises(UsageError):
        recon.verdict_label(report, "anthropic_api")
    verified = dataclasses.replace(report, channels=tuple(
        dataclasses.replace(v, verdict="reconciled", mapping_verified=True)
        for v in report.channels))
    assert recon.verdict_label(verified, "github_sandbox") == "reconciled"


def test_report_is_content_free_and_deterministic() -> None:
    store, rs = w.p1_world()
    a = json.dumps(to_json(w.reconcile(store, rs)), sort_keys=True)
    assert not re.search(r"\bp_[0-9a-f]{20}\b", a)
    assert CANARY_LOGIN not in a
    # the same records ingested in another order and split differently give the same report
    lines = store.cost_lines(since_ms=0, until_ms=2**53)
    aggs = store.aggregates(since_ms=0, until_ms=2**53)
    rng = random.Random(7)
    rng.shuffle(lines)
    rng.shuffle(aggs)
    store2, rs2 = w.world()
    half = len(lines) // 2
    w.ingest(store2, lines=lines[half:], aggs=aggs[: len(aggs) // 2], source_id="x1")
    w.ingest(store2, lines=lines[:half], aggs=aggs[len(aggs) // 2:], source_id="x2")
    b = json.dumps(to_json(w.reconcile(store2, rs2)), sort_keys=True)
    assert a == b


def test_record_stores_of_other_extensions_are_ignored() -> None:
    store, rs = w.p1_world()
    other = w.kit.MemoryRecordStore(store, name="other")
    from tokenbill.core.builders import make_config
    w.put_records(other, config=[make_config("run_flags", {"billing_mode.enterprise": "volume"})])
    report = recon.reconcile_copilot(store, [other, rs], w.PRICER,
                                     since_ms=w.day_ms("2026-09-01"),
                                     until_ms=w.day_ms("2026-10-01"), tolerance_pct="0.5",
                                     unexplained_pct="1.0", closed_only=False, today="2026-10-20")
    assert w.verdicts(report)["github_copilot"] == "reconciled"


# ---------------------------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------------------------

MODELS = ("Claude Sonnet 5", "GPT-5.5", "Auto: Claude Haiku 4.5", "Code Review", "GPT-4o mini",
          "Claude Opus 4.8 (fast mode)", "Grok 4.7")


@st.composite
def _worlds(draw):
    n = draw(st.integers(1, 6))
    rows = []
    for i in range(n):
        rows.append(dict(
            day=draw(st.integers(1, 30)), model=draw(st.sampled_from(MODELS)),
            team=draw(st.sampled_from(["t1", "t2", None])),
            convention=draw(st.sampled_from(["excl", "incl"])),
            read=draw(st.integers(0, 300_000)), write=draw(st.integers(0, 20_000)),
            scale=draw(st.sampled_from([None, Decimal("0.9"), Decimal("1.1"), Decimal("1.004")])),
            discount=draw(st.sampled_from(["0", "1", "0.5"])),
            unattributed=draw(st.booleans()), final=draw(st.booleans()), i=i))
    return rows


def _build(rows):
    store, rs = w.world()
    lines, aggs = [], []
    for r in rows:
        date = f"2026-09-{r['day']:02d}"
        model = r["model"]
        kw = dict(team=r["team"], convention=r["convention"], read=r["read"],
                  write=r["write"], unattributed=r["unattributed"], discount=r["discount"],
                  finality="final" if r["final"] else "provisional", principal=w.p(f"u{r['i']}"))
        unpriced = w.PRICER.unit_rates(w.list_ctx(model), ts_ms=w.day_ms(date)) is None
        if model in ("Code Review", "GPT-4o mini") or unpriced:
            kw["credits"] = "3"
        else:
            kw["scale"] = r["scale"]
        line, agg = w.row(date, model, **kw)
        lines.append(line)
        aggs.append(agg)
    aggs.extend(w.coverage("s_h", d, lines) for d in sorted({c.date_utc for c in lines}))
    return store, rs, lines, aggs


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_worlds())
def test_property_valid_report_and_permutation_invariance(rows) -> None:
    store, rs, lines, aggs = _build(rows)
    w.ingest(store, lines=lines, aggs=aggs)
    try:
        report = w.reconcile(store, rs)
    except TokenbillError:   # only library errors may escape (duplicate natural keys etc.)
        return
    assert report.verdict in VERDICTS and {v.verdict for v in report.channels} <= VERDICTS
    assert {r.status for r in report.rows} <= STATUSES
    assert report.unexplained_nano >= 0
    assert [k for k, _ in report.decisions] == sorted(k for k, _ in report.decisions)
    assert list(report.rows) == sorted(report.rows, key=lambda r: (r.key, r.status,
                                                                   r.residual_code or ""))
    store2, rs2 = w.world()
    w.ingest(store2, lines=list(reversed(lines)), aggs=list(reversed(aggs)))
    assert w.reconcile(store2, rs2) == report


@settings(max_examples=60, deadline=None)
@given(tol=st.one_of(st.text(max_size=6), st.integers(-5, 200), st.decimals(allow_nan=True)),
       today=st.text(max_size=12))
def test_property_hostile_arguments_raise_only_usage_errors(tol, today) -> None:
    store, rs = w.world()
    try:
        recon.reconcile_copilot(store, [rs], w.PRICER, since_ms=0, until_ms=w.DAY_MS,
                                tolerance_pct=tol, unexplained_pct="1", closed_only=False,
                                today=today)
    except UsageError:
        pass
