#!/usr/bin/env python3
"""Keep a private local code-signing identity without Apple account enrollment."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import subprocess
import tempfile


def run(*args: str) -> bytes:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Commands can contain the keychain password. Never include argv in errors.
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def private_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError(f"Expected an owner-only regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


@contextmanager
def signing_lock():
    """Serialize search-list access across checkouts and signing directories."""
    directory = Path.home() / "Library/Caches/RemCTL"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "signing.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("Signing lock must be an owner-only regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def signing_keychain(keychain: str | None):
    """Expose an isolated identity to codesign, then restore the search list."""
    if not keychain:
        yield
        return
    with signing_lock():
        command = ("/usr/bin/security", "list-keychains", "-d", "user")
        original = shlex.split(run(*command).decode())
        if keychain in original:
            yield
            return
        # --keychain restricts identity selection, but codesign still needs the
        # identity's keychain in the search list to resolve its certificate.
        try:
            run(*command, "-s", *original, keychain)
            yield
        finally:
            run(*command, "-s", *original)


def identity(directory: Path) -> dict[str, str]:
    """Create once, then fail closed if any part of the identity is lost."""
    # Identity creation also observes the search list and must not race signing
    # or another build creating the same identity for the first time.
    with signing_lock():
        return _identity(directory)


def _identity(directory: Path) -> dict[str, str]:
    directory = directory.expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if directory.resolve() != directory or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f"Signing directory must be canonical and owner-only: {directory}")
    marker = directory / "identity.json"
    password_file = directory / "password"
    keychain = directory / "identity.keychain-db"
    if marker.exists():
        value = json.loads(private_file(marker))
        password = private_file(password_file).decode()
        if not keychain.is_file() or keychain.is_symlink():
            raise ValueError("Local signing keychain is missing; restore it rather than changing the app identity")
        if value.get("keychain") != str(keychain) or len(value.get("identity", "")) != 40:
            raise ValueError("Invalid local signing identity")
        run("/usr/bin/security", "unlock-keychain", "-p", password, str(keychain))
        certificate = run("/usr/bin/security", "find-certificate", "-c", "RemCTL Local Development", "-p", str(keychain))
        actual = subprocess.run(["/usr/bin/openssl", "x509", "-outform", "DER"], input=certificate, capture_output=True, check=True).stdout
        if hashlib.sha1(actual).hexdigest().upper() != value["identity"]:
            raise ValueError("Local signing certificate changed; restore the preserved identity")
        return value
    if any(directory.iterdir()):
        raise ValueError(f"Incomplete signing identity at {directory}; restore it or inspect it before starting over")
    password = secrets.token_hex(32)
    search_list = run("/usr/bin/security", "list-keychains", "-d", "user")
    with tempfile.TemporaryDirectory(prefix="remctl-certificate-") as temporary:
        root = Path(temporary)
        key, certificate, archive = (root / name for name in ("key.pem", "certificate.pem", "identity.p12"))
        run("/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(certificate), "-days", "3650",
            "-subj", "/CN=RemCTL Local Development/", "-addext", "extendedKeyUsage=codeSigning",
            "-addext", "keyUsage=digitalSignature")
        run("/usr/bin/openssl", "pkcs12", "-export", "-inkey", str(key), "-in", str(certificate),
            "-out", str(archive), "-passout", "pass:" + password)
        der = run("/usr/bin/openssl", "x509", "-in", str(certificate), "-outform", "DER")
        run("/usr/bin/security", "create-keychain", "-p", password, str(keychain))
        try:
            run("/usr/bin/security", "unlock-keychain", "-p", password, str(keychain))
            run("/usr/bin/security", "import", str(archive), "-k", str(keychain), "-P", password, "-T", "/usr/bin/codesign")
            value = {"identity": hashlib.sha1(der).hexdigest().upper(), "keychain": str(keychain)}
            for path, data in ((password_file, password), (marker, json.dumps(value) + "\n")):
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
                    handle.write(data)
            if run("/usr/bin/security", "list-keychains", "-d", "user") != search_list:
                raise RuntimeError("Creating the isolated keychain unexpectedly changed the search list")
        except BaseException:
            # Preserve incomplete state for diagnosis rather than silently issuing a new key.
            raise
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.home() / "Library/Application Support/RemCTL Signing")
    args = parser.parse_args()
    print(json.dumps(identity(args.directory)))


if __name__ == "__main__":
    main()
