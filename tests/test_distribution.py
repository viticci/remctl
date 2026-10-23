"""Trust boundaries and install behavior for the portable distribution."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_distribution as distribution
import local_signing


class PluginLauncherTests(unittest.TestCase):
    LAUNCHERS = ("plugins/remctl/mcp.json", "plugins/claude-code/.mcp.json")

    def launcher(self, relative):
        return json.loads((ROOT / relative).read_text())["mcpServers"]["remctl"]

    def run_launcher(self, config, home, *, path="/usr/bin:/bin", extra=None):
        env = {"HOME": str(home), "PATH": path, **(extra or {})}
        return subprocess.run([config["command"], *config["args"]], env=env,
                              capture_output=True, text=True, timeout=30)

    def fake_cli(self, path, name):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nprintf "{name} %s\\n" "$@"\n')
        path.chmod(0o755)

    def test_discoverable_launcher_uses_installed_cli_without_external_python(self):
        for relative in self.LAUNCHERS:
            config = self.launcher(relative)
            # Agent Plugins rejects absolute commands before starting the MCP server.
            self.assertRegex(config["command"], r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
            # Claude Code expands ${VAR} in .mcp.json itself; leave every variable to sh.
            self.assertNotIn("${", json.dumps(config["args"]))
            with self.subTest(launcher=relative), tempfile.TemporaryDirectory(prefix="remctl home ") as directory:
                self.fake_cli(Path(directory) / "bin/remctl", "home-bin")
                result = self.run_launcher(config, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "home-bin mcp\n")

    def test_launcher_finds_a_local_bin_install(self):
        # install.sh with REMCTL_BIN_DIR=~/.local/bin, the other common layout.
        for relative in self.LAUNCHERS:
            with self.subTest(launcher=relative), tempfile.TemporaryDirectory(prefix="remctl home ") as directory:
                self.fake_cli(Path(directory) / ".local/bin/remctl", "local-bin")
                result = self.run_launcher(self.launcher(relative), directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "local-bin mcp\n")

    def test_launcher_prefers_remctl_bin_dir_then_path(self):
        for relative in self.LAUNCHERS:
            config = self.launcher(relative)
            with self.subTest(launcher=relative), tempfile.TemporaryDirectory(prefix="remctl home ") as directory:
                home = Path(directory)
                self.fake_cli(home / "bin/remctl", "home-bin")
                self.fake_cli(home / "custom dir/remctl", "custom")
                result = self.run_launcher(config, home, extra={"REMCTL_BIN_DIR": str(home / "custom dir")})
                self.assertEqual(result.stdout, "custom mcp\n")
                (home / "bin/remctl").unlink()
                self.fake_cli(home / "elsewhere/remctl", "on-path")
                result = self.run_launcher(config, home, path=f"{home / 'elsewhere'}:/usr/bin:/bin")
                self.assertEqual(result.stdout, "on-path mcp\n")

    def test_launcher_explains_a_missing_install(self):
        for relative in self.LAUNCHERS:
            with self.subTest(launcher=relative), tempfile.TemporaryDirectory(prefix="remctl home ") as directory:
                result = self.run_launcher(self.launcher(relative), directory)
                self.assertEqual(result.returncode, 127)
                self.assertIn("install.sh", result.stderr)
                self.assertEqual(result.stdout, "")


class RuntimeArchiveTests(unittest.TestCase):
    def archive(self, directory, members):
        path = Path(directory) / "runtime.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, kind, payload in members:
                entry = tarfile.TarInfo(name)
                if kind == "symlink":
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = payload
                    archive.addfile(entry)
                else:
                    data = payload.encode()
                    entry.size = len(data)
                    archive.addfile(entry, io.BytesIO(data))
        return path

    def test_rejects_traversal_before_writing_any_member(self):
        cases = [
            [("python/ok", "file", "ok"), ("python/../../escape", "file", "bad")],
            [("python/link", "symlink", "/tmp"), ("python/link/escape", "file", "bad")],
            [("python/link", "symlink", "lib"), ("python/link/escape", "file", "bad")],
            [("python/ok", "file", "a"), ("python/ok", "file", "b")],
        ]
        for members in cases:
            with self.subTest(members=members), tempfile.TemporaryDirectory() as directory:
                archive = self.archive(directory, members)
                out = Path(directory) / "out"
                out.mkdir()
                with self.assertRaises(ValueError):
                    distribution.extract_runtime(archive, out)
                self.assertEqual(list(out.iterdir()), [])

    def test_allows_internal_symlink_and_manifest_detects_changed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.archive(directory, [("python/bin/python3.13", "file", "binary"), ("python/bin/python3", "symlink", "python3.13")])
            root = distribution.extract_runtime(archive, Path(directory) / "out")
            before = distribution.tree_manifest(root)
            self.assertEqual(before["bin/python3"], {"type": "symlink", "target": "python3.13"})
            (root / "bin/python3.13").write_text("changed")
            self.assertNotEqual(before, distribution.tree_manifest(root))


FAKE_SEARCH_LIST = """#!/bin/sh
# Fake `security list-keychains -d user [-s KEYCHAIN...]` backed by a file.
state="$(dirname "$0")/search-list"
shift 3
if [ "$1" = "-s" ]; then
  shift
  : > "$state"
  for keychain in "$@"; do printf '    "%s"\\n' "$keychain" >> "$state"; done
