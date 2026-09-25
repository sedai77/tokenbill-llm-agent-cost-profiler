### CP-DET-LANES — `copilot.lanes` and the generic detectors on Copilot lanes (wave 2)

**Goal.** For developers whose Copilot usage is collected per request (VS Code `agent-traces.db` extracts and
OTel first — the adopting developers use VS Code and IntelliJ — then managed OTel, CLI events / store, gh-aw
runs), name the request-level causes of credit spend that billing data cannot show —
long-context bands, compactions, static tool/instruction overhead, subagent share, uncapped CI runs — and
prove that the generic SPEC detectors run correctly on Copilot lanes with Copilot fixes and without
duplicating Copilot kinds. Read SPEC §3.15, §3.22, §10.1–§10.2; addendum DC14, §3.6 (CA-31 … CA-33), §6.2 #4,
§9.3, §10.0, §10.3, §10.4, §19.3 #24–#26, Appendix C.G5, G5b, G11; rulings R-E16, R-E20.

**Owns.** `tokenbill/detect/copilot_lanes.py` (`CopilotLanes`, id `copilot.lanes`),
`tests/v2/copilot_det_lanes/**`.

**Consumes.** `core.findings` (`cohort_key` — the unchanged 3-tuple, in which billing class `pool` already
isolates Copilot lanes —, `product_family`, `build_finding`, `rate_nano`, `min_usd_gate`),
`core.transitions`, `core.catalog` (`fix_for`, `FAMILY_EXCLUSIONS`, `levers_for_kind(…, family="copilot")`),
`core.evidence`
(`TOOL_SEARCH_REDUCTION_BAND`), `core.labels`, `core.types`, `core.records`, `core.protocols.Pricer`,
`core.testing` (`FakePricer`, `assert_detector_conforms`, `FakeReplayer`), `core.builders`.

**Provides.** A registered lane detector (`families={"copilot"}`, **`requires={"credits"}`** — every Copilot
lane source declares `credits`, while `usage_sequence` exists only for VS Code traces and the experimental CLI
store, so the revision-2 `requires={"usage_sequence"}` switched `compaction-cost` and `ci-uncapped` off for
the default events-only CLI and gh-aw lanes —, `extension="copilot"`) with kinds `long-context-band`,
`compaction-cost`, `static-overhead`, `subagent-share`, `ci-uncapped`; gate tests for the generic detectors on
Copilot lanes.

**Build.**
1. `long-context-band`: premium = price at the resolved rates − price at default-tier rates on identical tokens
   through the pricer; EXACT when hypothesis A alone decided, ESTIMATED range when the lane's context tier made
   A and B disagree; recoverable ESTIMATED `upper_bound`; lever `copilot.context_default` with reach (CLI in
   trusted directories).
2. `compaction-cost` by `copilot_trigger` (forced share); `static-overhead` from COMPACTION event static
   tokens (else the static-prefix floor), carry at the lane's read / write mix, reduction band ESTIMATED
   `upper_bound`, lever `copilot.mcp_trim`; `subagent-share`; `ci-uncapped` over CI lanes (CLI `--ci` lanes
   and gh-aw lanes): spend per session p50/p90 and the uncapped share; gh-aw runs compared with their cap.
3. Per-kind input checks instead of one capability gate: `long-context-band` needs requests with a serving
   inference carrying input tokens (VS Code, OTel, store); `static-overhead` needs COMPACTION static-token
   attrs or a static-prefix floor; `compaction-cost` needs COMPACTION inferences or events (events-only CLI
   qualifies); `ci-uncapped` needs `workload_class=ci` lanes (CLI `--ci`, gh-aw); `subagent-share` needs
   SUBAGENT lanes. A lane lacking a kind's input is skipped for that kind only.
4. Pool-cohort labels (LIST_EQUIVALENT, "Copilot credits:" prefix) and R-E20 bases come from `build_finding`
   (lane findings carry no pool conversion: `recoverable` stays LIST_EQUIVALENT, and the aggregate plan does
   the invoice conversion); category `lever` for the three lever kinds, `aggregate` for `subagent-share` and
   `ci-uncapped` (k-anonymity from `COUNT_SOURCE` = `requests`); shard invariance within `cohort_key`.

**Acceptance tests.**
- C.G5 request (300,000 input, tier unknown) → premium 300,000,000 nano EXACT (660,000,000 band − 360,000,000
  default on identical tokens); C.G5b request (270,000 input, `context_tier=long_context`) → premium
  ESTIMATED [0; 285,000,000].
- An events-only CLI lane (capabilities without `usage_sequence`) and a gh-aw lane still yield `compaction-cost`
  and `ci-uncapped`; a VS Code extract lane yields `long-context-band`.
- Forced compactions counted and priced exactly; a lane with 30k tool-definition tokens → `static-overhead`
  range containing the hand value; CLI CI sessions without `credit_limit_nano` counted; gh-aw lanes compared
  with the 1,000 AIC default cap.
- Claude Code lanes of the same team are never included (family filter); `assert_detector_conforms` (shard
  invariance).
- Generic detectors on Copilot lanes (unit, with FakeReplayer / real core): `cache.miss-by-cause` model-switch
  finding carries the Copilot fix text and target `github-copilot`, no `CLAUDE_CODE_*` key; `ttl-expiry`
  appears only with a known τ; `premium.modifiers fast-premium`, `automation ci-run-cost` and
  `context.static-prefix static-prefix` produce **no** finding on Copilot lanes (FAMILY_EXCLUSIONS) while their
  Copilot replacements do; `cache.ttl-advisor` never runs on Copilot lanes.
- Gate (`importorskip` DETECT-CACHE / DETECT-OTHER / REPLAY): the CP-SYNTH agents team through the real
  detectors and replayer → `cache.miss-by-cause model-switch` recovered; excluded kinds absent; control clean.

**Size.** ~1.45k LOC including tests.
