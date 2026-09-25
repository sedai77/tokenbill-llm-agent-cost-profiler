# How Token Bill can prove savings from fleet policy changes

Research track: **gap-fleet-causal-savings-verification**. Written 2026-09-23. Research only: the Token Bill repo was not modified.

---

## 0. TL;DR

1. **Borrow the vocabulary from energy M&V.** Savings cannot be metered; they are the absence of spend. They are computed as an adjusted baseline minus actual spend, with routine and non-routine adjustments (IPMVP/FEMP).
   - Invoice reconciliation is IPMVP **Option C** (the whole-facility meter). Token Bill's replay engine is **Option D** (calibrated simulation).
   - Option D projections count only once the simulator reproduces billed spend. ASHRAE Guideline 14 sets the bar: NMBE within ±5% and CV(RMSE) ≤ 15% monthly; ±10% and ≤ 30% hourly.
   - On this corpus Token Bill v0.1.2 **fails** that gate. Its priced total has NMBE = −7.3% because of a missing Opus 5.5 price. Its chars/3.7 heuristic under-counts tool tokens by about 27–40%.
2. **Fleet changes are not randomized per task, but they can be randomized per workspace, and they should be.**
   - Roll out in randomized waves: a **stepped-wedge** design over MDM groups.
   - Analyze with heterogeneity-robust staggered DiD: Callaway–Sant'Anna, or Borusyak–Jaravel–Spiess imputation. Use constant prices and CUPED.
   - In the simulation built from the real calls:
     - These estimators land within about 2–4% of the truth, with about 95% CI coverage.
     - Naive before/after is off by +16.5% (billed $) or −31% (constant prices).
     - TWFE is off by −9%.
3. **Never pick the first rollout wave by recent spend.** Doing so creates regression to the mean.
   - With waves chosen on weeks 1–4 spend, the same "robust" estimators overstate savings by **+32–34%**, and CI coverage falls to 42–65%.
   - Randomize the wave order, or select on a window that does not overlap the baseline.
4. **Prompt caches are the interference boundary.**
   - Caches are isolated per workspace on the Claude API, Claude Platform on AWS and Microsoft Foundry, and per organization on Bedrock and Vertex. Treated and control developers in one workspace therefore share cache state.
   - Randomizing individuals inside a workspace, with 30% leakage, biased the estimate by **−28%**. Cluster randomization by workspace was unbiased.
   - Anthropic's own server-managed settings "apply uniformly to all users". Per-group policy needs endpoint-managed files or MDM profiles.
5. **Heavy tails decide what is detectable.**
   - Per-thread cost in the corpus has skew 18.7. Kohavi's 355·s² rule then needs about **125k threads per arm** before a mean is approximately normal. Per active day (skew 2.1) it needs about 1.5k.
   - CUPED on the developer's own pre-period cost cut estimator variance by **about 88%** (SD $1.76 → $0.61) and brought coverage to 94%.
   - Capping at p99 buys power but biases dollar savings by −13%. Use it for decisions, not for receipts.
6. **Minimum detectable effects** (80% power, CUPED, 4-week window):
   - About 13–14% with 1,000 developers in 40 workspaces.
   - About 6–7% with 5,000 developers.
   - About 4–5% with 10,000 developers.
   - A 6% cache-hygiene lever needs **roughly 5,000–10,000 developers**, or a longer window, before it can be verified. Token Bill must say so up front instead of shipping a noisy "saved $X".
7. **Monitoring.**
   - Daily peeking at a fixed-horizon test raised A/A false positives from 5.5% to **17.5%**. Six pre-registered weekly looks with Bonferroni held them at 2.5%.
   - On the real per-thread stream, peeking after every pair gave 23.8% false positives, versus 0.2% for an asymptotic confidence sequence.
   - Early-stopped estimates were inflated ×1.12. Report sequence-adjusted estimates.
8. **Switchbacks**, for org-wide-only switches such as server-managed settings or a shared gateway:
   - With about 1h of cache carryover, a naive analysis of 30-minute or 1-hour switches is biased **−31% to −40%**.
   - Daily blocks, or Bojinov's lag-p Horvitz–Thompson estimator, remove the bias. Variance is large: a 6% lever run for 28 days has an SD of about 62% of the effect.
9. **Attribution when levers overlap.** On the real calls, the three levers' standalone ceilings sum to $4,503; applied jointly they save $4,306, an overcount of 4.6%. The effort-cap lever's credit ranges from **$163 to $335 (69%)** depending on order.
   - Report **Shapley** credit, the average over all orders, which is exact and order-free.
   - Build the action plan in the FinOps order (usage before rate), as AWS Cost Optimization Hub's de-duplication and Anthropic's "mutually exclusive ceilings" rule both imply.
10. **Projections are systematically optimistic.** In energy efficiency:
    - Engineering projections were about 2.5× realized savings (Fowlie et al.).
    - Refrigerator swaps delivered about one quarter of the ex-ante estimate, and AC swaps *increased* use (Davis et al.).
    - The first 10 Opower sites overstated the next 101 (Allcott).

    In LLM agents, token reduction has raised bills (PointFive +6.8%; JetBrains/RTK +7.6%). Token Bill should publish every projection with a **realization-rate** interval backtested per lever class, and issue **signed receipts** only for verified results.
    - A receipt is canonical JSON, micro-dollar integers and a DSSE envelope, signed with `ssh-keygen -Y sign`.
    - The signing path was demonstrated working with stdlib Python plus OpenSSH: a tampered byte fails verification.

The full protocol is in §5. The worked synthetic rollout on `recs.pkl` is in §4.

---

## 1. Scope and method

**Question.** Fleet policy changes cannot be randomized per task. Examples include a managed-settings rollout, a gateway fix, a default model or effort level, and `autoCompactWindow`. Costs are heavy-tailed, prices and models change mid-window, and prompt caches couple developers. How can Token Bill still prove that such a change caused a lower bill?

**Sources opened for this track** (dates and venues are in §7):
- **M&V practice.**
  - DOE FEMP M&V Guidelines 4.0 (PDF), which carries the ASHRAE Guideline 14 calibration table.
  - The EVO IPMVP page for Core Concepts 2022. Its full text is behind an EVO login, so the principle is taken from FEMP.
  - CalTRACK 2.0 methods and the OpenEEmeter/OpenDSM README.
- **Causal-inference and experimentation papers, 20+, all opened as PDFs.** Callaway–Sant'Anna; Goodman-Bacon; Sun–Abraham; de Chaisemartin–D'Haultfœuille; Borusyak–Jaravel–Spiess; Arkhangelsky et al. (SDID); Bojinov–Simchi-Levi–Zhao; Hu–Wager; Ugander et al.; Aronow–Samii; Pouget-Abadie et al.; Deng et al. (CUPED); Deng–Knoblich–Lu; Kohavi et al. 2009 and 2014; Fabijan et al.; Hohnhold et al.; Johari et al.; Howard et al.; Waudby-Smith et al.; Lindon et al.; Ramdas et al.; Schultzberg–Ankargren; Hausman–Rapson; Hemming et al.; Shorrocks; Manheim–Garrabrant; Miller.
- **Energy-efficiency realized-vs-projected studies.** Fowlie–Greenstone–Wolfram; Davis–Fuchs–Gertler; Allcott.
- **LLM cost evidence.** PointFive (arXiv 2607.12161); the JetBrains RTK post; Bai et al. (arXiv 2604.22750); METR 2025 and 2026.
- **FinOps.** The Foundation's Rate Optimization and Usage Optimization pages; FOCUS v1.4 column specs; AWS Cost Optimization Hub docs; ProsperOps' ESR definition.
- **Signing.** DSSE, the in-toto attestation spec, RFC 8785 and the `ssh-keygen(1)` man page.
- **Anthropic docs** (public `.md` endpoints plus the bundled `claude-api` skill): prompt-caching isolation, the Usage & Cost Admin API, the Claude Code Analytics API, costs, monitoring, and server-managed and managed settings.

