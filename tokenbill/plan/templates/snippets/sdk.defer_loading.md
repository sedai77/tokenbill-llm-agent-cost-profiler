### sdk.defer_loading — defer large tool definitions

Mark rarely used tool definitions with `defer_loading: true` and let the model find them with tool
search, so they stop inflating every prompt. The projection is an upper bound (tool-defs-bloat
band).
