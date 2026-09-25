"""``ingest_paths`` / ``ingest_options`` into ``MemoryStore`` with fake adapters registered for the
test (SPEC §15; Copilot amendment A-9: records persisted after the ledger ingest, deferred files
re-read)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.core import extensions, registry
from tokenbill.core.builders import CANARY, make_principal
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.registry import ExtensionSpec
from tokenbill.core.testing import MemoryRecordStore, MemoryStore
from tokenbill.core.types import IngestOptions
from tokenbill.pipeline import common
from tokenbill.pipeline.common import (
    DQ_RECORDS_NOT_PERSISTED,
    ingest_options,
    ingest_paths,
    open_store,
)

from .support import (
    ALT_FORMAT,
    EXPORT_KEY,
    FILES_FORMAT,
    H_REPO,
    NAME_KEY,
    ORG_KEY,
    FakeUsageAdapter,
    PathRecordStore,
    PathStore,
    make_env,
    read_fake,
    req,
    write_fake,
)

pytestmark = pytest.mark.usefixtures("fake_adapters")


def _store() -> MemoryStore:
    return MemoryStore(org_key=ORG_KEY)


def _codes(notes: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in notes:
        out[n.code] = out.get(n.code, 0) + n.count
    return out


# ---------------------------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------------------------


def test_auto_sniff_ingests_and_prices_with_the_env_pricer(tmp_path: Path) -> None:
    f1 = write_fake(tmp_path / "a.jsonl", [req("L1", 0, principal="r_alice"),
                                           req("L1", 1, principal="r_alice")])
    f2 = write_fake(tmp_path / "b.jsonl", [req("L2", 0, team="search", principal="r_bob")])
    store, env = _store(), make_env()
    sources, notes = ingest_paths(store, [f1, f2], env, IngestOptions())
    assert [s.adapter for s in sources] == ["wiring-fake", "wiring-fake"]
    assert len(list(store.iter_requests())) == 3
    total = store.aggregate(since_ms=0, until_ms=2**53, group_by=()).rows[0].priced
    assert total.exact.nano == 3 * (1000 * 4_000 + 100 * 20_000)   # $4 / $20 per MTok, 1000 + 100
    assert notes == []
    # r_ principals were pseudonymized with the org key at write time
    principals = {r.attribution.principal for r in store.iter_requests()}
    assert principals == {pseudonym(ORG_KEY, "p", "alice"), pseudonym(ORG_KEY, "p", "bob")}


def test_explicit_adapter_and_errors(tmp_path: Path) -> None:
    f = write_fake(tmp_path / "a.jsonl", [req("L1", 0)])
    store, env = _store(), make_env()
    sources, _ = ingest_paths(store, [f], env, IngestOptions(), adapter="wiring-fake")
    assert sources[0].adapter == "wiring-fake"
    with pytest.raises(UsageError, match="unknown adapter"):
        ingest_paths(store, [f], env, IngestOptions(), adapter="no-such-adapter")
    with pytest.raises(UsageError, match="not installed"):
        ingest_paths(store, [f], env, IngestOptions(), adapter="wiring-missing")
    with pytest.raises(UsageError, match="sequence of paths"):
        ingest_paths(store, str(f), env, IngestOptions())  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="adapter"):
        ingest_paths(store, [f], env, IngestOptions(), adapter="")
    with pytest.raises(UsageError, match="IngestOptions"):
        ingest_paths(store, [f], env, {"lenient": True})  # type: ignore[arg-type]
    with pytest.raises(ContractViolation, match="IngestResult"):
        ingest_paths(store, [f], env, IngestOptions(), adapter="wiring-broken")


def test_unrecognized_file_is_a_usage_error(tmp_path: Path) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("hello\n")
    with pytest.raises(UsageError, match="no adapter recognizes"):
        ingest_paths(_store(), [other], make_env(), IngestOptions())


def test_directory_read_whole_by_its_one_adapter(tmp_path: Path) -> None:
    d = tmp_path / "tree"
    write_fake(d / "p1" / "s1.jsonl", [req("L1", 0)])
    write_fake(d / "p2" / "s2.jsonl", [req("L2", 0)])
    (d / "p2" / "meta.json").write_text("{}")            # unclaimed files are fine
    # hidden members are not sniffed (else two formats would force per-file reads)
    write_fake(d / ".hidden" / "x.jsonl", [req("LX", 0)], fmt=ALT_FORMAT)
    store = _store()
    sources, _ = ingest_paths(store, [d], make_env(), IngestOptions())
    assert len(sources) == 1 and FakeUsageAdapter.reads[0][1] == str(d)
    assert {r.lane_key for r in store.iter_requests()} == {"L1", "L2"}


def test_directory_with_two_formats_is_read_per_file(tmp_path: Path) -> None:
    d = tmp_path / "mixed"
    write_fake(d / "a.jsonl", [req("L1", 0)])
    write_fake(d / "b.jsonl", [req("L2", 0)], fmt=ALT_FORMAT)
    sources, notes = ingest_paths(_store(), [d], make_env(), IngestOptions())
    assert [s.adapter for s in sources] == ["wiring-fake", "wiring-fake-alt"]
    # sniffing b.jsonl passed every unimportable registered adapter: one note each, never repeated
    unavailable = [n for n in notes if n.code == registry.DQ_ADAPTER_UNAVAILABLE]
    assert len({n.detail for n in unavailable}) == len(unavailable)
    assert all(n.count == 1 for n in unavailable)


def test_directory_of_an_adapter_that_reads_files_only(tmp_path: Path) -> None:
    d = tmp_path / "otel"
    write_fake(d / "a.jsonl", [req("L1", 0)], fmt=FILES_FORMAT)
    write_fake(d / "b.jsonl", [req("L2", 0)], fmt=FILES_FORMAT)
    store = _store()
    sources, _ = ingest_paths(store, [d], make_env(), IngestOptions())
    assert [s.adapter for s in sources] == ["wiring-fake-files", "wiring-fake-files"]
    assert {r.lane_key for r in store.iter_requests()} == {"L1", "L2"}


def test_directory_without_a_known_file(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    (d / "x.txt").write_text("nothing")
    with pytest.raises(UsageError, match="directory"):
        ingest_paths(_store(), [d], make_env(), IngestOptions())


def test_same_file_twice_is_read_once(tmp_path: Path) -> None:
    f = write_fake(tmp_path / "a.jsonl", [req("L1", 0)])
    sources, _ = ingest_paths(_store(), [f, f], make_env(), IngestOptions())
    assert len(sources) == 1


def test_notes_merge_and_quarantine_and_key_mismatch(tmp_path: Path) -> None:
    note = {"code": "dq.version_histogram", "severity": "info", "count": 2, "detail": "v1"}
    f1 = write_fake(tmp_path / "a.jsonl", [req("L1", 0)], notes=[note], quarantine=2)
    f2 = write_fake(tmp_path / "b.jsonl", [req("L2", 0)], notes=[
        note, {"code": "dq.quarantined", "severity": "warn", "count": 1, "detail": "own"}],
        quarantine=1)
    f3 = write_fake(tmp_path / "c.jsonl", [req("L3", 0, repo=H_REPO)],
                    name_key_id=key_id(b"another name key, not the env's!!"))
    store = MemoryStore(org_key=ORG_KEY, name_key_id=key_id(NAME_KEY))
    _, notes = ingest_paths(store, [f1, f2, f3], make_env(), IngestOptions())
    by_code = {(n.code, n.detail): n for n in notes}
    assert by_code[("dq.version_histogram", "v1")].count == 4          # merged across files
    assert by_code[("dq.quarantined", "wiring-fake: records quarantined")].count == 2
    assert by_code[("dq.quarantined", "own")].count == 1                # not duplicated
    assert _codes(notes)["dq.name_key_mismatch"] == 1
    assert [r.attribution.repo for r in store.iter_requests() if r.lane_key == "L3"] == [None]


def test_notes_with_figures_and_tokens() -> None:
    from tokenbill.core.labels import Basis, exact
    from tokenbill.core.types import DataQualityNote

    notes = common._Notes()
    notes.add(DataQualityNote("dq.a", "info", 1, "d", tokens=5))
    notes.add(DataQualityNote("dq.a", "info", 2, "d"))
    notes.add(DataQualityNote("dq.a", "info", 1, "d", figure=exact(1, Basis.LIST)))
    notes.add(DataQualityNote("dq.b", "info", 1, "e"))
    notes.add(DataQualityNote("dq.b", "info", 1, "e"), once=True)
    out = notes.result()
    assert (out[0].count, out[0].tokens) == (3, 5) and out[1].figure is not None
    assert out[2].count == 1


# ---------------------------------------------------------------------------------------------
# deferral (addendum §5.10: a mixed file claimed by one adapter is re-read by another)
# ---------------------------------------------------------------------------------------------


def test_deferred_file_is_re_read_once(tmp_path: Path) -> None:
    f = write_fake(tmp_path / "mixed.jsonl", [req("L1", 0)],
                   stats={"defer:wiring-fake-b": 1, "defer:": 3, "defer:zero": 0},
                   b_records=[req("LB", 0, team="copilot-team")])
    store = _store()
    sources, notes = ingest_paths(store, [f], make_env(), IngestOptions())
    assert [s.adapter for s in sources] == ["wiring-fake", "wiring-fake-b"]
    assert [(name, path) for name, path, _ in FakeUsageAdapter.reads] == [
        ("wiring-fake", str(f)), ("wiring-fake-b", str(f))]   # b defers back: not re-read
    assert {r.lane_key for r in store.iter_requests()} == {"L1", "LB"}
    assert notes == []


def test_deferral_to_an_unavailable_adapter_is_a_note(tmp_path: Path) -> None:
    f = write_fake(tmp_path / "mixed.jsonl", [req("L1", 0)],
                   stats={"defer:wiring-missing": 4, "defer:never-registered": 2})
    sources, notes = ingest_paths(_store(), [f], make_env(), IngestOptions())
    assert len(sources) == 1
    details = {n.detail: n.count for n in notes if n.code == registry.DQ_ADAPTER_UNAVAILABLE}
    assert details == {
        "adapter wiring-missing is not installed; deferred resources not read": 4,
        "adapter never-registered is not installed; deferred resources not read": 2}


# ---------------------------------------------------------------------------------------------
# records: persisted after the ledger ingest (R-E21 adoption), or counted when they cannot be
# ---------------------------------------------------------------------------------------------


def _bundle(tmp_path: Path, name: str = "export.tbx") -> Path:
    return write_fake(tmp_path / name, [], principal_key_id=key_id(EXPORT_KEY),
                      licenses=[make_principal(1), make_principal(2)], config=1)


def test_records_persist_after_the_ledger_adopted_the_bundle_key(tmp_path: Path) -> None:
    store = MemoryStore(org_key=ORG_KEY, adopt_key_ids=True)
    records = MemoryRecordStore(store)
    bundle = _bundle(tmp_path)
    sources, notes = ingest_paths(store, [bundle], make_env(), IngestOptions(),
                                  adapter="copilot-export", record_stores=[records])
    assert store.meta()["adopted_key_id"] == key_id(EXPORT_KEY)
    assert len(records.licenses()) == 2 and len(records.config()) == 1
    assert notes == [] and sources[0].principal_key_id == key_id(EXPORT_KEY)
    # the order matters: persisting before the ledger ingest would drop the licenses
    fresh = MemoryStore(org_key=ORG_KEY, adopt_key_ids=True)
    early = MemoryRecordStore(fresh)
    result = read_fake(bundle, IngestOptions(), "copilot-export")
    counts = extensions.persist([early], result)
    assert counts["dq.principal_key_mismatch"] == 2 and early.licenses() == []


def test_records_under_another_key_are_counted(tmp_path: Path) -> None:
    store = _store()                         # no adoption: the bundle key id is not accepted
    records = MemoryRecordStore(store)
    _, notes = ingest_paths(store, [_bundle(tmp_path)], make_env(), IngestOptions(),
                            adapter="copilot-export", record_stores=[records])
    assert _codes(notes)["dq.principal_key_mismatch"] == 2
    assert records.licenses() == [] and len(records.config()) == 1


def test_records_without_a_record_store(tmp_path: Path) -> None:
    _, notes = ingest_paths(_store(), [_bundle(tmp_path)], make_env(), IngestOptions(),
                            adapter="copilot-export")
    assert _codes(notes) == {DQ_RECORDS_NOT_PERSISTED: 3}
    _, notes = ingest_paths(_store(), [_bundle(tmp_path, "b2.tbx")], make_env(), IngestOptions(),
                            adapter="copilot-export", record_stores=[])
    assert _codes(notes) == {DQ_RECORDS_NOT_PERSISTED: 3}


def _fake_extension(monkeypatch: pytest.MonkeyPatch, record_store: str) -> None:
    spec = ExtensionSpec(name="wiringtest", channels=("wiring_channel",), rate_files=(),
                         rate_verifier=None, reconciler=None, record_store=record_store,
                         context_enricher=None, summary_builder=None, section_renderer=None,
                         focus_rows=None, showback=None, policy_targets=(), panel_builder=None,
                         command_module=None)
    monkeypatch.setattr(registry, "EXTENSIONS", {"wiringtest": spec})


def test_record_stores_open_lazily_on_the_ledger_file(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.support:PathStore")
    _fake_extension(monkeypatch, "v2.wiring.support:PathRecordStore")
    env = make_env(org_key=ORG_KEY)
    store = open_store(tmp_path / "ledger.db", env)
    plain = write_fake(tmp_path / "usage.jsonl", [req("L1", 0)])
    ingest_paths(store, [plain], env, IngestOptions())
    assert PathRecordStore.opened == []                   # no records: nothing opened
    bundle = write_fake(tmp_path / "own.tbx", [], principal_key_id=key_id(ORG_KEY),
                        licenses=[make_principal(3)])
    _, notes = ingest_paths(store, [bundle, _bundle(tmp_path)], env, IngestOptions(),
                            adapter="copilot-export")
    assert PathRecordStore.opened == [str(tmp_path / "ledger.db")]    # opened once
    assert PathRecordStore.last is not None and len(PathRecordStore.last.licenses()) == 1
    assert _codes(notes) == {"dq.principal_key_mismatch": 2}


def test_missing_record_store_module_is_a_note(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.support:PathStore")
    _fake_extension(monkeypatch, "v2.wiring.no_such_records:Store")
    env = make_env()
    store = open_store(tmp_path / "ledger.db", env)
    _, notes = ingest_paths(store, [_bundle(tmp_path)], env, IngestOptions(),
                            adapter="copilot-export")
    assert _codes(notes) == {extensions.DQ_EXTENSION_UNAVAILABLE: 1, DQ_RECORDS_NOT_PERSISTED: 3}
    assert isinstance(store, PathStore)


# ---------------------------------------------------------------------------------------------
# naive usage and options
# ---------------------------------------------------------------------------------------------


def test_naive_usage_is_kept_as_source_stats(tmp_path: Path) -> None:
    f = write_fake(tmp_path / "cc.jsonl", [req("L1", 0)], stats={"lines": 3}, naive={
        "claude-opus-5-5": {"uncached_input": 2000, "output": 250, "cache_read": 0},
        "": {"output": 9}})
    store = _store()
    ingest_paths(store, [f], make_env(), IngestOptions())
    assert store.source_stats() == {"lines": 3, "naive:claude-opus-5-5:output": 250,
                                    "naive:claude-opus-5-5:uncached_input": 2000}


def test_options_are_completed_from_the_env(tmp_path: Path) -> None:
    f = write_fake(tmp_path / "a.jsonl", [req("L1", 0)])
    env = make_env(k=7, name_allowlist=frozenset({"github"}))
    base = IngestOptions(identity_mode="central-ingest", name_allowlist=frozenset({"linear"}),
                         since_ms=5, until_ms=10**13)
    ingest_paths(_store(), [f], env, base)
    opts = FakeUsageAdapter.reads[-1][2]
    assert opts.name_key == NAME_KEY and opts.name_key_id == key_id(NAME_KEY)
    assert opts.principal_key == ORG_KEY and opts.principal_key_id == key_id(ORG_KEY)
    assert opts.k_anonymity == 7 and opts.name_allowlist == frozenset({"github", "linear"})
    assert opts.now_ms == env.now_ms and (opts.since_ms, opts.until_ms) == (5, 10**13)
    assert CANARY not in repr(opts) and NAME_KEY.hex() not in repr(opts)


def test_explicit_option_keys_are_kept(tmp_path: Path) -> None:
    other = bytes(range(200, 232))
    base = IngestOptions(identity_mode="install", name_key=other, principal_key=other,
                         k_anonymity=9, now_ms=42)
    opts = common._complete_options(base, make_env(k=5))
    assert opts.name_key == other and opts.name_key_id == key_id(other)
    assert opts.principal_key == other and opts.principal_key_id == key_id(other)
    assert opts.k_anonymity == 9 and opts.now_ms == 42
    # collector modes never get the org key as principal key
    central = common._complete_options(IngestOptions(identity_mode="central"), make_env())
    assert central.principal_key is None
    # nothing to complete: the same object
    done = common._complete_options(opts, make_env(org_key=None, name_key=None, now_ms=0))
    assert done is opts


def test_ingest_options_builder() -> None:
    env = make_env()
    opts = ingest_options(env, since_ms=1, until_ms=2, team_map=(("r1", "payments"),))
    assert opts.identity_mode == "central-ingest" and opts.principal_key == ORG_KEY
    assert opts.team_map == (("r1", "payments"),) and (opts.since_ms, opts.until_ms) == (1, 2)
    assert ingest_options(make_env(org_key=None)).identity_mode == "install"
    assert ingest_options(env, identity_mode="two-stage").identity_mode == "two-stage"
    with pytest.raises(UsageError, match="identity mode"):
        ingest_options(env, identity_mode="email")
    with pytest.raises(UsageError, match="unknown ingest option"):
        ingest_options(env, bogus=1)
