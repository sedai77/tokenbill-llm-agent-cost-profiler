"""``copilot-export`` adapter (brief Build 6): conformance, sniffing and key-id adoption in the
analyst's store (ruling R-E21)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tokenbill.adapters.copilot_export import CopilotExportAdapter
from tokenbill.core import registry
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.protocols import Adapter
from tokenbill.core.registry import SNIFF_HEAD_BYTES
from tokenbill.core.testing import (
    MemoryRecordStore,
    MemoryStore,
    assert_adapter_conforms,
    conformance_ingest_options,
)

from .helpers import KEY, KEY2, KID, round_trip_results, seed, use_fake_registry, write_round_trip

ALL_CAPS = {"aggregates", "cost", "copilot_billing", "licenses", "activity", "config", "outcomes"}


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    out = tmp_path / "export.tbx"
    write_round_trip(out)
    return out


def bundle_under(tmp_path: Path, key: bytes, name: str) -> Path:
    from tokenbill.copilot.handoff import write_bundle

    out = tmp_path / name
    write_bundle(round_trip_results(key), out, manifest_seed=seed(key),
                 leak_terms=frozenset(), k=5, aggregate_only=False)
    return out


def test_conforms(bundle: Path) -> None:
    adapter = CopilotExportAdapter()
    assert isinstance(adapter, Adapter) and adapter.name == "copilot-export"
    first = assert_adapter_conforms(
        adapter, bundle, expect_capabilities=ALL_CAPS,
        opts=conformance_ingest_options(principal_key_id=KID, name_key_id=KID))
    assert first.source.principal_key_id == KID
    assert len(first.licenses) == 5
    assert all(n.code.startswith("export:") for n in first.notes)


def test_sniff(bundle: Path, tmp_path: Path) -> None:
    adapter = CopilotExportAdapter()
    assert adapter.sniff(bundle, bundle.read_bytes()[:SNIFF_HEAD_BYTES])
    xlsx = tmp_path / "book.xlsx"
    with zipfile.ZipFile(xlsx, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
    assert not adapter.sniff(xlsx, xlsx.read_bytes())
    late = tmp_path / "late.zip"
    with zipfile.ZipFile(late, "w") as zf:
        zf.writestr("readme.txt", "x")
        zf.writestr("manifest.json", "{}")
    assert not adapter.sniff(late, late.read_bytes())
    assert not adapter.sniff(bundle, b"report_time,login\n")
    assert not adapter.sniff(bundle, b"")


def test_registry_resolves_the_bundle(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch)
    adapter = registry.sniff_adapter(bundle)
    assert adapter is not None and adapter.name == "copilot-export"
    monkeypatch.undo()
    assert registry.get_adapter("copilot-export").name == "copilot-export"


def test_capabilities_are_those_present(tmp_path: Path) -> None:
    from tokenbill.copilot.handoff import write_bundle

    out = tmp_path / "config-only.tbx"
    write_bundle([round_trip_results()[2]], out, manifest_seed=seed(), leak_terms=frozenset(),
                 k=5, aggregate_only=False)
    res = CopilotExportAdapter().read(out, conformance_ingest_options())
    assert res.capabilities == frozenset({"config"})


def test_store_adopts_the_bundle_key_and_keeps_every_pseudonym(bundle: Path,
                                                                tmp_path: Path) -> None:
    store = MemoryStore(adopt_key_ids=True)
    records = MemoryRecordStore(store)
    res = CopilotExportAdapter().read(bundle, conformance_ingest_options())
    counts = store.ingest(res)
    assert counts["principals_nulled"] == 0 and counts["names_nulled"] == 0
    assert store.meta()["adopted_key_id"] == KID and store.meta()["org_key_mode"] == "adopted"
    principals = {c.principal for c in store.cost_lines() if c.principal}
    assert principals == {c.principal for c in res.cost_lines if c.principal}
    assert any(c.repo for c in store.cost_lines())
    stored = records.put(res, principal_key_id=res.source.principal_key_id)
    assert stored.get("dq.principal_key_mismatch", 0) == 0
    assert records.count_users(source="licenses", since_ms=0, until_ms=2**53, where={}) == 5

    other = bundle_under(tmp_path, KEY2, "other.tbx")
    res2 = CopilotExportAdapter().read(other, conformance_ingest_options())
    assert res2.source.principal_key_id == key_id(KEY2) != key_id(KEY)
    before = len(store.cost_lines())
    with pytest.raises(UsageError, match="same export key"):
        store.ingest(res2)
    assert len(store.cost_lines()) == before
    assert store.meta()["adopted_key_id"] == KID


def test_read_needs_options(bundle: Path) -> None:
    with pytest.raises(UsageError):
        CopilotExportAdapter().read(bundle, None)  # type: ignore[arg-type]


def test_the_same_key_twice_is_one_adoption(tmp_path: Path) -> None:
    one = bundle_under(tmp_path, KEY, "one.tbx")
    store = MemoryStore(adopt_key_ids=True)
    store.ingest(CopilotExportAdapter().read(one, conformance_ingest_options()))
    store.ingest(CopilotExportAdapter().read(one, conformance_ingest_options()))
    assert len([a for a in store.audit_log() if a[2] == "adopt_key_id"]) == 1
