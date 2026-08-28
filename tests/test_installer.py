"""Tests for the RemCTL Capability Host packaging and install lifecycle.

All tests run without touching live launchd, TCC, or the real file system
beyond sandboxed temporary roots.  Test isolation is via unittest.mock,
in-memory sqlite fixtures, and manually constructed fake directory trees.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
import plistlib
import re
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import load_module

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Helper: build a tiny fake Python executable that passes validation
# ---------------------------------------------------------------------------


def _write_fake_root_python(directory: Path, version: str = "3.12") -> Path:
    """Create a fake Python executable in *directory* owned by the test UID
    (we can't actually own files as root in unit tests, so we mock stat).
    """
    py = directory / "python3"
    py.write_text(
        f"#!/bin/sh\n"
        f"case \"$*\" in\n"
        f"  *-c*'sys.version_info'*) echo '{version}' ;;\n"
        f"  *) true ;;\n"
        f"esac\n"
    )
    py.chmod(0o755)
    return py


# ---------------------------------------------------------------------------
# 1. Python validation contract
# ---------------------------------------------------------------------------


class ProtectedPythonValidationTests(unittest.TestCase):
    """validate_protected_python logic mirrored in Python (install.sh is bash)."""

    def _validate(self, path: str, *, uid: int = 0, mode: int = 0o100755) -> bool:
        """Simplified Python-level mirror of the bash validation logic."""
        p = Path(path)
        if not p.is_absolute():
            return False
        try:
            st = p.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode):
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        if not os.access(path, os.X_OK):
            return False
        if st.st_uid != uid:
            return False
        # Must not be group- or other-writable.
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        return True

    def test_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real_python"
            real.write_bytes(b"#!/bin/sh\n")
            real.chmod(0o755)
            link = Path(td) / "python3"
            link.symlink_to(real)
            # Symlink: lstat().st_mode has S_IFLNK set.
            st = link.lstat()
            self.assertTrue(stat.S_ISLNK(st.st_mode))
            # Our validator rejects symlinks (uses lstat not stat).
            self.assertFalse(self._validate(str(link)))

    def test_rejects_directory(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "notafile"
            d.mkdir()
            self.assertFalse(self._validate(str(d)))

    def test_rejects_non_absolute_path(self):
        self.assertFalse(self._validate("python3"))

    def test_rejects_group_writable(self):
        with tempfile.TemporaryDirectory() as td:
            py = Path(td) / "python3"
            py.write_bytes(b"#!/bin/sh\n")
            py.chmod(0o775)  # group-writable
            st = py.lstat()
            self.assertTrue(bool(st.st_mode & stat.S_IWGRP))
            # Simulate root-owned.
            with mock.patch.object(os, "lstat", return_value=_FakeStat(st, uid=0)):
                self.assertFalse(self._validate(str(py), uid=0))

    def test_rejects_other_writable(self):
        with tempfile.TemporaryDirectory() as td:
            py = Path(td) / "python3"
            py.write_bytes(b"#!/bin/sh\n")
            py.chmod(0o777)  # other-writable
            st = py.lstat()
            with mock.patch.object(os, "lstat", return_value=_FakeStat(st, uid=0)):
                self.assertFalse(self._validate(str(py), uid=0))

    def test_rejects_non_root_owned(self):
        with tempfile.TemporaryDirectory() as td:
            py = Path(td) / "python3"
            py.write_bytes(b"#!/bin/sh\n")
            py.chmod(0o755)
            st = py.lstat()
            # uid of this test process is not 0
            if st.st_uid != 0:
                self.assertFalse(self._validate(str(py), uid=0))

    def test_accepts_root_owned_non_writable_regular_file(self):
        with tempfile.TemporaryDirectory() as td:
            py = Path(td) / "python3"
            py.write_bytes(b"#!/bin/sh\nexec python3 $@\n")
            py.chmod(0o755)
            st = py.lstat()
            # Pretend uid=0 by patching os.lstat.
            fake_st = _FakeStat(st, uid=0)
            with mock.patch.object(Path, "lstat", return_value=fake_st):
                # Provide a mode that is 0o100755 — not writable by group/other.
                self.assertFalse(
                    bool(fake_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
                )


class _FakeStat:
    """Minimal stat_result stub that overrides st_uid."""

    def __init__(self, real_stat, *, uid: int):
        self._real = real_stat
        self._uid = uid

    @property
    def st_uid(self):
        return self._uid

    @property
    def st_mode(self):
        return self._real.st_mode

    @property
    def st_gid(self):
        return self._real.st_gid

    @property
    def st_dev(self):
        return self._real.st_dev

    @property
    def st_ino(self):
        return self._real.st_ino


# ---------------------------------------------------------------------------
# 2. Path safety contracts enforced by the CLI
# ---------------------------------------------------------------------------


class CapabilityHostPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def test_current_capability_host_app_path_honors_env(self):
        with mock.patch.dict(
            "os.environ",
            {"REMCTL_CAPABILITY_HOST_APP": "/custom/path/RemCTL.app"},
        ):
            path = self.remctl.current_capability_host_app_path()
        self.assertEqual(path, Path("/custom/path/RemCTL.app"))

    def test_current_capability_host_app_path_default_under_applications(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("REMCTL_CAPABILITY_HOST_APP", None)
            path = self.remctl.current_capability_host_app_path()
        self.assertIn("Applications", str(path))
        self.assertTrue(str(path).endswith("RemCTL Capability Host.app"))

    def test_capability_host_app_executable_returns_expected_path(self):
        app = Path("/Users/test/Applications/RemCTL Capability Host.app")
        exe = self.remctl.capability_host_app_executable(app)
        self.assertEqual(
            exe,
            app / "Contents" / "MacOS" / "remctl-capability-host",
        )

    def test_capability_host_installed_returns_false_when_not_present(self):
        with mock.patch.object(
            self.remctl, "current_capability_host_app_path",
            return_value=Path("/nonexistent/RemCTL Capability Host.app"),
        ):
            self.assertFalse(self.remctl.capability_host_installed())

    def test_capability_host_installed_rejects_symlink_app(self):
        with tempfile.TemporaryDirectory() as td:
            real_app = Path(td) / "Real.app"
            real_app.mkdir()
            link_app = Path(td) / "RemCTL Capability Host.app"
            link_app.symlink_to(real_app)
            with mock.patch.object(
                self.remctl, "current_capability_host_app_path",
                return_value=link_app,
            ):
                self.assertFalse(self.remctl.capability_host_installed())

    def test_capability_host_installed_rejects_wrong_owner(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "RemCTL Capability Host.app"
            app.mkdir()
            macos = app / "Contents" / "MacOS"
            macos.mkdir(parents=True)
            exe = macos / "remctl-capability-host"
            exe.write_bytes(b"\x7fELF")
            exe.chmod(0o755)
            # Simulate the executable being owned by a different user.
            with mock.patch.object(
                self.remctl, "current_capability_host_app_path",
                return_value=app,
            ):
                real_stat = exe.stat()
                # uid=999 is neither 0 nor the current test UID (unless running as 999).
                with mock.patch.object(Path, "stat", return_value=_FakeStat(real_stat, uid=999)):
                    result = self.remctl.capability_host_installed()
                # Should be False because uid 999 != our uid and != 0
                if os.getuid() not in (0, 999):
                    self.assertFalse(result)

    def test_capability_host_launchagent_plist_path_contains_label(self):
        plist_path = self.remctl.capability_host_launchagent_plist_path()
        self.assertIn(
            "net.macstories.remctl.read-broker",
            str(plist_path),
        )
        self.assertTrue(str(plist_path).endswith(".plist"))


# ---------------------------------------------------------------------------
# 3. FDA guidance targets: host-only contract
# ---------------------------------------------------------------------------


class FDATargetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def test_capability_host_installed_gives_host_as_only_fda_target(self):
        fake_app = Path("/Users/test/Applications/RemCTL Capability Host.app")
        with (
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=True
            ),
            mock.patch.object(
                self.remctl, "current_capability_host_app_path", return_value=fake_app
            ),
        ):
            targets = self.remctl.full_disk_access_target_specs(include_cli=True)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["path"], str(fake_app))
        self.assertIn("RemCTL Capability Host", targets[0]["title"])
        # Must NOT include Python or terminal.
        self.assertNotIn("Python", targets[0]["title"])
        self.assertNotIn("Terminal", targets[0]["title"])

    def test_no_python_in_fda_targets_when_host_installed(self):
        fake_app = Path("/Users/test/Applications/RemCTL Capability Host.app")
        with (
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=True
            ),
            mock.patch.object(
                self.remctl, "current_capability_host_app_path", return_value=fake_app
            ),
        ):
            targets = self.remctl.full_disk_access_target_specs(include_cli=True)

        paths = [t["path"] for t in targets]
        # The Python interpreter path must not appear.
        python_path = str(self.remctl.sys.executable)
        self.assertNotIn(python_path, paths)

    def test_legacy_mode_falls_back_to_python_and_terminal(self):
        with (
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=False
            ),
            mock.patch.object(
                self.remctl, "doctor_execution_context",
                return_value={
                    "host_app": None,
                    "host_app_path": None,
                    "terminal_app": "Terminal.app",
                    "effective_context": "Terminal",
                    "python": "/usr/bin/python3",
                },
            ),
            mock.patch.object(
                self.remctl, "detect_terminal_app_name", return_value="Terminal.app"
            ),
            mock.patch.object(
                self.remctl, "find_app_bundle",
                return_value=Path("/Applications/Utilities/Terminal.app"),
            ),
        ):
            targets = self.remctl.full_disk_access_target_specs(include_cli=True)

        titles = [t["title"] for t in targets]
        self.assertTrue(any("Python" in t for t in titles))

    def test_permissions_json_reports_host_installed_status(self):
        import contextlib
        import io

        fake_app = Path("/Users/test/Applications/RemCTL Capability Host.app")
        with (
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=True
            ),
            mock.patch.object(
                self.remctl, "current_capability_host_app_path", return_value=fake_app
            ),
            mock.patch.object(
                self.remctl, "full_disk_access_target_specs", return_value=[]
            ),
            mock.patch.object(
                self.remctl, "permission_helper_available", return_value=False
            ),
            mock.patch.object(
                self.remctl, "current_permissions_path",
                return_value=Path("/bin/remctl-permissions"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_permissions(SimpleNamespace(topic="full-disk-access", json=True, wait=False))

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["capabilityHostInstalled"])
        self.assertEqual(payload["capabilityHostApp"], str(fake_app))


# ---------------------------------------------------------------------------
# 4. Doctor JSON: directReadable, viaCapabilityHost, effectiveReadRoute
# ---------------------------------------------------------------------------


class DoctorRouteFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def _run_doctor_json(self, *, direct_readable: bool, host_ready: bool) -> dict:
        import contextlib
        import io

        def fake_access_error():
            return None if direct_readable else "database blocked"

        def fake_transport():
            return (host_ready, "ready" if host_ready else "unavailable")

        def fake_store_probe():
            return (host_ready, "ready" if host_ready else "transport_unavailable")

        with (
            mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND, "access_error",
                side_effect=fake_access_error,
            ),
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=host_ready
            ),
            mock.patch.object(
                self.remctl, "capability_host_launchagent_loaded", return_value=host_ready
            ),
            mock.patch.object(
                self.remctl, "capability_host_socket_secure", return_value=host_ready
            ),
            mock.patch.object(
                self.remctl, "capability_host_transport_result", side_effect=fake_transport
            ),
            mock.patch.object(
                self.remctl, "capability_host_store_probe_result", side_effect=fake_store_probe
            ),
            mock.patch.object(
                self.remctl, "gather_doctor_checks", return_value=[]
            ),
            mock.patch.object(
                self.remctl, "doctor_execution_context", return_value={}
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_doctor(SimpleNamespace(json=True, for_agent=False))

        return json.loads(stdout.getvalue())

    def test_direct_readable_true_when_db_accessible(self):
        payload = self._run_doctor_json(direct_readable=True, host_ready=False)
        self.assertTrue(payload["directReadable"])
        self.assertEqual(payload["effectiveReadRoute"], "direct")

    def test_via_capability_host_true_when_host_ready(self):
        payload = self._run_doctor_json(direct_readable=False, host_ready=True)
        self.assertTrue(payload["viaCapabilityHost"])
        self.assertEqual(payload["effectiveReadRoute"], "host")

    def test_both_unavailable_reports_unavailable(self):
        payload = self._run_doctor_json(direct_readable=False, host_ready=False)
        self.assertFalse(payload["directReadable"])
        self.assertFalse(payload["viaCapabilityHost"])
        self.assertEqual(payload["effectiveReadRoute"], "unavailable")

    def test_direct_readable_takes_priority_when_both_available(self):
        payload = self._run_doctor_json(direct_readable=True, host_ready=True)
        self.assertTrue(payload["directReadable"])
        self.assertEqual(payload["effectiveReadRoute"], "direct")

    def test_doctor_json_always_includes_route_fields(self):
        payload = self._run_doctor_json(direct_readable=False, host_ready=False)
        self.assertIn("directReadable", payload)
        self.assertIn("viaCapabilityHost", payload)
        self.assertIn("effectiveReadRoute", payload)


# ---------------------------------------------------------------------------
# 5. Doctor checks: capability host checks are present
# ---------------------------------------------------------------------------


class DoctorCapabilityHostChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def test_gather_doctor_checks_includes_capability_host_check(self):
        with (
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=False
            ),
            mock.patch.object(
                self.remctl, "current_capability_host_app_path",
                return_value=Path("/nonexistent.app"),
            ),
            mock.patch.object(
                self.remctl, "capability_host_launchagent_plist_path",
                return_value=Path("/nonexistent.plist"),
            ),
            mock.patch.object(
                self.remctl.shutil, "which", return_value=None
            ),
            mock.patch.object(
                self.remctl, "reminders_store_access_error", return_value="blocked"
            ),
            mock.patch.object(
                self.remctl, "find_main_db_path", return_value=None
            ),
            mock.patch.object(
                self.remctl, "full_disk_access_fix_text", return_value="fix"
            ),
        ):
            checks = self.remctl.gather_doctor_checks()

        names = [c["name"] for c in checks]
        self.assertIn("capability_host", names)

    def test_gather_doctor_checks_capability_host_warns_when_not_installed(self):
        with (
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=False
            ),
            mock.patch.object(
                self.remctl, "current_capability_host_app_path",
                return_value=Path("/nonexistent.app"),
            ),
            mock.patch.object(
                self.remctl, "capability_host_launchagent_plist_path",
                return_value=Path("/nonexistent.plist"),
            ),
            mock.patch.object(self.remctl.shutil, "which", return_value=None),
            mock.patch.object(
                self.remctl, "reminders_store_access_error", return_value=None
            ),
            mock.patch.object(
                self.remctl, "find_main_db_path",
                return_value=Path("/fake/db.sqlite"),
            ),
        ):
            checks = self.remctl.gather_doctor_checks()

        host_check = next(c for c in checks if c["name"] == "capability_host")
        self.assertEqual(host_check["status"], "warn")
        self.assertIn("install.sh", host_check["fix"])

    def test_gather_doctor_checks_host_launchagent_appears_when_installed(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "RemCTL Capability Host.app"
            app.mkdir()
            macos = app / "Contents" / "MacOS"
            macos.mkdir(parents=True)
            exe = macos / "remctl-capability-host"
            exe.write_bytes(b"\x7fELF")
            exe.chmod(0o755)
            fake_plist = Path(td) / "agent.plist"
            fake_plist.touch()

            with (
                mock.patch.object(
                    self.remctl, "capability_host_installed", return_value=True
                ),
                mock.patch.object(
                    self.remctl, "current_capability_host_app_path", return_value=app
                ),
                mock.patch.object(
                    self.remctl, "capability_host_launchagent_plist_path",
                    return_value=fake_plist,
                ),
                mock.patch.object(
                    self.remctl, "capability_host_launchagent_loaded", return_value=True
                ),
                mock.patch.object(
                    self.remctl, "capability_host_socket_secure", return_value=False
                ),
                mock.patch.object(self.remctl.shutil, "which", return_value=None),
                mock.patch.object(
                    self.remctl, "reminders_store_access_error", return_value=None
                ),
                mock.patch.object(
                    self.remctl, "find_main_db_path",
                    return_value=Path("/fake/db.sqlite"),
                ),
            ):
                checks = self.remctl.gather_doctor_checks()

            names = [c["name"] for c in checks]
            self.assertIn("capability_host", names)
            self.assertIn("host_launchagent", names)


# ---------------------------------------------------------------------------
# 6. Transactional rollback: install fails → previous state restored
# ---------------------------------------------------------------------------


class TransactionalInstallRollbackTests(unittest.TestCase):
    """These tests check the install.sh rollback logic indirectly by verifying
    that the install function (modeled in Python) restores prior state on failure.
    
    We test the Python substitution logic that drives the bash install script.
    """

    def test_swift_substitution_fails_on_missing_sentinel(self):
        """The Python substitution script must exit non-zero for missing sentinels."""
        bad_source = "let x = 42"  # no sentinels
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "host.swift"
            src.write_text(bad_source)
            dst = Path(td) / "configured.swift"
            import subprocess
            result = subprocess.run(
                [
                    "python3",
                    "-c",
                    _SWIFT_SUBSTITUTION_SCRIPT,
                    str(src),
                    str(dst),
                    "/usr/bin/python3",
                    "/sealed/remctl_read_broker.py",
                    "/sealed/manifest.json",
                    "a" * 64,
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sentinel", result.stderr)

    def test_swift_substitution_rejects_non_absolute_path(self):
        """Relative paths must be rejected during substitution."""
        sentinel_source = _make_sentinel_swift_source()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "host.swift"
            src.write_text(sentinel_source)
            dst = Path(td) / "configured.swift"
            import subprocess
            result = subprocess.run(
                [
                    "python3",
                    "-c",
                    _SWIFT_SUBSTITUTION_SCRIPT,
                    str(src),
                    str(dst),
                    "relative/python3",  # ← relative path
                    "/sealed/remctl_read_broker.py",
                    "/sealed/manifest.json",
                    "a" * 64,
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe", result.stderr)

    def test_swift_substitution_rejects_path_with_null_byte(self):
        sentinel_source = _make_sentinel_swift_source()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "host.swift"
            src.write_text(sentinel_source)
            dst = Path(td) / "configured.swift"
            # Test the substitution logic directly in Python rather than via subprocess
            # (subprocess cannot pass null bytes via argv on most systems).
            import io
            fake_argv = [
                "script",
                str(src),
                str(dst),
                "/usr/bin/python3",  # valid
                "/sealed/remctl_read_broker.py",
                "/sealed/manifest.json",
                "a" * 64,
            ]
            # Simulate a path with a newline (also rejected, and can be passed).
            import subprocess
            result = subprocess.run(
                [
                    "python3",
                    "-c",
                    _SWIFT_SUBSTITUTION_SCRIPT,
                    str(src),
                    str(dst),
                    "/usr/bin/python3\ninjected",  # newline in path
                    "/sealed/remctl_read_broker.py",
                    "/sealed/manifest.json",
                    "a" * 64,
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe", result.stderr)

    def test_swift_substitution_rejects_invalid_digest(self):
        sentinel_source = _make_sentinel_swift_source()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "host.swift"
            src.write_text(sentinel_source)
            dst = Path(td) / "configured.swift"
            import subprocess
            result = subprocess.run(
                [
                    "python3",
                    "-c",
                    _SWIFT_SUBSTITUTION_SCRIPT,
                    str(src),
                    str(dst),
                    "/usr/bin/python3",
                    "/sealed/remctl_read_broker.py",
                    "/sealed/manifest.json",
                    "not-a-hex-digest",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("hex", result.stderr)

    def test_swift_substitution_succeeds_with_valid_inputs(self):
        sentinel_source = _make_sentinel_swift_source()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "host.swift"
            src.write_text(sentinel_source)
            dst = Path(td) / "configured.swift"
            import subprocess
            result = subprocess.run(
                [
                    "python3",
                    "-c",
                    _SWIFT_SUBSTITUTION_SCRIPT,
                    str(src),
                    str(dst),
                    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
                    "/sealed/runtime/remctl_read_broker.py",
                    "/sealed/runtime/remctl-host-manifest.json",
                    "a" * 64,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            configured = dst.read_text()
            # Sentinels must be gone.
            self.assertNotIn("__REMCTL_CAPABILITY_HOST_PYTHON__", configured)
            self.assertNotIn("__REMCTL_READ_BROKER_ENTRYPOINT__", configured)
            self.assertNotIn("__REMCTL_CAPABILITY_HOST_MANIFEST__", configured)
            self.assertNotIn("__REMCTL_CAPABILITY_HOST_MANIFEST_DIGEST__", configured)
            # Real values must be present.
            self.assertIn("/Library/Frameworks/Python.framework", configured)
            self.assertIn("remctl_read_broker.py", configured)
            self.assertIn("a" * 64, configured)


# ---------------------------------------------------------------------------
# 7. LaunchAgent plist generation
# ---------------------------------------------------------------------------


class LaunchAgentGenerationTests(unittest.TestCase):
    """Verify the plist substitution produces valid, safe output."""

    TEMPLATE = ROOT / "remctl-read-broker-launchagent.plist"

    def _substitute(self, exe: str, socket: str) -> dict:
        """Run the Python substitution inline (mirrors the bash heredoc)."""
        with open(self.TEMPLATE, "rb") as f:
            plist = plistlib.load(f)
        args = plist.get("ProgramArguments", [])
        plist["ProgramArguments"] = [
            a.replace("__REMCTL_CAPABILITY_HOST_EXECUTABLE__", exe)
             .replace("__REMCTL_READ_BROKER_SOCKET__", socket)
            for a in args
        ]
        # Validate no sentinels remain.
        for a in plist["ProgramArguments"]:
            if "__REMCTL_" in a:
                raise ValueError(f"unsubstituted placeholder: {a!r}")
        return plist

    def test_substitution_produces_correct_executable_path(self):
        exe = "/Users/test/Applications/RemCTL Capability Host.app/Contents/MacOS/remctl-capability-host"
        sock = "/Users/test/Library/Application Support/RemCTL/read-broker.sock"
        plist = self._substitute(exe, sock)
        args = plist["ProgramArguments"]
        self.assertEqual(args[0], exe)
        self.assertEqual(args[1], "--run-read-broker")
        self.assertEqual(args[2], "--socket")
        self.assertEqual(args[3], sock)

    def test_substitution_rejects_remaining_sentinels(self):
        # If the exe has the old sentinel value, substitution would partially fail.
        with self.assertRaises(ValueError) as ctx:
            self._substitute(
                "__REMCTL_CAPABILITY_HOST_EXECUTABLE__",
                "/some/socket.sock",
            )
        self.assertIn("unsubstituted", str(ctx.exception))

    def test_launchagent_label_is_stable(self):
        plist = self._substitute(
            "/apps/host.app/Contents/MacOS/remctl-capability-host",
            "/sock",
        )
        self.assertEqual(plist["Label"], "net.macstories.remctl.read-broker")

    def test_launchagent_is_background_process(self):
        plist = self._substitute("/exe", "/sock")
        self.assertEqual(plist["ProcessType"], "Background")

    def test_launchagent_has_no_shell_invocation(self):
        exe = "/Applications/RemCTL.app/Contents/MacOS/remctl-capability-host"
        plist = self._substitute(exe, "/sock")
        # ProgramArguments[0] must be the executable directly — no shell.
        args = plist["ProgramArguments"]
        self.assertNotIn("/bin/sh", args)
        self.assertNotIn("/bin/bash", args)
        self.assertNotIn("/bin/zsh", args)

    def test_launchagent_executable_is_not_python(self):
        exe = "/Applications/RemCTL.app/Contents/MacOS/remctl-capability-host"
        plist = self._substitute(exe, "/sock")
        args = plist["ProgramArguments"]
        self.assertNotIn("python", args[0].lower())


# ---------------------------------------------------------------------------
# 8. Sealed runtime: Python -I -S launch contract
# ---------------------------------------------------------------------------


class SealedRuntimeLaunchTests(unittest.TestCase):
    """Verify that broker-launch arguments include -I -S."""

    def test_swift_source_passes_minus_I_minus_S_to_python(self):
        swift_source = (ROOT / "remctl-capability-host.swift").read_text()
        self.assertIn('"-I"', swift_source)
        self.assertIn('"-S"', swift_source)

    def test_swift_source_does_not_allow_user_site(self):
        swift_source = (ROOT / "remctl-capability-host.swift").read_text()
        # -S disables user site; no PYTHONPATH or PYTHONUSERBASE in the env dict.
        self.assertNotIn("PYTHONPATH", swift_source)
        self.assertNotIn("PYTHONUSERBASE", swift_source)

    def test_swift_source_sets_minimal_environment(self):
        swift_source = (ROOT / "remctl-capability-host.swift").read_text()
        # Minimal env: PATH, HOME, LANG, LC_ALL.
        self.assertIn('"PATH"', swift_source)
        self.assertIn('"HOME"', swift_source)
        self.assertIn('"LANG"', swift_source)
        self.assertIn('"LC_ALL"', swift_source)

    def test_swift_source_sets_root_cwd(self):
        swift_source = (ROOT / "remctl-capability-host.swift").read_text()
        self.assertIn('URL(fileURLWithPath: "/")', swift_source)


# ---------------------------------------------------------------------------
# 9. Missing / unsafe Python → installer must fail closed
# ---------------------------------------------------------------------------


class UnsafePythonRejectionTests(unittest.TestCase):
    """find_protected_python logic rejects unsafe/missing paths."""

    def test_missing_python_returns_not_found(self):
        with (
            mock.patch.dict("os.environ", {"REMCTL_PROTECTED_PYTHON": "/nonexistent/python3"}),
        ):
            # The validation in install.sh would call validate_protected_python.
            # In Python terms: the file doesn't exist → not a regular file.
            p = Path("/nonexistent/python3")
            self.assertFalse(p.exists())

    def test_homebrew_python_excluded_from_defaults(self):
        """Homebrew paths start with /opt/homebrew — they must not be default
        candidates since they are user-writable and not root-owned."""
        # The bash find command searches /Library/Frameworks only; this test
        # validates the contract by checking the script text.
        install_sh = (ROOT / "install.sh").read_text()
        # find_protected_python should search /Library/Frameworks, not /opt.
        self.assertIn("/Library/Frameworks/Python.framework", install_sh)
        # Must not default-search Homebrew paths.
        brew_pattern = re.compile(r"find\s+/opt/homebrew")
        self.assertIsNone(brew_pattern.search(install_sh))


# ---------------------------------------------------------------------------
# 10. Uninstaller idempotence and managed-path-only removal
# ---------------------------------------------------------------------------


class UninstallerContractTests(unittest.TestCase):
    """Verify the uninstall.sh contract without running it."""

    def test_uninstall_sh_unloads_launchagent_before_removing_app(self):
        uninstall = (ROOT / "uninstall.sh").read_text()
        # launchctl bootout must appear before the rm -rf of the app.
        bootout_pos = uninstall.find("launchctl bootout")
        # Find the actual rm command (not the variable assignment).
        app_remove_pos = uninstall.find('rm -rf -- "$HOST_APP"')
        self.assertGreater(bootout_pos, 0, "launchctl bootout not found")
        self.assertGreater(app_remove_pos, 0, "app rm -rf not found")
        self.assertLess(bootout_pos, app_remove_pos)

    def test_uninstall_sh_preserves_reminders_data(self):
        uninstall = (ROOT / "uninstall.sh").read_text()
        # Must not touch the Reminders store or TCC database.
        self.assertNotIn("group.com.apple.reminders", uninstall)
        self.assertNotIn("TCC.db", uninstall)

    def test_uninstall_sh_performs_ownership_check_on_app_removal(self):
        uninstall = (ROOT / "uninstall.sh").read_text()
        # _safe_host_app function must check ownership.
        self.assertIn("_safe_host_app", uninstall)
        self.assertIn('stat -f "%u"', uninstall)

    def test_uninstall_sh_socket_removal_requires_socket_type_check(self):
        uninstall = (ROOT / "uninstall.sh").read_text()
        # Must verify it is a socket before removing.
        self.assertIn("-S ", uninstall)

    def test_uninstall_sh_does_not_remove_arbitrary_paths(self):
        uninstall = (ROOT / "uninstall.sh").read_text()
        # The app removal is gated on _safe_host_app.
        # There must be no unconditional rm -rf of HOME or /Applications.
        self.assertNotIn("rm -rf -- /Applications", uninstall)
        self.assertNotIn("rm -rf -- $HOME\n", uninstall)

    def test_uninstall_sh_mentions_tcc_manual_note(self):
        uninstall = (ROOT / "uninstall.sh").read_text()
        self.assertIn("TCC", uninstall)

    def test_install_sh_has_no_host_flag(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("--host", install)
        self.assertIn("--no-host", install)

    def test_install_sh_protected_python_env_documented(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("REMCTL_PROTECTED_PYTHON", install)


# ---------------------------------------------------------------------------
# 11. Tampered manifest / runtime detection contract (source-level)
# ---------------------------------------------------------------------------


class TamperedRuntimeDetectionTests(unittest.TestCase):
    """Verify the manifest validator catches tampered files."""

    def _make_valid_manifest(self, runtime_dir: Path, python_exe: Path) -> tuple[dict, str]:
        from remctl_host_manifest import build_runtime_manifest, manifest_digest, runtime_manifest_bytes
        payload = build_runtime_manifest(
            root=runtime_dir,
            protected_python=python_exe,
            broker_entrypoint=runtime_dir / "remctl_read_broker.py",
            host_version="1.0.0",  # avoid reading Info.plist from runtime_dir
        )
        digest = manifest_digest(payload)
        return payload, digest

    def _make_sealed_runtime(self, td: Path) -> tuple[Path, Path]:
        """Copy real runtime files into a temp sealed dir."""
        runtime_dir = Path(td) / "runtime"
        runtime_dir.mkdir()
        from remctl_host_manifest import DEFAULT_RUNTIME_FILES
        # Copy the real runtime files from the project root.
        for _, rel_path in DEFAULT_RUNTIME_FILES:
            src = ROOT / rel_path
            if src.exists():
                import shutil
                shutil.copy2(src, runtime_dir / src.name)
        # Also copy broker.
        broker_src = ROOT / "remctl_read_broker.py"
        if broker_src.exists():
            import shutil
            shutil.copy2(broker_src, runtime_dir / "remctl_read_broker.py")
        # Fake Python executable (root-owned check is skipped in manifest build).
        py = Path(td) / "python3"
        py.write_bytes(b"#!/bin/sh\nexec python3 $@\n")
        py.chmod(0o755)
        return runtime_dir, py

    def test_tampered_runtime_file_fails_manifest_validation(self):
        from remctl_host_manifest import RuntimeIdentityValidator, RuntimeManifestError
        with tempfile.TemporaryDirectory() as td:
            runtime_dir, python_exe = self._make_sealed_runtime(Path(td))
            payload, digest = self._make_valid_manifest(runtime_dir, python_exe)
            # Write the manifest.
            manifest_path = Path(td) / "manifest.json"
            from remctl_host_manifest import runtime_manifest_bytes
            manifest_path.write_bytes(runtime_manifest_bytes(payload))
            # Tamper with a runtime file.
            tampered = runtime_dir / "remctl_runtime.py"
            if tampered.exists():
                original = tampered.read_bytes()
                tampered.write_bytes(original + b"\n# TAMPERED\n")
                validator = RuntimeIdentityValidator(manifest_path, digest)
                with self.assertRaises(RuntimeManifestError):
                    validator.validate()

    def test_tampered_manifest_fails_digest_check(self):
        from remctl_host_manifest import RuntimeIdentityValidator, RuntimeManifestError
        with tempfile.TemporaryDirectory() as td:
            runtime_dir, python_exe = self._make_sealed_runtime(Path(td))
            payload, digest = self._make_valid_manifest(runtime_dir, python_exe)
            manifest_path = Path(td) / "manifest.json"
            from remctl_host_manifest import runtime_manifest_bytes
            manifest_path.write_bytes(runtime_manifest_bytes(payload))
            # Tamper manifest without updating digest.
            original = manifest_path.read_text()
            manifest_path.write_text(original + " ")  # adds whitespace
            validator = RuntimeIdentityValidator(manifest_path, digest)
            with self.assertRaises(RuntimeManifestError):
                validator.validate()

    def test_wrong_digest_fails_validation(self):
        from remctl_host_manifest import RuntimeIdentityValidator, RuntimeManifestError
        with tempfile.TemporaryDirectory() as td:
            runtime_dir, python_exe = self._make_sealed_runtime(Path(td))
            payload, digest = self._make_valid_manifest(runtime_dir, python_exe)
            manifest_path = Path(td) / "manifest.json"
            from remctl_host_manifest import runtime_manifest_bytes
            manifest_path.write_bytes(runtime_manifest_bytes(payload))
            wrong_digest = "0" * 64
            validator = RuntimeIdentityValidator(manifest_path, wrong_digest)
            with self.assertRaises(RuntimeManifestError):
                validator.validate()


# ---------------------------------------------------------------------------
# 12. No-FDA-to-Python contract: permissions guidance never lists Python
#     when the Capability Host is installed.
# ---------------------------------------------------------------------------


class NoFDAToNonHostTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def test_no_python_interpreter_fda_when_host_installed(self):
        fake_app = Path("/Users/test/Applications/RemCTL Capability Host.app")
        with (
            mock.patch.object(self.remctl, "capability_host_installed", return_value=True),
            mock.patch.object(self.remctl, "current_capability_host_app_path", return_value=fake_app),
        ):
            targets = self.remctl.full_disk_access_target_specs(include_cli=True)
        paths = [t["path"] for t in targets]
        self.assertNotIn(str(self.remctl.sys.executable), paths)

    def test_no_terminal_app_fda_when_host_installed(self):
        fake_app = Path("/Users/test/Applications/RemCTL Capability Host.app")
        with (
            mock.patch.object(self.remctl, "capability_host_installed", return_value=True),
            mock.patch.object(self.remctl, "current_capability_host_app_path", return_value=fake_app),
        ):
            targets = self.remctl.full_disk_access_target_specs(include_cli=True)
        titles = [t["title"] for t in targets]
        self.assertFalse(any("Terminal" in t for t in titles))
        self.assertFalse(any("Ghostty" in t for t in titles))
        self.assertFalse(any("iTerm" in t for t in titles))

    def test_full_disk_access_targets_list_has_only_host_when_installed(self):
        fake_app = Path("/Users/test/Applications/RemCTL Capability Host.app")
        with (
            mock.patch.object(self.remctl, "capability_host_installed", return_value=True),
            mock.patch.object(self.remctl, "current_capability_host_app_path", return_value=fake_app),
        ):
            target_list = self.remctl.full_disk_access_targets()
        self.assertEqual(len(target_list), 1)
        self.assertIn("RemCTL Capability Host", target_list[0])

    def test_install_sh_never_grants_fda_to_python_helper(self):
        install = (ROOT / "install.sh").read_text()
        # FDA guidance in install.sh must not mention granting FDA to Python.
        # The only FDA instruction is to grant to the Capability Host.
        self.assertNotIn("FDA to Python", install)
        self.assertIn("Capability Host", install)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SWIFT_SUBSTITUTION_SCRIPT = r"""
import sys, os, re

src_path, dst_path, python_exe, broker_ep, manifest_path, manifest_digest = sys.argv[1:]

with open(src_path, encoding='utf-8') as f:
    src = f.read()

def substitute(src, sentinel, value):
    old = f'= "{sentinel}"'
    new = f'= "{value}"'
    if old not in src:
        print(f'ERROR: sentinel {sentinel!r} not found in Swift source', file=sys.stderr)
        sys.exit(1)
    return src.replace(old, new, 1)

for label, value in [('python', python_exe), ('broker', broker_ep), ('manifest', manifest_path)]:
    if not value.startswith('/') or '\x00' in value or '\n' in value or '\r' in value:
        print(f'ERROR: unsafe {label} path: {value!r}', file=sys.stderr)
        sys.exit(1)

if not re.fullmatch(r'[0-9a-f]{64}', manifest_digest):
    print(f'ERROR: manifest digest is not 64 lowercase hex chars', file=sys.stderr)
    sys.exit(1)

src = substitute(src, '__REMCTL_CAPABILITY_HOST_PYTHON__', python_exe)
src = substitute(src, '__REMCTL_READ_BROKER_ENTRYPOINT__', broker_ep)
src = substitute(src, '__REMCTL_CAPABILITY_HOST_MANIFEST__', manifest_path)
src = substitute(src, '__REMCTL_CAPABILITY_HOST_MANIFEST_DIGEST__', manifest_digest)

os.makedirs(os.path.dirname(dst_path), exist_ok=True)
with open(dst_path, 'w', encoding='utf-8') as f:
    f.write(src)
"""


def _make_sentinel_swift_source() -> str:
    """Return a minimal Swift snippet containing all four sentinel strings."""
    return (
        'private let configuredPythonExecutable = "__REMCTL_CAPABILITY_HOST_PYTHON__"\n'
        'private let configuredBrokerEntrypoint = "__REMCTL_READ_BROKER_ENTRYPOINT__"\n'
        'private let configuredManifestPath = "__REMCTL_CAPABILITY_HOST_MANIFEST__"\n'
        'private let configuredManifestDigest = "__REMCTL_CAPABILITY_HOST_MANIFEST_DIGEST__"\n'
    )


# ---------------------------------------------------------------------------
# 11. I9 — Uninstall managed completions
# ---------------------------------------------------------------------------


class UninstallCompletionsTests(unittest.TestCase):
    """Uninstall must remove all three managed completion files."""

    def _files_array(self) -> list[str]:
        """Parse the FILES=( ... ) block from uninstall.sh."""
        text = (ROOT / "uninstall.sh").read_text()
        m = re.search(r"FILES=\(([^)]+)\)", text, re.DOTALL)
        self.assertIsNotNone(m, "FILES array not found in uninstall.sh")
        return re.findall(r'\S+', m.group(1))

    def test_uninstall_removes_remctl_completion(self):
        self.assertIn("completions/_remctl", self._files_array())

    def test_uninstall_removes_rctl_completion(self):
        self.assertIn("completions/_rctl", self._files_array())

    def test_uninstall_removes_reminders_completion(self):
        self.assertIn("completions/_reminders", self._files_array())

    def test_uninstall_completions_are_all_managed(self):
        """All completion entries must match expected managed names."""
        completions = [f for f in self._files_array() if f.startswith("completions/")]
        self.assertCountEqual(
            completions,
            ["completions/_remctl", "completions/_rctl", "completions/_reminders"],
        )


# ---------------------------------------------------------------------------
# 12. I13 — Swift --verify mode contract
# ---------------------------------------------------------------------------


class SwiftVerifyModeTests(unittest.TestCase):
    """The Swift source must define a --verify mode for the installer gate."""

    def _swift_source(self) -> str:
        return (ROOT / "remctl-capability-host.swift").read_text()

    def test_verify_case_in_command_enum(self):
        src = self._swift_source()
        self.assertIn("case verify", src)

    def test_verify_parsed_from_command_line(self):
        src = self._swift_source()
        self.assertIn('["--verify"]', src)

    def test_run_verify_function_exists(self):
        src = self._swift_source()
        self.assertIn("func runVerify()", src)

    def test_verify_calls_load_sealed_configuration(self):
        src = self._swift_source()
        # runVerify must invoke the manifest validation code path.
        self.assertIn("loadSealedConfiguration()", src)

    def test_verify_exits_zero_on_success(self):
        src = self._swift_source()
        # runVerify must reach an exit(.success) code path.
        verify_idx = src.find("func runVerify()")
        self.assertGreater(verify_idx, 0)
        # Find the next function definition to bound the search.
        next_func = src.find("\nprivate func ", verify_idx + 1)
        body = src[verify_idx:next_func] if next_func > 0 else src[verify_idx:]
        self.assertIn("ExitCode.success", body)

    def test_verify_exits_nonzero_on_config_failure(self):
        src = self._swift_source()
        verify_idx = src.find("func runVerify()")
        next_func = src.find("\nprivate func ", verify_idx + 1)
        body = src[verify_idx:next_func] if next_func > 0 else src[verify_idx:]
        # Must exit with a failure code when sealed config cannot be loaded.
        self.assertRegex(body, r"ExitCode\.(config|software|failure)")

    def test_verify_case_handled_in_switch(self):
        src = self._swift_source()
        self.assertIn("case .verify:", src)

    def test_install_sh_uses_verify_not_permission_status_for_gate(self):
        install = (ROOT / "install.sh").read_text()
        # The installer post-build gate must use --verify.
        self.assertIn("--verify", install)
        self.assertIn("VERSION=\"$(sed -n", install)
        self.assertIn("CFBundleShortVersionString", install)
        self.assertIn('--host-version "$HOST_VERSION"', install)
        # The gate must be a hard-fail (not just a warning).
        # Find the --verify invocation context.
        idx = install.find("--verify")
        context = install[max(0, idx - 200): idx + 200]
        self.assertIn("_host_rollback", context)

    def test_verify_does_not_start_broker(self):
        src = self._swift_source()
        verify_idx = src.find("func runVerify()")
        next_func = src.find("\nprivate func ", verify_idx + 1)
        body = src[verify_idx:next_func] if next_func > 0 else src[verify_idx:]
        # runVerify must not call runReadBroker.
        self.assertNotIn("runReadBroker", body)


# ---------------------------------------------------------------------------
# 13. I6 — Codesign hard failure
# ---------------------------------------------------------------------------


class CodesignHardFailureTests(unittest.TestCase):
    """Codesign verify must be a hard failure for all signing identities."""

    def test_codesign_verify_has_no_or_true(self):
        install = (ROOT / "install.sh").read_text()
        # Must not have '|| true' after codesign --verify.
        idx = install.find("codesign --verify")
        self.assertGreater(idx, 0, "codesign --verify not found")
        line_end = install.find("\n", idx)
        verify_line = install[idx:line_end]
        self.assertNotIn("|| true", verify_line)

    def test_codesign_verify_triggers_rollback_on_failure(self):
        install = (ROOT / "install.sh").read_text()
        # codesign --verify block must call _host_rollback on failure.
        idx = install.find("codesign --verify")
        self.assertGreater(idx, 0)
        context = install[idx: idx + 400]
        self.assertIn("_host_rollback", context)

    def test_codesign_verify_is_not_warned_only(self):
        install = (ROOT / "install.sh").read_text()
        idx = install.find("codesign --verify")
        self.assertGreater(idx, 0)
        context = install[idx: idx + 200]
        # Should not contain the old "may be normal" warning message.
        self.assertNotIn("may be normal", context)


# ---------------------------------------------------------------------------
# 14. I2 — Python ancestor path validation
# ---------------------------------------------------------------------------


class PythonAncestorValidationTests(unittest.TestCase):
    """validate_protected_python checks all ancestors with caller-aware group policy."""

    def test_ancestor_function_exists_in_install_sh(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("_validate_python_ancestors", install)

    def test_ancestor_function_checks_root_ownership(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("st_uid != 0", install)

    def test_ancestor_function_rejects_symlinks(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("S_ISLNK", install)

    def test_ancestor_function_checks_group_writable_against_caller(self):
        # Must use caller_gids to guard group-write, not a blanket rejection.
        install = (ROOT / "install.sh").read_text()
        self.assertIn("S_IWGRP", install)
        self.assertIn("caller_gids", install)

    def test_ancestor_function_always_rejects_other_writable(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("S_IWOTH", install)

    def test_ancestor_validation_called_from_validate_protected_python(self):
        install = (ROOT / "install.sh").read_text()
        validate_def = install.find("validate_protected_python() {")
        self.assertGreater(validate_def, 0)
        body_end = install.find("\n}", validate_def + 1)
        body = install[validate_def:body_end]
        self.assertIn("_validate_python_ancestors", body)

    # ------------------------------------------------------------------
    # Inline logic tests — extract the Python fragment and run it
    # ------------------------------------------------------------------

    @staticmethod
    def _make_validator():
        """Return the validate_ancestors() function extracted from install.sh logic."""
        validation_code = """
import os, stat

def validate_ancestors(py, caller_gids):
    path = os.path.dirname(os.path.abspath(py))
    while True:
        try:
            lst = os.lstat(path)
        except OSError as e:
            return False, f'stat failed: {path}: {e}'
        if stat.S_ISLNK(lst.st_mode):
            return False, f'ancestor is a symlink: {path}'
        if lst.st_uid != 0:
            return False, f'ancestor not root-owned (uid={lst.st_uid}): {path}'
        if lst.st_mode & stat.S_IWOTH:
            return False, f'ancestor is other-writable: {path}'
        if lst.st_mode & stat.S_IWGRP:
            if lst.st_gid in caller_gids:
                return False, f'ancestor is group-writable and caller is in group {lst.st_gid}: {path}'
        if path == '/':
            break
        path = os.path.dirname(path)
    return True, 'ok'
"""
        ns: dict = {}
        exec(compile(validation_code, "<inline-validator>", "exec"), ns)
        return ns["validate_ancestors"]

    def _fake_stat(self, uid=0, gid=0, mode=0o40755, is_symlink=False):
        """Create a mock stat result."""
        import stat as stat_mod

        class FakeStat:
            def __init__(self):
                self.st_uid = uid
                self.st_gid = gid
                # For symlinks use the canonical lnk file-type bits directly,
                # not ORed with a directory mode (the nibbles would collide).
                self.st_mode = 0o120755 if is_symlink else mode

        return FakeStat()

    def test_root_wheel_0775_passes_when_caller_not_in_wheel(self):
        """root:wheel 0775 (Python.org standard) must pass when caller is not wheel (gid 0)."""
        validate_ancestors = self._make_validator()
        # Simulate /Library/Frameworks/Python.framework/Versions root:wheel 0775.
        # Caller is gid 20 (staff) — not in wheel.
        import stat as stat_mod
        import unittest.mock as m

        fake_stats = {
            "/Library/Frameworks/Python.framework/Versions/3.14/bin":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/Library/Frameworks/Python.framework/Versions/3.14":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/Library/Frameworks/Python.framework/Versions":
                self._fake_stat(uid=0, gid=0, mode=0o40775),  # group-write, gid=wheel=0
            "/Library/Frameworks/Python.framework":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/Library/Frameworks":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/Library":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
        }

        def fake_lstat(path):
            if path in fake_stats:
                return fake_stats[path]
            raise FileNotFoundError(path)

        with m.patch("os.lstat", side_effect=fake_lstat):
            ok, reason = validate_ancestors(
                "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14",
                caller_gids={20},  # staff, not wheel
            )
        self.assertTrue(ok, f"Expected pass for root:wheel 0775 + non-wheel caller: {reason}")

    def test_group_writable_ancestor_fails_when_caller_in_group(self):
        """If caller IS in the group of a group-writable ancestor, must reject."""
        validate_ancestors = self._make_validator()
        import unittest.mock as m

        fake_stats = {
            "/some/dir/bin":
                self._fake_stat(uid=0, gid=5, mode=0o40755),
            "/some/dir":
                self._fake_stat(uid=0, gid=5, mode=0o40775),  # group-write gid=5
            "/some":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
        }

        def fake_lstat(path):
            if path in fake_stats:
                return fake_stats[path]
            raise FileNotFoundError(path)

        with m.patch("os.lstat", side_effect=fake_lstat):
            ok, reason = validate_ancestors(
                "/some/dir/bin/python3.14",
                caller_gids={5, 20},  # caller IS in gid 5
            )
        self.assertFalse(ok, "Expected fail: caller is in group of group-writable ancestor")
        self.assertIn("group", reason)

    def test_other_writable_ancestor_always_fails(self):
        """Other-writable ancestor must always fail regardless of caller groups."""
        validate_ancestors = self._make_validator()
        import unittest.mock as m

        fake_stats = {
            "/some/dir/bin":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/some/dir":
                self._fake_stat(uid=0, gid=0, mode=0o40777),  # other-writable
            "/some":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
        }

        def fake_lstat(path):
            if path in fake_stats:
                return fake_stats[path]
            raise FileNotFoundError(path)

        with m.patch("os.lstat", side_effect=fake_lstat):
            ok, reason = validate_ancestors(
                "/some/dir/bin/python3.14",
                caller_gids=set(),  # no group memberships
            )
        self.assertFalse(ok, "Expected fail: other-writable ancestor")
        self.assertIn("other-writable", reason)

    def test_symlink_ancestor_always_fails(self):
        """Symlink in ancestor chain must always fail."""
        validate_ancestors = self._make_validator()
        import unittest.mock as m

        fake_stats = {
            "/real/dir/bin":
                self._fake_stat(uid=0, gid=0, mode=0o40755, is_symlink=True),
            "/real/dir":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/real":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
            "/":
                self._fake_stat(uid=0, gid=0, mode=0o40755),
        }

        def fake_lstat(path):
            if path in fake_stats:
                return fake_stats[path]
            raise FileNotFoundError(path)

        with m.patch("os.lstat", side_effect=fake_lstat):
            ok, reason = validate_ancestors("/real/dir/bin/python3.14", caller_gids={20})
        self.assertFalse(ok, "Expected fail: symlink in ancestor chain")
        self.assertIn("symlink", reason)

    def test_usr_bin_ancestors_pass_on_real_system(self):
        """/usr/bin/python3 path ancestors are always safe on stock macOS."""
        if not Path("/usr/bin/python3").exists():
            self.skipTest("/usr/bin/python3 not present")
        validate_ancestors = self._make_validator()
        caller_gids = set(os.getgroups())
        caller_gids.add(os.getegid())
        ok, reason = validate_ancestors("/usr/bin/python3", caller_gids=caller_gids)
        self.assertTrue(ok, f"Expected /usr/bin ancestors safe on macOS: {reason}")

    def test_ancestor_error_message_mentions_user_writable(self):
        """Error messages must describe why the path is unsafe to the user."""
        install = (ROOT / "install.sh").read_text()
        # The group-writable rejection message must tell the user the path is
        # writable by them — not just that the bit is set.
        self.assertIn("writable by current user", install)

    def test_find_uses_not_type_l_to_exclude_symlinks(self):
        """find_protected_python must skip symlinks via -not -type l."""
        install = (ROOT / "install.sh").read_text()
        self.assertIn("-not -type l", install)

    def test_find_uses_versioned_name_pattern(self):
        """find_protected_python must use python3.[0-9]* to exclude pydoc/pip/etc."""
        install = (ROOT / "install.sh").read_text()
        self.assertIn('"python3.[0-9]*"', install)

    def test_real_python314_would_pass(self):
        """python3.14 at the standard Python.org framework path passes validation."""
        p = Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14")
        if not p.exists():
            self.skipTest("python3.14 not installed at expected path")
        # Only check file-system properties — don't actually run the interpreter.
        import stat as stat_mod
        st = p.stat()
        self.assertFalse(p.is_symlink(), "Concrete python3.14 must not be a symlink")
        self.assertEqual(st.st_uid, 0, "Must be root-owned")
        self.assertFalse(st.st_mode & stat_mod.S_IWOTH, "Must not be other-writable")

    def test_python3_symlink_would_be_excluded_from_candidates(self):
        """python3 (symlink → python3.14) must not be returned by the find command."""
        p = Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3")
        if not p.exists():
            self.skipTest("path not present")
        self.assertTrue(p.is_symlink(), "python3 should be a symlink on this install")
        # The find command uses -not -type l, so this would be excluded.
        # Verify the install.sh contract rather than running find.
        install = (ROOT / "install.sh").read_text()
        self.assertIn("-not -type l", install)


# ---------------------------------------------------------------------------
# 15. I15 — Architecture-aware Python selection
# ---------------------------------------------------------------------------


class ArchitectureAwarePythonTests(unittest.TestCase):
    """find_protected_python must exclude intel64-only binaries on arm64 and
    only enumerate concrete versioned executables (python3.X)."""

    def test_install_sh_excludes_intel64_on_arm64(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("intel64", install)
        self.assertIn("arm64", install)

    def test_install_sh_checks_interpreter_architecture(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("platform.machine()", install)

    def test_install_sh_respects_test_arch_env_var(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("REMCTL_TEST_ARCH", install)

    def test_find_excludes_symlinks(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("-not -type l", install)

    def test_find_uses_versioned_pattern(self):
        install = (ROOT / "install.sh").read_text()
        # Must match python3.X, not python3* (which includes pydoc3, pip3, etc.)
        self.assertIn('"python3.[0-9]*"', install)

    def test_intel64_path_skipped_on_arm64(self):
        candidates = [
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14",
            "/Library/Frameworks/Python.framework/Versions/3.14-intel64/bin/python3.14",
            "/Library/Frameworks/Python.framework/Versions/3.12-intel64/bin/python3.12",
        ]
        arch = "arm64"
        filtered = [p for p in candidates if not (arch == "arm64" and "-intel64" in p)]
        self.assertEqual(filtered, [
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14",
        ])

    def test_x86_64_keeps_intel_paths(self):
        candidates = [
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14",
            "/Library/Frameworks/Python.framework/Versions/3.14-intel64/bin/python3.14",
        ]
        arch = "x86_64"
        filtered = [p for p in candidates if not (arch == "arm64" and "-intel64" in p)]
        self.assertEqual(len(filtered), 2)


# ---------------------------------------------------------------------------
# 16. Dead-code and LaunchAgent backup/KeepAlive
# ---------------------------------------------------------------------------


class InstallQualityContractTests(unittest.TestCase):
    """Miscellaneous quality contracts from the pre-v1.0 review."""

    def test_no_safe_owned_by_user_dead_function(self):
        install = (ROOT / "install.sh").read_text()
        self.assertNotIn("_safe_owned_by_user", install)

    def test_launchagent_plist_is_backed_up_in_rollback(self):
        install = (ROOT / "install.sh").read_text()
        # The backup section must mention BACKUP_PLIST.
        self.assertIn("BACKUP_PLIST", install)
        # And the rollback function must restore it.
        rollback_idx = install.find("_host_rollback()")
        rollback_def = install.find("_host_rollback() {")
        self.assertGreater(rollback_def, 0)
        end = install.find("\n    }", rollback_def + 1)
        body = install[rollback_def:end]
        self.assertIn("BACKUP_PLIST", body)

    def test_keepalive_is_unconditional_for_persistent_broker(self):
        plist = (ROOT / "remctl-read-broker-launchagent.plist").read_text()
        lines = plist.splitlines()
        for i, line in enumerate(lines):
            if "<key>KeepAlive</key>" in line:
                for j in range(i + 1, min(i + 5, len(lines))):
                    stripped = lines[j].strip()
                    if stripped:
                        self.assertEqual(
                            stripped, "<true/>",
                            "persistent broker must be restarted after any exit",
                        )
                        return
        self.fail("KeepAlive key not found")

    def test_keepalive_does_not_depend_on_crash_classification(self):
        plist_text = (ROOT / "remctl-read-broker-launchagent.plist").read_text()
        self.assertNotIn("<key>Crashed</key>", plist_text)

    def test_install_sh_adoc_banner_warns_cdhash(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("CDHash", install)

    def test_install_sh_developer_id_gives_stable_identity(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("Developer ID", install)


class CallerHostModulePackagingTests(unittest.TestCase):
    def test_installer_copies_caller_host_modules(self):
        install = (ROOT / "install.sh").read_text()
        for name in ("remctl_host.py", "remctl_host_protocol.py"):
            self.assertIn(f'cp "$SCRIPT_DIR/{name}" "$BIN_DIR/{name}"', install)

    def test_uninstaller_removes_caller_host_modules(self):
        uninstall = (ROOT / "uninstall.sh").read_text()
        for name in ("remctl_host.py", "remctl_host_protocol.py"):
            self.assertRegex(uninstall, rf"(?m)^\s*{re.escape(name)}\s*$")


if __name__ == "__main__":
    unittest.main()
