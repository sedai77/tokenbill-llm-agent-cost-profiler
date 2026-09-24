"""``rollout_panel`` / ``rollout_truth`` / ``ab_campaign`` (SPEC §13; SYNTH-ORACLE acceptance):
a 25% stepped-wedge panel is deterministic with the requested waves and holdback; ``org_wide``
gives one series with a level shift; the A/B campaign has the RTK-like shape (tokens −38%,
turns +14%, cost +7%) in every task."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

import pytest

from tokenbill.core.errors import UsageError
from tokenbill.core.records import to_json
from tokenbill.synth.lanes_gen import ab_campaign, rollout_panel, rollout_truth

from .helpers import PRICER

KW = {"clusters": 20, "weeks": 10, "true_effect": "0.25", "waves": 4, "holdback": "0.2",
      "seed": 3}


def _per_dev_day(rows) -> Fraction:
    return Fraction(sum(r.cost_baseline_nano for r in rows), sum(r.active_dev_days for r in rows))


def test_panel_is_deterministic_and_seeded() -> None:
    a, b = rollout_panel(**KW), rollout_panel(**KW)
    assert [to_json(r) for r in a] == [to_json(r) for r in b]
    assert [to_json(r) for r in a] != [to_json(r) for r in rollout_panel(**{**KW, "seed": 4})]
    assert len(a) == 20 * 70
    assert [(r.cluster_id, r.date_utc) for r in a] == sorted((r.cluster_id, r.date_utc)
                                                             for r in a)
    assert a[0].date_utc == "2026-06-01" and all(r.active_dev_days >= 1 for r in a)
    assert all(type(r.cost_baseline_nano) is int and r.cost_baseline_nano > 0 for r in a)


def test_panel_has_the_requested_waves_and_holdback() -> None:
    rows = rollout_panel(**KW)
    arms = {(r.cluster_id, r.arm, r.wave) for r in rows}
    held = {c for c, arm, _w in arms if arm == "holdback"}
    assert len(held) == 4 and len({c for c, *_ in arms}) == 20
    assert all(not r.treated for r in rows if r.cluster_id in held)
    waves = defaultdict(set)
    first_treated: dict[str, str] = {}
    for r in rows:
        if r.arm == "treatment":
            waves[r.wave].add(r.cluster_id)
        if r.treated:
            first_treated.setdefault(r.cluster_id, r.date_utc)
    assert set(waves) == {"1", "2", "3", "4"} and all(len(v) == 4 for v in waves.values())
    # wave k starts at week k · (10 // 5) = 2k
    starts = {w: {first_treated[c] for c in members} for w, members in waves.items()}
    assert starts == {"1": {"2026-06-15"}, "2": {"2026-06-29"}, "3": {"2026-07-13"},
                      "4": {"2026-07-27"}}
    by_cluster = defaultdict(list)
    for r in rows:
        by_cluster[r.cluster_id].append(r.treated)
    assert all(flags == sorted(flags) for flags in by_cluster.values())   # never untreated again


def test_panel_effect_is_about_25_percent() -> None:
    rows = rollout_panel(**KW)
    truth = rollout_truth(**KW)
    level = _per_dev_day([r for r in rows if not r.treated])
    assert -Fraction(27, 100) < truth / level < -Fraction(23, 100)
    # a simple difference-in-differences against the holdback lands near the truth
    held = [r for r in rows if r.arm == "holdback"]
    treated_clusters = {r.cluster_id for r in rows if r.treated}
    post_dates = {r.date_utc for r in rows if r.treated}
    t_post = [r for r in rows if r.treated]
    t_pre = [r for r in rows if r.cluster_id in treated_clusters and not r.treated]
    h_post = [r for r in held if r.date_utc in post_dates]
    h_pre = [r for r in held if r.date_utc not in post_dates]
    ratio = (_per_dev_day(t_post) / _per_dev_day(t_pre)) / (_per_dev_day(h_post)
                                                           / _per_dev_day(h_pre))
    assert Fraction(70, 100) < ratio < Fraction(80, 100)
    assert truth == rollout_truth(**KW)


def test_org_wide_series_has_one_cluster_and_a_level_shift() -> None:
    rows = rollout_panel(**{**KW, "true_effect": 0.2, "org_wide": True})
    assert {r.cluster_id for r in rows} == {"org"} and len(rows) == 70
    assert {(r.arm, r.wave) for r in rows} == {(None, None)}
    treated = [r.date_utc for r in rows if r.treated]
    assert treated[0] == "2026-07-06" and len(treated) == 35 and treated == sorted(treated)
    pre, post = [r for r in rows if not r.treated], [r for r in rows if r.treated]
    assert Fraction(76, 100) < _per_dev_day(post) / _per_dev_day(pre) < Fraction(84, 100)
    truth = rollout_truth(**{**KW, "true_effect": Decimal("0.2"), "org_wide": True})
    assert -Fraction(22, 100) < truth / _per_dev_day(pre) < -Fraction(18, 100)
    assert rows[0].active_dev_days > 50


def test_price_change_moves_only_the_actual_cost_after_the_first_treatment() -> None:
    rows = rollout_panel(**{**KW, "price_change": "-0.2"})
    base = rollout_panel(**KW)
    assert [r.cost_baseline_nano for r in rows] == [r.cost_baseline_nano for r in base]
    first = min(r.date_utc for r in rows if r.treated)
    for r in rows:
        if r.date_utc < first:
            assert r.cost_actual_nano == r.cost_baseline_nano
        else:
            assert abs(r.cost_actual_nano * 5 - r.cost_baseline_nano * 4) <= 5
    assert all(r.cost_actual_nano == r.cost_baseline_nano for r in base)


def test_panel_options_and_errors() -> None:
    named = rollout_panel(clusters=["a", "b", "c", "d"], weeks=3, true_effect=0, waves=2,
                          holdback=1, seed=0)
    assert {r.cluster_id for r in named} == {"a", "b", "c", "d"}
    assert len({r.cluster_id for r in named if r.arm == "holdback"}) == 1
    assert all(r.outcome_prs is not None and r.outcome_prs >= 0 for r in named)
    no_hold = rollout_panel(clusters=6, weeks=4, true_effect="0.1", waves=3, holdback=0, seed=1)
    assert {r.arm for r in no_hold} == {"treatment"}
    assert rollout_truth(clusters=6, weeks=4, true_effect="0", waves=3, holdback=0, seed=1) == 0
    for bad in ({"weeks": 1}, {"waves": 0}, {"waves": 10}, {"holdback": 20},
                {"clusters": ["a", "a"]}, {"true_effect": "x"}, {"true_effect": True},
                {"clusters": 0}):
        with pytest.raises(UsageError):
            rollout_panel(**{**KW, **bad})


# ---------------------------------------------------------------------------------------------
# A/B campaign
# ---------------------------------------------------------------------------------------------


def _tokens(reqs) -> int:
    return sum(i.usage.total_input + i.usage.output for r in reqs for i in r.billable_inferences)


def _cost(reqs) -> int:
    return sum(PRICER.price_inference(i, ts_ms=r.ts_start_ms).figure.nano  # type: ignore[misc]
               for r in reqs for i in r.billable_inferences)


def _task(req) -> str:
    return dict(req.attribution.extra)["task_id"]


def test_ab_campaign_reproduces_the_rtk_like_shape() -> None:
    base, cand, outcomes = ab_campaign(tasks=20, trials=5, cost_effect=0.07,
                                       token_effect=-0.38, turn_effect=0.14, seed=1)
    assert abs(Fraction(_tokens(cand), _tokens(base)) - Fraction(62, 100)) < Fraction(1, 1000)
    assert abs(Fraction(len(cand), len(base)) - Fraction(114, 100)) < Fraction(1, 500)
    assert abs(Fraction(_cost(cand), _cost(base)) - Fraction(107, 100)) < Fraction(1, 1000)
    per_task = defaultdict(lambda: [[], []])
    for arm, reqs in ((0, base), (1, cand)):
        for r in reqs:
            per_task[_task(r)][arm].append(r)
    for b, c in per_task.values():   # every task is costlier and uses fewer tokens
        assert abs(Fraction(_cost(c), _cost(b)) - Fraction(107, 100)) < Fraction(1, 200)
        assert abs(Fraction(_tokens(c), _tokens(b)) - Fraction(62, 100)) < Fraction(1, 200)
    assert len(outcomes) == 20 * 5 * 2


def test_ab_campaign_outcomes_and_tags() -> None:
    base, cand, outcomes = ab_campaign(tasks=6, trials=5, cost_effect="0.07",
                                       token_effect="-0.38", turn_effect="0.14", seed=4)
    pairs = defaultdict(list)
    for o in outcomes:
        assert set(o) == {"task_id", "arm", "trial", "success", "order"}
        pairs[(o["task_id"], o["trial"])].append((o["order"], o["arm"]))
    assert all(sorted(v) in ([(1, "baseline"), (2, "candidate")],
                             [(1, "candidate"), (2, "baseline")]) for v in pairs.values())
    firsts = {min(v)[1] for v in pairs.values()}
    assert firsts == {"baseline", "candidate"}        # the arm order is randomized per pair
    assert {r.attribution.arm for r in base} == {"baseline"}
    assert {r.attribution.arm for r in cand} == {"candidate"}
    assert {dict(r.attribution.extra)["run_attempt"] for r in base} == {str(t) for t in range(5)}
    lanes = {r.lane_key for r in base} | {r.lane_key for r in cand}
    assert len(lanes) == 6 * 5 * 2
    again = ab_campaign(tasks=6, trials=5, cost_effect="0.07", token_effect="-0.38",
                        turn_effect="0.14", seed=4)
    assert [to_json(r) for r in again[1]] == [to_json(r) for r in cand]
    assert again[2] == outcomes


def test_ab_campaign_errors() -> None:
    with pytest.raises(UsageError):
        ab_campaign(tasks=0, trials=1, cost_effect=0, token_effect=0, turn_effect=0, seed=0)
    with pytest.raises(UsageError):
        ab_campaign(tasks=1, trials=1, cost_effect=0, token_effect=-1, turn_effect=0, seed=0)
    with pytest.raises(UsageError):   # far cheaper with more tokens: no read share gets there
        ab_campaign(tasks=2, trials=1, cost_effect=-0.9, token_effect=0.5, turn_effect=0,
                    seed=0)
    with pytest.raises(UsageError):
        ab_campaign(tasks=1, trials=1, cost_effect=0, token_effect=0, turn_effect=0, seed=0,
                    model="gpt-5.6-sol")
