"""OUT on store-produced data (``MemoryStore``, the foundation fake): FOCUS totals equal the
store's ``cost_rows`` sums; showback from ``aggregate`` + ``core.kanon.publish``; the content canary
never reaches any renderer output."""

from __future__ import annotations

import io
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from tokenbill.core.builders import assert_no_canary
from tokenbill.core.kanon import publish
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.finops.allocation import coverage
from tokenbill.outputs.focus import write_focus
from tokenbill.outputs.html import render_html
from tokenbill.outputs.result_json import dumps_result
from tokenbill.outputs.sarif import to_sarif
from tokenbill.outputs.showback import render_showback
from tokenbill.outputs.terminal import render_terminal

from .ledger import GROUP_BY, ORG_KEY, SINCE_MS, UNTIL_MS, ingest_result
from .sample import check, full_result, source


def store() -> MemoryStore:
    s = MemoryStore(org_key=ORG_KEY, pricer=FakePricer())
    s.ingest(ingest_result())
    return s


def focus_totals_match(s) -> None:
    rows = s.cost_rows(since_ms=SINCE_MS, until_ms=UNTIL_MS, group_by=GROUP_BY)
    assert rows
    out = io.StringIO()
    write_focus(rows, out, reconciled_channels=frozenset({"anthropic_api", "bedrock"}),
                allow_unreconciled=False, rate_card_sha="ab" * 32)
    import csv

    data = list(csv.DictReader(io.StringIO(out.getvalue())))
    want: dict[tuple, int] = defaultdict(int)
    for r in rows:
        want[(r.date_utc, r.basis.value)] += r.priced_nano
    got: dict[tuple, int] = defaultdict(int)
    for r in data:
        got[(r["ChargePeriodStart"][:10], r["x_PriceBasis"])] += int(
            Decimal(r["ListCost"]) * 10**9)
        if r["x_PriceBasis"] == "list_equivalent":
            assert r["BilledCost"] == "0"
    assert got == want
    teams = {json.loads(r["Tags"]).get("team") for r in data}
    assert "tiny" not in teams and "mini" not in teams      # below k = 5: merged
    assert "payments" in teams


def showback_from_aggregate(s, tmp_path: Path) -> None:
    raw = s.aggregate(since_ms=SINCE_MS, until_ms=UNTIL_MS, group_by=("team",))
    pub = publish(raw, k=5)
    days = s.cluster_days(cluster_kind="team", since="2026-09-22", until="2026-09-24")
    paths = publish(s.aggregate(since_ms=SINCE_MS, until_ms=UNTIL_MS,
                                group_by=("billing_path", "team")), k=5)
    written = render_showback(pub, days, [], None, tmp_path, formats=("html", "csv", "json"),
                              billing_paths=paths)
    doc = json.loads((tmp_path / "showback.json").read_text(encoding="utf-8"))
    names = {t["team"] for t in doc["teams"]}
    assert "tiny" not in names and "mini" not in names
    total = sum(int(t["exact"]["nano"]) for t in doc["teams"] if t["exact"])
    assert total == sum(r.priced.exact.nano or 0 for r in pub.rows)
    assert_no_canary(*(p.read_bytes() for p in written))


def test_focus_totals_equal_memory_store_cost_rows() -> None:
    focus_totals_match(store())


def test_showback_from_memory_store_aggregate(tmp_path: Path) -> None:
    showback_from_aggregate(store(), tmp_path)


def test_allocation_coverage_of_store_rows() -> None:
    rows = store().cost_rows(since_ms=SINCE_MS, until_ms=UNTIL_MS, group_by=GROUP_BY)
    assert coverage(rows) == "1"                         # every request has a team


def test_canary_is_absent_from_every_renderer_output(tmp_path: Path) -> None:
    r = full_result(inputs=((source("canary"), 1, 0),))
    blobs: list[str | bytes] = [dumps_result(r), dumps_result(r, deterministic=True),
                                render_terminal(r), render_html(r),
                                json.dumps(to_sarif(check(), tool_version="0.2.0"))]
    s = store()
    out = io.StringIO()
    write_focus(s.cost_rows(since_ms=SINCE_MS, until_ms=UNTIL_MS, group_by=GROUP_BY), out,
                reconciled_channels=frozenset({"anthropic_api", "bedrock"}),
                allow_unreconciled=False, rate_card_sha="ab" * 32)
    blobs.append(out.getvalue())
    assert_no_canary(*blobs)
    showback_from_aggregate(s, tmp_path)
