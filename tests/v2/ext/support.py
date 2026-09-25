"""Area-local helpers of the F-EXT tests: the ``fake`` / ``ghost`` extension specs and small
builders for contexts, run results, ingest results and ledgers (all from core types)."""

from __future__ import annotations

from typing import Any

from tokenbill.core import registry
from tokenbill.core.builders import FlatRates, make_request
from tokenbill.core.records import ActivityDay, ConfigSnapshot, CostLine, LicenseSnapshot, Request
from tokenbill.core.registry import ArgvAlias, ExtensionSpec
from tokenbill.core.testing import MemoryStore
from tokenbill.core.types import (
    AnalysisContext,
    IngestResult,
    PrivacyInfo,
    RunResult,
    SourceInfo,
    UsageAggregate,
)

from .fake_ext import hooks

#: Importable name of the fake extension package (``v2.ext.fake_ext`` under pytest's rootdir).
PKG = __name__.rsplit(".", 1)[0] + ".fake_ext"
HOOKS = f"{PKG}.hooks"
#: A module that does not exist, inside a package that does.
ABSENT = f"{PKG}.absent_module"
#: The shipped Copilot extension spec (captured before any test patches the registry).
COPILOT = registry.EXTENSIONS["copilot"]

#: Every hook name a fully declared extension can report as unavailable.
ALL_HOOKS = ("rate_files", "rate_verifier", "reconciler", "record_store", "context_enricher",
             "summary_builder", "section_renderer", "focus_rows", "showback", "policy_targets",
             "panel_builder", "command_module")

DAY_MS = 86_400_000
SEP_10_MS = 20_706 * DAY_MS   # 2026-09-10T00:00Z
WINDOW = {"since_ms": 20_697 * DAY_MS, "until_ms": 20_727 * DAY_MS}   # [2026-09-01, 2026-10-01)


def fake_spec(**overrides: Any) -> ExtensionSpec:
    """The test-only extension ``fake``: every hook in :mod:`.fake_ext.hooks`."""
    fields: dict[str, Any] = {
        "name": "fake",
        "channels": hooks.FAKE_CHANNELS,
        "rate_files": (f"{PKG}:fake_rates.json",),
        "rate_verifier": f"{HOOKS}:verify",
        "reconciler": f"{HOOKS}:reconcile",
        "record_store": f"{HOOKS}:FakeRecordStore",
        "context_enricher": f"{HOOKS}:enrich",
        "summary_builder": f"{HOOKS}:summarize",
        "section_renderer": f"{HOOKS}:FakeSection",
        "focus_rows": f"{HOOKS}:focus_rows",
        "showback": f"{HOOKS}:showback",
        "policy_targets": (("fake-target", f"{HOOKS}:policy_packs"),),
        "panel_builder": f"{HOOKS}:panel",
        "command_module": f"{PKG}.commands",
        "argv_aliases": (ArgvAlias("scan", "--fake", "any", ("fake", "scan")),),
    }
    fields.update(overrides)
    return ExtensionSpec(**fields)


def bare_spec(name: str, **overrides: Any) -> ExtensionSpec:
    """An extension declaring no hook at all (every optional field None / empty)."""
    fields: dict[str, Any] = {
        "name": name, "channels": (), "rate_files": (), "rate_verifier": None, "reconciler": None,
        "record_store": None, "context_enricher": None, "summary_builder": None,
        "section_renderer": None, "focus_rows": None, "showback": None, "policy_targets": (),
        "panel_builder": None, "command_module": None,
    }
    fields.update(overrides)
    return ExtensionSpec(**fields)


