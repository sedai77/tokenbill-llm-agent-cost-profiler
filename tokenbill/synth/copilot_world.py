"""The deterministic synthetic GitHub Copilot enterprise (addendum §18; CP-SYNTH).

:func:`generate` builds one enterprise (orgs ``org-a`` with seat policy ``assign_selected`` and
``org-b`` with ``assign_all``) of 146 seated developers in eleven teams plus a ``ci`` automation
identity, over 2026-07-03 … 2026-09-22 (``today`` 2026-09-23), as canonical records: AI usage report
rows (``CostLine`` + token ``UsageAggregate`` per day and cell key, one coverage aggregate per file
and day), seat / Actions / sandbox lines of the detailed report, seats (``LicenseSnapshot``),
usage-metrics user-days (``ActivityDay``), team outcomes, configuration (org seat policies, a
capped cost center, budgets, run flags) and the collector sessions (VS Code traces and outfile,
Copilot CLI events and OTel, gh-aw runs) with their lane events. The truth of every plant is
computed by the closed forms of :mod:`tokenbill.synth.copilot_truth`.

Team table (brief CP-SYNTH, 146 users): ``platform`` 20 Enterprise seats (6 idle removable, 4
plan-mix, 5 $0 user budgets on idle seats) · ``infra`` 10 team-assigned (5 idle) · ``ops`` 8 in
org B ``assign_all`` (5 idle) · ``payments`` 20 on Opus 4.8 (Opus 5.5 on 2026-09-22) and Sonnet 5,
no Auto · ``mobile`` 15 Opus 4.8 fast mode · ``data`` 15 on GPT-5.4 / GPT-5.5 in the capped cost
center ``cc-data`` (cap policy unknown) · ``agents`` 4 Copilot CLI users (compactions, uncapped
``--ci`` sessions) · ``vscode`` 15 with VS Code traces (10) or outfiles (5): long-context band
requests, forced compactions, a model switch, 30k tool-definition tokens, three conversations also
seen as CLI sessions · ``jetbrains`` 15 (org data only; 90% IntelliJ interactions) · ``core`` 20
(control: Auto, Sonnet 5) · ``tiny`` 4. Every other team's editor mix is 70% VS Code / 30%
JetBrains. ``ci``: code review (pseudo rows without a username, Actions on ``linux_16_core``),
direct CLI and one agentic workflow (``.lock.yml`` Actions lines, gh-aw runs, direct credits in
its repository).

**Money.** Report gross = token price (``core.testing.FakePricer`` point rates; Auto rows at ×0.9);
the pool discount is drawn per entity in time order (licensed rows of a day in principal order;
direct rows never draw it), net = gross − discount. Token counts are multiples of 10, so every
row's gross is a whole multiple of 100 nano and no closed form depends on a rounding order. July
and August run on the promo allowances (slack); September on the standard allowance (overage).

**Pricing guard.** Every row is dated where its model has a rate row and never within ±2 days of
one of the model's K-dated rate changes (:func:`tokenbill.synth.copilot_truth.k_dated_changes`);
Opus 5.5 only on 2026-09-22; GPT-5.6 Sol is never used. A violating window raises ``UsageError``.

**Variants.** ``volume`` (billing mode volume), ``slack`` (September usage of the enterprise
entity halved: under the pool), ``plan_unknown`` (the no-token handoff view: no seat lines, no
seats API — activity-report seats — no org settings, budgets or cost centers; answers-file run
flags), ``plan_conflict`` (seats API says Enterprise for org B, its seat lines say Business),
``plan_quota`` (``plan_unknown`` plus ``plan_quota`` rows from the report's
``total_monthly_quota``). The GitHub-side money is identical in every variant but ``slack``.

Everything is a pure function of the arguments: randomness only through
:func:`tokenbill.common.rng`, ids through ``core.ids``; no content, login or canary in any record
(logins appear only in :attr:`CopilotWorld.team_map`, for the writers).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction

from tokenbill.common import rng as _rng
from tokenbill.core import facts as _facts
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import (
    copilot_lane_key,
    copilot_session_key,
    hmac_hex,
    natural_id,
    pseudonym,
    request_id_for,
)
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, nano_to_credits_str
from tokenbill.core.records import (
    ActivityDay,
    Attempt,
    Attribution,
    ConfigSnapshot,
    CostLine,
    Fidelity,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    LicenseSnapshot,
    Outcome,
    OutcomeAggregate,
    PricingContext,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    WorkloadClass,
    to_json,
)
from tokenbill.core.testing import FakePricer
from tokenbill.synth import copilot_truth as _truth
from tokenbill.synth.copilot_truth import CopilotTruth, RowFact, SeatFact

__all__ = [
    "BASE_USERS",
    "CAP_CREDITS",
    "CC_DATA",
    "CONVENTIONS",
    "NAME_KEY",
    "ORG_A",
    "ORG_B",
    "PRINCIPAL_KEY",
    "TEAMS",
    "TODAY",
    "VARIANTS",
    "CopilotRecords",
    "CopilotWorld",
    "ReportSet",
    "TeamSpec",
    "User",
    "generate",
    "iter_records",
    "team_sizes",
]

TODAY = "2026-09-23"
WINDOW_START = "2026-07-03"
WINDOW_END = "2026-09-22"
ORG_A, ORG_B = "org-a", "org-b"
CC_DATA = "cc-data"
#: The capped cost center's pool target (its 15 Business seats' standard allowance).
CAP_CREDITS = Decimal(28_500)
BASE_USERS = 146
VARIANTS = ("volume", "slack", "plan_unknown", "plan_conflict", "plan_quota")
CONVENTIONS = ("excl", "incl", "undecidable")
#: Synthetic, public keys (they protect nothing): principals are ``pseudonym(PRINCIPAL_KEY, "p",
#: login)``, repositories and workflow paths ``pseudonym(NAME_KEY, "h", name)``.
PRINCIPAL_KEY = hashlib.sha256(b"tokenbill synthetic copilot: principal key (public)").digest()
NAME_KEY = hashlib.sha256(b"tokenbill synthetic copilot: name key (public)").digest()

_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000
_EPOCH = _dt.date(1970, 1, 1)
_CHANNEL = "github_copilot"
_SKU = "copilot_ai_credit"
_POOLED, _DIRECT = "ai_credit.user", "ai_credit.direct"
_REPORT = "github.ai_usage_report"
_METERED = "github.metered_usage"
_OPUS55_DAY = "2026-09-22"
_GPT55_FROM = "2026-07-13"          # the remap target gpt-5.6-terra is priced from 2026-07-09
_SEAT_CREATED = "2026-01-15"
_STATIC_SYSTEM, _STATIC_TOOLS = 4_000, 30_000
_STATIC = _STATIC_SYSTEM + _STATIC_TOOLS
_SUMMARY = 20_000
_CREDIT_LIMIT_NANO = 5_000_000_000   # --max-ai-credits 500
_REVIEW_CREDITS_NANO = 250_000_000   # 25 credits of code review per business day
_REVIEW_MINUTES, _AW_MINUTES = Decimal(30), Decimal(12)
_REVIEW_RUNNER, _AW_RUNNER = "linux_16_core", "actions_linux"
_REPO_REVIEW, _REPO_CI, _REPO_AW = "tb-synth/app-monorepo", "tb-synth/ci-tools", "tb-synth/triage"
_AW_PATH = ".github/workflows/triage.lock.yml"
_SANDBOX_USD_PER_MINUTE = Decimal("0.02")
_SONNET, _SONNET_AUTO = "Claude Sonnet 5", "Auto: Claude Sonnet 5"
_OPUS48, _OPUS48_FAST, _OPUS55 = "Claude Opus 4.8", "Claude Opus 4.8 (fast mode)", "Claude Opus 5.5"
_GPT54, _GPT55, _REVIEW = "GPT-5.4", "GPT-5.5", "code review"
#: One usage unit (tokens per unit of a user-day): Claude models report unknown-TTL writes, the GPT
#: rows price no write (the report shows none).
_UNIT_CLAUDE = UsageBuckets(uncached_input=20_000, cache_read=200_000, cache_write_unknown=10_000,
                            output=5_000)
_UNIT_GPT = UsageBuckets(uncached_input=20_000, cache_read=200_000, output=5_000)


# ---------------------------------------------------------------------------------------------
# teams and users
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeamSpec:
    """One team of the plant table: seats, editor mix, usage profile and planted seat counts."""

    name: str
    size: int
    org: str
    plan: str
    cost_center: str | None
    via_team: bool
    editor: str          # mix (70% VS Code / 30% JetBrains) | vscode | jetbrains | cli
    profile: str
    idle: int = 0
    plan_mix: int = 0
    zero_budgets: int = 0


TEAMS: tuple[TeamSpec, ...] = (
    TeamSpec("platform", 20, ORG_A, "enterprise", None, False, "mix", "platform", idle=6,
             plan_mix=4, zero_budgets=5),
    TeamSpec("infra", 10, ORG_A, "business", None, True, "mix", "normal", idle=5),
    TeamSpec("ops", 8, ORG_B, "business", None, False, "mix", "normal", idle=5),
    TeamSpec("payments", 20, ORG_A, "business", None, False, "mix", "payments"),
    TeamSpec("mobile", 15, ORG_A, "business", None, False, "mix", "mobile"),
    TeamSpec("data", 15, ORG_A, "business", CC_DATA, False, "mix", "data"),
    TeamSpec("agents", 4, ORG_A, "business", None, False, "cli", "agents"),
    TeamSpec("vscode", 15, ORG_A, "business", None, False, "vscode", "vscode"),
    TeamSpec("jetbrains", 15, ORG_A, "business", None, False, "jetbrains", "wide"),
    TeamSpec("core", 20, ORG_A, "business", None, False, "mix", "core"),
    TeamSpec("tiny", 4, ORG_A, "business", None, False, "mix", "normal"),
)
#: Units per business day (inclusive ranges) per profile component, drawn once per user × month.
_RANGES: Mapping[str, tuple[int, int]] = {
    "heavy": (11, 15), "normal": (8, 12), "wide": (10, 14), "core": (11, 15), "agents": (6, 8),
    "vscode": (10, 12), "opus": (2, 3), "fast": (1, 2), "fast_sonnet": (3, 5), "gpt54": (3, 4),
    "gpt55": (2, 3)}


@dataclass(frozen=True, slots=True)
class User:
    """One seated developer (the login never leaves :attr:`CopilotWorld.team_map`)."""

    login: str
    principal: str
    team: str
    index: int
    org: str
    plan: str
    cost_center: str | None
    via_team: bool
    idle: bool
    plan_mix: bool
    zero_budget: bool
    surface: str
    editor: str
    profile: str


def team_sizes(users: int = BASE_USERS) -> dict[str, int]:
    """Team → size: the plant table for 146 users, else proportional (largest remainder, ≥ 1)."""
    if type(users) is not int or users < len(TEAMS):
        raise UsageError(f"users: expected an int >= {len(TEAMS)}")
    base = {t.name: t.size for t in TEAMS}
    if users == BASE_USERS:
        return base
    extra = users - len(TEAMS)
    spare = BASE_USERS - len(TEAMS)
    parts = {n: (s - 1) * extra // spare for n, s in base.items()}
    order = sorted(base, key=lambda n: (-(((base[n] - 1) * extra) % spare), n))
    for n in order[:extra - sum(parts.values())]:
        parts[n] += 1
    return {n: 1 + parts[n] for n in base}


def _scaled(n: int, size: int, base: int) -> int:
    return min(size - 1, n * size // base) if n else 0


def _users(users: int) -> list[User]:
    sizes = team_sizes(users)
    width = 2 if users < 100 * len(TEAMS) else 4
    out: list[User] = []
    for spec in TEAMS:
        size = sizes[spec.name]
        idle = _scaled(spec.idle, size, spec.size)
        mix = _scaled(spec.plan_mix, size, spec.size)
        zero = min(idle, _scaled(spec.zero_budgets, size, spec.size))
        for i in range(size):
            login = CANARY_LOGIN if spec.name == "core" and i == 0 else (
                f"{spec.name}-dev-{i:0{width}d}")
            surface = {"vscode": "vscode", "jetbrains": "jetbrains", "cli": "cli"}.get(
                spec.editor, "vscode" if i % 10 < 7 else "jetbrains")
            out.append(User(
                login=login, principal=pseudonym(PRINCIPAL_KEY, "p", login), team=spec.name,
                index=i, org=spec.org, plan=spec.plan, cost_center=spec.cost_center,
                via_team=spec.via_team, idle=i < idle, plan_mix=idle <= i < idle + mix,
                zero_budget=i < zero, surface=surface, editor=spec.editor,
                profile=spec.profile))
    return out


# ---------------------------------------------------------------------------------------------
# records containers
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportSet:
    """One AI usage report file as the adapter maps it, under one token convention: its rows'
    cost lines (identical money in every set) and its token and coverage aggregates."""

    convention: str
    source_id: str
    cost_lines: tuple[CostLine, ...]
    aggregates: tuple[UsageAggregate, ...]


@dataclass(frozen=True)
class CopilotRecords:
    """The canonical records of one world (report set ``excl``)."""

    cost_lines: tuple[CostLine, ...]
    aggregates: tuple[UsageAggregate, ...]
    licenses: tuple[LicenseSnapshot, ...]
    activity: tuple[ActivityDay, ...]
    config: tuple[ConfigSnapshot, ...]
    outcomes: tuple[OutcomeAggregate, ...]
    sessions: tuple[Session, ...]
    #: (principal, login) of every seated user — only for the schema-true writers (CP-SYNTH-W),
    #: which must write logins the adapters map back through :attr:`CopilotWorld.team_map`.
    logins: tuple[tuple[str, str], ...] = ()

    @property
    def lanes(self) -> tuple[Lane, ...]:
        """Every lane of every session."""
        return tuple(lane for s in self.sessions for lane in s.lanes)

    @property
    def requests(self) -> tuple[Request, ...]:
        """Every request of every lane."""
        return tuple(r for lane in self.lanes for r in lane.requests)

    @property
    def events(self) -> tuple[LaneEvent, ...]:
        """Every lane event."""
        return tuple(e for lane in self.lanes for e in lane.events)

    def all(self) -> Iterator[object]:
        """Every record (sessions carry their lanes, requests and events)."""
        for group in (self.cost_lines, self.aggregates, self.licenses, self.activity,
                      self.config, self.outcomes, self.sessions):
            yield from group

    def json_lines(self) -> list[str]:
        """Canonical JSON of every record, in record order (the determinism check)."""
        return [json.dumps(to_json(r), sort_keys=True, separators=(",", ":"))
                for r in self.all()]


@dataclass(frozen=True)
class CopilotWorld:
    """A generated enterprise: canonical records, per-convention report sets and the truth."""

    seed: int
    users: int
    start: str
    end: str
    today: str
    variants: tuple[str, ...]
    conventions: tuple[str, ...]
    records: CopilotRecords
    reports: Mapping[str, ReportSet]
    truth: CopilotTruth
    team_map: Mapping[str, str]          # login → team (writers, handoff exports)
    cost_center_map: Mapping[str, str]   # login → cost center
    people: tuple[User, ...] = ()
    principal_key: bytes = PRINCIPAL_KEY
    name_key: bytes = NAME_KEY


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _ms(date: str) -> int:
    return (_dt.date.fromisoformat(date) - _EPOCH).days * _DAY_MS


def _date_of(ms: int) -> str:
    return (_EPOCH + _dt.timedelta(milliseconds=ms)).isoformat()


def _dates(start: str, end: str) -> list[str]:
    d0, d1 = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
    return [(d0 + _dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _business(date: str) -> bool:
    return _dt.date.fromisoformat(date).weekday() < 5


def _scale(u: UsageBuckets, k: int) -> UsageBuckets:
    return UsageBuckets(uncached_input=u.uncached_input * k, cache_read=u.cache_read * k,
                        cache_write_unknown=u.cache_write_unknown * k, output=u.output * k)


def _h(name: str) -> str:
    return pseudonym(NAME_KEY, "h", name)


def _source_id(name: str) -> str:
    return "s_" + hmac_hex(NAME_KEY, name.encode("utf-8"))


def _entity(cost_center: str | None, capped: bool = True) -> str:
    return f"cc:{cost_center}" if capped and cost_center == CC_DATA else "enterprise"


def _last_day(month: str) -> str:
    first = _dt.date.fromisoformat(f"{month}-01")
    return ((first.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
            - _dt.timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------------------------
# collector lanes (VS Code, CLI, OTel, gh-aw)
# ---------------------------------------------------------------------------------------------


@dataclass
class _Req:
    """One planned request: the report row it lands in and its priced inferences."""

    ts: int
    model: str
    usage: UsageBuckets
    compaction: UsageBuckets | None = None
    tier: str | None = None


@dataclass
class _Conv:
    """One planned conversation (one main lane) and every source that records it."""

    raw_id: str
    user: User | None
    sources: tuple[str, ...]           # adapters: copilot-vscode-traces, copilot-otel, …
    requests: list[_Req]
    events: list[tuple[int, LaneEventKind, dict[str, object]]]
    agent_product: str
    workload: WorkloadClass
    billing_path: str
    repo: str | None = None
    credit_limit: int | None = None


_SOURCE_KIND = {"copilot-vscode-traces": "copilot.vscode_traces", "copilot-otel": "copilot.otel",
                "copilot-cli": "copilot.cli_events", "gh-aw-token-usage": "gh_aw.token_usage"}
_PRIORITY = {"copilot-vscode-traces": 30, "copilot-cli": 25, "copilot-otel": 20,
             "gh-aw-token-usage": 20}
_REPORTER = {"copilot-vscode-traces": "copilot.vscode.turn",
             "copilot-otel": "copilot.otel.invoke_agent", "copilot-cli": "copilot.cli.checkpoint",
             "gh-aw-token-usage": "gh_aw.run_total"}


def _chain(model: str, ts: int, totals: Sequence[int], *, output: int, gpt: bool,
           first_read: int = 0, gap_ms: int = 90_000) -> list[_Req]:
    """Requests of one warm chain: request *i* reads the previous total input (the first reads
    *first_read*), Claude writes the rest but 2,000 fresh tokens, GPT reports it uncached."""
    out: list[_Req] = []
    prev = first_read
    for i, total in enumerate(totals):
        fresh = total - prev
        if gpt:
            usage = UsageBuckets(uncached_input=fresh, cache_read=prev, output=output)
        else:
            usage = UsageBuckets(uncached_input=2_000, cache_read=prev,
                                 cache_write_unknown=fresh - 2_000, output=output)
        out.append(_Req(ts=ts + i * gap_ms, model=model, usage=usage))
        prev = total
    return out


def _compaction(trigger: str, pre: int) -> dict[str, object]:
    return {"trigger": "manual" if trigger == "manual" else "auto", "pre_tokens": pre,
            "post_tokens": _SUMMARY, "duration_ms": 4_000, "copilot_trigger": trigger,
            "system_tokens": _STATIC_SYSTEM, "tool_definitions_tokens": _STATIC_TOOLS}


def _vscode_conversation(user: User, k: int, day: str) -> _Conv:
    """Conversation *k* of a vscode-team user: warm Sonnet 5 chains over the 34k static prefix;
    user 0–2 conversation 0 are GPT-5.5 long-context band plants (tier unknown / default /
    long_context), 3–6 carry a compaction (context_limit_retry, memory_pressure, threshold,
    manual), 7 a model switch to GPT-5.5; 8–10 conversation 0 are also recorded by the in-VS
    Code CLI agent (dedupe)."""
    i = user.index
    ts = _ms(day) + (10 + 4 * k) * _HOUR_MS + i * 300_000
    src = "copilot-vscode-traces" if i < 10 else "copilot-otel"
    sources = (src, "copilot-cli") if k == 0 and i in (8, 9, 10) else (src,)
    steps = [_STATIC + 2_000 + 8_000 * n for n in range(6)]
    reqs = _chain("claude-sonnet-5", ts, steps, output=1_000, gpt=False)
    events: list[tuple[int, LaneEventKind, dict[str, object]]] = []
    tier = None
    if k == 0 and i < 3:
        reqs = _chain("gpt-5.5", ts, [250_000 + 12_000 * n for n in range(4)], output=2_000,
                      gpt=True)
        tier = (None, "default", "long_context")[i]
        for r in reqs:
            r.tier = tier
    elif k == 0 and 3 <= i <= 6:
        trigger = ("context_limit_retry", "memory_pressure", "threshold", "manual")[i - 3]
        pre = steps[2]
        after = [_STATIC + _SUMMARY + 2_000 + 8_000 * n for n in range(3)]
        tail = _chain("claude-sonnet-5", ts + 3 * 90_000, after, output=1_000, gpt=False,
                      first_read=_STATIC)
        tail[0].compaction = UsageBuckets(cache_read=pre, output=_SUMMARY)
        reqs = reqs[:3] + tail
        events.append((tail[0].ts - 1, LaneEventKind.COMPACTION, _compaction(trigger, pre)))
    elif k == 0 and i == 7:
        head = reqs[:3]
        tail = _chain("gpt-5.5", ts + 3 * 90_000, steps[3:], output=1_000, gpt=True)
        reqs = head + tail
        events.append((tail[0].ts - 1, LaneEventKind.MODEL_SWITCH_USER,
                       {"from_model": "claude-sonnet-5", "to_model": "gpt-5.5"}))
    events.append((ts - 1, LaneEventKind.SESSION_META,
                   {"agent_type": None, "routing_mode": "direct", "context_tier": tier}))
    return _Conv(raw_id=f"vscode-{user.login}-{k}", user=user, sources=sources, requests=reqs,
                 events=events, agent_product="copilot_vscode",
                 workload=WorkloadClass.INTERACTIVE, billing_path="copilot_pool")


def _cli_conversation(user: User, k: int, day: str, *, ci: bool) -> _Conv:
    """A Copilot CLI session of an agents-team user: interactive sessions carry ``--max-ai-credits``
    (and, for user 2, two compactions); ``--ci`` sessions carry no cap; users 0–2's interactive
    sessions are also exported over OTel (user 3's is events-only)."""
    i = user.index
    ts = _ms(day) + (9 + 3 * k + (5 if ci else 0)) * _HOUR_MS + i * 600_000
    steps = [_STATIC + 2_000 + 6_000 * n for n in range(4 if ci else 6)]
    reqs = _chain("claude-sonnet-5", ts, steps, output=1_500, gpt=False, gap_ms=60_000)
    events: list[tuple[int, LaneEventKind, dict[str, object]]] = []
    if not ci and i == 2 and k == 0:
        triggers = {2: "threshold", 4: "context_limit_retry"}
        for at in range(1, len(reqs)):
            pre = reqs[at - 1].usage.total_input
            if at in triggers:
                reqs[at].compaction = UsageBuckets(cache_read=pre, output=_SUMMARY)
                reqs[at].usage = UsageBuckets(uncached_input=2_000, cache_read=_STATIC,
                                              cache_write_unknown=_SUMMARY, output=1_500)
                events.append((reqs[at].ts - 1, LaneEventKind.COMPACTION,
                               _compaction(triggers[at], pre)))
            else:
                reqs[at].usage = UsageBuckets(uncached_input=2_000, cache_read=pre,
                                              cache_write_unknown=6_000, output=1_500)
    limit = None if ci else _CREDIT_LIMIT_NANO
    events.append((ts - 1, LaneEventKind.SESSION_META,
                   {"agent_type": None, "credit_limit_nano": limit, "routing_mode": "direct"}))
    sources = ("copilot-cli", "copilot-otel") if not ci and i < 3 else ("copilot-cli",)
    return _Conv(raw_id=f"cli-{user.login}-{k}{'-ci' if ci else ''}", user=user, sources=sources,
                 requests=reqs, events=events, agent_product="copilot_cli",
                 workload=WorkloadClass.CI if ci else WorkloadClass.INTERACTIVE,
                 billing_path="copilot_pool", credit_limit=limit)


def _aw_run(n: int, day: str) -> _Conv:
    """gh-aw run *n* of the agentic workflow (three ``token-usage.jsonl`` lines, org-billed)."""
    ts = _ms(day) + 9 * _HOUR_MS
    wobble = n % 5
    usages = [
        UsageBuckets(uncached_input=6_000, cache_write_unknown=30_000, output=1_500),
        UsageBuckets(uncached_input=4_000, cache_read=30_000 + 10_000 * wobble,
                     cache_write_unknown=8_000, output=2_000),
        UsageBuckets(uncached_input=4_000, cache_read=38_000 + 10_000 * wobble,
                     cache_write_unknown=8_000, output=1_000 * (1 + n % 4)),
    ]
    reqs = [_Req(ts=ts + j * 120_000, model="claude-sonnet-5", usage=u)
            for j, u in enumerate(usages)]
    return _Conv(raw_id=f"gh-aw-triage-{n}", user=None, sources=("gh-aw-token-usage",),
                 requests=reqs, events=[], agent_product="copilot_gh_aw",
                 workload=WorkloadClass.CI, billing_path="copilot_direct", repo=_h(_REPO_AW))


class _Pricing:
    """FakePricer point prices, memoized (every row's gross)."""

    def __init__(self) -> None:
        self.pricer = FakePricer()
        self._memo: dict[tuple, int] = {}

    def ctx(self, model: str, *, routing: str = "direct", speed: str = "standard",
            billing_path: str = "copilot_pool", tier: str | None = None) -> PricingContext:
        return PricingContext(provider="github", channel=_CHANNEL, model=model, model_raw=model,
                              speed=speed, billing_path=billing_path, routing=routing,
                              context_tier=tier)

    def point(self, usage: UsageBuckets, model: str, date: str, **kw: object) -> int:
        key = (usage, model, date, tuple(sorted(kw.items())))
        if key not in self._memo:
            ctx = self.ctx(model, **kw)  # type: ignore[arg-type]
            fig = self.pricer.price_usage(usage, ctx, ts_ms=_ms(date) + 12 * _HOUR_MS).figure
            if fig.nano is None:
                raise UsageError(f"pricing guard: {model} has no rate row on {date}")
            self._memo[key] = fig.nano
        return self._memo[key]


def _plan_conversations(people: Sequence[User], dates: Sequence[str]) -> list[_Conv]:
    """Every collector conversation: September business days up to four days before the end."""
    end = dates[-1]
    cutoff = (_dt.date.fromisoformat(end) - _dt.timedelta(days=4)).isoformat()
    days = [d for d in dates if _business(d) and d <= cutoff and d >= f"{end[:7]}-01"]
    if not days:
        days = [d for d in dates if _business(d)][-1:]
    convs: list[_Conv] = []
    for u in people:
        if u.team == "vscode" and not u.idle:
            for k in (0, 1):
                convs.append(_vscode_conversation(u, k, days[(u.index + 7 * k) % len(days)]))
        elif u.team == "agents" and not u.idle:
            for k in (0, 1):
                convs.append(_cli_conversation(u, k, days[(3 + 2 * u.index + 5 * k) % len(days)],
                                               ci=False))
            if u.index < 2:
                convs.append(_cli_conversation(u, 0, days[(1 + u.index) % len(days)], ci=True))
    aw_from = (_dt.date.fromisoformat(end) - _dt.timedelta(days=30)).isoformat()
    aw_days = [d for d in dates if _business(d) and aw_from <= d <= cutoff]
    convs.extend(_aw_run(n, d) for n, d in enumerate(aw_days))
    return convs


def _sessions(convs: Sequence[_Conv], pricing: _Pricing) -> list[Session]:
    """The collector sessions: one per conversation and source (a conversation seen in two sources
    shares its session key, lane key and request ids)."""
    out: list[Session] = []
    for conv in convs:
        skey = copilot_session_key(conv.raw_id)
        lkey = copilot_lane_key(skey, "main", None)
        u = conv.user
        attribution = Attribution(
            principal=u.principal if u is not None else None, team=u.team if u else None,
            cost_center=u.cost_center if u else None, repo=conv.repo,
            agent_product=conv.agent_product, workload_class=conv.workload,
            billing_path=conv.billing_path,
            extra=(("workflow", _h(_AW_PATH)),) if conv.repo else ())
        for adapter in conv.sources:
            sid = _source_id(f"{adapter}:{conv.raw_id}")
            requests: list[Request] = []
            total = 0
            for seq, r in enumerate(conv.requests):
                date = _date_of(r.ts)
                msg = f"msg-{conv.raw_id}-{seq}"
                rid = request_id_for("github", msg, sid, f"line:{seq + 1}")
                infs: list[Inference] = []
                for n, (usage, kind) in enumerate(((r.compaction, InferenceKind.COMPACTION),
                                                   (r.usage, InferenceKind.MESSAGE))):
                    if usage is None:
                        continue
                    ctx = pricing.ctx(r.model, billing_path=conv.billing_path, tier=r.tier)
                    cost = pricing.point(usage, r.model, date, billing_path=conv.billing_path,
                                         tier=r.tier)
                    total += cost
                    infs.append(Inference(
                        inference_id=f"{rid}:{n}", kind=kind, usage=usage, pricing=ctx,
                        provider_reported_cost_nano=cost,
                        provider_reported_cost_basis="provider_estimate"))
                attempt = Attempt(
                    attempt_id=f"{rid}:a0", attempt_no=0, ts_start_ms=r.ts, ttft_ms=900,
                    duration_ms=6_000, outcome=Outcome.OK, http_status=200, error_type=None,
                    retry_layer=None, retry_after_ms=None, should_retry=None,
                    provider_request_id=None, provider_message_id=msg, model_served=r.model,
                    stop_reason="end_turn", inferences=tuple(infs))
                requests.append(Request(
                    request_id=rid, session_key=skey, lane_key=lkey, seq=seq,
                    attribution=attribution, params=RequestParams(model_requested=r.model),
                    attempts=(attempt,),
                    source=SourceRef(adapter=adapter, source_id=sid, locator=f"line:{seq + 1}",
                                     fidelity=Fidelity.NO_TTL_SPLIT,
                                     priority=_PRIORITY[adapter])))
            events = [LaneEvent(lane_key=lkey, ts_ms=ts, kind=kind,
                                attrs=tuple(sorted(attrs.items())))
                      for ts, kind, attrs in conv.events]
            last = conv.requests[-1].ts + 10_000
            events.append(LaneEvent(lane_key=lkey, ts_ms=last, kind=LaneEventKind.COST_STATE,
                                    attrs=(("reported_total_nano", total),
                                           ("reporter", _REPORTER[adapter]))))
            lane = Lane(lane_key=lkey, session_key=skey, kind=LaneKind.MAIN,
                        parent_lane_key=None, cache_scope_key=f"org:{_CHANNEL}:{ORG_A}",
                        requests=tuple(requests), events=tuple(events), ttl_observed="unknown")
            out.append(Session(session_key=skey, source_kind=_SOURCE_KIND[adapter],
                               attribution=attribution, lanes=(lane,),
                               started_ms=conv.requests[0].ts - 1, ended_ms=last))
    return out


# ---------------------------------------------------------------------------------------------
# the day loop: report rows, pool drawdown, activity
# ---------------------------------------------------------------------------------------------


@dataclass
class _Draft:
    """One report row before the pool discount: its key, tokens and gross."""

    date: str
    user: User | None
    label: str
    usage: UsageBuckets
    gross: int
    repo: str | None = None
    fixed: bool = False            # pseudo rows: gross is not token-priced


@dataclass
class _State:
    """Generation state shared by :func:`generate` and :func:`iter_records`."""

    seed: int
    people: list[User]
    dates: list[str]
    variants: tuple[str, ...]
    today: str
    pricing: _Pricing = field(default_factory=_Pricing)
    lane_rows: dict[tuple[str, str], dict[str, tuple[UsageBuckets, int]]] = field(
        default_factory=dict)
    _units: dict[tuple[str, str, str], int] = field(default_factory=dict)
    aw_rows: dict[str, tuple[UsageBuckets, int]] = field(default_factory=dict)
    remaining: dict[tuple[str, str], int] = field(default_factory=dict)
    used: set[tuple[str, str]] = field(default_factory=set)

    def units(self, user: User, month: str, part: str) -> int:
        key = (user.login, month, part)
        if key not in self._units:
            lo, hi = _RANGES[part]
            n = _rng(self.seed, "copilot-units", user.login, month, part).randint(lo, hi)
            if ("slack" in self.variants and month == self.dates[-1][:7]
                    and user.cost_center != CC_DATA):
                n = max(1, n // 2)
            self._units[key] = n
        return self._units[key]

    def pool(self, entity: str, month: str) -> int:
        """The true pool of an entity-month (seat SKU plans; the cap of the capped cost center)."""
        key = (entity, month)
        if key not in self.remaining:
            if entity != "enterprise":
                credits = CAP_CREDITS
            else:
                credits = Decimal(0)
                for u in self.people:
                    if _entity(u.cost_center) == "enterprise":
                        per, _ = _truth.allowance(u.plan, month)
                        credits = EXACT_CTX.add(credits, per)
            self.remaining[key] = decimal_to_nano(EXACT_CTX.multiply(credits, Decimal("0.01")))
        return self.remaining[key]


def _profile_rows(st: _State, user: User, date: str) -> list[tuple[str, int]]:
    month = date[:7]
    p = user.profile
    if p == "platform":
        return [(_SONNET, 3 if user.plan_mix else st.units(user, month, "heavy"))]
    if p == "payments":
        a = st.units(user, month, "opus")
        return [(_OPUS55 if date == _OPUS55_DAY else _OPUS48, a), (_SONNET, a + 2)]
    if p == "mobile":
        return [(_OPUS48_FAST, st.units(user, month, "fast")),
                (_SONNET, st.units(user, month, "fast_sonnet"))]
    if p == "data":
        rows = [(_GPT54, st.units(user, month, "gpt54"))]
        if date >= _GPT55_FROM:
            rows.append((_GPT55, st.units(user, month, "gpt55")))
        return rows
    if p == "core":
        return [(_SONNET_AUTO, st.units(user, month, "core"))]
    return [(_SONNET, st.units(user, month, p if p in _RANGES else "normal"))]


def _label_of(model: str) -> str:
    return {"claude-sonnet-5": _SONNET, "gpt-5.5": _GPT55}[model]


def _day_drafts(st: _State, date: str) -> list[_Draft]:
    """The report rows of one day, before the pool discount."""
    drafts: list[_Draft] = []
    business = _business(date)
    for u in st.people:
        if u.idle:
            continue
        rows: dict[str, list[object]] = {}
        if business:
            for label, units in _profile_rows(st, u, date):
                cm = normalize_copilot_model(label)
                unit = _UNIT_GPT if cm.model.startswith("gpt") else _UNIT_CLAUDE
                usage = _scale(unit, units)
                # a report row sums many requests: priced per unit (one request below every
                # long-context threshold), never as one band-sized request
                gross = units * st.pricing.point(unit, cm.model, date, routing=cm.routing,
                                                 speed=cm.speed)
                rows[label] = [usage, gross]
                st.used.add((cm.model if cm.speed == "standard" else f"{cm.model}#fast", date))
        for label, (usage, gross) in st.lane_rows.get((u.principal, date), {}).items():
            cur = rows.setdefault(label, [UsageBuckets(), 0])
            cur[0] = cur[0] + usage  # type: ignore[operator]
            cur[1] = cur[1] + gross  # type: ignore[operator]
        for label in sorted(rows):
            usage, gross = rows[label]
            drafts.append(_Draft(date, u, label, usage, gross))  # type: ignore[arg-type]
    if business:
        review = _scale(_UNIT_GPT, 2)
        drafts.append(_Draft(date, None, _REVIEW, review, _REVIEW_CREDITS_NANO,
                             repo=_h(_REPO_REVIEW), fixed=True))
        drafts.append(_Draft(date, None, _SONNET, _scale(_UNIT_CLAUDE, 2),
                             2 * st.pricing.point(_UNIT_CLAUDE, "claude-sonnet-5", date,
                                                  billing_path="copilot_direct"),
                             repo=_h(_REPO_CI)))
        st.used.add(("claude-sonnet-5", date))
    if date in st.aw_rows:
        usage, gross = st.aw_rows[date]
        drafts.append(_Draft(date, None, _SONNET, usage, gross, repo=_h(_REPO_AW)))
    return drafts


def _finality(date: str, today: str) -> str:
    lag = _facts.copilot_report_lag_days()
    cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=lag)).isoformat()
    return "provisional" if date > cutoff or date[:7] == today[:7] else "final"


