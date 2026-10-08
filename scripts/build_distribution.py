#!/usr/bin/env python3
"""Build an immutable RemCTL app with its matching Python, CLI and plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile

from build_capability_archive import SOURCE_MANIFEST
from local_signing import identity as local_identity, signing_keychain

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "RemCTL Capability Host.app"
INSTALLER_NAME = "Install RemCTL.app"
BUNDLE_ID = "net.macstories.remctl.capability-host"
MACH_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def sha(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def extract_runtime(archive: Path, destination: Path) -> Path:
    """Validate the complete archive before extracting; links stay within python/."""
    with tarfile.open(archive) as source:
        members = source.getmembers()
        links = {m.name.rstrip("/") for m in members if m.issym()}
        names = set()
        for member in members:
            name = member.name.rstrip("/")
            if (name in names or not (name == "python" or name.startswith("python/"))
                    or posixpath.normpath(name) != name or member.islnk()
                    or not (member.isdir() or member.isfile() or member.issym())
                    or any(name.startswith(link + "/") for link in links)):
                raise ValueError(f"Unsafe runtime archive member: {name}")
            names.add(name)
            if member.issym():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))
                if member.linkname.startswith("/") or not target.startswith("python/"):
                    raise ValueError(f"Runtime symlink escapes its directory: {name}")
        for member in members:
            path = destination / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.issym():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(member.linkname)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, path.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    return destination / "python"


def runtime_archive(architecture: str, cache: Path) -> tuple[Path, dict]:
    config = json.loads((ROOT / "scripts/python-runtime.json").read_text())
    pin = config["architectures"][architecture]
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / (pin["sha256"] + ".tar.gz")
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, suffix=".download", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            run("/usr/bin/curl", "--fail", "--location", "--silent", "--show-error", "--proto", "=https", "--tlsv1.2", pin["url"], "-o", temporary)
            if sha(temporary) != pin["sha256"]:
                raise ValueError("Downloaded Python runtime checksum does not match the pinned release")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if sha(archive) != pin["sha256"]:
        raise ValueError("Cached Python runtime checksum mismatch")
    return archive, config


def tree_manifest(root: Path) -> dict:
    entries = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Runtime symlink escapes bundle: {name}")
            entries[name] = {"type": "symlink", "target": os.readlink(path)}
        elif stat.S_ISDIR(mode):
            entries[name] = {"type": "directory"}
        elif stat.S_ISREG(mode):
            entries[name] = {"type": "file", "sha256": sha(path), "executable": bool(mode & 0o111)}
        else:
            raise ValueError(f"Unsupported runtime object: {name}")
    return entries


def sign(path: Path, signing: dict, release: bool, *, executable=False, entitlements=None):
    args = ["/usr/bin/codesign", "--force", "--sign", signing["identity"], "--timestamp" if release else "--timestamp=none"]
    if signing.get("keychain"):
        args += ["--keychain", signing["keychain"]]
    if executable:
        args += ["--options", "runtime"]
    if entitlements:
        args += ["--entitlements", str(entitlements)]
    with signing_keychain(signing.get("keychain")):
        run(*args, path, stdout=subprocess.DEVNULL)


def build_installer(output: Path, signing: dict, release: bool, target: str, version: str) -> Path:
    """The disk image's double-click entry point; see remctl-installer.swift."""
    app = output / INSTALLER_NAME
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "net.macstories.remctl.installer", "CFBundleName": "Install RemCTL",
        "CFBundleExecutable": "Install RemCTL", "CFBundleIconFile": "remctl", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version, "CFBundleVersion": version,
        "LSMinimumSystemVersion": "14.0", "LSUIElement": True,
    }))
    shutil.copy2(ROOT / "assets/remctl.icns", app / "Contents/Resources/remctl.icns")
    run("swiftc", "-target", target, "-O", "-framework", "AppKit", "-framework", "Foundation",
        ROOT / "remctl-installer.swift", "-o", app / "Contents/MacOS/Install RemCTL")
    sign(app, signing, release, executable=True)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
    return app


