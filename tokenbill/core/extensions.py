"""Channel-extension host (SPEC-v0.2-COPILOT §2.4, §3.8 CA-39; CORE-AMENDMENTS E-1; F-EXT).

Generic callers (RATES, WIRING, OUT, RECON, CLI-LEDGER, CLI-SAVINGS) reach channel-extension
code — today only GitHub Copilot — exclusively through the one-line functions of this module,
never through ``core.registry.EXTENSIONS`` directly. Rules shared by every function:

* Extensions are visited in **name order** (:func:`extensions`); within one extension, entries
  keep their declared order.
* Hook dotted paths (``"package.module:attr"``) are resolved **lazily**, at call time, with
  ``core.registry.load``; importing this module imports no extension module (only
  ``tokenbill.core``).
* A hook whose module (or attribute) cannot be imported, or a rate-file resource that does not
  exist, is skipped: the call appends one ``DataQualityNote(code="dq.extension_unavailable",
  severity="warn", count=1, detail="<extension>:<hook>")`` to the caller's ``notes`` list (when
  given; at most one note per extension and hook per call) and returns normally (ruling R-E18).
  Hook names are the ``ExtensionSpec`` field names (``rate_files``, ``reconciler``,
  ``record_store``, ``context_enricher``, ``summary_builder``, ``section_renderer``,
  ``focus_rows``, ``showback``, ``policy_targets``, ``panel_builder``, ``command_module``,
  ``rate_verifier``).
* Only ``ImportError`` (incl. ``ModuleNotFoundError``) raised while *resolving* a hook and missing
  ``importlib.resources`` resources are absorbed. Every other exception — including anything a
  hook raises while it runs, and malformed registrations — propagates. A hook that returns the
  wrong type raises ``ContractViolation``.
* Every function that can resolve an extension module takes the keyword ``notes`` (CA-39 plus the
  F-EXT delta: also ``command_modules``, ``capabilities_present``, ``policy_targets``, ``panel``,
  ``rate_verifiers`` and ``count_users_fn``). The pure table readers :func:`extensions`,
  :func:`delegated_channels` and :func:`rewrite_argv` import nothing and take none. The CLI
  listings :func:`command_modules` and :func:`policy_targets` only *locate* modules
  (``importlib.util.find_spec``), so building a parser never executes extension code; the hooks
  they name are resolved when dispatched (the CLI's lazy verb import, :func:`policy_packs`).

Decisions of this implementation where CA-39 is silent (documented for the wave-2 callers):

* ``run_reconcilers`` passes ``rounding_remainders`` only for a store that implements
  ``core.protocols.LedgerStats`` (else ``None``): a mapping **adapter name → USD** (exact
  ``Decimal``) built from the integer stats key :data:`ROUNDING_REMAINDER_STAT`
  (``rounding_remainder_e18``, 1e-18 USD units, addendum §5.1) of
  ``store.source_stats(adapter=<name>)`` for every registered adapter with a non-zero remainder,
  e.g. ``{"github-ai-usage": Decimal("1.2E-17")}``; ``{}`` when the store holds no remainder.
  (Addendum §12 names the AI usage report's entry by its source kind ``github.ai_usage_report``;
  the ledger groups stats by adapter, so the key is the adapter name ``github-ai-usage``.)
* ``render_sections(result, "json")`` returns one ``{<extension name>: <section object>}`` dict
  per extension (the ``RunResult`` slot name is the result@2 key); empty text sections and
  ``None`` JSON sections are left out.
* ``focus_rows`` owns the channels of every extension that declares a ``focus_rows`` hook — also
  when the hook is unavailable (then OUT emits no FOCUS row for them rather than collector-lane
  detail that could double count the provider's own lines); a returned row whose channel is not
  one of its extension's channels raises ``ContractViolation``.
* ``capabilities_present`` attributes a record store to the extension whose name equals the
  store's ``name``; a store whose name is no extension's name is attributed to every extension
  that declares a ``record_store`` hook.
* ``command_modules`` maps the extension name (the top-level verb, e.g. ``copilot``) to its
  module.
* ``count_users_fn`` needs ``core.kanon.scope_counter`` and ``core.catalog.COUNT_SOURCE``
  (F-KIT-C); when either is missing it records a note (detail ``kanon:scope_counter`` /
  ``catalog:COUNT_SOURCE``) and degrades conservatively (count source ``requests``; a counter
  that returns 0, so re-scoping can only suppress more).
"""

