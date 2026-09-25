# Verification: gap-org-behavior-value-guardrails

Verified 2026-09-23 by an adversarial fact-check pass. For each finding, the cited sources were re-opened with WebFetch or curl. Academic abstracts came from Crossref, Europe PMC, OpenAlex, Semantic Scholar and the arXiv API. Vendor docs were read from the raw `.md` where one is available. Web-search quota was exhausted, so only direct fetches were used.

**Tally:** 23 confirmed, 3 corrected, 0 unverifiable, 0 refuted.

| id | verdict |
|---|---|
| org-defaults-datadog | confirmed |
| cc-sticky-escalation | confirmed |
| defaults-beat-nudges-meta | confirmed |
| leaderboard-goodhart | confirmed |
| uber-policy-arc | confirmed |
| runaway-circuit-breaker | confirmed |
| blunt-caps-collateral | confirmed |
| enforcement-primitives | confirmed |
| nudge-decay-durability | confirmed |
| peer-comparison-outliers | **corrected** (units) |
| price-salience-mixed | confirmed |
| channel-code-review-hooks | confirmed (caveat) |
| anthropic-habits | confirmed |
| spec-quality | confirmed |
| context-files-mixed | confirmed |
| marginal-returns-curve | confirmed |
| quality-guardrail | confirmed |
| self-report-unreliable | **corrected** (METR sign) |
| value-adjusted-cost-metric | confirmed (synthesis) |
| vendor-leaderboards | **corrected** |
| mandates-gaming | confirmed |
| showback-chargeback | confirmed |
| education-channels | confirmed |
| labor-law-constraints | confirmed (caveat) |
| aggregation-k5 | confirmed |
| experiment-design | confirmed |
| machine-spend-separate | confirmed |

---

## Notes per finding

### org-defaults-datadog: confirmed
**Datadog blog** (2026-08-26; Bowen Chen, Yosra Chelbi, Nader Khalil, Dmitrii Ulianin), headline "saves over $1 million each month".
- Verbatim: "the default model for Claude Code and our agent skills was Opus 4.8". The switch to Sonnet 4.6 was "an 8% loss of proficiency in executing Datadog workflows while reducing AI costs by 36.7%".
- Savings from the switch: "over $687,000", measured over the past month.
- Evals: "over 140 different evaluations", nightly, with "weighted scores between deterministic results and LLM evaluations". They measured Datadog-specific tasks, not general coding.
- Effort: "changed the default effort level for Claude Code CLI from high to medium ... over $288,000 in monthly savings".
- Alerts: "768 distinct users triggered this alert" in the first week, on Anthropic provisioned keys. The method was "comparing the week before and after users received the alert", giving ">$150,000". That is a pre/post comparison with no control group. The daily-threshold DM example in the post is Cursor spend.

**Local effort curves:** `claude-api/shared/cost-optimization.md` (bundle 2.1.280), line 162: "Claude Opus 5 gave up about 2 points at `medium` for half the cost, and about 8 points at `low` for a quarter of it."

### cc-sticky-escalation: confirmed
- **model-config:** "`Enter`: switch model and save as your default" and "`s`: ... for this session only". Unless `CLAUDE_CODE_EFFORT_LEVEL` is set, `max` is session-only. Opus 5.5 defaults to `medium`.
- **Enterprise controls (model-config):** org default model "for the whole organization or per custom role"; per-role max effort per model; `maxEffortLevel` works "On any plan and any provider".
- **fast-mode:**
  - Pricing: "$8/$40 on Opus 5.5".
  - Persistence: "By default, fast mode you turn on in an interactive session persists across sessions". The fix is `fastModePerSessionOptIn: true`.
  - Team and Enterprise: "fast mode is disabled by default for Team and Enterprise organizations".
- **Standard Opus 5.5 price** is $4/$20 (local `shared/models.md` line 77 and `model-migration.md` line 1864), so fast mode is 2×.
- **settings-reference** lists `fastModePerSessionOptIn` and `maxEffortLevel`.

