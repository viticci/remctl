"""A broker must not outlive its host, and a new host must replace one that did.

These tests use real processes: a double-forked listener stands in for a broker
that launchd adopted after its host died. Nothing touches an installed host.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import remctl_broker

ROOT = Path(__file__).resolve().parents[1]

LISTENER = """
import os, socket, sys
path = sys.argv[1]
if sys.argv[2] == "orphan" and os.fork():
    os._exit(0)
server = socket.socket(socket.AF_UNIX)
server.bind(path)
os.chmod(path, 0o600)
server.listen(4)
print(os.getpid(), flush=True)
while True:
    server.accept()[0].close()
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(sys.platform == "darwin", "macOS process APIs are required")
class OrphanedBrokerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="remctl-orphan-")
        self.addCleanup(directory.cleanup)
        self.socket_path = Path(directory.name).resolve() / "capability-host.sock"
        self.socket_path.parent.chmod(0o700)

    def start_listener(self, kind):
        """Start a listener on the socket; an "orphan" is adopted by launchd."""
        process = subprocess.Popen(
            [sys.executable, "-c", LISTENER, str(self.socket_path), kind],
            stdout=subprocess.PIPE,
            text=True,
        )
        pid = int(process.stdout.readline())
        process.stdout.close()
        if kind == "orphan":
            process.wait(timeout=5)
            self.assertTrue(_wait_until(lambda: remctl_broker._is_orphaned_broker(pid)))
        self.addCleanup(self.stop, pid, process)
        return pid

    def stop(self, pid, process):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        self.socket_path.unlink(missing_ok=True)

    def test_new_broker_replaces_an_orphaned_broker(self):
        orphan = self.start_listener("orphan")
        remctl_broker._prepare_socket(self.socket_path).close()
        self.assertTrue(_wait_until(lambda: not _alive(orphan)))

    def test_live_broker_is_left_running(self):
        child = self.start_listener("child")
        with self.assertRaises(remctl_broker._ServerError) as raised:
            remctl_broker._prepare_socket(self.socket_path)
        self.assertEqual(raised.exception.code, "already_running")
        self.assertTrue(_alive(child))

    def test_unidentified_orphan_is_left_running(self):
        orphan = self.start_listener("orphan")
        with mock.patch.object(
            remctl_broker, "_process_executable_path", return_value=Path("/usr/bin/true")
        ), self.assertRaises(remctl_broker._ServerError) as raised:
            remctl_broker._prepare_socket(self.socket_path)
        self.assertEqual(raised.exception.code, "already_running")
        self.assertTrue(_alive(orphan))

    def test_broker_stops_when_its_host_is_killed(self):
        marker = self.socket_path.parent / "stopped"
        broker = (
            "import signal, sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import remctl_broker\n"
            f"signal.signal(signal.SIGTERM, lambda *_: (open({str(marker)!r}, 'w').close(), sys.exit(0)))\n"
            "remctl_broker._stop_when_parent_exits()\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        host = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {broker!r}])\n"
            "time.sleep(30)\n"
        )
        process = subprocess.Popen([sys.executable, "-c", host], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")
        process.stdout.close()
        self.assertFalse(marker.exists())
        process.kill()
        process.wait(timeout=5)
        self.assertTrue(_wait_until(marker.exists))


if __name__ == "__main__":
    unittest.main()
