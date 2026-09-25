### fallback.credit — do not pay twice for refusal fallbacks

When a refused request is retried on a fallback model, use the provider's documented fallback
mechanism so the declined attempt is billed according to the refusal rules instead of re-sending
the full prompt yourself. VERIFY the exact request option against the provider documentation
before rollout.
