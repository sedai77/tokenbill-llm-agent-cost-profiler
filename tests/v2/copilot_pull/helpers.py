"""CP-PULL test helpers: an in-process fake of ``api.github.com`` plus a signed-URL storage host,
and a small synthetic Copilot enterprise whose bodies use the verified field names of addendum §19.3
(values synthetic; ``CANARY_LOGIN`` planted wherever a login occurs). No socket is ever opened: the
fake is the injected opener."""

from __future__ import annotations

import datetime as _dt
import email.message
import io
import json
import re
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tokenbill.copilot.pull_common import TokenSource
from tokenbill.core.builders import CANARY_LOGIN

ENT = "acme"
ORG = "acme-eng"
API = "api.github.com"
BLOB = "https://blob.example.net"
#: Token-shaped canaries (``core.secrets`` would flag them; they must never reach a file or log).
TOKEN_A = "ghp_" + "Tb7cAnArY0a1b2c3d4e5f6g7h8i9j0KLmn"
TOKEN_B = "ghp_" + "Tb7cAnArY1z9y8x7w6v5u4t3s2r1q0PQrs"
USER_TOKEN = "github_pat_" + "Tb7cAnArY2_userTokenValue0123456789ab"
SIG = "sig=Tb7SiGnAtUrE0123456789abcdefXYZ%3D"
NOW = _dt.datetime(2026, 9, 25, 12, 0, tzinfo=_dt.timezone.utc)
NOW_MS = int(NOW.timestamp()) * 1000
DATE = "Fri, 25 Sep 2026 12:00:00 GMT"
CC_ID = "2eeb8ffe-6903-11ee-8c99-0242ac120002"
BUDGET_ID = "b0100000-0000-4000-8000-000000000001"

AI_USAGE_HEADER = ("date,product,sku,quantity,unit_type,applied_cost_per_quantity,gross_amount,"
                   "discount_amount,net_amount,username,organization,repository,cost_center_name,"
                   "model,input,output,cache_read,cache_write,total_monthly_quota")
DETAILED_HEADER = ("date,product,sku,quantity,unit_type,applied_cost_per_quantity,gross_amount,"
                   "discount_amount,net_amount,username,organization,repository,workflow_path,"
                   "cost_center_name")


