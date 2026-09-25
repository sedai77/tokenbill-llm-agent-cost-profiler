"""Area-local builders for the F-POOL acceptance worlds (addendum Appendix C.P1–P15 and the F-POOL
brief's P14b / P15b). Every record is synthetic and built with ``core.builders``."""

from __future__ import annotations

import datetime as dt

from tokenbill.core import builders as b
from tokenbill.core.records import ConfigSnapshot, CostLine, LicenseSnapshot, UsageAggregate

#: nano-USD per AI credit ($0.01).
C = 10**7
#: nano-USD per USD.
USD = 10**9
DAY_MS = 86_400_000


def ms(date: str) -> int:
    """Epoch milliseconds of a UTC date (the start of the day, plus one hour)."""
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * DAY_MS + 3_600_000


def people(n: int, seed: str = "u") -> list[str]:
    return [b.make_principal(f"{seed}{i}") for i in range(n)]


def rows(credits: int, *, date: str, users: list[str] | None = None, discount: int = 0,
         model: str = "Claude Sonnet 5", org: str | None = "org-a",
         cost_center: str | None = None, team: str | None = None, finality: str = "final",
         unattributed: bool = False, tokens: int = 0,
         ) -> tuple[list[CostLine], list[UsageAggregate]]:
    """AI usage report rows totalling *credits* (and *discount*) spread over *users* (one row each;
    the first user takes the remainder); unattributed rows have no principal."""
    who: list[str | None] = [None] if unattributed else list(users or people(1))
    lines: list[CostLine] = []
    aggs: list[UsageAggregate] = []
    n = len(who)
    for i, principal in enumerate(who):
        share = credits // n + (credits % n if i == 0 else 0)
        disc = discount // n + (discount % n if i == 0 else 0)
        line, agg = b.make_ai_usage_row(
            date_utc=date, model=model, credits=str(share), discount_credits=str(disc),
            principal=principal, unattributed=unattributed, organization=org,
            cost_center=cost_center, team=team, finality=finality,
            input_tokens=tokens, output_tokens=tokens // 10)
        lines.append(line)
        aggs.append(agg)
    return lines, aggs


def activity_seats(n: int, *, date: str = "2026-10-15", org: str | None = "org-a",
                   seed: str = "u") -> list[LicenseSnapshot]:
    """*n* activity-report seat holders (plan unknown, assignment unknown)."""
    return [b.make_license(p, snapshot_date=date, plan="unknown", org=org,
                           assigned_via_team=None,
                           source_kind="github.copilot_activity_report")
            for p in people(n, seed)]


def api_seats(n: int, plan: str, *, date: str = "2026-10-05", org: str | None = "org-a",
              seed: str = "u", cost_center: str | None = None) -> list[LicenseSnapshot]:
    """*n* seats-API seats of *plan*."""
    return [b.make_license(p, snapshot_date=date, plan=plan, org=org, cost_center=cost_center)
            for p in people(n, seed)]


def flags(pairs: dict[str, str | int | bool | None], entity: str = "run",
          snapshot_ms: int = 0) -> ConfigSnapshot:
    """A ``run_flags`` snapshot of the CLI (entity ``run``) or of the admin answers."""
    source = "tokenbill.admin_answers" if entity == "admin_answers" else "tokenbill.cli"
    return b.make_config("run_flags", pairs, entity_id=entity, source_kind=source,
                         snapshot_ms=snapshot_ms)


def quota(month: str, value: int, n_users: int, *, org: str = "org-a") -> ConfigSnapshot:
    """A ``plan_quota`` row as CP-BILL emits it under ``copilot-report-quota``."""
    return b.make_config("plan_quota", {"month": month, "quota": str(value), "n_users": n_users},
                         entity_id=f"org:{org}", source_kind="github.ai_usage_report",
                         snapshot_ms=ms(f"{month}-28"))


def org_settings(org: str, plan_type: str, *, date: str = "2026-10-02",
                 source_kind: str = "github.org_copilot_settings") -> ConfigSnapshot:
    return b.make_config("org_settings", {"plan_type": plan_type,
                                          "seat_management_setting": "assign_selected"},
                         entity_id=f"org:{org}", source_kind=source_kind, snapshot_ms=ms(date))


def cost_center(name: str, cap: str | None, *, enabled: bool = True,
                date: str = "2026-10-02") -> ConfigSnapshot:
    attrs: dict[str, str | int | bool | None] = {"pool_enabled": enabled}
    if cap is not None:
        attrs["pool_target_credits"] = cap
    return b.make_config("cost_center", attrs, entity_id=f"cc:{name}",
                         source_kind="github.cost_centers", snapshot_ms=ms(date))


def seat_count(org: str, plan: str, n: int, *, date: str = "2026-10-20", team: str = "t1",
               bucket: str = "0-7") -> ConfigSnapshot:
    return b.make_config("seat_counts", {"team": team, "plan": plan, "bucket": bucket, "n": n},
                         entity_id=f"org:{org}", source_kind="tokenbill.copilot_export",
                         snapshot_ms=ms(date))


def daily(month_days: dict[str, int], *, users: list[str] | None = None,
          cutoff: str | None = None) -> tuple[list[CostLine], list[UsageAggregate]]:
    """One pooled row per date (credits per day); rows after *cutoff* are provisional."""
    lines: list[CostLine] = []
    aggs: list[UsageAggregate] = []
    for date, credits in sorted(month_days.items()):
        fin = "provisional" if cutoff is not None and date > cutoff else "final"
        ls, ag = rows(credits, date=date, users=users, finality=fin)
        lines += ls
        aggs += ag
    return lines, aggs


def p9_series() -> dict[str, int]:
    """The binding C.P9 series (credits per day, 2026-09-01 … 20): business days 1–4 at 130,000,
    the last 10 business days 110k … 150k, the weekend Sep 5–6 at 30,000 and the last 4 weekend days
    20k / 30k / 30k / 40k — 2,000,000 credits."""
    series = {f"2026-09-{d:02d}": 130_000 for d in (1, 2, 3, 4)}
    last10 = [110_000, 110_000, 120_000, 120_000, 130_000, 130_000, 140_000, 140_000, 150_000,
              150_000]
    for d, v in zip((7, 8, 9, 10, 11, 14, 15, 16, 17, 18), last10, strict=True):
        series[f"2026-09-{d:02d}"] = v
    series.update({"2026-09-05": 30_000, "2026-09-06": 30_000, "2026-09-12": 20_000,
                   "2026-09-13": 30_000, "2026-09-19": 30_000, "2026-09-20": 40_000})
    return series
