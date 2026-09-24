"""The addendum §18 seat and budget plants on a builder-made world shaped like CP-SYNTH's (window
2026-07-03 … 09-22, today 2026-09-23; org A ``assign_selected``, org B ``assign_all``; platform on
Enterprise seats with 6 idle and 4 light users, infra team-assigned with 5 idle, ops in org B with 5
idle; five $0 user budgets in platform) — the same enrichment and assertions as the CP-SYNTH gate
test, so the gate's path is exercised before CP-SYNTH exists."""

from __future__ import annotations

from types import SimpleNamespace

from tokenbill.core import builders as b

from .helpers import (
    budget,
    detect,
    dims,
    evidence,
    of_kind,
    org_settings,
    people,
    rows,
    seats,
)
from .test_gate_synth import _assert_plants, _canonical_ctx

SNAP = "2026-09-22"


def _world() -> SimpleNamespace:
    lics = (seats(6, plan="enterprise", team="platform", seed="pi", date=SNAP)
            + seats(4, plan="enterprise", team="platform", seed="pl", date=SNAP, bucket="0-7")
            + seats(10, plan="enterprise", team="platform", seed="ph", date=SNAP, bucket="0-7")
            + seats(5, team="infra", seed="ii", date=SNAP, via_team=True)
            + seats(5, team="infra", seed="ia", date=SNAP, via_team=True, bucket="0-7")
            + seats(5, team="ops", org="org-b", seed="oi", date=SNAP)
            + seats(3, team="ops", org="org-b", seed="oa", date=SNAP, bucket="0-7"))
    cost, aggs = [], []
    for month, day in (("07", "15"), ("08", "14"), ("09", "10")):
        date = f"2026-{month}-{day}"
        cost += [b.make_seat_line("enterprise", "20", date_utc=f"2026-{month}-03"),
                 b.make_seat_line("business", "10", date_utc=f"2026-{month}-03"),
                 b.make_seat_line("business", "8", date_utc=f"2026-{month}-03",
                                  organization="org-b")]
        for users, credits, org in ((people(4, "pl"), 800, "org-a"),
                                    (people(10, "ph"), 3_000, "org-a"),
                                    (people(5, "ia"), 500, "org-a"),
                                    (people(3, "oa"), 500, "org-b")):
            for u in users:
                ls, ag = rows(credits, date=date, users=[u], org=org, discount=credits)
                cost += ls
                aggs += ag
    conf = [org_settings("org-a", "assign_selected", date="2026-07-03"),
            org_settings("org-b", "assign_all", date="2026-07-03"),
            *[budget(f"z{i}", "user", 0, stop=True, team="platform") for i in range(5)],
            budget("ent", "enterprise", 2_000, stop=False)]
    records = SimpleNamespace(cost_lines=cost, aggregates=aggs, licenses=lics, config=conf,
                              activity=[])
    return SimpleNamespace(records=records, truth=SimpleNamespace(), today="2026-09-23")


def test_plant_table_recovered() -> None:
    world = _world()
    found = detect(_canonical_ctx(world))
    _assert_plants(found, world)
    [status] = of_kind(found, "plan-status")
    assert evidence(status, "plan")["plan"] == "mixed"
    [mix] = of_kind(found, "plan-mix")
    assert "fewer than 3 closed months available" in mix.summary
    [ops] = [f for f in of_kind(found, "idle-seat") if dims(f).get("team") == "ops"]
    assert evidence(ops, "assignment:auto")["n"] == 5 and ops.recoverable is None
    assert not [f for f in found if f.kind.startswith("budget-zero")
                and dims(f).get("team") != "platform"]
