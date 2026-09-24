# RATES tests (`tests/v2/rates/`)

Package RATES (SPEC §6, §6.8, §6.9; facts §19.1, §19.2, §19.6; D26, D27, D36, D37; Copilot amendment
A-1). Modules: `tokenbill/rates/{schema,engine,contract,billing_rules,verify}.py`, the data in
`tokenbill/rates/data/` and the additive `tokenbill/pricing.py` rows (with `tests/test_pricing.py`).

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/rates tests/test_pricing.py`.
Coverage of the owned modules is 99% (`coverage run -m pytest tests/v2/rates tests/test_pricing.py`).

## Test map (brief acceptance → file)

| acceptance | file |
|---|---|
| golden billing corpus, all 24 cases in int nano, per-line splits of 7, 9, 23, 24 | `test_golden_billing.py` |
| load failures (no source, overlap, `published_absolute`, predicate key, promotion id, cache-rule `supports`), disabled rows unpriced | `test_schema.py` |
| stale-rate note (60 days), `verify_snapshot` zero diffs then one injected diff with its row id, `crosscheck_feed` never fails, live check with an injected opener, extension verifiers | `test_verify.py` |
| hypothesis: unit rates = `price_usage` to the nano, `None` only above scale 24; per-line identities; `price_total` partitions; Copilot band hypotheses | `test_properties.py` |
| layer precedence, effective dates, contract not on the subscription path, `price_total` never mixes allowance into `exact`, `assert_pricer_conforms(RateCard([load_builtin()]))` | `test_engine.py` |
| contract round trip `to_model_pricing(from_model_pricing(x)) == x` | `test_contract.py` |
| billing rules §6.6 (incl. `anthropic.max_tokens`, `openai.max_output_tokens`) | `test_billing_rules.py` |
| legacy parity §6.8 / facts parity D37 (incl. every `facts.copilot` row and modifier) | `test_legacy_parity.py`, `test_facts_parity.py` |
| fuzz of every parser (only `TokenbillError` escapes) | `test_fuzz.py` |

The no-float lint over `tokenbill/rates/` is the foundation's `tests/v2/core/test_no_float_money.py`.

## Fixtures (`tests/v2/fixtures/rates/`, all synthetic)

| file | provenance |
|---|---|
| `pricing_page.md` | written for these tests in the table layout of the Claude pricing page (Markdown); prices are the registry values (no prose copied) |
| `contract_acme.json` | synthetic `tokenbill/contract@1` document |
| `managed_settings_model_pricing.json` | synthetic managed-settings file; `modelPricing` shape from `core.facts` settings key `modelPricing` (verified 2026-09-23) plus the optional `cacheWrite1h` |
| `litellm_model_prices.json`, `openrouter_models.json` | synthetic feeds in the LiteLLM model-map and OpenRouter models-list shapes (non-authoritative cross-check) |

`tokenbill/rates/data/snapshots/pricing-2026-09-23.json` is the recorded parse (by
`verify.parse_pricing_page`) of https://platform.claude.com/docs/en/about-claude/pricing.md fetched
2026-09-24 (`as_of` 2026-09-23): 18 models, 18 batch rows, 3 fast-mode rows, US-geo 1.1×, web search
$0.01.

## Facts verified by RATES (primary sources, 2026-09-24; rows carry `verified_on` and `sources`)

* **Claude API rows outside `facts.json`** (pricing page + prompt-caching page): Mythos 5 $10/$50,
  reads 0.1×, min 512; Opus 4.7 $5/$25 min 2,048; Opus 4.6 $5/$25 min 4,096; Opus 4.5 $5/$25 min
  4,096; Sonnet 4.5 $3/$15 min 1,024; Opus 4.1 and Opus 4 $15/$75 min 1,024; Sonnet 4 $3/$15 min
  1,024; Haiku 3.5 $0.80/$4 min 2,048 (SPEC §19.1 **VERIFY** rows resolved: enabled). Retirement
  dates from the model-deprecations page.
* **Modifiers** (§19.2, checklist #4): batch 0.5 stacks with caching; US geo 1.1× for 4.6+ on the
  Claude API, Claude Platform on AWS and Foundry US Data Zone; fast mode $8/$40 (Opus 5.5) and
  $10/$50 (Opus 5, 4.8), first party only, cache multipliers on top; web search $10 / 1,000.
* **Bedrock** (§19.6, checklist #3): global and geo/in-region prices from the Bedrock pricing page's
  published unit map (us-east-1, published 2026-09-22); regional = global × 1.1 for 4.5+; minimum
  checkpoint sizes from the Bedrock prompt-caching guide (Opus 4.7 is 4,096 on Bedrock).
* **Vertex** (§19.6): global and regional tables of the Vertex pricing page; regional/multi-region
  × 1.1; the Sonnet 4.5 long-context rates.
* **OpenAI** (§19.6, checklist #19 partly): gpt-5.6-sol $4/$0.40/$5/$20, > 272K $8/$0.80/$10/$30,
  batch and flex 50%, fast 200% (priority requests use fast since 2026-07-30), regional processing
  +10% for models released on or after 2026-03-05, promotion "at least through November 21, 2026".
* **Refusal billing** (billing rules): pre-output refusals not billed, mid-stream refusals bill input
  and streamed output (refusals-and-fallback page).

## Unverified, assumed or not modeled (VERIFY)

* gpt-5.6-sol launch row 2026-07-09 → 2026-08-21 stays **disabled** (unpriced): the changelog gives
  the new price as "20% lower input, 33% lower output" but no launch cache prices (§19.8 #19).
* Vertex batch 0.5× is `stacking: assumed`: the global Opus 5.5 batch listing ($2.50 / $12.50 =
  0.625×) contradicts its own regional batch rows and batch cache prices (0.5×) (§19.8 #9).
* Vertex Sonnet 4.5 long-context threshold: the table header says "> 200K", a footnote "≥ 200K";
  modeled as `> 200,000`.
* Not modeled: Bedrock "Long Context" rows (Sonnet 4.6, Opus 4.6, Sonnet 4.5, Sonnet 4; no prices
  in the published unit map); a first-party Sonnet 4.5 long-context premium (not on the current page);
  Bedrock priority/flex tiers for Claude (not offered in the Anthropic table); Azure OpenAI rows and
  the Azure Data Zone premium (no Azure channel rows in v0.2).
* Bedrock/Vertex global prices of models listed only at the regional price are derived as
  regional ÷ 1.1 (Anthropic's documented 10% premium); web search on Vertex is priced only for the
  models the Vertex page lists (newer models: `web search requests unpriced` note).
* `stacking: assumed`: `anthropic.priority` (Priority Tier burn-down, factor 1, provenance only),
  `openai.regional_processing`, `vertex.batch`, `github.auto` × `github.compliance` (facts).
* `effective_from` of rows outside `facts.json` is the documented retirement floor minus one year
  (the facts rows' method) or the snapshot-id date for retired models — not a billing date.
* GitHub Copilot rows come from `core.facts` (verification `research`, R-E19) until CP-RATES ships
  `tokenbill/copilot/data/github_copilot.json`; `load_builtin(notes=…)` reports
  `dq.extension_unavailable` meanwhile.
* The `modelPricing` 1h rate is derived as `cacheWrite × 2 / 1.25` (SPEC §6.5) unless stated.

## Known cross-package issue

`tests/v2/telemetry/test_gate.py::test_gate_ratecard_prices_telem_fixtures_like_the_fake_pricer`
(TELEM) fails with the real RateCard on the OpenAI flex/priority and unknown-scope lines — see
`CONTRACT-CHANGE-RATES-1.md` item 1 for the cause and the proposed fix.
