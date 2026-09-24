"""``Figure`` and the honesty rules (SPEC §3.4, §1.2).

Every money value is a :class:`Figure` carrying evidence, basis, finality and calibration. The
construction rules below are enforced in ``__post_init__`` (``ContractViolation``); ``add`` /
``sub`` / ``scale`` combine figures without ever upgrading evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tokenbill.core.errors import ContractViolation
from tokenbill.core.money import EXACT_CTX, NANO_PER_USD, nano_to_usd_str
from tokenbill.core.records import TBEnum

__all__ = [
    "STRENGTH",
    "Basis",
    "Calibration",
    "Evidence",
    "Figure",
    "Finality",
    "add",
    "combine_weakest",
    "estimated",
    "exact",
    "figure_json",
    "scale",
    "sub",
    "unpriced",
    "zero",
]


class Evidence(TBEnum):
    EXACT = "exact"          # provider-billed usage × sourced rate row; pure arithmetic
    ESTIMATED = "estimated"  # any model, inference, reconstruction or assumption
    MEASURED = "measured"    # observational causal estimate on the billed ledger, with CI
    VERIFIED = "verified"    # randomized design with every guard passing, with CI


STRENGTH = {Evidence.EXACT: 3, Evidence.VERIFIED: 2, Evidence.MEASURED: 1, Evidence.ESTIMATED: 0}


class Basis(TBEnum):
    LIST = "list"
    CONTRACT = "contract"
    INVOICE = "invoice"
    PROVIDER_ESTIMATE = "provider_estimate"
    # seat-allowance usage priced at API list; not metered in dollars
    LIST_EQUIVALENT = "list_equivalent"


class Finality(TBEnum):
    PROVISIONAL = "provisional"
    FINAL = "final"
    NA = "n/a"


class Calibration(TBEnum):
    CALIBRATED = "calibrated"
    UNCALIBRATED = "uncalibrated"
    NA = "n/a"


_BILLED_BASES = frozenset({Basis.LIST, Basis.CONTRACT, Basis.INVOICE})
_UNPRICED_PREFIX = "unpriced:"


def _coerce(fig: Figure, name: str, enum_cls: type[TBEnum]) -> None:
    v = getattr(fig, name)
    if type(v) is enum_cls:
        return
    try:
        object.__setattr__(fig, name, enum_cls(v))
    except (ValueError, TypeError):
        raise ContractViolation(f"Figure.{name}: not a {enum_cls.__name__} value") from None


def _opt_int(fig: Figure, name: str) -> None:
    v = getattr(fig, name)
    if v is not None and type(v) is not int:
        raise ContractViolation(f"Figure.{name}: must be an int or None")


@dataclass(frozen=True, slots=True)
class Figure:
    """A labeled money value in int nano-USD (``nano is None`` = unpriced)."""

    nano: int | None
    evidence: Evidence
    basis: Basis
    finality: Finality = Finality.NA
    low_nano: int | None = None
    high_nano: int | None = None
    ci_level_pct: int | None = None       # e.g. 95 for MEASURED/VERIFIED
    calibration: Calibration = Calibration.NA
    upper_bound: bool = False
    provenance: tuple[str, ...] = ()      # rate-row ids, source ids, replay ids, receipt ids
    note: str = ""

    def __post_init__(self) -> None:
        _coerce(self, "evidence", Evidence)
        _coerce(self, "basis", Basis)
        _coerce(self, "finality", Finality)
        _coerce(self, "calibration", Calibration)
        for name in ("nano", "low_nano", "high_nano", "ci_level_pct"):
            _opt_int(self, name)
        if type(self.upper_bound) is not bool:
            raise ContractViolation("Figure.upper_bound: must be a bool")
        if not isinstance(self.note, str):
            raise ContractViolation("Figure.note: must be a str")
        prov = self.provenance
        if type(prov) is not tuple:
            if not isinstance(prov, (list, tuple)):
                raise ContractViolation("Figure.provenance: must be a tuple of str")
            prov = tuple(prov)
            object.__setattr__(self, "provenance", prov)
        if any(not isinstance(p, str) for p in prov):
            raise ContractViolation("Figure.provenance: must be a tuple of str")
        if self.ci_level_pct is not None and not 0 < self.ci_level_pct < 100:
            raise ContractViolation("Figure.ci_level_pct: must be in (0, 100)")
        if self.nano is None and not self.note.startswith(_UNPRICED_PREFIX):
            raise ContractViolation("Figure: an unpriced figure's note must start with 'unpriced:'")
        low, high = self.low_nano, self.high_nano
        if (low is None) != (high is None):
            raise ContractViolation("Figure: low_nano and high_nano are both set or both None")
        if low is not None and high is not None:
            if low > high:
                raise ContractViolation("Figure: low_nano > high_nano")
            if self.nano is not None and not low <= self.nano <= high:
                raise ContractViolation("Figure: nano outside [low_nano, high_nano]")
        ev = self.evidence
        if ev is Evidence.EXACT:
            if low is not None:
                raise ContractViolation("Figure: EXACT figures carry no range")
            if self.calibration is not Calibration.NA:
                raise ContractViolation("Figure: EXACT figures have calibration n/a")
            if self.upper_bound:
                raise ContractViolation("Figure: EXACT figures are never upper bounds")
        elif ev is Evidence.ESTIMATED:
            if self.calibration is Calibration.NA and not self.note:
                raise ContractViolation(
                    "Figure: ESTIMATED figures declare calibration or name their assumption "
                    "in note")
        elif low is None or self.ci_level_pct is None:
            raise ContractViolation(
                "Figure: MEASURED/VERIFIED figures need a range and ci_level_pct")
        if self.basis is Basis.INVOICE and ev is not Evidence.EXACT:
            raise ContractViolation("Figure: basis invoice requires evidence exact")

    @property
    def usd(self) -> Decimal | None:
        """The point value in USD (exact), or None when unpriced."""
        if self.nano is None:
            return None
        return EXACT_CTX.divide(Decimal(self.nano), Decimal(NANO_PER_USD))

    @property
    def is_billed_eligible(self) -> bool:
        """EXACT on a billed basis (list, contract, invoice); never
        list_equivalent/provider_estimate."""
        return self.evidence is Evidence.EXACT and self.basis in _BILLED_BASES


def _weaker(a: Evidence, b: Evidence) -> Evidence:
    return a if STRENGTH[a] <= STRENGTH[b] else b


def _combine_notes(a: str, b: str) -> str:
    if not b or a == b:
        return a
    if not a:
        return b
    parts = [p for p in a.split("; ") if p]
    for p in b.split("; "):
        if p and p not in parts:
            parts.append(p)
    return "; ".join(parts)


def _combine_calibration(a: Calibration, b: Calibration) -> Calibration:
    if Calibration.UNCALIBRATED in (a, b):
        return Calibration.UNCALIBRATED
    if Calibration.CALIBRATED in (a, b):
        return Calibration.CALIBRATED
    return Calibration.NA


def _combine_finality(a: Finality, b: Finality) -> Finality:
    if a is b:
        return a
    if Finality.PROVISIONAL in (a, b):
        return Finality.PROVISIONAL
    return Finality.NA


def _combine_ci(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _bounds(f: Figure) -> tuple[int, int]:
    assert f.nano is not None
    if f.low_nano is None or f.high_nano is None:
        return f.nano, f.nano
    return f.low_nano, f.high_nano


def _combine(a: Figure, b: Figure, *, subtract: bool) -> Figure:
    if not isinstance(a, Figure) or not isinstance(b, Figure):
        raise ContractViolation("add/sub: operands must be Figures")
    if a.basis is not b.basis:
        raise ContractViolation("add/sub: basis mismatch")
    evidence = _weaker(a.evidence, b.evidence)
    calibration = _combine_calibration(a.calibration, b.calibration)
    finality = _combine_finality(a.finality, b.finality)
    provenance = tuple(sorted(set(a.provenance) | set(b.provenance)))
    upper = a.upper_bound or b.upper_bound
    ci = _combine_ci(a.ci_level_pct, b.ci_level_pct)
    if evidence not in (Evidence.MEASURED, Evidence.VERIFIED):
        ci = None
    if evidence is Evidence.EXACT:
        calibration, upper = Calibration.NA, False
    if a.nano is None or b.nano is None:
        unpriced_note = a.note if a.nano is None else b.note
        if evidence is Evidence.MEASURED or evidence is Evidence.VERIFIED:
            evidence = Evidence.ESTIMATED  # an unpriced total cannot carry a confidence interval
            ci = None
        return Figure(nano=None, evidence=evidence, basis=a.basis, finality=finality,
                      calibration=calibration, upper_bound=upper, provenance=provenance,
                      note=unpriced_note)
    note = _combine_notes(a.note, b.note)
    has_range = a.low_nano is not None or b.low_nano is not None
    a_lo, a_hi = _bounds(a)
    b_lo, b_hi = _bounds(b)
    if subtract:
        nano = a.nano - b.nano
        low, high = a_lo - b_hi, a_hi - b_lo
    else:
        nano = a.nano + b.nano
        low, high = a_lo + b_lo, a_hi + b_hi
    if not has_range:
        low = high = None  # type: ignore[assignment]
    return Figure(nano=nano, evidence=evidence, basis=a.basis, finality=finality, low_nano=low,
                  high_nano=high, ci_level_pct=ci, calibration=calibration, upper_bound=upper,
                  provenance=provenance, note=note)


def add(a: Figure, b: Figure) -> Figure:
    """``a + b``: same basis (else ContractViolation), weaker evidence, ranges add (a point is its
    own range), None if either is unpriced, UNCALIBRATED if either is, provenance union."""
    return _combine(a, b, subtract=False)


def sub(a: Figure, b: Figure) -> Figure:
    """``a − b`` (savings): like :func:`add`, ranges subtract crosswise (low = a.low − b.high)."""
    return _combine(a, b, subtract=True)


def _scale_int(value: int, num: int, den: int) -> int:
    q, r = divmod(value * num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 == 1):
        q += 1
    return q


def scale(a: Figure, num: int, den: int) -> Figure:
    """``a × num / den`` exactly, rounded half-even per bound (``den > 0``)."""
    if type(num) is not int or type(den) is not int:
        raise ContractViolation("scale: num and den must be ints")
    if den <= 0:
        raise ContractViolation("scale: den must be > 0")
    if a.nano is None:
        return a
    nano = _scale_int(a.nano, num, den)
    low = high = None
    if a.low_nano is not None and a.high_nano is not None:
        x, y = _scale_int(a.low_nano, num, den), _scale_int(a.high_nano, num, den)
        low, high = min(x, y), max(x, y)
    return Figure(nano=nano, evidence=a.evidence, basis=a.basis, finality=a.finality, low_nano=low,
                  high_nano=high, ci_level_pct=a.ci_level_pct, calibration=a.calibration,
                  upper_bound=a.upper_bound, provenance=a.provenance, note=a.note)


def exact(nano: int, basis: Basis, *, provenance: Iterable[str] = (),
          finality: Finality = Finality.NA) -> Figure:
    """An EXACT figure (billed tokens × sourced rate)."""
    return Figure(nano=nano, evidence=Evidence.EXACT, basis=basis, finality=finality,
                  provenance=tuple(provenance))


def estimated(nano: int | None, basis: Basis, *, low: int | None = None, high: int | None = None,
              calibration: Calibration = Calibration.UNCALIBRATED, upper_bound: bool = False,
              note: str = "", provenance: Iterable[str] = ()) -> Figure:
    """An ESTIMATED figure; declares calibration (default UNCALIBRATED) and optionally a range."""
    return Figure(nano=nano, evidence=Evidence.ESTIMATED, basis=basis, low_nano=low, high_nano=high,
                  calibration=calibration, upper_bound=upper_bound, provenance=tuple(provenance),
                  note=note)


def unpriced(reason: str, basis: Basis = Basis.LIST) -> Figure:
    """An unpriced figure (``nano None``); the note is ``"unpriced: <reason>"`` (R2: unknown is not
    zero)."""
    note = reason if reason.startswith(_UNPRICED_PREFIX) else f"{_UNPRICED_PREFIX} {reason}"
    return Figure(nano=None, evidence=Evidence.EXACT, basis=basis, note=note)


def zero(basis: Basis) -> Figure:
    """EXACT 0 on *basis*."""
    return exact(0, basis)


# ---------------------------------------------------------------------------------------------
# canonical MONEY encoding and bills across bases (GitHub Copilot, C-20)
# ---------------------------------------------------------------------------------------------


def figure_json(fig: Figure) -> dict[str, object]:
    """The canonical ``MONEY`` object of SPEC §14.1 for *fig*: ``usd`` (exact decimal string, None
    when unpriced), ``nano``, ``evidence``, ``basis``, ``finality``, ``range`` (low/high as usd and
    nano, or None), ``ci_level_pct``, ``calibration``, ``upper_bound``, ``provenance``, ``note``.
    No float anywhere; every renderer of money delegates here."""
    if not isinstance(fig, Figure):
        raise ContractViolation("figure_json: expects a Figure")
    rng: dict[str, object] | None = None
    if fig.low_nano is not None and fig.high_nano is not None:
        rng = {
            "low_usd": nano_to_usd_str(fig.low_nano),
            "low_nano": fig.low_nano,
            "high_usd": nano_to_usd_str(fig.high_nano),
            "high_nano": fig.high_nano,
        }
    return {
        "usd": nano_to_usd_str(fig.nano) if fig.nano is not None else None,
        "nano": fig.nano,
        "evidence": fig.evidence.value,
        "basis": fig.basis.value,
        "finality": fig.finality.value,
        "range": rng,
        "ci_level_pct": fig.ci_level_pct,
        "calibration": fig.calibration.value,
        "upper_bound": fig.upper_bound,
        "provenance": list(fig.provenance),
        "note": fig.note,
    }


def _combined_finality(figs: Sequence[Figure]) -> Finality:
    values = {f.finality for f in figs}
    if Finality.PROVISIONAL in values:
        return Finality.PROVISIONAL
    if values == {Finality.FINAL}:
        return Finality.FINAL
    return Finality.NA


def combine_weakest(figs: Sequence[Figure], *, note: str) -> Figure:
    """Sum figures on different dollar bases (a Copilot invoice total, addendum R16).

    INVOICE iff every input is INVOICE; otherwise basis LIST, EXACT iff every input is EXACT
    without a range, else ESTIMATED with the summed range (a point is its own range). The note is
    *note* plus the non-invoice inputs by position (``input <i> (<basis>, <evidence>)``). Pool
    credits (LIST_EQUIVALENT) are never added to dollars and provider estimates are never billed
    numbers (R12): either basis raises ``ContractViolation``, as does an empty input. An unpriced
    input makes the total unpriced.
    """
    figs = tuple(figs)
    if not figs:
        raise ContractViolation("combine_weakest: no figures")
    if not isinstance(note, str):
        raise ContractViolation("combine_weakest: note must be a str")
    for f in figs:
        if not isinstance(f, Figure):
            raise ContractViolation("combine_weakest: inputs must be Figures")
        if f.basis is Basis.LIST_EQUIVALENT:
            raise ContractViolation("combine_weakest: list-equivalent credits are never dollars")
        if f.basis is Basis.PROVIDER_ESTIMATE:
            raise ContractViolation("combine_weakest: provider estimates are never billed")
    basis = Basis.INVOICE if all(f.basis is Basis.INVOICE for f in figs) else Basis.LIST
    non_invoice = [
        f"input {i} ({f.basis.value}, {f.evidence.value})"
        for i, f in enumerate(figs)
        if f.basis is not Basis.INVOICE
    ]
    full_note = note if not non_invoice else _combine_notes(
        note, "non-invoice: " + ", ".join(non_invoice))
    provenance = tuple(sorted({p for f in figs for p in f.provenance}))
    finality = _combined_finality(figs)
    all_exact = all(f.evidence is Evidence.EXACT and f.low_nano is None for f in figs)
    upper = any(f.upper_bound for f in figs)
    calibration = Calibration.NA
    for f in figs:
        calibration = _combine_calibration(calibration, f.calibration)
    if any(f.nano is None for f in figs):
        reason = full_note or "component unpriced"
        unpriced_note = reason if reason.startswith(_UNPRICED_PREFIX) else (
            f"{_UNPRICED_PREFIX} {reason}")
        if all_exact:
            return Figure(nano=None, evidence=Evidence.EXACT, basis=basis, finality=finality,
                          provenance=provenance, note=unpriced_note)
        return Figure(nano=None, evidence=Evidence.ESTIMATED, basis=Basis.LIST, finality=finality,
                      calibration=calibration, upper_bound=upper, provenance=provenance,
                      note=unpriced_note)
    total = sum(f.nano for f in figs)  # type: ignore[misc]
    if all_exact:
        return Figure(nano=total, evidence=Evidence.EXACT, basis=basis, finality=finality,
                      provenance=provenance, note=full_note)
    low = sum(_bounds(f)[0] for f in figs)
    high = sum(_bounds(f)[1] for f in figs)
    return Figure(nano=total, evidence=Evidence.ESTIMATED, basis=Basis.LIST, finality=finality,
                  low_nano=low, high_nano=high, calibration=calibration, upper_bound=upper,
                  provenance=provenance, note=full_note or "combined estimate")
