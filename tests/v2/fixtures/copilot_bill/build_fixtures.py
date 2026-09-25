"""Build the CP-BILL fixtures and MANIFEST.json (deterministic; imports nothing from tokenbill).

Run: ``python tests/v2/fixtures/copilot_bill/build_fixtures.py``. Shapes follow the documents named
in each MANIFEST entry (``schema_source``); every value is synthetic. Money is computed with
``Decimal`` from the documented Copilot list rates (addendum §19.2), so the expectations below are
closed-form and independent of the adapters.
"""

from __future__ import annotations

import csv
import io
import json
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANARY_LOGIN = "tb-canary-login-7f3a91"
REF = "https://docs.github.com/en/billing/reference/billing-reports"
CBP = "github/copilot-billing-preview src/reportAdapters.test.ts"
OAS = "github/rest-api-description descriptions/ghec/ghec.json 1.1.4 (+ api.github.com.json)"
DOC_ORDER = ["date", "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
             "gross_amount", "discount_amount", "net_amount", "username", "organization",
             "repository", "cost_center_name", "model", "input", "output", "cache_read",
             "cache_write", "total_monthly_quota"]
TRANSITION = ["date", "username", "product", "sku", "model", "quantity", "unit_type",
              "applied_cost_per_quantity", "gross_amount", "discount_amount", "net_amount",
              "exceeds_quota", "total_monthly_quota", "organization", "cost_center_name",
              "aic_quantity", "aic_gross_amount"]
#: USD per 1M tokens (input, output, cache read, cache write) — addendum §19.2 rows.
RATES = {"claude-opus-5-5": ("4", "20", "0.2", "5"), "claude-sonnet-5": ("2", "10", "0.2", "2.5"),
         "claude-haiku-4-5": ("1", "5", "0.1", "1.25"), "gpt-5.5": ("5", "30", "0.5", "0"),
         "claude-opus-4-8/fast": ("10", "50", "1.0", "12.5")}
LABELS = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5": "Claude Sonnet 5",
          "claude-haiku-4-5": "Claude Haiku 4.5", "gpt-5.5": "GPT-5.5",
          "claude-opus-4-8/fast": "Claude Opus 4.8 (fast mode)"}
TEAM_MAP = {"octo-alice": "platform", "octo-bob": "platform", "octo-carol": "infra",
            CANARY_LOGIN: "infra"}
MANIFEST: list[dict] = []


def nano(usd: Decimal) -> int:
    return int((usd * 10**9).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def fmt(d: Decimal) -> str:
    return format(d.normalize(), "f") if d else "0"


def priced(model: str, t: tuple[int, int, int, int], auto: bool = False) -> Decimal:
    rates = [Decimal(r) for r in RATES[model]]
    usd = sum((Decimal(n) * r for n, r in zip(t, rates, strict=True)), Decimal(0)) / 10**6
    return usd * Decimal("0.9") if auto else usd


def write_csv(rel: str, header: list[str], rows: list[dict], *, bom: bool = False,
              ui: bool = False) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n" if ui else "\n",
                   quoting=csv.QUOTE_ALL if ui else csv.QUOTE_MINIMAL)
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(h, "") for h in header])
    path = HERE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + buf.getvalue().encode())


def write_json(rel: str, doc: object, *, lines: bool = False) -> None:
    path = HERE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if lines:
        text = "".join(json.dumps(d, sort_keys=True) + "\n" for d in doc)  # type: ignore[union-attr]
    else:
        text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    path.write_text(text)


def entry(rel: str, adapter: str, provenance: str, source: str, role: str, expect: dict,
          **options: object) -> None:
    MANIFEST.append({"path": rel, "adapter": adapter, "provenance": provenance,
                     "values": "synthetic", "schema_source": source, "role": role,
                     "options": dict(sorted(options.items())), "expect": expect})


def totals(rows: list[dict]) -> dict:
    out = {"net_nano": 0, "gross_nano": 0, "rounding_remainder_e18": 0, "tokens": 0}
    for r in rows:
        out["net_nano"] += nano(Decimal(r["net_amount"]))
        out["gross_nano"] += nano(Decimal(r["gross_amount"]))
        exact = Decimal(r["net_amount"]) * 10**9
        out["rounding_remainder_e18"] += int((exact - nano(Decimal(r["net_amount"])))
                                             * 10**9)
        if r.get("_tokens"):
            out["tokens"] += sum(int(r[c] or 0) for c in ("input", "output", "cache_read",
                                                            "cache_write"))
    return out


