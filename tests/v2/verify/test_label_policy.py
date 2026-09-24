"""verify.label_policy: guards, decide, signable and the whole measurement (SPEC §13.4)."""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
from decimal import Decimal

import pytest

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, estimated
from tokenbill.core.types import GuardResult, PanelRow
from tokenbill.verify import label_policy as L
from tokenbill.verify import rollout as R

from .helpers import recon
from .panelgen import LEVEL_NANO, day, org_series, pre_panel, rollout_panel

LEVER = "cc.prompt_cache_ttl.main"
LOOK = "2026-08-10"
RATE_SHA = "ab" * 32
OK_RECON = recon({"anthropic_api": "reconciled"})


def _saving(fraction: float, cal: Calibration = Calibration.CALIBRATED):
    return estimated(round(fraction * LEVEL_NANO), Basis.LIST, calibration=cal)


@functools.cache
def _pre() -> tuple[PanelRow, ...]:
    return tuple(pre_panel(seed=21))


@functools.cache
def _cached_plan(items: tuple) -> R.MeasurePlan:
    args = {"lever_id": LEVER, "cluster_kind": "team", "design": "stepped_wedge", "waves": 4,
            "holdback": Decimal("0.25"), "seed": 7, "pre_panel": _pre(),
            "projection": _saving(0.25), "washout_hours": 6, "looks": (LOOK,)}
    args.update(dict(items))
    return R.plan([f"c{i:02d}" for i in range(24)], **args)


def _plan(**kw):
    return _cached_plan(tuple(sorted(kw.items())))


def _panel_for(plan, **kw):
    return rollout_panel(seed=21, assignment=(plan.holdback, [m for _, m in plan.waves]), **kw)


def _with_prereg(plan, **updates):
    pre = json.loads(plan.preregistration_json)
    pre.update(updates)
    text = R.canonical_json(pre)
    return dataclasses.replace(plan, preregistration_json=text,
                               preregistration_sha256=hashlib.sha256(text.encode()).hexdigest())


def _measure(plan, rows, **kw):
    args = {"plan": plan, "reconciliation": OK_RECON, "look": LOOK, "rate_card_sha256": RATE_SHA,
            "channels": ["anthropic_api"], "cache_scopes": {"ws:1": ("c00",)}, "boot": 200,
            "seed": 0}
    args.update(kw)
    return L.measure(rows, **args)


def _guard(result, name: str) -> GuardResult:
    return next(g for g in result.guards if g.name == name)


# ---------------------------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------------------------


def test_stepped_wedge_with_every_guard_passing_is_verified_and_signable() -> None:
    plan = _plan()
    g = _panel_for(plan)
    m = _measure(plan, g.rows)
    assert [x.name for x in m.guards] == list(L.GUARD_NAMES)
    assert all(x.passed for x in m.guards), [x for x in m.guards if not x.passed]
    assert m.estimate.evidence is Evidence.VERIFIED and m.estimate.ci_level_pct == 95
    assert m.estimate.low_nano <= m.estimate.nano <= m.estimate.high_nano
    assert m.estimate.nano == pytest.approx(-g.truth_att, rel=0.05)     # saving = −ATT
    assert m.signable
    assert m.unit == R.METRIC and m.design == "stepped_wedge" and m.lever_id == LEVER
    assert m.scope_label.startswith("fleet:") and dict(m.window)["since"] == day(0)
    assert m.assignment_log_sha256 == plan.assignment_log_sha256
    assert m.preregistration_sha256 == plan.preregistration_sha256
    assert m.rate_variance is not None and m.rate_variance.evidence is Evidence.EXACT
    assert m.rate_variance.nano == 0
    point, lo, hi = m.realization_rate
    assert Decimal(lo) <= Decimal(point) <= Decimal(hi)
    assert Decimal(point) == pytest.approx(Decimal(m.estimate.nano) / Decimal(plan.projection.nano))
    scope = dict(m.scope)
    assert scope["clusters"] == 24 and 0 < scope["treated_unit_days"] < scope["unit_days"]
    assert any("washout" in a for a in m.adjustments)


