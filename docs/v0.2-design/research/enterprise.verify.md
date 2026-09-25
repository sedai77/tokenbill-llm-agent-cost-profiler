# Fact-check: enterprise.md (research track "enterprise")

Checked on 2026-09-23. I opened every cited source with WebFetch, or with curl for raw Markdown, the GitHub API, the arXiv API or the PyPI Integrity API. Local copies of the pages I fetched are in `research/vent/`. For codebase claims I re-read the Token Bill clone, which I did not modify. I also re-ran the storage arithmetic and generated the demo report into `research/vent/demo_report.html`.

**Result: 19 confirmed, 7 corrected, 0 unverifiable, 0 refuted.**

| id | verdict |
|---|---|
| ent-reconciliation | corrected |
| ent-cache-diagnostics-privacy | confirmed |
| ent-claude-code-otel | confirmed |
| ent-anthropic-admin-apis | confirmed |
| ent-usage-normalization | confirmed |
| ent-otel-genai-semconv | corrected (minor) |
| ent-focus-1-4 | corrected (minor) |
| ent-privacy-by-default | confirmed |
| ent-secrets-in-traces | confirmed |
| ent-redaction-limits | confirmed |
| ent-local-transcripts | confirmed |
| ent-cloud-gateway-attribution | confirmed |
| ent-finops-kpis | confirmed |
| ent-allocation-showback | confirmed |
| ent-anomaly-detection | corrected |
| ent-forecast-budgets | corrected |
| ent-supply-chain-incidents | confirmed |
| ent-provenance-sbom | confirmed |
| ent-scorecard-osps | corrected |
| ent-scale-storage | confirmed |
| ent-ci-cost-gate | confirmed |
| ent-deployment-modes | corrected (minor) |
| ent-server-sso-rbac | confirmed |
| ent-report-a11y-hardening | confirmed |
| ent-eu-cra | confirmed |
| ent-market-tokenomics | confirmed |

---

## Notes per finding

### ent-reconciliation: CORRECTED

**Confirmed:**
- prompt-caching docs: 5-minute cache writes are "1.25 times" the base input price and 1-hour writes "2 times". The page also says "These multipliers stack with other pricing modifiers such as the Batch API discount and data residency."
- usage-cost-api: "Priority Tier costs … are not included in the cost endpoint."
- analytics-api:
  - Cost and usage values "can be revised for up to 30 days".
  - "Amount fields are decimal strings in cents … Avoid binary floating-point parsing for values that may exceed several million dollars."
- costs page:
  - The `/usage` figure is computed "at list price, unless a `modelPricing` table is in effect".
  - "Before v2.1.239, Claude Code didn't apply the 1.1× … so the session cost figure was lower than the bill."
- settings-reference: `modelPricing` is Managed scope, with `multiplier` and `overrides`, and requires v2.1.242 or later. Its `cacheWrite` field "covers both five-minute and one-hour cache writes".
- Repo, `tokenbill/pricing.py`: rates are `float`, there is a single `cache_write_multiplier=1.25` ("5-minute TTL writes"), and there is no Decimal, 1h, batch or residency handling.

**Corrections:**
1. Claude Code does not "bill at a 1.1× data-residency rate". The API bills data-residency (US-only inference) responses at 1.1×. Since v2.1.239, Claude Code's local cost estimate multiplies those responses' list-price cost by 1.1.
2. Token Bill is not strictly list-price-only. `tokenbill analyze --model-price MODEL=IN,OUT` (`cli.py`) overrides input and output $/MTok per model. Cache multipliers stay fixed at 1.25×/0.1×, and there is no 1h, batch, residency, priority or Decimal support. The conclusion still holds: it would not reconcile to contracted invoices.

Side observation: the PRICING table has no row for `claude-opus-5-5`. The docs list Opus 5.5 with a 0.05× cache read, so Token Bill would report tokens without dollars for that model.

### ent-cache-diagnostics-privacy: CONFIRMED

The cache-diagnostics page metadata gives beta header `cache-diagnosis-2026-04-07`. ZDR is "eligible" (qualified: it excludes Covered Models). The feature is available on the Claude API only; Claude Platform on AWS, Bedrock, Google Cloud and Foundry are "not available".

