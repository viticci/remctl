from __future__ import annotations

import tempfile
import threading
import time
import unittest
import subprocess
import sys
import socket
from pathlib import Path

import remctl_host
import remctl_read_broker
from unittest import mock
from helpers import StaticIdentityValidator, load_module
from remctl_host_operations import ReadOperations
from remctl_host_manifest import manifest_digest, runtime_manifest_bytes

build_manifest_payload = load_module(
    "build_host_runtime_manifest_test",
    "scripts/build_host_runtime_manifest.py",
).build_manifest_payload


class HostClientTests(unittest.TestCase):
    def test_real_broker_entrypoint_serves_health(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "private" / "broker.sock"
            root = Path(__file__).resolve().parent.parent
            manifest_path = Path(tmpdir) / "runtime-manifest.json"
            manifest_payload = build_manifest_payload(
                root=root,
                protected_python=Path(sys.executable).resolve(),
            )
            manifest_path.write_bytes(runtime_manifest_bytes(manifest_payload))
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(root / "remctl_read_broker.py"),
                    "--manifest",
                    str(manifest_path),
                    "--manifest-digest",
                    manifest_digest(manifest_payload),
                    "--socket",
                    str(socket_path),
                ],
                cwd="/",
                env={
                    "HOME": str(Path.home()),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "LANG": "en_US.UTF-8",
                    "LC_ALL": "en_US.UTF-8",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                for _ in range(200):
                    if socket_path.exists():
                        break
                    if process.poll() is not None:
                        self.fail(process.stderr.read())
                    time.sleep(0.01)
                result = remctl_host.health(
                    socket_path,
                    connect_timeout=1,
                    read_timeout=1,
                )
                self.assertEqual(result["status"], "ready")
                self.assertIn("manifestDigest", result)
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stderr is not None:
                    process.stderr.close()

    def test_health_round_trip_over_unix_socket(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "private" / "broker.sock"
            server = remctl_read_broker.BrokerServer(
                socket_path,
                remctl_read_broker.ReadBroker(
                    ReadOperations(mock.Mock()).handlers(),
                    identity_validator=StaticIdentityValidator(),
                ),
                workers=1,
                queue_limit=1,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            for _ in range(100):
                if socket_path.exists():
                    break
                time.sleep(0.01)
            try:
                result = remctl_host.health(
                    socket_path,
                    connect_timeout=1,
                    read_timeout=1,
                )
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["role"], "read-only-capability-host")
            finally:
                server.stop()
                thread.join(timeout=2)

    def test_unavailable_socket_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(remctl_host.HostUnavailable):
                remctl_host.health(
                    Path(tmpdir) / "missing.sock",
                    connect_timeout=0.1,
                    read_timeout=0.1,
                )

    def test_stalled_partial_frame_times_out(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "private" / "broker.sock"
            server = remctl_read_broker.BrokerServer(
                socket_path,
                remctl_read_broker.ReadBroker(
                    ReadOperations(mock.Mock()).handlers(),
                    identity_validator=StaticIdentityValidator(),
                ),
                workers=1,
                queue_limit=1,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            for _ in range(100):
                if socket_path.exists():
                    break
                time.sleep(0.01)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                with mock.patch.object(
                    remctl_read_broker,
                    "DEFAULT_CONNECTION_TIMEOUT",
                    0.05,
                ):
                    client.connect(str(socket_path))
                    client.sendall(b"\x00\x00")
                    client.settimeout(1)
                    response = remctl_host._decode_response(client)
                self.assertEqual(response["code"], "request_timeout")
            finally:
                client.close()
                server.stop()
                thread.join(timeout=2)

    def test_poll_condition_computes_read_timeout_from_server_bounds(self):
        with mock.patch.object(remctl_host, "call_host", return_value={"result": {"met": True}}) as call_host:
            result = remctl_host.poll_condition(
                Path("/Users/ninja-matt/Projects/remctl/fake.sock"),
                "list_state",
                "LIST-1",
                attempts=100,
                delay_milliseconds=500,
            )
        self.assertEqual(result, {"met": True})
        self.assertEqual(call_host.call_args.kwargs["read_timeout"], 60.0)

    def test_poll_condition_preserves_explicit_read_timeout(self):
        with mock.patch.object(remctl_host, "call_host", return_value={"result": {"met": False}}) as call_host:
            remctl_host.poll_condition(
                Path("/Users/ninja-matt/Projects/remctl/fake.sock"),
                "list_state",
                "LIST-1",
                read_timeout=99,
            )
        self.assertEqual(call_host.call_args.kwargs["read_timeout"], 99)


if __name__ == "__main__":
    unittest.main()
