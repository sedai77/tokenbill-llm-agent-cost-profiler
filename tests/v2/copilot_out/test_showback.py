"""Copilot showback (addendum §14.2): published aggregates only, pro-rata overage, editor split."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tokenbill.copilot.showback import (
    ALLOCATED_METHOD_ID,
    NOT_ATTRIBUTABLE,
    OTHER_TEAMS,
    PALETTES,
    allocate_overage,
    render_copilot_showback,
)
from tokenbill.core.builders import CANARY
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis, estimated, exact
from tokenbill.core.types import Finding, Scope

from .checks import html_violations, json_violations
from .worlds import p1_world, p13_world, result_of

FORMATS = ("html", "csv", "json")


def _write(result, tmp_path: Path) -> dict[str, str]:
    paths = render_copilot_showback(result, tmp_path, formats=FORMATS)
    return {p.suffix[1:]: p.read_text(encoding="utf-8") for p in paths}


def test_team_of_three_is_merged_and_never_named(tmp_path: Path) -> None:
    out = _write(result_of(p13_world().summary()), tmp_path)
    for text in out.values():
        assert "gamma" not in text and "delta" not in text
    assert OTHER_TEAMS in out["html"] and "alpha" in out["html"] and "beta" in out["html"]


def test_pro_rata_allocation_sums_to_the_entity_overage_per_scenario() -> None:
    s = p13_world().summary()
    alloc = allocate_overage(s)
    assert set(alloc) == {("enterprise", "2026-09", "business"),
                          ("enterprise", "2026-09", "enterprise")}
    for pm in s.pools:
        parts = alloc[(pm.entity_id, pm.month, pm.plan_scenario)]
        assert sum(parts.values()) == pm.overage_observed_nano
    business = alloc[("enterprise", "2026-09", "business")]
    assert business["alpha"] == 600 * 10**9 * 58 // 100
    assert sum(alloc[("enterprise", "2026-09", "enterprise")].values()) == 0


def test_known_plan_allocation_and_remainder_to_the_nano() -> None:
    s = p1_world().summary()
    alloc = allocate_overage(s)[("enterprise", "2026-09", "known")]
    assert sum(alloc.values()) == 4_200 * 10**9
    assert alloc[NOT_ATTRIBUTABLE] == 0
    odd = replace(s.pools[0], overage_observed_nano=4_200 * 10**9 + 1,
                  consumed_report_nano=s.pools[0].consumed_report_nano + 7)
    alloc2 = allocate_overage(replace(s, pools=(odd,)))[("enterprise", "2026-09", "known")]
    assert sum(alloc2.values()) == 4_200 * 10**9 + 1 and alloc2[NOT_ATTRIBUTABLE] >= 0


def test_allocation_without_teams_goes_to_not_attributable() -> None:
    s = p1_world().summary()
    alloc = allocate_overage(replace(s, teams=None))[("enterprise", "2026-09", "known")]
    assert alloc == {NOT_ATTRIBUTABLE: 4_200 * 10**9}


def test_showback_measures_labels_and_formats(tmp_path: Path) -> None:
    s = p1_world().summary()
    finding = Finding(
        finding_id="f-alpha", detector_id="copilot.org-scan", kind="model-mix",
        detector_version="1", category="aggregate", lever_class="rate", audience="org",
        title="Premium model mix", summary="s",
        scope=Scope(dims=(("product", "copilot"), ("team", "alpha"))), n_events=1, n_lanes=0,
        n_users=11, first_seen_ms=0, cost_observed=exact(10**9, Basis.LIST_EQUIVALENT),
        recoverable=estimated(10**9, Basis.LIST, note="x"))
    out = _write(replace(result_of(s), findings=(finding,)), tmp_path)
    assert html_violations(out["html"]) == []
    assert 'http-equiv="Content-Security-Policy"' in out["html"]
    assert "Premium model mix" in out["html"]
    doc = json.loads(out["json"])
    assert json_violations(doc) == []
    assert doc["allocated_method_id"] == ALLOCATED_METHOD_ID
    alpha = next(t for t in doc["teams"] if t["team"] == "alpha")
    measures = {m["measure"]: m for m in alpha["measures"]}
    assert measures["pooled credits"]["amount"]["basis"] == "list_equivalent"
    assert measures["GitHub's net of the team's rows"]["amount"]["basis"] == "invoice"
    over = measures["overage allocated"]["amount"]
    assert over["evidence"] == "estimated" and ALLOCATED_METHOD_ID in over["note"]
    assert "interactive credits per active developer-month p50" in measures
    assert alpha["findings"] == ["f-alpha"]
    rows = list(csv.DictReader(io.StringIO(out["csv"])))
    assert {r["team"] for r in rows} >= {"alpha", "beta", "gamma"}
    assert all("." not in r["amount_usd"] or r["amount_usd"].replace(".", "").isdigit()
               for r in rows if r["amount_usd"])


def test_editor_split_rows_and_scenario_measures(tmp_path: Path) -> None:
    out = _write(result_of(p13_world().summary()), tmp_path)
    doc = json.loads(out["json"])
    beta = next(t for t in doc["teams"] if t["team"] == "beta")
    assert {e["editor"] for e in beta["editors"]} == {"vscode", "jetbrains"}
    scen = {(m["measure"], m["scenario"]) for m in beta["measures"]}
    assert ("overage allocated", "business") in scen and ("overage allocated", "enterprise") in scen
    assert ("share of the entity pool", "business") in scen
    assert "Plan unknown" in out["html"]
    other = next(t for t in doc["teams"] if t["team"] == OTHER_TEAMS)
    assert other["editors"][0]["editor"] == "other"


def test_canary_never_in_showback(tmp_path: Path) -> None:
    w = p1_world()
    w.lines = [replace(c, description=f"{c.description} {CANARY}") for c in w.lines]
    out = _write(result_of(w.summary()), tmp_path)
    assert all(CANARY not in text for text in out.values())


def test_no_copilot_data_writes_nothing_and_bad_input_raises(tmp_path: Path) -> None:
    assert render_copilot_showback(result_of(None), tmp_path) == []
    with pytest.raises(UsageError):
        render_copilot_showback(result_of(None), tmp_path, formats=("pdf",))
    with pytest.raises(ContractViolation):
        render_copilot_showback("x", tmp_path)  # type: ignore[arg-type]
    s = p1_world().summary()
    bad = replace(s, teams=replace(s.editor_split, group_by=("team",)) if s.editor_split
                  else p13_world().summary().editor_split)
    with pytest.raises(ContractViolation):
        render_copilot_showback(result_of(bad), tmp_path)
    paths = render_copilot_showback(result_of(s), tmp_path, formats="csv")
    assert [p.name for p in paths] == ["copilot-showback.csv"]


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_palette_meets_wcag_aa(theme: str) -> None:
    p = PALETTES[theme]
    for surface in p["surface"].values():
        for text in p["text"].values():
            assert _contrast(text, surface) >= 4.5
    for mark in p["marks"].values():
        assert _contrast(mark, p["surface"]["bg"]) >= 3.0