### defaults-beat-nudges-meta: confirmed
- **Jachimowicz et al.** (Crossref abstract; BPP; online 2019-01-24): 58 studies, pooled n = 73,675, d = 0.68 (95% CI 0.53–0.83).
- **Mertens et al.** (Europe PMC abstract):
  - more than 200 studies, 450+ effect sizes, n = 2,149,683; d = 0.45 (0.39–0.52)
  - quote: "decision structure consistently outperform ... decision information"
  - moderate publication bias
  - dates: online 2021-12-30, issue 2022-01-04
- **Maier et al.** (PMC9351501 full text): "no evidence for the effectiveness of nudges remains". The letter adds: "all intervention categories and domains apart from 'finance' show evidence for heterogeneity, which implies that some nudges might be effective".
- **DellaVigna & Linos** (NBER w27594, July 2020): 126 RCTs, 23M+ people. Academic effects averaged 8.7 pp and nudge-unit effects 1.4 pp. "practitioners demonstrated nearly perfect calibration".
- **Bergquist et al.** (Europe PMC, 2023-03-21): 430 primary studies; bias-adjusted d = 0.18 (7 pp); about 2 pp in large-scale interventions (n > 9,000, k = 32). Social comparison and financial incentives were most effective, "education or feedback" least.

### leaderboard-goodhart: confirmed
- **The Decoder, Meta** (2026-04-07):
  - "Claudeonomics", 85,000+ employees, 60T tokens in 30 days, top user 281B tokens
  - titles "Token Legend" and "Cache Wizard"
  - "some employees just leave AI agents running for hours to pad their numbers"
- **MLQ, Meta** (2026-06-13, citing The Information): the leaderboard will be dismantled, replaced by "AI Gateway" with "automated alerts for unusual spending spikes"; "Full token budgets ... in 2027".
- **The Decoder, Amazon** (2026-05-29, citing FT):
  - "Kirorank"; target "more than 80 percent of its developers to use AI on a weekly basis"
  - "pointing AI agents at pointless tasks"; discontinued May 2026
  - Treadwell quote and "normalized deployments"
- **404 Media, Amazon** (2026-06-01, Maiberg): shutdown, employees say it "was easily cheated". Amazon's official line was that the goal was accomplished.
- **Uber:** TechCrunch 2026-06-02 and Fortune 2026-05-26 both tie the leaderboards to a budget exhausted in four months.
- **Microsoft:**
  - 404 Media (2026-08-04) is paywalled; the preview confirms the headline and new spend limits.
  - The memo text comes via LeadDev (2026-08-17, Kapani): Parikh quote plus "idle agents left running, questions asked of the model that the engineer already knew the answer to, work split into more turns than it needs, and the expensive reasoning tier selected for trivial tasks."
- **Kerr 1975:** AMJ 18(4):769–783 exists. The canonical DOI is 10.2307/255378; 10.5465/255378 resolves to the same record.

### uber-policy-arc: confirmed
- **TechCrunch** (2026-06-02, Ropek, citing Bloomberg): "a monthly $1,500 cap per employee and per agentic coding tool", which can be exceeded with approval.
- **Simon Willison** (2026-06-03): about $36k/yr, about 11% of a $330k median SWE package. He calls it rational compared with leaderboards.
- **Uber blog** (2026-08-27, Uday Kiran Medisetty):
  - "One shared tier across all interactive harnesses, not per-tool budgets. And separate tiers for managed agents."
  - "Alerts at 50/80/100% of expected spend"; "Manager sign-off for tier upgrades with quick propagation"
  - live cost counter "per harness and across all harnesses"; "Reasoning effort defaulted to Medium"
  - compaction "at 400k tokens even for 1M context window models"; "16 distinct anti-patterns"
  - cost per session −52% from the June peak; per 1,000 requests −34%; WAU ×7; agentic requests ×9.4
  - no per-lever quantification of the headline declines (only code-mode batching gets a number)
- **Fortune** (2026-05-26, Angelo): COO Macdonald, "That link is not there yet."

