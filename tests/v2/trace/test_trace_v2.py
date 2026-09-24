"""The trace@2 codec and adapter (SPEC §4, D3, D39, R-E3): the TRACE acceptance items."""

from __future__ import annotations

import dataclasses
import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tests.v2.trace.helpers import (
    FP_KID,
    NAME_KID,
    PRINCIPAL,
    PRINCIPAL_KID,
    T0,
    conversation,
    fingerprint_of,
    header,
    sample,
)
from tokenbill.adapters import trace_v2 as T
from tokenbill.core.builders import CANARY, assert_no_canary, make_request
from tokenbill.core.errors import ContractViolation, SourceError, UsageError
from tokenbill.core.records import (
    Attempt,
    Attribution,
    ContentTier,
    CostLine,
    Inference,
    InferenceKind,
    OutcomeAggregate,
    Request,
    Session,
    UsageAggregate,
    UsageBuckets,
    from_json,
    record_fields,
    to_json,
)
from tokenbill.core.registry import get_adapter, sniff_adapter
from tokenbill.core.testing import assert_adapter_conforms, conformance_ingest_options
from tokenbill.core.types import DataQualityNote, QuarantineItem

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trace"
PROFILES = ("usage", "fingerprint", "full")
RECORD_LISTS = ("sessions", "requests", "events", "aggregates", "cost_lines", "outcomes", "notes")


def _opts(**kw: Any) -> Any:
    return conformance_ingest_options(**kw)


def _read(path: Path, **kw: Any) -> Any:
    return T.TraceV2Adapter().read(path, _opts(**kw))


def _write_result(path: Path, result: Any, hdr: dict[str, Any]) -> int:
    return T.write_trace_v2(path, header=hdr, sessions=result.sessions, requests=result.requests,
                            events=result.events, aggregates=result.aggregates,
                            cost_lines=result.cost_lines, outcomes=result.outcomes,
                            notes=result.notes)


# ---------------------------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_write_read_write_is_byte_identical(tmp_path: Path, profile: str) -> None:
    s = sample(profile)
    first = tmp_path / "a.jsonl"
    n = s.write(first)
    assert n == len(first.read_text().splitlines())
    result = _read(first)
    assert result.quarantined == []
    for name in RECORD_LISTS:
        if name == "sessions":
            continue
        assert [to_json(x) for x in getattr(result, name)] == \
            [to_json(x) for x in getattr(s, name)], name
    # sessions come back with their lane shells attached
    assert [to_json(x) for x in result.sessions] == [to_json(x) for x in s.sessions]
    content = None
    if profile == "full":
        content = {i.h: i.text for i in T.iter_trace_v2(first) if isinstance(i, T.ContentItem)}
        assert content == s.content
    second = tmp_path / "b.jsonl"
    T.write_trace_v2(second, header=s.header, sessions=result.sessions, requests=result.requests,
                     events=result.events, aggregates=result.aggregates,
                     cost_lines=result.cost_lines, outcomes=result.outcomes,
                     notes=result.notes, content=content)
    assert first.read_bytes() == second.read_bytes()
    assert (first.stat().st_mode & 0o777) == 0o600


def test_records_decode_like_the_reference_from_json(tmp_path: Path) -> None:
    """R-E3: TRACE's own decoder on the hot path, ``core.records.from_json`` the reference."""
    s = sample("fingerprint")
    path = tmp_path / "t.jsonl"
    s.write(path)
    result = _read(path)
    for cls, items in ((Request, result.requests), (UsageAggregate, result.aggregates),
                       (CostLine, result.cost_lines), (OutcomeAggregate, result.outcomes),
                       (Session, result.sessions)):
        for item in items:
            assert from_json(cls, to_json(item)) == item


@pytest.mark.parametrize("profile", PROFILES)
def test_gzip_round_trip(tmp_path: Path, profile: str) -> None:
    s = sample(profile)
    plain, gz = tmp_path / "t.jsonl", tmp_path / "t.jsonl.gz"
    s.write(plain)
    s.write(gz)
    assert gzip.decompress(gz.read_bytes()) == plain.read_bytes()
    assert gz.read_bytes() == (s.write(tmp_path / "u.jsonl.gz"), (tmp_path / "u.jsonl.gz")
                               .read_bytes())[1]                    # deterministic gzip
    a, b = _read(plain), _read(gz)
    for name in RECORD_LISTS:
        assert [to_json(x) for x in getattr(a, name)] == [to_json(x) for x in getattr(b, name)]
    assert sniff_adapter(gz).name == "trace@2"