def test_price_cut_moves_only_the_rate_variance() -> None:
    """Acceptance: a simultaneous 20% price cut leaves the constant-price estimate unchanged and
    appears as EXACT rate variance."""
    plan = _plan()
    plain = _measure(plan, _panel_for(plan).rows)
    cut_panel = _panel_for(plan, price_cut=0.2)
    cut = _measure(plan, cut_panel.rows)
    assert cut.estimate == plain.estimate
    assert cut.rate_variance.evidence is Evidence.EXACT
    post = sum(r.cost_baseline_nano for r in cut_panel.rows if r.date_utc >= cut_panel.first_start)
    assert cut.rate_variance.nano == pytest.approx(-0.2 * post, rel=1e-6)


def test_srm_60_40_realized_vs_50_50_planned_is_not_verified() -> None:
    """Acceptance: 60/40 realized vs 50/50 planned → SRM fails → not VERIFIED."""
    plan = _with_prereg(_plan(), arm_shares={"control": "0.5", "treatment": "0.5"})
    ctrl = set(plan.holdback)
    rows = [PanelRow(sorted(ctrl)[0], "2026-06-01", 10, 10, 4000, None, None, False),
            PanelRow(plan.waves[0][1][0], "2026-06-01", 10, 10, 6000, None, None, False)]
    srm = L.guards({"panel": rows}, plan=plan, reconciliation=OK_RECON, projection=None)[0]
    assert srm.name == "srm" and not srm.passed and "p=" in srm.value
    balanced = [dataclasses.replace(rows[0], active_dev_days=5000),
                dataclasses.replace(rows[1], active_dev_days=5000)]
    assert L.guards({"panel": balanced}, plan=plan, reconciliation=OK_RECON,
                    projection=None)[0].passed
    # end to end: the measurement cannot be VERIFIED
    g = _panel_for(plan)
    skewed = [dataclasses.replace(r, active_dev_days=r.active_dev_days * 3)
              if r.cluster_id in ctrl else r for r in g.rows]
    m = _measure(plan, skewed)
    assert not _guard(m, "srm").passed
    assert m.estimate.evidence is Evidence.MEASURED


def test_planted_pre_trend_fails_the_placebo_guard() -> None:
    plan = _plan()
    rows = rollout_panel(seed=21, assignment=(plan.holdback, [m for _, m in plan.waves]),
                         pre_weeks=4, weeks=12, pre_trend=0.6).rows
    m = _measure(plan, rows)
    assert not _guard(m, "placebo").passed
    assert m.estimate.evidence is Evidence.MEASURED and "placebo" in m.estimate.note


def test_mde_refusal_caps_the_label() -> None:
    plan = _plan(projection=_saving(0.002))
    assert not plan.verification_design
    m = _measure(plan, _panel_for(plan).rows)
    assert not _guard(m, "mde").passed
    assert m.estimate.evidence is Evidence.MEASURED


def test_spend_targeted_assignment_caps_the_label_at_measured() -> None:
    plan = _with_prereg(_plan(), spend_targeted=True)
    m = _measure(plan, _panel_for(plan).rows)
    assert not _guard(m, "assignment_not_spend_targeted").passed
    assert m.estimate.evidence is Evidence.MEASURED
    assert m.signable       # MEASURED receipts can still be signed


def test_unregistered_look_fails_its_guard() -> None:
    plan = _plan()
    m = _measure(plan, _panel_for(plan).rows, look="2026-08-03")
    assert not _guard(m, "registered_looks").passed
    assert m.estimate.evidence is Evidence.MEASURED
    m2 = _measure(plan, _panel_for(plan).rows, looks_taken=[LOOK, "2026-08-01"])
    assert not _guard(m2, "registered_looks").passed


def test_unreconciled_channel_makes_the_measurement_unsignable() -> None:
    """Acceptance: a panel covering an unreconciled channel → signable=False."""
    plan = _plan()
    rows = _panel_for(plan).rows
    report = recon({"anthropic_api": "reconciled", "bedrock": "insufficient_data"})
    m = _measure(plan, rows, reconciliation=report, channels=["anthropic_api", "bedrock"])
    rg = _guard(m, "reconciliation")
    assert not rg.passed and "bedrock" in rg.value
    assert not m.signable
    # the Bedrock gap does not block a panel that covers only the API channel
    m2 = _measure(plan, rows, reconciliation=report, channels=["anthropic_api"])
    assert _guard(m2, "reconciliation").passed and m2.signable


