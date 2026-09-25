### CLI-LEDGER — CLI entry, command table, demo/analyze compatibility, ledger verbs (wave 3)

**Goal.** Wire the ledger side into the `tokenbill` command line: the static lazy command table for every verb
(both CLI packages), `demo` and `analyze` byte-identical to 0.1.2 on safe inputs with the corrected routing,
run-id namespacing, and the ledger verbs (`init`, `collect claude-code`, `collect claude-code-headless`,
`ingest`, `bill`, `reconcile`, `export`, `showback`, `pricing`, `purge`) (SPEC §15, §15.1, §16, D2, D19, D38).

**Owns.** `tokenbill/cli.py`, `tokenbill/pipeline/ledger.py`, `tokenbill/commands/{demo,analyze,init,collect,
ingest,bill,reconcile,export,showback,pricing,purge}.py`, `tests/v2/cli_ledger/**`.

**Consumes.** WIRING (`config`, `pipeline.common`), every wave-2 package through its documented API (SPEC
§4–§14) and the registry; the frozen v0.1 engine (`trace.read_trace`, `analyzer`, `simulator`, `breakers`,
`report`, `demo_traces`) for `demo` and `analyze --engine v1`; the goldens under `tests/v2/golden/` (read-only).

**Provides.** `cli.main(argv) -> int` with `COMMANDS: dict[str, str]` listing every verb of SPEC §15 (incl.
CLI-SAVINGS's modules, imported lazily); `pipeline.ledger.run_ingest`, `run_bill`, `run_reconcile`,
`run_export`, `run_showback`, `run_collect`, `run_collect_headless`, `run_analyze_v2`, `run_pricing`; command
modules with `add_parser(subparsers)` and `run(args) -> int`.

**Build.** Keep `_model_price`, `_profile_and_render`, `_cmd_demo`, `_cmd_analyze`,
`_tolerant_output_streams` and `main`'s error handling behaviorally identical; add global flags and the
command table. `analyze` per SPEC §15.1 in this order: apply `--model-price` to `PRICING`; strict
`trace.read_trace()` (errors propagate exactly as 0.1.2 — the frozen surrogate test expects exit 1 with
"unpaired surrogate"); namespace run ids that appear in more than one file; route to `run_analyze_v2` only on
moving markers, 1h markers or interleaved models (**not** on unknown models — the frozen test expects
"unavailable"), printing one stderr note; `--engine v1|v2` overrides. `demo` unchanged; `demo --fleet`
dispatches lazily to `pipeline.savings.run_demo_fleet`. `collect claude-code` = CC `collect_incremental` →
TRACE `write_trace_v2` (`usage` profile, header with identity mode and key ids; `--content full` refused;
Windows ACL warning surfaced). `collect claude-code-headless` = CC headless adapter on `--in` files → trace@2
`usage`, CI attribution from `GITHUB_*` env (repo and workflow HMAC'd with the name key). `ingest` =
`pipeline.common.ingest_paths` with allocation rules (OUT's `finops.allocation`), team map and k. `bill`
(exact/estimated/allowance, ESR, naive ratio, arbitrary `--group-by`, `--self` only). `reconcile` (ADMIN
adapters for the page/CUR/GCP files, RECON `reconcile` with the ledger streamed from the store, per-channel
verdicts, `--suggest-contract` rerun, `--live` pull, exit 3 unless `--report-only`). `export` (FOCUS with the
per-channel BilledCost rule, `--channel`, `--role`, `--chargeback`; trace2; ccusage). `showback`, `pricing
show/verify/diff/emit-model-pricing`, `purge` (audit rows). Exit codes 0/1/2/3/4; `--log-json`; `--plugins`;
`--deterministic`; offline unless `--live`.

**Acceptance tests.**
- goldens (`tests/v2/golden/`, captured by F-CORE): `demo` variants and `analyze` on the four demo traces are
  byte-identical after placeholder normalization; the 207 existing tests (incl. the frozen
  `tests/test_cli.py`) green;
- routing: moving `cache_control` markers, a `ttl:"1h"` marker and > 1 model change per run each switch to v2
  with exactly one stderr note; an unknown model alone stays on v1 ("unavailable"); a surrogate model string
  exits 1 with the 0.1.2 message; `--engine v1` forces legacy; two files sharing `run_id` total $22, not $40,
  in the v1 path;
- `collect claude-code` writes a trace@2 `usage` file: CANARY absent, principals `r_`/`c_` only, `--content full`
  → exit 2; re-running collects only new data; `collect claude-code-headless` on CC's execution-file fixture
  with `GITHUB_ACTIONS=true` → CI workload, repo `h_`;
- `ingest` auto-sniffs every adapter fixture (incl. CUR and GCP); `bill --group-by principal` without `--self`
  → exit 2; `bill` shows allowance apart from the exact bill; `reconcile` on a mismatched fixture → exit 3 and
  `--report-only` → exit 0; `reconcile --suggest-contract` rerun reconciled; `export focus` refuses an
  unreconciled channel (exit 3) and succeeds with `--channel` restricted to reconciled channels; `--live`
  without `--admin-key-env` → exit 2; `pricing verify` offline clean;
- config precedence end to end; `--strict-dq` → exit 4 on warnings; `--plugins` required for entry points;
  JSON outputs byte-identical across two processes with `--deterministic`; the whole ledger CLI runs under the
  socket guard.

**Size.** ~2.7k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-10** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
`core.pool`, Copilot catalog tables) are merged into `v0.2` before you start — read SPEC Appendix E.3 and
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/briefs/CORE-AMENDMENTS.md` (§2–§5) for the exact contracts, and the Copilot addendum
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/SPEC-v0.2-COPILOT.md` only as background. Do not implement Copilot adapters/detectors yourself
(the CP-* packages do); implement only your amendment item so the extension points work.

#### 5. Amendments to SPEC packages (A-; paste into briefs not yet started, late change requests otherwise)

| id | package | status @99ba098 | amendment | LOC |
|---|---|---|---|---|
| A-1 | RATES | not started | addendum §21.4 row, reading Copilot rows through `core.facts.copilot_rates()` / `core.extensions.extension_rate_files()` (missing → dq); predicate keys `routing`, `compliance_in`; band hypotheses; both Copilot paths → LIST_EQUIVALENT into `PricedTotal.pool` (`allowance` = subscription lines only); `pricing verify` runs `core.extensions.rate_verifiers()` | +120 |
| A-2 | STORE | not started | addendum §7.1 DDL and §21.4 row **plus** `SqliteStore(…, adopt_key_ids: bool = False)` (R-E21 replaces the §7.1 "Key-id adoption" paragraph: with or without its own org key the store adopts the principal and name key ids of the first `copilot-export` bundle only, one adopted id, `meta.adopted_key_id` / `adopted_name_key_id` / `org_key_mode` + an `audit` row, a second bundle key id → `UsageError`), `count_users(source="cost_lines")`, `source_stats` (implements `core.protocols.LedgerStats`), `purge(principal=…)` also for adopted-key rows, `ReconciliationReport` untouched (not stored) | +190, total 2,990 |
| A-3 | TRACE | not started | addendum §21.4 row; closed key sets derived with `core.records.record_fields` (C-4) and `core.records.RAW_USAGE_ENUMS` / `RAW_USAGE_NUMERIC`, replacing the hand lists (net-neutral, O-5) | ≤ 0 net |
| A-4 | TELEM | **started** | late change request (R-E23): `otlp` skips resources for which `core.models.is_copilot_resource` is true and sets `stats["defer:copilot-otel"]`; rebase onto the gate-F' core first | +40 |
| A-5 | RECON | not started | addendum §21.4 row; `merge_reports` also unions `ReconciliationReport.decisions` (conflicting values → `ContractViolation`) | +90 |
| A-6 | REPLAY | **started** | late change request (R-E23): accept billing class `pool` like `allowance` (LIST_EQUIVALENT; mixed classes still raise) | +10 |
| A-7 | PLAN | not started | addendum §21.4 row; skip `replay == "aggregate"` levers; lever lookup via `core.catalog.lever` (searches both tables) | +40 |
| A-8 | OUT | not started | addendum §21.4 row (`render_sections`, `write_focus(…, extra_rows, owned_channels)`, `money_json` → `figure_json`, BILL line for `PricedTotal.pool`); `outputs/ccusage.py` moves to CLI-LEDGER (O-5) | +40 net, total 2,890 |
| A-9 | WIRING | not started | addendum §21.4 row plus `open_store(…, adopt_key_ids=False)` passed to `SqliteStore`; `ingest_paths` persists records **after** the ledger ingest (so an adopted key id exists before `persist`) and re-reads deferred files | +90 |
| A-10 | CLI-LEDGER (wave 3) | not started | addendum §21.4 row; `reconcile` prints the merged decisions (`recon_decisions_of(reports)`) with its verdicts; nothing is persisted (C-17) | +45 |
| A-11 | CLI-SAVINGS (wave 3) | not started | addendum §21.4 row; because decisions are not persisted, `findings` / `report` (and `scan` on Copilot data) first run `core.extensions.run_reconcilers` + RECON's reconcile over the same window, then call `enrich(…, reconciled_channels=…, recon_decisions=recon_decisions_of(reports))` (without reconcilers, e.g. on a store without Copilot data, `recon_decisions=()`: `excl` convention and unclassified discounts, both labelled) | +65, total 2,965 |
| A-12 | INTEGRATION (wave 4) | not started | `docs/COPILOT.md` (fleet recipe: managed OTel incl. the JetBrains caveat, **VS Code `agent-traces.db` opt-in + daily `copilot collect --source vscode`**, CI post-step, gh-aw artifacts, auth table, privacy); **`docs/COPILOT-ADMIN.md` generated verbatim from `tokenbill/copilot/handoff_data/admin_guide.md`** with `tests/v2/e2e/test_copilot_admin_doc.py` asserting byte equality; flagship §18 incl. the handoff and plan-unknown paths; Copilot release gates (below); weekly YAML check | docs/tests |
| — | CC, ADMIN, BLOCK, VERIFY, DETECT-CACHE, DETECT-OTHER, SYNTH-ORACLE, SYNTH-FLEET | mixed | none | 0 |

