"""``collect claude-code`` / ``collect claude-code-headless`` end to end (SPEC §5.3, §5.4, §5.12):
content-free trace@2 ``usage`` files, r_/c_ principals only, incremental, CI attribution."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY

from .helpers import CC_HEADLESS, CC_PROJECTS, principals, read_trace2, run_main

pytestmark = pytest.mark.gate
pytest.importorskip("tokenbill.adapters.cc_collect")
pytest.importorskip("tokenbill.adapters.trace_v2")

KEY_HEX = bytes(range(32)).hex()


def _key(tmp_path: Path) -> Path:
    path = tmp_path / "collection.key"
    path.write_text(KEY_HEX + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _files(out: Path) -> list[Path]:
    return sorted(out.glob("tb-*.jsonl.gz"))


def _collect(tmp_path: Path, projects: Path, *extra: str) -> tuple[int, str, str]:
    return run_main(["collect", "claude-code", "--projects", str(projects), "--out",
                     str(tmp_path / "out"), "--state", str(tmp_path / "state.json"), "--final",
                     *extra])


def test_collect_writes_a_content_free_usage_file(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_DEVICE", "dev-042")
    code, out, err = _collect(tmp_path, CC_PROJECTS, "--principal-ref", "env:TB_DEVICE",
                              "--team", "payments", "--attr", "department=eng")
    assert code == 0, err
    (path,) = _files(tmp_path / "out")
    assert str(path) in out
    assert (path.stat().st_mode & 0o077) == 0
    raw = path.read_bytes()
    records = read_trace2(path)
    blob = json.dumps(records)
    assert CANARY not in blob and CANARY.encode() not in raw
    header = records[0]
    assert header["rec"] == "header" and header["profile"] == "usage"
    assert header["identity_mode"] == "central" and header["attribution"]["team"] == "payments"
    assert {r["rec"] for r in records} <= {"header", "dq", "session", "lane", "request", "event",
                                           "aggregate"}
    found = principals(records)
    assert found == {"r_dev-042"}
    assert all(p.startswith(("r_", "c_")) for p in found)
    sessions = [r["session_key"] for r in records if r["rec"] == "session"]
    assert len(sessions) == len(set(sessions))  # one record per session


def test_collect_two_stage_ships_c_principals(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_DEVICE", "dev-042")
    code, _out, err = _collect(tmp_path, CC_PROJECTS, "--principal-ref", "env:TB_DEVICE",
                               "--identity-mode", "two-stage", "--collection-key-file",
                               str(_key(tmp_path)))
    assert code == 0, err
    records = read_trace2(_files(tmp_path / "out")[0])
    found = principals(records)
    assert found and all(p.startswith("c_") for p in found)
    assert "dev-042" not in json.dumps(records)
    assert records[0]["principal_key_id"] and records[0]["name_key_id"]


def test_collect_refuses_content_and_bad_identities(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    code, _out, err = _collect(tmp_path, CC_PROJECTS, "--content", "full")
    assert code == 2 and "content tier none only" in err
    code, _out, err = _collect(tmp_path, CC_PROJECTS, "--content", "fingerprint")
    assert code == 2
    monkeypatch.setenv("TB_MAIL", "alice@example.com")
    code, _out, err = _collect(tmp_path, CC_PROJECTS, "--principal-ref", "env:TB_MAIL")
    assert code == 2 and "never an email" in err and "alice" not in err
    code, _out, err = _collect(tmp_path, CC_PROJECTS, "--identity-mode", "two-stage")
    assert code == 2 and "--collection-key-file" in err
    code, _out, err = _collect(tmp_path, tmp_path / "missing")
    assert code == 2 and "not found" in err
    code, _out, err = _collect(tmp_path, CC_PROJECTS, "--attr", "colour=red")
    assert code == 2
    assert not (tmp_path / "out").exists() or not _files(tmp_path / "out")


def test_rerunning_collects_only_new_data(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    shutil.copytree(CC_PROJECTS / "-home-dev-alpha", projects / "-home-dev-alpha")
    code, _out, err = _collect(tmp_path, projects)
    assert code == 0, err
    first = _files(tmp_path / "out")
    assert len(first) == 1
    code, out, _ = _collect(tmp_path, projects)
    assert code == 0 and "nothing new" in out
    assert _files(tmp_path / "out") == first
    shutil.copytree(CC_PROJECTS / "-home-dev-beta", projects / "-home-dev-beta")
    code, _out, _ = _collect(tmp_path, projects, "--format", "json")
    assert code == 0
    second = [p for p in _files(tmp_path / "out") if p not in first]
    assert len(second) == 1
    beta_only = tmp_path / "beta"
    code, _o, _e = run_main(["collect", "claude-code", "--projects",
                             str(CC_PROJECTS / "-home-dev-beta"), "--out", str(beta_only),
                             "--state", str(tmp_path / "beta-state.json"), "--final"])
    assert code == 0
    ids = {r["request_id"] for r in read_trace2(second[0]) if r["rec"] == "request"}
    expected = {r["request_id"] for r in read_trace2(_files(beta_only)[0])
                if r["rec"] == "request"}
    alpha = {r["request_id"] for r in read_trace2(first[0]) if r["rec"] == "request"}
    assert ids == expected and not ids & alpha


def test_collect_json_summary(tmp_path: Path) -> None:
    code, out, _ = _collect(tmp_path, CC_PROJECTS, "--format", "json")
    assert code == 0
    doc = json.loads(out)
    assert doc["schema"] == "tokenbill/collect@1" and doc["requests"] > 0
    assert doc["file"].endswith(".jsonl.gz") and doc["identity_mode"] == "central"
    code, out, _ = _collect(tmp_path, CC_PROJECTS, "--format", "json")
    assert json.loads(out)["file"] is None


def test_headless_ci_attribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, value in (("GITHUB_ACTIONS", "true"), ("GITHUB_REPOSITORY", "acme/payments-api"),
                       ("GITHUB_WORKFLOW", "claude-review"), ("GITHUB_RUN_ATTEMPT", "2")):
        monkeypatch.setenv(var, value)
    code, out, err = run_main(["collect", "claude-code-headless", "--in",
                               str(CC_HEADLESS / "claude-execution-output.json"), "--out",
                               str(tmp_path / "ci"), "--collection-key-file",
                               str(_key(tmp_path)), "--team", "platform"])
    assert code == 0, err
    (path,) = _files(tmp_path / "ci")
    records = read_trace2(path)
    blob = json.dumps(records)
    assert CANARY not in blob and "acme/payments-api" not in blob and "claude-review" not in blob
    reqs = [r for r in records if r["rec"] == "request"]
    assert reqs
    for r in reqs:
        attr = r["attribution"]
        assert attr["workload_class"] == "ci"
        assert attr["repo"].startswith("h_")
        assert attr["entrypoint"] == "claude-code-github-action"
        extra = dict(attr["extra"])
        assert extra["workflow"].startswith("h_") and extra["run_attempt"] == "2"
        assert attr["team"] == "platform"
    assert principals(records) == set()  # CI traffic: no principal by default
    assert records[0]["producer"]["adapter"] == "claude-code-headless"


def test_headless_without_a_key_drops_names(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/payments-api")
    code, _out, err = run_main(["collect", "claude-code-headless", "--in",
                                str(CC_HEADLESS / "stream.jsonl"),
                                str(CC_HEADLESS / "result-only.json"), "--out",
                                str(tmp_path / "ci")])
    assert code == 0
    assert "dq.ci_names_unhashed" in err
    records = read_trace2(_files(tmp_path / "ci")[0])
    assert "acme/payments-api" not in json.dumps(records)
    code, _out, err = run_main(["--strict-dq", "collect", "claude-code-headless", "--in",
                                str(CC_HEADLESS / "stream.jsonl"), "--out", str(tmp_path / "c2")])
    assert code == 4


def test_headless_missing_input_is_a_usage_error(tmp_path: Path) -> None:
    code, _out, err = run_main(["collect", "claude-code-headless", "--in",
                                str(tmp_path / "nope.json"), "--out", str(tmp_path / "ci")])
    assert code == 2 and "nope.json" in err


def test_collector_file_ingests_centrally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collector's r_ refs become p_ pseudonyms at central ingest (org key)."""
    pytest.importorskip("tokenbill.store.db")
    from .helpers import init_dir

    monkeypatch.setenv("TB_DEVICE", "dev-042")
    assert _collect(tmp_path, CC_PROJECTS, "--principal-ref", "env:TB_DEVICE")[0] == 0
    config = init_dir(tmp_path / "tb")
    code, out, err = run_main(["--config", str(config), "ingest", "--db", str(tmp_path / "l.db"),
                               str(tmp_path / "out"), "--format", "json"])
    assert code == 0, err
    doc = json.loads(out)
    assert doc["inputs"][0]["adapter"] == "trace@2" and doc["inputs"][0]["quarantined"] == 0
    from tokenbill.store.db import SqliteStore

    store = SqliteStore(tmp_path / "l.db", create=False, read_only=True)
    try:
        people = {r.attribution.principal for r in store.iter_requests()}
    finally:
        store.close()
    assert people and all(p.startswith("p_") for p in people)