from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tokenbill.core import registry
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.protocols import (
    ExtRecordStore,
    LedgerStats,
    LedgerStore,
    Pricer,
    SectionRenderer,
)
from tokenbill.core.registry import ExtensionSpec
from tokenbill.core.types import (
    ActionPlan,
    AnalysisContext,
    DataQualityNote,
    Finding,
    FocusRow,
    IngestResult,
    PanelRow,
    PolicyPack,
    ReconciliationReport,
    RunResult,
    Scope,
)

if TYPE_CHECKING:  # Python >= 3.11 location; only used in annotations
    from importlib.resources.abc import Traversable

__all__ = [
    "DEFAULT_COUNT_SOURCE",
    "DQ_EXTENSION_UNAVAILABLE",
    "ROUNDING_REMAINDER_STAT",
    "SECTION_FORMATS",
    "capabilities_present",
    "command_modules",
    "count_users_fn",
    "delegated_channels",
    "enrich",
    "extension_rate_files",
    "extensions",
    "focus_rows",
    "open_record_stores",
    "panel",
    "persist",
    "policy_packs",
    "policy_targets",
    "purge",
    "rate_verifiers",
    "recon_decisions_of",
    "render_sections",
    "retain",
    "rewrite_argv",
    "run_reconcilers",
    "showback",
    "summarize",
]

logger = logging.getLogger("tokenbill.core.extensions")

#: Data-quality code for an extension hook module or resource that is not installed (R-E18).
DQ_EXTENSION_UNAVAILABLE = "dq.extension_unavailable"
#: Integer ``IngestResult.stats`` key holding parse remainders in 1e-18 USD (addendum §5.1).
ROUNDING_REMAINDER_STAT = "rounding_remainder_e18"
#: Count source of a finding kind absent from ``core.catalog.COUNT_SOURCE`` (addendum CA-36).
DEFAULT_COUNT_SOURCE = "requests"
#: Formats :func:`render_sections` renders (the three ``SectionRenderer`` methods).
SECTION_FORMATS = ("terminal", "html", "json")

_NOTE_DETAIL_MAX = 256


# ---------------------------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------------------------


def _check_notes(notes: object) -> list[DataQualityNote] | None:
    if notes is not None and not isinstance(notes, list):
        raise ContractViolation("notes must be a list of DataQualityNote or None")
    return notes


class _Unavailable:
    """Collects the ``dq.extension_unavailable`` notes of one host call: at most one per
    (extension, hook); appended to the caller's list when there is one, always logged."""

    def __init__(self, notes: list[DataQualityNote] | None) -> None:
        self._notes = _check_notes(notes)
        self._seen: set[tuple[str, str]] = set()

    def add(self, owner: str, hook: str) -> None:
        if (owner, hook) in self._seen:
            return
        self._seen.add((owner, hook))
        detail = f"{owner}:{hook}"[:_NOTE_DETAIL_MAX]
        logger.info("extension hook %s unavailable; skipped", detail)
        if self._notes is not None:
            self._notes.append(DataQualityNote(code=DQ_EXTENSION_UNAVAILABLE, severity="warn",
                                               count=1, detail=detail))


def _resolve(spec: ExtensionSpec, hook: str, dotted: str, missing: _Unavailable) -> Any | None:
    """``core.registry.load(dotted)``, or None (with a note) when the module or attribute is not
    importable. A malformed entry raises (``UsageError`` from ``load``)."""
    try:
        return registry.load(dotted)
    except ImportError:
        missing.add(spec.name, hook)
        return None


def _module_of(dotted: str) -> str:
    module, sep, attr = dotted.partition(":")
    if not sep or not module or not attr:
        raise ContractViolation("extension hooks have the form 'package.module:attr'")
    return module


def _module_present(module: str) -> bool:
    """Whether *module* can be imported, located without executing it (``find_spec``)."""
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:  # a parent package is missing
        return False
    except ValueError:  # present in sys.modules without a spec (e.g. an injected module)
        return True


def _split_resource(entry: str) -> tuple[str, tuple[str, ...]]:
    package, sep, resource = entry.partition(":")
    parts = tuple(resource.split("/")) if resource else ()
    if not sep or not package or not parts or any(p in ("", ".", "..") for p in parts):
        raise ContractViolation("extension rate files have the form 'package:resource.json'")
    return package, parts


