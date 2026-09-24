# CONTRACT-CHANGE-ADMIN-1 — gaps found while building the ADMIN adapters

Raised by package ADMIN (wave 2) per SPEC §21 #3. ADMIN implements against the current contract;
nothing here changes a field or a signature. Items (a)–(e) are requests for the contract owner.

## (a) Team-level Enterprise Analytics aggregates need their own source kinds (additive)

**What.** SPEC §5.11 aggregates the Enterprise `user_usage_report` / `user_cost_report` endpoints to
`(date, team)` "exactly like the CC Analytics adapter", but §3.2 lists only
`anthropic.enterprise_usage` (aggregates) and `anthropic.enterprise_cost` (cost lines). `CostLine`
has no `team` field, so team-level costs cannot be cost lines, and team-level token aggregates
under `anthropic.enterprise_usage` would be **added to the organization-level `usage_report`
aggregates** by any consumer that sums a source kind (RECON's rate-card check would then price
twice the provider tokens against one invoice).

**Implemented.** Team-level rows are `UsageAggregate`s of the source kinds
`anthropic.enterprise_team_usage` (tokens) and `anthropic.enterprise_team_cost` (zero usage,
`reported_cost_nano` = Σ `amount` on basis `invoice`, `list_cost_nano` = Σ `list_amount`, dims incl.
`team`, `cost_type`, `token_type`). This mirrors Claude Code Analytics, whose team-level rows also
have their own kind (`anthropic.cc_analytics`).

**Proposed.** Add both values to the `UsageAggregate.source_kind` comment list in §3.2 and state in
§12 that team-level kinds are never summed with organization-level ones.

## (b) `CostLine` cannot carry some provider dimensions

`CostLine` has no `team`, `product` or `context_window`. Consequences (documented, not bugs):
CUR cost lines are per principal (`p_`) and the principal's **team** is only on the token
aggregates; Enterprise cost rows that differ only by `product` (chat vs Claude Code) and Admin cost
rows that differ only by `context_window` are summed into one line. **Proposed** (v0.3): an optional
sorted `dims: tuple[tuple[str, str], ...] = ()` on `CostLine`, like `UsageAggregate.dims`.

## (c) One stored version per `(date, team, source_kind)` / per id vs. paginated pulls

The store keeps one `OutcomeAggregate` per `(date, team, source_kind)` and one aggregate per
`agg_id` (the newest/final version). k-anonymity must see all users of a day at once. If the
per-user pages of one day are split across files and each file is ingested separately, each file
is rolled up (and k-suppressed) on its own users, and the store then keeps only one file's rows.
**Implemented:** every ADMIN adapter accepts a **directory** and reads all its pages in one pass.
**Proposed:** RECON's `pull` writes all pages of one endpoint and window into one JSONL file (or one
directory), and the CLI (`ingest`, `scan --org`) passes that file/directory to the adapter as one
source.

## (d) CUR model identity

Every CUR SKU rule in `facts.json` has `model: null` ("the model comes from the product
(servicename / line_item_product_code), not the usage type"). With no product → model table in the
contract, CUR token aggregates and cost lines carry no model even once rules are verified, so
RECON can only reconcile Bedrock in channel-total mode. **Proposed:** a `facts.json` table keyed by
the CUR product (e.g. `product_servicename` / marketplace product code) → model, consumed through
`core.catalog`, verified against a real CUR 2.0 export (SPEC §19.8 #17).

## (e) Workspace hashing flag

SPEC §5.1 hashes workspace ids only "with `--hash-workspaces`", but `IngestOptions` has no such
field and the ADMIN brief requires workspaces `h_` under the name key. **Implemented:** workspace /
project / account ids are always `h_` unless listed in `opts.name_allowlist`. **Proposed:** either
add `hash_workspaces: bool = True` to `IngestOptions` or state the allowlist behavior in §5.1.

## (f) k-anonymity of model cells inside a published team (privacy hardening, review)

**What.** SPEC §5.11 applies `opts.k_anonymity` to `(date, team)` groups and then emits
`UsageAggregate` rows with dims `(team, model)`. A published team of ≥ k people can still contain
a model cell used by a single person (e.g. the only Sonnet user of a 6-person team), and that
cell is one person's daily token usage. `UsageAggregate` carries no user count, so `core.kanon`
cannot suppress it later (R-E10 publishes rows whose user count is unknown).
**Implemented.** SPEC as written (k per `(date, team)`), documented in the README.
**Proposed.** At ingest, fold `(team, model)` cells with fewer than k distinct users into the
team's cell without a `model` dim (totals unchanged; the tokens stay available for team-day
coverage), or add an optional `n_users` to `UsageAggregate` so `publish()` can suppress them.