def ai_row(date: str, login: str, model: str, t: tuple[int, int, int, int] | None, *,
           auto: bool = False, sku: str = "copilot_ai_credit", org: str = "acme-a",
           repo: str = "", cc: str = "", discount: str = "all", gross: str | None = None,
           label: str | None = None, tail: bool = False, quota: str = "Unknown",
           product: str = "copilot") -> dict:
    """One AI usage report row; *discount* ``all`` (pool), ``none`` (overage), ``half``."""
    g = Decimal(gross) if gross is not None else priced(model, t or (0, 0, 0, 0), auto)
    d = {"all": g, "none": Decimal(0), "half": (g / 2).quantize(Decimal("1E-9"))}[discount]
    net = g - d
    text = label if label is not None else ("Auto: " if auto else "") + LABELS[model]
    row = {"date": date, "product": product, "sku": sku, "quantity": fmt(g * 100),
           "unit_type": "ai-credits", "applied_cost_per_quantity": "0.01", "gross_amount": fmt(g),
           "discount_amount": fmt(d), "net_amount": fmt(net), "username": login,
           "organization": org, "repository": repo, "cost_center_name": cc, "model": text,
           "total_monthly_quota": quota, "_tokens": t is not None}
    if t is not None:
        row.update(input=str(t[0]), output=str(t[1]), cache_read=str(t[2]),
                   cache_write=str(t[3]))
    if tail:  # a float tail as a JavaScript/JSON serializer writes it
        for k in ("gross_amount", "net_amount") if net else ("gross_amount", "discount_amount"):
            row[k] = row[k] + "0000000000001" if "." in row[k] else row[k] + ".0000000000001"
    return row


