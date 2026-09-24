"""GitHub Copilot result types (CORE-AMENDMENTS C-11 … C-18): ingest / context carriers, pool
figures, reconciler decisions and the Copilot types, with validators and JSON round trips."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import replace

import pytest

from tokenbill.core import types as t
from tokenbill.core.builders import (
    FlatRates,
    make_activity,
    make_config,
    make_license,
    make_plan_evidence,
    make_pool_month,
)
from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import Basis, estimated, exact, zero
from tokenbill.core.records import from_json, to_json


def names(cls: type) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def _round_trip(obj: object) -> None:
    doc = json.loads(json.dumps(to_json(obj)))
    assert from_json(type(obj), doc) == obj


# ---------- C-11 / C-12 ----------


def test_ingest_result_and_options_additions() -> None:
    res = t.IngestResult(t.SourceInfo("s", "a", "h", "0" * 64, 0, None, None),
                         [], [], [], [], [], [], [], [], {}, frozenset())
    assert (res.licenses, res.activity, res.config) == ([], [], [])
    res.licenses.append(make_license())
    fresh = t.IngestResult(res.source, [], [], [], [], [], [], [], [], {}, frozenset())
    assert fresh.licenses == []
    assert names(t.IngestResult)[-4:] == ["naive_usage", "licenses", "activity", "config"]
    opts = t.IngestOptions()
    assert (opts.cost_center_map, opts.otel_service_names, opts.experimental) == ((), (),
                                                                                  frozenset())
    assert names(t.IngestOptions)[-4:] == ["now_ms", "cost_center_map", "otel_service_names",
                                           "experimental"]
    assert t.EXPERIMENTAL_FLAGS == {"copilot-store", "copilot-cli-otel-file",
                                    "copilot-jetbrains-otel", "copilot-report-quota"}
    # the dataclass accepts any flag; the CLI rejects unknown ones (UsageError)
    assert t.IngestOptions(experimental=frozenset({"x"})).experimental == {"x"}


# ---------- C-13: PlanEvidence ----------


def test_plan_evidence() -> None:
    ev = make_plan_evidence(plan="mixed", source="report_quota",
                            seats={"enterprise": 10, "business": 30}, conflict=True,
                            evidence=("report_quota: 3900 x 10",))
    assert ev.seats == (("business", 30), ("enterprise", 10))
    _round_trip(ev)
    unknown = make_plan_evidence()
    assert (unknown.plan, unknown.source, unknown.seats) == ("unknown", "none",
                                                             (("unknown", 100),))
    for kw in ({"plan": "team"}, {"source": "guess"}, {"entity_id": "org"},
               {"month": "2026-13"}, {"month": "2026-9"}, {"seats": {"pro": 1}},
               {"seats": {"business": -1}}, {"conflict": "yes"}, {"evidence": (1,)}):
        with pytest.raises(ContractViolation):
            make_plan_evidence(**kw)
    for entity in ("enterprise", "org:acme", "cc:platform team"):
        assert make_plan_evidence(entity_id=entity).entity_id == entity


# ---------- PoolMonth ----------


def test_pool_month_and_scenarios() -> None:
    pm = make_pool_month()
    assert pm.pool_credits == "190000" and pm.pool_nano == 1_900_000_000_000
    assert (pm.plan_source, pm.plan_scenario, pm.plan_conflict) == ("none", None, False)
    _round_trip(pm)
    fc = estimated(3_100_000 * 10**7, Basis.LIST_EQUIVALENT, low=2_920_000 * 10**7,
                   high=3_280_000 * 10**7, note="forecast p10-p90")
    open_month = replace(pm, finality="open", forecast=fc, days_final=20, days_provisional=3,
                         notes=("plan unknown: scenario business",), seats_source="report_users")
    _round_trip(open_month)
    biz = make_pool_month(seats={"unknown": "100"}, plan_scenario="business",
                          consumed_report_nano=250_000 * 10**7, plan_source="none")
    ent = make_pool_month(seats={"unknown": "100"}, plan_scenario="enterprise",
                          consumed_report_nano=250_000 * 10**7, plan_source="none")
    assert (biz.pool_credits, biz.regime, biz.overage_observed_nano) == ("190000", "overage",
                                                                         600 * 10**9)
    assert (ent.pool_credits, ent.regime, ent.overage_observed_nano) == ("390000", "slack", 0)
    with pytest.raises(ValueError):
        make_pool_month(seats={"unknown": "1"})
    assert names(t.PoolMonth)[-4:] == ["notes", "plan_source", "plan_scenario", "plan_conflict"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("entity_id", "budget:1"),
        ("month", "2026-09-01"),
        ("billing_mode", "prepaid"),
        ("seats", (("pro", "1"),)),
        ("seats", (("business", "1e2"),)),
        ("seats_source", "guess"),
        ("pool_credits", "lots"),
        ("pool_nano", "1"),
        ("pool_draw_nano", 1.5),
        ("direct_draws_pool", "maybe"),
        ("capped_policy", "throttle"),
        ("days_final", -1),
        ("finality", "final"),
        ("forecast", 5),
        ("regime", "fine"),
        ("notes", (1,)),
        ("plan_source", "vibes"),
        ("plan_scenario", "unknown"),
        ("plan_conflict", 1),
        ("promo", 7),
    ],
)
def test_pool_month_rejects(field: str, value: object) -> None:
    with pytest.raises(ContractViolation):
        replace(make_pool_month(), **{field: value})


# ---------- bill lines, admin actions, summary, FOCUS rows ----------


def test_bill_lines() -> None:
    assert t.BILL_LINES[:3] == ("seats.business", "seats.enterprise", "seats.unknown_plan")
    assert t.BILL_LINES[-1] == "total.invoice" and len(t.BILL_LINES) == 16
    line = t.CopilotBillLine("2026-10", "enterprise", "seats.unknown_plan", "100", "seats",
                             estimated(1_900 * 10**9, Basis.LIST, note="100 x $19 list"),
                             scenario="business")
    _round_trip(line)
    total = t.CopilotBillLine("2026-10", "enterprise", "total.invoice", None, None,
                              exact(10, Basis.INVOICE), components=("seats.business", "sandbox"))
    _round_trip(total)
    for kw in ({"line": "seats.pro"}, {"month": "2026"}, {"quantity": "x"}, {"amount": 5},
               {"components": ("nope",)}, {"scenario": "mixed"}, {"entity_id": "run"}):
        with pytest.raises(ContractViolation):
            replace(line, **kw)


def _action(**kw: object) -> t.AdminAction:
    base = dict(action_id="a1", lever_id="copilot.seat_reclaim",
                admin_action="rest:org_selected_users_delete", where="REST",
                what="Remove idle directly assigned seats", doc_url="https://docs.github.com/x",
                rest_file="github/requests.jsonl", auth_note="org owner", reach="0.8",
                projection=estimated(190 * 10**9, Basis.LIST, note="10 x $19"),
                deadline="2026-09-28", needs_eval=False, tradeoff=False)
    base.update(kw)
    return t.AdminAction(**base)  # type: ignore[arg-type]


def test_admin_action() -> None:
    a = _action()
    _round_trip(a)
    assert set(t.ADMIN_ACTION_WHERE) >= {"REST", "managed-settings.json", "workflow frontmatter"}
    for kw in ({"where": "the moon"}, {"what": "x" * 401}, {"reach": "1.5"}, {"reach": "-0.1"},
               {"reach": "high"}, {"deadline": "soon"}, {"needs_eval": None}, {"lever_id": 5},
               {"projection": "1"}):
        with pytest.raises(ContractViolation):
            _action(**kw)
    assert _action(reach=None, projection=None, deadline=None, lever_id=None).reach is None


def _plan() -> t.ActionPlan:
    return t.ActionPlan(joint_saving=zero(Basis.LIST), headline_monthly=zero(Basis.LIST),
                        allowance_headroom_monthly=None, levers=(), groups=(),
                        method="shapley-exact", shapley_se=(), sample="copilot cells",
                        pool_headroom_monthly=estimated(5, Basis.LIST_EQUIVALENT, note="h"))


def test_copilot_summary_and_run_result() -> None:
    summary = t.CopilotSummary(
        window=("2026-09-01", "2026-10-01"),
        lines=(),
        pools=(make_pool_month(seats={"unknown": "100"}, plan_scenario="business"),
               make_pool_month(seats={"unknown": "100"}, plan_scenario="enterprise")),
        teams=None,
        seat_counts=[("core", "business:none_90d", 7), ("(other)", "unknown:0-7", 12)],
        plan=None,
        actions=(_action(),),
        channel_verdicts=(("github_copilot", "reconciled"),
                          ("github_actions", "insufficient_data")),
        plan_status=(make_plan_evidence(),),
        plans_by_scenario=(("enterprise", _plan()), ("business", _plan())),
    )
    assert summary.seat_counts[0] == ("core", "business:none_90d", 7)
    assert [s for s, _ in summary.plans_by_scenario] == ["business", "enterprise"]
    assert summary.channel_verdicts[0][0] == "github_actions"
    _round_trip(summary)
    rr = t.RunResult(command="copilot scan", window=(0, 1), inputs=(),
                     privacy=t.PrivacyInfo("none", None, "install", 5, 0), rate_card=None,
                     copilot=summary)
    doc = to_json(rr)
    assert doc["copilot"]["plans_by_scenario"][0][0] == "business"
    assert from_json(t.RunResult, json.loads(json.dumps(doc))) == rr
    assert names(t.RunResult)[-1] == "copilot" and t.RunResult(
        "x", (0, 1), (), t.PrivacyInfo("none", None, "install", 5, 0), None).copilot is None
    for kw in ({"seat_counts": (("t", "b", -1),)}, {"seat_counts": (("t", "b"),)},
               {"plans_by_scenario": (("mixed", _plan()),)}, {"window": ("a",)},
               {"lines": ("x",)}, {"plan_status": (1,)}, {"teams": 1}, {"notes": (2,)}):
        with pytest.raises(ContractViolation):
            replace(summary, **kw)


def test_focus_row() -> None:
    row = t.FocusRow(columns=(("BilledCost", "12.5"), ("ServiceName", "GitHub Copilot"),
                              ("x_CopilotEntity", "enterprise")), channel="github_copilot",
                     reconciled=True)
    assert row.columns[0][0] == "BilledCost"  # column order is kept
    _round_trip(row)
    for cols in ((("x_lower", "1"),), (("x_A", "1"),), (("billed_cost", "1"),),
                 (("BilledCost", 1),), (("BilledCost", "1"), ("BilledCost", "2"))):
        with pytest.raises(ContractViolation):
            t.FocusRow(columns=cols, channel="github_copilot", reconciled=False)  # type: ignore[arg-type]


# ---------- C-14 … C-17 ----------


def test_analysis_context_additions() -> None:
    ctx = t.AnalysisContext(pricer=FlatRates(), rules=None, replayer=None,  # type: ignore[arg-type]
                            calibration=None, window=(0, 1), capabilities=frozenset())
    assert (ctx.licenses, ctx.activity, ctx.config, ctx.outcomes, ctx.pools, ctx.plans) == (
        (), (), (), (), (), ())
    assert ctx.reconciled_channels == frozenset() and ctx.recon_decisions == ()
    assert names(t.AnalysisContext)[-9:] == [
        "shard", "licenses", "activity", "config", "outcomes", "pools", "plans",
        "reconciled_channels", "recon_decisions"]
    full = replace(ctx, licenses=(make_license(),), activity=(make_activity(),),
                   config=(make_config(),), pools=(make_pool_month(),),
                   plans=(make_plan_evidence(),), reconciled_channels=frozenset({"github_copilot"}),
                   recon_decisions=(("convention:s_1", "excl"),))
    assert full.pools[0].regime == "slack"


def test_pool_figures() -> None:
    total = t.PricedTotal(zero(Basis.LIST), None, None, 0, 0, 0, "1")
    assert total.pool is None and names(t.PricedTotal)[-1] == "pool"
    with_pool = replace(total, pool=exact(5, Basis.LIST_EQUIVALENT))
    _round_trip(with_pool)
    day = t.ClusterDay("2026-09-01", "team", "core", None, None, 3, 10, 5, 0)
    assert day.pool_nano == 0 and replace(day, pool_nano=7).pool_nano == 7
    assert names(t.Finding)[-1] == "headroom"
    assert names(t.ActionPlan)[-1] == "pool_headroom_monthly"
    _round_trip(_plan())


def _report(decisions: object = ()) -> t.ReconciliationReport:
    from tokenbill.core.labels import Finality

    return t.ReconciliationReport(
        window=("2026-09-01", "2026-10-01"), tolerance_pct="2", unexplained_tolerance_pct="1",
        rows=(), token_coverage_pct=None, dollar_coverage_pct=None, rate_card_error=None,
        over_count_rows=0, effective_discount=(), residuals=(), unexplained_nano=0, channels=(),
        verdict="insufficient_data", finality=Finality.PROVISIONAL, suggested_contract=None,
        rerun_verdict=None, decisions=decisions)  # type: ignore[arg-type]


def test_reconciliation_decisions() -> None:
    assert t.RECON_DECISION_PREFIXES == ("convention:", "gross_is_list:", "plan_fit:")
    rep = _report([("plan_fit:enterprise:2026-09", "enterprise"), ("convention:s_abc", "incl"),
                   ("gross_is_list:org:acme:2026-09", "unknown")])
    assert [k for k, _ in rep.decisions] == ["convention:s_abc", "gross_is_list:org:acme:2026-09",
                                             "plan_fit:enterprise:2026-09"]
    _round_trip(rep)
    assert _report().decisions == () and names(t.ReconciliationReport)[-1] == "decisions"
    for bad in ([("convention:s", "maybe")], [("gross_is_list:e:2026-09", "yes")],
                [("plan_fit:e:2026-09", "mixed")], [("verdict:x", "excl")],
                [("convention:", "excl")], [("convention:s", "excl"), ("convention:s", "incl")],
                [("convention:s", 1)]):
        with pytest.raises(ContractViolation):
            _report(bad)
