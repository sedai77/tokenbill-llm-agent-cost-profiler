"""Gate (merge gate 1): the VERIFY estimators on SYNTH-ORACLE's generators
(``synth.lanes_gen.rollout_panel`` / ``rollout_truth`` / ``ab_campaign``, PLAN §3 SYNTH-ORACLE).

``true_effect`` is the relative reduction of cost per active developer-day on treated
cluster-days; ``rollout_truth`` is the exact ATT (nano per active developer-day) of a panel.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokenbill.core.labels import Evidence
from tokenbill.core.testing import FakePricer
from tokenbill.verify import estimators as E
from tokenbill.verify.ab import paired_ab
from tokenbill.verify.its import event_study_its
from tokenbill.verify.panel import org_series

lanes_gen = pytest.importorskip("tokenbill.synth.lanes_gen")

pytestmark = pytest.mark.gate

KW = {"clusters": 30, "weeks": 12, "true_effect": "0.25", "waves": 4, "holdback": "0.2"}


def _check(seeds: range, boot: int) -> None:
    covered = 0
    for seed in seeds:
        rows = lanes_gen.rollout_panel(seed=seed, **KW)
        truth = lanes_gen.rollout_truth(seed=seed, **KW)
        att, lo, hi = E.imputation_did(rows, boot=boot, seed=seed)
        assert abs(att - truth) <= 0.05 * abs(truth), (seed, att, truth)
        covered += lo <= truth <= hi
    assert covered >= 0.9 * len(seeds)


def test_stepped_wedge_panels_from_synth_10_seeds() -> None:
    """Acceptance (gate): imputation within ±5% of the known effect, 95% CI coverage ≥ 90%."""
    _check(range(10), boot=400)


@pytest.mark.slow
def test_stepped_wedge_panels_from_synth_50_seeds() -> None:
    _check(range(50), boot=500)


def test_constant_price_estimate_ignores_a_price_cut() -> None:
    plain = lanes_gen.rollout_panel(seed=3, **KW)
    cut = lanes_gen.rollout_panel(seed=3, price_change="-0.2", **KW)
    assert E.imputation_did(plain, boot=50) == E.imputation_did(cut, boot=50)
    assert sum(r.cost_actual_nano for r in cut) < sum(r.cost_baseline_nano for r in cut)


def test_org_wide_series_from_synth_its() -> None:
    kw = dict(KW, clusters=1, weeks=20, waves=1)
    rows = lanes_gen.rollout_panel(seed=2, org_wide=True, **kw)
    truth = lanes_gen.rollout_truth(seed=2, org_wide=True, **kw)
    series = org_series(rows)
    change = min(r.date_utc for r in rows if r.treated)
    pre = [d for d, _, n in series if d < change and n > 0]
    delta, lo, hi, _placebo = event_study_its(series, change_date=change,
                                              placebo_date=pre[len(pre) // 2])
    assert abs(delta - truth) <= 0.05 * abs(truth), (delta, truth)
    assert lo <= delta <= hi


def test_rtk_like_ab_campaign_from_synth_is_costlier() -> None:
    base, cand, outcomes = lanes_gen.ab_campaign(tasks=20, trials=5, cost_effect="0.07",
                                                 token_effect="-0.38", turn_effect="0.14",
                                                 seed=1)
    res = paired_ab(base, cand, outcomes, pricer=FakePricer(), seed=0)
    assert res.verdict == "costlier"
    assert Decimal(res.token_delta_pct) == pytest.approx(Decimal("-38"), abs=Decimal("1"))
    assert Decimal(res.turn_delta_pct) == pytest.approx(Decimal("14"), abs=Decimal("1"))
    assert res.randomized_order and res.trials_per_arm == (5, 5)
    assert res.measurement.estimate.evidence is Evidence.VERIFIED
    assert res.scope_label.startswith("lab:")