def test_checked_in_gzip_fixture_matches_plain() -> None:
    assert gzip.decompress((FIXTURES / "trace2_usage.jsonl.gz").read_bytes()) == \
        (FIXTURES / "trace2_usage.jsonl").read_bytes()


def test_canary_absent_from_usage_and_fingerprint_files(tmp_path: Path) -> None:
    for profile in ("usage", "fingerprint"):
        path = tmp_path / f"{profile}.jsonl"
        sample(profile).write(path)
        assert_no_canary(path.read_bytes())
        result = _read(path)
        assert_no_canary(repr(result), json.dumps(to_json(result)))
    full = tmp_path / "full.jsonl"
    sample("full").write(full)
    assert CANARY.encode() in full.read_bytes()        # content is local-only, and it is there
    assert_no_canary(repr(_read(full)))                 # but never enters an IngestResult


def test_delta_encoding_defines_each_hash_once(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    sample("fingerprint").write(path)
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    defined = [b["h"] for x in lines if x["rec"] == "blocks" for b in x["blocks"]]
    assert len(defined) == len(set(defined))
    assert all("lookback_pos" not in b for x in lines if x["rec"] == "blocks"
               for b in x["blocks"])
    fps = [x["fp"] for x in lines if x["rec"] == "request" and x["fp"] is not None]
    first, second = fps
    assert first["parent"] is None and first["keep"] == 0
    assert second["parent"] == "rq_sample_1" and second["keep"] > 0
    assert len(second["append"]) < len(first["append"])
    assert second["markers"] and all(len(m) == 2 for m in second["markers"])


def test_redefined_hash_applies_to_later_requests(tmp_path: Path) -> None:
    fp1, _, _ = fingerprint_of(conversation(1))
    changed = tuple(dataclasses.replace(b, est_tokens=(b.est_tokens or 0) + 1)
                    if i == len(fp1.blocks) - 1 else b for i, b in enumerate(fp1.blocks))
    fp2 = dataclasses.replace(fp1, blocks=changed)
    reqs = [make_request("ln", i, T0 + i, {"output": 1}, request_id=f"rq{i}", fingerprint=fp)
            for i, fp in enumerate((fp1, fp2))]
    path = tmp_path / "t.jsonl"
    T.write_trace_v2(path, header=header("fingerprint"), requests=reqs)
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["rec"] for x in lines] == ["header", "blocks", "request", "blocks", "request"]
    assert lines[4]["fp"]["keep"] == len(fp1.blocks) - 1
    back = _read(path)
    assert [r.fingerprint for r in back.requests] == [fp1, fp2]


def test_usage_profile_drops_fingerprints(tmp_path: Path) -> None:
    fp_sample = sample("fingerprint")
    path = tmp_path / "t.jsonl"
    T.write_trace_v2(path, header=header("usage"), requests=fp_sample.requests)
    text = path.read_text()
    assert '"rec":"blocks"' not in text
    assert all(r.fingerprint is None for r in _read(path).requests)
    with pytest.raises(UsageError):
        T.write_trace_v2(path, header=header("fingerprint"), content={"h": "text"})


# ---------------------------------------------------------------------------------------------
# rejections (strict: SourceError; lenient: quarantine)
# ---------------------------------------------------------------------------------------------


def _lines(profile: str = "usage") -> list[dict[str, Any]]:
    return [json.loads(x) for x in (FIXTURES / f"trace2_{profile}.jsonl").read_text()
            .splitlines()]


def _dump(path: Path, lines: list[Any]) -> Path:
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines),
                    encoding="utf-8")
    return path


def _first(lines: list[dict[str, Any]], rec: str) -> dict[str, Any]:
    return next(x for x in lines if x["rec"] == rec)


def _mutated(mutate: Any, profile: str = "usage") -> list[dict[str, Any]]:
    lines = _lines(profile)
    mutate(lines)
    return lines


def _set_request(key: str, value: Any) -> Any:
    def go(lines: list[dict[str, Any]]) -> None:
        _first(lines, "request")["attribution"][key] = value
    return go


