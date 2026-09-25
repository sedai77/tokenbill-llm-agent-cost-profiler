"""Gate (merge gate 1): the CP-SYNTH world's seat and budget plants (addendum §18 table) recovered
by ``copilot.seats-budgets`` — platform 6 removable idle seats, 4 plan-mix seats and 5 $0 user
budgets; infra 5 team-assigned idle seats (projection None); ops 5 idle seats in the ``assign_all``
org B (``seat-auto-assign``) — and, in the ``plan_unknown`` variant, both scenario findings.

Two paths, each skipped until its sibling packages are merged: (1) the world's canonical records
enriched with ``core.pool`` exactly as CP-STORE's enricher does; (2) CP-SYNTH-W's files through the
real adapters (``core.registry.sniff_adapter``), the ledger and record store fakes of
``core.testing`` and the Copilot context enricher (``core.extensions.enrich``). Dollar truths are
compared when ``CopilotWorld.truth`` carries them (CP-SYNTH's closed forms); the counts are the
§18 plant table's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.core import pool as cpool
from tokenbill.core.types import AnalysisContext, Finding

from .helpers import ctx, detect, dims, evidence, of_kind

pytestmark = pytest.mark.gate


def _records(world: object, name: str) -> tuple:
    records = getattr(world, "records", world)
    value = records.get(name, ()) if isinstance(records, dict) else getattr(records, name, ())
    return tuple(value or ())


def _truth(world: object, *names: str) -> object | None:
    truth = getattr(world, "truth", None)
    for name in names:
        value = truth.get(name) if isinstance(truth, dict) else getattr(truth, name, None)
        if value is not None:
            return value
    return None


def _canonical_ctx(world: object) -> AnalysisContext:
    today = str(getattr(world, "today", "2026-09-23"))
    cost, aggs = _records(world, "cost_lines"), _records(world, "aggregates")
    lics, conf = _records(world, "licenses"), _records(world, "config")
    cells, _ = cpool.build_cells(aggs, cost, capped=cpool.capped_cost_centers(conf))
    pms = cpool.pool_months(cells, cost, lics, conf, today=today)
    plans = [pe for m in sorted({pm.month for pm in pms})
             for pe in cpool.detect_plans(cost, lics, conf, month=m)]
    return ctx(pools=pms, plans=plans, licenses=lics, config=conf, cost_lines=cost,
               activity=_records(world, "activity"), today=today)


def _team(findings: list[Finding], kind: str, team: str) -> list[Finding]:
    return [f for f in of_kind(findings, kind) if dims(f).get("team") == team]


def _assert_plants(findings: list[Finding], world: object) -> None:
    [platform] = _team(findings, "idle-seat", "platform")
    assert evidence(platform, "assignment:removable")["n"] == 6
    truth = _truth(world, "idle_seat_saving_nano", "seat_reclaim_nano")
    if isinstance(truth, int):
        assert platform.recoverable is not None and platform.recoverable.nano == truth
    [infra] = _team(findings, "idle-seat", "infra")
    assert evidence(infra, "assignment:team")["n"] == 5
    assert evidence(infra, "assignment:removable")["n"] == 0 and infra.recoverable is None
    [ops] = [f for f in of_kind(findings, "seat-auto-assign") if dims(f).get("org")]
    assert ops.n_users == 5
    truth = _truth(world, "seat_auto_assign_nano", "seat_policy_nano")
    if isinstance(truth, int):
        assert ops.recoverable is not None and ops.recoverable.nano == truth
    [mix] = _team(findings, "plan-mix", "platform")
    assert mix.n_users == 4
    [zero] = _team(findings, "budget-zero-user-budget", "platform")
    assert zero.n_events == 5
    assert of_kind(findings, "budget-stop-usage-off")


def test_gate_synth_world_canonical_records() -> None:
    synth = pytest.importorskip("tokenbill.synth.copilot_world")
    world = synth.generate(seed=7)
    _assert_plants(detect(_canonical_ctx(world)), world)


def test_gate_synth_plan_unknown_variant_both_scenarios() -> None:
    synth = pytest.importorskip("tokenbill.synth.copilot_world")
    world = synth.generate(seed=7, variants=("plan_unknown",))
    found = detect(_canonical_ctx(world))
    [status] = of_kind(found, "plan-status")
    assert evidence(status, "plan")["plan"] == "unknown"
    for kind in ("pool-regime", "idle-seat"):
        scenarios = {dims(f).get("plan_scenario") for f in of_kind(found, kind)}
        assert scenarios == {"business", "enterprise"}, kind
    assert of_kind(found, "plan-mix") == []
    truth = _truth(world, "pool_months")
    if truth:
        expected = {(pm.entity_id, pm.plan_scenario): pm.overage_observed_nano for pm in truth
                    if getattr(pm, "month", None) == max(p.month for p in truth)}
        got = {(dims(f)["entity"], dims(f).get("plan_scenario")): f.cost_observed.nano
               for f in of_kind(found, "pool-regime")}
        assert got == expected


def test_gate_synth_world_through_adapters_and_enricher(tmp_path: Path) -> None:
    synth = pytest.importorskip("tokenbill.synth.copilot_world")
    writers = pytest.importorskip("tokenbill.synth.copilot_writers")
    pytest.importorskip("tokenbill.copilot.enrich")
    from tokenbill.core import extensions, registry
    from tokenbill.core import testing as kit
    from tokenbill.core.types import IngestOptions

    world = synth.generate(seed=7)
    paths = writers.write_world(world, tmp_path)
    store = kit.MemoryStore(pricer=kit.FakePricer(), adopt_key_ids=True)
    records = [kit.MemoryRecordStore(store)]
    for path in sorted(paths.values() if isinstance(paths, dict) else paths):
        adapter = registry.sniff_adapter(Path(path))
        if adapter is None:
            continue
        result = adapter.read(Path(path), IngestOptions())
        store.ingest(result)
        extensions.persist(records, result)
    base = ctx()
    enriched = extensions.enrich(store, records, base, today=str(world.today),
                                 reconciled_channels=frozenset())
    findings = registry.run_detectors([], enriched, only=["copilot.seats-budgets"],
                                      aggregates_only=True)
    _assert_plants(findings, world)
