"""Ledger pipeline of the CLI (SPEC §15 "Pipeline", §5.4, §5.12, §12, §14, D19; CLI-LEDGER).

Pure functions behind the ledger verbs — ``init``, ``collect claude-code``, ``collect
claude-code-headless``, ``ingest``, ``bill``, ``reconcile``, ``export``, ``showback``, ``pricing``,
``purge`` — and the v2 engine of ``analyze``. Nothing here prints or parses argv: every function
takes explicit paths, windows and an :class:`~tokenbill.pipeline.common.Env`, so tests call them
without argparse, and ``tokenbill.commands.*`` only translate flags and render results.

Shared plumbing comes from :mod:`tokenbill.pipeline.common` (WIRING); the wave-2 packages are
reached through their documented APIs, imported lazily inside the functions that need them (so a
missing optional package fails only its own verb).

Decisions where the SPEC is silent (also listed in ``tests/v2/cli_ledger/README.md``):

* **Windows.** ``--since`` / ``--until`` are UTC dates; the window is ``[since 00:00, until 00:00)``
  (end exclusive, as the terminal header says). Without ``--since`` the window starts at the epoch;
  without ``--until`` it ends at the start of the day after ``env.now_ms`` (all data up to today).
* **Collector files.** One trace@2 ``usage`` file per run, ``<out>/tb-<host>-<created_ms>.jsonl.gz``
  (``<host>`` = 12 hex of the name-key HMAC of the host name, random when there is no name key);
  nothing is written when a run found no new records. The collector state is saved after the file.
* **reconcile** ingests the provider files it is given into the ledger (aggregates and cost lines
  merge idempotently, SPEC §7.3), then reconciles the window from the stored data: RECON for the
  core channels plus every extension reconciler (``core.extensions.run_reconcilers``), combined with
  RECON's ``merge_reports``. An extension report without rows, decisions or any channel beyond
  ``insufficient_data`` (a Claude-only ledger) is left out, so Claude-only fleets see no Copilot
  badges. Reconciliation results are not persisted (C-17): ``export focus`` re-runs the same
  reconciliation over its window to decide the per-channel ``BilledCost`` rule (D19).
* **ingest** applies allocation rules (``finops.allocation.apply_rules``) to every request before
  the ledger ingest and counts records per source for the result's ``inputs``.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import io
import json
import logging
import os
import re
import secrets
import socket
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote

from tokenbill.core import extensions, keys, registry
from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.ids import is_opaque_ref, key_id, pseudonym, stable_id
from tokenbill.core.jsonl import acl_warning, open_private
from tokenbill.core.labels import Basis
from tokenbill.core.records import (
    EXTRA_KEYS,
    Attribution,
    ContentTier,
    Lane,
    Request,
    Session,
    WorkloadClass,
)
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    PricingReport,
    PrivacyInfo,
    RateCardInfo,
    ReconciliationReport,
    RunResult,
    SourceInfo,
)
from tokenbill.pipeline.common import (
    Env,
    bill_summary,
    ingest_options,
    ingest_paths,
    open_store,
)

__all__ = [
    "ATTR_KEYS",
    "COLLECTOR_IDENTITY_MODES",
    "DAY_MS",
    "EXPORT_FORMATS",
    "FOCUS_GROUP_BY",
    "LIVE_SOURCES",
    "PRIVACY_NOTICE",
    "RECON_SOURCES",
    "CollectResult",
    "InitResult",
    "LivePull",
    "attribution_from",
    "ci_attribution",
    "collector_options",
    "date_start_ms",
    "day_of",
    "merge_sessions",
    "parse_attr_pairs",
    "parse_date",
    "parse_pct",
    "parse_resource_attributes",
    "privacy_info",
    "rate_card_info",
    "reconcile_store",
    "resolve_principal_ref",
    "resolve_window",
    "run_analyze_v2",
    "run_bill",
    "run_collect",
    "run_collect_headless",
    "run_emit_model_pricing",
    "run_export",
    "run_ingest",
    "run_init",
    "run_pricing",
    "run_purge",
    "run_reconcile",
    "run_showback",
]

logger = logging.getLogger("tokenbill.pipeline.ledger")

DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_PCT_RE = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?\Z")
_P_RE = re.compile(r"p_[0-9a-f]{20}\Z")
_ATTR_VALUE_MAX = 128
_REF_FILE_MAX = 4096
_FAR_MS = 253_402_300_800_000  # 10000-01-01T00:00Z: every ledger timestamp is before it

#: ``--attr`` / ``OTEL_RESOURCE_ATTRIBUTES`` keys → ``Attribution`` fields (``extra.<key>`` for the
#: allowlisted ``EXTRA_KEYS``). ``team.id`` / ``tokenbill.arm`` / ``tokenbill.wave`` are the
#: resource-attribute spellings of SPEC §5.4.
ATTR_KEYS: Mapping[str, str] = {
    "team": "team", "team.id": "team", "cost_center": "cost_center", "project": "project",
    "workload": "workload_class", "workload_class": "workload_class",
    "arm": "arm", "tokenbill.arm": "arm", "wave": "wave", "tokenbill.wave": "wave",
    "agent_type": "agent_type", "agent_product": "agent_product", "entrypoint": "entrypoint",
    "billing_path": "billing_path",
    **{key: f"extra.{key}" for key in EXTRA_KEYS},
}
#: Identity modes of the on-device / CI collectors (SPEC §5.4).
COLLECTOR_IDENTITY_MODES = ("central", "two-stage")
#: ``reconcile`` file flags (argparse dest) → the ADMIN / cloud-billing adapter that reads them.
RECON_SOURCES: Mapping[str, str] = {
    "usage_report": "anthropic-usage-report",
    "cost_report": "anthropic-cost-report",
    "cc_analytics": "anthropic-cc-analytics",
    "enterprise_analytics": "anthropic-enterprise-analytics",
    "openai_usage": "openai-usage-buckets",
    "openai_costs": "openai-costs",
    "aws_cur": "aws-cur",
    "gcp_billing": "gcp-billing",
}
#: ``reconcile --live`` endpoints (``recon.pull`` kinds) and their adapters: the usage/cost pair.
LIVE_SOURCES: tuple[tuple[str, str], ...] = (
    ("usage_report", "anthropic-usage-report"),
    ("cost_report", "anthropic-cost-report"),
)
#: ``export --format`` values.
EXPORT_FORMATS = ("focus", "trace2", "ccusage")
#: ``cost_rows`` grain of the FOCUS export: every ``LedgerCostRow`` dimension (SPEC §14.4).
FOCUS_GROUP_BY = ("date", "provider", "channel", "model", "team", "cost_center", "project",
                  "workspace_id", "lane_kind", "workload_class", "agent_product", "billing_path")
_ROUNDING_STAT = "rounding_remainder_e18"

#: Printed by ``tokenbill init`` (SPEC §8).
PRIVACY_NOTICE = """\
Privacy notice (Token Bill)
  * Collection is content-free: token counts, timings, model ids and hashed names only — never
    prompts, completions, tool inputs or file contents (content tier "none").
  * People are pseudonymized: collectors ship an opaque device/employee reference (r_) or a
    collection-key HMAC (c_); the central store keeps only p_ pseudonyms under the org key, which
    never leaves this host. Repositories, workspaces, MCP servers and skills are HMAC'd (h_).
  * Every published aggregate is k-anonymous (k = {k}); no command lists or ranks people, and
    individuals see only their own data (--self, `tokenbill me`).
  * Nothing is sent anywhere: every command is offline unless you pass --live.
  * Erasure: `tokenbill purge --db LEDGER --principal p_… --yes` (audited)."""


# =================================================================================================
# small parsers (pure; fuzzed in tests/v2/cli_ledger/test_parsers.py)
# =================================================================================================


def parse_date(text: object, what: str = "date") -> str:
    """A UTC date ``YYYY-MM-DD`` (validated, normalized); anything else raises ``UsageError``
    naming *what*, never echoing the value."""
    if not isinstance(text, str) or not _DATE_RE.match(text.strip()):
        raise UsageError(f"{what} must be a date YYYY-MM-DD")
    try:
        return _dt.date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        raise UsageError(f"{what} is not a valid calendar date") from None


def date_start_ms(date: str) -> int:
    """Epoch milliseconds of ``date`` 00:00 UTC."""
    return (_dt.date.fromisoformat(date) - _EPOCH).days * DAY_MS


def day_of(ms: int) -> str:
    """The UTC date of epoch milliseconds *ms*."""
    return (_EPOCH + _dt.timedelta(days=ms // DAY_MS)).isoformat()


def resolve_window(since: str | None, until: str | None, *, now_ms: int) -> tuple[int, int]:
    """``[since, until)`` in epoch ms from ``--since`` / ``--until`` dates (end exclusive).
    Defaults: the epoch, and the start of the day after *now_ms*. An empty window raises
    ``UsageError``."""
    lo = date_start_ms(parse_date(since, "--since")) if since is not None else 0
    hi = (date_start_ms(parse_date(until, "--until")) if until is not None
          else (now_ms // DAY_MS + 1) * DAY_MS)
    if hi <= lo:
        raise UsageError("--until must be after --since (the end date is exclusive)")
    return lo, hi


def parse_pct(text: object, what: str) -> str:
    """A percentage 0–999.999999 as a decimal string (``--tolerance-pct 0.5``)."""
    if isinstance(text, (int, Decimal)) and not isinstance(text, bool):
        text = str(text)
    if not isinstance(text, str) or not _PCT_RE.match(text.strip()):
        raise UsageError(f"{what} must be a non-negative decimal percentage (e.g. 0.5)")
    return text.strip()


def _attr_value(key: str, value: str) -> str:
    value = value.strip()
    if (not value or len(value) > _ATTR_VALUE_MAX
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise UsageError(f"attribute {key}: value must be 1–{_ATTR_VALUE_MAX} printable characters")
    return value


def parse_attr_pairs(pairs: Iterable[str]) -> dict[str, str]:
    """``--attr K=V`` values → ``{Attribution field (or extra.<key>): value}``. Keys must be in
    :data:`ATTR_KEYS`; a later pair wins. Malformed pairs raise ``UsageError`` (key named, value
    never echoed)."""
    out: dict[str, str] = {}
    for pair in pairs:
        if not isinstance(pair, str):
            raise UsageError("--attr expects K=V")
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise UsageError("--attr expects K=V")
        target = ATTR_KEYS.get(key)
        if target is None:
            shown = key[:32] if key.isprintable() else "(unprintable)"
            raise UsageError(f"--attr: unknown key {shown!r} (allowed: "
                             f"{', '.join(sorted(ATTR_KEYS))})")
        out[target] = _attr_value(key, value)
    return out


def parse_resource_attributes(text: str | None) -> dict[str, str]:
    """``OTEL_RESOURCE_ATTRIBUTES`` (``k=v,k=v``, values percent-encoded) → the recognized
    attribution values; unknown keys and malformed entries are ignored (other tools share the
    variable)."""
    out: dict[str, str] = {}
    if not text:
        return out
    for item in text.split(","):
        key, sep, value = item.partition("=")
        target = ATTR_KEYS.get(key.strip())
        if not sep or target is None:
            continue
        try:
            out[target] = _attr_value(key.strip(), unquote(value, errors="strict"))
        except (UsageError, UnicodeDecodeError):
            logger.debug("OTEL_RESOURCE_ATTRIBUTES: ignoring a malformed %s value", target)
    return out


def attribution_from(values: Mapping[str, str], base: Attribution | None = None) -> Attribution:
    """Apply parsed attribute values (:func:`parse_attr_pairs`) to *base* (default: empty); an
    invalid value (e.g. an unknown workload class or billing path) raises ``UsageError``."""
    attr = base if base is not None else Attribution()
    changes: dict[str, Any] = {}
    extra = dict(attr.extra)
    for field, value in values.items():
        if field.startswith("extra."):
            extra[field[len("extra."):]] = value
        elif field == "workload_class":
            try:
                changes[field] = WorkloadClass(value)
            except ValueError:
                raise UsageError("workload must be one of "
                                 f"{', '.join(w.value for w in WorkloadClass)}") from None
        else:
            changes[field] = value
    if extra != dict(attr.extra):
        changes["extra"] = tuple(sorted(extra.items()))
    if not changes:
        return attr
    try:
        return dataclasses.replace(attr, **changes)
    except (ContractViolation, ValueError, TypeError) as exc:
        raise UsageError(f"invalid attribution: {exc}") from None


def resolve_principal_ref(spec: str | None, environ: Mapping[str, str]) -> str | None:
    """``--principal-ref env:VAR | mdm-file:PATH | none`` → the opaque reference (or None). The
    reference must match ``[A-Za-z0-9._-]{1,64}`` and never be an email (SPEC §5.4); it is never
    echoed in errors."""
    if spec is None or spec.strip() == "none":
        return None
    kind, sep, arg = spec.strip().partition(":")
    if not sep or not arg:
        raise UsageError("--principal-ref must be env:VAR, mdm-file:PATH or none")
    if kind == "env":
        value = environ.get(arg)
        if not value:
            raise UsageError("--principal-ref env:VAR: the variable is not set")
    elif kind == "mdm-file":
        path = Path(arg).expanduser()
        try:
            with open(path, "rb") as fh:
                raw = fh.read(_REF_FILE_MAX + 1)
        except OSError:
            raise UsageError(f"--principal-ref: cannot read {path.name}") from None
        if len(raw) > _REF_FILE_MAX:
            raise UsageError(f"--principal-ref: {path.name} is too large for a reference")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise UsageError(f"--principal-ref: {path.name} is not UTF-8 text") from None
    else:
        raise UsageError("--principal-ref must be env:VAR, mdm-file:PATH or none")
    if not is_opaque_ref(value):
        raise UsageError("principal ref must match [A-Za-z0-9._-]{1,64} (never an email)")
    return value


# =================================================================================================
# result helpers
# =================================================================================================


def privacy_info(env: Env, *, content_tier: ContentTier = ContentTier.NONE,
                 identity_mode: str | None = None, suppressed_groups: int = 0) -> PrivacyInfo:
    """The ``privacy`` block of a result: the org key id (never the key), the identity mode
    (default ``central-ingest`` with an org key, else ``install``), k and the merged groups."""
    mode = identity_mode or ("central-ingest" if env.org_key is not None else "install")
    return PrivacyInfo(content_tier=content_tier,
                       key_id=key_id(env.org_key) if env.org_key is not None else None,
                       identity_mode=mode, k=env.k, suppressed_groups=suppressed_groups)


def rate_card_info(pricer: object, today: str) -> RateCardInfo:
    """``RateCard.info(today=…)`` when the pricer has it; else a minimal block from its hash and
    basis (fakes)."""
    info = getattr(pricer, "info", None)
    if callable(info):
        return info(today=today)
    sha = getattr(pricer, "rate_card_sha256", None) or getattr(pricer, "sha256", "") or ""
    basis = getattr(pricer, "basis", Basis.LIST)
    return RateCardInfo(sha256=str(sha), layers=(), stale_rows=(), contract=None,
                        basis=basis if isinstance(basis, Basis) else Basis.LIST)


def _pricer_notes(pricer: object, today: str) -> list[DataQualityNote]:
    dq = getattr(pricer, "data_quality", None)
    return list(dq(today=today)) if callable(dq) else []


def _today(env: Env) -> str:
    return day_of(env.now_ms)


def _close(obj: object) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        close()


def _check_sources(paths: Sequence[Path]) -> list[Path]:
    out = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise UsageError(f"{path.name}: no such file or directory")
        out.append(path)
    return out


def _dedupe_notes(notes: Iterable[DataQualityNote]) -> tuple[DataQualityNote, ...]:
    seen: set[DataQualityNote] = set()
    out = []
    for note in notes:
        if note not in seen:
            seen.add(note)
            out.append(note)
    return tuple(out)


class _IngestTap:
    """A ledger proxy for ``ingest_paths``: applies allocation rules to each result's requests
    before the ledger ingest and counts records and quarantined lines per source."""

    def __init__(self, store: Any, rules: object | None = None) -> None:
        self._store = store
        self._rules = rules
        self._counts: dict[str, list[Any]] = {}
        self.path = getattr(store, "path", None)

    def ingest(self, result: IngestResult, *, pricer: object = None) -> Any:
        if self._rules is not None:
            _allocate(result, self._rules)
        seen = {r.request_id for r in result.requests}
        lane_requests = sum(1 for s in result.sessions for lane in s.lanes for r in lane.requests
                            if r.request_id not in seen)
        records = (len(result.requests) + lane_requests + len(result.aggregates)
                   + len(result.cost_lines) + len(result.outcomes) + len(result.licenses)
                   + len(result.activity) + len(result.config))
        entry = self._counts.setdefault(result.source.source_id, [result.source, 0, 0])
        entry[1] += records
        entry[2] += len(result.quarantined)
        return self._store.ingest(result, pricer=pricer)

    def inputs(self) -> tuple[tuple[SourceInfo, int, int], ...]:
        return tuple((src, n, q) for src, n, q in self._counts.values())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _allocate(result: IngestResult, rules: object) -> None:
    from tokenbill.finops.allocation import apply_rules

    def fix(req: Request) -> Request:
        attr = apply_rules(req, rules)  # type: ignore[arg-type]
        return req if attr is req.attribution else dataclasses.replace(req, attribution=attr)

    result.requests = [fix(r) for r in result.requests]
    sessions = []
    for s in result.sessions:
        lanes = tuple(dataclasses.replace(lane, requests=tuple(fix(r) for r in lane.requests))
                      if lane.requests else lane for lane in s.lanes)
        sessions.append(dataclasses.replace(s, lanes=lanes) if lanes != s.lanes else s)
    result.sessions = sessions


# =================================================================================================
# init
# =================================================================================================


@dataclass(frozen=True)
class InitResult:
    """What ``tokenbill init`` created (paths only; never key material)."""

    directory: Path
    config_path: Path
    key_paths: tuple[Path, ...]
    store_dir: Path | None
    identity_mode: str
    collector: bool
    notes: tuple[DataQualityNote, ...] = ()


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def run_init(directory: Path, *, identity_mode: str = "central", k: int = 5,
             collector: bool = False, force: bool = False) -> InitResult:
    """Create the Token Bill directory (``0700``): ``config.json`` and, on the central host, the
    org key (``org.key``, ``0600``), the collection key for ``two-stage`` (``collection.key``) and
    the store directory (``store/``, ``0700``). ``collector=True`` writes only the collector
    config (identity mode and k; collectors receive the collection key through MDM). An existing
    config is kept unless *force*; existing keys are never replaced."""
    from tokenbill.config import IDENTITY_MODES, Config, config_json

    if identity_mode not in IDENTITY_MODES:
        raise UsageError(f"--identity-mode must be one of {', '.join(IDENTITY_MODES)}")
    directory = Path(directory).expanduser()
    config_path = directory / "config.json"
    if config_path.exists() and not force:
        raise UsageError(f"{config_path.name} already exists in {directory.name} "
                         "(pass --force to overwrite it; keys are never replaced)")
    _private_dir(directory)
    notes: list[DataQualityNote] = []
    key_paths: list[Path] = []
    store_dir: Path | None = None
    if collector:
        config = Config(k=k, identity_mode=identity_mode)
    else:
        org = directory / "org.key"
        keys.load_or_create(org, notes=notes)
        key_paths.append(org)
        collection_name = None
        if identity_mode == "two-stage":
            coll = directory / "collection.key"
            keys.load_or_create(coll, notes=notes)
            key_paths.append(coll)
            collection_name = coll.name
        store_dir = directory / "store"
        _private_dir(store_dir)
        config = Config(k=k, identity_mode=identity_mode, key_file=org.name,
                        collection_key_file=collection_name)
    text = json.dumps(config_json(config), indent=2, sort_keys=False) + "\n"
    with open_private(config_path, "w") as fh:
        fh.write(text)
        warning = acl_warning(fh)
    if warning:
        notes.append(DataQualityNote(code=warning, severity="warn", count=1,
                                     detail="owner-only ACL not enforced on the config file"))
    return InitResult(directory=directory, config_path=config_path, key_paths=tuple(key_paths),
                      store_dir=store_dir, identity_mode=identity_mode, collector=collector,
                      notes=_dedupe_notes(notes))


# =================================================================================================
# collectors
# =================================================================================================


@dataclass(frozen=True)
class CollectResult:
    """A collector run: the trace@2 file written (None when nothing new), its record counts and
    the data-quality notes (retention warning, ACL warning, adapter notes)."""

    path: Path | None
    requests: int
    sessions: int
    notes: tuple[DataQualityNote, ...]


def collector_options(*, identity_mode: str = "central", principal_ref: str | None = None,
                      collection_key: bytes | None = None,
                      attribution: Attribution | None = None, now_ms: int = 0,
                      since_ms: int | None = None, content: str = "none") -> IngestOptions:
    """``IngestOptions`` of the on-device / CI collectors (SPEC §5.4): content tier ``none`` only
    (``--content full`` or ``fingerprint`` → ``PrivacyError``); ``central`` ships ``r_<ref>``;
    ``two-stage`` HMACs the reference with the collection key (``c_``, required). The collection
    key is the name key in both modes."""
    if content != ContentTier.NONE.value:
        raise PrivacyError("collect writes content tier none only (no prompts, no fingerprints)")
    if identity_mode not in COLLECTOR_IDENTITY_MODES:
        raise UsageError(f"--identity-mode must be one of {', '.join(COLLECTOR_IDENTITY_MODES)}")
    if identity_mode == "two-stage" and not collection_key:
        raise UsageError("identity mode two-stage needs --collection-key-file")
    kid = key_id(collection_key) if collection_key else ""
    two = identity_mode == "two-stage"
    return IngestOptions(content_tier=ContentTier.NONE, identity_mode=identity_mode,
                         name_key=collection_key or b"", name_key_id=kid,
                         principal_key=collection_key if two else None,
                         principal_key_id=kid if two else None, principal_ref=principal_ref,
                         attribution=attribution or Attribution(), since_ms=since_ms,
                         now_ms=now_ms)


def ci_attribution(environ: Mapping[str, str], name_key: bytes | None,
                   base: Attribution | None = None
                   ) -> tuple[Attribution, list[DataQualityNote]]:
    """CI attribution from the ``GITHUB_*`` environment (SPEC §5.12 rule 6): ``GITHUB_ACTIONS=true``
    → workload ``ci`` and entrypoint ``claude-code-github-action`` (unless set); ``repo`` =
    ``h_(GITHUB_REPOSITORY)`` and ``extra.workflow`` = ``h_(GITHUB_WORKFLOW)`` with the name key;
    ``extra.run_attempt`` = ``GITHUB_RUN_ATTEMPT`` (digits). Without a name key the repository and
    workflow names are dropped (never shipped in clear) with ``dq.ci_names_unhashed``."""
    attr = base if base is not None else Attribution()
    notes: list[DataQualityNote] = []
    changes: dict[str, Any] = {}
    extra = dict(attr.extra)
    if environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        if attr.workload_class is WorkloadClass.UNKNOWN:
            changes["workload_class"] = WorkloadClass.CI
        if attr.entrypoint is None:
            changes["entrypoint"] = "claude-code-github-action"
    dropped = 0
    for var, target in (("GITHUB_REPOSITORY", "repo"), ("GITHUB_WORKFLOW", "workflow")):
        value = (environ.get(var) or "").strip()
        if not value:
            continue
        if not name_key:
            dropped += 1
            continue
        hashed = pseudonym(name_key, "h", value)
        if target == "repo":
            changes["repo"] = hashed
        else:
            extra["workflow"] = hashed
    attempt = (environ.get("GITHUB_RUN_ATTEMPT") or "").strip()
    if attempt.isdigit() and len(attempt) <= 9 and attempt.isascii():
        extra["run_attempt"] = attempt
    if dropped:
        notes.append(DataQualityNote(
            code="dq.ci_names_unhashed", severity="warn", count=dropped,
            detail="GITHUB_REPOSITORY / GITHUB_WORKFLOW dropped: no collection (name) key to "
                   "HMAC them (--collection-key-file)"))
    if extra != dict(attr.extra):
        changes["extra"] = tuple(sorted(extra.items()))
    return (dataclasses.replace(attr, **changes) if changes else attr), notes


def _host_tag(name_key: bytes | None) -> str:
    if name_key:
        return pseudonym(name_key, "h", socket.gethostname() or "localhost")[2:14]
    return secrets.token_hex(6)


def _acl_probe(out_dir: Path) -> DataQualityNote | None:
    """On Windows, whether an owner-only ACL can be applied in *out_dir* (D44)."""
    if os.name != "nt":  # pragma: no cover - exercised on Windows runners
        return None
    probe = out_dir / f".tb-acl-probe-{secrets.token_hex(4)}"  # pragma: no cover
    with open_private(probe, "w") as fh:  # pragma: no cover
        warning = acl_warning(fh)
    probe.unlink()  # pragma: no cover
    if warning:  # pragma: no cover
        return DataQualityNote(code=warning, severity="warn", count=1,
                               detail="collector output: owner-only ACL not enforced; write "
                                      "into an MDM-restricted directory (D44)")
    return None  # pragma: no cover


def merge_sessions(sessions: Iterable[Session]) -> list[Session]:
    """One ``Session`` per ``session_key`` (a trace@2 file holds each session once): lanes are
    unioned (a lane seen twice merges its requests and events), the window widened; the first
    session's source kind and attribution are kept. First-seen order."""
    merged: dict[str, Session] = {}
    lanes: dict[str, dict[str, Lane]] = {}
    for s in sessions:
        prior = merged.get(s.session_key)
        if prior is None:
            merged[s.session_key] = s
        else:
            merged[s.session_key] = dataclasses.replace(
                prior, started_ms=min(prior.started_ms, s.started_ms),
                ended_ms=max(prior.ended_ms, s.ended_ms))
        known = lanes.setdefault(s.session_key, {})
        for lane in s.lanes:
            old = known.get(lane.lane_key)
            if old is None:
                known[lane.lane_key] = lane
                continue
            reqs = {r.request_id: r for r in (*old.requests, *lane.requests)}
            events = tuple(dict.fromkeys((*old.events, *lane.events)))
            known[lane.lane_key] = dataclasses.replace(old, requests=tuple(reqs.values()),
                                                       events=events)
    return [dataclasses.replace(s, lanes=tuple(lanes[key].values()))
            for key, s in merged.items()]


