# CONTRACT-CHANGE-BLOCK-1 — `block:` repair ids, their levers, cohort-scoped cache sharing and trace@1 lane inference

Raised by BLOCK (wave 2). Implemented against the current contract; nothing in `core/*` was edited.

## 1. The `block:` repair vocabulary is not part of the contract

**What.** SPEC §9.7 routes "repairs prefixed `block:`" to `BlockReplayer`, and `core.policy`
accepts any `block:[a-z0-9][a-z0-9_-]{0,63}` id (CONTRACT-CHANGE-F-SEM-1 §2), but no text names the
ids. The breaker table (§10.4) describes each repair in prose only.

**Current implementation** (`tokenbill.sim.block_replay.BLOCK_REPAIRS`; an unknown `block:` id is a
`UsageError`):

| id | repair (§10.4 "repair priced") | breaker kind |
|---|---|---|
| `block:volatile` | system blocks with volatile classes hash as their `h_norm` twin | `volatile-system` |
| `block:sort_keys` | every block hashes as `h_sorted` | `serialization-churn` |
| `block:tool_order` | tool definitions in first-seen order (per lane) | `tool-churn` (order) |
| `block:tool_superset` | every request sends the lane's constant tool superset (added tokens priced) | `tool-churn` (subset) |
| `block:pin_params` | tier salts pinned to the lane's first values | `param-churn` |
| `block:add_end` | one end-of-prompt breakpoint on requests with none | `missing-breakpoint` |
| `block:drop_unread` | drop the observed breakpoints `write-never-read` flags: entries never read, billed as written, not explained by fan-out, lookback overflow, a divergence of the next request, an idle gap past the TTL or a lane's final write (hindsight) | `write-never-read` |
| `block:stagger` | send one, await its first token, then the rest | `fanout` |

`lookback-overflow` and `breakpoint-placement` are priced with `breakpoints=every_15` and the
cheapest canonical placement.

**Proposal (additive).** List these ids in SPEC §9.7 (or a `core.policy.BLOCK_REPAIRS` tuple) so PLAN
and the CLI can validate and render them.

## 2. Levers for the block repairs (catalog, F-KIT)

**What.** `core.catalog.LEVERS` links only `breakpoint-placement`, `missing-breakpoint`,
`lookback-overflow`, `write-never-read` (`blocks.breakpoints`, grid `breakpoints=static_plus_end`,
`breakpoints=every_15`) and `fanout` (`fanout.stagger`, grid `repair=stagger_fanout`, replay
`usage`). `volatile-system`, `serialization-churn`, `tool-churn` and `param-churn` link no lever, so
PLAN cannot rank their (replay-priced) savings; `fanout.stagger` routes a block-level `fanout`
finding to the usage-level repair.

**Proposal (additive, data only).**

| lever_id | class | grid | replay | kinds |
|---|---|---|---|---|
| `blocks.stable_system` | cache_transform | `repair=block:volatile` | block | volatile-system |
| `blocks.sort_keys` | cache_transform | `repair=block:sort_keys` | block | serialization-churn |
| `blocks.tool_order` | cache_transform | `repair=block:tool_order`, `repair=block:tool_superset` | block | tool-churn |
| `blocks.pin_params` | cache_transform | `repair=block:pin_params` | block | param-churn |

and `repair=block:add_end`, `repair=block:drop_unread` in the `blocks.breakpoints` grid. Until then
BLOCK findings carry `lever_ids = core.catalog.levers_for_kind(kind)` (empty for the four kinds
above) and their `recoverable` is the standalone replay saving.

## 3. Cache sharing is modeled per `(team, lane_kind)` cohort

**What.** §9.7 #2 keys entries by `(cache_scope_key, model, chain hash)`, i.e. every lane of a
workspace shares entries. D30 and §10.1 require every cross-lane computation to stay inside a
cohort so sharded and unsharded runs are identical (`assert_detector_conforms` checks it; shards
are teams or `(team, lane_kind)`).

**Current implementation.** `BlockReplayer` and `BlockBreakers` simulate each `(team, lane_kind)`
group of lanes (billing class is uniform per replay; the detector also splits by billing class)
with its own entry table keyed `(cache scope, model, chain hash)`. A lane whose scope is
`"unknown"` never shares entries with another lane. Cross-team reuse of one workspace's cache (e.g.
two teams sharing a system prompt in one workspace) is therefore not modeled: the replay is
conservative there.

**Proposal.** Ratify the cohort scope in §9.7 #2 ("within a cohort, keyed by …").

## 4. trace@1 lane inference conflicts with the §10.4 dual-engine acceptance (TRACE, contract owner)

**What.** §5.5 infers lanes (`infer_lanes`, §5.8) when a trace@1 run "contains more than one
model or the fingerprints show non-prefix-extending successors", and §5.8 lets a request join an
existing lane only with "the same model and tools-tier hash; otherwise it opens a new lane". The
v0.1 `tool-churn` demo rotates the tool order every 4th call, so every rotation is a
non-prefix-extending successor whose tools-tier hash is new: read literally, `TraceV1Adapter`
splits the run into one lane per tool order, each lane has a constant order, and no lane can show
`tool-churn` — the §10.4 acceptance ("`tool-churn` → `tool-churn` (order) at the rotation calls")
becomes impossible. The breakers compare a request with its lane predecessor only (cross-lane
comparison would break the cohort/shard rules of §10.1).

The same rule has a second effect on real traffic: independent requests of one trace@1 run that
share a preamble (a batch job, the exp2b-a3 shape) join one lane by longest common prefix, and
each one then "rewrites" its predecessor's final turn — a false `history-rewrite` (the v0.1
false positive the codebase review reported as exp10). BLOCK's own exp2b-a3 fixture encodes the
requests as separate lanes, as the recorder's per-request lanes would.

**Current implementation.** BLOCK is unchanged; `test_gate_dual_engine.py` asserts one lane per
demo run and fails with a message pointing here if the adapter splits it (verified in scratch
with a stand-in adapter that keeps one lane per run: all six gate tests pass, also with
timestamps rounded differently from v0.1's).

**Proposal.** In `infer_lanes`, key lane membership on the model and the tools-tier **multiset**
(`h_sorted`, order-insensitive) rather than the tools-tier hash — or infer lanes in trace@1 only
when a run interleaves models; and open a new lane when a successor diverges from every open
lane's last request inside the messages tier at the final turn (a sibling, not a continuation).
