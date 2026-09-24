"""Merge gate F' — the GitHub Copilot core composes end to end (CORE-AMENDMENTS §4; owner F-KIT-C).

builders → Copilot cost lines, aggregates, licenses (one activity-report seat with plan
``unknown``) and a Copilot lane → ``MemoryStore`` + ``MemoryRecordStore`` → ``core.pool``
(``detect_plans`` / ``build_cells`` / ``pool_months``: the C.P9 month and the C.P13 scenario pair)
→ ``FakePricer`` prices a ``copilot_pool`` inference into ``PricedTotal.pool`` (C.G1) →
``run_detectors(aggregates_only=False)`` then ``(True)`` with a test aggregate detector
(``extension="copilot"``) and test lane detectors (family filter, ``FAMILY_EXCLUSIONS`` drop,
aggregate detector once, skipped silently without ``ext:copilot``) → ``rescope_findings`` with
``count_users_fn`` (cost-line and license sources, the R-E16 entity exemption, ``plan_scenario``
kept) → ``build_finding`` with a LIST recoverable beside a LIST_EQUIVALENT ``cost_observed`` and
``headroom`` on a ``product=copilot`` scope (R-E20) → ``core.extensions`` with a fake extension
(``rewrite_argv`` incl. the multi-token target, ``render_sections``, ``focus_rows`` owned channels,
``run_reconcilers``, ``recon_decisions_of``, missing-module dq) → ``MemoryStore(org_key=K,
adopt_key_ids=True)`` with a ``copilot-export`` batch under A (adopted), an ``r_`` batch
(pseudonymized with K) and another adapter's batch under B (nulled, B not adopted) → ``publish``
keeps a ``users_unknown`` row (R-E10) → a ``RunResult`` with ``copilot`` set. Also: every
``AGGREGATE_GRIDS`` entry round-trips, every Copilot lever's ``patch_keys`` resolves, every
``fix_for`` entry targets ``github-copilot``, and ``sniff_adapter`` with every Copilot adapter
module absent raises nothing and records ``dq.adapter_unavailable``.

The parts that need a sibling wave-1.5b package (F-SEM-C, F-EXT, F-POOL) skip until it is merged
(``pytest.importorskip`` / a feature check); the rest runs on F-CORE-C + F-KIT-C alone.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokenbill.core import catalog, kanon
from tokenbill.core import registry as reg
from tokenbill.core import testing as kit
from tokenbill.core.builders import (
    CANARY_LOGIN,
    assert_no_canary,
    make_ai_usage_row,
    make_config,
    make_copilot_ctx,
    make_inference,
    make_license,
    make_pool_month,
    make_request,
    make_seat_line,
)
from tokenbill.core.errors import ContractViolation
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.labels import Basis, Evidence, estimated, exact
from tokenbill.core.records import Lane, UsageBuckets, to_json
from tokenbill.core.types import (
    AdminAction,
    AggRow,
    AnalysisContext,
    CopilotSummary,
    FocusRow,
    IngestResult,
    PricedTotal,
    PrivacyInfo,
    RateCardInfo,
    RawAggregate,
    ReconciliationReport,
    RunResult,
    Scope,
    SourceInfo,
)

pytestmark = pytest.mark.gate

ORG = bytes(range(11, 43))          # the store's own org key K
EXPORT_A = bytes(range(21, 53))     # the admin's export key (bundle)
OTHER_B = bytes(range(31, 63))      # somebody else's key
W = {"since_ms": 0, "until_ms": 2**53}
CREDIT_NANO = 10_000_000


def p(name: str, key: bytes = ORG) -> str:
    return pseudonym(key, "p", name)


def source(source_id: str, adapter: str, key: bytes | None) -> SourceInfo:
    return SourceInfo(source_id=source_id, adapter=adapter, name_hmac="h_" + "4" * 20,
                      sha256=source_id, bytes=1, name_key_id=None,
                      principal_key_id=key_id(key) if key else None)


def ingest_result(src: SourceInfo, **parts) -> IngestResult:
    result = IngestResult(source=src, requests=list(parts.get("requests", [])), sessions=[],
                          events=[], aggregates=list(parts.get("aggregates", [])),
                          cost_lines=list(parts.get("cost_lines", [])), outcomes=[],
                          quarantined=[], notes=[], stats=dict(parts.get("stats", {})),
                          capabilities=frozenset({"credits", "copilot_billing", "licenses"}))
    result.licenses = list(parts.get("licenses", []))
    result.config = list(parts.get("config", []))
    return result


# ---------------------------------------------------------------------------------------------
# the world (builders → MemoryStore + MemoryRecordStore)
# ---------------------------------------------------------------------------------------------

#: Appendix C.P9 (binding series of the F-POOL brief): observed days 1–20 of September 2026.
P9_DAYS = {**{d: 130_000 for d in (1, 2, 3, 4)},
           **dict(zip((7, 8, 9, 10, 11, 14, 15, 16, 17, 18),
                      (110_000, 110_000, 120_000, 120_000, 130_000, 130_000, 140_000, 140_000,
                       150_000, 150_000), strict=True)),
           5: 30_000, 6: 30_000, 12: 20_000, 13: 30_000, 19: 30_000, 20: 40_000}


def build_world() -> SimpleNamespace:
    pricer = kit.FakePricer()
    store = kit.MemoryStore(org_key=ORG, pricer=pricer)
    # pooled AI usage report rows (C.P9) and the seat lines of C.P1 (1,000 Business + 200
    # Enterprise seats → pool 2,680,000 credits)
    lines, aggs = [], []
    for day, credits in sorted(P9_DAYS.items()):
        line, agg = make_ai_usage_row(date_utc=f"2026-09-{day:02d}", principal=p("heavy"),
                                      credits=str(credits), team="platform",
                                      cost_center="cc-eng", finality="final",
                                      input_tokens=1000, output_tokens=100)
        lines.append(line)
        aggs.append(agg)
    for i, team in enumerate(("platform", "platform", "data", "data", "data", "infra")):
        line, agg = make_ai_usage_row(date_utc="2026-09-10", principal=p(f"dev{i}"),
                                      credits="1", team=team, cost_center="cc-eng",
                                      model="GPT-5.5", finality="final")
        lines.append(line)
        aggs.append(agg)
    seats = [make_seat_line("business", "1000", date_utc="2026-09-01"),
             make_seat_line("enterprise", "200", date_utc="2026-09-01")]
    lane_reqs = [make_request("cp-lane", i, kit._ts("2026-09-23") + 60_000 * i,
                              {"uncached_input": 12_000, "cache_read": 180_000,
                               "cache_write_unknown": 6000, "output": 3000}, "claude-opus-5-5",
                              provider="github", channel="github_copilot",
                              billing_path="copilot_pool", request_id=f"rq-cp-{i}",
                              attribution={"principal": p("dev0"), "team": "platform",
                                           "billing_path": "copilot_pool",
                                           "agent_product": "copilot_vscode"})
                 for i in range(2)]
    claude_reqs = [make_request("cc-lane", 0, kit._ts("2026-09-23"), {"output": 500},
                                "claude-opus-5-5", request_id="rq-cc-0",
                                attribution={"principal": p("dev9"), "team": "platform",
                                             "billing_path": "api_key",
                                             "agent_product": "claude_code"},
                                billing_path="api_key")]
    store.ingest(ingest_result(source("report", "github-ai-usage", ORG), cost_lines=lines + seats,
                               aggregates=aggs, stats={"records": len(lines),
                                                       "rounding_remainders": 0}))
    store.ingest(ingest_result(source("otel", "copilot-otel", ORG),
                               requests=lane_reqs + claude_reqs))
    records = kit.MemoryRecordStore(store)
    licenses = [make_license(p(f"dev{i}"), snapshot_date="2026-09-20", team=team,
                             last_activity_bucket="0-7" if i < 4 else "none_90d")
                for i, team in enumerate(("platform", "platform", "data", "data", "data",
                                          "infra"))]
    licenses.append(make_license(p("report-seat"), snapshot_date="2026-09-20", team="infra",
                                 plan="unknown", org=None, assigned_via_team=None,
                                 source_kind="github.copilot_activity_report"))
    counts = records.put(ingest_result(source("seats", "github-copilot-seats", ORG),
                                       licenses=licenses,
                                       config=[make_config("run_flags",
                                                           {"promo_eligible": True})]),
                         principal_key_id=key_id(ORG))
    assert counts["licenses"] == 7 and counts[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 0
    return SimpleNamespace(pricer=pricer, store=store, records=records, licenses=licenses,
                           lines=lines + seats, aggs=aggs)


# ---------------------------------------------------------------------------------------------
# catalog contracts and sniffing (F-CORE-C + F-KIT-C only)
# ---------------------------------------------------------------------------------------------


def test_catalog_contracts() -> None:
    for lever_id, grid in catalog.AGGREGATE_GRIDS.items():
        assert catalog.lever(lever_id).replay == "aggregate"
        for spec in grid:
            assert catalog.to_aggregate_spec(catalog.parse_aggregate_spec(spec)) == spec
    for lv in catalog.COPILOT_LEVERS:
        for key in lv.patch_keys:
            assert key in catalog.COPILOT_ALLOWLIST or key in catalog.ADMIN_ACTIONS, key
    for (detector_id, kind), fix in catalog._COPILOT_FIXES.items():
        assert fix.target == "github-copilot", (detector_id, kind)
        assert catalog.fix_for(detector_id, kind or "x", "copilot") is not None
        assert all(k in catalog.COPILOT_ALLOWLIST for k, _ in fix.config_patch or ())
    assert [lv.lever_id for lv in catalog.levers_for_kind("idle-seat", family="copilot")] == [
        "copilot.seat_reclaim"]


COPILOT_ADAPTERS = ("copilot-cli", "copilot-otel", "copilot-vscode-traces", "gh-aw-token-usage",
                    "github-ai-usage", "github-metered-usage", "github-billing-api",
                    "github-copilot-config", "github-copilot-metrics", "github-copilot-seats",
                    "github-agent-tasks", "github-usage-records",
                    "github-copilot-activity-report", "copilot-export")


def test_sniff_with_every_copilot_adapter_module_absent(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    for name in COPILOT_ADAPTERS:
        module = reg.BUILTIN_ADAPTERS[name].partition(":")[0]
        monkeypatch.setitem(sys.modules, module, None)  # importing it raises ImportError
    path = tmp_path / "unknown.bin"
    path.write_bytes(b"\x00\x01 not a trace \xff" * 10)
    notes: list = []
    reg.sniff_adapter(path, notes=notes)  # raises nothing
    missing = {n.detail.split()[1] for n in notes if n.code == reg.DQ_ADAPTER_UNAVAILABLE}
    assert set(COPILOT_ADAPTERS) <= missing


# ---------------------------------------------------------------------------------------------
# pool ledger (F-POOL)
# ---------------------------------------------------------------------------------------------


def test_pool_months_p9_and_p13() -> None:
    pool = pytest.importorskip("tokenbill.core.pool")
    w = build_world()
    cost_lines = w.store.cost_lines(**W)
    cells, _clamped = pool.build_cells(w.store.aggregates(**W), cost_lines)
    assert cells and all(c.entity_id == "enterprise" for c in cells)
    seat_licenses = [x for x in w.records.licenses(**W)
                     if x.source_kind == "github.copilot_seats"]
    plans = pool.detect_plans(cost_lines, seat_licenses, [], month="2026-09")
    (ev,) = [e for e in plans if e.entity_id == "enterprise"]
    assert (ev.plan, ev.source) == ("mixed", "seat_lines")
    months = [m for m in pool.pool_months(cells, cost_lines, seat_licenses, [],
                                          today="2026-09-23")
              if m.month == "2026-09" and m.entity_id == "enterprise"]
    (sept,) = months
    assert sept.plan_scenario is None and sept.finality == "open"
    assert sept.pool_credits == "2680000" and sept.regime == "overage"
    assert sept.forecast is not None and sept.forecast.evidence is Evidence.ESTIMATED
    assert (sept.forecast.nano, sept.forecast.low_nano, sept.forecast.high_nano) == (
        3_100_000 * CREDIT_NANO, 2_920_000 * CREDIT_NANO, 3_280_000 * CREDIT_NANO)
    assert sept.overage_forecast is not None and sept.overage_forecast.nano == 420_000 * \
        CREDIT_NANO
    # C.P13: 100 activity-report seats, plan unknown, 250,000 pooled credits in a closed month
    report_seats = [make_license(p(f"s{i}"), snapshot_date="2026-10-15", plan="unknown",
                                 org=None, assigned_via_team=None,
                                 source_kind="github.copilot_activity_report")
                    for i in range(100)]
    oct_line, oct_agg = make_ai_usage_row(date_utc="2026-10-10", principal=p("s0"),
                                          credits="250000", finality="final")
    cells13, _ = pool.build_cells([oct_agg], [oct_line])
    (ev13,) = pool.detect_plans([oct_line], report_seats, [], month="2026-10")
    assert (ev13.plan, ev13.source) == ("unknown", "none")
    assert dict(ev13.seats) == {"unknown": 100}
    pair = sorted(pool.pool_months(cells13, [oct_line], report_seats,
                                   [make_config("run_flags", {"billing_mode.enterprise":
                                                              "metered"})],
                                   today="2026-11-10"),
                  key=lambda m: m.plan_scenario or "")
    assert [m.plan_scenario for m in pair] == ["business", "enterprise"]
    business, enterprise = pair
    assert (business.pool_credits, business.regime) == ("190000", "overage")
    assert (enterprise.pool_credits, enterprise.regime) == ("390000", "slack")
    assert business.consumed_report_nano == enterprise.consumed_report_nano


# ---------------------------------------------------------------------------------------------
# pricing, detectors, publication (F-CORE-C + F-KIT-C; the family filter needs F-SEM-C)
# ---------------------------------------------------------------------------------------------


def test_fake_pricer_prices_copilot_into_the_pool() -> None:
    w = build_world()
    g1 = make_inference(UsageBuckets(uncached_input=12_000, cache_read=180_000,
                                     cache_write_unknown=6000, output=3000),
                        ctx=make_copilot_ctx(), inference_id="g1")
    total = kit.fake_price_total(w.pricer, [(g1, kit._ts("2026-09-23"))])
    assert total.pool is not None and total.allowance is None
    assert (total.pool.nano, total.pool.low_nano, total.pool.high_nano) == (
        174_000_000, 174_000_000, 192_000_000)
    lanes = list(w.store.iter_lanes(**W))
    agg = w.store.aggregate(group_by=["team"], **W)
    platform = next(r for r in agg.rows if dict(r.dims)["team"] == "platform")
    assert platform.priced.pool is not None and platform.priced.pool.nano == 348_000_000
    assert {lane.billing_class for lane in lanes} == {"pool", "billed"}


class GateAggregateDetector:
    """An aggregate Copilot detector: one entity-level finding from the cost lines."""

    id = "gate.copilot-aggregate"
    version = "1"
    kinds = ("pool-regime",)
    requires = frozenset()
    extension = "copilot"
    aggregate = True
    families = frozenset({"copilot"})
    runs = 0

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list:
        type(self).runs += 1
        credits = sum(c.list_amount_nano or 0 for c in ctx.cost_lines
                      if c.cost_type == "ai_credit.user")
        return [gate_finding(self.id, "pool-regime", {"product": "copilot",
                                                      "entity": "enterprise"}, 0, credits)]


class GateLaneDetector:
    """Registered as ``premium.modifiers``: a finding per lane, kinds fast-premium (excluded for
    Copilot) and geo-premium (kept)."""

    id = "premium.modifiers"
    version = "1"
    kinds = ("fast-premium", "geo-premium")
    requires = frozenset({"usage_sequence"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list:
        from tokenbill.core.findings import product_family

        out = []
        for lane in lanes:
            dims = {"team": lane.team or "", "lane_kind": lane.kind.value,
                    "billing_class": lane.billing_class}
            if product_family(lane) == "copilot":
                dims["product"] = "copilot"
            for kind in self.kinds:
                out.append(gate_finding(self.id, kind, dims, 1, 1000))
        return out


class GateTtlDetector:
    """Registered as ``cache.ttl-advisor`` (excluded for Copilot at detector level)."""

    id = "cache.ttl-advisor"
    version = "1"
    kinds = ("ttl-heterogeneous",)
    requires = frozenset({"usage_sequence"})
    seen: list[str] = []

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list:
        type(self).seen.extend(lane.lane_key for lane in lanes)
        return []


def gate_finding(detector_id: str, kind: str, dims: dict[str, str], n_users: int, nano: int):
    from tokenbill.core.types import Finding

    scope = Scope(dims=tuple(sorted(dims.items())))
    copilot_scope = dims.get("product") == "copilot"
    return Finding(
        finding_id=reg._finding_id(detector_id, kind, scope), detector_id=detector_id,
        kind=kind, detector_version="1", category="aggregate", lever_class="none",
        audience="org", title=kind, summary="No mechanical fix.", scope=scope, n_events=1,
        n_lanes=0, n_users=n_users, first_seen_ms=0,
        cost_observed=exact(nano, Basis.LIST_EQUIVALENT if copilot_scope else Basis.LIST),
        recoverable=None, references=("gate",))


def test_run_detectors_phases_families_and_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    findings_mod = pytest.importorskip("tokenbill.core.findings")
    if not hasattr(findings_mod, "product_family"):
        pytest.skip("F-SEM-C (core.findings.product_family) not merged")
    w = build_world()
    monkeypatch.setattr(reg, "BUILTIN_DETECTORS", {"premium.modifiers": GateLaneDetector,
                                                   "cache.ttl-advisor": GateTtlDetector})
    monkeypatch.setattr(reg, "_PLUGIN_DETECTORS", {GateAggregateDetector.id:
                                                   GateAggregateDetector})
    GateAggregateDetector.runs = 0
    GateTtlDetector.seen = []
    lanes = list(w.store.iter_lanes(**W))
    caps = frozenset({"usage_sequence", "ext:copilot"})
    ctx = AnalysisContext(pricer=w.pricer, rules=None, replayer=None, calibration=None,
                          window=(0, 2**53), capabilities=caps,
                          cost_lines=tuple(w.store.cost_lines(**W)))
    per_shard = reg.run_detectors(lanes, ctx, aggregates_only=False)
    assert GateAggregateDetector.runs == 0
    assert GateTtlDetector.seen == ["cc-lane"]  # never sees the Copilot lane
    kinds = {(f.kind, dict(f.scope.dims).get("product")) for f in per_shard}
    assert ("fast-premium", "copilot") not in kinds  # FAMILY_EXCLUSIONS drop
    assert {("geo-premium", "copilot"), ("fast-premium", None), ("geo-premium", None)} <= kinds
    once = reg.run_detectors([], ctx, aggregates_only=True)
    assert GateAggregateDetector.runs == 1
    assert [f.detector_id for f in once if f.category != "data-quality"] == [
        GateAggregateDetector.id]
    silent = reg.run_detectors([], dataclasses.replace(ctx, capabilities=frozenset(
        {"usage_sequence"})), aggregates_only=True)
    assert silent == [] and GateAggregateDetector.runs == 1  # skipped silently


def counter(w: SimpleNamespace):
    """F-EXT's ``count_users_fn`` when merged, else the kanon scope counter it wraps."""
    try:
        from tokenbill.core import extensions
    except ImportError:
        return kanon.scope_counter(w.store, [w.records], **W)
    return extensions.count_users_fn(w.store, [w.records], **W)


