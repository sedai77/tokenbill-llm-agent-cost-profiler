"""Live pulls of GitHub Copilot sources: the shared machinery (addendum §5.12, §19.4, §22; CP-PULL).

:func:`pull` records GitHub's Copilot billing, metrics, seats, configuration and (with a user token)
agent-task sources into a directory of files the CP-BILL / CP-ORGDATA adapters read, safely and
resumably. The pulled directory is **raw**: it holds GitHub logins and is never the artifact handed
to the product owner; for the admin handoff CP-WIRE pulls :data:`HANDOFF_KINDS` into
:func:`private_workdir` and runs CP-HANDOFF's export on it.

* **Units.** Work is split into units — one report-export window, one metrics report day, one REST
  page set, one agent-task repository — because GitHub App installation tokens expire after one
  hour. ``manifest.json`` (:class:`Manifest`, no auth data) is rewritten atomically after every
  unit; ``resume=True`` skips completed units (metrics days newer than D-3 are ``provisional`` and
  pulled again; ``not_ready`` / ``incomplete`` / ``forbidden`` units are retried).
* **HTTP.** Every request goes through one injectable *opener* (``opener(request, timeout=30)``,
  default: a TLS-verifying ``urllib`` opener); ``Accept: application/vnd.github+json``,
  ``X-GitHub-Api-Version: 2026-03-10``; ``Link`` pagination with ``per_page=100`` (and the billing
  endpoints' ``has_next_page``); secondary-rate-limit backoff honoring ``retry-after`` /
  ``x-ratelimit-reset`` (at most 60 s per wait, bounded retries) through the injected *sleep*.
* **Auth.** :class:`TokenSource` reads a token from an environment variable or a private token file
  (0600 on POSIX, else :class:`UsageError`) before every unit; a 401 re-reads it once and a second
  401 raises :class:`TokenExpired` (CLI exit 3, "refresh the token and re-run with --resume"). The
  ``Authorization`` header is added as an *unredirected* header and only for GitHub hosts: a signed
  download URL never receives the token. Tokens are never logged, printed, written or put into an
  error message.
* **Recording.** REST pages are written as JSON lines ``{"fetched_ms", "request": {path, query},
  "response": <body>}`` (plus ``"headers": {"link": …}`` when GitHub sent one — the only response
  header ever kept); download links are dropped from bodies and signed URLs scrubbed; report CSVs
  are stored verbatim under their unit id. Before a unit's files are renamed into place,
  ``core.secrets.find_secrets``, the token values and a signed-URL pattern are scanned over every
  line: any hit aborts the unit (its files are deleted). Canonical UUIDs are exempt from the
  ``high_entropy`` detector (see ``tests/v2/copilot_pull/CONTRACT-CHANGE-CP-PULL.md``).

Output is deterministic under an injected opener and sleep: ``fetched_ms`` is ``now_ms`` plus the
seconds slept, JSON is canonical, units and files are sorted.
"""

from __future__ import annotations

import calendar
import contextlib
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any

from tokenbill.core.errors import GateFailed, SourceError, UsageError
from tokenbill.core.jsonl import ACL_WARNING, open_private
from tokenbill.core.secrets import find_secrets

__all__ = [
    "ACCEPT",
    "API_BASE",
    "API_VERSION",
    "AUTH_TABLE",
    "EXPORT_CAP_S",
    "EXPORT_POLL_S",
    "HANDOFF_KINDS",
    "KEEP_RAW_HINT",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "MAX_RETRIES",
    "MAX_WAIT_S",
    "PER_PAGE",
    "PULL_KINDS",
    "P_AGENT_TASK",
    "P_AGENT_TASKS",
    "P_AI_CREDIT_USAGE",
    "P_BUDGETS",
    "P_COST_CENTERS",
    "P_ENTERPRISE_METRICS",
    "P_ENTERPRISE_SEATS",
    "P_EXPORT",
    "P_EXPORT_STATUS",
    "P_ORG_BILLING",
    "P_ORG_METRICS",
    "P_ORG_SEATS",
    "P_USAGE",
    "P_USAGE_SUMMARY",
    "P_USER_STATES",
    "RESUME_HINT",
    "STATUSES",
    "TIMEOUT_S",
    "AuthRow",
    "Client",
    "Manifest",
    "Outcome",
    "Page",
    "PlannedUnit",
    "PullError",
    "PullForbidden",
    "PullScope",
    "Response",
    "TokenExpired",
    "TokenSource",
    "UnitContext",
    "UnitEntry",
    "api_path",
    "auth_row_for",
    "auth_table_text",
    "dump_json",
    "envelope_line",
    "is_signed_url",
    "iter_pages",
    "link_next",
    "parse_json",
    "private_workdir",
    "pull",
    "rate_limit_wait",
    "require_complete",
    "retry_after_seconds",
    "scrub",
    "unit_hash",
]

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
API_VERSION = "2026-03-10"
ACCEPT = "application/vnd.github+json"
USER_AGENT = "tokenbill-copilot-pull"
TIMEOUT_S = 30
PER_PAGE = 100
#: Longest single wait (rate limits, 409 back-off, 5xx back-off), seconds.
MAX_WAIT_S = 60
#: Retries of one request after rate limits, 5xx answers or network failures.
MAX_RETRIES = 6
MAX_PAGES = 10_000
MAX_PAGE_BYTES = 64 * 2**20
MAX_DOWNLOAD_BYTES = 16 * 2**30
#: Report-export polling interval and the per-unit cap (then the unit is left incomplete).
EXPORT_POLL_S = 30
EXPORT_CAP_S = 30 * 60
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "tokenbill/copilot-pull@1"
#: Every pullable kind (``copilot pull --sources``).
PULL_KINDS = ("ai_usage", "metered", "summary", "metrics", "seats", "config", "agent_tasks")
#: What ``copilot pull --out FILE.tbx`` pulls: metrics = ``users-1-day``, ``user-teams-1-day``,
#: ``enterprise-1-day``; config = budgets + user-states, cost centers, ``GET
#: /orgs/{org}/copilot/billing``. Agent tasks are never part of a handoff.
HANDOFF_KINDS = ("ai_usage", "metered", "summary", "metrics", "seats", "config")
#: Unit statuses: ``complete`` (skipped by ``--resume``), ``provisional`` (recorded, pulled again on
#: the next run), ``not_ready`` (GitHub has no data yet: metrics 204/404), ``incomplete`` (token
#: expiry, export cap, network or data failure) and ``forbidden`` (HTTP 403).
STATUSES = ("complete", "provisional", "not_ready", "incomplete", "forbidden")
RESUME_HINT = "token expired: refresh the token and re-run with --resume"
KEEP_RAW_HINT = "re-run with --keep-raw DIR to resume"

_CHUNK = 1 << 16
_GZIP_MAGIC = b"\x1f\x8b"
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,4096}\Z")
_MAX_TOKEN_FILE_BYTES = 64 * 1024
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_ORG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
_REPO_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_UNIT_ID_RE = re.compile(r"[a-z_]+(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}){1,4}\Z")
_PART_RE = re.compile(r"\{([a-z_]+)\}")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
#: Query parameters that make a URL a signed (credential-bearing) link.
_SIGNED_PARAMS = frozenset({
    "sig", "signature", "x-amz-signature", "x-amz-credential", "x-amz-security-token",
    "x-goog-signature", "x-goog-credential", "token", "access_token", "jwt", "se", "skoid",
    "sktid", "code", "key-pair-id", "policy"})
_SIGNED_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*[?&](?:sig|signature|x-amz-signature|x-amz-credential|"
    r"x-amz-security-token|x-goog-signature|x-goog-credential|token|access_token|jwt|se|skoid|"
    r"sktid|key-pair-id|policy)=", re.IGNORECASE)
#: Body keys whose values are download links (signed; used at once, never recorded).
_LINK_KEYS = frozenset({"download_urls", "download_links", "download_url", "download_link"})
_SIGNED_REPLACEMENT = "[signed URL removed]"


# ---------------------------------------------------------------------------------------------
# the authentication table (addendum §19.4; printed by ``copilot pull --help``)
# ---------------------------------------------------------------------------------------------


#: Request path templates (``{name}`` parts are percent-encoded by :func:`api_path`); requests are
#: labelled ``"METHOD <template>"`` in messages, so no value (org, repository, id) is ever named.
P_EXPORT = "/enterprises/{e}/settings/billing/reports"
P_EXPORT_STATUS = "/enterprises/{e}/settings/billing/reports/{report_id}"
P_USAGE_SUMMARY = "/enterprises/{e}/settings/billing/usage/summary"
P_USAGE = "/enterprises/{e}/settings/billing/usage"
P_AI_CREDIT_USAGE = "/enterprises/{e}/settings/billing/ai_credit/usage"
P_BUDGETS = "/enterprises/{e}/settings/billing/budgets"
P_USER_STATES = "/enterprises/{e}/settings/billing/budgets/{budget_id}/user-states"
P_COST_CENTERS = "/enterprises/{e}/settings/billing/cost-centers"
P_ENTERPRISE_SEATS = "/enterprises/{e}/copilot/billing/seats"
P_ORG_BILLING = "/orgs/{org}/copilot/billing"
P_ORG_SEATS = "/orgs/{org}/copilot/billing/seats"
P_ENTERPRISE_METRICS = "/enterprises/{e}/copilot/metrics/reports/{report}"
P_ORG_METRICS = "/orgs/{org}/copilot/metrics/reports/{report}"
P_AGENT_TASKS = "/agents/repos/{o}/{r}/tasks"
P_AGENT_TASK = "/agents/repos/{o}/{r}/tasks/{task_id}"


