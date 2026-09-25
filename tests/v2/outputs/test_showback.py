"""Team showback (SPEC §14.5): published aggregates only, anchors with sources, billing paths."""

from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.kanon import publish
from tokenbill.core.labels import Basis, exact
from tokenbill.core.records import UsageBuckets
from tokenbill.core.testing import published_for_tests
from tokenbill.core.types import AggRow, ClusterDay, PricedTotal, RawAggregate
from tokenbill.outputs.result_json import rule_violations
from tokenbill.outputs.showback import render_showback

from .sample import T0, T1, agg_row, findings, plan

PSEUDONYM_RE = re.compile(r"\b[pcr]_[0-9a-f]{20}\b")
URL_RE = re.compile(r"(?i)\bhttps?://")


def teams_agg():
    rows = (agg_row((("team", "payments"), ("model", "claude-opus-5-5")), 9, "1200",
                    allowance="80"),
            agg_row((("team", "payments"), ("model", "claude-sonnet-5")), 7, "300"),
            agg_row((("team", "search"), ("model", "claude-opus-5-5")), 6, "900", pool="15"),
            agg_row((("team", "ops"), ("model", "claude-opus-5-5")), 5, "20"),
            agg_row((("team", "tiny"), ("model", "claude-opus-5-5")), 2, "40"),
            agg_row((("team", None), ("model", "claude-opus-5-5")), 0, "5"))
    return publish(RawAggregate(group_by=("team", "model"), rows=rows, window=(T0, T1)), k=5)


def path_row(path: str, team: str, users: int, nano: int, requests: int) -> AggRow:
    fig = exact(nano, Basis.LIST_EQUIVALENT if path in ("subscription", "copilot_pool")
                else Basis.LIST)
    priced = PricedTotal(
        exact=exact(0, Basis.LIST) if fig.basis is Basis.LIST_EQUIVALENT else fig,
        estimated=None, allowance=fig if path == "subscription" else None,
        priced_inferences=1, unpriced_inferences=0, unpriced_tokens=0, coverage="1",
        pool=fig if path == "copilot_pool" else None)
    return AggRow(dims=(("billing_path", path), ("team", team)), n_users=users,
                  n_requests=requests, usage=UsageBuckets(uncached_input=10), priced=priced)


def paths_agg():
    rows = (path_row("subscription", "payments", 10, 3_000 * 10**9, 900),
            path_row("subscription", "search", 5, 500 * 10**9, 100),
            path_row("usage_credits", "payments", 6, 400 * 10**9, 250),
            path_row("api_key", "tiny", 2, 90 * 10**9, 30),
            path_row("copilot_pool", "search", 5, 60 * 10**9, 40))
    return published_for_tests(RawAggregate(group_by=("billing_path", "team"), rows=rows,
                                            window=(T0, T1)))


def days():
    return [ClusterDay("2026-09-01", "team", "payments", None, None, 9, 50, 600 * 10**9, 0),
            ClusterDay("2026-09-02", "team", "payments", None, None, 6, 40, 600 * 10**9, 0),
            ClusterDay("2026-09-01", "team", "search", None, None, 3, 10, 90 * 10**9, 0),
            ClusterDay("2026-09-01", "workspace", "payments", None, None, 99, 1, 1, 0)]


def render(tmp_path: Path, **kw) -> list[Path]:
    args = dict(formats=("html", "csv", "json"), billing_paths=paths_agg())
    args.update(kw)
    return render_showback(teams_agg(), days(), findings(), plan(), tmp_path, **args)


def test_writes_every_format(tmp_path: Path) -> None:
    written = render(tmp_path)
    names = sorted(p.name for p in written)
    assert "index.html" in names and "showback.csv" in names and "showback.json" in names
    assert "showback-billing-paths.csv" in names
    assert len([n for n in names if n.startswith("team-")]) == 4   # payments, search, other, none
    assert written == sorted(written)


def test_html_pages_are_safe_and_show_anchors_with_sources(tmp_path: Path) -> None:
    for page in (p for p in render(tmp_path) if p.suffix == ".html"):
        text = page.read_text(encoding="utf-8")
        assert "Content-Security-Policy" in text and "<script" not in text.lower()
        assert not URL_RE.search(text)
        assert not PSEUDONYM_RE.search(text)
        assert "code.claude.com/docs/en/costs" in text          # anchor source, no scheme
        assert "$13" in text and "$30" in text and "84.0%" in text and "94.0%" in text
        assert "2026-09-23" in text                              # verification date
        for m in re.finditer(r"</svg>", text):
            assert text[m.end():m.end() + 16].startswith("<table><caption>")