def test_rescope_with_scope_counts_and_scenarios() -> None:
    w = build_world()
    count = counter(w)
    idle = gate_finding("copilot.seats-budgets", "idle-seat",
                        {"product": "copilot", "entity": "enterprise", "team": "data",
                         "bucket": "none_90d", "plan_scenario": "business"}, 1, 190)
    premium = [gate_finding("copilot.org-scan", "premium-model-share",
                            {"product": "copilot", "entity": "enterprise", "team": t,
                             "cost_center": "cc-eng"}, 2, 100)
               for t in ("platform", "data", "infra")]
    regime = gate_finding("copilot.seats-budgets", "pool-regime",
                          {"product": "copilot", "entity": "enterprise",
                           "plan_scenario": "enterprise"}, 3, 5)
    out = kanon.rescope_findings([idle, *premium, regime], k=5, count_users=count)
    by_kind = {f.kind: f for f in out}
    assert by_kind["pool-regime"] is regime  # R-E16: entity count source, entity scope
    merged = by_kind["premium-model-share"]
    assert dict(merged.scope.dims) == {"product": "copilot", "entity": "enterprise",
                                       "cost_center": "cc-eng"}
    assert merged.n_users == 7  # distinct cost-line people of cc-eng (heavy + dev0..dev5)
    # the license count of team data × none_90d is 1 (< k): the finding climbs the Copilot chain
    # to the entity root (7 licensed people), keeping its plan_scenario
    rescoped = by_kind["idle-seat"]
    assert dict(rescoped.scope.dims) == {"product": "copilot", "entity": "enterprise",
                                         "plan_scenario": "business"}
    assert rescoped.n_users == 7
    assert kanon.rescope_findings([idle], k=8, count_users=count) == []


