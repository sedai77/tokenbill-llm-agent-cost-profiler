"""SPEC §11.3: the SessionStart hook template — subprocess runs with a temporary HOME (the
acceptance behavior) plus in-process runs of ``main`` for the edge cases."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

TEMPLATE = (Path(__file__).resolve().parents[3] / "tokenbill" / "plan" / "templates"
            / "tokenbill_session_start.py")


def _run(home: Path, payload: object, *, env: dict[str, str] | None = None,
         raw: bytes | None = None) -> subprocess.CompletedProcess:
    data = raw if raw is not None else json.dumps(payload).encode()
    environ = {"HOME": str(home), "PATH": os.environ.get("PATH", ""),
               "PYTHONDONTWRITEBYTECODE": "1"}
    environ.update(env or {})
    return subprocess.run([sys.executable, str(TEMPLATE)], input=data, capture_output=True,
                          env=environ, timeout=60, check=False)


def _resume(session: str = "sess-1", usd: object = 3.2, expired: object = True) -> dict:
    return {"session_id": session, "hook_event_name": "SessionStart", "source": "resume",
            "seconds_since_last_response": 7200, "context_tokens": 400000,
            "prompt_cache_likely_expired": expired, "estimated_cache_write_usd": usd}


def _state(home: Path) -> Path:
    return home / ".cache" / "tokenbill" / "hook_counts.json"


def test_expired_expensive_resume_prints_a_system_message(tmp_path) -> None:
    proc = _run(tmp_path, _resume(usd=3.2))
    assert proc.returncode == 0 and proc.stderr == b""
    out = json.loads(proc.stdout)
    assert set(out) == {"systemMessage"}
    assert "$3.20" in out["systemMessage"]
    assert "/compact to continue this task or /clear for a new one" in out["systemMessage"]
    state = _state(tmp_path)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert "sess-1" not in state.read_text()          # only a hash of the session id


def test_below_threshold_or_not_expired_prints_nothing(tmp_path) -> None:
    for payload in (_resume(usd=0.99), _resume(expired=False), _resume(expired="true"),
                    {"session_id": "s", "source": "startup"}, _resume(usd="3.20"),
                    _resume(usd=True), _resume(usd=-1), _resume(session=""),
                    _resume(session=7)):
        proc = _run(tmp_path, payload)
        assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""


def test_threshold_from_the_environment(tmp_path) -> None:
    proc = _run(tmp_path, _resume(usd=3.2), env={"TOKENBILL_HOOK_THRESHOLD_USD": "5"})
    assert proc.stdout == b""
    proc = _run(tmp_path, _resume(usd=5, session="b"),
                env={"TOKENBILL_HOOK_THRESHOLD_USD": "5"})
    assert "$5.00" in json.loads(proc.stdout)["systemMessage"]
    proc = _run(tmp_path, _resume(usd=1.5, session="c"),
                env={"TOKENBILL_HOOK_THRESHOLD_USD": "banana"})
    assert "$1.50" in json.loads(proc.stdout)["systemMessage"]


def test_malformed_input_prints_nothing_and_exits_zero(tmp_path) -> None:
    for raw in (b"", b"not json", b"[1, 2]", b"\xff\xfe", b'{"prompt_cache_likely_expired":',
                b"NaN", b"{" * 100_000):
        proc = _run(tmp_path, None, raw=raw)
        assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""


def test_frequency_caps(tmp_path) -> None:
    assert _run(tmp_path, _resume(session="a")).stdout
    assert _run(tmp_path, _resume(session="a")).stdout == b""     # once per session
    assert _run(tmp_path, _resume(session="b")).stdout
    assert _run(tmp_path, _resume(session="c")).stdout
    assert _run(tmp_path, _resume(session="d")).stdout == b""     # three per week
    doc = json.loads(_state(tmp_path).read_text())
    assert len(doc["shown"]) == 3 and len(doc["sessions"]) == 3


def test_older_messages_leave_the_weekly_window(tmp_path) -> None:
    now = time.time_ns() // 1_000_000
    week = 7 * 86_400_000
    state = _state(tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"shown": [now - week - 1_000] * 3,
                                 "sessions": {"x": [now - week - 1_000]}}))
    assert _run(tmp_path, _resume(session="new")).stdout
    state.write_text("{corrupt")
    assert _run(tmp_path, _resume(session="again")).stdout


def test_unwritable_state_keeps_the_hook_silent(tmp_path) -> None:
    cache = tmp_path / ".cache"
    cache.write_text("a file where the directory should be")
    proc = _run(tmp_path, _resume())
    assert proc.returncode == 0 and proc.stdout == b""


# ---------------------------------------------------------------------------------------------
# in process (coverage of the template's branches)
# ---------------------------------------------------------------------------------------------


@pytest.fixture()
def hook(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("tb_hook_under_test", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOKENBILL_HOOK_THRESHOLD_USD", raising=False)
    return module


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _main(hook, monkeypatch, payload: object, raw: bytes | None = None) -> str:
    data = raw if raw is not None else json.dumps(payload).encode()
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(data))
    out = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", out)
    assert hook.main() == 0
    return out.getvalue()


def test_in_process_message_and_caps(hook, monkeypatch) -> None:
    got = _main(hook, monkeypatch, _resume(usd=12.345))
    assert "$12.34" in json.loads(got)["systemMessage"]     # half-even to cents
    assert _main(hook, monkeypatch, _resume(usd=12.345)) == ""
    assert _main(hook, monkeypatch, None, raw=b"x" * (hook.MAX_INPUT_BYTES + 1)) == ""
    assert _main(hook, monkeypatch, _resume(usd=10**10)) == ""
    assert _main(hook, monkeypatch, _resume(session="s" * 600)) == ""


def test_in_process_threshold_parsing(hook, monkeypatch) -> None:
    from decimal import Decimal

    for raw, want in (("2.5", Decimal("2.5")), ("-1", hook.DEFAULT_THRESHOLD_USD),
                      ("Infinity", hook.DEFAULT_THRESHOLD_USD), ("x", hook.DEFAULT_THRESHOLD_USD),
                      ("1e12", hook.DEFAULT_THRESHOLD_USD)):
        monkeypatch.setenv("TOKENBILL_HOOK_THRESHOLD_USD", raw)
        assert hook._threshold() == want


def test_in_process_state_edge_cases(hook, monkeypatch, tmp_path) -> None:
    state = tmp_path / ".cache" / "tokenbill" / "hook_counts.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"shown": "nope", "sessions": {"k": "nope", "j": [1, "x"]}}))
    assert _main(hook, monkeypatch, _resume(session="z"))
    state.write_text("[]")
    assert _main(hook, monkeypatch, _resume(session="y"))
    state.write_bytes(b"\xff")
    assert _main(hook, monkeypatch, _resume(session="w"))
    state.write_bytes(b"{" + b" " * (hook.MAX_STATE_BYTES + 1))
    assert hook._load_state(state) == {}

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(hook.json, "dump", boom)
    assert _main(hook, monkeypatch, _resume(session="v")) == ""
    assert not list(state.parent.glob(".*.tmp"))            # the temp file is cleaned up


def test_in_process_unreadable_stdin(hook, monkeypatch) -> None:
    class _Broken:
        @property
        def buffer(self):
            raise OSError("closed")

    monkeypatch.setattr(hook.sys, "stdin", _Broken())
    assert hook._read_input() is None
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(b'{"a": NaN}'))
    assert hook._read_input() == {"a": None}
