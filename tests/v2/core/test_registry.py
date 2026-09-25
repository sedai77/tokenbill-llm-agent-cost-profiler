"""SPEC §3.7 registry: exact string maps, lazy loading, sniffing, detectors, plugins."""

from __future__ import annotations

import gzip
from collections.abc import Sequence
from pathlib import Path

import pytest

from tokenbill.core import registry as reg
from tokenbill.core.builders import FlatRates, make_lane, make_request, make_usage
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.labels import Basis, estimated
from tokenbill.core.records import Lane
from tokenbill.core.types import AnalysisContext, DataQualityNote, Finding, Scope


def test_string_maps_match_the_spec() -> None:
    assert list(reg.BUILTIN_ADAPTERS) == [
        "claude-code",
        "claude-code-headless",
        "trace@1",
        "trace@2",
        "copilot-cli",
        "copilot-otel",
        "otlp",
        "openai",
        "bedrock",
        "anthropic-responses",
        "anthropic-usage-report",
        "anthropic-cost-report",
        "anthropic-cc-analytics",
        "anthropic-enterprise-analytics",
        "openai-usage-buckets",
        "openai-costs",
        "aws-cur",
        "gcp-billing",
        "copilot-vscode-traces",
        "gh-aw-token-usage",
        "github-ai-usage",
        "github-metered-usage",
        "github-billing-api",
        "github-copilot-config",
        "github-copilot-metrics",
        "github-copilot-seats",
        "github-agent-tasks",
        "github-usage-records",
        "github-copilot-activity-report",
        "copilot-export",
    ]
    assert reg.BUILTIN_ADAPTERS["claude-code"] == "tokenbill.adapters.claude_code:ClaudeCodeAdapter"
    assert reg.BUILTIN_ADAPTERS["trace@2"] == "tokenbill.adapters.trace_v2:TraceV2Adapter"
    assert (
        reg.BUILTIN_ADAPTERS["gcp-billing"]
        == "tokenbill.adapters.cloud_billing:GcpBillingExportAdapter"
    )
    assert len(reg.BUILTIN_DETECTORS) == 23
    assert reg.BUILTIN_DETECTORS["aggregate.org-scan"] == "tokenbill.recon.orgscan:OrgScan"
    assert reg.BUILTIN_DETECTORS["block.breakers"] == "tokenbill.detect.block:BlockBreakers"
    assert reg.BUILTIN_DETECTORS["cache.miss-by-cause"] == "tokenbill.detect.cache_miss:MissByCause"
    assert reg.CONVENTION_MODULES == (
        "tokenbill.adapters.conventions_ext",
        "tokenbill.adapters.copilot_conventions",
        "tokenbill.adapters.copilot_otel",
        "tokenbill.adapters.github_billing",
        "tokenbill.adapters.gh_aw",
    )
    for dotted in (*reg.BUILTIN_ADAPTERS.values(), *reg.BUILTIN_DETECTORS.values()):
        module, _, cls = dotted.partition(":")
        assert module.startswith("tokenbill.") and cls[:1].isupper()


def test_load_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    assert reg.load("collections:OrderedDict").__name__ == "OrderedDict"
    assert reg.load("tokenbill.core.builders:FlatRates") is FlatRates
    with pytest.raises(ModuleNotFoundError):
        reg.load("tokenbill.adapters.does_not_exist:Nope")
    with pytest.raises(ImportError):
        reg.load("tokenbill.core.builders:Nope")
    with pytest.raises(UsageError):
        reg.load("no-colon")
    monkeypatch.setitem(reg.BUILTIN_ADAPTERS, "x-missing", "tokenbill.adapters.does_not_exist:Nope")
    with pytest.raises(ModuleNotFoundError):  # raised only when this entry is requested
        reg.get_adapter("x-missing")
    with pytest.raises(UsageError):
        reg.get_adapter("no-such-adapter")


class JsonAdapter:
    name = "test-json"
    capabilities = frozenset({"timing"})

    def sniff(self, path: Path, head: bytes) -> bool:
        return head.startswith(b'{"hello"')

    def read(self, path: Path, opts: object) -> object:  # pragma: no cover - not exercised
        raise NotImplementedError


class ExplodingAdapter(JsonAdapter):
    name = "boom"

    def sniff(self, path: Path, head: bytes) -> bool:
        raise ValueError("hostile input")


@pytest.fixture
def only_test_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = {name: "tokenbill.adapters.does_not_exist:Nope" for name in reg.BUILTIN_ADAPTERS}
    monkeypatch.setattr(reg, "BUILTIN_ADAPTERS", missing)
    monkeypatch.setattr(
        reg, "_PLUGIN_ADAPTERS", {"a-boom": ExplodingAdapter, "b-json": JsonAdapter}
    )


