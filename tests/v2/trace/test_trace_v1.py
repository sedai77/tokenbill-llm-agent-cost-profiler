"""The trace@1 adapter (SPEC §5.5, D12, ruling R-E27): the TRACE acceptance items."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tests.v2.trace.helpers import NAME_KEY, PRINCIPAL_KEY, PRINCIPAL_KID
from tokenbill.adapters.trace_v1 import TraceV1Adapter
from tokenbill.core.builders import CANARY, assert_no_canary, plant_canary
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.labels import Evidence
from tokenbill.core.lanes import group_lanes
from tokenbill.core.records import ContentTier, Fidelity, LaneKind, to_json
from tokenbill.core.registry import get_adapter, sniff_adapter
from tokenbill.core.testing import FakePricer, assert_adapter_conforms, conformance_ingest_options
from tokenbill.demo_traces import SCENARIOS, scenario
from tokenbill.pricing import price_usd
from tokenbill.trace import write_trace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trace"
TIERS = (ContentTier.NONE, ContentTier.FINGERPRINT)


def _opts(**kw: object) -> object:
    return conformance_ingest_options(**kw)


def _demo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.jsonl"
    write_trace(path, scenario(name))
    return path


# ---------------------------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_demo_scenarios_price_to_v01_as_billed_within_one_nano(tmp_path: Path, name: str,
                                                                tier: ContentTier) -> None:
    calls = scenario(name)
    result = TraceV1Adapter().read(_demo(tmp_path, name), _opts(content_tier=tier))
    assert len(result.requests) == len(calls)
    pricer = FakePricer()
    for req, call in zip(result.requests, calls, strict=True):
        (inf,) = req.billable_inferences
        priced = pricer.price_inference(inf, ts_ms=req.ts_start_ms)
        assert priced.unpriced_reason is None
        assert priced.figure.evidence is Evidence.EXACT and priced.estimated is None
        v01_nano = Decimal(repr(price_usd(call.model, call.usage))) * 10**9
        assert abs(Decimal(priced.exact_nano) - v01_nano) <= 1


@pytest.mark.parametrize("tier", TIERS)
def test_breakpoint_count_mapping(tmp_path: Path, tier: ContentTier) -> None:
    for name in SCENARIOS:
        result = TraceV1Adapter().read(_demo(tmp_path, name), _opts(content_tier=tier))
        codes = {n.code: n.count for n in result.notes}
        for req in result.requests:
            if name == "no-cache":
                assert req.params.breakpoints == ()
                assert req.params.automatic_caching is False
            else:
                (bp,) = req.params.breakpoints
                assert bp.assumed and bp.ttl == "5m"
                n_blocks = (len(req.fingerprint.blocks) if req.fingerprint is not None
                            else bp.block_index + 1)
                assert bp.block_index == n_blocks - 1
                assert req.params.automatic_caching is None
        if name == "no-cache":
            assert "dq.breakpoint_assumed_end" not in codes
        else:
            assert codes["dq.breakpoint_assumed_end"] == len(result.requests)


def test_writes_are_exact_5m_without_1h_markers_and_a_range_with_them() -> None:
    result = TraceV1Adapter().read(FIXTURES / "trace1_runs.jsonl", _opts())
    agent = [r for r in result.requests if r.model == "claude-sonnet-5"]
    pricer = FakePricer()
    for req in agent[:3]:
        (inf,) = req.billable_inferences
        assert inf.usage.cache_write_5m == 400 and inf.usage.cache_write_unknown == 0
        assert inf.pricing.write_ttl_hint is None
    (inf,) = agent[3].billable_inferences
    assert inf.usage.cache_write_5m == 0 and inf.usage.cache_write_unknown == 400
    assert inf.pricing.write_ttl_hint == "1h"
    priced = pricer.price_inference(inf, ts_ms=agent[3].ts_start_ms)
    est = priced.estimated
    assert est is not None and est.evidence is Evidence.ESTIMATED
    # 400 writes on Sonnet 5 ($2/MTok): 5m 1.25× = $0.001, 1h 2× = $0.0016, point at the 1h hint
    assert (est.low_nano, est.high_nano, est.nano) == (1_000_000, 1_600_000, 1_600_000)
    codes = {n.code: n.count for n in result.notes}
    assert codes["dq.ttl_1h_markers_in_trace1"] == 1


def test_explicit_markers_become_breakpoints_at_block_indexes() -> None:
    result = TraceV1Adapter().read(FIXTURES / "trace1_runs.jsonl",
                                   _opts(content_tier=ContentTier.FINGERPRINT))
    agent = [r for r in result.requests if r.model == "claude-sonnet-5"]
    third, fourth = agent[2], agent[3]
    last = len(third.fingerprint.blocks) - 1
    assert [(b.block_index, b.ttl, b.assumed) for b in third.params.breakpoints] == \
        [(last, "5m", False)]
    last = len(fourth.fingerprint.blocks) - 1
    assert [(b.block_index, b.ttl, b.assumed) for b in fourth.params.breakpoints] == \
        [(last, "1h", False)]


def test_two_files_reusing_a_run_id_are_two_sessions(tmp_path: Path) -> None:
    calls = scenario("well-behaved")[:3]
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_trace(a, calls)
    write_trace(b, calls)
    ra = TraceV1Adapter().read(a, _opts())
    rb = TraceV1Adapter().read(b, _opts())
    assert {s.session_key for s in ra.sessions}.isdisjoint(s.session_key for s in rb.sessions)
    assert {r.request_id for r in ra.requests}.isdisjoint(r.request_id for r in rb.requests)
    assert ra.source.source_id != rb.source.source_id


def test_interleaved_two_model_run_gets_two_inferred_lanes() -> None:
    result = TraceV1Adapter().read(FIXTURES / "trace1_runs.jsonl", _opts())
    mixed = [s for s in result.sessions if len(s.lanes) == 2]
    assert len(mixed) == 1
    (session,) = mixed
    assert all(lane.lane_exact is False and lane.kind is LaneKind.API_RUN
               for lane in session.lanes)
    lanes = [ln for ln in group_lanes(result.requests, result.events, result.sessions)
             if ln.session_key == session.session_key]
    assert sorted({r.model for r in ln.requests}.pop() for ln in lanes) == \
        ["claude-haiku-4-5", "claude-opus-5-5"]
    assert all(len({r.model for r in ln.requests}) == 1 for ln in lanes)
    assert {n.code: n.count for n in result.notes}["dq.lanes_inferred"] == 1
    single = [s for s in result.sessions if len(s.lanes) == 1]
    assert single and single[0].lanes[0].lane_exact is True


def test_demo_runs_stay_one_lane_even_with_tool_churn(tmp_path: Path) -> None:
    for name in SCENARIOS:
        result = TraceV1Adapter().read(_demo(tmp_path, name),
                                       _opts(content_tier=ContentTier.FINGERPRINT))
        lanes = group_lanes(result.requests, result.events, result.sessions)
        assert len(lanes) == 1 and lanes[0].lane_exact
        assert [r.seq for r in lanes[0].requests] == list(range(len(result.requests)))


def test_malformed_lines_are_quarantined_or_raise_in_strict_mode() -> None:
    path = FIXTURES / "trace1_malformed.jsonl"
    result = TraceV1Adapter().read(path, _opts())
    assert [(q.locator, q.reason) for q in result.quarantined] == [
        ("line:2", "bad_json"), ("line:3", "not_object"), ("line:4", "bad_type:schema"),
        ("line:5", "missing:usage.output_tokens"), ("line:6", "bad_type:cache_breakpoints"),
        ("line:8", "bad_type:index"), ("line:9", "bad_type:ts")]
    assert len(result.requests) == 2
    assert {n.code: n.count for n in result.notes}["dq.quarantined"] == 7
    assert_no_canary(repr(result.quarantined))
    with pytest.raises(SourceError, match=r"trace1_malformed.jsonl: line 2: bad_json"):
        TraceV1Adapter().read(path, _opts(lenient=False))


def test_other_quarantine_reasons(tmp_path: Path) -> None:
    good = json.loads((FIXTURES / "trace1_runs.jsonl").read_text().splitlines()[0])
    lines = [json.dumps({k: v for k, v in good.items() if k != "schema"}),
             json.dumps({**good, "model": "x" * 200}),
             json.dumps({**good, "run_id": "r2", "ts": 1e300}),
             "﻿" + json.dumps({**good, "run_id": "r3"}),
             json.dumps({**good, "run_id": "r4", "stop_reason": "not a token " + CANARY})]
    path = tmp_path / "t.jsonl"
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8") + b"\xff\xfe\n")
    result = TraceV1Adapter().read(path, _opts())
    assert [q.reason for q in result.quarantined] == [
        "missing:schema", "bad_type:model", "bad_type:ts", "bad_json", "bad_json"]
    (req,) = result.requests
    assert req.final_attempt.stop_reason is None
    assert_no_canary(repr(result), json.dumps(to_json(result)))


def test_bom_on_the_first_line_is_accepted(tmp_path: Path) -> None:
    line = (FIXTURES / "trace1_runs.jsonl").read_text().splitlines()[0]
    path = tmp_path / "bom.jsonl"
    path.write_text("﻿" + line + "\n", encoding="utf-8")
    assert len(TraceV1Adapter().read(path, _opts()).requests) == 1


@pytest.mark.parametrize(("tier", "caps"), [
    (ContentTier.NONE, {"usage_sequence", "timing", "params"}),
    (ContentTier.FINGERPRINT, {"usage_sequence", "timing", "params", "blocks"}),
])
def test_adapter_conforms(tier: ContentTier, caps: set[str]) -> None:
    for name in ("trace1_runs.jsonl", "trace1_malformed.jsonl"):
        result = assert_adapter_conforms(TraceV1Adapter(), FIXTURES / name,
                                         expect_capabilities=caps,
                                         opts=_opts(content_tier=tier))
        assert result.source.adapter == "trace@1"


def test_empty_file_has_no_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    result = assert_adapter_conforms(TraceV1Adapter(), path, expect_capabilities=set())
    assert result.requests == [] and result.sessions == []


# ---------------------------------------------------------------------------------------------
# records, identity, privacy
# ---------------------------------------------------------------------------------------------


def test_request_shape(tmp_path: Path) -> None:
    calls = scenario("well-behaved")
    result = TraceV1Adapter().read(_demo(tmp_path, "well-behaved"), _opts())
    req, call = result.requests[1], calls[1]
    att = req.final_attempt
    (inf,) = att.inferences
    assert req.seq == call.index == 1
    assert att.ts_start_ms == round(call.ts * 1000)
    assert inf.usage.uncached_input == call.usage.input_tokens
    assert inf.usage.cache_read == call.usage.cache_read_input_tokens
    assert inf.usage.cache_write_5m == call.usage.cache_creation_input_tokens
    assert inf.usage.output == call.usage.output_tokens
    assert (inf.pricing.channel, inf.pricing.billing_path, inf.pricing.model) == \
        ("anthropic_api", "api_key", "claude-sonnet-5")
    assert req.attribution.billing_path == "api_key"
    assert att.convention_id is None
    assert json.loads(att.raw_usage_json) == {
        "input_tokens": call.usage.input_tokens,
        "cache_read_input_tokens": call.usage.cache_read_input_tokens,
        "cache_creation_input_tokens": call.usage.cache_creation_input_tokens,
        "output_tokens": call.usage.output_tokens}
    assert req.source.adapter == "trace@1" and req.source.fidelity is Fidelity.NO_TTL_SPLIT
    assert req.source.locator == "line:2"
    (session,) = result.sessions
    assert session.source_kind == "trace@1"
    assert session.started_ms == result.requests[0].ts_start_ms
    assert session.ended_ms == result.requests[-1].ts_start_ms
    assert result.stats["runs"] == 1 and result.stats["requests"] == len(calls)


def test_identity_modes() -> None:
    path = FIXTURES / "trace1_runs.jsonl"
    install = TraceV1Adapter().read(path, _opts(principal_ref="dev-7"))
    assert {r.attribution.principal for r in install.requests} == {
        pseudonym(PRINCIPAL_KEY, "p", "dev-7")}
    assert install.source.principal_key_id == PRINCIPAL_KID
    central = TraceV1Adapter().read(path, _opts(principal_ref="dev-7", identity_mode="central",
                                                principal_key=None, principal_key_id=None))
    assert {r.attribution.principal for r in central.requests} == {"r_dev-7"}
    assert central.source.principal_key_id is None
    two = TraceV1Adapter().read(path, _opts(principal_ref="dev-7", identity_mode="two-stage"))
    assert {r.attribution.principal for r in two.requests} == {
        pseudonym(PRINCIPAL_KEY, "c", "dev-7")}
    none = TraceV1Adapter().read(path, _opts())
    assert {r.attribution.principal for r in none.requests} == {None}
    assert none.source.principal_key_id is None
    with pytest.raises(UsageError):
        TraceV1Adapter().read(path, _opts(principal_ref="dev@example.com"))
    with pytest.raises(UsageError):
        TraceV1Adapter().read(path, _opts(principal_ref="dev-7", principal_key=None))
    preset = pseudonym(PRINCIPAL_KEY, "p", "x")
    from tokenbill.core.records import Attribution
    given = TraceV1Adapter().read(path, _opts(attribution=Attribution(principal=preset,
                                                                      team="core")))
    assert {(r.attribution.principal, r.attribution.team) for r in given.requests} == {
        (preset, "core")}
    assert given.source.principal_key_id == PRINCIPAL_KID


def test_options_attribution_billing_path_is_mirrored(tmp_path: Path) -> None:
    from tokenbill.core.records import Attribution
    result = TraceV1Adapter().read(_demo(tmp_path, "no-cache"),
                                   _opts(attribution=Attribution(billing_path="subscription")))
    req = result.requests[0]
    assert req.attribution.billing_path == "subscription"
    assert req.serving_inference.pricing.billing_path == "subscription"


def test_time_window(tmp_path: Path) -> None:
    calls = scenario("well-behaved")
    path = _demo(tmp_path, "well-behaved")
    since = round(calls[3].ts * 1000)
    until = round(calls[6].ts * 1000)
    result = TraceV1Adapter().read(path, _opts(since_ms=since, until_ms=until))
    assert [r.seq for r in result.requests] == [3, 4, 5]
    assert result.stats["skipped_window"] == len(calls) - 3


def test_canary_absent_in_every_tier(tmp_path: Path) -> None:
    calls = scenario("well-behaved")[:4]
    path = tmp_path / "c.jsonl"
    path.write_text("".join(json.dumps(plant_canary(json.loads(json.dumps(
        {"schema": "tokenbill/trace@1", "run_id": c.run_id, "index": c.index, "ts": c.ts,
         "model": c.model, "system": c.system, "tools": list(c.tools),
         "messages": list(c.messages), "cache_breakpoints": c.cache_breakpoints,
         "usage": vars(c.usage), "stop_reason": c.stop_reason})))) + "\n" for c in calls))
    assert CANARY in path.read_text()
    for tier in (*TIERS, ContentTier.FULL):
        result = TraceV1Adapter().read(path, _opts(content_tier=tier))
        assert len(result.requests) == 4
        assert_no_canary(repr(result), json.dumps(to_json(result)))


def test_gzip_and_registry_sniffing(tmp_path: Path) -> None:
    plain = _demo(tmp_path, "timestamp")
    gz = tmp_path / "timestamp.jsonl.gz"
    gz.write_bytes(gzip.compress(plain.read_bytes(), mtime=0))
    a = TraceV1Adapter().read(plain, _opts())
    b = TraceV1Adapter().read(gz, _opts())
    assert [(r.seq, r.serving_inference.usage, r.params) for r in a.requests] == \
        [(r.seq, r.serving_inference.usage, r.params) for r in b.requests]
    assert sniff_adapter(plain).name == "trace@1"
    assert sniff_adapter(gz).name == "trace@1"
    assert sniff_adapter(FIXTURES / "trace1_runs.jsonl").name == "trace@1"
    assert isinstance(get_adapter("trace@1"), TraceV1Adapter)


def test_sniff_heads() -> None:
    sniff = TraceV1Adapter().sniff
    recorder_line = b'{"schema": "tokenbill/trace@1", "run_id": "r", "index": 0}\n'
    canonical_prefix = b'{"cache_breakpoints":1,"index":0,"messages":[{"role":"user"'
    assert sniff(Path("x"), recorder_line)
    assert sniff(Path("x"), b"\xef\xbb\xbf" + recorder_line)
    assert sniff(Path("x"), canonical_prefix)
    assert not sniff(Path("x"), b'{"rec":"header","schema":"tokenbill/trace@2"}\n')
    assert not sniff(Path("x"), b"not json\n")
    assert not sniff(Path("x"), b'{"a": 1}\n')


def test_fingerprint_tier_needs_a_name_key(tmp_path: Path) -> None:
    path = _demo(tmp_path, "no-cache")
    with pytest.raises(UsageError):
        TraceV1Adapter().read(path, _opts(content_tier=ContentTier.FINGERPRINT, name_key=b""))
    with pytest.raises(UsageError):
        TraceV1Adapter().read(path, _opts(content_tier="bogus"))
    with pytest.raises(UsageError):
        TraceV1Adapter().read(path, object())  # type: ignore[arg-type]


def test_fingerprint_key_and_source_key_ids(tmp_path: Path) -> None:
    result = TraceV1Adapter().read(_demo(tmp_path, "tool-churn"),
                                   _opts(content_tier=ContentTier.FINGERPRINT))
    assert {r.fingerprint.key_id for r in result.requests} == {key_id(NAME_KEY)}
    assert result.source.name_key_id == key_id(NAME_KEY)
    assert result.source.name_hmac.startswith("h_")
    no_key = TraceV1Adapter().read(_demo(tmp_path, "no-cache"),
                                   _opts(name_key=b"", name_key_id=""))
    assert no_key.source.name_key_id is None and no_key.source.name_hmac == ""


def test_unreadable_file_raises_source_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        TraceV1Adapter().read(tmp_path / "missing.jsonl", _opts())