def _write_collected(results: Sequence[IngestResult], out_dir: Path, opts: IngestOptions, *,
                     created_ms: int, adapter: str, extra_notes: Sequence[DataQualityNote] = ()
                     ) -> CollectResult:
    from tokenbill import __version__
    from tokenbill.adapters.trace_v2 import write_trace_v2

    sessions: list[Session] = []
    requests: list[Request] = []
    events: list[Any] = []
    aggregates: list[Any] = []
    cost_lines: list[Any] = []
    outcomes: list[Any] = []
    notes: list[DataQualityNote] = list(extra_notes)
    name_key_id: str | None = None
    principal_key_id: str | None = None
    for r in results:
        sessions.extend(r.sessions)
        requests.extend(r.requests)
        events.extend(r.events)
        aggregates.extend(r.aggregates)
        cost_lines.extend(r.cost_lines)
        outcomes.extend(r.outcomes)
        notes.extend(r.notes)
        name_key_id = name_key_id or r.source.name_key_id
        principal_key_id = principal_key_id or r.source.principal_key_id
    sessions = merge_sessions(sessions)
    lane_ids = {rq.request_id for s in sessions for lane in s.lanes for rq in lane.requests}
    n_requests = len(lane_ids | {rq.request_id for rq in requests})
    if not (n_requests or events or aggregates or cost_lines or outcomes):
        return CollectResult(path=None, requests=0, sessions=0, notes=_dedupe_notes(notes))
    out_dir = Path(out_dir).expanduser()
    host = _host_tag(opts.name_key or None)
    path = out_dir / f"tb-{host}-{created_ms}.jsonl.gz"
    n = 1
    while path.exists():
        path = out_dir / f"tb-{host}-{created_ms}-{n}.jsonl.gz"
        n += 1
    header = {"trace_id": stable_id("t", adapter, host, created_ms), "profile": "usage",
              "identity_mode": opts.identity_mode,
              "name_key_id": name_key_id or (opts.name_key_id or None),
              "principal_key_id": principal_key_id, "created_ms": created_ms,
              "producer": {"name": "tokenbill", "version": __version__, "adapter": adapter},
              "attribution": opts.attribution}
    write_trace_v2(path, header=header, sessions=sessions, requests=requests, events=events,
                   aggregates=aggregates, cost_lines=cost_lines, outcomes=outcomes,
                   notes=_dedupe_notes(notes))
    return CollectResult(path=path, requests=n_requests, sessions=len(sessions),
                         notes=_dedupe_notes(notes))