else
  cat "$state"
fi
"""

SIGNER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import local_signing
local_signing.SEARCH_LIST = tuple(sys.argv[3:])
print('ready', flush=True)
with local_signing.signing_keychain(sys.argv[2]):
    print('entered', flush=True)
    if sys.argv[2].endswith('wait'):
        time.sleep(30)
"""


@unittest.skipUnless(sys.platform == "darwin", "macOS signing")
class LocalSigningTests(unittest.TestCase):
    """Search-list changes go to a fake; the real user search list is only read."""

    def setUp(self):
        self.real_search_list = local_signing.run("/usr/bin/security", "list-keychains", "-d", "user")
        directory = tempfile.TemporaryDirectory(prefix="rctl-search-list-")
        self.addCleanup(directory.cleanup)
        fake = Path(directory.name) / "security"
        fake.write_text(FAKE_SEARCH_LIST)
        fake.chmod(0o755)
        (fake.parent / "search-list").write_bytes(self.real_search_list)
        self.fake_search_list = (str(fake), "list-keychains", "-d", "user")
        original = local_signing.SEARCH_LIST
        local_signing.SEARCH_LIST = self.fake_search_list
        self.addCleanup(setattr, local_signing, "SEARCH_LIST", original)

    def tearDown(self):
        self.assertEqual(self.real_search_list, local_signing.run("/usr/bin/security", "list-keychains", "-d", "user"))

    def search_list(self):
        return local_signing.run(*self.fake_search_list)

    def signer(self, keychain):
        return subprocess.Popen(
            [sys.executable, "-c", SIGNER, str(ROOT / "scripts"), keychain, *self.fake_search_list],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_concurrent_signers_wait_before_reading_the_search_list(self):
        before = self.search_list()
        # Cover both a shared keychain and independent signing directories.
        for keychain in ("/tmp/rctl-first.keychain-db", "/tmp/rctl-second.keychain-db"):
            with self.subTest(keychain=keychain):
                child = None
                try:
                    with local_signing.signing_keychain("/tmp/rctl-first.keychain-db"):
                        self.assertIn(b"rctl-first", self.search_list())
                        child = self.signer(keychain)
                        self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                        self.assertEqual(child.stdout.readline(), b"ready\n")
                        self.assertFalse(select.select([child.stdout], [], [], 0.25)[0],
                                         "Concurrent signer entered before the first restored the list")
                    stdout, stderr = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 0, stderr.decode())
                    self.assertEqual(stdout, b"entered\n")
                    self.assertEqual(before, self.search_list())
                finally:
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.communicate()

    def test_search_list_is_restored_when_signing_is_interrupted(self):
        before = self.search_list()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                child = self.signer("/tmp/rctl-interrupted.keychain-wait")
                try:
                    self.assertEqual(child.stdout.readline(), b"ready\n")
                    self.assertEqual(child.stdout.readline(), b"entered\n")
                    self.assertIn(b"rctl-interrupted", self.search_list())
                    child.send_signal(signum)
                    child.communicate(timeout=10)
                    self.assertNotEqual(child.returncode, 0)
                    self.assertEqual(before, self.search_list())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()

    def test_identity_survives_rebuild_and_rejects_a_different_key(self):
        before = self.search_list()
        with tempfile.TemporaryDirectory(prefix="rctl-sign-") as temporary:
            root = Path(temporary).resolve()
            first = local_signing.identity(root / "first")
            second = local_signing.identity(root / "second")
            try:
                self.assertEqual(first, local_signing.identity(root / "first"))
                binaries = []
                for name, identity in (("first", first), ("rebuilt", first), ("other", second)):
                    binary = root / name / "probe"
                    binary.parent.mkdir(exist_ok=True)
                    shutil.copyfile("/usr/bin/false" if name == "rebuilt" else "/usr/bin/true", binary)
                    binary.chmod(0o755)
                    distribution.sign(binary, identity, False, executable=True)
                    self.assertEqual(before, self.search_list())
                    binaries.append(binary)
                with self.assertRaises(subprocess.CalledProcessError):
                    distribution.sign(root / "missing-binary", first, False, executable=True)
                self.assertEqual(before, self.search_list())
                requirement = '=certificate leaf = H"' + first["identity"].lower() + '"'
                for binary in binaries[:2]:
                    subprocess.run(["codesign", "--verify", "--strict", "-R", requirement, str(binary)], check=True, capture_output=True)
                rejected = subprocess.run(["codesign", "--verify", "--strict", "-R", requirement, str(binaries[2])], capture_output=True)
                self.assertNotEqual(rejected.returncode, 0)
                (root / "first/password").chmod(0o644)
                with self.assertRaises(ValueError):
                    local_signing.identity(root / "first")
            finally:
                for identity in (first, second):
                    local_signing.run("/usr/bin/security", "delete-keychain", identity["keychain"])
        self.assertEqual(before, self.search_list())


