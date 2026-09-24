"""Area-local helpers for the CP-ORGDATA tests (imported only by tests in this directory)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tokenbill.copilot.teammap import build_maps
from tokenbill.core.builders import CANARY, CANARY_EMAIL, CANARY_LOGIN
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.records import to_json
from tokenbill.core.types import IngestOptions, IngestResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "copilot_orgdata"
NAME_KEY = bytes(range(0, 32))
PRINCIPAL_KEY = bytes(range(32, 64))
NOW_MS = 1_790_380_800_000          # 2026-09-26T00:00:00Z
TEAM_MAP, CC_MAP = build_maps(FIXTURES / "metrics" / "user-teams-1-day.ndjson",
                              FIXTURES / "config" / "cost_centers.json", k=5)


def opts(**kw: Any) -> IngestOptions:
    """Central-ingest options with fixed keys, the fixture team / cost-center maps and clock."""
    base: dict[str, Any] = {
        "identity_mode": "central-ingest", "name_key": NAME_KEY, "name_key_id": key_id(NAME_KEY),
        "principal_key": PRINCIPAL_KEY, "principal_key_id": key_id(PRINCIPAL_KEY),
        "team_map": tuple(TEAM_MAP.items()), "cost_center_map": tuple(CC_MAP.items()),
        "k_anonymity": 5, "now_ms": NOW_MS,
    }
    base.update(kw)
    return IngestOptions(**base)


def p_of(login: str) -> str:
    """The ``p_`` the adapters produce for *login* under the test principal key."""
    return pseudonym(PRINCIPAL_KEY, "p", login.strip().lower())


def h(value: str) -> str:
    """The ``h_`` of *value* under the test name key."""
    return pseudonym(NAME_KEY, "h", value)


def blob(result: IngestResult) -> str:
    """Every record of a result as one JSON string (for leak checks)."""
    return json.dumps(to_json(result), sort_keys=True) + repr(result)


def assert_no_identity(result: IngestResult, *logins: str) -> None:
    """No canary, canary login / e-mail, raw login or e-mail address anywhere in *result*."""
    text = blob(result)
    low = text.lower()
    for needle in (CANARY, CANARY_LOGIN, CANARY_EMAIL, *logins):
        assert needle.lower() not in low, needle
    assert "@" not in text


def attrs(snap: Any) -> dict[str, Any]:
    """``dict(snapshot.attrs)``."""
    return dict(snap.attrs)


def write_lines(path: Path, records: list[Any]) -> Path:
    """Write *records* as NDJSON (numbers as given; ``Decimal`` via ``str``)."""
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in records),
                    encoding="utf-8")
    return path
