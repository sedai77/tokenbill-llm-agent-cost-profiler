### gateway.restore_caching — restore prompt caching behind a gateway

A proxy that drops `cache_control` breakpoints (or the `anthropic-beta` header) makes every prompt
uncached input. Checklist: (1) pass `cache_control` and `anthropic-beta` through unchanged; (2) for
LiteLLM, add `cache_control_injection_points` from `litellm-config.patch.yaml`; (3) re-run
`tokenbill findings` after a day of traffic and confirm the cache-read share recovers.
