"""Gate (merge gate 1): CP-SYNTH's synthetic Copilot enterprise through ``copilot.lanes`` and the
real generic detectors (DETECT-CACHE / DETECT-OTHER) and replayer (REPLAY) — addendum §18 plant
table: the vscode team's band premiums and ``cache.miss-by-cause model-switch``, the agents team's
``compaction-cost`` (also on the events-only session), ``static-overhead`` and CLI ``ci-uncapped``,
the excluded generic kinds absent, the core control team clean.

Skipped until ``tokenbill.synth.copilot_world`` exists (CP-SYNTH is built in parallel); the
world's record and truth attribute names are read defensively (the contract owner aligns names at
gate 1)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from tokenbill.core import registry
from tokenbill.core.findings import min_usd_nano, product_family
from tokenbill.core.lanes import group_lanes
from tokenbill.core.records import Lane
from tokenbill.core.transitions import lane_first_reads_of, static_prefix_floor
from tokenbill.core.types import AnalysisContext, Finding

from .helpers import ctx, only

pytestmark = pytest.mark.gate

LANE_CAPS = frozenset({"credits", "ext:copilot", "usage_sequence", "timing", "params", "events",
                       "appended", "lanes_exact"})
EXCLUDED = (("premium.modifiers", "fast-premium"), ("automation", "ci-run-cost"),
            ("context.static-prefix", "static-prefix"), ("model.routing", "default-model"),
            ("model.routing", "default-effort"))
EXCLUDED_DETECTORS = ("cache.ttl-advisor", "cache.gateway-disabled", "cache.cold-fanout",
                      "premium.sticky-escalation", "block.breakers")


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _world_lanes() -> tuple[Any, list[Lane]]:
    synth = pytest.importorskip("tokenbill.synth.copilot_world")
    world = synth.generate(seed=7)
    rec = _get(world, "records", default=world)
    # Always group through group_lanes: the world exposes one raw lane per source session
    # (a conversation delivered by several adapters), and the pipeline dedups by request_id in
    # the store before lanes reach a detector. Grouping here reproduces that deduped view, so a
    # conversation's compaction is counted once, not once per source (matches the truth's dedup).
    sessions = tuple(_get(rec, "sessions", default=()))
    requests = tuple(_get(rec, "requests", default=()))
    events = tuple(_get(rec, "events", "lane_events", default=()))
    lanes = group_lanes(requests, events, sessions) if requests or sessions else (
        _get(rec, "lanes", default=()))
    lanes = [ln for ln in lanes if isinstance(ln, Lane)]
    if not lanes:
        pytest.skip("the CP-SYNTH world carries no lanes under a known attribute name")
    return world, lanes


def _gate_ctx(lanes: Iterable[Lane], *, replayer: Any = None) -> AnalysisContext:
    from tokenbill.core.cache_rules import RulesTable

    floor = static_prefix_floor(lane_first_reads_of(lanes))
    # min_usd 0.10: compaction credits are small (one summary call each), as CP-DET-LANES'
    # own test_compaction.py validates; the plant spend is real but below the $1 default.
    return ctx(caps=LANE_CAPS, rules=RulesTable(), replayer=replayer, floor=floor, min_usd="0.10")


def _team(findings: Iterable[Finding], kind: str, team: str) -> list[Finding]:
    return only(list(findings), kind, team=team)


def _truth(world: Any, *names: str) -> Any:
    return _get(_get(world, "truth", default=None), *names)


def test_copilot_lanes_plants_recovered() -> None:
    world, lanes = _world_lanes()
    copilot = [ln for ln in lanes if product_family(ln) == "copilot"]
    assert copilot, "the world has Copilot lanes"
    c = _gate_ctx(copilot)
    found = registry.run_detectors(lanes, c, only=["copilot.lanes"], aggregates_only=False)
    assert _team(found, "long-context-band", "vscode"), "vscode band requests"
    assert _team(found, "compaction-cost", "agents"), "agents compactions"
    assert _team(found, "ci-uncapped", "agents"), "agents --ci lane"
    band_truth = _truth(world, "band_premium_nano", "long_context_band_nano")
    if isinstance(band_truth, int):
        got = sum(f.cost_observed.nano or 0 for f in _team(found, "long-context-band", "vscode"))
        assert got == band_truth
    compaction_truth = _truth(world, "compaction_nano", "compaction_cost_nano")
    if isinstance(compaction_truth, int):
        got = sum(f.cost_observed.nano or 0 for f in _team(found, "compaction-cost", "agents"))
        assert got == compaction_truth
    static_truth = _truth(world, "static_overhead_nano", "static_overhead_range")
    statics = [f for f in found if f.kind == "static-overhead"]
    if static_truth is not None and statics:
        lows = sum((f.recoverable or f.cost_observed).low_nano or 0 for f in statics)
        highs = sum((f.recoverable or f.cost_observed).high_nano or 0 for f in statics)
        value = static_truth if isinstance(static_truth, int) else static_truth[0]
        assert lows <= value <= highs or isinstance(static_truth, tuple) and \
            static_truth[0] <= highs and lows <= static_truth[1]


def test_generic_detectors_on_copilot_lanes_with_the_real_replayer() -> None:
    replay = pytest.importorskip("tokenbill.sim.usage_replay")
    pytest.importorskip("tokenbill.detect.cache_miss")
    pytest.importorskip("tokenbill.detect.context")
    world, lanes = _world_lanes()
    replayer_cls = _get(replay, "UsageReplayer", "Replayer", default=None)
    replayer = replayer_cls() if callable(replayer_cls) else None
    c = _gate_ctx([ln for ln in lanes if product_family(ln) == "copilot"], replayer=replayer)
    found = registry.run_detectors(lanes, c, aggregates_only=False)
    copilot = [f for f in found if dict(f.scope.dims).get("product") == "copilot"]
    switches = [f for f in copilot if f.detector_id == "cache.miss-by-cause"
                and f.kind == "model-switch"]
    assert switches, "cache.miss-by-cause model-switch recovered on Copilot lanes"
    for f in switches:
        assert f.fix is not None and f.fix.target == "github-copilot"
    for f in copilot:
        assert (f.detector_id, f.kind) not in EXCLUDED, f.kind
        assert f.detector_id not in EXCLUDED_DETECTORS, f.detector_id
        blob = repr(f.fix)
        assert "CLAUDE_CODE_" not in blob
    limit = min_usd_nano(c)
    for f in copilot:
        if dict(f.scope.dims).get("team") == "core":
            for fig in (f.recoverable, f.headroom):
                assert fig is None or fig.nano is None or fig.nano < limit, (f.detector_id,
                                                                             f.kind)
