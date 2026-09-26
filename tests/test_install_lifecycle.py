from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from remctl_capability_policy import PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"
LAUNCH_AGENT = ROOT / "remctl-capability-host-launchagent.plist"
APP_NAME = "RemCTL Capability Host.app"
LABEL = "net.macstories.remctl.capability-host"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(bin_dir: Path, relative_paths: list[str]) -> None:
    entries = {}
    for relative in relative_paths:
        path = bin_dir / relative
        if path.is_symlink():
            entries[relative] = {"type": "symlink", "target": os.readlink(path)}
        else:
            entries[relative] = {"type": "file", "sha256": sha256(path)}
    (bin_dir / ".remctl-install-manifest.json").write_text(
        json.dumps({"version": 1, "entries": entries}, sort_keys=True) + "\n"
    )
    (bin_dir / ".remctl-install-manifest.json").chmod(0o600)


@unittest.skipUnless(sys.platform == "darwin", "macOS installer test")
class InstallerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        candidates = [Path(sys.executable)] + [
            Path(path)
            for path in (
                "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
                "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
                "/usr/local/bin/python3.13",
                "/usr/local/bin/python3.14",
                "/opt/homebrew/bin/python3.13",
                "/opt/homebrew/bin/python3.14",
            )
        ]
        cls.capability_python = next(
            (
                candidate.resolve()
                for candidate in candidates
                if candidate.is_file()
                and subprocess.run(
                    [str(candidate), "-I", "-S", "-c", "import sys;raise SystemExit(sys.version_info<(3,13))"],
                    check=False,
                ).returncode
                == 0
            ),
            None,
        )
        if cls.capability_python is None:
            raise unittest.SkipTest("Python 3.13+ is required")
        for tool in ("swiftc", "clang", "codesign", "plutil"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} is required")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rctl-", dir="/private/tmp")
        self.prefix = Path(self.temporary.name)
        self.bin = self.prefix / "bin"
        self.apps = self.prefix / "Applications"
        self.agents = self.prefix / "Library" / "LaunchAgents"
        self.config = self.prefix / "config" / "remctl"
        self.app = self.apps / APP_NAME
        self.agent = self.agents / f"{LABEL}.plist"
        self.socket = self.prefix / "Library" / "Application Support" / "RemCTL" / "capability-host.sock"
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PREFIX": str(self.prefix),
                "REMCTL_BIN_DIR": str(self.bin),
                "REMCTL_APP_DIR": str(self.apps),
                "REMCTL_LAUNCH_AGENT_DIR": str(self.agents),
                "REMCTL_CONFIG_DIR": str(self.config),
                "REMCTL_SKIP_LAUNCHSERVICES": "1",
                "REMCTL_CAPABILITY_PYTHON": str(self.capability_python),
                "REMCTL_CODESIGN_IDENTITY": "-",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": str(self.prefix / "tmp"),
            }
        )
        (self.prefix / "tmp").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_script(
        self,
        script: Path,
        *arguments: str,
        environment: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(script), *arguments],
            cwd=ROOT,
            env=environment or self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=90,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"{script.name} exited {result.returncode}\n"
                f"--- captured output ---\n{result.stdout}"
            )
        return result

    def assert_no_backups(self) -> None:
        backups = list(self.prefix.rglob("*.remctl-transaction-backup"))
        self.assertEqual(backups, [])

    def test_help_and_terminal_guidance_distinguish_host_permissions(self) -> None:
        install_help = self.run_script(INSTALL, "--help").stdout
        uninstall_help = self.run_script(UNINSTALL, "--help").stdout
        install_source = INSTALL.read_text()
        uninstall_source = UNINSTALL.read_text()

        self.assertIn("command-line client supports Python 3.10+", install_help)
        self.assertIn("signed host requires", install_help)
        self.assertIn("after an authorized upgrade/reinstall", install_help)
        self.assertIn("Reminders, and Automation", uninstall_help)
        self.assertIn("First capability-host install: run", install_source)
        self.assertIn("remctl onboard", install_source)
        self.assertIn("opens the exact-host Full Disk Access guide only if needed", install_source)
        self.assertIn("If you change Full Disk Access", install_source)
        self.assertIn("restart the host", install_source)
        self.assertIn("then run '$BIN_DIR/remctl doctor'", install_source)
        self.assertIn("Upgrade/reinstall complete", install_source)
        self.assertIn("existing permission grants remain valid", install_source)
        self.assertIn("only to reopen or repair the exact-host Full Disk Access guide", install_source)
        self.assertIn("remctl permissions full-disk-access", install_source)
        self.assertIn("Resolve the reported checks", install_source)
        self.assertIn("Full Disk Access, Reminders, and Automation grants were not reset", uninstall_source)

    def test_custom_prefix_agent_migrates_and_rolls_back(self) -> None:
        self.run_script(INSTALL, "--shell-completions", "none")
        legacy = self.agent
        original = legacy.read_bytes()
        environment = self.environment.copy()
        environment["HOME"] = str(self.prefix / "home")
        environment.pop("REMCTL_LAUNCH_AGENT_DIR")
        destination = Path(environment["HOME"]) / "Library/LaunchAgents" / f"{LABEL}.plist"
        destination.parent.mkdir(parents=True)
        new_backup = Path(str(destination) + ".remctl-transaction-backup")
        new_backup.write_bytes(original)
        blocked = self.run_script(UNINSTALL, "--keep-config", environment=environment, check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Unresolved installer backup", blocked.stdout)
        self.assertTrue(self.app.exists())
        self.assertEqual(legacy.read_bytes(), original)
        new_backup.unlink()
        self.run_script(UNINSTALL, "--dry-run", "--keep-config", environment=environment)

        # An unrelated file at the old path must never be adopted or removed.
        legacy.write_text("foreign plist")
        refused = self.run_script(INSTALL, "--shell-completions", "none", environment=environment, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Refusing to migrate", refused.stdout)
        self.assertEqual(legacy.read_text(), "foreign plist")
        legacy.write_bytes(original)

        failed_env = dict(environment, REMCTL_TEST_PUBLISH_FAIL_AT="3")
        failed = self.run_script(INSTALL, "--shell-completions", "none", environment=failed_env, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertFalse(destination.exists())
        self.assert_installed_contract()
        self.assert_no_backups()

        self.run_script(INSTALL, "--shell-completions", "none", environment=environment)
        self.assertFalse(legacy.exists())
        self.agent = destination
        self.assert_installed_contract()
        self.assert_no_backups()
        # A committed migration can still leave an old-plist backup if cleanup stops.
        old_backup = Path(str(legacy) + ".remctl-transaction-backup")
        old_backup.write_bytes(original)
        for script in (INSTALL, UNINSTALL):
            blocked = self.run_script(script, environment=environment, check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("backup", blocked.stdout)
            self.assertTrue(self.app.exists())
            self.assertTrue(destination.exists())
            self.assertTrue(old_backup.exists())
        old_backup.unlink()
        self.run_script(UNINSTALL, "--keep-config", environment=environment)
        self.assertFalse(destination.exists())
        self.assertFalse(self.app.exists())

    def test_simulation_cannot_use_real_home_launchagents_by_default(self) -> None:
        environment = self.environment.copy()
        environment.pop("REMCTL_LAUNCH_AGENT_DIR")
        for script in (INSTALL, UNINSTALL):
            result = self.run_script(script, environment=environment, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires a LaunchAgent directory under PREFIX", result.stdout)
        self.assertFalse(self.app.exists())

    def test_custom_prefix_reinstall_repairs_missing_legacy_agent(self) -> None:
        self.run_script(INSTALL, "--shell-completions", "none")
        backup = Path(str(self.agent) + ".remctl-transaction-backup")
        self.agent.rename(backup)
        environment = self.environment.copy()
        environment["HOME"] = str(self.prefix / "home")
        environment.pop("REMCTL_LAUNCH_AGENT_DIR")
        blocked = self.run_script(INSTALL, "--shell-completions", "none", environment=environment, check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Stale transaction backup", blocked.stdout)
        self.assertTrue(backup.exists())
        backup.unlink()
        self.run_script(UNINSTALL, "--dry-run", "--keep-config", environment=environment)
        self.run_script(INSTALL, "--shell-completions", "none", environment=environment)
        self.agent = Path(environment["HOME"]) / "Library/LaunchAgents" / f"{LABEL}.plist"
        self.assert_installed_contract()
        self.run_script(UNINSTALL, "--keep-config", environment=environment)
        self.assertFalse(self.agent.exists())

    def test_custom_prefix_new_install_uses_home_launchagents(self) -> None:
        environment = self.environment.copy()
        environment["HOME"] = str(self.prefix / "home")
        environment.pop("REMCTL_LAUNCH_AGENT_DIR")
        self.run_script(INSTALL, "--shell-completions", "none", environment=environment)
        self.assertFalse(self.agent.exists())
        self.agent = Path(environment["HOME"]) / "Library/LaunchAgents" / f"{LABEL}.plist"
        self.assert_installed_contract()
        self.run_script(UNINSTALL, "--keep-config", environment=environment)
        self.assertFalse(self.agent.exists())

    def test_bootstrap_rejects_doctor_before_install_work(self) -> None:
        result = self.run_script(INSTALL, "--bootstrap", "--doctor", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("--bootstrap and --doctor cannot be combined", result.stdout)
        self.assertIn("remctl onboard", result.stdout)
        self.assertIn("complete any Full Disk Access step", result.stdout)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.app.exists())
        self.assertFalse(self.agent.exists())
        self.assertFalse(self.config.exists())

    def test_launchagent_template_uses_standard_process_class(self) -> None:
        with LAUNCH_AGENT.open("rb") as handle:
            agent = plistlib.load(handle)
        self.assertNotIn("ProcessType", agent)
        self.assertEqual(agent["Label"], LABEL)
        self.assertEqual(
            agent["ProgramArguments"],
            [
                "__REMCTL_CAPABILITY_HOST__",
                "--run-capability-host",
                "--socket",
                "__REMCTL_CAPABILITY_HOST_SOCKET__",
            ],
        )
        self.assertIs(agent["RunAtLoad"], True)
        self.assertIs(agent["KeepAlive"], True)
        self.assertEqual(agent["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(agent["Umask"], 0o77)
        self.assertEqual(agent["StandardOutPath"], "/dev/null")
        self.assertEqual(agent["StandardErrorPath"], "/dev/null")

    def assert_installed_contract(self) -> None:
        host = self.app / "Contents" / "MacOS" / "RemCTL Capability Host"
        resources = self.app / "Contents" / "Resources"
        self.assertTrue(host.is_file())
        self.assertTrue(os.access(host, os.X_OK))
        self.assertTrue((resources / "CapabilityRuntime" / "bin" / "remctl-bridge").is_file())
        self.assertTrue((resources / "CapabilityRuntime" / "bin" / "remctl-private").is_file())
        self.assertEqual(
            (resources / "remctl-capability-python-path").read_text().strip(),
            str(self.capability_python),
        )
        self.assertEqual(
            (resources / "remctl-capability-host-socket-path").read_text().strip(),
            str(self.socket),
        )
        self.assertEqual(
            (resources / "remctl-capability-host-launch-agent-path").read_text().strip(),
            str(self.agent),
        )
        self.assertEqual(
            (resources / "remctl-capability-host-launch-agent-path").stat().st_mode & 0o777,
            0o644,
        )
        runtime_manifest = json.loads(
            (resources / "remctl-capability-runtime.json").read_text(encoding="utf-8")
        )
        self.assertEqual(runtime_manifest["protocolVersion"], PROTOCOL_VERSION)
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(self.app)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        with (self.app / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIdentifier"], LABEL)
        self.assertIs(info["LSUIElement"], True)
        self.assertNotIn("LSBackgroundOnly", info)
        with self.agent.open("rb") as handle:
            agent = plistlib.load(handle)
        self.assertEqual(self.agent.stat().st_mode & 0o777, 0o644)
        self.assertEqual(agent["Label"], LABEL)
        self.assertEqual(
            agent["ProgramArguments"],
            [str(host), "--run-capability-host", "--socket", str(self.socket)],
        )
        self.assertEqual(os.readlink(self.bin / "rctl"), "remctl")
        self.assertEqual(os.readlink(self.bin / "reminders"), "remctl")
        self.assertEqual((self.bin / ".remctl-capability-host-app").stat().st_mode & 0o777, 0o600)
        manifest = json.loads((self.bin / ".remctl-install-manifest.json").read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["entries"]["rctl"], {"type": "symlink", "target": "remctl"})
        for name in ("_remctl", "_rctl", "_reminders"):
            self.assertTrue((self.bin / "completions" / name).is_file())

    def test_staged_install_idempotence_rollback_and_uninstall(self) -> None:
        # Dry-run performs the expensive build and strict signature check but
        # does not publish an app, LaunchAgent, or CLI.
        dry = self.run_script(INSTALL, "--bootstrap", "--dry-run", "--shell-completions", "none")
        self.assertIn("Dry run complete", dry.stdout)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.agent.exists())
        self.assertFalse((self.bin / "remctl").exists())

        first_install = self.run_script(INSTALL, "--bootstrap", "--shell-completions", "none")
        self.assertIn("First capability-host install", first_install.stdout)
        self.assertIn("remctl onboard", first_install.stdout)
        self.assertNotIn("existing permission grants remain valid", first_install.stdout)
        self.assert_installed_contract()
        self.assert_no_backups()
        self.assertTrue(self.config.is_dir())

        # A requested stable identity must fail before publication and must not
        # silently fall back to an ad-hoc signature.
        old_host = sha256(self.app / "Contents" / "MacOS" / "RemCTL Capability Host")
        old_cli = sha256(self.bin / "remctl")
        invalid_identity = self.environment.copy()
        invalid_identity["REMCTL_CODESIGN_IDENTITY"] = "RemCTL Installer Invalid Identity"
        failed_sign = self.run_script(
            INSTALL,
            "--bootstrap",
            "--shell-completions",
            "none",
            environment=invalid_identity,
            check=False,
        )
        self.assertNotEqual(failed_sign.returncode, 0)
        self.assertEqual(sha256(self.app / "Contents" / "MacOS" / "RemCTL Capability Host"), old_host)
        self.assertEqual(sha256(self.bin / "remctl"), old_cli)

        # Reinstalling the same generation is supported and leaves no backup
        # files or staging directories behind.
        reinstall = self.run_script(INSTALL, "--bootstrap", "--shell-completions", "none")
        self.assertIn("Upgrade/reinstall complete", reinstall.stdout)
        self.assertIn("existing permission grants remain valid", reinstall.stdout)
        self.assertNotIn("First capability-host install", reinstall.stdout)
        self.assert_installed_contract()
        self.assert_no_backups()

        # Make the installed generation distinguishable, then inject a failure
        # after several renames. The journal must restore that exact generation.
        runtime = self.bin / "remctl_runtime.py"
        old_runtime = sha256(runtime)
        old_host = sha256(self.app / "Contents" / "MacOS" / "RemCTL Capability Host")
        injected = self.environment.copy()
        injected["REMCTL_TEST_PUBLISH_FAIL_AT"] = "5"
        failed_publish = self.run_script(
            INSTALL,
            "--bootstrap",
            "--shell-completions",
            "none",
            environment=injected,
            check=False,
        )
        self.assertNotEqual(failed_publish.returncode, 0)
        self.assertEqual(sha256(runtime), old_runtime)
        self.assertEqual(sha256(self.app / "Contents" / "MacOS" / "RemCTL Capability Host"), old_host)
        self.assert_no_backups()

        uninstall_dry = self.run_script(UNINSTALL, "--dry-run", "--keep-config")
        self.assertIn("Dry run complete", uninstall_dry.stdout)
        self.assertTrue(self.app.exists())
        self.assertTrue((self.bin / "remctl").exists())

        self.run_script(UNINSTALL, "--keep-config")
        self.assertFalse(self.app.exists())
        self.assertFalse(self.agent.exists())
        self.assertFalse((self.bin / "remctl").exists())
        self.assertFalse((self.bin / "rctl").exists())
        self.assertFalse((self.bin / "reminders").exists())
        self.assertTrue(self.config.is_dir())

    def test_first_install_transport_health_does_not_run_slow_permission_status(self) -> None:
        source = INSTALL.read_text()
        function = source.split("transport_available() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("remctl_broker.ping(timeout=10)", function)
        self.assertNotIn("remctl_broker.status", function)
        self.assertIn("wait_for_transport", source)

        self.bin.mkdir()
        ping_marker = self.prefix / "ping-called"
        status_marker = self.prefix / "status-called"
        (self.bin / "remctl_broker.py").write_text(
            "import os, time\n"
            "from pathlib import Path\n"
            "def ping(*, timeout):\n"
            "    Path(os.environ['PING_MARKER']).write_text(str(timeout))\n"
            f"    return {{'status': 'ok', 'protocolVersion': {PROTOCOL_VERSION!r}}}\n"
            "def status(*, timeout):\n"
            "    Path(os.environ['STATUS_MARKER']).write_text(str(timeout))\n"
            "    time.sleep(5)\n",
            encoding="utf-8",
        )
        command = re.search(r"-c '([^']+)'", function)
        self.assertIsNotNone(command)
        environment = self.environment.copy()
        environment.update(
            {"PING_MARKER": str(ping_marker), "STATUS_MARKER": str(status_marker)}
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", command.group(1), str(self.bin)],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ping_marker.read_text(), "10")
        self.assertFalse(status_marker.exists())

    def test_unowned_root_and_stale_backup_fail_closed(self) -> None:
        self.bin.mkdir()
        self.config.mkdir(parents=True)
        unrelated = self.bin / "remctl"
        unrelated.write_text("unrelated")
        settings = self.config / "settings.json"
        settings.write_text("{}")
        result = self.run_script(UNINSTALL, check=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(unrelated.read_text(), "unrelated")
        self.assertEqual(settings.read_text(), "{}")

        backup = self.bin / "remctl.remctl-transaction-backup"
        backup.write_text("recovery generation")
        blocked = self.run_script(UNINSTALL, check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Unresolved installer backup", blocked.stdout)
        self.assertEqual(backup.read_text(), "recovery generation")

    def test_installer_refuses_foreign_alias_before_build(self) -> None:
        self.bin.mkdir()
        alias = self.bin / "rctl"
        alias.write_text("foreign command")
        result = self.run_script(
            INSTALL,
            "--dry-run",
            "--shell-completions",
            "none",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to replace unrelated alias path", result.stdout)
        self.assertEqual(alias.read_text(), "foreign command")
        self.assertFalse(self.app.exists())

    def test_installer_and_uninstaller_refuse_modified_manifest_artifact(self) -> None:
        self.run_script(INSTALL, "--shell-completions", "none")
        for relative in (
            "remctl",
            "remctl-private",
            "completions/_remctl",
        ):
            path = self.bin / relative
            original = path.read_bytes()
            original_mode = path.stat().st_mode & 0o777
            path.write_bytes(b"foreign replacement\n")
            blocked = self.run_script(UNINSTALL, "--keep-config", check=False)
            self.assertNotEqual(blocked.returncode, 0, relative)
            self.assertEqual(path.read_bytes(), b"foreign replacement\n", relative)
            path.write_bytes(original)
            path.chmod(original_mode)

        foreign = self.bin / "remctl_runtime.py"
        foreign.write_text("# foreign replacement\n")

        reinstall = self.run_script(
            INSTALL, "--dry-run", "--shell-completions", "none", check=False
        )
        self.assertNotEqual(reinstall.returncode, 0)
        self.assertIn("foreign, modified, or unmanifested files", reinstall.stdout)
        self.assertIn("Ownership mismatch: remctl_runtime.py", reinstall.stdout)
        self.assertEqual(foreign.read_text(), "# foreign replacement\n")

        uninstall = self.run_script(UNINSTALL, "--keep-config", check=False)
        self.assertNotEqual(uninstall.returncode, 0)
        self.assertIn("without an exact installer ownership marker", uninstall.stdout)
        self.assertEqual(foreign.read_text(), "# foreign replacement\n")
        self.assertTrue(self.app.exists())

    def test_unmanifested_host_requires_explicit_one_time_adoption(self) -> None:
        self.run_script(INSTALL, "--shell-completions", "none")
        (self.bin / ".remctl-install-manifest.json").unlink()

        refused = self.run_script(
            INSTALL, "--dry-run", "--shell-completions", "none", check=False
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--adopt-existing-install", refused.stdout)

        adopted = self.run_script(
            INSTALL,
            "--adopt-existing-install",
            "--shell-completions",
            "none",
        )
        self.assertEqual(adopted.returncode, 0)
        self.assert_installed_contract()

    def test_failed_rollback_preserves_recovery_evidence(self) -> None:
        self.run_script(INSTALL, "--shell-completions", "none")
        injected = self.environment.copy()
        injected["REMCTL_TEST_PUBLISH_FAIL_AT"] = "5"
        # -4 = the fourth published pair (remctl_runtime.py) counted from the oldest
        # journal entry, so the injected failure always hits a file that has a backup.
        injected["REMCTL_TEST_ROLLBACK_FAIL_AT"] = "-4"
        result = self.run_script(
            INSTALL,
            "--shell-completions",
            "none",
            environment=injected,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RECOVERY ERROR", result.stdout)
        self.assertTrue(list(self.prefix.rglob("*.remctl-transaction-backup")))
        self.assertTrue(list((self.prefix / "tmp").glob("remctl-build.*/publish-journal")))

    def test_uninstall_refuses_live_socket_then_removes_same_stale_inode(self) -> None:
        self.bin.mkdir()
        marker = self.bin / ".remctl-capability-host-app"
        marker.write_text(str(self.app) + "\n")
        marker.chmod(0o600)
        write_manifest(self.bin, [".remctl-capability-host-app"])
        self.socket.parent.mkdir(parents=True, mode=0o700)
        self.socket.parent.chmod(0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket))
            self.socket.chmod(0o600)
            listener.listen(1)
            blocked = self.run_script(UNINSTALL, "--keep-config", check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("still live or changed identity", blocked.stdout)
            self.assertTrue(self.socket.exists())
            self.assertTrue(marker.exists())
        finally:
            listener.close()

        removed = self.run_script(UNINSTALL, "--keep-config", check=False)
        self.assertEqual(removed.returncode, 0, removed.stdout)
        self.assertFalse(self.socket.exists())
        self.assertFalse(marker.exists())

    def test_installer_has_protected_import_and_identity_continuity_checks(self) -> None:
        source = INSTALL.read_text()
        self.assertIn("all(import_root_protected(entry) for entry in sys.path if entry)", source)
        self.assertIn("POLICY_PROTOCOL_VERSION", source)
        self.assertIn("ast.literal_eval", source)
        self.assertNotIn('"protocolVersion":1', source)
        self.assertIn("TeamIdentifier=", source)
        self.assertIn("designated => ", source)
        self.assertIn('codesign -d --extract-certificates="$CERTIFICATE_PREFIX"', source)
        self.assertIn("LEAF_CERTIFICATE_SHA1", source)
        self.assertIn("^[0-9A-F]{40}$", source)
        self.assertIn("rollback verification failed", source)

    def test_real_identity_name_dry_run_extracts_canonical_leaf_hash(self) -> None:
        identities = subprocess.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        ).stdout
        match = re.search(
            r'^\s*\d+\)\s+([0-9A-F]{40})\s+"(Apple Development:[^"]+)"',
            identities,
            re.MULTILINE,
        )
        if match is None:
            self.skipTest("No Apple Development signing identity is available")

        environment = self.environment.copy()
        environment.pop("REMCTL_SKIP_LAUNCHSERVICES", None)
        environment["REMCTL_CODESIGN_IDENTITY"] = match.group(2)
        protected_python = next(
            (
                path
                for path in (
                    Path("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"),
                    Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"),
                )
                if path.exists()
            ),
            None,
        )
        if protected_python is None:
            self.skipTest("No protected framework Python is available")
        environment["REMCTL_CAPABILITY_PYTHON"] = str(protected_python)
        result = self.run_script(
            INSTALL,
            "--dry-run",
            "--shell-completions",
            "none",
            environment=environment,
        )
        self.assertIn("Signing: REMCTL_CODESIGN_IDENTITY", result.stdout)
        self.assertIn(f"Signing certificate SHA-1: {match.group(1)}", result.stdout)
        self.assertIn("Dry run complete", result.stdout)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.agent.exists())

    def test_uninstaller_never_resets_privacy_grants(self) -> None:
        self.assertNotIn("tccutil", UNINSTALL.read_text().lower())


if __name__ == "__main__":
    unittest.main()