def run_collect(projects: Path, out_dir: Path, opts: IngestOptions, *, state_path: Path,
                now_ms: int, final: bool = False) -> CollectResult:
    """``collect claude-code`` (SPEC §5.3, §5.4): CC's ``collect_incremental`` over *projects*
    with the private collector state at *state_path* → one trace@2 ``usage`` file in *out_dir*
    holding only the new, closed records since the last run. Never opens a socket."""
    from tokenbill.adapters.cc_collect import CollectorState, collect_incremental

    projects = Path(projects).expanduser()
    if not projects.exists():
        raise UsageError(f"projects directory {projects.name} not found (--projects)")
    out_dir = Path(out_dir).expanduser()
    _private_dir(out_dir)
    extra = [n for n in (_acl_probe(out_dir),) if n is not None]
    state_path = Path(state_path).expanduser()
    state = CollectorState.load(state_path)
    results = list(collect_incremental(projects, state, opts, now_ms=now_ms, final=final))
    outcome = _write_collected(results, out_dir, opts, created_ms=now_ms, adapter="claude-code",
                               extra_notes=extra)
    state.save(state_path)  # after the file: a crash in between re-collects (the merge dedupes)
    return outcome


def run_collect_headless(inputs: Sequence[Path], out_dir: Path, opts: IngestOptions, *,
                         now_ms: int) -> CollectResult:
    """``collect claude-code-headless`` (SPEC §5.12): CC's headless adapter on execution files,
    ``stream-json`` / ``json`` outputs or Agent SDK logs → one trace@2 ``usage`` file. The CI
    attribution is in ``opts.attribution`` (see :func:`ci_attribution`)."""
    from tokenbill.adapters.cc_headless import ClaudeCodeHeadlessAdapter

    paths = _check_sources(inputs)
    if not paths:
        raise UsageError("collect claude-code-headless needs --in FILE")
    out_dir = Path(out_dir).expanduser()
    _private_dir(out_dir)
    extra = [n for n in (_acl_probe(out_dir),) if n is not None]
    adapter = ClaudeCodeHeadlessAdapter()
    if opts.now_ms != now_ms:
        opts = dataclasses.replace(opts, now_ms=now_ms)
    results = [adapter.read(p, opts) for p in paths]
    return _write_collected(results, out_dir, opts, created_ms=now_ms,
                            adapter="claude-code-headless", extra_notes=extra)