### runaway-circuit-breaker: confirmed
- **TechCrunch** (2026-06-05, Bellan):
  - Faros CEO Gordon quoting a CTO: "One of my engineers spent $40,000 on tokens last month".
  - Jellyfish's Arcolano: about 18.6× per-developer consumption in nine months.
  - Priceline's Cursor renewal came in 4–5×.
  - The Axios $500M report appears only second-hand, and the finding correctly marks it unverified.
- **Jellyfish** (2026-07-27, Arcolano & Albarran): median $50–100/month; top 5% $5,000+/month.

### blunt-caps-collateral: confirmed
- **NBER w21632** (Oct 2015): spending fell 11.79–13.80%, "entirely due to outright reductions in quantity", with no price shopping after two years. Both "potentially valuable" preventive care and "potentially wasteful" imaging were cut.
- **Jack & Smith** (AEJ: Applied 2020, Crossref abstract): electricity use −14%.
- **Sexton** (REStat 2015, OpenAlex abstract): automatic bill payment raised residential use 4.0% and commercial use up to 8.1%.
- **Cursor pooled usage:** "a developer working on a critical project can hit their cap while other allocations sit unused". The `.md` URL returned HTTP 200 via curl; WebFetch got a 404 on `.md` but the page itself works. "Block critical work" is a paraphrase.
- **GitHub:** "Raising ULBs without raising the enterprise budget can cause the enterprise budget to block users before they reach their individual budgets."

### enforcement-primitives: confirmed
- **costs doc:**
  - usage-credit spend limits "at the organization, group, or individual member level"
  - `/usage-credits` sends a request to admins
  - the Enterprise Analytics API gives per-user usage and cost
- **model-config:** org default model per role, and per-role effort caps (Enterprise).
- **gateway spend-limits doc:**
  - `daily`/`weekly`/`monthly` caps with scope `user`/`rbac_group`/`organization`; 429 enforcement
  - Claude Code warns "once utilization passes 75%, and again past 95%"
  - `/audit` mutation trail
  - Nuance: a group or org cap is a per-seat default, not a shared pool.
- **GitHub:** "Approve and increase"; enterprise, cost-center and user budgets with "Stop usage when budget limit is reached".
- **Cursor Admin API:** `/teams/user-spend-limit` (Enterprise only) and a bulk endpoint in preview.
- **Consumption guide:** light/standard/power tiers, "Start conservatively", "investigate before automatically raising it", and "Giving everyone Claude Code and Cowork access on day one is the fastest way to generate unexpected consumption."

### nudge-decay-durability: confirmed
- **Allcott & Rogers** (AER 104(10), Oct 2014): action-and-backsliding cycles that attenuate; after two years, decay of 10–20% per year once reports stop.
- **Ito, Ida & Tanaka** (AEJ: Economic Policy 10(1), Feb 2018): moral suasion habituates and dishabituates; incentives give larger effects, little habituation and habit formation.
- **Brandon et al.** (NBER w23277, Mar 2017, rev. Apr 2022): 38 experiments; persistence comes via technology adoption.
- **Ancker et al.** (BMC MIDM, 2017-04-10): "reminder acceptance dropped by 30% for each additional reminder received per encounter". No desensitization over time for new alerts.
- **Gilbert & Graff Zivin** (NBER w19510, Oct 2013): −0.6% to −1% after bill receipt.

### peer-comparison-outliers: CORRECTED
Most of the claims check out:
- **Allcott 2011** (Semantic Scholar abstract): −2.0%, equivalent to an 11–20% price rise; −6.3% in the top decile and −0.3% in the bottom decile.
- **Schultz et al. 2007** (Crossref abstract): a descriptive norm produced a boomerang in low users, and adding an injunctive message removed it.
- **Ryskina et al.** (JGIM 2018-05-22, Europe PMC abstract): an RCT with 114 interns and residents found no significant average change (−0.14; 95% CI −0.56 to 0.27; p = 0.50).

