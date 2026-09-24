"""verify.ab: the paired lab comparison (SPEC §13.5)."""

from __future__ import annotations

import dataclasses
import hashlib
import re
from decimal import Decimal

import pytest

from tokenbill.core.builders import CANARY, FlatRates, assert_no_canary, make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import to_json
from tokenbill.core.testing import FakePricer
from tokenbill.verify import ab as A

from .helpers import MODEL, rtk_campaign, ts


def test_rtk_like_campaign_is_costlier_and_verified_at_lab_scope() -> None:
    """Acceptance: tokens −38%, turns +14%, cost +7% → costlier; VERIFIED only at the lab scope,
    with randomized order and ≥ 5 trials per task-arm."""
    base, cand, outcomes = rtk_campaign(seed=1)
    res = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), seed=3)
    assert res.verdict == "costlier"
    assert Decimal(res.token_delta_pct) == pytest.approx(Decimal("-38"), abs=Decimal("2"))
    assert Decimal(res.turn_delta_pct) == pytest.approx(Decimal("14"), abs=Decimal("3"))
    assert Decimal(res.success_delta_pct) == 0
    assert Decimal(res.read_delta_pct) < Decimal("-40")
    b_cps, c_cps = res.cost_per_success
    assert c_cps.nano / b_cps.nano == pytest.approx(1.07, abs=0.03)
    task_ids = sorted({o["task_id"] for o in outcomes})
    digest = hashlib.sha256("\n".join(task_ids).encode()).hexdigest()[:12]
    assert res.scope_label == f"lab:{digest}" == A.lab_scope_label(task_ids)
    assert res.randomized_order and res.trials_per_arm == (5, 5) and res.n_tasks == 20
    for fig in (b_cps, c_cps, res.paired_difference, res.measurement.estimate):
        assert fig.evidence is Evidence.VERIFIED and fig.ci_level_pct == 95
        assert fig.low_nano <= fig.nano <= fig.high_nano
        assert fig.basis is Basis.LIST
    assert res.paired_difference.low_nano > 0
    m = res.measurement
    assert m.design == "ab" and m.unit == "cost per task" and m.scope_label == res.scope_label
    assert m.estimate.nano == -res.paired_difference.nano
    assert m.estimate.nano < 0 and m.estimate.high_nano < 0          # a negative saving
    assert not m.signable and m.projected is None and m.rate_variance is None
    assert m.rate_card_sha256 == "flat" and dict(m.scope)["tasks"] == 20
    assert all(g.passed for g in m.guards)


def test_fixed_order_or_too_few_trials_is_measured() -> None:
    base, cand, outcomes = rtk_campaign(seed=2, randomized=False)
    res = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=2000)
    assert res.verdict == "costlier" and not res.randomized_order
    assert res.measurement.estimate.evidence is Evidence.MEASURED
    base, cand, outcomes = rtk_campaign(seed=2, trials=4)
    res = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=2000)
    assert res.trials_per_arm == (4, 4)
    assert res.measurement.estimate.evidence is Evidence.MEASURED
    unordered = [{k: v for k, v in o.items() if k != "order"} for o in outcomes]
    res = A.paired_ab(base, cand, unordered, pricer=FlatRates(), boot=500)
    assert not res.randomized_order


def test_cheaper_and_no_difference_verdicts() -> None:
    base, cand, outcomes = rtk_campaign(seed=4, cand_scale=0.7)
    res = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=2000)
    assert res.verdict == "cheaper" and res.measurement.estimate.nano > 0
    same_b, _, outcomes = rtk_campaign(seed=5)
    twin = [dataclasses.replace(r, lane_key="T" + r.lane_key) for r in same_b]
    res = A.paired_ab(same_b, twin, outcomes, pricer=FlatRates(), boot=2000)
    assert res.verdict == "no-difference"
    assert res.measurement.estimate.evidence is Evidence.MEASURED   # CI includes 0


