"""The savings side of the pipeline (SPEC §15, §15.2, §17, §18, D18, D30, D32; CLI-SAVINGS).

Pure functions over a ledger that return typed results (no printing): ``run_findings``,
``run_calibrate``, ``run_whatif``, ``run_policy``, ``run_check_effect``, ``run_scan``,
``run_scan_org``, ``run_report``, ``run_demo_fleet`` and ``run_check``. Every function takes
explicit parameters (store, Env, window, paths) so tests call it without argparse. The CLI glue
the ``tokenbill.commands`` modules share (flag helpers, Env from arguments, output writing, the
exit-code mapping) sits at the end of this module.

**The findings path** (SPEC §15, ``run_findings``), streamed shard by shard:

1. ``lane_index`` → ``core.shards.plan_shards`` (cap ``Config.shard_max_requests``, one shard per
   team, split per lane kind above the cap);
2. a survey pass over the shards: capabilities present in the data (see
   :func:`capabilities_of`), the COMPACTION ``post_tokens`` of every lane (the org median, R-E40,
   computed once per run) and — in the self view — the principals seen;
3. the static-prefix floor from ``lane_first_reads`` (``core.transitions.static_prefix_floor``);
4. the model gate: two streaming passes (``sim.calibrate`` pass 1 per shard, ρ fitted per fold,
   pass 2 per shard, finish) — partials add, so the report is shard-invariant;
5. RECON's reconcile over the window plus ``core.extensions.run_reconcilers`` when an extension
   has data (``merge_reports``); decisions are never persisted, so they are passed to
   ``core.extensions.enrich(…, recon_decisions=recon_decisions_of(reports))`` (A-11);
6. per shard ``run_detectors(…, emit_missing=False, aggregates_only=False)`` through
   ``pipeline.common.map_shards`` (``--jobs``), then once ``run_detectors([], …,
   emit_missing=True, aggregates_only=True)`` (org scan and other aggregate detectors, ruling
   R-E17) → ``merge_findings``;
7. ``rescope_findings(count_users=core.extensions.count_users_fn(…))`` (k-anonymity; the self view
   never suppresses, R-E10); break-glass ``tail.runaway`` session findings bypass re-scoping and
   are audited (R-E13);
8. ``build_action_plan`` (seeded stratified sample, full-scope joint replay through the same
   shard mapper, billed and allowance apart) and the Shapley credits of its levers attached to
   their findings (``Finding.recoverable_shapley`` / ``projected_monthly``, split in proportion
   to the findings' standalone recoverable figures) — findings are then ranked by Shapley credit;
9. ``put_findings`` (org view only).

Results are identical for every ``--jobs`` value and shard cap (every cross-shard step is an
order-independent merge, and ``merge_findings`` sorts).

Decisions where the SPEC is silent are listed in ``tests/v2/cli_savings/README.md``.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import functools
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import IO, Any

from tokenbill.common import TokenbillError
from tokenbill.config import Config, load_config
from tokenbill.core import extensions, kanon, registry
from tokenbill.core.errors import GateFailed, PrivacyError, SourceError, UsageError
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.jsonl import load_json_exact, open_private
from tokenbill.core.labels import Basis, Figure, scale
from tokenbill.core.protocols import ExtRecordStore, LedgerStore, Pricer
from tokenbill.core.records import (
    BILLING_PATHS,
    ContentTier,
    Fidelity,
    Lane,
    LaneEventKind,
    WorkloadClass,
)
from tokenbill.core.shards import merge_findings, plan_shards, shard_where
from tokenbill.core.types import (
    ActionPlan,
    AnalysisContext,
    BillSummary,
    CalibrationReport,
    CheckResult,
    DataQualityNote,
    Finding,
    IngestOptions,
    LaneIndexRow,
    PolicyPack,
    PrivacyInfo,
    RateCardInfo,
    ReconciliationReport,
    ReplayResult,
    RunResult,
    ShardKey,
    SourceInfo,
)
from tokenbill.pipeline import common
from tokenbill.pipeline.common import Env

__all__ = [
    "CHECK_KEY",
    "DAY_MS",
    "DEFAULT_CLAUDE_DIR",
    "FINDINGS_GROUP_BY",
    "LIVE_PULLS",
    "ORG_SCAN_ADAPTERS",
    "SCAN_KEY",
    "SELF_REF",
    "capabilities_of",
    "resolve_window",
    "run_calibrate",
    "run_check",
    "run_check_effect",
    "run_check_result",
    "run_demo_fleet",
    "run_findings",
    "run_policy",
    "run_report",
    "run_scan",
    "run_scan_org",
    "run_whatif",
]

logger = logging.getLogger("tokenbill.pipeline.savings")

DAY_MS = 86_400_000
_MAX_MS = 2**53
#: Install key of ``scan``'s single-user store when no org key is configured: a public constant
#: (the temporary store holds one person's own data, deleted after the run; the key names that one
#: person consistently, it protects nothing).
SCAN_KEY = hashlib.sha256(b"tokenbill scan: local install key (single-user store)").digest()
#: The opaque principal reference of the local user in ``scan`` / ``me``.
SELF_REF = "self"
#: Fingerprint key of ``check`` (hashes are compared only within one run; public constant).
CHECK_KEY = hashlib.sha256(b"tokenbill check: fingerprint key (one run only)").digest()
DEFAULT_CLAUDE_DIR = Path("~/.claude/projects")
#: ``scan --org`` flag → ADMIN adapter.
ORG_SCAN_ADAPTERS: Mapping[str, str] = {
    "usage_report": "anthropic-usage-report", "cost_report": "anthropic-cost-report",
    "cc_analytics": "anthropic-cc-analytics",
    "enterprise_analytics": "anthropic-enterprise-analytics",
    "aws_cur": "aws-cur", "gcp_billing": "gcp-billing",
}
#: ``scan --org --live`` pulls: (``recon.pull`` kind, adapter reading the recorded pages).
LIVE_PULLS: tuple[tuple[str, str], ...] = (
    ("usage_report", "anthropic-usage-report"), ("cost_report", "anthropic-cost-report"),
    ("claude_code", "anthropic-cc-analytics"))
#: ``findings --group-by`` dimensions (``principal`` only in the self view).
FINDINGS_GROUP_BY = ("team", "repo", "agent_type", "model", "lane_kind", "principal")
_TAIL = "tail.runaway"
_TOLERANCE_PCT = "0.5"
_UNEXPLAINED_PCT = "1.0"
_DQ_BREAK_GLASS = "break-glass"
#: Ledgers with at most this many requests in the window keep their lanes in memory between the
#: passes of an in-process run (survey, two calibration passes, detectors, joint replay); larger
#: ones stream every pass from the store (bounded memory, SPEC §17).
LANE_CACHE_MAX_REQUESTS = 200_000
#: Shapley sample of ``demo --fleet`` (the plan's credits are scaled to one full-scope joint
#: replay, SPEC §11.2 step 6; a small sample keeps the demo within its 60 s budget, §17).
DEMO_SAMPLE_LANES = 64


# =============================================================================================
# small helpers
# =============================================================================================


def _date(ms: int) -> str:
    return (_dt.date(1970, 1, 1) + _dt.timedelta(days=ms // DAY_MS)).isoformat()


def _date_ms(date: str) -> int:
    try:
        day = _dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise UsageError("dates must be YYYY-MM-DD") from None
    return (day - _dt.date(1970, 1, 1)).days * DAY_MS


def today_of(env: Env) -> str:
    """The UTC date of the Env clock (``YYYY-MM-DD``)."""
    return _date(env.now_ms)


def _data_bounds(store: LedgerStore) -> tuple[int, int] | None:
    """``[first day, last day + 1)`` of the requests, aggregates and cost lines in *store*."""
    days: list[int] = []
    raw = store.aggregate(since_ms=0, until_ms=_MAX_MS, group_by=("date",))
    for row in raw.rows:
        value = dict(row.dims).get("date")
        if isinstance(value, str) and value:
            days.append(_date_ms(value))
    for agg in store.aggregates(None, since_ms=0, until_ms=_MAX_MS):
        days.append(agg.bucket_start_ms - agg.bucket_start_ms % DAY_MS)
        days.append(max(agg.bucket_end_ms - 1, agg.bucket_start_ms) // DAY_MS * DAY_MS)
    for line in store.cost_lines(None, since_ms=0, until_ms=_MAX_MS):
        days.append(_date_ms(line.date_utc))
    if not days:
        return None
    return (min(days), max(days) + DAY_MS)


def resolve_window(store: LedgerStore, since_ms: int | None,
                   until_ms: int | None) -> tuple[int, int]:
    """The analysis window ``[since, until)``: the given bounds, else the whole UTC days the store's
    data covers (an empty store gives one day from *since*, or from the epoch). ``UsageError`` when
    ``until <= since``."""
    for value, name in ((since_ms, "since"), (until_ms, "until")):
        if value is not None and (type(value) is not int or not 0 <= value <= _MAX_MS):
            raise UsageError(f"--{name} must be a date (YYYY-MM-DD)")
    if since_ms is None or until_ms is None:
        bounds = _data_bounds(store)
        lo = since_ms if since_ms is not None else (bounds[0] if bounds else 0)
        hi = until_ms if until_ms is not None else (bounds[1] if bounds else lo + DAY_MS)
    else:
        lo, hi = since_ms, until_ms
    if hi <= lo:
        raise UsageError("the window is empty: --until must be after --since")
    return lo, hi


def rate_card_info(pricer: Pricer, today: str) -> RateCardInfo:
    """``RateCardInfo`` of *pricer* (``RateCard.info`` when available)."""
    info = getattr(pricer, "info", None)
    if callable(info):
        got = info(today=today)
        if isinstance(got, RateCardInfo):
            return got
    return RateCardInfo(sha256=pricer.rate_card_sha256, layers=(), stale_rows=(),
                        contract=None, basis=pricer.basis)


def _rate_notes(pricer: Pricer, today: str) -> list[DataQualityNote]:
    dq = getattr(pricer, "data_quality", None)
    if not callable(dq):
        return []
    got = dq(today=today)
    return [n for n in got if isinstance(n, DataQualityNote)]


def _privacy(env: Env, *, identity_mode: str, suppressed: int = 0,
             tier: ContentTier = ContentTier.NONE) -> PrivacyInfo:
    return PrivacyInfo(content_tier=tier,
                       key_id=key_id(env.org_key) if env.org_key is not None else None,
                       identity_mode=identity_mode, k=env.k, suppressed_groups=suppressed)


def _run_id(command: str, window: tuple[int, int], pricer: Pricer, *extra: object) -> str:
    return stable_id("run", command, window[0], window[1], pricer.rate_card_sha256,
                     *(str(x) for x in extra))


def _record_stores(db_path: Path | None, notes: list[DataQualityNote]) -> list[ExtRecordStore]:
    if db_path is None:
        return []
    found: list[DataQualityNote] = []
    stores = extensions.open_record_stores(Path(db_path), create=False, notes=found)
    notes.extend(found)
    return stores


def _dq_warnings(notes: Iterable[DataQualityNote]) -> int:
    return sum(1 for n in notes if n.severity in ("warn", "error"))


# =============================================================================================
# capabilities (derived from the data: the store does not keep adapters' declarations)
# =============================================================================================


def capabilities_of(lanes: Iterable[Lane]) -> frozenset[str]:
    """The SPEC §5.1 lane-level capabilities the data of *lanes* shows: ``usage_sequence`` and
    ``timing`` (any request), ``ttft``, ``ttl_split`` and ``iterations`` (a full-fidelity source
    or a 5m/1h write split), ``attempts`` (a retried or failed attempt), ``diagnostics``,
    ``appended``, ``events``, ``human_prompts``, ``quota_state``, ``lanes_exact``, ``params``
    (effort, thinking, max_tokens or breakpoints), ``blocks`` (fingerprints),
    ``attribution.team`` and ``workload``. The union over shards equals the whole store's."""
    caps: set[str] = set()
    for lane in lanes:
        if lane.events:
            caps.add("events")
            kinds = {e.kind for e in lane.events}
            if LaneEventKind.HUMAN_PROMPT in kinds:
                caps.add("human_prompts")
            if LaneEventKind.QUOTA_STATE in kinds:
                caps.add("quota_state")
        if lane.team:
            caps.add("attribution.team")
        if not lane.requests:
            continue
        caps.update(("usage_sequence", "timing"))
        if lane.lane_exact:
            caps.add("lanes_exact")
        for req in lane.requests:
            _request_caps(req, caps)
    return frozenset(caps)


