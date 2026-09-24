"""``rates_verify.verify`` (``pricing verify`` for channel ``github_copilot``): the packaged
snapshot gives zero discrepancies, an injected price change gives one naming the row, every
end-of-day revision since billing started verifies against the replayed file, and the live path
runs only through injected openers (the autouse socket guard proves no network is touched)."""

from __future__ import annotations

import copy
import datetime as _dt
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot import rates_verify as rv
from tokenbill.core.errors import PricingError, SourceError, UsageError
from tokenbill.core.types import Discrepancy

from .support import YML_DIR, layer, rate_doc, revisions, row

OPUS = "github/github_copilot/claude-opus-5-5/2026-09-22"
SNAP = rv.read_snapshot()
LIVE_TEXT = (YML_DIR / "2026-09-22_d1153b57c9.yml").read_text(encoding="utf-8")


def _layer(doc: dict[str, Any]) -> Any:
    return rv.layer_from_json(doc, name="test")


def _entries() -> list[dict[str, str]]:
    return [dict(e) for e in SNAP.entries]


def _entry(entries: list[dict[str, str]], model: str, tier: str = "Default") -> dict[str, str]:
    return next(e for e in entries if e["model"].startswith(model)
                and e.get("tier", "Default") == tier)


def _compare(doc: dict[str, Any] | None = None, entries: list[dict[str, str]] | None = None,
             as_of: str = "2026-09-22") -> list[Discrepancy]:
    return rv.compare(_layer(doc) if doc is not None else layer(),
                      rv.quotes_from_maps(entries if entries is not None else _entries()),
                      as_of=as_of, source="test")


def _fields(found: list[Discrepancy]) -> list[tuple[str, str, str, str, bool]]:
    return [(d.row_id, d.field, d.ours, d.theirs, d.authoritative) for d in found]


def test_packaged_snapshot_has_zero_discrepancies() -> None:
    assert SNAP.date == "2026-09-22" and len(SNAP.entries) == 44
    assert rv.verify(layer()) == []
    assert rv.verify(layer(), snapshot=None, live=False, opener=None) == []


def test_snapshot_is_the_recorded_parse_of_its_revision() -> None:
    assert list(SNAP.entries) == rv.parse_yaml_list(LIVE_TEXT)
    assert "2026-09-22_d1153b57c9" in SNAP.label


def test_injected_price_change_gives_one_discrepancy_naming_the_row() -> None:
    doc = rate_doc()
    row(doc, OPUS)["usd_per_mtok"]["output"] = "21.00"
    found = rv.verify(_layer(doc))
    assert found == [Discrepancy(row_id=OPUS, field="output", ours="21", theirs="20",
                                 source=SNAP.label, authoritative=True)]


def test_injected_table_change_in_a_snapshot_file(tmp_path: Path) -> None:
    doc = json.loads(rv.importlib.resources.files(rv.DATA_PACKAGE).joinpath(
        rv.SNAPSHOT_RESOURCE).read_text(encoding="utf-8"))
    _entry(doc["entries"], "GPT-6 Sol")["cached_input"] = "$0.25"
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert _fields(rv.verify(layer(), snapshot=path)) == [
        ("github/github_copilot/gpt-6-sol/2026-09-22", "cache_read", "0.2", "0.25", True)]


def test_every_end_of_day_revision_since_billing_verifies() -> None:
    """The replayed file reproduces the whole history: verifying each day's last revision at its
    date finds nothing but two documented, expected classes."""
    last = {r.date: r for r in revisions()}
    checked = 0
    for date, rev in sorted(last.items()):
        if date < rv.BILLING_START:
            continue
        for d in rv.verify(layer(), snapshot=YML_DIR / f"{rev.name}.yml"):
            # the Modifier schema has no dates: fast mode was listed only from 2026-06-29
            fast = (d.row_id, d.field, d.theirs) == ("github.fast.opus-4-8", "fast_mode",
                                                     "not listed") and date < "2026-06-29"
            # Sonnet 5's promo footnote (2026-06-30 → 08-11) came with unchanged prices
            sonnet = (d.field, d.authoritative) == ("promotion", False) and "sonnet-5" in d.row_id
            assert fast or sonnet, (rev.name, d)
        checked += 1
    assert checked == 28  # distinct revision days from 2026-06-02 to 2026-09-22


# ---------------------------------------------------------------------------------------------
# compare(): every discrepancy kind
# ---------------------------------------------------------------------------------------------


def test_missing_and_unlisted_rows() -> None:
    entries = [e for e in _entries() if not e["model"].startswith("Kimi K3")]
    entries.append({"model": "Kimi K4", "provider": "moonshot_ai", "input": "$1",
                    "cached_input": "$0.10", "output": "$2"})
    assert _fields(_compare(entries=entries)) == [
        ("github/github_copilot/kimi-k3/2026-08-07", "row", "in force", "not listed", True),
        ("github/github_copilot/kimi-k4", "row", "missing", "listed", True)]


