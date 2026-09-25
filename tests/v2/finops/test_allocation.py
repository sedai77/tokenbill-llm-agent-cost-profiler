"""Allocation rules (SPEC §14.6): first match per field, splits, (unallocated), coverage KPI."""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis
from tokenbill.core.records import Request, WorkloadClass
from tokenbill.core.types import LedgerCostRow
from tokenbill.finops import allocation as A


def req(**attr: object) -> Request:
    channel = attr.pop("channel", "anthropic_api")
    model = attr.pop("model", "claude-opus-5-5")
    return make_request("L1", 0, 0, {"uncached_input": 10, "output": 5}, model,
                        attribution=attr, channel=channel)


RULES = {"rules": [
    {"id": "sdk", "match": {"entrypoint": {"prefix": "sdk-"}},
     "set": {"team": "platform", "workload_class": "service"}},
    {"id": "ws",
     "match": {"workspace_id": {"in": ["h_aaaaaaaaaaaaaaaaaaaa", "h_bbbbbbbbbbbbbbbbbbbb"]}},
     "set": {"team": "payments", "cost_center": "cc-pay", "project": "checkout"}},
    {"id": "mdm", "match": {"extra.mdm_group": {"eq": "ml"}}, "set": {"cost_center": "cc-ml"}},
    {"id": "shared", "match": {"repo": {"regex": "^h_ff"}},
     "split": {"method": "proportional", "by": "dev_days", "targets": ["payments", "search"]}},
]}


def rules() -> A.RuleSet:
    return A.parse_rules(RULES)


def test_first_match_wins_per_target_field() -> None:
    a = A.apply_rules(req(entrypoint="sdk-py", workspace_id="h_aaaaaaaaaaaaaaaaaaaa",
                          extra=(("mdm_group", "ml"),)), rules())
    assert a.team == "platform"                         # rule 1 set team first
    assert a.workload_class is WorkloadClass.SERVICE
    assert a.cost_center == "cc-pay"                    # rule 2 is the first to set cost_center
    assert a.project == "checkout"
    b = A.apply_rules(req(extra=(("mdm_group", "ml"),), team="search"), rules())
    assert b.team == "search" and b.cost_center == "cc-ml"   # own team kept


def test_unmatched_is_unallocated_and_split_marks_the_team() -> None:
    assert A.apply_rules(req(), rules()).team == A.UNALLOCATED
    assert A.apply_rules(req(repo="h_ff00000000000000000a"), rules()).team == A.split_marker(
        "shared")
    # a request that already has a team and no matching rule keeps it
    assert A.apply_rules(req(team="ops"), rules()).team == "ops"


def test_match_on_model_channel_and_provider() -> None:
    rs = A.parse_rules({"rules": [
        {"match": {"model": {"regex": "haiku"}, "channel": {"eq": "bedrock"},
                   "provider": {"eq": "anthropic"}}, "set": {"project": "batch-jobs"}}]})
    hit = A.apply_rules(req(model="claude-haiku-4-5", channel="bedrock"), rs)
    miss = A.apply_rules(req(model="claude-haiku-4-5"), rs)
    assert hit.project == "batch-jobs" and miss.project is None
    assert rs.rules[0].rule_id == "r1"
    empty = A.parse_rules({"rules": [{"set": {"team": "all"}}]})
    assert A.apply_rules(req(), empty).team == "all"     # an empty match matches everything
    with pytest.raises(UsageError):
        A.apply_rules(req(), RULES)  # type: ignore[arg-type]


def cost_row(team: str | None, nano: int, *, qty: int = 1000, basis: Basis = Basis.LIST,
             low: int = 0, high: int = 0, users: int = 9) -> LedgerCostRow:
    return LedgerCostRow(date_utc="2026-09-01", provider="anthropic", channel="anthropic_api",
                         model="claude-opus-5-5", bucket="output", team=team, cost_center=None,
                         project=None, workspace_id=None, lane_kind="main",
                         workload_class="interactive", agent_product=None, billing_path="api_key",
                         quantity=qty, priced_nano=nano, estimated_low_nano=low,
                         estimated_high_nano=high, basis=basis, rate_row_id=None, n_users=users)


def test_split_rows_sum_to_the_original_and_carry_the_method() -> None:
    marker = A.split_marker("shared")
    rows = A.split_rows([cost_row(marker, 1_000_000_001, qty=7, low=5, high=11),
                         cost_row("ops", 50)], rules(), weights={"payments": 2, "search": 1})
    split = [r for r in rows if r.team in ("payments", "search")]
    assert [r.team for r in split] == ["payments", "search"]
    assert sum(r.priced_nano for r in split) == 1_000_000_001
    assert [r.priced_nano for r in split] == [666_666_667, 333_333_334]
    assert sum(r.quantity for r in split) == 7
    assert sum(r.estimated_low_nano for r in split) == 5
    assert sum(r.estimated_high_nano for r in split) == 11
    for r in split:
        assert isinstance(r, A.AllocatedCostRow)
        assert r.method_id == "tokenbill.split.proportional.dev_days"
        details = json.loads(r.method_details)
        assert details["rule"] == "shared" and details["weights"] == {"payments": 2, "search": 1}
        assert r.n_users == 9
    assert rows[-1].team == "ops" and not isinstance(rows[-1], A.AllocatedCostRow)


