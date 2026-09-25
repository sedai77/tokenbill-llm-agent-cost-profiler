# Verification notes: gap-automated-workloads-ci-evals

Fact-checked 2026-09-23. Each cited source was opened, either with WebFetch or by curl plus grep on the raw page. Local claims were checked against the bundled claude-api skill docs (v2.1.280) and the local claude-code-action clone. For the clone, the key lines were also checked against GitHub `main`.

**Tally:** 23 confirmed, 3 corrected, 0 unverifiable, 0 refuted.

| id | verdict |
|---|---|
| auto-share-proxy | confirmed |
| ci-review-unit-costs | confirmed (minor nuances) |
| ci-trigger-multiplier | confirmed |
| ci-review-addressed-rate | confirmed |
| ci-review-tiering | confirmed |
| ci-cross-run-cache | **corrected** (git-snapshot detail) |
| ci-fanout-stagger | confirmed |
| batch-flex-50 | confirmed |
| ci-caps-budgets | confirmed |
| ci-headless-ingest | confirmed |
| detect-signatures | confirmed |
| sched-cadence-ttl | confirmed |
| sched-event-driven | confirmed |
| ci-result-cache | confirmed |
| eval-subset-irt | **corrected** (headline range) |
| eval-stats-trials | confirmed |
| eval-judge-choice | confirmed |
| eval-batch-judges | confirmed |
| eval-rerun-failures | confirmed |
| dev-test-bleed | confirmed |
| emb-prices-2026 | confirmed |
| emb-incremental-hash | confirmed |
| emb-dims-quant | confirmed |
| emb-shared-space | confirmed (stronger source found) |
| emb-scope-decision | **corrected** (price-ratio range) |
| auto-unit-metrics | confirmed |

---

## auto-share-proxy: CONFIRMED

Source: anthropic.com Economic Index report, published 2025-09-15 (raw HTML grepped).

- Verbatim: "1P API usage is automation dominant: 77% of business uses involve automation usage patterns, compared to about 50% for Claude.ai users."
- Verbatim: "Little less than half of all API traffic maps to computer and mathematical tasks".
- Verbatim: "Overall, we find evidence of weak price sensitivity."
- Verbatim: "each 1% cost increase is associated with a 0.29% reduction in usage frequency", with elasticity −0.29. Strictly, this measures task *prevalence* in the API sample. The same report also finds that across occupational categories, higher-cost tasks have *higher* usage (elasticity +3).

Datadog State of AI Engineering (July 2026) confirms 69% of input tokens are system prompts, 28% of spans show cached reads, and 59% of agentic requests make a single call. It has no split of spend by CI, eval, scheduled or interactive work.

## ci-review-unit-costs: CONFIRMED

Every figure matches its source:

- **Anthropic Code Review** ("Each review averages $15-25"; "completing in 20 minutes on average"): code.claude.com/docs/en/code-review.
- **Copilot review** ("$0.05 USD to $1 USD … 'Lite' … $0.25 USD to $5 USD with 'Balanced'", plus Actions minutes): docs.github.com Copilot code review.
- **Copilot legacy plans** (13 premium requests per review, $0.04 per additional request): copilot-requests. This applies only to Pro and Pro+ annual plans that stayed on request billing after 2026-06-01.
- **CodeRabbit**: $24, $48 and $72 per dev per month, with $0.25 per reviewed file as overage. The $24–72 figures are annual-billing prices; monthly billing is $30 and $60.
- **Claude Code interactive**: $150–250 per dev per month (costs page).

Derived figures:

- Break-even of 150/25 = 6 to 250/15 ≈ 17 runs is correct.
- The 15–500× span is $15/$1 = 15 and $25/$0.05 = 500, which is correct.
- In the report body (§1.2), "~$2k" for 12,000 Copilot Lite runs is loose; the true range is $600–12,000.

## ci-trigger-multiplier: CONFIRMED

