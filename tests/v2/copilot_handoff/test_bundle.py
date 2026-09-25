"""Bundle format ``tokenbill/copilot-export@1`` (brief Build 1): round trip, determinism, file
mode, member layout and the manifest."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tokenbill.copilot.handoff import (
    EXPORT_SCHEMA,
    MANIFEST_KEYS,
    RECORD_MEMBERS,
    BundleManifest,
    LeakTerms,
    read_bundle,
    write_bundle,
)
from tokenbill.core.builders import CANARY_LOGIN, make_ai_usage_row, make_license
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.records import record_key, to_json

from .helpers import KEY, KEY2, NOW_MS, ORG, _result, round_trip_results, seed, write_round_trip

REPO = Path(__file__).resolve().parents[3]


def canonical_set(records: list) -> list[str]:
    return sorted(json.dumps(to_json(r), sort_keys=True) for r in records)


def test_round_trip_every_kind(tmp_path: Path) -> None:
    out = tmp_path / "export.tbx"
    manifest = write_round_trip(out)
    assert manifest.privacy_mode == "pseudonymous" and manifest.teams_merged == 0
    read_manifest, result = read_bundle(out)
    assert read_manifest == manifest
    inputs = round_trip_results()
    for kind in ("cost_lines", "aggregates", "outcomes", "licenses", "activity", "config"):
        expected = [r for res in inputs for r in getattr(res, kind)]
        assert canonical_set(getattr(result, kind)) == canonical_set(expected), kind
        assert manifest.count(kind) == len(expected)
    canary_p = pseudonym(KEY, "p", CANARY_LOGIN)
    assert any(lic.principal == canary_p for lic in result.licenses)
    assert result.source.adapter == "copilot-export"
    assert result.source.principal_key_id == key_id(KEY) == result.source.name_key_id
    assert result.capabilities == frozenset({"aggregates", "cost", "copilot_billing", "licenses",
                                             "activity", "config", "outcomes"})
    assert result.requests == [] and result.quarantined == []


def test_member_layout_and_bytes(tmp_path: Path) -> None:
    out = tmp_path / "export.tbx"
    write_round_trip(out)
    data = out.read_bytes()
    assert data[:4] == b"PK\x03\x04" and data[30:43] == b"manifest.json"
    with zipfile.ZipFile(out) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == ["manifest.json", *RECORD_MEMBERS]
        assert zf.comment == b""
        for info in infos:
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.external_attr >> 16 == 0o100600
            assert info.extra == b"" and info.comment == b""
            assert info.create_system == 3
        manifest = json.loads(zf.read("manifest.json"))
        assert list(manifest) == sorted(MANIFEST_KEYS)
        assert set(manifest) == set(MANIFEST_KEYS)
        lines = zf.read("records/licenses.jsonl").decode().splitlines()
    keys = [json.loads(x) for x in lines]
    assert lines == [json.dumps(k, sort_keys=True, separators=(",", ":")) for k in keys]
    licenses = read_bundle(out)[1].licenses
    assert [record_key(x) for x in licenses] == sorted(record_key(x) for x in licenses)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_file_mode_0600_and_no_temp_left(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "export.tbx"
    write_round_trip(out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert sorted(p.name for p in out.parent.iterdir()) == ["export.tbx"]
    write_round_trip(out)                       # overwrite in place (atomic rename)
    assert sorted(p.name for p in out.parent.iterdir()) == ["export.tbx"]


def test_byte_identical_across_processes(tmp_path: Path) -> None:
    here = tmp_path / "here.tbx"
    write_round_trip(here)
    code = ("import sys; from pathlib import Path; "
            "from tests.v2.copilot_handoff.helpers import write_round_trip; "
            "write_round_trip(Path(sys.argv[1]))")
    digests = set()
    for seed_value in ("1", "2"):
        there = tmp_path / f"there-{seed_value}.tbx"
        subprocess.run([sys.executable, "-c", code, str(there)], check=True, cwd=REPO,
                       env={**os.environ, "PYTHONHASHSEED": seed_value}, capture_output=True)
        digests.add(hashlib.sha256(there.read_bytes()).hexdigest())
    assert digests == {hashlib.sha256(here.read_bytes()).hexdigest()}


def test_manifest_contents(tmp_path: Path) -> None:
    out = tmp_path / "export.tbx"
    manifest = write_round_trip(out, manifest_seed=seed(
        window=("2026-09-01", "2026-09-30"), key_rotated=True, rows_excluded=4,
        experimental=["copilot-report-quota"], dq={"dq.something": 2}))
    doc = manifest.to_json()
    assert doc["schema"] == EXPORT_SCHEMA and doc["tool_version"] == "0.2.0-test"
    assert doc["created_ms"] == NOW_MS
    assert doc["window"] == {"since": "2026-09-01", "until": "2026-09-30"}
    assert doc["orgs"] == [ORG] and "org:acme-org" in doc["entities"]
    assert doc["key_rotated"] is True and doc["rows_excluded"] == 4 and doc["k"] == 5
    assert doc["experimental"] == ["copilot-report-quota"]
    assert {"code": "dq.something", "count": 2} in doc["dq"]
    assert doc["leak_scan"] == {"terms": 2, "result": "clean"}
    assert doc["plan_evidence"] == [{"entity_id": "enterprise", "month": "2026-09",
                                     "plan": "enterprise", "source": "seats_api",
                                     "conflict": False}]
    assert [s["adapter"] for s in doc["sources"]] == ["fake-ai-usage", "fake-config", "fake-org"]
    assert BundleManifest.from_json(json.loads(json.dumps(doc))) == manifest
    text = json.dumps(doc)
    assert CANARY_LOGIN not in text and "export.tbx" not in text and str(tmp_path) not in text


def test_dedupe_latest_fetch_wins_and_window(tmp_path: Path) -> None:
    p = pseudonym(KEY, "p", "someone")
    old, _ = make_ai_usage_row(principal=p, credits="1", fetched_ms=1, date_utc="2026-09-10")
    new, _ = make_ai_usage_row(principal=p, credits="9", fetched_ms=2, date_utc="2026-09-10")
    early, _ = make_ai_usage_row(principal=p, credits="5", date_utc="2026-08-31")
    lic_in = make_license(p, snapshot_date="2026-09-02")
    lic_out = make_license(p, snapshot_date="2026-10-02")
    src = dataclasses.replace(round_trip_results()[0].source)
    res = _result(src, cost_lines=[new, old, early], licenses=[lic_in, lic_out])
    out = tmp_path / "w.tbx"
    manifest = write_bundle([res], out, manifest_seed=seed(window=("2026-09-01", "2026-09-30")),
                            leak_terms=frozenset(), k=2, aggregate_only=False)
    _, got = read_bundle(out)
    assert [c.quantity for c in got.cost_lines] == ["9"]
    assert [x.snapshot_date for x in got.licenses] == ["2026-09-02"]
    assert dict(manifest.dq)["dq.copilot_export_outside_window"] == 2


def test_inputs_under_another_key_are_refused(tmp_path: Path) -> None:
    results = round_trip_results(KEY2)
    with pytest.raises(UsageError, match="another key"):
        write_bundle(results, tmp_path / "x.tbx", manifest_seed=seed(), leak_terms=frozenset(),
                     k=5, aggregate_only=False)
    names_only = results[0]
    bad_names = dataclasses.replace(names_only.source, principal_key_id=key_id(KEY),
                                    name_key_id=key_id(KEY2))
    names_only = dataclasses.replace(names_only, source=bad_names)
    with pytest.raises(UsageError, match="name-hashed"):
        write_bundle([names_only], tmp_path / "x.tbx", manifest_seed=seed(),
                     leak_terms=frozenset(), k=5, aggregate_only=False)
    assert not (tmp_path / "x.tbx").exists()


def test_lane_records_are_dropped_with_a_note(tmp_path: Path) -> None:
    from tokenbill.core.builders import make_request

    res = dataclasses.replace(round_trip_results()[0], requests=[make_request("ln-1", 0, 1000)])
    manifest = write_bundle([res], tmp_path / "l.tbx", manifest_seed=seed(),
                            leak_terms=frozenset(), k=5, aggregate_only=False)
    assert dict(manifest.dq)["dq.copilot_export_lane_records_dropped"] == 1


@pytest.mark.parametrize(("bad", "match"), [
    ({"tool_version": ""}, "tool_version"),
    ({"principal_key_id": "nope"}, "principal_key_id"),
    ({"created_ms": -1}, "created_ms"),
    ({"window": ("2026-09-31", None)}, "since"),
    ({"window": ("2026-09-10", "2026-09-01")}, "after"),
    ({"window": "2026-09"}, "window"),
    ({"key_rotated": "yes"}, "key_rotated"),
    ({"rows_excluded": -3}, "rows_excluded"),
    ({"experimental": [1]}, "experimental"),
    ({"dq": {"dq.x": -1}}, "dq"),
    ({"surprise": 1}, "manifest_seed keys"),
])
def test_seed_validation(tmp_path: Path, bad: dict, match: str) -> None:
    with pytest.raises(UsageError, match=match):
        write_bundle(round_trip_results(), tmp_path / "s.tbx", manifest_seed=seed(**bad),
                     leak_terms=frozenset(), k=5, aggregate_only=False)


def test_argument_validation(tmp_path: Path) -> None:
    kw = dict(manifest_seed=seed(), leak_terms=frozenset(), aggregate_only=False)
    with pytest.raises(UsageError, match="k must"):
        write_bundle(round_trip_results(), tmp_path / "a.tbx", k=1, **kw)
    with pytest.raises(UsageError, match="frozenset"):
        write_bundle(round_trip_results(), tmp_path / "a.tbx", k=5,
                     manifest_seed=seed(), leak_terms=["x"], aggregate_only=False)
    with pytest.raises(UsageError, match="IngestResult"):
        write_bundle(["nope"], tmp_path / "a.tbx", k=5, **kw)  # type: ignore[list-item]
    with pytest.raises(UsageError, match="since"):
        write_bundle([], tmp_path / "a.tbx", k=5, leak_terms=frozenset(), aggregate_only=False,
                     manifest_seed=seed(window={"since": "x"}))


def test_window_mapping_and_empty_bundle(tmp_path: Path) -> None:
    out = tmp_path / "empty.tbx"
    manifest = write_bundle([], out, manifest_seed=seed(window={"since": "2026-09-01"}),
                            leak_terms=frozenset(), k=5, aggregate_only=False)
    assert manifest.window == ("2026-09-01", None)
    assert all(n == 0 for _, n in manifest.counts)
    got_manifest, got = read_bundle(out)
    assert got_manifest == manifest and got.capabilities == frozenset()


def test_read_bundle_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        read_bundle(tmp_path / "missing.tbx")


def test_leak_terms_type_is_private() -> None:
    terms = LeakTerms({"Secret-Login": "login", "12345678": "login", "x": "login"})
    assert repr(terms) == "LeakTerms(<2 terms>)" == str(terms)
    assert "secret-login" in terms and terms.category("12345678") == "user_id"
    assert terms.category("unknown-term") == "login"
    merged = terms.merged(LeakTerms({"secret-login": "repository_short", "team-x": "login"}))
    assert merged.category("secret-login") == "login" and "team-x" in merged
    assert LeakTerms(["T2x"]).merged(["t2y "]) == frozenset({"t2x", "t2y"})
