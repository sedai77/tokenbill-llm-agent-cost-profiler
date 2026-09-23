"""Code-level guards (SPEC §8.9): the no-network socket guard and the ownership tooling.

The no-float lint lives in test_no_float_money.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# ---------- socket guard ----------


def test_non_loopback_connect_is_refused(socket_guard: list[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(RuntimeError) as exc:
        s.connect(("192.0.2.1", 443))  # TEST-NET-1: never routed
    assert "socket guard" in str(exc.value)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(RuntimeError):
        s.connect_ex(("198.51.100.7", 80))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(RuntimeError):
        s.sendto(b"x", ("203.0.113.9", 53))
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 443), timeout=1)
    with pytest.raises(RuntimeError):
        socket.getaddrinfo("api.anthropic.com", 443)
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s, pytest.raises(RuntimeError):
        s.connect(("2001:db8::1", 443, 0, 0))
    assert socket_guard == [
        "connect",
        "connect_ex",
        "sendto",
        "create_connection",
        "getaddrinfo",
        "connect",
    ]
    socket_guard.clear()  # the refusals above were intended


def test_loopback_and_socketpair_are_allowed(socket_guard: list[str]) -> None:
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            conn, _ = server.accept()
            with conn:
                client.sendall(b"hi")
                assert conn.recv(2) == b"hi"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c2:
            assert c2.connect_ex(("localhost", port)) == 0
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo(None, 80)

    async def roundtrip() -> int:
        await asyncio.sleep(0)
        return 7

    assert asyncio.run(roundtrip()) == 7
    assert socket_guard == []


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX on this platform")
def test_unix_sockets_are_allowed(tmp_path: Path, socket_guard: list[str]) -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tb") as short:  # AF_UNIX paths must be short
        path = str(Path(short) / "s")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
    assert socket_guard == []


def test_is_local_host() -> None:
    conftest = sys.modules[
        next(
            name
            for name in sys.modules
            if name.endswith("conftest") and hasattr(sys.modules[name], "is_local_host")
        )
    ]
    is_local = conftest.is_local_host
    for host in (
        "127.0.0.1",
        "127.8.9.10",
        "::1",
        "localhost",
        "LOCALHOST.",
        "",
        None,
        b"127.0.0.1",
        "::ffff:127.0.0.1",
        "fe80::1%lo0".replace("fe80::1", "::1"),
    ):
        assert is_local(host), host
    for host in ("10.0.0.1", "8.8.8.8", "example.com", "::ffff:8.8.8.8", b"\xff", 5, "0.0.0.0"):
        assert not is_local(host), host


# ---------- ownership ----------


def _load_script(name: str):  # noqa: ANN202 - returns a module
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_ownership = _load_script("check_ownership")


def test_glob_semantics() -> None:
    g = check_ownership.glob_to_regex
    assert g("tests/v2/core/**").match("tests/v2/core/a/b.py")
    assert g("tests/v2/core/**").match("tests/v2/core/x.py")
    assert not g("tests/v2/core/**").match("tests/v2/corex/x.py")
    assert g("tokenbill/core/*.py").match("tokenbill/core/x.py")
    assert not g("tokenbill/core/*.py").match("tokenbill/core/sub/x.py")
    assert g("docs/**/*.md").match("docs/a.md") and g("docs/**/*.md").match("docs/a/b/c.md")
    assert g("a?c").match("abc") and not g("a?c").match("a/c")


def test_toml_subset_parser() -> None:
    text = (REPO / "OWNERSHIP.toml").read_text(encoding="utf-8")
    parsed = check_ownership.parse_toml_subset(text)
    try:
        import tomllib
    except ImportError:  # Python 3.10
        tomllib = None
    if tomllib is not None:
        assert parsed == tomllib.loads(text)
    assert parsed["schema"] == "tokenbill/ownership@1"
    assert "F-CORE" in parsed["packages"] and "INTEGRATION" in parsed["packages"]
    tricky = 'a = "x # not a comment"  # comment\n[t.u]\nb = ["1", "2"]\nc = [\n "q\\"r", # c\n]\n'
    assert check_ownership.parse_toml_subset(tricky) == {
        "a": "x # not a comment",
        "t": {"u": {"b": ["1", "2"], "c": ['q"r']}},
    }
    for bad in ("a = 1\n", "a = [\n", "junk\n", 'a = "x"\n[a]\n'):
        with pytest.raises(ValueError):
            check_ownership.parse_toml_subset(bad)


def test_ownership_table_mirrors_appendix_o() -> None:
    own = check_ownership.load_ownership(REPO / "OWNERSHIP.toml")
    assert len(own.packages) == 23  # 22 work packages + INTEGRATION
    assert own.owners_of("tokenbill/core/records.py") == ["F-CORE"]
    assert own.owners_of("tokenbill/core/policy.py") == ["F-SEM"]
    assert own.owners_of("tokenbill/core/testing.py") == ["F-KIT"]
    assert own.owners_of("tokenbill/trace.py") == ["FROZEN"]
    assert own.owners_of("tests/test_cli.py") == ["FROZEN"]
    assert own.owners_of("tests/test_pricing.py") == ["RATES"]
    assert own.owners_of(".github/workflows/ownership.yml") == ["F-CORE"]
    assert own.owners_of(".github/workflows/ci.yml") == ["INTEGRATION"]
    assert own.owners_of("tests/v2/golden/demo.stdout.txt") == ["F-CORE"]
    assert own.owners_of("tokenbill/rates/data/anthropic.json") == ["RATES"]
    assert own.owners_of("tokenbill/plan/templates/tokenbill_session_start.py") == ["PLAN"]
    assert own.owners_of("somewhere/else.txt") == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_every_tracked_file_has_exactly_one_owner() -> None:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError:
        pytest.skip("not a git checkout")
    own = check_ownership.load_ownership(REPO / "OWNERSHIP.toml")
    bad = {f: own.owners_of(f) for f in out.stdout.splitlines() if len(own.owners_of(f)) != 1}
    assert bad == {}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_check_flags_out_of_package_and_frozen_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(REPO / "OWNERSHIP.toml", repo / "OWNERSHIP.toml")
    (repo / "tokenbill").mkdir()
    (repo / "tokenbill" / "trace.py").write_text("frozen\n")
    (repo / "tokenbill" / "pricing.py").write_text("rates\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pkg/F-CORE")
    (repo / "tokenbill" / "core").mkdir()
    (repo / "tokenbill" / "core" / "money.py").write_text("ok\n")
    main = check_ownership.main
    assert main(["--package", "F-CORE", "--base", "main", "--repo", str(repo)]) == 0
    (repo / "tokenbill" / "pricing.py").write_text("changed by the wrong package\n")
    (repo / "tokenbill" / "trace.py").write_text("frozen file edited\n")
    capsys.readouterr()
    assert main(["--package", "F-CORE", "--base", "main", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "outside F-CORE ownership: tokenbill/pricing.py (owned by RATES)" in out
    assert "FROZEN file changed: tokenbill/trace.py" in out
    assert (
        main(["--package", "RATES", "--base", "main", "--repo", str(repo)]) == 1
    )  # frozen still fails
    assert main(["--frozen-only", "--base", "main", "--repo", str(repo)]) == 1
    (repo / "tokenbill" / "trace.py").write_text("frozen\n")
    assert main(["--frozen-only", "--base", "main", "--repo", str(repo)]) == 0
    assert main(["--package", "NOPE", "--base", "main", "--repo", str(repo)]) == 2
    assert main(["--package", "F-CORE", "--base", "no-such-ref", "--repo", str(repo)]) == 2
    with pytest.raises(KeyError):
        check_ownership.check("NOPE", [], check_ownership.load_ownership(repo / "OWNERSHIP.toml"))
