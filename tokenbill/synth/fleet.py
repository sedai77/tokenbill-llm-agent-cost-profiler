"""The deterministic synthetic fleet behind ``tokenbill demo --fleet`` (SPEC §18, SYNTH-FLEET).

:func:`generate` builds eleven teams (61 developers by default) over a 28-day window with planted
waste, the provider-side records an organisation would also hold (Admin usage/cost pages, Claude
Code Analytics, an AWS CUR 2.0 export) and — with ``out_dir`` — schema-true source files for every
adapter family (:mod:`tokenbill.synth.writers`). The truth of every plant is computed at generation
time by the closed forms of :mod:`tokenbill.synth.truth`, independently of REPLAY and DETECT.

Canonical records are generated directly (no adapter round trip). Everything is a pure function of
``(seed, devs, days)``: randomness only through :func:`tokenbill.common.rng` scoped by team,
developer and day; ids through :func:`tokenbill.core.ids.stable_id`; prices only in
:mod:`tokenbill.synth.truth` (``core.testing.FakePricer``).

The window starts on 2026-09-22 (the first day Opus 5.5 is priced); ``today`` is the last day of
the window plus 25 days, so the last six days are provisional (revisable) and earlier days closed.

Team table (SPEC §18): ``platform`` gateway strips caching · ``payments`` 5m main lanes with 12% of
gaps in 5–60 min · ``search`` subscription sessions to ~900k · ``mobile`` subscription cold resumes
(two overage days) · ``infra`` sticky fast/xhigh devs plus the cross-team extras (refusal
fallbacks with declined outputs 0 and 6, a placeholder-usage call, a hidden compaction, one call on
an unpriced model) · ``data`` routing, same-tier, default model/effort and an Opus 4.8 → Opus 5
migration (plus the legacy Priority-tier bucket) · ``ops`` Bedrock regional with a runaway loop ·
``ci-bots`` claude-code-action runs with truncation and single-shot service calls · ``agents``
Agent SDK lanes with 7-minute gaps, warm context-edit churn and 14k tokens of tool definitions ·
``core`` healthy control · ``tiny`` three developers.

Scale mode (``scale_requests=N``) streams exactly ``N`` canonical requests (team patterns cycled
over a growing virtual population) in bounded memory; it computes no plant truth and writes no
files.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import random
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tokenbill.common import rng as _rng
from tokenbill.core.builders import CANARY
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import hmac_hex, key_id, pseudonym, request_id_for, stable_id
from tokenbill.core.lanes import group_lanes
from tokenbill.core.records import (
    AppendedItem,
    Attempt,
    Attribution,
    BlockRef,
    Breakpoint,
    ContentFingerprint,
    CostLine,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Outcome,
    OutcomeAggregate,
    PricingContext,
    Request,
    RequestParams,
    Session,
    UsageAggregate,
    UsageBuckets,
    UsageSource,
    WorkloadClass,
)
from tokenbill.core.types import IngestOptions, IngestResult, SourceInfo

if TYPE_CHECKING:   # synth.truth imports this module lazily; the annotation needs no import
    from tokenbill.synth.truth import FleetTruth

__all__ = [
    "COMPACTION_POST_TOKENS",
    "FLEET_FP_KEY",
    "FLEET_NAME_KEY",
    "FLEET_ORG_KEY",
    "FLEET_WORKSPACES",
    "TEAMS",
    "WINDOW_START",
    "DevInfo",
    "FleetHints",
    "FleetWorld",
    "TeamSpec",
    "fleet_ingest_options",
    "generate",
    "team_sizes",
]

# ---------------------------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------------------------

WINDOW_START = "2026-09-22"
#: Synthetic, public keys (demo only; they protect nothing). The canonical records carry principals
#: ``pseudonym(FLEET_ORG_KEY, "p", ref)`` and names ``pseudonym(FLEET_NAME_KEY, "h", value)``; block
#: hashes of the agents' fingerprints are HMACs under ``FLEET_FP_KEY``.
FLEET_ORG_KEY = hashlib.sha256(b"tokenbill synthetic fleet: org key (public, demo only)").digest()
FLEET_NAME_KEY = hashlib.sha256(b"tokenbill synthetic fleet: name key (public, demo only)").digest()
FLEET_FP_KEY = hashlib.sha256(b"tokenbill synthetic fleet: block key (public, demo only)").digest()
_FP_KEY_ID = key_id(FLEET_FP_KEY)
#: Every COMPACTION event in the fleet reports this summary size, so the compaction-window replay's
#: ``S_c`` (org median ``post_tokens``, else the default) is 20,283 whichever lanes it sees.
COMPACTION_POST_TOKENS = 20_283
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CC_VERSION = "2.1.270"
_CI_VERSIONS = ("2.1.268", "2.1.270", "2.1.271")
_AWS_ACCOUNT = "111122223333"
_OPUS55, _OPUS5, _OPUS48 = "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"
_FABLE5, _SONNET5 = "claude-fable-5", "claude-sonnet-5"
_UNPRICED_MODEL = "claude-sonnet-5-5"   # announced 2026-09-22, not priced (SPEC §19.1)
_TOOL_DEF_TOKENS = 14_000
_TOOL_DEFS = 20
_SYSTEM_TOKENS = 3_000


@dataclass(frozen=True, slots=True)
class TeamSpec:
    """One row of the SPEC §18 team table."""

    name: str
    devs: int
    channel: str
    billing_path: str
    plant: str


TEAMS: tuple[TeamSpec, ...] = (
    TeamSpec("platform", 6, "anthropic_api", "api_key", "gateway strips cache_control"),
    TeamSpec("payments", 7, "anthropic_api", "api_key", "5m main lanes, 12% of gaps 5-60 min"),
    TeamSpec("search", 6, "anthropic_api", "subscription", "sessions grow to ~900k"),
    TeamSpec("mobile", 6, "anthropic_api", "subscription", "cold resumes; two overage days"),
    TeamSpec("infra", 6, "anthropic_api", "api_key", "sticky fast / xhigh; cross-team extras"),
    TeamSpec("data", 6, "anthropic_api", "api_key", "routing, defaults, Opus 4.8 -> 5 migration"),
    TeamSpec("ops", 5, "bedrock", "bedrock", "runaway loop; regional endpoint; CUR"),
    TeamSpec("ci-bots", 5, "anthropic_api", "api_key", "CI runs, truncation, single-shot calls"),
    TeamSpec("agents", 5, "anthropic_api", "api_key", "SDK keepalive, edit churn, tool defs"),
    TeamSpec("core", 6, "anthropic_api", "api_key", "healthy control"),
    TeamSpec("tiny", 3, "anthropic_api", "api_key", "three developers"),
)
_TEAM_BY_NAME = {t.name: t for t in TEAMS}
_BASE_DEVS = sum(t.devs for t in TEAMS)   # 61


def team_sizes(devs: int = _BASE_DEVS) -> dict[str, int]:
    """Developers per team: the §18 table, with any developers above 61 dealt round-robin to the
    non-tiny teams (the tiny team stays at 3, below k)."""
    if type(devs) is not int or devs < _BASE_DEVS:
        raise UsageError(f"devs must be an int >= {_BASE_DEVS} (the SPEC §18 team table)")
    sizes = {t.name: t.devs for t in TEAMS}
    growable = [t.name for t in TEAMS if t.name != "tiny"]
    for i in range(devs - _BASE_DEVS):
        sizes[growable[i % len(growable)]] += 1
    return sizes


# ---------------------------------------------------------------------------------------------
# world
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DevInfo:
    """A synthetic developer (or CI bot / service owner). Raw strings live only here (writer
    hints); canonical records carry ``principal`` (``p_``) and ``cwd_key`` (``h_``) only."""

    ref: str                 # opaque device/employee ref, e.g. "payments-03"
    team: str
    principal: str           # pseudonym(FLEET_ORG_KEY, "p", ref)
    cwd: str                 # synthetic working directory (content: carries the canary)
    cwd_key: str             # pseudonym(FLEET_NAME_KEY, "h", cwd)
    email: str               # synthetic analytics actor (example.com)
    iam_arn: str | None      # CUR line_item_iam_principal (ops only)


@dataclass
class FleetHints:
    """Writer hints: native ids and synthetic raw strings the canonical records only carry as
    HMACs. Never rendered (``FleetWorld`` keeps it out of ``repr``)."""

    devs: dict[str, DevInfo] = field(default_factory=dict)
    session_native: dict[str, str] = field(default_factory=dict)   # session_key → native id
    session_dev: dict[str, str] = field(default_factory=dict)      # session_key → dev ref
    session_team: dict[str, str] = field(default_factory=dict)
    session_day: dict[str, int] = field(default_factory=dict)      # session_key → day index
    lane_agent: dict[str, str] = field(default_factory=dict)       # lane_key → agent id | "main"
    ci_runs: dict[str, tuple[str, str]] = field(default_factory=dict)  # session → (repo, workflow)
    transcript_sessions: tuple[str, ...] = ()
    headless_sessions: tuple[str, ...] = ()
    workspaces: dict[str, str] = field(default_factory=dict)       # team → workspace id
    team_map: dict[str, str] = field(default_factory=dict)         # raw actor → team
    migration_day: int = 0
    overage_days: tuple[int, ...] = ()
    runaway_session: str | None = None
    pages: Any = None                                              # ProviderPages


class _Stream:
    """A re-iterable, lazily generated record sequence (scale mode)."""

    def __init__(self, factory: Callable[[], Iterator[Any]], length: int | None = None) -> None:
        self._factory = factory
        self._length = length

    def __iter__(self) -> Iterator[Any]:
        return self._factory()

    def __len__(self) -> int:
        if self._length is None:
            self._length = sum(1 for _ in self._factory())
        return self._length

    def __repr__(self) -> str:
        return f"<synthetic record stream len={self._length}>"


@dataclass(frozen=True)
class FleetWorld:
    """The synthetic world (SPEC §18).

    ``sessions`` are Session/Lane *shells* (lanes without requests/events, as adapters emit them);
    ``requests`` and ``events`` are the flat canonical records (``core.lanes.group_lanes`` or
    :meth:`lanes` assembles them); ``aggregates`` / ``cost_lines`` / ``outcomes`` are the
    provider-side records the written pages carry. In scale mode the three lane-level collections
    are lazy re-iterable streams.
    """

    sessions: Sequence[Session]
    requests: Sequence[Request]
    events: Sequence[LaneEvent]
    aggregates: tuple[UsageAggregate, ...]
    cost_lines: tuple[CostLine, ...]
    outcomes: tuple[OutcomeAggregate, ...]
    source_files: dict[str, Path]
    truth: FleetTruth
    today: str
    seed: int = 7
    days: int = 28
    window_start: str = WINDOW_START
    hints: FleetHints = field(default_factory=FleetHints, repr=False, compare=False)

    @property
    def window_end(self) -> str:
        """The last day of the window (inclusive)."""
        return _date_add(self.window_start, self.days - 1)

    @property
    def since_ms(self) -> int:
        """Window start (UTC midnight, ms)."""
        return _date_ms(self.window_start)

    @property
    def until_ms(self) -> int:
        """Window end (exclusive, ms)."""
        return _date_ms(self.window_start) + self.days * _DAY_MS

    def lanes(self, team: str | None = None) -> list[Lane]:
        """The assembled lanes (all teams, or one), in ``(session_key, lane_key)`` order."""
        lanes = group_lanes(self.requests, self.events, self.sessions)
        if team is None:
            return lanes
        return [lane for lane in lanes if lane.team == team]

    def ingest_result(self) -> IngestResult:
        """Every canonical record as one :class:`~tokenbill.core.types.IngestResult` (principals
        under :data:`FLEET_ORG_KEY`, names under :data:`FLEET_NAME_KEY`): ingest it into a store
        built with ``org_key=FLEET_ORG_KEY`` and ``name_key_id=key_id(FLEET_NAME_KEY)``."""
        digest = hashlib.sha256(f"synthetic-fleet:{self.seed}:{self.days}".encode()).hexdigest()
        source = SourceInfo(
            source_id=stable_id("s", "synthetic-fleet", self.seed, self.days),
            adapter="synthetic-fleet", name_hmac=pseudonym(FLEET_NAME_KEY, "h", "synthetic-fleet"),
            sha256=digest, bytes=0, name_key_id=key_id(FLEET_NAME_KEY),
            principal_key_id=key_id(FLEET_ORG_KEY))
        caps = frozenset({"usage_sequence", "timing", "ttl_split", "iterations", "attempts",
                          "appended", "events", "human_prompts", "lanes_exact", "params", "blocks",
                          "attribution.team", "workload", "aggregates", "cost", "outcomes",
                          "quota_state"})
        return IngestResult(source=source, requests=list(self.requests),
                            sessions=list(self.sessions), events=list(self.events),
                            aggregates=list(self.aggregates), cost_lines=list(self.cost_lines),
                            outcomes=list(self.outcomes), quarantined=[], notes=[],
                            stats={"requests": len(self.requests)}, capabilities=caps)

    def ingest_options(self, **kw: Any) -> IngestOptions:
        """:func:`fleet_ingest_options` with this world's clock (``now_ms`` = UTC midnight of
        :attr:`today`, so provider-side finality matches the canonical records) and team map.
        Keyword arguments override."""
        base: dict[str, Any] = {"now_ms": _date_ms(self.today)}
        if self.truth is not None and self.truth.team_map:
            base["team_map"] = self.truth.team_map
        base.update(kw)
        return fleet_ingest_options(**base)


def fleet_ingest_options(**kw: Any) -> IngestOptions:
    """``IngestOptions`` for reading the written source files consistently with the canonical
    records: name key :data:`FLEET_NAME_KEY`, org key :data:`FLEET_ORG_KEY` (central ingest) and
    the fleet's provider workspace ids allowlisted in clear (canonical records carry them
    verbatim, SPEC §3.2; adapters would otherwise emit ``h_`` pseudonyms of them). ``now_ms`` is
    not set here (the default 0 means "no clock"): pass it, or use :meth:`FleetWorld.ingest_options`
    which sets it to ``today``. Keyword arguments override."""
    base: dict[str, Any] = dict(
        name_key=FLEET_NAME_KEY, name_key_id=key_id(FLEET_NAME_KEY), principal_key=FLEET_ORG_KEY,
        principal_key_id=key_id(FLEET_ORG_KEY), identity_mode="central-ingest",
        name_allowlist=FLEET_WORKSPACES)
    base.update(kw)
    return IngestOptions(**base)


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _date_ms(date: str) -> int:
    return (_dt.date.fromisoformat(date) - _EPOCH).days * _DAY_MS


def _date_add(date: str, days: int) -> str:
    return (_dt.date.fromisoformat(date) + _dt.timedelta(days=days)).isoformat()


def date_of(ts_ms: int) -> str:
    """UTC date of a millisecond timestamp."""
    return (_EPOCH + _dt.timedelta(days=ts_ms // _DAY_MS)).isoformat()


def _b62(*parts: object, n: int = 22) -> str:
    """*n* base-62 characters derived from a SHA-256 of the parts (ids that look like provider
    ids)."""
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).digest()
    return "".join(_B62[byte % 62] for byte in digest[:n])


def _msg_ids(seed: int, lane_key: str, seq: int) -> tuple[str, str]:
    """Provider-looking ``msg_01…`` and ``req_01…`` ids of one request (one SHA-512)."""
    digest = hashlib.sha512(f"{seed}\x1f{lane_key}\x1f{seq}".encode()).digest()
    return ("msg_01" + "".join(_B62[b % 62] for b in digest[:22]),
            "req_01" + "".join(_B62[b % 62] for b in digest[22:44]))


def _uuid(*parts: object) -> str:
    h = hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def _workspace(team: str) -> str:
    return "wrkspc_01" + _b62("workspace", team, n=22)


#: The provider workspace id of every team (synthetic ``wrkspc_01…`` ids; not personal data).
FLEET_WORKSPACES: frozenset[str] = frozenset(_workspace(t.name) for t in TEAMS)


def _ttl_observed(requests: Iterable[Request]) -> str:
    """A lane shell's observed TTL by the ``core.lanes.group_lanes`` rule (billed 5m and/or 1h
    writes); ``group_lanes`` recomputes it when the lanes are assembled."""
    saw_5m = saw_1h = False
    for req in requests:
        for inf in req.billable_inferences:
            saw_5m = saw_5m or inf.usage.cache_write_5m > 0
            saw_1h = saw_1h or inf.usage.cache_write_1h > 0
    return "mixed" if saw_5m and saw_1h else "1h" if saw_1h else "5m" if saw_5m else "unknown"


def _even4(n: int) -> int:
    """The largest multiple of 4 not above ``n`` (reasoning tokens that scale exactly)."""
    return n - n % 4


# ---------------------------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------------------------


@dataclass
class _Row:
    """One request of a lane under construction (serving-inference usage plus request extras)."""

    ts_ms: int
    usage: UsageBuckets
    model: str
    stop_reason: str | None = "tool_use"
    human: bool = False
    model_raw: str | None = None
    usage_source: UsageSource = UsageSource.FINAL
    output_upper: int | None = None
    # (model, usage, billable, billing rule) of a refused attempt before a fallback
    declined: tuple[str, UsageBuckets, bool | None, str] | None = None
    speed: str = "standard"
    service_tier: str = "standard"
    billing_path: str | None = None
    effort: str | None = None
    session_effort: str | None = None
    applied_edits: tuple[tuple[str, int], ...] = ()
    fingerprint: ContentFingerprint | None = None
    breakpoints: tuple[Breakpoint, ...] = ()
    appended: tuple[AppendedItem, ...] = ()
    duration_ms: int = 4_000
    client_version: str | None = None
    kind: InferenceKind = InferenceKind.MESSAGE


@dataclass
class _LaneSpec:
    """Lane-level defaults."""

    team: str
    dev: DevInfo
    kind: LaneKind
    channel: str
    billing_path: str
    scope: str
    agent_product: str
    workload: WorkloadClass
    entrypoint: str | None
    client_version: str | None
    effort: str | None = None
    agent_type: str | None = None
    endpoint_scope: str = "unknown"
    extra: tuple[tuple[str, str], ...] = ()
    repo: str | None = None
    max_tokens: int | None = None
    thinking: str | None = None
    tool_choice: str | None = None
    stream: bool | None = None
    context_management: str | None = None


class _Builder:
    """Collects canonical records; caches shared immutable pieces (contexts, attributions)."""

    def __init__(self, seed: int, days: int) -> None:
        self.seed = seed
        self.days = days
        self.start_ms = _date_ms(WINDOW_START)
        self.requests: list[Request] = []
        self.events: list[LaneEvent] = []
        self.sessions: list[Session] = []
        self.hints = FleetHints()
        self._ctx: dict[tuple, PricingContext] = {}
        self._attr: dict[tuple, Attribution] = {}
        self._params: dict[tuple, RequestParams] = {}
        self._lanes: list[Lane] = []
        self._session: tuple[str, str, DevInfo, str, int, int] | None = None
        self.ref_suffix = ""    # scale mode: one virtual population per epoch

    # ---------- shared pieces ----------

    def rng(self, *scope: object) -> random.Random:
        """The seeded RNG of one scope (team, developer, day, …)."""
        return _rng(self.seed, "fleet", *scope)

    def day_ms(self, day: int) -> int:
        """UTC midnight (ms) of window day *day*."""
        return self.start_ms + day * _DAY_MS

    def dev(self, team: str, index: int) -> DevInfo:
        """Developer *index* of *team* (created on first use, registered in the team map)."""
        ref = f"{team}-{index:02d}{self.ref_suffix}"
        info = self.hints.devs.get(ref)
        if info is None:
            cwd = f"/home/{ref}/src/{team}-service {CANARY}"
            arn = (f"arn:aws:sts::{_AWS_ACCOUNT}:assumed-role/ClaudeCodeBedrock/{ref}"
                   if team == "ops" else None)
            info = DevInfo(ref=ref, team=team, principal=pseudonym(FLEET_ORG_KEY, "p", ref),
                           cwd=cwd, cwd_key=pseudonym(FLEET_NAME_KEY, "h", cwd),
                           email=f"{ref}@fleet.example.com", iam_arn=arn)
            self.hints.devs[ref] = info
            self.hints.team_map[info.email] = team
            self.hints.team_map[ref] = team
            if arn is not None:
                self.hints.team_map[arn] = team
        return info

    def ctx(self, channel: str, model: str, model_raw: str, speed: str, tier: str,
            billing_path: str, endpoint_scope: str) -> PricingContext:
        """A shared PricingContext."""
        key = (channel, model, model_raw, speed, tier, billing_path, endpoint_scope)
        got = self._ctx.get(key)
        if got is None:
            got = self._ctx[key] = PricingContext(
                provider="anthropic", channel=channel, model=model, model_raw=model_raw,
                service_tier=tier, speed=speed, endpoint_scope=endpoint_scope,
                billing_path=billing_path)
        return got

    def attribution(self, spec: _LaneSpec, billing_path: str, client_version: str | None,
                    query_source: str) -> Attribution:
        """A shared Attribution for a lane's requests."""
        key = (spec.team, spec.dev.ref, spec.agent_product, spec.agent_type, query_source,
               spec.workload, spec.entrypoint, client_version, billing_path, spec.extra,
               spec.repo, spec.scope)
        got = self._attr.get(key)
        if got is None:
            ws = spec.scope[3:] if spec.scope.startswith("ws:") else None
            got = self._attr[key] = Attribution(
                principal=spec.dev.principal, team=spec.team, cost_center=f"cc-{spec.team}",
                repo=spec.repo, workspace_id=ws, agent_product=spec.agent_product,
                agent_type=spec.agent_type, query_source=query_source,
                workload_class=spec.workload, entrypoint=spec.entrypoint,
                client_version=client_version, billing_path=billing_path,
                cwd_key=spec.dev.cwd_key, extra=spec.extra)
        return got

    def params(self, spec: _LaneSpec, row: _Row, requested: str) -> RequestParams:
        """Shared RequestParams of a request."""
        effort = row.effort if row.effort is not None else spec.effort
        session_effort = row.session_effort if row.session_effort is not None else spec.effort
        key = (requested, spec.max_tokens, spec.stream, spec.thinking, effort, session_effort,
               spec.tool_choice, row.breakpoints, spec.context_management)
        got = self._params.get(key)
        if got is None:
            got = self._params[key] = RequestParams(
                model_requested=requested, max_tokens=spec.max_tokens, stream=spec.stream,
                thinking=spec.thinking, effort=effort, session_effort=session_effort,
                tool_choice=spec.tool_choice, breakpoints=row.breakpoints,
                automatic_caching=None, context_management=spec.context_management)
        return got

    # ---------- sessions and lanes ----------

    def open_session(self, team: str, dev: DevInfo, native: str, source_kind: str,
                     key_kind: str | None = None) -> str:
        """Start a session. Its key is ``stable_id("ses", key_kind or source_kind, native id)``
        — the adapters' rule, e.g. ``"claude-code"`` for every Claude Code session including
        headless runs (SPEC §5.3 #11)."""
        session_key = stable_id("ses", key_kind or source_kind, native)
        self.hints.session_native[session_key] = native
        self.hints.session_dev[session_key] = dev.ref
        self.hints.session_team[session_key] = team
        self._session = (session_key, source_kind, dev, team, 2**62, 0)
        self._lanes = []
        return session_key

    def lane_key(self, session_key: str, agent: str) -> str:
        """``stable_id("ln", native session id, agent id or "main")``."""
        native = self.hints.session_native[session_key]
        key = stable_id("ln", native, agent)
        self.hints.lane_agent[key] = agent
        return key

    def add_lane(self, spec: _LaneSpec, lane_key: str, rows: Sequence[_Row], *,
                 parent: str | None = None, events: Iterable[LaneEvent] = (),
                 extra_requests: Sequence[Request] = ()) -> None:
        """Materialise *rows* as requests of one lane (plus its shell and events)."""
        assert self._session is not None
        session_key, source_kind, dev, team, lo, hi = self._session
        requests: list[Request] = []
        query_source = "main" if spec.kind is LaneKind.MAIN else (
            "compaction" if spec.kind is LaneKind.COMPACTION else "subagent")
        for k in range(len(rows) - 1):   # a response ends before the next request starts
            room = rows[k + 1].ts_ms - rows[k].ts_ms - 500
            rows[k].duration_ms = max(500, min(rows[k].duration_ms, room))
        for seq, row in enumerate(rows):
            billing_path = row.billing_path or spec.billing_path
            version = row.client_version or spec.client_version
            attr = self.attribution(spec, billing_path, version, query_source)
            msg, req_hint = _msg_ids(self.seed, lane_key, seq)
            rid = request_id_for("anthropic", msg, "", "")
            raw = row.model_raw or row.model
            ctx = self.ctx(spec.channel, row.model, raw, row.speed, row.service_tier,
                           billing_path, spec.endpoint_scope)
            infs: list[Inference] = []
            if row.declined is not None:
                d_model, d_usage, d_billable, d_rule = row.declined
                d_ctx = self.ctx(spec.channel, d_model, d_model, row.speed, row.service_tier,
                                 billing_path, spec.endpoint_scope)
                infs.append(Inference(inference_id=stable_id("inf", rid, 0),
                                      kind=InferenceKind.FALLBACK_DECLINED, usage=d_usage,
                                      pricing=d_ctx, billable=d_billable, billing_rule_id=d_rule))
            kind = InferenceKind.FALLBACK if row.declined is not None else row.kind
            infs.append(Inference(
                inference_id=stable_id("inf", rid, len(infs)), kind=kind, usage=row.usage,
                pricing=ctx, usage_source=row.usage_source, billable=True,
                output_upper=row.output_upper))
            attempt = Attempt(
                attempt_id=stable_id("at", rid, 0), attempt_no=0, ts_start_ms=row.ts_ms,
                ttft_ms=None, duration_ms=row.duration_ms, outcome=Outcome.OK, http_status=None,
                error_type=None, retry_layer=None, retry_after_ms=None, should_retry=None,
                provider_request_id=req_hint,
                provider_message_id=msg, model_served=raw, stop_reason=row.stop_reason,
                inferences=tuple(infs), applied_edits=row.applied_edits,
                convention_id="anthropic.messages")
            requested = row.declined[0] if row.declined is not None else raw
            requests.append(Request(
                request_id=rid, session_key=session_key, lane_key=lane_key, seq=seq,
                attribution=attr, params=self.params(spec, row, requested), attempts=(attempt,),
                fingerprint=row.fingerprint, appended=row.appended))
            lo = min(lo, row.ts_ms)
            hi = max(hi, row.ts_ms + row.duration_ms)
        requests.extend(extra_requests)
        evs = list(events)
        self.requests.extend(requests)
        self.events.extend(evs)
        shell = Lane(lane_key=lane_key, session_key=session_key, kind=spec.kind,
                     parent_lane_key=parent, cache_scope_key=spec.scope, requests=(),
                     events=(), ttl_observed=_ttl_observed(requests), lane_exact=True)
        self._lanes.append(shell)
        self._session = (session_key, source_kind, dev, team, lo, hi)

    def close_session(self) -> None:
        """Finish the open session: its shell (lanes without requests) is recorded."""
        assert self._session is not None
        session_key, source_kind, dev, team, lo, hi = self._session
        if lo < 2**62:
            self.hints.session_day[session_key] = (lo - self.start_ms) // _DAY_MS
        if self._lanes:
            main_spec_attr = Attribution(principal=dev.principal, team=team,
                                         cost_center=f"cc-{team}")
            self.sessions.append(Session(
                session_key=session_key, source_kind=source_kind, attribution=main_spec_attr,
                lanes=tuple(sorted(self._lanes, key=lambda ln: ln.lane_key)),
                started_ms=lo if lo < 2**62 else 0, ended_ms=max(hi, lo if lo < 2**62 else 0)))
        self._session = None
        self._lanes = []