def _request_caps(req: Any, caps: set[str]) -> None:
    src = req.source
    if src is None or src.fidelity >= Fidelity.FULL:
        caps.update(("ttl_split", "iterations"))
    if req.fingerprint is not None:
        caps.add("blocks")
    if req.appended:
        caps.add("appended")
    p = req.params
    if (p.effort is not None or p.thinking is not None or p.max_tokens is not None
            or p.breakpoints or p.session_effort is not None):
        caps.add("params")
    if req.attribution.workload_class is not WorkloadClass.UNKNOWN:
        caps.add("workload")
    if len(req.attempts) > 1:
        caps.add("attempts")
    for att in req.attempts:
        if att.ttft_ms is not None:
            caps.add("ttft")
        if att.diagnostics is not None:
            caps.add("diagnostics")
        for inf in att.inferences:
            if inf.usage.cache_write_5m or inf.usage.cache_write_1h:
                caps.add("ttl_split")


def _store_caps(store: LedgerStore, since_ms: int, until_ms: int) -> frozenset[str]:
    window = {"since_ms": since_ms, "until_ms": until_ms}
    caps = set()
    if store.aggregates(None, **window):
        caps.add("aggregates")
    if store.cost_lines(None, **window):
        caps.add("cost")
    if store.outcomes(**window):
        caps.add("outcomes")
    return frozenset(caps)


# =============================================================================================
# shard tasks (picklable callables for pipeline.common.map_shards)
# =============================================================================================


@dataclass
class LaneSource:
    """Loads lanes of the window for ``build_action_plan`` (``load_lanes(lane_keys, shard)``) and
    the shard tasks: from the running ``map_shards`` store, else a read-only store opened on
    ``db_path``, else the in-memory ``memory`` store (``--jobs 1`` only). With ``principal`` only
    that person's lanes are returned (the self view; stores never filter by person). ``cache``
    (in-process runs of small ledgers only; never pickled) keeps each shard's lanes between the
    passes of one run — lanes are immutable, so every pass sees identical data."""

    since_ms: int
    until_ms: int
    db_path: str | None = None
    principal: str | None = None
    team: str | None = None
    memory: Any = field(default=None, repr=False, compare=False)
    cache: dict[ShardKey, list[Lane]] | None = field(default=None, repr=False, compare=False)

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["memory"] = None           # a worker reads the store it opened by path
        state["cache"] = None
        return state

    def mine(self, lane: Lane) -> bool:
        """Whether *lane* belongs to the self principal (always True without one)."""
        if self.principal is None:
            return True
        return any(req.attribution.principal == self.principal for req in lane.requests)

    def _load(self, store: LedgerStore, lane_keys: Collection[str] | None,
              shard: ShardKey | None) -> list[Lane]:
        where = shard_where(shard) if shard is not None else (
            {"team": self.team} if self.team is not None else None)
        keys = None if lane_keys is None else sorted(lane_keys)
        return list(store.iter_lanes(since_ms=self.since_ms, until_ms=self.until_ms,
                                     where=where, lane_keys=keys))

    def _fetch(self, lane_keys: Collection[str] | None, shard: ShardKey | None) -> list[Lane]:
        try:
            store = common.shard_store()
        except UsageError:
            store = None
        if store is not None:
            return self._load(store, lane_keys, shard)
        if self.memory is not None:
            return self._load(self.memory, lane_keys, shard)
        if self.db_path is None:
            raise UsageError("no ledger to load lanes from")
        opened = registry.load(common.STORE_CLASS)(Path(self.db_path), create=False,
                                                   read_only=True)
        try:
            return self._load(opened, lane_keys, shard)
        finally:
            close = getattr(opened, "close", None)
            if callable(close):
                close()

    def __call__(self, lane_keys: Collection[str] | None,
                 shard: ShardKey | None) -> list[Lane]:
        if self.cache is not None and lane_keys is None and shard is not None:
            lanes = self.cache.get(shard)
            if lanes is None:
                lanes = self.cache[shard] = self._fetch(None, shard)
        else:
            lanes = self._fetch(lane_keys, shard)
        return [lane for lane in lanes if self.mine(lane)]


@dataclass(frozen=True)
class _SurveyPart:
    caps: frozenset[str]
    posts: tuple[int, ...]
    principals: frozenset[str]
    lane_keys: tuple[str, ...]


@dataclass
class _Survey:
    source: LaneSource
    keep_keys: bool

    def __call__(self, shard: ShardKey) -> _SurveyPart:
        lanes = self.source(None, shard)
        principals: set[str] = set()
        for lane in lanes:
            for req in lane.requests:
                if req.attribution.principal and len(principals) < 2:
                    principals.add(req.attribution.principal)
        return _SurveyPart(caps=capabilities_of(lanes),
                           posts=tuple(common.compaction_post_tokens(lanes)),
                           principals=frozenset(principals),
                           lane_keys=tuple(sorted(ln.lane_key for ln in lanes))
                           if self.keep_keys else ())


@dataclass
class _Calibrate:
    source: LaneSource
    pricer: Pricer
    rules: Any
    granularity: str
    floor: Mapping[tuple[str, str], int]
    rho: Mapping[int, Mapping[str, Decimal]] | None = None

    def __call__(self, shard: ShardKey) -> Any:
        from tokenbill.sim import calibrate as cal

        lanes = self.source(None, shard)
        kw: dict[str, Any] = {"pricer": self.pricer, "rules": self.rules,
                              "granularity": self.granularity, "static_prefix_floor": self.floor}
        if self.rho is None:
            return cal.calibrate_pass1(lanes, **kw)
        return cal.calibrate_pass2(lanes, self.rho, **kw)


@dataclass
class _Detect:
    source: LaneSource
    ctx: AnalysisContext
    only: tuple[str, ...] | None

    def __call__(self, shard: ShardKey) -> list[Finding]:
        from tokenbill.core.registry import run_detectors

        lanes = self.source(None, shard)
        ctx = dataclasses.replace(self.ctx, shard=shard)
        return run_detectors(lanes, ctx, only=list(self.only) if self.only else None,
                             emit_missing=False, aggregates_only=False)


@dataclass
class _Replay:
    source: LaneSource
    ctx: AnalysisContext
    policy: Any
    mode: str
    lane_keys: frozenset[str] | None = None

    def __call__(self, shard: ShardKey | None) -> dict[str, ReplayResult]:
        lanes = self.source(self.lane_keys if shard is None else None, shard)
        by_class: dict[str, list[Lane]] = {}
        for lane in lanes:
            by_class.setdefault(lane.billing_class, []).append(lane)
        out: dict[str, ReplayResult] = {}
        replayer = self.ctx.replayer
        assert replayer is not None
        for cls in sorted(by_class):
            out[cls] = replayer.replay(by_class[cls], self.policy, mode=self.mode,
                                       pricer=self.ctx.pricer, rules=self.ctx.rules,
                                       calibration=self.ctx.calibration,
                                       static_prefix_floor=self.ctx.static_prefix_floor)
        return out


# =============================================================================================
# one analysis run over a ledger
# =============================================================================================