- **Code Review pricing text:** "After every push: runs on each push, multiplying cost by the number of pushes". Once and Manual modes exist, and fork PRs are reviewed only on `@claude review`.
- **github-actions page:** the review workflow uses `types: [opened, synchronize, ready_for_review, reopened]`. It gives the skip rules verbatim ("draft and closed pull requests, … automated or trivial ones, and pull requests that already have a comment from Claude"). Cost controls list `--max-turns`, timeouts and concurrency.
- **GitHub concurrency:** `cancel-in-progress: true` cancels "any currently running job or workflow in the same concurrency group"; pending runs are already replaced by default.
- **paths-ignore:** skips a run only when all changed paths match.
- **Cursor Bugbot:** "Runs automatic reviews on every PR update" by default. Incremental review ("only the changes since the previous Bugbot review") is the default.
- **Copilot:** excludes dependency lockfiles, logs and SVGs.
- "Canceling still bills tokens already spent" is sound inference, not a quoted source.

## ci-review-addressed-rate: CONFIRMED

Source: arXiv 2508.18771, Sun et al., v1 2025-08-26 and v2 2026-04-25. The HTML was grepped.

- 22,326 AI comments across 178 mature repos from 16 actions.
- "60% of valid human review comments led to code changes, compared to only 0.9%–19.2% for valid AI-generated comments".
- Hunk-level actions: 6.5–19.2%. File-level actions: 0.9–4.2%.
- anc95/ChatGPT-CodeReview: 12.8% of manually triggered comments addressed vs 6.8% of automatic ones.
- Nuance: the address rates are for *valid* comments, on a labelled subsample of four actions.

The Code Review analytics show "Count of review comments that were auto-resolved because a developer addressed the issue".

## ci-review-tiering: CONFIRMED

- **Copilot Balanced** "Routes pull requests to a higher-reasoning model for longer analysis". Lite and Balanced price ranges were confirmed above.
- **Code Review:** "A fleet of specialized agents … full codebase" plus a verification step.
- **headless page:** shows `gh pr diff "$1" | claude -p --append-system-prompt "You are a security engineer. Review for vulnerabilities." --output-format json`. The build-script example says "Piping the diff means Claude doesn't need Bash permission to read it".
- **optimizing-for-cost:** "Claude Opus 5 gave up about 2 points at `medium` for half the cost and about 8 points at `low` for a quarter of it".

## ci-cross-run-cache: CORRECTED

**Confirmed parts:**

- **Claude Code prompt-caching page:** "the cache is effectively scoped to one machine and directory". Upgrades rebuild the cache. The default TTL is 5 min on an API key.
- **Workspace isolation:** per workspace on the Claude API, Claude Platform on AWS and Foundry, and per organization on Bedrock and Google Cloud (bundled prompt-caching.md line 130).
- **The flag** (cli-reference): "Move per-machine sections from the system prompt (working directory, environment info, memory paths, git-repo flag) into the first user message." The SDK option is `excludeDynamicSections`.
- **Cookbook** (published 2026-08-09): $0.2784 → $0.1273, "54% cheaper vs variable-first".
- **Datadog:** 28% and 69%.
- **Illustrative math:** 40k-token prefix at $2.50 or $6.25 per MTok write vs $0.20 or $0.50 per MTok read, × 50k runs, gives $4.6k–11.5k. Correct.
- **Poisson figures:** 3.4%, 34% and 97% are correct.

**What is wrong:** the finding puts "the startup git snapshot in the system prompt". The docs do not say that.

- The Agent SDK page lists what the preset embeds in the system prompt: working directory, git-repo flag, platform, shell, OS version and auto-memory paths.
- The prompt-caching page says only that "each conversation also carries the branch and recent commits from that snapshot".
- The exclude-dynamic flag does not list the git status snapshot among what it moves.

**Corrected claim:** Claude Code's preset system prompt embeds cwd, the git-repo flag, platform, shell, OS version and auto-memory paths. The startup git snapshot (branch, recent commits) is carried in the conversation, at a location the docs don't specify. `--exclude-dynamic-system-prompt-sections` moves the per-machine sections, but it is not documented to neutralize git-snapshot differences. CI runs on different SHAs may therefore still diverge after the system prompt.

