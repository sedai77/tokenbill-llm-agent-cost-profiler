# Verification: gap-commercial-commitment-levers

Verified 2026-09-23 by an adversarial fact-checker. I re-fetched every cited source myself into `scratchpad/research/vccl2/`, using curl plus a local HTML-to-text converter. I did not reuse the original agent's `ccl/` copies. Key raw files:
- `aws_pl.json`: the AWS Price List, publicationDate 2026-09-22T16:44:18Z
- `az_ret.json`: an Azure Retail Prices API query for eastus2 "Provisioned" meters
- the Vertex, Azure, AWS, Anthropic, Claude Code, GitHub and Cursor pages, as `.html` and `.txt`
- `arx_*.xml`: arXiv API metadata

## Summary

| id | verdict |
|---|---|
| cc-capacity-parity | confirmed |
| cc-capacity-model-lag | corrected |
| cc-deflation-hazard | confirmed |
| cc-cache-consumption-matrix | confirmed |
| cc-sizing-newsvendor | confirmed |
| cc-overflow-mechanics | confirmed |
| cc-renewal-traps | corrected |
| cc-ccu-private-offers | confirmed |
| cc-marketplace-drawdown | confirmed (minor nuances) |
| cc-anthropic-discount-visibility | confirmed |
| cc-priority-legacy | confirmed |
| cc-bedrock-reserved | confirmed |
| cc-vertex-pt | confirmed (1 nuance) |
| cc-azure-ptu | confirmed |
| cc-openai-scale-tier | confirmed |
| cc-seat-vs-usage | confirmed |
| cc-ttl-billing-path | confirmed |
| cc-copilot-credits | confirmed |
| cc-cursor-token-rate | confirmed |
| cc-timing-calendar | confirmed (1 nuance) |
| cc-reconciliation-matrix | confirmed |
| cc-channel-divergence | confirmed (doc-lag evidence strengthened) |
| cc-seat-insurance-model | confirmed |

Nothing was fabricated. Every paper exists with the stated authors, dates and venues, and every quoted string I searched for was found verbatim.

---

## Per-finding notes

### cc-capacity-parity: confirmed

**AWS Price List** (v20260922164418), Reserved SKUs, global:

| Model | 1-month input | 3-month input | 1-month output | 3-month output |
|---|---|---|---|---|
| Sonnet 4.5 / 4.6 | 0.18 | 0.162 | 0.90 | 0.81 |
| Opus 4.5 / 4.6 | 0.30 | 0.27 | 1.50 | 1.35 |
| Haiku 4.5 | 0.06 | 0.054 | 0.30 | 0.27 |

- Geo/Regional prices are exactly 1.1×.
- The description reads "Per Hour per 1K Input TPM Reserved 1 Month Global", while the unit field reads "1M TPM Hour". The report flags this inconsistency correctly.
- 1K TPM for 1 hour is 60K tokens. At $3/MTok that is $0.18, so the parity arithmetic holds. The 3-month term is 0.162 ÷ 0.18 = 90%.
- The finding's phrase "Claude 4.6 generation is $0.18" applies only to Sonnet 4.6; Opus 4.6 is $0.30. The body text is correct.

**Vertex** pricing page GSU table (global): 1 week $7.14, 1 month $3.698630137, 3 months $3.287671233, 1 year $2.739726027.
- Supported-models table (updated 2026-09-22): Opus 5 / 4.x at 210 tok/s, Sonnet 5 / 4.6 at 350, Fable 5.1 at 105, Haiku 4.5 at 1,050.
- 210 × 2.628M s × $5/MTok = $2,759.4 per GSU-month.
- Break-even: 2,700 ÷ 2,759.4 = 97.8%, then 87.0%, 72.5%, and 189% for the 1-week term. These are reproduced.

**Azure Retail Prices API** (queried):
- Provisioned Managed Global $1.00/hr, Data Zone $1.10, Regional $2.00.
- Reservation Global $260 (1 month) and $2,652 (1 year). Data Zone and Regional $286 and $2,916.

