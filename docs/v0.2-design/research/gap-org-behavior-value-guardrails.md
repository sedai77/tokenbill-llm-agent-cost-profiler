# Token Bill research track: org behavior, value guardrails and nudges

Research date: 2026-09-23. Track: `gap-org-behavior-value-guardrails`.

**Scope.** This track covers what durably changes developer behavior on AI spend, and what cost controls do to productivity:

- defaults, nudges, showback and chargeback, and caps
- published corporate AI-spend policies from 2025–2026, and their Goodhart failures
- value counter-metrics, and a value-adjusted cost metric with a throughput alarm
- education that measurably cuts spend
- privacy and labor-law limits on per-developer cost visibility

**Method and sources.**

- Every source cited below was opened during this pass. "Accessed" marks living documents that carry no date.
- My web-search quota ran out early in the pass. Discovery then relied on the HN Algolia API, Google News RSS, Crossref, OpenAlex, Semantic Scholar, Europe PMC, NCBI E-utilities and the arXiv API.
- Several news sites refused automated fetches (Business Insider, AP, Ars Technica, The Verge, FT, Bloomberg, CNBC). For those I cite the secondary outlet I could open and name the original reporter.
- Claims seen only in headlines are marked *unverified*.

**Evidence labels.**

- **strong**: an RCT, a large natural experiment or meta-analysis, a primary vendor document, or a first-party company report with numbers.
- **moderate**: a single observational dataset, a vendor dataset, or consistent reporting from several outlets.
- **weak**: a single secondary report, a headline, or an inference by analogy.

---

## 1. Executive summary

1. **Changing the default beats trying to persuade people, by roughly an order of magnitude.**
   - Datadog's largest savings came from two default changes:
     - Claude Code default model Opus 4.8 → Sonnet 4.6: 36.7% cost cut, **$687k/month**, for an 8% drop in weighted proficiency on 140+ internal evals.
     - Default effort high → medium: **$288k/month**.
   - Its Slack nudges saved about **$150k in their first week**, across 768 users, with no control group and no follow-up reported.
   - The research literature points the same way. A 58-study meta-analysis puts the effect of defaults at d = 0.68. A 200-study meta-analysis finds that "decision structure" (defaults) consistently beats "decision information" (messages).
   - Nudges shrink once you account for publication bias. Academic papers average 8.7 percentage points; the same kinds of nudges run at scale by two government nudge units averaged 1.4 pp.
   - **Implication:** Token Bill's biggest lever is a policy simulator that outputs a managed-settings patch, not a messaging engine.
2. **Leaderboards of consumption are now a documented failure at four large companies.**
   - **Meta.** The "Claudeonomics" intranet leaderboard had gamified titles such as "Token Legend", and agents were left running to inflate scores. It is being dismantled in favour of an AI Gateway with alerts and 2027 budgets.
   - **Amazon.** "KiroRank" backed a target of more than 80% of developers using AI weekly. Staff ran pointless tasks to climb it, and it was shut down in May 2026.
   - **Uber.** Its usage leaderboard preceded the loss of its entire 2026 AI-coding budget by April.
   - **Microsoft.** EVP Jay Parikh's August 2026 memo said: "Tokenmaxxing is not what we are optimizing for."
   - Parikh's list of waste behaviors (idle agents, questions the engineer already knew the answer to, work split into too many turns, the expensive reasoning tier on trivial tasks) is, in effect, a spec for Token Bill detectors.
   - **Implication:** Token Bill must never rank people by spend or tokens, in either direction.
3. **Cost controls do cut valuable use, not just waste, unless they are targeted.**
   - The best natural experiment is in health care. When 10,000+ employees moved from free care to a high-deductible plan, spending fell 11.8–13.8%. The cuts hit potentially valuable care (preventive services) as well as wasteful care, and people did not learn to price-shop (Brot-Goldberg et al., QJE 2017).
   - Prepaid metering cut electricity use 14% (Jack & Smith 2020). Blunt budget salience works, but blindly.
   - Vendors now say the same. Cursor sells pooled usage because per-user caps block critical work while other allocations sit unused. GitHub warns that raising user-level budgets without the enterprise budget blocks users early.
   - **Implication:** before any cap ships, Token Bill should show a "cap collateral damage" simulation: who would have been blocked, and what they shipped.
4. **Top users produce more, at sharply falling marginal return.**
   - Jellyfish, Q1 2026 (12,000 developers, 200 companies, about 7,500 with token data joined to PRs):
     - Top-20% users spent $1,822 per quarter for 23 merged PRs; bottom-20% users spent $3 for 11.
     - Top-decile users used about 9.9× the tokens per PR of the median user.
   - Faros 2026 (22,000 developers): bugs per developer +54%, incidents per PR +242.7%, median review time +441.5%.
   - METR's RCTs: developers were 19% *slower* in early 2025 while believing they were 20% faster. Late 2025 was ambiguous (−18% and −4%, both with CIs crossing zero, and heavy selection bias).
   - **Implication:** judge cost cuts against objective team-level throughput and quality, not self-reports, and use a holdout.
5. **Nudges decay, and the ones that last change something durable.**
   - Opower reports produce action-and-backsliding cycles that shrink over time. After two years of treatment, the effect decays only 10–20% per year once reports stop (Allcott & Rogers, AER 2014).
   - Moral suasion habituates with repetition and recovers after a gap. Price incentives are larger, habituate little, and build habits (Ito, Ida & Tanaka 2018).
   - Across 38 experiments, most effects that persisted did so through a technology or capital change rather than habit alone (Brandon et al.).
   - Repeated alerts lose force: acceptance fell 30% per extra reminder per encounter (Ancker et al. 2017).
   - **Implication:** every Token Bill nudge should end in a one-click *configuration* change (hook, setting, CLAUDE.md edit). Nudges should be rate-limited and have per-type cooldowns.
6. **Channel matters more than message.**
   - Google's Tricorder shows results at code review, on changed lines only, with an effective false-positive rate under 10%. Google found that when developers have to go to a dashboard or run a separate CLI, usage drops.
   - Microsoft's "Nudge" RCT (147 repos, 8,500 PRs) cut overdue-PR resolution time 60%, and recipients rated 73% of notifications positive.
   - Passive price display to professionals is weak. Showing lab fees at order entry cut orders 8.6% at one hospital (Feldman 2013), but a 3-hospital, one-year RCT found no change (Sedrak 2017). A peer-comparison report card to physicians had no average effect; it moved only the outliers (Ryskina 2018).
   - **Implication:** Token Bill should put nudges at the decision point:
     - the `SessionStart` resume-cost fields
     - status line, `/usage` flags, and hooks
     - private threshold-triggered DMs
     - opt-in PR checks
7. **Privacy and labor law cap how individual the data can be.**
   - Under German BetrVG §87(1) no. 6, works councils must agree to any technical device *objectively capable* of monitoring behavior or performance. Law firms read this to cover AI tools that log who used them, when and how often.
   - BetrVG §80(3) now deems an AI expert necessary for the works council. GDPR Art. 88 allows national rules, with specific safeguards for workplace monitoring.
   - The UK ICO requires a DPIA for high-risk monitoring and the least intrusive means.
   - The EU AI Act lists AI used to monitor and evaluate workers' performance and behaviour as high-risk (Annex III 4(b)), with obligations from 2 December 2027. It bans workplace emotion inference outright (Art. 5(1)(f)).
   - New York requires written notice of electronic monitoring (Civil Rights Law §52-c).
   - Microsoft Viva Insights enforces a minimum group size of at least 5.
   - DX tells customers not to use AI metrics for individual performance evaluation.
   - **Implication:** default to team-level aggregation with k ≥ 5, a private self-view for individuals, pseudonymous IDs, no prompt content, no sentiment or emotion inference, and a generator for the works-council and DPIA pack.
8. **The market already ships the enforcement primitives; nobody ships the judgement.** Claude, GitHub and Cursor all provide:
   - per-user and per-group spend limits
   - org default model and per-role effort caps
   - fast mode off by default for Team and Enterprise, plus a per-session opt-in
   - budget-increase request queues
   - 75%/95% spend warnings in Claude Code

   Claude's own analytics dashboard ships a **top-10 usage leaderboard**. What no one ships:
   - policy what-ifs priced from real telemetry *and* checked against an eval or throughput guardrail
   - nudges with holdouts, decay tracking and precision gates
   - a value-adjusted cost that survives Goodhart
   - privacy-by-default aggregation designed with works councils in mind

   That combination is Token Bill's opening.

---

## 2. Intervention catalog

The effect sizes are the best available anchors. Unless marked AI-specific, they come from analogous domains (energy, health care, public policy) and should be treated as priors to be re-measured with a holdout.