@dataclass(frozen=True, slots=True)
class AuthRow:
    """One call of addendum §19.4 with the credential it needs.

    ``call`` is the call as the admin guide (CP-HANDOFF ``admin_guide_text()``) spells it —
    CP-WIRE's gate test checks that every row's ``call`` occurs in the guide; two rows name a path
    family (``method`` gives the verb). ``paths`` are the request templates the row covers (every
    request a pull makes matches exactly one row). ``kinds`` are the pull kinds that make the call
    (empty for calls Token Bill only *emits* as request files, ``emitted=True``, and never
    executes)."""

    call: str
    method: str
    paths: tuple[str, ...]
    who: str
    classic_scopes: str
    minimal: str
    notes: str
    kinds: tuple[str, ...] = ()
    emitted: bool = False


_BILLING_WHO = ("enterprise admins, billing managers (usage: also org owners), custom role with "
                "enterprise-billing read; GitHub App installation token with \"Enterprise "
                "billing: read\"")
_BILLING_SCOPES = "none documented (roles only); fine-grained PATs not supported"
_BILLING_MINIMAL = "the role's classic PAT (no scope documented) or the App permission"
_ORG_WHO = "org owners; App org permission \"GitHub Copilot Business: read\""


def _billing(call: str, path: str, notes: str, kinds: tuple[str, ...]) -> AuthRow:
    method = call.split(" ", 1)[0]
    return AuthRow(call, method, (path,), _BILLING_WHO, _BILLING_SCOPES, _BILLING_MINIMAL, notes,
                   kinds)


#: The §19.4 table, one call per row (the multi-call rows of §19.4 are split).
AUTH_TABLE: tuple[AuthRow, ...] = (
    _billing("POST /enterprises/{e}/settings/billing/reports", P_EXPORT,
             "report export (ai_credit, detailed) in windows of at most 31 days; one export at a "
             "time (409: wait); whether App read suffices for the POST: VERIFY (§19.5 #30); on "
             "403 use the UI download", ("ai_usage", "metered")),
    _billing("GET /enterprises/{e}/settings/billing/reports/{report_id}", P_EXPORT_STATUS,
             "polled every 30 s for at most 30 min per window; download links used at once and "
             "never stored", ("ai_usage", "metered")),
    _billing("GET /enterprises/{e}/settings/billing/usage/summary", P_USAGE_SUMMARY,
             "per month (changelog 2026-08-26)", ("summary",)),
    _billing("GET /enterprises/{e}/settings/billing/usage", P_USAGE,
             "per month and cost center, including cost_center_id=none", ("summary",)),
    _billing("GET /enterprises/{e}/settings/billing/ai_credit/usage", P_AI_CREDIT_USAGE,
             "per month", ("summary",)),
    _billing("GET /enterprises/{e}/settings/billing/budgets", P_BUDGETS, "budget settings",
             ("config",)),
    _billing("GET /enterprises/{e}/settings/billing/budgets/{budget_id}/user-states",
             P_USER_STATES, "summarized to counts at ingest", ("config",)),
    _billing("GET /enterprises/{e}/settings/billing/cost-centers", P_COST_CENTERS,
             "pools and caps; the ids of the per-cost-center usage", ("config", "summary")),
    AuthRow("GET /enterprises/{e}/copilot/billing/seats", "GET", (P_ENTERPRISE_SEATS,),
            "enterprise owners / billing managers", "manage_billing:copilot or read:enterprise",
            "classic PAT read:enterprise", "App access undocumented: VERIFY (§19.5 #21)",
            ("seats",)),
    AuthRow("GET /orgs/{org}/copilot/billing", "GET", (P_ORG_BILLING,), _ORG_WHO,
            "manage_billing:copilot or read:org", "classic PAT read:org",
            "billing-manager access: VERIFY (§19.5 #32)", ("config",)),
    AuthRow("GET /orgs/{org}/copilot/billing/seats", "GET", (P_ORG_SEATS,), _ORG_WHO,
            "manage_billing:copilot or read:org", "classic PAT read:org",
            "billing-manager access: VERIFY (§19.5 #32)", ("seats",)),
    AuthRow("GET /enterprises/{e}/copilot/metrics/reports/users-1-day?day=", "GET",
            (P_ENTERPRISE_METRICS,),
            "enterprise owners / billing managers; App \"Enterprise Copilot metrics: read\"",
            "manage_billing:copilot or read:enterprise", "classic PAT read:enterprise",
            "also user-teams-1-day and enterprise-1-day, one call per day; signed download "
            "links used at once, never stored; 204/404 = not ready", ("metrics",)),
    AuthRow("/orgs/{org}/copilot/metrics/reports/", "GET", (P_ORG_METRICS,),
            "org owners, fine-grained \"View Organization Copilot Metrics\"", "read:org",
            "classic PAT read:org", "organization variants, pulled only without an enterprise",
            ("metrics",)),
    AuthRow("GET /agents/repos/{o}/{r}/tasks", "GET", (P_AGENT_TASKS, P_AGENT_TASK),
            "GitHub App user token or fine-grained PAT with \"Agent tasks\" read", "—",
            "--github-user-token-env", "installation tokens not supported; per listed repository "
            "(--agent-repos), task pages and tasks with sessions; never part of a handoff; "
            "/agents/tasks is never called", ("agent_tasks",)),
    AuthRow("DELETE /orgs/{org}/copilot/billing/selected_users", "DELETE",
            ("/orgs/{org}/copilot/billing/selected_users",), "org owners",
            "manage_billing:copilot or admin:org", "—",
            "emitted as a request file, never executed; enterprise variants: admin:enterprise or "
            "manage_billing:copilot", emitted=True),
    AuthRow("DELETE /orgs/{org}/copilot/billing/selected_teams", "DELETE",
            ("/orgs/{org}/copilot/billing/selected_teams",), "org owners",
            "manage_billing:copilot or admin:org", "—",
            "emitted as a request file, never executed", emitted=True),
    AuthRow("PUT /enterprises/{e}/copilot/policies/coding_agent", "PUT",
            ("/enterprises/{e}/copilot/policies/coding_agent",), "enterprise owners",
            "admin:enterprise or manage_billing:copilot", "—",
            "emitted as a request file, never executed", emitted=True),
    AuthRow("/enterprises/{e}/settings/billing/", "POST / PATCH",
            (P_BUDGETS, "/enterprises/{e}/settings/billing/budgets/{budget_id}", P_COST_CENTERS,
             "/enterprises/{e}/settings/billing/cost-centers/{cost_center_id}"),
            "enterprise admins, billing managers; App \"Enterprise billing: read and write\"",
            "roles only", "—", "budgets and cost centers: emitted as request files, never executed",
            emitted=True),
)


def auth_row_for(label: str) -> AuthRow | None:
    """The read-only :data:`AUTH_TABLE` row of a request label ``"METHOD <template>"``, or None."""
    method, _, path = label.partition(" ")
    return next((row for row in AUTH_TABLE if not row.emitted and row.method == method
                 and path in row.paths), None)


