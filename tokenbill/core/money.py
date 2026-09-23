"""Exact money arithmetic (SPEC §3.3, D6, R1).

Rates are decimal strings parsed with :func:`usd`; derivations run under :data:`EXACT_CTX`, which
traps ``Inexact``; each (inference, bucket) line is rounded half-even **once** to int nano-USD with
:func:`decimal_to_nano`; every further sum is an int sum. No float ever enters this module.
"""

from __future__ import annotations

from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)

__all__ = [
    "EXACT_CTX",
    "MICRO_PER_USD",
    "MTOK",
    "NANO_PER_USD",
    "RATIO_CTX",
    "cents_to_nano",
    "decimal_to_nano",
    "fmt_usd",
    "from_cents",
    "nano_to_micro",
    "nano_to_usd_str",
    "ratio",
    "scaled_to_nano",
    "token_amount",
    "token_nano",
    "usd",
    "usd_str_to_nano",
]

EXACT_CTX = Context(
    prec=60, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact]
)
RATIO_CTX = Context(
    prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow]
)
NANO_PER_USD = 10**9
MICRO_PER_USD = 10**6
MTOK = 10**6

#: Rounded amounts must stay below 10**_MAX_NANO_EXP nano-USD. Far beyond any real amount; the bound
#: keeps hostile exponents in source strings (``"1E+999999999"``) from exhausting time and memory.
_MAX_NANO_EXP = 4000
_ONE = Decimal(1)


def _check_type(value: object, allowed: tuple[type, ...], what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, allowed):
        raise TypeError(
            f"{what}: expected {', '.join(t.__name__ for t in allowed)}, got {type(value).__name__}"
        )


def _parse(value: str | int | Decimal, what: str) -> Decimal:
    _check_type(value, (str, int, Decimal), what)
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    else:
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{what}: not a decimal string") from None
    if not d.is_finite():
        raise ValueError(f"{what}: NaN and infinity are not money")
    return d


def _shift(d: Decimal, places: int) -> Decimal:
    """``d × 10**places`` by exponent arithmetic: exact for any number of digits, never rounds."""
    sign, digits, exp = d.as_tuple()
    return Decimal((sign, digits, exp + places))  # type: ignore[operator]


def _div_half_even(n: int, d: int) -> int:
    """``n / d`` rounded half-even, for ``d > 0``."""
    q, r = divmod(n, d)
    twice = 2 * r
    if twice > d or (twice == d and q % 2 == 1):
        q += 1
    return q


def _decimal_to_int_half_even(d: Decimal, shift: int) -> int:
    """``round_half_even(d × 10**shift)``, exact for any number of digits (one rounding).

    Magnitudes of 10**_MAX_NANO_EXP and above raise ``ValueError``; values below 0.1 after the
    shift are 0 without touching their (possibly huge negative) exponent.
    """
    if not d:  # zero, whatever its exponent
        return 0
    x = _shift(d, shift)
    adjusted = x.adjusted()  # exponent of the most significant digit
    if adjusted >= _MAX_NANO_EXP:
        raise ValueError("amount out of range")
    if adjusted < -1:  # |x| < 0.1
        return 0
    ctx = Context(prec=adjusted + 2, rounding=ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX,
                  traps=[InvalidOperation])
    return int(x.quantize(_ONE, context=ctx))


def usd(value: str | int | Decimal) -> Decimal:
    """A USD amount (or rate) as an exact ``Decimal``; float/bool → ``TypeError``, NaN/Inf →
    ``ValueError``."""
    return _parse(value, "usd")


def from_cents(value: str | int) -> Decimal:
    """Cents (a decimal string or int) to exact USD: ``"12345.678"`` → ``Decimal("123.45678")``."""
    _check_type(value, (str, int), "from_cents")
    return _shift(_parse(value, "from_cents"), -2)


def _to_nano_with_remainder(amount_usd: Decimal) -> tuple[int, Decimal]:
    nano = _decimal_to_int_half_even(amount_usd, 9)
    if nano == 0:  # the whole amount is remainder (exact, whatever its exponent)
        return 0, amount_usd
    scaled = _shift(amount_usd, 9)
    _, digits, exp = scaled.as_tuple()
    # Enough precision for the exact difference: every digit of both operands, aligned.
    prec = len(digits) + abs(exp) + Decimal(nano).adjusted() + 3  # type: ignore[arg-type]
    ctx = Context(prec=prec, rounding=ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX,
                  traps=[InvalidOperation, Inexact])
    remainder_scaled = ctx.subtract(scaled, Decimal(nano))
    return nano, _shift(remainder_scaled, -9)


