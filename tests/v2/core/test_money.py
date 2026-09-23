"""SPEC §3.3 money: exact decimal derivation, one half-even rounding per line, int nano sums."""

from __future__ import annotations

from decimal import Decimal, Inexact

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import money as m
from tokenbill.core.money import (
    EXACT_CTX,
    cents_to_nano,
    decimal_to_nano,
    fmt_usd,
    from_cents,
    nano_to_micro,
    nano_to_usd_str,
    ratio,
    scaled_to_nano,
    token_amount,
    token_nano,
    usd,
    usd_str_to_nano,
)

FAST = settings(max_examples=200, deadline=None)


def test_acceptance_values() -> None:
    assert token_nano(1_000_000, usd("4.00"), Decimal("0.05")) == 200_000_000
    assert token_nano(1, usd("0.25")) == 250
    assert token_nano(3, usd("25"), Decimal("0.8537")) == 64_028  # 64,027.5 → even
    assert from_cents("12345.678") == Decimal("123.45678")
    nano, rem = cents_to_nano("0.00000000001")
    assert nano == 0 and rem == Decimal("1E-13")
    assert cents_to_nano("0.00000000001") == (0, Decimal("1E-13"))
    with pytest.raises(TypeError):
        usd(0.1)  # type: ignore[arg-type]
    with pytest.raises(Inexact):
        EXACT_CTX.divide(Decimal(1), Decimal(3))


def test_constants() -> None:
    assert m.NANO_PER_USD == 10**9 and m.MICRO_PER_USD == 10**6 and m.MTOK == 10**6
    assert EXACT_CTX.prec == 60
    assert m.RATIO_CTX.prec == 28


def test_usd_parsing() -> None:
    assert usd("4.00") == Decimal("4")
    assert usd(" 1.5 ") == Decimal("1.5")
    assert usd(3) == Decimal(3)
    assert usd(Decimal("0.1")) == Decimal("0.1")
    assert usd("-2.5") == Decimal("-2.5")
    for bad in (True, 1.0, None, b"1"):
        with pytest.raises(TypeError):
            usd(bad)  # type: ignore[arg-type]
    for bad in ("NaN", "Infinity", "-inf", "abc", ""):
        with pytest.raises(ValueError):
            usd(bad)
    with pytest.raises(ValueError):
        usd(Decimal("NaN"))


