"""SPEC §5.2: the seven TELEM conventions, golden sum-check fixtures and the shared parsers."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.v2.telemetry.helpers import GOLDEN, ORG_KEY, central, p_of
from tokenbill.adapters import conventions_ext as ext
from tokenbill.core import conventions
from tokenbill.core.conventions import BadUsageError
from tokenbill.core.errors import SourceError, TokenbillError, UsageError
from tokenbill.core.ids import pseudonym
from tokenbill.core.records import UsageBuckets
from tokenbill.core.testing import conformance_ingest_options

CASES = json.loads(GOLDEN.read_text())["cases"]
TELEM_CONVENTIONS = ("bedrock.converse", "openai.responses", "openai.chat", "otel.genai",
                     "otel.genai.legacy", "openinference", "claude_code.otel")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_sum_check_fixture(case: dict[str, Any]) -> None:
    buckets, codes = conventions.normalize(case["convention"], case["raw"])
    assert buckets == UsageBuckets(**case["buckets"])
    assert codes == case["codes"]
    total = case["provider_total_input"]
    if total is not None:  # Σ buckets reproduces the provider's billed input total
        assert conventions.sum_check(buckets, total, None) == []
        assert buckets.total_input == total


def test_brief_openai_responses_case() -> None:
    buckets, codes = conventions.normalize("openai.responses", {
        "input_tokens": 10_000, "output_tokens": 0,
        "input_tokens_details": {"cached_tokens": 6_000, "cache_write_tokens": 2_000}})
    assert (buckets.uncached_input, buckets.cache_read, buckets.cache_write_other,
            buckets.cache_write_other_ttl_s) == (2_000, 6_000, 2_000, 1_800)
    assert codes == []


def test_every_convention_is_registered_on_import() -> None:
    registered = {c.convention_id: c for c in conventions.registered_conventions()}
    for cid in TELEM_CONVENTIONS:
        conv = registered[cid]
        assert conv.enabled
        assert conventions.get_convention(cid) == conv
    inclusive = {cid for cid in TELEM_CONVENTIONS if registered[cid].inclusive_input}
    assert inclusive == {"openai.responses", "openai.chat", "otel.genai", "otel.genai.legacy",
                         "openinference"}
    for conv, fn in ext._CONVENTIONS:  # re-registration (a re-import) is idempotent
        conventions.register_convention(conv, fn)
    assert {c.convention_id for c in conventions.registered_conventions()} >= set(
        TELEM_CONVENTIONS)


@pytest.mark.parametrize("convention, raw", [
    ("openai.responses", {"input_tokens": 100, "output_tokens": 5,
                          "input_tokens_details": {"cached_tokens": 90,
                                                   "cache_write_tokens": 20}}),
    ("openai.chat", {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 11}}),
])
def test_negative_uncached_residual_fails_the_sum_check(convention: str,
                                                        raw: dict[str, Any]) -> None:
    with pytest.raises(ext.SumCheckError, match="bad_usage"):
        conventions.normalize(convention, raw)
    assert issubclass(ext.SumCheckError, BadUsageError)


def test_openai_total_tokens_and_reasoning_checks() -> None:
    _b, codes = conventions.normalize("openai.responses", {
        "input_tokens": 100, "output_tokens": 10, "total_tokens": 999})
    assert codes == ["dq.sum_check_failed"]
    buckets, codes = conventions.normalize("openai.responses", {
        "input_tokens": 100, "output_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 11}})
    assert buckets.output_reasoning is None and codes == ["dq.sum_check_failed"]
    empty, codes = conventions.normalize("openai.chat", {})
    assert empty == UsageBuckets() and codes == []


@pytest.mark.parametrize("convention, raw", [
    ("openai.responses", {"input_tokens": -1}),
    ("openai.responses", {"input_tokens": "12"}),
    ("openai.responses", {"input_tokens_details": []}),
    ("openai.chat", {"completion_tokens_details": {"rejected_prediction_tokens": 1.5}}),
    ("bedrock.converse", {"inputTokens": True}),
    ("bedrock.converse", {"cacheDetails": {"ttl": "5m"}}),
    ("bedrock.converse", {"cacheDetails": ["5m"]}),
    ("bedrock.converse", {"cacheDetails": [{"ttl": "5m", "inputTokens": 2**60}]}),
    ("otel.genai", {"gen_ai.usage.input_tokens": 2**54}),
    ("otel.genai", {"gen_ai.usage.cache_read.input_tokens": 2**53,
                    "gen_ai.usage.cache_write.input_tokens": 2**53}),
    ("openinference", {"llm.token_count.total": "x"}),
    ("claude_code.otel", {"cache_creation_tokens": None, "input_tokens": 3.0}),
])
def test_malformed_usage_raises_bad_usage(convention: str, raw: dict[str, Any]) -> None:
    with pytest.raises(BadUsageError, match="bad_usage"):
        conventions.normalize(convention, raw)


@pytest.mark.parametrize("fn", [ext.normalize_bedrock_converse, ext.normalize_openai_chat,
                                ext.normalize_openai_responses, ext.normalize_otel_genai,
                                ext.normalize_otel_genai_legacy, ext.normalize_openinference,
                                ext.normalize_claude_code_otel])
def test_normalizers_refuse_non_mappings(fn: Any) -> None:
    with pytest.raises(BadUsageError):
        fn([1, 2])  # type: ignore[arg-type]


def test_bedrock_details_edge_cases() -> None:
    b, codes = ext.normalize_bedrock_converse({
        "inputTokens": 1, "cacheWriteInputTokens": 100,
        "cacheDetails": [{"ttl": "5m", "inputTokens": 80}, {"ttl": "1h", "inputTokens": 40}]})
    assert (b.cache_write_5m, b.cache_write_1h, b.cache_write_unknown) == (80, 40, 0)
    assert codes == ["dq.sum_check_failed"]  # Σ cacheDetails ≤ cacheWrite violated; split kept
    b, codes = ext.normalize_bedrock_converse({
        "cacheWriteInputTokens": 50, "cacheDetails": [{"ttl": "30m", "inputTokens": 50}]})
    assert b.cache_write_unknown == 50 and codes == ["dq.unknown_fields"]
    b, codes = ext.normalize_bedrock_converse({"cacheDetails": [{"ttl": "1h", "inputTokens": 7}]})
    assert b.cache_write_1h == 7 and codes == []  # no total: the split is the total
    b, codes = ext.normalize_bedrock_converse({"cacheDetails": []})
    assert b == UsageBuckets() and codes == []


def test_legacy_prefers_new_names_when_both_present() -> None:
    b, _ = ext.normalize_otel_genai_legacy({
        "gen_ai.usage.input_tokens": 100, "gen_ai.usage.prompt_tokens": 999,
        "gen_ai.usage.output_tokens": 5, "gen_ai.usage.completion_tokens": 999,
        "gen_ai.usage.cache_write.input_tokens": 10})
    assert (b.uncached_input, b.cache_write_unknown, b.output) == (90, 10, 5)


# ---------------------------------------------------------------------------------------------
# properties and fuzz (only TokenbillError subclasses may escape)
# ---------------------------------------------------------------------------------------------

_VALUES = st.one_of(st.none(), st.booleans(), st.integers(-5, 2**54), st.floats(),
                    st.text(max_size=5), st.lists(st.integers(0, 10), max_size=2),
                    st.dictionaries(st.sampled_from(["ttl", "inputTokens", "cached_tokens",
                                                     "cache_write_tokens", "reasoning_tokens"]),
                                    st.one_of(st.integers(-1, 10**6), st.sampled_from(
                                        ["5m", "1h", "x"])), max_size=3))
_KEYS = sorted({*ext.GENAI_LEGACY_KEYS, *ext.GENAI_KEYS, *ext.OPENINFERENCE_KEYS,
                *ext.CLAUDE_CODE_OTEL_KEYS, "inputTokens", "outputTokens",
                "cacheReadInputTokens", "cacheWriteInputTokens", "cacheDetails", "input_tokens",
                "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens_details",
                "output_tokens_details", "prompt_tokens_details", "completion_tokens_details"})


@settings(max_examples=300, deadline=None)
@given(convention=st.sampled_from(TELEM_CONVENTIONS),
       raw=st.dictionaries(st.sampled_from(_KEYS), _VALUES, max_size=6))
def test_fuzz_normalizers_only_raise_tokenbill_errors(convention: str,
                                                      raw: dict[str, Any]) -> None:
    try:
        buckets, codes = conventions.normalize(convention, raw)
    except TokenbillError:
        return
    assert isinstance(buckets, UsageBuckets)
    assert all(c.startswith("dq.") for c in codes)


@settings(max_examples=200, deadline=None)
@given(inp=st.integers(0, 10**9), read=st.integers(0, 10**9), write=st.integers(0, 10**9),
       out=st.integers(0, 10**7))
def test_inclusive_span_conventions_preserve_the_provider_total(inp: int, read: int, write: int,
                                                                 out: int) -> None:
    raw = {"gen_ai.usage.input_tokens": inp, "gen_ai.usage.cache_read.input_tokens": read,
           "gen_ai.usage.cache_write.input_tokens": write, "gen_ai.usage.output_tokens": out}
    b, codes = ext.normalize_otel_genai(raw)
    if read + write <= inp:
        assert b.total_input == inp and codes == []
    else:  # exclusive treatment keeps every reported token
        assert b.total_input == inp + read + write and codes == ["dq.convention_mismatch"]
    assert b.output == out


@settings(max_examples=200, deadline=None)
@given(inp=st.integers(0, 10**9), cached=st.integers(0, 10**9), write=st.integers(0, 10**9))
def test_openai_responses_buckets_sum_to_input(inp: int, cached: int, write: int) -> None:
    raw = {"input_tokens": inp, "output_tokens": 1,
           "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": write}}
    if cached + write > inp:
        with pytest.raises(ext.SumCheckError):
            ext.normalize_openai_responses(raw)
        return
    b, _ = ext.normalize_openai_responses(raw)
    assert b.total_input == inp
    assert (b.cache_write_other_ttl_s is not None) == (write > 0)


# ---------------------------------------------------------------------------------------------
# shared parsers and plumbing
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2026-09-23T09:00:00Z", 1_790_154_000_000),
    ("2026-09-23T09:00:00.250Z", 1_790_154_000_250),
    ("2026-09-23T09:00:00.123456789Z", 1_790_154_000_123),
    ("2026-09-23T11:00:00+02:00", 1_790_154_000_000),
    ("2026-09-23T04:30:00-0430", 1_790_154_000_000),
    ("2026-09-23 09:00:00", 1_790_154_000_000),
    ("2026-09-23", 1_790_121_600_000),
    ("2026-13-01T00:00:00Z", None), ("1969-12-31T00:00:00Z", None), ("yesterday", None),
    (12, None), ("9" * 80, None),
])
def test_parse_iso_ms(text: object, expected: int | None) -> None:
    assert ext.parse_iso_ms(text) == expected


@pytest.mark.parametrize("value, expected", [
    (5, 5), ("17", 17), (" 42 ", 42), (3.0, 3), (-1, None), ("-3", None), (2.5, None),
    (True, None), (float("nan"), None), (2**54, None), ("1" * 40, None), (None, None),
])
def test_to_int(value: object, expected: int | None) -> None:
    assert ext.to_int(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("0.1375", 137_500_000), (0.24756, 247_560_000), (2, 2_000_000_000),
    ("0.0000000005", 0), ("0.0000000015", 2), (-1, None), ("NaN", None), ("abc", None),
    (float("inf"), None), (True, None), (None, None), ([1], None), ("1" * 70, None),
    ("1e4001", None),
])
def test_usd_to_nano(value: object, expected: int | None) -> None:
    assert ext.usd_to_nano(value) == expected


def test_usd_to_nano_never_uses_binary_float_digits() -> None:
    assert ext.usd_to_nano(0.1) == 100_000_000  # not 100000000.00000000555
    assert Decimal(repr(0.1)) == Decimal("0.1")


def test_clean_label_and_canonical_usage_json() -> None:
    assert ext.clean_label(" claude-opus-5-5 ") == "claude-opus-5-5"
    assert ext.clean_label("a b") is None
    assert ext.clean_label("x" * 65) is None
    assert ext.clean_label("arn:aws:x/y", enum=True) is None
    assert ext.clean_label(7) is None
    text = ext.canonical_usage_json({"b": 1, "a": {"tier": "standard", "note": "free text!",
                                                   "f": 1.5, "n": None, "t": True},
                                     "list": [1, "x y", {"k": 2}], 3: 4})
    assert text == '{"a":{"n":null,"t":true,"tier":"standard"},"b":1,"list":[1,{"k":2}]}'
    assert ext.canonical_usage_json({"x": [{"y": 1}] * 2000}) is None  # beyond 8 KiB
    deep: dict[str, Any] = {"v": 1}
    for _ in range(12):
        deep = {"d": deep}
    assert "v" not in (ext.canonical_usage_json(deep) or "")


def test_cache_scope_table() -> None:
    assert ext.cache_scope("anthropic_api", "ws1") == "ws:ws1"
    assert ext.cache_scope("foundry", None) == "ws:unknown"
    assert ext.cache_scope("bedrock", "h_x") == "org:bedrock:h_x"
    assert ext.cache_scope("openai_api", None) == "org:openai_api:unknown"
    assert ext.cache_scope("azure_openai", "h_s") == "sub:h_s"
    assert ext.cache_scope("copilot", "x") == "unknown"  # additive channels: table-driven


def test_identity_modes() -> None:
    raw = "alice@example.com"
    assert ext.principal_for(central(), raw) == p_of(raw)
    assert ext.principal_for(conformance_ingest_options(), raw) == pseudonym(
        conformance_ingest_options().principal_key or b"", "p", raw)
    assert ext.principal_for(central(principal_key=None), raw) is None
    assert ext.principal_for(central(), None) is None
    assert ext.principal_for(central(identity_mode="central", principal_ref="dev-7"),
                             raw) == "r_dev-7"
    assert ext.principal_for(central(identity_mode="central"), raw) is None
    assert ext.principal_for(central(identity_mode="two-stage", principal_ref="dev-7"),
                             raw) == pseudonym(ORG_KEY, "c", "dev-7")
    assert ext.principal_for(central(identity_mode="two-stage"), raw) is None
    with pytest.raises(UsageError):
        ext.check_identity_mode(central(identity_mode="laptop"))
    with pytest.raises(UsageError):
        ext.check_identity_mode(central(identity_mode="central", principal_ref="a@b.c"))


def test_team_map_and_names() -> None:
    opts = central(team_map=(("alice@example.com", "payments"),),
                   name_allowlist=frozenset({"github"}))
    assert ext.team_for(opts, [None, "bob", "alice@example.com"]) == "payments"
    assert ext.team_for(central(team_map=()), ["alice@example.com"]) is None
    assert ext.name_or_hash(opts, "github") == "github"
    assert ext.name_or_hash(opts, "Bash", frozenset({"Bash"})) == "Bash"
    assert ext.name_or_hash(opts, "mcp__vault__read").startswith("h_")
    assert ext.name_or_hash(opts, "  ") is None and ext.name_or_hash(opts, 3) is None


def test_error_type_table_and_key_parts() -> None:
    assert ext.error_type_for(None) == "connection"
    assert ext.error_type_for(529) == "overloaded"
    assert ext.error_type_for(429) == "rate_limit"
    assert ext.error_type_for(418) == "other"
    assert ext.key_part(7) == "7" and ext.key_part(True) is None
    assert ext.key_part(" s ") == "s" and ext.key_part("x" * 600) is None
    assert ext.meta_ts({"ts": "2026-09-23T09:00:00Z"}) == 1_790_154_000_000
    assert ext.meta_ts(None) is None


def test_head_record() -> None:
    obj, line = ext.head_record(b'\n\n{"a": 1}\n{"b": 2}')
    assert obj == {"a": 1} and line == b'{"a": 1}'
    obj, line = ext.head_record(b'{"a": "unterminated')
    assert obj is None and line.startswith(b"{")
    assert ext.head_record(b"   \n") == (None, b"")
    assert ext.head_record(b"[1]\n") == (None, b"")


def test_source_scan_strict_mode_raises(tmp_path: Any) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text("{}\n")
    scan = ext.SourceScan("otlp", path, central(lenient=False))
    with pytest.raises(SourceError, match="line:3: bad_json"):
        scan.quarantine("line:3", "bad_json")


def test_clean_attr_never_passes_emails_or_paths() -> None:
    assert ext.clean_attr(" Platform Eng ") == "Platform Eng"
    assert ext.clean_attr("cc-42") == "cc-42"
    assert ext.clean_attr("alice@example.com") is None
    assert ext.clean_attr("/home/alice/repo") is None
    assert ext.clean_attr("x" * 65) is None and ext.clean_attr("   ") is None
    assert ext.clean_attr(12) is None
