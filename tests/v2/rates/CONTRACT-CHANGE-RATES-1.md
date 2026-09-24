# CONTRACT-CHANGE-RATES-1 — OpenAI tier modifiers vs. the FakePricer parity assumption, and RATES readings

Filed by RATES (wave 2b) per SPEC §21 #3. RATES implements the current contract; the items below
need a ruling or an additive change by the contract owner. Item 1 makes one sibling gate test fail on
this branch.

## 1. TELEM gate test assumes RateCard == FakePricer on every OpenAI inference (fails)

`tests/v2/telemetry/test_gate.py::test_gate_ratecard_prices_telem_fixtures_like_the_fake_pricer`
(owner TELEM) asserts `(exact_nano, figure.nano)` equal for every inference FakePricer prices. With the
real `RateCard` two kinds of TELEM fixture lines differ, both by design of the SPEC:

* **OpenAI service tiers.** The brief and SPEC §6.2 #3 require the OpenAI tier modifiers (flex 0.5,
  fast 2.0, batch 0.5; verified 2026-09-24 on developers.openai.com/api/docs/pricing; `priority` is
  priced as `fast`, the 2026-07-30 changelog). `core/facts.json` carries no OpenAI modifier, so
  FakePricer prices the fixture's `service_tier: "flex"` / `"priority"` gpt-5.6-sol responses at the
  standard rate (e.g. 30,400,000 nano where RateCard gives 15,200,000).
* **OpenAI regional processing.** SPEC §6.2 #4 lists "OpenAI regional processing" among the
  scope-priced cases: `endpoint_scope == "unknown"` on `openai_api` prices every line as the range
  `[global, regional]` (+10% for models released on or after 2026-03-05 — gpt-5.6-sol; verified on
  the pricing page). The TELEM OpenAI adapter emits `endpoint_scope = "unknown"` unless told
  otherwise, so RateCard's `exact_nano` is 0 where FakePricer's is the full price (the point is
  equal).

Proposed resolution (either):

* (a, preferred) the contract owner appends the five OpenAI modifiers exactly as shipped in
  `tokenbill/rates/data/openai.json` (`openai.batch`, `openai.flex`, `openai.fast`,
  `openai.priority`, `openai.regional_processing`) to `core/facts.json` `modifiers`. FakePricer loads
  every facts modifier (its predicate set already includes `endpoint_scope`), so parity returns and
  RATES' facts-parity test covers them. `tests/v2/core/test_facts_evidence.py` pins the modifier id
  set and needs the matching T-edit.
* (b) TELEM restricts the comparison to inferences whose pricing context the facts modifiers cover
  (skip `openai_api` inferences with a service tier other than `standard` or an unknown endpoint
  scope).

No other merged gate test that imports RATES fails (REPLAY `test_gate_ratecard`, the CC and ADMIN
seams run green or skip on the missing STORE/RECON modules).

## 2. Additive surface beyond the SPEC signatures (no signature changed)

* `rates.schema.load_builtin(provider=None, *, notes=None)`: *notes* receives the
  `dq.extension_unavailable` note of `core.extensions.extension_rate_files()` (A-1 "missing → dq").
  While `tokenbill/copilot/data/github_copilot.json` (CP-RATES) is not installed, the Copilot rows
  and modifiers come from `core.facts.copilot_rates()` / `copilot_modifiers()`; once the file exists
  it replaces them (facts parity then compares shared rows on their pricing fields, R-E34).
* `rates.schema`: `parse_layer`, `make_layer`, `parse_model_price` (the `--model-price` value parser,
  `UsageError` on malformed values), `layer_kind`; channel `"*"` rows (used by `model_price_layer`)
  match every channel.
* `rates.engine.RateCard`: `sha256`, `unpriced_reason(ctx, ts_ms=)`, `unit_rates`, `stale_rows(today=)`,
  `data_quality(today=)` (`dq.stale_rate`), `info(today=)` (`RateCardInfo`), `used_rows()`,
  `list_card()`, `rows()`, `modifiers()`; module constants `UNPRICED_REASONS` (reason → dq code, the
  same strings as `core.testing.UNPRICED_DQ`), `DQ_SCOPE_UNKNOWN`, `DQ_STALE_RATE`,
  `DQ_COPILOT_BAND_HYPOTHESIS`, `DQ_COPILOT_WRITE_FOLDED`.
* `rates.contract`: `make_overlay`, `contract_json`, `dumps_model_pricing`; `from_model_pricing(obj, *,
  name, effective_from="1970-01-01", channels=())`. `ContractOverlay.assumed_fields` uses
  `"cache_write_1h"` when every model's 1h rate was derived from `cacheWrite`, else
  `"cache_write_1h@<model>"` per derived model (so the round trip holds when only some models state
  `cacheWrite1h`).
* `rates.billing_rules`: `BillingRule` (with `billable`, `param()`), `rules()`, `rule(id)`,
  `refusal_rule(output_tokens)` (D10 thresholds from the data), `parse_rules`.
* `rates.verify`: `parse_pricing_page`, `load_snapshot`, `compare_snapshot`, `verify_extensions`,
  `verify_report` / `show_report` / `diff_layers` (`PricingReport`), `stale_rows(layer, today=)`,
  `promotions_ending(today, days=14)` (the weekly live check's promotion alarm).

## 3. Readings of the SPEC text (please ratify)

1. **Scope-priced rows, not channels.** An unknown endpoint scope gives a range only when a modifier
   predicated on `endpoint_scope` would apply to the row but for the scope. Bedrock models before 4.5
   (no regional premium, "earlier models retain their existing pricing") therefore stay EXACT with an
   unknown scope; FakePricer decides per channel (identical on every facts row).
2. **Contract overrides modify resolved rows.** SPEC §6.2 #2 names the contract overlay first in the
   layered lookup; RATES applies per-model overrides to the row resolved from the layers (as
   FakePricer) and never synthesizes a row from an override, so a model absent from every layer stays
   unpriced (use `--rates` to add a row).
3. **`price_total` basis.** `PricedTotal.exact` / `estimated` carry the common basis of their
   inferences; a mix of LIST and CONTRACT (a contract restricted to some channels) is labelled with the
   card's basis plus the note `mixed bases: list and contract`.
4. **Unknown-TTL writes on a row without a 1h write price** (Bedrock Opus 4.1 / Opus 4 / Sonnet 4 /
   Haiku 3.5) collapse to the 5m rate (zero-width, ESTIMATED): the model has no 1h writes. FakePricer's
   `[5m, input]` fallback would build an inverted range (unreachable on facts rows, all of which carry
   a 1h multiplier).
5. **Partial streams** follow billing rule `<provider>.abort.client` (§6.3): unknown today, so
   `[0, full]` as in FakePricer; a documented rule would make them EXACT.
6. **Model ids.** SPEC §19.1 writes `claude-haiku-3-5`; the API id is `claude-3-5-haiku-20241022`, so
   the row's model is `claude-3-5-haiku` with alias `claude-haiku-3-5`.
7. **Retired first-party models.** Opus 4.1 / Opus 4 / Sonnet 4 / Haiku 3.5 are retired on the Claude
   API but their prices are still published; their `anthropic_api` rows stay open-ended (a retired
   model produces no first-party usage), which keeps the §6.8 legacy parity for the Opus 4.1 / Opus 4
   rows `pricing.py` gains now that their minimum cacheable prefix (1,024) is verified.
