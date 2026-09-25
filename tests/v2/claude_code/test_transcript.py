"""Acceptance tests of the Claude Code transcript importer (SPEC §5.3; brief CC)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.records import (
    InferenceKind,
    LaneEventKind,
    LaneKind,
    Outcome,
    UsageSource,
)
from tokenbill.core.testing import FakePricer

from .helpers import (
    CC,
    NOW_MS,
    attrs,
    bf,
    by_message,
    canonical,
    events,
    note,
    opts,
    read,
    set_mtime,
    write,
)

OPUS = "claude-opus-5-5"


def tx(**kw: object) -> object:
    return bf.Tx("11111111-0000-4000-8000-000000000001", **kw)


# --- step 1-3: grouping, max output, duplicates, synthetic -------------------------------------

def test_split_message_is_one_request_with_the_max_output(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_split", OPUS, inp=1000, w5=2000, outputs=(3, 250, 470))
    r = read(tmp_path, t)
    assert len(r.requests) == 1
    [inf] = r.requests[0].attempts[0].inferences
    assert inf.usage.output == 470
    assert inf.usage.uncached_input == 1000 and inf.usage.cache_write_5m == 2000
    assert r.naive_usage[OPUS].uncached_input == 3 * 1000       # naive: every line counted
    assert r.naive_usage[OPUS].output == 3 + 250 + 470
    assert r.stats["assistant_lines"] == 3


def test_ties_keep_the_later_line_and_stop_reason_is_the_last_non_null(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.assistant_line("msg_tie", OPUS, bf.usage(5, out=40), {"type": "text", "text": "a"},
                     stop="tool_use")
    t.assistant_line("msg_tie", OPUS, bf.usage(5, out=40), {"type": "text", "text": "b"},
                     stop=None, perTurnEffort="low")
    r = read(tmp_path, t)
    [req] = r.requests
    assert req.attempts[0].stop_reason == "tool_use"
    assert req.params.effort == "low"   # the later (kept) line's fields


def test_split_usage_mismatch_keeps_the_max_output_entry(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.assistant_line("msg_mm", OPUS, bf.usage(5, read=100, out=3), None)
    t.assistant_line("msg_mm", OPUS, bf.usage(5, read=900, out=80), None, stop="end_turn")
    r = read(tmp_path, t)
    [inf] = r.requests[0].attempts[0].inferences
    assert inf.usage.cache_read == 900 and inf.usage.output == 80
    assert note(r, "dq.split_usage_mismatch").count == 1


def test_duplicate_uuid_lines_are_dropped(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_dup", OPUS, inp=10, outputs=(3, 50))
    t.duplicate_last()
    t.tool_result("toolu_dup", "x")
    t.duplicate_last()
    r = read(tmp_path, t)
    assert note(r, "dq.duplicate_uuid_lines").count == 2
    assert r.naive_usage[OPUS].uncached_input == 20    # the duplicate line is not in the naive sum
    assert len(r.requests) == 1


def test_synthetic_messages_are_skipped(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.assistant_line("msg_syn", "<synthetic>", bf.usage(), {"type": "text", "text": "API Error"},
                     stop="stop_sequence", isApiErrorMessage=True, apiErrorStatus=500)
    r = read(tmp_path, t)
    assert r.requests == []
    assert note(r, "dq.synthetic_skipped").count == 1
    assert r.naive_usage == {}


def test_api_error_entry_with_a_real_model_is_an_http_error_attempt(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_err", OPUS, inp=10, outputs=(0,), stop="end_turn",
           line_extra={"isApiErrorMessage": True, "apiErrorStatus": 429})
    [req] = read(tmp_path, t).requests
    att = req.attempts[0]
    assert att.outcome is Outcome.HTTP_ERROR and att.http_status == 429
    assert att.error_type == "rate_limit"


# --- step 2/4: timing -------------------------------------------------------------------------

def test_ts_start_is_the_preceding_trigger_and_duration_spans_the_lines(tmp_path: Path) -> None:
    t = tx()
    t.human("go")                     # trigger at T0+5s
    trigger = t.t
    t.call("msg_t", OPUS, inp=10, outputs=(3, 250, 470), dt_ms=4_000)
    last = t.t
    [req] = read(tmp_path, t).requests
    att = req.attempts[0]
    assert att.ts_start_ms == trigger
    assert att.duration_ms == last - trigger
    assert att.ttft_ms is None


def test_without_a_trigger_ts_start_falls_back_to_the_first_entry(tmp_path: Path) -> None:
    t = tx()
    t.call("msg_first", OPUS, inp=10, outputs=(3, 9), dt_ms=1_000)
    [req] = read(tmp_path, t).requests
    assert req.ts_start_ms == bf.T0_MS + 1_000


# --- step 5: iterations and refusal rule ------------------------------------------------------

def test_declined_zero_output_plus_fallback(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    its = [bf.iteration("message", "claude-fable-5", inp=900, out=0),
           bf.iteration("fallback_message", "claude-opus-4-8", inp=900, out=210)]
    t.call("msg_fb", "claude-fable-5", inp=900, outputs=(210,), stop="end_turn", iterations=its)
    [req] = read(tmp_path, t).requests
    declined, fallback = req.attempts[0].inferences
    assert declined.kind is InferenceKind.FALLBACK_DECLINED and declined.billable is False
    assert declined.pricing.model == "claude-fable-5"
    assert declined.billing_rule_id == "anthropic.refusal.pre_output"
    assert fallback.kind is InferenceKind.FALLBACK and fallback.billable is True
    assert fallback.pricing.model == "claude-opus-4-8"


def test_declined_six_output_tokens_is_ambiguous(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    its = [bf.iteration("message", "claude-fable-5", inp=900, out=6),
           bf.iteration("fallback_message", "claude-opus-4-8", inp=900, out=210)]
    t.call("msg_fb6", "claude-fable-5", inp=900, outputs=(210,), stop="end_turn", iterations=its)
    [req] = read(tmp_path, t).requests
    declined = req.attempts[0].inferences[0]
    assert declined.billable is None
    assert declined.billing_rule_id == "anthropic.refusal.ambiguous"


def test_compaction_iteration_is_its_own_inference(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    its = [bf.iteration("compaction", inp=50_000, out=3_000),
           bf.iteration("message", inp=4_000, out=300)]
    t.call("msg_ci", OPUS, inp=4_000, outputs=(300,), stop="end_turn", iterations=its)
    r = read(tmp_path, t)
    kinds = [i.kind for i in r.requests[0].attempts[0].inferences]
    assert kinds == [InferenceKind.COMPACTION, InferenceKind.MESSAGE]
    assert r.requests[0].attempts[0].inferences[0].usage.uncached_input == 50_000
    assert note(r, "dq.iterations_mismatch") is None   # Σ MESSAGE elements == top level
    pricer = FakePricer()
    comp, message = r.requests[0].attempts[0].inferences
    ts = r.requests[0].ts_start_ms
    priced = [pricer.price_inference(i, ts_ms=ts) for i in (comp, message)]
    assert all(p.figure.nano and p.figure.nano > 0 for p in priced)   # each priced on its own
    assert priced[0].figure.nano > priced[1].figure.nano                # 50k in vs 4k in


# --- step 5: TTL split -------------------------------------------------------------------------

def test_ttl_split_is_preserved(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_ttl", OPUS, inp=5, w5=2_000, w1=3_000, outputs=(40,), stop="end_turn")
    r = read(tmp_path, t)
    u = r.requests[0].attempts[0].inferences[0].usage
    assert (u.cache_write_5m, u.cache_write_1h, u.cache_write_unknown) == (2_000, 3_000, 0)
    assert "ttl_split" in r.capabilities


def test_ttl_split_residual_becomes_cache_write_unknown(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_res", OPUS, inp=5, w5=2_000, w1=3_000, total_write=6_000, outputs=(40,),
           stop="end_turn")
    r = read(tmp_path, t)
    u = r.requests[0].attempts[0].inferences[0].usage
    assert (u.cache_write_5m, u.cache_write_1h, u.cache_write_unknown) == (2_000, 3_000, 1_000)
    assert note(r, "dq.ttl_split_residual").count == 1


def test_split_exceeding_the_total_is_noted(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_ex", OPUS, inp=5, w5=2_000, w1=3_000, total_write=4_000, outputs=(40,),
           stop="end_turn")
    r = read(tmp_path, t)
    assert note(r, "dq.ttl_split_exceeds_total").count == 1


def test_no_split_object_means_unknown_ttl_and_no_ttl_split_capability(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_ns", OPUS, inp=5, total_write=700, outputs=(40,), stop="end_turn", split=False)
    r = read(tmp_path, t)
    assert r.requests[0].attempts[0].inferences[0].usage.cache_write_unknown == 700
    assert "ttl_split" not in r.capabilities


# --- step 6: MESSAGE_START_ONLY ------------------------------------------------------------------

def _mso_transcript(outputs: tuple[int, ...] = (3,)) -> object:
    t = tx()
    t.human("go")
    t.call("msg_done", OPUS, inp=5, w5=1_000, outputs=(3, 400), stop="tool_use")
    t.tool_result("toolu_done", "ok")
    t.call("msg_mso", OPUS, inp=5, read=1_000, outputs=outputs, stop=None,
           blocks=[{"type": "tool_use", "id": "toolu_mso", "name": "Bash",
                    "input": {"command": "x" * 1_000}}])
    t.tool_result("toolu_mso", "ok")
    return t


def test_message_start_only_placeholder(tmp_path: Path) -> None:
    r = read(tmp_path, _mso_transcript())
    req = by_message(r)["msg_mso"]
    [inf] = req.attempts[0].inferences
    assert inf.usage_source is UsageSource.MESSAGE_START_ONLY
    assert inf.usage.output == 3
    # upper = max(logged 3, median of complete tool_use calls 400, ceil(bytes / 2.5)): the tool
    # input {"command": "x" * 1000} is 1,014 JSON bytes -> ceil(405.6) = 406 tokens
    assert inf.output_upper == 406
    n = note(r, "dq.message_start_only")
    assert n.count == 1 and n.tokens == inf.output_upper - 3
    assert n.figure is None
    priced = FakePricer().price_inference(inf, ts_ms=req.ts_start_ms)
    exact = {line.bucket: line.exact for line in priced.lines}
    assert exact["output"] is False and exact["cache_read"] is True   # D27 per-line exactness


def test_message_start_only_upper_uses_content_bytes_when_larger(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_mso2", OPUS, inp=5, outputs=(3,), stop=None,
           blocks=[{"type": "tool_use", "id": "toolu_big", "name": "Write",
                    "input": {"content": "y" * 5_000}}])
    t.tool_result("toolu_big", "ok")
    r = read(tmp_path, t)
    inf = r.requests[0].attempts[0].inferences[0]
    assert inf.usage_source is UsageSource.MESSAGE_START_ONLY
    # ceil(bytes / 2.5) for a 4.7+ tokenizer: {"content": "y" * 5000} is 5,014 bytes -> 2,006
    assert inf.output_upper == 2_006


def test_message_start_only_upper_is_the_median_when_it_is_largest(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    for i, out in enumerate((100, 400, 700)):
        t.call(f"msg_cm{i}", OPUS, inp=5, outputs=(3, out), stop="tool_use")
        t.tool_result(f"toolu_cm{i}", "ok")
    t.call("msg_mso3", OPUS, inp=5, outputs=(3,), stop=None,
           blocks=[{"type": "tool_use", "id": "toolu_small", "name": "Read", "input": {}}])
    t.tool_result("toolu_small", "ok")
    inf = by_message(read(tmp_path, t))["msg_mso3"].attempts[0].inferences[0]
    assert inf.usage_source is UsageSource.MESSAGE_START_ONLY
    assert inf.output_upper == 400          # median of the complete tool_use calls 100/400/700


def test_no_stop_call_with_a_large_output_is_final(tmp_path: Path) -> None:
    r = read(tmp_path, _mso_transcript(outputs=(900,)))
    inf = by_message(r)["msg_mso"].attempts[0].inferences[0]
    assert inf.usage_source is UsageSource.FINAL and inf.output_upper is None


def test_no_stop_call_not_answered_by_a_tool_result_is_final(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_x", OPUS, inp=5, outputs=(3,), stop=None)
    t.human("next")
    r = read(tmp_path, t)
    assert r.requests[0].attempts[0].inferences[0].usage_source is UsageSource.FINAL


# --- step 5: params, diagnostics, edits --------------------------------------------------------

def test_per_turn_effort_overrides_effort(tmp_path: Path) -> None:
    t = tx(effort="medium")
    t.human("go")
    t.call("msg_eff", OPUS, inp=5, outputs=(9,), stop="end_turn",
           line_extra={"perTurnEffort": "high", "advisorModel": "claude-opus-5-5"})
    t.human("again")
    t.call("msg_eff2", OPUS, inp=5, outputs=(9,), stop="end_turn")
    reqs = by_message(read(tmp_path, t))
    assert reqs["msg_eff"].params.effort == "high"
    assert reqs["msg_eff"].params.session_effort == "medium"
    assert reqs["msg_eff"].params.advisor_model == OPUS
    assert reqs["msg_eff2"].params.effort == "medium"


def test_diagnostics_edits_and_thinking_dropped(tmp_path: Path) -> None:
    r = CC.read(_alpha(tmp_path), opts())
    req = by_message(r)["msg_06AlphaDiagnos06"]
    att = req.attempts[0]
    assert att.diagnostics is not None
    assert att.diagnostics.reason == "system_changed"
    assert att.diagnostics.provider_reason == "system_changed"
    assert att.diagnostics.missed_input_tokens_estimate == 2_100
    assert att.thinking_dropped == 2
    assert att.applied_edits == (("clear_tool_uses_20250919", 1_200),)
    assert "diagnostics" in r.capabilities


def _alpha(tmp_path: Path) -> Path:
    return bf.alpha_main().write(tmp_path / "alpha" / f"{bf.SID_ALPHA}.jsonl")


def test_unknown_diagnostic_reason_is_unavailable(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_d", OPUS, inp=5, outputs=(9,), stop="end_turn")
    path = write(tmp_path, t)
    text = path.read_text().replace('"stop_reason":"end_turn"',
                                    '"stop_reason":"end_turn","diagnostics":{"cache_miss_reason":'
                                    '{"type":"brand_new_reason","cache_missed_input_tokens":7}}')
    path.write_text(text)
    diag = CC.read(path, opts()).requests[0].attempts[0].diagnostics
    assert diag.reason == "unavailable" and diag.provider_reason == "brand_new_reason"


def test_request_id_is_kept_only_as_a_hint_and_collisions_are_noted(tmp_path: Path) -> None:
    r = CC.read(_alpha(tmp_path), opts())
    reqs = by_message(r)
    assert reqs["msg_06AlphaDiagnos06"].attempts[0].provider_request_id == "req_shared_collision"
    assert note(r, "dq.request_id_collision").count == 1
    # request ids are keyed by message.id only
    assert reqs["msg_06AlphaDiagnos06"].request_id != reqs["msg_07AlphaCollide07"].request_id


# --- step 8: events and billing path -----------------------------------------------------------

def test_quota_overage_switches_the_billing_path(tmp_path: Path) -> None:
    b = bf.beta_subscription()
    r = read(tmp_path, b, billing_path="subscription")
    reqs = by_message(r)
    path = {m: q.attempts[0].inferences[0].pricing.billing_path for m, q in reqs.items()}
    assert path == {"msg_31BetaAllowance31": "subscription",
                    "msg_32BetaOverage032": "usage_credits",
                    "msg_33BetaOverage033": "usage_credits"}
    assert reqs["msg_33BetaOverage033"].attribution.billing_path == "usage_credits"
    quota = events(r, LaneEventKind.QUOTA_STATE)
    assert [attrs(e)["using_overage"] for e in quota] == [False, True]
    assert attrs(quota[0])["resets_at_ms"] == 1_790_085_600_000
    assert note(r, "dq.subscription_allowance").count == 1
    assert "quota_state" in r.capabilities


def test_billing_path_unset_is_unknown_and_api_key_is_kept(tmp_path: Path) -> None:
    b = bf.beta_subscription()
    assert {q.attempts[0].inferences[0].pricing.billing_path
            for q in read(tmp_path, b).requests} == {"unknown"}
    assert {q.attempts[0].inferences[0].pricing.billing_path
            for q in read(tmp_path, b, billing_path="api_key").requests} == {"api_key"}


@pytest.mark.parametrize(("path", "channel"), [("bedrock", "bedrock"), ("vertex", "vertex"),
                                               ("foundry", "foundry"),
                                               ("claude_platform_aws", "claude_platform_aws")])
def test_cloud_billing_paths_select_the_channel(tmp_path: Path, path: str, channel: str) -> None:
    t = tx()
    t.human("go")
    t.call("msg_c", OPUS, inp=5, outputs=(9,), stop="end_turn")
    [req] = read(tmp_path, t, billing_path=path).requests
    assert req.attempts[0].inferences[0].pricing.channel == channel


def test_endpoint_scope_comes_from_attr(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_s", OPUS, inp=5, outputs=(9,), stop="end_turn")
    [req] = read(tmp_path, t, attr={"billing_path": "vertex",
                                    "extra": (("endpoint_scope", "global"),)}).requests
    assert req.attempts[0].inferences[0].pricing.endpoint_scope == "global"
    [req] = read(tmp_path, t).requests
    assert req.attempts[0].inferences[0].pricing.endpoint_scope == "unknown"


def test_compaction_event_and_estimated_compaction_inference(tmp_path: Path) -> None:
    r = CC.read(_alpha(tmp_path), opts())
    [ev] = events(r, LaneEventKind.COMPACTION)
    assert attrs(ev) == {"trigger": "manual", "pre_tokens": 18_000, "post_tokens": 2_100,
                         "duration_ms": 31_000, "dropped_tokens": 15_900}
    comp = [q for q in r.requests if q.lane_key.endswith("#compaction")]
    assert len(comp) == 1
    [inf] = comp[0].attempts[0].inferences
    assert inf.kind is InferenceKind.COMPACTION and inf.usage_source is UsageSource.ESTIMATED
    assert inf.billable is True
    assert inf.usage.cache_read == 18_000 and inf.usage.output == 2_100   # warm: read
    shells = {lane.lane_key: lane for s in r.sessions for lane in s.lanes}
    lane = shells[comp[0].lane_key]
    assert lane.kind is LaneKind.COMPACTION and lane.parent_lane_key == ev.lane_key
    n = note(r, "dq.hidden_compaction_estimated")
    assert n.count == 1 and n.tokens == 20_100
    priced = FakePricer().price_inference(inf, ts_ms=comp[0].ts_start_ms)
    assert priced.exact_nano == 0 and priced.estimated is not None   # never in the exact bill


def test_cold_compaction_is_a_write_at_the_lane_ttl(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_w", OPUS, inp=5, w1=4_000, outputs=(9,), stop="end_turn")
    t.system("compact_boundary", dt_ms=3_000_000, compactMetadata={
        "trigger": "auto", "preTokens": 50_000, "postTokens": 3_000})
    warm = [q for q in read(tmp_path, t).requests if q.lane_key.endswith("#compaction")][0]
    assert warm.attempts[0].inferences[0].usage.cache_read == 50_000   # 50 min < 1h TTL
    t = tx()
    t.human("go")
    t.call("msg_w", OPUS, inp=5, w1=4_000, outputs=(9,), stop="end_turn")
    t.system("compact_boundary", dt_ms=4_000_000, compactMetadata={
        "trigger": "auto", "preTokens": 50_000, "postTokens": 3_000})
    r = read(tmp_path, t)
    comp = [q for q in r.requests if q.lane_key.endswith("#compaction")][0]
    u = comp.attempts[0].inferences[0].usage
    assert u.cache_read == 0 and u.cache_write_1h == 50_000
    t2 = tx()
    t2.human("go")
    t2.call("msg_w5", OPUS, inp=5, w5=4_000, outputs=(9,), stop="end_turn")
    t2.system("compact_boundary", dt_ms=600_000, compactMetadata={"preTokens": 40_000})
    comp = [q for q in read(tmp_path, t2).requests if q.lane_key.endswith("#compaction")][0]
    assert comp.attempts[0].inferences[0].usage.cache_write_5m == 40_000
    [ev] = events(read(tmp_path, t2), LaneEventKind.COMPACTION)
    assert attrs(ev) == {"pre_tokens": 40_000}


def test_fallback_error_attachment_human_and_upgrade_events(tmp_path: Path) -> None:
    r = CC.read(_alpha(tmp_path), opts())
    [fb] = events(r, LaneEventKind.MODEL_FALLBACK)          # block + system entry: one event
    assert attrs(fb) == {"from_model": "claude-fable-5", "to_model": "claude-opus-4-8",
                         "trigger": "refusal"}
    [err] = events(r, LaneEventKind.API_ERROR)
    assert attrs(err) == {"status": 529, "error_type": "overloaded", "retry_attempt": 1,
                          "max_retries": 10, "retry_in_ms": 2_000}
    inj = events(r, LaneEventKind.CONTEXT_INJECTION)
    assert [attrs(e)["att_type"] for e in inj] == ["new_file", "todo"]
    assert all(set(attrs(e)) == {"att_type", "n_bytes"} and attrs(e)["n_bytes"] > 0
               for e in inj)
    # human prompts: origin human, no origin (plain), never isMeta / isCompactSummary / results
    assert len(events(r, LaneEventKind.HUMAN_PROMPT)) == 5
    [up] = events(r, LaneEventKind.UPGRADE)
    assert attrs(up) == {"from_version": "2.1.270", "to_version": "2.1.271"}
    [cost] = events(r, LaneEventKind.COST_STATE)
    assert attrs(cost) == {"reported_total_nano": 123_456_789,
                           "reporter": "claude_code.cost_state"}


@pytest.mark.parametrize(("error", "expected"), [
    ({"status": 429}, "rate_limit"), ({"status": 529}, "overloaded"),
    ({"connection": True}, "connection"), ({"status": 500}, "server_error"),
    ({"status": 418}, "other"), ({"error": {"type": "overloaded_error"}}, "overloaded")])
def test_api_error_type_from_status(tmp_path: Path, error: dict, expected: str) -> None:
    t = tx()
    t.system("api_error", error=dict(error, message="secret text"))
    [ev] = events(read(tmp_path, t), LaneEventKind.API_ERROR)
    assert attrs(ev)["error_type"] == expected


def test_human_prompt_detection_rules(tmp_path: Path) -> None:
    t = tx()
    t.human("with origin")                                     # origin.kind human
    t.human("plain", origin=False)                             # no origin, not meta
    t.meta_user("meta")                                        # isMeta
    t.add(t.base("user", 10, origin={"kind": "task-notification"},
                 message={"role": "user", "content": "bg"}))   # origin, not human
    t.add(t.base("user", 10, isCompactSummary=True,
                 message={"role": "user", "content": "summary"}))
    t.call("msg_h", OPUS, inp=5, outputs=(9,), stop="tool_use")
    t.tool_result("toolu_h", "result")                         # tool_result, no origin
    r = read(tmp_path, t)
    assert len(events(r, LaneEventKind.HUMAN_PROMPT)) == 2
    assert "human_prompts" in r.capabilities


def test_compact_summary_is_not_appended_user_text(tmp_path: Path) -> None:
    """R-E33 (gate-1 fixup 2): an ``isCompactSummary`` user entry is neither a HUMAN_PROMPT nor a
    ``user_text`` appended item of the next request; human text alongside it still is."""
    t = tx()
    t.human("hello")
    t.call("msg_a", OPUS, inp=5, outputs=(9,), stop="end_turn")
    t.system("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 9_000,
                                                  "postTokens": 700, "durationMs": 1_000})
    t.add(t.base("user", 10, isCompactSummary=True, isVisibleInTranscriptOnly=True,
                 message={"role": "user", "content": "summary " * 40}))
    t.call("msg_b", OPUS, inp=5, outputs=(9,), stop="end_turn")
    t.add(t.base("user", 10, isCompactSummary=True,
                 message={"role": "user", "content": [{"type": "text", "text": "sum"}]}))
    t.human("next")
    t.call("msg_c", OPUS, inp=5, outputs=(9,), stop="end_turn")
    r = read(tmp_path, t)
    reqs = by_message(r)
    assert [(a.kind, a.n_bytes) for a in reqs["msg_a"].appended] == [("user_text", 5)]
    assert reqs["msg_b"].appended == ()
    assert [(a.kind, a.n_bytes) for a in reqs["msg_c"].appended] == [("user_text", 4)]
    assert len(events(r, LaneEventKind.HUMAN_PROMPT)) == 2
    assert len(events(r, LaneEventKind.COMPACTION)) == 1


def test_appended_items_are_sizes_only(tmp_path: Path) -> None:
    t = tx()
    t.human("hello")
    t.call("msg_a", OPUS, inp=5, outputs=(3, 60), stop="tool_use",
           blocks=[{"type": "text", "text": "t"},
                   {"type": "tool_use", "id": "toolu_a", "name": "mcp__github__list_issues",
                    "input": {}}])
    t.tool_result("toolu_a", "héllo", is_error=True, images=2)
    t.attachment("new_file", {"x": 1})
    t.call("msg_b", OPUS, inp=5, outputs=(9,), stop="end_turn")
    r = read(tmp_path, t)
    reqs = by_message(r)
    assert [(a.kind, a.name, a.n_bytes) for a in reqs["msg_a"].appended] == [
        ("user_text", None, 5)]
    tool, att = reqs["msg_b"].appended
    assert tool.kind == "tool_result" and tool.n_bytes == len("héllo".encode())
    assert tool.is_error is True and tool.images == 2
    assert tool.name.startswith("h_")           # MCP tool names are pseudonymous
    assert (att.kind, att.name, att.n_bytes) == ("attachment", "new_file", len('{"x":1}'))


def test_builtin_tool_names_are_clear(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_bash", OPUS, inp=5, outputs=(3, 60), stop="tool_use")
    t.tool_result("toolu_bash", "out")
    t.call("msg_next", OPUS, inp=5, outputs=(9,), stop="end_turn")
    [item] = by_message(read(tmp_path, t))["msg_next"].appended
    assert item.name == "Bash"


# --- step 10: attribution -----------------------------------------------------------------------

def test_attribution_fields(tmp_path: Path) -> None:
    r = CC.read(_alpha(tmp_path), opts(attr={"team": "payments", "workspace_id": "wrk_1"}))
    a = by_message(r)["msg_01AlphaSplit0001"].attribution
    assert a.agent_product == "claude_code" and a.query_source == "main"
    assert a.team == "payments" and a.client_version == "2.1.270" and a.entrypoint == "cli"
    assert a.cwd_key.startswith("h_") and a.skill.startswith("h_")
    assert a.mcp_server.startswith("h_")
    assert r.sessions[0].lanes[0].cache_scope_key == "ws:wrk_1"
    blob = canonical(r)
    assert "feature/" not in blob and "gitBranch" not in blob and "/home/dev" not in blob


def test_version_histogram_and_unknown_entry_types(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_v1", OPUS, inp=5, outputs=(9,), stop="end_turn")
    t.version = "2.1.280"
    t.human("go")
    t.call("msg_v2", OPUS, inp=5, outputs=(9,), stop="end_turn")
    t.add({"type": "brand-new-thing", "x": 1})
    t.add({"type": "summary", "summary": "s", "leafUuid": "u"})
    r = read(tmp_path, t)
    hist = note(r, "dq.version_histogram")
    assert hist.count == 2 and "2.1.270=1" in hist.detail and "2.1.280=1" in hist.detail
    unknown = note(r, "dq.unknown_entry_type")
    assert unknown.count == 1 and "brand-new-thing=1" in unknown.detail


def test_retention_warning_uses_the_injected_clock(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_old", OPUS, inp=5, outputs=(9,), stop="end_turn")
    path = write(tmp_path, t)
    set_mtime(path, NOW_MS - 28 * 86_400_000)
    assert note(CC.read(path, opts()), "dq.retention_warning").count == 1
    set_mtime(path, NOW_MS - 26 * 86_400_000)
    assert note(CC.read(path, opts()), "dq.retention_warning") is None
    assert note(CC.read(path, opts(now_ms=0)), "dq.retention_warning") is None


# --- step 11: lanes -------------------------------------------------------------------------------

def test_subagent_and_workflow_lanes(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    r = CC.read(root / "projects", opts())
    lanes = {lane.lane_key: lane for s in r.sessions for lane in s.lanes}
    main = [x for x in lanes.values() if x.kind is LaneKind.MAIN]
    subs = [x for x in lanes.values() if x.kind is LaneKind.SUBAGENT]
    wfs = [x for x in lanes.values() if x.kind is LaneKind.WORKFLOW_AGENT]
    alpha_main = next(x for x in main if x.session_key == subs[0].session_key)
    assert len(subs) == 1 and len(wfs) == 1
    assert subs[0].parent_lane_key == alpha_main.lane_key
    assert wfs[0].parent_lane_key == alpha_main.lane_key
    sub_reqs = [q for q in r.requests if q.lane_key == subs[0].lane_key]
    assert {q.attribution.query_source for q in sub_reqs} == {"subagent"}
    assert {q.attribution.agent_type for q in sub_reqs} == {"Explore"}
    metas = [e for e in r.events if e.kind is LaneEventKind.SESSION_META]
    assert {(attrs(e)["agent_type"], attrs(e)["spawn_depth"]) for e in metas} == {
        ("Explore", 1), ("workflow-subagent", 2)}
    assert all(lane.lane_exact for lane in lanes.values())


def test_in_file_sidechain_entries_get_a_subagent_lane(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_main", OPUS, inp=5, outputs=(9,), stop="end_turn")
    t.agent_id = "side1"
    t.call("msg_side", "claude-sonnet-5", inp=5, outputs=(9,), stop="end_turn")
    r = read(tmp_path, t)
    lanes = {lane.lane_key: lane.kind for s in r.sessions for lane in s.lanes}
    reqs = by_message(r)
    assert lanes[reqs["msg_side"].lane_key] is LaneKind.SUBAGENT
    assert lanes[reqs["msg_main"].lane_key] is LaneKind.MAIN


def test_session_and_lane_keys_are_stable_ids(tmp_path: Path) -> None:
    from tokenbill.core.ids import stable_id

    t = tx()
    t.human("go")
    t.call("msg_k", OPUS, inp=5, outputs=(9,), stop="end_turn")
    [req] = read(tmp_path, t).requests
    sid = "11111111-0000-4000-8000-000000000001"
    assert req.session_key == stable_id("ses", "claude-code", sid)
    assert req.lane_key == stable_id("ln", sid, "main")
    assert req.request_id == stable_id("rq", "anthropic", "msg_k")


# --- robustness ---------------------------------------------------------------------------

def test_truncated_last_line_is_quarantined_and_the_rest_imported(tmp_path: Path) -> None:
    path = bf.truncated_case().write(tmp_path / "t.jsonl")
    r = CC.read(path, opts())
    assert [q.reason for q in r.quarantined] == ["bad_json"]
    assert len(r.requests) == 1
    assert note(r, "dq.quarantined").count == 1


def test_same_file_twice_gives_identical_results(tmp_path: Path) -> None:
    path = _alpha(tmp_path)
    assert canonical(CC.read(path, opts())) == canonical(CC.read(path, opts()))


def test_malformed_records_are_quarantined_with_content_free_reasons(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.raw("[1, 2]")
    t.raw('{"type": "assistant", "message": "nope", "uuid": "u1"}')
    t.raw('{"type": "assistant", "message": {"model": "m", "usage": {}}, "uuid": "u2"}')
    t.raw('{"type": "assistant", "message": {"id": "msg_q", "model": "m"}, "uuid": "u3"}')
    t.raw('{"type": "assistant", "message": {"id": "msg_q2", "model": "m", '
          '"usage": {"input_tokens": -4}}, "uuid": "u4"}')
    t.raw("{not json")
    r = read(tmp_path, t)
    assert sorted(q.reason for q in r.quarantined) == sorted(
        ["not_object", "missing:message", "missing:message.id", "missing:message.usage",
         "bad_usage", "bad_json"])
    assert all(q.locator.startswith("offset:") for q in r.quarantined)


def test_group_without_any_timestamp_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text('{"type":"assistant","sessionId":"s","uuid":"u","message":{"id":"msg_nt",'
                    '"model":"claude-opus-5-5","usage":{"input_tokens":1,"output_tokens":2},'
                    '"stop_reason":"end_turn"}}\n')
    r = CC.read(path, opts())
    assert r.requests == [] and [q.reason for q in r.quarantined] == ["missing:timestamp"]


def test_strict_mode_raises_on_the_first_bad_record(tmp_path: Path) -> None:
    path = bf.truncated_case().write(tmp_path / "t.jsonl")
    with pytest.raises(SourceError):
        CC.read(path, opts(lenient=False))


def test_fingerprint_tier_is_refused(tmp_path: Path) -> None:
    path = _alpha(tmp_path)
    with pytest.raises(UsageError):
        CC.read(path, opts(content_tier="fingerprint"))
    with pytest.raises(UsageError):
        CC.read(path, opts(content_tier="full"))


def test_directory_read_equals_the_union_of_files(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    whole = CC.read(root / "projects", opts())
    ids = set()
    from tokenbill.adapters.claude_code import iter_claude_files
    for f in iter_claude_files(root / "projects"):
        ids |= {q.request_id for q in CC.read(f, opts()).requests}
    assert {q.request_id for q in whole.requests} == ids
    assert whole.source.adapter == "claude-code" and whole.source.bytes > 0


def test_iter_claude_files_skips_journal_and_meta(tmp_path: Path) -> None:
    from tokenbill.adapters.claude_code import iter_claude_files

    root = tmp_path / "tree"
    bf.build(root)
    names = [p.name for p in iter_claude_files(root / "projects")]
    assert "journal.jsonl" not in names and not any(n.endswith(".json") for n in names)
    assert len(names) == 4
    assert list(iter_claude_files(root / "missing")) == []
    one = next(iter_claude_files(root / "projects"))
    assert list(iter_claude_files(one)) == [one]


def test_sniff(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    main = root / "projects" / "-home-dev-alpha" / f"{bf.SID_ALPHA}.jsonl"
    assert CC.sniff(main, main.read_bytes()[:65536]) is True
    for p in (root / "headless").iterdir():
        assert CC.sniff(p, p.read_bytes()[:65536]) is False
    journal = root / "projects" / "-home-dev-alpha" / "journal.jsonl"
    assert CC.sniff(journal, journal.read_bytes()) is False
    assert CC.sniff(tmp_path / "x.txt", b'{"type":"user","sessionId":"s"}') is False
    assert CC.sniff(main, b"garbage\n\x00\xff") is False


def test_since_until_window(tmp_path: Path) -> None:
    path = _alpha(tmp_path)
    full = CC.read(path, opts())
    cut = sorted(q.ts_start_ms for q in full.requests)[3]
    part = CC.read(path, opts(since_ms=cut))
    assert all(q.ts_start_ms >= cut for q in part.requests)
    assert 0 < len(part.requests) < len(full.requests)
    assert all(e.ts_ms >= cut for e in part.events)


# --- edge paths -------------------------------------------------------------------------------

def test_duplicate_window_is_the_last_2000_uuids(tmp_path: Path) -> None:
    from tokenbill.adapters.claude_code import UUID_WINDOW

    t = tx()
    t.human("first")
    first = t.lines[-1]
    for i in range(UUID_WINDOW + 5):
        t.human(f"filler {i}")
    t.raw(first)                      # beyond the window: kept (bounded memory, same as cursor)
    t.duplicate_last()                # right after: dropped
    r = read(tmp_path, t)
    assert note(r, "dq.duplicate_uuid_lines").count == 1


def test_first_line_without_timestamp_takes_a_later_one(tmp_path: Path) -> None:
    t = tx()
    line = t.assistant_line("msg_nots", OPUS, bf.usage(5, out=3), None)
    t.lines[-1] = t.lines[-1].replace(f'"timestamp":"{line["timestamp"]}",', "")
    t.assistant_line("msg_nots", OPUS, bf.usage(5, out=30), None, stop="end_turn")
    [req] = read(tmp_path, t).requests
    assert req.ts_start_ms == t.t


def test_quota_reset_formats_and_unset_overage(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    for i, resets in enumerate((1_790_085_600.5, "2026-09-22T14:00:00.000Z", 1_790_085_600_000,
                                None)):
        t.call(f"msg_q{i}", OPUS, inp=5, outputs=(9,), stop="end_turn",
               line_extra={"quotaLimits": {"status": f"s{i}", "resetsAt": resets,
                                           "isUsingOverage": "yes"}})
        t.human("next")
    r = read(tmp_path, t, billing_path="subscription")
    resets = [attrs(e)["resets_at_ms"] for e in events(r, LaneEventKind.QUOTA_STATE)]
    assert resets == [1_790_085_600_500, 1_790_085_600_000, 1_790_085_600_000, None]
    assert {q.attempts[0].inferences[0].pricing.billing_path for q in r.requests} == {
        "subscription"}                                 # "yes" is not a bool: no overage


def test_pasted_images_and_text_blocks_in_user_entries(tmp_path: Path) -> None:
    t = tx()
    t.add(t.base("user", 10, message={"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "data": "QUJD"}}, "junk"]}))
    t.call("msg_img", OPUS, inp=5, outputs=(9,), stop="end_turn")
    [req] = read(tmp_path, t).requests
    assert [(a.kind, a.n_bytes, a.images) for a in req.appended] == [
        ("user_text", 4, 0), ("image", 4, 1)]


def test_cost_state_without_a_known_amount_is_counted(tmp_path: Path) -> None:
    t = tx()
    t.add(t.base("cost-state", 10, spend="n/a"))
    t.add(t.base("cost-state", 10, totalCostUSD=-1))
    r = read(tmp_path, t)
    assert events(r, LaneEventKind.COST_STATE) == []
    assert note(r, "dq.unknown_fields").count == 2


def test_compaction_without_pre_tokens_is_only_an_event(tmp_path: Path) -> None:
    t = tx()
    t.system("compact_boundary", compactMetadata={"trigger": "weird", "postTokens": 5})
    r = read(tmp_path, t)
    assert r.requests == []
    [ev] = events(r, LaneEventKind.COMPACTION)
    assert attrs(ev) == {"post_tokens": 5}


def test_unknown_model_is_unpriced_and_noted(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_alias", "opus", inp=5, outputs=(9,), stop="end_turn")
    t.system("compact_boundary", compactMetadata={"preTokens": 10})
    r = read(tmp_path, t)
    assert {q.attempts[0].inferences[0].pricing.model for q in r.requests} == {""}
    assert note(r, "dq.unpriced_model").count == 2
    assert r.naive_usage == {}


def test_bedrock_model_ids_hint_the_channel(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_br", "global.anthropic.claude-opus-5-5", inp=5, outputs=(9,), stop="end_turn")
    [req] = read(tmp_path, t).requests
    ctx = req.attempts[0].inferences[0].pricing
    assert (ctx.channel, ctx.model, ctx.endpoint_scope) == ("bedrock", OPUS, "global")


def test_gzip_transcripts_are_read(tmp_path: Path) -> None:
    import gzip

    path = tmp_path / "s.jsonl.gz"
    path.write_bytes(gzip.compress(bf.alpha_main().text().encode()))
    assert CC.sniff(path, gzip.decompress(path.read_bytes())[:65536]) is True
    assert len(CC.read(path, opts()).requests) == 8


def test_secrets_are_counted_by_type_only(tmp_path: Path) -> None:
    t = tx()
    t.human("my key is sk-ant-" + "Q" * 30)
    t.call("msg_sec", OPUS, inp=5, outputs=(3, 9), stop="tool_use")
    t.tool_result("toolu_sec", "AKIA" + "ABCDEFGHIJKLMNOP")
    r = read(tmp_path, t)
    n = note(r, "dq.secrets_observed")
    assert n.count == 2 and "anthropic_key=1" in n.detail and "aws_access_key=1" in n.detail
    assert "QQQQ" not in canonical(r) and "AKIA" not in canonical(r)


def test_read_of_a_missing_path_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        CC.read(tmp_path / "nope.jsonl", opts())
    with pytest.raises(UsageError):
        CC.read(tmp_path, "not options")  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        CC.read(tmp_path, opts(content_tier="bogus"))


def test_sniff_accepts_a_first_line_longer_than_the_head(tmp_path: Path) -> None:
    import json as _json

    # Claude Code writes keys in insertion order: sessionId and type precede the message
    line = _json.dumps({"parentUuid": None, "sessionId": "s1", "type": "user",
                        "message": {"role": "user", "content": "x" * 100_000}, "uuid": "u1"},
                       separators=(",", ":"))
    path = tmp_path / "s1.jsonl"
    path.write_text(line + "\n")
    assert CC.sniff(path, path.read_bytes()[:65536]) is True
    assert CC.sniff(path, b'{"session_id": "s", "type": "user", "sessionId": 1') is False


def test_workflow_rollup_files_are_counted_never_read(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    wf = root / "projects" / "-home-dev-alpha" / bf.SID_ALPHA / "workflows" / "run-1"
    (wf / "run.json").write_text('{"totalTokens": 999999}')
    r = CC.read(root / "projects", opts())
    assert note(r, "dq.rollup_not_spend").count == 2      # 1 toolUseResult + 1 roll-up file
    assert sum(i.usage.total_input for q in r.requests for i in q.attempts[0].inferences) < 999999


def test_lane_kind_ignores_distant_ancestors_named_like_containers() -> None:
    from tokenbill.adapters.claude_code import file_layout

    main = Path("/Users/workflows/.claude/projects/-home-x/abc.jsonl")
    assert file_layout(main).kind is LaneKind.MAIN
    sub = Path("/Users/workflows/.claude/projects/-home-x/abc/subagents/agent-7.jsonl")
    lay = file_layout(sub)
    assert (lay.kind, lay.agent_id, lay.session_hint) == (LaneKind.SUBAGENT, "7", "abc")
    wf = Path("/Users/me/.claude/projects/-home-x/abc/workflows/run-2/agent-w.jsonl")
    lay = file_layout(wf)
    assert (lay.kind, lay.agent_id, lay.session_hint) == (LaneKind.WORKFLOW_AGENT, "w", "abc")


# --- review fixes --------------------------------------------------------------------------------

def test_far_reappearing_message_stays_one_request(tmp_path: Path) -> None:
    """A late split line of a message emitted long before (beyond the closed-group window) re-opens
    that request instead of emitting a second one: one request per message id, the first
    appearance's start and appended items, nothing taken from the next request, counted once."""
    from tokenbill.adapters.claude_code import CLOSED_WINDOW

    t = tx()
    t.human("go")
    trigger = t.t
    t.call("msg_far", OPUS, inp=10, w5=100, outputs=(3, 250), stop="tool_use")
    t.tool_result("toolu_far", "ok")
    for i in range(CLOSED_WINDOW + 4):
        t.call(f"msg_f{i:03d}", OPUS, inp=10, outputs=(3, 25), stop="tool_use")
        t.tool_result(f"toolu_f{i:03d}", "ok")
    t.assistant_line("msg_far", OPUS, bf.usage(10, 0, 100, 0, 470), {"type": "text", "text": "x"},
                     stop="end_turn")
    t.call("msg_after", OPUS, inp=10, outputs=(3, 9), stop="end_turn")
    t.human("done")
    r = read(tmp_path, t)
    ids = [q.request_id for q in r.requests]
    assert len(ids) == len(set(ids)) == CLOSED_WINDOW + 6
    far = by_message(r)["msg_far"]
    assert far.attempts[0].inferences[0].usage.output == 470
    assert far.ts_start_ms == trigger
    assert [a.kind for a in far.appended] == ["user_text"]
    assert [a.kind for a in by_message(r)["msg_after"].appended] == ["tool_result"]
    assert note(r, "dq.version_histogram").detail.endswith(f"2.1.270={CLOSED_WINDOW + 6}")
    assert r.naive_usage[OPUS].output == 3 + 250 + 470 + (CLOSED_WINDOW + 4) * 28 + 12


