# Gap track: automated LLM workloads (CI agents, review bots, headless jobs, scheduled agents, evals, embeddings)

- **Track:** `gap-automated-workloads-ci-evals`
- **Date:** 2026-09-23
- **Product:** Token Bill v0.1.2 (Anthropic-only pricing; JSONL traces; cache replay; 5 cache breakers)
- **Question:** An enterprise with thousands of developers runs LLM work that no human waits on. What is that work, how can Token Bill detect it, which levers shrink it, and how much do they save?

## How to read this report

- **Evidence labels.**
  - **strong:** primary documentation or a peer-reviewed or arXiv paper that I opened, stating the number directly.
  - **moderate:** a primary source that states the mechanism but not a measured saving, or a vendor figure for a single configuration.
  - **weak:** an inference or a single-source vendor claim.
- **Dollar figures.**
  - All dollar examples use the Anthropic list prices I opened on 2026-09-23 (pricing page). Opus 5 is $5 input / $25 output, with cache reads at $0.50 and 5-minute cache writes at $6.25. Sonnet 5 is $2 / $10, with reads at $0.20 and 5-minute writes at $2.50. Haiku 4.5 is $1 / $5.
  - Anything labelled **illustrative** is my own arithmetic on those prices, not a measured result.
- **Local sources.** The bundled Anthropic `claude-api` skill docs (`/private/tmp/claude-501/bundled-skills/2.1.280/.../claude-api/shared/*.md`) count as authoritative for API behaviour. Where one of them is the only source, I cite it as "bundled doc".
- **Sources.** Every source was opened during this pass. Web search was exhausted early in the session, so I reached every source by fetching a known URL directly. Anything I could not confirm is marked unknown.

---

## 0. Executive summary

1. **Nobody publishes an "automated share of LLM spend" figure.** I found no Datadog, Anthropic, GitHub, Cursor, CodeRabbit or Graphite figure for the share of enterprise spend that goes to CI, evals or scheduled work.
   - The best proxy is Anthropic's Economic Index: **77% of business API usage shows automation patterns**, against about 50% on Claude.ai.
   - The unit prices, however, show that automated review alone can match or exceed interactive spend:
     - Anthropic's managed Code Review averages **$15–25 per review**.
     - Claude Code interactive use averages **$150–250 per developer per month**.
     - So automated review spend passes interactive spend at about **6–17 review runs per developer per month**. With "review on every push", a handful of PRs gets there.
   - Token Bill should measure this share, not assume it. "Automation share of spend" should be a headline metric.
2. **Per-review cost varies by 15–500× across products**, and the cause is depth, not token price:
   - GitHub Copilot code review "Lite": $0.05–1.
   - Copilot "Balanced": $0.25–5.
   - Anthropic's multi-agent Code Review: $15–25.
   - A large-scale study of 22,326 AI review comments found that only **0.9–19.2%** led to code changes, against 60% for human comments. Comments on manually triggered reviews were addressed at **12.8% vs 6.8%** for automatic ones. Hunk-level tools beat file-level tools.
   - The metric that matters is therefore **$ per addressed finding**, not $ per review.
3. **The biggest free lever for CI agents is trigger policy.**
   - Anthropic's docs say "after every push" review multiplies cost by the number of pushes.
   - The practical defaults are:
     - review on open or ready-for-review;
     - opt-in re-review;
     - cancel superseded runs;
     - skip drafts, bot PRs and lockfile-only diffs;
     - path filters.
   - These cut runs, not tokens per run, so they stack with every other lever.
4. **CI runs of Claude Code usually cannot share a prompt cache across runs.**
   - Each session's system prompt embeds the working directory, platform, OS, memory paths and the startup git snapshot.
   - Caches are also isolated per workspace.
   - Token Bill should check for these fixes:
     - `--exclude-dynamic-system-prompt-sections` (SDK: `excludeDynamicSections`);
     - one shared CI workspace;
     - a pinned CLI version fleet-wide;
     - `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` when run gaps are 5–60 minutes.
   - These turn a per-run cold write (about 1.25× input) into a read (0.1×).
   - In Anthropic's cookbook, a "stable-first" shared prefix across independent tasks cut cost **54%**.
   - Datadog reports that only **28%** of LLM spans show any cached-read tokens, while system prompts are **69%** of input tokens.
5. **Batch or flex pricing takes 50% off single-shot automated work.** This holds for Anthropic, OpenAI, Bedrock and Gemini. Voyage embeddings get 33%.
   - The discount stacks with caching.
   - Batch cache hits are best-effort, "30% to 98%".
   - Batch does **not** apply to Claude Managed Agents, so scheduled deployments pay full price plus $0.08 per session-hour. It also does not apply to tool-loop agents unless the loop is flattened.
   - Evals and LLM judges are the most batch-eligible automated workload.
6. **Scheduled agents have a failure mode specific to the cache TTL.**
   - A `/loop` or scheduled task "fires on its interval even while the session is idle, sending your full context each time".
   - If the cadence is longer than the cache TTL, every fire re-writes the whole context. Illustratively, that is about $0.94 per fire for 150k tokens on Opus 5.
   - Token Bill should report $/fire, no-op fire rate, and cadence against TTL.
7. **Eval cost can fall by 50–140× through item selection** without losing ranking fidelity:
   - tinyBenchmarks: 100 items stand in for MMLU's 14K.
   - metabench keeps under 3% of items.
   - Fluid Benchmarking uses 50× fewer items.
   - Efficient Benchmarking cuts compute by 100× or more.
   - Separately, sizing trials to the effect you care about avoids over-running. Anthropic's rule of thumb puts the noise floor at about `1/sqrt(n·R)`: 25 cases × 2 reps ≈ ±14 points.
   - Cheap judges (Haiku, a panel of LLM evaluators or "juries" (PoLL), 7× cheaper; cascaded judges) are fine for **ranking**, but less so for absolute scores.
8. **Embeddings belong in scope, but as a small P2 module.** It should be a ledger plus a churn and dimension calculator, not a vector-DB optimizer.
   - Embedding tokens cost $0.02–0.20 per MTok, 10–250× below LLM input and output.
   - Re-embedding a 5B-token corpus once costs $100–1,000, about 5–50 Code Reviews.
   - The waste that matters is **re-embedding unchanged chunks**, and **storage from full-width float32 vectors**. int8 is 4× smaller and binary 32× smaller, with 96–99% retention after rescoring.

---

## 1. Q1: How big is automated LLM spend? (share, unit costs, benchmarks)

### 1.1 Share of spend: no direct published figure (open question)

No opened source (Datadog, Anthropic, GitHub, Cursor, CodeRabbit, Graphite, FinOps Foundation) breaks enterprise LLM spend into CI, eval and scheduled versus interactive. The closest proxies:

| Proxy | Value | Source |
|---|---|---|
| Business API traffic with automation (delegation) patterns | **77%**, vs ~50% on Claude.ai | Anthropic Economic Index, 2025-09-15 |
| API traffic mapping to computer/math tasks | "little less than half" | same |
| Price elasticity of API task usage | 1% cost increase → 0.29% usage decrease ("weak price sensitivity") | same |
| Share of input tokens that are system prompts, across Datadog customers | **69%** | Datadog State of AI Engineering, July 2026 |
| LLM spans with any cached-read tokens | **28%** | same |
| Agentic app requests that made a single service call | 59% | same |
| Dev/test bleed named as an anomaly root cause | qualitative | FinOps Foundation "Tokenomics", 2026-06-03 |

**Conclusion.** Token Bill must *measure* the automated share per organization. Section 2 gives the classifier; section 5 gives the metrics.

### 1.2 Published unit costs for automated work

| Workload | Published unit cost | Source (date) |
|---|---|---|
| Claude Code, interactive | ~$13 per dev per active day; $150–250 per dev per month; <$30 per active day for 90% of users | code.claude.com/docs/en/costs (accessed 2026-09-23) |
| Anthropic Code Review (managed, multi-agent, full-codebase) | **$15–25 per review** on average, scaling with PR size; ~20 minutes average | code.claude.com/docs/en/code-review (accessed 2026-09-23) |
| GitHub Copilot code review, AI-credit plans | **Lite $0.05–$1**, **Balanced $0.25–$5** per review, plus GitHub Actions minutes | docs.github.com Copilot code review (accessed 2026-09-23) |
| GitHub Copilot code review, legacy request-based Pro/Pro+ | 13 premium requests per review × $0.04 = **$0.52** | docs.github.com copilot-requests (accessed 2026-09-23) |
| Copilot cloud (coding) agent | 1 premium request × model multiplier per session, plus steering comments; uses Actions minutes; timeout configurable (max 59 minutes) | docs.github.com (accessed 2026-09-23) |
| CodeRabbit | $24 / $48 / $72 per dev per month; 5/8/10/12 PR reviews per dev per hour; overage **$0.25 per reviewed file**; agent $0.40 per agent-minute | coderabbit.ai/pricing (accessed 2026-09-23) |
| Graphite | $20 per user per month (limited AI reviews); $40 per user per month (unlimited AI reviews) | graphite.com/pricing (accessed 2026-09-23) |
| Cursor Bugbot | usage-based; runs on every PR update by default; incremental review on by default | cursor.com/docs/bugbot (accessed 2026-09-23) |
| Claude Managed Agents session (scheduled deployments) | model tokens at list price, **no batch discount**, plus **$0.08 per session-hour** of runtime | platform.claude.com pricing (accessed 2026-09-23) |
| Agent eval campaign (HAL) | ~**$40,000** for 21,730 rollouts across 9 models and 9 benchmarks (≈$1.84 per rollout); 2.5B tokens | arXiv 2510.11977 (2025-10-13) |
| Agentic coding eval per task (Anthropic, SWE-bench Pro, Opus 5) | ~$0.93 per task at fixed effort; ~$0.45 with low effort plus a re-run of failures | platform.claude.com optimizing-for-cost-and-intelligence (accessed 2026-09-23) |

**Break-even (derived from the rows above).**

- Automated review spend equals interactive Claude Code spend at `(150…250 $/dev/mo) / (15…25 $/review)` = **6–17 review runs per developer per month**.
- With "after every push", review runs = pushes. Four PRs a month with three pushes each gives 12 runs. That puts the organization's review bill at or above its interactive bill.
- The same 12 runs on Copilot Lite cost **$0.60–12**.
- **Illustrative:** for 1,000 developers at 12 runs each, the difference between tiers is roughly $2k versus $180–300k a month.

---

## 2. Workload taxonomy and detection rules

### 2.1 Taxonomy

| ID | Class | Examples | Human waiting? | Batch-eligible? | Primary levers |
|---|---|---|---|---|---|
| W1 | Interactive dev | Claude Code REPL, IDE | yes | no | caching, model/effort, context hygiene (other tracks) |
| W2 | CI coding agent, event-triggered | `claude-code-action` `@claude` mention, issue→PR, Copilot cloud agent, Codex action | no (asynchronous) | no (tool loop) | turn and $ caps, cross-run cache, trigger hygiene, model/effort |
| W3 | Automated PR review | Anthropic Code Review, Copilot review, Bugbot, CodeRabbit, Graphite, self-hosted `/code-review` in Actions | no | partially (single-shot diff review) | trigger policy, tiered depth, incremental review, cancel superseded runs, dedupe |
| W4 | Headless scripted jobs | `claude -p` in build scripts (lint, triage, commit messages), Agent SDK services | no | often (single-shot) | `--bare`, `--exclude-dynamic-system-prompt-sections`, batch, caps |
| W5 | Scheduled or background agents | GitHub `schedule`, Managed Agents deployments, Routines, Desktop scheduled tasks, `/loop` | no | no (Managed Agents: no batch) | cadence vs TTL, no-op detection, event-driven triggers, per-run budget |
| W6 | Eval suites and LLM-as-judge | promptfoo or pytest evals, hill-climbs, judge rubrics | no | **yes** (single-shot cases, judges) | subset selection, trial sizing, cheap judges, batch or flex, result cache |
| W7 | Prompt-regression tests in CI | eval on PRs that touch `prompts/**` | no | yes | path filters, result cache, discriminating subset |
| W8 | Offline data jobs | classification, enrichment, backfills, report generation | no | **yes** | batch or flex, caching, model right-sizing |
| W9 | Embedding, re-embedding and rerank | RAG indexing, codebase indexing | no | yes (OpenAI, Gemini, Voyage batch) | content-hash incremental, dims or quantization, batch |
| W10 | Dev/test bleed | notebooks, experiments, prod keys used in test | sometimes | n/a | separate keys or workspaces, spend limits, cheaper models |