def _event(lane_key: str, ts_ms: int, kind: LaneEventKind, **attrs: Any) -> LaneEvent:
    return LaneEvent(lane_key=lane_key, ts_ms=ts_ms, kind=kind, attrs=tuple(attrs.items()))


# ---------------------------------------------------------------------------------------------
# lane shapes
# ---------------------------------------------------------------------------------------------


def _gaps(r: random.Random, n: int, p_long: float, long_s: tuple[int, int],
          short_s: tuple[int, int] = (15, 280)) -> list[int]:
    """``n − 1`` gaps in seconds: bursty (warm under 5m) or long (5–60 min, never within 10 s of
    300 s or 3,600 s, so no transition is ambiguous)."""
    return [r.randint(*long_s) if r.random() < p_long else r.randint(*short_s)
            for _ in range(n - 1)]


def _grow(r: random.Random, gaps: Sequence[int], t0: int, app: tuple[int, int],
          ttl_s: int, u: tuple[int, int] = (1, 9)) -> list[tuple[int, int, int, int, bool]]:
    """Rows ``(offset_s, R, W, U, cold)`` of a Claude Code–shaped lane billed by the documented
    rules: request 0 writes its prefix; a request within the TTL reads the whole previous prefix
    and writes the appended tokens; after a longer gap it rewrites everything."""
    rows = []
    prefix = 0
    offset = 0
    for i in range(len(gaps) + 1):
        if i:
            offset += gaps[i - 1]
        uncached = r.randint(*u)
        if i == 0:
            reads, writes, cold = 0, t0 - uncached, True
        elif gaps[i - 1] <= ttl_s:
            reads, writes, cold = prefix, r.randint(*app), False
        else:
            reads, writes, cold = 0, prefix + r.randint(*app), True
        prefix = reads + writes
        rows.append((offset, reads, writes, uncached, cold))
    return rows


