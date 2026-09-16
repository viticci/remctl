#!/usr/bin/env python3
"""Build RemCTL's exact, sourceless Capability Host Python archive."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
import tempfile
import zipfile
from importlib import _bootstrap_external
from pathlib import Path


SOURCE_MANIFEST = {
    "remctl_cli": "remctl",
    "remctl_runtime": "remctl_runtime.py",
    "remctl_images": "remctl_images.py",
    "remctl_serialization": "remctl_serialization.py",
    "remctl_smart_lists": "remctl_smart_lists.py",
    "remctl_broker": "remctl_broker.py",
    "remctl_capability_policy": "remctl_capability_policy.py",
    "remctl_capabilities": "remctl_capabilities.py",
    "remctl_mcp": "remctl_mcp.py",
}
DISCOVERED_MODULE_PATTERN = "remctl_*.py"
ARCHIVE_DESCRIPTOR = 198
ARCHIVE_FD_ENV = "REMCTL_CAPABILITY_ARCHIVE_FD"
HOST_ACTIVE_ENV = "REMCTL_CAPABILITY_HOST_ACTIVE"
HOST_APP_ENV = "REMCTL_CAPABILITY_HOST_APP"
HOST_CDHASH_ENV = "REMCTL_CAPABILITY_HOST_CDHASH"
RUNTIME_ENV = "REMCTL_CAPABILITY_RUNTIME"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest-output", type=Path)
    return parser


def _regular_source(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"Capability archive source is not a regular file: {path}")
    payload = path.read_bytes()
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Capability archive source is not UTF-8: {path}") from exc
    return payload


def _verify_source_manifest(root: Path) -> dict[str, bytes]:
    root_metadata = root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise ValueError("Capability archive source root must be a real directory")
    discovered = {path.name for path in root.glob(DISCOVERED_MODULE_PATTERN)}
    expected_discovered = {
        source for source in SOURCE_MANIFEST.values() if source.startswith("remctl_")
    }
    if discovered != expected_discovered:
        missing = sorted(expected_discovered - discovered)
        unexpected = sorted(discovered - expected_discovered)
        raise ValueError(
            "Capability archive module manifest is stale: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return {
        module: _regular_source(root / relative)
        for module, relative in SOURCE_MANIFEST.items()
    }


def _pyc_bytes(source: bytes | str, logical_name: str) -> bytes:
    text = source.decode("utf-8") if isinstance(source, bytes) else source
    code = compile(text, logical_name, "exec", dont_inherit=True, optimize=0)
    source_hash = importlib.util.source_hash(text.encode("utf-8"))
    return _bootstrap_external._code_to_hash_pyc(
        code,
        source_hash,
        checked=True,
    )


def _archive_main_source() -> str:
    version = tuple(sys.version_info[:3])
    cache_tag = sys.implementation.cache_tag
    magic = list(importlib.util.MAGIC_NUMBER)
    return f'''from __future__ import annotations

import importlib.util
import os
import re
import stat
import sys


ARCHIVE_DESCRIPTOR = {ARCHIVE_DESCRIPTOR!r}
ARCHIVE_FD_ENV = {ARCHIVE_FD_ENV!r}
BUILD_PYTHON_VERSION = {version!r}
BUILD_CACHE_TAG = {cache_tag!r}
BUILD_MAGIC_NUMBER = bytes({magic!r})
HOST_ACTIVE_ENV = {HOST_ACTIVE_ENV!r}
HOST_APP_ENV = {HOST_APP_ENV!r}
HOST_CDHASH_ENV = {HOST_CDHASH_ENV!r}
RUNTIME_ENV = {RUNTIME_ENV!r}


def _sealed_runtime() -> tuple[int, str]:
    if (
        tuple(sys.version_info[:3]) != BUILD_PYTHON_VERSION
        or sys.implementation.cache_tag != BUILD_CACHE_TAG
        or importlib.util.MAGIC_NUMBER != BUILD_MAGIC_NUMBER
    ):
        raise SystemExit("sealed capability archive Python runtime does not match")
    try:
        archive_fd = int(os.environ[ARCHIVE_FD_ENV])
        metadata = os.fstat(archive_fd)
    except (KeyError, TypeError, ValueError, OSError):
        raise SystemExit("sealed capability archive descriptor is unavailable") from None
    archive_path = f"/dev/fd/{{archive_fd}}"
    if (
        archive_fd != ARCHIVE_DESCRIPTOR
        or sys.argv[0] != archive_path
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 0
    ):
        raise SystemExit("sealed capability archive descriptor is invalid")
    if os.environ.get(HOST_ACTIVE_ENV) != "1":
        raise SystemExit("sealed capability host environment is incomplete")
    expected_cdhash = os.environ.get(HOST_CDHASH_ENV, "")
    if re.fullmatch(r"[0-9a-f]{{40}}", expected_cdhash) is None:
        raise SystemExit("sealed capability host identity is invalid")
    supplied_runtime = os.environ.get(RUNTIME_ENV, "")
    supplied_app = os.environ.get(HOST_APP_ENV, "")
    if not os.path.isabs(supplied_runtime) or not os.path.isabs(supplied_app):
        raise SystemExit("sealed capability host environment is incomplete")
    runtime = os.path.realpath(supplied_runtime)
    app = os.path.realpath(supplied_app)
    if runtime != supplied_runtime or app != supplied_app or os.getcwd() != runtime:
        raise SystemExit("sealed capability host paths are not canonical")
    resources = os.path.dirname(runtime)
    contents = os.path.dirname(resources)
    derived_app = os.path.dirname(contents)
    if (
        os.path.basename(runtime) != "CapabilityRuntime"
        or os.path.basename(resources) != "Resources"
        or os.path.basename(contents) != "Contents"
        or not app.endswith(".app")
        or app != derived_app
        or not os.path.isdir(runtime)
    ):
        raise SystemExit("sealed capability host app layout is invalid")
    helpers = os.path.join(runtime, "bin")
    for helper in ("remctl-bridge", "remctl-private"):
        path = os.path.join(helpers, helper)
        try:
            helper_metadata = os.lstat(path)
        except OSError:
            raise SystemExit("sealed native capability runtime is incomplete") from None
        if (
            not stat.S_ISREG(helper_metadata.st_mode)
            or stat.S_ISLNK(helper_metadata.st_mode)
            or not os.access(path, os.X_OK)
        ):
            raise SystemExit("sealed native capability runtime is invalid")
    os.environ.update({{
        "REMCTL_BRIDGE_PATH": os.path.join(helpers, "remctl-bridge"),
        "REMCTL_PRIVATE_PATH": os.path.join(helpers, "remctl-private"),
    }})
    return archive_fd, runtime


def main() -> int:
    _sealed_runtime()
    if len(sys.argv) < 2:
        raise SystemExit("sealed capability archive requires a mode")
    mode, arguments = sys.argv[1], sys.argv[2:]
    if mode == "service":
        if len(arguments) != 2 or arguments[0] != "--socket":
            raise SystemExit("sealed capability service arguments are invalid")
        from remctl_broker import server_main
        return int(server_main(arguments) or 0)
    if mode == "cli":
        import remctl_cli
        sys.argv = ["remctl", *arguments]
        result = remctl_cli.main()
        return int(result or 0)
    raise SystemExit("unknown sealed capability archive mode")


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _zip_entry(name: str, payload: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o444) << 16
    return info, payload


def build_archive(source_root: Path, output: Path) -> dict[str, object]:
    sources = _verify_source_manifest(source_root.resolve(strict=True))
    entries = [
        _zip_entry("__main__.pyc", _pyc_bytes(_archive_main_source(), "__main__.py"))
    ]
    entries.extend(
        _zip_entry(f"{module}.pyc", _pyc_bytes(source, f"{module}.py"))
        for module, source in sorted(sources.items())
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=False) as archive:
            for info, payload in entries:
                archive.writestr(info, payload)
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {
        "version": 1,
        "archiveSection": "__TEXT,__rctl_pyz",
        "pythonVersion": list(sys.version_info[:3]),
        "pythonCacheTag": sys.implementation.cache_tag,
        "modules": sorted(SOURCE_MANIFEST),
        "entries": sorted(info.filename for info, _ in entries),
        "sourceless": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_archive(args.source_root, args.output)
    if args.manifest_output is not None:
        args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
