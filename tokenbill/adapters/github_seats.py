"""GitHub Copilot seats adapter (addendum §5.6; package CP-ORGDATA).

:class:`CopilotSeatsAdapter` (registry name ``github-copilot-seats``) reads the seat lists
``GET /enterprises/{e}/copilot/billing/seats`` and ``GET /orgs/{org}/copilot/billing/seats``
(``{total_seats, seats[…]}`` pages, CP-PULL envelopes, JSON arrays of pages, directories) into one
:class:`~tokenbill.core.records.LicenseSnapshot` per (person, organization):

* ``principal`` — the ``p_`` of ``assignee.login`` under the principal key; name, e-mail, avatar,
  URLs and ids of the assignee are dropped at parse time; team and cost center from the maps;
* ``org`` — the seat's ``organization.login``, else the org of the request path
  (``/orgs/{org}/…``) or ``--attr workspace_id=<org>``; a person seated through several
  organizations keeps one snapshot per organization;
* ``plan`` — the seat's ``plan_type`` (``business`` | ``enterprise`` | ``unknown``, the latter
  also when absent): plan evidence of precedence 2 for ``core.pool.detect_plans``;
* ``assigned_via_team`` — ``assigning_team is not None`` (always a bool here; ``None`` is reserved
  for the UI activity report);
* ``last_activity_bucket`` / ``last_authenticated_bucket`` — days between the timestamp and the
  snapshot date: ``0-7``, ``8-30``, ``31-90``, ``none_90d`` (also for a null timestamp: the API
  returns nil after 90 days);
* ``last_activity_surface`` — ``core.catalog.editor_family(last_activity_editor)``
  (``vscode/1.77.3/copilot/1.86.82`` → ``vscode``; JetBrains strings **VERIFY**, facts
  ``copilot.editor_families``).

The snapshot date is the fetch date: the envelope's ``fetched_ms`` / ``fetched_at``, else
``opts.now_ms``; with neither, the latest timestamp in the source
(``dq.copilot_snapshot_date_assumed``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tokenbill.adapters.github_config import (
    DAY_MS,
    PRODUCT,
    BadRecord,
    Item,
    OrgRead,
    day_of_ms,
    head_keys,
    head_request_path,
    login_key,
    parse_day,
    parse_ts_ms,
    safe_label,
)
from tokenbill.core.catalog import editor_family
from tokenbill.core.records import LICENSE_PLANS, LicenseSnapshot
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["CopilotSeatsAdapter", "bucket_of"]

_SEATS_PATH_RE = re.compile(r"/copilot/billing/seats\Z")
_ORG_PATH_RE = re.compile(r"/orgs/([^/]+)/copilot/billing/seats\Z")


def bucket_of(ts_ms: int | None, snapshot_date: str) -> str:
    """The recency bucket of a timestamp against the snapshot date (UTC days; a timestamp after
    the snapshot counts as 0 days): ``0-7``, ``8-30``, ``31-90``, else (or None) ``none_90d``."""
    if ts_ms is None:
        return "none_90d"
    days = max(0, (parse_ts_ms(snapshot_date, "snapshot_date") - ts_ms // DAY_MS * DAY_MS)
               // DAY_MS)
    if days <= 7:
        return "0-7"
    if days <= 30:
        return "8-30"
    return "31-90" if days <= 90 else "none_90d"


@dataclass(frozen=True)
class _Seat:
    """A parsed seat before the snapshot date is known."""

    principal: str
    team: str | None
    cost_center: str | None
    org: str | None
    plan: str
    created: str | None
    pending: str | None
    activity_ms: int | None
    auth_ms: int | None
    surface: str | None
    via_team: bool
    fetched_ms: int | None


class CopilotSeatsAdapter:
    """Copilot seat lists → one ``LicenseSnapshot`` per (principal, org)."""

    name = "github-copilot-seats"
    capabilities = frozenset({"licenses"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """A seats page (``total_seats`` / ``seats`` with assignees) or its recorded envelope."""
        req = head_request_path(head)
        if req is not None and _SEATS_PATH_RE.search(req):
            return True
        keys = head_keys(head)
        return "seats" in keys and ("total_seats" in keys or "assignee" in keys)

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse every seat of *path* (file or directory)."""
        ctx = OrgRead(self.name, path, opts)
        seats: list[_Seat] = []
        for item in ctx.items():
            body = item.body
            if not (isinstance(body, dict) and isinstance(body.get("seats"), list)):
                ctx.stat("skipped_documents")
                continue
            if type(body.get("total_seats")) is int:
                ctx.stat("total_seats_reported", body["total_seats"])
            for i, seat in enumerate(body["seats"]):
                try:
                    seats.append(_seat(ctx, item, seat))
                    ctx.stat("records")
                except BadRecord as exc:
                    ctx.quarantine(f"{item.locator}/seats[{i}]", exc.reason)
        fallback = ctx.opts.now_ms
        if fallback <= 0 and any(s.fetched_ms is None for s in seats):
            stamps = [t for s in seats for t in (s.activity_ms, s.auth_ms) if t is not None]
            fallback = max(stamps, default=0)
            ctx.note("dq.copilot_snapshot_date_assumed", "warn", "seat list without a fetch "
                     "time or clock: snapshot date = the latest activity in the source")
        table: dict[tuple[str, str], LicenseSnapshot] = {}
        for s in seats:
            fetched = s.fetched_ms if s.fetched_ms is not None else fallback
            snap = _snapshot(s, day_of_ms(fetched), fetched)
            key = (snap.principal, snap.org or "")
            cur = table.get(key)
            if cur is not None:
                ctx.stat("duplicate_seats")
                snap = _merge(cur, snap)
            table[key] = snap
        return ctx.result(self.capabilities, licenses=table.values())