def _usage(reads: int, writes: int, uncached: int, out: int, bucket: str,
           reasoning: int | None = None) -> UsageBuckets:
    if bucket == "1h":
        return UsageBuckets(uncached_input=uncached, cache_read=reads, cache_write_1h=writes,
                            output=out, output_reasoning=reasoning)
    return UsageBuckets(uncached_input=uncached, cache_read=reads, cache_write_5m=writes,
                        output=out, output_reasoning=reasoning)


def _human_flags(r: random.Random, gaps: Sequence[int], p: float = 0.2) -> list[bool]:
    """Request 0 and every request after a long pause start a human turn; others with *p*."""
    return [True] + [g > 300 or r.random() < p for g in gaps]


def _stop_reasons(humans: Sequence[bool]) -> list[str]:
    """``tool_use`` when the next request answers a tool call, ``end_turn`` before a human turn
    and at the end of the lane."""
    n = len(humans)
    return ["end_turn" if i == n - 1 or humans[i + 1] else "tool_use" for i in range(n)]


def _cc_spec(b: _Builder, team: str, dev: DevInfo, kind: LaneKind, *, effort: str | None,
             billing_path: str | None = None, agent_type: str | None = None,
             extra: tuple[tuple[str, str], ...] = ()) -> _LaneSpec:
    spec = _TEAM_BY_NAME[team]
    return _LaneSpec(team=team, dev=dev, kind=kind, channel=spec.channel,
                     billing_path=billing_path or spec.billing_path,
                     scope="ws:" + b.hints.workspaces[team], agent_product="claude_code",
                     workload=WorkloadClass.INTERACTIVE, entrypoint="cli",
                     client_version=_CC_VERSION, effort=effort, agent_type=agent_type, extra=extra)


