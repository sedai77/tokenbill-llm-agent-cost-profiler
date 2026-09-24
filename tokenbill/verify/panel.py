"""Cluster-day panel at constant prices (SPEC §13.1, R8, D26).

The primary unit is **cost per active developer-day**, intention-to-treat, per cluster-day.
:func:`build_panel` reads active developer-days from ``LedgerStore.cluster_days`` (they survive
identity retention) and reprices every billable inference of the window twice: at the
**pre-registered baseline rate card** (``cost_baseline_nano``, the measurement metric) and at the
actual card (``cost_actual_nano``, for the EXACT rate variance). Allowance (list-equivalent,
``billing_class="allowance"``) spend is measured separately and never enters invoice savings.

Conventions (documented contract of this module):

* Cluster ids come from the request attribution per cluster kind (:data:`CLUSTER_FIELDS`); requests
  without one are outside every cluster. The date of a request is the UTC date of its start (the
  same rule as ``cluster_days``).
* Costs mirror the store's ``cluster_day`` columns: on the billed class only **exact** lines count
  (``PricedInference.exact_nano`` — billed tokens × a sourced rate); range lines (unknown-TTL
  writes, uncertain billing) are left out so the rate variance stays EXACT; unpriced inferences
  count 0.
  On the allowance class the list-equivalent point counts.
* ``arms`` maps a cluster to its assigned arm (intention-to-treat) as ``"<arm>"`` or
  ``"<arm>@YYYY-MM-DD"`` (the adoption date). Arms named in :data:`CONTROL_ARMS` are never
  treated. Without a date a cluster is treated from the first day its requests carry a non-control
  ``tokenbill.arm`` tag (the tag ships with the wave's MDM payload). Without an ``arms`` entry the
  arm is the one the telemetry reports. Treatment is absorbing.
* ``outcome_prs`` is filled from team-level ``OutcomeAggregate`` rows when the cluster kind is
  ``team`` (the largest ``pull_requests`` per team-day across outcome sources).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping, Sequence

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Figure, exact
from tokenbill.core.protocols import LedgerStore, Pricer
from tokenbill.core.records import Request
from tokenbill.core.records import billing_class as billing_class_of
from tokenbill.core.types import PanelRow, PricedInference

__all__ = [
    "BILLING_CLASSES",
    "CLUSTER_FIELDS",
    "CONTROL_ARMS",
    "UNKNOWN_SCOPE",
    "build_panel",
    "cache_scope_clusters",
    "cluster_of",
    "org_series",
    "panel_channels",
    "panel_window",
    "parse_arm",
    "rate_variance",
]

_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)

#: Cluster kind → where the cluster id lives on a request's attribution: ``("attr", field)`` or
#: ``("extra", key)``. Cache-isolation units (SPEC §13.2): workspace, MDM group, IdP group via the
#: Claude apps gateway; ``team`` for team-level panels.
CLUSTER_FIELDS: Mapping[str, tuple[str, str]] = {
    "team": ("attr", "team"),
    "workspace": ("attr", "workspace_id"),
    "mdm_group": ("extra", "mdm_group"),
    "gateway": ("extra", "gateway"),
}

#: Arm labels that mean "never treated".
CONTROL_ARMS = frozenset({"control", "holdback"})

#: The cache scope of lanes without a session shell (``core.lanes.group_lanes``).
UNKNOWN_SCOPE = "unknown"

#: Billing classes a panel can measure (``core.records.billing_class``).
BILLING_CLASSES = ("billed", "allowance")


def _date_ms(date: str, name: str) -> int:
    try:
        d = _dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise UsageError(f"{name} must be a YYYY-MM-DD date") from None
    return (d - _EPOCH).days * _DAY_MS


def _date_of(ts_ms: int) -> str:
    return (_EPOCH + _dt.timedelta(days=ts_ms // _DAY_MS)).isoformat()


def cluster_of(request: Request, cluster_kind: str) -> str | None:
    """The cluster id of *request* under *cluster_kind* (None when it carries none)."""
    where = CLUSTER_FIELDS.get(cluster_kind)
    if where is None:
        raise UsageError(f"unknown cluster kind {cluster_kind!r}")
    kind, name = where
    attr = request.attribution
    if kind == "attr":
        value = getattr(attr, name)
    else:
        value = dict(attr.extra).get(name)
    return value or None


def parse_arm(value: str) -> tuple[str, str | None]:
    """``"<arm>"`` → ``(arm, None)``; ``"<arm>@YYYY-MM-DD"`` → ``(arm, date)``."""
    if not isinstance(value, str) or not value:
        raise UsageError("arm labels must be non-empty strings")
    arm, sep, date = value.partition("@")
    if not arm:
        raise UsageError("arm labels must be non-empty strings")
    if not sep:
        return arm, None
    _date_ms(date, "arm adoption date")
    return arm, date


def _check_window(since: str, until: str) -> tuple[int, int]:
    lo, hi = _date_ms(since, "since"), _date_ms(until, "until")
    if hi <= lo:
        raise UsageError("until must be after since")
    return lo, hi


def _costs(priced: PricedInference, cls: str) -> int:
    fig = priced.figure
    if fig.nano is None:
        return 0
    if cls == "allowance":
        return fig.nano if fig.basis is Basis.LIST_EQUIVALENT else 0
    if fig.basis is Basis.LIST_EQUIVALENT:
        return 0
    return priced.exact_nano


def _requests(store: LedgerStore, lo: int, hi: int) -> Iterable[Request]:
    return store.iter_requests(since_ms=lo, until_ms=hi)


def build_panel(store: LedgerStore, *, cluster_kind: str, since: str, until: str,
                baseline_pricer: Pricer, actual_pricer: Pricer,
                arms: Mapping[str, str] | None = None,
                billing_class: str = "billed") -> list[PanelRow]:
    """The cluster-day panel over ``[since, until)`` (UTC dates), one row per cluster and day,
    ordered by (cluster, date). See the module docstring for the conventions."""
    if cluster_kind not in CLUSTER_FIELDS:
        raise UsageError(f"unknown cluster kind {cluster_kind!r}")
    if billing_class not in BILLING_CLASSES:
        raise UsageError(f"billing_class must be one of {', '.join(BILLING_CLASSES)}")
    lo, hi = _check_window(since, until)
    assigned = {c: parse_arm(v) for c, v in (arms or {}).items()}
    cells: dict[tuple[str, str], list[int]] = {}      # (cluster, date) → [baseline, actual, devs]
    tags: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for cd in store.cluster_days(cluster_kind=cluster_kind, since=since, until=until):
        key = (cd.cluster_id, cd.date_utc)
        cell = cells.setdefault(key, [0, 0, 0])
        cell[2] += cd.active_users
        prev = tags.get(key, (None, None))
        arm = prev[0]
        if cd.arm is not None and (arm is None or arm in CONTROL_ARMS):
            arm = cd.arm
        tags[key] = (arm, prev[1] if prev[1] is not None else cd.wave)
    for req in _requests(store, lo, hi):
        cluster = cluster_of(req, cluster_kind)
        if cluster is None:
            continue
        key = (cluster, _date_of(req.ts_start_ms))
        cell = cells.setdefault(key, [0, 0, 0])
        for att in req.attempts:
            for inf in att.inferences:
                if inf.billable is False:
                    continue
                if billing_class_of(inf.pricing.billing_path) != billing_class:
                    continue
                cell[0] += _costs(baseline_pricer.price_inference(inf, ts_ms=att.ts_start_ms),
                                  billing_class)
                cell[1] += _costs(actual_pricer.price_inference(inf, ts_ms=att.ts_start_ms),
                                  billing_class)
        a = req.attribution
        if a.arm is not None or a.wave is not None:
            prev = tags.get(key, (None, None))
            arm = prev[0]
            if a.arm is not None and (arm is None or arm in CONTROL_ARMS):
                arm = a.arm
            tags[key] = (arm, prev[1] if prev[1] is not None else a.wave)
    # adoption per cluster: explicit date, else the first day with a non-control arm tag
    first_tag: dict[str, str] = {}
    for (cluster, date), (arm, _) in sorted(tags.items()):
        if arm is not None and arm not in CONTROL_ARMS:
            first_tag.setdefault(cluster, date)
    outcomes: dict[tuple[str, str], int] = {}
    if cluster_kind == "team":
        for o in store.outcomes(since_ms=lo, until_ms=hi):
            if since <= o.date_utc < until:
                k = (o.team, o.date_utc)
                outcomes[k] = max(outcomes.get(k, 0), o.pull_requests)
    rows = []
    for (cluster, date), (base, actual, devs) in sorted(cells.items()):
        tag_arm, tag_wave = tags.get((cluster, date), (None, None))
        if cluster in assigned:
            arm, start = assigned[cluster]
            if arm in CONTROL_ARMS:
                treated = False
            elif start is not None:
                treated = date >= start
            else:
                treated = cluster in first_tag and date >= first_tag[cluster]
        else:
            arm = tag_arm
            treated = cluster in first_tag and date >= first_tag[cluster]
        rows.append(PanelRow(cluster_id=cluster, date_utc=date, cost_baseline_nano=base,
                             cost_actual_nano=actual, active_dev_days=devs, arm=arm,
                             wave=tag_wave, treated=treated,
                             outcome_prs=outcomes.get((cluster, date))))
    return rows


def panel_window(panel: Sequence[PanelRow]) -> tuple[str, str]:
    """``(first date, last date)`` of *panel* (``UsageError`` when empty)."""
    if not panel:
        raise UsageError("empty panel")
    dates = [r.date_utc for r in panel]
    return min(dates), max(dates)


def rate_variance(panel: Sequence[PanelRow], *, post_from: str | None = None,
                  basis: Basis = Basis.LIST) -> Figure:
    """The EXACT price effect (R8): ``Σ (cost_actual − cost_baseline)`` over rows dated on or after
    *post_from* (default: the first treated date; every row when nothing is treated). Negative when
    prices fell."""
    if post_from is None:
        treated = [r.date_utc for r in panel if r.treated]
        post_from = min(treated) if treated else ""
    total = sum(r.cost_actual_nano - r.cost_baseline_nano for r in panel
                if r.date_utc >= post_from)
    return exact(total, basis, provenance=("rate_variance",))


def org_series(panel: Sequence[PanelRow]) -> list[tuple[str, int, int]]:
    """The org-level daily series ``(date, Σ cost_baseline_nano, Σ active_dev_days)`` for the ITS
    design, in date order."""
    acc: dict[str, list[int]] = {}
    for r in panel:
        a = acc.setdefault(r.date_utc, [0, 0])
        a[0] += r.cost_baseline_nano
        a[1] += r.active_dev_days
    return [(d, c, n) for d, (c, n) in sorted(acc.items())]


def panel_channels(store: LedgerStore, *, cluster_kind: str, since: str, until: str,
                   billing_class: str = "billed") -> tuple[str, ...]:
    """The pricing channels of the billable inferences a panel over the same window covers (for
    the per-channel reconciliation guard)."""
    if cluster_kind not in CLUSTER_FIELDS:
        raise UsageError(f"unknown cluster kind {cluster_kind!r}")
    lo, hi = _check_window(since, until)
    out: set[str] = set()
    for req in _requests(store, lo, hi):
        if cluster_of(req, cluster_kind) is None:
            continue
        for att in req.attempts:
            for inf in att.inferences:
                if inf.billable is not False and \
                        billing_class_of(inf.pricing.billing_path) == billing_class:
                    out.add(inf.pricing.channel)
    return tuple(sorted(out))


def cache_scope_clusters(store: LedgerStore, *, cluster_kind: str, since: str,
                         until: str) -> dict[str, tuple[str, ...]]:
    """Cache scope → the clusters whose lanes share it (for the cluster ≥ cache-scope guard: a
    scope spanning two clusters means treated and control traffic share a cache). Lanes whose
    scope is ``"unknown"`` (no session shell) carry no evidence of sharing and are skipped."""
    if cluster_kind not in CLUSTER_FIELDS:
        raise UsageError(f"unknown cluster kind {cluster_kind!r}")
    lo, hi = _check_window(since, until)
    scopes: dict[str, set[str]] = {}
    for lane in store.iter_lanes(since_ms=lo, until_ms=hi):
        if lane.cache_scope_key == UNKNOWN_SCOPE:
            continue
        for req in lane.requests:
            cluster = cluster_of(req, cluster_kind)
            if cluster is not None:
                scopes.setdefault(lane.cache_scope_key, set()).add(cluster)
    return {k: tuple(sorted(v)) for k, v in sorted(scopes.items())}