def ai_usage_main() -> None:
    a, b, c, d, e = "octo-alice", "octo-bob", "octo-carol", "octo-dave", "octo-eve"
    t = (12_000, 3_000, 180_000, 6_000)
    rows = [ai_row(f"2026-08-{day}", a, "claude-sonnet-5", t) for day in range(25, 32)]
    rows += [ai_row("2026-08-27", d, "gpt-5.5", (20_000, 4_000, 80_000, 0), discount="none"),
             ai_row("2026-08-30", d, "gpt-5.5", (8_000, 1_000, 40_000, 0), tail=True)]
    rows += [ai_row(f"2026-09-{day:02d}", b, "claude-haiku-4-5", (50_000, 9_000, 300_000,
                                                                   12_000), auto=True)
             for day in (2, 9, 16)]
    rows.append(ai_row("2026-09-09", b, "claude-haiku-4-5", (20_000, 4_000, 100_000, 0)))
    for day in range(3, 8):
        r = ai_row(f"2026-09-{day:02d}", c, "claude-sonnet-5", (30_000, 6_000, 250_000, 9_000))
        if day in (5, 6):
            r["date"] = f"9/{day}/26"
        rows.append(r)
    fast = ai_row("2026-09-10", CANARY_LOGIN, "claude-opus-4-8/fast", (10_000, 2_000, 90_000, 0))
    fast["date"] = "2026-09-10T00:00:00Z"
    rows += [fast, ai_row("2026-09-11", CANARY_LOGIN, "claude-opus-4-8/fast",
                          (6_000, 1_000, 40_000, 2_000), repo="acme-a/payments-api")]
    for day, gross in ((4, "0.35"), (8, "1.2"), (15, "0.8"), (19, "0.42"), (22, "0.65")):
        rows.append(ai_row(f"2026-09-{day:02d}", "", "", (40_000, 5_000, 200_000, 0),
                           gross=gross, discount="none", label="Copilot Code Review",
                           quota=""))
    rows += [ai_row(f"2026-09-{day}", a, "claude-sonnet-5", (60_000, 15_000, 400_000, 20_000),
                    sku="coding_agent_ai_credit") for day in (12, 18)]
    rows += [ai_row("2026-09-14", d, "gpt-5.5", (15_000, 2_500, 60_000, 0),
                    repo="acme-a/web-app", discount="none"),
             ai_row("2026-09-17", a, "claude-sonnet-5", (9_000, 2_000, 70_000, 1_000),
                    cc="Platform, EMEA"),
             ai_row("2026-09-13", d, "claude-sonnet-5", (7_000, 1_000, 50_000, 0), org="acme-b"),
             ai_row("2026-09-20", e, "gpt-5.5", (25_000, 5_000, 90_000, 0), org="acme-b",
                    discount="half"),
             ai_row("2026-09-21", a, "claude-sonnet-5", (4_000, 1_000, 10_000, 0),
                    sku="spark_ai_credit", product="spark"),
             ai_row("2026-09-19", a, "", (30_000, 8_000, 150_000, 0), gross="0.9",
                    label="Copilot Coding Agent")]
    op = (12_000, 3_000, 180_000, 6_000)
    rows += [ai_row("2026-09-22", a, "claude-opus-5-5", op, discount="none", tail=True),
             ai_row("2026-09-23", a, "claude-opus-5-5", op, discount="none"),
             ai_row("2026-09-22", c, "claude-opus-5-5", op, discount="none"),
             ai_row("2026-09-23", CANARY_LOGIN, "claude-opus-5-5", op, discount="none"),
             ai_row("2026-09-23", b, "claude-opus-5-5", op, discount="none"),
             ai_row("2026-09-22", d, "claude-opus-5-5", op, discount="none"),
             ai_row("2026-09-22", e, "claude-opus-5-5", op, discount="none", org="acme-b")]
    assert len(rows) == 40, len(rows)
    write_csv("ai_usage_2026-09.csv", DOC_ORDER, rows, bom=True)
    # 40 natural keys; one token cell holds two rows (alice and bob, platform, 2026-09-23);
    # 29 distinct days
    entry("ai_usage_2026-09.csv", "github-ai-usage", "primary", f"{REF} (documented field "
          "order) + total_monthly_quota (" + CBP + ")", "acceptance",
          {"rows": 40, "cost_lines": 40, "token_aggregates": 39, "coverage_aggregates": 29,
           "quarantined": 0, "unattributed": 5, **totals(rows)}, now="2026-09-23",
          team_map=TEAM_MAP)