def auth_table_text() -> str:
    """The :data:`AUTH_TABLE` as plain text for ``copilot pull --help`` (read-only calls first)."""
    lines = ["Calls, who may make them and the minimal credential (addendum §19.4):", ""]
    for emitted in (False, True):
        if emitted:
            lines += ["", "Calls Token Bill only writes as request files (never executed):", ""]
        for row in AUTH_TABLE:
            if row.emitted != emitted:
                continue
            call = row.call if row.call.startswith(row.method + " ") else (
                f"{row.method} {row.call}")
            lines.append(f"  {call}")
            lines.append(f"      who: {row.who}")
            lines.append(f"      classic PAT scopes (OAS): {row.classic_scopes}")
            if not emitted:
                lines.append(f"      Token Bill needs: {row.minimal}")
            lines.append(f"      notes: {row.notes}")
    lines += ["", "Never grant manage_billing:copilot, admin:org, admin:enterprise or any write "
              "permission to the token used for pulls."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------------------------


class TokenExpired(GateFailed):
    """A 401 survived one re-read of the token: the pull stopped cleanly (CLI exit 3).

    The message is :data:`RESUME_HINT`; ``unit_id`` names the unit left incomplete."""

    def __init__(self, message: str = RESUME_HINT, *, unit_id: str | None = None) -> None:
        super().__init__(message)
        self.unit_id = unit_id


class PullError(SourceError):
    """A unit could not be completed; ``code`` is the content-free reason in the manifest."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class PullForbidden(PullError):
    """HTTP 403 without rate-limit signals: a missing permission (unit status ``forbidden``)."""

    def __init__(self, label: str) -> None:
        row = auth_row_for(label)
        need = f" (needs: {row.who}; {row.minimal})" if row is not None else ""
        super().__init__(f"{label}: HTTP 403 — missing permission{need}; see `tokenbill copilot "
                         "pull --help`, or use the UI download of this source (admin guide, "
                         "path B)", "http_403")
        self.endpoint = label


class _NetworkError(Exception):
    """A transport failure (the exception type name only; never a URL or a token)."""


class _TooLarge(Exception):
    pass


class _Rejected(Exception):
    """A recorded file failed the secret / token / signed-URL scan."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------------------------
# token sources
# ---------------------------------------------------------------------------------------------


def _token_of(data: bytes) -> str | None:
    """The token in a file's or variable's bytes: the first non-blank line, stripped; None when it
    is not one run of printable ASCII characters."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in text.splitlines():
        value = line.strip()
        if value:
            return value if _TOKEN_RE.match(value) else None
    return None


class TokenSource:
    """Where a GitHub token comes from: an environment variable (``--github-token-env VAR``) or a
    private token file (``--github-token-file PATH``).

    :meth:`get` reads the value anew on every call (the admin's tooling may rotate the file at any
    time); nothing caches, logs, prints or writes it, and :func:`repr` names only the source. A
    token file must be a regular file without group / other permission bits on POSIX (else
    :class:`UsageError`); on Windows the mode cannot be checked, so :attr:`warnings` carries
    ``dq.windows_acl_not_enforced`` (``core.jsonl`` conventions, D44)."""

    __slots__ = ("_env", "_file", "_platform", "_warnings")

    def __init__(self, *, env: str | None = None, file: str | os.PathLike[str] | None = None,
                 platform: str | None = None) -> None:
        if (env is None) == (file is None):
            raise UsageError("a token source is either an environment variable or a token file")
        if env is not None and (not isinstance(env, str) or not _ENV_NAME_RE.match(env)):
            raise UsageError("--github-token-env names an environment variable (letters, digits "
                             "and _)")
        self._env = env
        try:
            self._file = Path(file) if file is not None else None
        except TypeError:
            raise UsageError("--github-token-file names a file") from None
        self._platform = platform if platform is not None else os.name
        self._warnings: list[str] = []

    @classmethod
    def from_env(cls, name: str) -> TokenSource:
        """A token read from the environment variable *name*."""
        return cls(env=name)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str], *, platform: str | None = None
                  ) -> TokenSource:
        """A token read from the private file *path*."""
        return cls(file=path, platform=platform)

    @property
    def label(self) -> str:
        """``environment variable NAME`` or ``token file NAME`` (never the value)."""
        if self._env is not None:
            return f"environment variable {self._env}"
        assert self._file is not None
        return f"token file {self._file.name}"

    @property
    def warnings(self) -> tuple[str, ...]:
        """Data-quality codes raised while reading (``dq.windows_acl_not_enforced``)."""
        return tuple(self._warnings)

    def __repr__(self) -> str:
        return f"TokenSource({self.label})"

    def get(self) -> str:
        """The current token (re-read now). :class:`UsageError` when it is missing, empty,
        malformed or — for a file — not private."""
        if self._env is not None:
            raw = os.environ.get(self._env)
            if raw is None or not raw.strip():
                raise UsageError(f"{self.label} is not set")
            token = _token_of(raw.encode("utf-8", "surrogateescape"))
        else:
            token = _token_of(self._read_file())
        if token is None:
            raise UsageError(f"{self.label} does not hold a token (one line of printable "
                             "characters)")
        return token

    def _read_file(self) -> bytes:
        path = self._file
        assert path is not None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            raise UsageError(f"{self.label} not found") from None
        except OSError as exc:
            raise UsageError(f"{self.label} is unreadable ({type(exc).__name__})") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UsageError(f"{self.label} is not a regular file")
            if self._platform != "nt":
                if st.st_mode & 0o077:
                    raise UsageError(f"{self.label} must be private (chmod 600): it is readable "
                                     "or writable by others")
            elif ACL_WARNING not in self._warnings:
                self._warnings.append(ACL_WARNING)
                logger.warning("%s: permissions are not checked on Windows (%s); keep it in a "
                               "private folder", self.label, ACL_WARNING)
            if st.st_size > _MAX_TOKEN_FILE_BYTES:
                raise UsageError(f"{self.label} is too large to hold a token")
            chunks: list[bytes] = []
            total = 0
            while total <= _MAX_TOKEN_FILE_BYTES:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except OSError as exc:
            raise UsageError(f"{self.label} is unreadable ({type(exc).__name__})") from None
        finally:
            os.close(fd)
        if total > _MAX_TOKEN_FILE_BYTES:
            raise UsageError(f"{self.label} is too large to hold a token")
        return b"".join(chunks)


# ---------------------------------------------------------------------------------------------
# clock, headers, JSON
# ---------------------------------------------------------------------------------------------


class _Clock:
    """``now_ms`` of the pull plus every second slept through the injected *sleep* (so recorded
    ``fetched_ms`` values are deterministic under a fake sleep)."""

    def __init__(self, now_ms: int, sleep: Callable[[int], object]) -> None:
        self._base = now_ms
        self._slept = 0
        self._sleep = sleep

    def now_ms(self) -> int:
        return self._base + self._slept * 1000

    def sleep(self, seconds: int) -> None:
        seconds = max(0, int(seconds))
        self._sleep(seconds)
        self._slept += seconds


def _headers(raw: Any) -> dict[str, str]:
    """Response headers as a lower-cased dict (first value wins); only used in memory."""
    out: dict[str, str] = {}
    items = getattr(raw, "items", None)
    if not callable(items):
        return out
    try:
        pairs = list(items())
    except Exception:  # noqa: BLE001 - hostile header objects are ignored
        return out
    for key, value in pairs:
        if isinstance(key, str) and isinstance(value, str):
            out.setdefault(key.lower(), value)
    return out


def _date_s(value: str | None) -> int | None:
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or when.tzinfo is None:
        return None
    try:
        return calendar.timegm(when.utctimetuple())
    except (OverflowError, ValueError):
        return None


def retry_after_seconds(value: str | None, now_s: int) -> int | None:
    """``retry-after`` in whole seconds (a number of seconds rounded up, or an HTTP date relative
    to *now_s*, never negative); None when absent or malformed. Not capped (see
    :func:`rate_limit_wait`)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = Decimal(text)
    except (InvalidOperation, ValueError):
        when = _date_s(text)
        return None if when is None else max(when - now_s, 0)
    if not seconds.is_finite() or seconds < 0 or seconds > 10**9:
        return None
    return int(seconds.to_integral_value(rounding=ROUND_CEILING))


def rate_limit_wait(status: int, headers: Mapping[str, str], body: bytes, now_s: int
                    ) -> int | None:
    """Seconds to wait before retrying a secondary-rate-limited request, or None when *status* /
    *headers* / *body* do not signal a rate limit: ``retry-after`` first, then
    ``x-ratelimit-remaining: 0`` with ``x-ratelimit-reset`` (epoch seconds), else 60 s for a 429
    or a 403 whose body mentions a rate limit (GitHub's documented fallback). Always between 1
    and :data:`MAX_WAIT_S`."""
    if status not in (403, 429):
        return None
    after = retry_after_seconds(headers.get("retry-after"), now_s)
    if after is not None:
        return min(max(after, 1), MAX_WAIT_S)
    if headers.get("x-ratelimit-remaining", "").strip() == "0":
        reset = headers.get("x-ratelimit-reset", "").strip()
        if reset.isdigit() and len(reset) <= 12:
            return min(max(int(reset) - now_s, 1), MAX_WAIT_S)
        return MAX_WAIT_S
    if status == 429 or b"rate limit" in body[:4096].lower():
        return MAX_WAIT_S
    return None


_LINK_RE = re.compile(r"<([^<>]*)>([^<]*)")
_REL_RE = re.compile(r";\s*rel\s*=\s*(?:\"([^\"]*)\"|([^\s;,]+))", re.IGNORECASE)


def link_next(value: str | None) -> str | None:
    """The ``rel="next"`` URL of a ``Link`` header, or None."""
    if not value:
        return None
    for m in _LINK_RE.finditer(value[:64 * 1024]):
        for rel in _REL_RE.finditer(m.group(2)):
            if "next" in (rel.group(1) or rel.group(2) or "").lower().split():
                url = m.group(1).strip()
                return url or None
    return None


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token}")