class FakeResponse:
    """A minimal ``http.client.HTTPResponse`` stand-in (``read(n)`` consumes the body)."""

    def __init__(self, status: int, body: bytes | dict | list = b"",
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.raw_headers = {"Date": DATE, **(headers or {})}
        self.headers = email.message.Message()
        for k, v in self.raw_headers.items():
            self.headers[k] = v
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._buf = io.BytesIO(self.body)
        self.closed = False

    def clone(self) -> FakeResponse:
        """A fresh, unread copy (routes answer the same response more than once)."""
        return FakeResponse(self.status, self.body, dict(self.raw_headers))

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        self.closed = True


def ok(body: bytes | dict | list, status: int = 200, **headers: str) -> FakeResponse:
    """A 2xx answer (headers use ``_`` for ``-``: ``retry_after="5"``)."""
    return FakeResponse(status, body, {k.replace("_", "-"): v for k, v in headers.items()})


def err(status: int, body: bytes | dict = b"", **headers: str) -> FakeResponse:
    """A non-2xx answer (raised as ``urllib.error.HTTPError`` by :class:`FakeGitHub`)."""
    return FakeResponse(status, body, {k.replace("_", "-"): v for k, v in headers.items()})


@dataclass
class Call:
    """One request the fake saw."""

    method: str
    url: str
    host: str
    path: str
    query: dict[str, str]
    auth: str | None
    redirectable_auth: bool
    headers: dict[str, str]
    body: bytes | None


Handler = Callable[[Call], Any]


@dataclass
class _Route:
    method: str
    pattern: re.Pattern[str]
    answers: list[Any] | Handler


@dataclass
class FakeGitHub:
    """The injected opener: records every request, answers from routes, enforces tokens on GitHub
    hosts (a request without a valid ``Bearer`` token gets 401) and raises non-2xx answers as
    ``HTTPError`` like ``urllib`` does."""

    valid: set[str] = field(default_factory=lambda: {TOKEN_A, USER_TOKEN})
    calls: list[Call] = field(default_factory=list)
    routes: list[_Route] = field(default_factory=list)
    raise_http_errors: bool = True
    hook: Callable[[Call], None] | None = None

    def route(self, method: str, pattern: str, answers: list[Any] | Handler | Any) -> None:
        """Answer ``METHOD`` requests whose host + path fully match the regex *pattern* (a list is
        consumed in order, its last answer repeating; a callable gets the :class:`Call`)."""
        if not isinstance(answers, list) and not callable(answers):
            answers = [answers]
        self.routes.insert(0, _Route(method, re.compile(pattern), answers))

    def __call__(self, request: Any, timeout: int) -> Any:
        assert timeout == 30
        parts = urllib.parse.urlsplit(request.full_url)
        auth = request.get_header("Authorization")
        call = Call(method=request.get_method(), url=request.full_url, host=parts.netloc,
                    path=parts.path, query=dict(urllib.parse.parse_qsl(parts.query)), auth=auth,
                    redirectable_auth="Authorization" in request.headers,
                    headers={k.lower(): v for k, v in request.header_items()},
                    body=request.data)
        self.calls.append(call)
        if self.hook is not None:
            self.hook(call)
        if parts.netloc in (API, "github.com"):
            token = auth[len("Bearer "):] if auth and auth.startswith("Bearer ") else None
            if token not in self.valid:
                return self._answer(err(401, {"message": "Bad credentials"}), call)
        target = parts.netloc + parts.path
        for route in self.routes:
            if route.method == call.method and route.pattern.fullmatch(target):
                if callable(route.answers):
                    answer = route.answers(call)
                else:
                    answer = route.answers.pop(0) if len(route.answers) > 1 else route.answers[0]
                return self._answer(answer, call)
        return self._answer(err(404, {"message": "Not Found"}), call)

    def _answer(self, answer: Any, call: Call) -> Any:
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer) and not isinstance(answer, FakeResponse):
            answer = answer(call)
        if isinstance(answer, FakeResponse):
            answer = answer.clone()
        if self.raise_http_errors and isinstance(answer, FakeResponse) and answer.status >= 400:
            raise urllib.error.HTTPError(call.url, answer.status, "error", answer.headers,
                                         io.BytesIO(answer.read()))
        return answer

    # ----- queries ----------------------------------------------------------------------------

    def api_calls(self) -> list[Call]:
        """Requests to the API host."""
        return [c for c in self.calls if c.host == API]

    def paths(self, method: str | None = None) -> list[str]:
        """Paths of the API requests (optionally of one method)."""
        return [c.path for c in self.api_calls() if method is None or c.method == method]


def token_file(tmp_path: Path, value: str = TOKEN_A, mode: int = 0o600) -> Path:
    """A token file with *value* and *mode*."""
    path = tmp_path / "secrets" / "gh-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def source(tmp_path: Path, value: str = TOKEN_A) -> TokenSource:
    """A private token file source holding *value*."""
    return TokenSource.from_file(token_file(tmp_path, value))


# ---------------------------------------------------------------------------------------------
# the synthetic enterprise
# ---------------------------------------------------------------------------------------------


def ai_usage_csv(start: str) -> bytes:
    """An AI usage report CSV (documented columns; a canary login row and a direct row)."""
    rows = [f"{start},copilot,copilot_ai_credit,10.5,ai-credits,0.01,0.105,0.105,0,"
            f"{CANARY_LOGIN},{ORG},,cc-platform,Claude Sonnet 5,12000,3000,180000,6000,Unknown",
            f"{start},copilot,copilot_ai_credit,2,ai-credits,0.01,0.02,0,0.02,,{ORG},,,"
            "Code Review,1000,200,0,0,Unknown"]
    return ("﻿" + AI_USAGE_HEADER + "\n" + "\n".join(rows) + "\n").encode("utf-8")


def detailed_csv(start: str) -> bytes:
    """A detailed usage report CSV with a seat line."""
    row = (f"{start},copilot,copilot_for_business,1,user-months,19,19,0,19,{CANARY_LOGIN},{ORG},"
           ",,cc-platform")
    return (DETAILED_HEADER + "\n" + row + "\n").encode("utf-8")


