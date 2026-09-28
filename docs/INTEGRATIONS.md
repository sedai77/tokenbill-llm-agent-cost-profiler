# Token Bill Enterprise Integrations

This guide describes the integration surface that is actually packaged in
Token Bill v0.2.0. It is intentionally more conservative than a provider
marketing matrix: a source is not an end-to-end cost-management product until
its evidence, identity, billing, reporting, and rollout paths are all present
and tested together.

For organization roles, pilot gates, privacy defaults, and measurement
requirements, read [ENTERPRISE.md](ENTERPRISE.md) first.

## Operating Model

Token Bill is normally deployed as a restricted local or CI job:

1. The provider, cloud platform, or observability system exports approved
   evidence into a protected location.
2. A collector or scheduled job runs Token Bill with a pinned adapter and the
   least sensitive content tier that answers the question.
3. The local SQLite ledger stores normalized evidence and data-quality notes.
4. Provider billing evidence is reconciled in the same UTC window.
5. Findings, reports, exports, and policy artifacts enter the enterprise's
   existing finance and change-control process.

This design lets an enterprise choose its own credential boundaries. Supplied
files are processed locally. The only built-in live pull in v0.2.0 is the
optional Anthropic Admin API reconciliation path. It takes its key from an
environment variable and can record returned pages to a protected directory.

## Common Foundation

Create a dedicated workspace once per pilot or environment:

```bash
PILOT=.tokenbill-pilot
CFG="$PILOT/config.json"
DB="$PILOT/store/tokenbill.db"

tokenbill init --dir "$PILOT" --identity-mode central --k 5
```

Use `--identity-mode two-stage` when a local collector must transform identity
references before records cross into the central analysis environment. Keep the
generated keys, `config.json`, ledger, reports, raw exports, and private rate
overlays outside source control.

In a scheduled job, use all of the following:

- An explicit `--adapter`, rather than automatic format detection.
- `--content none` unless an approved question needs fingerprint evidence.
- A fixed `--since` and `--until` UTC window.
- `--strict-dq` only after discovery has established expected source coverage.
- A protected, retention-managed source archive sufficient to reproduce a
  financial conclusion.

## Integration Matrix

| Source | Packaged adapters or commands | Billing path | Release boundary |
| --- | --- | --- | --- |
| Claude Code and Anthropic | `collect claude-code`, `collect claude-code-headless`, `claude-code`, `trace@1`, `trace@2`, Anthropic report adapters | Anthropic Admin usage/cost pages; optional live Admin pull | Supported controlled-pilot workflow. |
| OpenAI and Azure-shaped data | `openai`, `openai-usage-buckets`, `openai-costs` | OpenAI usage/cost exports | File-based import and reconciliation. |
| Amazon Bedrock | `bedrock`, `aws-cur` | AWS CUR 2.0 CSV/CSV.gz | File-based import and reconciliation. |
| Generic observability | `otlp`, `copilot-otel` where applicable | Provider billing export for the underlying model service | Normalize trace evidence, then reconcile by provider/channel. |
| GitHub Copilot | `github-ai-usage`, `github-metered-usage`, `github-billing-api`, configuration/metrics/seats/activity adapters, `copilot-vscode-traces`, `copilot-export` | GitHub organization exports or recorded responses | Evidence-import and detector preview only; not an end-to-end public Copilot money workflow. |

Use `tokenbill ingest --help` to see the adapter contract in the installed
version. Adapter names are deliberately explicit so a scheduled job has an
auditable source mapping.

## Amazon Bedrock

### Source Preparation

Ask the cloud platform or application team for two separate evidence streams:

1. Bedrock invocation logs or application-side Converse response records for
   request-level usage and behavior analysis.
2. AWS Cost and Usage Report (CUR) files for the same time window, including
   the Bedrock line items and the organization's approved account, region,
   cost-allocation, and discount context.

The application telemetry explains usage. The CUR establishes the cloud cost
claim. Do not replace one with the other.

### Ingest, Reconcile, And Report

```bash
tokenbill --config "$CFG" ingest --db "$DB" \
  --adapter bedrock --content none exports/bedrock/

tokenbill --config "$CFG" reconcile --db "$DB" \
  --aws-cur exports/aws-cur/cur-2026-09.csv.gz \
  --since 2026-09-01 --until 2026-10-01 \
  --contract protected/bedrock-contract.json

tokenbill --config "$CFG" findings --db "$DB" \
  --since 2026-09-01 --until 2026-10-01 --min-usd 25

tokenbill --config "$CFG" report --db "$DB" \
  --since 2026-09-01 --until 2026-10-01 -o reports/bedrock-september.html
```

The `--contract` file is an access-controlled overlay for approved private
prices, discounts, or treatment rules. Version it with the finance-approved
effective dates. A rate-card result is a diagnostic estimate until the CUR
reconciliation passes for the same channel and window.

### Operational Controls

