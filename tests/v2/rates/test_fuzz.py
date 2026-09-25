"""Hypothesis fuzz of every parser RATES owns (SPEC §21 #5): only ``TokenbillError`` subclasses may
escape — rate documents, ``--model-price`` values, billing rules, contracts and ``modelPricing``
blocks, the pricing page — and ``crosscheck_feed`` never raises at all."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.rates.billing_rules import parse_rules
from tokenbill.rates.contract import from_model_pricing, load_contract
from tokenbill.rates.schema import parse_layer, parse_model_price
from tokenbill.rates.verify import crosscheck_feed, parse_pricing_page, verify_live

from .helpers import FIXTURES, builtin, doc, row

FUZZ = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])

json_scalars = st.one_of(st.none(), st.booleans(), st.integers(-10**20, 10**20),
                         st.text(max_size=12), st.sampled_from(["2", "0.5", "2026-01-01", "*"]))
json_values = st.recursive(json_scalars, lambda inner: st.one_of(
    st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=12), inner, max_size=4)),
    max_leaves=20)
ROW_KEYS = list(row().keys())


def _survives(fn: Any, *args: Any, **kw: Any) -> None:
    try:
        fn(*args, **kw)
    except TokenbillError:
        pass


@FUZZ
@given(st.one_of(st.binary(max_size=200), st.text(max_size=200)))
def test_parse_layer_raw_bytes(data: Any) -> None:
    _survives(parse_layer, data, "user:fuzz")


@FUZZ
@given(key=st.sampled_from(ROW_KEYS), value=json_values)
def test_parse_layer_mutated_row(key: str, value: Any) -> None:
    _survives(parse_layer, json.dumps(doc([row(**{key: value})])), "user:fuzz")


@FUZZ
@given(key=st.sampled_from(["modifier_id", "kind", "factor", "base_usd_per_mtok", "applies_to",
                            "when", "stacking", "sources"]), value=json_values)
def test_parse_layer_mutated_modifier(key: str, value: Any) -> None:
    mod = {"modifier_id": "f", "kind": "multiply", "factor": "0.5", "applies_to": ["*"],
           "when": {"service_tier": "batch"}, "stacking": "documented", key: value}
    _survives(parse_layer, json.dumps(doc(modifiers=[mod])), "user:fuzz")


@FUZZ
@given(top=st.dictionaries(st.sampled_from(["schema", "provider", "as_of", "rows", "modifiers"]),
                           json_values, max_size=5))
def test_parse_layer_document_shapes(top: dict[str, Any]) -> None:
    _survives(parse_layer, json.dumps({**doc(), **top}), "user:fuzz")


@FUZZ
@given(st.text(max_size=60))
def test_parse_model_price(text: str) -> None:
    _survives(parse_model_price, text)


@FUZZ
@given(st.one_of(st.binary(max_size=200), json_values.map(json.dumps)))
def test_parse_rules(data: Any) -> None:
    _survives(parse_rules, data)


@FUZZ
@given(key=st.sampled_from(["rule_id", "provider", "failure_mode", "billed_input",
                            "billed_partial_output", "cache_written", "confidence", "source",
                            "params", "verified_on", "notes", "finding"]), value=json_values)
def test_parse_rules_mutated(key: str, value: Any) -> None:
    rule = {"rule_id": "a.b", "provider": "a", "failure_mode": "b", "billed_input": "no",
            "billed_partial_output": "no", "cache_written": "no", "confidence": "documented",
            "source": "s", key: value}
    _survives(parse_rules, json.dumps({"schema": "tokenbill/billing-rules@1", "rules": [rule]}))


@FUZZ
@given(json_values)
def test_from_model_pricing(value: Any) -> None:
    _survives(from_model_pricing, value if isinstance(value, dict) else {"overrides": value},
              name="fuzz")


@FUZZ
@given(st.one_of(st.binary(max_size=200), json_values.map(json.dumps)))
def test_load_contract(tmp_path_factory: Any, data: Any) -> None:
    path = Path(tmp_path_factory.mktemp("c")) / "c.json"
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    _survives(load_contract, path)


PAGE = (FIXTURES / "pricing_page.md").read_text(encoding="utf-8")


@FUZZ
@given(cut=st.integers(0, len(PAGE)), junk=st.text(max_size=40))
def test_parse_pricing_page_mutations(cut: int, junk: str) -> None:
    _survives(parse_pricing_page, PAGE[:cut] + junk + PAGE[cut:])


@FUZZ
@given(st.text(max_size=300))
def test_parse_pricing_page_random(text: str) -> None:
    _survives(parse_pricing_page, text)


@FUZZ
@given(st.binary(max_size=300))
def test_verify_live_random_pages(data: bytes) -> None:
    _survives(verify_live, builtin(), "https://example.test/pricing.md",
              opener=lambda url, timeout: data, today="2026-09-24")


@FUZZ
@given(json_values)
def test_crosscheck_feed_never_raises(feed: Any) -> None:
    for candidate in (feed, json.dumps(feed), {"data": [feed]}, {"m": feed}):
        found = crosscheck_feed(builtin(), candidate, today="2026-09-24")
        assert all(not d.authoritative for d in found)
