"""github-agent-tasks (addendum §5.7) and the github-usage-records refusal (§5.8)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from tokenbill.adapters.github_agent_tasks import AgentTasksAdapter
from tokenbill.adapters.github_usage_records import UsageRecordsRefusal
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.records import UsageBuckets
from tokenbill.core.testing import assert_adapter_conforms, conformance_ingest_options

from .helpers import FIXTURES, assert_no_identity, blob, h, opts

TASKS = FIXTURES / "agent_tasks"
RECORDS = FIXTURES / "usage_records"
TASKS_ADAPTER = AgentTasksAdapter()
REFUSAL = UsageRecordsRefusal()


def test_sessions_become_provider_estimate_aggregates() -> None:
    res = TASKS_ADAPTER.read(TASKS / "repo_tasks.jsonl", opts())
    by_state = {dict(a.dims)["state"]: a for a in res.aggregates}
    assert set(by_state) == {"completed", "failed", "timed_out"}
    done = by_state["completed"]
    assert done.reported_cost_nano == 232_848_000             # 23,284,800,000 nano-credits ÷ 100
    assert done.reported_cost_basis == "provider_estimate" and done.finality == "final"
    assert done.source_kind == "github.agent_tasks"
    dims = dict(done.dims)
    assert dims == {"channel": "github_copilot", "model": "claude-sonnet-4-6",
                    "state": "completed", "artifact": "branch+pull",
                    "repo": h("acme-eng/web-TB-CANARY-7f3a91"), "team": "(unmapped)"}
    assert dict(by_state["failed"].dims)["model"] == "gpt-5.4"
    assert by_state["failed"].reported_cost_nano == 10_000_000
    assert dict(by_state["timed_out"].dims)["artifact"] == "none"
    assert done.bucket_end_ms - done.bucket_start_ms == 45 * 60_000
    assert all(a.usage == UsageBuckets() for a in res.aggregates)       # no tokens: estimate
    codes = {n.code: n for n in res.notes}
    assert codes["dq.copilot_legacy_pru"].count == 1                  # premium_requests skipped
    assert codes["dq.copilot_agent_tasks_coverage"].count == 2
    assert "2 listed repositories" in codes["dq.copilot_agent_tasks_coverage"].detail
    assert_no_identity(res, "Fix the login", "Please fix", "boom", "copilot/fix")


def test_team_from_the_session_user_id() -> None:
    res = TASKS_ADAPTER.read(TASKS / "repo_tasks.jsonl", opts(team_map=(("1006", "payments"),)))
    teams = {dict(a.dims)["state"]: dict(a.dims)["team"] for a in res.aggregates}
    assert teams == {"completed": "(unmapped)", "failed": "(unmapped)", "timed_out": "payments"}


def test_user_scope_tasks_refused_in_central_mode() -> None:
    res = TASKS_ADAPTER.read(TASKS / "user_tasks.json", opts())
    assert res.aggregates == [] and res.capabilities == frozenset()
    (note,) = res.notes
    assert note.code == "dq.copilot_agent_tasks_user_scope" and note.count == 1
    mine = TASKS_ADAPTER.read(TASKS / "user_tasks.json", opts(identity_mode="install"))
    assert len(mine.aggregates) == 1 and mine.aggregates[0].reported_cost_nano == 5_000_000


def test_list_page_without_sessions_and_bad_sessions(tmp_path: Path) -> None:
    res = TASKS_ADAPTER.read(TASKS / "task_list.json", opts())
    assert res.aggregates == [] and res.stats["tasks_without_sessions"] == 1
    task = json.loads((TASKS / "user_tasks.json").read_text(encoding="utf-8"))["response"]
    good = task["sessions"][0]
    task["sessions"] = [good, dict(good, id=None), dict(good, created_at=None),
                        dict(good, id="s2", usage={"type": "ai_credits", "amount": 1.5}),
                        dict(good, id="s3", created_at="not a time"), "x",
                        dict(good, id="s4", state="exploded", usage=None,
                             created_at="2020-01-01T00:00:00Z")]
    path = tmp_path / "t.json"
    path.write_text(json.dumps([task, "not a task"]), encoding="utf-8")
    res = TASKS_ADAPTER.read(path, opts())
    assert sorted(q.reason for q in res.quarantined) == [
        "bad_type:created_at", "bad_type:usage.amount", "missing:created_at", "missing:id",
        "not_object", "not_object"]
    states = sorted(dict(a.dims)["state"] for a in res.aggregates)
    assert states == ["completed", "unknown"]
    unknown = next(a for a in res.aggregates if dict(a.dims)["state"] == "unknown")
    assert unknown.reported_cost_nano is None and unknown.reported_cost_basis is None
    assert unknown.finality == "provisional"
    assert dict(unknown.dims)["repo"] == h("id:1296269")
    windowed = TASKS_ADAPTER.read(path, opts(since_ms=1_700_000_000_000))
    assert len(windowed.aggregates) == 1 and windowed.stats["outside_window"] == 1


def test_latest_fetch_wins_per_session(tmp_path: Path) -> None:
    lines = (TASKS / "repo_tasks.jsonl").read_text(encoding="utf-8").splitlines()
    newer = json.loads(lines[0])
    newer["fetched_ms"] += 1
    newer["response"]["sessions"][0]["usage"]["amount"] = 100
    path = tmp_path / "two.jsonl"
    path.write_text(lines[0] + "\n" + json.dumps(newer) + "\n", encoding="utf-8")
    res = TASKS_ADAPTER.read(path, opts())
    done = next(a for a in res.aggregates if dict(a.dims)["state"] == "completed")
    assert done.reported_cost_nano == 1


def test_tasks_sniff_and_conformance() -> None:
    for path in TASKS.iterdir():
        assert TASKS_ADAPTER.sniff(path, path.read_bytes()[:65536])
    other = FIXTURES / "seats" / "org_seats.json"
    assert not TASKS_ADAPTER.sniff(other, other.read_bytes()[:65536])
    assert_adapter_conforms(TASKS_ADAPTER, TASKS / "repo_tasks.jsonl",
                            expect_capabilities={"aggregates"},
                            opts=conformance_ingest_options())
    assert_adapter_conforms(TASKS_ADAPTER, TASKS / "user_tasks.json",
                            expect_capabilities={"aggregates"},
                            opts=conformance_ingest_options())       # install mode: self-view


# ---------- usage records: refused ----------


@pytest.mark.parametrize("name", ["usage_records.json", "usage_records.ndjson"])
def test_usage_records_are_refused(name: str) -> None:
    res = REFUSAL.read(RECORDS / name, opts())
    assert (res.requests, res.aggregates, res.cost_lines, res.licenses, res.activity,
            res.config, res.outcomes) == ([], [], [], [], [], [], [])
    (note,) = res.notes
    assert note.code == "dq.raw_bodies_ignored" and note.count == 2
    assert res.stats["records_ignored"] == 2 and res.capabilities == frozenset()
    assert_no_identity(res, "Hello", "chat/completions", "req-abc")
    assert REFUSAL.sniff(RECORDS / name, (RECORDS / name).read_bytes())


def test_usage_records_envelope_gz_and_directory(tmp_path: Path) -> None:
    records = json.loads((RECORDS / "usage_records.json").read_text(encoding="utf-8"))
    env = {"request": {"path": "/enterprises/acme/copilot/usage-records"}, "response": records}
    path = tmp_path / "env.json.gz"
    path.write_bytes(gzip.compress(json.dumps(env).encode()))
    head = gzip.decompress(path.read_bytes())[:65536]
    assert REFUSAL.sniff(path, head)
    assert REFUSAL.read(path, opts()).notes[0].count == 2
    big = tmp_path / "dir"
    big.mkdir()
    (big / "a.ndjson").write_text(
        "".join(json.dumps(r) + "\n" for r in records * 700), encoding="utf-8")
    (big / "b.json").write_text(json.dumps(records), encoding="utf-8")
    res = REFUSAL.read(big, opts(name_key=b""))
    assert res.notes[0].count == 1402 and res.source.name_key_id is None
    assert res.source.source_id.startswith("s_")
    bad = tmp_path / "bad.json.gz"
    bad.write_bytes(b"\x1f\x8b\x08\x00garbage")
    with pytest.raises(SourceError):
        REFUSAL.read(bad, opts())
    with pytest.raises(SourceError):
        REFUSAL.read(tmp_path / "missing.json", opts())
    with pytest.raises(UsageError):
        REFUSAL.read(path, None)  # type: ignore[arg-type]


def test_usage_records_not_claimed_by_other_adapters_and_conform() -> None:
    from tokenbill.core import registry

    for name in ("usage_records.json", "usage_records.ndjson"):
        adapter = registry.sniff_adapter(RECORDS / name)
        assert adapter is not None and adapter.name == "github-usage-records"
    res = assert_adapter_conforms(REFUSAL, RECORDS / "usage_records.json",
                                  expect_capabilities=set(), opts=conformance_ingest_options())
    assert "TB-CANARY" not in blob(res)
