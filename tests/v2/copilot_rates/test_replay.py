"""Replay of the 38 dated pricing-YAML revisions (CP-RATES brief, Build #1; acceptance: "replaying
the revisions reproduces the §19.2 history rows with their date sources")."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.copilot import rates_verify as rv
from tokenbill.core.errors import PricingError, SourceError

from .support import YML_DIR, rate_doc, revisions

N = None
#: Addendum §19.2 (in force on 2026-09-22 and the replayed history), transcribed independently of
#: the replay: model, effective_from, effective_to, date source of the start (of the end),
#: input / cached / write / output, and the long-context band (threshold, input, output) or None.
HISTORY_19_2 = [
    ("gpt-5-mini", "2026-06-01", N, "B", N, "0.25", "0.025", N, "2.00", N),
    ("gpt-5.3-codex", "2026-06-01", N, "B", N, "1.75", "0.175", N, "14.00", N),
    ("gpt-5.4", "2026-06-01", "2026-06-04", "B", "K", "2.50", "0.25", N, "15.00", N),
    ("gpt-5.4", "2026-06-04", N, "K", N, "2.50", "0.25", N, "15.00", (272_000, "5.00", "22.50")),
    ("gpt-5.4-mini", "2026-06-01", N, "B", N, "0.75", "0.075", N, "4.50", N),
    ("gpt-5.4-nano", "2026-06-01", N, "B", N, "0.20", "0.02", N, "1.25", N),
    ("gpt-5.5", "2026-06-01", "2026-06-04", "B", "K", "5.00", "0.50", N, "30.00", N),
    ("gpt-5.5", "2026-06-04", N, "K", N, "5.00", "0.50", N, "30.00", (272_000, "10.00", "45.00")),
    ("gpt-5.6-luna", "2026-07-09", "2026-07-30", "K", "K", "1.00", "0.10", N, "6.00",
     (200_000, "2.00", "9.00")),
    ("gpt-5.6-luna", "2026-07-30", "2026-08-03", "K", "K", "0.20", "0.02", N, "1.20",
     (200_000, "0.40", "1.80")),
    ("gpt-5.6-luna", "2026-08-03", N, "K", N, "0.20", "0.02", "0.25", "1.20",
     (200_000, "0.40", "1.80")),
    ("gpt-5.6-sol", "2026-07-09", "2026-08-03", "K", "K", "5.00", "0.50", N, "30.00",
     (272_000, "10.00", "45.00")),
    ("gpt-5.6-sol", "2026-08-03", "2026-08-20", "K", "K", "5.00", "0.50", "6.25", "30.00",
     (272_000, "10.00", "45.00")),
    ("gpt-5.6-sol", "2026-08-20", "2026-08-21", "K", "K", "2.50", "0.25", "3.125", "15.00",
     (272_000, "5.00", "22.50")),
    ("gpt-5.6-sol", "2026-08-21", "2026-09-04", "K", "D", "2.00", "0.20", "2.50", "10.00",
     (272_000, "4.00", "15.00")),
    ("gpt-5.6-sol", "2026-09-04", N, "D", N, "4.00", "0.40", "5.00", "20.00",
     (272_000, "8.00", "30.00")),
    ("gpt-5.6-terra", "2026-07-09", "2026-07-30", "K", "K", "2.50", "0.25", N, "15.00",
     (272_000, "5.00", "22.50")),
    ("gpt-5.6-terra", "2026-07-30", "2026-08-03", "K", "K", "2.00", "0.20", N, "12.00",
     (272_000, "4.00", "18.00")),
    ("gpt-5.6-terra", "2026-08-03", N, "K", N, "2.00", "0.20", "2.50", "12.00",
     (272_000, "4.00", "18.00")),
    ("gpt-6-astra", "2026-09-04", N, "K", N, "10.00", "1.00", "12.50", "50.00",
     (272_000, "20.00", "75.00")),
    ("gpt-6-luna", "2026-09-22", N, "C", N, "0.10", "0.01", "0.125", "0.50",
     (272_000, "0.20", "0.75")),
    ("gpt-6-sol", "2026-09-22", N, "C", N, "2.00", "0.20", "2.50", "10.00",
     (272_000, "4.00", "15.00")),
    ("claude-haiku-4-5", "2026-06-01", N, "B", N, "1.00", "0.10", "1.25", "5.00", N),
    ("claude-sonnet-4", "2026-06-01", N, "B", N, "3.00", "0.30", "3.75", "15.00", N),
    ("claude-sonnet-4-6", "2026-06-01", N, "B", N, "3.00", "0.30", "3.75", "15.00", N),
    ("claude-sonnet-4-5", "2026-06-01", "2026-09-04", "B", "K", "3.00", "0.30", "3.75", "15.00",
     N),
    ("claude-opus-4-5", "2026-06-01", "2026-09-04", "B", "K", "5.00", "0.50", "6.25", "25.00", N),
    ("claude-opus-4-6", "2026-06-01", "2026-09-04", "B", "K", "5.00", "0.50", "6.25", "25.00", N),
    ("claude-opus-4-7", "2026-06-01", N, "B", N, "5.00", "0.50", "6.25", "25.00", N),
    ("claude-opus-4-8", "2026-06-01", N, "B", N, "5.00", "0.50", "6.25", "25.00", N),
    ("claude-opus-5", "2026-07-24", N, "K", N, "5.00", "0.50", "6.25", "25.00", N),
    ("claude-opus-5-5", "2026-09-22", N, "C", N, "4.00", "0.20", "5.00", "20.00", N),
    ("claude-sonnet-5", "2026-06-30", N, "K", N, "2.00", "0.20", "2.50", "10.00", N),
    ("claude-fable-5", "2026-06-09", N, "K", N, "10.00", "1.00", "12.50", "50.00", N),
    ("claude-fable-5-1", "2026-09-01", N, "K", N, "10.00", "0.25", "12.50", "50.00", N),
    ("gemini-3.5-flash", "2026-06-01", N, "B", N, "1.50", "0.15", N, "9.00", N),
    ("gemini-3.6-flash", "2026-07-21", "2026-08-13", "K", "K", "1.50", "0.15", N, "7.50", N),
    ("gemini-3.6-flash", "2026-08-13", "2027-01-01", "K", "D", "0.75", "0.075", N, "3.75", N),
    ("gemini-3.7-flash", "2026-08-13", "2027-01-01", "K", "D", "0.75", "0.075", N, "3.75", N),
    ("gemini-3.8-flash", "2026-09-03", "2027-01-01", "K", "D", "0.75", "0.075", N, "3.75", N),
    ("grok-4.5", "2026-07-28", N, "K", N, "2.00", "0.50", N, "6.00", (200_000, "4.00", "12.00")),
    ("grok-4.6", "2026-08-14", N, "K", N, "2.00", "0.50", N, "6.00", (200_000, "4.00", "12.00")),
    ("grok-4.7", "2026-09-21", N, "K", N, "2.00", "0.50", N, "6.00", (200_000, "4.00", "12.00")),
    ("mai-code-1.1-flash", "2026-08-11", N, "K", N, "0.20", "0.02", N, "1.20", N),
    ("kimi-k2.7-code", "2026-07-01", N, "K", N, "0.95", "0.19", N, "4.00", N),
    ("kimi-k3", "2026-08-07", N, "K", N, "3.00", "0.30", N, "15.00", N),
]
#: Ruling R-E34: closed intervals of models the table no longer lists (not in facts).
CLOSED_R_E34 = [
    ("gpt-4.1", "2026-06-01", "2026-06-03"), ("gpt-5.2", "2026-06-01", "2026-06-05"),
    ("gpt-5.2-codex", "2026-06-01", "2026-06-05"), ("gemini-2.5-pro", "2026-06-01", "2026-07-31"),
    ("gemini-3-flash", "2026-06-01", "2026-07-31"), ("gemini-3.1-pro", "2026-06-01", "2026-06-04"),
    ("gemini-3.1-pro", "2026-06-04", "2026-09-04"), ("raptor-mini", "2026-06-01", "2026-09-04"),
    ("mai-code-1-flash", "2026-06-02", "2026-09-10"),
]


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def test_revisions_load_in_commit_order() -> None:
    revs = revisions()
    assert len(revs) == 38 and len(rv.replay(revs)) == 55 + 1  # 55 rows + the fast listing
    assert [r.committed for r in revs] == sorted(r.committed for r in revs)
    assert revs[0].name == "2026-04-27_698d333cc3" and revs[-1].name == "2026-09-22_d1153b57c9"
    same_day = [r.name for r in revs if r.date == "2026-09-04"]
    assert same_day == ["2026-09-04_9b446626f0", "2026-09-04_3b48681428",
                        "2026-09-04_be4d995f7e"]  # commit order, not file-name order


@pytest.mark.parametrize("expected", HISTORY_19_2, ids=lambda e: f"{e[0]}@{e[1]}")
def test_replay_reproduces_the_19_2_rows_with_date_sources(expected: tuple) -> None:
    model, start, end, source, end_source, inp, cached, write, out, band = expected
    iv = next(i for i in rv.replay(revisions())
              if (i.model, i.speed, i.effective_from) == (model, "standard", start))
    q = iv.latest.default
    assert (q.input, q.cached_input, q.cache_write, q.output) == (
        _dec(inp), _dec(cached), _dec(write), _dec(out))
    if band is None:
        assert iv.latest.long_context is None and iv.latest.threshold is None
    else:
        assert (iv.latest.threshold, iv.latest.long_context.input,
                iv.latest.long_context.output) == (band[0], _dec(band[1]), _dec(band[2]))
    # the replay dates B/K; C and D come from changelogs / the pricing text (same dates here,
    # except the pricing-text ends of open promotional rows)
    assert iv.date_source == ("B" if source == "B" else "K")
    if end_source == "D" and iv.effective_to is None:
        assert model.startswith("gemini-")
    else:
        assert iv.effective_to == end
    row = next(r for r in rate_doc()["rows"]
               if r["row_id"] == f"github/github_copilot/{model}/{start}")
    assert row["effective_to"] == end
    assert row["notes"].startswith(f"date_source={source}")
    if end is not None:
        assert f"effective_to date_source={end_source}" in row["notes"]


def test_fast_mode_listing_and_closed_rows() -> None:
    intervals = rv.replay(revisions())
    fast = [i for i in intervals if i.speed == "fast"]
    assert [(i.model, i.effective_from, i.effective_to, i.date_source) for i in fast] == [
        ("claude-opus-4-8", "2026-06-29", None, "K")]  # Opus 4.6 fast ended before billing
    got = {(i.model, i.effective_from, i.effective_to) for i in intervals}
    assert set(CLOSED_R_E34) <= got
    models = {i.model for i in intervals}
    assert not models & {"goldeneye", "grok-code-fast-1"}  # gone before 2026-06-01
    assert models == {e[0] for e in HISTORY_19_2} | {m for m, _, _ in CLOSED_R_E34}
    rows = {r["row_id"] for r in rate_doc()["rows"]}
    assert {f"github/github_copilot/{m}/{s}" for m, s, _ in CLOSED_R_E34} <= rows
    assert len(rows) == len(HISTORY_19_2) + len(CLOSED_R_E34)


def test_sources_name_the_first_revision_and_the_band_revision() -> None:
    by = {(i.model, i.effective_from): i for i in rv.replay(revisions()) if i.speed == "standard"}
    assert by[("gpt-5.4", "2026-06-04")].since == "2026-04-27_698d333cc3"
    assert by[("gpt-5.4", "2026-06-04")].band_since == "2026-06-04_82c6d5838e"
    assert by[("gpt-5.5", "2026-06-01")].since == "2026-04-27_5c45104bf9"  # added that afternoon
    assert by[("claude-opus-5-5", "2026-09-22")].since == "2026-09-22_ad7af5d5af"
    assert by[("gpt-6-astra", "2026-09-04")].since == "2026-09-04_be4d995f7e"
    assert by[("claude-opus-4-8", "2026-06-01")].band_since is None


# ---------------------------------------------------------------------------------------------
# replay mechanics on synthetic revisions
# ---------------------------------------------------------------------------------------------


def _rev(committed: str, sha_digit: str, *entries: dict[str, str]) -> rv.Revision:
    return rv.Revision(committed=committed, sha=sha_digit * 40,
                       quotes=rv.quotes_from_maps(list(entries)))


def _kimi(price: str = "$3.00", **kw: str) -> dict[str, str]:
    return {"model": "Kimi K3", "input": price, "output": "$15.00", "category": "Powerful", **kw}


def _grok_band(threshold: str = "200K") -> list[dict[str, str]]:
    return [{"model": "Grok 4.7", "input": "$2", "output": "$6", "threshold": f"≤ {threshold}"},
            {"model": "Grok 4.7", "input": "$4", "output": "$12", "tier": "Long context",
             "threshold": f"> {threshold}"}]


def test_same_day_revisions_keep_the_last_state() -> None:
    revs = [_rev("2026-07-01T01:00:00Z", "a", _kimi("$9.00")),
            _rev("2026-07-01T02:00:00Z", "b", _kimi()),
            _rev("2026-07-02T00:00:00Z", "c", _kimi(), {"model": "Kimi K2", "input": "$1",
                                                         "output": "$2"}),
            _rev("2026-07-02T05:00:00Z", "d", _kimi())]
    ivs = rv.replay(list(reversed(revs)))  # any input order
    assert [(i.model, i.effective_from, i.effective_to, i.since) for i in ivs] == [
        ("kimi-k3", "2026-07-01", None, "2026-07-01_bbbbbbbbbb")]  # K2 listed and withdrawn


def test_clamping_gaps_and_band_changes() -> None:
    revs = [_rev("2026-05-01T00:00:00Z", "a", _kimi("$1.00"), *_grok_band()),
            _rev("2026-05-20T00:00:00Z", "b", _kimi("$2.00"), *_grok_band()),
            _rev("2026-06-01T00:00:00Z", "c", _kimi("$3.00"), *_grok_band("272K")),
            _rev("2026-06-10T00:00:00Z", "d", *_grok_band("272K")),
            _rev("2026-06-20T00:00:00Z", "e", _kimi("$3.00"), *_grok_band("272K"))]
    ivs = rv.replay(revs)
    kimi = [(i.effective_from, i.effective_to, i.date_source, i.to_source)
            for i in ivs if i.model == "kimi-k3"]
    assert kimi == [("2026-06-01", "2026-06-10", "K", "K"),   # starts on the billing start: K
                    ("2026-06-20", None, "K", None)]          # reappears after a gap
    (grok,) = [i for i in ivs if i.model == "grok-4.7"]       # the 200K band ended on 06-01
    assert (grok.effective_from, grok.effective_to, grok.date_source, grok.quote.threshold) == (
        "2026-06-01", None, "K", 272_000)
    assert grok.since == "2026-05-01_aaaaaaaaaa"               # default tier unchanged
    assert grok.band_since == "2026-06-01_cccccccccc"


def test_latest_listing_carries_category_changes() -> None:
    revs = [_rev("2026-06-05T00:00:00Z", "a", _kimi()),
            _rev("2026-06-06T00:00:00Z", "b", _kimi(category="Versatile"))]
    (iv,) = rv.replay(revs)
    assert (iv.quote.category, iv.latest.category) == ("Powerful", "Versatile")


def test_clamped_start_is_billing_date_source() -> None:
    revs = [_rev("2026-05-01T00:00:00Z", "a", _kimi()), _rev("2026-07-01T00:00:00Z", "b")]
    (iv,) = rv.replay(revs)
    assert (iv.first_seen, iv.effective_from, iv.effective_to, iv.date_source, iv.to_source) == (
        "2026-05-01", "2026-06-01", "2026-07-01", "B", "K")
    assert rv.replay(revs, billing_start="2026-07-01") == ()  # ends on the start: dropped


# ---------------------------------------------------------------------------------------------
# load_revisions errors
# ---------------------------------------------------------------------------------------------


def _copy(tmp_path: Path, n: int = 3) -> Path:
    lines = (YML_DIR / "commits.txt").read_text(encoding="utf-8").splitlines()[:n]  # newest
    for line in lines:
        ts, sha = line.split()
        name = f"{ts[:10]}_{sha[:10]}.yml"
        shutil.copy(YML_DIR / name, tmp_path / name)
    (tmp_path / "commits.txt").write_text("\n".join(lines) + "\n\n", encoding="utf-8")
    return tmp_path


def test_load_revisions_from_a_copy(tmp_path: Path) -> None:
    revs = rv.load_revisions(_copy(tmp_path))
    assert [r.name for r in revs] == ["2026-09-21_3662f645b2", "2026-09-22_ad7af5d5af",
                                      "2026-09-22_d1153b57c9"]


def test_load_revisions_errors(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        rv.load_revisions(tmp_path / "absent")
    d = _copy(tmp_path)
    (d / "2026-01-01_0000000000.yml").write_text("- model: X\n", encoding="utf-8")
    with pytest.raises(PricingError, match="disagree"):
        rv.load_revisions(d)
    (d / "2026-01-01_0000000000.yml").unlink()
    (d / "commits.txt").write_text("2026-09-22 d1153b57c9\n", encoding="utf-8")
    with pytest.raises(PricingError, match="line 1"):
        rv.load_revisions(d)
