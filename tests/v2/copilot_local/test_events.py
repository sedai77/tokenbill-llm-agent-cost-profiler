"""Events-only acceptance (CP-LOCAL brief): the two-session fixture, recovery, resume legs,
unclean shutdowns, privacy and the file policy."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tokenbill.adapters.copilot_cli import (
    CopilotCliAdapter,
    copilot_home,
    iter_events_files,
    never_opened,
    parse_event_line,
    parse_ts_ms,
    session_ids,
)
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import copilot_lane_key, copilot_session_key, key_id, pseudonym
from tokenbill.core.records import (
    Attribution,
    InferenceKind,
    LaneEventKind,
    LaneKind,
    UsageSource,
    to_json,
)
from tokenbill.core.testing import CONFORMANCE_NAME_KEY, FakePricer

from .helpers import (
    G11_DETAILS,
    G11_TOTAL_NANO_AIU,
    HOME,
    SID_A,
    SID_B,
    T0,
    Events,
    blob,
    notes_by_code,
    opts,
    session_file,
    usage,
)

ADAPTER = CopilotCliAdapter()


@pytest.fixture(scope="module")
def fixture_result():
    return ADAPTER.read(HOME, opts())


def _requests(result, session_id: str):
    sk = copilot_session_key(session_id)
    return [r for r in result.requests if r.session_key == sk]


def _events(result, kind: LaneEventKind):
    return [e for e in result.events if e.kind is kind]


def test_sessions_lanes_and_keys(fixture_result) -> None:
    sk_a, sk_b = copilot_session_key(SID_A), copilot_session_key(SID_B)
    sessions = {s.session_key: s for s in fixture_result.sessions}
    assert set(sessions) == {sk_a, sk_b}
    lanes_a = {lane.lane_key: lane for lane in sessions[sk_a].lanes}
    main_a = copilot_lane_key(sk_a, "main", None)
    sub_a = copilot_lane_key(sk_a, "subagent", "agent-1")
    assert lanes_a[main_a].kind is LaneKind.MAIN and lanes_a[main_a].parent_lane_key is None
    assert lanes_a[sub_a].kind is LaneKind.SUBAGENT and lanes_a[sub_a].parent_lane_key == main_a
    assert all(lane.cache_scope_key == "unknown" and lane.lane_exact for lane in lanes_a.values())
    lanes_b = {lane.kind for lane in sessions[sk_b].lanes}
    assert lanes_b == {LaneKind.MAIN, LaneKind.COMPACTION}
    assert sessions[sk_a].source_kind == "copilot-cli"
    assert sessions[sk_a].started_ms == T0 + 1000


def test_output_only_requests_and_chunk_grouping(fixture_result) -> None:
    reqs = _requests(fixture_result, SID_A)
    by_api = {r.final_attempt.provider_message_id: r for r in reqs}
    assert set(by_api) == {"chatcmpl-A1", "chatcmpl-A2", "chatcmpl-S1", "chatcmpl-A3"}
    a1 = by_api["chatcmpl-A1"]
    inf = a1.final_attempt.inferences[0]
    assert inf.kind is InferenceKind.MESSAGE and inf.usage_source is UsageSource.FINAL
    assert inf.usage.output == 120 and inf.usage.total_input == 0      # max, not the chunk sum
    assert a1.final_attempt.provider_request_id == "GH:0003"   # first chunk's requestId
    assert a1.request_id == ADAPTER_REQUEST_ID("chatcmpl-A1")
    assert a1.source.priority == 39 and int(a1.source.fidelity) == 2   # NO_TTL_SPLIT
    assert a1.attribution.agent_product == "copilot_cli"
    assert a1.params.effort == "high"


def ADAPTER_REQUEST_ID(api: str) -> str:  # noqa: N802 - mirrors core.ids.request_id_for
    from tokenbill.core.ids import request_id_for

    return request_id_for("github", api, "", "")


def test_pricing_context_per_request(fixture_result) -> None:
    reqs = {r.final_attempt.provider_message_id: r for r in _requests(fixture_result, SID_A)}
    ctx = reqs["chatcmpl-A1"].final_attempt.inferences[0].pricing
    assert (ctx.provider, ctx.channel, ctx.model, ctx.routing) == (
        "github", "github_copilot", "claude-sonnet-4-5", "direct")
    assert ctx.billing_path == "copilot_pool" and ctx.compliance is None
    assert ctx.context_tier == "long_context"                          # contextTier → requests
    assert ctx.write_ttl_hint is None                                   # before the checkpoint
    after = reqs["chatcmpl-A2"].final_attempt.inferences[0].pricing
    assert after.write_ttl_hint == "5m"                                 # 300 s TTL
    assert reqs["chatcmpl-A3"].final_attempt.inferences[0].pricing.model == "gpt-5.4"
    auto = _requests(fixture_result, SID_B)
    msg = [r for r in auto if r.final_attempt.inferences[0].kind is InferenceKind.MESSAGE]
    assert {r.final_attempt.inferences[0].pricing.routing for r in msg} == {"auto"}
    assert {r.final_attempt.inferences[0].pricing.context_tier for r in msg} == {"default"}


def test_subagent_lane(fixture_result) -> None:
    sub = copilot_lane_key(copilot_session_key(SID_A), "subagent", "agent-1")
    reqs = [r for r in fixture_result.requests if r.lane_key == sub]
    assert len(reqs) == 1 and reqs[0].attribution.query_source == "subagent"


def test_exact_compaction_inference_g11(fixture_result) -> None:
    comp = [r for r in _requests(fixture_result, SID_B)
            if r.final_attempt.inferences[0].kind is InferenceKind.COMPACTION]
    assert len(comp) == 1
    att = comp[0].final_attempt
    inf = att.inferences[0]
    assert (inf.usage.uncached_input, inf.usage.cache_read, inf.usage.cache_write_unknown,
            inf.usage.output) == (6, 127_386, 2_220, 6_210)
    assert inf.provider_reported_cost_nano == 232_848_000
    assert inf.provider_reported_cost_basis == "provider_estimate"
    assert inf.pricing.model == "claude-opus-4-7" and inf.pricing.routing == "direct"
    assert att.convention_id == "github_copilot.token_details" and att.duration_ms == 4_200
    assert "dq.copilot_nano_aiu_mismatch" not in notes_by_code(fixture_result)
    lane = copilot_lane_key(copilot_session_key(SID_B), "compaction", None)
    assert comp[0].lane_key == lane
    # the point at the Opus 4.7 rates reproduces the provider figure (Appendix C G11)
    priced = FakePricer().price_inference(inf, ts_ms=att.ts_start_ms)
    assert priced.figure.nano == 232_848_000 and priced.unpriced_reason is None


def test_compaction_event(fixture_result) -> None:
    (ev,) = _events(fixture_result, LaneEventKind.COMPACTION)
    attrs = dict(ev.attrs)
    assert attrs == {"trigger": "auto", "copilot_trigger": "threshold", "pre_tokens": 150_000,
                     "post_tokens": 20_000, "duration_ms": 4_200, "dropped_tokens": 130_000,
                     "system_tokens": 3_000, "tool_definitions_tokens": 9_000}
    assert ev.lane_key == copilot_lane_key(copilot_session_key(SID_B), "main", None)


def test_model_switch_truncation_error_checkpoint(fixture_result) -> None:
    (switch,) = _events(fixture_result, LaneEventKind.MODEL_SWITCH_USER)
    assert dict(switch.attrs) == {"from_model": "claude-sonnet-4-5", "to_model": "gpt-5.4"}
    (edit,) = _events(fixture_result, LaneEventKind.CONTEXT_EDIT)
    assert dict(edit.attrs) == {"edit_type": "copilot_truncation", "cleared_input_tokens": 4_000}
    (err,) = _events(fixture_result, LaneEventKind.API_ERROR)
    assert dict(err.attrs) == {"status": 429, "error_type": "rate_limit"}
    (cost,) = _events(fixture_result, LaneEventKind.COST_STATE)
    assert dict(cost.attrs) == {"reported_total_nano": 10_000_000,
                                "reporter": "copilot.cli.checkpoint"}
    metas = _events(fixture_result, LaneEventKind.SESSION_META)
    by_lane = {m.lane_key: dict(m.attrs) for m in metas}
    a = by_lane[copilot_lane_key(copilot_session_key(SID_A), "main", None)]
    assert a == {"routing_mode": "direct", "context_tier": "long_context",
                 "credit_limit_nano": 5_000_000_000}
    b = by_lane[copilot_lane_key(copilot_session_key(SID_B), "main", None)]
    assert b == {"routing_mode": "auto", "context_tier": "default"}
    assert len(_events(fixture_result, LaneEventKind.HUMAN_PROMPT)) == 2


def test_appended_sizes_only(fixture_result) -> None:
    reqs = {r.final_attempt.provider_message_id: r for r in _requests(fixture_result, SID_A)}
    (user,) = reqs["chatcmpl-A1"].appended
    assert user.kind == "user_text" and user.name is None and user.n_bytes > 100
    (tool,) = reqs["chatcmpl-A2"].appended
    assert tool.kind == "tool_result" and tool.name == pseudonym(CONFORMANCE_NAME_KEY, "h", "bash")
    assert not tool.is_error


def test_rollup_aggregates(fixture_result) -> None:
    aggs = {dict(a.dims)["model"]: a for a in fixture_result.aggregates}
    assert set(aggs) == {"claude-sonnet-4-5", "gpt-5.4", "claude-haiku-4-5"}
    sonnet = aggs["claude-sonnet-4-5"]
    assert sonnet.source_kind == "copilot.cli_rollup" and sonnet.finality == "final"
    assert (sonnet.usage.uncached_input, sonnet.usage.cache_read,
            sonnet.usage.cache_write_unknown, sonnet.usage.output) == (6, 10_069, 13_324, 250)
    assert sonnet.reported_cost_nano == 20_000_000
    assert sonnet.reported_cost_basis == "provider_estimate"
    assert dict(sonnet.dims)["convention"] == "github_copilot.shutdown_rollup"
    assert dict(sonnet.dims)["channel"] == "github_copilot"
    notes = notes_by_code(fixture_result)
    assert notes["dq.copilot_compaction_reset"] == 1     # session B compacted before its shutdown
    assert notes["dq.copilot_billing_path_assumed"] == 1


def test_capabilities_and_canary(fixture_result) -> None:
    assert fixture_result.capabilities == frozenset(
        {"timing", "events", "lanes_exact", "params", "credits", "appended"})
    assert "usage_sequence" not in fixture_result.capabilities
    assert_no_canary(blob(fixture_result))
    assert fixture_result.stats["ephemeral_skipped"] == 1
    assert fixture_result.quarantined == []


def test_deterministic(fixture_result) -> None:
    again = ADAPTER.read(HOME, opts())
    assert to_json(again) == to_json(fixture_result)


def test_single_file_and_session_dir_reads() -> None:
    one = ADAPTER.read(session_file(HOME, SID_A), opts())
    assert {r.session_key for r in one.requests} == {copilot_session_key(SID_A)}
    same = ADAPTER.read(session_file(HOME, SID_A).parent, opts())
    assert [r.request_id for r in same.requests] == [r.request_id for r in one.requests]
    state = ADAPTER.read(HOME / "session-state", opts())
    assert len({r.session_key for r in state.requests}) == 2


# ----------------------------------------------------------------------------- recovery, legs


def test_concatenated_line_recovered(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s1")
    good = ev.message("chatcmpl-X", 33)
    ev.objs.pop()
    import json

    truncated = b'{"type": "assistant.message", "data": {"content": "' + CANARY.encode() + b' tr'
    ev.raw(truncated + json.dumps(good).encode())
    path = ev.write(tmp_path / "session-state" / "s1" / "events.jsonl")
    result = ADAPTER.read(path, opts())
    assert notes_by_code(result)["dq.copilot_concatenated_line"] == 1
    assert [r.final_attempt.inferences[0].usage.output for r in result.requests] == [33]
    assert_no_canary(blob(result))


def test_two_complete_events_without_newline(tmp_path: Path) -> None:
    import json

    ev = Events()
    ev.start("s1")
    first = ev.message("c1", 5)
    second = ev.message("c2", 7)
    ev.objs[-2:] = []
    ev.raw(json.dumps(first).encode() + json.dumps(second).encode())
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    assert sorted(r.final_attempt.inferences[0].usage.output for r in result.requests) == [5, 7]


def test_unrecoverable_lines_quarantined(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s1")
    ev.raw(b"not json " + CANARY.encode())
    ev.raw(b"[1, 2]")
    ev.add("assistant.message", {"outputTokens": 1}, timestamp="yesterday")
    ev.objs[-1]["timestamp"] = "yesterday"
    path = ev.write(tmp_path / "e" / "events.jsonl")
    result = ADAPTER.read(path, opts())
    reasons = sorted(q.reason for q in result.quarantined)
    assert reasons == ["bad_json", "bad_type:timestamp"]
    assert notes_by_code(result)["dq.quarantined"] == 2
    assert_no_canary(blob(result))
    with pytest.raises(SourceError):
        ADAPTER.read(path, opts(lenient=False))


def test_bad_envelopes(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s1")
    ev.add("assistant.message", {})
    ev.objs[-1]["type"] = 7
    ev.add("assistant.message", {})
    del ev.objs[-1]["type"]
    ev.add("assistant.message", {})
    ev.objs[-1]["data"] = "text"
    ev.add("assistant.message", {})
    del ev.objs[-1]["timestamp"]
    ev.add("session.truncation", {"tokensRemovedDuringTruncation": -1})
    ev.add("session.model_change", {"previousModel": "x"})
    ev.add("session.usage_checkpoint", {"totalNanoAiu": "lots"})
    ev.add("session.shutdown", {"modelMetrics": {"m": {"usage": {"inputTokens": -5}}}})
    ev.add("session.shutdown", {"modelMetrics": "none"})
    ev.add("session.shutdown", {})
    ev.add("session.shutdown", {"modelMetrics": {"m": {"usage": 3}}})
    ev.add("session.shutdown", {"modelMetrics": {"m": {"usage": {"reasoningTokens": "x"}}}})
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    assert sorted(q.reason for q in result.quarantined) == sorted([
        "bad_type:type", "missing:type", "bad_type:data", "missing:timestamp",
        "bad_type:tokensRemovedDuringTruncation", "missing:newModel", "bad_type:totalNanoAiu",
        "bad_usage", "bad_type:modelMetrics", "missing:modelMetrics", "bad_usage", "bad_usage"])


def test_unknown_and_ignored_types(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s1")
    ev.add("session.brand_new_event", {"x": CANARY})
    ev.add(f"weird {CANARY}", {})
    ev.add("assistant.turn_end", {"turnId": "0"})
    ev.add("assistant.usage", {"inputTokens": 5})
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    (note,) = [n for n in result.notes if n.code == "dq.unknown_entry_type"]
    assert note.count == 2 and "session.brand_new_event=1" in note.detail
    assert "other=1" in note.detail
    assert result.stats["events_ignored"] == 2
    assert_no_canary(blob(result))


def test_three_leg_resumed_session_differenced(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s3", selectedModel="gpt-5.4")
    ev.message("c1", 10, model="gpt-5.4")
    ev.shutdown({"gpt-5.4": usage(1_000, 400, 100, 10)}, nano={"gpt-5.4": 1_000_000})
    ev.resume(selectedModel="gpt-5.4")
    ev.message("c2", 20, model="gpt-5.4")
    ev.shutdown({"gpt-5.4": usage(3_000, 1_400, 300, 30)}, nano={"gpt-5.4": 3_000_000})
    ev.resume()
    ev.message("c3", 5, model="gpt-5.4")
    ev.shutdown({"gpt-5.4": usage(3_500, 1_600, 300, 35, reasoning=3)},
                nano={"gpt-5.4": 3_500_000})
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    legs = sorted(result.aggregates, key=lambda a: a.bucket_start_ms)
    assert [(a.usage.uncached_input, a.usage.cache_read, a.usage.cache_write_unknown,
             a.usage.output) for a in legs] == [(500, 400, 100, 10), (800, 1_000, 200, 20),
                                                 (300, 200, 0, 5)]
    assert [a.reported_cost_nano for a in legs] == [10_000, 20_000, 5_000]
    assert legs[2].usage.output_reasoning == 3        # absent before: counted from 0
    assert len({a.agg_id for a in legs}) == 3
    assert notes_by_code(result)["dq.copilot_resume_legs"] == 2
    assert "dq.copilot_unclean_shutdown" not in notes_by_code(result)
    # sum of the per-leg deltas equals the final cumulative rollup
    assert sum(a.usage.output for a in legs) == 35


def test_fresh_epoch_and_compaction_reset(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s4")
    ev.shutdown({"m": usage(1_000, 0, 0, 10)})
    ev.resume()
    ev.shutdown({"m": usage(400, 0, 0, 4)})                 # less than before: a new epoch
    ev.resume()
    ev.compaction()
    ev.shutdown({"m": usage(900, 0, 0, 9)})                 # counters reset at the compaction
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    legs = sorted(result.aggregates, key=lambda a: a.bucket_start_ms)
    assert [a.usage.uncached_input for a in legs] == [1_000, 400, 900]
    assert notes_by_code(result)["dq.copilot_compaction_reset"] == 1


def test_missing_shutdown_is_unclean(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s5")
    ev.message("c1", 10)
    ev.resume()                                             # leg 0 never shut down
    ev.message("c2", 10)
    ev.shutdown({"claude-sonnet-4.5": usage(100, 0, 0, 20)}, kind="error")
    ev.resume()
    ev.message("c3", 10)                                    # last leg: no shutdown at EOF
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    assert notes_by_code(result)["dq.copilot_unclean_shutdown"] == 3
    assert len(result.aggregates) == 1
    assert_no_canary(blob(result))


def test_model_fallback_and_limits(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s6", selectedModel="claude-opus-4.8")
    ev.add("session.model_change", {"newModel": "claude-sonnet-4.5", "cause": "refusal_fallback",
                                    "contextTier": None, "reasoningEffort": None})
    ev.add("session.model_change", {"newModel": "gpt-5.4", "source": "automatic",
                                    "previousModel": "claude-sonnet-4.5"})
    ev.add("session.model_change", {"newModel": "auto"})
    ev.add("session.session_limits_changed", {"sessionLimits": {"maxAiCredits": 12.5}})
    ev.add("session.session_limits_changed", {"sessionLimits": None})
    ev.message("c1", 3, model="claude-haiku-4.5")
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    falls = _events(result, LaneEventKind.MODEL_FALLBACK)
    assert [dict(f.attrs) for f in falls] == [
        {"from_model": "claude-opus-4-8", "to_model": "claude-sonnet-4-5", "trigger": "refusal"},
        {"from_model": "claude-sonnet-4-5", "to_model": "gpt-5.4", "trigger": "unknown"}]
    (switch,) = _events(result, LaneEventKind.MODEL_SWITCH_USER)
    assert dict(switch.attrs) == {"from_model": "gpt-5.4", "to_model": "auto"}
    limits = [dict(m.attrs) for m in _events(result, LaneEventKind.SESSION_META)]
    assert {"credit_limit_nano": 125_000_000} in limits and len(limits) == 2
    (req,) = result.requests
    assert req.final_attempt.inferences[0].pricing.routing == "auto"


def test_compaction_variants(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s7")
    ev.compaction(total=G11_TOTAL_NANO_AIU + 5)             # sum-check off by 5 nano-AIU
    ev.compaction(details=[{"tokenType": "mystery", "tokenCount": 1, "batchSize": 1,
                            "costPerBatch": 1}])
    ev.compaction(success=False, trigger="manual")          # failed: no event, still billed
    ev.compaction(trigger="manual", compactionTokensUsed={"model": "gpt-4o-mini"})
    ev.compaction(total=0, model="gpt-4o-mini",
                  details=[dict(d, costPerBatch=0) for d in G11_DETAILS])
    result = ADAPTER.read(ev.write(tmp_path / "e" / "events.jsonl"), opts())
    notes = notes_by_code(result)
    assert notes["dq.copilot_nano_aiu_mismatch"] == 1
    assert [q.reason for q in result.quarantined] == ["bad_usage"]
    comps = _events(result, LaneEventKind.COMPACTION)
    assert [dict(c.attrs)["trigger"] for c in comps] == ["auto", "auto", "manual", "auto"]
    infs = [r.final_attempt.inferences[0] for r in result.requests]
    assert len(infs) == 3
    utility = infs[-1]
    assert utility.billable is False
    assert utility.billing_rule_id == "github.copilot.utility_unbilled"


def test_billing_path_compliance_and_identity(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s8")
    ev.message("c1", 3)
    path = ev.write(tmp_path / "e" / "events.jsonl")
    attr = Attribution(billing_path="copilot_direct", team="platform",
                       extra=(("copilot_compliance", "fedramp"),))
    result = ADAPTER.read(path, opts(attribution=attr, principal_ref="dev-7",
                                     identity_mode="central"))
    (req,) = result.requests
    ctx = req.final_attempt.inferences[0].pricing
    assert ctx.billing_path == "copilot_direct" and ctx.compliance == "fedramp"
    assert req.attribution.principal == "r_dev-7" and req.attribution.team == "platform"
    assert "dq.copilot_billing_path_assumed" not in notes_by_code(result)
    other = ADAPTER.read(path, opts(attribution=Attribution(billing_path="api_key")))
    assert other.requests[0].final_attempt.inferences[0].pricing.billing_path == "copilot_pool"
    assert "dq.copilot_billing_path_assumed" in notes_by_code(other)
    two = ADAPTER.read(path, opts(identity_mode="two-stage", principal_ref="dev-7"))
    assert two.requests[0].attribution.principal.startswith("c_")
    assert two.source.principal_key_id == opts().principal_key_id
    with pytest.raises(UsageError):
        ADAPTER.read(path, opts(identity_mode="central", principal_ref="a@b.c"))
    with pytest.raises(UsageError):
        ADAPTER.read(path, opts(identity_mode="nope"))
    with pytest.raises(UsageError):
        ADAPTER.read(path, opts(identity_mode="install", principal_ref="x", principal_key=None))


def test_window_and_no_name_key(tmp_path: Path) -> None:
    ev = Events()
    ev.start("s9")
    ev.message("c1", 3)
    ev.message("c2", 4, dt_ms=86_400_000)
    path = ev.write(tmp_path / "e" / "events.jsonl")
    result = ADAPTER.read(path, opts(until_ms=T0 + 3_600_000))
    assert [r.final_attempt.provider_message_id for r in result.requests] == ["c1"]
    bare = ADAPTER.read(path, opts(name_key=b"", name_key_id=""))
    assert bare.requests[0].attribution.repo is None and bare.source.name_key_id is None
    keyed = ADAPTER.read(path, opts())
    assert keyed.requests[0].attribution.repo == pseudonym(CONFORMANCE_NAME_KEY, "h",
                                                          f"acme/{CANARY}")
    assert keyed.source.name_key_id == key_id(CONFORMANCE_NAME_KEY)


def test_content_tier_and_file_policy(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        ADAPTER.read(HOME, opts(content_tier="fingerprint"))
    with pytest.raises(UsageError):
        ADAPTER.read(HOME, opts(content_tier="bogus"))
    with pytest.raises(UsageError):
        ADAPTER.read(HOME, "not options")  # type: ignore[arg-type]
    for name in ("config.json", "mcp-config.json", "providers.json", "session.db", "plan.md",
                 "apps.json"):
        assert never_opened(tmp_path / name)
        with pytest.raises(UsageError):
            ADAPTER.read(tmp_path / name, opts())
    assert never_opened(tmp_path / "logs" / "process-1.log")
    assert never_opened(tmp_path / "session-state" / "x" / "checkpoints" / "001.json")
    assert never_opened(tmp_path / "mcp-oauth-config" / "t.json")
    assert not never_opened(Path("events.jsonl")) and not never_opened(Path(""))
    with pytest.raises(SourceError):
        ADAPTER.read(tmp_path / "missing", opts())
    store = tmp_path / "session-store.db"
    store.write_bytes(b"x")
    with pytest.raises(UsageError):
        ADAPTER.read(store, opts())


def test_home_resolution_and_session_ids(tmp_path: Path) -> None:
    assert copilot_home({"COPILOT_HOME": str(tmp_path)}) == tmp_path
    assert copilot_home({"HOME": "/home/runner"}) == Path("/home/runner/.copilot")
    assert copilot_home({}).name == ".copilot"
    assert session_ids(HOME) == frozenset({SID_A, SID_B})
    assert session_ids(tmp_path) == frozenset()
    (tmp_path / "session-state" / "empty").mkdir(parents=True)
    assert iter_events_files(tmp_path) == []
    assert session_ids(tmp_path) == frozenset({"empty"})


def test_sniff() -> None:
    head = session_file(HOME, SID_A).read_bytes()[:65536]
    assert ADAPTER.sniff(session_file(HOME, SID_A), head)
    cc = b'{"type":"user","sessionId":"x","uuid":"u","message":{}}\n'
    assert not ADAPTER.sniff(Path("t.jsonl"), cc)
    assert not ADAPTER.sniff(Path("t.json"), head)
    assert not ADAPTER.sniff(Path("config.json"), head)
    assert not ADAPTER.sniff(Path("t.jsonl"), b"\n\n")
    assert not ADAPTER.sniff(Path("t.jsonl"), "text")  # type: ignore[arg-type]
    assert not ADAPTER.sniff(Path("t.jsonl"), b'{"resourceSpans": []}\n')


def test_parse_helpers() -> None:
    assert parse_ts_ms("2026-09-20T10:00:00Z") == T0
    assert parse_ts_ms("2026-09-20T12:00:00.5+02:00") == T0 + 500
    assert parse_ts_ms("2026-09-20 10:00:00") == T0
    assert parse_ts_ms(T0 // 1000) == T0 and parse_ts_ms(T0) == T0
    for bad in ("2026-02-30T00:00:00Z", "1969-12-31T23:59:59Z", "x", None, -5, "9" * 50,
                "2026-13-01T00:00:00Z", 10**20):
        assert parse_ts_ms(bad) is None
    assert parse_event_line(b"") == ([], False)
    assert parse_event_line(b'{"a": 1}') == ([{"a": 1}], False)


def test_windows_attribution_dataclass_roundtrip() -> None:
    attr = Attribution(extra=(("copilot_compliance", "data_residency"),))
    assert dataclasses.replace(attr).extra == attr.extra