Also note that the flag applies only with the default system prompt.

## ci-fanout-stagger: CONFIRMED

- **Bundled prompt-caching.md § Concurrent-request timing:** "A cache entry becomes readable only after the first response **begins streaming**. N parallel requests with identical prefixes all pay full price". Its fix: "send 1 request, await the first streamed token … then fire the remaining N-1".
- **Claude Code docs:** workflow fan-outs hold "all but the first for up to 5 seconds by default".
- **Bundled § Pre-warming:** worth it only when "first-request latency is user-visible (chat/voice/interactive - not background jobs)".
- **batch-processing.md:** "`max_tokens: 0` … is not supported inside a batch".

## batch-flex-50: CONFIRMED

**Anthropic Batch:**

- "All usage is charged at 50% of the standard API prices"; discounts stack with caching.
- "most batches finishing in less than 1 hour"; 24 h expiry; 100,000 requests or 256 MB per batch.
- Cache hits are best-effort, "30% to 98%"; the 1-hour cache is suggested.

**Managed Agents** (pricing.md): "Batch API discount | Sessions are stateful and interactive. There is no batch mode."

**OpenAI:**

- Batch: "50% cost discount", including `/v1/embeddings`.
- Flex: "priced at Batch API rates"; suited to "model evaluations, data enrichment, and asynchronous workloads".
- A 429 "Resource Unavailable" is not charged.

**Other providers:**

- Bedrock: batch "50% lower price" for select models; "Flex tier pricing is at 50% discount"; Priority carries a 75% premium.
- Gemini: batch "50% cost reduction".
- Voyage: batch "33% discount", 12-hour window.

## ci-caps-budgets: CONFIRMED

- **`--max-budget-usd`** (cli-reference): "(print mode only). Spend from subagents counts toward the cap". Cap enforcement requires v2.1.217+.
- **`--max-turns`:** "Exits with an error when the limit is reached. No limit by default."
- **github-actions cost list:** `--max-turns`, workflow timeouts and concurrency controls.
- **optimizing-for-cost:**
  - "two problems carried 43% of the spend" (20-problem WideSearch).
  - Task budgets: "cut cost per task 44% for about 3 points … tightest … 58% for 6 points" (Fable 5.1, SWE-bench Pro).
  - `max_tokens` caps "bought proportionally fewer solves" ($21 vs $22 per solved task).
- **arXiv 2604.22750** (Bai et al., submitted 2026-04-24): "runs on the same task can differ by up to 30x in total tokens". Self-prediction correlation is "up to 0.39".

## ci-headless-ingest: CONFIRMED

- **Local clone** (`gapci/cca`, HEAD 46a42b4, 2026-09-23 09:39 −0700, origin anthropics/claude-code-action):
  - `parse-sdk-options.ts:284` sets `env.CLAUDE_CODE_ENTRYPOINT = "claude-code-github-action"`. The same line and number are on GitHub `main`.
  - `execution-file.ts` writes `$RUNNER_TEMP/claude-execution-output.json` and `setOutput("execution_file")`.
  - `run-claude-sdk.ts` pushes every SDK message into `messages` before writing the file.
- **Minor:** the sanitized log summary includes `num_turns`, `total_cost_usd` *and* a sanitized `modelUsage`, not only the first two.
- **headless page:** `--output-format json` "includes `total_cost_usd` and a per-model cost breakdown". `--bare` "is the recommended mode for scripted and SDK calls, and will become the default for `-p` in a future release".
- **cost-tracking page:** deduplicate by message ID. "Per-step `output_tokens` is a placeholder".

## detect-signatures: CONFIRMED

- **monitoring-usage:**
  - `app.entrypoint` values "`cli`, `sdk-cli`, `sdk-ts`, `sdk-py`, or `claude-vscode`", gated by `OTEL_METRICS_INCLUDE_ENTRYPOINT` (default false).
  - The `active_time` type is `"user"` for keyboard input and `"cli"` for tool and AI work.
  - The `user_prompt` event attributes include no non-interactive or source flag.