**Azure sizing page** (ms.date 2026-09-11) states "At list price and full utilization, PTU and Global Standard are approximately equivalent on a normalized-token basis".
- Microsoft's own table uses a 30-day month ($260 vs $259.20), which is about 100%. A 730-hour month gives 98.9%. "About 99%" is fine.
- 1 year: 221 ÷ 262.8 = 84%.
- Hourly: 730 ÷ 262.8 = 278%.

**FinOps page** (Last updated March 17, 2026) contains "The cost per token is 33% higher than the shared capacity rate", in a "Target peak utilization is 75%" example.

**Caveat, kept from the report:** whether the published GSU price table applies to Claude orders is not stated. Claude PT is ordered via an account rep.

### cc-capacity-model-lag: corrected

**Confirmed:**
- The service-tiers page says Priority Tier is "no longer available for purchase" and is supported on all models "except Claude Fable 5.1, Claude Mythos 5.1, Claude Mythos 5, Claude Mythos Preview, Claude Opus 5.5, Claude Opus 5, and Claude Sonnet 5".
- Bedrock model-card tier tables, parsed from the icon-yes/icon-no images:
  - Opus 5.5: Reserved no, and Batch no.
  - Sonnet 5 and Fable 5.1: Reserved no.
  - Sonnet 4.6 and Opus 4.6: Reserved yes.
- The Vertex PT partner table has no Opus 5.5 row. (It is on supported-models, not the purchase page that was cited.)
- Prices match: Opus 5.5 is $4/$20 with $0.20 reads; Opus 5 and 4.x are $5/$25 with $0.50; Sonnet 5 is $2/$10; Sonnet 4.6 is $3/$15.
- The Opus 5.5 post says "it will cost 40% less than Opus 5 on typical workloads".

**Correction:** "Reserved Sonnet 4.6 costs about 50% more per token than Sonnet 5" is literally true at list, but it overstates the per-workload gap.
- The Anthropic pricing page says "Claude 4.7 and later models ... use a newer tokenizer ... approximately 30% more tokens for the same text. Claude Sonnet 4.6 and earlier models use the previous tokenizer."
- The bundled model-migration guide says "Claude Sonnet 5 uses the same new tokenizer as Opus 4.7/4.8 ... approximately 30% more tokens than on Sonnet 4.6".
- For the same text, Sonnet 5 costs about $2 × 1.3 = $2.60 per Sonnet-4.6-equivalent MTok, against $3.00. That makes Sonnet 4.6 roughly 15% dearer per unit of text, not 50%. The real per-task gap must be measured.
- The same caveat applies to Reserved Opus 4.6 (old tokenizer) against Opus 5.5 (new tokenizer). At list, Opus 5.5 input for the same text may cost about the same or more ($4 × 1.3 ≈ $5.2 vs $5). Anthropic's 40% saving is stated against Opus 5, which uses the same tokenizer, not against Opus 4.6.

### cc-deflation-hazard: confirmed

- **Deprecations table:** `claude-opus-4-1-20250805` is $15 input on the pricing page, Opus 4.5 is $5, and Opus 5.5 is $4. 1 − 4/15 = 73.3%, over 2025-08-05 to 2026-09-22, which is about 13.5 months.
- **Fable 5.1 page:** "Cache reads now cost 75% less, or $0.25 per million tokens". Fable 5 was $1, so this is −75%. The page date shows only "September 2026". The deprecations floor "Not sooner than September 1, 2027" implies a 2026-09-01 launch.
- **Opus 5.5 post** (September 22, 2026): "$4 and $20 per million, 20% less than Opus 5" and "Cache reads ... $0.20 per million tokens, 60% less than Opus 5".
- **Pricing footnote 3:** "The previously scheduled increase to $3/$15 ... on September 1, 2026 will not occur."
- **Epoch AI** (Mar. 12, 2025): "ranging from 9x to 900x per year" and "40x per year" for GPT-4-level GPQA.
- **Vertex purchase page:** "term fees are not cancelable ... will apply regardless of actual usage or if the model is discontinued".
- **Foundry CCU page** (ms.date 2026-08-14): "The rate in effect at the time of each call applies, so changes to Anthropic's published rates take effect immediately for subsequent calls." This is verbatim.

