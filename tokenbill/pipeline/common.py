"""Shared pipeline plumbing for the CLI packages (SPEC §15 "Pipeline", §3.21, D30; WIRING).

Pure functions without printing, so ``pipeline.ledger`` (CLI-LEDGER) and ``pipeline.savings``
(CLI-SAVINGS) build on the same pieces:

* :class:`Env` / :func:`build_env` — the per-command environment: the resolved :class:`Config`, the
  pricer (the RATES ``RateCard`` loaded lazily from :data:`RATE_ENGINE`, or any factory passed as
  ``pricer_factory`` — tests pass ``core.testing.FakePricer``), the cache-rule table, keys.
* :func:`open_store` — the ledger (``tokenbill.store.db:SqliteStore``, loaded lazily from
  :data:`STORE_CLASS`) with the Env's keys and pricer; ``adopt_key_ids`` passes R-E21 adoption
  through.
* :func:`ingest_options` / :func:`ingest_paths` — the adapter ingestion loop: sniff or select
  adapters, complete ``IngestOptions`` from the Env, ingest into the ledger, then persist seat /
  activity / config records through ``core.extensions.persist`` (**after** the ledger ingest, so an
  adopted key id exists before the record stores check it) and re-read deferred files
  (``stats["defer:<adapter>"]``).
* :func:`map_shards` — one call per ``ShardKey``, sequential or in a process pool whose workers open
  the store read-only by path (:func:`shard_store`); results in shard order.
* :func:`bill_summary` — the ``BillSummary`` of a window: totals (exact / estimated apart; allowance
  = subscription list-equivalent lines only, pool = Copilot list-equivalent lines), ESR, k-anonymous
  breakdowns, the Claude Code naive line-sum ratio and footnotes.
* R-E40 / R-E24 helpers — :func:`org_compaction_median`, :func:`analysis_thresholds` and
  :func:`with_compaction_post`: the org median COMPACTION summary size, computed once per run.

Decisions where the SPEC is silent (also listed in ``tests/v2/wiring/README.md``):

* ``build_env``: explicit arguments win over the Config's (``rates``, ``contract``, ``key_file``,
  ``collection_key_file``); without a collection key the org key doubles as the name key (a
  single-key central host); rate layers go to ``RateCard(layers, contract=…)`` lowest precedence
  first: builtin, ``--rates`` files in order, then the ``--model-price`` layer.
* ``ingest_paths`` completes the given ``IngestOptions`` from the Env: the name key when unset, the
  org key as principal key for identity modes ``install`` / ``central-ingest`` when unset, missing
  key ids, ``k_anonymity = max(opts, env.k)``, the Config's name allowlist (union) and ``now_ms``
  when 0. A directory given with ``adapter="auto"`` is read whole by the one adapter that claims its
  files, or file by file when several do. The Claude Code naive usage (``IngestResult.naive_usage``)
  is kept as integer source stats ``naive:<model>:<bucket>`` so :func:`bill_summary` can price it
  later.
* ``bill_summary`` totals come from the store's priced columns (``aggregate(pricer=None)``); ESR and
  the naive ratio are priced with the Env pricer. ESR = 1 − Σ exact ÷ Σ no-cache equivalent over the
  window's billable inferences priced EXACT on a billed basis; it is None without the RATES engine's
  ``no_cache_equivalent_nano``.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import multiprocessing
import pickle
import time
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from tokenbill.config import Config
from tokenbill.core import extensions, keys, registry
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import ContractViolation, PricingError, SourceError, UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.jsonl import load_json_exact
from tokenbill.core.kanon import publish, require_self_or_aggregate
from tokenbill.core.labels import Basis, Evidence, Figure, add, exact
from tokenbill.core.money import ratio
from tokenbill.core.protocols import (
    Adapter,
    CacheRulesProvider,
    ExtRecordStore,
    LedgerStats,
    LedgerStore,
    Pricer,
)
from tokenbill.core.records import (
    COPILOT_BILLING_PATHS,
    Inference,
    Lane,
    LaneEventKind,
    PricingContext,
    UsageSource,
)
from tokenbill.core.types import (
    BillSummary,
    DataQualityNote,
    IngestOptions,
    IngestResult,
    Policy,
    PricedTotal,
    PublishedAggregate,
    ShardKey,
    SourceInfo,
)

__all__ = [
    "AGGREGATE_DIMS",
    "COMPACTION_POST_TOKENS_KEY",
    "DEFER_STAT_PREFIX",
    "DQ_RECORDS_NOT_PERSISTED",
    "FOOTNOTE_PLACEHOLDER",
    "FOOTNOTE_TRACE_V1",
    "FOOTNOTE_UNPRICED",
    "INGEST_IDENTITY_MODES",
    "NAIVE_STAT_PREFIX",
    "NO_CACHE_EQUIVALENT",
    "RATE_CONTRACT_MODULE",
    "RATE_ENGINE",
    "RATE_SCHEMA_MODULE",
    "STORE_CLASS",
    "Env",
    "analysis_thresholds",
    "bill_summary",
    "build_env",
    "compaction_post_tokens",
    "ingest_options",
    "ingest_paths",
    "load_rate_card",
    "load_team_map",
    "map_shards",
    "median_tokens",
    "open_store",
    "org_compaction_median",
    "shard_store",
    "with_compaction_post",
]

logger = logging.getLogger("tokenbill.pipeline.common")

T = TypeVar("T")

#: The RATES rate card class and helpers, resolved lazily (never imported at module import).
RATE_ENGINE = "tokenbill.rates.engine:RateCard"
RATE_SCHEMA_MODULE = "tokenbill.rates.schema"
RATE_CONTRACT_MODULE = "tokenbill.rates.contract"
NO_CACHE_EQUIVALENT = "tokenbill.rates.engine:no_cache_equivalent_nano"
#: The STORE ledger class, resolved lazily; opened as ``cls(path, create=…, org_key=…,
#: name_key_id=…, pricer=…)`` by :func:`open_store` and as ``cls(path, create=False,
#: read_only=True)`` by shard workers.
STORE_CLASS = "tokenbill.store.db:SqliteStore"

#: ``IngestOptions.identity_mode`` values (SPEC §5.1).
INGEST_IDENTITY_MODES = ("install", "central", "two-stage", "central-ingest")
#: ``IngestResult.stats`` prefix of a deferral: ``defer:<adapter>`` = resources left to that
#: adapter.
DEFER_STAT_PREFIX = "defer:"
#: Source-stats prefix of the Claude Code naive line sums: ``naive:<model>:<bucket>`` = tokens.
NAIVE_STAT_PREFIX = "naive:"
#: Data-quality code: seat / activity / config records read but not stored (no record store).
DQ_RECORDS_NOT_PERSISTED = "dq.records_not_persisted"
#: Threshold key of the org median compaction summary size (ruling R-E40).
COMPACTION_POST_TOKENS_KEY = "context.compaction-window.post_tokens"
#: ``LedgerStore.aggregate`` group-by whitelist (SPEC §7.2); breakdown dimensions come from here.
AGGREGATE_DIMS = ("date", "team", "cost_center", "workspace_id", "workload_class", "lane_kind",
                  "model", "agent_type", "agent_product", "repo", "arm", "wave", "skill",
                  "mcp_server", "billing_path", "channel")

FOOTNOTE_TRACE_V1 = (
    "trace@1 cache writes are priced at the 5-minute rate; writes of calls that carried a "
    "ttl \"1h\" marker are a range between the 5-minute and 1-hour rates (estimated).")
FOOTNOTE_PLACEHOLDER = (
    "Some outputs were logged as streaming placeholders (message_start only): their output lines "
    "are estimated ranges, shown beside the exact bill.")
FOOTNOTE_UNPRICED = (
    "Unpriced inferences (unknown model or date) are reported as coverage, never as $0.")

_TRACE_V1 = "trace@1"
_CLAUDE_CODE = "claude-code"
_DQ_PRINCIPAL_KEY_MISMATCH = "dq.principal_key_mismatch"
_DQ_NAME_KEY_MISMATCH = "dq.name_key_mismatch"
_NAIVE_BUCKETS = {  # UsageBuckets field → UnitRates bucket
    "uncached_input": "uncached_input", "cache_read": "cache_read",
    "cache_write_5m": "cache_write_5m", "cache_write_1h": "cache_write_1h",
    "cache_write_other": "cache_write_other", "cache_write_unknown": "cache_write_unknown",
    "output": "output", "web_search_requests": "web_search",
}
_BILLED_BASES = (Basis.LIST, Basis.CONTRACT)
_TEAM_MAP_MAX_BYTES = 16 << 20
_TEXT_MAX = 256


# =============================================================================================
# Env
# =============================================================================================


@dataclass(frozen=True)
class Env:
    """Resolved config + keys + pricer, built once per command (SPEC §15). Key bytes are kept out of
    ``repr``."""

    config: Config
    pricer: Pricer
    rules: CacheRulesProvider
    k: int
    jobs: int
    org_key: bytes | None = field(repr=False)
    name_key: bytes | None = field(repr=False)
    name_key_id: str | None
    now_ms: int


def load_rate_card(*, rates: Sequence[Path] = (), contract: Path | None = None,
                   model_prices: Sequence[tuple[str, str, str]] = ()) -> Pricer:
    """The RATES ``RateCard`` over the builtin registry, the ``--rates`` files (in order) and the
    ``--model-price`` layer, with the contract overlay (SPEC §6.2). Layers are passed lowest
    precedence first. A missing ``tokenbill.rates`` raises :class:`PricingError`."""
    try:
        schema_mod = importlib.import_module(RATE_SCHEMA_MODULE)
        card = registry.load(RATE_ENGINE)
    except ImportError:
        raise PricingError("the rate engine (tokenbill.rates) is not installed") from None
    layers = [schema_mod.load_builtin()]
    for path in rates:
        layers.append(schema_mod.load_file(Path(path), f"user:{Path(path).name}"))
    if model_prices:
        layers.append(schema_mod.model_price_layer(tuple(model_prices)))
    overlay = None
    if contract is not None:
        try:
            load_contract = registry.load(RATE_CONTRACT_MODULE + ":load_contract")
        except ImportError:
            raise PricingError("the contract loader (tokenbill.rates.contract) is not installed") \
                from None
        overlay = load_contract(Path(contract))
    return card(layers, contract=overlay)


def _model_price(spec: object) -> tuple[str, str, str]:
    if (not isinstance(spec, (tuple, list)) or len(spec) != 3
            or not all(isinstance(part, str) and part.strip() for part in spec)):
        raise UsageError("--model-price entries are (model, input USD/MTok, output USD/MTok)")
    return (spec[0].strip(), spec[1].strip(), spec[2].strip())


def _load_key(path: object, what: str) -> bytes | None:
    if path is None:
        return None
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise UsageError(f"{what}: expected a file path")
    return keys.load(Path(path).expanduser())


def build_env(config: Config, *, rates: Sequence[Path] = (), contract: Path | None = None,
              model_prices: Sequence[tuple[str, str, str]] = (), key_file: Path | None = None,
              collection_key_file: Path | None = None, now_ms: int | None = None,
              pricer_factory: Callable[..., Pricer] | None = None) -> Env:
    """Build the per-command :class:`Env`.

    Explicit arguments win over the Config (``rates`` over ``config.rates``, ``contract`` over
    ``config.contract``, ``key_file`` / ``collection_key_file`` over the Config's). Keys are read
    with ``core.keys.load`` (0600 checks); the org key is the principal key, the collection key the
    name key, and without a collection key the org key also names. The pricer comes from
    ``pricer_factory(rates=…, contract=…, model_prices=…)`` (default :func:`load_rate_card`, the
    registry-style lazy import of ``tokenbill.rates.engine``). ``now_ms`` defaults to the wall
    clock."""
    if not isinstance(config, Config):
        raise UsageError("build_env expects a Config")
    rate_files = tuple(Path(p).expanduser() for p in (rates if rates else config.rates))
    contract_src = contract if contract is not None else config.contract
    contract_path = Path(contract_src).expanduser() if contract_src is not None else None
    specs = tuple(_model_price(s) for s in model_prices)
    factory = pricer_factory if pricer_factory is not None else load_rate_card
    pricer = factory(rates=rate_files, contract=contract_path, model_prices=specs)
    if not isinstance(pricer, Pricer):
        raise ContractViolation("the pricer factory must return a Pricer")
    org_key = _load_key(key_file if key_file is not None else config.key_file, "key file")
    collection = _load_key(collection_key_file if collection_key_file is not None
                           else config.collection_key_file, "collection key file")
    name_key = collection if collection is not None else org_key
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    if type(now_ms) is not int or now_ms < 0:
        raise UsageError("now_ms must be a non-negative int (ms since the epoch)")
    return Env(config=config, pricer=pricer, rules=RulesTable(), k=config.k, jobs=config.jobs,
               org_key=org_key, name_key=name_key,
               name_key_id=key_id(name_key) if name_key is not None else None, now_ms=now_ms)


# =============================================================================================
# store
# =============================================================================================

# Ledger objects opened by open_store → their database file (so ingest_paths can open the extension
# record stores on the same file).
_STORE_PATHS: weakref.WeakKeyDictionary[object, Path] = weakref.WeakKeyDictionary()


def _store_class() -> Any:
    try:
        return registry.load(STORE_CLASS)
    except ImportError:
        raise UsageError("the SQLite store (tokenbill.store) is not installed") from None


def open_store(path: Path, env: Env, *, create: bool = True,
               adopt_key_ids: bool = False) -> LedgerStore:
    """Open (or create) the ledger at *path* with the Env's org key, name key id and pricer.
    ``create=False`` on a missing file raises ``UsageError``; ``adopt_key_ids=True`` is passed
    through to the store (ruling R-E21: adopt the first ``copilot-export`` bundle's key ids)."""
    path = Path(path).expanduser()
    if not create and not path.exists():
        raise UsageError(f"store {path.name}: not found")
    kwargs: dict[str, object] = {"create": create, "org_key": env.org_key,
                                 "name_key_id": env.name_key_id, "pricer": env.pricer}
    cls = _store_class()
    if adopt_key_ids:
        kwargs["adopt_key_ids"] = True
        try:
            store = cls(path, **kwargs)
        except TypeError:
            raise UsageError("the installed store does not support key-id adoption") from None
    else:
        store = cls(path, **kwargs)
    try:
        _STORE_PATHS[store] = path
    except TypeError:  # not weak-referenceable: record stores need an explicit list
        logger.debug("store object is not weak-referenceable; record-store path not kept")
    return store


def _store_path(store: object) -> Path | None:
    try:
        known = _STORE_PATHS.get(store)
    except TypeError:
        known = None
    if known is not None:
        return known
    for attr in ("db_path", "path"):
        value = getattr(store, attr, None)
        if isinstance(value, (str, Path)) and str(value):
            return Path(value)
    return None


# =============================================================================================
# ingestion
# =============================================================================================


def ingest_options(env: Env, *, identity_mode: str | None = None, **fields: Any) -> IngestOptions:
    """``IngestOptions`` for this Env: *identity_mode* (default ``central-ingest`` with an org key,
    else ``install``) and any other ``IngestOptions`` field in *fields* (window, team map,
    attribution, renormalize, lenient, …), completed like :func:`ingest_paths` does (keys, k, name
    allowlist, clock). Unknown fields or modes raise ``UsageError``."""
    mode = identity_mode if identity_mode is not None else (
        "central-ingest" if env.org_key is not None else "install")
    if mode not in INGEST_IDENTITY_MODES:
        raise UsageError(f"identity mode must be one of {', '.join(INGEST_IDENTITY_MODES)}")
    names = {f.name for f in dataclasses.fields(IngestOptions)}
    unknown = sorted(set(fields) - names)
    if unknown:
        raise UsageError(f"unknown ingest option(s): {', '.join(unknown)}")
    return _complete_options(IngestOptions(identity_mode=mode, **fields), env)


def _complete_options(opts: IngestOptions, env: Env) -> IngestOptions:
    if not isinstance(opts, IngestOptions):
        raise UsageError("ingest_paths expects IngestOptions")
    changes: dict[str, Any] = {}
    if not opts.name_key and env.name_key:
        changes["name_key"] = env.name_key
        changes["name_key_id"] = env.name_key_id or key_id(env.name_key)
    elif opts.name_key and not opts.name_key_id:
        changes["name_key_id"] = key_id(opts.name_key)
    if (opts.principal_key is None and env.org_key is not None
            and opts.identity_mode in ("install", "central-ingest")):
        changes["principal_key"] = env.org_key
        changes["principal_key_id"] = key_id(env.org_key)
    elif opts.principal_key is not None and not opts.principal_key_id:
        changes["principal_key_id"] = key_id(opts.principal_key)
    if env.k > opts.k_anonymity:
        changes["k_anonymity"] = env.k
    allow = frozenset(opts.name_allowlist) | env.config.name_allowlist
    if allow != opts.name_allowlist:
        changes["name_allowlist"] = allow
    if not opts.now_ms and env.now_ms:
        changes["now_ms"] = env.now_ms
    return dataclasses.replace(opts, **changes) if changes else opts


class _Notes:
    """Data-quality notes of one call: identical (code, severity, detail) notes merge their counts
    (and tokens); :meth:`once` keeps only the first of a kind (sniffing repeats per file)."""

    def __init__(self) -> None:
        self._notes: list[DataQualityNote] = []
        self._index: dict[tuple[str, str, str], int] = {}

    def add(self, note: DataQualityNote, *, once: bool = False) -> None:
        if note.figure is not None:
            self._notes.append(note)
            return
        key = (note.code, note.severity, note.detail)
        at = self._index.get(key)
        if at is None:
            self._index[key] = len(self._notes)
            self._notes.append(note)
            return
        if once:
            return
        prev = self._notes[at]
        tokens = (None if prev.tokens is None and note.tokens is None
                  else (prev.tokens or 0) + (note.tokens or 0))
        self._notes[at] = dataclasses.replace(prev, count=prev.count + note.count, tokens=tokens)

    def new(self, code: str, severity: str, count: int, detail: str, *,
            once: bool = False) -> None:
        self.add(DataQualityNote(code=code, severity=severity, count=count,
                                 detail=detail[:_TEXT_MAX]), once=once)

    def extend(self, notes: Iterable[DataQualityNote], *, once: bool = False) -> None:
        for note in notes:
            self.add(note, once=once)

    def result(self) -> list[DataQualityNote]:
        return list(self._notes)


class _RecordStores:
    """The extension record stores of one ``ingest_paths`` call: the caller's list, else opened
    lazily on the ledger's database file (only when a result carries records)."""

    def __init__(self, store: LedgerStore, given: Sequence[ExtRecordStore] | None,
                 notes: _Notes) -> None:
        self._store = store
        self._stores: list[ExtRecordStore] | None = list(given) if given is not None else None
        self._opened = given is not None
        self._notes = notes

    def _get(self) -> list[ExtRecordStore]:
        if not self._opened:
            self._opened = True
            path = _store_path(self._store)
            if path is not None:
                found: list[DataQualityNote] = []
                self._stores = extensions.open_record_stores(path, create=True, notes=found)
                self._notes.extend(found, once=True)
        return self._stores or []

    def persist(self, result: IngestResult) -> None:
        n = len(result.licenses) + len(result.activity) + len(result.config)
        if not n:
            return
        stores = self._get()
        if not stores:
            self._notes.new(DQ_RECORDS_NOT_PERSISTED, "warn", n,
                            f"{result.source.adapter}: no extension record store for this ledger; "
                            "seat, activity and configuration records were not stored")
            return
        found: list[DataQualityNote] = []
        counts = extensions.persist(stores, result, found)
        self._notes.extend(found)
        skipped = counts.get(_DQ_PRINCIPAL_KEY_MISMATCH, 0)
        if skipped:
            self._notes.new(_DQ_PRINCIPAL_KEY_MISMATCH, "warn", skipped,
                            f"{result.source.adapter}: records under another key id were not "
                            "stored")


def _naive_stats(result: IngestResult) -> None:
    """Keep ``result.naive_usage`` as integer source stats ``naive:<model>:<bucket>``."""
    for model, usage in sorted(result.naive_usage.items()):
        if not model:
            continue
        for name in _NAIVE_BUCKETS:
            tokens = getattr(usage, name)
            if tokens:
                result.stats[f"{NAIVE_STAT_PREFIX}{model}:{name}"] = tokens


def _deferrals(result: IngestResult) -> list[tuple[str, int]]:
    out = []
    for key, value in sorted(result.stats.items()):
        if (key.startswith(DEFER_STAT_PREFIX) and type(value) is int and value > 0
                and key[len(DEFER_STAT_PREFIX):]):
            out.append((key[len(DEFER_STAT_PREFIX):], value))
    return out


class _Ingest:
    """State of one :func:`ingest_paths` call."""

    def __init__(self, store: LedgerStore, env: Env, opts: IngestOptions, notes: _Notes,
                 records: _RecordStores) -> None:
        self.store = store
        self.env = env
        self.opts = opts
        self.notes = notes
        self.records = records
        self.sources: list[SourceInfo] = []
        self.done: set[tuple[str, str]] = set()
        self._adapters: dict[str, Adapter] = {}

    def explicit(self, name: str) -> Adapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            try:
                adapter = registry.get_adapter(name)
            except ImportError:
                raise UsageError(f"adapter {name} is not installed") from None
            self._adapters[name] = adapter
        return adapter

    def sniff(self, path: Path) -> Adapter | None:
        found: list[DataQualityNote] = []
        adapter = registry.sniff_adapter(path, notes=found)
        self.notes.extend(found, once=True)
        return adapter

    def plan(self, path: Path, adapter: str) -> list[tuple[Path, Adapter]]:
        if adapter != "auto":
            return [(path, self.explicit(adapter))]
        if not path.is_dir():
            chosen = self.sniff(path)
            if chosen is None:
                raise UsageError(f"{path.name}: no adapter recognizes this file "
                                 "(pass --adapter NAME)")
            return [(path, chosen)]
        claimed: list[tuple[Path, Adapter]] = []
        for member in sorted(p for p in path.rglob("*") if p.is_file()
                             and not any(part.startswith(".")
                                         for part in p.relative_to(path).parts)):
            chosen = self.sniff(member)
            if chosen is not None:
                claimed.append((member, chosen))
        names = {a.name for _, a in claimed}
        if not names:
            raise UsageError(f"{path.name}: no adapter recognizes a file in this directory "
                             "(pass --adapter NAME)")
        if len(names) == 1:
            return [(path, claimed[0][1])]
        return claimed

    def read(self, path: Path, adapter: Adapter) -> None:
        self.done.add((str(path), adapter.name))
        result = adapter.read(path, self.opts)
        if not isinstance(result, IngestResult):
            raise ContractViolation(f"adapter {adapter.name} did not return an IngestResult")
        _naive_stats(result)
        counts = self.store.ingest(result, pricer=self.env.pricer)
        self.records.persist(result)  # after the ledger ingest (R-E21 adoption first)
        self.sources.append(result.source)
        self.notes.extend(result.notes)
        name = result.source.adapter
        if result.quarantined and not any(n.code == "dq.quarantined" for n in result.notes):
            self.notes.new("dq.quarantined", "warn", len(result.quarantined),
                           f"{name}: records quarantined")
        for code in (_DQ_NAME_KEY_MISMATCH, _DQ_PRINCIPAL_KEY_MISMATCH):
            value = counts.get(code, 0) if isinstance(counts, Mapping) else 0
            if type(value) is int and value > 0:
                self.notes.new(code, "warn", value,
                               f"{name}: values under another key id were not stored")
        for deferred, n in _deferrals(result):
            if (str(path), deferred) in self.done:
                continue
            try:
                follow = self.explicit(deferred)
            except UsageError:
                self.notes.new(registry.DQ_ADAPTER_UNAVAILABLE, "warn", n,
                               f"adapter {deferred} is not installed; deferred resources not read")
                continue
            self.read(path, follow)


def ingest_paths(store: LedgerStore, paths: Sequence[Path], env: Env, opts: IngestOptions, *,
                 adapter: str = "auto", record_stores: Sequence[ExtRecordStore] | None = None
                 ) -> tuple[list[SourceInfo], list[DataQualityNote]]:
    """Read *paths* with their adapters and ingest them into *store* (priced with ``env.pricer``).

    *adapter* ``"auto"`` sniffs each file (``core.registry.sniff_adapter``; an unrecognized file
    raises ``UsageError``); a name selects that adapter (``UsageError`` when unknown or not
    installed). For each result: ledger ingest, then seat / activity / config records through
    ``core.extensions.persist`` into *record_stores* (default: the extension record stores opened on
    the database file of a store opened with :func:`open_store`; without any, the records are
    counted as ``dq.records_not_persisted``), then every ``stats["defer:<adapter>"] > 0`` re-reads
    the same file with that adapter once. Returns the ``SourceInfo`` of every result in read order
    and the merged data-quality notes (adapters', quarantine and key-id mismatches, unavailable
    adapters)."""
    if isinstance(paths, (str, Path)):
        raise UsageError("ingest_paths expects a sequence of paths")
    if not isinstance(adapter, str) or not adapter:
        raise UsageError("adapter must be 'auto' or an adapter name")
    notes = _Notes()
    run = _Ingest(store, env, _complete_options(opts, env), notes,
                  _RecordStores(store, record_stores, notes))
    for raw in paths:
        path = Path(raw).expanduser()
        for member, chosen in run.plan(path, adapter):
            if (str(member), chosen.name) not in run.done:
                run.read(member, chosen)
    return run.sources, notes.result()


def load_team_map(path: Path) -> tuple[tuple[str, str], ...]:
    """A team map file for ``IngestOptions.team_map`` (``--team-map FILE``): a JSON object mapping a
    raw actor reference to a team, e.g. ``{"alice@example.com": "payments"}`` (applied at ingest
    then discarded, SPEC §5.1). Returns sorted pairs; malformed files raise ``UsageError`` with a
    content-free message (no reference or team is ever echoed)."""
    path = Path(path).expanduser()
    try:
        data = load_json_exact(path, max_bytes=_TEAM_MAP_MAX_BYTES)
    except SourceError as exc:
        raise UsageError(f"team map {exc}") from None
    if not isinstance(data, dict):
        raise UsageError(f"team map {path.name}: must be a JSON object of reference → team")
    pairs: dict[str, str] = {}
    for ref, team in data.items():
        if not (isinstance(ref, str) and ref.strip() and isinstance(team, str) and team.strip()
                and len(team) <= 128 and len(ref) <= 1024
                and not any(ord(c) < 32 or ord(c) == 127 for c in ref + team)):
            raise UsageError(f"team map {path.name}: every entry must map a non-empty reference "
                             "to a team name of at most 128 characters")
        pairs[ref.strip()] = team.strip()
    return tuple(sorted(pairs.items()))


# =============================================================================================
# shards
# =============================================================================================

_SHARD_STORE: LedgerStore | None = None


def shard_store() -> LedgerStore:
    """The read-only ledger of the running :func:`map_shards` call (in the worker process, or in
    this process when it runs sequentially). ``UsageError`` outside such a call or without
    ``db_path``."""
    if _SHARD_STORE is None:
        raise UsageError("no shard store is open: map_shards was called without db_path")
    return _SHARD_STORE


def _open_read_only(db_path: str, store_class: str) -> LedgerStore:
    return registry.load(store_class)(Path(db_path), create=False, read_only=True)


def _close(store: object) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _worker_init(db_path: str | None, store_class: str) -> None:
    global _SHARD_STORE
    _SHARD_STORE = _open_read_only(db_path, store_class) if db_path is not None else None


def map_shards(fn: Callable[[ShardKey], T], shards: Sequence[ShardKey], *, jobs: int,
               db_path: Path | None) -> list[T]:
    """``[fn(shard) for shard in shards]``, in shard order (SPEC §15, D30).

    ``jobs == 1`` (or a single shard) runs in this process; otherwise a ``ProcessPoolExecutor``
    (spawn start method, ``min(jobs, len(shards))`` workers) runs *fn*, which must then be picklable
    (a top-level function, or a ``functools.partial`` of one with picklable arguments; else
    ``ContractViolation``). With *db_path* the store is opened read-only by path
    (:data:`STORE_CLASS`, ``cls(path, create=False, read_only=True)``) — once per worker, or once
    here when sequential — and *fn* reaches it through :func:`shard_store`. Results are identical
    for every ``jobs`` value; an exception in *fn* propagates."""
    global _SHARD_STORE
    items = list(shards)
    if type(jobs) is not int or jobs < 1:
        raise UsageError("jobs must be an int >= 1")
    if not all(isinstance(s, ShardKey) for s in items):
        raise ContractViolation("map_shards expects ShardKeys")
    path = None if db_path is None else str(Path(db_path).expanduser())
    if path is not None and not Path(path).exists():
        raise UsageError(f"store {Path(path).name}: not found")
    if not items:
        return []
    workers = min(jobs, len(items))
    store_class = STORE_CLASS
    if workers == 1:
        previous = _SHARD_STORE
        store = _open_read_only(path, store_class) if path is not None else None
        _SHARD_STORE = store
        try:
            return [fn(shard) for shard in items]
        finally:
            _SHARD_STORE = previous
            if store is not None:
                _close(store)
    try:
        pickle.dumps(fn)
    except Exception:
        raise ContractViolation("map_shards: fn must be picklable (a top-level function) when "
                                "jobs > 1") from None
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context, initializer=_worker_init,
                             initargs=(path, store_class)) as pool:
        return list(pool.map(fn, items))


# =============================================================================================
# bill summary
# =============================================================================================


def _breakdown_specs(group_by: Sequence[str]) -> list[tuple[str, ...]]:
    if isinstance(group_by, str):
        group_by = [group_by]
    specs: list[tuple[str, ...]] = []
    for spec in group_by:
        if not isinstance(spec, str):
            raise UsageError("group-by entries are comma-separated dimension lists")
        dims = tuple(d.strip() for d in spec.split(",") if d.strip())
        if not dims:
            continue
        require_self_or_aggregate(dims, None)
        unknown = [d for d in dims if d not in AGGREGATE_DIMS]
        if unknown:
            raise UsageError(f"unknown group-by dimension(s): {', '.join(unknown)} "
                             f"(allowed: {', '.join(AGGREGATE_DIMS)})")
        if len(set(dims)) != len(dims):
            raise UsageError("a group-by list names a dimension twice")
        if dims not in specs:
            specs.append(dims)
    return specs


def _billed_basis(pricer: Pricer) -> Basis:
    basis = getattr(pricer, "basis", Basis.LIST)
    return basis if basis in _BILLED_BASES else Basis.LIST


def _zero_total(pricer: Pricer) -> PricedTotal:
    return PricedTotal(exact=exact(0, _billed_basis(pricer)), estimated=None, allowance=None,
                       priced_inferences=0, unpriced_inferences=0, unpriced_tokens=0,
                       coverage="1", pool=None)


def _add_opt(a: Figure | None, b: Figure | None) -> Figure | None:
    if a is None:
        return b
    if b is None:
        return a
    return add(a, b)


def _window_total(store: LedgerStore, env: Env, since_ms: int, until_ms: int) -> PricedTotal:
    """The window's PricedTotal with ``allowance`` restricted to the subscription path and ``pool``
    = the Copilot paths' list-equivalent lines (addendum §21.4 WIRING row), whatever split the
    store's own total uses."""
    raw = store.aggregate(since_ms=since_ms, until_ms=until_ms, group_by=())
    total = raw.rows[0].priced if raw.rows else _zero_total(env.pricer)
    by_path = store.aggregate(since_ms=since_ms, until_ms=until_ms, group_by=("billing_path",))
    allowance: Figure | None = None
    pool: Figure | None = None
    for row in by_path.rows:
        path = dict(row.dims).get("billing_path")
        listed = _add_opt(row.priced.allowance, row.priced.pool)
        if path in COPILOT_BILLING_PATHS:
            pool = _add_opt(pool, listed)
        else:
            allowance = _add_opt(allowance, listed)
    return dataclasses.replace(total, allowance=allowance, pool=pool)


def _no_cache_fn() -> Callable[[Pricer, Inference, int], int] | None:
    try:
        return registry.load(NO_CACHE_EQUIVALENT)
    except ImportError:
        return None


def _ratio_text(num: int, den: int) -> str:
    """``num / den`` as a plain decimal string (28 significant digits); callers pass ``den > 0``."""
    value = ratio(num, den)
    if value is None:  # pragma: no cover - den > 0 at every call site
        raise ContractViolation("ratio of a zero denominator")
    return format(value.normalize(), "f")


class _WindowScan:
    """One pass over the window's requests: ESR sums and footnote flags."""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.no_cache = _no_cache_fn()
        self.exact = 0
        self.no_cache_total = 0
        self.trace_v1 = False
        self.placeholder = False

    def scan(self, store: LedgerStore, since_ms: int, until_ms: int) -> None:
        for req in store.iter_requests(since_ms=since_ms, until_ms=until_ms):
            if req.source is not None and req.source.adapter == _TRACE_V1:
                self.trace_v1 = True
            for att in req.attempts:
                for inf in att.inferences:
                    if inf.billable is False:
                        continue
                    if inf.usage_source is UsageSource.MESSAGE_START_ONLY:
                        self.placeholder = True
                    if self.no_cache is not None:
                        self._esr(inf, att.ts_start_ms)

    def _esr(self, inf: Inference, ts_ms: int) -> None:
        assert self.no_cache is not None
        priced = self.env.pricer.price_inference(inf, ts_ms=ts_ms)
        fig = priced.figure
        if fig.nano is None or fig.evidence is not Evidence.EXACT or fig.basis not in _BILLED_BASES:
            return
        self.exact += priced.exact_nano
        self.no_cache_total += int(self.no_cache(self.env.pricer, inf, ts_ms))

    def esr(self) -> str | None:
        if self.no_cache is None or self.no_cache_total <= 0:
            return None
        return _ratio_text(self.no_cache_total - self.exact, self.no_cache_total)


def _naive_usage(store: LedgerStore) -> dict[str, dict[str, int]]:
    if not isinstance(store, LedgerStats):
        return {}
    out: dict[str, dict[str, int]] = {}
    for key, value in store.source_stats().items():
        if not key.startswith(NAIVE_STAT_PREFIX) or type(value) is not int or value <= 0:
            continue
        model, _, bucket = key[len(NAIVE_STAT_PREFIX):].rpartition(":")
        if model and bucket in _NAIVE_BUCKETS:
            out.setdefault(model, {})[bucket] = out.get(model, {}).get(bucket, 0) + value
    return out


def _linear_nano(pricer: Pricer, ctx: PricingContext, ts_ms: int,
                 usage: Mapping[str, int]) -> int | None:
    """Σ bucket tokens × unit rate (no long-context band, which applies per request): the naive
    ratio compares two token sums at the same rates."""
    try:
        rates = pricer.unit_rates(ctx, ts_ms=ts_ms)
    except (PricingError, UsageError):
        return None
    if rates is None:
        return None
    return sum(rates.bucket_nano(_NAIVE_BUCKETS[name], tokens) for name, tokens in usage.items())


def _naive_ratio(store: LedgerStore, env: Env) -> str | None:
    """Claude Code naive line sum ÷ de-duplicated usage, both priced with the Env pricer at each
    model's latest Claude Code context (SPEC §5.3 #12); store-wide, like the source stats."""
    naive = _naive_usage(store)
    if not naive:
        return None
    dedup: dict[str, dict[str, int]] = {}
    latest: dict[str, tuple[int, PricingContext]] = {}
    for req in store.iter_requests():
        if req.source is None or req.source.adapter != _CLAUDE_CODE:
            continue
        for att in req.attempts:
            for inf in att.inferences:
                model = inf.pricing.model
                if inf.billable is False or model not in naive:
                    continue
                sums = dedup.setdefault(model, {})
                for name in _NAIVE_BUCKETS:
                    sums[name] = sums.get(name, 0) + getattr(inf.usage, name)
                prev = latest.get(model)
                if prev is None or att.ts_start_ms >= prev[0]:
                    latest[model] = (att.ts_start_ms, inf.pricing)
    num = den = 0
    for model in sorted(naive):
        if model not in latest:
            continue
        ts_ms, ctx = latest[model]
        a = _linear_nano(env.pricer, ctx, ts_ms, naive[model])
        b = _linear_nano(env.pricer, ctx, ts_ms, dedup[model])
        if a is None or b is None:
            continue
        num += a
        den += b
    if den <= 0:
        return None
    return _ratio_text(num, den)


def bill_summary(store: LedgerStore, env: Env, *, since_ms: int, until_ms: int,
                 group_by: Sequence[str]) -> BillSummary:
    """The ``BillSummary`` of ``[since_ms, until_ms)`` (SPEC §15, §14.1).

    * ``total`` — the store's priced total; ``allowance`` holds only subscription list-equivalent
      lines and ``pool`` the Copilot paths' (never mixed into ``exact``);
    * ``esr`` — 1 − exact ÷ no-cache equivalent (``rates.engine.no_cache_equivalent_nano``) over
      billable inferences priced EXACT on a billed basis with the Env pricer; None when the engine
      is not installed or nothing qualifies;
    * ``breakdowns`` — one k-anonymous ``PublishedAggregate`` (``core.kanon.publish``, ``k =
      env.k``) per *group_by* entry, each a comma-separated list of :data:`AGGREGATE_DIMS` (key =
      the list joined by commas); person dimensions raise ``PrivacyError``, unknown ones
      ``UsageError``;
    * ``naive_ratio`` — Claude Code naive line sum ÷ de-duplicated usage priced with the Env pricer
      (None without Claude Code naive stats or a ``LedgerStats`` store);
    * ``footnotes`` — trace@1 write pricing, placeholder outputs, unpriced coverage.
    """
    if type(since_ms) is not int or type(until_ms) is not int or since_ms > until_ms:
        raise UsageError("bill window must be int ms with since <= until")
    specs = _breakdown_specs(group_by)
    total = _window_total(store, env, since_ms, until_ms)
    breakdowns: list[tuple[str, PublishedAggregate]] = []
    for dims in specs:
        raw = store.aggregate(since_ms=since_ms, until_ms=until_ms, group_by=dims)
        breakdowns.append((",".join(dims), publish(raw, k=env.k)))
    scan = _WindowScan(env)
    scan.scan(store, since_ms, until_ms)
    footnotes = []
    if scan.trace_v1:
        footnotes.append(FOOTNOTE_TRACE_V1)
    if scan.placeholder:
        footnotes.append(FOOTNOTE_PLACEHOLDER)
    if total.unpriced_inferences:
        footnotes.append(FOOTNOTE_UNPRICED)
    return BillSummary(total=total, esr=scan.esr(), breakdowns=tuple(breakdowns),
                       naive_ratio=_naive_ratio(store, env), footnotes=tuple(footnotes))


# =============================================================================================
# org median compaction summary size (R-E40, R-E24)
# =============================================================================================


def compaction_post_tokens(lanes: Iterable[Lane]) -> list[int]:
    """The ``post_tokens`` of every COMPACTION event of *lanes* (events without it are skipped)."""
    out: list[int] = []
    for lane in lanes:
        for event in lane.events:
            if event.kind is not LaneEventKind.COMPACTION:
                continue
            value = dict(event.attrs).get("post_tokens")
            if type(value) is int and value >= 0:
                out.append(value)
    return out


def median_tokens(values: Iterable[int]) -> int | None:
    """The median of *values* in whole tokens (even counts: the mean of the two middle values,
    rounded half-even); None when empty. Independent of the input order."""
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    if n % 2:
        return ordered[n // 2]
    q, r = divmod(ordered[n // 2 - 1] + ordered[n // 2], 2)
    if r and q % 2:
        q += 1
    return q


def org_compaction_median(store: LedgerStore, *, since_ms: int, until_ms: int) -> int | None:
    """The org median COMPACTION summary size (``post_tokens``) over the window's lanes, computed
    once per run from the whole store (ruling R-E40), so sharded and unsharded runs agree."""
    return median_tokens(compaction_post_tokens(store.iter_lanes(since_ms=since_ms,
                                                                 until_ms=until_ms)))


def analysis_thresholds(env: Env, store: LedgerStore, *, since_ms: int,
                        until_ms: int) -> dict[str, str]:
    """``AnalysisContext.thresholds`` for a run: the Config's detector overrides plus
    :data:`COMPACTION_POST_TOKENS_KEY` = the org median (R-E40) when the store has COMPACTION events
    and the Config does not set the key itself. Sorted by key."""
    thresholds = dict(env.config.thresholds)
    if COMPACTION_POST_TOKENS_KEY not in thresholds:
        median = org_compaction_median(store, since_ms=since_ms, until_ms=until_ms)
        if median is not None:
            thresholds[COMPACTION_POST_TOKENS_KEY] = str(median)
    return dict(sorted(thresholds.items()))


def with_compaction_post(policy: Policy, post_tokens: int | None) -> Policy:
    """*policy* with the org median passed as ``post=`` of its ``compact-window`` clause (R-E24):
    a clause without a summary size gets *post_tokens*; an explicit ``post=`` or a policy without
    the clause is returned unchanged, as is everything when *post_tokens* is None."""
    window = policy.compaction_window
    if post_tokens is None or window is None or window[1] is not None:
        return policy
    if type(post_tokens) is not int or post_tokens < 0:
        raise UsageError("post_tokens must be a non-negative int")
    return dataclasses.replace(policy, compaction_window=(window[0], post_tokens))