def test_reopened_message_keeps_its_first_billing_path(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_bp", OPUS, inp=10, outputs=(3, 250), stop="tool_use")
    t.tool_result("toolu_bp", "ok")
    t.call("msg_q", OPUS, inp=10, outputs=(3, 9), stop="end_turn",
           line_extra={"quotaLimits": {"status": "allowed_warning", "isUsingOverage": True}})
    t.assistant_line("msg_bp", OPUS, bf.usage(10, out=470), {"type": "text", "text": "x"},
                     stop="end_turn")
    t.human("next")
    r = read(tmp_path, t, billing_path="subscription")
    reqs = by_message(r)
    late = reqs["msg_bp"]
    assert late.attempts[0].inferences[0].usage.output == 470
    assert late.attempts[0].inferences[0].pricing.billing_path == "subscription"
    assert late.attribution.billing_path == "subscription"
    assert reqs["msg_q"].attempts[0].inferences[0].pricing.billing_path == "usage_credits"
    assert note(r, "dq.subscription_allowance").count == 1


def test_naive_usage_covers_the_since_until_window(tmp_path: Path) -> None:
    t = tx()
    t.human("a")
    t.call("msg_n1", OPUS, inp=100, outputs=(3, 50), stop="end_turn")
    t.human("b", dt_ms=3_600_000)
    cut = t.t
    t.call("msg_n2", OPUS, inp=7, outputs=(3, 60), stop="end_turn")
    r = read(tmp_path, t, since_ms=cut)
    assert set(by_message(r)) == {"msg_n2"}
    assert r.naive_usage[OPUS].uncached_input == 2 * 7
    assert read(tmp_path, t, until_ms=cut).naive_usage[OPUS].uncached_input == 2 * 100


def test_nested_subagent_links_to_the_main_lane(tmp_path: Path) -> None:
    import json as _json

    from tokenbill.core.ids import stable_id

    sid = "11111111-0000-4000-8000-000000000001"
    sub = bf.Tx(sid, agent_id="child1")
    sub.human("sub", origin=False)
    sub.call("msg_nest", OPUS, inp=5, outputs=(3, 9), stop="end_turn")
    folder = tmp_path / "projects" / "-x" / sid / "subagents"
    path = sub.write(folder / "agent-child1.jsonl")
    (folder / "agent-child1.meta.json").write_text(_json.dumps(
        {"agentType": "Explore", "parentAgentId": "parent9", "spawnDepth": 2}))
    r = CC.read(path, opts())
    [req] = r.requests
    lanes = {lane.lane_key: lane for s in r.sessions for lane in s.lanes}
    assert lanes[req.lane_key].kind is LaneKind.SUBAGENT
    assert lanes[req.lane_key].parent_lane_key == stable_id("ln", sid, "main")


def test_directory_read_locators_distinguish_same_named_files(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    sid = "11111111-0000-4000-8000-000000000001"
    for run in ("run-1", "run-2"):
        t = bf.Tx(sid, agent_id="w")
        t.human("go", origin=False)
        t.raw("{broken")
        t.call("msg_" + run.replace("-", ""), OPUS, inp=5, outputs=(3, 9), stop="end_turn")
        t.write(root / "-x" / sid / "workflows" / run / "agent-w.jsonl")
    r = CC.read(root, opts())
    assert len({q.locator for q in r.quarantined}) == 2
    assert len({q.source.locator for q in r.requests}) == 2


def test_session_attribution_carries_the_configured_billing_path(tmp_path: Path) -> None:
    t = tx()
    t.human("go")
    t.call("msg_sb", OPUS, inp=5, outputs=(9,), stop="end_turn")
    r = read(tmp_path, t, billing_path="subscription")
    assert r.sessions[0].attribution.billing_path == "subscription"
    assert read(tmp_path, t).sessions[0].attribution.billing_path is None


def test_rewritten_history_reopens_each_message_once(tmp_path: Path) -> None:
    """Every message re-written later with fresh uuids (beyond the recent-request scan, so the
    id → request map is used): still one request per message, at its first position and start."""
    from tokenbill.adapters.claude_code import _RECENT_SCAN

    n = _RECENT_SCAN + 144
    t = tx()
    t.human("go")
    starts = {}
    for i in range(n):
        starts[f"msg_w{i:04d}"] = t.t
        t.call(f"msg_w{i:04d}", OPUS, inp=10, outputs=(3, 25), stop="tool_use")
        t.tool_result(f"toolu_w{i:04d}", "ok")
    for i in range(n):
        t.assistant_line(f"msg_w{i:04d}", OPUS, bf.usage(10, out=30), None, stop="end_turn")
        t.human("x", origin=False)
    r = read(tmp_path, t)
    reqs = by_message(r)
    assert len(r.requests) == len(reqs) == n
    assert [q.attempts[0].provider_message_id for q in r.requests] == sorted(reqs)
    assert all(q.attempts[0].inferences[0].usage.output == 30 for q in r.requests)
    assert all(reqs[m].ts_start_ms == ts for m, ts in starts.items())