def test_sniff_adapter(tmp_path: Path, only_test_adapters: None) -> None:
    plain = tmp_path / "x.jsonl"
    plain.write_bytes(b'{"hello": 1}\n')
    adapter = reg.sniff_adapter(plain)
    assert isinstance(adapter, JsonAdapter) and adapter.name == "test-json"
    gz = tmp_path / "x.jsonl.gz"
    gz.write_bytes(gzip.compress(b'{"hello": 2}\n' * 10000))
    assert isinstance(reg.sniff_adapter(gz), JsonAdapter)
    bad_gz = tmp_path / "bad.gz"
    bad_gz.write_bytes(b'{"hello": "not gzip"}')
    assert isinstance(reg.sniff_adapter(bad_gz), JsonAdapter)  # falls back to the raw head
    zst = tmp_path / "x.zst"
    zst.write_bytes(b"\x28\xb5\x2f\xfd garbage")
    assert reg.sniff_adapter(zst) is None
    other = tmp_path / "other.txt"
    other.write_bytes(b"nope")
    assert reg.sniff_adapter(other) is None
    with pytest.raises(SourceError):
        reg.sniff_adapter(tmp_path / "missing.jsonl")


def test_sniff_decompresses_zst_through_compression_zstd(
    tmp_path: Path, only_test_adapters: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    package, module = types.ModuleType("compression"), types.ModuleType("compression.zstd")
    module.open = lambda path, mode: gzip.open(path, mode)  # type: ignore[attr-defined]
    package.zstd = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "compression", package)
    monkeypatch.setitem(sys.modules, "compression.zstd", module)
    zst = tmp_path / "x.jsonl.zst"
    zst.write_bytes(gzip.compress(b'{"hello": 3}\n'))
    assert isinstance(reg.sniff_adapter(zst), JsonAdapter)  # head decompressed
    corrupt = tmp_path / "raw.zst"
    corrupt.write_bytes(b'{"hello": "not compressed"}')
    assert isinstance(reg.sniff_adapter(corrupt), JsonAdapter)  # falls back to the raw head


def test_sniff_reads_at_most_64_kib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    class Recorder(JsonAdapter):
        def sniff(self, path: Path, head: bytes) -> bool:
            seen.append(len(head))
            return False

    monkeypatch.setattr(reg, "BUILTIN_ADAPTERS", {})
    monkeypatch.setattr(reg, "_PLUGIN_ADAPTERS", {"rec": Recorder})
    big = tmp_path / "big.jsonl"
    big.write_bytes(b"x" * (200 * 1024))
    assert reg.sniff_adapter(big) is None
    assert seen == [64 * 1024]


# ---------- detectors ----------


def _ctx(capabilities: frozenset[str] = frozenset({"usage_sequence"})) -> AnalysisContext:
    return AnalysisContext(
        pricer=FlatRates(),
        rules=None,
        replayer=None,
        calibration=None,  # type: ignore[arg-type]
        window=(1000, 2000),
        capabilities=capabilities,
    )


def _finding(detector_id: str, fid: str, nano: int | None) -> Finding:
    rec = estimated(nano, Basis.LIST, note="x") if nano is not None else None
    return Finding(
        finding_id=fid,
        detector_id=detector_id,
        kind="k",
        detector_version="1",
        category="breaker",
        lever_class="none",
        audience="org",
        title="t",
        summary="s",
        scope=Scope(dims=()),
        n_events=1,
        n_lanes=1,
        n_users=1,
        first_seen_ms=0,
        cost_observed=estimated(1, Basis.LIST, note="x"),
        recoverable=rec,
        references=("r",),
    )


