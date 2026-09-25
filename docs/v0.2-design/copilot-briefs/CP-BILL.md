### CP-BILL — GitHub billing adapters: AI usage report, metered usage CSV, billing REST pages (wave 2)

**Goal.** Parse GitHub's invoice-side truth for Copilot into exact, content-free, revision-safe records: the
AI usage report (per user × day × model tokens, credits, gross, discount, net), the detailed / summarized
usage reports (seats, Actions minutes by Copilot and agentic workflows, sandboxes, Code Quality) and the
billing REST pages — from either source an admin can produce: the report-export API (`copilot pull`) or the
**UI downloads** of the no-token handoff path (Usage → AI usage → Get usage report; the detailed usage report),
which are the same CSV formats. These are the primary, release-blocking inputs for an organization whose
developers use VS Code and IntelliJ, and the seat SKU lines are the strongest plan evidence (owner answer 2).
Read SPEC §3.2–§3.5, §5.1, §5.13, §8; addendum DC2, DC4, DC21, DC22, §3.1 (CA-4, CA-5,
CA-19), §4, §5.1–§5.3, §5.11 (report conventions), §19.3 #1–#6, #16–#17, §19.5 #1, #8, #9, #16;
`F-POOL.md` (`detect_plans` sources); `CORE-AMENDMENTS.md` items C-5, C-9, C-10, C-12.

**Owns.** `tokenbill/adapters/github_billing.py` (`AiUsageReportAdapter`, `MeteredUsageAdapter`,
`BillingApiAdapter`; registers conventions `github.ai_usage_report.excl` / `.incl` on import),
`tests/v2/copilot_bill/**`, `tests/v2/fixtures/copilot_bill/**`.

**Consumes.** `core.records` (`CostLine` CA-4 fields, `GITHUB_COST_TYPES`, `UsageAggregate` CA-5),
`core.types`, `core.money`, `core.jsonl.load_json_exact`, `core.ids` (`pseudonym`, `natural_id`),
`core.models.normalize_copilot_model`, `core.catalog` (`copilot_cost_type`, `copilot_workload`,
`runner_rate`), `core.conventions`, `core.facts`, `core.builders`, `core.testing`.

**Provides.** Registry adapters `github-ai-usage`, `github-metered-usage`, `github-billing-api`; the two
report conventions (golden sum-check fixture each); fixtures with a `MANIFEST.json` (provenance class per
file) read by CP-STORE, CP-RECON and CP-SYNTH-W gate tests.

**Build.**
1. AI usage report per addendum §5.1: header mapping, BOM, quoted fields, three date formats, exact money
   (remainders as `stats["rounding_remainder_e18"]`), row identity, legacy PRU / April directional / preview
   columns, model normalization into `model` / `routing` / `speed` / `pseudo` fields, cost types from
   `GITHUB_COST_TYPES`, workloads from pseudo or SKU, empty username → `ai_credit.direct`, team / cost-center
   maps then `p_`, tokens under `excl`, natural ids with in-file duplicate summing, one aggregate per (day,
   team, cost center, org, model, sku, routing, speed, pseudo), one coverage aggregate per (file, day),
   finality by `report_lag_days` and open month, `fetched_ms = opts.now_ms`.
2. Detailed / summarized CSV per §5.2: channels by product, the three dynamic Copilot paths and the
   `.lock.yml` → `agentic_workflow` rule with `workflow = h_(path)` (**VERIFY** in your README), other paths
   never stored (count only), seat SKUs → `seat` with plan, both larger-runner spellings, Actions discounts
   kept.
3. Billing REST pages per §5.3 (alias table for SKU / unit names; integer `quantity` ignored for AI-credit
   SKUs; export envelopes yield no records; `download_urls` never stored).
4. **Plan evidence.** Seat SKU lines keep `sku` exactly (`copilot_for_business`, `copilot_enterprise`,
   `copilot_standalone`) — `core.pool.detect_plans` maps them. Only with `"copilot-report-quota" in
   opts.experimental`: `total_monthly_quota` (skipping `Unknown` and 0) becomes one
   `ConfigSnapshot(kind="plan_quota", source_kind="github.ai_usage_report", entity_id="org:<organization>")`
   per (month, org, quota value) with attrs `month`, `quota` (decimal string), `n_users` (distinct principals
   with that quota in the month) — counts only, no principal; without the flag the column is read for the
   legacy-row rule only (`dq.copilot_report_quota_ignored` counts rows carrying it).

**Facts to verify.** Current AI usage report columns and grouping (documented grouping `date, model,
username`); model label format; `spark_ai_credits` spelling; seat SKU unit; REST SKU display names;
agentic-workflow `workflow_path`. Unknown shapes quarantine with a reason.

**Acceptance tests.**
- GitHub's test row parses to gross = discount = 427,262,130 nano, net 0, routing auto, model
  `claude-haiku-4-5`, cost type `ai_credit.user`.
- 40-row fixture: row identity; aggregates sum to rows; empty username → `ai_credit.direct`, no principal;
  `CANARY_LOGIN` absent everywhere; `p_` under the principal key id; pseudo code-review rows keep tokens and
  carry `pseudo="code_review"`, workload `copilot_code_review`; `M/D/YY` / ISO dates; BOM; `Unknown` quota;
  legacy PRU and April files handled.
- Overlap fixtures: ingesting `ai_usage_overlap_a.csv` then `_b.csv` into `MemoryStore` yields each natural
  id once with `_b`'s revised amount (latest `fetched_ms`); in-file duplicate natural keys are summed with the
  dq code.
- Metered CSV: seats with plans, three dynamic paths, a `.lock.yml` row → `agentic_workflow` with an `h_`
  workflow, user workflows dropped (count only), sandbox rows on `github_sandbox`, `linux_16_core` and
  `actions_linux_16_core` both mapped.
- REST pages: both OAS example variants parse; exact decimals; envelopes produce no records.
- Plan quota: a fixture month with 60 users at quota 3900 and 5 at `Unknown` → with the flag one `plan_quota`
  snapshot (`quota "3900"`, `n_users 60`) and no `p_` in it; without the flag none and the dq count; a
  UI-download file and an export-API file of the same rows produce identical records (source ids aside).
- Convention sum-check goldens for `.excl` and `.incl`; `assert_adapter_conforms` on all three adapters;
  hypothesis fuzz of the CSV and JSON parsers; no float in the module.

**Size.** ~2.5k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
