from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import load_module
from remctl_host_manifest import manifest_digest, runtime_manifest_bytes

builder = load_module(
    "build_host_runtime_manifest_script_test",
    "scripts/build_host_runtime_manifest.py",
)


class BuildHostRuntimeManifestTests(unittest.TestCase):
    def test_build_manifest_payload_is_deterministic(self):
        root = Path(__file__).resolve().parent.parent
        payload_one = builder.build_manifest_payload(
            root=root,
            protected_python=Path(sys.executable).resolve(),
            host_version="1.0.0",
        )
        payload_two = builder.build_manifest_payload(
            root=root,
            protected_python=Path(sys.executable).resolve(),
            host_version="1.0.0",
        )
        self.assertEqual(payload_one, payload_two)
        self.assertEqual(
            manifest_digest(payload_one),
            manifest_digest(payload_two),
        )

    def test_main_writes_manifest_and_reports_digest(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "runtime-manifest.json"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = builder.main(
                    [
                        "--python",
                        str(Path(sys.executable).resolve()),
                        "--root",
                        str(root),
                        "--host-version",
                        "1.0.0",
                        "--output",
                        str(output_path),
                    ]
                )
            written_bytes = output_path.read_bytes()
            written_payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["manifest"], str(output_path.resolve(strict=False)))
        self.assertEqual(payload["manifestDigest"], manifest_digest(written_payload))
        self.assertEqual(written_bytes, runtime_manifest_bytes(written_payload))


if __name__ == "__main__":
    unittest.main()