class PrebuiltCopyTests(unittest.TestCase):
    def test_prebuilt_app_is_verified_from_a_private_copy_that_is_removed(self):
        # sudo may not reach an app on a user-mounted disk image, so the
        # installer checks and runs a copy. An unsigned app stops at the check.
        with tempfile.TemporaryDirectory(prefix="rd-", dir="/private/tmp") as temporary:
            root = Path(temporary)
            app = root / "mounted" / distribution.APP_NAME
            (app / "Contents/MacOS").mkdir(parents=True)
            (root / "tmp").mkdir()
            env = dict(os.environ, PREFIX=str(root / "prefix"), TMPDIR=str(root / "tmp"),
                       REMCTL_LAUNCH_AGENT_DIR=str(root / "prefix/agents"), REMCTL_SKIP_LAUNCHSERVICES="1")
            result = subprocess.run([str(ROOT / "install.sh"), "--prebuilt", str(app), "--dry-run"],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid app signature", result.stderr)
            self.assertIn("/remctl-prebuilt.", result.stderr)
            self.assertEqual(list((root / "tmp").iterdir()), [])


@unittest.skipUnless(os.environ.get("REMCTL_TEST_DISTRIBUTION_APP"), "set REMCTL_TEST_DISTRIBUTION_APP to a built app")
class PrebuiltInstallTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(os.environ["REMCTL_TEST_DISTRIBUTION_APP"]).resolve()
        self.temporary = tempfile.TemporaryDirectory(prefix="rd-", dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.env = dict(os.environ, PREFIX=str(self.root), REMCTL_LAUNCH_AGENT_DIR=str(self.root / "agents"), REMCTL_SKIP_LAUNCHSERVICES="1", REMCTL_CONFIG_DIR=str(self.root / ".config/remctl"))
        self.env.pop("REMCTL_CAPABILITY_PYTHON", None)
        self.python = self.app / "Contents/Resources/Python/bin/python3.13"

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, *extra):
        return subprocess.run([str(ROOT / "install.sh"), "--prebuilt", str(self.app), "--allow-local-build", "--shell-completions", "none", *extra], env=self.env, capture_output=True, text=True, timeout=120)

    def test_install_upgrade_rollback_and_uninstall(self):
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        client = self.root / "bin/remctl"
        version = subprocess.run([str(self.python), "-B", str(client), "--version"], capture_output=True, text=True)
        self.assertEqual(version.returncode, 0, version.stderr)
        manifest = (self.root / "bin/.remctl-install-manifest.json").read_bytes()
        self.env["REMCTL_TEST_PUBLISH_FAIL_AT"] = "3"
        failed = self.install()
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((self.root / "bin/.remctl-install-manifest.json").read_bytes(), manifest)
        self.assertEqual(list(self.root.rglob("*.remctl-transaction-backup")), [])
        del self.env["REMCTL_TEST_PUBLISH_FAIL_AT"]
        upgraded = self.install()
        self.assertEqual(upgraded.returncode, 0, upgraded.stdout + upgraded.stderr)
        # Running installation checks must never modify the signed input bundle.
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(self.app)], check=True, capture_output=True)
        removed = subprocess.run([str(ROOT / "uninstall.sh"), "--keep-config"], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(client.exists())
        self.assertFalse((self.root / "Applications" / distribution.APP_NAME).exists())

    def test_default_rejects_local_signature_before_installing(self):
        result = subprocess.run([str(ROOT / "install.sh"), "--prebuilt", str(self.app), "--dry-run"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Developer ID", result.stderr)
        self.assertFalse((self.root / "bin/remctl").exists())

    def test_rejects_tampered_app_before_installing(self):
        copy = self.root / distribution.APP_NAME
        shutil.copytree(self.app, copy, symlinks=True)
        (copy / "Contents/Resources/Client/remctl").write_text("tampered")
        self.app = copy
        result = self.install("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid app signature", result.stderr)
        self.assertFalse((self.root / "bin/remctl").exists())

    def test_runtime_install_requires_admin(self):
        if os.geteuid() == 0:
            self.skipTest("requires an ordinary user")
        # A valid manifest with a new content address has no installed runtime.
        app = self.root / distribution.APP_NAME
        shutil.copytree(self.app, app, symlinks=True)
        resources = app / "Contents/Resources"
        manifest = resources / "python-manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        destination = Path(f"/Library/RemCTL/Python/{digest}")
        self.assertFalse(destination.exists())
        (resources / "remctl-capability-python-path").write_text(str(destination / "bin/python3.13") + "\n")
        identity = local_signing.identity(self.root / "runtime-signing")
        try:
            distribution.sign(app, identity, False, executable=True)
            result = subprocess.run([str(app / "Contents/MacOS/RemCTL Capability Host"), "--install-python-runtime"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertIn("administrator authorization", result.stderr)
            self.assertFalse(destination.exists())
            entries = json.loads(manifest.read_bytes())
            file_entry = next(entry for entry in entries.values() if entry["type"] == "file")
            file_entry["sha256"] = "0" * 64
            manifest.write_text(json.dumps(entries))
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            (resources / "remctl-capability-python-path").write_text(f"/Library/RemCTL/Python/{digest}/bin/python3.13\n")
            distribution.sign(app, identity, False, executable=True)
            invalid = subprocess.run([str(app / "Contents/MacOS/RemCTL Capability Host"), "--install-python-runtime"], capture_output=True, text=True, timeout=30)
            self.assertEqual(invalid.returncode, 65, invalid.stderr)
            self.assertNotIn("administrator authorization", invalid.stderr)
        finally:
            local_signing.run("/usr/bin/security", "delete-keychain", identity["keychain"])

    def test_existing_runtime_is_verified_without_admin(self):
        python = Path((self.app / "Contents/Resources/remctl-capability-python-path").read_text().strip())
        if not python.exists():
            self.skipTest("requires this app's protected runtime to be installed")
        result = subprocess.run([str(self.app / "Contents/MacOS/RemCTL Capability Host"), "--install-python-runtime"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already installed", result.stdout)

    def test_signing_migration_requires_explicit_choice(self):
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        identity = local_signing.identity(self.root / "new-signing")
        try:
            copy = self.root / "different" / distribution.APP_NAME
            shutil.copytree(self.app, copy, symlinks=True)
            distribution.sign(copy, identity, False, executable=True)
            self.app = copy
            # The real dry-run path enforces continuity without launching a job.
            del self.env["REMCTL_SKIP_LAUNCHSERVICES"]
            rejected = self.install("--dry-run")
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertIn("--migrate-signing", rejected.stdout + rejected.stderr)
            accepted = self.install("--dry-run", "--migrate-signing")
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        finally:
            local_signing.run("/usr/bin/security", "delete-keychain", identity["keychain"])


if __name__ == "__main__":
    unittest.main()
