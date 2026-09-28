# TokenBill Enterprise Pilot Guide

## Status

TokenBill v0.2.0 is a release candidate for a controlled enterprise pilot. It is
not an unattended policy-enforcement service. A pilot should begin in
observation-only mode, reconcile costs with finance, validate recommendation
quality on representative work, and enable a policy only after approval.

GitHub Copilot import and detector components are present, but v0.2.0 does not
ship the public `tokenbill copilot` workflow or a complete audited Copilot
summary and policy path. Treat Copilot as an integration preview, not as a
Copilot invoice or automated-policy product. See
[INTEGRATIONS.md](INTEGRATIONS.md) for the exact provider boundaries and
deployment recipes.

The product boundary is intentional:

- TokenBill records evidence, explains cost, models a documented alternative,
  emits a reviewable policy pack, and measures the outcome.
- TokenBill does not automatically downgrade models, rewrite prompts, delete
  context, modify developer settings, or claim that a subscription allowance
  is cash savings.

## What The Pilot Proves

A successful pilot answers these questions with source provenance:

1. What was billed, what was an allowance or credit, and what remains unknown?
2. Which request, workflow, cache, retry, model, or policy behavior drove cost?
3. Which recommended change is eligible for a particular provider and workload?
4. Did the change preserve task quality and reduce actual incremental spend?

TokenBill labels a figure as billed, priced/list-equivalent, estimated, measured,
or verified. Do not combine those labels in an executive total.

## Supported Evidence Paths

The table describes the v0.2 integration surface. "Automated coverage" means
synthetic, conformance, fuzz, or gate coverage in the repository. It is not a
promise that an organization-specific provider contract has been reconciled.

| Evidence path | Typical source | Intended use | Pilot boundary |
|---|---|---|---|
| Legacy trace | TokenBill trace@1 JSONL | Local v0.1 cache-breaker analysis | Legacy behavior remains supported; use trace@2 for fleet collection. |
| Governed trace | TokenBill trace@2 usage/fingerprint records | API and agent usage ledger | Content-free usage is the default. Full prompt collection is not accepted by the fleet ingest path. |
| Claude Code | Local transcripts, headless output, Agent SDK streams | Personal, CI, and fleet workload analysis | Treat local transcript collection as sensitive source-code metadata. |
| OpenTelemetry | OTLP GenAI and related conventions | Provider-neutral request/attempt evidence | Retain semantic-convention version and data-quality notes; providers expose different fields. |
| API and cloud billing | Anthropic Admin pages, OpenAI usage/cost exports, AWS CUR 2.0 CSV/CSV.gz, GCP billing CSV/JSONL | Reconciliation and invoice-aware reporting | Reconcile per provider and channel. Rate-card estimates do not replace invoices. |
| GitHub Copilot | Billing, metrics, seats, configuration, local/VS Code traces, OTEL, export bundles | Evidence-import and detector evaluation | Integration preview only: keep AI credits, subscription allowance, seats, and activity metrics as separate facts; do not use generic reports as Copilot invoices. |
| Policy packs | Claude Code, LiteLLM, and SDK targets | Human-reviewed rollout artifacts | Emission only. Apply through the enterprise's normal change-control process. Copilot policy wiring is not packaged in v0.2.0. |

Out of scope for this release candidate:

- automatic prompt compression or context deletion;
- automatic model routing or cascading without workload quality labels;
- universal rate assumptions across providers, regions, or contracts;
- individual spend ranking or per-developer seat assignment recommendations;
- presenting self-hosted throughput gains as third-party API-token savings.

## Pilot Roles

| Role | Responsibility |
|---|---|
| Executive sponsor | Defines the savings, quality, and developer-experience outcome. |
| FinOps owner | Supplies contract/invoice evidence and accepts reconciliation tolerances. |
| Security and privacy owner | Approves data tier, key custody, retention, identity mode, and access model. |
| Platform owner | Operates collectors, ledger storage, policy delivery, and rollback. |
| Workload owner | Defines task-quality evaluation and accepts or rejects recommendations. |
| Developer representatives | Review ergonomics, policy impact, and exception paths before broad rollout. |

## Safe Rollout Sequence

### 1. Initialize a protected pilot workspace

Create a dedicated workspace and choose the identity mode with security. The
default central mode pseudonymizes people centrally. The two-stage mode is for
organizations that require a separate collector-side identity transformation.

~~~sh
tokenbill init --dir .tokenbill-pilot --identity-mode central --k 5
~~~

Keep generated keys and the ledger outside source control. Do not put provider
credentials in config files, reports, or ticket attachments.

### 2. Collect the least sensitive useful evidence

Start with content-free trace@2 records and provider exports. Use a small
volunteer cohort or CI workload before endpoint-wide collection.

~~~sh
tokenbill collect claude-code --help
tokenbill collect claude-code-headless --help
tokenbill ingest --db .tokenbill-pilot/ledger.db --content none SOURCE...
~~~

Use the strict data-quality mode in scheduled jobs once the source mapping is
known:

~~~sh
tokenbill --strict-dq ingest --db .tokenbill-pilot/ledger.db --content none SOURCE...
~~~

Quarantine malformed records during discovery. Move to strict ingest only after
the expected schema and source coverage are documented.

### 3. Reconcile before publishing money

Load provider usage and cost evidence for the same UTC window. Reconciliation
is per channel; one unavailable cloud invoice must not silently bless another
provider's figures.

~~~sh
tokenbill reconcile --db .tokenbill-pilot/ledger.db \
  --usage-report ANTHROPIC_USAGE... \
  --cost-report ANTHROPIC_COST... \
  --openai-usage OPENAI_USAGE... \
  --openai-costs OPENAI_COSTS... \
  --aws-cur AWS_CUR... \
  --gcp-billing GCP_EXPORT...