def ai_usage_small() -> None:
    header = ["date", "username", "product", "sku", "model", "quantity", "unit_type",
              "applied_cost_per_quantity", "gross_amount", "discount_amount", "net_amount",
              "total_monthly_quota", "organization", "cost_center_name"]
    test_row = {"date": "2026-06-01", "username": "mona", "product": "copilot",
                "sku": "copilot_ai_credit", "model": "Auto: Claude Haiku 4.5",
                "quantity": "42.726213", "unit_type": "ai-credits",
                "applied_cost_per_quantity": "0.01", "gross_amount": "0.4272621300000001",
                "discount_amount": "0.4272621300000001", "net_amount": "0",
                "total_monthly_quota": "3900", "organization": "example-org",
                "cost_center_name": ""}
    write_csv("ai_usage_github_test_row.csv", header, [test_row])
    entry("ai_usage_github_test_row.csv", "github-ai-usage", "primary",
          CBP + " ('detects native AI Credits reports when alias columns are absent')",
          "acceptance", {"rows": 1, "cost_lines": 1, "gross_nano": 427262130,
                         "net_nano": 0, "discount_nano": 427262130}, now="2026-09-23")
    legacy = []
    for date, login, sku, model, q, exceeds in (
            ("2026-05-29", "mona", "copilot_premium_request", "Auto: Claude Haiku 4.5", 2,
             "False"),
            ("2026-05-29", "hubot", "copilot_premium_request", "Claude Sonnet 4", 12, "True"),
            ("2026-05-30", "mona", "coding_agent_premium_request", "Coding Agent", 1, "False"),
            ("2026-05-30", "hubot", "spark_premium_request", "Claude Sonnet 4", 3, "True"),
            ("2026-05-31", "", "copilot_premium_request", "Copilot Code Review", 4, "False")):
        g = Decimal("0.04") * q
        legacy.append({"date": date, "username": login, "product": "spark" if "spark" in sku
                       else "copilot", "sku": sku, "model": model, "quantity": str(q),
                       "unit_type": "requests", "applied_cost_per_quantity": "0.04",
                       "gross_amount": fmt(g), "discount_amount": "0", "net_amount": fmt(g),
                       "exceeds_quota": exceeds, "total_monthly_quota": "300",
                       "organization": "example-org", "cost_center_name": "Cost Center A",
                       "aic_quantity": str(q * 10), "aic_gross_amount": fmt(g * Decimal("2.5"))})
    write_csv("ai_usage_legacy_pru.csv", TRANSITION, legacy)
    entry("ai_usage_legacy_pru.csv", "github-ai-usage", "primary", CBP + " (transition-period "
          "preview header)", "legacy", {"rows": 5, "cost_lines": 5, "token_aggregates": 0,
                                        "unattributed": 1, **totals(legacy)}, now="2026-09-23")
    april = [dict(legacy[0], date="2026-04-10", aic_quantity="20"),
             dict(legacy[1], date="2026-04-11"),
             # GitHub's two back-fill rows: quantity 0 with quota 300 → dropped; kept otherwise
             dict(legacy[0], date="2026-04-25", model="GPT-5", quantity="0", gross_amount="0",
                  net_amount="0", organization="", cost_center_name=""),
             dict(legacy[0], date="2026-04-25", model="GPT-5", quantity="10",
                  gross_amount="0.40", net_amount="0.40", total_monthly_quota="0",
                  organization="", cost_center_name="", aic_quantity="100",
                  aic_gross_amount="1.00")]
    write_csv("ai_usage_april_preview.csv", TRANSITION, april)
    kept = [april[0], april[1], april[3]]
    entry("ai_usage_april_preview.csv", "github-ai-usage", "primary", CBP + " (April back-fill "
          "rows)", "legacy", {"rows": 4, "cost_lines": 3, "backfill_duplicates_dropped": 1,
                              "directional_rows": 3, **totals(kept)}, now="2026-09-23")
    native = header + ["aic_quantity", "aic_gross_amount"]
    rows = [dict(test_row, quantity="96.9990345", gross_amount="0.969990345",
                 discount_amount="0", net_amount="0.969990345", aic_quantity="96.9990345",
                 aic_gross_amount="0.969990345"),
            dict(test_row, date="6/1/26", model="Claude Sonnet 4", quantity="96.9990345",
                 gross_amount="0.969990345", discount_amount="0", net_amount="0.969990345",
                 aic_quantity="0", aic_gross_amount="0")]
    write_csv("ai_usage_preview_columns.csv", native, rows)
    entry("ai_usage_preview_columns.csv", "github-ai-usage", "primary", CBP + " (native report "
          "with alias columns)", "legacy", {"rows": 2, "cost_lines": 2, "preview_rows": 1,
                                            **totals(rows)}, now="2026-09-23")


def overlap_and_duplicates() -> None:
    t = (10_000, 2_000, 100_000, 0)
    a = [ai_row(f"2026-09-{day:02d}", login, "claude-sonnet-5", t, quota="")
         for day in range(1, 11) for login in ("octo-alice", "octo-bob")]
    b = [ai_row(f"2026-09-{day:02d}", login, "claude-sonnet-5", t, quota="")
         for day in range(8, 18) for login in ("octo-alice", "octo-bob")]
    revised = next(r for r in b if r["date"] == "2026-09-09" and r["username"] == "octo-bob")
    revised.update(discount_amount="0", net_amount=revised["gross_amount"])
    write_csv("ai_usage_overlap_a.csv", DOC_ORDER, a)
    write_csv("ai_usage_overlap_b.csv", DOC_ORDER, b)
    for rel, rows, now in (("ai_usage_overlap_a.csv", a, "2026-09-11"),
                           ("ai_usage_overlap_b.csv", b, "2026-09-18")):
        entry(rel, "github-ai-usage", "primary", f"{REF} (documented field order)", "overlap",
              {"rows": 20, "cost_lines": 20, **totals(rows)}, now=now, team_map=TEAM_MAP)
    dup = [ai_row("2026-09-02", "octo-alice", "claude-sonnet-5", t, quota=""),
           ai_row("2026-09-02", "octo-alice", "claude-sonnet-5", (5_000, 1_000, 50_000, 0),
                  quota=""),
           ai_row("2026-09-02", "octo-alice", "claude-haiku-4-5", t, auto=True, quota=""),
           ai_row("2026-09-02", "octo-alice", "claude-haiku-4-5", t, quota="")]
    write_csv("ai_usage_duplicates.csv", DOC_ORDER, dup)
    entry("ai_usage_duplicates.csv", "github-ai-usage", "primary", f"{REF}", "duplicates",
          {"rows": 4, "cost_lines": 3, "duplicate_keys": 1, "token_aggregates": 3,
           **totals(dup)}, now="2026-09-23", team_map=TEAM_MAP)


