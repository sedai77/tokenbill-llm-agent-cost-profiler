# Fact-check: gap-fleet-causal-savings-verification

Checked 2026-09-23 by an adversarial fact-checker.

## How the checks were done

**Primary sources.** I opened them myself rather than relying on the researcher's cached `causal_src/` texts:
- PDFs were fetched with `curl` and converted with `pdftotext`: FEMP M&V 4.0, Cash for Coolers, PointFive, Bojinov, CUPED, Kohavi 2009 and 2014, Miller, the delta method, Johari, SDID, Fabijan SRM, Hohnhold, and the Shorrocks draft.
- NBER abstract pages; the arXiv API (all 22 IDs); Crossref for venues and DOIs; Europe PMC for Hemming.
- Anthropic docs through their public `.md` endpoints; the GitHub API for the FOCUS tags and column files.
- WebFetch for EVO, JetBrains, METR, AWS COH, the FinOps Foundation and ProsperOps.

Downloads are in `research/verify_fleet_causal/`.

**Simulations.** All scripts were copied to `research/vfc_sim/` and re-run there; the researcher's files were left untouched.
- `a_levers_attribution.py` reproduces `a_levers_attribution.out` byte-for-byte.
- `b_d1`, `b_rand`, `c_mde`, `c_peek`, `c_seq`, `c_sw`, `d_backtest_receipt` and `e_power_formula` all reproduce exactly, apart from elapsed-time lines. The signed receipt verifies (rc=0) and a one-byte tamper fails (rc=255) with OpenSSH_10.3p1.

**Reproducibility gap (not a numbers problem).** The shipped `a_levers_attribution.py` writes a `blocks.pkl` **without** the `sub` field (L2-eligible spend) that `b_fleet_rollout.py` needs.
- Following §8's order (run `a_…` first) overwrites the shipped `blocks.pkl`, and `b_fleet_rollout.py` then crashes with `KeyError: 'sub'`.
- The B/C/D/E outputs reproduce only with the shipped intermediate `blocks.pkl`, which was built by an unsaved version of script A.

**Extra check I ran.** `vfc_sim/rtm_check.py` isolates the regression-to-the-mean claim; results are under rtm-targeting.

## Verdicts

### mv-adjusted-baseline: CONFIRMED
- **FEMP M&V 4.0 (Nov 2015), p. 2-1.** Actual savings "cannot be measured because they represent the absence of energy or water use". Equation 2-1 is Savings = (Baseline − Post-Installation) ± Adjustments.
- **Adjustments.** The glossary defines routine adjustments (expected changes in independent variables) and non-routine ones (unexpected changes).
- **Option C requirements (§4.4):**
  - savings greater than about 10% to 20% of consumption;
  - at least 12 (preferably 24) months of pre-installation data;
  - at least 9 (preferably 12) months of performance data.
- **EVO.** The page exists. Core Concepts 2022 needs an EVO login ("login into you EVO web account").

### ashrae-calibration-gate: CORRECTED
**Confirmed:**
- FEMP Table 4-2 gives ±5% / 15% monthly and ±10% / 30% hourly.
- A re-run gives $10,786.64 vs $11,632.75, which is −7.3%.
- chars/3.7 against measured medians of 2.22–2.66 chars per token means about 28–40% fewer tokens.
- The activity-driver CV(RMSE) is 67.3% daily and 38.8% weekly.

**Wrong or overstated:**
1. **The −7.3% is not caused by the missing Opus 5.5 price.** `19_tokenbill_pricing_gap.out` shows unpriced `claude-opus-5-5` = **$66.61** (733 calls), only 0.6% of the bill. The $846.10 gap breaks down (per `empirical.verify.md`) as:

   | Component | Amount |
   |---|---:|
   | 1h cache writes priced at 1.25× instead of 2× | $758.63 (6.5%) |
   | Unpriced Opus 5.5 | $66.61 (0.6%) |
   | Fallback iterations | $20.86 |

   The dominant cause is TTL-unaware cache-write pricing.
