"""Copilot team showback (addendum §14.2 "Showback", CP-OUT).

:func:`render_copilot_showback` writes the per-team Copilot showback from **published aggregates
only** (``CopilotSummary.teams``, ``seat_counts``, ``editor_split``; never a raw record):

* seats by plan and activity bucket (k-anonymous counts);
* pooled credits (list-equivalent, never dollars) and GitHub's own per-row net (dollars, labelled
  by R16 per entity × month and combined with ``combine_weakest``) — credits and dollars are never
  summed;
* the entity overage allocated **pro rata by pooled credits** (``AllocatedMethodId =
  copilot_pro_rata_credits``, ESTIMATED): per entity × month (and per plan scenario while the plan
  is unknown) the allocation over the published team rows plus one "not attributable" row for
  credits merged below *k* sums to the entity's overage to the nano;
* the share of the entity pool (per scenario while unknown);
* pooled interactive credits per active developer-month: p50 / p90 of the team's month cells
  (cells with ≥ k users only), weighted by developers;
* code review, cloud agent, agentic workflow and Code Quality credits per team;
* the VS Code vs JetBrains split (``editor_split``, ESTIMATED apportionment; other editors merged);
* the team's top Copilot findings (Shapley-ranked).

Formats: ``html`` (``copilot-showback.html``: CSP, no scripts, no external URL, every SVG followed
by its table twin with a caption), ``csv`` (``copilot-showback.csv``) and ``json``
(``copilot-showback.json``: MONEY objects, ``evidence`` keys, no floats). Merged small teams print
as ``(other teams)``; rows whose user count is unknown print "users unknown" (R-E47).
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from tokenbill.copilot.render import chip, clean, credits_text, esc, label, money
from tokenbill.copilot.summary import (
    INTERACTIVE,
    TEAM_GROUP_BY,
    apportion,
    is_reconciled,
    r16_figure,
)
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.kanon import USERS_UNKNOWN, row_notes
from tokenbill.core.labels import (
    Basis,
    Evidence,
    Figure,
    combine_weakest,
    estimated,
    exact,
    figure_json,
    scale,
)
from tokenbill.core.money import nano_to_usd_str, ratio
from tokenbill.core.types import CopilotSummary, Finding, PoolMonth, PublishedAggregate, RunResult

__all__ = ["ALLOCATED_METHOD_ID", "CSP", "FORMATS", "PALETTES", "allocate_overage",
           "render_copilot_showback"]

FORMATS = ("html", "csv", "json")
#: FOCUS ``AllocatedMethodId`` of the pro-rata overage allocation (addendum §14.2).
ALLOCATED_METHOD_ID = "copilot_pro_rata_credits"
#: Content-Security-Policy of the showback page (SPEC §8.7).
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
#: Theme colors: ``text`` keys reach 4.5:1 against both surfaces, ``marks`` 3:1 (WCAG 2.2 AA).
PALETTES: dict[str, dict[str, dict[str, str]]] = {
    "light": {"surface": {"bg": "#ffffff", "panel": "#f2f3f5"},
              "text": {"fg": "#1a1a1a", "muted": "#4a4d52"},
              "marks": {"bar": "#2563a8", "line": "#6b6f76"}},
    "dark": {"surface": {"bg": "#111316", "panel": "#1c1f24"},
             "text": {"fg": "#ececec", "muted": "#b7bbc2"},
             "marks": {"bar": "#7fb0ff", "line": "#8a8f98"}},
}
OTHER_TEAMS = "(other teams)"
UNATTRIBUTED = "(unattributed)"
NOT_ATTRIBUTABLE = "(not attributable: merged below k)"
_WORKLOAD_WORD = {INTERACTIVE: "IDE, chat, CLI and agent use", "copilot_code_review": "code review",
                  "copilot_cloud_agent": "cloud agent", "agentic_workflow": "agentic workflows",
                  "code_quality": "Code Quality"}
_TOP_FINDINGS = 5
_SCHEMA = "tokenbill/copilot-showback@1"
_INTRO = ("window {} … {}; teams below k are merged into (other teams). Credits are "
          "list-equivalent, never dollars; GitHub's net is labelled invoice only for closed, "
          "reconciled months; the allocated overage is ESTIMATED.")


@dataclass
class _Team:
    name: str
    credits: int = 0
    nets: list[Figure] = field(default_factory=list)
    users: int = 0
    users_unknown: bool = False
    workloads: dict[str, int] = field(default_factory=dict)
    dev_cells: list[tuple[int, int]] = field(default_factory=list)   # (mean nano, developers)
    overage: dict[str, int] = field(default_factory=dict)            # scenario key → nano
    shares: dict[str, tuple[int, int]] = field(default_factory=dict)  # key → (credits, pool)
    seats: list[tuple[str, int]] = field(default_factory=list)
    editors: list[tuple[str, int | None, int, Figure | None]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _is_merged(value: object) -> bool:
    return isinstance(value, str) and value.startswith("(other")


def _team_name(value: str | None) -> str:
    if value is None or value == "":
        return UNATTRIBUTED
    return OTHER_TEAMS if _is_merged(value) else value


def _scenario_key(pm: PoolMonth) -> str:
    return pm.plan_scenario or "known"


def allocate_overage(summary: CopilotSummary) -> dict[tuple[str, str, str], dict[str, int]]:
    """Per (entity, month, scenario key) the pool month's overage allocated to the published
    team names pro rata by pooled credits (largest remainder), plus :data:`NOT_ATTRIBUTABLE` for
    consumption merged below k; each allocation sums to the pool month's overage exactly. The
    scenario key is ``known`` or the plan scenario."""
    teams = summary.teams
    out: dict[tuple[str, str, str], dict[str, int]] = {}
    for pm in summary.pools:
        weights: dict[str, int] = defaultdict(int)
        if teams is not None:
            for row in teams.rows:
                dims = dict(row.dims)
                if dims.get("entity") == pm.entity_id and dims.get("month") == pm.month:
                    weights[_team_name(dims.get("team"))] += (
                        row.priced.pool.nano if row.priced.pool is not None
                        and row.priced.pool.nano is not None else 0)
        names = sorted(weights)
        rest = max(0, pm.consumed_report_nano - sum(weights.values()))
        parts = apportion(pm.overage_observed_nano, [weights[n] for n in names] + [rest])
        alloc = dict(zip([*names, NOT_ATTRIBUTABLE], parts, strict=True))
        if sum(parts) != pm.overage_observed_nano:   # all weights zero: nothing to attribute
            alloc[NOT_ATTRIBUTABLE] = pm.overage_observed_nano - sum(
                v for k, v in alloc.items() if k != NOT_ATTRIBUTABLE)
        out[(pm.entity_id, pm.month, _scenario_key(pm))] = alloc
    return out


def _percentile(cells: Sequence[tuple[int, int]], q: int) -> int:
    """Nearest-rank weighted percentile *q* (0–100) of cell means weighted by developers."""
    ordered = sorted(cells)
    total = sum(w for _, w in ordered)
    rank = max(1, -(-q * total // 100))
    seen = 0
    for value, w in ordered:
        seen += w
        if seen >= rank:
            return value
    return ordered[-1][0]


def _teams(result: RunResult, s: CopilotSummary) -> dict[str, _Team]:
    agg = s.teams
    if agg is not None and not isinstance(agg, PublishedAggregate):
        raise ContractViolation("showback accepts only published aggregates")
    if agg is not None and tuple(agg.group_by) != TEAM_GROUP_BY:
        raise ContractViolation("CopilotSummary.teams must be grouped by entity, month, team, "
                                "workload")
    teams: dict[str, _Team] = {}
    verdicts = dict(s.channel_verdicts)
    closed = {(p.entity_id, p.month): p.finality == "closed" for p in s.pools}
    k = agg.k if agg is not None else 5
    month_users: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in (agg.rows if agg is not None else ()):
        dims = dict(row.dims)
        name = _team_name(dims.get("team"))
        t = teams.setdefault(name, _Team(name))
        credits = row.priced.pool.nano if row.priced.pool is not None else 0
        credits = credits or 0
        t.credits += credits
        net = row.priced.exact.nano or 0
        key = (str(dims.get("entity")), str(dims.get("month")))
        t.nets.append(r16_figure(net, closed=closed.get(key, False), final=True,
                                 reconciled=is_reconciled(verdicts, "github_copilot")))
        workload = str(dims.get("workload"))
        t.workloads[workload] = t.workloads.get(workload, 0) + credits
        unknown = USERS_UNKNOWN in row_notes(row, group_by=agg.group_by)  # type: ignore[union-attr]
        t.users_unknown = t.users_unknown or unknown
        month_users[(name, *key)] = max(month_users[(name, *key)], row.n_users)
        if (workload == INTERACTIVE and not _is_merged(dims.get("month"))
                and name not in (OTHER_TEAMS,) and row.n_users >= k):
            mean = scale(exact(credits, Basis.LIST_EQUIVALENT), 1, row.n_users).nano or 0
            t.dev_cells.append((mean, row.n_users))
    for (name, _e, _m), n in month_users.items():
        teams[name].users = max(teams[name].users, n)
    for (entity, month, scen), alloc in allocate_overage(s).items():
        pm = next(p for p in s.pools if p.entity_id == entity and p.month == month
                  and _scenario_key(p) == scen)
        for name, nano in alloc.items():
            t = teams.setdefault(name, _Team(name))
            t.overage[scen] = t.overage.get(scen, 0) + nano
            if agg is not None and name != NOT_ATTRIBUTABLE:
                own = sum((r.priced.pool.nano or 0) for r in agg.rows
                          if r.priced.pool is not None
                          and dict(r.dims).get("entity") == entity
                          and dict(r.dims).get("month") == month
                          and _team_name(dict(r.dims).get("team")) == name)
                c, p = t.shares.get(scen, (0, 0))
                t.shares[scen] = (c + own, p + pm.pool_nano)
    for team, pb, n in s.seat_counts:
        name = OTHER_TEAMS if team == "(other)" else team
        teams.setdefault(name, _Team(name)).seats.append((pb, n))
    if s.editor_split is not None:
        for row in s.editor_split.rows:
            dims = dict(row.dims)
            name = _team_name(dims.get("team"))
            fam = dims.get("editor_family")
            unknown = USERS_UNKNOWN in row_notes(row, group_by=s.editor_split.group_by)
            teams.setdefault(name, _Team(name)).editors.append(
                ("other editors" if _is_merged(fam) else str(fam),
                 None if unknown else row.n_users, row.n_requests, row.priced.pool))
    for f in result.findings:
        dims = dict(f.scope.dims)
        if dims.get("product") == "copilot" and dims.get("team") in teams:
            teams[str(dims["team"])].findings.append(f)
    for t in teams.values():
        t.findings.sort(key=lambda f: (-_saving(f), f.finding_id))
        del t.findings[_TOP_FINDINGS:]
    return dict(sorted(teams.items(), key=lambda kv: (kv[0].startswith("("), kv[0])))


def _saving(f: Finding) -> int:
    fig = f.recoverable_shapley or f.recoverable
    return fig.nano if fig is not None and fig.nano is not None else -1


def _net(t: _Team) -> Figure | None:
    if not t.nets:
        return None
    return combine_weakest(t.nets, note="GitHub's per-row net of the team's pooled rows")


def _alloc_fig(nano: int, scen: str) -> Figure:
    note = (f"overage allocated pro rata by pooled credits ({ALLOCATED_METHOD_ID})"
            + ("" if scen == "known" else f"; plan unknown: scenario {scen}"))
    return estimated(nano, Basis.LIST, note=note)


def _share(c: int, p: int) -> str:
    value = ratio(c * 100, p)
    return "pool unknown" if value is None else f"{value.quantize(Decimal(1))}% of the entity pool"


def _measures(t: _Team) -> list[tuple[str, str, Figure | None, str]]:
    """(measure, scenario, figure, text) rows of one team — credits and dollars apart."""
    rows: list[tuple[str, str, Figure | None, str]] = [
        ("pooled credits", "", exact(t.credits, Basis.LIST_EQUIVALENT),
         credits_text(t.credits) + " — list-equivalent, not invoice dollars")]
    net = _net(t)
    if net is not None:
        rows.append(("GitHub's net of the team's rows", "", net, "dollars (R16 label)"))
    for scen in sorted(t.overage):
        rows.append(("overage allocated", scen, _alloc_fig(t.overage[scen], scen),
                     f"AllocatedMethodId {ALLOCATED_METHOD_ID}"))
    for scen in sorted(t.shares):
        c, p = t.shares[scen]
        rows.append(("share of the entity pool", scen, None, _share(c, p)))
    for workload in sorted(t.workloads):
        rows.append((f"credits: {_WORKLOAD_WORD.get(workload, workload)}", "",
                     exact(t.workloads[workload], Basis.LIST_EQUIVALENT),
                     credits_text(t.workloads[workload])))
    if t.dev_cells:
        for q in (50, 90):
            v = _percentile(t.dev_cells, q)
            rows.append((f"interactive credits per active developer-month p{q}", "",
                         exact(v, Basis.LIST_EQUIVALENT), credits_text(v)))
    return rows


def _svg(t: _Team) -> str:
    items = sorted(t.workloads.items())
    top = max([v for _, v in items] + [1])
    bars = []
    for i, (w, v) in enumerate(items):
        width = (max(0, v) * 300 + top // 2) // top
        bars.append(f'<rect x="150" y="{6 + 20 * i}" width="{width}" height="12" '
                    'style="fill:var(--bar)"/>'
                    f'<text x="4" y="{16 + 20 * i}" style="fill:var(--fg);font-size:11px">'
                    f"{esc(_WORKLOAD_WORD.get(w, w))}</text>")
    height = 12 + 20 * len(items)
    return (f'<svg role="img" aria-label="{esc(f"credits by workload, {t.name}")}" '
            f'viewBox="0 0 460 {height}" width="460" height="{height}">' + "".join(bars)
            + "</svg>")


def _table(caption: str, head: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    return ("<table><caption>" + esc(caption) + "</caption><thead><tr>"
            + "".join(f'<th scope="col">{esc(h)}</th>' for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
            + "</tbody></table>")


def _css() -> str:
    def vars_of(theme: str) -> str:
        p = PALETTES[theme]
        return ";".join(f"--{k}:{v}" for part in p.values() for k, v in part.items())

    return (":root{color-scheme:light dark;" + vars_of("light") + "}"
            "@media (prefers-color-scheme: dark){:root{" + vars_of("dark") + "}}"
            "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"
            "sans-serif}main{max-width:1100px;margin:0 auto;padding:24px}"
            "table{border-collapse:collapse;margin:.4rem 0 1rem}caption{text-align:left;"
            "font-weight:600}td,th{border:1px solid var(--line);padding:3px 8px;text-align:left}"
            "section{background:var(--panel);padding:8px 16px;margin:12px 0}"
            ".muted{color:var(--muted)}")


def _html(result: RunResult, s: CopilotSummary, teams: Mapping[str, _Team]) -> str:
    out = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
           f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
           "<title>Token Bill — Copilot showback</title>",
           f"<style>{_css()}</style></head><body><main>",
           "<h1>GitHub Copilot showback</h1>",
           f'<p class="muted">{esc(_INTRO.format(s.window[0], s.window[1]))}</p>']
    if any(p.plan_scenario for p in s.pools):
        out.append("<p><strong>Plan unknown</strong>: overage allocation and pool share are shown "
                   "for Business and for Enterprise; neither is chosen.</p>")
    for t in teams.values():
        out.append(f'<section><h2>{esc(t.name)}</h2>')
        users = "users unknown" if t.users_unknown and not t.users else (
            f"at least {t.users} active developers" if t.users else "no pooled usage")
        out.append(f'<p class="muted">{esc(users)}</p>')
        if t.seats:
            out.append(_table(f"Seats by plan and activity bucket — {t.name}",
                              ["plan:bucket", "seats"], [(pb, str(n)) for pb, n in t.seats]))
        out.append(_table(f"Credits and dollars (never summed) — {t.name}",
                          ["measure", "scenario", "amount", "label", "detail"],
                          [(m, sc, money(f) if f else "", label(f) if f else "", txt)
                           for m, sc, f, txt in _measures(t)]))
        if t.workloads:
            out.append(_svg(t))
            out.append(_table(f"Credits by workload — {t.name}", ["workload", "credits"],
                              [(_WORKLOAD_WORD.get(w, w), credits_text(v))
                               for w, v in sorted(t.workloads.items())]))
        if t.editors:
            out.append(_table(f"Editors (credits apportioned by interactions, ESTIMATED) — "
                              f"{t.name}", ["editor", "developers", "interactions", "credits"],
                              [(fam, "users unknown" if n is None else str(n), str(i),
                                chip(f)) for fam, n, i, f in t.editors]))
        if t.findings:
            out.append("<ul>" + "".join(
                f"<li>{esc(f.title)} ({esc(chip(f.recoverable_shapley or f.recoverable))})</li>"
                for f in t.findings) + "</ul>")
        out.append("</section>")
    out.append("</main></body></html>")
    return "".join(out)


def _csv(teams: Mapping[str, _Team]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["team", "measure", "scenario", "amount_usd", "evidence", "basis", "detail"])
    for t in teams.values():
        for pb, n in t.seats:
            w.writerow([clean(t.name), f"seats {pb}", "", "", "exact", "", str(n)])
        for m, sc, f, txt in _measures(t):
            usd = "" if f is None or f.nano is None else nano_to_usd_str(f.nano)
            w.writerow([clean(t.name), m, sc, usd, f.evidence.value if f else "",
                        f.basis.value if f else "", clean(txt)])
        for fam, n, i, f in t.editors:
            usd = "" if f is None or f.nano is None else nano_to_usd_str(f.nano)
            w.writerow([clean(t.name), f"editor {fam} credits", "", usd,
                        f.evidence.value if f else "", f.basis.value if f else "",
                        f"{'users unknown' if n is None else n} developers; {i} interactions"])
    return buf.getvalue()


def _json(s: CopilotSummary, teams: Mapping[str, _Team]) -> str:
    doc = {"schema": _SCHEMA, "window": list(s.window), "evidence": Evidence.EXACT.value,
           "k": s.teams.k if s.teams is not None else None,
           "allocated_method_id": ALLOCATED_METHOD_ID, "teams": []}
    for t in teams.values():
        doc["teams"].append({  # type: ignore[union-attr]
            "team": clean(t.name), "evidence": Evidence.EXACT.value,
            "users": None if t.users_unknown and not t.users else t.users,
            "seats": [{"plan_bucket": pb, "seats": n, "evidence": "exact"} for pb, n in t.seats],
            "measures": [{"measure": m, "scenario": sc or None,
                          "amount": None if f is None else figure_json(f), "detail": clean(txt)}
                         for m, sc, f, txt in _measures(t)],
            "editors": [{"editor": fam, "developers": n, "interactions": i,
                         "credits": None if f is None else figure_json(f), "evidence": "exact"}
                        for fam, n, i, f in t.editors],
            "findings": [f.finding_id for f in t.findings],
        })
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False) + "\n"


def render_copilot_showback(result: RunResult, out_dir: Path,
                            formats: Sequence[str] = ("html",)) -> list[Path]:
    """Write the Copilot showback of *result* into *out_dir* (see the module docstring); returns
    the paths written (none without Copilot data). ``UsageError`` for an unknown format."""
    if not isinstance(result, RunResult):
        raise ContractViolation("render_copilot_showback expects a RunResult")
    if isinstance(formats, str):
        formats = (formats,)
    bad = sorted(set(formats) - set(FORMATS))
    if bad:
        raise UsageError(f"showback format(s) {', '.join(bad)} not in {', '.join(FORMATS)}")
    s = result.copilot
    if s is None:
        return []
    teams = _teams(result, s)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fmt in FORMATS:
        if fmt not in formats:
            continue
        name, text = {"html": ("copilot-showback.html", lambda: _html(result, s, teams)),
                      "csv": ("copilot-showback.csv", lambda: _csv(teams)),
                      "json": ("copilot-showback.json", lambda: _json(s, teams))}[fmt]
        path = out_dir / name
        path.write_text(text(), encoding="utf-8")
        written.append(path)
    return written