~~~

For a live Admin API pull, use an environment variable for the key and record
the returned pages in a protected location. The command never writes the key:

~~~sh
tokenbill reconcile --db .tokenbill-pilot/ledger.db --live \
  --admin-key-env ANTHROPIC_ADMIN_API_KEY --record protected/admin-pages
~~~

Do not label a total as billed until its channel has passed the finance-agreed
reconciliation gate. Show list-equivalent allowance and estimated opportunity
beside, not inside, the invoice total.

### 4. Start with low-risk findings

Run findings over a fixed time window. Begin with observed cache, retry, and
failure-path behavior, not model changes or lossy transformations.

~~~sh
tokenbill findings --db .tokenbill-pilot/ledger.db \
  --since 2026-09-01 --until 2026-10-01 --min-usd 25
~~~

Each candidate action needs an owner, affected cohort, rate-card/billing basis,
quality metric, expected savings interval, and rollback condition.

### 5. Emit policies for review, never direct application

Policy output is a reviewable artifact. The tool does not apply it.

~~~sh
tokenbill policy --db .tokenbill-pilot/ledger.db \
  --target claude-code --cohort-by team -o proposed-policy
~~~

Do not include trade-off levers such as default-model or effort changes until
the workload owner has approved a measurement plan. Review the generated patch,
the current settings, source version, and rollback artifact through normal
change management.

### 6. Measure the rollout, then create a receipt

For a safe, approved change, register a randomized or interrupted-time-series
measurement design before rollout. Compare cost and quality together.

~~~sh
tokenbill measure plan --help
tokenbill measure run --help
tokenbill receipt create --help
~~~

Required outcome measures vary by workload. At minimum collect task success,
evaluation score or human acceptance, latency, retry/rework rate, and billed
incremental spend. A model-cost reduction with increased rework is not a
savings result.

## Evidence and Data Controls

### Privacy defaults

- Use content tier none unless fingerprint evidence is necessary for a defined
  cache or block-analysis question.
- Store opaque principal and name identifiers, not raw email addresses,
  workspace paths, repository names, prompt content, or API keys.
- Publish organization findings only at the configured k-anonymity threshold.
- Use self view only for one person's ledger. TokenBill rejects merged records
  with conflicting principal provenance.
- Establish a retention schedule and test the audited purge path before
  collecting employee data.

### Cost integrity

- Preserve raw provider evidence and source identifiers long enough to
  reproduce a bill.
- Keep effective-dated list prices, contract overlays, promotions, regions,
  and service tiers as distinct rate facts.
- Reconcile by provider, channel, currency/billing period, and known
  discount/allowance basis.
- Treat rate-card-only projections as estimates. Do not use them for a
  finance showback that claims invoice truth.

### Quality and rollout integrity

- Use shadow recommendations and canaries before enforcement.
- Put explicit ceilings on retry loops, fanout, output, and any new
  intervention's blast radius.
- Set a quality floor and a rollback threshold before enabling a model,
  compression, or caching change.
- Record the prompt, tool, model, and policy version in the evaluation plan.
- Keep a named exception route for safety, incident response, and workloads
  with atypical quality requirements.

## Decision Rules For Optimization

| Candidate | Default decision |
|---|---|
| Stable static prefix and observed cache miss | Recommend cache layout repair after provider eligibility and reuse economics are checked. |
| Batch/Flex eligible asynchronous job | Recommend only when the workload's completion deadline permits it. |
| Repeated deterministic request | Evaluate exact-result caching with tenant and authorization isolation. |
| Retrieval carries large duplicate or irrelevant context | Measure retrieval-token yield first; prefer extraction and top-k controls before lossy compression. |
| Retry or agent loop has material cost | Recommend bounded retries, backoff, idempotency, and branch/observation caps. |
| Cheaper model or lower effort setting | Shadow and measure first; do not enforce from cost evidence alone. |
| Semantic cache | Require model/prompt/tool versioning, data classification, acceptance-quality monitoring, and an escape hatch. |
| Self-hosted serving optimization | Track GPU/capacity economics separately from external provider-token savings. |

## Pilot Exit Criteria

Do not expand beyond the pilot until all criteria are met:

- Reconciliation meets the finance-approved tolerance for every reported
  channel, with unexplained residuals visible.
- Collection coverage, data-quality notes, source freshness, and retention
  behavior are measured and accepted by the platform and privacy owners.
- At least one low-risk optimization has a documented baseline, approved
  rollout, non-inferior quality result, and realized savings receipt.
- Policy changes are reviewable, reversible, and auditable.
- Full test suite, lint, zero-dependency audit, SBOM, metadata validation, and
  fixed-epoch reproducible build have passed for the release candidate.
- The enterprise has a named operational owner, security contact, and incident
  route for the service.

## Sources and Refresh Cadence

Provider mechanics change. Re-verify cache, batch, flex, billing, and metric
facts before each release and when a provider changes a model, endpoint, or
pricing tier. Useful primary sources include:

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [OpenAI cost optimization](https://developers.openai.com/api/docs/guides/cost-optimization)
- [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Gemini caching](https://ai.google.dev/gemini-api/docs/caching)
- [Amazon Bedrock prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)
- [GitHub Copilot usage metrics](https://docs.github.com/en/copilot/reference/copilot-usage-metrics/copilot-usage-metrics)
- [GitHub Copilot enterprise billing](https://docs.github.com/en/copilot/concepts/billing-and-usage/organizations-and-enterprises)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