2. **Guideline year.** FEMP's footnote cites "ASHRAE Guideline 14-**2015**, Section 5.3.3.3.10", not G14-2014.
3. **"NMBE" is loose.** It is a single-total bias; no monthly series or CV(RMSE) was computed for the replay.
4. **The tokenizer point is narrower than stated.**
   - `tokenbill/trace.py` uses chars/3.7 **only** to split each call's billed totals proportionally. Those splits are rescaled to the exact billed total, and the same ratio is also used for min-cacheable and breaker thresholds.
   - So the heuristic skews relative attribution and thresholds. It does not "under-count" billed tool tokens in any reported dollar total.
   - The 2.2–2.7 figures are chars per *appended* token for whole transitions, not pure tool-result text.

**Corrected claim.** v0.1.2's priced total is −7.3% vs billed on this corpus, failing ±5%. About 6.5 points come from pricing 1h cache writes at 1.25× instead of 2×, 0.6 points from the missing Opus 5.5 row, and about 0.2 points from fallback iterations. chars/3.7 overstates chars per token relative to the measured 2.2–2.7, which biases proportional splits and thresholds but not billed totals.

### caltrack-fsu: CORRECTED (minor)
**Confirmed** from CalTRACK Methods 2.0 (docs.caltrack.org):
- §4.3.2.4: FSU uses "a modified version of the ASHRAE Guideline 14 formulation".
- Sun & Baltazar daily coefficients a = −0.00024, b = 0.03535, d = 1.00286.
- §4.3.2.5: FSU_portfolio = √ΣΔU² / ΣU.
- §4.3.2.1: building CV(RMSE) default of 100% for portfolio use.
- §4.3.2.3: NWA "less than 15%", pay-for-performance "may require 25%".
- §4.3.2.6: "population trends" bias is not removed by aggregation.

**Correction.** The OpenDSM README (formerly OpenEEmeter) says comparison groups "are frequently used **after** DR/EEmeter" to correct for external population-level effects using non-participant meters. It lists "Integrate comparison groups" as a future **technical goal**. So OpenEEmeter/OpenDSM does not itself implement comparison groups yet; practitioners apply them downstream.

### projection-optimism: CONFIRMED
- **Fowlie, Greenstone, Wolfram (NBER w21331, July 2015).** More than 30,000 households; "model-projected savings are roughly 2.5 times the actual savings". QJE listed as the published version.
- **Davis, Fuchs, Gertler (w18044, May 2012), PDF §5.4.**
  - Refrigerators save 132 kWh/yr, "about one-quarter" of the World Bank's 481 kWh/yr.
  - AC replacement *increases* use by about 80 kWh/yr.
- **Allcott (w18373, Sep 2012, rev. Sep 2014; QJE 130(3) 2015).** 111 RCTs and 8.6M households; the first ten "substantially overstate efficacy in the next 101 sites".

### llm-token-not-bill: CONFIRMED
- **PointFive, arXiv 2607.12161 v5 (2026-08-12), by Weinberger and Hozez.**
  - Abstract: "paired, provider-billed" Claude Code tasks, where tool-output tokens −38.4% came with billed cost +6.8% (CI +2.8 to +11.3).
  - §5.4: task-clustered bootstrap with 10,000 resamples; ICC 0.37–0.55; Kish effective size of 38–45 tasks despite 712 runs per arm.
- **JetBrains (Shiryaev, July 2026).**
  - 425 billed trials, claude-sonnet-5, Claude Code 2.1.201.
  - Low effort: +7.6% (p = 0.004). High effort: +0.1% (p = 0.99).
  - "Identical attempts … differ by a median 22% in cost".
- **Bai et al., arXiv 2604.22750 v2 (2026-04-29).** Runs "can differ by up to 30x in total tokens".

### staggered-did: CONFIRMED
**Theory:**
- Goodman-Bacon (w25018; J. Econometrics 225(2):254–277, 2021): TWFE is a weighted average of all 2×2 DDs and is biased when effects change over time.
- de Chaisemartin & D'Haultfœuille (AER 110, 2020): negative weights.
- Sun & Abraham: contaminated leads and lags.
- Callaway & Sant'Anna v4 (2020-12-01) and BJS v5 (2024-01-16): correct IDs and dates.

**Simulation.** The re-run reproduces `b_d1.out` exactly: truth $3.248 (25.1%); naive +16.5% / −31.0%; TWFE −9.1%; CS −4.0% (98%); CS at constant prices −3.1%; imputation −1.8% (95%).