def test_uncalibrated_projection_and_allowance_are_unsignable() -> None:
    plan = _plan(projection=_saving(0.25, Calibration.UNCALIBRATED))
    m = _measure(plan, _panel_for(plan).rows)
    assert m.estimate.evidence is Evidence.VERIFIED and not m.signable
    plan2 = _plan()
    allowance = _measure(plan2, _panel_for(plan2).rows, basis=Basis.LIST_EQUIVALENT)
    assert allowance.estimate.basis is Basis.LIST_EQUIVALENT and not allowance.signable


def test_cluster_rct_end_to_end() -> None:
    plan = _plan(design="cluster_rct", waves=1)
    rows = rollout_panel(seed=21, design="cluster_rct",
                         assignment=(plan.holdback, [m for _, m in plan.waves])).rows
    m = _measure(plan, rows, washout_days=2)
    assert m.design == "cluster_rct"
    assert m.estimate.evidence in (Evidence.VERIFIED, Evidence.MEASURED)
    assert m.estimate.nano > 0
    assert any("CUPED" in a for a in m.adjustments)
    assert _guard(m, "washout").passed
    short = _measure(plan, rows, washout_days=0)
    assert not _guard(short, "washout").passed


def test_its_org_wide_is_measured_at_most() -> None:
    """Acceptance: plan(org_wide_delivery=True) → its; the org series is MEASURED, never
    VERIFIED; a planted pre-trend leaves it unlabeled (ESTIMATED, unsignable)."""
    series, truth = org_series(seed=0)
    pp = [PanelRow("org", d, c, c, n, None, None, False) for d, c, n in series if d < day(90)]
    plan = R.plan(["org"], lever_id="cc.default_model", cluster_kind="team", design="its",
                  waves=1, holdback=Decimal("0.2"), seed=1, pre_panel=pp, projection=None,
                  washout_hours=1, looks=(LOOK,), org_wide_delivery=True)
    assert plan.design == "its" and not plan.verification_design
    rows = [PanelRow("org", d, c, c, n, None, None, d >= day(90)) for d, c, n in series]
    m = _measure(plan, rows, cache_scopes=None)
    assert m.estimate.evidence is Evidence.MEASURED
    assert m.estimate.nano == pytest.approx(-truth, rel=0.05)
    assert _guard(m, "placebo").passed and _guard(m, "srm").value.startswith("n/a")
    assert m.assignment_log_sha256 is None and m.signable
    kinked, _ = org_series(seed=0, pre_ramp=(40, 10, -0.06))
    bad = [PanelRow("org", d, c, c, n, None, None, d >= day(90)) for d, c, n in kinked]
    m2 = _measure(plan, bad, cache_scopes=None)
    assert m2.estimate.evidence is Evidence.ESTIMATED and not m2.signable
    assert "not a measurement" in m2.estimate.note
    # without a pre-registered change date the first treated day is used
    bare = _with_prereg(plan, change_date=None, placebo_date=None)
    m3 = _measure(bare, rows, cache_scopes=None)
    assert m3.estimate.evidence is Evidence.MEASURED
    never = [dataclasses.replace(r, treated=False) for r in rows]
    with pytest.raises(UsageError):
        _measure(bare, never)


# ---------------------------------------------------------------------------------------------
# individual guards
# ---------------------------------------------------------------------------------------------


def test_guards_with_missing_inputs_fail() -> None:
    plan = _plan()
    res = {g.name: g for g in L.guards({}, plan=plan, reconciliation=OK_RECON,
                                       projection=plan.projection)}
    assert not res["srm"].passed and not res["placebo"].passed
    assert not res["registered_looks"].passed and not res["washout"].passed
    assert not res["cluster_cache_scope"].passed
    assert res["quality_noninferiority"].passed and res["mde"].passed
    assert res["reconciliation"].passed           # overall verdict when channels are not given
    its_plan = _plan(design="its")
    res = {g.name: g for g in L.guards({}, plan=its_plan, reconciliation=OK_RECON,
                                       projection=None)}
    assert not res["placebo"].passed and res["srm"].passed and res["washout"].passed
    assert res["cluster_cache_scope"].passed
    no_shares = _with_prereg(plan, arm_shares=None)
    rows = _panel_for(plan).rows
    assert not L.guards({"panel": rows}, plan=no_shares, reconciliation=OK_RECON,
                        projection=None)[0].passed
    broken = _with_prereg(plan, arm_shares={"control": "x", "treatment": "1"})
    assert L.guards({"panel": rows}, plan=broken, reconciliation=OK_RECON,
                    projection=None)[0].value == "not computable"


