"""verify.rollout: seeded plans, holdback, MDE from A/A, pre-registration, ITS for org-wide
changes (SPEC §13.2)."""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import pytest

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, estimated
from tokenbill.verify import rollout as R

from .panelgen import LEVEL_NANO, pre_panel

LEVER = "cc.prompt_cache_ttl.main"
LOOKS = ("2026-08-10", "2026-08-24")


def _clusters(n: int = 24) -> list[str]:
    return [f"c{i:02d}" for i in range(n)]


def _plan(**kw):
    args = {"lever_id": LEVER, "cluster_kind": "team", "design": "stepped_wedge", "waves": 4,
            "holdback": Decimal("0.25"), "seed": 7, "pre_panel": None, "projection": None,
            "washout_hours": 6, "looks": LOOKS}
    args.update(kw)
    clusters = args.pop("clusters", _clusters())
    return R.plan(clusters, **args)


def _saving(fraction: float):
    nano = round(fraction * LEVEL_NANO)
    return estimated(nano, Basis.LIST, calibration=Calibration.CALIBRATED)


def test_plan_is_seeded_deterministic_and_order_free() -> None:
    a = _plan()
    assert a == _plan(clusters=list(reversed(_clusters())))
    b = _plan(seed=8)
    assert (a.waves, a.holdback) != (b.waves, b.holdback)
    assert a.design == "stepped_wedge" and a.lever_id == LEVER and a.cluster_kind == "team"
    assert a.looks == LOOKS


def test_holdback_and_waves_cover_every_cluster_once() -> None:
    p = _plan(clusters=_clusters(40), holdback=Decimal("0.20"), waves=4)
    assert len(p.holdback) == 8
    assert [n for n, _ in p.waves] == [1, 2, 3, 4]
    assert [len(m) for _, m in p.waves] == [8, 8, 8, 8]
    everyone = list(p.holdback) + [c for _, m in p.waves for c in m]
    assert sorted(everyone) == _clusters(40)
    # the wave order is a seeded shuffle, not the sorted order
    assert everyone != sorted(everyone)
    small = _plan(clusters=_clusters(4), holdback="0.10", waves=9)
    assert len(small.holdback) == 1 and len(small.waves) == 3


def test_wave_order_never_depends_on_spend() -> None:
    pp = pre_panel(seed=1)
    tripled = [dataclasses.replace(r, cost_baseline_nano=r.cost_baseline_nano * 3
                                   if r.cluster_id in ("c01", "c05") else r.cost_baseline_nano)
               for r in pp]
    a, b = _plan(pre_panel=pp), _plan(pre_panel=tripled)
    assert (a.waves, a.holdback) == (b.waves, b.holdback) == \
        (_plan().waves, _plan().holdback)


def test_otel_tags_payloads_and_assignment_log() -> None:
    p = _plan()
    tags = dict(p.otel_tags)
    assert sorted(tags) == _clusters()
    for c in p.holdback:
        assert tags[c] == "tokenbill.arm=control,tokenbill.wave=0"
    for n, members in p.waves:
        for c in members:
            assert tags[c] == f"tokenbill.arm={LEVER},tokenbill.wave={n}"
    assert R.payload_names(p)[0] == (1, f"tokenbill-{LEVER}-wave1")
    log = R.assignment_log_text(p)
    lines = [json.loads(line) for line in log.splitlines()]
    assert len(lines) == 24 and lines[0]["position"] == 0 and lines[0]["seed"] == 7
    assert {ln["arm"] for ln in lines} == {"control", LEVER}
    assert R.verify_assignment(p) and R.verify_assignment(p, log)
    assert not R.verify_assignment(p, log.replace('"wave":1', '"wave":2'))
    assert R.is_randomized(p)
    tampered = dataclasses.replace(p, preregistration_json=p.preregistration_json + " ")
    assert not R.verify_assignment(tampered) and not R.is_randomized(tampered)
    with pytest.raises(UsageError):
        R.preregistration(tampered)
    arms = R.arms_for(p, {1: "2026-08-03"})
    first = p.waves[0][1][0]
    assert arms[first] == f"{LEVER}@2026-08-03" and arms[p.holdback[0]] == "control"
    assert R.arms_for(p)[p.waves[1][1][0]] == LEVER
    with pytest.raises(UsageError):
        R.arms_for(p, {1: "soon"})


def test_preregistration_contents() -> None:
    proj = _saving(0.5)
    p = _plan(projection=proj, rate_card_sha256="ab" * 32)
    pre = R.preregistration(p)
    for key in ("lever", "metric", "estimator", "looks", "rate_card_sha256", "mde", "projection"):
        assert key in pre
    assert pre["lever"] == LEVER and pre["metric"] == R.METRIC
    assert pre["estimator"] == "imputation_did" and pre["looks"] == list(LOOKS)
    assert pre["rate_card_sha256"] == "ab" * 32
    assert pre["projection"]["nano"] == proj.nano
    assert pre["assignment_log_sha256"] == p.assignment_log_sha256
    assert pre["seed"] == 7 and pre["assignment"] == "seeded"
    # no floats anywhere in the pre-registration
    json.loads(p.preregistration_json, parse_float=lambda s: pytest.fail(f"float {s}"))
    assert set(pre["arm_shares"]) == {"control", "treatment"}