REJECTIONS: dict[str, tuple[Any, str, str]] = {
    "unknown key": (lambda ls: _first(ls, "request").update(ext={"x": 1}), "usage",
                    "unknown_key:ext"),
    "unknown nested key": (lambda ls: _first(ls, "request")["attempts"][0].update(note="x"),
                           "usage", "unknown_key:note"),
    "string over 256 chars": (lambda ls: _first(ls, "cost_line").update(description="d" * 257),
                              "usage", "bad_type:description"),
    "principal with @": (_set_request("principal", "r_dev@example.com"), "usage",
                         "bad_type:principal"),
    "c_ principal without two-stage": (_set_request("principal", "c_" + "0" * 20), "usage",
                                       "bad_type:principal"),
    "r_ principal outside central mode": (_set_request("principal", "r_dev-7"), "usage",
                                          "bad_type:principal"),
    "extra key outside EXTRA_KEYS": (_set_request("extra", [["favorite_color", "red"]]),
                                     "usage", "bad_type:extra"),
    "float number": (lambda ls: _first(ls, "request").update(seq=1.5), "usage",
                     "bad_type:seq"),
    "float inside usage": (lambda ls: _first(ls, "request")["attempts"][1]["inferences"][0]
                           ["usage"].update(output=2.0), "usage", "bad_type:output"),
    "integer beyond 2**53": (lambda ls: _first(ls, "event").update(ts_ms=2**60), "usage",
                             "bad_type:ts_ms"),
    "negative outside money": (lambda ls: _first(ls, "request").update(seq=-1), "usage",
                               "bad_type:seq"),
    "fp in a usage profile": (lambda ls: _first(ls, "request").update(
        fp={"parent": None, "keep": 0, "append": [], "tier_end": [0, 0, 0]}), "usage",
        "bad_type:fp"),
    "repo not h_": (_set_request("repo", "acme/payments"), "usage", "bad_type:repo"),
    "bad raw usage string": (lambda ls: _first(ls, "request")["attempts"][1].update(
        raw_usage={"input_tokens": 1, "note": "free text"}), "usage", "bad_usage"),
    "bad raw usage float": (lambda ls: _first(ls, "request")["attempts"][1].update(
        raw_usage={"input_tokens": 1.5}), "usage", "bad_type:input_tokens"),
    "missing required key": (lambda ls: _first(ls, "request").pop("attempts"), "usage",
                             "missing:attempts"),
    "wrong type": (lambda ls: _first(ls, "request").update(attempts={}), "usage",
                   "bad_type:attempts"),
    "bad enum": (lambda ls: _first(ls, "lane").update(kind="spaceship"), "usage",
                 "bad_type:kind"),
    "bad event attr": (lambda ls: _first(ls, "event")["attrs"].update(mood="grumpy"), "usage",
                       "bad_type:attrs"),
    "bad dq severity": (lambda ls: _first(ls, "dq").update(severity="panic"), "usage",
                        "bad_type:severity"),
    "missing schema": (lambda ls: _first(ls, "request").pop("schema"), "usage",
                       "missing:schema"),
    "wrong schema": (lambda ls: _first(ls, "request").update(schema="tokenbill/trace@3"),
                     "usage", "bad_type:schema"),
    "unknown rec": (lambda ls: ls.append({"rec": "mystery", "schema": T.SCHEMA}), "usage",
                    "bad_type:rec"),
    "missing rec": (lambda ls: ls.append({"schema": T.SCHEMA}), "usage", "missing:rec"),
    "second header": (lambda ls: ls.append(dict(ls[0])), "usage", "bad_type:rec"),
    "undefined appended hash": (lambda ls: _first(ls, "request")["fp"]["append"].append("f" * 32),
                                "fingerprint", "missing:append"),
    "unknown parent": (lambda ls: [x for x in ls if x["rec"] == "request"][1]["fp"].update(
        parent="rq_nowhere"), "fingerprint", "missing:parent"),
    "keep beyond parent": (lambda ls: [x for x in ls if x["rec"] == "request"][1]["fp"].update(
        keep=10_000), "fingerprint", "bad_type:keep"),
    "blocks with another key id": (lambda ls: _first(ls, "blocks").update(key_id="k_ffffffffffff"),
                                   "fingerprint", "bad_type:key_id"),
    "markers disagree with params": (lambda ls: _first(ls, "request")["fp"].update(
        markers=[[0, "5m"]]), "fingerprint", "bad_type:markers"),
    "content in a fingerprint profile": (lambda ls: ls.append(
        {"rec": "content", "schema": T.SCHEMA, "h": "a" * 32, "text": "t"}), "fingerprint",
        "bad_type:content"),
}


