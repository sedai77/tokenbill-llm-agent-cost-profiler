"""``pull`` end to end with the fake GitHub: units and manifest, recording rules, token rotation and
expiry, resume, 403 handling, secret scanning, determinism (addendum §5.12; brief CP-PULL)."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot import pull_common
from tokenbill.copilot.pull_common import (
    ACCEPT,
    API_VERSION,
    AUTH_TABLE,
    HANDOFF_KINDS,
    KEEP_RAW_HINT,
    MANIFEST_NAME,
    RESUME_HINT,
    Manifest,
    TokenExpired,
    TokenSource,
    pull,
    require_complete,
)
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import GateFailed, UsageError

from .helpers import (
    API,
    CC_ID,
    ENT,
    NOW_MS,
    ORG,
    TOKEN_A,
    TOKEN_B,
    Call,
    FakeGitHub,
    World,
    all_recorded_bytes,
    err,
    ok,
    source,
    template_regex,
    token_file,
)


def run(tmp_path: Path, gh: FakeGitHub, *, out: str = "out", kinds: Any = HANDOFF_KINDS,
        token: TokenSource | None = None, since: str = "2026-09-01", until: str = "2026-09-02",
        sleeps: list[int] | None = None, **kw: Any) -> Manifest:
    sleeps = sleeps if sleeps is not None else []
    return pull(kinds, enterprise=kw.pop("enterprise", ENT), orgs=kw.pop("orgs", [ORG]),
                since=since, until=until, token=token or source(tmp_path),
                out_dir=tmp_path / out, opener=gh, sleep=sleeps.append, now_ms=NOW_MS, **kw)


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------------------------
# the handoff pull
# ---------------------------------------------------------------------------------------------


def test_handoff_pull_records_every_source(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    sleeps: list[int] = []
    manifest = run(tmp_path, gh, sleeps=sleeps)
    out = tmp_path / "out"
    assert manifest.complete and not manifest.incomplete and not manifest.forbidden
    assert {u.status for u in manifest.units} == {"complete"}
    assert {u.kind for u in manifest.units} == set(HANDOFF_KINDS)
    ids = {u.id for u in manifest.units}
    assert {"config/budgets", "config/cost_centers", f"config/org_billing/{ORG}",
            "seats/enterprise", f"seats/org/{ORG}", "summary/usage_summary/2026-09",
            "summary/ai_credit_usage/2026-09", "summary/usage_by_cost_center/2026-09",
            "ai_usage/2026-09-01_2026-09-02", "metered/2026-09-01_2026-09-02"} <= ids
    assert sum(1 for i in ids if i.startswith("metrics/")) == 6  # 3 reports x 2 days
    assert sleeps == [30, 30]  # one poll per export
    # every file private, directories 0700, files listed in the manifest exist
    for path in out.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    files = manifest.files(out)
    assert files and all(f.is_file() for f in files)
    assert sorted(p for p in out.rglob("*") if p.is_file()) == sorted([*files,
                                                                       out / MANIFEST_NAME])
    assert Manifest.load(out / MANIFEST_NAME) == manifest
    # the report CSVs are stored verbatim under the unit id
    csv_text = (out / "ai_usage/2026-09-01_2026-09-02.csv").read_text(encoding="utf-8-sig")
    assert csv_text.startswith("date,product,sku") and CANARY_LOGIN in csv_text
    # the summary unit asked for every cost center and for "none"
    ccs = [c.query.get("cost_center_id") for c in gh.api_calls()
           if c.path.endswith("/settings/billing/usage")]
    assert ccs == [CC_ID, "none"]


def test_requests_carry_github_headers_and_auth_only_to_github(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    run(tmp_path, gh)
    api = gh.api_calls()
    assert api
    for call in api:
        assert call.headers["accept"] == ACCEPT
        assert call.headers["x-github-api-version"] == API_VERSION == "2026-03-10"
        assert call.headers["user-agent"]
        assert call.auth == f"Bearer {TOKEN_A}"
        assert not call.redirectable_auth  # unredirected: never forwarded on a redirect
    downloads = [c for c in gh.calls if c.host != API]
    assert len(downloads) == 8 and all(c.auth is None for c in downloads)
    post = next(c for c in api if c.method == "POST")
    assert json.loads(post.body or b"") == {"report_type": "ai_credit",
                                            "start_date": "2026-09-01",
                                            "end_date": "2026-09-02", "send_email": False}
    assert post.headers["content-type"] == "application/json"
    # per_page=100 on list endpoints
    seats = [c for c in api if c.path.endswith("/copilot/billing/seats")]
    assert all(c.query["per_page"] == "100" for c in seats)


def test_every_request_is_a_row_of_the_auth_table(tmp_path: Path) -> None:
    gh = FakeGitHub(valid={TOKEN_A, "github_pat_user"})
    World(gh)
    gh.route("GET", rf"{API}/agents/repos/[^/]+/[^/]+/tasks", ok({"tasks": []}))
    run(tmp_path, gh, kinds=[*HANDOFF_KINDS, "agent_tasks"],
        user_token=TokenSource.from_file(token_file(tmp_path / "u", "github_pat_user")),
        agent_repos=["acme-eng/web"])
    rows = [(row.method, template_regex(p)) for row in AUTH_TABLE if not row.emitted
            for p in row.paths]
    for call in gh.api_calls():
        assert any(m == call.method and rx.fullmatch(call.path) for m, rx in rows), call.path


def test_recorded_files_hold_no_auth_no_signed_url_and_no_header_but_link(
        tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    manifest = run(tmp_path, gh)
    out = tmp_path / "out"
    blob = all_recorded_bytes(out)
    for needle in (TOKEN_A.encode(), b"Bearer", b"uthorization", b"sig=", b"blob.example.net",
                   b"download_urls", b"download_links", b"Set-Cookie", b"x-ratelimit",
                   b"X-GitHub-Api-Version"):
        assert needle not in blob, needle
    for rel in manifest.files():
        if rel.suffix != ".jsonl":
            continue
        for doc in lines(out / rel):
            assert set(doc) <= {"fetched_ms", "request", "response", "headers"}
            assert set(doc["request"]) == {"path", "query"}
            assert set(doc.get("headers", {})) <= {"link"}
            assert doc["fetched_ms"] == NOW_MS or doc["fetched_ms"] > NOW_MS


def test_manifest_is_content_free(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    run(tmp_path, gh)
    text = (tmp_path / "out" / MANIFEST_NAME).read_text()
    for needle in (CANARY_LOGIN, TOKEN_A, "http", "Bearer", "sig", "authorization"):
        assert needle not in text
    doc = json.loads(text)
    assert set(doc) == {"schema", "api_version", "enterprise", "orgs", "since", "until", "kinds",
                        "created_ms", "updated_ms", "units"}
    for unit in doc["units"]:
        assert {"id", "kind", "status", "files"} <= set(unit)
        assert ("window" in unit) or ("day" in unit) or unit["kind"] in ("seats", "config")


def test_pull_is_deterministic(tmp_path: Path) -> None:
    outs = []
    for name in ("a", "b"):
        gh = FakeGitHub()
        World(gh)
        run(tmp_path, gh, out=name)
        root = tmp_path / name
        outs.append({p.relative_to(root).as_posix(): p.read_bytes()
                     for p in sorted(root.rglob("*")) if p.is_file()})
    assert outs[0] == outs[1]


# ---------------------------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------------------------


def test_resume_skips_completed_units(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    first = run(tmp_path, gh)
    gh2 = FakeGitHub()
    World(gh2)
    second = run(tmp_path, gh2, resume=True)
    assert gh2.calls == []
    assert [u.to_json() for u in second.units] == [u.to_json() for u in first.units]
    assert second.created_ms == first.created_ms


def test_a_directory_holding_a_pull_needs_resume(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    run(tmp_path, gh)
    gh2 = FakeGitHub()
    with pytest.raises(UsageError, match="--resume"):
        run(tmp_path, gh2)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.txt").write_text("x")
    with pytest.raises(UsageError, match="not empty"):
        run(tmp_path, gh2, out="other")
    (tmp_path / "file").write_text("x")
    with pytest.raises(UsageError, match="directory"):
        run(tmp_path, gh2, out="file")
    assert gh2.calls == []


def test_resume_refuses_another_enterprise_and_bad_manifest(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    run(tmp_path, gh)
    with pytest.raises(UsageError, match="another enterprise"):
        run(tmp_path, FakeGitHub(), resume=True, enterprise="other-ent")
    (tmp_path / "out" / MANIFEST_NAME).write_text('{"schema": "x"}')
    with pytest.raises(UsageError, match="not a Token Bill pull manifest"):
        run(tmp_path, FakeGitHub(), resume=True)


def test_resume_reruns_units_whose_files_are_missing_and_cleans_partials(
        tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    run(tmp_path, gh)
    out = tmp_path / "out"
    (out / "seats" / "enterprise.jsonl").unlink()
    stale = out / "seats" / ".stale.jsonl.partial"
    stale.write_text("x")
    gh2 = FakeGitHub()
    World(gh2)
    manifest = run(tmp_path, gh2, resume=True)
    assert {c.path for c in gh2.api_calls()} == {f"/enterprises/{ENT}/copilot/billing/seats"}
    assert (out / "seats" / "enterprise.jsonl").is_file() and not stale.exists()
    assert manifest.complete


# ---------------------------------------------------------------------------------------------
# tokens: rotation, expiry, never leaked
# ---------------------------------------------------------------------------------------------


def test_token_file_rotated_mid_pull_is_reread_once_after_a_401(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    path = token_file(tmp_path, TOKEN_A)
    gh = FakeGitHub()
    World(gh)

    def rotate(call: Call) -> None:
        # the 4th API call: the installation token expired and the admin's tooling rotated it
        if call.host == API and len(gh.api_calls()) == 4:
            gh.valid = {TOKEN_B}
            path.write_text(TOKEN_B + "\n")

    gh.hook = rotate
    manifest = run(tmp_path, gh, token=TokenSource.from_file(path))
    assert manifest.complete
    auths = [c.auth for c in gh.api_calls()]
    assert auths[:4] == [f"Bearer {TOKEN_A}"] * 4
    assert auths[4] == f"Bearer {TOKEN_B}"  # the retry after the one re-read
    assert gh.api_calls()[3].path == gh.api_calls()[4].path
    assert set(auths[4:]) == {f"Bearer {TOKEN_B}"}
    log = caplog.text
    assert TOKEN_A not in log and TOKEN_B not in log
    blob = all_recorded_bytes(tmp_path / "out")
    assert TOKEN_A.encode() not in blob and TOKEN_B.encode() not in blob


def test_rotation_between_units_needs_no_401(tmp_path: Path) -> None:
    path = token_file(tmp_path, TOKEN_A)
    gh = FakeGitHub(valid={TOKEN_A, TOKEN_B})
    World(gh)

    def rotate(call: Call) -> None:
        if call.path.endswith("/cost-centers") and len(gh.api_calls()) == 3:
            path.write_text(TOKEN_B + "\n")

    gh.hook = rotate
    run(tmp_path, gh, token=TokenSource.from_file(path))
    auths = [c.auth for c in gh.api_calls()]
    assert auths[3] == f"Bearer {TOKEN_B}"  # read again before the next unit


def test_second_401_raises_token_expired_then_resume_finishes(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    gh = FakeGitHub()
    World(gh)

    def expire(call: Call) -> None:
        if call.host == API and len(gh.api_calls()) == 6:
            gh.valid = set()  # every token is refused from now on

    gh.hook = expire
    with pytest.raises(TokenExpired) as info:
        run(tmp_path, gh)
    assert str(info.value) == RESUME_HINT
    assert info.value.unit_id == "seats/enterprise"
    manifest = Manifest.load(tmp_path / "out" / MANIFEST_NAME)
    done = {u.id for u in manifest.units if u.status == "complete"}
    assert done == {"config/budgets", "config/cost_centers", f"config/org_billing/{ORG}"}
    stopped = manifest.unit("seats/enterprise")
    assert stopped is not None and stopped.status == "incomplete"
    assert stopped.reason == "token_expired" and stopped.files == ()
    assert not list((tmp_path / "out").rglob(".*.partial"))
    # exactly one re-read: the refused request was sent twice, then the pull stopped
    last = gh.api_calls()[-2:]
    assert [c.path for c in last] == [f"/enterprises/{ENT}/copilot/billing/seats"] * 2
    # refresh the token and resume: completed units are not requested again
    gh2 = FakeGitHub(valid={TOKEN_B})
    World(gh2)
    (tmp_path / "secrets" / "gh-token").write_text(TOKEN_B + "\n")
    final = run(tmp_path, gh2, token=TokenSource.from_file(tmp_path / "secrets" / "gh-token"),
                resume=True)
    assert final.complete
    requested = {c.path for c in gh2.api_calls()}
    assert f"/enterprises/{ENT}/settings/billing/budgets" not in requested
    assert f"/orgs/{ORG}/copilot/billing" not in requested
    assert f"/enterprises/{ENT}/copilot/billing/seats" in requested
    for text in (caplog.text, all_recorded_bytes(tmp_path / "out").decode("utf-8", "replace")):
        assert TOKEN_A not in text and TOKEN_B not in text


def test_token_file_0644_is_refused_before_any_request(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    loose = TokenSource.from_file(token_file(tmp_path, TOKEN_A, mode=0o644))
    with pytest.raises(UsageError, match="private") as info:
        run(tmp_path, gh, token=loose)
    assert TOKEN_A not in str(info.value)
    assert gh.calls == []


def test_env_token_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_TEST_GH_TOKEN", TOKEN_A)
    gh = FakeGitHub()
    World(gh)
    manifest = run(tmp_path, gh, token=TokenSource.from_env("TB_TEST_GH_TOKEN"),
                   kinds=["seats"])
    assert manifest.complete
    monkeypatch.delenv("TB_TEST_GH_TOKEN")
    with pytest.raises(UsageError, match="not set"):
        run(tmp_path, gh, out="o2", token=TokenSource.from_env("TB_TEST_GH_TOKEN"),
            kinds=["seats"])


# ---------------------------------------------------------------------------------------------
# failures of one unit
# ---------------------------------------------------------------------------------------------


def test_403_marks_the_unit_forbidden_and_the_pull_goes_on(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    gh = FakeGitHub()
    World(gh)
    gh.route("GET", rf"{API}/enterprises/{ENT}/copilot/billing/seats",
             err(403, {"message": "Resource not accessible by integration"}))
    manifest = run(tmp_path, gh, kinds=["seats", "config"])
    unit = manifest.unit("seats/enterprise")
    assert unit is not None and unit.status == "forbidden" and unit.reason == "http_403"
    assert unit.endpoint == "GET /enterprises/{e}/copilot/billing/seats"
    assert manifest.unit(f"seats/org/{ORG}").status == "complete"  # type: ignore[union-attr]
    assert not manifest.complete and manifest.forbidden == (unit,)
    assert "read:enterprise" in caplog.text and "path B" in caplog.text
    with pytest.raises(GateFailed, match="re-run with --keep-raw DIR to resume"):
        require_complete(manifest, keep_raw=False)
    with pytest.raises(GateFailed, match="--resume"):
        require_complete(manifest, keep_raw=True)


def test_require_complete_passes_a_complete_pull(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    require_complete(run(tmp_path, gh, kinds=["seats"]), keep_raw=False)
    assert KEEP_RAW_HINT == "re-run with --keep-raw DIR to resume"


def test_http_error_and_bad_json_leave_the_unit_incomplete(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing", ok(b"{not json"))
    gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing/seats", err(422))
    manifest = run(tmp_path, gh, kinds=["seats", "config"])
    assert manifest.unit(f"config/org_billing/{ORG}").reason == "invalid_json"  # type: ignore
    assert manifest.unit(f"seats/org/{ORG}").reason == "http_422"  # type: ignore[union-attr]
    assert [u.id for u in manifest.incomplete] == [f"config/org_billing/{ORG}",
                                                   f"seats/org/{ORG}"]


def test_a_secret_in_a_response_aborts_the_unit(tmp_path: Path,
                                                caplog: pytest.LogCaptureFixture) -> None:
    gh = FakeGitHub()
    World(gh)
    leaked = "ghp_" + "LeAkEdToKeN0123456789abcdefGHIJ"
    gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing",
             ok({"plan_type": "business", "note": f"token {leaked}"}))
    manifest = run(tmp_path, gh, kinds=["config"])
    unit = manifest.unit(f"config/org_billing/{ORG}")
    assert unit is not None and unit.status == "incomplete"
    assert unit.reason == "rejected_github_token" and unit.files == ()
    blob = all_recorded_bytes(tmp_path / "out")
    assert leaked.encode() not in blob and leaked not in caplog.text
    assert not list((tmp_path / "out").rglob(".*"))
    # the cost-center UUIDs of the other units are not "high entropy" secrets
    assert manifest.unit("config/cost_centers").status == "complete"  # type: ignore[union-attr]


def test_the_token_echoed_by_a_response_aborts_the_unit(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    token = "tok" + TOKEN_A[4:]  # not token-shaped: only the literal-value scan can catch it
    gh.valid = {token}
    gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing", ok({"echo": f"x{token}x"}))
    manifest = run(tmp_path, gh, kinds=["config"], orgs=[ORG],
                   token=TokenSource.from_file(token_file(tmp_path, token)))
    assert manifest.unit(f"config/org_billing/{ORG}").reason == "rejected_token"  # type: ignore


def test_signed_urls_in_bodies_are_scrubbed(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing", ok(
        {"plan_type": "business", "report": "https://x.example.net/f?X-Amz-Signature=abc",
         "download_url": "https://x.example.net/g", "avatar": "https://a.example/u/1?v=4"}))
    manifest = run(tmp_path, gh, kinds=["config"])
    doc = lines(tmp_path / "out" / f"config/org_billing/{ORG}.jsonl")[0]
    assert doc["response"] == {"plan_type": "business", "report": "[signed URL removed]",
                               "avatar": "https://a.example/u/1?v=4"}
    assert manifest.complete


def test_an_interrupt_is_recorded_and_propagates(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing/seats", KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, gh, kinds=["seats"])
    manifest = Manifest.load(tmp_path / "out" / MANIFEST_NAME)
    assert manifest.unit(f"seats/org/{ORG}").reason == "interrupted"  # type: ignore[union-attr]
    assert manifest.unit("seats/enterprise").status == "complete"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kw, match", [
    ({"kinds": ["bogus"]}, "choose sources"),
    ({"kinds": []}, "choose sources"),
    ({"kinds": 3}, "list of source kinds"),
    ({"enterprise": "bad slug!"}, "enterprise slug"),
    ({"enterprise": None, "kinds": ["ai_usage"]}, "required for ai_usage"),
    ({"enterprise": None, "orgs": [], "kinds": ["seats"]}, "name the enterprise"),
    ({"orgs": ["no/slash"]}, "organization #1"),
    ({"since": "2026-9-1"}, "since must be"),
    ({"until": "2026-02-30"}, "until must be"),
    ({"since": "2026-09-05", "until": "2026-09-01"}, "not be before"),
    ({"now_ms": -1}, "now_ms"),
    ({"token": "ghp_x"}, "token source"),
    ({"metrics_reports": ["bogus"]}, "metrics reports"),
    ({"since": "2026-10-01", "until": "2026-10-02", "kinds": ["metrics"]}, "nothing to pull"),
])
def test_bad_arguments_are_usage_errors(tmp_path: Path, kw: dict[str, Any], match: str) -> None:
    gh = FakeGitHub()
    args: dict[str, Any] = {"kinds": ["seats"], "enterprise": ENT, "orgs": [ORG],
                            "since": "2026-09-01", "until": "2026-09-02",
                            "token": source(tmp_path), "now_ms": NOW_MS}
    args.update(kw)
    kinds = args.pop("kinds")
    with pytest.raises(UsageError, match=match):
        pull(kinds, out_dir=tmp_path / "out", opener=gh, sleep=lambda s: None, **args)
    assert gh.calls == []


def test_kinds_as_a_comma_string_and_duplicate_orgs(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    manifest = run(tmp_path, gh, kinds="seats, config", orgs=[ORG, ORG.upper()])
    assert manifest.kinds == ("seats", "config") and manifest.orgs == (ORG,)


def test_now_ms_may_be_a_callable(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    manifest = pull(["seats"], enterprise=ENT, orgs=[], since="2026-09-01", until="2026-09-01",
                    token=source(tmp_path), out_dir=tmp_path / "o", opener=gh,
                    sleep=lambda s: None, now_ms=lambda: NOW_MS)
    assert manifest.created_ms == NOW_MS


def test_org_only_pull_without_enterprise(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    manifest = run(tmp_path, gh, enterprise=None, kinds=["seats", "config"])
    assert {u.id for u in manifest.units} == {f"seats/org/{ORG}", f"config/org_billing/{ORG}"}
    assert manifest.enterprise is None and manifest.complete


def test_out_dir_is_created_private(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    manifest = run(tmp_path, gh, out="deep/er/out", kinds=["seats"])
    assert manifest.complete
    for d in ("deep", "deep/er", "deep/er/out"):
        assert stat.S_IMODE((tmp_path / d).stat().st_mode) == 0o700


def test_default_opener_is_tls_verifying() -> None:
    opener = pull_common._default_opener()
    handlers = getattr(opener, "__self__", None).handlers  # type: ignore[union-attr]
    https = [h for h in handlers if h.__class__.__name__ == "HTTPSHandler"]
    assert https and https[0]._context.verify_mode.name == "CERT_REQUIRED"