def _e18_to_usd(count: int) -> Decimal:
    """``count`` × 1e-18 USD as an exact Decimal (no context rounding)."""
    sign = 1 if count < 0 else 0
    return Decimal((sign, tuple(int(d) for d in str(abs(count))), -18))


def _first(iterable: Iterable[Any]) -> Any | None:
    """The first item of *iterable* (None when empty), closing a generator / cursor afterwards —
    the ``LIMIT 1`` of the protocol iterators."""
    it = iter(iterable)
    try:
        return next(it, None)
    finally:
        close = getattr(it, "close", None)
        if callable(close):
            close()


def _pairs_of(decisions: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(decisions, tuple):
        raise ContractViolation("recon_decisions must be a tuple of (key, value) string pairs")
    for pair in decisions:
        if not (isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], str)
                and isinstance(pair[1], str)):
            raise ContractViolation("recon_decisions must be a tuple of (key, value) string pairs")
    return decisions


# ---------------------------------------------------------------------------------------------
# pure table readers (import nothing, take no notes)
# ---------------------------------------------------------------------------------------------


def extensions() -> list[ExtensionSpec]:
    """Every registered channel extension (``core.registry.EXTENSIONS``), sorted by name.

    Raises ``ContractViolation`` for an entry that is not an ``ExtensionSpec`` or whose key differs
    from its ``name``."""
    specs: list[ExtensionSpec] = []
    for key, spec in registry.EXTENSIONS.items():
        if not isinstance(spec, ExtensionSpec) or spec.name != key:
            raise ContractViolation("EXTENSIONS maps each extension name to its ExtensionSpec")
        specs.append(spec)
    specs.sort(key=lambda s: s.name)
    return specs


def delegated_channels() -> frozenset[str]:
    """The channels owned by extensions (RECON's ``reconcile`` and ``OrgScan`` skip them)."""
    return frozenset(ch for spec in extensions() for ch in spec.channels)


def rewrite_argv(argv: Sequence[str]) -> list[str]:
    """Rewrite a CLI alias before argparse (addendum DC23, §15).

    *argv* is the argument vector without the program name, so ``argv[0]`` is the verb. Aliases are
    tried per extension (name order) in declared order; the first match wins:

    * ``position="first"`` matches when ``argv[0] == verb`` and ``argv[1] == trigger``;
    * ``position="any"`` matches when ``argv[0] == verb`` and the trigger token occurs anywhere
      after the verb and before a ``--`` end-of-options marker, as a whole token (``--copilot=1``
      is not matched).

    On a match the verb and every occurrence of the trigger (before ``--``) are removed and the
    alias target prefix (one or more tokens) is put in front of the remaining arguments, e.g.
    ``["collect", "copilot-cli", "--out", "D"]`` → ``["copilot", "collect", "--source", "cli",
    "--out", "D"]``. Without a match a copy of *argv* is returned unchanged.
    """
    args = list(argv)
    if not args:
        return args
    verb, rest = args[0], args[1:]
    end = rest.index("--") if "--" in rest else len(rest)
    for spec in extensions():
        for alias in spec.argv_aliases:
            if alias.verb != verb:
                continue
            if alias.position == "first":
                if rest and rest[0] == alias.trigger:
                    return [*alias.target, *rest[1:]]
            elif alias.trigger in rest[:end]:
                kept = [token for token in rest[:end] if token != alias.trigger]
                return [*alias.target, *kept, *rest[end:]]
    return args


def recon_decisions_of(reports: Sequence[ReconciliationReport]) -> tuple[tuple[str, str], ...]:
    """The sorted union of every report's ``decisions`` (CORE-AMENDMENTS C-17, E-1).

    The same key with the same value in several reports appears once; the same key with two
    different values raises ``ContractViolation`` (two reconcilers disagreeing is a defect, never
    resolved silently). The result is what ``enrich(…, recon_decisions=…)`` expects."""
    merged: dict[str, str] = {}
    for report in reports:
        if not isinstance(report, ReconciliationReport):
            raise ContractViolation("recon_decisions_of expects ReconciliationReports")
        for key, value in report.decisions:
            prior = merged.setdefault(key, value)
            if prior != value:
                prefix = key.split(":", 1)[0]
                raise ContractViolation(
                    f"reconciliation reports disagree on a '{prefix}:' decision")
    return tuple(sorted(merged.items()))