def _cost_line(d: _Draft, discount: int, today: str) -> tuple[CostLine, RowFact]:
    cm = normalize_copilot_model(d.label)
    u = d.user
    principal = u.principal if u is not None else None
    cost_type = _POOLED if u is not None else _DIRECT
    workload = "copilot_code_review" if cm.pseudo == "code_review" else None
    line = CostLine(
        line_id=natural_id("cl", _REPORT, d.date, principal, cm.model or d.label, _SKU,
                           u.org if u else ORG_A, u.cost_center if u else None, d.repo),
        source_kind=_REPORT, date_utc=d.date, channel=_CHANNEL,
        workspace_id=u.org if u else ORG_A, description=f"{_SKU} {d.label}"[:128],
        model=cm.model or None, cost_type=cost_type, token_type=None, sku=_SKU,
        service_tier=None, inference_geo=None, endpoint_scope=None,
        amount_nano=d.gross - discount, list_amount_nano=d.gross,
        finality=_finality(d.date, today), principal=principal, fetched_ms=_ms(today),
        quantity=nano_to_credits_str(d.gross), unit="ai-credits",
        cost_center=u.cost_center if u else None, team=u.team if u else None, repo=d.repo,
        workload=workload, routing=cm.routing, speed=cm.speed, pseudo=cm.pseudo)
    fact = RowFact(date=d.date, principal=principal, team=u.team if u else None,
                   org=u.org if u else ORG_A, cost_center=u.cost_center if u else None,
                   cost_type=cost_type, model=cm.model, routing=cm.routing, speed=cm.speed,
                   pseudo=cm.pseudo, repo=d.repo, usage=d.usage, gross=d.gross,
                   discount=discount)
    return line, fact


