"""Self-contained HTML report and aligned terminal summary for Token Bill.

``render_report`` turns run profiles, scenario results, and detected cache
breakers into ONE HTML string with inline CSS and hand-rolled inline SVG — no
scripts, no external stylesheets, fonts, or images — so the file can be mailed
around or committed to a repo and render identically offline, in light and
dark (``prefers-color-scheme``). ``render_text_summary`` renders the same
story as an aligned plain-text summary for the CLI.

Both renderers are pure: everything environment-dependent (the date, the
trace name, whether the data is synthetic) arrives through the ``meta``
mapping — this module never reads the clock or the filesystem.

Honesty rules (product law, see docs/SPEC.md):

- Every dollar figure and token total shown comes from the trace's real
  billed ``usage`` fields, or exact arithmetic on them. Exact.
- Percentages and simulated scenarios that rest on char-based attribution
  are approximate by construction and carry an explicit "approx" marker
  wired to a methodology footnote. An approximate number is never presented
  as billed.

Input shapes (duck-typed against the SPEC interfaces; only documented
attribute names are touched, so the renderer works with any object exposing
them): ``profiles`` is a sequence of ``analyzer.RunProfile``; ``scenarios``
maps ``run_id`` to the ``simulator.ScenarioResult`` list for that run;
``breakers`` maps ``run_id`` to the ``breakers.Breaker`` list for that run.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tokenbill import __version__

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tokenbill.analyzer import CallProfile, RunProfile
    from tokenbill.breakers import Breaker
    from tokenbill.simulator import ScenarioResult

logger = logging.getLogger("tokenbill.report")

#: Provider pricing/caching documentation quoted in the methodology footnotes.
PRICING_DOC_URL = "https://platform.claude.com/docs/en/pricing.md"

#: Evidence spans are truncated so one pathological breaker cannot balloon
#: the report; the char budgets differ because HTML gets a scrollable <pre>.
_EVIDENCE_LIMIT_HTML = 240
_EVIDENCE_LIMIT_TEXT = 120

#: Waterfall stack order (bottom to top) and display labels. The categories
#: map 1:1 onto the billed usage fields — the waterfall is exact.
_WATERFALL_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("read", "cache read"),
    ("write", "cache write"),
    ("uncached", "uncached input"),
    ("output", "output"),
)

#: pricing.cost_breakdown keys per SPEC are uncached/write/read/output; the
#: aliases absorb harmless naming drift without ever silently returning zero.
_DOLLAR_ALIASES: dict[str, tuple[str, ...]] = {
    "uncached": ("uncached", "input", "uncached_input"),
    "write": ("write", "cache_write", "cache_creation"),
    "read": ("read", "cache_read"),
    "output": ("output", "out"),
}

_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


# --- shared formatting helpers -------------------------------------------------


def _usd(value: float | None) -> str:
    """Dollars for display: cents precision, four decimals under a dime."""
    if value is None:
        return "n/a"
    if abs(value) >= 0.10 or value == 0:
        return f"${value:,.2f}"
    return f"${value:.4f}"


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def _fmt_tokens(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1e6:.1f}M"
    if value >= 10_000:
        return f"{value / 1e3:.0f}k"
    if value >= 1_000:
        return f"{value / 1e3:.1f}k"
    return f"{value:.0f}"


def _count_word(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return word if n == 1 else (plural or word + "s")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text)} chars total]"


def _one_line(text: str, limit: int) -> str:
    flattened = text.replace("\r", "").replace("\n", "\\n")
    return _truncate(flattened, limit)


def _dollar(breakdown: Mapping[str, float], category: str) -> float:
    """Pull one category from a cost_breakdown dict, tolerating alias names."""
    for key in _DOLLAR_ALIASES[category]:
        if key in breakdown:
            return float(breakdown[key])
    raise KeyError(
        f"cost breakdown has no {category!r} entry (found keys: {sorted(breakdown)})"
    )


def _call_tokens(call_profile: CallProfile) -> dict[str, int]:
    """Billed token counts by waterfall category — exact, straight from usage."""
    usage = call_profile.call.usage
    return {
        "read": usage.cache_read_input_tokens,
        "write": usage.cache_creation_input_tokens,
        "uncached": usage.input_tokens,
        "output": usage.output_tokens,
    }


def _scenario_named(results: Sequence[ScenarioResult], name: str) -> ScenarioResult | None:
    for result in results:
        if result.name == name:
            return result
    return None


# --- aggregation ---------------------------------------------------------------


@dataclass(frozen=True)
class _RunView:
    """One run's totals, computed once and shared by the HTML/text renderers."""

    run_id: str
    n_calls: int
    tokens: dict[str, int]              # summed billed tokens by category (exact)
    dollars: dict[str, float] | None    # summed billed dollars; None if any call unpriced
    unpriced_calls: int
    redundancy: float                   # totals.redundancy_fraction (approx basis)
    models: tuple[str, ...]             # first-appearance order