**The error is in the units of the outlier result.** The finding writes "1 test/day" and "0.80 tests/day". The source uses patient-days: physicians whose rate deviated by more than 1.0 test per **patient-day** cut 0.80 orders per **patient-day** (95% CI −1.58 to −0.02, p = 0.04).

### price-salience-mixed: confirmed
- **Feldman 2013** (JAMA IM): active arm went from 3.72 to 3.40 tests per patient-day, −8.59%, at Johns Hopkins.
- **Sedrak 2017** (PRICE RCT): 3 hospitals, 1 year, 98,529 patients and 142,921 admissions. No significant change (0.05 tests per patient-day, P = .06; fees P = .47).
- **Chetty, Looney & Kroft** (AER 2009): tax-inclusive tags cut demand 8%.
- **Sexton:** +4.0%.

### channel-code-review-hooks: confirmed (with caveat)
- **Tricorder PDF** (Google archive 43322):
  - "when developers have to navigate to a dashboard or run a standalone command line tool, analysis usage drops off"
  - FindBugs "command-line tool was used by only 35 developers in 2014"
  - results at code review with "a very low effective false positive rate here (< 10%)", "on changed lines by default"
- **Maddila et al.** (TOSEM 2023-03-30): 147 repos, −60% resolution time for 8,500 PRs, 73% resolved as positive; scaled to 8,000 repos and 210,000 notifications.
- **hooks doc (raw md):** on `resume`/`fork`, `SessionStart` receives `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired` and `estimated_cache_write_usd`, "to report what resuming a stale conversation costs before the first request" (v2.1.251+).
- **Caveat:** "outperform dashboards" is Google's qualitative experience. Neither source is a head-to-head comparison of channels.

### anthropic-habits: confirmed
- **costs doc:**
  - "Unexpectedly high spend on an API or cloud-provider plan: usually traces back to long sessions that were never cleared or to Opus left as the default model. The highest-impact habits to share are clearing between unrelated tasks and matching the model to the job."
  - "`/compact` ... is itself a large request ... `/clear` costs nothing."
- **best-practices doc:**
  - "After two failed corrections, `/clear`"
  - "Plan mode ... adds overhead ... If you could describe the diff in one sentence, skip the plan."
  - "Scope investigations narrowly or use subagents"
- The correction about `/compact` is accurate. Note the costs doc scopes the "unexpectedly high spend" statement to API and cloud-provider plans.

### spec-quality: confirmed
arXiv API: 2608.25399v1, 2026-08-26, sole author Jakub Smékal. Kimi K3 at three thinking efforts, 2,700 runs:
- a bare user story raises spend 29.7%
- run-to-run variance unaffected
- task sensitivity 13–115%
- the predictor prices configurations "from a single cheap probe on an unseen task within 36%"

### context-files-mixed: confirmed
- **Lulla et al.** (arXiv 2601.20404, 2026-01-28, v2 2026-03-30): 10 repos, 124 PRs. Median runtime Δ28.64% and output tokens Δ16.58%, "comparable task completion behavior". The paper reports this as an association.
- **Gloaguen et al.** (2602.11988, 2026-02-12): "does not generally improve task success rates, while increasing inference cost by over 20% on average".
- **dos Santos et al.** (2606.15828, 2026-06-14; SCAM 2026): 100 repos; Lint Leakage 62%, Context Bloat 42%, Skill Leakage 35%.
- **Jellyfish** (2026-07-27): "Every doubling of context-file investment gives you 29% more additional throughput".

### marginal-returns-curve: confirmed
- **Jellyfish** (2026-04-15, Arcolano):
  - 12,000 developers at 200 companies in Q1 2026; about 7,500 with measurable data
  - bottom 20% spent $3/quarter for 11 PRs; top 20% spent $1,822 for 23 PRs
  - about 7M tokens per PR at the median vs about 69M for the top decile
  - weekly PRs rise from 0.77 to 2.15
- **Jellyfish** (2026-07-27): p90 about 10× tokens for about 2× merged code; no significant quality differences.
- **TechCrunch** (2026-04-17, Fernholz): 7,548 engineers; "the tools are generating volume, not value"; GitClear 9.4× churn.

