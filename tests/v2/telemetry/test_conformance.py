"""``assert_adapter_conforms`` for all four TELEM adapters, registry integration, determinism
across processes, and ingest into the foundation ``MemoryStore``."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.v2.telemetry import helpers as h
from tokenbill.adapters.anthropic_responses import AnthropicResponsesAdapter
from tokenbill.adapters.bedrock import BedrockAdapter
from tokenbill.adapters.openai import OpenAIUsageAdapter
from tokenbill.adapters.otel import OtlpJsonAdapter
from tokenbill.core import registry
from tokenbill.core.ids import key_id
from tokenbill.core.protocols import Adapter
from tokenbill.core.records import LaneKind, to_json
from tokenbill.core.testing import FakePricer, MemoryStore, assert_adapter_conforms

BASE_CAPS = {"timing", "usage_sequence"}
CASES = [
    (OtlpJsonAdapter, h.OTLP_CC, BASE_CAPS | {"aggregates", "appended", "attempts", "events",
                                              "human_prompts", "lanes_exact", "params", "ttft"}),
    (OtlpJsonAdapter, h.OTLP_GENAI, BASE_CAPS | {"lanes_exact", "params"}),
    (OtlpJsonAdapter, h.OTLP_OI, BASE_CAPS),
    (OpenAIUsageAdapter, h.OPENAI, BASE_CAPS | {"attribution.team", "diagnostics", "params"}),
    (BedrockAdapter, h.BEDROCK, BASE_CAPS | {"attempts", "attribution.team"}),
    (AnthropicResponsesAdapter, h.ANTHROPIC,
     BASE_CAPS | {"attempts", "attribution.team", "diagnostics", "iterations", "lanes_exact",
                  "ttl_split"}),
]
IDS = [path.stem for _cls, path, _caps in CASES]


@pytest.mark.parametrize("cls, path, caps", CASES, ids=IDS)
def test_adapter_conforms_install_mode(cls: type, path: Path, caps: set[str]) -> None:
    result = assert_adapter_conforms(cls(), path, expect_capabilities=caps)
    assert h.CANARY not in h.blob(result)


@pytest.mark.parametrize("cls, path, caps", CASES, ids=IDS)
def test_adapter_conforms_central_ingest(cls: type, path: Path, caps: set[str]) -> None:
    opts = h.central()
    expected = set(caps)
    if path == h.OTLP_CC:
        expected.add("attribution.team")  # the fixture's user maps to a team
    result = assert_adapter_conforms(cls(), path, expect_capabilities=expected, opts=opts)
    text = h.blob(result)
    assert h.CANARY not in text and h.CANARY_EMAIL.lower() not in text
    for req in result.requests:
        principal = req.attribution.principal
        assert principal is None or principal.startswith("p_")
    if any(r.attribution.principal for r in result.requests):
        assert result.source.principal_key_id == key_id(h.ORG_KEY)


@pytest.mark.parametrize("cls, path, caps", CASES, ids=IDS)
def test_protocol_surface_and_registry(cls: type, path: Path, caps: set[str]) -> None:
    adapter = cls()
    assert isinstance(adapter, Adapter)
    assert set(caps) <= adapter.capabilities
    assert isinstance(registry.get_adapter(adapter.name), cls)
    sniffed = registry.sniff_adapter(path)
    assert sniffed is not None and sniffed.name == adapter.name


def test_each_fixture_is_claimed_by_exactly_one_telem_adapter() -> None:
    adapters = [OtlpJsonAdapter(), OpenAIUsageAdapter(), BedrockAdapter(),
                AnthropicResponsesAdapter()]
    for _cls, path, _caps in CASES:
        head = path.read_bytes()[:65536]
        assert sum(a.sniff(path, head) for a in adapters) == 1, path.name


def test_registry_normalize_loads_the_conventions_lazily() -> None:
    code = ("from tokenbill.core import conventions as c\n"
            "print(c.normalize('otel.genai', {'gen_ai.usage.input_tokens': 5})[0].uncached_input)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=Path(__file__).resolve().parents[3])
    assert out.stdout.strip() == "5"


def _dump_in_subprocess(path: Path, seed: str) -> str:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from tokenbill.core import registry\n"
        "from tokenbill.core.records import to_json\n"
        "from tests.v2.telemetry.helpers import central\n"
        "p = Path(sys.argv[1])\n"
        "r = registry.sniff_adapter(p).read(p, central())\n"
        "print(json.dumps(to_json(r), sort_keys=True))\n")
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True,
                         check=True, env=env, cwd=Path(__file__).resolve().parents[3])
    return out.stdout


@pytest.mark.parametrize("path", [h.OTLP_CC, h.OTLP_GENAI, h.OPENAI],
                         ids=["otlp_cc", "otlp_genai", "openai"])
def test_byte_identical_across_processes(path: Path) -> None:
    first, second = _dump_in_subprocess(path, "1"), _dump_in_subprocess(path, "4242")
    assert first == second
    here = registry.sniff_adapter(path).read(path, h.central())
    assert json.loads(first) == json.loads(json.dumps(to_json(here), sort_keys=True))


def test_everything_ingests_into_the_memory_store() -> None:
    store = MemoryStore(org_key=h.ORG_KEY, name_key_id=key_id(h.NAME_KEY), pricer=FakePricer())
    expected = 0
    for _cls, path, _caps in CASES:
        result = registry.sniff_adapter(path).read(path, h.central())
        counts = store.ingest(result)
        assert counts["principals_nulled"] == 0 and counts["names_nulled"] == 0
        assert counts["dq.name_key_mismatch"] == 0
        expected += len(result.requests)
        assert store.ingest(result)["skipped"] == 1  # idempotent
    lanes = list(store.iter_lanes())
    assert sum(len(lane.requests) for lane in lanes) == expected
    kinds = {lane.kind for lane in lanes}
    assert {LaneKind.MAIN, LaneKind.SUBAGENT, LaneKind.API_RUN} <= kinds
    assert [a.source_kind for a in store.aggregates("otel.metric")] == ["otel.metric"]


def test_a_genai_span_and_a_recorded_response_merge_by_message_id(tmp_path: Path) -> None:
    span_file = h.write_lines(tmp_path / "spans.jsonl", [h.spans([h.span(
        "chat", "0000000000000001", h.T0, h.T0 + 1_000,
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic",
         "gen_ai.request.model": "claude-opus-5-5", "gen_ai.response.id": "msg_shared",
         "gen_ai.usage.input_tokens": 1_010, "gen_ai.usage.cache_read.input_tokens": 1_000,
         "gen_ai.usage.output_tokens": 50})])])
    response_file = h.write_lines(tmp_path / "responses.jsonl", [{
        "request_meta": {"ts_ms": h.T0, "session": "s"},
        "response": {"id": "msg_shared", "type": "message", "model": "claude-opus-5-5",
                     "usage": {"input_tokens": 10, "cache_read_input_tokens": 1_000,
                               "output_tokens": 50}}}])
    store = MemoryStore(org_key=h.ORG_KEY, name_key_id=key_id(h.NAME_KEY), pricer=FakePricer())
    spans_result = OtlpJsonAdapter().read(span_file, h.central())
    responses_result = AnthropicResponsesAdapter().read(response_file, h.central())
    assert spans_result.requests[0].request_id == responses_result.requests[0].request_id
    store.ingest(spans_result)
    store.ingest(responses_result)
    (req,) = list(store.iter_requests())
    assert req.source is not None and req.source.adapter == "anthropic-responses"  # priority 35


def test_checked_in_fixtures_match_their_builder(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("tb_telem_fixture_builder",
                                                  h.FIXTURES / "build_fixtures.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(tmp_path)
    built = sorted(p.name for p in tmp_path.iterdir())
    assert built == sorted(p.name for p in h.FIXTURES.iterdir() if p.suffix in (".jsonl", ".json"))
    for name in built:
        assert (tmp_path / name).read_bytes() == (h.FIXTURES / name).read_bytes(), name
