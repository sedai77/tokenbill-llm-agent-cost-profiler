"""SPEC §11.3: the LiteLLM fragment renderer and its stdlib mini-parser (with fuzzing: only
``TokenbillError`` subclasses may escape)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.errors import TokenbillError, UsageError
from tokenbill.plan.litellm import (
    parse_yaml_subset,
    render_injection_points,
    validate_litellm_fragment,
)

GOOD = """\
# comment
model_list:
  - model_name: "claude-opus-5-5"   # trailing comment
    litellm_params:
      cache_control_injection_points:
        - location: message
          role: system
        - location: 'message'
          index: -1
"""


def test_render_and_validate_round_trip() -> None:
    text = render_injection_points(["claude-sonnet-5", "anthropic/claude-opus-5-5",
                                    "claude-sonnet-5"])
    validate_litellm_fragment(text)
    doc = parse_yaml_subset(text)
    names = [e["model_name"] for e in doc["model_list"]]
    assert names == ["anthropic/claude-opus-5-5", "claude-sonnet-5"]
    points = doc["model_list"][0]["litellm_params"]["cache_control_injection_points"]
    assert points == [{"location": "message", "role": "system"},
                      {"location": "message", "index": -1}]
    assert "ttl" not in text
    with_ttl = render_injection_points(["m1"], ttl="1h")
    assert '# ttl: "1h"  # VERIFY' in with_ttl and "VERIFY" in with_ttl.splitlines()[3]
    assert parse_yaml_subset(with_ttl) == parse_yaml_subset(render_injection_points(["m1"]))


@pytest.mark.parametrize("models, ttl", [
    ([], None), ("claude", None), (["bad\nname"], None), ([""], None), ([7], None),
    (["ok"], "2h"), (["x" * 300], None), ([f"m{i}" for i in range(1001)], None),
])
def test_render_rejects_bad_input(models, ttl) -> None:
    with pytest.raises(UsageError):
        render_injection_points(models, ttl=ttl)


def test_the_parser_reads_its_yaml_subset() -> None:
    validate_litellm_fragment(GOOD)
    assert parse_yaml_subset("a: 1\nb: true\nc: ~\nd: 'it''s'\ne: \"x\\ty\"\nf: plain text\n"
                             ) == {"a": 1, "b": True, "c": None, "d": "it's", "e": "x\ty",
                                   "f": "plain text"}
    assert parse_yaml_subset("- a\n- - b\n  - c\n-\n  k: v\n") == ["a", ["b", "c"], {"k": "v"}]
    assert parse_yaml_subset("k:\n- 1\n- 2\nz: null\n") == {"k": [1, 2], "z": None}
    assert parse_yaml_subset("\"quoted key\": 1\n'single': 2\n") == {"quoted key": 1,
                                                                    "single": 2}
    assert parse_yaml_subset("k:\nnext: 1\n") == {"k": None, "next": 1}
    assert parse_yaml_subset("url: http://x/y\n") == {"url": "http://x/y"}
    assert parse_yaml_subset("a: 'x # not a comment'\n") == {"a": "x # not a comment"}


@pytest.mark.parametrize("text", [
    "", "# only a comment\n", "a:\n\tb: 1\n", "a: [1, 2]\n", "a: {b: 1}\n", "a: &x 1\n",
    "a: !tag 1\n", "a: |\n  text\n", "---\na: 1\n", "a: 1\na: 2\n", "a: 1\n  b: 2\n",
    "a:\n  b: 1\n c: 2\n", "a: 'unterminated\n", "a: \"bad \\q\"\n", "a: 'x'y'\n",
    "- a\nb: 1\n", "a: b: c\n", "1a: x\n", "a: \"x\"extra\n", "\"k\" 1\n",
    "a: \x01\n", "a: 'x\n", "\"unterminated: 1\n", "'': 1\n", "a: x:\n",
    "a:\n" + "".join("  " * i + "- \n" for i in range(40)),
])
def test_the_parser_refuses_everything_else(text) -> None:
    with pytest.raises(UsageError):
        parse_yaml_subset(text)


@pytest.mark.parametrize("text", [
    "a: 1\n",
    "model_list: []\n",
    "model_list: 1\n",
    "model_list:\n  - x\n",
    "model_list:\n  - model_name: a\n    litellm_params: {}\n".replace("{}", "x"),
    "model_list:\n  - model_name: a\n    litellm_params:\n      other: 1\n",
    "model_list:\n  - model_name: a\n    litellm_params:\n"
    "      cache_control_injection_points: 1\n",
    "model_list:\n  - model_name: a\n    litellm_params:\n"
    "      cache_control_injection_points:\n        - location: tool\n          role: system\n",
    "model_list:\n  - model_name: a\n    litellm_params:\n"
    "      cache_control_injection_points:\n        - location: message\n",
    "model_list:\n  - model_name: a\n    litellm_params:\n"
    "      cache_control_injection_points:\n        - location: message\n          role: x\n",
    "model_list:\n  - model_name: a\n    litellm_params:\n"
    "      cache_control_injection_points:\n        - location: message\n          index: x\n",
    "model_list:\n  - model_name: a\n    litellm_params:\n"
    "      cache_control_injection_points:\n        - location: message\n          index: 1\n"
    "          extra: 2\n",
    GOOD + GOOD.split("\n", 1)[1].replace("model_list:\n", ""),   # duplicate model_name
    "model_list:\n  - model_name: 7\n    litellm_params:\n"
    "      cache_control_injection_points:\n        - location: message\n          index: 1\n",
])
def test_validation_rejects_wrong_structures(text) -> None:
    with pytest.raises(UsageError):
        validate_litellm_fragment(text)


def test_size_and_type_limits() -> None:
    with pytest.raises(UsageError):
        parse_yaml_subset(12)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        parse_yaml_subset("a: " + "x" * 1_000_001)


_NAMES = st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=0x2FF),
                 min_size=1, max_size=40)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=400))
def test_fuzz_arbitrary_text_raises_only_usage_errors(text: str) -> None:
    try:
        validate_litellm_fragment(text)
    except TokenbillError:
        pass


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(["model_list:", "  - model_name: a", "    litellm_params:",
                                 "      cache_control_injection_points:",
                                 "        - location: message", "          role: system",
                                 "          index: -1", "- x", "k: 'v'", "  # c", "", "---",
                                 "a: \"b\"", "  -", "x:", "      - location: message"]),
                max_size=30))
def test_fuzz_line_soup_raises_only_usage_errors(lines: list[str]) -> None:
    try:
        validate_litellm_fragment("\n".join(lines))
    except TokenbillError:
        pass


@settings(max_examples=200, deadline=None)
@given(st.lists(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._:/@+ -]{0,30}", fullmatch=True),
                min_size=1, max_size=5),
       st.sampled_from([None, "5m", "1h"]))
def test_every_rendered_fragment_validates(models: list[str], ttl: str | None) -> None:
    text = render_injection_points(models, ttl=ttl)
    validate_litellm_fragment(text)
    doc = parse_yaml_subset(text)
    assert sorted(e["model_name"] for e in doc["model_list"]) == sorted(set(models))


@settings(max_examples=200, deadline=None)
@given(_NAMES)
def test_fuzz_model_names_are_rendered_or_refused(name: str) -> None:
    try:
        text = render_injection_points([name])
    except TokenbillError:
        return
    validate_litellm_fragment(text)
