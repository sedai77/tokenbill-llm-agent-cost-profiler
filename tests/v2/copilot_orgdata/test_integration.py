"""CP-ORGDATA outputs through the core: registry sniffing, ``MemoryStore`` + ``MemoryRecordStore``
(``core.extensions.persist``), ``core.pool.detect_plans`` plan evidence, and fixture determinism."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.adapters.github_config import CopilotConfigAdapter
from tokenbill.adapters.github_metrics import CopilotMetricsAdapter
from tokenbill.adapters.github_seats import CopilotSeatsAdapter
from tokenbill.core import extensions, pool, registry
from tokenbill.core.ids import key_id
from tokenbill.core.records import from_json, to_json
from tokenbill.core.testing import MemoryRecordStore, MemoryStore
from tokenbill.core.types import IngestResult

from .helpers import FIXTURES, NAME_KEY, PRINCIPAL_KEY, opts

EXPECTED_ADAPTER = {"config": "github-copilot-config", "metrics": "github-copilot-metrics",
                    "seats": "github-copilot-seats", "agent_tasks": "github-agent-tasks",
                    "usage_records": "github-usage-records"}
WINDOW = {"since_ms": 0, "until_ms": 2**45}


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file() and p.suffix not in (".py", ".pyc")
                  and "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_registry_sniffs_the_owning_adapter(path: Path) -> None:
    adapter = registry.sniff_adapter(path)
    assert adapter is not None and adapter.name == EXPECTED_ADAPTER[path.parent.name]


def test_other_packages_fixtures_are_not_claimed() -> None:
    from tokenbill.adapters.github_agent_tasks import AgentTasksAdapter
    from tokenbill.adapters.github_usage_records import UsageRecordsRefusal

    ours = (CopilotConfigAdapter(), CopilotMetricsAdapter(), CopilotSeatsAdapter(),
            AgentTasksAdapter(), UsageRecordsRefusal())
    # the non-GitHub fixture areas (Copilot areas may legitimately hold GitHub org data)
    others = [p for p in FIXTURES.parent.rglob("*") if p.is_file() and p.suffix != ".pyc"
              and not p.relative_to(FIXTURES.parent).parts[0].startswith("copilot")]
    assert others
    for path in others:
        head = path.read_bytes()[:65536]
        assert not [a.name for a in ours if a.sniff(path, head)], path


def test_store_record_store_and_plan_evidence() -> None:
    store = MemoryStore(org_key=PRINCIPAL_KEY, name_key_id=key_id(NAME_KEY))
    records = MemoryRecordStore(store)
    results = [CopilotSeatsAdapter().read(FIXTURES / "seats", opts()),
               CopilotConfigAdapter().read(FIXTURES / "config", opts()),
               CopilotMetricsAdapter().read(FIXTURES / "metrics" / "users-1-day_12x3.ndjson",
                                            opts())]
    for result in results:
        store.ingest(result)
        counts = extensions.persist([records], result)
        assert counts.get("dq.principal_key_mismatch", 0) == 0
    assert len(records.licenses()) == 12 and len(records.activity()) == 36
    assert len(store.outcomes()) == 6
    assert records.count_users(source="licenses", where={"team": "platform"}, **WINDOW) == 5
    assert records.count_users(source="activity", where={"team": "payments"}, **WINDOW) == 5
    evidence = pool.detect_plans([], records.licenses(), records.config(), month="2026-09")
    assert evidence and {e.source for e in evidence} == {"seats_api"}


def test_org_settings_plan_type_is_plan_evidence(tmp_path: Path) -> None:
    doc = json.loads((FIXTURES / "seats" / "org_seats.json").read_text(encoding="utf-8"))
    for seat in doc["response"]["seats"]:
        seat.pop("plan_type")                                  # the seats say nothing
    seats_path = tmp_path / "seats.json"
    seats_path.write_text(json.dumps(doc), encoding="utf-8")
    org = {"request": {"path": "/orgs/acme-eng/copilot/billing"}, "fetched_ms": 1_790_337_600_000,
           "response": {"seat_breakdown": {"total": 2}, "seat_management_setting": "assign_all",
                        "plan_type": "enterprise"}}
    org_path = tmp_path / "org.json"
    org_path.write_text(json.dumps(org), encoding="utf-8")
    licenses = CopilotSeatsAdapter().read(seats_path, opts(cost_center_map=())).licenses
    config = CopilotConfigAdapter().read(org_path, opts()).config
    assert {s.plan for s in licenses} == {"unknown"}
    (ev,) = pool.detect_plans([], licenses, config, month="2026-09", entity_mode="org")
    assert (ev.entity_id, ev.plan, ev.source) == ("org:acme-eng", "enterprise", "org_settings")
    unknown = pool.detect_plans([], licenses, [], month="2026-09", entity_mode="org")
    assert unknown[0].plan == "unknown"


def test_results_round_trip_through_json() -> None:
    for sub in ("config", "metrics", "seats", "agent_tasks", "usage_records"):
        adapter = registry.sniff_adapter(next(p for p in _fixture_files()
                                              if p.parent.name == sub))
        assert adapter is not None
        result = adapter.read(FIXTURES / sub, opts())
        again = from_json(IngestResult, json.loads(json.dumps(to_json(result))))
        assert to_json(again) == to_json(result)


def test_checked_in_fixtures_equal_a_fresh_build(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import sys

    monkeypatch.setattr(sys, "dont_write_bytecode", True)     # nothing written into fixtures/
    spec = importlib.util.spec_from_file_location("build_fixtures",
                                                  FIXTURES / "build_fixtures.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HERE", tmp_path)
    written = module.build()
    assert sorted(written) == sorted(p.relative_to(FIXTURES).as_posix() for p in _fixture_files())
    for rel in written:
        assert (tmp_path / rel).read_bytes() == (FIXTURES / rel).read_bytes(), rel
