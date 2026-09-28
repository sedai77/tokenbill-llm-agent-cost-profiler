"""The HTTP layer: ``Link`` / ``has_next_page`` pagination, secondary-rate-limit backoff with the
injected sleep, 5xx and network retries, the 401 re-read, size limits, downloads and the helpers."""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot import pull_common
from tokenbill.copilot.pull_common import (
    MAX_RETRIES,
    MAX_WAIT_S,
    Client,
    PullError,
    PullForbidden,
    TokenExpired,
    TokenSource,
    _Clock,
    iter_pages,
    link_next,
    rate_limit_wait,
    retry_after_seconds,
)
from tokenbill.core.errors import UsageError

from .helpers import API, DATE, NOW_MS, TOKEN_A, TOKEN_B, Call, FakeGitHub, err, ok, source

PATH = "/enterprises/acme/copilot/billing/seats"
SEATS = rf"{API}{PATH}"


def client(tmp_path: Path, gh: Any, sleeps: list[int] | None = None,
           token: TokenSource | None = None) -> Client:
    sleeps = sleeps if sleeps is not None else []
    return Client(token or source(tmp_path), opener=gh, clock=_Clock(NOW_MS, sleeps.append))


def link(page: int, host: str = API) -> str:
    return f'<https://{host}{PATH}?per_page=100&page={page}>; rel="next"'


# ---------------------------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------------------------


def test_link_pagination_across_three_pages(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, [ok({"seats": [1]}, Link=link(2)), ok({"seats": [2]}, Link=link(3)),
                            ok({"seats": [3]})])
    pages = list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    assert [p.body["seats"] for p in pages] == [[1], [2], [3]]
    assert [p.query for p in pages] == [{"per_page": "100"},
                                        {"per_page": "100", "page": "2"},
                                        {"per_page": "100", "page": "3"}]
    assert pages[0].link == link(2) and pages[2].link is None
    assert [c.query.get("page") for c in gh.calls] == [None, "2", "3"]


def test_link_may_name_another_path_form_on_the_api_host(tmp_path: Path) -> None:
    gh = FakeGitHub()
    other = f'<https://{API}/organizations/77/copilot/billing/seats?page=2>; rel="next"'
    gh.route("GET", SEATS, ok({"seats": [1]}, Link=other))
    gh.route("GET", rf"{API}/organizations/77/copilot/billing/seats", ok({"seats": [2]}))
    pages = list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    assert len(pages) == 2 and gh.calls[1].path == "/organizations/77/copilot/billing/seats"


def test_has_next_page_pagination(tmp_path: Path) -> None:
    gh = FakeGitHub()
    path = "/enterprises/acme/settings/billing/budgets"
    gh.route("GET", rf"{API}{path}", lambda c: ok(
        {"budgets": [c.query.get("page", "1")],
         "has_next_page": c.query.get("page", "1") != "3"}))
    pages = list(iter_pages(client(tmp_path, gh), path, label="GET x"))
    assert [p.body["budgets"] for p in pages] == [["1"], ["2"], ["3"]]


def test_pagination_link_to_another_host_is_refused(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, ok({"seats": [1]}, Link=link(2, host="evil.example.com")))
    with pytest.raises(PullError, match="outside the API host") as info:
        list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    assert info.value.code == "bad_link"
    assert [c.host for c in gh.calls] == [API]  # the token never went there


def test_repeated_page_and_page_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, ok({"seats": [1]}, Link=f'<https://{API}{PATH}?per_page=100>; '
                                                  'rel="next"'))
    with pytest.raises(PullError, match="repeats"):
        list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    monkeypatch.setattr(pull_common, "MAX_PAGES", 2)
    gh2 = FakeGitHub()
    gh2.route("GET", SEATS, lambda c: ok({"seats": []},
                                         Link=link(int(c.query.get("page", "1")) + 1)))
    with pytest.raises(PullError, match="more than 2 pages"):
        list(iter_pages(client(tmp_path, gh2), PATH, label="GET x"))


