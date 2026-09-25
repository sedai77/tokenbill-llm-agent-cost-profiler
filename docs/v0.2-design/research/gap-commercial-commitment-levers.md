# Token Bill research track: commercial and commitment levers

Research date: 2026-09-23. Track: `gap-commercial-commitment-levers`.

**Scope.** This track covers the commercial layer that sits above prompt-level optimization:
- commitments, reservations and provisioned capacity
- private offers and marketplace drawdown
- seat plans versus usage billing for coding agents
- timing levers
- programmatic reconciliation

Every source below was opened during this pass. Where a page is a living document, the date is the page's own "last updated" or `ms.date`, or "accessed 2026-09-23".

**Figure labels used throughout:**
- **[LIST]** is a public list price or published term.
- **[CONTRACT]** is contract-dependent: private offers, enterprise discounts and EDP/PPA terms vary per customer. Token Bill must take these as inputs, never assume them.
- **[DERIVED]** is computed in this pass from public prices, with the formula shown. The working files are in `scratchpad/research/ccl/`: `fm.json` is the AWS Price List file, `az1.json` is the Azure Retail Prices API result, and there are text copies of the Vertex pages.

---

## 1. Executive summary

1. **Reserved and provisioned capacity for frontier models costs about the same as pay-as-you-go at 100% utilization. It is an availability product, not a discount.** [DERIVED from LIST]
   - **Bedrock Reserved:** break-even utilization is exactly 100% on a 1-month term and 90% on a 3-month term. The Claude price list has input at $0.18 per 1K TPM-hour, which equals 60K tokens × $3/MTok.
   - **Vertex Provisioned Throughput (GSU):** break-even is about 98% on 1 month, 87% on 3 months and 72.5% on 1 year. The 1-week term never breaks even.
   - **Azure PTU:** the 1-month reservation breaks even at about 99% and the 1-year at about 84%. Hourly PTU needs 278% utilization, so it never breaks even.
   - The FinOps Foundation measured the same thing independently: running fully provisioned at a 75% peak-utilization target cost "33% higher than the shared capacity rate".
   - **Implication:** Token Bill must stop anyone buying capacity "to save money". Capacity is justified by an SLO, or by caching mechanics that make capacity cheaper (see F4).
2. **Every Claude capacity commitment is tied to older, more expensive models.**
   - Anthropic Priority Tier is no longer sold. It does not support Opus 5.5, Opus 5, Sonnet 5, Fable 5.1 or Mythos.
   - Bedrock Reserved is offered for Opus 4.6 and Sonnet 4.6, but not for Opus 5.5, Sonnet 5 or Fable 5.1 (per the model cards).
   - Vertex Provisioned Throughput lists no Opus 5.5. Its Sonnet 5 GSU is rated for the old price, which makes it **1.47× pay-as-you-go even at 100% utilization** [DERIVED].
   - Meanwhile the successors are cheaper: Opus 5.5 is 20% below Opus 5 at list and about 40% cheaper on typical workloads, and Fable 5.1 cache reads are 75% cheaper.
   - A commitment tied to a model is the single largest stranded-dollar risk in this layer.
3. **Price deflation is fast.**
   - Opus-tier list input price went from $15 (Opus 4.1, 2025-08-05) to $5 (Opus 4.5) to $4 (Opus 5.5, 2026-09-22). That is **−73% in about 13.5 months**.
   - Two major cuts landed three weeks apart: 2026-09-01 and 2026-09-22.
   - Vertex Provisioned Throughput fees "apply regardless of actual usage or if the model is discontinued".
   - Commitment sizing therefore has to model a price-cut hazard, keep terms short, and prefer dollar commitments that are not tied to a model (for example, CCU private offers, where "changes to Anthropic's published rates take effect immediately").
4. **The lever that actually saves money is the negotiated discount, applied everywhere it should be** [CONTRACT]. Common ways it leaks:
   - AWS member accounts still on the public offer.
   - Usage before a private offer was accepted. Discounts are "not ... retroactive".
   - Bedrock discounts that do not carry over to Claude Platform on AWS.
   - Foundry deployments still on legacy per-model billing.
   - Marketplace spend that does not draw down MACC or AWS commitments. Examples: Azure purchases made by credit card, or CCU billed by another cloud.
5. **Seat versus usage for coding agents is a per-developer decision with a heavy-tailed distribution.**
   - Anthropic reports about **$13 per developer per active day** and **$150–250 per developer per month**, with **90% of users under $30 per active day**.
   - At org level, flat seats come out about 2–3.5× cheaper than per-token Enterprise: Pylon paid $400K/yr on Team against a projected ~$1.4M/yr. The 12–40× figures apply to individual Max plans, not organizations.
   - The billing path also changes the default cache TTL. It is 1h on a subscription within plan usage, and 5m on usage credits, an API key or a cloud provider.
6. **Channel fees can exceed the model's own price.**
   - Cursor charges **$0.25/MTok on input, output and cached tokens**, including BYOK.
   - That is more than Opus 5.5's $0.20 cache read, so a cached token costs 2.25× what it costs direct.
   - On a cache-heavy agent mix, the fee adds **+41% (Opus 5.5) and +63% (Sonnet 5)** [DERIVED, illustrative mix].
   - GitHub Copilot's included credits equal the seat price ($19 buys 1,900 credits at $0.01) and are pooled across the enterprise.
7. **Most of this can be reconciled programmatically, with known gaps:**
   - Priority Tier is missing from Anthropic's cost report.
   - Claude Platform on AWS has no usage or cost API.
   - Foundry shows a single CCU line.
   - Bedrock CUR has no request IDs, and cost computed from logs "does not reflect discounts, commitments … or provisioned throughput".

---

## 2. Instrument catalog (as of 2026-09-23)

