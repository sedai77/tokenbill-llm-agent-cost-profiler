### RATES — Pricing registry, billing rules, v0.1 pricing compat (wave 2)

**Goal.** Exact, sourced, effective-dated pricing that every bill and counterfactual depends on (SPEC §6;
facts §19.1, §19.2, §19.6; D26, D27, D36). Only rate arithmetic on billed tokens is ever EXACT; exactness
is decided per line; allowance usage is priced on basis `list_equivalent`.

**Owns.** `tokenbill/pricing.py`, `tokenbill/rates/{schema,engine,contract,billing_rules,verify}.py`,
`tokenbill/rates/data/**` (`anthropic.json`, `bedrock.json`, `vertex.json`, `openai.json`,
`billing_rules.json`, `snapshots/pricing-2026-09-23.json`), `tests/test_pricing.py`, `tests/v2/rates/**`,
`tests/v2/fixtures/rates/**`.

**Consumes.** `core.types` (`RateRow`, `Modifier`, `RateLayer`, `ResolvedRates`, `UnitRates`, `PricedLine`,
`PricedInference`, `PricedTotal`, `ContractOverlay`, `Discrepancy`, `PricingReport`), `core.labels`,
`core.money`, `core.records`, `core.models`, `core.catalog` (`PROMOTIONS`, `promotion_for`), `core.facts`,
`core.protocols.Pricer`, `core.testing.assert_pricer_conforms`.

**Provides.** `rates.schema.load_builtin(provider=None) -> RateLayer`, `load_file(path, name) -> RateLayer`,
`model_price_layer(specs: Sequence[tuple[str, str, str]]) -> RateLayer` (`--model-price`);
`rates.engine.RateCard(layers, contract=None)` implementing every `Pricer` method (incl. `unit_rates`,
`supports`, `min_cacheable_tokens`, `tokenizer_family`, long-context band and `output_upper` via
`price_usage`), `rates.engine.price_total(pricer, items) -> PricedTotal`,
`rates.engine.no_cache_equivalent_nano(pricer, inference, ts_ms) -> int`; `rates.contract.load_contract`,
`from_model_pricing`, `to_model_pricing`; `rates.billing_rules.rule_for(provider, failure_mode) ->
BillingRule`; `rates.verify.verify_snapshot`, `verify_live` (opt-in network, injectable opener),
`crosscheck_feed` (warnings only).

**Build.** Data rows per SPEC §19.1 (Anthropic, channel `anthropic_api`; `claude_platform_aws`/`foundry` fall
back per §6.2), Fable 5.1 / Mythos 5.1 from 2026-09-01, Bedrock and Vertex rows at global prices plus
regional ×1.1 modifiers keyed on `endpoint_scope` (unknown scope → `scope_range`), OpenAI gpt-5.6-sol with the
disabled launch row, the promotional row to 2026-11-22 (`promotion` id), the > 272K whole-request band, flex
0.5 / fast 2.0 / batch 0.5 / regional +10% modifiers; Anthropic batch 0.5, US geo 1.1 (generation ≥ 4.6, 1P
and Claude Platform on AWS), fast-mode `replace_base` rows (Opus 5.5 $8/$40; Opus 5 and 4.8 $10/$50), Priority
burn-down noted `stacking: assumed`; web search $0.01/request. **VERIFY** rows ship `enabled: false`. Load
validation, resolution (incl. `dq.promotion_expired`, unknown scope, billing path), per-line pricing table and
unit-rate scale per SPEC §6.1–§6.4 (never raise for non-integral pico rates). Contract overlay per §6.5.
Billing rules §6.6 (incl. `anthropic.max_tokens`, `openai.max_output_tokens`). Staleness and verify per
§6.7 (default snapshot is the packaged file). `pricing.py` additive edit exactly per §6.8 (must not import
`rates/`), with the matching `SPEC_TABLE` rows in `tests/test_pricing.py` **in the same commit**.

**Facts to verify** (record `verified_on`): §19.1 rows outside facts.json; §19.2 modifiers; §19.6 OpenAI
tiers, Bedrock/Vertex regional; checklist §19.8 #1, #3, #4, #9, #19. Unverifiable → disabled row /
`stacking: assumed`, listed in your README.

**Acceptance tests.**
- the golden billing corpus SPEC §6.9, all 24 cases, compared in int nano (incl. per-line exact/estimated
  splits of cases 7, 9, 23, 24);
- load failures: row without source, overlapping interval, `published_absolute` mismatch, unknown predicate
  key, unknown promotion id, a cache-rule `supports` entry; disabled rows unpriced with `unverified rate row`;
- stale-rate note for a row verified 60 days before the run date; `verify_snapshot` zero diffs, then one
  injected diff reported with its row id; `crosscheck_feed` never fails;
- hypothesis: for random rows, modifiers, contract multipliers (≤ 6 decimals) and buckets,
  `unit_rates(...).bucket_nano` sums equal `price_usage` points to the nano; `unit_rates` is None only when the
  scale would exceed 24;
- layer precedence (contract > `--model-price` > builtin); effective-date lookup; contract overlay not applied
  to the subscription path; `price_total` never mixes allowance into `exact`;
