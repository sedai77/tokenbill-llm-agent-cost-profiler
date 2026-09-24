# CONTRACT-CHANGE-KIT-4 — filtering on a missing value (`ShardKey(team=None)`)

**What.** `LedgerStore.iter_lanes / iter_requests / count_users` take `where: Mapping[str, str]`, and
`core.shards.shard_where(key) -> dict[str, str]` must express the shard of unattributed requests
(`ShardKey(team=None)`, SPEC §3.5 "None = unattributed requests"). A `str` value cannot be None.

**Current implementation.** `MemoryStore` treats the empty string as NULL: `{"team": ""}` selects
lanes/requests whose team is missing (RULINGS K-3).

**Proposal.** Rule that `""` in a `where` mapping means "is NULL" for every `LedgerStore`, and that
`shard_where(ShardKey(team=None, …))` returns `{"team": ""}`. Affected: F-SEM (`shard_where`), STORE
(`SqliteStore` filters), WIRING (shard mapping).