def quota_pair() -> None:
    rows = []
    for i in range(65):
        rows.append(ai_row(f"2026-09-{1 + i % 5:02d}", f"dev{i:03d}", "claude-sonnet-5",
                           (1_000 * (i + 1), 500, 20_000, 0),
                           quota="3900" if i < 60 else "Unknown"))
    rows.append(ai_row("2026-09-03", "", "", (5_000, 1_000, 10_000, 0), gross="0.25",
                       discount="none", label="Copilot Code Review", quota=""))
    write_csv("ai_usage_quota_api.csv", DOC_ORDER, rows)
    ui_rows = [dict(r, date=f"9/{int(r['date'][-2:])}/26") for r in rows]
    ui_order = DOC_ORDER[9:10] + DOC_ORDER[:9] + DOC_ORDER[10:]
    write_csv("ai_usage_quota_ui.csv", ui_order, ui_rows, bom=True, ui=True)
    for rel, kind in (("ai_usage_quota_api.csv", "report export API (ai_credit)"),
                      ("ai_usage_quota_ui.csv", "UI download (BOM, M/D/YY, CRLF, quoted)")):
        entry(rel, "github-ai-usage", "primary", f"{REF}; {kind}", "plan_quota",
              {"rows": 66, "cost_lines": 66, "quota_users": 60, "quota_unknown": 5,
               **totals(rows)}, now="2026-09-23")


def edge() -> None:
    good = ai_row("2026-09-02", "octo-alice", "claude-sonnet-5", (1_000, 100, 0, 0), quota="")
    bad = [dict(good, date="2026-13-40"), dict(good, net_amount=""),
           dict(good, gross_amount="1,234.00"), dict(good, input="-5"),
           dict(good, output="1.5"), dict(good, sku=""), dict(good, organization="acme a!"),
           dict(good, date=""), dict(good, quantity="1e999")]
    ok = [dict(good, sku="copilot_mystery_credit"),
          dict(good, date="2026-09-01T01:30:00+02:00", model="Claude Haiku 4.5"),
          dict(good, date="9/3/2026", net_amount="0.5", model="Claude Opus 4.8"),
          dict(good, model="", date="2026-09-04"), good]
    write_csv("ai_usage_edge.csv", DOC_ORDER, bad + ok)
    entry("ai_usage_edge.csv", "github-ai-usage", "primary", f"{REF}", "edge",
          {"rows": 14, "cost_lines": 5, "quarantined": 9, **totals(ok)}, now="2026-09-23",
          team_map=TEAM_MAP)