### quality-guardrail: confirmed
- **Faros 2025** (10,000+ developers, 1,255 teams): tasks +21%, PRs +98%, review time +91%, bugs +9%, PR size +154%. Correlation "evaporates at the company level".
- **Faros 2026** (22,000 developers, 4,000+ teams, 2 years):
  - epics +66%, tasks +33.7%, merge rate +16.2%
  - bugs +54%, incidents-to-PR +242.7%, churn +861%
  - review time +441.5%, merged without review +31.3%
  - no explicit publication date found
- **DORA 2025** (2025-09-23): nearly 5,000 respondents, 90% use AI. Adoption has a positive relationship with throughput and a negative one with stability.
- **DORA AI Capabilities Model:** the seven capabilities match.

### self-report-unreliable: CORRECTED
The following all check out:
- **METR 2025-07-10:** 16 developers, 246 issues; 19% longer; forecast a 24% speedup, believed 20% afterwards.
- **METR 2026-02-24:** 57 developers, 800+ tasks.
- **DX** (2026-05-20, Bruneaux): net time gain, agent hourly rate, and a warning against individual-level evaluation.
- **SPACE** (ACM Queue 19(1), 2021): "cannot be measured by a single metric or dimension".

**The error is in how the METR update figures read.** In METR's sign convention, "a speedup of -18% with a confidence interval between -38% and +9%" and "-4% ... -15% to +9%" are changes in task time. Negative means faster, so the point estimates are speedups (METR: "Our raw results show some evidence for speedup"). Both CIs cross zero. METR also says selection effects "likely biases downwards our estimate of AI-assisted speedup". Placed next to "19% slower", the finding invites the reverse reading.

### value-adjusted-cost-metric: confirmed (synthesis)
- **CUPED** (WSDM 2013-02-04, OpenAlex abstract): "reduce variance by about 50%, effectively achieving the same statistical power with only half of the users, or half the duration" (Bing).
- **Component sources check out:**
  - DX: net time gain and agent hourly rate.
  - Uber: managed-agent cost per merged PR plus revert rate.
  - Jellyfish: cost per PR, reported by quintile ($0.28 vs $89.32) and tokens per PR by decile.
- **Not from any source:**
  - The CPQ formula, the q-weights and the break-even test are the author's own design.
  - "Enabling team-level detection" is an extrapolation; CUPED's 50% was measured at user level on Bing.

### vendor-leaderboards: CORRECTED
Raw `analytics.md`:
- The overview bullet says "Leaderboard: top contributors ranked by Claude Code usage".
- The detailed section says "The Leaderboard shows the top 10 users ranked by **contribution volume**", toggling between PRs and lines of code with Claude Code. It sits under contribution metrics, which "require additional setup to connect your GitHub organization" (GitHub app plus Owner toggles).
- Only Admins and Owners can view the dashboard.
- "Export all users" and the "Identify power users" framing are confirmed. So are accept rate, lines accepted, and "deliberately conservative" contribution metrics.

**Correction:** it is a top-10 contribution leaderboard (Claude-assisted PRs or lines), not a token or spend leaderboard. It is also not live by default: it needs the GitHub integration. Treat it as a potential input-volume Goodhart risk, but different from Meta- or Amazon-style token boards.

### mandates-gaming: confirmed
- **TechCrunch** (2025-08-22): Armstrong required onboarding to Copilot or Cursor "by the end of the week" and fired those without a valid reason. He called it "heavy-handed". Collison: "It's not clear how you run an AI-coded code base"; Armstrong: "I agree."
- **TechCrunch** (2026-06-24, Ropek): Accenture moved from "risk losing out on promotions" to restricting routine AI use. Justice Kwak: "Spend is becoming very unpredictable".
- **Pragmatic Engineer** (2026-06-17): "managers shall inspect token count during perf reviews".
- **Cursor** (2025-07-04): apology, refund of unexpected charges "over the past 3 weeks" (June 16–July 4), and a promise of "advance notice".
- **The Register** (2026-04-26, Claburn): mentions Meta and Shopify treating token use as a KPI, citing the NYT. Shopify is correctly marked unverified.

