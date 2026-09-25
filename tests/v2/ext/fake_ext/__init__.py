"""Test-only channel extension ``fake`` (addendum §3.8): every ``ExtensionSpec`` hook lives in
:mod:`.hooks` (plus the command module :mod:`.commands` and the rate resource
``fake_rates.json``); the hooks record their calls so the host tests can check the arguments."""
