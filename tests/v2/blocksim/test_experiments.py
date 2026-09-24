"""Codebase-experiment regressions (SPEC §10.4), read from the checked-in fixtures in
``tests/v2/fixtures/blocksim/`` (see ``exps.py`` for how each was re-encoded)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.v2.blocksim.exps import load
from tests.v2.blocksim.helpers import context, kinds
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.policy import parse_policy
from tokenbill.core.testing import FakePricer
from tokenbill.detect.block import BlockBreakers
from tokenbill.sim.block_replay import CANONICAL_PLACEMENTS, BlockReplayer, read_agreement

P = FakePricer()


def detect(lanes, min_usd: str = "0"):
    return BlockBreakers().detect(lanes, context(min_usd=min_usd))


def replay(lanes, spec: str):
    return BlockReplayer().replay(lanes, parse_policy(spec), mode="documented", pricer=P,
                                  rules=RulesTable(), calibration=None, keep_outcomes=True)


@pytest.fixture(scope="module")
def exps():
    return {name: load(name) for name in ("exp1", "exp2b_a3", "exp2b_a2", "exp9_1", "exp6_2")}


def test_exp1_moving_marker_is_no_history_rewrite_and_reads_agree(exps) -> None:
    lanes = exps["exp1"]
    markers = [r.params.breakpoints[0].block_index for r in lanes[0].requests]
    assert len(set(markers)) == len(markers)                  # the marker moves every turn
    assert detect(lanes) == []
    outs = BlockReplayer().predict(lanes, pricer=P)
    agreement = read_agreement(outs, lanes)
    assert agreement is not None and Decimal("0.95") <= agreement <= Decimal("1.05")


def test_exp2b_a3_shared_preamble_placement_saves_about_56_percent(exps) -> None:
    lanes = exps["exp2b_a3"]
    findings = detect(lanes, min_usd="0.01")
    f = next(f for f in findings if f.kind == "breakpoint-placement")
    assert "static_plus_end" in f.title
    share = Decimal(f.recoverable.nano) / Decimal(f.cost_observed.nano)
    assert Decimal("0.54") <= share <= Decimal("0.58"), share
    assert set(kinds(findings)) <= {"breakpoint-placement", "write-never-read"}
    assert replay(lanes, "breakpoints=static_plus_end").saving.nano == f.recoverable.nano


def test_exp2b_a2_concurrent_identical_requests_have_no_phantom_saving(exps) -> None:
    lanes = exps["exp2b_a2"]
    outs = BlockReplayer().predict(lanes, pricer=P)
    assert len(outs) == 5
    assert all(o.usage.cache_read == 0 and o.usage.cache_write_5m > 0 for o in outs)
    for placement in CANONICAL_PLACEMENTS:
        assert replay(lanes, f"breakpoints={placement}").saving.nano == 0
    findings = detect(lanes, min_usd="0.01")
    assert kinds(findings) == ["fanout"]
    f = findings[0]
    assert f.recoverable.upper_bound and f.recoverable.nano > 0 and f.n_events == 4


def test_exp9_1_alternating_key_order_is_serialization_churn(exps) -> None:
    lanes = exps["exp9_1"]
    findings = detect(lanes, min_usd="0.01")
    assert kinds(findings) == ["serialization-churn"]
    f = findings[0]
    assert f.n_events == len(lanes[0].requests) - 1
    assert f.recoverable.nano == replay(lanes, "repair=block:sort_keys").saving.nano > 0


def test_exp6_2_twenty_five_blocks_per_turn_is_lookback_overflow(exps) -> None:
    lanes = exps["exp6_2"]
    findings = detect(lanes, min_usd="0.01")
    assert kinds(findings) == ["lookback-overflow"]
    f = findings[0]
    assert "every ~15" in f.fix.text
    assert f.recoverable.nano == replay(lanes, "breakpoints=every_15").saving.nano > 0
    assert f.n_events == len(lanes[0].requests) - 1
    repaired = replay(lanes, "breakpoints=every_15").outcomes
    assert all(o.usage.cache_read > 0 for o in repaired[1:])