@pytest.mark.parametrize("case", sorted(REJECTIONS))
def test_rejections(tmp_path: Path, case: str) -> None:
    mutate, profile, reason = REJECTIONS[case]
    path = _dump(tmp_path / "t.jsonl", _mutated(mutate, profile))
    result = _read(path)
    assert reason in [q.reason for q in result.quarantined], result.quarantined
    assert any(n.code == "dq.quarantined" for n in result.notes)
    with pytest.raises(SourceError, match=reason.replace("*", r"\*")):
        _read(path, lenient=False)


def test_blocks_in_a_usage_profile_are_rejected(tmp_path: Path) -> None:
    lines = _lines("usage")
    lines.insert(1, {"rec": "blocks", "schema": T.SCHEMA, "key_id": FP_KID, "blocks": []})
    result = _read(_dump(tmp_path / "t.jsonl", lines))
    assert [q.reason for q in result.quarantined] == ["bad_type:blocks"]


def test_missing_header_quarantines_every_line(tmp_path: Path) -> None:
    lines = _lines("usage")[1:]
    path = _dump(tmp_path / "t.jsonl", lines)
    result = _read(path)
    assert len(result.quarantined) == len(lines)
    assert {q.reason for q in result.quarantined} == {"missing:header"}
    assert result.requests == [] and result.capabilities == frozenset()
    with pytest.raises(SourceError, match="missing:header"):
        _read(path, lenient=False)
    bad_header = _lines("usage")
    bad_header[0]["profile"] = "everything"
    result = _read(_dump(tmp_path / "u.jsonl", bad_header))
    assert result.quarantined[0].reason == "bad_type:profile"
    assert {q.reason for q in result.quarantined[1:]} == {"missing:header"}


def test_lines_that_are_not_json_objects(tmp_path: Path) -> None:
    lines: list[Any] = _lines("usage")
    lines[1:1] = ["{broken", "[1]", '{"rec":"dq","schema":"tokenbill/trace@2","count":NaN}',
                  '{"rec":"x","v":"\\ud800"}']
    path = _dump(tmp_path / "t.jsonl", lines)
    with open(path, "ab") as f:
        f.write(b"\xff\xfe\n")
    result = _read(path)
    assert [q.reason for q in result.quarantined] == ["bad_json", "not_object", "bad_json",
                                                      "bad_json", "bad_json"]


def test_identity_modes_in_the_header(tmp_path: Path) -> None:
    s = sample("usage")
    reqs = [dataclasses.replace(r, attribution=dataclasses.replace(r.attribution,
                                                                   principal="r_dev-7"))
            for r in s.requests]
    path = tmp_path / "central.jsonl"
    T.write_trace_v2(path, header=header("usage", identity_mode="central",
                                         principal_key_id=None),
                     requests=reqs)
    back = _read(path)
    assert {r.attribution.principal for r in back.requests} == {"r_dev-7"}
    assert back.source.principal_key_id is None
    two = tmp_path / "two.jsonl"
    c_reqs = [dataclasses.replace(r, attribution=dataclasses.replace(r.attribution,
                                                                     principal="c_" + "a" * 20))
              for r in s.requests]
    T.write_trace_v2(two, header=header("usage", identity_mode="two-stage"), requests=c_reqs)
    assert {r.attribution.principal for r in _read(two).requests} == {"c_" + "a" * 20}
    with pytest.raises(ContractViolation, match="principal"):
        T.write_trace_v2(tmp_path / "bad.jsonl", header=header("usage"), requests=reqs)
    with pytest.raises(ContractViolation, match="principal"):
        T.write_trace_v2(tmp_path / "bad2.jsonl",
                         header=header("usage", principal_key_id=None), requests=s.requests)
    with pytest.raises(ContractViolation, match="name_key_id"):
        T.write_trace_v2(tmp_path / "bad3.jsonl", header=header("usage", name_key_id=None),
                         requests=s.requests)


def test_duplicates_and_orphan_lanes(tmp_path: Path) -> None:
    lines = _lines("usage")
    lines.append(_first(lines, "request"))
    lines.append(_first(lines, "session"))
    lines.append(_first(lines, "lane"))
    orphan = dict(_first(lines, "lane"), lane_key="ln_orphan", session_key="ses_nowhere")
    lines.append(orphan)
    path = _dump(tmp_path / "t.jsonl", lines)
    result = _read(path)
    reasons = [q.reason for q in result.quarantined]
    assert reasons == ["bad_type:request_id", "bad_type:session_key", "bad_type:lane_key",
                       "missing:session"]
    assert len(result.requests) == 3 and len(result.sessions) == 1
    for bad in (lines[:-1], lines[:-2], lines[:-3]):
        with pytest.raises(SourceError):
            _read(_dump(tmp_path / "s.jsonl", bad), lenient=False)
    only_orphan = _lines("usage") + [orphan]
    with pytest.raises(SourceError, match="missing:session"):
        _read(_dump(tmp_path / "o.jsonl", only_orphan), lenient=False)