def _run_view(profile: RunProfile) -> _RunView:
    tokens = dict.fromkeys((name for name, _ in _WATERFALL_CATEGORIES), 0)
    dollars = dict.fromkeys(tokens, 0.0)
    unpriced = 0
    models: list[str] = []
    for call_profile in profile.calls:
        for category, count in _call_tokens(call_profile).items():
            tokens[category] += count
        if call_profile.dollars is None:
            unpriced += 1
        else:
            for category in dollars:
                dollars[category] += _dollar(call_profile.dollars, category)
        model = call_profile.call.model
        if model not in models:
            models.append(model)
    return _RunView(
        run_id=profile.run.run_id,
        n_calls=len(profile.calls),
        tokens=tokens,
        dollars=None if unpriced else dollars,
        unpriced_calls=unpriced,
        redundancy=float(profile.totals.redundancy_fraction),
        models=tuple(models),
    )


@dataclass(frozen=True)
class _Overview:
    """Aggregate numbers behind the headline sentence."""

    n_runs: int
    n_calls: int
    models: tuple[str, ...]
    redundancy: float            # billed-input-token weighted across runs (approx basis)
    n_breakers: int
    billed_usd: float | None     # sum of as-billed scenario dollars (None if any unknown)
    recovered_usd: float | None  # billed minus fixed-cache dollars (None if any unknown)


def _overview(
    profiles: Sequence[RunProfile],
    scenarios: Mapping[str, Sequence[ScenarioResult]],
    breakers: Mapping[str, Sequence[Breaker]],
    views: Sequence[_RunView],
) -> _Overview:
    models: list[str] = []
    for view in views:
        models.extend(m for m in view.models if m not in models)

    weight_sum = 0
    weighted_redundancy = 0.0
    for profile, view in zip(profiles, views, strict=True):
        weight = sum(cp.call.usage.total_input for cp in profile.calls)
        weight_sum += weight
        weighted_redundancy += view.redundancy * weight

    def _scenario_sum(name: str) -> float | None:
        """Sum one scenario's dollars across runs; None if any run lacks a price."""
        total = 0.0
        for view in views:
            result = _scenario_named(scenarios.get(view.run_id, ()), name)
            if result is None or result.dollars is None:
                return None
            total += result.dollars
        return total if views else None

    billed = _scenario_sum("as-billed")
    fixed = _scenario_sum("fixed-cache")
    recovered = None if billed is None or fixed is None else billed - fixed

    return _Overview(
        n_runs=len(views),
        n_calls=sum(view.n_calls for view in views),
        models=tuple(models),
        redundancy=weighted_redundancy / weight_sum if weight_sum else 0.0,
        n_breakers=sum(len(breakers.get(view.run_id, ())) for view in views),
        billed_usd=billed,
        recovered_usd=recovered,
    )