def test_deterministic_per_seed() -> None:
    base, cand, outcomes = rtk_campaign(seed=6, tasks=8)
    a = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=1000, seed=1)
    assert a == A.paired_ab(list(reversed(base)), cand, list(reversed(outcomes)),
                            pricer=FlatRates(), boot=1000, seed=1)
    b = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=1000, seed=2)
    assert a.cost_per_success[0].nano == b.cost_per_success[0].nano
    assert a.cost_per_success[0].low_nano != b.cost_per_success[0].low_nano


def test_failures_stay_in_the_cost_per_success_numerator() -> None:
    base, cand, outcomes = rtk_campaign(seed=7, tasks=6)
    worse = [dict(o, success=False) if o["arm"] == "candidate" and o["trial"] in (1, 2) else o
             for o in outcomes]
    res = A.paired_ab(base, cand, worse, pricer=FlatRates(), boot=500)
    ok = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=500)
    assert res.cost_per_success[1].nano == pytest.approx(
        ok.cost_per_success[1].nano * 4 / 2, rel=1e-6)
    assert Decimal(res.success_delta_pct) == Decimal("-50.00")


def test_task_ids_never_leak_into_results() -> None:
    base, cand, outcomes = rtk_campaign(seed=8, tasks=6, task_prefix=f"secret {CANARY}")
    res = A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=300)
    assert_no_canary(repr(res), str(to_json(res)))
    assert re.fullmatch(r"lab:[0-9a-f]{12}", res.scope_label)


def test_allowance_and_mixed_bases() -> None:
    base, cand, outcomes = rtk_campaign(seed=9, tasks=4)
    sub = [dataclasses.replace(r, attempts=tuple(
        dataclasses.replace(a, inferences=tuple(
            dataclasses.replace(i, pricing=dataclasses.replace(i.pricing,
                                                               billing_path="subscription"))
            for i in a.inferences)) for a in r.attempts)) for r in base + cand]
    res = A.paired_ab(sub[:len(base)], sub[len(base):], outcomes, pricer=FlatRates(), boot=300)
    assert res.cost_per_success[0].basis is Basis.LIST_EQUIVALENT
    with pytest.raises(UsageError):
        A.paired_ab(sub[:len(base)], cand, outcomes, pricer=FlatRates(), boot=300)


def test_validation() -> None:
    base, cand, outcomes = rtk_campaign(seed=10, tasks=3)
    no_task = make_request("L-x", 0, ts("2026-09-01"), {"output": 10}, MODEL)
    unpriced = make_request("L-y", 0, ts("2026-09-01"), {"output": 10}, "no-such-model",
                            attribution={"extra": (("task_id", "task-000"),)})
    bad_calls = [
        lambda: A.paired_ab(base + [no_task], cand, outcomes, pricer=FlatRates()),
        lambda: A.paired_ab(base + [unpriced], cand, outcomes, pricer=FakePricer()),
        lambda: A.paired_ab(["x"], cand, outcomes, pricer=FlatRates()),  # type: ignore[list-item]
        lambda: A.paired_ab(base, cand, [], pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, outcomes + [outcomes[0]], pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, [o for o in outcomes if o["task_id"] != "task-000"],
                            pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, [o for o in outcomes if not (
            o["task_id"] == "task-000" and o["arm"] == "candidate")], pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, [dict(o, success=False) for o in outcomes],
                            pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=0),
        lambda: A.paired_ab(base, cand, ["row"], pricer=FlatRates()),  # type: ignore[list-item]
        lambda: A.paired_ab(base, cand, [dict(outcomes[0], task_id="")], pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, [dict(outcomes[0], arm="c")], pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, [dict(outcomes[0], trial=-1)], pricer=FlatRates()),
        lambda: A.paired_ab(base, cand, [dict(outcomes[0], success="yes")], pricer=FlatRates()),
    ]
    for call in bad_calls:
        with pytest.raises(UsageError):
            call()
    # a task that one arm never ran (outcome rows only) costs nothing in that arm
    extra = [dict(o, task_id="task-empty") for o in outcomes if o["task_id"] == "task-000"]
    res = A.paired_ab(base, cand, outcomes + extra, pricer=FlatRates(), boot=200)
    assert res.n_tasks == 4