# ---------------------------------------------------------------------------------------------
# rates, CLI tables
# ---------------------------------------------------------------------------------------------


def extension_rate_files(*, notes: list[DataQualityNote] | None = None) -> tuple[Traversable, ...]:
    """The rate files (``tokenbill/rates@1`` JSON) extensions ship, as ``importlib.resources``
    handles in extension-name and declared order. Entries are ``"package:resource.json"`` (the
    resource may name a sub-path with ``/``; the anchor must be a package, never a plain module). A
    missing package or resource is skipped with a ``<extension>:rate_files`` note (RATES loads the
    rest beside its builtin files); importing the package runs its ``__init__`` only."""
    missing = _Unavailable(notes)
    found: list[Traversable] = []
    for spec in extensions():
        for entry in spec.rate_files:
            package, parts = _split_resource(entry)
            try:
                anchor = importlib.import_module(package)
            except ImportError:
                missing.add(spec.name, "rate_files")
                continue
            if getattr(anchor, "__path__", None) is None:  # same rule on 3.10 and 3.12+
                raise ContractViolation("extension rate files are resources of a package")
            node = importlib.resources.files(anchor)
            for part in parts:
                node = node.joinpath(part)
            if not node.is_file():
                missing.add(spec.name, "rate_files")
                continue
            found.append(node)
    return tuple(found)


def rate_verifiers(*, notes: list[DataQualityNote] | None = None) -> list[str]:
    """Dotted paths of the extensions' rate verifiers that resolve (``pricing verify`` loads them
    with ``core.registry.load`` and runs them); an unavailable one is left out with a
    ``<extension>:rate_verifier`` note, so the caller's own load cannot fail."""
    missing = _Unavailable(notes)
    out: list[str] = []
    for spec in extensions():
        if spec.rate_verifier is None:
            continue
        if _resolve(spec, "rate_verifier", spec.rate_verifier, missing) is not None:
            out.append(spec.rate_verifier)
    return out


def command_modules(*, notes: list[DataQualityNote] | None = None) -> dict[str, str]:
    """Top-level verb → command module (``add_parser(subparsers)`` / ``run(args)``) for the CLI's
    ``COMMANDS`` merge. The verb is the extension name; a module that does not exist is left out
    with a ``<extension>:command_module`` note (located, never imported here)."""
    missing = _Unavailable(notes)
    out: dict[str, str] = {}
    for spec in extensions():
        if spec.command_module is None:
            continue
        if _module_present(spec.command_module):
            out[spec.name] = spec.command_module
        else:
            missing.add(spec.name, "command_module")
    return out


def _target_table() -> list[tuple[ExtensionSpec, str, str]]:
    table: list[tuple[ExtensionSpec, str, str]] = []
    seen: set[str] = set()
    for spec in extensions():
        for target, dotted in spec.policy_targets:
            if target in seen:
                raise ContractViolation("two extensions declare the same policy target")
            seen.add(target)
            table.append((spec, target, dotted))
    return table


def policy_targets(*, notes: list[DataQualityNote] | None = None) -> dict[str, str]:
    """Policy target (``policy --target``) → pack-builder dotted path, for targets whose builder
    module exists; a missing one is left out with a ``<extension>:policy_targets`` note."""
    missing = _Unavailable(notes)
    out: dict[str, str] = {}
    for spec, target, dotted in _target_table():
        if _module_present(_module_of(dotted)):
            out[target] = dotted
        else:
            missing.add(spec.name, "policy_targets")
    return out


# ---------------------------------------------------------------------------------------------
# record stores
# ---------------------------------------------------------------------------------------------


def open_record_stores(db_path: Path, *, create: bool,
                       notes: list[DataQualityNote] | None = None) -> list[ExtRecordStore]:
    """Open every extension's record store on the ledger's database file: the ``record_store``
    class is instantiated as ``cls(db_path, create=create)``. An unavailable class is skipped with a
    ``<extension>:record_store`` note; an object that is not an ``ExtRecordStore`` raises
    ``ContractViolation``."""
    missing = _Unavailable(notes)
    stores: list[ExtRecordStore] = []
    for spec in extensions():
        if spec.record_store is None:
            continue
        cls = _resolve(spec, "record_store", spec.record_store, missing)
        if cls is None:
            continue
        store = cls(db_path, create=create)
        if not isinstance(store, ExtRecordStore):
            raise ContractViolation("an extension record_store must implement ExtRecordStore")
        stores.append(store)
    return stores


