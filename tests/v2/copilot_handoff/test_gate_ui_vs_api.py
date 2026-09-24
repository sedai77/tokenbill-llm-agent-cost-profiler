"""Gate (merge gate 1): the CP-SYNTH world written once as API recordings and once as UI downloads
gives two bundles with equal records after dropping source ids — the UI path lacks only what the
UI lacks (seat assignment kind, org settings, per-seat plan) — and both pass the leak gate.

Needs CP-SYNTH (``generate``), CP-SYNTH-W (``write_world``), CP-BILL and CP-ORGDATA; skipped until
they are merged (SPEC §21 #4).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot.handoff import export_from_files, read_bundle
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.records import to_json

from .helpers import KEY, NOW_MS

pytestmark = pytest.mark.gate


def _modules() -> tuple[Any, Any]:
    world_mod = pytest.importorskip("tokenbill.synth.copilot_world")
    writers = pytest.importorskip("tokenbill.synth.copilot_writers")
    for name in ("tokenbill.adapters.github_billing", "tokenbill.adapters.github_config",
                 "tokenbill.adapters.github_metrics", "tokenbill.adapters.github_seats"):
        pytest.importorskip(name)
    return world_mod, writers


def _team_map(world: Any) -> dict[str, str]:
    for attr in ("team_map", "login_teams", "teams_by_login"):
        value = getattr(world, attr, None)
        if isinstance(value, dict) and value:
            return {str(k): str(v) for k, v in value.items()}
    pytest.skip("the CP-SYNTH world exposes no login → team map")


def _files(written: Any, root: Path) -> list[Path]:
    if isinstance(written, dict):
        return sorted(Path(p) for p in written.values() if Path(p).is_file())
    return [root]


def _canon(records: list, drop: tuple[str, ...] = ()) -> list[str]:
    out = []
    for rec in records:
        doc = to_json(rec)
        for key in drop:
            doc.pop(key, None)
        out.append(json.dumps(doc, sort_keys=True))
    return sorted(out)


def test_ui_and_api_paths_give_equal_bundles(tmp_path: Path) -> None:
    world_mod, writers = _modules()
    world = world_mod.generate()
    team_map = _team_map(world)
    bundles = {}
    for handoff in ("api", "ui"):
        root = tmp_path / handoff
        written = writers.write_world(world.records, root, handoff=handoff)
        out = tmp_path / f"{handoff}.tbx"
        report = export_from_files(_files(written, root), out, key=KEY, team_map=team_map,
                                   cost_center_map={}, since=None, until=None, now_ms=NOW_MS,
                                   tool_version="gate")
        assert report.manifest.leak_scan.result == "clean"
        with zipfile.ZipFile(out) as zf:
            blob = b"".join(zf.read(n) for n in zf.namelist()).lower()
        assert CANARY_LOGIN.encode() not in blob
        bundles[handoff] = read_bundle(out)[1]
    api, ui = bundles["api"], bundles["ui"]
    assert _canon(ui.cost_lines, ("fetched_ms",)) == _canon(api.cost_lines, ("fetched_ms",))
    assert _canon(ui.aggregates, ("fetched_ms",)) == _canon(api.aggregates, ("fetched_ms",))
    seated_api = {lic.principal for lic in api.licenses}
    seated_ui = {lic.principal for lic in ui.licenses}
    assert seated_ui and seated_ui <= seated_api
    assert all(lic.plan == "unknown" and lic.assigned_via_team is None for lic in ui.licenses
               if lic.source_kind == "github.copilot_activity_report")
    assert not [c for c in ui.config if c.source_kind == "github.org_copilot_settings"]
