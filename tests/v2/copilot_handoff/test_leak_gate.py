"""Leak gate (brief Build 4): identity terms harvested from the raw inputs, the member scan and the
export abort."""

from __future__ import annotations

import dataclasses
import gzip
import json
import zipfile
from pathlib import Path

import pytest

from tokenbill.copilot.handoff import (
    LeakTerms,
    harvest_leak_terms,
    leak_scan,
)
from tokenbill.core.builders import CANARY, CANARY_EMAIL, CANARY_LOGIN
from tokenbill.core.errors import PrivacyError

from .helpers import export, fake_budget_adapter, fake_usage_adapter, use_fake_registry, write_world

TOKEN = "ghp_" + "Ab1" * 12


def leaky(value_of) -> type:
    return fake_usage_adapter(leak=lambda line, row: dataclasses.replace(
        line, description=value_of(line, row)))


def assert_aborted(tmp_path: Path, err: pytest.ExceptionInfo, member: str, category: str,
                   *secrets: str) -> None:
    msg = str(err.value)
    assert msg.startswith("export aborted:") and member in msg and category in msg
    for s in secrets:
        assert s.lower() not in msg.lower()
    assert not (tmp_path / "out.tbx").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize(("value_of", "category", "secret"), [
    (lambda line, row: f"{line.sku} {row['username']}", "login", "dev-alpha-01x"),
    (lambda line, row: f"see https://github.com/{row['username']}", "url", "github.com"),
    (lambda line, row: f"{line.sku} {TOKEN}", "secret (github_token)", TOKEN),
    (lambda line, row: f"{line.sku} {CANARY_LOGIN}", "canary", CANARY_LOGIN),
    (lambda line, row: f"{line.sku} payments-api@corp.example", "e-mail pattern", "payments-api"),
])
def test_leaking_adapter_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value_of,
                                category: str, secret: str) -> None:
    use_fake_registry(monkeypatch, usage=leaky(value_of))
    world = write_world(tmp_path / "in")
    with pytest.raises(PrivacyError) as err:
        export(world, tmp_path / "out.tbx")
    assert_aborted(tmp_path, err, "records/cost_lines.jsonl", category, secret)


def test_email_in_a_budget_attr_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch, budgets=fake_budget_adapter(("sku", "finance@corp.example")))
    world = write_world(tmp_path / "in")
    with pytest.raises(PrivacyError) as err:
        export(world, tmp_path / "out.tbx")
    assert_aborted(tmp_path, err, "records/config.jsonl", "e-mail pattern", "finance")


def test_team_label_equal_to_a_login_aborts_with_rename_hint(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch)
    world = write_world(tmp_path / "in")
    team_map = {login: ("dev-beta-00x" if team == "alpha" else team)
                for login, team in world["team_map"].items()}
    with pytest.raises(PrivacyError) as err:
        export(world, tmp_path / "out.tbx", team_map=team_map)
    msg = str(err.value)
    assert "login in a team label" in msg and "rename the team in --team-map-csv" in msg
    assert "dev-beta-00x" not in msg
    assert_aborted(tmp_path, err, "records/", "team label")


def test_clean_export_records_the_term_count(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch)
    world = write_world(tmp_path / "in")
    report = export(world, tmp_path / "out.tbx")
    terms = harvest_leak_terms([world["dir"]], team_map_logins=world["team_map"])
    assert report.manifest.leak_scan.terms == len(terms) > 15
    assert report.manifest.leak_scan.result == "clean"
    with zipfile.ZipFile(tmp_path / "out.tbx") as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist()).lower()
    blob = blob.replace(b"tokenbill/copilot-export@1", b"")
    for login in world["logins"]:
        assert login.lower().encode() not in blob
    for needle in (CANARY, CANARY_LOGIN, CANARY_EMAIL, "acme-org/service", "https://", "@"):
        assert needle.lower().encode() not in blob


# ---------------------------------------------------------------------------------------------
# leak_scan units
# ---------------------------------------------------------------------------------------------