def test_build_finding_r_e20_on_a_copilot_scope() -> None:
    findings_mod = pytest.importorskip("tokenbill.core.findings")
    if not hasattr(findings_mod, "product_family"):
        pytest.skip("F-SEM-C (R-E20 build_finding) not merged")
    from tokenbill.core.types import Fix

    scope = findings_mod.make_scope(product="copilot", entity="enterprise", team="platform")
    fields = dict(
        finding_id=findings_mod.finding_id("cache.miss-by-cause", "model-switch", scope),
        detector_id="cache.miss-by-cause", kind="model-switch", detector_version="1",
        category="lever", lever_class="cache_transform", audience="org",
        title="Model switches rebuilt the cache", summary="Model switches rebuilt the cache.",
        scope=scope, n_events=3, n_lanes=1, n_users=6, first_seen_ms=0,
        cost_observed=exact(174_000_000, Basis.LIST_EQUIVALENT),
        recoverable=estimated(40_000_000, Basis.LIST, note="pool-converted"),
        headroom=estimated(60_000_000, Basis.LIST_EQUIVALENT, note="headroom"),
        fix=Fix(text="Pin the model.", config_patch=(("env.CLAUDE_CODE_SUBAGENT_MODEL",
                                                      '"x"'),),
                target="claude-code-managed-settings", doc_url=None),
        references=("gate",))
    built = findings_mod.build_finding(**fields)
    assert built.headroom is not None and built.headroom.basis is Basis.LIST_EQUIVALENT
    assert built.fix == catalog.fix_for("cache.miss-by-cause", "model-switch", "copilot")
    with pytest.raises(ContractViolation):
        findings_mod.build_finding(**{**fields, "headroom": estimated(1, Basis.LIST, note="x")})