### 2.2 Detection signatures (all opened and verified)

| Signal | What it proves | Where it comes from | Strength |
|---|---|---|---|
| `CLAUDE_CODE_ENTRYPOINT="claude-code-github-action"` | Run is `claude-code-action` (W2/W3) | Set in `base-action/src/parse-sdk-options.ts:284` of anthropics/claude-code-action (commit 46a42b4, 2026-09-23) | strong |
| OTel `app.entrypoint` ∈ {`cli`, `sdk-cli`, `sdk-ts`, `sdk-py`, `claude-vscode`} | Headless or SDK vs interactive launch. **Opt-in:** `OTEL_METRICS_INCLUDE_ENTRYPOINT=true` (default false) | code.claude.com monitoring-usage | strong (only if enabled) |
| OTel `claude_code.active_time.total{type="user"}` ≈ 0 while `type="cli"` > 0 | No keyboard interaction, so no human at the terminal | same | moderate |
| `claude_code.user_prompt.command_name` / `command_source` | Prompt is a slash command or skill (for example `/code-review`), typical of automation | same | moderate |
| Claude Code Analytics API `actor.type="api_actor"` with `api_key_name`; `terminal_type` | API-key actor (CI/service), not an OAuth user | platform.claude.com claude-code-analytics-api | strong for "non-human identity" |
| Workload Identity Federation service account `svac_…`; federation rule on issuer `token.actions.githubusercontent.com`, subject `repo:org/repo:ref:…` | CI identity (GitHub OIDC), scoped to a workspace | platform.claude.com wif-admin-api | strong |
| Usage report `service_tiers[]=batch`; `group_by` `api_key_id` / `workspace_id` | Traffic already batched; per-key and per-workspace attribution | platform.claude.com usage-cost-api | strong |
| GitHub env `CI=true`, `GITHUB_ACTIONS=true`, `GITHUB_EVENT_NAME` (`schedule`, `pull_request`, `issue_comment`, `workflow_dispatch`), `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_SHA`, `GITHUB_WORKFLOW_REF` | CI provenance; `RUN_ATTEMPT>1` means a re-run | docs.github.com variables | strong (if propagated into `OTEL_RESOURCE_ATTRIBUTES`) |
| GitHub billing `workflow_path = dynamic/agents/copilot-pull-request-reviewer` | Copilot code review Actions minutes | docs.github.com Copilot models-and-pricing | strong |
| Managed Agents `deployment_run.trigger_context.type` ∈ {`schedule`, `manual`} | Scheduled vs manual firing | bundled doc `managed-agents-scheduled-deployments.md` | strong |
| `/usage` "Loops" rows (fires, per-run tokens) | Heavy `/loop` or scheduled tasks (local) | code.claude.com costs (v2.1.242+) | strong (local only) |
| Inter-arrival periodicity near a cron lattice, with jitter tolerance | Scheduled work. The detector must tolerate documented jitter: Managed Agents up to 15% of the interval (cap 9 min); `/loop` up to 30 min or half the interval; GitHub `schedule` delayed "at the start of every hour" and can drop jobs | bundled scheduled-deployments doc; code.claude.com scheduled-tasks; docs.github.com events | moderate (heuristic) |
| Many requests with a byte-identical system prompt and tools that differ only in one payload slot, plus a judge-style rubric prompt | Eval or judge traffic (W6) | Token Bill's own canonical rendering (heuristic) | weak to moderate |
| `/v1/embeddings` or rerank endpoints; the same `doc_id` re-sent with an unchanged content hash | W9 re-embedding churn | gateway or provider logs (heuristic) | moderate |

### 2.3 Classifier rule (proposed)

Compute a score and assign a class when confidence is ≥ 0.8. Otherwise label the traffic `unknown-automated` or `unknown`.

- **automated = true** when any strong signal is present:
  - the entrypoint env is `claude-code-github-action`;
  - `app.entrypoint` ∈ {`sdk-*`};
  - an `api_actor` or `svac_` identity;
  - `CI=true`;
  - `service_tier=batch`;
  - a Managed Agents `trigger_context`.
- **Otherwise, automated = true** when there is no human keystroke time *and* the interval is periodic, or when the request comes from a key or workspace tagged `ci`, `eval` or `batch`.
- **Sub-class** by `GITHUB_EVENT_NAME` (pull_request → W3/W7; issue_comment → W2; schedule → W5), prompt fingerprint (judge rubric → W6), and endpoint (embeddings → W9).
- **Always report the confidence** and the signals used. A FinOps reader must be able to audit every attribution.

---

## 3. Lever table (sourced savings ranges)

Ranked by breadth of applicability, not by dollar ceiling. The ceiling depends on the organization's mix and must come from its own measurement.

| # | Lever | Workloads | Sourced effect | Evidence | Key caveats |
|---|---|---|---|---|---|
| L1 | **Trigger policy**: review once (on open or ready), opt-in re-review, skip drafts, forks and bots, path filters, cancel superseded runs | W2, W3, W7 | "After every push" multiplies cost by pushes (Anthropic). Manual-trigger comments were addressed 12.8% vs 6.8% automatic (study of 22,326 comments) | strong (mechanism), moderate (value) | Less coverage of late-pushed bugs; mitigate with an opt-in `@claude review` |
| L2 | **Tiered review depth**: diff-only or lite first, deep review only for risky PRs | W3 | Per-review cost from $0.05 (Copilot Lite) to $25 (Anthropic multi-agent), a 15–500× span. Hunk-level tools addressed at 6.5–19.2% vs file-level 0.9–4.2% | moderate | Risk tiering needs a path, size or label policy; deep review catches more cross-file bugs |
| L3 | **Cross-run prompt-cache reuse**: exclude dynamic sections, one CI workspace, pinned CLI, 1h TTL for 5–60 min gaps | W2, W3, W4 | Stable-first shared prefix across independent tasks: **54%** cheaper (Anthropic cookbook). Agent-loop caching 2.7–5.3× (Anthropic). Only 28% of spans read cache (Datadog) | strong | Only the static prefix benefits. Unique diff payload never caches. Haiku 4.5 minimum cacheable prefix is 4,096 tokens |
| L4 | **Fan-out stagger** (fire one, await first token, fire N−1) | W3 multi-agent, W6 parallel evals, matrix jobs | "N parallel requests with identical prefixes all pay full price"; Claude Code holds fan-out siblings up to 5 s | strong (mechanism) | Adds seconds of latency; savings = (N−1) × prefix × (1.25−0.1) × input price |
| L5 | **Batch or flex tier** for single-shot automated calls | W4, W6, W7, W8, W9 | **50%** (Anthropic, OpenAI Batch and Flex, Bedrock batch and Flex, Gemini batch); **33%** Voyage; stacks with caching | strong | ≤24 h (Anthropic, OpenAI), 12 h (Voyage); no tool loop mid-batch; not for Managed Agents; batch cache hits best-effort (30–98%) |
| L6 | **Caps and budgets**: `--max-turns`, `--max-budget-usd`, job `timeout-minutes`, Managed Agents deployment budgets, workspace and Code Review monthly caps | all automated | Hard caps limit the tail (top 2 of 20 problems carried 43% of spend; 30× run-to-run token variance). Model-visible task budgets: **44%** less cost for ~3 points, **58%** for ~6 points | strong | Hard caps waste the tokens already spent when they fire. Set them at p95–p99 of history; use effort or task budgets for savings |
| L7 | **Cheap triage or cascade** (Haiku or low effort decides whether and how deep) | W3, W6, W8 | FrugalGPT: up to 98% cost reduction at GPT-4 quality. Haiku 4.5 at ~1/10 of Opus 5's cost per question (63% vs 92% accuracy). Opus 5 at medium effort: ~50% cheaper for ~2 points on coding | strong (papers, vendor) | Triage errors skip real bugs. Measure recall on a labelled PR set |
| L8 | **Re-run failures at higher effort** (checker-gated) | W2, W6 | ~$0.45 vs $0.93 per task at ~93% vs 91.7% pass (SWE-bench Pro, Opus 5) | strong | Needs a reliable checker (tests); failures take twice the wall-clock |
| L9 | **Content-keyed result cache**: (diff or SHA, prompt version, model, params) → reuse | W3, W6, W7, W9 | promptfoo caches by default (14-day TTL) keyed on provider, request digest and config. Code Review skips PRs it already commented on | moderate | Deterministic reuse changes the sampling semantics of evals. Key on everything that changes the output |
| L10 | **Event-driven instead of polling** (Monitor tool, Channels, webhooks) | W5 | Anthropic: Monitor "is often more token-efficient" than re-running a prompt; scheduled fires send the full context each time | moderate | Needs an event source |
| L11 | **Cadence vs TTL alignment** for scheduled agents | W5 | Illustrative (Opus 5, 150k context): ~$0.075 per fire when warm vs ~$0.94 per fire when cold | strong (mechanism), illustrative ($) | Jitter can push a cadence past the TTL |
| L12 | **Eval subset selection** (IRT, anchor points, adaptive) | W6, W7 | tinyBenchmarks 100 vs 14K items; metabench <3% of items at 1.24% RMSE; Fluid Benchmarking 50× fewer items; Efficient Benchmarking ≥100× less compute; Sort & Search ~1000× | strong | Validated for static benchmarks. For private agent evals, re-validate subset fidelity against the full set periodically |
| L13 | **Trial sizing and paired statistics** | W6 | Noise floor ≈ 1/√(n·R): 25×2 ≈ ±14 pts, 100×2 ≈ ±7. Paired differences exploit 0.3–0.7 per-question score correlation. Clustered SEs can be over 3× naive | strong | Under-powered evals waste the whole spend |
| L14 | **Judge choice and cascade** | W6, W7 | PoLL panel of smaller judges over 7× cheaper than a GPT-4 judge, with less bias. Trust-or-Escalate starts with cheap judges and escalates. Haiku judge "cheap and fast enough to run on every PR" | moderate | Small judges are fine for ranking, not absolute scores (Judging the Judges) |
| L15 | **Judge batching and packing** | W6 | Batch API 50%. Batch prompting up to 5× (6 samples per call, few-shot era, pre-caching) | moderate | Packing can bias judges; with caching, the amortization gain is smaller |
| L16 | **Embedding hygiene**: content-hash incremental, MRL dims, quantization, shared-space models, batch | W9 | Hash-skip of unchanged documents (LlamaIndex); chunk-content cache and 92% cross-clone similarity (Cursor); MRL 768 dims keep 67.99 vs 68.16 MTEB (Gemini); int8 4×, binary 32× storage with 96–99% retention after rescoring | strong | Changing dims or model forces a full re-index |
| L17 | **Dev/test bleed controls** | W10 | FinOps names it an anomaly root cause; workspace spend and rate limits exist | moderate | Organizational, not technical |
| L18 | **Headless determinism**: `--bare` | W4 | Skips hooks, plugins, MCP, CLAUDE.md and auto-memory; the "recommended mode for scripted and SDK calls" | moderate | Loses CLAUDE.md context unless passed explicitly |