@dataclass
class _Analysis:
    """The shared state of one run over a ledger window (see the module docstring)."""

    store: LedgerStore
    env: Env
    db_path: Path | None
    since_ms: int
    until_ms: int
    today: str
    principal: str | None = None
    self_view: bool = False
    team: str | None = None
    jobs: int = 1
    shard_cap: int = 250_000
    record_stores: list[ExtRecordStore] = field(default_factory=list)
    notes: list[DataQualityNote] = field(default_factory=list)
    text_notes: list[str] = field(default_factory=list)
    # survey results
    index: list[LaneIndexRow] = field(default_factory=list)
    shards: list[ShardKey] = field(default_factory=list)
    caps: frozenset[str] = frozenset()
    post_median: int | None = None
    floor: dict[tuple[str, str], int] = field(default_factory=dict)
    lane_cache: dict[ShardKey, list[Lane]] | None = None

    @property
    def window(self) -> tuple[int, int]:
        return (self.since_ms, self.until_ms)

    def source(self) -> LaneSource:
        return LaneSource(self.since_ms, self.until_ms,
                          db_path=str(self.db_path) if self.db_path is not None else None,
                          principal=self.principal, team=self.team,
                          memory=None if self.db_path is not None else self.store,
                          cache=self.lane_cache)

    def map(self, fn: Callable[[ShardKey], Any], shards: Sequence[ShardKey]) -> list[Any]:
        jobs = self.jobs if self.db_path is not None else 1
        return common.map_shards(fn, shards, jobs=jobs, db_path=self.db_path)

    def mapper(self) -> Callable[[Callable[[ShardKey], Any], Sequence[ShardKey]], list[Any]]:
        return functools.partial(common.map_shards,
                                 jobs=self.jobs if self.db_path is not None else 1,
                                 db_path=self.db_path)

    # ---------- 1–3: index, shards, survey, floor ----------

    def survey(self) -> None:
        index = list(self.store.lane_index(since_ms=self.since_ms, until_ms=self.until_ms))
        if self.team is not None:
            index = [row for row in index if (row.team or "") == self.team]
        shards = plan_shards(index, max_requests=self.shard_cap)
        in_process = self.db_path is None or self.jobs == 1
        if in_process and sum(row.requests for row in index) <= LANE_CACHE_MAX_REQUESTS:
            self.lane_cache = {}
        explicit = self.principal is not None
        parts: list[_SurveyPart] = self.map(_Survey(self.source(), keep_keys=explicit), shards)
        caps: set[str] = set()
        posts: list[int] = []
        principals: set[str] = set()
        mine: set[str] = set()
        for part in parts:
            caps |= part.caps
            posts.extend(part.posts)
            principals |= part.principals
            mine.update(part.lane_keys)
        if self.self_view and not explicit:
            if len(principals) > 1:
                raise UsageError("this ledger holds several people's data: pass --principal-ref "
                                 "REF with the org key (key holder only) for the self view")
            self.principal = next(iter(principals), None)
        if explicit:
            index = [row for row in index if row.lane_key in mine]
            shards = plan_shards(index, max_requests=self.shard_cap)
        self.index = index
        self.shards = shards
        caps |= _store_caps(self.store, self.since_ms, self.until_ms)
        caps |= extensions.capabilities_present(self.store, self.record_stores,
                                                since_ms=self.since_ms, until_ms=self.until_ms,
                                                notes=self.notes)
        self.caps = frozenset(caps)
        self.post_median = common.median_tokens(posts)
        from tokenbill.core.transitions import static_prefix_floor

        reads = self.store.lane_first_reads(since_ms=self.since_ms, until_ms=self.until_ms)
        self.floor = static_prefix_floor(reads)

    def thresholds(self, min_usd: str | None = None) -> dict[str, str]:
        """``AnalysisContext.thresholds`` (as ``pipeline.common.analysis_thresholds``, with the
        org median of the survey pass instead of a second pass over the lanes)."""
        out = dict(self.env.config.thresholds)
        out[common.MIN_USD_KEY] = min_usd if min_usd is not None else self.env.config.min_usd
        if common.COMPACTION_POST_TOKENS_KEY not in out and self.post_median is not None:
            out[common.COMPACTION_POST_TOKENS_KEY] = str(self.post_median)
        return dict(sorted(out.items()))

    # ---------- 4: the model gate ----------

    def calibrate(self, granularity: str = "day") -> CalibrationReport:
        from tokenbill.sim import calibrate as cal

        if granularity not in cal.GRANULARITIES:
            raise UsageError(f"--granularity must be one of {', '.join(cal.GRANULARITIES)}")
        task = _Calibrate(self.source(), self.env.pricer, self.env.rules, granularity,
                          self.floor)
        parts1 = self.map(task, self.shards)
        rho = cal.fit_rho_by_fold(parts1, folds=5)
        parts2 = self.map(dataclasses.replace(task, rho=rho), self.shards)
        return cal.finish_calibration(parts1, parts2, granularity=granularity, folds=5)

    # ---------- 5: reconciliation and enrichment ----------

    def reconcile(self) -> tuple[ReconciliationReport | None, list[ReconciliationReport]]:
        """RECON's report over the window (when the store holds provider records) merged with the
        extensions' (when an extension has data); returns ``(merged or None, extension reports)``.
        """
        from tokenbill.recon import reconcile as rc

        window = {"since_ms": self.since_ms, "until_ms": self.until_ms}
        reports: list[ReconciliationReport] = []
        ext_reports: list[ReconciliationReport] = []
        if any(c.startswith("ext:") for c in self.caps):
            ext_reports = extensions.run_reconcilers(
                self.store, self.record_stores, self.env.pricer, since_ms=self.since_ms,
                until_ms=self.until_ms, tolerance_pct=_TOLERANCE_PCT,
                unexplained_pct=_UNEXPLAINED_PCT, closed_only=False, today=self.today,
                notes=self.notes)
        aggregates = self.store.aggregates(None, **window)
        cost_lines = self.store.cost_lines(None, **window)
        if aggregates or cost_lines:
            reports.append(rc.reconcile(
                self.store.iter_usage_records(**window), aggregates, cost_lines, self.env.pricer,
                tolerance_pct=Decimal(_TOLERANCE_PCT), unexplained_pct=Decimal(_UNEXPLAINED_PCT),
                today=self.today))
        reports.extend(ext_reports)
        if not reports:
            return None, ext_reports
        return rc.merge_reports(reports), ext_reports

    def context(self, *, calibration: CalibrationReport | None, min_usd: str | None = None,
                break_glass: str | None = None,
                reconciliation: ReconciliationReport | None = None,
                ext_reports: Sequence[ReconciliationReport] = ()) -> AnalysisContext:
        from tokenbill.sim.usage_replay import UsageReplayer

        window = {"since_ms": self.since_ms, "until_ms": self.until_ms}
        ctx = AnalysisContext(
            pricer=self.env.pricer, rules=self.env.rules, replayer=UsageReplayer(),
            calibration=calibration, window=self.window, capabilities=self.caps,
            thresholds=self.thresholds(min_usd), k_anonymity=self.env.k,
            self_principal=self.principal if self.self_view else None,
            break_glass=break_glass, now_ms=self.until_ms,
            static_prefix_floor=dict(self.floor),
            aggregates=tuple(self.store.aggregates(None, **window)),
            cost_lines=tuple(self.store.cost_lines(None, **window)))
        reconciled: frozenset[str] = frozenset()
        if reconciliation is not None:
            from tokenbill.recon.reconcile import reconciled_channels

            reconciled = reconciled_channels(reconciliation)
        return extensions.enrich(self.store, self.record_stores, ctx, today=self.today,
                                 reconciled_channels=reconciled,
                                 recon_decisions=extensions.recon_decisions_of(ext_reports),
                                 notes=self.notes)

    # ---------- 6: detectors ----------

    def detect(self, ctx: AnalysisContext, only: Sequence[str] | None) -> list[Finding]:
        from tokenbill.core.registry import run_detectors

        found: list[DataQualityNote] = []
        from tokenbill.core.registry import all_detectors

        if only is None:
            all_detectors(notes=found)   # records dq.detector_unavailable once per missing module
            self.notes.extend(found)
        parts = self.map(_Detect(self.source(), ctx, tuple(only) if only else None), self.shards)
        once = run_detectors([], ctx, only=list(only) if only else None, emit_missing=True,
                             aggregates_only=True)
        return merge_findings([*parts, once])

    # ---------- 7: publication ----------

    def publish(self, findings: Sequence[Finding], *, break_glass: str | None,
                actor: str) -> tuple[list[Finding], int]:
        """k-anonymous findings (the self view publishes only the principal's own findings,
        unsuppressed) and the number withheld or merged."""
        if self.self_view:
            return list(findings), 0
        revealed: list[Finding] = []
        rest: list[Finding] = []
        for f in findings:
            if (break_glass and f.detector_id == _TAIL
                    and any(k == "session" for k, _v in f.scope.dims)):
                revealed.append(f)
            else:
                rest.append(f)
        count = extensions.count_users_fn(self.store, self.record_stores,
                                          since_ms=self.since_ms, until_ms=self.until_ms,
                                          notes=self.notes)
        published = kanon.rescope_findings(rest, k=self.env.k, count_users=count)
        if break_glass:
            self.store.audit(actor, _DQ_BREAK_GLASS, {
                "reason": break_glass[:400], "detector": _TAIL, "findings": len(revealed),
                "window": f"{_date(self.since_ms)}..{_date(self.until_ms - 1)}"})
            self.text_notes.append(f"break-glass: {len(revealed)} runaway session(s) named "
                                   "(team and session pseudonym only); an audit row was written")
        out = merge_findings([published, revealed])
        return out, max(0, len(rest) - len(published))

    # ---------- 8: plan ----------

    def plan(self, findings: Sequence[Finding], ctx: AnalysisContext, *, seed: int,
             sample_lanes: int, include_tradeoffs: bool) -> ActionPlan:
        from tokenbill.plan.action_plan import build_action_plan
        from tokenbill.plan.realization import observed_rr

        days = max(1, -(-(self.until_ms - self.since_ms) // DAY_MS))
        rr = observed_rr(self.store.receipts()) or None
        return build_action_plan(findings, self.index, self.source(), self.shards, ctx,
                                 window_days=days, include_tradeoffs=include_tradeoffs,
                                 seed=seed, sample_lanes=sample_lanes, observed_rr=rr,
                                 map_shards=self.mapper())


def attach_shapley(findings: Sequence[Finding], plan: ActionPlan) -> list[Finding]:
    """Findings with ``recoverable_shapley`` / ``projected_monthly`` from the plan's levers: each
    lever's Shapley credit (and monthly projection) is split over the findings it names in
    proportion to their standalone recoverable points (equally when those are zero or unpriced),
    and a finding sums the shares of its levers (Shapley credits of distinct levers add). Only
    levers with a credit in the finding's basis count. Ranked by (−credit, detector, id)."""
    by_id = {f.finding_id: f for f in findings}
    shares: dict[str, list[Figure]] = {}
    monthly: dict[str, list[Figure]] = {}
    for lever in plan.levers:
        linked = [by_id[i] for i in lever.finding_ids if i in by_id]
        if not linked or lever.shapley.nano is None or not lever.group.startswith(
                ("billed:", "allowance:", "pool:")):
            continue
        weights = [_point(f.recoverable) for f in linked]
        total = sum(weights)
        for i, f in enumerate(linked):
            if not _same_basis(f, lever.shapley.basis):
                continue
            num, den = (weights[i], total) if total > 0 else (1, len(linked))
            shares.setdefault(f.finding_id, []).append(scale(lever.shapley, num, den))
            if lever.projected_monthly.nano is not None:
                monthly.setdefault(f.finding_id, []).append(
                    scale(lever.projected_monthly, num, den))
    out = []
    for f in findings:
        credit = _sum(shares.get(f.finding_id, []))
        proj = _sum(monthly.get(f.finding_id, []))
        if credit is not None:
            f = dataclasses.replace(f, recoverable_shapley=credit,
                                    projected_monthly=proj if proj is not None
                                    else f.projected_monthly)
        out.append(f)
    out.sort(key=shapley_rank)
    return out


def _point(fig: Figure | None) -> int:
    return max(0, fig.nano) if fig is not None and fig.nano is not None else 0


def _same_basis(f: Finding, basis: Basis) -> bool:
    ref = f.recoverable if f.recoverable is not None else f.cost_observed
    return ref.basis is basis


def _sum(figs: Sequence[Figure]) -> Figure | None:
    from tokenbill.core.labels import add

    if not figs:
        return None
    return functools.reduce(add, figs[1:], figs[0])


def shapley_rank(f: Finding) -> tuple[int, str, str]:
    """``(−(Shapley credit, else recoverable) point, detector id, finding id)``."""
    fig = f.recoverable_shapley if f.recoverable_shapley is not None else f.recoverable
    point = fig.nano if fig is not None and fig.nano is not None else 0
    return (-point, f.detector_id, f.finding_id)


def _group_findings(findings: Sequence[Finding], dim: str) -> tuple[list[Finding], list[str]]:
    """Findings ordered by their *dim* scope value (then rank), and one count note per group."""
    def value(f: Finding) -> str:
        return dict(f.scope.dims).get(dim) or "(all)"

    ordered = sorted(findings, key=lambda f: (value(f), shapley_rank(f)))
    counts: dict[str, int] = {}
    for f in ordered:
        counts[value(f)] = counts.get(value(f), 0) + 1
    notes = [f"{dim} {name}: {n} finding(s)" for name, n in counts.items()]
    return ordered, notes


def _principal_of(env: Env, ref: str | None) -> str | None:
    if ref is None:
        return None
    from tokenbill.core.ids import is_opaque_ref

    if not is_opaque_ref(ref):
        raise UsageError("--principal-ref must match [A-Za-z0-9._-]{1,64} (never an email)")
    if env.org_key is None:
        raise UsageError("--principal-ref needs the org key (key_file in the config)")
    return pseudonym(env.org_key, "p", ref)


def _analysis(store: LedgerStore, env: Env, *, db_path: Path | None, since_ms: int | None,
              until_ms: int | None, today: str | None, jobs: int | None,
              shard_max_requests: int | None, self_view: bool, principal_ref: str | None,
              principal: str | None = None, team: str | None = None,
              record_stores: Sequence[ExtRecordStore] | None = None) -> _Analysis:
    lo, hi = resolve_window(store, since_ms, until_ms)
    notes: list[DataQualityNote] = []
    stores = list(record_stores) if record_stores is not None else _record_stores(db_path, notes)
    who = principal if principal is not None else _principal_of(env, principal_ref)
    cap = shard_max_requests if shard_max_requests is not None else \
        env.config.shard_max_requests
    if type(cap) is not int or cap < 1:
        raise UsageError("the shard cap must be a positive int")
    n_jobs = jobs if jobs is not None else env.jobs
    if type(n_jobs) is not int or n_jobs < 1:
        raise UsageError("--jobs must be an int >= 1")
    run = _Analysis(store=store, env=env, db_path=Path(db_path) if db_path else None,
                    since_ms=lo, until_ms=hi, today=today or today_of(env), principal=who,
                    self_view=self_view, team=team, jobs=n_jobs, shard_cap=cap,
                    record_stores=stores, notes=notes)
    run.notes.extend(_rate_notes(env.pricer, run.today))
    return run


# =============================================================================================
# public pipeline functions
# =============================================================================================


def run_calibrate(store: LedgerStore, env: Env, *, db_path: Path | None = None,
                  since_ms: int | None = None, until_ms: int | None = None,
                  granularity: str = "day", jobs: int | None = None,
                  shard_max_requests: int | None = None, today: str | None = None) -> RunResult:
    """The model gate (SPEC §9.6) over the window, streamed in two passes over the shards."""
    run = _analysis(store, env, db_path=db_path, since_ms=since_ms, until_ms=until_ms,
                    today=today, jobs=jobs, shard_max_requests=shard_max_requests,
                    self_view=False, principal_ref=None)
    run.survey()
    report = run.calibrate(granularity)
    return RunResult(command="calibrate", window=run.window, inputs=(),
                     privacy=_privacy(env, identity_mode="central"),
                     rate_card=rate_card_info(env.pricer, run.today),
                     data_quality=tuple(run.notes), calibration=report)


@dataclass(frozen=True)
class _FindingsRun:
    run: _Analysis
    ctx: AnalysisContext
    findings: list[Finding]
    plan: ActionPlan | None
    calibration: CalibrationReport
    reconciliation: ReconciliationReport | None
    suppressed: int
    copilot: Any


def _findings_run(run: _Analysis, *, detectors: Sequence[str] | None, min_usd: str | None,
                  break_glass: str | None, shapley: bool, include_tradeoffs: bool, seed: int,
                  sample_lanes: int | None, granularity: str, actor: str,
                  calibration: CalibrationReport | None = None) -> _FindingsRun:
    if break_glass is not None and (not isinstance(break_glass, str) or not break_glass.strip()):
        raise UsageError("--break-glass needs a reason")
    if break_glass is not None and run.self_view:
        raise UsageError("--break-glass is an org-view action (not with --self)")
    if min_usd is not None:
        from tokenbill.config import Config as _C

        _C(min_usd=min_usd)          # validates (UsageError)
    run.survey()
    if calibration is None:
        calibration = run.calibrate(granularity)
    reconciliation, ext_reports = run.reconcile()
    ctx = run.context(calibration=calibration, min_usd=min_usd, break_glass=break_glass,
                      reconciliation=reconciliation, ext_reports=ext_reports)
    raw = run.detect(ctx, detectors)
    findings, suppressed = run.publish(raw, break_glass=break_glass, actor=actor)
    plan = None
    if shapley:
        plan = run.plan(findings, ctx, seed=seed,
                        sample_lanes=sample_lanes if sample_lanes is not None
                        else run.env.config.sample_lanes, include_tradeoffs=include_tradeoffs)
        findings = attach_shapley(findings, plan)
    copilot = None
    if "ext:copilot" in ctx.capabilities:
        summaries = extensions.summarize(run.store, run.record_stores, ctx, findings, plan,
                                         run.env.pricer, today=run.today, k=run.env.k,
                                         notes=run.notes)
        copilot = summaries.get("copilot")
    return _FindingsRun(run, ctx, findings, plan, calibration, reconciliation, suppressed,
                        copilot)


def run_findings(store: LedgerStore, env: Env, *, db_path: Path | None = None,
                 since_ms: int | None = None, until_ms: int | None = None,
                 detectors: Sequence[str] | None = None, min_usd: str | None = None,
                 group_by: str | None = None, self_view: bool = False,
                 principal_ref: str | None = None, break_glass: str | None = None,
                 shapley: bool = True, include_tradeoffs: bool = False, seed: int = 0,
                 sample_lanes: int | None = None, jobs: int | None = None,
                 shard_max_requests: int | None = None, today: str | None = None,
                 record_stores: Sequence[ExtRecordStore] | None = None, persist: bool = True,
                 granularity: str = "day", actor: str = "cli") -> RunResult:
    """Detectors over the ledger, ranked by Shapley-credited recoverable dollars (see the module
    docstring for every step). *db_path* (the store's file) enables ``--jobs`` process pools;
    without it the shards run in this process. ``group_by`` orders the findings by one scope
    dimension (``principal`` only with *self_view*: ``PrivacyError``); ``break_glass`` names
    runaway sessions (audited); ``shapley=False`` skips the plan."""
    if group_by is not None:
        if group_by not in FINDINGS_GROUP_BY:
            raise UsageError(f"--group-by must be one of {', '.join(FINDINGS_GROUP_BY)}")
        kanon.require_self_or_aggregate([group_by], "self" if self_view else None)
    run = _analysis(store, env, db_path=db_path, since_ms=since_ms, until_ms=until_ms,
                    today=today, jobs=jobs, shard_max_requests=shard_max_requests,
                    self_view=self_view, principal_ref=principal_ref,
                    record_stores=record_stores)
    fr = _findings_run(run, detectors=detectors, min_usd=min_usd, break_glass=break_glass,
                       shapley=shapley, include_tradeoffs=include_tradeoffs, seed=seed,
                       sample_lanes=sample_lanes, granularity=granularity, actor=actor)
    findings = fr.findings
    notes = list(run.text_notes)
    if group_by is not None:
        findings, group_notes = _group_findings(findings, group_by)
        notes.extend(group_notes)
    if persist and not self_view:
        store.put_findings(_run_id("findings", run.window, env.pricer, seed), findings)
    return RunResult(command="findings", window=run.window, inputs=(),
                     privacy=_privacy(env, identity_mode="self" if self_view else "central",
                                      suppressed=fr.suppressed),
                     rate_card=rate_card_info(env.pricer, run.today),
                     data_quality=tuple(run.notes), calibration=fr.calibration,
                     findings=tuple(findings), action_plan=fr.plan, notes=tuple(notes),
                     copilot=fr.copilot)


def run_whatif(store: LedgerStore, env: Env, *, policies: Sequence[str],
               db_path: Path | None = None, since_ms: int | None = None,
               until_ms: int | None = None, mode: str = "documented", shapley: bool = False,
               sample_lanes: int | None = None, seed: int = 0, jobs: int | None = None,
               shard_max_requests: int | None = None, today: str | None = None) -> RunResult:
    """Counterfactual replays of *policies* (SPEC §9.5 grammar) over the window, per billing
    class (a replay never mixes billed and allowance lanes), in the documented and/or calibrated
    mode (the model gate runs when ``calibrated`` is asked). ``compact-window`` clauses without
    ``post=`` get the org median summary size (R-E24). With *shapley* the policies are the
    players of an exact (≤ 6) or Monte Carlo Shapley split of their joint saving on a seeded
    stratified sample; the credits are reported as notes."""
    from tokenbill.core.policy import parse_policy
    from tokenbill.core.shards import merge_replay, stratified_sample

    if mode not in ("documented", "calibrated", "both"):
        raise UsageError("--mode must be documented, calibrated or both")
    specs = list(dict.fromkeys(policies))
    if not specs:
        raise UsageError("whatif needs at least one --policy SPEC")
    parsed = [parse_policy(s) for s in specs]
    run = _analysis(store, env, db_path=db_path, since_ms=since_ms, until_ms=until_ms,
                    today=today, jobs=jobs, shard_max_requests=shard_max_requests,
                    self_view=False, principal_ref=None)
    run.survey()
    parsed = [common.with_compaction_post(p, run.post_median) for p in parsed]
    modes = ["documented", "calibrated"] if mode == "both" else [mode]
    calibration = run.calibrate() if "calibrated" in modes else None
    ctx = run.context(calibration=calibration)
    source = run.source()
    replays: list[ReplayResult] = []
    for policy in parsed:
        for m in modes:
            parts = run.map(_Replay(source, ctx, policy, m), run.shards)
            classes = sorted({c for part in parts for c in part})
            for cls in classes:
                replays.append(merge_replay([part[cls] for part in parts if cls in part]))
    notes: list[str] = []
    if not run.shards:
        notes.append("no lanes in the window: nothing to replay")
    if shapley and len(parsed) > 1 and run.index:
        n = sample_lanes if sample_lanes is not None else env.config.sample_lanes
        sample = stratified_sample(run.index, n=n, seed=seed)
        notes.extend(_whatif_shapley(parsed, specs, source, ctx, sample, seed,
                                     modes[-1], len(run.index)))
    elif shapley:
        notes.append("shapley: needs at least two policies")
    return RunResult(command="whatif", window=run.window, inputs=(),
                     privacy=_privacy(env, identity_mode="central"),
                     rate_card=rate_card_info(env.pricer, run.today),
                     data_quality=tuple(run.notes), calibration=calibration,
                     replays=tuple(replays), notes=tuple(notes))


def _whatif_shapley(parsed: Sequence[Any], specs: Sequence[str], source: LaneSource,
                    ctx: AnalysisContext, sample: frozenset[str], seed: int, mode: str,
                    total_lanes: int) -> list[str]:
    from tokenbill.core.money import fmt_usd
    from tokenbill.core.policy import combine
    from tokenbill.core.shapley import MAX_EXACT_PLAYERS, shapley_exact, shapley_mc

    by_spec = dict(zip(specs, parsed, strict=True))
    task = _Replay(source, ctx, None, mode, lane_keys=sample)
    unpriced = False

    def value(coalition: frozenset[str]) -> int:
        nonlocal unpriced
        if not coalition:
            return 0
        members = [by_spec[s] for s in sorted(coalition)]
        policy = functools.reduce(combine, members[1:], members[0])
        total = 0
        for result in dataclasses.replace(task, policy=policy)(None).values():
            if result.saving.nano is None:
                unpriced = True
            else:
                total += result.saving.nano
        return total

    if len(specs) <= min(6, MAX_EXACT_PLAYERS):
        credits, method = shapley_exact(specs, value), "exact"
    else:
        credits, _se = shapley_mc(specs, value, seed=seed)
        method = "monte carlo (200 permutations)"
    lines = [f"shapley ({method}, {mode}, sample of {len(sample)}/{total_lanes} lanes, seed "
             f"{seed}; estimated, not scaled to the full scope):"]
    lines += [f"  {spec}: {fmt_usd(credits[spec])}" for spec in specs]
    if unpriced:
        lines.append("  some coalitions changed unpriced requests: their savings count as $0")
    return lines


def run_policy(store: LedgerStore, env: Env, *, target: str = "claude-code",
               db_path: Path | None = None, since_ms: int | None = None,
               until_ms: int | None = None, current: Mapping[str, object] | None = None,
               cohort_by: str | None = None, include_tradeoffs: bool = False,
               contract: Path | None = None, out_dir: Path | None = None, seed: int = 0,
               jobs: int | None = None, shard_max_requests: int | None = None,
               today: str | None = None,
               record_stores: Sequence[ExtRecordStore] | None = None) -> RunResult:
    """Per-cohort policy packs (SPEC §11.3) from the findings path's plan: the built-in targets
    (``claude-code``, ``litellm``, ``sdk``) through PLAN's ``build_policy_packs`` (with
    ``rates.contract.to_model_pricing`` as the model-pricing emitter), extension targets (e.g.
    ``github-copilot``) through ``core.extensions.policy_packs``. With *out_dir* every pack is
    written (``plan.policy_pack.render_pack``). Nothing is ever applied."""
    from tokenbill.plan import policy_pack as pp

    ext_targets = extensions.policy_targets()
    if target not in pp.TARGETS and target not in ext_targets:
        known = sorted({*pp.TARGETS, *ext_targets})
        raise UsageError(f"unknown --target {target!r} (one of {', '.join(known)})")
    run = _analysis(store, env, db_path=db_path, since_ms=since_ms, until_ms=until_ms,
                    today=today, jobs=jobs, shard_max_requests=shard_max_requests,
                    self_view=False, principal_ref=None, record_stores=record_stores)
    fr = _findings_run(run, detectors=None, min_usd=None, break_glass=None, shapley=True,
                       include_tradeoffs=include_tradeoffs, seed=seed, sample_lanes=None,
                       granularity="day", actor="cli")
    notes = list(run.text_notes)
    packs: list[PolicyPack]
    if target in pp.TARGETS:
        overlay = None
        emitter = None
        if contract is not None:
            from tokenbill.rates.contract import load_contract, to_model_pricing

            overlay = load_contract(Path(contract))
            emitter = to_model_pricing
        assert fr.plan is not None
        packs = pp.build_policy_packs(fr.plan, fr.findings, target=target, current=current,
                                      cohort_by=cohort_by, include_tradeoffs=include_tradeoffs,
                                      contract=overlay, model_pricing_emitter=emitter)
    else:
        partial = RunResult(command="policy", window=run.window, inputs=(),
                            privacy=_privacy(env, identity_mode="central"), rate_card=None,
                            findings=tuple(fr.findings), action_plan=fr.plan,
                            copilot=fr.copilot)
        packs = extensions.policy_packs(target, store, run.record_stores, fr.ctx, fr.findings,
                                        partial, out_dir=out_dir, current=current,
                                        cohort_by=cohort_by or "team",
                                        include_tradeoffs=include_tradeoffs, notes=run.notes)
    if not packs:
        notes.append(f"no {target} pack: no lever to deliver in this window")
    if out_dir is not None and target in pp.TARGETS:
        written = [p for pack in packs for p in pp.render_pack(pack, Path(out_dir))]
        notes.append(f"wrote {len(written)} file(s) under the output directory")
    return RunResult(command="policy", window=run.window, inputs=(),
                     privacy=_privacy(env, identity_mode="central", suppressed=fr.suppressed),
                     rate_card=rate_card_info(env.pricer, run.today),
                     data_quality=tuple(run.notes), calibration=fr.calibration,
                     findings=tuple(fr.findings), action_plan=fr.plan,
                     policy_packs=tuple(packs), notes=tuple(notes), copilot=fr.copilot)


def run_check_effect(store: LedgerStore, env: Env, *, lever_id: str, cohort: str = "all",
                     since_ms: int, days: int = 7, target: str | None = None,
                     today: str | None = None) -> RunResult:
    """Post-rollout effectiveness (SPEC §11.5): a ``setting-not-effective`` finding when the
    rolled-out value of *lever_id* is not observed in *cohort*'s lanes within ``[since, since +
    days)``, else none (a note says it is in effect or that there is no evidence)."""
    from tokenbill.plan.effectiveness import check_effect

    if type(days) is not int or not 1 <= days <= 366:
        raise UsageError("--days must be an int in [1, 366]")
    until = since_ms + days * DAY_MS
    lanes = list(store.iter_lanes(since_ms=since_ms, until_ms=until))
    finding = check_effect(lanes, lever_id=lever_id, cohort=cohort, since_ms=since_ms, days=days,
                           target=target)
    notes = []
    if finding is None:
        notes.append(f"{lever_id} in cohort {cohort}: in effect, or no evidence in the window "
                     f"({len(lanes)} lane(s))")
    day = today or today_of(env)
    return RunResult(command="policy check-effect", window=(since_ms, until), inputs=(),
                     privacy=_privacy(env, identity_mode="central"),
                     rate_card=rate_card_info(env.pricer, day),
                     findings=(finding,) if finding is not None else (), notes=tuple(notes))


# =============================================================================================
# scan (self view) and scan --org
# =============================================================================================


def _scan_env(env: Env) -> Env:
    key = env.org_key if env.org_key is not None else SCAN_KEY
    name = env.name_key if env.name_key is not None else key
    return dataclasses.replace(env, org_key=key, name_key=name, name_key_id=key_id(name))


def _inputs(store: LedgerStore, sources: Sequence[SourceInfo],
            notes: Sequence[DataQualityNote]) -> tuple[tuple[SourceInfo, int, int], ...]:
    out = []
    for src in sources:
        records = 0
        stats = getattr(store, "source_stats", None)
        if callable(stats):
            got = stats(adapter=src.adapter)
            records = int(got.get("records", got.get("requests", 0)))
        quarantined = sum(n.count for n in notes if n.code == "dq.quarantined"
                          and n.detail.startswith(src.adapter))
        out.append((src, records, quarantined))
    return tuple(out)


@dataclass
class _TempStore:
    """A ledger at *db_path*, or a temporary one deleted on close (``scan``'s default)."""

    env: Env
    db_path: Path | None
    adopt: bool = False
    _tmp: tempfile.TemporaryDirectory | None = None  # type: ignore[type-arg]
    store: Any = None
    path: Path | None = None

    def __enter__(self) -> _TempStore:
        if self.db_path is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="tokenbill-scan-")
            self.path = Path(self._tmp.name) / "ledger.db"
        else:
            self.path = Path(self.db_path).expanduser()
        self.store = common.open_store(self.path, self.env, create=True,
                                       adopt_key_ids=self.adopt)
        return self

    def __exit__(self, *exc: object) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        if self._tmp is not None:
            self._tmp.cleanup()


def _bill(store: LedgerStore, env: Env, window: tuple[int, int], *, audience: str,
          group_by: Sequence[str] = ("model",)) -> BillSummary:
    return common.bill_summary(store, env, since_ms=window[0], until_ms=window[1],
                               group_by=list(group_by), audience=audience)


def run_scan(env: Env, *, claude_dir: Path | None = None, since_ms: int | None = None,
             until_ms: int | None = None, db_path: Path | None = None,
             billing_path: str | None = None, jobs: int | None = None, seed: int = 0,
             today: str | None = None) -> RunResult:
    """One-shot local Claude Code review, self view (SPEC §15 ``scan``; ``me`` is an alias):
    discover the transcripts under *claude_dir* (default ``~/.claude/projects``) → the Claude Code
    adapter with the install identity (``p_`` of :data:`SELF_REF` under the org key, else
    :data:`SCAN_KEY`) and *billing_path* (default: config threshold ``scan.billing_path``, else
    ``unknown``) → a temporary ledger (or *db_path*, kept) priced with the Env pricer → the
    findings path (self view: nothing is suppressed) → bill, data quality, calibration, findings
    and the action plan."""
    root = Path(claude_dir if claude_dir is not None else DEFAULT_CLAUDE_DIR).expanduser()
    if not root.exists():
        raise UsageError("no Claude Code transcripts found (pass --claude-dir DIR)")
    path = billing_path if billing_path is not None else env.config.thresholds.get(
        "scan.billing_path")
    if path is not None and path not in BILLING_PATHS:
        raise UsageError(f"--billing-path must be one of {', '.join(BILLING_PATHS)}")
    senv = _scan_env(env)
    from tokenbill.core.records import Attribution

    attribution = Attribution(billing_path=path) if path is not None else Attribution()
    opts = common.ingest_options(senv, identity_mode="install", principal_ref=SELF_REF,
                                 attribution=attribution, since_ms=since_ms, until_ms=until_ms)
    with _TempStore(senv, db_path) as held:
        sources, ingest_notes = common.ingest_paths(held.store, [root], senv, opts,
                                                    adapter="claude-code")
        principal = pseudonym(senv.org_key, "p", SELF_REF)  # type: ignore[arg-type]
        run = _analysis(held.store, senv, db_path=held.path, since_ms=since_ms,
                        until_ms=until_ms, today=today, jobs=jobs, shard_max_requests=None,
                        self_view=True, principal_ref=None, principal=principal)
        run.notes[:0] = ingest_notes
        fr = _findings_run(run, detectors=None, min_usd=None, break_glass=None, shapley=True,
                           include_tradeoffs=False, seed=seed, sample_lanes=None,
                           granularity="day", actor="scan")
        bill = _bill(held.store, senv, run.window, audience="self")
        inputs = _inputs(held.store, sources, ingest_notes)
    notes = list(run.text_notes)
    if db_path is None:
        notes.append("scan used a temporary ledger (deleted); pass --db PATH to keep it")
    return RunResult(command="scan", window=run.window, inputs=inputs,
                     privacy=_privacy(senv, identity_mode="install"),
                     rate_card=rate_card_info(env.pricer, run.today), bill=bill,
                     data_quality=tuple(run.notes), calibration=fr.calibration,
                     reconciliation=fr.reconciliation, findings=tuple(fr.findings),
                     action_plan=fr.plan, notes=tuple(notes), copilot=fr.copilot)


def _pull_live(key_env: str, since_ms: int | None, until_ms: int | None, out_dir: Path,
               opener: Any, today: str) -> list[tuple[Path, str]]:
    from tokenbill.recon.pull import pull

    until = _date(until_ms) if until_ms is not None else today
    since = _date(since_ms) if since_ms is not None else _date(_date_ms(until) - 30 * DAY_MS)
    got: list[tuple[Path, str]] = []
    for kind, adapter in LIVE_PULLS:
        for path in pull(kind, key_env=key_env, since=since, until=until, out_dir=out_dir,
                         opener=opener):
            got.append((path, adapter))
    return got


def run_scan_org(env: Env, *, files: Mapping[str, Sequence[Path]] | None = None,
                 live: bool = False, admin_key_env: str | None = None,
                 team_map: Path | None = None, since_ms: int | None = None,
                 until_ms: int | None = None, db_path: Path | None = None,
                 opener: Any = None, today: str | None = None) -> RunResult:
    """Aggregate-only org scan (SPEC §10.3, §12.6, D32): the ADMIN adapters on the Admin /
    Analytics pages and the cloud billing exports (*files*: ``ORG_SCAN_ADAPTERS`` kind → paths),
    or a ``--live`` pull with the key in the environment variable *admin_key_env* → a temporary
    ledger → reconciliation (RECON + extensions) → the aggregate detectors (``aggregate.org-scan``
    and any extension's) → a reconciled view of the provider bill and the org-scan findings. No
    per-request ledger is needed."""
    groups = {k: list(v) for k, v in (files or {}).items() if v}
    unknown = sorted(set(groups) - set(ORG_SCAN_ADAPTERS))
    if unknown:
        raise UsageError(f"unknown org-scan input kind(s): {', '.join(unknown)}")
    if live and not admin_key_env:
        raise UsageError("--live needs --admin-key-env VAR (the Admin API key is read from that "
                         "environment variable only)")
    if not live and not groups:
        raise UsageError("scan --org needs provider files (--usage-report, --cost-report, …) "
                         "or --live")
    day = today or today_of(env)
    opts_kw: dict[str, Any] = {"since_ms": since_ms, "until_ms": until_ms}
    if team_map is not None:
        opts_kw["team_map"] = common.load_team_map(Path(team_map))
    opts = common.ingest_options(env, **opts_kw)
    with tempfile.TemporaryDirectory(prefix="tokenbill-org-") as tmp, \
            _TempStore(env, db_path) as held:
        reads: list[tuple[Path, str]] = []
        if live:
            assert admin_key_env is not None
            reads += _pull_live(admin_key_env, since_ms, until_ms, Path(tmp), opener, day)
        for kind in ORG_SCAN_ADAPTERS:
            reads += [(Path(p), ORG_SCAN_ADAPTERS[kind]) for p in groups.get(kind, [])]
        sources: list[SourceInfo] = []
        notes: list[DataQualityNote] = []
        for path, adapter in reads:
            got, found = common.ingest_paths(held.store, [path], env, opts, adapter=adapter)
            sources += got
            notes += found
        run = _analysis(held.store, env, db_path=held.path, since_ms=since_ms,
                        until_ms=until_ms, today=day, jobs=1, shard_max_requests=None,
                        self_view=False, principal_ref=None)
        run.notes[:0] = notes
        run.survey()
        reconciliation, ext_reports = run.reconcile()
        ctx = run.context(calibration=None, reconciliation=reconciliation,
                          ext_reports=ext_reports)
        from tokenbill.core.registry import run_detectors

        raw = run_detectors([], ctx, emit_missing=False, aggregates_only=True)
        findings, suppressed = run.publish(raw, break_glass=None, actor="scan-org")
        inputs = _inputs(held.store, sources, notes)
    text = ["org scan: provider-side aggregates only (no per-request ledger); the bill is the "
            "providers' invoice lines, see the reconciliation"]
    if reconciliation is None:
        text.append("no usage or cost records in the inputs: nothing to reconcile")
    return RunResult(command="scan --org", window=run.window, inputs=inputs,
                     privacy=_privacy(env, identity_mode="central-ingest", suppressed=suppressed),
                     rate_card=rate_card_info(env.pricer, day), data_quality=tuple(run.notes),
                     reconciliation=reconciliation, findings=tuple(findings), notes=tuple(text))


# =============================================================================================
# report, demo --fleet
# =============================================================================================


def _team_bill(store: LedgerStore, env: Env, window: tuple[int, int], team: str) -> BillSummary:
    where = {"team": team}
    raw = store.aggregate(since_ms=window[0], until_ms=window[1], group_by=(), where=where)
    total = raw.rows[0].priced if raw.rows else _zero_total(env.pricer)
    by_model = store.aggregate(since_ms=window[0], until_ms=window[1], group_by=("model",),
                               where=where)
    return BillSummary(total=total, esr=None,
                       breakdowns=(("model", kanon.publish(by_model, k=env.k)),),
                       footnotes=("team page: the bill of this team only",))


def _zero_total(pricer: Pricer) -> Any:
    from tokenbill.core.labels import exact
    from tokenbill.core.types import PricedTotal

    basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST
    return PricedTotal(exact=exact(0, basis), estimated=None, allowance=None,
                       priced_inferences=0, unpriced_inferences=0, unpriced_tokens=0,
                       coverage="1")


def _calibration_from_json(path: Path) -> CalibrationReport:
    """A ``CalibrationReport`` from a ``calibrate --format json`` document (result@2)."""
    try:
        doc = load_json_exact(Path(path), max_bytes=64 << 20)
    except SourceError as exc:
        raise UsageError(f"calibration {exc}") from None
    cal = doc.get("calibration") if isinstance(doc, dict) else None
    if not isinstance(cal, dict):
        raise UsageError("calibration file: not a tokenbill calibrate --format json document")
    try:
        th = cal["thresholds"]
        return CalibrationReport(
            granularity=_s(cal["granularity"]), n_periods=_i(cal["n_periods"]),
            status=_s(cal["status"]), mode_used=_os(cal.get("mode_used")),
            nmbe_pct=_os(cal.get("nmbe_pct")), cvrmse_pct=_os(cal.get("cvrmse_pct")),
            nmbe_pct_calibrated=_os(cal.get("nmbe_pct_calibrated")),
            cvrmse_pct_calibrated=_os(cal.get("cvrmse_pct_calibrated")),
            thresholds=(_s(th["nmbe_pct"]), _s(th["cvrmse_pct"])),
            rho=tuple((_s(r["gap_band"]), _i(r["hits"]), _i(r["trials"]), _s(r["wilson_low"]),
                       _s(r["wilson_high"])) for r in cal.get("rho", [])),
            diag_confusion=tuple((_s(r["predicted"]), _s(r["server_reason"]), _i(r["count"]))
                                 for r in cal.get("diag_confusion", [])),
            diag_precision_recall=tuple((_s(r["class"]), _s(r["precision"]), _s(r["recall"]))
                                        for r in cal.get("diag_precision_recall", [])),
            unlabeled=_i(cal.get("unlabeled", 0)),
            no_comparison_labels=_i(cal.get("no_comparison_labels", 0)),
            ttl_corroboration=tuple(_i(x) for x in cal.get("ttl_corroboration", [0, 0]))[:2],
            notes=tuple(_s(n) for n in cal.get("notes", [])))
    except (KeyError, TypeError, AttributeError, ValueError):
        raise UsageError("calibration file: malformed calibration object") from None


def _s(v: object) -> str:
    if not isinstance(v, str) or len(v) > 4096:
        raise ValueError("string")
    return v


def _os(v: object) -> str | None:
    return None if v is None else _s(v)


def _i(v: object) -> int:
    if type(v) is not int or not -(2**63) <= v <= 2**63:
        raise ValueError("int")
    return v


def run_report(store: LedgerStore, env: Env, *, db_path: Path | None = None,
               since_ms: int | None = None, until_ms: int | None = None,
               team: str | None = None, self_view: bool = False,
               principal_ref: str | None = None, calibration: Path | None = None,
               reconciliation: Path | None = None, seed: int = 0, jobs: int | None = None,
               shard_max_requests: int | None = None, today: str | None = None,
               record_stores: Sequence[ExtRecordStore] | None = None) -> RunResult:
    """The fleet / team / self report (SPEC §14.3 renders it as HTML): bill, data quality,
    reconciliation (RECON + extension reconcilers over the window, A-11), calibration, findings
    and the action plan. *team* restricts to one team (``PrivacyError`` below k people); a
    *reconciliation* file (``reconcile --format json``) is noted but the report reconciles the same
    window itself (decisions are never persisted); a *calibration* file (``calibrate --format
    json``) replaces the model gate run."""
    if team is not None and self_view:
        raise UsageError("--team and --self are exclusive")
    run = _analysis(store, env, db_path=db_path, since_ms=since_ms, until_ms=until_ms,
                    today=today, jobs=jobs, shard_max_requests=shard_max_requests,
                    self_view=self_view, principal_ref=principal_ref, team=team,
                    record_stores=record_stores)
    if team is not None:
        people = store.count_users(since_ms=run.since_ms, until_ms=run.until_ms,
                                   where={"team": team})
        if people < env.k:
            raise PrivacyError("this team has fewer than k people: its figures are only shown "
                               "inside the org report")
    loaded = _calibration_from_json(calibration) if calibration is not None else None
    fr = _findings_run(run, detectors=None, min_usd=None, break_glass=None, shapley=True,
                       include_tradeoffs=False, seed=seed, sample_lanes=None,
                       granularity="day", actor="report", calibration=loaded)
    notes = list(run.text_notes)
    if reconciliation is not None:
        notes.append("the reconciliation shown was recomputed for this window (decisions are "
                     "never read back from files)")
    if team is not None:
        bill = _team_bill(store, env, run.window, team)
    else:
        audience = "self" if self_view else "org"
        bill = _bill(store, env, run.window, audience=audience,
                     group_by=("model",) if self_view else ("team", "model"))
    return RunResult(command="report", window=run.window, inputs=(),
                     privacy=_privacy(env, identity_mode="self" if self_view else "central",
                                      suppressed=fr.suppressed),
                     rate_card=rate_card_info(env.pricer, run.today), bill=bill,
                     data_quality=tuple(run.notes), reconciliation=fr.reconciliation,
                     calibration=fr.calibration, findings=tuple(fr.findings),
                     action_plan=fr.plan, notes=tuple(notes), copilot=fr.copilot)


#: Top-level directories of ``synth.fleet`` source files read through the real adapters in
#: ``demo --fleet``: the provider pages and the CUR export (the canonical records carry the lane
#: data; the transcript / OTLP / trace@2 / execution files of the same lanes would duplicate it).
FLEET_FILE_FAMILIES = ("admin", "cur")


def run_demo_fleet(*, seed: int = 7, out_dir: Path | None = None, jobs: int = 1,
                   env: Env | None = None, pricer_factory: Callable[..., Pricer] | None = None,
                   devs: int = 61, days: int = 28) -> RunResult:
    """The synthetic fleet end to end (SPEC §18; ``demo --fleet``): ``synth.fleet.generate``
    (files written under *out_dir*, else a temporary directory) → the canonical lane records and
    the written Admin pages and CUR export through the real ADMIN adapters → a temporary ledger
    under the fleet's public demo keys (keyless, networkless) → reconciliation, the findings path
    and the action plan → a ``RunResult`` marked ``synthetic``. Byte-identical for a seed."""
    from tokenbill.synth import fleet

    if env is None:
        env = common.build_env(Config(jobs=jobs), pricer_factory=pricer_factory, now_ms=0)
    fenv = dataclasses.replace(env, org_key=fleet.FLEET_ORG_KEY, name_key=fleet.FLEET_NAME_KEY,
                               name_key_id=key_id(fleet.FLEET_NAME_KEY))
    with tempfile.TemporaryDirectory(prefix="tokenbill-fleet-") as tmp:
        base = Path(out_dir) if out_dir is not None else Path(tmp) / "fleet"
        world = fleet.generate(seed, devs=devs, days=days, out_dir=base)
        fenv = dataclasses.replace(fenv, now_ms=_date_ms(world.today))
        with _TempStore(fenv, Path(tmp) / "ledger.db") as held:
            result = world.ingest_result()
            result.aggregates = []
            result.cost_lines = []
            held.store.ingest(result, pricer=fenv.pricer)
            dirs = sorted({base / family for family in FLEET_FILE_FAMILIES
                           if (base / family).is_dir()})
            sources, notes = common.ingest_paths(held.store, dirs, fenv, world.ingest_options())
            run = _analysis(held.store, fenv, db_path=held.path, since_ms=world.since_ms,
                            until_ms=world.until_ms, today=world.today, jobs=jobs,
                            shard_max_requests=None, self_view=False, principal_ref=None)
            run.notes[:0] = notes
            fr = _findings_run(run, detectors=None, min_usd=None, break_glass=None,
                               shapley=True, include_tradeoffs=False, seed=seed,
                               sample_lanes=None, granularity="day", actor="demo")
            bill = _bill(held.store, fenv, run.window, audience="org",
                         group_by=("team", "model", "billing_path"))
            inputs = _inputs(held.store, [result.source, *sources], notes)
    text = [f"SYNTHETIC DATA: tokenbill demo --fleet (seed {seed}, {devs} developers, {days} "
            "days); every number is generated, nothing describes a real organization",
            *run.text_notes]
    return RunResult(command="demo --fleet", window=run.window, inputs=inputs,
                     privacy=_privacy(fenv, identity_mode="central-ingest",
                                      suppressed=fr.suppressed),
                     rate_card=rate_card_info(fenv.pricer, world.today), bill=bill,
                     data_quality=tuple(run.notes), reconciliation=fr.reconciliation,
                     calibration=fr.calibration, findings=tuple(fr.findings),
                     action_plan=fr.plan, synthetic=True, notes=tuple(text),
                     copilot=fr.copilot)


# =============================================================================================
# check (CI gate)
# =============================================================================================


def _check_lanes(paths: Sequence[Path]) -> tuple[list[Lane], dict[str, str], list[str]]:
    from tokenbill.core.lanes import group_lanes
    from tokenbill.core.registry import sniff_adapter

    lanes: list[Lane] = []
    files: dict[str, str] = {}
    warnings: list[str] = []
    opts = IngestOptions(content_tier=ContentTier.FINGERPRINT, name_key=CHECK_KEY,
                         name_key_id=key_id(CHECK_KEY), identity_mode="install")
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise UsageError(f"{path.name}: not a file")
        adapter = sniff_adapter(path)
        if adapter is None or adapter.name not in ("trace@1", "trace@2"):
            raise UsageError(f"{path.name}: check reads trace@1 or trace@2 files")
        result = adapter.read(path, opts)
        if result.quarantined:
            warnings.append(f"{path.name}: {len(result.quarantined)} record(s) quarantined")
        for lane in group_lanes(result.requests, result.events, result.sessions):
            files.setdefault(lane.session_key, path.name)
            lanes.append(lane)
    return lanes, files, warnings


def run_check(paths: Sequence[Path] = (), *, env: Env, store: LedgerStore | None = None,
              since_ms: int | None = None, until_ms: int | None = None,
              options: Any = None, baseline: Path | None = None) -> CheckResult:
    """The CI gate (SPEC §15.2): the lanes of trace@1 / trace@2 *paths* (fingerprinted on import)
    or of *store*'s window, judged by :func:`tokenbill.gate.run_check` against *baseline* (a
    previous ``check --format json`` output)."""
    return run_check_result(paths, env=env, store=store, since_ms=since_ms, until_ms=until_ms,
                            options=options, baseline=baseline).check  # type: ignore[return-value]


def run_check_result(paths: Sequence[Path] = (), *, env: Env, store: LedgerStore | None = None,
                     since_ms: int | None = None, until_ms: int | None = None,
                     options: Any = None, baseline: Path | None = None) -> RunResult:
    """:func:`run_check` wrapped in a ``RunResult`` (what ``check`` renders and ``--write-baseline``
    saves): the lanes' window, the rate card and the ``check`` slot."""
    from tokenbill import gate

    if bool(paths) == (store is not None):
        raise UsageError("check reads TRACE files or --db, not both")
    base = gate.load_baseline(baseline) if baseline is not None else None
    if store is not None:
        lo, hi = resolve_window(store, since_ms, until_ms)
        lanes = list(store.iter_lanes(since_ms=lo, until_ms=hi))
        files: dict[str, str] = {}
        warnings: list[str] = []
    else:
        lanes, files, warnings = _check_lanes(paths)
    if not lanes:
        raise UsageError("check: the inputs hold no requests")
    check = gate.run_check(lanes, pricer=env.pricer, rules=env.rules, options=options,
                           baseline=base, files=files, notes=warnings)
    starts = [req.ts_start_ms for lane in lanes for req in lane.requests]
    window = (min(starts), max(starts) + 1) if starts else (0, 1)
    return RunResult(command="check", window=window, inputs=(),
                     privacy=_privacy(env, identity_mode="install",
                                      tier=ContentTier.FINGERPRINT if paths else ContentTier.NONE),
                     rate_card=rate_card_info(env.pricer, today_of(env)), check=check)


# =============================================================================================
# command-line glue (tokenbill.commands.*): flags, Env, output, exit codes
# =============================================================================================

#: Exit codes (SPEC §15).
EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, EXIT_GATE, EXIT_DQ = 0, 1, 2, 3, 4
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def date_arg(text: str) -> int:
    """argparse type: ``YYYY-MM-DD`` (UTC) → ms since the epoch."""
    if not isinstance(text, str) or not _DATE_RE.match(text.strip()):
        raise argparse.ArgumentTypeError("expected a date YYYY-MM-DD")
    try:
        return _date_ms(text.strip())
    except UsageError:
        raise argparse.ArgumentTypeError("expected a valid date YYYY-MM-DD") from None


def positive_int(text: str) -> int:
    """argparse type: an int ≥ 1 (at most 2**31)."""
    try:
        value = int(str(text).strip(), 10)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer") from None
    if not 1 <= value <= 2**31:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def non_negative_int(text: str) -> int:
    """argparse type: an int ≥ 0 (at most 2**31)."""
    try:
        value = int(str(text).strip(), 10)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a non-negative integer") from None
    if not 0 <= value <= 2**31:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return value


def decimal_arg(text: str) -> str:
    """argparse type: a finite, non-negative decimal (kept as its string)."""
    from decimal import InvalidOperation

    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("expected a decimal number") from None
    if not value.is_finite() or value < 0 or len(str(text)) > 64:
        raise argparse.ArgumentTypeError("expected a non-negative decimal number")
    return str(text).strip()


def add_db(parser: argparse.ArgumentParser, *, required: bool = False,
           help_text: str = "the ledger (SQLite) to analyze") -> None:
    """``--db PATH``. The default is suppressed so a global ``--db`` given before the verb is
    kept (argparse subparser defaults would overwrite it)."""
    parser.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS, help=help_text
                        + (" (required)" if required else ""))


