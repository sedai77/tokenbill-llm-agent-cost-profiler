"""Unit and hypothesis fuzz tests of the parsers CLI-LEDGER owns: only ``TokenbillError``
subclasses (or argparse's ``ArgumentTypeError`` for argparse ``type=`` callables) may escape, and
errors never echo the rejected value."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill import cli
from tokenbill.common import TokenbillError
from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.records import Attribution, WorkloadClass
from tokenbill.pipeline import ledger

SECRET = "sk-secret-VALUE-1234567890"
FUZZ = settings(max_examples=200, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture])


# --- dates and windows -------------------------------------------------------------------------


def test_parse_date_normalizes_and_rejects() -> None:
    assert ledger.parse_date(" 2026-09-01 ", "--since") == "2026-09-01"
    for bad in ("2026-9-1", "2026-02-30", "yesterday", "", None, 20260901):
        with pytest.raises(UsageError):
            ledger.parse_date(bad, "--since")


def test_resolve_window_defaults_and_order() -> None:
    now = ledger.date_start_ms("2026-09-25") + 5_000
    assert ledger.resolve_window(None, None, now_ms=now) == (0, ledger.date_start_ms("2026-09-26"))
    assert ledger.resolve_window("2026-09-01", "2026-09-02", now_ms=now) == (
        ledger.date_start_ms("2026-09-01"), ledger.date_start_ms("2026-09-02"))
    with pytest.raises(UsageError, match="after --since"):
        ledger.resolve_window("2026-09-02", "2026-09-02", now_ms=now)
    assert ledger.day_of(ledger.date_start_ms("2026-09-02") + 1) == "2026-09-02"


@FUZZ
@given(st.one_of(st.none(), st.text(max_size=20)), st.one_of(st.none(), st.text(max_size=20)))
def test_resolve_window_fuzz(since: str | None, until: str | None) -> None:
    try:
        lo, hi = ledger.resolve_window(since, until, now_ms=1_790_000_000_000)
    except UsageError as exc:  # fixed, content-free messages
        assert str(exc).startswith(("--since ", "--until "))
    else:
        assert 0 <= lo < hi


def test_parse_pct() -> None:
    assert ledger.parse_pct("0.5", "--tolerance-pct") == "0.5"
    assert ledger.parse_pct(1, "x") == "1"
    for bad in ("-1", "1e3", "abc", "1000", True, None):
        with pytest.raises(UsageError):
            ledger.parse_pct(bad, "x")


@FUZZ
@given(st.one_of(st.text(max_size=12), st.integers(), st.none(), st.booleans()))
def test_parse_pct_fuzz(value: object) -> None:
    try:
        text = ledger.parse_pct(value, "--tolerance-pct")
    except UsageError:
        return
    from decimal import Decimal

    assert Decimal(text) >= 0


# --- attributes ---------------------------------------------------------------------------------


def test_parse_attr_pairs_and_attribution() -> None:
    values = ledger.parse_attr_pairs(["team.id=payments", "workload=ci", "department=eng",
                                      "tokenbill.arm=a", "cost_center=cc-1"])
    assert values == {"team": "payments", "workload_class": "ci", "extra.department": "eng",
                      "arm": "a", "cost_center": "cc-1"}
    attr = ledger.attribution_from(values)
    assert attr.team == "payments" and attr.workload_class is WorkloadClass.CI
    assert dict(attr.extra) == {"department": "eng"}
    assert ledger.attribution_from({}) == Attribution()
    with pytest.raises(UsageError, match="unknown key"):
        ledger.parse_attr_pairs(["colour=blue"])
    with pytest.raises(UsageError, match="K=V"):
        ledger.parse_attr_pairs(["team"])
    with pytest.raises(UsageError):
        ledger.parse_attr_pairs(["team="])
    with pytest.raises(UsageError, match="workload"):
        ledger.attribution_from({"workload_class": "nightly"})
    with pytest.raises(UsageError, match="invalid attribution"):
        ledger.attribution_from({"billing_path": "barter"})


def test_attr_errors_never_echo_values() -> None:
    with pytest.raises(UsageError) as exc:
        ledger.parse_attr_pairs([f"team={SECRET}\x00"])
    assert SECRET not in str(exc.value)


@FUZZ
@given(st.lists(st.text(max_size=40), max_size=4))
def test_parse_attr_pairs_fuzz(pairs: list[str]) -> None:
    try:
        values = ledger.parse_attr_pairs(pairs)
        ledger.attribution_from(values)
    except TokenbillError:
        pass


def test_parse_resource_attributes() -> None:
    got = ledger.parse_resource_attributes(
        "team.id=data%20platform,service.name=x,cost_center=cc-9,bad,tokenbill.wave=2,"
        "workload=%ZZ")
    assert got == {"team": "data platform", "cost_center": "cc-9", "wave": "2"}
    assert ledger.parse_resource_attributes(None) == {}


@FUZZ
@given(st.text(max_size=80))
def test_parse_resource_attributes_fuzz(text: str) -> None:
    ledger.parse_resource_attributes(text)  # never raises


# --- principal refs -----------------------------------------------------------------------------


def test_resolve_principal_ref(tmp_path: Path) -> None:
    assert ledger.resolve_principal_ref(None, {}) is None
    assert ledger.resolve_principal_ref("none", {}) is None
    assert ledger.resolve_principal_ref("env:DEV", {"DEV": "dev-042"}) == "dev-042"
    ref = tmp_path / "ref"
    ref.write_text("device.7\n", encoding="utf-8")
    assert ledger.resolve_principal_ref(f"mdm-file:{ref}", {}) == "device.7"
    for spec, env in (("env:DEV", {}), ("ldap:x", {}), ("env:", {}), ("mdm-file:/nope", {}),
                      ("env:DEV", {"DEV": "alice@example.com"})):
        with pytest.raises(UsageError) as exc:
            ledger.resolve_principal_ref(spec, env)
        assert "alice" not in str(exc.value)
    big = tmp_path / "big"
    big.write_bytes(b"a" * 5000)
    with pytest.raises(UsageError, match="too large"):
        ledger.resolve_principal_ref(f"mdm-file:{big}", {})
    binary = tmp_path / "bin"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(UsageError, match="UTF-8"):
        ledger.resolve_principal_ref(f"mdm-file:{binary}", {})


@FUZZ
@given(st.text(max_size=40), st.text(max_size=70))
def test_resolve_principal_ref_fuzz(spec: str, value: str) -> None:
    try:
        ref = ledger.resolve_principal_ref(spec, {"V": value})
    except UsageError as exc:
        assert value not in str(exc) or len(value.strip()) < 8 or value in spec
    else:
        assert ref is None or ("@" not in ref and 1 <= len(ref) <= 64)


# --- collector options, CI attribution ----------------------------------------------------------


def test_collector_options() -> None:
    opts = ledger.collector_options(principal_ref="dev-1", now_ms=5)
    assert opts.identity_mode == "central" and opts.principal_key is None
    key = bytes(range(32))
    two = ledger.collector_options(identity_mode="two-stage", collection_key=key)
    assert two.principal_key == key and two.name_key == key and two.principal_key_id
    with pytest.raises(PrivacyError):
        ledger.collector_options(content="full")
    with pytest.raises(UsageError):
        ledger.collector_options(identity_mode="install")
    with pytest.raises(UsageError, match="collection-key"):
        ledger.collector_options(identity_mode="two-stage")


def test_ci_attribution() -> None:
    env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "acme/app", "GITHUB_WORKFLOW": "ci",
           "GITHUB_RUN_ATTEMPT": "3"}
    attr, notes = ledger.ci_attribution(env, bytes(range(32)))
    assert attr.workload_class is WorkloadClass.CI
    assert attr.entrypoint == "claude-code-github-action"
    assert attr.repo is not None and attr.repo.startswith("h_") and "acme" not in attr.repo
    extra = dict(attr.extra)
    assert extra["workflow"].startswith("h_") and extra["run_attempt"] == "3" and not notes
    bare, notes = ledger.ci_attribution(env, None)
    assert bare.repo is None and "workflow" not in dict(bare.extra)
    assert [n.code for n in notes] == ["dq.ci_names_unhashed"]
    same, notes = ledger.ci_attribution({}, None)
    assert same == Attribution() and notes == []


# --- CLI-level parsers --------------------------------------------------------------------------


@FUZZ
@given(st.text(max_size=40))
def test_model_price_fuzz(spec: str) -> None:
    try:
        model, a, b = cli._model_price(spec)
    except argparse.ArgumentTypeError:
        return
    assert model and 0 <= a <= 1e9 and 0 <= b <= 1e9


def test_positive_int() -> None:
    assert cli._positive_int("3") == 3
    for bad in ("0", "-1", "x"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._positive_int(bad)


def test_verb_index() -> None:
    assert cli._verb_index(["--db", "x", "--quiet", "bill", "--db", "y"]) == 3
    assert cli._verb_index(["--format=json", "demo"]) == 1
    assert cli._verb_index(["--version"]) is None
    assert cli._verb_index(["--", "demo"]) == 1
    assert cli._verb_index(["--"]) is None


@FUZZ
@given(st.lists(st.sampled_from(["--db", "x", "--quiet", "bill", "-v", "--jobs", "2",
                                 "--format", "--", "demo", "--config"]), max_size=8))
def test_verb_index_fuzz(argv: list[str]) -> None:
    at = cli._verb_index(argv)
    assert at is None or 0 <= at < len(argv)


@FUZZ
@given(st.sampled_from(["purge", "bill", "export", "reconcile", "showback", "frobnicate"]),
       st.lists(st.text(max_size=12), max_size=5))
def test_main_never_raises_on_arbitrary_argv(tmp_path_factory: pytest.TempPathFactory,
                                             verb: str, argv: list[str]) -> None:
    """Any argv ends in an exit code (argparse usage errors, clean errors) — never an exception."""
    argv = [a for a in argv if a not in ("-h", "--help", "--version")]
    old = os.getcwd()
    os.chdir(tmp_path_factory.mktemp("fuzz"))
    try:
        from .helpers import run_main

        code, _out, err = run_main([verb, *argv])
    finally:
        os.chdir(old)
    assert code in (0, 1, 2, 3, 4)
    assert "Traceback" not in err and "internal error" not in err