def test_blocks_record_lookback_is_recomputed(tmp_path: Path) -> None:
    lines = _lines("fingerprint")
    for x in lines:
        if x["rec"] == "blocks":
            for b in x["blocks"]:
                b["lookback_pos"] = 99           # informational: readers recompute
    result = _read(_dump(tmp_path / "t.jsonl", lines))
    expected = _read(FIXTURES / "trace2_fingerprint.jsonl")
    assert [r.fingerprint for r in result.requests] == [r.fingerprint for r in expected.requests]


def test_markers_fill_empty_breakpoints(tmp_path: Path) -> None:
    lines = _lines("fingerprint")
    req = _first(lines, "request")
    req["params"]["breakpoints"] = []
    result = _read(_dump(tmp_path / "t.jsonl", lines))
    (bp,) = result.requests[0].params.breakpoints
    assert [bp.block_index, bp.ttl] == req["fp"]["markers"][0] and not bp.assumed


# ---------------------------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------------------------


EXPECTED_CAPS = {"aggregates", "appended", "attempts", "attribution.team", "cost",
                 "diagnostics", "events", "human_prompts", "iterations", "outcomes", "params",
                 "quota_state", "timing", "ttft", "ttl_split", "usage_sequence", "workload"}


@pytest.mark.parametrize("profile", PROFILES)
def test_adapter_conforms_and_reports_present_capabilities(profile: str) -> None:
    caps = EXPECTED_CAPS | ({"blocks"} if profile != "usage" else set())
    result = assert_adapter_conforms(T.TraceV2Adapter(), FIXTURES / f"trace2_{profile}.jsonl",
                                     expect_capabilities=caps)
    assert result.source.adapter == "trace@2"
    assert result.source.name_key_id == NAME_KID
    assert result.source.principal_key_id == PRINCIPAL_KID
    assert result.stats["requests"] == 3 and result.stats["quarantined"] == 0


def test_conformance_of_the_malformed_fixture() -> None:
    result = assert_adapter_conforms(T.TraceV2Adapter(), FIXTURES / "trace2_malformed.jsonl",
                                     expect_capabilities=EXPECTED_CAPS)
    assert [q.reason for q in result.quarantined] == [
        "unknown_key:color", "bad_type:count", "bad_type:rec", "bad_type:blocks",
        "bad_type:detail"]


def test_source_key_ids_follow_the_header(tmp_path: Path) -> None:
    other_key = bytes(range(200, 232))
    result = _read(FIXTURES / "trace2_usage.jsonl", name_key=other_key, name_key_id="k_x")
    assert result.source.name_key_id == NAME_KID and result.source.name_hmac == ""
    result = _read(FIXTURES / "trace2_usage.jsonl", name_key=b"", name_key_id="")
    assert result.source.name_key_id == NAME_KID
    path = tmp_path / "nokeys.jsonl"
    T.write_trace_v2(path, header=header("usage", name_key_id=None, principal_key_id=None),
                     requests=[make_request("ln", 0, T0, {"output": 1}, request_id="rq")])
    bare = _read(path)
    assert bare.source.principal_key_id is None
    assert bare.source.name_key_id == NAME_KID and bare.source.name_hmac.startswith("h_")


def test_iter_trace_v2_yields_every_record_in_file_order(tmp_path: Path) -> None:
    items = list(T.iter_trace_v2(FIXTURES / "trace2_full.jsonl"))
    kinds = [type(i).__name__ for i in items]
    assert kinds[0] == "TraceHeader"
    assert {"Session", "Lane", "Request", "LaneEvent", "UsageAggregate", "CostLine",
            "OutcomeAggregate", "DataQualityNote", "ContentItem"} <= set(kinds)
    hdr = items[0]
    assert (hdr.profile, hdr.fp_key_id, hdr.identity_mode) == ("full", FP_KID, "install")
    assert dict(hdr.producer)["adapter"] == "test"
    assert hdr.attribution == Attribution(team="payments")
    bad = list(T.iter_trace_v2(FIXTURES / "trace2_malformed.jsonl"))
    assert sum(isinstance(i, QuarantineItem) for i in bad) == 5
    with pytest.raises(SourceError):
        list(T.iter_trace_v2(FIXTURES / "trace2_malformed.jsonl", lenient=False))