def test_write_and_band_discrepancies() -> None:
    entries = _entries()
    _entry(entries, "GPT-6 Luna")["cache_write"] = "$0.15"   # non-Claude: both classes
    _entry(entries, "Claude Haiku 4.5")["cache_write"] = "Not applicable"
    _entry(entries, "Grok 4.6", "Long context")["output"] = "$13.00"
    band = _entry(entries, "GPT-5.4", "Long context")
    band["threshold"] = "> 300K"
    _entry(entries, "GPT-5.4")["threshold"] = "≤ 300K"
    luna = "github/github_copilot/gpt-6-luna/2026-09-22"
    haiku = "github/github_copilot/claude-haiku-4-5/2026-06-01"
    assert _fields(_compare(entries=entries)) == [
        (haiku, "cache_write_1h", "2", "not applicable", True),
        (haiku, "cache_write_5m", "1.25", "not applicable", True),
        ("github/github_copilot/gpt-5.4/2026-06-04", "long_context.threshold", "272000",
         "300000", True),
        (luna, "cache_write_1h", "0.125", "0.15", True),
        (luna, "cache_write_5m", "0.125", "0.15", True),
        ("github/github_copilot/grok-4.6/2026-08-14", "long_context.output", "12", "13", True)]
    entries = _entries()
    for tier in ("Default", "Long context"):
        _entry(entries, "Grok 4.7", tier).pop("threshold")
    entries.remove(_entry(entries, "Grok 4.7", "Long context"))
    assert _fields(_compare(entries=entries)) == [
        ("github/github_copilot/grok-4.7/2026-09-21", "long_context.threshold", "200000",
         "not applicable", True)]


def test_warnings_category_promotion_alias_disabled() -> None:
    entries = _entries()
    _entry(entries, "Kimi K3")["category"] = "Versatile"
    _entry(entries, "GPT-5.6 Sol")["model"] = "GPT-5.6 Sol[^gpt-56-sol-promo]"
    doc = rate_doc()
    row(doc, "github/github_copilot/gemini-3.6-flash/2026-08-13")["promotion"] = None
    row(doc, "github/github_copilot/grok-4.5/2026-07-28")["aliases"] = ["xAI Grok 4.5"]
    row(doc, OPUS)["enabled"] = False
    row(doc, "github/github_copilot/gpt-5-mini/2026-06-01")["notes"] = "date_source=B"
    assert _fields(_compare(doc, entries)) == [
        (OPUS, "enabled", "false", "listed", False),
        ("github/github_copilot/gemini-3.6-flash/2026-08-13", "promotion", "none",
         "promotion footnote", False),
        ("github/github_copilot/gpt-5-mini/2026-06-01", "category", "not recorded",
         "Lightweight", False),
        ("github/github_copilot/gpt-5.6-sol/2026-09-04", "promotion", "none",
         "promotion footnote", False),
        ("github/github_copilot/grok-4.5/2026-07-28", "aliases", "xAI Grok 4.5", "Grok 4.5",
         False),
        ("github/github_copilot/kimi-k3/2026-08-07", "category", "Powerful", "Versatile", False)]


def test_rows_are_matched_by_alias() -> None:
    doc = rate_doc()
    r = row(doc, "github/github_copilot/kimi-k3/2026-08-07")
    r["model"] = "moonshot-kimi-k3"
    r["row_id"] = "github/github_copilot/moonshot-kimi-k3/2026-08-07"
    assert _compare(doc) == []


def test_fast_mode_discrepancies() -> None:
    fast_entry = _entry(_entries(), "Claude Opus 4.8 (fast mode)")
    doc = rate_doc()
    doc["modifiers"] = doc["modifiers"][:2]
    assert _fields(_compare(doc)) == [("github/github_copilot/claude-opus-4-8/2026-06-01",
                                       "fast_mode", "no replace_base modifier",
                                       "fast mode listed", True)]
    doc = rate_doc()
    doc["modifiers"][2]["base_usd_per_mtok"]["output"] = "60.00"
    row(doc, "github/github_copilot/claude-opus-4-8/2026-06-01")["supports"] = []
    assert _fields(_compare(doc)) == [
        ("github.fast.opus-4-8", "output", "60", "50", True),
        ("github/github_copilot/claude-opus-4-8/2026-06-01", "supports", "none", "fast_mode",
         False)]
    entries = [e for e in _entries() if e is not None]
    entries.remove(_entry(entries, "Claude Opus 4.8 (fast mode)"))
    assert _fields(_compare(entries=entries)) == [
        ("github.fast.opus-4-8", "fast_mode", "priced", "not listed", True)]
    only_fast = [dict(fast_entry, model="Claude Opus 9 (fast mode)")]
    found = _fields(_compare(entries=only_fast))
    assert ("github/github_copilot/claude-opus-9", "fast_mode", "missing", "listed", True) \
        in found


# ---------------------------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------------------------