def test_from_cents_and_cents_to_nano() -> None:
    assert from_cents(150) == Decimal("1.50")
    assert from_cents("0") == Decimal(0)
    with pytest.raises(TypeError):
        from_cents(Decimal("1"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        from_cents(1.5)  # type: ignore[arg-type]
    assert cents_to_nano("12345.678") == (123_456_780_000, Decimal(0))
    nano, rem = cents_to_nano("0.00000005")  # 5e-10 USD: exactly half a nano → even (0)
    assert nano == 0 and rem == Decimal("5E-10")
    nano, rem = cents_to_nano("0.00000015")  # 1.5 nano → 2
    assert nano == 2 and rem == Decimal("-5E-10")
    many = "1." + "9" * 150
    nano, rem = cents_to_nano(many)  # never raises on many-decimal strings
    assert nano == 20_000_000 and abs(rem) <= Decimal("5E-10")
    big = "9" * 70
    nano, rem = cents_to_nano(big)
    assert nano == int(big) * 10**7 and rem == 0
    assert cents_to_nano(-250) == (-2_500_000_000, Decimal(0))


def test_usd_str_to_nano() -> None:
    assert usd_str_to_nano("1.2345678915") == (1_234_567_892, Decimal("-5E-10"))
    assert usd_str_to_nano("0.0000000025") == (2, Decimal("5E-10"))
    assert usd_str_to_nano("-3") == (-3_000_000_000, Decimal(0))
    with pytest.raises(ValueError):
        usd_str_to_nano("1e")


@given(st.decimals(min_value=Decimal("-1e9"), max_value=Decimal("1e9"), places=14))
@FAST
def test_remainder_invariant(value: Decimal) -> None:
    nano, rem = usd_str_to_nano(str(value))
    assert Decimal(nano) / Decimal(10**9) + rem == value
    assert abs(rem) <= Decimal("5E-10")


def test_hostile_exponents_are_bounded() -> None:
    """Source amounts are parsed from files: an extreme exponent must neither hang nor exhaust
    memory (it used to build a 10**999999999 integer)."""
    assert usd_str_to_nano("1E-999999999") == (0, Decimal("1E-999999999"))
    assert cents_to_nano("5E-100000") == (0, Decimal("5E-100002"))
    for bad in ("1E+999999999", "-1E+5000"):
        with pytest.raises(ValueError, match="out of range"):
            usd_str_to_nano(bad)
    with pytest.raises(ValueError, match="out of range"):
        cents_to_nano("1E+99999")
    with pytest.raises(ValueError, match="out of range"):
        decimal_to_nano(Decimal("1E+999999999"))
    assert decimal_to_nano(Decimal("4E-999999999")) == 0
    tiny = "0." + "0" * 5000 + "1"  # more digits than int() accepts from a string
    assert usd_str_to_nano(tiny) == (0, Decimal(tiny))
    nano, rem = usd_str_to_nano("1." + "5" * 5000)
    assert nano == 1_555_555_556 and abs(rem) <= Decimal("5E-10")


def test_decimal_to_nano() -> None:
    assert decimal_to_nano(Decimal("0.0000000005")) == 0
    assert decimal_to_nano(Decimal("0.0000000015")) == 2
    assert decimal_to_nano(Decimal("-0.0000000015")) == -2
    assert decimal_to_nano(Decimal("1E+3")) == 1_000_000_000_000
    assert decimal_to_nano(Decimal("0")) == 0
    with pytest.raises(TypeError):
        decimal_to_nano(0.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        decimal_to_nano(Decimal("Infinity"))


def test_token_amount_exactness() -> None:
    assert token_amount(1_000_000, Decimal("5")) == Decimal("5")
    assert token_amount(3, Decimal("25"), Decimal("0.8537")) == Decimal("0.0000640275")
    with pytest.raises(TypeError):
        token_amount(1.0, Decimal("1"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        token_amount(1, 1.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        token_amount(1, Decimal("1"), 0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        token_amount(True, Decimal("1"))  # type: ignore[arg-type]
    with pytest.raises(Inexact):
        token_amount(123456789012345678901234567891, Decimal("1." + "3" * 34))
    with pytest.raises(ValueError):
        token_amount(1, Decimal("NaN"))
    with pytest.raises(ValueError):
        token_amount(1, Decimal("1"), Decimal("Infinity"))
    with pytest.raises(ValueError):
        token_amount(1, Decimal("1"), Decimal("sNaN"))


def test_scaled_to_nano_and_micro() -> None:
    assert scaled_to_nano(125, 8) == 1250  # 125e-8 USD = 1.25e-6 USD
    assert scaled_to_nano(135, 11) == 1  # 1.35 nano → 1
    assert scaled_to_nano(15, 10) == 2  # 1.5 → 2 (even)
    assert scaled_to_nano(25, 10) == 2  # 2.5 → 2 (even)
    assert scaled_to_nano(7, 9) == 7
    assert scaled_to_nano(7, 6) == 7000
    assert scaled_to_nano(-15, 10) == -2
    assert nano_to_micro(1500) == 2 and nano_to_micro(2500) == 2 and nano_to_micro(2501) == 3
    assert nano_to_micro(-1500) == -2
    with pytest.raises(TypeError):
        scaled_to_nano(1.0, 8)  # type: ignore[arg-type]


@given(
    st.integers(0, 10**12), st.decimals(min_value=Decimal("0"), max_value=Decimal("100"), places=6)
)
@FAST
def test_scaled_path_agrees_with_decimal_path(tokens: int, rate: Decimal) -> None:
    s = 6
    while (rate * Decimal(10) ** (s - 6)) != (rate * Decimal(10) ** (s - 6)).to_integral_value():
        s += 1
    numerator = int(rate * Decimal(10) ** (s - 6))
    assert scaled_to_nano(tokens * numerator, s) == token_nano(tokens, rate)


def test_nano_to_usd_str() -> None:
    assert nano_to_usd_str(1_500_000) == "0.0015"
    assert nano_to_usd_str(2_000_000_000) == "2"
    assert nano_to_usd_str(0) == "0"
    assert nano_to_usd_str(-1) == "-0.000000001"
    assert nano_to_usd_str(1_234_567_890_123) == "1234.567890123"
    with pytest.raises(TypeError):
        nano_to_usd_str(1.5)  # type: ignore[arg-type]


@given(st.integers(-(10**20), 10**20))
@FAST
def test_nano_to_usd_str_round_trips(nano: int) -> None:
    text = nano_to_usd_str(nano)
    assert "e" not in text.lower()
    assert usd_str_to_nano(text) == (nano, Decimal(0))


def test_ratio() -> None:
    assert ratio(1, 3) == Decimal("0.3333333333333333333333333333")
    assert ratio(5, 0) is None
    assert ratio(0, 7) == 0
    with pytest.raises(TypeError):
        ratio(1.0, 2)  # type: ignore[arg-type]


def test_fmt_usd() -> None:
    assert fmt_usd(1_234_567_890_000) == "$1,234.57"
    assert fmt_usd(None) == "unpriced"
    assert fmt_usd(-500_000_000) == "-$0.50"
    assert fmt_usd(5_000_000) == "$0.00"  # 0.005 → half-even to 0.00
    assert fmt_usd(15_000_000) == "$0.02"
    assert fmt_usd(1_234_567_890_000, places=0) == "$1,235"
    assert fmt_usd(1, places=10) == "$0.0000000010"
    assert fmt_usd(1_500_000, places=4) == "$0.0015"
    with pytest.raises(ValueError):
        fmt_usd(1, places=-1)
    with pytest.raises(TypeError):
        fmt_usd(1.0)  # type: ignore[arg-type]
