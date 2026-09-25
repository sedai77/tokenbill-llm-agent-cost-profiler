"""Build ``tokenbill/copilot/data/github_copilot.json`` and the packaged pricing snapshot from the
dated pricing-YAML revisions in ``tests/v2/fixtures/copilot_rates/yml/`` (CP-RATES dev tool).

The rate file is a pure function of the revisions (``rates_verify.replay``) plus the few facts the
YAML cannot carry, each listed below with its date source (addendum §6.1): changelog-dated starts
(**C**), dates taken from the pricing text (**D**) and free-text notes. ``test_rate_file.py``
asserts that the committed files equal this output byte for byte, so a new YAML revision is added
by copying it (and its ``commits.txt`` line) into the fixtures and running::

    uv run --python 3.12 python -m tests.v2.copilot_rates.build_rate_file --write

then reviewing the diff (and ``core/facts.json``'s ``copilot.rates`` at the next contract window).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from decimal import Context, Decimal, Inexact
from pathlib import Path
from typing import Any

from tokenbill.copilot import rates_verify as rv
from tokenbill.core.catalog import copilot_promotion_for
from tokenbill.core.facts import load as load_facts

REPO = Path(__file__).resolve().parents[3]
YML_DIR = REPO / "tests" / "v2" / "fixtures" / "copilot_rates" / "yml"
RATE_PATH = REPO / "tokenbill" / "copilot" / "data" / rv.RATE_FILE
SNAPSHOT_PATH = REPO / "tokenbill" / "copilot" / "data" / rv.SNAPSHOT_RESOURCE
SNAPSHOT_REVISION = "2026-09-22_d1153b57c9"
RETRIEVED = "2026-09-23"
AS_OF = "2026-09-23"
FINDING = "copilot-billing F4"

#: Starts dated by a source other than the docs commit: C = changelog, D = the pricing text.
START_SOURCES: dict[tuple[str, str], str] = {
    ("claude-opus-5-5", "2026-09-22"): "C",   # changelog 2026-09-22
    ("gpt-6-sol", "2026-09-22"): "C",         # changelog 2026-09-22
    ("gpt-6-luna", "2026-09-22"): "C",        # changelog 2026-09-22
    ("gpt-5.6-sol", "2026-09-04"): "D",       # promotion "through September 3" ended
}
#: Ends dated by the pricing text (D): (model, effective_from) → (effective_to, extra note).
_GEMINI_PROMO = "promotional price through 2026-12-31 (pricing text); no row after the promotion"
END_SOURCES: dict[tuple[str, str], tuple[str, str | None]] = {
    ("gpt-5.6-sol", "2026-08-21"): ("2026-09-04", "promotion 'through September 3' (pricing text)"),
    ("gemini-3.6-flash", "2026-08-13"): ("2027-01-01", _GEMINI_PROMO),
    ("gemini-3.7-flash", "2026-08-13"): ("2027-01-01", _GEMINI_PROMO),
    ("gemini-3.8-flash", "2026-09-03"): ("2027-01-01", _GEMINI_PROMO),
}
#: Free-text notes the YAML cannot express.
EXTRA_NOTES: dict[tuple[str, str], str] = {
    ("claude-opus-5-5", "2026-09-22"): "billed at provider list pricing (changelog 2026-09-22)",
    ("claude-sonnet-5", "2026-06-30"):
        "promo footnote 2026-06-30 to 2026-08-11 with unchanged prices",
    ("gpt-5.6-sol", "2026-08-20"):
        "K-dated one-day row (a mechanics test of the replay, not a billing fact)",
    ("raptor-mini", "2026-06-01"):
        "generation unknown (no version in the name; '0'); docs note: uses GPT-5 mini pricing",
}
#: Tokenizer family per YAML provider (only the Claude families are documented; the rest are
#: assumptions noted on every row, README "unverified").
TOKENIZERS = {"openai": "openai-o200k", "github": "openai-o200k", "google": "gemini",
              "xai": "grok", "microsoft": "mai", "moonshot_ai": "kimi"}
_WRITE_1H = "cache_write_1h unpublished: 2 x input assumed (SDK cacheWrite1hPrice exists; VERIFY)"
_EXACT = Context(prec=60, traps=[Inexact])

MODIFIERS: list[dict[str, Any]] = [
    {"modifier_id": "github.auto", "kind": "multiply", "factor": "0.9", "base_usd_per_mtok": {},
     "applies_to": ["*"], "when": {"channel_in": "github_copilot", "routing": "auto"},
     "stacking": "assumed",
     "sources": [{"url": "https://docs.github.com/en/copilot/concepts/auto-model-selection",
                  "retrieved": RETRIEVED, "finding": "copilot-billing F2; changelog 2026-09-14"}],
     "notes": "10% on paid plans in Chat, CLI, Copilot app and cloud agent, for every Auto tier "
              "(Efficiency / Balance / Intelligence); charged at the model Auto selects; how it "
              "appears in the report VERIFY"},
    {"modifier_id": "github.compliance", "kind": "multiply", "factor": "1.1",
     "base_usd_per_mtok": {}, "applies_to": ["*"],
     "when": {"channel_in": "github_copilot", "compliance_in": "data_residency,fedramp"},
     "stacking": "assumed",
     "sources": [
         {"url": "https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/"
                 "github-copilot-with-data-residency",
          "retrieved": RETRIEVED, "finding": "copilot-billing fact-check (data residency)"},
         {"url": "https://docs.github.com/en/copilot/concepts/enterprise/fedramp-models",
          "retrieved": RETRIEVED, "finding": "copilot-billing fact-check (FedRAMP)"}],
     "notes": "+10% while a restrict-to-compliant-models policy is on; stacking with Auto and "
              "rounding undocumented (assumed multiplicative, rounded once per line)"},
]


def _ratio(num: Decimal, den: Decimal) -> str:
    """``num / den`` as a plain decimal string; raises if the quotient is not exact."""
    return format(_EXACT.divide(num, den).normalize(), "f")


def _generation(model: str) -> str:
    parts = model.split("-")
    if model.startswith(rv.CLAUDE_PREFIX):
        return ".".join(p for p in parts[2:] if p.isdigit())
    for part in parts:
        digits = part.lstrip("k") if part.startswith("k") else part
        if digits[:1].isdigit():
            return digits
    return "0"  # no version in the name (Raptor mini)


def _tokenizer(model: str, provider: str) -> str:
    if model.startswith(rv.CLAUDE_PREFIX):
        gen = tuple(int(x) for x in _generation(model).split("."))
        return "claude-4.7+" if gen >= (4, 7) else "claude-legacy"
    return TOKENIZERS[provider]


def _write_expected(model: str) -> bool:
    """Whether the table's column comment makes a write price due (Anthropic, GPT-5.6+)."""
    if model.startswith(rv.CLAUDE_PREFIX):
        return True
    if model.startswith("gpt-"):
        gen = tuple(int(x) for x in _generation(model).split("."))
        return gen >= (5, 6)
    return False