def test_time_window_filters_requests_and_events() -> None:
    result = _read(FIXTURES / "trace2_usage.jsonl", since_ms=T0 + 1, until_ms=T0 + 65_000)
    assert [r.request_id for r in result.requests] == ["rq_sample_2"]
    assert all(T0 + 1 <= e.ts_ms < T0 + 65_000 for e in result.events)
    assert result.stats["skipped_window"] == 2 + 2


def test_renormalize_rebuilds_inferences_from_raw_usage(tmp_path: Path) -> None:
    raw = json.dumps({"input_tokens": 10, "cache_read_input_tokens": 500,
                      "cache_creation_input_tokens": 50,
                      "cache_creation": {"ephemeral_5m_input_tokens": 20,
                                         "ephemeral_1h_input_tokens": 30},
                      "output_tokens": 5, "service_tier": "standard"}, sort_keys=True)
    stale = UsageBuckets(uncached_input=10, cache_read=500, cache_write_5m=50, output=5)
    iter_raw = json.dumps({"input_tokens": 10, "output_tokens": 5, "iterations": [
        {"type": "compaction", "input_tokens": 900, "output_tokens": 40},
        {"type": "message", "input_tokens": 10, "output_tokens": 5}]}, sort_keys=True)
    reqs = []
    for i, (text, usage) in enumerate(((raw, stale), (iter_raw, UsageBuckets(uncached_input=10,
                                                                              output=5)))):
        base = make_request("ln", i, T0 + i, usage, request_id=f"rq{i}")
        att = dataclasses.replace(base.attempts[0], raw_usage_json=text,
                                  convention_id="anthropic.messages")
        reqs.append(dataclasses.replace(base, attempts=(att,)))
    untouched = make_request("ln", 5, T0 + 5, {"output": 3}, request_id="rq5")
    unknown = dataclasses.replace(untouched, request_id="rq6", attempts=(dataclasses.replace(
        untouched.attempts[0], raw_usage_json='{"output_tokens":3}', convention_id="nope.x"),))
    path = tmp_path / "t.jsonl"
    T.write_trace_v2(path, header=header("usage", name_key_id=None, principal_key_id=None),
                     requests=[*reqs, untouched, unknown])
    plain = _read(path)
    assert plain.requests[0].serving_inference.usage == stale
    fixed = _read(path, renormalize=True)
    first = fixed.requests[0].serving_inference
    assert (first.usage.cache_write_5m, first.usage.cache_write_1h,
            first.usage.cache_write_unknown) == (20, 30, 0)
    assert first.inference_id == plain.requests[0].serving_inference.inference_id
    kinds = [i.kind for i in fixed.requests[1].final_attempt.inferences]
    assert kinds == [InferenceKind.COMPACTION, InferenceKind.MESSAGE]
    assert fixed.requests[2] == plain.requests[2] and fixed.requests[3] == plain.requests[3]
    codes = {n.code: n.count for n in fixed.notes}
    assert codes["dq.renormalized"] == 2
    assert fixed.stats["renormalize_failed"] == 1
    assert "dq.renormalized" not in {n.code for n in plain.notes}


def test_renormalize_other_conventions_and_skips(tmp_path: Path) -> None:
    pytest.importorskip("tokenbill.adapters.conventions_ext")
    raw = json.dumps({"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40},
                      "output_tokens": 9})
    base = make_request("ln", 0, T0, {"uncached_input": 100, "output": 9}, "gpt-5.6-sol",
                        request_id="rq_oa")
    att = dataclasses.replace(base.attempts[0], raw_usage_json=raw,
                              convention_id="openai.responses")
    two = dataclasses.replace(att, inferences=att.inferences * 2)
    residual = dataclasses.replace(att, inferences=(dataclasses.replace(
        att.inferences[0], kind=InferenceKind.OUTPUT_RESIDUAL),))
    reqs = [dataclasses.replace(base, attempts=(att,)),
            dataclasses.replace(base, request_id="rq_two", attempts=(two,)),
            dataclasses.replace(base, request_id="rq_res", attempts=(residual,))]
    path = tmp_path / "t.jsonl"
    T.write_trace_v2(path, header=header("usage", name_key_id=None, principal_key_id=None),
                     requests=reqs)
    fixed = _read(path, renormalize=True)
    usage = fixed.requests[0].serving_inference.usage
    assert (usage.uncached_input, usage.cache_read, usage.output) == (60, 40, 9)
    assert fixed.requests[1:] == _read(path).requests[1:]