| # | Intervention | Mechanism | Best evidence (effect) | Evidence | Decay / durability | Productivity risk | Token Bill role |
|---|---|---|---|---|---|---|---|
| I1 | **Default model** for the fleet (org default, per role) | Default / decision structure | Datadog, Opus 4.8 → Sonnet 4.6: −36.7% cost, $687k/mo, −8% eval proficiency (AI-specific) | strong | Persistent while the default holds; people can switch back | Real (8% on evals); needs an eval gate | Simulate, eval-gate, emit patch |
| I2 | **Default effort** (and per-role effort caps) | Default | Datadog high → medium: $288k/mo. Anthropic: Opus 5 at `medium` gave up about 2 points for about half the cost on long-horizon coding. Uber also defaults to Medium. | strong | Persistent; but `/effort` + Enter saves a level permanently, so escalations stick | Low to moderate | Simulate; detect "sticky escalation" |
| I3 | **Non-sticky expensive modes** (fast mode per-session opt-in; `max` session-only) | Default plus friction | Fast mode is 2× Opus price ($8/$40 vs $4/$20 on Opus 5.5). It is off by default for Team and Enterprise, and it persists across sessions unless `fastModePerSessionOptIn` is set. | moderate (mechanism documented; effect not measured) | Durable | Low | Detect persisted fast mode; emit setting |
| I4 | **Context and compaction defaults** | Default | Uber auto-compacts at 400K even on 1M-context models; no measured effect published | weak–moderate | Durable | Unknown (compaction quality) | Simulate from traces |
| I5 | **Threshold-triggered private DM** (daily spend crosses a line) | Salience + just-in-time | Datadog: 768 users, about $150k in week 1 (no control). Uber alerts at 50/80/100% of expected spend. | moderate | Unknown for AI; energy analog decays unless spaced | Low if private | Nudge engine with holdout |
| I6 | **Live price salience** (status line cost) | Salience | Invisible bills raise use 4.0% (Sexton 2015). Tax-inclusive price tags cut demand 8% (Chetty et al. 2009). Lab fee display: −8.6% at one site (Feldman 2013), null in a 1-year 3-hospital RCT (Sedrak 2017). | moderate (mixed) | Cyclical (bill arrival −0.6 to −1%, Gilbert & Graff Zivin) | Very low | Ship status-line script; never the main lever |
| I7 | **Peer comparison** (descriptive + injunctive) | Social norms | Opower: −2.0% on average; −6.3% top decile; −0.3% bottom decile (Allcott 2011). Physicians: no average effect, but outliers cut orders (Ryskina 2018). Descriptive-only messages can boomerang for low users (Schultz 2007). | strong (energy), moderate (professionals) | Action and backsliding; decays 10–20%/yr after 2 yrs (Allcott & Rogers 2014) | Low if private and outlier-only | Private, outlier-only, efficiency-framed |
| I8 | **Public consumption leaderboards** | Status competition | Meta, Amazon, Uber, Microsoft: gaming, idle agents, fabricated tasks, then shutdown | strong (multiple independent reports) | n/a | High (Goodhart) | **Never build.** Flag as an anti-pattern |
| I9 | **Hard per-user caps** | Budget constraint | Prepaid meters −14% (Jack & Smith 2020). A deductible cut valuable and wasteful care alike (Brot-Goldberg 2017). Uber $1,500/tool/month with permission to exceed (June 2026). | moderate (by analogy) | Durable | **High** unless targeted | Cap simulator with collateral-damage report |
| I10 | **Soft tiers + fast approval** | Budget + escalation | Uber (Aug 2026): one shared tier across harnesses, manager sign-off, quick propagation. Claude guide: start conservatively, investigate before raising. | moderate | Durable | Low to moderate | Tier designer; approval SLA metrics |
| I11 | **Team showback** | Accountability | FinOps: showback is always required; no causal effect data found for AI | weak (direct), moderate (energy analog) | Needs recurring salience | Low | FOCUS-shaped team reports with waste |
| I12 | **Team chargeback** (P&L) | Price incentive | Economic incentives: larger effect, little habituation, habit formation (Ito et al. 2018); no AI data | weak–moderate | More durable than suasion | Moderate (teams may under-use) | Only after allocation coverage is high |
| I13 | **Workflow-integrated actionable findings** (code review, hooks) | Just-in-time + low friction | Tricorder: <10% effective FP at code review; dashboards lose usage. Microsoft Nudge RCT: −60% PR resolution time, 73% positive. | strong (for the channel) | Sustained at Google/Microsoft scale | Low if precise | Detectors surface as PR check or hook with fix |
| I14 | **Convert-to-config nudges** (one click applies a hook or setting) | Technology adoption | Most persistent nudge effects run through technology adoption (Brandon et al., 38 experiments) | moderate | **Most durable** | Low | Every nudge ends in a patch |
| I15 | **Spec-quality education** | Skill | A bare user story instead of a full spec: +29.7% tokens (13–115% by task) (Smékal 2026). Anthropic: after two failed corrections, `/clear` and re-prompt. | moderate | Unknown | None | Detect vague prompts and correction loops |
| I16 | **Context-file hygiene** (CLAUDE.md / AGENTS.md) | Skill + config | Mixed: −16.6% output tokens and −28.6% runtime (Lulla 2026). No success gain and +20% cost (Gloaguen 2026). Context Bloat in 42% of files (dos Santos 2026). Jellyfish: +29% throughput per doubling of context-file investment (observational). | moderate (conflicting) | Durable (file) | Can hurt | Lint plus A/B per repo, never blanket mandates |
| I17 | **Surface gating** (who gets Claude Code or agents) | Access | Anthropic consumption guide: giving everyone Claude Code and Cowork on day one is the fastest route to unexpected consumption | weak | Durable | Moderate (adoption) | Role-tier advisor |
| I18 | **Adoption mandates by activity** (80% weekly usage, AI usage in reviews) | Mandate | Amazon, Meta, Shopify, Coinbase: gaming and tokenmaxxing followed | moderate | n/a | High | Anti-pattern |

**Planning effect sizes.**

- **Defaults on the affected traffic:** 20–40% cost reduction. Datadog's model switch was 36.7%; Anthropic's effort curves give about 50% for about 2 points on coding. Each change must pass an eval.
- **Nudges:** plan for about 1–5% of fleet spend, concentrated in the top decile. Nudge-unit RCTs average 1.4 pp (DellaVigna & Linos). Large-scale climate interventions average about 2 pp (Bergquist et al. 2023). After bias correction, Maier et al. find no evidence of a mean nudge effect, though effects are heterogeneous. Datadog's first-week $150k is an upper-bound anecdote with no counterfactual.
- **Caps:** effective at cutting spend. The productivity cost is unknown until measured, so require a holdout.

---

## 3. Findings

### F1. Default changes produced most of Datadog's >$1M/month saving [strong]
- **Datadog, 2026-08-26** (Bowen Chen et al.):
  - Default Claude Code model Opus 4.8 → Sonnet 4.6: −36.7% cost, **$687k/month**, 8% loss in weighted proficiency across 140+ evals run nightly on a self-serve agentic eval platform.
  - Default effort high → medium: **$288k/month**.
  - Slack workflow when a user crosses a daily spend threshold on provisioned keys: DMs the user a link to internal cost-saving docs. 768 users in the first week, **>$150k** reduced spend.
  - Headroom context compression pilot (>1,000 engineers, 1-week snapshot): −39.3% input tokens, −35.7% output tokens, −27% cost per user.
  - No developer-reaction or throughput data was published.
- **Anthropic's published effort curves** (claude-api skill, `shared/cost-optimization.md`, bundled with Claude Code v2.1.280):
  - Long-horizon coding on Opus 5: about 2 points lost at `medium` for about half the cost; about 8 points lost at `low` for about a quarter.
  - Research workloads: `medium` matched the default at 70–85% of its cost.
  - "Run at `low`, re-run failures at default": about 93% pass at about $0.70/task, vs 91.7% at $1.39.
- **Source dates:** Datadog blog 2026-08-26; Anthropic local skill doc (accessed 2026-09-23).
- **Token Bill should:**
  - make **policy what-ifs** first-class: price last month's real traffic under each candidate default (model, effort, fast mode, compaction window, cache TTL)
  - require an eval, or a throughput holdout, before recommending anything that trades capability
  - output a `managed-settings.json` patch plus a rollout plan

### F2. Claude Code's own defaults make escalations "sticky"; that is a hidden default effect [strong, from docs]
- **Effort** (Claude Code model-config and settings-reference, accessed 2026-09-23):
  - Picking a level in `/effort` and pressing Enter **saves it as your default for later sessions**. Pressing `s` applies it to the session only.
  - `max` is session-only unless set by an environment variable.
  - Opus 5.5 defaults to `medium`; most other models default to `high`.
- **Fast mode** (2× Opus price: $8/$40 on Opus 5.5 vs $4/$20 list):
  - A fast mode turned on in an interactive session **persists across sessions by default**, unless the org sets `fastModePerSessionOptIn: true`.
  - It is disabled by default for Team and Enterprise until an Owner enables it.
- **Enterprise controls:** an **org default model** (per custom role; optionally overriding user selection), **per-role effort caps**, `maxEffortLevel` on any provider, and `availableModels` plus `enforceAvailableModels`.
- **Token Bill should:**
  - detect **sticky escalations**: users whose saved effort, model or fast-mode preference has stayed above the org default for N days while their task mix is small diffs or short sessions
  - price the gap, and recommend per-session opt-in policies
- This is a Token Bill-specific detector that no competitor ships.
- **Sources:**
  - https://code.claude.com/docs/en/model-config
  - https://code.claude.com/docs/en/settings-reference
  - https://code.claude.com/docs/en/fast-mode

### F3. Defaults beat information in the choice-architecture literature, but the average nudge is small once publication bias is removed [strong]
- **Jachimowicz et al. 2019** (Behavioural Public Policy, 58 studies, n = 73,675): defaults d = 0.68 (95% CI 0.53–0.83).
  - Defaults work better in consumer domains and worse in environmental ones.
  - They work better when read as an endorsement or as the status quo.
- **Mertens et al. 2022** (PNAS, 200+ studies, 450+ effect sizes, n = 2.1M): overall d = 0.45.
  - "Decision structure" interventions (defaults) consistently beat "decision information".
  - Moderate publication bias.
- **Maier et al. 2022** (PNAS letter) re-analysed the same data. After correcting for publication bias, they find no evidence of a mean nudge effect, but heterogeneity remains, so some nudges work.
- **DellaVigna & Linos 2022** (Econometrica; 126 nudge-unit RCTs, 23M people): 1.4 pp at scale vs 8.7 pp in academic papers. Practitioners forecast effects almost perfectly.
- **Bergquist et al. 2023** (PNAS second-order meta-analysis, 430 studies):
  - d = 0.18 after bias adjustment.
  - About 2 pp in adequately powered large-scale interventions.
  - Social comparison and financial incentives were most effective; education and feedback were least effective.
- **Token Bill should:**
  - rank recommendations by mechanism: default > budget/price > targeted comparison > education
  - report nudge effects only against a randomized holdout
- **Sources:**
  - https://doi.org/10.1017/bpp.2018.43 (2019-01-24)
  - https://doi.org/10.1073/pnas.2107346118 (2022-01-04)
  - https://doi.org/10.1073/pnas.2200300119 (2022-07-19; text via Europe PMC PMC9351501)
  - https://www.nber.org/papers/w27594 (2020-07; Econometrica 2022, doi 10.3982/ecta18709)
  - https://doi.org/10.1073/pnas.2214851120 (2023-03-21)

### F4. Leaderboards of token consumption are a documented Goodhart failure [strong]
- **Meta.**
  - "Claudeonomics" intranet leaderboard: 85,000+ employees; 60 trillion tokens in 30 days; top user 281B tokens; titles "Token Legend", "Cache Wizard" and others.
  - Agents were left running to inflate scores.
  - Sources: The Decoder 2026-04-07, citing The Information; MLQ 2026-04-08.
  - The Pragmatic Engineer (2026-06-17) reports managers inspected token counts in performance reviews.
  - MLQ (2026-06-13, citing The Information) reports the leaderboard was being dismantled and replaced by an "AI Gateway" with real-time tracking and spike alerts, with formal budgets from 2027.
- **Amazon.**
  - "KiroRank" backed a goal of more than 80% of developers using AI weekly. Staff ran agents on pointless tasks to climb it.
  - SVP Dave Treadwell told staff not to use AI just for the sake of using it. Before retiring the dashboard, Amazon shifted it to "normalized deployments".
  - Sources: The Decoder 2026-05-29 and MLQ 2026-05-30, citing the FT. 404 Media (2026-06-01) confirms the shutdown and cheating. Amazon's official line was that the goal had been met.
- **Uber.**
  - It ranked usage competitively on leaderboards, then burned its 2026 AI-coding budget by April.
  - Sources: TechCrunch 2026-06-02 and 2026-06-05; Fortune 2026-05-26.
  - The CTO later said the company had moved beyond tokenmaxxing (LeadDev 2026-08-17, citing Fortune 2026-08-07).