- Keep the raw CUR and the derived report for the required audit window.
- Reconcile by account, region, currency, period, and known discount basis.
- Review provisional or revised CUR periods before publishing an executive
  total; `reconcile --closed-only` can exclude the revision window.
- Use application or OTLP traces to attach workflow context; CUR alone cannot
  reliably explain a request, retry loop, tool fanout, or cache decision.
- Reconfirm Bedrock logging, pricing, and CUR mechanics against the provider's
  current documentation before each production rollout. The pilot guide links
  the primary source material.

## GitHub Copilot

### What Is Packaged

The v0.2.0 source tree contains parsers for several Copilot-adjacent evidence
formats:

- GitHub AI usage and legacy premium-request CSV reports via
  `github-ai-usage`.
- Detailed or summarized metered-usage CSV reports via
  `github-metered-usage`.
- Recorded GitHub billing REST response pages via `github-billing-api`.
- Exported Copilot configuration, metrics, seat, activity, agent-task, usage,
  and export-bundle formats through their named adapters.
- Read-only, allowlisted local VS Code agent-trace data through
  `copilot-vscode-traces` when an organization has explicitly enabled that
  source.
- OpenTelemetry and Copilot CLI/GitHub Actions usage artifacts where the
  organization already produces them.

The importers preserve the distinction between seats, subscription allowance,
AI credits, metered usage, and activity. User identity references are handled
through the selected Token Bill identity mode; source exports should still be
treated as sensitive organization data.

### Controlled Evidence Pilot

Export reports through the enterprise's normal GitHub owner or billing-manager
process. Store the export in a protected location and use the matching explicit
adapter:

```bash
tokenbill --config "$CFG" ingest --db "$DB" \
  --adapter github-ai-usage --content none exports/github/ai-usage.csv

tokenbill --config "$CFG" ingest --db "$DB" \
  --adapter github-metered-usage --content none exports/github/metered-usage.csv

tokenbill --config "$CFG" findings --db "$DB" \
  --since 2026-09-01 --until 2026-10-01
```

For a recorded API response or another export type, select the matching name
from `tokenbill ingest --help`; do not use an unrelated parser simply because
it accepts the file.

### Current Product Boundary

Copilot is not yet a complete public enterprise workflow in v0.2.0. The
repository contains the import and detector pieces, but the public wiring is
unfinished:

- `tokenbill copilot` is not a shipped CLI command.
- Token Bill does not currently authenticate to GitHub with OAuth or perform a
  live GitHub API pull.
- The generic `bill`, `report`, `showback`, and `policy` commands do not render
  the Copilot pool, credit, allowance, and seat model as a complete audited
  Copilot financial result.
- A generic bill can omit extension-owned Copilot facts. It must not be used as
  a Copilot invoice, subscription allocation, procurement claim, or automated
  policy decision.

Use the current path to validate source availability, data handling, and
detector value with a volunteer cohort. Do not announce it as a completed
Copilot showback or savings product until the missing command, dedicated
summary/policy path, reconciliation semantics, and provider-plan fixtures are
implemented and tested together.

## OpenAI, Azure-Shaped Records, And OTLP

OpenAI and generic OTLP inputs follow the same local pattern:

```bash
tokenbill --config "$CFG" ingest --db "$DB" \
  --adapter openai --content none exports/openai/responses.jsonl

tokenbill --config "$CFG" ingest --db "$DB" \
  --adapter otlp --content none exports/otel/genai.json

tokenbill --config "$CFG" reconcile --db "$DB" \
  --openai-usage exports/openai/usage.json \
  --openai-costs exports/openai/costs.json \
  --since 2026-09-01 --until 2026-10-01
```

The telemetry establishes request behavior. The usage/cost export establishes
the financial control point. Retain semantic-convention and provider-schema
versions with the source archive so a later schema change can be explained.

## Local Claude Code And Legacy Traces

For managed Claude Code collection, inspect the command and scope access first:

```bash
tokenbill collect claude-code --help
tokenbill collect claude-code-headless --help
```

For a single-agent investigation, the legacy `Recorder` writes `trace@1` JSONL
and `tokenbill analyze` evaluates Anthropic cache behavior. Those records can
include prompts and source context. They are useful for a tightly controlled
debugging workflow, but not the default enterprise data tier.

## Reporting And Change Management

Before publishing a report, establish the following for every included
provider/channel:

1. The UTC window, provider, account, currency, region, and contract basis.
2. Whether each number is billed/reconciled, rate-card/list-equivalent,
   modeled/estimated, or measured/verified.
3. Data-quality status, source freshness, and unresolved reconciliation
   residuals.
4. An owner, quality metric, rollback condition, and approval route for every
   proposed change.

Token Bill emits evidence and review artifacts. It does not apply a model
routing, prompt, cache, or IDE policy itself. A lower token result is not a
realized savings outcome until quality, rework, and incremental spend are
measured after an approved rollout.
