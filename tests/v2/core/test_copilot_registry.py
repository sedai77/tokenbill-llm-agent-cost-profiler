"""GitHub Copilot registry and protocol contracts (CORE-AMENDMENTS C-24 … C-27): registry entries,
sniffing with data-quality notes, detector phases / extension gating / product-family filtering,
``ExtensionSpec`` / ``EXTENSIONS`` and the extension protocols."""

from __future__ import annotations

import sys
import types
from collections.abc import Sequence
from pathlib import Path

import pytest

from tokenbill.core import registry as reg
from tokenbill.core.builders import FlatRates, make_lane, make_request, make_usage
from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import Basis, estimated
from tokenbill.core.protocols import (
    ChannelReconciler,
    ExtRecordStore,
    LedgerStats,
    LedgerStore,
    SectionRenderer,
)
from tokenbill.core.records import Lane
from tokenbill.core.types import AnalysisContext, DataQualityNote, Finding, Scope

# ---------- C-24: registry entries ----------


def test_copilot_registry_entries() -> None:
    adapters = list(reg.BUILTIN_ADAPTERS)
    assert adapters.index("copilot-cli") == adapters.index("otlp") - 2
    assert adapters.index("copilot-otel") == adapters.index("otlp") - 1
    assert adapters[-12:] == [
        "copilot-vscode-traces", "gh-aw-token-usage", "github-ai-usage", "github-metered-usage",
        "github-billing-api", "github-copilot-config", "github-copilot-metrics",
        "github-copilot-seats", "github-agent-tasks", "github-usage-records",
        "github-copilot-activity-report", "copilot-export",
    ]
    expected = {
        "copilot-cli": "tokenbill.adapters.copilot_cli:CopilotCliAdapter",
        "copilot-otel": "tokenbill.adapters.copilot_otel:CopilotOtelAdapter",
        "copilot-vscode-traces": "tokenbill.adapters.copilot_vscode:VsCodeAgentTracesAdapter",
        "gh-aw-token-usage": "tokenbill.adapters.gh_aw:GhAwTokenUsageAdapter",
        "github-ai-usage": "tokenbill.adapters.github_billing:AiUsageReportAdapter",
        "github-metered-usage": "tokenbill.adapters.github_billing:MeteredUsageAdapter",
        "github-billing-api": "tokenbill.adapters.github_billing:BillingApiAdapter",
        "github-copilot-config": "tokenbill.adapters.github_config:CopilotConfigAdapter",
        "github-copilot-metrics": "tokenbill.adapters.github_metrics:CopilotMetricsAdapter",
        "github-copilot-seats": "tokenbill.adapters.github_seats:CopilotSeatsAdapter",
        "github-agent-tasks": "tokenbill.adapters.github_agent_tasks:AgentTasksAdapter",
        "github-usage-records": "tokenbill.adapters.github_usage_records:UsageRecordsRefusal",
        "github-copilot-activity-report":
            "tokenbill.adapters.github_activity_report:ActivityReportAdapter",
        "copilot-export": "tokenbill.adapters.copilot_export:CopilotExportAdapter",
    }
    for name, dotted in expected.items():
        assert reg.BUILTIN_ADAPTERS[name] == dotted
    assert "copilot-admin-answers" not in reg.BUILTIN_ADAPTERS  # read via --answers (CA-44)
    assert list(reg.BUILTIN_DETECTORS)[-3:] == ["copilot.seats-budgets", "copilot.org-scan",
                                                "copilot.lanes"]
    assert reg.BUILTIN_DETECTORS["copilot.lanes"] == "tokenbill.detect.copilot_lanes:CopilotLanes"
    assert "tokenbill.core.spans" not in reg.CONVENTION_MODULES  # withdrawn (CA-42)
    assert reg.DQ_ADAPTER_UNAVAILABLE == "dq.adapter_unavailable"


class JsonAdapter:
    name = "test-json"
    capabilities = frozenset({"timing"})

    def sniff(self, path: Path, head: bytes) -> bool:
        return head.startswith(b'{"hello"')

    def read(self, path: Path, opts: object) -> object:  # pragma: no cover - not exercised
        raise NotImplementedError


def test_sniff_adapter_notes_missing_modules(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "BUILTIN_ADAPTERS", {
        "copilot-export": "tokenbill.adapters.copilot_export_missing:CopilotExportAdapter",
        "gone": "tokenbill.adapters.does_not_exist:Nope",
        "bad-attr": "tokenbill.core.builders:NoSuchAdapter",
    })
    monkeypatch.setattr(reg, "_PLUGIN_ADAPTERS", {"z-json": JsonAdapter})
    src = tmp_path / "x.jsonl"
    src.write_bytes(b'{"hello": 1}\n')
    notes: list[DataQualityNote] = []
    adapter = reg.sniff_adapter(src, notes=notes)
    assert isinstance(adapter, JsonAdapter)
    assert [n.code for n in notes] == ["dq.adapter_unavailable"] * 3
    assert all(n.count == 1 and n.severity == "warn" for n in notes)
    assert "copilot-export" in notes[0].detail and "gone" in notes[1].detail
    assert reg.sniff_adapter(src) is not None  # notes optional; nothing raised
    with pytest.raises(ModuleNotFoundError):  # an explicit request still raises
        reg.get_adapter("copilot-export")


