# VERIFY tests (`tests/v2/verify/`)

Package VERIFY (SPEC §13, D15): measurement (imputation DiD, CUPED cluster DIM, event-study ITS),
paired lab A/B, guards and labels, and DSSE receipts signed with OpenSSH.

| module | provides |
|---|---|
| `tokenbill/verify/stats.py` | `bootstrap_ci`, `cluster_bootstrap_ci`, `resample_counts`, `cuped`, `srm_pvalue` (χ² via the regularized gamma function `gammaincc`), `chi2_sf`, `wilson`, `ols`, `newey_west_se` |
| `tokenbill/verify/panel.py` | `build_panel` (§13.1), `rate_variance` (EXACT, R8), `org_series`, `panel_channels`, `cache_scope_clusters`, `cluster_of`, `parse_arm` |
| `tokenbill/verify/rollout.py` | `plan` (§13.2), `preregistration`, `assignment_log_text`, `verify_assignment`, `is_randomized`, `arms_for`, `payload_names`, `projection_per_dev_day`, `spend_targeting` |
| `tokenbill/verify/estimators.py` | `imputation_did`, `cuped_cluster_dim` (§13.3), `placebo_did`, `placebo_cuped`, `quality_lower_bound`, `point_estimate` |
| `tokenbill/verify/its.py` | `event_study_its` (§13.3), `its_point`, `fixed_b_critical_value` |
| `tokenbill/verify/label_policy.py` | `guards`, `decide`, `signable` (§13.4), `measure` (the whole measurement → `MeasurementResult`) |
| `tokenbill/verify/ab.py` | `paired_ab` (§13.5), `lab_scope_label` |
| `tokenbill/verify/receipts.py` | `build_receipt`, `canonical_bytes`, `pae`, `sign`, `verify_envelope` (§13.6), `refusal_reasons`, `envelope_receipt`, `receipt_row`, `store_receipt` |

## Fixtures and provenance

Everything is **synthetic**; there are no checked-in data files and no real transcripts.

- `panelgen.py` — seeded (`common.rng`) cluster-day panels: `rollout_panel` (stepped wedge or
  cluster RCT; additive model `Y0 = a_c + b_t + ε`, a known relative effect, optional planted
  differential pre-trend, price cut, PR outcomes, explicit plan assignment) returning the
  in-sample truth; `org_series` (daily org series with day-of-week effects, AR(1) noise, a level
  shift and an optional planted pre-change ramp); `pre_panel`.
- `helpers.py` — `MemoryStore` ingest helpers and request builders (schema: `core.records`,
  SPEC §3.2; attribution arm/wave tags per §5.4), a minimal `ReconciliationReport` builder, the
  RTK-like lab campaign `rtk_campaign` (per-request usage chosen so that at `FlatRates` the
  candidate has ≈ −38% tokens, +14% turns, +7% cost), and `sample_measurement` (a hand-built
  `MeasurementResult` for receipts).
- Receipt signing tests generate a throwaway ed25519 key with `ssh-keygen` in `tmp_path`.

## Acceptance map (brief → test)