def test_writer_sanitizes_raw_usage(tmp_path: Path) -> None:
    base = make_request("ln", 0, T0, {"output": 1}, request_id="rq")
    raw = json.dumps({"input_tokens": 3, "output_tokens": 1, "service_tier": "standard",
                      "note": "free text " + CANARY, "ratio": 0.5, "neg": -1,
                      "bad key!": 1, "tokenType": "input", "contextTier": "galaxy",
                      "totalNanoAiu": "12", "iterations": [{"type": "message",
                                                            "model": "claude-opus-5-5"}]})
    att = dataclasses.replace(base.attempts[0], raw_usage_json=raw)
    path = tmp_path / "t.jsonl"
    T.write_trace_v2(path, header=header("usage", name_key_id=None, principal_key_id=None),
                     requests=[dataclasses.replace(base, attempts=(att,))])
    assert_no_canary(path.read_bytes())
    got = json.loads(_read(path).requests[0].final_attempt.raw_usage_json)
    assert got == {"input_tokens": 3, "output_tokens": 1, "service_tier": "standard",
                   "note": None, "ratio": None, "neg": None, "tokenType": "input",
                   "contextTier": None, "totalNanoAiu": None,
                   "iterations": [{"type": "message", "model": "claude-opus-5-5"}]}
    assert T.sanitize_raw_usage(None) is None
    assert T.sanitize_raw_usage("not json") is None
    assert T.sanitize_raw_usage("[1]") is None
    assert T.sanitize_raw_usage(json.dumps({"a": [1] * 5000})) is None
    deep: Any = 1
    for _ in range(12):
        deep = {"d": deep}
    assert "d" in T.sanitize_raw_usage(json.dumps(deep))
    assert T.sanitize_raw_usage('{"initiator":"agent","b":true,"x":[1,"s",null]}') == \
        {"initiator": "agent", "b": True, "x": [1, None, None]}


def test_make_header_validation() -> None:
    hdr = T.make_header({"trace_id": "t_x", "profile": "usage"})
    assert hdr.identity_mode == "install" and dict(hdr.producer)["name"] == "tokenbill"
    assert T.make_header(hdr) == hdr
    assert T.make_header({"trace_id": "t_x", "profile": "usage",
                          "producer": [("name", "p")]}).producer == (("name", "p"),)
    bad = [{"profile": "usage"}, {"trace_id": "x", "profile": "usage"},
           {"trace_id": "t_x", "profile": "nope"},
           {"trace_id": "t_x", "profile": "usage", "identity_mode": "open"},
           {"trace_id": "t_x", "profile": "usage", "name_key_id": "key"},
           {"trace_id": "t_x", "profile": "usage", "fp_key_id": FP_KID},
           {"trace_id": "t_x", "profile": "usage", "producer": {"name": "x", "os": "y"}},
           {"trace_id": "t_x", "profile": "usage", "producer": {"name": 1}},
           {"trace_id": "t_x", "profile": "usage", "created_ms": -1},
           {"trace_id": "t_x", "profile": "usage", "attribution": {"principal": "r_x"}},
           {"trace_id": "t_x", "profile": "usage", "extra": object()}]
    for h in bad:
        with pytest.raises(UsageError):
            T.make_header(h)
    with pytest.raises(UsageError):
        T.make_header("t_x")  # type: ignore[arg-type]


def test_writer_refuses_invalid_records(tmp_path: Path) -> None:
    fp, _, _ = fingerprint_of(conversation(1))
    req = make_request("ln", 0, T0, {"output": 1}, request_id="rq", fingerprint=fp)
    with pytest.raises(ContractViolation, match="fp_key_id"):
        T.write_trace_v2(tmp_path / "a.jsonl", header=header("fingerprint",
                                                             fp_key_id="k_000000000000"),
                         requests=[req])
    dup = dataclasses.replace(fp, blocks=(fp.blocks[0], dataclasses.replace(
        fp.blocks[0], est_tokens=999)) + fp.blocks[2:])
    with pytest.raises(ContractViolation, match="share a hash"):
        T.write_trace_v2(tmp_path / "b.jsonl", header=header("fingerprint"),
                         requests=[dataclasses.replace(req, fingerprint=dup)])
    long_note = DataQualityNote(code="dq.x", severity="info", count=1, detail="d" * 300)
    with pytest.raises(ContractViolation, match="bad_type:detail"):
        T.write_trace_v2(tmp_path / "c.jsonl", header=header("usage"), notes=[long_note])
    with pytest.raises(UsageError):
        T.TraceV2Writer(open(tmp_path / "d.jsonl", "w"), header("usage")).content("h", "t")


