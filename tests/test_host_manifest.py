from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import write_runtime_manifest_fixture
from remctl_host_manifest import (
    RuntimeIdentityValidator,
    RuntimeManifestError,
    runtime_manifest_bytes,
    sha256_digest_bytes,
)


class RuntimeIdentityValidatorTests(unittest.TestCase):
    def test_validate_accepts_strict_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path, manifest_digest, _, _ = write_runtime_manifest_fixture(
                Path(tmpdir)
            )
            identity = RuntimeIdentityValidator(
                manifest_path,
                manifest_digest,
            ).validate()
        self.assertEqual(identity.role, "read-only-capability-host")
        self.assertEqual(identity.manifest_digest, manifest_digest)
        self.assertEqual(identity.runtime_files[0].key, "hostManifest")

    def test_validate_rejects_tampered_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path, manifest_digest, _, _ = write_runtime_manifest_fixture(
                Path(tmpdir)
            )
            validator = RuntimeIdentityValidator(manifest_path, manifest_digest)
            manifest_path.write_text('{"tampered":true}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeManifestError, "manifest digest mismatch"):
                validator.validate()

    def test_validate_rejects_runtime_file_tamper_after_construction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path, manifest_digest, _, files = write_runtime_manifest_fixture(
                Path(tmpdir)
            )
            validator = RuntimeIdentityValidator(manifest_path, manifest_digest)
            validator.validate()
            files["host_manifest"].write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeManifestError, r"runtimeFiles\[0\] digest mismatch"):
                validator.validate()

    def test_validate_rejects_manifest_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            manifest_path, manifest_digest, _, _ = write_runtime_manifest_fixture(tmp_path)
            symlink_path = tmp_path / "manifest-link.json"
            symlink_path.symlink_to(manifest_path)
            validator = RuntimeIdentityValidator(symlink_path, manifest_digest)
            with self.assertRaisesRegex(RuntimeManifestError, "manifest must not be a symlink"):
                validator.validate()

    def test_validate_rejects_unknown_manifest_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path, _, payload, _ = write_runtime_manifest_fixture(Path(tmpdir))
            payload["extra"] = True
            manifest_path.write_bytes(runtime_manifest_bytes(payload))
            manifest_digest = sha256_digest_bytes(manifest_path.read_bytes())
            validator = RuntimeIdentityValidator(manifest_path, manifest_digest)
            with self.assertRaisesRegex(RuntimeManifestError, "unknown fields"):
                validator.validate()

    def test_validate_rejects_wrong_identity_fields(self):
        cases = (
            ("role", "writer"),
            ("bundleIdentifier", "example.invalid.bundle"),
            ("cliVersion", "9.9.9"),
            ("protocolVersion", 999),
            ("schemaManifestVersion", 999),
            ("schemaManifestDigest", "0" * 64),
        )
        for field, value in cases:
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as tmpdir:
                    manifest_path, _, payload, _ = write_runtime_manifest_fixture(
                        Path(tmpdir)
                    )
                    payload[field] = value
                    manifest_path.write_bytes(runtime_manifest_bytes(payload))
                    manifest_digest = sha256_digest_bytes(manifest_path.read_bytes())
                    validator = RuntimeIdentityValidator(
                        manifest_path,
                        manifest_digest,
                    )
                    with self.assertRaises(RuntimeManifestError):
                        validator.validate()


if __name__ == "__main__":
    unittest.main()