def add_format(parser: argparse.ArgumentParser, choices: Sequence[str] = ("text", "json")
               ) -> None:
    """``--format`` (default suppressed; the global flag or ``text`` applies)."""
    parser.add_argument("--format", choices=list(choices), default=argparse.SUPPRESS,
                        help=f"output format ({', '.join(choices)}; default text)")


def add_window(parser: argparse.ArgumentParser) -> None:
    """``--since`` / ``--until`` (UTC dates; the window is ``[since, until)``)."""
    parser.add_argument("--since", type=date_arg, metavar="DATE", default=None,
                        help="window start, YYYY-MM-DD (UTC, inclusive; default: first day with "
                             "data)")
    parser.add_argument("--until", type=date_arg, metavar="DATE", default=None,
                        help="window end, YYYY-MM-DD (UTC, exclusive; default: the day after the "
                             "last day with data)")


def add_run_flags(parser: argparse.ArgumentParser) -> None:
    """``--jobs`` and ``--shard-max-requests`` (defaults suppressed / from the config)."""
    parser.add_argument("--jobs", type=positive_int, metavar="N", default=argparse.SUPPRESS,
                        help="process pool over shards (results identical for any N; default "
                             "from the config, 1)")
    parser.add_argument("--shard-max-requests", type=positive_int, metavar="N", default=None,
                        help="split a team's shard per lane kind above N requests (default "
                             "from the config, 250000)")