# =================================================================================================
# ingest
# =================================================================================================


def _content_tier(content: str, *, allow_fingerprint: bool = True) -> ContentTier:
    try:
        tier = ContentTier(content)
    except ValueError:
        raise UsageError("--content must be none or fingerprint") from None
    if tier is ContentTier.FULL:
        raise PrivacyError("content tier full is local only: the ledger and exports never hold "
                           "content")
    if tier is ContentTier.FINGERPRINT and not allow_fingerprint:
        raise PrivacyError("this command writes content tier none only")
    return tier


def run_ingest(store_path: Path, sources: Sequence[Path], env: Env, *, adapter: str = "auto",
               content: str = "none", rules: object | None = None,
               team_map: tuple[tuple[str, str], ...] = (),
               attribution: Attribution | None = None, since_ms: int | None = None,
               until_ms: int | None = None, strict: bool = False,
               renormalize: bool = False, experimental: frozenset[str] = frozenset()
               ) -> RunResult:
    """``ingest SOURCE…``: read every source with its (sniffed or named) adapter through
    ``pipeline.common.ingest_paths`` into the ledger at *store_path* (created when missing), with
    the allocation *rules* applied to each request, the team map, the Config's k and name allowlist,
    and the org / collection keys of *env*. Returns the sources read (records and quarantined
    counts) and the merged data-quality notes."""
    tier = _content_tier(content)
    paths = _check_sources(sources)
    if not paths:
        raise UsageError("ingest needs at least one SOURCE")
    store = open_store(store_path, env)
    try:
        tap = _IngestTap(store, rules)
        fields: dict[str, Any] = {
            "content_tier": tier, "team_map": tuple(team_map),
            "attribution": attribution or Attribution(), "since_ms": since_ms,
            "until_ms": until_ms, "lenient": not strict, "renormalize": renormalize}
        if experimental:
            fields["experimental"] = frozenset(experimental)
        opts = ingest_options(env, **fields)
        _sources, notes = ingest_paths(tap, paths, env, opts, adapter=adapter)
        today = _today(env)
        lo = since_ms if since_ms is not None else 0
        hi = until_ms if until_ms is not None else (env.now_ms // DAY_MS + 1) * DAY_MS
        return RunResult(command="ingest", window=(lo, max(hi, lo)), inputs=tap.inputs(),
                         privacy=privacy_info(env, content_tier=tier),
                         rate_card=rate_card_info(env.pricer, today),
                         data_quality=_dedupe_notes([*notes, *_pricer_notes(env.pricer, today)]))
    finally:
        _close(store)


# =================================================================================================
# bill
# =================================================================================================


def run_bill(store_path: Path, env: Env, *, since_ms: int, until_ms: int,
             group_by: Sequence[str] = (), self_view: bool = False,
             reprice: bool = False) -> RunResult:
    """``bill``: the exact bill of ``[since_ms, until_ms)`` with the estimated, allowance and pool
    figures beside it, ESR, the naive line-sum ratio and k-anonymous breakdowns
    (``pipeline.common.bill_summary``). ``reprice`` first re-prices the window with ``env.pricer``
    (rates / contract / basis of this run). ``self_view`` (``--self``) publishes without
    suppression and is only allowed on a ledger holding one person's data."""
    store = open_store(store_path, env, create=False)
    try:
        if reprice:
            store.reprice(env.pricer, since_ms=since_ms, until_ms=until_ms)
        if self_view:
            people = store.count_users(since_ms=since_ms, until_ms=until_ms, where={})
            if people > 1:
                raise PrivacyError("--self needs a ledger holding one person's data "
                                   f"(this window has {people} people); use a personal ledger "
                                   "(tokenbill scan / tokenbill me)")
        bill = bill_summary(store, env, since_ms=since_ms, until_ms=until_ms,
                            group_by=list(group_by), audience="self" if self_view else "org")
        today = _today(env)
        suppressed = sum(agg.suppressed_rows for _key, agg in bill.breakdowns)
        return RunResult(command="bill", window=(since_ms, until_ms), inputs=(),
                         privacy=privacy_info(env, suppressed_groups=suppressed),
                         rate_card=rate_card_info(env.pricer, today), bill=bill,
                         data_quality=_dedupe_notes(_pricer_notes(env.pricer, today)))
    finally:
        _close(store)


# =================================================================================================
# reconcile
# =================================================================================================


@dataclass(frozen=True)
class LivePull:
    """``reconcile --live --admin-key-env VAR [--record DIR]``: the key's environment variable
    (read by ``recon.pull``; never logged), where to keep the recorded pages (None: a private
    temporary directory, deleted afterwards) and the injectable opener / sleep (tests)."""

    key_env: str
    record_dir: Path | None = None
    opener: Callable[..., Any] | None = None
    sleep: Callable[[int], object] | None = None


def _remainders(store: object) -> dict[str, Decimal] | None:
    """Adapter name → Σ parse remainders in USD from ``LedgerStats.source_stats`` (the mapping
    ``core.extensions.run_reconcilers`` builds for extension reconcilers, R-E44)."""
    from tokenbill.core.protocols import LedgerStats

    if not isinstance(store, LedgerStats):
        return None
    if _ROUNDING_STAT not in store.source_stats():
        return {}
    names = list(registry.BUILTIN_ADAPTERS) + sorted(registry._PLUGIN_ADAPTERS)
    out: dict[str, Decimal] = {}
    for name in names:
        count = int(store.source_stats(adapter=name).get(_ROUNDING_STAT, 0))
        if count:
            out[name] = Decimal(count).scaleb(-18)
    return out


def _rerun_factory(pricer: object) -> Callable[[Any], Any] | None:
    layers = getattr(pricer, "layers", None)
    if layers is not None:
        try:
            card = registry.load("tokenbill.rates.engine:RateCard")
        except ImportError:  # pragma: no cover - the rate engine ships with the package
            return None
        return lambda overlay: card(layers, contract=overlay)
    return None  # reconcile falls back to pricer.with_contract (fakes)


def _uninformative(report: ReconciliationReport) -> bool:
    return (not report.rows and not report.decisions
            and all(c.verdict == "insufficient_data" for c in report.channels))


def reconcile_store(store: Any, env: Env, *, db_path: Path | None, since_ms: int, until_ms: int,
                    tolerance_pct: str = "0.5", unexplained_pct: str = "1.0",
                    closed_only: bool = False, suggest_contract: bool = False,
                    notes: list[DataQualityNote] | None = None) -> ReconciliationReport:
    """RECON's ``reconcile`` of the window (ledger streamed from *store*, provider aggregates and
    cost lines from the store) merged (``recon.reconcile.merge_reports``) with every extension
    reconciler's report (``core.extensions.run_reconcilers`` over the record stores on *db_path*);
    uninformative extension reports are left out (module doc)."""
    from tokenbill.recon.reconcile import merge_reports, reconcile

    today = _today(env)
    window = {"since_ms": since_ms, "until_ms": until_ms}
    report = reconcile(store.iter_usage_records(**window), store.aggregates(**window),
                       store.cost_lines(**window), env.pricer,
                       tolerance_pct=Decimal(tolerance_pct),
                       unexplained_pct=Decimal(unexplained_pct), closed_only=closed_only,
                       today=today, suggest_contract=suggest_contract,
                       rerun_pricer_factory=_rerun_factory(env.pricer),
                       rounding_remainders=_remainders(store))
    record_stores = (extensions.open_record_stores(db_path, create=True, notes=notes)
                     if db_path is not None else [])
    try:
        ext = extensions.run_reconcilers(store, record_stores, env.pricer, since_ms=since_ms,
                                         until_ms=until_ms, tolerance_pct=tolerance_pct,
                                         unexplained_pct=unexplained_pct,
                                         closed_only=closed_only, today=today, notes=notes)
    finally:
        for rs in record_stores:
            _close(rs)
    ext = [r for r in ext if not _uninformative(r)]
    return merge_reports([report, *ext]) if ext else report


def _write_contracts(report: ReconciliationReport, store: Any, env: Env, path: Path, *,
                     since_ms: int, until_ms: int, closed_only: bool) -> list[Path]:
    from tokenbill.rates.contract import contract_json
    from tokenbill.recon.reconcile import suggest_contracts

    if report.suggested_contract is None:
        return []
    window = {"since_ms": since_ms, "until_ms": until_ms}
    overlays = list(suggest_contracts(store.aggregates(**window), store.cost_lines(**window),
                                      env.pricer, today=_today(env), closed_only=closed_only))
    if not overlays:
        overlays = [report.suggested_contract]
    path = Path(path).expanduser()
    written = []
    for i, overlay in enumerate(overlays):
        target = path if i == 0 else path.with_name(f"{path.stem}-{i + 1}{path.suffix}")
        target.write_text(json.dumps(contract_json(overlay), indent=2) + "\n", encoding="utf-8")
        written.append(target)
    return written


def _pull_live(live: LivePull, since_ms: int, until_ms: int, out_dir: Path) -> list[tuple[Path,
                                                                                           str]]:
    from tokenbill.recon.pull import pull

    since = day_of(since_ms)
    until = day_of(until_ms)
    got: list[tuple[Path, str]] = []
    for kind, adapter in LIVE_SOURCES:
        kwargs: dict[str, Any] = {"key_env": live.key_env, "since": since, "until": until,
                                  "out_dir": out_dir, "opener": live.opener}
        if live.sleep is not None:
            kwargs["sleep"] = live.sleep
        for path in pull(kind, **kwargs):
            got.append((path, adapter))
    return got


def run_reconcile(store_path: Path, env: Env, *, files: Mapping[str, Sequence[Path]] | None = None,
                  since_ms: int, until_ms: int, tolerance_pct: str = "0.5",
                  unexplained_pct: str = "1.0", closed_only: bool = False,
                  suggest_contract: Path | None = None, live: LivePull | None = None
                  ) -> tuple[RunResult, tuple[Path, ...]]:
    """``reconcile``: ingest the provider files (*files*: :data:`RECON_SOURCES` key → paths; or the
    ``--live`` pull) into the ledger, then :func:`reconcile_store` over ``[since_ms, until_ms)``.
    With *suggest_contract* the suggested overlay(s) are written there (the first at the path,
    further ones as ``<stem>-<n><suffix>``) and the report carries the re-run verdict. Returns the
    result and the contract files written."""
    tolerance_pct = parse_pct(tolerance_pct, "--tolerance-pct")
    unexplained_pct = parse_pct(unexplained_pct, "--unexplained-pct")
    files = dict(files or {})
    unknown = sorted(set(files) - set(RECON_SOURCES))
    if unknown:
        raise UsageError(f"unknown reconcile source(s): {', '.join(unknown)}")
    checked = {kind: _check_sources(paths) for kind, paths in files.items() if paths}
    store_path = Path(store_path).expanduser()
    store = open_store(store_path, env)
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        tap = _IngestTap(store)
        notes: list[DataQualityNote] = []
        opts = ingest_options(env)
        for kind in RECON_SOURCES:
            if kind in checked:
                _s, found = ingest_paths(tap, checked[kind], env, opts,
                                         adapter=RECON_SOURCES[kind])
                notes.extend(found)
        if live is not None:
            lo = since_ms if since_ms > 0 else until_ms - 31 * DAY_MS
            if live.record_dir is not None:
                out_dir = Path(live.record_dir).expanduser()
                _private_dir(out_dir)
            else:
                tmp = tempfile.TemporaryDirectory(prefix="tokenbill-pull-")
                out_dir = Path(tmp.name)
            for path, adapter in _pull_live(live, lo, until_ms, out_dir):
                _s, found = ingest_paths(tap, [path], env, opts, adapter=adapter)
                notes.extend(found)
        report = reconcile_store(store, env, db_path=store_path, since_ms=since_ms,
                                 until_ms=until_ms, tolerance_pct=tolerance_pct,
                                 unexplained_pct=unexplained_pct, closed_only=closed_only,
                                 suggest_contract=suggest_contract is not None, notes=notes)
        written: list[Path] = []
        if suggest_contract is not None:
            written = _write_contracts(report, store, env, suggest_contract, since_ms=since_ms,
                                       until_ms=until_ms, closed_only=closed_only)
        today = _today(env)
        result = RunResult(command="reconcile", window=(since_ms, until_ms), inputs=tap.inputs(),
                           privacy=privacy_info(env), rate_card=rate_card_info(env.pricer, today),
                           reconciliation=report,
                           data_quality=_dedupe_notes([*notes, *_pricer_notes(env.pricer,
                                                                              today)]))
        return result, tuple(written)
    finally:
        _close(store)
        if tmp is not None:
            tmp.cleanup()


# =================================================================================================
# export
# =================================================================================================


def _team_days(store: Any, since_ms: int, until_ms: int) -> dict[str, int]:
    weights: dict[str, int] = {}
    for day in store.cluster_days(cluster_kind="team", since=day_of(since_ms),
                                  until=day_of(until_ms + DAY_MS - 1)):
        weights[day.cluster_id] = weights.get(day.cluster_id, 0) + day.active_users
    return weights


def _export_focus(store: Any, env: Env, db_path: Path, out: IO[str], *, since_ms: int,
                  until_ms: int, allow_unreconciled: bool, channels: frozenset[str] | None,
                  role: str, chargeback: bool, rules: object | None,
                  notes: list[DataQualityNote]) -> int:
    from tokenbill.finops.allocation import coverage, split_rows
    from tokenbill.outputs.focus import write_focus
    from tokenbill.recon.reconcile import reconciled_channels

    report = reconcile_store(store, env, db_path=db_path, since_ms=since_ms, until_ms=until_ms,
                             notes=notes)
    reconciled = reconciled_channels(report)
    rows = store.cost_rows(since_ms=since_ms, until_ms=until_ms, group_by=FOCUS_GROUP_BY)
    if rules is not None:
        rows = split_rows(rows, rules, weights=_team_days(store, since_ms, until_ms))  # type: ignore[arg-type]
    record_stores = extensions.open_record_stores(db_path, create=True, notes=notes)
    try:
        extra, owned = extensions.focus_rows(store, record_stores, since_ms=since_ms,
                                             until_ms=until_ms, reconciled_channels=reconciled,
                                             k=env.k, allow_unreconciled=allow_unreconciled,
                                             role=role, notes=notes)
    finally:
        for rs in record_stores:
            _close(rs)
    buf = io.StringIO()
    n = write_focus(rows, buf, reconciled_channels=reconciled,
                    allow_unreconciled=allow_unreconciled, role=role,
                    rate_card_sha=rate_card_info(env.pricer, _today(env)).sha256, k=env.k,
                    chargeback=chargeback, allocation_coverage=coverage(rows),
                    channels=channels, extra_rows=extra, owned_channels=owned,
                    findings=store.findings(), reconciliation=report)
    out.write(buf.getvalue())  # only after every gate passed: nothing is written on refusal
    return n


def _export_trace2(store: Any, out: IO[str] | None, out_path: Path | None, *, since_ms: int,
                   until_ms: int, content: ContentTier) -> int:
    from tokenbill import __version__
    from tokenbill.adapters.trace_v2 import TraceV2Writer, write_trace_v2

    lanes: list[Lane] = list(store.iter_lanes(since_ms=since_ms, until_ms=until_ms))
    by_session: dict[str, list[Lane]] = {}
    for lane in lanes:
        by_session.setdefault(lane.session_key, []).append(lane)
    sessions = []
    fp_keys: set[str] = set()
    for key in sorted(by_session):
        members = by_session[key]
        reqs = [r for lane in members for r in lane.requests]
        stamps = [a.ts_start_ms for r in reqs for a in r.attempts] or [
            e.ts_ms for lane in members for e in lane.events] or [0]
        kinds = sorted({r.source.adapter for r in reqs if r.source is not None})
        for r in reqs:
            if r.fingerprint is not None:
                fp_keys.add(r.fingerprint.key_id)
        sessions.append(Session(session_key=key, source_kind=kinds[0] if kinds else "ledger",
                                attribution=Attribution(), lanes=tuple(members),
                                started_ms=min(stamps), ended_ms=max(stamps)))
    meta = store.meta() if callable(getattr(store, "meta", None)) else {}
    principal_key = meta.get("org_key_id") or meta.get("adopted_key_id") or None
    profile = "fingerprint" if content is ContentTier.FINGERPRINT else "usage"
    if profile == "fingerprint" and len(fp_keys) > 1:
        raise UsageError("export --content fingerprint: the window mixes fingerprint keys")
    header: dict[str, Any] = {
        "trace_id": stable_id("t", "export", since_ms, until_ms), "profile": profile,
        "identity_mode": "central-ingest", "name_key_id": meta.get("name_key_id") or None,
        "principal_key_id": principal_key,
        "fp_key_id": (next(iter(fp_keys)) if fp_keys else None) if profile == "fingerprint"
        else None,
        "created_ms": 0, "producer": {"name": "tokenbill", "version": __version__,
                                      "adapter": "trace@2"}}
    window = {"since_ms": since_ms, "until_ms": until_ms}
    aggregates = store.aggregates(**window)
    cost_lines = store.cost_lines(**window)
    outcomes = store.outcomes(**window)
    if out_path is not None:
        return write_trace_v2(out_path, header=header, sessions=sessions, aggregates=aggregates,
                              cost_lines=cost_lines, outcomes=outcomes)
    assert out is not None
    writer = TraceV2Writer(out, header)
    for s in sessions:
        writer.session(s)
    for s in sessions:
        for lane in s.lanes:
            for r in lane.requests:
                writer.request(r)
    for s in sessions:
        for lane in s.lanes:
            for e in lane.events:
                writer.event(e)
    for a in aggregates:
        writer.aggregate(a)
    for c in cost_lines:
        writer.cost_line(c)
    for o in outcomes:
        writer.outcome(o)
    return writer.count


def run_export(store_path: Path, env: Env, *, fmt: str, since_ms: int, until_ms: int,
               out: IO[str] | None = None, out_path: Path | None = None, grain: str = "day",
               content: str = "none", allow_unreconciled: bool = False,
               channels: Sequence[str] | None = None, role: str = "primary",
               chargeback: bool = False, rules: object | None = None,
               report: str = "daily", notes: list[DataQualityNote] | None = None) -> int:
    """``export``: FOCUS 1.4 CSV (per-channel ``BilledCost`` rule from a fresh reconciliation of
    the window, D19; ``channels`` restricts; ``role``; ``chargeback`` gate; allocation splits),
    a trace@2 file of the ledger (``content`` none → ``usage`` profile, ``fingerprint`` →
    ``fingerprint``; ``full`` is refused) or the ccusage-compatible daily / monthly JSON. Writes to
    *out_path* when given, else to *out*. Returns the rows / records written. ``GateFailed`` (exit
    3) when the FOCUS gates refuse; nothing is written then."""
    if fmt not in EXPORT_FORMATS:
        raise UsageError(f"export --format must be one of {', '.join(EXPORT_FORMATS)}")
    tier = _content_tier(content)
    if grain != "day":
        raise UsageError("--grain hour is not available: the ledger's cost rows are daily "
                         "(v0.2); use --grain day")
    if fmt == "ccusage" and report not in ("daily", "monthly"):
        raise UsageError("--report must be daily or monthly")
    if out is None and out_path is None:
        raise UsageError("export needs an output (-o FILE or stdout)")
    store_path = Path(store_path).expanduser()
    store = open_store(store_path, env, create=False)
    notes = notes if notes is not None else []
    try:
        if fmt == "trace2":
            return _export_trace2(store, out, out_path, since_ms=since_ms, until_ms=until_ms,
                                  content=tier)
        buf = io.StringIO()
        if fmt == "focus":
            n = _export_focus(store, env, store_path, buf, since_ms=since_ms, until_ms=until_ms,
                              allow_unreconciled=allow_unreconciled,
                              channels=frozenset(channels) if channels else None, role=role,
                              chargeback=chargeback, rules=rules, notes=notes)
        else:
            from tokenbill.outputs.ccusage import to_ccusage

            rows = store.cost_rows(since_ms=since_ms, until_ms=until_ms,
                                   group_by=("date", "model"))
            buf.write(to_ccusage(rows, report=report))
            n = len(rows)
        if out_path is not None:
            Path(out_path).expanduser().write_text(buf.getvalue(), encoding="utf-8", newline="")
        else:
            assert out is not None
            out.write(buf.getvalue())
        return n
    finally:
        _close(store)


# =================================================================================================
# showback
# =================================================================================================


def run_showback(store_path: Path, env: Env, out_dir: Path, *, formats: Sequence[str] = ("html",),
                 since_ms: int, until_ms: int,
                 notes: list[DataQualityNote] | None = None) -> list[Path]:
    """``showback``: per-team pages (HTML with CSP, CSV, JSON) from published aggregates only
    (``outputs.showback.render_showback``: team spend, developer-days, cache-read share, latest
    findings, per billing path) plus every extension's pages (``core.extensions.showback``).
    Never individuals."""
    from tokenbill.core.kanon import publish
    from tokenbill.outputs.showback import render_showback

    store = open_store(store_path, env, create=False)
    try:
        window = {"since_ms": since_ms, "until_ms": until_ms}
        teams = publish(store.aggregate(**window, group_by=("team",)), k=env.k)
        paths = publish(store.aggregate(**window, group_by=("billing_path", "team")), k=env.k)
        days = store.cluster_days(cluster_kind="team", since=day_of(since_ms),
                                  until=day_of(until_ms + DAY_MS - 1))
        findings = store.findings()
        written = render_showback(teams, days, findings, None, Path(out_dir).expanduser(),
                                  formats=tuple(formats), billing_paths=paths)
        bill = bill_summary(store, env, since_ms=since_ms, until_ms=until_ms, group_by=[])
        result = RunResult(command="showback", window=(since_ms, until_ms), inputs=(),
                           privacy=privacy_info(env), rate_card=rate_card_info(env.pricer,
                                                                               _today(env)),
                           bill=bill, findings=tuple(findings))
        written += extensions.showback(result, Path(out_dir).expanduser(), tuple(formats),
                                       notes=notes)
        return sorted(set(written))
    finally:
        _close(store)


# =================================================================================================
# pricing
# =================================================================================================


def _pricing_result(report: PricingReport, today: str, k: int) -> RunResult:
    day = date_start_ms(today)
    return RunResult(command="pricing", window=(day, day + DAY_MS), inputs=(),
                     privacy=PrivacyInfo(content_tier=ContentTier.NONE, key_id=None,
                                         identity_mode="install", k=k, suppressed_groups=0),
                     rate_card=None, pricing=report)


def _row_matches(row: Any, model: str | None, at: str | None) -> bool:
    if model is not None:
        from tokenbill.core.models import normalize_model

        wanted = {model, normalize_model(model).model or model}
        if not wanted & {row.model, *row.aliases}:
            return False
    if at is not None:
        if row.effective_from > at:
            return False
        if row.effective_to is not None and at >= row.effective_to:
            return False
    return True


def _layer(spec: Path | str) -> Any:
    from tokenbill.rates.schema import load_builtin, load_file

    if str(spec) == "builtin":
        return load_builtin()
    path = Path(spec).expanduser()
    if not path.is_file():
        raise UsageError(f"rate file {path.name} not found")
    return load_file(path, f"user:{path.name}")


def run_pricing(action: str, *, today: str, k: int = 5, model: str | None = None,
                at: str | None = None, rates: Sequence[Path] = (), snapshot: Path | None = None,
                live: bool = False, feeds: Sequence[Path] = (), a: Path | str | None = None,
                b: Path | str | None = None, opener: Callable[..., Any] | None = None,
                notes: list[DataQualityNote] | None = None) -> RunResult:
    """``pricing show [MODEL] [--at DATE]`` (the builtin registry and ``--rates`` layers, filtered),
    ``pricing verify`` (offline packaged snapshot or ``--snapshot``; ``--live`` fetches the pricing
    page; the extensions' verifiers; ``--feed`` LiteLLM / OpenRouter files as non-authoritative
    warnings) and ``pricing diff A B`` (rate files, or ``builtin``). ``verify``'s ``ok`` is False
    on any authoritative discrepancy (CLI exit 3)."""
    from tokenbill.rates import verify as rv

    if action == "show":
        layers = [_layer("builtin"), *(_layer(p) for p in rates)]
        rows = tuple(r for layer in layers for r in layer.rows if _row_matches(r, model, at))
        mods = tuple(m for layer in layers for m in layer.modifiers)
        stale = tuple(s for layer in layers for s in rv.stale_rows(layer, today=today))
        report = PricingReport(kind="show", rows=rows, modifiers=mods, discrepancies=(),
                               stale_rows=stale, ok=True)
    elif action == "verify":
        layer = _layer("builtin")
        report = rv.verify_report(layer, today=today, snapshot_path=snapshot, live=live,
                                  opener=opener, notes=notes)
        extra = []
        for feed in feeds:
            path = Path(feed).expanduser()
            from tokenbill.core.jsonl import load_json_exact

            try:
                doc = load_json_exact(path, max_bytes=64 << 20)
            except Exception as exc:  # SourceError / OSError → a usage error naming the file
                raise UsageError(f"--feed {path.name}: not a readable JSON file") from exc
            extra.extend(rv.crosscheck_feed(layer, doc))
        if extra:
            report = dataclasses.replace(report,
                                         discrepancies=(*report.discrepancies, *extra))
    elif action == "diff":
        if a is None or b is None:
            raise UsageError("pricing diff needs two rate files A B (or 'builtin')")
        report = rv.diff_layers(_layer(a), _layer(b))
    else:
        raise UsageError("pricing action must be show, verify, diff or emit-model-pricing")
    return _pricing_result(report, today, k)


def run_emit_model_pricing(contract: Path) -> str:
    """``pricing emit-model-pricing --contract FILE``: the Claude Code managed-settings
    ``modelPricing`` block of a contract overlay (``rates.contract.dumps_model_pricing``)."""
    from tokenbill.rates.contract import dumps_model_pricing, load_contract

    path = Path(contract).expanduser()
    if not path.is_file():
        raise UsageError(f"contract file {path.name} not found")
    return dumps_model_pricing(load_contract(path))


# =================================================================================================
# purge
# =================================================================================================


def run_purge(store_path: Path, env: Env, *, principal: str | None = None,
              before_ms: int | None = None, actor: str = "tokenbill purge") -> dict[str, int]:
    """``purge``: erase one principal's rows (``p_…`` pseudonym, e.g. printed by ``tokenbill
    copilot pseudonym``; also rows under an adopted key id) and/or everything before *before_ms*,
    in the ledger and in every extension record store. Each store writes its own audit row (never
    the identity). Returns ``{"requests": …, "records": …}`` removed."""
    if principal is None and before_ms is None:
        raise UsageError("purge needs --principal P or --before DATE")
    if principal is not None and not _P_RE.match(principal):
        raise UsageError("--principal must be a p_ pseudonym (p_ + 20 hex); see "
                         "`tokenbill copilot pseudonym`")
    path = Path(store_path).expanduser()
    store = open_store(path, env, create=False)
    try:
        n = store.purge(principal=principal, before_ms=before_ms, actor=actor)
        notes: list[DataQualityNote] = []
        record_stores = extensions.open_record_stores(path, create=True, notes=notes)
        try:
            m = extensions.purge(record_stores, principal=principal, before_ms=before_ms,
                                 actor=actor)
        finally:
            for rs in record_stores:
                _close(rs)
        return {"requests": n, "records": m}
    finally:
        _close(store)


# =================================================================================================
# analyze (v2 engine)
# =================================================================================================


def run_analyze_v2(paths: Sequence[Path], env: Env) -> RunResult:
    """The v2 engine of ``analyze`` (SPEC §15.1): read trace@1 files through ``TraceV1Adapter``
    (the Env's pricer carries any ``--model-price`` layer) into a temporary ledger and return the
    exact bill (per model), coverage (unknown models are ``unpriced``, never $0) and data
    quality. Runs of different files never merge (sessions are keyed by file, ruling R-E27)."""
    files = _check_sources(paths)
    with tempfile.TemporaryDirectory(prefix="tokenbill-analyze-") as tmp:
        store = open_store(Path(tmp) / "analyze.db", env)
        try:
            tap = _IngestTap(store)
            opts = ingest_options(env, identity_mode="install")
            _s, notes = ingest_paths(tap, files, env, opts, adapter="trace@1")
            stamps = [a.ts_start_ms for r in store.iter_requests() for a in r.attempts]
            lo = (min(stamps) // DAY_MS) * DAY_MS if stamps else 0
            hi = (max(stamps) // DAY_MS + 1) * DAY_MS if stamps else DAY_MS
            bill = bill_summary(store, env, since_ms=lo, until_ms=hi, group_by=["model"],
                                audience="self")
            today = _today(env)
            return RunResult(command="analyze", window=(lo, hi), inputs=tap.inputs(),
                             privacy=privacy_info(env, identity_mode="install"),
                             rate_card=rate_card_info(env.pricer, today), bill=bill,
                             data_quality=_dedupe_notes([*notes,
                                                         *_pricer_notes(env.pricer, today)]))
        finally:
            _close(store)