| acceptance item | test |
|---|---|
| stepped wedge, known 25% effect: estimate within ±5% of truth, 95% CI coverage ≥ 90% (10 seeds; 50 under `slow`) | `test_estimators.py::test_stepped_wedge_25pct_effect_recovered_{10,50}_seeds` |
| CUPED reduces variance on a correlated pre-period | `test_estimators.py::test_cuped_reduces_variance_on_a_correlated_pre_period`, `test_stats.py::test_cuped_reduces_variance_on_a_correlated_covariate` |
| ITS: 20% level shift within ±5%, MEASURED, placebo passes; planted pre-trend fails the placebo; never VERIFIED | `test_its.py`, `test_label_policy.py::test_its_org_wide_is_measured_at_most` |
| simultaneous 20% price cut: constant-price estimate unchanged, EXACT rate variance | `test_estimators.py::test_simultaneous_price_cut_…`, `test_label_policy.py::test_price_cut_moves_only_the_rate_variance`, `test_panel.py` (FakePricer + 0.8 contract), `test_e2e_fakes.py` |
| 60/40 realized vs 50/50 planned → SRM fails → not VERIFIED | `test_label_policy.py::test_srm_60_40_…`, `test_stats.py::test_srm_pvalue` |
| placebo with a planted pre-trend fails | `test_estimators.py::test_placebo_passes_on_clean_and_fails_on_planted_pre_trend`, `test_label_policy.py::test_planted_pre_trend_fails_the_placebo_guard` |
| MDE refusal when the planted effect is below MDE | `test_rollout.py::test_mde_from_200_aa_rerandomizations_and_refusal`, `test_label_policy.py::test_mde_refusal_caps_the_label` |
| spend-targeted assignment caps the label at MEASURED | `test_rollout.py::test_spend_targeted_user_assignment_…`, `test_label_policy.py::test_spend_targeted_assignment_…` |
| an unregistered look fails its guard | `test_label_policy.py::test_unregistered_look_fails_its_guard` |
| panel covering an unreconciled channel → `signable=False` | `test_label_policy.py::test_unreconciled_channel_makes_the_measurement_unsignable` |
| `plan(org_wide_delivery=True)` without MDM/gateway clusters → `design="its"`, `verification_design=False` | `test_rollout.py::test_org_wide_without_mdm_or_gateway_is_its` |
| `ab` on an RTK-like campaign → `costlier`; VERIFIED only at `lab:<hash>` with randomized order and ≥ 5 trials | `test_ab.py` |
| receipt canonical bytes identical across two processes; sign/verify with ed25519 (`needs_ssh_keygen`); flipped byte fails; ESTIMATED / allowance / unsignable refused; missing `ssh-keygen` → clear error | `test_receipts.py` |
| gate: estimators on `synth.lanes_gen.rollout_panel` (+ `rollout_truth`, `ab_campaign`) | `test_gate_synth.py` (`gate`; verified locally against a scratch overlay of `pkg/SYNTH-ORACLE` c5c7763 and again in review at 42e0f15: all pass, incl. the 50-seed `slow` variant) |
| gate: `build_panel` on a real `SqliteStore` equals MemoryStore | `test_gate_store.py` (`gate`; STORE not available on this branch) |

Also: `test_fuzz.py` (hypothesis: ITS series, panels, A/B outcomes, canonical JSON, DSSE envelopes,
receipt rows, `created`/arm parsing, plan inputs, pre-registration JSON — only `TokenbillError`
escapes), `test_e2e_fakes.py` (MemoryStore → pre-period panel → plan → tagged rollout → panel →
measure → receipt → sign → `put_receipt`, canary-named clusters never reach outputs).

Markers: `slow` (50-seed recovery), `gate`, `needs_ssh_keygen`.

## Contract notes and interpretations (for consumers: CLI-SAVINGS, OUT, PLAN)

1. **Units.** `MeasurePlan.mde_nano` and the `projection` passed to `plan` are nano-USD **per
   active developer-day** (the metric's unit); `projection` is a projected **saving** (positive =
   cheaper). `rollout.projection_per_dev_day` converts a monthly projection.
2. **`MeasurementResult.estimate` is a saving** (`−ATT`, positive when the treated arm got
   cheaper) with its CI; estimators return the ATT (treated − control, negative = cheaper). The
   realization rate is `estimate / projected`.
3. **`build_panel`** (module docstring): on the billed class only exact lines count
   (`PricedInference.exact_nano`, mirroring `cluster_day.exact_nano`, so the rate variance stays
   EXACT); range lines and unpriced inferences contribute 0; the allowance class counts the
   list-equivalent point. `arms` values are `"<arm>"` or `"<arm>@YYYY-MM-DD"`; `control`/`holdback`
   are never treated; without a date, treatment starts on the first day the cluster's telemetry
   carries its assigned arm tag; treatment is absorbing (ITT). `outcome_prs` = the largest
   `pull_requests` per team-day across outcome sources. Lanes with cache scope `"unknown"` are
   skipped by `cache_scope_clusters`. Active developer-days come from `cluster_days`, which splits
   a cluster-day by arm/wave tag; a developer whose requests carry two tags on one day (the MDM
   payload landing mid-day, tagged and untagged sources) is counted once when every active request
   of that cluster-day still carries its principal (else, identity purged, the per-tag counts are
   summed).
4. **Labels.** `decide` returns ESTIMATED ("not a measurement") when no label applies — an ITS
   whose placebo fails, or no CI. SPEC §13.4: `measure` "never emits EXACT or ESTIMATED", and
   `MeasurementResult.estimate` is MEASURED or VERIFIED; so `label_policy.measure` raises
   `NotAMeasurement` (a `GateFailed`: CLI exit 3) whose `guards` and `estimate` (the unlabeled
   ESTIMATED saving with its interval) carry the diagnostics. A spend-targeted or user-supplied
   assignment is MEASURED at best (no logged seed; guard `assignment_not_spend_targeted`).