def cents_to_nano(value: str | int) -> tuple[int, Decimal]:
    """Cents → ``(nano, remainder_usd)``: half-even to 1e-9 USD, ``remainder = exact − nano·1e-9``.

    ``|remainder| ≤ 5e-10``; RECON sums remainders into the ``cents_rounding`` residual. Never
    raises on many-decimal cents strings.
    """
    return _to_nano_with_remainder(from_cents(value))


def usd_str_to_nano(value: str) -> tuple[int, Decimal]:
    """A USD decimal string (CUR, GCP export) → ``(nano, remainder_usd)``, like
    :func:`cents_to_nano`."""
    return _to_nano_with_remainder(usd(value))


def decimal_to_nano(amount_usd: Decimal) -> int:
    """ROUND_HALF_EVEN to 1e-9 USD: the ONLY rounding of computed amounts (once per line bucket)."""
    _check_type(amount_usd, (Decimal,), "decimal_to_nano")
    if not amount_usd.is_finite():
        raise ValueError("decimal_to_nano: NaN and infinity are not money")
    return _decimal_to_int_half_even(amount_usd, 9)


def token_amount(tokens: int, usd_per_mtok: Decimal, *factors: Decimal) -> Decimal:
    """``tokens × usd_per_mtok × Π factors / 10**6`` in USD, exact under :data:`EXACT_CTX`.

    An inexact derivation raises ``decimal.Inexact`` (60 digits of precision make that a
    malformed-input signal).
    """
    _check_type(tokens, (int,), "token_amount tokens")
    _check_type(usd_per_mtok, (Decimal,), "token_amount rate")
    if not usd_per_mtok.is_finite():
        raise ValueError("token_amount: NaN and infinity are not rates")
    amount = EXACT_CTX.multiply(Decimal(tokens), usd_per_mtok)
    for factor in factors:
        _check_type(factor, (Decimal,), "token_amount factor")
        if not factor.is_finite():
            raise ValueError("token_amount: NaN and infinity are not factors")
        amount = EXACT_CTX.multiply(amount, factor)
    return EXACT_CTX.divide(amount, Decimal(MTOK))


def token_nano(tokens: int, usd_per_mtok: Decimal, *factors: Decimal) -> int:
    """``decimal_to_nano(token_amount(...))``: one priced line, rounded once."""
    return decimal_to_nano(token_amount(tokens, usd_per_mtok, *factors))


def scaled_to_nano(amount_scaled: int, scale_exp: int) -> int:
    """``amount_scaled × 10**-scale_exp`` USD → nano, half-even (the unit-rate hot path)."""
    _check_type(amount_scaled, (int,), "scaled_to_nano amount")
    _check_type(scale_exp, (int,), "scaled_to_nano scale")
    shift = 9 - scale_exp
    if shift >= 0:
        return amount_scaled * 10**shift
    return _div_half_even(amount_scaled, 10 ** (-shift))


def nano_to_usd_str(nano: int) -> str:
    """Exact decimal USD string without exponent: ``1500000`` → ``"0.0015"``, ``2_000_000_000`` →
    ``"2"``."""
    _check_type(nano, (int,), "nano_to_usd_str")
    sign = "-" if nano < 0 else ""
    whole, frac = divmod(abs(nano), NANO_PER_USD)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{str(frac).rjust(9, '0').rstrip('0')}"


def nano_to_micro(nano: int) -> int:
    """Nano-USD → micro-USD, half-even (receipts)."""
    _check_type(nano, (int,), "nano_to_micro")
    return _div_half_even(nano, NANO_PER_USD // MICRO_PER_USD)


def ratio(num: int, den: int) -> Decimal | None:
    """``num / den`` under :data:`RATIO_CTX` (28 digits, half-even); None when ``den == 0``."""
    _check_type(num, (int,), "ratio")
    _check_type(den, (int,), "ratio")
    if den == 0:
        return None
    return RATIO_CTX.divide(Decimal(num), Decimal(den))


def fmt_usd(nano: int | None, places: int = 2) -> str:
    """Display form: ``"$1,234.57"`` (half-even), ``"-$0.50"``; None → ``"unpriced"``."""
    if nano is None:
        return "unpriced"
    _check_type(nano, (int,), "fmt_usd")
    _check_type(places, (int,), "fmt_usd places")
    if places < 0:
        raise ValueError("fmt_usd: places must be >= 0")
    shift = 9 - places
    units = nano * 10 ** (-shift) if shift < 0 else _div_half_even(nano, 10**shift)
    sign = "-" if units < 0 else ""
    whole, frac = divmod(abs(units), 10**places)
    text = f"{whole:,}"
    if places:
        text += "." + str(frac).rjust(places, "0")
    return f"{sign}${text}"
