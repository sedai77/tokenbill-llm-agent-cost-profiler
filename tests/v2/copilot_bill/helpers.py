"""Shared helpers of the CP-BILL tests: fixture paths, keys, options and readers."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from tokenbill.adapters.github_billing import (
    AiUsageReportAdapter,
    BillingApiAdapter,
    MeteredUsageAdapter,
)
from tokenbill.core.ids import key_id
from tokenbill.core.records import to_json
from tokenbill.core.types import IngestOptions, IngestResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "copilot_bill"
MANIFEST: dict[str, Any] = json.loads((FIXTURES / "MANIFEST.json").read_text())
ENTRIES = {e["path"]: e for e in MANIFEST["files"]}
PRINCIPAL_KEY = bytes(range(1, 33))
NAME_KEY = bytes(range(33, 65))
ADAPTERS = {a.name: a for a in (AiUsageReportAdapter(), MeteredUsageAdapter(),
                                BillingApiAdapter())}


def ms(date: str) -> int:
    """Epoch ms of 00:00 UTC on *date* (``YYYY-MM-DD``)."""
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * 86_400_000


def opts(now: str = "2026-09-23", team_map: dict[str, str] | None = None,
         **kw: Any) -> IngestOptions:
    """Central-ingest options with the test keys and the injected clock *now*."""
    base: dict[str, Any] = {
        "identity_mode": "central-ingest", "principal_key": PRINCIPAL_KEY,
        "principal_key_id": key_id(PRINCIPAL_KEY), "name_key": NAME_KEY,
        "name_key_id": key_id(NAME_KEY), "now_ms": ms(now),
        "team_map": tuple(sorted((team_map or {}).items()))}
    base.update(kw)
    return IngestOptions(**base)


def entry_opts(rel: str, **kw: Any) -> IngestOptions:
    """The options a MANIFEST entry names (``now``, ``team_map``), plus *kw*."""
    o = ENTRIES[rel]["options"]
    return opts(kw.pop("now", o.get("now", "2026-09-23")), kw.pop("team_map", o.get("team_map")),
                **kw)


def read(rel: str, adapter: str | None = None, **kw: Any) -> IngestResult:
    """Read fixture *rel* with its MANIFEST adapter and options."""
    name = adapter or ENTRIES[rel]["adapter"]
    return ADAPTERS[name].read(FIXTURES / rel, entry_opts(rel, **kw))


def dump(result: IngestResult) -> str:
    """The canonical JSON text of a result (for leak checks)."""
    return json.dumps(to_json(result), sort_keys=True)


def notes(result: IngestResult) -> dict[str, int]:
    """Data-quality code → count."""
    return {n.code: n.count for n in result.notes}


def token_aggs(result: IngestResult) -> list:
    return [a for a in result.aggregates if a.source_kind == "github.ai_usage_report"]


def coverage_aggs(result: IngestResult) -> list:
    return [a for a in result.aggregates if a.source_kind == "github.ai_usage_report.coverage"]


def write(tmp: Path, name: str, text: str) -> Path:
    path = tmp / name
    path.write_text(text)
    return path
