"""Admin answers (brief Build 8, addendum §5.17): questionnaire JSON → ``run_flags`` +
``org_settings`` statements."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot import admin_answers
from tokenbill.copilot.admin_answers import ANSWERS_SCHEMA, FIELDS, parse_answers, template_text
from tokenbill.core import pool
from tokenbill.core.builders import make_license
from tokenbill.core.errors import UsageError
from tokenbill.core.records import CONFIG_KEYS, _prefixed_key_ok

from .helpers import FIXTURES, NOW_MS


def write(tmp_path: Path, doc: Any, name: str = "answers.json") -> Path:
    path = tmp_path / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def answers(tmp_path: Path, **fields: Any) -> list:
    return parse_answers(write(tmp_path, {"schema": ANSWERS_SCHEMA, **fields}),
                         snapshot_ms=NOW_MS)


def test_template_parses_to_an_empty_run_flags_snapshot(tmp_path: Path) -> None:
    text = template_text()
    doc = json.loads(text)
    assert doc["schema"] == ANSWERS_SCHEMA
    assert set(k for k in doc if not k.startswith("_help")) == {"schema", *FIELDS}
    assert all(f"_help_{f}" in doc for f in FIELDS)
    assert len(text.splitlines()) <= 45
    (snap,) = parse_answers(write(tmp_path, text), snapshot_ms=NOW_MS)
    assert (snap.kind, snap.source_kind, snap.entity_id, snap.attrs) == (
        "run_flags", "tokenbill.admin_answers", "admin_answers", ())
    assert snap.snapshot_ms == NOW_MS == snap.fetched_ms


def test_filled_answers_flatten_exactly() -> None:
    snaps = parse_answers(FIXTURES / "answers_filled.json", snapshot_ms=NOW_MS)
    flags, *orgs = snaps
    assert dict(flags.attrs) == {
        "plan.enterprise": "business",
        "plan.org:octo-labs": "mixed",
        "pool_seats.enterprise.business": 120,
        "billing_mode.enterprise": "metered",
        "renewal_date.enterprise": "2027-01-31",
        "capped_policy.Platform": "block",
        "compliance": "none",
        "promo_eligible": True,
        "paid_usage_policy": "enabled",
        "budget_stop.enterprise": False,
        "budget_stop.cc:Platform": True,
    }
    assert all(_prefixed_key_ok(k, CONFIG_KEYS["run_flags"]) for k, _ in flags.attrs)
    assert [(o.kind, o.entity_id, o.attrs, o.source_kind) for o in orgs] == [
        ("org_settings", "org:octo-labs", (("seat_management_setting", "assign_selected"),),
         "tokenbill.admin_answers"),
        ("org_settings", "org:octo-web", (("seat_management_setting", "assign_all"),),
         "tokenbill.admin_answers"),
    ]


def test_unknown_and_null_are_omitted(tmp_path: Path) -> None:
    (snap,) = answers(tmp_path, plan={"enterprise": "unknown", "org:a": None}, compliance=None,
                      promo_eligible="unknown", pool_seats={"enterprise": {"business": None}},
                      renewal_date={"enterprise": "unknown"}, budget_stop=None,
                      seat_policy={"org:a": "unknown"}, capped_policy=None)
    assert snap.attrs == ()


def test_statements_feed_plan_detection(tmp_path: Path) -> None:
    flags = answers(tmp_path, plan={"enterprise": "enterprise"})
    seats = [make_license(f"p_{i:020x}", plan="unknown", snapshot_date="2026-09-10",
                          source_kind="github.copilot_activity_report", assigned_via_team=None)
             for i in range(3)]
    (pe,) = pool.detect_plans([], seats, flags, month="2026-09")
    assert (pe.plan, pe.source) == ("enterprise", "admin_statement")


@pytest.mark.parametrize(("fields", "fragment"), [
    ({"plans": {}}, "unknown field 'plans'"),
    ({"plan": {"enterprise": "platinum"}}, "plan"),
    ({"plan": {"enterprise": "Business edition, I think"}}, "plan"),
    ({"plan": {"alice": "business"}}, "keys must be"),
    ({"plan": ["business"]}, "object keyed by entity"),
    ({"compliance": "hipaa"}, "compliance"),
    ({"promo_eligible": "yes"}, "promo_eligible"),
    ({"renewal_date": {"enterprise": "next spring"}}, "renewal_date"),
    ({"renewal_date": {"enterprise": "2027-02-30"}}, "renewal_date"),
    ({"pool_seats": {"enterprise": {"business": -1}}}, "pool_seats"),
    ({"pool_seats": {"enterprise": {"business": True}}}, "pool_seats"),
    ({"pool_seats": {"enterprise": {"premium": 3}}}, "business or enterprise"),
    ({"capped_policy": {"Platform": "block"}}, "cc:<cost center name>"),
    ({"capped_policy": {"cc:ops@example.com": "block"}}, "capped_policy"),
    ({"seat_policy": {"enterprise": "assign_all"}}, "org:<login>"),
    ({"seat_policy": {"org:bad login": "assign_all"}}, "seat_policy"),
    ({"budget_stop": {"enterprise": "sometimes"}}, "budget_stop"),
    ({"paid_usage_policy": "on"}, "paid_usage_policy"),
    ({"billing_mode": {"enterprise": "credit card"}}, "billing_mode"),
])
def test_invalid_answers(tmp_path: Path, fields: dict[str, Any], fragment: str) -> None:
    with pytest.raises(UsageError) as err:
        answers(tmp_path, **fields)
    assert fragment in str(err.value)


def test_unknown_key_that_is_not_a_token_is_not_echoed(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as err:
        answers(tmp_path, **{"alice@example.com": 1})
    assert "alice" not in str(err.value) and "a key" in str(err.value)


def test_document_level_errors(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="schema"):
        parse_answers(write(tmp_path, {"plan": {}}), snapshot_ms=0)
    with pytest.raises(UsageError, match="JSON object"):
        parse_answers(write(tmp_path, "[]"), snapshot_ms=0)
    with pytest.raises(UsageError, match="valid JSON"):
        parse_answers(write(tmp_path, "{nope"), snapshot_ms=0)
    with pytest.raises(UsageError, match="whole numbers"):
        parse_answers(write(tmp_path, f'{{"schema": "{ANSWERS_SCHEMA}", "pool_seats": '
                            '{"enterprise": {"business": 1.5}}}'), snapshot_ms=0)
    with pytest.raises(UsageError, match="twice"):
        parse_answers(write(tmp_path, f'{{"schema": "{ANSWERS_SCHEMA}", "compliance": '
                            '"none", "compliance": "fedramp"}'), snapshot_ms=0)
    with pytest.raises(UsageError, match="not readable"):
        parse_answers(tmp_path / "missing.json", snapshot_ms=0)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (2**20 + 1))
    with pytest.raises(UsageError, match="1 MiB"):
        parse_answers(big, snapshot_ms=0)
    with pytest.raises(UsageError, match="snapshot_ms"):
        parse_answers(FIXTURES / "answers_filled.json", snapshot_ms=-1)
    with pytest.raises(UsageError, match="building"):
        parse_answers(FIXTURES / "answers_filled.json", snapshot_ms=10**18)


def test_help_keys_are_ignored_and_bom_accepted(tmp_path: Path) -> None:
    path = tmp_path / "bom.json"
    doc = {"schema": ANSWERS_SCHEMA, "_help_anything": "free text is fine here",
           "_help": ["also fine"], "compliance": "fedramp"}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode())
    (snap,) = parse_answers(path, snapshot_ms=1)
    assert snap.attrs == (("compliance", "fedramp"),)


def test_module_surface() -> None:
    assert admin_answers.SOURCE_KIND == "tokenbill.admin_answers"
    assert admin_answers.__all__ == ["ANSWERS_SCHEMA", "FIELDS", "SOURCE_KIND", "parse_answers",
                                     "template_text"]