def seat(login: str, org: str = ORG) -> dict[str, Any]:
    """One seat object (seats API field names)."""
    return {"assignee": {"login": login, "id": 1000, "type": "User",
                         "avatar_url": "https://avatars.githubusercontent.com/u/1000?v=4",
                         "url": f"https://api.github.com/users/{login}"},
            "organization": {"login": org}, "plan_type": "business", "assigning_team": None,
            "created_at": "2026-03-01T09:00:00Z", "last_activity_at": "2026-09-18T10:00:00Z",
            "last_activity_editor": "vscode/1.77.3/copilot/1.86.82",
            "last_authenticated_at": "2026-09-18T10:00:00Z", "pending_cancellation_date": None,
            "updated_at": "2026-09-01T00:00:00Z"}


def metrics_lines(report: str, day: str) -> bytes:
    """NDJSON of one metrics report day (usage-metrics field names)."""
    if report == "users-1-day":
        recs = [{"day": day, "enterprise_id": "1", "user_id": 1000, "user_login": CANARY_LOGIN,
                 "user_initiated_interaction_count": 7, "code_generation_activity_count": 3,
                 "code_acceptance_activity_count": 2, "loc_added_sum": 32, "loc_deleted_sum": 6,
                 "ai_credits_used": 12.5, "used_chat": True,
                 "totals_by_ide": [{"ide": "vscode", "user_initiated_interaction_count": 7}]}]
    elif report == "user-teams-1-day":
        recs = [{"day": day, "enterprise_id": "1", "slug": "platform", "team_id": 44,
                 "user_id": 1000, "user_login": CANARY_LOGIN}]
    else:
        recs = [{"day": day, "enterprise_id": "1", "daily_active_users": 12,
                 "loc_added_sum": 54, "loc_deleted_sum": 6,
                 "code_acceptance_activity_count": 40}]
    return b"".join(json.dumps(r).encode() + b"\n" for r in recs)


def _usage_items(model: bool) -> list[dict[str, Any]]:
    item = {"product": "Copilot", "sku": "Copilot AI Credits", "unitType": "credits",
            "pricePerUnit": 0.01, "grossQuantity": 1000, "grossAmount": 10.0,
            "discountQuantity": 1000, "discountAmount": 10.0, "netQuantity": 0, "netAmount": 0}
    if model:
        item["model"] = "Claude Sonnet 5"
    return [item]


