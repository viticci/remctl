from __future__ import annotations

import json
import os
import socket
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from io import StringIO

import remctl_host_protocol as protocol
from remctl_host_manifest import RuntimeIdentityValidator
import remctl_read_broker as broker
from remctl_host_operations import ReadOperations
from unittest import mock
from helpers import StaticIdentityValidator, write_runtime_manifest_fixture


def request(operation="health", **fields):
    return {
        "protocolVersion": protocol.PROTOCOL_VERSION,
        "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
        "requestId": "broker-test",
        "operation": operation,
        **fields,
    }


def frame(payload):
    data = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def unframe(data):
    size = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4:4 + size])


class ReadBrokerTests(unittest.TestCase):
    @staticmethod
    def _broker():
        fake_remctl = mock.Mock()
        return broker.ReadBroker(
            ReadOperations(fake_remctl).handlers(),
            identity_validator=StaticIdentityValidator(),
        )

    def test_prepare_socket_creates_private_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "private"
            path = parent / "broker.sock"
            broker.prepare_socket_path(path)
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)

    def test_prepare_socket_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "private"
            parent.mkdir(mode=0o700)
            target = parent / "target"
            target.write_text("not a socket")
            path = parent / "broker.sock"
            path.symlink_to(target)
            with self.assertRaises(broker.BrokerSecurityError):
                broker.prepare_socket_path(path)

    def test_prepare_socket_rejects_regular_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "broker.sock"
            path.write_text("occupied")
            with self.assertRaises(broker.BrokerSecurityError):
                broker.prepare_socket_path(path)

    def test_prepare_socket_removes_owned_stale_socket(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "broker.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            broker.prepare_socket_path(path)
            self.assertFalse(path.exists())

    def test_bind_server_socket_uses_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "private" / "broker.sock"
            server = broker.bind_server_socket(path)
            try:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.stat().st_uid, os.getuid())
            finally:
                server.close()
                path.unlink()

    def test_health_response_contains_protocol_identity(self):
        response = unframe(self._broker().handle_frame(frame(request())))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"]["role"], "read-only-capability-host")
        self.assertEqual(
            response["schemaManifestDigest"],
            protocol.SCHEMA_MANIFEST_DIGEST,
        )
        self.assertEqual(
            response["result"]["manifestDigest"],
            "a" * 64,
        )
        self.assertNotIn("protectedPython", response["result"])
        self.assertNotIn("brokerEntrypoint", response["result"])

    def test_mutation_request_is_rejected_before_dispatch(self):
        response = unframe(
            self._broker().handle_frame(frame(request("add", title="Milk")))
        )
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "unsupported_operation")

    def test_unimplemented_operation_fails_closed(self):
        # Every declared OPERATION is now implemented, so fail-closed behaviour
        # is exercised via an operation name that is not in the schema at all.
        response = unframe(
            self._broker().handle_frame(
                frame(request("snapshot.doesNotExist", identifier="LIST-1"))
            )
        )
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "unsupported_operation")

    def test_registered_handlers_match_manifest(self):
        self.assertEqual(
            set(self._broker()._handlers),
            set(protocol.IMPLEMENTED_OPERATIONS),
        )

    def test_handler_manifest_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "exactly match"):
            broker.ReadBroker({}, identity_validator=StaticIdentityValidator())

    def test_runtime_identity_is_revalidated_before_every_dispatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path, manifest_digest, _, files = write_runtime_manifest_fixture(
                Path(tmpdir)
            )
            fake_remctl = mock.Mock()
            read_broker = broker.ReadBroker(
                ReadOperations(fake_remctl).handlers(),
                identity_validator=RuntimeIdentityValidator(
                    manifest_path,
                    manifest_digest,
                ),
            )
            healthy = unframe(read_broker.handle_frame(frame(request())))
            self.assertEqual(healthy["status"], "ok")
            files["host_operations"].write_text("tampered\n", encoding="utf-8")
            tampered = unframe(read_broker.handle_frame(frame(request())))
            self.assertEqual(tampered["status"], "error")
            self.assertEqual(tampered["code"], "host_identity_invalid")

    def test_frame_rejects_length_mismatch(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            broker.decode_frame(struct.pack(">I", 10) + b"short")
        self.assertEqual(caught.exception.code, "invalid_frame")

    def test_main_requires_manifest_arguments(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            broker.main(["--socket", "/Users/ninja-matt/Projects/remctl/broker.sock"])
        self.assertNotEqual(caught.exception.code, 0)
        self.assertIn("--manifest", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
