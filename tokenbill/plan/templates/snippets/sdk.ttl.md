### sdk.ttl — cache TTL on SDK / API agents

Set the TTL on the prompt-cache breakpoints of the agent's requests (Messages API):

```json
{"cache_control": {"type": "ephemeral", "ttl": "{ttl}"}}
```

Use the value this plan chose (`{ttl}`). 1h writes cost more than 5m writes and pay off only when
idle gaps between calls often exceed five minutes. Docs:
https://platform.claude.com/docs/en/build-with-claude/prompt-caching
