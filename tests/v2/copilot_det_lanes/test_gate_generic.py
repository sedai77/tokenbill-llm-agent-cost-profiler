"""Gate: the generic SPEC detectors on Copilot lanes (addendum §10.4) with the real core
(``core.registry.run_detectors``, ``core.cache_rules``, ``core.findings``, ``core.catalog``) and the
merged DETECT-CACHE / DETECT-OTHER / CP-DET-USAGE detectors (``importorskip``-guarded): Copilot
fixes and no ``CLAUDE_CODE_*`` key on kept kinds, ``ttl-expiry`` only with a known τ, the
``FAMILY_EXCLUSIONS`` kinds absent while their Copilot replacements appear, and
``cache.ttl-advisor`` never running on Copilot lanes."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from tokenbill.core import catalog, registry
from tokenbill.core.builders import make_ai_usage_row, make_pool_month, make_principal
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.findings import COPILOT_TITLE_PREFIX
from tokenbill.core.records import LaneKind, to_json
from tokenbill.core.testing import FakeReplayer
from tokenbill.core.types import AnalysisContext, Finding

from .helpers import (
    CLI,
    CREDIT,
    OPUS48,
    SONNET5,
    T0,
    VSCODE,
    attr,
    compaction_event,
    ctx,
    lane,
    only,
    req,
    run,
)

pytestmark = pytest.mark.gate

GENERIC_CAPS = frozenset({"credits", "ext:copilot", "usage_sequence", "timing", "params",
                          "events", "appended", "lanes_exact"})
RULES = RulesTable()


def gctx(**kw: object) -> AnalysisContext:
    kw.setdefault("caps", GENERIC_CAPS)
    kw.setdefault("min_usd", "0.10")
    return ctx(rules=RULES, replayer=FakeReplayer(), **kw)  # type: ignore[arg-type]


def _no_claude_code_keys(findings: Sequence[Finding]) -> None:
    for f in findings:
        blob = json.dumps(to_json(f.fix)) if f.fix is not None else ""
        assert "CLAUDE_CODE_" not in blob and "claude-code" not in blob, f.kind


def switch_lane(key: str = "vs-sw", **kw: object) -> object:
    """A VS Code conversation that switches from Sonnet 5 to Opus 4.8 mid-session: the Opus
    request rewrites the 100k prefix (a model-switch miss)."""
    return lane(key, [
        req(key, 0, 0, w=100_000, u=2_000, o=500, model=SONNET5, **kw),
        req(key, 1, 30, w=102_500, u=2_000, o=500, model=OPUS48, **kw),
        req(key, 2, 60, r=102_500, w=2_000, u=2_000, o=500, model=OPUS48, **kw),
    ])


def test_miss_by_cause_model_switch_carries_the_copilot_fix() -> None:
    pytest.importorskip("tokenbill.detect.cache_miss")
    found = registry.run_detectors([switch_lane()], gctx(),
                                   only=["cache.miss-by-cause", "cache.switch-churn"],
                                   aggregates_only=False)
    [miss] = only(found, "model-switch")
    dims = dict(miss.scope.dims)
    assert dims["billing_class"] == "pool" and dims["product"] == "copilot"
    assert miss.title.startswith(COPILOT_TITLE_PREFIX)
    assert "list-equivalent AI-credit value" in miss.summary
    fix = miss.fix
    assert fix is not None and fix.target == "github-copilot" and fix.doc_url
    assert fix.text.startswith("Auto switches models only at cache boundaries")
    assert fix.config_patch is None
    assert fix == catalog.fix_for("cache.miss-by-cause", "model-switch", "copilot")
    _no_claude_code_keys(found)


def test_ttl_expiry_only_with_a_known_tau() -> None:
    pytest.importorskip("tokenbill.detect.cache_miss")

    def idle(key: str, hint: str | None) -> object:
        return lane(key, [req(key, 0, 0, w=100_000, u=2_000, o=500, model=OPUS48,
                              write_ttl_hint=hint),
                          req(key, 1, 7_200, w=100_000, u=2_000, o=500, model=OPUS48,
                              write_ttl_hint=hint)])

    unknown = registry.run_detectors([idle("vs-t0", None)], gctx(),
                                     only=["cache.miss-by-cause"], aggregates_only=False)
    assert only(unknown, "ttl-expiry") == []
    assert unknown, "the miss is still reported under another cause"
    known = registry.run_detectors([idle("vs-t1", "5m")], gctx(),
                                   only=["cache.miss-by-cause"], aggregates_only=False)
    [ttl] = only(known, "ttl-expiry")
    assert ttl.fix is not None and ttl.fix.target == "github-copilot"
    _no_claude_code_keys(known)


def test_fast_premium_excluded_while_org_scan_fast_mode_replaces_it() -> None:
    premium = pytest.importorskip("tokenbill.detect.premium")
    pytest.importorskip("tokenbill.detect.copilot_org")
    key = "vs-fast"
    fast = lane(key, [req(key, i, 30 * i, r=90_000, u=10_000, o=2_000, model=OPUS48,
                          speed="fast") for i in range(8)])
    c = gctx()
    # the generic detector itself prices the premium on the Copilot lane …
    direct = premium.PremiumModifiers().detect([fast], c)
    assert only(direct, "fast-premium")
    # … and the registry drops it (FAMILY_EXCLUSIONS: report shows 100% of fast use)
    assert catalog.FAMILY_EXCLUSIONS[("premium.modifiers", "fast-premium")] == \
        frozenset({"copilot"})
    via = registry.run_detectors([fast], c, only=["premium.modifiers"], aggregates_only=False)
    assert only(via, "fast-premium") == []
    # the replacement: copilot.org-scan fast-mode from the AI usage report (Appendix C.G8)
    line, agg = make_ai_usage_row(date_utc="2026-09-10", model="Claude Opus 4.8 (fast mode)",
                                  credits="29", input_tokens=10_000, cache_read_tokens=90_000,
                                  output_tokens=2_000, team="mobile",
                                  principal=make_principal(3))
    pools = (make_pool_month(seats={"business": "1000", "enterprise": "200"},
                             consumed_report_nano=3_100_000 * CREDIT),)
    org = ctx(caps=GENERIC_CAPS | {"aggregates", "copilot_billing"}, min_usd="0.01",
              aggregates=(agg,), cost_lines=(line,), pools=pools)
    replaced = registry.run_detectors([], org, only=["copilot.org-scan"], aggregates_only=True)
    [mode] = only(replaced, "fast-mode")
    assert mode.cost_observed.nano == 145_000_000


def _ci_lanes() -> list:
    a = attr(team="agents", product=CLI, workload="ci", billing_path="copilot_direct")
    out = []
    for n in range(3):
        key = f"ci-{n}"
        out.append(lane(key, [req(key, i, 60 * i, u=100_000, o=2_000, model=SONNET5, a=a)
                              for i in range(3)]))
    return out


def test_ci_run_cost_excluded_while_ci_uncapped_replaces_it() -> None:
    automation = pytest.importorskip("tokenbill.detect.automation")
    lanes = _ci_lanes()
    c = gctx()
    assert only(automation.Automation().detect(lanes, c), "ci-run-cost")
    found = registry.run_detectors(lanes, c, only=["automation", "copilot.lanes"],
                                   aggregates_only=False)
    assert only(found, "ci-run-cost") == []
    [uncapped] = only(found, "ci-uncapped")
    assert uncapped.detector_id == "copilot.lanes"
    assert catalog.FAMILY_EXCLUSIONS[("automation", "ci-run-cost")] == frozenset({"copilot"})


def test_static_prefix_excluded_while_static_overhead_replaces_it() -> None:
    context = pytest.importorskip("tokenbill.detect.context")
    key = "vs-sp"
    requests = [req(key, 0, 0, w=60_000, u=2_000, o=500, model=OPUS48)]
    requests += [req(key, i, 30 * i, r=60_000, u=2_000, o=500, model=OPUS48)
                 for i in range(1, 40)]
    ln = lane(key, requests, scope="ws:vs",
              events=[compaction_event(key, 5, "threshold", system=5_000, tools=30_000)])
    c = gctx(floor={("ws:vs", OPUS48): 35_000}, min_usd="0.01")
    assert only(context.StaticPrefix().detect([ln], c), "static-prefix")
    found = registry.run_detectors([ln], c, only=["context.static-prefix", "copilot.lanes"],
                                   aggregates_only=False)
    assert only(found, "static-prefix") == []
    [static] = only(found, "static-overhead")
    assert static.recoverable is not None and static.recoverable.upper_bound


def test_ttl_advisor_never_runs_on_copilot_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    cache_ttl = pytest.importorskip("tokenbill.detect.cache_ttl")
    seen: list[str] = []
    real = cache_ttl.TtlAdvisor.detect

    def spy(self: object, lanes: Sequence[object], c: AnalysisContext) -> list[Finding]:
        seen.extend(ln.lane_key for ln in lanes)  # type: ignore[attr-defined]
        return real(self, lanes, c)  # type: ignore[arg-type]

    monkeypatch.setattr(cache_ttl.TtlAdvisor, "detect", spy)
    lanes = [switch_lane(), *_ci_lanes()]
    found = registry.run_detectors(lanes, gctx(min_usd="0"), aggregates_only=False)
    assert seen == []
    assert not [f for f in found if f.detector_id in ("cache.ttl-advisor", "cache.gateway-"
                                                      "disabled", "cache.cold-fanout",
                                                      "premium.sticky-escalation")]
    assert not [f for f in found if (f.detector_id, f.kind) in
                (("model.routing", "default-model"), ("model.routing", "default-effort"))]
    for f in found:
        dims = dict(f.scope.dims)
        if dims.get("billing_class") == "pool":
            assert dims.get("product") == "copilot"
    _no_claude_code_keys(found)


def test_copilot_lanes_see_only_copilot_lanes_through_the_registry() -> None:
    a = attr(product=VSCODE)
    key = "vs-g5"
    ln = lane(key, [req(key, 0, 0, u=20_000, r=280_000, o=4_000, a=a)],
              kind=LaneKind.MAIN)
    found = registry.run_detectors([ln], gctx(), only=["copilot.lanes"], aggregates_only=False)
    assert [f.kind for f in found] == ["long-context-band"]
    assert run([ln], gctx()) == found
    assert T0 > 0
