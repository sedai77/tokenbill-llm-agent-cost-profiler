"""GitHub Copilot analysis-context enricher (CP-STORE; addendum §3.2 CA-11, §7.2, §10.0;
CORE-AMENDMENTS C-14, C-17, C-26; rulings R-E21, R-E22, R-E46).

:func:`enrich_context` is ``ExtensionSpec.context_enricher`` of the ``copilot`` extension
(``core.extensions.enrich`` calls it): it extends an :class:`~tokenbill.core.types.AnalysisContext`
with everything every Copilot detector reads and returns a frozen replacement. It never prices
anything, never re-derives a reconciler decision and never re-implements the pool rule (every pool
figure comes from :mod:`tokenbill.core.pool`).

**Window (monthly grain).** The Copilot pool resets on the 1st (UTC), so the enricher reads the
calendar months the context window touches: ``[first day of the window's first month, first day
after its last month)``. Cost lines on the Copilot channels and Copilot aggregates of those months
are added to ``ctx.cost_lines`` / ``ctx.aggregates`` (rows already there are kept, ids
deduplicated);
``ctx.licenses`` / ``ctx.activity`` hold the record stores' rows of those months; ``ctx.outcomes``
gains the ``github.copilot_metrics`` outcome rows of those months; ``ctx.config`` holds the
configuration as of the window: count rows (``seat_counts``, ``activity_counts``, ``plan_quota``)
of the months, every ``run_flags`` snapshot up to the later of the window end and the end of
*today* (statements of this run; ``core.pool.run_flags`` merges them, the CLI snapshot over the
admin answers per key), and per other kind and entity the snapshots inside the months plus the
latest one before them (or, when neither exists, the earliest one after them).

**Cells and conventions.** CP-RECON decides the AI usage report's token convention per report file
(decision ``convention:<source_id>``). Cost lines carry no source; the report's coverage aggregates
(``github.ai_usage_report.coverage``, one per file × day, dim ``source``) name the files each day
comes from, so each day takes the decision of its covering file — the most recently fetched one when
exports overlap (the one whose rows won latest-fetch-wins), or a report aggregate's own ``source``
dim when no coverage row exists. :func:`decided_cells` builds day-grain cells with
``core.pool.build_cells`` once per convention over the days that carry it (``excl`` without a
decision; ``undecidable`` → ``excl`` plus ``dq.copilot_convention_undecidable``, R-E46).

**Plans and pools (R-E22).** ``core.pool.detect_plans`` per month with Copilot seat or usage data
→ ``ctx.plans``; ``core.pool.pool_months(…, plans=…)`` → ``ctx.pools`` (two ``PoolMonth`` per
entity × month while a plan is unknown), with ``gross_is_list`` from the decisions
``gross_is_list:<entity>:<YYYY-MM>``, ``recent_estimates`` from ``ActivityDay.reported_cost_nano``
(provider estimates, which ``pool_months`` keeps out of every report sum), promo eligibility,
billing modes and cap policies from the ``run_flags`` in ``ctx.config``. Seats are counted from the
licenses of one principal key id per month (R-E21: people under two key ids are never joined; the
key id with the most seat holders is used and the month's pool months carry
``dq.copilot_key_id_mixed``). Data-quality codes the enricher raises are appended to the affected
pool months' ``notes`` (the enricher signature has no notes list).

**Context.** ``reconciled_channels`` and ``recon_decisions`` are merged into the context's
(conflicting decision values raise ``ContractViolation``); ``capabilities`` gains ``ext:copilot``
and, for record rows it adds, ``licenses`` / ``activity`` / ``config``. A context whose window holds
no Copilot data (no Copilot cost line, aggregate, outcome, request, license, activity day or
configuration row) is returned unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from tokenbill.core import pool
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.protocols import ExtRecordStore, LedgerStore
from tokenbill.core.records import (
    COPILOT_AGG_SOURCE_KINDS,
    COPILOT_CHANNELS,
    COUNT_CONFIG_KINDS,
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
    record_key,
    to_json,
)
from tokenbill.core.registry import EXTENSIONS
from tokenbill.core.types import AnalysisContext, DataQualityNote, PlanEvidence, PoolMonth

__all__ = [
    "CAPABILITY",
    "COPILOT_OUTCOME_SOURCE",
    "COVERAGE_SOURCE_KIND",
    "DQ_CONVENTION_UNDECIDABLE",
    "DQ_KEY_ID_MIXED",
    "EXTENSION",
    "REPORT_SOURCE_KIND",
    "day_conventions",
    "decided_cells",
    "enrich_context",
]

#: The extension this enricher belongs to (``core.registry.EXTENSIONS`` key).
EXTENSION = "copilot"
#: The capability added when Copilot data exists.
CAPABILITY = f"ext:{EXTENSION}"
#: Source kind of the AI usage report's cell aggregates (tokens under a convention).
REPORT_SOURCE_KIND = "github.ai_usage_report"
#: Source kind of the report's per file × day coverage aggregates (dim ``source``).
COVERAGE_SOURCE_KIND = "github.ai_usage_report.coverage"
#: Source kind of Copilot outcome rows (usage metrics).
COPILOT_OUTCOME_SOURCE = "github.copilot_metrics"
#: R-E46: a report file whose token convention CP-RECON could not decide (``excl`` assumed).
DQ_CONVENTION_UNDECIDABLE = "dq.copilot_convention_undecidable"
#: Addendum §7.2: seat rows under more than one principal key id (joined by team only).
DQ_KEY_ID_MIXED = "dq.copilot_key_id_mixed"

_CONVENTION_PREFIX = "convention:"
_GROSS_PREFIX = "gross_is_list:"
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_MAX_MS = 253_402_300_800_000            # 10000-01-01T00:00:00Z (exclusive)
_COPILOT_CHANNEL = "github_copilot"
_EXTRA_AGG_SOURCE_KINDS = frozenset({*COPILOT_AGG_SOURCE_KINDS, "github.ai_usage_report.quota"})
_SEAT_OR_POOLED = frozenset({"seat", *pool.POOLED_COST_TYPES})


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _date_of_ms(ms: int) -> str:
    return (_EPOCH + _dt.timedelta(days=ms // _DAY_MS)).isoformat()


def _ms_of_date(date: _dt.date) -> int:
    return (date - _EPOCH).days * _DAY_MS


def _parse_today(today: object) -> _dt.date:
    if not isinstance(today, str) or len(today) != 10:
        raise UsageError("enrich_context: today must be a YYYY-MM-DD string")
    try:
        return _dt.date.fromisoformat(today)
    except ValueError:
        raise UsageError("enrich_context: today must be a YYYY-MM-DD string") from None


def _month_window(window: object) -> tuple[int, int] | None:
    """``[start of the first month, start of the month after the last)`` of a ``[start, end)``
    context window (None when the window is empty or outside 1970 … 9999)."""
    if (not isinstance(window, tuple) or len(window) != 2
            or not all(type(v) is int for v in window)):
        raise UsageError("enrich_context: ctx.window must be (start_ms, end_ms) ints")
    start, end = max(window[0], 0), min(window[1], _MAX_MS)
    if end <= start:
        return None
    first = _dt.date.fromisoformat(_date_of_ms(start)).replace(day=1)
    last = _dt.date.fromisoformat(_date_of_ms(end - 1))
    if last.month == 12:
        after_ms = (_MAX_MS if last.year == 9999
                    else _ms_of_date(_dt.date(last.year + 1, 1, 1)))
    else:
        after_ms = _ms_of_date(_dt.date(last.year, last.month + 1, 1))
    return _ms_of_date(first), after_ms


def _pairs(value: object, what: str) -> tuple[tuple[str, str], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise UsageError(f"enrich_context: {what} must be (key, value) string pairs")
    out: list[tuple[str, str]] = []
    for item in value:
        if (not isinstance(item, (tuple, list)) or len(item) != 2
                or not all(isinstance(x, str) for x in item)):
            raise UsageError(f"enrich_context: {what} must be (key, value) string pairs")
        out.append((item[0], item[1]))
    return tuple(out)


def _merge_decisions(*groups: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Union of decision pairs, sorted; one key with two values → ``ContractViolation``."""
    merged: dict[str, str] = {}
    for group in groups:
        for key, value in group:
            if merged.get(key, value) != value:
                raise ContractViolation("recon_decisions: one decision key with two values")
            merged[key] = value
    return tuple(sorted(merged.items()))


