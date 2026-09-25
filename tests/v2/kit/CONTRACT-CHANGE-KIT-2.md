# CONTRACT-CHANGE-KIT-2 — `LeverDef.patch_keys` vs snippet delivery

**What.** SPEC §3.20 says `patch_keys` holds "ALLOWLIST keys or snippet ids", while the F-KIT acceptance
test requires "every `patch_keys` entry is in `ALLOWLIST`", and SPEC §11.3 has the policy pack reject
any key outside `ALLOWLIST` (`UsageError`). SDK / gateway / CI snippets (`sdk.ttl`, `sdk.keepalive`,
`batch.eligible`, `fanout.stagger`, `retry.single_owner`, `fallback.credit`,
`gateway.restore_caching`, `ci.shared_prefix`, `blocks.breakpoints`, `sdk.defer_loading`) are not
settings keys.

**Current implementation.** `patch_keys` contains only `ALLOWLIST` keys; snippet-delivered levers have
`patch_keys = ()`. PLAN can derive the snippet from the lever id.

**Proposal (additive).** Add `LeverDef.snippets: tuple[str, ...] = ()` with stable snippet ids
(`sdk.cache_control_ttl`, `sdk.keepalive_daemon`, `sdk.message_batches`, `sdk.stagger_fanout`,
`sdk.retry_owner`, `api.fallback_credit_beta`, `litellm.cache_control_injection_points`,
`ci.shared_prefix`, `sdk.breakpoints`, `sdk.defer_loading`), keeping `patch_keys` ⊆ `ALLOWLIST`.
Affected: F-KIT (catalog data), PLAN (policy pack rendering).
