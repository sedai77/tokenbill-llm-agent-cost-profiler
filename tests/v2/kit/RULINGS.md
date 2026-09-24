# Contract-owner rulings (F-KIT)

The F-KIT agent is the contract owner for waves 2–3 (SPEC §21 #3, PLAN §1.4): it rules on disputed
gate tests and records every ruling here. During wave 1 the orchestrator holds that role; the entries
below marked **interpretation** are the readings F-KIT implemented where the SPEC is silent or
ambiguous, pending confirmation. Real gaps are raised separately as `CONTRACT-CHANGE-KIT-*.md`.

## Applied rulings (SPEC Appendix E)

- **R-E1** (`core.kanon`): data-quality findings — kinds `dq.*` and category `data-quality`, including
  the `run_detectors` missing-capabilities finding with empty scope and `n_users = 0` — are exempt from
  k-anonymity suppression and re-scoping. Implemented in `kanon._exempt`; tested in
  `test_kanon.py::test_exemptions_r_e1_self_and_aggregate`.

## Interpretations implemented in wave 1

**K-1 — `publish`: a merged row's `n_users` is the largest constituent count.** Rows of one aggregate
can share people (a developer appears in several model, bucket or day rows), so the only exact lower
bound on the distinct users of a union is the maximum child count — the same rule SPEC §8.4 states for
`rescope_findings` without `count_users`. Consequence: an `"(other: <k users)"` row made only of small
rows is always still below *k* and absorbs the smallest remaining row of its parent (complementary
suppression); a parent without any row of *k* users collapses one level up; what cannot reach *k* at
the top is withheld (`suppressed_rows` / `suppressed_users`). Published totals equal raw totals
whenever any row of the table has *k* users. See `CONTRACT-CHANGE-KIT-3.md`.

**K-2 — `rescope_findings` scope hierarchy.** Levels, finest first (`kanon.RESCOPE_LEVELS`): keep
{team, cost_center, lane_kind, billing_class} → {team, cost_center, billing_class} → {cost_center,
billing_class} → {billing_class}. `billing_class` is kept at every level so list-equivalent and billed
figures never add (R10). An org finding whose scope names `principal`, `session` or `session_key` is
re-scoped even at *k* or more users (break-glass session naming is applied by the caller after
publication). `self` findings pass through (the caller shows them only in the self view). Merging adds
the figures, sums events/lanes, unions lever ids and references, keeps the first fix, takes the lowest
confidence, and prefixes the summary with the re-scoping note. **Complementary suppression** (review
fix): when findings of the same detector and kind under the parent are already published, the merge
also absorbs the one at exactly the parent scope, else the smallest of them (fewest users, then lowest
cost), so the small children's figures never stand alone beside their published siblings under the
parent's larger user count. **Text scrubbing** (review fix): in a re-scoped finding the values of the
dropped scope dimensions are replaced by `"(other)"` in the title, summary, fix text, evidence
attributes and `validated_against` (whole tokens; values still in the parent scope are kept), so an
org-level finding never names the small team it came from. Provider-side `aggregate` findings
without person or API-key dimensions describe workspaces and models, not people (SPEC §8.5), and pass
through; an API-key-scoped one below *k* is re-scoped to its workspace. See `CONTRACT-CHANGE-KIT-3.md`.

**K-3 — `where` filters: the empty string matches a missing value.** `LedgerStore` filters are
`Mapping[str, str]`, so `MemoryStore` reads `""` as NULL (e.g. `{"team": ""}` = unattributed
requests). Proposed for `SqliteStore` and for `core.shards.shard_where(ShardKey(team=None))`; see
`CONTRACT-CHANGE-KIT-4.md`.

**K-4 — `cluster_days(since, until)` is half-open `[since, until)`**, like every other window.

**K-5 — `cost_rows`: dimensions not in `group_by`** are `""` in string fields and None in optional
fields; `rate_row_id` is set only when one row id covers the whole cell.

**K-6 — lever grids** are complete policy specs: clauses that carry a selector in `Policy` (`ttl`,
`keepalive`, `model`, `effort`) carry the lever's selector explicitly; the others (`compact-window`,
`cold-resume`, `fast`, `geo`, `regional`, `batch`, `repair`, `breakpoints`) carry none. "Subagent +
workflow agent" levers repeat the clause per lane kind (`ttl` and `model` are repeatable);
`model.same_tier_upgrade` is one grid value remapping every model with a successor at once;
`sdk.keepalive` covers `agent_product:agent_sdk` only (`Policy.keepalive` is not repeatable) — see
`CONTRACT-CHANGE-KIT-1.md`. Levers whose SPEC selector is "lanes with fingerprints" or "per model with
a successor" use `all`. `patch_keys` hold only `ALLOWLIST` keys (the acceptance test requires it);
snippet-delivered levers have none — see `CONTRACT-CHANGE-KIT-2.md`. `finding_kinds` link the §10
kinds named in each lever's fix or recoverable column. `upper_bound` is True for every trajectory
lever (§9.1 #4) and for levers whose §10 projection is stated as an upper bound: `fanout.stagger`
(cold-fanout), `ci.shared_prefix`, `cc.tool_search` and `sdk.defer_loading` (tool-defs-bloat).

**K-7 — lifecycle.** `RETIREMENTS` holds the retirement floors and the dates of already retired models;
`retiring_within` also returns dates already past. `promotion_for` matches `start ≤ date ≤
not_before_end` (inclusive: "at least through"). `SUCCESSORS` come from facts.json and hold only
same-tier, same-tokenizer pairs.

**K-8 — `map_sku`** matches a rule's pattern against the whole provider code (`fullmatch`) and returns
the first verified rule; unverified rules never map.

**K-9 — key files** hold 64 lowercase hex characters and a newline (`load` also accepts ≥ 32 raw
bytes; content made only of hex digits is always read as hex). On POSIX any group or other permission
bit (`mode & 0o077`) is refused with `PrivacyError`; the type/size/mode checks are repeated with
`fstat` on the descriptor actually read. `load_or_create` writes a private temporary file and
publishes it with a hard link (a concurrent creator's complete key wins; no reader sees a partial
key); without hard links it writes in place with `O_EXCL` and a losing creator retries short reads
for up to one second. `load` / `load_or_create` take keyword-only
`runner`, `platform` and `notes` (additive; used for the Windows ACL path, D44).

**K-10 — FakePricer conventions** (RATES decides its own; the parity test compares rows and
modifiers): multiply modifiers scale token buckets (per-request server-tool prices are not scaled by
them); a contract multiplier scales every bucket incl. per-request prices, while per-model contract
overrides are final prices; `multi_region` endpoints take the regional modifier; the long-context
band applies when `total_input` *exceeds* the threshold; a bucket the row does not price falls back
(other write ↔ 5m write, then input; reads → input). Unpriced reasons: "not priceable", "no rate row",
"unverified rate row", "model before effective date", "promotion expired" (`testing.UNPRICED_DQ` maps
them to data-quality codes).

**K-11 — store merge determinism** (`MemoryStore`, the executable specification of §7.3). The merged
ledger is a function of the set of contributions: the surviving request id is the smallest id among
contributions carrying a provider message id (else the smallest id); exact usage-set ties go to the
canonically smallest contribution ("keep the existing set" is order-dependent), and the lane, session
and sequence number travel with the winning usage set; a provider request id
seen with two message ids anywhere is never a join key. Pricing follows §7.2: each ingest prices
the contributions it stores with its own pricer (else the constructor's), a merged request uses the
pricer of its winning contribution, and `reprice(pricer, since_ms, until_ms)` re-prices exactly the
requests starting in its window; `purge` deletes every contribution merged into a purged request.
When a provider aggregate or cost line id is stored in several versions, the `final` one wins, then
the most recently fetched, then canonical order. `assert_store_conforms` keeps each collision
pair and split-entry pair inside one source, so an incremental store that pre-scans each batch for
collisions passes; it fixes the store's name key id up front through the factory so that h_ nulling
does not depend on ingest order. Factory convention: `factory(org_key=…, name_key_id=…, pricer=…)`.

**K-12 — `FakeReplayer` keys** are `policy.spec()`; before F-SEM's `core.policy` exists they fall back
to `policy.name` (`FakeReplayer.policy_key`). Its savings sit on the last request of each lane in
`outcomes`.

## Contract-owner grants for wave 1.5 (GitHub Copilot) — orchestrator, 2026-09-23

- **G-1 (O-1):** F-CORE-C is a contract-owner amendment run on F-CORE's files: its branch is checked with
  `scripts/check_ownership.py --package F-CORE`. F-KIT-C likewise runs on F-KIT's files (`--package F-KIT`),
  F-SEM-C on F-SEM's files (`--package F-SEM`). F-EXT and F-POOL are new owners added to `OWNERSHIP.toml` by
  F-CORE-C (C-32).
- **G-2 (O-4):** research-derived fixture inputs are copied by their OWNING packages at build time (the
  orchestrator does not commit files owned by packages that do not exist yet): CP-RATES copies
  `/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/raw/yml/*.yml` + `commits.txt` into `tests/v2/fixtures/copilot_rates/yml/`; CP-OTEL copies
  `/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/src/**/awf-v0.28.7-aic-token-usage.jsonl` into `tests/v2/fixtures/copilot_otel/gh_aw/` and
  `/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/inputs/vscode-otel-DDL.sql` into `tests/v2/fixtures/copilot_otel/vscode/DDL.sql`; CP-VSCODE copies the same
  DDL into `tests/v2/fixtures/copilot_vscode/DDL.sql`; CP-HANDOFF writes the activity-report provenance note from
  `/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/raw/_en_enterprise-cloud_latest_copilot_reference_metrics-data.md` into
  `tests/v2/fixtures/copilot_handoff/README.md`.
- **G-3 (R-E23):** TELEM and REPLAY started on the gate-F core; after gate F' they rebase and apply A-4 / A-6
  (recorded as `tests/v2/kit/CONTRACT-CHANGE-COPILOT-TELEM.md` / `-REPLAY.md`).

## Interpretations implemented in wave 1.5b (F-KIT-C, GitHub Copilot)

**KC-1 — separate Copilot tables.** `COPILOT_LEVERS`, `COPILOT_ALLOWLIST`, `COPILOT_PROMOTIONS` beside the
pinned SPEC tables. `lever()`, `allowed()` and `promotion_for()` search the SPEC table first, then the
Copilot one (addendum CA-35 / CA-36); `copilot_allowed()` / `copilot_promotion_for()` search only the
Copilot table. Levers without an aggregate grid (behavioural, `replay none` rows of §11.1) have
`replay="none"`; `selector` is `all` (cell levers touch no lane); `upper_bound` is True for trajectory
levers (SPEC §9.1 #4) and for `copilot.mcp_trim` (`static-overhead` is an upper bound).
`copilot.seat_reclaim_team` links no finding kind (the brief's acceptance test requires
`levers_for_kind("idle-seat", family="copilot")` to be the seat-reclaim lever alone; `idle-seat`
findings name it by id). `copilot.vscode_traces_optin` delivers through the brief's
`admin:vscode_db_exporter_optin` (the addendum's `admin:communicate_vscode_traces_optin` is not an id
of the brief). `copilot.telemetry_on`'s `copilot.managed.telemetry.*` is the eight telemetry keys.

**KC-2 — aggregate grammar.** `copilot:<param>=<value>[@<scope>]`; the canonical form always writes the
scope (`@all` included, as the addendum's grids do), so `to_aggregate_spec(parse_aggregate_spec(s)) ==
s` for canonical strings and `parse(to(x)) == x` always. Value domains: `auto`/`fast` on|off,
`auto_tier` efficiency|balance|intelligence, `seat_policy` assign_selected|disabled, `plan`
business|enterprise, `context_tier` default|long_context, `mcp` trim|off, `seats_idle`/`seats_team`
`<n>d`, `aw_cap` a positive int, `remap` a model id, `runner` a runner SKU known to `runner_rate`.
Scope values contain no whitespace, `@` or `;`; `entity:` takes `enterprise | org:<o> | cc:<n>`.
Per-team / per-org candidates (`@team:<t>`, `@org:<o>`) are built by CP-PLAN; the static grids use
`@all` (the seat-policy grid too).

**KC-3 — `copilot_allowance`.** `(credits per seat, promo label)`; the label is
`copilot.promo.<plan>.<YYYY-MM of promo_from>` (the facts carry no promotion id for the seat promo).
`unknown` / `mixed` raise `UsageError` (K-3 overrides CA-36's `ContractViolation`).

**KC-4 — `agent_family(agent_product)`** returns the product family (`copilot` for every `copilot_*`
agent product, else `default`), the vocabulary of `levers_for_kind`, `fix_for` and
`FAMILY_EXCLUSIONS`.

**KC-5 — `fix_for`** covers the §10.4 generic detectors with Copilot text **and** the Copilot
detectors' own kinds (§10.1–§10.3 fix columns): F-SEM-C's `build_finding` replaces the fix of every
`product=copilot` finding with `fix_for(…)` when one exists, so the Copilot detectors need entries too.
Generic kinds without a Copilot text (`failure.path`, `context.size-tax`, `cache.unread-write`,
`rebaseline`) return None (F-SEM-C then strips non-Copilot keys and appends "(no Copilot setting
known)").

**KC-6 — Copilot parent chain.** `COPILOT_RESCOPE_LEVELS`: first every dim outside the chain is dropped
(e.g. `lane_kind`, `workload`, `surface`, `sku`), then `team`, `bucket`, `plan`, `model`,
`cost_center`; the root keeps `product`, `entity`, `org`, `plan_scenario` **and `billing_class`** (so
pool and billed figures never add, as in SPEC's chain). Complementary suppression only absorbs peers
of the same chain. The R-E16 exemption set also admits `billing_class` (it names no people).

**KC-7 — `scope_counter`.** `entity`-source findings that R-E16 does not exempt (e.g. a team-scoped
`agentic-workflow-cost`) are counted over cost lines. A scope dim the source cannot filter (e.g. `plan`
on cost lines), an unknown source or a missing store counts 0 — an unknown count can only suppress
more. Licenses / activity: the largest per-record-store count (stores never share people across key
ids). `requests` counts call `ledger.count_users` without the `source` keyword (older stores).

**KC-8 — R-E10 note.** `PublishedAggregate` / `AggRow` have no notes field (frozen F-CORE types), so a
kept users-unknown row is recognisable by `n_users == 0` and `kanon.row_notes(row, group_by=…)`
returns `("users_unknown",)` for it; renderers print "users unknown". Person-proxy keys:
`principal`, `session`, `session_key`, `api_key_id`, `api_key`, `cwd_key`. `audience` ∈ {org, self}.

**KC-9 — R-E31.** A merged summary keeps the whole original summary whenever it fits beside the
re-scoping prefix (the prefix shortens to `[re-scoped for k-anonymity] ` or `[re-scoped] ` first);
otherwise whole trailing sentences are dropped (ending "…") while labelling sentences (list-equivalent,
not invoice, estimated, upper bound, unpriced, provider estimate, no mechanical fix, scenario) are always
kept; only if those alone overflow is the text cut at a word boundary. Summaries that fitted before are
byte-identical.

**KC-10 — FakePricer on Copilot.** Band hypothesis B applies only on channel `github_copilot`; a
disagreeing B widens **every** line (point A). Rows without a write price fold every write bucket (5m,
1h, other, unknown TTL) into input as zero-width ESTIMATED lines. A non-billable Copilot call is EXACT $0
even without a rate row (G12); elsewhere a missing row stays unpriced. Contracts never apply to the
Copilot paths nor to channel `github_copilot`. `rate_card_sha256` also covers the Copilot rows and
modifiers. The Copilot goldens run in `assert_pricer_conforms` only for a LIST card carrying the facts'
Copilot rows (a contract card skips them, like the SPEC cases).

**KC-11 — MemoryStore.** Adoption follows R-E21 (only `SourceInfo.adapter == "copilot-export"`, one
adopted key id, a second → `UsageError` before anything is stored). `meta()` always reports
`org_key_mode` (`own` | `adopted` | `none`), `adopted_key_id`, `adopted_name_key_id`. The ingest
counts gain `dq.principal_key_mismatch` (principals nulled); `dq.name_key_mismatch` keeps its wave-1
meaning (names + principals). Latest-fetch-wins (addendum §7.1) applies to Copilot records (cost lines
on `COPILOT_CHANNELS`, aggregates of Copilot source kinds or channels, `github.copilot_metrics`
outcomes); other records keep "final, then latest" (pinned by `test_memory_store.py`).

**KC-12 — MemoryRecordStore.** Accepted key ids are read from the ledger's `meta()` at every `put`
(`org_key_id`, `adopted_key_id`) plus an optional constructor key id. Person records under another key
id are skipped (counted); configuration rows are always stored (no person). The count-row fallback
applies only when the window holds no person row of that source: `seat_counts` are summed per entity
and snapshot day (they partition that entity's seats) and the largest sum wins; `activity_counts` give
their largest `n_people`. `date_from` / `date_to` are inclusive dates. `assert_record_store_conforms`
calls `factory(path)` or `factory(path, org_key)`; the store must accept `p_` values under
`key_id(RECORD_STORE_ORG_KEY)`.

**KC-13 — additional conformance helpers** (additive, for STORE A-2 and REPLAY A-6):
`assert_store_copilot_conforms(factory)` (factory also takes `adopt_key_ids`) and
`assert_replayer_conforms(…, pool=False)` (opt-in, so REPLAY's merged conformance test is unaffected
until it applies A-6).