def _prices(tier: rv.TierPrices, claude: bool) -> dict[str, str]:
    out = {"input": str(tier.input)}
    if tier.cached_input is not None:
        out["cache_read"] = str(tier.cached_input)
    if tier.cache_write is not None:
        out["cache_write_5m"] = str(tier.cache_write)
        out["cache_write_1h"] = str(tier.cache_write if not claude else 2 * tier.input)
    out["output"] = str(tier.output)
    return out


def _row(iv: rv.Interval, following: rv.Interval | None, fast: rv.Interval | None,
         retire: dict[str, str], utility: frozenset[str]) -> dict[str, Any]:
    q, model = iv.latest, iv.model
    claude = model.startswith(rv.CLAUDE_PREFIX)
    d = q.default
    key = (model, iv.effective_from)
    start_source = START_SOURCES.get(key, iv.date_source)
    end, end_source, end_note = iv.effective_to, iv.to_source, None
    if key in END_SOURCES:
        end, end_note = END_SOURCES[key]
        assert iv.effective_to in (None, end), key
        end_source = "D"
    multipliers: dict[str, str] = {}
    published: dict[str, str] = {}
    if d.cached_input is not None:
        multipliers["cache_read"] = _ratio(d.cached_input, d.input)
        published["cache_read"] = str(d.cached_input)
    if d.cache_write is not None:
        multipliers["cache_write_5m"] = _ratio(d.cache_write, d.input)
        multipliers["cache_write_1h"] = "2.0" if claude else multipliers["cache_write_5m"]
        published["cache_write_5m"] = str(d.cache_write)
    notes = [f"date_source={start_source}"]
    if end is not None:
        notes.append(f"effective_to date_source={end_source}")
    notes.append(f"category {q.category}")
    if claude and d.cache_write is not None:
        notes.append(_WRITE_1H)
    elif d.cache_write is None:
        notes.append("no write price listed in this interval: write bucket disabled (VERIFY)"
                     if _write_expected(model)
                     else "no cache write price (Not applicable)")
    long_context = None
    if q.long_context is not None:
        band = _prices(q.long_context, claude)
        assert set(band) == {"input", "output", *multipliers}, key
        long_context = {"threshold": q.threshold, "usd_per_mtok": band}
        notes.append(f"long-context band > {q.threshold} input tokens (hypothesis A; threshold "
                     "VERIFY)")
    if not claude:
        notes.append("tokenizer family assumed")
    if model in utility:
        notes.append("also a utility model (unbilled only when nano-AIU is 0)")
    if following is not None and following.effective_from == end and q.long_context is None \
            and following.quote.long_context is not None and following.quote.default == d:
        notes.append(f"no long-context price listed before {end}")
    if end is not None and iv.to_source == "K" and following is None:
        retired = retire.get(model)
        notes.append((f"retired {retired}; " if retired and retired <= end else "")
                     + "row closed when the docs dropped it")
    supports = []
    if fast is not None and (end is None or fast.effective_from < end):
        supports = ["fast_mode"]
        notes.append(f"fast mode priced by modifier github.fast.{model[len(rv.CLAUDE_PREFIX):]} "
                     f"(listed {fast.effective_from}, {fast.date_source})")
    if end_note:
        notes.append(end_note)
    if key in EXTRA_NOTES:
        notes.append(EXTRA_NOTES[key])
    promo = copilot_promotion_for(model, iv.effective_from)
    finding = f"{FINDING}; yml {iv.since}"
    if iv.band_since is not None and iv.band_since != iv.since:
        finding += f"; band yml {iv.band_since}"
    return {
        "row_id": f"{rv.PROVIDER}/{rv.CHANNEL}/{model}/{iv.effective_from}",
        "channel": rv.CHANNEL, "model": model, "aliases": [q.display],
        "generation": _generation(model), "effective_from": iv.effective_from,
        "effective_to": end,
        "usd_per_mtok": {"input": str(d.input), "output": str(d.output)},
        "multipliers": multipliers, "published_absolute": published,
        "min_cacheable_tokens": None, "tokenizer_family": _tokenizer(model, q.provider),
        "per_request_usd": {}, "long_context": long_context,
        "promotion": promo.promotion_id if promo is not None else None,
        "supports": supports, "enabled": True, "verified_on": RETRIEVED,
        "notes": "; ".join(notes),
        "sources": [{"url": rv.DOCS_URL, "retrieved": RETRIEVED, "finding": finding}],
    }