def persist(record_stores: Sequence[ExtRecordStore], result: IngestResult, *,
            notes: list[DataQualityNote] | None = None) -> dict[str, int]:
    """Route ``result.licenses`` / ``activity`` / ``config`` to the record stores.

    Each store receives ``put(result, principal_key_id=result.source.principal_key_id)`` and keeps
    the records it owns, checking the key id against the ledger's ``meta`` (ruling R-E21). Callers
    persist **after** the ledger ingest of the same result, so a store that adopted a key id has it
    before the record stores check it. Returns the stores' counts summed per key (``{}`` when the
    result carries no such record). *notes* is accepted for the uniform host signature (no module is
    resolved here)."""
    _check_notes(notes)
    if not isinstance(result, IngestResult):
        raise ContractViolation("persist expects an IngestResult")
    if not (result.licenses or result.activity or result.config):
        return {}
    counts: dict[str, int] = {}
    for store in record_stores:
        got = store.put(result, principal_key_id=result.source.principal_key_id)
        for key, value in got.items():
            counts[key] = counts.get(key, 0) + int(value)
    return dict(sorted(counts.items()))


def retain(record_stores: Sequence[ExtRecordStore], *, identity_before_ms: int,
           notes: list[DataQualityNote] | None = None) -> int:
    """Apply identity retention in every record store; returns the rows removed in total."""
    _check_notes(notes)
    return sum(int(store.retain(identity_before_ms=identity_before_ms))
               for store in record_stores)


def purge(record_stores: Sequence[ExtRecordStore], *, principal: str | None,
          before_ms: int | None, actor: str,
          notes: list[DataQualityNote] | None = None) -> int:
    """Erase one principal's rows and/or rows older than *before_ms* in every record store (each
    store writes its own audit row); returns the rows removed in total."""
    _check_notes(notes)
    return sum(int(store.purge(principal=principal, before_ms=before_ms, actor=actor))
               for store in record_stores)


def _stores_of(spec: ExtensionSpec, record_stores: Sequence[ExtRecordStore],
               names: frozenset[str]) -> list[ExtRecordStore]:
    out: list[ExtRecordStore] = []
    for store in record_stores:
        name = getattr(store, "name", None)
        if name == spec.name or (name not in names and spec.record_store is not None):
            out.append(store)
    return out


def capabilities_present(store: LedgerStore, record_stores: Sequence[ExtRecordStore], *,
                         since_ms: int, until_ms: int,
                         notes: list[DataQualityNote] | None = None) -> frozenset[str]:
    """``"ext:<name>"`` for every extension with data in ``[since_ms, until_ms)``: a cost line, an
    aggregate (``channel`` dim) or a request on one of its channels in the ledger, or a license,
    activity day or config snapshot in one of its record stores (e.g. an activity-report-only
    handoff has only licenses). Requests are probed per channel with a ``LIMIT 1`` style read of
    ``iter_requests(where={"channel": …})``; the protocol lists are read once per call. *notes* is
    accepted for the uniform host signature (no module is resolved here)."""
    _check_notes(notes)
    specs = extensions()
    names = frozenset(spec.name for spec in specs)
    window = {"since_ms": since_ms, "until_ms": until_ms}
    lines: list[Any] | None = None
    aggregates: list[Any] | None = None
    present: set[str] = set()

    def ledger_has(channels: frozenset[str]) -> bool:
        nonlocal lines, aggregates
        if lines is None:
            lines = store.cost_lines(None, **window)
        if any(line.channel in channels for line in lines):
            return True
        if aggregates is None:
            aggregates = store.aggregates(None, **window)
        if any(dict(agg.dims).get("channel") in channels for agg in aggregates):
            return True
        return any(
            _first(store.iter_requests(since_ms=since_ms, until_ms=until_ms,
                                       where={"channel": channel})) is not None
            for channel in sorted(channels))

    def records_have(stores: Sequence[ExtRecordStore]) -> bool:
        return any(rs.licenses(**window) or rs.activity(**window) or rs.config(**window)
                   for rs in stores)

    for spec in specs:
        channels = frozenset(spec.channels)
        if (channels and ledger_has(channels)) or records_have(
                _stores_of(spec, record_stores, names)):
            present.add(f"ext:{spec.name}")
    return frozenset(present)


# ---------------------------------------------------------------------------------------------
# reconciliation and enrichment
# ---------------------------------------------------------------------------------------------


