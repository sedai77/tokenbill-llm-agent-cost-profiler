"""Waterfalls, redundancy, and segment attribution over recorded runs.

Honesty rules (see docs/SPEC.md):

- Dollar figures come from real billed ``usage`` via :mod:`tokenbill.pricing`.
  Exact.
- Char-based numbers (segment attribution, repeated-prefix fractions, the
  redundancy fraction) are **approximate**, used only to apportion a call's
  billed totals, and every approximate token figure is scaled so segments sum
  to the call's billed ``total_input``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tokenbill.pricing import cost_breakdown
from tokenbill.trace import Call, Run, common_prefix_chars, render_segments, rendered_text

_DOLLAR_KEYS = ("uncached", "write", "read", "output")
_TOKEN_KEYS = ("uncached", "cache_write", "cache_read", "output", "total_input")


@dataclass(frozen=True)
class SegmentShare:
    """One rendered segment's approximate share of a call's billed input.

    ``approx_tokens`` is the call's billed ``total_input`` scaled by this
    segment's char fraction — approximate by construction, but the shares of
    one call always sum to the billed total.
    """

    kind: str
    label: str
    chars: int
    char_fraction: float
    approx_tokens: float


@dataclass(frozen=True)
class CallProfile:
    """Per-call profile: exact billed dollars plus approximate attribution."""

    call: Call
    dollars: dict[str, float] | None  # pricing.cost_breakdown, None if model unknown
    segments: list[SegmentShare]  # per-segment approx attribution
    repeated_prefix_chars: int  # LCP with previous call (0 for first)
    repeated_fraction_of_input: float  # approx: repeated chars / rendered chars


@dataclass(frozen=True)
class RunTotals:
    """Run-level aggregates.

    ``tokens`` (exact, from billed usage): ``uncached``, ``cache_write``,
    ``cache_read``, ``output``, ``total_input``.

    ``dollars`` (exact, billed): ``uncached``, ``write``, ``read``,
    ``output``, ``total`` — or ``None`` when any call's model has no pricing
    entry (a partial dollar total would be misleading; tokens stay exact).

    ``redundancy_fraction`` is the headline number: the share of cumulative
    billed input tokens attributable to re-sent byte-identical prefix that
    was NOT served from cache. ``redundancy_is_approx`` is always ``True``:
    the fraction rests on char-based prefix attribution.
    """

    tokens: dict[str, int]
    dollars: dict[str, float] | None
    redundancy_fraction: float
    redundancy_is_approx: bool = True


@dataclass(frozen=True)
class RunProfile:
    """Everything the report needs about one run."""

    run: Run
    calls: list[CallProfile]
    totals: RunTotals


def _segment_parts(segment: Any) -> tuple[str, str, str]:
    """Normalize a trace Segment to ``(kind, label, text)``.

    The SPEC writes Segment as a 3-tuple; tolerate an attribute-bearing
    equivalent so a NamedTuple or dataclass from trace.py also works.
    """
    try:
        kind, label, text = segment
    except (TypeError, ValueError):
        kind, label, text = segment.kind, segment.label, segment.text
    return str(kind), str(label), str(text)


def _profile_call(call: Call, prev: Call | None) -> tuple[CallProfile, float]:
    """Build one CallProfile; also return this call's wasted-input tokens."""
    rendered_chars = len(rendered_text(call))
    total_input = call.usage.total_input

    segments: list[SegmentShare] = []
    for segment in render_segments(call):
        kind, label, text = _segment_parts(segment)
        fraction = (len(text) / rendered_chars) if rendered_chars else 0.0
        segments.append(
            SegmentShare(
                kind=kind,
                label=label,
                chars=len(text),
                char_fraction=fraction,
                approx_tokens=total_input * fraction,
            )
        )

    if prev is None:
        repeated_chars = 0
    else:
        repeated_chars = common_prefix_chars(prev, call)
    repeated_fraction = (repeated_chars / rendered_chars) if rendered_chars else 0.0

    # Redundancy contribution (see profile_run docstring): repeated prefix
    # char fraction x billed total_input, minus tokens already served from
    # cache; clamped at 0 per call.
    wasted = max(0.0, total_input * repeated_fraction - call.usage.cache_read_input_tokens)

    profile = CallProfile(
        call=call,
        dollars=cost_breakdown(call.model, call.usage),
        segments=segments,
        repeated_prefix_chars=repeated_chars,
        repeated_fraction_of_input=repeated_fraction,
    )
    return profile, wasted


def profile_run(run: Run) -> RunProfile:
    """Profile one run: per-call waterfalls plus the run redundancy fraction.

    Redundancy definition (the headline number; also in DESIGN.md): for call
    ``i > 0`` the re-sent prefix is ``common_prefix_chars(call[i-1],
    call[i])`` of the canonical rendered text. Its token value is the call's
    billed ``total_input`` scaled by the repeated char fraction; the portion
    already served as ``cache_read_input_tokens`` is subtracted (cache reads
    are cheap — they are not waste), and each call's contribution is clamped
    at 0. The fraction is the sum of those contributions divided by the sum
    of billed ``total_input`` over all calls. Approximate by construction
    (char-based attribution); labeled via ``redundancy_is_approx``.
    """
    call_profiles: list[CallProfile] = []
    wasted_total = 0.0
    prev: Call | None = None
    for call in run.calls:
        profile, wasted = _profile_call(call, prev)
        call_profiles.append(profile)
        wasted_total += wasted
        prev = call

    tokens = dict.fromkeys(_TOKEN_KEYS, 0)
    for call in run.calls:
        tokens["uncached"] += call.usage.input_tokens
        tokens["cache_write"] += call.usage.cache_creation_input_tokens
        tokens["cache_read"] += call.usage.cache_read_input_tokens
        tokens["output"] += call.usage.output_tokens
        tokens["total_input"] += call.usage.total_input

    dollars: dict[str, float] | None
    if any(p.dollars is None for p in call_profiles):
        dollars = None
    else:
        dollars = dict.fromkeys(_DOLLAR_KEYS, 0.0)
        for profile in call_profiles:
            assert profile.dollars is not None  # narrowed by the branch above
            for key in _DOLLAR_KEYS:
                dollars[key] += profile.dollars[key]
        dollars["total"] = sum(dollars[key] for key in _DOLLAR_KEYS)

    billed_input = tokens["total_input"]
    redundancy = (wasted_total / billed_input) if billed_input else 0.0

    return RunProfile(
        run=run,
        calls=call_profiles,
        totals=RunTotals(tokens=tokens, dollars=dollars, redundancy_fraction=redundancy),
    )


def profile_trace(runs: list[Run]) -> list[RunProfile]:
    """Profile every run in a trace, preserving order."""
    return [profile_run(run) for run in runs]