### cc-cache-consumption-matrix: confirmed

- **Anthropic service tiers:** reads 0.1, 5m writes 1.25, 1h writes 2.00, and US-only 1.1 on input and output (Claude 4.6 and later only). The page states "These burndown rates reflect the relative pricing of each token type".
- **Bedrock Reserved:** the service-tiers page says TPM "includes both InputTokenCount and CacheWriteInputTokens".
- **Bedrock burndown page:**
  - "CacheReadInputTokenCount don't contribute ... not counted toward your quota".
  - Output burndown is 15× for "Claude models version 4.8", 10× for "Opus 5.5, Claude Sonnet 5, Claude Opus 5, and Claude Fable 5.1", and 5× for 4.7 and below.
  - Deduction at the start of a request is "Total input tokens + max_tokens".
- **Vertex table:** output 5, 5m write 1.25, 1h write 2, hit 0.1. Fable 5.1 hit is 0.025.
- **Azure:**
  - "Cached tokens are deducted 100% from the utilization calculation". Worked example: 110 PTU without cache, 80 PTU at a 50% cache rate (gpt-5.2 Data Zone).
  - GPT-6 Astra normalized weights: cached 0.1, write 1.25, output 5.

### cc-sizing-newsvendor: confirmed

**Math.** d/dC [r·p·C·T + p·∫(D − C)⁺] = 0 gives the time-fraction with D > C equal to r. This is correct.

**Papers (arXiv API):**
- 1305.5608 is Wang, Li, Liang, "To Reserve or Not to Reserve: Optimal Online Multi-Instance Acquisition in IaaS Clouds", 2013-05-24, comment "appeared in USENIX ICAC 2013". The abstract gives 2−α and e/(e−1+α), described as "best possible".
- 2502.08022 is Bergemann and Wang, 2025-02-11. The optimal mechanism is "implemented by ... the two-part tariff and the committed spend contract". The finding's "committed-spend is the vendor-optimal screen" is fair; a two-part tariff is equivalent.
- 2005.12249 is Ambati, Irwin, Shenoy, HotCloud 2020. It cites "up to 60%" savings and "demand risk", and studies the RI Marketplace as a mitigation.

**Other sources:**
- The Usage API supports `1m` buckets, up to 1,440 per request.
- On Vertex, "the entire request is processed as an on-demand request". Dynamic enforcement windows run from 40–120 s for 3 GSU or fewer down to 1–5 s for 50 GSU or more.

### cc-overflow-mechanics: confirmed

- **Bedrock:** "automatically overflows to the Standard tier". "ResolvedServiceTier shows the actual tier that served your requests".
- **Vertex:**
  - The whole request spills to pay-as-you-go, shown as `spillover`.
  - `X-Vertex-AI-LLM-Request-Type: dedicated` returns 429, and `shared` bypasses PT.
- **Azure:**
  - "deployed PTUs that exceed the quantity are billed at the hourly rate". $1.00 against $260 ÷ 730 = $0.356 is 2.8×.
  - Spillover uses `x-ms-spillover-deployment` and is not supported for DeepSeek or Llama.
- **Anthropic:** "Requests beyond your committed capacity automatically fall back to standard tier."
- **OpenAI:** "Scale Tier spillover traffic doesn't automatically move to Fast mode."

### cc-renewal-traps: corrected

**Confirmed:**
- Bedrock Reserved quote, verbatim.
- Llama 2 (13B/70B) PT at $21.18/hr (1-month) against $13.08/hr (6-month) is −38.2%.
- **Vertex:**
  - "you can't cancel the order in the middle of your term".
  - "A decrease in GSUs is applied during auto-renewal for the next term".
  - Weekly terms can't auto-renew.
  - Orders can be split.
