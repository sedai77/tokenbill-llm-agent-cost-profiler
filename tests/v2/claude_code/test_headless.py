"""Headless / CI / Agent SDK streams (SPEC §5.12, D33; brief CC)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.adapters.cc_headless import load_messages
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.labels import Evidence
from tokenbill.core.records import InferenceKind, LaneEventKind, LaneKind, WorkloadClass
from tokenbill.core.testing import FakePricer

from .helpers import HEADLESS, attrs, bf, by_message, canonical, events, note, opts, read_headless

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"


def _residual(r: object) -> object:
    [req] = [q for q in r.requests if not q.attempts[0].provider_message_id]
    return req


def test_parallel_tool_calls_sharing_an_id_count_once(tmp_path: Path) -> None:
    r = read_headless(tmp_path, bf.headless_stream().jsonl())
    steps = by_message(r)
    assert set(steps) == {"msg_h01Parallel", "msg_h02Agent", "msg_h03Sub", "msg_h04Final"}
    assert steps["msg_h01Parallel"].attempts[0].inferences[0].usage.cache_write_5m == 9_000


def test_output_residual_per_model_priced_exact(tmp_path: Path) -> None:
    r = read_headless(tmp_path, bf.headless_stream().jsonl())
    req = _residual(r)
    by_model = {i.pricing.model: i for i in req.attempts[0].inferences}
    assert set(by_model) == {OPUS, SONNET}
    assert by_model[OPUS].usage.output == 1_400 - 4       # modelUsage − Σ logged step outputs
    assert by_model[SONNET].usage.output == 600 - 1
    for inf in by_model.values():
        assert inf.kind is InferenceKind.OUTPUT_RESIDUAL
        assert inf.usage.total_input == 0
        priced = FakePricer().price_inference(inf, ts_ms=req.ts_start_ms)
        assert priced.figure.evidence is Evidence.EXACT and priced.estimated is None
    assert req.serving_inference is None                  # never enters transitions
    main = next(lane.lane_key for s in r.sessions for lane in s.lanes
                if lane.kind is LaneKind.MAIN)
    assert req.lane_key == main


def test_subagent_steps_get_their_own_lane(tmp_path: Path) -> None:
    r = read_headless(tmp_path, bf.headless_stream().jsonl())
    lanes = {lane.lane_key: lane for s in r.sessions for lane in s.lanes}
    sub = by_message(r)["msg_h03Sub"]
    assert lanes[sub.lane_key].kind is LaneKind.SUBAGENT
    assert lanes[sub.lane_key].parent_lane_key == by_message(r)["msg_h01Parallel"].lane_key
    assert sub.attribution.query_source == "subagent"
    assert not any(lane.lane_exact for lane in lanes.values())   # no timestamps
    assert "timing" not in r.capabilities


def test_total_cost_is_only_a_cost_state_event(tmp_path: Path) -> None:
    r = read_headless(tmp_path, bf.headless_stream().jsonl())
    [ev] = events(r, LaneEventKind.COST_STATE)
    assert attrs(ev) == {"reported_total_nano": 421_000_000, "reporter": "claude_code.headless"}
    for req in r.requests:
        for inf in req.attempts[0].inferences:
            assert inf.provider_reported_cost_nano is None
    assert r.aggregates == []


def _resumed(inflate: int) -> str:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-00000000abcd")
    hx.assistant("msg_r1", OPUS, bf.usage(10, 0, 1_000, 0, 1))
    hx.assistant("msg_r2", OPUS, bf.usage(10, 1_000, 50, 0, 1), stop="end_turn")
    hx.result(bf.model_usage(OPUS, 20 + inflate, 400, read=1_000, write=1_050))
    return hx.jsonl()


def test_resumed_session_totals_skip_the_residual(tmp_path: Path) -> None:
    r = read_headless(tmp_path, _resumed(inflate=5_000))
    assert all(q.attempts[0].provider_message_id for q in r.requests)
    assert note(r, "dq.headless_resumed_totals").count == 1
    ok = read_headless(tmp_path, _resumed(inflate=0))
    assert _residual(ok).attempts[0].inferences[0].usage.output == 398


def test_zeroed_result_is_skipped(tmp_path: Path) -> None:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-00000000dead")
    hx.assistant("msg_z1", OPUS, bf.usage(10, 0, 1_000, 0, 1), stop="end_turn")
    hx.result(bf.model_usage(OPUS, 0, 0, cost="0"), subtype="error_during_execution",
              is_error=True, cost="0")
    r = read_headless(tmp_path, hx.jsonl())
    assert len(r.requests) == 1
    assert note(r, "dq.headless_zeroed_result").count == 1


def test_negative_residual_is_dropped(tmp_path: Path) -> None:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-00000000ne9a")
    hx.assistant("msg_n1", OPUS, bf.usage(10, 0, 1_000, 0, 900), stop="end_turn")
    hx.result(bf.model_usage(OPUS, 10, 100, write=1_000))
    r = read_headless(tmp_path, hx.jsonl())
    n = note(r, "dq.headless_output_residual")
    assert n.count == 1 and n.tokens == 800 and n.severity == "warn"
    assert len(r.requests) == 1


def test_without_model_usage_the_main_lane_residual_uses_result_usage(tmp_path: Path) -> None:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-00000000nomu")
    hx.assistant("msg_m1", OPUS, bf.usage(10, 0, 1_000, 0, 2))
    hx.assistant("msg_m2", SONNET, bf.usage(5, 0, 500, 0, 1), parent="toolu_x")
    hx.assistant("msg_m3", OPUS, bf.usage(10, 1_000, 40, 0, 2), stop="end_turn")
    hx.result(None, out=300, inp=20, read=1_000, write=1_040)
    r = read_headless(tmp_path, hx.jsonl())
    [inf] = _residual(r).attempts[0].inferences
    assert inf.pricing.model == OPUS and inf.usage.output == 300 - 4   # subagents excluded


def test_array_and_jsonl_forms_parse_identically(tmp_path: Path) -> None:
    hx = bf.headless_execution_file()
    arr = read_headless(tmp_path, hx.array(), name="claude-execution-output.json")
    lines = read_headless(tmp_path, hx.jsonl(), name="claude-execution-output.jsonl")
    strip = [(q.request_id, [i.usage for i in q.attempts[0].inferences]) for q in arr.requests]
    assert strip == [(q.request_id, [i.usage for i in q.attempts[0].inferences])
                     for q in lines.requests]
    assert _residual(arr).attempts[0].inferences[0].usage.output == 517


def test_truncated_array_keeps_complete_messages(tmp_path: Path) -> None:
    text = bf.headless_execution_file().array()
    cut = text[: text.rfind(",\n") + 32]          # the result message is cut mid-object
    r = read_headless(tmp_path, cut, name="x.json")
    assert set(by_message(r)) == {"msg_e01Step", "msg_e02Step"}
    assert [q.reason for q in r.quarantined] == ["bad_json"]


def test_json_output_alone_is_an_aggregate_only(tmp_path: Path) -> None:
    text = json.dumps(bf.headless_result_only(), default=lambda o: float(o.literal), indent=2)
    r = read_headless(tmp_path, text, name="result.json")
    assert r.requests == [] and r.events == []
    [agg] = r.aggregates
    assert agg.source_kind == "claude_code.headless_result"
    assert dict(agg.dims) == {"channel": "anthropic_api", "model": OPUS}
    assert (agg.usage.uncached_input, agg.usage.cache_read, agg.usage.cache_write_unknown,
            agg.usage.output) == (30, 40_000, 5_000, 800)
    assert agg.reported_cost_basis == "provider_estimate"
    assert agg.reported_cost_nano == 234_500_000


def test_json_output_without_model_usage(tmp_path: Path) -> None:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-0000000c4c4c")
    msg = hx.result(None, out=10, inp=3, read=100, write=5, cost="0.01")
    r = read_headless(tmp_path, bf._dumps_raw(msg), name="r.json")
    [agg] = r.aggregates
    assert dict(agg.dims) == {"channel": "anthropic_api"}
    assert agg.usage.output == 10 and agg.reported_cost_nano == 10_000_000


def test_timestamps_when_present(tmp_path: Path) -> None:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-0000000c5c5c", timestamps=True)
    hx.assistant("msg_t1", OPUS, bf.usage(3, 0, 100, 0, 1), [
        {"type": "tool_use", "id": "toolu_t", "name": "Bash", "input": {}}])
    hx.tool_result("toolu_t", "ok")
    hx.assistant("msg_t2", OPUS, bf.usage(3, 100, 10, 0, 1), stop="end_turn")
    r = read_headless(tmp_path, hx.jsonl())
    assert "timing" in r.capabilities
    steps = by_message(r)
    assert steps["msg_t2"].ts_start_ms == bf.T0_MS + 2_000       # the preceding tool_result
    assert steps["msg_t1"].ts_start_ms == bf.T0_MS + 1_000       # no trigger: its own time
    assert steps["msg_t2"].attempts[0].duration_ms == 1_000
    assert all(lane.lane_exact for s in r.sessions for lane in s.lanes)


def test_ci_attribution_comes_from_options(tmp_path: Path) -> None:
    r = read_headless(tmp_path, bf.headless_stream().jsonl(),
                      attr={"workload_class": "ci", "entrypoint": "claude-code-github-action",
                            "team": "platform", "extra": (("run_attempt", "2"),)})
    a = by_message(r)["msg_h01Parallel"].attribution
    assert a.workload_class is WorkloadClass.CI and a.team == "platform"
    assert a.entrypoint == "claude-code-github-action" and a.agent_product == "claude_code"
    assert dict(a.extra) == {"run_attempt": "2"}
    assert "workload" in r.capabilities


def test_synthetic_and_malformed_messages(tmp_path: Path) -> None:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-0000000c6c6c")
    hx.assistant("msg_s", "<synthetic>", bf.usage())
    hx._msg("assistant", message="nope")
    hx._msg("assistant", message={"model": OPUS, "usage": {}})
    hx._msg("assistant", message={"id": "msg_nou", "model": OPUS})
    hx._msg("assistant", message={"id": "msg_bad", "model": OPUS,
                                  "usage": {"input_tokens": "many"}})
    hx._msg("mystery")
    text = hx.jsonl() + "[1]\n{broken\n"
    r = read_headless(tmp_path, text)
    assert note(r, "dq.synthetic_skipped").count == 1
    assert sorted(q.reason for q in r.quarantined) == sorted(
        ["missing:message", "missing:message.id", "missing:message.usage", "bad_usage",
         "not_object", "bad_json"])
    assert note(r, "dq.unknown_entry_type").count == 1


def test_sniff(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    bf.build(root)
    for p in (root / "headless").iterdir():
        assert HEADLESS.sniff(p, p.read_bytes()[:65536]) is True, p.name
    main = root / "projects" / "-home-dev-alpha" / f"{bf.SID_ALPHA}.jsonl"
    assert HEADLESS.sniff(main, main.read_bytes()[:65536]) is False
    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps({"type": "result", "session_id": "s", "modelUsage": {}},
                                 indent=2))
    assert HEADLESS.sniff(pretty, pretty.read_bytes()) is True
    assert HEADLESS.sniff(pretty, b"\xef\xbb\xbf" + pretty.read_bytes()) is True
    assert HEADLESS.sniff(pretty, b"hello") is False
    assert HEADLESS.sniff(pretty, b'{"session_id": "s", "type": "result"') is False


def test_read_errors(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        HEADLESS.read(tmp_path / "missing.jsonl", opts())
    p = tmp_path / "s.jsonl"
    p.write_text(bf.headless_stream().jsonl())
    with pytest.raises(UsageError):
        HEADLESS.read(p, opts(content_tier="fingerprint"))
    p.write_text("{broken\n")
    with pytest.raises(SourceError):
        HEADLESS.read(p, opts(lenient=False))


def test_gzip_and_bom_inputs(tmp_path: Path) -> None:
    import gzip

    p = tmp_path / "s.jsonl.gz"
    p.write_bytes(gzip.compress(bf.headless_stream().jsonl().encode()))
    assert len(HEADLESS.read(p, opts()).requests) == 5
    loaded = load_messages(b"\xef\xbb\xbf" + bf.headless_stream().jsonl().encode())
    assert len(loaded.messages) == len(bf.headless_stream().messages)
    assert load_messages(b"[\xff]").bad == [("index:0", "bad_json")]
    assert load_messages(b'[1, {"type": "x"}]').bad == [("index:0", "not_object")]


def test_same_file_twice_is_identical(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text(bf.headless_stream().jsonl())
    assert canonical(HEADLESS.read(p, opts())) == canonical(HEADLESS.read(p, opts()))


def test_subscription_billing_nan_literals_and_iter_messages(tmp_path: Path) -> None:
    from tokenbill.adapters.cc_headless import iter_messages

    r = read_headless(tmp_path, bf.headless_stream().jsonl(), billing_path="subscription")
    assert {i.pricing.billing_path for q in r.requests for i in q.attempts[0].inferences} == {
        "subscription"}
    assert note(r, "dq.subscription_allowance").count == 5
    p = tmp_path / "s.jsonl"
    p.write_text(bf.headless_stream().jsonl())
    assert len(list(iter_messages(p))) == len(bf.headless_stream().messages)
    nan = read_headless(tmp_path, '{"type": "result", "session_id": "s", "total_cost_usd": NaN}\n'
                        + bf.headless_stream().jsonl())
    assert [q.reason for q in nan.quarantined] == ["bad_json"]


def test_unpriced_step_models_and_ttl_residual_notes(tmp_path: Path) -> None:
    hx = bf.Hx("9e0d2c3b-0000-4000-8000-0000000c7c7c")
    hx.assistant("msg_u1", "sonnet", bf.usage(3, 0, 100, 0, 1, total_write=150),
                 stop="end_turn")
    r = read_headless(tmp_path, hx.jsonl())
    assert note(r, "dq.unpriced_model").count == 1
    assert note(r, "dq.ttl_split_residual").count == 1
