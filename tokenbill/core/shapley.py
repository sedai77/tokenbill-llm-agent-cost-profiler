"""Shapley credit over joint replays (SPEC §3.16, §11.2, D16; F-SEM).

Values are int nano-USD savings of lever coalitions. Exact Shapley uses :class:`fractions.Fraction`
weights; Monte Carlo uses seeded permutations (``common.rng``). Every result is converted to int
nano with the **largest-remainder rule** so the credits sum exactly to the grand-coalition value
(efficiency), ties broken by player id order. No floats: standard errors use ``Decimal.sqrt`` and
are rounded half-even to int nano.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction

from tokenbill.common import rng
from tokenbill.core.errors import UsageError

__all__ = ["MAX_EXACT_PLAYERS", "largest_remainder", "scale_credits", "shapley_exact",
           "shapley_mc"]

#: ``shapley_exact`` refuses more players (2^k joint replays); PLAN switches to MC above 6.
MAX_EXACT_PLAYERS = 10

_SQRT_CTX = Context(prec=50, rounding=ROUND_HALF_EVEN)

ValueFn = Callable[[frozenset[str]], int]


def _check_players(players: Sequence[str]) -> list[str]:
    ordered = list(players)
    if any(not isinstance(p, str) for p in ordered):
        raise UsageError("shapley: players must be strings")
    if len(set(ordered)) != len(ordered):
        raise UsageError("shapley: duplicate player ids")
    return sorted(ordered)


def _value(value: ValueFn, memo: dict[frozenset[str], int], coalition: frozenset[str]) -> int:
    got = memo.get(coalition)
    if got is None:
        got = value(coalition)
        if type(got) is not int:
            raise UsageError("shapley: the value function must return int nano")
        memo[coalition] = got
    return got


def largest_remainder(shares: Mapping[str, Fraction], target: int) -> dict[str, int]:
    """Round exact *shares* to ints summing to *target*: floor every share, then give +1 to the
    players with the largest fractional parts (ties by player id order). Requires
    ``Σ floor(shares) ≤ target ≤ Σ floor(shares) + len(shares)``."""
    keys = sorted(shares)
    floors = {k: math.floor(shares[k]) for k in keys}
    deficit = target - sum(floors.values())
    if not 0 <= deficit <= len(keys):
        raise UsageError("largest_remainder: target is not reachable by rounding")
    order = sorted(keys, key=lambda k: (-(shares[k] - floors[k]), k))
    for k in order[:deficit]:
        floors[k] += 1
    return {k: floors[k] for k in keys}


def shapley_exact(players: Sequence[str], value: ValueFn) -> dict[str, int]:
    """Exact Shapley values of *players* under *value* (int nano per coalition).

    Weights ``|S|!(k−|S|−1)!/k!`` as Fractions; the result is converted with the largest-remainder
    rule so ``Σφ == value(all) − value(∅)`` exactly (``== value(all)`` for savings games, where the
    empty coalition saves nothing). Ties go to the player id first in sorted order. ``k > 10``
    raises :class:`UsageError`. Example (§3.16): exact 8 / 17.5 / 4.5 → 8 / 18 / 4.
    """
    ordered = _check_players(players)
    k = len(ordered)
    if k > MAX_EXACT_PLAYERS:
        raise UsageError(f"shapley_exact supports at most {MAX_EXACT_PLAYERS} players")
    if k == 0:
        return {}
    memo: dict[frozenset[str], int] = {}
    k_fact = math.factorial(k)
    weights = [Fraction(math.factorial(s) * math.factorial(k - s - 1), k_fact) for s in range(k)]
    phi: dict[str, Fraction] = {}
    for idx, player in enumerate(ordered):
        others = ordered[:idx] + ordered[idx + 1:]
        total = Fraction(0)
        for mask in range(1 << (k - 1)):
            members = frozenset(others[j] for j in range(k - 1) if mask >> j & 1)
            with_p = _value(value, memo, members | {player})
            without = _value(value, memo, members)
            total += weights[len(members)] * (with_p - without)
        phi[player] = total
    target = _value(value, memo, frozenset(ordered)) - _value(value, memo, frozenset())
    return largest_remainder(phi, target)


def _isqrt_fraction(x: Fraction) -> int:
    """``round_half_even(sqrt(x))`` for a non-negative Fraction, without floats."""
    if x <= 0:
        return 0
    root = _SQRT_CTX.divide(Decimal(x.numerator), Decimal(x.denominator)).sqrt(_SQRT_CTX)
    return int(root.to_integral_value(rounding=ROUND_HALF_EVEN))


def shapley_mc(players: Sequence[str], value: ValueFn, *, permutations: int = 200,
               seed: int = 0) -> tuple[dict[str, int], dict[str, int]]:
    """Monte Carlo Shapley values from *permutations* seeded random orderings.

    Returns ``(values, standard errors)`` in int nano. Each permutation's marginal contributions
    telescope to ``value(all) − value(∅)``, so the mean credits already sum to it exactly; they
    are rounded with the largest-remainder rule (ties by player id). The standard error of a
    player is ``s / √n`` (sample standard deviation of its marginal contributions; 0 with one
    permutation). Deterministic for a given seed and player set (order-independent).
    """
    ordered = _check_players(players)
    if type(permutations) is not int or permutations < 1:
        raise UsageError("shapley_mc: permutations must be a positive int")
    if not ordered:
        return {}, {}
    memo: dict[frozenset[str], int] = {}
    empty = _value(value, memo, frozenset())
    sums = dict.fromkeys(ordered, 0)
    squares = dict.fromkeys(ordered, 0)
    generator = rng(seed, "core.shapley.shapley_mc", *ordered)
    for _ in range(permutations):
        order = list(ordered)
        generator.shuffle(order)
        coalition: frozenset[str] = frozenset()
        before = empty
        for player in order:
            coalition = coalition | {player}
            after = _value(value, memo, coalition)
            marginal = after - before
            sums[player] += marginal
            squares[player] += marginal * marginal
            before = after
    n = permutations
    means = {p: Fraction(sums[p], n) for p in ordered}
    target = _value(value, memo, frozenset(ordered)) - empty
    values = largest_remainder(means, target)
    errors: dict[str, int] = {}
    for p in ordered:
        if n < 2:
            errors[p] = 0
            continue
        variance = (Fraction(squares[p]) - Fraction(sums[p] * sums[p], n)) / (n - 1)
        errors[p] = _isqrt_fraction(variance / n)
    return values, errors


def scale_credits(credits: Mapping[str, int], target_total: int) -> dict[str, int]:
    """Rescale *credits* proportionally so they sum to *target_total* exactly (largest-remainder
    rule, ties by id): sample Shapley credits → the full-scope joint saving (§11.2 step 6).

    When the credits sum to 0 the proportions are undefined and the target is split equally. An
    empty mapping only scales to 0 (else :class:`UsageError`).
    """
    if type(target_total) is not int:
        raise UsageError("scale_credits: target_total must be an int")
    keys = sorted(credits)
    if any(type(credits[k]) is not int for k in keys):
        raise UsageError("scale_credits: credits must be ints")
    if not keys:
        if target_total != 0:
            raise UsageError("scale_credits: no credits to scale to a non-zero total")
        return {}
    total = sum(credits[k] for k in keys)
    if total == 0:
        shares = {k: Fraction(target_total, len(keys)) for k in keys}
    else:
        shares = {k: Fraction(credits[k] * target_total, total) for k in keys}
    return largest_remainder(shares, target_total)
