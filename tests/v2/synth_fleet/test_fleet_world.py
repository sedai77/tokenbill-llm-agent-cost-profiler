"""SPEC §18 acceptance on the generated world (seed 7): teams, plants and truth for every team,
the control team, the tiny team, billing paths and channels, provider-side records, reconciliation
truth and the store round trip."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise

import pytest

from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.records import LaneEventKind, LaneKind, billing_class, to_json
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.core.transitions import classify_transitions
from tokenbill.synth import fleet as F
from tokenbill.synth import truth as T

DAY_MS = 86_400_000


def test_team_table_and_sizes(world: F.FleetWorld) -> None:
    assert [t.name for t in F.TEAMS] == ["platform", "payments", "search", "mobile", "infra",
                                         "data", "ops", "ci-bots", "agents", "core", "tiny"]
    assert sum(t.devs for t in F.TEAMS) == 61
    sizes = {t.team: (t.devs, len(t.principals)) for t in world.truth.teams}
    assert sizes == {t.name: (t.devs, t.devs) for t in F.TEAMS}
    assert world.truth.team("tiny").principals and len(world.truth.team("tiny").principals) == 3
    grown = F.team_sizes(72)
    assert sum(grown.values()) == 72 and grown["tiny"] == 3
    with pytest.raises(UsageError):
        F.team_sizes(60)
    for bad in ({"devs": 12}, {"days": 3}, {"days": 40}, {"seed": "7"},
                {"scale_requests": 0}):
        with pytest.raises(UsageError):
            F.generate(**bad)  # type: ignore[arg-type]


def test_window_today_and_provisional_days(world: F.FleetWorld) -> None:
    assert world.window_start == "2026-09-22" and world.window_end == "2026-10-19"
    assert world.today == "2026-11-13"                      # window end + 25 days
    recon = world.truth.recon
    assert recon.provisional_dates == tuple(f"2026-10-{d}" for d in range(14, 20))
    for line in world.cost_lines:
        assert line.finality == ("provisional" if line.date_utc in recon.provisional_dates
                                 else "final")
    for req in world.requests:
        assert world.since_ms <= req.ts_start_ms < world.until_ms


def test_every_team_has_plants_and_truth(world: F.FleetWorld) -> None:
    teams_with_plants = {p.team for p in world.truth.plants}
    assert teams_with_plants == {t.name for t in F.TEAMS} - {"core", "tiny"}
    expected = {
        ("platform", "cache.gateway-disabled", "no-cache"),
        ("payments", "cache.ttl-advisor", "ttl-1h-recommended"),
        ("search", "context.size-tax", "context-tax"),
        ("search", "context.compaction-window", "compaction-window"),
        ("mobile", "cache.cold-resume", "cold-resume"),
        ("infra", "premium.modifiers", "fast-premium"),
        ("infra", "premium.sticky-escalation", "sticky-escalation"),
        ("data", "model.routing", "delegation-routing"),
        ("data", "model.routing", "same-tier-upgrade"),
        ("data", "model.routing", "default-model"),
        ("data", "model.routing", "default-effort"),
        ("data", "model.routing", "rebaseline"),
        ("ops", "tail.runaway", "runaway-session"),
        ("ops", "premium.modifiers", "regional-premium"),
        ("ci-bots", "automation", "ci-cross-run"),
        ("ci-bots", "automation", "batch-eligible"),
        ("ci-bots", "automation", "ci-run-cost"),
        ("ci-bots", "failure.path", "max-tokens-truncation"),
        ("agents", "cache.ttl-advisor", "keepalive-recommended"),
        ("agents", "cache.rebuild", "edit-churn"),
        ("agents", "context.static-prefix", "tool-defs-bloat"),
    }
    assert {(p.team, p.detector_id, p.kind) for p in world.truth.plants} == expected
    keys = {lane.lane_key for lane in world.lanes()}
    for p in world.truth.plants:
        assert p.lane_keys and set(p.lane_keys) <= keys, p.plant_id
        for fig, tol in p.tolerances:
            assert p.expected(fig) is not None or fig == "delta_output_per_request"
            assert tol == "range" or Decimal(tol) >= 0
        assert world.truth.plant(p.plant_id) is p
    assert {c for c, _ in world.truth.expectations} == {"agents", "core", "tiny"}
    assert world.truth.plants_for(team="data", kind="default-model")[0].policy.startswith("model=")
    with pytest.raises(UsageError):
        world.truth.plant("nope")


def test_figures_are_meaningful(world: F.FleetWorld) -> None:
    p = world.truth.plant
    assert 0 < p("payments.ttl-1h").recoverable_nano < p("payments.ttl-1h").cost_observed_nano
    assert Fraction(p("payments.ttl-1h").detail("gap_share_5_60min")) > Fraction(1, 20)
    assert p("payments.ttl-1h").shapley_nano == p("payments.ttl-1h").recoverable_nano
    assert 0 < p("platform.no-cache").recoverable_nano < p("platform.no-cache").cost_observed_nano
    assert p("search.compaction-window").recoverable_nano > 0
    assert int(p("search.compaction-window").detail("added_compactions")) <= 3 * int(
        p("search.compaction-window").detail("sessions"))
    assert p("search.size-tax").basis == "list_equivalent"
    assert int(p("search.size-tax").detail("max_context")) <= 900_000
    for suffix in ("allowance", "overage"):
        cr = p(f"mobile.cold-resume.{suffix}")
        assert 0 < cr.recoverable_nano < cr.cost_observed_nano
    assert p("mobile.cold-resume.allowance").basis == "list_equivalent"
    assert p("mobile.cold-resume.overage").basis == "list"
    fast = p("infra.fast-premium")
    assert fast.cost_observed_nano == fast.recoverable_nano > 0
    assert sum(int(v) for k, v in fast.details if k.startswith("self:")) == fast.recoverable_nano
    assert p("ops.regional-premium").recoverable_nano > 0
    run = p("ops.runaway")
    assert int(run.detail("rolling_1h_max_nano")) > int(run.detail("threshold_nano"))
    assert int(run.detail("sessions")) >= 100
    trunc = p("ci-bots.truncation")
    assert 0 < trunc.recoverable_nano <= trunc.cost_observed_nano
    batch = p("ci-bots.batch-eligible")
    assert batch.recoverable_nano * 2 == batch.cost_observed_nano     # 0.5× on uncached/output
    ka = p("agents.keepalive")
    assert ka.recoverable_nano > int(ka.detail("ttl_1h_saving_nano")) > 0
    tb = p("agents.tool-defs-bloat")
    for d in ("d_physical_nano", "d_proportional_nano"):
        band = int(tb.detail(d))
        assert tb.within("recoverable", 0, low=band // 2, high=band * 85 // 100)
    churn = p("agents.edit-churn")
    assert 0 < churn.recoverable_nano <= churn.cost_observed_nano
    rb = p("data.rebaseline")
    assert Decimal(rb.detail("delta_output_per_request")) > 0
    assert Decimal("1.25") < Decimal(rb.detail("migrated_output_ratio")) < Decimal("1.45")
    assert rb.detail("change_date") == "2026-10-06"
    for plant in world.truth.plants_for(kind="same-tier-upgrade"):
        assert 0 < plant.recoverable_nano < plant.cost_observed_nano


def test_billing_paths_channels_and_scopes(world: F.FleetWorld) -> None:
    per_team: dict[str, Counter] = {}
    overage_days = {F._date_add(world.window_start, d) for d in world.hints.overage_days}
    for req in world.requests:
        team = req.attribution.team
        inf = req.serving_inference or req.attempts[0].inferences[0]
        per_team.setdefault(team, Counter())[(inf.pricing.channel, req.attribution.billing_path,
                                              inf.pricing.endpoint_scope)] += 1
        if team == "mobile":
            date = F.date_of(req.ts_start_ms)
            assert req.attribution.billing_path == ("usage_credits" if date in overage_days
                                                   else "subscription")
    assert set(per_team["search"]) == {("anthropic_api", "subscription", "unknown")}
    assert set(per_team["ops"]) == {("bedrock", "bedrock", "regional")}
    for team in ("platform", "payments", "infra", "data", "ci-bots", "agents", "core", "tiny"):
        assert {k[1] for k in per_team[team]} == {"api_key"}, team
    quota = [e for e in world.events if e.kind is LaneEventKind.QUOTA_STATE]
    assert quota and all(dict(e.attrs)["using_overage"] is True for e in quota)
    assert {F.date_of(e.ts_ms) for e in quota} == overage_days
    for lane in world.lanes("ops"):
        assert lane.cache_scope_key == "org:bedrock:111122223333"


def test_control_team_is_clean(world: F.FleetWorld) -> None:
    pricer, rules = FakePricer(), RulesTable()
    core_lanes = world.lanes("core")
    assert core_lanes
    for lane in core_lanes:
        for t in classify_transitions(lane, pricer=pricer, rules=rules):
            assert not t.is_miss_event, (lane.lane_key, t)
        for req in lane.requests:
            inf = req.serving_inference
            assert inf is not None and inf.pricing.model == "claude-sonnet-5"
            assert inf.usage.total_input < 150_000
            assert req.params.effort == "medium" and inf.pricing.speed == "standard"
        assert len(lane.requests) >= 2
    starts = sorted((ln.requests[0].ts_start_ms, ln.requests[0].attribution.cwd_key)
                    for ln in core_lanes)
    for (a, ca), (b, cb) in pairwise(starts):
        assert b - a > 10_000 or ca != cb        # no cold fan-out


def test_plant_lanes_follow_the_documented_rules(world: F.FleetWorld) -> None:
    pricer, rules = FakePricer(), RulesTable()
    causes: dict[str, Counter] = {}
    for lane in world.lanes():
        for t in classify_transitions(lane, pricer=pricer, rules=rules):
            if t.is_miss_event:
                causes.setdefault(lane.team or "", Counter())[t.cause] += 1
            assert not t.ambiguous
    assert set(causes["payments"]) == {"ttl-expiry"}
    assert set(causes["mobile"]) == {"ttl-expiry"}
    assert set(causes["agents"]) == {"ttl-expiry", "compaction"}
    assert "search" not in causes and "platform" not in causes and "core" not in causes
    # mobile cold resumes: idle 1–8 h at 400–600k context
    for lane in world.lanes("mobile"):
        for a, b in pairwise(lane.requests):
            gap = b.ts_start_ms - a.ts_start_ms
            if gap > 3_600_000:
                assert 3_600_000 < gap <= 8 * 3_600_000
                prefix = a.serving_inference.usage.cache_read + \
                    a.serving_inference.usage.cache_write
                assert 400_000 <= prefix <= 624_000


def test_team_specific_shapes(world: F.FleetWorld) -> None:
    hints = world.hints
    infra = world.lanes("infra")
    fast_devs = {hints.devs[f"infra-0{i}"].principal for i in (0, 1)}
    for lane in infra:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is None or lane.kind is not LaneKind.MAIN:
                continue
            assert (inf.pricing.speed == "fast") == (req.attribution.principal in fast_devs)
            if req.attribution.principal == hints.devs["infra-02"].principal:
                assert req.params.effort == req.params.session_effort == "xhigh"
    fast_days = {F.date_of(ln.requests[0].ts_start_ms) for ln in infra
                 if ln.requests[0].attribution.principal in fast_devs}
    assert len(fast_days) > 5
    kinds = Counter(inf.kind.value for q in world.requests for a in q.attempts
                    for inf in a.inferences if q.attribution.team == "infra")
    assert kinds["fallback_declined"] == 2 and kinds["compaction"] == 1
    declined = sorted(inf.usage.output for q in world.requests for a in q.attempts
                      for inf in a.inferences if inf.kind.value == "fallback_declined")
    assert declined == [0, 6]
    sources = Counter(inf.usage_source.value for q in world.requests for a in q.attempts
                      for inf in a.inferences)
    assert sources["message_start_only"] == 1 and sources["estimated"] == 1
    assert world.truth.recon.unpriced_models == ("claude-sonnet-5-5",)
    comp = [e for e in world.events if e.kind is LaneEventKind.COMPACTION]
    assert comp and all(dict(e.attrs)["post_tokens"] == F.COMPACTION_POST_TOKENS for e in comp)
    # data: migration day, +35% output per call on the default devs, workflow agents on Opus 5
    mig = world.since_ms + hints.migration_day * DAY_MS
    for lane in world.lanes("data"):
        model = lane.requests[0].model
        if lane.kind is LaneKind.WORKFLOW_AGENT:
            assert model == "claude-opus-5"
        elif lane.requests[0].attribution.principal in {hints.devs[f"data-0{i}"].principal
                                                        for i in range(4)}:
            assert model == ("claude-opus-4-8" if lane.requests[0].ts_start_ms < mig
                             else "claude-opus-5")
        else:
            assert model == "claude-fable-5"
    priority = {F.date_of(q.ts_start_ms) for q in world.requests
                if q.serving_inference and q.serving_inference.pricing.service_tier == "priority"}
    assert priority == {"2026-09-22", "2026-09-23"}
    # ops: one runaway loop of 400 requests over 3 h without a human prompt after the first
    run = [ln for ln in world.lanes("ops") if ln.session_key == hints.runaway_session]
    assert len(run) == 1 and len(run[0].requests) == 400
    span = run[0].requests[-1].ts_start_ms - run[0].requests[0].ts_start_ms
    assert 3 * 3_600_000 - 30_000 <= span <= 3 * 3_600_000
    assert sum(e.kind is LaneEventKind.HUMAN_PROMPT for e in run[0].events) == 1
    # ci-bots: ~15% of steps truncated at 16,384, CLI versions drift across runs
    ci = world.lanes("ci-bots")
    steps = [q for ln in ci if ln.kind is LaneKind.MAIN for q in ln.requests]
    trunc = [q for q in steps if q.final_attempt.stop_reason == "max_tokens"]
    assert Fraction(len(trunc), len(steps)) > Fraction(8, 100)
    assert all(q.serving_inference.usage.output == 16_384 for q in trunc)
    assert {q.attribution.client_version for q in steps} == {"2.1.268", "2.1.270", "2.1.271"}
    assert all(q.params.max_tokens == 16_384 for q in steps)
    # agents: 7-minute idles, 14k tokens of non-deferred tool definitions on every request
    gaps = [b.ts_start_ms - a.ts_start_ms for ln in world.lanes("agents")
            for a, b in pairwise(ln.requests)]
    assert any(390_000 <= g <= 451_000 for g in gaps)
    assert not any(240_000 < g <= 300_000 or 450_999 < g < 3_600_000 for g in gaps
                   if not 390_000 <= g <= 451_000)
    for ln in world.lanes("agents"):
        for q in ln.requests:
            tools = [b for b in q.fingerprint.blocks if b.tier == "tools"]
            assert sum(b.est_tokens for b in tools) == 14_000 and not any(b.deferred
                                                                          for b in tools)
            assert sum(b.est_tokens for b in q.fingerprint.blocks) == \
                q.serving_inference.usage.total_input


def test_canonical_records_are_content_free(world: F.FleetWorld) -> None:
    blobs = [json.dumps(to_json(r)) for r in world.requests[:2_000]]
    blobs += [json.dumps(to_json(r)) for r in (*world.sessions, *world.events,
                                                 *world.aggregates, *world.cost_lines,
                                                 *world.outcomes)]
    blobs.append(repr(world)[:10_000])
    blobs.append(repr(world.truth))
    assert_no_canary(*blobs)
    assert CANARY in world.hints.devs["infra-00"].cwd        # only the writer hints carry it


def test_store_round_trip_and_spend(world: F.FleetWorld) -> None:
    store = MemoryStore(org_key=F.FLEET_ORG_KEY, name_key_id=key_id(F.FLEET_NAME_KEY),
                        pricer=FakePricer())
    counts = store.ingest(world.ingest_result())
    assert counts["requests"] == len(world.requests)
    assert counts["dq.name_key_mismatch"] == 0 and counts["principals_pseudonymized"] == 0
    rows = store.cost_rows(since_ms=world.since_ms, until_ms=world.until_ms,
                           group_by=["team", "billing_path"])
    by_team: dict[str, list[int]] = {}
    for row in rows:
        slot = by_team.setdefault(row.team, [0, 0, 0])
        slot[1 if row.basis.value == "list_equivalent" else 0] += row.priced_nano
        slot[2] += row.estimated_high_nano
    for team in world.truth.teams:
        exact, allowance, ranged = by_team.get(team.team, [0, 0, 0])
        assert allowance == team.spend_allowance_nano, team.team
        if team.team == "infra":     # placeholder, hidden compaction, refusal: range lines
            assert exact < team.spend_billed_nano <= exact + ranged
        else:
            assert (exact, ranged) == (team.spend_billed_nano, 0), team.team
    recon = world.truth.recon
    assert recon.seat_allowance_nano == sum(t.spend_allowance_nano for t in world.truth.teams)


def test_provider_records_and_reconciliation_truth(world: F.FleetWorld) -> None:
    recon = world.truth.recon
    assert dict(recon.contract_multipliers) == {"anthropic_api": "0.85", "bedrock": "0.90"}
    invoice = dict(recon.invoice_nano)
    listed = dict(recon.provider_list_nano)
    for channel, mult in (("anthropic_api", Decimal("0.85")), ("bedrock", Decimal("0.90"))):
        lines = [c for c in world.cost_lines if c.channel == channel]
        assert abs(invoice[channel] - mult * listed[channel]) <= len(lines)
    assert recon.priority_list_nano > 0 and recon.seat_allowance_nano > 0
    assert recon.overage_dates == ("2026-09-30", "2026-10-01")
    # the usage report is the provider's view of billed first-party usage
    billed = [q for q in world.requests
              if (q.serving_inference or q.attempts[0].inferences[0]).pricing.channel ==
              "anthropic_api" and billing_class(q.attribution.billing_path) == "billed"]
    report = T.TokenTotals()
    for agg in world.aggregates:
        if agg.source_kind == "anthropic.usage_report":
            report = report + T.TokenTotals.of(agg.usage)
    assert report == T.token_totals(billed, provider=True)
    assert all(c.model != "claude-sonnet-5-5" or c.date_utc in recon.provisional_dates
               for c in world.cost_lines)
    tiers = {dict(a.dims)["service_tier"] for a in world.aggregates
             if a.source_kind == "anthropic.usage_report"}
    assert tiers == {"standard", "priority"}
    # analytics: team-level only, k ≥ 5, the tiny team never alone
    for out in world.outcomes:
        assert out.n_users >= 5 and out.team != "tiny"
    for agg in world.aggregates:
        if agg.source_kind == "anthropic.cc_analytics":
            assert dict(agg.dims)["team"] != "tiny"
            assert agg.reported_cost_basis == "provider_estimate"
    cur = [c for c in world.cost_lines if c.source_kind == "aws.cur2"]
    assert cur and all(c.principal and c.principal.startswith("p_") for c in cur)
    assert all(c.list_amount_nano and c.amount_nano < c.list_amount_nano for c in cur)


def test_in_process_determinism_and_other_seeds() -> None:
    a = F.generate(seed=11, days=7)
    b = F.generate(seed=11, days=7)
    dump = [json.dumps(to_json(r), sort_keys=True) for r in a.requests]
    assert dump == [json.dumps(to_json(r), sort_keys=True) for r in b.requests]
    assert a.truth.plants == b.truth.plants
    assert a.today == "2026-10-23"
    c = F.generate(seed=12, days=7)
    assert [q.request_id for q in c.requests] != [q.request_id for q in a.requests]
    big = F.generate(seed=11, days=7, devs=66)
    assert len(big.truth.team("payments").principals) == 7 + 1


def test_ingest_options_and_world_helpers(world: F.FleetWorld) -> None:
    opts = F.fleet_ingest_options(now_ms=5)
    assert opts.name_key_id == key_id(F.FLEET_NAME_KEY) and opts.now_ms == 5
    assert "name_key" not in repr(opts) or F.FLEET_NAME_KEY.hex() not in repr(opts)
    assert len(world.lanes("tiny")) == world.truth.team("tiny").lanes
    shells = [ln for s in world.sessions for ln in s.lanes]
    assert all(not ln.requests and not ln.events for ln in shells)
    assert len(shells) == len(world.lanes())
    assert dataclasses.replace(world, source_files={}).truth is world.truth
    assert F.model_display("claude-opus-5-5") == "Claude Opus 5.5"