- **Azure:**
  - US$50,000 per 12-month rolling window.
  - "has a new term starting from the time of exchange".
  - "in the future there might be a 12% early termination fee".
  - "lowest price of either your purchase price or the current price" (ms.date 2026-07-22).

**Corrections:**
1. **Bedrock PT: two products are conflated.** The prov-thru-delete page says:
   - "You can't delete a Provisioned Throughput **by Model Units** with commitment before the commitment term is complete."
   - Auto-renew cancellation, and "Auto renew cannot be re-enabled after it is cancelled", are described for "Provisioned Throughput **by Tokens**".

   A detector must key these rules to the PT type.
2. **Vertex: the 5-day freeze is narrower than stated.** It applies only when the order "expires in less than five days **and isn't configured for auto-renewal**". It is not a blanket "no changes within 5 days of expiry".

### cc-ccu-private-offers: confirmed

**Anthropic pricing page:**
- "$0.01 per CCU (fixed; discounts apply at token-to-CCU conversion...)".
- Hourly metering, arrears only, "no prepaid credits", "single CCU line item".
- "Discounts cannot be applied retroactively to usage incurred before your private offer is accepted."

**Claude Platform on AWS doc:**
- "There is no CCU balance or commitment."
- "Negotiated discounts and AWS Marketplace private offers don't transfer automatically between Bedrock and Claude Platform on AWS."
- "Spend is calculated at list prices and can take about 2 hours ... The overshoot is billed."
- Admin API "usage reports, cost reports ... are not currently available".

**Foundry page:**
- "A private offer can carry different discount rates for different Claude models."
- Deployments created before CCU GA "continue to bill on their existing per-model plan".

### cc-marketplace-drawdown: confirmed (minor nuances)

**MACC page** (meta `updated_at` 2025-10-24; the page shows "Last updated on 2025-09-25"):
- Only "Azure benefit eligible" offers count, bought via the Azure portal on an agreement subscription.
- "100% of the pretax purchase amount".
- Credit-card purchases don't contribute, and Azure-prepayment purchases are not eligible.

**Foundry pages:**
- CCU "is MACC-eligible". "CCU billed by other cloud providers doesn't decrement your Microsoft Azure Consumption Commitment."
- The manage-costs page (ms.date 2026-08-27) says "you can't use Azure Prepayment credit to pay for charges for other provider models".

**AWS:**
- Claude Platform on AWS lists "AWS commitment retirement" under "What stays the same".
- Bedrock Claude SKUs all have `USE1-MP:` usage types, "AWS Marketplace software usage".

**Nuances:**
- The buyer guide says "Member accounts that were previously subscribed to the product must also accept the new private offer". The management account can share an offer with members.
- On expiry, "You will either automatically move to the product's public pricing **or lose your subscription**". It does not always revert to public pricing.

### cc-anthropic-discount-visibility: confirmed

**Analytics API reference:**
- `amount` is "post-discount, pre-credit" and `list_amount` is "pre-discount".
- Data is "not final until about 30 days after the usage date".
- RBAC rows use "Any-membership semantics".

**Analytics overview:** "for seat-based Enterprise plans, they reflect usage credits only". Values "can be revised for up to 30 days".

**Console cost report:**
- `bucket_width` is "1d" only, and amounts are in cents.
- There is no `list_amount` field (0 occurrences).
- `service_tier` is batch or standard.
- The Usage/Cost doc says "Priority Tier costs ... are not included in the cost endpoint".

**Other:**
- Bundled cost-optimization.md: "effective realized rates come from dividing cost-report amounts by the usage report's matching token counts". It is framed as a fallback.
- Claude Code `modelPricing` has a multiplier (0 < m ≤ 10) and per-model overrides. It applies to `/usage`, the status line, the SDK `total_cost_usd`, `--max-budget-usd` and OTel.
- **Caveat worth carrying:** the `cacheWrite` override "covers both five-minute and one-hour cache writes". It cannot express the 1.25× vs 2× split.

