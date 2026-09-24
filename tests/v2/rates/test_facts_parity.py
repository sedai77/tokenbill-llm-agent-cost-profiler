"""D37 facts parity: every ``core/facts.json`` rate row and modifier equals the registry's
(SPEC §6.8), including every ``facts.copilot`` row and modifier (addendum §6.2 #6; with the Copilot
rate file installed, rows are compared on their pricing fields, R-E34)."""

from __future__ import annotations

import pytest

from tokenbill.core import extensions
from tokenbill.core.facts import load as load_facts
from tokenbill.core.types import RateRow

from .helpers import builtin

_PRICING_FIELDS = ("channel", "model", "effective_from", "effective_to", "input_usd_per_mtok",
                   "output_usd_per_mtok", "cache_read_mult", "cache_write_5m_mult",
                   "cache_write_1h_mult", "cache_write_other_mult", "long_context_threshold",
                   "long_context_usd_per_mtok", "promotion", "enabled")


def _registry_rows() -> dict[str, RateRow]:
    return {r.row_id: r for r in builtin().rows}


@pytest.mark.parametrize("fact", load_facts().rate_rows, ids=lambda r: r.row_id)
def test_every_facts_rate_row_equals_the_registry(fact: RateRow) -> None:
    assert _registry_rows()[fact.row_id] == fact


@pytest.mark.parametrize("fact", load_facts().modifiers, ids=lambda m: m.modifier_id)
def test_every_facts_modifier_equals_the_registry(fact: object) -> None:
    mods = {m.modifier_id: m for m in builtin().modifiers}
    assert mods[fact.modifier_id] == fact  # type: ignore[attr-defined]


@pytest.mark.parametrize("fact", load_facts().copilot.rate_rows, ids=lambda r: r.row_id)
def test_every_copilot_facts_row_equals_the_registry(fact: RateRow) -> None:
    ours = _registry_rows().get(fact.row_id)
    if extensions.extension_rate_files():  # pragma: no cover - CP-RATES' file installed
        if ours is None:
            pytest.skip("row not shared with the Copilot rate file (R-E34)")
        assert {f: getattr(ours, f) for f in _PRICING_FIELDS} == \
            {f: getattr(fact, f) for f in _PRICING_FIELDS}
    else:
        assert ours == fact


def test_every_copilot_facts_modifier_equals_the_registry() -> None:
    mods = {m.modifier_id: m for m in builtin().modifiers}
    for fact in load_facts().copilot.modifiers:
        assert mods[fact.modifier_id] == fact
