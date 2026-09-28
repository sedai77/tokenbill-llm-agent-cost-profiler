"""AI usage, detailed / summarized usage CSVs and billing REST pages (addendum §5.1–§5.3), read
back with the local minimal readers."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from tokenbill.core.builders import CANARY, CANARY_LOGIN, make_ai_usage_row, make_config
from tokenbill.core.money import usd_str_to_nano
from tokenbill.synth import copilot_writers as w

from .helpers import read_csv, read_jsonl
from .world import World, build_world

AI_LINES = "github.ai_usage_report"


def _ai_lines(world: World, *, legacy: bool = False) -> list:
    return [c for c in world.records["cost_lines"] if c.source_kind == AI_LINES
            and (c.cost_type == "ai_credit.legacy_pru") == legacy]


def _nano(text: str) -> int:
    return usd_str_to_nano(str(Decimal(text)))[0]


def _team_money(rows: list[dict[str, str]], team_map: dict[str, str]) -> dict:
    out: dict = defaultdict(lambda: [0, 0])
    for r in rows:
        cell = out[team_map.get(r["username"]) if r["username"] else None]
        cell[0] += _nano(r["gross_amount"])
        cell[1] += _nano(r["net_amount"])
    return dict(out)


def test_ai_usage_header_rows_and_money(world: World, full: tuple[Path, dict[str, Path]],
                                        ident: w.Identity) -> None:
    _, files = full
    header, rows, bom = read_csv(files["billing/ai_usage_report_excl.csv"])
    assert bom
    assert tuple(header) == (*w.AI_USAGE_HEADER, "total_monthly_quota")
    lines = _ai_lines(world)
    assert len(rows) == len(lines)
    expected: dict = defaultdict(lambda: [0, 0])
    for c in lines:
        cell = expected[c.team]
        cell[0] += c.list_amount_nano
        cell[1] += c.amount_nano
    assert _team_money(rows, ident.team_map()) == dict(expected)
    for r in rows:   # gross - discount = net at nano precision (float tails included)
        assert abs(_nano(r["gross_amount"]) - _nano(r["discount_amount"])
                   - _nano(r["net_amount"])) <= 1


def test_ai_usage_tokens_per_team_under_each_convention(
        world: World, full: tuple[Path, dict[str, Path]], ident: w.Identity) -> None:
    _, files = full
    expected: dict = defaultdict(lambda: [0, 0, 0, 0])
    for a in world.records["aggregates"]:
        if a.source_kind == AI_LINES:
            cell = expected[dict(a.dims).get("team")]
            for i, v in enumerate((a.usage.uncached_input, a.usage.cache_read,
                                   a.usage.cache_write, a.usage.output)):
                cell[i] += v
    team_map = ident.team_map()
    for conv in ("excl", "incl", "undecidable"):
        _, rows, _ = read_csv(files[f"billing/ai_usage_report_{conv}.csv"])
        got: dict = defaultdict(lambda: [0, 0, 0, 0])
        for r in rows:
            if not r["input"]:
                continue
            cell = got[team_map.get(r["username"]) if r["username"] else None]
            inp, read, write, out = (int(r[k]) for k in ("input", "cache_read", "cache_write",
                                                          "output"))
            uncached = inp - read - write if conv == "incl" else inp
            for i, v in enumerate((uncached, read, write, out)):
                cell[i] += v
        want = {t: ([u, 0, 0, o] if conv == "undecidable" else [u, r, wr, o])
                for t, (u, r, wr, o) in expected.items()}
        assert dict(got) == want, conv


def test_ai_usage_quirks(world: World, full: tuple[Path, dict[str, Path]]) -> None:
    _, files = full
    ai = {rel: read_csv(p) for rel, p in files.items()
          if re.fullmatch(r"billing/ai_usage_(report|overlap)_\w+\.csv", rel)}
    slash = [rel for rel, (_, rows, _) in ai.items() if "/" in rows[0]["date"]]
    assert slash == ["billing/ai_usage_overlap_a.csv"]
    assert all(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", r["date"])
               for r in ai[slash[0]][1])
    _, rows, _ = ai["billing/ai_usage_report_excl.csv"]
    assert any(len(r["gross_amount"].split(".")[-1]) == 16 for r in rows)   # float tails
    labels = {r["model"] for r in rows}
    assert {"Auto: Claude Haiku 4.5", "Claude Opus 4.8 (fast mode)", "Code Review",
            "Claude Opus 4.8", "GPT-5.5", "Claude Sonnet 5"} <= labels
    assert all(r["username"] == "" for r in rows if r["model"] == "Code Review")
    assert any(r["username"] == CANARY_LOGIN for r in rows)
    assert {r["total_monthly_quota"] for r in rows if r["username"]} == {"Unknown"}
    assert any(r["quantity"] == "42.726213" for r in rows)
    assert CANARY not in files["billing/ai_usage_report_excl.csv"].read_text()


def test_ai_usage_without_quirks(world: World, tmp_path: Path, ident: w.Identity) -> None:
    files = w.write_ai_usage_csv(world, tmp_path, quirks=False, identity=ident)
    assert sorted(files) == ["billing/ai_usage_legacy_pru.csv",
                             "billing/ai_usage_report_excl.csv"]
    header, rows, bom = read_csv(files["billing/ai_usage_report_excl.csv"])
    assert not bom and tuple(header) == w.AI_USAGE_HEADER
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"]) for r in rows)
    assert all(len(r["gross_amount"].split(".")[-1]) <= 9 for r in rows)


def test_revision_pair(world: World, full: tuple[Path, dict[str, Path]]) -> None:
    root, files = full
    _, main, _ = read_csv(files["billing/ai_usage_report_excl.csv"])
    _, a, _ = read_csv(files["billing/ai_usage_overlap_a.csv"])
    _, b, _ = read_csv(files["billing/ai_usage_overlap_b.csv"])
    info = w.revision_info(world)
    assert info is not None and len(info["overlap"]) == 3
    iso = {r["date"] for r in main}

    def iso_of(d: str) -> str:
        m, day, y = d.split("/")
        return f"20{y}-{int(m):02d}-{int(day):02d}"

    a_dates = {iso_of(r["date"]) for r in a}
    b_dates = {r["date"] for r in b}
    assert a_dates | b_dates == iso and a_dates & b_dates == set(info["overlap"])
    key = ("username", "model", "organization", "repository", "cost_center_name")
    main_idx = {(r["date"], *(r[k] for k in key)): r for r in main}
    stale = [r for r in a if _nano(main_idx[(iso_of(r["date"]), *(r[k] for k in key))][
        "net_amount"]) != _nano(r["net_amount"])]
    assert len(stale) == 1 and iso_of(stale[0]["date"]) == info["date"]
    assert _nano(stale[0]["net_amount"]) == info["stale_net_nano"]
    assert all(_nano(main_idx[(r["date"], *(r[k] for k in key))]["net_amount"])
               == _nano(r["net_amount"]) for r in b)


def test_legacy_file(world: World, full: tuple[Path, dict[str, Path]]) -> None:
    _, files = full
    header, rows, _ = read_csv(files["billing/ai_usage_legacy_pru.csv"])
    assert tuple(header) == w.LEGACY_HEADER and "exceeds_quota" in header
    assert len(rows) == len(_ai_lines(world, legacy=True)) == 1
    assert rows[0]["sku"] == "copilot_premium_request"


def test_plan_quota_values(tmp_path: Path) -> None:
    world = build_world()
    world.records["config"].append(make_config("plan_quota", {"month": "2026-09",
                                                              "quota": "3900", "n_users": 4},
                                               entity_id="org:org-a"))
    files = w.write_ai_usage_csv(world, tmp_path, revision_pair=False)
    _, rows, _ = read_csv(files["billing/ai_usage_report_excl.csv"])
    ident = w.build_identity(world)
    plans = {ident.login(x.principal): x.plan for x in world.records["licenses"]}
    for r in rows:
        if r["username"]:
            assert r["total_monthly_quota"] == ("3900" if plans[r["username"]] == "enterprise"
                                                else "1900")
        else:
            assert r["total_monthly_quota"] == ""


def test_report_sets_render_each_convention_from_its_records(tmp_path: Path) -> None:
    line, agg = make_ai_usage_row(date_utc="2026-09-10", input_tokens=100, output_tokens=10,
                                  cache_read_tokens=50, cache_write_tokens=5, team="core")
    no_cache, agg2 = make_ai_usage_row(date_utc="2026-09-10", input_tokens=155, output_tokens=10,
                                       team="core")
    files = w.write_ai_usage_csv([line, agg], tmp_path, conventions=("excl", "undecidable"),
                                 report_sets={"undecidable": [no_cache, agg2]}, quirks=False)
    _, rows, _ = read_csv(files["billing/ai_usage_report_undecidable.csv"])
    assert [(r["input"], r["cache_read"], r["cache_write"]) for r in rows] == [("155", "0", "0")]


def test_orphan_token_aggregate_gets_a_zero_money_row(tmp_path: Path) -> None:
    _, agg = make_ai_usage_row(date_utc="2026-09-10", input_tokens=10, output_tokens=2,
                               team="core", principal="p_" + "1" * 20)
    line, _ = make_ai_usage_row(date_utc="2026-09-10", input_tokens=0, team="core",
                                principal="p_" + "1" * 20)
    files = w.write_ai_usage_csv([agg, line], tmp_path, quirks=False)
    _, rows, _ = read_csv(files["billing/ai_usage_report_excl.csv"])
    assert len(rows) == 1 and rows[0]["input"] == "10"   # the line matches the aggregate
    files = w.write_ai_usage_csv([agg], tmp_path / "x", quirks=False)
    _, rows, _ = read_csv(files["billing/ai_usage_report_excl.csv"])
    assert [(r["gross_amount"], r["input"], r["output"]) for r in rows] == [("0", "10", "2")]


def test_detailed_report(world: World, full: tuple[Path, dict[str, Path]],
                         ident: w.Identity) -> None:
    _, files = full
    header, rows, bom = read_csv(files["billing/detailed_usage_report.csv"])
    assert bom and tuple(header) == w.DETAILED_HEADER
    paths = {r["workflow_path"] for r in rows if r["product"] == "actions"}
    dynamic = {p for p in paths if p.startswith("dynamic/")}
    assert dynamic == {"dynamic/agents/copilot-pull-request-reviewer",
                       "dynamic/copilot-pull-request-reviewer/copilot-pull-request-reviewer",
                       "dynamic/copilot-swe-agent/copilot", "dynamic/github-code-quality/codeql"}
    assert any(p.endswith(".lock.yml") and p.startswith(".github/workflows/") for p in paths)
    assert w.USER_WORKFLOW_PATH in paths
    assert any(r["sku"] == "linux_16_core" for r in rows)
    assert any(r["product"] == "sandbox" for r in rows)
    metered = [c for c in world.records["cost_lines"] if c.source_kind == "github.metered_usage"]
    real = [r for r in rows if r["workflow_path"] != w.USER_WORKFLOW_PATH]
    assert len(real) == len(metered)
    want: dict = defaultdict(int)
    for c in metered:
        want[(c.channel, c.team)] += c.amount_nano
    got: dict = defaultdict(int)
    channel = {"copilot": "github_copilot", "actions": "github_actions",
               "sandbox": "github_sandbox"}
    team_map = ident.team_map()
    for r in real:
        got[(channel[r["product"]], team_map.get(r["username"]))] += _nano(r["net_amount"])
    assert dict(got) == dict(want)


def test_summarized_report_sums(world: World, full: tuple[Path, dict[str, Path]]) -> None:
    _, files = full
    header, rows, _ = read_csv(files["billing/summarized_usage_report.csv"])
    assert tuple(header) == w.SUMMARIZED_HEADER
    total = sum(_nano(r["net_amount"]) for r in rows)
    want = sum(c.amount_nano for c in world.records["cost_lines"]
               if c.source_kind in ("github.metered_usage", AI_LINES))
    assert total == want


def test_billing_pages_two_months(tmp_path: Path) -> None:
    rows = [make_ai_usage_row(date_utc=d, model=m, credits="12.5", discount_credits="2.5",
                              principal="p_" + "2" * 20, team="core")
            for d in ("2026-08-30", "2026-09-02") for m in ("Claude Sonnet 5", "GPT-5.5")]
    records = [x for pair in rows for x in pair]
    files = w.write_billing_pages(records, tmp_path)
    docs = read_jsonl(files["billing/billing_api.jsonl"])
    ai_pages = [d for d in docs if d["request"]["path"].endswith("/ai_credit/usage")]
    assert {item["sku"] for d in ai_pages for item in d["response"]["usageItems"]} == {
        "Copilot AI Credits", "AI Credit"}
    assert {item["unitType"] for d in ai_pages for item in d["response"]["usageItems"]} == {
        "credits", "ai-credits"}
    net = sum(Decimal(str(item["netAmount"])) for d in ai_pages
              for item in d["response"]["usageItems"])
    assert net == Decimal("0.4")
    summary = [d for d in docs if d["request"]["path"].endswith("/usage/summary")]
    assert len(summary) == 2 and all(d["response"]["timePeriod"]["year"] == 2026
                                     for d in summary)
    assert w.write_billing_pages([], tmp_path / "none") == {}
