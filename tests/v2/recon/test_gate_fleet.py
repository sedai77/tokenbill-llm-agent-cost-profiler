"""Gate (SPEC §18, PLAN §1.5): the synthetic fleet's provider records and ledger, reconciled,
recover SYNTH-FLEET's closed-form reconciliation truth (``FleetTruth.recon``): the contract
multipliers per channel, the Priority Tier and seat-allowance residuals, the revision window, and a
re-run on the suggested contracts that reconciles."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core.ids import key_id
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.recon.reconcile import reconcile, suggest_contracts

pytestmark = [pytest.mark.gate, pytest.mark.slow]


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, list]:
    synth = pytest.importorskip("tokenbill.synth.fleet")
    world = synth.generate(seed=7, out_dir=Path(tmp_path_factory.mktemp("recon-fleet")))
    store = MemoryStore(org_key=synth.FLEET_ORG_KEY, name_key_id=key_id(synth.FLEET_NAME_KEY))
    store.ingest(world.ingest_result(), pricer=FakePricer())
    return world, list(store.iter_usage_records())


def test_fleet_reconciliation_recovers_the_truth(fleet: tuple[Any, list]) -> None:
    world, records = fleet
    truth = world.truth.recon
    pricer = FakePricer()
    report = reconcile(iter(records), world.aggregates, world.cost_lines, pricer,
                       today=world.today, suggest_contract=True)
    codes = dict(report.residuals)
    assert codes["priority_excluded_from_cost_report"] == truth.priority_list_nano
    assert codes["seat_allowance_unmetered"] == truth.seat_allowance_nano
    verdicts = {c.channel: c for c in report.channels}
    assert verdicts["anthropic_api"].verdict == "not_reconciled"      # list vs a 15% contract
    assert verdicts["bedrock"].verdict == "reconciled"                 # channel totals
    assert verdicts["bedrock"].mapping_verified is False
    overlays = {o.channels: o.multiplier for o in suggest_contracts(
        world.aggregates, world.cost_lines, pricer, today=world.today)}
    expected = {(ch,): Decimal(mult) for ch, mult in truth.contract_multipliers}
    assert overlays == expected
    assert report.rerun_verdict == "reconciled"
    provisional = {dict(r.key)["date"] for r in report.rows if r.status == "provisional"}
    assert provisional == set(truth.provisional_dates)
    assert abs(report.unexplained_nano) < 1_000_000                   # rounding only
    assert report.over_count_rows == 0
