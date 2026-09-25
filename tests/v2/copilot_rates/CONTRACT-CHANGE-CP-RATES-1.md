# CONTRACT-CHANGE-CP-RATES-1 — observations on the frozen rate contract (SPEC §21 #3)

Implemented against the current contract; nothing in `tokenbill/core/*` was edited. For the
contract owner at the next window.

1. **`Modifier` has no effective dates** (SPEC §3.5, `tokenbill/rates@1`). GitHub lists
   "Claude Opus 4.8 (fast mode) (preview)" only from 2026-06-29 (docs commit, K-dated), but the
   `replace_base` modifier `github.fast.opus-4-8` (facts and the rate file) applies whenever
   `speed == "fast"`. Impact is small (the fast option did not exist in Copilot before), and
   `rates_verify.verify` reports the modifier as "priced / not listed" for earlier revisions.
   Proposal (v0.3): optional `effective_from` / `effective_to` on modifiers (JSON keys of the same
   names, `None` = open), resolved like rows.

2. **Per-row `provider` in rate files.** SPEC §6.1's file example and addendum §6.1's Copilot row
   put `provider` at the file level only; `facts.json`'s `copilot.rates` rows (read by
   `core.facts._rate_row`) also carry it per row. `github_copilot.json` follows SPEC / addendum
   §6.1 (file level only; each row has exactly the §6.1 keys). RATES' loader must take the provider
   from the file; `test_gate_rates.py::test_rates_load_file_agrees_with_the_copilot_loader` checks
   it at gate 1.

3. **No verification status on `RateRow`.** Ruling R-E19 keeps Copilot facts at
   `verification: "research"`; the rate file has no field for it (the §6.1 schema has none), so
   the status lives in `facts.json` and in `tests/v2/copilot_rates/README.md`. No change proposed
   unless the release gate needs per-row status in outputs.

4. **Snapshot format owned by CP-RATES.** `tokenbill/copilot/data/snapshots/*.json` use a package
   format `tokenbill/copilot-pricing-snapshot@1` (`revision`, `commit`, `committed`, `date`,
   `retrieved`, `source`, `docs_url`, `entries`: the mini-parser's maps of strings). `verify`
   takes rows in force on `date`; with `live=True` on the UTC date of the response's `Date` header
   (today without one). Signature unchanged: `verify(layer, *, snapshot=None, live=False,
   opener=None)`.
