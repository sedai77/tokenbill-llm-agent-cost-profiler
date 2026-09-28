"""Agent tasks: only with a user token and listed repositories, one unit per repository, the
repository count in the manifest, ``/agents/tasks`` never requested (addendum §5.12, §19.3 #14)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot.pull_common import MANIFEST_NAME, Manifest, TokenSource, pull, unit_hash
from tokenbill.copilot.pull_metrics import task_ids
from tokenbill.core.errors import UsageError

from .helpers import API, ENT, NOW_MS, ORG, TOKEN_A, USER_TOKEN, FakeGitHub, World, ok, source
from .helpers import token_file as make_token_file

REPOS = ["acme-eng/web-app", "acme-eng/api"]


def task(tid: str, *, sessions: int | None = 1, updated: str = "2026-09-20T10:00:00Z",
         with_sessions: bool = False) -> dict[str, Any]:
    doc: dict[str, Any] = {"id": tid, "state": "completed", "created_at": updated,
                           "updated_at": updated, "creator_type": "user",
                           "artifacts": [{"type": "pull"}]}
    if sessions is not None:
        doc["session_count"] = sessions
    if with_sessions:
        doc["sessions"] = [{"id": f"s-{tid}", "state": "completed", "model": "claude-sonnet-5",
                            "usage": {"type": "ai_credits", "amount": 1000}}]
    return doc


def world(gh: FakeGitHub) -> None:
    def tasks(call: Any) -> Any:
        page = call.query.get("page", "1")
        if page == "1":
            nxt = f"https://{API}{call.path}?per_page=100&page=2"
            return ok({"tasks": [task("a1"), task("a2", sessions=0),
                                 task("a3", updated="2026-08-01T00:00:00Z")]},
                      Link=f'<{nxt}>; rel="next"')
        return ok({"tasks": [task("a4", sessions=None), task("a5", with_sessions=True)]})

    gh.route("GET", rf"{API}/agents/repos/[^/]+/[^/]+/tasks", tasks)
    gh.route("GET", rf"{API}/agents/repos/[^/]+/[^/]+/tasks/([a-z0-9]+)", lambda c: ok(
        task(c.path.rsplit("/", 1)[1], with_sessions=True)))


def run(tmp_path: Path, gh: FakeGitHub, *, kinds: Any = ("agent_tasks",),
        user_token: TokenSource | None = None, repos: Any = REPOS,
        resume: bool = False) -> Manifest:
    if user_token is None:
        user_token = TokenSource.from_file(make_token_file(tmp_path / "user", USER_TOKEN))
    return pull(kinds, enterprise=ENT, orgs=[ORG], since="2026-09-01", until="2026-09-24",
                token=source(tmp_path), user_token=user_token, agent_repos=repos,
                out_dir=tmp_path / "out", opener=gh, sleep=lambda s: None, now_ms=NOW_MS,
                resume=resume)


def test_two_repositories_give_two_units_and_repos_2(tmp_path: Path) -> None:
    gh = FakeGitHub()
    world(gh)
    manifest = run(tmp_path, gh)
    assert manifest.repos == 2 and manifest.complete
    assert [u.kind for u in manifest.units] == ["agent_tasks", "agent_tasks"]
    assert {u.id for u in manifest.units} == {f"agent_tasks/{unit_hash(r)}" for r in REPOS}
    assert all(u.repos == 1 for u in manifest.units)
    doc = json.loads((tmp_path / "out" / MANIFEST_NAME).read_text())
    assert doc["repos"] == 2
    text = (tmp_path / "out" / MANIFEST_NAME).read_text()
    assert "web-app" not in text and "acme-eng/api" not in text
    paths = gh.paths()
    assert "/agents/tasks" not in paths and not any(p.startswith("/agents/tasks") for p in paths)
    # list pages (2 per repo) and details only for tasks with sessions to fetch (a1, a4)
    details = sorted(p for p in paths if p.count("/") == 6)
    assert details == sorted(f"/agents/repos/{r}/tasks/{t}" for r in REPOS for t in ("a1", "a4"))
    assert all(c.auth == f"Bearer {USER_TOKEN}" for c in gh.api_calls())
    rel = manifest.units[0].files[0]
    lines = [json.loads(x) for x in (tmp_path / "out" / rel).read_text().splitlines()]
    assert len(lines) == 4 and lines[0]["request"]["path"].endswith("/tasks")


def test_the_github_token_serves_the_other_kinds(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    world(gh)
    manifest = run(tmp_path, gh, kinds=("seats", "agent_tasks"))
    assert manifest.complete
    for call in gh.api_calls():
        want = USER_TOKEN if call.path.startswith("/agents/") else TOKEN_A
        assert call.auth == f"Bearer {want}"


def test_agent_tasks_need_a_user_token_and_repositories(tmp_path: Path) -> None:
    gh = FakeGitHub()
    with pytest.raises(UsageError, match="user-token"):
        pull(["agent_tasks"], enterprise=ENT, orgs=[], since="2026-09-01", until="2026-09-02",
             token=source(tmp_path), agent_repos=REPOS, out_dir=tmp_path / "out", opener=gh,
             sleep=lambda s: None, now_ms=NOW_MS)
    with pytest.raises(UsageError, match="agent-repos"):
        run(tmp_path, gh, repos=[])
    with pytest.raises(UsageError, match="repository #2") as info:
        run(tmp_path, gh, repos=["acme-eng/ok", "secret-org/../x y"])
    assert "secret-org" not in str(info.value)
    with pytest.raises(UsageError):
        run(tmp_path, gh, repos=["acme/.."])
    assert gh.calls == []


def test_duplicate_repositories_and_a_string(tmp_path: Path) -> None:
    gh = FakeGitHub()
    world(gh)
    manifest = run(tmp_path, gh, repos=["acme-eng/API", "acme-eng/api"])
    assert manifest.repos == 1
    manifest2 = run(tmp_path / "b", gh, repos="acme-eng/api")
    assert manifest2.repos == 1


def test_a_missing_task_detail_is_skipped(tmp_path: Path) -> None:
    gh = FakeGitHub(raise_http_errors=False)
    world(gh)
    gh.route("GET", rf"{API}/agents/repos/[^/]+/[^/]+/tasks/a4", ok({}, status=404))
    manifest = run(tmp_path, gh, repos=REPOS[:1])
    assert manifest.complete


def test_task_ids_filter() -> None:
    since = 1_788_220_800_000  # 2026-09-01
    body = {"tasks": [task("a1"), task("a1"), task("a2", sessions=0), task("../x"),
                      task("a3", updated="2026-08-01T00:00:00Z"), task("a4", updated="bad"),
                      task("a5", with_sessions=True), "junk", {"id": 5, "state": "x"}]}
    assert task_ids(body, since_ms=since) == ["a1", "a4"]
    assert task_ids([task("b1")]) == ["b1"]
    assert task_ids({"tasks": None}) == [] and task_ids("x") == []
    naive = task("n1", updated="2026-09-02T00:00:00")
    assert task_ids([naive], since_ms=since) == ["n1"]
