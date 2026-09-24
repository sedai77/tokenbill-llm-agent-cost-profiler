"""String-map registration of adapters and detectors (SPEC §3.7).

Registration is by dotted paths fixed here; each owning package implements the class at exactly that
path. Imports are lazy: a module that does not exist yet (its branch is not merged) raises
``ModuleNotFoundError`` only when that entry is requested explicitly; enumeration
(``sniff_adapter``, ``all_detectors``) skips it with a data-quality note (ruling R-E18).
Third-party entry points load only through ``load_plugins(True)``.

GitHub Copilot (CORE-AMENDMENTS C-24 … C-26): the Copilot adapters, detectors and convention modules
are registered here; detectors may declare ``extension`` (run only with ``ext:<name>`` in the
capabilities), ``aggregate`` (run once per run, ``run_detectors(aggregates_only=True)``, ruling
R-E17) and ``families`` (product families whose lanes they see); ``EXTENSIONS`` maps channel
extensions to their hook modules (resolved by ``core.extensions``).
"""

from __future__ import annotations

import gzip
import importlib
import importlib.metadata
import logging
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tokenbill.core.errors import ContractViolation, SourceError, UsageError
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import unpriced
from tokenbill.core.protocols import Adapter, Detector
from tokenbill.core.records import Lane
from tokenbill.core.types import AnalysisContext, DataQualityNote, Finding, Scope

__all__ = [
    "BUILTIN_ADAPTERS",
    "BUILTIN_DETECTORS",
    "CONVENTION_MODULES",
    "DQ_ADAPTER_UNAVAILABLE",
    "DQ_DETECTOR_UNAVAILABLE",
    "EXTENSIONS",
    "SNIFF_HEAD_BYTES",
    "ArgvAlias",
    "ExtensionSpec",
    "all_detectors",
    "get_adapter",
    "load",
    "load_plugins",
    "run_detectors",
    "sniff_adapter",
]

logger = logging.getLogger("tokenbill.core.registry")