def _allocate(st: _State, drafts: Sequence[_Draft]) -> list[int]:
    """Pool discounts of one day's rows: licensed rows draw their entity's pool in principal order;
    direct rows never draw it (``direct_draws_pool`` = no)."""
    out = [0] * len(drafts)
    order = sorted(range(len(drafts)), key=lambda i: (
        drafts[i].user is None, drafts[i].user.principal if drafts[i].user else "",
        drafts[i].label))
    for i in order:
        d = drafts[i]
        if d.user is None:
            continue
        key = (_entity(d.user.cost_center), d.date[:7])
        left = st.pool(*key)
        take = min(left, d.gross)
        st.remaining[key] = left - take
        out[i] = take
    return out


_AGG_DIMS = ("channel", "cost_center", "model", "organization", "pseudo", "routing", "sku", "speed",
             "team")


def _aggregates(lines: Sequence[CostLine], facts: Sequence[RowFact], date: str, today: str,
                convention: str) -> list[UsageAggregate]:
    """Token aggregates of one day under *convention* (``incl``: input holds reads and writes;
    ``undecidable``: no cache columns)."""
    groups: dict[tuple, list[tuple[CostLine, RowFact]]] = defaultdict(list)
    for line, fact in zip(lines, facts, strict=True):
        dims = {"channel": _CHANNEL, "organization": line.workspace_id, "team": line.team,
                "cost_center": line.cost_center, "model": line.model, "sku": line.sku,
                "routing": line.routing, "speed": line.speed, "pseudo": line.pseudo}
        groups[tuple(sorted((k, v) for k, v in dims.items() if v is not None))].append(
            (line, fact))
    out: list[UsageAggregate] = []
    start = _ms(date)
    for dims, members in sorted(groups.items()):
        total = sum((f.usage for _, f in members), UsageBuckets())
        if convention == "incl":
            usage = replace(total, uncached_input=total.total_input)
        elif convention == "undecidable":
            usage = UsageBuckets(uncached_input=total.uncached_input, output=total.output)
        else:
            usage = total
        out.append(UsageAggregate(
            agg_id=natural_id("ag", _REPORT, date, *(f"{k}={v}" for k, v in dims)),
            source_kind=_REPORT, bucket_start_ms=start, bucket_end_ms=start + _DAY_MS,
            dims=dims, usage=usage, reported_cost_nano=sum(ln.amount_nano for ln, _ in members),
            reported_cost_basis="invoice",
            list_cost_nano=sum(ln.list_amount_nano or 0 for ln, _ in members),
            finality=members[0][0].finality, fetched_ms=_ms(today)))
    return out


