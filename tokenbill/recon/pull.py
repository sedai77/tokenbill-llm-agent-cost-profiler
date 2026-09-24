"""Live pull of Anthropic Admin / Analytics pages (SPEC §12.5; opt-in, ``--live`` only).

:func:`pull` records every page of one endpoint over ``[since, until)`` into **one** JSONL file of
recorded-page wrappers ``{"endpoint", "fetched_ms", "response"}`` — the format the ADMIN adapters
read, one file per endpoint and window so a whole window is ingested as one source
(CONTRACT-CHANGE-ADMIN-1 (c)). Behavior:

* stdlib ``urllib`` with TLS verification (``ssl.create_default_context()``), 30 s timeout;
* windows split into chunks of at most 31 days (the APIs' maximum per query); Claude Code Analytics
  is queried one day at a time (its ``starting_at`` is a single day);
* pagination by ``has_more`` / ``next_page`` → ``page`` (verified 2026-09-24 against the Usage &
  Cost, Claude Code Analytics and Enterprise Analytics API references); a missing or repeated
  cursor stops with :class:`SourceError`;
* client-side rate limit (60 requests per minute for Enterprise Analytics);
* retry on HTTP 429 and 5xx with capped exponential backoff (1, 2, 4, … s, at most 60 s) honoring
  ``retry-after`` (seconds or an HTTP date), capped at 60 s; other failures raise
  :class:`SourceError` naming the endpoint and status only.

The key is read from the environment variable *key_env* only — never logged, never written, never in
an exception message; recorded pages carry no request headers. Tests inject ``opener`` and ``sleep``
(the suite's socket guard proves no real network). CUR and GCP exports are files the organization
already produces: Token Bill never calls AWS or GCP APIs. No floats (money module, SPEC §2.4).
"""

from __future__ import annotations

import calendar
import datetime as _dt
import email.utils
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.jsonl import open_private

__all__ = [
    "API_BASE",
    "API_VERSION",
    "CHUNK_DAYS",
    "MAX_PAGES",
    "MAX_RETRIES",
    "PULL_KINDS",
    "RETRY_AFTER_CAP_S",
    "TIMEOUT_S",
    "PullKind",
    "pull",
]

logger = logging.getLogger(__name__)

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
TIMEOUT_S = 30
CHUNK_DAYS = 31
MAX_RETRIES = 6
RETRY_AFTER_CAP_S = 60
MAX_PAGES = 10_000
_FAST_BETA = "fast-mode-2026-02-01"
_USER_AGENT = "tokenbill-recon-pull"


@dataclass(frozen=True, slots=True)
class PullKind:
    """One pullable endpoint: its path, fixed query parameters (``group_by[]`` repeated), how the
    window is expressed (``"range"``: ``starting_at``/``ending_at`` RFC 3339 per ≤ 31-day chunk;
    ``"day"``: ``starting_at=YYYY-MM-DD`` per day), the client-side rate limit (requests per
    minute, None = none) and an ``anthropic-beta`` header."""

    path: str
    params: tuple[tuple[str, str], ...]
    window: str = "range"
    rpm: int | None = None
    beta: str | None = None


def _group_by(*names: str) -> tuple[tuple[str, str], ...]:
    return tuple(("group_by[]", n) for n in names)


#: Pullable endpoints. Person-level groupings (``account_id``, ``service_account_id``, user ids) are
#: never requested: the per-user Enterprise endpoints are rolled up to teams at ingest by ADMIN.
PULL_KINDS: Mapping[str, PullKind] = {
    "usage_report": PullKind(
        "/v1/organizations/usage_report/messages",
        (("bucket_width", "1d"), ("limit", "31"),
         *_group_by("api_key_id", "workspace_id", "model", "service_tier", "context_window",
                    "inference_geo", "speed")),
        beta=_FAST_BETA),
    "cost_report": PullKind(
        "/v1/organizations/cost_report",
        (("bucket_width", "1d"), ("limit", "31"), *_group_by("workspace_id", "description"))),
    "claude_code": PullKind(
        "/v1/organizations/usage_report/claude_code", (("limit", "1000"),), window="day"),
    "enterprise_usage": PullKind(
        "/v1/organizations/analytics/usage_report",
        (("bucket_width", "1d"), ("limit", "31"),
         *_group_by("product", "model", "context_window", "inference_geo", "speed")), rpm=60),
    "enterprise_cost": PullKind(
        "/v1/organizations/analytics/cost_report",
        (("bucket_width", "1d"), ("limit", "31"),
         *_group_by("product", "model", "context_window", "inference_geo", "speed", "cost_type",
                    "token_type")), rpm=60),
    "enterprise_user_usage": PullKind(
        "/v1/organizations/analytics/user_usage_report", (("bucket_width", "1d"),), rpm=60),
    "enterprise_user_cost": PullKind(
        "/v1/organizations/analytics/user_cost_report", (("bucket_width", "1d"),), rpm=60),
}