- **Microsoft.**
  - Jay Parikh's email (404 Media, 2026-08-04). Per LeadDev, it listed waste as:
    - idle agents
    - questions the engineer already knew the answer to
    - work split into more turns than needed
    - the expensive reasoning tier on trivial tasks
  - Alex Heath (Sources, 2026-08-05) reports a new internal default model alongside the crackdown.
- **Other executives.**
  - Cognizant's CEO called token consumption a vanity metric (Fortune 2026-06-01).
  - Per LeadDev's summary of an upcoming LeadDev AI Impact Report: 58% of organizations measure AI impact via token usage; 57% say that fails to capture value; only 19% rate tokenmaxxing effective. The methodology is not yet published.
- **Classic grounding:**
  - Kerr, "On the Folly of Rewarding A, While Hoping for B" (AMJ 1975).
  - Manheim & Garrabrant, "Categorizing Variants of Goodhart's Law" (arXiv 1803.04585, 2018).
  - Counterpoint: relative-performance feedback on real *output* can raise productivity with no prize attached (Blanes i Vidal & Nossol, Management Science 2011). The failure here is ranking an **input**.
- **Token Bill should:**
  - never render per-person rankings of spend or tokens
  - treat Parikh's four behaviors as **waste detectors**:
    - idle or looping agents with no human turn
    - trivial prompts on the top reasoning tier
    - turn fragmentation (many short turns re-sending a large context)
    - duplicate questions
  - ship a "Goodhart audit" that flags any org metric rewarding input volume
- **Sources:**
  - https://the-decoder.com/meta-employees-compete-for-token-consumption-on-an-internal-ai-leaderboard/ (2026-04-07)
  - https://mlq.ai/news/meta-makes-internal-leaderboard-for-employee-ai-token-usage/ (2026-04-08)
  - https://mlq.ai/news/meta-caps-internal-ai-token-spending-after-costs-approach-billions-in-2026/ (2026-06-13)
  - https://newsletter.pragmaticengineer.com/p/why-is-meta-destroying-its-engineering (2026-06-17)
  - https://the-decoder.com/amazon-kills-internal-ai-leaderboard-after-employees-gamed-it-with-pointless-tasks/ (2026-05-29)
  - https://mlq.ai/news/amazon-scraps-internal-ai-leaderboard-after-employees-gamed-it-with-fake-tasks/ (2026-05-30)
  - https://www.404media.co/amazon-shuts-down-internal-ai-leaderboard-after-employees-cheated/ (2026-06-01)
  - https://www.404media.co/microsoft-tells-engineers-tokenmaxxing-is-not-what-we-are-optimizing-for/ (2026-08-04)
  - https://sources.news/p/microsoft-cracks-down-on-tokenmaxxing (2026-08-05)
  - https://leaddev.com/reporting/the-tokenmaxxing-hype-didnt-last-long (2026-08-17)
  - https://fortune.com/2026/06/01/cognizant-ceo-ravi-kumar-s-hiring-entry-level-tokenmaxxing-vanity-metric/ (2026-06-01)
  - https://doi.org/10.5465/255378 (1975)
  - https://arxiv.org/abs/1803.04585 (2018)
  - https://doi.org/10.1287/mnsc.1110.1383 (2011)

### F5. Uber's arc: leaderboard → budget blown → per-tool cap → shared tier with alerts and approvals [strong]
- **Budget and first cap.**
  - The 2026 AI-coding budget was gone by April (TechCrunch, Bellan, 2026-06-05).
  - Bloomberg (via TechCrunch, Ropek, 2026-06-02) reported a cap of **$1,500 per employee per month per agentic coding tool** (Claude Code, Cursor), exceedable with permission and tracked on an internal dashboard.
  - Simon Willison (2026-06-03) put two tools × $1,500 × 12 at $36k/yr, about 11% of Uber's median SWE compensation (his estimate), and called the cap a rational response compared with leaderboards.
  - The COO said the link between spend and shipped features does not exist yet (Fortune, 2026-05-26).
- **Uber's own engineering blog** (Medisetty, 2026-08-27):
  - **one shared tier across all interactive harnesses, not per-tool budgets**, with separate tiers for managed agents
  - Slack alerts at **50/80/100% of expected spend**, and manager sign-off for tier upgrades with quick propagation
  - a live cost counter in the harness **status line** (per harness and across all harnesses), plus a cost-check skill for on-demand breakdown and status-line coaching
  - reasoning effort defaulted to **Medium**; auto-compaction at **400K** even on 1M-context models
  - a session-analysis dashboard flagging **16 anti-patterns** (named examples: suboptimal model routing, MCP context bloat, cache expiry after breaks, prompt init overhead)
  - Feb–Aug 2026: weekly active users ×7 and agentic requests ×9.4. Cost per 1,000 requests fell about 34% from peak, and **cost per session fell 52% from the June peak**.
  - No causal attribution to any single lever, and no nudge-effect data.
  - Managed-agent outcome metrics: cost per merged PR, review, alert or cleanup; revert rate; F1; MTTR.
- **Inference (mine):** moving from per-tool caps to one shared tier suggests per-tool caps shift usage between tools and make admin work. Treat this as weak until Uber says so.
- **Token Bill should** support:
  - cross-harness shared budgets
  - expected-spend alerts at configurable thresholds
  - approval-latency metrics
  - a status-line script with contracted-rate costs
  - a catalogue that covers Uber's anti-pattern classes
- **Sources:**
  - https://techcrunch.com/2026/06/05/the-token-bill-comes-due-inside-the-industry-scramble-to-manage-ais-runaway-costs/
  - https://techcrunch.com/2026/06/02/uber-caps-employee-ai-spending-after-blowing-through-budget-in-four-months/
  - https://simonwillison.net/2026/Jun/3/uber-caps-usage/
  - https://fortune.com/2026/05/26/uber-coo-ai-spending-tokens-claude-code/
  - https://www.uber.com/us/en/blog/efficient-software-factory/ (2026-08-27)

### F6. Runaway-spend anecdotes set the tail-risk requirement [moderate]
- TechCrunch (2026-06-05):
  - One developer ran up a **$40,000 token bill in a month**; the CTO was unsure whether to curb or encourage it (Faros AI CEO Vitaly Gordon).
  - Jellyfish's Nicholas Arcolano: per-developer consumption up **about 18.6× in nine months**.
  - Priceline's Cursor renewal came in 4–5× higher.
  - Axios reportedly described one company facing a **$500M** Claude bill after failing to set usage limits. **Unverified:** I could not open a primary source; Tom's Hardware's page showed only the headline.
- Jellyfish (2026-07-27): median developers spend $50–100/month; the top 5% spend $5,000+/month.
- Meta's CTO reportedly said a top engineer spent the equivalent of his salary in tokens for 10× output. The Decoder notes no evidence was given.
- **Token Bill should** ship a **runaway circuit breaker**, separate from monthly caps. Examples: an idle agent loop, spend velocity above k× the personal baseline within an hour, or a scheduled task firing on a huge context. It should page the owner and optionally pause through the gateway API.
- **Sources:**
  - TechCrunch 2026-06-05 (above)
  - https://jellyfish.co/blog/why-the-real-roi-from-ai-isnt-showing-up-yet/ (2026-07-27)
  - https://the-decoder.com/meta-employees-compete-for-token-consumption-on-an-internal-ai-leaderboard/ (2026-04-07)
  - https://www.tomshardware.com/tech-industry/artificial-intelligence/mystery-company-accidentally-blew-usd500-million-on-claude-in-a-single-month-failed-to-put-usage-limit-on-licenses-for-employees (headline only)

### F7. Blunt budget constraints cut valuable use along with waste [strong, by analogy]
- **Brot-Goldberg, Chandra, Handel & Kolstad** (QJE 2017; NBER w21632). Employees moved from free care to a high-deductible plan:
  - spending fell 11.79–13.80%, entirely through lower quantity
  - no price shopping, even after two years
  - cuts hit potentially valuable care (preventive) and potentially wasteful care (imaging) alike
  - people responded to the spot price, not the true end-of-year shadow price
- **Jack & Smith** (AEJ: Applied 2020): prepaid metering cut electricity use **14%**, partly through higher marginal price sensitivity.
- **Sexton** (REStat 2015): automatic bill payment, which makes bills invisible, raised residential electricity use **4.0%** and commercial use up to 8.1%.
- **Vendor positions:**
  - Cursor's pooled usage: without it, a developer on a critical project can hit their cap while other allocations sit unused.
  - GitHub: raising user-level budgets without raising the enterprise budget can block users before they reach their individual budgets.
- **Implication:** caps and prices make spend salient and will cut it, but they don't discriminate between waste and value. Pair any cap with targeted waste fixes, and measure throughput.
- **Token Bill should** build a **cap collateral-damage simulator**. For a candidate cap policy, replay history and list who would have been blocked and when. Join that to the PRs, deploys and incidents those users produced in the blocked window, and report "$ saved vs PRs at risk".
- **Sources:**
  - https://www.nber.org/papers/w21632 (2015; QJE 2017)
  - https://doi.org/10.1257/app.20180155 (2020)
  - https://doi.org/10.1162/rest_a_00465 (2015)
  - https://cursor.com/docs/enterprise/pooled-usage.md (accessed)
  - https://docs.github.com/en/copilot/tutorials/budgets/optimizing-your-budget-configuration (accessed)

### F8. The enforcement primitives exist in every major tool, so Token Bill should configure them, not rebuild them [strong]
- **Claude for Teams and Enterprise:**
  - usage-credit spend limits at the org, group or individual level
  - a `/usage-credits` request flow that sends a request to admins
  - Enterprise Analytics API per-user cost
  - org default model, per-role model restrictions and per-role effort limits
- **Claude apps gateway:**
  - per-user, group or org caps (daily, weekly or monthly) enforced live with a 429
  - Claude Code warns at **75% and 95%** of the cap and shows a Spend limit bar and a status-line field
  - an audit trail, and PII retention defaults: identity 90 days, spend 13 months
  - an erase path for data subject access requests (DSARs)
- **Claude Console:** workspace spend limits.
- **GitHub Copilot:**
  - enterprise, cost-center and user-level budgets, with a "Stop usage when budget limit is reached" switch
  - members request budget increases, and admins "Approve and increase"
- **Cursor:** a per-user spend-limit Admin API (Enterprise), bulk limits, pooled usage, dynamic spend limits and alerts.
- **Anthropic's consumption guide** recommends:
  - light, standard and power role tiers
  - starting conservatively
  - investigating a group that nears its cap before raising it; the answer may be model guidance rather than budget
  - gating Claude Code and Cowork access on day one
- **Token Bill should** add a "policy compiler" with one intent (tiers, caps, defaults, alerts) and several targets:
  - Claude managed settings
  - Claude admin and gateway spend-limit API calls
  - GitHub budgets
  - Cursor limits
