from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "remctl-capability-host.swift"
INFO = ROOT / "remctl-capability-host-Info.plist"
AGENT = ROOT / "remctl-capability-host-launchagent.plist"
ARCHIVE_BUILDER = ROOT / "scripts/build_capability_archive.py"
APP_NAME = "RemCTL Capability Host.app"
EXECUTABLE_NAME = "RemCTL Capability Host"
BUNDLE_ID = "net.macstories.remctl.capability-host"
WINDOW_TEST_BUNDLE_ID = "net.macstories.remctl.capability-host.runtime-tests"


def _mac_tools() -> tuple[str, str]:
    if sys.platform != "darwin":
        raise unittest.SkipTest("RemCTL Capability Host is macOS-only")
    swiftc = shutil.which("swiftc")
    codesign = shutil.which("codesign")
    if not swiftc or not codesign:
        raise unittest.SkipTest("Swift compiler and codesign are required")
    return swiftc, codesign


def _signed_app(
    root: Path,
    compiled_host: Path,
    *,
    bundle_identifier: str = BUNDLE_ID,
) -> Path:
    _swiftc, codesign = _mac_tools()
    app = root / APP_NAME
    executable_dir = app / "Contents/MacOS"
    runtime_bin = app / "Contents/Resources/CapabilityRuntime/bin"
    executable_dir.mkdir(parents=True)
    runtime_bin.mkdir(parents=True)
    shutil.copy2(compiled_host, executable_dir / EXECUTABLE_NAME)
    shutil.copy2(INFO, app / "Contents/Info.plist")
    if bundle_identifier != BUNDLE_ID:
        info_path = app / "Contents/Info.plist"
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        info["CFBundleIdentifier"] = bundle_identifier
        with info_path.open("wb") as handle:
            plistlib.dump(info, handle)
    (app / "Contents/Resources/remctl-capability-python-path").write_text(
        sys.executable + "\n",
        encoding="utf-8",
    )
    (app / "Contents/Resources/remctl-capability-host-socket-path").write_text(
        str(root / "capability-host.sock") + "\n",
        encoding="utf-8",
    )
    for name in ("remctl-bridge", "remctl-private"):
        helper = runtime_bin / name
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o755)
    subprocess.run(
        [codesign, "--force", "--deep", "--sign", "-", str(app)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    subprocess.run(
        [codesign, "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return app


def _wait_for(
    path: Path,
    timeout: float = 10,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process is not None and process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"host exited before creating {path}: "
                f"stdout={stdout.decode(errors='replace')!r} "
                f"stderr={stderr.decode(errors='replace')!r}"
            )
        time.sleep(0.02)
    detail = ""
    if process is not None:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
        detail = (
            f"; process exited {process.returncode}"
            f"; stdout={stdout.decode(errors='replace')!r}"
            f"; stderr={stderr.decode(errors='replace')!r}"
        )
    raise AssertionError(f"timed out waiting for {path}{detail}")


def _run_window_test(
    app: Path, flag: str, output_path: Path
) -> subprocess.CompletedProcess[str]:
    """Reap only this window test's exact invocation, even if open times out."""
    executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
    expected_command = f"{executable} {flag} {output_path}"

    def exact_test_processes():
        rows = subprocess.run(
            ["/bin/ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()
        matches = []
        for row in rows:
            fields = row.strip().split(maxsplit=1)
            if len(fields) == 2 and fields[1] == expected_command:
                matches.append(int(fields[0]))
        return matches

    try:
        return subprocess.run(
            ["/usr/bin/open", "-W", "-n", str(app), "--args", flag, str(output_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        # open is only the launcher; its child may survive a launcher timeout.
        leaked = exact_test_processes()
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 2
        while leaked and time.monotonic() < deadline:
            time.sleep(0.05)
            leaked = exact_test_processes()
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if exact_test_processes():
            raise AssertionError(expected_command)


def _identity_probe(app: Path, root: Path) -> subprocess.Popen[bytes]:
    ready = root / "ready"
    resume = root / "resume"
    process = subprocess.Popen(
        [
            str(app / f"Contents/MacOS/{EXECUTABLE_NAME}"),
            "--test-verify-running-host",
            str(ready),
            str(resume),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(ready, process=process)
    return process


def _protected_python() -> Path:
    candidates = [
        Path("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"),
        Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        result = subprocess.run(
            [str(candidate), "-I", "-S", "-c", "import sys; assert sys.version_info >= (3, 13)"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return candidate.resolve(strict=True)
    raise unittest.SkipTest("a protected framework Python 3.13+ is required")


def _service_app(
    root: Path,
    python: Path,
    *,
    simulate_permission_hang: bool = False,
    force_archive_fd_199: bool = False,
) -> tuple[Path, Path]:
    swiftc, codesign = _mac_tools()
    archive = root / "remctl-capability.pyz"
    subprocess.run(
        [
            str(python),
            "-I",
            "-S",
            str(ARCHIVE_BUILDER),
            "--source-root",
            str(ROOT),
            "--output",
            str(archive),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    app = root / APP_NAME
    executable_dir = app / "Contents/MacOS"
    resources = app / "Contents/Resources"
    runtime_bin = resources / "CapabilityRuntime/bin"
    executable_dir.mkdir(parents=True)
    runtime_bin.mkdir(parents=True)
    shutil.copy2(INFO, app / "Contents/Info.plist")
    socket_path = root / "capability-host.sock"
    (resources / "remctl-capability-python-path").write_text(
        str(python) + "\n",
        encoding="utf-8",
    )
    (resources / "remctl-capability-host-socket-path").write_text(
        str(socket_path) + "\n",
        encoding="utf-8",
    )
    (resources / "remctl-capability-host-test-state-path").write_text(
        str(root / "host-state.json") + "\n",
        encoding="utf-8",
    )
    if simulate_permission_hang:
        (resources / "remctl-capability-host-test-hang-permission").write_text(
            "1\n",
            encoding="utf-8",
        )
        (resources / "remctl-capability-host-test-prompt-ready-path").write_text(
            str(root / "prompt-ready") + "\n",
            encoding="utf-8",
        )
        (resources / "remctl-capability-host-test-terminal-state-path").write_text(
            str(root / "terminal-state.json") + "\n",
            encoding="utf-8",
        )
    if force_archive_fd_199:
        (resources / "remctl-capability-host-test-force-archive-fd-199").write_text(
            "1\n",
            encoding="utf-8",
        )
    for name in ("remctl-bridge", "remctl-private"):
        helper = runtime_bin / name
        helper.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' \'{"protocolVersion":2}\'\n',
            encoding="utf-8",
        )
        helper.chmod(0o755)
    executable = executable_dir / EXECUTABLE_NAME
    target = f"{os.uname().machine}-apple-macosx14.0"
    subprocess.run(
        [
            swiftc,
            "-target",
            target,
            "-warnings-as-errors",
            "-O",
            "-D",
            "REMCTL_TESTING",
            "-framework",
            "AppKit",
            "-framework",
            "CoreServices",
            "-framework",
            "EventKit",
            "-framework",
            "Foundation",
            "-framework",
            "Security",
            "-Xlinker",
            "-sectcreate",
            "-Xlinker",
            "__TEXT",
            "-Xlinker",
            "__rctl_pyz",
            "-Xlinker",
            str(archive),
            "-o",
            str(executable),
            str(SOURCE),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    subprocess.run(
        [codesign, "--force", "--deep", "--sign", "-", str(app)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return app, socket_path


class CapabilityHostRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        swiftc, _codesign = _mac_tools()
        cls.permission_gate_payload = None
        cls.compiled_temp = tempfile.TemporaryDirectory()
        compiled_root = Path(cls.compiled_temp.name)
        cls.compiled_host = compiled_root / EXECUTABLE_NAME
        cls.compiled_release_host = compiled_root / f"{EXECUTABLE_NAME}-release"
        archive = compiled_root / "empty.pyz"
        archive.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        target = f"{os.uname().machine}-apple-macosx14.0"
        common = [
                swiftc,
                "-target",
                target,
                "-warnings-as-errors",
                "-O",
                "-framework",
                "Foundation",
                "-framework",
                "AppKit",
                "-framework",
                "CoreServices",
                "-framework",
                "EventKit",
                "-framework",
                "Security",
                "-Xlinker",
                "-sectcreate",
                "-Xlinker",
                "__TEXT",
                "-Xlinker",
                "__rctl_pyz",
                "-Xlinker",
                str(archive),
        ]
        for output, extra in (
            (cls.compiled_host, ["-D", "REMCTL_TESTING"]),
            (cls.compiled_release_host, []),
        ):
            result = subprocess.run(
                common + extra + ["-o", str(output), str(SOURCE)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.compiled_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_bundle_and_launchagent_contracts_are_exact(self):
        with INFO.open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIdentifier"], BUNDLE_ID)
        self.assertEqual(info["CFBundleExecutable"], EXECUTABLE_NAME)
        self.assertIs(info["LSUIElement"], True)
        self.assertNotIn("LSBackgroundOnly", info)
        self.assertEqual(info["LSMinimumSystemVersion"], "14.0")
        self.assertTrue(info["NSRemindersFullAccessUsageDescription"])
        self.assertTrue(info["NSAppleEventsUsageDescription"])

        with AGENT.open("rb") as handle:
            agent = plistlib.load(handle)
        self.assertEqual(
            agent,
            {
                "Label": BUNDLE_ID,
                "ProgramArguments": [
                    "__REMCTL_CAPABILITY_HOST__",
                    "--run-capability-host",
                    "--socket",
                    "__REMCTL_CAPABILITY_HOST_SOCKET__",
                ],
                "RunAtLoad": True,
                "KeepAlive": True,
                "LimitLoadToSessionType": "Aqua",
                "Umask": 63,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            },
        )

    def test_host_source_has_sealed_identity_and_sanitized_launch_contract(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('getsectiondata(header64, "__TEXT", "__rctl_pyz"', source)
        self.assertIn("kSecCSStrictValidate | kSecCSCheckNestedCode", source)
        self.assertIn("signingCDHash(staticCode) == runningHash", source)
        self.assertIn("fchmod(descriptor, 0o600)", source)
        self.assertIn('arguments: ["service", "--socket", socketPath]', source)
        self.assertIn('private let nativeDescriptor: Int32 = 199', source)
        self.assertIn('private let nativeFDEnvironment = "REMCTL_CAPABILITY_NATIVE_FD"', source)
        self.assertIn("makeNativeSocketPair()", source)
        self.assertIn("class NativePermissionServer", source)
        self.assertIn("class CapabilityHostApplicationDelegate", source)
        self.assertIn("nativeChannel: pair.child", source)
        self.assertIn("withExtendedLifetime(delegate)", source)
        self.assertIn("let ready = poll(&item, 1, -1)", source)
        self.assertIn(
            "guard let firstHeaderByte = readFirstNativeByte(descriptor: descriptor)",
            source,
        )
        self.assertIn("let deadline = DispatchTime.now() + nativeIOTimeout", source)
        self.assertIn("count: 3,\n        deadline: deadline", source)
        self.assertNotIn("let status = waitForChild(pid)\n    termination.cancel()", source)
        self.assertIn('"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"', source)
        self.assertIn('"TMPDIR": "/private/tmp"', source)
        self.assertNotIn("ProcessInfo.processInfo.environment", source)
        for forbidden in ("PYTHONPATH", "DYLD_", "LD_LIBRARY_PATH", "PWD"):
            self.assertNotIn(forbidden, source)
        start_body = source[
            source.index("    func start() {"):
            source.index("    func windowDidBecomeKey")
        ]
        self.assertLess(
            start_body.index("capturePreviousActiveApplication()"),
            start_body.index("beginRegularActivationPolicy()"),
        )
        self.assertLess(
            start_body.index("beginRegularActivationPolicy()"),
            start_body.index("makePermissionWindow()"),
        )
        terminal_body = source[
            source.index("    private func finishTerminal("):
            source.index("    private func restorePreviousActiveApplication")
        ]
        self.assertLess(
            terminal_body.index("restorePreviousActiveApplication"),
            terminal_body.index("restoreAccessoryActivationPolicy()"),
        )
        self.assertIn("self?.permissionController?.shutdown()", source)

    def test_signed_host_accepts_its_original_running_identity(self):
        app = _signed_app(self.root, self.compiled_host)
        process = _identity_probe(app, self.root)
        (self.root / "resume").write_text("resume", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (stdout, stderr))

    def test_running_host_rejects_a_valid_post_start_resign(self):
        _swiftc, codesign = _mac_tools()
        app = _signed_app(self.root, self.compiled_host)
        process = _identity_probe(app, self.root)
        (app / "Contents/Resources/changed-after-launch").write_text(
            "changed",
            encoding="utf-8",
        )
        subprocess.run(
            [codesign, "--force", "--deep", "--sign", "-", str(app)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        (self.root / "resume").write_text("resume", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 65, (stdout, stderr))

    def test_release_host_exposes_only_the_sealed_service_invocation(self):
        app = _signed_app(self.root, self.compiled_release_host)
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        rejected_arguments = (
            ["--run-capability-host", "--socket", "/tmp/other.sock"],
            ["--permission-status"],
            ["--request-reminders"],
            ["--request-automation"],
            ["--probe-full-disk-access"],
            ["--test-permission-window-output", "/tmp/result.json"],
            ["--test-permission-gate"],
            ["--test-automation-status-cache"],
            ["--test-activation-unavailable"],
        )
        for arguments in rejected_arguments:
            with self.subTest(arguments=arguments):
                rejected = subprocess.run(
                    [str(executable), *arguments],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(rejected.returncode, 64)

        binary_strings = subprocess.run(
            ["/usr/bin/strings", str(executable)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        self.assertIn("--run-capability-host", binary_strings)
        for removed in (
            "--permission-status",
            "--request-reminders",
            "--request-automation",
            "--probe-full-disk-access",
            "--test-permission-window-output",
            "--test-permission-gate",
            "--test-automation-status-cache",
            "--test-activation-unavailable",
            WINDOW_TEST_BUNDLE_ID,
            "OneShotPermissionApplicationDelegate",
            "runPermissionPrompt",
        ):
            self.assertNotIn(removed, binary_strings)

    def test_permission_runner_has_a_visible_key_window_before_request(self):
        app = _signed_app(
            self.root,
            self.compiled_host,
            bundle_identifier=WINDOW_TEST_BUNDLE_ID,
        )
        output_path = self.root / "permission-window.json"
        result = _run_window_test(
            app, "--test-permission-window-output", output_path
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "ok", payload)
        self.assertTrue(payload["windowVisible"])
        self.assertTrue(payload["windowKey"])
        self.assertTrue(payload["applicationActive"])
        self.assertTrue(payload["runningApplicationActive"])
        self.assertEqual(payload["frontmostPID"], payload["pid"])
        self.assertTrue(payload["requestStarted"])
        self.assertEqual(payload["activationPolicyDuringGate"], 0)
        self.assertEqual(payload["activationPolicyAfterCompletion"], 1)
        self.assertEqual(payload["windowTitle"], "RemCTL Permissions — Waiting for macOS")
        self.assertTrue(payload["applicationFinishedLaunching"])
        self.assertEqual(payload["bundleIdentifier"], WINDOW_TEST_BUNDLE_ID)
        self.assertGreater(payload["pid"], 0)

    def _permission_gate_payload(self):
        """Run the shared gate simulation once for both sets of assertions."""
        if type(self).permission_gate_payload is not None:
            return type(self).permission_gate_payload
        app = _signed_app(self.root, self.compiled_host)
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        result = subprocess.run(
            [str(executable), "--test-permission-gate"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        type(self).permission_gate_payload = payload
        return payload

    def test_permission_gate_requires_every_predicate_and_one_fresh_click(self):
        payload = self._permission_gate_payload()
        self.assertTrue(payload["ready"])
        self.assertEqual(
            payload["blocked"],
            {
                "applicationInactive": True,
                "runningApplicationInactive": True,
                "notFrontmost": True,
                "windowHidden": True,
                "windowNotKey": True,
            },
        )
        self.assertEqual(payload["requestCountBeforeClick"], 0)
        self.assertEqual(payload["requestCountAfterClick"], 1)
        self.assertEqual(payload["requestCountAfterSecondClick"], 1)
        self.assertFalse(payload["cancellationAfterRequest"])
        self.assertTrue(payload["firstFinish"])
        self.assertFalse(payload["secondFinish"])
        self.assertEqual(payload["raceRequestCountAfterLoss"], 0)
        self.assertTrue(payload["raceRequiresFreshClick"])
        self.assertEqual(payload["raceRequestCountAfterFreshClick"], 1)
        self.assertTrue(payload["cancellationAccepted"])
        self.assertEqual(payload["cancellationRequestCount"], 0)
        self.assertTrue(payload["cancellationTerminal"])
        self.assertTrue(payload["restoreEligible"])
        self.assertTrue(payload["restoreRejectsTerminated"])
        self.assertTrue(payload["restoreRejectsSelf"])
        self.assertTrue(payload["restoreRejectsLostFocus"])

    def test_post_dispatch_timeout_wins_once_and_rejects_late_callback(self):
        payload = self._permission_gate_payload()
        self.assertTrue(payload["timeoutRequestStarted"])
        self.assertTrue(payload["timeoutAcceptedAfterRequest"])
        self.assertFalse(payload["lateCallbackAcceptedAfterTimeout"])
        self.assertEqual(payload["timeoutCompletionCount"], 1)
        self.assertTrue(payload["timeoutTerminal"])

    def test_window_close_cancels_only_before_irreversible_request(self):
        app = _signed_app(self.root, self.compiled_host)
        output_path = self.root / "permission-close.json"
        result = _run_window_test(
            app, "--test-permission-window-close-output", output_path
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(output_path.read_text(encoding="utf-8")),
            {"status": "cancelled", "activationPolicyAfterCompletion": 1},
        )
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            'permissionWindow?.standardWindowButton(.closeButton)?.isEnabled = false', source
        )

    def test_activation_failure_is_bounded_without_capability_construction(self):
        app = _signed_app(self.root, self.compiled_host)
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        started = time.monotonic()
        result = subprocess.run(
            [str(executable), "--test-activation-unavailable"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 3)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "promptUnavailable",
                "capabilityConstructed": False,
                "activationPolicyAfterCompletion": 1,
            },
        )

    def test_eventkit_callback_error_is_bounded_and_keeps_domain_and_code(self):
        app = _signed_app(self.root, self.compiled_host)
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        result = subprocess.run(
            [str(executable), "--test-eventkit-error-status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "error:EK_Error_Unsafe:37")

    def test_native_protocol_rejects_fractional_and_boolean_versions(self):
        app = _signed_app(self.root, self.compiled_host)
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        result = subprocess.run(
            [str(executable), "--test-native-protocol-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"integer": True, "fractional": False, "boolean": False},
        )

    def test_automation_status_cache_is_bounded_single_flight_and_refreshable(self):
        app = _signed_app(self.root, self.compiled_host)
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        result = subprocess.run(
            [str(executable), "--test-automation-status-cache"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["firstStatus"], "unknown")
        self.assertLess(payload["firstElapsed"], 0.5)
        self.assertEqual(payload["secondStatusWhilePending"], "unknown")
        self.assertEqual(payload["pendingStartCount"], 1)
        self.assertEqual(payload["resolvedStatus"], "authorized")
        self.assertEqual(payload["resolvedStartCount"], 1)
        self.assertEqual(payload["authoritativeInitialStatus"], "unknown")
        self.assertEqual(payload["authoritativeStatusWhilePreflightPending"], "denied")
        self.assertEqual(payload["authoritativePendingStartCount"], 1)
        self.assertEqual(payload["authoritativeFinalStatus"], "denied")
        self.assertEqual(payload["authoritativeStartCount"], 1)
        self.assertEqual(payload["ttlInitialStatus"], "authorized")
        self.assertEqual(payload["ttlStaleWhileRefreshing"], "authorized")
        self.assertEqual(payload["ttlRefreshedStatus"], "denied")
        self.assertEqual(payload["ttlRefreshStartCount"], 2)
        self.assertEqual(payload["targetInitialStatus"], "targetNotRunning")
        self.assertEqual(payload["targetBeforeRetryStatus"], "targetNotRunning")
        self.assertEqual(payload["targetCountBeforeRetry"], 1)
        self.assertEqual(payload["targetStaleWhileRefreshing"], "targetNotRunning")
        self.assertEqual(payload["targetRefreshedStatus"], "authorized")
        self.assertEqual(payload["targetRefreshStartCount"], 2)
        self.assertEqual(payload["warmAuthorizedInitialStatus"], "authorized")
        self.assertEqual(payload["warmAuthorizedWhileTargetStops"], "authorized")
        self.assertEqual(payload["warmAuthorizedAfterTargetStops"], "authorized")
        self.assertEqual(payload["warmAuthorizedCountAfterTargetStops"], 2)
        self.assertEqual(payload["warmAuthorizedWhileDeniedRefreshes"], "authorized")
        self.assertEqual(payload["warmAuthorizedLaterDenied"], "denied")
        self.assertEqual(payload["warmAuthorizedFinalStartCount"], 3)
        self.assertEqual(payload["warmDeniedInitialStatus"], "denied")
        self.assertEqual(payload["warmDeniedWhileTargetStops"], "denied")
        self.assertEqual(payload["warmDeniedAfterTargetStops"], "denied")
        self.assertEqual(payload["warmNotDeterminedInitialStatus"], "notDetermined")
        self.assertEqual(
            payload["warmNotDeterminedWhileTargetStops"],
            "notDetermined",
        )
        self.assertEqual(payload["warmNotDeterminedAfterTargetStops"], "notDetermined")
        self.assertEqual(payload["launchFailureAuthorized"], "authorized")
        self.assertEqual(payload["launchFailureAfterAuthorized"], "authorized")
        self.assertEqual(payload["launchFailureTimedOut"], "timedOut")
        self.assertEqual(payload["launchFailureCancelled"], "cancelled")
        self.assertEqual(
            payload["launchFailurePromptUnavailable"],
            "promptUnavailable",
        )
        self.assertEqual(payload["launchFailureAfterTimedOut"], "authorized")
        self.assertEqual(payload["coldLaunchFailure"], "targetNotRunning")
        self.assertEqual(payload["definitiveReplacementAuthorized"], "authorized")
        self.assertEqual(payload["definitiveReplacementDenied"], "denied")
        self.assertEqual(
            payload["definitiveReplacementNotDetermined"],
            "notDetermined",
        )

    def test_signed_host_runs_the_real_sealed_broker(self):
        from remctl_broker import PROTOCOL_VERSION, dispatch, status

        python = _protected_python()
        app, socket_path = _service_app(self.root, python)
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        process = subprocess.Popen(
            [str(executable), "--run-capability-host", "--socket", str(socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        environment_names = (
            "REMCTL_CAPABILITY_HOST_APP",
            "REMCTL_CAPABILITY_HOST_SOCKET",
            "REMCTL_CAPABILITY_HOST_LAUNCH_AGENT",
        )
        previous = {name: os.environ.get(name) for name in environment_names}
        try:
            _wait_for(socket_path, process=process)
            state_path = self.root / "host-state.json"
            _wait_for(state_path, process=process)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["pid"], process.pid)
            self.assertEqual(state["bundleIdentifier"], BUNDLE_ID)
            self.assertTrue(state["applicationFinishedLaunching"])
            self.assertEqual(state["activationPolicy"], 1)
            self.assertEqual(state["windowCount"], 0)
            self.assertGreater(state["brokerPID"], 0)
            broker_pid = state["brokerPID"]

            def assert_same_persistent_processes():
                self.assertIsNone(process.poll())
                rows = subprocess.run(
                    ["/bin/ps", "-axo", "pid=,ppid=,command="],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout.splitlines()
                parsed = []
                for row in rows:
                    fields = row.strip().split(maxsplit=2)
                    if len(fields) == 3:
                        parsed.append((int(fields[0]), int(fields[1]), fields[2]))
                broker_rows = [row for row in parsed if row[0] == broker_pid]
                self.assertEqual(len(broker_rows), 1)
                self.assertEqual(broker_rows[0][1], process.pid)
                host_executable = str(executable)
                host_processes = [
                    pid for pid, _ppid, command in parsed
                    if command.startswith(host_executable)
                ]
                self.assertEqual(host_processes, [process.pid])

            time.sleep(5.5)
            assert_same_persistent_processes()
            os.environ["REMCTL_CAPABILITY_HOST_APP"] = str(app)
            os.environ["REMCTL_CAPABILITY_HOST_SOCKET"] = str(socket_path)
            os.environ["REMCTL_CAPABILITY_HOST_LAUNCH_AGENT"] = str(
                self.root / "missing-launch-agent.plist"
            )
            payload = status(timeout=10)
            self.assertTrue(payload["available"], payload)
            self.assertEqual(payload["protocolVersion"], PROTOCOL_VERSION)
            self.assertEqual(
                set(payload["permissions"]),
                {
                    "status",
                    "fullDiskAccess",
                    "reminders",
                    "automation",
                    "automationTarget",
                },
            )
            time.sleep(5.5)
            assert_same_persistent_processes()
            second_payload = status(timeout=10)
            self.assertTrue(second_payload["available"])
            self.assertEqual(second_payload["protocolVersion"], PROTOCOL_VERSION)
            assert_same_persistent_processes()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                dispatch(["lists", "--json"], timeout=10)
            self.assertNotIn("sealed capability", stderr.getvalue())
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)

    def test_service_remaps_archive_collision_away_from_native_descriptor(self):
        from remctl_broker import PROTOCOL_VERSION, status

        python = _protected_python()
        app, socket_path = _service_app(
            self.root,
            python,
            force_archive_fd_199=True,
        )
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        process = subprocess.Popen(
            [str(executable), "--run-capability-host", "--socket", str(socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        environment_names = (
            "REMCTL_CAPABILITY_HOST_APP",
            "REMCTL_CAPABILITY_HOST_SOCKET",
            "REMCTL_CAPABILITY_HOST_LAUNCH_AGENT",
        )
        previous = {name: os.environ.get(name) for name in environment_names}
        try:
            _wait_for(socket_path, process=process)
            state_path = self.root / "host-state.json"
            _wait_for(state_path, process=process)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            broker_pid = state["brokerPID"]

            os.environ["REMCTL_CAPABILITY_HOST_APP"] = str(app)
            os.environ["REMCTL_CAPABILITY_HOST_SOCKET"] = str(socket_path)
            os.environ["REMCTL_CAPABILITY_HOST_LAUNCH_AGENT"] = str(
                self.root / "missing-launch-agent.plist"
            )
            payload = status(timeout=10)
            self.assertTrue(payload["available"], payload)
            self.assertEqual(payload["protocolVersion"], PROTOCOL_VERSION)

            descriptor_rows = subprocess.run(
                [
                    "/usr/sbin/lsof",
                    "-a",
                    "-p",
                    str(broker_pid),
                    "-d",
                    "198,199",
                    "-F",
                    "ft",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.splitlines()
            descriptor_types = {}
            descriptor = None
            for row in descriptor_rows:
                if row.startswith("f"):
                    descriptor = int(row[1:])
                elif row.startswith("t") and descriptor in (198, 199):
                    descriptor_types[descriptor] = row[1:]

            self.assertEqual(descriptor_types.get(198), "REG")
            self.assertEqual(descriptor_types.get(199), "unix")
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)

    def test_sigterm_cancels_an_active_native_permission_request(self):
        from remctl_broker import request_permission

        python = _protected_python()
        app, socket_path = _service_app(
            self.root,
            python,
            simulate_permission_hang=True,
        )
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        process = subprocess.Popen(
            [str(executable), "--run-capability-host", "--socket", str(socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        environment_names = (
            "REMCTL_CAPABILITY_HOST_APP",
            "REMCTL_CAPABILITY_HOST_SOCKET",
            "REMCTL_CAPABILITY_HOST_LAUNCH_AGENT",
        )
        previous = {name: os.environ.get(name) for name in environment_names}
        request_outcome = []
        request_thread = None
        try:
            _wait_for(socket_path, process=process)
            state_path = self.root / "host-state.json"
            _wait_for(state_path, process=process)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            broker_pid = state["brokerPID"]
            os.environ["REMCTL_CAPABILITY_HOST_APP"] = str(app)
            os.environ["REMCTL_CAPABILITY_HOST_SOCKET"] = str(socket_path)
            os.environ["REMCTL_CAPABILITY_HOST_LAUNCH_AGENT"] = str(
                self.root / "missing-launch-agent.plist"
            )

            def request_access():
                try:
                    request_outcome.append(request_permission("reminders", timeout=20))
                except Exception as error:
                    request_outcome.append(error)

            request_thread = threading.Thread(target=request_access, daemon=True)
            request_thread.start()
            _wait_for(self.root / "prompt-ready", process=process)
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            terminal_state_path = self.root / "terminal-state.json"
            _wait_for(terminal_state_path)
            self.assertEqual(
                json.loads(terminal_state_path.read_text(encoding="utf-8")),
                {
                    "status": "cancelled",
                    "activationPolicy": 1,
                    "permissionWindowVisible": False,
                    "terminal": True,
                },
            )
            request_thread.join(timeout=5)
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(request_thread.is_alive())
            self.assertTrue(request_outcome)
            broker_status = subprocess.run(
                ["/bin/ps", "-p", str(broker_pid), "-o", "pid="],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(broker_status.stdout.strip(), "")
            host_rows = subprocess.run(
                ["/bin/ps", "-axo", "command="],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout
            self.assertNotIn(str(executable), host_rows)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
            if request_thread is not None:
                request_thread.join(timeout=1)


class WindowLauncherCleanupTests(unittest.TestCase):
    def test_timeout_reaps_only_the_exact_window_invocation(self):
        app = Path("/private/tmp/remctl-window-fixture") / APP_NAME
        executable = app / f"Contents/MacOS/{EXECUTABLE_NAME}"
        output = app.parent / "permission-window.json"
        flag = "--test-permission-window-output"
        timeout = subprocess.TimeoutExpired("open", 15)
        running = True

        def run(command, **_kwargs):
            if command[0] == "/usr/bin/open":
                raise timeout
            self.assertEqual(command, ["/bin/ps", "-axo", "pid=,command="])
            rows = [
                f"222 {executable} {flag} {app.parent / 'other-output.json'}",
                f"333 /Applications/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME} --run-capability-host --socket /installed.sock",
            ]
            if running:
                rows.append(f"111 {executable} {flag} {output}")
            return subprocess.CompletedProcess(command, 0, stdout="\n".join(rows))

        def terminate(_pid, _signal):
            nonlocal running
            running = False

        with (
            mock.patch.object(subprocess, "run", side_effect=run),
            mock.patch.object(os, "kill", side_effect=terminate) as kill,
            mock.patch.object(time, "sleep"),
            self.assertRaises(subprocess.TimeoutExpired) as raised,
        ):
            _run_window_test(app, flag, output)
        self.assertIs(raised.exception, timeout)
        kill.assert_called_once_with(111, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
