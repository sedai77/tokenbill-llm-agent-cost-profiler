"""A hook module whose import fails with something other than ImportError (the host must let the
error propagate: only unavailable modules degrade to a data-quality note)."""

raise RuntimeError("fake_ext.broken is broken on purpose")
