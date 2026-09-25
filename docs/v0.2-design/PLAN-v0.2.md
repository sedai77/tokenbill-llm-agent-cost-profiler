# Token Bill v0.2 — Build Plan (revision 2)

Companion to `SPEC-v0.2.md` (the authoritative contract). A builder sees **only** its brief below, the
SPEC and the repository, so every brief is self-contained and points to SPEC sections for algorithms.
SPEC §21 (build protocol) binds every package. Revision 2 splits the foundation, the detectors, the
synthetic data and the CLI into packages of ≤ 3k LOC, moves every shared catalog into `tokenbill/core/`,
adds the gate tests the first review found missing, and names a contract owner (SPEC D20, D25, D29, D42).

---

## 1. Rules of engagement

### 1.1 Waves and merge gates

```
wave 0   F-CORE  (contracts, scaffolding, v0.1.2 goldens, facts task) ─── one agent
            │  merge gate 0: core importable, goldens captured, ownership CI live, 207 tests green
wave 1   F-SEM  ∥  F-KIT   (shared semantics  ∥  fakes, conformance, catalogs, keys, k-anonymity)
            │  merge gate F: tokenbill/core/* frozen; smoke-on-fakes green; contract owner = F-KIT agent
wave 2   RATES CC TRACE TELEM ADMIN RECON STORE REPLAY BLOCK SYNTH-ORACLE SYNTH-FLEET VERIFY
         DETECT-CACHE DETECT-OTHER PLAN OUT WIRING          (17 packages in parallel)
            │  daily canary merges run every gate test; merge gate 1
wave 3   CLI-LEDGER  ∥  CLI-SAVINGS
            │  merge gate 2
wave 4   INTEGRATION (e2e, supply chain, docs, version) → adversarial review → 0.2.0
```

Critical path: F-CORE → max(F-SEM, F-KIT) → slowest wave-2 package → slowest CLI package →
INTEGRATION. No package depends on another package of its own wave: wave-2 packages use only `core/*` and
test against the foundation fakes; cross-package checks are **gate tests** (`@pytest.mark.gate` +
`pytest.importorskip`) that run at the daily canary merge and must pass at merge gate 1. F-SEM and F-KIT
depend only on F-CORE (F-KIT's conformance helpers never import F-SEM; F-KIT gate tests that need F-SEM
modules run at merge gate F).

### 1.2 Disjoint ownership

- Every file of the repository is owned by exactly one package (table §2.1, mirrored in SPEC Appendix O), by
  INTEGRATION, or is FROZEN. `OWNERSHIP.toml` (created by F-CORE from Appendix O) and `scripts/check_ownership.py` enforce it in CI
  (`.github/workflows/ownership.yml`, created by F-CORE, active from wave 0).
- FROZEN (never edited): `tokenbill/__main__.py common.py trace.py analyzer.py simulator.py breakers.py
  report.py demo_traces.py py.typed`, `examples/**`, `CODE_OF_CONDUCT.md`, `LICENSE`, and every existing
  `tests/test_*.py` except `tests/test_pricing.py` (RATES).
- Existing files that change: `tokenbill/pricing.py` and `tests/test_pricing.py` (RATES),
  `tokenbill/instrument.py` (TRACE), `tokenbill/cli.py` (CLI-LEDGER), `tokenbill/__init__.py` (INTEGRATION),
  `pyproject.toml`, `uv.lock`, `.gitignore` (F-CORE), `README.md DESIGN.md CHANGELOG.md CONTRIBUTING.md
  SECURITY.md Makefile docs/** .github/**` (INTEGRATION, except `.github/workflows/ownership.yml`: F-CORE).
- Shared subpackage `__init__.py` files are docstring-only and owned by F-CORE; implementations register
  through the string maps already in `core/registry.py` (SPEC §3.7) and the static command table in
  `cli.py` (SPEC §15).
- Reading another package's files (e.g. the goldens under `tests/v2/golden/`, fixtures in gate tests) is
  allowed; writing them is not.

### 1.3 Definition of done (every package)

SPEC §21 #5: typed and documented public API; ruff clean; ≥ 90% line coverage of owned modules
(`coverage run -m pytest tests/v2/<area>`); hypothesis fuzz tests for owned parsers; deterministic outputs;
the brief's acceptance tests; no floats in money modules; canary absent from every output;
`tests/v2/<area>/README.md` listing fixtures and unverified facts; the 207 existing tests green;
`scripts/check_ownership.py --package <ID>` clean.

### 1.4 Contract changes, contract owner, canary merges

- `tokenbill/core/*` is frozen after merge gate F. A builder who finds a contract gap writes
  `tests/v2/<area>/CONTRACT-CHANGE-<ID>.md` and implements against the current contract; field or
  signature changes are applied by the contract owner **between waves only**; affected packages adapt at
  the next merge gate.
- **Contract owner / arbiter:** the F-KIT agent stays available through wave 3. It may hot-fix *bugs* (not
  fields or signatures) in `core/*` implementations and fakes mid-wave on a `core-hotfix/<n>` branch that
  every active worktree merges; each hotfix is time-boxed to 2 hours and announced. It arbitrates gate-test
  failures whose cause is disputed (e.g. SYNTH-ORACLE vs REPLAY differential mismatches: the SPEC text
  decides; if the SPEC is ambiguous, the arbiter rules and records the ruling in `tests/v2/kit/RULINGS.md`).
- **Daily canary merge:** the orchestrator merges every finished (or checkpointed) wave-2 branch into
  `v0.2-canary` and runs all `gate` tests there, routing failures to the owning builder and core issues to
  the contract owner. Builders may merge the canary into a scratch branch to run their own gates locally
  (never committed).

### 1.5 Merge gates

**Gate 0 (after F-CORE):** `import tokenbill.core.{records,money,labels,types,protocols,registry,…}` works;
goldens captured from the untouched tree; ownership CI green; 207 tests green on 3.10 and 3.13.

**Gate F (after F-SEM ∥ F-KIT):** `tests/v2/kit/test_smoke_fakes.py` (SPEC §3.18) green; F-KIT gate tests
that need F-SEM (catalog grids parse with `core.policy.parse_policy`; `assert_detector_conforms` on a
test detector using `core.findings`) green; `core/*` frozen.

**Gate 1 (after wave 2)** — all branches merged; ownership clean per package; `pytest -m "not perf"` green
including every gate test:

| gate test (owner) | proves |
|---|---|
| `tests/v2/gates/test_gate1_smoke.py` (F-KIT) | CC fixture tree → `SqliteStore` priced with `RateCard` → reconcile against ADMIN fixtures → calibrate → `run_detectors` → `build_action_plan`, all real modules, no pipeline code needed |
| `tests/v2/trace/test_gate_collector_roundtrip.py` (TRACE) | CC `collect_incremental` → `write_trace_v2(profile usage)` → `TraceV2Adapter.read` → records equal (`to_json`) → `MemoryStore` ingest equals direct ingest |
| `tests/v2/trace/test_gate_ratecard.py` (TRACE) | demo trace@1 exact bill with the real `RateCard` equals v0.1 as-billed within 1 nano per call |
| `tests/v2/blocksim/test_gate_dual_engine.py` (BLOCK) | dual-engine agreement through TRACE's `TraceV1Adapter` + fingerprints (SPEC §10.4) |
| `tests/v2/synth_oracle/test_differential_replay.py` (SYNTH-ORACLE) | `UsageReplayer` == `ReferenceReplay` to the nano on ≥ 500 random lanes per policy family |
| `tests/v2/synth_fleet/test_gate_files_through_adapters.py` (SYNTH-FLEET) | every written source file (transcripts, execution files, OTLP, trace@2, admin pages, CUR) read by the real adapters reproduces the canonical records' token totals per team; truth vs oracle per plant |
| `tests/v2/store/test_gate_adapters.py` (STORE) | CC/TELEM/TRACE/ADMIN fixture files through the real adapters ingest and merge correctly, priced with `RateCard` |
| `tests/v2/recon/test_gate_ratecard_admin.py` (RECON) | ADMIN's recorded fixtures parsed by the real adapters reconcile with the real `RateCard` |
| `tests/v2/detect_cache/test_gate_*.py` (DETECT-CACHE) | real `UsageReplayer`: A.1 → `ttl-1h-recommended` $1.1508, A.2b → `ttl-5m-recommended` $0.318 (min_usd 0.10), A.2 → none; SYNTH-FLEET plants payments/platform/mobile/agents recovered; control team clean |
| `tests/v2/detect_other/test_gate_*.py` (DETECT-OTHER) | SYNTH-FLEET plants search/infra/data/ops/ci-bots/agents recovered with the real replayer; control team clean |
| `tests/v2/plan/test_gate_*.py` (PLAN) | real replayer: A.1 TTL lever Shapley == standalone; payments TTL lever Shapley within ±5% of truth; sharded == unsharded |
| `tests/v2/verify/test_gate_*.py` (VERIFY) | stepped-wedge panels from `synth.lanes_gen.rollout_panel`; `build_panel` on a real `SqliteStore` |
| `tests/v2/outputs/test_gate_*.py` (OUT) | FOCUS totals equal `SqliteStore.cost_rows` sums; showback from real `publish` |
| `tests/v2/wiring/test_gate_*.py` (WIRING) | `build_env` with the real `RateCard`; `open_store` + `map_shards(jobs=2)` on a real `SqliteStore` equals `jobs=1` |

**Gate 2 (after wave 3):** both CLI packages merged; golden demo/analyze byte-identical; every command's exit
codes; `tokenbill --version`; the full suite green on Python 3.10 and 3.13; the frozen `tests/test_cli.py`
green.

### 1.6 Release gates (checked by INTEGRATION before 0.2.0 GA)