class World:
    """Routes for every handoff kind of :data:`ENT` / :data:`ORG` plus export bookkeeping.

    Report exports: POST answers ``processing``; each export is ``processing`` for
    ``polls_until_done`` status GETs, then ``completed`` with one signed download link."""

    def __init__(self, gh: FakeGitHub, *, polls_until_done: int = 1,
                 seat_pages: int = 3) -> None:
        self.gh = gh
        self.polls_until_done = polls_until_done
        self.exports: dict[str, dict[str, Any]] = {}
        base = rf"{API}/enterprises/{ENT}"
        gh.route("POST", rf"{base}/settings/billing/reports", self._post_export)
        gh.route("GET", rf"{base}/settings/billing/reports/([A-Za-z0-9_.-]+)",
                 self._get_export)
        gh.route("GET", rf"blob\.example\.net/exports/([A-Za-z0-9_.-]+)\.csv", self._download)
        gh.route("GET", rf"{base}/settings/billing/usage/summary", ok(
            {"enterprise": ENT, "timePeriod": {"year": 2026, "month": 9},
             "usageItems": _usage_items(False)}))
        gh.route("GET", rf"{base}/settings/billing/ai_credit/usage", ok(
            {"enterprise": ENT, "timePeriod": {"year": 2026, "month": 9},
             "usageItems": _usage_items(True)}))
        gh.route("GET", rf"{base}/settings/billing/usage", lambda c: ok(
            {"usageItems": [{"date": "2026-09-01", "product": "Copilot", "sku": "Copilot AI "
                             "Credits", "quantity": 10, "unitType": "credits",
                             "pricePerUnit": 0.01, "grossAmount": 0.1, "discountAmount": 0,
                             "netAmount": 0.1, "organizationName": ORG}]}))
        gh.route("GET", rf"{base}/settings/billing/cost-centers", ok(
            {"costCenters": [{"id": CC_ID, "name": "cc-platform", "state": "active",
                              "ai_credit_pool_enabled": True,
                              "resources": [{"type": "User", "name": CANARY_LOGIN}]}]}))
        gh.route("GET", rf"{base}/settings/billing/budgets", ok(
            {"budgets": [{"id": BUDGET_ID, "budget_scope": "multi_user_cost_center",
                          "budget_type": "SkuPricing", "budget_amount": 250.0,
                          "budget_product_sku": "copilot_ai_credit",
                          "prevent_further_usage": True,
                          "budget_alerting": {"will_alert": False, "alert_recipients": []}}],
             "has_next_page": False, "total_count": 1}))
        gh.route("GET", rf"{base}/settings/billing/budgets/{BUDGET_ID}/user-states", ok(
            {"user_states": [{"user": CANARY_LOGIN, "consumed_amount": 10,
                              "target_amount": 250.0}],
             "has_next_page": False, "total_count": 1}))
        pages = [ok({"total_seats": seat_pages, "seats": [seat(CANARY_LOGIN if i == 0
                                                             else f"dev-{i:02d}")]},
                    Link=self._link(i + 2, seat_pages) if i + 1 < seat_pages else "")
                 for i in range(seat_pages)]
        gh.route("GET", rf"{base}/copilot/billing/seats", lambda c: pages[
            int(c.query.get("page", "1")) - 1])
        gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing", ok(
            {"plan_type": "business", "seat_management_setting": "assign_selected",
             "ide_chat": "enabled", "platform_chat": "disabled", "cli": "enabled",
             "seat_breakdown": {"total": 3, "added_this_cycle": 0, "pending_cancellation": 0,
                                "pending_invitation": 0, "active_this_cycle": 3,
                                "inactive_this_cycle": 0}}))
        gh.route("GET", rf"{API}/orgs/{ORG}/copilot/billing/seats", ok(
            {"total_seats": 1, "seats": [seat(CANARY_LOGIN)]}))
        gh.route("GET", rf"{base}/copilot/metrics/reports/([a-z0-9-]+)", self._metrics)
        gh.route("GET", r"blob\.example\.net/metrics/([a-z0-9-]+)/(\d{4}-\d{2}-\d{2})\.ndjson",
                 self._metrics_download)

    @staticmethod
    def _link(page: int, last: int) -> str:
        url = f"https://{API}/enterprises/{ENT}/copilot/billing/seats?per_page=100"
        return (f'<{url}&page={page}>; rel="next", <{url}&page={last}>; rel="last"')

    def _post_export(self, call: Call) -> FakeResponse:
        payload = json.loads(call.body or b"{}")
        rid = f"rpt-{payload['report_type']}-{payload['start_date']}"
        self.exports[rid] = {"payload": payload, "polls": 0}
        return ok({"id": rid, "report_type": payload["report_type"], "status": "processing",
                   "start_date": payload["start_date"], "end_date": payload["end_date"]},
                  status=202)

    def _get_export(self, call: Call) -> FakeResponse:
        rid = call.path.rsplit("/", 1)[1]
        state = self.exports.get(rid)
        if state is None:
            return err(404)
        state["polls"] += 1
        if state["polls"] < self.polls_until_done:
            return ok({"id": rid, "status": "processing"})
        return ok({"id": rid, "status": "completed",
                   "download_urls": [f"{BLOB}/exports/{rid}.csv?sv=2025-01-05&se=2026-09-26"
                                     f"&sp=r&{SIG}"]})

    def _download(self, call: Call) -> FakeResponse:
        rid = call.path.rsplit("/", 1)[1][:-len(".csv")]
        payload = self.exports[rid]["payload"]
        maker = ai_usage_csv if payload["report_type"] == "ai_credit" else detailed_csv
        return ok(maker(payload["start_date"]), Content_Type="text/csv")

    def _metrics(self, call: Call) -> FakeResponse:
        report = call.path.rsplit("/", 1)[1]
        day = call.query["day"]
        return ok({"download_links": [f"{BLOB}/metrics/{report}/{day}.ndjson?{SIG}"],
                   "report_day": day})

    def _metrics_download(self, call: Call) -> FakeResponse:
        _, _, report, name = call.path.split("/")
        return ok(metrics_lines(report, name[:-len(".ndjson")]))


def all_recorded_bytes(root: Path) -> bytes:
    """Every byte under *root* (recorded files, manifest)."""
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def template_regex(template: str) -> re.Pattern[str]:
    """A request-path template (``{name}`` parts) as a full-match regex."""
    return re.compile(re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(template)))