- It should also report approval-queue latency, because slow approvals are where caps hurt output.
- **Sources:**
  - https://code.claude.com/docs/en/costs
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
  - https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide
  - https://docs.github.com/en/copilot/how-tos/administer-copilot/manage-budget-requests
  - https://cursor.com/docs/account/teams/admin-api.md (all accessed 2026-09-23)

### F9. Nudge effects decay and habituate; durability comes from changing capital or configuration [strong]
- **Allcott & Rogers** (AER 2014, Opower):
  - initial reports cause high-frequency action-and-backsliding cycles, which attenuate over time
  - if reports stop after two years, effects are fairly persistent, decaying **10–20% per year**
  - consumers are slow to habituate
- **Ito, Ida & Tanaka** (AEJ: Policy 2018):
  - moral suasion shows significant habituation, and the effect is restored by leaving time between interventions
  - dynamic pricing gives larger effects, little habituation and habit formation
- **Brandon, Ferraro, List, Metcalfe, Price & Rundhammer** (NBER w23277, rev. 2022, 38 experiments): most energy reductions persist after treatment ends, consistent with **technology adoption** rather than habit alone.
- **Gilbert & Graff Zivin** (NBER w19510; JEBO 2014): consumption falls 0.6–1% after each bill arrives.
- **Ancker et al.** (BMC MIDM 2017, 112 clinicians):
  - acceptance fell **30% per additional reminder per encounter**, and 10% per 5-point rise in the share of repeated reminders
  - no desensitization over time for newly deployed alerts
- **Token Bill should:**
  - make every nudge actionable as a *config change*, such as enabling `fastModePerSessionOptIn`, adding a PreToolUse output filter, setting `model: haiku` for a subagent, or pruning CLAUDE.md
  - rate-limit nudges: at most one interruptive nudge per session, at most about 3 per person per week, and a per-type cooldown
  - track decay against a persistent holdout
- **Sources:**
  - https://www.aeaweb.org/articles?id=10.1257/aer.104.10.3003 (2014)
  - https://www.aeaweb.org/articles?id=10.1257/pol.20160093 (2018)
  - https://www.nber.org/papers/w23277 (2017, rev. 2022)
  - https://www.nber.org/papers/w19510 (2013)
  - https://doi.org/10.1186/s12911-017-0430-8 (2017-04-10)

### F10. Peer comparison works mainly on heavy users; descriptive-only framing can backfire [strong]
- **Allcott 2011** (J. Public Econ; 600,000 households):
  - Opower reports cut use 2.0%, equal to an 11–20% short-run price increase
  - top-decile users cut **6.3%**; bottom-decile users cut 0.3%
- **Schultz et al. 2007** (Psych Science): a descriptive norm alone made low users *increase* use (the boomerang effect). Adding an injunctive message eliminated the boomerang.
- **Ryskina et al. 2018** (JGIM; RCT with 114 residents; weekly emailed comparison with the service average plus a personal dashboard):
  - no significant average change
  - physicians more than 1 test/patient-day from the peer rate cut 0.80 tests/patient-day
- **Asensio & Delmas** (PNAS 2015): environmental and health framing beat cost-savings framing, 8% vs control.
- **Implications for AI spend:**
  - Show comparisons only privately, and only to outliers (for example the top decile of cost per outcome within a similar task mix).
  - Frame on efficiency (cost per merged PR, cache reuse), never raw spend.
  - Always add an injunctive statement plus a concrete fix.
  - Don't show peer medians to low users: the boomerang can push them *up* toward tokenmaxxing.
- **Sources:**
  - https://doi.org/10.1016/j.jpubeco.2011.03.003 (abstract via Semantic Scholar; MIT WP https://dspace.mit.edu/bitstream/1721.1/51712/1/2009-014.pdf)
  - https://doi.org/10.1111/j.1467-9280.2007.01917.x (2007)
  - https://doi.org/10.1007/s11606-018-4482-y (2018)
  - https://doi.org/10.1073/pnas.1401880112 (2015)

