"""Hypothesis fuzzing of every parser this package owns: only ``TokenbillError`` may escape
(SPEC §21 #5, §8.10). ``TB_HANDOFF_FUZZ_EXAMPLES`` raises the example count for longer runs."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.adapters.github_activity_report import (
    ActivityReportAdapter,
    parse_timestamp,
)
from tokenbill.common import TokenbillError
from tokenbill.copilot.admin_answers import ANSWERS_SCHEMA, FIELDS, parse_answers
from tokenbill.copilot.handoff import (
    LeakTerms,
    harvest_leak_terms,
    leak_scan,
    read_bundle,
    read_team_map_csv,
)
from tokenbill.core.ids import key_id
from tokenbill.core.records import EDITOR_FAMILIES, LICENSE_BUCKETS
from tokenbill.core.types import IngestOptions

from .helpers import KEY, NOW_MS, write_round_trip

EXAMPLES = int(os.environ.get("TB_HANDOFF_FUZZ_EXAMPLES", "60"))
FUZZ = settings(max_examples=EXAMPLES, deadline=None, derandomize=True,
                suppress_health_check=[HealthCheck.too_slow])
HEADER = "report_time,login,last_authenticated_at,last_activity_at,last_surface_used"
OPTS = IngestOptions(identity_mode="central-ingest", principal_key=KEY,
                     principal_key_id=key_id(KEY), name_key=KEY, name_key_id=key_id(KEY),
                     now_ms=NOW_MS)

cells = st.one_of(
    st.text(max_size=30),
    st.sampled_from(["2026-09-20T08:00:00Z", "9/1/2026 10:00 AM", "", "Unspecified",
                     "VS Code 1.2", "JetBrains PyCharm", "2026-02-30", "13/13/2026",
                     "9999-12-31T23:59:59-05:00", "0000-01-01", '"quoted, cell"']),
)
rows = st.lists(st.lists(cells, min_size=0, max_size=7), max_size=8)


def _csv(header: str, table: list[list[str]]) -> str:
    buf = io.StringIO()
    buf.write(header + "\n")
    for row in table:
        buf.write(",".join(json.dumps(c) if ("," in c or '"' in c or "\n" in c) else c
                           for c in row) + "\n")
    return buf.getvalue()


@FUZZ
@given(table=rows, header=st.sampled_from([HEADER, "login,last_activity_at,last_surface_used",
                                           HEADER + ",extra"]), bom=st.booleans(),
       lenient=st.booleans())
def test_activity_report_parser(table: list[list[str]], header: str, bom: bool,
                                lenient: bool) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "report.csv"
        path.write_text(("﻿" if bom else "") + _csv(header, table), encoding="utf-8",
                        errors="surrogatepass")
        try:
            res = ActivityReportAdapter().read(
                path, IngestOptions(**{**_opts(), "lenient": lenient}))
        except TokenbillError:
            return
        for lic in res.licenses:
            assert lic.principal.startswith("p_") and lic.plan == "unknown"
            assert lic.last_activity_bucket in LICENSE_BUCKETS
            assert lic.last_activity_surface in (*EDITOR_FAMILIES, None)
        assert res.stats["records"] == len(res.licenses)


def _opts() -> dict:
    return {f: getattr(OPTS, f) for f in ("identity_mode", "principal_key", "principal_key_id",
                                          "name_key", "name_key_id", "now_ms")}


@FUZZ
@given(st.binary(max_size=200))
def test_activity_report_bytes(raw: bytes) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "r.csv"
        path.write_bytes(HEADER.encode() + b"\n" + raw)
        adapter = ActivityReportAdapter()
        assert isinstance(adapter.sniff(path, raw), bool)
        try:
            adapter.read(path, OPTS)
        except TokenbillError:
            pass


@FUZZ
@given(st.text(max_size=40))
def test_parse_timestamp(text: str) -> None:
    try:
        parse_timestamp(text)
    except ValueError:
        pass


def _good_members() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "good.tbx"
        write_round_trip(path)
        with zipfile.ZipFile(path) as zf:
            return {n: zf.read(n) for n in zf.namelist()}


GOOD = _good_members()


@FUZZ
@given(member=st.sampled_from(sorted(GOOD)), pos=st.integers(min_value=0, max_value=10**6),
       new=st.binary(min_size=1, max_size=8))
def test_read_bundle_mutated_members(member: str, pos: int, new: bytes) -> None:
    data = GOOD[member]
    at = pos % (len(data) + 1)
    mutated = {**GOOD, member: data[:at] + new + data[at + len(new):]}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "m.tbx"
        with zipfile.ZipFile(path, "w") as zf:
            for name in GOOD:
                zf.writestr(name, mutated[name], compress_type=zipfile.ZIP_DEFLATED)
        try:
            manifest, res = read_bundle(path)
        except TokenbillError:
            return
        assert manifest.count("licenses") == len(res.licenses)


@FUZZ
@given(st.binary(max_size=400))
def test_read_bundle_random_bytes(raw: bytes) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "r.tbx"
        path.write_bytes(b"PK\x03\x04" + raw)
        try:
            read_bundle(path)
        except TokenbillError:
            pass


json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-5, 10**10), st.text(max_size=12),
              st.sampled_from(["unknown", "business", "enterprise", "mixed", "metered", "block",
                               "2026-12-31", "assign_all", "none", "enabled"])),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.sampled_from(["enterprise", "org:acme", "cc:Platform", "business",
                                         "enterprise ", "x"]), inner, max_size=3)),
    max_leaves=10)


@FUZZ
@given(fields=st.dictionaries(st.sampled_from([*FIELDS, "_help_x", "surprise"]), json_values,
                              max_size=6),
       schema=st.sampled_from([ANSWERS_SCHEMA, "other"]))
def test_parse_answers(fields: dict, schema: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.json"
        path.write_text(json.dumps({"schema": schema, **fields}), encoding="utf-8")
        try:
            snaps = parse_answers(path, snapshot_ms=NOW_MS)
        except TokenbillError:
            return
        assert snaps[0].kind == "run_flags"


@FUZZ
@given(members=st.dictionaries(st.sampled_from(["manifest.json", "records/config.jsonl",
                                                 "records/x.jsonl"]),
                               st.one_of(st.binary(max_size=80),
                                         json_values.map(lambda v: json.dumps(v).encode())),
                               max_size=3),
       terms=st.lists(st.text(max_size=10), max_size=5))
def test_leak_scan_never_raises(members: dict[str, bytes], terms: list[str]) -> None:
    hits = leak_scan(members, LeakTerms(terms))
    assert all(member in members and isinstance(cat, str) for member, cat in hits)


@FUZZ
@given(content=st.one_of(st.binary(max_size=200),
                         json_values.map(lambda v: json.dumps(v).encode()),
                         rows.map(lambda t: _csv("username,repository,email", t).encode())))
def test_harvest_never_raises(content: bytes) -> None:
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "input.dat").write_bytes(content)
        terms = harvest_leak_terms([Path(d)])
        assert isinstance(terms, LeakTerms)


@FUZZ
@given(table=rows, header=st.sampled_from(["login,team", "login,team,cost_center", "a,b", ""]))
def test_team_map_csv(table: list[list[str]], header: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "teams.csv"
        path.write_text(_csv(header, table), encoding="utf-8", errors="surrogatepass")
        try:
            mapping = read_team_map_csv(path)
        except TokenbillError:
            return
        assert all("@" not in login and login.strip() == login for login in mapping)
