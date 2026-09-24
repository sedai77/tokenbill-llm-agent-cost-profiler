"""Gate (merge gate 1): the VERIFY estimators on SYNTH-ORACLE's generators
(``synth.lanes_gen.rollout_panel`` and ``ab_campaign``, PLAN §3 SYNTH-ORACLE).

The truth of a generated panel is relative: ``true_effect`` is the fractional change of cost per
active developer-day, so each check compares the imputation estimate relative to the imputed
counterfactual level (and the ITS shift relative to the late pre-period level) with it.
"""

from __future__ import annotations

import pytest

from tokenbill.core.builders import FlatRates
from tokenbill.verify import estimators as E
from tokenbill.verify.ab import paired_ab
from tokenbill.verify.its import event_study_its
from tokenbill.verify.panel import org_series

lanes_gen = pytest.importorskip("tokenbill.synth.lanes_gen")

pytestmark = pytest.mark.gate

TRUE_EFFECT = 0.25


def _relative(rows, boot: int, seed: int) -> tuple[float, float, float]:
    res = E._did(rows, metric="cost", washout_days=0, boot=boot, seed=seed, scope="gate")
    lo, hi = res.ci()
    return res.att / res.base, lo / res.base, hi / res.base


def _check(seeds: range, boot: int) -> None:
    covered = 0
    for seed in seeds:
        rows = lanes_gen.rollout_panel(clusters=30, weeks=12, true_effect=TRUE_EFFECT, waves=4,
                                       holdback=0.2, seed=seed)
        assert rows and any(r.treated for r in rows) and any(not r.treated for r in rows)
        rel, lo, hi = _relative(rows, boot, seed)
        assert abs(abs(rel) - TRUE_EFFECT) <= 0.05 * TRUE_EFFECT, (seed, rel)
        covered += min(abs(lo), abs(hi)) <= TRUE_EFFECT <= max(abs(lo), abs(hi))
    assert covered >= 0.9 * len(seeds)


def test_stepped_wedge_panels_from_synth_10_seeds() -> None:
    _check(range(10), boot=400)


@pytest.mark.slow
def test_stepped_wedge_panels_from_synth_50_seeds() -> None:
    _check(range(50), boot=500)


def test_rollout_panel_is_deterministic() -> None:
    a = lanes_gen.rollout_panel(clusters=12, weeks=8, true_effect=TRUE_EFFECT, waves=3,
                                holdback=0.25, seed=4)
    b = lanes_gen.rollout_panel(clusters=12, weeks=8, true_effect=TRUE_EFFECT, waves=3,
                                holdback=0.25, seed=4)
    assert a == b
    assert E.imputation_did(a, boot=50, seed=1) == E.imputation_did(b, boot=50, seed=1)


def test_org_wide_series_from_synth_its() -> None:
    rows = lanes_gen.rollout_panel(clusters=1, weeks=20, true_effect=TRUE_EFFECT, waves=1,
                                   holdback=0.2, seed=2, org_wide=True)
    series = org_series(rows)
    change = min(r.date_utc for r in rows if r.treated)
    pre = [(d, c, n) for d, c, n in series if d < change and n > 0]
    placebo = pre[len(pre) // 2][0]
    delta, lo, hi, _placebo_ok = event_study_its(series, change_date=change,
                                                 placebo_date=placebo)
    late = pre[-14:]
    level = sum(c for _, c, _ in late) / sum(n for _, _, n in late)
    assert abs(abs(delta / level) - TRUE_EFFECT) <= 0.05 * TRUE_EFFECT
    assert lo <= delta <= hi


def test_rtk_like_ab_campaign_from_synth_is_costlier() -> None:
    base, cand, outcomes = lanes_gen.ab_campaign(tasks=20, trials=5, cost_effect=0.07,
                                                 token_effect=-0.38, turn_effect=0.14, seed=1)
    res = paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=10_000, seed=0)
    assert res.verdict == "costlier"
    assert res.scope_label.startswith("lab:")
