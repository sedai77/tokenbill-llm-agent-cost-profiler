# CONTRACT-CHANGE-DETECT-CACHE-1 — per-kind capability requirements and non-decimal thresholds

Raised by DETECT-CACHE (wave 2) under SPEC §21 #3. Nothing here blocks the package: the code
implements the current contract with the interpretations below. The contract owner decides whether
to fold them into SPEC Appendix E.

## 1. `Detector.requires` cannot express per-kind or alternative requirements

SPEC §10.2 lists `cache.rebuild` as requiring "usage_sequence, timing, events (compaction-cold) /
attempts or events (edit-churn)". `core.protocols.Detector.requires` is one `frozenset[str]` that
`core.registry.run_detectors` checks with `requires <= ctx.capabilities`, so it cannot say "events
for one kind" or "attempts **or** events".

Implemented: `RebuildEvents.requires = {"usage_sequence", "timing"}`; the detector runs
compaction-cold only when `"events" in ctx.capabilities` and edit churn only when `"events"` or
`"attempts"` is present. A source without either yields no rebuild finding and no
missing-capabilities note for that kind.

Proposed (additive, optional): `Detector.requires_by_kind: Mapping[str, tuple[frozenset[str], ...]]`
(kind → alternatives, any one of which suffices), with `run_detectors` emitting one data-quality
note per kind that cannot run while the detector still runs its other kinds.

## 2. `ctx.thresholds` values that are not decimals

`AnalysisContext.thresholds` is documented as "detector overrides (decimal strings)" and
`core.findings.threshold` parses every value as a `Decimal`. The §10.2 trigger of
`cache.gateway-disabled` beta-header-dropped reads `thresholds["policy.ttl.<team>"]`, whose natural
value is a TTL (`"1h"`), not a decimal.

Implemented: the detector reads `ctx.thresholds.get("policy.ttl.<team>")` directly and accepts
`"1h"`, `"3600"` or `"3600s"`; it never passes that key to `core.findings.threshold`.

Proposed: document in §3.5 that `policy.*` keys carry policy values (TTL strings) and are read raw,
or move team policy into a dedicated `AnalysisContext.team_policy: Mapping[str, str]`.