- **Analytics API:** `api_actor` with `api_key_name`, and `terminal_type`.
- **WIF Admin API:** `svac_…` service accounts; GitHub issuer `https://token.actions.githubusercontent.com`; subject `repo:org/repo:ref:…`.
- **Usage-cost API:** filters and groups by API key, workspace, model, service tier, context window, data residency and speed. No service-account dimension is documented.
- **GitHub variables:** `CI`, `GITHUB_EVENT_NAME` and `GITHUB_RUN_ATTEMPT`, verbatim.
- **Copilot billing:** "Filter by `workflow_path` using the value `dynamic/agents/copilot-pull-request-reviewer`".

## sched-cadence-ttl: CONFIRMED

**Claude Code `/loop` and scheduled tasks:**

- costs page: a scheduled task "fires on its interval even while the session is idle, sending your full context each time".
- scheduled-tasks page: jitter up to 30 min, or half the interval for sub-hourly tasks, deterministic per task; 7-day expiry; "up to 50 scheduled tasks".

**Routines:** "The minimum interval is one hour", plus a "daily cap on how many runs can start per account".

**Managed Agents deployments** (bundled scheduled-deployments doc): jitter "up to 15% of the interval … floored at 5 seconds and capped at 9 minutes"; "Maximum 1000 scheduled deployments per organization"; a `budget` is copied onto each session. Pricing.md confirms $0.08 per session-hour and no batch discount.

**GitHub `schedule`:** "High load times include the start of every hour … some queued jobs may be dropped".

**Arithmetic:** Opus 5 prices are $0.50 per MTok read and $6.25 per MTok 5-minute write. 150k tokens gives $0.075 warm vs $0.9375 cold. That is 288 × 0.075 = $21.6 a day, or 24 × 0.9375 = $22.5 a day. Correct.

## sched-event-driven: CONFIRMED

- scheduled-tasks page: "To react to events as they happen instead of polling, see Channels: your CI can push the failure into the session directly."
- Monitor "avoids polling altogether and is often more token-efficient and responsive than re-running a prompt on an interval".
- Routines support API and GitHub-event triggers.

## ci-result-cache: CONFIRMED

- **promptfoo caching:** `PROMPTFOO_CACHE_ENABLED` defaults to true. The key covers the provider id, a deterministic request digest, provider config and vars. TTL is 14 days, on disk.
- **promptfoo GitHub Action example:** `paths: - 'prompts/**'`, plus an `actions/cache` step at `~/.cache/promptfoo`.
- **GitHub variables:** `GITHUB_RUN_ID` "does not change if you re-run"; `GITHUB_RUN_ATTEMPT` "increments with each re-run".
- **Review skill** skips "pull requests that already have a comment from Claude".

## eval-subset-irt: CORRECTED

All eight papers exist and are correctly titled, dated and attributed:

- **tinyBenchmarks** (ICML 2024, 2024-02-22): "14K examples … 100 curated examples".
- **Anchor Points** (EACL 2024, 2023-09-14): "1-30 anchor points outperforms uniform sampling", 87 model–prompt pairs.
- **Efficient Benchmarking** (NAACL 2024, 2023-08-22): "reducing computation by x100 or more".
- **metabench** (ICLR 2025, 2024-07-04): 28,632 items, "less than 3%", 1.24% RMSE, 0.58% total.
- **Fluid Benchmarking** (COLM 2025, 2025-09-14): "higher validity and less variance on MMLU with fifty times fewer items".
- **Amortized** (2025-03-17): 22 benchmarks and 172 LMs; difficulty predicted from content; adaptive testing.
- **PromptEval** (NeurIPS 2024, 2024-05-27): quantiles across 100 templates "with a budget equivalent to two single-prompt evaluations".
- **Sort & Search** (2024-02-29; NeurIPS'24): "180 GPU days to 5 GPU hours (about 1000x reduction)".

**What is wrong:** the headline "cut eval cost 50–1000×" has the wrong lower bound. metabench's <3% of items is only about 33× or more.

**Corrected claim:** item or compute reductions of about 33× (metabench) to about 1000× (Sort & Search, lifelong evaluation). Fluid is 50×, tinyBenchmarks about 140×, and Efficient Benchmarking 100×+.

## eval-stats-trials: CONFIRMED

- **Anthropic post** (2024-11-19): "clustered standard errors … can be over three times as large as naive standard errors". It also covers resampling, a per-question correlation "between 0.3 and 0.7" and power analysis.
- **Miller, arXiv 2411.00640** (2024-11-01): confirmed.
- **Bundled eval-audit.md:** "`1/sqrt(n·R)`: 25 cases × 2 reps ~ ±14 points, 100 × 2 ~ ±7".
- **Bundled cost-optimization.md:** "around fifty cases and at least five trials per configuration".
- **Artificial Analysis:** per-benchmark repeats of 1–5. The ±1% 95% CI applies to the overall Intelligence Index, not to each benchmark.
- **Signal and Noise** (2025-08-18): "filtering noisy subtasks … leads to more reliable multi-task evaluations".
- **Demystifying evals** (2026-01-09): pass@k and pass^k; "20-50 simple tasks".

## eval-judge-choice: CONFIRMED

- **Bundled build-eval.md:84:** "`claude-haiku-4-5` is cheap and fast enough to run on every PR".
- **build-eval.md:118:** "otherwise up to half the real cost is invisible".
- **PoLL** (2404.18796, 2024-04-29): "outperforms a single large judge … over seven times less expensive".
- **Trust or Escalate** (2407.18370, 2024-07-25): "over 80% human agreement with almost 80% test coverage", with cheap initial judges.
- **Judging the Judges** (2406.12624, 2024-06-18; v6 2025-08-18): "only the best (and largest) models achieve reasonable alignment". Smaller models "may provide a reasonable signal" for ranking.

## eval-batch-judges: CONFIRMED

- The batch doc lists "Large-scale evaluations" as a use case. Flex names "model evaluations".
- **Batch Prompting** (2301.08721; EMNLP 2023 Industry): "up to 5x with six samples in batch", few-shot.
- **Arithmetic:** 10k × (3k × $5 per MTok + 300 × $25 per MTok) = $225 for Opus 5. Haiku 4.5 at $1/$5 per MTok is $45, or $22.5 batched. 10× is correct.

## eval-rerun-failures: CONFIRMED

- **optimizing-for-cost:** "about 93% passed for about $0.45 each, against 91.7% for $0.93". Fable 5.1 low: "$0.54 per solved task, against 77.4% for $0.84 from Claude Sonnet 5".
- **HAL** (2510.11977, 2025-10-13): "21,730 agent rollouts across 9 models and 9 benchmarks … about $40,000", 2.5B tokens, "higher reasoning effort reducing accuracy in the majority of runs".
- **AI Agents That Matter** (2407.01502, 2024-07-01): "jointly optimizing the two metrics".

## dev-test-bleed: CONFIRMED

- **FinOps Tokenomics** (2026-06-03): the "Development and testing bleed" text is verbatim. It includes "Integrate AI cost estimation into the CI/CD pipeline".
- Its levers: right-sizing 60–90%, batch 50%, caching 50–90% on cached tokens.
- The costs page confirms the auto-created "Claude Code" workspace, with workspace spend and rate limits.

## emb-prices-2026: CONFIRMED

- **OpenAI:** $0.02 and $0.13; no separate embedding batch row. The Batch guide lists `/v1/embeddings` at a 50% discount.
- **Voyage:** $0.02, $0.06 and $0.12. The first 200M tokens are free per account for each of these models. Batch gets 33% off, and "Free token credits do not apply to Batch API usage".
- **Gemini** (updated 2026-09-23): Embedding 2 text is $0.20 standard and $0.10 batch.
- **Cohere:** the pricing page shows only Model Vault hourly pricing. On the embed doc, the embed-v4.0 row lists only the "Embed" endpoint, while v3 models list "Embed, Embed Jobs".
- **Bedrock:** the embedding price rows were not visible; Flex is 50% off.

## emb-incremental-hash: CONFIRMED

- **LlamaIndex:** "each node + transformation combination is hashed and cached". With a docstore, an unchanged hash is skipped and a changed hash is "re-processed and upserted".
- **Cursor** (2026-01-27): Merkle tree; "caches embeddings by chunk content"; "92% similarity"; 7.87 s → 525 ms median.
- **Illustrative:** 5B × $0.02–0.12 = $100–600 a day, or $3k–18k a month. At 2% churn that is $60–360 a month. Arithmetic correct; churn is an assumption.

## emb-dims-quant: CONFIRMED

- **Gemini embeddings** (updated 2026-09-17): 68.16, 68.17, 67.99, 67.55, 66.19 and 63.31. Recommended dims are 768, 1536 and 3072.
- **OpenAI:** 3-large at 256 "still outperforming an unshortened `text-embedding-ada-002`".
- **Titan V2 blog** (2024-04-30): 512 dims keeps ~99% and 256 dims keeps 97%, with 75% storage saved.
- **Hugging Face** (2024-03-22): binary is 32× smaller and 24.76× faster, keeping ~92.5%, or ~96% with rescoring. int8 is 4× smaller and 3.66× faster, keeping ~99.3%.
- **MRL** (2022-05-26; v4 2024-02-08): "up to 14x smaller embedding size … at the same level of accuracy".

## emb-shared-space: CONFIRMED

- docs.voyageai.com/docs/embeddings says only "All embeddings created with the 4 series are compatible with each other. See blog post for details". Prices are confirmed.
- The linked Voyage blog (blog.voyageai.com/2026/01/15/voyage-4/, 2026-01-15) explicitly supports the claim:
  - "query embeddings generated using voyage-4-lite can be used to search for document embeddings generated using voyage-4-large".
  - "Upgrade to voyage-4 or voyage-4-large for query embeddings … without re-vectorizing documents".
- Cite the blog as the primary source.
- Nuance: the no-re-embed claim applies to changing the *query* model. Moving documents to a new tier still needs re-embedding to benefit.

## emb-scope-decision: CORRECTED

What checks out:

- 5B × $0.02–0.20 = $100–1,000 is correct.
- "About 5–50 Code Reviews" uses a $20 midpoint; at $15–25 the range is 4–67.

**What is wrong:** "10–250× below LLM tokens" has the wrong lower bound. Current Anthropic input list prices are Haiku 4.5 $1, Sonnet 5 $2 and Opus 5 $5 per MTok.

**Corrected claim:** embeddings at $0.02–0.20 per MTok are about 5× (Gemini Embedding 2 vs Haiku 4.5 input) to 250× (3-small or voyage-4-lite vs Opus 5 input) below LLM input tokens. Against Sonnet 5 input they are 10–100× below. The scope decision itself is judgment, not a sourced fact.

## auto-unit-metrics: CONFIRMED

- **FinOps:** cost per query or API call, per user per month, per successful outcome, and per business transaction.
- **Code Review:** "Cost weekly", auto-resolved "Feedback", repository breakdown, and "average cost per review for each repo" in admin settings.
- **Bundled cost-optimization.md:5:** "cost per completed task, not cost per token".
- The metric definitions themselves are the report's proposal.

---

### Other body-level notes (not in the finding list)

- L3's "Agent-loop caching 2.7–5.3×" matches the live optimizing-for-cost page. The bundled v2.1.280 doc says 2.5–3.7× at 81–90% hit rates; the two sources differ.
- L3's "Haiku 4.5 minimum cacheable prefix 4,096" is confirmed in bundled prompt-caching.md.
- The Opus 5.5 pricing row ($4/$20, read $0.20) exists in pricing.md. The report does not use it, but any model table in Token Bill must include it.