def _fast_modifier(fast: rv.Interval, base_rows: list[dict[str, Any]]) -> dict[str, Any]:
    q = fast.latest
    model = fast.model
    for row in base_rows:  # the table's fast prices must follow the row's cache multipliers
        mult = row["multipliers"]
        assert _ratio(q.default.cached_input, q.default.input) == mult["cache_read"], model
        assert _ratio(q.default.cache_write, q.default.input) == mult["cache_write_5m"], model
    return {
        "modifier_id": f"github.fast.{model[len(rv.CLAUDE_PREFIX):]}", "kind": "replace_base",
        "factor": None,
        "base_usd_per_mtok": {"input": str(q.default.input), "output": str(q.default.output)},
        "applies_to": ["*"],
        "when": {"channel_in": rv.CHANNEL, "model_in": model, "speed": "fast"},
        "stacking": "documented",
        "sources": [{"url": rv.DOCS_URL, "retrieved": RETRIEVED,
                     "finding": f"yml {fast.since} ({q.display})"}],
        "notes": f"listed from {fast.effective_from} (date_source={fast.date_source}); cache "
                 f"prices follow the row multipliers (${q.default.cached_input} read, "
                 f"${q.default.cache_write} write)",
    }


def build_rate_doc(revisions: Sequence[rv.Revision]) -> dict[str, Any]:
    """The ``tokenbill/rates@1`` document of channel ``github_copilot``."""
    facts = load_facts().copilot
    retire = {m: r.retire_on for m, r in facts.retirements.items() if r.successor is None}
    intervals = rv.replay(revisions)
    fast = {iv.model: iv for iv in intervals if iv.speed == "fast"}
    by_model: dict[str, list[rv.Interval]] = {}
    for iv in intervals:
        if iv.speed == "standard":
            by_model.setdefault(iv.model, []).append(iv)
    rows: list[dict[str, Any]] = []
    fast_rows: dict[str, list[dict[str, Any]]] = {}
    for model in sorted(by_model):
        group = by_model[model]
        for i, iv in enumerate(group):
            row = _row(iv, group[i + 1] if i + 1 < len(group) else None, fast.get(model),
                       retire, facts.utility_models)
            rows.append(row)
            if row["supports"]:
                fast_rows.setdefault(model, []).append(row)
    modifiers = list(MODIFIERS)
    for model in sorted(fast):
        assert model in fast_rows, model  # a fast listing needs a base row
        modifiers.append(_fast_modifier(fast[model], fast_rows[model]))
    return {"schema": rv.RATES_SCHEMA, "provider": rv.PROVIDER, "as_of": AS_OF, "rows": rows,
            "modifiers": modifiers}


