"""Workload classifier (SPEC §14.6): each signal, tie-breaks, ≥ 95% accuracy on a labeled set.

The labeled set is synthetic and seeded: 12 lanes per class, drawn from the shapes each class has in
the sources (CI action runs, SDK services, batch jobs, cron-like schedules, tagged eval runs,
interactive Claude Code sessions with human prompts, subagent lanes), with realistic noise (jittered
gaps, a few human prompts on interactive lanes, mixed entrypoints)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokenbill.common import rng
from tokenbill.core.builders import make_lane, make_request
from tokenbill.core.errors import ContractViolation
from tokenbill.core.records import Lane, LaneEvent, LaneEventKind, LaneKind, WorkloadClass
from tokenbill.finops.allocation import parse_rules
from tokenbill.finops.workload import AUTOMATION_RANK, classify, signals

W = WorkloadClass
MIN = 60_000


def lane(n: int, *, gaps_ms: list[int] | None = None, entry: str | None = None,
         tag: W = W.UNKNOWN, tier: str = "standard", prompts: int = 0,
         kind: LaneKind = LaneKind.MAIN, key: str = "L", **attr: object) -> Lane:
    ts, reqs = 0, []
    for i in range(n):
        if i:
            ts += (gaps_ms[i - 1] if gaps_ms else 20_000)
        reqs.append(make_request(key, i, ts, {"uncached_input": 100, "output": 10},
                                 attribution={"entrypoint": entry, "workload_class": tag, **attr},
                                 service_tier=tier))
    events = [LaneEvent(key, 1000 * j, LaneEventKind.HUMAN_PROMPT) for j in range(prompts)]
    return make_lane(reqs, kind=kind, events=events)


def test_each_signal() -> None:
    assert classify(lane(3, entry="claude-code-github-action")) == (W.CI, "0.95")
    assert classify(lane(3, entry="sdk-py")) == (W.SERVICE, "0.6")
    assert classify(lane(3, tag=W.EVAL)) == (W.EVAL, "0.99")
    assert classify(lane(3, tier="batch")) == (W.BATCH, "0.99")
    assert classify(lane(12)) == (W.SERVICE, "0.7")
    assert classify(lane(12, kind=LaneKind.SUBAGENT)) == (W.UNKNOWN, "0")
    assert classify(lane(6, gaps_ms=[3_600_000] * 5)) == (W.SCHEDULED, "0.8")
    assert classify(lane(3, prompts=2)) == (W.INTERACTIVE, "0.5")
    assert classify(lane(30, entry="cli")) == (W.INTERACTIVE, "0.5")
    assert classify(lane(2)) == (W.UNKNOWN, "0")
    assert classify(make_lane([], lane_key="empty")) == (W.UNKNOWN, "0")


def test_cadence_guards() -> None:
    # regular but fast (tool-call loop) → not scheduled; irregular hourly → not scheduled
    assert classify(lane(6, gaps_ms=[5_000] * 5))[0] is not W.SCHEDULED
    assert classify(lane(6, gaps_ms=[600_000, 7_200_000, 900_000, 5_400_000, 400_000]))[0] \
        is not W.SCHEDULED
    # a human prompt disables the cadence rule
    assert classify(lane(6, gaps_ms=[3_600_000] * 5, prompts=1))[0] is W.INTERACTIVE
    # three requests are too few
    assert classify(lane(3, gaps_ms=[3_600_000] * 2))[0] is W.UNKNOWN


def test_ties_go_to_the_more_automated_class() -> None:
    both = lane(3, tag=W.INTERACTIVE, tier="batch")      # 0.99 vs 0.99
    assert classify(both) == (W.BATCH, "0.99")
    assert AUTOMATION_RANK[W.BATCH] > AUTOMATION_RANK[W.SCHEDULED] > AUTOMATION_RANK[W.CI] > (
        AUTOMATION_RANK[W.SERVICE]) > AUTOMATION_RANK[W.INTERACTIVE] > AUTOMATION_RANK[W.UNKNOWN]


def test_rule_set_signal() -> None:
    rules = parse_rules({"rules": [{"match": {"agent_product": {"eq": "evals"}},
                                    "set": {"workload_class": "eval"}}]})
    lv = lane(3, agent_product="evals")
    assert classify(lv) == (W.UNKNOWN, "0")
    assert classify(lv, rules=rules) == (W.EVAL, "0.9")
    tagged = lane(3, agent_product="evals", tag=W.EVAL)
    assert [s for s in signals(tagged, rules=rules) if s[2] == "allocation rule"] == []


def test_rejects_non_lanes() -> None:
    with pytest.raises(ContractViolation):
        classify("lane")  # type: ignore[arg-type]


def labeled_set() -> list[tuple[Lane, W]]:
    r = rng(7, "tests.finops.workload")
    out: list[tuple[Lane, W]] = []
    for i in range(12):
        out.append((lane(r.randint(2, 30), entry="claude-code-github-action",
                         key=f"ci{i}", prompts=0), W.CI))
        out.append((lane(r.randint(2, 40), entry=r.choice(["sdk-py", "sdk-ts"]),
                         key=f"svc{i}"), W.SERVICE))
        out.append((lane(r.randint(10, 60), kind=LaneKind.API_RUN, key=f"api{i}"), W.SERVICE))
        out.append((lane(r.randint(1, 20), tier="batch", entry=r.choice([None, "sdk-py"]),
                         key=f"batch{i}"), W.BATCH))
        period = r.choice([900_000, 3_600_000, 86_400_000])
        gaps = [period + r.randint(-period // 20, period // 20) for _ in range(r.randint(4, 12))]
        out.append((lane(len(gaps) + 1, gaps_ms=gaps, key=f"cron{i}"), W.SCHEDULED))
        out.append((lane(r.randint(2, 50), tag=W.EVAL, entry=r.choice([None, "sdk-py"]),
                         key=f"eval{i}"), W.EVAL))
        gaps = [r.randint(2_000, 400_000) for _ in range(r.randint(3, 60))]
        out.append((lane(len(gaps) + 1, gaps_ms=gaps, entry="cli", prompts=r.randint(1, 8),
                         key=f"ia{i}"), W.INTERACTIVE))
        out.append((lane(r.randint(3, 80), entry="cli", key=f"ia-noprompt{i}"), W.INTERACTIVE))
    return out


def test_accuracy_on_the_labeled_set_is_at_least_95_percent() -> None:
    data = labeled_set()
    correct = sum(1 for lv, want in data if classify(lv)[0] is want)
    assert len(data) == 96
    assert Decimal(correct) / Decimal(len(data)) >= Decimal("0.95"), [
        (lv.lane_key, want.value, classify(lv)) for lv, want in data if classify(lv)[0] is not want]


def test_deterministic() -> None:
    a = [classify(lv) for lv, _w in labeled_set()]
    b = [classify(lv) for lv, _w in labeled_set()]
    assert a == b
