"""Gate tests of CP-STORE (``@pytest.mark.gate``; merge gate 1): the record store beside the real
``SqliteStore`` (STORE), the CP-BILL / CP-ORGDATA fixtures through their real adapters, and a
CP-HANDOFF bundle in a keyless adopting store. Each test is guarded with
``pytest.importorskip`` on the sibling package it needs."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tokenbill.common import TokenbillError
from tokenbill.copilot.record_store import COPILOT_TABLES, CopilotRecordStore
from tokenbill.core import extensions
from tokenbill.core import testing as kit
from tokenbill.core.builders import make_activity, make_license
from tokenbill.core.ids import key_id
from tokenbill.core.records import record_key
from tokenbill.core.types import IngestOptions, IngestResult

from .support import KEY_A, ORG_KEY, W, p, result, rows

pytestmark = pytest.mark.gate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NAME_KEY = bytes(range(128, 160))
EXPORT_KEY = bytes(range(160, 192))
NOW_MS = 1_790_000_000_000          # 2026-09-21T…Z: a fixed ingest clock
#: Adapters of CP-BILL, CP-ORGDATA and CP-HANDOFF whose fixtures this gate reads.
COPILOT_ADAPTERS = frozenset({"github-ai-usage", "github-metered-usage", "github-billing-api",
                              "github-copilot-config", "github-copilot-metrics",
                              "github-copilot-seats", "github-agent-tasks",
                              "github-copilot-activity-report"})


def sqlite_store(path: Path, **kw: Any) -> Any:
    db = pytest.importorskip("tokenbill.store.db")
    return db.SqliteStore(path, **kw)


def store_dump(path: Path) -> list[str]:
    """``iterdump`` of STORE's own tables (every table that is not a Copilot table)."""
    conn = sqlite3.connect(str(path))
    try:
        own = set(COPILOT_TABLES)
        return [line for line in conn.iterdump()
                if not any(t in line for t in own) and "ON copilot_" not in line]
    finally:
        conn.close()


def copilot_dump(store: CopilotRecordStore) -> list[str]:
    return sorted(repr(x) for x in (*store.licenses(**W), *store.activity(**W),
                                    *store.config(**W)))


# ---------------------------------------------------------------------------------------------
# beside the real SqliteStore
# ---------------------------------------------------------------------------------------------


def test_conformance_with_the_real_ledger_factory() -> None:
    pytest.importorskip("tokenbill.store.db")

    def factory(path: Path, org_key: bytes) -> CopilotRecordStore:   # R-E45
        sqlite_store(path, org_key=org_key)
        return CopilotRecordStore(path)

    assert kit.assert_record_store_conforms(factory, permutations=4)["batches"] == 5