def metered() -> None:
    hdr = ["date", "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
           "gross_amount", "discount_amount", "net_amount", "username", "organization",
           "repository", "workflow_path", "cost_center_name"]

    def row(date: str, product: str, sku: str, qty: str, unit: str, price: str, *,
            login: str = "", org: str = "acme-a", repo: str = "", path: str = "",
            discount: str = "0", cc: str = "") -> dict:
        g = Decimal(qty) * Decimal(price)
        return {"date": date, "product": product, "sku": sku, "quantity": qty, "unit_type": unit,
                "applied_cost_per_quantity": price, "gross_amount": fmt(g),
                "discount_amount": fmt(g if discount == "all" else Decimal(discount)),
                "net_amount": fmt(Decimal(0) if discount == "all" else g - Decimal(discount)),
                "username": login, "organization": org, "repository": repo,
                "workflow_path": path, "cost_center_name": cc}

    agent = "dynamic/copilot-swe-agent/copilot"
    review = "dynamic/agents/copilot-pull-request-reviewer"
    review2 = "dynamic/copilot-pull-request-reviewer/copilot-pull-request-reviewer"
    rows = [row("2026-09-01", "copilot", "copilot_for_business", "1", "user-months", "19",
                login="octo-alice", cc="Platform CC"),
            row("2026-09-01", "copilot", "copilot_for_business", "1", "user-months", "19",
                login="octo-bob"),
            row("2026-09-01", "copilot", "copilot_for_business", "0.5333333", "user-months",
                "19", login=CANARY_LOGIN),
            row("2026-09-01", "copilot", "copilot_enterprise", "10", "user-months", "39",
                org="acme-b"),
            row("2026-09-05", "actions", "actions_linux", "120", "minutes", "0.006",
                login="octo-alice", repo="acme-a/web-app", path=agent, discount="all"),
            row("2026-09-05", "actions", "actions_linux", "30", "minutes", "0.006",
                repo="acme-a/private-api", path=agent),
            row("2026-09-06", "actions", "linux_16_core", "40", "minutes", "0.042",
                repo="acme-a/web-app", path=review),
            row("2026-09-06", "actions", "actions_linux_16_core", "25", "minutes", "0.042",
                repo="acme-a/private-api", path=review2),
            row("2026-09-07", "actions", "actions_linux", "15", "minutes", "0.006",
                repo="acme-a/web-app", path="dynamic/github-code-quality/codeql"),
            row("2026-09-07", "actions", "actions_linux", "60", "minutes", "0.006",
                repo="acme-a/ops", path=".github/workflows/issue-triage.lock.yml"),
            row("2026-09-07", "actions", "actions_linux", "500", "minutes", "0.006",
                repo="acme-a/web-app", path=".github/workflows/ci.yml"),
            row("2026-09-07", "actions", "actions_storage", "2", "gigabyte-hours", "0.0003"),
            row("2026-09-08", "sandbox", "sandbox_linux", "90", "minutes", "0.008",
                login="octo-carol"),
            row("2026-09-08", "sandbox", "sandbox_memory", "12", "gigabyte-hours", "0.001"),
            row("2026-09-01", "code_quality", "code_quality_licenses", "3", "user-months", "10"),
            row("2026-09-09", "copilot", "copilot_ai_credit", "1234.5", "ai-credits", "0.01",
                login="octo-alice", discount="all"),
            row("2026-08-30", "copilot", "copilot_for_business", "1", "user-months", "19",
                login="octo-carol"),
            row("2026-09-09", "packages", "packages_storage", "3", "gigabyte-hours", "0.0008")]
    write_csv("detailed_2026-09.csv", hdr, rows)
    kept = [r for i, r in enumerate(rows) if i not in (10, 11, 17)]
    entry("detailed_2026-09.csv", "github-metered-usage", "primary", f"{REF} (detailed usage "
          "report fields)", "acceptance",
          {"rows": 18, "cost_lines": 15, "seat_lines": 5, "actions_lines": 6,
           "sandbox_lines": 2, "user_workflows_dropped": 1, "actions_unattributed_dropped": 1,
           "rows_other_products": 1, **totals(kept)}, now="2026-09-23", team_map=TEAM_MAP)
    summ_hdr = [h for h in hdr if h not in ("username", "workflow_path")]
    summ = [rows[0] | {"quantity": "40", "gross_amount": "760", "net_amount": "760"},
            rows[3], rows[5], rows[12], rows[15], rows[14]]
    write_csv("summarized_2026-09.csv", summ_hdr, summ)
    kept_s = [summ[i] for i in (0, 1, 3, 4, 5)]
    entry("summarized_2026-09.csv", "github-metered-usage", "primary", f"{REF} (summarized "
          "usage report fields)", "acceptance",
          {"rows": 6, "cost_lines": 5, "actions_unattributed_dropped": 1, **totals(kept_s)},
          now="2026-09-23")


