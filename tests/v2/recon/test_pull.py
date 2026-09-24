"""SPEC §12.5 ``recon.pull`` with a fake opener and a fake sleep: pagination, 31-day chunking, the
client-side rate limit, retries capped at 60 s, the key never leaking, and no real network."""

from __future__ import annotations

import email.message
import io
import json
import logging
import os
import urllib.error
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core.errors import SourceError, UsageError
from tokenbill.recon import pull as pull_mod
from tokenbill.recon.pull import PULL_KINDS, pull

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "recon" / "pull"
KEY = "sk-ant-admin01-TESTKEY-9f8e7d6c5b4a"
ENV = "TB_TEST_ADMIN_KEY"
DATE = "Mon, 24 Aug 2026 10:00:00 GMT"
DATE_MS = 1_787_565_600_000


def page(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Response:
    """A minimal ``http.client.HTTPResponse`` stand-in."""

    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self.headers = email.message.Message()
        for k, v in (headers or {"Date": DATE}).items():
            self.headers[k] = v
        self._body = body
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class Opener:
    """Records every request; answers from a script (a list consumed in order, or a function)."""

    def __init__(self, script: list[Any] | Callable[[Any], Any]):
        self.script = script
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: int) -> Any:
        assert timeout == 30
        self.requests.append(request)
        item = self.script(request) if callable(self.script) else self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def params(self, i: int) -> list[tuple[str, str]]:
        return urllib.parse.parse_qsl(urllib.parse.urlsplit(self.requests[i].full_url).query)


def ok(body: bytes | dict, **headers: str) -> Response:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Response(200, data, {"Date": DATE, **headers})


EMPTY = {"data": [], "has_more": False, "next_page": None}


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(ENV, KEY)
    return KEY


def run(kind: str, opener: Any, tmp_path: Path, since: str = "2026-08-10",
        until: str = "2026-08-12", sleeps: list | None = None) -> list[Path]:
    sleeps = sleeps if sleeps is not None else []
    return pull(kind, key_env=ENV, since=since, until=until, out_dir=tmp_path / "pages",
                opener=opener, sleep=sleeps.append)


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_pagination_and_recorded_wrappers(key: str, tmp_path: Path) -> None:
    opener = Opener([ok(page("usage_report_p1.json")), ok(page("usage_report_p2.json"))])
    sleeps: list = []
    (path,) = run("usage_report", opener, tmp_path, sleeps=sleeps)
    assert path.name == "usage_report_2026-08-10_2026-08-12.jsonl"
    assert oct(path.stat().st_mode & 0o777) == "0o600" or os.name == "nt"
    records = lines_of(path)
    assert [r["endpoint"] for r in records] == ["/v1/organizations/usage_report/messages"] * 2
    assert all(r["fetched_ms"] == DATE_MS for r in records)
    assert records[0]["response"]["has_more"] is True
    assert records[1]["response"] == json.loads(page("usage_report_p2.json"))
    first, second = opener.params(0), opener.params(1)
    assert ("starting_at", "2026-08-10T00:00:00Z") in first
    assert ("ending_at", "2026-08-12T00:00:00Z") in first
    assert ("group_by[]", "workspace_id") in first and ("page", "x") not in first
    assert not any(k == "page" for k, _ in first)
    assert ("page", "page_MjAyNi0wOC0xMQ==") in second
    req = opener.requests[0]
    assert req.get_method() == "GET" and req.full_url.startswith("https://api.anthropic.com/")
    assert req.headers["X-api-key"] == KEY and req.headers["Anthropic-version"] == "2023-06-01"
    assert req.headers["Anthropic-beta"] == "fast-mode-2026-02-01"
    assert KEY not in path.read_text() and sleeps == []
    assert not list(path.parent.glob("*.partial"))