### F11. Passive price display to professionals is weak; showing cost at the point of decision helps only a little [strong]
- **Feldman et al. 2013** (JAMA IM; Johns Hopkins, 61 tests randomized): displaying fees at order entry cut active-arm ordering from 3.72 to 3.40 tests per patient-day (−8.59%). Control-arm ordering rose 5.64%.
- **Sedrak et al. 2017** (PRICE RCT; 3 hospitals, 1 year, 142,921 admissions): no significant change in ordering (+0.05/patient-day, P = .06) or fees.
- **Chetty, Looney & Kroft** (AER 2009): posting tax-inclusive prices cut demand 8%, so salience matters when prices are otherwise hidden.
- **Implication.**
  - A status-line dollar counter (as at Uber, and in Claude Code's own cost field) is cheap and harmless, but it should not be the main lever.
  - Its best use is making otherwise invisible costs visible at the moment of choice, such as a stale-session resume, fast mode, or an xhigh effort pick.
- **Sources:**
  - https://doi.org/10.1001/jamainternmed.2013.232 (2013-05)
  - https://doi.org/10.1001/jamainternmed.2017.1144 (2017-07-01)
  - https://doi.org/10.1257/aer.99.4.1145 (2009)

### F12. Where the nudge appears decides whether it is used: code review and hooks beat dashboards [strong]
- **Sadowski et al., Tricorder** (ICSE 2015):
  - Google says tools used through a dashboard or separate CLI saw usage drop off; FindBugs' CLI had only 35 users in 2014.
  - Tricorder shows results **at code review time, on changed lines**, with an **effective false-positive rate under 10%**, where a false positive is a report the developer chose not to act on.
  - It disables analyzers that annoy developers or whose bugs go unfixed.
  - IDE-only integration was untenable because many developers don't use IDEs.
- **Maddila et al., "Nudge"** (ACM TOSEM 2023):
  - RCT on 147 Microsoft repos: **−60% PR resolution time** for 8,500 overdue PRs.
  - 73% of notifications were resolved as positive.
  - Scaled to 8,000 repos and 210,000 notifications in a year.
- **Claude Code channels** (docs, accessed 2026-09-23):
  - `SessionStart` hooks receive `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired` and `estimated_cache_write_usd` on resume or fork. The docs say this is so a hook can report the cost of resuming a stale conversation before the first request.
  - Any hook can return a `systemMessage` shown to the user.
  - The status line reads cost, `prompt_cache` and `rate_limits.spend_limit`.
  - `/usage` already flags any behavior that is 10% or more of recent usage.
  - `companyAnnouncements` shows one random entry per session at startup.
  - `spinnerTipsOverride` supports per-tip `cooldownSessions` and `priority`.
- **Token Bill should** use channels in this order:
  - (1) settings and defaults
  - (2) hook or status line at the decision point
  - (3) a private DM on a threshold
  - (4) an opt-in PR check with the top cost driver and fix
  - (5) spinner tips and announcements for awareness only
  - (6) dashboards for admins only
- It should also gate every nudge type on a measured "not useful" rate below 10%, with thumbs up/down feedback and automatic disable.
- **Sources:**
  - https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/ (PDF opened)
  - https://doi.org/10.1145/3544791 (2023-03-30)
  - https://code.claude.com/docs/en/hooks
  - https://code.claude.com/docs/en/settings-reference

### F13. Anthropic names clearing between tasks and model choice as the highest-impact habits [strong]
- **Costs doc.** Unexpectedly high API spend usually traces to long sessions that were never cleared, or Opus left as default. The highest-impact habits to share are **clearing between unrelated tasks** and **matching the model to the job**.
  - `/clear` costs nothing.
  - `/compact` is itself a large request.
  - Agent teams use about 7× the tokens *when teammates run in plan mode*.
- **Best-practices doc.**
  - After two failed corrections on one issue, `/clear` and re-prompt with what you learned.
  - Plan mode adds overhead; skip it when the diff can be described in one sentence.
  - Scope investigations or use subagents.
  - Give verification targets.
- **Correction to the brief.** The costs doc names `/clear` and model choice. It does not name `/compact` as a highest-impact habit, and it warns that compaction is expensive.
- **Token Bill should** build detectors for:
  - the kitchen-sink session (many unrelated tasks in one context)
  - correction loops (≥ 3 corrections)
  - unscoped exploration (hundreds of file reads)
  - plan-mode overuse on tiny diffs
  - Opus on trivial tasks
- Each should come with priced counterfactuals.
- **Sources:**
  - https://code.claude.com/docs/en/costs
  - https://code.claude.com/docs/en/best-practices (accessed 2026-09-23)

### F14. Spec quality is a measurable cost driver [moderate]
- **Smékal** (arXiv 2608.25399, 2026-08-26; 2,700 runs, Kimi K3, three effort levels):
  - cutting a full task spec to a bare user story **raises token spend 29.7%**, with task sensitivity from 13% to 115%
  - run-to-run variance is unchanged
  - a predictor prices spec × effort configurations within 36% on unseen tasks
- **Caveats:** one author, one model, benchmark tasks.
- **Consistent evidence:** Anthropic's costs doc says vague requests trigger broad scanning. The best-practices doc recommends interviewing, then writing a SPEC.md, then running it in a fresh session.
- **Token Bill should** add a "spec-quality" signal:
  - prompt length, specificity (file references, acceptance tests) and follow-up correction count, set against session cost
  - a nudge that suggests the interview-spec-fresh-session workflow for large tasks
- **Sources:**
  - https://arxiv.org/abs/2608.25399 (2026-08-26)
  - https://code.claude.com/docs/en/best-practices

### F15. Context files are not automatically a win: the evidence conflicts [moderate]
- **Lulla et al.** (arXiv 2601.20404, 2026-01-28; 10 repos, 124 PRs): AGENTS.md gave **−28.64% median runtime** and **−16.58% output tokens**, with comparable completion.
- **Gloaguen, Mündler, Müller, Raychev & Vechev** (arXiv 2602.11988, 2026-02-12): context files did not generally improve task success and **raised inference cost by more than 20%**. Repository overviews were not helpful; non-standard practices were.
- **dos Santos et al.** (arXiv 2606.15828, 2026-06-14; 100 repos):
  - Lint Leakage in 62% of files
  - Context Bloat in 42%
  - Skill Leakage in 35%
- **Jellyfish** (2026-07-27, observational): each doubling of context-file investment gave +29% throughput.
- **Anthropic** recommends keeping CLAUDE.md under 200 lines and moving specialized workflows into skills.
- **Token Bill should** build a CLAUDE.md/AGENTS.md linter that uses these smell heuristics and prices each file's per-session token carry. It should recommend A/B tests per repo, not blanket "write more context" mandates.
- **Sources:**
  - https://arxiv.org/abs/2601.20404
  - https://arxiv.org/abs/2602.11988
  - https://arxiv.org/abs/2606.15828
  - https://jellyfish.co/blog/why-the-real-roi-from-ai-isnt-showing-up-yet/
  - https://code.claude.com/docs/en/costs

### F16. Throughput rises with tokens at steeply diminishing returns [moderate]
- **Jellyfish** (Arcolano, 2026-04-15; 12,000 developers at 200 companies in Q1 2026; about 7,500 with joinable token-to-PR data):
  - Monthly tokens: p50 51M; p90 380M.
  - Approximate monthly cost at Claude API pricing: p50 $52.38; p75 $226.58; p90 $691.14.
  - Quarterly spend and merged PRs: top 20% spent $1,822 for 23 PRs; bottom 20% spent $3 for 11.
  - Weekly PRs: 0.77 at low usage vs 2.15 at high usage.
  - Tokens per PR: about 7M at the median vs about 69M at the top decile (9.9×).
- **Jellyfish** (2026-07-27):
  - The p90 developer uses about 10× the median's tokens for about 2× the merged code.
  - Quality (bugs, escaped defects, reverts) showed no dramatic difference between low and high adopters.
  - Moving from zero to 100% adoption was associated with about 2× merged PRs, but the average organization saw only +27% epic throughput.
- **TechCrunch** (2026-04-17) cites Jellyfish (7,548 engineers) as concluding the tools produce volume rather than value. It also reports GitClear's 9.4× higher code churn for regular AI users.
- **Token Bill should** compute the org's own **marginal efficiency curve**: merged-PR throughput and quality by spend decile, at team level or with k-anonymous buckets. Use it to set tier ceilings where marginal PRs per dollar collapse, rather than picking round numbers.
- **Sources:**
  - https://jellyfish.co/blog/is-tokenmaxxing-cost-effective-new-data-from-jellyfish-explains/ (2026-04-15)
  - https://jellyfish.co/blog/why-the-real-roi-from-ai-isnt-showing-up-yet/ (2026-07-27)
  - https://techcrunch.com/2026/04/17/tokenmaxxing-is-making-developers-less-productive-than-they-think/ (2026-04-17)

### F17. Quality and review load can erase throughput gains, so the guardrail must cover quality as well as speed [moderate]
- **Faros 2025** (2025-07-23; 10,000+ developers, 1,255 teams):
  - at the individual level: tasks +21%, PRs merged +98%, review time +91%, bugs +9%, PR size +154%
  - at the company level: no significant correlation between AI adoption and improvement
- **Faros 2026** ("Acceleration Whiplash"; 22,000 developers, 4,000+ teams, 2 years of telemetry):
  - Throughput: epics per developer +66%, tasks +33.7%, PR merge rate +16.2%.
  - Quality: bugs per developer +54%, incidents-to-PR ratio +242.7%, code churn +861%.
  - Review: median review time +441.5%, PRs merged without review +31.3%.
- **DORA 2025** (~5,000 respondents, 2025-09-23):
  - 90% use AI at work; AI amplifies existing strengths and weaknesses
  - adoption correlates positively with throughput and negatively with delivery stability
  - seven AI capabilities: clear AI stance; healthy data ecosystems; AI-accessible internal data; strong version control; small batches; user-centric focus; quality internal platforms
- **Token Bill should** put a quality term in the value metric (reverts, incidents, change-failure rate, review time), so that "cheaper per PR" can't win by shipping worse PRs.
- **Sources:**
  - https://www.faros.ai/blog/ai-software-engineering (2025-07-23)
  - https://www.faros.ai/blog/ai-acceleration-whiplash-takeaways (2026, accessed)
  - https://cloud.google.com/blog/products/ai-machine-learning/announcing-the-2025-dora-report (2025-09-23)
  - https://cloud.google.com/blog/products/ai-machine-learning/introducing-doras-inaugural-ai-capabilities-model (2025-09-23)

### F18. Self-reported productivity can't guard cost cuts [strong]
- **METR** (2025-07-10; RCT, 16 experienced OSS developers, 246 issues):
  - AI-allowed tasks took **19% longer**
  - developers had forecast a 24% speedup, and afterwards believed they had been about 20% faster
- **METR update** (2026-02-24; 57 developers, 800+ tasks):
  - −18% (CI −38% to +9%) for returning developers
  - −4% (CI −15% to +9%) for new developers
  - 30–50% of developers avoided submitting tasks they preferred to do with AI, and some refused to work without AI
  - METR says the data is only very weak evidence of how large any speedup is
- **SPACE** (Forsgren et al., ACM Queue 2021): productivity can't be captured by one metric or by activity alone.
- **DX AI Measurement Framework** (2026-05-20):
  - utilization, impact and cost dimensions
  - cost metrics: AI spend per developer, **net time gain** (time saved minus AI spend), and **agent hourly rate** (human-equivalent hours / AI spend)
  - it strongly cautions against using the metrics for individual performance evaluation
  - Q1 2026 benchmarks: 3.9 h/week saved on average; 27.4% of merged code AI-authored; 2.4 vs 1.5 PRs/week for daily users vs non-users
- **Token Bill should:**
  - make objective, team-level, holdout-controlled throughput and quality the guardrail
  - treat surveys as secondary signals and label them as such
- **Sources:**
  - https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/
  - https://metr.org/blog/2026-02-24-uplift-update/
  - https://www.microsoft.com/en-us/research/publication/the-space-of-developer-productivity-theres-more-to-it-than-you-think/ (2021)
  - https://getdx.com/blog/ai-measurement-framework-guide/ (2026-05-20)

### F19. Vendor analytics normalize leaderboards and activity metrics [strong]
- Claude Code's Team/Enterprise analytics dashboard includes a **Leaderboard** of the top 10 contributors ranked by Claude Code usage, and "Export all users".
- It also reports suggestion accept rate and lines accepted, and pitches the leaderboard as a way to find power users.
- Contribution metrics (PRs with Claude Code) are deliberately conservative.
- **Implication:** enterprises will see leaderboards by default.
- **Token Bill should** position itself as the Goodhart-safe layer:
  - use team-level outcome metrics
  - advise admins to restrict the leaderboard views to enablement staff
  - never import per-user rankings into its own reports
- **Source:** https://code.claude.com/docs/en/analytics (accessed 2026-09-23)

### F20. Mandates tied to activity breed gaming [moderate]
- **Coinbase** (TechCrunch, 2025-08-22): Brian Armstrong required engineers to onboard to Copilot or Cursor within a week, and fired those who didn't without a valid reason. He called it heavy-handed; he and Collison voiced doubts about running AI-written codebases.
- **Accenture** (TechCrunch, 2026-06-24): moved from warning that non-users risked promotions to restricting AI on basic tasks. Its agentic AI lead said spend had become very unpredictable.
- **Meta** added token counts to performance reviews (Pragmatic Engineer, 2026-06-17).
- **Shopify** is reported to treat AI use as a KPI (The Register, 2026-04-26). I could not open the original April 2025 memo, so its specifics are **unverified** here.
- **Cursor** (blog, 2025-07-04) apologised for an unclear pricing change, refunded three weeks of unexpected charges and promised advance notice.
- **Token Bill should:**
  - recommend value-linked adoption goals (for example, share of merged PRs with passing tests that had AI assistance) at team level, instead of usage targets
  - include a "policy change comms" template (advance notice, what changes, how to request more) in the rollout kit
- **Sources:**
  - https://techcrunch.com/2025/08/22/coinbase-ceo-explains-why-he-fired-engineers-who-didnt-try-ai-immediately/
  - https://techcrunch.com/2026/06/24/companies-are-scrambling-to-stop-employees-from-maxing-out-ai-budgets-with-small-tasks/
  - https://newsletter.pragmaticengineer.com/p/why-is-meta-destroying-its-engineering
  - https://www.theregister.com/2026/04/26/ai_price_tag/
  - https://cursor.com/blog/june-2025-pricing

### F21. Showback is required, but its effect on AI spend is unproven; chargeback's analog (price) is more durable than suasion [weak–moderate]
- **FinOps Framework** (Invoicing & Chargeback capability):
  - showback is always required in any FinOps practice
  - chargeback depends on accounting policy and may be unwarranted when costs map to one cost center
  - its KPIs concern accuracy and timeliness, not behavior change
- **State of FinOps 2026** (1,192 respondents, $83B+ cloud spend):
  - 98% now manage AI spend (up from 63% in 2025 and 31% in 2024)
  - top challenges: visibility into AI pricing, allocating AI cost, and proving value
- I found no rigorous study of showback or chargeback effects on AI or cloud consumption. The nearest causal analogs:
  - invisible billing raises consumption 4% (Sexton)
  - price incentives outlast moral suasion (Ito et al.)
  - prepaid metering cuts use 14% (Jack & Smith)
- **Token Bill should:**
  - make team showback the default
  - gate chargeback exports on allocation coverage (for example ≥ 95% of spend attributed) and two stable quarters of showback
  - measure the showback-to-chargeback switch with a stepped-wedge rollout
- **Sources:**
  - https://www.finops.org/framework/capabilities/invoicing-chargeback/ (accessed)
  - https://data.finops.org/ (2026-02)
  - plus F7 and F9 sources

### F22. Education delivery: settings > hooks > DMs > PR checks > tips; spinner tips and announcements are low-salience [moderate]
- **Claude Code delivery primitives** (settings reference):
  - `companyAnnouncements`: one random entry per session at startup; the first entry on a person's very first launch.
  - `spinnerTipsOverride`: up to 200 tips, per-tip `cooldownSessions` (0–1000) and `priority`, a custom label, and `excludeDefault`.
  - Hooks' `systemMessage` shows a warning to the user.
  - `statusLine` runs any command.
- **Evidence:** from F9 and F12. Repeated, generic reminders lose force (Ancker). Workflow-integrated, precise, actionable findings sustain use (Tricorder, Nudge).
- **Token Bill should** generate:
  - a `spinnerTipsOverride` pack, with IDs and cooldowns, that rotates the top 5 org-specific waste fixes
  - a single `companyAnnouncements` entry pointing to the private self-view
- Behavior-specific nudges should go only through hooks and the status line, when the behavior happens.
- **Sources:** https://code.claude.com/docs/en/settings-reference ; https://code.claude.com/docs/en/hooks (accessed 2026-09-23)

### F23. Labor law makes per-developer cost visibility a co-determination matter in Germany and a monitoring matter across the EU, UK and New York [strong]
- **BetrVG** (official English translation incl. amendments to 19 July 2024):
  - §87(1) no. 6 gives works councils co-determination over introducing and using technical devices designed to monitor employees' behavior or performance
  - §80(3) deems an AI expert necessary when the works council assesses AI
  - §90 requires informing the works council about working procedures that use AI
- **Orrick** (2024-09-17) on the Hamburg labour court's ChatGPT decision: devices that are **objectively capable** of monitoring trigger co-determination. An AI tool on company devices gives the employer information about who uses it, when, why and how often.
- **GDPR Art. 88** allows Member State rules for employee data, with safeguards covering transparency and workplace monitoring systems. WP29 Opinion 2/2017 on data processing at work was adopted 23 June 2017.
- **UK ICO** (monitoring workers):
  - a DPIA is required before high-risk monitoring
  - consent is usually inappropriate in employment
  - use the least intrusive means
  - workers should be able to see and challenge monitoring results used in performance management
  - the guidance is under review after the Data (Use and Access) Act
- **EU AI Act:**
  - Annex III 4(b): AI used to monitor and evaluate workers' performance and behavior is high-risk
  - Art. 26(7): employer deployers must inform workers' representatives and affected workers before putting such a system into service
  - Art. 5(1)(f): AI that infers emotions in the workplace is prohibited
  - per the artificialintelligenceact.eu timeline, Annex III obligations apply from **2 December 2027**
- **New York Civil Rights Law §52-c:** prior written notice of monitoring email, phone or internet usage, acknowledged by the employee and posted.
- **Unverified here:** France's CNIL fined Amazon France Logistique €32M over granular warehouse productivity indicators (HN title, 2024-01-23). The CNIL page has since been withdrawn, so I couldn't read the decision.
- **Token Bill should:**
  - ship aggregation-by-default
  - generate a data inventory plus a DPIA and works-council pack (purposes, fields, retention, access matrix, "not for performance evaluation" clause)
  - offer a no-content mode
  - never infer sentiment or emotion from prompts
  - if it adds ML scoring of individuals, treat that as potentially Annex III high-risk
- **Sources:**
  - https://www.gesetze-im-internet.de/englisch_betrvg/englisch_betrvg.html
  - https://www.orrick.com/en/Insights/2024/09/AI-and-German-Co-Determination-What-Employers-Need-to-Know
  - https://gdpr-info.eu/art-88-gdpr/
  - https://ec.europa.eu/newsroom/article29/items/610169
  - https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/employment/monitoring-workers/data-protection-and-monitoring-workers/
  - https://artificialintelligenceact.eu/annex/3/
  - https://artificialintelligenceact.eu/article/26/
  - https://artificialintelligenceact.eu/article/5/
  - https://artificialintelligenceact.eu/implementation-timeline/
  - https://www.nysenate.gov/legislation/laws/CVR/52-C*2

### F24. An acceptable aggregation level: team-level with k ≥ 5, plus private self-view [moderate]
- **Microsoft Viva Insights** (privacy settings, 2025-11-20):
  - the minimum group size (aggregation threshold) must be **at least 5**
  - end-user opt-out removes behavioral metrics from row-level outputs, but notably not Copilot usage data
- **DX** discourages individual-level use of AI metrics.
- **Claude apps gateway** retention defaults: identity 90 days; spend counters 13 months, keyed on pseudonymous OIDC `sub`. It warns that `q=`/`user_ids[]` query strings end up in proxy logs.
- **GitHub's Copilot metrics API** now offers per-user daily reports, so the market is moving toward *more* individual data, not less.
- **Token Bill defaults:**
  - Team views with k ≥ 5 and complementary suppression.
  - The individual sees their own data only, as a "personal mirror".
  - Named per-person data only through a logged break-glass path for runaway-spend incidents, with its scope agreed with the works council.
  - Pseudonymous IDs with rotating salts.
  - 90-day individual retention and 13-month aggregates.
- **Sources:**
  - https://learn.microsoft.com/en-us/viva/insights/advanced/setup-maint/privacy-settings
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
  - https://docs.github.com/en/rest/copilot/copilot-metrics (accessed)
  - https://getdx.com/blog/ai-measurement-framework-guide/

### F25. Experiment design for thousands of developers: cluster randomization, stepped-wedge rollout, variance reduction [strong, methods]
- **CUPED** (Deng, Xu, Kohavi & Walker, WSDM 2013): uses pre-period data to cut metric variance by about **50%** on Bing, giving the same power with half the users or half the time.
- **Stepped-wedge cluster randomised trials** (Hemming et al., BMJ 2015): every cluster eventually gets the intervention, and the rollout order is randomized. That suits "everyone gets the policy, but we want causal estimates".
- **DellaVigna & Linos:** practitioners forecast nudge effects well, but academic effect sizes are inflated, so pre-register the effect you expect.
- **Token Bill should** ship an experiment harness:
  - team-level randomization and stepped waves
  - CUPED-adjusted difference-in-differences on throughput, quality and cost
  - a persistent 5–10% holdout for decay
  - pre-registration of the minimum detectable effect (MDE) and stop rules
- **Sources:**
  - https://doi.org/10.1145/2433396.2433413 (2013)
  - https://doi.org/10.1136/bmj.h391 (2015-02-06)
  - NBER w27594

### F26. Reasons to cap agent sessions and managed agents separately from humans [moderate]
- **Uber** separates tiers for interactive harnesses and managed agents. It measures managed agents by cost per merged PR, review, alert or cleanup, with revert rate, F1 and MTTR.
- **Claude Code docs** list idle-time spenders:
  - scheduled `/loop` tasks
  - cross-session messages
  - goal check-ins (capped at 3 idle check-ins per goal)
  - agent teammates that keep consuming until they exit
- **Token Bill should:**
  - attribute agent and automation spend to the owning team as "machine" spend, with outcome units per agent type
  - apply circuit breakers (F6) rather than human-style nudges
- **Sources:**
  - https://www.uber.com/us/en/blog/efficient-software-factory/
  - https://code.claude.com/docs/en/costs

---

## 4. Value-adjusted cost (VAC) and the throughput alarm

**Principles.**

- Measure at team level (k ≥ 5).
- Outcomes must be objective and quality-weighted.
- Every cost change is judged against a holdout.
- Surveys are secondary.

This follows F16–F18, F24 and F25.

**Inputs per team *t*, week *w*:**

- `S`: billed AI spend at contracted rates. Sources: Admin/Analytics API, Claude Code OTel `cost_usd` with the `modelPricing` managed setting, and gateway meters. Split into interactive and managed-agent spend.
- `PR`: merged PRs (human + agent-authored; DX counts agent PRs in team throughput).
- `q(PR)`: a quality weight.
  - 0 if the PR is reverted within 30 days, or linked to a Sev-1/2 incident within 30 days.
  - 0.5 if it is followed by a fix-forward touching the same lines within 14 days.
  - 1 otherwise.
  - Configurable.
- `U = Σ q(PR)`: quality-adjusted outcome units.
  - Optional size normalization: bucket PRs by changed-lines deciles to resist PR splitting.
  - Alternative unit: completed tickets or epics.
- `Guard`:
  - PR cycle time (first commit to merge)
  - review time
  - change-failure rate / revert rate
  - count of cap-block events and approval latency
  - optional pulse survey (DX-style)

**Metrics.**

1. **Cost per quality-adjusted outcome:** `CPQ = S / U`. This is the headline.
2. **Marginal efficiency:** `ME_d = ΔU / ΔS` across spend deciles *d* (a Jellyfish-style curve). Tier ceilings go where `ME_d` falls below a threshold, for example below 25% of the median decile's `U/S`.
3. **Net value (DX-style, secondary):** `NV = H_saved × r_loaded − S`. `H_saved` comes from survey or human-equivalent-hours estimates, so label it "self-reported" (METR).
4. **Value-adjusted cost change from intervention *i*:** `VAC_i = (S₁/U₁ ÷ S₀/U₀)_treated ÷ (S₁/U₁ ÷ S₀/U₀)_holdout`. A value below 1 is good.
5. **Break-even test:** an intervention is net-positive only if `ΔS_saved > ΔU_lost × V_unit`.
   - `V_unit` is the org's value of a quality-adjusted PR. The default proxy is loaded engineering cost per PR, set from HR inputs, never guessed.

**Throughput alarm (per intervention, per cohort).**

- **Estimator:** CUPED-adjusted difference-in-differences of weekly `U` per active developer (treated vs holdout). Use the 4–8 pre-weeks as the covariate and cluster-robust SEs by team.
- **Fire an ALARM when any of these holds:**
  - the one-sided 90% lower confidence bound of the throughput change is below −5% (pre-registered MDE; adjustable)
  - revert or change-failure rate rises by more than 2 pp
  - median PR cycle time rises by more than 15%
  - cap-block events exceed 1 per 20 developer-weeks with approval latency above 4 business hours
  - the break-even test fails at the point estimate
- **Action:** recommend rollback of that lever only (one lever per diff, following Anthropic's cost-optimization guidance), and open an incident note.
- **Decay watch:** re-estimate at weeks 2, 6 and 12 against the persistent holdout, then flag a nudge whose effect halves (Allcott & Rogers pattern).

The thresholds above are starting points for the pilot to calibrate. They are not taken from a source.

---

## 5. Rollout playbook: pilot, then cohorts, then organization

**Phase 0: governance and baseline (weeks 0–4).**

- Governance:
  - DPIA and works-council agreement (DE/AT/NL/FR), a purpose limitation that excludes performance evaluation, and employee notice (NY §52-c style).
  - Turn on no-content telemetry: Claude Code OTel with prompts redacted, gateway meters, and the Analytics API.
  - Set `modelPricing` to contracted rates.
- Outcomes:
  - Join git/PR/CI/incident data at team level.
  - Choose an eval harness for capability trades (Datadog used 140+ internal evals).
- Record 4+ weeks of baseline: spend distribution, CPQ, marginal efficiency curve, top waste causes, sticky escalations.
- Guardrails to baseline: throughput, cycle time, reverts, review time, cap events.

**Phase 1: pilot (weeks 4–10). 2–4 volunteer teams (50–150 developers) plus a matched holdout.**

- Levers:
  - (a) defaults on the pilot only: effort `medium`, `fastModePerSessionOptIn`, subagent `model: haiku` where evals allow, auto-compact window
  - (b) the personal-mirror status line
  - (c) the `SessionStart` resume-cost nudge
  - (d) a private daily-threshold DM
- Go/no-go rules:
  - The throughput alarm must not fire.
  - Each nudge's "not useful" rate must stay below 10%.
  - Measured savings must be at least 50% of the simulated savings.

**Phase 2: cohorts (weeks 10–22). A stepped wedge across 6–10 cohorts in random order, 2–3 weeks per wave.**

- Add:
  - opt-in PR cost checks
  - soft tiers with approval flow and 50/80/100% alerts
  - the runaway circuit breaker
  - the spinner-tip pack
- Measure the effect per wave and the decay per cohort.
- Kill any lever that fails break-even.

**Phase 3: organization (week 22+).**

- Roll out everywhere, and keep a rotating 5% holdout for at least 2 quarters.
- Re-run default policy what-ifs at each model release; defaults drift as vendors change them (for example, Opus 5.5 now defaults to `medium`).
- Consider chargeback only after allocation coverage reaches 95% or more for two quarters.
- Report quarterly: CPQ trend, marginal efficiency curve, savings by lever with CIs, guardrail status.

**Guardrail metrics for every phase.**

| Metric | Level | Alarm (starting point) |
|---|---|---|
| Quality-adjusted merged PRs / active developer / week (CUPED DiD) | team | lower 90% CB < −5% |
| Revert or change-failure rate | team | +2 pp |
| PR cycle time (median) | team | +15% |
| Review time (median) | team | +20% |
| Cap-block events; approval latency | team | > 1 per 20 dev-weeks; > 4 business hours |
| Nudge "not useful" rate | nudge type | > 10% → auto-disable |
| Pulse satisfaction / "AI slowed me down" | team | −0.3 on a 5-point scale (secondary) |
| Shadow-AI indicator (personal-account usage, if measurable) | org | rising after a cap |

The last row is a hypothesis: caps may push usage to personal accounts, which DX mentions as "shadow AI". It is not measured in any source I found.

---

## 6. What Token Bill should build

Build order follows the evidence: defaults and policy first, then outcome join and guardrails, then nudges, then budgets, all under privacy-by-default.

### B1. Policy simulator and managed-settings compiler (P0, effort L)
- **Input:** fleet telemetry (OTel, Analytics API, gateway logs, or Token Bill traces).
- **What-ifs to price:**
  - default model per role
  - default effort and max effort
  - `fastModePerSessionOptIn`
  - `availableModels`
  - `autoCompactWindow`
  - `promptCacheTtl`
  - subagent model
- **Output:**
  - dollars with CIs
  - affected share of spend and users
  - the capability-trade flag, with a required eval hook (bring-your-own eval, or Anthropic-style effort sweep results)
- **Output artifacts:**
  - `managed-settings.json` patch
  - Claude admin API / gateway spend-limit calls
  - GitHub/Cursor budget configs
  - rollout plan
- **Sticky-escalation detector (F2):** users whose saved effort, model or fast mode sits above the org default for more than N days while their task mix is small.

### B2. Outcome join and value-adjusted cost (P0, effort M)
- Connectors: GitHub/GitLab (merged PRs, reverts, PR size), CI (pass/fail), incidents (PagerDuty/Jira), Claude Code Analytics contribution metrics.
- Metrics: CPQ, marginal efficiency curve by spend decile, managed-agent cost per outcome (Uber-style), and DX-style net time gain labeled as self-reported.
- Anti-gaming: size-bucketed PR units, quality weights, and team-level only.

### B3. Guardrail and experiment engine (P0, effort M)
- Team-level randomization, stepped waves, persistent holdout, CUPED DiD, pre-registration file, alarm rules (Section 4), and a per-lever rollback recommendation.
- Every Token Bill recommendation carries a verification record: simulated vs realized savings, and guardrail status.

### B4. Nudge engine (P1, effort M)
- **Triggers:** event-based, at the point of decision.

| ID | Trigger | Channel | Message pattern (injunctive + $ + one fix) | Convert-to-config |
|---|---|---|---|---|
| N1 | Resume with `prompt_cache_likely_expired` and `estimated_cache_write_usd` > $X | `SessionStart` hook `systemMessage` | "Resuming re-sends N tokens (~$Y). If the task changed, `/clear` is free." | Offer `promptCacheTtl`/resume-from-summary settings |
| N2 | Kitchen-sink session (topic shifts, context > T, low cache reuse) | status line + `UserPromptSubmit` hook warning | "This session is carrying ~$Z/turn of unrelated context. Most sessions in this repo clear between tickets." | None (habit); track |
| N3 | Correction loop (≥ 3 corrections on the same issue) | hook warning | Anthropic's rule: `/clear` + rewrite the prompt with what you learned | Offer the spec template skill |
| N4 | Sticky escalation (xhigh/max/fast persisted > 7 days on small diffs) | private DM | "$W/week above team default; your recent tasks are small." | One click: session-only effort, `fastModePerSessionOptIn` |
| N5 | Idle agent, loop or scheduled task on large context with no human turn | DM + circuit breaker | "Loop X re-sent 400k tokens 96×/day" | Pause, or set cadence and cap |
| N6 | Daily spend > personal p95 and > team threshold | private DM (Datadog-style) | Top 3 causes with $ and fixes | Links to B1 patches |
| N7 | Top reasoning tier on trivial turns (short prompt, tiny diff) | status line hint | Parikh-list waste framing | Per-role effort cap suggestion |
| N8 | PR merged with high cost-per-line or a waste pattern | **opt-in** PR check (author-visible; team default off in EU) | Top driver + fix | Hook/CLAUDE.md patch PR |

- **Frequency caps:** at most 1 interruptive nudge per session; at most 3 per person per week; a cooldown per nudge type (mirrors `spinnerTipsOverride.cooldownSessions`); suppress repeats (Ancker).
- **Precision gate:** thumbs up/down on every nudge, and auto-disable a type whose "not useful" rate is 10% or more over a rolling 200 impressions (Tricorder).
- **Framing rules:**
  - Descriptive + injunctive (Schultz).
  - Efficiency, never raw spend rank.
  - Private by default.
  - Environmental or "shared budget" framing may beat pure cost (Asensio & Delmas). Test it.
  - Never show peer medians to low-usage users (boomerang).
- **Holdout:** 10% per nudge type at person-week level, and report effect and decay.

### B5. Personal mirror (P1, effort S)
- A status-line script:
  - session and day cost at contracted rates, cache warm/cold, and spend-limit percentage
  - one rotating fix drawn from the user's own top waste
- A weekly private digest to self only: the user's own CPQ trend vs their own baseline, with no ranking.
- A generator for `spinnerTipsOverride` (IDs, cooldowns, priorities) and a single `companyAnnouncements` entry.

### B6. Budget and cap designer (P1, effort M)
- Simulates per-user vs pooled vs tiered caps on history. Outputs:
  - would-have-blocked events
  - the **collateral-damage report** (PRs, deploys and incidents from blocked developer-days) (F7)
  - enterprise-vs-user budget consistency checks (GitHub's warning)
- Recommends:
  - soft tiers with expected-spend alerts (50/80/100, or 75/95)
  - a shared cross-harness tier
  - an approval SLA, and a metric for request-to-approve latency
- Circuit breaker for runaway spend, distinct from caps (F6).

### B7. Goodhart audit and anti-gaming detectors (P1, effort S)
- Flags metrics in use that reward input: tokens, sessions, "% using AI weekly", AI usage in reviews.
- Detects tokenmaxxing signatures as waste:
  - agents with no human turns
  - trivial prompts on the top tier
  - turn fragmentation
  - duplicate-question re-asks
  - fabricated busywork (long sessions with no diff, commit or PR)

### B8. Privacy and labor compliance mode (P0, effort M)
- **Defaults:**
  - no prompt or response content
  - team aggregation with k ≥ 5 and complementary suppression
  - pseudonymous IDs (salted OIDC `sub`)
  - retention: 90 days individual, 13 months aggregates
  - self-view only for individual data
  - break-glass access with an audit log
  - **no sentiment or emotion inference** (AI Act Art. 5(1)(f))
- **Generators:**
  - data inventory
  - DPIA draft
  - works-council agreement annex (purpose limitation, "not for performance evaluation", access matrix, deletion)
  - employee notice template
- **Jurisdiction presets:** DE/AT (co-determination), FR, UK (ICO), US-NY (notice), and default.

### B9. Education kit (P2, effort S)
- A CLAUDE.md/AGENTS.md linter: smells from dos Santos et al. plus per-session token carry, and a per-repo A/B recommendation (F15).
- A spec template and "interview, then spec, then fresh session" skill (F14), a correction-loop guide, and plan-mode-when guidance (F13).

---

## 7. Anti-patterns Token Bill must not build (and should flag)

1. **Public leaderboards of tokens or spend, in either direction.** A "least spend" board invites under-use and gaming (F4).
2. **Token or usage counts in performance reviews,** or activity-based adoption targets such as "80% weekly usage" (F4, F20).
3. **Hard per-user caps without fast approval and a collateral-damage check** (F7, F8).
4. **Per-tool caps** that shift usage between tools. Prefer one shared tier across harnesses (F5).
5. **Individual cost KPIs or individual cost-per-PR targets.** They invite PR splitting and avoidance of hard tasks (F4, F17).
6. **Dashboards as the main behavior lever** (F12).
7. **Frequent, repeated, generic nudges** (F9).
8. **Descriptive-only peer comparisons, or showing medians to low users** (F10).
9. **Justifying spend with self-reported speedups** (F18).
10. **Changing defaults without an eval or throughput holdout** (F1).
11. **Capturing prompt content, or inferring sentiment or emotion, for cost analytics** (F23).
12. **Surprise policy or pricing changes without notice** (F20).

---

## 8. Open questions

1. Does the enterprise already run an eval harness (as Datadog does) to gate model and effort default changes? Without one, B1 can only propose capability trades, not apply them.
2. Which jurisdictions are the developers in? Works-council agreements in DE/AT/NL/FR can add 2–6 months to Phase 0. That timeline is my estimate from typical process, not from a source.
3. Is the Claude Code analytics leaderboard enabled, and who can see it? Is AI usage used in performance reviews today?
4. Datadog's $150k first-week nudge figure has no stated counterfactual or persistence. Can Datadog, or our own pilot, measure week-12 effects against a holdout?
5. There is no rigorous published evidence on showback vs chargeback effects on AI or cloud consumption. Our stepped-wedge rollout could produce the first.
6. How much do per-user caps push usage to personal accounts (shadow AI)? No data found.
7. What is the right `V_unit` (value of a quality-adjusted PR) for break-even tests? It needs Finance and HR input.
8. Primary sources I could not open: the Shopify April 2025 memo; the CNIL Amazon France Logistique decision (page withdrawn); the Axios $500M report; the Bloomberg Uber cap article. Also the full text of Parikh's email (404 Media is paywalled; the quote comes via LeadDev).
9. The Jellyfish context-file finding (+29% per doubling) conflicts with Gloaguen et al. (+20% cost, no success gain). Per-repo A/B testing is needed before any org-wide "write more context" push.

---

## 9. Source list (all opened during this pass)

**Company and news reports.**

- Datadog, "How Datadog saves money by optimizing AI usage", 2026-08-26 — https://www.datadoghq.com/blog/how-datadog-saves-money-by-optimizing-ai-usage/
- TechCrunch (Bellan), "The token bill comes due", 2026-06-05 — https://techcrunch.com/2026/06/05/the-token-bill-comes-due-inside-the-industry-scramble-to-manage-ais-runaway-costs/
- TechCrunch (Ropek), "Uber caps employee AI spending…", 2026-06-02 — https://techcrunch.com/2026/06/02/uber-caps-employee-ai-spending-after-blowing-through-budget-in-four-months/
- Simon Willison, "Uber's $1,500/month AI limit…", 2026-06-03 — https://simonwillison.net/2026/Jun/3/uber-caps-usage/
- Fortune (Angelo), Uber COO, 2026-05-26 — https://fortune.com/2026/05/26/uber-coo-ai-spending-tokens-claude-code/
- Uber Engineering (Medisetty), "Running a Software Factory Efficiently at Uber Scale", 2026-08-27 — https://www.uber.com/us/en/blog/efficient-software-factory/
- LeadDev (Kapani), "The tokenmaxxing hype didn't last long", 2026-08-17 — https://leaddev.com/reporting/the-tokenmaxxing-hype-didnt-last-long
- 404 Media (Maiberg), Microsoft memo, 2026-08-04 — https://www.404media.co/microsoft-tells-engineers-tokenmaxxing-is-not-what-we-are-optimizing-for/
- Sources (Heath), "Microsoft cracks down on tokenmaxxing", 2026-08-05 — https://sources.news/p/microsoft-cracks-down-on-tokenmaxxing
- 404 Media, Amazon leaderboard, 2026-06-01 — https://www.404media.co/amazon-shuts-down-internal-ai-leaderboard-after-employees-cheated/
- The Decoder (Schreiner), Amazon KiroRank, 2026-05-29 — https://the-decoder.com/amazon-kills-internal-ai-leaderboard-after-employees-gamed-it-with-pointless-tasks/
- MLQ, Amazon, 2026-05-30 — https://mlq.ai/news/amazon-scraps-internal-ai-leaderboard-after-employees-gamed-it-with-fake-tasks/
- The Decoder (Bastian), Meta leaderboard, 2026-04-07 — https://the-decoder.com/meta-employees-compete-for-token-consumption-on-an-internal-ai-leaderboard/
- MLQ, Meta leaderboard, 2026-04-08 — https://mlq.ai/news/meta-makes-internal-leaderboard-for-employee-ai-token-usage/
- MLQ, Meta caps, 2026-06-13 — https://mlq.ai/news/meta-caps-internal-ai-token-spending-after-costs-approach-billions-in-2026/
- The Pragmatic Engineer, "Is Meta destroying its engineering organization?", 2026-06-17 — https://newsletter.pragmaticengineer.com/p/why-is-meta-destroying-its-engineering
- The Pragmatic Engineer, "Tokenmaxxing as a weird new trend", 2026-04-16 (preview only) — https://newsletter.pragmaticengineer.com/p/the-pulse-tokenmaxxing-as-a-weird
- TechCrunch (Ropek), "Companies are scrambling…", 2026-06-24 — https://techcrunch.com/2026/06/24/companies-are-scrambling-to-stop-employees-from-maxing-out-ai-budgets-with-small-tasks/
- TechCrunch, Coinbase, 2025-08-22 — https://techcrunch.com/2025/08/22/coinbase-ceo-explains-why-he-fired-engineers-who-didnt-try-ai-immediately/
- Fortune (Fore), Cognizant CEO, 2026-06-01 — https://fortune.com/2026/06/01/cognizant-ceo-ravi-kumar-s-hiring-entry-level-tokenmaxxing-vanity-metric/
- The Register (Claburn), 2026-04-26 — https://www.theregister.com/2026/04/26/ai_price_tag/
- TechCrunch (Fernholz), "Tokenmaxxing is making developers less productive…", 2026-04-17 — https://techcrunch.com/2026/04/17/tokenmaxxing-is-making-developers-less-productive-than-they-think/
- Cursor, "Clarifying our pricing", 2025-07-04 — https://cursor.com/blog/june-2025-pricing

**Measurement and value.**

- Jellyfish (Arcolano), 2026-04-15 — https://jellyfish.co/blog/is-tokenmaxxing-cost-effective-new-data-from-jellyfish-explains/
- Jellyfish (Arcolano, Albarran), 2026-07-27 — https://jellyfish.co/blog/why-the-real-roi-from-ai-isnt-showing-up-yet/
- Faros AI, Productivity Paradox, 2025-07-23 — https://www.faros.ai/blog/ai-software-engineering
- Faros AI, Acceleration Whiplash (2026) — https://www.faros.ai/blog/ai-acceleration-whiplash-takeaways
- Faros AI (Gordon), "From token maxxing to outcome maxxing", 2026-09-21 — https://www.faros.ai/blog/from-token-maxxing-to-outcome-maxxing
- DX (Bruneaux), AI Measurement Framework, 2026-05-20 — https://getdx.com/blog/ai-measurement-framework-guide/
- METR, 2025-07-10 — https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/
- METR, 2026-02-24 — https://metr.org/blog/2026-02-24-uplift-update/
- Google Cloud, 2025 DORA report, 2025-09-23 — https://cloud.google.com/blog/products/ai-machine-learning/announcing-the-2025-dora-report
- Google Cloud, DORA AI Capabilities Model, 2025-09-23 — https://cloud.google.com/blog/products/ai-machine-learning/introducing-doras-inaugural-ai-capabilities-model
- Forsgren et al., SPACE, ACM Queue 2021 — https://www.microsoft.com/en-us/research/publication/the-space-of-developer-productivity-theres-more-to-it-than-you-think/

**Vendor documentation (accessed 2026-09-23).**

- Claude Code costs — https://code.claude.com/docs/en/costs
- Claude Code best practices — https://code.claude.com/docs/en/best-practices
- Claude Code fast mode — https://code.claude.com/docs/en/fast-mode
- Claude Code model configuration — https://code.claude.com/docs/en/model-config
- Claude Code settings reference — https://code.claude.com/docs/en/settings-reference
- Claude Code analytics — https://code.claude.com/docs/en/analytics
- Claude Code hooks — https://code.claude.com/docs/en/hooks
- Claude apps gateway spend limits — https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
- Claude Enterprise consumption guide — https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide
- Anthropic claude-api skill `shared/cost-optimization.md` (local, Claude Code v2.1.280 bundle)
- GitHub budgets guidance — https://docs.github.com/en/copilot/tutorials/budgets/optimizing-your-budget-configuration
- GitHub budget requests — https://docs.github.com/en/copilot/how-tos/administer-copilot/manage-budget-requests
- GitHub Copilot metrics API — https://docs.github.com/en/rest/copilot/copilot-metrics
- Cursor admin API — https://cursor.com/docs/account/teams/admin-api.md
- Cursor pooled usage — https://cursor.com/docs/enterprise/pooled-usage.md
- FinOps Invoicing & Chargeback — https://www.finops.org/framework/capabilities/invoicing-chargeback/
- State of FinOps 2026 — https://data.finops.org/
- Microsoft Viva Insights privacy settings, 2025-11-20 — https://learn.microsoft.com/en-us/viva/insights/advanced/setup-maint/privacy-settings

**Academic.**

- Allcott 2011, J. Public Econ — https://doi.org/10.1016/j.jpubeco.2011.03.003
- Allcott & Rogers 2014, AER — https://www.aeaweb.org/articles?id=10.1257/aer.104.10.3003
- Ito, Ida & Tanaka 2018, AEJ: Policy — https://www.aeaweb.org/articles?id=10.1257/pol.20160093
- Brandon et al., NBER w23277 (2017, rev. 2022) — https://www.nber.org/papers/w23277
- Gilbert & Graff Zivin, NBER w19510 (2013) — https://www.nber.org/papers/w19510
- Sexton 2015, REStat — https://doi.org/10.1162/rest_a_00465
- Chetty, Looney & Kroft 2009, AER — https://doi.org/10.1257/aer.99.4.1145
- Jack & Smith 2020, AEJ: Applied — https://doi.org/10.1257/app.20180155
- Brot-Goldberg et al., NBER w21632 / QJE 2017 — https://www.nber.org/papers/w21632
- Schultz et al. 2007, Psych Sci — https://doi.org/10.1111/j.1467-9280.2007.01917.x
- Asensio & Delmas 2015, PNAS — https://doi.org/10.1073/pnas.1401880112
- Jachimowicz et al. 2019, BPP — https://doi.org/10.1017/bpp.2018.43
- Mertens et al. 2022, PNAS — https://doi.org/10.1073/pnas.2107346118
- Maier et al. 2022, PNAS — https://doi.org/10.1073/pnas.2200300119
- DellaVigna & Linos, NBER w27594 / Econometrica 2022 — https://www.nber.org/papers/w27594
- Bergquist et al. 2023, PNAS — https://doi.org/10.1073/pnas.2214851120
- Feldman et al. 2013, JAMA IM — https://doi.org/10.1001/jamainternmed.2013.232
- Sedrak et al. 2017, JAMA IM — https://doi.org/10.1001/jamainternmed.2017.1144
- Ryskina et al. 2018, JGIM — https://doi.org/10.1007/s11606-018-4482-y
- Ancker et al. 2017, BMC MIDM — https://doi.org/10.1186/s12911-017-0430-8
- Blanes i Vidal & Nossol 2011, Mgmt Sci — https://doi.org/10.1287/mnsc.1110.1383
- Kerr 1975, AMJ — https://doi.org/10.5465/255378
- Manheim & Garrabrant 2018 — https://arxiv.org/abs/1803.04585
- Sadowski et al. 2015, ICSE (Tricorder) — https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/
- Maddila et al. 2023, TOSEM (Nudge) — https://doi.org/10.1145/3544791
- Deng et al. 2013, WSDM (CUPED) — https://doi.org/10.1145/2433396.2433413
- Hemming et al. 2015, BMJ — https://doi.org/10.1136/bmj.h391
- Smékal 2026 — https://arxiv.org/abs/2608.25399
- Lulla et al. 2026 — https://arxiv.org/abs/2601.20404
- Gloaguen et al. 2026 — https://arxiv.org/abs/2602.11988
- dos Santos et al. 2026 — https://arxiv.org/abs/2606.15828

**Law and regulation.**

- BetrVG, official English translation (incl. amendments to 2024-07-19) — https://www.gesetze-im-internet.de/englisch_betrvg/englisch_betrvg.html
- Orrick, 2024-09-17 — https://www.orrick.com/en/Insights/2024/09/AI-and-German-Co-Determination-What-Employers-Need-to-Know
- GDPR Art. 88 — https://gdpr-info.eu/art-88-gdpr/
- WP29 Opinion 2/2017 (adopted 2017-06-23) — https://ec.europa.eu/newsroom/article29/items/610169
- ICO, monitoring workers — https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/employment/monitoring-workers/data-protection-and-monitoring-workers/
- EU AI Act Annex III — https://artificialintelligenceact.eu/annex/3/
- EU AI Act Art. 26 — https://artificialintelligenceact.eu/article/26/
- EU AI Act Art. 5 — https://artificialintelligenceact.eu/article/5/
- EU AI Act implementation timeline — https://artificialintelligenceact.eu/implementation-timeline/
- NY Civil Rights Law §52-c — https://www.nysenate.gov/legislation/laws/CVR/52-C*2
