"""MemoryStore for GitHub Copilot (CORE-AMENDMENTS K-5; ruling R-E21; addendum §7.1; F-KIT-C
acceptance): key-id adoption, cost-line user counts, source stats, pool totals, gateway clusters,
latest-fetch-wins — with the wave-1 behaviour unchanged by default."""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from tokenbill.core import testing as kit
from tokenbill.core.builders import make_ai_usage_row, make_request
from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.protocols import LedgerStats, LedgerStore
from tokenbill.core.types import IngestResult, SourceInfo

W = {"since_ms": 0, "until_ms": 2**53}
K_ORG = bytes(range(10, 42))
A = bytes(range(50, 82))
B = bytes(range(90, 122))
C = bytes(range(130, 162))
DAY_MS = kit._ts("2026-09-10")


def src(source_id: str, adapter: str, principal_key: bytes | None,
        name_key: bytes | None = None) -> SourceInfo:
    return SourceInfo(source_id=source_id, adapter=adapter, name_hmac="h_" + "1" * 20,
                      sha256=source_id, bytes=1,
                      name_key_id=key_id(name_key) if name_key else None,
                      principal_key_id=key_id(principal_key) if principal_key else None)


def batch(source: SourceInfo, principals: list[str], *, lines: bool = True,
          repo_key: bytes | None = None) -> IngestResult:
    reqs, cost_lines = [], []
    for i, who in enumerate(principals):
        attr = {"principal": who, "team": "t", "billing_path": "copilot_pool"}
        if repo_key is not None:
            attr["repo"] = pseudonym(repo_key, "h", f"repo-{i}")
        reqs.append(make_request(f"{source.source_id}-{i}", 0, DAY_MS + i, {"output": 100},
                                 "claude-sonnet-5", provider="github",
                                 channel="github_copilot", billing_path="copilot_pool",
                                 request_id=f"rq-{source.source_id}-{i}", attribution=attr))
        if lines and who.startswith("p_"):
            cost_lines.append(make_ai_usage_row(principal=who, credits=str(i + 1),
                                                date_utc="2026-09-10")[0])
    return IngestResult(source=source, requests=reqs, sessions=[], events=[], aggregates=[],
                        cost_lines=cost_lines, outcomes=[], quarantined=[], notes=[],
                        stats={"records": len(reqs)}, capabilities=frozenset())


def principals(store: kit.MemoryStore) -> set[str | None]:
    return {r.attribution.principal for r in store.iter_requests(**W)}