def line(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def scan(obj: dict, terms: dict[str, str], member: str = "records/cost_lines.jsonl"
         ) -> list[str]:
    return [cat for _, cat in leak_scan({member: line(obj)}, LeakTerms(terms))]


def test_exact_and_contained_logins() -> None:
    terms = {"mona-lisa": "login", "bob": "login"}
    assert scan({"description": "MONA-LISA"}, terms) == ["login"]
    assert scan({"description": "sku for mona-lisa's work"}, terms) == ["login"]
    assert scan({"description": "Bob"}, terms) == ["login"]
    assert scan({"description": "bobby tables"}, terms) == []        # short: exact only
    assert scan({"team": "mona-lisa"}, terms) == ["login in a team label"]
    assert scan({"workspace_id": "bob"}, terms) == ["login in an organization label"]


def test_repository_and_workflow_terms_skip_organizational_labels() -> None:
    terms = {"platform": "repository_short", "acme/platform": "repository",
             ".github/workflows/triage.lock.yml": "workflow_path"}
    assert scan({"team": "platform", "cost_center": "platform", "workspace_id": "acme"},
                terms) == []
    assert scan({"description": "platform"}, terms) == ["repository name"]
    assert scan({"description": "x acme/platform y"}, terms) == ["repository name"]
    assert scan({"description": "platform-team tools"}, terms) == []      # short: exact only
    assert scan({"description": "ran .github/workflows/triage.lock.yml"},
                terms) == ["workflow path"]
    assert scan({"attrs": [["plan.org:platform", "business"]]}, terms,
                member="records/config.jsonl") == []


def test_merged_team_labels_only_outside_organizational_fields() -> None:
    terms = {"tiny-team": "merged_team"}
    assert scan({"cost_center": "tiny-team"}, terms) == []
    assert scan({"team": "tiny-team"}, terms) == ["merged team label"]
    assert scan({"description": "tiny-team"}, terms) == ["merged team label"]
    assert scan({"description": "the tiny-team crew"}, terms) == []


def test_numeric_ids_and_machine_values() -> None:
    terms = {"58323171": "user_id", "4242": "user_id", "acme-dev": "login"}
    assert scan({"description": "58323171"}, terms) == ["numeric user id"]
    assert scan({"description": "user 58323171 x"}, terms) == ["numeric user id"]
    assert scan({"quantity": "58323171"}, terms) == []                  # decimal quantity
    assert scan({"description": "1.58323171"}, terms) == []            # inside a decimal
    assert scan({"description": "4242"}, terms) == ["numeric user id"]
    assert scan({"description": "a4242b"}, terms) == []                 # < 6 digits: exact only
    assert scan({"amount_nano": 58323171}, terms) == []                 # numbers are not strings
    assert scan({"line_id": "cl_acde0000000000000000acde"}, {"acde00": "login"}) == []
    assert scan({"principal": "p_" + "a" * 20}, {"aaaaaa": "login"}) == []
    assert scan({"date_utc": "2026-09-10"}, {"2026-09-10": "login"}) == []
    assert scan({"cost_type": "actions"}, {"actions": "repository_short"}) == []
    assert scan({"source_kind": "github.copilot_seats"}, {"github": "login"}) == []


def test_content_checks_without_terms() -> None:
    assert scan({"description": "x@y.example"}, {}) == ["e-mail pattern"]
    assert scan({"description": f"token {TOKEN}"}, {}) == ["secret (github_token)"]
    assert scan({"description": "http://intranet/x"}, {}) == ["url"]
    assert scan({"description": CANARY}, {}) == ["canary"]
    assert scan({"description": "a b c"}, {}) == []
    assert leak_scan({"manifest.json": json.dumps(
        {"schema": "tokenbill/copilot-export@1"}).encode()}, frozenset()) == []


def test_structural_problems_and_plain_frozensets() -> None:
    hits = leak_scan({"records/a.jsonl": b"{broken\n", "manifest.json": b"\xff\xfe"},
                     frozenset({"broken"}))
    assert ("manifest.json", "not utf-8") in hits
    assert ("records/a.jsonl", "not json") in hits
    assert ("records/a.jsonl", "login") in hits
    assert leak_scan({"records/x.jsonl": line({"description": "someone"})},
                     frozenset({"SomeOne"})) == [("records/x.jsonl", "login")]
    assert leak_scan({"manifest.json": b"[1, 2"}, frozenset()) == [("manifest.json", "not json")]


def test_member_order_in_results() -> None:
    members = {"records/outcomes.jsonl": line({"team": "x@y.example"}),
               "manifest.json": line({"orgs": ["https://x"]})}
    assert leak_scan(members, frozenset()) == [("manifest.json", "url"),
                                               ("records/outcomes.jsonl", "e-mail pattern")]


# ---------------------------------------------------------------------------------------------
# harvest units
# ---------------------------------------------------------------------------------------------


def test_harvest_csv_json_ndjson_gz_and_zip(tmp_path: Path) -> None:
    d = tmp_path / "raw"
    d.mkdir()
    (d / "usage.csv").write_text("﻿date,Username,organization,repository,workflow_path\n"
                                 "2026-09-10,Octo-Cat,acme,acme/payments-api,.github/wf.yml\n"
                                 "2026-09-10,,acme,,\n", encoding="utf-8")
    (d / "seats.json").write_text(json.dumps({"seats": [
        {"assignee": {"login": "seat-user", "id": 98765432, "name": "Seat User"},
         "organization": {"login": "acme-org-login"},
         "assigning_team": {"name": "Team Label", "slug": "team-label"}}]}), encoding="utf-8")
    (d / "metrics.ndjson").write_text("\n".join(json.dumps(r) for r in (
        {"user_login": "metrics-user", "user_id": 11112222},
        {"user_login": "second-user", "user_id": "33334444"})) + "\n", encoding="utf-8")
    (d / "pull.jsonl").write_text(json.dumps({"request": {"path": "/orgs/acme-org-login/x"},
                                              "response": {"budgets": [
                                                  {"budget_scope": "user",
                                                   "budget_entity_name": "budget-user",
                                                   "user": "budget-user2",
                                                   "budget_alerting": {"alert_recipients":
                                                                       ["alert-user"]}},
                                                  {"budget_scope": "repository",
                                                   "budget_entity_name": "acme/secret-repo"}],
                                                  "costCenters": [{"resources": [
                                                      {"type": "User", "name": "cc-user"},
                                                      {"type": "Repo", "name": "acme/cc-repo"},
                                                      {"type": "Org", "name": "acme"}]}],
                                                  "tasks": [{"user": {"id": 5556667},
                                                             "repository": {"name": "tasks-repo",
                                                                            "full_name":
                                                                            "acme/tasks-repo"}}],
                                                  "email": "someone@corp.example",
                                                  "repositoryName": "rest-repo"}}) + "\n",
                                encoding="utf-8")
    (d / "gz.csv.gz").write_bytes(gzip.compress(b"login,team\ngz-user,x\n"))
    (d / "bad.gz").write_bytes(b"\x1f\x8bnot gzip")
    (d / "sheet.xlsx").write_bytes(b"PK\x03\x04[Content_Types].xml")
    (d / ".hidden.csv").write_text("login\nhidden-user\n", encoding="utf-8")
    terms = harvest_leak_terms([d], team_map_logins=["map-user", "ab"])
    cat = terms.category  # type: ignore[attr-defined]
    for login in ("octo-cat", "seat-user", "seat user", "metrics-user", "second-user",
                  "budget-user", "budget-user2", "alert-user", "cc-user", "gz-user", "map-user"):
        assert login in terms and cat(login) == "login", login
    for uid in ("98765432", "11112222", "33334444", "5556667"):
        assert cat(uid) == "user_id", uid
    assert cat("someone@corp.example") == "email"
    assert cat("acme/payments-api") == "repository" and cat("payments-api") == "repository_short"
    assert cat("acme/secret-repo") == "repository" and cat("acme/cc-repo") == "repository"
    assert cat("rest-repo") == "repository_short" and cat("acme/tasks-repo") == "repository"
    assert cat(".github/wf.yml") == "workflow_path"
    for absent in ("acme-org-login", "team label", "team-label", "acme", "hidden-user", "ab"):
        assert absent not in terms, absent


def test_harvest_tolerates_garbage(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text('login\n"unterminated\n', encoding="utf-8")
    (tmp_path / "b.json").write_text("{not json\n[1,\n", encoding="utf-8")
    (tmp_path / "c.csv").write_text("", encoding="utf-8")
    (tmp_path / "d.csv").write_text("no,identity,columns\n1,2,3\n", encoding="utf-8")
    terms = harvest_leak_terms([tmp_path])
    assert isinstance(terms, LeakTerms)
