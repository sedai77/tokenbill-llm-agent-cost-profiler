"""Realization-rate priors and monthly projections (SPEC §11.2 steps 7–8, D15).

A Shapley credit is what a lever saved in the analysis window under the replay model. What an
organization actually realizes after rollout differs (adoption, behavior change, model error), so
every projection is an ESTIMATED :class:`~tokenbill.core.labels.Figure` whose point is the credit ×
the class's p50 realization rate and whose range is credit × [p10, p90], normalized to a 30-day
month. The priors come from ``core.catalog.RR_PRIORS`` (one table for the SPEC and Copilot plans):

=================  ==========================================================
class              prior (p10 / p50 / p90)
=================  ==========================================================
rate               1.0 / 1.0 / 1.0
cache_transform    0.8 / 0.9 / 1.0
trajectory         −0.2 / 0.5 / 1.0 (the p50 is a design judgment, stated in the report)
behavioral         not projected (``project`` returns None)
=================  ==========================================================

Priors are never updated automatically (D15): when the store holds at least
:data:`MIN_RECEIPTS` receipts of a class, the observed mean realization rate is shown **beside**
the prior (:func:`observed_rr`, ``ActionPlan.observed_rr``).

Money is int nano-USD; arithmetic is exact (:class:`fractions.Fraction`) with one half-even
rounding per bound. No floats.
"""

from __future__ import annotations

import re
import types
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from tokenbill.core.catalog import LEVER_CLASSES, RR_PRIORS
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Calibration, Evidence, Figure
from tokenbill.core.types import ReceiptRow

__all__ = [
    "BEHAVIORAL",
    "DAYS_PER_MONTH",
    "MIN_RECEIPTS",
    "PRIORS",
    "TRAJECTORY_P50_NOTE",
    "crosses_zero",
    "monthly_nano",
    "observed_rr",
    "parse_observed_rr",
    "project",
    "prior_note",
    "round_half_even",
]

#: Every projection is normalized to a 30-day month (SPEC §11.2 step 7).
DAYS_PER_MONTH = 30
#: Receipts needed before the observed mean realization rate of a class is shown (SPEC §11.2).
MIN_RECEIPTS = 3
#: The lever class that is never projected.
BEHAVIORAL = "behavioral"
#: Stated in reports next to trajectory projections (SPEC §11.2 table).
TRAJECTORY_P50_NOTE = "trajectory p50 0.5 is a design judgment, not a measurement"

#: p10 / p50 / p90 realization-rate priors per projected lever class (``behavioral`` is absent:
#: it is never projected). Read from ``core.catalog.RR_PRIORS``.
PRIORS: Mapping[str, tuple[Decimal, Decimal, Decimal]] = types.MappingProxyType({
    cls: prior for cls, prior in RR_PRIORS.items() if prior is not None
})

_DECIMAL_RE = re.compile(r"-?(?:\d{1,12}(?:\.\d{1,12})?|\.\d{1,12})\Z")
_ZERO_NOTE = "range crosses zero: the lever may cost more than it saves"


def round_half_even(value: Fraction) -> int:
    """*value* rounded to the nearest int, ties to even (exact, no floats)."""
    floor = value.numerator // value.denominator
    rest = value - floor
    if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and floor % 2 == 1):
        return floor + 1
    return floor


def _check_days(window_days: object) -> int:
    if type(window_days) is not int or window_days < 1:
        raise UsageError("window_days must be a positive int")
    return window_days


def monthly_nano(nano: int, window_days: int) -> Fraction:
    """``nano × 30 / window_days`` exactly (SPEC §11.2 step 7)."""
    return Fraction(nano * DAYS_PER_MONTH, _check_days(window_days))


def _prior_text(prior: tuple[Decimal, Decimal, Decimal]) -> str:
    return "/".join(format(p.normalize(), "f") for p in prior)


def prior_note(lever_class: str) -> str:
    """The label every projection of *lever_class* carries (the prior and, for trajectory
    levers, the design-judgment statement)."""
    prior = PRIORS.get(lever_class)
    if prior is None:
        return f"{lever_class} lever: not projected"
    note = f"realization-rate prior p10/p50/p90 {_prior_text(prior)} ({lever_class})"
    if lever_class == "trajectory":
        note += f"; {TRAJECTORY_P50_NOTE}"
    return note


def crosses_zero(fig: Figure) -> bool:
    """True when *fig* has a range whose low bound is negative and high bound positive."""
    return (fig.low_nano is not None and fig.high_nano is not None
            and fig.low_nano < 0 < fig.high_nano)