### cc-priority-legacy: confirmed

- A commitment covers ITPM and OTPM, for 1, 3, 6 or 12 months, on "A specific model version", targeting 99.5% uptime.
- `service_tier` is "auto" or "standard_only", and `usage.service_tier` returns "priority".
- The `anthropic-priority-{input,output}-tokens-{limit,remaining,reset}` headers exist.
- The Usage API FAQ: "Priority Tier costs are not available in the cost endpoint".

### cc-bedrock-reserved: confirmed

- Terms are 1 or 3 months, at a "fixed price per 1K tokens-per-minute and are billed monthly".
- Minimums are 100,000 input TPM and 10,000 output TPM. Access is via the AWS account team.
- The price table matches the price list exactly. Regional is 1.1×.
- The 3-month term is exactly −10%.
- Minimum monthly cost, using 730 h/month:

| Model | Input 100 × rate × 730 | Output 10 × rate × 730 | Total |
|---|---|---|---|
| Haiku 4.5 | $4,380 | $2,190 | $6,570 |
| Sonnet 4.6 | $13,140 | $6,570 | $19,710 |
| Opus 4.6 | $21,900 | $10,950 | $32,850 |

### cc-vertex-pt: confirmed (one nuance)

**Confirmed:**
- GSU prices as listed. "1 week (available only for Google models)".
- **Minimums** (supported-models table): Fable 5.1 1, Opus 5 1, Sonnet 5 / 4.6 / 4.5 / 4 25, Opus 4 through 4.8 35, Haiku 4.5 8.
- "Provisioned Throughput doesn't support batch prediction calls". "you must use the specific model version ID ... and not a model version alias". These are stated in the Google-models section.
- **Promo:** "Effective August 13, 2026 through December 31, 2026 ... 50% of net eligible Provisioned Throughput spending on Gemini 3.8 Flash, Gemini 3.7 Flash, and Gemini 3.6 Flash". Credits "expire 30 days after issuance".
- **Monitoring:** request_type is dedicated, spillover or shared. "Anthropic models also have a filter for Provisioned Throughput but only for tokens and token_count".

**Nuance:** the non-global 1.1× GSU price carries a footnote. It takes effect "for the Generally available Gemini 3 and later families of all Google models starting on July 1, 2026". Its applicability to Claude orders is not stated.

### cc-azure-ptu: confirmed

- The Retail API matches every price: $1.00, $1.10 and $2.00 hourly; $260 / $2,652 Global; $286 / $2,916 Data Zone and Regional; and a `$312 /Month` "Provisioned Throughput Units" meter.
- 1-month reservation: 1 − (260 ÷ 730) ÷ 1.00 = 64.4% off hourly.
- 1-year reservation: 1 − (2,652 ÷ 8,760) = 69.7% off hourly. 221 ÷ 260 = 0.85, so it is 15% off the 1-month term.
- **Billing page:**
  - "Model-independent".
  - "Reservations don't guarantee capacity". "always create deployments first, then purchase the Azure Reservation".
  - Excess PTUs above the reservation are "charged at the standard hourly rate".
  - The "Commitment model" applies to customers "onboarded before the August 2024 self-service update".

### cc-openai-scale-tier: confirmed

- The raw `service_tier.py` literal is ["auto","default","flex","scale","priority","fast","ultrafast"]. The GitHub API shows the last commit to that file as ff14a33c at 2026-08-17T15:42:09Z.
- **fast-mode.md:**
  - "Priority processing was renamed Fast mode on July 30, 2026".
  - "purchased Scale Tier TPM bundles". "Scale Tier spillover traffic doesn't automatically move to Fast mode". "consider purchasing Scale Tier quota".
  - "All processing modes count toward your annual Enterprise spend commitment".
  - "GPT-5.6 Sol's promotional pricing is available at least through November 21, 2026".