Web search was unavailable (the session's budget was exhausted), so every source was opened directly: PDFs with `curl` + `pdftotext`, metadata via the arXiv API, Semantic Scholar and Europe PMC. Venues are stated only where a primary page, a DOI or another paper's bibliography confirmed them. Everything else is marked "arXiv".

**Empirical work.** Everything is stdlib Python in `research/causal_sim/`, with `.out` files:
- **`a_levers_attribution.py`** replays three levers exactly on each of the 43,383 real calls in `recs.pkl`.
  - **L1, prefix-stability fix.** Removes the avoidable waste of `unexplained_prefix_change` and `model_switch` misses, using the same miss detector as `06_cache_misses.py`.
  - **L2, subagent default model → Sonnet 5.** Reprices subagent and workflow-agent calls on the same tokens.
  - **L3, effort cap.** Output −20%.
  - It also computes Shapley and sequential attribution, unit-of-analysis distributions and an Option-C baseline check.
- **`b_fleet_rollout.py`** builds a synthetic fleet of 1,000–10,000 developers from the 54 real day-blocks and Monte-Carlos rollout designs and estimators against known ground truth.
- **`c_power_seq_switchback.py`** computes MDE tables from A/A re-randomization, the cost of peeking, switchbacks with cache carryover, and anytime-valid monitoring on the real thread stream.
- **`d_backtest_receipt.py`** runs one worked stepped-wedge rollout end to end: realization rate, placebo check and a signed receipt.
- **`e_power_formula.py`** gives the closed-form sample-size rule, cross-checked against the simulation.

**Honest limits of the synthetic world.**
- `recs.pkl` is one power user. Between-developer spread is an **assumption**: lognormal σ_b = 1.0, chosen to match Anthropic's published fleet stats (mean about $13 per active day, 90% of users under $30). Within-developer day-to-day variation is **measured**: daily CV = 1.13 over 54 active days.
- The 30% contamination and +20% trajectory penalty are assumptions, labelled as such. They are there to show what each design does, not to estimate those quantities.

---

## 2. Why this is hard, in numbers from the corpus

Distributions by candidate unit, measured on `recs.pkl` (`a_levers_attribution.out`, A2):

| Unit | n | mean | median | p90 | CV | skew | top-10% share | Kohavi 355·s² (min n per arm for a normal mean) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| active day | 54 | $215.42 | $146.70 | $540.62 | 1.13 | 2.1 | 34.9% | 1,514 |
| thread (agent/task) | 1,471 | $7.91 | $1.68 | $9.37 | 7.75 | 18.7 | 74.3% | 124,581 |
| human prompt (main thread) | 549 | $11.91 | $5.52 | $31.99 | 1.62 | 3.9 | 47.8% | 5,477 |
| session (incl. agents) | 29 | $401.13 | $36.74 | $1,236.78 | 1.87 | 2.4 | 48.5% | 2,004 |

Other facts that shape the design:

- **Same-task variance.** Runs of the same agentic task vary up to 30× in total tokens (Bai et al.). Identical attempts of the same task in the same arm differed by a median 22% in cost in JetBrains' RTK study.
- **Prices change mid-window.** v0.1.2's missing Opus 5.5 price alone moves this corpus's total by 7.3%.
- **Caches couple developers.** They are workspace-scoped on the Claude API, Claude Platform on AWS and Foundry, and org-scoped on Bedrock and Vertex. Entries live at least 5 minutes (standard) or 1 hour (extended), so cache warmth carries over after any switch.
- **Server-managed Claude Code settings cannot target a group.** The claude.ai console applies one policy to the whole org. Per-group policy requires endpoint-managed `managed-settings.json` files or MDM profiles, deployed per group.

---

## 3. Findings

Each finding lists the claim, the evidence, what it means for Token Bill, and the evidence strength.

### F1. Savings are an adjusted-baseline counterfactual, not a meter reading (IPMVP/FEMP) (strong)
- **Savings formula.** FEMP's M&V Guidelines 4.0 (Nov 2015) say actual savings cannot be measured, because they are an *absence* of use. Savings are computed as Savings = (Baseline − Post-installation) ± Adjustments.
- **Adjustments.** *Routine* adjustments cover expected changes in independent variables such as weather or occupancy. *Non-routine* adjustments cover unexpected changes unrelated to the measure.
- **Avoided energy use** is defined against the baseline adjusted to reporting-period conditions.
- **Option C.** Whole-facility utility data is recommended only if all of these hold:
  - savings exceed about **10–20%** of metered consumption;
  - there are at least **12 (preferably 24) months** of baseline;
  - there are at least **9 (preferably 12) months** of reporting data;
  - site changes are tracked as non-routine adjustments.
- **Core Concepts 2022.** EVO publishes IPMVP Core Concepts 2022, but only behind a login. The same principle is quoted here from FEMP.
- **Token Bill.**
  - Define **avoided cost** = adjusted-baseline cost − actual cost.
  - Keep an explicit *routine adjustment model* for active developer-days, task volume, a constant rate card and calendar.
  - Keep a *non-routine adjustment log*: re-orgs, onboarding waves, model launches, Claude Code upgrades, outages.
  - Invoice reconciliation is the Option C meter. Replay is Option D.
  - Option C's "savings > 10–20% of the meter" rule translates directly: a lever smaller than about 10% of a workspace's bill cannot be verified from that workspace's invoice alone. It needs a comparison group (F6, F10).
- Sources: FEMP M&V Guidelines 4.0 (2015-11); EVO IPMVP page (retrieved 2026-09-23).

### F2. ASHRAE Guideline 14 calibration tolerances give the simulator's pass bar, and Token Bill v0.1.2 fails it on this corpus (strong)
- **The tolerances.** FEMP Table 4-2, citing ASHRAE Guideline 14 §5.3.3.3.10: a calibrated simulation must reach MBE ±5% and CV(RMSE) ≤ 15% on monthly data, or ±10% and ≤ 30% on hourly data. Models should be calibrated to monthly data at minimum. Guideline 14 also requires overall savings uncertainty below prescribed thresholds.
- **How Token Bill v0.1.2 does on this corpus:**
  - **Priced total.** $10,786.64 against $11,632.75 billed, so **NMBE = −7.3%**. This fails the ±5% gate; the cause is a missing Opus 5.5 price (`19_tokenbill_pricing_gap.out`).
  - **Token heuristic.** The redundancy attribution assumes 3.7 chars/token. The measured median is **2.2–2.7 chars per appended token**, depending on the tool (`08_dupes_fanout.out`). Tool-result tokens are therefore under-counted by about 27–40%.
  - **Activity-driver baseline** (Option C style: cost against human prompts and active half-hours). CV(RMSE) is **67.3% daily and 38.8% weekly** (`a_levers_attribution.out`, A3). A single developer's usage-driver regression cannot serve as a baseline; comparison groups are required.
- **Token Bill.** Add a `calibrate` command:
  - Run as-billed replay against the invoice or Admin API per workspace-day. Report NMBE and CV(RMSE) monthly and daily.
  - Mark any projection **"estimated (uncalibrated)"** until the monthly tolerances are met.
  - Re-run calibration automatically on every price-table or tokenizer change.
- Sources: FEMP M&V 4.0 Table 4-2; research outputs 08/19; `causal_sim/a_levers_attribution.out`.

### F3. CalTRACK gives stdlib-ready formulas for savings uncertainty and portfolio aggregation (strong)
- **Site-level uncertainty.** CalTRACK 2.0 computes site-level **fractional savings uncertainty** with a modified Guideline 14 formula. It includes Sun & Baltazar's empirical coefficients for autocorrelated residuals: a,b,d = −0.00024, 0.03535, 1.00286 for daily data.
- **Portfolio aggregation.** Site results aggregate as FSU_portfolio = √(Σ ΔU²) / Σ U_save.
- **Portfolio defaults.**
  - A building-level CV(RMSE) threshold of **100%**.
  - FSU thresholds set by the buyer, for example **< 15%** for a non-wires-alternative procurement and **< 25%** for pay-for-performance.
- **Bias warning.** Aggregation shrinks uncertainty but not systematic bias from "population trends". OpenEEmeter/OpenDSM lists comparison groups (non-participant meters) as the correction for external population-level effects.
- **Token Bill.** Compute FSU per workspace and aggregate across the fleet. Require portfolio FSU ≤ 25% at 90% confidence before a fleet-level number is called "measured". Always pair it with a comparison group to handle population trends such as new models or fleet-wide workload growth.
- Sources: CalTRACK methods v2.0 (docs.caltrack.org, retrieved 2026-09-23); OpenEEmeter/OpenDSM README (GitHub, retrieved 2026-09-23).

### F4. Engineering projections systematically overstate realized savings, sometimes with the wrong sign (strong)
- **Weatherization.** Fowlie, Greenstone and Wolfram (NBER w21331, 2015; QJE 2018) ran a 30,000-household experiment on the US Weatherization Assistance Program. Model-projected savings were about **2.5×** actual savings. Upfront costs were about twice the actual savings.
- **Cash for Coolers.** Davis, Fuchs and Gertler (NBER w18044, 2012; AEJ: Economic Policy 2014), using billing records for Mexico's 1.5M-household programme:
  - Refrigerator replacement saved about 132 kWh/yr, **about one quarter** of the World Bank's ex-ante 481–482 kWh/yr.
  - Air-conditioner replacement **increased** use by about 80 kWh/yr (rebound).
- **Opower.** Allcott (NBER w18373; QJE 2015) studied 111 RCTs covering 8.6M households. Predictions from the first 10 sites **substantially overstated** efficacy in the next 101 ("site-selection bias"): utilities that adopted first had more responsive customers.
- **Token Bill.**
  1. Track a **realization rate** RR = verified / projected for every lever class (F20).
  2. Never extrapolate a pilot team's effect to the fleet without a representativeness check. Early adopters are the most responsive.
  3. Measure **total spend per developer-day**, not per-call cost, so that rebound shows up. When calls get cheaper, developers and agents may use more.
- Sources: NBER w21331 (2015-07; published QJE 2018, doi 10.1093/qje/qjy005); NBER w18044 (2012-05; AEJ:EP 6(4) 2014); NBER w18373 (2012-09, rev. 2014-09; QJE 130(3) 2015).

### F5. For LLM agents, token reduction is not bill reduction, so replay projections of trajectory-changing levers are only estimates (strong)
- **PointFive** (Weinberger & Hozez, arXiv 2607.12161, v5 2026-08-12), using paired, provider-billed Claude Code campaigns:
  - The most aggressive compression removed **38.4%** of tool-output tokens yet raised billed cost by **6.8%**, because cache traffic and agent trajectories changed.
  - Their statistics are a model for Token Bill's lab mode:
    - paired within-block differences;
    - a **task-clustered bootstrap** with 10,000 resamples;
    - repository-clustered bootstrap and leave-one-repo-out checks;
    - an order-sensitivity check;
    - failed runs kept in the numerator of cost per success.
  - The cost ICC across repetitions was 0.37–0.55. So 712 runs per arm gave a Kish effective size of only **about 38–45 tasks**.
- **JetBrains** (Shiryaev, July 2026) ran a paired A/B on 86 SkillsBench tasks: 425 billed trials, claude-sonnet-5, Claude Code 2.1.201. RTK made runs **+7.6% more expensive** at low effort (Wilcoxon p = 0.004) and +0.1% (p = 0.99) at high effort. Identical attempts varied by a median 22% in cost.
- **Bai et al.** (arXiv 2604.22750): runs vary up to 30× on the same task.
- **Token Bill.** Any lever that can change the agent's trajectory must stay labelled **"estimated"** until verified in the field or in a paired billed lab campaign with cluster-robust intervals:
  - compression;
  - tool-output caps;
  - model or effort changes;
  - compaction window.
- Sources: arXiv 2607.12161 (2026-08-12); blog.jetbrains.com/ai/2026/07/rtk-claude-code-token-savings (2026-07); arXiv 2604.22750 (2026-04-29).

### F6. For staggered rollouts, two-way fixed effects is unreliable; use Callaway–Sant'Anna or imputation (strong)
- **Theory.**
  - Goodman-Bacon (J. Econometrics 225(2) 2021; NBER w25018): the TWFE DiD estimate is a weighted average of all 2×2 comparisons and is biased when effects change over time.
  - de Chaisemartin & D'Haultfœuille (AER 2020): the weights can be **negative**, so the coefficient can have the wrong sign.
  - Sun & Abraham (J. Econometrics; arXiv v2 2020-09-22): lead and lag coefficients get contaminated, and spurious pre-trends appear.
  - Callaway & Sant'Anna (J. Econometrics; arXiv v4 2020-12-01): identify group-time effects ATT(g,t) using never-treated or not-yet-treated comparisons, with conditional parallel trends.
  - Borusyak, Jaravel & Spiess (arXiv v5 2024-01-16) derive the efficient **imputation** estimator.
- **Simulation** (D1, randomized wave order; 80 worlds; `b_d1.out`). Lever L2 is rolled out by MDM to 3 waves of 10 workspaces, with 10 never treated. True realized saving is $3.248 per treated dev-day (25.1%).

| Estimator | Bias vs truth | RMSE | 95% CI coverage |
|---|---:|---:|---:|
| naive pre/post, billed $ | **+16.5%** (price cut mistaken for the lever) | 25.5% | – |
| naive pre/post, constant prices | **−31.0%** (workload growth hides savings) | 35.9% | – |
| TWFE workspace + week FE | −9.1% | 20.3% | – |
| Callaway–Sant'Anna (not-yet-treated) | −4.0% | 25.2% | 98% |
| Callaway–Sant'Anna, constant prices | −3.1% | 22.2% | – |
| BJS-style imputation | **−1.8%** | 22.8% | 95% |
| CS on log cost (percent effect) | −5.2% | 18.4% | 95% |

- **Token Bill.**
  - Implement CS ATT(g,t) with not-yet-treated controls and stdlib imputation (alternating-projection fixed effects).
  - Run a stratified cluster bootstrap by workspace.
  - Report both $ and % effects at a **constant rate card**. Never use TWFE for staggered rollouts.
- Sources: arXiv 1803.09015, 1804.05785, 1803.08807, 2108.12419; NBER w25018.

### F7. Picking the first wave by recent spend creates regression to the mean and silently inflates savings by about a third (strong: simulation plus theory)
- **Simulation.** The same D1 setup, but the 10 highest-spending workspaces from weeks 1–4 adopt first. That is the obvious FinOps instinct: fix the biggest spenders first.
  - CS overstates savings by **+34.2%** (coverage 65%).
  - Imputation overstates by **+32.3%** (coverage 42%).
  - The multiplicative variant overstates by +34.3% (coverage 45%).
  - The spend that got a workspace picked was partly noise, and that noise regresses away after adoption.
- **Why the literature applies.** CS and Sun–Abraham assume (conditional) parallel trends. Selecting on the pre-period outcome breaks them.
- **Token Bill.**
  - Assign wave order **at random** (F8).
  - If business constraints force targeting, select on a window disjoint from the baseline, add a **placebo test** (fake adoption date in the pre-period), and label the result "measured", not "verified".
  - The worked example's placebo was −$0.007 per dev-day, −0.2% of projection (`d_backtest_receipt.out`).
- Sources: `causal_sim/b_d1.out`; the CS and Sun–Abraham assumptions above.

### F8. The stepped wedge is the right default for "we will roll this out anyway" (moderate to strong)
- **What it is.** In a stepped-wedge cluster randomized trial, every cluster eventually crosses over, in a randomized order. It suits interventions that will be rolled out regardless, and cases where simultaneous rollout is logistically implausible.
- **Transition periods.** Hemming, Lilford & Girling (Statistics in Medicine 2015, online 2014-10-24, PMC4286109) generalize to **incomplete** designs in which implementation or transition periods collect no data. That is exactly a washout for cache warmth and settings propagation.
- **Mapping to Claude Code.** Endpoint-managed settings (`managed-settings.json` or an MDM profile) can be deployed per group. The claude.ai console applies one policy to everyone and "can't target a group yet". Randomized waves therefore have to go through Jamf, Intune or Group Policy groups.
- **Token Bill.** Ship `tokenbill plan-rollout`:
  - It takes the list of workspaces, teams or MDM groups.
  - It emits a randomized wave schedule with a seed and a hash of the assignment log.
  - It emits the MDM payloads per wave.
  - It pre-registers the analysis.
- Sources: Hemming et al. 2015 (Europe PMC full text); code.claude.com/docs/en/managed-settings.md and server-managed-settings.md (retrieved 2026-09-23).

### F9. Prompt-cache isolation defines the interference boundary: cluster at the workspace, or the org on Bedrock/Vertex (strong)
- **The isolation rules.** Caches are isolated per workspace on the Claude API, Claude Platform on AWS and Microsoft Foundry. Bedrock and Google Cloud isolate per organization. Hits require 100% identical prefixes.
- **Why SUTVA fails inside one cache domain.** Treated and control developers in the same domain share warm prefixes. A change to the shared system prompt, tools or model splits one cache pool into two, so both arms lose hit rate.
- **What theory says.**
  - Ugander et al. (KDD '13; arXiv 1305.6979): graph-cluster randomization with exposure-probability Horvitz–Thompson estimation.
  - Aronow & Samii (Ann. Appl. Stat. 11(4) 2017): general interference framework.
  - Johari et al. (arXiv 2002.05670): which side you randomize in a shared-resource marketplace determines the bias.
- **Simulation** (D2/D3, lever L1, 120 worlds, `b_rand.out`). True global effect is $0.721 per dev-day (5.70%).
  - Randomizing individuals inside workspaces, with 30% of the effect leaking to same-workspace controls: **−27.7% bias**.
  - Cluster randomization by workspace, analyzed at the workspace level: **+3.4% bias** with CUPED, 94% coverage.
- **Token Bill.**
  - Detect the cache domain per provider (workspace ID or org ID).
  - Refuse unit-level designs for levers that touch shared prefixes, gateway routes or shared repositories. For those, cluster by workspace, repo or gateway route.
- Sources: platform.claude.com prompt-caching docs (retrieved 2026-09-23); arXiv 1305.6979, 1305.6156, 2002.05670; `causal_sim/b_rand.out`.

### F10. Test for interference rather than assume it away (moderate)
- **The test.** Pouget-Abadie, Saveski, Saint-Jacques, Duan, Xu, Ghosh and Airoldi (arXiv 1704.01190, v4 2019-01-29; deployed on LinkedIn) run a hierarchical design:
  - Some clusters randomize units individually and others randomize whole clusters.
  - Under no interference, the two estimators agree, as in a Durbin–Wu–Hausman test.
  - The test makes no assumptions about the network, bounds the variance and bounds type I error analytically.
  - Saveski et al. (KDD '17) is the related LinkedIn study.
- **Token Bill.**
  - For levers whose interference is plausible but uncertain, allocate some workspaces to individual randomization and some to cluster randomization, and report the Hausman-type difference.
  - If it is significant, publish only the cluster estimate.
  - Report the individual-vs-cluster ratio as an empirical **spillover factor** for that lever class.
- Sources: arXiv 1704.01190; Saveski et al. KDD '17 (via the bibliography of arXiv 2002.05670).

### F11. Switchbacks for org-wide-only changes: size blocks to the carryover, and use lag-p estimators or long blocks (strong)
- **Bojinov, Simchi-Levi & Zhao** (arXiv 2009.00148 v4 2025-09-17; Management Science 69(7):3759–3777, 2023):
  - **Optimal design.** With carryover of order m and T = n·m periods, randomize every m periods: T* = {1, 2m+1, 3m+1, …, (n−2)m+1}.
  - **Estimation and inference.** A Horvitz–Thompson estimator of the lag-p effect, with exact randomization p-values or a finite-population CLT.
  - **Choosing p.** If p < m the estimator is biased, so err toward larger p. Each identification test for m needs T/p > 100.
  - **Heavy tails.** Heavy-tailed noise slows the CLT: T = 120 was too small, while T = 1,200 behaved normally.
  - **Period length.** Keep each period shorter than the carryover.
- **Hu & Wager** (arXiv 2209.00197, v5 2026-09-21): under geometric mixing, carryover degrades standard switchback error to T^(−1/3). **Burn-in** periods restore about √(log T)·T^(−1/2).
- **Simulation.** 1,000 developers, real day-shifted stream, 28 days, carryover weights 0.6/0.25/0.15 over 1 hour (`c_sw.out`). Bias is shown with ±2 Monte-Carlo standard errors.
  - Naive difference, 30-min switches: **−39.7% ± 4.4**.
  - Naive difference, 1-h switches: **−31.1% ± 6.1**.
  - Naive difference, 4-h switches: −8.3% ± 8.1.
  - Naive difference, daily switches: **−0.7% ± 3.9**, with SD 62% of the effect.
  - Hajek lag-2 estimators: unbiased within MC error, but SD 135–199% of the effect.
  - On one developer's stream, no switchback design can detect a 6% lever.
- **Token Bill.**
  - Use switchbacks only for org-wide gateway or server-managed changes, with block length ≥ max(cache TTL, session length, settings-refresh interval), typically daily.
  - Burn in the first block-hours after each switch.
  - Regression-adjust for hour-of-week.
  - Pre-compute power: most 5–10% levers need months of daily switchbacks.
- Sources: arXiv 2009.00148 (v4 2025-09-17), with the venue confirmed in the arXiv 2209.00197 bibliography; `causal_sim/c_sw.out`.

### F12. CUPED is the single biggest power multiplier because developer intensity persists (strong)
- **The method.** Deng, Xu, Kohavi & Walker (WSDM 2013) adjust with the same metric's pre-period value.
  - Variance falls by a factor (1 − ρ²); about **50%** at Bing.
  - A longer pre-period helps more than a longer experiment.
  - Units without pre-period data get an indicator, which is equivalent to stratifying on presence.
- **Simulation** (cluster design, 40 workspaces, 120 worlds). The SD of the $ estimate went from $1.758 (difference in means) to **$0.605** with CUPED, a variance reduction of about 88%. Coverage was 94%.
- **Closed form.** With between-developer σ_b = 1.0 and within-developer daily CV 1.13 (measured), the correlation between pre and post 20-day means is ρ ≈ 0.955 (`e_power_formula.out`).
- **Token Bill.**
  - CUPED by default, using each developer's or workspace's 4–8-week pre-period cost per active dev-day.
  - Handle new hires, 30% of the synthetic fleet, with a missing-pre indicator.
  - Keep the full difference-in-differences (θ = 1) as a robustness check: +2.6% bias, SD $0.746.
- Sources: exp-platform.com CUPED PDF (WSDM 2013); `causal_sim/b_rand.out`, `e_power_formula.out`.

### F13. Heavy tails: the minimum sample for a normal mean, capping, and log-scale estimands (strong)
- **Rules of thumb.**
  - Kohavi, Deng, Longbotham & Xu (KDD 2014), rule 7: a normal approximation for a mean needs at least **355·s²** observations per variant, where s is skewness. Capping Bing revenue per user at $10 per week cut skew from 18 to 5.3 and made changes 30% smaller detectable.
  - Kohavi et al. (Data Min. Knowl. Disc. 18:140–181, 2009): n ≈ 16σ²/Δ² per variant for 80% power at α = 0.05.
- **What the corpus needs.** Per-thread cost (skew 18.7) needs about **124,581 threads per arm**. Per active day (skew 2.1) needs 1,514.
- **Simulation.**
  - Capping dev-days at the pre-period p99 cut the SD to $0.452 but biased $ savings by **−13.2%**.
  - Log-ratio estimates target geometric means and were −11.9% off the arithmetic % saving.
  - Quantile effects need the Poisson-bootstrap closed form of Schultzberg & Ankargren (arXiv 2202.10992, Spotify).
- **Token Bill.**
  - The primary **$ estimand is the arithmetic mean** of cost per active dev-day, because that is what the invoice sums. Use it with CUPED.
  - Capped and log versions are secondary, for decisions and robustness.
  - Run the 355·s² check automatically and refuse normal-theory intervals below it. Fall back to cluster bootstrap and aggregate units.
- Sources: KDD 2014 rules-of-thumb PDF; DMKD 2009 survey PDF; arXiv 2202.10992; `causal_sim/a_levers_attribution.out`, `b_rand.out`.

### F14. Ratio metrics and clustered data need delta-method or cluster-robust variance (strong)
- **Ratio metrics.** Deng, Knoblich & Lu (arXiv 1803.06336) cover the case where the randomization unit (developer or workspace) is coarser than the analysis unit (dev-day, PR or prompt), as in "cost per PR". The metric is then a ratio of means and needs the **delta method**.
- **Clustered errors.** Miller (Anthropic; arXiv 2411.00640) finds clustered standard errors on real evals can be **over 3× larger** than naive ones. He recommends paired differences and power analysis.
- **Token Bill.** All estimators use the linearized ratio variance at the randomization unit, as the simulation code does. Report the number of clusters next to the number of observations.
- Sources: arXiv 1803.06336; arXiv 2411.00640; PointFive ICC (F5).

### F15. Continuous monitoring: peeking inflates false positives, so use pre-registered looks or confidence sequences (strong)
- **Literature.**
  - Johari, Koomen, Pekelis & Walsh (Operations Research 70, 2022; arXiv 1512.04922 v3): p-values under continuous monitoring are "wholly unreliable". The mixture SPRT (mSPRT) gives always-valid p-values and was deployed at Optimizely.
  - Howard, Ramdas, McAuliffe & Sekhon (Ann. Statist. 49(2) 2021): nonparametric confidence sequences.
  - Waudby-Smith et al. (arXiv 2103.06476, v 2024-03-14): **asymptotic confidence sequences** that need only CLT-type conditions. The closed-form boundary is μ̂ ± σ̂·√(2(tρ²+1)/(t²ρ²)·log(√(tρ²+1)/α)).
  - Lindon, Ham, Tingley & Bojinov (JASA 2026, doi 10.1080/01621459.2026.2692052): anytime-valid **regression-adjusted** (CUPED-like) inference, demonstrated on Netflix A/B data.
  - Ramdas et al. (arXiv 2210.01948): overview of anytime-valid inference.
- **Simulation.**
  - Fleet A/A, 200 workspaces, CUPED: a single look had 5.5% false positives, **daily peeking 17.5%**, and 6 weekly Bonferroni looks 2.5% (`c_peek.out`).
  - Real thread stream, 735 pairs, 1,000 reps: naive peeking after every pair had **23.8%** false positives, the asymptotic CS 0.2%.
  - Under a real 29.9% lever, the CS detected in 43.8% of runs (median stop at pair 51). Estimates at stopping were inflated **×1.12**.
- **Token Bill.**
  - The dashboard shows a CS, not a p-value.
  - Enforce a burn-in of at least 100 units, or 2 weeks.
  - "Verified" only at a pre-registered look.
  - Report the sequence-adjusted interval, not the point estimate at stopping.
- Sources: arXiv 1512.04922, 1810.08240, 2103.06476, 2210.08589, 2210.01948; `causal_sim/c_peek.out`, `c_seq.out`.

### F16. Choose and guard the unit: cost per active developer-day is primary; per PR, per prompt and per task are gameable (moderate to strong)
- **Units the data supports.** The Claude Code Analytics Admin API returns **daily per-user** records: sessions, lines of code, commits, **PRs by Claude Code**, tool accept/reject, and per-model tokens with estimated cost. OTel adds per-request detail. Cost per active dev-day is therefore directly supported.
- **Each unit's failure mode:**
  - **Per PR:** PR splitting, and Claude-attributed PRs versus all PRs.
  - **Per human prompt:** prompt batching. Prompt counts also change endogenously when the agent improves.
  - **Per task:** task definition and selection.
- **METR.**
  - Early 2025: developers **forecast a 24% speed-up and still believed in a 20% speed-up** while actually taking 19% longer.
  - 2026: developers opted out of no-AI conditions and chose tasks strategically, so the signal became unreliable and self-reports "can be quite unreliable".
  - The time differences may not represent value differences, because task types were substituted.
- **Goodhart.** Manheim & Garrabrant (arXiv 1803.04585) classify how optimizing a proxy breaks its link to the goal: regressional, extremal, causal and adversarial.
- **Token Bill.**
  - **Primary metric:** cost per active dev-day at constant prices, analyzed intention-to-treat. Do not let developers opt out of arms after assignment.
  - **Guardrails:** merged PRs per dev-day, and revert or rework rate.
  - **Secondary:** cost per merged PR (delta method).
  - Never use self-reports.
- Sources: platform.claude.com claude-code-analytics-api.md (retrieved 2026-09-23); METR blog 2025-07-10 and 2026-02-24; arXiv 1803.04585.

### F17. Overlapping levers: sequential attribution depends on order; Shapley is exact and order-free (strong)
- **Guidance and practice.**
  - Anthropic's bundled cost-optimization guidance says ceilings that claim the same tokens are **mutually exclusive**. Compute each unconditionally, rank them, then deflate each for overlap with the levers above it, so the shortlist never sums past the bill.
  - AWS Cost Optimization Hub **de-duplicates overlapping recommendations**. For example, delete and rightsize are exclusive, and it keeps the higher. It also warns that estimated monthly savings is "a quick approximation".
  - The FinOps Framework warns that rate and usage optimizations **double count** cost avoidance and recommends usage before rate.
  - Shorrocks (J. Econ. Inequality 11:99–126, 2013; 1999 draft opened) shows sequential elimination decompositions are exact but **path-dependent**. Averaging over all m! orders (Shapley) removes the path dependence.
- **Measured on the real calls** (`a_levers_attribution.out`, A1). Standalone ceilings: L1 $684.82, L2 $3,483.20, L3 $335.45. They sum to $4,503.47 against **$4,305.96 joint**, an overcount of $197.50 (4.6%).

| Lever | Standalone | Sequential range over 6 orders | Shapley |
|---|---:|---:|---:|
| L1 prefix fix | $684.82 | $659.32–$684.82 | $672.07 |
| L2 subagent → Sonnet 5 | $3,483.20 | $3,285.69–$3,483.20 | $3,384.45 |
| L3 output −20% | $335.45 | **$163.44–$335.45 (69% spread)** | $249.45 |

- **Token Bill.**
  - Compute 2^k joint replays (k ≤ 10 means 1,024 replays, cheap).
  - Report Shapley credit per lever in receipts.
  - Show the FinOps-order sequential waterfall in the action plan.
  - Never add standalone ceilings.
- Sources: bundled claude-api `shared/cost-optimization.md`; AWS COH docs (coh-savings-opportunities, coh-estimated-monthly-savings); finops.org Rate Optimization page; Shorrocks 2013 (Semantic Scholar metadata plus author's draft).

### F18. FinOps already separates "rate" from "usage" savings; Token Bill should adopt the same split (strong)
- **FinOps definitions.**
  - The Foundation's Rate Optimization KPI is the **Effective Savings Rate**: ESR = 1 − (actual spend with discounts / equivalent on-demand spend). Unused commitments and uncovered usage lower ESR.
  - ProsperOps (2019, updated 2026-02-23) presents ESR as the ROI output metric, as opposed to utilization and coverage inputs.
  - FOCUS v1.4 (tagged 2026-06-04) defines the cost columns:
    - ListCost is list unit price × quantity, "commonly used for calculating savings" from rate optimization.
    - ContractedCost is used for savings from negotiation.
    - EffectiveCost is amortized and includes all discounts.
    - BilledCost is the invoice.
  - AWS COH's **cost efficiency** = 1 − potential savings / optimizable spend, on rolling 30-day spend.
- **Token Bill.**
  - **Effective Token Savings Rate** = 1 − BilledCost / ListCost-equivalent. The list equivalent prices all input as uncached, on the standard tier and at list rates.
  - Break it down into caching, batch, TTL choice and negotiated discount. This is **exact** arithmetic on billed usage and needs no experiment.
  - Keep **usage savings**, meaning fewer or cheaper tokens because behaviour changed, separate. Those require causal verification.
  - Emit FOCUS-shaped rows so FinOps tools can ingest the output.
- Sources: finops.org/framework/capabilities/rate-optimization and usage-optimization (retrieved 2026-09-23); FOCUS_Spec v1.4 column files (GitHub tag v1.4, 2026-06-04); prosperops.com ESR post; AWS coh-cost-efficiency docs.

### F19. Org-wide changes without controls: treat them as an event study (RDiT/ITS) plus synthetic control, and label them "measured" (moderate)
- **The method.** Hausman & Rapson (NBER w23602, 2017, rev. 2018) warn that regression discontinuity in time differs from cross-sectional RD in three ways:
  - estimates are biased if autoregression is ignored;
  - short-run and long-run effects differ;
  - the design is closer to an **event study**.
- **Synthetic control.** Arkhangelsky, Athey, Hirshberg, Imbens & Wager (arXiv 1812.09970 v4 2021) combine DiD and synthetic control with unit and time weights, building on Abadie–Diamond–Hainmueller (JASA 2010, as cited there).
- **Token Bill.**
  - For price changes, org-wide model launches or server-managed settings, fit an ITS with weekly FE and AR errors.
  - Where possible, build a synthetic control from other workspaces or orgs, for example via a federated benchmark.
  - Label the result **"measured"**, never "verified".
- Sources: NBER w23602; arXiv 1812.09970.

### F20. Backtest the simulator: realization rate by lever class, with a published error bar (moderate: design plus simulation)
- **Worked example** (stepped wedge, lever L2, 1,000 developers; `d_backtest_receipt.out`):
  - Replay projection: $4.398 per treated dev-day ($75,764).
  - Verified: **$2.958 (95% CI $1.782–$3.917)**, or $50,965.
  - Realization rate: **0.67 (CI 0.34–0.98)**. The true value in this synthetic world is 0.908, reflecting the +20% trajectory penalty.
  - The CI missed the truth by about 2% in this draw. Coverage across 40 worlds was 95% (F6).
  - Even a 25% lever is known only to about ±40% at this fleet size.
- **Literature priors.** Energy-efficiency RRs were about 0.25–0.4 (F4). LLM token-reduction RRs can be **negative** (F5).
- **Token Bill.**
  - Store every verified rollout's (projection, verified, CI) triple by lever class.
  - Maintain an empirical-Bayes RR distribution per class.
  - Publish new projections as replay × [RR p10, p50, p90].
  - Until a class has at least 3 verified rollouts, use a conservative prior:

    | Lever class | RR prior |
    |---|---|
    | exact rate | 1.0 |
    | deterministic request transforms | 0.8–1.0 |
    | trajectory-changing | −0.2 to 1.0 |
    | behavioural | no replay; field-only |

- Sources: `causal_sim/d_backtest_receipt.out`; F4 and F5 sources.

### F21. Assignment, telemetry and reconciliation plumbing already exists in the Anthropic stack (strong)
- **Cohort labels.** Claude Code OTel supports `OTEL_RESOURCE_ATTRIBUTES`, for example `department=…,team.id=…,cost_center=…`, and can copy them onto metric data points. This can carry `tokenbill.arm` and `tokenbill.wave`, delivered through the managed-settings `env` block.
- **Usage and cost data.**
  - The Admin API **usage report** buckets at 1m, 1h or 1d. The **cost report** is daily only and groups by workspace or description.
  - Priority Tier costs are *not* in the cost endpoint and must be taken from usage.
  - Data typically appears within 5 minutes.
- **Sample ratio mismatch.** Fabijan et al. (KDD '19) found about **6%** of Microsoft experiments had an SRM.
- **Token Bill.**
  - Tag arm and wave through `env`.
  - Reconcile ledger ↔ `cost_report` per workspace-day, adding Priority Tier from usage.
  - Run an SRM χ² on active dev-days by arm, and block "verified" if p < 0.001.
- Sources: code.claude.com monitoring-usage.md; platform.claude.com usage-cost-api.md; KDD '19 SRM PDF.

### F22. Long-run behaviour: learning, novelty and rebound need a holdback (moderate)
- **The evidence.**
  - Hohnhold, O'Brien & Tang (KDD '15, Google) show that user learning effects matter and are measurable with long-running designs.
  - METR's 2026 selection effects (F16) and the rebound findings (F4) point the same way.
  - Effects measured in week 1 may decay (novelty) or grow (learning), and cheaper calls may raise volume.
- **Token Bill.** Keep a **5% long-term holdback** per lever class for 8–12 weeks, clustered by workspace. Report the effect trajectory by week since adoption: CS event-study aggregation.
- Sources: KDD '15 PDF; METR 2026-02-24.

---

## 4. Worked synthetic rollout on `recs.pkl` (43,383 real Claude Code calls)

### 4.1 Construction
- **Day-blocks.** Every real call is priced exactly with the corpus rate card, and three levers are replayed per call (§1). Calls aggregate into 54 active-day blocks. Each block keeps its real cost mix, L1 share, L2 share, L2-eligible share (32.5% unweighted) and Opus share.
- **Fleet.** W workspaces × 25 developers, over 12 weeks × 5 weekdays. On each active day (p = 0.8), a developer draws a real day-block, scaled by:
  - developer intensity (lognormal σ_b = 1.0), calibrated to $13 per active dev-day;
  - a workspace factor (σ = 0.3);
  - workload growth (+1.5% per week);
  - a **33% Opus price cut in week 7**, which hits every arm.
  - 30% of developers join mid-window, with no pre-period.
- **Lever A (L1, ≈5.9%).** Deployed per workspace. If developers are randomized inside a workspace, 30% of the effect leaks to controls.
- **Lever B (L2, ≈30% projected).** Deployed per workspace by MDM. The realized effect includes an assumed +20% trajectory penalty on switched subagent spend, so the true realization rate is about 0.91.
- **Adoption ramp.** The effect is 50% in the adoption week and 100% after.

### 4.2 Results
| Question | Result | File |
|---|---|---|
| Which staggered-DiD estimator? | CS −3 to −4%, imputation −1.8%, 95–98% coverage; naive +16.5% or −31%; TWFE −9.1% | `b_d1.out` |
| Targeting top spenders first? | CS +34%, imputation +32%, coverage 42–65% (regression to the mean) | `b_d1.out` |
| Individual vs cluster randomization? | individual −27.7% (contamination); cluster CUPED +3.4%, coverage 94% | `b_rand.out` |
| CUPED gain | SD $1.758 → $0.605 (−88% variance) | `b_rand.out` |
| Cap p99 | SD $0.452, but −13.2% $ bias | `b_rand.out` |
| MDE (80% power), CUPED, 4-week post, 6-week pre | 1,000 devs: 13.6% cluster / 11.9% individual; 5,000: 7.3% / 5.7%; 10,000: 4.5% / 4.0% | `c_mde.out` |
| Peeking | 5.5% single look → 17.5% daily peeking; 2.5% with 6 weekly Bonferroni looks | `c_peek.out` |
| Switchback, 1h carryover | naive 30 min −39.7%, 1 h −31.1%, daily −0.7% | `c_sw.out` |
| Anytime-valid | peeking 23.8% FP vs CS 0.2%; CS power 43.8%, stop-estimate ×1.12 | `c_seq.out` |
| Attribution | standalone sum overcounts by 4.6%; L3 credit spread 69% by order; Shapley L1 $672 / L2 $3,384 / L3 $249 | `a_levers_attribution.out` |
| Realization-rate backtest | projected $4.398 vs verified $2.958 (CI 1.782–3.917); RR 0.67 (0.34–0.98); truth 0.908; placebo −0.2% | `d_backtest_receipt.out` |
| Signed receipt | 1,498 canonical bytes; `ssh-keygen -Y verify` passes; 1 byte tampered fails | `d_backtest_receipt.out`, `receipt.*` |

### 4.3 Closed-form sample-size rule, cross-checked
Setup (`e_power_formula.out`):
- Kohavi's n = 16·CV²/δ², extended to a two-level lognormal fleet.
- Inputs: σ_b = 1.0 (assumption fitted to the published $13 mean and 90% below $30); σ_ws = 0.3; within-developer daily CV 1.132 (**measured**).

With D = 20 active days and CUPED, the developers needed per arm are:

| Effect δ | Developers per arm |
|---:|---:|
| 3% | 3,391 |
| 5% | 1,221 |
| 10% | 306 |
| 20% | 77 |

The simulation's MDEs are about 1.5× the formula's because of late joiners without pre-periods, trends and the price cut. Examples: 4.0% simulated vs 2.5% by formula at 5,000 per arm; 11.9% vs 7.8% at 500 per arm.

**Rule:** apply a 1.5× safety factor to the formula's MDE, which is about 2.3× on n. Better, compute the MDE by A/A re-randomization on the org's own pre-period data before launching.

---

## 5. What Token Bill should build: the verification protocol

Everything below can be implemented in the Python standard library. The simulation scripts already implement:
- CS ATT(g,t), imputation, TWFE-via-alternating-projections, CUPED and the ratio delta method;
- the stratified cluster bootstrap and Bojinov lag-p Hajek estimators;
- the asymptotic confidence sequence and Shapley over replays;
- canonical JSON and DSSE PAE.

They fit in about 600 lines. Signing shells out to `ssh-keygen -Y sign/verify`, which OpenSSH ships everywhere; version 10.3 was tested here.

### 5.1 Evidence labels, printed on every dollar figure
| Label | Meaning | Minimum evidence |
|---|---|---|
| **exact** | Arithmetic identity on billed usage, with no behavioural counterfactual | Billed usage plus a versioned rate card. Examples: Effective Token Savings Rate; repricing the same tokens under two rate cards (price variance); ledger ↔ invoice reconciliation delta. |
| **estimated** | Counterfactual from replay or simulation of a lever **not yet** deployed (IPMVP Option D) | Replay calibration status (F2). Shown as replay × realization-rate interval for the lever class (F20), plus the MDE for the planned design. "Uncalibrated" if the monthly NMBE/CV(RMSE) gate fails. |
| **measured** | Observed change against an adjusted baseline, without randomized assignment (Option C style) | ITS/RDiT, CS-DiD with self-selected waves, or synthetic control. Placebo/pre-trend test passed. CI reported. Assumptions listed. Portfolio FSU ≤ 25% at 90%. |
| **verified** | Causal estimate from randomized (or randomized-order) assignment, with every guard passing | Stepped wedge, cluster RCT or switchback, with every §5.6 criterion passing. The receipt is signed. |

### 5.2 Design per lever class
| Lever class (examples) | Interference / carryover | Design | Estimator | Label reachable |
|---|---|---|---|---|
| **Rate:** price-card change, negotiated discount, batch tier, TTL pricing on same tokens | none | none: arithmetic | Repricing at constant usage | exact |
| **Deterministic request transform, no trajectory change:** gateway `cache_control` placement, tool-order normalization, system-prompt pinning | Shared prefix within the cache domain; carryover ≤ TTL | Cluster RCT by workspace/route, **or** daily switchback with burn-in ≥ 1h | CUPED DiM at the cluster level; lag-p Hajek for switchbacks | verified |
| **Managed-settings default affecting behaviour:** model/effort default, `CLAUDE_CODE_SUBAGENT_MODEL`, `autoCompactWindow`, tool deferral | Shared cache domain; session stickiness | **Stepped wedge** over MDM groups (endpoint-managed), randomized wave order, 10–25% never-treated holdback | CS ATT(g,t) or imputation at constant prices + CUPED; event-study by weeks since adoption | verified |
| **Org-wide only:** server-managed settings, org gateway, provider model launch | Everyone at once | Daily switchback if reversible; otherwise ITS/RDiT + synthetic control | Lag-p Hajek; AR-error ITS; SDID | verified (switchback) / measured |
| **Opt-in tool or plugin:** RTK-like hooks, MCP servers | Self-selection | **Encouragement design:** randomize the invitation or default-on | ITT, plus complier effect via Wald ratio | verified (ITT) |
| **Training / guidelines** | Team spillover | Cluster RCT by team; long holdback | CUPED DiM; event-study | verified |
| **Lab benchmarking of a lever before rollout** | – | Paired billed campaign (PointFive/JetBrains style), ≥ 50 tasks × 5 trials | Task-clustered bootstrap; Wilcoxon; cost per success | estimated (lab) → prior for RR |

### 5.3 Baseline adjustment model

**Routine adjustments** (always applied):
1. **Unit normalization.** Cost per active developer-day. "Active" means at least one billed request that day, from the Analytics API or OTel.
2. **Constant rate card.** Reprice every call in both periods with the rate card in force at pre-registration, and record its hash. Report the billed-price effect separately, as an exact price variance.
3. **Calendar.** Week fixed effects (DiD), or hour-of-week (switchbacks).
4. **Pre-period covariate (CUPED).** The same metric over the prior 4–8 weeks, with a missing-pre indicator for new hires.
5. **Workload exposure, only if pre-treatment.** Sessions, PRs or human prompts in the *pre* period.

**Never adjust for post-treatment mediators.** Tokens per call, turns, model mix and cache hit rate are what the lever changes. Adjusting for them erases the effect.

**Non-routine adjustments** (logged events):
- re-orgs, bulk onboarding, repo migrations;
- Claude Code version jumps, provider model launches, outages or incidents.

Each becomes an excluded window, an event dummy or a re-baselining, with a reason string included in the receipt.

### 5.4 Interference guard
1. **Cluster at the cache-isolation boundary.** That is the workspace on the Claude API, Claude Platform on AWS and Foundry, and the org on Bedrock and Vertex. Where one org shares a cache pool (Bedrock/Vertex), only between-org or switchback designs avoid interference.
2. **Cluster by repo** when the lever lives in the repo, such as `CLAUDE.md`, skills or hooks.
3. **Washout.** Drop max(1h cache TTL, p90 session length, settings-refresh interval) after each switch or adoption. For stepped wedges, weight the adoption week at 0 or 0.5.
4. **Test for interference** (Pouget-Abadie/Saveski two-level design) for any new lever class. Report the individual/cluster spillover ratio.
5. **Enforce assignment compliance.** Hash the assignment log before launch, check SRM on active dev-days by arm, and analyze intention-to-treat.

### 5.5 Sample-size rules
- **Before launch**, compute the MDE by **A/A re-randomization on the org's own last 8–12 weeks**. Cross-check against n = 16·CV²·(1 − ρ²)·DEFF/δ² with a 1.5× safety factor.
- **Refuse to launch as "verification"** when MDE > 0.8 × projected effect, and say which fleet size or window would suffice. In the synthetic fleet a 6% cache lever needs about 5,000–10,000 developers.
- **Cluster count.** At least 20 clusters per arm, using t-critical values with df = clusters − 2. Prefer ≥ 50.
- **Skewness check.** If the per-unit skewness s gives fewer than 355·s² units per arm, aggregate to coarser units (dev-window) or use cluster-bootstrap percentile intervals.
- **Duration.** At least 2 full weeks post-washout, for day-of-week balance. At least 4 weeks for levers expected to show learning. Keep the holdback running for 8–12 weeks.

### 5.6 Acceptance criteria for "verified"
1. **Pre-registration hash** committed before launch: lever, design, unit, estimator, looks, rate card, assignment log.
2. **SRM** p ≥ 0.001 on active dev-days and developers by arm.
3. **Placebo/pre-trend** test (fake adoption in the pre-period) within ± MDE/2, and its CI includes 0.
4. **Ledger ↔ invoice reconciliation** within ±1% per workspace-month against Admin API `cost_report`, plus Priority Tier from `usage_report`.
5. **Interference guard** satisfied (§5.4).
6. **95% CI lower bound > 0** at a pre-registered look, or an anytime-valid CS excluding 0 after the burn-in. Portfolio FSU ≤ 25% at 90%.
7. **Quality guardrail non-inferiority.** Merged PRs per dev-day and revert rate within pre-set margins.
8. **Realization rate recorded** against the replay projection.
9. **Receipt signed** by the FinOps key and archived.

### 5.7 Simulator calibration gate (Option D)
- **Monthly gate.** Before any projection is shown without the "uncalibrated" flag, as-billed replay must reproduce the invoice per workspace-month within **NMBE ±5% and CV(RMSE) ≤ 15%**.
- **Daily gate.** For daily series, **±10% / ≤ 30%**. These are FEMP's ASHRAE Guideline 14 tolerances.
- **Component-level calibration.**
  - chars→tokens per tool type: currently 3.7, measured 2.2–2.7;
  - cache hit/miss classification against billed `cache_read`;
  - model price coverage: currently fails for Opus 5.5.
- **Re-calibrate** on every rate-card or tokenizer change.

### 5.8 Attribution
- **Receipts.** Shapley credit over 2^k joint replays for projections, and over factorial or sequential rollouts for verified effects.
- **Action plan.** A FinOps-order sequential waterfall: usage levers, then rate levers.
- **Guard.** Never sum standalone ceilings.

### 5.9 Receipt schema (v0, demonstrated)
```json
{"_type":"urn:tokenbill:receipt:v0",
 "subject":[{"name":"lever:<id>=<value>","digest":{"sha256":"<hash of managed-settings payload / gateway commit>"}}],
 "predicate":{
  "label":"verified|measured|estimated|exact",
  "lever":{"id","class","delivery"},
  "scope":{"workspaces","developers","treated_dev_days"},
  "design":{"type","waves_week","never_treated","assignment_log_sha256","randomisation_seed","estimator","inference"},
  "metric":{"unit":"cost per active developer-day","prices":"constant baseline rate card","rate_card_sha256"},
  "result":{"saving_per_unit_usd_micro","ci95_usd_micro":[lo,hi],"total_saving_usd_micro",
            "projected_per_unit_usd_micro","realisation_rate_milli","realisation_rate_ci95_milli"},
  "guards":{"srm_p","placebo_pre_trend_usd_micro","ci_excludes_zero","invoice_reconciliation_delta_ppm",
            "interference","calibration":{"nmbe_ppm","cvrmse_ppm"},"quality":{...}},
  "adjustments":{"routine":[...],"non_routine":[{"event","window","treatment"}]},
  "attribution":{"method":"shapley","levers":{...}},
  "window":{"baseline","reporting","washout"},
  "tool":{"name","version"},"created"}}
```
- **Money** is integer micro-USD and ratios are integer milli-units, so canonicalization never meets floats. Python's `json.dumps(sort_keys=True, separators=(",",":"))` then matches RFC 8785 JCS for this subset.
- **Signing.** The signature covers the DSSE PAE `"DSSEv1" SP len(type) SP type SP len(body) SP body`. The envelope follows in-toto: subject, predicate, envelope. Signing uses `ssh-keygen -Y sign -n tokenbill-receipt`, and verification checks against an `allowed_signers` file.

### 5.10 Commands to ship, in stdlib order
1. `tokenbill ledger` + `reconcile`: ingest OTel, the Analytics API and transcripts; reconcile to `cost_report`; produce exact labels and ESR.
2. `tokenbill calibrate`: the Option D gate (NMBE, CV(RMSE), per-component).
3. `tokenbill project`: replay ceilings, Shapley overlap, RR-adjusted intervals, MDE for the proposed design.
4. `tokenbill plan-rollout`: randomized stepped-wedge or cluster assignment by cache domain; MDM payloads per wave; `OTEL_RESOURCE_ATTRIBUTES` arm tags; pre-registration hash.
5. `tokenbill monitor`: CUPED estimates, SRM and CS at pre-registered looks.
6. `tokenbill verify`: the final estimate, all §5.6 guards, and a signed receipt.
7. `tokenbill backtest`: the RR database by lever class, feeding back into `project`.

---

## 6. Open questions
- **Fleet parameters.** Real between-developer dispersion and workspace ICC in large enterprises. This work used σ_b = 1.0 inferred from Anthropic's $13 / p90 < $30 figures. Power users like this corpus's ($215 per active day) suggest heavier tails. Token Bill should estimate both from each org's pre-period.
- **Settings propagation.**
  - How quickly managed-settings changes reach running Claude Code sessions: whether sessions keep old settings until restart, and the refresh interval.
  - This sets the carryover order m for switchbacks and the washout for stepped wedges. It is not documented in the pages opened here.
- **Per-user cost outside Claude Code.** The Analytics API gives per-user estimated cost for Claude Code only. Raw Messages API traffic needs the gateway or OTel for per-developer attribution.
- **Quality guardrails.** Which outcome metrics (merged PRs, reverts, review comments) are both available and resistant to gaming for non-inferiority margins, and what margins enterprises accept.
- **Governance.** HR, works-council and ethics constraints on randomizing developers' tooling. The stepped wedge with universal eventual rollout is usually the most acceptable form.
- **Cross-org benchmarking.** Whether a federated donor pool across enterprises (synthetic control for org-wide changes) is feasible under privacy constraints.
- **Venue confirmation.** Some venues could not be confirmed from primary pages. Kept as arXiv: Waudby-Smith et al. 2103.06476, Pouget-Abadie et al. 1704.01190, Deng–Knoblich–Lu 1803.06336, Johari–Li–Liskovich–Weintraub 2002.05670, SDID 1812.09970, BJS 2108.12419.

---

## 7. Sources, all opened on 2026-09-23 unless noted

**M&V and energy economics**
- DOE FEMP, *M&V Guidelines: Measurement and Verification for Performance-Based Contracts v4.0*, Nov 2015. https://www.energy.gov/sites/default/files/2016/01/f28/mv_guide_4_0.pdf
- EVO, IPMVP page with Core Concepts 2022 (full text behind login). https://evo-world.org/en/products-services-mainmenu-en/protocols/ipmvp
- CalTRACK Methods v2.0. https://docs.caltrack.org/en/latest/methods.html
- OpenEEmeter/OpenDSM README. https://raw.githubusercontent.com/openeemeter/eemeter/master/README.md
- Fowlie, Greenstone, Wolfram, *Do Energy Efficiency Investments Deliver?* NBER w21331, 2015-07; QJE 2018. https://www.nber.org/papers/w21331
- Davis, Fuchs, Gertler, *Cash for Coolers*, NBER w18044, 2012-05; AEJ: Economic Policy 6(4), 2014. https://www.nber.org/papers/w18044
- Allcott, *Site Selection Bias in Program Evaluation*, NBER w18373, 2012-09, rev. 2014-09; QJE 130(3), 2015. https://www.nber.org/papers/w18373
- Hausman, Rapson, *Regression Discontinuity in Time*, NBER w23602, 2017-07, rev. 2018-04. https://www.nber.org/papers/w23602

**Causal inference and experimentation.** Every paper below was opened as a PDF.

*Difference-in-differences and synthetic control*
- Callaway & Sant'Anna, *DiD with Multiple Time Periods*, arXiv 1803.09015 v4 (2020-12-01); J. Econometrics (per the Borusyak et al. bibliography).
- Goodman-Bacon, *DiD with Variation in Treatment Timing*, NBER w25018; J. Econometrics 225(2):254–277, 2021.
- Sun & Abraham, *Estimating Dynamic Treatment Effects in Event Studies…*, arXiv 1804.05785 v2 (2020-09-22); J. Econometrics (per bibliographies).
- de Chaisemartin & D'Haultfœuille, *TWFE estimators with heterogeneous treatment effects*, arXiv 1803.08807 v7 (2020-03-05); AER 110, 2020 (doi 10.1257/aer.20181169).
- Borusyak, Jaravel & Spiess, *Revisiting Event Study Designs*, arXiv 2108.12419 v5 (2024-01-16).
- Arkhangelsky, Athey, Hirshberg, Imbens & Wager, *Synthetic Difference in Differences*, arXiv 1812.09970 v4 (2021-07-02). Cites Abadie–Diamond–Hainmueller, JASA 105(490), 2010.
- Hemming, Lilford & Girling, *Stepped-wedge cluster randomised controlled trials: a generic framework…*, Statistics in Medicine (online 2014-10-24), PMC4286109.

*Switchbacks and interference*
- Bojinov, Simchi-Levi & Zhao, *Design and Analysis of Switchback Experiments*, arXiv 2009.00148 v4 (2025-09-17); Management Science 69(7):3759–3777, 2023 (per the Hu & Wager bibliography).
- Hu & Wager, *Switchback Experiments under Geometric Mixing*, arXiv 2209.00197 v5 (2026-09-21).
- Ugander, Karrer, Backstrom & Kleinberg, *Graph Cluster Randomization*, arXiv 1305.6979 (2013-05-30); KDD '13.
- Aronow & Samii, *Estimating Average Causal Effects Under General Interference*, arXiv 1305.6156 v4; Ann. Appl. Stat. 11(4), 2017.
- Pouget-Abadie et al., *Testing for Arbitrary Interference on Experimentation Platforms*, arXiv 1704.01190 v4 (2019-01-29).
- Johari, Li, Liskovich & Weintraub, *Experimental Design in Two-Sided Platforms*, arXiv 2002.05670 v5 (2021-09-26).

*Variance reduction, heavy tails and ratio metrics*
- Deng, Xu, Kohavi & Walker, *Improving the Sensitivity of Online Controlled Experiments by Utilizing Pre-Experiment Data* (CUPED), WSDM 2013, Rome, 2013-02-04. https://exp-platform.com/Documents/2013-02-CUPED-ImprovingSensitivityOfControlledExperiments.pdf
- Deng, Knoblich & Lu, *Applying the Delta Method in Metric Analytics*, arXiv 1803.06336 v4 (2018-09-12).
- Kohavi, Longbotham, Sommerfield & Henne, *Controlled experiments on the web: survey and practical guide*, Data Min. Knowl. Disc. 18:140–181 (online 2008-07-30; 2009).
- Kohavi, Deng, Longbotham & Xu, *Seven Rules of Thumb for Web Site Experimenters*, KDD 2014 (skewness table corrected 2015-01-06).
- Fabijan et al., *Diagnosing Sample Ratio Mismatch in Online Controlled Experiments*, KDD '19.
- Hohnhold, O'Brien & Tang, *Focusing on the Long-term*, KDD '15.
- Schultzberg & Ankargren, *Resampling-free bootstrap inference for quantiles*, arXiv 2202.10992 v2 (2022-03-09).

*Sequential and anytime-valid inference*
- Johari, Pekelis & Walsh, *Always Valid Inference*, arXiv 1512.04922 v3 (2019-07-16); Operations Research 70:1806–1821, 2022 (per the Ramdas et al. bibliography).
- Howard, Ramdas, McAuliffe & Sekhon, *Time-uniform, nonparametric, nonasymptotic confidence sequences*, Ann. Statist. 49(2):1055–1080, 2021 (arXiv 1810.08240 v9).
- Waudby-Smith, Arbour, Sinha, Kennedy & Ramdas, *Time-uniform central limit theory and asymptotic confidence sequences*, arXiv 2103.06476 (updated 2024-03-14).
- Lindon, Ham, Tingley & Bojinov, *Anytime-Valid Linear Models and Regression Adjusted Causal Inference in Randomized Experiments*, arXiv 2210.08589 v5 (2025-07-07); JASA 2026, doi 10.1080/01621459.2026.2692052.
- Ramdas, Grünwald, Vovk & Shafer, *Game-theoretic statistics and safe anytime-valid inference*, arXiv 2210.01948 v2 (2023-06-17), submitted to Statistical Science.

*Attribution, metrics and eval statistics*
- Shorrocks, *Decomposition procedures for distributional analysis: a unified framework based on the Shapley value*, J. Econ. Inequality 11:99–126 (online 2012-01-07). Author's 1999 draft opened: http://www.komkon.org/~tacik/science/shapley.pdf
- Manheim & Garrabrant, *Categorizing Variants of Goodhart's Law*, arXiv 1803.04585 v4 (2019-02-24).
- Miller, *Adding Error Bars to Evals*, arXiv 2411.00640 (2024-11-01).

**LLM cost evidence**
- Weinberger & Hozez (PointFive), *Token Reduction Is Not Cost Reduction*, arXiv 2607.12161 v5 (2026-08-12).
- Shiryaev (JetBrains), RTK paired A/B, July 2026. https://blog.jetbrains.com/ai/2026/07/rtk-claude-code-token-savings/
- Bai et al., *How Do AI Agents Spend Your Money?*, arXiv 2604.22750 v2 (2026-04-29).
- METR, 2025-07-10 and 2026-02-24 blog posts. https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/ ; https://metr.org/blog/2026-02-24-uplift-update/

**FinOps**
- FinOps Foundation, Rate Optimization capability (ESR KPI) and Usage Optimization capability. https://www.finops.org/framework/capabilities/rate-optimization/ ; https://www.finops.org/framework/capabilities/usage-optimization/
- FOCUS spec v1.4 column definitions (ListCost, ContractedCost, EffectiveCost, BilledCost), GitHub tag v1.4 (commit 2026-06-04). https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec
- ProsperOps, *Effective Savings Rate* (published 2019-04-17, modified 2026-02-23). https://www.prosperops.com/blog/effective-savings-rate/
- AWS Cost Optimization Hub docs: overview, cost efficiency, estimated monthly savings, savings opportunities (de-duplication). https://docs.aws.amazon.com/cost-management/latest/userguide/cost-optimization-hub.html

**Signing**
- DSSE protocol. https://github.com/secure-systems-lab/dsse/blob/master/protocol.md
- in-toto Attestation Framework spec v1.2. https://github.com/in-toto/attestation/blob/main/spec/README.md
- RFC 8785, JSON Canonicalization Scheme, June 2020. https://www.rfc-editor.org/rfc/rfc8785
- ssh-keygen(1), `-Y sign` / `-Y verify`. https://man.openbsd.org/ssh-keygen.1

**Anthropic / Claude Code**
- Prompt caching (workspace isolation, TTLs). https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Usage & Cost Admin API. https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- Claude Code Analytics API. https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api
- Claude Code costs ($13 per active day; 90% under $30). https://code.claude.com/docs/en/costs
- Monitoring (`OTEL_RESOURCE_ATTRIBUTES`). https://code.claude.com/docs/en/monitoring-usage
- Server-managed settings (no per-group). https://code.claude.com/docs/en/server-managed-settings
- Managed settings (per-group via file or MDM). https://code.claude.com/docs/en/managed-settings
- Bundled `claude-api` skill `shared/cost-optimization.md`: mutually exclusive ceilings; measured vs estimated labels; about 50 cases × 5 trials for cutover.

---

## 8. Reproduce
All paths are under `…/scratchpad/research/causal_sim/`. Stdlib Python 3.13, plus `ssh-keygen` for signing.

```
python3 a_levers_attribution.py      # -> a_levers_attribution.out, blocks.pkl
python3 b_fleet_rollout.py d1         # -> b_d1.out   (~90 s)
python3 b_fleet_rollout.py rand       # -> b_rand.out (~7 s)
python3 c_power_seq_switchback.py mde|peek|sw|seq   # -> c_mde.out, c_peek.out, c_sw.out, c_seq.out
python3 d_backtest_receipt.py         # -> d_backtest_receipt.out, receipt.json/.pae/.pae.sig/.dsse.json
python3 e_power_formula.py            # -> e_power_formula.out
```

- **Privacy.** Scripts print aggregates only: counts, dollars, token totals. No prompt, tool or content data.
- **Demo key.** `demo_signing_key` is a throwaway, for the demo only.
- **Source texts.** Extracted texts of all opened papers are in `research/causal_src/`.