def _adapter_names() -> list[str]:
    names = list(registry.BUILTIN_ADAPTERS)
    names.extend(sorted(n for n in registry._PLUGIN_ADAPTERS if n not in registry.BUILTIN_ADAPTERS))
    return names


def _rounding_remainders(store: LedgerStats) -> dict[str, Decimal]:
    """Adapter name → Σ parse remainder in USD (exact), from ``source_stats`` (see module doc)."""
    if not store.source_stats().get(ROUNDING_REMAINDER_STAT):
        return {}
    out: dict[str, Decimal] = {}
    for name in _adapter_names():
        count = int(store.source_stats(adapter=name).get(ROUNDING_REMAINDER_STAT, 0))
        if count:
            out[name] = _e18_to_usd(count)
    return out


def run_reconcilers(store: LedgerStore, record_stores: Sequence[ExtRecordStore], pricer: Pricer, *,
                    since_ms: int, until_ms: int, tolerance_pct: str, unexplained_pct: str,
                    closed_only: bool, today: str,
                    notes: list[DataQualityNote] | None = None) -> list[ReconciliationReport]:
    """Run every extension's ``ChannelReconciler`` over the window; one report per available
    reconciler, in extension-name order (RECON's ``merge_reports`` combines them with its own).

    ``rounding_remainders`` is computed from ``store.source_stats()`` only when
    ``isinstance(store, LedgerStats)`` (adapter name → USD, see the module doc), else ``None``.
    A report of the wrong type raises ``ContractViolation``."""
    missing = _Unavailable(notes)
    reports: list[ReconciliationReport] = []
    remainders: dict[str, Decimal] | None = None
    computed = False
    for spec in extensions():
        if spec.reconciler is None:
            continue
        fn = _resolve(spec, "reconciler", spec.reconciler, missing)
        if fn is None:
            continue
        if not computed:
            remainders = _rounding_remainders(store) if isinstance(store, LedgerStats) else None
            computed = True
        report = fn(store, record_stores, pricer, since_ms=since_ms, until_ms=until_ms,
                    tolerance_pct=tolerance_pct, unexplained_pct=unexplained_pct,
                    closed_only=closed_only, today=today, rounding_remainders=remainders)
        if not isinstance(report, ReconciliationReport):
            raise ContractViolation("a channel reconciler must return a ReconciliationReport")
        reports.append(report)
    return reports


def enrich(store: LedgerStore, record_stores: Sequence[ExtRecordStore], ctx: AnalysisContext, *,
           today: str, reconciled_channels: frozenset[str],
           recon_decisions: tuple[tuple[str, str], ...] = (),
           notes: list[DataQualityNote] | None = None) -> AnalysisContext:
    """Let every extension's ``context_enricher`` extend *ctx* (licenses, activity, config,
    outcomes, plans, pools, ``reconciled_channels``, ``recon_decisions``, ``ext:<name>``), chained
    in extension-name order. *recon_decisions* — normally
    ``recon_decisions_of(run_reconcilers(…))`` — is forwarded unchanged to each enricher as
    ``recon_decisions=`` (the only path by which reconciler decisions reach the enricher; they are
    never persisted). Without an available enricher *ctx* is returned unchanged."""
    missing = _Unavailable(notes)
    if not isinstance(ctx, AnalysisContext):
        raise ContractViolation("enrich expects an AnalysisContext")
    _pairs_of(recon_decisions)
    for spec in extensions():
        if spec.context_enricher is None:
            continue
        fn = _resolve(spec, "context_enricher", spec.context_enricher, missing)
        if fn is None:
            continue
        ctx = fn(store, record_stores, ctx, today=today, reconciled_channels=reconciled_channels,
                 recon_decisions=recon_decisions)
        if not isinstance(ctx, AnalysisContext):
            raise ContractViolation("a context enricher must return an AnalysisContext")
    return ctx


def summarize(store: LedgerStore, record_stores: Sequence[ExtRecordStore], ctx: AnalysisContext,
              findings: Sequence[Finding], plan: ActionPlan | None, pricer: Pricer, *, today: str,
              k: int, notes: list[DataQualityNote] | None = None) -> dict[str, object]:
    """Extension name (the ``RunResult`` slot, e.g. ``copilot``) → the summary its
    ``summary_builder`` returns; builders returning None are left out."""
    missing = _Unavailable(notes)
    out: dict[str, object] = {}
    for spec in extensions():
        if spec.summary_builder is None:
            continue
        fn = _resolve(spec, "summary_builder", spec.summary_builder, missing)
        if fn is None:
            continue
        summary = fn(store, record_stores, ctx, findings, plan, pricer, today=today, k=k)
        if summary is not None:
            out[spec.name] = summary
    return out