### showback-chargeback: confirmed
- **FinOps:** "Showback is always required in any FinOps practice, but chargeback is dependent on organizational accounting policies", and chargeback may be "unwarranted" for single cost centers. The KPIs are processing time, on-time invoices, GL recharge rate, accuracy, and estimate-vs-actual variance, so accuracy and timeliness rather than behavior.
- **State of FinOps 2026:** 1,192 respondents, $83B+; 98% manage AI spend, up from 63% in 2025 and 31% in 2024. Challenges include visibility, allocation and value.

### education-channels: confirmed
- **settings-reference (raw):**
  - `companyAnnouncements` "picks one at random for each session; on a person's very first launch it shows the first entry"
  - `spinnerTipsOverride`: at most 200 tips, `cooldownSessions` 0–1000, `priority` −10 to 10, `excludeDefault`
- **hooks:** `systemMessage` surfaces to the user, varying by event.
- **costs:** `/usage` flags behaviors at 10% or more of recent usage.
- "Low-salience" is an inference, not a measured claim.

### labor-law-constraints: confirmed (with caveat)
- **BetrVG English translation** (amended by the Act of 19 July 2024):
  - §87(1) no. 6 covers "technical devices designed to monitor the behaviour or performance"
  - §80(3): assessing AI means "it is deemed necessary to call on the advice of an expert"
  - §90 covers working procedures "including the use of artificial intelligence"
- **Orrick** (2024-09-17): "objectively capable" of monitoring triggers co-determination. With AI tools on company devices, "the employer has access to information such as who uses the AI tool, when, for what reason, and how often".
  - **Caveat:** the Hamburg court itself found co-determination NOT triggered, because ChatGPT was used via private browser accounts and a browser agreement already existed. The finding does not say this.
- **GDPR Art. 88(2):** national rules "shall include suitable and specific measures", with regard to "monitoring systems at the work place". It applies to Member State rules adopted under 88(1).
- **ICO:** DPIA for high-risk monitoring, least intrusive means, consent rarely appropriate, under review after the DUA Act.
- **AI Act:**
  - Annex III 4(b) quoted
  - Art. 26(7) quoted
  - Art. 5(1)(f) quoted
  - The timeline site (updated 2026-08-31) gives 2 December 2027 for Annex III high-risk obligations.
- **NY §52-c:** prior written notice on hiring, acknowledged and posted.

### aggregation-k5: confirmed
- **Viva Insights** (ms.date 2025-11-20): "You'll need to set this number to at least five". Opt-out does not cover Copilot usage data.
- **Gateway doc:**
  - `principal_emails` retention is 90 days since last activity; `spend` is 13 months.
  - `spend` and audit rows "reference the pseudonymous OIDC `sub` only".
  - The warning about `q=`/`user_ids[]` in proxy access logs is confirmed.
- **GitHub REST:** `users-1-day` report endpoints at enterprise and org level ("detailed user-level usage data").
- **DX:** avoid individual-level evaluation.

### experiment-design: confirmed
- **CUPED:** see value-adjusted-cost-metric.
- **Hemming et al.** (BMJ 2015-02-06; the abstract comes from the Birmingham repository because BMJ and PubMed blocked fetches): "random and sequential crossover of clusters from control to intervention until all clusters are exposed", suited to "political or logistical constraints".
- **DellaVigna & Linos:** practitioners' forecasts were nearly perfectly calibrated.

### machine-spend-separate: confirmed
- **Uber blog:** separate managed-agent tiers; "cost per merged PR, cost per review, cost per alert, cost per cleanup" plus "revert rate, F1, MTTR".
- **Costs doc, "Why usage climbs":**
  - scheduled tasks fire while idle (and `/usage` Loops rows)
  - cross-session messages
  - goal check-ins, "at most three idle check-ins per goal"
  - "each active teammate keeps consuming tokens until it exits"
