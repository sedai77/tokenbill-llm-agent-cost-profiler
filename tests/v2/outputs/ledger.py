"""A small synthetic ledger for the store-backed OUT tests (MemoryStore here, SqliteStore in the
gate tests): four teams on two days and two channels, one subscription (allowance) lane, one
request without a principal. Attribution names carry no content; the canary is planted only in an
attribute renderers never read (the request's ``source`` locator)."""

from __future__ import annotations

import datetime as _dt
import hashlib

from tokenbill.core.builders import CANARY, make_request
from tokenbill.core.ids import key_id
from tokenbill.core.records import Fidelity, Request, SourceRef
from tokenbill.core.types import IngestResult, SourceInfo

ORG_KEY = b"out-tests-org-key-0123456789abcd"
EPOCH = _dt.date(1970, 1, 1)
DAY_MS = 86_400_000
DAYS = ("2026-09-22", "2026-09-23")
SINCE_MS = (_dt.date(2026, 9, 22) - EPOCH).days * DAY_MS
UNTIL_MS = SINCE_MS + 2 * DAY_MS
TEAMS = {"payments": 7, "search": 6, "tiny": 2, "mini": 1}
GROUP_BY = ("date", "provider", "channel", "model", "team", "cost_center", "project",
            "workspace_id", "lane_kind", "workload_class", "agent_product", "billing_path")


def _ts(day: str, minute: int) -> int:
    return (_dt.date.fromisoformat(day) - EPOCH).days * DAY_MS + 9 * 3_600_000 + minute * 60_000


def requests() -> list[Request]:
    out: list[Request] = []
    for day in DAYS:
        for team, devs in TEAMS.items():
            for dev in range(devs):
                channel = "bedrock" if team == "search" and dev == 5 else "anthropic_api"
                lane = f"L-{team}-{dev}-{day}"
                attr = {"principal": f"r_{team}{dev}", "team": team, "billing_path": "api_key",
                        "agent_product": "claude_code"}
                out.append(make_request(
                    lane, 0, _ts(day, dev), {"uncached_input": 1000 + dev, "cache_read": 20_000,
                                             "cache_write_5m": 1500, "output": 400 + 7 * dev},
                    "claude-opus-5-5", attribution=attr, channel=channel,
                    billing_path="api_key", message_id=f"msg_{lane}",
                    source=SourceRef("trace@2", "s_out", f"line:{dev} {CANARY}",
                                     Fidelity.FULL, 50)))
        out.append(make_request(
            f"L-sub-{day}", 0, _ts(day, 30), {"uncached_input": 500, "output": 100},
            "claude-opus-5-5", attribution={"principal": "r_sub", "team": "payments",
                                            "billing_path": "subscription"},
            billing_path="subscription", message_id=f"msg_sub_{day}"))
        out.append(make_request(
            f"L-anon-{day}", 0, _ts(day, 40), {"uncached_input": 700, "output": 70},
            "claude-opus-5-5", attribution={"team": "search", "billing_path": "api_key"},
            billing_path="api_key", message_id=f"msg_anon_{day}"))
    return out


def ingest_result() -> IngestResult:
    reqs = requests()
    return IngestResult(
        source=SourceInfo(source_id="s_out", adapter="trace@2", name_hmac="h_out",
                          sha256=hashlib.sha256(b"out-ledger").hexdigest(), bytes=1,
                          name_key_id=None, principal_key_id=key_id(ORG_KEY)),
        requests=reqs, sessions=[], events=[], aggregates=[], cost_lines=[], outcomes=[],
        quarantined=[], notes=[], stats={"records": len(reqs)},
        capabilities=frozenset({"usage_sequence"}))