| # | Instrument | Unit | Term | Minimum | What cached tokens consume | Overflow billing | Cancellation / renewal | Primary source (date) |
|---|---|---|---|---|---|---|---|---|
| I1 | Anthropic API pay-as-you-go (Console) | $/MTok per token type | none | none | Billed at 0.1× input (0.05× Opus 5.5, 0.025× Fable/Mythos 5.1). 5m writes 1.25×, 1h writes 2× [LIST] | n/a; tier spend caps | n/a | platform.claude.com/docs/en/about-claude/pricing (accessed 2026-09-23) |
| I2 | Anthropic enterprise volume discount / commit | $ (negotiated) | [CONTRACT] | [CONTRACT] | Discount applies per contract | [CONTRACT] | [CONTRACT] | Pricing page: "Volume discounts … negotiated on a case-by-case basis" (accessed) |
| I3 | Anthropic **Priority Tier** (legacy) | ITPM + OTPM, on a **specific model version** | 1, 3, 6 or 12 months | [CONTRACT] | Burndown: reads 0.1, 5m writes 1.25, 1h writes 2.0, US geo 1.1× | Falls back to standard tier | **No longer sold.** Existing commitments run to contract end | platform.claude.com/docs/en/api/service-tiers (accessed) |
| I4 | Claude **Enterprise, usage-based** | US$20/seat/mo (annual) + usage "at API rates" | annual seats | [CONTRACT] | Usage billed at API rates, so the cache multipliers apply | Spend limits at org, group and user level | annual | claude.com/pricing (accessed); support 11845131 (updated 2026-09-22) |
| I5 | Claude **Team** seats (Standard / Premium) and **seat-based Enterprise** (legacy Premium seats) | seat; allowance on 5h and weekly windows | monthly or annual | [CONTRACT] | Not metered in $ inside the allowance. Beyond it, usage credits at "standard API rates" | Usage credits (prepaid), capped by spend limits | Team Std $20 annual / $25 monthly; Prem $100 / $125 [LIST] | claude.com/pricing; code.claude.com/docs/en/costs; support 12429409 (2026-08-10) |
| I6 | **Claude Platform on AWS** (CCU via AWS Marketplace) | CCU = $0.01 | none ("no CCU balance or commitment") | none | Same token rates as 1P, then converted to CCU | Tier spend caps (computed **at list**) | Private offer; discounts **not retroactive**; Bedrock offers don't transfer | pricing page §Claude Platform on AWS; platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws (accessed) |
| I7 | **Claude in Microsoft Foundry** (CCU via Azure Marketplace) | CCU (fixed price) | none | none | Same as 1P | n/a | Private offer with **per-model discount rates**; **MACC-eligible** | learn.microsoft.com/…/claude-models-billing (ms.date 2026-08-14) |
| I8 | Bedrock on-demand plus AWS Marketplace private offer (Claude billed as `MP:` "Amazon Bedrock Edition" usage types) | $/MTok | offer term [CONTRACT] | [CONTRACT] | Cache read/write SKUs | n/a | Offer expiry reverts to public pricing | AWS Price List `AmazonBedrockFoundationModels` v20260922164418; AWS Marketplace buyer guide (accessed) |
| I9 | **Bedrock Reserved tier** | $ per hour per **1K input TPM** and per **1K output TPM** | **1 or 3 months** | **100K input TPM, 10K output TPM** | **InputTokenCount + CacheWriteInputTokens** count at 1:1. Cache reads are not counted toward TPM | "automatically overflows to the Standard tier" (on-demand price) | "Billing continues until you delete the Reserved Tier reservation with the help of your AWS account manager". Targets 99.5% uptime | docs.aws.amazon.com/bedrock/latest/userguide/service-tiers-inference.html (accessed); price list v20260922164418 |
| I10 | Bedrock Provisioned Throughput (Model Units) | MU (model-specific TPM) | no-commit, 1 month, 6 months | MUs via support case | per model | n/a (dedicated ARN) | Commit PTs can't be deleted before term. **Auto-renew**; cancelling auto-renew is irreversible. Claude base models only up to Claude 3.5 Sonnet v2 | prov-throughput.html; prov-thru-supported.html; prov-thru-delete.html (accessed) |
| I11 | **Vertex AI Provisioned Throughput** (GSU) | GSU = N tokens/sec after burndown (Claude: 105–1,050 tok/s) | **1 week (Google models only), 1 month, 3 months, 1 year** | Claude minimum GSU: 1 (Fable 5.1, Opus 5), 25 (Sonnet 5 / 4.6), 35 (Opus 4.5–4.8), 8 (Haiku 4.5) | Burndown: output ×5, 5m write ×1.25, 1h write ×2, **hit ×0.1** (×0.025 Fable 5.1) | Default: **the whole request** goes pay-as-you-go ("spillover"). Header `dedicated` returns 429, `shared` bypasses | **Non-cancelable**; fees "apply regardless of actual usage or if the model is discontinued". Auto-renew option. GSU decreases apply at renewal. Model changes stay within the same publisher | docs.cloud.google.com/…/provisioned-throughput/{purchase…, supported-models, use…} (updated 2026-09-22) |
| I12 | **Azure PTU, hourly** | $/PTU-hr | none | 15 PTU (Global/DZ), 25–50 (Regional) | GPT-5.x: cached tokens "deducted 100%", so they don't consume PTU. GPT-6 Astra: normalized weights (cached 0.1, write 1.25) | Spillover to a standard deployment (`x-ms-spillover-deployment`) | "can't be paused", billing stops only on delete | learn.microsoft.com/…/provisioned-throughput (2026-07-15), …-billing (2026-05-22), …-sizing (2026-09-11) |
| I13 | **Azure PTU reservation** | PTU (model-independent) per deployment type | **1 month or 1 year** | any quantity; only deployed PTUs in scope benefit | as I12 | "Excess is billed hourly" | Refunds up to **US$50K per rolling 12 months**. Exchanges reset the term. A future 12% ETF is possible. Auto-renew option | …-billing (2026-05-22); exchange-and-refund (2026-07-22) |
| I14 | Azure **MACC** drawdown by marketplace spend | $ | [CONTRACT] | [CONTRACT] | n/a | n/a | Only "Azure benefit eligible" offers, bought through the Azure portal on an agreement subscription. Credit-card and Prepayment-funded purchases don't count | learn.microsoft.com/marketplace/azure-consumption-commitment-benefit (updated 2025-10-24) |
| I15 | AWS commitment (EDP/PPA) drawdown | $ | [CONTRACT] | [CONTRACT] | n/a | n/a | Claude Platform on AWS keeps "AWS commitment retirement" the same as Bedrock. Caps and eligibility are [CONTRACT] | claude-platform-on-aws page, "What stays the same" (accessed) |
| I16 | OpenAI **Scale Tier** and Enterprise spend commitment | "Scale Tier TPM bundles"; annual $ commitment | [unverified] | [unverified] | "eligible cached input tokens receive the same discounts" | "Scale Tier spillover traffic doesn't automatically move to Fast mode" | [unverified]: the dedicated pages returned 403/404 | developers.openai.com/api/docs/guides/fast-mode (accessed); openai-python `service_tier.py` (commit 2026-08-17) |
| I17 | GitHub Copilot Business / Enterprise | seat; **AI credits = seat price** (1,900 or 3,900 credits at $0.01) | monthly | none | Credits consumed by "input, output, and cached tokens" at API rates | $0.01/credit beyond the **enterprise pool** | Removed seats are billed to the end of the cycle | github.blog (2026-04-27, effective 2026-06-01); docs.github.com seats-and-billing (accessed) |
| I18 | Cursor Teams / Enterprise | seat: Std $40, Premium $120 (5× usage) | monthly | none | **Cursor Token Rate $0.25/MTok** on input, output **and cached** tokens (includes Auto and BYOK) | on-demand at list API + token rate | Teams usage is per-user and "does not transfer"; Enterprise pools usage | cursor.com/docs/account/teams/pricing (accessed) |
| I19 | Promotional credits (Vertex) | 50% monthly credit on Gemini 3.6/3.7/3.8 Flash PT | 2026-08-13 to 2026-12-31 | n/a | n/a | n/a | Credits "expire 30 days after issuance" | cloud.google.com/vertex-ai/generative-ai/pricing (accessed) |

---

## 3. Findings

### F1. Reserved and provisioned capacity costs the same as pay-as-you-go at 100% utilization. It is bought for SLOs, not discounts (`cc-capacity-parity`)

**Facts [LIST]:**
- **AWS Price List** (`AmazonBedrockFoundationModels`, us-east-1, publicationDate 2026-09-22T16:44:18Z), Claude Sonnet 4.6, global:
  - Reserved input: `$0.18` "Per Hour per 1K Input TPM Reserved 1 Month" and `$0.162` for 3 months.
  - Reserved output: `$0.90` (1 month) and `$0.81` (3 months).
  - On-demand is $3 input and $15 output per MTok.
  - Opus 4.6 is $0.30/$1.50 (1 month) against $5/$25 on-demand. Haiku 4.5 is $0.06/$0.30 against $1/$5.
  - Regional ("Geo") reserved prices are 1.1× global.
- **Vertex** GSU prices, global, per GSU-hour: 1 week $7.14, 1 month $3.6986 (≈$2,700/mo), 3 months $3.2877 (≈$2,400/mo), 1 year $2.7397 (≈$2,000/mo). Non-global endpoints are 1.1×.
- **Vertex** Claude throughput per GSU: Opus 5 / 4.x at 210 tok/s, Sonnet 5 / 4.6 at 350, Fable 5.1 at 105, Haiku 4.5 at 1,050.
- **Azure Retail Prices API** (eastus2, queried 2026-09-23):
  - Hourly PTU: Global $1.00, Data Zone $1.10, Regional $2.00 per PTU-hr.
  - Reservation, Global: $260/PTU per month (1-month term) or $2,652/PTU per year.
  - Reservation, Data Zone: $286 per month or $2,916 per year.
- Microsoft's own sizing page states for GPT-6 Astra: "At list price and full utilization, PTU and Global Standard are approximately equivalent on a normalized-token basis".

**Derived break-even utilization u\*** (reserved fee ÷ pay-as-you-go value of full capacity) [DERIVED]:

| Instrument | 1 wk | 1 mo | 3 mo | 1 yr | Hourly |
|---|---|---|---|---|---|
| Bedrock Reserved (Sonnet 4.6 / Opus 4.6 / Haiku 4.5, global) | – | **100%** | **90%** | – | – |
| Vertex GSU (Opus 5, Opus 4.x, Sonnet 4.6, Fable 5.1, Haiku 4.5) | 189% | **97.8%** | **87.0%** | **72.5%** | – |
| Vertex GSU, **Sonnet 5** ($2 list) | 283% | **146.8%** | **130.5%** | **108.7%** | – |
| Azure PTU (GPT-6 Astra, global) | – | **≈99%** | – | **≈84%** | **278%** |

- The **Vertex** Claude GSU throughputs are clearly calibrated to a common value. One GSU-month at 100% is worth $2,759 at each model's list price (for example, Opus 5: 210 tok/s × 2.628M s × $5/MTok). The only exception is Sonnet 5, whose GSU is still rated as if Sonnet 5 cost $3. The pricing page says the planned increase to $3/$15 "will not occur".
- **Minimum monthly exposure** [DERIVED]:
  - Bedrock Reserved minimum (100K input + 10K output TPM): **$19,710 per month** on Sonnet 4.6, **$32,850** on Opus 4.6, **$6,570** on Haiku 4.5 (1-month term).
  - Vertex Sonnet 4.6 minimum of 25 GSU: about **$67,500 per month**.
  - Azure: 15 PTU for $3,900 per month (reservation) or $10,950 (hourly).
- **FinOps Foundation** ("Effect of Optimization on AI Forecasting", 2026-03-17): fully provisioned at a "75% target peak utilization" costs "33% higher than the shared capacity rate". Allowing up to 30% failover to shared capacity left "<1% of all requests" reverted.

**Sources:**
- https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/current/us-east-1/index.json — AWS Price List (published 2026-09-22)
- https://docs.aws.amazon.com/bedrock/latest/userguide/service-tiers-inference.html (accessed 2026-09-23)
- https://cloud.google.com/vertex-ai/generative-ai/pricing — Provisioned Throughput section (accessed 2026-09-23)
- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/provisioned-throughput/supported-models (updated 2026-09-22)
- https://prices.azure.com/api/retail/prices (queried 2026-09-23)
- https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing (ms.date 2026-09-11)
- https://www.finops.org/wg/effect-of-optimization-on-ai-forecasting/ (2026-03-17)

**Caveats:**
- It is not documented whether the Vertex GSU price table applies to Claude orders. The calibration strongly suggests it does. Mark it [CONTRACT] until confirmed on an order.
- The AWS price list `unit` field reads "1M TPM Hour" while the description says "Per Hour per 1K Input TPM". Only the per-1K reading matches the docs ("fixed price per 1K tokens-per-minute") and produces parity.

**Token Bill:**
- Compute u\* per instrument from live price feeds. Refuse to label any reservation a "saving" unless the measured utilization is above u\* and there is an SLO reason.
- Report the "availability premium" in dollars: fee minus the pay-as-you-go value of the tokens actually consumed.

**Evidence:** strong (the Vertex Claude mapping is moderate).

### F2. Every Claude capacity commitment trails the newest and cheapest models (`cc-capacity-model-lag`)

