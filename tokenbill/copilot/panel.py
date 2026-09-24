"""GitHub Copilot verification panels: cluster × day rows from AI usage report cells at constant
prices (addendum §13; SPEC §13.1, R8; CP-RECON).

:func:`build_copilot_panel` is the extension's ``panel_builder`` (``measure --panel copilot`` via
``core.extensions.panel``). It returns ``PanelRow``s that VERIFY's estimators consume unchanged:

* **Costs.** Report tokens of every AI-credit cell (``core.pool.build_cells``, day grain) are
  repriced at the pre-registered **baseline** card (``cost_baseline_nano``, R8) and at the
  **actual** card (``cost_actual_nano``, for the EXACT rate variance), with the point rates of each
  card's ``Pricer.unit_rates`` (base rates with modifiers; cells sum many requests, so never a
  long-context band). The token convention of each AI usage report file is the one
  :mod:`tokenbill.copilot.recon` decides with the actual card (``excl`` when undecidable, R-E46).
  Pseudo cells (code review, cloud agent, …) enter at their reported gross in both columns; a
  cell a card cannot price counts 0 in that column (as VERIFY's panel does); legacy
  premium-request cells are not token usage and are left out. The unit is list-equivalent Copilot
  credits (nano-USD).
* **Clusters.** ``cluster_kind`` ``team`` (default), ``cost_center`` or ``org``: the cell's team,
  cost center or organization; cells without one are outside every cluster.
* **Active developer-days** = distinct principals with an ``ActivityDay`` that day in the cluster
  (``ActivityDay.team`` / ``cost_center``; for ``org`` the principal's organizations from the seat
  snapshots of the window). Record stores keep only rows under accepted key ids (their own or the
  adopted export key, R-E21) and the count never joins rows across key ids. Counts are raw; k ≥ 5
  applies when a panel is published. Without activity rows a cluster-day has 0 developer-days.
* **Arms.** ``arms`` maps a cluster to ``"<arm>"`` or ``"<arm>@YYYY-MM-DD"`` (the adoption date).
  ``control`` / ``holdback`` are never treated; a dated arm is treated from its date on; an arm
  without a date is treated on every day of the window (the assignment preceded it). Without an
  entry a row has arm None and is untreated. ``wave`` and ``outcome_prs`` are None.

Rows are ordered by (cluster, date); one row per cluster-day with a cell or an activity row.
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from collections.abc import Mapping, Sequence

from tokenbill.copilot import recon as _recon
from tokenbill.core import pool as _pool
from tokenbill.core.errors import UsageError
from tokenbill.core.protocols import ExtRecordStore, LedgerStore, Pricer
from tokenbill.core.types import PanelRow

__all__ = ["CLUSTER_KINDS", "CONTROL_ARMS", "build_copilot_panel"]

#: Cluster kinds a Copilot panel can use.
CLUSTER_KINDS = ("team", "cost_center", "org")
#: Arm labels that mean "never treated" (as VERIFY's panel).
CONTROL_ARMS = frozenset({"control", "holdback"})

_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)


def _date_ms(value: object, name: str) -> int:
    if not isinstance(value, str):
        raise UsageError(f"{name}: expected a YYYY-MM-DD string")
    try:
        day = _dt.date.fromisoformat(value)
    except ValueError:
        raise UsageError(f"{name}: expected a YYYY-MM-DD string") from None
    if len(value) != 10:
        raise UsageError(f"{name}: expected a YYYY-MM-DD string")
    return (day - _EPOCH).days * _DAY_MS


def _parse_arms(arms: Mapping[str, str] | None) -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for cluster, value in (arms or {}).items():
        if not isinstance(cluster, str) or not isinstance(value, str) or not value:
            raise UsageError("arms: cluster → '<arm>' or '<arm>@YYYY-MM-DD' strings")
        arm, sep, date = value.partition("@")
        if not arm:
            raise UsageError("arms: an arm label must be non-empty")
        if sep:
            _date_ms(date, "arm adoption date")
        out[cluster] = (arm, date if sep else None)
    return out


def _cluster(kind: str, cell: _pool.Cell) -> str | None:
    value = {"team": cell.team, "cost_center": cell.cost_center, "org": cell.org}[kind]
    return value or None


def build_copilot_panel(store: LedgerStore, record_stores: Sequence[ExtRecordStore], *,
                        cluster_kind: str = "team", since: str, until: str,
                        baseline_pricer: Pricer, actual_pricer: Pricer,
                        arms: Mapping[str, str] | None = None) -> list[PanelRow]:
    """Cluster × day ``PanelRow``s over ``[since, until)`` (UTC dates) from AI usage report cells;
    see the module docstring for costs, clusters, developer-days and arms."""
    if cluster_kind not in CLUSTER_KINDS:
        raise UsageError(f"cluster_kind: one of {', '.join(CLUSTER_KINDS)}")
    lo, hi = _date_ms(since, "since"), _date_ms(until, "until")
    if hi <= lo:
        raise UsageError("until must be after since")
    assigned = _parse_arms(arms)
    inp = _recon._read_inputs(store, record_stores, lo, hi)
    cells_excl, _ = _pool.build_cells(inp.report_aggs, inp.lines, grain="day", convention="excl",
                                      capped=inp.capped)
    cells_incl, _ = _pool.build_cells(inp.report_aggs, inp.lines, grain="day", convention="incl",
                                      capped=inp.capped)
    sources = _recon.report_sources(inp.coverage)
    actual = _recon._Prices(actual_pricer)
    baseline = _recon._Prices(baseline_pricer)
    md_lines: dict[tuple[str, str, str], int] = defaultdict(int)
    for line in inp.lines:
        if line.source_kind == _recon.REPORT_KIND and line.pseudo is None:
            md_lines[(line.date_utc, line.model or "", line.workspace_id or "")] += 1
    decisions = _recon._decide(
        _recon._price_cells(cells_excl, cells_incl, actual, sources, inp.compliance), md_lines,
        _recon.DEFAULT_TOLERANCE_PCT, _recon.k_dated_boundaries())
    costs: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for ce, ci in zip(cells_excl, cells_incl, strict=True):
        date = ce.date_utc
        if date is None or ce.cost_type not in ("ai_credit.user", "ai_credit.direct", "other"):
            continue
        cluster = _cluster(cluster_kind, ce)
        if cluster is None:
            continue
        cell = costs[(cluster, date)]
        if ce.pseudo is not None or not ce.model:
            cell[0] += ce.gross_nano
            cell[1] += ce.gross_nano
            continue
        dec = decisions.get(sources.get(date, _recon.FALLBACK_SOURCE_ID))
        usage = ci.usage if dec is not None and dec.used == "incl" else ce.usage
        ctx = _recon._variant_ctx(ce, "base", inp.compliance)
        cell[0] += baseline.point(usage, ctx, date) or 0
        cell[1] += actual.point(usage, ctx, date) or 0
    devs = _dev_days(record_stores, cluster_kind, lo, hi)
    rows: list[PanelRow] = []
    for cluster, date in sorted(set(costs) | set(devs)):
        base, act = costs.get((cluster, date), (0, 0))
        arm, start = assigned.get(cluster, (None, None))
        if arm is None or arm in CONTROL_ARMS:
            treated = False
        else:
            treated = start is None or date >= start
        rows.append(PanelRow(cluster_id=cluster, date_utc=date, cost_baseline_nano=base,
                             cost_actual_nano=act, active_dev_days=len(devs.get((cluster, date),
                                                                                ())),
                             arm=arm, wave=None, treated=treated, outcome_prs=None))
    return rows


def _dev_days(record_stores: Sequence[ExtRecordStore], kind: str, lo: int, hi: int
              ) -> dict[tuple[str, str], set[str]]:
    """(cluster, date) → distinct principals with an ``ActivityDay`` (never ids in the output)."""
    stores = _recon._copilot_stores(record_stores)
    orgs: dict[str, set[str]] = defaultdict(set)
    if kind == "org":
        for store in stores:
            for lic in store.licenses(since_ms=lo, until_ms=hi):
                if lic.org:
                    orgs[lic.principal].add(lic.org)
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for store in stores:
        for day in store.activity(since_ms=lo, until_ms=hi):
            if kind == "team":
                clusters = {day.team} if day.team else set()
            elif kind == "cost_center":
                clusters = {day.cost_center} if day.cost_center else set()
            else:
                clusters = orgs.get(day.principal, set())
            for cluster in clusters:
                out[(cluster, day.date_utc)].add(day.principal)
    return out