def test_thirty_one_day_chunking(key: str, tmp_path: Path) -> None:
    opener = Opener(lambda req: ok(EMPTY))
    run("cost_report", opener, tmp_path, since="2026-01-01", until="2026-03-15")
    windows = [(dict(opener.params(i))["starting_at"], dict(opener.params(i))["ending_at"])
               for i in range(len(opener.requests))]
    assert windows == [("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
                       ("2026-02-01T00:00:00Z", "2026-03-04T00:00:00Z"),
                       ("2026-03-04T00:00:00Z", "2026-03-15T00:00:00Z")]


def test_claude_code_analytics_is_pulled_per_day(key: str, tmp_path: Path) -> None:
    opener = Opener(lambda req: ok(EMPTY))
    (path,) = run("claude_code", opener, tmp_path, since="2026-09-08", until="2026-09-11")
    assert [dict(opener.params(i))["starting_at"] for i in range(3)] == [
        "2026-09-08", "2026-09-09", "2026-09-10"]
    assert len(lines_of(path)) == 3


def test_enterprise_rate_limit_is_sixty_per_minute(key: str, tmp_path: Path) -> None:
    opener = Opener(lambda req: ok(EMPTY))
    sleeps: list = []
    run("enterprise_usage", opener, tmp_path, since="2026-01-01", until="2026-03-01",
        sleeps=sleeps)
    assert len(opener.requests) == 2 and sleeps == [1]
    assert "anthropic-beta" not in {k.lower() for k in opener.requests[0].headers}


def test_retry_after_is_honored_and_capped(key: str, tmp_path: Path) -> None:
    script = [Response(429, headers={"retry-after": "120"}),
              Response(429, headers={"retry-after": "2.5"}),
              Response(503), Response(502),
              ok(EMPTY)]
    sleeps: list = []
    run("cost_report", Opener(script), tmp_path, sleeps=sleeps)
    assert sleeps == [60, 3, 4, 8]      # retry-after wins; else the backoff of that attempt


def test_http_error_exceptions_are_retried(key: str, tmp_path: Path) -> None:
    hdrs = email.message.Message()
    hdrs["retry-after"] = "Mon, 24 Aug 2026 10:00:05 GMT"
    err = urllib.error.HTTPError("https://api.anthropic.com/x", 429, "Too Many Requests", hdrs,
                                 io.BytesIO(b""))
    sleeps: list = []
    run("cost_report", Opener([err, ok(EMPTY)]), tmp_path, sleeps=sleeps)
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 60


def test_retries_are_bounded(key: str, tmp_path: Path) -> None:
    sleeps: list = []
    with pytest.raises(SourceError) as info:
        run("cost_report", Opener(lambda req: Response(500)), tmp_path, sleeps=sleeps)
    assert "HTTP 500" in str(info.value) and KEY not in str(info.value)
    assert sleeps == [1, 2, 4, 8, 16, 32]
    assert not list((tmp_path / "pages").glob("*"))


def test_client_errors_are_not_retried(key: str, tmp_path: Path) -> None:
    sleeps: list = []
    with pytest.raises(SourceError, match="HTTP 401"):
        run("usage_report", Opener([Response(401)]), tmp_path, sleeps=sleeps)
    assert sleeps == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"x": NaN}', b"\xff\xfe"])
def test_malformed_pages_are_source_errors(key: str, tmp_path: Path, body: bytes) -> None:
    with pytest.raises(SourceError):
        run("usage_report", Opener([ok(body)]), tmp_path)


def test_broken_cursors_stop(key: str, tmp_path: Path) -> None:
    looping = {"data": [], "has_more": True, "next_page": "page_same"}
    with pytest.raises(SourceError, match="cursor"):
        run("usage_report", Opener(lambda req: ok(looping)), tmp_path)
    missing = {"data": [], "has_more": True, "next_page": None}
    with pytest.raises(SourceError, match="cursor"):
        run("usage_report", Opener([ok(missing)]), tmp_path)