def _headline_parts(overview: _Overview) -> tuple[str, str]:
    """The headline sentence, split as (percentage, remainder).

    Split so the HTML renderer can attach the approx footnote marker directly
    to the percentage while the text renderer joins the two pieces verbatim.
    """
    pct = overview.redundancy * 100
    pct_text = f"{pct:.1f}%" if pct < 9.95 else f"{pct:.0f}%"
    # Token wording is deliberate: redundancy_fraction is a share of billed
    # input TOKENS. A dollar share would differ whenever a run mixes rate
    # classes (reads 0.1x, writes 1.25x, uncached 1.0x) — do not claim spend.
    base = "of billed input tokens went to re-sending bytes the model had already seen"
    n = overview.n_breakers
    if n == 0:
        return pct_text, f"{base}; no cache breakers detected."
    fixes = _count_word(n)
    if overview.recovered_usd is None:
        found = f"{fixes} {_plural(n, 'fix', 'fixes')} identified"
        return pct_text, f"{base}; {found} (dollar impact unknown: unpriced model)."
    recovered = overview.recovered_usd
    if recovered < 0.005:
        detected = f"{fixes} {_plural(n, 'breaker')} detected"
        return pct_text, f"{base}; {detected}, but the estimated recovery is under a cent."
    verb = "recovers" if n == 1 else "recover"
    subject = "the fix below" if n == 1 else f"the {fixes} fixes below"
    return (
        pct_text,
        f"{base}; {subject} {verb} an estimated "
        f"{_usd(recovered)} of {_usd(overview.billed_usd)}.",
    )


# --- SVG (hand-rolled; styled via CSS classes defined in _CSS) -----------------


