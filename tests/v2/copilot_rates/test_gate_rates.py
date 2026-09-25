"""Gate tests with RATES (merge gate 1; ``importorskip`` until RATES is merged): its loader reads
the shipped Copilot file through ``core.extensions.extension_rate_files()`` exactly as CP-RATES'
own loader does, its RateCard passes ``assert_pricer_conforms`` with the Copilot goldens (also run
per case in ``test_goldens.py``), and ``pricing verify``'s extension verifier finds nothing on the
card's layer. PLAN RATES "Provides": ``rates.schema.load_builtin``, ``load_file``,
``rates.engine.RateCard(layers, contract=None)``."""

from __future__ import annotations

import importlib.resources

import pytest

from tokenbill.core import extensions, registry
from tokenbill.core.testing import assert_pricer_conforms
from tokenbill.core.types import DataQualityNote

from .support import layer, ratecard

pytestmark = pytest.mark.gate


def test_rates_builtin_layer_carries_the_copilot_file() -> None:
    schema = pytest.importorskip("tokenbill.rates.schema")
    builtin = schema.load_builtin()
    ours = {r.row_id: r for r in layer().rows}
    theirs = {r.row_id: r for r in builtin.rows if r.channel == "github_copilot"}
    assert theirs == ours  # the file (incl. the R-E34 closed rows), not only facts' subset
    assert set(layer().modifiers) <= set(builtin.modifiers)


def test_rates_load_file_agrees_with_the_copilot_loader() -> None:
    schema = pytest.importorskip("tokenbill.rates.schema")
    notes: list[DataQualityNote] = []
    (resource,) = extensions.extension_rate_files(notes)
    with importlib.resources.as_file(resource) as path:
        loaded = schema.load_file(path, "copilot")
    assert loaded.rows == layer().rows and loaded.modifiers == layer().modifiers
    assert notes == []


def test_ratecard_conforms_with_the_copilot_goldens() -> None:
    summary = assert_pricer_conforms(ratecard())
    assert {"C.G1", "C.G5b", "C.G9", "C.G15", "C.G16"} <= set(summary["golden_cases"])


def test_pricing_verify_extension_verifier_on_the_builtin_layer() -> None:
    schema = pytest.importorskip("tokenbill.rates.schema")
    builtin = schema.load_builtin()
    verifiers = extensions.rate_verifiers(notes=[])
    assert verifiers == ["tokenbill.copilot.rates_verify:verify"]
    for dotted in verifiers:
        assert registry.load(dotted)(builtin) == []
