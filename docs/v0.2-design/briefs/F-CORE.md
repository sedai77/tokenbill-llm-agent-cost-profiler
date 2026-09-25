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