def fmt_of(args: argparse.Namespace, default: str = "text") -> str:
    """The output format of *args* (verb flag, else global flag, else *default*)."""
    value = getattr(args, "format", None)
    return value if isinstance(value, str) and value else default


def env_from_args(args: argparse.Namespace, *, overrides: Mapping[str, object] | None = None,
                  rates: Sequence[Path] = (), contract: Path | None = None,
                  model_prices: Sequence[tuple[str, str, str]] = (),
                  environ: Mapping[str, str] | None = None) -> Env:
    """The command's Env: ``load_config(--config, TOKENBILL_* env, flags)`` → ``build_env``.
    ``--jobs`` (verb or global) overrides the config; *overrides* adds verb flags (e.g.
    ``min_usd``)."""
    cfg_path = getattr(args, "config", None)
    layer: dict[str, object] = dict(overrides or {})
    jobs = getattr(args, "jobs", None)
    if jobs is not None:
        layer["jobs"] = jobs
    config = load_config(Path(cfg_path) if cfg_path else None,
                         os.environ if environ is None else environ, layer)
    factory = PRICER_FACTORY
    return common.build_env(config, rates=rates, contract=contract, model_prices=model_prices,
                            pricer_factory=factory, now_ms=_NOW_MS)


#: Test hooks: a pricer factory (``None`` = the RATES rate card) and a fixed clock.
PRICER_FACTORY: Callable[..., Pricer] | None = None
_NOW_MS: int | None = None


