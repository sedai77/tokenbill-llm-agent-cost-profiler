"""Merge gate 1 (SPEC Appendix G.1, PLAN §1.5): every written source file read by the **real**
adapter reproduces the canonical records' token totals per team (and the CUR / cost-report
invoice totals the canonical cost lines). Each family skips (``importorskip``) until its adapter
package (CC, TELEM, TRACE, ADMIN) is merged."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable

import pytest

from tokenbill.core.records import Attribution, Request, WorkloadClass
from tokenbill.core.registry import get_adapter
from tokenbill.core.types import IngestResult
from tokenbill.synth.fleet import FleetWorld, fleet_ingest_options
from tokenbill.synth.truth import TokenTotals, token_totals

pytestmark = pytest.mark.gate


def _now_ms(world: FleetWorld) -> int:
    return (_dt.date.fromisoformat(world.today) - _dt.date(1970, 1, 1)).days * 86_400_000


def _read(world: FleetWorld, adapter_name: str, family: str, **opts_kw: object
          ) -> list[IngestResult]:
    adapter = get_adapter(adapter_name)
    source = world.truth.source(family)
    opts = fleet_ingest_options(now_ms=_now_ms(world), team_map=world.truth.team_map, **opts_kw)
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


def test_claude_code_transcripts(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.claude_code")
    results = _read(world, "claude-code", "claude-code", identity_mode="install",
                    attribution=Attribution(team="infra", billing_path="api_key"))
    expected = dict(world.truth.source("claude-code").totals)["infra"]
    assert token_totals(_requests(results)) == expected


def test_headless_execution_files(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.cc_headless")
    results = _read(world, "claude-code-headless", "claude-code-headless",
                    identity_mode="install",
                    attribution=Attribution(team="ci-bots", billing_path="api_key",
                                            workload_class=WorkloadClass.CI,
                                            entrypoint="claude-code-github-action"))
    expected = dict(world.truth.source("claude-code-headless").totals)["ci-bots"]
    assert token_totals(_requests(results)) == expected


def test_otlp_logs(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.otel")
    results = _read(world, "otlp", "otlp", attribution=Attribution(billing_path="api_key"))
    expected = dict(world.truth.source("otlp").totals)["core"]
    assert token_totals(_requests(results)).merged_writes() == expected


def test_trace_v2_fingerprint_file(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.trace_v2")
    results = _read(world, "trace@2", "trace@2")
    requests = _requests(results)
    expected = dict(world.truth.source("trace@2").totals)["agents"]
    assert token_totals(requests) == expected
    canonical = {q.request_id for q in world.requests if q.attribution.team == "agents"}
    assert {q.request_id for q in requests} == canonical
    assert all(q.fingerprint is not None for q in requests)


def _agg_totals(results: Iterable[IngestResult], source_kind: str) -> TokenTotals:
    total = TokenTotals()
    for res in results:
        for agg in res.aggregates:
            if agg.source_kind == source_kind:
                total = total + TokenTotals.of(agg.usage)
    return total


def test_admin_usage_and_cost_reports(world: FleetWorld) -> None:
    pytest.importorskip("tokenbill.adapters.anthropic_admin")
    usage = _read(world, "anthropic-usage-report", "anthropic-usage-report")
    assert _agg_totals(usage, "anthropic.usage_report") == dict(
        world.truth.source("anthropic-usage-report").totals)["all"]
    cost = _read(world, "anthropic-cost-report", "anthropic-cost-report")
    lines = [c for res in cost for c in res.cost_lines]
    assert sum(c.amount_nano for c in lines) == world.truth.source(
        "anthropic-cost-report").cost_nano
    assert {c.channel for c in lines} == {"anthropic_api"}


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