- The request uses `diagnostics.previous_message_id`. The response gives `cache_miss_reason.type`, one of `model_changed`, `system_changed`, `tools_changed`, `messages_changed`, `previous_message_not_found` or `unavailable`.
- The four `*_changed` types carry `cache_missed_input_tokens`, which is "a magnitude indicator rather than a billing number".
- "Fingerprints contain only hashes and token-count estimates (never raw prompt content)."
- "Send the beta header on every turn."
- The bundled `prompt-caching.md` agrees: "send the header on **every** request … a one-shot retrofit fails with `previous_message_not_found`".

### ent-claude-code-otel: CONFIRMED

Source: `monitoring-usage.md`.

- `CLAUDE_CODE_ENABLE_TELEMETRY=1` is required.
- Metrics include `claude_code.token.usage` (type input/output/cacheRead/cacheCreation) and `claude_code.cost.usage`.
- Attributes include `model`, `query_source` (main/subagent/auxiliary), `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name` and `mcp_tool.name`. User-defined names are redacted to "custom" or "third-party".
- The `claude_code.api_request` event carries `cost_usd`, `cost_usd_micros`, `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` and `request_id`.
- The page gives the example `OTEL_RESOURCE_ATTRIBUTES="department=engineering,team.id=platform,cost_center=eng-123"`.
- `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` and `OTEL_LOG_RAW_API_BODIES` all default to disabled.
- The section "How managed settings lock the OTLP destination" applies from v2.1.217.

The costs page confirms the `/usage` "Prompt cache (main)" line:
- A miss is counted at more than 5% and at least 2,000 tokens.
- "likely cause" text requires v2.1.260.
- It covers "the main conversation only, not subagents", computed on this machine.

### ent-anthropic-admin-apis: CONFIRMED

usage-cost-api:
- Bucket limits: 1m up to 1,440; 1h up to 168; 1d up to 31.
- Filter and group by API key, workspace, model, service tier, context window, data residency (`inference_geo`) and speed (beta).
- "typically appears within 5 minutes". Polling once per minute is supported.
- The Cost API is daily only, "decimal strings in lowest units (cents)", and excludes Priority Tier.

claude-code-analytics-api:
- "up to 1-hour delay"; limit up to 1000; free.
- `estimated_cost.amount` is in cents.
- Excludes Bedrock, Foundry, Google Cloud and Claude Platform on AWS.

analytics-api:
- "typically available within four hours … may take up to 24 hours"; revised for up to 30 days.
- 60 requests per minute, org-wide.
- Key scope is `read:analytics`.

### ent-usage-normalization: CONFIRMED

- OTel `anthropic.md` note [27]: "Anthropic `input_tokens` excludes cached tokens. Compute: gen_ai.usage.input_tokens = input_tokens + cache_read_input_tokens + cache_write_input_tokens".
- OpenInference: `prompt_details.*` "are sub-counts of `llm.token_count.prompt` … already included in it", and providers should fold cache read/write back into prompt.
- Langfuse: "each token must be counted in exactly one key".
- OpenAI:
  - "For GPT-5.6 and later, cache writes cost 1.25×".
  - "Cache read charge: 0.1×".
  - Fields are `usage.input_tokens_details.cached_tokens` and `cache_write_tokens`.
  - The 0.1× read rate is stated for GPT-5.6+. Earlier models are "model-dependent".
- Claude Code uses `cacheRead`/`cacheCreation`.

### ent-otel-genai-semconv: CORRECTED (minor)

**Confirmed:**
- GitHub releases: v1.40.0 was published 2026-02-19 and v1.42.0 on 2026-06-12.
- The v1.40.0 changelog adds `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_creation.input_tokens`.
- The v1.42.0 changelog says: "Move Generative AI semantic conventions to a dedicated repository".
- The new repo was created 2026-05-05. Its documents have status Development.
- `gen_ai.usage.cache_write.input_tokens` exists.
- `gen_ai.client.inference.usage.*` are Counters with required `gen_ai.token.modality`, which has an `unknown` fallback.
- Histograms "should not be used for total usage or cost calculations. Use histograms exclusively to monitor usage percentiles".
- `gen_ai.input.messages`, `gen_ai.system_instructions` and `gen_ai.tool.definitions` are Opt-In.
- The opentelemetry.io registry (1.44.0) shows "GenAI Attributes (Moved)".