---

## 4. Findings (detailed)

### F1. There is no public "automated share of spend"; the Economic Index 77% is the best proxy (`auto-share-proxy`)

- **Facts.**
  - Anthropic Economic Index (2025-09-15): "77% of business uses involve automation usage patterns, compared to about 50% for Claude.ai users."
  - "Little less than half of all API traffic maps to computer and mathematical tasks."
  - Weak price sensitivity: 1% cost increase → 0.29% usage decrease.
  - No opened vendor (Datadog, GitHub, Cursor, CodeRabbit, Graphite) reports what share of LLM spend is CI, eval or scheduled.
- **Implication.** "Automation" in the Economic Index means directive delegation, not necessarily "no human waiting". It is an upper-bound-style proxy for the share that could tolerate batch or asynchronous processing.
- **Token Bill should:**
  - make **Automated share of spend** (W2–W9 over total) a first-class KPI, with the classifier's confidence shown;
  - break it down by class, repo, team and workflow.
- **Sources.** https://www.anthropic.com/research/anthropic-economic-index-september-2025-report (2025-09-15)
- **Evidence:** moderate (proxy). **Savings:** enabling. **Effort:** M.

### F2. Unit costs make automated review a first-order line item (`ci-review-unit-costs`)

- **Facts.** See §1.2.
  - Anthropic Code Review averages $15–25 per review and ~20 minutes. It is billed separately through usage credits, with a monthly spend cap and a per-repo average-cost column.
  - Copilot review costs $0.05–1 (Lite) or $0.25–5 (Balanced), plus Actions minutes. Legacy request plans pay 13 premium requests × $0.04.
  - CodeRabbit: $24–72 per dev per month, then $0.25 per reviewed file.
  - Claude Code interactive: $150–250 per dev per month.
- **Implication.** Review spend passes interactive spend at 6–17 review runs per dev per month. Review product and depth choice alone spans 15–500× per review.
- **Token Bill should:**
  - ingest managed-review spend (Anthropic Code Review analytics and cost figures; GitHub billing reports filtered by `copilot-pull-request-reviewer`) alongside Claude Code;
  - compute $ per reviewed PR and $ per review run per repo;
  - flag repos where review spend per developer exceeds interactive spend per developer.
- **Sources.**
  - https://code.claude.com/docs/en/code-review (accessed 2026-09-23)
  - https://code.claude.com/docs/en/costs (accessed 2026-09-23)
  - https://docs.github.com/en/copilot/concepts/agents/code-review (accessed 2026-09-23)
  - https://docs.github.com/en/copilot/concepts/billing/copilot-requests (accessed 2026-09-23)
  - https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing (accessed 2026-09-23)
  - https://www.coderabbit.ai/pricing (accessed 2026-09-23)
  - https://graphite.com/pricing (accessed 2026-09-23)
- **Evidence:** strong. **Savings:** the choice of depth and trigger drives 15–500× per review. **Effort:** M.

### F3. The trigger multiplier: every push re-bills the review (`ci-trigger-multiplier`)

- **Facts.**
  - Anthropic Code Review offers three modes:
    - "Once after PR creation": runs once per PR.
    - "After every push": "multiplying cost by the number of pushes".
    - "Manual": opt in with `@claude review`, or `@claude review always` to subscribe.
  - Fork PRs are never auto-reviewed.
  - The `claude-code-action` review workflow triggers on `opened, synchronize, ready_for_review, reopened`. `synchronize` means every push.
  - That workflow's skill skips "draft and closed pull requests, pull requests it judges not to need a review, such as automated or trivial ones, and pull requests that already have a comment from Claude."
  - Cursor Bugbot runs "on every PR update" by default, with incremental review on.
  - GitHub `concurrency` with `cancel-in-progress: true` cancels pending or in-progress runs in the same group.
  - `paths` / `paths-ignore` filters stop a workflow when only ignored files change.
  - Copilot review excludes lockfiles, logs and SVGs.
- **Implication.**
  - Trigger policy changes the *number* of runs, so it multiplies with every per-run lever.
  - Canceling an in-progress LLM job still bills the tokens already spent. Cancellation saves only the remainder, which is another reason to trigger on `ready_for_review` rather than on `synchronize`.