def test_read_snapshot_forms_and_errors(tmp_path: Path) -> None:
    rev = rv.read_snapshot(YML_DIR / "2026-08-21_1cabce7332.yml")
    assert rev.date == "2026-08-21" and "2026-08-21_1cabce7332" in rev.label
    bad_name = tmp_path / "pricing.yml"
    bad_name.write_text(LIVE_TEXT, encoding="utf-8")
    with pytest.raises(UsageError):
        rv.read_snapshot(bad_name)
    with pytest.raises(SourceError):
        rv.read_snapshot(tmp_path / "absent.json")
    path = tmp_path / "snap.json"
    for doc in ({"schema": "x"}, {"schema": rv.SNAPSHOT_SCHEMA, "revision": "r", "date": "d",
                                  "entries": []},
                {"schema": rv.SNAPSHOT_SCHEMA, "revision": "r", "date": "2026-09-22",
                 "entries": [{"model": 1}]}, [1]):
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(PricingError):
            rv.read_snapshot(path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(PricingError):
        rv.read_snapshot(path)


# ---------------------------------------------------------------------------------------------
# live (injected openers only)
# ---------------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes, *, status: int | None = 200,
                 date: str | None = "Tue, 22 Sep 2026 20:00:00 GMT") -> None:
        self.body, self.status = body, status
        self.headers = {"Date": date} if date is not None else {}
        self.closed = False

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response or FakeResponse(LIVE_TEXT.encode())
        self.error = error
        self.calls: list[tuple[urllib.request.Request, int]] = []

    def __call__(self, request: urllib.request.Request, timeout: int) -> Any:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def test_live_verify_through_an_injected_opener() -> None:
    opener = FakeOpener()
    assert rv.verify(layer(), live=True, opener=opener) == []
    ((request, timeout),) = opener.calls
    assert request.full_url == rv.YAML_URL and timeout == 30
    assert request.get_header("User-agent") == rv.USER_AGENT
    assert opener.response.closed


def test_live_discrepancies_name_the_live_source() -> None:
    text = LIVE_TEXT.replace("input: $4.00\n  cached_input: $0.20", "input: $4.40\n  "
                             "cached_input: $0.22")
    found = rv.verify(layer(), live=True, opener=FakeOpener(FakeResponse(text.encode())))
    assert {(d.row_id, d.field) for d in found} == {(OPUS, "input"), (OPUS, "cache_read")}
    assert all(d.source == f"live {rv.YAML_URL} (2026-09-22)" for d in found)


def test_live_opener_director_and_default_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    class Director:
        def __init__(self) -> None:
            self.fake = FakeOpener()

        def open(self, request: urllib.request.Request, timeout: int) -> Any:
            return self.fake(request, timeout)

    director = Director()
    assert rv.fetch_live(opener=director) == (LIVE_TEXT, "2026-09-22")
    default = FakeOpener(FakeResponse(b"- model: Kimi K3\n", status=None,
                                      date="Wed, 23 Sep 2026 23:59:00"))  # no zone: UTC
    monkeypatch.setattr(urllib.request, "urlopen", default)
    assert rv.fetch_live() == ("- model: Kimi K3\n", "2026-09-23")
    assert len(default.calls) == 1


def test_live_response_date_falls_back_to_today() -> None:
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    for date in (None, "yesterday-ish"):
        _, got = rv.fetch_live(opener=FakeOpener(FakeResponse(b"", date=date)))
        assert got in (today, (_dt.date.fromisoformat(today) + _dt.timedelta(days=1)).isoformat())


@pytest.mark.parametrize("opener", [
    FakeOpener(error=urllib.error.URLError("offline")),
    FakeOpener(error=urllib.error.HTTPError(rv.YAML_URL, 503, "busy", {}, None)),  # type: ignore
    FakeOpener(error=TimeoutError()),
    FakeOpener(FakeResponse(b"x", status=404)),
    FakeOpener(FakeResponse(b"\xff\xfe")),
    FakeOpener(FakeResponse(b"#" * (rv.MAX_YAML_CHARS * 4 + 1))),
    FakeOpener(FakeResponse("not bytes")),  # type: ignore[arg-type]
], ids=["url", "http", "timeout", "status", "utf8", "size", "type"])
def test_live_failures_are_source_errors(opener: FakeOpener) -> None:
    with pytest.raises(SourceError):
        rv.verify(layer(), live=True, opener=opener)


def test_live_and_snapshot_are_exclusive() -> None:
    with pytest.raises(UsageError):
        rv.verify(layer(), live=True, snapshot=YML_DIR / "2026-09-22_d1153b57c9.yml",
                  opener=FakeOpener())


def test_live_document_that_is_not_the_table_is_a_pricing_error() -> None:
    with pytest.raises(PricingError):
        rv.verify(layer(), live=True, opener=FakeOpener(FakeResponse(b"<html>moved</html>")))


def test_layer_rows_of_other_channels_are_ignored() -> None:
    other = copy.deepcopy(layer())
    assert rv.compare(other, rv.quotes_from_maps(SNAP.entries), as_of="2026-09-22",
                      source="x") == []