def test_team_page_contents(tmp_path: Path) -> None:
    render(tmp_path)
    (payments,) = [p for p in tmp_path.glob("team-payments-*.html")]
    text = payments.read_text(encoding="utf-8")
    assert "$1,500.00 exact·list" in text                         # both model cells summed
    assert "$80.00 allowance·list-equivalent (not billed)" in text
    assert "Cost per active developer-day: <span" in text and "$80.00 exact·list" in text
    assert "TTL expiry re-writes on payments main lanes" in text
    assert "cc.prompt_cache_ttl.main" in text                      # related lever
    assert "Cache-read share: 94.7%" in text
    (other,) = [p for p in tmp_path.glob("team-other-teams-*.html")]
    assert "(other teams)" in other.read_text(encoding="utf-8")
    (unattributed,) = [p for p in tmp_path.glob("team-unattributed-*.html")]
    assert "users unknown" in unattributed.read_text(encoding="utf-8")


def test_billing_path_distribution_only_for_groups_of_k(tmp_path: Path) -> None:
    render(tmp_path)
    rows = list(csv.DictReader(io.StringIO(
        (tmp_path / "showback-billing-paths.csv").read_text(encoding="utf-8"))))
    by_path = {r["billing_path"]: r for r in rows}
    assert set(by_path) == {"subscription", "usage_credits", "copilot_pool"}   # api_key: 2 users
    sub = by_path["subscription"]
    assert sub["developers"] == "15" and sub["cells"] == "2"
    assert sub["mean_label"] == "exact·list_equivalent"
    assert sub["p50_usd"] and sub["p90_usd"]
    assert Decimal(sub["overage_share"]) == Decimal("250") / Decimal("1250")
    assert by_path["usage_credits"]["p50_usd"] == ""        # one cell: no distribution
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Spend per active developer-month by billing path" in index


def test_json_follows_the_schema_rule(tmp_path: Path) -> None:
    render(tmp_path)
    doc = json.loads((tmp_path / "showback.json").read_text(encoding="utf-8"))
    assert rule_violations(doc) == []
    assert Decimal("0.99") < Decimal(doc["allocation_coverage"]) < 1   # the unattributed $5
    names = [t["team"] for t in doc["teams"]]
    assert names == ["payments", "search", "(other teams)", "(unattributed)"]
    unknown = doc["teams"][-1]
    assert unknown["users"] is None and unknown["notes"] == ["users_unknown"]
    assert {a["checked_on"] for a in doc["anchors"]} == {"2026-09-23"}


def test_csv_has_no_individuals(tmp_path: Path) -> None:
    render(tmp_path)
    text = (tmp_path / "showback.csv").read_text(encoding="utf-8")
    assert not PSEUDONYM_RE.search(text)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["team"] for r in rows][:2] == ["payments", "search"]
    assert rows[0]["exact_usd"] == "1500" and rows[0]["exact_label"] == "exact·list"


def test_refuses_raw_aggregates_and_person_groupings(tmp_path: Path) -> None:
    raw = RawAggregate(group_by=("team",), rows=(), window=(T0, T1))
    with pytest.raises(ContractViolation):
        render_showback(raw, [], [], None, tmp_path)  # type: ignore[arg-type]
    person = published_for_tests(RawAggregate(group_by=("team", "api_key_id"), rows=(),
                                              window=(T0, T1)))
    with pytest.raises(PrivacyError):
        render_showback(person, [], [], None, tmp_path)
    no_team = published_for_tests(RawAggregate(group_by=("model",), rows=(), window=(T0, T1)))
    with pytest.raises(UsageError):
        render_showback(no_team, [], [], None, tmp_path)
    with pytest.raises(UsageError):
        render_showback(teams_agg(), [], [], None, tmp_path, billing_paths=teams_agg())
    with pytest.raises(UsageError):
        render_showback(teams_agg(), [], [], None, tmp_path, formats=("pdf",))
    with pytest.raises(UsageError):
        render_showback(teams_agg(), [], [], None, tmp_path, formats=())
    with pytest.raises(ContractViolation):
        render_showback(teams_agg(), ["x"], [], None, tmp_path)  # type: ignore[list-item]
    with pytest.raises(ContractViolation):
        render_showback(teams_agg(), [], ["x"], None, tmp_path)  # type: ignore[list-item]


def test_html_only_default_and_deterministic(tmp_path: Path) -> None:
    a = render_showback(teams_agg(), days(), findings(), None, tmp_path / "a")
    b = render_showback(teams_agg(), days(), findings(), None, tmp_path / "b")
    assert all(p.suffix == ".html" for p in a)
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]