def _date(value: object, what: str) -> _dt.date:
    if not isinstance(value, str) or len(value) != 10:
        raise UsageError(f"pull: {what} must be a YYYY-MM-DD date")
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        raise UsageError(f"pull: {what} must be a YYYY-MM-DD date") from None


def _read_key(key_env: object) -> str:
    if not isinstance(key_env, str) or not key_env:
        raise UsageError("pull: --admin-key-env names the environment variable holding the key")
    key = os.environ.get(key_env, "")
    if not key.strip():
        raise UsageError(f"pull: environment variable {key_env} is not set")
    if any(ord(c) < 0x21 or ord(c) > 0x7e for c in key):
        raise UsageError(f"pull: the value of {key_env} is not a valid API key")
    return key


def _chunks(kind: PullKind, since: _dt.date, until: _dt.date) -> list[tuple[tuple[str, str], ...]]:
    """The window parameters of every request series of ``[since, until)``."""
    out: list[tuple[tuple[str, str], ...]] = []
    step = 1 if kind.window == "day" else CHUNK_DAYS
    start = since
    while start < until:
        end = min(start + _dt.timedelta(days=step), until)
        if kind.window == "day":
            out.append((("starting_at", start.isoformat()),))
        else:
            out.append((("starting_at", f"{start.isoformat()}T00:00:00Z"),
                        ("ending_at", f"{end.isoformat()}T00:00:00Z")))
        start = end
    return out


def _default_opener() -> Callable[..., Any]:
    handler = urllib.request.HTTPSHandler(context=ssl.create_default_context())
    return urllib.request.build_opener(handler).open


def _call(opener: Any, request: urllib.request.Request) -> Any:
    fn = opener if callable(opener) else getattr(opener, "open", None)
    if not callable(fn):
        raise UsageError("pull: opener must be callable or have an open() method")
    return fn(request, timeout=TIMEOUT_S)


def _retry_after(value: str | None, now_s: int) -> int | None:
    """``retry-after`` in whole seconds (a decimal number of seconds, rounded up, or an HTTP date
    relative to *now_s*), capped at :data:`RETRY_AFTER_CAP_S`; None when absent or malformed."""
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = Decimal(text)
    except InvalidOperation:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None or when.tzinfo is None:
            return None
        delta = calendar.timegm(when.utctimetuple()) - now_s
        return min(max(delta, 0), RETRY_AFTER_CAP_S)
    if not seconds.is_finite() or seconds < 0:
        return None
    whole = int(seconds.to_integral_value(rounding=ROUND_CEILING))
    return min(whole, RETRY_AFTER_CAP_S)


def _fetched_ms(headers: Any) -> int:
    """The response ``Date`` header (deterministic under a recorded opener), else the clock."""
    value = headers.get("date") if headers is not None else None
    if isinstance(value, str):
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when is not None and when.tzinfo is not None:
                return calendar.timegm(when.utctimetuple()) * 1000
        except (TypeError, ValueError, IndexError):
            pass
    return time.time_ns() // 1_000_000