def _coverage(lines: Sequence[CostLine], date: str, source: str, today: str) -> UsageAggregate:
    start = _ms(date)
    return UsageAggregate(
        agg_id=natural_id("ag", "github.ai_usage_report.coverage", source, date),
        source_kind="github.ai_usage_report.coverage", bucket_start_ms=start,
        bucket_end_ms=start + _DAY_MS, dims=(("channel", _CHANNEL), ("source", source)),
        usage=UsageBuckets(), reported_cost_nano=sum(c.amount_nano for c in lines),
        reported_cost_basis="invoice", list_cost_nano=sum(c.list_amount_nano or 0 for c in lines),
        finality=_finality(date, today), fetched_ms=_ms(today))


def _activity(st: _State, date: str, drafts: Sequence[_Draft]) -> list[ActivityDay]:
    """Usage-metrics user-days: interactions 10 per unit, the team's editor split of ``ide:*``,
    CLI counts for the agents team; ``reported_cost_nano`` = the user's gross of the day."""
    per: dict[str, list[_Draft]] = defaultdict(list)
    for d in drafts:
        if d.user is not None:
            per[d.user.principal].append(d)
    out: list[ActivityDay] = []
    for u in st.people:
        items = per.get(u.principal)
        if not items or not _business(date):
            continue
        k = sum(d.usage.output for d in items) // 5_000 or 1
        counts: dict[str, int] = {"code_generation": 5 * k, "code_acceptance": 2 * k,
                                  "loc_suggested_add": 40 * k, "loc_added": 15 * k,
                                  "mcp_distinct": 2}
        flags = ["used_chat", "used_agent"]
        if u.editor == "cli":
            counts.update({"cli_sessions": 1, "cli_requests": 4 * k,
                           "cli_prompt_tokens": 80_000 * k})
            flags = ["used_cli"]
        else:
            counts["interactions"] = 10 * k
            split = {"mix": (7, 3), "vscode": (10, 0), "jetbrains": (1, 9)}[u.editor]
            counts.update({f"ide:{name}": n * k for name, n in zip(("vscode", "intellij"), split)
                           if n})
        out.append(ActivityDay(
            date_utc=date, product=_CHANNEL, principal=u.principal, team=u.team,
            cost_center=u.cost_center, reported_cost_nano=sum(d.gross for d in items),
            counts=tuple(sorted(counts.items())), flags=tuple(sorted(flags)),
            fetched_ms=_ms(st.today)))
    return out