class SeqDetector:
    id = "test.seq"
    version = "1"
    kinds = ("k",)
    requires = frozenset({"usage_sequence"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        return [
            _finding(self.id, f"fd_{lane.lane_key}", 100 * (i + 1)) for i, lane in enumerate(lanes)
        ]


class BlockDetector:
    id = "test.blocks"
    version = "2"
    kinds = ("k",)
    requires = frozenset({"blocks", "fingerprint_extra"})

    def detect(
        self, lanes: Sequence[Lane], ctx: AnalysisContext
    ) -> list[Finding]:  # pragma: no cover
        raise AssertionError("must not run without its capabilities")


class NoDollarDetector(SeqDetector):
    id = "test.nodollar"

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        return [_finding(self.id, "fd_z", None)]


@pytest.fixture
def test_detectors(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = {k: "tokenbill.detect.does_not_exist:Nope" for k in reg.BUILTIN_DETECTORS}
    monkeypatch.setattr(reg, "BUILTIN_DETECTORS", missing)
    monkeypatch.setattr(
        reg,
        "_PLUGIN_DETECTORS",
        {"test.blocks": BlockDetector, "test.seq": SeqDetector, "test.nodollar": NoDollarDetector},
    )


def test_all_detectors_skips_unimportable_with_notes(test_detectors: None) -> None:
    notes: list[DataQualityNote] = []
    dets = reg.all_detectors(notes=notes)
    assert [d.id for d in dets] == ["test.blocks", "test.nodollar", "test.seq"]
    assert len(notes) == len(reg.BUILTIN_DETECTORS)
    assert {n.code for n in notes} == {"dq.detector_unavailable"}
    assert reg.all_detectors() and all(n.count == 1 for n in notes)


def test_run_detectors_missing_capabilities(test_detectors: None) -> None:
    lanes = [
        make_lane([make_request("L1", 0, 0, make_usage(output=1))], lane_key="L1"),
        make_lane([make_request("L2", 0, 0, make_usage(output=1))], lane_key="L2"),
    ]
    out = reg.run_detectors(lanes, _ctx())
    dq = [f for f in out if f.category == "data-quality"]
    assert len(dq) == 1
    f = dq[0]
    assert (f.detector_id, f.kind, f.recoverable) == ("test.blocks", "missing-capabilities", None)
    assert f.cost_observed.nano is None and f.cost_observed.note == "unpriced: capabilities missing"
    assert "blocks" in f.summary and "fingerprint_extra" in f.summary and f.references
    assert f.first_seen_ms == 1000 and f.detector_version == "2"
    # sorted by (-recoverable point or 0, detector_id, finding_id)
    assert [x.finding_id for x in out if x.category != "data-quality"] == ["fd_L2", "fd_L1", "fd_z"]
    assert out[-1].detector_id == "test.seq" or out.index(f) > 1
    none = reg.run_detectors(lanes, _ctx(), emit_missing=False)
    assert all(x.category != "data-quality" for x in none) and len(none) == 3
    # pipeline pattern: shards with emit_missing=False, then once with no lanes
    once = reg.run_detectors([], _ctx(), emit_missing=True)
    assert [x.kind for x in once if x.category == "data-quality"] == ["missing-capabilities"]


def test_run_detectors_only(test_detectors: None) -> None:
    lane = make_lane([make_request("L1", 0, 0, make_usage(output=1))], lane_key="L1")
    out = reg.run_detectors([lane], _ctx(), only=["test.seq", "test.seq"])
    assert [f.detector_id for f in out] == ["test.seq"]
    with pytest.raises(UsageError):
        reg.run_detectors([lane], _ctx(), only=["test.unknown"])
    everything = _ctx(frozenset({"usage_sequence", "blocks", "fingerprint_extra"}))
    with pytest.raises(AssertionError):
        reg.run_detectors([lane], everything, only=["test.blocks"])


def test_run_detectors_explicit_missing_module_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(reg.BUILTIN_DETECTORS, "x.missing", "tokenbill.detect.does_not_exist:Nope")
    with pytest.raises(ModuleNotFoundError):
        reg.run_detectors([], _ctx(), only=["x.missing"])


# ---------- plugins ----------


class _EP:
    def __init__(self, name: str, obj: object, fail: bool = False) -> None:
        self.name, self._obj, self._fail = name, obj, fail

    def load(self) -> object:
        if self._fail:
            raise ImportError("broken plugin")
        return self._obj


def test_plugins_not_loaded_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_entry_points(group: str) -> list[_EP]:
        calls.append(group)
        if group == "tokenbill.adapters":
            return [
                _EP("zz-json", JsonAdapter),
                _EP("claude-code", JsonAdapter),
                _EP("broken", None, True),
                _EP("not-a-class", 42),
            ]
        return [_EP("test.seq", SeqDetector)]

    monkeypatch.setattr(reg, "_entry_points", fake_entry_points)
    monkeypatch.setattr(reg, "_PLUGIN_ADAPTERS", {})
    monkeypatch.setattr(reg, "_PLUGIN_DETECTORS", {})
    assert reg.load_plugins(False) == []
    assert calls == [] and reg._PLUGIN_ADAPTERS == {}
    loaded = reg.load_plugins(True)
    assert loaded == ["zz-json", "test.seq"]
    assert reg._PLUGIN_ADAPTERS == {"zz-json": JsonAdapter}  # built-in names cannot be shadowed
    assert isinstance(reg.get_adapter("zz-json"), JsonAdapter)
    assert "test.seq" in reg._PLUGIN_DETECTORS


def test_real_entry_points_query_is_harmless() -> None:
    assert list(reg._entry_points("tokenbill.no-such-group")) == []
