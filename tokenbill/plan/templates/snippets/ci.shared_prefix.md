### ci.shared_prefix — share the static prefix across CI runs

Keep the static prefix of CI agent calls (system prompt, tool definitions, repository context)
byte-identical across runs and put a cache breakpoint after it, so runs that start within the TTL
read it instead of writing it again.