BUILTIN_ADAPTERS: dict[str, str] = {  # sniff order = this order
    "claude-code": "tokenbill.adapters.claude_code:ClaudeCodeAdapter",
    "claude-code-headless": "tokenbill.adapters.cc_headless:ClaudeCodeHeadlessAdapter",
    "trace@1": "tokenbill.adapters.trace_v1:TraceV1Adapter",
    "trace@2": "tokenbill.adapters.trace_v2:TraceV2Adapter",
    # Copilot resources are claimed before the generic OTLP reader (disjoint by construction, §5.10)
    "copilot-cli": "tokenbill.adapters.copilot_cli:CopilotCliAdapter",
    "copilot-otel": "tokenbill.adapters.copilot_otel:CopilotOtelAdapter",
    "otlp": "tokenbill.adapters.otel:OtlpJsonAdapter",
    "openai": "tokenbill.adapters.openai:OpenAIUsageAdapter",
    "bedrock": "tokenbill.adapters.bedrock:BedrockAdapter",
    "anthropic-responses": "tokenbill.adapters.anthropic_responses:AnthropicResponsesAdapter",
    "anthropic-usage-report": "tokenbill.adapters.anthropic_admin:UsageReportAdapter",
    "anthropic-cost-report": "tokenbill.adapters.anthropic_admin:CostReportAdapter",
    "anthropic-cc-analytics": "tokenbill.adapters.anthropic_admin:ClaudeCodeAnalyticsAdapter",
    "anthropic-enterprise-analytics":
        "tokenbill.adapters.anthropic_admin:EnterpriseAnalyticsAdapter",
    "openai-usage-buckets": "tokenbill.adapters.openai_admin:OpenAIUsageBucketsAdapter",
    "openai-costs": "tokenbill.adapters.openai_admin:OpenAICostsAdapter",
    "aws-cur": "tokenbill.adapters.cloud_billing:AwsCurAdapter",
    "gcp-billing": "tokenbill.adapters.cloud_billing:GcpBillingExportAdapter",
    # GitHub Copilot (C-24), appended in this order
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
BUILTIN_DETECTORS: dict[str, str] = {  # §10 lists kinds per class
    "cache.miss-by-cause": "tokenbill.detect.cache_miss:MissByCause",
    "cache.switch-churn": "tokenbill.detect.cache_miss:SwitchChurn",
    "cache.rebuild": "tokenbill.detect.cache_miss:RebuildEvents",
    "cache.cold-resume": "tokenbill.detect.cache_ttl:ColdResume",
    "cache.ttl-advisor": "tokenbill.detect.cache_ttl:TtlAdvisor",
    "cache.gateway-disabled": "tokenbill.detect.cache_structure:GatewayDisabled",
    "cache.unread-write": "tokenbill.detect.cache_structure:UnreadWrite",
    "cache.cold-fanout": "tokenbill.detect.cache_structure:ColdFanout",
    "context.size-tax": "tokenbill.detect.context:SizeTax",
    "context.compaction-window": "tokenbill.detect.context:CompactionWindow",
    "context.static-prefix": "tokenbill.detect.context:StaticPrefix",
    "attrib.carry": "tokenbill.detect.context:Carry",
    "premium.modifiers": "tokenbill.detect.premium:PremiumModifiers",
    "premium.sticky-escalation": "tokenbill.detect.premium:StickyEscalation",
    "model.routing": "tokenbill.detect.model:Routing",
    "failure.path": "tokenbill.detect.failure:FailurePath",
    "automation": "tokenbill.detect.automation:Automation",
    "tail.runaway": "tokenbill.detect.tail:Runaway",
    "aggregate.org-scan": "tokenbill.recon.orgscan:OrgScan",
    "block.breakers": "tokenbill.detect.block:BlockBreakers",
    # GitHub Copilot (C-24)
    "copilot.seats-budgets": "tokenbill.detect.copilot_seats:CopilotSeatsBudgets",
    "copilot.org-scan": "tokenbill.detect.copilot_org:CopilotOrgScan",
    "copilot.lanes": "tokenbill.detect.copilot_lanes:CopilotLanes",
}
# importing a convention module registers its conventions; a missing one is skipped (R-E18)
CONVENTION_MODULES: tuple[str, ...] = (
    "tokenbill.adapters.conventions_ext",
    "tokenbill.adapters.copilot_conventions",
    "tokenbill.adapters.copilot_otel",
    "tokenbill.adapters.github_billing",
    "tokenbill.adapters.gh_aw",
)

#: Bytes of (decompressed) head handed to ``Adapter.sniff``.
SNIFF_HEAD_BYTES = 64 * 1024
#: Data-quality code for a registered detector whose module cannot be imported (not merged yet).
DQ_DETECTOR_UNAVAILABLE = "dq.detector_unavailable"
#: Data-quality code for a registered adapter skipped while sniffing (module not importable).
DQ_ADAPTER_UNAVAILABLE = "dq.adapter_unavailable"

# Third-party registrations (entry points), filled only by load_plugins(True): name → class.
_PLUGIN_ADAPTERS: dict[str, type] = {}
_PLUGIN_DETECTORS: dict[str, type] = {}


def load(dotted: str) -> type:
    """Import ``"package.module:Class"`` and return the class (lazy; errors surface only here)."""
    module_name, sep, attr = dotted.partition(":")
    if not sep or not module_name or not attr:
        raise UsageError("registry entries have the form 'package.module:Class'")
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ImportError(f"{module_name} has no attribute {attr}") from None
    return obj


def _resolve(entry: str | type) -> type:
    return entry if isinstance(entry, type) else load(entry)


def get_adapter(name: str) -> Adapter:
    """Instantiate the adapter registered under *name* (built-in, or a loaded plugin)."""
    entry: str | type | None = BUILTIN_ADAPTERS.get(name)
    if entry is None:
        entry = _PLUGIN_ADAPTERS.get(name)
    if entry is None:
        raise UsageError(f"unknown adapter {name!r}")
    return _resolve(entry)()


def _read_head(path: Path) -> bytes:
    try:
        with open(path, "rb") as f:
            raw = f.read(SNIFF_HEAD_BYTES)
    except OSError as exc:
        raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None
    suffix = path.suffix.lower()
    if suffix == ".gz":
        try:
            with gzip.open(path, "rb") as gz:
                return gz.read(SNIFF_HEAD_BYTES)
        except (OSError, EOFError, zlib.error):
            return raw
    if suffix == ".zst":
        try:
            zstd = importlib.import_module("compression.zstd")
        except ImportError:
            return raw
        try:  # Python >= 3.14 (or an injected stand-in in tests)
            with zstd.open(path, "rb") as zf:
                return zf.read(SNIFF_HEAD_BYTES)
        except Exception:  # corrupt stream: sniff the raw bytes
            return raw
    return raw


def sniff_adapter(path: Path, *, notes: list[DataQualityNote] | None = None) -> Adapter | None:
    """The first adapter (fixed order: built-ins, then plugins by name) whose ``sniff`` accepts the
    ≤ 64 KiB head of *path* (decompressed for ``.gz``, and ``.zst`` when possible); None if none
    does. A registered adapter whose module cannot be imported is skipped; one
    ``dq.adapter_unavailable`` note per such entry is appended to *notes* when given (R-E18).
    :func:`get_adapter` still raises for an explicitly requested missing adapter."""
    path = Path(path)
    head = _read_head(path)
    names: list[str] = list(BUILTIN_ADAPTERS) + sorted(_PLUGIN_ADAPTERS)
    for name in names:
        try:
            adapter = get_adapter(name)
        except ImportError:
            logger.debug("adapter %s unavailable; skipped while sniffing", name)
            if notes is not None:
                notes.append(
                    DataQualityNote(
                        code=DQ_ADAPTER_UNAVAILABLE,
                        severity="warn",
                        count=1,
                        detail=f"adapter {name} is not installed; skipped while sniffing",
                    )
                )
            continue
        try:
            accepted = adapter.sniff(path, head)
        except Exception:  # a sniffer must never abort discovery on hostile input
            logger.debug("adapter %s sniff raised; treated as no match", name, exc_info=True)
            continue
        if accepted:
            return adapter
    return None


def _detector_entries() -> list[tuple[str, str | type]]:
    entries: list[tuple[str, str | type]] = list(BUILTIN_DETECTORS.items())
    entries.extend(sorted(_PLUGIN_DETECTORS.items()))
    return entries


def all_detectors(*, notes: list[DataQualityNote] | None = None) -> list[Detector]:
    """Every registered detector, instantiated and sorted by id. Unimportable ones are skipped with
    a ``dq.detector_unavailable`` note appended to *notes* (when given) and logged."""
    found: list[Detector] = []
    for det_id, entry in _detector_entries():
        try:
            cls = _resolve(entry)
        except ImportError as exc:
            logger.info("detector %s unavailable (%s)", det_id, type(exc).__name__)
            if notes is not None:
                notes.append(
                    DataQualityNote(
                        code=DQ_DETECTOR_UNAVAILABLE,
                        severity="warn",
                        count=1,
                        detail=f"detector {det_id} is not installed; skipped",
                    )
                )
            continue
        found.append(cls())
    found.sort(key=lambda d: d.id)
    return found


def _explicit_detectors(only: Sequence[str]) -> list[Detector]:
    table = dict(_detector_entries())
    chosen: list[Detector] = []
    seen: set[str] = set()
    for det_id in only:
        if det_id in seen:
            continue
        seen.add(det_id)
        entry = table.get(det_id)
        if entry is None:
            raise UsageError(f"unknown detector {det_id!r}")
        chosen.append(_resolve(entry)())
    chosen.sort(key=lambda d: d.id)
    return chosen


def _finding_id(detector_id: str, kind: str, scope: Scope) -> str:
    try:
        from tokenbill.core.findings import finding_id  # F-SEM (wave 1)
    except ImportError:
        return stable_id("fd", detector_id, kind, *(f"{k}={v}" for k, v in scope.dims))
    return finding_id(detector_id, kind, scope)


def _missing_capabilities_finding(
    detector: Detector, missing: Iterable[str], ctx: AnalysisContext
) -> Finding:
    names = ", ".join(sorted(missing))
    scope = Scope(dims=())
    kind = "missing-capabilities"
    title = f"{detector.id}: missing capabilities"
    summary = (
        f"Detector {detector.id} did not run: the loaded sources lack {names}. "
        "No mechanical fix; add a source that provides these capabilities."
    )
    return Finding(
        finding_id=_finding_id(detector.id, kind, scope),
        detector_id=detector.id,
        kind=kind,
        detector_version=detector.version,
        category="data-quality",
        lever_class="none",
        audience="org",
        title=title[:120],
        summary=summary[:400],
        scope=scope,
        n_events=0,
        n_lanes=0,
        n_users=0,
        first_seen_ms=ctx.window[0],
        cost_observed=unpriced("unpriced: capabilities missing"),
        recoverable=None,
        confidence="high",
        references=("dq.missing-capabilities",),
    )


def _finding_sort_key(f: Finding) -> tuple[int, str, str]:
    p50 = f.recoverable.nano if f.recoverable is not None and f.recoverable.nano is not None else 0
    return (-p50, f.detector_id, f.finding_id)


#: Product family of lanes and findings when ``core.findings.product_family`` is unavailable.
DEFAULT_FAMILY = "default"


def _product_family_fn() -> Callable[[Lane], str] | None:
    """``core.findings.product_family`` (F-SEM-C), imported lazily; None when missing."""
    try:
        from tokenbill.core import findings as _findings
    except ImportError:
        return None
    return getattr(_findings, "product_family", None)


def _family_exclusions() -> Mapping[tuple[str, str | None], frozenset[str]]:
    """``core.catalog.FAMILY_EXCLUSIONS`` (F-KIT-C), imported lazily; empty when missing."""
    try:
        from tokenbill.core import catalog as _catalog
    except ImportError:
        return {}
    table = getattr(_catalog, "FAMILY_EXCLUSIONS", None)
    return table if table is not None else {}


class _Families:
    """Per-call cache of lane product families (computed only when a detector filters lanes)."""

    def __init__(self, lanes: Sequence[Lane]) -> None:
        self._lanes = lanes
        self._families: list[str] | None = None

    def of_lanes(self) -> list[str]:
        if self._families is None:
            fn = _product_family_fn()
            self._families = [
                fn(lane) if fn is not None else DEFAULT_FAMILY for lane in self._lanes
            ]
        return self._families


def _lanes_for(detector: Detector, lanes: Sequence[Lane], families: _Families,
               exclusions: Mapping[tuple[str, str | None], frozenset[str]]) -> Sequence[Lane]:
    allowed = getattr(detector, "families", None)
    excluded = exclusions.get((detector.id, None), frozenset())
    if allowed is None and not excluded:
        return lanes  # unchanged: every pre-Copilot detector sees exactly today's lanes
    return [
        lane
        for lane, fam in zip(lanes, families.of_lanes(), strict=True)
        if fam not in excluded and (allowed is None or fam in allowed)
    ]


def _excluded_finding(f: Finding,
                      exclusions: Mapping[tuple[str, str | None], frozenset[str]]) -> bool:
    if not exclusions:
        return False
    family = dict(f.scope.dims).get("product", DEFAULT_FAMILY)
    return family in exclusions.get((f.detector_id, f.kind), frozenset()) or family in (
        exclusions.get((f.detector_id, None), frozenset())
    )


def run_detectors(
    lanes: Sequence[Lane],
    ctx: AnalysisContext,
    *,
    only: Sequence[str] | None = None,
    emit_missing: bool = True,
    aggregates_only: bool | None = None,
) -> list[Finding]:
    """Run the registered detectors (or only the ids in *only*) over *lanes*.

    A detector whose ``requires`` is not a subset of ``ctx.capabilities`` does not run; with
    *emit_missing* it yields exactly one ``data-quality`` finding of kind ``missing-capabilities``
    (no dollars). The pipeline calls this per shard with ``emit_missing=False`` and once with no
    lanes and ``emit_missing=True``. Output is sorted by (−recoverable point or 0, detector_id,
    finding_id); findings are returned unpublished (the caller applies
    ``core.kanon.rescope_findings``).

    Detector class attributes (read with ``getattr`` defaults, so existing detectors need none):
    ``extension`` (None) — the detector runs only when ``f"ext:{extension}"`` is in
    ``ctx.capabilities`` and is otherwise skipped silently (no missing-capabilities finding);
    ``aggregate`` (False) — see *aggregates_only*; ``families`` (None = every family) — the product
    families whose lanes it sees. Lanes are filtered by ``core.findings.product_family(lane)``
    (``"default"`` when unavailable) against ``families`` and the detector-level entries
    ``(detector_id, None)`` of ``core.catalog.FAMILY_EXCLUSIONS`` (empty when unavailable);
    findings whose scope ``product`` dim (``"default"`` when absent) is excluded for their
    ``(detector_id, kind)`` or their whole detector are dropped after the run.

    *aggregates_only*: ``None`` runs every detector (today's behavior plus the gating above);
    ``False`` only ``aggregate=False`` detectors and never emits missing-capabilities findings (the
    per-shard call); ``True`` only ``aggregate=True`` detectors plus the missing-capabilities pass
    over every detector (the once-per-run call, ruling R-E17).
    """
    detectors = all_detectors() if only is None else _explicit_detectors(only)
    caps = frozenset(ctx.capabilities)
    exclusions = _family_exclusions()
    families = _Families(lanes)
    findings: list[Finding] = []
    for detector in detectors:
        extension = getattr(detector, "extension", None)
        if extension is not None and f"ext:{extension}" not in caps:
            continue
        missing = frozenset(detector.requires) - caps
        if missing:
            if emit_missing and aggregates_only is not False:
                findings.append(_missing_capabilities_finding(detector, missing, ctx))
            continue
        if aggregates_only is not None and bool(getattr(detector, "aggregate", False)) is not (
            aggregates_only
        ):
            continue
        out = detector.detect(_lanes_for(detector, lanes, families, exclusions), ctx)
        findings.extend(f for f in out if not _excluded_finding(f, exclusions))
    findings.sort(key=_finding_sort_key)
    return findings


# ---------------------------------------------------------------------------------------------
# channel extensions (GitHub Copilot, C-26; resolved by core.extensions, never imported here)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgvAlias:
    """A CLI alias rewritten before argparse (``core.extensions.rewrite_argv``)."""

    verb: str
    trigger: str
    position: str                 # "first" (argv[1] == trigger) | "any" (anywhere after the verb)
    target: tuple[str, ...]       # replacement prefix; the trigger token is removed

    def __post_init__(self) -> None:
        if self.position not in ("first", "any"):
            raise ContractViolation("ArgvAlias.position must be 'first' or 'any'")
        if not self.target or not all(isinstance(t, str) and t for t in self.target):
            raise ContractViolation("ArgvAlias.target must be a non-empty tuple of tokens")


@dataclass(frozen=True)
class ExtensionSpec:
    """One channel extension: dotted paths (``"module:attr"``) of its hooks, resolved lazily by
    ``core.extensions``; a missing module degrades to a ``dq.extension_unavailable`` note."""

    name: str                                    # "copilot" (RunResult slot name)
    channels: tuple[str, ...]                    # owned channels: RECON/OrgScan skip; FOCUS owned
    rate_files: tuple[str, ...]                  # "package:resource.json"
    rate_verifier: str | None
    reconciler: str | None                       # core.protocols.ChannelReconciler
    record_store: str | None                     # "module:Class" implementing ExtRecordStore
    # (store, record_stores, ctx, *, today, reconciled_channels, recon_decisions=())
    #   -> AnalysisContext
    context_enricher: str | None
    # (store, record_stores, ctx, findings, plan, pricer, *, today, k) -> summary
    summary_builder: str | None
    section_renderer: str | None                 # "module:Class" implementing SectionRenderer
    # (store, record_stores, *, since_ms, until_ms, reconciled_channels, k, allow_unreconciled,
    #  role) -> Iterable[FocusRow]
    focus_rows: str | None
    showback: str | None                         # (result, out_dir, formats) -> list[Path]
    policy_targets: tuple[tuple[str, str], ...]  # target → builder
    panel_builder: str | None
    command_module: str | None                   # extra top-level verb module ("copilot")
    argv_aliases: tuple[ArgvAlias, ...] = ()


EXTENSIONS: dict[str, ExtensionSpec] = {
    "copilot": ExtensionSpec(
        name="copilot",
        channels=("github_copilot", "github_actions", "github_sandbox"),
        rate_files=("tokenbill.copilot.data:github_copilot.json",),
        rate_verifier="tokenbill.copilot.rates_verify:verify",
        reconciler="tokenbill.copilot.recon:reconcile_copilot",
        record_store="tokenbill.copilot.record_store:CopilotRecordStore",
        context_enricher="tokenbill.copilot.enrich:enrich_context",
        summary_builder="tokenbill.pipeline.copilot:summarize",
        section_renderer="tokenbill.copilot.render:CopilotSection",
        focus_rows="tokenbill.copilot.focus:focus_rows",
        showback="tokenbill.copilot.showback:render_copilot_showback",
        policy_targets=(("github-copilot", "tokenbill.pipeline.copilot:policy_packs"),),
        panel_builder="tokenbill.copilot.panel:build_copilot_panel",
        command_module="tokenbill.commands.copilot",
        argv_aliases=(
            ArgvAlias("scan", "--copilot", "any", ("copilot", "scan")),
            ArgvAlias("me", "--copilot", "any", ("copilot", "me")),
            ArgvAlias("collect", "copilot-cli", "first", ("copilot", "collect", "--source", "cli")),
            ArgvAlias("collect", "copilot-vscode", "first",
                      ("copilot", "collect", "--source", "vscode")),
        ),
    ),
}


def _entry_points(group: str) -> Iterable[importlib.metadata.EntryPoint]:
    return importlib.metadata.entry_points(group=group)


def load_plugins(enabled: bool) -> list[str]:
    """Load third-party ``tokenbill.adapters`` / ``tokenbill.detectors`` entry points — ONLY when
    *enabled* (CLI ``--plugins``). Built-in names cannot be overridden. Returns the names loaded."""
    if not enabled:
        return []
    loaded: list[str] = []
    for group, target, builtin in (
        ("tokenbill.adapters", _PLUGIN_ADAPTERS, BUILTIN_ADAPTERS),
        ("tokenbill.detectors", _PLUGIN_DETECTORS, BUILTIN_DETECTORS),
    ):
        for ep in sorted(_entry_points(group), key=lambda e: e.name):
            if ep.name in builtin:
                logger.warning("plugin %s ignored: it would shadow a built-in", ep.name)
                continue
            try:
                obj = ep.load()
            except Exception as exc:  # a broken plugin must not break the run
                logger.warning("plugin %s failed to load (%s)", ep.name, type(exc).__name__)
                continue
            if not isinstance(obj, type):
                logger.warning("plugin %s ignored: not a class", ep.name)
                continue
            target[ep.name] = obj
            loaded.append(ep.name)
    return loaded