1. At least one real, redacted Anthropic usage_report + cost_report page pair from the adopting
   organization is added as an ADMIN/RECON fixture (SPEC §19.8 #6); until then `reconcile` prints
   `dq.recon_schema_unverified`.
2. The opt-in local-corpus test (`tests/v2/e2e/test_local_corpus.py`, marker `local_corpus`, skipped unless
   `TOKENBILL_LOCAL_CORPUS=1`) runs `scan` over the machine's real `~/.claude/projects` and matches, to the
   cent, an independent ~80-line reference computed inside the test (de-duplicate by `message.id` keeping max
   output, skip `<synthetic>`, price at `core/facts.json` rates).
3. `pricing verify` (offline) clean; every VERIFY item of SPEC §19.8 either verified (facts.json
   `verification: "primary"`) or shipped disabled/commented and listed in the release notes.

---

## 2. Package table

| id | wave | title | depends_on | LOC (code + tests) |
|---|---|---|---|---|
| F-CORE | 0 | Foundation core: contracts, scaffolding, goldens, facts | — | 3,400 |
| F-SEM | 1 | Foundation semantics: conventions, cache rules, transitions, policy grammar, shards, Shapley, detector helpers | F-CORE | 2,900 |
| F-KIT | 1 | Foundation kit: fakes, conformance suites, catalogs, keys, k-anonymity, gate-1 smoke | F-CORE | 2,900 |
| RATES | 2 | Pricing registry, billing rules, v0.1 pricing compat | F-CORE, F-SEM, F-KIT | 2,600 |
| CC | 2 | Claude Code transcript importer, collector, headless/CI/SDK streams | F-CORE, F-SEM, F-KIT | 3,000 |
| TRACE | 2 | trace@1/trace@2 adapters, fingerprints, Recorder v2 | F-CORE, F-SEM, F-KIT | 3,000 |
| TELEM | 2 | OTLP/JSON, OpenAI (incl. cache diagnostics), Bedrock, Anthropic-response adapters; conventions | F-CORE, F-SEM, F-KIT | 2,600 |
| ADMIN | 2 | Admin/Analytics page adapters and cloud billing exports (CUR 2.0, GCP) | F-CORE, F-SEM, F-KIT | 2,500 |
| RECON | 2 | Reconciliation ledger gate, residuals, cost maps, live pull, org scan | F-CORE, F-SEM, F-KIT | 2,700 |
| STORE | 2 | SQLite store, merge, rollups, retention, pseudonymization | F-CORE, F-SEM, F-KIT | 2,800 |
| REPLAY | 2 | Usage-level replay engine and the model gate | F-CORE, F-SEM, F-KIT | 2,800 |
| BLOCK | 2 | Block-level replay and cache-breaker suite | F-CORE, F-SEM, F-KIT | 2,600 |
| SYNTH-ORACLE | 2 | Reference replay oracle, synthetic lanes, rollout panels | F-CORE, F-SEM, F-KIT | 2,300 |
| SYNTH-FLEET | 2 | Synthetic fleet with planted waste, closed-form truth, schema-true source writers | F-CORE, F-SEM, F-KIT | 2,900 |
| VERIFY | 2 | Measurement (DiD, CUPED, ITS), lab A/B, guards, signed receipts | F-CORE, F-SEM, F-KIT | 2,900 |
| DETECT-CACHE | 2 | Cache detectors (miss by cause, switch churn, rebuild, cold resume, TTL advisor, gateway, unread write, fan-out) | F-CORE, F-SEM, F-KIT | 2,800 |
| DETECT-OTHER | 2 | Context, premium, model routing, failure, automation, tail detectors | F-CORE, F-SEM, F-KIT | 3,000 |
| PLAN | 2 | Action plan, realization intervals, policy packs, hook, effectiveness | F-CORE, F-SEM, F-KIT | 2,500 |
| OUT | 2 | Renderers, FOCUS, SARIF, ccusage, showback, allocation, workload | F-CORE, F-SEM, F-KIT | 3,000 |
| WIRING | 2 | Config and shared pipeline plumbing (env, store opening, ingestion loop, shard mapping) | F-CORE, F-SEM, F-KIT | 1,300 |
| CLI-LEDGER | 3 | CLI entry, command table, demo/analyze compat, ledger verbs | all wave 2 | 2,700 |
| CLI-SAVINGS | 3 | Savings, verification and CI-gate verbs; scan / scan --org; demo --fleet | all wave 2 | 2,900 |
| INTEGRATION | 4 | integration step (not a work package): e2e, supply chain, docs, version, release gates | all | ~1,800 + docs |

Total ≈ 60k LOC including tests.

### 2.1 File ownership (mirrored into `OWNERSHIP.toml`)

| package | owns |
|---|---|
| F-CORE | `OWNERSHIP.toml`, `scripts/check_ownership.py`, `scripts/capture_goldens.py`, `pyproject.toml`, `uv.lock`, `.gitignore`, `.github/workflows/ownership.yml`, `tokenbill/core/{__init__,errors,records,money,labels,types,protocols,registry,ids,jsonl,textsafe,secrets,models,lanes,evidence,facts,builders}.py`, `tokenbill/core/facts.json`, `tokenbill/{adapters,rates,recon,store,sim,detect,plan,verify,outputs,finops,synth,pipeline,commands}/__init__.py`, `tests/conftest.py`, `tests/v2/__init__.py`, `tests/v2/core/**`, `tests/v2/golden/**` |
| F-SEM | `tokenbill/core/{conventions,cache_rules,transitions,shapley,policy,shards,findings}.py`, `tests/v2/sem/**` |
| F-KIT | `tokenbill/core/{testing,catalog,keys,kanon}.py`, `tests/v2/kit/**`, `tests/v2/gates/**` |
| RATES | `tokenbill/pricing.py`, `tokenbill/rates/{schema,engine,contract,billing_rules,verify}.py`, `tokenbill/rates/data/**`, `tests/test_pricing.py`, `tests/v2/rates/**`, `tests/v2/fixtures/rates/**` |
| CC | `tokenbill/adapters/{claude_code,cc_collect,cc_headless}.py`, `tests/v2/claude_code/**`, `tests/v2/fixtures/claude_code/**` |
| TRACE | `tokenbill/adapters/{fingerprint,trace_v1,trace_v2}.py`, `tokenbill/instrument.py`, `tests/v2/trace/**`, `tests/v2/fixtures/trace/**` |
| TELEM | `tokenbill/adapters/{conventions_ext,otel,openai,bedrock,anthropic_responses}.py`, `tests/v2/telemetry/**`, `tests/v2/fixtures/telemetry/**` |
| ADMIN | `tokenbill/adapters/{anthropic_admin,openai_admin,cloud_billing}.py`, `tests/v2/admin/**`, `tests/v2/fixtures/admin/**` |
| RECON | `tokenbill/recon/{reconcile,residuals,costmap,pull,orgscan}.py`, `tests/v2/recon/**`, `tests/v2/fixtures/recon/**` |
| STORE | `tokenbill/store/{schema,db,merge,rollups,retention,pseudonym}.py`, `tests/v2/store/**` |
| REPLAY | `tokenbill/sim/{usage_replay,calibrate}.py`, `tests/v2/sim/**` |
| BLOCK | `tokenbill/sim/block_replay.py`, `tokenbill/detect/block.py`, `tests/v2/blocksim/**`, `tests/v2/fixtures/blocksim/**` |
| SYNTH-ORACLE | `tokenbill/synth/{oracle,lanes_gen}.py`, `tests/v2/synth_oracle/**` |
| SYNTH-FLEET | `tokenbill/synth/{fleet,truth,writers}.py`, `tests/v2/synth_fleet/**` |
| VERIFY | `tokenbill/verify/{stats,estimators,its,ab,rollout,label_policy,receipts,panel}.py`, `tests/v2/verify/**` |
| DETECT-CACHE | `tokenbill/detect/{cache_miss,cache_ttl,cache_structure}.py`, `tests/v2/detect_cache/**` |
| DETECT-OTHER | `tokenbill/detect/{context,premium,model,failure,automation,tail}.py`, `tests/v2/detect_other/**` |
| PLAN | `tokenbill/plan/{action_plan,realization,policy_pack,litellm,effectiveness}.py`, `tokenbill/plan/templates/**`, `tests/v2/plan/**` |
| OUT | `tokenbill/outputs/{result_json,terminal,html,focus,sarif,ccusage,showback}.py`, `tokenbill/finops/{allocation,workload}.py`, `tests/v2/outputs/**`, `tests/v2/finops/**` |
| WIRING | `tokenbill/config.py`, `tokenbill/pipeline/common.py`, `tests/v2/wiring/**` |
| CLI-LEDGER | `tokenbill/cli.py`, `tokenbill/pipeline/ledger.py`, `tokenbill/commands/{demo,analyze,init,collect,ingest,bill,reconcile,export,showback,pricing,purge}.py`, `tests/v2/cli_ledger/**` |
| CLI-SAVINGS | `tokenbill/gate.py`, `tokenbill/pipeline/{savings,verification}.py`, `tokenbill/commands/{scan,me,calibrate,findings,whatif,policy,measure,ab,receipt,check,report}.py`, `tests/v2/cli_savings/**` |
| INTEGRATION | `tokenbill/__init__.py`, `README.md`, `DESIGN.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `Makefile`, `docs/**`, `.github/**` except `.github/workflows/ownership.yml`, `scripts/{sbom.py,check_zero_deps.py,repro_check.sh,perf_gates.py}`, `tests/v2/e2e/**` |
| FROZEN | `tokenbill/{__main__,common,trace,analyzer,simulator,breakers,report,demo_traces}.py`, `tokenbill/py.typed`, `examples/**`, `CODE_OF_CONDUCT.md`, `LICENSE`, `tests/test_{analyzer,breakers,cli,demo_recovers_planted_waste,demo_traces,examples,instrument,performance,report,simulator,trace}.py` |

---

## 3. Work-package briefs

Every brief assumes SPEC-v0.2.md. "SPEC §x" references are binding. All packages: Python ≥ 3.10, stdlib
only, no network, follow SPEC §21. "Gate" tests are marked `@pytest.mark.gate`, guarded with
`pytest.importorskip`, and must pass at the merge gate named in SPEC Appendix G (mirrors PLAN §1.5).

### F-CORE — Foundation core: contracts, scaffolding, goldens, facts (wave 0)

**Goal.** Build the frozen declarations every other package imports — `tokenbill/core/*` modules of SPEC
§3.1–§3.10, §3.12, §3.13, §3.17, §3.24, §3.25 exactly as written — plus the package skeleton, the test guards,
the ownership tooling, the v0.1.2 golden outputs and the single transcription of verified facts
(`core/facts.json`). Nineteen packages then build against what you ship, so exact fidelity to the SPEC
signatures, field names and enum values matters more than anything else. About half of this package is
transcription of SPEC code blocks. Read SPEC §0–§3, §4.2, §5.1–§5.2, §7.3, §8.9, §16.2, §19, §21.

**Owns.** `OWNERSHIP.toml`, `scripts/check_ownership.py`, `scripts/capture_goldens.py`, `pyproject.toml`,
`uv.lock`, `.gitignore`, `.github/workflows/ownership.yml`, `tokenbill/core/{__init__,errors,records,money,
labels,types,protocols,registry,ids,jsonl,textsafe,secrets,models,lanes,evidence,facts,builders}.py`,
`tokenbill/core/facts.json`, docstring-only `tokenbill/{adapters,rates,recon,store,sim,detect,plan,verify,
outputs,finops,synth,pipeline,commands}/__init__.py`, `tests/conftest.py`, `tests/v2/__init__.py`,
`tests/v2/core/**`, `tests/v2/golden/**`.

**Build.**
1. **Goldens first.** `scripts/capture_goldens.py` runs the untouched v0.1.2 CLI in-process — `demo`, `demo -o
   report.html`, each `--scenario`, `--seed 7` and `--seed 11`, and `analyze` on the four demo scenarios
   written with the frozen `trace.write_trace` (stdout and HTML) — and stores outputs with the version string
   and report date replaced by placeholders under `tests/v2/golden/`. Run it before changing anything else.
2. `errors.py`; `records.py` (every record of SPEC §3.2 with `__post_init__` validation, `TBEnum` whose
   `__str__`/`__format__` return `.value`, `billing_class()`, `EXTRA_KEYS`, lossless `to_json`/`from_json`);
   `money.py` (§3.3, incl. `cents_to_nano`/`usd_str_to_nano` returning `(nano, remainder)`); `labels.py`
   (§3.4: `Figure`, `Basis.LIST_EQUIVALENT`, rules, `add`, `sub`, `scale`, `exact`, `estimated`,
   `unpriced`, `zero`); `types.py` (all of §3.5 incl. `IngestOptions` with name/principal keys, team map,
   k, renormalize; `PricedInference.exact_nano/estimated`; `PricedTotal.allowance`; `ReplayResult`
   baseline/cost/saving; `CalibrationPartial`; `ShardKey`, `LaneIndexRow`; `PanelRow`, `MeasurePlan`,
   `AbResult`, `ReceiptRow`, `ChannelVerdict`, `PricingReport`, `Discrepancy`; `RunResult` with every slot;
   the module-private `_PUBLISH_TOKEN`; `Policy` whose `observed/combine/spec/is_observed` delegate lazily to
   `tokenbill.core.policy` — F-SEM); `protocols.py` (§3.6 incl. the extended `LedgerStore`).
3. `registry.py`: the exact string maps of §3.7; `load`, `get_adapter`, `sniff_adapter` (≤ 64 KiB head,
   decompressing `.gz`, and `.zst` only when `compression.zstd` exists), `all_detectors`, `run_detectors`
   (with `emit_missing`), `load_plugins(enabled)`.
4. `ids.py`, `jsonl.py` (incl. `open_text` with zstd guard and `open_private` with the Windows `icacls`
   best-effort path behind an injectable runner), `textsafe.py`, `secrets.py` (§3.8–§3.10); `models.py`
   (§3.12, Vertex ids → scope `unknown`); `lanes.py` (§3.13).
5. **Facts task** (`core/facts.json` + `core/facts.py` loader, §3.24): transcribe from SPEC §19 the rate rows
   and modifiers FakePricer needs (claude-opus-5-5, claude-opus-5, claude-opus-4-8, claude-fable-5,
   claude-fable-5-1 [from 2026-09-01], claude-mythos-5-1, claude-sonnet-5, claude-sonnet-4-6, claude-haiku-4-5
   on `anthropic_api`; claude-opus-5 on `bedrock` global and regional; gpt-5.6-sol [disabled launch row +
   promotional row to 2026-11-22]; batch, US geo, fast, Bedrock regional modifiers), every SPEC §11.4
   settings key with `verified`/`min_version`, the §3.17 evidence constants, lifecycle (same-tier successors
   only; retirements; the gpt-5.6-sol promotion; announced models), FOCUS 1.4 columns, the §19.4 headless
   field names, and the CUR usage-type / GCP SKU rules (`sku_rules`, each `verified: false` unless checked). If your environment has network access, re-verify each against its primary URL (priority:
   §19.8 #12 TTL value formats, #18 default model/effort keys, #11 FOCUS columns, #5 `modelPricing`) and set
   `verification: "primary"`; otherwise keep `"research"` and `verified_on: 2026-09-23`. `evidence.py` exposes
   typed constants read from facts.json.
6. `builders.py` (§3.25): CANARY helpers, builders, `FlatRates` (scale 8; subscription → LIST_EQUIVALENT).
7. Guards: `tests/conftest.py` (autouse socket guard that allows loopback, `AF_UNIX` and `socketpair`;
   markers `perf`, `slow`, `gate`, `needs_ssh_keygen`, `local_corpus`); `tests/v2/__init__.py`;
   `tests/v2/core/test_no_float_money.py` (AST scan of the §2.4 money paths, skipping paths that do not exist
   yet); `OWNERSHIP.toml` mirroring SPEC Appendix O (= PLAN §2.1); `scripts/check_ownership.py --package ID --base REF` (lists files
   changed vs base via `git diff --name-only`, exits 1 with violations; FROZEN files always violate);
   `.github/workflows/ownership.yml` (runs the check on pull requests; SHA-pinned actions, `permissions: {}`).
8. `pyproject.toml`: dev extra adds `hypothesis` and `coverage`; pytest `addopts = "-ra -m 'not perf'"`
   (default prepend import mode); markers registered; package data (`core/facts.json`,
   `rates/data/**`, `plan/templates/**`) included in the wheel. Relock `uv.lock`. `.gitignore` adds
   `.hypothesis/`, `.coverage*`, `.tokenbill/`.

**Consumes.** Frozen v0.1 modules only (you may import `tokenbill.common`, `tokenbill.trace`,
`tokenbill.breakers.VOLATILE_PATTERNS`, `tokenbill.demo_traces`, `tokenbill.cli` for goldens; never edit them).

**Acceptance tests** (`tests/v2/core/`):
- the 207 existing tests pass with the new `conftest.py` (including the four asyncio tests of
  `tests/test_instrument.py`; the guard allows loopback socketpairs) and a non-loopback connect is refused;
- every record round-trips `to_json`/`from_json` (hypothesis); `UsageBuckets` rejects negatives, values
  > 2**53, reasoning > output, a non-zero `cache_write_other` without TTL; `__add__` rules;
  `f"{LaneKind.MAIN}" == "main"` and `str(Basis.LIST) == "list"` (the 3.12 enum-format trap);
- money: `token_nano(1_000_000, usd("4.00"), Decimal("0.05")) == 200_000_000`; `token_nano(1, usd("0.25")) ==
  250`; `token_nano(3, usd("25"), Decimal("0.8537")) == 64_028`; `from_cents("12345.678") ==
  Decimal("123.45678")`; `cents_to_nano("0.00000000001") == (0, Decimal("1E-13"))`; `usd(0.1)` raises
  `TypeError`; an inexact `EXACT_CTX` operation raises;
- `Figure`: every construction rule raises as specified; `add` takes the weakest evidence, raises on basis
  mismatch (incl. LIST vs LIST_EQUIVALENT), adds ranges, propagates None; `is_billed_eligible` is False for
  LIST_EQUIVALENT and PROVIDER_ESTIMATE; `sub` subtracts ranges crosswise;
- `PublishedAggregate` constructed without the token raises;
- registry: lazy import (a missing module raises only when requested); `run_detectors` emits exactly one DQ
  finding for unmet `requires` (and none with `emit_missing=False`); plugins not loaded unless enabled;
- models: every id of SPEC §6.9 case 16, `<synthetic>`, config aliases;
- ids / jsonl / secrets / textsafe unit tests (0600 mode on POSIX, gzip, zstd error message on < 3.14,
  oversize line, `head_sha`, every secret type, ANSI/C0/C1 stripping, the Windows ACL path via a fake runner);
- `FlatRates.unit_rates(...).scale_exp == 8`; facts.json loads, every entry has `source`, `finding`,
  `verified_on`, `verification`;
- ownership script flags an out-of-package file and a FROZEN file;
- goldens exist and are non-empty.

**Size.** ~3.4k LOC including tests (the one package allowed above 3k; mostly SPEC transcription).

### F-SEM — Foundation semantics (wave 1)

**Goal.** The shared semantics that several wave-2 packages must compute identically: Anthropic usage
normalization with the iterations/refusal rules, the cache-rule table with the conditional effort exemption,
the single transition/miss/cause definition, Shapley, the policy grammar and selectors, the shard contract,
and the detector helpers (SPEC §3.11, §3.14–§3.16, §3.19, §3.21, §3.22, D28–D30). Five packages depend on
`transitions.py`; implement it precisely.

**Owns.** `tokenbill/core/{conventions,cache_rules,transitions,shapley,policy,shards,findings}.py`,
`tests/v2/sem/**`.

**Consumes (F-CORE).** `core.records`, `core.types`, `core.labels`, `core.money`, `core.ids`, `core.lanes`,
`core.models`, `core.evidence`, `core.protocols`, `core.builders` (`FlatRates`, `make_*`, `lane_from_table`).
Do not import `core.testing` (F-KIT, same wave).

**Provides.** `core.conventions` (`Convention`, `register_convention`, `get_convention`, `normalize`,
`sum_check`, `anthropic_inferences`; registers `anthropic.messages` and a disabled `codex.rollout`);
`core.cache_rules` (`CacheRules`, `RulesTable`, `effort_change_keeps_cache`, constants); `core.transitions`
(`classify_transitions`, `static_prefix_floor`, `lane_first_reads_of`, constants); `core.shapley`
(`shapley_exact`, `shapley_mc`, `scale_credits`); `core.policy` (`to_spec`, `parse_policy`, `combine`,
`lane_matches`, `request_matches`, `selector_terms`); `core.shards` (`plan_shards`, `shard_where`,
`shard_of_lanes`, `merge_replay`, `merge_findings`, `stratified_sample`, `SHARD_MAX_REQUESTS`);
`core.findings` (`cohort_key`, `finding_id`, `make_scope`, `build_finding`, `miss_waste`, `min_usd_nano`,
`threshold`, `fit_cpt`, `top_evidence`, `sum_figures`, `rate_nano`).

**Build.** Exactly the SPEC sections above. Conventions: the §3.11 mapping, residual rules, iterations
invariant checks and the refusal table (0 → not billable; 1–16 → `billable=None`; > 16 → billable). Cache
rules: rows for anthropic_api, claude_platform_aws, foundry, bedrock, vertex (workspace/organization scope),
openai_api (organization), azure_openai (subscription), each with sources; `effort_change_keeps_cache` exactly
per D28 with numeric dotted version comparison. Transitions: precedence incl. `plan-toggle`, the effort
exemption, canonical diagnostic reasons (`param_changed`, `key_changed`, `compacted`), requests without a
serving inference skipped. Policy grammar: canonical ordering, repeatable `model=` and `effort=` clauses with
selectors, `model:<id>` selector term. Shards: team shards split by lane kind above the cap; merges add figures
and counts deterministically.

**Acceptance tests** (`tests/v2/sem/`):
- conventions: 5m/1h split, unknown residual with note, negative residual note; iterations: len-1 sum == top,
  fallback last == top, mismatch → `dq.iterations_mismatch`; SPEC Appendix A.8 (0 → `billable=False`
  `anthropic.refusal.pre_output`; 6 → `None` `anthropic.refusal.ambiguous`; 2,127 → `True`
  `anthropic.refusal.mid_stream`); compaction + message iterations → two inferences; advisor with and without
  `advisor_model`; `codex.rollout` raises `PricingError`; `sum_check` flags a 17-vs-17,119 defect;
- cache rules: every row of SPEC Appendix A.13; Azure scope `subscription`;
- transitions: Appendix A.9 thresholds; one fixture per cause rule in precedence order (compaction beats ttl;
  model switch beats ttl; refusal-fallback; plan-toggle; ping-pong; effort change ignored for Claude Code on
  Opus 5.5 but a cause for an SDK lane on Opus 5.5 without the beta; each canonical diag label;
  context-shrank; unexplained); ±10 s ambiguity; `predicted_hit is None` when τ unknown;
  `static_prefix_floor` needs ≥ 5 lanes; a request with only an OUTPUT_RESIDUAL inference is skipped;
- shapley: Appendix A.7 (credits 8/18/4, Σ = 30); `shapley_mc` within 3 SE of exact on a 5-player game;
  k > 10 raises; `scale_credits` preserves Σ exactly;
- policy: `parse_policy(to_spec(p)) == p` for hypothesis-generated policies; unknown clause/selector →
  `UsageError`; `combine` conflict raises; `lane_matches` on each selector key;
- shards: `plan_shards` splits a 300k-request team by lane kind and not a 200k one; `merge_replay` of two halves
  equals the whole (using a trivial in-test replayer); `merge_findings` raises on a duplicate id;
  `stratified_sample` deterministic per seed and the whole index when small;
- findings helpers: `finding_id` independent of evidence order; `miss_waste` on hand fixtures; `fit_cpt` falls
  back below 30 samples;
- no-float lint clean on `core/`.

**Size.** ~2.9k LOC including tests.

### F-KIT — Foundation kit: fakes, conformance, catalogs, keys, k-anonymity (wave 1)

**Goal.** The reference fakes and conformance suites every wave-2 package tests against, the data-only
catalogs (levers, settings allowlist, lifecycle), key-file handling, the single k-anonymity implementation,
and the gate-1 smoke test (SPEC §3.18, §3.20, §3.23, §8.2, §8.4, §11.1, §11.4, D29, D42). You are also the
**contract owner** for waves 2–3 (SPEC §21 #3): keep your session available to hot-fix core bugs and arbitrate
gate-test disputes (record rulings in `tests/v2/kit/RULINGS.md`).

**Owns.** `tokenbill/core/{testing,catalog,keys,kanon}.py`, `tests/v2/kit/**`, `tests/v2/gates/**`.

**Consumes (F-CORE).** `core.records`, `core.types`, `core.labels`, `core.money`, `core.ids`, `core.lanes`,
`core.facts`, `core.protocols`, `core.builders`, `core.registry`. Do not import F-SEM modules except in
gate-F tests (`importorskip`).

**Provides.** `core.testing`: `FakePricer` (rows/modifiers from `core.facts`; every `Pricer` method per SPEC
§6.2–§6.4 incl. per-line exactness, placeholder-output range, unknown-scope range, LIST_EQUIVALENT for the
subscription path, `with_contract`), `MemoryStore` (every `LedgerStore` method incl. §7.3 merge rules,
`lane_index`, `lane_first_reads`, `count_users`, cursors, findings, receipts, `aggregate`, `cost_rows`,
`cluster_days`, purge/audit), `FakeReplayer` (table keyed `(lane_key, spec)`, `from_function`),
`published_for_tests`, conformance helpers `assert_pricer_conforms`, `assert_adapter_conforms`,
`assert_store_conforms`, `assert_replayer_conforms`, `assert_detector_conforms` (incl. the shard-invariance
check by `(team, lane_kind)` groups), `smoke_pipeline_on_fakes`. `core.catalog`: `LeverDef`, `LEVERS` (SPEC §11.1
verbatim), `lever`, `levers_for_kind`, `AllowedKey`, `ALLOWLIST` (§11.4; `verified` from facts.json),
`allowed`, `Promotion`, `SUCCESSORS`, `RETIREMENTS`, `PROMOTIONS`, `ANNOUNCED_UNPRICED`, `successor`,
`retiring_within`, `promotion_for`, `SkuRule`, `SKU_RULES` (from facts.json `sku_rules`), `map_sku` (verified
rules only). `core.keys`: `load_or_create`, `load`. `core.kanon`: `publish`,
`rescope_findings(…, count_users=None)`, `require_self_or_aggregate`, `merge_small_groups`.

**Build.** FakePricer must reproduce SPEC §6.9 cases 1–10, 18, 21, 23, 24 exactly (the parity test in RATES
ties its rows to the real registry). MemoryStore is the executable specification of the store: same merge,
idempotence and privacy behavior as SPEC §7 (no SQL). k-anonymity per SPEC §8.4 including complementary
suppression and exact re-scoping via `count_users`. Catalog data exactly per SPEC §11.1/§11.4/§6.7 (successors
only for same-tier, same-tokenizer pairs). Gate-1 smoke test (`tests/v2/gates/test_gate1_smoke.py`): written
now against SPEC signatures, every real module behind `importorskip`: CC fixture tree (from
`tests/v2/fixtures/claude_code/`, read-only) → `SqliteStore` priced with `RateCard` → `reconcile` against
`tests/v2/fixtures/admin/` pages via ADMIN adapters → `calibrate_lanes` → `run_detectors` →
`build_action_plan` with the real `UsageReplayer`; asserts only structural invariants (labels, no floats,
exact bill equals Σ priced lines, findings sorted, no canary).

**Acceptance tests** (`tests/v2/kit/`):
- `assert_pricer_conforms(FakePricer())` and on `FlatRates`; FakePricer reproduces the §6.9 cases listed
  above; `unit_rates` agrees with `price_usage` on 200 random usages;
- `assert_store_conforms(MemoryStore)`; `MemoryStore.aggregate(group_by=["principal"])` raises; merge of a
  FULL transcript request and a NO_TTL_SPLIT OTel request keeps transcript usage and fills OTel attribution;
- `FakeReplayer` sums only the lanes it is given (cohort subsets work);
- `assert_detector_conforms` catches an undeclared kind, a missing reference, and a shard-dependent test
  detector;
- k-anonymity property test over 1,000 seeded random tables (no published row with `n_users < k`, totals
  preserved, no suppressed child recoverable by subtraction — brute force over subsets); `rescope_findings`
  with `count_users` refuses to publish a parent whose distinct users are 4 although children sum to 5;
- keys: file 0600/dir 0700 (POSIX), group-readable key refused, Windows path warns (fake runner);
- catalog: every `LEVERS` grid string parses with `core.policy.parse_policy` (gate F, `importorskip`); every
  `patch_keys` entry is in `ALLOWLIST`; `successor("claude-opus-4-8") is None`;
  `promotion_for("gpt-5.6-sol", "openai_api", "2026-10-01")` found; `map_sku` ignores unverified rules;
- `test_smoke_fakes.py` composes builders → MemoryStore → FakePricer → transitions (gate F) → a registered
  test detector → FakeReplayer → shapley → `RunResult`.

**Size.** ~2.9k LOC including tests.

### RATES — Pricing registry, billing rules, v0.1 pricing compat (wave 2)

**Goal.** Exact, sourced, effective-dated pricing that every bill and counterfactual depends on (SPEC §6;
facts §19.1, §19.2, §19.6; D26, D27, D36). Only rate arithmetic on billed tokens is ever EXACT; exactness
is decided per line; allowance usage is priced on basis `list_equivalent`.

**Owns.** `tokenbill/pricing.py`, `tokenbill/rates/{schema,engine,contract,billing_rules,verify}.py`,
`tokenbill/rates/data/**` (`anthropic.json`, `bedrock.json`, `vertex.json`, `openai.json`,
`billing_rules.json`, `snapshots/pricing-2026-09-23.json`), `tests/test_pricing.py`, `tests/v2/rates/**`,
`tests/v2/fixtures/rates/**`.

**Consumes.** `core.types` (`RateRow`, `Modifier`, `RateLayer`, `ResolvedRates`, `UnitRates`, `PricedLine`,
`PricedInference`, `PricedTotal`, `ContractOverlay`, `Discrepancy`, `PricingReport`), `core.labels`,
`core.money`, `core.records`, `core.models`, `core.catalog` (`PROMOTIONS`, `promotion_for`), `core.facts`,
`core.protocols.Pricer`, `core.testing.assert_pricer_conforms`.

**Provides.** `rates.schema.load_builtin(provider=None) -> RateLayer`, `load_file(path, name) -> RateLayer`,
`model_price_layer(specs: Sequence[tuple[str, str, str]]) -> RateLayer` (`--model-price`);
`rates.engine.RateCard(layers, contract=None)` implementing every `Pricer` method (incl. `unit_rates`,
`supports`, `min_cacheable_tokens`, `tokenizer_family`, long-context band and `output_upper` via
`price_usage`), `rates.engine.price_total(pricer, items) -> PricedTotal`,
`rates.engine.no_cache_equivalent_nano(pricer, inference, ts_ms) -> int`; `rates.contract.load_contract`,
`from_model_pricing`, `to_model_pricing`; `rates.billing_rules.rule_for(provider, failure_mode) ->
BillingRule`; `rates.verify.verify_snapshot`, `verify_live` (opt-in network, injectable opener),
`crosscheck_feed` (warnings only).

**Build.** Data rows per SPEC §19.1 (Anthropic, channel `anthropic_api`; `claude_platform_aws`/`foundry` fall
back per §6.2), Fable 5.1 / Mythos 5.1 from 2026-09-01, Bedrock and Vertex rows at global prices plus
regional ×1.1 modifiers keyed on `endpoint_scope` (unknown scope → `scope_range`), OpenAI gpt-5.6-sol with the
disabled launch row, the promotional row to 2026-11-22 (`promotion` id), the > 272K whole-request band, flex
0.5 / fast 2.0 / batch 0.5 / regional +10% modifiers; Anthropic batch 0.5, US geo 1.1 (generation ≥ 4.6, 1P
and Claude Platform on AWS), fast-mode `replace_base` rows (Opus 5.5 $8/$40; Opus 5 and 4.8 $10/$50), Priority
burn-down noted `stacking: assumed`; web search $0.01/request. **VERIFY** rows ship `enabled: false`. Load
validation, resolution (incl. `dq.promotion_expired`, unknown scope, billing path), per-line pricing table and
unit-rate scale per SPEC §6.1–§6.4 (never raise for non-integral pico rates). Contract overlay per §6.5.
Billing rules §6.6 (incl. `anthropic.max_tokens`, `openai.max_output_tokens`). Staleness and verify per
§6.7 (default snapshot is the packaged file). `pricing.py` additive edit exactly per §6.8 (must not import
`rates/`), with the matching `SPEC_TABLE` rows in `tests/test_pricing.py` **in the same commit**.

**Facts to verify** (record `verified_on`): §19.1 rows outside facts.json; §19.2 modifiers; §19.6 OpenAI
tiers, Bedrock/Vertex regional; checklist §19.8 #1, #3, #4, #9, #19. Unverifiable → disabled row /
`stacking: assumed`, listed in your README.

**Acceptance tests.**
- the golden billing corpus SPEC §6.9, all 24 cases, compared in int nano (incl. per-line exact/estimated
  splits of cases 7, 9, 23, 24);
- load failures: row without source, overlapping interval, `published_absolute` mismatch, unknown predicate
  key, unknown promotion id, a cache-rule `supports` entry; disabled rows unpriced with `unverified rate row`;
- stale-rate note for a row verified 60 days before the run date; `verify_snapshot` zero diffs, then one
  injected diff reported with its row id; `crosscheck_feed` never fails;
- hypothesis: for random rows, modifiers, contract multipliers (≤ 6 decimals) and buckets,
  `unit_rates(...).bucket_nano` sums equal `price_usage` points to the nano; `unit_rates` is None only when the
  scale would exceed 24;
- layer precedence (contract > `--model-price` > builtin); effective-date lookup; contract overlay not applied
  to the subscription path; `price_total` never mixes allowance into `exact`;
- contract round trip `to_model_pricing(from_model_pricing(x)) == x`;
- `assert_pricer_conforms(RateCard([load_builtin()]))`;
- legacy parity (§6.8), facts parity (every `core/facts.json` rate row and modifier equals the registry's), and
  `tests/test_pricing.py` green; existing suite green; no-float lint clean on `rates/`.

**Size.** ~2.6k LOC including tests.

### CC — Claude Code transcript importer, collector, headless/CI/SDK streams (wave 2)

**Goal.** The most valuable fleet sources: a streaming, content-free importer for `~/.claude/projects/**`
that de-duplicates correctly and preserves the 5m/1h split, iterations, diagnostics, per-turn effort, quota
state and lane structure; the incremental cursor logic the on-device collector uses; and the headless/CI/Agent
SDK stream adapter for traffic that never reaches a laptop (SPEC §5.3, §5.4, §5.12, D26, D27, D33, D40). Get the
empirical traps right: one billed call appears on ~2.4 lines; naive summing overstates spend 2.33×.

**Owns.** `tokenbill/adapters/{claude_code,cc_collect,cc_headless}.py`, `tests/v2/claude_code/**`,
`tests/v2/fixtures/claude_code/**`.

**Consumes.** `core.records`, `core.types` (`IngestOptions`, `IngestResult`, `DataQualityNote`,
`QuarantineItem`, `SourceInfo`), `core.conventions.anthropic_inferences`, `core.models.normalize_model`,
`core.ids`, `core.jsonl`, `core.secrets` (counts only), `core.evidence` (CPT defaults), `core.builders`
(`CANARY`), `core.testing` (`MemoryStore`, `assert_adapter_conforms`).

**Provides.** `adapters.claude_code.ClaudeCodeAdapter` (registry name `claude-code`; capabilities per §5.3;
`none` tier only — `fingerprint` → `UsageError`), `iter_claude_files(root) -> Iterator[Path]` (skips
`journal.jsonl`); `IngestResult.naive_usage` per normalized model; `adapters.cc_collect.FileCursor`,
`CollectorState.load/save` (private file), `collect_incremental(root, state, opts, *, now_ms) ->
Iterator[IngestResult]`; `adapters.cc_headless.ClaudeCodeHeadlessAdapter` (`claude-code-headless`).

**Algorithm.** SPEC §5.3 steps 1–12 exactly (dedupe by `message.id` keeping max `output_tokens`; drop
duplicate `(file, uuid)` lines; skip `<synthetic>`; `ts_start` from the last preceding user/attachment
entry; `perTurnEffort` over `effort`; iterations/refusal rule via `anthropic_inferences`; MESSAGE_START_ONLY
with `output_upper` in tokens and `dq.message_start_only` carrying tokens only; compaction event + ESTIMATED
compaction inference on a `#compaction` lane; fallback/api_error/attachment/human-prompt/cost-state/upgrade/
quota-state events; billing path from `opts.attribution` refined by `quotaLimits.isUsingOverage`; appended
items as sizes only; attribution with `cwd_key`, `gitBranch` dropped, MCP/skill/plugin names HMAC'd with
`opts.name_key` unless allowlisted; endpoint scope from `opts.attribution.extra`/`--attr`; lanes from paths
and meta.json; self-checks; `requestId` only as `provider_request_id` with the collision note). Identity per
§5.1/§5.4: `central` → `r_<ref>` (reject refs with `@` via `UsageError`), `two-stage` → `c_` HMAC with
`opts.principal_key`, `install` → `p_`; `SourceInfo.name_key_id`/`principal_key_id` set. Incremental cursors
per §5.3 (offset advanced only to the earliest unclosed assistant group; head-hash rotation → full re-read;
bounded recent-uuid set). Headless streams per §5.12 (dedupe, lanes by `parent_tool_use_id`, per-model
OUTPUT_RESIDUAL inferences from `modelUsage`, resumed/zeroed result rules, `total_cost_usd` → COST_STATE,
CI attribution from `opts.attribution`).

**Fixtures.** Synthetic, schema-true per SPEC §19.4, generated by a builder script
`tests/v2/fixtures/claude_code/build_fixtures.py` whose outputs are checked in (other packages' gate tests read
them); plant `CANARY` in message text, thinking, tool inputs, tool results, `cwd`, `gitBranch`, file paths
inside `toolUseResult`, attachment content, headless stream content and an email-shaped string. Include a
`MANIFEST.json` describing each file and its expected token totals.

**Facts to verify.** §19.8 #15 (`quotaLimits` semantics) and #16 (headless message keys); unverified →
README.

**Acceptance tests.**
- one message split over 3 lines with outputs 3 / 250 / 470 → one request, output 470; `naive_usage` 3× the
  input tokens for that model;
- duplicate-uuid line dropped (`dq.duplicate_uuid_lines`); `<synthetic>` skipped;
- iterations: declined output 0 on `claude-fable-5` + fallback on `claude-opus-4-8` → two inferences, declined
  `billable=False`; declined output 6 → `billable=None`; compaction iteration priced as its own inference;
- 1h 3,000 + 5m 2,000 split preserved; `cache_creation_input_tokens` 6,000 with that split →
  `cache_write_unknown` 1,000 and `dq.ttl_split_residual`;
- no-stop placeholder (output 3) followed by a matching `tool_result` → `MESSAGE_START_ONLY`, `output_upper ≥
  3`, `dq.message_start_only` with a token count and **no** figure; a no-stop call with output 900 is FINAL;
- `perTurnEffort` present → `params.effort` = it and `session_effort` = `effort`;
- `quotaLimits.isUsingOverage` true with `billing_path=subscription` → following requests `usage_credits`,
  earlier ones `subscription`; QUOTA_STATE events emitted;
- `compact_boundary` → COMPACTION event + ESTIMATED compaction inference; `model_refusal_fallback` →
  MODEL_FALLBACK; `api_error` → API_ERROR with `error_type` from status; attachments → CONTEXT_INJECTION with
  `n_bytes` only; human prompt detection rules; version change → UPGRADE;
- subagent file → SUBAGENT lane linked to the main lane; workflow path → WORKFLOW_AGENT; diagnostics and
  `thinking_dropped` captured; `requestId` mapping to two message ids → `dq.request_id_collision`;
- self-checks: version histogram, retention warning (mtime older than 27 days; injected clock);
- a truncated last line is quarantined and the rest imported; the same file read twice → identical results;
- incremental: ingest half a file, append the rest (including a higher-output split entry of an emitted id),
  resume → merging both results into `MemoryStore` equals a one-shot import; head-hash change → full re-read;
  an in-flight last group is re-read next time;
- headless: parallel tool calls sharing one message id counted once; a stream with per-step placeholders and
  a `result` with `modelUsage` → OUTPUT_RESIDUAL per model equal to the difference, priced EXACT by FakePricer;
  subagent steps on their own lane; resumed-session result skipped with `dq.headless_resumed_totals`; zeroed
  result skipped; `total_cost_usd` only as COST_STATE; the execution-file array form and JSONL form both
  parse; `--output-format json` alone → headless-result aggregate only;
- identity modes (`r_`, `c_`, `p_`); a ref containing `@` rejected; names HMAC'd unless allowlisted; the same
  MCP name yields the same `h_` under the same name key in both adapters;
- CANARY absent from `repr` and `to_json` of every result; no `none`-tier string field > 64 bytes of source
  text (property test); `assert_adapter_conforms` for both adapters; hypothesis fuzz (random truncation/field
  deletion) never raises outside `TokenbillError`;
- perf (marker `perf`): ≥ 25,000 assistant lines/s on a 200,000-line synthetic file, peak RSS ≤ 150 MB; PR
  variant 20,000 lines.

**Size.** ~3.0k LOC including tests.

### TRACE — trace@1/trace@2 adapters, fingerprints, Recorder v2 (wave 2)

**Goal.** The content-free interchange format (trace@2, one format with `usage` / `fingerprint` / `full`
profiles), the bridge from legacy trace@1 files into the v2 ledger (including the breakpoint-count mapping the
dual-engine gate needs), block fingerprinting, and the recorder upgrade for API agents that records every
HTTP attempt — without changing any v0.1 behavior (SPEC §4, §5.5–§5.8, D12, D13, D35).

**Owns.** `tokenbill/adapters/{fingerprint,trace_v1,trace_v2}.py`, `tokenbill/instrument.py`,
`tests/v2/trace/**`, `tests/v2/fixtures/trace/**`.

**Consumes.** `core.records`, `core.types`, `core.ids`, `core.jsonl`, `core.conventions`, `core.models`,
`core.secrets`, `core.keys`, `core.builders`, `core.testing`; frozen `tokenbill.trace` (`_parse_call`,
`read_trace`, `write_trace`), `tokenbill.breakers.VOLATILE_PATTERNS`, `tokenbill.demo_traces`,
`tokenbill.common.canonical_json`.

**Provides.** `adapters.fingerprint.fingerprint_request`, `volatile_spans`, `normalize_volatile`,
`lookback_positions`, `infer_lanes` (§5.8); `adapters.trace_v1.TraceV1Adapter` (`trace@1`);
`adapters.trace_v2.TraceV2Adapter` (`trace@2`, honoring `opts.renormalize`), `write_trace_v2`,
`iter_trace_v2` (§4.4); `instrument.Recorder` and `recording` with the §5.7 signature (defaults keep v0.1
behavior).

**Build.** Fingerprinting per §5.8 (cache_control stripped before hashing; wire order preserved; `h`,
`h_sorted`, `h_norm`; volatile classes before hashing; run-collapsed lookback positions; est tokens; images;
`deferred` tools; markers; content map only in FULL). trace@1 adapter per §5.5 (lenient per-line parsing via
`trace._parse_call`; 5m writes EXACT unless a `ttl:"1h"` marker → `cache_write_unknown` with hint `1h` and
`dq.ttl_1h_markers_in_trace1`; **breakpoint mapping**: explicit markers → `Breakpoint`s; a positive
`cache_breakpoints` count without recoverable positions → one `assumed` end-of-messages breakpoint and
`dq.breakpoint_assumed_end`; count 0 → none; `session_key` namespaced by source; lanes inferred when a run
interleaves models or lanes). trace@2 codec per §4 (closed key sets, 256-char cap, principal/HMAC formats,
`EXTRA_KEYS`, integers only, profile rules, delta-encoded fingerprints, gzip, canonical line output, header key
ids → `SourceInfo`). Recorder per §5.7: `format="trace@1"` is byte-for-byte the v0.1 path; `format="trace@2"`
adds beta namespaces, pre-send snapshot (`model_dump()` when present), **duck-typed httpx event hooks** that turn
SDK-internal retries into separate attempts (status, `x-stainless-retry-count`, `retry-after(-ms)`,
`x-should-retry`) with `dq.sdk_retries_invisible` when hooks cannot attach, exceptions and aborted streams
(PARTIAL_STREAM), raw usage + convention id, RequestParams, opt-in diagnostics header and
`previous_message_id`, bounded background queue that never raises into the caller, `close()` flush; never
imports `anthropic` or `httpx`.

**Acceptance tests.**
- fingerprint: moving `cache_control` from message 3 to 4 leaves every block hash unchanged; swapping dict
  key order changes `h` but not `h_sorted`; the demo `timestamp` scenario's system blocks differ in `h` and
  match in `h_norm` with class `iso_datetime`; 25 consecutive `tool_result` blocks share one lookback
  position; image estimate `ceil(w/28)·ceil(h/28)` with caps; `defer_loading` → `deferred`; markers and TTLs
  extracted; content map only in FULL;
- trace@1: the four demo scenarios ingest and price (with `FakePricer`, which has `claude-sonnet-5`) to v0.1
  `as-billed` dollars within 1 nano per call; `well-behaved`/`timestamp`/`tool-churn` (count 1) each get one
  assumed end breakpoint, `no-cache` (count 0) none; a `ttl:"1h"` marker yields the unknown-TTL range; two
  files reusing `run_id` → two sessions; an interleaved two-model run → two inferred lanes (`lane_exact=False`);
  a malformed line is quarantined in lenient mode and raises in strict mode; `assert_adapter_conforms`;
- trace@2: write → read → write byte-identical (all record types, all profiles); rejected: unknown key,
  string > 256 chars, principal containing `@`, `c_` principal without `two-stage`, an `extra` key outside
  `EXTRA_KEYS`, a float number, `fp`/`blocks` in a `usage` profile, missing header; `.gz` round trip;
  `renormalize` rebuilds inferences from `raw_usage` and records `dq.renormalized`; CANARY absent from
  `usage` and `fingerprint` files; hypothesis fuzz of the reader;
- recorder: existing `tests/test_instrument.py` passes unchanged; with fake SDK doubles in `trace@2` mode:
  `beta.messages.create/stream` recorded; a payload mutated after the call is recorded as sent; a fake client
  exposing `_client.event_hooks` that performs two 529 attempts then a 200 yields three attempts with statuses,
  retry counts 0/1/2 and `retry_after_ms`; a client without hooks yields one attempt and
  `dq.sdk_retries_invisible`; an exception yields an attempt with outcome `http_error` and no usage; an aborted
  stream yields `PARTIAL_STREAM`; async client variants (async hooks); a failing writer never raises into the
  caller and increments `dropped`; `diagnostics=True` adds the beta header and `previous_message_id`; a
  300-call session with a 150k-token context produces a trace@2 file < 2% of the trace@1 size;
- gate `test_gate_ratecard.py` (`importorskip("tokenbill.rates.engine")`): demo trace@1 exact bill with the
  real `RateCard` equals v0.1 as-billed within 1 nano per call;
- gate `test_gate_collector_roundtrip.py` (`importorskip("tokenbill.adapters.cc_collect")`): CC
  `collect_incremental` over CC's checked-in fixture tree → `write_trace_v2(profile usage)` →
  `TraceV2Adapter.read` → records equal by `to_json`; ingesting either into `MemoryStore` gives identical
  dumps.

**Size.** ~3.0k LOC including tests.

### TELEM — OTLP/JSON, OpenAI, Bedrock, Anthropic-response adapters; conventions (wave 2)

**Goal.** Normalize every non-transcript usage source into disjoint buckets without double counting, keep
OpenAI's cache diagnostics as ground truth, and refuse to treat telemetry cost estimates as bills (SPEC §5.2,
§5.9, §5.10, D23, D43, R4).

**Owns.** `tokenbill/adapters/{conventions_ext,otel,openai,bedrock,anthropic_responses}.py`,
`tests/v2/telemetry/**`, `tests/v2/fixtures/telemetry/**`.

**Consumes.** `core.conventions` (`register_convention`, `Convention`, `sum_check`, `anthropic_inferences`),
`core.records`, `core.types`, `core.ids`, `core.jsonl` (`open_text`, zstd guard), `core.models`,
`core.builders`, `core.testing`.

**Provides.** `adapters/conventions_ext.py` registering on import `bedrock.converse`, `openai.responses`,
`openai.chat`, `otel.genai`, `otel.genai.legacy`, `openinference`, `claude_code.otel` (mappings §5.2);
`adapters.otel.OtlpJsonAdapter` (`otlp`), `adapters.openai.OpenAIUsageAdapter` (`openai`),
`adapters.bedrock.BedrockAdapter` (`bedrock`), `adapters.anthropic_responses.AnthropicResponsesAdapter`
(`anthropic-responses`).

**Build.** Per §5.9–§5.10: OTLP file-exporter JSON lines (logs, metrics, traces; `.gz`; `.zst` only on Python ≥
3.14 else a clear `SourceError`), int64 values as strings parsed to int, attribute arrays; Claude Code
`api_request` → requests with fidelity NO_TTL_SPLIT, writes → `cache_write_unknown`, `cost_usd` →
`provider_reported_cost_nano` basis `provider_estimate`, `dq.no_ttl_split`, billing path from `opts`;
identities (`user.email`, `user.id`, `user.account_uuid`) mapped to team via `opts.team_map`, pseudonymized with
`opts.principal_key` (org key; `central-ingest`) and dropped; names with `opts.name_key`; resource attributes →
attribution; lanes from `session.id` + `query_source` (+ `agent_id` from beta spans, which also give
`ttft_ms`); `api_error` → event + failed attempt; `tool_result` → appended item; metrics →
`UsageAggregate(source_kind="otel.metric")` never in the ledger; raw-body events ignored with
`dq.raw_bodies_ignored`; GenAI spans and OpenInference LLM-kind spans only. OpenAI: served tier;
`incomplete_details.reason == "max_output_tokens"` → `stop_reason = "max_tokens"`; `prompt_cache_diagnostics` →
`CacheDiagnostic` with the canonical-reason mapping of §5.10; `azure_openai` channel → `sub:` cache scope.
Bedrock invocation logs (`identity.arn` → team map → `p_`, allowlisted `requestMetadata`, usage from the logged
body, `normalize_model` for Bedrock ids, billing path `bedrock`). Anthropic responses and batch results
(`service_tier` batch; `{request_meta, response}` pairs with `channel`, `endpoint_scope`, `model_raw`,
`billing_path`).

**Facts to verify.** §19.4 OTel/OpenAI/Bedrock field names; checklist §19.8 #8 (incl. OpenAI diagnostics paths).

**Acceptance tests.**
- a golden sum-check fixture per convention: OpenAI Responses `input 10000, cached 6000, cache_write 2000` →
  uncached 2,000, read 6,000, `cache_write_other` 2,000 with TTL 1,800 s; Chat with reasoning subset; OTel
  GenAI new and legacy names; OpenInference trace with one AGENT span wrapping two LLM spans → exactly two
  requests; Bedrock `cacheDetails` split with residual → unknown; a CrewAI-style span (17 input tokens with
  implied 17,119) → `dq.sum_check_failed`; a GenAI span with `read + write > input` → treated exclusive with
  `dq.convention_mismatch`;
- OTLP: 3 `api_request` events + 1 `api_error` + token metrics → 3 requests, 1 failed attempt/API_ERROR event,
  metric aggregates not in `requests`; `intValue` strings parsed; `cost_usd` only as `provider_estimate`;
  `user.email` absent from every output and replaced by a `p_` pseudonym under the principal key; team from the
  team map; raw-body events counted and ignored; beta `llm_request` span sets `ttft_ms` and exact lanes; a
  `.zst` file on Python < 3.14 → `SourceError` naming `compression: none`;
- OpenAI: served `service_tier`; each diagnostics reason maps to its canonical reason and keeps
  `provider_reason`; `cache_missed_tokens` never priced; truncated response → `stop_reason = "max_tokens"`;
  Azure channel → `sub:` scope; Anthropic batch results carry `service_tier="batch"`; a Vertex `request_meta`
  supplies `model_raw` and `endpoint_scope`; Bedrock `global.` id → scope global;
- `assert_adapter_conforms` for all four adapters; CANARY absent; hypothesis fuzz per parser.

**Size.** ~2.6k LOC including tests.

### ADMIN — Admin/Analytics page adapters and cloud billing exports (wave 2)

**Goal.** Parse the provider-side truth — Anthropic usage/cost/Analytics pages, OpenAI organization usage and
costs, AWS CUR 2.0 and the GCP billing export — into `UsageAggregate`, `CostLine` and team-level
`OutcomeAggregate` records, exactly, content-free and never per person (SPEC §5.11, §5.13, D31, D32).

**Owns.** `tokenbill/adapters/{anthropic_admin,openai_admin,cloud_billing}.py`, `tests/v2/admin/**`,
`tests/v2/fixtures/admin/**`.

**Consumes.** `core.records` (`UsageAggregate`, `CostLine`, `OutcomeAggregate`), `core.types`
(`IngestOptions`, `IngestResult`), `core.money` (`cents_to_nano`, `usd_str_to_nano`), `core.ids`, `core.jsonl`,
`core.kanon.merge_small_groups`, `core.models`, `core.catalog` (`map_sku`, `SKU_RULES`), `core.builders`,
`core.testing` (`assert_adapter_conforms`). Map CUR usage types / GCP SKUs only through
`core.catalog.map_sku` (verified rules only); unmapped rows keep `sku`, leave `model`/`token_type` empty and
count in `dq.unmapped_sku` — never guess.

**Provides.** Adapters `UsageReportAdapter`, `CostReportAdapter`, `ClaudeCodeAnalyticsAdapter`,
`EnterpriseAnalyticsAdapter`, `OpenAIUsageBucketsAdapter`, `OpenAICostsAdapter`, `AwsCurAdapter`,
`GcpBillingExportAdapter` at the registry paths of SPEC §3.7; checked-in recorded-page fixtures with a
`MANIFEST.json` (RECON's and F-KIT's gate tests read them).

**Build.** Page adapters per §5.11: cents decimal strings parsed exactly with remainders recorded in
`IngestResult.stats["rounding_remainder_e18"]` (the remainder in 1e-18 USD units as an int); provisional within
the 30-day revision window relative to `opts.now_ms`; Claude Code Analytics and Enterprise user-level endpoints
**aggregated to (date, team) at ingest** through `opts.team_map` with `opts.k_anonymity` (`core.kanon`), groups
below k merged into `(other)` or dropped with `dq.outcomes_suppressed`; actor refs never leave the adapter.
Cloud billing per §5.13: CUR 2.0 CSV/CSV.gz (Bedrock product codes only; `line_item_iam_principal` → team map →
`p_` with the principal key; account ids → `h_` with the name key; `pricing_unit` 1K vs 1M normalization;
net vs unblended cost), GCP billing export CSV/JSONL (Claude SKUs; credits; labels allowlist; region → scope).
Channel set on every aggregate and cost line.

**Facts to verify.** §19.4 usage/cost/analytics field names and CUR/GCP columns; checklist §19.8 #6, #7
(field names; the map itself is RECON's), #17 (column names only). Build fixtures shaped from the documented
responses; mark unverified names in your README.

**Acceptance tests.**
- usage report pages → aggregates with the right buckets and dims (api keys and workspaces `h_` under the name
  key); cost report → cost lines with exact nano and remainders; many-decimal cents strings parsed without
  float (no-float lint clean on `adapters/cloud_billing.py`);
- Claude Code Analytics per-user rows become `(date, team)` aggregates and outcome rows; a team with 3 users
  is merged into `(other)` or dropped; no output row carries a principal, actor id or email;
- Enterprise Analytics `amount`/`list_amount` parsed; dates within 30 days of the injected clock are
  provisional;
- OpenAI usage buckets and costs parsed with channel `openai_api`;
- CUR: a synthetic CUR 2.0 CSV (and its `.gz`) with Bedrock input/output/cache rows in 1K and 1M units →
  token aggregates and cost lines; IAM principals become `p_` and teams; non-Bedrock rows skipped; Parquet →
  `SourceError` naming the CSV export; GCP: Claude SKU rows with credits → cost lines with `amount = cost +
  credits`, labels allowlisted, region → endpoint scope;
- `assert_adapter_conforms` on every adapter; CANARY absent; hypothesis fuzz of every parser.

**Size.** ~2.5k LOC including tests.

### RECON — Reconciliation ledger gate, residuals, cost maps, live pull, org scan (wave 2)

**Goal.** The trust anchor: prove (or disprove) per channel that the ledger and rate card match the
provider's own usage and invoice data, explain residuals, derive contract discounts, and give an admin
aggregate findings from Admin data alone (SPEC §12, §10.3, D19, D26, D31, D32). Works fully offline from
recorded pages; live pulls are opt-in.

**Owns.** `tokenbill/recon/{reconcile,residuals,costmap,pull,orgscan}.py`, `tests/v2/recon/**`,
`tests/v2/fixtures/recon/**`.

**Consumes.** `core.records` (`UsageAggregate`, `CostLine`, `UsageRecord`), `core.types` (`ReconRow`,
`ChannelVerdict`, `ReconciliationReport`, `ContractOverlay`, `Finding`, `AnalysisContext`), `core.catalog`
(`map_sku`), `core.money`,
`core.labels`, `core.findings` (finding helpers), `core.protocols` (`Pricer`, `Detector`), `core.builders`
(`make_aggregate`, `make_cost_line`), `core.testing` (`FakePricer.with_contract`, `assert_detector_conforms`).

**Provides.** `recon.reconcile.reconcile(...)` (signature §12); `recon.costmap.COST_TYPE_MAP`;
`recon.residuals.classify(...)`; `recon.pull.pull(kind, *, key_env, since, until,
out_dir, opener=None, sleep=time.sleep)`; `recon.orgscan.OrgScan` (registry `aggregate.org-scan`, requires
`{"aggregates"}`, kinds §10.3).

**Build.** Reconciliation per §12.1–§12.4 **per channel**: rate-card check priced from provider usage
(independent of the ledger), token coverage with the over-count rule, dollar coverage (allowance excluded),
the residual classifier in order (incl. `seat_allowance_unmetered`, `cents_rounding` from parse remainders,
`cloud_credits`, `unmapped_cost_type` for disabled SKU rules), effective discount, `--suggest-contract` overlay
with `channels` and **re-run** with `rerun_pricer_factory`, channel verdicts with `mapping_verified`, overall
verdict, and the **channel-total mode** of §12.1 when SKU/cost-type rules are unverified. `COST_TYPE_MAP` is
data from documented `cost_type`/`token_type` values; CUR/GCP rules come from `core.catalog.map_sku` (F-KIT). The ledger
is streamed (`Iterable[UsageRecord]`), memory bounded by keys. Live pull per §12.5. Org scan per §10.3: EXACT
premiums via `pricer.price_usage` on aggregate buckets, cache-read share vs the 0.84/0.94/0.80 benchmarks with
sources, write:read thrash, TTL mix, batch share, effective discount; workspace/model scopes only.

**Facts to verify.** `cost_type`/`token_type` enumerations, pagination names (checklist §19.8 #6, #10), SKU
rules (#17). Unverified → README and disabled rules.

**Acceptance tests** (build aggregates and cost lines with `core.builders`; do not import ADMIN):
- usage + cost records priced exactly → rate-card error 0, channel and overall verdict `reconciled`;
- cost report 15% below list on every model → effective discount 0.15; suggested overlay multiplier 0.85; the
  re-run is `reconciled` on CONTRACT basis; a non-uniform discount → "not a simple multiplier";
- two channels: `anthropic_api` reconciled and `bedrock` without invoice lines → bedrock `insufficient_data`,
  overall `not_reconciled`, and the report lists which channels a FOCUS export may include;
- a CUR-shaped channel with net cost 10% below unblended → effective discount 0.10 and a suggested bedrock
  overlay; with every SKU rule disabled the channel reconciles in channel-total mode ("totals only; schema
  unverified", `mapping_verified = False`), and a 3% unexplained gap on totals fails it;
- a Priority-tier usage bucket → residual `priority_excluded_from_cost_report`, not an error; code execution
  line → `code_execution_cost_report_only`; dates inside the revision window → provisional, excluded with
  `closed_only`; provider tokens absent from the ledger → `unobserved_traffic`; subscription-path ledger
  dollars → `seat_allowance_unmetered`; parse remainders → `cents_rounding`; an unknown cost type →
  `unmapped_cost_type`;
- ledger tokens 3% above provider on one day → over-count row, verdict `not_reconciled`; no invoice rows →
  `insufficient_data`; no-float lint clean on `recon/`;
- `pull` with a fake opener and fake sleep: pagination, 31-day chunking, rate limit, `retry-after` capped at 60 s;
  the key string never appears in recorded pages, logs or exception messages (grep test); missing `key_env` →
  `UsageError`; the socket guard proves no real network;
- org scan on hand-built aggregates: fast/geo/priority premiums exact to the nano; cache-read share 0.60 →
  finding with benchmark sources; share 0.90 → none; `assert_detector_conforms(OrgScan())`;
- gate `test_gate_ratecard_admin.py` (`importorskip` RATES engine and ADMIN adapters): ADMIN's recorded
  fixtures parsed by the real adapters reconcile with the real `RateCard`.

**Size.** ~2.7k LOC including tests.

### STORE — SQLite store, merge, rollups, retention, pseudonymization (wave 2)

**Goal.** A fleet-scale, content-free ledger with exact integer money (exact, estimated and allowance lines
kept apart), idempotent order-independent merges, streaming lane access for sharded analysis, rollups that
survive identity retention, and privacy enforced at write time (SPEC §7, §8.1–§8.3, D26, D27, D39, D41).

**Owns.** `tokenbill/store/{schema,db,merge,rollups,retention,pseudonym}.py`, `tests/v2/store/**`.

**Consumes.** `core.records`, `core.types` (`IngestResult`, `RawAggregate`, `AggRow`, `LedgerCostRow`,
`ClusterDay`, `Finding`, `ReceiptRow`, `LaneIndexRow`), `core.protocols` (`LedgerStore`, `Pricer`), `core.ids`,
`core.lanes.group_lanes`, `core.errors.PrivacyError`, `core.jsonl.open_private`, `core.builders`, `core.testing`
(`FakePricer`, `MemoryStore` as the reference, `assert_store_conforms`, `CANARY`).

**Provides.** `store.db.SqliteStore` (the full `LedgerStore` of SPEC §3.6: `ingest`, `reprice`,
`iter_lanes(where, lane_keys)`, `iter_requests`, `iter_usage_records`, `lane_index`, `lane_first_reads`,
`count_users`, `aggregates`, `cost_lines`, `outcomes`, `aggregate`, `cluster_days`, `cost_rows`,
`get_cursor`/`set_cursor`, `put_findings`/`findings`, `put_receipt`/`receipts`, `purge`, `audit`, `meta`;
`read_only=True` mode for shard workers); `store.schema` (DDL §7.1, forward-only migrations); `store.merge`
(§7.3); `store.rollups.refresh_rollups`; `store.retention.apply_retention`, `purge`, `rotate`;
`store.pseudonym.Pseudonymizer`.

**Build.** DDL exactly as §7.1 (int nano money incl. `exact_nano`, `est_*`, per-bucket columns and
`exact_mask`; `attr_extra_json`; `billing_path`; denormalized `lanes.team/billing_class`; WAL; 0600 file,
0700 dir; indexes). `ingest` in 5,000-row transactions; pseudonymize `r_`/`c_` with the org key before any
write (`PrivacyError` without a key); accept `p_` only under the store's org key id and `h_` only under its
name key id (else null those fields with `dq.name_key_mismatch`); price with the given pricer and store point,
exact, estimated and per-bucket nano; same-sha source re-ingest is a no-op. Merge rules §7.3 including
per-field attribution priority (and per `extra` key) and the `request_index` collision flag. `iter_lanes`
streams by lane with bounded memory. `aggregate` over the whitelisted dimensions with `COUNT(DISTINCT
principal)`; principal/session in `group_by` → `PrivacyError`. `count_users` is an exact distinct count that
never returns ids. Rollups §7.4 (refreshed before identity retention; `allowance_nano` kept apart).
Retention/purge/audit/rotation §7.5.

**Acceptance tests.**
- `assert_store_conforms(SqliteStore)`;
- idempotence: ingesting the same sources twice leaves an identical `iterdump()` of the data tables; order
  independence over all permutations of 5 small sources (transcript-, OTel-, trace@2-, trace@1- and
  responses-shaped `IngestResult`s built with foundation builders);
- cross-source merge: a FULL-fidelity transcript request and a NO_TTL_SPLIT OTel request joined through
  `provider_request_id` → one row with transcript usage, OTel-only attribution filled (incl. an `extra` key),
  two bits in `sources_mask`; a `requestId` seen with two message ids is never used as a join key;
- two trace@1 sessions reusing `run_id` → separate sessions and a total of $22, not $40;
- invariants Σ request priced nano == Σ inference priced nano and Σ exact + Σ est == Σ priced; a placeholder
  inference stores its input lines as exact and its output line as estimated; subscription inferences land in
  `allowance_nano` rollups and never in `exact_nano`; `cost_rows` per-bucket sums equal ledger totals exactly;
- `attr_extra_json` round-trips (`mdm_group` → `cluster_day` kind `mdm_group`; `task_id` survives);
- file mode 0600 / dir 0700 (POSIX); `r_`/`c_` principals stored only as `p_`; the raw ref and CANARY never
  appear in the SQLite file bytes; ingest without an org key but with `r_` principals → `PrivacyError`; a source
  whose `name_key_id` differs from the store's → names nulled, `dq.name_key_mismatch`;
- `aggregate(group_by=["principal"])` → `PrivacyError`; `count_users` equals a brute-force distinct count;
  rollups give correct `active_users`; retention nulls principals only after rollups refresh and `cluster_day`
  survives; `purge` deletes, vacuums and writes an audit row without the raw identity; key rotation writes
  `key_events`;
- `lane_index`, `lane_first_reads`, `iter_lanes(where={"team": …, "lane_kind": …})` and `lane_keys` sampling
  are consistent with `MemoryStore`; two read-only connections scan concurrently;
- perf (marker `perf`): ingest ≥ 20,000 requests/s; `iter_lanes` over 10⁶ requests ≤ 30 s; `lane_index` over
  10⁶ ≤ 5 s; peak RSS ≤ 500 MB; PR variants at 10⁵;
- gate `test_gate_adapters.py` (`importorskip` CC/TELEM/TRACE/ADMIN adapters and the RATES engine): their
  checked-in fixture files read through the real adapters ingest, merge and price correctly.

**Size.** ~2.8k LOC including tests.

### REPLAY — Usage-level replay engine and the model gate (wave 2)

**Goal.** The counterfactual engine for every source (content-free) with minimal-change semantics, the
two-way TTL flips, keepalive (streaming-safe rules), compaction window, cold resume, rate transforms incl.
selector-scoped effort, repairs, and the streaming two-pass predictive calibration gate that alone can mark
projections CALIBRATED (SPEC §9.1–§9.6, D7–D9, D26, D28, D30).

**Owns.** `tokenbill/sim/{usage_replay,calibrate}.py`, `tests/v2/sim/**`.

**Consumes.** `core.transitions` (`classify_transitions`, `static_prefix_floor`, `lane_first_reads_of`),
`core.cache_rules`, `core.policy` (`parse_policy`, `lane_matches`, `request_matches`), `core.catalog`
(`successor`), `core.shards.merge_replay`, `core.types` (`Policy`, `ReplayResult`, `ReplayRequestOutcome`,
`CalibrationPartial`, `CalibrationReport`, `Transition`, `UnitRates`), `core.labels`, `core.money`,
`core.evidence`, `core.protocols` (`Replayer`, `Pricer`), `core.builders` (`FlatRates`, `lane_from_table`),
`core.testing` (`FakePricer`, `assert_replayer_conforms`).

**Provides.** `sim.usage_replay.UsageReplayer` (`Replayer`); `sim.calibrate.calibrate(lane_batches, *, pricer,
rules, granularity="day", folds=5, seed=0, static_prefix_floor=None) -> CalibrationReport`,
`calibrate_lanes(lanes, **kw)`, `calibrate_pass1`, `calibrate_pass2`, `finish_calibration`.

**Build.** Exactly §9.2–§9.4 with the fixed application order (rate transforms → context transforms →
cache-state transforms → TTL re-rating/min-prefix/batch → pricing); `baseline`/`cost`/`saving` Figures
per §3.5 (points and bounds; savings per request then summed); passthrough inferences priced unchanged unless a
rate transform applies; ambiguity ranges; keepalive only for non-Claude-Code lanes and skipped only for
structured outputs / forced tool_choice / `thinking=enabled:*` / batch (streaming lanes are allowed);
`effort` clauses scoped by selector; mixed billing classes → `UsageError`; integer unit rates on the hot path
with the `price_usage` fallback for range lines; calibrated mode using the report's ρ. Model gate exactly §9.6:
two streaming passes over `lane_batches()`, mergeable partials, one-step-ahead predictions that never read `R_i`,
FEMP thresholds, ≥ 12 periods, ρ per gap band with Wilson CIs (pooled below 30 trials), day-level 5-fold
cross-validation, diagnostics confusion matrix with the canonical-reason classes (Anthropic and OpenAI),
no-comparison labels and TTL corroboration counted separately.

**Acceptance tests.**
- Appendix A.1–A.6 and A.2b exactly to the nano with `FakePricer` (5m→1h saves $1.1508; bursty lane → 1h
  costs +$0.318; A.2b 1h→5m saves $0.318; 1h→5m on A.1 costs +$1.1508; keepalive $0.6924, 4 pings for 20
  minutes, 15 capped pings and cold for 2 hours, Claude Code lane skipped; a streaming SDK lane is **not**
  skipped; a structured-output lane is; compaction window $1.999 and "no change" above the lane maximum);
- identity: `replay(Policy.observed())` has `cost == baseline` (points and bounds) and `saving == 0` on 200
  random lanes including placeholder and unknown-TTL inferences (`assert_replayer_conforms`); minimal change:
  requests a policy does not affect have `changed=False`;
- ambiguity: a 305 s gap under `ttl=5m` yields a range spanning hit and miss;
- model remap applies the tokenizer band only across families; same-tier upgrade has no band; effort scaling
  with a selector touches only matched lanes; `fast=off` flips fast-toggle misses; batch hit band low/high;
  each repair (restore_caching, stagger_fanout, retry_backoff_cap, fallback_credit, shared_ci_prefix) against a
  hand-computed fixture; allowance and billed lanes in one call → `UsageError`;
- sharding: replaying two lane halves and merging with `core.shards.merge_replay` equals one replay;
- calibrated mode multiplies flips by ρ; without a passing report the result is UNCALIBRATED;
- calibrate: lanes generated from the documented rules → NMBE 0, CV(RMSE) 0, `pass`; the same lanes with 10% of
  predicted hits flipped to misses (seeded) → ρ ≈ 0.90 ± 0.03 and the calibrated variant passes; 11 periods →
  `insufficient_data`; 128 idle > 1 h transitions labeled `previous_message_not_found` → TTL corroboration
  128/128 and not counted in precision/recall; `model_changed` label vs predicted model switch counted
  correctly; an OpenAI `param_changed` label vs a predicted effort change counted in the param class;
  `unavailable` counted as no-comparison; the report is identical for one batch vs five batches;
- perf (marker `perf`): 10⁶ requests × 1 policy ≤ 30 s, O(n) scaling check (2× requests ≤ 2.3× time);
  calibrate over 10⁶ requests ≤ 120 s.

**Size.** ~2.8k LOC including tests.

### BLOCK — Block-level replay and cache-breaker suite (wave 2)

**Goal.** For fingerprinted API traffic (recorder trace@2, trace@1 via adapter): a documented-rules block
cache model (tier salts with the conditional effort exemption, 4 breakpoints, 20-position lookback with run
collapsing, first-token visibility, workspace/org scope) and the 11 block breakers with priced repairs,
agreeing with the v0.1 engine on its demo (SPEC §9.7, §10.4, D28).

**Owns.** `tokenbill/sim/block_replay.py`, `tokenbill/detect/block.py`, `tests/v2/blocksim/**`,
`tests/v2/fixtures/blocksim/**`.

**Consumes.** `core.records` (`BlockRef`, `ContentFingerprint`, `Breakpoint`, `Request`, `Lane`),
`core.cache_rules` (incl. `effort_change_keeps_cache`), `core.findings`, `core.types`, `core.protocols`
(`Replayer`, `Detector`, `Pricer`), `core.labels`, `core.builders` (`make_block`), `core.testing`
(`FakePricer`, `assert_replayer_conforms`, `assert_detector_conforms`).

**Provides.** `sim.block_replay.BlockReplayer` (`Replayer` for `breakpoint_policy` and `block:` repairs;
lanes without fingerprints skipped with a reason), `BlockReplayer.predict(lanes, *, pricer, rules,
placement="observed") -> list[ReplayRequestOutcome]`, `first_divergence(prev, cur) -> tuple[str, int, str] |
None` (tier, block index, cause); `detect.block.BlockBreakers` (registry `block.breakers`, `requires =
{"blocks"}`, kinds §10.4).

**Semantics.** Minimal change: `replay(policy)` returns, per lane, billed cost − (model(observed placement) −
model(policy)), so `replay(Policy.observed())` equals the billed ledger exactly and model error cancels;
breaker `recoverable` = model(observed) − model(repair), floored at 0 with the v0.1 wording, None for kinds
without a mechanical repair. Assumed end-of-messages breakpoints (from trace@1 counts) count as observed
markers; `missing-breakpoint` fires only with no markers at all. Billed-vs-predicted disagreement without a
hash divergence is an agreement metric, never a breaker.

**Fixtures.** Build `ContentFingerprint`s in tests with `core.builders.make_block` and a small test-local
hashing helper that follows SPEC §5.8 (hashes exclude `cache_control`). Re-encode the codebase experiments
deterministically: exp1 (8-turn loop, marker moved to the newest block each turn, billed usage of a working
cache), exp2b-a3 (six independent requests sharing a ~20k-char preamble), exp2b-a2 (five byte-identical
requests at one timestamp), exp9-1 (tools alternating between two key orders, billed cold writes), exp6-2 (25
text blocks appended per turn, billed full rewrites).

**Acceptance tests.**
- tier salts: an effort change invalidates only the messages tier for an SDK lane without the per-message
  beta, and nothing for a Claude Code Opus 5.5 lane (SPEC Appendix A.13 rows); a tool definition change
  invalidates all tiers; a speed toggle invalidates system + messages;
- lookback: an entry 21 collapsed positions back is not found; 25 consecutive `tool_result` blocks count as
  one position; TTL refresh on read; entries written at the same timestamp are not visible to siblings;
  breakpoint prefixes below the model minimum write nothing; block tokens rescaled to billed `total_input`;
- exp1 → no history-rewrite, read agreement ≥ 0.95; exp2b-a3 → `breakpoint-placement` saving ≈ 56% of input $
  (static_plus_end); exp2b-a2 → five predicted writes, no phantom 65% saving, a `fanout` finding with an
  upper-bound saving; exp9-1 → `serialization-churn`; exp6-2 → `lookback-overflow` with the every-15 fix;
- one fixture per breaker kind (11) with hand-computed recoverable; negative recoverable floored with the
  v0.1 wording; an assumed end breakpoint suppresses `missing-breakpoint`;
- `assert_replayer_conforms(BlockReplayer())`, `assert_detector_conforms(BlockBreakers())` (incl. shard
  invariance);
- perf: a 4,000-call run ≤ 2 s;
- gate `test_gate_dual_engine.py` (`importorskip("tokenbill.adapters.trace_v1")`): the four v0.1 demo
  scenarios read through `TraceV1Adapter` (fingerprint tier) → `timestamp`: exactly `volatile-system` at call
  1; `tool-churn`: exactly `tool-churn` (order) at the rotation calls, no `volatile-system`, no
  `missing-breakpoint`; `no-cache`: exactly `missing-breakpoint`; `well-behaved`: none, with
  `predict(placement="end")` reads equal to v1 optimal-cache reads within rounding; and the effort-exemption
  agreement with `core.transitions` on the same lanes.

**Size.** ~2.6k LOC including tests.

### SYNTH-ORACLE — Reference replay oracle, synthetic lanes, rollout panels (wave 2)

**Goal.** Independent ground truth for the replay engine and the verification estimators: a deliberately
simple reference implementation of the usage-replay semantics, random and closed-form lane generators, and
panel/campaign generators with known effects (SPEC §9.8, Appendix A, §13). Do not read or copy REPLAY code:
independence is the point.

**Owns.** `tokenbill/synth/{oracle,lanes_gen}.py`, `tests/v2/synth_oracle/**`.

**Consumes.** `core.records`, `core.types` (`Policy`, `ReplayResult`, `ReplayRequestOutcome`, `PanelRow`),
`core.transitions`, `core.cache_rules`, `core.policy`, `core.labels`, `core.money`, `core.evidence`,
`core.protocols`, `core.builders`, `core.testing` (`FakePricer`, `assert_replayer_conforms`).

**Provides.** `synth.oracle.ReferenceReplay` (`Replayer`; Decimal path through `Pricer.price_usage` only;
readable per-request code for TTL two-way flips, keepalive, compaction window, cold resume, model remap,
effort (selector-scoped), fast_off, geo_global, batch, restore_caching, stagger_fanout);
`synth.lanes_gen.random_lanes(seed, n, *, family) -> list[Lane]` (families: ttl, keepalive, compaction,
cold_resume, remap, effort, rates, batch, repairs, placeholder, unknown_ttl), `closed_form(name) ->
tuple[list[Lane], dict[str, int]]` (Appendix A fixtures incl. A.2b, A.10, A.11 with expected nano),
`rollout_panel(*, clusters, weeks, true_effect, waves, holdback, seed, org_wide=False) -> list[PanelRow]`,
`ab_campaign(*, tasks, trials, cost_effect, token_effect, turn_effect, seed) -> tuple[list[Request],
list[Request], list[dict]]`.

**Build.** Oracle per SPEC §9.2–§9.4 written independently from the SPEC (one function per policy family,
no integer hot path, no sharing with REPLAY); `baseline`/`cost`/`saving` Figures with points and bounds.
Generators seeded via `common.rng`; random lanes include every inference shape the ledger can hold (placeholder
output, unknown-TTL writes, refusal iterations, allowance lanes in a separate family).

**Acceptance tests.**
- the oracle reproduces Appendix A.1–A.6, A.2b and the replay-related parts of A.10/A.11 to the nano and
  satisfies `assert_replayer_conforms`;
- `random_lanes` deterministic per seed; `closed_form` expectations match the oracle;
- `rollout_panel` with a 25% effect is deterministic and has the requested waves and holdback; `org_wide=True`
  yields a single-cluster series with a level shift for the ITS tests; `ab_campaign` reproduces the RTK-like
  shape (tokens −38%, turns +14%, cost +7%);
- gate `test_differential_replay.py` (`importorskip("tokenbill.sim.usage_replay")`): the fast
  `UsageReplayer` equals `ReferenceReplay` to the nano (points and bounds) on ≥ 500 random lanes per policy
  family (documented mode), reporting the first differing request on failure.

**Size.** ~2.3k LOC including tests.

### SYNTH-FLEET — Synthetic fleet with planted waste, closed-form truth, schema-true writers (wave 2)

**Goal.** The deterministic synthetic fleet behind `tokenbill demo --fleet`, the per-plant gate tests of the
detectors and plan, and the flagship e2e: eleven teams with planted waste, truth computed independently of
REPLAY and DETECT, and schema-true source files for every adapter family (SPEC §18, §19.4).

**Owns.** `tokenbill/synth/{fleet,truth,writers}.py`, `tests/v2/synth_fleet/**`.

**Consumes.** `core.records`, `core.types`, `core.labels`, `core.money`, `core.ids`, `core.jsonl`,
`core.evidence`, `core.builders` (builders, `CANARY`), `core.testing.FakePricer` (all prices; its rows equal
RATES' by the parity test). Do not import REPLAY, DETECT or SYNTH-ORACLE outside gate tests.

**Provides.** `synth.fleet.generate(seed=7, *, devs=61, days=28, out_dir=None, scale_requests=None) ->
FleetWorld` (`FleetWorld(sessions, requests, events, aggregates, cost_lines, outcomes, source_files: dict[str,
Path], truth: FleetTruth, today: str)`); `synth.truth.FleetTruth` (per plant: detector id, kind, scope, expected
int nano for cost_observed / recoverable / Shapley, tolerance) and the closed-form functions that compute it;
`synth.writers.write_cc_transcripts`, `write_headless_streams`, `write_otlp`, `write_trace_v2_fingerprint`,
`write_admin_pages`, `write_cur_csv` (schema-true per SPEC §19.4).

**Build.** Teams and plants exactly per SPEC §18 table (platform, payments, search, mobile, infra, data, ops on
Bedrock with CUR, ci-bots with execution files and truncation, agents with fingerprinted SDK lanes, core
control, tiny), plus the cross-team extras (refusal fallbacks with declined outputs 0 and 6, placeholder usage,
hidden compactions, one unknown-model call), the reference Anthropic cost report (15% discount, a Priority
bucket, provisional days relative to `today` = window end + 25 days) and the CUR file (10% discount). Truth by
per-plant closed forms at generation time (e.g. payments: for each generated 5–60-minute gap the generator knows
`E`, the appended tokens and the write/read rates, so the 1h saving is summed exactly as in Appendix A.1).
Canonical records generated directly; `source_files` generated by the writers from the same records (CANARY
planted in every content field of the transcript sample and headless streams). Scale mode streams in bounded
memory. Everything byte-deterministic per seed.

**Acceptance tests.**
- `generate(seed=7)` is byte-identical across two processes (subprocess test hashing all files); plants and
  truth present for every team; the control team's lanes produce no miss events under `core.transitions`; the
  tiny team has 3 principals; subscription teams carry `billing_path="subscription"`; the ops team is on
  channel `bedrock` with regional scope;
- each closed form reproduces its SPEC Appendix counterpart on a one-lane version of the plant (A.1 for
  payments, A.5 for mobile, A.10 for agents' edit churn, A.11 for ci-bots truncation);
- written files parse as their formats (JSONL/JSON/CSV) and contain the documented fields; transcripts and
  headless streams contain CANARY; no other output does;
- scale mode yields 10⁶ requests in ≤ 60 s with bounded memory (marker `perf`);
- gate `test_gate_files_through_adapters.py` (`importorskip` CC, TELEM, TRACE, ADMIN adapters): every written
  source file read by the real adapter reproduces the canonical records' per-team token totals (and CUR/admin
  totals the cost lines); gate `test_gate_truth_vs_oracle.py` (`importorskip("tokenbill.synth.oracle")`):
  each replay-based plant truth equals `ReferenceReplay` on the plant's lanes within 1 nano.

**Size.** ~2.9k LOC including tests.

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

### DETECT-CACHE — Cache detectors (wave 2)

**Goal.** The eight content-free cache detector classes that name each cache-waste mechanism, its billed
cost, its recoverable dollars (through the replayer with the linked lever) and a deployable fix (SPEC §10.1,
the `cache.*` rows of §10.2, D11, D14, D21, D26, D28).

**Owns.** `tokenbill/detect/{cache_miss,cache_ttl,cache_structure}.py`, `tests/v2/detect_cache/**`.

**Consumes.** `core.transitions`, `core.cache_rules.effort_change_keeps_cache`, `core.findings` (helpers),
`core.policy` (`parse_policy`), `core.catalog` (`levers_for_kind`, `ALLOWLIST`), `core.types` (`Finding`,
`Fix`, `Scope`, `EvidenceItem`, `AnalysisContext`, `Policy`), `core.labels`, `core.money`, `core.evidence`,
`core.protocols` (`Detector`), `core.registry.run_detectors`, `core.builders` (`lane_from_table`, builders),
`core.testing` (`FakePricer`, `FakeReplayer`, `assert_detector_conforms`).

**Provides.** Detector classes at the registry paths of SPEC §3.7: `detect.cache_miss.{MissByCause,
SwitchChurn, RebuildEvents}`, `detect.cache_ttl.{ColdResume, TtlAdvisor}`, `detect.cache_structure.{
GatewayDisabled, UnreadWrite, ColdFanout}`.

**Build.** Every `cache.*` row of SPEC §10.2: trigger, `cost_observed` (EXACT only for billed arithmetic),
`recoverable` (`ctx.replayer.replay` with the linked policy on the cohort's lanes, or the stated formula; None
where no mechanical repair), kinds (incl. `plan-toggle`, `edit-churn` with the K* evidence, TTL advisor that
recommends only a policy different from the observed TTL and reports the keepalive break-even), fix text and
config patch (keys only from `core.catalog.ALLOWLIST`), lever ids from `core.catalog.levers_for_kind`,
references, `needs_eval`/`upper_bound`, audience rules, allowance-cohort labeling (basis LIST_EQUIVALENT,
"Allowance headroom:" titles), `min_usd` and other thresholds from `ctx.thresholds`, deterministic ids
(`core.findings.finding_id`) and ordering. Cross-lane logic stays inside `core.findings.cohort_key` cohorts
(shard invariance). Findings are returned unpublished.

**Acceptance tests.**
- one hand-computed fixture per kind (to the nano); cause precedence (compaction + idle gap → compaction;
  switch + gap → model-switch); Appendix A.9 thresholds; Appendix A.5 cold resume (exactly one event; $4.00 EXACT
  observed; $3.90 ESTIMATED premium); Appendix A.10 edit churn ($0.31 EXACT, $0.2576 ESTIMATED, K* 37.2) with
  `{"min_usd": "0.10"}`; an effort change on a Claude Code Opus 5.5 lane is not a miss cause, on an SDK lane it
  is (`effort-change` kind);
- TTL advisor with `FakeReplayer` recommends per the replayed values, emits `ttl-heterogeneous` with counts
  only when < 60% of ≥ k principals benefit, never proposes keepalive for Claude Code lanes, proposes it for an
  SDK lane with 7-minute gaps, adds the gateway note when the lane is behind a gateway, labels an allowance
  cohort's saving LIST_EQUIVALENT;
- gateway no-cache (a), beta-header-dropped (b) with a `policy.ttl.<team>` threshold, tool-search-disabled (c)
  via `attribution.extra["gateway"]`; write-never-read; oversized-ttl EXACT arithmetic; cold fan-out (upper
  bound); switch-churn sub-kinds incl. plan-toggle; compaction-cold;
- unmet `requires` → exactly one `missing-capabilities` finding via `run_detectors`; healthy-control lanes →
  no finding with recoverable ≥ `min_usd`; `assert_detector_conforms` for all 8 classes (incl. shard
  invariance); CANARY absent;
- gate (`importorskip("tokenbill.sim.usage_replay")`): with the real `UsageReplayer`, the Appendix A.1 lane
  yields `ttl-1h-recommended` with saving $1.1508; the A.2b lane with `{"min_usd": "0.10"}` yields
  `ttl-5m-recommended` with $0.318; the A.2 lane (already 5m) yields no TTL finding;
- gate (`importorskip` `tokenbill.synth.fleet` and the replayer): on `synth.fleet.generate()` the payments,
  platform, mobile and agents (keepalive, edit-churn) plants are recovered within their `FleetTruth`
  tolerances and the core team produces no cache finding ≥ `min_usd`.

**Size.** ~2.8k LOC including tests.

### DETECT-OTHER — Context, premium, model routing, failure, automation, tail detectors (wave 2)

**Goal.** The remaining ten usage-level detector classes: context size and compaction window, the harness
static prefix and tool-definition bloat, carry and config tax, premiums and sticky escalation, model routing
(delegation, same-tier, **default model, default effort**, effort mix, **rebaseline**), failure paths
(**incl. max-tokens truncation**), automation (**incl. CI run cost**) and the runaway tail (SPEC §10.1, the
non-cache rows of §10.2, D14, D26, D34).

**Owns.** `tokenbill/detect/{context,premium,model,failure,automation,tail}.py`, `tests/v2/detect_other/**`.

**Consumes.** `core.transitions`, `core.findings` (helpers incl. `fit_cpt`, `rate_nano`), `core.policy`,
`core.catalog` (`successor`, `retiring_within`, `levers_for_kind`, `ALLOWLIST`), `core.types`, `core.labels`,
`core.money`, `core.evidence` (tokenizer band, tool-search band, max-tokens and code-review benchmarks,
CPT defaults), `core.protocols` (`Detector`), `core.registry.run_detectors`, `core.builders`,
`core.testing` (`FakePricer`, `FakeReplayer`, `assert_detector_conforms`).

**Provides.** Detector classes at the registry paths of SPEC §3.7: `detect.context.{SizeTax,
CompactionWindow, StaticPrefix, Carry}`, `detect.premium.{PremiumModifiers, StickyEscalation}`,
`detect.model.Routing`, `detect.failure.FailurePath`, `detect.automation.Automation`, `detect.tail.Runaway`.

**Build.** Every non-cache row of SPEC §10.2 with the framework rules of §10.1 (cohort confinement, allowance
labeling, `min_usd` and thresholds from `ctx`, allowlisted config patches, deterministic ids): size-tax exact
decomposition; compaction-window grid with guards; static-prefix harness cost from `ctx.static_prefix_floor`
and tool-defs-bloat from fingerprints (reduction band 0.50–0.85, upper bound); carry/config tax with the cpt
fit; premiums EXACT on identical tokens; sticky escalation self vs org (count only when ≥ k); routing kinds
incl. `default-model` (replay `model=claude-sonnet-5@agent_product:claude_code,lane_kind:main` with band,
trade-off, `needs_eval`) and `default-effort` (replay `effort=medium,scale=0.5@…`), same-tier via
`core.catalog.successor` only, `rebaseline` (dominant-model change detection, 14-day before/after means,
tokenizer-family note, thinking-default note, stale-prompt flag at +20% output per call, behavioral, no
recoverable); failure kinds incl. `max-tokens-truncation` (billing rule documented; retry detection within 120
s and `T ≥ 0.95·T_trunc`); automation kinds incl. `ci-run-cost` (per repo/workflow, benchmark with source,
CI vs interactive $); runaway with cohort statistics.

**Acceptance tests.**
- one hand-computed fixture per kind (to the nano); premium detector on SPEC §6.9 case 4 vs case 1 yields an
  EXACT fast premium of $0.068 with `{"min_usd": "0.01"}`; Appendix A.11 truncation ($0.71376 EXACT,
  $0.53532 ESTIMATED upper bound) with `{"min_usd": "0.10"}`;
- size-tax exact decomposition; compaction-window curve with the extra-compactions guard (via `FakeReplayer`);
  static-prefix info with S known and absent; tool-defs-bloat range on a 14k-token non-deferred tools tier
  and none when tools are deferred; carry and config-tax (first listing only; cpt fit with ≥ 30 samples else
  defaults); sticky escalation (self vs org count ≥ k); routing: delegation with band, same-tier exact
  arithmetic labeled ESTIMATED and no same-tier finding for Opus 4.8 (no successor), default-model and
  default-effort on Claude Code main lanes only, rebaseline Δ% on a synthetic migration; failure path (each
  sub-kind, incl. SDK attempts from recorder hooks counting toward retry storms); automation (each sub-kind);
  runaway (break-glass semantics: team only unless `ctx.break_glass`);
- allowance cohort: size-tax figures carry LIST_EQUIVALENT and "Allowance headroom:" titles;
- unmet `requires` → exactly one `missing-capabilities` finding via `run_detectors`; healthy-control lanes → no
  finding with recoverable ≥ `min_usd`; `assert_detector_conforms` for all 10 classes (incl. shard
  invariance); CANARY absent;
- gate (`importorskip` `tokenbill.synth.fleet` and `tokenbill.sim.usage_replay`): on `synth.fleet.generate()`
  the search, infra, data (routing, default-model, default-effort, rebaseline), ops, ci-bots (automation,
  truncation) and agents (tool-defs-bloat) plants are recovered within tolerance and the core team produces no
  finding ≥ `min_usd`.

**Size.** ~3.0k LOC including tests.

### PLAN — Action plan, realization intervals, policy packs, hook, effectiveness (wave 2)

**Goal.** Turn findings into a ranked, overlap-aware plan (Shapley on a sample scaled to one full-scope joint
replay, realization-rate intervals, a headline that never sums standalone ceilings, allowance headroom kept
apart) and deployable, per-cohort, never-auto-applied patch packs with a SessionStart hook and post-rollout
effectiveness checks (SPEC §11, D15, D16, D26, D30, D34).

**Owns.** `tokenbill/plan/{action_plan,realization,policy_pack,litellm,effectiveness}.py`,
`tokenbill/plan/templates/**` (incl. `tokenbill_session_start.py`), `tests/v2/plan/**`.

**Consumes.** `core.catalog` (`LEVERS`, `LeverDef`, `ALLOWLIST`, `allowed`, `lever`), `core.policy`
(`parse_policy`, `combine`, `lane_matches`, `to_spec`), `core.shapley` (`shapley_exact`, `shapley_mc`,
`scale_credits`), `core.shards` (`stratified_sample`, `merge_replay`), `core.types` (`Finding`, `ActionPlan`,
`LeverResult`, `PolicyPack`, `PolicyEntry`, `Policy`, `AnalysisContext`, `ContractOverlay`, `LaneIndexRow`,
`ShardKey`), `core.labels`, `core.evidence`, `core.protocols` (`Replayer`), `core.builders`, `core.testing`
(`FakeReplayer`). `rates.contract.to_model_pricing` is used only through the injected `model_pricing_emitter`.

**Provides.** `plan.action_plan.build_action_plan(...)` (SPEC §11.2 signature); `plan.realization.PRIORS`,
`project(...)`; `plan.policy_pack.build_policy_packs(...)`, `render_pack(...)`; `plan.litellm.
render_injection_points(...)`, `validate_litellm_fragment(...)`; `plan.effectiveness.check_effect(...)`.

**Build.** Exactly §11: candidates; billed and allowance classes planned separately; grid choice on a seeded
stratified sample (`core.shards.stratified_sample`); interaction groups; Shapley on the sample (exact k ≤ 6,
MC above with SE); one full-scope joint replay via `map_shards` + `merge_replay` and `scale_credits` so Σφ
equals it; monthly normalization; priors (rate 1/1/1; cache 0.8/0.9/1.0; trajectory −0.2/0.5/1.0; behavioral
not projected); headline Σ φ·RR with comonotone range and calibration inheritance; `allowance_headroom_monthly`
apart; observed RR beside priors when ≥ 3 receipts; trade-offs (incl. `cc.default_model`,
`cc.default_effort`) only with `include_tradeoffs`. Packs: allowlist enforcement (unknown key raises; VERIFY
keys only as README comments), RFC 7386 merge patch vs `current`, rollback patch, `autoCompactWindow` + env
pairing, OTEL arm/wave tags, per-cohort packs, org-wide rollout note pointing to `measure plan --design its`,
LiteLLM fragment, hook template (stdlib only; threshold; frequency caps; never blocks; exit 0), README.
Effectiveness checks for TTL, autocompact, default model and effort levers.

**Facts to verify.** Nothing beyond `core.facts` (the allowlist's verified flags come from there); report any
key you believe is wrong via a contract-change note.

**Acceptance tests.**
- Shapley on the Appendix A.7 value function via `FakeReplayer` (credits 8/18/4; Σ = joint); scaling to a
  larger full-scope joint saving preserves Σφ to the nano and the group proportions; disjoint groups add;
  k = 7 uses MC with SE; standalone sums never appear in any `ActionPlan` field; an in-memory `load_lanes`
  with 1 vs 4 shards gives identical plans;
- realization: a trajectory lever's projection range crosses zero and is labeled; headline = Σ φ·RR(p50) with
  p10/p90; behavioral levers absent from the headline; trade-offs excluded unless requested; allowance levers
  appear only in `allowance_headroom_monthly`; observed RR shown after 3 receipts;
- packs: compaction lever → JSON patch with `autoCompactWindow` and `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`; TTL
  lever → `promptCacheTtl` only as a README comment while its value format is unverified in facts.json (and in
  JSON once verified); `cc.default_model` → `model` as a README comment (VERIFY) plus `availableModels` only
  with `include_tradeoffs`; merge patch against `current` keeps unrelated keys, and applying the rollback patch
  restores the original; unknown key raises; per-cohort packs for a heterogeneity finding; OTEL tags present;
  LiteLLM fragment passes the mini-parser;
- hook (subprocess, temporary HOME): expired + $3.20 → JSON `systemMessage` containing "$3.20"; below
  threshold → no output; malformed input → no output, exit 0; frequency caps enforced;
- effectiveness: 1h write share 95% → no finding; 40% → `setting-not-effective`; autocompact median check;
  default-model share check;
- CANARY absent from packs; no network;
- gate (`importorskip("tokenbill.sim.usage_replay")`): with the real replayer on the Appendix A.1 lane, the TTL
  lever's Shapley credit equals its standalone saving; gate (`importorskip("tokenbill.synth.fleet")`): the
  payments team's TTL lever Shapley credit is within ±5% of `FleetTruth`.

**Size.** ~2.5k LOC including tests.

### OUT — Renderers, FOCUS, SARIF, ccusage, showback, allocation, workload (wave 2)

**Goal.** Every output an enterprise consumes, with labels travelling with every number, allowance kept out
of billed columns, and privacy enforced at the rendering boundary: result@2 JSON, terminal (every `RunResult`
slot), single-file HTML, FOCUS 1.4 CSV (per-channel BilledCost rule), SARIF, ccusage JSON, team showback (incl.
per-billing-path list-equivalent distribution), plus the allocation-rules engine and workload classifier (SPEC
§14, R3, R4, R10, D17, D19, D26).

**Owns.** `tokenbill/outputs/{result_json,terminal,html,focus,sarif,ccusage,showback}.py`,
`tokenbill/finops/{allocation,workload}.py`, `tests/v2/outputs/**`, `tests/v2/finops/**`.

**Consumes.** `core.types` (`RunResult` and every slot type, `BillSummary`, `LedgerCostRow`, `ClusterDay`,
`CheckResult`, `PublishedAggregate`), `core.labels`, `core.money`, `core.textsafe`, `core.records`,
`core.kanon.publish`, `core.facts` (FOCUS columns, benchmark anchors with sources), `core.builders`,
`core.testing` (`published_for_tests` for renderer-only tests).

**Provides.** The renderer API of SPEC §14.7 exactly; `finops.allocation.load_rules`, `apply_rules`,
`coverage`; `finops.workload.classify`.

**Build.** §14.1 schema rule (no floats anywhere; every object with an integer leaf has `evidence`; money
only as MONEY objects; `list_equivalent` never under billed keys; `--deterministic` ordering); terminal ≤ 100
columns with label chips and a section for every `RunResult` slot (bill with allowance line, data quality,
calibration, reconciliation per channel, findings, plan with allowance headroom apart, packs, replays, measure
plan, measurements, ab, check, pricing); HTML with CSP, no scripts, table twins with captions, WCAG 2.2 AA
contrast in both themes, size budget; FOCUS columns from facts.json and the `x_` columns of §14.4, per-channel
BilledCost honesty rule (`reconciled_channels`, `channels` filter), allowance rows with `BilledCost = 0`,
`--role enrichment`, chargeback coverage gate, k-anonymous merging via `core.kanon.publish` with
`x_SuppressedUsers`; SARIF 2.1.0 rules; ccusage daily/monthly (numbers rendered from decimal strings, never
Python floats); showback from published aggregates only, with anchors and benchmarks shown with sources and the
per-billing-path list-equivalent distribution; allocation rules and workload classifier per §14.6. Renderers
accept only `PublishedAggregate` for grouped data and only `Figure.is_billed_eligible` values in billed columns
(raise `ContractViolation` otherwise).

**Facts to verify.** FOCUS 1.4 columns come from facts.json; ccusage JSON field names (checklist §19.8 #13).

**Acceptance tests.**
- `validate_result_json` rejects a float, an integer object without `evidence`, a money value not in MONEY
  form, and a `list_equivalent` figure under `bill.exact`; a full `RunResult` fixture with every slot filled
  round-trips deterministically;
- a billed column fed an ESTIMATED or LIST_EQUIVALENT Figure raises; unpriced renders as "unpriced (N
  inferences)"; the terminal renders each slot type (snapshot tests);
- HTML: no external URL, CSP meta present, no `<script>`, every `<svg>` chart followed by a `<table>` with
  `<caption>`, no pseudonym strings; terminal sanitizes ANSI in names;
- FOCUS: required columns present; every `x_` name matches `^x_[A-Z][A-Za-z0-9]{1,48}$`; totals equal the input
  `LedgerCostRow` sums per day and team; a channel not in `reconciled_channels` → refusal unless
  `allow_unreconciled` (then `x_Reconciled=false`) or excluded by `channels`; allowance rows have `BilledCost =
  0` and `x_PriceBasis = list_equivalent`; `role="enrichment"` zeroes Billed/Effective cost; chargeback below
  95% coverage refused; rows under k merged with `x_SuppressedUsers`;
- SARIF has the required 2.1.0 keys and one result per violation; ccusage output parses and matches the
  documented field names; showback contains no individual identifiers, shows the anchors with sources and the
  billing-path distribution only for groups with ≥ k users;
- allocation: first-match semantics, proportional splits sum to the original, `(unallocated)` line, coverage
  KPI; workload classifier ≥ 95% accuracy on a labeled fixture set; CANARY absent from every renderer output;
- gate (`importorskip("tokenbill.store.db")`): FOCUS totals equal `SqliteStore.cost_rows` sums on a small
  ledger; showback built from a real `aggregate` + `core.kanon.publish`.

**Size.** ~3.0k LOC including tests.

### WIRING — Config and shared pipeline plumbing (wave 2)

**Goal.** The small shared layer both CLI packages build on, so they can run in parallel in wave 3: config
loading with precedence, the per-command `Env` (pricer, rules, keys), store opening, the adapter ingestion
loop, the shard mapper with a process pool, and the bill summary (SPEC §15 "Pipeline", §3.21, D30).

**Owns.** `tokenbill/config.py`, `tokenbill/pipeline/common.py`, `tests/v2/wiring/**`.

**Consumes.** `core.types`, `core.protocols`, `core.registry` (`get_adapter`, `sniff_adapter`, `load`),
`core.keys`, `core.ids`, `core.shards`, `core.kanon.publish`, `core.cache_rules.RulesTable`, `core.builders`,
`core.testing` (`FakePricer`, `MemoryStore`). Real `RateCard` and `SqliteStore` are loaded lazily by dotted path
(`tokenbill.rates.engine:RateCard`, `tokenbill.store.db:SqliteStore`) so this package tests with fakes.

**Provides.** `config.Config`, `config.load_config(path, environ, overrides) -> Config`;
`pipeline.common.Env`, `build_env(...)`, `open_store(...)`, `ingest_paths(...)`, `map_shards(...)`,
`bill_summary(...)` (SPEC §15 signatures).

**Build.** Config precedence CLI overrides > `TOKENBILL_*` env > JSON file (`./.tokenbill/config.json`, then
`~/.config/tokenbill/config.json`) > defaults; unknown keys → `UsageError`. `build_env` resolves keys
(`core.keys.load`; org key and collection key), the rate card (built-in + `--rates` + `--model-price` layers +
contract; injectable factory for tests) and the rules table. `ingest_paths` sniffs or selects adapters, builds
`IngestOptions` (identity mode, name/principal keys, team map, k, allowlist, window, renormalize), ingests,
and returns sources and DQ notes. `map_shards` runs a function per `ShardKey` sequentially or in a
`ProcessPoolExecutor` (workers open the store read-only by path), returning results in shard order.
`bill_summary` builds a `BillSummary` (exact/estimated/allowance totals, ESR via
`rates.engine.no_cache_equivalent_nano` when available, published breakdowns for arbitrary whitelisted
dimension lists, naive ratio from the CC `naive_usage` priced with the Env pricer).

**Acceptance tests.**
- config precedence (every layer), unknown key rejected, defaults per SPEC §15;
- `build_env` with an injected FakePricer factory; `ingest_paths` into `MemoryStore` with a fake adapter
  registered for the test; `map_shards` with jobs=1 and jobs=2 (a picklable top-level function) returns identical
  ordered results; `bill_summary` on MemoryStore publishes breakdowns (k enforced) and keeps allowance apart;
- gate (`importorskip` RATES engine and STORE db): `build_env` loads the real `RateCard`; `open_store` +
  `map_shards(jobs=2)` over a real `SqliteStore` equals jobs=1.

**Size.** ~1.3k LOC including tests.

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

### CLI-SAVINGS — Savings, verification and CI-gate verbs (wave 3)

**Goal.** Wire the savings side into the CLI: `scan` (local self view), `scan --org` (aggregate-only org
scan), `me`, `calibrate`, `findings` (sharded, pooled, re-scoped, Shapley-ranked), `whatif`, `policy` and
`policy check-effect`, `measure plan/run`, `ab`, `receipt`, `check` (CI gate), `report`, and `demo --fleet`
(SPEC §15, §15.2, §17, §18, D18, D30, D32).

**Owns.** `tokenbill/gate.py`, `tokenbill/pipeline/{savings,verification}.py`,
`tokenbill/commands/{scan,me,calibrate,findings,whatif,policy,measure,ab,receipt,check,report}.py`,
`tests/v2/cli_savings/**`.

**Consumes.** WIRING (`config`, `pipeline.common`: `Env`, `build_env`, `open_store`, `ingest_paths`,
`map_shards`, `bill_summary`), every wave-2 package through its documented API. Do not import
`pipeline.ledger` (CLI-LEDGER, same wave); `cli.py`'s command table (CLI-LEDGER) already lists your modules —
test your commands by building an argparse parser from your modules' `add_parser` functions, and leave full
`main()` tests to merge gate 2.

**Provides.** `pipeline.savings.run_calibrate`, `run_findings`, `run_whatif`, `run_policy`, `run_scan`,
`run_scan_org`, `run_report`, `run_demo_fleet`, `run_check`; `pipeline.verification.run_measure_plan`,
`run_measure_run`, `run_ab`, `run_receipt`; `gate.run_check(...) -> CheckResult` (SPEC §15.2); the command
modules listed above.

**Build.** `run_findings` exactly as SPEC §15: `lane_index` → `plan_shards` (config `shard_max_requests`) →
static-prefix floor from `lane_first_reads` → calibrate (two streaming passes over shards) → per-shard
`run_detectors(emit_missing=False)` through `map_shards` (`--jobs`) → once `run_detectors([],
emit_missing=True)` plus `aggregate.org-scan` on the store's aggregates/cost lines → `merge_findings` →
`rescope_findings(count_users=…store.count_users…)` → `build_action_plan` (sample + full-scope scaling,
billed/allowance apart) → `put_findings`. `scan` = discover → CC adapter (install identity; `--billing-path`
default from config) → temporary `SqliteStore` → price → the `run_findings` path → render (self view; `me` is a
pure alias). `scan --org` = ADMIN adapters on the page/CUR/GCP files (or `--live` pull) → temporary store →
reconcile → org scan findings → render. `whatif` replays policies (documented/calibrated/both; optional
Shapley). `policy` builds packs with `model_pricing_emitter=rates.contract.to_model_pricing`. `measure plan`
(incl. `--org-wide` → ITS design), `measure run` (panel at the baseline card; DiD/CUPED/ITS; guards with the
per-channel reconciliation), `ab`, `receipt create/sign/verify` (refusals → exit 3). `check` per §15.2 with
SARIF. `report` renders HTML for fleet/team/self. `demo --fleet` = `synth.fleet.generate` → ingest source
files → full pipeline → report, marked synthetic, keyless, networkless.

**Acceptance tests.**
- `scan` on CC's fixture tree prints the exact bill with label chips, the allowance line when the billing path
  is subscription, data-quality lines (naive ratio), calibration status and top recoverable levers;
  `--format json` validates with `validate_result_json`; `me` output equals `scan --self`;
- `scan --org` on ADMIN's recorded pages alone (no transcripts) prints a reconciled bill and org-scan
  findings; `--live` without `--admin-key-env` → exit 2;
- `findings` on a store built from `synth.fleet.generate()` equals across `--jobs 1` and `--jobs 2` and across
  shard caps of 250k and 1 (byte-identical JSON); the tiny team never appears; `--group-by principal` without
  `--self` → exit 2; `--break-glass` writes an audit row;
- `calibrate --require-pass` exit 3 when failing; `whatif --policy ttl=1h@lane_kind:main` reports saving with
  label and calibration; `policy -o DIR` writes the pack files; `measure plan --org-wide` yields an ITS design;
  `receipt sign` of an ESTIMATED receipt → exit 3;
- `check`: demo `timestamp` vs `well-behaved` baseline → exit 3 with `TB-NEW-BREAKER` (volatile-system) and
  `TB-CACHE-SHARE` in SARIF; `well-behaved` vs itself → exit 0; fewer than 3 runs → cost check skipped with a
  warning;
- `demo --fleet` completes keyless in < 60 s, labels output synthetic, and its JSON is byte-identical across two
  processes;
- perf (marker `perf`, nightly): `findings` + plan over a 10⁶-request synthetic store ≤ 8 min single process,
  peak RSS ≤ 1.5 GB, 2× requests ≤ 2.3× time; `--jobs 4` ≥ 2.5× faster on a 4-core runner.

**Size.** ~2.9k LOC including tests.

---

## 4. Integration step (wave 4, after merge gate 2)

Owned files: `tokenbill/__init__.py`, `README.md`, `DESIGN.md`, `CHANGELOG.md`, `CONTRIBUTING.md`,
`SECURITY.md`, `Makefile`, `docs/**`, `.github/**` (except `.github/workflows/ownership.yml`),
`scripts/{sbom.py,check_zero_deps.py,repro_check.sh,perf_gates.py}`, `tests/v2/e2e/**`.

1. **Version**: bump `__version__` to `0.2.0`; resolve any merge-gate contract-change notes with the contract
   owner.
2. **End-to-end tests** (`tests/v2/e2e/`, each a separate file so failures localize):
   `test_fleet_flagship.py` (SPEC §18: every plant within tolerance, control clean, per-channel reconciliation
   via suggested contracts 0.85 / 0.90, allowance never billed, k-anonymity, canary absent, byte determinism
   across two processes and across `--jobs 1/2`, `demo --fleet` < 60 s); `test_plan_recovery.py` (payments TTL
   lever Shapley within ±5%, headline range, allowance headroom apart); `test_verification_panels.py` (stepped
   wedge 25% effect; ITS org-wide change, MEASURED only); `test_receipts_e2e.py` (measure → receipt → sign →
   verify, `needs_ssh_keygen`); `test_canary_e2e.py` (every output: SQLite bytes, trace@2, result JSON, HTML,
   terminal, FOCUS, SARIF, ccusage, showback, policy packs); `test_no_egress.py` (the whole CLI except `--live`
   paths under the socket guard); `test_dual_engine_cli.py` (analyze routing on the demo corpus);
   `test_local_corpus.py` (marker `local_corpus`, release gate SPEC Appendix G.2 #2); perf gates at full size
   (nightly) via `scripts/perf_gates.py`.
3. **Supply chain** (SPEC §8.10): SHA-pinned actions, top-level `permissions: {}`, Dependabot, zizmor, CodeQL,
   Scorecard workflows; CI matrix Linux/macOS × 3.10–3.13 plus Windows × 3.12 running the full suite; per-package
   coverage report; zero-runtime-dependency check; stdlib CycloneDX SBOM with zero runtime components; build
   provenance (`actions/attest-build-provenance`), PEP 740 attestations, reproducible-build rebuild-and-diff;
   weekly `pricing verify --live` job that opens an issue on drift and 14 days before a promotion's end; release
   workflow kept on Trusted Publishing.
4. **Docs**: move the v0.1 contract to `docs/SPEC-v0.1.md` and install SPEC-v0.2 as `docs/SPEC.md`;
   `docs/PRIVACY.md` (§8.11 DPIA + one works-council template + employee notice), `docs/FLEET.md` (MDM collector
   rollout, identity modes and key custody, the CI post-step with `actions/upload-artifact` + `tokenbill collect
   claude-code-headless --in "$RUNNER_TEMP/claude-execution-output.json" --attr workload=ci`, Windows directory
   restrictions, the CUR 2.0 CSV export with an Athena query, the GCP billing-export query, the reference
   OpenTelemetry Collector config with `redaction` + `file` exporter `format: json`, `compression: none`, and
   the fleet-scale runbook with `--jobs`), `docs/VERIFY.md` (verifying releases and receipts; ITS vs randomized
   designs), `docs/THREAT-MODEL.md`; rewrite README (fleet quick start, `scan --org` first-day path, labels and
   honesty rules incl. list-equivalent allowance; **rewrite "Related work"**: provider tools are no longer "silent
   on why" — Claude Code `/usage` names likely causes and Anthropic and OpenAI ship cache diagnostics; position
   Token Bill as the fleet-wide, reconciled, verified layer); DESIGN.md sections for the ledger, gates, replay
   semantics, shards and threats to validity; CHANGELOG 0.2.0; SECURITY.md CVD timeline; CONTRIBUTING
   (ownership, gates, contract owner).
5. **Demo refresh**: regenerate README terminal excerpts from `tokenbill demo --fleet` (synthetic, labeled);
   keep the v0.1 demo excerpt unchanged.
6. **Release gates** (SPEC Appendix G.2 = PLAN §1.6): real redacted admin page pair added (or the release notes state the
   `dq.recon_schema_unverified` limitation), local-corpus test run on a maintainer machine, `pricing verify`
   offline clean, VERIFY items listed; full suite on the matrix, perf gates, ownership check; adversarial
   review hand-off.