def rest() -> None:
    item = {"product": "Copilot", "sku": "Copilot AI Credits", "model": "GPT-5",
            "unitType": "credits", "pricePerUnit": 0.01, "grossQuantity": 100,
            "grossAmount": 1.0, "discountQuantity": 0, "discountAmount": 0.0,
            "netQuantity": 100, "netAmount": 1.0}
    user_item = dict(item, product="Copilot AI Credits", sku="AI Credit", unitType="ai-credits")
    docs = {"rest/oas_ai_credit_enterprise.json": {"timePeriod": {"year": 2025},
                                                   "enterprise": "GitHub", "usageItems": [item]},
            "rest/oas_ai_credit_org.json": {"timePeriod": {"year": 2025},
                                            "organization": "GitHub", "usageItems": [item]},
            "rest/oas_ai_credit_user.json": {"timePeriod": {"year": 2025}, "user": "monalisa",
                                             "usageItems": [user_item]},
            "rest/oas_premium_request.json": {"timePeriod": {"year": 2025}, "enterprise": "GitHub",
                                              "usageItems": [dict(item, sku="Copilot Premium "
                                                                  "Request", unitType="requests",
                                                                  pricePerUnit=0.04,
                                                                  grossAmount=4.0,
                                                                  netAmount=4.0)]},
            "rest/oas_usage_summary.json": {"timePeriod": {"year": 2025}, "enterprise": "GitHub",
                                            "usageItems": [{"product": "Actions",
                                                            "sku": "actions_linux",
                                                            "unitType": "minutes",
                                                            "pricePerUnit": 0.008,
                                                            "grossQuantity": 1000,
                                                            "grossAmount": 8.0,
                                                            "discountQuantity": 0,
                                                            "discountAmount": 0.0,
                                                            "netQuantity": 1000,
                                                            "netAmount": 8.0}]},
            "rest/oas_usage.json": {"usageItems": [{"date": "2023-08-01", "product": "Actions",
                                                    "sku": "Actions Linux", "quantity": 100,
                                                    "unitType": "minutes", "pricePerUnit": 0.008,
                                                    "grossAmount": 0.8, "discountAmount": 0,
                                                    "netAmount": 0.8, "organizationName": "GitHub",
                                                    "repositoryName": "github/example"}]},
            "rest/oas_report_exports.json": {"usage_report_exports": [
                {"id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "report_type": "detailed",
                 "start_date": "2024-01-01", "end_date": "2024-01-31", "status": "completed",
                 "download_urls": ["https://github.com/enterprises/github/metered_exports/1234"],
                 "created_at": "2024-01-15T10:30:00Z", "actor": "monalisa"}]}}
    for rel, doc in docs.items():
        write_json(rel, doc)
    expect = {"rest/oas_ai_credit_enterprise.json": (1, 1_000_000_000),
              "rest/oas_ai_credit_org.json": (1, 1_000_000_000),
              "rest/oas_ai_credit_user.json": (1, 1_000_000_000),
              "rest/oas_premium_request.json": (1, 4_000_000_000),
              "rest/oas_usage_summary.json": (1, 8_000_000_000),
              "rest/oas_usage.json": (1, 800_000_000), "rest/oas_report_exports.json": (0, 0)}
    for rel, (n, net) in expect.items():
        entry(rel, "github-billing-api", "primary", OAS + " (response example)", "oas",
              {"cost_lines": n, "net_nano": net, "export_envelopes": int(n == 0)},
              now="2026-09-23")
    base = "/enterprises/acme/settings/billing"
    ai_items = [dict(item, model=m, sku="Copilot AI Credits", grossQuantity=q, grossAmount=g,
                     discountQuantity=q, discountAmount=g, netQuantity=0, netAmount=0)
                for m, q, g in (("Claude Sonnet 5", 12345.6789, 123.456789),
                                ("Auto: Claude Haiku 4.5", 5000, 50.0),
                                ("Copilot Code Review", 800.5, 8.005))]
    pages = [
        {"request": {"path": f"{base}/ai_credit/usage", "query": {"year": 2026, "month": 9}},
         "response": {"timePeriod": {"year": 2026, "month": 9}, "enterprise": "acme",
                      "costCenter": {"id": "cc-1", "name": "Platform CC"},
                      "usageItems": ai_items}},
        {"request": {"path": f"{base}/usage/summary", "query": {"year": 2026, "month": 8}},
         "response": {"timePeriod": {"year": 2026, "month": 8}, "enterprise": "acme",
                      "usageItems": [
                          {"product": "Copilot", "sku": "copilot_ai_credit",
                           "unitType": "ai-credits", "pricePerUnit": 0.01,
                           "grossQuantity": 250000, "grossAmount": 2500.0,
                           "discountQuantity": 190000, "discountAmount": 1900.0,
                           "netQuantity": 60000, "netAmount": 600.0},
                          {"product": "Copilot", "sku": "copilot_for_business",
                           "unitType": "user-months", "pricePerUnit": 19, "grossQuantity": 100,
                           "grossAmount": 1900, "discountQuantity": 0, "discountAmount": 0,
                           "netQuantity": 100, "netAmount": 1900},
                          {"product": "Packages", "sku": "packages_storage",
                           "unitType": "gigabyte-hours", "pricePerUnit": 0.0008,
                           "grossQuantity": 10, "grossAmount": 0.008, "discountQuantity": 0,
                           "discountAmount": 0, "netQuantity": 10, "netAmount": 0.008}]}},
        {"request": {"path": f"{base}/usage", "query": {"cost_center_id": "cc-1"}},
         "response": {"usageItems": [
             {"date": "2026-09-02", "product": "Copilot", "sku": "copilot_ai_credit",
              "quantity": 1235, "unitType": "ai-credits", "pricePerUnit": 0.01,
              "grossAmount": 12.3456789, "discountAmount": 12.3456789, "netAmount": 0,
              "organizationName": "acme-a"},
             {"date": "2026-09-02", "product": "Actions", "sku": "actions_linux",
              "quantity": 100, "unitType": "minutes", "pricePerUnit": 0.006,
              "grossAmount": 0.6, "discountAmount": 0, "netAmount": 0.6,
              "organizationName": "acme-a", "repositoryName": "acme-a/web-app"}]}},
        {"request": {"path": f"{base}/reports", "query": {}},
         "response": {"id": "rep-1", "report_type": "ai_credit", "start_date": "2026-09-01",
                      "end_date": "2026-09-30", "status": "completed",
                      "download_urls": ["https://example.invalid/signed?token=never-stored"]}}]
    write_json("rest/pull_pages_2026-09.jsonl", pages, lines=True)
    entry("rest/pull_pages_2026-09.jsonl", "github-billing-api", "primary", OAS + " (schemas; "
          "CP-PULL request/response envelopes)", "acceptance",
          {"pages": 4, "cost_lines": 7, "export_envelopes": 1, "items_other_products": 1,
           "integer_quantity": 1, "net_nano": 2_500_600_000_000}, now="2026-09-23")