**Correction:** "schema_url is TODO" is only half right. The new repo's README section "Schema URL" reads "TODO". However, `model/manifest.yaml` already declares `schema_url: https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev` with `stability: development`.

### ent-focus-1-4: CORRECTED (minor)

**Confirmed:**
- The FinOps article is dated 2026-06-10, by Shawn Alpay and Matt Cowsert. It says "ratified … on June 4, 2026" and "2 datasets, 47 columns, 6 attributes, 17 glossary entries". It says 1.5 will add "AI model identity and token consumption (input and output)" plus a Price Sheet dataset.
- The v1.4 tag is dated 2026-06-04 and v1.3 is dated 2025-12-08.
- `servicecategory.md` at v1.4 contains "AI and Machine Learning".
- `custom_column_handling.md` says columns "MUST include the `x_` prefix", with a SHOULD of 50 characters or fewer.
- The allocated* columns are present at v1.3 and absent at v1.2.
- SiliconANGLE (2026-06-08) carries the cardinality quote.

**Corrections to the #2018 details:**
- The issue was closed as completed on 2026-07-27, so it was last updated then, not 2026-07-23 (the last status comment is 2026-07-23).
- Per the TF2 decisions (2026-06-03 to 07-01), token consumption reuses the existing PricingUnit, ConsumedQuantity and ConsumedUnit with no new columns. Model identity is added as FOCUS-defined properties in SkuPriceDetails (ModelDeveloper, ModelFamily, ModelId, ModelVersion; PR #2442). The report says model identity also goes into ConsumedQuantity/ConsumedUnit, which is wrong.
- Cached versus uncached tokens were explicitly carved out of #2018 into FR #2099.
- "Service-specific opt-in" belongs to PR #2473 (dataset configuration), not #2018 itself.

### ent-privacy-by-default: CONFIRMED

EDPB PDF (edpb_guidelines_202501_pseudonymisation_en.pdf):
- "Adopted on 16 January 2025", marked "version for public consultation".
- Contains "is to be considered information on an identifiable natural person … and is therefore" personal data.
- Contains "information that the pseudonymising controller keeps secret".
- Lists "Message Authentication Codes (MACs)".
- Says "the secrets should have sufficient entropy".

Gateway spend-limits page:
- `spend` retention default 13 months, `admin_audit` 365 days, `principal_emails` 90 days.
- DSAR delete via SQL.
- Warns that `q=`/`user_ids[]` values end up in proxy logs.

The gateway page says it "doesn't log or store prompt or completion content". Monitoring-usage confirms that content capture is off by default.

### ent-secrets-in-traces: CONFIRMED

GitGuardian (2026-03-17):
- 28.65M secrets (+34%).
- AI service secrets 1,275,105 (+81%).
- Claude Code-assisted commits 3.2% versus 1.5%.
- 24,008 unique MCP-config secrets, of which 2,117 are valid.
- Internal repos about 6× more likely to contain secrets.
- About 28% of incidents originate outside repositories.

The `.claude` directory doc says: "not encrypted at rest … If a tool reads a `.env` file or a command prints a credential, that value is written to projects/<project>/<session>.jsonl". OWASP LLM02:2025 is "Sensitive Information Disclosure".

### ent-redaction-limits: CONFIRMED

- arXiv 2604.12064 (submitted 2026-04-13), by Justice Owusu Agyemang et al.: 8 techniques, 1,300 samples, 4,014 annotations. It reports "0.6% combined leak on PII and 31.3% on proprietary code" and "no single technique dominates".
- Presidio README: "there is no guarantee that Presidio will find all sensitive information".

### ent-local-transcripts: CONFIRMED

The `.claude` directory doc confirms the path, "every message, tool call, and tool result", "not encrypted at rest", and `cleanupPeriodDays` defaulting to 30.

ccusage:
- The homepage lists 18 CLIs and says it "reads local usage logs … without uploading".
- The cost-modes page says it "falls back to pricing cache_creation_input_tokens at the standard cache creation rate" when the 5m/1h split is missing.
- The 18-CLI count comes from ccusage.com, not from the cost-modes page.

### ent-cloud-gateway-attribution: CONFIRMED

AWS blog (2026-04-17):
- "automatically attributes inference costs to the IAM principal".
- CUR 2.0 `line_item_iam_principal` and `line_item_usage_type`.
- `iamPrincipal/` tag prefix.
- "Claude Code workloads → Scenario 3" (federated authentication).

Gateway spend limits:
- Caps can be per user, per rbac_group or per organization, and daily, weekly or monthly.
- Over-cap requests get 429 `billing_error`.
- Claude Code warns past 75% and 95%.
- Unknown models are metered at $5/$25 per MTok.
- Enforcement "fails open by default".

LiteLLM: SpendLogs, `max_budget`/`budget_duration`, and `litellm_zero_cost_requests_total`.

Caveat on the report body only: the AWS post does not mention application inference profiles or cost-allocation tags for InvokeModel/Converse.

### ent-finops-kpis: CONFIRMED

- FinOps for AI Overview (2026-02-17): Cost per Inference, per Token, per API Call, ROI and Time to Business Value. It recommends a showback model.
- Tokenomics paper (2026-06-03):
  - "cost per successful outcome".
  - Levers: 60–90%, 50%, 50–90% on cached tokens, 20–60%, 10–40% and 10–30%.
  - The "Run" stage includes cost estimation in CI/CD.

### ent-allocation-showback: CONFIRMED

- Allocation capability KPIs are the three percentage KPIs. Shared costs are split by fixed, proportional, proxy-metric or even split.
- FinOps for AI tag keys: Project, Environment, Workload, Team, CostCenter, UsageType, Purpose and Criticality.
- The Tokenomics paper names API key governance as the minimum viable control and puts harness costs at "40 to 60%" of feature spend.

### ent-anomaly-detection: CORRECTED

**Confirmed:**
- FinOps anomaly KPI: "Total Cost of Anomaly Spikes / Total AI Spend", with <2% green, 2–7% yellow and >7% red. MTTD is listed.
- AWS Cost Anomaly Detection: "approximately three times a day", "up to 24 hours to detect", "10 days of historical service usage data".
- OWASP LLM10 contains the literal "Denial of Wallet (DoW)".
- arXiv 2604.22750 (Bai et al., 2026-04-24): "up to 30x" variation and correlation "up to 0.39".
- NIST EWMA: λ is usually 0.2–0.3, with k=3.

**Correction (RecurGuard, arXiv 2606.07968, 2026-06-06):**
- The 99% (OverThink) and 92% (ExtendAttack) detection rates were measured on a single model, DS-R1-Qwen-7B.
- "22.8×" is not a general amplification figure for reasoning-token attacks. It appears in the adaptive stress test ("full semantic evasion reduces amplification from 22.8x to 2.2x"). In the same test, topical attacks keep 11.9× with about a 50% joint miss rate.

**Caveat on the report body (executive summary):** it says AWS "cannot say why spend moved". That is overstated: the AWS docs say you "can investigate the root causes of the anomaly, ranked by their dollar impact" across service, account, Region and usage type. What AWS lacks is prompt- or cache-level cause.

### ent-forecast-budgets: CORRECTED

**Confirmed:**
- FinOps Forecasting defines the Accuracy Rate and Drift Rate formulas. It describes Crawl as historical, Walk as rolling and trend-based, and Run as adding driver-based methods. It says "Each organization defines its acceptable variance".
- Anthropic's costs page: "$13 per developer per active day and $150-250 per developer per month … below $30 per active day for 90% of users".
- Caps exist at the workspace level (Console), the org, group and individual level (Teams/Enterprise), and in the gateway.

**Correction:** the "Effect of Optimization on AI Forecasting" paper (last updated March 17, 2026) does not contain the "40–60% compounding" figure. I scanned every percentage on the page.
- Its figures include:
  - a 99% token reduction from hashing and differential (User Story #2)
  - "Up to 85% cost reduction" for RouteLLM
  - 10–30%, 15–40%, 20–50%, 25–60% and 15–35% savings attributed to other tools
  - 14–20% from ARM migration
- The 85% is a vendor tool's claimed "up to" figure, not a measured result.
- Drop the 40–60% compounding claim, or find its real source. The Tokenomics paper's "40 to 60%" is about harness cost share, not compounding savings.

### ent-supply-chain-incidents: CONFIRMED

- LiteLLM blog:
  - Incident on 2026-03-24 affecting 1.82.7 and 1.82.8, a credential stealer.
  - "originated from the Trivy dependency used in our CI/CD security scanning workflow".
  - `litellm_init.pth`.
  - CI/CD v2 and cosign-signed images from v1.83.0-nightly.
- Snyk: "exfiltrated the `PYPI_PUBLISH` token"; the .pth file runs "on every Python interpreter startup".
- CISA alert dated 2025-03-18, revised 2025-03-26.
- GitHub (2025-08-15): SHA-pinning enforcement plus the `!` blocklist.
- PyPI: "expire no more than 15 minutes"; recommends required reviewers and tag protection.
- PyPI Integrity API for tokenbill-0.1.2 wheel: publisher is `{repository: sedai77/tokenbill-llm-agent-cost-profiler, workflow: release.yml, environment: pypi}` with 1 attestation.
- Repo: `ci.yml` has no `permissions:`. All actions are tag-pinned (`@v4`, `@v5`, `@release/v1`).

### ent-provenance-sbom: CONFIRMED

- gh-action-pypi-publish v1.11.0 (published 2024-10-30): attestations "on by default".
- PyPI blog (2024-11-14): "more than 20,000 attestations".
- GitHub docs: "provides SLSA v1.0 Build Level 2", and reusable workflows reach L3.
- SLSA v1.2 was announced 2025-11-24 and adds a Source Track; it is backward compatible with v1.1.
- Immutable releases went GA on 2025-10-28.
- PEP 770 is Final, resolved 2025-04-11, and reserves `.dist-info/sboms`.
- uv's CycloneDX export is "in preview".
- Hatch is reproducible by default via SOURCE_DATE_EPOCH. Token Bill uses hatchling.
- Scorecard Signed-Releases looks for `*.sig`, `*.sigstore` and `*.intoto.jsonl`, and there is a separate SBOM check.

### ent-scorecard-osps: CORRECTED

**Confirmed:**
- Scorecard risk levels: Dangerous-Workflow Critical; Token-Permissions, Signed-Releases, Dependency-Update-Tool, Maintained and Code-Review High; Pinned-Dependencies, SAST, SBOM and Fuzzing Medium.
- OSPS L2 is "for any code project that has at least 2 maintainers and a small number of consistent users".
- The L2 examples are real L2 controls: AC-04.01, BR-06.01, GV-01.01, LE-01.01, QA-03.01, SA-03.01 and VM-01.01.
- api.securityscorecards.dev returns 404 for the repo.
- GitHub contributors: only sedai77. The repo was created 2026-07-27 and has 1 star.

**Correction:** the control counts are wrong. The 2026-02-19 Baseline overview lists 24 controls at Level 1, 19 new at Level 2 and 21 new at Level 3, which is 64 in total. Counted cumulatively, that is 24 / 43 / 64. It is not 20 / 16 / 24.

### ent-scale-storage: CONFIRMED

- The recorder (`instrument.py`) writes full `system`, `tools` and `messages` per call.
- `read_trace()` does `p.read_text()` and returns `list[Run]`, so everything sits in memory.
- Re-running the arithmetic (200 calls, linear growth to 150k tokens, 3.7 characters per token) gives 55.8 MB full-content versus 0.555 MB content-addressed, a ratio of 100.5.
- SQLite: "fewer than 100K hits/day should work fine … demonstrated … 10 times that", with a 281 TB limit. The finding's "100K+" slightly overstates SQLite's conservative wording.
- DuckDB supports glob, auto-detect and gzip/zstd.
- The OTLP file exporter spec has status Development and is JSON lines with one TracesData, MetricsData or LogsData per line.
- The collector fileexporter is alpha for traces, metrics and logs, and supports rotation, zstd and group_by.

### ent-ci-cost-gate: CONFIRMED

- The bundled `cost-optimization.md` (§2.1) says: "Verify from usage, not from code review - and re-verify after every prompt-assembly change: on a warmed-up loop, cache_read_input_tokens should dominate".
- Cache diagnostics compares consecutive requests turn by turn.
- The Tokenomics Run stage includes CI/CD.
- The repo has `tests/test_demo_recovers_planted_waste.py`, the deterministic planted-waste harness.

### ent-deployment-modes: CORRECTED (minor)

**Confirmed:**
- Gateway page: "Telemetry fan-out (OTLP/HTTP) … both protobuf and JSON encodings" and "OTLP/gRPC: Not supported".
- The redactionprocessor fails closed: an empty `allowed_keys` removes all attributes. It offers a `hash_function` option such as md5.
- fileexporter supports JSON, rotation and zstd.
- LiteLLM uses cosign-signed images.

**Corrections:**
- redactionprocessor stability is alpha for logs and metrics but beta for traces, not simply "alpha".
- "Enterprises expect CLI, scheduled job, CI and collector-pipeline options" is the author's inference. None of the cited sources states it.

### ent-server-sso-rbac: CONFIRMED

- Gateway: RBAC by IdP group, and audit rows "attributed to `admin-key:<id>` or `oidc:<sub>`".
- Gateway: `enforcement.fail_closed_on_error`, and retention tables.
- RFC 7644 is dated September 2015. RFC 9700 is a BCP dated January 2025.
- The OWASP ASVS `v5.0.0_release` tag was published 2025-05-30.

### ent-report-a11y-hardening: CONFIRMED

- WCAG 2.2 is a W3C Recommendation dated 12 December 2024, with 4.5:1 (1.4.3), 3:1 (1.4.11) and 2.5.8 at AA.
- `report.py` SVGs carry `role="img"` and `aria-label`.
- The generated demo report has 0 `<table>` elements and no Content-Security-Policy.
- SECURITY.md lines 66–68 describe the terminal ANSI escape risk.

### ent-eu-cra: CONFIRMED

- EC CRA reporting page (last update 11 September 2026):
  - Manufacturers are obliged from 11 September 2026.
  - Deadlines: 24h early warning; 72h notification; final report 14 days after a corrective measure for vulnerabilities, or 1 month for severe incidents.
  - Reports go through the ENISA Single Reporting Platform.
  - Stewards are obliged from 11 December 2027 under Art. 71(2).
- The obligations cover actively exploited vulnerabilities and severe incidents. The page does not name "Article 14", but that is the CRA article on manufacturer reporting.
- PEP 770 cites SSDF and CRA.
- NIST SP 800-218 v1.1 (February 2022) is still the latest final. SSDF v1.2 (SP 800-218r1) exists only as an initial public draft dated 2025-12-17, and the `/r1/final` URL returns 404.

### ent-market-tokenomics: CONFIRMED

- LF press release (04 August 2026):
  - "30 industry leaders" and 30 initial members, including Vantage, Finout, Flexera, Cast.ai, Kion, IBM, JPMorganChase, SAP and ServiceNow.
  - Scope: "define token value/density, including input, output, reasoning, and cache", and "Token Cost Telemetry: improved AI cost reporting schemas in FOCUS v1.5 and beyond".
  - The governing board convened 2026-07-30.
- CIO Dive (2026-06-12) says it was announced 2026-06-09 at FinOps X. The report body's "intent 2026-06-03" is not supported by what I opened.
- The Anthropic partner list is CloudZero, Datadog, Grafana Cloud, Harness, Honeycomb and Vantage.
- The claim that no reviewed doc offers fleet-level causal waste attribution is a bounded reading of docs, not a market audit, and is labeled that way.
