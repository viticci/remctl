"""Test-run isolation from the developer's real RemCTL environment.

RemCTL reads a config file and several environment variables that change how
commands behave. On a contributor's own Mac those are usually set to something
non-default -- a stored `accountScope`, a pinned `dbPath` -- which would
otherwise leak into the test run and produce failures that do not reproduce in
CI (or, worse, passes that hide a real break).

This pins every test process to an empty config directory and clears the
environment overrides, so the suite behaves the same on a stock checkout and on
a heavily configured machine.
"""

from __future__ import annotations

import os
import tempfile

_ISOLATED_CONFIG = tempfile.TemporaryDirectory(prefix="remctl-test-config-")

# Point config lookups at an empty directory for the whole session.
os.environ["REMCTL_CONFIG_DIR"] = _ISOLATED_CONFIG.name

# Drop anything that would steer store selection or account scope.
for _var in (
    "REMCTL_ACCOUNT_SCOPE",
    "REMCTL_DB",
    "REMCTL_STORE_DIR",
    "REMCTL_IMAGES",
    "REMCTL_IMAGE_MODE",
    "REMCTL_IMAGE_WIDTH",
):
    os.environ.pop(_var, None)