def goldens() -> None:
    op, g = (12_000, 3_000, 180_000, 6_000), 174_000_000   # Appendix C.G1 point
    for conv, inp in (("excl", op[0]), ("incl", op[0] + op[2] + op[3])):
        write_json(f"conventions/golden_{conv}.json", {
            "convention": f"github.ai_usage_report.{conv}", "rows": [
                {"model": "claude-opus-5-5", "date": "2026-09-23", "routing": "direct",
                 "raw": {"input": inp, "output": op[1], "cache_read": op[2],
                         "cache_write": op[3]},
                 "buckets": {"uncached_input": 12_000, "cache_read": 180_000,
                             "cache_write_unknown": 6_000, "output": 3_000},
                 "gross_nano": g},
                {"model": "gpt-5.5", "date": "2026-09-10", "routing": "direct",
                 "raw": {"input": 20_000 + (250_000 if conv == "incl" else 0), "output": 4_000,
                         "cache_read": 250_000, "cache_write": 0},
                 "buckets": {"uncached_input": 20_000, "cache_read": 250_000,
                             "cache_write_unknown": 0, "output": 4_000},
                 "gross_nano": 345_000_000}]})   # Appendix C.G5 second case


def main() -> None:
    ai_usage_main()
    ai_usage_small()
    overlap_and_duplicates()
    quota_pair()
    edge()
    metered()
    rest()
    goldens()
    doc = {"schema": "tokenbill/copilot-bill-fixtures@1", "keys": "tests choose the keys; "
           "values are synthetic and never real exports", "team_map": TEAM_MAP,
           "files": sorted(MANIFEST, key=lambda e: e["path"])}
    (HERE / "MANIFEST.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