def _canonical(rec: object) -> str:
    return json.dumps(to_json(rec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_copilot_aggregate(agg: UsageAggregate) -> bool:
    if agg.source_kind in _EXTRA_AGG_SOURCE_KINDS or agg.source_kind.startswith("github."):
        return True
    return dict(agg.dims).get("channel") in COPILOT_CHANNELS


# ---------------------------------------------------------------------------------------------
# conventions and cells
# ---------------------------------------------------------------------------------------------


def _decision_map(recon_decisions: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {k[len(_CONVENTION_PREFIX):]: v for k, v in recon_decisions
            if k.startswith(_CONVENTION_PREFIX)}


def _report_aggregate(agg: UsageAggregate) -> bool:
    return (agg.source_kind == REPORT_SOURCE_KIND
            and dict(agg.dims).get("channel", _COPILOT_CHANNEL) == _COPILOT_CHANNEL)


def _day_sources(aggregates: Sequence[UsageAggregate]) -> dict[str, str]:
    """Day → the report file (``source`` dim) its rows come from: the most recently fetched
    coverage row of the day, else the most recently fetched report aggregate carrying a
    ``source`` dim."""
    coverage: dict[str, tuple[int, str]] = {}
    tagged: dict[str, tuple[int, str]] = {}
    for agg in aggregates:
        source = dict(agg.dims).get("source")
        if not source:
            continue
        day = _date_of_ms(agg.bucket_start_ms)
        table = (coverage if agg.source_kind == COVERAGE_SOURCE_KIND
                 else tagged if _report_aggregate(agg) else None)
        if table is None:
            continue
        cand = (agg.fetched_ms, source)
        if day not in table or cand > table[day]:
            table[day] = cand
    return {day: src for day, (_, src) in {**tagged, **coverage}.items()}


def day_conventions(aggregates: Iterable[UsageAggregate],
                    recon_decisions: Iterable[tuple[str, str]] = ()
                    ) -> tuple[dict[str, str], dict[str, str]]:
    """Per report day: the convention its cells are built with (``excl`` | ``incl``) and, for days
    whose file CP-RECON left ``undecidable``, that file's id (R-E46: ``excl`` assumed). Days without
    a known file or decision use ``excl``. Returns ``(day → convention, day → undecidable source)``.
    """
    aggs = list(aggregates)
    decisions = _decision_map(recon_decisions)
    sources = _day_sources(aggs)
    days = {_date_of_ms(a.bucket_start_ms) for a in aggs if _report_aggregate(a)} | set(sources)
    conventions: dict[str, str] = {}
    undecidable: dict[str, str] = {}
    for day in sorted(days):
        source = sources.get(day)
        decided = decisions.get(source) if source is not None else None
        if decided == "incl":
            conventions[day] = "incl"
        else:
            conventions[day] = "excl"
            if decided == "undecidable" and source is not None:
                undecidable[day] = source
    return conventions, undecidable


def _cells(aggregates: Sequence[UsageAggregate], cost_lines: Sequence[CostLine],
           recon_decisions: Iterable[tuple[str, str]], capped: Mapping[str, Decimal],
           entity_mode: str) -> tuple[list[pool.Cell], dict[str, str], int]:
    conventions, undecidable = day_conventions(aggregates, recon_decisions)
    groups = sorted(set(conventions.values()) | {"excl"})
    if groups == ["excl"]:
        cells, clamped = pool.build_cells(aggregates, cost_lines, grain="day", convention="excl",
                                          capped=capped, entity_mode=entity_mode)
        return cells, undecidable, clamped
    out: list[pool.Cell] = []
    clamped = 0
    for conv in groups:
        def mine(day: str, conv: str = conv) -> bool:
            return conventions.get(day, "excl") == conv
        part, n = pool.build_cells(
            [a for a in aggregates if mine(_date_of_ms(a.bucket_start_ms))],
            [c for c in cost_lines if mine(c.date_utc)], grain="day", convention=conv,
            capped=capped, entity_mode=entity_mode)
        out.extend(part)
        clamped += n
    # the groups partition the days, so a stable sort by day keeps build_cells' order per day
    out.sort(key=lambda c: (c.month, c.date_utc or ""))
    return out, undecidable, clamped


def decided_cells(aggregates: Iterable[UsageAggregate], cost_lines: Iterable[CostLine], *,
                  recon_decisions: Iterable[tuple[str, str]] = (),
                  capped: Mapping[str, Decimal] | None = None,
                  entity_mode: str = "enterprise"
                  ) -> tuple[list[pool.Cell], tuple[DataQualityNote, ...]]:
    """Day-grain Copilot cells (``core.pool.build_cells``) under the conventions CP-RECON decided
    (see :func:`day_conventions`): identical to ``build_cells(…, convention=c)`` when every day
    carries convention ``c``. Returns the cells and the data-quality notes
    (``dq.copilot_convention_undecidable`` with the number of undecidable report files)."""
    aggs, lines = list(aggregates), list(cost_lines)
    decisions = _pairs(recon_decisions, "recon_decisions")
    cells, undecidable, _ = _cells(aggs, lines, decisions, capped or {}, entity_mode)
    notes: tuple[DataQualityNote, ...] = ()
    if undecidable:
        n = len(set(undecidable.values()))
        notes = (DataQualityNote(code=DQ_CONVENTION_UNDECIDABLE, severity="warn", count=n,
                                 detail=f"report token convention undecidable for {n} file(s): "
                                        "excl assumed, token figures ESTIMATED"),)
    return cells, notes


# ---------------------------------------------------------------------------------------------
# record stores
# ---------------------------------------------------------------------------------------------


def _copilot_stores(record_stores: Sequence[ExtRecordStore]) -> list[ExtRecordStore]:
    """The record stores of this extension (``core.extensions`` rule: a store named ``copilot``,
    or one whose name is no extension's)."""
    names = set(EXTENSIONS)
    return [rs for rs in record_stores
            if getattr(rs, "name", None) == EXTENSION or getattr(rs, "name", None) not in names]


def _newest(table: dict[str, tuple[Any, str]], rec: Any, kid: str) -> None:
    key = record_key(rec)
    cur = table.get(key)
    if cur is None or (rec.fetched_ms, _canonical(rec)) > (cur[0].fetched_ms,
                                                           _canonical(cur[0])):
        table[key] = (rec, kid)


def _licenses(stores: Sequence[ExtRecordStore], window: Mapping[str, int]
              ) -> list[tuple[LicenseSnapshot, str]]:
    """Licenses of the window with their principal key ids (``""`` when a store cannot tell),
    one per natural key over every store."""
    table: dict[str, tuple[Any, str]] = {}
    for rs in stores:
        with_ids = getattr(rs, "licenses_with_key_ids", None)
        if callable(with_ids):
            pairs = list(with_ids(**window))
        else:
            key_of = getattr(rs, "principal_key_id_of", None)
            pairs = [(lic, (key_of(lic) or "") if callable(key_of) else "")
                     for lic in rs.licenses(**window)]
        for lic, kid in pairs:
            _newest(table, lic, kid)
    return [table[k] for k in sorted(table, key=lambda k: (table[k][0].snapshot_date, k))]


def _activity(stores: Sequence[ExtRecordStore], window: Mapping[str, int]) -> list[ActivityDay]:
    table: dict[str, tuple[Any, str]] = {}
    for rs in stores:
        for day in rs.activity(**window):
            _newest(table, day, "")
    return [table[k][0] for k in sorted(table, key=lambda k: (table[k][0].date_utc, k))]


def _config(stores: Sequence[ExtRecordStore], window: Mapping[str, int]
            ) -> list[ConfigSnapshot]:
    table: dict[str, tuple[Any, str]] = {}
    for rs in stores:
        for cfg in rs.config(**window):
            _newest(table, cfg, "")
    return sorted((v[0] for v in table.values()), key=lambda c: (c.snapshot_ms, record_key(c)))


def _select_config(rows: Sequence[ConfigSnapshot], lo: int, hi: int) -> list[ConfigSnapshot]:
    """The configuration as of ``[lo, hi)`` (see the module docstring)."""
    first_month, last_month = _date_of_ms(lo)[:7], _date_of_ms(hi - 1)[:7]
    out: list[ConfigSnapshot] = []
    inside: set[tuple[str, str]] = set()
    before: dict[tuple[str, str], ConfigSnapshot] = {}
    after: dict[tuple[str, str], ConfigSnapshot] = {}
    for c in rows:
        if c.kind == "run_flags":
            out.append(c)
            continue
        in_window = lo <= c.snapshot_ms < hi
        if c.kind in COUNT_CONFIG_KINDS:
            month = dict(c.attrs).get("month") if c.kind == "plan_quota" else None
            if in_window or (isinstance(month, str)
                             and first_month <= month <= last_month):
                out.append(c)
            continue
        key = (c.kind, c.entity_id)
        if in_window:
            out.append(c)
            inside.add(key)
        elif c.snapshot_ms < lo:
            if key not in before or (c.snapshot_ms, c.fetched_ms) >= (before[key].snapshot_ms,
                                                                     before[key].fetched_ms):
                before[key] = c
        elif key not in after or (c.snapshot_ms, -c.fetched_ms) < (after[key].snapshot_ms,
                                                                  -after[key].fetched_ms):
            after[key] = c
    out.extend(before.values())
    out.extend(c for key, c in after.items() if key not in inside and key not in before)
    return sorted(out, key=lambda c: (c.snapshot_ms, record_key(c)))


def _seat_licenses(pairs: Sequence[tuple[LicenseSnapshot, str]]
                   ) -> tuple[list[LicenseSnapshot], set[str]]:
    """The licenses seats are counted from: per month, those of one principal key id (the one
    with the most seat holders, ties → the smaller id) when several are present (R-E21)."""
    by_month: dict[str, dict[str, list[LicenseSnapshot]]] = defaultdict(lambda: defaultdict(list))
    for lic, kid in pairs:
        by_month[lic.snapshot_date[:7]][kid].append(lic)
    out: list[LicenseSnapshot] = []
    mixed: set[str] = set()
    for month in sorted(by_month):
        groups = by_month[month]
        if len(groups) > 1:
            mixed.add(month)
            kid = min(groups, key=lambda k: (-len({x.principal for x in groups[k]}), k))
            out.extend(groups[kid])
        else:
            out.extend(next(iter(groups.values())))
    return out, mixed


def _recent_estimates(activity: Sequence[ActivityDay], capped: Mapping[str, Decimal]
                      ) -> list[tuple[str, str | None, int]]:
    """``(date, entity or None, nano)`` provider estimates (``ai_credits_used``) per day and
    capped cost center (None = the enterprise); never report data (R12)."""
    per: dict[tuple[str, str | None], int] = defaultdict(int)
    for day in activity:
        nano = day.reported_cost_nano
        if nano is None or nano <= 0:
            continue
        entity = pool.entity_of(day.cost_center, None, capped=capped, entity_mode="enterprise")
        per[(day.date_utc, None if entity == "enterprise" else entity)] += nano
    return [(d, e, n) for (d, e), n in sorted(per.items(), key=lambda kv: (kv[0][0],
                                                                         kv[0][1] or ""))]


def _data_months(cells: Sequence[pool.Cell], lines: Sequence[CostLine],
                 licenses: Sequence[LicenseSnapshot], config: Sequence[ConfigSnapshot],
                 lo: int, hi: int) -> list[str]:
    """Months of the window with seat or usage data (the months ``detect_plans`` runs for)."""
    first, last = _date_of_ms(lo)[:7], _date_of_ms(hi - 1)[:7]
    months = {c.month for c in cells}
    months.update(x.date_utc[:7] for x in lines
                  if x.channel == _COPILOT_CHANNEL and x.cost_type in _SEAT_OR_POOLED)
    months.update(x.snapshot_date[:7] for x in licenses)
    for c in config:
        if c.kind == "seat_counts":
            months.add(_date_of_ms(c.snapshot_ms)[:7])
        elif c.kind == "plan_quota":
            month = dict(c.attrs).get("month")
            if isinstance(month, str):
                months.add(month)
    return sorted(m for m in months if first <= m <= last)


def _union(existing: Sequence[Any], new: Iterable[Any], key: Any) -> tuple[Any, ...]:
    seen = {key(x) for x in existing}
    added = []
    for x in new:
        k = key(x)
        if k not in seen:
            seen.add(k)
            added.append(x)
    return (*existing, *added)


def _with_note(pm: PoolMonth, note: str) -> PoolMonth:
    return pm if note in pm.notes else dataclasses.replace(pm, notes=(*pm.notes, note))


def _requests_on_copilot(store: LedgerStore, lo: int, hi: int) -> bool:
    raw = store.aggregate(since_ms=lo, until_ms=hi, group_by=("channel",))
    return any(dict(row.dims).get("channel") in COPILOT_CHANNELS for row in raw.rows)


# ---------------------------------------------------------------------------------------------
# the enricher
# ---------------------------------------------------------------------------------------------


def enrich_context(store: LedgerStore, record_stores: Sequence[ExtRecordStore],
                   ctx: AnalysisContext, *, today: str, reconciled_channels: frozenset[str],
                   recon_decisions: Sequence[tuple[str, str]] = (),
                   entity_mode: str = "enterprise") -> AnalysisContext:
    """Extend *ctx* with the Copilot licenses, activity, configuration, outcomes, plan evidence and
    pool months of the months its window touches (see the module docstring).

    *recon_decisions* are CP-RECON's decision pairs (``core.extensions.recon_decisions_of``);
    *reconciled_channels* the channels the reconcilers reconciled; *today* (``YYYY-MM-DD``) dates
    open months and forecasts; *entity_mode* (``enterprise`` | ``org``, keyword beyond the hook
    signature, default ``enterprise``) selects the pool entities. Returns *ctx* itself when its
    window holds no Copilot data, else a frozen replacement."""
    if not isinstance(ctx, AnalysisContext):
        raise ContractViolation("enrich_context expects an AnalysisContext")
    today_d = _parse_today(today)
    if entity_mode not in pool.ENTITY_MODES:
        raise UsageError("enrich_context: entity_mode must be 'enterprise' or 'org'")
    channels = frozenset(reconciled_channels)
    if not all(isinstance(c, str) for c in channels):
        raise UsageError("enrich_context: reconciled_channels must be strings")
    decisions = _merge_decisions(_pairs(ctx.recon_decisions, "ctx.recon_decisions"),
                                 _pairs(recon_decisions, "recon_decisions"))
    bounds = _month_window(ctx.window)
    if bounds is None:
        return ctx
    lo, hi = bounds
    window = {"since_ms": lo, "until_ms": hi}
    horizon = max(hi, _ms_of_date(today_d) + _DAY_MS)

    lines = [c for c in store.cost_lines(None, **window) if c.channel in COPILOT_CHANNELS]
    aggs = [a for a in store.aggregates(None, **window) if _is_copilot_aggregate(a)]
    outcomes = [o for o in store.outcomes(**window) if o.source_kind == COPILOT_OUTCOME_SOURCE]
    stores = _copilot_stores(record_stores)
    lic_pairs = _licenses(stores, window)
    activity = _activity(stores, window)
    all_config = _config(stores, {"since_ms": 0, "until_ms": horizon})
    config = _select_config(all_config, lo, hi)
    has_data = bool(lines or aggs or outcomes or lic_pairs or activity
                    or any(lo <= c.snapshot_ms < hi for c in all_config))
    if not has_data and not _requests_on_copilot(store, lo, hi):
        return ctx

    capped = pool.capped_cost_centers(config)
    cells, undecidable, _ = _cells(aggs, lines, decisions, capped, entity_mode)
    seat_lics, mixed = _seat_licenses(lic_pairs)
    plans: list[PlanEvidence] = []
    for month in _data_months(cells, lines, seat_lics, config, lo, hi):
        plans.extend(pool.detect_plans(lines, seat_lics, config, month=month,
                                       entity_mode=entity_mode))
    gross = tuple((k, v) for k, v in decisions if k.startswith(_GROSS_PREFIX))
    pools = pool.pool_months(cells, lines, seat_lics, config, today=today_d.isoformat(),
                             promo_eligible=True,
                             recent_estimates=_recent_estimates(activity, capped),
                             gross_is_list=gross, plans=plans, entity_mode=entity_mode)
    undecidable_months: dict[str, set[str]] = defaultdict(set)
    for day, source in undecidable.items():
        undecidable_months[day[:7]].add(source)
    notes: dict[str, list[str]] = defaultdict(list)
    for month, sources in undecidable_months.items():
        notes[month].append(f"{DQ_CONVENTION_UNDECIDABLE}: report token convention undecidable "
                            f"for {len(sources)} file(s); excl assumed, token figures ESTIMATED")
    for month in mixed:
        notes[month].append(f"{DQ_KEY_ID_MIXED}: seat rows under several principal key ids; "
                            "seats counted under one key id, other rows joined by team only")
    for i, pm in enumerate(pools):
        for note in notes.get(pm.month, ()):
            pm = _with_note(pm, note)
        pools[i] = pm

    licenses = tuple(lic for lic, _ in lic_pairs)
    caps = set(ctx.capabilities) | {CAPABILITY}
    caps.update(name for name, rows in (("licenses", licenses), ("activity", activity),
                                        ("config", config)) if rows)
    return dataclasses.replace(
        ctx,
        capabilities=frozenset(caps),
        aggregates=_union(ctx.aggregates, aggs, key=lambda a: a.agg_id),
        cost_lines=_union(ctx.cost_lines, lines, key=lambda c: c.line_id),
        licenses=licenses,
        activity=tuple(activity),
        config=tuple(config),
        outcomes=_union(ctx.outcomes, outcomes,
                        key=lambda o: (o.date_utc, o.team, o.source_kind)),
        pools=tuple(pools),
        plans=tuple(plans),
        reconciled_channels=ctx.reconciled_channels | channels,
        recon_decisions=decisions,
    )
