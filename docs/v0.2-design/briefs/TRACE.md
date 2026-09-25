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


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-3** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
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



---
**ERRATA ROUTING (orchestrator):** read SPEC Appendix E.4 (rulings R-E24 … R-E33) — items addressed to TRACE are binding for you.
