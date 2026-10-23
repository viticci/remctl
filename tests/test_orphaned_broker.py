"""A broker must not outlive its host, and a new host must replace one that did.

These tests use real processes: a double-forked listener started with a
broker's `service --socket <path>` arguments stands in for a broker that launchd
adopted after its host died. Nothing touches an installed host.
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
import os, socket
path, kind = os.environ["REMCTL_TEST_SOCKET"], os.environ["REMCTL_TEST_KIND"]
if kind == "orphan" and os.fork():
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

    def start_listener(self, kind, broker=True):
        """Start a listener on the socket; an "orphan" is adopted by launchd.

        A broker is launched the way the host launches one, with its script on
        an inherited descriptor. Otherwise the same listener runs through `-c`
        with the broker's trailing arguments, which must not be enough.
        """
        script = self.socket_path.parent / "listener.py"
        script.write_text(LISTENER)
        descriptor = os.open(script, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        service = ["service", "--socket", str(self.socket_path)]
        if broker:
            command = [sys.executable, "-I", "-S", f"/dev/fd/{descriptor}", *service]
        else:
            command = [sys.executable, "-c", LISTENER, *service]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            text=True,
            pass_fds=(descriptor,),
            env={**os.environ, "REMCTL_TEST_SOCKET": str(self.socket_path), "REMCTL_TEST_KIND": kind},
        )
        pids = []
        self.addCleanup(self.stop, pids, process)
        pids.append(int(process.stdout.readline()))
        process.stdout.close()
        if kind == "orphan":
            process.wait(timeout=5)
            self.assertTrue(_wait_until(lambda: remctl_broker._process_info(pids[0])[0] == 1))
        return pids[0]

    def stop(self, pids, process):
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.kill()
        process.wait(timeout=5)
        self.socket_path.unlink(missing_ok=True)

    def test_new_broker_replaces_an_orphaned_broker(self):
        orphan = self.start_listener("orphan")
        remctl_broker._prepare_socket(self.socket_path).close()
        self.assertTrue(_wait_until(lambda: not _alive(orphan)))

    def test_installer_stops_an_orphaned_broker(self):
        # install.sh calls this after stopping the old host, before removing the socket.
        orphan = self.start_listener("orphan")
        self.assertTrue(remctl_broker.stop_orphaned_broker_at(self.socket_path))
        self.assertTrue(_wait_until(lambda: not _alive(orphan)))
        self.assertFalse(remctl_broker.stop_orphaned_broker_at(self.socket_path.parent / "missing.sock"))

    def test_installer_accepts_the_old_hosts_python(self):
        # The upgrade runs another Python than the orphan; only the one the old
        # host recorded, passed by install.sh, identifies it.
        orphan = self.start_listener("orphan")
        executable_path = remctl_broker._process_executable_path

        def elsewhere_for_self(pid):
            return Path("/Elsewhere/Python/bin/python3.13") if pid == os.getpid() else executable_path(pid)

        with mock.patch.object(remctl_broker, "_process_executable_path", side_effect=elsewhere_for_self):
            self.assertFalse(remctl_broker.stop_orphaned_broker_at(self.socket_path))
            self.assertTrue(_alive(orphan))
            self.assertFalse(remctl_broker.stop_orphaned_broker_at(self.socket_path, "/usr/bin/python3"))
            self.assertTrue(remctl_broker.stop_orphaned_broker_at(self.socket_path, sys.executable))
        self.assertTrue(_wait_until(lambda: not _alive(orphan)))

    def test_installer_leaves_a_live_broker_running(self):
        child = self.start_listener("child")
        self.assertFalse(remctl_broker.stop_orphaned_broker_at(self.socket_path))
        self.assertTrue(_alive(child))

    def test_live_broker_is_left_running(self):
        child = self.start_listener("child")
        with self.assertRaises(remctl_broker._ServerError) as raised:
            remctl_broker._prepare_socket(self.socket_path)
        self.assertEqual(raised.exception.code, "already_running")
        self.assertTrue(_alive(child))

    def test_orphan_without_broker_launch_shape_is_left_running(self):
        # Same interpreter, socket, and trailing arguments, but not launched as a broker.
        orphan = self.start_listener("orphan", broker=False)
        with self.assertRaises(remctl_broker._ServerError) as raised:
            remctl_broker._prepare_socket(self.socket_path)
        self.assertEqual(raised.exception.code, "already_running")
        self.assertTrue(_alive(orphan))

    def test_reused_pid_is_never_signalled(self):
        # The broker exits and its pid now names a process with another start time.
        probe = mock.Mock(**{"getsockopt.return_value": 4242})
        with mock.patch.object(
            remctl_broker, "_orphaned_broker_identity", side_effect=[(100, 0), (200, 0)]
        ), mock.patch.object(remctl_broker.os, "kill") as kill:
            self.assertTrue(remctl_broker._stop_orphaned_broker(probe, self.socket_path))
        kill.assert_not_called()

    def broker_script(self, marker, host):
        return (
            "import os, signal, sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import remctl_broker\n"
            f"signal.signal(signal.SIGTERM, lambda *_: (open({str(marker)!r}, 'w').close(), sys.exit(0)))\n"
            f"remctl_broker._stop_when_parent_exits({host})\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )

    def test_broker_stops_when_host_is_gone_before_watching(self):
        # The host recorded at startup is no longer this process's parent.
        marker = self.socket_path.parent / "stopped"
        process = subprocess.Popen(
            [sys.executable, "-c", self.broker_script(marker, 1)], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        process.wait(timeout=5)
        process.stdout.close()
        self.assertTrue(marker.exists())

    def test_broker_stops_when_its_host_is_killed(self):
        marker = self.socket_path.parent / "stopped"
        broker = self.broker_script(marker, "os.getppid()")
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
