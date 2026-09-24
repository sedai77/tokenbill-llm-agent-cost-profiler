### sdk.keepalive — keep an idle SDK agent's cache warm

While an SDK/API agent idles, re-send the cached prefix with `max_tokens` 0 every 240 s for up to
3,600 s (not with streaming, structured outputs, a forced `tool_choice` or thinking enabled); the
pings bill cache reads only. Break-even idle time is κ(w/r − 1) with κ = 240 s (w, r: the write and
read multipliers of the model). Never ping Claude Code sessions. Docs:
https://platform.claude.com/docs/en/build-with-claude/prompt-caching
