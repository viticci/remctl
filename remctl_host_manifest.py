"""Runtime identity manifest support for the RemCTL Capability Host."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import plistlib
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from remctl_host_protocol import (
    PROTOCOL_VERSION,
    SCHEMA_MANIFEST_DIGEST,
    SCHEMA_MANIFEST_VERSION,
)

RUNTIME_MANIFEST_VERSION = 1
HOST_ROLE = "read-only-capability-host"
EXPECTED_BUNDLE_IDENTIFIER = "net.macstories.remctl.capability-host"
EXPECTED_LAUNCH_AGENT_LABEL = "net.macstories.remctl.read-broker"
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_FILE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CLI_VERSION_RE = re.compile(r'^VERSION = "([^"\\n]+)"$', re.MULTILINE)
DEFAULT_RUNTIME_FILES: tuple[tuple[str, str], ...] = (
    ("remctl", "remctl"),
    ("hostManifest", "remctl_host_manifest.py"),
    ("hostOperations", "remctl_host_operations.py"),
    ("hostProtocol", "remctl_host_protocol.py"),
    ("images", "remctl_images.py"),
    ("runtime", "remctl_runtime.py"),
    ("serialization", "remctl_serialization.py"),
    ("smartLists", "remctl_smart_lists.py"),
)


class RuntimeManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManifestFileIdentity:
    path: str
    sha256: str


@dataclass(frozen=True)
class RuntimeFileIdentity(ManifestFileIdentity):
    key: str


@dataclass(frozen=True)
class ValidatedRuntimeIdentity:
    manifest_path: str
    manifest_digest: str
    runtime_manifest_version: int
    role: str
    bundle_identifier: str
    launch_agent_label: str
    cli_version: str
    host_version: str
    protocol_version: int
    schema_manifest_version: int
    schema_manifest_digest: str
    protected_python: ManifestFileIdentity
    broker_entrypoint: ManifestFileIdentity
    runtime_files: tuple[RuntimeFileIdentity, ...]

    def health_payload(self, implemented_operations: tuple[str, ...]) -> dict[str, Any]:
        return {
            "status": "ready",
            "role": self.role,
            "bundleIdentifier": self.bundle_identifier,
            "launchAgentLabel": self.launch_agent_label,
            "cliVersion": self.cli_version,
            "hostVersion": self.host_version,
            "runtimeManifestVersion": self.runtime_manifest_version,
            "manifestDigest": self.manifest_digest,
            "protocolVersion": self.protocol_version,
            "schemaManifestVersion": self.schema_manifest_version,
            "schemaManifestDigest": self.schema_manifest_digest,
            "protectedPythonSha256": self.protected_python.sha256,
            "brokerEntrypointSha256": self.broker_entrypoint.sha256,
            "runtimeFileDigests": [
                {"key": item.key, "sha256": item.sha256}
                for item in self.runtime_files
            ],
            "implementedOperations": list(implemented_operations),
        }


class RuntimeIdentityValidatorProtocol(Protocol):
    def validate(self) -> ValidatedRuntimeIdentity:
        """Return a fully validated runtime identity or raise."""


def _reject(message: str) -> RuntimeManifestError:
    return RuntimeManifestError(message)


def _normalize_digest(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise _reject(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _validated_absolute_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _reject(f"{label} must be a non-empty string")
    if value[0] != "/" or any(char in value for char in ("\x00", "\n", "\r")):
        raise _reject(f"{label} must be a safe absolute path")
    normalized_text = os.path.normpath(value)
    if not normalized_text.startswith("/"):
        raise _reject(f"{label} must resolve to an absolute path")
    return normalized_text


def _read_regular_file(path: Path, *, label: str, executable: bool = False) -> bytes:
    safe_path = Path(_validated_absolute_path(str(path), label=label))
    try:
        before = safe_path.lstat()
    except FileNotFoundError as exc:
        raise _reject(f"{label} does not exist") from exc
    if stat.S_ISLNK(before.st_mode):
        raise _reject(f"{label} must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise _reject(f"{label} must be a regular file")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(safe_path, flags)
    except OSError as exc:
        raise _reject(f"unable to open {label}") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode):
            raise _reject(f"{label} must be a regular file")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise _reject(f"{label} changed during validation")
        if executable and not (after.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
            raise _reject(f"{label} must be executable")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def sha256_digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_regular_file(path: Path, *, label: str, executable: bool = False) -> str:
    return sha256_digest_bytes(
        _read_regular_file(path, label=label, executable=executable)
    )


def runtime_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def manifest_digest(manifest: dict[str, Any]) -> str:
    return sha256_digest_bytes(runtime_manifest_bytes(manifest))


def _version_from_remctl_source(remctl_path: Path) -> str:
    source = _read_regular_file(remctl_path, label="runtime file remctl")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _reject("runtime file remctl must be UTF-8") from exc
    match = CLI_VERSION_RE.search(text)
    if match is None:
        raise _reject("unable to locate VERSION in remctl")
    return match.group(1)


def _host_version_from_info_plist(info_plist_path: Path) -> str:
    data = _read_regular_file(info_plist_path, label="capability host Info.plist")
    try:
        payload = plistlib.loads(data)
    except plistlib.InvalidFileException as exc:
        raise _reject("capability host Info.plist is invalid") from exc
    version = payload.get("CFBundleShortVersionString")
    if not isinstance(version, str) or not version:
        raise _reject("capability host Info.plist must define CFBundleShortVersionString")
    return version


def default_runtime_file_specs(root: Path) -> tuple[tuple[str, Path], ...]:
    root = Path(os.path.abspath(root))
    return tuple((key, root / relative_path) for key, relative_path in DEFAULT_RUNTIME_FILES)


def build_runtime_manifest(
    *,
    root: Path,
    protected_python: Path,
    broker_entrypoint: Path | None = None,
    host_version: str | None = None,
    runtime_files: tuple[tuple[str, Path], ...] | None = None,
) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    broker_path = (
        Path(os.path.abspath(broker_entrypoint))
        if broker_entrypoint is not None
        else root / "remctl_read_broker.py"
    )
    runtime_file_specs = runtime_files or default_runtime_file_specs(root)
    # B1/N3 build-time: every runtime file — not just remctl — must share the
    # sealed directory with the broker entrypoint.  A divergence would allow a
    # manifest to reference files outside the validated runtime tree.
    remctl_specs = [(k, p) for k, p in runtime_file_specs if k == "remctl"]
    if not remctl_specs:
        raise _reject("runtime_files must include a 'remctl' key")
    _, remctl_runtime_path = remctl_specs[0]
    broker_dir = Path(os.path.abspath(broker_path)).parent.resolve()
    if broker_dir != Path(os.path.abspath(remctl_runtime_path)).parent.resolve():
        raise _reject(
            "broker entrypoint and remctl runtime file must be in the same directory"
        )
    for key, path in runtime_file_specs:
        if Path(os.path.abspath(path)).parent.resolve() != broker_dir:
            raise _reject(
                f"runtime file '{key}' must be in the same sealed directory as broker/remctl"
            )
    cli_version = _version_from_remctl_source(remctl_runtime_path)
    resolved_host_version = host_version or _host_version_from_info_plist(
        root / "remctl-capability-host-Info.plist"
    )
    payload = {
        "runtimeManifestVersion": RUNTIME_MANIFEST_VERSION,
        "role": HOST_ROLE,
        "bundleIdentifier": EXPECTED_BUNDLE_IDENTIFIER,
        "launchAgentLabel": EXPECTED_LAUNCH_AGENT_LABEL,
        "cliVersion": cli_version,
        "hostVersion": resolved_host_version,
        "protocolVersion": PROTOCOL_VERSION,
        "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
        "protectedPython": {
            "path": _validated_absolute_path(str(protected_python), label="protectedPython.path"),
            "sha256": hash_regular_file(
                Path(protected_python),
                label="protectedPython.path",
                executable=True,
            ),
        },
        "brokerEntrypoint": {
            "path": _validated_absolute_path(str(broker_path), label="brokerEntrypoint.path"),
            "sha256": hash_regular_file(
                broker_path,
                label="brokerEntrypoint.path",
            ),
        },
        "runtimeFiles": [
            {
                "key": key,
                "path": _validated_absolute_path(str(path), label=f"runtimeFiles[{key}].path"),
                "sha256": hash_regular_file(path, label=f"runtimeFiles[{key}].path"),
            }
            for key, path in runtime_file_specs
        ],
    }
    return payload


class RuntimeIdentityValidator:
    def __init__(
        self,
        manifest_path: Path,
        expected_manifest_digest: str,
        *,
        expected_role: str = HOST_ROLE,
        expected_bundle_identifier: str = EXPECTED_BUNDLE_IDENTIFIER,
        expected_launch_agent_label: str = EXPECTED_LAUNCH_AGENT_LABEL,
    ):
        manifest_path = Path(manifest_path)
        if not manifest_path.is_absolute():
            raise _reject("manifest path must be absolute")
        self._manifest_path = manifest_path
        self._expected_manifest_digest = _normalize_digest(
            expected_manifest_digest,
            label="manifest digest",
        )
        self._expected_role = expected_role
        self._expected_bundle_identifier = expected_bundle_identifier
        self._expected_launch_agent_label = expected_launch_agent_label

    def validate(self) -> ValidatedRuntimeIdentity:
        manifest_bytes = _read_regular_file(
            self._manifest_path,
            label="manifest",
        )
        actual_manifest_digest = sha256_digest_bytes(manifest_bytes)
        if not hmac.compare_digest(actual_manifest_digest, self._expected_manifest_digest):
            raise _reject("manifest digest mismatch")
        try:
            payload = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _reject("manifest must be valid UTF-8 JSON") from exc
        return self._validate_payload(payload)

    def _validate_payload(self, payload: Any) -> ValidatedRuntimeIdentity:
        if not isinstance(payload, dict):
            raise _reject("manifest must be a JSON object")
        expected_keys = {
            "runtimeManifestVersion",
            "role",
            "bundleIdentifier",
            "launchAgentLabel",
            "cliVersion",
            "hostVersion",
            "protocolVersion",
            "schemaManifestVersion",
            "schemaManifestDigest",
            "protectedPython",
            "brokerEntrypoint",
            "runtimeFiles",
        }
        extras = set(payload).difference(expected_keys)
        missing = expected_keys.difference(payload)
        if extras:
            raise _reject(f"manifest contains unknown fields: {', '.join(sorted(extras))}")
        if missing:
            raise _reject(f"manifest is missing required fields: {', '.join(sorted(missing))}")

        runtime_manifest_version = self._require_exact_int(
            payload,
            "runtimeManifestVersion",
            RUNTIME_MANIFEST_VERSION,
        )
        role = self._require_exact_string(payload, "role", self._expected_role)
        bundle_identifier = self._require_exact_string(
            payload,
            "bundleIdentifier",
            self._expected_bundle_identifier,
        )
        launch_agent_label = self._require_exact_string(
            payload,
            "launchAgentLabel",
            self._expected_launch_agent_label,
        )
        cli_version = self._require_non_empty_string(payload, "cliVersion")
        host_version = self._require_non_empty_string(payload, "hostVersion")
        protocol_version = self._require_exact_int(
            payload,
            "protocolVersion",
            PROTOCOL_VERSION,
        )
        schema_manifest_version = self._require_exact_int(
            payload,
            "schemaManifestVersion",
            SCHEMA_MANIFEST_VERSION,
        )
        schema_manifest_digest = _normalize_digest(
            self._require_string(payload, "schemaManifestDigest"),
            label="schemaManifestDigest",
        )
        if not hmac.compare_digest(schema_manifest_digest, SCHEMA_MANIFEST_DIGEST):
            raise _reject("schema manifest digest mismatch")

        protected_python = self._validate_file_record(
            payload["protectedPython"],
            label="protectedPython",
            executable=True,
        )
        broker_entrypoint = self._validate_file_record(
            payload["brokerEntrypoint"],
            label="brokerEntrypoint",
        )
        runtime_files = self._validate_runtime_files(payload["runtimeFiles"])
        cli_runtime = next((item for item in runtime_files if item.key == "remctl"), None)
        if cli_runtime is None:
            raise _reject("runtimeFiles must declare the remctl runtime file")
        if cli_version != _version_from_remctl_source(Path(cli_runtime.path)):
            raise _reject("cliVersion mismatch")

        # B1/N3: every runtime file must share the sealed directory with the
        # broker entrypoint — not just remctl.
        broker_dir = Path(broker_entrypoint.path).parent
        if Path(cli_runtime.path).parent != broker_dir:
            raise _reject(
                "broker entrypoint and remctl runtime file must be in the same directory"
            )
        for item in runtime_files:
            if Path(item.path).parent != broker_dir:
                raise _reject(
                    f"runtime file '{item.key}' must be in the same sealed directory as broker/remctl"
                )

        all_paths = [
            protected_python.path,
            broker_entrypoint.path,
            *(item.path for item in runtime_files),
        ]
        if len(all_paths) != len(set(all_paths)):
            raise _reject("manifest must not declare duplicate runtime file paths")

        return ValidatedRuntimeIdentity(
            manifest_path=str(self._manifest_path),
            manifest_digest=self._expected_manifest_digest,
            runtime_manifest_version=runtime_manifest_version,
            role=role,
            bundle_identifier=bundle_identifier,
            launch_agent_label=launch_agent_label,
            cli_version=cli_version,
            host_version=host_version,
            protocol_version=protocol_version,
            schema_manifest_version=schema_manifest_version,
            schema_manifest_digest=schema_manifest_digest,
            protected_python=protected_python,
            broker_entrypoint=broker_entrypoint,
            runtime_files=runtime_files,
        )

    @staticmethod
    def _require_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise _reject(f"{key} must be a string")
        return value

    @classmethod
    def _require_non_empty_string(cls, payload: dict[str, Any], key: str) -> str:
        value = cls._require_string(payload, key)
        if not value or any(char in value for char in ("\x00", "\n", "\r")):
            raise _reject(f"{key} must be a non-empty safe string")
        return value

    @classmethod
    def _require_exact_string(cls, payload: dict[str, Any], key: str, expected: str) -> str:
        value = cls._require_string(payload, key)
        if value != expected:
            raise _reject(f"{key} mismatch")
        return value

    @staticmethod
    def _require_exact_int(payload: dict[str, Any], key: str, expected: int) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _reject(f"{key} must be an integer")
        if value != expected:
            raise _reject(f"{key} mismatch")
        return value

    def _validate_file_record(
        self,
        payload: Any,
        *,
        label: str,
        executable: bool = False,
    ) -> ManifestFileIdentity:
        if not isinstance(payload, dict):
            raise _reject(f"{label} must be an object")
        extras = set(payload).difference({"path", "sha256"})
        missing = {"path", "sha256"}.difference(payload)
        if extras:
            raise _reject(f"{label} contains unknown fields")
        if missing:
            raise _reject(f"{label} is missing required fields")
        return self._validate_path_digest_fields(
            payload["path"],
            payload["sha256"],
            label=label,
            executable=executable,
        )

    def _validate_path_digest_fields(
        self,
        path_value: Any,
        digest_value: Any,
        *,
        label: str,
        executable: bool = False,
    ) -> ManifestFileIdentity:
        path = _validated_absolute_path(path_value, label=f"{label}.path")
        digest = _normalize_digest(digest_value, label=f"{label}.sha256")
        actual_digest = hash_regular_file(
            Path(path),
            label=f"{label}.path",
            executable=executable,
        )
        if not hmac.compare_digest(actual_digest, digest):
            raise _reject(f"{label} digest mismatch")
        return ManifestFileIdentity(path=path, sha256=digest)

    def _validate_runtime_files(self, payload: Any) -> tuple[RuntimeFileIdentity, ...]:
        if not isinstance(payload, list) or not payload:
            raise _reject("runtimeFiles must be a non-empty array")
        validated: list[RuntimeFileIdentity] = []
        seen_keys: set[str] = set()
        for index, item in enumerate(payload):
            label = f"runtimeFiles[{index}]"
            if not isinstance(item, dict):
                raise _reject(f"{label} must be an object")
            extras = set(item).difference({"key", "path", "sha256"})
            missing = {"key", "path", "sha256"}.difference(item)
            if extras:
                raise _reject(f"{label} contains unknown fields")
            if missing:
                raise _reject(f"{label} is missing required fields")
            key = item["key"]
            if not isinstance(key, str) or not RUNTIME_FILE_KEY_RE.fullmatch(key):
                raise _reject(f"{label}.key is invalid")
            if key in seen_keys:
                raise _reject("runtimeFiles must not declare duplicate keys")
            seen_keys.add(key)
            record = self._validate_path_digest_fields(
                item["path"],
                item["sha256"],
                label=label,
            )
            validated.append(
                RuntimeFileIdentity(key=key, path=record.path, sha256=record.sha256)
            )
        validated.sort(key=lambda item: item.key)
        return tuple(validated)
