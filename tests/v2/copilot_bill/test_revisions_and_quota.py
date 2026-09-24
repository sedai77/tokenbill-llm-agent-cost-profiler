"""Revision safety (overlapping exports, in-file duplicates) and plan-quota evidence, incl. the
UI-download vs export-API equality (addendum §5.1 rules 7–8, CP-BILL brief build 4)."""

from __future__ import annotations

import json
from pathlib import Path

from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.ids import pseudonym
from tokenbill.core.pool import build_cells, detect_plans
from tokenbill.core.records import record_key, to_json
from tokenbill.core.testing import MemoryStore

from .helpers import (
    ADAPTERS,
    ENTRIES,
    FIXTURES,
    PRINCIPAL_KEY,
    coverage_aggs,
    dump,
    entry_opts,
    ms,
    notes,
    read,
    token_aggs,
)

FLAG = frozenset({"copilot-report-quota"})


def _bob_0909(lines: list) -> list:
    bob = pseudonym(PRINCIPAL_KEY, "p", "octo-bob")
    return [c for c in lines if c.principal == bob and c.date_utc == "2026-09-09"]


def test_overlapping_exports_keep_each_natural_id_once_latest_wins() -> None:
    a, b = read("ai_usage_overlap_a.csv"), read("ai_usage_overlap_b.csv")
    (old,), (new,) = _bob_0909(a.cost_lines), _bob_0909(b.cost_lines)
    assert old.line_id == new.line_id and old.amount_nano == 0 < new.amount_nano
    assert b.cost_lines[0].fetched_ms > a.cost_lines[0].fetched_ms
    shared = {c.line_id for c in a.cost_lines} & {c.line_id for c in b.cost_lines}
    assert len(shared) == 6                                  # 3 overlapping days x 2 users
    for order in ((a, b), (b, a)):
        store = MemoryStore(org_key=PRINCIPAL_KEY)
        for result in order:
            store.ingest(result)
        lines = store.cost_lines("github.ai_usage_report")
        assert len(lines) == len({c.line_id for c in lines}) == 34
        (kept,) = _bob_0909(lines)
        assert kept == new                                   # latest fetched_ms wins
        aggs = store.aggregates("github.ai_usage_report")
        assert len(aggs) == 17                               # one team cell per day
        cell = next(g for g in aggs if g.bucket_start_ms == ms(old.date_utc))
        assert cell.fetched_ms == new.fetched_ms
    exp = ENTRIES["ai_usage_overlap_b.csv"]["expect"]
    assert sum(c.amount_nano for c in b.cost_lines) == exp["net_nano"]


def test_in_file_duplicates_are_summed_with_the_dq_code() -> None:
    r = read("ai_usage_duplicates.csv")
    exp = ENTRIES["ai_usage_duplicates.csv"]["expect"]
    assert len(r.cost_lines) == exp["cost_lines"] == 3
    assert notes(r)["dq.copilot_duplicate_key_summed"] == 1 == r.stats["duplicate_keys"]
    sonnet = next(c for c in r.cost_lines if c.model == "claude-sonnet-5")
    assert sonnet.list_amount_nano == 60_000_000 + 30_000_000     # both rows' gross
    assert sonnet.quantity == "9"                                 # 6 + 3 credits, exact
    assert sum(c.amount_nano for c in r.cost_lines) == exp["net_nano"]
    assert sum(c.list_amount_nano for c in r.cost_lines) == exp["gross_nano"]
    assert len(token_aggs(r)) == 3
    assert {c.routing for c in r.cost_lines if c.model == "claude-haiku-4-5"} == {"auto",
                                                                                   "direct"}


def test_plan_quota_with_the_flag() -> None:
    r = read("ai_usage_quota_api.csv", experimental=FLAG)
    (snap,) = r.config
    assert (snap.kind, snap.source_kind, snap.entity_id) == ("plan_quota",
                                                             "github.ai_usage_report", "org:acme-a")
    assert dict(snap.attrs) == {"month": "2026-09", "quota": "3900", "n_users": 60}
    assert "p_" not in json.dumps(to_json(snap)) and r.stats["quota_unknown"] == 5
    assert "config" in r.capabilities and "dq.copilot_report_quota_ignored" not in notes(r)
    assert record_key(snap).startswith("plan_quota")
    # F-POOL reads the rows (Appendix C.P15 variant: the 5 users without a quota stay unknown)
    (evidence,) = detect_plans(r.cost_lines, [], r.config, month="2026-09")
    assert (evidence.plan, evidence.source) == ("unknown", "report_quota")
    assert dict(evidence.seats) == {"enterprise": 60, "unknown": 5}


def test_plan_quota_without_the_flag() -> None:
    r = read("ai_usage_quota_api.csv")
    assert r.config == [] and "config" not in r.capabilities
    assert notes(r)["dq.copilot_report_quota_ignored"] == 60
    (evidence,) = detect_plans(r.cost_lines, [], r.config, month="2026-09")
    assert evidence.plan == "unknown" and evidence.source == "none"


def _without_source(result) -> dict:  # noqa: ANN001
    """Records of a result minus source ids (coverage aggregates carry the source)."""
    cov = [dict(to_json(a), agg_id=None, dims=[["channel", "github_copilot"]])
           for a in coverage_aggs(result)]
    return {"cost_lines": [to_json(c) for c in result.cost_lines],
            "aggregates": [to_json(a) for a in token_aggs(result)], "coverage": cov,
            "config": [to_json(c) for c in result.config], "notes": result.notes,
            "quarantined": result.quarantined}


def test_ui_download_equals_export_api_file(tmp_path: Path) -> None:
    api = read("ai_usage_quota_api.csv", experimental=FLAG)
    ui = read("ai_usage_quota_ui.csv", experimental=FLAG)
    # the UI download has CRLF line ends; a git checkout may have normalized them, so re-create
    raw = (FIXTURES / "ai_usage_quota_ui.csv").read_bytes().replace(b"\r\n", b"\n")
    assert raw.startswith(b"\xef\xbb\xbf") and b'"9/1/26"' in raw
    crlf = tmp_path / "ai_usage_quota_ui.csv"
    crlf.write_bytes(raw.replace(b"\n", b"\r\n"))
    ui_crlf = ADAPTERS["github-ai-usage"].read(crlf, entry_opts("ai_usage_quota_ui.csv",
                                                                experimental=FLAG))
    assert api.source.source_id != ui.source.source_id
    assert _without_source(api) == _without_source(ui) == _without_source(ui_crlf)
    assert len(api.cost_lines) == ENTRIES["ai_usage_quota_ui.csv"]["expect"]["cost_lines"]
    for r in (api, ui):
        assert CANARY_LOGIN not in dump(r) and "dev000" not in dump(r)


def test_core_pool_cells_join_lines_and_token_aggregates() -> None:
    r = read("ai_usage_2026-09.csv")
    exp = ENTRIES["ai_usage_2026-09.csv"]["expect"]
    for convention in ("excl", "incl"):
        cells, _ = build_cells(r.aggregates, r.cost_lines, grain="day", convention=convention)
        assert not [c for c in cells if c.cost_type == "other"]   # every token cell has its line
        assert sum(c.gross_nano for c in cells) == exp["gross_nano"]
        assert sum(c.net_nano for c in cells) == exp["net_nano"]
    cells, _ = build_cells(r.aggregates, r.cost_lines, grain="month")
    assert sum(c.usage.total_input + c.usage.output for c in cells) == exp["tokens"]
    assert {c.cost_type for c in cells} == {"ai_credit.user", "ai_credit.direct"}
