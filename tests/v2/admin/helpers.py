"""Area-local helpers for the ADMIN tests (imported only by tests in ``tests/v2/admin``)."""

from __future__ import annotations

import dataclasses
import gzip
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.adapters.anthropic_admin import (
    ClaudeCodeAnalyticsAdapter,
    CostReportAdapter,
    EnterpriseAnalyticsAdapter,
    UsageReportAdapter,
)
from tokenbill.adapters.cloud_billing import AwsCurAdapter, GcpBillingExportAdapter
from tokenbill.adapters.openai_admin import OpenAICostsAdapter, OpenAIUsageBucketsAdapter
from tokenbill.core import catalog
from tokenbill.core.builders import CANARY
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.records import to_json
from tokenbill.core.types import IngestOptions, IngestResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "admin"
MANIFEST: dict[str, Any] = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
NAME_KEY = bytes(range(0, 32))
PRINCIPAL_KEY = bytes(range(32, 64))
NOW_MS: int = MANIFEST["options"]["now_ms"]
TEAM_MAP: dict[str, str] = MANIFEST["options"]["team_map"]
DAY_MS = 86_400_000

ADAPTERS = {
    "anthropic-usage-report": UsageReportAdapter,
    "anthropic-cost-report": CostReportAdapter,
    "anthropic-cc-analytics": ClaudeCodeAnalyticsAdapter,
    "anthropic-enterprise-analytics": EnterpriseAnalyticsAdapter,
    "openai-usage-buckets": OpenAIUsageBucketsAdapter,
    "openai-costs": OpenAICostsAdapter,
    "aws-cur": AwsCurAdapter,
    "gcp-billing": GcpBillingExportAdapter,
}


def opts(**kw: Any) -> IngestOptions:
    """Central-ingest options with fixed name/principal keys, the MANIFEST team map and clock."""
    base: dict[str, Any] = {
        "identity_mode": "central-ingest", "name_key": NAME_KEY, "name_key_id": key_id(NAME_KEY),
        "principal_key": PRINCIPAL_KEY, "principal_key_id": key_id(PRINCIPAL_KEY),
        "team_map": tuple(sorted(TEAM_MAP.items())), "k_anonymity": 5, "now_ms": NOW_MS,
    }
    base.update(kw)
    return IngestOptions(**base)


def h(value: str) -> str:
    """The expected ``h_`` pseudonym of *value* under the test name key."""
    return pseudonym(NAME_KEY, "h", value)


def p(value: str) -> str:
    """The expected ``p_`` pseudonym of *value* under the test principal key."""
    return pseudonym(PRINCIPAL_KEY, "p", value)


def fixture(rel: str) -> Path:
    return FIXTURES / rel


def manifest_entry(rel: str) -> dict[str, Any]:
    for entry in MANIFEST["files"]:
        if entry["path"] == rel:
            return entry
    raise KeyError(rel)


def read(adapter_name: str, path: Path, **kw: Any) -> IngestResult:
    return ADAPTERS[adapter_name]().read(path, opts(**kw))


def write_json(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, default=_default), encoding="utf-8")
    return path


def write_jsonl(path: Path, objs: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(o, default=_default) + "\n" for o in objs),
                    encoding="utf-8")
    return path


def write_gz(path: Path, data: bytes) -> Path:
    path.write_bytes(gzip.compress(data, mtime=0))
    return path


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(type(obj).__name__)


def strings(obj: Any) -> Iterator[str]:
    """Every string (keys included) inside a JSON-like value."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def result_json(result: IngestResult) -> str:
    return json.dumps(to_json(result), sort_keys=True)


def assert_person_free(result: IngestResult, *raw: str) -> None:
    """No output string carries an e-mail, a raw actor/principal reference, or the canary."""
    blob = result_json(result) + repr(result)
    assert CANARY not in blob
    assert "@" not in blob
    for value in raw:
        assert value not in blob, "raw identity leaked"
    for agg in result.aggregates:
        assert not {k for k, _ in agg.dims} & {"principal", "account_id", "user_id", "actor",
                                               "email", "service_account_id"}


def verified_rules() -> tuple[catalog.SkuRule, ...]:
    """The facts.json SKU rules with ``verified=True`` (for tests of the mapped path)."""
    return tuple(dataclasses.replace(r, verified=True) for r in catalog.SKU_RULES)


def tokens(result: IngestResult) -> int:
    return sum(a.usage.total_input + a.usage.output for a in result.aggregates)


def dims(agg: Any) -> dict[str, str]:
    return dict(agg.dims)