# ---------------------------------------------------------------------------------------------
# the extension host (F-EXT) with a fake extension defined here
# ---------------------------------------------------------------------------------------------

FAKE_CHANNELS = ("gate_channel_a", "gate_channel_b")
RECEIVED: dict[str, object] = {}


def fake_reconciler(ledger, record_stores, pricer, *, since_ms, until_ms, tolerance_pct,
                    unexplained_pct, closed_only, today, rounding_remainders=None):
    RECEIVED["rounding_remainders"] = rounding_remainders
    RECEIVED["record_stores"] = len(record_stores)
    return ReconciliationReport(
        window=("2026-09-01", "2026-10-01"), tolerance_pct=tolerance_pct,
        unexplained_tolerance_pct=unexplained_pct, rows=(), token_coverage_pct=None,
        dollar_coverage_pct=None, rate_card_error=None, over_count_rows=0,
        effective_discount=(), residuals=(), unexplained_nano=0, channels=(),
        verdict="insufficient_data", finality=exact(0, Basis.LIST).finality,
        suggested_contract=None, rerun_verdict=None,
        decisions=(("convention:report", "excl"), ("gross_is_list:enterprise:2026-09", "true")))


def fake_focus_rows(store, record_stores, *, since_ms, until_ms, reconciled_channels, k,
                    allow_unreconciled, role):
    return [FocusRow(columns=(("BilledCost", "1.00"), ("x_Source", "gate")),
                     channel=FAKE_CHANNELS[0], reconciled=False)]


