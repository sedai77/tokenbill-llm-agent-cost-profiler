### retry.single_owner — one retry layer, capped backoff

Let exactly one layer own retries (the SDK's `max_retries` or your own wrapper, not both), cap the
backoff, and honor `retry-after`. Stacked retry layers multiply attempts, and a retry that arrives
after the cache TTL rewrites the whole prompt.
