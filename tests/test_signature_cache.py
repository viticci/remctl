"""The signed-host verification cache, exercised against a fake codesign.

The fake is a real executable in a temporary bin directory, so the broker runs
it through subprocess exactly as it runs /usr/bin/codesign. Nothing here reads
the Reminders database or talks to an installed Capability Host.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import remctl_broker

CDHASH = "0123456789abcdef0123456789abcdef01234567"

FAKE_CODESIGN = """#!/bin/sh
# Fake codesign: behaviour comes from $STATE/mode, every call is logged.
state="__STATE__"
echo "$*" >> "$state/calls"
mode="$(cat "$state/mode")"
case "$mode" in
  sleep) i=0; while [ ! -f "$state/release" ] && [ $i -lt 200 ]; do sleep 0.05; i=$((i+1)); done ;;
  fail) echo "a sealed resource is missing or invalid" >&2; exit 1 ;;
esac
case "$1" in
  -dvvv) printf 'Identifier=net.macstories.remctl.capability-host\\nCDHash=%s\\n' "$(cat "$state/cdhash")" >&2 ;;
esac
exit 0
"""


class SignatureCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="remctl-signature-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.state = root / "state"
        self.state.mkdir()
        (self.state / "calls").write_text("")
        self.set_mode("ok")
        (self.state / "cdhash").write_text(CDHASH)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.codesign = bin_dir / "codesign"
        self.codesign.write_text(FAKE_CODESIGN.replace("__STATE__", str(self.state)))
        self.codesign.chmod(0o755)
        self.app = root / "RemCTL Capability Host.app"
        runtime = self.app / "Contents/Resources/CapabilityRuntime"
        (runtime / "bin").mkdir(parents=True)
        (self.app / "Contents/MacOS").mkdir(parents=True)
        self.executable = self.app / "Contents/MacOS/RemCTL Capability Host"
        self.executable.write_bytes(b"signed host")
        (runtime / "bin/remctl-bridge").write_bytes(b"bridge")
        self.runtime = remctl_broker.HostedRuntime(
            app=self.app,
            runtime=runtime,
            archive_fd=198,
            cdhash=CDHASH,
            python=Path(sys.executable),
            bridge=runtime / "bin/remctl-bridge",
            private=runtime / "bin/remctl-private",
        )
        for patch in (
            mock.patch.object(remctl_broker, "CODESIGN_PATH", str(self.codesign)),
            mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        remctl_broker._reset_signature_cache()
        self.addCleanup(remctl_broker._reset_signature_cache)

    def set_mode(self, mode):
        (self.state / "mode").write_text(mode)

    def verifications(self):
        return sum(1 for line in (self.state / "calls").read_text().splitlines() if line.startswith("--verify"))

    def verify(self):
        remctl_broker._verify_sealed_bundle(self.runtime)

    def assert_code(self, code):
        with self.assertRaises(remctl_broker._ServerError) as raised:
            self.verify()
        self.assertEqual(raised.exception.code, code)
        self.assertFalse(raised.exception.dispatched)
        self.assertFalse(raised.exception.indeterminate)
        return raised.exception

    def test_fake_codesign_is_the_one_on_path(self):
        import shutil
        self.assertEqual(shutil.which("codesign"), str(self.codesign))

    def test_unchanged_bundle_is_verified_once_within_the_ttl(self):
        for _ in range(5):
            self.verify()
        self.assertEqual(self.verifications(), 1)

    def test_content_change_inside_the_bundle_forces_reverification(self):
        self.verify()
        self.set_mode("fail")
        # Same size, rewritten in place: only ctime/mtime reveal it.
        self.executable.write_bytes(b"evil  host")
        self.assert_code("host_signature_invalid")
        self.assertEqual(self.verifications(), 2)
        # A failure is never remembered as a pass.
        self.assert_code("host_signature_invalid")
        self.assertEqual(self.verifications(), 3)

    def test_added_removed_or_chmodded_entries_force_reverification(self):
        changes = (
            lambda: (self.app / "Contents/Resources/injected.dylib").write_bytes(b"x"),
            lambda: (self.app / "Contents/Resources/injected.dylib").unlink(),
            lambda: self.runtime.bridge.chmod(0o755),
            lambda: self.runtime.bridge.rename(self.runtime.bridge.with_name("moved")),
        )
        self.verify()
        expected = 1
        for change in changes:
            change()
            self.verify()
            expected += 1
            self.assertEqual(self.verifications(), expected)
            self.verify()
            self.assertEqual(self.verifications(), expected)

    def test_running_cdhash_change_is_invalid_and_not_cached(self):
        (self.state / "cdhash").write_text("f" * 40)
        self.assert_code("host_signature_invalid")
        (self.state / "cdhash").write_text(CDHASH)
        self.verify()
        self.verify()
        self.assertEqual(self.verifications(), 2)

    def test_cache_expires_after_the_ttl(self):
        with mock.patch.object(remctl_broker, "SIGNATURE_CACHE_TTL_SECONDS", 0.2):
            self.verify()
            self.verify()
            self.assertEqual(self.verifications(), 1)
            time.sleep(0.3)
            self.verify()
            self.assertEqual(self.verifications(), 2)

    def test_timeout_is_retryable_and_never_reported_as_invalid(self):
        self.set_mode("sleep")
        with mock.patch.object(remctl_broker, "SIGNATURE_CHECK_TIMEOUT_SECONDS", 0.3):
            error = self.assert_code("host_signature_check_timeout")
        self.assertIn("retry", str(error))
        response = remctl_broker._error_response(error)
        self.assertEqual(response["code"], "host_signature_check_timeout")
        self.assertFalse(response["dispatched"])
        # Not cached: the next call verifies again and succeeds.
        self.set_mode("ok")
        self.verify()
        self.verify()
        self.assertEqual(self.verifications(), 2)

    def test_client_sees_a_retry_safe_rejection(self):
        self.set_mode("sleep")
        with mock.patch.object(remctl_broker, "SIGNATURE_CHECK_TIMEOUT_SECONDS", 0.3):
            error = self.assert_code("host_signature_check_timeout")
        with self.assertRaises(remctl_broker.CapabilityHostError) as raised:
            remctl_broker._handle_run_response(remctl_broker._error_response(error))
        self.assertEqual(raised.exception.code, "host_signature_check_timeout")
        self.assertTrue(raised.exception.retry_safe)

    def test_concurrent_requests_share_one_verification(self):
        self.set_mode("sleep")
        errors = []

        def call():
            try:
                self.verify()
            except remctl_broker._ServerError as exc:
                errors.append(exc)

        with mock.patch.object(remctl_broker, "SIGNATURE_CHECK_TIMEOUT_SECONDS", 10):
            threads = [threading.Thread(target=call) for _ in range(8)]
            # Release the fake while the first verification holds the lock.
            threading.Timer(0.3, (self.state / "release").touch).start()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(self.verifications(), 1)

    def test_busy_verification_lock_reports_timeout_not_invalid(self):
        with mock.patch.object(remctl_broker, "SIGNATURE_LOCK_WAIT_SECONDS", 0.1):
            with remctl_broker._SIGNATURE_LOCK:
                self.assert_code("host_signature_check_timeout")
        self.assertEqual(self.verifications(), 0)

    def test_unreadable_bundle_is_never_cached(self):
        with mock.patch.object(remctl_broker, "_bundle_fingerprint", return_value=None):
            self.verify()
            self.verify()
        self.assertEqual(self.verifications(), 2)

    def test_missing_codesign_is_invalid(self):
        with mock.patch.object(remctl_broker, "CODESIGN_PATH", str(self.codesign) + "-missing"):
            self.assert_code("host_signature_invalid")



class SignatureDefaultsTests(unittest.TestCase):
    def test_production_uses_the_system_codesign_and_a_bounded_cache(self):
        # An absolute path: a codesign earlier on PATH must never verify the host.
        self.assertEqual(remctl_broker.CODESIGN_PATH, "/usr/bin/codesign")
        self.assertLessEqual(remctl_broker.SIGNATURE_CACHE_TTL_SECONDS, 600)


if __name__ == "__main__":
    unittest.main()
