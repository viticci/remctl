from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Without these, the CLI falls back to a built or installed helper and bridge,
# which write to real Reminders. The stubs refuse writes; tests mock the calls.
os.environ.setdefault("REMCTL_PRIVATE_PATH", str(ROOT / "tests" / "remctl-private-stub"))
os.environ.setdefault("REMCTL_BRIDGE_PATH", str(ROOT / "tests" / "remctl-bridge-stub"))


def load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
