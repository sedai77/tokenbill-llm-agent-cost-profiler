"""``copilot-otel`` acceptance tests (addendum §5.10; CP-OTEL brief): VS Code OTel-JS dumps, the
experimental CLI envelope, JetBrains behind its flag, mixed OTLP files, native vs synthesized
spans, and the claim rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.adapters.copilot_otel import (
    CONVENTION,
    REPORTER_INVOKE_AGENT,
    CopilotOtelAdapter,
    sniff_copilot_otel,
)
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import SourceError
from tokenbill.core.ids import copilot_lane_key, copilot_session_key, pseudonym
from tokenbill.core.lanes import group_lanes
from tokenbill.core.records import Fidelity, LaneEventKind, LaneKind, Outcome
from tokenbill.core.testing import CONFORMANCE_PRINCIPAL_KEY

from .helpers import (
    CLI_FILE,
    CLI_FLAG,
    COPILOT_OTLP,
    GH_AW,
    JB_FLAG,
    JETBRAINS,
    MIXED,
    PRINCIPAL_KEY_ID,
    VSCODE_DUMP,
    by_message,
    codes,
    inference,
    no_leak,
    opts,
)

ADAPTER = CopilotOtelAdapter()
TELEM_CLAUDE = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / \
    "otlp_claude_code.jsonl"


def _head(path: Path) -> bytes:
    return path.read_bytes()[:64 * 1024]


# ---------------------------------------------------------------------------------------------
# (a) VS Code OTel-JS dump
# ---------------------------------------------------------------------------------------------

def test_vscode_dump_two_chat_spans_become_two_requests() -> None:
    result = ADAPTER.read(VSCODE_DUMP, opts())
    assert len(result.requests) == 2
    reqs = by_message(result)
    one, two = reqs["resp_vs_001"], reqs["resp_vs_002"]
    inf1, inf2 = inference(one), inference(two)
    # inclusive input decomposed; creation → unknown-TTL write
    assert (inf1.usage.uncached_input, inf1.usage.cache_read, inf1.usage.cache_write_unknown,
            inf1.usage.output) == (2000, 15000, 3000, 700)
    assert inf1.usage.total_input == 20000
    assert inf1.pricing.write_ttl_hint is None
    assert (inf2.usage.uncached_input, inf2.usage.cache_read, inf2.usage.cache_write_unknown,
            inf2.usage.output, inf2.usage.output_reasoning) == (2000, 6000, 0, 400, 100)
    # nano-AIU → provider estimate (÷ 100)
    assert inf1.provider_reported_cost_nano == 123_456_000
    assert inf2.provider_reported_cost_nano == 10_000_000
    assert {inf1.provider_reported_cost_basis, inf2.provider_reported_cost_basis} == {
        "provider_estimate"}
    # models and routing
    assert (inf1.pricing.model, inf1.pricing.routing) == ("claude-sonnet-4-5", "direct")
    assert (inf2.pricing.model, inf2.pricing.routing) == ("gpt-5-mini", "auto")
    assert two.params.model_requested == "auto" and two.params.effort == "medium"
    assert one.params.effort is None
    for req in (one, two):
        inf = inference(req)
        assert (inf.pricing.provider, inf.pricing.channel) == ("github", "github_copilot")
        assert inf.pricing.billing_path == "copilot_pool"
        assert req.attribution.billing_path == "copilot_pool"
        assert req.attribution.agent_product == "copilot_vscode"
        assert req.attempts[0].convention_id == CONVENTION
        assert req.source is not None and req.source.fidelity is Fidelity.NO_TTL_SPLIT
        assert req.source.priority == 21 and req.source.adapter == "copilot-otel"
    assert one.attempts[0].ttft_ms == 850
    assert one.attempts[0].duration_ms == 4000
    assert codes(result)["dq.raw_bodies_ignored"] >= 8
    assert codes(result)["dq.copilot_billing_path_assumed"] == 2
    assert result.stats["ignored_records"] == 1          # the log record line
    assert result.capabilities == {"usage_sequence", "timing", "params", "credits"}
    no_leak(result)


def test_vscode_dump_session_lane_and_identity() -> None:
    team_map = ((CANARY_LOGIN, "payments"),)
    result = ADAPTER.read(VSCODE_DUMP, opts(team_map=team_map, identity_mode="central-ingest"))
    session_key = copilot_session_key("conv-vscode-1")
    lane_key = copilot_lane_key(session_key, "main", None)
    assert {r.session_key for r in result.requests} == {session_key}
    assert {r.lane_key for r in result.requests} == {lane_key}
    assert [r.seq for r in sorted(result.requests, key=lambda r: r.ts_start_ms)] == [0, 1]
    principal = pseudonym(CONFORMANCE_PRINCIPAL_KEY, "p", CANARY_LOGIN)
    assert {r.attribution.principal for r in result.requests} == {principal}
    assert {r.attribution.team for r in result.requests} == {"payments"}
    assert all(r.attribution.repo and r.attribution.repo.startswith("h_")
               for r in result.requests)
    assert {r.attribution.client_version for r in result.requests} == {"0.35.3"}
    assert result.source.principal_key_id == PRINCIPAL_KEY_ID
    (session,) = result.sessions
    assert session.session_key == session_key
    lanes = group_lanes(result.requests, result.events, result.sessions)
    assert [lane.kind for lane in lanes] == [LaneKind.MAIN]
    no_leak(result)


def test_central_mode_uses_the_collector_ref() -> None:
    result = ADAPTER.read(VSCODE_DUMP, opts(identity_mode="central", principal_ref="dev-42"))
    assert {r.attribution.principal for r in result.requests} == {"r_dev-42"}
    assert result.source.principal_key_id is None


def test_two_stage_mode_and_missing_principal_key() -> None:
    result = ADAPTER.read(VSCODE_DUMP, opts(identity_mode="two-stage", principal_ref="dev-42"))
    assert {r.attribution.principal[:2] for r in result.requests} == {"c_"}
    result = ADAPTER.read(VSCODE_DUMP, opts(principal_key=None, principal_key_id=None))
    assert {r.attribution.principal for r in result.requests} == {None}


# ---------------------------------------------------------------------------------------------
# (c) CLI envelope (experimental)
# ---------------------------------------------------------------------------------------------

def test_cli_file_with_flag_invoke_agent_and_three_chats() -> None:
    result = ADAPTER.read(CLI_FILE, opts(experimental=CLI_FLAG))
    assert len(result.requests) == 3
    infs = [inference(r) for r in result.requests]
    session_key = copilot_session_key("cli-session-1")
    main = copilot_lane_key(session_key, "main", None)
    cost = [e for e in result.events if e.kind is LaneEventKind.COST_STATE]
    assert len(cost) == 1
    attrs = dict(cost[0].attrs)
    assert attrs["reporter"] == REPORTER_INVOKE_AGENT
    total = sum(i.provider_reported_cost_nano or 0 for i in infs)
    assert attrs["reported_total_nano"] == total == 75_000_000
    assert cost[0].lane_key == main
    auto = [i for i in infs if i.pricing.routing == "auto"]
    assert len(auto) == 1 and auto[0].pricing.model == "claude-haiku-4-5"
    utility = [i for i in infs if i.pricing.model == "gpt-4o-mini"]
    assert utility[0].billable is False
    assert utility[0].billing_rule_id == "github.copilot.utility_unbilled"
    assert {r.attribution.agent_product for r in result.requests} == {"copilot_cli"}
    kinds = sorted(e.kind.value for e in result.events)
    assert kinds == ["compaction", "context_edit", "cost_state", "session_meta"]
    compaction = next(e for e in result.events if e.kind is LaneEventKind.COMPACTION)
    assert dict(compaction.attrs) == {"trigger": "auto", "copilot_trigger": "threshold",
                                      "pre_tokens": 90000, "post_tokens": 12000}
    edit = next(e for e in result.events if e.kind is LaneEventKind.CONTEXT_EDIT)
    assert dict(edit.attrs) == {"edit_type": "copilot_truncation", "cleared_input_tokens": 5000}
    assert "events" in result.capabilities
    assert result.stats["compaction_other_events"] == 1
    no_leak(result)


def test_cli_file_without_flag_is_quarantined() -> None:
    result = ADAPTER.read(CLI_FILE, opts())
    assert result.requests == [] and result.events == []
    assert {q.reason for q in result.quarantined} == {"experimental:copilot-cli-otel-file"}
    assert len(result.quarantined) == 4
    assert codes(result)["dq.quarantined"] == 4
    with pytest.raises(SourceError):
        ADAPTER.read(CLI_FILE, opts(lenient=False))


# ---------------------------------------------------------------------------------------------
# (d) JetBrains (experimental)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("names", [("copilot-intellij=copilot_jetbrains",), ("copilot-intellij",)])
def test_jetbrains_resource_needs_the_flag(names: tuple[str, ...]) -> None:
    off = ADAPTER.read(JETBRAINS, opts(otel_service_names=names))
    assert off.requests == []
    assert off.stats["jetbrains_resources_skipped"] == 1
    assert codes(off)["dq.copilot_jetbrains_otel_experimental"] == 1
    on = ADAPTER.read(JETBRAINS, opts(otel_service_names=names, experimental=JB_FLAG))
    assert len(on.requests) == 2
    assert {r.attribution.agent_product for r in on.requests} == {"copilot_jetbrains"}
    assert codes(on)["dq.copilot_jetbrains_otel_unmapped"] == 1   # the unknown-layout span
    no_leak(on)


def test_jetbrains_without_configuration_is_foreign() -> None:
    result = ADAPTER.read(JETBRAINS, opts(experimental=JB_FLAG))
    assert result.requests == [] and result.stats["foreign_resources"] == 1
    other = ADAPTER.read(JETBRAINS, opts(otel_service_names=("copilot-intellij=copilot_other",),
                                         experimental=JB_FLAG))
    assert {r.attribution.agent_product for r in other.requests} == {"copilot_other"}
    bad = ADAPTER.read(JETBRAINS, opts(otel_service_names=("copilot-intellij=nonsense",)))
    assert {r.attribution.agent_product for r in bad.requests} == {"copilot_other"}


# ---------------------------------------------------------------------------------------------
# mixed OTLP/JSON files and the claim rule
# ---------------------------------------------------------------------------------------------

def test_mixed_file_reads_only_copilot_resources() -> None:
    assert ADAPTER.sniff(MIXED, _head(MIXED)) is False
    result = ADAPTER.read(MIXED, opts(otel_service_names=("acme-copilot-proxy",)))
    reqs = by_message(result)
    assert set(reqs) == {"resp_mix_1", "resp_mix_2", "resp_mix_3"}
    assert reqs["resp_mix_1"].attribution.agent_product == "copilot_cli"
    # a custom service.name (configured) and a resource recognized only by github.copilot.* keys
    assert reqs["resp_mix_2"].attribution.agent_product == "copilot_other"
    assert reqs["resp_mix_3"].attribution.agent_product == "copilot_other"
    assert result.stats["foreign_resources"] == 1      # the Claude Code resource
    assert all(inference(r).pricing.channel == "github_copilot" for r in result.requests)
    no_leak(result)
    unconfigured = ADAPTER.read(MIXED, opts())
    assert set(by_message(unconfigured)) == {"resp_mix_1", "resp_mix_3"}
    assert unconfigured.stats["foreign_resources"] == 2


def test_sniff_claim_rule() -> None:
    assert ADAPTER.sniff(VSCODE_DUMP, _head(VSCODE_DUMP)) is True
    assert ADAPTER.sniff(COPILOT_OTLP, _head(COPILOT_OTLP)) is True
    assert ADAPTER.sniff(CLI_FILE, _head(CLI_FILE)) is True
    assert ADAPTER.sniff(MIXED, _head(MIXED)) is False
    assert ADAPTER.sniff(JETBRAINS, _head(JETBRAINS)) is False    # no Copilot marker
    assert sniff_copilot_otel(_head(JETBRAINS), extra_service_names=("copilot-intellij",))
    assert ADAPTER.sniff(GH_AW, _head(GH_AW)) is False
    if TELEM_CLAUDE.exists():
        assert ADAPTER.sniff(TELEM_CLAUDE, _head(TELEM_CLAUDE)) is False
    assert sniff_copilot_otel(b"") is False
    assert sniff_copilot_otel(b"not json\n") is False
    assert sniff_copilot_otel(b'{"a": 1}\n') is False
    line = VSCODE_DUMP.read_bytes().split(b"\n")[0]
    assert sniff_copilot_otel(line + b"\n" + line[:40]) is True    # truncated tail ignored
    assert sniff_copilot_otel(line[:40] + b"\n" + line + b"\n") is False


# ---------------------------------------------------------------------------------------------
# native vs synthesized spans, subagents, dedupe
# ---------------------------------------------------------------------------------------------

def test_native_span_wins_over_synthesized_span_of_one_call() -> None:
    result = ADAPTER.read(COPILOT_OTLP, opts())
    reqs = by_message(result)
    assert set(reqs) == {"resp_native_1", "resp_native_2", None}
    assert codes(result)["dq.copilot_synthesized_span_skipped"] == 1
    assert codes(result)["dq.copilot_synthesized_span_kept"] == 1
    session = copilot_session_key("cli-conv-9")
    main = copilot_lane_key(session, "main", None)
    sub = copilot_lane_key(session, "subagent", "sub-1")
    assert reqs["resp_native_1"].session_key == session
    assert reqs["resp_native_1"].lane_key == main
    assert reqs["resp_native_2"].lane_key == sub
    assert reqs["resp_native_2"].attribution.query_source == "subagent"
    assert reqs["resp_native_2"].attribution.agent_type.startswith("h_")
    kept = reqs[None]
    assert kept.session_key == copilot_session_key("cli-conv-10")
    assert kept.attribution.agent_product == "copilot_vscode"
    lanes = {lane.lane_key: lane for lane in group_lanes(result.requests, result.events,
                                                         result.sessions)}
    assert lanes[sub].kind is LaneKind.SUBAGENT and lanes[sub].parent_lane_key == main
    cost = [e for e in result.events if e.kind is LaneEventKind.COST_STATE]
    assert [dict(e.attrs)["reported_total_nano"] for e in cost] == [40_000_000]  # root only
    edit = next(e for e in result.events if e.kind is LaneEventKind.CONTEXT_EDIT)
    assert edit.lane_key == sub
    no_leak(result)


def test_a_span_read_twice_counts_once(tmp_path: Path) -> None:
    doubled = tmp_path / "doubled.jsonl"
    doubled.write_bytes(VSCODE_DUMP.read_bytes() * 2)
    result = ADAPTER.read(doubled, opts())
    assert len(result.requests) == 2
    assert result.stats["duplicate_spans"] == 2


def test_requests_without_response_id_are_keyed_by_conversation_turn_span(tmp_path: Path
                                                                          ) -> None:
    lines = [json.loads(x) for x in VSCODE_DUMP.read_text().splitlines()]
    for obj in lines:
        obj.get("attributes", {}).pop("gen_ai.response.id", None)
    path = tmp_path / "noid.jsonl"
    path.write_text("".join(json.dumps(o) + "\n" for o in lines + lines))
    result = ADAPTER.read(path, opts())
    assert len(result.requests) == 2
    assert all(r.attempts[0].provider_message_id is None for r in result.requests)


# ---------------------------------------------------------------------------------------------
# edge rules
# ---------------------------------------------------------------------------------------------

def _span_line(attrs: dict[str, object], *, service: str = "copilot-chat",
               span_id: str = "0000000000000001", start: object = None,
               events: list[dict[str, object]] | None = None,
               status: dict[str, object] | None = None) -> str:
    obj = {"traceId": "ab" * 16, "spanId": span_id, "name": "chat x", "kind": 2,
           "startTime": start if start is not None else [1_790_157_600, 0],
           "endTime": [1_790_157_601, 500_000_000],
           "attributes": {"gen_ai.operation.name": "chat", "gen_ai.conversation.id": "c-1",
                          **attrs},
           "events": events or [], "status": status or {"code": 0},
           "resource": {"attributes": {"service.name": service}}}
    return json.dumps(obj)


def _read_lines(tmp_path: Path, *lines: str, **kw: object):
    path = tmp_path / "spans.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    return ADAPTER.read(path, opts(**kw))


def test_utility_and_byok_rules(tmp_path: Path) -> None:
    base = {"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 10}
    result = _read_lines(
        tmp_path,
        _span_line({**base, "gen_ai.response.model": "gpt-4o-mini",
                    "gen_ai.response.id": "u0", "copilot_chat.copilot_usage_nano_aiu": 0},
                   span_id="0000000000000001"),
        _span_line({**base, "gen_ai.response.model": "gpt-4o-mini",
                    "gen_ai.response.id": "u1", "copilot_chat.copilot_usage_nano_aiu": 5},
                   span_id="0000000000000002"),
        _span_line({**base, "gen_ai.response.model": "gpt-5.4-nano", "gen_ai.response.id": "u2"},
                   span_id="0000000000000003"),
        _span_line({**base, "gen_ai.response.model": "claude-sonnet-4.5",
                    "gen_ai.response.id": "u3", "copilot_chat.interaction_type":
                    "conversation-background", "copilot_chat.copilot_usage_nano_aiu": 0},
                   span_id="0000000000000004"),
        _span_line({**base, "gen_ai.response.model": "claude-sonnet-4.5",
                    "gen_ai.response.id": "u4", "copilot_chat.endpoint_type": "byok"},
                   span_id="0000000000000005"),
    )
    infs = {k: inference(r) for k, r in by_message(result).items()}
    assert (infs["u0"].billable, infs["u0"].billing_rule_id) == (
        False, "github.copilot.utility_unbilled")
    assert infs["u1"].billable is True          # a utility model with nano-AIU > 0 is billed
    assert infs["u2"].billable is True          # never by name alone
    assert infs["u3"].billable is False
    assert by_message(result)["u3"].attribution.query_source == "auxiliary"
    assert (infs["u4"].billable, infs["u4"].billing_rule_id) == (
        False, "github.copilot.byok_not_billed_by_github")


def test_context_tier_effort_and_billing_path(tmp_path: Path) -> None:
    base = {"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 10,
            "gen_ai.response.model": "gpt-5.4"}
    from dataclasses import replace

    from tokenbill.core.records import Attribution
    attribution = replace(Attribution(), billing_path="copilot_direct",
                          extra=(("copilot_compliance", "fedramp"),))
    result = _read_lines(
        tmp_path,
        _span_line({**base, "gen_ai.response.id": "t1",
                    "copilot_chat.request.max_prompt_tokens": 400_000}, span_id="01"),
        _span_line({**base, "gen_ai.response.id": "t2",
                    "copilot_chat.request.max_prompt_tokens": 128_000,
                    "gen_ai.request.reasoning.level": "HIGH"}, span_id="02"),
        _span_line({**base, "gen_ai.response.id": "t3", "copilot_chat.context_tier":
                    "long_context", "gen_ai.request.reasoning.level": "bad value!"},
                   span_id="03"),
        attribution=attribution)
    reqs = by_message(result)
    assert inference(reqs["t1"]).pricing.context_tier == "long_context"
    assert inference(reqs["t2"]).pricing.context_tier == "default"
    assert inference(reqs["t3"]).pricing.context_tier == "long_context"
    assert reqs["t2"].params.effort == "high" and reqs["t3"].params.effort is None
    assert {inference(r).pricing.billing_path for r in result.requests} == {"copilot_direct"}
    assert {inference(r).pricing.compliance for r in result.requests} == {"fedramp"}
    assert "dq.copilot_billing_path_assumed" not in codes(result)


def test_bad_usage_mismatch_legacy_and_missing_time(tmp_path: Path) -> None:
    result = _read_lines(
        tmp_path,
        _span_line({"gen_ai.usage.input_tokens": "lots", "gen_ai.response.id": "b1"},
                   span_id="01"),
        _span_line({"gen_ai.usage.input_tokens": 100, "gen_ai.usage.cache_read.input_tokens": 90,
                    "gen_ai.usage.cache_creation.input_tokens": 50,
                    "gen_ai.usage.output_tokens": 5, "gen_ai.usage.reasoning.output_tokens": 9,
                    "gen_ai.response.id": "b2"}, span_id="02"),
        _span_line({"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 5,
                    "gen_ai.usage.cache_read_input_tokens": 40, "gen_ai.response.id": "b3"},
                   span_id="03"),
        _span_line({"gen_ai.usage.input_tokens": 1, "gen_ai.response.id": "b4"}, span_id="04",
                   start="not a time"),
        json.dumps({"traceId": "zz", "spanId": "yy", "name": "chat",
                    "attributes": {"copilot_chat.turn.index": 1},
                    "resource": {"attributes": {"service.name": "copilot-chat"}}}),
        json.dumps({"hrTime": [1, 0], "body": "log", "resource": {"attributes": {}}}),
        "[1, 2]", "{bad json",
    )
    reqs = by_message(result)
    assert set(reqs) == {"b2"}
    usage = inference(reqs["b2"]).usage
    assert (usage.uncached_input, usage.cache_read, usage.cache_write_unknown) == (100, 90, 50)
    assert usage.output_reasoning is None
    found = codes(result)
    assert found["dq.convention_mismatch"] == 1 and found["dq.sum_check_failed"] == 1
    assert found["dq.copilot_legacy_otel_names"] == 1
    reasons = sorted(q.reason for q in result.quarantined)
    assert reasons == ["bad_json", "bad_usage", "missing:spanId", "missing:startTime",
                       "not_object"]


def test_error_span_window_and_nano_invalid(tmp_path: Path) -> None:
    base = {"gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 1}
    lines = (
        _span_line({**base, "gen_ai.response.id": "e1", "error.type": "rate_limited",
                    "copilot_chat.copilot_usage_nano_aiu": -3}, span_id="01",
                   status={"code": 2}),
        _span_line({**base, "gen_ai.response.id": "e2"}, span_id="02",
                   start=[1_790_157_000, 0]),
    )
    result = _read_lines(tmp_path, *lines, since_ms=1_790_157_500_000)
    reqs = by_message(result)
    assert set(reqs) == {"e1"} and result.stats["out_of_window"] == 1
    att = reqs["e1"].attempts[0]
    assert att.outcome is Outcome.HTTP_ERROR and att.error_type == "rate_limited"
    assert inference(reqs["e1"]).provider_reported_cost_nano is None
    assert codes(result)["dq.copilot_nano_aiu_invalid"] == 1


def test_json_lines_collector_extract_principal(tmp_path: Path) -> None:
    def res(principal: object, kid: object) -> dict[str, object]:
        return {"service.name": "copilot-chat", "tokenbill.collector": "copilot-vscode-collect@1",
                "tokenbill.principal": principal, "tokenbill.principal_key_id": kid,
                "tokenbill.team": "search"}

    base = {"gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 1}
    lines = []
    for i, (principal, kid) in enumerate((("c_0123456789abcdef0123", "k_aaaaaaaaaaaa"),
                                          ("c_0123456789abcdef0123", "k_bbbbbbbbbbbb"),
                                          ("alice@example.com", "k_aaaaaaaaaaaa"),
                                          ("r_dev.7", None))):
        obj = json.loads(_span_line({**base, "gen_ai.response.id": f"x{i}"},
                                    span_id=f"0{i + 1}"))
        obj["resource"] = {"attributes": res(principal, kid)}
        lines.append(json.dumps(obj))
    result = _read_lines(tmp_path, *lines)
    reqs = by_message(result)
    assert reqs["x0"].attribution.principal == "c_0123456789abcdef0123"
    assert reqs["x1"].attribution.principal is None      # another key id
    assert reqs["x2"].attribution.principal is None      # not a collector principal
    assert reqs["x3"].attribution.principal == "r_dev.7"
    assert {r.attribution.team for r in result.requests} == {"search"}
    assert result.source.principal_key_id == "k_aaaaaaaaaaaa"
    assert codes(result)["dq.copilot_collector_principal_invalid"] == 2
    assert "alice" not in repr(result)


def test_marker_keys_are_ignored_without_the_marker(tmp_path: Path) -> None:
    obj = json.loads(_span_line({"gen_ai.usage.input_tokens": 5, "gen_ai.response.id": "m1"}))
    obj["resource"] = {"attributes": {"service.name": "copilot-chat",
                                      "tokenbill.principal": "r_someone",
                                      "tokenbill.team": "eng"}}
    result = _read_lines(tmp_path, json.dumps(obj))
    (req,) = result.requests
    assert req.attribution.principal is None and req.attribution.team is None


def test_span_events_on_otlp_and_windowed_events(tmp_path: Path) -> None:
    ev = [{"name": "github.copilot.session.compaction_complete", "time": [1_790_157_600, 1],
           "attributes": {"trigger": "manual", "systemTokens": 11, "toolDefinitionsTokens": 22,
                          "durationMs": 33}},
          {"name": "github.copilot.session.truncation", "attributes": {}},
          {"name": "something.else", "time": [1_790_157_600, 1]}]
    result = _read_lines(tmp_path, _span_line({"gen_ai.usage.input_tokens": 1,
                                               "gen_ai.response.id": "v1"}, events=ev))
    comp = next(e for e in result.events if e.kind is LaneEventKind.COMPACTION)
    assert dict(comp.attrs) == {"trigger": "manual", "copilot_trigger": "manual",
                                "system_tokens": 11, "tool_definitions_tokens": 22,
                                "duration_ms": 33}
    edit = next(e for e in result.events if e.kind is LaneEventKind.CONTEXT_EDIT)
    assert dict(edit.attrs) == {"edit_type": "copilot_truncation"}
    assert len(result.events) == 2
