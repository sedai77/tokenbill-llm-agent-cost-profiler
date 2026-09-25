"""The pricing-YAML mini-parser and quote normalization (CP-RATES brief, Build #3), incl. the
SPEC §21 #5 fuzz tests: only TokenbillError subclasses escape the parsers."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.copilot import rates_verify as rv
from tokenbill.core.errors import PricingError

from .support import YML_DIR

SAMPLE = """\
# Column keys: a comment
---
- model: 'GPT-5.4[^2]'   # a trailing comment
  provider: openai
  release_status: GA
  category: Versatile
  threshold: '\u2264 272K'
  tier: Default
  input: $2.50
  cached_input: $0.25
  output: $15.00
  cache_write: Not applicable
  notes: "Prompts \\u2264272K tokens"

- model: GPT-5.4
  provider: openai
  threshold: '> 272K'
  tier: 'Long context'
  input: $5.00
  cached_input: $0.50
  output: $22.50
-
  model: Claude Opus 4.8 (fast mode) (preview)
  provider: anthropic
  input: $10.00
  cached_input: $1.00
  output: $50.00
  cache_write: $12.50
"""


def test_parse_flat_list_of_maps() -> None:
    items = rv.parse_yaml_list(SAMPLE)
    assert len(items) == 3
    assert items[0]["model"] == "GPT-5.4[^2]"
    assert items[0]["threshold"] == "\u2264 272K"
    assert items[0]["notes"] == "Prompts \u2264272K tokens"
    assert items[0]["cache_write"] == "Not applicable"
    assert items[1]["tier"] == "Long context"
    assert items[2]["model"] == "Claude Opus 4.8 (fast mode) (preview)"


@pytest.mark.parametrize(("raw", "value"), [
    ("'it''s'", "it's"),
    ('"a\\tb\\n\\"c\\"\\\\"', 'a\tb\n"c"\\'),
    ('"\\x41\\u00e9\\U0001F600\\/\\ \\_\\N"', "A\u00e9\U0001F600/ \xa0\x85"),
    ("'quoted' # comment", "quoted"),
    ("C#sharp", "C#sharp"),
    ("plain value   # comment", "plain value"),
    ("$0.25", "$0.25"),
    ("", ""),
])
def test_scalars(raw: str, value: str) -> None:
    assert rv.parse_yaml_list(f"- key: {raw}\n") == [{"key": value}]


def test_crlf_and_unicode_line_separators_inside_values() -> None:
    text = "- model: 'a\u2028b'\r\n  input: \"x\x85y\"\r\n"
    assert rv.parse_yaml_list(text) == [{"model": "a\u2028b", "input": "x\x85y"}]


@pytest.mark.parametrize("text", [
    "model: x\n",                       # a map, not a list
    "  model: x\n",                     # a key outside an item
    "- model: x\n    input: y\n",       # deeper indentation (nested block)
    "- model: x\n  input: y\n   z: w\n",
    "- model: [a, b]\n",                # flow collection
    "- model: {a: b}\n",
    "- model: |\n",                     # block scalar
    "- model: >\n",
    "- model: &anchor x\n",
    "- model: *alias\n",
    "- model: !tag x\n",
    "- model: - x\n",
    "- model: x\n\t input: y\n",        # tab in indentation
    "- model: x\n  model: y\n",         # duplicate key
    "- model: 'x' y\n",                 # text after a quoted value
    "- model: 'x\n",                    # unterminated
    '- model: "x\n',
    '- model: "x\\q"\n',                # unknown escape
    '- model: "x\\u12"\n',              # short escape
    '- model: "\\U00110000"\n',         # outside Unicode
    '- model: "x\\',
    "- no colon here\n",
    "- 9key: x\n",
])
def test_malformed_yaml_raises(text: str) -> None:
    with pytest.raises(PricingError):
        rv.parse_yaml_list(text)


def test_parser_limits() -> None:
    with pytest.raises(PricingError):
        rv.parse_yaml_list(b"- model: x\n")  # type: ignore[arg-type]
    with pytest.raises(PricingError):
        rv.parse_yaml_list("#" * (rv.MAX_YAML_CHARS + 1))


def test_every_fixture_revision_parses() -> None:
    files = sorted(YML_DIR.glob("*.yml"))
    assert len(files) == 38
    for path in files:
        items = rv.parse_yaml_list(path.read_text(encoding="utf-8"))
        assert items and all("model" in item and "input" in item for item in items)
        assert rv.quotes_from_maps(items)


# ---------------------------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------------------------


def test_quotes_merge_tiers_strip_footnotes_and_map_names() -> None:
    quotes = rv.quotes_from_maps(rv.parse_yaml_list(SAMPLE))
    assert list(quotes) == [("gpt-5.4", "standard"), ("claude-opus-4-8", "fast")]
    gpt = quotes[("gpt-5.4", "standard")]
    assert gpt.display == "GPT-5.4" and gpt.footnotes == ("2",)
    assert gpt.provider == "openai" and gpt.category == "Versatile"
    assert gpt.release_status == "GA"
    assert gpt.threshold == 272_000
    assert gpt.default == rv.TierPrices(Decimal("2.50"), Decimal("0.25"), None, Decimal("15.00"))
    assert gpt.long_context == rv.TierPrices(Decimal("5.00"), Decimal("0.50"), None,
                                             Decimal("22.50"))
    fast = quotes[("claude-opus-4-8", "fast")]
    assert fast.display == "Claude Opus 4.8 (fast mode) (preview)"
    assert fast.default.cache_write == Decimal("12.50") and fast.long_context is None
    assert str(fast.default.input) == "10.00"  # published spelling kept


def _one(**entry: str) -> rv.Quote:
    base = {"model": "Kimi K3", "input": "$3.00", "output": "$15.00"}
    return next(iter(rv.quotes_from_maps([{**base, **entry}]).values()))


@pytest.mark.parametrize(("threshold", "tokens"), [
    ("> 1M", 1_000_000), ("> 1.5K", 1500), ("> 200000", 200_000), ("> 272K tokens", 272_000)])
def test_threshold_forms(threshold: str, tokens: int) -> None:
    q = rv.quotes_from_maps([
        {"model": "Grok 4.7", "input": "$2", "output": "$6", "tier": "Default"},
        {"model": "Grok 4.7", "input": "$4", "output": "$12", "tier": "Long context",
         "threshold": threshold}])[("grok-4.7", "standard")]
    assert q.threshold == tokens


def test_price_forms() -> None:
    q = _one(input="$1,000.00", cached_input="n/a", cache_write="", category="")
    assert q.default.input == Decimal("1000.00")
    assert q.default.cached_input is None and q.default.cache_write is None
    assert q.category is None and q.provider == ""


@pytest.mark.parametrize("entries", [
    [{"input": "$1", "output": "$2"}],                                    # no model
    [{"model": "Auto", "input": "$1", "output": "$2"}],                   # pseudo label
    [{"model": "Copilot code review", "input": "$1", "output": "$2"}],
    [{"model": "Auto: Kimi K3", "input": "$1", "output": "$2"}],          # routed label
    [{"model": "Kimi K3", "output": "$2"}],                               # no input
    [{"model": "Kimi K3", "input": "Not applicable", "output": "$2"}],
    [{"model": "Kimi K3", "input": "$abc", "output": "$2"}],
    [{"model": "Kimi K3", "input": "$1e3", "output": "$2"}],
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "tier": "Premium"}],
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "tier": "Long context"}],
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "threshold": "> 5K"}],
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "threshold": "< 5K"}],
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "threshold": "\u2264 1.0005K"}],
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "threshold": "> 0"}],
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "tier": "Long context",
      "threshold": "> 5K"}],                                              # band without default
    [{"model": "Kimi K3", "input": "$1", "output": "$2"}] * 2,            # tier listed twice
    [{"model": "Kimi K3", "input": "$1", "output": "$2", "threshold": "\u2264 5K"},
     {"model": "Kimi K3", "input": "$2", "output": "$4", "tier": "Long context",
      "threshold": "> 6K"}],                                              # thresholds disagree
    ["not a map"],
    [{"model": 3, "input": "$1", "output": "$2"}],
])
def test_bad_quotes_raise(entries: list) -> None:
    with pytest.raises(PricingError):
        rv.quotes_from_maps(entries)


# ---------------------------------------------------------------------------------------------
# fuzz and round trip (SPEC §21 #5)
# ---------------------------------------------------------------------------------------------

_VOCAB = ["model", "provider", "category", "threshold", "tier", "input", "cached_input",
          "output", "cache_write", "notes", "release_status"]
_FRAGMENTS = st.sampled_from([
    "- ", "  ", "model: ", "input: ", "'", '"', "\\u2264", "$", "0.25", "> 272K", "\u2264 200K",
    "Long context", "Default", "Not applicable", "#", "[^x]", "\n", "\r\n", "\t", "|", "&",
    "GPT-5.4", "Claude Opus 4.8 (fast mode)", ":", "-", "\\", "\u2028"])


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.text(max_size=400), st.lists(_FRAGMENTS, max_size=60).map("".join)))
def test_fuzz_parser_raises_only_tokenbill_errors(text: str) -> None:
    try:
        items = rv.parse_yaml_list(text)
        rv.quotes_from_maps(items)
    except TokenbillError:
        pass


_VALUES = st.one_of(st.text(max_size=30), st.sampled_from([
    "$0.25", "$1,000.00", "Not applicable", "> 272K", "\u2264 272K", "> 1M", "Long context",
    "Default", "GPT-5.4", "Grok 4.7[^promo]", "Auto", "Claude Opus 4.8 (fast mode)", "$x"]))


@settings(max_examples=300, deadline=None)
@given(st.lists(st.dictionaries(st.sampled_from(_VOCAB), _VALUES, max_size=8), max_size=6))
def test_fuzz_quotes_raise_only_pricing_errors(items: list[dict[str, str]]) -> None:
    try:
        quotes = rv.quotes_from_maps(items)
    except PricingError:
        return
    for (model, speed), q in quotes.items():
        assert model and speed in ("standard", "fast") and q.default.input >= 0


_KEYS = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True)
_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)


def _single(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@settings(max_examples=200, deadline=None)
@given(st.lists(st.dictionaries(_KEYS, _TEXT, min_size=1, max_size=6), max_size=5),
       st.booleans())
def test_round_trip_quoted_values(items: list[dict[str, str]], double: bool) -> None:
    if not double and any("\n" in v or "\r" in v for item in items for v in item.values()):
        return  # single-quoted scalars cannot carry line breaks on one line
    lines = []
    for item in items:
        for i, (key, value) in enumerate(item.items()):
            scalar = json.dumps(value, ensure_ascii=False) if double else _single(value)
            lines.append(("- " if i == 0 else "  ") + f"{key}: {scalar}")
        lines.append("")
    assert rv.parse_yaml_list("\n".join(lines)) == items