**Note.** Coverage is estimated from only 40 worlds, about ±3.5 pp Monte Carlo error. My independent-seed run gave CS −1.2% and imputation −1.2%, so the conclusion is robust.

### rtm-targeting: CORRECTED
**Confirmed.** The reported numbers reproduce exactly: CS +34.2% (coverage 65%) and imputation +32.3% (coverage 42%).

**Overstated: the attribution to regression to the mean, and the "about a third" magnitude.**
- Both headline figures are in **billed $**. In the same run, CS at **constant prices**, which is the report's own recommended analysis, is only **+9.2%**, and TWFE is +2.1%.
- My decomposition (`vfc_sim/rtm_check.py`, 80 worlds, own seed):

  | World | Estimator | Billed $ | Constant prices |
  |---|---|---:|---:|
  | Default (price cut + growth) | CS | +28.2% | +4.7% |
  | Default (price cut + growth) | Imputation | +31.3% | +6.3% |
  | No price cut, no growth (pure RTM) | CS | +16.8% | +16.8% |
  | No price cut, no growth (pure RTM) | Imputation | +23.4% | +23.4% |

- So the roughly +32–34% combines two things:
  - **Regression to the mean:** roughly +17–23%.
  - **Level-DiD bias from a multiplicative price cut hitting the higher-level, early-adopting workspaces:** about +25 points. Growth partly offsets it.

**Corrected claim.** Targeting the first wave at the highest weeks-1–4 spenders biases savings upward. The bias is about +17–23% from regression to the mean alone, and about +32–34% when billed-$ estimators also absorb the week-7 price cut. At constant prices the CS bias was +5–9% in this world. The direction is robust; the magnitude depends on estimator and price basis.

### stepped-wedge-mdm: CORRECTED
**Confirmed:**
- Hemming, Lilford & Girling, Stat Med 34(2) 2015, online 2014-10-24 (Europe PMC). The design suits interventions "implemented irrespective of evidence" or when it is "logistically implausible to roll out … simultaneously".
- Incomplete SW-CRTs where "implementation or transition periods, data are not collected".
- server-managed-settings.md: "Settings apply uniformly to all users in the organization. Per-group configurations are not yet supported."

**Correction.** managed-settings.md adds that "a self-hosted **Claude apps gateway** delivers managed settings per IdP group". So per-group policy does not *require* endpoint-managed files or MDM.

**Related: §6 "settings propagation … not documented" is wrong.** The docs state:
- server-managed settings are fetched at startup and polled **hourly**;
- MDM and HKCU policy is checked every **30 minutes**;
- file-based `managed-settings.json` is reloaded when the file changes.

This directly sets the carryover order m and the washout length.

### cache-interference-cluster: CONFIRMED
- **Prompt-caching docs.** Caches are "isolated per workspace … Claude API, Claude Platform on AWS, and Microsoft Foundry; Bedrock and Google Cloud maintain organization-level cache isolation". Hits need "100% identical prompt segments".
- **Papers.** Ugander et al. (1305.6979, 2013-05-30), Aronow & Samii (Ann. Appl. Stat. 11(4) 2017) and Johari–Li–Liskovich–Weintraub (2002.05670 v5, 2021-09-26) all exist as described.
- **Simulation.** The re-run reproduces `b_rand.out`: truth 5.70%; individual −27.7%; cluster CUPED +3.4%, coverage 94%.
- **Caveat.** The 30% leakage is an assumption, as the report already labels it. Johari et al. is about two-sided-market randomization bias, not clustering per se.

### interference-test: CONFIRMED
arXiv 1704.01190 v4 (2019-01-29), by Pouget-Abadie, Saveski, Saint-Jacques, Duan, Xu, Ghosh and Airoldi. The abstract states:
- it is Durbin-Wu-Hausman in spirit;
- it makes no assumptions on the interference model or the network;
- it has a sharp variance bound and an analytical type-I bound;
- it is illustrated on LinkedIn.

### switchback-carryover: CONFIRMED
**Bojinov, Simchi-Levi & Zhao** (arXiv 2009.00148 v4, 2025-09-17; Crossref: Management Science 69(7):3759–3777, 2023):
- T* = {1, 2m+1, 3m+1, …, (n−2)m+1};
- "when p < m, the Horvitz-Thompson estimator … will be biased";
- with heavy-tailed noise, "T = 120 is too small" and T = 1200 behaves normally.