def open_ledger(args: argparse.Namespace, env: Env, *, required: bool = True
                ) -> tuple[LedgerStore | None, Path | None]:
    """The ledger named by ``--db`` (``UsageError`` when missing and *required*)."""
    raw = getattr(args, "db", None)
    if not raw:
        if required:
            raise UsageError("this command needs --db PATH (the ledger)")
        return None, None
    path = Path(raw).expanduser()
    return common.open_store(path, env, create=False), path


def close_ledger(store: object) -> None:
    """Close *store* when it has ``close``."""
    close = getattr(store, "close", None)
    if callable(close):
        close()


def render(result: RunResult, fmt: str, *, deterministic: bool = False, width: int = 100) -> str:
    """*result* as terminal text or ``tokenbill/result@2`` JSON (newline-terminated)."""
    if fmt == "json":
        from tokenbill.outputs.result_json import dumps_result

        text = dumps_result(result, deterministic=deterministic)
        return text if text.endswith("\n") else text + "\n"
    from tokenbill.outputs.terminal import render_terminal

    return render_terminal(result, width=width)


def write_text(path: Path, text: str) -> None:
    """Write *text* to *path* owner-only (parents created)."""
    path = Path(path).expanduser()
    with open_private(path, "w") as handle:
        handle.write(text)


