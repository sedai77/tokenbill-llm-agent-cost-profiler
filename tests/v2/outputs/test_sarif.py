"""SARIF 2.1.0 (SPEC §14.5): required keys, one result per violation, rules."""

from __future__ import annotations

import dataclasses
import json

import pytest

from tokenbill.core.errors import ContractViolation
from tokenbill.core.types import CheckViolation
from tokenbill.outputs.sarif import RULES, SARIF_VERSION, to_sarif

from .sample import check


def test_required_keys_and_one_result_per_violation() -> None:
    log = to_sarif(check(), tool_version="0.2.0")
    json.dumps(log)                                   # serializable, no custom types
    assert log["version"] == SARIF_VERSION == "2.1.0"
    assert log["$schema"].endswith("sarif-2.1.0.json")
    (run,) = log["runs"]
    driver = run["tool"]["driver"]
    assert driver["name"] == "tokenbill" and driver["version"] == "0.2.0"
    assert [r["id"] for r in driver["rules"]] == ["TB-CACHE-SHARE", "TB-NEW-BREAKER",
                                                  "TB-COST-REGRESSION", "TB-SERIALIZATION-CHURN"]
    for rule in driver["rules"]:
        assert rule["shortDescription"]["text"] and rule["fullDescription"]["text"]
    results = run["results"]
    assert len(results) == len(check().violations) == 2
    for res, v in zip(results, check().violations, strict=True):
        assert res["ruleId"] == v.rule_id
        assert driver["rules"][res["ruleIndex"]]["id"] == v.rule_id
        assert res["level"] in ("error", "warning", "note", "none")
        assert res["message"]["text"]
        loc = res["locations"][0]
        assert loc["physicalLocation"]["artifactLocation"]["uri"] == "timestamp.jsonl"
        assert loc["logicalLocations"][0]["fullyQualifiedName"].startswith("run=r1#call=")
        assert len(res["partialFingerprints"]["tokenbillViolation/v1"]) == 32
    assert run["properties"]["passed"] is False
    assert run["invocations"] == [{"executionSuccessful": True}]


def test_passing_check_has_no_results_and_fingerprints_are_stable() -> None:
    ok = dataclasses.replace(check(), passed=True, violations=())
    assert to_sarif(ok, tool_version="0.2.0")["runs"][0]["results"] == []
    a = to_sarif(check(), tool_version="x")["runs"][0]["results"]
    b = to_sarif(check(), tool_version="y")["runs"][0]["results"]
    assert [r["partialFingerprints"] for r in a] == [r["partialFingerprints"] for r in b]


def test_locations_without_run_and_messages_are_sanitized() -> None:
    v = CheckViolation("TB-COST-REGRESSION", "warning", "median up \x1b[31m20%", "baseline.json")
    res = to_sarif(dataclasses.replace(check(), violations=(v,)), tool_version="0")
    r = res["runs"][0]["results"][0]
    assert "logicalLocations" not in r["locations"][0]
    assert r["message"]["text"] == "median up 20%"


def test_unknown_rule_or_level_is_a_contract_violation() -> None:
    for v in (CheckViolation("TB-OTHER", "error", "m", "f"),
              CheckViolation("TB-CACHE-SHARE", "fatal", "m", "f")):
        with pytest.raises(ContractViolation):
            to_sarif(dataclasses.replace(check(), violations=(v,)), tool_version="0")
    with pytest.raises(ContractViolation):
        to_sarif("x", tool_version="0")  # type: ignore[arg-type]
    assert set(RULES) == {"TB-CACHE-SHARE", "TB-NEW-BREAKER", "TB-COST-REGRESSION",
                          "TB-SERIALIZATION-CHURN"}