**Hu & Wager** (v5 2026-09-21): error decays as T^(−1/3); burn-in gives log(T)^(1/2)·T^(−1/2).

**Simulation.** The re-run reproduces `c_sw.out`: 30-min −39.7%, 1h −31.1%, 4h −8.3%, daily −0.7% with SD 61.6% of a 6.10% effect.

### cuped: CONFIRMED
- **CUPED PDF (WSDM '13, Rome, Feb 4–8, 2013).**
  - var·(1−ρ²);
  - about 50% variance reduction at Bing;
  - the same variable from the pre-period "tends to give the best variance reduction";
  - §4.2 handles missing pre-experiment data with an indicator/binary covariate.
- **Simulation.** The re-run gives SD $1.758 → $0.605, which is 1 − (0.605/1.758)² = 88.2%, with coverage 94%. `e_power_formula` gives ρ = 0.955 at D = 20.

### heavy-tails-power: CONFIRMED
- **Kohavi et al. KDD 2014.** "355 × s² for each variant". Capping Revenue/User at $10 per week took "skewness drop from 18 to 5.3" and could "detect a change 30% smaller".
- **Kohavi et al. DMKD 18:140–181 (2009).** n = 16σ²/Δ².
- **Schultzberg & Ankargren** 2202.10992 v2 (2022-03-09) is at Spotify.
- **Local results reproduce:** skew 18.7 → 124,581; skew 2.1 → 1,514; MDE 13.6% / 7.3% / 4.5% for 40 / 200 / 400 workspaces × 25, cluster design, 4 weeks, CUPED; p99 cap −13.2%.
- **Minor.** The 2014 PDF carries a 2015-01-06 erratum changing the table skewness from 18.2 / 5.3 to 17.9 / 5.2. The capping sentence still says 18 → 5.3.

### ratio-delta-cluster-se: CONFIRMED
- Deng, Knoblich & Lu (1803.06336 v4, 2018-09-12) discuss the randomization unit versus the analysis unit and ratio metrics.
- Miller (Anthropic, 2411.00640, 2024-11-01), Table 4: clustered SEs "can be over 3X larger than naive". The largest ratio is 3.05.

### sequential-monitoring: CONFIRMED
- **Johari et al.**
  - Abstract: p-values are "wholly unreliable" under continuous monitoring; the method was deployed at "a large scale commercial A/B testing platform".
  - The authors were Optimizely employees or an advisor. The mSPRT is described.
  - Published in OR 70(3):1806–1821 (2022), with Koomen as co-author.
- **Howard et al.** Ann. Statist. 49(2) 2021.
- **Waudby-Smith et al.** v9 2024-03-14.
- **Lindon et al.** DOI 10.1080/01621459.2026.2692052, JASA, online 2026-08-27. The published title is slightly different.
- **Ramdas et al.** v2 2023-06-17.
- **Simulation.** The re-run reproduces 5.5% / 17.5% / 2.5%, then 23.8% vs 0.2%, and ×1.12.

### units-gaming: CONFIRMED
- **Claude Code Analytics API.** Daily aggregated records per actor: sessions, LOC, `commits_by_claude_code`, `pull_requests_by_claude_code`, and `estimated_cost` by model.
- **METR (2025-07-10).** Developers expected a 24% speed-up, believed in 20%, and actually took 19% longer.
- **METR (2026-02-24).** One developer did no AI-disallowed tasks. 30–50% of developers skipped submitting tasks they did not want to do without AI. Self-report estimates "can be quite unreliable". Time differences "may not represent value-differences".
- **Manheim & Garrabrant** v4 (2019-02-24): four Goodhart variants.

### shapley-attribution: CONFIRMED
- **Numbers.** The re-run of `a_levers_attribution.py` reproduces A1 exactly: $684.82 + $3,483.20 + $335.45 = $4,503.47 vs $4,305.96 joint (4.59%). L3 ranges $163.44–$335.45 (69.0%). Shapley is 672.07 / 3,384.45 / 249.45.
- **Shorrocks.** The 1999 draft says sequential elimination "is therefore exact" but has a "path dependence" problem, remedied over the m! sequences. Published in J. Econ. Inequality 11(1):99–126, online 2012-01-07.
- **AWS COH.** It "deduplicates amongst resource optimization strategies"; for example, delete versus rightsize keeps the higher.
- **FinOps.** Teams "may double count potential cost avoidance".
- **Bundled claude-api `cost-optimization.md` (2.1.280), line 86.** "Ceilings that claim the same tokens … are mutually exclusive".

### finops-rate-vs-usage: CONFIRMED
- **FinOps Rate Optimization.** ESR = 1 − (Actual Spend with Discounts / Equivalent Spend at On Demand Rate). Under-utilization and uncovered usage lower ESR.
- **FOCUS v1.4.** The release is dated 2026-06-04 on GitHub. The column files are:
  - ListCost: "commonly used for calculating savings based on various rate optimization activities";
  - ContractedCost: savings from negotiation;
  - EffectiveCost: includes amortized prepayments;
  - BilledCost: as invoiced.
- **AWS cost efficiency.** 1 − Potential Savings / Total Optimizable Spend, on rolling 30-day spend. The coh-estimated-monthly-savings page calls estimated savings "a quick approximation of future savings".
- **ProsperOps.** Published April 2019, updated February 2026. ESR is presented as the output (ROI) metric, as opposed to utilization and coverage inputs.
- **Nuance for the report body.** FinOps says usage-first is ideal "in an ideal world" but warns against waiting for usage optimization before doing rate work.

### its-rdit-org-wide: CONFIRMED
- **Hausman & Rapson** (w23602, July 2017, rev. April 2018; Annu. Rev. Resour. Econ. 2018). Estimates are biased if the AR structure is ignored or if short-run and long-run effects differ. The framework is "closer to an event study".
- **SDID** (1812.09970 v4, 2021-07-02). Unit and time weights; cites Abadie, Diamond & Hainmueller 2010.

### realization-rate-backtest: CONFIRMED
- **Worked example.** The re-run reproduces `d_backtest_receipt.out`: $4.398 ($75,764); $2.958 [1.782, 3.917] ($50,965); RR 0.67 (0.34–0.98); true 0.908; placebo −0.2%.
- **"About ±40%"** matches a CI of −40% / +32% around the point estimate.
- **EE priors.** 1/2.5 = 0.40 (Fowlie) and 132/481 = 0.27 (Davis) support RR ≈ 0.25–0.4.
- **Negative RR.** PointFive (fewer tokens, higher cost) supports it.
- **Caveat.** This is one synthetic draw; its CI misses the simulation truth of $3.991. The report discloses this.

### assignment-telemetry-srm: CONFIRMED
- **monitoring-usage.md.** `OTEL_RESOURCE_ATTRIBUTES="department=engineering,team.id=platform,cost_center=eng-123"`. `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES` defaults to true.
- **usage-cost-api.md:**
  - usage buckets 1m / 1h / 1d;
  - cost report "Daily granularity only (1d)", grouped by workspace or description;
  - Priority Tier "not included in the cost endpoint";
  - data "typically appears within 5 minutes".
- **Fabijan et al. (KDD '19).** "approximately 6% of experiments at Microsoft exhibit an SRM".

### signed-receipts: CORRECTED (minor)
**Confirmed:**
- DSSE: PAE(type, body) = "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body.
- RFC 8785 JCS (June 2020).
- ssh-keygen(1): `-Y sign -n namespace` and `-Y verify -f allowed_signers_file`.
- The demo re-runs: 1,498 canonical ASCII bytes, verify rc=0, one-byte tamper rc=255, OpenSSH_10.3p1.

**Correction.** in-toto Attestation v1.2 defines **four layers: Predicate, Statement, Envelope and Bundle**. "Subject" is a field of the Statement layer, not a layer.

**Also note.** The demo receipt uses a custom `_type` of `urn:tokenbill:receipt:v0` with no `predicateType`. It is in-toto-*shaped*, not a conforming in-toto Statement v1.

### long-run-holdback: CONFIRMED
- Hohnhold, O'Brien & Tang, "Focusing on the Long-term: It's Good for Users and Business", KDD '15 Sydney: the method quantifies long-term user learning effects.
- Cash for Coolers: AC +80 kWh/yr rebound (§5.4 of the PDF).