# ---------------------------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------------------------


def render_sections(result: RunResult, fmt: str, *, width: int = 100,
                    notes: list[DataQualityNote] | None = None) -> list[str | dict]:
    """Render every extension's output section of *result*: *fmt* ``"terminal"`` → text (≤ *width*
    columns), ``"html"`` → an escaped ``<section>``, ``"json"`` → ``{<extension name>: <object>}``
    (MONEY via ``figure_json``). The ``section_renderer`` class is instantiated without arguments
    and must implement ``SectionRenderer``; empty text and ``None`` objects are left out."""
    if fmt not in SECTION_FORMATS:
        raise UsageError("section format must be one of terminal, html, json")
    missing = _Unavailable(notes)
    out: list[str | dict] = []
    for spec in extensions():
        if spec.section_renderer is None:
            continue
        cls = _resolve(spec, "section_renderer", spec.section_renderer, missing)
        if cls is None:
            continue
        renderer = cls()
        if not isinstance(renderer, SectionRenderer):
            raise ContractViolation("a section_renderer must implement SectionRenderer")
        if fmt == "json":
            doc = renderer.json(result)
            if doc is None:
                continue
            if not isinstance(doc, dict):
                raise ContractViolation("SectionRenderer.json must return a dict or None")
            out.append({spec.name: doc})
            continue
        text = renderer.terminal(result, width=width) if fmt == "terminal" else renderer.html(
            result)
        if not isinstance(text, str):
            raise ContractViolation("SectionRenderer.terminal/html must return text")
        if text:
            out.append(text)
    return out


def focus_rows(store: LedgerStore, record_stores: Sequence[ExtRecordStore], *, since_ms: int,
               until_ms: int, reconciled_channels: frozenset[str], k: int,
               allow_unreconciled: bool, role: str,
               notes: list[DataQualityNote] | None = None
               ) -> tuple[list[FocusRow], frozenset[str]]:
    """The extensions' FOCUS 1.4 rows and the channels they own (addendum §14.3).

    Owned channels are the ``ExtensionSpec.channels`` of every extension declaring a ``focus_rows``
    hook — also for channels with no row and for an unavailable hook — so OUT drops its ledger rows
    of those channels (``write_focus(…, extra_rows=rows, owned_channels=owned)``). A row that is
    not a ``FocusRow`` or whose channel its extension does not own raises
    ``ContractViolation``."""
    missing = _Unavailable(notes)
    rows: list[FocusRow] = []
    owned: set[str] = set()
    for spec in extensions():
        if spec.focus_rows is None:
            continue
        owned.update(spec.channels)
        fn = _resolve(spec, "focus_rows", spec.focus_rows, missing)
        if fn is None:
            continue
        for row in fn(store, record_stores, since_ms=since_ms, until_ms=until_ms,
                      reconciled_channels=reconciled_channels, k=k,
                      allow_unreconciled=allow_unreconciled, role=role):
            if not isinstance(row, FocusRow):
                raise ContractViolation("an extension focus_rows hook must yield FocusRows")
            if row.channel not in spec.channels:
                raise ContractViolation("an extension FOCUS row is on a channel it does not own")
            rows.append(row)
    return rows, frozenset(owned)


def showback(result: RunResult, out_dir: Path, formats: Sequence[str], *,
             notes: list[DataQualityNote] | None = None) -> list[Path]:
    """Write every extension's showback pages for *result* into *out_dir*; returns the paths
    written, in extension-name order."""
    missing = _Unavailable(notes)
    written: list[Path] = []
    for spec in extensions():
        if spec.showback is None:
            continue
        fn = _resolve(spec, "showback", spec.showback, missing)
        if fn is None:
            continue
        paths = fn(result, out_dir, formats)
        for path in paths:
            if not isinstance(path, Path):
                raise ContractViolation("an extension showback hook must return Paths")
            written.append(path)
    return written


# ---------------------------------------------------------------------------------------------
# policy, panels, k-anonymity
# ---------------------------------------------------------------------------------------------