def emit(result: RunResult, args: argparse.Namespace, *, out: IO[str] | None = None,
         html_out: Path | None = None) -> None:
    """Print *result* in the chosen format (unless ``--quiet`` with text) and write the HTML
    report to *html_out* when given."""
    stream = out if out is not None else sys.stdout
    fmt = fmt_of(args)
    deterministic = bool(getattr(args, "deterministic", False))
    if html_out is not None:
        from tokenbill.outputs.html import render_html

        write_text(html_out, render_html(result))
    if fmt == "text" and getattr(args, "quiet", False):
        return
    stream.write(render(result, fmt, deterministic=deterministic))


def dq_exit(result: RunResult, args: argparse.Namespace, env: Env | None) -> int:
    """4 under ``--strict-dq`` when the data-quality warnings reach the configured threshold."""
    if not getattr(args, "strict_dq", False):
        return EXIT_OK
    threshold = env.config.strict_dq_threshold if env is not None else 1
    if _dq_warnings(result.data_quality) >= max(threshold, 1):
        return EXIT_DQ
    return EXIT_OK


def exit_code(exc: BaseException) -> int:
    """The exit code of an exception (SPEC §15): gates 3, usage and privacy 2, others 1."""
    if isinstance(exc, GateFailed):
        return EXIT_GATE
    if isinstance(exc, (UsageError, PrivacyError)):
        return EXIT_USAGE
    return EXIT_RUNTIME