def test_split_without_developer_days_is_equal_and_unknown_markers_pass() -> None:
    rows = A.split_rows([cost_row(A.split_marker("shared"), 10)], rules(), weights={})
    assert [r.priced_nano for r in rows] == [5, 5]
    assert json.loads(rows[0].method_details)["weights_basis"].startswith("equal")
    ghost = cost_row(A.split_marker("gone"), 10)
    assert A.split_rows([ghost], rules(), weights={}) == [ghost]
    with pytest.raises(UsageError):
        A.split_rows([cost_row(A.split_marker("shared"), 10)], rules(), weights={"payments": 1.5})


@settings(max_examples=200, deadline=None)
@given(st.integers(-10**15, 10**15), st.lists(st.integers(0, 10**6), min_size=1, max_size=8))
def test_apportion_is_exact(total: int, weights: list[int]) -> None:
    if not sum(weights):
        weights = [1] * len(weights)
    parts = A._apportion(total, weights)
    assert sum(parts) == total
    den = sum(weights)
    for p, w in zip(parts, weights, strict=True):
        assert abs(p * den - total * w) < den          # within one unit of the exact share


def test_coverage_kpi() -> None:
    rows = [cost_row("payments", 950), cost_row(A.UNALLOCATED, 30), cost_row(None, 20),
            cost_row("search", 10_000, basis=Basis.LIST_EQUIVALENT)]
    assert Decimal(A.coverage(rows)) == Decimal("0.95")
    assert A.coverage([]) == "1"
    assert A.coverage([cost_row(A.split_marker("shared"), 5)]) == "1"
    assert A.is_allocated("x") and not A.is_allocated("") and not A.is_allocated(None)


def test_load_rules_from_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"schema": A.SCHEMA, **RULES}), encoding="utf-8")
    rs = A.load_rules(path)
    assert [r.rule_id for r in rs.rules] == ["sdk", "ws", "mdm", "shared"]
    assert len(rs.sha256) == 64 and rs.rule("mdm") is not None and rs.rule("x") is None
    with pytest.raises(UsageError):
        A.load_rules(tmp_path / "missing.json")
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (1 << 20) + b"{}")
    with pytest.raises(UsageError):
        A.load_rules(big)
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff{")
    with pytest.raises(UsageError):
        A.load_rules(bad)
    nan = tmp_path / "nan.json"
    nan.write_text('{"rules": [NaN]}', encoding="utf-8")
    with pytest.raises(UsageError):
        A.load_rules(nan)


BAD = [
    [], {"rules": {}}, {"rules": [], "extra": 1}, {"schema": "x", "rules": []},
    {"rules": ["x"]}, {"rules": [{"set": {"team": "a"}, "bogus": 1}]},
    {"rules": [{"id": "bad id", "set": {"team": "a"}}]},
    {"rules": [{"id": "a", "set": {"team": "x"}}, {"id": "a", "set": {"team": "y"}}]},
    {"rules": [{"match": [], "set": {"team": "a"}}]},
    {"rules": [{"match": {"principal": {"eq": "p_x"}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"extra.secret": {"eq": "x"}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"team": {"eq": "a", "in": ["b"]}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"team": {"like": "a"}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"team": {"in": []}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"team": {"eq": ""}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"team": {"eq": "a\x00"}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"team": {"regex": "a"}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"repo": {"regex": "(a+)+$"}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"repo": {"regex": "(a)\\1"}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"repo": {"regex": "x" * 201}}, "set": {"team": "a"}}]},
    {"rules": [{"match": {"repo": {"regex": "("}}, "set": {"team": "a"}}]},
    {"rules": [{"set": []}]}, {"rules": [{"set": {"owner": "a"}}]},
    {"rules": [{"set": {"workload_class": "nightly"}}]},
    {"rules": [{"set": {"team": "(unallocated)"}}]}, {"rules": [{"set": {"team": "(split:x)"}}]},
    {"rules": [{}]},
    {"rules": [{"split": {"targets": ["a"]}, "set": {"team": "b"}}]},
    {"rules": [{"split": {"method": "equal", "targets": ["a"]}}]},
    {"rules": [{"split": {"by": "requests", "targets": ["a"]}}]},
    {"rules": [{"split": {"targets": []}}]}, {"rules": [{"split": {"targets": ["a", "a"]}}]},
    {"rules": [{"split": {"targets": ["(unallocated)"]}}]}, {"rules": [{"split": "a"}]},
    {"rules": [{"split": {"targets": ["a"], "weights": [1]}}]},
    {"rules": [{"match": {3: {"eq": "a"}}, "set": {"team": "a"}}]},
]


@pytest.mark.parametrize("doc", BAD)
def test_malformed_rules_are_usage_errors(doc: object) -> None:
    with pytest.raises(UsageError):
        A.parse_rules(doc)


def test_regex_subset() -> None:
    ok = A.parse_rules({"rules": [{"match": {"repo": {"regex": "(?:ab)+|(?P<x>c)?[)(]\\d{2}"}},
                                   "set": {"team": "a"}}]})
    assert ok.rules[0].conditions[0].pattern is not None
    for pattern in ("((a+))+", "(?:a*)*", "(?P=x)", "\\g<1>", "(?(1)a|b)"):
        with pytest.raises(UsageError):
            A._check_regex(pattern, "t")


def test_rules_never_match_missing_values() -> None:
    cond = A.Condition("team", "prefix", ("a",))
    assert not cond.test(None) and cond.test("abc")
    rs = A.parse_rules({"rules": [{"match": {"channel": {"eq": "x"}}, "set": {"team": "a"}}]})
    no_serving = dataclasses.replace(req(), attempts=(dataclasses.replace(
        req().attempts[0], inferences=()),))
    assert A.apply_rules(no_serving, rs).team == A.UNALLOCATED