def test_network_failures_carry_no_key(key: str, tmp_path: Path) -> None:
    with pytest.raises(SourceError) as info:
        run("usage_report", Opener([OSError(f"connection reset while sending {KEY}")]),
            tmp_path)
    assert KEY not in str(info.value) and info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_key_never_in_pages_logs_or_errors(key: str, tmp_path: Path,
                                           caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    echo = {"data": [], "has_more": False, "next_page": None, "note": f"echo {KEY}"}
    script = [Response(429, headers={"retry-after": "1"}), ok(echo)]
    (path,) = run("usage_report", Opener(script), tmp_path)
    blobs = [path.read_text(), caplog.text]
    try:
        run("usage_report", Opener([Response(403)]), tmp_path)
    except SourceError as exc:
        blobs.append(str(exc))
    assert all(KEY not in blob for blob in blobs)
    assert "[redacted]" in blobs[0]


def test_missing_key_env_and_bad_arguments(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    monkeypatch.delenv(ENV, raising=False)
    opener = Opener([])
    with pytest.raises(UsageError, match=ENV):
        run("usage_report", opener, tmp_path)
    monkeypatch.setenv(ENV, "   ")
    with pytest.raises(UsageError):
        run("usage_report", opener, tmp_path)
    monkeypatch.setenv(ENV, "sk-bad\nX-Injected: 1")
    with pytest.raises(UsageError) as info:
        run("usage_report", opener, tmp_path)
    assert "Injected" not in str(info.value)
    monkeypatch.setenv(ENV, KEY)
    for kwargs in ({"since": "2026-08-12", "until": "2026-08-10"},
                   {"since": "2026/08/10"}, {"until": "tomorrow"}):
        with pytest.raises(UsageError):
            run("usage_report", opener, tmp_path, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="unknown kind"):
        run("messages", opener, tmp_path)
    with pytest.raises(UsageError):
        pull("usage_report", key_env="", since="2026-08-10", until="2026-08-11",
             out_dir=tmp_path, opener=opener, sleep=lambda s: None)
    assert opener.requests == []


def test_opener_objects_with_open(key: str, tmp_path: Path) -> None:
    class Wrapped:
        def __init__(self) -> None:
            self.inner = Opener(lambda req: ok(EMPTY))

        def open(self, request: Any, timeout: int) -> Any:
            return self.inner(request, timeout)

    wrapped = Wrapped()
    run("enterprise_user_cost", wrapped, tmp_path)
    assert len(wrapped.inner.requests) == 1
    with pytest.raises(UsageError):
        run("cost_report", object(), tmp_path)


def test_socket_guard_proves_no_real_network(key: str, tmp_path: Path,
                                             socket_guard: list[str]) -> None:
    with pytest.raises(SourceError):
        pull("cost_report", key_env=ENV, since="2026-08-10", until="2026-08-11",
             out_dir=tmp_path, sleep=lambda s: None)
    assert socket_guard, "the default opener must reach the (blocked) network"
    socket_guard.clear()


def test_retry_after_parser() -> None:
    now = DATE_MS // 1000
    assert pull_mod._retry_after(None, now) is None
    assert pull_mod._retry_after("7", now) == 7
    assert pull_mod._retry_after("0.2", now) == 1
    assert pull_mod._retry_after("-3", now) is None
    assert pull_mod._retry_after("soon", now) is None
    assert pull_mod._retry_after("Mon, 24 Aug 2026 10:00:30 GMT", now) == 30
    assert pull_mod._retry_after("Mon, 24 Aug 2026 11:00:00 GMT", now) == 60
    assert pull_mod._retry_after("Mon, 24 Aug 2026 09:00:00 GMT", now) == 0
    assert pull_mod._retry_after("Infinity", now) is None


def test_every_kind_is_an_admin_or_analytics_endpoint() -> None:
    for name, spec in PULL_KINDS.items():
        assert spec.path.startswith("/v1/organizations/"), name
        assert not {v for k, v in spec.params if k == "group_by[]"} & {
            "account_id", "service_account_id", "user_id", "claude_tag_user_id"}


@pytest.mark.gate
def test_recorded_pages_read_with_the_admin_adapters(key: str, tmp_path: Path) -> None:
    admin = pytest.importorskip("tokenbill.adapters.anthropic_admin")
    from tokenbill.core.ids import key_id
    from tokenbill.core.types import IngestOptions
    from tokenbill.recon.reconcile import reconcile

    from .helpers import PRICER, TODAY
    usage_opener = Opener([ok(page("usage_report_p1.json")), ok(page("usage_report_p2.json"))])
    (usage_path,) = run("usage_report", usage_opener, tmp_path)
    (cost_path,) = run("cost_report", Opener([ok(page("cost_report_p1.json"))]), tmp_path)
    name_key = bytes(range(32))
    opts = IngestOptions(identity_mode="central-ingest", name_key=name_key,
                         name_key_id=key_id(name_key), now_ms=DATE_MS + 40 * 86_400_000)
    usage = admin.UsageReportAdapter().read(usage_path, opts)
    cost = admin.CostReportAdapter().read(cost_path, opts)
    assert len(usage.aggregates) == 2 and len(cost.cost_lines) == 8
    assert not usage.quarantined and not cost.quarantined
    report = reconcile([], usage.aggregates, cost.cost_lines, PRICER, today=TODAY)
    assert {c.channel: c.verdict for c in report.channels} == {"anthropic_api": "reconciled"}
    assert report.rate_card_error == ("0", "0", "0")