- **Token Bill should:**
  - replay a repo's PR event history (opened, synchronize, ready_for_review, comments, run attempts) under alternative trigger policies and price each one;
  - detect superseded runs (a new SHA on the same PR within the run's duration), re-run attempts (`GITHUB_RUN_ATTEMPT>1` on the same SHA), draft-PR runs, bot-authored PRs and lockfile-only diffs;
  - report the dollars each detection would have saved.
- **Sources.**
  - https://code.claude.com/docs/en/code-review
  - https://code.claude.com/docs/en/github-actions (accessed 2026-09-23)
  - https://cursor.com/docs/bugbot (accessed 2026-09-23)
  - https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency (accessed 2026-09-23)
  - https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax (accessed 2026-09-23)
  - https://docs.github.com/en/copilot/concepts/agents/code-review
- **Evidence:** strong. **Savings:** the "every push" → "once" change removes (pushes per PR − 1) of the runs; unknown per organization until measured. **Effort:** M.

### F4. Value per dollar: most AI review comments are not acted on (`ci-review-addressed-rate`)

- **Facts.** "Does AI Code Review Lead to Code Changes? A Case Study of GitHub Actions" (arXiv 2508.18771, 2025-08-26, revised 2026-04-25) studied 22,326 AI comments from 16 review actions in 178 repositories.
  - AI comments led to changes **0.9–19.2%** of the time, against 60% for valid human comments.
  - Hunk-level tools: 6.5–19.2%. File-level tools: 0.9–4.2%.
  - For one action, manually triggered reviews were addressed **12.8% vs 6.8%** automatic.
  - Short comments with code were more effective.
- **Implication.**
  - "$ per review" rewards cheap noise. The enterprise metric should be **$ per addressed finding** (resolved thread, or a code change touching the commented hunk).
  - Anthropic Code Review exposes "comments auto-resolved because a developer addressed the issue", which is directly usable.
- **Token Bill should:**
  - join review cost to GitHub review-thread outcomes (resolved, outdated, or a code change on the hunk);
  - compute $ per addressed finding by tool, tier and repo;
  - recommend manual or opt-in triggers where the automatic address rate is low.
- **Sources.**
  - https://arxiv.org/abs/2508.18771 (2025-08-26; v2 2026-04-25)
  - https://arxiv.org/html/2508.18771 (opened)
  - https://code.claude.com/docs/en/code-review
- **Evidence:** moderate (one study; OSS repos). **Savings:** unknown; this is the value side of the cost/value ratio. **Effort:** M.

### F5. Tiered depth: diff-first review, deep review only when risk warrants (`ci-review-tiering`)

- **Facts.**
  - Copilot routes "Balanced" reviews "to a higher-reasoning model for longer analysis" at 5× the Lite range.
  - Anthropic Code Review uses "a fleet of specialized agents" plus a verification step over the full codebase ($15–25).
  - Anthropic's headless docs show the diff-only pattern: `gh pr diff "$1" | claude -p --append-system-prompt "...Review for vulnerabilities." --output-format json`. Piping the diff means Claude doesn't need Bash to read it.
  - Opus 5 on coding: `medium` effort ≈ 50% cheaper for ~2 points; `low` ≈ 75% cheaper for ~8 points.
  - Local `/code-review` exposes effort levels: `low`/`medium` report only high-confidence findings; `high` to `max` broaden coverage.
- **Token Bill should:**
  - provide a **risk-tier policy** (paths, diff size, labels, author) with a simulator that prices each tier on history;
  - recommend `lite` for the long tail and `deep` for risky paths;
  - measure address rate per tier (F4) to validate the policy.
- **Sources.**
  - https://docs.github.com/en/copilot/concepts/agents/code-review
  - https://code.claude.com/docs/en/code-review
  - https://code.claude.com/docs/en/headless (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md (accessed 2026-09-23)
- **Evidence:** moderate. **Savings:** per-review cost for the lite tier is 5–100× below deep review (vendor ranges). **Effort:** M.

### F6. CI runs of Claude Code do not share a prompt cache unless configured to (`ci-cross-run-cache`)

- **Facts.**
  - Claude Code: "the cache is effectively scoped to one machine and directory". Each conversation carries the working directory, platform, shell and OS version, and the system prompt names auto-memory paths.
  - "Sequential sessions share the prefix only when the git status snapshot taken at startup matches."
  - The fix is `--exclude-dynamic-system-prompt-sections` (CLI) or `excludeDynamicSections: true` (SDK). It moves "per-machine sections… into the first user message. Improves prompt-cache reuse across different users and machines running the same task."
  - CLAUDE.md is injected into the conversation, not the system prompt, so per-repo CLAUDE.md does not break the system-prompt cache.
  - Upgrading Claude Code "typically updates the system prompt or tool definitions", which triggers a cold rebuild.
  - Caches are isolated per workspace (Claude API, Claude Platform on AWS, Foundry) or per organization (Bedrock, Google Cloud). Each model has its own cache, and on most models each effort level has its own.
  - For API keys, the default TTL is 5 minutes. `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`, `promptCacheTtl`, or `ENABLE_PROMPT_CACHING_1H=1` switches to 1 hour.
  - The 1-hour TTL wins once "~1 turn in 30 follows a 5-min to 1-hour pause". With no pauses, the 5-minute default is 11–15% cheaper.
  - Anthropic cookbook: moving to a stable-first prefix across three independent claims cut cost 54% ($0.2784 → $0.1273).
  - Datadog: only 28% of LLM spans show cached reads, while 69% of input tokens are system prompts.
- **Illustrative.**
  - Prefix size: 40k tokens, in line with a sibling track's *local* measurement of median first-call prefix writes of 33–49k tokens in Claude Code transcripts (not a public source).
  - A cold write per run costs $0.10 on Sonnet 5 and $0.25 on Opus 5. A read costs $0.008 and $0.02. At 50,000 CI runs a month the difference is **$4.6k–11.5k a month**.
  - The Poisson hit-rate `1−e^(−λ·TTL)` (bundled cost-optimization doc):
    - a single repo with 10 runs a day gets 3.4% hits at a 5-minute TTL and 34% at 1 hour;
    - a fleet-wide standardized prefix at 1,000 runs a day gets 97% at 5 minutes.
  - So **standardizing the CI agent config across repos is what makes caching work**.
- **Token Bill should add a "cross-run cache" breaker family for CI:**
  - dynamic sections in the system prompt (detected from rendered-prefix divergence at the cwd, git or OS lines);
  - CLI version drift across runs;
  - CI traffic split across workspaces;
  - model or effort drift across jobs;
  - a TTL mismatch against the observed inter-run gap distribution.
  - Each should carry dollars recovered from a replay.
- **Sources.**
  - https://code.claude.com/docs/en/prompt-caching (accessed 2026-09-23)
  - https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts (accessed 2026-09-23)
  - https://code.claude.com/docs/en/cli-reference (accessed 2026-09-23)
  - https://code.claude.com/docs/en/agent-sdk/cost-tracking (accessed 2026-09-23)
  - https://platform.claude.com/cookbook/cost-optimization-cost-optimization (2026-08-09)
  - https://www.datadoghq.com/state-of-ai-engineering/ (July 2026)
  - https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md
  - bundled `shared/prompt-caching.md`, `shared/cost-optimization.md`
- **Evidence:** strong. **Savings:** the static-prefix share of CI input spend moves from 1.25× to 0.1×; 54% in Anthropic's worked example. **Effort:** M.

### F7. Fan-out and matrix jobs pay N cold writes; stagger, don't pre-warm (`ci-fanout-stagger`)

- **Facts.**
  - "A cache entry becomes readable only after the first response begins streaming. N parallel requests with identical prefixes all pay full price." The fix: send one request, await the first streamed token, then fire the remaining N−1.
  - Claude Code's workflow fan-out already "holds all but the first for up to 5 seconds by default".
  - Pre-warming (`max_tokens: 0`) is a latency lever. It is worth it only when "first-request latency is user-visible (chat/voice/interactive - not background jobs)". It is not supported inside Batches.
  - Sibling local measurement (not public): 780 agent first calls started ≤10 s after a sibling's, writing 15.8M tokens, about a 1.2% premium.
- **Token Bill should:**
  - detect bursts of ≥2 calls with an identical cacheable prefix that start within one time-to-first-token of each other, where all show `cache_creation` and none show `cache_read`;
  - price the loss as (N−1)·prefix·(write−read);
  - recommend a stagger (GitHub matrix `max-parallel`, or a first-token gate in the harness);
  - never recommend pre-warming for W2–W9.
- **Sources.**
  - bundled `shared/prompt-caching.md` (§Concurrent-request timing; §Pre-warming)
  - https://code.claude.com/docs/en/prompt-caching (workflow fan-outs)
  - https://platform.claude.com/docs/en/build-with-claude/batch-processing.md (accessed 2026-09-23)
- **Evidence:** strong. **Savings:** (N−1)/N of the prefix-write premium on each burst. **Effort:** S.

### F8. Batch and flex: 50% off single-shot automated work, with caveats (`batch-flex-50`)

- **Facts.**
  - **Anthropic Batch.**
    - Discount: "50% cost reduction on all token usage".
    - Timing: "most batches finishing in less than 1 hour"; results after completion or 24 h; expiry at 24 h; results kept 29 days.
    - Limits: 100,000 requests or 256 MB per batch.
    - Caching: stacks with caching, but "cache hits are provided on a best-effort basis… 30% to 98%". Anthropic suggests the 1-hour cache for shared context.
    - Unsupported: `stream`, `speed` (fast mode) and `max_tokens: 0`.
    - Batches may overshoot a workspace spend limit.
  - **Managed Agents:** "Batch API discount — Sessions are stateful and interactive. There is no batch mode."
  - **OpenAI.** Batch is "50% cost discount", within 24 h. It supports `/v1/embeddings`, with a separate higher rate-limit pool. Flex is priced "at Batch API rates, with additional discounts from prompt caching" and suits "model evaluations, data enrichment, and asynchronous workloads". Under capacity limits it returns 429 "Resource Unavailable", which is not charged.
  - **Bedrock.** Batch inference is 50% below on-demand for select models. Flex tier is at a 50% discount; Priority is at a 75% premium.
  - **Gemini.** Batch is 50% (for example Gemini Embedding 2 text: $0.20 → $0.10 per MTok).
  - **Voyage.** Batch is a 33% discount with a 12-hour window.
- **Implication.**
  - W6 (evals and judges), W7, W8 and single-shot W4 are batch-eligible.
  - W2, W3 and W5 tool loops are not, unless flattened. Flattening "changes how the model reasons", per the bundled doc.
- **Token Bill should:**
  - classify every automated call as batch-eligible when all hold: single-shot (no tool_use continuation), no human-waiting signal, and latency slack above 1 h from workflow deadlines;
  - price the move at the provider's batch rate;
  - model batch cache hits as a 30–98% band, not a point estimate;
  - flag Managed Agents deployments as ineligible;
  - read `service_tier` from usage reports to show what is already batched.
- **Sources.**
  - https://platform.claude.com/docs/en/build-with-claude/batch-processing.md (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/about-claude/pricing.md (accessed 2026-09-23)
  - https://developers.openai.com/api/docs/guides/batch (accessed 2026-09-23)
  - https://developers.openai.com/api/docs/guides/flex-processing (accessed 2026-09-23)
  - https://aws.amazon.com/bedrock/pricing/ (accessed 2026-09-23)
  - https://ai.google.dev/gemini-api/docs/pricing (updated 2026-09-23)
  - https://docs.voyageai.com/docs/batch-inference (accessed 2026-09-23)
- **Evidence:** strong. **Savings:** 50% (33% Voyage) of eligible spend. **Effort:** M.

### F9. Caps are damage limits; savings come from model-visible budgets and effort (`ci-caps-budgets`)

- **Facts.**
  - `--max-budget-usd`: "Maximum dollar amount to spend on API calls before stopping (print mode only). Spend from subagents counts toward the cap." It stops background subagents at the cap (v2.1.217+).
  - `--max-turns`: "Exits with an error when the limit is reached. No limit by default."
  - The GitHub Action docs list the cost controls: `--max-turns`, workflow-level timeouts, concurrency controls, a concise CLAUDE.md, and specific prompts.
  - Managed Agents deployments accept a per-session `budget` copied onto each fired session.
  - Code Review has a monthly spend cap; workspaces have spend limits.
  - Tail concentration: in WideSearch (20 problems) the "top 2 problems carried 43% of total spend".
  - Run-to-run token variance in agentic coding: "up to 30x" (arXiv 2604.22750). Models' self-predictions of their own usage correlate at ≤0.39.
  - Model-visible task budgets (Fable 5.1, SWE-bench Pro): 44% cost reduction for ~3 points, 58% for ~6 points.
  - Anthropic warns that a tight `max_tokens` cap "bought proportionally fewer solves". Hard truncation is not a savings lever.
  - All client-side dollar figures (`total_cost_usd`, `--max-budget-usd`) are list-price estimates unless `modelPricing` is set.
- **Token Bill should:**
  - compute per-workflow p50, p95 and p99 of $ per run and turns per run, and emit recommended `--max-turns` and `--max-budget-usd` at p99, with a projection of how many historical runs each would have cut;
  - separately, recommend effort or task-budget changes as the actual savings lever, gated on an eval;
  - reconcile client estimates against the usage and cost API.
- **Sources.**
  - https://code.claude.com/docs/en/cli-reference
  - https://code.claude.com/docs/en/github-actions
  - bundled `shared/managed-agents-scheduled-deployments.md`
  - https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md
  - https://arxiv.org/abs/2604.22750 (2026-04-24)
  - https://code.claude.com/docs/en/agent-sdk/cost-tracking
- **Evidence:** strong. **Savings:** caps give tail protection; task budgets 44–58% at a quality cost. **Effort:** S.

### F10. Headless runs: `--bare`, JSON cost output, and the action's execution file are the ingest surface (`ci-headless-ingest`)

- **Facts.**
  - `--bare` "skip[s] auto-discovery of hooks, skills, custom commands, subagents, plugins, MCP servers, auto memory, and CLAUDE.md". Anthropic calls it "the recommended mode for scripted and SDK calls", and says it "will become the default for `-p` in a future release".
  - `--output-format json` returns `total_cost_usd` with a per-model breakdown. `stream-json` gives per-step usage; deduplicate by message id, and per-step `output_tokens` is a placeholder.
  - `claude-code-action` writes all SDK messages to `$RUNNER_TEMP/claude-execution-output.json`, exposed as output `execution_file`. The log output is sanitized to `total_cost_usd` and `num_turns`, while the file holds the full messages.
  - `--no-session-persistence` avoids writing transcripts in print mode.
- **Token Bill should:**
  - ship a `tokenbill ingest claude-code-action <execution_file>` path and an `-p --output-format stream-json` path that map to the canonical call record, including subagent `parent_tool_use_id`;
  - provide a 5-line GitHub step that uploads the execution file as an artifact;
  - recommend `--bare`, with CLAUDE.md passed explicitly via `--append-system-prompt-file`, for W4 jobs to keep the prefix small and deterministic.
- **Sources.**
  - https://code.claude.com/docs/en/headless (accessed 2026-09-23)
  - https://code.claude.com/docs/en/cli-reference
  - https://code.claude.com/docs/en/agent-sdk/cost-tracking
  - https://github.com/anthropics/claude-code-action (commit 46a42b4, 2026-09-23; `base-action/src/execution-file.ts`, `run-claude-sdk.ts`)
- **Evidence:** strong (mechanism). **Savings:** enabling, plus smaller prefixes. **Effort:** S.

### F11. Telemetry signatures that separate automated from interactive traffic (`detect-signatures`)

- **Facts.** See §2.2. The most reliable signals are:
  - `CLAUDE_CODE_ENTRYPOINT=claude-code-github-action` (hard-coded by the action);
  - OTel `app.entrypoint` (`cli`, `sdk-cli`, `sdk-ts`, `sdk-py`, `claude-vscode`), which is **off by default**;
  - Analytics API `api_actor` + `api_key_name`;
  - WIF service accounts (`svac_…`) bound to a GitHub OIDC subject;
  - usage report `service_tier`, `api_key_id` and `workspace_id`;
  - GitHub env vars propagated via `OTEL_RESOURCE_ATTRIBUTES`;
  - the GitHub billing `workflow_path` for the Copilot reviewer.
- **Negative results.**
  - There is **no** `prompt.source` or non-interactive flag on `claude_code.user_prompt`. `-p` prompts still look like user prompts.
  - The usage report does not show a service-account dimension (only API key and workspace). Map `svac_` identities to workspaces instead.
- **Token Bill should:**
  - ship an "attribution kit": recommended `OTEL_METRICS_INCLUDE_ENTRYPOINT=true`, plus `OTEL_RESOURCE_ATTRIBUTES` with `workload`, `repo`, `workflow`, `run_id`, `run_attempt`, `sha`, `pr`, `cost_center`;
  - add a lint that warns when automated traffic arrives without them;
  - recommend one key or service account per workload class.
- **Sources.**
  - https://code.claude.com/docs/en/monitoring-usage (accessed 2026-09-23)
  - https://github.com/anthropics/claude-code-action
  - https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api.md (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/manage-claude/wif-admin-api.md (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/manage-claude/usage-cost-api.md (accessed 2026-09-23)
  - https://docs.github.com/en/actions/reference/workflows-and-actions/variables (accessed 2026-09-23)
  - https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
- **Evidence:** strong. **Savings:** enabling. **Effort:** M.

### F12. Scheduled agents: full context every fire, jitter, TTL, and no batch (`sched-cadence-ttl`)

- **Facts.**
  - **Claude Code `/loop` and scheduled tasks.**
    - A scheduled task "fires on its interval even while the session is idle, sending your full context each time".
    - Recurring tasks expire after 7 days.
    - Jitter: up to 30 minutes, or half the interval for sub-hourly tasks. The offset is deterministic per task.
    - Up to 50 tasks per session.
    - `/usage` "Loops" rows show fires, per-run tokens and totals (v2.1.242+).
  - **Routines:** minimum interval 1 hour; a daily run cap per account; each GitHub event creates a new session with no reuse.
  - **Managed Agents scheduled deployments.**
    - Jitter up to 15% of the interval, floored at 5 seconds and capped at 9 minutes.
    - Maximum 1,000 deployments per organization.
    - Rate-limited fires are recorded, not retried.
    - An optional per-session `budget`.
    - Pricing: tokens at list price with no batch discount, plus $0.08 per session-hour.
  - **GitHub `schedule`.**
    - The minimum interval is 5 minutes.
    - Runs are delayed at high load ("the start of every hour"), and jobs can be dropped.
    - In a public repo the schedule is disabled after 60 days of inactivity.
- **Illustrative (Opus 5, 150k-token context).**
  - A fire inside the cache TTL reads about $0.075. A fire after TTL expiry re-writes about $0.94 at the 5-minute write price.
  - A 5-minute loop costs about $21.6 a day. An hourly loop on an API key with a 5-minute TTL costs about $22.5 a day.
  - Either can run up to 7 days before expiry.
- **Token Bill should:**
  - detect periodic series with jitter tolerance;
  - compute $ per fire, $ per day and $ over the remaining lifetime;
  - flag cadence > TTL (recommend 1 h TTL, a keep-alive, or a shorter context);
  - flag **no-op fires**, where the output says "nothing to do" or no tool writes or commits happen;
  - flag scheduled Managed Agents with no `budget`.
- **Sources.**
  - https://code.claude.com/docs/en/scheduled-tasks (accessed 2026-09-23)
  - https://code.claude.com/docs/en/costs
  - https://code.claude.com/docs/en/routines (accessed 2026-09-23)
  - bundled `shared/managed-agents-scheduled-deployments.md`
  - https://platform.claude.com/docs/en/about-claude/pricing.md
  - https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows (accessed 2026-09-23)
- **Evidence:** strong (mechanism). **Savings:** up to ~12× per fire when a TTL miss becomes a hit (0.94 vs 0.075); no-op fires are 100% waste. **Effort:** M.

### F13. Replace polling with events (`sched-event-driven`)

- **Facts.**
  - Anthropic: "To react to events as they happen instead of polling, see Channels: your CI can push the failure into the session directly."
  - The Monitor tool "avoids polling altogether and is often more token-efficient and responsive than re-running a prompt on an interval".
  - Routines accept API and GitHub triggers as alternatives to schedules.
- **Token Bill should** flag polling loops whose fires mostly produce no state change, and recommend an event trigger with the projected fire reduction.
- **Sources.**
  - https://code.claude.com/docs/en/scheduled-tasks
  - https://code.claude.com/docs/en/routines
- **Evidence:** moderate (no measured number). **Savings:** proportional to the no-op fire rate. **Effort:** S.

### F14. Content-keyed result caching and re-run dedupe (`ci-result-cache`)

- **Facts.**
  - promptfoo caches LLM responses by default, keyed on provider, a deterministic request digest, provider config and variables. The default TTL is 14 days and the cache lives on disk. Its GitHub Action example persists the cache with `actions/cache`, and its `paths: ['prompts/**']` filter runs evals only when prompts change.
  - GitHub keeps `GITHUB_RUN_ID` constant across re-runs and increments `GITHUB_RUN_ATTEMPT`.
  - Anthropic's review skill skips PRs "that already have a comment from Claude".
- **Implication.** For review and prompt-regression workloads, an identical (diff hash, prompt or skill version, model, effort) tuple should never be re-billed. Examples: re-run attempts; a force-push that doesn't change the diff; a rebase onto an unchanged base.
- **Token Bill should:**
  - detect duplicate-work runs (same SHA or diff hash plus the same config);
  - price them;
  - ship a tiny cache-key helper, `tokenbill ci-key`, that workflows can use with `actions/cache` to skip the agent step.
- **Sources.**
  - https://www.promptfoo.dev/docs/configuration/caching/ (accessed 2026-09-23)
  - https://www.promptfoo.dev/docs/integrations/github-action/ (accessed 2026-09-23)
  - https://docs.github.com/en/actions/reference/workflows-and-actions/variables
  - https://code.claude.com/docs/en/github-actions
- **Evidence:** moderate. **Savings:** 100% of duplicate runs; the frequency is unknown until measured. **Effort:** S.

### F15. Eval item selection (IRT, anchor points, adaptive) cuts eval cost 50–1000× (`eval-subset-irt`)

- **Facts.**
  - **tinyBenchmarks** (ICML 2024, arXiv 2402.14992): MMLU 14K → 100 curated items. Tools and tiny versions are released for the Open LLM Leaderboard, MMLU, HELM and AlpacaEval 2.0.
  - **Anchor Points** (EACL 2024, arXiv 2309.08638): 1–30 anchor points rank models better than uniform sampling across 87 model-prompt pairs.
  - **Efficient Benchmarking** (NAACL 2024, arXiv 2308.11696): HELM compute cut "by x100 or more" with minimal loss of reliability.
  - **metabench** (ICLR 2025, arXiv 2407.12844): under 3% of 28,632 items; 1.24% RMSE per benchmark, 0.58% for the total.
  - **Fluid Benchmarking** (COLM 2025, arXiv 2509.11106): adaptive IRT item selection with "higher validity and less variance on MMLU with fifty times fewer items".
  - **Amortized model-based evaluation** (arXiv 2503.13335, 2025): predicts difficulty from content for adaptive testing across 22 benchmarks and 172 LMs.
  - **PromptEval** (NeurIPS 2024, arXiv 2405.17202): performance quantiles over 100 prompt templates for the budget of two single-prompt evaluations.
  - **Sort & Search** (arXiv 2402.19472): ~1000× lower compute (180 GPU-days → 5 GPU-hours) for lifelong evaluation.
- **Caveat.** These results are for static benchmarks with many models. For an enterprise's private agent evals, Token Bill should re-validate subset fidelity: rank correlation and absolute error of subset vs full, on scheduled full runs.
- **Token Bill should:**
  - ship an "eval sizer" that reads per-case results from past runs;
  - rank cases by discriminative signal (cross-rep variance, disagreement, IRT discrimination);
  - propose a PR-gate subset plus a nightly or weekly full run;
  - track subset-vs-full drift.
- **Sources.** arXiv abstracts opened: 2402.14992, 2309.08638, 2308.11696, 2407.12844, 2509.11106, 2503.13335, 2405.17202, 2402.19472 (dates in §11).
- **Evidence:** strong. **Savings:** 50–140× fewer items on public benchmarks; unknown for private evals. **Effort:** M.

### F16. Size trials to the decision; use paired and clustered statistics (`eval-stats-trials`)

- **Facts.**
  - **Anthropic statistical approach** (2024-11-19; paper arXiv 2411.00640):
    - report the SEM;
    - cluster standard errors, which "can be over three times as large" as naive ones;
    - resample answers per question to reduce within-question variance;
    - use paired differences, since frontier models' per-question scores correlate at 0.3–0.7;
    - run a power analysis to choose the question count.
  - **Bundled eval-audit rule:** noise floor ≈ `1/sqrt(n·R)`, so 25 cases × 2 reps ≈ ±14 points and 100 × 2 ≈ ±7. The production-cutover bar is "around fifty cases and at least five trials".
  - **Artificial Analysis** uses 1–5 repeats per benchmark to hold a 95% CI under ±1% on its index.
  - **Signal and Noise** (arXiv 2508.13144): filter noisy subtasks and prefer metrics with better signal-to-noise.
  - **Anthropic agent-evals guide** (2026-01-09): run multiple trials; choose pass@k or pass^k by use case; start with 20–50 tasks.
- **Illustrative.** 100 cases × 5 reps gives a noise floor of about ±4.5 points. Running 500 × 5 costs 5× more for a ±2 point floor. Most PR-gate decisions do not need ±2.
- **Token Bill should:**
  - compute each eval's realized noise floor, compare it with the effect size the team acts on, and flag **over-powered** evals (paying for precision nobody uses) and **under-powered** ones (paying for noise);
  - report $ per decision.
- **Sources.**
  - https://www.anthropic.com/research/statistical-approach-to-model-evals (2024-11-19)
  - https://arxiv.org/abs/2411.00640 (2024-11-01)
  - https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents (2026-01-09)
  - https://artificialanalysis.ai/methodology/intelligence-benchmarking (accessed 2026-09-23)
  - https://arxiv.org/abs/2508.13144 (2025-08-18)
  - bundled `shared/evals/eval-audit.md`, `shared/cost-optimization.md`
- **Evidence:** strong. **Savings:** right-sizing reps and cases, often 2–5× on over-powered suites (illustrative). **Effort:** M.

### F17. Judge-model choice: cheap judges for ranking, strong judges for absolute scores (`eval-judge-choice`)

- **Facts.**
  - The bundled build-eval doc says `claude-haiku-4-5` "is cheap and fast enough to run on every PR", `claude-sonnet-5` is a balanced middle, and `claude-opus-5` is for nuanced criteria.
  - Don't use the model under test as its own judge.
  - Prefer programmatic or end-state checks for agents.
  - Record judge usage separately, "otherwise up to half the real cost is invisible".
  - **PoLL** (arXiv 2404.18796): a panel of smaller models outperforms a single GPT-4 judge "while being over seven times less expensive".
  - **Trust or Escalate** (arXiv 2407.18370): cascaded cheap-to-strong judges with a guaranteed >80% human agreement at about 80% coverage.
  - **Judging the Judges** (arXiv 2406.12624): "only the best (and largest) models achieve reasonable alignment with humans". Smaller models and lexical metrics "may provide a reasonable signal" for ranking.
  - Anthropic's agent-evals guide: code-based graders are "Fast", "Cheap", "Objective"; model graders are "More expensive than code".
- **Token Bill should:**
  - tag judge calls;
  - report judge share of eval spend;
  - recommend Haiku or panel judges for pairwise and ranking gates, with a periodic calibration sample against the strong judge;
  - flag evals whose judge costs more than the model under test.
- **Sources.**
  - bundled `shared/evals/build-eval.md`
  - https://arxiv.org/abs/2404.18796 (2024-04-29)
  - https://arxiv.org/abs/2407.18370 (2024-07-25)
  - https://arxiv.org/abs/2406.12624 (2024-06-18; rev. 2025-08-18)
  - https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- **Evidence:** moderate. **Savings:** Opus 5 → Haiku 4.5 judge ≈ 5× per judge token at list price; PoLL >7×. **Effort:** S.

### F18. Batch the judges (and single-shot cases); packing is secondary (`eval-batch-judges`)

- **Facts.**
  - The Anthropic Batch docs list "running large-scale evaluations" as a core use case, and OpenAI Flex names "model evaluations".
  - Batch prompting (EMNLP 2023 Industry, arXiv 2301.08721): "up to 5x" token and time savings with 6 samples per call under few-shot prompting. That saving comes from amortizing shared demos, which prompt caching now reprices at 0.1×.
- **Illustrative.** 10,000 judge calls at 3k input + 300 output tokens:
  - Opus 5 costs $225 sync or $112.5 batch.
  - Haiku 4.5 costs $45 sync or $22.5 batch.
  - Judge choice plus batch together is **10×**.
- **Token Bill should** mark judge calls and single-shot eval cases as batch-eligible and simulate the batch bill with a 30–98% cache band.
- **Sources.**
  - https://platform.claude.com/docs/en/build-with-claude/batch-processing.md
  - https://developers.openai.com/api/docs/guides/flex-processing
  - https://arxiv.org/abs/2301.08721 (2023-01-19)
- **Evidence:** moderate. **Savings:** 50% of judge spend (batch), and up to ~10× combined with a judge downgrade. **Effort:** S.

### F19. Agentic eval runs: cost per solved task, cheap-first then escalate (`eval-rerun-failures`)

- **Facts.**
  - Anthropic, SWE-bench Pro with Opus 5: fixed effort costs ~$0.93 per task at 91.7%. Low effort plus a re-run of failures at the default costs ~$0.45 per task at ~93%.
  - Fable 5.1 at low effort cost $0.54 per *solved* task, against $0.84 for Sonnet 5 at default, despite a 5× higher per-token price.
  - HAL: ~$40k for 21,730 rollouts (≈$1.84 each). Higher reasoning effort *reduced* accuracy in most runs.
  - "AI Agents That Matter" (arXiv 2407.01502) calls for cost-controlled evaluation and joint cost/accuracy optimization.
- **Illustrative.** A 500-case × 5-rep agent eval at $0.93 per task costs $2,325 per configuration. A 100-case subset at $0.45 per task (cheap-first) costs $225, about **10× less**.
- **Token Bill should:**
  - report eval spend as $ per solved task and $ per config;
  - recommend cheap-first plus escalate when a checker exists;
  - plot the Pareto frontier of score against $ per task.
- **Sources.**
  - https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md
  - https://arxiv.org/abs/2510.11977 (2025-10-13)
  - https://arxiv.org/abs/2407.01502 (2024-07-01)
- **Evidence:** strong. **Savings:** ~50% at the same pass rate (Anthropic). **Effort:** M.

### F20. Dev/test bleed: isolate by key or workspace, cap, and estimate cost in CI (`dev-test-bleed`)

- **Facts.**
  - FinOps "Tokenomics" (2026-06-03): "Development and testing bleed: developer testing in production environments or non-production accounts without spend controls is a common source of anomalous spend that has no business value."
  - It recommends key scoping, workload-level budget alerts, and "Integrate AI cost estimation into the CI/CD pipeline."
  - Its levers table: batch 50%, caching 50–90% of cached tokens, model right-sizing 60–90%.
  - Claude Console creates a dedicated "Claude Code" workspace with its own spend and rate limits.
- **Token Bill should:**
  - flag automated traffic on production keys;
  - flag non-production keys without spend limits;
  - flag eval and test traffic on frontier models where the eval does not require them;
  - produce a pre-merge "estimated $ impact" for workflow and prompt changes (the FinOps CI/CD integration item).
- **Sources.**
  - https://www.finops.org/wg/token-economics-saas/ (2026-06-03)
  - https://code.claude.com/docs/en/costs
- **Evidence:** moderate. **Savings:** unknown; anomaly-class spend. **Effort:** S–M.

### F21. Embedding prices in 2026, with batch (`emb-prices-2026`)

| Provider / model | Standard $/MTok | Batch | Notes | Source |
|---|---|---|---|---|
| OpenAI text-embedding-3-small | $0.02 | Batch API "50% cost discount" and supports `/v1/embeddings` (no separate row on the model page) | 1536 dims default | developers.openai.com pricing, embeddings, batch |
| OpenAI text-embedding-3-large | $0.13 | same | 3072 dims default | same |
| Voyage voyage-4-lite / voyage-4 / voyage-4-large | $0.02 / $0.06 / $0.12 (200M free tokens each) | **33%** discount, 12 h window; free tokens don't apply | voyage-code-4 and voyage-context-4 $0.12; rerank-3 $0.05, rerank-3-lite $0.02 | docs.voyageai.com pricing |
| Gemini Embedding 2 | text $0.20 | text $0.10 (50%) | multimodal (image $0.45, audio $6.50, video $12.00 per MTok standard) | ai.google.dev pricing (updated 2026-09-23) |
| Cohere Embed v4 | **not publicly listed** on the pages opened (Model Vault hourly pricing only) | Embed v4 "does not support Embed Jobs" | 256–1536 dims, 128k context | cohere.com/pricing; docs.cohere.com |
| Bedrock Titan Text Embeddings V2 / Cohere on Bedrock | **not confirmed** (the price rows were not visible) | Bedrock batch 50% for select models; Flex tier 50% off | Titan V2 dims 256/512/1024 | aws.amazon.com/bedrock/pricing |

- **Implication.** The cheapest embeddings cost 100× less per token than Sonnet 5 input ($2) and 500× less than its output ($10).
- **Evidence:** strong for the confirmed rows; Cohere and Bedrock prices unknown. **Effort:** S.

### F22. Re-embedding churn: content-hash incremental indexing (`emb-incremental-hash`)

- **Facts.**
  - **LlamaIndex** ingestion pipeline:
    - each node and transformation combination "is hashed and cached";
    - with a docstore, "If a duplicate `doc_id` is detected and the hash is unchanged, the node is skipped", and a changed hash triggers a re-process and upsert.
  - **Cursor** (2026-01-27):
    - a Merkle tree re-hashes only edited files and their parent directories;
    - it "caches embeddings by chunk content. Unchanged chunks hit the cache";
    - "clones of the same codebase average 92% similarity across users within an organization";
    - index reuse cut median time-to-first-query from 7.87 s to 525 ms, and p99 from 4.03 h to 21 s.
- **Illustrative.**
  - A full nightly re-index of a 5B-token corpus costs $100 a day on text-embedding-3-small and $600 on voyage-4-large, or $3k–18k a month.
  - Assuming 2% daily churn (an assumption), incremental indexing costs $60–360 a month, **50× less**.
- **Token Bill should:**
  - record embedding calls with a content hash of each input (hash only; no text retained);
  - report the **re-embedded-unchanged share** and its dollars;
  - flag full re-index jobs.
- **Sources.**
  - https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/ (accessed 2026-09-23)
  - https://cursor.com/blog/secure-codebase-indexing (2026-01-27)
- **Evidence:** strong (mechanism); the saving depends on churn. **Effort:** M.

### F23. Dimension truncation (MRL) and quantization: storage and quality numbers (`emb-dims-quant`)

- **Facts.**
  - **Matryoshka Representation Learning** (arXiv 2205.13147): "up to 14x smaller embedding size… at the same level of accuracy" on ImageNet-1K; up to 14× retrieval speed-ups.
  - **Gemini gemini-embedding-001 MTEB by dimension:**

    | Dimensions | 2048 | 1536 | 768 | 512 | 256 | 128 |
    |---|---|---|---|---|---|---|
    | MTEB | 68.16 | 68.17 | 67.99 | 67.55 | 66.19 | 63.31 |

    Google recommends 768, 1536 or 3072.
  - **OpenAI:** text-embedding-3-large shortened to 256 dims "still outperform[s]" ada-002 at 1536.
  - **Titan V2** (2024-04-30): 512 dims keep "approximately 99 percent" of the accuracy of 1024; 256 keep 97%, with a "75 percent" storage saving.
  - **Quantization** (Hugging Face, 2024-03-22): binary is 32× smaller and ~24.8× faster, keeping ~92.5% of performance, or ~96% with rescoring. int8 is 4× smaller and ~3.66× faster, keeping ~99% with rescoring.
  - **Voyage** `output_dtype` int8, uint8, binary and ubinary give "4x or 32x" reductions.
- **Illustrative.** 10M vectors at 1024 dims take 41 GB as float32, 10 GB as int8, 1.3 GB as binary, and 10 GB as float32 at 256 dims.
- **Token Bill should** provide a storage and quality calculator and flag indexes stored at full float32 width with no measured need.
- **Sources.**
  - https://arxiv.org/abs/2205.13147 (2022-05-26; rev. 2024-02-08)
  - https://ai.google.dev/gemini-api/docs/embeddings (updated 2026-09-17)
  - https://developers.openai.com/api/docs/guides/embeddings (accessed 2026-09-23)
  - https://aws.amazon.com/blogs/aws/amazon-titan-text-v2-now-available-in-amazon-bedrock-optimized-for-improving-rag/ (2024-04-30)
  - https://huggingface.co/blog/embedding-quantization (2024-03-22)
  - https://docs.voyageai.com/docs/flexible-dimensions-and-quantization (accessed 2026-09-23)
- **Evidence:** strong. **Savings:** vector storage 4–32×; embedding API $ unchanged by dims. **Effort:** S.

### F24. Shared-embedding-space model families avoid re-indexing and cheapen queries (`emb-shared-space`)

- **Facts.** Voyage: "All embeddings created with the 4 series are compatible with each other". voyage-4-large, voyage-4 and voyage-4-lite share one space. Their list prices are $0.12, $0.06 and $0.02.
- **Implication.**
  - Index documents once with `-large`, then embed queries with `-lite`, which is 6× cheaper per query token.
  - Moving between tiers does not force a full re-embed.
- **Token Bill should** detect mixed-family deployments and price the index-large and query-lite split.
- **Sources.**
  - https://docs.voyageai.com/docs/embeddings (accessed 2026-09-23)
  - https://docs.voyageai.com/docs/pricing
- **Evidence:** moderate (vendor claim; quality impact of asymmetric use not quantified). **Savings:** up to 6× on query embedding. **Effort:** S.

### F25. Embeddings module scope decision: IN, as P2 "ledger + hygiene", not a vector-DB optimizer (`emb-scope-decision`)

See §8 for the full rationale. In short:

- Embeddings are cheap per token and small next to agentic CI.
- They still need a ledger so their share is known.
- Churn and storage waste are real and easy to detect.
- Token Bill already has the needed primitives (canonical hashing, JSONL).

**Evidence:** moderate (decision). **Effort:** M.

### F26. Unit metrics for automated work (`auto-unit-metrics`)

- **Facts.**
  - FinOps recommends cost per query, per user per month, **per successful outcome** and per business transaction.
  - Anthropic Code Review analytics show PRs reviewed, weekly cost, auto-resolved comments, and per-repo average cost per review.
  - Anthropic stresses "cost per completed task, not cost per token".
- **Token Bill should** implement the metric specs in §5 as named columns and CI outputs.
- **Sources.**
  - https://www.finops.org/wg/token-economics-saas/
  - https://code.claude.com/docs/en/code-review
  - bundled `shared/cost-optimization.md`
- **Evidence:** moderate. **Effort:** M.

---

## 5. Metric specs

All metrics come from the canonical call record. Billed usage is authoritative; client estimates are labelled.

Proposed new fields on every call:

- `workload_class`, `workload_confidence`
- `ci.provider`, `repo`, `workflow_ref`, `run_id`, `run_attempt`, `sha`, `pr_number`, `event_name`
- `schedule_id` / `deployment_id` / `routine_id`, `fire_ts_expected`
- `eval_run_id`, `case_id`, `rep`, `is_judge`
- `service_tier`, `is_embedding`, `content_hash`

### 5.1 $ per CI run

- **Definition.** Sum of billed cost over all calls sharing (`ci.provider`, `repo`, `workflow_ref`, `run_id`, `run_attempt`), including subagents.
  - Report `run_attempt>1` both separately and rolled up to `run_id`.
- **Components.**
  - uncached input, cache write 5m and 1h, cache read, output, and server tools (web search $10 per 1k);
  - Managed Agents runtime ($0.08 per hour) where applicable;
  - optionally, runner minutes.
- **Diagnostics per run:**
  - turns;
  - cold-prefix write $ on the first call;
  - cache hit %;
  - stop reason and outcome (`success` | `error_max_turns` | `error_max_budget_usd` | `timeout` | `cancelled` | `failed`);
  - `superseded` (a newer SHA started on the same PR before this run ended);
  - `duplicate_of` (same diff hash and config).
- **Aggregates.** p50, p95 and p99 per workflow; $ per successful run (total $ / successful runs, so failed runs are charged to successes).

### 5.2 $ per reviewed PR

- **Definition.** Sum over all review-class runs (W3) attached to a PR across its lifetime: every push, re-run, manual `@claude review`, triage call and managed-review charge. Divide by PRs that received ≥1 completed review.
- **Companion metrics:**
  - review runs per PR (the trigger multiplier);
  - **$ per addressed finding** = review $ / findings addressed (thread resolved, or code change on the hunk within N commits);
  - address rate;
  - $ per merged PR;
  - share of review $ on drafts, bots, lockfile-only diffs and superseded runs (the waste split).

### 5.3 $ per eval run

- **Definition.** Sum over all calls with the same `eval_run_id`:
  - model under test, split into first attempts and retries;
  - judge (`is_judge`);
  - failed or harness-error attempts, which still count.
- **Companion metrics:**
  - $ per case-rep;
  - judge share %;
  - batch share %;
  - realized noise floor (paired 95% CI half-width at n·R);
  - **$ per decision** (eval $ / keep-or-revert decisions made);
  - subset fidelity (rank correlation and MAE vs the last full run) when a subset is used;
  - $ per solved task for agentic evals.

### 5.4 Also: $ per scheduled fire and no-op rate

- **$ per fire.** Cost of a scheduled fire, plus $ per day and $ over the remaining lifetime (7-day `/loop` expiry, deployment until paused).
- **No-op rate.** The share of fires with no state change: no commit, PR, comment, tool write or non-trivial output.

---

## 6. Reference CI budget and gating config

These are **starting defaults** to be replaced by each organization's p95 after 2–4 weeks of Token Bill measurement. The anchors are the vendor ranges in §1.2. The numbers are proposals, not measured optima.

### 6.1 Policy file (`.tokenbill/ci-policy.yml`, proposed)

```yaml
version: 1
pricing_basis: contracted            # list | contracted (use modelPricing / invoice reconciliation)
workspaces:
  ci_shared: wrkspc_CI_AGENTS        # one workspace for all CI agent traffic -> shared prompt cache (F6)
budgets:                             # per-run hard caps (damage limits, F9); set to org p99 after baseline
  ci.triage:       { model: claude-haiku-4-5,  max_usd: 0.25, max_turns: 3 }
  ci.review.lite:  { model: claude-sonnet-5,   effort: medium, max_usd: 2.00,  max_turns: 15 }
  ci.review.deep:  { model: claude-opus-5,     effort: high,   max_usd: 25.00, max_turns: 60 }
  ci.issue_to_pr:  { max_usd: 10.00, max_turns: 50 }
  sched.default:   { max_usd: 5.00,  max_turns: 30, require_event_trigger_if_noop_rate_over: 0.5 }
  eval.pr_gate:    { max_usd: 20.00, cases: 50, reps: 3, judge: claude-haiku-4-5, tier: batch_if_slack_gt_1h }
  eval.nightly:    { max_usd: 250.00, cases: all, reps: 5, tier: batch }
monthly_caps:                        # org-level backstops; mirror in Console workspace spend limits and Code Review cap
  ci_total_usd: 50000
  alert_at_pct: [50, 80, 100]
tiering:                             # F5 risk tiers
  skip:
    only_paths: ["**/*.lock", "**/package-lock.json", "docs/**", "**/*.md", "**/*.svg", "**/gen/**", "vendor/**"]
    authors: ["dependabot[bot]", "renovate[bot]"]
    max_changed_lines: 3
    drafts: true
  deep:
    paths: ["src/auth/**", "**/migrations/**", "infra/**", "**/security/**"]
    labels: ["security", "needs-deep-review"]
    min_changed_lines: 400
  lite: default
triggers:                            # F3
  review_on: [opened, ready_for_review]
  rereview: manual                   # "@claude review" (opt-in); no auto review on synchronize
  cancel_superseded: true
  dedupe_key: [diff_sha256, skill_version, model, effort]   # F14
cache:                               # F6/F7
  exclude_dynamic_system_prompt_sections: required
  pin_claude_code_version: required
  prompt_cache_ttl: auto             # 1h if median inter-run gap in ci_shared is 5-60 min, else 5m
  stagger_fanout: true               # first-token gate / max-parallel
gates:
  on_run_over_budget: warn           # warn | fail
  on_missing_attribution: warn       # OTEL_RESOURCE_ATTRIBUTES / entrypoint not set (F11)
  on_policy_violation: [review_on_synchronize, no_path_filter, dynamic_sections_in_prefix]
```

### 6.2 Workflow (GitHub Actions, reference)

Flag names were verified against the docs opened. Wherever a flag passes through `claude_args`, confirm it against your pinned action version: the action docs say `claude_args` "accepts any Claude Code CLI argument".

```yaml
name: ai-review
on:
  pull_request:
    types: [opened, ready_for_review]              # not 'synchronize' (F3)
    paths-ignore: ["**/*.lock", "docs/**", "**/*.md", "**/*.svg", "vendor/**"]
  issue_comment:
    types: [created]                               # opt-in re-review via "@claude review"
concurrency:
  group: ai-review-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: true                          # cancel superseded runs (F3)
permissions: { contents: read, pull-requests: write, id-token: write }
jobs:
  triage:
    if: ${{ github.event.pull_request.draft != true && !endsWith(github.actor, '[bot]') }}
    runs-on: ubuntu-latest
    timeout-minutes: 5
    outputs: { tier: ${{ steps.t.outputs.tier }}, model: ${{ steps.t.outputs.model }},
               effort: ${{ steps.t.outputs.effort }}, budget: ${{ steps.t.outputs.budget }},
               key: ${{ steps.t.outputs.key }} }
    steps:
      - uses: actions/checkout@v6
        with: { fetch-depth: 0 }
      - id: t
        run: tokenbill ci-triage --base "origin/${{ github.base_ref }}" --policy .tokenbill/ci-policy.yml >> "$GITHUB_OUTPUT"
  review:
    needs: triage
    if: ${{ needs.triage.outputs.tier != 'skip' }}
    runs-on: ubuntu-latest
    timeout-minutes: 25
    env:
      OTEL_METRICS_INCLUDE_ENTRYPOINT: "true"                       # F11
      OTEL_RESOURCE_ATTRIBUTES: "workload=ci.review.${{ needs.triage.outputs.tier }},repo=${{ github.repository }},run_id=${{ github.run_id }},run_attempt=${{ github.run_attempt }},sha=${{ github.sha }},pr=${{ github.event.pull_request.number }},cost_center=eng-platform"
      CLAUDE_CODE_PROMPT_CACHE_TTL: ${{ vars.CI_PROMPT_CACHE_TTL }}  # '1h' only if inter-run gaps are 5-60 min (F6)
    steps:
      - uses: actions/cache@v4                                      # content-keyed dedupe (F14)
        id: dedupe
        with: { path: .tokenbill/review-done, key: "ai-review-${{ needs.triage.outputs.key }}" }
      - uses: actions/checkout@v6
        if: steps.dedupe.outputs.cache-hit != 'true'
        with: { fetch-depth: 1 }
      - uses: anthropics/claude-code-action@v1                      # pin to a commit SHA; pin Claude Code version fleet-wide
        if: steps.dedupe.outputs.cache-hit != 'true'
        with:
          anthropic_federation_rule_id: ${{ vars.ANTHROPIC_FEDERATION_RULE_ID }}   # WIF, no static key (F11)
          anthropic_organization_id: ${{ vars.ANTHROPIC_ORG_ID }}
          anthropic_workspace_id: ${{ vars.CI_AGENTS_WORKSPACE_ID }}               # one shared CI workspace (F6)
          plugin_marketplaces: "https://github.com/anthropics/claude-code.git"
          plugins: "code-review@claude-code-plugins"
          prompt: "/code-review:code-review --comment ${{ github.repository }}/pull/${{ github.event.pull_request.number }}"
          claude_args: >-
            --model ${{ needs.triage.outputs.model }}
            --effort ${{ needs.triage.outputs.effort }}
            --max-turns 30
            --max-budget-usd ${{ needs.triage.outputs.budget }}
            --exclude-dynamic-system-prompt-sections
            --allowedTools "mcp__github_inline_comment__create_inline_comment"
      - uses: actions/upload-artifact@v4                            # ingest surface (F10)
        if: always()
        with: { name: claude-execution-${{ github.run_id }}-${{ github.run_attempt }}, path: "${{ runner.temp }}/claude-execution-output.json" }
      - if: always()
        run: tokenbill ci-gate --execution-file "${{ runner.temp }}/claude-execution-output.json" --policy .tokenbill/ci-policy.yml --workload "ci.review.${{ needs.triage.outputs.tier }}"
```

**Headless (`claude -p`) variant for W4 jobs:**

```bash
claude --bare -p "…" --append-system-prompt-file .claude/ci-context.md --exclude-dynamic-system-prompt-sections \
  --max-turns 10 --max-budget-usd 1 --output-format json --no-session-persistence
```

- One caveat: `--exclude-dynamic-system-prompt-sections` "only applies with the default system prompt". It is ignored when `--system-prompt` is set.
- `--bare` skips CLAUDE.md, hooks, plugins and MCP, so pass any needed context explicitly.

### 6.3 What `tokenbill ci-gate` should output (spec)

It should print one line and emit JSON for the PR check:

- run $ (billed, or labelled "estimated");
- budget % used;
- cache hit %;
- cold-prefix $;
- turns;
- outcome;
- policy violations;
- the next action, for example: "dynamic sections present in the prefix: add `--exclude-dynamic-system-prompt-sections`, est. $0.23/run".

It should exit non-zero only when a gate is set to `fail`.

---

## 7. Eval-cost playbook (what Token Bill recommends, in order)

1. **Graders.** Use programmatic or end-state checks first. Use a judge only where it is needed (F17).
2. **Judge choice.**
   - Haiku or a panel of judges for PR gates and ranking.
   - A strong judge on a calibration sample and for absolute scores (F17).
3. **Size the suite to the decision.** Compute the noise floor and compare it with the smallest effect the team acts on. Trim to discriminating cases for the loop; run the full set nightly (F15, F16).
4. **Batch or flex** for every single-shot case and every judge call. Use a 1-hour cache for the shared rubric prefix (F8, F18).
5. **Result cache** keyed by (case, prompt version, model, params) for unchanged cases on prompt-regression PRs (F14).
6. **Agentic evals.** Run cheap-first, then re-run failures. Report $ per solved task and the Pareto frontier (F19).
7. **Path filters.** Run prompt-regression evals only when `prompts/**`, `skills/**` or model config change (F14, promptfoo pattern).

---

## 8. Embeddings module: scope decision

**Decision: IN scope, as a P2 lightweight module.** It is an "embedding ledger + hygiene" module, not a vector-database cost optimizer. It ships after the CI and eval modules.

**Rationale.**

1. **Spend magnitude is usually small, but unknown.**
   - Embedding list prices are $0.02–0.20 per MTok (F21), versus $1–10 per MTok for the LLM tokens that drive agent bills.
   - A full re-embed of a 5B-token corpus costs $100–1,000, which is 5–50 Anthropic Code Reviews at $20.
   - So embeddings rarely top the bill. No source quantifies the enterprise share, though, so Token Bill must **measure** it before optimizing. Leaving it out would leave the "total LLM bill" claim incomplete.
2. **The waste is structural and easy to detect:**
   - re-embedding unchanged content, caught by content-hash dedupe (F22);
   - full nightly re-indexes;
   - full-width float32 storage (F23; 4–32× storage);
   - sync calls where batch would do (33–50%, F8).
   - All of these fit Token Bill's existing canonical-hash and JSONL design.
3. **Enterprise credibility.** None of the Claude pricing pages I opened lists an embedding model, so an enterprise that uses Claude for generation buys embeddings from another provider (OpenAI, Voyage, Gemini, Cohere or Bedrock; F21). A "best-in-market" bill optimizer that ignores a whole API category invites the objection "what about our RAG pipeline?"
4. **Out of scope, deliberately:**
   - vector-DB pricing and infrastructure (Pinecone, pgvector);
   - retrieval-quality evaluation beyond reporting published MTEB-by-dimension numbers;
   - re-ranking optimization beyond cost accounting.
   - These are different buyers and different data.

**Module contents (v1):**

- provider price table (§F21), with batch rates and free-tier handling (Voyage 200M);
- ingestion from gateway logs or provider usage exports, capturing model, tokens, dims, dtype, batch flag and a per-input content hash;
- detectors: re-embedded-unchanged share, full re-index jobs, sync-where-batch-possible, float32-at-full-dims, mixed shared-space opportunities (F24);
- a storage calculator (dims × dtype × vectors) that cites the published quality-retention numbers.

---

## 9. What Token Bill should build (prioritized)

**P0: data and classification (weeks 1–4)**

1. **Canonical record extension** with the §5 workload fields. Keep backwards compatibility with `tokenbill/trace@1`.
2. **Ingestors:**
   - `claude-code-action` `execution_file`;
   - `claude -p` `json` / `stream-json` (dedupe by message id; output tokens from the result);
   - Agent SDK result messages;
   - OTLP with resource attributes;
   - Admin usage report (`service_tier`, `api_key_id`, `workspace_id`, 1m/1h buckets);
   - Claude Code Analytics API (`api_actor`);
   - Managed Agents `deployment_runs`;
   - GitHub workflow-run metadata (event, attempt, SHA, PR) and billing usage (`workflow_path`);
   - promptfoo results JSON.
3. **Workload classifier** (§2.3) with confidence and an audit trail.
4. **Headline metrics:** automated share of spend; $ per CI run; $ per reviewed PR; $ per addressed finding; $ per eval run; $ per decision; $ per scheduled fire; no-op rate.

**P1: detectors, simulators and the CI gate (weeks 4–10)**

5. **Trigger-policy replay** over PR event history: once vs every push, manual re-review, drafts, bots, paths, cancel superseded runs, dedupe. Output $ saved per policy (F3, F14).
6. **Cross-run cache breakers for CI:**
   - dynamic system-prompt sections;
   - CLI version drift;
   - workspace split;
   - model or effort drift;
   - TTL vs the gap distribution;
   - fan-out cold writes.
   - Each comes with a replay and dollars recovered. This extends Token Bill's existing 5 breakers (F6, F7).
7. **Batch-eligibility engine** with provider-specific rates and the 30–98% batch-cache band. Excludes Managed Agents and tool loops (F8).
8. **Cap recommender:** p95/p99 per workflow, with a projection of runs cut (F9).
9. **Scheduled-work analyzer:** periodicity with jitter tolerance, cadence vs TTL, no-op fires, missing deployment budgets (F12, F13).
10. **`tokenbill ci-gate` and `ci-triage`**, a GitHub Action or check, and the policy file (§6).

**P1: eval module (weeks 6–12)**

11. **Eval sizer:** noise floor vs decision threshold, discriminating-subset proposal, subset-fidelity tracking, judge share, batch simulation, $ per solved task and the Pareto frontier (F15–F19).

**P2 (weeks 10–16)**

12. **Embeddings ledger and hygiene module** (§8).
13. **Value join:** review-thread outcomes and address rate by tool, tier and repo (F4). Used to recommend manual triggers or tier changes.
14. **Dev/test bleed rules** and a pre-merge "$ impact of this workflow or prompt change" estimate (F20).

**Design principles:**

- **Label every figure** as billed, estimated or simulated.
- **Show savings as ranges** tied to the cited source's conditions.
- **Never recommend a quality-affecting lever** (model, effort, tier, subset, judge) without an eval or a labelled-sample check.
- **Reconcile to the invoice** (usage and cost API) before any chargeback.

---

## 10. Open questions

1. What share of enterprise LLM spend is automated (W2–W9)? No public figure exists. Token Bill should publish anonymized aggregates if customers opt in.
2. Does `--max-budget-usd` (print mode) pass through `claude-code-action`'s `claude_args` → SDK `maxBudgetUsd` in all action versions? The source shows `max-turns` special-cased. Other flags pass as extraArgs; not verified end to end.
3. Does `--exclude-dynamic-system-prompt-sections` apply to `claude-code-action`'s system prompt (default preset vs custom)? The docs say it is ignored with `--system-prompt`.
4. How large is Claude Code's static system prompt plus tool prefix in a typical CI configuration (plugins, MCP)? The only figure found is a local sibling measurement (33–49k median first-call writes).
5. What are Cohere Embed v4 and Bedrock Titan V2 per-token prices in 2026? They were not visible on the pages I opened.
6. Do Anthropic batch requests read cache entries written by synchronous traffic, and vice versa, within a workspace? The docs promise only "best-effort".
7. What is the address rate (F4) for Anthropic Code Review and Copilot Balanced? The study covered OSS GitHub Actions tools up to its date, not these managed products.
8. Can Anthropic usage reports group by WIF service account? Only API key and workspace dimensions were documented.
9. For private agent evals, how well do IRT or anchor subsets preserve rankings? The published results are on static public benchmarks.

---

## 11. Sources (all opened on 2026-09-23 unless a publication date is given)

**Anthropic, Claude Code and Claude API**

- Claude Code GitHub Actions: https://code.claude.com/docs/en/github-actions
- Code Review (managed): https://code.claude.com/docs/en/code-review
- Headless / `claude -p`: https://code.claude.com/docs/en/headless
- CLI reference: https://code.claude.com/docs/en/cli-reference
- Costs: https://code.claude.com/docs/en/costs
- Prompt caching in Claude Code: https://code.claude.com/docs/en/prompt-caching
- Monitoring (OTel): https://code.claude.com/docs/en/monitoring-usage
- Scheduled tasks / `/loop`: https://code.claude.com/docs/en/scheduled-tasks
- Routines: https://code.claude.com/docs/en/routines
- Agent SDK, modifying system prompts: https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts
- Agent SDK, cost tracking: https://code.claude.com/docs/en/agent-sdk/cost-tracking
- anthropics/claude-code-action (cloned at commit 46a42b4, 2026-09-23): https://github.com/anthropics/claude-code-action (`docs/solutions.md`, `docs/configuration.md`, `base-action/src/parse-sdk-options.ts`, `base-action/src/execution-file.ts`)
- Pricing: https://platform.claude.com/docs/en/about-claude/pricing.md
- Batch processing: https://platform.claude.com/docs/en/build-with-claude/batch-processing.md
- Optimizing for cost and intelligence: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md
- Cookbook, cost optimization (2026-08-09): https://platform.claude.com/cookbook/cost-optimization-cost-optimization
- Usage and Cost API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api.md
- Claude Code Analytics API: https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api.md
- WIF Admin API: https://platform.claude.com/docs/en/manage-claude/wif-admin-api.md
- Economic Index (2025-09-15): https://www.anthropic.com/research/anthropic-economic-index-september-2025-report
- A statistical approach to model evaluations (2024-11-19): https://www.anthropic.com/research/statistical-approach-to-model-evals
- Demystifying evals for AI agents (2026-01-09): https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Bundled claude-api skill docs (local, v2.1.280): `shared/cost-optimization.md`, `shared/prompt-caching.md`, `shared/managed-agents-scheduled-deployments.md`, `shared/evals/build-eval.md`, `shared/evals/eval-audit.md`, `python/claude-api/batches.md`

**GitHub**

- Copilot code review: https://docs.github.com/en/copilot/concepts/agents/code-review
- Copilot premium requests: https://docs.github.com/en/copilot/concepts/billing/copilot-requests
- Copilot models and pricing (AI credits, code review billing): https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
- Copilot cloud agent: https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent
- Actions concurrency: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency
- Events that trigger workflows: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- Workflow syntax (paths filters): https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- Variables: https://docs.github.com/en/actions/reference/workflows-and-actions/variables

**Other vendors and tools**

- Cursor Bugbot: https://cursor.com/docs/bugbot
- Cursor, "Securely indexing large codebases" (2026-01-27): https://cursor.com/blog/secure-codebase-indexing
- CodeRabbit pricing: https://www.coderabbit.ai/pricing
- Graphite pricing: https://graphite.com/pricing
- OpenAI Codex pricing: https://learn.chatgpt.com/docs/pricing
- openai/codex-action: https://github.com/openai/codex-action
- OpenAI Batch: https://developers.openai.com/api/docs/guides/batch
- OpenAI Flex: https://developers.openai.com/api/docs/guides/flex-processing
- OpenAI pricing: https://developers.openai.com/api/docs/pricing
- OpenAI embeddings guide: https://developers.openai.com/api/docs/guides/embeddings
- text-embedding-3-large model page: https://developers.openai.com/api/docs/models/text-embedding-3-large
- Voyage pricing: https://docs.voyageai.com/docs/pricing
- Voyage batch: https://docs.voyageai.com/docs/batch-inference
- Voyage flexible dimensions and quantization: https://docs.voyageai.com/docs/flexible-dimensions-and-quantization
- Voyage embeddings: https://docs.voyageai.com/docs/embeddings
- Gemini pricing (updated 2026-09-23): https://ai.google.dev/gemini-api/docs/pricing
- Gemini embeddings (updated 2026-09-17): https://ai.google.dev/gemini-api/docs/embeddings
- Cohere pricing: https://cohere.com/pricing
- Cohere Embed: https://docs.cohere.com/docs/cohere-embed
- Bedrock pricing: https://aws.amazon.com/bedrock/pricing/
- Titan Text Embeddings V2 blog (2024-04-30): https://aws.amazon.com/blogs/aws/amazon-titan-text-v2-now-available-in-amazon-bedrock-optimized-for-improving-rag/
- Hugging Face, "Embedding Quantization" (2024-03-22): https://huggingface.co/blog/embedding-quantization
- LlamaIndex ingestion pipeline: https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/
- promptfoo caching: https://www.promptfoo.dev/docs/configuration/caching/
- promptfoo GitHub Action: https://www.promptfoo.dev/docs/integrations/github-action/
- Datadog, State of AI Engineering (July 2026): https://www.datadoghq.com/state-of-ai-engineering/
- FinOps Foundation, Tokenomics (2026-06-03): https://www.finops.org/wg/token-economics-saas/
- Artificial Analysis methodology: https://artificialanalysis.ai/methodology/intelligence-benchmarking

**Papers (arXiv abstract pages opened)**

- tinyBenchmarks, Maia Polo et al., ICML 2024 (2024-02-22): https://arxiv.org/abs/2402.14992
- Anchor Points, Vivek et al., EACL 2024 (2023-09-14): https://arxiv.org/abs/2309.08638
- Efficient Benchmarking of Language Models, Perlitz et al., NAACL 2024 (2023-08-22): https://arxiv.org/abs/2308.11696
- metabench, Kipnis et al., ICLR 2025 (2024-07-04): https://arxiv.org/abs/2407.12844
- Fluid Language Model Benchmarking, Hofmann et al., COLM 2025 (2025-09-14): https://arxiv.org/abs/2509.11106
- Reliable and Efficient Amortized Model-based Evaluation, Truong et al. (2025-03-17): https://arxiv.org/abs/2503.13335
- Efficient multi-prompt evaluation (PromptEval), Maia Polo et al., NeurIPS 2024 (2024-05-27): https://arxiv.org/abs/2405.17202
- Efficient Lifelong Model Evaluation (Sort & Search), Prabhu et al. (2024-02-29): https://arxiv.org/abs/2402.19472
- Adding Error Bars to Evals, Miller (2024-11-01): https://arxiv.org/abs/2411.00640
- Signal and Noise, Heineman et al. (2025-08-18): https://arxiv.org/abs/2508.13144
- Replacing Judges with Juries (PoLL), Verga et al. (2024-04-29): https://arxiv.org/abs/2404.18796
- Trust or Escalate, Jung et al. (2024-07-25): https://arxiv.org/abs/2407.18370
- Judging the Judges, Thakur et al. (2024-06-18; rev. 2025-08-18): https://arxiv.org/abs/2406.12624
- Batch Prompting, Cheng et al., EMNLP 2023 Industry (2023-01-19): https://arxiv.org/abs/2301.08721
- FrugalGPT, Chen, Zaharia and Zou (2023-05-09): https://arxiv.org/abs/2305.05176
- Holistic Agent Leaderboard, Kapoor et al. (2025-10-13): https://arxiv.org/abs/2510.11977
- AI Agents That Matter, Kapoor et al. (2024-07-01): https://arxiv.org/abs/2407.01502
- How Do AI Agents Spend Your Money?, Bai et al. (2026-04-24): https://arxiv.org/abs/2604.22750
- Does AI Code Review Lead to Code Changes?, Sun et al. (2025-08-26; rev. 2026-04-25): https://arxiv.org/abs/2508.18771
- Matryoshka Representation Learning, Kusupati et al. (2022-05-26; rev. 2024-02-08): https://arxiv.org/abs/2205.13147

**Internal (not public)**

- Sibling track `empirical.md` in this research folder, a local measurement of Claude Code transcripts: first-call prefix writes median 33–49k tokens; concurrent fan-out premium ~1.2%. Used only for the illustrative sizing in F6 and F7.
