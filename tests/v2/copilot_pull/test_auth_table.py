"""``AUTH_TABLE`` (addendum §19.4) and ``HANDOFF_KINDS``: every request template is a row, the
``--help`` text, and (gate) every row's call appears in CP-HANDOFF's admin guide."""

from __future__ import annotations

import re

import pytest

from tokenbill.copilot import pull_common
from tokenbill.copilot.pull_common import (
    AUTH_TABLE,
    HANDOFF_KINDS,
    PULL_KINDS,
    auth_row_for,
    auth_table_text,
)


def templates() -> list[str]:
    return [getattr(pull_common, n) for n in pull_common.__all__ if n.startswith("P_")]


def test_every_request_template_is_exactly_one_read_only_row() -> None:
    for template in templates():
        method = "POST" if template == pull_common.P_EXPORT else "GET"
        rows = [r for r in AUTH_TABLE if not r.emitted and r.method == method
                and template in r.paths]
        assert len(rows) == 1, template
        assert auth_row_for(f"{method} {template}") is rows[0]
    assert auth_row_for("GET /nowhere") is None
    assert auth_row_for("DELETE /orgs/{org}/copilot/billing/selected_users") is None


def test_rows_are_well_formed() -> None:
    kinds_used: set[str] = set()
    for row in AUTH_TABLE:
        assert row.call and row.who and row.classic_scopes and row.notes and row.paths
        if row.emitted:
            assert not row.kinds
        else:
            assert row.kinds and set(row.kinds) <= set(PULL_KINDS)
            kinds_used |= set(row.kinds)
        head = row.call.split(" ", 1)[0]
        assert head == row.method or row.call.startswith("/")
    assert kinds_used == set(PULL_KINDS)
    calls = [r.call for r in AUTH_TABLE]
    assert len(calls) == len(set(calls))
    for needed in ("DELETE /orgs/{org}/copilot/billing/selected_users",
                   "DELETE /orgs/{org}/copilot/billing/selected_teams",
                   "PUT /enterprises/{e}/copilot/policies/coding_agent",
                   "GET /agents/repos/{o}/{r}/tasks"):
        assert needed in calls
    # the minimal credentials never ask for write scopes
    for row in AUTH_TABLE:
        if not row.emitted:
            assert "manage_billing" not in row.minimal and "admin:" not in row.minimal


def test_handoff_kinds() -> None:
    assert HANDOFF_KINDS == ("ai_usage", "metered", "summary", "metrics", "seats", "config")
    assert set(HANDOFF_KINDS) < set(PULL_KINDS) and "agent_tasks" not in HANDOFF_KINDS


def test_help_text_lists_every_call() -> None:
    text = auth_table_text()
    for row in AUTH_TABLE:
        assert row.call in text
    assert "never executed" in text and "Never grant manage_billing:copilot" in text
    assert text.index("POST /enterprises/{e}/settings/billing/reports") < text.index(
        "request files (never executed)")


@pytest.mark.gate
def test_every_auth_table_call_appears_in_the_admin_guide() -> None:
    handoff = pytest.importorskip("tokenbill.copilot.handoff")
    guide = handoff.admin_guide_text()
    missing = [row.call for row in AUTH_TABLE if row.call not in guide]
    assert missing == []
    # the guide's own "calls path A makes" table names no request CP-PULL does not make
    section = guide.split("## 3.", 1)[1].split("## 4.", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")][2:]
    for line in rows:
        for call in re.findall(r"`((?:GET|POST) /[^`?]+)", line):
            assert any(call.split(" ", 1)[1] in r.paths
                       or call.split(" ", 1)[1].startswith(r.call.split("?", 1)[0]
                                                           .split(" ", 1)[-1])
                       for r in AUTH_TABLE), call
