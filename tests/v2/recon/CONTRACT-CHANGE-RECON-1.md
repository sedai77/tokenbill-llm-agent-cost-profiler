# CONTRACT-CHANGE-RECON-1 — gaps found while building RECON

Raised by package RECON (wave 2b) per SPEC §21 #3. RECON implements against the current contract;
nothing here changes a field or a signature. Items (a)–(e) are requests for the contract owner.

## (a) One `suggested_contract` cannot carry per-channel contracts

**What.** `ReconciliationReport.suggested_contract` is a single `ContractOverlay`, and an overlay has
one `multiplier` plus per-**model** `overrides` for its `channels`. Organizations routinely hold
different contracts per channel (the synthetic fleet: `anthropic_api` 0.85, `bedrock` 0.90 —
`FleetTruth.recon.contract_multipliers`); a model sold on both channels cannot carry two
multipliers in one overlay.

**Implemented.** `recon.reconcile.suggest_contracts(aggregates, cost_lines, pricer, *, today,
closed_only=False) -> tuple[ContractOverlay, ...]` returns one overlay per group of channels that
share a uniform multiplier (a channel whose models need different multipliers gets its own overlay
with per-model `overrides`), largest invoice first. `reconcile(…, suggest_contract=True)` reports the
first as `suggested_contract` and re-runs **each channel with the overlay of its own group**
(`rerun_verdict` covers every channel). The CLI should write every overlay of `suggest_contracts`.

**Proposed.** `ReconciliationReport` += `suggested_contracts: tuple[ContractOverlay, ...] = ()`
(appended, `OMIT_DEFAULT`), `suggested_contract` = its first element.

## (b) The report carries no data-quality notes

`ReconciliationReport` has no `notes`. Conditions the SPEC wants surfaced are left for the caller to
derive: `dq.recon_schema_unverified` from `ChannelVerdict.mapping_verified == False`, and a second
invoice source on one channel (the Admin cost report and the Enterprise Analytics cost endpoint both
present: RECON uses the first of `costmap.INVOICE_PRECEDENCE` and ignores the other, never adding
them). **Proposed:** `notes: tuple[DataQualityNote, ...] = ()` appended.

## (c) `UsageRecord` loses the placeholder output's upper bound

`Inference.output_upper` (the upper estimate of a `MESSAGE_START_ONLY` call's output) is not on
`UsageRecord`, so the ledger's placeholder output prices as a zero-width range. RECON treats
reconstructed usage (`MESSAGE_START_ONLY`, `PARTIAL_STREAM`, `ESTIMATED`) as a lower bound that may
hide up to the provider's excess tokens in the same bucket (residual `estimated_components`; the
fleet's placeholder plant is explained this way). **Proposed:** `UsageRecord.output_upper: int |
None = appended(None)`, filled by the stores from the inference.

## (d) Aggregated provider usage and long-context bands

The rate-card check prices each provider aggregate with `Pricer.price_usage`; a pricer that selects
long-context band rates from the request's total input (FakePricer does for rows with
`long_context_threshold`) sees the whole bucket's input. No current `facts.json` row has a
threshold, and the usage report's `context_window` dim tells which buckets are long-context.
**Proposed:** a `price_usage(…, band: bool | None = None)` override (or a documented
`context_window`-aware `price_aggregate`) for aggregate pricing.

## (e) CUR model identity (echoes CONTRACT-CHANGE-ADMIN-1 (d))

Every CUR SKU rule has `model: null`; even with verified rules a CUR line maps to a bucket but not
to a model, so Bedrock reconciles on channel totals. RECON maps a CUR token line only when the line
itself carries a model (or a verified rule names one); otherwise the channel is in channel-total mode
with `mapping_verified = False`.