class FakeSection:
    name = "gate"

    def terminal(self, result: RunResult, *, width: int) -> str:
        return "GATE SECTION"

    def html(self, result: RunResult) -> str:
        return "<section>gate</section>"

    def json(self, result: RunResult) -> dict | None:
        return {"gate": True}


def fake_spec(name: str, *, missing: bool = False):
    module = "tokenbill.gate_missing_module" if missing else __name__
    return reg.ExtensionSpec(
        name=name, channels=FAKE_CHANNELS if not missing else ("gate_channel_z",), rate_files=(),
        rate_verifier=None, reconciler=f"{module}:fake_reconciler", record_store=None,
        context_enricher=None, summary_builder=None, section_renderer=f"{module}:FakeSection",
        focus_rows=f"{module}:fake_focus_rows", showback=None, policy_targets=(),
        panel_builder=None, command_module=None,
        argv_aliases=(reg.ArgvAlias("collect", f"{name}-cli", "first",
                                    (name, "collect", "--source", "cli")),))


def test_extension_host_with_a_fake_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    ext = pytest.importorskip("tokenbill.core.extensions")
    w = build_world()
    monkeypatch.setitem(reg.EXTENSIONS, "gatefake", fake_spec("gatefake"))
    monkeypatch.setitem(reg.EXTENSIONS, "gatemissing", fake_spec("gatemissing", missing=True))
    assert ext.rewrite_argv(["collect", "copilot-cli", "--out", "D"]) == [
        "copilot", "collect", "--source", "cli", "--out", "D"]
    assert ext.rewrite_argv(["scan", "--copilot", "--since", "X"]) == [
        "copilot", "scan", "--since", "X"]
    assert ext.rewrite_argv(["collect", "gatefake-cli"]) == [
        "gatefake", "collect", "--source", "cli"]
    assert ext.rewrite_argv(["collect", "claude-code"]) == ["collect", "claude-code"]
    result = RunResult(command="gate", window=(0, 1), inputs=(),
                       privacy=PrivacyInfo(content_tier="none", key_id=None,
                                           identity_mode="central-ingest", k=5,
                                           suppressed_groups=0), rate_card=None)
    notes: list = []
    sections = ext.render_sections(result, "terminal", width=100, notes=notes)
    assert "GATE SECTION" in sections
    assert any(n.code == "dq.extension_unavailable" and "gatemissing" in n.detail
               for n in notes)
    notes = []
    rows, owned = ext.focus_rows(w.store, [w.records], since_ms=0, until_ms=2**53,
                                 reconciled_channels=frozenset(), k=5, allow_unreconciled=True,
                                 role="primary", notes=notes)
    assert any(r.channel == "gate_channel_a" for r in rows)
    assert {"gate_channel_a", "gate_channel_b"} <= set(owned)  # b owned without rows
    notes = []
    reports = ext.run_reconcilers(w.store, [w.records], w.pricer, since_ms=0, until_ms=2**53,
                                  tolerance_pct="1", unexplained_pct="1", closed_only=False,
                                  today="2026-09-23", notes=notes)
    assert RECEIVED["rounding_remainders"] is not None  # MemoryStore is LedgerStats
    decisions = ext.recon_decisions_of(reports)
    assert ("convention:report", "excl") in decisions and list(decisions) == sorted(decisions)
    clash = dataclasses.replace(reports[0], decisions=(("convention:report", "incl"),))
    with pytest.raises(ContractViolation):
        ext.recon_decisions_of([*reports, clash])
    count = ext.count_users_fn(w.store, [w.records], since_ms=0, until_ms=2**53)
    f = gate_finding("copilot.org-scan", "fast-mode", {"product": "copilot",
                                                        "entity": "enterprise",
                                                        "team": "data"}, 1, 1)
    assert count(f, f.scope) == 3


