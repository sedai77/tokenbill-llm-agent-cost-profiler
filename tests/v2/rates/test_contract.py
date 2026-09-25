"""Contract overlays: Token Bill contract files and Claude Code ``modelPricing`` (SPEC §6.5)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core.errors import PricingError, SourceError
from tokenbill.core.labels import Basis
from tokenbill.core.records import UsageBuckets
from tokenbill.rates import contract as contract_mod
from tokenbill.rates.contract import (
    contract_json,
    dumps_model_pricing,
    from_model_pricing,
    load_contract,
    make_overlay,
    to_model_pricing,
)
from tokenbill.rates.engine import RateCard

from .helpers import FIXTURES, builtin, ctx, nano, ts


def _model_pricing_fixture() -> dict[str, Any]:
    text = (FIXTURES / "managed_settings_model_pricing.json").read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)["modelPricing"]


def test_model_pricing_round_trip() -> None:
    x = _model_pricing_fixture()
    overlay = from_model_pricing(x, name="managed")
    assert to_model_pricing(overlay) == x
    # the stated numbers are exact decimals
    over = dict(overlay.overrides)
    assert dict(over["claude-opus-5-5"])["cache_write_5m"] == Decimal("4.25")
    assert overlay.multiplier == Decimal("0.85")


def test_model_pricing_derives_the_1h_write_unless_stated() -> None:
    overlay = from_model_pricing(_model_pricing_fixture(), name="managed")
    over = {m: dict(b) for m, b in overlay.overrides}
    # cacheWrite 4.25 covers both TTLs: 1h = 4.25 × 2 / 1.25 = 6.8, assumed
    assert over["claude-opus-5-5"]["cache_write_1h"] == Decimal("6.8")
    assert overlay.assumed_fields == ("cache_write_1h@claude-opus-5-5",)
    only_derived = from_model_pricing({"overrides": {"m": {"cacheWrite": 1}}}, name="d")
    assert only_derived.assumed_fields == ("cache_write_1h",)
    # Sonnet 5 states cacheWrite1h = 3.2 (kept, exported again)
    assert over["claude-sonnet-5"]["cache_write_1h"] == Decimal("3.2")
    assert "cacheWrite1h" in to_model_pricing(overlay)["overrides"]["claude-sonnet-5"]  # type: ignore[index]
    assert "cacheWrite1h" not in to_model_pricing(overlay)["overrides"]["claude-opus-5-5"]  # type: ignore[index]


def test_model_pricing_accepts_json_floats_and_strings() -> None:
    raw = json.loads((FIXTURES / "managed_settings_model_pricing.json").read_text())
    overlay = from_model_pricing(raw["modelPricing"], name="floats")
    assert overlay.multiplier == Decimal("0.85")
    assert dict(dict(overlay.overrides)["claude-opus-5-5"])["input"] == Decimal("3.4")
    assert from_model_pricing({"multiplier": "0.9"}, name="s").multiplier == Decimal("0.9")
    assert to_model_pricing(from_model_pricing({"multiplier": 1}, name="one")) == {"multiplier": 1}


@pytest.mark.parametrize("bad", [
    {"multiplier": True}, {"multiplier": -1}, {"multiplier": "1e3"}, {"multiplier": [1]},
    {"multiplier": 11}, {"multiplier": float("inf")}, {"discount": 0.1},
    {"overrides": {"m": {"cacheHit": 1}}}, {"overrides": {"m": 3}}, {"overrides": []},
])
def test_model_pricing_rejects_malformed_blocks(bad: dict[str, Any]) -> None:
    with pytest.raises(PricingError):
        from_model_pricing(bad, name="bad")
    with pytest.raises(PricingError):
        from_model_pricing([], name="bad")  # type: ignore[arg-type]


def test_dumps_model_pricing_writes_exact_numbers() -> None:
    overlay = from_model_pricing(_model_pricing_fixture(), name="managed")
    text = dumps_model_pricing(overlay)
    assert '"multiplier": 0.85' in text and '"input": 3.4' in text and '"output": 17' in text
    assert json.loads(text, parse_float=Decimal) == _model_pricing_fixture()
    with pytest.raises(PricingError):
        to_model_pricing("x")  # type: ignore[arg-type]


def test_load_contract_file_and_its_json_round_trip(tmp_path: Path) -> None:
    overlay = load_contract(FIXTURES / "contract_acme.json")
    assert overlay.name == "acme-2026" and overlay.multiplier == Decimal("0.85")
    assert overlay.channels == ("anthropic_api", "claude_platform_aws")
    assert len(overlay.sha256) == 64 and not overlay.derived
    out = tmp_path / "again.json"
    out.write_text(json.dumps(contract_json(overlay)), encoding="utf-8")
    assert load_contract(out) == overlay


def test_contract_file_prices_on_its_channels() -> None:
    overlay = load_contract(FIXTURES / "contract_acme.json")
    card = RateCard([builtin()], contract=overlay)
    usage = UsageBuckets(uncached_input=1_000_000, cache_read=1_000_000, output=1_000_000)
    # overrides input 3.40, output 17.00 are final; reads 0.20 × 0.85 = 0.17
    p = card.price_usage(usage, ctx(), ts_ms=ts("2026-09-23"))
    assert p.figure.nano == nano("20.57") and p.figure.basis is Basis.CONTRACT
    other = card.price_usage(usage, ctx(channel="foundry"), ts_ms=ts("2026-09-23"))
    assert other.figure.basis is Basis.LIST and other.figure.nano == nano("24.2")


def test_load_contract_imports_a_managed_settings_file() -> None:
    overlay = load_contract(FIXTURES / "managed_settings_model_pricing.json")
    assert overlay.name == "managed_settings_model_pricing"
    assert to_model_pricing(overlay) == _model_pricing_fixture()


@pytest.mark.parametrize(("doc", "match"), [
    ({"schema": "tokenbill/contract@2"}, "schema"),
    ({"multiplier": 0.85}, "decimal strings"),
    ({"overrides": {"m": "cheap"}}, "bucket objects"),
    ({"overrides": {"m": {"tokens": "1"}}}, "unknown override bucket"),
    ({"effective_from": "soon"}, "YYYY-MM-DD"),
    ({"effective_to": "2025-01-01"}, "after effective_from"),
    ({"name": ""}, "printable"),
    ({"channels": "anthropic_api"}, "must be a list"),
    ({"channels": [""]}, "non-empty"),
    ({"assumed_fields": ["everything"]}, "name buckets"),
    ({"derived": "no"}, "true or false"),
    ({"multiplier": "12"}, "above 10"),
    ({"modelPricing": 3}, "must be an object"),
])
def test_load_contract_validation(tmp_path: Path, doc: dict[str, Any], match: str) -> None:
    base = json.loads((FIXTURES / "contract_acme.json").read_text())
    if "modelPricing" in doc:
        base = doc
    else:
        base.update(doc)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(PricingError, match=match):
        load_contract(path)


def test_load_contract_io_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SourceError):
        load_contract(tmp_path / "missing.json")
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(PricingError, match="not valid JSON"):
        load_contract(path)
    monkeypatch.setattr(contract_mod, "_MAX_BYTES", 4)
    path.write_text("{}   ", encoding="utf-8")
    with pytest.raises(PricingError, match="4 MiB"):
        load_contract(path)


def test_make_overlay_override_model_ids() -> None:
    with pytest.raises(PricingError):
        make_overlay(name="x", multiplier=None, overrides={"\x00": {}},
                     effective_from="2026-01-01")