def test_keyless_store_adopts_the_first_export_bundle() -> None:
    store = kit.MemoryStore(adopt_key_ids=True, now_ms=7)
    assert store.meta()["org_key_mode"] == "none" and store.meta()["adopted_key_id"] == ""
    a_people = [pseudonym(A, "p", u) for u in ("u1", "u2")]
    counts = store.ingest(batch(src("bundle", "copilot-export", A, A), a_people, repo_key=A))
    meta = store.meta()
    assert (meta["adopted_key_id"], meta["org_key_mode"], meta["org_key_id"]) == (
        key_id(A), "adopted", key_id(A))
    assert meta["adopted_name_key_id"] == key_id(A) and meta["name_key_id"] == key_id(A)
    assert principals(store) == set(a_people)
    assert all(r.attribution.repo is not None for r in store.iter_requests(**W))
    assert counts[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 0
    assert {c.principal for c in store.cost_lines(**W)} == set(a_people)
    (row,) = [r for r in store.audit_log() if r[2] == "adopt_key_id"]
    assert row[0] == 7 and key_id(A) in row[3] and "u1" not in row[3]
    # re-ingesting the bundle (or another bundle under A) is fine
    store.ingest(batch(src("bundle-2", "copilot-export", A, A),
                       [pseudonym(A, "p", "u3")]))
    assert len(principals(store)) == 3


def test_other_adapters_are_never_adopted() -> None:
    store = kit.MemoryStore(adopt_key_ids=True)
    b_people = [pseudonym(B, "p", "v1")]
    counts = store.ingest(batch(src("vscode", "copilot-vscode-traces", B), b_people))
    assert counts[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 2  # the request and its cost line
    assert principals(store) == {None} and store.cost_lines(**W)[0].principal is None
    assert store.meta()["adopted_key_id"] == "" and store.meta()["org_key_mode"] == "none"
    assert store.dq_counts()[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 2
    # the bundle arriving afterwards is still adopted, the earlier nulls stay null
    store.ingest(batch(src("bundle", "copilot-export", A, A), [pseudonym(A, "p", "u1")]))
    assert store.meta()["adopted_key_id"] == key_id(A)
    assert principals(store) == {None, pseudonym(A, "p", "u1")}


def test_a_keyless_adopting_store_takes_its_name_key_from_the_bundle_only() -> None:
    """R-E21: a store without its own key sets ``name_key_id`` to the bundle's; a source ingested
    earlier never lends it its name key (the SPEC §7.2 first-ingest rule is for other stores)."""
    store = kit.MemoryStore(adopt_key_ids=True)
    vendor = batch(src("vendor", "github-ai-usage", B, B), [pseudonym(B, "p", "v1")],
                   repo_key=B)
    counts = store.ingest(vendor)
    assert counts["names_nulled"] == 1 and store.meta()["name_key_id"] == ""
    assert {r.attribution.repo for r in store.iter_requests(**W)} == {None}
    store.ingest(batch(src("bundle", "copilot-export", A, A), [pseudonym(A, "p", "u1")],
                       repo_key=A))
    meta = store.meta()
    assert (meta["name_key_id"], meta["adopted_name_key_id"]) == (key_id(A), key_id(A))
    assert {r.attribution.repo for r in store.iter_requests(**W)} == {
        None, pseudonym(A, "h", "repo-0")}
    # a store that is not adopting keeps the SPEC rule: the first source sets the name key id
    plain = kit.MemoryStore()
    plain.ingest(batch(src("vendor", "github-ai-usage", B, B), [pseudonym(B, "p", "v1")],
                       repo_key=B))
    assert plain.meta()["name_key_id"] == key_id(B)


def test_a_second_bundle_key_is_refused_atomically() -> None:
    store = kit.MemoryStore(adopt_key_ids=True)
    store.ingest(batch(src("bundle", "copilot-export", A, A), [pseudonym(A, "p", "u1")]))
    before = kit._store_dump(store)
    with pytest.raises(UsageError, match="same export key"):
        store.ingest(batch(src("bundle-b", "copilot-export", B, B), [pseudonym(B, "p", "x")]))
    assert kit._store_dump(store) == before and store.meta()["adopted_key_id"] == key_id(A)


def test_collector_principals_need_the_own_org_key() -> None:
    keyless = kit.MemoryStore(adopt_key_ids=True)
    keyless.ingest(batch(src("bundle", "copilot-export", A, A), [pseudonym(A, "p", "u1")]))
    with pytest.raises(PrivacyError):
        keyless.ingest(batch(src("cli", "copilot-cli", None), ["r_dev1"], lines=False))


def test_org_keyed_store_keeps_both_key_spaces() -> None:
    store = kit.MemoryStore(org_key=K_ORG, adopt_key_ids=True)
    assert store.meta()["org_key_mode"] == "own"
    store.ingest(batch(src("bundle", "copilot-export", A, A), [pseudonym(A, "p", "u1")]))
    store.ingest(batch(src("cli", "copilot-cli", None), ["r_dev1"], lines=False))
    store.ingest(batch(src("own", "github-ai-usage", K_ORG), [pseudonym(K_ORG, "p", "u9")]))
    counts = store.ingest(batch(src("other", "github-ai-usage", C),
                                [pseudonym(C, "p", "z")]))
    meta = store.meta()
    assert (meta["org_key_id"], meta["adopted_key_id"], meta["org_key_mode"]) == (
        key_id(K_ORG), key_id(A), "own")
    assert principals(store) == {pseudonym(A, "p", "u1"), pseudonym(K_ORG, "p", "dev1"),
                                 pseudonym(K_ORG, "p", "u9"), None}
    assert counts[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 2
    # the two key spaces never join: the same person under A and K are two values
    assert pseudonym(A, "p", "u1") != pseudonym(K_ORG, "p", "u1")
    assert store.purge(principal=pseudonym(A, "p", "u1"), actor="dpo") == 1


def test_default_store_never_adopts() -> None:
    store = kit.MemoryStore(org_key=K_ORG)
    counts = store.ingest(batch(src("bundle", "copilot-export", A, A),
                                [pseudonym(A, "p", "u1")]))
    assert counts[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 2 and principals(store) == {None}
    assert store.meta()["adopted_key_id"] == "" and store.meta()["org_key_mode"] == "own"
    assert counts["dq.name_key_mismatch"] == 2  # the wave-1 lumped count is unchanged


def test_default_behaviour_is_the_wave_1_store() -> None:
    kit.assert_store_conforms(lambda **kw: kit.MemoryStore(**kw), permutations=6)
    kit.assert_store_conforms(lambda **kw: kit.MemoryStore(adopt_key_ids=True, **kw),
                              permutations=3)
    store = kit.MemoryStore()
    assert isinstance(store, LedgerStore) and isinstance(store, LedgerStats)


def test_copilot_store_conformance() -> None:
    summary = kit.assert_store_copilot_conforms(lambda **kw: kit.MemoryStore(**kw))
    assert summary["cost_line_users"] == 3


class _NoAdoption(kit.MemoryStore):
    def _adoption(self, src):  # type: ignore[override]
        return False


class _Unfiltered(kit.MemoryStore):
    def count_users(self, *, since_ms, until_ms, where, source="requests"):  # type: ignore[override]
        return super().count_users(since_ms=since_ms, until_ms=until_ms, where={}, source=source)


class _FinalWins(kit.MemoryStore):
    @staticmethod
    def _choose(cands):  # type: ignore[override]
        best = max(cands, key=lambda c: (getattr(cands[c], "finality", "") == "final", c))
        return cands[best]


class _KeepsNames(kit.MemoryStore):
    """Keeps h_ cost-line names whatever their name key id (the wave-1 rule nulled only
    ``workspace_id``)."""

    def ingest(self, result, *, pricer=None):  # type: ignore[override]
        src_info = dataclasses.replace(result.source, name_key_id=None)
        own = self._meta["adopted_name_key_id"] or self._meta["name_key_id"] or None
        if result.source.name_key_id and own is not None:
            src_info = dataclasses.replace(result.source, name_key_id=own)
        result = dataclasses.replace(result, source=src_info)
        return super().ingest(result, pricer=pricer)


@pytest.mark.parametrize("broken,message", [
    (_NoAdoption, "adopts"), (_Unfiltered, "count_users"), (_FinalWins, "latest-fetch"),
    (_KeepsNames, "h_ cost-line names"),
])
def test_copilot_store_conformance_catches(broken, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_store_copilot_conforms(lambda **kw: broken(**kw))


def test_hashed_cost_line_names_follow_the_name_key() -> None:
    """R-E21: ``h_`` values under a name key id that is neither the store's nor the adopted one
    are nulled — on cost lines too (``repo`` / ``workflow`` of GitHub rows, C-4)."""
    store = kit.MemoryStore(org_key=K_ORG, name_key_id=key_id(K_ORG), adopt_key_ids=True)
    def lines(source, key):
        line, _ = make_ai_usage_row(principal=pseudonym(K_ORG, "p", "a"), credits="2",
                                    repo=pseudonym(key, "h", "repo"))
        line = dataclasses.replace(line, workflow=pseudonym(key, "h", "wf"))
        return IngestResult(source=source, requests=[], sessions=[], events=[], aggregates=[],
                            cost_lines=[line], outcomes=[], quarantined=[], notes=[], stats={},
                            capabilities=frozenset())
    own = store.ingest(lines(src("own", "github-ai-usage", K_ORG, K_ORG), K_ORG))
    assert own["names_nulled"] == 0
    foreign = lines(src("foreign", "github-ai-usage", K_ORG, C), C)
    counts = store.ingest(foreign)
    assert counts["names_nulled"] == 2 and counts["dq.name_key_mismatch"] == 2
    assert counts[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 0  # the principal key matches
    by_repo = {(c.repo, c.workflow) for c in store.cost_lines(**W)}
    assert (None, None) in by_repo
    assert (pseudonym(K_ORG, "h", "repo"), pseudonym(K_ORG, "h", "wf")) in by_repo
    # an adopted bundle's names are kept
    bundle = batch(src("bundle", "copilot-export", A, A), [pseudonym(A, "p", "u1")])
    bundle.cost_lines = [dataclasses.replace(bundle.cost_lines[0],
                                             repo=pseudonym(A, "h", "repo"))]
    assert store.ingest(bundle)["names_nulled"] == 0
    assert pseudonym(A, "h", "repo") in {c.repo for c in store.cost_lines(**W)}
    # re-ingesting a source is skipped with every dq count present
    again = store.ingest(foreign)
    assert again["skipped"] == 1 and again[kit.DQ_PRINCIPAL_KEY_MISMATCH] == 0


def test_count_users_over_cost_lines() -> None:
    store = kit.MemoryStore(org_key=K_ORG)
    people = [pseudonym(K_ORG, "p", u) for u in ("a", "b", "c")]
    store.ingest(batch(src("own", "github-ai-usage", K_ORG), people))
    extra = make_ai_usage_row(principal=people[0], team="t2", cost_center="cc9", credits="3",
                              date_utc="2026-09-11")[0]
    direct = make_ai_usage_row(unattributed=True, credits="4", date_utc="2026-09-11")[0]
    store.ingest(IngestResult(source=src("own2", "github-ai-usage", K_ORG), requests=[],
                              sessions=[], events=[], aggregates=[], cost_lines=[extra, direct],
                              outcomes=[], quarantined=[], notes=[], stats={},
                              capabilities=frozenset()))
    cnt = store.count_users
    assert cnt(where={}, source="cost_lines", **W) == 3
    assert cnt(where={"team": "t2"}, source="cost_lines", **W) == 1
    assert cnt(where={"cost_center": "cc9", "channel": "github_copilot"}, source="cost_lines",
               **W) == 1
    assert cnt(where={"team": ""}, source="cost_lines", **W) == 3  # no team on the report rows
    assert cnt(where={"cost_type": "ai_credit.direct"}, source="cost_lines", **W) == 0
    assert cnt(where={}, source="cost_lines", since_ms=kit._date_start_ms("2026-09-11"),
               until_ms=2**53) == 1
    assert cnt(where={}, **W) == 3  # requests (default source) unchanged
    with pytest.raises(PrivacyError):
        cnt(where={"principal": people[0]}, source="cost_lines", **W)
    with pytest.raises(UsageError):
        cnt(where={"lane_kind": "main"}, source="cost_lines", **W)
    with pytest.raises(UsageError):
        cnt(where={}, source="licenses", **W)


def test_source_stats_sum_integer_stats() -> None:
    store = kit.MemoryStore(org_key=K_ORG)
    one = batch(src("s1", "github-ai-usage", K_ORG), [pseudonym(K_ORG, "p", "a")])
    one.stats = {"records": 3, "rounding_remainders": 2, "flag": True}  # type: ignore[dict-item]
    two = batch(src("s2", "copilot-cli", None), ["r_x"], lines=False)
    two.stats = {"records": 5}
    store.ingest(one)
    store.ingest(two)
    store.ingest(one)  # skipped: counted once
    assert store.source_stats() == {"records": 8, "rounding_remainders": 2}
    assert store.source_stats(adapter="copilot-cli") == {"records": 5}
    assert store.source_stats(adapter="nothing") == {}


def test_pool_totals_and_clusters() -> None:
    pricer = kit.FakePricer()
    store = kit.MemoryStore(org_key=K_ORG, pricer=pricer)
    copilot = batch(src("cp", "github-ai-usage", K_ORG), [pseudonym(K_ORG, "p", u)
                                                          for u in ("a", "b")])
    sub = make_request("L-sub", 0, DAY_MS, {"uncached_input": 1000}, "claude-sonnet-5",
                       billing_path="subscription", request_id="rq-sub",
                       attribution={"principal": "r_s", "team": "t",
                                    "billing_path": "subscription",
                                    "extra": (("gateway", "gw-2"),)})
    copilot.requests.append(sub)
    store.ingest(copilot)
    total = store.aggregate(group_by=[], **W).rows[0].priced
    assert total.pool is not None and total.pool.nano == 2_000_000  # 2 × 100 × $10/MTok
    allow = pricer.price_inference(sub.serving_inference, ts_ms=DAY_MS).figure.nano
    assert allow and total.allowance is not None and total.allowance.nano == allow
    days = store.cluster_days(cluster_kind="team", since="2026-09-10", until="2026-09-11")
    assert [(d.pool_nano, d.allowance_nano, d.exact_nano) for d in days] == [
        (2_000_000, allow, 0)]
    gw = store.cluster_days(cluster_kind="gateway", since="2026-09-10", until="2026-09-11")
    assert [(d.cluster_id, d.requests) for d in gw] == [("gw-2", 1)]
    with pytest.raises(UsageError):
        store.cluster_days(cluster_kind="planet", since="2026-09-10", until="2026-09-11")


def test_latest_fetch_wins_for_copilot_records_only() -> None:
    line, agg = make_ai_usage_row(principal=pseudonym(K_ORG, "p", "a"), fetched_ms=1)
    newer = dataclasses.replace(line, amount_nano=7, finality="provisional", fetched_ms=5)
    newer_agg = dataclasses.replace(agg, reported_cost_nano=7, finality="provisional",
                                    fetched_ms=5)
    for order in itertools.permutations([(line, agg), (newer, newer_agg)]):
        store = kit.MemoryStore(org_key=K_ORG)
        for i, (cl, ag) in enumerate(order):
            store.ingest(IngestResult(source=src(f"s{i}", "github-ai-usage", K_ORG),
                                      requests=[], sessions=[], events=[], aggregates=[ag],
                                      cost_lines=[cl], outcomes=[], quarantined=[], notes=[],
                                      stats={}, capabilities=frozenset()))
        assert [c.amount_nano for c in store.cost_lines(**W)] == [7]
        assert [a.reported_cost_nano for a in store.aggregates(**W)] == [7]
    # non-Copilot records keep "final, then latest" (test_memory_store pins it too)
    base = kit._cost_line()
    final = dataclasses.replace(base, finality="final", fetched_ms=1, amount_nano=1)
    later = dataclasses.replace(base, fetched_ms=9, amount_nano=2)
    assert kit.MemoryStore._choose({"a": final, "b": later}) is final
    assert kit._latest_fetch_wins(line) and not kit._latest_fetch_wins(base)


def test_sources_mask_bits_include_copilot_sources() -> None:
    bits = kit.SOURCES_MASK_BITS
    assert (bits["copilot-cli"], bits["copilot-otel"], bits["copilot-vscode-traces"],
            bits["gh-aw-token-usage"], bits["copilot-export"]) == (256, 512, 1024, 2048, 4096)
    assert len(set(bits.values())) == len(bits)
    assert all(v & (v - 1) == 0 for v in bits.values())  # one bit each