def _svg_waterfall(calls: Sequence[CallProfile]) -> str:
    """Stacked per-call bars of billed tokens: read / write / uncached / output."""
    stacks = [(cp.call.index, _call_tokens(cp)) for cp in calls]
    n = max(1, len(stacks))
    slot = max(10, min(34, 860 // n))
    left, right, top, bottom = 64, 14, 34, 46
    px, py = n * slot, 190
    # The legend row must never be clipped by the viewBox on short runs: its
    # width is computed with the same advance arithmetic the loop below uses.
    legend_w = left + sum(
        14 + int(len(label) * 6.4) + 18 for _, label in _WATERFALL_CATEGORIES
    )
    w, h = max(left + px + right, legend_w), top + py + bottom
    ymax = max((sum(tokens.values()) for _, tokens in stacks), default=0) or 1

    parts = [
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        'aria-label="Per-call billed token waterfall: cache read, cache write, '
        'uncached input, and output tokens, stacked per call">'
    ]
    lx = left
    for category, label in _WATERFALL_CATEGORIES:
        parts.append(f'<rect class="wf-{category}" x="{lx}" y="7" width="10" height="10"/>')
        parts.append(f'<text x="{lx + 14}" y="16">{label}</text>')
        lx += 14 + int(len(label) * 6.4) + 18
    for i in range(5):
        value = ymax * i / 4
        y = top + (1 - value / ymax) * py
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + px}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" '
            f'text-anchor="end">{_fmt_tokens(value)}</text>'
        )
    parts.append(f'<rect class="frame" x="{left}" y="{top}" width="{px}" height="{py}"/>')
    bar_w = slot * 0.7
    label_step = max(1, -(-n // 12))  # at most ~12 x-axis labels
    for position, (index, tokens) in enumerate(stacks):
        x = left + position * slot + (slot - bar_w) / 2
        y_cursor = top + py
        for category, _ in _WATERFALL_CATEGORIES:
            height = tokens[category] / ymax * py
            if height <= 0:
                continue
            y_cursor -= height
            parts.append(
                f'<rect class="wf-{category}" x="{x:.1f}" y="{y_cursor:.1f}" '
                f'width="{bar_w:.1f}" height="{height:.1f}"/>'
            )
        if position % label_step == 0:
            cx = left + (position + 0.5) * slot
            parts.append(
                f'<text class="tick" x="{cx:.1f}" y="{h - bottom + 16}" '
                f'text-anchor="middle">{index}</text>'
            )
    parts.append(
        f'<text class="axis" x="{left + px / 2:.1f}" y="{h - 8}" '
        'text-anchor="middle">call index</text>'
    )
    parts.append(
        f'<text class="axis" x="14" y="{top + py / 2:.1f}" text-anchor="middle" '
        f'transform="rotate(-90 14 {top + py / 2:.1f})">billed tokens</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _svg_scenarios(results: Sequence[ScenarioResult]) -> str:
    """Horizontal dollar bars, one per scenario, labels at both ends."""
    label_w, bar_max, right, top, row_h = 118, 300, 96, 8, 30
    n = max(1, len(results))
    w, h = label_w + bar_max + right, top + n * row_h + 8
    max_dollars = max((r.dollars for r in results if r.dollars is not None), default=0.0)

    parts = [
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        'aria-label="Scenario cost comparison in dollars: as-billed, no-cache, '
        'optimal-cache, and fixed-cache">'
    ]
    for i, result in enumerate(results):
        y = top + i * row_h
        name = html.escape(result.name)
        parts.append(
            f'<text class="tick" x="{label_w - 8}" y="{y + 19}" text-anchor="end">{name}</text>'
        )
        if result.dollars is None or max_dollars <= 0:
            bar_len = 0.0
        else:
            bar_len = max(1.5, result.dollars / max_dollars * bar_max)
        css = "sc-fixed" if result.name == "fixed-cache" else "sc-bar"
        if bar_len > 0:
            parts.append(
                f'<rect class="{css}" x="{label_w}" y="{y + 6}" '
                f'width="{bar_len:.1f}" height="16" rx="2"/>'
            )
        parts.append(
            f'<text class="val" x="{label_w + bar_len + 8:.1f}" '
            f'y="{y + 19}">{html.escape(_usd(result.dollars))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# --- HTML assembly -------------------------------------------------------------

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fdfdfc; --fg: #20242c; --muted: #6b7280; --grid: #d8dade;
  --accent: #2563eb; --ok: #15803d; --bad: #b91c1c;
  --banner-bg: #fef3c7; --banner-fg: #92400e; --code-bg: #f3f4f6;
  --c-read: #0f766e; --c-write: #b45309; --c-uncached: #dc2626; --c-output: #64748b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151a; --fg: #e5e7eb; --muted: #9ca3af; --grid: #333a45;
    --accent: #60a5fa; --ok: #4ade80; --bad: #f87171;
    --banner-bg: #3b2f14; --banner-fg: #fde68a; --code-bg: #1d222a;
    --c-read: #2dd4bf; --c-write: #fbbf24; --c-uncached: #f87171; --c-output: #94a3b8;
  }
}
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 62rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
h1 { margin: 0 0 0.25rem; font-size: 1.6rem; }
h2 { margin: 2.5rem 0 0.5rem; font-size: 1.2rem; }
h3 { margin: 1.5rem 0 0.5rem; font-size: 1rem; }
.meta { color: var(--muted); margin: 0 0 0.5rem; }
.banner { background: var(--banner-bg); color: var(--banner-fg);
  padding: 0.75rem 1rem; border-radius: 0.5rem; margin: 1rem 0; }
.headline { font-size: 1.25rem; line-height: 1.45; margin: 1.25rem 0;
  max-width: 46rem; }
sup.approx a { color: var(--accent); text-decoration: none; font-weight: 700; }
.figures { display: flex; flex-wrap: wrap; gap: 1.5rem; align-items: flex-start; }
figure { margin: 0; max-width: 100%; overflow-x: auto; }
figcaption { color: var(--muted); font-size: 0.85rem; max-width: 30rem;
  margin-top: 0.25rem; }
svg text { fill: var(--fg); font: 11px system-ui, sans-serif; }
svg .tick, svg .axis { fill: var(--muted); }
svg .val { font-variant-numeric: tabular-nums; }
svg .frame { fill: none; stroke: var(--grid); }
svg .grid { stroke: var(--grid); stroke-dasharray: 2 3; }
svg .wf-read { fill: var(--c-read); }
svg .wf-write { fill: var(--c-write); }
svg .wf-uncached { fill: var(--c-uncached); }
svg .wf-output { fill: var(--c-output); }
svg .sc-bar { fill: var(--accent); opacity: 0.85; }
svg .sc-fixed { fill: var(--ok); opacity: 0.9; }
ul.notes { color: var(--muted); font-size: 0.85rem; padding-left: 1.25rem; }
.breakers { display: grid; gap: 0.75rem; }
.breaker { border: 1px solid var(--grid); border-radius: 0.5rem;
  padding: 0.75rem 1rem; }
.breaker-head { margin: 0 0 0.4rem; }
.breaker .kind { font-weight: 700; color: var(--bad); }
.breaker .where { color: var(--muted); }
.breaker .recover { color: var(--ok); font-weight: 600; }
.breaker .fix { margin: 0.25rem 0; }
pre.evidence { background: var(--code-bg); padding: 0.5rem 0.75rem;
  border-radius: 0.4rem; overflow-x: auto; white-space: pre-wrap;
  word-break: break-all; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
.footnotes { color: var(--muted); font-size: 0.9rem; }
.footnotes li { margin-bottom: 0.4rem; }
.footnotes a, a { color: var(--accent); }
footer { margin-top: 3rem; color: var(--muted); font-size: 0.85rem; }
"""

_APPROX_SUP = (
    '<sup class="approx"><a href="#fn-approx" '
    'title="Approximate: char-based attribution scaled to billed totals">&#8776;</a></sup>'
)


def _breaker_card(breaker: Breaker) -> str:
    if breaker.est_recovered_usd is None:
        recover = '<span class="where">recovery estimate unavailable</span>'
    else:
        recover = (
            f'<span class="recover">recovers &#8776; '
            f"{html.escape(_usd(breaker.est_recovered_usd))}</span>"
        )
    evidence = html.escape(_truncate(breaker.evidence, _EVIDENCE_LIMIT_HTML))
    return (
        '<article class="breaker">'
        f'<p class="breaker-head"><span class="kind">{html.escape(breaker.kind)}</span> '
        f'<span class="where">first at call index {breaker.first_call_index}</span> '
        f"&#183; {recover}</p>"
        f'<p class="fix">Fix: {html.escape(breaker.fix)}</p>'
        f'<pre class="evidence">{evidence}</pre>'
        "</article>"
    )


def _run_section(
    view: _RunView,
    profile: RunProfile,
    results: Sequence[ScenarioResult],
    found: Sequence[Breaker],
) -> str:
    tokens = view.tokens
    token_bits = " &#183; ".join(
        f"{label} {tokens[category]:,}" for category, label in _WATERFALL_CATEGORIES
    )
    if view.dollars is None:
        dollar_bits = (
            f"billed dollars unavailable ({view.unpriced_calls} of {view.n_calls} "
            "calls have a model outside the bundled pricing table)"
        )
    else:
        total = sum(view.dollars.values())
        dollar_bits = f"billed {html.escape(_usd(total))}"
    stats = (
        f'<p class="meta">{view.n_calls} calls &#183; {token_bits} tokens &#183; '
        f"{dollar_bits} &#183; redundant input {_pct(view.redundancy)}{_APPROX_SUP}</p>"
    )

    figures = [
        f"<figure>{_svg_waterfall(profile.calls)}"
        "<figcaption>Billed tokens per call (exact, from the trace's usage fields):"
        " cache reads and writes, uncached input, output.</figcaption></figure>"
    ]
    if results:
        figures.append(
            f"<figure>{_svg_scenarios(results)}"
            "<figcaption>Dollars under each scenario. as-billed and no-cache are exact"
            " arithmetic on billed usage; optimal-cache and fixed-cache are simulations"
            f" on the approx char basis{_APPROX_SUP}.</figcaption></figure>"
        )
    notes = [r for r in results if r.note]
    notes_html = (
        '<ul class="notes">'
        + "".join(
            f"<li><strong>{html.escape(r.name)}</strong>: {html.escape(r.note)}</li>"
            for r in notes
        )
        + "</ul>"
        if notes
        else ""
    )
    if found:
        breaker_html = (
            f"<h3>Cache breakers ({len(found)})</h3>"
            '<div class="breakers">' + "".join(_breaker_card(b) for b in found) + "</div>"
        )
    else:
        breaker_html = '<h3>Cache breakers</h3><p class="meta">None detected.</p>'
    return (
        f"<section><h2>Run {html.escape(view.run_id)}</h2>{stats}"
        f'<div class="figures">{"".join(figures)}</div>{notes_html}{breaker_html}</section>'
    )


def render_report(
    profiles: Sequence[RunProfile],
    scenarios: Mapping[str, Sequence[ScenarioResult]],
    breakers: Mapping[str, Sequence[Breaker]],
    meta: Mapping[str, object],
) -> str:
    """Render one self-contained HTML report for the profiled trace.

    ``meta`` keys (all optional): ``trace`` (str, shown in the header),
    ``date`` (str, shown in the header — pass it in; this function never
    reads the clock), ``synthetic`` (bool, shows the synthetic-data banner).
    """
    views = [_run_view(profile) for profile in profiles]
    overview = _overview(profiles, scenarios, breakers, views)
    pct_text, rest = _headline_parts(overview)

    trace = html.escape(str(meta.get("trace", "trace")))
    date = html.escape(str(meta.get("date", "")))
    models = ", ".join(html.escape(m) for m in overview.models) or "none"
    meta_bits = [
        trace,
        f"{overview.n_runs} {_plural(overview.n_runs, 'run')}",
        f"{overview.n_calls} {_plural(overview.n_calls, 'call')}",
        f"models: {models}",
    ]
    if date:
        meta_bits.append(date)
    header_meta = " &#183; ".join(meta_bits)

    banner = (
        '<div class="banner" role="note"><strong>Synthetic demo data.</strong> '
        "This report was produced from bundled deterministic scenarios with planted "
        "waste patterns. It demonstrates the analyzer; the dollars describe the "
        "simulation, not a real bill.</div>"
        if bool(meta.get("synthetic", False))
        else ""
    )
    headline = (
        f'<p class="headline">~{html.escape(pct_text)}{_APPROX_SUP} {html.escape(rest)}</p>'
    )
    sections = "".join(
        _run_section(
            view,
            profile,
            scenarios.get(view.run_id, ()),
            breakers.get(view.run_id, ()),
        )
        for view, profile in zip(views, profiles, strict=True)
    )
    if not sections:
        sections = '<p class="meta">No runs in this trace.</p>'

    footnotes = [
        '<li id="fn-approx"><strong>Exact vs approximate.</strong> Every dollar figure '
        "and token total comes from the trace's real billed usage fields (or exact "
        "arithmetic on them). Numbers marked &#8776; rest on char-based attribution "
        "(len/3.7), scaled so segments sum to each call's billed total — useful for "
        "proportions, never presented as billed.</li>",
        "<li><strong>Cache simulation rules.</strong> 300-second cache TTL, refreshed on "
        "read (sliding-window assumption, documented); cache entries are per-model; "
        "writes billed at the model's write premium only when a later call in the "
        "replay actually reads the entry (an optimal policy never caches what nothing "
        "reads back), so optimal-cache never exceeds no-cache; a prefix must meet the "
        "model's minimum cacheable length; the simulator places a single cache "
        "breakpoint at the end of messages each call (optimal placement). It models "
        "the provider's documented rules, not undocumented server behavior.</li>",
        "<li><strong>Redundancy.</strong> For each call after the first: the "
        "byte-identical rendered prefix shared with the previous call, valued at the "
        "call's billed input scaled by char fraction; input already served as cache "
        "reads is subtracted (cache reads are cheap — they are not waste).</li>",
        "<li><strong>Scenarios.</strong> as-billed = ground truth from usage; no-cache = "
        "all input at the full uncached rate; optimal-cache = replay under the "
        "documented cache rules; fixed-cache = optimal-cache after neutralizing the "
        "detected breakers.</li>",
        f"<li><strong>Pricing.</strong> Bundled table shipped with tokenbill "
        f'{__version__}, verified 2026-07 against <a href="{PRICING_DOC_URL}">the '
        "provider price list</a>. claude-sonnet-5 is billed at introductory rates "
        "($2/$10 per MTok) through 2026-08-31; this table uses the standard $3/$15 "
        "rates, so sonnet-5 dollar figures can overstate real bills during that "
        "window. Re-verify before release-grade accounting.</li>",
    ]

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token Bill &#8212; {trace}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<header>
<h1>Token Bill</h1>
<p class="meta">{header_meta}</p>
</header>
{banner}
{headline}
{sections}
<h2>Methodology</h2>
<section class="footnotes">
<ol>{"".join(footnotes)}</ol>
</section>
<footer>Generated by tokenbill {__version__}.</footer>
</main>
</body>
</html>
"""
    logger.debug("rendered %d-byte report for %d run(s)", len(doc), overview.n_runs)
    return doc


# --- terminal summary ----------------------------------------------------------


def _text_row(label: str, text: str) -> str:
    return f"  {label:<17}{text}"


def render_text_summary(
    profiles: Sequence[RunProfile],
    scenarios: Mapping[str, Sequence[ScenarioResult]],
    breakers: Mapping[str, Sequence[Breaker]],
    meta: Mapping[str, object] | None = None,
) -> str:
    """Render the aligned plain-text summary the CLI prints.

    Leads with the headline sentence, then per-run billed totals, scenario
    dollars (with proportional hash bars), and breakers with their fixes.
    Pure, like :func:`render_report` — the date and trace name come via
    ``meta`` and nothing else is read from the environment.
    """
    views = [_run_view(profile) for profile in profiles]
    overview = _overview(profiles, scenarios, breakers, views)
    pct_text, rest = _headline_parts(overview)

    lines = [f"~{pct_text} {rest}"]
    intro_bits = []
    if meta and meta.get("trace"):
        intro_bits.append(str(meta["trace"]))
    intro_bits.append(f"{overview.n_runs} {_plural(overview.n_runs, 'run')}")
    intro_bits.append(f"{overview.n_calls} {_plural(overview.n_calls, 'call')}")
    if overview.models:
        intro_bits.append("models: " + ", ".join(overview.models))
    lines.append(" | ".join(intro_bits))
    if meta and meta.get("synthetic"):
        lines.append("[synthetic demo data: bundled scenarios with planted waste]")
    lines.append("")

    for view in views:
        lines.append(f"Run {view.run_id}")
        tokens = view.tokens
        token_bits = " | ".join(
            f"{label} {tokens[category]:,}" for category, label in _WATERFALL_CATEGORIES
        )
        lines.append(_text_row("billed tokens", token_bits))
        if view.dollars is None:
            lines.append(
                _text_row(
                    "billed dollars",
                    f"unavailable ({view.unpriced_calls} of {view.n_calls} calls "
                    "have no bundled pricing)",
                )
            )
        else:
            dollars = view.dollars
            breakdown = " | ".join(
                f"{label} {_usd(dollars[category])}"
                for category, label in _WATERFALL_CATEGORIES
            )
            lines.append(
                _text_row("billed dollars", f"{_usd(sum(dollars.values()))}  ({breakdown})")
            )
        lines.append(
            _text_row(
                "redundant input",
                f"~{_pct(view.redundancy)} of billed input tokens re-sent (approx)",
            )
        )

        results = scenarios.get(view.run_id, ())
        if results:
            lines.append(_text_row("scenarios", ""))
            name_w = max(len(r.name) for r in results)
            usd_texts = [_usd(r.dollars) for r in results]
            usd_w = max(len(t) for t in usd_texts)
            max_dollars = max(
                (r.dollars for r in results if r.dollars is not None), default=0.0
            )
            for result, usd_text in zip(results, usd_texts, strict=True):
                if result.dollars is not None and max_dollars > 0:
                    bar = "#" * max(1, round(result.dollars / max_dollars * 24))
                else:
                    bar = ""
                lines.append(f"    {result.name:<{name_w}}  {usd_text:>{usd_w}}  {bar}")
            for result in results:
                if result.note:
                    lines.append(f"    note ({result.name}): {result.note}")

        found = breakers.get(view.run_id, ())
        if found:
            lines.append(_text_row("breakers", ""))
            for breaker in found:
                if breaker.est_recovered_usd is None:
                    recovery = "recovery estimate n/a"
                else:
                    recovery = f"recovers ~{_usd(breaker.est_recovered_usd)}"
                lines.append(
                    f"    {breaker.kind} | first at call index "
                    f"{breaker.first_call_index} | {recovery}"
                )
                lines.append(f"      fix: {breaker.fix}")
                lines.append(
                    f"      evidence: {_one_line(breaker.evidence, _EVIDENCE_LIMIT_TEXT)}"
                )
        else:
            lines.append(_text_row("breakers", "none detected"))
        lines.append("")

    lines.append(
        "approx (~): char-based attribution scaled to billed totals; dollar and "
        "token totals come from real billed usage."
    )
    # rstrip: header rows with empty values must not carry padding whitespace,
    # so saved output diffs cleanly against the README's quoted excerpt.
    return "\n".join(line.rstrip() for line in lines)