def parse_json(data: bytes, what: str) -> Any:
    """A JSON body with exact numbers (fractions as ``Decimal``); :class:`PullError` otherwise."""
    try:
        text = data.decode("utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        return json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PullError(f"{what}: the response is not valid JSON", "invalid_json") from None


def dump_json(value: Any) -> str:
    """Canonical JSON (sorted keys, no spaces, ASCII) that keeps ``Decimal`` numbers exact;
    ``ValueError`` for anything JSON cannot hold."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite number")
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(dump_json(v) for v in value) + "]"
    if isinstance(value, Mapping):
        items = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("non-string key")
            items.append(json.dumps(key) + ":" + dump_json(value[key]))
        return "{" + ",".join(items) + "}"
    raise ValueError(f"not JSON: {type(value).__name__}")


def is_signed_url(text: str) -> bool:
    """Whether *text* is an http(s) URL carrying a signature / credential query parameter."""
    if not text[:8].lower().startswith(("http://", "https://")):
        return False
    try:
        query = urllib.parse.urlsplit(text).query
        params = urllib.parse.parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return True
    return any(k.lower() in _SIGNED_PARAMS for k, _ in params)


def scrub(value: Any) -> tuple[Any, int]:
    """A copy of a JSON value without download links (``download_urls`` / ``download_links`` keys
    are dropped) and with every signed URL string replaced; returns ``(value, n_removed)``."""
    removed = 0

    def walk(v: Any) -> Any:
        nonlocal removed
        if isinstance(v, str):
            if is_signed_url(v):
                removed += 1
                return _SIGNED_REPLACEMENT
            return v
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if isinstance(k, str) and k.lower() in _LINK_KEYS:
                    removed += 1
                    continue
                out[k] = walk(x)
            return out
        return v

    try:
        return walk(value), removed
    except RecursionError:
        raise PullError("a response is nested too deeply to record", "invalid_json") from None


def envelope_line(path: str, query: Mapping[str, str], body: Any, *, fetched_ms: int,
                  link: str | None = None) -> str:
    """One recorded request as a canonical JSON line (no newline): ``{"fetched_ms", "request":
    {path, query}, "response": body}`` plus ``"headers": {"link": …}`` when given — the only
    response header ever recorded. *body* is scrubbed first."""
    clean, _ = scrub(body)
    doc: dict[str, Any] = {"fetched_ms": fetched_ms,
                           "request": {"path": path, "query": {str(k): str(v)
                                                               for k, v in query.items()}},
                           "response": clean}
    if link:
        doc["headers"] = {"link": link}
    try:
        return dump_json(doc)
    except (ValueError, RecursionError):
        raise PullError("a response cannot be recorded as JSON", "invalid_json") from None


def api_path(template: str, **parts: object) -> str:
    """*template* with every ``{name}`` replaced by the percent-encoded value of ``parts[name]``."""
    def sub(m: re.Match[str]) -> str:
        if m.group(1) not in parts:
            raise ValueError(f"api_path: missing part {m.group(1)}")
        return urllib.parse.quote(str(parts[m.group(1)]), safe="")
    return _PART_RE.sub(sub, template)


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    """A response kept in memory: status, lower-cased headers and the (bounded) body."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        """A response header by (case-insensitive) name."""
        return self.headers.get(name.lower())

    def json(self, what: str) -> Any:
        """The body as JSON with exact numbers (:func:`parse_json`)."""
        return parse_json(self.body, what)


def _default_opener() -> Callable[..., Any]:
    handler = urllib.request.HTTPSHandler(context=ssl.create_default_context())
    return urllib.request.build_opener(handler).open


def _call(opener: Any, request: urllib.request.Request) -> Any:
    fn = opener if callable(opener) else getattr(opener, "open", None)
    if not callable(fn):
        raise UsageError("pull: opener must be callable or have an open() method")
    return fn(request, timeout=TIMEOUT_S)


def _read_limited(response: Any, limit: int, sink: IO[bytes] | None = None) -> bytes:
    """Read a response body (to *sink* when given) up to *limit* bytes; ``_TooLarge`` beyond it.
    Response objects whose ``read`` takes no size argument are read in one call."""
    chunks: list[bytes] = []
    total = 0
    sized = True
    while True:
        if sized:
            try:
                chunk = response.read(_CHUNK)
            except TypeError:
                sized = False
                chunk = response.read()
        else:
            chunk = b""
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise _NetworkError("TypeError")
        total += len(chunk)
        if total > limit:
            raise _TooLarge()
        if sink is not None:
            sink.write(chunk)
        else:
            chunks.append(bytes(chunk))
        if not sized:
            break
    return b"".join(chunks)


class Client:
    """GitHub HTTP for one token source: retries, rate limits, the 401 re-read, downloads.

    :meth:`refresh` re-reads the token (before every unit); the value lives only in this object
    and in the unredirected ``Authorization`` header of requests to GitHub hosts."""

    def __init__(self, token: TokenSource, *, opener: Any, clock: _Clock,
                 api_base: str = API_BASE) -> None:
        self._source = token
        self._token: str | None = None
        self._seen: set[str] = set()
        self._opener = opener
        self.clock = clock
        self.api_base = api_base.rstrip("/")
        parts = urllib.parse.urlsplit(self.api_base)
        self._api_host = parts.netloc.lower()
        host = self._api_host
        self._auth_hosts = frozenset({host, host[4:]} if host.startswith("api.") else {host})
        self.requests = 0

    # ----- token ------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the token anew (:meth:`TokenSource.get`)."""
        self._token = self._source.get()
        self._seen.add(self._token)

    def secrets(self) -> frozenset[str]:
        """Every token value read so far (for the recorded-file scan; memory only)."""
        return frozenset(self._seen)

    # ----- requests ---------------------------------------------------------------------------

    def api(self, method: str, path: str, query: Sequence[tuple[str, str]] = (),
            payload: Mapping[str, Any] | None = None, *, label: str,
            accept: Sequence[int] = (200,), passthrough: Sequence[int] = ()) -> Response:
        """One API request (*path* already encoded) with retries; statuses in *accept* or
        *passthrough* are returned, a 403 raises :class:`PullForbidden`, anything else
        :class:`PullError`."""
        url = self.api_base + path + (("?" + urllib.parse.urlencode(list(query))) if query
                                      else "")
        data = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")

        def build(token: str | None) -> urllib.request.Request:
            request = urllib.request.Request(url, data=data, method=method, headers={
                "Accept": ACCEPT, "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT})
            if data is not None:
                request.add_header("Content-Type", "application/json")
            if token is not None:
                request.add_unredirected_header("Authorization", f"Bearer {token}")
            return request

        resp = self._exchange(build, auth=True, label=label, limit=MAX_PAGE_BYTES)
        if resp.status in accept or resp.status in passthrough:
            return resp
        if resp.status == 403:
            raise PullForbidden(label)
        raise PullError(f"{label}: HTTP {resp.status}", f"http_{resp.status}")

    def download(self, url: str, sink: IO[bytes], *, label: str) -> None:
        """Stream a download link into *sink* (truncated before every attempt). The token is sent
        only to GitHub hosts, never to a signed storage URL; the URL never appears in a message."""
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            raise PullError(f"{label}: malformed download link", "bad_link") from None
        if parts.scheme.lower() != "https" or not parts.netloc:
            raise PullError(f"{label}: download links must be https", "bad_link")
        auth = parts.netloc.lower() in self._auth_hosts

        def build(token: str | None) -> urllib.request.Request:
            request = urllib.request.Request(url, method="GET", headers={
                "Accept": "*/*", "User-Agent": USER_AGENT})
            if token is not None:
                request.add_unredirected_header("Authorization", f"Bearer {token}")
            return request

        resp = self._exchange(build, auth=auth, label=label, limit=MAX_DOWNLOAD_BYTES, sink=sink)
        if resp.status != 200:
            raise PullError(f"{label}: HTTP {resp.status}", "download_failed")

    def same_host(self, url: str, label: str) -> tuple[str, list[tuple[str, str]]]:
        """``(path, query pairs)`` of a pagination link on the API host (else :class:`PullError`:
        the token never follows a link elsewhere)."""
        try:
            parts = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        except ValueError:
            raise PullError(f"{label}: malformed pagination link", "bad_link") from None
        if parts.scheme.lower() != "https" or parts.netloc.lower() != self._api_host:
            raise PullError(f"{label}: a pagination link points outside the API host",
                            "bad_link")
        return parts.path or "/", query

    def _exchange(self, build: Callable[[str | None], urllib.request.Request], *, auth: bool,
                  label: str, limit: int, sink: IO[bytes] | None = None) -> Response:
        reauthed = False
        attempt = 0
        backoff = 1
        while True:
            if auth and self._token is None:
                self.refresh()
            request = build(self._token if auth else None)
            if sink is not None:
                sink.seek(0)
                sink.truncate()
            try:
                status, headers, body = self._send(request, limit=limit, sink=sink)
            except _NetworkError as exc:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise PullError(f"{label}: network failure ({exc})", "network") from None
                logger.warning("%s: network failure (%s), retrying in %d s", label, exc, backoff)
                self.clock.sleep(backoff)
                backoff = min(backoff * 2, MAX_WAIT_S)
                continue
            if status == 401 and auth:
                if reauthed:
                    raise TokenExpired()
                logger.info("%s: HTTP 401, reading the token again", label)
                self.refresh()
                reauthed = True
                continue
            now_s = _date_s(headers.get("date")) or self.clock.now_ms() // 1000
            wait = rate_limit_wait(status, headers, body, now_s)
            if wait is None and 500 <= status <= 599:
                wait = backoff
                backoff = min(backoff * 2, MAX_WAIT_S)
            if wait is None:
                return Response(status, headers, body)
            attempt += 1
            if attempt > MAX_RETRIES:
                raise PullError(f"{label}: HTTP {status} after {MAX_RETRIES} retries",
                                "rate_limited" if status in (403, 429) else f"http_{status}")
            logger.warning("%s: HTTP %d, retrying in %d s", label, status, wait)
            self.clock.sleep(wait)

    def _send(self, request: urllib.request.Request, *, limit: int,
              sink: IO[bytes] | None) -> tuple[int, dict[str, str], bytes]:
        self.requests += 1
        try:
            response = _call(self._opener, request)
        except urllib.error.HTTPError as exc:
            status, headers, body = exc.code, _headers(exc.headers), b""
            if status in (403, 429):
                try:
                    body = exc.read(4096) or b""
                except Exception:  # noqa: BLE001 - an unreadable error body is simply empty
                    body = b""
            with contextlib.suppress(Exception):
                exc.close()
            return int(status), headers, body if isinstance(body, bytes) else b""
        except UsageError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures carry no secret: type only
            raise _NetworkError(type(exc).__name__) from None
        try:
            status = getattr(response, "status", None)
            if not isinstance(status, int):
                status = response.getcode()
            if not isinstance(status, int):
                raise _NetworkError("no status")
            headers = _headers(getattr(response, "headers", None))
            if 200 <= status <= 299:
                body = _read_limited(response, limit, sink)
            else:
                body = _read_limited(response, 1 << 20)
        except _TooLarge:
            raise PullError("a response exceeds the size limit", "too_large") from None
        except _NetworkError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken response is a transport failure
            raise _NetworkError(type(exc).__name__) from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        return status, headers, body


@dataclass(frozen=True)
class Page:
    """One page of a paged GET: the effective query, the parsed body and the ``Link`` header."""

    query: Mapping[str, str]
    body: Any
    link: str | None


def iter_pages(client: Client, path: str, query: Sequence[tuple[str, str]] = (), *,
               label: str, paged: bool = True, missing_ok: bool = False) -> Iterator[Page]:
    """Every page of a GET: ``Link`` ``rel="next"`` (on the API host only) or a body
    ``has_next_page: true`` (next ``page``); ``per_page=100`` when *paged*. A 404 ends the pages
    when *missing_ok*. Repeated pages and more than :data:`MAX_PAGES` raise :class:`PullError`."""
    params = list(query)
    if paged and not any(k == "per_page" for k, _ in params):
        params.append(("per_page", str(PER_PAGE)))
    req_path = path
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for _ in range(MAX_PAGES):
        key = (req_path, tuple(params))
        if key in seen:
            raise PullError(f"{label}: pagination repeats a page", "pagination")
        seen.add(key)
        resp = client.api("GET", req_path, params, label=label,
                          passthrough=(404,) if missing_ok else ())
        if resp.status == 404:
            return
        body = resp.json(label)
        link = resp.header("link")
        yield Page(dict(params), body, link)
        nxt = link_next(link)
        if nxt is not None:
            req_path, params = client.same_host(nxt, label)
            continue
        if isinstance(body, dict) and body.get("has_next_page") is True:
            current = dict(params).get("page", "1")
            number = int(current) if current.isdigit() and len(current) < 9 else 1
            params = [(k, v) for k, v in params if k != "page"] + [("page", str(number + 1))]
            continue
        return
    raise PullError(f"{label}: more than {MAX_PAGES} pages", "pagination")


# ---------------------------------------------------------------------------------------------
# recording: private partial files, scanned before they are renamed into place
# ---------------------------------------------------------------------------------------------


def _safe_rel(rel: str) -> bool:
    if not isinstance(rel, str) or not rel or len(rel) > 512 or "\\" in rel or "\0" in rel:
        return False
    if rel.startswith("/") or re.match(r"[A-Za-z]:", rel):
        return False
    return all(part not in ("", ".", "..") and not part.startswith(".")
               for part in rel.split("/"))


def _partial_of(root: Path, rel: str) -> Path:
    final = root / rel
    return final.with_name("." + final.name + ".partial")


def _scan_file(path: Path, tokens: Iterable[str]) -> str | None:
    """The first reason a recorded file must not be kept (``token``, ``signed_url``, a
    ``core.secrets`` type or ``unreadable``), or None when it is clean. Gzip files are scanned
    decompressed; canonical UUIDs are not ``high_entropy`` secrets."""
    values = [t for t in tokens if t]
    try:
        with open(path, "rb") as probe:
            magic = probe.read(2)
        opener: Callable[..., Any] = gzip.open if magic == _GZIP_MAGIC else open
        with opener(path, "rb") as fh:
            for raw in fh:
                text = raw.decode("utf-8", "replace")
                if any(v in text for v in values):
                    return "token"
                if _SIGNED_URL_RE.search(text):
                    return "signed_url"
                for kind, start, end in find_secrets(text):
                    if kind == "high_entropy" and _UUID_RE.fullmatch(text[start:end]):
                        continue
                    return kind
    except (OSError, EOFError, gzip.BadGzipFile):
        return "unreadable"
    except Exception:  # noqa: BLE001 - zlib errors of a corrupt stream
        return "unreadable"
    return None


class _Recorder:
    """The files of one unit, written to private partial files (``.<name>.partial``, skipped by
    every directory reader) and renamed into place by :meth:`commit` after the scan."""

    def __init__(self, root: Path, unit_id: str, tokens: Callable[[], Iterable[str]]) -> None:
        self.root = root
        self.unit_id = unit_id
        self._tokens = tokens
        self._text: dict[str, IO[str]] = {}
        self._binary: dict[str, tuple[str, str]] = {}   # partial rel → (stem, suffix)
        self._spools: list[Path] = []

    def append(self, rel: str, line: str) -> None:
        """Append one JSON line to the unit file *rel* (relative to the pull directory)."""
        if not _safe_rel(rel):
            raise ValueError("unsafe recorded file name")
        handle = self._text.get(rel)
        if handle is None:
            handle = open_private(_partial_of(self.root, rel), "w")
            self._text[rel] = handle
        handle.write(line + "\n")

    @contextlib.contextmanager
    def binary(self, stem: str, suffix: str) -> Iterator[IO[bytes]]:
        """A private binary sink for ``<stem><suffix>`` (``.gz`` is appended at commit when the
        content is gzip-compressed)."""
        rel = stem + suffix
        if not _safe_rel(rel):
            raise ValueError("unsafe recorded file name")
        handle = open_private(_partial_of(self.root, rel), "wb")
        self._binary[rel] = (stem, suffix)
        try:
            yield handle
        finally:
            handle.close()

    @contextlib.contextmanager
    def spool(self, name: str) -> Iterator[tuple[Path, IO[bytes]]]:
        """A private scratch file (deleted with the unit's partial files)."""
        path = _partial_of(self.root, f"{self.unit_id}_{name}.spool")
        self._spools.append(path)
        handle = open_private(path, "wb")
        try:
            yield path, handle
        finally:
            handle.close()

    def _close(self) -> None:
        for handle in self._text.values():
            with contextlib.suppress(OSError):
                handle.close()

    def discard(self) -> None:
        """Delete every partial and scratch file of the unit."""
        self._close()
        for rel in [*self._text, *self._binary]:
            with contextlib.suppress(OSError):
                _partial_of(self.root, rel).unlink()
        for path in self._spools:
            with contextlib.suppress(OSError):
                path.unlink()
        self._text.clear()
        self._binary.clear()
        self._spools.clear()

    def commit(self, previous: Sequence[str]) -> tuple[str, ...]:
        """Scan every partial file, then rename them into place and delete files the unit's
        previous run left that this run did not write; returns the sorted relative names.
        ``_Rejected`` (files deleted) when a scan hits."""
        self._close()
        for path in self._spools:
            with contextlib.suppress(OSError):
                path.unlink()
        self._spools.clear()
        tokens = tuple(self._tokens())
        finals: dict[str, Path] = {}
        for rel in self._text:
            finals[rel] = _partial_of(self.root, rel)
        for rel, (stem, suffix) in self._binary.items():
            partial = _partial_of(self.root, rel)
            try:
                with open(partial, "rb") as fh:
                    gz = fh.read(2) == _GZIP_MAGIC
            except OSError:
                gz = False
            finals[stem + suffix + (".gz" if gz else "")] = partial
        for partial in finals.values():
            reason = _scan_file(partial, tokens)
            if reason is not None:
                self.discard()
                raise _Rejected(reason)
        for rel, partial in finals.items():
            os.replace(partial, self.root / rel)
        written = tuple(sorted(finals))
        for old in previous:
            if old not in finals and _safe_rel(old):
                target = self.root / old
                with contextlib.suppress(OSError):
                    if target.resolve().is_relative_to(self.root.resolve()):
                        target.unlink()
        self._text.clear()
        self._binary.clear()
        return written


# ---------------------------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------------------------


def _check_day(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise UsageError(f"pull: {what} must be a YYYY-MM-DD date")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise UsageError(f"pull: {what} must be a YYYY-MM-DD date") from None
    return value


@dataclass(frozen=True, slots=True)
class UnitEntry:
    """One unit in ``manifest.json``: ``id``, ``kind``, ``window`` (inclusive dates) or ``day``,
    ``status`` (:data:`STATUSES`), ``files`` (relative to the pull directory), ``repos`` (agent-task
    units), a content-free ``reason`` / ``endpoint`` when not complete, the pending ``export_id``
    of an export left processing (so ``--resume`` polls it instead of exporting again) and
    ``skipped`` (unparseable NDJSON lines dropped)."""

    id: str
    kind: str
    status: str
    files: tuple[str, ...] = ()
    window: tuple[str, str] | None = None
    day: str | None = None
    repos: int | None = None
    reason: str | None = None
    endpoint: str | None = None
    export_id: str | None = None
    skipped: int = 0

    def to_json(self) -> dict[str, Any]:
        """The manifest object of the unit (absent optional fields are omitted)."""
        doc: dict[str, Any] = {"id": self.id, "kind": self.kind, "status": self.status,
                               "files": list(self.files)}
        if self.window is not None:
            doc["window"] = list(self.window)
        if self.day is not None:
            doc["day"] = self.day
        for key in ("repos", "reason", "endpoint", "export_id"):
            value = getattr(self, key)
            if value is not None:
                doc[key] = value
        if self.skipped:
            doc["skipped"] = self.skipped
        return doc

    @classmethod
    def from_json(cls, doc: Any) -> UnitEntry:
        """Validate and read one unit object (``ValueError`` on any deviation)."""
        if not isinstance(doc, dict):
            raise ValueError("unit")
        uid, kind, status = doc.get("id"), doc.get("kind"), doc.get("status")
        if not isinstance(uid, str) or not _UNIT_ID_RE.match(uid) or ".." in uid:
            raise ValueError("unit id")
        if kind not in PULL_KINDS or status not in STATUSES:
            raise ValueError("unit kind/status")
        files = doc.get("files", [])
        if not isinstance(files, list) or not all(_safe_rel(f) for f in files):
            raise ValueError("unit files")
        window = doc.get("window")
        if window is not None:
            if (not isinstance(window, list) or len(window) != 2
                    or not all(isinstance(d, str) for d in window)):
                raise ValueError("unit window")
            window = (window[0], window[1])
        day = doc.get("day")
        repos = doc.get("repos")
        skipped = doc.get("skipped", 0)
        if day is not None and not isinstance(day, str):
            raise ValueError("unit day")
        if repos is not None and (type(repos) is not int or repos < 0):
            raise ValueError("unit repos")
        if type(skipped) is not int or skipped < 0:
            raise ValueError("unit skipped")
        texts = {k: doc.get(k) for k in ("reason", "endpoint", "export_id")}
        if any(v is not None and (not isinstance(v, str) or len(v) > 256)
               for v in texts.values()):
            raise ValueError("unit text")
        export_id = texts["export_id"]
        if export_id is not None and not _ID_RE.match(export_id):
            raise ValueError("unit export id")
        return cls(id=uid, kind=kind, status=status, files=tuple(files), window=window, day=day,
                   repos=repos, reason=texts["reason"], endpoint=texts["endpoint"],
                   export_id=export_id, skipped=skipped)


@dataclass(frozen=True, slots=True)
class Manifest:
    """``manifest.json`` of a pull directory (no auth data, no URLs): the scope and every unit."""

    enterprise: str | None
    orgs: tuple[str, ...]
    since: str
    until: str
    kinds: tuple[str, ...]
    units: tuple[UnitEntry, ...] = ()
    repos: int | None = None
    created_ms: int = 0
    updated_ms: int = 0
    schema: str = MANIFEST_SCHEMA
    api_version: str = API_VERSION

    @property
    def complete(self) -> bool:
        """No unit is ``incomplete`` or ``forbidden`` (``not_ready`` metrics days are fine)."""
        return not any(u.status in ("incomplete", "forbidden") for u in self.units)

    @property
    def incomplete(self) -> tuple[UnitEntry, ...]:
        """Units left ``incomplete`` (token expiry, export cap, failures)."""
        return tuple(u for u in self.units if u.status == "incomplete")

    @property
    def forbidden(self) -> tuple[UnitEntry, ...]:
        """Units GitHub refused with 403 (missing permission)."""
        return tuple(u for u in self.units if u.status == "forbidden")

    def unit(self, unit_id: str) -> UnitEntry | None:
        """The unit with *unit_id*, or None."""
        return next((u for u in self.units if u.id == unit_id), None)

    def files(self, root: Path | None = None, *, kinds: Iterable[str] | None = None
              ) -> list[Path]:
        """The recorded files (of *kinds*, default all), relative or under *root*, sorted."""
        wanted = frozenset(kinds) if kinds is not None else None
        rels = sorted({f for u in self.units if wanted is None or u.kind in wanted
                       for f in u.files})
        base = Path(root) if root is not None else Path()
        return [base / r for r in rels]

    def to_json(self) -> dict[str, Any]:
        """The manifest as a JSON object (units sorted by id)."""
        doc: dict[str, Any] = {
            "schema": self.schema, "api_version": self.api_version,
            "enterprise": self.enterprise, "orgs": list(self.orgs), "since": self.since,
            "until": self.until, "kinds": list(self.kinds), "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
            "units": [u.to_json() for u in sorted(self.units, key=lambda u: u.id)]}
        if self.repos is not None:
            doc["repos"] = self.repos
        return doc

    def dumps(self) -> str:
        """Canonical, indented JSON text of the manifest."""
        return json.dumps(self.to_json(), sort_keys=True, indent=1, ensure_ascii=True) + "\n"

    @classmethod
    def from_json(cls, doc: Any) -> Manifest:
        """Validate and read a manifest object (``ValueError`` on any deviation)."""
        if not isinstance(doc, dict) or doc.get("schema") != MANIFEST_SCHEMA:
            raise ValueError("schema")
        ent = doc.get("enterprise")
        orgs, kinds, units = doc.get("orgs"), doc.get("kinds"), doc.get("units")
        if ent is not None and (not isinstance(ent, str) or not _SLUG_RE.match(ent)):
            raise ValueError("enterprise")
        if not isinstance(orgs, list) or not all(isinstance(o, str) and _ORG_RE.match(o)
                                                 for o in orgs):
            raise ValueError("orgs")
        if not isinstance(kinds, list) or not all(k in PULL_KINDS for k in kinds):
            raise ValueError("kinds")
        if not isinstance(units, list):
            raise ValueError("units")
        since, until = doc.get("since"), doc.get("until")
        if not isinstance(since, str) or not isinstance(until, str):
            raise ValueError("window")
        stamps = [doc.get("created_ms", 0), doc.get("updated_ms", 0)]
        if not all(type(v) is int and v >= 0 for v in stamps):
            raise ValueError("stamps")
        repos = doc.get("repos")
        if repos is not None and (type(repos) is not int or repos < 0):
            raise ValueError("repos")
        entries = tuple(UnitEntry.from_json(u) for u in units)
        if len({u.id for u in entries}) != len(entries):
            raise ValueError("duplicate unit")
        api_version = doc.get("api_version", API_VERSION)
        if not isinstance(api_version, str):
            raise ValueError("api_version")
        return cls(enterprise=ent, orgs=tuple(orgs), since=since, until=until,
                   kinds=tuple(kinds), units=entries, repos=repos, created_ms=stamps[0],
                   updated_ms=stamps[1], api_version=api_version)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """Read ``manifest.json`` (:class:`UsageError` when it is not a pull manifest)."""
        path = Path(path)
        try:
            raw = path.read_bytes()
            if len(raw) > 64 * 2**20:
                raise ValueError("size")
            return cls.from_json(json.loads(raw.decode("utf-8")))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            raise UsageError(f"{path.name} of {path.parent.name} is not a Token Bill pull "
                             "manifest") from None


def _write_manifest(root: Path, manifest: Manifest) -> None:
    tmp = root / f".{MANIFEST_NAME}.partial"
    with open_private(tmp, "w") as fh:
        fh.write(manifest.dumps())
    os.replace(tmp, root / MANIFEST_NAME)


# ---------------------------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PullScope:
    """What one pull covers: entities, the inclusive date window, the UTC date of ``now_ms``,
    the metrics reports and the agent-task repositories (all validated)."""

    enterprise: str | None
    orgs: tuple[str, ...]
    since: str
    until: str
    today: str
    lag_days: int
    metrics_reports: tuple[str, ...]
    agent_repos: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outcome:
    """What a unit runner reports: ``complete`` | ``not_ready`` | ``incomplete``, a content-free
    ``reason``, the pending ``export_id`` and the count of ``skipped`` lines."""

    status: str
    reason: str | None = None
    export_id: str | None = None
    skipped: int = 0


@dataclass(frozen=True)
class PlannedUnit:
    """A unit of work: its id and kind, the runner and how the manifest describes it.

    ``refresh`` units (metrics days newer than D-3) are recorded as ``provisional`` and pulled
    again on the next run; ``user_token`` units run with the user token; ``export`` units are
    report exports (one at a time per account)."""

    id: str
    kind: str
    run: Callable[[UnitContext], Outcome]
    window: tuple[str, str] | None = None
    day: str | None = None
    refresh: bool = False
    user_token: bool = False
    export: bool = False
    repos: int | None = None


@dataclass
class UnitContext:
    """What a unit runner gets: the unit, its client, the recorder, the scope and the unit's entry
    of the previous run (``--resume``)."""

    unit: PlannedUnit
    client: Client
    scope: PullScope
    recorder: _Recorder
    previous: UnitEntry | None = None
    #: The report export this unit started or resumed (kept in the manifest if the unit stops).
    export_id: str | None = None

    def record(self, rel: str, path: str, query: Mapping[str, str], body: Any,
               link: str | None = None) -> None:
        """Record one response body as an envelope line of the unit file *rel*."""
        self.recorder.append(rel, envelope_line(path, query, body,
                                                fetched_ms=self.client.clock.now_ms(), link=link))

    def record_pages(self, rel: str, path: str, query: Sequence[tuple[str, str]] = (), *,
                     label: str, paged: bool = True, missing_ok: bool = False) -> list[Any]:
        """Record every page of a GET (:func:`iter_pages`) under *path* (the logical request
        path: a ``Link`` may name another form of it) and return the bodies."""
        bodies = []
        for page in iter_pages(self.client, path, query, label=label, paged=paged,
                               missing_ok=missing_ok):
            self.record(rel, path, page.query, page.body, page.link)
            bodies.append(page.body)
        return bodies


# ---------------------------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------------------------


def _check_kinds(kinds: Iterable[str]) -> tuple[str, ...]:
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    try:
        items = list(kinds)
    except TypeError:
        raise UsageError("pull: kinds must be a list of source kinds") from None
    if not items or not all(isinstance(k, str) and k in PULL_KINDS for k in items):
        raise UsageError(f"pull: choose sources from {', '.join(PULL_KINDS)}")
    return tuple(k for k in PULL_KINDS if k in items)


def _check_orgs(orgs: Iterable[str]) -> tuple[str, ...]:
    if isinstance(orgs, str):
        orgs = [orgs]
    out: list[str] = []
    seen: set[str] = set()
    for i, org in enumerate(orgs or ()):
        if not isinstance(org, str) or not _ORG_RE.match(org.strip()):
            raise UsageError(f"pull: organization #{i + 1} is not a GitHub organization login")
        name = org.strip()
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return tuple(out)


def _check_repos(repos: Iterable[str]) -> tuple[str, ...]:
    if isinstance(repos, str):
        repos = [repos]
    out: list[str] = []
    seen: set[str] = set()
    for i, repo in enumerate(repos or ()):
        text = repo.strip() if isinstance(repo, str) else ""
        m = _REPO_RE.match(text)
        if m is None or m.group(2) in (".", ".."):
            raise UsageError(f"pull: agent repository #{i + 1} is not owner/repo")
        if text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return tuple(out)


def _now(now_ms: Any) -> int:
    value = now_ms() if callable(now_ms) else now_ms
    if type(value) is not int or value < 0:
        raise UsageError("pull: now_ms must be an int >= 0 (milliseconds since the epoch)")
    return value


def _prepare_out_dir(out: Path, resume: bool) -> Manifest | None:
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise UsageError(f"pull: --out {out.name} must be a directory")
    if not out.exists():
        missing = []
        d = out
        while not d.exists():
            missing.append(d)
            if d.parent == d:
                break
            d = d.parent
        for d in reversed(missing):
            with contextlib.suppress(FileExistsError):
                d.mkdir(mode=0o700)
            if os.name != "nt":
                os.chmod(d, 0o700)
        return None
    manifest_path = out / MANIFEST_NAME
    if manifest_path.is_file():
        if not resume:
            raise UsageError(f"pull: {out.name} already holds a pull; pass --resume to continue "
                             "it, or choose an empty directory")
        return Manifest.load(manifest_path)
    if any(not p.name.startswith(".") for p in out.iterdir()):
        raise UsageError(f"pull: {out.name} is not empty; choose an empty directory for a pull")
    return None


def _clean_partials(out: Path) -> None:
    for path in sorted(out.rglob(".*.partial")):
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                path.unlink()


def pull(kinds: Iterable[str], *, enterprise: str | None, orgs: Sequence[str] = (),
         since: str, until: str, token: TokenSource, user_token: TokenSource | None = None,
         agent_repos: Sequence[str] = (), out_dir: Path, resume: bool = False,
         opener: Any = None, sleep: Callable[[int], object] = time.sleep, now_ms: int,
         metrics_reports: Sequence[str] | None = None) -> Manifest:
    """Pull *kinds* (:data:`PULL_KINDS`) of *enterprise* / *orgs* over ``[since, until]`` (UTC
    dates, inclusive) into *out_dir* and return its :class:`Manifest`.

    *token* serves every GitHub call except agent tasks, which need *user_token* (a GitHub App user
    token or fine-grained PAT) and *agent_repos* (``owner/repo``) — else :class:`UsageError`.
    *metrics_reports* defaults to the handoff reports (``users-1-day``, ``user-teams-1-day``,
    ``enterprise-1-day``; ``repos-1-day`` may be added). *opener* is ``opener(request,
    timeout=30)`` or an object with that ``open`` method; *sleep* receives whole seconds; *now_ms*
    is the run time (``fetched_ms``, D-3, today).

    Raises :class:`UsageError` before any request for bad arguments, a non-private token file or a
    non-empty *out_dir* without *resume*; :class:`TokenExpired` when a 401 survives a re-read (the
    manifest then records the unit as incomplete). Other unit failures are recorded in the
    manifest (``incomplete`` / ``forbidden``) and the pull goes on."""
    from tokenbill.copilot import pull_billing, pull_metrics  # the unit planners

    kinds_t = _check_kinds(kinds)
    ent = enterprise.strip() if isinstance(enterprise, str) else enterprise
    if ent is not None and (not isinstance(ent, str) or not _SLUG_RE.match(ent)):
        raise UsageError("pull: --github-enterprise must be an enterprise slug")
    orgs_t = _check_orgs(orgs)
    start = _check_day(since, "since")
    end = _check_day(until, "until")
    if end < start:
        raise UsageError("pull: until must not be before since")
    now = _now(now_ms)
    if not isinstance(token, TokenSource):
        raise UsageError("pull: a token source (--github-token-env or --github-token-file) is "
                         "required")
    repos_t: tuple[str, ...] = ()
    if "agent_tasks" in kinds_t:
        if not isinstance(user_token, TokenSource):
            raise UsageError("pull: agent tasks need --github-user-token-env (a GitHub App user "
                             "token or fine-grained PAT with \"Agent tasks\" read; installation "
                             "tokens are not supported)")
        repos_t = _check_repos(agent_repos)
        if not repos_t:
            raise UsageError("pull: agent tasks need --agent-repos FILE (owner/repo lines)")
    reports = pull_metrics.check_reports(metrics_reports)
    missing_ent = [k for k in kinds_t if k in pull_billing.ENTERPRISE_KINDS]
    if ent is None and missing_ent:
        raise UsageError("pull: --github-enterprise is required for "
                         + ", ".join(missing_ent))
    if ent is None and not orgs_t and any(k != "agent_tasks" for k in kinds_t):
        raise UsageError("pull: name the enterprise (--github-enterprise) or organizations "
                         "(--github-org)")
    today = _dt.datetime.fromtimestamp(now // 1000, tz=_dt.timezone.utc).date().isoformat()
    from tokenbill.core.facts import copilot_report_lag_days

    scope = PullScope(enterprise=ent, orgs=orgs_t, since=start, until=end, today=today,
                      lag_days=copilot_report_lag_days(), metrics_reports=reports,
                      agent_repos=repos_t)
    plan = [*pull_billing.plan(scope, kinds_t), *pull_metrics.plan(scope, kinds_t)]
    if not plan:
        raise UsageError("pull: nothing to pull in this window (check --since/--until and the "
                         "entities)")
    ids = [u.id for u in plan]
    if len(set(ids)) != len(ids):  # pragma: no cover - planners build unique ids
        raise UsageError("pull: duplicate units")

    out = Path(out_dir)
    prior = _prepare_out_dir(out, bool(resume))
    if prior is not None and prior.enterprise != ent:
        raise UsageError(f"pull: {out.name} holds a pull of another enterprise; choose another "
                         "directory")
    # read every token once before the first request (a non-private file stops here)
    token.get()
    if repos_t:
        assert user_token is not None
        user_token.get()
    if prior is not None:
        _clean_partials(out)

    clock = _Clock(now, sleep)
    the_opener = opener if opener is not None else _default_opener()
    client = Client(token, opener=the_opener, clock=clock)
    user_client = Client(user_token, opener=the_opener, clock=clock) if repos_t else None
    entries: dict[str, UnitEntry] = {u.id: u for u in prior.units} if prior else {}
    created = prior.created_ms if prior else now

    def manifest() -> Manifest:
        return Manifest(enterprise=ent, orgs=orgs_t, since=start, until=end, kinds=kinds_t,
                        units=tuple(sorted(entries.values(), key=lambda u: u.id)),
                        repos=len(repos_t) if repos_t else None, created_ms=created,
                        updated_ms=clock.now_ms())

    def entry(pu: PlannedUnit, status: str, *, files: tuple[str, ...] = (),
              reason: str | None = None, endpoint: str | None = None,
              export_id: str | None = None, skipped: int = 0) -> UnitEntry:
        return UnitEntry(id=pu.id, kind=pu.kind, status=status, files=files, window=pu.window,
                         day=pu.day, repos=pu.repos, reason=reason, endpoint=endpoint,
                         export_id=export_id, skipped=skipped)

    export_blocked = False
    for pu in plan:
        prev = entries.get(pu.id) if resume else None
        if prev is not None and prev.status == "complete" and all(
                (out / f).is_file() for f in prev.files):
            logger.info("unit %s: complete, skipped (--resume)", pu.id)
            continue
        previous_files = entries[pu.id].files if pu.id in entries else ()
        if pu.export and export_blocked:
            entries[pu.id] = entry(pu, "incomplete", files=previous_files,
                                   reason="export_pending",
                                   export_id=prev.export_id if prev else None)
            _write_manifest(out, manifest())
            continue
        unit_client = user_client if pu.user_token else client
        assert unit_client is not None
        recorder = _Recorder(out, pu.id, lambda: client.secrets() | (
            user_client.secrets() if user_client else frozenset()))
        ctx = UnitContext(unit=pu, client=unit_client, scope=scope, recorder=recorder,
                          previous=prev)
        try:
            unit_client.refresh()  # before each unit: a rotated token file is picked up
            outcome = pu.run(ctx)
            if outcome.status == "complete":
                files = recorder.commit(previous_files)
                status = "provisional" if pu.refresh else "complete"
                entries[pu.id] = entry(pu, status, files=files, skipped=outcome.skipped)
            else:
                recorder.discard()  # earlier files of the unit stay until a run replaces them
                entries[pu.id] = entry(pu, outcome.status, files=previous_files,
                                       reason=outcome.reason, export_id=outcome.export_id,
                                       skipped=outcome.skipped)
                if pu.export and (outcome.export_id is not None
                                  or outcome.reason == "export_busy"):
                    export_blocked = True  # one export per account at a time
        except TokenExpired as exc:
            recorder.discard()
            entries[pu.id] = entry(pu, "incomplete", files=previous_files,
                                   reason="token_expired",
                                   export_id=ctx.export_id or (prev.export_id if prev else None))
            _write_manifest(out, manifest())
            exc.unit_id = pu.id
            logger.error("unit %s: %s", pu.id, RESUME_HINT)
            raise
        except PullForbidden as exc:
            recorder.discard()
            entries[pu.id] = entry(pu, "forbidden", files=previous_files, reason=exc.code,
                                   endpoint=exc.endpoint)
            logger.warning("unit %s: %s", pu.id, exc)
        except PullError as exc:
            recorder.discard()
            entries[pu.id] = entry(pu, "incomplete", files=previous_files, reason=exc.code,
                                   export_id=ctx.export_id)
            logger.warning("unit %s: incomplete (%s)", pu.id, exc)
        except _Rejected as exc:
            entries[pu.id] = entry(pu, "incomplete", files=previous_files,
                                   reason=f"rejected_{exc.code}")
            logger.warning("unit %s: a recorded response holds a %s; the unit was not kept",
                           pu.id, exc.code)
        except UsageError:
            recorder.discard()
            _write_manifest(out, manifest())
            raise
        except BaseException:
            recorder.discard()
            entries[pu.id] = entry(pu, "incomplete", files=previous_files, reason="interrupted")
            with contextlib.suppress(Exception):
                _write_manifest(out, manifest())
            raise
        _write_manifest(out, manifest())
        logger.info("unit %s: %s", pu.id, entries[pu.id].status)
    result = manifest()
    _write_manifest(out, result)
    return result


def require_complete(manifest: Manifest, *, keep_raw: bool) -> None:
    """Handoff mode (``--out FILE.tbx``): :class:`GateFailed` (exit 3) when any unit is left
    incomplete or forbidden — the raw directory is never left behind silently, so without
    ``--keep-raw`` the message is :data:`KEEP_RAW_HINT`."""
    if manifest.complete:
        return
    bad = [*manifest.incomplete, *manifest.forbidden]
    reasons = ", ".join(sorted({u.reason or u.status for u in bad}))
    detail = f"{len(bad)} unit(s) not complete ({reasons})"
    if keep_raw:
        raise GateFailed(f"{detail}; the raw directory was kept: re-run with --resume and the "
                         "same --keep-raw DIR")
    raise GateFailed(f"{detail}; {KEEP_RAW_HINT}")


# ---------------------------------------------------------------------------------------------
# the private work directory of the handoff (``copilot pull --out FILE.tbx``)
# ---------------------------------------------------------------------------------------------


def _default_runner(argv: Sequence[str]) -> int:
    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return -1
    return proc.returncode


def _harden_dir(path: Path, *, runner: Callable[[Sequence[str]], int] | None,
                platform: str | None, notes: list[str] | None) -> None:
    """Mode 0700 (POSIX) or an owner-only inherited ACL (Windows, ``icacls`` through *runner*);
    a failed ACL is logged and noted as ``dq.windows_acl_not_enforced``."""
    plat = platform if platform is not None else os.name
    if plat != "nt":
        os.chmod(path, 0o700)
        return
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    code = -1
    if user:
        code = (runner or _default_runner)(["icacls", str(path), "/inheritance:r", "/grant:r",
                                            f"{user}:(OI)(CI)F"])
    if code != 0:
        logger.warning("%s: owner-only ACL not enforced (%s)", path.name, ACL_WARNING)
        if notes is not None and ACL_WARNING not in notes:
            notes.append(ACL_WARNING)


def _rmtree(path: Path) -> None:
    def onerror(func: Callable[..., Any], target: str, _exc: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
            func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onerror)
    else:  # pragma: no cover - Python 3.10 / 3.11
        shutil.rmtree(path, onerror=onerror)


@contextlib.contextmanager
def private_workdir(*, parent: Path | None = None, keep_raw: Path | None = None,
                    resume: bool = False,
                    runner: Callable[[Sequence[str]], int] | None = None,
                    platform: str | None = None,
                    notes: list[str] | None = None) -> Iterator[Path]:
    """A private temporary directory for a raw pull (``tempfile.mkdtemp`` under *parent* or the
    user's temp dir; mode 0700, on Windows an owner-only ACL or ``dq.windows_acl_not_enforced`` in
    *notes*). On exit — also after an exception or ``KeyboardInterrupt`` — it is removed
    recursively, or moved to *keep_raw* (created 0700; the raw files hold logins and become the
    admin's responsibility, which is logged as a warning).

    An existing *keep_raw* that is not an empty directory raises :class:`UsageError` on entry,
    before any request — except with *resume* when it holds a kept pull (its ``manifest.json``):
    that directory is then used in place and left there."""
    keep = Path(keep_raw) if keep_raw is not None else None
    if keep is not None and (keep.exists() or keep.is_symlink()):
        if keep.is_symlink() or not keep.is_dir():
            raise UsageError(f"--keep-raw {keep.name} must name a new or empty directory")
        if any(keep.iterdir()):
            if resume and (keep / MANIFEST_NAME).is_file():
                _harden_dir(keep, runner=runner, platform=platform, notes=notes)
                logger.warning("resuming the raw pull kept in %s (it holds GitHub logins; "
                               "delete it when done)", keep)
                yield keep
                return
            raise UsageError(f"--keep-raw {keep.name} already holds files; choose a new or empty "
                             "directory")
    base = Path(parent) if parent is not None else None
    if base is not None and not base.is_dir():
        try:
            base.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            raise UsageError(f"the temporary directory parent {base.name} cannot be created "
                             f"({type(exc).__name__})") from None
    work = Path(tempfile.mkdtemp(prefix="tokenbill-pull-", dir=base))
    try:
        _harden_dir(work, runner=runner, platform=platform, notes=notes)
        yield work
    finally:
        in_flight = sys.exc_info()[1] is not None
        if keep is not None:
            try:
                if keep.is_dir() and not any(keep.iterdir()):
                    keep.rmdir()
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(work), str(keep))
                _harden_dir(keep, runner=runner, platform=platform, notes=notes)
                logger.warning("raw GitHub data kept in %s: it holds logins and is your "
                               "responsibility; delete it when done", keep)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    _rmtree(work)
                if not in_flight:
                    raise SourceError(f"--keep-raw {keep.name}: could not move the raw pull "
                                      f"there ({type(exc).__name__}); it was deleted") from None
                logger.error("--keep-raw %s: could not move the raw pull there; it was deleted",
                             keep.name)
        else:
            try:
                _rmtree(work)
            except OSError:
                logger.error("could not delete the private work directory %s", work)
            if work.exists():
                logger.error("the private work directory %s still exists; delete it", work)
                if not in_flight:
                    raise SourceError("the private work directory could not be deleted")


def unit_hash(text: str) -> str:
    """A short, stable, content-free id part for *text* (agent-task repositories)."""
    return hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:16]