def test_guard_inputs_are_type_checked() -> None:
    plan = _plan()
    for bad in ({"panel": [object()]}, {"placebo": (1, 2)}, {"cache_scopes": ["x"]},
                {"quality_lower": "low"}, {"boot": "many", "panel": []}):
        with pytest.raises(UsageError):
            L.guards(bad, plan=plan, reconciliation=OK_RECON, projection=None)
    with pytest.raises(UsageError):
        L.guards({}, plan="plan", reconciliation=OK_RECON, projection=None)  # type: ignore
    with pytest.raises(UsageError):
        L.guards({}, plan=plan, reconciliation="ok", projection=None)  # type: ignore


def test_placebo_computed_from_the_panel_when_absent() -> None:
    plan = _plan()
    rows = _panel_for(plan).rows
    got = {g.name: g for g in L.guards({"panel": rows, "boot": 100}, plan=plan,
                                       reconciliation=OK_RECON, projection=None)}
    assert got["placebo"].passed
    rct = _plan(design="cluster_rct", waves=1)
    rct_rows = rollout_panel(seed=21, design="cluster_rct",
                             assignment=(rct.holdback, [m for _, m in rct.waves])).rows
    got = {g.name: g for g in L.guards({"panel": rct_rows, "boot": 100}, plan=rct,
                                       reconciliation=OK_RECON, projection=None)}
    assert got["placebo"].passed
    short = [r for r in rows if r.date_utc >= day(12)]
    got = {g.name: g for g in L.guards({"panel": short, "boot": 50}, plan=plan,
                                       reconciliation=OK_RECON, projection=None)}
    assert not got["placebo"].passed and "not computable" in got["placebo"].value


def test_mde_guard() -> None:
    plan = _plan()
    ok = L.guards({}, plan=plan, reconciliation=OK_RECON, projection=_saving(0.25))[2]
    assert ok.name == "mde" and ok.passed
    assert not L.guards({}, plan=plan, reconciliation=OK_RECON,
                        projection=_saving(0.0001))[2].passed
    assert not L.guards({}, plan=plan, reconciliation=OK_RECON,
                        projection=estimated(None, Basis.LIST, note="unpriced: x"))[2].passed
    no_mde = dataclasses.replace(plan, mde_nano=None)
    assert not L.guards({}, plan=no_mde, reconciliation=OK_RECON,
                        projection=_saving(0.25))[2].passed
    assert L.guards({}, plan=plan, reconciliation=OK_RECON, projection=None)[2].value.startswith(
        "n/a")


def test_reconciliation_guard_window_and_overall() -> None:
    plan = _plan()
    narrow = recon({"anthropic_api": "reconciled"}, window=("2026-07-01", "2026-12-31"))
    g = L.guards({"window": ("2026-06-01", "2026-08-09"), "channels": ["anthropic_api"]},
                 plan=plan, reconciliation=narrow, projection=None)[3]
    assert not g.passed and "window" in g.value
    assert not L.guards({"channels": []}, plan=plan, reconciliation=OK_RECON,
                        projection=None)[3].passed
    bad = recon({"anthropic_api": "not_reconciled"})
    assert not L.guards({}, plan=plan, reconciliation=bad, projection=None)[3].passed


def test_cache_scope_guard() -> None:
    plan = _plan()
    spans = {"ws:shared": ("c00", "c01"), "ws:solo": ("c02",)}
    g = L.guards({"cache_scopes": spans}, plan=plan, reconciliation=OK_RECON,
                 projection=None)[4]
    assert not g.passed and g.value.startswith("1 of 2")
    rate_lever = _plan(lever_id="geo.global")
    assert L.guards({}, plan=rate_lever, reconciliation=OK_RECON,
                    projection=None)[4].value.startswith("n/a")
    unknown = _plan(lever_id="custom.lever")
    assert not L.guards({}, plan=unknown, reconciliation=OK_RECON, projection=None)[4].passed