class _Session:
    """One pull: headers, pacing and retries (the key lives only in ``self._headers``)."""

    def __init__(self, name: str, kind: PullKind, key: str, opener: Any,
                 sleep: Callable[[int], object]) -> None:
        self.name = name
        self.kind = kind
        self._headers = {"x-api-key": key, "anthropic-version": API_VERSION,
                         "accept": "application/json", "user-agent": _USER_AGENT}
        if kind.beta:
            self._headers["anthropic-beta"] = kind.beta
        self._key = key
        self.opener = opener
        self.sleep = sleep
        self.requests = 0
        self.interval = -(-60 // kind.rpm) if kind.rpm else 0

    def get(self, params: tuple[tuple[str, str], ...]) -> tuple[str, int]:
        """GET the endpoint with *params*: ``(one-line JSON body, fetched_ms)``."""
        url = f"{API_BASE}{self.kind.path}?{urllib.parse.urlencode(params)}"
        backoff = 1
        for attempt in range(MAX_RETRIES + 1):
            if self.requests and self.interval:
                self.sleep(self.interval)
            self.requests += 1
            request = urllib.request.Request(url, headers=dict(self._headers), method="GET")
            status, headers, body = self._send(request)
            if status == 200:
                return self._page(body), _fetched_ms(headers)
            if status != 429 and not 500 <= status <= 599:
                raise SourceError(f"pull {self.name}: HTTP {status} from {self.kind.path}")
            if attempt == MAX_RETRIES:
                break
            wait = _retry_after(headers.get("retry-after") if headers is not None else None,
                                time.time_ns() // 1_000_000_000)
            delay = min(backoff if wait is None else wait, RETRY_AFTER_CAP_S)
            logger.warning("pull %s: HTTP %d from %s, retrying in %d s", self.name, status,
                           self.kind.path, delay)
            self.sleep(delay)
            backoff = min(backoff * 2, RETRY_AFTER_CAP_S)
        raise SourceError(f"pull {self.name}: HTTP {status} from {self.kind.path} after "
                          f"{MAX_RETRIES} retries")

    def _send(self, request: urllib.request.Request) -> tuple[int, Any, bytes]:
        try:
            response = _call(self.opener, request)
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - closing a failed response never matters
                pass
            return status, headers, b""
        except UsageError:
            raise
        except Exception as exc:  # noqa: BLE001 - network failures carry no key, re-raised bare
            raise SourceError(f"pull {self.name}: request to {self.kind.path} failed "
                              f"({type(exc).__name__})") from None
        try:
            status = getattr(response, "status", None)
            if not isinstance(status, int):
                status = response.getcode()
            headers = getattr(response, "headers", None)
            body = response.read() if status == 200 else b""
        except Exception as exc:  # noqa: BLE001 - a broken response is a source error
            raise SourceError(f"pull {self.name}: unreadable response from {self.kind.path} "
                              f"({type(exc).__name__})") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not isinstance(status, int):
            raise SourceError(f"pull {self.name}: response from {self.kind.path} has no status")
        return status, headers, body if isinstance(body, bytes) else b""

    def _page(self, body: bytes) -> str:
        """The body as one JSON line (validated as a page object, numbers kept verbatim)."""
        try:
            text = body.decode("utf-8")
            page = json.loads(text, parse_float=Decimal, parse_constant=_reject)
        except (UnicodeDecodeError, ValueError):
            raise SourceError(f"pull {self.name}: {self.kind.path} returned invalid JSON") \
                from None
        if not isinstance(page, dict):
            raise SourceError(f"pull {self.name}: {self.kind.path} returned a non-object page")
        # JSON strings cannot hold raw line breaks, so this keeps the document valid on one line
        text = text.replace("\r", " ").replace("\n", " ").strip()
        if self._key in text:
            text = text.replace(self._key, "[redacted]")
        return text


def _reject(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def _pages(session: _Session, window: tuple[tuple[str, str], ...]) -> list[tuple[str, int]]:
    """Every page of one request series (``has_more`` / ``next_page`` → ``page``)."""
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    cursor: str | None = None
    while True:
        params = session.kind.params + window + ((("page", cursor),) if cursor else ())
        body, fetched = session.get(params)
        out.append((body, fetched))
        page = json.loads(body, parse_float=Decimal)
        if page.get("has_more") is not True:
            return out
        cursor = page.get("next_page")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise SourceError(f"pull {session.name}: has_more without a new next_page cursor")
        seen.add(cursor)
        if len(seen) >= MAX_PAGES:
            raise SourceError(f"pull {session.name}: more than {MAX_PAGES} pages")


def pull(kind: str, *, key_env: str, since: str, until: str, out_dir: Path,
         opener: Callable[..., Any] | None = None,
         sleep: Callable[[int], object] = time.sleep) -> list[Path]:
    """Pull every page of endpoint *kind* (a key of :data:`PULL_KINDS`) over ``[since, until)``
    (UTC dates) into ``out_dir/<kind>_<since>_<until>.jsonl`` (owner-only) and return its path.

    *opener* is ``opener(request, timeout=30)`` or an object with that ``open`` method (default: a
    TLS-verifying ``urllib`` opener); *sleep* receives whole seconds. A missing or empty *key_env*,
    an unknown *kind* or a bad window raises :class:`UsageError`; HTTP and network failures raise
    :class:`SourceError` (no partial file is left behind)."""
    spec = PULL_KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise UsageError(f"pull: unknown kind (choose from {', '.join(sorted(PULL_KINDS))})")
    start, end = _date(since, "since"), _date(until, "until")
    if end <= start:
        raise UsageError("pull: until must be after since")
    key = _read_key(key_env)
    session = _Session(kind, spec, key, opener if opener is not None else _default_opener(),
                       sleep)
    out_dir = Path(out_dir)
    target = out_dir / f"{kind}_{start.isoformat()}_{end.isoformat()}.jsonl"
    partial = target.with_name(target.name + ".partial")
    pages = 0
    try:
        with open_private(partial, "w") as handle:
            for window in _chunks(spec, start, end):
                for body, fetched in _pages(session, window):
                    handle.write(f'{{"endpoint": {json.dumps(spec.path)}, '
                                 f'"fetched_ms": {fetched}, "response": {body}}}\n')
                    pages += 1
        os.replace(partial, target)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    logger.info("pull %s: %d page(s) for %s..%s", kind, pages, start.isoformat(), end.isoformat())
    return [target]
