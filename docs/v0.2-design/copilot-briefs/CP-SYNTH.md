### CP-SYNTH — Synthetic Copilot enterprise with planted causes and closed-form truth (wave 2)

**Goal.** A deterministic Copilot enterprise — billing rows, seats, activity, configuration, collector lanes,
VS Code traces and agentic-workflow runs as canonical records — with planted causes whose dollar truth is
computed by closed forms independent of the detectors and the plan, priced only on days where every model
used has a row and away from K-dated rate boundaries, so gate tests can prove Token Bill recovers each plant,
keeps the control team clean and never leaks a login or content. Read SPEC §18, §8.8, §21; addendum §4,
§6.1 (date sources), §9.2, §10, §12.1, §18 (plant table), §19.2, Appendix C.

**Owns.** `tokenbill/synth/copilot_world.py` (`generate`, `CopilotWorld`), `tokenbill/synth/copilot_truth.py`
(closed-form truths), `tests/v2/copilot_synth/**`.

**Consumes.** `core.records`, `core.types`, `core.pool` (only to build expected pool months — detector and
plan truths use their own closed forms), `core.money`, `core.ids`, `core.builders` (`CANARY`,
`CANARY_LOGIN`, builders), `core.testing.FakePricer` (every Copilot row), `core.facts`, `common.rng` (seeded).

**Provides.** `generate(seed=7, *, users=146, start="2026-07-03", end="2026-09-22", conventions=("excl",
"incl", "undecidable"), variants=()) -> CopilotWorld` with `records` (cost lines incl. seat / Actions /
sandbox lines, aggregates, coverage aggregates, licenses, activity, config incl. `run_flags`, outcomes, CLI /
OTel / VS Code / gh-aw lanes and events), `truth: CopilotTruth` (int nano, counts, ranges), `today =
"2026-09-23"`, and per-convention report record sets; variants `"volume"` (enterprise billing mode volume) and
`"slack"` (September under the pool), **`"plan_unknown"`** (the world as the no-token handoff sees it: AI usage
report + detailed report without seat SKU lines + activity report (plan `unknown`, assignment unknown) + no
seats API, no org settings; truth carries **both** scenario pool months and savings) and **`"plan_conflict"`**
(seats API says Enterprise for org B while its seat SKU lines say Business; truth: seat lines win) and
**`"plan_quota"`** (`plan_unknown` plus the AI usage report's `total_monthly_quota` column, read by CP-BILL
under `copilot-report-quota`; truth: plan from `report_quota` per Appendix C.P15). Team sizes are this
brief's (146 users), not addendum §18's revision-3 table (127); the truths are closed-form either way.

**Build.**
1. Teams and plants as addendum §18 (platform, infra, ops, payments, mobile, data, ci, core control, tiny) with
   org A `assign_selected`, org B `assign_all`, team-assigned seats in infra, five $0 user budgets in platform,
   one capped cost center with an unknown cap policy, compliance off — with the editor mix of the adopting
   organization (owner answer 3):
   - **vscode (15, enlarged from 5):** VS Code chat-span lanes for 10 users (CP-SYNTH-W writes them as raw
     `agent-traces.db` files; CP-VSCODE's collector extracts them in gate tests) and outfile lanes for 5;
     plants: long-context band requests (some with a known context tier → A/B ranges), forced compactions, model
     switches (`cache.miss-by-cause model-switch`), 30k tool-definition tokens (`static-overhead`); 3 of the
     conversations also exist as `~/.copilot` sessions of the in-VS Code CLI agent (dedupe truth: each request
     counted once);
   - **jetbrains (15, new):** org data only (seats with a JetBrains `last_activity_editor`, users-1-day with
     `totals_by_ide` 90% `intellij`, report rows); no local data; truth: Auto reach 0.1, the model-policy
     recommendation, no lane findings;
   - **agents (4, shrunk from 10):** CLI events (+ store for the experimental gate) and CLI OTel —
     `compaction-cost`, CLI `ci-uncapped`;
   - every other team's `ide:*` mix is 70% VS Code / 30% JetBrains (reach truth per team); 146 users in total
     (addendum §18's 127 + 10 vscode + 15 jetbrains − 6 agents).
2. Pricing guard: every generated inference and report row is dated where its model has a row (Opus 4.8
   before 2026-09-22, Opus 5.5 only on 2026-09-22, Sonnet 5 from the window start) and no model is used within
   ±2 days of one of its K-dated rate changes; GPT-5.6 Sol is not used. A self-check test enforces this from
   `core.facts.copilot_rates()`.
3. Report rows: tokens → gross via FakePricer (Auto ×0.9 for Auto rows), discounts by the pool drawdown in
   time order (licensed rows draw first; direct rows per the variant), net = gross − discount; token columns
   rendered under each convention; the undecidable variant has zero cache tokens.
4. Truth by closed forms per plant (addendum §18 tolerances): seat counts by assignment kind and seat-change
   savings per regime (C.P2–P4, P10–P12 formulas); price-only remap and fast premiums on identical tokens; Auto
   10% × reach; forced-migration Δ; direct-org net; larger-runner range; agentic-workflow per-run p50/p90;
   band premiums (A and A/B ranges); compaction spend; static-overhead range; promo-cliff delta; pool months
   per entity (incl. cap-policy range); for `plan_unknown`: both scenario pool months, both seat-change
   savings (C.P13b formulas) and `plan-status = unknown`; for `plan_conflict`: the winning plan and the conflict
   flag; per-team Auto reach from the `ide:*` mix.
5. Scale mode (`users=5000`, 30 days) streams records for the addendum §17 performance gates.

**Acceptance tests.**
- Determinism: same seed → identical records (`to_json`) across two processes; different seed → different.
- Internal consistency: every report row satisfies gross − discount = net; aggregates equal row sums; coverage
  aggregates equal file-day sums; pool months from truth equal `core.pool.pool_months` on the canonical
  records (the only place `core.pool` is compared); the pricing guard holds.
- The `excl` and `incl` report sets have identical money and differ only in the `input` column; the
  undecidable set has no cache tokens.
- Gate (real detectors / plan / reconciler via CP-SYNTH-W files): each plant recovered within tolerance;
  control team has no finding with invoice or headroom ≥ `min_usd`; tiny team merged everywhere; the
  reconciler decides `excl` and `incl` blind and refuses the undecidable set.
- The `plan_unknown` variant's records contain no seat SKU line, no seats-API license and no `org_settings`
  snapshot, and every license has `plan="unknown"`, `assigned_via_team=None`; its truth has exactly two
  scenario pool months per entity × month.
- Scale mode within the §17 budget and bounded memory (`perf` marker; a 1/10 PR variant).

**Size.** ~2.6k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E49 (Copilot lanes excluded from dual-engine gates).
