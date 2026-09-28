"""Live pulls of GitHub billing, seats and Copilot configuration (addendum §5.12; CP-PULL).

The unit planners and runners :func:`tokenbill.copilot.pull_common.pull` uses for the kinds

* ``ai_usage`` / ``metered`` — the report export ``POST /enterprises/{e}/settings/billing/reports``
  (``report_type`` ``ai_credit`` / ``detailed``; ``summarized`` windows may span 366 days) with
  ``{report_type, start_date, end_date, send_email: false}`` in windows of at most 31 days (one
  unit each, clamped to today), polled every 30 s up to 30 min (then the unit is left incomplete
  with its export id, so ``--resume`` polls the same export instead of starting another; a 409
  "another export is being generated" waits within the same cap). Each ``download_urls`` entry is
  downloaded at once — with the token only when it points at a GitHub host — and stored verbatim
  as ``<unit id>.csv`` (``_partN`` for several parts, ``.csv.gz`` when compressed); the URL itself
  is never stored. Read by CP-BILL's ``github-ai-usage`` / ``github-metered-usage`` adapters.
* ``summary`` — per month: ``…/usage/summary`` and ``…/ai_credit/usage`` (``year``, ``month``),
  and ``…/usage`` per cost center including ``cost_center_id=none`` (the ids come from ``GET
  …/cost-centers``, which that unit reads but does not record). Read by ``github-billing-api``.
* ``seats`` — ``GET /enterprises/{e}/copilot/billing/seats`` and ``GET
  /orgs/{org}/copilot/billing/seats`` per organization (paged).
* ``config`` — budgets with every budget's ``user-states``, cost centers, and ``GET
  /orgs/{org}/copilot/billing`` per organization. Read by CP-ORGDATA's adapters.

Every REST page is recorded as a ``pull_common.envelope_line``. No money is computed here and no
float is used.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tokenbill.copilot.pull_common import (
    EXPORT_CAP_S,
    EXPORT_POLL_S,
    MAX_WAIT_S,
    P_AI_CREDIT_USAGE,
    P_BUDGETS,
    P_COST_CENTERS,
    P_ENTERPRISE_SEATS,
    P_EXPORT,
    P_EXPORT_STATUS,
    P_ORG_BILLING,
    P_ORG_SEATS,
    P_USAGE,
    P_USAGE_SUMMARY,
    P_USER_STATES,
    Outcome,
    PlannedUnit,
    PullError,
    PullScope,
    UnitContext,
    api_path,
    iter_pages,
    retry_after_seconds,
)

__all__ = [
    "ENTERPRISE_KINDS",
    "REPORT_TYPES",
    "REPORT_WINDOW_DAYS",
    "ExportState",
    "budget_ids",
    "cost_center_ids",
    "export_windows",
    "months",
    "parse_export",
    "plan",
]

logger = logging.getLogger(__name__)

#: Pull kind → report-export ``report_type``.
REPORT_TYPES = {"ai_usage": "ai_credit", "metered": "detailed"}
#: Longest window per report type (days, inclusive; addendum §19.3 #1, #4, #30).
REPORT_WINDOW_DAYS = {"ai_credit": 31, "detailed": 31, "premium_request": 31, "summarized": 366}
#: Kinds that need ``--github-enterprise`` (everything here except seats and org settings).
ENTERPRISE_KINDS = frozenset({"ai_usage", "metered", "summary"})
#: Export ``status`` values (lower-cased) meaning done / failed; anything else is still running
#: (the documented values are ``processing`` and ``completed``; the others are defensive).
_DONE = frozenset({"completed", "complete", "succeeded", "success", "done", "ready"})
_FAILED = frozenset({"failed", "failure", "error", "errored", "expired", "cancelled",
                     "canceled"})
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_NONE_CC = "none"


# ---------------------------------------------------------------------------------------------
# windows and parsers
# ---------------------------------------------------------------------------------------------


def export_windows(since: str, until: str, report_type: str) -> list[tuple[str, str]]:
    """Consecutive inclusive ``(start, end)`` windows covering ``[since, until]`` of at most
    :data:`REPORT_WINDOW_DAYS` days each."""
    step = REPORT_WINDOW_DAYS[report_type]
    start = _dt.date.fromisoformat(since)
    last = _dt.date.fromisoformat(until)
    out: list[tuple[str, str]] = []
    while start <= last:
        end = min(start + _dt.timedelta(days=step - 1), last)
        out.append((start.isoformat(), end.isoformat()))
        start = end + _dt.timedelta(days=1)
    return out


def months(since: str, until: str) -> list[tuple[int, int]]:
    """``(year, month)`` of every calendar month touching ``[since, until]``."""
    start = _dt.date.fromisoformat(since)
    last = _dt.date.fromisoformat(until)
    out: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (last.year, last.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


@dataclass(frozen=True)
class ExportState:
    """A report export as GitHub describes it: id, ``completed`` | ``processing`` | ``failed`` and
    the download links (kept in memory only)."""

    id: str
    status: str
    urls: tuple[str, ...]

    @property
    def completed(self) -> bool:
        """Whether the export is done."""
        return self.status == "completed"

    @property
    def failed(self) -> bool:
        """Whether GitHub reports the export as failed."""
        return self.status == "failed"


def parse_export(doc: Any) -> ExportState:
    """An export document (the POST answer or ``GET …/reports/{id}``; a ``usage_report_exports``
    list with one entry is accepted) → :class:`ExportState`; :class:`PullError` for any other
    shape. A status other than a known done / failed value counts as still processing; download
    links must be https URLs."""
    if isinstance(doc, dict) and isinstance(doc.get("usage_report_exports"), list):
        items = doc["usage_report_exports"]
        doc = items[0] if len(items) == 1 else None
    if not isinstance(doc, dict):
        raise PullError("report export: unexpected answer", "export_shape")
    rid = doc.get("id")
    if isinstance(rid, int) and not isinstance(rid, bool):
        rid = str(rid)
    if not isinstance(rid, str) or not _ID_RE.match(rid) or ".." in rid:
        raise PullError("report export: the answer has no usable id", "export_shape")
    raw_status = doc.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    links = doc.get("download_urls")
    urls: tuple[str, ...] = ()
    if links is not None:
        if not isinstance(links, list) or not all(isinstance(u, str) for u in links):
            raise PullError("report export: malformed download links", "export_shape")
        if not all(u.lower().startswith("https://") for u in links):
            raise PullError("report export: download links must be https", "bad_link")
        urls = tuple(links)
    if status in _FAILED:
        state = "failed"
    elif status in _DONE or (not status and urls):
        state = "completed"
    else:
        state = "processing"
    return ExportState(rid, state, urls)


def _ids(items: Any, key: str) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        value = item.get(key) if isinstance(item, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str) and _ID_RE.match(value) and ".." not in value \
                and value not in out:
            out.append(value)
    return out


def budget_ids(body: Any) -> list[str]:
    """The budget ids of a budgets page (``budgets[].id``; invalid ids are skipped)."""
    return _ids(body.get("budgets") if isinstance(body, dict) else None, "id")


def cost_center_ids(body: Any) -> list[str]:
    """The cost-center ids of a cost-centers page (``costCenters[].id``; invalid ids skipped)."""
    return _ids(body.get("costCenters") if isinstance(body, dict) else None, "id")


# ---------------------------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------------------------


def _run_export(ctx: UnitContext, kind: str, window: tuple[str, str]) -> Outcome:
    report_type = REPORT_TYPES[kind]
    ent = ctx.scope.enterprise
    client = ctx.client
    post_label = f"POST {P_EXPORT}"
    status_label = f"GET {P_EXPORT_STATUS}"
    waited = 0
    state: ExportState | None = None
    pending = ctx.previous.export_id if ctx.previous is not None else None
    if pending:
        resp = client.api("GET", api_path(P_EXPORT_STATUS, e=ent, report_id=pending),
                          label=status_label, passthrough=(404,))
        if resp.status == 200:
            state = parse_export(resp.json(status_label))
            if state.failed or state.id != pending:
                state = None
        if state is not None:
            ctx.export_id = state.id
            logger.info("unit %s: resuming report export", ctx.unit.id)
    payload = {"report_type": report_type, "start_date": window[0], "end_date": window[1],
               "send_email": False}
    while state is None:
        resp = client.api("POST", api_path(P_EXPORT, e=ent), payload=payload, label=post_label,
                          accept=(200, 201, 202), passthrough=(409,))
        if resp.status == 409:  # another export of this account is being generated
            if waited >= EXPORT_CAP_S:
                return Outcome("incomplete", reason="export_busy")
            after = retry_after_seconds(resp.header("retry-after"),
                                        client.clock.now_ms() // 1000)
            wait = min(max(after if after is not None else EXPORT_POLL_S, 1), MAX_WAIT_S)
            logger.info("unit %s: another report export is running; waiting %d s", ctx.unit.id,
                        wait)
            client.clock.sleep(wait)
            waited += wait
            continue
        state = parse_export(resp.json(post_label))
        if state.failed:
            return Outcome("incomplete", reason="export_failed")
        ctx.export_id = state.id
    status_path = api_path(P_EXPORT_STATUS, e=ent, report_id=state.id)
    while not state.completed:
        if waited >= EXPORT_CAP_S:
            logger.warning("unit %s: the report export is still running after %d min; the unit "
                           "is left incomplete (--resume continues it)", ctx.unit.id,
                           EXPORT_CAP_S // 60)
            return Outcome("incomplete", reason="export_timeout", export_id=state.id)
        client.clock.sleep(EXPORT_POLL_S)
        waited += EXPORT_POLL_S
        state = parse_export(client.api("GET", status_path, label=status_label)
                             .json(status_label))
        if state.failed:
            return Outcome("incomplete", reason="export_failed")
    if not state.urls:
        return Outcome("incomplete", reason="export_without_download")
    stem = ctx.unit.id
    for i, url in enumerate(state.urls, start=1):
        name = stem if len(state.urls) == 1 else f"{stem}_part{i}"
        with ctx.recorder.binary(name, ".csv") as sink:
            client.download(url, sink, label=f"download {i} of the {report_type} report")
    return Outcome("complete")


def _run_pages(ctx: UnitContext, template: str, parts: dict[str, str],
               query: Sequence[tuple[str, str]] = (), *, paged: bool = True) -> Outcome:
    ctx.record_pages(f"{ctx.unit.id}.jsonl", api_path(template, **parts), query,
                     label=f"GET {template}", paged=paged)
    return Outcome("complete")


def _run_budgets(ctx: UnitContext) -> Outcome:
    ent = ctx.scope.enterprise
    rel = f"{ctx.unit.id}.jsonl"
    bodies = ctx.record_pages(rel, api_path(P_BUDGETS, e=ent), label=f"GET {P_BUDGETS}")
    ids: list[str] = []
    for body in bodies:
        ids.extend(i for i in budget_ids(body) if i not in ids)
    for bid in ids:
        ctx.record_pages(rel, api_path(P_USER_STATES, e=ent, budget_id=bid),
                         label=f"GET {P_USER_STATES}", missing_ok=True)
    return Outcome("complete")


def _run_usage_by_cost_center(ctx: UnitContext, year: int, month: int) -> Outcome:
    ent = ctx.scope.enterprise
    label = f"GET {P_COST_CENTERS}"
    ids: list[str] = []
    for page in iter_pages(ctx.client, api_path(P_COST_CENTERS, e=ent), label=label):
        ids.extend(i for i in cost_center_ids(page.body) if i not in ids)
    rel = f"{ctx.unit.id}.jsonl"
    path = api_path(P_USAGE, e=ent)
    for cc in [*ids, _NONE_CC]:
        ctx.record_pages(rel, path, (("year", str(year)), ("month", str(month)),
                                     ("cost_center_id", cc)),
                         label=f"GET {P_USAGE}", paged=False)
    return Outcome("complete")


# ---------------------------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------------------------


def _export_unit(kind: str, window: tuple[str, str]) -> PlannedUnit:
    return PlannedUnit(id=f"{kind}/{window[0]}_{window[1]}", kind=kind,
                       run=lambda ctx: _run_export(ctx, kind, window), window=window,
                       export=True)


def plan(scope: PullScope, kinds: Iterable[str]) -> list[PlannedUnit]:
    """The billing, seats and configuration units of *kinds* for *scope* (in pull order: REST page
    sets first, report exports last)."""
    wanted = frozenset(kinds)
    ent = scope.enterprise
    units: list[PlannedUnit] = []
    if "config" in wanted:
        if ent is not None:
            units.append(PlannedUnit(id="config/budgets", kind="config", run=_run_budgets))
            units.append(PlannedUnit(
                id="config/cost_centers", kind="config",
                run=lambda ctx: _run_pages(ctx, P_COST_CENTERS, {"e": ent})))
        for org in scope.orgs:
            units.append(PlannedUnit(
                id=f"config/org_billing/{org}", kind="config",
                run=lambda ctx, org=org: _run_pages(ctx, P_ORG_BILLING, {"org": org},
                                                    paged=False)))
    if "seats" in wanted:
        if ent is not None:
            units.append(PlannedUnit(
                id="seats/enterprise", kind="seats",
                run=lambda ctx: _run_pages(ctx, P_ENTERPRISE_SEATS, {"e": ent})))
        for org in scope.orgs:
            units.append(PlannedUnit(
                id=f"seats/org/{org}", kind="seats",
                run=lambda ctx, org=org: _run_pages(ctx, P_ORG_SEATS, {"org": org})))
    if "summary" in wanted and ent is not None:
        for year, month in months(scope.since, scope.until):
            ym = f"{year:04d}-{month:02d}"
            window = (f"{ym}-01", f"{ym}-{calendar.monthrange(year, month)[1]:02d}")
            query = (("year", str(year)), ("month", str(month)))
            units.append(PlannedUnit(
                id=f"summary/usage_summary/{ym}", kind="summary", window=window,
                run=lambda ctx, q=query: _run_pages(ctx, P_USAGE_SUMMARY, {"e": ent}, q,
                                                    paged=False)))
            units.append(PlannedUnit(
                id=f"summary/ai_credit_usage/{ym}", kind="summary", window=window,
                run=lambda ctx, q=query: _run_pages(ctx, P_AI_CREDIT_USAGE, {"e": ent}, q,
                                                    paged=False)))
            units.append(PlannedUnit(
                id=f"summary/usage_by_cost_center/{ym}", kind="summary", window=window,
                run=lambda ctx, y=year, m=month: _run_usage_by_cost_center(ctx, y, m)))
    last = min(scope.until, scope.today)
    for kind in ("ai_usage", "metered"):
        if kind in wanted and ent is not None and scope.since <= last:
            units.extend(_export_unit(kind, w)
                         for w in export_windows(scope.since, last, REPORT_TYPES[kind]))
    return units
