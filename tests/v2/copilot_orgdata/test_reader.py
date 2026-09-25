"""The shared org-data reader and parsers in ``adapters.github_config``."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.adapters import github_config as gc
from tokenbill.core import jsonl
from tokenbill.core.errors import SourceError

from .helpers import opts


@pytest.mark.parametrize(("text", "ms"), [
    ("2026-09-20", 1_789_862_400_000), ("2026-09-20T00:00:00Z", 1_789_862_400_000),
    ("2026-09-20T01:02:03.456789Z", 1_789_866_123_456), ("2026-09-20 01:02", 1_789_866_120_000),
    ("2026-09-19T18:00:00-06:00", 1_789_862_400_000), ("2026-09-20T05:30:00+0530",
                                                      1_789_862_400_000),
    ("2026-09-20T02:00:00+02", 1_789_862_400_000), ("2026-09-20T00:00:00 UTC", 1_789_862_400_000),
    ("1970-01-01T00:00:00Z", 0)])
def test_parse_ts_ms(text: str, ms: int) -> None:
    assert gc.parse_ts_ms(text, "t") == ms


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-02-30", "2026-09-20T25:00:00Z",
                                 "2026-09-20T10:00:00+24:00", "1969-12-31T23:59:59Z", 5, None,
                                 "2026-09-20T10:00:00+05:99"])
def test_parse_ts_ms_rejects(bad: object) -> None:
    with pytest.raises(gc.BadRecord):
        gc.parse_ts_ms(bad, "t")


def test_small_parsers() -> None:
    assert gc.parse_day(" 2026-09-20 ", "d") == "2026-09-20"
    for bad in ("2026-9-20", "2026-02-30", 20260920):
        with pytest.raises(gc.BadRecord):
            gc.parse_day(bad, "d")
    assert gc.login_key("  OctoCat ") == "octocat" and gc.login_key("") is None
    assert gc.login_key(5) is None and gc.login_key("a\x00b") is None
    assert gc.login_key("x" * 257) is None
    assert gc.safe_label(" team\tA ") == "team_A" and gc.safe_label("x" * 200) is None
    assert gc.safe_label(None) is None and gc.safe_label("  ") is None
    assert gc.safe_token(123) == "123" and gc.safe_token(True) is None
    assert gc.safe_token("a b") is None and gc.safe_token("b-1.x_2") == "b-1.x_2"
    assert gc.count_of(Decimal("12.0"), "c") == 12 and gc.count_of(0, "c") == 0
    for bad in (-1, Decimal("1.5"), 2**53 + 1, True, "3", None, Decimal("1E+40")):
        with pytest.raises(gc.BadRecord):
            gc.count_of(bad, "c")
    assert gc.decimal_of(Decimal("-2.5"), "d") == Decimal("-2.5")
    assert gc.day_of_ms(gc.day_start_ms("2026-09-20") + 5) == "2026-09-20"


def test_head_helpers() -> None:
    head = (b'{"fetched_ms": 1, "request": {"path": "https://api.github.com/orgs/a/copilot/'
            b'billing?x=1", "query": {}}, "response": {"seat_breakdown": {}}}')
    assert gc.head_request_path(head) == "/orgs/a/copilot/billing"
    assert gc.head_request_path(b'{"endpoint": "/x"}') is None
    nested = b'{"request": {"query": {"day": "2026-09-20"}, "path": "/orgs/b/copilot/billing"}}'
    assert gc.head_request_path(nested) == "/orgs/b/copilot/billing"
    assert {"seat_breakdown", "request", "path"} <= gc.head_keys(head)


def _ctx(path: Path, **kw: object) -> gc.OrgRead:
    return gc.OrgRead("test", path, opts(**kw))


def test_ndjson_bad_lines_are_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "x.ndjson"
    path.write_bytes(b'{"a": 1}\n\n{bad json\n[1, 2]\n{"b": 2.50}\n')
    ctx = _ctx(path)
    items = list(ctx.items())
    assert [i.body for i in items] == [{"a": 1}, {"b": Decimal("2.50")}]
    assert [(q.locator, q.reason) for q in ctx.quarantined] == [("line:3", "bad_json"),
                                                               ("line:4", "not_object")]
    strict = _ctx(path, lenient=False)
    with pytest.raises(SourceError):
        list(strict.items())


def test_ndjson_with_a_broken_first_line(tmp_path: Path) -> None:
    path = tmp_path / "x.ndjson"
    path.write_bytes(b'{"a": 1\n{"b": 2}\n')
    ctx = _ctx(path)
    assert [i.body for i in ctx.items()] == [{"b": 2}]
    assert [(q.locator, q.reason) for q in ctx.quarantined] == [("line:1", "bad_json")]


def test_oversize_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jsonl, "MAX_LINE_BYTES", 64)
    path = tmp_path / "x.ndjson"
    path.write_bytes(b'{"a": "' + b"x" * 200 + b'"}\n{"b": 1}\n')
    ctx = _ctx(path)
    assert [i.body for i in ctx.items()] == [{"b": 1}]
    assert [q.reason for q in ctx.quarantined] == ["oversize_line"]


def test_whole_documents_and_wrappers(tmp_path: Path) -> None:
    doc = tmp_path / "d.json"
    doc.write_text(json.dumps([{"x": 1}, 3, {"request": {"url": "https://h/p/q?user=me",
                                                          "query": {"day": 2}},
                                             "response": {"status": 200, "headers": {"Link": ""},
                                                          "body": '{"y": 1.5}'},
                                             "fetched_at": "2026-09-20T00:00:00Z"},
                               {"endpoint": "/v1/x", "response": {"z": 1}, "fetched_ms": 7}],
                              indent=2), encoding="utf-8")
    ctx = _ctx(doc)
    items = list(ctx.items())
    assert [i.locator for i in items] == ["item:0", "item:2", "item:3"]
    env = items[1]
    assert env.path == "/p/q" and env.query == {"user": "me", "day": "2"}
    assert env.body == {"y": Decimal("1.5")} and env.fetched_ms == 1_789_862_400_000
    assert items[2].path == "/v1/x" and items[2].fetched_ms == 7
    assert ctx.fetched(items[0]) == opts().now_ms
    assert [q.reason for q in ctx.quarantined] == ["not_object"]
    bad_body = tmp_path / "b.json"
    bad_body.write_text(json.dumps({"request": {"path": "/p"}, "response": "{nope"}),
                        encoding="utf-8")
    ctx = _ctx(bad_body)
    assert list(ctx.items()) == [] and ctx.quarantined[0].reason == "bad_json"


def test_invalid_documents_gz_and_directories(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{\n "a": [1, 2\n', encoding="utf-8")
    ctx = _ctx(bad)
    assert list(ctx.items()) == [] and ctx.quarantined[0].locator == "file"
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    assert list(_ctx(empty).items()) == []
    gz = tmp_path / "x.ndjson.gz"
    gz.write_bytes(gzip.compress(b'{"a": 1}\n{"a": 2}\n'))
    assert len(list(_ctx(gz).items())) == 2
    corrupt = tmp_path / "c.ndjson.gz"
    data = gzip.compress(b"".join(b'{"a": %d}\n' % (i * 7919 % 100003) for i in range(5000)))
    corrupt.write_bytes(data[: len(data) // 2])
    ctx = _ctx(corrupt)
    list(ctx.items())
    assert ctx.quarantined and ctx.quarantined[-1].reason == "unreadable"
    folder = tmp_path / "dir"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "a.json").write_text('{"a": 1}', encoding="utf-8")
    (folder / "b.ndjson").write_text('{"b": 1}\n', encoding="utf-8")
    (folder / "notes.md").write_text("# not data", encoding="utf-8")
    (folder / "manifest.json").write_text('{"units": []}', encoding="utf-8")
    (folder / ".hidden.json").write_text('{"h": 1}', encoding="utf-8")
    ctx = _ctx(folder)
    assert [i.locator for i in ctx.items()] == ["f0:line:1", "f1:line:1"]
    info = ctx.source_info()
    assert info.bytes == 17 and info.principal_key_id is None


def test_keep_latest() -> None:
    from tokenbill.core.builders import make_config

    table: dict[str, object] = {}
    old = make_config("org_settings", {"cli": "enabled"}, entity_id="org:a", fetched_ms=1)
    new = make_config("org_settings", {"cli": "disabled"}, entity_id="org:a", fetched_ms=2)
    gc.keep_latest(table, "k", new)
    gc.keep_latest(table, "k", old)
    assert table["k"] is new
    gc.keep_latest(table, "j", old)
    gc.keep_latest(table, "j", new)
    assert table["j"] is new
