"""Team showback (SPEC §14.5, §8.4, §8.5; package OUT).

:func:`render_showback` writes per-team showback pages from **published aggregates only**
(``PublishedAggregate``; a raw aggregate or a person / person-proxy grouping is refused): per team
the spend with its basis chips (exact bill, allowance and Copilot credits apart, estimates beside),
cost per active developer-day beside the published $13 / $30 anchors (with source and date),
cache-read share against the 84% / 94% / 80% benchmarks (with source and date), context per request
(mean, and p50/p90 across the team's published cells when the aggregate has finer cells), the top
findings (Shapley-ranked, labelled), the related action-plan levers and the organization's
allocation coverage. With ``billing_paths`` (a published aggregate grouped by ``billing_path`` and
finer dims such as ``team``) the index adds, **per billing path**, the spend per active
developer-month (mean, and p50 / p90 of cell means weighted by developers, cells with ≥ k users
only, a path shown only when those cells reach k developers) and the overage share of seat requests
(billing path ``usage_credits`` against ``subscription``) — the seat-mix input for finance.

Formats: ``html`` (``index.html`` + ``team-<slug>.html``; CSP, no scripts, no external URL, every
chart followed by its table twin), ``csv`` (``showback.csv`` and ``showback-billing-paths.csv``)
and ``json`` (``showback.json``; the SPEC §14.1 schema rule: MONEY objects, ``evidence`` keys, no
floats).
Nothing names an individual: rows are teams (merged small teams print as "(other teams)"), rows with
an unknown user count print "users unknown" (R-E47).
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from tokenbill.core import evidence as _evidence
from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.kanon import PERSON_PROXY_DIMS, USERS_UNKNOWN, row_notes
from tokenbill.core.labels import Basis, Figure, add, estimated, exact, figure_json, scale
from tokenbill.core.money import nano_to_usd_str, ratio
from tokenbill.core.records import UsageBuckets
from tokenbill.core.types import ActionPlan, AggRow, ClusterDay, Finding, PublishedAggregate
from tokenbill.finops.allocation import is_allocated
from tokenbill.outputs.html import bar_figure, esc, money, page, table
from tokenbill.outputs.result_json import canonical_dumps, date_of, require_published
from tokenbill.outputs.terminal import chip, pct

__all__ = ["FORMATS", "render_showback"]

FORMATS = ("html", "csv", "json")
_DAY_MS = 86_400_000
#: An average month (365.25 / 12 = 487/16 days) for per-developer-month figures.
_MONTH_NUM, _MONTH_DEN = 487, 16
_OTHER = "(other teams)"
_UNATTRIBUTED = "(unattributed)"
_TOP = 5
_EXACT = "exact"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class _Team:
    name: str
    users: int = 0
    users_unknown: bool = True
    requests: int = 0
    usage: UsageBuckets = field(default_factory=UsageBuckets)
    exact: Figure | None = None
    estimated: Figure | None = None
    allowance: Figure | None = None
    pool: Figure | None = None
    cell_contexts: list[int] = field(default_factory=list)
    dev_days: int = 0
    day_exact: int = 0
    daily: list[int] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _add(a: Figure | None, b: Figure | None) -> Figure | None:
    if a is None:
        return b
    if b is None:
        return a
    return add(a, b)


def _add_usage(a: UsageBuckets, b: UsageBuckets) -> UsageBuckets:
    ttl = a.cache_write_other_ttl_s
    if ttl is not None and b.cache_write_other_ttl_s not in (None, ttl):
        b = dataclasses.replace(b, cache_write_other_ttl_s=ttl)   # informational only
    return a + b


def _team_name(value: str | None) -> str:
    if value is None:
        return _UNATTRIBUTED
    if value.startswith("(other"):
        return _OTHER
    return value


def _check_grouping(agg: PublishedAggregate, what: str, needed: str) -> None:
    require_published(agg, what)
    person = sorted(set(agg.group_by) & PERSON_PROXY_DIMS)
    if person:
        raise PrivacyError(f"{what}: showback never groups by person ({', '.join(person)})")
    if needed not in agg.group_by:
        raise UsageError(f"{what}: the aggregate must be grouped by {needed}")


def _teams(agg: PublishedAggregate) -> dict[str, _Team]:
    idx = agg.group_by.index("team")
    out: dict[str, _Team] = {}
    for row in agg.rows:
        name = _team_name(row.dims[idx][1])
        t = out.setdefault(name, _Team(name))
        unknown = USERS_UNKNOWN in row_notes(row, group_by=agg.group_by)
        if not unknown:
            t.users = max(t.users, row.n_users)   # a lower bound on distinct users of the team
            t.users_unknown = False
        t.requests += row.n_requests
        t.usage = _add_usage(t.usage, row.usage)
        t.exact = _add(t.exact, row.priced.exact)
        t.estimated = _add(t.estimated, row.priced.estimated)
        t.allowance = _add(t.allowance, row.priced.allowance)
        t.pool = _add(t.pool, row.priced.pool)
        if row.n_requests:
            t.cell_contexts.append(row.usage.total_input // row.n_requests)
    return out


def _percentile(values: Sequence[int], q: int) -> int:
    """Nearest-rank percentile *q* (0–100) of *values* (sorted copy)."""
    ordered = sorted(values)
    rank = max(1, -(-q * len(ordered) // 100))
    return ordered[rank - 1]


def _cluster_days(teams: dict[str, _Team], days: Iterable[ClusterDay], k: int) -> None:
    for d in days:
        if not isinstance(d, ClusterDay):
            raise ContractViolation("cluster_days must be ClusterDay rows")
        if d.cluster_kind != "team" or d.cluster_id not in teams:
            continue
        t = teams[d.cluster_id]
        t.dev_days += d.active_users
        t.day_exact += d.exact_nano
        if d.active_users >= k:
            t.daily.append(d.exact_nano // d.active_users)


def _per_dev_day(t: _Team, basis: Basis) -> Figure | None:
    if t.dev_days <= 0:
        return None
    return scale(exact(t.day_exact, basis), 1, t.dev_days)


def _cache_share(u: UsageBuckets) -> str | None:
    value = ratio(u.cache_read, u.total_input)
    return None if value is None else format(value.normalize(), "f")


def _cache_verdict(share: str | None) -> str:
    if share is None:
        return "no input tokens"
    s = Decimal(share)
    if s >= _evidence.CACHE_READ_SHARE_TOP_DECILE.value:  # type: ignore[operator]
        return "top-decile cache reuse"
    if s >= _evidence.CACHE_READ_SHARE_MEDIAN.value:  # type: ignore[operator]
        return "at or above the published median"
    if s >= _evidence.CACHE_READ_SHARE_INVESTIGATE_BELOW.value:  # type: ignore[operator]
        return "below the published median"
    return "below the investigate line"


def _saving(f: Finding) -> int:
    fig = f.recoverable_shapley or f.recoverable
    return -1 if fig is None or fig.nano is None else fig.nano


def _attach_findings(teams: dict[str, _Team], findings: Iterable[Finding]) -> None:
    for f in findings:
        if not isinstance(f, Finding):
            raise ContractViolation("findings must be Finding objects")
        dims = dict(f.scope.dims)
        if set(dims) & PERSON_PROXY_DIMS or f.audience != "org":
            continue
        team = dims.get("team")
        if team in teams:
            teams[team].findings.append(f)
    for t in teams.values():
        t.findings.sort(key=lambda f: (-_saving(f), f.finding_id))


def _coverage(agg: PublishedAggregate) -> str:
    idx = agg.group_by.index("team")
    total = allocated = 0
    for row in agg.rows:
        n = row.priced.exact.nano or 0
        total += n
        if is_allocated(row.dims[idx][1]):   # merged small teams are allocated too
            allocated += n
    value = ratio(allocated, total)
    return "1" if value is None else format(value.normalize(), "f")


# ---------------------------------------------------------------------------------------------
# billing paths
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _PathStat:
    path: str
    users: int
    cells: int
    mean: Figure
    p50: Figure | None
    p90: Figure | None
    overage_share: str | None


def _path_figure(row: AggRow, path: str) -> Figure | None:
    if path == "subscription":
        return row.priced.allowance
    if path in ("copilot_pool", "copilot_direct"):
        return row.priced.pool
    return row.priced.exact


def _billing_paths(agg: PublishedAggregate, k: int) -> list[_PathStat]:
    idx = agg.group_by.index("billing_path")
    days = max(1, -(-(agg.window[1] - agg.window[0]) // _DAY_MS))
    by_path: dict[str, list[AggRow]] = {}
    requests: dict[str, int] = {}
    for row in agg.rows:
        path = row.dims[idx][1] or "unknown"
        requests[path] = requests.get(path, 0) + row.n_requests
        if row.n_users >= k:
            by_path.setdefault(path, []).append(row)
    sub, over = requests.get("subscription", 0), requests.get("usage_credits", 0)
    share = ratio(over, sub + over)
    out = []
    for path in sorted(by_path):
        cells = [(r, _path_figure(r, path)) for r in by_path[path]]
        cells = [(r, f) for r, f in cells if f is not None and f.nano is not None]
        users = sum(r.n_users for r, _f in cells)
        if not cells or users < k:
            continue
        total: Figure | None = None
        per_dev: list[int] = []
        for r, f in cells:
            total = _add(total, f)
            cell_mean = scale(f, _MONTH_NUM, _MONTH_DEN * r.n_users * days)
            per_dev.extend([cell_mean.nano or 0] * r.n_users)
        assert total is not None
        mean = scale(total, _MONTH_NUM, _MONTH_DEN * users * days)
        basis = total.basis
        note = "cell means weighted by active developers (not per-person values)"
        p50 = p90 = None
        if len(cells) > 1:
            p50 = estimated(_percentile(per_dev, 50), basis, note=f"p50 of {note}")
            p90 = estimated(_percentile(per_dev, 90), basis, note=f"p90 of {note}")
        out.append(_PathStat(path=path, users=users, cells=len(cells), mean=mean, p50=p50,
                             p90=p90, overage_share=None if share is None or path != "subscription"
                             else format(share.normalize(), "f")))
    return out


# ---------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------


def _slug(name: str) -> str:
    base = _SLUG_RE.sub("-", name.lower()).strip("-")[:40] or "team"
    return f"team-{base}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"


def _anchor(const: _evidence.EvidenceConstant, text: str) -> tuple[str, str, str, str]:
    return (text, str(const.value), const.source_url, const.checked_on)


_ANCHORS = (
    _anchor(_evidence.CC_FLEET_USD_PER_ACTIVE_DAY, "typical cost per active developer-day (USD)"),
    _anchor(_evidence.CC_FLEET_USD_PER_ACTIVE_DAY_P90_UNDER,
            "90% of developers stay below (USD per active day)"),
)
_BENCHMARKS = (
    _anchor(_evidence.CACHE_READ_SHARE_MEDIAN, "median cache-read share"),
    _anchor(_evidence.CACHE_READ_SHARE_TOP_DECILE, "top-decile cache-read share"),
    _anchor(_evidence.CACHE_READ_SHARE_INVESTIGATE_BELOW, "investigate below"),
)


def _users(t: _Team) -> str:
    return "users unknown" if t.users_unknown else f"{t.users:,}"


def _team_levers(t: _Team, plan: ActionPlan | None) -> list[tuple[str, Figure]]:
    if plan is None:
        return []
    ids = {f.finding_id for f in t.findings}
    return [(lv.lever_id, lv.shapley) for lv in plan.levers if ids & set(lv.finding_ids)]


def _benchmark_table() -> str:
    rows = [[what, pct(value) if Decimal(value) < 1 else f"${value}", src, when]
            for what, value, src, when in (*_ANCHORS, *_BENCHMARKS)]
    return table("Published benchmarks (with source and verification date; not predictions)",
                 ["benchmark", "value", "source", "checked"], rows)


def _team_page(t: _Team, plan: ActionPlan | None, k: int) -> str:
    basis = t.exact.basis if t.exact is not None else Basis.LIST
    spend = [f"<li>Exact bill: {money(t.exact)}</li>" if t.exact is not None else ""]
    if t.estimated is not None:
        spend.append(f"<li>Estimated, beside the bill: {money(t.estimated)}</li>")
    if t.allowance is not None:
        spend.append("<li>Seat allowance (not billed): "
                     f"{money(t.allowance, kind='allowance')}</li>")
    if t.pool is not None:
        spend.append(f"<li>Copilot credits (not billed): "
                     f"{money(t.pool, kind='Copilot credits')}</li>")
    pdd = _per_dev_day(t, basis)
    share = _cache_share(t.usage)
    ctx = [f"mean {t.usage.total_input // t.requests:,} tokens" if t.requests else "no requests"]
    if len(t.cell_contexts) > 1:
        ctx.append(f"p50 {_percentile(t.cell_contexts, 50):,} · p90 "
                   f"{_percentile(t.cell_contexts, 90):,} across published cells")
    daily = ""
    if len(t.daily) > 1:
        daily = (f" (daily p50 {esc(chip(exact(_percentile(t.daily, 50), basis)))}, p90 "
                 f"{esc(chip(exact(_percentile(t.daily, 90), basis)))} over days with ≥ {k} "
                 "active developers)")
    finding_rows = [[esc(f.title), money(f.projected_monthly, per="/mo", range_label="p10–p90")
                     if f.projected_monthly is not None else (money(f.recoverable_shapley)
                     if f.recoverable_shapley is not None else ""), money(f.cost_observed),
                     esc(f.kind)] for f in t.findings[:_TOP]]
    levers = [[esc(lid), money(fig)] for lid, fig in _team_levers(t, plan)]
    body = [f"<header><h1>Showback — {esc(t.name)}</h1><p class=\"muted\">{esc(_users(t))} "
            f"active developers · {t.requests:,} requests · {t.dev_days:,} developer-days</p>"
            '<p><a href="index.html">All teams</a></p></header>',
            f'<section><h2>Spend</h2><ul>{"".join(spend)}</ul>'
            f"<p>Cost per active developer-day: {money(pdd) if pdd else 'n/a'}{daily}</p>"
            f"<p>Cache-read share: {esc(pct(share))} — {esc(_cache_verdict(share))}</p>"
            f"<p>Context per request: {esc(' · '.join(ctx))}</p></section>",
            "<section><h2>Top findings (Shapley-ranked)</h2>"
            + table("Findings", ["finding", "monthly / Shapley", "observed", "kind"],
                    finding_rows, raw=True) + "</section>",
            ("<section><h2>Related action-plan levers</h2>"
             + table("Levers (Shapley credit, never summed)", ["lever", "Shapley (window)"],
                     levers, raw=True) + "</section>") if levers else "",
            "<section><h2>Benchmarks</h2>" + _benchmark_table() + "</section>"]
    return page(f"Showback — {t.name}", body)


def _index_page(teams: list[_Team], paths: list[_PathStat], coverage: str,
                plan: ActionPlan | None, agg: PublishedAggregate) -> str:
    items = [(t.name, (t.exact.nano or 0) if t.exact is not None else 0) for t in teams]
    rows = [[f'<a href="{_slug(t.name)}.html">{esc(t.name)}</a>', esc(_users(t)),
             esc(f"{t.requests:,}"), money(t.exact) if t.exact is not None else "",
             money(t.allowance, kind="allowance") if t.allowance is not None else "",
             esc(pct(_cache_share(t.usage)))] for t in teams]
    twin = table(f"Spend by team (published aggregate, k = {agg.k}; {agg.suppressed_rows} rows "
                 "merged or withheld)", ["team", "users", "requests", "exact bill",
                                         "allowance (list-equivalent)", "cache-read share"],
                 rows, raw=True, numeric=(1, 2))
    body = [f"<header><h1>Team showback</h1><p class=\"muted\">{esc(date_of(agg.window[0]))} to "
            f"{esc(date_of(agg.window[1]))} (end exclusive) · allocation coverage "
            f"{esc(pct(coverage))}</p></header>",
            "<section><h2>Spend by team</h2>" + bar_figure("Exact bill by team", items, twin)
            + "</section>"]
    if plan is not None:
        body.append("<section><h2>Organization action plan</h2><p>Headline: "
                    f"{money(plan.headline_monthly, per='/mo', range_label='p10–p90')}</p>"
                    "</section>")
    if paths:
        prow = [[esc(p.path), esc(f"{p.users:,}"), money(p.mean, per="/dev-month"),
                 money(p.p50, per="/dev-month") if p.p50 else "n/a",
                 money(p.p90, per="/dev-month") if p.p90 else "n/a",
                 esc(pct(p.overage_share)) if p.overage_share is not None else ""]
                for p in paths]
        body.append("<section><h2>Spend per active developer-month by billing path</h2>"
                    + table("Per billing path (cells with at least k developers)",
                            ["billing path", "developers", "mean", "p50", "p90",
                             "overage share of seat requests"], prow, raw=True, numeric=(1,))
                    + "</section>")
    body.append("<section><h2>Benchmarks</h2>" + _benchmark_table() + "</section>")
    return page("Team showback", body)


def _money_cell(fig: Figure | None) -> tuple[str, str]:
    if fig is None or fig.nano is None:
        return "", ""
    return nano_to_usd_str(fig.nano), f"{fig.evidence.value}·{fig.basis.value}"


def _csv(teams: list[_Team], paths: list[_PathStat]) -> tuple[str, str]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["team", "users", "requests", "exact_usd", "exact_label", "allowance_usd",
                "allowance_label", "pool_usd", "pool_label", "developer_days",
                "cost_per_dev_day_usd", "cache_read_share", "context_mean_tokens",
                "top_finding_ids"])
    for t in teams:
        basis = t.exact.basis if t.exact is not None else Basis.LIST
        name = t.name
        if name.startswith(("=", "+", "-", "@")):
            name = "'" + name
        w.writerow([name, "unknown" if t.users_unknown else t.users, t.requests,
                    *_money_cell(t.exact), *_money_cell(t.allowance), *_money_cell(t.pool),
                    t.dev_days, _money_cell(_per_dev_day(t, basis))[0],
                    _cache_share(t.usage) or "",
                    t.usage.total_input // t.requests if t.requests else "",
                    " ".join(f.finding_id for f in t.findings[:_TOP])])
    pbuf = io.StringIO()
    pw = csv.writer(pbuf, lineterminator="\n")
    pw.writerow(["billing_path", "developers", "cells", "mean_usd_per_dev_month", "mean_label",
                 "p50_usd", "p90_usd", "overage_share"])
    for p in paths:
        pw.writerow([p.path, p.users, p.cells, *_money_cell(p.mean), _money_cell(p.p50)[0],
                     _money_cell(p.p90)[0], p.overage_share or ""])
    return buf.getvalue(), pbuf.getvalue()


def _opt(fig: Figure | None) -> dict[str, object] | None:
    return None if fig is None else figure_json(fig)


def _json(teams: list[_Team], paths: list[_PathStat], coverage: str, agg: PublishedAggregate,
          k: int) -> dict[str, object]:
    return {
        "schema": "tokenbill/showback@1",
        "window": {"since": date_of(agg.window[0]), "until": date_of(agg.window[1])},
        "privacy": {"k": k, "suppressed_rows": agg.suppressed_rows,
                    "suppressed_users": agg.suppressed_users, "evidence": _EXACT},
        "allocation_coverage": coverage,
        "anchors": [{"name": n, "value": v, "source": s, "checked_on": d}
                    for n, v, s, d in (*_ANCHORS, *_BENCHMARKS)],
        "teams": [{
            "team": t.name,
            "users": None if t.users_unknown else t.users,
            "notes": [USERS_UNKNOWN] if t.users_unknown else [],
            "requests": t.requests, "developer_days": t.dev_days,
            "exact": _opt(t.exact), "estimated": _opt(t.estimated),
            "allowance": _opt(t.allowance), "pool": _opt(t.pool),
            "cost_per_dev_day": _opt(_per_dev_day(t, t.exact.basis if t.exact else Basis.LIST)),
            "cache_read_share": _cache_share(t.usage),
            "cache_read_verdict": _cache_verdict(_cache_share(t.usage)),
            "context_mean_tokens": t.usage.total_input // t.requests if t.requests else None,
            "top_findings": [{"finding_id": f.finding_id, "kind": f.kind, "title": f.title,
                              "projected_monthly": _opt(f.projected_monthly),
                              "recoverable_shapley": _opt(f.recoverable_shapley),
                              "cost_observed": figure_json(f.cost_observed)}
                             for f in t.findings[:_TOP]],
            "evidence": _EXACT,
        } for t in teams],
        "billing_paths": [{"billing_path": p.path, "developers": p.users, "cells": p.cells,
                           "mean_per_dev_month": figure_json(p.mean), "p50": _opt(p.p50),
                           "p90": _opt(p.p90), "overage_share": p.overage_share,
                           "evidence": _EXACT} for p in paths],
    }


def render_showback(teams: PublishedAggregate, cluster_days: Sequence[ClusterDay],
                    findings: Sequence[Finding], plan: ActionPlan | None, out_dir: Path, *,
                    formats: Sequence[str] = ("html",),
                    billing_paths: PublishedAggregate | None = None) -> list[Path]:
    """Write the team showback into *out_dir* in *formats*; returns the written paths (sorted).

    *teams* must be a ``PublishedAggregate`` grouped by ``team`` (and optionally finer dims),
    *billing_paths* one grouped by ``billing_path``; a raw aggregate raises ``ContractViolation``,
    a person or person-proxy grouping ``PrivacyError``, an unknown format ``UsageError``.
    """
    _check_grouping(teams, "showback teams", "team")
    if billing_paths is not None:
        _check_grouping(billing_paths, "showback billing paths", "billing_path")
    fmts = tuple(formats)
    if not fmts or any(f not in FORMATS for f in fmts):
        raise UsageError("showback formats must be a non-empty subset of html, csv, json")
    k = teams.k
    by_team = _teams(teams)
    _cluster_days(by_team, cluster_days, k)
    _attach_findings(by_team, findings)
    ordered = sorted(by_team.values(), key=lambda t: (
        t.name in (_OTHER, _UNATTRIBUTED), -((t.exact.nano or 0) if t.exact else 0), t.name))
    paths = _billing_paths(billing_paths, k) if billing_paths is not None else []
    coverage = _coverage(teams)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(name: str, text: str) -> None:
        target = out / name
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)

    if "html" in fmts:
        write("index.html", _index_page(ordered, paths, coverage, plan, teams))
        for t in ordered:
            write(f"{_slug(t.name)}.html", _team_page(t, plan, k))
    if "csv" in fmts:
        team_csv, path_csv = _csv(ordered, paths)
        write("showback.csv", team_csv)
        if billing_paths is not None:
            write("showback-billing-paths.csv", path_csv)
    if "json" in fmts:
        write("showback.json", canonical_dumps(_json(ordered, paths, coverage, teams, k)) + "\n")
    return sorted(written)