def test_sniff_with_every_copilot_adapter_missing(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate F' clause: with every Copilot adapter module absent, sniffing raises nothing and
    records one ``dq.adapter_unavailable`` note per Copilot adapter."""
    copilot = [n for n in reg.BUILTIN_ADAPTERS
               if "copilot" in n or n.startswith(("github-", "gh-"))]
    assert len(copilot) == 14
    absent = {n: f"tokenbill.adapters._absent_{n.replace('-', '_')}:Absent" for n in copilot}
    monkeypatch.setattr(reg, "BUILTIN_ADAPTERS", absent)
    monkeypatch.setattr(reg, "_PLUGIN_ADAPTERS", {})
    src = tmp_path / "report.csv"
    src.write_bytes(b"date,username,product,sku,model,quantity,unit_type\n")
    notes: list[DataQualityNote] = []
    assert reg.sniff_adapter(src, notes=notes) is None
    assert len(notes) == 14 and {n.code for n in notes} == {"dq.adapter_unavailable"}
    assert [n.detail.split()[1] for n in notes] == copilot


# ---------- C-25: detectors ----------


def _ctx(capabilities: Sequence[str] = ("usage_sequence",)) -> AnalysisContext:
    return AnalysisContext(pricer=FlatRates(), rules=None, replayer=None,  # type: ignore[arg-type]
                           calibration=None, window=(1000, 2000),
                           capabilities=frozenset(capabilities))


def _finding(detector_id: str, fid: str, kind: str = "k",
             dims: tuple[tuple[str, str], ...] = (), nano: int = 1) -> Finding:
    return Finding(
        finding_id=fid, detector_id=detector_id, kind=kind, detector_version="1",
        category="breaker", lever_class="none", audience="org", title="t", summary="s",
        scope=Scope(dims=dims), n_events=1, n_lanes=1, n_users=1, first_seen_ms=0,
        cost_observed=estimated(1, Basis.LIST, note="x"),
        recoverable=estimated(nano, Basis.LIST, note="x"), references=("r",))


SEEN: dict[str, list[str]] = {}


class LaneDetector:
    id = "t.lane"
    version = "1"
    kinds = ("k", "dup")
    requires = frozenset({"usage_sequence"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        SEEN.setdefault(self.id, []).extend(lane.lane_key for lane in lanes)
        out = [_finding(self.id, f"fd_{lane.lane_key}", nano=10) for lane in lanes]
        out.append(_finding(self.id, "fd_dup_copilot", kind="dup", dims=(("product", "copilot"),)))
        out.append(_finding(self.id, "fd_dup_default", kind="dup"))
        return out


class AggregateDetector:
    id = "t.aggregate"
    version = "1"
    kinds = ("agg",)
    requires = frozenset({"aggregates"})
    aggregate = True
    extension = "copilot"

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        SEEN.setdefault(self.id, []).append(str(len(lanes)))
        return [_finding(self.id, "fd_agg", kind="agg", dims=(("product", "copilot"),), nano=5)]


class CopilotOnlyDetector:
    id = "t.copilot-lanes"
    version = "1"
    kinds = ("c",)
    requires = frozenset()
    families = frozenset({"copilot"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        SEEN.setdefault(self.id, []).extend(lane.lane_key for lane in lanes)
        return []


class NeedsBlocks:
    id = "t.blocks"
    version = "1"
    kinds = ("b",)
    requires = frozenset({"blocks"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:  # noqa: ARG002
        raise AssertionError("must not run")


class ExtensionMissingCaps:
    id = "t.ext-missing"
    version = "1"
    kinds = ("x",)
    requires = frozenset({"licenses"})
    extension = "copilot"
    aggregate = True

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:  # noqa: ARG002
        raise AssertionError("must not run without licenses")


@pytest.fixture
def toy_detectors(monkeypatch: pytest.MonkeyPatch) -> None:
    SEEN.clear()
    monkeypatch.setattr(reg, "BUILTIN_DETECTORS", {})
    monkeypatch.setattr(reg, "_PLUGIN_DETECTORS", {
        "t.lane": LaneDetector, "t.aggregate": AggregateDetector,
        "t.copilot-lanes": CopilotOnlyDetector, "t.blocks": NeedsBlocks,
        "t.ext-missing": ExtensionMissingCaps,
    })


def _lanes() -> list[Lane]:
    claude = make_lane([make_request("C1", 0, 0, make_usage(output=1))], lane_key="C1")
    copilot = make_lane([make_request("G1", 0, 0, make_usage(output=1), provider="github",
                                      channel="github_copilot", billing_path="copilot_pool")],
                        lane_key="G1")
    return [claude, copilot]


def _family(lane: Lane) -> str:
    return "copilot" if lane.billing_class == "pool" else "default"


def test_phases_and_extension_gating(toy_detectors: None) -> None:
    lanes = _lanes()
    no_ext = reg.run_detectors(lanes, _ctx(("usage_sequence", "aggregates")))
    ids = [f.detector_id for f in no_ext]
    assert "t.aggregate" not in ids and "t.ext-missing" not in ids  # silent without ext:copilot
    assert [f.kind for f in no_ext if f.category == "data-quality"] == ["missing-capabilities"]
    caps = ("usage_sequence", "aggregates", "ext:copilot")
    everything = reg.run_detectors(lanes, _ctx(caps))
    assert {f.detector_id for f in everything} == {"t.lane", "t.aggregate", "t.blocks",
                                                   "t.ext-missing"}
    per_shard = reg.run_detectors(lanes, _ctx(caps), aggregates_only=False)
    assert {f.detector_id for f in per_shard} == {"t.lane"}  # no missing-capabilities findings
    once = reg.run_detectors([], _ctx(caps), aggregates_only=True)
    assert [f.detector_id for f in once if f.category != "data-quality"] == ["t.aggregate"]
    missing = sorted(f.detector_id for f in once if f.category == "data-quality")
    assert missing == ["t.blocks", "t.ext-missing"]  # the pass covers lane detectors too
    quiet = reg.run_detectors([], _ctx(caps), aggregates_only=True, emit_missing=False)
    assert all(f.category != "data-quality" for f in quiet)
    only = reg.run_detectors(lanes, _ctx(caps), only=["t.aggregate"], aggregates_only=False)
    assert only == []
    # output order unchanged: (−recoverable point, detector_id, finding_id)
    keyed = [(-(f.recoverable.nano if f.recoverable and f.recoverable.nano else 0),
              f.detector_id, f.finding_id) for f in everything]
    assert keyed == sorted(keyed)


def test_family_filter_and_exclusions(toy_detectors: None,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.core import catalog, findings

    lanes = _lanes()
    # F-SEM-C / F-KIT-C not merged yet: every lane is family "default", nothing is excluded
    monkeypatch.delattr(findings, "product_family", raising=False)
    monkeypatch.delattr(catalog, "FAMILY_EXCLUSIONS", raising=False)
    out = reg.run_detectors(lanes, _ctx())
    assert SEEN["t.copilot-lanes"] == [] and sorted(SEEN["t.lane"]) == ["C1", "G1"]
    assert {f.finding_id for f in out} >= {"fd_dup_copilot", "fd_dup_default"}
    SEEN.clear()
    monkeypatch.setattr(findings, "product_family", _family, raising=False)
    monkeypatch.setattr(catalog, "FAMILY_EXCLUSIONS", {
        ("t.lane", None): frozenset({"copilot"}),       # the detector never sees Copilot lanes
        ("t.lane", "dup"): frozenset({"copilot"}),      # and its dup kind is dropped for Copilot
    }, raising=False)
    out = reg.run_detectors(lanes, _ctx())
    assert SEEN["t.lane"] == ["C1"] and SEEN["t.copilot-lanes"] == ["G1"]
    fids = {f.finding_id for f in out}
    assert "fd_dup_copilot" not in fids and {"fd_C1", "fd_dup_default"} <= fids
    SEEN.clear()
    monkeypatch.setattr(catalog, "FAMILY_EXCLUSIONS", {("t.lane", "dup"): frozenset({"copilot"})},
                        raising=False)
    out = reg.run_detectors(lanes, _ctx())
    assert sorted(SEEN["t.lane"]) == ["C1", "G1"]  # kind-level only: lanes unfiltered
    assert "fd_dup_copilot" not in {f.finding_id for f in out}


def test_family_tables_tolerate_missing_modules(toy_detectors: None,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tokenbill.core.findings", None)  # import raises ImportError
    monkeypatch.setitem(sys.modules, "tokenbill.core.catalog", None)
    import tokenbill.core as core_pkg

    monkeypatch.delattr(core_pkg, "findings", raising=False)
    monkeypatch.delattr(core_pkg, "catalog", raising=False)
    assert reg._product_family_fn() is None and reg._family_exclusions() == {}
    out = reg.run_detectors(_lanes(), _ctx())
    assert SEEN["t.copilot-lanes"] == [] and "fd_dup_copilot" in {f.finding_id for f in out}


def test_existing_detectors_see_the_same_lanes(toy_detectors: None) -> None:
    lanes = _lanes()
    reg.run_detectors(lanes, _ctx())
    assert SEEN["t.lane"] == ["C1", "G1"]  # no families attribute → today's lanes, in order


# ---------- C-26 ----------


def test_extension_spec() -> None:
    spec = reg.EXTENSIONS["copilot"]
    assert spec.name == "copilot" and spec.channels == ("github_copilot", "github_actions",
                                                        "github_sandbox")
    assert spec.rate_files == ("tokenbill.copilot.data:github_copilot.json",)
    assert spec.context_enricher == "tokenbill.copilot.enrich:enrich_context"
    assert spec.policy_targets == (("github-copilot", "tokenbill.pipeline.copilot:policy_packs"),)
    aliases = {(a.verb, a.trigger): a for a in spec.argv_aliases}
    assert aliases[("collect", "copilot-cli")].target == ("copilot", "collect", "--source", "cli")
    assert aliases[("collect", "copilot-vscode")].target == ("copilot", "collect", "--source",
                                                             "vscode")
    assert aliases[("scan", "--copilot")].position == "any"
    assert aliases[("collect", "copilot-cli")].position == "first"
    for hook in (spec.reconciler, spec.record_store, spec.summary_builder, spec.section_renderer,
                 spec.focus_rows, spec.showback, spec.panel_builder, spec.rate_verifier):
        module, _, attr = (hook or "").partition(":")
        assert module.startswith("tokenbill.") and attr
    with pytest.raises(ContractViolation):
        reg.ArgvAlias("scan", "--x", "middle", ("x",))
    with pytest.raises(ContractViolation):
        reg.ArgvAlias("scan", "--x", "any", ())
    with pytest.raises(ContractViolation):
        reg.ArgvAlias("scan", "--x", "any", ("",))
    for verb, trigger in (("", "--x"), ("scan", ""), (None, "--x")):
        with pytest.raises(ContractViolation):
            reg.ArgvAlias(verb, trigger, "any", ("x",))  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        reg.ArgvAlias("scan", "--x", "any", "copilot")  # type: ignore[arg-type]
    listed = reg.ArgvAlias("scan", "--x", "any", ["copilot", "scan"])  # type: ignore[arg-type]
    assert listed.target == ("copilot", "scan") and hash(listed) == hash(
        reg.ArgvAlias("scan", "--x", "any", ("copilot", "scan")))
    assert hash(spec)  # every spec value is immutable


def test_registry_import_loads_no_wave2_module() -> None:
    code = ("import sys, tokenbill.core.registry as r; "
            "bad = [m for m in sys.modules if m.startswith(('tokenbill.copilot.', "
            "'tokenbill.adapters.', 'tokenbill.detect.'))]; print(bad); assert not bad")
    import subprocess

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


# ---------- C-27 protocols ----------


def test_ledger_store_unchanged_and_ledger_stats() -> None:
    from tokenbill.core.testing import MemoryStore

    store = MemoryStore()
    assert isinstance(store, LedgerStore)  # no new LedgerStore member
    assert hasattr(LedgerStats, "_is_runtime_protocol")

    class Stats:
        def source_stats(self, *, adapter: str | None = None) -> dict[str, int]:
            return {"rounding_remainder_e18": 3}

    assert isinstance(Stats(), LedgerStats) and not isinstance(object(), LedgerStats)
    import inspect

    sig = inspect.signature(LedgerStore.count_users)
    assert sig.parameters["source"].default == "requests"


def test_extension_protocols() -> None:
    class Records:
        name = "copilot"

        def put(self, result: object, *, principal_key_id: str | None) -> dict[str, int]:
            return {}

        def licenses(self, **window: int) -> list:
            return []

        def activity(self, **window: int) -> list:
            return []

        def config(self, **window: int) -> list:
            return []

        def count_users(self, *, since_ms: int, until_ms: int, where: object,
                        source: str) -> int:
            return 0

        def retain(self, *, identity_before_ms: int) -> int:
            return 0

        def purge(self, *, principal: str | None, before_ms: int | None, actor: str) -> int:
            return 0

    class Renderer:
        name = "copilot"

        def terminal(self, result: object, *, width: int) -> str:
            return ""

        def html(self, result: object) -> str:
            return "<section></section>"

        def json(self, result: object) -> dict | None:
            return None

    def reconcile(ledger, record_stores, pricer, **kw):  # noqa: ANN001, ANN003, ANN202
        raise NotImplementedError

    assert isinstance(Records(), ExtRecordStore) and not isinstance(Renderer(), ExtRecordStore)
    assert isinstance(Renderer(), SectionRenderer) and not isinstance(Records(), SectionRenderer)
    assert isinstance(reconcile, ChannelReconciler)
    assert not isinstance(types.SimpleNamespace(), ChannelReconciler)