# ---------------------------------------------------------------------------------------------
# key-id adoption, users-unknown rows, and the RunResult
# ---------------------------------------------------------------------------------------------


def test_adoption_publication_and_run_result() -> None:
    store = kit.MemoryStore(org_key=ORG, adopt_key_ids=True, pricer=kit.FakePricer())
    bundle_line, bundle_agg = make_ai_usage_row(principal=p("admin-u1", EXPORT_A), credits="5",
                                                team="platform")
    store.ingest(ingest_result(source("bundle", "copilot-export", EXPORT_A),
                               cost_lines=[bundle_line], aggregates=[bundle_agg]))
    cli_req = make_request("cli-lane", 0, kit._ts("2026-09-23"), {"output": 10},
                           "claude-sonnet-5", provider="github", channel="github_copilot",
                           billing_path="copilot_direct", request_id="rq-cli",
                           attribution={"principal": "r_dev-laptop-1", "team": "platform",
                                        "billing_path": "copilot_direct"})
    store.ingest(ingest_result(source("cli", "copilot-cli", None), requests=[cli_req]))
    vendor_line, _ = make_ai_usage_row(principal=p("stranger", OTHER_B), credits="7",
                                       date_utc="2026-09-11")
    counts = store.ingest(ingest_result(source("vendor", "github-ai-usage", OTHER_B),
                                        cost_lines=[vendor_line]))
    meta = store.meta()
    assert meta["adopted_key_id"] == key_id(EXPORT_A) and meta["org_key_id"] == key_id(ORG)
    assert counts[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 1
    principals = {c.principal for c in store.cost_lines(**W)}
    assert principals == {p("admin-u1", EXPORT_A), None}
    (req,) = store.iter_requests(**W)
    assert req.attribution.principal == p("dev-laptop-1")
    # R-E10: a provider aggregate without a principal dimension, grouped by model
    unknown = RawAggregate(group_by=("model",), rows=(
        AggRow(dims=(("model", "claude-opus-5-5"),), n_users=0, n_requests=4,
               usage=UsageBuckets(uncached_input=10),
               priced=PricedTotal(exact=exact(0, Basis.LIST), estimated=None, allowance=None,
                                  priced_inferences=1, unpriced_inferences=0, unpriced_tokens=0,
                                  coverage="1", pool=exact(9, Basis.LIST_EQUIVALENT))),),
        window=(0, 1))
    published = kanon.publish(unknown, k=5)
    assert len(published.rows) == 1
    assert kanon.row_notes(published.rows[0], group_by=published.group_by) == (
        kanon.USERS_UNKNOWN,)
    teams = kanon.publish(store.aggregate(group_by=["team"], **W), k=5)
    pools = (make_pool_month(month="2026-09", seats={"unknown": "100"},
                             plan_scenario="business", seats_source="licenses"),
             make_pool_month(month="2026-09", seats={"unknown": "100"},
                             plan_scenario="enterprise", seats_source="licenses"))
    confirm = catalog.ADMIN_ACTIONS["admin:plan_confirm"]
    action = AdminAction(action_id="act-1", lever_id=None, admin_action=confirm.action_id,
                         where=confirm.where, what=confirm.template, doc_url=confirm.doc_url,
                         rest_file=None, auth_note=confirm.auth_note, reach=None,
                         projection=None, deadline=None, needs_eval=False, tradeoff=False)
    summary = CopilotSummary(window=("2026-09-01", "2026-10-01"), lines=(), pools=pools,
                             teams=teams, seat_counts=(("platform", "business:0-7", 5),),
                             plan=None, actions=(action,),
                             channel_verdicts=(("github_copilot", "not_reconciled"),),
                             notes=("plan unknown: both scenarios shown",))
    run = RunResult(command="copilot scan", window=(0, 2**53),
                    inputs=((source("bundle", "copilot-export", EXPORT_A), 1, 0),),
                    privacy=PrivacyInfo(content_tier="none", key_id=key_id(ORG),
                                        identity_mode="central-ingest", k=5,
                                        suppressed_groups=teams.suppressed_rows),
                    rate_card=RateCardInfo(sha256=kit.FakePricer().rate_card_sha256,
                                           layers=("facts",), stale_rows=(), contract=None,
                                           basis=Basis.LIST),
                    copilot=summary)
    encoded = to_json(run)
    assert encoded["copilot"]["pools"][0]["plan_scenario"] == "business"
    assert run.copilot is summary and len(run.copilot.pools) == 2
    assert_no_canary(str(encoded))
    assert CANARY_LOGIN not in str(encoded)
    assert Decimal(pools[1].pool_credits) == Decimal(390000)