def policy_packs(target: str, store: LedgerStore, record_stores: Sequence[ExtRecordStore],
                 ctx: AnalysisContext, findings: Sequence[Finding], result: RunResult | None, *,
                 out_dir: Path | None, current: object, cohort_by: str, include_tradeoffs: bool,
                 notes: list[DataQualityNote] | None = None) -> list[PolicyPack]:
    """Policy packs for an extension *target* (e.g. ``github-copilot``). The target's builder is
    called as ``builder(store, record_stores, ctx, findings, result, out_dir=…, current=…,
    cohort_by=…, include_tradeoffs=…)`` (CP-WIRE's hook signature; the target is implied by the
    builder). An unavailable builder yields ``[]`` with a ``<extension>:policy_targets`` note; a
    target no extension declares raises ``UsageError``."""
    missing = _Unavailable(notes)
    for spec, declared, dotted in _target_table():
        if declared != target:
            continue
        fn = _resolve(spec, "policy_targets", dotted, missing)
        if fn is None:
            return []
        packs = list(fn(store, record_stores, ctx, findings, result, out_dir=out_dir,
                        current=current, cohort_by=cohort_by,
                        include_tradeoffs=include_tradeoffs))
        if not all(isinstance(p, PolicyPack) for p in packs):
            raise ContractViolation("an extension policy builder must return PolicyPacks")
        return packs
    raise UsageError(f"unknown policy target {target!r}")


def panel(name: str, store: LedgerStore, record_stores: Sequence[ExtRecordStore], *,
          notes: list[DataQualityNote] | None = None, **kw: Any) -> list[PanelRow]:
    """Verification panel rows of extension *name* (``measure --panel <name>``): its
    ``panel_builder(store, record_stores, **kw)``. An unavailable builder yields ``[]`` with a
    ``<name>:panel_builder`` note; an extension that is unknown or declares no panel raises
    ``UsageError``."""
    missing = _Unavailable(notes)
    for spec in extensions():
        if spec.name != name:
            continue
        if spec.panel_builder is None:
            break
        fn = _resolve(spec, "panel_builder", spec.panel_builder, missing)
        if fn is None:
            return []
        rows = list(fn(store, record_stores, **kw))
        if not all(isinstance(r, PanelRow) for r in rows):
            raise ContractViolation("an extension panel builder must return PanelRows")
        return rows
    raise UsageError(f"no verification panel named {name!r}")


def _no_users(finding: Finding | Scope, scope: Scope | None = None) -> int:
    """Fallback counter while ``core.kanon.scope_counter`` is unavailable: zero users, so
    ``rescope_findings`` can only re-scope further or withhold (never publish too small a group)."""
    return 0


def count_users_fn(ledger: LedgerStore, record_stores: Sequence[ExtRecordStore], *,
                   since_ms: int, until_ms: int,
                   notes: list[DataQualityNote] | None = None
                   ) -> Callable[[Finding, Scope], int]:
    """The ``count_users`` argument of ``core.kanon.rescope_findings`` for runs with extension
    data: ``core.kanon.scope_counter(ledger, record_stores, since_ms=…, until_ms=…,
    source_of=…)`` where ``source_of(finding)`` is ``core.catalog.COUNT_SOURCE[(detector_id,
    kind)]`` (default ``"requests"``). Both are imported lazily; see the module doc for the
    conservative fallback when either is missing."""
    missing = _Unavailable(notes)
    try:
        from tokenbill.core import catalog as _catalog
    except ImportError:  # pragma: no cover - core.catalog is part of the merged foundation
        _catalog = None
    table: Mapping[tuple[str, str], str] | None = getattr(_catalog, "COUNT_SOURCE", None)
    if table is None:
        missing.add("catalog", "COUNT_SOURCE")
        table = {}
    sources: Mapping[tuple[str, str], str] = table

    def source_of(finding: Finding) -> str:
        return sources.get((finding.detector_id, finding.kind), DEFAULT_COUNT_SOURCE)

    try:
        from tokenbill.core import kanon as _kanon
    except ImportError:  # pragma: no cover - core.kanon is part of the merged foundation
        _kanon = None
    scope_counter = getattr(_kanon, "scope_counter", None)
    if scope_counter is None:
        missing.add("kanon", "scope_counter")
        return _no_users
    return scope_counter(ledger, record_stores, since_ms=since_ms, until_ms=until_ms,
                         source_of=source_of)