**Facts [LIST]:**
- **Anthropic Priority Tier:**
  - "no longer available for purchase". Existing commitments run "through their contract end date".
  - A commitment is "A specific model version". It is "supported on all available Claude models except Claude Fable 5.1, Claude Mythos 5.1, Claude Mythos 5, Claude Mythos Preview, Claude Opus 5.5, Claude Opus 5, and Claude Sonnet 5".
- **Bedrock model cards** (parsed tier tables):
  - Reserved is available for **Opus 4.6 and Sonnet 4.6** but **not for Opus 5.5, Sonnet 5 or Fable 5.1**.
  - The price list has Reserved SKUs only for Haiku 4.5, Opus 4.5, Opus 4.6, Sonnet 4.5 and Sonnet 4.6.
- **Vertex:**
  - The Provisioned Throughput partner-model table lists no Opus 5.5 (updated 2026-09-22).
  - Model changes on an order are "limited to a specific publisher". Console self-service changes apply only to "online orders for Google models".
- **Successors are cheaper at list:**
  - Opus 5.5 is $4/$20 with $0.20 reads, against $5/$25 and $0.50 for Opus 4.6–5.
  - Sonnet 5 is $2/$10 against $3/$15 for Sonnet 4.6.

**Derived [DERIVED]:**
- Staying on Reserved Sonnet 4.6 at parity instead of pay-as-you-go Sonnet 5 costs **+50% per input and output token**.
- Staying on Priority Opus 4.8 instead of standard Opus 5.5 costs **+25% on input and output list price** and **+150% on cache reads**. Anthropic states Opus 5.5 "will cost 40% less than Opus 5 on typical workloads".

**Sources:**
- https://platform.claude.com/docs/en/api/service-tiers (accessed 2026-09-23)
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-5-5.html and the sonnet-5, fable-5-1, sonnet-4-6 and opus-4-6 cards (accessed 2026-09-23)
- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/provisioned-throughput/purchase-provisioned-throughput (2026-09-22)
- https://www.anthropic.com/news/claude-opus-5-5 (2026-09-22)

**Token Bill:** add a "capacity bound to a superseded model" detector:
- Monthly stranded $ = Σ (bound-model tokens × (bound rate − successor rate)), with measured cost per task where evals exist.
- Emit a migration plan tied to each instrument's change window: Vertex needs account-rep changes; Bedrock needs the account manager; Priority Tier ends at contract end.

**Evidence:** strong.

### F3. Price deflation and repricing cadence create stranded-commitment risk (`cc-deflation-hazard`)

**Facts [LIST]:**
- Opus-tier list input prices:
  - $15 for Opus 4.1 (`claude-opus-4-1-20250805`)
  - $5 for Opus 4.5 (retirement "not sooner than November 24, 2026")
  - $4 for Opus 5.5 (2026-09-22)
- **Fable 5.1** (2026-09-01): "Cache reads now cost 75% less". "For typical workloads, costs are reduced by around 25% … up to around 45%" on agentic tasks.
- **Opus 5.5** (2026-09-22): "20% less than Opus 5" on list price, reads "60% less", "40% less than Opus 5 on typical workloads".
- **Sonnet 5**: the introductory $2/$10 "is now the standard price". The scheduled increase to $3/$15 "will not occur". This is a reprice upward that was cancelled, which is exactly what mis-rated the Vertex Sonnet 5 GSU (F1).
- **Epoch AI** (2025-03-12): the price to reach a fixed capability level fell "9x to 900x per year" depending on the milestone, and "40x per year" for GPT-4-level GPQA.
- **Vertex** Provisioned Throughput: "term fees are not cancelable for the duration of the term and will apply regardless of actual usage or if the model is discontinued … Google won't proactively cancel auto-renewal for discontinued models."
- **CCU** has the opposite property. "The rate in effect at the time of each call applies, so changes to Anthropic's published rates take effect immediately."

**Derived [DERIVED]:** −73% on Opus-tier list input in about 13.5 months, and two major Anthropic cuts 21 days apart.

**Sources:**
- https://platform.claude.com/docs/en/about-claude/pricing (accessed)
- https://platform.claude.com/docs/en/about-claude/model-deprecations (accessed)
- https://www.anthropic.com/claude-fable-and-mythos-5-1 (2026-09-01)
- https://www.anthropic.com/news/claude-opus-5-5 (2026-09-22)
- https://epoch.ai/data-insights/llm-inference-price-trends (2025-03-12)
- https://docs.cloud.google.com/…/purchase-provisioned-throughput (2026-09-22)
- https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing (2026-08-14)

**Token Bill:**
- Forecast dollar demand as tokens × price path, with a price-cut hazard. Base it on vendor history plus announced launches (Sonnet 5.5 and Haiku 5.5 are due "in the coming weeks").
- Prefer dollar commitments that are not tied to a model (CCU private offers) over capacity tied to a model.
- Cap terms at the shorter of the instrument term and the expected time to the next successor.

**Evidence:** strong.

### F4. What cached tokens consume differs by instrument, and that moves break-even (`cc-cache-consumption-matrix`)

**Facts [LIST]:**
- **Anthropic Priority:** burndown is 0.1 for reads, 1.25 for 5m writes, 2.00 for 1h writes, and 1.1× for `inference_geo: "us"`. "These burndown rates reflect the relative pricing of each token type."
- **Bedrock:**
  - Reserved consumption "includes both `InputTokenCount` and `CacheWriteInputTokens`". Cache reads "don't contribute" to quota.
  - Output burndown on on-demand quotas is **10×** for Opus 5.5, Sonnet 5, Opus 5 and Fable 5.1, **15×** for Claude 4.8, and 5× for ≤4.7.
  - Quota is deducted at request start as input + `max_tokens`.
- **Vertex Claude GSU:** 1 input = 1, output = 5, 5m write = 1.25, 1h write = 2, hit = 0.1 (Fable 5.1: 0.025). For Sonnet 4.5 at ≥200K input, input = 2, output = 7.5, hit = 0.2.
- **Azure GPT-5.x PTU:** "Cached tokens are deducted 100% from the utilization calculation". PTUs = (input TPM × (1 − cache rate) + ratio × output TPM) ÷ input TPM per PTU. With a 50% cache rate, Microsoft's worked example drops from 110 to 80 PTUs.
- **Azure GPT-6 Astra:** normalized tokens, with cached input at 0.1 and cache write at 1.25, mirroring price.

**Derived implications:**
- **Vertex and Priority** burn down at price ratios, so caching is neutral for break-even. It only frees capacity.
- **Azure GPT-5.x** consumes nothing for cached input, while pay-as-you-go still bills it at the cached rate. A high-cache workload therefore improves PTU economics compared with pay-as-you-go.
- **Bedrock Reserved** bills cache writes at 1× capacity, against 1.25× or 2× pay-as-you-go. Write-heavy workloads get an effective discount on writes. How cache reads on Reserved requests are billed is not documented (open question).

**Sources:**
- https://platform.claude.com/docs/en/api/service-tiers
- https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html (accessed)
- https://docs.cloud.google.com/…/supported-models (2026-09-22)
- https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing (2026-09-11)

**Token Bill:**
- Store a burndown map per (instrument, model, token type).
- In capacity mode, express every cache fix as freed capacity units, and as dollars only where it lets a reservation shrink at the next change window.
- Add `max_tokens` right-sizing for Bedrock. It frees start-of-request quota without changing the bill.

**Evidence:** strong.

### F5. Sizing rule: reserve up to the demand level exceeded a fraction r of the time, and size spend commitments near a low quantile (`cc-sizing-newsvendor`)

**Derivation [DERIVED]:**
- Let capacity C be reserved at price ratio r, where r is the reserved fee divided by the pay-as-you-go value of full capacity (u\* from F1). Overflow is paid at pay-as-you-go.
- Expected cost = r·p·C·T + p·E∫(D(t) − C)⁺dt. Setting d/dC = 0 gives **P(D > C\*) = r**.
- In words: reserve up to the burndown-adjusted demand level that is exceeded r of the time, measured per enforcement window on the load-duration curve.
- Examples: Vertex 1-year (r = 0.725) reserves the level exceeded 72.5% of minutes. Bedrock 3-month (r = 0.9) reserves the level exceeded 90% of minutes. When r ≥ 1, reserve **nothing** unless an SLO requires it.
- Anthropic's usage report offers `1m` buckets (up to 1,440). That is enough to build the curve for first-party traffic.

**Spend commitments (take-or-pay, discount d on all spend) [DERIVED, hypothetical tiers]:**
- Use a lognormal monthly demand with P10/P50/P90 = $0.64M / $1.00M / $1.57M.
- Tiers of (C = 0.8, d = 10%), (1.0, 15%), (1.2, 20%), (1.5, 25%) give expected cost $0.991M / $1.001M / $1.038M / $1.153M, against $1.064M pay-as-you-go.
- **The best choice is the small commitment near P25.**
- Add a 25% mid-term price cut and the 15%-at-P50 commitment becomes worse than pay-as-you-go ($0.937M against $0.931M).
- Use P50 for the expected-value decision and P90 or CVaR to cap exposure.