- contract round trip `to_model_pricing(from_model_pricing(x)) == x`;
- `assert_pricer_conforms(RateCard([load_builtin()]))`;
- legacy parity (§6.8), facts parity (every `core/facts.json` rate row and modifier equals the registry's), and
  `tests/test_pricing.py` green; existing suite green; no-float lint clean on `rates/`.

**Size.** ~2.6k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-1** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
`core.pool`, Copilot catalog tables) are merged into `v0.2` before you start — read SPEC Appendix E.3 and
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/briefs/CORE-AMENDMENTS.md` (§2–§5) for the exact contracts, and the Copilot addendum
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/SPEC-v0.2-COPILOT.md` only as background. Do not implement Copilot adapters/detectors yourself
(the CP-* packages do); implement only your amendment item so the extension points work.

#### 5. Amendments to SPEC packages (A-; paste into briefs not yet started, late change requests otherwise)

| id | package | status @99ba098 | amendment | LOC |
|---|---|---|---|---|
| A-1 | RATES | not started | addendum §21.4 row, reading Copilot rows through `core.facts.copilot_rates()` / `core.extensions.extension_rate_files()` (missing → dq); predicate keys `routing`, `compliance_in`; band hypotheses; both Copilot paths → LIST_EQUIVALENT into `PricedTotal.pool` (`allowance` = subscription lines only); `pricing verify` runs `core.extensions.rate_verifiers()` | +120 |
| A-2 | STORE | not started | addendum §7.1 DDL and §21.4 row **plus** `SqliteStore(…, adopt_key_ids: bool = False)` (R-E21 replaces the §7.1 "Key-id adoption" paragraph: with or without its own org key the store adopts the principal and name key ids of the first `copilot-export` bundle only, one adopted id, `meta.adopted_key_id` / `adopted_name_key_id` / `org_key_mode` + an `audit` row, a second bundle key id → `UsageError`), `count_users(source="cost_lines")`, `source_stats` (implements `core.protocols.LedgerStats`), `purge(principal=…)` also for adopted-key rows, `ReconciliationReport` untouched (not stored) | +190, total 2,990 |
| A-3 | TRACE | not started | addendum §21.4 row; closed key sets derived with `core.records.record_fields` (C-4) and `core.records.RAW_USAGE_ENUMS` / `RAW_USAGE_NUMERIC`, replacing the hand lists (net-neutral, O-5) | ≤ 0 net |
| A-4 | TELEM | **started** | late change request (R-E23): `otlp` skips resources for which `core.models.is_copilot_resource` is true and sets `stats["defer:copilot-otel"]`; rebase onto the gate-F' core first | +40 |
| A-5 | RECON | not started | addendum §21.4 row; `merge_reports` also unions `ReconciliationReport.decisions` (conflicting values → `ContractViolation`) | +90 |
| A-6 | REPLAY | **started** | late change request (R-E23): accept billing class `pool` like `allowance` (LIST_EQUIVALENT; mixed classes still raise) | +10 |
| A-7 | PLAN | not started | addendum §21.4 row; skip `replay == "aggregate"` levers; lever lookup via `core.catalog.lever` (searches both tables) | +40 |
| A-8 | OUT | not started | addendum §21.4 row (`render_sections`, `write_focus(…, extra_rows, owned_channels)`, `money_json` → `figure_json`, BILL line for `PricedTotal.pool`); `outputs/ccusage.py` moves to CLI-LEDGER (O-5) | +40 net, total 2,890 |
| A-9 | WIRING | not started | addendum §21.4 row plus `open_store(…, adopt_key_ids=False)` passed to `SqliteStore`; `ingest_paths` persists records **after** the ledger ingest (so an adopted key id exists before `persist`) and re-reads deferred files | +90 |
| A-10 | CLI-LEDGER (wave 3) | not started | addendum §21.4 row; `reconcile` prints the merged decisions (`recon_decisions_of(reports)`) with its verdicts; nothing is persisted (C-17) | +45 |
| A-11 | CLI-SAVINGS (wave 3) | not started | addendum §21.4 row; because decisions are not persisted, `findings` / `report` (and `scan` on Copilot data) first run `core.extensions.run_reconcilers` + RECON's reconcile over the same window, then call `enrich(…, reconciled_channels=…, recon_decisions=recon_decisions_of(reports))` (without reconcilers, e.g. on a store without Copilot data, `recon_decisions=()`: `excl` convention and unclassified discounts, both labelled) | +65, total 2,965 |
| A-12 | INTEGRATION (wave 4) | not started | `docs/COPILOT.md` (fleet recipe: managed OTel incl. the JetBrains caveat, **VS Code `agent-traces.db` opt-in + daily `copilot collect --source vscode`**, CI post-step, gh-aw artifacts, auth table, privacy); **`docs/COPILOT-ADMIN.md` generated verbatim from `tokenbill/copilot/handoff_data/admin_guide.md`** with `tests/v2/e2e/test_copilot_admin_doc.py` asserting byte equality; flagship §18 incl. the handoff and plan-unknown paths; Copilot release gates (below); weekly YAML check | docs/tests |
| — | CC, ADMIN, BLOCK, VERIFY, DETECT-CACHE, DETECT-OTHER, SYNTH-ORACLE, SYNTH-FLEET | mixed | none | 0 |