def _message(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:400]


def run_command(body: Callable[[], int], *, verb: str, err: IO[str] | None = None) -> int:
    """Run a command body, mapping expected failures to exit codes with one content-free line on
    stderr (never a traceback for a user error)."""
    stream = err if err is not None else sys.stderr
    try:
        return body()
    except TokenbillError as exc:
        code = exit_code(exc)
        kind = {EXIT_GATE: "gate failed", EXIT_USAGE: "usage error"}.get(code, "error")
        stream.write(f"tokenbill {verb}: {kind}: {_message(exc)}\n")
        return code
    except (OSError, ValueError) as exc:
        stream.write(f"tokenbill {verb}: error: {type(exc).__name__}: {_message(exc)}\n")
        return EXIT_RUNTIME


def load_json_arg(path: Path, what: str, *, max_bytes: int = 16 << 20) -> object:
    """A JSON file named by a flag; failures are ``UsageError`` naming *what* only."""
    try:
        return load_json_exact(Path(path).expanduser(), max_bytes=max_bytes)
    except SourceError as exc:
        raise UsageError(f"{what} {exc}") from None


def json_text(obj: object) -> str:
    """Canonical indented JSON (sorted keys, ASCII) with a trailing newline."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def iter_dates(since: str, days: int) -> Iterator[str]:
    """``days`` consecutive UTC dates starting at *since*."""
    start = _dt.date.fromisoformat(since)
    for i in range(days):
        yield (start + _dt.timedelta(days=i)).isoformat()