**Research:**
- **Wang, Li and Liang**, "To Reserve or Not to Reserve" (arXiv:1305.5608, 2013, USENIX ICAC 2013). Online reservation algorithms that need no demand forecast are (2−α)-competitive (deterministic) and e/(e−1+α)-competitive (randomized), where α is the reservation discount parameter. Those are the best possible ratios. This suits a monthly ladder when forecasts are poor.
- **Bergemann and Wang**, "Optimal Pricing of Cloud Services: Committed Spend under Demand Uncertainty" (arXiv:2502.08022, 2025-02-11). The vendor-optimal contract gives larger discounts for larger fixed payments, and can be implemented as a committed-spend contract. The buyer's forecast signal is exactly what the vendor screens on.
- **Ambati, Irwin and Shenoy**, "No Reservations" (arXiv:2005.12249, HotCloud 2020). Reservations can save "up to 60%" but carry "demand risk". A resale marketplace mitigates that risk. LLM capacity has no such secondary market (only Azure's $50K refund cap and Vertex order splits), so buyers should hold less.

**Token Bill:** implement the closed-form rule plus a replay simulator for spillover granularity. Vertex spills **the whole request** and enforces over dynamic windows of 1–120 s depending on GSU count.

**Evidence:** strong for the math, moderate for applying it to LLMs.

### F6. Overflow and spillover mechanics differ, and some overflow is priced far above list (`cc-overflow-mechanics`)

**Facts [LIST]:**
- **Bedrock Reserved** "automatically overflows to the Standard tier". `ResolvedServiceTier` in CloudWatch "shows the actual tier that served your requests". The tier is also "visible in API response and AWS CloudTrail Events".
- **Vertex:**
  - "If a request exceeds the remaining Provisioned Throughput quota, the entire request is processed as an on-demand request … billed at the pay-as-you-go rate". It shows as `request_type=spillover`.
  - `X-Vertex-AI-LLM-Request-Type: dedicated` returns 429 instead. `shared` bypasses provisioned capacity.
  - Output is estimated up front, and quota is reconciled after the response.
- **Azure:**
  - Spillover routes non-200 responses to a standard deployment via `x-ms-spillover-deployment`. It is not supported for DeepSeek or Llama.
  - Deployed PTUs above the reservation quantity are "billed at the standard hourly rate". That is $1.00/PTU-hr against about $0.356 effective under a 1-month reservation, **2.8×** [DERIVED].
- **Anthropic Priority** falls back to standard. "Requests assigned Priority Tier pull from both the Priority Tier capacity and the regular rate limits".
- **OpenAI:** "Scale Tier spillover traffic doesn't automatically move to Fast mode."

**Sources:**
- service-tiers-inference.html (AWS)
- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/provisioned-throughput/use-provisioned-throughput (2026-09-22)
- https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput (2026-07-15) and …-billing (2026-05-22)
- https://developers.openai.com/api/docs/guides/fast-mode (accessed)

**Token Bill:** detector "overflow at list or hourly" (§6.3 D2).

**Evidence:** strong.

### F7. Cancellation and renewal traps are where commitments quietly overrun (`cc-renewal-traps`)

**Facts [LIST]:**
- **Bedrock Reserved:** "Billing continues until you delete the Reserved Tier reservation with the help of your AWS account manager."
- **Bedrock Provisioned Throughput:**
  - Commit PTs "can't [be] delete[d] … before the commitment term is complete".
  - PTs "automatically renew at the end of each commitment term".
  - "Auto renew cannot be re-enabled after it is cancelled".
  - The 6-month discount is large. Llama models are shown at $21.18/hr (1-month) against $13.08/hr (6-month), −38%.
- **Vertex:**
  - The order is "a commitment, which means that you can't cancel the order in the middle of your term".
  - GSU decreases apply "during auto-renewal for the next term".
  - No changes are allowed if the order "expires in less than five days and isn't configured for auto-renewal".
  - Weekly terms can't auto-renew.
  - Orders can be split for partial migrations.
- **Azure reservations:**
  - "The total canceled commitment can't exceed 50,000 USD in a 12-month rolling window".
  - "in the future there might be a 12% early termination fee".
  - An exchange "has a new term starting from the time of exchange".
  - Refunds "are calculated based on the lowest price of either your purchase price or the current price". Price cuts therefore shrink refunds.

**Sources:**
- https://docs.aws.amazon.com/bedrock/latest/userguide/prov-thru-delete.html (accessed)
- https://docs.aws.amazon.com/bedrock/latest/userguide/prov-throughput.html (accessed)
- https://aws.amazon.com/bedrock/pricing/ (accessed)
- https://docs.cloud.google.com/…/purchase-provisioned-throughput (2026-09-22)
- https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/exchange-and-refund-azure-reservations (2026-07-22)

**Token Bill:** a commitment calendar holding each term end, auto-renew state, notice deadline (Vertex: at least 5 days) and remaining refund headroom (Azure: $50K), with alerts 30, 14 and 5 days out.

**Evidence:** strong.

### F8. CCU marketplace billing: discounts applied as fewer CCUs, with no commitment, but easy to leak (`cc-ccu-private-offers`)

**Facts [LIST]:**
- **Claude Platform on AWS and Foundry:**
  - The CCU price is $0.01 and "fixed; discounts apply at token-to-CCU conversion".
  - Metering is hourly and payment is in arrears. There are "no prepaid credits".
  - "Your AWS bill shows a single CCU line item".
- **Claude Platform on AWS:**
  - "There is no CCU balance or commitment."
  - "Discounts cannot be applied retroactively to usage incurred before your private offer is accepted."
  - "Negotiated discounts and AWS Marketplace private offers don't transfer automatically between Bedrock and Claude Platform on AWS."
  - "Spend is calculated at list prices and can take about 2 hours to reflect recent usage … The overshoot is billed."
  - New organizations start on the Start tier, with its monthly spend cap.
  - The programmatic usage and cost APIs are "not currently available".
- **Foundry:**
  - "A private offer can carry different discount rates for different Claude models."
  - "CCU billing applies to Claude deployments you create in Foundry going forward." Older deployments stay on per-model token billing.
  - "Foundry estimates don't reflect private-offer discounts."

**Derived:** effective discount = 1 − (CCU × $0.01) ÷ (list-priced token usage for the same hour or day).
- On Claude Platform on AWS, list-priced usage comes from the Console Cost page or Token Bill traces.
- On Foundry, it comes from the Monitor tab's per-model tokens × list rates.

**Sources:**
- https://platform.claude.com/docs/en/about-claude/pricing (accessed)
- https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws (accessed)
- https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing (2026-08-14)

**Token Bill:** detectors for pre-acceptance usage, legacy (non-CCU) Foundry deployments, realized discount below contract, and spend caps computed at list (translate caps into invoice dollars).

**Evidence:** strong.

### F9. Marketplace spend and commitment drawdown: eligibility rules that silently fail (`cc-marketplace-drawdown`)

**Facts [LIST]:**
- **Azure MACC:**
  - "Eligible Microsoft Marketplace purchases automatically count toward fulfilling this commitment."
  - "100% of the pretax purchase amount also contributes" for "Azure benefit eligible" offers.
  - It does not count for purchases "directly on Microsoft Marketplace via credit card", nor for purchases "using Azure prepayment".
  - Claude CCU "is **MACC-eligible**", but "CCU billed by other cloud providers doesn't decrement your Microsoft Azure Consumption Commitment".
  - "you can't use Azure Prepayment credit to pay for charges for other provider models because they're billed through Azure Marketplace".
- **AWS:**
  - Claude Platform on AWS lists "AWS commitment retirement" under "What stays the same" compared with Bedrock.
  - Claude on Bedrock appears in the price list as AWS Marketplace usage types (`USE1-MP:…`, "AWS Marketplace software usage").
  - For private offers, "Member accounts that were previously subscribed to the product must also accept the new private offer to benefit from the pricing".
  - When an agreement expires "You will either automatically move to the product's public pricing or lose your subscription".
  - "Buyers can only be subscribed to one offer at any given time."
- Drawdown caps and percentages on AWS EDP/PPA are **[CONTRACT]**. No public AWS page stating them was found in this pass.

**Sources:**
- https://learn.microsoft.com/en-us/marketplace/azure-consumption-commitment-benefit (updated 2025-10-24)
- https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing (2026-08-14)
- https://learn.microsoft.com/en-us/azure/foundry/concepts/manage-costs (2026-08-27)
- https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws (accessed)
- https://docs.aws.amazon.com/marketplace/latest/buyerguide/buyer-private-offers.html (accessed)
- https://docs.aws.amazon.com/marketplace/latest/userguide/private-offers-overview.html (accessed)

**Token Bill:** detector "uncredited marketplace spend" (§6.3 D5).

**Evidence:** strong for Azure, moderate for AWS (terms are contract-dependent).

### F10. Anthropic enterprise discounts can be observed programmatically: `amount` vs `list_amount`, and cost ÷ usage (`cc-anthropic-discount-visibility`)

**Facts [LIST]:**
- The pricing page says volume discounts are "negotiated on a case-by-case basis".
- **Enterprise Analytics** `cost_report` and `user_cost_report`:
  - `amount` is "post-discount, pre-credit". `list_amount` is "pre-discount".
  - Grouping is available by `product`, `model`, `token_type`, `rbac_group_id`, `speed` and `inference_geo`, in 1d, 1h or 1m buckets.
  - Values "can be revised for up to 30 days". A `data_refreshed_at` watermark marks the final point.
  - "The cost and usage endpoints apply to usage-based Enterprise plans; for seat-based Enterprise plans, they reflect usage credits only."
  - RBAC group rows overlap ("any-membership semantics").
- **Console** `cost_report`:
  - Daily only, one `amount` in cents, and no `list_amount`.
  - Grouped by description, it returns `model`, `token_type`, `service_tier` (batch or standard), `context_window` and `inference_geo`.
  - "Priority Tier costs … are not included".
- Anthropic's own cost guide says realized rates come "from dividing cost-report amounts by the usage report's matching token counts — same model, same token type".
- **Claude Code** `modelPricing` (managed setting):
  - `multiplier` (a discount below 1, a markup up to 10) and per-model `overrides` for input, output, cacheRead and cacheWrite.
  - It applies to `/usage`, the status line, the SDK's `total_cost_usd`, `--max-budget-usd` and the OTel cost metric.

**Sources:**
- https://platform.claude.com/docs/en/api/admin/analytics (accessed)
- https://platform.claude.com/docs/en/manage-claude/analytics-api (accessed)
- https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-cost-report (accessed)
- https://platform.claude.com/docs/en/manage-claude/usage-cost-api (accessed)
- Bundled `claude-api/shared/cost-optimization.md` (local)
- https://code.claude.com/docs/en/settings-reference (accessed)

**Token Bill:**
- Compute effective discount per model and token type daily.
- Generate the `modelPricing` managed-settings JSON from realized rates, so every developer's `/usage` shows contract dollars.
- Hold finance-grade totals until day +30.

**Evidence:** strong.

### F11. Legacy Anthropic Priority Tier: measure it, reconcile it outside the cost report, and plan the exit (`cc-priority-legacy`)

**Facts [LIST]:**
- A commitment is ITPM + OTPM for 1, 3, 6 or 12 months on one model version, targeting 99.5% uptime.
- `service_tier: "auto" | "standard_only"`. `usage.service_tier` returns `priority` when the request ran on the tier.
- The `anthropic-priority-{input,output}-tokens-{limit,remaining,reset}` headers indicate eligibility.
- Usage is available via the usage report's `service_tier` dimension. Costs are **not** in the cost endpoint.

**Source:** https://platform.claude.com/docs/en/api/service-tiers and https://platform.claude.com/docs/en/manage-claude/usage-cost-api (accessed).

**Token Bill:**
- Compute utilization per minute from the headers or usage.
- Flag any `standard_only` traffic on committed models, which wastes the commitment.
- Price the exit: at contract end, move to Opus 5.5 at standard (F2).

**Evidence:** strong.

### F12. Bedrock Reserved tier in detail (`cc-bedrock-reserved`)

**Facts [LIST]:**
- Separate input and output TPM capacities. "Customers can reserve capacity for 1 month or 3 month duration". "Customers pay a fixed price per 1K tokens-per-minute and are billed monthly".
- The minimum is 100,000 input TPM and 10,000 output TPM. Access requires the AWS account team.
- The reservation is "separate from your on-demand quota". It is set at the account level, not per request.
- Price list (global, 1-month / 3-month, per 1K TPM-hour):

| Model | Input 1 mo | Input 3 mo | Output 1 mo | Output 3 mo |
|---|---|---|---|---|
| Opus 4.5 / 4.6 | $0.30 | $0.27 | $1.50 | $1.35 |
| Sonnet 4.5 / 4.6 | $0.18 | $0.162 | $0.90 | $0.81 |
| Haiku 4.5 | $0.06 | $0.054 | $0.30 | $0.27 |

- Regional prices are ×1.1.

**Derived:** the 3-month term is exactly a 10% discount, and 1-month is parity (F1). Minimum monthly spend is $6.6K–$32.9K.

**Sources:**
- service-tiers-inference.html
- quotas-token-burndown.html
- AWS Price List v20260922164418

**Token Bill:** ingest CloudWatch `ResolvedServiceTier` and token metrics by `ServiceTier`, plus the price list. Compute utilization of reserved input and output TPM separately, since output is usually the binding constraint.

**Evidence:** strong.

### F13. Vertex Provisioned Throughput in detail (`cc-vertex-pt`)

**Facts [LIST]:**
- Terms are 1 week (Google models only), 1 month, 3 months and 1 year.
- Orders take "from a few minutes to a few weeks". Billing starts only when the order is Active.
- Increases apply immediately. Decreases apply at renewal.
- Model and region changes are "typically fulfilled within 10 business days".
- Provisioned Throughput "doesn't support batch prediction calls" and requires explicit model version IDs, not aliases.
- **Promo:** "Effective August 13, 2026 through December 31, 2026, Google Cloud is injecting a monthly billing credit equal to 50% of net eligible Provisioned Throughput spending on Gemini 3.8 Flash, Gemini 3.7 Flash, and Gemini 3.6 Flash". The credits "expire 30 days after issuance".
- **Monitoring** (preview) uses `aiplatform.googleapis.com/publisher/online_serving/{dedicated_gsu_limit, consumed_token_throughput, dedicated_token_limit, token_count}` with `request_type ∈ {dedicated, spillover, shared}`. "Anthropic models also have a filter for Provisioned Throughput but only for tokens and token_count."

**Sources:**
- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/provisioned-throughput/purchase-provisioned-throughput, …/use-provisioned-throughput, …/supported-models (all updated 2026-09-22)
- https://cloud.google.com/vertex-ai/generative-ai/pricing (accessed)

**Token Bill:** a Vertex connector (Cloud Monitoring and Billing export). Because Claude lacks the throughput metrics, compute Claude utilization from token counts × burndown ÷ (GSU × tok/s).

**Evidence:** strong.

### F14. Azure PTU: hourly vs reservation vs pay-as-you-go (`cc-azure-ptu`)

**Facts [LIST]:**
- PTUs are "Model-independent" and region-specific. Reservations are per deployment type (Global, Data Zone, Regional), apply to matching scope, and "don't guarantee capacity".
- Microsoft says to "create deployments first, then purchase the Azure Reservation".
- Customers from before August 2024 may still be on the legacy "Commitment model". The retail API still shows a `$312/Month` "Provisioned Throughput Units" meter.

**Derived:**
- A 1-month reservation is 64.4% off hourly. A 1-year reservation is 69.7% off hourly, or 15% off the 1-month reservation.
- Against pay-as-you-go, see F1.

**Sources:**
- learn.microsoft.com …/provisioned-throughput (2026-07-15)
- …/provisioned-throughput-billing (2026-05-22)
- Azure Retail Prices API (2026-09-23)

**Token Bill:** detect PTU deployments above reservation quantity (billed hourly at 2.8×), and reservations below 100% utilization. Use the Azure reservation utilization export.

**Evidence:** strong.

### F15. OpenAI Scale Tier still exists as a purchasable product, but its terms could not be verified (`cc-openai-scale-tier`)

**Facts:**
- The openai-python `ServiceTier` literal still includes `"scale"` (file last changed 2026-08-17).
- OpenAI's Fast mode guide (Priority processing was "renamed Fast mode on July 30, 2026") refers to "purchased Scale Tier TPM bundles", "Scale Tier spillover traffic", and "consider purchasing Scale Tier quota".
- The same guide says: "All processing modes count toward your annual Enterprise spend commitment".
- It also says "GPT-5.6 Sol's promotional pricing is available at least through November 21, 2026".
- openai.com/api-scale-tier and help.openai.com returned 403, and the developers guide `/guides/scale-tier` returned 404. Unit prices, term and minimums are **unverified**.

**Sources:**
- https://developers.openai.com/api/docs/guides/fast-mode (accessed 2026-09-23)
- https://github.com/openai/openai-python/blob/main/src/openai/types/responses/service_tier.py (commit ff14a33c, 2026-08-17)
- https://developers.openai.com/api/docs/pricing (accessed)

**Token Bill:** model Scale Tier as a generic "TPM bundle" instrument with contract-supplied terms, and track `service_tier` in responses.

**Evidence:** weak for terms, moderate for existence.

### F16. Seat vs usage for Claude: the org-level gap is about 2–3.5×, and assignment is per developer (`cc-seat-vs-usage`)

**Facts [LIST]:**
- **Plan prices:**
  - Team Standard is "$20 Per seat / month if billed annually. $25 if billed monthly".
  - Team Premium is "$100 … annually. $125 … monthly".
  - Enterprise is "Seat price + usage at API rates US$20/seat/month, billed annually".
- **Seat allowance:** "each member's Claude Code usage draws from a per-seat allowance that resets on a rolling five-hour window and a weekly window … its size depends on the member's seat tier". "Usage inside the seat allowance isn't metered in dollars."
- **Usage credits** are "billed at standard API rates", prepaid, with org, group and member spend limits.
- **Mixing:** "If your organization mixes sign-in methods, each developer is metered according to the one they authenticated with."
- **Older Enterprise plans:** "Claude Code is available on Chat + Claude Code seats (usage-based billing) and Premium seats (seat-based billing)".
- **Fleet benchmark:** "the average cost is around $13 per developer per active day and $150-250 per developer per month, with costs remaining below $30 per active day for 90% of users".
- **Quesma** (Jacek Migdal, 2026-08-11):
  - Individual Max plans run 12–40× API-equivalent (Max 20x at $200 ≈ $8,000).
  - At org level, Pylon paid "$400K/yr vs ~$1.4M/yr projected (3.5x)".
  - Leaders "saw the bill at least double. Most reported roughly 3x".
  - Advice: "move to Enterprise only once you outgrow 150 seats".

**Derived:**
- A Team Premium seat beats usage-based Enterprise for a developer whose list-equivalent usage U satisfies U > $80 ÷ (1 − d) **and** who stays inside the Premium allowance. Here d is the [CONTRACT] Enterprise discount.
- The average developer ($150–250/mo) fits that description. The top decile (over $30/active day) may exhaust the allowance and pay usage credits at API rates.
- **Caveat:** the seat numbers are throttled demand, so the 3.5× includes usage that seats would have blocked.

**Sources:**
- https://claude.com/pricing (accessed)
- https://code.claude.com/docs/en/costs (accessed)
- https://support.claude.com/en/articles/11845131 (updated 2026-09-22)
- https://support.claude.com/en/articles/12429409 (2026-08-10)
- https://quesma.com/blog/claude-code-pricing-for-enterprise/ (2026-08-11)

**Token Bill:** the seat-assignment optimizer (§6.2).

**Evidence:** strong for mechanics, moderate for the 3.5× figure (a single org, anecdotal).

### F17. The billing path changes the default prompt-cache TTL, so seat and API cost comparisons must be replayed, not scaled (`cc-ttl-billing-path`)

**Facts [LIST]:**
- "Claude Code requests the one-hour TTL only on a Claude subscription within your plan's included usage."
- The main conversation gets 1h there, and 5m on "Usage credits, API key, or cloud provider". "Once you go over your plan's usage limit … Claude Code drops the main conversation to the cheaper five-minute TTL."
- Overrides are available via `promptCacheTtl` / `CLAUDE_CODE_PROMPT_CACHE_TTL` and `subagentPromptCacheTtl`.

**Local evidence:** a single-developer replay elsewhere in this research set (`empirical.md`) estimated about 12% main-thread overpay at 5m compared with 1h for a heavy user. This is internal and not public.

**Source:** https://code.claude.com/docs/en/prompt-caching (accessed 2026-09-23).

**Token Bill:**
- When the optimizer moves a user between seat, usage credits and API key, re-price with the replay engine under the new TTL.
- Emit the managed setting `promptCacheTtl: "1h"` for heavy API-key users with long gaps.

**Evidence:** strong.

### F18. GitHub Copilot: the seat is a pooled prepaid credit bundle plus unlimited completions (`cc-copilot-credits`)

**Facts [LIST]:**
- "Copilot Business at $19 USD per user per month, includes 1,900 AI credits per user". Enterprise is "$39 … 3,900 AI credits".
- "Each license contributes AI credits to a shared enterprise pool, and usage beyond the pool is charged at $0.01 USD per AI credit."
- "Code completions and next edit suggestions are not billed in AI credits".
- Credits are consumed by "input, output, and cached tokens, according to the published API rates".
- Business and Enterprise received "promotional bonus credits for the first three months (June–August)" of 2026.
- "When you remove seats, billing for those seats continues until the end of the current billing cycle."
- `ai_credits_used` per user per day "is a metrics signal … not a billed total" and is "not currently broken down by feature, model, or surface".

**Sources:**
- https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/ (2026-04-27)
- https://docs.github.com/en/copilot/concepts/billing-and-usage/organizations-and-enterprises/seats-and-billing-cycles (accessed)
- https://docs.github.com/en/billing/concepts/product-billing/github-copilot-licenses (accessed)
- https://github.blog/changelog/2026-06-19-ai-credits-consumed-per-user-now-in-the-copilot-usage-metrics-api/ (2026-06-19)

**Token Bill:**
- Track pool utilization and flag the September promo cliff.
- Assign seats to completion users. Heavy agent users burn credits at list and may be cheaper on a discounted direct contract [CONTRACT].

**Evidence:** strong.

### F19. The Cursor Token Rate ($0.25/MTok, including cached tokens and BYOK) can exceed the model's own cache price (`cc-cursor-token-rate`)

**Facts [LIST]:**
- Teams Standard is $40/user/mo. Premium is $120 with "5x the usage".
- Teams included usage "does not transfer between team members". "Our Enterprise plan offers pooled usage".
- "The Cursor Token Rate is $0.25 per million tokens … applies to input tokens, output tokens, and cached tokens on eligible third-party model requests. This includes when Auto routes to a third-party model. This applies to BYOK as well." First-party models are exempt.
- The Admin API returns `totalCents`, `chargedCents` and `cursorTokenFee` per event, with spend-limit endpoints.

**Derived** (illustrative agent mix per 1M tokens: 94% cache read, 3% uncached input, 2% 5m write, 1% output):

| Model | Model cost per 1M tokens | Cursor fee as % | Cost of a cached token vs direct |
|---|---|---|---|
| Opus 5.5 | $0.608 | +41% | 2.25× |
| Sonnet 5 | $0.398 | +63% | 2.25× |
| Fable 5.1 | $1.285 | +19% | 2.00× |

**Sources:**
- https://cursor.com/docs/account/teams/pricing (accessed)
- https://cursor.com/docs/account/teams/admin-api (accessed)
- https://cursor.com/pricing (accessed)

**Token Bill:** detector "channel fee" (§6.3 D9).

**Evidence:** strong for the fee, moderate for the mix, which Token Bill should measure per org.

### F20. Timing levers: deprecation floors, promo cliffs and launch windows (`cc-timing-calendar`)

**Facts [LIST]:**
- **Anthropic deprecation policy:** "at least 60 days' notice before model retirement". The table uses "Not sooner than" retirement dates, which fall one year after launch. For example, Opus 5.5 is "Not sooner than September 22, 2027" and Fable 5.1 "September 1, 2027".
- **Nearest floors:**
  - Sonnet 4.5: not sooner than 2026-09-29
  - Haiku 4.5: 2026-10-15
  - Opus 4.5: 2026-11-24
  - Opus 4.6: 2027-02-05
  - Sonnet 4.6: 2027-02-17
- **Retired on 1P but still billed on partner clouds:** Opus 4.1 and Sonnet 4 remain available on Bedrock and Google Cloud, and Opus 4 on Google Cloud. Opus 4.1 and Opus 4 cost $15/$75.
- **Promotions:**
  - Gemini 3.8 Flash is "$0.75 through December 31, 2026. $1.50 starting January 1, 2027". Output, caching, storage, Flex and Priority all double at the same time. On Vertex the promo is delivered "through 50% credits back on net spend".
  - The Vertex Provisioned Throughput 50% credit ends 2026-12-31.
  - GPT-5.6 Sol promo runs "at least through November 21, 2026".
  - The Copilot bonus credits ended in August 2026.
- **Launches:** "Claude Sonnet 5.5 and Claude Haiku 5.5 will follow in the coming weeks" (2026-09-22).
- **Azure:** the reservation exchange policy changes 2027-02-01 for savings-plan-covered services. PTU coverage is not stated.

**Sources:**
- https://platform.claude.com/docs/en/about-claude/model-deprecations (accessed)
- https://platform.claude.com/docs/en/about-claude/pricing (accessed)
- https://ai.google.dev/gemini-api/docs/pricing (updated 2026-09-23)
- https://cloud.google.com/vertex-ai/generative-ai/pricing (accessed)
- https://developers.openai.com/api/docs/guides/fast-mode (accessed)
- https://www.anthropic.com/news/claude-opus-5-5 (2026-09-22)
- https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/exchange-and-refund-azure-reservations (2026-07-22)

**Token Bill:** a machine-readable pricing and lifecycle calendar, with alerts:
- Promo cliff: projected bill after the promo ends.
- Do not commit before an announced launch.
- Deprecation floor inside a commitment term.
- Legacy premium: traffic on $15/$75 models on partner clouds.

**Evidence:** strong.

### F21. Reconciliation matrix: what can be read programmatically, and the gaps (`cc-reconciliation-matrix`)

| Channel | Usage source | Cost source | Effective-rate formula | Known gaps |
|---|---|---|---|---|
| Anthropic Console | `usage_report/messages` (1m/1h/1d; model, token type incl. 5m/1h writes, service_tier, geo, speed, workspace, api_key) | `cost_report` (1d, cents, token_type, service_tier) | cost ÷ matching tokens per (model, token_type, day) | Priority Tier cost excluded. Cost report has no api_key filter |
| Claude Enterprise | Analytics `usage_report` | Analytics `cost_report`, `user_cost_report` (`amount`, `list_amount`) | 1 − amount/list_amount | Seat-based plans show credits only. Revisions for 30 days. Bedrock Claude Code activity missing |
| Claude Code (Console) | Claude Code Analytics API (per user per day) | `estimated_cost` | – | Estimate at list; 1P only |
| Claude Platform on AWS | Console only (no API) | AWS CUR: single CCU line | CCU × $0.01 ÷ list-priced usage | No usage or cost API |
| Foundry (Claude) | Foundry Monitor tab (per-model tokens) | Cost Management: single CCU meter | as above | Project tags not supported for Marketplace models. Estimates exclude discounts |
| Bedrock | CloudWatch (`InputTokenCount`, `CacheWrite…`, `ResolvedServiceTier`), invocation logs | CUR 2.0 (hour/day by usage type; caller identity) | CUR $ ÷ CloudWatch tokens per model/usage type/day | No per-request IDs in CUR. Log cost excludes discounts, commitments and PT |
| Vertex | Cloud Monitoring (`token_count`, `request_type`) | Billing export (SKU) | SKU $ ÷ tokens | Claude has only token filters on PT metrics |
| Azure OpenAI | Azure Monitor | Cost Management plus reservation utilization | – | Ingestion delay |
| Price feeds | – | AWS Price List API (incl. Reserved SKUs); Azure Retail Prices API (PTU hourly and reservation); Vertex pricing page | – | Vertex has no JSON feed found |
| Copilot | `ai_credits_used` (metrics) | Billing usage | – | Not billing-grade; no model breakdown |
| Cursor | `/teams/filtered-usage-events` | `chargedCents`, `cursorTokenFee` | – | 20 req/min |

**Sources:** as cited in F8–F13 and F18–F19, plus https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-faq.html (accessed).

**Evidence:** strong.

### F22. The same model is priced differently across channels, which creates arbitrage and drift (`cc-channel-divergence`)

**Facts [LIST]:**
- Vertex lists **Opus 5.5 Batch Input $2.50 / Batch Output $12.50**, against 1P batch at $2 / $10. Vertex 5m batch writes are $2.50 and batch hits $0.10.
- Bedrock's price list shows no Opus 5.5 batch SKU, and its model card shows Batch unsupported for Opus 5.5.
- Regional or multi-region endpoints carry "a 10% premium over global". `inference_geo: "us"` is 1.1× on "all token pricing categories".

**Sources:**
- https://cloud.google.com/vertex-ai/generative-ai/pricing (accessed)
- https://platform.claude.com/docs/en/about-claude/pricing (accessed)
- AWS Price List v20260922164418
- Bedrock Opus 5.5 model card (accessed)

**Token Bill:** a cross-channel rate diff job that flags any (model, token_type, tier) where the channel in use costs more than an eligible alternative, with the data-residency constraint respected. The Vertex batch figure may be a documentation lag, so re-verify it on the bill.

**Evidence:** moderate.

### F23. Capped-usage subscriptions behave like insurance: heavy-tailed per-user demand needs portfolio and reserve modeling (`cc-seat-insurance-model`)

**Facts:**
- Gomes (arXiv:2605.16699, 2026-05-15) argues that capped-usage LLM subscriptions such as Claude Code share insurance's structure: "a fixed premium decoupled from realized consumption, stochastic per-user demand with heavy-tailed severity, a non-fungible cap that resets on a fixed schedule". The paper proposes frequency-severity decomposition and "Monte Carlo reserve adequacy".
- Anthropic's fleet figures ($13 average per active day, 90% under $30) confirm the skew.

**Sources:**
- https://arxiv.org/abs/2605.16699 (2026-05-15)
- https://code.claude.com/docs/en/costs (accessed)

**Token Bill:** in the seat optimizer, model per-user demand as frequency (active days) × severity ($ per active day), fitted per user with shrinkage to team priors. Evaluate pooled instruments (Copilot pool, Cursor Enterprise) at the portfolio level.

**Evidence:** moderate (a preprint).

---

## 4. Seat vs usage: per-developer cost functions (for the optimizer)

Let U be a developer's list-equivalent monthly usage in $ at API list prices, d_c the [CONTRACT] discount on channel c, and A_t the seat allowance of tier t in list-$. A_t is not published and must be estimated from throttling events.

| Option | Monthly cost function | TTL default | Notes |
|---|---|---|---|
| Claude Team Standard | 20 (annual) + (U − A_std)⁺ via usage credits | 1h within allowance; 5m on credits | Usage credits prepaid, API rates |
| Claude Team Premium | 100 (annual) + (U − A_prem)⁺ | same | Quesma: move to Enterprise past about 150 seats |
| Claude Enterprise (usage-based) | 20 + (1 − d_ent)·U | [open question] | `amount` vs `list_amount` visible |
| Console API key | (1 − d_api)·U | 5m (set 1h) | Claude Code Analytics per user |
| Claude Platform on AWS / Foundry | (1 − d_ccu)·U | 5m | Retires AWS commitments or MACC |
| Copilot Business / Enterprise | 19 or 39 minus pooled credits used + overage at $0.01 per credit (list API rates) | vendor-managed | Completions unlimited |
| Cursor Teams Std / Premium | 40 or 120 + (U_3p − A)⁺ + 0.25 × tokens_3p/1e6 | vendor-managed | Token rate applies even with BYOK |

---

## 5. Timing calendar (next 120 days)

| Date | Event | Lever |
|---|---|---|
| "coming weeks" after 2026-09-22 | Sonnet 5.5 / Haiku 5.5 launch | Do not lock Sonnet-tier commitments until pricing is known |
| ≥ 2026-09-29 | Sonnet 4.5 earliest 1P retirement | Migrate. Check Bedrock/Vertex schedules and Reserved bindings |
| ≥ 2026-10-15 | Haiku 4.5 earliest 1P retirement | same |
| ≥ 2026-11-21 | GPT-5.6 Sol promo floor | Re-price routing |
| ≥ 2026-11-24 | Opus 4.5 earliest 1P retirement | Bedrock Reserved Opus 4.5 exposure |
| 2026-12-31 | Vertex Gemini Flash Provisioned Throughput 50% credit ends; Gemini 3.8/3.7/3.6 Flash promo ends | Budget ×2 on 2027-01-01 |
| monthly | Vertex credits expire 30 days after issuance | Use-it-or-lose-it alert |

---

## 6. What Token Bill should build

### 6.1 Commitment-sizing optimizer (spec)

**Inputs:**
1. **Demand:** burndown-adjusted demand per enforcement window (1-minute default) per model, channel, region and deployment type, for the trailing 90 days. Sources:
   - Anthropic `usage_report` with 1m buckets
   - Bedrock CloudWatch by `ServiceTier` and `ResolvedServiceTier`
   - Vertex `token_count` by `request_type`
   - Azure deployment utilization
   - Token Bill traces
2. **Forecast** with P10, P50 and P90. It is driver-based: seats × adoption × tokens per active developer, cross-checked against Anthropic's $150–250/dev/month benchmark.
3. **Price path:** current list prices from the price feeds, plus a price-cut hazard. The hazard combines the vendor's cadence (two Anthropic cuts in 21 days), announced launches, promo end dates and deprecation floors.
4. **Instrument menu** (§2 catalog): unit, term, minimum, increment, price per unit-hour, burndown map, overflow rule, model binding and switchability, cancellation and renewal rules, and drawdown eligibility.
5. **Contracts [CONTRACT]:** discount schedule by model, token type and channel; commitment tiers; true-up rules; overflow pricing; EDP/PPA/MACC drawdown rules.
6. **SLOs per workload class:** the latency or availability that requires dedicated capacity, and the maximum spillover rate.
7. **Risk appetite:** a CVaR level α, and a maximum stranded $ allowed.

**Objective:** minimize E[TotalCost] + λ·CVaR_α(TotalCost) over horizon H. TotalCost is the sum of:
- reservation fees
- overflow tokens × overflow rate (including the Azure hourly excess)
- (commitment − eligible spend)⁺ shortfall true-ups
- minus credits
- plus migration costs

**Constraints:**
- Minimums and increments (Bedrock 100K/10K TPM; Vertex GSU minimums; Azure 15 PTU with increments of 5).
- SLO coverage: reserved capacity ≥ the demand quantile the SLO requires.
- **Model binding:** capacity on model m serves only m. The term must not extend past m's deprecation floor or a known successor date unless the instrument allows a switch (Vertex: same publisher, via account rep).
- Change windows: Vertex decreases only at renewal and nothing inside 5 days of expiry; Azure refund headroom ≤ $50K per 12 months.
- Budget and concentration limits per vendor.
- Data-residency constraints on the channel and endpoint choice.

**Method:**
1. Closed-form newsvendor per model: reserve at the demand level where P(D > C) = r (F5), then round to increments and minimums.
2. Replay simulator for spillover granularity: Vertex whole-request spill and dynamic windows; Bedrock `max_tokens` start-of-request deduction.
3. A stochastic program over demand × price-cut scenarios to pick commitment tiers.
4. An online ladder (break-even rule, after Wang et al. 2013) when forecast error exceeds a threshold.

**Outputs:**
- A per-model portfolio: {instrument, units, term, auto-renew off/on, notice date}.
- Expected saving versus pay-as-you-go with P10 and P90, u\* against expected utilization, P(shortfall), expected stranded $ under a 20% or 40% price-cut scenario.
- "Do not buy" verdicts, for example Vertex Sonnet 5 GSU with u\* > 100%.
- Every number tagged [LIST], [CONTRACT] or [DERIVED].

### 6.2 Seat-assignment optimizer (spec)

**Inputs per developer:**
- 8–12 weeks of list-equivalent daily usage by agent: Claude Code OTel and transcripts, Enterprise Analytics `user_cost_report`, Copilot `ai_credits_used`, Cursor usage events.
- Allowance-hit events: "You've hit your session limit / weekly limit", usage-credit draws.
- Feature use: completions and next-edit suggestions (Copilot); Max mode (Cursor).
- Session idle-gap distribution, for TTL replay.
- Current plan and seat term.

**Plan menu** (§4) and **contract discounts [CONTRACT]**.

**Org constraints:**
- One claude.ai plan type per org (Team or Enterprise).
- Contracted seat minimums and annual terms.
- Compliance needs (SSO/SCIM, ZDR, BAA) per channel.
- Removed seats are billed to the end of the cycle.

**Objective:** minimize Σᵢ E[costᵢ(planᵢ)] + κ·E[blocked-hoursᵢ] + switching costs. Here κ is the $ value of a developer-hour blocked by an allowance, set by the customer.

**Method:**
- Estimate A_t (allowance in list-$) from censored weekly usage at cap, using Kaplan–Meier or Tobit on the weeks that hit limits.
- Model per-user demand as frequency × severity with team-level shrinkage (F23).
- Evaluate pooled instruments at pool level.
- Re-price TTL-sensitive paths via replay (F17).
- Solve a small integer program: greedy per user, then an org constraint repair.
- Apply hysteresis: act only if the saving exceeds a threshold for 2 consecutive cycles.

**Outputs:**
- Per-user recommendation with $ delta and confidence.
- Tier counts.
- Usage-credit caps per RBAC group.
- The managed settings JSON (`promptCacheTtl`, `modelPricing`).
- A "Team vs Enterprise at N seats" crossover chart.

### 6.3 Detectors (specs)

| ID | Detector | Trigger / formula | Data | Output |
|---|---|---|---|---|
| D1 | **Underutilized reservation** | util = consumed burndown units ÷ (capacity × minutes) over 7/30 days. Flag if util < u\*. Excess $ = fee − pay-as-you-go value of the consumed tokens | Vertex `consumed_token_throughput`/`dedicated_token_limit` (Claude: `token_count` × burndown); Bedrock `ResolvedServiceTier=reserved` tokens vs reserved TPM (input and output separately); Azure reservation Utilization %; Anthropic priority headers or `service_tier=priority` usage | Downsize at the next change window, with the notice date |
| D2 | **Overflow at list / hourly** | overflow tokens (Bedrock resolved=default while reserved exists; Vertex `request_type=spillover`; Azure spillover deployment or PTU above reservation; Anthropic standard with priority headers present) × overflow rate. Flag if the overflow level is exceeded more than r of the time (should upsize) or if Azure hourly excess > 0 (2.8×) | same | Upsize, cap, or re-route to Flex or Batch |
| D3 | **Seat-tier mismatch** | Premium seat with U < estimated A_std for 2 cycles → downgrade (saves $80–100/mo). Standard seat with usage credits > $80/mo for 2 cycles → upgrade. Zero-activity seats for 30 days → remove. Cursor Std with on-demand > $80 → Premium. Copilot pool utilization < threshold → trim non-completion seats | Enterprise Analytics, spend CSV, OTel, Copilot metrics, Cursor API | Per-user action with $ |
| D4 | **Stranded-commitment risk** | For each commitment: P(eligible spend < commitment) under the price-cut scenarios; model-binding loss = Σ remaining term × (bound rate − successor rate) × tokens; term end past a deprecation floor or successor launch | Contract ledger, price calendar, forecasts | Risk $ and mitigation (split order, migrate, don't renew) |
| D5 | **Uncredited marketplace spend** | For each marketplace charge (CCU on AWS or Azure, Bedrock `MP:` usage types): (a) billed to an account or subscription outside the commitment agreement; (b) bought by credit card or Prepayment (Azure); (c) usage before private-offer acceptance; (d) linked account not on the private offer; (e) Foundry deployment on legacy per-model billing; (f) realized discount < contracted − tolerance. Loss $ = list usage × (d_contract − d_realized) + non-retired commitment $ | CUR 2.0, Azure Cost Management export, Console cost, contract ledger | Fix list per account or subscription |
| D6 | **Capacity bound to a superseded model** | reservation or Priority model m with successor m′ cheaper at list or per task | F2 table | Migration plan |
| D7 | **Renewal / notice trap** | auto-renew on and utilization < u\*, or notice deadline < 30 days | Contract ledger | Alert 30, 14 and 5 days out |
| D8 | **Promo cliff** | projected spend at post-promo rates − current rates ≥ threshold | Price calendar | Budget delta and alternatives |
| D9 | **Channel fee** | Cursor `cursorTokenFee` ÷ `totalCents` by user and model; cached-token share | Cursor API | Re-route cache-heavy work or use first-party models |
| D10 | **Legacy-model premium** | tokens on models retired on 1P but billed on partner clouds (Opus 4.1 / 4 at $15/$75) | CUR, Vertex billing | Migrate to Opus 5.5 ($4/$20) |
| D11 | **Spend cap at list** | Claude Platform on AWS caps are computed at list, so a cap in invoice $ = cap × (1 − d) | Console settings, contract | Adjust caps |
| D12 | **Cross-channel price divergence** | same (model, token_type, tier) cheaper on another eligible channel | Price feeds | Route advice |

### 6.4 Data model and connectors

- **Contract ledger** (YAML or JSON, versioned): instruments, terms, discounts [CONTRACT], burndown maps, notice dates, drawdown rules.
- **Rate-card service:** pulls the AWS Price List API, the Azure Retail Prices API, Anthropic, Vertex and Gemini pricing pages and the model-deprecation tables. Every row stores `source_url` and `fetched_at`.
- **FOCUS-aligned fact table:** ListCost, EffectiveCost and BilledCost per (time, user, channel, model, token_type, resolved_tier), plus capacity-consumed units.
- **Connectors:**
  - Anthropic: Admin usage and cost, Enterprise Analytics, Claude Code Analytics
  - AWS: CUR 2.0, CloudWatch, Price List
  - GCP: Billing export, Cloud Monitoring
  - Azure: Cost Management exports, reservations, Retail API
  - OpenAI usage and costs
  - GitHub Copilot metrics and billing
  - Cursor Admin API

---

## 7. Open questions

1. How are **cache reads** billed on Bedrock **Reserved** requests? They don't consume reserved TPM. Are they billed at the on-demand cache-read rate? When will Reserved support Opus 5.5, Sonnet 5 and Fable 5.1?
2. Does the published Vertex **GSU price table apply to Claude** orders? Will Google re-rate the Sonnet 5 GSU (350 tok/s) and add Opus 5.5?
3. What are the **AWS EDP/PPA** rules for Marketplace spend (Claude on Bedrock `MP:` usage, Claude Platform on AWS, Claude Enterprise via AWS Marketplace): cap percentages and eligibility? Do Google Cloud commitments draw down Claude-on-Vertex spend?
4. What are the current **OpenAI Scale Tier** unit prices, term and minimum? The pages were not accessible.
5. What is each Claude seat tier's **allowance in list-$ terms**? Must it be estimated empirically? What TTL does Claude Code request on **usage-based Enterprise**: subscription (1h) or credits (5m)?
6. What are the **Anthropic enterprise commitment** structures: true-up rules, overflow pricing, whether Priority-style capacity is offered under a new name ("contact sales" for guaranteed capacity)?
7. Is Vertex's Opus 5.5 **batch** price of $2.50/$12.50 intended, or a documentation lag against the 1P $2/$10?
8. Does the Azure 2027-02-01 exchange-policy change cover **PTU reservations**?

---

## 8. Sources (all opened 2026-09-23 unless dated)

**Anthropic:**
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/api/service-tiers
- https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-cost-report
- https://platform.claude.com/docs/en/api/admin/analytics
- https://platform.claude.com/docs/en/manage-claude/analytics-api
- https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws
- https://platform.claude.com/docs/en/about-claude/model-deprecations
- https://www.anthropic.com/news/claude-opus-5-5 (2026-09-22)
- https://www.anthropic.com/claude-fable-and-mythos-5-1 (2026-09-01)
- https://claude.com/pricing
- https://code.claude.com/docs/en/costs
- https://code.claude.com/docs/en/prompt-caching
- https://code.claude.com/docs/en/settings-reference
- https://support.claude.com/en/articles/11845131 (updated 2026-09-22)
- https://support.claude.com/en/articles/12429409 (2026-08-10)
- https://support.claude.com/en/articles/14782391 (updated this week)
- Bundled `claude-api/shared/{admin-api,cost-optimization,claude-platform-on-aws,models}.md` (local)

**AWS:**
- https://docs.aws.amazon.com/bedrock/latest/userguide/service-tiers-inference.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/prov-throughput.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/prov-thru-supported.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/prov-thru-delete.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/prov-thru-purchase.html
- https://docs.aws.amazon.com/bedrock/latest/APIReference/API_CreateProvisionedModelThroughput.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html and model-card-anthropic-claude-{opus-5-5,sonnet-5,fable-5-1,sonnet-4-6,opus-4-6}.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-faq.html
- https://aws.amazon.com/bedrock/pricing/
- https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/current/us-east-1/index.json (v20260922164418)
- https://docs.aws.amazon.com/marketplace/latest/buyerguide/buyer-private-offers.html
- https://docs.aws.amazon.com/marketplace/latest/userguide/private-offers-overview.html

**Google:**
- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/provisioned-throughput/purchase-provisioned-throughput (2026-09-22)
- …/supported-models (2026-09-22)
- …/use-provisioned-throughput (2026-09-22)
- https://cloud.google.com/vertex-ai/generative-ai/pricing
- https://ai.google.dev/gemini-api/docs/pricing (2026-09-23)

**Microsoft:**
- https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput (2026-07-15)
- …/provisioned-throughput-billing (2026-05-22)
- …/how-to/provisioned-throughput-sizing (2026-09-11)
- https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/exchange-and-refund-azure-reservations (2026-07-22)
- https://learn.microsoft.com/en-us/marketplace/azure-consumption-commitment-benefit (2025-10-24)
- https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing (2026-08-14)
- https://learn.microsoft.com/en-us/azure/foundry/concepts/manage-costs (2026-08-27)
- https://prices.azure.com/api/retail/prices (queried 2026-09-23)

**OpenAI:**
- https://developers.openai.com/api/docs/guides/fast-mode
- https://developers.openai.com/api/docs/pricing
- https://github.com/openai/openai-python/blob/main/src/openai/types/responses/service_tier.py (2026-08-17)

**Coding agents:**
- https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/ (2026-04-27)
- https://github.blog/changelog/2026-06-19-ai-credits-consumed-per-user-now-in-the-copilot-usage-metrics-api/ (2026-06-19)
- https://docs.github.com/en/copilot/concepts/billing-and-usage/organizations-and-enterprises/seats-and-billing-cycles
- https://docs.github.com/en/billing/concepts/product-billing/github-copilot-licenses
- https://cursor.com/pricing
- https://cursor.com/docs/account/teams/pricing
- https://cursor.com/docs/account/teams/admin-api

**Analysis and research:**
- https://quesma.com/blog/claude-code-pricing-for-enterprise/ (2026-08-11)
- https://www.finops.org/wg/effect-of-optimization-on-ai-forecasting/ (2026-03-17)
- https://epoch.ai/data-insights/llm-inference-price-trends (2025-03-12)
- https://arxiv.org/abs/1305.5608 (2013; ICAC 2013)
- https://arxiv.org/abs/2502.08022 (2025-02-11)
- https://arxiv.org/abs/2005.12249 (2020; HotCloud 2020)
- https://arxiv.org/abs/2605.16699 (2026-05-15)
