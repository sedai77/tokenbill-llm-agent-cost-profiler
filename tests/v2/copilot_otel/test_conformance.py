"""Conformance (``core.testing.assert_adapter_conforms``), the two conventions, the
``map_chat_spans`` module API and fixture reproducibility."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from tokenbill.adapters.copilot_otel import (
    CONVENTION,
    CopilotOtelAdapter,
    SpanEvent,
    SpanView,
    map_chat_spans,
    normalize_copilot_otel,
    product_for,
    service_products,
    time_ms,
)
from tokenbill.adapters.copilot_vscode import VsCodeAgentTracesAdapter
from tokenbill.adapters.gh_aw import CONVENTION as GH_AW_CONVENTION
from tokenbill.adapters.gh_aw import GhAwTokenUsageAdapter, normalize_gh_aw
from tokenbill.core.conventions import BadUsageError, get_convention, normalize
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.registry import BUILTIN_ADAPTERS, CONVENTION_MODULES, load
from tokenbill.core.testing import assert_adapter_conforms
from tokenbill.core.types import SourceInfo

from .helpers import (
    CLI_FILE,
    CLI_FLAG,
    COPILOT_OTLP,
    FIXTURES,
    GH_AW,
    JB_FLAG,
    JETBRAINS,
    MIXED,
    PRINCIPAL_KEY_ID,
    VSCODE_DUMP,
    build_db,
    extract_meta,
    opts,
    standard_spans,
)

OTEL_CAPS = {"usage_sequence", "timing", "params", "credits"}


# ---------------------------------------------------------------------------------------------
# conformance
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("path", "kw", "caps"), [
    (VSCODE_DUMP, {}, OTEL_CAPS),
    (VSCODE_DUMP, {"identity_mode": "central-ingest"}, OTEL_CAPS),
    (CLI_FILE, {"experimental": CLI_FLAG}, OTEL_CAPS | {"events"}),
    (COPILOT_OTLP, {}, OTEL_CAPS | {"events"}),
    (MIXED, {"otel_service_names": ("acme-copilot-proxy",)}, OTEL_CAPS),
    (JETBRAINS, {"otel_service_names": ("copilot-intellij=copilot_jetbrains",),
                 "experimental": JB_FLAG}, OTEL_CAPS),
    (CLI_FILE, {}, set()),
])
def test_copilot_otel_conforms(path: Path, kw: dict[str, object], caps: set[str]) -> None:
    assert_adapter_conforms(CopilotOtelAdapter(), path, expect_capabilities=caps, opts=opts(**kw))


def test_vscode_traces_conforms(tmp_path: Path) -> None:
    raw = build_db(tmp_path / "agent-traces.db", standard_spans())
    assert_adapter_conforms(VsCodeAgentTracesAdapter(), raw, expect_capabilities=OTEL_CAPS,
                            opts=opts())
    extract = build_db(tmp_path / "extract.db", standard_spans(), content=False,
                       meta=extract_meta())
    result = assert_adapter_conforms(VsCodeAgentTracesAdapter(), extract,
                                     expect_capabilities=OTEL_CAPS, opts=opts())
    assert result.source.principal_key_id == PRINCIPAL_KEY_ID


def test_gh_aw_conforms() -> None:
    env = {"GITHUB_REPOSITORY": "acme/repo", "GITHUB_WORKFLOW": "triage"}
    assert_adapter_conforms(GhAwTokenUsageAdapter(env=env), GH_AW,
                            expect_capabilities={"credits", "timing", "aggregates"}, opts=opts())


def test_registry_paths_resolve_to_these_classes() -> None:
    assert load(BUILTIN_ADAPTERS["copilot-otel"]) is CopilotOtelAdapter
    assert load(BUILTIN_ADAPTERS["copilot-vscode-traces"]) is VsCodeAgentTracesAdapter
    assert load(BUILTIN_ADAPTERS["gh-aw-token-usage"]) is GhAwTokenUsageAdapter
    assert "tokenbill.adapters.copilot_otel" in CONVENTION_MODULES
    assert "tokenbill.adapters.gh_aw" in CONVENTION_MODULES
    for adapter in (CopilotOtelAdapter(), VsCodeAgentTracesAdapter(), GhAwTokenUsageAdapter()):
        assert "credits" in adapter.capabilities


# ---------------------------------------------------------------------------------------------
# conventions
# ---------------------------------------------------------------------------------------------

def test_github_copilot_otel_convention() -> None:
    conv = get_convention(CONVENTION)
    assert conv.provider == "github" and conv.inclusive_input and conv.enabled
    buckets, notes = normalize(CONVENTION, {"gen_ai.usage.input_tokens": 100,
                                            "gen_ai.usage.cache_read.input_tokens": 60,
                                            "gen_ai.usage.cache_creation.input_tokens": 30,
                                            "gen_ai.usage.output_tokens": 9,
                                            "gen_ai.usage.reasoning_tokens": 4})
    assert (buckets.uncached_input, buckets.cache_read, buckets.cache_write_unknown,
            buckets.output, buckets.output_reasoning, notes) == (10, 60, 30, 9, 4, [])
    buckets, notes = normalize_copilot_otel({"gen_ai.usage.input_tokens": 10,
                                             "gen_ai.usage.cache_read.input_tokens": 60})
    assert (buckets.uncached_input, buckets.cache_read, notes) == (10, 60,
                                                                  ["dq.convention_mismatch"])
    assert normalize_copilot_otel({})[0].total_input == 0
    assert normalize_copilot_otel({"gen_ai.usage.input_tokens": 2**53})[0].uncached_input \
        == 2**53
    for bad in ({"gen_ai.usage.input_tokens": -1}, {"gen_ai.usage.output_tokens": True},
                {"gen_ai.usage.input_tokens": 2**53 + 1}, "not a mapping"):
        with pytest.raises(BadUsageError):
            normalize_copilot_otel(bad)  # type: ignore[arg-type]


def test_gh_aw_convention() -> None:
    conv = get_convention(GH_AW_CONVENTION)
    assert conv.provider == "github" and conv.enabled
    base = {"input_tokens": 100, "cache_read_tokens": 50, "cache_write_tokens": 20,
            "output_tokens": 10, "reasoning_tokens": 3}
    inclusive, notes = normalize_gh_aw({**base, "input_tokens_include_cache": True})
    assert (inclusive.uncached_input, inclusive.cache_write_unknown, notes) == (30, 20, [])
    exclusive, notes = normalize_gh_aw({**base, "input_tokens_include_cache": False})
    assert (exclusive.uncached_input, exclusive.total_input, notes) == (100, 170, [])
    decided, _ = normalize_gh_aw(base)
    assert decided.uncached_input == 30
    mismatch, notes = normalize_gh_aw({**base, "input_tokens": 10})
    assert (mismatch.uncached_input, notes) == (10, ["dq.convention_mismatch"])
    over, notes = normalize_gh_aw({**base, "reasoning_tokens": 99})
    assert over.output_reasoning is None and notes == ["dq.sum_check_failed"]
    for bad in ({"input_tokens_include_cache": "yes"}, {"output_tokens": 1.5}, [1]):
        with pytest.raises(BadUsageError):
            normalize_gh_aw(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# map_chat_spans and small helpers
# ---------------------------------------------------------------------------------------------

def _source() -> SourceInfo:
    return SourceInfo(source_id="s_test", adapter="copilot-otel", name_hmac="", sha256="0" * 64,
                      bytes=0, name_key_id=None, principal_key_id=None)


def test_map_chat_spans_module_api() -> None:
    span = SpanView(
        name="chat", trace_id="t1", span_id="s1", parent_span_id=None, start_ms=1_000,
        end_ms=1_500, attributes={"gen_ai.operation.name": "chat",
                                  "gen_ai.conversation.id": "conv",
                                  "gen_ai.response.model": "claude-opus-4.8",
                                  "gen_ai.response.id": "r1",
                                  "gen_ai.usage.input_tokens": 50,
                                  "gen_ai.usage.output_tokens": 5,
                                  "user.name": "someone"},
        resource={"service.name": "github-copilot"},
        events=(SpanEvent("github.copilot.session.shutdown", 1_600, {}),),
        locator="span:1", agent_product="copilot_cli")
    result = map_chat_spans([span], opts(), source=_source(), dialect="otel-js")
    (req,) = result.requests
    assert req.attribution.principal.startswith("p_")
    assert result.source.principal_key_id == key_id(opts().principal_key)
    assert [e.kind.value for e in result.events] == ["session_meta"]
    assert result.capabilities == OTEL_CAPS | {"events"}
    with pytest.raises(UsageError):
        map_chat_spans([span], opts(), source=_source(), dialect="nope")
    with pytest.raises(UsageError):
        map_chat_spans([span], opts(identity_mode="weird"), source=_source(), dialect="otel-js")
    with pytest.raises(UsageError):
        map_chat_spans([span], opts(identity_mode="central", principal_ref="a@b"),
                       source=_source(), dialect="otel-js")
    with pytest.raises(UsageError):
        map_chat_spans([span], object(), source=_source(), dialect="otel-js")  # type: ignore


def test_spans_without_conversation_fall_back_to_the_trace() -> None:
    spans = [SpanView(name="chat", trace_id="t9", span_id=f"s{i}", parent_span_id=None,
                      start_ms=1_000 + i, end_ms=None, attributes={
                          "gen_ai.usage.input_tokens": 5, "gen_ai.response.id": f"r{i}"},
                      locator=f"span:{i}", agent_product="copilot_other") for i in range(2)]
    result = map_chat_spans(spans, opts(), source=_source(), dialect="otlp-json")
    assert len({r.session_key for r in result.requests}) == 1
    assert result.stats["conversation_from_trace"] == 2
    orphan = SpanView(name="chat", trace_id=None, span_id=None, parent_span_id=None,
                      start_ms=5, end_ms=4, attributes={"gen_ai.usage.input_tokens": 1},
                      locator="span:x")
    (req,) = map_chat_spans([orphan], opts(), source=_source(), dialect="otlp-json").requests
    assert req.attempts[0].duration_ms is None
    assert req.attribution.agent_product == "copilot_other"


def test_service_products_and_time_parsing() -> None:
    configured = service_products(["a", "b=copilot_jetbrains", "c=bad", " =x", "a=copilot_other",
                                   "my-intellij", 7])  # type: ignore[list-item]
    assert configured == {"a": None, "b": "copilot_jetbrains", "c": "copilot_other",
                          "my-intellij": None}
    assert product_for("github-copilot", configured) == "copilot_cli"
    assert product_for("copilot-chat", configured) == "copilot_vscode"
    assert product_for("a", configured) == "copilot_other"
    assert product_for("b", configured) == "copilot_jetbrains"
    assert product_for("my-intellij", configured) == "copilot_jetbrains"
    assert product_for(None, configured) == "copilot_other"
    assert time_ms([1_790_157_600, 5_000_000]) == 1_790_157_600_005
    assert time_ms([1, 10**9]) is None and time_ms([-1, 0]) is None
    assert time_ms("1790157600123000000") == 1_790_157_600_123
    assert time_ms(1_790_157_600_123_000) == 1_790_157_600_123          # µs
    assert time_ms(1_790_157_600_123) == 1_790_157_600_123              # ms
    assert time_ms("2026-09-23T10:00:00.5+02:00") == 1_790_157_600_000 - 7_200_000 + 500
    assert time_ms("2026-02-31T00:00:00Z") is None
    assert time_ms(0) is None and time_ms(None) is None and time_ms("x") is None


# ---------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------

def test_checked_in_fixtures_equal_a_fresh_build(tmp_path: Path) -> None:
    module = runpy.run_path(str(FIXTURES / "build_fixtures.py"))
    texts = module["build"](tmp_path)
    for rel, text in texts.items():
        assert (FIXTURES / rel).read_text(encoding="utf-8") == text, rel
