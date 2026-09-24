"""Hypothesis fuzz and property tests of the CC parsers (SPEC §21 #5, §8.1).

* random truncation, field deletion and value replacement never raise anything but
  ``TokenbillError`` (both adapters, the collector);
* no ``none``-tier string field carries more than 64 bytes of source text, for transcripts seeded
  with long random canary strings (``assert_adapter_conforms`` performs the byte-window check);
* the hot-path shortcuts equal the core functions they stand in for.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tokenbill.adapters import claude_code as cc
from tokenbill.adapters.cc_collect import CollectorState, collect_incremental
from tokenbill.common import TokenbillError
from tokenbill.core import jsonl
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.conventions import BadUsageError, anthropic_inferences
from tokenbill.core.conventions import normalize_anthropic_messages as core_normalize
from tokenbill.core.records import PricingContext, UsageSource, to_json
from tokenbill.core.secrets import find_secrets
from tokenbill.core.testing import assert_adapter_conforms

from .helpers import CC, HEADLESS, bf, canonical, opts

FUZZ = settings(max_examples=60, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture,
                                       HealthCheck.too_slow])
ALPHA = bf.alpha_main().text()
STREAM = bf.headless_stream().jsonl()
ARRAY = bf.headless_execution_file().array()

json_values = st.recursive(
    st.none() | st.booleans() | st.integers(min_value=-(2**60), max_value=2**60)
    | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=80),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=12), children, max_size=4),
    max_leaves=8)


def _paths(obj: Any, prefix: tuple = ()) -> list[tuple]:
    out = [prefix] if prefix else []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _paths(v, (*prefix, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _paths(v, (*prefix, i))
    return out


def _mutate(text: str, data: st.DataObject) -> str:
    """Delete or replace random fields of random lines, then maybe truncate at a random byte."""
    lines = text.split("\n")
    for _ in range(data.draw(st.integers(0, 6))):
        i = data.draw(st.integers(0, len(lines) - 1))
        try:
            obj = json.loads(lines[i])
        except ValueError:
            continue
        paths = _paths(obj)
        if not paths:
            continue
        path = data.draw(st.sampled_from(paths))
        parent: Any = obj
        for key in path[:-1]:
            parent = parent[key]
        if data.draw(st.booleans()):
            if isinstance(parent, dict):
                del parent[path[-1]]
            else:
                parent.pop(path[-1])
        else:
            parent[path[-1]] = data.draw(json_values)
        lines[i] = json.dumps(obj)
    out = "\n".join(lines)
    if data.draw(st.booleans()):
        out = out[: data.draw(st.integers(0, len(out)))]
    return out


@FUZZ
@given(data=st.data())
def test_transcript_fuzz_never_raises_outside_tokenbill_errors(tmp_path: Path,
                                                               data: st.DataObject) -> None:
    path = tmp_path / "projects" / "-x" / "fuzz.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_mutate(ALPHA, data), encoding="utf-8")
    try:
        r = CC.read(path, opts(billing_path="subscription"))
    except TokenbillError:
        return
    assert_no_canary(repr(r), canonical(r))
    state = CollectorState()
    try:
        for res in collect_incremental(tmp_path / "projects", state, opts(),
                                       now_ms=bf.T0_MS):
            assert_no_canary(canonical(res))
    except TokenbillError:
        pass


@FUZZ
@given(data=st.data(), form=st.sampled_from(["jsonl", "array"]))
def test_headless_fuzz_never_raises_outside_tokenbill_errors(tmp_path: Path, data: st.DataObject,
                                                             form: str) -> None:
    text = _mutate(STREAM, data) if form == "jsonl" else _truncate(ARRAY, data)
    path = tmp_path / "h.jsonl"
    path.write_text(text, encoding="utf-8")
    try:
        r = HEADLESS.read(path, opts())
    except TokenbillError:
        return
    assert_no_canary(repr(r), canonical(r))


def _truncate(text: str, data: st.DataObject) -> str:
    return text[: data.draw(st.integers(0, len(text)))]


@FUZZ
@given(raw=st.binary(max_size=300))
def test_random_bytes_never_raise(tmp_path: Path, raw: bytes) -> None:
    for name, adapter in (("r.jsonl", CC), ("r.json", HEADLESS)):
        path = tmp_path / name
        path.write_bytes(raw)
        try:
            adapter.read(path, opts())
        except TokenbillError:
            pass
        assert isinstance(adapter.sniff(path, raw), bool)


long_text = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)),
                    min_size=65, max_size=200)


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(texts=st.lists(long_text, min_size=6, max_size=6), model=st.text(min_size=65,
                                                                         max_size=90))
def test_no_field_carries_more_than_64_bytes_of_source_text(tmp_path: Path, texts: list[str],
                                                            model: str) -> None:
    """Long, random strings in every content-bearing field (and as the model, version, stop
    reason, entrypoint and attribution names) never reach a record verbatim."""
    t = bf.Tx("44444444-0000-4000-8000-000000000004", cwd=texts[0], branch=texts[1],
              version=texts[2][:80], entrypoint=texts[3])
    t.human(CANARY + texts[4])
    t.call("msg_p1", "claude-opus-5-5", inp=5, outputs=(3, 60), stop="tool_use",
           blocks=[{"type": "text", "text": texts[5]},
                   {"type": "tool_use", "id": "toolu_p1", "name": texts[0], "input": texts[1]}],
           line_extra={"attributionSkill": texts[2], "attributionAgent": texts[3],
                       "perTurnEffort": texts[4]})
    t.tool_result("toolu_p1", texts[5])
    t.attachment(texts[0][:40], texts[1])
    t.call("msg_p2", model, inp=5, outputs=(9,), stop=texts[2], service_tier=texts[3])
    t.system("api_error", error={"status": texts[4], "message": texts[5]})
    t.system("model_refusal_fallback", originalModel=texts[0], fallbackModel=texts[1])
    path = t.write(tmp_path / "projects" / "-p" / "prop.jsonl")
    r = CC.read(path, opts())
    assert_adapter_conforms(CC, path, expect_capabilities=r.capabilities, opts=opts())
    assert_no_canary(repr(r), canonical(r))


# --- hot-path shortcuts equal the core functions --------------------------------------------------

count = st.one_of(st.none(), st.integers(min_value=0, max_value=2**53), st.integers(-5, -1),
                  st.just(True), st.just("7"), st.floats(allow_nan=False, allow_infinity=False))
usage_objects = st.fixed_dictionaries(
    {},
    optional={"input_tokens": count, "cache_read_input_tokens": count, "output_tokens": count,
              "cache_creation_input_tokens": count,
              "cache_creation": st.one_of(st.none(), st.just([]), st.fixed_dictionaries(
                  {}, optional={"ephemeral_5m_input_tokens": count,
                                "ephemeral_1h_input_tokens": count})),
              "server_tool_use": st.one_of(st.none(), st.just("x"), st.fixed_dictionaries(
                  {}, optional={"web_search_requests": count, "web_fetch_requests": count})),
              "output_tokens_details": st.one_of(st.none(), st.fixed_dictionaries(
                  {}, optional={"thinking_tokens": count})),
              "service_tier": st.sampled_from(["standard", "priority", "a b", ""]),
              "inference_geo": st.sampled_from(["us", "not_available", "x" * 70]),
              "iterations": st.one_of(st.none(), st.just([]))})


@settings(max_examples=400, deadline=None)
@given(u=usage_objects)
def test_usage_counts_equals_the_core_normalizer(u: dict) -> None:
    fast = cc.usage_counts(u)
    try:
        b, _ = core_normalize(u)
    except BadUsageError:
        assert fast is None
        return
    if fast is not None:
        assert fast == (b.uncached_input, b.cache_read, b.cache_write_5m, b.cache_write_1h,
                        b.cache_write_unknown, b.output, b.web_search_requests,
                        b.web_fetch_requests)


CTX = PricingContext(provider="anthropic", channel="anthropic_api", model="claude-opus-5-5",
                     model_raw="claude-opus-5-5")


@settings(max_examples=400, deadline=None)
@given(u=usage_objects, source=st.sampled_from([UsageSource.FINAL,
                                                 UsageSource.MESSAGE_START_ONLY]))
def test_message_inferences_equals_anthropic_inferences(u: dict, source: UsageSource) -> None:
    try:
        want, want_notes = anthropic_inferences(u, message_model="claude-opus-5-5", ctx=CTX,
                                                id_prefix="rq_x", usage_source=source)
    except BadUsageError:
        with pytest.raises(BadUsageError):
            cc.message_inferences(u, "claude-opus-5-5", CTX, "rq_x", source, None)
        return
    got, got_notes = cc.message_inferences(u, "claude-opus-5-5", CTX, "rq_x", source, None)
    assert [to_json(i) for i in got] == [to_json(i) for i in want]
    assert got_notes == want_notes


line_bytes = st.one_of(
    st.binary(max_size=80),
    json_values.map(lambda v: json.dumps(v).encode()),
    st.dictionaries(st.text(max_size=6), json_values, max_size=4).map(
        lambda d: json.dumps(d).encode()),
    st.sampled_from([b'{"a": NaN}', b'{"a": 1e999}', b'{"a": "\\ud800"}', b"\xef\xbb\xbf{}",
                     b"[" * 5000 + b"]" * 5000, b'{"a": "\\udc00\\ud800"}', b"",
                     b'{"a": "\\ud83d\\ude00"}']))


@settings(max_examples=400, deadline=None)
@given(raw=line_bytes)
def test_parse_line_equals_the_core_parser(raw: bytes) -> None:
    assert cc.parse_line(raw) == jsonl.parse_json_line(raw)


secret_texts = st.one_of(
    st.text(max_size=120),
    st.sampled_from(["key sk-ant-abcdefghijklmnop", "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a1" * 12,
                     "xoxb-1234567890-abc", "eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl",
                     "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
                     "Zx8Qp2Lm9Tr4Vb7Nc1Hk5Wj3Ys6Df0Ga", "github_pat_" + "A1" * 12]),
    st.text(alphabet="abcdefABCDEF0123456789+/=_-", min_size=30, max_size=60))


@settings(max_examples=400, deadline=None)
@given(text=secret_texts)
def test_secret_prefilter_is_a_superset(text: str) -> None:
    if find_secrets(text):
        assert cc._may_hold_secret(text)


@settings(max_examples=300, deadline=None)
@given(when=st.datetimes(min_value=dt.datetime(1970, 1, 2), max_value=dt.datetime(2200, 1, 1)),
       fmt=st.sampled_from(["Z", "+00:00", "+05:30", "-08:00", "", "frac6"]))
def test_parse_ts_ms_matches_datetime(when: dt.datetime, fmt: str) -> None:
    when = when.replace(microsecond=(when.microsecond // 1000) * 1000)
    if fmt == "frac6":
        text = when.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        aware = when.replace(tzinfo=dt.timezone.utc)
    elif fmt in ("Z", ""):
        text = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}" + fmt
        aware = when.replace(tzinfo=dt.timezone.utc)
    else:
        sign = 1 if fmt[0] == "+" else -1
        tz = dt.timezone(sign * dt.timedelta(hours=int(fmt[1:3]), minutes=int(fmt[4:6])))
        text = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}" + fmt
        aware = when.replace(tzinfo=tz)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    expected = (aware - epoch) // dt.timedelta(milliseconds=1)
    assume(expected >= 0)
    assert cc.parse_ts_ms(text) == expected


def test_parse_ts_ms_rejects_garbage() -> None:
    for bad in ("2026-13-01T00:00:00.000Z", "2026-01-01T25:00:00.000Z", "yesterday", None,
                "2026-01-01T00:00:00.000Zjunk", 1.5, -1, "1969-12-31T23:59:59.000Z",
                "2026-02-30T00:61:00.000Z", "2026-00-10T00:00:00Z"):
        assert cc.parse_ts_ms(bad) is None
    assert cc.parse_ts_ms(1_790_067_600_000) == 1_790_067_600_000
    assert cc.parse_ts_ms("2026-09-22 09:00:00") == 1_790_067_600_000


def test_parse_ts_ms_rejects_impossible_calendar_dates() -> None:
    for bad in ("2026-02-31T10:00:00.000Z", "2026-02-29T10:00:00.000Z", "2026-04-31T10:00:00Z",
                "2100-02-29T00:00:00.000Z", "2026-06-31T10:00:00+02:00"):
        assert cc.parse_ts_ms(bad) is None
    for good in ("2028-02-29T10:00:00.000Z", "2000-02-29T00:00:00Z", "2026-12-31T23:59:59.999Z"):
        assert cc.parse_ts_ms(good) is not None


iteration_objects = st.dictionaries(
    st.sampled_from(["type", "model", "input_tokens", "output_tokens", "cache_creation", "x"]),
    st.one_of(st.integers(-3, 10**9), st.sampled_from(["message", "claude-opus-5-5", "a b"]),
    st.none(), st.floats(allow_nan=False, allow_infinity=False),
    st.dictionaries(st.sampled_from(["ephemeral_5m_input_tokens", "y"]),
                    st.integers(-3, 10**6) | st.text(max_size=5), max_size=2)), max_size=5)
clean_usage = st.dictionaries(
    st.sampled_from(sorted(cc._USAGE_KEYS)),
    st.one_of(st.integers(0, 10**9), st.none(), st.booleans(), st.sampled_from(["standard", "us"]),
              st.dictionaries(st.sampled_from(sorted(cc._USAGE_NESTED_KEYS)),
                              st.integers(0, 10**6) | st.none(), max_size=3)),
    max_size=8)


@settings(max_examples=300, deadline=None)
@given(u=st.one_of(
    clean_usage, st.dictionaries(st.text(max_size=8), json_values, max_size=5),
    st.builds(lambda base, its: {**base, "iterations": its}, clean_usage,
              st.lists(iteration_objects | json_values, max_size=3))))
def test_safe_usage_json_fast_path_equals_the_slow_path(u: dict) -> None:
    fast = cc.safe_usage_json(u)
    slow = cc._safe_usage_json_slow(u)
    assert fast == slow
    if fast is not None:
        _check_raw_usage(json.loads(fast), top=True)


def _check_raw_usage(value: Any, *, top: bool = False, iteration: bool = False) -> None:
    """trace@2 raw_usage rules (§4.2): integers in [0, 2**53], no floats, strings only under the
    allowlisted enum keys."""
    for k, v in value.items():
        if isinstance(v, str):
            allowed = (top and k in ("service_tier", "speed", "inference_geo")) or (
                iteration and k in ("type", "model"))
            assert allowed and len(v) <= 64, k
        elif isinstance(v, dict):
            _check_raw_usage(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    _check_raw_usage(x, iteration=top and k == "iterations")
                else:
                    assert x is None or isinstance(x, bool) or (
                        isinstance(x, int) and 0 <= x <= 2**53)
        else:
            assert not isinstance(v, float)
            assert v is None or isinstance(v, bool) or 0 <= v <= 2**53
