"""Merge gate 1 (SPEC Appendix G.1, PLAN §1.5): every written source file read by the **real**
adapter reproduces the canonical records' token totals per team (and the CUR / cost-report
invoice totals the canonical cost lines) — and, where the SPEC fixes the mapping, the records
themselves: request ids, timestamps and usage of the Claude Code files (§5.3, §5.12), the OTLP
events (§5.9), and every provider-side aggregate / cost line / outcome (§5.11, §5.13) with its
workspace, dims and finality. Each family skips (``importorskip``) until its adapter package (CC,
TELEM, TRACE, ADMIN) is merged."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from tokenbill.core.records import (
    Attribution,
    CostLine,
    InferenceKind,
    LaneEventKind,
    LaneKind,
    Request,
    UsageAggregate,
    WorkloadClass,
)
from tokenbill.core.registry import get_adapter
from tokenbill.core.types import IngestResult
from tokenbill.synth.fleet import FleetWorld
from tokenbill.synth.truth import TokenTotals, token_totals
from tokenbill.synth.writers import write_cc_transcripts

pytestmark = pytest.mark.gate


def _read(world: FleetWorld, adapter_name: str, family: str, **opts_kw: object
          ) -> list[IngestResult]:
    adapter = get_adapter(adapter_name)
    source = world.truth.source(family)
    opts = world.ingest_options(**opts_kw)
    return [adapter.read(world.source_files[key], opts) for key in source.keys
            if not key.endswith(".meta.json")]


def _requests(results: Iterable[IngestResult]) -> list[Request]:
    seen: dict[str, Request] = {}
    for res in results:
        for req in res.requests:
            seen.setdefault(req.request_id, req)
        for session in res.sessions:
            for lane in session.lanes:
                for req in lane.requests:
                    seen.setdefault(req.request_id, req)
    return list(seen.values())


def _canonical(world: FleetWorld, family: str) -> dict[str, Request]:
    sample = set(world.truth.source(family).session_keys)
    return {q.request_id: q for q in world.requests if q.session_key in sample}


def _inputs(req: Request) -> tuple[int, int, int, int]:
    u = req.serving_inference.usage   # type: ignore[union-attr]
    return u.uncached_input, u.cache_read, u.cache_write_5m, u.cache_write_1h


def test_claude_code_transcripts(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.claude_code")
    results = _read(world, "claude-code", "claude-code", identity_mode="install",
                    attribution=Attribution(team="infra", billing_path="api_key",
                                            workspace_id=world.hints.workspaces["infra"]))
    requests = _requests(results)
    expected = dict(world.truth.source("claude-code").totals)["infra"]
    assert token_totals(requests) == expected
    # §5.3 #7/#11: request ids from message ids, session and lane keys from the session id;
    # #4: the request starts at its trigger (the preceding user entry); usage per request
    canonical = _canonical(world, "claude-code")
    got = {q.request_id: q for q in requests}
    hidden = {rid for rid, q in canonical.items() if q.serving_inference is None}
    assert set(canonical) - hidden <= set(got)
    boundaries = [(e.lane_key, e.ts_ms) for e in world.events
                  if e.kind is LaneEventKind.COMPACTION]
    after_compaction = {min((q for q in canonical.values()
                             if q.lane_key == lane_key and q.ts_start_ms > ts),
                            key=lambda q: q.ts_start_ms).request_id
                        for lane_key, ts in boundaries
                        if any(q.lane_key == lane_key for q in canonical.values())}
    for rid, want in canonical.items():
        if rid in hidden:
            continue
        have = got[rid]
        assert (have.session_key, have.lane_key, have.ts_start_ms) == (
            want.session_key, want.lane_key, want.ts_start_ms), rid
        assert _inputs(have) == _inputs(want), rid
        assert [inf.kind for a in have.attempts for inf in a.inferences] == [
            inf.kind for a in want.attempts for inf in a.inferences], rid
        if rid not in after_compaction:     # whether a compact summary is appended input is
            assert have.appended == want.appended, rid     # the importer's call (§5.3 #8/#9)


def test_headless_execution_files(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.cc_headless")
    results = _read(world, "claude-code-headless", "claude-code-headless",
                    identity_mode="install",
                    attribution=Attribution(team="ci-bots", billing_path="api_key",
                                            workload_class=WorkloadClass.CI,
                                            entrypoint="claude-code-github-action"))
    requests = _requests(results)
    expected = dict(world.truth.source("claude-code-headless").totals)["ci-bots"]
    assert token_totals(requests) == expected
    # §5.12 #2: timed streams (every message carries a timestamp); steps keep their ids, keys,
    # start times and input usage; the per-step output placeholders are restored by one
    # OUTPUT_RESIDUAL request per run (#3)
    canonical = _canonical(world, "claude-code-headless")
    got = {q.request_id: q for q in requests}
    assert set(canonical) <= set(got)
    for rid, want in canonical.items():
        have = got[rid]
        assert (have.session_key, have.lane_key, have.ts_start_ms) == (
            want.session_key, want.lane_key, want.ts_start_ms), rid
        assert _inputs(have) == _inputs(want), rid
    extra = [q for rid, q in got.items() if rid not in canonical]
    assert extra and all(inf.kind is InferenceKind.OUTPUT_RESIDUAL
                         for q in extra for a in q.attempts for inf in a.inferences)
    assert {q.session_key for q in extra} <= {q.session_key for q in canonical.values()}
    since, until = world.since_ms, world.until_ms
    assert all(since <= q.ts_start_ms < until for q in requests)


def test_otlp_logs(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.otel")
    results = _read(world, "otlp", "otlp", attribution=Attribution(billing_path="api_key"))
    requests = _requests(results)
    expected = dict(world.truth.source("otlp").totals)["core"]
    assert token_totals(requests).merged_writes() == expected
    # §5.9: one request per api_request event, ts_start = timeUnixNano − duration_ms, the
    # principal from user.id through the org key, the team through the team map
    canonical = {q.final_attempt.provider_request_id: q
                 for q in _canonical(world, "otlp").values()}
    assert len(requests) == len(canonical)
    for have in requests:
        want = canonical[have.final_attempt.provider_request_id]
        assert (have.ts_start_ms, have.final_attempt.duration_ms, have.model) == (
            want.ts_start_ms, want.final_attempt.duration_ms, want.model)
        assert (have.attribution.principal, have.attribution.team) == (
            want.attribution.principal, want.attribution.team)
        assert TokenTotals.of(have.serving_inference.usage).merged_writes() == \
            TokenTotals.of(want.serving_inference.usage).merged_writes()   # type: ignore[union-attr]


def test_trace_v2_fingerprint_file(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.trace_v2")
    results = _read(world, "trace@2", "trace@2")
    requests = _requests(results)
    expected = dict(world.truth.source("trace@2").totals)["agents"]
    assert token_totals(requests) == expected
    canonical = {q.request_id: q for q in world.requests if q.attribution.team == "agents"}
    assert {q.request_id for q in requests} == set(canonical)
    for have in requests:
        want = canonical[have.request_id]
        assert have.fingerprint is not None and want.fingerprint is not None
        assert [b.h for b in have.fingerprint.blocks] == [b.h for b in want.fingerprint.blocks]
        assert (have.session_key, have.lane_key, have.seq, have.ts_start_ms) == (
            want.session_key, want.lane_key, want.seq, want.ts_start_ms)
        assert have.attribution == want.attribution and have.params == want.params
        assert have.appended == want.appended
        assert [a.applied_edits for a in have.attempts] == [a.applied_edits for a in want.attempts]


def _agg_totals(results: Iterable[IngestResult], source_kind: str) -> TokenTotals:
    total = TokenTotals()
    for res in results:
        for agg in res.aggregates:
            if agg.source_kind == source_kind:
                total = total + TokenTotals.of(agg.usage)
    return total


def _agg_rows(aggs: Iterable[UsageAggregate], source_kind: str) -> list[tuple]:
    return sorted(((a.bucket_start_ms, a.bucket_end_ms, a.dims, a.usage, a.reported_cost_nano,
                    a.finality) for a in aggs if a.source_kind == source_kind), key=repr)


def _cost_rows(lines: Iterable[CostLine], source_kind: str) -> list[tuple]:
    return sorted(((c.date_utc, c.channel, c.workspace_id, c.model, c.cost_type, c.token_type,
                    c.sku, c.description, c.amount_nano, c.list_amount_nano, c.principal,
                    c.finality) for c in lines if c.source_kind == source_kind), key=repr)


def test_admin_usage_and_cost_reports(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.anthropic_admin")
    usage = _read(world, "anthropic-usage-report", "anthropic-usage-report")
    assert _agg_totals(usage, "anthropic.usage_report") == dict(
        world.truth.source("anthropic-usage-report").totals)["all"]
    # every aggregate (workspace in clear, tier/speed/geo dims, finality vs today)
    assert _agg_rows((a for res in usage for a in res.aggregates),
                     "anthropic.usage_report") == _agg_rows(world.aggregates,
                                                            "anthropic.usage_report")
    cost = _read(world, "anthropic-cost-report", "anthropic-cost-report")
    lines = [c for res in cost for c in res.cost_lines]
    assert sum(c.amount_nano for c in lines) == world.truth.source(
        "anthropic-cost-report").cost_nano
    assert {c.channel for c in lines} == {"anthropic_api"}
    assert _cost_rows(lines, "anthropic.cost_report") == _cost_rows(world.cost_lines,
                                                                    "anthropic.cost_report")


def test_claude_code_analytics(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.anthropic_admin")
    results = _read(world, "anthropic-cc-analytics", "anthropic-cc-analytics", k_anonymity=5)
    got = _agg_totals(results, "anthropic.cc_analytics")
    assert got.merged_writes() == dict(world.truth.source("anthropic-cc-analytics").totals)["all"]
    teams = {o.team for res in results for o in res.outcomes}
    assert "tiny" not in teams
    for res in results:
        for agg in res.aggregates:
            assert "principal" not in dict(agg.dims)
    # per (date, team) after k-merging: tokens, provider-estimate cost, outcomes
    assert _agg_rows((a for res in results for a in res.aggregates),
                     "anthropic.cc_analytics") == _agg_rows(world.aggregates,
                                                            "anthropic.cc_analytics")
    assert sorted((o for res in results for o in res.outcomes), key=repr) == sorted(
        world.outcomes, key=repr)


def test_aws_cur(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.cloud_billing")
    results = _read(world, "aws-cur", "aws-cur")
    source = world.truth.source("aws-cur")
    lines = [c for res in results for c in res.cost_lines]
    assert sum(c.amount_nano for c in lines) == source.cost_nano
    assert {c.channel for c in lines} == {"bedrock"}
    tokens = sum(sum(TokenTotals.of(a.usage).as_tuple()) for res in results
                 for a in res.aggregates if a.source_kind == "aws.cur2")
    assert tokens == sum(dict(source.totals)["all"].as_tuple())
    # §5.13: account → h_ workspace, IAM principal → p_ (org key), usage type as sku
    assert _cost_rows(lines, "aws.cur2") == _cost_rows(world.cost_lines, "aws.cur2")


def _overage_and_workflow_sessions(world: FleetWorld) -> tuple[list[str], list[str]]:
    hints = world.hints
    overage = set(world.truth.recon.overage_dates)
    mobile = sorted(sk for sk, team in hints.session_team.items() if team == "mobile"
                    and any(q.session_key == sk and q.attribution.billing_path == "usage_credits"
                            for q in world.requests))
    assert mobile and overage
    workflow = sorted({ln.session_key for ln in world.lanes("data")
                       if ln.kind is LaneKind.WORKFLOW_AGENT})[:3]
    assert workflow
    return mobile[:4], workflow


def test_claude_code_transcripts_of_other_teams(world: FleetWorld, tmp_path: Path) -> None:
    """``write_cc_transcripts`` on any Claude Code session: the importer recovers the billing path
    (``usage_credits`` while ``quotaLimits.isUsingOverage``, §5.3 #8) and the lane kinds
    (``…/workflows/…`` → WORKFLOW_AGENT) of the canonical records."""
    pytest.importorskip("tokenbill.adapters.claude_code")
    mobile, workflow = _overage_and_workflow_sessions(world)
    kinds = {ln.lane_key: ln.kind for ln in world.lanes()}
    adapter = get_adapter("claude-code")
    for team, sessions, path in (("mobile", mobile, "subscription"),
                                 ("data", workflow, "api_key")):
        files = write_cc_transcripts(world, tmp_path / team, sessions)
        opts = world.ingest_options(identity_mode="install", attribution=Attribution(
            team=team, billing_path=path, workspace_id=world.hints.workspaces[team]))
        results = [adapter.read(p, opts) for k, p in files.items()
                   if not k.endswith(".meta.json")]
        got = {q.request_id: q for q in _requests(results)}
        shells = {ln.lane_key: ln.kind for res in results for s in res.sessions for ln in s.lanes}
        canonical = [q for q in world.requests if q.session_key in set(sessions)]
        assert canonical
        for want in canonical:
            have = got[want.request_id]
            assert have.attribution.billing_path == want.attribution.billing_path
            assert shells[have.lane_key] is kinds[want.lane_key]
