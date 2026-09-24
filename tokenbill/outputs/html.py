"""Single-file HTML report (SPEC §14.3, §8.7; package OUT).

:func:`render_html` writes one self-contained page: inline CSS and SVG, **no scripts, no external
resource and no external URL** (source URLs print without their scheme, as text), the SPEC §8.7
Content-Security-Policy meta tag, light and dark themes through ``prefers-color-scheme`` with WCAG
2.2 AA contrast (text ≥ 4.5:1 on the page and panel backgrounds, chart marks ≥ 3:1; checked by the
tests from :data:`PALETTES`), labels always spelled out (never color alone) and every ``<svg>``
chart immediately followed by its ``<table>`` twin with a ``<caption>``.

Sections: evidence legend; bill (exact, coverage, per-channel reconciliation badges, estimated,
allowance and Copilot pool apart); where the money goes (published aggregates only, with the
context tax of the ``bucket`` breakdown); findings ranked by Shapley credit (label badges, evidence,
fix, config patch); action plan; calibration panel (NMBE / CV(RMSE), ρ, diagnostics matrix);
reconciliation panel (per channel); policy-pack preview; replays, measurement, A/B, check and
pricing panels when present; data quality; methodology and provenance (rate card, sources,
verification dates, assumptions, threats to validity). Large inputs keep the page under
:data:`MAX_BYTES` by printing the top :data:`TOP_ROWS` rows / :data:`TOP_FINDINGS` findings
(the JSON output has everything).

Every text passes ``textsafe.sanitize``, person pseudonyms (``p_``/``c_`` + 20 hex) are replaced
and everything is HTML-escaped. Extension sections (``RunResult.copilot``) come from
``core.extensions.render_sections(result, "html")`` and are refused (``ContractViolation``) when
they carry a script or an external URL.
"""

from __future__ import annotations

import html as _html
import json
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal

from tokenbill.core import evidence as _evidence
from tokenbill.core import extensions
from tokenbill.core.errors import ContractViolation
from tokenbill.core.kanon import USERS_UNKNOWN, row_notes
from tokenbill.core.labels import Basis, Evidence, Figure
from tokenbill.core.money import ratio
from tokenbill.core.types import (
    ActionPlan,
    BillSummary,
    CalibrationReport,
    Finding,
    PublishedAggregate,
    ReconciliationReport,
    RunResult,
)
from tokenbill.outputs.result_json import (
    check_median,
    date_of,
    extension_slots_present,
    policy_spec,
    require_allowance,
    require_billed,
    require_published,
)
from tokenbill.outputs.terminal import chip, pct, scrub

__all__ = [
    "CSP",
    "MAX_BYTES",
    "PALETTES",
    "TOP_FINDINGS",
    "TOP_ROWS",
    "bar_figure",
    "esc",
    "money",
    "page",
    "render_html",
    "table",
]

