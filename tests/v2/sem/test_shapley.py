"""SPEC §3.16 / Appendix A.7: exact and Monte Carlo Shapley, largest-remainder rounding, scaling."""

from __future__ import annotations

from fractions import Fraction
from itertools import permutations as all_orders

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.errors import UsageError
from tokenbill.core.shapley import (
    MAX_EXACT_PLAYERS,
    largest_remainder,
    scale_credits,
    shapley_exact,
    shapley_mc,
)

A7 = {frozenset(): 0, frozenset("A"): 10, frozenset("B"): 20, frozenset("C"): 5,
      frozenset("AB"): 26, frozenset("AC"): 15, frozenset("BC"): 24, frozenset("ABC"): 30}


def test_appendix_a7() -> None:
    credits = shapley_exact(["A", "B", "C"], A7.__getitem__)
    assert credits == {"A": 8, "B": 18, "C": 4}          # exact 8 / 17.5 / 4.5
    assert sum(credits.values()) == 30


def test_player_order_does_not_matter() -> None:
    assert shapley_exact(["C", "A", "B"], A7.__getitem__) == {"A": 8, "B": 18, "C": 4}


def test_each_coalition_is_evaluated_once() -> None:
    calls: list[frozenset[str]] = []

    def value(s: frozenset[str]) -> int:
        calls.append(s)
        return A7[s]

    shapley_exact(["A", "B", "C"], value)
    assert len(calls) == len(set(calls)) == 8


def test_edge_cases() -> None:
    assert shapley_exact([], A7.__getitem__) == {}
    assert shapley_exact(["A"], lambda s: 7 * len(s)) == {"A": 7}
    with pytest.raises(UsageError):
        shapley_exact(["A", "A"], A7.__getitem__)
    with pytest.raises(UsageError):
        shapley_exact([1], A7.__getitem__)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        shapley_exact(["A"], lambda s: 1.5)  # type: ignore[arg-type,return-value]


def test_more_than_ten_players_raises() -> None:
    players = [f"p{i:02d}" for i in range(MAX_EXACT_PLAYERS + 1)]
    with pytest.raises(UsageError):
        shapley_exact(players, len)
    assert sum(shapley_exact(players[:10], len).values()) == 10


def test_nonzero_empty_coalition_keeps_efficiency() -> None:
    credits = shapley_exact(["A", "B"], lambda s: 100 + 10 * len(s))
    assert credits == {"A": 10, "B": 10}      # Σφ = v(N) − v(∅)


def test_negative_values_round_by_largest_remainder() -> None:
    v = {frozenset(): 0, frozenset("A"): -5, frozenset("B"): 3, frozenset("AB"): -1}
    credits = shapley_exact(["A", "B"], v.__getitem__)
    assert sum(credits.values()) == -1
    assert credits == {"A": -4, "B": 3}       # exact −4.5 / 3.5 → tie to A (id order)


def brute_force(players: list[str], value) -> dict[str, Fraction]:
    orders = list(all_orders(players))
    out = dict.fromkeys(players, Fraction(0))
    for order in orders:
        seen: frozenset[str] = frozenset()
        for p in order:
            out[p] += value(seen | {p}) - value(seen)
            seen = seen | {p}
    return {p: v / len(orders) for p, v in out.items()}


@settings(max_examples=60, deadline=None)
@given(st.integers(1, 5), st.dictionaries(st.integers(0, 31), st.integers(-10**6, 10**6)))
def test_matches_brute_force_within_one_nano(k: int, table: dict[int, int]) -> None:
    players = [chr(ord("A") + i) for i in range(k)]

    def value(s: frozenset[str]) -> int:
        mask = sum(1 << players.index(p) for p in s)
        return table.get(mask, 0)

    credits = shapley_exact(players, value)
    exact = brute_force(players, value)
    assert sum(credits.values()) == value(frozenset(players)) - value(frozenset())
    for p in players:
        assert abs(credits[p] - exact[p]) < 1


# ---------------------------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------------------------

FIVE = ["a", "b", "c", "d", "e"]
WEIGHTS = {"a": 1_000_000, "b": 2_500_000, "c": 400_000, "d": 3_000_000, "e": 700_000}


