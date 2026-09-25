"""``gh-aw-token-usage`` acceptance tests (addendum §5.14; CP-OTEL brief)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tokenbill.adapters.gh_aw import GhAwTokenUsageAdapter
from tokenbill.core.ids import pseudonym
from tokenbill.core.records import (
    Attribution,
    LaneEventKind,
    LaneKind,
    Outcome,
    WorkloadClass,
    billing_class,
)
from tokenbill.core.testing import CONFORMANCE_NAME_KEY, FakePricer

from .helpers import GH_AW, GH_AW_MIXED, T0_MS, VSCODE_DUMP, codes, inference, no_leak, opts

ADAPTER = GhAwTokenUsageAdapter(env={})
CREDITS = {"req-0001": 12_345_670, "req-0002": 123_450, "req-0003": 421_000,
           "req-0004": 15_000_000, "req-0005": 9_000_000}


def _by_rid(result) -> dict[str, object]:
    return {r.attempts[0].provider_request_id: r for r in result.requests}


def test_five_lines_five_requests_with_exact_provider_estimates() -> None:
    result = ADAPTER.read(GH_AW, opts())
    reqs = _by_rid(result)
    assert set(reqs) == set(CREDITS)
    for rid, nano in CREDITS.items():
        inf = inference(reqs[rid])
        assert inf.provider_reported_cost_nano == nano
        assert inf.provider_reported_cost_basis == "provider_estimate"
        assert inf.billable is True
        assert (inf.pricing.provider, inf.pricing.channel) == ("github", "github_copilot")
        assert inf.pricing.billing_path == "copilot_direct"
        assert billing_class(inf.pricing.billing_path) == "pool"
    for req in result.requests:
        assert req.attribution.agent_product == "copilot_gh_aw"
        assert req.attribution.workload_class is WorkloadClass.CI
        assert req.attribution.billing_path == "copilot_direct"
        assert req.attempts[0].convention_id == "gh_aw.token_usage"
        assert req.attempts[0].outcome is Outcome.OK
    first = inference(reqs["req-0001"]).usage
    assert (first.uncached_input, first.cache_read, first.cache_write_unknown, first.output) == (
        2000, 9000, 1000, 800)
    third = inference(reqs["req-0003"]).usage          # flag absent: sum-check → inclusive
    assert (third.uncached_input, third.cache_read, third.output_reasoning) == (2952, 2048, 256)
    assert reqs["req-0001"].attribution.entrypoint == "/v1/messages"
    assert reqs["req-0004"].attribution.entrypoint is None
    assert result.stats["paths_dropped"] == 1
    assert reqs["req-0001"].attempts[0].ts_start_ms == T0_MS - 2000
    assert reqs["req-0001"].attempts[0].duration_ms == 2000
    assert reqs["req-0001"].params.stream is True
    assert codes(result)["dq.copilot_billing_path_assumed"] == 5
    assert result.capabilities == {"credits", "timing", "aggregates"}
    # one session, main lane
    (session,) = result.sessions
    (lane,) = session.lanes
    assert lane.kind is LaneKind.MAIN
    assert {r.lane_key for r in result.requests} == {lane.lane_key}
    no_leak(result)


def test_run_total_cost_state_and_aggregate() -> None:
    result = ADAPTER.read(GH_AW, opts())
    (event,) = result.events
    assert event.kind is LaneEventKind.COST_STATE
    assert dict(event.attrs) == {"reported_total_nano": 36_890_120, "reporter": "gh_aw.run_total"}
    assert event.ts_ms == T0_MS + 12000
    (agg,) = result.aggregates
    assert agg.source_kind == "gh_aw.run"
    assert agg.reported_cost_nano == 36_890_120
    assert agg.reported_cost_basis == "provider_estimate"
    dims = dict(agg.dims)
    assert dims["channel"] == "github_copilot" and dims["source"] == result.source.source_id
    assert "repo" not in dims and "workflow" not in dims
    total = sum((inference(r).usage for r in result.requests), start=agg.usage.__class__())
    assert agg.usage == total
    assert agg.fetched_ms == opts().now_ms
    assert (agg.bucket_start_ms, agg.bucket_end_ms) == (T0_MS - 2000, T0_MS + 12000)
    assert sum(CREDITS.values()) == agg.reported_cost_nano


def test_utility_model_stays_unpriced_and_aic_is_a_tool_estimate() -> None:
    result = ADAPTER.read(GH_AW, opts())
    req = _by_rid(result)["req-0002"]
    inf = inference(req)
    assert inf.pricing.model == "gpt-4o-mini-2024-07-18"
    priced = FakePricer().price_inference(inf, ts_ms=req.ts_start_ms)
    assert priced.figure.nano is None and priced.unpriced_reason
    assert inf.provider_reported_cost_nano == 123_450           # never used as a price


def test_mixed_file_skips_non_copilot_and_quarantines_bad_lines() -> None:
    result = ADAPTER.read(GH_AW_MIXED, opts())
    assert len(result.requests) == 5
    assert result.stats["non_copilot_lines"] == 1
    assert result.stats["other_events"] == 1
    reasons = sorted(q.reason for q in result.quarantined)
    assert reasons == ["bad_json", "bad_usage", "missing:timestamp"]
    no_leak(result)


def test_repo_and_workflow_from_env_and_attr() -> None:
    env = {"GITHUB_REPOSITORY": "acme/secret-repo", "GITHUB_WORKFLOW_REF":
           "acme/secret-repo/.github/workflows/triage.lock.yml@refs/heads/main"}
    result = GhAwTokenUsageAdapter(env=env).read(GH_AW, opts())
    repo = pseudonym(CONFORMANCE_NAME_KEY, "h", "acme/secret-repo")
    workflow = pseudonym(CONFORMANCE_NAME_KEY, "h", env["GITHUB_WORKFLOW_REF"])
    assert {r.attribution.repo for r in result.requests} == {repo}
    assert {dict(r.attribution.extra)["workflow"] for r in result.requests} == {workflow}
    dims = dict(result.aggregates[0].dims)
    assert (dims["repo"], dims["workflow"]) == (repo, workflow)
    assert "secret" not in repr(result)
    attribution = replace(Attribution(), repo="acme/other", billing_path="copilot_pool",
                          extra=(("workflow", "nightly"), ("copilot_compliance",
                                                           "data_residency")))
    stated = GhAwTokenUsageAdapter(env=env).read(GH_AW, opts(attribution=attribution))
    assert {r.attribution.repo for r in stated.requests} == {
        pseudonym(CONFORMANCE_NAME_KEY, "h", "acme/other")}
    assert {inference(r).pricing.billing_path for r in stated.requests} == {"copilot_pool"}
    assert {inference(r).pricing.compliance for r in stated.requests} == {"data_residency"}
    assert "dq.copilot_billing_path_assumed" not in codes(stated)
    keyless = GhAwTokenUsageAdapter(env=env).read(GH_AW, opts(name_key=b"", name_key_id=""))
    assert {r.attribution.repo for r in keyless.requests} == {None}
    assert keyless.stats["names_dropped_no_key"] >= 1


def test_status_duplicates_window_and_credits(tmp_path: Path) -> None:
    lines = GH_AW.read_text().splitlines()
    bad_status = lines[0].replace('"status":200', '"status":429').replace(
        '"req-0001"', '"req-9"').replace('"ai_credits_this_response":1.234567',
                                         '"ai_credits_this_response":"oops"')
    no_status = lines[1].replace('"status":200,', '').replace('"req-0002"', '"req-10"').replace(
        '"ai_credits_total":1.246912', '"ai_credits_total":-1')
    path = tmp_path / "token-usage.jsonl"
    path.write_text("\n".join([lines[0], lines[0], bad_status, no_status]) + "\n")
    result = ADAPTER.read(path, opts())
    reqs = _by_rid(result)
    assert set(reqs) == {"req-0001", "req-9", "req-10"}
    assert result.stats["duplicate_lines"] == 1
    assert reqs["req-9"].attempts[0].outcome is Outcome.HTTP_ERROR
    assert reqs["req-9"].attempts[0].http_status == 429
    assert inference(reqs["req-9"]).provider_reported_cost_nano is None
    assert reqs["req-10"].attempts[0].outcome is Outcome.UNKNOWN
    assert result.stats["bad_credits"] == 2
    windowed = ADAPTER.read(GH_AW, opts(since_ms=T0_MS + 5000))
    assert len(windowed.requests) == 3 and windowed.stats["out_of_window"] == 2
    empty = ADAPTER.read(GH_AW, opts(until_ms=T0_MS - 10))
    assert empty.requests == [] and empty.aggregates == [] and empty.sessions == []
    assert empty.capabilities == frozenset()


def test_sniff(tmp_path: Path) -> None:
    assert ADAPTER.sniff(GH_AW, GH_AW.read_bytes()[:65536]) is True
    assert ADAPTER.sniff(VSCODE_DUMP, VSCODE_DUMP.read_bytes()[:65536]) is False
    assert ADAPTER.sniff(GH_AW, b"") is False
    assert ADAPTER.sniff(GH_AW, b'{"_schema":"token-usage/v1","event":"start"}\n') is False
    assert ADAPTER.sniff(GH_AW, b"\n\n not json") is False


def test_default_env_is_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/from-env")
    monkeypatch.delenv("GITHUB_WORKFLOW_REF", raising=False)
    monkeypatch.setenv("GITHUB_WORKFLOW", "Triage")
    result = GhAwTokenUsageAdapter().read(GH_AW, opts())
    assert {r.attribution.repo for r in result.requests} == {
        pseudonym(CONFORMANCE_NAME_KEY, "h", "acme/from-env")}
    assert {dict(r.attribution.extra)["workflow"] for r in result.requests} == {
        pseudonym(CONFORMANCE_NAME_KEY, "h", "Triage")}
