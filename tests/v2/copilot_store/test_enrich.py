"""enrich_context (CP-STORE brief "Build" 1–4 and acceptance tests; addendum Appendix C.P9, P13,
P14; rulings R-E21, R-E22, R-E46) on MemoryStore + MemoryRecordStore (and CopilotRecordStore)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tokenbill.copilot.enrich import (
    CAPABILITY,
    DQ_CONVENTION_UNDECIDABLE,
    DQ_KEY_ID_MIXED,
    day_conventions,
    decided_cells,
    enrich_context,
)
from tokenbill.copilot.record_store import CopilotRecordStore
from tokenbill.core import builders as b
from tokenbill.core import extensions
from tokenbill.core import testing as kit
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.pool import build_cells
from tokenbill.core.records import OutcomeAggregate
from tokenbill.core.types import AnalysisContext

from .support import (
    KEY_A,
    ORG_KEY,
    USD,
    C,
    activity_seats,
    api_seats,
    coverage,
    flags,
    ledger_meta,
    month_window,
    ms,
    p,
    p9_ledger_rows,
    people,
    result,
    rows,
)

SEPT = month_window("2026-09")
OCT = month_window("2026-10")
PRICER = kit.FakePricer()


def base_ctx(window: tuple[int, int] = SEPT, **kw) -> AnalysisContext:
    return AnalysisContext(pricer=PRICER, rules=None, replayer=None,  # type: ignore
                           calibration=None, window=window, capabilities=frozenset({"usage"}),
                           **kw)


def world(lines=(), aggs=(), licenses=(), activity=(), config=(), *, outcomes=(),
          org_key: bytes | None = ORG_KEY) -> tuple[kit.MemoryStore, kit.MemoryRecordStore]:
    ledger = kit.MemoryStore(org_key=org_key)
    if lines or aggs or outcomes:
        r = result(cost_lines=lines, aggregates=aggs, adapter="github-ai-usage", key=org_key,
                   source_id="report")
        r.outcomes = list(outcomes)
        ledger.ingest(r)
    records = kit.MemoryRecordStore(ledger)
    if licenses or activity or config:
        counts = records.put(result(licenses, activity, config, key=org_key),
                             principal_key_id=key_id(org_key) if org_key else None)
        assert not counts["skipped"]
    return ledger, records


def enrich(ledger, records, ctx=None, *, today="2026-09-23", **kw) -> AnalysisContext:
    return enrich_context(ledger, [records], ctx or base_ctx(), today=today,
                          reconciled_channels=kw.pop("reconciled_channels", frozenset()), **kw)


# ---------------------------------------------------------------------------------------------
# Appendix C worlds
# ---------------------------------------------------------------------------------------------


def test_p9_pool_month_and_ext_copilot() -> None:
    lines, aggs = p9_ledger_rows()
    ledger, records = world(lines, aggs)
    ctx = base_ctx()
    out = enrich(ledger, records, ctx)
    assert CAPABILITY in out.capabilities and "usage" in out.capabilities
    assert CAPABILITY not in ctx.capabilities                     # a frozen replacement
    [pm] = out.pools
    assert (pm.entity_id, pm.month, pm.finality, pm.regime) == ("enterprise", "2026-09", "open",
                                                               "overage")
    assert (pm.pool_credits, pm.seats_source, pm.days_final, pm.days_provisional) == (
        "2680000", "seat_lines", 20, 2)
    assert pm.consumed_report_nano == 2_200_000 * C and pm.consumed_estimate_nano == 0
    fc, over = pm.forecast, pm.overage_forecast
    assert fc is not None and over is not None
    assert (fc.nano, fc.low_nano, fc.high_nano) == (3_100_000 * C, 2_920_000 * C, 3_280_000 * C)
    assert (fc.evidence, fc.basis) == (Evidence.ESTIMATED, Basis.LIST_EQUIVALENT)
    assert (over.nano, over.low_nano, over.high_nano) == (4_200 * USD, 2_400 * USD, 6_000 * USD)
    assert over.evidence is Evidence.ESTIMATED
    [pe] = out.plans
    assert (pe.plan, pe.source, pe.seats) == ("mixed", "seat_lines",
                                              (("business", 1000), ("enterprise", 200)))
    assert len(out.cost_lines) == len(lines) and len(out.aggregates) == len(aggs)


def test_p13_plan_unknown_two_scenarios_and_one_evidence() -> None:
    usage, aggs = rows(250_000, date="2026-10-10", users=people(50), discount=250_000)
    ledger, records = world(usage, aggs, activity_seats(100),
                            config=[flags({"billing_mode.enterprise": "metered"})])
    out = enrich(ledger, records, base_ctx(OCT), today="2026-11-10")
    [pe] = out.plans
    assert (pe.entity_id, pe.month, pe.plan, pe.source, pe.seats) == (
        "enterprise", "2026-10", "unknown", "none", (("unknown", 100),))
    biz, ent = out.pools
    assert (biz.plan_scenario, ent.plan_scenario) == ("business", "enterprise")
    assert (biz.pool_credits, biz.overage_observed_nano, biz.regime) == ("190000", 600 * USD,
                                                                         "overage")
    assert (ent.pool_credits, ent.overage_observed_nano, ent.regime) == ("390000", 0, "slack")
    assert {pm.billing_mode for pm in out.pools} == {"metered"}
    assert len(out.licenses) == 100 and all(x.assigned_via_team is None for x in out.licenses)
    assert {"licenses", "config", CAPABILITY} <= out.capabilities


def test_p14_conflicting_evidence() -> None:
    seat = b.make_seat_line("enterprise", "50", date_utc="2026-09-01")
    usage, aggs = rows(100_000, date="2026-09-10", discount=100_000)
    ledger, records = world([seat, *usage], aggs, api_seats(50, "business"),
                            config=[flags({"plan.enterprise": "business"})])
    out = enrich(ledger, records, today="2026-10-10")
    [pm] = out.pools
    assert (pm.pool_credits, pm.plan_source, pm.plan_conflict, pm.plan_scenario) == (
        "195000", "seat_lines", True, None)
    [pe] = out.plans
    assert pe.conflict and pe.plan == "enterprise"
    assert any("dq.copilot_plan_conflict" in line for line in pe.evidence)


def test_billing_mode_from_run_flags_and_the_cli_snapshot_wins() -> None:
    usage, aggs = rows(10_000, date="2026-09-10")
    seat = b.make_seat_line("business", "10", date_utc="2026-09-01")
    answers = flags({"billing_mode.enterprise": "azure", "promo_eligible": False},
                    entity="admin_answers", snapshot_ms=ms("2026-09-02"))
    # the CLI snapshot is taken today (after the window): it still applies to the window
    cli = flags({"billing_mode.enterprise": "volume"}, snapshot_ms=ms("2026-10-02"))
    ledger, records = world([seat, *usage], aggs, config=[answers, cli])
    out = enrich(ledger, records, today="2026-10-02")
    [pm] = out.pools
    assert pm.billing_mode == "volume"
    ledger2, records2 = world([seat, *usage], aggs)
    [plain] = enrich(ledger2, records2, today="2026-10-02").pools
    assert plain.billing_mode == "metered"


def test_store_without_copilot_data_leaves_the_context_unchanged() -> None:
    ctx = base_ctx()
    ledger, records = world()
    assert enrich(ledger, records, ctx) is ctx
    claude = kit.MemoryStore(org_key=ORG_KEY)
    claude.ingest(result(cost_lines=[b.make_cost_line(5 * USD, date_utc="2026-09-10")],
                         aggregates=[b.make_aggregate({"uncached_input": 5},
                                                      bucket_start_ms=ms("2026-09-10"),
                                                      bucket_end_ms=ms("2026-09-11"))],
                         adapter="anthropic-admin"))
    assert enrich(claude, kit.MemoryRecordStore(claude), ctx,
                  reconciled_channels=frozenset({"anthropic_api"}),
                  recon_decisions=(("convention:s_x", "excl"),)) is ctx
    # Copilot data outside the window's months does not count either
    usage, aggs = rows(10, date="2026-08-10")
    ledger, records = world(usage, aggs, activity_seats(3, date="2026-08-01"))
    assert enrich(ledger, records, ctx) is ctx
    # an empty window is left alone
    empty = base_ctx((ms("2026-09-10"), ms("2026-09-10")))
    assert enrich(ledger, records, empty) is empty


def test_lanes_only_copilot_data_adds_the_capability() -> None:
    ledger = kit.MemoryStore(org_key=ORG_KEY)
    req = b.make_request("lane", 0, ms("2026-09-10", 12), {"uncached_input": 100, "output": 10},
                         "claude-opus-5-5", provider="github", channel="github_copilot",
                         billing_path="copilot_pool", request_id="rq-1",
                         attribution={"principal": "r_dev", "team": "t",
                                      "billing_path": "copilot_pool"})
    ledger.ingest(dataclasses.replace(result(adapter="copilot-otel", key=None), requests=[req]))
    out = enrich(ledger, kit.MemoryRecordStore(ledger))
    assert CAPABILITY in out.capabilities and out.pools == () and out.plans == ()


# ---------------------------------------------------------------------------------------------
# conventions (CP-RECON decisions, R-E46)
# ---------------------------------------------------------------------------------------------


def tokens_world() -> tuple[list, list]:
    lines, aggs = [], []
    for day in ("2026-09-03", "2026-09-04"):
        ls, ag = rows(1_000, date=day, input_tokens=50_000, cache_read=30_000,
                      cache_write=5_000)
        lines += ls
        aggs += ag
    return lines, aggs


def test_incl_decision_changes_uncached_exactly_as_build_cells() -> None:
    lines, aggs = tokens_world()
    cov = [coverage("s_report", d) for d in ("2026-09-03", "2026-09-04")]
    excl, _ = build_cells(aggs, lines, convention="excl")
    incl, _ = build_cells(aggs, lines, convention="incl")
    got, notes = decided_cells(aggs + cov, lines,
                               recon_decisions=(("convention:s_report", "incl"),))
    assert got == incl and notes == ()
    assert [c.usage.uncached_input for c in got] != [c.usage.uncached_input for c in excl]
    assert decided_cells(aggs + cov, lines)[0] == excl                   # no decision → excl
    assert decided_cells(aggs + cov, lines,
                         recon_decisions=(("convention:s_report", "excl"),))[0] == excl
    # the enricher carries the decision to the detectors and builds pools from the same cells
    ledger, records = world(lines, aggs + cov)
    out = enrich(ledger, records, recon_decisions=(("convention:s_report", "incl"),))
    assert out.recon_decisions == (("convention:s_report", "incl"),)
    assert decided_cells(out.aggregates, out.cost_lines,
                         recon_decisions=out.recon_decisions)[0] == incl


def test_mixed_files_use_each_days_decision() -> None:
    lines, aggs = tokens_world()
    cov = [coverage("s_a", "2026-09-03"), coverage("s_b", "2026-09-04")]
    decisions = (("convention:s_a", "incl"), ("convention:s_b", "excl"))
    conventions, undecidable = day_conventions(aggs + cov, decisions)
    assert conventions == {"2026-09-03": "incl", "2026-09-04": "excl"} and undecidable == {}
    got, _ = decided_cells(aggs + cov, lines, recon_decisions=decisions)
    incl = [c for c in build_cells(aggs, lines, convention="incl")[0]
            if c.date_utc == "2026-09-03"]
    excl = [c for c in build_cells(aggs, lines, convention="excl")[0]
            if c.date_utc == "2026-09-04"]
    assert got == incl + excl


def test_overlapping_exports_take_the_latest_file_and_tagged_aggregates() -> None:
    lines, aggs = tokens_world()
    cov = [coverage("s_old", "2026-09-03", fetched_ms=1), coverage("s_new", "2026-09-03",
                                                                   fetched_ms=9)]
    conv, _ = day_conventions(aggs + cov, (("convention:s_old", "excl"),
                                           ("convention:s_new", "incl")))
    assert conv["2026-09-03"] == "incl" and conv["2026-09-04"] == "excl"
    tagged = [dataclasses.replace(a, dims=tuple(sorted((*a.dims, ("source", "s_t")))))
              for a in aggs]
    conv, _ = day_conventions(tagged, (("convention:s_t", "incl"),))
    assert set(conv.values()) == {"incl"}
    other = b.make_aggregate(None, source_kind="gh_aw.run", dims={"source": "s_x"},
                             bucket_start_ms=ms("2026-09-05"), bucket_end_ms=ms("2026-09-06"))
    assert day_conventions([other])[0] == {}


def test_undecidable_convention_uses_excl_with_the_dq_code() -> None:
    lines, aggs = tokens_world()
    cov = [coverage("s_u", d) for d in ("2026-09-03", "2026-09-04")]
    got, notes = decided_cells(aggs + cov, lines,
                               recon_decisions=(("convention:s_u", "undecidable"),))
    assert got == build_cells(aggs, lines, convention="excl")[0]
    [note] = notes
    assert (note.code, note.count, note.severity) == (DQ_CONVENTION_UNDECIDABLE, 1, "warn")
    ledger, records = world(lines, aggs + cov)
    out = enrich(ledger, records, recon_decisions=(("convention:s_u", "undecidable"),))
    assert len(out.pools) == 2      # report users only: plan unknown, two scenarios
    assert all(any(n.startswith(DQ_CONVENTION_UNDECIDABLE) for n in pm.notes)
               for pm in out.pools)
    clean = enrich(ledger, records, recon_decisions=(("convention:s_u", "excl"),))
    assert not any(n.startswith("dq.") for pm in clean.pools for n in pm.notes)


# ---------------------------------------------------------------------------------------------
# estimates, discounts, decisions, channels
# ---------------------------------------------------------------------------------------------


def test_recent_estimates_never_enter_report_sums() -> None:
    usage, aggs = rows(1_000, date="2026-09-10", users=people(2))
    capped_usage, capped_aggs = rows(100, date="2026-09-10", cost_center="capped")
    usage, aggs = usage + capped_usage, aggs + capped_aggs
    act = [b.make_activity(people(2)[0], date_utc="2026-09-10", reported_cost_nano=5 * C),
           b.make_activity(people(2)[0], date_utc="2026-09-22", reported_cost_nano=7 * C),
           b.make_activity(people(2)[1], date_utc="2026-09-22", reported_cost_nano=3 * C,
                           cost_center="capped"),
           b.make_activity(people(2)[1], date_utc="2026-09-21", reported_cost_nano=None),
           b.make_activity(people(2)[0], date_utc="2026-09-20", reported_cost_nano=-4)]
    ledger, records = world(usage, aggs, activity=act,
                            config=[b.make_config("cost_center", {"pool_enabled": True,
                                                                  "pool_target_credits": "100"},
                                                  entity_id="cc:capped",
                                                  source_kind="github.cost_centers",
                                                  snapshot_ms=ms("2026-09-01"))])
    out = enrich(ledger, records)
    by_entity = {pm.entity_id: pm for pm in out.pools}
    ent = by_entity["enterprise"]
    assert ent.consumed_report_nano == 1_000 * C          # the report only
    assert ent.consumed_estimate_nano == 7 * C             # 09-22 only (09-10 is in the report)
    assert by_entity["cc:capped"].consumed_estimate_nano == 3 * C
    assert len(out.activity) == 5 and "activity" in out.capabilities


def test_gross_is_list_decision_classifies_the_pool_draw() -> None:
    usage, aggs = rows(100_000, date="2026-09-10", discount=100_000)
    seat = b.make_seat_line("business", "100", date_utc="2026-09-01")
    ledger, records = world([seat, *usage], aggs)
    [plain] = enrich(ledger, records, today="2026-10-10").pools
    assert plain.pool_draw_nano is None and plain.discount_unclassified_nano == 100_000 * C
    [decided] = enrich(ledger, records, today="2026-10-10",
                       recon_decisions=(("gross_is_list:enterprise:2026-09", "true"),)).pools
    assert decided.pool_draw_nano == 100_000 * C and decided.discount_unclassified_nano == 0


def test_channels_and_decisions_merge() -> None:
    usage, aggs = rows(10, date="2026-09-10")
    ledger, records = world(usage, aggs)
    ctx = base_ctx(reconciled_channels=frozenset({"anthropic_api"}),
                   recon_decisions=(("convention:s_b", "excl"),))
    out = enrich(ledger, records, ctx, reconciled_channels=frozenset({"github_copilot"}),
                 recon_decisions=[("gross_is_list:enterprise:2026-09", "unknown"),
                                  ("convention:s_b", "excl")])
    assert out.reconciled_channels == {"anthropic_api", "github_copilot"}
    assert out.recon_decisions == (("convention:s_b", "excl"),
                                   ("gross_is_list:enterprise:2026-09", "unknown"))
    with pytest.raises(ContractViolation):
        enrich(ledger, records, ctx, recon_decisions=(("convention:s_b", "incl"),))


def test_outcomes_rows_and_existing_context_rows_are_kept() -> None:
    usage, aggs = rows(10, date="2026-09-10")
    cop = OutcomeAggregate(date_utc="2026-09-10", team="(enterprise)", n_users=12, sessions=0,
                           commits=0, pull_requests=4, lines_added=0, lines_removed=0,
                           edits_accepted=0, edits_rejected=0, source_kind="github.copilot_metrics",
                           extra=(("prs_merged", 3),))
    cc = dataclasses.replace(cop, source_kind="anthropic.cc_analytics", extra=())
    ledger, records = world(usage, aggs, outcomes=[cop, cc])
    existing = b.make_cost_line(1, date_utc="2026-09-02")
    ctx = base_ctx(cost_lines=(existing, usage[0]), aggregates=(aggs[0],), outcomes=(cc,))
    out = enrich(ledger, records, ctx)
    assert out.cost_lines == (existing, usage[0]) and out.aggregates == (aggs[0],)
    assert out.outcomes == (cc, cop)


def test_org_entity_mode() -> None:
    usage, aggs = rows(10_000, date="2026-09-10", org="acme")
    ledger, records = world([b.make_seat_line("business", "5", organization="acme"), *usage],
                            aggs)
    out = enrich(ledger, records, entity_mode="org")
    assert {pm.entity_id for pm in out.pools} == {"org:acme"}
    assert {pe.entity_id for pe in out.plans} == {"org:acme"}


# ---------------------------------------------------------------------------------------------
# configuration as of the window, record stores, key ids
# ---------------------------------------------------------------------------------------------


def test_configuration_as_of_the_window() -> None:
    def cc(name: str, day: str, cap: str) -> object:
        return b.make_config("cost_center", {"pool_enabled": True, "pool_target_credits": cap},
                             entity_id=f"cc:{name}", source_kind="github.cost_centers",
                             snapshot_ms=ms(day))

    count_rows = [b.make_config("seat_counts", {"team": "t", "bucket": "*", "n_people": 5},
                                entity_id="org:o", snapshot_ms=ms(d)) for d in
                  ("2026-08-20", "2026-09-20", "2026-10-02")]
    quota = b.make_config("plan_quota", {"month": "2026-09", "quota": "3900", "n_users": 5},
                          entity_id="org:o", source_kind="github.ai_usage_report",
                          snapshot_ms=ms("2026-10-03"))
    old_flags = flags({"compliance": "none"}, snapshot_ms=ms("2025-01-01"))
    future = flags({"promo_eligible": True}, snapshot_ms=ms("2026-12-01"))
    rows_ = [cc("a", "2026-07-01", "1"), cc("a", "2026-08-01", "2"), cc("a", "2026-09-10", "3"),
             cc("a", "2026-10-05", "4"), cc("b", "2026-08-15", "5"), cc("c", "2026-10-05", "6"),
             cc("c", "2026-10-09", "7"), *count_rows, quota, old_flags, future]
    usage, aggs = rows(10, date="2026-09-10")
    ledger, records = world(usage, aggs, config=rows_)
    out = enrich(ledger, records, today="2026-10-10")
    got = {(c.kind, c.entity_id, dict(c.attrs).get("pool_target_credits"),
            c.snapshot_ms) for c in out.config}
    caps = sorted(x[2] for x in got if x[0] == "cost_center")
    assert caps == ["2", "3", "5", "6"]       # a: latest before + inside; b: before; c: earliest
    assert [c.snapshot_ms for c in out.config if c.kind == "seat_counts"] == [ms("2026-09-20")]
    assert quota in out.config and old_flags in out.config and future not in out.config


def test_copilot_record_store_gives_the_same_context(tmp_path: Path) -> None:
    usage, aggs = rows(250_000, date="2026-10-10", users=people(50), discount=250_000)
    seats = activity_seats(100)
    act = [b.make_activity(x, date_utc="2026-10-11", reported_cost_nano=2 * C)
           for x in people(3)]
    conf = [flags({"billing_mode.enterprise": "metered"}, snapshot_ms=ms("2026-10-01"))]
    ledger, memory = world(usage, aggs, seats, act, conf)
    path = tmp_path / "ledger.db"
    ledger_meta(path, org_key_id=key_id(ORG_KEY))
    sqlite_store = CopilotRecordStore(path)
    sqlite_store.put(result(list(reversed(seats)), act, conf), principal_key_id=key_id(ORG_KEY))
    a = enrich(ledger, memory, base_ctx(OCT), today="2026-10-20")
    b_ = enrich(ledger, sqlite_store, base_ctx(OCT), today="2026-10-20")
    assert a == b_ and len(a.pools) == 2


def test_record_stores_of_other_extensions_are_ignored_and_duplicates_merge() -> None:
    usage, aggs = rows(10, date="2026-09-10")
    ledger, records = world(usage, aggs, api_seats(3, "business"))
    twin = kit.MemoryRecordStore(ledger)
    twin.put(result(api_seats(3, "business")), principal_key_id=key_id(ORG_KEY))
    foreign = kit.MemoryRecordStore(ledger, name="other")
    foreign.put(result(api_seats(9, "business", seed="z")), principal_key_id=key_id(ORG_KEY))

    class Plain:            # an ExtRecordStore that cannot tell key ids
        name = "copilot"

        def licenses(self, **window):
            return records.licenses(**window)

        def activity(self, **window):
            return []

        def config(self, **window):
            return []

    out = enrich_context(ledger, [records, twin, Plain()], base_ctx(), today="2026-09-23",
                         reconciled_channels=frozenset())
    assert len(out.licenses) == 3
    import tokenbill.core.registry as reg
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(reg.EXTENSIONS, "other", dataclasses.replace(reg.EXTENSIONS["copilot"],
                                                                name="other"))
        out = enrich_context(ledger, [records, foreign], base_ctx(), today="2026-09-23",
                             reconciled_channels=frozenset())
        assert len(out.licenses) == 3


def test_seats_are_counted_under_one_key_id_per_month() -> None:
    ledger = kit.MemoryStore(org_key=ORG_KEY, adopt_key_ids=True)
    ledger.ingest(result(adapter="copilot-export", key=KEY_A, source_id="bundle"))
    records = kit.MemoryRecordStore(ledger)
    own = [b.make_license(p(ORG_KEY, f"u{i}"), snapshot_date="2026-09-05") for i in range(4)]
    bundle = [b.make_license(p(KEY_A, f"u{i}"), snapshot_date="2026-09-05") for i in range(6)]
    records.put(result(own), principal_key_id=key_id(ORG_KEY))
    records.put(result(bundle, key=KEY_A), principal_key_id=key_id(KEY_A))
    usage, aggs = rows(10, date="2026-09-10")
    ledger.ingest(result(cost_lines=usage, aggregates=aggs, adapter="github-ai-usage"))
    out = enrich(ledger, records, today="2026-10-10")
    [pe] = out.plans
    assert pe.seats == (("business", 6),)                 # the bundle's six, never 4 + 6
    [pm] = out.pools
    assert any(n.startswith(DQ_KEY_ID_MIXED) for n in pm.notes)
    assert len(out.licenses) == 10


def test_extension_host_calls_the_registered_enricher() -> None:
    lines, aggs = p9_ledger_rows()
    ledger, records = world(lines, aggs)
    notes: list = []
    out = extensions.enrich(ledger, [records], base_ctx(), today="2026-09-23",
                            reconciled_channels=frozenset({"github_copilot"}),
                            recon_decisions=(("gross_is_list:enterprise:2026-09", "true"),),
                            notes=notes)
    assert notes == [] and CAPABILITY in out.capabilities and len(out.pools) == 1
    assert out.reconciled_channels == {"github_copilot"}


def test_order_of_records_does_not_matter() -> None:
    usage, aggs = rows(250_000, date="2026-10-10", users=people(50), discount=250_000)
    seats = activity_seats(20)
    one = world(usage, aggs, seats)
    two = world(list(reversed(usage)), list(reversed(aggs)), list(reversed(seats)))
    assert enrich(*one, base_ctx(OCT), today="2026-11-01") == enrich(
        *two, base_ctx(OCT), today="2026-11-01")


# ---------------------------------------------------------------------------------------------
# argument checks and extreme windows
# ---------------------------------------------------------------------------------------------


def test_argument_checks() -> None:
    ledger, records = world(*rows(10, date="2026-09-10"))
    with pytest.raises(ContractViolation):
        enrich_context(ledger, [records], "ctx", today="2026-09-23",  # type: ignore[arg-type]
                       reconciled_channels=frozenset())
    for bad in ("2026-9-23", "2026-02-30", None, "yesterday!!"):
        with pytest.raises(UsageError):
            enrich(ledger, records, today=bad)
    with pytest.raises(UsageError):
        enrich(ledger, records, entity_mode="team")
    with pytest.raises(UsageError):
        enrich(ledger, records, reconciled_channels=frozenset({1}))
    for bad in ("convention:x", [("a",)], [("a", 1)], 5):
        with pytest.raises(UsageError):
            enrich(ledger, records, recon_decisions=bad)
    with pytest.raises(UsageError):
        enrich(ledger, records, base_ctx(window=(0,)))  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        decided_cells([], [], recon_decisions="x")  # type: ignore[arg-type]


def test_extreme_windows() -> None:
    lines, aggs = p9_ledger_rows()
    ledger, records = world(lines, aggs)
    wide = enrich(ledger, records, base_ctx((0, 2**53)))
    assert [pm.month for pm in wide.pools] == ["2026-09"]
    late = base_ctx((ms("9999-12-01"), 2**53))
    assert enrich(ledger, records, late) is late
    neg = base_ctx((-(10**15), ms("2026-09-02")))
    assert [pm.month for pm in enrich(ledger, records, neg).pools] == ["2026-09"]
    dec = enrich(ledger, records, base_ctx((ms("2026-12-05"), ms("2026-12-06"))))
    assert dec.pools == ()