def interacting(s: frozenset[str]) -> int:
    """Additive part plus overlapping pairs (substitutes) and one complementarity."""
    v = sum(WEIGHTS[p] for p in s)
    if {"a", "b"} <= s:
        v -= 900_000
    if {"c", "d"} <= s:
        v -= 350_000
    if {"b", "e"} <= s:
        v += 1_200_000
    if {"a", "d", "e"} <= s:
        v -= 2_000_000
    return v


def test_mc_within_three_standard_errors_of_exact() -> None:
    exact = shapley_exact(FIVE, interacting)
    values, errors = shapley_mc(FIVE, interacting, permutations=200, seed=7)
    assert sum(values.values()) == interacting(frozenset(FIVE))
    for p in FIVE:
        assert errors[p] > 0
        assert abs(values[p] - exact[p]) <= 3 * errors[p] + 1


def test_mc_is_deterministic_per_seed_and_order_independent() -> None:
    a = shapley_mc(FIVE, interacting, permutations=50, seed=3)
    b = shapley_mc(list(reversed(FIVE)), interacting, permutations=50, seed=3)
    c = shapley_mc(FIVE, interacting, permutations=50, seed=4)
    assert a == b
    assert a != c


def test_mc_additive_game_has_zero_error() -> None:
    values, errors = shapley_mc(FIVE, lambda s: sum(WEIGHTS[p] for p in s), permutations=20)
    assert values == WEIGHTS and set(errors.values()) == {0}


def test_mc_edge_cases() -> None:
    assert shapley_mc([], len) == ({}, {})
    values, errors = shapley_mc(FIVE, interacting, permutations=1)
    assert sum(values.values()) == interacting(frozenset(FIVE)) and set(errors.values()) == {0}
    with pytest.raises(UsageError):
        shapley_mc(FIVE, interacting, permutations=0)
    with pytest.raises(UsageError):
        shapley_mc(["a", "a"], interacting)
    many = [f"lever{i:02d}" for i in range(14)]           # MC has no player limit
    values, _ = shapley_mc(many, len, permutations=10)
    assert values == dict.fromkeys(many, 1)


# ---------------------------------------------------------------------------------------------
# scaling and rounding helpers
# ---------------------------------------------------------------------------------------------


def test_scale_credits_preserves_the_total_exactly() -> None:
    credits = {"ttl": 8, "fast": 18, "geo": 4}
    scaled = scale_credits(credits, 1_000_001)
    assert sum(scaled.values()) == 1_000_001
    # exact 600,000.6 / 133,333.47 / 266,666.93: the two largest remainders get +1
    assert scaled == {"fast": 600_001, "geo": 133_333, "ttl": 266_667}
    assert scale_credits(credits, 30) == credits
    assert scale_credits(credits, 0) == {"ttl": 0, "fast": 0, "geo": 0}


def test_scale_credits_edge_cases() -> None:
    assert scale_credits({}, 0) == {}
    with pytest.raises(UsageError):
        scale_credits({}, 5)
    assert scale_credits({"a": 5, "b": -5}, 11) == {"a": 6, "b": 5}   # Σ 0: equal split
    assert sum(scale_credits({"a": -3, "b": 7}, -100).values()) == -100
    with pytest.raises(UsageError):
        scale_credits({"a": 1}, 1.5)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        scale_credits({"a": 1.5}, 1)  # type: ignore[dict-item]


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(st.text("abcdefgh", min_size=1, max_size=3),
                       st.integers(-10**12, 10**12), max_size=8),
       st.integers(-10**13, 10**13))
def test_scale_credits_property(credits: dict[str, int], target: int) -> None:
    if not credits:
        return
    scaled = scale_credits(credits, target)
    assert set(scaled) == set(credits)
    assert sum(scaled.values()) == target
    total = sum(credits.values())
    if total:
        for k, v in credits.items():
            assert abs(scaled[k] - Fraction(v * target, total)) < 1


def test_largest_remainder_rejects_unreachable_targets() -> None:
    assert largest_remainder({"a": Fraction(1, 2), "b": Fraction(1, 2)}, 1) == {"a": 1, "b": 0}
    with pytest.raises(UsageError):
        largest_remainder({"a": Fraction(1, 2)}, 5)
