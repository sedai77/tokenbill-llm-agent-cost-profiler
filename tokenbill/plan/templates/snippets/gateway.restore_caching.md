### gateway.restore_caching — restore prompt caching behind a gateway

A proxy can drop or rewrite the caching controls that its upstream provider needs, making every
prompt uncached input. Apply the route-specific guidance in the finding rather than copying one
provider's controls to another:

- Anthropic: preserve `cache_control` and, for the 1h TTL, `anthropic-beta`; LiteLLM can use
  `cache_control_injection_points`.
- OpenAI Responses: preserve `prompt_cache_options` and explicit
  `prompt_cache_breakpoint` content blocks when the route uses them.
- Amazon Bedrock: preserve the model/API-specific cache controls and inspect returned cache usage.

Re-run `tokenbill findings` after a day of traffic and confirm that cache reads, writes, and
uncached input move in the expected direction.
