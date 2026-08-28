from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remctl_host_manifest import (
    EXPECTED_BUNDLE_IDENTIFIER,
    EXPECTED_LAUNCH_AGENT_LABEL,
    HOST_ROLE,
    PROTOCOL_VERSION,
    RUNTIME_MANIFEST_VERSION,
    SCHEMA_MANIFEST_DIGEST,
    SCHEMA_MANIFEST_VERSION,
    ManifestFileIdentity,
    RuntimeFileIdentity,
    ValidatedRuntimeIdentity,
    sha256_digest_bytes,
    runtime_manifest_bytes,
)


def load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_test_identity() -> ValidatedRuntimeIdentity:
    return ValidatedRuntimeIdentity(
        manifest_path="/fixtures/runtime-manifest.json",
        manifest_digest="a" * 64,
        runtime_manifest_version=RUNTIME_MANIFEST_VERSION,
        role=HOST_ROLE,
        bundle_identifier=EXPECTED_BUNDLE_IDENTIFIER,
        launch_agent_label=EXPECTED_LAUNCH_AGENT_LABEL,
        cli_version="test-cli",
        host_version="test-host",
        protocol_version=PROTOCOL_VERSION,
        schema_manifest_version=SCHEMA_MANIFEST_VERSION,
        schema_manifest_digest=SCHEMA_MANIFEST_DIGEST,
        protected_python=ManifestFileIdentity(
            path="/fixtures/protected-python",
            sha256="b" * 64,
        ),
        broker_entrypoint=ManifestFileIdentity(
            path="/fixtures/remctl_read_broker.py",
            sha256="c" * 64,
        ),
        runtime_files=(
            RuntimeFileIdentity(
                key="hostManifest",
                path="/fixtures/remctl_host_manifest.py",
                sha256="d" * 64,
            ),
            RuntimeFileIdentity(
                key="hostOperations",
                path="/fixtures/remctl_host_operations.py",
                sha256="e" * 64,
            ),
        ),
    )


class StaticIdentityValidator:
    def __init__(self, identity: ValidatedRuntimeIdentity | None = None):
        self.identity = identity or build_test_identity()

    def validate(self) -> ValidatedRuntimeIdentity:
        return self.identity


def write_runtime_manifest_fixture(
    directory: Path,
    *,
    host_version: str = "1.0.0",
) -> tuple[Path, str, dict[str, object], dict[str, Path]]:
    runtime_dir = directory / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "protected_python": runtime_dir / "python-protected",
        "broker_entrypoint": runtime_dir / "remctl_read_broker.py",
        "host_manifest": runtime_dir / "remctl_host_manifest.py",
        "host_operations": runtime_dir / "remctl_host_operations.py",
        "remctl_cli": runtime_dir / "remctl",
    }
    files["protected_python"].write_text("#!/usr/bin/env python3\nprint('python')\n", encoding="utf-8")
    files["protected_python"].chmod(0o700)
    files["broker_entrypoint"].write_text("print('broker')\n", encoding="utf-8")
    files["host_manifest"].write_text("print('manifest')\n", encoding="utf-8")
    files["host_operations"].write_text("print('ops')\n", encoding="utf-8")
    files["remctl_cli"].write_text('VERSION = "1.7.1"\n', encoding="utf-8")
    payload: dict[str, object] = {
        "runtimeManifestVersion": RUNTIME_MANIFEST_VERSION,
        "role": HOST_ROLE,
        "bundleIdentifier": EXPECTED_BUNDLE_IDENTIFIER,
        "launchAgentLabel": EXPECTED_LAUNCH_AGENT_LABEL,
        "cliVersion": "1.7.1",
        "hostVersion": host_version,
        "protocolVersion": PROTOCOL_VERSION,
        "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
        "protectedPython": {
            "path": str(files["protected_python"]),
            "sha256": sha256_digest_bytes(files["protected_python"].read_bytes()),
        },
        "brokerEntrypoint": {
            "path": str(files["broker_entrypoint"]),
            "sha256": sha256_digest_bytes(files["broker_entrypoint"].read_bytes()),
        },
        "runtimeFiles": [
            {
                "key": "hostManifest",
                "path": str(files["host_manifest"]),
                "sha256": sha256_digest_bytes(files["host_manifest"].read_bytes()),
            },
            {
                "key": "hostOperations",
                "path": str(files["host_operations"]),
                "sha256": sha256_digest_bytes(files["host_operations"].read_bytes()),
            },
            {
                "key": "remctl",
                "path": str(files["remctl_cli"]),
                "sha256": sha256_digest_bytes(files["remctl_cli"].read_bytes()),
            },
        ],
    }
    manifest_path = directory / "runtime-manifest.json"
    manifest_bytes = runtime_manifest_bytes(payload)
    manifest_path.write_bytes(manifest_bytes)
    return (
        manifest_path,
        sha256_digest_bytes(manifest_bytes),
        payload,
        files,
    )
