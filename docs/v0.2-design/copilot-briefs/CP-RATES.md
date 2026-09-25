### CP-RATES — GitHub Copilot rate data, YAML verifier, golden cases (wave 2)

**Goal.** Ship the effective-dated, sourced GitHub Copilot rate card (channel `github_copilot`, provider
`github`) that RATES' `RateCard` loads through `core.extensions.extension_rate_files()`, plus a stdlib
verifier that diffs it against GitHub's published pricing YAML. Copilot rows are independent of the
Anthropic/OpenAI rows. Read SPEC §3.5 (`RateRow`, `Modifier`), §6.1–§6.3, §6.7, §6.9, D36; addendum DC1, DC5,
DC6, §6, §19.2, §19.5 #4, #5, #14, #15, #24, Appendix C.G1–G17; `CORE-AMENDMENTS.md` items C-28, K-3, A-1 and
ruling R-E19 (Copilot facts stay `verification: "research"` until the release gate).

**Owns.** `tokenbill/copilot/data/__init__.py` (docstring-only), `tokenbill/copilot/data/github_copilot.json`,
`tokenbill/copilot/data/snapshots/**`, `tokenbill/copilot/rates_verify.py`, `tests/v2/copilot_rates/**`,
`tests/v2/fixtures/copilot_rates/**`.

**Consumes.** `core.types` (`RateRow`, `Modifier`, `RateLayer`, `Discrepancy`, `SourceCitation`),
`core.models.normalize_copilot_model`, `core.money`, `core.facts` (`copilot_rates()` — your file must agree
with it exactly on every row), `core.catalog.COPILOT_PROMOTIONS` (the four Copilot promotion ids; the SPEC
table `PROMOTIONS` is pinned to the
OpenAI promotion and never holds them),
`core.testing.FakePricer` (reference for goldens until RATES lands).

**Provides.** `github_copilot.json` in the `tokenbill/rates@1` schema (addendum §6.1) and
`rates_verify.verify(layer, *, snapshot: Path | None = None, live: bool = False, opener=None) ->
list[Discrepancy]` (`EXTENSIONS["copilot"].rate_verifier`).

**Build.**
1. Replay the 38 dated YAML revisions (the fact-checked count; `commits.txt` has 38 lines) checked in by the orchestrator at
   `tests/v2/fixtures/copilot_rates/yml/<YYYY-MM-DD>_<sha>.yml` with `commits.txt` (ISO timestamp + full SHA
   per line; these names carry the dates — do not use SHA-only copies). One row per (model, tier) interval;
   `effective_from` = first date the row appears, clamped to 2026-06-01; `effective_to` exclusive;
   `notes` = `date_source=<B|C|D|K>; category <…>` per addendum §6.1 (C for Opus 5.5 / GPT-6 Sol / GPT-6
   Luna on 2026-09-22; D for the GPT-5.6 Sol $4/$20 row from 2026-09-04 and the Gemini promo end); sources =
   docs URL + `yml <date>_<sha>` + `retrieved: 2026-09-23`.
2. Row rules: published absolute prices; Claude rows: `cache_write_5m` = published write multiplier,
   `cache_write_1h` = 2.0 × input with the VERIFY note (DC6), `published_absolute` lists only published
   buckets; non-Claude
   rows with a write price carry it in both classes; "Not applicable" → null; long-context rows (key
   `long_context.threshold`, as the merged `core/facts.py::_rate_row` reads it — not
   addendum §6.1's `threshold_input_tokens`; 272,000 /
   200,000, **VERIFY**); `claude-opus-4-8` `supports: ["fast_mode"]` with the `github.fast.opus-4-8`
   replace_base modifier; modifiers `github.auto` ×0.9, `github.compliance` ×1.1 (`stacking: assumed`);
   promotional rows reference promotion ids; the GPT-5.6 rows of 2026-07-30 → 08-03 ship the write buckets
   disabled; GPT-5.4/5.5 band rows start 2026-06-04; retired models keep closed rows; `aliases` = display
   names; `tokenizer_family` per vendor (claude-4.7+ for Opus 4.7+/Sonnet 5/Fable; document the rest as
   unverified).
3. `rates_verify`: stdlib mini-parser (flat list of maps; strips quotes, `[^…]` footnotes, `$`, `≤ / >`
   thresholds, `Not applicable`, `Default` / `Long context`), compared with rows in force at the snapshot
   date; `--live` fetches the raw YAML through an injected opener (tests: fake opener, socket guard); price
   discrepancies are authoritative, category/notes warnings.

**Facts to verify.** Threshold semantics; the 1h write price (SDK `cacheWrite1hPrice` exists, value
unpublished); K-dated effective dates; tokenizer families. List each in your README with its fallback.

**Acceptance tests.**
- Load validation (non-overlapping intervals per (channel, model); derived cache prices equal
  `published_absolute` where published; exact Decimals; promotion ids exist); **gate** (`importorskip
  tokenbill.rates.engine`): `RateCard` loads the file via `core.extensions.extension_rate_files()` and passes
  `assert_pricer_conforms`.
- Appendix C.G1–G10, G14–G16 with FakePricer (unit) and the real `RateCard` (gate): exact nano and labels
  (G1 range, G5/G10 EXACT, G5b range with `context_tier`, G9 expiry, G16 unpriced).
- Facts parity: every row of the file equals the matching `facts.copilot.rates` row, and vice versa.
- `verify` against the packaged snapshot → zero discrepancies; an injected price change → one discrepancy
  naming the row; replaying the revisions reproduces the §19.2 history rows with their date sources.

**Size.** ~1.5k LOC including tests (plus the JSON data).


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.5 R-E34 — your data file carries every billable interval replayed from the dated pricing-YAML revisions (including closed intervals of models no longer listed); copy the inputs from `/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/raw/yml/` (RULINGS G-2).