5. **`plan` extensions** (keyword-only, defaults keep the SPEC signature): `change_date` (ITS; default
   the day after the pre-period panel) and `rate_card_sha256` (recorded in the pre-registration).
   The pre-registration JSON carries `{lever, metric, estimator, looks, rate_card_sha256, mde,
   projection}` plus `design, cluster_kind, seed, assignment ("seeded"|"user"|"org_wide"),
   assignment_log_sha256, arm_shares, spend_targeted, waves, payloads, washout_hours,
   change_date/placebo_date (ITS)` — integers and strings only.
6. **Receipts.** Additive predicate fields `basis` and `signable` (the refusal rules need them);
   booleans are the strings `"true"`/`"false"`; absent optional values are omitted;
   `calibration` is downgraded to `uncalibrated` when the measurement's projection is not
   CALIBRATED. `sig` is the base64 of the armored SSH signature file; `keyid` is the key's
   `SHA256:` fingerprint from `KEY.pub` (empty when absent). Refusals raise `GateFailed` (exit 3);
   a missing `ssh-keygen` raises `SshKeygenUnavailable`.
7. **A/B.** The verdict comes from the 95% CI of the mean per-task paired cost difference
   (candidate − baseline per task run); cost per success (failures in the numerator) is reported
   with its own interval. `measurement.unit = "cost per task"`, `lever_id = "unspecified"`
   (`paired_ab` has no lever argument), `signable = False` (a lab result is not invoice savings).
   `randomized_order` = every trial has both arms with distinct integer `order` values and each arm
   ran first in some trial.
8. **ITS small-sample interval.** Newey–West (lag 7) SEs get the `n/(n−k)` factor and the
   Kiefer–Vogelsang fixed-b critical value; with ~90 pre-days and AR(1) 0.3 noise the placebo still
   falsely fails ≈ 9% of series (pinned by `test_its.py::test_placebo_and_interval_calibration…`),
   so ITS results are MEASURED at best and a failed placebo is reported, not hidden.
9. **SRM** compares active developer-days by arm with the pre-registered shares (pre-period
   dev-days of the assigned clusters). Dev-days are clustered, so on very large panels organic
   headcount drift between arms can also trip p < 0.001; that is reported as a failing guard.
10. **Cache scope guard.** Cache-touching lever classes are `cache_transform` and `trajectory`; a
    lever id missing from `core.catalog` is treated as cache-touching.
11. **CUPED** targets the population ATT (a missing-pre indicator joins the regression for new
    clusters); with an effect proportional to cluster level its CI need not bracket the in-sample
    truth, so its tests check accuracy, not in-sample coverage.
12. Contract gap filed: `CONTRACT-CHANGE-VERIFY-1.md` (`cluster_days` kind `gateway`).
13. **Input validation.** Every date VERIFY reads is strict `YYYY-MM-DD` (`stats.iso_date`):
    Python 3.11+ `date.fromisoformat` also accepts `YYYYMMDD` and ISO week dates, which would make
    3.10 and 3.12 disagree and break the string ordering of dates. Receipt `created` is parsed by
    hand (date, or date-time with optional seconds, ≤ 6-digit fraction and `Z`/`±HH:MM`; no offset
    = UTC). `panel.check_rows` validates `PanelRow`s for every consumer (int money and dev-days,
    bool `treated`); `guards` type-checks `result_inputs`. Malformed input raises `UsageError`. The
    reconciliation window is compared by the `YYYY-MM-DD` its bounds start with (a date or an ISO
    date-time); anything else fails the guard as "not comparable".

## Facts

- Kiefer–Vogelsang (2005) fixed-b 0.975 quantile for the Bartlett kernel,
  `1.9600 + 2.9694 b + 0.4160 b² − 0.5324 b³` — **verified 2026-09-23** against
  <https://www.york.ac.uk/media/economics/documents/discussionpapers/2015/1515.pdf> (which quotes
  KV 2005; re-checked in review on 2026-09-23: the paper prints α0 = 1.9600, α1 = 2.9694,
  α2 = 0.4160, α3 = −0.5324 for the 0.975 quantile).
- DSSE PAE and the OpenSSH `-Y sign/verify` invocations are SPEC §19.7 facts; the DSSE example
  vector `DSSEv1 29 http://example.com/HelloWorld 11 hello world` is pinned in
  `test_receipts.py`.
- All other constants (MDE = 2.8 × SD over 200 A/A draws, 0.8 × |projection|, SRM p < 0.001,
  −5% non-inferiority at one-sided 90%, B = 2,000 / 10,000, HAC lag 7, 10–25% holdback) are SPEC
  §13 parameters, not external facts.
- **Unverified:** none.