def build(output: Path, signing: dict, release: bool, cache: Path, architecture: str | None = None) -> Path:
    architecture = architecture or platform.machine()
    archive, config = runtime_archive(architecture, cache)
    output = output.absolute()
    if output.exists():
        raise ValueError(f"Output already exists; choose a new build directory: {output}")
    output.mkdir(parents=True)
    app = output / APP_NAME
    resources = app / "Contents/Resources"
    native = app / "Contents/MacOS"
    helpers = resources / "CapabilityRuntime/bin"
    client = resources / "Client"
    for directory in (native, helpers, client, app / "Contents/Frameworks"):
        directory.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="remctl-python-") as temporary:
        python_tree = extract_runtime(archive, Path(temporary))
        shutil.move(str(python_tree), app / "Contents/Resources/Python")
    runtime = app / "Contents/Resources/Python"
    python = runtime / "bin/python3.13"
    # pip, idle and config scripts are development tools with build-machine
    # shebangs. Ship only the interpreter entry points in this code directory.
    for path in (runtime / "bin").iterdir():
        if path.name not in {"python", "python3", "python3.13"}:
            path.unlink()
    # Bytecode must match the interpreter that is actually shipped.
    run(python, "-B", "-I", "-S", ROOT / "scripts/build_capability_archive.py", "--source-root", ROOT,
        "--output", output / "capability.pyz", "--manifest-output", output / "archive-manifest.json")
    info = plistlib.loads((ROOT / "remctl-capability-host-Info.plist").read_bytes())
    info["CFBundleShortVersionString"] = re.search(r'^VERSION = "([^"]+)"', (ROOT / "remctl").read_text(), re.M).group(1)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    shutil.copy2(ROOT / "assets/remctl.icns", resources / "remctl.icns")
    shutil.copy2(ROOT / "LICENSE", resources / "LICENSE.txt")
    shutil.copy2(ROOT / "ui/THIRD-PARTY-NOTICES.txt", resources / "UI-THIRD-PARTY-NOTICES.txt")
    target = architecture + "-apple-macosx14.0"
    run("swiftc", "-target", target, "-O", "-framework", "EventKit", "-framework", "Foundation", ROOT / "remctl-bridge.swift", "-o", helpers / "remctl-bridge")
    run("clang", "-arch", architecture, "-mmacosx-version-min=14.0", "-fobjc-arc", "-O", "-F/System/Library/PrivateFrameworks", "-framework", "Foundation", "-framework", "AppKit", "-framework", "ReminderKit", ROOT / "remctl-private.m", "-o", helpers / "remctl-private")
    run("swiftc", "-target", target, "-O", "-framework", "AppKit", "-framework", "Foundation", ROOT / "remctl-permissions.swift", "-o", client / "remctl-permissions")
    run("swiftc", "-target", target, ROOT / "scripts/render_list_badges.swift", "-o", client / "remctl-list-artwork")
    run(client / "remctl-list-artwork", ROOT / "remctl", client / "remctl-list-symbols.json")
    # Sign Mach-O leaves explicitly; --deep is reserved for verification.
    for path in sorted(runtime.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                magic = handle.read(4)
            if magic in MACH_MAGIC:
                sign(path, signing, release, executable=True)
    manifest = json.dumps(tree_manifest(runtime), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (resources / "python-manifest.json").write_bytes(manifest)
    runtime_id = hashlib.sha256(manifest).hexdigest()
    protected_python = f"/Library/RemCTL/Python/{runtime_id}/bin/python3.13"
    (resources / "remctl-capability-python-path").write_text(protected_python + "\n")
    (resources / "remctl-capability-host-socket-path").write_text("portable-user\n")
    for name in SOURCE_MANIFEST.values():
        shutil.copy2(ROOT / name, client / name)
    for name in ("remctl_workspace.html", "remctl_mcp_widget.html"):
        shutil.copy2(ROOT / name, client / name)
    for name in ("remctl-mcp-icon.png", "remctl-mcp-icon-512.png", "remctl-permissions-icon.png"):
        shutil.copy2(ROOT / "assets" / name, client / name)
    # The same file remains importable by existing MCP code and executable by a shell.
    source = (client / "remctl").read_text().split("\n", 1)[1]
    (client / "remctl").write_text('#!/bin/sh\n""":"\nexec ' + shlex.quote(protected_python) + ' -E -s -B "$0" "$@"\n":"""\n' + source)
    (client / "remctl").chmod(0o755)
    shutil.copytree(ROOT / "plugins", resources / "plugins")
    shutil.copytree(ROOT / ".agents/plugins", resources / ".agents/plugins")
    shutil.copy2(ROOT / "remctl-capability-host-launchagent.plist", resources / "launchagent.plist")
    (resources / "Distribution").mkdir()
    for name in ("install.sh", "uninstall.sh"):
        shutil.copy2(ROOT / name, resources / "Distribution" / name)
    (resources / "distribution.json").write_text(json.dumps({"format": 1, "version": info["CFBundleShortVersionString"], "architecture": architecture, "pythonVersion": config["version"], "pythonRuntimeID": runtime_id, "signing": "developer-id" if release else "local", "teamID": "4W35M4UN6R" if release else None}, sort_keys=True) + "\n")
    entitlements = output / "host-entitlements.plist"
    entitlements.write_bytes(plistlib.dumps({"com.apple.security.automation.apple-events": True}))
    for directory in (helpers, client):
        for path in directory.iterdir():
            if path.is_file():
                with path.open("rb") as handle:
                    magic = handle.read(4)
                if magic in MACH_MAGIC:
                    sign(path, signing, release, executable=True)
    for name in ("remctl-bridge", "remctl-private"):
        shutil.copy2(helpers / name, client / name)
    run("swiftc", "-target", target, "-O", "-framework", "Foundation", "-framework", "AppKit", "-framework", "CoreServices", "-framework", "EventKit", "-framework", "Security", "-framework", "CryptoKit",
        "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__rctl_pyz", "-Xlinker", output / "capability.pyz", ROOT / "remctl-capability-host.swift", "-o", native / "RemCTL Capability Host")
    sign(app, signing, release, executable=True, entitlements=entitlements)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    if release:
        run("/usr/bin/codesign", "--verify", "--strict", "-R", '=identifier "net.macstories.remctl.capability-host" and anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = "4W35M4UN6R"', app)
        build_installer(output, signing, release, target, info["CFBundleShortVersionString"])
    print(app)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--identity", help="Explicit Apple signing identity; omit for free local signing")
    parser.add_argument("--preserve-identity", help="Keep an existing installation's certificate")
    parser.add_argument("--arch", choices=("arm64", "x86_64"), default=platform.machine())
    parser.add_argument("--release", action="store_true", help="Require Developer ID signing for a notarizable release")
    parser.add_argument("--signing-directory", type=Path, default=Path.home() / "Library/Application Support/RemCTL Signing")
    parser.add_argument("--cache", type=Path, default=ROOT / ".build/downloads")
    args = parser.parse_args()
    if args.release and not args.identity:
        parser.error("--release requires a Developer ID Application identity")
    if args.identity:
        signing = {"identity": args.identity}
    elif args.preserve_identity:
        marker = args.signing_directory / "identity.json"
        if marker.is_file() and json.loads(marker.read_text()).get("identity") == args.preserve_identity:
            signing = local_identity(args.signing_directory)
        else:
            signing = {"identity": args.preserve_identity}
    else:
        signing = local_identity(args.signing_directory)
    build(args.output, signing, args.release, args.cache, args.arch)


if __name__ == "__main__":
    main()
