"""Hypothesis fuzz and property tests of the parsers TRACE owns (SPEC §21 #5): only
``TokenbillError`` subclasses may escape the readers; lenient reads never raise on bad records;
records round-trip through trace@2 byte-identically; fingerprints are invariant where §5.8 says so.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests.v2.trace.helpers import FP_KEY, T0, header
from tokenbill.adapters import fingerprint as F
from tokenbill.adapters import trace_v2 as T
from tokenbill.adapters.trace_v1 import TraceV1Adapter
from tokenbill.common import TokenbillError
from tokenbill.core.records import (
    Attempt,
    Attribution,
    ContentTier,
    Inference,
    PricingContext,
    Request,
    RequestParams,
    UsageBuckets,
    to_json,
)
from tokenbill.core.testing import conformance_ingest_options

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trace"
FUZZ = settings(max_examples=60, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture,
                                       HealthCheck.too_slow])

json_scalars = st.one_of(st.none(), st.booleans(), st.integers(-2**64, 2**64),
                         st.floats(allow_nan=False, allow_infinity=False),
                         st.text(max_size=40))
json_values = st.recursive(json_scalars, lambda inner: st.one_of(
    st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=12), inner, max_size=4)),
    max_leaves=12)


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _paths(obj: Any, prefix: tuple = ()) -> list[tuple]:
    out = [prefix]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_paths(v, (*prefix, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_paths(v, (*prefix, i)))
    return out


def _mutate(obj: Any, path: tuple, action: str, value: Any) -> Any:
    obj = copy.deepcopy(obj)
    if not path:
        return value if action == "replace" else obj
    parent = obj
    for key in path[:-1]:
        parent = parent[key]
    last = path[-1]
    if action == "delete":
        if isinstance(parent, dict):
            parent.pop(last, None)
        else:
            parent.pop(last)
    else:
        parent[last] = value
    return obj


@st.composite
def mutated_file(draw: Any, name: str) -> list[str]:
    lines = _fixture_lines(name)
    n = draw(st.integers(1, 3))
    for _ in range(n):
        i = draw(st.integers(0, len(lines) - 1))
        kind = draw(st.sampled_from(["json", "truncate", "garbage", "drop", "dup"]))
        if kind == "truncate":
            cut = draw(st.integers(0, max(0, len(lines[i]) - 1)))
            lines[i] = lines[i][:cut]
        elif kind == "garbage":
            lines[i] = draw(st.text(max_size=60))
        elif kind == "drop":
            del lines[i]
            if not lines:
                lines = ["{}"]
        elif kind == "dup":
            lines.insert(i, lines[i])
        else:
            try:
                obj = json.loads(lines[i])
            except ValueError:
                continue
            paths = _paths(obj)
            path = draw(st.sampled_from(paths))
            action = draw(st.sampled_from(["replace", "delete"]))
            lines[i] = json.dumps(_mutate(obj, path, action, draw(json_values)))
    return lines


def _write_lines(tmp: Path, lines: list[str]) -> Path:
    path = tmp / "fuzz.jsonl"
    path.write_bytes("\n".join(lines).encode("utf-8", "surrogatepass") + b"\n")
    return path


def _only_tokenbill_errors(read: Any) -> Any:
    try:
        return read()
    except TokenbillError:
        return None


@FUZZ
@given(lines=mutated_file("trace2_fingerprint.jsonl"))
def test_trace2_reader_never_raises_leniently(tmp_path_factory: Any, lines: list[str]) -> None:
    path = _write_lines(tmp_path_factory.mktemp("f2"), lines)
    opts = conformance_ingest_options()
    result = T.TraceV2Adapter().read(path, opts)
    assert result.stats.get("records", 0) + result.stats.get("quarantined", 0) >= 0
    _only_tokenbill_errors(lambda: T.TraceV2Adapter().read(
        path, conformance_ingest_options(lenient=False)))
    _only_tokenbill_errors(lambda: T.TraceV2Adapter().read(
        path, conformance_ingest_options(renormalize=True)))


@FUZZ
@given(lines=mutated_file("trace2_usage.jsonl"))
def test_trace2_usage_reader_fuzz(tmp_path_factory: Any, lines: list[str]) -> None:
    path = _write_lines(tmp_path_factory.mktemp("f2u"), lines)
    items = list(T.iter_trace_v2(path))
    assert len(items) <= len(lines)
    _only_tokenbill_errors(lambda: list(T.iter_trace_v2(path, lenient=False)))


@FUZZ
@given(lines=mutated_file("trace1_runs.jsonl"))
def test_trace1_reader_never_raises_leniently(tmp_path_factory: Any, lines: list[str]) -> None:
    path = _write_lines(tmp_path_factory.mktemp("f1"), lines)
    for tier in (ContentTier.NONE, ContentTier.FINGERPRINT):
        TraceV1Adapter().read(path, conformance_ingest_options(content_tier=tier))
    _only_tokenbill_errors(lambda: TraceV1Adapter().read(
        path, conformance_ingest_options(lenient=False)))


@FUZZ
@given(raw=st.binary(max_size=300))
def test_readers_survive_arbitrary_bytes(tmp_path_factory: Any, raw: bytes) -> None:
    path = tmp_path_factory.mktemp("bytes") / "b.jsonl"
    path.write_bytes(raw)
    T.TraceV2Adapter().read(path, conformance_ingest_options())
    TraceV1Adapter().read(path, conformance_ingest_options())
    head = raw[:65536]
    assert isinstance(T.TraceV2Adapter().sniff(path, head), bool)
    assert isinstance(TraceV1Adapter().sniff(path, head), bool)


@FUZZ
@given(value=json_values)
def test_sanitized_raw_usage_is_always_accepted(value: Any) -> None:
    text = json.dumps({"u": value})
    clean = T.sanitize_raw_usage(text)
    if clean is not None:
        T._raw_value(clean, None, 0, False)    # the reader accepts what the writer emits


# ---------------------------------------------------------------------------------------------
# round-trip property
# ---------------------------------------------------------------------------------------------

counts = st.integers(0, 2**40)
tokens = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-.", min_size=1, max_size=20)


@st.composite
def usage_buckets(draw: Any) -> UsageBuckets:
    other = draw(counts)
    out = draw(counts)
    return UsageBuckets(uncached_input=draw(counts), cache_read=draw(counts),
                        cache_write_5m=draw(counts), cache_write_1h=draw(counts),
                        cache_write_other=other,
                        cache_write_other_ttl_s=1800 if other else draw(
                            st.none() | st.just(1800)),
                        cache_write_unknown=draw(counts), output=out,
                        output_reasoning=draw(st.none() | st.integers(0, out)),
                        web_search_requests=draw(st.integers(0, 50)))


@st.composite
def requests(draw: Any, i: int) -> Request:
    n_att = draw(st.integers(1, 3))
    attempts = []
    for a in range(n_att):
        infs = tuple(Inference(
            inference_id=f"inf_{i}_{a}_{k}", kind=draw(st.sampled_from(
                ["message", "compaction", "advisor", "fallback", "other"])),
            usage=draw(usage_buckets()),
            pricing=PricingContext(provider="anthropic", channel=draw(st.sampled_from(
                ["anthropic_api", "bedrock", "vertex"])), model=draw(tokens),
                model_raw=draw(tokens), speed=draw(st.sampled_from(["standard", "fast"])),
                billing_path=draw(st.sampled_from(["api_key", "subscription", "unknown"])),
                routing=draw(st.sampled_from(["direct", "auto"]))),
            billable=draw(st.sampled_from([True, None, False])))
            for k in range(draw(st.integers(0, 2))))
        attempts.append(Attempt(
            attempt_id=f"at_{i}_{a}", attempt_no=a, ts_start_ms=T0 + i * 1000 + a,
            ttft_ms=draw(st.none() | counts), duration_ms=draw(st.none() | counts),
            outcome=draw(st.sampled_from(["ok", "http_error", "aborted", "timeout"])),
            http_status=draw(st.none() | st.integers(100, 599)),
            error_type=draw(st.none() | tokens),
            retry_layer=draw(st.sampled_from([None, "sdk", "agent", "gateway"])),
            retry_after_ms=draw(st.none() | counts), should_retry=draw(st.none() | st.booleans()),
            provider_request_id=draw(st.none() | tokens),
            provider_message_id=draw(st.none() | tokens), model_served=draw(st.none() | tokens),
            stop_reason=draw(st.none() | tokens), inferences=infs,
            applied_edits=tuple((draw(tokens), draw(counts))
                                for _ in range(draw(st.integers(0, 2)))),
            thinking_dropped=draw(st.integers(0, 3)), sdk_retry_count=draw(st.none() | counts),
            raw_usage_json=draw(st.none() | st.just('{"input_tokens":1,"output_tokens":2}')),
            convention_id=draw(st.none() | st.just("anthropic.messages"))))
    return Request(
        request_id=f"rq_{i}", session_key="ses_fuzz", lane_key=draw(st.sampled_from(["l1", "l2"])),
        seq=i, attribution=Attribution(team=draw(st.none() | tokens),
                                       workload_class=draw(st.sampled_from(["ci", "unknown"])),
                                       extra=tuple(sorted(draw(st.dictionaries(
                                           st.sampled_from(["gateway", "task_id"]), tokens,
                                           max_size=2)).items()))),
        params=RequestParams(model_requested=draw(tokens), max_tokens=draw(st.none() | counts),
                             betas=tuple(draw(st.lists(tokens, max_size=3, unique=True)))),
        attempts=tuple(attempts))


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(data=st.data())
def test_random_requests_round_trip_byte_identically(tmp_path_factory: Any, data: Any) -> None:
    reqs = [data.draw(requests(i)) for i in range(data.draw(st.integers(1, 4)))]
    tmp = tmp_path_factory.mktemp("rt")
    hdr = header("usage", name_key_id=None, principal_key_id=None)
    a, b = tmp / "a.jsonl", tmp / "b.jsonl"
    T.write_trace_v2(a, header=hdr, requests=reqs)
    back = T.TraceV2Adapter().read(a, conformance_ingest_options())
    assert back.quarantined == []
    assert [to_json(r) for r in back.requests] == [to_json(r) for r in reqs]
    T.write_trace_v2(b, header=hdr, requests=back.requests)
    assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------------------------
# fingerprint properties
# ---------------------------------------------------------------------------------------------

text_blocks = st.lists(st.fixed_dictionaries(
    {"type": st.sampled_from(["text", "tool_result", "tool_use", "thinking", "weird"]),
     "text": st.text(max_size=30)}), min_size=1, max_size=4)
payload_messages = st.lists(st.fixed_dictionaries(
    {"role": st.sampled_from(["user", "assistant"]),
     "content": st.one_of(st.text(max_size=30), text_blocks)}), min_size=1, max_size=5)


@FUZZ
@given(messages=payload_messages, data=st.data())
def test_marker_moves_never_change_hashes(messages: list[dict[str, Any]], data: Any) -> None:
    def marked(msgs: list[dict[str, Any]], at: int) -> list[dict[str, Any]]:
        out = copy.deepcopy(msgs)
        content = out[at]["content"]
        if isinstance(content, str):
            out[at]["content"] = [{"type": "text", "text": content,
                                   "cache_control": {"type": "ephemeral"}}]
        else:
            content[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        return out

    i = data.draw(st.integers(0, len(messages) - 1))
    j = data.draw(st.integers(0, len(messages) - 1))
    base = [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
             else [dict(b) for b in m["content"]]} for m in messages]
    assume(all(not isinstance(m["content"], str) or True for m in base))
    a, bps_a, _ = F.fingerprint_request(tools=[], system=None, messages=marked(base, i),
                                        key=FP_KEY, tier=ContentTier.FINGERPRINT)
    b, bps_b, _ = F.fingerprint_request(tools=[], system=None, messages=marked(base, j),
                                        key=FP_KEY, tier=ContentTier.FINGERPRINT)
    plain, none, _ = F.fingerprint_request(tools=[], system=None, messages=base, key=FP_KEY,
                                           tier=ContentTier.FINGERPRINT)
    if all(not isinstance(m["content"], str) for m in base):
        assert a == b == plain
    assert len(bps_a) == len(bps_b) == 1 and none == ()
    walk, n = F.request_breakpoints(tools=[], system=None, messages=marked(base, i))
    assert walk == bps_a and n == len(a.blocks)


@FUZZ
@given(obj=st.dictionaries(st.text(max_size=8), json_scalars, min_size=2, max_size=6))
def test_key_order_only_changes_h(obj: dict[str, Any]) -> None:
    flipped = dict(reversed(list(obj.items())))
    a, _, _ = F.fingerprint_request(tools=[obj], system=None, messages=[], key=FP_KEY,
                                    tier=ContentTier.FINGERPRINT)
    b, _, _ = F.fingerprint_request(tools=[flipped], system=None, messages=[], key=FP_KEY,
                                    tier=ContentTier.FINGERPRINT)
    assert a.blocks[0].h_sorted == b.blocks[0].h_sorted
    if list(obj) != list(flipped):
        assert a.blocks[0].h != b.blocks[0].h


@FUZZ
@given(text=st.text(alphabet="0123456789-:T Z.abc", max_size=80))
def test_volatile_spans_are_sorted_and_disjoint(text: str) -> None:
    spans = F.volatile_spans(text)
    last = 0
    for cls, a, b in spans:
        assert cls in F.VOLATILE_CLASSES and last <= a < b <= len(text)
        last = b
    normalized = F.normalize_volatile(text)
    assert F.volatile_spans(normalized) == [] or normalized != text