#: The Content-Security-Policy of every OUT HTML page (SPEC §8.7).
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
#: Page size budget (SPEC §14.3: ≤ 5 MB for 10^4 lanes).
MAX_BYTES = 5 * 1024 * 1024
TOP_ROWS = 50
TOP_FINDINGS = 50
#: Theme colors (CSS custom properties). ``text`` keys must reach 4.5:1 against ``bg`` and
#: ``panel``; ``marks`` keys 3:1 against ``bg`` (WCAG 2.2 AA 1.4.3 / 1.4.11).
PALETTES: dict[str, dict[str, dict[str, str]]] = {
    "light": {
        "surface": {"bg": "#ffffff", "panel": "#f2f3f5"},
        "text": {"fg": "#1a1a1a", "muted": "#4a4d52", "exact": "#135f28", "est": "#5a2b9c",
                 "le": "#7a4000", "bad": "#9f1b1b", "accent": "#0b4f94"},
        "marks": {"bar": "#2563a8", "bar-le": "#9a5a00", "line": "#6b6f76"},
    },
    "dark": {
        "surface": {"bg": "#111316", "panel": "#1c1f24"},
        "text": {"fg": "#ececec", "muted": "#b7bbc2", "exact": "#7fd18f", "est": "#c9a4ff",
                 "le": "#ffb86b", "bad": "#ff8f8f", "accent": "#8cb8ff"},
        "marks": {"bar": "#7fb0ff", "bar-le": "#ffb86b", "line": "#8a8f98"},
    },
}
_SCHEME_RE = re.compile(r"(?i)\b(?:https?|ftp)://")
_SCRIPT_RE = re.compile(r"(?i)<\s*script|javascript:")
_INPUT_BUCKETS = frozenset({"uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                            "cache_write_other", "cache_write_unknown"})


def _vars(theme: str) -> str:
    p = PALETTES[theme]
    items = {**p["surface"], **p["text"], **p["marks"]}
    return ";".join(f"--{k}:{v}" for k, v in items.items())


_CSS = (
    ":root{color-scheme:light dark;" + _vars("light") + "}"
    "@media (prefers-color-scheme: dark){:root{" + _vars("dark") + "}}"
    "*{box-sizing:border-box}"
    "body{margin:0;background:var(--bg);color:var(--fg);"
    "font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
    "main{max-width:1120px;margin:0 auto;padding:24px}"
    "h1{font-size:1.6rem;margin:0 0 .3rem}h2{font-size:1.25rem;margin:2rem 0 .6rem;"
    "border-bottom:1px solid var(--line);padding-bottom:.2rem}"
    "h3{font-size:1.05rem;margin:1.2rem 0 .4rem}"
    "p,li,dd{max-width:80ch}.muted{color:var(--muted)}"
    "section,article{margin:0 0 1rem}article{background:var(--panel);padding:.8rem 1rem;"
    "border-left:4px solid var(--line);border-radius:4px}"
    "table{border-collapse:collapse;margin:.4rem 0 1rem;width:100%;background:var(--bg)}"
    "caption{text-align:left;font-weight:600;padding:.3rem 0;color:var(--fg)}"
    "th,td{border:1px solid var(--line);padding:.25rem .5rem;text-align:left;vertical-align:top}"
    "td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}"
    ".chip{font-weight:600}.ev-exact{color:var(--exact)}.ev-estimated{color:var(--est)}"
    ".ev-measured,.ev-verified{color:var(--accent)}.ev-le{color:var(--le)}.bad{color:var(--bad)}"
    ".ok{color:var(--exact)}.banner{background:var(--panel);border:2px solid var(--bad);"
    "padding:.5rem 1rem;font-weight:700;color:var(--bad)}"
    "pre{background:var(--panel);padding:.5rem;overflow:auto;color:var(--fg)}"
    "figure{margin:0 0 1rem}svg{max-width:100%;height:auto}"
    "svg .bar{fill:var(--bar)}svg .bar-le{fill:var(--bar-le)}svg text{fill:var(--fg);"
    "font:12px system-ui,sans-serif}"
    "dl.legend{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem}"
    "dt{font-weight:700}"
)


def esc(text: object, limit: int | None = 2000) -> str:
    """Sanitized, pseudonym-scrubbed, scheme-stripped and HTML-escaped text."""
    return _html.escape(_SCHEME_RE.sub("", scrub(text, limit)), quote=True)


def _ev_class(fig: Figure) -> str:
    if fig.basis is Basis.LIST_EQUIVALENT:
        return "ev-le"
    return f"ev-{fig.evidence.value}"


def money(fig: Figure, **kw: object) -> str:
    """A money value with its label chip (``terminal.chip`` text; the class only adds color)."""
    return f'<span class="chip {_ev_class(fig)}">{esc(chip(fig, **kw))}</span>'  # type: ignore[arg-type]


def table(caption: str, headers: Sequence[str], rows: Iterable[Sequence[str]], *,
          numeric: Sequence[int] = (), raw: bool = False) -> str:
    """A ``<table>`` with ``<caption>``; cells are escaped unless *raw* (pre-rendered HTML)."""
    head = "".join(f'<th scope="col"{" class=num" if i in numeric else ""}>{esc(h)}</th>'
                   for i, h in enumerate(headers))
    body = []
    for row in rows:
        cells = "".join(
            f'<td{" class=num" if i in numeric else ""}>{c if raw else esc(c)}</td>'
            for i, c in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return (f"<table><caption>{esc(caption)}</caption><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def bar_figure(title: str, items: Sequence[tuple[str, int]], twin: str, *,
               allowance: bool = False) -> str:
    """An inline SVG bar chart of ``(label, value)`` followed by its table twin *twin* (the
    accessible, color-free rendering of the same numbers)."""
    rows = list(items)[:TOP_ROWS]
    top = max((v for _l, v in rows), default=0)
    height = 22 * len(rows) + 8
    cls = "bar-le" if allowance else "bar"
    parts = [f'<svg viewBox="0 0 720 {height}" width="720" '
             f'height="{height}" role="img" aria-label="{esc(title)}"><title>{esc(title)}</title>']
    for i, (label, value) in enumerate(rows):
        y = 4 + 22 * i
        w = 0 if top <= 0 or value <= 0 else max(1, value * 440 // top)
        parts.append(f'<text x="0" y="{y + 15}">{esc(label, 32)}</text>'
                     f'<rect class="{cls}" x="220" y="{y + 2}" width="{w}" height="16"/>')
    parts.append("</svg>")
    return f"<figure>{''.join(parts)}{twin}</figure>"


def page(title: str, body: Sequence[str]) -> str:
    """A complete, self-contained HTML document with the CSP meta tag and the theme CSS."""
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(title, 200)}</title><style>{_CSS}</style></head><body><main>"
            + "".join(body) + "</main></body></html>\n")


# ---------------------------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------------------------

_LEGEND = (
    ("exact", "billed tokens × a sourced rate row; pure arithmetic"),
    ("estimated", "any model, reconstruction or assumption; shown with its range and "
                  "calibration"),
    ("measured / verified", "a rollout estimate on the billed ledger with a confidence interval"),
    ("list-equivalent", "seat-allowance usage or Copilot credits priced at list; never billed"),
)


def _legend() -> str:
    items = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in _LEGEND)
    return f'<section id="legend"><h2>Evidence labels</h2><dl class="legend">{items}</dl></section>'


def _header(r: RunResult) -> str:
    out = [f"<header><h1>Token Bill — {esc(r.command)}</h1>"
           f'<p class="muted">{esc(date_of(r.window[0]))} to {esc(date_of(r.window[1]))} (end '
           f"exclusive) · {len(r.inputs)} sources · privacy: content tier "
           f"{esc(r.privacy.content_tier.value)}, k = {r.privacy.k}, "
           f"{r.privacy.suppressed_groups} small groups merged or withheld</p>"]
    if r.synthetic:
        out.append('<p class="banner">Synthetic demo data — not a real bill.</p>')
    out.append("</header>")
    return "".join(out)


def _verdict(verdict: str) -> str:
    label = verdict.replace("_", " ")
    cls = "ok" if verdict == "reconciled" else "bad"
    return f'<span class="chip {cls}">{esc(label)}</span>'


def _bill(r: RunResult, bill: BillSummary) -> str:
    t = bill.total
    exact = require_billed(t.exact, "html bill exact")
    lines = [f"<li>Exact bill: {money(exact, unpriced_count=t.unpriced_inferences)}</li>",
             f"<li>Coverage: {esc(pct(t.coverage))} of billable tokens priced"
             + (f"; {t.unpriced_inferences:,} inferences ({t.unpriced_tokens:,} tokens) unpriced "
                "— unknown is not zero" if t.unpriced_inferences else "") + "</li>"]
    if r.rate_card is not None:
        lines.append(f"<li>Basis {esc(r.rate_card.basis.value)} · rate card "
                     f"<code>{esc(r.rate_card.sha256[:16])}</code></li>")
    if t.estimated is not None:
        if t.estimated.basis is Basis.LIST_EQUIVALENT:
            raise ContractViolation("html bill estimated: list-equivalent figures are never billed")
        lines.append(f"<li>Estimated, beside the bill (never in it): {money(t.estimated)}</li>")
    if bill.esr is not None:
        lines.append(f"<li>Effective token savings rate: {esc(pct(bill.esr))} (exact)</li>")
    if bill.naive_ratio is not None:
        lines.append(f"<li>Naive line-sum ratio: {esc(bill.naive_ratio)}×</li>")
    apart = []
    if t.allowance is not None:
        allowance = require_allowance(t.allowance, "html allowance")
        apart.append(f"<li>Seat allowance: {money(allowance, kind='allowance')}</li>")
    if t.pool is not None:
        pool = require_allowance(t.pool, "html pool")
        apart.append(f"<li>Copilot credits seen by collectors: "
                     f"{money(pool, kind='Copilot credits')}</li>")
    badges = ""
    if r.reconciliation is not None:
        badges = table("Reconciliation per channel", ["channel", "verdict"],
                       [[esc(c.channel), _verdict(c.verdict)] for c in r.reconciliation.channels],
                       raw=True)
    else:
        badges = ('<p class="muted">Not reconciled against provider reports: the bill is the '
                  "ledger at the rate card.</p>")
    foot = "".join(f"<li>{esc(f)}</li>" for f in bill.footnotes)
    return ('<section id="bill"><h2>Bill</h2><ul>' + "".join(lines) + "</ul>"
            + (f"<h3>Not billed (list-equivalent)</h3><ul>{''.join(apart)}</ul>" if apart else "")
            + badges + (f'<ul class="muted">{foot}</ul>' if foot else "") + "</section>")


def _context_tax(agg: PublishedAggregate) -> str:
    if "bucket" not in agg.group_by:
        return ""
    idx = agg.group_by.index("bucket")
    total = inputs = 0
    for row in agg.rows:
        n = row.priced.exact.nano or 0
        total += n
        if row.dims[idx][1] in _INPUT_BUCKETS:
            inputs += n
    share = ratio(inputs, total)
    if share is None:
        return ""
    return (f"<p>Context tax: {esc(pct(str(share)))} of the exact bill is paid for input-side "
            "tokens (uncached input, cache reads and cache writes) — exact arithmetic on the "
            "bucket breakdown.</p>")


def _breakdown(key: str, agg: PublishedAggregate) -> str:
    require_published(agg, f"html breakdown {key}")
    rows = list(agg.rows)
    rows.sort(key=lambda r: -(r.priced.exact.nano or 0))
    shown = rows[:TOP_ROWS]
    items, cells = [], []
    for row in shown:
        exact = require_billed(row.priced.exact, f"html breakdown {key}")
        label = " · ".join("(none)" if v is None else str(v) for _k, v in row.dims) or "(all)"
        users = "users unknown" if USERS_UNKNOWN in row_notes(row, group_by=agg.group_by) \
            else f"{row.n_users:,}"
        allowance = money(require_allowance(row.priced.allowance, "html breakdown allowance"),
                          kind="allowance") if row.priced.allowance is not None else ""
        items.append((label, exact.nano or 0))
        cells.append([esc(label), esc(users), esc(f"{row.n_requests:,}"),
                      money(exact, unpriced_count=row.priced.unpriced_inferences), allowance])
    more = (f'<p class="muted">{len(rows) - len(shown):,} more rows in the JSON output.</p>'
            if len(rows) > len(shown) else "")
    twin = table(f"Exact bill by {key} (published aggregate, k = {agg.k}; "
                 f"{agg.suppressed_rows} rows merged or withheld)",
                 [key, "users", "requests", "exact bill", "allowance (list-equivalent)"], cells,
                 numeric=(1, 2), raw=True)
    return (f"<h3>By {esc(key)}</h3>" + bar_figure(f"Exact bill by {key}", items, twin) + more
            + _context_tax(agg))


def _money_goes(bill: BillSummary) -> str:
    if not bill.breakdowns:
        return ""
    return ('<section id="where"><h2>Where the money goes</h2>'
            + "".join(_breakdown(k, agg) for k, agg in bill.breakdowns) + "</section>")


def _saving_nano(f: Finding) -> int:
    fig = f.recoverable_shapley or f.recoverable
    return -1 if fig is None or fig.nano is None else fig.nano


def _finding(f: Finding, rank: int) -> str:
    figs = []
    if f.projected_monthly is not None:
        figs.append(f"monthly {money(f.projected_monthly, per='/mo', range_label='p10–p90')}")
    if f.recoverable_shapley is not None:
        figs.append(f"Shapley credit {money(f.recoverable_shapley)}")
    elif f.recoverable is not None:
        figs.append(f"recoverable {money(f.recoverable)}")
    if f.headroom is not None:
        figs.append(f"headroom {money(require_allowance(f.headroom, 'html headroom'))}")
    figs.append(f"observed {money(f.cost_observed)}")
    tags = [t for t, on in (("needs eval", f.needs_eval), ("upper bound", any(
        x is not None and x.upper_bound for x in (f.recoverable, f.projected_monthly)))) if on]
    scope = ", ".join(f"{k}={v}" for k, v in f.scope.dims) or "org"
    ev = "".join(f"<li>{esc(e.kind)}: " + esc(", ".join(f"{k}={v}" for k, v in e.attrs)) + "</li>"
                 for e in f.evidence[:5])
    fix = ""
    if f.fix is not None:
        fix = f"<p>Fix: {esc(f.fix.text)}</p>"
        if f.fix.config_patch:
            patch = json.dumps({k: v for k, v in f.fix.config_patch}, indent=2, sort_keys=True)
            fix += f"<pre><code>{esc(patch, 4000)}</code></pre>"
        if f.fix.doc_url:
            fix += f'<p class="muted">Docs: {esc(f.fix.doc_url)}</p>'
    return (f"<article><h3>{rank}. {esc(f.title)}</h3>"
            f"<p>{' · '.join(figs)}</p>"
            f'<p class="muted">scope {esc(scope)} · {f.n_events:,} events · {f.n_lanes:,} lanes · '
            f"confidence {esc(f.confidence)}{' · ' + esc(', '.join(tags)) if tags else ''}</p>"
            f"<p>{esc(f.summary)}</p>" + (f"<ul>{ev}</ul>" if ev else "") + fix + "</article>")


def _findings(findings: Sequence[Finding]) -> str:
    dq = [f for f in findings if f.category == "data-quality" or f.kind.startswith("dq.")]
    rest = [f for f in findings if f not in dq]

    def headroom(f: Finding) -> bool:
        fig = f.recoverable_shapley or f.recoverable
        return f.headroom is not None or (fig is not None and fig.basis is Basis.LIST_EQUIVALENT)


    def key(f: Finding) -> tuple[int, str]:
        return (-_saving_nano(f), f.finding_id)

    billed = sorted((f for f in rest if not headroom(f)), key=key)
    room = sorted((f for f in rest if headroom(f)), key=key)
    out = ['<section id="findings"><h2>Findings, ranked by Shapley credit</h2>'
           '<p class="muted">Standalone ceilings are never summed; the action plan total comes '
           "from a joint replay.</p>"]
    out.extend(_finding(f, i) for i, f in enumerate(billed[:TOP_FINDINGS], 1))
    if len(billed) > TOP_FINDINGS:
        out.append(f'<p class="muted">{len(billed) - TOP_FINDINGS:,} more findings in the JSON '
                   "output.</p>")
    if room:
        out.append("<h3>Allowance and pool headroom (list-equivalent, not invoice dollars)</h3>")
        out.extend(_finding(f, i) for i, f in enumerate(room[:TOP_FINDINGS], 1))
    if dq:
        out.append(table("Data-quality findings", ["finding", "summary"],
                         [[f.title, f.summary] for f in dq[:TOP_FINDINGS]]))
    out.append("</section>")
    return "".join(out)


def _plan(p: ActionPlan) -> str:
    rows = [[esc(lv.lever_id), esc(lv.lever_class), money(lv.shapley),
             money(lv.projected_monthly, per="/mo", range_label="p10–p90"),
             esc(", ".join(t for t, on in (("needs eval", lv.needs_eval),
                                           ("upper bound", lv.upper_bound)) if on))]
            for lv in p.levers]
    extra = ""
    if p.allowance_headroom_monthly is not None:
        extra += ("<p>Allowance headroom (not invoice dollars): " + money(require_allowance(
            p.allowance_headroom_monthly, "html plan allowance"), per="/mo",
            range_label="p10–p90") + "</p>")
    if p.pool_headroom_monthly is not None:
        extra += ("<p>Copilot pool headroom: " + money(require_allowance(
            p.pool_headroom_monthly, "html plan pool"), per="/mo", range_label="p10–p90")
            + "</p>")
    return ('<section id="plan"><h2>Action plan</h2>'
            f"<p>Headline: {money(p.headline_monthly, per='/mo', range_label='p10–p90')} "
            f"(billed-basis levers; {esc(p.method)})</p>"
            f"<p>Joint saving in the window: {money(p.joint_saving)}</p>"
            + table("Levers (Shapley credit, never summed standalone)",
                    ["lever", "class", "Shapley (window)", "projected monthly", "tags"], rows,
                    raw=True) + extra + "</section>")


def _calibration(c: CalibrationReport) -> str:
    gate = table("Model gate", ["metric", "documented", "calibrated", "threshold"], [
        ["NMBE %", c.nmbe_pct or "n/a", c.nmbe_pct_calibrated or "n/a", f"±{c.thresholds[0]}"],
        ["CV(RMSE) %", c.cvrmse_pct or "n/a", c.cvrmse_pct_calibrated or "n/a", c.thresholds[1]],
    ])
    rho = table("Hit rate ρ per gap band (95% Wilson interval)",
                ["gap band", "hits", "trials", "low", "high"],
                [[b, f"{h:,}", f"{t:,}", lo, hi] for b, h, t, lo, hi in c.rho], numeric=(1, 2))
    reasons = sorted({s for _p, s, _n in c.diag_confusion})
    preds = sorted({p for p, _s, _n in c.diag_confusion})
    counts = {(p, s): n for p, s, n in c.diag_confusion}
    matrix = table("Diagnostics matrix (predicted cause × server reason)", ["predicted", *reasons],
                   [[p, *(f"{counts.get((p, s), 0):,}" for s in reasons)] for p in preds],
                   numeric=tuple(range(1, len(reasons) + 1)))
    return (f'<section id="calibration"><h2>Calibration</h2><p>Status {esc(c.status)} · '
            f"{c.n_periods} {esc(c.granularity)} periods · label {esc(c.calibration().value)}</p>"
            + gate + rho + matrix + "</section>")


def _reconciliation(rep: ReconciliationReport) -> str:
    chans = table("Channels", ["channel", "verdict", "invoice sources", "mapping verified"],
                  [[esc(c.channel), _verdict(c.verdict), esc(", ".join(c.invoice_sources)),
                    "yes" if c.mapping_verified else "no"] for c in rep.channels], raw=True)
    residuals = table("Residuals (estimated)", ["code", "amount"],
                      [[code, chip(Figure(nano=n, evidence=Evidence.ESTIMATED,
                                          basis=Basis.LIST, note="residual"))]
                       for code, n in rep.residuals])
    return (f'<section id="reconciliation"><h2>Reconciliation</h2><p>Verdict '
            f"{_verdict(rep.verdict)} · finality {esc(rep.finality.value)} · window "
            f"{esc(rep.window[0])} to {esc(rep.window[1])} · token coverage "
            f"{esc(rep.token_coverage_pct or 'n/a')}% · dollar coverage "
            f"{esc(rep.dollar_coverage_pct or 'n/a')}%</p>" + chans + residuals + "</section>")


def _packs(r: RunResult) -> str:
    out = ['<section id="policy"><h2>Policy-pack preview</h2>']
    for pack in r.policy_packs:
        rows = [[esc(e.key), esc(e.value_json), money(e.projection, per="/mo",
                                                      range_label="p10–p90")
                 if e.projection is not None else "", "yes" if e.verified_key else "no (VERIFY)"]
                for e in pack.entries]
        out.append(f"<h3>{esc(pack.target)} · cohort {esc(pack.cohort)}</h3>"
                   + table("Settings", ["key", "value", "projection", "verified key"], rows,
                           raw=True)
                   + f"<pre><code>{esc(pack.merge_patch_json, 8000)}</code></pre>")
    out.append("</section>")
    return "".join(out)


def _others(r: RunResult) -> str:
    out = []
    basis = r.rate_card.basis if r.rate_card is not None else Basis.LIST
    if r.replays:
        out.append('<section id="whatif"><h2>What-if replays</h2>' + table(
            "Counterfactual replays", ["policy", "mode", "baseline", "cost", "saving"],
            [[esc(policy_spec(x.policy)), esc(x.mode), money(x.baseline), money(x.cost),
              money(x.saving)] for x in r.replays], raw=True) + "</section>")
    if r.measure_plan is not None:
        m = r.measure_plan
        out.append('<section id="measure-plan"><h2>Measure plan</h2>' + table(
            f"{m.lever_id} · {m.design}", ["wave", "clusters"],
            [[str(w), ", ".join(cs)] for w, cs in m.waves]
            + [["holdback", ", ".join(m.holdback)]]) + "</section>")
    ms = list(r.measurements) + ([r.ab.measurement] if r.ab is not None else [])
    if ms:
        out.append('<section id="measurements"><h2>Measurements</h2>' + table(
            "Measured savings", ["lever", "design", "unit", "estimate", "signable"],
            [[esc(m.lever_id), esc(m.design), esc(m.unit), money(m.estimate),
              "yes" if m.signable else "no"] for m in ms], raw=True) + "</section>")
    if r.check is not None:
        c = r.check
        med = check_median(c.median_cost_nano, basis, "median cost per run")
        out.append('<section id="check"><h2>Check</h2><p>'
                   + ("passed" if c.passed else '<span class="bad">failed</span>')
                   + f" · {c.runs} runs · median {money(med) if med else 'n/a'}</p>"
                   + table("Violations", ["rule", "level", "message", "location"],
                           [[v.rule_id, v.level, v.message, v.location] for v in c.violations])
                   + "</section>")
    if r.pricing is not None:
        p = r.pricing
        out.append('<section id="pricing"><h2>Pricing</h2>' + table(
            f"Rate rows ({p.kind})", ["row", "input $/MTok", "output $/MTok", "enabled"],
            [[x.row_id, str(x.input_usd_per_mtok), str(x.output_usd_per_mtok),
              "yes" if x.enabled else "no"] for x in p.rows[:TOP_ROWS]], numeric=(1, 2))
            + "</section>")
    return "".join(out)


def _data_quality(r: RunResult) -> str:
    if not r.data_quality:
        return ""
    return ('<section id="data-quality"><h2>Data quality</h2>' + table(
        "Data-quality notes", ["code", "severity", "count", "detail"],
        [[n.code, n.severity, f"{n.count:,}", n.detail] for n in r.data_quality], numeric=(2,))
        + "</section>")


def _methodology(r: RunResult) -> str:
    rc = r.rate_card
    rows = [["rate card", rc.sha256 if rc else "none"],
            ["layers", ", ".join(rc.layers) if rc else "none"],
            ["contract", (rc.contract or "none") if rc else "none"]]
    sources = table("Sources", ["adapter", "source", "records", "quarantined"],
                    [[s.adapter, s.source_id, f"{n:,}", f"{q:,}"] for s, n, q in
                     r.inputs[:TOP_ROWS]], numeric=(2, 3))
    bench = []
    for const, what in ((_evidence.CACHE_READ_SHARE_MEDIAN, "median cache-read share"),
                        (_evidence.CACHE_READ_SHARE_TOP_DECILE, "top-decile cache-read share"),
                        (_evidence.CACHE_READ_SHARE_INVESTIGATE_BELOW,
                         "investigate below this cache-read share"),
                        (_evidence.CC_FLEET_USD_PER_ACTIVE_DAY,
                         "Claude Code cost per active developer-day (typical)"),
                        (_evidence.CC_FLEET_USD_PER_ACTIVE_DAY_P90_UNDER,
                         "90% of developers stay below (per active day)")):
        value = const.value
        shown = pct(str(value)) if isinstance(value, Decimal) and value < 1 else f"${value}"
        bench.append([what, shown, const.source_url, const.checked_on])
    assumptions = sorted({a for x in r.replays for a in x.assumptions})
    threats = [
        "Unpriced usage is excluded from the exact bill and reported as coverage (unknown is not "
        "zero).",
        "Estimates are ranges from models; their calibration status is printed with them.",
        "List-equivalent allowance and Copilot credits are not invoice dollars.",
        f"Groups below k = {r.privacy.k} users are merged or withheld (k-anonymity).",
        "Published benchmarks are shown with their source and date; they are not predictions.",
    ]
    return ('<section id="methodology"><h2>Methodology and provenance</h2>'
            + table("Rate card", ["item", "value"], rows) + sources
            + table("Published benchmarks (source and verification date)",
                    ["benchmark", "value", "source", "checked"], bench)
            + (("<h3>Assumptions</h3><ul>" + "".join(f"<li>{esc(a)}</li>" for a in assumptions)
                + "</ul>") if assumptions else "")
            + "<h3>Threats to validity</h3><ul>"
            + "".join(f"<li>{esc(t)}</li>" for t in threats) + "</ul></section>")


def _extension_html(r: RunResult) -> str:
    if not extension_slots_present(r):
        return ""
    parts = []
    for text in extensions.render_sections(r, "html"):
        body = str(text)
        if _SCRIPT_RE.search(body) or _SCHEME_RE.search(body):
            raise ContractViolation("an extension HTML section carries a script or an external URL")
        parts.append(body)
    return "".join(parts)


def render_html(result: RunResult) -> str:
    """The single-file HTML report of *result* (SPEC §14.3); see the module docstring. Raises
    ``ContractViolation`` for a non-billed-eligible figure in a billed place, a billed figure in an
    allowance place, a non-published breakdown, or an unsafe extension section."""
    if not isinstance(result, RunResult):
        raise ContractViolation("render_html expects a RunResult")
    body = [_header(result), _legend()]
    if result.bill is not None:
        body.append(_bill(result, result.bill))
        body.append(_money_goes(result.bill))
    if result.findings:
        body.append(_findings(result.findings))
    if result.action_plan is not None:
        body.append(_plan(result.action_plan))
    if result.calibration is not None:
        body.append(_calibration(result.calibration))
    if result.reconciliation is not None:
        body.append(_reconciliation(result.reconciliation))
    if result.policy_packs:
        body.append(_packs(result))
    body.append(_others(result))
    body.append(_extension_html(result))
    body.append(_data_quality(result))
    if result.notes:
        body.append('<section id="notes"><h2>Notes</h2><ul>'
                    + "".join(f"<li>{esc(n)}</li>" for n in result.notes) + "</ul></section>")
    body.append(_methodology(result))
    doc = page(f"Token Bill — {result.command}", body)
    if len(doc.encode("utf-8")) > MAX_BYTES:
        raise ContractViolation("HTML report exceeds the 5 MB budget")
    return doc