def _cc_main_rows(b: _Builder, r: random.Random, start_ms: int, *, n: int, model: str,
                  ttl: str, p_long: float, long_s: tuple[int, int], t0: tuple[int, int],
                  app: tuple[int, int], out: tuple[int, int], p_human: float = 0.2,
                  speed: str = "standard", tier: str = "standard",
                  reasoning_share: bool = False, out_scale: tuple[int, int] = (1, 1)
                  ) -> list[_Row]:
    """A standard Claude Code main lane."""
    gaps = _gaps(r, n, p_long, long_s)
    humans = _human_flags(r, gaps, p_human)
    stops = _stop_reasons(humans)
    ttl_s = 3600 if ttl == "1h" else 300
    rows = []
    for i, (off, reads, writes, uncached, _cold) in enumerate(
            _grow(r, gaps, r.randint(*t0), app, ttl_s)):
        o = r.randint(*out) * out_scale[0] // out_scale[1]
        reasoning = _even4(o // 2) if reasoning_share else None
        rows.append(_Row(ts_ms=start_ms + off * 1000 + r.randint(0, 999),
                         usage=_usage(reads, writes, uncached, o, ttl, reasoning), model=model,
                         stop_reason=stops[i], human=humans[i], speed=speed, service_tier=tier,
                         duration_ms=r.randint(2_000, 40_000)))
    return rows


def _human_events(lane_key: str, rows: Sequence[_Row]) -> list[LaneEvent]:
    return [_event(lane_key, row.ts_ms, LaneEventKind.HUMAN_PROMPT) for row in rows if row.human]


def _subagent(b: _Builder, r: random.Random, session_key: str, parent_key: str, team: str,
              dev: DevInfo, start_ms: int, *, model: str, n: tuple[int, int],
              effort: str | None, speed: str = "standard", ttl: str = "5m",
              billing_path: str | None = None, kind: LaneKind = LaneKind.SUBAGENT,
              agent_type: str = "general-purpose", index: int = 0) -> None:
    """A subagent (or workflow agent) lane: bursty, warm, starting at *start_ms*."""
    agent = _b62("agent", b.seed, session_key, index, n=8).lower()
    lane_key = b.lane_key(session_key, agent)
    spec = _cc_spec(b, team, dev, kind, effort=effort, billing_path=billing_path,
                    agent_type=agent_type)
    count = r.randint(*n)
    gaps = [r.randint(5, 90) for _ in range(count - 1)]
    rows = []
    for i, (off, reads, writes, uncached, _c) in enumerate(
            _grow(r, gaps, r.randint(12_000, 20_000), (1_000, 4_000),
                  3600 if ttl == "1h" else 300)):
        rows.append(_Row(ts_ms=start_ms + off * 1000 + r.randint(0, 999),
                         usage=_usage(reads, writes, uncached, r.randint(150, 900), ttl),
                         model=model, speed=speed,
                         stop_reason="end_turn" if i == count - 1 else "tool_use",
                         duration_ms=r.randint(2_000, 20_000)))
    _attach_subagent_appended(b.rng("appended", lane_key), rows)
    meta = _event(lane_key, rows[0].ts_ms, LaneEventKind.SESSION_META, agent_type=agent_type,
                  spawn_depth=1, model_alias=model.split("-")[1])
    b.add_lane(spec, lane_key, rows, parent=parent_key, events=[meta])


# ---------------------------------------------------------------------------------------------
# teams
# ---------------------------------------------------------------------------------------------


def _session_start(r: random.Random, b: _Builder, day: int, hours: tuple[int, int]) -> int:
    """A session start (ms) between the given UTC hours of *day*."""
    return b.day_ms(day) + r.randint(hours[0] * 3600, hours[1] * 3600) * 1000


def _gen_platform(b: _Builder, devs: Sequence[int]) -> None:
    """Behind a LiteLLM gateway that strips ``cache_control``: every input token uncached."""
    for d in devs:
        dev = b.dev("platform", d)
        for day in range(b.days):
            r = b.rng("platform", d, day)
            if r.random() >= 0.6:
                continue
            start = _session_start(r, b, day, (8, 16))
            sk = b.open_session("platform", dev, _uuid("platform", b.seed, d, day), "claude-code")
            lane_key = b.lane_key(sk, "main")
            spec = _cc_spec(b, "platform", dev, LaneKind.MAIN, effort="medium",
                            extra=(("gateway", "litellm-proxy"),))
            n = r.randint(8, 24)
            gaps = _gaps(r, n, 0.15, (320, 1500))
            humans = _human_flags(r, gaps)
            stops = _stop_reasons(humans)
            total = r.randint(18_000, 30_000)
            rows = []
            offset = 0
            for i in range(n):
                if i:
                    offset += gaps[i - 1]
                    total += r.randint(1_500, 6_000)
                rows.append(_Row(ts_ms=start + offset * 1000 + r.randint(0, 999),
                                 usage=UsageBuckets(uncached_input=total,
                                                    output=r.randint(150, 1_200)),
                                 model=_OPUS55, stop_reason=stops[i], human=humans[i],
                                 duration_ms=r.randint(2_000, 40_000)))
            b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
            b.close_session()


def _gen_payments(b: _Builder, devs: Sequence[int]) -> None:
    """Claude Code main lanes on Opus 5.5 billed 5m; 12% of gaps fall in 5–60 minutes."""
    for d in devs:
        dev = b.dev("payments", d)
        for day in range(b.days):
            r = b.rng("payments", d, day)
            if r.random() >= 0.6:
                continue
            start = _session_start(r, b, day, (8, 16))
            sk = b.open_session("payments", dev, _uuid("payments", b.seed, d, day),
                                "claude-code")
            lane_key = b.lane_key(sk, "main")
            spec = _cc_spec(b, "payments", dev, LaneKind.MAIN, effort="medium")
            rows = _cc_main_rows(b, r, start, n=r.randint(15, 35), model=_OPUS55, ttl="5m",
                                 p_long=0.12, long_s=(320, 3500), t0=(18_000, 28_000),
                                 app=(1_500, 5_000), out=(200, 1_500))
            b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
            if r.random() < 0.3:
                k = r.randint(1, len(rows) - 1)
                _subagent(b, r, sk, lane_key, "payments", dev, rows[k].ts_ms + r.randint(15, 40)
                          * 1000, model=_SONNET5, n=(3, 8), effort="medium")
            b.close_session()


def _gen_search(b: _Builder, devs: Sequence[int]) -> None:
    """Subscription main lanes (1h) on Sonnet 5 growing to ~900k; no compaction below 967k."""
    for d in devs:
        dev = b.dev("search", d)
        for day in range(b.days):
            r = b.rng("search", d, day)
            if r.random() >= 0.35:
                continue
            start = _session_start(r, b, day, (7, 13))
            sk = b.open_session("search", dev, _uuid("search", b.seed, d, day), "claude-code")
            lane_key = b.lane_key(sk, "main")
            spec = _cc_spec(b, "search", dev, LaneKind.MAIN, effort="medium")
            target = r.randint(820_000, 900_000)
            t0 = r.randint(22_000, 30_000)
            appends = []
            total = t0
            while True:
                a = r.randint(18_000, 30_000)
                if total + a + 9 > target:
                    break
                appends.append(a)
                total += a
            n = len(appends) + 1
            gaps = _gaps(r, n, 0.1, (320, 3000), (20, 280))
            humans = _human_flags(r, gaps)
            stops = _stop_reasons(humans)
            rows = []
            prefix = 0
            offset = 0
            for i in range(n):
                uncached = r.randint(1, 9)
                if i == 0:
                    reads, writes = 0, t0 - uncached
                else:
                    offset += gaps[i - 1]
                    reads, writes = prefix, appends[i - 1]
                prefix = reads + writes
                rows.append(_Row(ts_ms=start + offset * 1000 + r.randint(0, 999),
                                 usage=_usage(reads, writes, uncached, r.randint(300, 2_000),
                                              "1h"),
                                 model=_SONNET5, stop_reason=stops[i], human=humans[i],
                                 duration_ms=r.randint(3_000, 50_000)))
            b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
            b.close_session()


def _gen_mobile(b: _Builder, devs: Sequence[int]) -> None:
    """Subscription main lanes (1h) on Opus 5.5: build to 400–600k, idle 1–8 h, resume cold.
    On the two overage days every session runs on usage credits (QUOTA_STATE using_overage)."""
    overage = b.hints.overage_days
    for d in devs:
        dev = b.dev("mobile", d)
        for day in range(b.days):
            r = b.rng("mobile", d, day)
            active = r.random() < 0.45
            if not (active or day in overage):
                continue
            start = _session_start(r, b, day, (6, 8))
            sk = b.open_session("mobile", dev, _uuid("mobile", b.seed, d, day), "claude-code")
            lane_key = b.lane_key(sk, "main")
            billing_path = "usage_credits" if day in overage else "subscription"
            spec = _cc_spec(b, "mobile", dev, LaneKind.MAIN, effort="medium",
                            billing_path=billing_path)
            target = r.randint(400_000, 600_000)
            gaps: list[int] = []
            t0 = r.randint(20_000, 30_000)
            appends: list[int] = []
            total = t0
            while total < target:
                a = r.randint(12_000, 24_000)
                appends.append(a)
                total += a
                gaps.append(r.randint(320, 2_400) if r.random() < 0.08 else r.randint(20, 280))
            idle = r.randint(3_700, 28_000)
            resume = r.randint(4, 8)
            for j in range(resume):
                appends.append(r.randint(2_000, 8_000))
                gaps.append(idle if j == 0 else r.randint(20, 280))
            n = len(appends) + 1
            humans = [True] + [g > 300 or r.random() < 0.2 for g in gaps]
            stops = _stop_reasons(humans)
            rows = []
            prefix = 0
            offset = 0
            for i in range(n):
                uncached = r.randint(1, 9)
                if i == 0:
                    reads, writes = 0, t0 - uncached
                else:
                    offset += gaps[i - 1]
                    if gaps[i - 1] <= 3600:
                        reads, writes = prefix, appends[i - 1]
                    else:
                        reads, writes = 0, prefix + appends[i - 1]
                prefix = reads + writes
                rows.append(_Row(ts_ms=start + offset * 1000 + r.randint(0, 999),
                                 usage=_usage(reads, writes, uncached, r.randint(300, 1_800),
                                              "1h"),
                                 model=_OPUS55, stop_reason=stops[i], human=humans[i],
                                 duration_ms=r.randint(3_000, 40_000)))
            events = _human_events(lane_key, rows)
            if day in overage:
                events.append(_event(
                    lane_key, rows[0].ts_ms, LaneEventKind.QUOTA_STATE, status="allowed",
                    rate_limit_type="seven_day", using_overage=True, overage_status="allowed",
                    resets_at_ms=b.day_ms(day + 1)))
            b.add_lane(spec, lane_key, rows, events=events)
            b.close_session()


def _gen_infra(b: _Builder, devs: Sequence[int]) -> None:
    """Opus 5.5 main lanes; devs 0–1 sticky fast mode, dev 2 sticky xhigh; the cross-team extras
    on devs 3–5 (transcript days)."""
    days = b.days
    placeholder_day, compaction_day = min(1, days - 1), min(2, days - 1)
    for d in devs:
        dev = b.dev("infra", d)
        fast = d in (0, 1)
        effort = "xhigh" if d == 2 else "medium"
        for day in range(days):
            r = b.rng("infra", d, day)
            planted = (d, day) in ((3, placeholder_day), (5, compaction_day))
            if r.random() >= 0.7 and not planted:
                continue
            start = _session_start(r, b, day, (8, 15))
            sk = b.open_session("infra", dev, _uuid("infra", b.seed, d, day), "claude-code")
            lane_key = b.lane_key(sk, "main")
            spec = _cc_spec(b, "infra", dev, LaneKind.MAIN, effort=effort)
            rows = _cc_main_rows(b, r, start, n=r.randint(12, 30), model=_OPUS55, ttl="5m",
                                 p_long=0.08, long_s=(320, 2_400), t0=(18_000, 28_000),
                                 app=(1_500, 5_000), out=(200, 1_500),
                                 speed="fast" if fast else "standard")
            extra_requests: list[Request] = []
            extra_events: list[LaneEvent] = []
            if (d, day) == (3, placeholder_day):
                _plant_placeholder(r, rows)
            if (d, day) == (5, compaction_day):
                extra_requests, extra_events = _plant_hidden_compaction(b, r, sk, lane_key, rows)
            _attach_appended(r, rows)
            events = [*_human_events(lane_key, rows), *extra_events]   # after any time shift
            b.add_lane(spec, lane_key, rows, events=events, extra_requests=extra_requests)
            if extra_requests:
                comp_key = lane_key + "#compaction"
                comp_spec = dataclasses.replace(spec, kind=LaneKind.COMPACTION)
                b.add_lane(comp_spec, comp_key, [], parent=lane_key)
            if r.random() < 0.25:
                k = r.randint(1, len(rows) - 1)
                _subagent(b, r, sk, lane_key, "infra", dev,
                          rows[k].ts_ms + r.randint(15, 40) * 1000, model=_SONNET5, n=(3, 7),
                          effort=effort)
            b.close_session()
        if d == 3:
            _plant_refusal(b, dev, day=0, declined_output=0)
        if d == 4:
            _plant_refusal(b, dev, day=min(1, days - 1), declined_output=6)
            _plant_unpriced_model(b, dev, day=days - 2)


def _attach_appended(r: random.Random, rows: Sequence[_Row]) -> None:
    """Appended items (sizes only): a human prompt or the tool result answering the previous
    request's tool call."""
    tools = ("Read", "Bash", "Grep", "Edit", "Glob")
    for i, row in enumerate(rows):
        if row.human:
            row.appended = (AppendedItem(kind="user_text", name=None,
                                         n_bytes=r.randint(40, 400)),)
        elif i > 0:
            row.appended = (AppendedItem(kind="tool_result", name=tools[r.randrange(len(tools))],
                                         n_bytes=r.randint(200, 6_000)),)


def _attach_subagent_appended(r: random.Random, rows: Sequence[_Row]) -> None:
    """Appended items of a subagent lane (sizes only): the task prompt handed over by the parent,
    then the tool result answering the previous request's tool call (the transcript writer
    serialises exactly these sizes)."""
    tools = ("Read", "Bash", "Grep", "Edit", "Glob")
    for i, row in enumerate(rows):
        if i == 0:
            row.appended = (AppendedItem(kind="user_text", name=None,
                                         n_bytes=r.randint(200, 2_000)),)
        else:
            row.appended = (AppendedItem(kind="tool_result", name=tools[r.randrange(len(tools))],
                                         n_bytes=r.randint(200, 6_000)),)


def _plant_placeholder(r: random.Random, rows: list[_Row]) -> None:
    """One MESSAGE_START_ONLY call: logged output 3 (a lower bound), true output in
    ``output_upper``; the next request answers its tool call."""
    for i in range(1, len(rows) - 1):
        if not rows[i + 1].human:
            u = rows[i].usage
            rows[i].usage = dataclasses.replace(u, output=3)
            rows[i].output_upper = max(u.output, 420)
            rows[i].usage_source = UsageSource.MESSAGE_START_ONLY
            rows[i].stop_reason = None
            return


def _plant_hidden_compaction(b: _Builder, r: random.Random, session_key: str, lane_key: str,
                             rows: list[_Row]) -> tuple[list[Request], list[LaneEvent]]:
    """A ``compact_boundary`` between two requests: a COMPACTION event, the Claude Code importer's
    ESTIMATED compaction call on ``<lane>#compaction`` (input read warm, output = summary), and the
    next request rewrites the compacted context."""
    j = len(rows) // 2
    prev = rows[j - 1]
    pre = prev.usage.total_input
    ts_c = prev.ts_ms + 30_000
    if ts_c + 20_000 >= rows[j].ts_ms:
        shift = ts_c + 20_000 - rows[j].ts_ms + 1_000
        for row in rows[j:]:
            row.ts_ms += shift
    events = [_event(lane_key, ts_c, LaneEventKind.COMPACTION, trigger="auto", pre_tokens=pre,
                     post_tokens=COMPACTION_POST_TOKENS, duration_ms=28_000,
                     dropped_tokens=None)]
    # the compacted context: summary + what follows, rewritten; later requests read it
    prefix = 0
    for k in range(j, len(rows)):
        u = rows[k].usage
        appended = r.randint(1_500, 5_000)
        if k == j:
            reads, writes = 0, COMPACTION_POST_TOKENS + appended - u.uncached_input
        elif rows[k].ts_ms - rows[k - 1].ts_ms <= 300_000:
            reads, writes = prefix, appended
        else:
            reads, writes = 0, prefix + appended
        prefix = reads + writes
        rows[k].usage = dataclasses.replace(u, cache_read=reads, cache_write_5m=writes)
    comp_key = lane_key + "#compaction"
    rid = stable_id("rq", "claude-code-compaction", lane_key, ts_c)
    ctx = b.ctx("anthropic_api", _OPUS55, _OPUS55, "standard", "standard", "api_key", "unknown")
    inf = Inference(inference_id=stable_id("inf", rid, 0), kind=InferenceKind.COMPACTION,
                    usage=UsageBuckets(cache_read=pre, output=COMPACTION_POST_TOKENS),
                    pricing=ctx, usage_source=UsageSource.ESTIMATED, billable=True)
    attempt = Attempt(attempt_id=stable_id("at", rid, 0), attempt_no=0, ts_start_ms=ts_c,
                      ttft_ms=None, duration_ms=28_000, outcome=Outcome.OK, http_status=None,
                      error_type=None, retry_layer=None, retry_after_ms=None, should_retry=None,
                      provider_request_id=None, provider_message_id=None, model_served=_OPUS55,
                      stop_reason=None, inferences=(inf,))
    dev = b.hints.devs[b.hints.session_dev[session_key]]
    attr = Attribution(principal=dev.principal, team="infra", cost_center="cc-infra",
                       workspace_id=b.hints.workspaces["infra"], agent_product="claude_code",
                       query_source="compaction", workload_class=WorkloadClass.INTERACTIVE,
                       entrypoint="cli", client_version=_CC_VERSION, billing_path="api_key",
                       cwd_key=dev.cwd_key)
    request = Request(request_id=rid, session_key=session_key, lane_key=comp_key, seq=0,
                      attribution=attr, params=RequestParams(model_requested=_OPUS55),
                      attempts=(attempt,))
    b.hints.lane_agent[comp_key] = "compaction"
    return [request], events


def _plant_refusal(b: _Builder, dev: DevInfo, *, day: int, declined_output: int) -> None:
    """A short lane whose first call is refused on Fable 5 and served by the Opus 4.8 fallback
    (``usage.iterations``); declined output 0 → not billable, 1–16 → billing uncertain."""
    r = b.rng("infra-refusal", dev.ref, day)
    start = _session_start(r, b, day, (16, 18))
    sk = b.open_session("infra", dev, _uuid("infra-refusal", b.seed, dev.ref, day),
                        "claude-code")
    lane_key = b.lane_key(sk, "main")
    spec = _cc_spec(b, "infra", dev, LaneKind.MAIN, effort="medium")
    t0 = r.randint(16_000, 22_000)
    uncached = 4
    declined_usage = UsageBuckets(uncached_input=uncached, cache_write_5m=t0 - uncached,
                                  output=declined_output)
    billable: bool | None = False if declined_output == 0 else (
        None if declined_output <= 16 else True)
    rule = ("anthropic.refusal.pre_output" if declined_output == 0 else
            "anthropic.refusal.ambiguous" if declined_output <= 16 else
            "anthropic.refusal.mid_stream")
    rows = [_Row(ts_ms=start, usage=UsageBuckets(uncached_input=uncached,
                                                 cache_write_5m=t0 - uncached,
                                                 output=r.randint(300, 900)),
                 model=_OPUS48, human=True, declined=(_FABLE5, declined_usage, billable, rule),
                 stop_reason="tool_use")]
    prefix = t0 - uncached
    t = start
    for k in range(2):
        a = r.randint(1_500, 4_000)
        t += r.randint(30, 120) * 1000
        rows.append(_Row(ts_ms=t,
                         usage=UsageBuckets(uncached_input=3, cache_read=prefix,
                                            cache_write_5m=a, output=r.randint(200, 700)),
                         model=_OPUS48, stop_reason="tool_use" if k == 0 else "end_turn"))
        prefix += a
    _attach_appended(r, rows)
    events = _human_events(lane_key, rows)
    events.append(_event(lane_key, start, LaneEventKind.MODEL_FALLBACK, from_model=_FABLE5,
                         to_model=_OPUS48, trigger="refusal", credited=None))
    b.add_lane(spec, lane_key, rows, events=events)
    b.close_session()


def _plant_unpriced_model(b: _Builder, dev: DevInfo, *, day: int) -> None:
    """One call on an announced, unpriced model (coverage < 1, SPEC §18), on a provisional
    day."""
    r = b.rng("infra-unpriced", dev.ref, day)
    start = _session_start(r, b, day, (17, 19))
    sk = b.open_session("infra", dev, _uuid("infra-unpriced", b.seed, dev.ref, day),
                        "claude-code")
    lane_key = b.lane_key(sk, "main")
    spec = _cc_spec(b, "infra", dev, LaneKind.MAIN, effort="medium")
    rows = [
        _Row(ts_ms=start, usage=UsageBuckets(uncached_input=5, cache_write_5m=4_200, output=180),
             model=_UNPRICED_MODEL, human=True, stop_reason="end_turn"),
    ]
    _attach_appended(r, rows)
    b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
    b.close_session()


def _gen_data(b: _Builder, devs: Sequence[int]) -> None:
    """Devs 0–3 on the default main model (Opus 4.8 → Opus 5 on the migration day, +35% output
    per call), devs 4–5 on Fable 5; effort high; workflow agents on Opus 5; the legacy Priority
    tier on the first two days of Opus 4.8 traffic."""
    mig = b.hints.migration_day
    for d in devs:
        dev = b.dev("data", d)
        default_dev = d % 6 < 4
        for day in range(b.days):
            r = b.rng("data", d, day)
            if r.random() >= 0.65:
                continue
            start = _session_start(r, b, day, (8, 15))
            sk = b.open_session("data", dev, _uuid("data", b.seed, d, day), "claude-code")
            lane_key = b.lane_key(sk, "main")
            spec = _cc_spec(b, "data", dev, LaneKind.MAIN, effort="high")
            if default_dev:
                model = _OPUS48 if day < mig else _OPUS5
                scale = (1, 1) if day < mig else (135, 100)
            else:
                model, scale = _FABLE5, (1, 1)
            tier = "priority" if default_dev and day < 2 and model == _OPUS48 else "standard"
            rows = _cc_main_rows(b, r, start, n=r.randint(12, 28), model=model, ttl="5m",
                                 p_long=0.08, long_s=(320, 2_400), t0=(18_000, 28_000),
                                 app=(1_500, 5_000), out=(400, 1_600), tier=tier,
                                 reasoning_share=True, out_scale=scale)
            b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
            if r.random() < 0.5:
                k = r.randint(1, len(rows) - 1)
                t = rows[k].ts_ms + r.randint(15, 40) * 1000
                for w in range(2):
                    _subagent(b, r, sk, lane_key, "data", dev, t + w * r.randint(15, 45) * 1000,
                              model=_OPUS5, n=(5, 12), effort=None,
                              kind=LaneKind.WORKFLOW_AGENT, agent_type="workflow-subagent",
                              index=w)
            b.close_session()


def _gen_ops(b: _Builder, devs: Sequence[int]) -> None:
    """Claude Code on Bedrock (Opus 5, regional endpoint); one runaway loop session."""
    runaway_day = b.days - 2 if b.days < 18 else 16
    for d in devs:
        dev = b.dev("ops", d)
        for day in range(b.days):
            r = b.rng("ops", d, day)
            if r.random() >= 0.8:
                continue
            for s in range(r.randint(1, 2)):
                start = _session_start(r, b, day, (8 + 5 * s, 12 + 5 * s))
                sk = b.open_session("ops", dev, _uuid("ops", b.seed, d, day, s), "claude-code")
                lane_key = b.lane_key(sk, "main")
                spec = _ops_spec(b, dev)
                rows = _cc_main_rows(b, r, start, n=r.randint(6, 16), model=_OPUS5, ttl="5m",
                                     p_long=0.1, long_s=(320, 2_000), t0=(20_000, 40_000),
                                     app=(2_000, 8_000), out=(300, 1_500), p_human=0.3)
                for row in rows:
                    row.model_raw = "us.anthropic." + _OPUS5
                b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
                b.close_session()
        if d == 2:
            _plant_runaway(b, dev, runaway_day)


def _ops_spec(b: _Builder, dev: DevInfo) -> _LaneSpec:
    return _LaneSpec(team="ops", dev=dev, kind=LaneKind.MAIN, channel="bedrock",
                     billing_path="bedrock", scope=f"org:bedrock:{_AWS_ACCOUNT}",
                     agent_product="claude_code", workload=WorkloadClass.INTERACTIVE,
                     entrypoint="cli", client_version=_CC_VERSION, effort="medium",
                     endpoint_scope="regional", extra=(("endpoint_scope", "regional"),))


def _plant_runaway(b: _Builder, dev: DevInfo, day: int) -> None:
    """A 3-hour loop: 400 requests 27 s apart, context growing from 150k, no human prompt after
    the first."""
    r = b.rng("ops-runaway", dev.ref, day)
    start = b.day_ms(day) + 13 * 3600_000
    sk = b.open_session("ops", dev, _uuid("ops-runaway", b.seed, dev.ref, day), "claude-code")
    b.hints.runaway_session = sk
    lane_key = b.lane_key(sk, "main")
    spec = _ops_spec(b, dev)
    rows = []
    prefix = 0
    for i in range(400):
        if i == 0:
            reads, writes = 0, 150_000
        else:
            reads, writes = prefix, r.randint(1_500, 2_500)
        prefix = reads + writes
        rows.append(_Row(ts_ms=start + i * 27_000 + r.randint(0, 999),
                         usage=UsageBuckets(uncached_input=r.randint(1, 9), cache_read=reads,
                                            cache_write_5m=writes, output=r.randint(4_000, 7_000)),
                         model=_OPUS5, model_raw="us.anthropic." + _OPUS5, human=i == 0,
                         stop_reason="tool_use", duration_ms=r.randint(8_000, 20_000)))
    b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
    b.close_session()


def _gen_ci(b: _Builder, devs: Sequence[int]) -> None:
    """claude-code-action runs (Sonnet 5, max_tokens 16,384): full first-call writes (dynamic
    system sections), CLI version drift, 15% truncated steps mostly retried within 120 s; plus
    single-shot service calls (batch-eligible)."""
    workflows = ("claude-review.yml", "claude-fix.yml")
    for d in devs:
        bot = b.dev("ci-bots", d)
        repo_raw = f"fleet-example/{['api', 'web', 'infra-tools', 'sdk', 'docs'][d % 5]}-{d}"
        repo = pseudonym(FLEET_NAME_KEY, "h", repo_raw)
        for day in range(b.days):
            r = b.rng("ci-bots", d, day)
            for push in range(r.randint(1, 3)):
                t = _session_start(r, b, day, (9, 18))
                for run in range(r.randint(1, 2)):
                    t += r.randint(20, 200) * 1000 if run else 0
                    wf = workflows[run % 2]
                    _ci_run(b, r, bot, repo, repo_raw, wf, t, (d, day, push, run))
            for k in range(r.randint(1, 3)):
                _single_shot(b, r, bot, _session_start(r, b, day, (0, 23)), (d, day, k))


def _ci_run(b: _Builder, r: random.Random, bot: DevInfo, repo: str, repo_raw: str,
            workflow: str, start: int, key: tuple) -> None:
    sk = b.open_session("ci-bots", bot, _uuid("ci-bots", b.seed, *key), "claude-code-headless",
                        key_kind="claude-code")
    b.hints.ci_runs[sk] = (repo_raw, workflow)
    lane_key = b.lane_key(sk, "main")
    spec = _LaneSpec(team="ci-bots", dev=bot, kind=LaneKind.MAIN, channel="anthropic_api",
                     billing_path="api_key", scope="ws:" + b.hints.workspaces["ci-bots"],
                     agent_product="claude_code", workload=WorkloadClass.CI,
                     entrypoint="claude-code-github-action",
                     client_version=r.choice(_CI_VERSIONS), repo=repo,
                     extra=(("run_attempt", "1"),
                            ("workflow", pseudonym(FLEET_NAME_KEY, "h", workflow))),
                     max_tokens=16_384)
    steps = r.randint(4, 12)
    rows: list[_Row] = []
    prefix = 0
    t = start
    for i in range(steps):
        uncached = r.randint(1, 9)
        if i == 0:
            reads, writes = 0, r.randint(30_000, 45_000)
        else:
            t += r.randint(5, 90) * 1000
            reads, writes = prefix, r.randint(2_000, 8_000)
        prefix = reads + writes
        truncated = i > 0 and r.random() < 0.15
        rows.append(_Row(ts_ms=t + r.randint(0, 999),
                         usage=UsageBuckets(uncached_input=uncached, cache_read=reads,
                                            cache_write_5m=writes,
                                            output=16_384 if truncated else r.randint(300, 3_000)),
                         model=_SONNET5, human=False,
                         stop_reason="max_tokens" if truncated else "tool_use",
                         duration_ms=r.randint(5_000, 60_000)))
        if truncated and i < steps - 1 and r.random() < 0.8:
            t += r.randint(30, 100) * 1000   # the retry: same prompt, warm
            rows.append(_Row(ts_ms=t + r.randint(0, 999),
                             usage=UsageBuckets(uncached_input=uncached, cache_read=prefix,
                                                output=r.randint(300, 3_000)),
                             model=_SONNET5, stop_reason="tool_use",
                             duration_ms=r.randint(5_000, 60_000)))
    if rows[-1].stop_reason != "max_tokens":
        rows[-1].stop_reason = "end_turn"
    b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
    b.close_session()


def _single_shot(b: _Builder, r: random.Random, bot: DevInfo, start: int, key: tuple) -> None:
    sk = b.open_session("ci-bots", bot, _uuid("ci-single", b.seed, *key), "anthropic-responses")
    lane_key = b.lane_key(sk, "main")
    spec = _LaneSpec(team="ci-bots", dev=bot, kind=LaneKind.API_RUN, channel="anthropic_api",
                     billing_path="api_key", scope="ws:" + b.hints.workspaces["ci-bots"],
                     agent_product="api", workload=WorkloadClass.SERVICE, entrypoint="sdk-py",
                     client_version=None)
    rows = [_Row(ts_ms=start, usage=UsageBuckets(uncached_input=r.randint(3_000, 12_000),
                                                 output=r.randint(200, 1_500)),
                 model=_SONNET5, stop_reason="end_turn", duration_ms=r.randint(2_000, 15_000))]
    b.add_lane(spec, lane_key, rows)
    b.close_session()


def _gen_agents(b: _Builder, devs: Sequence[int]) -> None:
    """Agent SDK runs (Opus 5.5, 5m, recorder fingerprints): 25% of gaps are 7-minute idles,
    40% of runs clear tool results while warm with few calls left, 14k tokens of non-deferred
    tool definitions on every call."""
    for d in devs:
        dev = b.dev("agents", d)
        for day in range(b.days):
            r = b.rng("agents", d, day)
            if r.random() >= 0.75:
                continue
            for run in range(r.randint(1, 3)):
                start = _session_start(r, b, day, (1 + 7 * run, 6 + 7 * run))
                _agent_run(b, r, dev, start, (d, day, run))


def _agent_run(b: _Builder, r: random.Random, dev: DevInfo, start: int, key: tuple) -> None:
    sk = b.open_session("agents", dev, _uuid("agents", b.seed, *key), "trace@2")
    lane_key = b.lane_key(sk, "main")
    spec = _LaneSpec(team="agents", dev=dev, kind=LaneKind.API_RUN, channel="anthropic_api",
                     billing_path="api_key", scope="ws:" + b.hints.workspaces["agents"],
                     agent_product="agent_sdk", workload=WorkloadClass.SERVICE,
                     entrypoint="sdk-py", client_version=None, tool_choice="auto", stream=True,
                     context_management="set")
    n = r.randint(8, 18)
    edit_at = n - 1 - r.randint(1, 5) if r.random() < 0.4 and n >= 12 else None
    blocks = _Blocks()
    rows: list[_Row] = []
    t = start
    prev_prefix = 0
    for i in range(n):
        edits: tuple[tuple[str, int], ...] = ()
        if i == 0:
            blocks.append(_blk(key, "task", "messages", "text", "user", r.randint(4_000, 12_000)))
            reads, writes, gap = 0, blocks.tokens, 0
        else:
            idle = r.random() < 0.25 and i != edit_at
            gap = r.randint(20, 60) if i == edit_at else (
                r.randint(390, 450) if idle else r.randint(10, 200))
            t += gap * 1000
            keep_until = len(blocks.items)
            if i == edit_at:
                cleared, keep_until = blocks.clear_tool_results(key, i)
                if cleared:
                    edits = (("clear_tool_uses_20250919", cleared),)
            blocks.append(_blk(key, f"use{i}", "messages", "tool_use", "assistant",
                               r.randint(100, 300)))
            size = r.randint(8_000, 16_000) if r.random() < 0.3 else r.randint(1_500, 4_000)
            blocks.append(_blk(key, f"res{i}", "messages", "tool_result", "user", size))
            if edits:
                reads = sum(bl.est_tokens or 0 for bl in blocks.items[:keep_until])
            elif gap <= 300:
                reads = prev_prefix
            else:
                reads = 0
            writes = blocks.tokens - reads
        prev_prefix = reads + writes
        marker = (Breakpoint(block_index=len(blocks.items) - 1, ttl="5m"),)
        rows.append(_Row(ts_ms=t + r.randint(0, 999),
                         usage=UsageBuckets(cache_read=reads, cache_write_5m=writes,
                                            output=r.randint(200, 900)),
                         model=_OPUS55, stop_reason="end_turn" if i == n - 1 else "tool_use",
                         applied_edits=edits, fingerprint=blocks.fingerprint(),
                         breakpoints=marker, duration_ms=r.randint(3_000, 25_000)))
    b.add_lane(spec, lane_key, rows)
    b.close_session()


def _blk(key: tuple, name: str, tier: str, kind: str, role: str | None, est: int,
         pos: int = 0) -> BlockRef:
    """A content-free block: HMACs (under :data:`FLEET_FP_KEY`) of synthetic content that is
    never written anywhere."""
    content = f"{key}:{name}".encode()
    tag = tier.encode() + b"\0"
    cpt10 = {"tool_def": 27, "tool_result": 25}.get(kind, 36)
    return BlockRef(h=hmac_hex(FLEET_FP_KEY, tag + content, 32),
                    h_sorted=hmac_hex(FLEET_FP_KEY, b"sorted\0" + content, 32),
                    h_norm=hmac_hex(FLEET_FP_KEY, b"norm\0" + tag + content, 32),
                    tier=tier, kind=kind, role=role, n_bytes=est * cpt10 // 10, est_tokens=est,
                    lookback_pos=pos)


def _base_blocks() -> tuple[BlockRef, ...]:
    """Twenty non-deferred tool definitions (14,000 tokens) and one system block, shared by every
    run (same hashes)."""
    per = _TOOL_DEF_TOKENS // _TOOL_DEFS
    tools = [_blk(("tools",), f"tool{i}", "tools", "tool_def", None, per, i)
             for i in range(_TOOL_DEFS)]
    system = _blk(("system",), "system", "system", "system_text", None, _SYSTEM_TOKENS,
                  _TOOL_DEFS)
    return (*tools, system)


_BASE_BLOCKS = _base_blocks()


class _Blocks:
    """The rendered blocks of an agent run in wire order, with collapsed lookback positions
    (consecutive tool_use / tool_result blocks share a position) and a running token count."""

    def __init__(self) -> None:
        self.items: list[BlockRef] = list(_BASE_BLOCKS)
        self.tokens = sum(bl.est_tokens or 0 for bl in self.items)

    def append(self, block: BlockRef) -> None:
        """Append a block with its collapsed lookback position."""
        last = self.items[-1]
        same = block.kind == last.kind and block.kind in ("tool_use", "tool_result")
        pos = last.lookback_pos if same else last.lookback_pos + 1
        self.items.append(dataclasses.replace(block, lookback_pos=pos))
        self.tokens += block.est_tokens or 0

    def clear_tool_results(self, key: tuple, step: int) -> tuple[int, int]:
        """Context editing (``clear_tool_uses``): the oldest tool results (keeping the last three)
        become an 8-token placeholder until ≥ 15,000 tokens are cleared; positions do not move.
        Returns ``(cleared tokens, index of the first cleared block)``."""
        results = [i for i, bl in enumerate(self.items) if bl.kind == "tool_result"]
        cleared = 0
        first = len(self.items)
        for i in results[:-3]:
            if cleared >= 15_000:
                break
            old = self.items[i]
            self.items[i] = _blk(key, f"cleared{step}.{i}", "messages", "tool_result", "user", 8,
                                 old.lookback_pos)
            cleared += (old.est_tokens or 0) - 8
            first = min(first, i)
        self.tokens -= cleared
        return cleared, first

    def fingerprint(self) -> ContentFingerprint:
        """The current block list as a ContentFingerprint."""
        n = len(self.items)
        return ContentFingerprint(key_id=_FP_KEY_ID, blocks=tuple(self.items),
                                  tier_end=(_TOOL_DEFS, _TOOL_DEFS + 1, n))


def _gen_healthy(team: str, p_active: float) -> Callable[[_Builder, Sequence[int]], None]:
    """Healthy Claude Code usage on Sonnet 5: every gap within the 5m TTL (no miss event),
    contexts below 150k, effort medium, occasional Sonnet subagents started > 10 s apart."""

    def gen(b: _Builder, devs: Sequence[int]) -> None:
        for d in devs:
            dev = b.dev(team, d)
            for day in range(b.days):
                r = b.rng(team, d, day)
                if r.random() >= p_active:
                    continue
                for s in range(1 if r.random() < 0.7 else 2):
                    start = _session_start(r, b, day, (8 + 5 * s, 12 + 5 * s))
                    sk = b.open_session(team, dev, _uuid(team, b.seed, d, day, s),
                                        "otlp" if team == "core" else "claude-code")
                    lane_key = b.lane_key(sk, "main")
                    spec = _cc_spec(b, team, dev, LaneKind.MAIN, effort="medium")
                    rows = _cc_main_rows(b, r, start, n=r.randint(6, 18), model=_SONNET5,
                                         ttl="5m", p_long=0.0, long_s=(320, 321),
                                         t0=(15_000, 25_000), app=(1_000, 4_000),
                                         out=(150, 1_200))
                    b.add_lane(spec, lane_key, rows, events=_human_events(lane_key, rows))
                    if r.random() < 0.3:
                        k = r.randint(1, len(rows) - 1)
                        _subagent(b, r, sk, lane_key, team, dev,
                                  rows[k].ts_ms + r.randint(15, 40) * 1000, model=_SONNET5,
                                  n=(3, 7), effort="medium")
                    b.close_session()

    return gen


_GENERATORS: dict[str, Callable[[_Builder, Sequence[int]], None]] = {
    "platform": _gen_platform,
    "payments": _gen_payments,
    "search": _gen_search,
    "mobile": _gen_mobile,
    "infra": _gen_infra,
    "data": _gen_data,
    "ops": _gen_ops,
    "ci-bots": _gen_ci,
    "agents": _gen_agents,
    "core": _gen_healthy("core", 0.6),
    "tiny": _gen_healthy("tiny", 0.5),
}


# ---------------------------------------------------------------------------------------------
# provider-side records (Admin pages, Claude Code Analytics, CUR 2.0)
# ---------------------------------------------------------------------------------------------

#: Reference contract discounts: the Anthropic cost report bills 85% of list, the CUR 90%.
CONTRACT_MULTIPLIERS: Mapping[str, Decimal] = {"anthropic_api": Decimal("0.85"),
                                               "bedrock": Decimal("0.90")}
#: cost_report ``token_type`` per ledger bucket (primary source: the Cost API reference,
#: checked 2026-09-23).
TOKEN_TYPES: Mapping[str, str] = {
    "uncached_input": "uncached_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write_5m": "cache_creation.ephemeral_5m_input_tokens",
    "cache_write_1h": "cache_creation.ephemeral_1h_input_tokens",
    "output": "output_tokens",
}
_TOKEN_LABELS = {
    "uncached_input": "Input Tokens", "cache_read": "Cache Read Input Tokens",
    "cache_write_5m": "5m Cache Write Input Tokens",
    "cache_write_1h": "1h Cache Write Input Tokens", "output": "Output Tokens",
}
#: CUR 2.0 usage types for Bedrock in us-east-1 (regional endpoint); the shapes follow
#: ``core.catalog.SKU_RULES`` (all ``verified: false`` until a real export confirms them).
CUR_USAGE_TYPES: Mapping[str, str] = {
    "uncached_input": "USE1-MP:USE1_InputTokenCount-Units",
    "output": "USE1-MP:USE1_OutputTokenCount-Units",
    "cache_read": "USE1-MP:USE1_CacheReadInputTokenCount-Units",
    "cache_write_5m": "USE1-MP:USE1_CacheWriteInputTokenCount-Units",
    "cache_write_1h": "USE1-MP:USE1_CacheWrite1hInputTokenCount-Units",
}
_CUR_LABELS = {"uncached_input": "input tokens", "output": "output tokens",
               "cache_read": "cache read input tokens",
               "cache_write_5m": "cache write input tokens",
               "cache_write_1h": "1h cache write input tokens"}
_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h", "output")


@dataclass
class ProviderPages:
    """Raw provider rows the writers serialise (one entry per page row)."""

    usage_rows: list[dict[str, Any]] = field(default_factory=list)
    cost_rows: list[dict[str, Any]] = field(default_factory=list)
    analytics_rows: list[dict[str, Any]] = field(default_factory=list)
    cur_rows: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ProviderRecords:
    """The provider-side canonical records, the raw page rows and the reconciliation truth."""

    aggregates: tuple[UsageAggregate, ...]
    cost_lines: tuple[CostLine, ...]
    outcomes: tuple[OutcomeAggregate, ...]
    pages: ProviderPages
    recon: Any


def model_display(model: str) -> str:
    """``claude-opus-5-5`` → ``Claude Opus 5.5`` (cost-report descriptions)."""
    parts = model.split("-")[1:]
    name = parts[0].capitalize()
    return "Claude " + name + " " + ".".join(parts[1:])


def _dec_str(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _provisional(date: str, today: str) -> bool:
    return (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(date)).days <= 30


def _provider_records(lanes: Sequence[Lane], hints: FleetHints, *, days: int, today: str,
                      coster: Any, seed: int = 7) -> ProviderRecords:
    """The reference provider records of the fleet (SPEC §18): usage and cost reports for billed
    first-party traffic (cost at 85% of list, the Priority-tier bucket absent from the cost
    report, seat-allowance usage absent from both), Claude Code Analytics per user-day aggregated
    to teams with k = 5, and the CUR 2.0 rows of the Bedrock team at 90% of list."""
    from tokenbill.core.kanon import merge_small_groups
    from tokenbill.core.money import cents_to_nano, usd_str_to_nano
    from tokenbill.synth.truth import ReconTruth

    usage: dict[tuple[str, str, str, str, str, str], list[int]] = {}
    cost: dict[tuple[str, str, str, str], int] = {}
    cur: dict[tuple[str, str, str], int] = {}
    analytics: dict[tuple[str, str], dict[str, Any]] = {}
    priority = seat = unpriced_tokens = 0
    provider_list = {"anthropic_api": 0, "bedrock": 0}
    unpriced_models: set[str] = set()
    for lane in lanes:
        for req in lane.requests:
            attr = req.attribution
            for att in req.attempts:
                ts = att.ts_start_ms
                date = date_of(ts)
                for inf in att.inferences:
                    if inf.billable is False:
                        continue
                    ctx = inf.pricing
                    u = inf.usage
                    out = u.output
                    if inf.usage_source is UsageSource.MESSAGE_START_ONLY:
                        out = max(out, inf.output_upper or out)
                    qty = {"uncached_input": u.uncached_input, "cache_read": u.cache_read,
                           "cache_write_5m": u.cache_write_5m,
                           "cache_write_1h": u.cache_write_1h, "output": out}
                    price_ctx = ctx
                    if coster.unit(ctx, ts) is None:
                        unpriced_models.add(ctx.model)
                        unpriced_tokens += sum(qty.values())
                        price_ctx = dataclasses.replace(ctx, model=_SONNET5, model_raw=_SONNET5)
                    lines = {b: coster.line(b, q, price_ctx, ts) for b, q in qty.items() if q}
                    if attr.agent_product == "claude_code" and ctx.channel == "anthropic_api":
                        dev = _dev_of(hints, req)
                        slot = analytics.setdefault((date, dev.ref), {"models": {},
                                                                      "sessions": set()})
                        slot["sessions"].add(req.session_key)
                        m = slot["models"].setdefault(ctx.model or ctx.model_raw, [0, 0, 0, 0, 0])
                        m[0] += qty["uncached_input"]
                        m[1] += qty["output"]
                        m[2] += qty["cache_read"]
                        m[3] += qty["cache_write_5m"] + qty["cache_write_1h"]
                        m[4] += sum(lines.values())
                    if ctx.billing_path == "subscription":
                        seat += coster.inference(inf, ts) or 0
                        continue
                    if ctx.channel == "anthropic_api":
                        ws = hints.workspaces[lane.team or ""]
                        key = (date, ws, ctx.model or ctx.model_raw, ctx.service_tier, ctx.speed,
                               "global")
                        row = usage.setdefault(key, [0, 0, 0, 0, 0])
                        for i, b in enumerate(_BUCKETS):
                            row[i] += qty[b]
                        if ctx.service_tier == "priority":
                            priority += sum(lines.values())
                            continue
                        for b, amount in lines.items():
                            ck = (date, ws, ctx.model or ctx.model_raw, TOKEN_TYPES[b])
                            cost[ck] = cost.get(ck, 0) + amount
                        provider_list["anthropic_api"] += sum(lines.values())
                    elif ctx.channel == "bedrock":
                        dev = _dev_of(hints, req)
                        for b, q in qty.items():
                            if q:
                                ck3 = (date, dev.iam_arn or "", b)
                                cur[ck3] = cur.get(ck3, 0) + q
                        provider_list["bedrock"] += sum(lines.values())

    pages = ProviderPages()
    aggregates: list[UsageAggregate] = []
    cost_lines: list[CostLine] = []
    outcomes: list[OutcomeAggregate] = []
    for (date, ws, model, tier, speed, geo), vals in sorted(usage.items()):
        pages.usage_rows.append({"date": date, "workspace_id": ws, "model": model,
                                 "service_tier": tier, "speed": speed, "inference_geo": geo,
                                 **dict(zip(_BUCKETS, vals, strict=True))})
        dims = {"channel": "anthropic_api", "workspace_id": ws, "model": model,
                "service_tier": tier, "speed": speed, "inference_geo": geo}
        start = _date_ms(date)
        aggregates.append(UsageAggregate(
            agg_id=stable_id("ag", "anthropic.usage_report", date, ws, model, tier, speed, geo),
            source_kind="anthropic.usage_report", bucket_start_ms=start,
            bucket_end_ms=start + _DAY_MS, dims=tuple(sorted(dims.items())),
            usage=UsageBuckets(uncached_input=vals[0], cache_read=vals[1],
                               cache_write_5m=vals[2], cache_write_1h=vals[3], output=vals[4]),
            finality="provisional" if _provisional(date, today) else "final"))

    mult = CONTRACT_MULTIPLIERS["anthropic_api"]
    for (date, ws, model, token_type), list_nano in sorted(cost.items()):
        cents = _dec_str(Decimal(list_nano) * mult / Decimal(10**7))
        bucket = next(b for b, t in TOKEN_TYPES.items() if t == token_type)
        desc = f"{model_display(model)} Usage - {_TOKEN_LABELS[bucket]}"
        pages.cost_rows.append({"date": date, "workspace_id": ws, "model": model,
                                "token_type": token_type, "description": desc, "amount": cents,
                                "service_tier": "standard", "inference_geo": "global"})
        nano, _rem = cents_to_nano(cents)
        cost_lines.append(CostLine(
            line_id=stable_id("cl", "anthropic.cost_report", date, ws, model, token_type),
            source_kind="anthropic.cost_report", date_utc=date, channel="anthropic_api",
            workspace_id=ws, description=desc, model=model, cost_type="tokens",
            token_type=token_type, sku=None, service_tier="standard", inference_geo="global",
            endpoint_scope=None, amount_nano=nano,
            finality="provisional" if _provisional(date, today) else "final"))

    cur_mult = CONTRACT_MULTIPLIERS["bedrock"]
    account_h = pseudonym(FLEET_NAME_KEY, "h", _AWS_ACCOUNT)
    ref_ctx = PricingContext(provider="anthropic", channel="bedrock", model=_OPUS5,
                             model_raw="us.anthropic." + _OPUS5, endpoint_scope="regional",
                             billing_path="bedrock")
    for (date, arn, bucket), tokens in sorted(cur.items()):
        rates = coster.pricer.resolve(ref_ctx, ts_ms=_date_ms(date))
        rate = {"uncached_input": rates.input, "output": rates.output,
                "cache_read": rates.cache_read, "cache_write_5m": rates.cache_write_5m,
                "cache_write_1h": rates.cache_write_1h}[bucket]
        amount_units = Decimal(tokens).scaleb(-6)
        unblended = amount_units * rate
        net = unblended * cur_mult
        usage_type = CUR_USAGE_TYPES[bucket]
        desc = f"{model_display(_OPUS5)} (Amazon Bedrock Edition) {_CUR_LABELS[bucket]}"
        pages.cur_rows.append({
            "bill_billing_period_start_date": date[:8] + "01T00:00:00Z",
            "bill_payer_account_id": _AWS_ACCOUNT,
            "line_item_line_item_type": "Usage",
            "line_item_usage_start_date": date + "T00:00:00Z",
            "line_item_usage_end_date": _date_add(date, 1) + "T00:00:00Z",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": usage_type,
            "line_item_operation": "InvokeModelInference",
            "line_item_line_item_description": desc,
            "line_item_usage_amount": _dec_str(amount_units),
            "pricing_unit": "1M tokens",
            "line_item_unblended_rate": _dec_str(rate),
            "line_item_unblended_cost": _dec_str(unblended),
            "line_item_net_unblended_cost": _dec_str(net),
            "line_item_currency_code": "USD",
            "line_item_usage_account_id": _AWS_ACCOUNT,
            "line_item_iam_principal": arn,
            "product_region_code": "us-east-1",
            "product_servicecode": "AmazonBedrock",
            "product_product_name": f"{model_display(_OPUS5)} (Amazon Bedrock Edition)",
        })
        amount_nano, _rem = usd_str_to_nano(_dec_str(net))
        list_nano, _rem = usd_str_to_nano(_dec_str(unblended))
        principal = pseudonym(FLEET_ORG_KEY, "p", arn)
        final = "provisional" if _provisional(date, today) else "final"
        cost_lines.append(CostLine(
            line_id=stable_id("cl", "aws.cur2", date, account_h, usage_type, principal),
            source_kind="aws.cur2", date_utc=date, channel="bedrock", workspace_id=account_h,
            description=desc, model=None, cost_type=None, token_type=None, sku=usage_type,
            service_tier=None, inference_geo=None, endpoint_scope=None, amount_nano=amount_nano,
            list_amount_nano=list_nano, finality=final, principal=principal))
        start = _date_ms(date)
        aggregates.append(UsageAggregate(
            agg_id=stable_id("ag", "aws.cur2", date, account_h, usage_type, principal),
            source_kind="aws.cur2", bucket_start_ms=start, bucket_end_ms=start + _DAY_MS,
            dims=(("channel", "bedrock"), ("sku", usage_type), ("workspace_id", account_h)),
            usage=UsageBuckets(uncached_input=tokens), finality=final))

    by_date: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
    for (date, ref), slot in sorted(analytics.items()):
        dev = hints.devs[ref]
        r = _rng(seed, "analytics", ref, date)
        human = dev.team != "ci-bots"
        actor = ({"type": "user_actor", "email_address": dev.email} if human else
                 {"type": "api_actor", "api_key_name": f"{ref}-key"})
        if not human:
            hints.team_map[f"{ref}-key"] = dev.team
        n_sessions = len(slot["sessions"])
        added, removed = r.randint(50, 900) * n_sessions, r.randint(10, 400) * n_sessions
        commits, prs = r.randint(0, 3 * n_sessions), r.randint(0, n_sessions)
        tools = {t: {"accepted": r.randint(0, 40), "rejected": r.randint(0, 6)}
                 for t in ("edit_tool", "multi_edit_tool", "write_tool", "notebook_edit_tool")}
        breakdown = []
        models: dict[str, tuple[int, int, int, int, int]] = {}
        for model, (inp, out, read, create, nano) in sorted(slot["models"].items()):
            cents = int((Decimal(nano) / Decimal(10**7)).quantize(Decimal(1)))
            breakdown.append({"model": model,
                              "tokens": {"input": inp, "output": out, "cache_read": read,
                                         "cache_creation": create},
                              "estimated_cost": {"currency": "USD", "amount": cents}})
            models[model] = (inp, out, read, create, cents)
        pages.analytics_rows.append({
            "date": date + "T00:00:00Z", "actor": actor,
            "organization_id": _uuid("fleet-org"),
            "customer_type": "subscription" if dev.team in ("search", "mobile") else "api",
            "terminal_type": r.choice(("vscode", "iTerm.app", "tmux", "ghostty")),
            "core_metrics": {"num_sessions": n_sessions,
                             "lines_of_code": {"added": added, "removed": removed},
                             "commits_by_claude_code": commits,
                             "pull_requests_by_claude_code": prs},
            "tool_actions": tools, "model_breakdown": breakdown})
        accepted = sum(v["accepted"] for v in tools.values())
        rejected = sum(v["rejected"] for v in tools.values())
        payload = {"models": models, "outcome": (n_sessions, commits, prs, added, removed,
                                                  accepted, rejected)}
        by_date.setdefault(date, []).append((dev.team, 1, payload))
    for date, rows in sorted(by_date.items()):
        per_team: dict[str, tuple[str, int, dict[str, Any]]] = {}
        for team, n, payload in rows:
            prev = per_team.get(team)
            per_team[team] = (team, n, payload) if prev is None else (
                team, prev[1] + n, _add_payload(prev[2], payload))
        merged, _dropped = merge_small_groups(list(per_team.values()), k=5)
        start = _date_ms(date)
        final = "provisional" if _provisional(date, today) else "final"
        for label, n_users, payload in merged:
            for model, (inp, out, read, create, cents) in sorted(payload["models"].items()):
                aggregates.append(UsageAggregate(
                    agg_id=stable_id("ag", "anthropic.cc_analytics", date, label, model),
                    source_kind="anthropic.cc_analytics", bucket_start_ms=start,
                    bucket_end_ms=start + _DAY_MS,
                    dims=(("channel", "anthropic_api"), ("model", model), ("team", label)),
                    usage=UsageBuckets(uncached_input=inp, cache_read=read,
                                       cache_write_unknown=create, output=out),
                    reported_cost_nano=cents * 10**7, reported_cost_basis="provider_estimate",
                    finality=final))
            sess, commits, prs, added, removed, acc, rej = payload["outcome"]
            outcomes.append(OutcomeAggregate(
                date_utc=date, team=label, n_users=n_users, sessions=sess, commits=commits,
                pull_requests=prs, lines_added=added, lines_removed=removed,
                edits_accepted=acc, edits_rejected=rej))

    window = [_date_add(WINDOW_START, d) for d in range(days)]
    invoice = {"anthropic_api": sum(c.amount_nano for c in cost_lines
                                    if c.source_kind == "anthropic.cost_report"),
               "bedrock": sum(c.amount_nano for c in cost_lines if c.source_kind == "aws.cur2")}
    recon = ReconTruth(
        contract_multipliers=tuple((k, str(v)) for k, v in sorted(CONTRACT_MULTIPLIERS.items())),
        invoice_nano=tuple(sorted(invoice.items())),
        provider_list_nano=tuple(sorted(provider_list.items())),
        priority_list_nano=priority, seat_allowance_nano=seat,
        provisional_dates=tuple(d for d in window if _provisional(d, today)),
        unpriced_models=tuple(sorted(unpriced_models)), unpriced_tokens=unpriced_tokens,
        overage_dates=tuple(_date_add(WINDOW_START, d) for d in hints.overage_days))
    return ProviderRecords(aggregates=tuple(aggregates), cost_lines=tuple(cost_lines),
                           outcomes=tuple(outcomes), pages=pages, recon=recon)


def _add_payload(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Sum two analytics payloads (per-model token/cost tuples and outcome counts)."""
    models = dict(a["models"])
    for model, vals in b["models"].items():
        prev = models.get(model)
        models[model] = vals if prev is None else tuple(x + y for x, y in zip(prev, vals,
                                                                              strict=True))
    outcome = tuple(x + y for x, y in zip(a["outcome"], b["outcome"], strict=True))
    return {"models": models, "outcome": outcome}


def _dev_of(hints: FleetHints, req: Request) -> DevInfo:
    return hints.devs[hints.session_dev[req.session_key]]


# ---------------------------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------------------------


def _prepare(b: _Builder) -> None:
    days = b.days
    b.hints.migration_day = days // 2
    first = min(8, days - 3)
    b.hints.overage_days = (first, first + 1)
    for team in TEAMS:
        b.hints.workspaces[team.name] = _workspace(team.name)


def _check_args(seed: object, devs: object, days: object) -> None:
    if type(seed) is not int:
        raise UsageError("seed must be an int")
    team_sizes(devs)  # type: ignore[arg-type]
    if type(days) is not int or not 7 <= days <= 31:
        raise UsageError("days must be an int in [7, 31]")


def generate(seed: int = 7, *, devs: int = 61, days: int = 28, out_dir: Path | None = None,
             scale_requests: int | None = None) -> FleetWorld:
    """Build the synthetic fleet (SPEC §18); see the module docstring.

    With *out_dir* the source files are written there (``source_files`` maps a relative key to
    each path); without it ``source_files`` is empty. With *scale_requests* the world streams
    exactly that many canonical requests lazily (no truth, no files, no provider-side records).
    """
    _check_args(seed, devs, days)
    if out_dir is not None and not isinstance(out_dir, (str, PathLike)):
        raise UsageError("out_dir must be a path")
    sizes = team_sizes(devs)
    last = _date_add(WINDOW_START, days - 1)
    today = _date_add(last, 25)
    if scale_requests is not None:
        if type(scale_requests) is not int or scale_requests < 1:
            raise UsageError("scale_requests must be a positive int")
        return _scale_world(seed, sizes, days, scale_requests, today)
    from tokenbill.synth import truth as _truth  # late: truth imports this module's helpers

    b = _Builder(seed, days)
    _prepare(b)
    for team in TEAMS:
        _GENERATORS[team.name](b, range(sizes[team.name]))
    b.hints.transcript_sessions = _transcript_sessions(b)
    b.hints.headless_sessions = tuple(sorted(
        sk for sk, (_repo, _wf) in b.hints.ci_runs.items()
        if _session_day(b, sk) < min(3, days)))
    requests = tuple(sorted(b.requests, key=lambda q: (q.session_key, q.lane_key, q.seq)))
    events = tuple(sorted(b.events, key=lambda e: (e.lane_key, e.ts_ms, e.kind.value,
                                                   repr(e.attrs))))
    sessions = tuple(sorted(b.sessions, key=lambda s: s.session_key))
    lanes = group_lanes(requests, events, sessions)
    coster = _truth.Coster()
    provider = _provider_records(lanes, b.hints, days=days, today=today, coster=coster,
                                 seed=seed)
    b.hints.pages = provider.pages
    world = FleetWorld(sessions=sessions, requests=requests, events=events,
                       aggregates=provider.aggregates, cost_lines=provider.cost_lines,
                       outcomes=provider.outcomes, source_files={}, truth=None, today=today,
                       seed=seed, days=days, hints=b.hints)
    fleet_truth = _truth.build_truth(world, lanes, provider, coster)
    world = dataclasses.replace(world, truth=fleet_truth)
    if out_dir is not None:
        from tokenbill.synth import writers as _writers

        try:
            files = _writers.write_all(world, Path(out_dir))
        except OSError as exc:   # unwritable / not a directory: a usage error, content-free
            raise UsageError(f"cannot write the fleet source files to out_dir "
                             f"({type(exc).__name__})") from None
        world = dataclasses.replace(
            world, source_files=files,
            truth=_truth.with_sources(fleet_truth, world, lanes, files))
    return world


def _session_day(b: _Builder, session_key: str) -> int:
    return b.hints.session_day.get(session_key, 10**6)


def _transcript_sessions(b: _Builder) -> tuple[str, ...]:
    """The infra transcript sample: every infra session of the first four days plus the
    unpriced-model session."""
    out = []
    for s in b.sessions:
        if b.hints.session_team.get(s.session_key) != "infra":
            continue
        day = (s.started_ms - b.start_ms) // _DAY_MS
        native = b.hints.session_native[s.session_key]
        if day < 4 or native == _uuid("infra-unpriced", b.seed, "infra-04", b.days - 2):
            out.append(s.session_key)
    return tuple(sorted(out))


# ---------------------------------------------------------------------------------------------
# scale mode
# ---------------------------------------------------------------------------------------------


class _StreamBuilder(_Builder):
    """A builder that hands each finished session's records to a sink instead of keeping them."""

    def __init__(self, seed: int, days: int, sink: Callable[[list[Request], list[LaneEvent],
                                                            list[Session]], None]) -> None:
        super().__init__(seed, days)
        self._sink = sink

    def close_session(self) -> None:
        """Hand the session's records to the sink and forget them."""
        super().close_session()
        self._sink(self.requests, self.events, self.sessions)
        self.requests, self.events, self.sessions = [], [], []
        self.hints.session_native.clear()
        self.hints.session_dev.clear()
        self.hints.session_team.clear()
        self.hints.lane_agent.clear()
        self.hints.ci_runs.clear()
        self.hints.session_day.clear()


def _scale_iter(seed: int, sizes: Mapping[str, int], days: int, limit: int, what: str
                ) -> Iterator[Any]:
    """Yield *what* ("requests" | "events" | "sessions") of the first *limit* requests' sessions:
    team patterns over epoch after epoch of virtual developers (seed scoped by epoch)."""
    produced = 0
    buffer: list[Any] = []

    def sink(reqs: list[Request], evs: list[LaneEvent], sess: list[Session]) -> None:
        nonlocal produced
        if produced >= limit:
            return
        take = reqs[: limit - produced]
        produced += len(take)
        if what == "requests":
            buffer.extend(take)
        elif what == "events":
            keep = {q.lane_key for q in take}
            buffer.extend(e for e in evs if e.lane_key in keep)
        else:
            keep_s = {q.session_key for q in take}
            buffer.extend(s for s in sess if s.session_key in keep_s)

    epoch = 0
    while produced < limit:
        b = _StreamBuilder(seed + 1_000_003 * epoch, days, sink)
        b.ref_suffix = f"-e{epoch}" if epoch else ""   # new developers every epoch
        _prepare(b)
        for team in TEAMS:
            gen = _GENERATORS[team.name]
            for d in range(sizes[team.name]):
                if produced >= limit:
                    break
                gen(b, (d,))
                yield from buffer
                buffer.clear()
        epoch += 1
        if epoch > 10**6:  # pragma: no cover - cannot happen: every epoch yields requests
            break


def _scale_world(seed: int, sizes: Mapping[str, int], days: int, limit: int,
                 today: str) -> FleetWorld:
    from tokenbill.synth import truth as _truth

    def stream(what: str) -> _Stream:
        return _Stream(lambda: _scale_iter(seed, sizes, days, limit, what),
                       limit if what == "requests" else None)

    return FleetWorld(sessions=stream("sessions"), requests=stream("requests"),
                      events=stream("events"), aggregates=(), cost_lines=(), outcomes=(),
                      source_files={}, truth=_truth.empty_truth(seed, days, today), today=today,
                      seed=seed, days=days)
