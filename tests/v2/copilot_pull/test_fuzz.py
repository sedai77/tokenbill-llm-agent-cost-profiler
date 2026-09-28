"""Hypothesis fuzz of every parser CP-PULL owns: only ``TokenbillError`` subclasses may escape
(SPEC §21 #5), and scrubbed / recorded output never keeps a signed URL."""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.copilot.pull_billing import budget_ids, cost_center_ids, parse_export
from tokenbill.copilot.pull_common import (
    MAX_WAIT_S,
    Manifest,
    TokenSource,
    UnitEntry,
    dump_json,
    envelope_line,
    is_signed_url,
    link_next,
    parse_json,
    rate_limit_wait,
    retry_after_seconds,
    scrub,
)
from tokenbill.copilot.pull_metrics import download_links, iter_records, task_ids

FUZZ = settings(max_examples=150, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture])

json_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=40),
                         st.decimals(allow_nan=False, allow_infinity=False, places=4))
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(st.lists(children, max_size=4),
                               st.dictionaries(st.text(max_size=12), children, max_size=4)),
    max_leaves=20)
urlish = st.one_of(
    st.text(max_size=60),
    st.builds(lambda host, q: f"https://{host}/p?{q}",
              st.sampled_from(["a.example", "blob.example.net"]),
              st.sampled_from(["sig=x", "v=4", "se=1&sp=r", "X-Amz-Signature=z", "", "token=t"])))


@FUZZ
@given(st.one_of(st.none(), st.text(max_size=200)))
def test_link_next_never_raises(value: str | None) -> None:
    result = link_next(value)
    assert result is None or isinstance(result, str)


@FUZZ
@given(st.one_of(st.none(), st.text(max_size=60)), st.integers(0, 2**40))
def test_retry_after_is_none_or_non_negative(value: str | None, now: int) -> None:
    result = retry_after_seconds(value, now)
    assert result is None or (isinstance(result, int) and result >= 0)


@FUZZ
@given(st.integers(100, 599), st.dictionaries(
    st.sampled_from(["retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "date"]),
    st.text(max_size=30)), st.binary(max_size=64), st.integers(0, 2**40))
def test_rate_limit_wait_is_bounded(status: int, headers: dict[str, str], body: bytes,
                                    now: int) -> None:
    wait = rate_limit_wait(status, headers, body, now)
    assert wait is None or 1 <= wait <= MAX_WAIT_S


@FUZZ
@given(st.binary(max_size=200))
def test_parse_json_raises_only_pull_errors(data: bytes) -> None:
    try:
        parse_json(data, "fuzz")
    except TokenbillError:
        pass


@FUZZ
@given(json_values)
def test_parse_export_raises_only_pull_errors(doc: Any) -> None:
    try:
        state = parse_export(doc)
    except TokenbillError:
        return
    assert state.status in ("processing", "completed", "failed")
    assert all(u.lower().startswith("https://") for u in state.urls)


@FUZZ
@given(json_values)
def test_download_links_and_id_extractors(doc: Any) -> None:
    try:
        links = download_links(doc)
        assert all(u.lower().startswith("https://") for u in links)
    except TokenbillError:
        pass
    for ids in (budget_ids(doc), cost_center_ids(doc), task_ids(doc, since_ms=0)):
        assert all(isinstance(i, str) and ".." not in i and "/" not in i for i in ids)


@FUZZ
@given(json_values, st.lists(urlish, max_size=4))
def test_scrub_removes_every_signed_url(doc: Any, urls: list[str]) -> None:
    value = {"body": doc, "urls": urls, "download_urls": urls}
    clean, _ = scrub(value)
    assert "download_urls" not in clean

    def walk(v: Any) -> None:
        if isinstance(v, str):
            assert not is_signed_url(v)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(clean)
    try:
        line = envelope_line("/p", {"q": "1"}, value, fetched_ms=0)
    except TokenbillError:
        return
    assert "\n" not in line and json.loads(line)["request"] == {"path": "/p",
                                                                "query": {"q": "1"}}


@FUZZ
@given(json_values)
def test_dump_json_round_trips_exactly(doc: Any) -> None:
    try:
        text = dump_json(doc)
    except ValueError:
        return
    back = json.loads(text, parse_float=Decimal)
    assert dump_json(back) == text


@FUZZ
@given(json_values)
def test_manifest_from_json_raises_only_value_errors(doc: Any) -> None:
    for candidate in (doc, {"schema": "tokenbill/copilot-pull@1", "enterprise": None,
                            "orgs": [], "since": "a", "until": "b", "kinds": [],
                            "units": [doc]}):
        try:
            Manifest.from_json(candidate)
        except ValueError:
            pass


@FUZZ
@given(st.binary(max_size=300))
def test_manifest_load_raises_only_usage_errors(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "manifest.json"
        path.write_bytes(data)
        try:
            Manifest.load(path)
        except TokenbillError:
            pass


@FUZZ
@given(st.builds(UnitEntry, id=st.sampled_from(["seats/enterprise", "metrics/a/b/2026-09-01"]),
                 kind=st.sampled_from(["seats", "metrics"]),
                 status=st.sampled_from(["complete", "incomplete", "not_ready"]),
                 files=st.lists(st.sampled_from(["a/b.jsonl", "c.csv"]), max_size=2,
                                unique=True).map(tuple),
                 day=st.one_of(st.none(), st.just("2026-09-01")),
                 repos=st.one_of(st.none(), st.integers(0, 9)),
                 reason=st.one_of(st.none(), st.text(max_size=10)),
                 export_id=st.one_of(st.none(), st.just("rpt-1")),
                 skipped=st.integers(0, 5)))
def test_unit_entry_round_trip(entry: UnitEntry) -> None:
    assert UnitEntry.from_json(json.loads(json.dumps(entry.to_json()))) == entry


@FUZZ
@given(st.binary(max_size=300))
def test_token_file_contents_raise_only_usage_errors(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "tok"
        path.write_bytes(data)
        path.chmod(0o600)
        try:
            token = TokenSource.from_file(path).get()
        except TokenbillError:
            return
        assert token and all(0x21 <= ord(c) <= 0x7e for c in token)


@FUZZ
@given(st.binary(max_size=400), st.booleans())
def test_iter_records_raises_only_pull_errors(data: bytes, gz: bool) -> None:
    import gzip

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "dl"
        path.write_bytes(gzip.compress(data, mtime=0) if gz else data)
        try:
            recs = list(iter_records(path))
        except TokenbillError:
            return
        assert all(r is None or isinstance(r, dict) for r in recs)