- I also got 403 on openai.com/api-scale-tier/ and 404 on /api/docs/guides/scale-tier. Terms remain unverified, as the finding says.

### cc-seat-vs-usage: confirmed

**claude.com/pricing:**
- Team Standard "$20 Per seat / month if billed annually. $25 if billed monthly"; Premium "$100 ... $125".
- Enterprise "Seat price + usage at API rates US$20/seat/month, billed annually".

**Claude Code costs page:**
- "per-seat allowance that resets on a rolling five-hour window and a weekly window".
- "Usage inside the seat allowance isn't metered in dollars".
- "each developer is metered according to the one they authenticated with".
- "$13 per developer per active day and $150-250 per developer per month ... below $30 per active day for 90% of users".

**Support articles:**
- 12429409 (dateModified 2026-08-10): "billed at standard API rates", prepay via "Add funds".
- 11845131 (dateModified 2026-09-22) contains the older-Enterprise seat language.

**Quesma** (Jacek Migdal, 11 August 2026):
- Table rows include Pylon at "$400K/yr | ~$1.4M/yr projected | 3.5x". The 12×, 15×, 21× and 40× rows are individual Max plans; the 40× figure is SemiAnalysis's Max 20x ≈ $8,000.
- "saw the bill at least double. Most reported roughly 3x."
- "move to Enterprise only once you outgrow 150 seats".
- The 3.5× figure is Quesma relaying Pylon, a single organization.

### cc-ttl-billing-path: confirmed

- The prompt-caching "Which TTL each request gets" section is verbatim: "requests the one-hour TTL only on a Claude subscription within your plan's included usage". The table column reads "Usage credits, API key, or cloud provider", with the main conversation at "Five minutes". "drops the main conversation to the cheaper five-minute TTL".
- `promptCacheTtl`, `CLAUDE_CODE_PROMPT_CACHE_TTL` and `subagentPromptCacheTtl` exist.
- **Local replay:** empirical.md reports all-5m at +11.8% against the as-billed baseline. `20_ttl_per_session.out` gives all-5m $7,310.95 against all-1h $6,613.30, which is +10.5% relative to an all-1h replay.
  - 1h was cheaper in only 15 of 29 sessions.
  - This is main-thread spend, about 6.6% of the total bill per empirical.verify.md.
  - "About 12%" is acceptable, but it is single-developer and internal.

### cc-copilot-credits: confirmed

**Docs:**
- "Copilot Business at $19 USD per user per month, includes 1,900 AI credits per user". Enterprise "$39 ... 3,900 AI credits".
- "shared enterprise pool ... $0.01 USD per AI credit". "Code completions and next edit suggestions are not billed in AI credits".
- "When you remove seats, billing for those seats continues until the end of the current billing cycle."

**Blog** (April 27, 2026; effective June 1):
- Tokens "including input, output, and cached tokens, according to the published API rates".
- "promotional included usage for June, July, and August".

**Changelog** (June 19, 2026): "not currently broken down by feature, model, or surface". "a metrics signal for analyzing consumption, not a billed total".

### cc-cursor-token-rate: confirmed

**Teams pricing doc:**
- Standard is $40/user/mo. Premium is "5x usage at $120/user/mo".
- Usage "does not transfer between team members". "Our Enterprise plan offers pooled usage".
- "The Cursor Token Rate applies to input tokens, output tokens, and cached tokens ... includes when Auto routes to a third-party model. This applies to BYOK as well. First-party Cursor models ... are exempt."

**Admin API:** `totalCents`, `chargedCents` and `cursorTokenFee` fields are present.

**Arithmetic re-computed** for the mix of 94% reads, 3% input, 2% 5m writes and 1% output:

| Model | Model cost per 1M tokens | Fee ($0.25) as % | Cached token vs direct |
|---|---|---|---|
| Opus 5.5 | $0.608 | +41.1% | 0.45 ÷ 0.20 = 2.25× |
| Sonnet 5 | $0.398 | +62.8% | 2.25× |
| Fable 5.1 | $1.285 | +19.5% | 0.50 ÷ 0.25 = 2.0× |