def _outcomes(date: str, activity: Sequence[ActivityDay]) -> list[OutcomeAggregate]:
    """Team outcome rows (teams with ≥ 5 active users that day) and the enterprise PR row."""
    teams: dict[str, int] = defaultdict(int)
    for a in activity:
        teams[a.team or ""] += 1
    out = [OutcomeAggregate(date_utc=date, team=t, n_users=n, sessions=n, commits=2 * n,
                            pull_requests=n // 2, lines_added=150 * n, lines_removed=60 * n,
                            edits_accepted=8 * n, edits_rejected=3 * n,
                            source_kind="github.copilot_metrics")
           for t, n in sorted(teams.items()) if t and n >= 5]
    if activity:
        n = len(activity)
        out.append(OutcomeAggregate(
            date_utc=date, team="(enterprise)", n_users=n, sessions=n, commits=2 * n,
            pull_requests=n // 2, lines_added=150 * n, lines_removed=60 * n,
            edits_accepted=8 * n, edits_rejected=3 * n, source_kind="github.copilot_metrics",
            extra=(("prs_created_by_copilot", 2), ("prs_merged", n // 3),
                   ("prs_merged_created_by_copilot", 1), ("prs_reviewed_by_copilot", n // 4))))
    return out


# ---------------------------------------------------------------------------------------------
# seats, detailed report, configuration
# ---------------------------------------------------------------------------------------------


def _months(dates: Sequence[str]) -> list[str]:
    return sorted({d[:7] for d in dates})


def _snapshots(dates: Sequence[str]) -> list[str]:
    """One seat snapshot per month: its last day, or the window's end."""
    return [min(_last_day(m), dates[-1]) for m in _months(dates)]


def _licenses(st: _State, *, view: str) -> list[LicenseSnapshot]:
    out: list[LicenseSnapshot] = []
    activity_report = view in ("unknown", "quota")
    for snap in _snapshots(st.dates):
        for u in st.people:
            plan = u.plan
            if view == "conflict" and u.org == ORG_B:
                plan = "enterprise"
            out.append(LicenseSnapshot(
                snapshot_date=snap, product=_CHANNEL,
                plan="unknown" if activity_report else plan, principal=u.principal,
                team=u.team, cost_center=u.cost_center,
                org=None if activity_report else u.org,
                seat_created=None if activity_report else _SEAT_CREATED,
                pending_cancellation=None,
                last_activity_bucket="none_90d" if u.idle else "0-7",
                last_activity_surface=None if u.idle else u.surface,
                last_authenticated_bucket="31-90" if u.idle else "0-7",
                assigned_via_team=None if activity_report else u.via_team,
                fetched_ms=_ms(snap) + 12 * _HOUR_MS,
                source_kind=("github.copilot_activity_report" if activity_report
                             else "github.copilot_seats")))
    return out


_SEAT_SKU = {"business": "copilot_for_business", "enterprise": "copilot_enterprise"}


def _metered_lines(st: _State, convs: Sequence[_Conv], *, seats: bool) -> list[CostLine]:
    """Detailed-report lines: seat lines (the 1st of each month), Copilot-workload Actions lines
    (code review on ``linux_16_core``, the agentic workflow on ``actions_linux``) and one sandbox
    line per month."""
    out: list[CostLine] = []
    today_ms = _ms(st.today)
    runner = {r.sku: r.usd_per_minute for r in _facts.copilot_runner_rates().values()}

    def line(date: str, *, channel: str, cost_type: str, sku: str, quantity: Decimal,
             unit_usd: Decimal, unit: str, workload: str | None = None, repo: str | None = None,
             workflow: str | None = None, cc: str | None = None, org: str = ORG_A) -> CostLine:
        gross = decimal_to_nano(EXACT_CTX.multiply(quantity, unit_usd))
        return CostLine(
            line_id=natural_id("cl", _METERED, date, sku, org, cc, repo, workflow, workload),
            source_kind=_METERED, date_utc=date, channel=channel, workspace_id=org,
            description=f"{sku} {unit}", model=None, cost_type=cost_type, token_type=None,
            sku=sku, service_tier=None, inference_geo=None, endpoint_scope=None,
            amount_nano=gross, list_amount_nano=gross, finality=_finality(date, st.today),
            fetched_ms=today_ms, quantity=f"{quantity.normalize():f}", unit=unit,
            cost_center=cc, repo=repo, workload=workload, workflow=workflow)

    for month in _months(st.dates):
        if seats:
            counts: dict[tuple[str, str | None, str], int] = defaultdict(int)
            for u in st.people:
                counts[(u.org, u.cost_center, u.plan)] += 1
            for (org, cc, plan), n in sorted(counts.items(), key=lambda kv: tuple(
                    v or "" for v in kv[0])):
                out.append(line(f"{month}-01", channel=_CHANNEL, cost_type="seat",
                                sku=_SEAT_SKU[plan], quantity=Decimal(n),
                                unit_usd=_facts.copilot_plans()[plan].seat_usd_per_month,
                                unit="seat-months", cc=cc, org=org))
        sandbox_day = f"{month}-15"
        if st.dates[0] <= sandbox_day <= st.dates[-1]:
            out.append(line(sandbox_day, channel="github_sandbox", cost_type="sandbox",
                            sku="sandbox_linux", quantity=Decimal(120),
                            unit_usd=_SANDBOX_USD_PER_MINUTE, unit="minutes"))
    for date in st.dates:
        if _business(date):
            out.append(line(date, channel="github_actions", cost_type="actions",
                            sku=_REVIEW_RUNNER, quantity=_REVIEW_MINUTES,
                            unit_usd=runner[_REVIEW_RUNNER], unit="minutes",
                            workload="copilot_code_review", repo=_h(_REPO_REVIEW)))
    for conv in convs:
        if conv.agent_product != "copilot_gh_aw":
            continue
        date = _date_of(conv.requests[0].ts)
        out.append(line(date, channel="github_actions", cost_type="actions", sku=_AW_RUNNER,
                        quantity=_AW_MINUTES, unit_usd=runner[_AW_RUNNER], unit="minutes",
                        workload="agentic_workflow", repo=_h(_REPO_AW), workflow=_h(_AW_PATH)))
    return out


def _config(st: _State, *, view: str, active_by_month: Mapping[str, set[str]]
            ) -> list[ConfigSnapshot]:
    """Configuration of the view: org seat policies, the capped cost center, budgets and run
    flags (pulled data); the handoff views carry only the answers file's run flags."""
    snap = _ms(st.dates[-1]) + 12 * _HOUR_MS
    out: list[ConfigSnapshot] = []
    flags: dict[str, str | int | bool | None] = {"promo_eligible": True, "compliance": "none"}
    if "volume" in st.variants:
        flags.update({"billing_mode.enterprise": "volume", f"billing_mode.cc:{CC_DATA}": "volume"})
    if view in ("unknown", "quota"):
        out.append(ConfigSnapshot(snapshot_ms=snap, source_kind="tokenbill.admin_answers",
                                  kind="run_flags", entity_id="admin_answers",
                                  attrs=tuple(sorted(flags.items())), fetched_ms=snap))
        if view == "quota":
            plans = {u.principal: u for u in st.people}
            for month, active in sorted(active_by_month.items()):
                per: dict[tuple[str, int], int] = defaultdict(int)
                for p in active:
                    u = plans[p]
                    per[(u.org, int(_truth.allowance(u.plan, month)[0]))] += 1
                for (org, quota), n in sorted(per.items()):
                    out.append(ConfigSnapshot(
                        snapshot_ms=_ms(_last_day(month)), source_kind=_REPORT,
                        kind="plan_quota", entity_id=f"org:{org}",
                        attrs=(("month", month), ("n_users", n), ("quota", str(quota))),
                        fetched_ms=snap))
        return out
    out.append(ConfigSnapshot(snapshot_ms=snap, source_kind="tokenbill.cli", kind="run_flags",
                              entity_id="run", attrs=tuple(sorted(flags.items())),
                              fetched_ms=snap))
    if view == "conflict":
        out.append(ConfigSnapshot(snapshot_ms=snap, source_kind="tokenbill.admin_answers",
                                  kind="run_flags", entity_id="admin_answers",
                                  attrs=((f"plan.org:{ORG_B}", "enterprise"),), fetched_ms=snap))
    for org, policy, plan in ((ORG_A, "assign_selected", None), (ORG_B, "assign_all", "business")):
        attrs: dict[str, str | int | bool | None] = {"seat_management_setting": policy}
        if plan is not None:
            attrs["plan_type"] = plan
        out.append(ConfigSnapshot(snapshot_ms=snap, source_kind="github.org_copilot_settings",
                                  kind="org_settings", entity_id=f"org:{org}",
                                  attrs=tuple(sorted(attrs.items())), fetched_ms=snap))
    n_cc = sum(1 for u in st.people if u.cost_center == CC_DATA)
    out.append(ConfigSnapshot(snapshot_ms=snap, source_kind="github.cost_centers",
                              kind="cost_center", entity_id=f"cc:{CC_DATA}",
                              attrs=(("n_users", n_cc), ("pool_enabled", True),
                                     ("pool_target_credits", f"{CAP_CREDITS:f}"),
                                     ("state", "active")), fetched_ms=snap))
    zero = [u for u in st.people if u.zero_budget]
    for n, u in enumerate(zero):
        out.append(ConfigSnapshot(snapshot_ms=snap, source_kind="github.budgets", kind="budget",
                                  entity_id=f"budget:zu-{n + 1}",
                                  attrs=(("amount_nano", 0), ("prevent_further_usage", True),
                                         ("scope", "user"), ("sku", "ai_credits"),
                                         ("target", "enterprise"), ("team", u.team),
                                         ("type", "BundlePricing")), fetched_ms=snap))
    out.append(ConfigSnapshot(snapshot_ms=snap, source_kind="github.budgets", kind="budget",
                              entity_id="budget:ent-ai",
                              attrs=(("amount_nano", 2_000 * 10**9),
                                     ("prevent_further_usage", False), ("scope", "enterprise"),
                                     ("sku", "ai_credits"), ("target", "enterprise"),
                                     ("type", "BundlePricing"), ("will_alert", True)),
                              fetched_ms=snap))
    return out


def _gh_aw_aggregates(convs: Sequence[_Conv], today: str) -> list[UsageAggregate]:
    out = []
    for conv in convs:
        if conv.agent_product != "copilot_gh_aw":
            continue
        usage = sum((r.usage for r in conv.requests), UsageBuckets())
        start = conv.requests[0].ts
        source = _source_id(f"gh-aw-token-usage:{conv.raw_id}")
        dims = tuple(sorted({"channel": _CHANNEL, "model": "claude-sonnet-5",
                             "organization": ORG_A, "repo": _h(_REPO_AW),
                             "workflow": _h(_AW_PATH), "source": source}.items()))
        out.append(UsageAggregate(
            agg_id=natural_id("ag", "gh_aw.run", conv.raw_id), source_kind="gh_aw.run",
            bucket_start_ms=start, bucket_end_ms=conv.requests[-1].ts + 60_000, dims=dims,
            usage=usage, finality="final", fetched_ms=_ms(today)))
    return out


# ---------------------------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------------------------


def _check(seed: object, start: str, end: str, variants: Iterable[str],
           conventions: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...], list[str]]:
    if type(seed) is not int:
        raise UsageError("seed: expected an int")
    try:
        dates = _dates(start, end)
    except (TypeError, ValueError):
        raise UsageError("start / end: expected YYYY-MM-DD dates") from None
    if not dates:
        raise UsageError("start must not be after end")
    var = tuple(sorted(set(variants)))
    unknown = [v for v in var if v not in VARIANTS]
    if unknown:
        raise UsageError(f"unknown variant(s): {', '.join(unknown)}")
    if "plan_quota" in var and "plan_unknown" not in var:
        var = tuple(sorted({*var, "plan_unknown"}))
    if "plan_conflict" in var and "plan_unknown" in var:
        raise UsageError("plan_conflict and plan_unknown are exclusive views")
    conv = tuple(c for c in CONVENTIONS if c in set(conventions))
    if not conv or set(conventions) - set(CONVENTIONS):
        raise UsageError(f"conventions: a non-empty subset of {', '.join(CONVENTIONS)}")
    return var, conv, dates


def _view(variants: Sequence[str]) -> str:
    if "plan_quota" in variants:
        return "quota"
    if "plan_unknown" in variants:
        return "unknown"
    return "conflict" if "plan_conflict" in variants else "known"


def _guard(used: Iterable[tuple[str, str]]) -> None:
    """The pricing guard: a row per model and date, away from K-dated changes (±2 days)."""
    changes = _truth.k_dated_changes()
    for key, date in sorted(used):
        model = key.partition("#")[0]
        if model == "gpt-5.6-sol" or _truth.rate_row(model, date) is None:
            raise UsageError(f"pricing guard: {model} has no rate row on {date}")
        if model == "claude-opus-5-5" and date != _OPUS55_DAY:
            raise UsageError("pricing guard: Opus 5.5 is used only on 2026-09-22")
        day = _dt.date.fromisoformat(date)
        for k in changes.get(model, ()):
            if abs((day - _dt.date.fromisoformat(k)).days) <= 2:
                raise UsageError(f"pricing guard: {model} within 2 days of a K-dated change")


def _state(seed: int, users: int, dates: list[str], variants: tuple[str, ...],
           today: str) -> _State:
    return _State(seed=seed, people=_users(users), dates=dates, variants=variants, today=today)


def _day(st: _State, date: str) -> tuple[list[CostLine], list[RowFact], list[ActivityDay]]:
    drafts = _day_drafts(st, date)
    discounts = _allocate(st, drafts)
    lines: list[CostLine] = []
    facts: list[RowFact] = []
    for d, disc in zip(drafts, discounts, strict=True):
        line, fact = _cost_line(d, disc, st.today)
        lines.append(line)
        facts.append(fact)
    return lines, facts, _activity(st, date, drafts)


def iter_records(seed: int = 7, *, users: int = 5_000, start: str = "2026-08-24",
                 end: str = WINDOW_END, today: str = TODAY) -> Iterator[object]:
    """Scale mode (addendum §17 performance gates): stream the report rows, token and coverage
    aggregates, activity days and seats of a *users*-seat enterprise day by day in bounded memory
    (no collector lanes, no truth)."""
    variants, _, dates = _check(seed, start, end, (), ("excl",))
    st = _state(seed, users, dates, variants, today)
    source = _source_id("ai_usage_excl.csv")
    for date in dates:
        lines, facts, activity = _day(st, date)
        yield from lines
        if lines:
            yield from _aggregates(lines, facts, date, today, "excl")
            yield _coverage(lines, date, source, today)
        yield from activity
    yield from _licenses(st, view="known")


def generate(seed: int = 7, *, users: int = BASE_USERS, start: str = WINDOW_START,
             end: str = WINDOW_END, conventions: Iterable[str] = CONVENTIONS,
             variants: Iterable[str] = (), today: str = TODAY) -> CopilotWorld:
    """Generate the synthetic Copilot enterprise (see the module docstring) and its truth."""
    var, convs_wanted, dates = _check(seed, start, end, variants, conventions)
    st = _state(seed, users, dates, var, today)
    view = _view(var)
    convs = _plan_conversations(st.people, dates)
    sessions = _sessions(convs, st.pricing)
    for conv in convs:
        for r in conv.requests:
            date = _date_of(r.ts)
            usage = r.usage + r.compaction if r.compaction is not None else r.usage
            gross = sum(st.pricing.point(u, r.model, date, billing_path=conv.billing_path,
                                         tier=r.tier)
                        for u in (r.usage, r.compaction) if u is not None)
            st.used.add((r.model, date))
            if conv.user is None:
                prev = st.aw_rows.get(date, (UsageBuckets(), 0))
                st.aw_rows[date] = (prev[0] + usage, prev[1] + gross)
            else:
                per = st.lane_rows.setdefault((conv.user.principal, date), {})
                prev = per.get(_label_of(r.model), (UsageBuckets(), 0))
                per[_label_of(r.model)] = (prev[0] + usage, prev[1] + gross)
    all_lines: list[CostLine] = []
    all_facts: list[RowFact] = []
    activity: list[ActivityDay] = []
    outcomes: list[OutcomeAggregate] = []
    sources = {c: _source_id(f"ai_usage_{c}.csv") for c in convs_wanted}
    report_aggs: dict[str, list[UsageAggregate]] = {c: [] for c in convs_wanted}
    for date in dates:
        lines, facts, acts = _day(st, date)
        all_lines.extend(lines)
        all_facts.extend(facts)
        activity.extend(acts)
        outcomes.extend(_outcomes(date, acts))
        if lines:
            for c in convs_wanted:
                report_aggs[c].extend(_aggregates(lines, facts, date, today, c))
                report_aggs[c].append(_coverage(lines, date, sources[c], today))
    _guard(st.used)
    active: dict[str, set[str]] = defaultdict(set)
    for f in all_facts:
        if f.principal is not None:
            active[f.date[:7]].add(f.principal)
    metered = _metered_lines(st, convs, seats=view not in ("unknown", "quota"))
    gh_aw = _gh_aw_aggregates(convs, today)
    records = CopilotRecords(
        cost_lines=tuple(all_lines + metered),
        aggregates=tuple(report_aggs.get("excl", report_aggs[convs_wanted[0]]) + gh_aw),
        licenses=tuple(_licenses(st, view=view)), activity=tuple(activity),
        config=tuple(_config(st, view=view, active_by_month=active)), outcomes=tuple(outcomes),
        sessions=tuple(sessions), logins=tuple(sorted((u.principal, u.login)
                                                      for u in st.people)))
    reports = {c: ReportSet(convention=c, source_id=sources[c], cost_lines=tuple(all_lines),
                            aggregates=tuple(report_aggs[c])) for c in convs_wanted}
    truth = _build_truth(st, view, all_facts, metered, gh_aw, sessions, convs_wanted)
    return CopilotWorld(
        seed=seed, users=users, start=dates[0], end=dates[-1], today=today, variants=var,
        conventions=convs_wanted, records=records, reports=reports, truth=truth,
        team_map={u.login: u.team for u in st.people},
        cost_center_map={u.login: u.cost_center for u in st.people if u.cost_center},
        people=tuple(st.people))


# ---------------------------------------------------------------------------------------------
# truth assembly (closed forms in copilot_truth)
# ---------------------------------------------------------------------------------------------


def _seat_view(st: _State, view: str, facts: Sequence[RowFact]
               ) -> dict[tuple[str, str], dict[str, int]]:
    """What the analyst's plan sources say about seats per (entity, month)."""
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    months = _months(st.dates)
    if view in ("known", "conflict"):
        for m in months:
            for u in st.people:
                out[(_entity(u.cost_center), m)][u.plan] += 1
        return out
    active: dict[str, set[str]] = defaultdict(set)
    for f in facts:
        if f.principal is not None:
            active[f.date[:7]].add(f.principal)
    for m in months:
        for u in st.people:
            plan = u.plan if view == "quota" and u.principal in active[m] else "unknown"
            out[("enterprise", m)][plan] += 1
    return out


def _category(model: str, date: str) -> str | None:
    row = _truth.rate_row(model, date)
    if row is None:
        return None
    for part in row.notes.split(";"):
        part = part.strip()
        if part.startswith("category "):
            return part[len("category "):]
    return None


def _build_truth(st: _State, view: str, facts: Sequence[RowFact], metered: Sequence[CostLine],
                 gh_aw: Sequence[UsageAggregate], sessions: Sequence[Session],
                 conventions: Sequence[str]) -> CopilotTruth:
    capped = view in ("known", "conflict")
    lag = _facts.copilot_report_lag_days()
    caps = {CC_DATA: CAP_CREDITS} if capped else {}
    pairs = [(_entity(f.cost_center, capped), f) for f in facts]
    pms = _truth.pool_month_truths(pairs, _seat_view(st, view, facts), caps, today=st.today,
                                   lag=lag)
    mode = "volume" if "volume" in st.variants else "metered"
    latest = max(pm.month for pm in pms)

    def pm_of(entity: str, scen: str | None, month: str = latest) -> _truth.TruthPoolMonth:
        return next(p for p in pms if p.entity_id == entity and p.month == month
                    and p.plan_scenario == scen)

    teams = {t.name: [u for u in st.people if u.team == t.name] for t in TEAMS}
    idle = {t: sum(1 for u in us if u.idle) for t, us in teams.items()}
    idle_seats: dict[str, dict[str, int]] = {}
    by_scen: dict[tuple[str, str | None], int | None] = {}
    idle_saving = auto_saving = None
    for team, us in teams.items():
        n = idle[team]
        if not n:
            continue
        kind = ("unknown" if view in ("unknown", "quota") else "auto" if us[0].org == ORG_B
                else "team" if us[0].via_team else "removable")
        idle_seats[team] = {kind: n}
        entity = _entity(us[0].cost_center, capped)
        scens: tuple[str | None, ...] = (("business", "enterprise")
                                         if view in ("unknown", "quota") else (None,))
        for scen in scens:
            if kind == "team":
                by_scen[(team, scen)] = None
                continue
            plan = "unknown" if scen is not None else us[0].plan
            got = _truth.seat_change_saving(pm_of(entity, scen), {plan: -n}, billing_mode=mode)
            by_scen[(team, scen)] = got[0] if got is not None else None
    if view in ("known", "conflict"):
        idle_saving = by_scen.get(("platform", None))
        auto_saving = by_scen.get(("ops", None))
    reach = {"mix": Fraction(7, 10), "vscode": Fraction(1), "jetbrains": Fraction(1, 10),
             "cli": Fraction(1)}
    team_reach = {t.name: reach[t.editor] for t in TEAMS}
    shares = {t.name: ({"vscode": Fraction(7, 10), "jetbrains": Fraction(3, 10)}
                       if t.editor == "mix" else {"vscode": Fraction(1)} if t.editor == "vscode"
                       else {"jetbrains": Fraction(9, 10), "vscode": Fraction(1, 10)}
                       if t.editor == "jetbrains" else {"cli": Fraction(1)}) for t in TEAMS}
    fast = 0
    premium: dict[str, int] = defaultdict(int)
    premium_credits: dict[str, int] = defaultdict(int)
    team_credits: dict[str, int] = defaultdict(int)
    auto: dict[str, Fraction] = defaultdict(Fraction)
    migration: dict[tuple[str, str], int] = defaultdict(int)
    remaps = _facts.copilot_remaps()
    retire = _facts.copilot_retirements()
    for f in facts:
        team = f.team
        if team is not None:
            team_credits[team] += f.gross
        if f.pseudo is not None or not f.model:
            continue
        if f.speed == "fast":
            fast += ((_truth.price(f.usage, f.model, f.date, routing=f.routing, speed="fast") or 0)
                     - (_truth.price(f.usage, f.model, f.date, routing=f.routing) or 0))
        remap = remaps.get(f.model)
        if team is not None and remap is not None and _category(f.model, f.date) == "Powerful":
            premium[team] += ((_truth.price(f.usage, f.model, f.date, routing=f.routing) or 0)
                              - (_truth.price(f.usage, remap.target, f.date,
                                              routing=f.routing) or 0))
            premium_credits[team] += f.gross
        if team is not None and f.routing == "direct":
            auto[team] += Fraction(f.gross, 10) * team_reach[team]
        info = retire.get(f.model)
        if info is not None and info.successor and st.today <= info.retire_on:
            alt_fact = remaps.get(info.successor)
            alt = alt_fact.target if alt_fact is not None else f.model
            migration[(_entity(f.cost_center, capped), f.model)] += (
                (_truth.price(f.usage, info.successor, st.today, routing=f.routing) or 0)
                - (_truth.price(f.usage, alt, st.today, routing=f.routing) or 0))
    runner_rate = _facts.copilot_runner_rates()["actions_linux"].usd_per_minute
    larger = [c for c in metered if c.sku == _REVIEW_RUNNER]
    net = sum(c.amount_nano for c in larger)
    minutes = sum((Decimal(c.quantity or "0") for c in larger), Decimal(0))
    low = max(0, net - decimal_to_nano(EXACT_CTX.multiply(minutes, runner_rate)))
    runs = tuple(_truth.price(a.usage, "claude-sonnet-5", _date_of(a.bucket_start_ms)) or 0
                 for a in gh_aw)
    promo_months = sorted({p.month for p in pms if p.promo})
    cliff: dict[str, int] = {}
    if promo_months and latest > promo_months[-1]:
        for p in pms:
            if p.month != latest:
                continue
            before = next((q for q in pms if q.month == promo_months[-1]
                           and q.entity_id == p.entity_id and q.plan_scenario == p.plan_scenario),
                          None)
            if before is None:
                continue
            use = before.consumed_report_nano
            delta = max(0, use - p.pool_nano) - max(0, use - before.pool_nano)
            cliff[p.entity_id + (f"|{p.plan_scenario}" if p.plan_scenario else "")] = delta
    cap_range = {p.month: (0, p.overage_observed_nano) for p in pms
                 if p.entity_id == f"cc:{CC_DATA}"}
    plans: list[tuple[str, str, str, str, bool]] = []
    for p in pms:
        if p.plan_scenario == "enterprise":
            continue
        seats = dict(p.seats)
        if view in ("unknown", "quota"):
            plan, source = "unknown", ("report_quota" if view == "quota" else "none")
        else:
            known = {k for k in seats if k != "unknown"}
            plan = "mixed" if len(known) > 1 else next(iter(known))
            source = "seat_lines"
        plans.append((p.entity_id, p.month, plan, source,
                      view == "conflict" and p.entity_id == "enterprise"))
    lanes = _truth.lane_truth(sessions)
    return CopilotTruth(
        today=st.today, variants=st.variants,
        team_sizes=tuple((t, len(us)) for t, us in teams.items()), control_team="core",
        tiny_team="tiny", plan_view=view, plans=tuple(plans), pool_months=pms,
        idle_seats=idle_seats, idle_seat_saving_nano=idle_saving,
        seat_auto_assign_nano=auto_saving, idle_seat_saving_by_scenario=by_scen,
        plan_mix_seats={t: sum(1 for u in us if u.plan_mix) for t, us in teams.items()
                        if any(u.plan_mix for u in us)},
        zero_user_budgets={t: sum(1 for u in us if u.zero_budget) for t, us in teams.items()
                           if any(u.zero_budget for u in us)},
        fast_premium_nano=fast, premium_remap_saving_by_team=dict(premium),
        premium_share_by_team={t: Fraction(v, team_credits[t]) for t, v in premium_credits.items()},
        auto_reach=team_reach,
        auto_saving_by_team={t: int(v) if v.denominator == 1 else round(v)
                             for t, v in auto.items()},
        editor_share=shares, forced_migration_delta=dict(migration),
        direct_org_net_nano=sum(f.net for f in facts if f.cost_type == _DIRECT),
        larger_runner=(net, low, net), agentic_run_prices=runs,
        agentic_run_p50_nano=_truth.nearest_rank(runs, 1, 2) if runs else 0,
        agentic_run_p90_nano=_truth.nearest_rank(runs, 9, 10) if runs else 0,
        promo_cliff_nano=cliff, cap_overage_range=cap_range, lanes=lanes,
        report_convention={c: c for c in conventions},
        extra={"seat_facts": tuple(SeatFact(
            principal=u.principal, team=u.team, org=u.org, cost_center=u.cost_center,
            plan=u.plan, via_team=u.via_team, idle=u.idle, plan_mix=u.plan_mix,
            zero_budget=u.zero_budget, surface=u.surface) for u in st.people)})
