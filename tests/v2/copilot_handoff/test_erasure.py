"""Erasure and opt-out (brief Provides): ``pseudonym_of``, ``exclude_logins`` and the private
pre-filter."""

from __future__ import annotations

import gzip
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from tokenbill.copilot import handoff
from tokenbill.copilot.handoff import pseudonym_of, read_bundle
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import SourceError, UsageError

from .helpers import KEY, KEY2, export, use_fake_registry, write_world


def test_pseudonym_of_matches_the_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch)
    world = write_world(tmp_path / "in")
    export(world, tmp_path / "out.tbx")
    _, res = read_bundle(tmp_path / "out.tbx")
    p = pseudonym_of(CANARY_LOGIN, key=KEY)
    assert p == pseudonym_of(f"  {CANARY_LOGIN}\n", key=KEY)
    assert any(c.principal == p for c in res.cost_lines)
    assert sum(lic.principal == p for lic in res.licenses) == 2      # seats API + activity report
    assert any(a.principal == p for a in res.activity)
    assert pseudonym_of(CANARY_LOGIN, key=KEY2) != p


def test_pseudonym_of_errors() -> None:
    with pytest.raises(UsageError):
        pseudonym_of("   ", key=KEY)
    with pytest.raises(UsageError):
        pseudonym_of("octocat", key=b"short")


def test_exclude_logins_removes_every_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch)
    world = write_world(tmp_path / "in")
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*a: object, **kw: object) -> str:
        path = real_mkdtemp(*a, **kw)  # type: ignore[arg-type]
        made.append(path)
        return path

    monkeypatch.setattr(handoff.tempfile, "mkdtemp", spy)
    report = export(world, tmp_path / "out.tbx", exclude_logins=frozenset({CANARY_LOGIN.upper()}),
                    key_rotated=True)
    manifest = report.manifest
    # usage 2 rows + seats 1 + metrics 2 lines + activity report 1 row
    assert manifest.rows_excluded == 6
    assert manifest.key_rotated is True and manifest.leak_scan.result == "clean"
    p = pseudonym_of(CANARY_LOGIN, key=KEY)
    _, res = read_bundle(tmp_path / "out.tbx")
    assert all(p not in (x.principal for x in getattr(res, kind))
               for kind in ("cost_lines", "licenses", "activity"))
    assert len({lic.principal for lic in res.licenses}) == 14
    assert made and not any(Path(d).exists() for d in made)
    # the raw inputs are untouched
    assert CANARY_LOGIN in (world["dir"] / "usage.csv").read_text(encoding="utf-8")


def test_prefilter_directory_is_private_and_removed_on_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch)
    world = write_world(tmp_path / "in")
    seen: list[tuple[str, int]] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*a: object, **kw: object) -> str:
        path = real_mkdtemp(*a, **kw)  # type: ignore[arg-type]
        seen.append((path, 0))
        return path

    class Boom:
        name = "boom"
        capabilities = frozenset()

        def sniff(self, path: Path, head: bytes) -> bool:
            return path.name == "usage.csv"

        def read(self, path: Path, opts: object) -> object:
            parent = path.parent.parent
            seen.append((str(parent), stat.S_IMODE(os.stat(parent).st_mode)))
            raise SourceError("usage.csv: boom")

    monkeypatch.setattr(handoff.tempfile, "mkdtemp", spy)
    table = use_fake_registry(monkeypatch)
    monkeypatch.setattr(handoff, "_REFUSED_ADAPTERS", frozenset())
    from tokenbill.core import registry

    monkeypatch.setattr(registry, "BUILTIN_ADAPTERS", {"boom": Boom, **table})
    with pytest.raises(SourceError, match="boom"):
        export(world, tmp_path / "out.tbx", exclude_logins=frozenset({CANARY_LOGIN}))
    workdir, mode = seen[-1]
    if os.name != "nt":
        assert mode == 0o700
    assert not Path(workdir).exists()
    assert not (tmp_path / "out.tbx").exists()


# ---------------------------------------------------------------------------------------------
# the raw-row filter
# ---------------------------------------------------------------------------------------------

EXCLUDED = frozenset({"gone-user"})


def filt(text: str) -> tuple[str, int]:
    return handoff._filter_text(text, EXCLUDED)


def test_filter_csv_rows() -> None:
    text = "date,Username,amount\n2026-09-01,GONE-USER,0.4272621300000001\n2026-09-01,kept,1\n"
    out, n = filt(text)
    assert n == 1 and out == "date,Username,amount\n2026-09-01,kept,1\n"
    assert filt("date,amount\n1,2\n") == ("date,amount\n1,2\n", 0)
    assert filt("") == ("", 0)
    unchanged = "login,team\nkept,a\n"
    assert filt(unchanged) == (unchanged, 0)


def test_filter_json_documents_keep_numbers_exact() -> None:
    doc = ('{"total_seats": 2, "seats": [{"assignee": {"login": "gone-user", "id": 7}, '
           '"x": 0.4272621300000001}, {"assignee": {"login": "kept"}, "y": 1e-7, '
           '"z": 12345678901234567890}]}')
    out, n = filt(doc)
    assert n == 1
    assert "gone-user" not in out and "0.4272621300000001" not in out
    assert '"y":1e-7' in out and "12345678901234567890" in out
    assert json.loads(out)["seats"] == [{"assignee": {"login": "kept"}, "y": 1e-7,
                                         "z": 12345678901234567890}]
    assert filt('{"assignee": {"login": "gone-user"}}') == ("", 1)
    assert filt('{"seats": []}') == ('{"seats": []}', 0)


def test_filter_ndjson_and_envelopes() -> None:
    lines = [json.dumps({"user_login": "gone-user", "day": "2026-09-01"}),
             json.dumps({"user_login": "kept", "v": 1.5}),
             "{broken",
             "",
             json.dumps({"request": {"path": "/x"}, "response": {"seats": [
                 {"assignee": {"login": "Gone-User"}}, {"assignee": {"login": "k2"}}]}})]
    out, n = filt("\n".join(lines) + "\n")
    assert n == 2
    kept = out.splitlines()
    assert kept[0] == lines[1] and kept[1] == "{broken"
    assert json.loads(kept[-1])["response"]["seats"] == [{"assignee": {"login": "k2"}}]
    assert filt(lines[1] + "\n") == (lines[1] + "\n", 0)


def test_prefilter_rewrites_gzip_inputs(tmp_path: Path) -> None:
    raw = tmp_path / "usage.csv.gz"
    raw.write_bytes(gzip.compress(b"username,v\ngone-user,1\nkept,2\n"))
    other = tmp_path / "other.csv"
    other.write_text("username,v\nkept,1\n", encoding="utf-8")
    zipped = tmp_path / "b.zip"
    zipped.write_bytes(b"PK\x03\x04 container")
    with handoff._prefiltered([raw, other, zipped], EXCLUDED) as (paths, n):
        assert n == 1
        assert paths[1] == other and paths[2] == zipped
        assert paths[0] != raw and paths[0].name == raw.name
        assert gzip.decompress(paths[0].read_bytes()) == b"username,v\nkept,2\n"
    assert not paths[0].exists()
    with handoff._prefiltered([raw], frozenset({"  "})) as (paths, n):
        assert paths == [raw] and n == 0