def test_two_record_stores_and_a_ledger_share_one_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    ledger = sqlite_store(path, org_key=ORG_KEY, pricer=kit.FakePricer())
    lines, aggs = rows(100, date="2026-09-10", users=[p(ORG_KEY, "u1")])
    ledger.ingest(result(cost_lines=lines, aggregates=aggs, adapter="github-ai-usage",
                         source_id="rep"))
    one = CopilotRecordStore(path)
    two = CopilotRecordStore(path, create=False)
    assert one.accepted_key_ids() == {key_id(ORG_KEY)}
    before = store_dump(path)
    batch = result([make_license(p(ORG_KEY, "u1"), snapshot_date="2026-09-20")],
                   [make_activity(p(ORG_KEY, "u1"), date_utc="2026-09-20")])
    for store in (one, two, one):
        store.put(batch, principal_key_id=key_id(ORG_KEY))
    assert store_dump(path) == before                 # STORE's tables are untouched
    assert copilot_dump(one) == copilot_dump(two) and len(one.licenses(**W)) == 1
    again = sqlite_store(path, org_key=ORG_KEY)       # the ledger reopens the shared file
    assert len(again.cost_lines(**W)) >= 1
    conn = sqlite3.connect(str(path))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_purge_writes_through_the_store_audit_contract(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    sqlite_store(path, org_key=ORG_KEY)
    store = CopilotRecordStore(path, now_ms=5)
    store.put(result([make_license(p(ORG_KEY, "u1"), snapshot_date="2026-09-20")]),
              principal_key_id=key_id(ORG_KEY))
    assert store.purge(principal=p(ORG_KEY, "u1"), before_ms=None, actor="gate") == 1
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT actor, action, detail_json FROM audit").fetchall()
    conn.close()
    assert ("gate", "purge", '{"by":"principal","rows":1,"store":"copilot"}') in rows


def test_keyless_adopting_ledger_and_record_store(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    ledger = sqlite_store(path, adopt_key_ids=True)
    records = CopilotRecordStore(path)
    lic = make_license(p(KEY_A, "u1"), snapshot_date="2026-09-20")
    bundle = result([lic], adapter="copilot-export", key=KEY_A, source_id="bundle")
    assert records.put(bundle, principal_key_id=key_id(KEY_A))["skipped"] == 1   # not yet
    ledger.ingest(bundle)                              # the ledger adopts the bundle's key id
    assert ledger.meta().get("adopted_key_id") == key_id(KEY_A)
    counts = extensions.persist([records], bundle)
    assert counts["licenses"] == 1 and records.principal_key_id_of(lic) == key_id(KEY_A)
    other = result([make_license(p(ORG_KEY, "x"), snapshot_date="2026-09-20")],
                   adapter="github-copilot-seats", key=ORG_KEY, source_id="seats")
    assert records.put(other, principal_key_id=key_id(ORG_KEY))["skipped"] == 1


# ---------------------------------------------------------------------------------------------
# CP-BILL and CP-ORGDATA fixtures through the real adapters
# ---------------------------------------------------------------------------------------------


def fixture_files(area: str) -> list[Path]:
    root = FIXTURES / area
    if not root.is_dir():
        pytest.skip(f"fixtures of {area} are not merged yet")
    return sorted(f for f in root.rglob("*") if f.is_file()
                  and f.suffix.lower() in (".csv", ".json", ".ndjson", ".jsonl"))


def ingest_options() -> IngestOptions:
    return IngestOptions(identity_mode="central-ingest", principal_key=ORG_KEY,
                         principal_key_id=key_id(ORG_KEY), name_key=NAME_KEY,
                         name_key_id=key_id(NAME_KEY), now_ms=NOW_MS)


def read_fixtures(paths: list[Path]) -> list[IngestResult]:
    from tokenbill.core import registry

    out: list[IngestResult] = []
    for path in paths:
        adapter = registry.sniff_adapter(path)
        if adapter is None or adapter.name not in COPILOT_ADAPTERS:
            continue
        try:
            out.append(adapter.read(path, ingest_options()))
        except TokenbillError:          # fixtures that the adapter refuses on purpose
            continue
    return out


def load(path: Path, results: list[IngestResult]) -> tuple[Any, CopilotRecordStore]:
    ledger = sqlite_store(path, org_key=ORG_KEY, name_key_id=key_id(NAME_KEY),
                          pricer=kit.FakePricer())
    records = CopilotRecordStore(path)
    for res in results:
        ledger.ingest(res)
        extensions.persist([records], res)
    return ledger, records


def ledger_view(ledger: Any) -> list[str]:
    return sorted(repr(x) for x in (*ledger.cost_lines(**W), *ledger.aggregates(**W),
                                    *ledger.outcomes(**W)))


def test_bill_and_orgdata_fixtures_twice_give_identical_contents(tmp_path: Path) -> None:
    pytest.importorskip("tokenbill.store.db")
    pytest.importorskip("tokenbill.adapters.github_billing")
    pytest.importorskip("tokenbill.adapters.github_seats")
    results = read_fixtures(fixture_files("copilot_bill") + fixture_files("copilot_orgdata"))
    if not results:
        pytest.skip("no Copilot fixture was read")
    ledger, records = load(tmp_path / "ledger.db", results)
    first = (ledger_view(ledger), copilot_dump(records))
    for res in results:                                  # the same files again
        ledger.ingest(res)
        extensions.persist([records], res)
    assert (ledger_view(ledger), copilot_dump(records)) == first
    fresh_ledger, fresh = load(tmp_path / "reversed.db", list(reversed(results)))
    assert (ledger_view(fresh_ledger), copilot_dump(fresh)) == first
    people = [x for res in results for x in (*res.licenses, *res.activity)]
    assert len(records.licenses(**W)) + len(records.activity(**W)) == len(
        {(type(x).__name__, record_key(x)) for x in people})
    assert all(x.principal.startswith("p_") for x in records.licenses(**W))


def test_overlapping_exports_keep_one_row_per_natural_id(tmp_path: Path) -> None:
    pytest.importorskip("tokenbill.store.db")
    pytest.importorskip("tokenbill.adapters.github_billing")
    files = [f for f in fixture_files("copilot_bill") if "overlap" in f.name]
    if len(files) < 2:
        pytest.skip("the CP-BILL overlap fixtures are not merged yet")
    results = read_fixtures(files)
    ledger, _ = load(tmp_path / "ledger.db", results)
    lines = ledger.cost_lines(**W)
    ids = [x.line_id for x in lines]
    assert len(ids) == len(set(ids)) == len({x.line_id for r in results for x in r.cost_lines})
    newest = {}
    for res in results:
        for x in res.cost_lines:
            if x.line_id not in newest or x.fetched_ms >= newest[x.line_id].fetched_ms:
                newest[x.line_id] = x
    assert {x.line_id: x.amount_nano for x in lines} == {
        k: v.amount_nano for k, v in newest.items()}


# ---------------------------------------------------------------------------------------------
# a CP-HANDOFF bundle in a keyless adopting ledger
# ---------------------------------------------------------------------------------------------


def bundle_path(tmp_path: Path) -> Path:
    """A ``tokenbill/copilot-export@1`` bundle: CP-HANDOFF's checked-in fixture when there is one,
    else one written by ``handoff.write_bundle`` from builder records under the export key."""
    ready = sorted((FIXTURES / "copilot_handoff").glob("*.tbx")) if (
        FIXTURES / "copilot_handoff").is_dir() else []
    if ready:
        return ready[0]
    handoff = pytest.importorskip("tokenbill.copilot.handoff")
    who = [p(EXPORT_KEY, f"dev{i}") for i in range(6)]
    lines, aggs = rows(600, date="2026-09-10", users=who, team="platform")
    seats = [make_license(x, snapshot_date="2026-09-15", team="platform") for x in who]
    days = [make_activity(x, date_utc="2026-09-10", team="platform") for x in who]
    res = result(seats, days, cost_lines=lines, aggregates=aggs, adapter="github-ai-usage",
                 key=EXPORT_KEY, source_id="raw")
    res = dataclasses.replace(res, source=dataclasses.replace(res.source,
                                                              name_key_id=key_id(EXPORT_KEY)))
    out = tmp_path / "export.tbx"
    handoff.write_bundle([res], out, manifest_seed={
        "tool_version": "gate", "created_ms": NOW_MS, "window": (None, None),
        "principal_key_id": key_id(EXPORT_KEY), "name_key_id": key_id(EXPORT_KEY)},
        leak_terms=frozenset(), k=5, aggregate_only=False)
    return out


def test_handoff_bundle_in_a_keyless_adopting_store_keeps_every_p(tmp_path: Path) -> None:
    pytest.importorskip("tokenbill.store.db")
    pytest.importorskip("tokenbill.adapters.copilot_export")
    from tokenbill.core import registry

    path = bundle_path(tmp_path)
    bundle = registry.get_adapter("copilot-export").read(path, IngestOptions(now_ms=NOW_MS))
    db = tmp_path / "analyst.db"
    ledger = sqlite_store(db, adopt_key_ids=True, pricer=kit.FakePricer())
    records = CopilotRecordStore(db)
    ledger.ingest(bundle)
    counts = extensions.persist([records], bundle)
    assert not counts.get("skipped") and not counts.get("dq.principal_key_mismatch")
    kept = {x.principal for x in (*records.licenses(**W), *records.activity(**W))}
    assert kept == {x.principal for x in (*bundle.licenses, *bundle.activity)}
    assert {c.principal for c in ledger.cost_lines(**W) if c.principal} == {
        c.principal for c in bundle.cost_lines if c.principal}
    assert records.accepted_key_ids() == {bundle.source.principal_key_id}