def test_writer_drops_note_figures_and_dedupes_requests(tmp_path: Path) -> None:
    s = sample("usage")
    note = dataclasses.replace(s.notes[0], figure=object())  # type: ignore[arg-type]
    path = tmp_path / "t.jsonl"
    lane = dataclasses.replace(s.sessions[0].lanes[0], requests=(s.requests[0],))
    session = dataclasses.replace(s.sessions[0], lanes=(lane, s.sessions[0].lanes[1]))
    n = T.write_trace_v2(path, header=s.header, sessions=[session], requests=s.requests,
                         notes=[note])
    back = _read(path)
    assert back.notes[0].figure is None
    assert [r.request_id for r in back.requests] == [r.request_id for r in s.requests]
    assert n == 1 + 1 + 1 + 2 + 3


def test_sniff_and_registry() -> None:
    sniff = T.TraceV2Adapter().sniff
    head = (FIXTURES / "trace2_usage.jsonl").read_bytes()[:65536]
    assert sniff(Path("x"), head)
    assert sniff(Path("x"), b"\xef\xbb\xbf" + head)
    assert not sniff(Path("x"), b'{"schema":"tokenbill/trace@1"}\n')
    assert not sniff(Path("x"), b'{"rec":"session","schema":"tokenbill/trace@2"}\n')
    assert sniff(Path("x"), b'{"rec":"header","schema":"tokenbill/trace@2","trace_id":"t_')
    assert not sniff(Path("x"), b'{"rec":"header","schema":"tokenbill/trace@2",\n{')
    assert not sniff(Path("x"), b'["header","tokenbill/trace@2"]\n')
    assert sniff_adapter(FIXTURES / "trace2_fingerprint.jsonl").name == "trace@2"
    assert isinstance(get_adapter("trace@2"), T.TraceV2Adapter)


def test_read_requires_options_and_a_readable_file(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        T.TraceV2Adapter().read(FIXTURES / "trace2_usage.jsonl", object())  # type: ignore
    with pytest.raises(SourceError):
        _read(tmp_path / "missing.jsonl")


def test_closed_key_sets_follow_the_core_dataclasses() -> None:
    """A-3: the key sets are derived with record_fields, so appended core fields are accepted."""
    assert T._S_COST_LINE.allowed == record_fields(CostLine) | {"rec", "schema"}
    assert {"quantity", "workload", "pseudo"} <= T._S_COST_LINE.allowed
    assert {"routing", "compliance", "context_tier"} <= T._S_PRICING.allowed
    assert "extra" in T._S_OUTCOME.allowed
    assert T._S_ATTEMPT.allowed == (record_fields(Attempt) - {"raw_usage_json"}) | {"raw_usage"}
    assert T._S_INFERENCE.required == {"inference_id", "kind", "usage", "pricing"}
    assert "figure" not in T._S_DQ.allowed


def test_empty_and_minimal_files(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    assert T.write_trace_v2(path, header=header("usage")) == 1
    result = _read(path)
    assert result.requests == [] and result.capabilities == frozenset()
    blank = tmp_path / "blank.jsonl"
    blank.write_text("")
    assert _read(blank).stats.get("records", 0) == 0


def test_principal_in_cost_lines_needs_a_principal_key_id(tmp_path: Path) -> None:
    lines = _lines("usage")
    lines[0]["principal_key_id"] = None
    for x in lines:
        if x["rec"] in ("request", "session"):
            x["attribution"]["principal"] = None
    result = _read(_dump(tmp_path / "t.jsonl", lines))
    assert [q.reason for q in result.quarantined] == ["bad_type:principal_key_id"]
    assert PRINCIPAL.encode() not in b"".join(q.reason.encode() for q in result.quarantined)


def test_inference_records_keep_copilot_fields(tmp_path: Path) -> None:
    result = _read(FIXTURES / "trace2_usage.jsonl")
    inf: Inference = result.requests[2].serving_inference
    assert (inf.pricing.routing, inf.pricing.compliance, inf.pricing.context_tier) == \
        ("auto", "fedramp", "long_context")
    assert result.cost_lines[0].amount_nano == -1_250_000
    assert result.aggregates[0].reported_cost_nano == -5
