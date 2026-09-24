"""MemoryRecordStore and ``assert_record_store_conforms`` (CORE-AMENDMENTS K-5; addendum §7.2;
ruling R-E21; F-KIT-C acceptance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.core import testing as kit
from tokenbill.core.builders import make_activity, make_config, make_license
from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.protocols import ExtRecordStore
from tokenbill.core.types import IngestResult, SourceInfo

W = {"since_ms": 0, "until_ms": 2**53}
ORG = kit.RECORD_STORE_ORG_KEY
A = bytes(range(3, 35))
B = bytes(range(7, 39))


def rs_factory(path: Path) -> kit.MemoryRecordStore:
    assert isinstance(path, Path)
    return kit.MemoryRecordStore(kit.MemoryStore(org_key=ORG))


def test_memory_record_store_conforms() -> None:
    summary = kit.assert_record_store_conforms(rs_factory)
    assert summary == {"batches": 5, "orders": 12}
    seen: list[bytes] = []

    def two_args(path: Path, org_key: bytes) -> kit.MemoryRecordStore:
        seen.append(org_key)
        return kit.MemoryRecordStore(org_key_id=key_id(org_key))

    kit.assert_record_store_conforms(two_args, permutations=3)
    assert seen and set(seen) == {ORG}
    assert isinstance(kit.MemoryRecordStore(), ExtRecordStore)


def lic(key: bytes, who: str, **kw) -> object:
    return make_license(pseudonym(key, "p", who), snapshot_date="2026-09-20", **kw)


def result(*licenses, activity=(), config=(), key: bytes | None = None) -> IngestResult:
    src = SourceInfo(source_id="x", adapter="copilot-export", name_hmac="h_" + "2" * 20,
                     sha256="x", bytes=1, name_key_id=None,
                     principal_key_id=key_id(key) if key else None)
    r = IngestResult(source=src, requests=[], sessions=[], events=[], aggregates=[],
                     cost_lines=[], outcomes=[], quarantined=[], notes=[], stats={},
                     capabilities=frozenset())
    r.licenses, r.activity, r.config = list(licenses), list(activity), list(config)
    return r


def test_key_ids_follow_the_ledger_meta_including_adoption() -> None:
    ledger = kit.MemoryStore(adopt_key_ids=True)
    rs = kit.MemoryRecordStore(ledger)
    assert rs.accepted_key_ids() == frozenset()
    early = rs.put(result(lic(A, "u1")), principal_key_id=key_id(A))
    assert early[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 1 and rs.licenses(**W) == []
    bundle = result(key=A)
    ledger.ingest(bundle)  # a copilot-export source: the ledger adopts A
    assert rs.accepted_key_ids() == {key_id(A)}
    ok = rs.put(result(lic(A, "u1"), activity=[make_activity(pseudonym(A, "p", "u1"))]),
                principal_key_id=key_id(A))
    assert (ok["licenses"], ok["activity"], ok[kit.DQ_PRINCIPAL_KEY_MISMATCH]) == (1, 1, 0)
    other = rs.put(result(lic(B, "x")), principal_key_id=key_id(B))
    assert other["skipped"] == 1 and len(rs.licenses(**W)) == 1
    (stored,) = rs.licenses(**W)
    assert rs.principal_key_id_of(stored) == key_id(A)
    assert rs.principal_key_id_of(lic(A, "nobody")) is None  # type: ignore[arg-type]


def test_org_keyed_ledger_with_adoption_accepts_both_key_ids() -> None:
    ledger = kit.MemoryStore(org_key=ORG, adopt_key_ids=True)
    ledger.ingest(result(key=A))
    rs = kit.MemoryRecordStore(ledger)
    assert rs.accepted_key_ids() == {key_id(ORG), key_id(A)}
    rs.put(result(lic(ORG, "u1", team="t")), principal_key_id=key_id(ORG))
    rs.put(result(lic(A, "u1", team="t")), principal_key_id=key_id(A))
    (k_row,) = [x for x in rs.licenses(**W) if x.principal == pseudonym(ORG, "p", "u1")]
    (a_row,) = [x for x in rs.licenses(**W) if x.principal == pseudonym(A, "p", "u1")]
    assert rs.principal_key_id_of(k_row) == key_id(ORG)
    assert rs.principal_key_id_of(a_row) == key_id(A)
    # the same person under two key ids is two values: counted twice, never joined
    assert rs.count_users(where={"team": "t"}, source="licenses", **W) == 2


def test_configuration_rows_carry_no_person_and_are_always_stored() -> None:
    rs = kit.MemoryRecordStore(org_key_id=key_id(ORG))
    cfg = make_config("run_flags", {"promo_eligible": True})
    counts = rs.put(result(lic(ORG, "u1"), config=[cfg]), principal_key_id=None)
    assert (counts["licenses"], counts["config"], counts["skipped"]) == (0, 1, 1)
    assert rs.config(**W) == [cfg] and rs.licenses(**W) == []
    assert rs.put(result(config=[cfg]), principal_key_id="k_whatever")["config"] == 1
    with pytest.raises(UsageError):
        rs.put("not a result", principal_key_id=None)  # type: ignore[arg-type]


def test_count_users_filters_and_fallbacks() -> None:
    rs = kit.MemoryRecordStore(org_key_id=key_id(ORG))
    rs.put(result(lic(ORG, "u1", team="t", last_activity_surface="jetbrains"),
                  lic(ORG, "u2", team="t", last_activity_surface="vscode"),
                  activity=[make_activity(pseudonym(ORG, "p", "u1"), date_utc="2026-09-20",
                                          team="t")]),
           principal_key_id=key_id(ORG))
    count = rs.count_users
    assert count(where={"editor_family": "jetbrains"}, source="licenses", **W) == 1
    assert count(where={"surface": "vscode"}, source="licenses", **W) == 1
    assert count(where={"date_from": "2026-09-21"}, source="licenses", **W) == 0
    assert count(where={"date_to": "2026-09-20"}, source="licenses", **W) == 2
    assert count(where={"team": "t"}, source="activity", **W) == 1
    assert count(where={"org": "org-a"}, source="activity", **W) == 0  # days carry no org
    assert count(where={"org": ""}, source="activity", **W) == 1
    assert count(where={"cost_center": ""}, source="activity", **W) == 1
    with pytest.raises(PrivacyError):
        count(where={"session": "s"}, source="activity", **W)
    with pytest.raises(UsageError):
        count(where={"lane_kind": "main"}, source="licenses", **W)
    with pytest.raises(UsageError):
        count(where={}, source="requests", **W)
    # aggregate-only bundles: activity_counts → the largest n_people; seat_counts summed per
    # entity and snapshot day, the largest sum
    summary = kit.MemoryRecordStore()
    t0 = kit._date_start_ms("2026-09-20")
    summary.put(result(config=[
        make_config("activity_counts", {"team": "t", "month": "2026-08", "n_people": 6},
                    entity_id="enterprise", snapshot_ms=t0),
        make_config("activity_counts", {"team": "t", "month": "2026-09", "n_people": 8},
                    entity_id="enterprise", snapshot_ms=t0),
        make_config("seat_counts", {"team": "t", "bucket": "0-7", "n": 4, "n_people": 4},
                    entity_id="org:a", snapshot_ms=t0),
        make_config("seat_counts", {"team": "t", "bucket": "none_90d", "n": 3, "n_people": 3},
                    entity_id="org:a", snapshot_ms=t0),
        make_config("seat_counts", {"team": "t", "bucket": "0-7", "n": 5, "n_people": 5},
                    entity_id="org:b", snapshot_ms=t0),
        make_config("seat_counts", {"team": "t", "bucket": "0-7", "n": 2, "n_people": 2},
                    entity_id="cc:ml", snapshot_ms=t0),
    ]), principal_key_id=None)
    assert summary.count_users(where={"team": "t"}, source="activity", **W) == 8
    assert summary.count_users(where={"team": "t"}, source="licenses", **W) == 7
    assert summary.count_users(where={"org": "b"}, source="licenses", **W) == 5
    assert summary.count_users(where={"cost_center": "ml"}, source="licenses", **W) == 2
    assert summary.count_users(where={"bucket": "none_90d"}, source="licenses", **W) == 3
    assert summary.count_users(where={"surface": "vscode"}, source="licenses", **W) == 0
    assert summary.count_users(where={"product": "github_copilot"}, source="licenses",
                               **W) == 7
    assert summary.count_users(where={"product": "other"}, source="licenses", **W) == 0
    assert summary.count_users(where={"date_from": "2026-09-21"}, source="licenses", **W) == 0


def test_windows_retention_and_purge() -> None:
    rs = kit.MemoryRecordStore(org_key_id=key_id(ORG), now_ms=3)
    old = make_license(pseudonym(ORG, "p", "o"), snapshot_date="2026-05-01")
    cfg = make_config("run_flags", {"promo_eligible": False},
                      snapshot_ms=kit._date_start_ms("2026-05-01"))
    rs.put(result(old, lic(ORG, "u1"), config=[cfg]), principal_key_id=key_id(ORG))
    assert len(rs.licenses(since_ms=kit._date_start_ms("2026-09-01"))) == 1
    assert len(rs.config(until_ms=kit._date_start_ms("2026-06-01"))) == 1
    with pytest.raises(UsageError):
        rs.licenses(since=0)  # type: ignore[call-arg]
    assert rs.retain(identity_before_ms=kit._date_start_ms("2026-06-01")) == 1
    assert len(rs.config(**W)) == 1
    with pytest.raises(UsageError):
        rs.purge(principal=None, before_ms=None, actor="dpo")
    assert rs.purge(principal=None, before_ms=kit._date_start_ms("2026-06-01"),
                    actor="dpo") == 1  # the old configuration row
    assert rs.purge(principal=pseudonym(ORG, "p", "u1"), before_ms=None, actor="dpo") == 1
    log = rs.audit_log()
    assert [row[2] for row in log] == ["purge", "purge"] and all(row[0] == 3 for row in log)
    assert "u1" not in log[1][3] and "p_" not in log[1][3]


class _FirstWins(kit.MemoryRecordStore):
    @staticmethod
    def _upsert(table, rec, value):  # type: ignore[override]
        from tokenbill.core.records import record_key

        table.setdefault(record_key(rec), value)


class _AnyKey(kit.MemoryRecordStore):
    def accepted_key_ids(self):  # type: ignore[override]
        return frozenset({"k_not_accepted", *super().accepted_key_ids()})


class _PerRow(kit.MemoryRecordStore):
    def count_users(self, *, since_ms, until_ms, where, source):  # type: ignore[override]
        rows = self.licenses(since_ms=since_ms, until_ms=until_ms) if source == "licenses" \
            else self.activity(since_ms=since_ms, until_ms=until_ms)
        return len(rows) if not where else super().count_users(
            since_ms=since_ms, until_ms=until_ms, where=where, source=source)


class _RetainsConfig(kit.MemoryRecordStore):
    def retain(self, *, identity_before_ms):  # type: ignore[override]
        n = super().retain(identity_before_ms=identity_before_ms)
        self._config.clear()
        return n


class _NoFallback(kit.MemoryRecordStore):
    def count_users(self, *, since_ms, until_ms, where, source):  # type: ignore[override]
        if not self.licenses(since_ms=since_ms, until_ms=until_ms):
            return 0
        return super().count_users(since_ms=since_ms, until_ms=until_ms, where=where,
                                   source=source)


@pytest.mark.parametrize("broken,message", [
    (_FirstWins, "latest-fetch-wins"), (_AnyKey, "another principal key id"),
    (_PerRow, r"count_users\(licenses, \{\}\) = 5 != 4"), (_RetainsConfig, "retain"),
    (_NoFallback, "seat_counts"),
])
def test_record_store_conformance_catches(broken, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_record_store_conforms(
            lambda path: broken(kit.MemoryStore(org_key=ORG)), permutations=2)