def project(shapley: Figure, lever_class: str, *, window_days: int, upper_bound: bool,
            calibration: Calibration) -> Figure | None:
    """The monthly projection of a lever's Shapley credit (SPEC §11.2 step 8).

    Point ``φ × 30/window_days × RR_p50``; range ``φ × 30/window_days × [RR_p10, RR_p90]``
    (bounds swap for a negative credit); ESTIMATED on the credit's basis with *calibration* (from
    the replay) and *upper_bound* inherited. A range that crosses zero is labeled in the note.
    ``behavioral`` levers are never projected (None). An unpriced credit gives an unpriced
    projection (R2). Unknown classes and non-positive windows raise :class:`UsageError`.
    """
    if not isinstance(shapley, Figure):
        raise UsageError("project: shapley must be a Figure")
    if lever_class == BEHAVIORAL:
        return None
    prior = PRIORS.get(lever_class)
    if prior is None:
        raise UsageError(f"project: unknown lever class {lever_class!r} "
                         f"(one of {', '.join(LEVER_CLASSES)})")
    days = _check_days(window_days)
    if not isinstance(calibration, Calibration):
        try:
            calibration = Calibration(calibration)
        except ValueError:
            raise UsageError("project: calibration must be a Calibration") from None
    note = prior_note(lever_class) + f"; monthly (x{DAYS_PER_MONTH}/{days} days)"
    upper = bool(upper_bound or shapley.upper_bound)
    if shapley.nano is None:
        reason = shapley.note if shapley.note.startswith("unpriced:") else "unpriced: credit"
        return Figure(nano=None, evidence=Evidence.ESTIMATED, basis=shapley.basis,
                      calibration=calibration, upper_bound=upper,
                      provenance=shapley.provenance, note=f"{reason}; {note}")
    p10, p50, p90 = (Fraction(p) for p in prior)
    point = round_half_even(monthly_nano(shapley.nano, days) * p50)
    # the credit's own range (a projection from findings) widens the prior's: every bound × p10
    # and × p90, comonotone (a Shapley credit is a point, so this is φ × [p10, p90])
    bounds = {shapley.nano}
    if shapley.low_nano is not None and shapley.high_nano is not None:
        bounds |= {shapley.low_nano, shapley.high_nano}
    ends = [round_half_even(monthly_nano(b, days) * rr) for b in bounds for rr in (p10, p90)]
    low, high = min(*ends, point), max(*ends, point)
    fig = Figure(nano=point, evidence=Evidence.ESTIMATED, basis=shapley.basis,
                 low_nano=low, high_nano=high, calibration=calibration, upper_bound=upper,
                 provenance=shapley.provenance, note=note)
    if crosses_zero(fig):
        fig = Figure(nano=point, evidence=Evidence.ESTIMATED, basis=shapley.basis,
                     low_nano=low, high_nano=high, calibration=calibration, upper_bound=upper,
                     provenance=shapley.provenance, note=f"{note}; {_ZERO_NOTE}")
    return fig


def _rr_value(text: str) -> Fraction | None:
    if not isinstance(text, str) or not _DECIMAL_RE.match(text.strip()):
        return None
    try:
        return Fraction(Decimal(text.strip()))
    except (InvalidOperation, ValueError):  # pragma: no cover - the regex admits plain decimals
        return None


def _decimal_str(value: Fraction, places: int = 4) -> str:
    scaled = round_half_even(value * 10**places)
    sign = "-" if scaled < 0 else ""
    whole, frac = divmod(abs(scaled), 10**places)
    text = f"{whole}.{str(frac).rjust(places, '0')}".rstrip("0").rstrip(".")
    return sign + (text or "0")


def observed_rr(receipts: Iterable[ReceiptRow], *,
                min_receipts: int = MIN_RECEIPTS) -> dict[str, tuple[str, int]]:
    """Observed mean realization rate per lever class from signed receipts (D15).

    Only receipts with a parseable ``realization_rate`` (decimal string) count; a class appears
    once it has at least *min_receipts* of them. The value is ``(mean as a decimal string rounded
    half-even to 4 places, n)`` — the shape :func:`tokenbill.plan.action_plan.build_action_plan`
    accepts as ``observed_rr``. Priors are never changed by it.
    """
    if type(min_receipts) is not int or min_receipts < 1:
        raise UsageError("observed_rr: min_receipts must be a positive int")
    sums: dict[str, list[Fraction]] = {}
    for row in receipts:
        if not isinstance(row, ReceiptRow):
            raise UsageError("observed_rr: expects ReceiptRow values")
        if row.realization_rate is None:
            continue
        value = _rr_value(row.realization_rate)
        if value is None:
            continue
        sums.setdefault(row.lever_class, []).append(value)
    out: dict[str, tuple[str, int]] = {}
    for cls in sorted(sums):
        values = sums[cls]
        if len(values) >= min_receipts:
            out[cls] = (_decimal_str(sum(values, Fraction(0)) / len(values)), len(values))
    return out


def parse_observed_rr(observed: Mapping[str, tuple[str, int]] | None
                      ) -> tuple[tuple[str, str, int], ...]:
    """The ``ActionPlan.observed_rr`` tuple of *observed* (class → (mean RR, n receipts)): classes
    with fewer than :data:`MIN_RECEIPTS` receipts are left out; malformed entries raise
    :class:`UsageError`. Sorted by class."""
    if observed is None:
        return ()
    if not isinstance(observed, Mapping):
        raise UsageError("observed_rr must be a mapping of lever class to (rate, n)")
    out: list[tuple[str, str, int]] = []
    for cls in sorted(observed, key=str):
        entry = observed[cls]
        if not isinstance(cls, str) or not cls:
            raise UsageError("observed_rr: lever classes must be non-empty strings")
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise UsageError("observed_rr: values must be (rate, n) pairs")
        rate, n = entry
        if type(n) is not int or n < 0:
            raise UsageError("observed_rr: receipt counts must be non-negative ints")
        if _rr_value(rate) is None:
            raise UsageError("observed_rr: rates must be decimal strings")
        if n >= MIN_RECEIPTS:
            out.append((cls, rate.strip(), n))
    return tuple(out)