### cc-timing-calendar: confirmed (one nuance)

- **Deprecations page:**
  - "at least 60 days' notice before model retirement for publicly released models".
  - Floors: Sonnet 4.5 is "Not sooner than September 29, 2026", Haiku 4.5 October 15, 2026, Opus 4.5 November 24, 2026, and Opus 5.5 September 22, 2027. Each is one year after launch.
  - Opus 4.1 and Opus 4 are retired on 1P.
- **Pricing page:** Opus 4.1 is "retired, except on Bedrock and Google Cloud" and Opus 4 "retired, except on Google Cloud", both $15/$75. The Bedrock price list confirms Opus 4.1 at $15/$75. I did not find an Opus 4 or 4.1 row on the Vertex pricing page.
- **Gemini API page** (Last updated 2026-09-23): 3.8 Flash is "$0.75 through December 31, 2026. $1.50 starting January 1, 2027".
- **Vertex:** "Promotional pricing provided through 50% credits back on net spend". The PT credit ends 2026-12-31.
- **OpenAI** Sol promo: at least through Nov 21, 2026.
- **Opus 5.5 post:** "Claude Sonnet 5.5 and Claude Haiku 5.5 will follow in the coming weeks".
- **Nuance:** no deprecation notice has been posted for Sonnet 4.5; its deprecation column is "N/A". Because of the 60-day notice rule, its practical earliest retirement is at least 60 days after a future notice, not 2026-09-29.

### cc-reconciliation-matrix: confirmed

- **Usage API:** 1m, 1h and 1d buckets; filters and grouping by service tier, geo and speed. **Cost API:** daily, cents.
- **Analytics:** `amount` and `list_amount`.
- **Claude Platform on AWS:** usage and cost reports are not available via the Admin API, and the bill has a single CCU line.
- **Foundry:** a single CCU meter, per-model tokens in the Monitor tab, and "Foundry estimates don't reflect private-offer discounts".
- **Bedrock FAQ:**
  - "Both classic CUR and CUR 2.0 aggregate cost by usage type over an hour or a day, and neither carries a per-request identifier".
  - "It does not reflect discounts, commitments, batch pricing, free tier, or provisioned throughput".
  - The recommended join is logs to CUR "at the model/usage-type/day grain". The finding says "join to CloudWatch", which is loosely worded; the logs are CloudWatch Logs.
- **Price feeds:** the AWS Price List contains Reserved rows, and the Azure Retail API contains PTU hourly and reservation rows. Both verified.

### cc-channel-divergence: confirmed

- **Vertex global** Opus 5.5: Batch Input $2.50, Batch Output $12.50, 5m Batch Cache Write $2.50, 1h Batch Cache Write $4.00, Batch Cache Hit $0.10.
- **Anthropic 1P batch:** $2 / $10.
- **Bedrock:** the price list has only standard Opus 5.5 SKUs (no batch), and the model card shows Batch unsupported.
- **Regional and US-only premiums:** "Regional and multi-region endpoints include a 10% premium over global". `inference_geo: "us"` is 1.1× on "all token pricing categories".
- **Doc-lag evidence strengthened:** the Vertex non-global Opus 5.5 rows list batch at $2.20 / $11.00. That is 1.1 × 1P batch and lower than the global row, which only makes sense if the global $2.50 / $12.50 row is stale.

### cc-seat-insurance-model: confirmed

- arXiv 2605.16699 is Caio Gomes, "Your SaaS Is an Insurance Product: A Modeling Framework", 2026-05-15.
- The abstract contains the quoted phrase verbatim, names Claude Code explicitly, and proposes "frequency-severity decomposition ... and Monte Carlo reserve adequacy".
- It is a preprint that argues the case rather than an empirical result. The Anthropic fleet figures are as stated on the costs page.
