"""Property and fuzz tests of CP-STORE (SPEC §21 #5): the SQLite record store agrees with the
reference fake (``core.testing.MemoryRecordStore``) on random batches and orders; hostile record,
``where``, window and enricher inputs let only ``TokenbillError`` escape; enricher invariants
(scenario pairs, estimates kept out of report sums, input-order independence)."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.copilot.enrich import day_conventions, decided_cells, enrich_context
from tokenbill.copilot.record_store import CopilotRecordStore
from tokenbill.core import builders as b
from tokenbill.core import testing as kit
from tokenbill.core.ids import key_id
from tokenbill.core.records import (
    ACTIVITY_FLAGS,
    ACTIVITY_KEYS,
    EDITOR_FAMILIES,
    LICENSE_BUCKETS,
    LICENSE_PLANS,
    LICENSE_SOURCE_KINDS,
)
from tokenbill.core.types import AnalysisContext

from .support import ORG_KEY, W, activity_seats, ledger_meta, month_window, ms, p, result, rows

SETTINGS = settings(max_examples=40, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
KID = key_id(ORG_KEY)
PEOPLE = [p(ORG_KEY, f"u{i}") for i in range(4)]
DATES = ["2026-09-01", "2026-09-15", "2026-09-30"]
TEXT = st.sampled_from([None, "a", "b", "", "t\x1fx", "back\\slash", "ü-team", "(other)"])

licenses = st.builds(
    b.make_license, st.sampled_from(PEOPLE), snapshot_date=st.sampled_from(DATES),
    plan=st.sampled_from(LICENSE_PLANS), team=TEXT, cost_center=TEXT,
    org=st.sampled_from([None, "org-a", "org-b"]), seat_created=TEXT,
    pending_cancellation=TEXT, last_activity_bucket=st.sampled_from(LICENSE_BUCKETS),
    last_activity_surface=st.sampled_from([None, *EDITOR_FAMILIES]),
    last_authenticated_bucket=st.sampled_from([*LICENSE_BUCKETS, "unknown"]),
    assigned_via_team=st.sampled_from([None, True, False]),
    fetched_ms=st.integers(0, 3), source_kind=st.sampled_from(LICENSE_SOURCE_KINDS))
activity = st.builds(
    b.make_activity, st.sampled_from(PEOPLE), date_utc=st.sampled_from(DATES), team=TEXT,
    cost_center=TEXT,
    reported_cost_nano=st.one_of(st.none(), st.integers(-(2**63), 2**63 - 1)),
    counts=st.dictionaries(st.sampled_from([*ACTIVITY_KEYS[:5], "ide:vscode", "model:x.y"]),
                           st.integers(0, 2**53), max_size=4),
    flags=st.sets(st.sampled_from(ACTIVITY_FLAGS), max_size=3), fetched_ms=st.integers(0, 3))
attr_values = st.one_of(st.none(), st.booleans(), st.integers(-(2**63 - 1), 2**63 - 1),
                        st.text(max_size=8))
configs = st.one_of(
    st.builds(lambda a, t, f: b.make_config("run_flags", a, snapshot_ms=t, fetched_ms=f),
              st.dictionaries(st.sampled_from(["promo_eligible", "compliance", "plan.enterprise",
                                               "billing_mode.enterprise"]), attr_values,
                              max_size=3),
              st.sampled_from([ms(d) for d in DATES]), st.integers(0, 3)),
    st.builds(lambda team, n, t: b.make_config("seat_counts", {"team": team, "bucket": "*",
                                                               "n_people": n},
                                               entity_id="org:o", snapshot_ms=t),
              st.sampled_from(["a", "b"]), st.integers(0, 20),
              st.sampled_from([ms(d) for d in DATES])))


def fresh_store(tmp: str, name: str) -> CopilotRecordStore:
    path = Path(tmp) / name
    ledger_meta(path, org_key_id=KID)
    return CopilotRecordStore(path)


def dump(store) -> tuple:
    return (store.licenses(**W), store.activity(**W), store.config(**W))


@SETTINGS
@given(batches=st.lists(st.tuples(st.lists(licenses, max_size=4), st.lists(activity, max_size=3),
                                  st.lists(configs, max_size=2)), min_size=1, max_size=4),
       seed=st.integers(0, 2**16))
def test_sqlite_store_agrees_with_the_fake_in_any_order(batches, seed) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_store = fresh_store(tmp, "a.db")
        shuffled = fresh_store(tmp, "b.db")
        fake = kit.MemoryRecordStore(org_key_id=KID)
        for lics, acts, confs in batches:
            batch = result(lics, acts, confs)
            try:
                got = sqlite_store.put(batch, principal_key_id=KID)
            except TokenbillError:
                continue            # a value SQLite cannot hold: nothing of the batch stored
            assert got == fake.put(batch, principal_key_id=KID)
        order = list(range(len(batches)))
        random.Random(seed).shuffle(order)
        for i in order:
            try:
                shuffled.put(result(*batches[i]), principal_key_id=KID)
            except TokenbillError:
                continue
        assert dump(sqlite_store) == dump(shuffled)
        lics, acts, confs = dump(sqlite_store)
        f_lics, f_acts, f_confs = dump(fake)
        assert (sorted(map(repr, lics)), sorted(map(repr, acts)), sorted(map(repr, confs))) == (
            sorted(map(repr, f_lics)), sorted(map(repr, f_acts)), sorted(map(repr, f_confs)))
        for where in ({}, {"team": "a"}, {"org": ""}, {"plan": "business"},
                      {"bucket": "none_90d", "org": "org-a"}, {"surface": "jetbrains"},
                      {"date_from": DATES[1]}):
            for source in ("licenses", "activity"):
                try:
                    want = fake.count_users(where=where, source=source, **W)
                except TokenbillError:
                    continue
                assert sqlite_store.count_users(where=where, source=source, **W) == want


where_values = st.one_of(st.text(max_size=12), st.integers(), st.none(), st.just(""))


@SETTINGS
@given(where=st.dictionaries(st.sampled_from(["team", "org", "plan", "bucket", "product",
                                              "surface", "editor_family", "date_from",
                                              "date_to", "principal", "session", "junk",
                                              "cost_center"]), where_values, max_size=4),
       since=st.integers(-(2**63), 2**63), until=st.integers(-(2**63), 2**63),
       source=st.sampled_from(["licenses", "activity", "cost_lines", ""]))
def test_count_users_fuzz_only_tokenbill_errors(where, since, until, source) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh_store(tmp, "f.db")
        store.put(result([b.make_license(PEOPLE[0], snapshot_date=DATES[0])]),
                  principal_key_id=KID)
        try:
            n = store.count_users(where=where, source=source, since_ms=since, until_ms=until)
        except TokenbillError:
            return
        assert isinstance(n, int) and n >= 0


@SETTINGS
@given(window=st.dictionaries(st.sampled_from(["since_ms", "until_ms", "since"]),
                              st.one_of(st.integers(-(2**70), 2**70), st.text(max_size=3),
                                        st.none()), max_size=2),
       purge_before=st.one_of(st.none(), st.integers(-(2**70), 2**70)),
       principal=st.one_of(st.none(), st.sampled_from(PEOPLE), st.text(max_size=24)))
def test_windows_retention_and_purge_fuzz(window, purge_before, principal) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh_store(tmp, "w.db")
        store.put(result([b.make_license(x, snapshot_date=DATES[1]) for x in PEOPLE],
                         [b.make_activity(PEOPLE[0], date_utc=DATES[2])],
                         [b.make_config(snapshot_ms=ms(DATES[0]))]), principal_key_id=KID)
        for call in (store.licenses, store.activity, store.config):
            try:
                assert isinstance(call(**window), list)
            except TokenbillError:
                pass
        try:
            store.retain(identity_before_ms=purge_before)  # type: ignore[arg-type]
        except TokenbillError:
            pass
        try:
            removed = store.purge(principal=principal, before_ms=purge_before, actor="fuzz")
        except TokenbillError:
            return
        assert removed >= 0


@SETTINGS
@given(today=st.one_of(st.text(max_size=12), st.sampled_from(["2026-09-23", "2026-13-01",
                                                               "0000-01-01", "9999-12-31"])),
       decisions=st.lists(st.tuples(st.sampled_from(["convention:s_a", "gross_is_list:"
                                                     "enterprise:2026-09", "plan_fit:x", "zz"]),
                                    st.sampled_from(["excl", "incl", "undecidable", "true",
                                                     "unknown", ""])), max_size=3),
       start=st.integers(-(2**60), 2**60), span=st.integers(-5, 2**45),
       mode=st.sampled_from(["enterprise", "org", "team"]))
def test_enricher_fuzz_only_tokenbill_errors(today, decisions, start, span, mode) -> None:
    lines, aggs = rows(1_000, date="2026-09-10", input_tokens=100, cache_read=50)
    ledger = kit.MemoryStore(org_key=ORG_KEY)
    ledger.ingest(result(cost_lines=lines, aggregates=aggs, adapter="github-ai-usage"))
    records = kit.MemoryRecordStore(ledger)
    ctx = AnalysisContext(pricer=kit.FakePricer(), rules=None, replayer=None,  # type: ignore
                          calibration=None, window=(start, start + span),
                          capabilities=frozenset())
    try:
        out = enrich_context(ledger, [records], ctx, today=today,
                             reconciled_channels=frozenset(), recon_decisions=decisions,
                             entity_mode=mode)
    except TokenbillError:
        return
    assert isinstance(out, AnalysisContext)


@SETTINGS
@given(n_seats=st.integers(0, 30), n_users=st.integers(1, 6), known=st.sampled_from(
    [None, "business", "enterprise"]), credits=st.integers(0, 300_000),
       est=st.integers(0, 5 * 10**7), seed=st.integers(0, 2**16))
def test_enricher_invariants(n_seats, n_users, known, credits, est, seed) -> None:
    users = [b.make_principal(f"u{i}") for i in range(n_users)]
    usage, aggs = rows(credits, date="2026-10-10", users=users)
    if known is None:
        seats = activity_seats(n_seats, date="2026-10-12")
    else:
        seats = [b.make_license(x, snapshot_date="2026-10-12", plan=known)
                 for x in (b.make_principal(f"u{i}") for i in range(n_seats))]
    act = [b.make_activity(users[0], date_utc="2026-10-30", reported_cost_nano=est),
           b.make_activity(users[0], date_utc="2026-10-10", reported_cost_nano=est)]

    def build(order: int):
        rnd = random.Random(seed + order)
        ledger = kit.MemoryStore(org_key=ORG_KEY)
        ls, ag, st_ = list(usage), list(aggs), list(seats)
        for items in (ls, ag, st_):
            rnd.shuffle(items)
        ledger.ingest(result(cost_lines=ls, aggregates=ag, adapter="github-ai-usage"))
        records = kit.MemoryRecordStore(ledger)
        records.put(result(st_, act), principal_key_id=KID)
        ctx = AnalysisContext(pricer=PRICER, rules=None, replayer=None,  # type: ignore
                              calibration=None, window=month_window("2026-10"),
                              capabilities=frozenset())
        return enrich_context(ledger, [records], ctx, today="2026-10-31",
                              reconciled_channels=frozenset())

    out = build(0)
    assert out == build(1)                                  # input-order independent
    for pe in out.plans:
        pms = [pm for pm in out.pools if (pm.entity_id, pm.month) == (pe.entity_id, pe.month)]
        if dict(pe.seats).get("unknown"):
            assert sorted(pm.plan_scenario for pm in pms) == ["business", "enterprise"]
            a, b_ = pms
            assert a.consumed_report_nano == b_.consumed_report_nano   # never merged or chosen
        else:
            assert [pm.plan_scenario for pm in pms] == [None]
    for pm in out.pools:
        assert pm.consumed_report_nano == credits * 10**7          # the report only
        assert pm.consumed_estimate_nano == (est if est > 0 else 0)  # 10-30 (10-10 is reported)


PRICER = kit.FakePricer()


@SETTINGS
@given(days=st.lists(st.tuples(st.sampled_from(DATES), st.sampled_from(["s_a", "s_b", None]),
                               st.integers(0, 3)), max_size=6),
       decided=st.dictionaries(st.sampled_from(["s_a", "s_b"]),
                               st.sampled_from(["excl", "incl", "undecidable"]), max_size=2))
def test_convention_per_day_matches_build_cells_per_group(days, decided) -> None:
    from tokenbill.core.pool import build_cells

    lines, aggs = [], []
    for date in DATES:
        ls, ag = rows(100, date=date, input_tokens=1_000, cache_read=400, cache_write=100)
        lines += ls
        aggs += ag
    cov = [b.make_aggregate(None, source_kind="github.ai_usage_report.coverage",
                            agg_id=f"c{i}", bucket_start_ms=ms(d), bucket_end_ms=ms(d) + 1,
                            dims={"channel": "github_copilot", "source": s}, fetched_ms=f)
           for i, (d, s, f) in enumerate(days) if s is not None]
    decisions = tuple((f"convention:{k}", v) for k, v in sorted(decided.items()))
    conv, undecidable = day_conventions(aggs + cov, decisions)
    cells, notes = decided_cells(aggs + cov, lines, recon_decisions=decisions)
    for date in DATES:
        want = [c for c in build_cells(aggs, lines, convention=conv.get(date, "excl"))[0]
                if c.date_utc == date]
        assert [c for c in cells if c.date_utc == date] == want
    assert bool(notes) == bool(undecidable)