def _org_of(ctx: OrgRead, item: Item, seat: Mapping[str, Any]) -> str | None:
    org = seat.get("organization")
    if isinstance(org, dict) and safe_label(org.get("login")) is not None:
        return safe_label(org["login"])
    m = _ORG_PATH_RE.search(item.path or "")
    if m is not None:
        return safe_label(m.group(1))
    return safe_label(ctx.opts.attribution.workspace_id)


def _opt_ts(seat: Mapping[str, Any], key: str) -> int | None:
    value = seat.get(key)
    return None if value is None else parse_ts_ms(value, key)


def _seat(ctx: OrgRead, item: Item, seat: object) -> _Seat:
    if not isinstance(seat, dict):
        raise BadRecord("not_object")
    assignee = seat.get("assignee")
    login = assignee.get("login") if isinstance(assignee, dict) else None
    if login_key(login) is None:
        raise BadRecord("missing:assignee.login")
    user_id = assignee.get("id") if isinstance(assignee, dict) else None
    plan = seat.get("plan_type")
    created = _opt_ts(seat, "created_at")
    pending = seat.get("pending_cancellation_date")
    editor = seat.get("last_activity_editor")
    return _Seat(
        principal=ctx.principal(login), team=ctx.team(login, user_id),
        cost_center=ctx.cost_center(login, user_id), org=_org_of(ctx, item, seat),
        plan=plan if plan in LICENSE_PLANS else "unknown",
        created=day_of_ms(created) if created is not None else None,
        pending=parse_day(pending, "pending_cancellation_date") if pending is not None else None,
        activity_ms=_opt_ts(seat, "last_activity_at"),
        auth_ms=_opt_ts(seat, "last_authenticated_at"),
        surface=editor_family(editor) if isinstance(editor, str) and editor.strip() else None,
        via_team=seat.get("assigning_team") is not None, fetched_ms=item.fetched_ms)


def _snapshot(s: _Seat, snapshot_date: str, fetched: int) -> LicenseSnapshot:
    return LicenseSnapshot(
        snapshot_date=snapshot_date, product=PRODUCT, plan=s.plan, principal=s.principal,
        team=s.team, cost_center=s.cost_center, org=s.org, seat_created=s.created,
        pending_cancellation=s.pending,
        last_activity_bucket=bucket_of(s.activity_ms, snapshot_date),
        last_activity_surface=s.surface,
        last_authenticated_bucket=bucket_of(s.auth_ms, snapshot_date),
        assigned_via_team=s.via_team, fetched_ms=fetched, source_kind="github.copilot_seats")


_BUCKET_ORDER = ("0-7", "8-30", "31-90", "none_90d")


def _merge(a: LicenseSnapshot, b: LicenseSnapshot) -> LicenseSnapshot:
    """Two seats of one person in one org (e.g. two enterprise teams): the most recent activity,
    a direct assignment wins over a team one, the earlier creation date."""
    recent = min(a.last_activity_bucket, b.last_activity_bucket, key=_BUCKET_ORDER.index)
    auth = min(a.last_authenticated_bucket, b.last_authenticated_bucket, key=_BUCKET_ORDER.index)
    first = a if (a.last_activity_bucket == recent) else b
    created = min((c for c in (a.seat_created, b.seat_created) if c is not None), default=None)
    pending = None if a.pending_cancellation is None or b.pending_cancellation is None else max(
        a.pending_cancellation, b.pending_cancellation)
    plan = a.plan if a.plan == b.plan else ("enterprise" if "enterprise" in (a.plan, b.plan)
                                            else "unknown")
    return LicenseSnapshot(
        snapshot_date=max(a.snapshot_date, b.snapshot_date), product=a.product, plan=plan,
        principal=a.principal, team=a.team, cost_center=a.cost_center, org=a.org,
        seat_created=created, pending_cancellation=pending, last_activity_bucket=recent,
        last_activity_surface=first.last_activity_surface, last_authenticated_bucket=auth,
        assigned_via_team=bool(a.assigned_via_team and b.assigned_via_team),
        fetched_ms=max(a.fetched_ms, b.fetched_ms), source_kind=a.source_kind)
