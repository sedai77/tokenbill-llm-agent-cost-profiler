"""Gate (merge gate 1): ``copilot.org-scan`` on CP-SYNTH's synthetic enterprise — the payments,
mobile, data and ci plants are recovered, the jetbrains team's Auto reach equals its truth and the
core control team stays clean (addendum §18). Skipped until ``tokenbill.synth.copilot_world``
exists; the truth attribute names are read defensively (CP-SYNTH builds them in parallel)."""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Any

import pytest

from tokenbill.core import pool
from tokenbill.core.findings import min_usd_nano
from tokenbill.core.testing import FakePricer, _ts
from tokenbill.core.types import AnalysisContext, Finding
from tokenbill.detect.copilot_org import CopilotOrgScan

from .worlds import ALL_CAPS, attrs, only

pytestmark = pytest.mark.gate


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _world_ctx() -> tuple[Any, AnalysisContext]:
    synth = pytest.importorskip("tokenbill.synth.copilot_world")
    world = synth.generate(seed=7)
    rec = _get(world, "records", default=world)
    lines = tuple(_get(rec, "cost_lines", default=()))
    aggs = tuple(_get(rec, "aggregates", default=()))
    licenses = tuple(_get(rec, "licenses", default=()))
    activity = tuple(_get(rec, "activity", default=()))
    config = tuple(_get(rec, "config", default=()))
    outcomes = tuple(_get(rec, "outcomes", default=()))
    today = _get(world, "today", default="2026-09-23")
    cells, _ = pool.build_cells(aggs, lines, grain="day")
    pools = pool.pool_months(cells, lines, licenses, config, today=today)
    ctx = AnalysisContext(pricer=FakePricer(), rules=None, replayer=None, calibration=None,
                          window=(0, _ts(today)), capabilities=ALL_CAPS, now_ms=_ts(today),
                          aggregates=aggs, cost_lines=lines, licenses=licenses,
                          activity=activity, config=config, outcomes=outcomes,
                          pools=tuple(pools), reconciled_channels=frozenset({"github_copilot"}))
    return world, ctx


def _team(findings: list[Finding], kind: str, team: str) -> list[Finding]:
    return only(findings, kind, team=team)


def test_plants_recovered_and_control_clean() -> None:
    world, ctx = _world_ctx()
    findings = CopilotOrgScan().detect([], ctx)
    assert _team(findings, "premium-model-share", "payments")
    assert _team(findings, "auto-adoption", "payments")
    assert _team(findings, "fast-mode", "mobile")
    migrated = {dict(f.scope.dims).get("model") for f in only(findings, "forced-migration")}
    assert migrated & {"gpt-5.4", "gpt-5.5"}
    for kind in ("direct-org-usage", "larger-runner", "agentic-workflow-cost"):
        assert only(findings, kind), kind
    limit = min_usd_nano(ctx)
    for f in findings:
        if dict(f.scope.dims).get("team") == "core":
            for fig in (f.recoverable, f.headroom):
                assert fig is None or fig.nano is None or fig.nano < limit, f.kind
    truth = _get(world, "truth")
    fast = _get(truth, "fast_premium_nano", "fast_mode_premium_nano")
    if isinstance(fast, int):
        assert sum(f.cost_observed.nano or 0 for f in _team(findings, "fast-mode", "mobile")) \
            == fast
    direct = _get(truth, "direct_org_net_nano", "direct_net_nano")
    if isinstance(direct, int):
        assert sum(f.cost_observed.nano or 0 for f in only(findings, "direct-org-usage")) \
            == direct


def test_jetbrains_team_reach_equals_its_truth() -> None:
    world, ctx = _world_ctx()
    findings = CopilotOrgScan().detect([], ctx)
    auto = _team(findings, "auto-adoption", "jetbrains")
    assert auto, "the jetbrains team routes directly: an auto-adoption finding is expected"
    reach = Fraction(attrs(auto[0], "reach")["reach"])
    truth = _get(_get(world, "truth"), "auto_reach", "reach_by_team", default={})
    expected = _get(truth, "jetbrains")
    if expected is not None:
        assert reach == Fraction(expected)
    else:
        assert reach <= Fraction(1, 5)                 # ≈ 90% ide:intellij interactions
    mix = _team(findings, "editor-mix", "jetbrains")
    assert mix and "server-side model policy" in mix[0].fix.text
