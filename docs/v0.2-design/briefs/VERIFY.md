### VERIFY — Measurement (DiD, CUPED, ITS), lab A/B, guards, signed receipts (wave 2)

**Goal.** Turn rollouts into finance-grade evidence: randomized rollout plans at cache-isolation units (and an
honest ITS design for org-wide changes), MDE honesty, imputation DiD / CUPED / event-study ITS at constant
prices, guards that decide MEASURED vs VERIFIED, a paired lab A/B for vendor claims, and DSSE receipts signed
with OpenSSH (SPEC §13, D15).

**Owns.** `tokenbill/verify/{stats,estimators,its,ab,rollout,label_policy,receipts,panel}.py`,
`tests/v2/verify/**`.

**Consumes.** `core.types` (`MeasurementResult`, `MeasurePlan`, `AbResult`, `GuardResult`, `PanelRow`,
`ReconciliationReport`, `ReceiptRow`, `Policy`), `core.labels`, `core.money`, `core.protocols` (`LedgerStore`,
`Pricer`), `core.ids`, `common.rng`, `core.builders`, `core.testing` (`MemoryStore`, `FakePricer`).

**Provides.** `verify.stats` (`bootstrap_ci`, `cluster_bootstrap_ci`, `cuped`, `srm_pvalue` (χ² via the
regularized gamma function), `wilson`, `newey_west_se`); `verify.panel.build_panel(...)` (§13.1 signature);
`verify.rollout.plan(...) -> MeasurePlan` (§13.2); `verify.estimators.cuped_cluster_dim(...)`,
`imputation_did(...)`; `verify.its.event_study_its(...)` (§13.3); `verify.ab.paired_ab(...) -> AbResult`
(§13.5); `verify.label_policy.guards(...)`, `decide(...)`, `signable(...)` (§13.4);
`verify.receipts.build_receipt`, `canonical_bytes`, `pae`, `sign`, `verify_envelope` (§13.6).

**Build.** Exactly SPEC §13.1–§13.6: panel at the pre-registered baseline rate card with rate variance
reported separately as EXACT (allowance measured separately); plan with seeded wave order, 10–25% holdback,
washout, assignment log and pre-registration hashes, A/A MDE (200 re-randomizations) and the `MDE > 0.8 ×
|projection|` refusal, spend-targeting check, org-wide warning and `design="its"`; estimators (no TWFE, no
naive pre/post; floats internally, results to int nano with MEASURED/VERIFIED Figures); ITS with day-of-week
effects, HAC (lag 7) CI and a placebo date, MEASURED ceiling; guards (SRM, placebo, MDE, per-channel
reconciliation, cluster ≥ cache scope, team-level quality non-inferiority, registered looks, washout);
`decide`; `signable`; paired A/B with task-clustered bootstrap and the lab-scope VERIFIED rule; receipts
(integer-only canonical JSON, DSSE PAE, `ssh-keygen -Y` via an injectable `runner` without a shell; refusal
rules incl. allowance basis).

**Acceptance tests.**
- stepped-wedge panels with a known 25% effect (a local seeded generator): imputation estimate within ±5% of
  truth and 95% CI coverage ≥ 90% over 50 seeds (10 in PR CI, 50 under `slow`); CUPED reduces variance on a
  correlated pre-period;
- ITS: an org-wide series with a 20% level shift → estimate within ±5%, label MEASURED, placebo passes; a
  series with a planted pre-trend fails the placebo; never VERIFIED;
- a simultaneous 20% price cut leaves the constant-price estimate unchanged and appears as EXACT rate
  variance;
- 60/40 realized vs 50/50 planned → SRM fails → not VERIFIED; placebo with a planted pre-trend fails; MDE
  refusal when the planted effect is below MDE; spend-targeted assignment caps the label at MEASURED; an
  unregistered look fails its guard; a panel covering an unreconciled channel → `signable=False`;
  `plan(org_wide_delivery=True)` without MDM/gateway clusters → `design="its"`, `verification_design=False`;
- `ab` on an RTK-like campaign (tokens −38%, turns +14%, cost +7%) → `costlier`; VERIFIED only at
  `lab:<hash>` scope with randomized order and ≥ 5 trials per task-arm;
- receipt canonical bytes identical across two processes; sign then verify with a generated ed25519 key
  succeeds (`needs_ssh_keygen`); flipping one payload byte fails verification; signing an ESTIMATED,
  allowance-basis or unsignable receipt is refused; missing `ssh-keygen` gives a clear error;
- gate (`importorskip("tokenbill.synth.lanes_gen")`): the same estimator checks on `rollout_panel` output;
  gate (`importorskip("tokenbill.store.db")`): `build_panel` on a real `SqliteStore` equals the MemoryStore
  result.

**Size.** ~2.9k LOC including tests.
