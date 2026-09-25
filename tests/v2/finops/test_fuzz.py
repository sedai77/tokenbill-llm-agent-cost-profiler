"""Fuzzing the allocation-rules parser (SPEC §21 #5): arbitrary JSON documents, rule-shaped
documents, raw file bytes and regex text — only ``TokenbillError`` subclasses may escape, and every
accepted rule set applies to a request without error."""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import make_request
from tokenbill.core.errors import TokenbillError
from tokenbill.finops import allocation as A

json_leaf = st.none() | st.booleans() | st.integers() | st.text(max_size=12)
json_doc = st.recursive(json_leaf, lambda c: st.lists(c, max_size=4)
                        | st.dictionaries(st.text(max_size=10), c, max_size=4), max_leaves=25)
fields = st.sampled_from([*A.MATCH_FIELDS, "extra.mdm_group", "extra.workflow", "principal",
                          "bogus"])
ops = st.sampled_from([*A.OPERATORS, "like"])
values = st.one_of(st.text(max_size=12), st.lists(st.text(max_size=8), max_size=3), st.integers())
rule = st.fixed_dictionaries(
    {},
    optional={"id": st.text(max_size=8), "match": st.dictionaries(fields, st.dictionaries(
                  ops, values, min_size=1, max_size=2), max_size=3),
              "set": st.dictionaries(st.sampled_from([*A.TARGETS, "owner"]),
                                     st.sampled_from(["payments", "ci", "service", "", "x" * 300]),
                                     max_size=3),
              "split": st.fixed_dictionaries({"targets": st.lists(st.text(max_size=6),
                                                                  max_size=3)})})
REQUEST = make_request("L", 0, 0, {"uncached_input": 1, "output": 1},
                       attribution={"team": "t", "entrypoint": "sdk-py", "repo": "h_" + "a" * 20,
                                    "extra": (("mdm_group", "ml"),)})


def _check(doc: object) -> None:
    try:
        rs = A.parse_rules(doc)
    except TokenbillError:
        return
    A.apply_rules(REQUEST, rs)


@settings(max_examples=300, deadline=None)
@given(json_doc)
def test_arbitrary_json_only_raises_tokenbill_errors(doc: object) -> None:
    _check(doc)
    _check({"rules": doc})


@settings(max_examples=300, deadline=None)
@given(st.lists(rule, max_size=5))
def test_rule_shaped_documents(rules: list[dict]) -> None:
    _check({"rules": rules})


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=60))
def test_regex_text(pattern: str) -> None:
    _check({"rules": [{"match": {"repo": {"regex": pattern}}, "set": {"team": "a"}}]})


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(max_size=200) | json_doc.map(lambda d: json.dumps(d).encode()))
def test_file_bytes(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "rules.json"
    path.write_bytes(data)
    try:
        A.apply_rules(REQUEST, A.load_rules(path))
    except TokenbillError:
        pass
