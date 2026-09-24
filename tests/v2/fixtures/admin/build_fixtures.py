#!/usr/bin/env python3
"""Build the ADMIN recorded-page fixtures and their MANIFEST.json (package ADMIN, SPEC §5.11,
§5.13).

Stand-alone: ``python tests/v2/fixtures/admin/build_fixtures.py`` rewrites every fixture file in
this directory, byte-identically on every run and Python version (no randomness; numbers come from
SHA-256). It never imports ``tokenbill``: expected totals in MANIFEST.json are computed here in
closed form from the generated rows, so the adapter tests compare two independent computations.
Rates for the reconcilable usage/cost pair are read from ``tokenbill/core/facts.json`` (the same
rows FakePricer and the RateCard use), so the cost report equals the usage report priced at list.

All data is synthetic. Shapes follow the documented responses (retrieved 2026-09-23):
Usage & Cost Admin API, Claude Code Analytics API, Claude Enterprise Analytics API reference, the
OpenAI OpenAPI description (``UsageCompletionsResult``, ``CostsResult``), the AWS CUR 2.0 data
dictionary and the GCP standard usage cost export schema. Values the documentation does not fix
(CUR ``pricing_unit`` strings, GCP ``usage.unit``, SKU ids, descriptions) are invented and listed as
unverified in ``tests/v2/admin/README.md``.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
FACTS = REPO / "tokenbill" / "core" / "facts.json"
CANARY = "TB-CANARY-7f3a91"
CANARY_EMAIL = f"canary.{CANARY}@example.com"
NOW = "2026-09-23"
MTOK = Decimal(1_000_000)
DOCS = {
    "usage": "https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-messages-usage-report",
    "cost": "https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-cost-report",
    "cc": "https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api",
    "ent": "https://platform.claude.com/docs/en/api/admin/analytics",
    "openai": "https://github.com/openai/openai-openapi (UsageCompletionsResult, CostsResult)",
    "cur": "https://docs.aws.amazon.com/cur/latest/userguide/table-dictionary-cur2.html",
    "gcp": "https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/"
           "standard-usage",
}


def num(tag: str, lo: int, hi: int) -> int:
    """A deterministic integer in [lo, hi] derived from *tag*."""
    h = int(hashlib.sha256(tag.encode()).hexdigest()[:12], 16)
    return lo + h % (hi - lo + 1)


def rates() -> dict[str, dict[str, Decimal]]:
    facts = json.loads(FACTS.read_text(encoding="utf-8"))
    out = {}
    for row in facts["rates"]:
        if row["channel"] != "anthropic_api" or not row.get("enabled", True):
            continue
        inp = Decimal(row["usd_per_mtok"]["input"])
        mult = row["multipliers"]
        out[row["model"]] = {
            "uncached_input_tokens": inp,
            "output_tokens": Decimal(row["usd_per_mtok"]["output"]),
            "cache_read_input_tokens": inp * Decimal(mult["cache_read"]),
            "cache_creation.ephemeral_5m_input_tokens": inp * Decimal(mult["cache_write_5m"]),
            "cache_creation.ephemeral_1h_input_tokens": inp * Decimal(mult["cache_write_1h"]),
            "web_search": Decimal(row["per_request_usd"]["web_search"]),
        }
    return out


def iso(day: str, hour: int = 0) -> str:
    return f"{day}T{hour:02d}:00:00Z"


def next_day(day: str) -> str:
    return date.fromordinal(date.fromisoformat(day).toordinal() + 1).isoformat()


def unix(day: str) -> int:
    return (date.fromisoformat(day).toordinal() - date(1970, 1, 1).toordinal()) * 86_400


def cents(usd: Decimal) -> str:
    """Exact cents decimal string without exponent."""
    text = format((usd * 100).normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def nano(usd: Decimal) -> int:
    return int((usd * 10**9).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def dump_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def dump_jsonl(path: Path, objs: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(o, sort_keys=True, separators=(",", ":")) + "\n"
                            for o in objs), encoding="utf-8")


def write_gz(path: Path, data: bytes) -> None:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(data)
    path.write_bytes(buf.getvalue())


def usage_tokens(u: dict) -> int:
    cc = u.get("cache_creation", {})
    return (u["uncached_input_tokens"] + u.get("cache_read_input_tokens", 0)
            + cc.get("ephemeral_5m_input_tokens", 0) + cc.get("ephemeral_1h_input_tokens", 0)
            + u["output_tokens"])


# ---------------------------------------------------------------------------------------------
# Anthropic usage + cost report (the reconcilable pair) and the edge pair
# ---------------------------------------------------------------------------------------------

MODEL_NAMES = {"claude-opus-5": "Claude Opus 5", "claude-sonnet-5": "Claude Sonnet 5",
               "claude-haiku-4-5": "Claude Haiku 4.5"}
TOKEN_LABELS = {
    "uncached_input_tokens": "Input Tokens", "output_tokens": "Output Tokens",
    "cache_read_input_tokens": "Cache Read Tokens",
    "cache_creation.ephemeral_5m_input_tokens": "Cache Write Tokens (5m)",
    "cache_creation.ephemeral_1h_input_tokens": "Cache Write Tokens (1h)",
}
RECON_DAYS = ("2026-08-10", "2026-08-11")
RECON_GROUPS = (  # (workspace_id, api_key_id, model, inference_geo)
    ("wrkspc_01PaymentsProd", "apikey_01PaymentsApp", "claude-opus-5", "global"),
    ("wrkspc_01PaymentsProd", "apikey_02PaymentsBatchJobs", "claude-sonnet-5", "global"),
    ("wrkspc_02SearchProd", "apikey_03SearchRanker", "claude-opus-5", "global"),
    (None, "apikey_04DefaultWorkspace", "claude-haiku-4-5", "not_available"),
)


def recon_usage_result(day: str, ws: str | None, key: str, model: str, geo: str) -> dict:
    tag = f"{day}|{ws}|{key}|{model}"
    return {
        "account_id": None, "api_key_id": key,
        "cache_creation": {"ephemeral_1h_input_tokens": num(tag + "w1", 0, 400_000),
                           "ephemeral_5m_input_tokens": num(tag + "w5", 10_000, 2_000_000)},
        "cache_read_input_tokens": num(tag + "r", 1_000_000, 90_000_000),
        "context_window": "0-200k", "inference_geo": geo, "model": model,
        "output_tokens": num(tag + "o", 50_000, 3_000_000),
        "server_tool_use": {"web_search_requests": num(tag + "ws", 0, 40)
                            if model == "claude-opus-5" else 0},
        "service_account_id": None, "service_tier": "standard",
        "uncached_input_tokens": num(tag + "u", 100_000, 9_000_000),
        "workspace_id": ws,
    }


def build_recon_pair(r: dict) -> tuple[dict, dict, dict]:
    usage_page = {"data": [], "has_more": False, "next_page": None}
    cost_acc: dict[tuple, Decimal] = defaultdict(Decimal)
    web: dict[tuple, Decimal] = defaultdict(Decimal)
    tokens_total = 0
    for day in RECON_DAYS:
        results = []
        for ws, key, model, geo in RECON_GROUPS:
            res = recon_usage_result(day, ws, key, model, geo)
            results.append(res)
            tokens_total += usage_tokens(res)
            rate = r[model]
            for tt, n in (("uncached_input_tokens", res["uncached_input_tokens"]),
                          ("output_tokens", res["output_tokens"]),
                          ("cache_read_input_tokens", res["cache_read_input_tokens"]),
                          ("cache_creation.ephemeral_5m_input_tokens",
                           res["cache_creation"]["ephemeral_5m_input_tokens"]),
                          ("cache_creation.ephemeral_1h_input_tokens",
                           res["cache_creation"]["ephemeral_1h_input_tokens"])):
                cost_acc[(day, ws, model, geo, tt)] += Decimal(n) * rate[tt] / MTOK
            searches = res["server_tool_use"]["web_search_requests"]
            if searches:
                web[(day, ws)] += Decimal(searches) * rate["web_search"]
        usage_page["data"].append({"starting_at": iso(day), "ending_at": iso(next_day(day)),
                                   "results": results})
    cost_page = {"data": [], "has_more": False, "next_page": None}
    amount_nano = 0
    lines = 0
    for day in RECON_DAYS:
        results = []
        for (d, ws, model, geo, tt), usd in sorted(cost_acc.items(),
                                                   key=lambda kv: tuple(map(str, kv[0]))):
            if d != day:
                continue
            results.append({
                "amount": cents(usd), "context_window": "0-200k", "cost_type": "tokens",
                "currency": "USD",
                "description": f"{MODEL_NAMES[model]} Usage - {TOKEN_LABELS[tt]}",
                "inference_geo": geo, "model": model, "service_tier": "standard",
                "token_type": tt, "workspace_id": ws,
            })
            amount_nano += nano(usd)
            lines += 1
        for (d, ws), usd in sorted(web.items(), key=lambda kv: tuple(map(str, kv[0]))):
            if d != day:
                continue
            results.append({"amount": cents(usd), "context_window": None,
                            "cost_type": "web_search", "currency": "USD",
                            "description": "Web Search Usage", "inference_geo": None,
                            "model": None, "service_tier": None, "token_type": None,
                            "workspace_id": ws})
            amount_nano += nano(usd)
            lines += 1
        cost_page["data"].append({"starting_at": iso(day), "ending_at": iso(next_day(day)),
                                  "results": results})
    expect = {"usage_aggregates": len(RECON_DAYS) * len(RECON_GROUPS), "usage_tokens": tokens_total,
              "cost_lines": lines, "amount_nano": amount_nano}
    return usage_page, cost_page, expect


def build_usage_pages_jsonl() -> tuple[list[dict], dict]:
    """Two pages (has_more / next_page) of a provisional window, one JSONL file."""
    pages = []
    days = ("2026-09-10", "2026-09-11")
    tokens_total = 0
    for i, day in enumerate(days):
        results = []
        for ws, key, model, geo in RECON_GROUPS[:2]:
            res = recon_usage_result(day, ws, key, model, geo)
            tokens_total += usage_tokens(res)
            results.append(res)
        pages.append({"data": [{"starting_at": iso(day), "ending_at": iso(next_day(day)),
                                "results": results}],
                      "has_more": i == 0, "next_page": "page_MjAyNi0wOS0xMQ==" if i == 0
                      else None})
    return pages, {"usage_aggregates": 4, "usage_tokens": tokens_total}


def build_edge_pair() -> tuple[dict, dict, dict]:
    day = "2026-08-12"
    base = {"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
            "cache_read_input_tokens": 0, "context_window": "0-200k", "speed": "standard",
            "server_tool_use": {"web_search_requests": 0}, "service_account_id": None,
            "workspace_id": "wrkspc_03Research", "api_key_id": "apikey_05Research"}
    results = [
        # Priority Tier: usage report only (not in the cost report)
        {**base, "account_id": None, "inference_geo": "global", "model": "claude-opus-5",
         "output_tokens": 120_000, "service_tier": "priority", "uncached_input_tokens": 800_000},
        # grouped by account (a person): two rows that differ only by account are summed
        {**base, "account_id": "user_01AliceExample", "inference_geo": "us",
         "model": "claude-sonnet-5", "output_tokens": 10_000, "service_tier": "standard",
         "uncached_input_tokens": 200_000},
        {**base, "account_id": "user_02BobExample", "inference_geo": "us",
         "model": "claude-sonnet-5", "output_tokens": 30_000, "service_tier": "standard",
         "uncached_input_tokens": 400_000},
        # fast mode, 1M context, web fetch
        {**base, "account_id": None, "inference_geo": "global", "model": "claude-opus-5",
         "output_tokens": 5_000, "service_tier": "standard", "uncached_input_tokens": 250_000,
         "speed": "fast", "context_window": "200k-1M",
         "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 2}},
    ]
    usage = {"data": [{"starting_at": iso(day), "ending_at": iso(next_day(day)),
                       "results": results}], "has_more": False, "next_page": None}
    tokens_total = sum(usage_tokens(x) for x in results)
    ws = "wrkspc_03Research"

    def row(amount: str, cost_type: str, description: str, model: str | None = None,
            token_type: str | None = None, geo: str | None = None,
            context_window: str | None = None, workspace: str | None = ws) -> dict:
        return {"amount": amount, "context_window": context_window, "cost_type": cost_type,
                "currency": "USD", "description": description, "inference_geo": geo,
                "model": model, "service_tier": "standard" if model else None,
                "token_type": token_type, "workspace_id": workspace}

    # list prices: Sonnet 5 at the US-geo 1.1x ($2.20 / $11.00 per MTok) on 600k in / 40k out;
    # Opus 5 fast mode ($10 / $50 base) on 250k in / 5k out; 3 web searches at $0.01.
    # The Sonnet input string carries 4.5e-15 USD beyond the nano (the remainder under test);
    # the fast-mode output is split over two context windows whose strings sum exactly to 25 c.
    cost_rows = [
        row("1234.5", "code_execution", "Code Execution Usage"),
        row("132.00000000000045", "tokens", "Claude Sonnet 5 Usage - Input Tokens",
            "claude-sonnet-5", "uncached_input_tokens", "us", "0-200k"),
        row("44", "tokens", "Claude Sonnet 5 Usage - Output Tokens", "claude-sonnet-5",
            "output_tokens", "us", "0-200k"),
        row("250", "tokens", "Claude Opus 5 Usage - Input Tokens (Fast)", "claude-opus-5",
            "uncached_input_tokens", "global", "200k-1M"),
        row("12.5000000004", "tokens", "Claude Opus 5 Usage - Output Tokens (Fast)",
            "claude-opus-5", "output_tokens", "global", "0-200k"),
        row("12.4999999996", "tokens", "Claude Opus 5 Usage - Output Tokens (Fast)",
            "claude-opus-5", "output_tokens", "global", "200k-1M"),
        row("3", "web_search", "Web Search Usage"),
        row("7", "session_usage", "Session Usage", workspace=None),
    ]
    cost = {"data": [{"starting_at": iso(day), "ending_at": iso(next_day(day)),
                      "results": cost_rows}], "has_more": False, "next_page": None}
    # closed-form expectations: one line per row except the folded fast-mode output
    line_cents = [Decimal("1234.5"), Decimal("132.00000000000045"), Decimal(44), Decimal(250),
                  Decimal("12.5000000004") + Decimal("12.4999999996"), Decimal(3), Decimal(7)]
    line_usd = [c / 100 for c in line_cents]
    amount_nano = sum(nano(x) for x in line_usd)
    rem_e18 = sum(int(((x * 10**9 - nano(x)) * 10**9).quantize(Decimal(1),
                                                               rounding=ROUND_HALF_EVEN))
                  for x in line_usd)
    expect = {"usage_aggregates": 3, "usage_tokens": tokens_total, "cost_lines": len(line_usd),
              "amount_nano": amount_nano, "rounding_remainder_e18": rem_e18}
    return usage, cost, expect


# ---------------------------------------------------------------------------------------------
# Claude Code Analytics
# ---------------------------------------------------------------------------------------------

def team_users() -> dict[str, list[str]]:
    return {
        "platform": [f"dev{i:02d}@example.com" for i in range(1, 6)] + [CANARY_EMAIL],
        "payments": [f"dev{i:02d}@example.com" for i in range(7, 12)],
        "mobile": [f"dev{i:02d}@example.com" for i in range(12, 15)],
        "tiny": ["dev15@example.com", "dev16@example.com"],
    }


def cc_record(day: str, actor: dict, tag: str) -> dict:
    models = ["claude-opus-5"] + (["claude-sonnet-5"] if num(tag + "m", 0, 1) else [])
    breakdown = []
    for m in models:
        t = {"input": num(tag + m + "i", 1_000, 400_000),
             "output": num(tag + m + "o", 1_000, 90_000),
             "cache_read": num(tag + m + "r", 10_000, 9_000_000),
             "cache_creation": num(tag + m + "c", 1_000, 600_000)}
        breakdown.append({"model": m, "tokens": t,
                          "estimated_cost": {"currency": "USD",
                                             "amount": num(tag + m + "$", 1, 9_000)}})
    return {
        "date": iso(day), "actor": actor,
        "organization_id": "dc9f6c26-b22c-4831-8d01-0446bada88f1",
        "customer_type": "api", "terminal_type": f"vscode {CANARY}",
        "core_metrics": {"num_sessions": num(tag + "s", 1, 9),
                         "lines_of_code": {"added": num(tag + "la", 0, 2_000),
                                           "removed": num(tag + "lr", 0, 900)},
                         "commits_by_claude_code": num(tag + "c", 0, 12),
                         "pull_requests_by_claude_code": num(tag + "p", 0, 3)},
        "tool_actions": {k: {"accepted": num(tag + k + "a", 0, 50),
                             "rejected": num(tag + k + "r", 0, 6)}
                         for k in ("edit_tool", "multi_edit_tool", "write_tool",
                                   "notebook_edit_tool")},
        "model_breakdown": breakdown,
    }


def user_actor(email: str) -> dict:
    return {"type": "user_actor", "email_address": email}


def cc_expect(records: list[dict], teams_of: dict[str, str], k: int) -> dict:
    """Closed-form k-anonymous rollup per day (mirrors SPEC §5.11 / §8.4 merge_small_groups)."""
    per: dict[tuple[str, str], dict] = {}
    for rec in records:
        day = rec["date"][:10]
        a = rec["actor"]
        ref = a.get("email_address") or a.get("api_key_name")
        team = teams_of.get(ref, "(unmapped)")
        cell = per.setdefault((day, team), {"users": set(), "commits": 0, "tokens": 0,
                                            "cost_cents": 0, "models": set()})
        cell["users"].add(ref)
        cell["commits"] += rec["core_metrics"]["commits_by_claude_code"]
        for b in rec["model_breakdown"]:
            cell["tokens"] += sum(b["tokens"].values())
            cell["cost_cents"] += b["estimated_cost"]["amount"]
            cell["models"].add(b["model"])
    out = {"outcomes": [], "dropped_groups": 0, "aggregates": 0, "tokens": 0, "cost_nano": 0}
    for day in sorted({d for d, _ in per}):
        big, small = [], []
        for (d, team), cell in sorted(per.items()):
            if d != day:
                continue
            (big if len(cell["users"]) >= k and team != "(other)" else small).append((team, cell))
        rows = [(t, len(c["users"]), c["commits"], c["tokens"], c["cost_cents"], c["models"])
                for t, c in big]
        if small:
            n = sum(len(c["users"]) for _, c in small)
            if n >= k:
                rows.append(("(other)", n, sum(c["commits"] for _, c in small),
                             sum(c["tokens"] for _, c in small),
                             sum(c["cost_cents"] for _, c in small),
                             set().union(*(c["models"] for _, c in small))))
            else:
                out["dropped_groups"] += len(small)
        for team, n, commits, toks, cost_cents, models in rows:
            out["outcomes"].append({"date": day, "team": team, "n_users": n, "commits": commits})
            out["aggregates"] += len(models)
            out["tokens"] += toks
            out["cost_nano"] += cost_cents * 10_000_000
    return out


def build_cc() -> tuple[dict, list[dict], dict, dict, dict[str, str]]:
    users = team_users()
    teams_of = {e: t for t, es in users.items() for e in es}
    day1 = "2026-09-10"
    recs1 = []
    for team in ("platform", "payments", "mobile", "tiny"):
        for e in users[team]:
            recs1.append(cc_record(day1, user_actor(e), f"{day1}{e}"))
    recs1.append(cc_record(day1, {"type": "api_actor", "api_key_name": "ci-bot-key"},
                           f"{day1}ci"))
    page1 = {"data": recs1, "has_more": False, "next_page": None}
    day2 = "2026-09-11"
    recs2 = [cc_record(day2, user_actor(e), f"{day2}{e}") for e in users["platform"][:5]]
    recs2 += [cc_record(day2, user_actor(e), f"{day2}{e}") for e in users["mobile"]]
    recs2 += [cc_record(day2, user_actor(users["tiny"][0]), f"{day2}tiny")]
    pages2 = [{"data": recs2[:4], "has_more": True, "next_page": "page_Y2M="},
              {"data": recs2[4:], "has_more": False, "next_page": None}]
    return (page1, pages2, cc_expect(recs1, teams_of, 5), cc_expect(recs2, teams_of, 5),
            teams_of)


# ---------------------------------------------------------------------------------------------
# Enterprise Analytics
# ---------------------------------------------------------------------------------------------

def build_enterprise(teams_of: dict[str, str], rate_table: dict
                     ) -> dict[str, tuple[dict, dict]]:
    org = {"organization_id": "org_01EnterpriseExample"}
    days = ("2026-08-01", "2026-09-01")   # final and provisional relative to 2026-09-23
    usage = {"data": [], "has_more": False, "next_page": None,
             "data_refreshed_at": "2026-09-22T12:00:00Z", **org}
    toks = 0
    for day in days:
        results = []
        for product in ("chat", "claude_code"):
            for model in ("claude-opus-5", "claude-sonnet-5"):
                tag = f"ent{day}{product}{model}"
                r = {"cache_creation": {"ephemeral_1h_input_tokens": 0,
                                        "ephemeral_5m_input_tokens": num(tag + "w", 0, 90_000)},
                     "cache_read_input_tokens": num(tag + "r", 0, 900_000),
                     "claude_tag_category": None, "claude_tag_user_id": None,
                     "context_window": "0-200k", "inference_geo": "global", "model": model,
                     "output_tokens": num(tag + "o", 1_000, 80_000), "product": product,
                     "rbac_group_id": "rbac_group_01Eng", "requests": num(tag + "q", 1, 900),
                     "server_tool_use": {"web_search_requests": 0}, "slack_channel_id": None,
                     "speed": "standard", "uncached_input_tokens": num(tag + "u", 1_000, 500_000)}
                toks += usage_tokens(r)
                results.append(r)
        usage["data"].append({"starting_at": iso(day), "ending_at": iso(next_day(day)),
                              "results": results})
    cost = {"data": [], "has_more": False, "next_page": None,
            "data_refreshed_at": "2026-09-22T12:00:00Z", **org}
    amount_nano = list_nano = lines = 0
    token_fields = (("uncached_input_tokens", lambda r: r["uncached_input_tokens"]),
                    ("output_tokens", lambda r: r["output_tokens"]),
                    ("cache_read_input_tokens", lambda r: r["cache_read_input_tokens"]),
                    ("cache_creation.ephemeral_5m_input_tokens",
                     lambda r: r["cache_creation"]["ephemeral_5m_input_tokens"]))
    acc: dict[tuple, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for bucket in usage["data"]:
        day = bucket["starting_at"][:10]
        results = []
        for u in bucket["results"]:
            for tt, get in token_fields:
                n = get(u)
                if not n:
                    continue
                listed = Decimal(n) * rate_table[u["model"]][tt] / MTOK * 100    # cents
                amount = listed * Decimal("0.8")                               # 20% discount
                results.append({"amount": f"{amount:.6f}", "list_amount": f"{listed:.6f}",
                                "currency": "USD", "cost_type": "tokens", "token_type": tt,
                                "model": u["model"], "product": u["product"],
                                "requests": None, "speed": "standard",
                                "inference_geo": "global", "context_window": "0-200k",
                                "rbac_group_id": None, "slack_channel_id": None,
                                "claude_tag_category": None, "claude_tag_user_id": None})
                key = (day, u["model"], tt)                    # product folds into one line
                acc[key][0] += amount / 100
                acc[key][1] += listed / 100
        results.append({"amount": "41280.000000", "list_amount": "51600.000000",
                        "currency": "USD", "cost_type": "code_execution", "token_type": None,
                        "model": None, "product": "claude_code", "requests": 0, "speed": None,
                        "inference_geo": None, "context_window": None, "rbac_group_id": None,
                        "slack_channel_id": None, "claude_tag_category": None,
                        "claude_tag_user_id": None})
        acc[(day, None, "code_execution")][0] += Decimal("412.8")
        acc[(day, None, "code_execution")][1] += Decimal("516")
        cost["data"].append({"starting_at": bucket["starting_at"],
                             "ending_at": bucket["ending_at"], "results": results})
    for a, lst in acc.values():
        amount_nano += nano(a)
        list_nano += nano(lst)
        lines += 1
    # per-user endpoints (bucket_width 1d): platform 6 users, payments 3 + tiny 1 (dropped)
    users = team_users()
    day = "2026-09-01"
    user_usage = {"data": [], "has_more": False, "next_page": None,
                  "data_refreshed_at": "2026-09-22T12:00:00Z", **org}
    members = [(e, i) for i, e in enumerate(users["platform"])]
    members += [(e, 10 + i) for i, e in enumerate(users["payments"][:3])]
    members += [(users["tiny"][0], 20)]
    user_tokens_published = 0
    for email, i in members:
        tag = f"entu{email}"
        r = {"actor": {"type": "user_actor", "user_id": f"user_{i:04d}Example",
                       "email": email, "name": f"Person {i} {CANARY}", "deleted": False},
             "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                "ephemeral_5m_input_tokens": num(tag + "w", 0, 9_000)},
             "cache_read_input_tokens": num(tag + "r", 0, 90_000),
             "claude_tag_category": None, "claude_tag_user_id": None,
             "context_window": "0-200k", "ending_at": iso(next_day(day)),
             "inference_geo": "global", "model": "claude-opus-5",
             "output_tokens": num(tag + "o", 100, 9_000), "product": "claude_code",
             "rbac_group_id": None, "requests": num(tag + "q", 1, 90),
             "server_tool_use": {"web_search_requests": 0}, "slack_channel_id": None,
             "speed": "standard", "starting_at": iso(day), "total_tokens": 0,
             "uncached_input_tokens": num(tag + "u", 100, 50_000)}
        r["total_tokens"] = usage_tokens(r)
        if teams_of[email] == "platform":
            user_tokens_published += r["total_tokens"]
        user_usage["data"].append(r)
    user_cost = {"data": [], "has_more": False, "next_page": None,
                 "data_refreshed_at": "2026-09-22T12:00:00Z", **org}
    cost_members = [(e, i) for i, e in enumerate(users["platform"][:5])]
    cost_members += [(e, 10 + i) for i, e in enumerate(users["payments"])]
    user_cost_nano = 0
    for email, i in cost_members:
        listed = Decimal(num(f"entuc{email}", 100, 900_000)) / 100
        amount = listed * Decimal("0.8")
        user_cost["data"].append({
            "actor": {"type": "user_actor", "user_id": f"user_{i:04d}Example", "email": email,
                      "name": f"Person {i}", "deleted": False},
            "amount": f"{amount:.6f}", "list_amount": f"{listed:.6f}", "currency": "USD",
            "cost_type": "tokens", "token_type": "output_tokens", "model": "claude-opus-5",
            "product": "claude_code", "requests": 3, "speed": "standard",
            "inference_geo": "global", "context_window": "0-200k", "rbac_group_id": None,
            "slack_channel_id": None, "claude_tag_category": None, "claude_tag_user_id": None,
            "starting_at": iso(day), "ending_at": iso(next_day(day))})
        user_cost_nano += nano(amount / 100)
    return {
        "usage": (usage, {"aggregates": len(days) * 4, "usage_tokens": toks}),
        "cost": (cost, {"cost_lines": lines, "amount_nano": amount_nano,
                        "list_amount_nano": list_nano}),
        "user_usage": (user_usage, {"aggregates": 1, "usage_tokens": user_tokens_published,
                                    "dropped_groups": 2}),
        "user_cost": (user_cost, {"aggregates": 2, "reported_cost_nano": user_cost_nano}),
    }


# ---------------------------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------------------------

OPENAI_RATES = {  # gpt-5.6-sol from 2026-08-21 (facts.json): $4 in, 0.1x read, 1.25x write, $20
    "input": Decimal("4.00"), "cached input": Decimal("0.40"), "cache write": Decimal("5.00"),
    "output": Decimal("20.00")}


def build_openai() -> tuple[dict, dict, dict, dict]:
    """Usage buckets and costs for gpt-5.6-sol; every aggregate stays below the 272K long-context
    threshold (an aggregate is not a request), costs = usage priced at list per line item."""
    days = ("2026-09-01", "2026-09-02")
    usage = {"object": "page", "data": [], "has_more": False, "next_page": None}
    toks = 0
    per: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for day in days:
        results = []
        for project, key in (("proj_01Search", "key_01Search"),
                             ("proj_02Support", "key_02Support")):
            tag = f"oai{day}{project}"
            cached = num(tag + "c", 0, 150_000)
            write = num(tag + "w", 0, 20_000)
            uncached = num(tag + "u", 1_000, 90_000)
            out = num(tag + "o", 1_000, 200_000)
            toks += cached + write + uncached + out
            for item, n in (("input", uncached), ("cached input", cached),
                            ("cache write", write), ("output", out)):
                per[(day, project)][item] += n
            results.append({"object": "organization.usage.completions.result",
                            "input_tokens": cached + write + uncached, "output_tokens": out,
                            "input_cached_tokens": cached, "input_cache_write_tokens": write,
                            "input_uncached_tokens": uncached, "input_audio_tokens": 0,
                            "output_audio_tokens": 0, "num_model_requests": num(tag, 1, 500),
                            "project_id": project, "user_id": None, "api_key_id": key,
                            "model": "gpt-5.6-sol", "batch": False, "service_tier": None})
        # a user-grouped pair (person dim dropped, rows summed) and a non-completions result
        for uid in ("user_01Ann", "user_02Ben"):
            results.append({"object": "organization.usage.completions.result",
                            "input_tokens": 1_000, "output_tokens": 100,
                            "input_cached_tokens": 0, "input_cache_write_tokens": 0,
                            "input_uncached_tokens": 1_000, "num_model_requests": 1,
                            "project_id": "proj_03Lab", "user_id": uid, "api_key_id": None,
                            "model": "gpt-5.6-sol", "batch": False, "service_tier": "default"})
            per[(day, "proj_03Lab")]["input"] += 1_000
            per[(day, "proj_03Lab")]["output"] += 100
            toks += 1_100
        results.append({"object": "organization.usage.embeddings.result", "input_tokens": 999,
                        "num_model_requests": 3, "project_id": None, "user_id": None,
                        "api_key_id": None, "model": "text-embedding-3-small"})
        usage["data"].append({"object": "bucket", "start_time": unix(day),
                              "end_time": unix(next_day(day)), "results": results})
    costs = {"object": "page", "data": [], "has_more": False, "next_page": None}
    amount_nano = lines = 0
    for day in days:
        results = []
        for (d, project), items in sorted(per.items()):
            if d != day:
                continue
            for item, n in sorted(items.items()):
                if not n:
                    continue
                value = Decimal(n) * OPENAI_RATES[item] / MTOK
                results.append({"object": "organization.costs.result",
                                "amount": {"value": value, "currency": "usd"},
                                "line_item": f"gpt-5.6-sol, {item}", "project_id": project,
                                "api_key_id": None, "quantity": None, "quantity_unit": None})
                amount_nano += nano(value)
                lines += 1
        costs["data"].append({"object": "bucket", "start_time": unix(day),
                              "end_time": unix(next_day(day)), "results": results})
    return (usage, {"aggregates": 6, "usage_tokens": toks}, costs,
            {"cost_lines": lines, "amount_nano": amount_nano})


# ---------------------------------------------------------------------------------------------
# AWS CUR 2.0 and GCP billing export
# ---------------------------------------------------------------------------------------------

CUR_COLUMNS = ["identity_line_item_id", "bill_billing_period_start_date",
               "line_item_usage_start_date", "line_item_usage_end_date",
               "line_item_usage_account_id", "line_item_line_item_type",
               "line_item_product_code", "line_item_usage_type", "line_item_operation",
               "line_item_line_item_description", "line_item_usage_amount", "pricing_unit",
               "line_item_currency_code", "line_item_unblended_cost",
               "line_item_net_unblended_cost", "line_item_iam_principal", "tags"]
CUR_TEAM_MAP = {
    "arn:aws:iam::111122223333:role/PaymentsAppRole": "payments",
    "arn:aws:iam::444455556666:role/SearchRankerRole": "search",
}


def build_cur() -> tuple[str, dict]:
    rows = []
    unit_sizes = {"1K tokens": 1_000, "1M tokens": 1_000_000}
    specs = [  # (account, principal, usage type, unit, tokens, usd per token-unit)
        ("111122223333", "arn:aws:sts::111122223333:assumed-role/PaymentsAppRole/i-0abc123",
         "USE1-MP:USE1_InputTokenCount-Units", "1K tokens", 1_234_567, Decimal("0.0055")),
        ("111122223333", "arn:aws:sts::111122223333:assumed-role/PaymentsAppRole/i-0abc123",
         "USE1-MP:USE1_OutputTokenCount-Units", "1M tokens", 98_765, Decimal("27.5")),
        ("111122223333", f"arn:aws:sts::111122223333:assumed-role/DataSciRole/{CANARY_EMAIL}",
         "USE1-MP:USE1_CacheReadInputTokenCount_Global-Units", "1M tokens", 5_500_000,
         Decimal("0.5")),
        ("444455556666", "arn:aws:sts::444455556666:assumed-role/SearchRankerRole/batch-7",
         "USE1-MP:USE1_CacheWriteInputTokenCount-Units", "1K tokens", 250_000, Decimal("0.006875")),
        ("444455556666", "arn:aws:iam::444455556666:user/svc-legacy",
         "USE1-MP:USE1_InputTokenCount_Global-Units", "Units", 42_000, Decimal("5")),
    ]
    tokens_by_unit_known = 0
    amount_nano = list_nano = 0
    cost_keys = set()
    agg_keys = set()
    for day in ("2026-09-01", "2026-09-02"):
        for hour in (9, 17):
            for i, (acct, principal, ut, unit, toks, rate) in enumerate(specs):
                size = unit_sizes.get(unit, 1_000_000)
                amount_units = Decimal(toks) / size
                unblended = (amount_units * rate).quantize(Decimal("0.000000001"))
                net = (unblended * Decimal("0.9")).quantize(Decimal("0.000000001"))
                tags = {"iamPrincipal/team": "ml-research"} if "DataSci" in principal else {}
                rows.append([f"li-{day}-{hour}-{i}", "2026-09-01T00:00:00Z",
                             f"{day}T{hour:02d}:00:00Z", f"{day}T{hour + 1:02d}:00:00Z", acct,
                             "Usage", "AmazonBedrockFoundationModels", ut, "InvokeModel",
                             f"Claude usage {CANARY}", format(amount_units, "f"), unit, "USD",
                             format(unblended, "f"), format(net, "f"), principal,
                             json.dumps(tags, sort_keys=True) if tags else ""])
                amount_nano += nano(net)
                list_nano += nano(unblended)
                cost_keys.add((day, acct, ut, principal))
                if unit in unit_sizes:
                    tokens_by_unit_known += toks
                    agg_keys.add((day, acct, ut, principal))
        # a credit, a tax row, a non-Bedrock row and a marketplace row found by "anthropic"
        rows.append([f"li-{day}-credit", "2026-09-01T00:00:00Z", f"{day}T00:00:00Z",
                     f"{day}T23:59:59Z", "111122223333", "Credit",
                     "AmazonBedrockFoundationModels", "USE1-MP:USE1_InputTokenCount-Units",
                     "InvokeModel", "Promotional credit", "0", "1K tokens", "USD", "-1.25",
                     "-1.25", "", ""])
        amount_nano += nano(Decimal("-1.25"))
        list_nano += nano(Decimal("-1.25"))
        cost_keys.add((day, "credit"))
        rows.append([f"li-{day}-tax", "2026-09-01T00:00:00Z", f"{day}T00:00:00Z",
                     f"{day}T23:59:59Z", "111122223333", "Tax", "AmazonBedrockFoundationModels",
                     "USE1-MP:USE1_InputTokenCount-Units", "", "Tax", "0", "", "USD", "3.10",
                     "3.10", "", ""])
        rows.append([f"li-{day}-ec2", "2026-09-01T00:00:00Z", f"{day}T00:00:00Z",
                     f"{day}T23:59:59Z", "111122223333", "Usage", "AmazonEC2",
                     "USE1-BoxUsage:m7i.large", "RunInstances", "m7i.large", "24", "Hrs", "USD",
                     "2.4192", "2.4192", "", ""])
        rows.append([f"li-{day}-mp", "2026-09-01T00:00:00Z", f"{day}T00:00:00Z",
                     f"{day}T23:59:59Z", "444455556666", "Usage", "AWSMarketplace",
                     "USE1-anthropic.claude-opus-5-output-tokens", "InvokeModel",
                     "Marketplace", "12.5", "1K tokens", "USD", "0.3125", "0.3125", "", ""])
        amount_nano += nano(Decimal("0.3125"))
        list_nano += nano(Decimal("0.3125"))
        tokens_by_unit_known += 12_500
        cost_keys.add((day, "mp"))
        agg_keys.add((day, "mp"))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CUR_COLUMNS)
    writer.writerows(rows)
    expect = {"cost_lines": len(cost_keys), "amount_nano": amount_nano,
              "list_amount_nano": list_nano, "usage_tokens": tokens_by_unit_known,
              "aggregates": 10, "rows_skipped_not_bedrock": 2, "rows_skipped_tax": 2,
              "rows_unit_unknown": 4}
    return buf.getvalue(), expect


GCP_CSV_COLUMNS = ["billing_account_id", "service.description", "sku.id", "sku.description",
                   "usage_start_time", "usage_end_time", "project.id", "project.name", "labels",
                   "location.region", "location.location", "cost", "currency", "usage.amount",
                   "usage.unit", "credits", "cost_type"]


def build_gcp() -> tuple[list[dict], str, dict]:
    rows = []
    specs = [  # (sku id, description, region, team label, tokens, usd per token)
        ("A1B2-C3D4-0001", "Claude Opus 5 Input Tokens", "global", "search", 2_000_000,
         Decimal("0.000005")),
        ("A1B2-C3D4-0002", "Claude Opus 5 Output Tokens", "us-east5", "search", 150_000,
         Decimal("0.0000275")),
        ("A1B2-C3D4-0003", "Claude Sonnet 5 Cache Read Tokens", "europe-west1", "data",
         9_000_000, Decimal("0.00000022")),
    ]
    amount_nano = list_nano = 0
    tokens_total = 0
    for day in ("2026-09-01", "2026-09-02"):
        for i, (sku, desc, region, team, toks, rate) in enumerate(specs):
            cost = Decimal(toks) * rate
            credit = -(cost * Decimal("0.05")).quantize(Decimal("0.000001"))
            rows.append({
                "billing_account_id": "0X0X0X-0X0X0X-0X0X0X",
                "service": {"id": "C7E2-9256-1C43", "description": "Vertex AI"},
                "sku": {"id": sku, "description": desc},
                "usage_start_time": f"{day} 0{i}:00:00 UTC",
                "usage_end_time": f"{day} 0{i + 1}:00:00 UTC",
                "project": {"id": "acme-ml-prod", "name": f"ACME ML {CANARY}"},
                "labels": [{"key": "team", "value": team},
                           {"key": "prompt_label", "value": CANARY}],
                "location": {"region": region, "location": region},
                "cost": cost, "currency": "USD",
                "usage": {"amount": toks, "unit": "tokens"},
                "credits": [{"name": "Committed use discount", "amount": credit,
                             "type": "DISCOUNT"}],
                "cost_type": "regular",
            })
            amount_nano += nano(cost + credit)
            list_nano += nano(cost)
            tokens_total += toks
        # no location (scope unknown), unit "count" (tokens not derived)
        rows.append({
            "billing_account_id": "0X0X0X-0X0X0X-0X0X0X",
            "service": {"id": "C7E2-9256-1C43", "description": "Vertex AI"},
            "sku": {"id": "A1B2-C3D4-0009", "description": "Claude Haiku 4.5 Input Tokens"},
            "usage_start_time": f"{day} 12:00:00 UTC", "usage_end_time": f"{day} 13:00:00 UTC",
            "project": {"id": "acme-ml-dev", "name": "dev"}, "labels": [],
            "location": {"region": None, "location": None}, "cost": Decimal("0.5"),
            "currency": "USD", "usage": {"amount": 500_000, "unit": "count"}, "credits": [],
            "cost_type": "regular"})
        amount_nano += nano(Decimal("0.5"))
        list_nano += nano(Decimal("0.5"))
        # a Gemini row (not Claude) and a tax row
        rows.append({
            "billing_account_id": "0X0X0X-0X0X0X-0X0X0X",
            "service": {"id": "C7E2-9256-1C43", "description": "Vertex AI"},
            "sku": {"id": "FFFF-0000-1111", "description": "Gemini 3 Flash Input Tokens"},
            "usage_start_time": f"{day} 12:00:00 UTC", "usage_end_time": f"{day} 13:00:00 UTC",
            "project": {"id": "acme-ml-prod", "name": "x"}, "labels": [],
            "location": {"region": "us-central1", "location": "us-central1"},
            "cost": Decimal("1.0"), "currency": "USD", "usage": {"amount": 1, "unit": "tokens"},
            "credits": [], "cost_type": "regular"})
        rows.append({
            "billing_account_id": "0X0X0X-0X0X0X-0X0X0X",
            "service": {"id": "C7E2-9256-1C43", "description": "Vertex AI"},
            "sku": {"id": "A1B2-C3D4-0001", "description": "Claude Opus 5 Input Tokens"},
            "usage_start_time": f"{day} 00:00:00 UTC", "usage_end_time": f"{day} 23:59:59 UTC",
            "project": {"id": "acme-ml-prod", "name": "x"}, "labels": [],
            "location": {"region": "global", "location": "global"}, "cost": Decimal("0.77"),
            "currency": "USD", "usage": {"amount": 0, "unit": "tokens"}, "credits": [],
            "cost_type": "tax"})
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(GCP_CSV_COLUMNS)
    for r in rows:
        writer.writerow([
            r["billing_account_id"], r["service"]["description"], r["sku"]["id"],
            r["sku"]["description"], r["usage_start_time"], r["usage_end_time"],
            r["project"]["id"], r["project"]["name"],
            json.dumps(r["labels"], sort_keys=True), r["location"]["region"] or "",
            r["location"]["location"] or "", str(r["cost"]), r["currency"],
            str(r["usage"]["amount"]), r["usage"]["unit"],
            json.dumps([{**c, "amount": str(c["amount"])} for c in r["credits"]],
                       sort_keys=True), r["cost_type"]])
    expect = {"amount_nano": amount_nano, "list_amount_nano": list_nano,
              "usage_tokens": tokens_total, "rows_skipped_not_claude": 2, "rows_skipped_tax": 2,
              "cost_lines": 8, "aggregates": 6, "rows_unit_unknown": 2}
    return rows, buf.getvalue(), expect


def gcp_jsonl(rows: list[dict]) -> str:
    """JSONL with JSON numbers (never quoted) for money, as BigQuery exports it."""
    return "".join(_dumps_decimal(r) + "\n" for r in rows)


def _dumps_decimal(obj: object) -> str:
    """Compact sorted JSON writing ``Decimal`` values as exact JSON numbers."""
    if isinstance(obj, dict):
        return "{" + ",".join(f"{json.dumps(k)}:{_dumps_decimal(v)}"
                              for k, v in sorted(obj.items())) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(_dumps_decimal(v) for v in obj) + "]"
    if isinstance(obj, Decimal):
        return format(obj, "f")
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------

def main() -> None:
    r = rates()
    files = []

    def entry(path: str, adapter: str, role: str, doc: str, expect: dict, **extra: object) -> None:
        files.append({"path": path, "adapter": adapter, "role": role,
                      "provenance": f"synthetic; shape from {DOCS[doc]} (retrieved 2026-09-23)",
                      "expect": expect, **extra})

    usage, cost, exp = build_recon_pair(r)
    dump_json(HERE / "anthropic" / "usage_report_2026-08.json", usage)
    dump_json(HERE / "anthropic" / "cost_report_2026-08.json", cost)
    entry("anthropic/usage_report_2026-08.json", "anthropic-usage-report", "recon_pair", "usage",
          {"aggregates": exp["usage_aggregates"], "usage_tokens": exp["usage_tokens"],
           "finality": "final"})
    entry("anthropic/cost_report_2026-08.json", "anthropic-cost-report", "recon_pair", "cost",
          {"cost_lines": exp["cost_lines"], "amount_nano": exp["amount_nano"],
           "rounding_remainder_e18": 0, "finality": "final"})
    pages, exp2 = build_usage_pages_jsonl()
    dump_jsonl(HERE / "anthropic" / "usage_report_2026-09_pages.jsonl", pages)
    entry("anthropic/usage_report_2026-09_pages.jsonl", "anthropic-usage-report", "pagination",
          "usage", {"aggregates": exp2["usage_aggregates"], "usage_tokens": exp2["usage_tokens"],
                    "finality": "provisional"})
    eu, ec, exp3 = build_edge_pair()
    dump_json(HERE / "anthropic" / "edge" / "usage_report_edge.json", eu)
    dump_json(HERE / "anthropic" / "edge" / "cost_report_edge.json", ec)
    entry("anthropic/edge/usage_report_edge.json", "anthropic-usage-report", "edge", "usage",
          {"aggregates": exp3["usage_aggregates"], "usage_tokens": exp3["usage_tokens"]})
    entry("anthropic/edge/cost_report_edge.json", "anthropic-cost-report", "edge", "cost",
          {"cost_lines": exp3["cost_lines"], "amount_nano": exp3["amount_nano"],
           "rounding_remainder_e18": exp3["rounding_remainder_e18"]})

    page1, pages2, cc1, cc2, teams_of = build_cc()
    dump_json(HERE / "anthropic" / "cc_analytics_2026-09-10.json", page1)
    dump_jsonl(HERE / "anthropic" / "cc_analytics_2026-09-11_pages.jsonl", pages2)
    entry("anthropic/cc_analytics_2026-09-10.json", "anthropic-cc-analytics", "k_anonymity",
          "cc", {"outcomes": cc1["outcomes"], "aggregates": cc1["aggregates"],
                 "usage_tokens": cc1["tokens"], "reported_cost_nano": cc1["cost_nano"],
                 "dropped_groups": cc1["dropped_groups"]})
    entry("anthropic/cc_analytics_2026-09-11_pages.jsonl", "anthropic-cc-analytics",
          "k_anonymity", "cc",
          {"outcomes": cc2["outcomes"], "aggregates": cc2["aggregates"],
           "usage_tokens": cc2["tokens"], "reported_cost_nano": cc2["cost_nano"],
           "dropped_groups": cc2["dropped_groups"]})

    ent = build_enterprise(teams_of, r)
    for name, (page, exp_e) in ent.items():
        rel = f"anthropic/enterprise/{name}_report.json"
        dump_json(HERE / rel, page)
        entry(rel, "anthropic-enterprise-analytics", "enterprise", "ent", exp_e)

    ou, oue, oc, oce = build_openai()
    dump_json(HERE / "openai" / "usage_completions_2026-09.json", ou)
    (HERE / "openai" / "costs_2026-09.json").write_text(
        _dumps_decimal(oc) + "\n", encoding="utf-8")
    entry("openai/usage_completions_2026-09.json", "openai-usage-buckets", "openai", "openai",
          oue)
    entry("openai/costs_2026-09.json", "openai-costs", "openai", "openai", oce)

    cur_csv, cur_exp = build_cur()
    (HERE / "cloud").mkdir(parents=True, exist_ok=True)
    (HERE / "cloud" / "cur2_bedrock_2026-09.csv").write_text(cur_csv, encoding="utf-8")
    write_gz(HERE / "cloud" / "cur2_bedrock_2026-09.csv.gz", cur_csv.encode("utf-8"))
    for rel in ("cloud/cur2_bedrock_2026-09.csv", "cloud/cur2_bedrock_2026-09.csv.gz"):
        entry(rel, "aws-cur", "cloud", "cur", cur_exp)
    gcp_rows, gcp_csv, gcp_exp = build_gcp()
    (HERE / "cloud" / "gcp_billing_2026-09.jsonl").write_text(gcp_jsonl(gcp_rows),
                                                              encoding="utf-8")
    (HERE / "cloud" / "gcp_billing_2026-09.csv").write_text(gcp_csv, encoding="utf-8")
    for rel in ("cloud/gcp_billing_2026-09.jsonl", "cloud/gcp_billing_2026-09.csv"):
        entry(rel, "gcp-billing", "cloud", "gcp", gcp_exp)

    team_map = dict(sorted({**teams_of, **CUR_TEAM_MAP}.items()))
    manifest = {
        "schema": "tokenbill/admin-fixtures@1",
        "generated_by": "tests/v2/fixtures/admin/build_fixtures.py",
        "synthetic": True,
        "options": {"now": NOW, "now_ms": unix(NOW) * 1000, "k_anonymity": 5,
                    "team_map": team_map, "identity_mode": "central-ingest"},
        "recon_pairs": [{
            "channel": "anthropic_api", "usage": "anthropic/usage_report_2026-08.json",
            "cost": "anthropic/cost_report_2026-08.json", "basis": "list",
            "dates": list(RECON_DAYS),
            "note": "cost = usage priced at list with tokenbill/core/facts.json rates "
                    "(standard tier, global/not_available geo, standard speed); every amount is "
                    "nano-exact; closed dates relative to options.now",
        }],
        "files": files,
    }
    dump_json(HERE / "MANIFEST.json", manifest)


if __name__ == "__main__":
    main()