def test_quality_noninferiority_guard() -> None:
    plan = _plan()
    good = _panel_for(plan, prs=True, pr_effect=0.0).rows
    worse = _panel_for(plan, prs=True, pr_effect=-0.25).rows
    kw = {"plan": plan, "reconciliation": OK_RECON, "projection": None}
    assert L.guards({"panel": good, "boot": 200, "washout_days": 1}, **kw)[5].passed
    q = L.guards({"panel": worse, "boot": 200, "washout_days": 1}, **kw)[5]
    assert q.name == "quality_noninferiority" and not q.passed
    assert L.guards({"quality_lower": -0.01}, **kw)[5].passed
    assert not L.guards({"quality_lower": -0.2}, **kw)[5].passed
    its_plan = _plan(design="its")
    assert L.guards({"panel": worse}, plan=its_plan, reconciliation=OK_RECON,
                    projection=None)[5].value.startswith("n/a")
    few = worse[:30]
    assert not L.guards({"panel": few, "boot": 20}, **kw)[5].passed


# ---------------------------------------------------------------------------------------------
# decide and signable
# ---------------------------------------------------------------------------------------------

PASS = (GuardResult("srm", True, "p=0.5", "p ≥ 0.001"),
        GuardResult("placebo", True, "includes 0", "placebo CI includes 0"))
FAIL = (GuardResult("srm", False, "p=0", "p ≥ 0.001"),
        GuardResult("placebo", True, "includes 0", "placebo CI includes 0"))


@pytest.mark.parametrize("design", ["cluster_rct", "stepped_wedge", "ab"])
def test_decide_randomized_designs(design: str) -> None:
    kw = {"design": design, "randomized": True, "assignment_hash_matches": True}
    assert L.decide(guards=PASS, ci=(-10, -2), **kw) is Evidence.VERIFIED
    assert L.decide(guards=PASS, ci=(-10, 2), **kw) is Evidence.MEASURED   # CI spans 0
    assert L.decide(guards=FAIL, ci=(-10, -2), **kw) is Evidence.MEASURED
    assert L.decide(guards=PASS, ci=(-10, -2), design=design, randomized=False,
                    assignment_hash_matches=True) is Evidence.MEASURED
    assert L.decide(guards=PASS, ci=(-10, -2), design=design, randomized=True,
                    assignment_hash_matches=False) is Evidence.MEASURED
    assert L.decide(guards=PASS, ci=None, **kw) is Evidence.ESTIMATED  # type: ignore[arg-type]
    assert L.decide(guards=PASS, ci=(5, 1), **kw) is Evidence.ESTIMATED


def test_decide_its_is_never_verified() -> None:
    kw = {"design": "its", "randomized": True, "assignment_hash_matches": True}
    assert L.decide(guards=PASS, ci=(-10, -2), **kw) is Evidence.MEASURED
    no_placebo = (PASS[0], GuardResult("placebo", False, "excludes 0", ""))
    assert L.decide(guards=no_placebo, ci=(-10, -2), **kw) is Evidence.ESTIMATED
    assert L.decide(guards=(), ci=(-10, -2), **kw) is Evidence.ESTIMATED
    with pytest.raises(UsageError):
        L.decide(guards=PASS, ci=(1, 2), design="pre_post", randomized=True,
                 assignment_hash_matches=True)


def test_signable() -> None:
    cal = _saving(0.25)
    uncal = _saving(0.25, Calibration.UNCALIBRATED)
    assert L.signable(Evidence.VERIFIED, reconciled=True, projection=None)
    assert L.signable(Evidence.MEASURED, reconciled=True, projection=cal)
    assert not L.signable(Evidence.MEASURED, reconciled=True, projection=uncal)
    assert not L.signable(Evidence.VERIFIED, reconciled=False, projection=None)
    assert not L.signable(Evidence.ESTIMATED, reconciled=True, projection=None)
    assert not L.signable(Evidence.EXACT, reconciled=True, projection=None)
    assert L.signable("measured", reconciled=True, projection=None)  # type: ignore[arg-type]
    assert not L.signable("bogus", reconciled=True, projection=None)  # type: ignore[arg-type]


def test_measure_validation() -> None:
    plan = _plan()
    with pytest.raises(UsageError):
        _measure(plan, [])
    untreated = [dataclasses.replace(r, treated=False) for r in _panel_for(plan).rows]
    with pytest.raises(UsageError):
        _measure(plan, untreated)
    odd = dataclasses.replace(plan, design="pre_post")
    with pytest.raises(UsageError):
        _measure(odd, _panel_for(plan).rows)