def test_missing_ok_404_ends_the_pages(tmp_path: Path) -> None:
    gh = FakeGitHub()
    assert list(iter_pages(client(tmp_path, gh), PATH, label="GET x", missing_ok=True)) == []
    with pytest.raises(PullError) as info:
        list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    assert info.value.code == "http_404"


# ---------------------------------------------------------------------------------------------
# rate limits and retries
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer, wait", [
    (err(403, {"message": "You have exceeded a secondary rate limit"}, retry_after="5"), 5),
    (err(429, retry_after="120"), MAX_WAIT_S),
    (err(429, retry_after="0"), 1),
    (err(403, x_ratelimit_remaining="0", x_ratelimit_reset=str(NOW_MS // 1000 + 30)), 30),
    (err(403, x_ratelimit_remaining="0", x_ratelimit_reset=str(NOW_MS // 1000 + 3600)),
     MAX_WAIT_S),
    (err(403, x_ratelimit_remaining="0"), MAX_WAIT_S),
    (err(429), MAX_WAIT_S),
    (err(403, {"message": "secondary rate limit"}), MAX_WAIT_S),
    (err(502), 1),
])
def test_secondary_rate_limits_are_honored_with_the_injected_sleep(
        tmp_path: Path, answer: Any, wait: int) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, [answer, ok({"seats": []})])
    sleeps: list[int] = []
    pages = list(iter_pages(client(tmp_path, gh, sleeps), PATH, label="GET x"))
    assert len(pages) == 1 and sleeps == [wait] and len(gh.calls) == 2


def test_retries_are_bounded(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, err(429, retry_after="7"))
    sleeps: list[int] = []
    with pytest.raises(PullError, match="after 6 retries") as info:
        list(iter_pages(client(tmp_path, gh, sleeps), PATH, label="GET x"))
    assert info.value.code == "rate_limited"
    assert sleeps == [7] * MAX_RETRIES and len(gh.calls) == MAX_RETRIES + 1


def test_5xx_backoff_doubles_and_caps(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, err(503))
    sleeps: list[int] = []
    with pytest.raises(PullError) as info:
        list(iter_pages(client(tmp_path, gh, sleeps), PATH, label="GET x"))
    assert info.value.code == "http_503" and sleeps == [1, 2, 4, 8, 16, 32]


def test_network_failures_retry_then_fail_without_details(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, urllib.error.URLError("dns failure for secret-host"))
    sleeps: list[int] = []
    with pytest.raises(PullError) as info:
        list(iter_pages(client(tmp_path, gh, sleeps), PATH, label="GET x"))
    assert info.value.code == "network" and "secret-host" not in str(info.value)
    assert len(sleeps) == MAX_RETRIES
    gh2 = FakeGitHub()
    gh2.route("GET", SEATS, [TimeoutError(), ok({"seats": []})])
    assert len(list(iter_pages(client(tmp_path, gh2), PATH, label="GET x"))) == 1


def test_403_without_rate_limit_is_forbidden(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, err(403, {"message": "Must have admin rights"}))
    with pytest.raises(PullForbidden) as info:
        list(iter_pages(client(tmp_path, gh), PATH,
                        label="GET /enterprises/{e}/copilot/billing/seats"))
    assert info.value.endpoint == "GET /enterprises/{e}/copilot/billing/seats"
    assert "read:enterprise" in str(info.value)


def test_non_raising_openers_are_handled_alike(tmp_path: Path) -> None:
    gh = FakeGitHub(raise_http_errors=False)
    gh.route("GET", SEATS, [err(429, retry_after="3"), err(401), ok({"seats": []})])
    sleeps: list[int] = []
    c = client(tmp_path, gh, sleeps)
    assert len(list(iter_pages(c, PATH, label="GET x"))) == 1
    assert sleeps == [3]


def test_401_rereads_once_then_token_expired(tmp_path: Path) -> None:
    gh = FakeGitHub(valid={TOKEN_B})
    gh.route("GET", SEATS, ok({"seats": []}))
    with pytest.raises(TokenExpired):
        list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    assert [c.auth for c in gh.calls] == [f"Bearer {TOKEN_A}"] * 2


def test_refresh_errors_propagate_as_usage_errors(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, ok({"seats": []}))
    c = client(tmp_path, gh, token=TokenSource.from_env("TB_SURELY_UNSET_VAR_X"))
    with pytest.raises(UsageError):
        list(iter_pages(c, PATH, label="GET x"))
    assert gh.calls == []


def test_response_read_without_size_argument(tmp_path: Path) -> None:
    class OldResponse:
        status = 200
        headers = {"Date": DATE}

        def read(self) -> bytes:
            return b'{"seats": []}'

        def getcode(self) -> int:
            return 200

    pages = list(iter_pages(client(tmp_path, lambda r, timeout: OldResponse()), PATH,
                            label="GET x"))
    assert pages[0].body == {"seats": []}


def test_opener_object_with_open_method_and_bad_opener(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", SEATS, ok({"seats": []}))

    class Holder:
        def open(self, request: Any, timeout: int) -> Any:
            return gh(request, timeout)

    assert len(list(iter_pages(client(tmp_path, Holder()), PATH, label="GET x"))) == 1
    with pytest.raises(UsageError, match="opener"):
        list(iter_pages(client(tmp_path, object()), PATH, label="GET x"))


def test_broken_responses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NoStatus:
        status = None

        def getcode(self) -> None:
            return None

        def read(self, n: int = -1) -> bytes:
            return b""

    with pytest.raises(PullError) as info:
        list(iter_pages(client(tmp_path, lambda r, timeout: NoStatus()), PATH, label="GET x"))
    assert info.value.code == "network"

    class Exploding:
        status = 200
        headers = None

        def read(self, n: int = -1) -> bytes:
            raise ConnectionResetError()

    with pytest.raises(PullError):
        list(iter_pages(client(tmp_path, lambda r, timeout: Exploding()), PATH, label="GET x"))
    monkeypatch.setattr(pull_common, "MAX_PAGE_BYTES", 10)
    gh = FakeGitHub()
    gh.route("GET", SEATS, ok({"seats": [1, 2, 3, 4, 5]}))
    with pytest.raises(PullError) as big:
        list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    assert big.value.code == "too_large"


def test_http_error_with_unreadable_body(tmp_path: Path) -> None:
    class Body(io.BytesIO):
        def read(self, *a: Any) -> bytes:
            raise OSError("gone")

    def opener(request: Any, timeout: int) -> Any:
        raise urllib.error.HTTPError(request.full_url, 403, "x", None, Body())  # type: ignore

    with pytest.raises(PullForbidden):
        list(iter_pages(client(tmp_path, opener), PATH, label="GET x"))


# ---------------------------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------------------------


def test_downloads_send_the_token_to_github_hosts_only(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", r"blob\.example\.net/f", ok(b"a,b\n"))
    gh.route("GET", r"github\.com/enterprises/acme/metered_exports/1", ok(b"c,d\n"))
    c = client(tmp_path, gh)
    sink = io.BytesIO()
    c.download("https://blob.example.net/f?sig=abc", sink, label="download 1")
    assert sink.getvalue() == b"a,b\n" and gh.calls[-1].auth is None
    sink = io.BytesIO()
    c.download("https://github.com/enterprises/acme/metered_exports/1", sink, label="d")
    assert sink.getvalue() == b"c,d\n" and gh.calls[-1].auth == f"Bearer {TOKEN_A}"
    assert not gh.calls[-1].redirectable_auth


def test_download_retries_truncate_the_sink_and_errors_hide_the_url(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", r"blob\.example\.net/f", [err(503), ok(b"final")])
    sink = io.BytesIO(b"stale bytes")
    client(tmp_path, gh).download("https://blob.example.net/f?sig=abc", sink, label="dl")
    assert sink.getvalue() == b"final"
    gh.route("GET", r"blob\.example\.net/g", err(403, {"message": "AuthenticationFailed"}))
    with pytest.raises(PullError) as info:
        client(tmp_path, gh).download("https://blob.example.net/g?sig=SECRET", io.BytesIO(),
                                      label="download 2")
    assert "SECRET" not in str(info.value) and "blob" not in str(info.value)
    for bad in ("http://blob.example.net/x", "ftp://x/y", "https://", "https://[::1"):
        with pytest.raises(PullError) as info:
            client(tmp_path, gh).download(bad, io.BytesIO(), label="dl")
        assert info.value.code == "bad_link"


def test_download_404_is_a_failure(tmp_path: Path) -> None:
    gh = FakeGitHub(raise_http_errors=False)
    with pytest.raises(PullError) as info:
        client(tmp_path, gh).download("https://blob.example.net/none", io.BytesIO(), label="d")
    assert info.value.code == "download_failed"


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def test_link_next_variants() -> None:
    assert link_next(None) is None and link_next("") is None
    assert link_next('<https://a/x?page=2>; rel="next", <https://a/x?page=9>; rel="last"') == \
        "https://a/x?page=2"
    assert link_next('<https://a/x?page=9>; rel="last", <https://a/y>; rel=next') == "https://a/y"
    assert link_next('<https://a/z>; rel="prev next"') == "https://a/z"
    assert link_next('<https://a/z>; rel="prev"') is None
    assert link_next("<>; rel=next") is None
    assert link_next("garbage") is None


def test_retry_after_seconds() -> None:
    assert retry_after_seconds(None, 0) is None
    assert retry_after_seconds(" ", 0) is None
    assert retry_after_seconds("5", 0) == 5
    assert retry_after_seconds("1.2", 0) == 2
    assert retry_after_seconds("-1", 0) is None
    assert retry_after_seconds("NaN", 0) is None
    assert retry_after_seconds("1e12", 0) is None
    now = NOW_MS // 1000
    assert retry_after_seconds("Fri, 25 Sep 2026 12:00:30 GMT", now) == 30
    assert retry_after_seconds("Fri, 25 Sep 2026 11:00:00 GMT", now) == 0
    assert retry_after_seconds("tomorrow", now) is None


def test_rate_limit_wait_ignores_other_statuses() -> None:
    assert rate_limit_wait(200, {"retry-after": "5"}, b"", 0) is None
    assert rate_limit_wait(404, {}, b"rate limit", 0) is None
    assert rate_limit_wait(403, {}, b"no", 0) is None
    assert rate_limit_wait(403, {"x-ratelimit-remaining": "0",
                                 "x-ratelimit-reset": "12"}, b"", 100) == 1


def test_date_header_sets_now_for_reset(tmp_path: Path) -> None:
    gh = FakeGitHub()
    reset = str(NOW_MS // 1000 + 3600 + 20)
    gh.route("GET", SEATS, [err(403, x_ratelimit_remaining="0", x_ratelimit_reset=reset,
                                Date="Fri, 25 Sep 2026 13:00:00 GMT"), ok({"seats": []})])
    sleeps: list[int] = []
    list(iter_pages(client(tmp_path, gh, sleeps), PATH, label="GET x"))
    assert sleeps == [20]


def test_hook_records_calls(tmp_path: Path) -> None:
    seen: list[Call] = []
    gh = FakeGitHub(hook=seen.append)
    gh.route("GET", SEATS, ok({"seats": []}))
    list(iter_pages(client(tmp_path, gh), PATH, label="GET x"))
    assert len(seen) == 1
