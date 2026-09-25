"""github-copilot-seats: seat snapshots, buckets, assignment kind, editor family (addendum §5.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.adapters.github_seats import CopilotSeatsAdapter, bucket_of
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.records import Attribution, record_key
from tokenbill.core.testing import assert_adapter_conforms, conformance_ingest_options

from .helpers import FIXTURES, assert_no_identity, opts, p_of

SEATS = FIXTURES / "seats"
ADAPTER = CopilotSeatsAdapter()
BUCKETS = ("0-7", "0-7", "8-30", "8-30", "31-90", "31-90", "none_90d", "none_90d")


def _by_principal(result):  # type: ignore[no-untyped-def]
    return {(s.principal, s.org): s for s in result.licenses}


def test_boundary_buckets_against_the_fetch_date() -> None:
    res = ADAPTER.read(SEATS / "enterprise_seats.json", opts())
    seats = _by_principal(res)
    assert {s.snapshot_date for s in res.licenses} == {"2026-09-20"}   # envelope fetched_at
    got = [seats[(p_of(f"dev-{j + 2:02d}"), "acme-eng")].last_activity_bucket for j in range(8)]
    assert tuple(got) == BUCKETS
    auth = [seats[(p_of(f"dev-{j + 2:02d}"), "acme-eng")].last_authenticated_bucket
            for j in range(8)]
    assert tuple(auth) == BUCKETS


@pytest.mark.parametrize(("ts", "bucket"), [
    ("2026-09-20T23:59:59Z", "0-7"), ("2026-09-21T10:00:00Z", "0-7"),
    ("2026-09-13T00:00:00Z", "0-7"), ("2026-09-12T23:59:59Z", "8-30"),
    ("2026-06-22T00:00:00Z", "31-90"), ("2026-06-21T23:59:59Z", "none_90d"), (None, "none_90d")])
def test_bucket_of(ts: str | None, bucket: str) -> None:
    from tokenbill.adapters.github_config import parse_ts_ms
    assert bucket_of(None if ts is None else parse_ts_ms(ts, "t"), "2026-09-20") == bucket


def test_team_assignment_plan_and_two_orgs() -> None:
    res = ADAPTER.read(SEATS / "enterprise_seats.json", opts())
    seats = _by_principal(res)
    canary = [s for s in res.licenses if s.principal == p_of(CANARY_LOGIN)]
    assert sorted(s.org for s in canary) == ["acme-data", "acme-eng"]        # one per org
    assert len({record_key(s) for s in canary}) == 2
    eng = seats[(p_of(CANARY_LOGIN), "acme-eng")]
    data = seats[(p_of(CANARY_LOGIN), "acme-data")]
    assert eng.assigned_via_team is True and data.assigned_via_team is False
    assert eng.plan == "business" and data.plan == "enterprise"
    assert eng.last_activity_surface == "vscode" and data.last_activity_surface == "jetbrains"
    assert eng.team == "platform" and eng.cost_center == "cc-platform"
    assert eng.seat_created == "2026-03-01" and eng.source_kind == "github.copilot_seats"
    no_plan = seats[(p_of("dev-09"), "acme-eng")]
    assert no_plan.plan == "unknown" and no_plan.last_activity_surface is None
    pending = seats[(p_of("dev-08"), "acme-eng")]
    assert pending.pending_cancellation == "2026-10-01"
    assert all(isinstance(s.assigned_via_team, bool) for s in res.licenses)
    assert res.stats["total_seats_reported"] == 9 and len(res.licenses) == 10


def test_no_assignee_identity_survives() -> None:
    res = ADAPTER.read(SEATS / "enterprise_seats.json", opts())
    assert_no_identity(res, "dev-02", "octocat", "Name ", "avatar", "U_1000")
    assert res.source.principal_key_id == opts().principal_key_id


def test_org_from_request_path_or_attribution(tmp_path: Path) -> None:
    res = ADAPTER.read(SEATS / "org_seats.json", opts())
    assert {s.org for s in res.licenses} == {"acme-eng"}
    assert {s.snapshot_date for s in res.licenses} == {"2026-09-25"}          # fetched_ms
    jet = next(s for s in res.licenses if s.principal == p_of("dev-11"))
    assert jet.assigned_via_team is True and jet.last_activity_surface == "jetbrains"
    assert jet.team is None                        # only in the team below k
    body = json.loads((SEATS / "org_seats.json").read_text(encoding="utf-8"))["response"]
    bare = tmp_path / "seats.json"
    bare.write_text(json.dumps(body), encoding="utf-8")
    assert {s.org for s in ADAPTER.read(bare, opts()).licenses} == {None}
    named = ADAPTER.read(bare, opts(attribution=Attribution(workspace_id="acme-eng")))
    assert {s.org for s in named.licenses} == {"acme-eng"}
    assert {s.snapshot_date for s in named.licenses} == {"2026-09-26"}        # opts.now_ms


def test_snapshot_date_assumed_without_clock(tmp_path: Path) -> None:
    body = json.loads((SEATS / "org_seats.json").read_text(encoding="utf-8"))["response"]
    bare = tmp_path / "seats.json"
    bare.write_text(json.dumps(body), encoding="utf-8")
    res = ADAPTER.read(bare, opts(now_ms=0))
    assert {s.snapshot_date for s in res.licenses} == {"2026-09-18"}
    assert "dq.copilot_snapshot_date_assumed" in {n.code for n in res.notes}


def test_duplicate_seat_of_one_person_in_one_org_merges(tmp_path: Path) -> None:
    body = json.loads((SEATS / "org_seats.json").read_text(encoding="utf-8"))
    first = body["response"]["seats"][0]
    second = dict(first, assigning_team={"slug": "x"}, last_activity_at="2026-08-01T00:00:00Z",
                  plan_type="enterprise", pending_cancellation_date="2026-10-01")
    body["response"]["seats"] = [first, second]
    path = tmp_path / "dup.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    res = ADAPTER.read(path, opts())
    (seat,) = res.licenses
    assert seat.assigned_via_team is False and seat.last_activity_bucket == "0-7"
    assert seat.plan == "enterprise" and seat.pending_cancellation is None
    assert res.stats["duplicate_seats"] == 1


def test_bad_seats_are_quarantined(tmp_path: Path) -> None:
    body = json.loads((SEATS / "org_seats.json").read_text(encoding="utf-8"))
    good = body["response"]["seats"][0]
    body["response"]["seats"] = [
        good, {"created_at": "2026-01-01T00:00:00Z"}, dict(good, assignee={"login": " "}),
        dict(good, last_activity_at="yesterday"),
        dict(good, pending_cancellation_date="2026/10/01"), "seat"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    res = ADAPTER.read(path, opts())
    assert len(res.licenses) == 1
    assert sorted(q.reason for q in res.quarantined) == [
        "bad_type:last_activity_at", "bad_type:pending_cancellation_date",
        "missing:assignee.login", "missing:assignee.login", "not_object"]


def test_sniff() -> None:
    for path in SEATS.iterdir():
        assert ADAPTER.sniff(path, path.read_bytes()[:65536])
    for other in (FIXTURES / "config" / "org_billing.jsonl",
                  FIXTURES / "metrics" / "users-1-day_12x3.ndjson"):
        assert not ADAPTER.sniff(other, other.read_bytes()[:65536])


@pytest.mark.parametrize("name", ["enterprise_seats.json", "org_seats.json"])
def test_conforms(name: str) -> None:
    assert_adapter_conforms(ADAPTER, SEATS / name, expect_capabilities={"licenses"},
                            opts=conformance_ingest_options())