def test_mde_from_200_aa_rerandomizations_and_refusal() -> None:
    """Acceptance: MDE refusal when the planted effect is below the MDE."""
    pp = pre_panel(seed=2)
    big = _plan(pre_panel=pp, projection=_saving(0.25))
    assert big.mde_nano is not None and big.mde_nano > 0
    assert big.verification_design and big.clusters_needed is None
    small = _plan(pre_panel=pp, projection=_saving(0.002))
    assert small.mde_nano == big.mde_nano
    assert 5 * small.mde_nano > 4 * small.projection.nano
    assert not small.verification_design
    assert small.clusters_needed is not None and small.clusters_needed > 24
    assert any("verification needs about" in w for w in small.warnings)
    no_pre = _plan(projection=_saving(0.25))
    assert no_pre.mde_nano is None and not no_pre.verification_design
    rct = _plan(design="cluster_rct", pre_panel=pp, waves=3, projection=_saving(0.25))
    assert rct.mde_nano is not None and len(rct.waves) == 1
    assert any("one wave" in w for w in rct.warnings)
    unpriced = _plan(projection=estimated(None, Basis.LIST, note="unpriced: x"))
    assert not unpriced.verification_design
    assert any("no projection" in w for w in _plan().warnings)


def test_washout_is_at_least_one_hour() -> None:
    assert _plan(washout_hours=0).washout_hours == 1
    assert _plan(washout_hours=30).washout_hours == 30
    assert _plan(cluster_kind="mdm_group", washout_hours=0).washout_hours == 1


def test_spend_targeted_user_assignment_is_capped_at_measured() -> None:
    """Acceptance: a user assignment that treats the biggest spenders first is flagged."""
    pp = pre_panel(seed=3)
    spend: dict[str, int] = {}
    for r in pp:
        spend[r.cluster_id] = spend.get(r.cluster_id, 0) + r.cost_baseline_nano
    ranked = sorted(spend, key=lambda c: -spend[c])
    treated = {c: str(1 + i * 4 // 18) for i, c in enumerate(ranked[:18])}
    treated.update({c: "control" for c in ranked[18:]})
    p = _plan(pre_panel=pp, treated=treated)
    pre = R.preregistration(p)
    assert pre["spend_targeted"] is True and pre["assignment"] == "user"
    assert not p.verification_design and not R.is_randomized(p)
    assert any("regression to the mean" in w for w in p.warnings)
    assert set(p.holdback) == set(ranked[18:])
    # an assignment unrelated to spend is not flagged (but still has no logged seed)
    shuffled = sorted(spend)
    fair = {c: ("control" if i % 4 == 0 else str(1 + i % 3)) for i, c in enumerate(shuffled)}
    q = _plan(pre_panel=pp, treated=fair)
    assert R.preregistration(q)["spend_targeted"] is False
    assert any("no logged seed" in w for w in q.warnings)
    no_pre = _plan(treated=fair)
    assert R.preregistration(no_pre)["spend_targeted"] is None
    assert any("cannot be checked" in w for w in no_pre.warnings)
    assert R.spend_targeting(pp[:3], {"c00": 1}) is None


def test_org_wide_without_mdm_or_gateway_is_its() -> None:
    """Acceptance: org-wide delivery without MDM/gateway clusters → design its, no verification
    design, pre-registered change and placebo dates."""
    pp = pre_panel(seed=4)
    p = _plan(org_wide_delivery=True, pre_panel=pp, projection=_saving(0.25),
              treated={"c00": "1"})
    assert p.design == "its" and not p.verification_design
    assert R.ORG_WIDE_WARNING in p.warnings
    assert p.holdback == () and p.waves == ((1, tuple(_clusters())),)
    pre = R.preregistration(p)
    assert pre["estimator"] == "event_study_its"
    assert pre["change_date"] == "2026-06-29" and pre["placebo_date"] == "2026-06-15"
    assert pre["mde"] is not None
    assert not R.is_randomized(p)
    explicit = _plan(org_wide_delivery=True, change_date="2026-07-06")
    assert R.preregistration(explicit)["change_date"] == "2026-07-06"
    assert any("no placebo date" in w for w in explicit.warnings)
    bare = _plan(design="its")
    assert any("no change date" in w for w in bare.warnings)
    mdm = _plan(org_wide_delivery=True, cluster_kind="mdm_group")
    assert mdm.design == "stepped_wedge" and R.ORG_WIDE_WARNING in mdm.warnings


def test_validation() -> None:
    bad = [
        {"clusters": []}, {"clusters": ["a", "a"]}, {"clusters": ["a", ""]},
        {"lever_id": "a,b"}, {"lever_id": ""}, {"cluster_kind": "person"},
        {"design": "twfe"}, {"waves": 0}, {"holdback": 0.2}, {"holdback": True},
        {"holdback": "abc"}, {"holdback": "NaN"}, {"holdback": Decimal("0.3")},
        {"washout_hours": -1}, {"looks": ["someday"]}, {"projection": 5}, {"seed": "7"},
        {"clusters": ["only"]},
        {"treated": {"zz": "1"}}, {"treated": {"c00": "1"}},
        {"treated": {c: "x" for c in _clusters()}},
        {"treated": {c: "0" for c in _clusters()}},
        {"treated": {c: "wave0" if c == "c00" else "control" for c in _clusters()}},
        {"design": "its", "change_date": "later"},
    ]
    for kw in bad:
        with pytest.raises(UsageError):
            _plan(**kw)


def test_projection_per_dev_day() -> None:
    monthly = estimated(30_000 * 10**9, Basis.LIST, calibration=Calibration.CALIBRATED)
    per_day = R.projection_per_dev_day(monthly, 1500)
    assert per_day.nano == 20 * 10**9 and per_day.calibration is Calibration.CALIBRATED
    with pytest.raises(UsageError):
        R.projection_per_dev_day(monthly, 0)