def build_snapshot_doc(revisions: Sequence[rv.Revision], directory: Path = YML_DIR
                       ) -> dict[str, Any]:
    """The recorded parse of revision :data:`SNAPSHOT_REVISION` (the packaged snapshot)."""
    rev = next(r for r in revisions if r.name == SNAPSHOT_REVISION)
    text = (directory / f"{rev.name}.yml").read_text(encoding="utf-8")
    return {
        "schema": rv.SNAPSHOT_SCHEMA, "revision": rev.name, "commit": rev.sha,
        "committed": rev.committed, "date": rev.date, "retrieved": RETRIEVED,
        "source": f"https://github.com/github/docs/blob/{rev.sha}/data/tables/copilot/"
                  "models-and-pricing.yml",
        "docs_url": rv.DOCS_URL, "entries": rv.parse_yaml_list(text),
    }


def render(doc: dict[str, Any]) -> str:
    """The committed text of a generated document (stable key order, one trailing newline)."""
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Print or ``--write`` the generated rate file and snapshot."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="write the package data files")
    args = parser.parse_args(argv)
    revisions = rv.load_revisions(YML_DIR)
    outputs = {RATE_PATH: render(build_rate_doc(revisions)),
               SNAPSHOT_PATH: render(build_snapshot_doc(revisions))}
    for path, text in outputs.items():
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        print(f"{path.relative_to(REPO)}: {len(text)} bytes"
              + (" (written)" if args.write else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
