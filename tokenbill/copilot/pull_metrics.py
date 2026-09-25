"""Live pulls of Copilot usage metrics and cloud-agent tasks (addendum §5.12; CP-PULL).

The unit planners and runners :func:`tokenbill.copilot.pull_common.pull` uses for the kinds

* ``metrics`` — one unit per report and UTC day: ``GET
  /enterprises/{e}/copilot/metrics/reports/{report}?day=`` (``users-1-day``, ``user-teams-1-day``,
  ``enterprise-1-day`` — the handoff set — and optionally ``repos-1-day``); without an enterprise
  the organization variants ``GET /orgs/{org}/copilot/metrics/reports/{report}?day=`` (with
  ``organization-1-day`` for ``enterprise-1-day``). 204 / 404 → ``not_ready`` (GitHub has not
  processed the day; addendum §19.5 #21). Every ``download_links`` entry (signed NDJSON) is
  downloaded at once, each record recorded as an envelope line whose request is the report call
  (so the adapters see the organization of an org report) — the link itself is never stored.
  Days newer than D-3 (``facts.copilot.report_lag_days``) are recorded as ``provisional`` and
  pulled again on the next run; days after today are not requested.
* ``agent_tasks`` — only with a user token and listed repositories: per repository ``GET
  /agents/repos/{o}/{r}/tasks`` (paged) and, for every task updated in the window whose page
  carries no sessions (and whose ``session_count`` is not 0), ``GET
  /agents/repos/{o}/{r}/tasks/{task_id}``. ``GET /agents/tasks`` (the authenticated user's own
  tasks) is never called. Unit ids name a repository only by a hash.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.copilot.pull_common import (
    P_AGENT_TASK,
    P_AGENT_TASKS,
    P_ENTERPRISE_METRICS,
    P_ORG_METRICS,
    Outcome,
    PlannedUnit,
    PullError,
    PullScope,
    UnitContext,
    api_path,
    iter_pages,
    unit_hash,
)
from tokenbill.core.errors import UsageError
from tokenbill.core.jsonl import MAX_LINE_BYTES, parse_json_line

__all__ = [
    "METRICS_REPORTS",
    "METRICS_REPORTS_ALL",
    "ORG_REPORT_NAMES",
    "check_reports",
    "download_links",
    "iter_records",
    "metrics_days",
    "plan",
    "task_ids",
]

logger = logging.getLogger(__name__)

#: The metrics reports of a handoff pull (``HANDOFF_KINDS`` ``metrics``).
METRICS_REPORTS = ("users-1-day", "user-teams-1-day", "enterprise-1-day")
#: Every per-day report a pull may request (``repos-1-day`` never in a handoff: repository data).
METRICS_REPORTS_ALL = (*METRICS_REPORTS, "repos-1-day")
#: Organization report names that differ from the enterprise ones (**VERIFY**).
ORG_REPORT_NAMES = {"enterprise-1-day": "organization-1-day"}
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MAX_ARRAY_BYTES = 256 * 2**20
_GZIP_MAGIC = b"\x1f\x8b"


def check_reports(reports: Iterable[str] | str | None) -> tuple[str, ...]:
    """The metrics reports to pull (default :data:`METRICS_REPORTS`); :class:`UsageError` for an
    unknown or empty selection."""
    if reports is None:
        return METRICS_REPORTS
    if isinstance(reports, str):
        reports = [r.strip() for r in reports.split(",") if r.strip()]
    try:
        items = list(reports)
    except TypeError:
        raise UsageError("pull: metrics reports must be a list of report names") from None
    if not items or not all(isinstance(r, str) and r in METRICS_REPORTS_ALL for r in items):
        raise UsageError(f"pull: metrics reports are {', '.join(METRICS_REPORTS_ALL)}")
    return tuple(r for r in METRICS_REPORTS_ALL if r in items)


def metrics_days(since: str, until: str, today: str) -> list[str]:
    """Every UTC day of ``[since, min(until, today)]``."""
    start = _dt.date.fromisoformat(since)
    last = min(_dt.date.fromisoformat(until), _dt.date.fromisoformat(today))
    out: list[str] = []
    while start <= last:
        out.append(start.isoformat())
        start += _dt.timedelta(days=1)
    return out


def download_links(doc: Any) -> tuple[str, ...]:
    """The ``download_links`` of a metrics report answer (https URLs; possibly none);
    :class:`PullError` for any other shape."""
    if not isinstance(doc, dict) or "download_links" not in doc:
        raise PullError("metrics report: the answer has no download_links", "metrics_shape")
    links = doc["download_links"]
    if links is None:
        return ()
    if not isinstance(links, list) or not all(isinstance(u, str) for u in links):
        raise PullError("metrics report: malformed download_links", "metrics_shape")
    if not all(u.lower().startswith("https://") for u in links):
        raise PullError("metrics report: download links must be https", "bad_link")
    return tuple(links)


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token}")


def iter_records(path: Path) -> Iterator[dict[str, Any] | None]:
    """The records of a downloaded metrics file — NDJSON (one object per line) or one JSON array,
    plain or gzip — with exact numbers; None for each line or element that is not a JSON object
    (counted as skipped by the caller). A corrupt stream raises :class:`PullError`."""
    with open(path, "rb") as probe:
        magic = probe.read(2)
    opener: Any = gzip.open if magic == _GZIP_MAGIC else open
    try:
        with opener(path, "rb") as fh:
            first = True
            while True:
                line = fh.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    while True:  # skip the rest of an oversize line without holding it
                        rest = fh.readline(1 << 16)
                        if not rest or rest.endswith(b"\n"):
                            break
                    first = False
                    yield None
                    continue
                stripped = line.strip()
                if first and stripped.startswith(b"\xef\xbb\xbf"):
                    stripped = stripped[3:].strip()
                if not stripped:
                    continue
                if first and stripped.startswith(b"["):
                    yield from _array_records(path, opener)
                    return
                first = False
                yield parse_json_line(stripped, exact_numbers=True)
    except (OSError, EOFError, gzip.BadGzipFile):
        raise PullError("a metrics download is corrupt", "corrupt_download") from None


def _array_records(path: Path, opener: Any) -> Iterator[dict[str, Any] | None]:
    with opener(path, "rb") as fh:
        raw = fh.read(_MAX_ARRAY_BYTES + 1)
    if len(raw) > _MAX_ARRAY_BYTES:
        raise PullError("a metrics download exceeds the size limit", "too_large")
    try:
        text = raw.decode("utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        doc = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        yield None
        return
    if not isinstance(doc, list):
        yield None
        return
    for item in doc:
        yield item if isinstance(item, dict) else None


def _ts_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        when = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    try:
        return int(when.timestamp() * 1000)
    except (OverflowError, ValueError, OSError):
        return None


def task_ids(body: Any, *, since_ms: int | None = None) -> list[str]:
    """Ids of the tasks on a task page whose details must be fetched: valid ids, not
    ``session_count`` 0, without ``sessions`` on the page, updated (else created) at or after
    *since_ms* when that timestamp parses."""
    tasks = body.get("tasks") if isinstance(body, dict) else body
    out: list[str] = []
    if not isinstance(tasks, list):
        return out
    for task in tasks:
        if not isinstance(task, dict) or isinstance(task.get("sessions"), list):
            continue
        tid = task.get("id")
        if not isinstance(tid, str) or not _ID_RE.match(tid) or ".." in tid or tid in out:
            continue
        count = task.get("session_count")
        if type(count) is int and count == 0:
            continue
        ts = _ts_ms(task.get("updated_at") or task.get("created_at"))
        if since_ms is not None and ts is not None and ts < since_ms:
            continue
        out.append(tid)
    return out


# ---------------------------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------------------------


def _run_metrics_day(ctx: UnitContext, template: str, parts: dict[str, str], report: str,
                     day: str) -> Outcome:
    path = api_path(template, report=report, **parts)
    label = f"GET {template}"
    resp = ctx.client.api("GET", path, (("day", day),), label=label, passthrough=(204, 404))
    if resp.status in (204, 404):
        return Outcome("not_ready", reason=f"http_{resp.status}")
    links = download_links(resp.json(label))
    rel = f"{ctx.unit.id}.jsonl"
    skipped = 0
    for i, url in enumerate(links, start=1):
        with ctx.recorder.spool(f"dl{i}") as (spool_path, sink):
            ctx.client.download(url, sink, label=f"download {i} of the {report} report")
        for rec in iter_records(spool_path):
            if rec is None:
                skipped += 1
                continue
            ctx.record(rel, path, {"day": day}, rec)
    if skipped:
        logger.warning("unit %s: %d line(s) of the download are not JSON objects (dropped)",
                       ctx.unit.id, skipped)
    return Outcome("complete", skipped=skipped)


def _run_agent_tasks(ctx: UnitContext, repo: str) -> Outcome:
    owner, name = repo.split("/", 1)
    rel = f"{ctx.unit.id}.jsonl"
    list_path = api_path(P_AGENT_TASKS, o=owner, r=name)
    since_ms = int(_dt.datetime.fromisoformat(ctx.scope.since)
                   .replace(tzinfo=_dt.timezone.utc).timestamp()) * 1000
    ids: list[str] = []
    for page in iter_pages(ctx.client, list_path, label=f"GET {P_AGENT_TASKS}"):
        ctx.record(rel, list_path, page.query, page.body, page.link)
        ids.extend(t for t in task_ids(page.body, since_ms=since_ms) if t not in ids)
    label = f"GET {P_AGENT_TASK}"
    for tid in ids:
        path = api_path(P_AGENT_TASK, o=owner, r=name, task_id=tid)
        resp = ctx.client.api("GET", path, label=label, passthrough=(404,))
        if resp.status == 404:  # deleted between the list and the detail call
            continue
        ctx.record(rel, path, {}, resp.json(label))
    return Outcome("complete")


# ---------------------------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------------------------


def _targets(scope: PullScope) -> Sequence[tuple[str, str, dict[str, str], bool]]:
    if scope.enterprise is not None:
        return [("enterprise", P_ENTERPRISE_METRICS, {"e": scope.enterprise}, False)]
    return [(f"org-{org}", P_ORG_METRICS, {"org": org}, True) for org in scope.orgs]


def plan(scope: PullScope, kinds: Iterable[str]) -> list[PlannedUnit]:
    """The metrics and agent-task units of *kinds* for *scope*."""
    wanted = frozenset(kinds)
    units: list[PlannedUnit] = []
    if "metrics" in wanted:
        fresh_after = (_dt.date.fromisoformat(scope.today)
                       - _dt.timedelta(days=scope.lag_days)).isoformat()
        days = metrics_days(scope.since, scope.until, scope.today)
        for report in scope.metrics_reports:
            for target, template, parts, is_org in _targets(scope):
                name = ORG_REPORT_NAMES.get(report, report) if is_org else report
                for day in days:
                    units.append(PlannedUnit(
                        id=f"metrics/{name}/{target}/{day}", kind="metrics", day=day,
                        refresh=day > fresh_after,
                        run=lambda ctx, t=template, p=parts, n=name, d=day:
                            _run_metrics_day(ctx, t, p, n, d)))
    if "agent_tasks" in wanted:
        for repo in scope.agent_repos:
            units.append(PlannedUnit(
                id=f"agent_tasks/{unit_hash(repo)}", kind="agent_tasks", user_token=True,
                repos=1, window=(scope.since, scope.until),
                run=lambda ctx, r=repo: _run_agent_tasks(ctx, r)))
    return units