def ghost_spec(name: str = "ghost", module: str = ABSENT,
               rate_package: str | None = None) -> ExtensionSpec:
    """An extension whose every hook is an attribute ``ghost_*`` of *module* (absent by default,
    so every hook is unavailable); its rate file is ``rates.json`` of *rate_package* (default
    *module*)."""
    return ExtensionSpec(
        name=name, channels=(f"{name}_ch",),
        rate_files=(f"{rate_package or module}:ghost_rates.json",),
        rate_verifier=f"{module}:ghost_verify", reconciler=f"{module}:ghost_reconcile",
        record_store=f"{module}:GhostStore", context_enricher=f"{module}:ghost_enrich",
        summary_builder=f"{module}:ghost_summarize", section_renderer=f"{module}:GhostSection",
        focus_rows=f"{module}:ghost_focus_rows", showback=f"{module}:ghost_showback",
        policy_targets=((f"{name}-target", f"{module}:ghost_policy_packs"),),
        panel_builder=f"{module}:ghost_panel", command_module=module)


def make_ctx(**kw: Any) -> AnalysisContext:
    """A bare analysis context over September 2026."""
    return AnalysisContext(pricer=FlatRates(), rules=None, replayer=None,  # type: ignore[arg-type]
                           calibration=None, window=(WINDOW["since_ms"], WINDOW["until_ms"]),
                           capabilities=frozenset(kw.pop("capabilities", ("usage_sequence",))),
                           **kw)


def make_run_result(command: str = "scan") -> RunResult:
    """A minimal run result."""
    return RunResult(command=command, window=(WINDOW["since_ms"], WINDOW["until_ms"]), inputs=(),
                     privacy=PrivacyInfo("none", None, "install", 5, 0),  # type: ignore[arg-type]
                     rate_card=None)


def source(source_id: str = "src_1", adapter: str = "fake-adapter",
           principal_key_id: str | None = "k_org") -> SourceInfo:
    """A source description (no ``h_`` values, principals under *principal_key_id*)."""
    return SourceInfo(source_id=source_id, adapter=adapter, name_hmac="h_" + "0" * 20,
                      sha256=source_id.encode().hex().ljust(64, "0")[:64], bytes=1,
                      name_key_id=None, principal_key_id=principal_key_id)


def ingest_result(*, src: SourceInfo | None = None, requests: tuple[Request, ...] = (),
                  aggregates: tuple[UsageAggregate, ...] = (),
                  cost_lines: tuple[CostLine, ...] = (),
                  licenses: tuple[LicenseSnapshot, ...] = (),
                  activity: tuple[ActivityDay, ...] = (),
                  config: tuple[ConfigSnapshot, ...] = (),
                  stats: dict[str, int] | None = None) -> IngestResult:
    """An ``IngestResult`` holding exactly the given records."""
    return IngestResult(source=src or source(), requests=list(requests), sessions=[], events=[],
                        aggregates=list(aggregates), cost_lines=list(cost_lines), outcomes=[],
                        quarantined=[], notes=[], stats=dict(stats or {}),
                        capabilities=frozenset(), licenses=list(licenses),
                        activity=list(activity), config=list(config))


def fake_request(channel: str, ts_ms: int = SEP_10_MS, lane: str = "ln_fake") -> Request:
    """One request on *channel* (provider ``fake``, no principal)."""
    return make_request(lane, 0, ts_ms, {"uncached_input": 10, "output": 5}, "fake-model",
                        provider="fake", channel=channel)


def memory_store(*results: IngestResult) -> MemoryStore:
    """A ``MemoryStore`` with *results* ingested (no pricer)."""
    store = MemoryStore()
    for result in results:
        store.ingest(result)
    return store


class PlainLedger:
    """A ledger stand-in without ``source_stats`` (so not a ``LedgerStats``)."""

    def __init__(self) -> None:
        self.stats_calls = 0


class StatsLedger:
    """A ledger stand-in implementing ``LedgerStats``: per-adapter integer stats."""

    def __init__(self, by_adapter: dict[str, dict[str, int]]) -> None:
        self.by_adapter = by_adapter
        self.stats_calls: list[str | None] = []

    def source_stats(self, *, adapter: str | None = None) -> dict[str, int]:
        self.stats_calls.append(adapter)
        if adapter is not None:
            return dict(self.by_adapter.get(adapter, {}))
        total: dict[str, int] = {}
        for stats in self.by_adapter.values():
            for key, value in stats.items():
                total[key] = total.get(key, 0) + value
        return total
