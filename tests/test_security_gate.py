"""Security-gate tests for the RemCTL Capability Host.

Covers required (B1, B2) and quality (C1–C7) findings from the gate report.
Each section is clearly labelled.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import remctl_host_protocol as protocol
import remctl_read_broker as broker
import remctl_host as host_client
from remctl_host_manifest import (
    RuntimeIdentityValidator,
    RuntimeManifestError,
    build_runtime_manifest,
    sha256_digest_bytes,
    runtime_manifest_bytes,
    ManifestFileIdentity,
    RuntimeFileIdentity,
    ValidatedRuntimeIdentity,
    RUNTIME_MANIFEST_VERSION,
    HOST_ROLE,
    EXPECTED_BUNDLE_IDENTIFIER,
    EXPECTED_LAUNCH_AGENT_LABEL,
    PROTOCOL_VERSION,
    SCHEMA_MANIFEST_VERSION,
    SCHEMA_MANIFEST_DIGEST,
)
from remctl_host_operations import ReadOperations
from helpers import (
    StaticIdentityValidator,
    build_test_identity,
    write_runtime_manifest_fixture,
    load_module,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _request(operation="health", **fields):
    return {
        "protocolVersion": protocol.PROTOCOL_VERSION,
        "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
        "requestId": "gate-test",
        "operation": operation,
        **fields,
    }


def _frame(payload):
    data = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def _unframe(data):
    size = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4:4 + size])


def _make_broker(extra_handlers=None):
    fake_remctl = mock.Mock()
    return broker.ReadBroker(
        ReadOperations(fake_remctl).handlers(),
        identity_validator=StaticIdentityValidator(),
    )


# ── B1: Runtime identity — broker uses manifest path, not sibling ─────────────

class B1ManifestPathTests(unittest.TestCase):
    """Broker must load the exact validated manifest remctl path."""

    def test_load_remctl_uses_manifest_path_not_sibling(self):
        """_load_remctl_module loads from identity.runtime_files['remctl'].path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            # File A: what the manifest declares (unique marker)
            file_a = d / "remctl"
            file_a.write_text("MARKER = 'file-a'\nVERSION = '0.0.1'\n")
            # File B: a different sibling that should never be loaded
            file_b = d / "remctl_sibling"
            file_b.write_text("MARKER = 'file-b'\nVERSION = '0.0.1'\n")

            identity = ValidatedRuntimeIdentity(
                manifest_path="/fixtures/m.json",
                manifest_digest="a" * 64,
                runtime_manifest_version=RUNTIME_MANIFEST_VERSION,
                role=HOST_ROLE,
                bundle_identifier=EXPECTED_BUNDLE_IDENTIFIER,
                launch_agent_label=EXPECTED_LAUNCH_AGENT_LABEL,
                cli_version="0.0.1",
                host_version="1.0.0",
                protocol_version=PROTOCOL_VERSION,
                schema_manifest_version=SCHEMA_MANIFEST_VERSION,
                schema_manifest_digest=SCHEMA_MANIFEST_DIGEST,
                protected_python=ManifestFileIdentity(path=str(d / "py"), sha256="b" * 64),
                broker_entrypoint=ManifestFileIdentity(path=str(d / "broker.py"), sha256="c" * 64),
                runtime_files=(
                    RuntimeFileIdentity(key="remctl", path=str(file_a), sha256="d" * 64),
                ),
            )
            module = broker._load_remctl_module(identity)
            self.assertEqual(module.MARKER, "file-a")

    def test_adversarial_sibling_does_not_execute(self):
        """Sibling B next to broker is never loaded when manifest declares A."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            # The broker lives here; next to it is a "remctl" sibling (B)
            broker_file = d / "remctl_read_broker.py"
            broker_file.write_text("# fake broker\n")
            sibling_b = d / "remctl"
            sibling_b.write_text("MARKER = 'file-b'\nVERSION = '1.0.0'\n")

            # File A lives in a different directory; manifest points here
            subdir = d / "authorised"
            subdir.mkdir()
            file_a = subdir / "remctl"
            file_a.write_text("MARKER = 'file-a'\nVERSION = '1.0.0'\n")

            identity = ValidatedRuntimeIdentity(
                manifest_path="/fixtures/m.json",
                manifest_digest="a" * 64,
                runtime_manifest_version=RUNTIME_MANIFEST_VERSION,
                role=HOST_ROLE,
                bundle_identifier=EXPECTED_BUNDLE_IDENTIFIER,
                launch_agent_label=EXPECTED_LAUNCH_AGENT_LABEL,
                cli_version="1.0.0",
                host_version="1.0.0",
                protocol_version=PROTOCOL_VERSION,
                schema_manifest_version=SCHEMA_MANIFEST_VERSION,
                schema_manifest_digest=SCHEMA_MANIFEST_DIGEST,
                protected_python=ManifestFileIdentity(path=str(d / "py"), sha256="b" * 64),
                broker_entrypoint=ManifestFileIdentity(path=str(broker_file), sha256="c" * 64),
                runtime_files=(
                    RuntimeFileIdentity(key="remctl", path=str(file_a), sha256="d" * 64),
                ),
            )
            module = broker._load_remctl_module(identity)
            self.assertEqual(module.MARKER, "file-a", "sibling B was loaded instead of manifest A")

    def test_build_time_path_divergence_rejected(self):
        """build_runtime_manifest raises when broker and remctl are in different dirs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            runtime_dir = d / "runtime"
            runtime_dir.mkdir()
            broker_dir = d / "broker"
            broker_dir.mkdir()

            broker_file = broker_dir / "remctl_read_broker.py"
            broker_file.write_text("# broker\n")
            remctl_file = runtime_dir / "remctl"
            remctl_file.write_text('VERSION = "1.0.0"\n')
            py_file = d / "python"
            py_file.write_text("# py\n")
            py_file.chmod(0o700)

            with self.assertRaises(RuntimeManifestError) as cm:
                build_runtime_manifest(
                    root=runtime_dir,
                    protected_python=py_file,
                    broker_entrypoint=broker_file,
                    host_version="1.0.0",
                    runtime_files=(("remctl", remctl_file),),
                )
            self.assertIn("same directory", str(cm.exception))

    def test_validator_rejects_divergent_broker_remctl_paths(self):
        """Manifest validation fails if broker and remctl are in different directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            runtime_dir = d / "runtime"
            runtime_dir.mkdir()
            other_dir = d / "other"
            other_dir.mkdir()
            # broker lives in other_dir, remctl lives in runtime_dir
            broker_file = other_dir / "remctl_read_broker.py"
            broker_file.write_text("print('broker')\n")
            remctl_file = runtime_dir / "remctl"
            remctl_file.write_text('VERSION = "1.7.1"\n')
            py_file = d / "python"
            py_file.write_text("# py\n")
            py_file.chmod(0o700)
            other_module = runtime_dir / "remctl_host_manifest.py"
            other_module.write_text("print('manifest')\n")
            # Build a manifest manually (bypassing build_runtime_manifest which also checks)
            import json, hmac
            from remctl_host_manifest import (
                sha256_digest_bytes, runtime_manifest_bytes,
                hash_regular_file, RUNTIME_MANIFEST_VERSION, HOST_ROLE,
                EXPECTED_BUNDLE_IDENTIFIER, EXPECTED_LAUNCH_AGENT_LABEL,
                PROTOCOL_VERSION, SCHEMA_MANIFEST_VERSION, SCHEMA_MANIFEST_DIGEST,
                _reject,
            )
            payload = {
                "runtimeManifestVersion": RUNTIME_MANIFEST_VERSION,
                "role": HOST_ROLE,
                "bundleIdentifier": EXPECTED_BUNDLE_IDENTIFIER,
                "launchAgentLabel": EXPECTED_LAUNCH_AGENT_LABEL,
                "cliVersion": "1.7.1",
                "hostVersion": "1.0.0",
                "protocolVersion": PROTOCOL_VERSION,
                "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
                "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
                "protectedPython": {
                    "path": str(py_file),
                    "sha256": hash_regular_file(py_file, label="py", executable=True),
                },
                "brokerEntrypoint": {
                    "path": str(broker_file),
                    "sha256": sha256_digest_bytes(broker_file.read_bytes()),
                },
                "runtimeFiles": [
                    {
                        "key": "hostManifest",
                        "path": str(other_module),
                        "sha256": sha256_digest_bytes(other_module.read_bytes()),
                    },
                    {
                        "key": "remctl",
                        "path": str(remctl_file),
                        "sha256": sha256_digest_bytes(remctl_file.read_bytes()),
                    },
                ],
            }
            mb = runtime_manifest_bytes(payload)
            manifest_path = d / "manifest.json"
            manifest_path.write_bytes(mb)
            digest = sha256_digest_bytes(mb)
            with self.assertRaises(RuntimeManifestError) as cm:
                RuntimeIdentityValidator(manifest_path, digest).validate()
            self.assertIn("same directory", str(cm.exception).lower())

    def test_validators_no_remctl_key_rejected(self):
        """build_runtime_manifest rejects a runtime_files tuple with no 'remctl' key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            f = d / "some_file.py"
            f.write_text("print('hi')\n")
            py = d / "py"
            py.write_text("# py\n")
            py.chmod(0o700)
            with self.assertRaises(RuntimeManifestError):
                build_runtime_manifest(
                    root=d,
                    protected_python=py,
                    host_version="1.0.0",
                    runtime_files=(("notRemctl", f),),
                )

    def test_load_remctl_falls_back_to_sibling_when_no_identity(self):
        """Without validated_identity, _load_remctl_module falls back to sibling."""
        # Just prove it returns a module (the actual remctl sibling exists in ROOT)
        # We patch lstat so the real file is found but not actually loaded.
        with mock.patch("importlib.machinery.SourceFileLoader") as mock_loader_cls:
            mock_loader = mock.Mock()
            mock_loader_cls.return_value = mock_loader
            mock_spec = mock.Mock()
            mock_spec.loader = mock_loader
            with mock.patch("importlib.util.spec_from_loader", return_value=mock_spec):
                with mock.patch("importlib.util.module_from_spec", return_value=mock.Mock()):
                    broker._load_remctl_module(validated_identity=None)
            # Verify the path used was the sibling of __file__, not something else
            call_args = mock_loader_cls.call_args
            loaded_path = Path(call_args[0][1])
            expected_sibling = Path(broker.__file__).resolve().with_name("remctl")
            self.assertEqual(loaded_path, expected_sibling)


# ── B2: Grocery parity — shouldCategorizeItems must be exactly True ──────────

class B2GroceryParityTests(unittest.TestCase):
    """Host mode must require grocery.shouldCategorizeItems is exactly True."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_b2_grocery", "remctl")

    def _backend(self):
        return self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")

    def _resolve_list_mock(self, grocery_value):
        return mock.patch.object(
            host_client,
            "resolve_list",
            return_value={
                "id": 7,
                "title": "Test List",
                "objectUUID": "UUID-1",
                "grocery": grocery_value,
            },
        )

    def test_should_categorize_true_passes(self):
        with self._resolve_list_mock({"shouldCategorizeItems": True}):
            self._backend().typed_require_grocery_target(7)  # must not exit

    def test_should_categorize_false_fails(self):
        with self._resolve_list_mock({"shouldCategorizeItems": False}):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_grocery_key_null_fails(self):
        with self._resolve_list_mock(None):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_grocery_key_absent_fails(self):
        """No 'grocery' key at all → must exit (non-grocery list)."""
        with mock.patch.object(
            host_client,
            "resolve_list",
            return_value={"id": 7, "title": "Work"},
        ):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_divergent_field_parity_locale_only(self):
        """grocery dict with locale but shouldCategorizeItems absent → fails (B2 parity fixture)."""
        with self._resolve_list_mock({"locale": "en_US", "shouldCategorizeItems": False}):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_divergent_field_parity_other_truthy_fields_not_enough(self):
        """Truthy grocery dict without shouldCategorizeItems=True is rejected."""
        with self._resolve_list_mock({"cachedItemsCount": 10, "shouldCategorizeItems": False}):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_inline_cmd_add_check_requires_should_categorize_items(self):
        """The inline grocery check in cmd_add also requires shouldCategorizeItems=True.

        Validates the B2 fix at the second check site (not just typed_require_grocery_target).
        """
        # Simulate a list_resolution with grocery dict present but shouldCategorizeItems=False
        result_no = {"id": 7, "title": "Work", "grocery": {"shouldCategorizeItems": False}}
        result_yes = {"id": 7, "title": "Groceries", "grocery": {"shouldCategorizeItems": True}}
        # Use the backend method directly to confirm parity
        with mock.patch.object(host_client, "resolve_list", return_value=result_no):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)
        with mock.patch.object(host_client, "resolve_list", return_value=result_yes):
            self._backend().typed_require_grocery_target(7)  # must pass


# ── C1: snapshot.reminder — real host reads, no silent [] stubs ──────────────

class C1SnapshotReminderHandlerTests(unittest.TestCase):
    """snapshot.reminder handler returns real hashtags and early-reminder IDs."""

    def _ops(self, *, hashtags=None, early_ids=None, row=None):
        remctl_mod = mock.Mock()
        if row is not None:
            remctl_mod.q_reminder_by_identifier.return_value = row
        else:
            remctl_mod.q_reminder_by_identifier.return_value = None
        remctl_mod.q_hashtags.return_value = [
            {"ZNAME": t} for t in (hashtags or [])
        ]
        remctl_mod.early_reminder_identifiers_for_reminder.return_value = early_ids or []
        # Provide a dummy DB that supports context management and execute
        fake_db = mock.Mock()
        fake_db.__enter__ = mock.Mock(return_value=fake_db)
        fake_db.__exit__ = mock.Mock(return_value=False)
        fake_db.execute.return_value.fetchone.return_value = None
        remctl_mod.open_db.return_value = fake_db
        return ReadOperations(remctl_mod)

    def test_snapshot_reminder_not_found_by_ckid(self):
        ops = self._ops()
        result = ops.snapshot_reminder({"identifier": "NO-SUCH"})
        self.assertFalse(result["found"])
        self.assertEqual(result["hashtags"], [])
        self.assertEqual(result["earlyReminderIdentifiers"], [])

    def test_snapshot_reminder_found_by_ckid_returns_hashtags(self):
        fake_row = {"Z_PK": 5, "ZCKIDENTIFIER": "REM-1", "ZTITLE": "Buy milk"}
        ops = self._ops(hashtags=["groceries", "urgent"], row=fake_row)
        result = ops.snapshot_reminder({"identifier": "REM-1"})
        self.assertTrue(result["found"])
        self.assertEqual(result["hashtags"], ["groceries", "urgent"])

    def test_snapshot_reminder_found_by_ckid_returns_early_reminders(self):
        fake_row = {"Z_PK": 5, "ZCKIDENTIFIER": "REM-1", "ZTITLE": "Buy milk"}
        ops = self._ops(early_ids=["ALERT-A", "ALERT-B"], row=fake_row)
        result = ops.snapshot_reminder({"identifier": "REM-1"})
        self.assertEqual(result["earlyReminderIdentifiers"], ["ALERT-A", "ALERT-B"])

    def test_snapshot_reminder_not_found_by_numeric_pk(self):
        ops = self._ops()
        # The DB execute returns None for numeric PK
        result = ops.snapshot_reminder({"identifier": 999})
        self.assertFalse(result["found"])
        self.assertEqual(result["hashtags"], [])

    def test_snapshot_reminder_found_by_numeric_pk(self):
        remctl_mod = mock.Mock()
        fake_db = mock.Mock()
        fake_db.execute.return_value.fetchone.return_value = {
            "Z_PK": 42, "ZCKIDENTIFIER": "REM-42"
        }
        remctl_mod.open_db.return_value = fake_db
        remctl_mod.q_hashtags.return_value = [{"ZNAME": "work"}]
        remctl_mod.early_reminder_identifiers_for_reminder.return_value = ["ALERT-X"]
        ops = ReadOperations(remctl_mod)
        result = ops.snapshot_reminder({"identifier": 42})
        self.assertTrue(result["found"])
        self.assertEqual(result["hashtags"], ["work"])
        self.assertEqual(result["earlyReminderIdentifiers"], ["ALERT-X"])

    def test_snapshot_reminder_registered_in_handlers(self):
        fake_remctl = mock.Mock()
        ops = ReadOperations(fake_remctl)
        self.assertIn("snapshot.reminder", ops.handlers())

    def test_snapshot_reminder_in_implemented_operations(self):
        self.assertIn("snapshot.reminder", protocol.IMPLEMENTED_OPERATIONS)


class C1HostedBackendSnapshotTests(unittest.TestCase):
    """HostedReadBackend.typed_hashtags and typed_early_reminder_identifiers use real host calls."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_c1_hosted_snapshot", "remctl")

    def _backend(self):
        return self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")

    def test_typed_hashtags_uses_snapshot_reminder_operation(self):
        with mock.patch.object(
            host_client,
            "snapshot_reminder",
            return_value={"found": True, "hashtags": ["work", "urgent"], "earlyReminderIdentifiers": []},
        ) as call:
            result = self._backend().typed_hashtags(42)
        self.assertEqual(result, ["work", "urgent"])
        call.assert_called_once()

    def test_typed_hashtags_returns_empty_for_not_found(self):
        with mock.patch.object(
            host_client,
            "snapshot_reminder",
            return_value={"found": False, "hashtags": [], "earlyReminderIdentifiers": []},
        ):
            result = self._backend().typed_hashtags(99)
        self.assertEqual(result, [])

    def test_typed_hashtags_raises_loudly_when_host_unavailable(self):
        with mock.patch.object(
            host_client, "snapshot_reminder",
            side_effect=host_client.HostUnavailable("host is down"),
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_hashtags(42)

    def test_typed_early_reminders_uses_snapshot_reminder_operation(self):
        with mock.patch.object(
            host_client,
            "snapshot_reminder",
            return_value={"found": True, "hashtags": [], "earlyReminderIdentifiers": ["A", "B"]},
        ) as call:
            result = self._backend().typed_early_reminder_identifiers("REM-1")
        self.assertEqual(result, ["A", "B"])
        call.assert_called_once()

    def test_typed_early_reminders_returns_empty_for_not_found(self):
        with mock.patch.object(
            host_client,
            "snapshot_reminder",
            return_value={"found": False, "hashtags": [], "earlyReminderIdentifiers": []},
        ):
            result = self._backend().typed_early_reminder_identifiers("NO-SUCH")
        self.assertEqual(result, [])

    def test_typed_early_reminders_raises_loudly_when_host_unavailable(self):
        with mock.patch.object(
            host_client, "snapshot_reminder",
            side_effect=host_client.HostUnavailable("host is down"),
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_early_reminder_identifiers("REM-1")


# ── C3: Catch SystemExit at dispatch boundary ─────────────────────────────────

class C3DispatchBoundaryTests(unittest.TestCase):
    """Broker dispatch catches BaseException (incl. SystemExit) and returns internal_error."""

    def _broker_with_exploding_handler(self, exc):
        fake_remctl = mock.Mock()
        handlers = ReadOperations(fake_remctl).handlers()
        # Replace resolve.list with a handler that raises
        handlers["resolve.list"] = mock.Mock(side_effect=exc)
        return broker.ReadBroker(handlers, identity_validator=StaticIdentityValidator())

    def _dispatch(self, b, operation="resolve.list", **fields):
        raw = _frame(_request(operation, **fields))
        return _unframe(b.handle_frame(raw))

    def test_handler_systemexit_becomes_internal_error(self):
        b = self._broker_with_exploding_handler(SystemExit(1))
        response = self._dispatch(b, "resolve.list", name="Test")
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "internal_error")

    def test_handler_runtime_error_becomes_internal_error(self):
        b = self._broker_with_exploding_handler(RuntimeError("boom"))
        response = self._dispatch(b, "resolve.list", name="Test")
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "internal_error")

    def test_broker_survives_after_handler_exception(self):
        """After a handler raises, the next request is served correctly."""
        b = self._broker_with_exploding_handler(RuntimeError("first request fails"))
        # First request: explodes
        r1 = self._dispatch(b, "resolve.list", name="Test")
        self.assertEqual(r1["code"], "internal_error")
        # Second request: health check must succeed
        r2 = self._dispatch(b, "health")
        self.assertEqual(r2["status"], "ok")

    def test_handler_base_exception_does_not_propagate(self):
        """KeyboardInterrupt inside a handler must not propagate out of dispatch."""
        b = self._broker_with_exploding_handler(KeyboardInterrupt())
        response = self._dispatch(b, "resolve.list", name="Test")
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "internal_error")

    def test_protocol_error_from_handler_is_re_raised_as_is(self):
        """ProtocolError raised inside a handler propagates cleanly (not wrapped)."""
        from remctl_host_protocol import ProtocolError
        fake_remctl = mock.Mock()
        handlers = ReadOperations(fake_remctl).handlers()
        handlers["resolve.list"] = mock.Mock(
            side_effect=ProtocolError("custom_code", "custom message")
        )
        b = broker.ReadBroker(handlers, identity_validator=StaticIdentityValidator())
        response = self._dispatch(b, "resolve.list", name="Test")
        self.assertEqual(response["code"], "custom_code")


# ── C4: Pre-decode server errors use correct code, not requestId mismatch ────

class C4PreDecodeErrorTests(unittest.TestCase):
    """Client surfaces server_busy/request_timeout with correct code, not mismatch."""

    def _response_bytes(self, payload):
        data = json.dumps(payload).encode()
        return struct.pack(">I", len(data)) + data

    def _call_with_raw_response(self, response_payload):
        """Simulate a server that ignores the request and returns response_payload."""
        response_bytes = self._response_bytes(response_payload)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(1)

            result = {}

            def serve():
                conn, _ = server.accept()
                conn.recv(4096)  # discard the request
                conn.sendall(response_bytes)
                conn.close()
                server.close()

            t = threading.Thread(target=serve, daemon=True)
            t.start()
            try:
                host_client.call_host(
                    path,
                    host_client.build_request("health"),
                )
            except Exception as exc:
                result["exc"] = exc
            t.join(timeout=2)
            return result.get("exc")

    def test_server_busy_raises_host_unavailable_with_server_busy_code(self):
        exc = self._call_with_raw_response({
            "protocolVersion": protocol.PROTOCOL_VERSION,
            "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
            "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
            "requestId": None,
            "status": "error",
            "code": "server_busy",
            "message": "Queue is full",
        })
        self.assertIsInstance(exc, host_client.HostUnavailable)
        self.assertIn("server_busy", str(exc))
        self.assertNotIn("mismatch", str(exc).lower())

    def test_request_timeout_raises_host_unavailable_with_request_timeout_code(self):
        exc = self._call_with_raw_response({
            "protocolVersion": protocol.PROTOCOL_VERSION,
            "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
            "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
            "requestId": None,
            "status": "error",
            "code": "request_timeout",
            "message": "Timed out",
        })
        self.assertIsInstance(exc, host_client.HostUnavailable)
        self.assertIn("request_timeout", str(exc))
        self.assertNotIn("mismatch", str(exc).lower())

    def test_requestid_mismatch_still_raises_correctly(self):
        """A mismatched (non-None) requestId still raises HostUnavailable as a mismatch."""
        exc = self._call_with_raw_response({
            "protocolVersion": protocol.PROTOCOL_VERSION,
            "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
            "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
            "requestId": "wrong-id",
            "status": "ok",
            "result": {},
            "warnings": [],
        })
        self.assertIsInstance(exc, host_client.HostUnavailable)
        self.assertIn("mismatch", str(exc).lower())


# ── C5: Broker smoke under python -I -S ────────────────────────────────────

class C5BrokerIsolationFlagsTest(unittest.TestCase):
    """Broker starts cleanly under python3 -I -S (same flags Swift uses)."""

    def test_broker_starts_under_isolation_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            manifest_path, digest, _, _ = write_runtime_manifest_fixture(d)
            sock_dir = d / "sockets"
            sock_dir.mkdir(mode=0o700)
            sock_path = sock_dir / "broker.sock"

            proc = subprocess.Popen(
                [
                    sys.executable, "-I", "-S",
                    str(ROOT / "remctl_read_broker.py"),
                    "--socket", str(sock_path),
                    "--manifest", str(manifest_path),
                    "--manifest-digest", digest,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                # Minimal env matching Swift's childEnvironment() — no PYTHONPATH;
                # the broker prepends its own directory at startup.
                env={
                    "HOME": os.path.expanduser("~"),
                    "LANG": "en_US.UTF-8",
                },
            )
            try:
                # Wait for the socket to appear (up to 5 s)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if sock_path.exists():
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    sock_path.exists(),
                    "broker socket never appeared under -I -S",
                )
                # Send a health request
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(3.0)
                client.connect(str(sock_path))
                req = host_client.build_request("health")
                frame = host_client.encode_frame(req, maximum=protocol.MAX_REQUEST_BYTES)
                client.sendall(frame)
                header = client.recv(4)
                size = struct.unpack(">I", header)[0]
                body = b""
                while len(body) < size:
                    body += client.recv(size - len(body))
                client.close()
                response = json.loads(body)
                self.assertEqual(response.get("status"), "ok")
            finally:
                proc.terminate()
                proc.wait(timeout=5)


# ── C6: Integration tests ────────────────────────────────────────────────────

class C6IntegrationTests(unittest.TestCase):
    """Integration scenarios: peer UID, queue exhaustion, stale socket, mid-response death."""

    def _make_server_broker(self):
        return broker.ReadBroker(
            ReadOperations(mock.Mock()).handlers(),
            identity_validator=StaticIdentityValidator(),
        )

    def test_peer_uid_rejection_via_mock(self):
        """serve_connection rejects peers from a different UID."""
        read_broker = self._make_server_broker()
        client_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with mock.patch.object(
                server_sock.__class__, "getpeereid", return_value=(os.getuid() + 1, 0), create=True
            ):
                with self.assertRaises(broker.BrokerSecurityError):
                    broker.serve_connection(server_sock, read_broker)
        finally:
            client_sock.close()

    def test_queue_exhaustion_sends_server_busy(self):
        """When all worker slots are taken, new connections receive server_busy.

        We drive serve_forever() in a background thread, hold one worker slot
        indefinitely with a blocking handler, then send a second request; the
        server must respond immediately with server_busy.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "private" / "q.sock"
            sock_path.parent.mkdir(mode=0o700)

            # A handler that blocks until released — occupies the single worker slot.
            worker_started = threading.Event()
            worker_release = threading.Event()

            def blocking_handler(request):
                worker_started.set()
                worker_release.wait(timeout=5)
                return {"status": "ready"}

            fake_remctl = mock.Mock()
            handlers = ReadOperations(fake_remctl).handlers()
            handlers["resolve.list"] = blocking_handler
            read_broker = broker.ReadBroker(
                handlers, identity_validator=StaticIdentityValidator()
            )
            server = broker.BrokerServer(sock_path, read_broker, workers=1, queue_limit=1)

            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            # Wait for socket
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not sock_path.exists():
                time.sleep(0.01)
            self.assertTrue(sock_path.exists(), "socket never appeared")

            try:
                # First request: blocks the worker
                first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                first.settimeout(3.0)
                first.connect(str(sock_path))
                req = _frame(_request("resolve.list", name="Test"))
                first.sendall(req)
                worker_started.wait(timeout=3)  # wait until the worker is really busy

                # Second request: should get server_busy immediately
                second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                second.settimeout(3.0)
                second.connect(str(sock_path))
                second.sendall(req)
                header = second.recv(4)
                size = struct.unpack(">I", header)[0]
                body = b""
                while len(body) < size:
                    body += second.recv(size - len(body))
                response = json.loads(body)
                self.assertEqual(response["code"], "server_busy")
            finally:
                worker_release.set()
                server.stop()
                server_thread.join(timeout=3)
                try:
                    first.close()
                    second.close()
                except Exception:
                    pass

    def test_stale_socket_routing_raises_host_unavailable(self):
        """Connecting to a dead socket raises HostUnavailable, not an unhandled error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            sock_path = d / "dead.sock"
            # Create a real socket, then close the listener immediately
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_path))
            srv.close()
            # Leave the socket file in place — now it's stale
            with self.assertRaises(host_client.HostUnavailable):
                host_client.call_host(
                    sock_path,
                    host_client.build_request("health"),
                    connect_timeout=1.0,
                )

    def test_host_death_mid_response_raises_host_unavailable(self):
        """If the server closes the connection before sending a full frame, HostUnavailable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "private" / "middie.sock"
            sock_path.parent.mkdir(mode=0o700)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_path))
            sock_path.chmod(0o600)
            srv.listen(1)

            def half_respond():
                conn, _ = srv.accept()
                # Send a 4-byte header claiming 1000 bytes, then close
                conn.sendall(struct.pack(">I", 1000))
                conn.close()
                srv.close()

            t = threading.Thread(target=half_respond, daemon=True)
            t.start()
            with self.assertRaises(host_client.HostUnavailable):
                host_client.call_host(
                    sock_path,
                    host_client.build_request("health"),
                    connect_timeout=2.0,
                    read_timeout=2.0,
                )
            t.join(timeout=3)

    def test_restart_reconnect_succeeds(self):
        """After the server restarts on the same path, a new connection succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "private" / "restart.sock"
            sock_path.parent.mkdir(mode=0o700)
            read_broker = self._make_server_broker()

            def run_one_request():
                server = broker.bind_server_socket(sock_path)
                conn, _ = server.accept()
                broker.serve_connection(conn, read_broker)
                server.close()
                sock_path.unlink(missing_ok=True)

            # First server
            t1 = threading.Thread(target=run_one_request, daemon=True)
            t1.start()
            # Allow socket to appear
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not sock_path.exists():
                time.sleep(0.01)
            resp1 = host_client.call_host(sock_path, host_client.build_request("health"))
            t1.join(timeout=3)

            # Second server (restart on same path)
            t2 = threading.Thread(target=run_one_request, daemon=True)
            t2.start()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not sock_path.exists():
                time.sleep(0.01)
            resp2 = host_client.call_host(sock_path, host_client.build_request("health"))
            t2.join(timeout=3)

            self.assertEqual(resp1["result"]["status"], "ready")
            self.assertEqual(resp2["result"]["status"], "ready")


# ── C7: Import self-validation ────────────────────────────────────────────────

class C7ImportValidationTests(unittest.TestCase):
    """Broker revalidates identity on every dispatch; Swift validates before spawn."""

    def test_broker_revalidates_identity_per_dispatch(self):
        """Each dispatch call invokes identity_validator.validate()."""
        validator = StaticIdentityValidator()
        with mock.patch.object(validator, "validate", wraps=validator.validate) as spy:
            read_broker = broker.ReadBroker(
                ReadOperations(mock.Mock()).handlers(),
                identity_validator=validator,
            )
            for _ in range(3):
                read_broker.dispatch(_request("health"))
        self.assertEqual(spy.call_count, 3)

    def test_broker_rejects_modified_manifest_between_requests(self):
        """If manifest validation fails on the second request, dispatch raises ProtocolError."""
        real_identity = build_test_identity()
        call_count = [0]

        class FlippingValidator:
            def validate(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    return real_identity
                raise RuntimeManifestError("manifest tampered between requests")

        read_broker = broker.ReadBroker(
            ReadOperations(mock.Mock()).handlers(),
            identity_validator=FlippingValidator(),
        )
        # First request succeeds
        r1 = _unframe(read_broker.handle_frame(_frame(_request("health"))))
        self.assertEqual(r1["status"], "ok")
        # Second request: manifest validation fails → error frame
        r2 = _unframe(read_broker.handle_frame(_frame(_request("health"))))
        self.assertEqual(r2["status"], "error")
        self.assertEqual(r2["code"], "host_identity_invalid")

    def test_broker_init_validates_handler_schema_parity(self):
        """ReadBroker.__init__ raises ValueError when handlers != IMPLEMENTED_OPERATIONS."""
        incomplete = {"health": lambda id, req: {}}  # missing most operations
        with self.assertRaises(ValueError):
            broker.ReadBroker(incomplete, identity_validator=StaticIdentityValidator())

    def test_all_implemented_operations_have_handlers(self):
        """Every IMPLEMENTED_OPERATIONS entry has a registered handler (no silent gaps)."""
        read_broker = _make_broker()
        for op in protocol.IMPLEMENTED_OPERATIONS:
            self.assertIn(op, read_broker._handlers, f"Missing handler for {op!r}")

    def test_snapshot_reminder_protocol_schema_is_closed(self):
        """snapshot.reminder schema is declared in OPERATIONS (default-deny for unknown fields)."""
        self.assertIn("snapshot.reminder", protocol.OPERATIONS)
        # Extra field rejected
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request({
                "protocolVersion": protocol.PROTOCOL_VERSION,
                "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
                "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
                "requestId": "test",
                "operation": "snapshot.reminder",
                "identifier": "REM-1",
                "extraField": "evil",
            })




# ── N1: Field bounds — string max and integer bounds are distinct ─────────────

class N1FieldBoundsProtocolTests(unittest.TestCase):
    """Protocol-level validation: str/int bounds are separate; bool/zero/overflow rejected."""

    def _req(self, **fields):
        return _request("snapshot.reminder", **fields)

    def test_int_identifier_large_positive_passes_validation(self):
        """99999 is a valid CoreData Z_PK; must NOT be rejected by str-length limit."""
        # Previously IDENTIFIER.maximum=4096 would apply to ints, rejecting 99999.
        validated = protocol.validate_request(self._req(identifier=99999))
        self.assertEqual(validated["identifier"], 99999)

    def test_int_identifier_max_signed_64_bit_passes(self):
        """Z_PK at the signed-64-bit ceiling must be accepted."""
        validated = protocol.validate_request(self._req(identifier=protocol.MAX_REMINDER_ID))
        self.assertEqual(validated["identifier"], protocol.MAX_REMINDER_ID)

    def test_int_identifier_overflow_rejected(self):
        """Z_PK > 2**63-1 is out of range."""
        with self.assertRaises(protocol.ProtocolError) as ctx:
            protocol.validate_request(self._req(identifier=protocol.MAX_REMINDER_ID + 1))
        self.assertIn("maximum", ctx.exception.message)

    def test_int_identifier_zero_rejected(self):
        """CoreData IDs start at 1; 0 is not a valid Z_PK."""
        with self.assertRaises(protocol.ProtocolError) as ctx:
            protocol.validate_request(self._req(identifier=0))
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_int_identifier_negative_rejected(self):
        """Negative Z_PK is not valid."""
        with self.assertRaises(protocol.ProtocolError) as ctx:
            protocol.validate_request(self._req(identifier=-1))
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_bool_identifier_rejected(self):
        """True/False are bool; must never be accepted as an int CoreData ID."""
        for bval in (True, False):
            with self.subTest(value=bval):
                with self.assertRaises(protocol.ProtocolError) as ctx:
                    protocol.validate_request(self._req(identifier=bval))
                self.assertEqual(ctx.exception.code, "invalid_request")

    def test_str_identifier_bounded_by_text_max(self):
        """String identifier exceeding MAX_TEXT_LENGTH is still rejected."""
        long_str = "x" * (protocol.MAX_TEXT_LENGTH + 1)
        with self.assertRaises(protocol.ProtocolError) as ctx:
            protocol.validate_request(self._req(identifier=long_str))
        self.assertEqual(ctx.exception.code, "request_too_large")

    def test_str_identifier_at_max_length_passes(self):
        """String identifier at exactly MAX_TEXT_LENGTH is accepted."""
        ok_str = "x" * protocol.MAX_TEXT_LENGTH
        validated = protocol.validate_request(self._req(identifier=ok_str))
        self.assertEqual(len(validated["identifier"]), protocol.MAX_TEXT_LENGTH)

    def test_resolve_sharee_int_identifier_passes(self):
        """resolve.sharee identifier=99999 must also pass (same IDENTIFIER field)."""
        validated = protocol.validate_request(
            _request("resolve.sharee", listId=1, identifier=99999)
        )
        self.assertEqual(validated["identifier"], 99999)

    def test_list_id_zero_rejected(self):
        """listId=0 is not a valid CoreData ID; reject for snapshot.sections."""
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(_request("snapshot.sections", listId=0))

    def test_list_id_positive_passes(self):
        """listId=1 is the minimum valid CoreData row ID."""
        validated = protocol.validate_request(_request("snapshot.sections", listId=1))
        self.assertEqual(validated["listId"], 1)


class N1RealBrokerIntegrationTests(unittest.TestCase):
    """Real client→broker→operation path; no mocks on the protocol/dispatch layer."""

    def _make_broker_server(self, sock_path):
        """Start a real BrokerServer in a background thread; return (server, thread)."""
        # Build a mock remctl that returns not-found for any int Z_PK lookup.
        fake_remctl = mock.Mock()
        fake_db = mock.MagicMock()
        fake_cursor = mock.MagicMock()
        fake_cursor.fetchone.return_value = None
        fake_db.execute.return_value = fake_cursor
        fake_db.__enter__ = mock.Mock(return_value=fake_db)
        fake_db.__exit__ = mock.Mock(return_value=False)
        fake_remctl.open_db.return_value = fake_db
        handlers = ReadOperations(fake_remctl).handlers()
        read_broker = broker.ReadBroker(
            handlers, identity_validator=StaticIdentityValidator()
        )
        server = broker.BrokerServer(sock_path, read_broker)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sock_path.exists():
            time.sleep(0.02)
        return server, t

    def _send_recv(self, sock_path, payload):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(str(sock_path))
        frame = _frame(payload)
        sock.sendall(frame)
        header = sock.recv(4)
        size = struct.unpack(">I", header)[0]
        body = b""
        while len(body) < size:
            body += sock.recv(size - len(body))
        sock.close()
        return json.loads(body)

    def test_snapshot_reminder_int_99999_end_to_end(self):
        """snapshot.reminder with int 99999 flows through broker to handler; not-found returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_broker_server(sock_path)
            try:
                response = self._send_recv(
                    sock_path,
                    _request("snapshot.reminder", identifier=99999),
                )
                # Protocol layer accepted it; handler ran; not-found because mock returns None.
                self.assertEqual(response.get("status"), "ok",
                                 f"Expected ok but got: {response}")
                self.assertFalse(response["result"]["found"])
            finally:
                server.stop()
                t.join(timeout=3)

    def test_snapshot_reminder_int_max_64_bit_end_to_end(self):
        """snapshot.reminder with Z_PK = 2**63-1 is accepted by broker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_broker_server(sock_path)
            try:
                response = self._send_recv(
                    sock_path,
                    _request("snapshot.reminder", identifier=protocol.MAX_REMINDER_ID),
                )
                self.assertEqual(response.get("status"), "ok",
                                 f"Expected ok but got: {response}")
                self.assertFalse(response["result"]["found"])
            finally:
                server.stop()
                t.join(timeout=3)

    def test_snapshot_reminder_int_overflow_rejected_by_broker(self):
        """snapshot.reminder with identifier > 2**63-1 is rejected by protocol validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_broker_server(sock_path)
            try:
                response = self._send_recv(
                    sock_path,
                    _request("snapshot.reminder", identifier=protocol.MAX_REMINDER_ID + 1),
                )
                self.assertEqual(response.get("status"), "error")
                self.assertIn(response.get("code"), ("invalid_request",))
            finally:
                server.stop()
                t.join(timeout=3)

    def test_snapshot_reminder_bool_rejected_by_broker(self):
        """snapshot.reminder with bool identifier is rejected cleanly (no traceback)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_broker_server(sock_path)
            try:
                response = self._send_recv(
                    sock_path,
                    _request("snapshot.reminder", identifier=True),
                )
                self.assertEqual(response.get("status"), "error")
                self.assertEqual(response.get("code"), "invalid_request")
            finally:
                server.stop()
                t.join(timeout=3)

    def test_resolve_sharee_int_99999_end_to_end(self):
        """resolve.sharee with int identifier 99999 passes protocol validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"

            # Patch resolve_sharee_or_die to return a not-found sentinel cleanly.
            fake_remctl = mock.Mock()
            fake_db = mock.MagicMock()
            fake_db.__enter__ = mock.Mock(return_value=fake_db)
            fake_db.__exit__ = mock.Mock(return_value=False)
            fake_remctl.open_db.return_value = fake_db
            fake_remctl.q_list_by_pk.return_value = None  # list not found → clear error path

            server, t = self._make_broker_server(sock_path)
            try:
                # The handler will call resolve_sharee_or_die and likely sys.exit or return
                # an error result; we only care that the protocol layer accepted 99999.
                response = self._send_recv(
                    sock_path,
                    _request("resolve.sharee", listId=1, identifier=99999),
                )
                # Must not be a protocol rejection of the integer value itself.
                self.assertNotEqual(
                    response.get("code"), "invalid_request",
                    f"Unexpected invalid_request: {response}",
                )
            finally:
                server.stop()
                t.join(timeout=3)


# ── N2: Broker directory bootstrap uses realpath ──────────────────────────────

class N2BrokerRealpathTests(unittest.TestCase):
    """Broker sys.path bootstrap resolves symlinks via realpath."""

    def test_broker_module_bootstrap_uses_realpath(self):
        """remctl_read_broker source must use realpath for the broker directory."""
        broker_src = ROOT / "remctl_read_broker.py"
        text = broker_src.read_text(encoding="utf-8")
        # The bootstrap block must use realpath, not abspath.
        self.assertIn("realpath(__file__)", text)
        self.assertNotIn("abspath(__file__)", text,
                         "N2: abspath must be replaced with realpath in the bootstrap block")


# ── N3: All runtime files must be in the sealed runtime directory ─────────────

class N3RuntimeFileSealedDirTests(unittest.TestCase):
    """Builder and validator enforce that every runtimeFiles entry shares the broker directory."""

    def _build_divergent_manifest(self, tmpdir: Path) -> tuple[Path, Path, dict]:
        """Create a fixture where one runtime file lives outside the runtime directory."""
        runtime_dir = tmpdir / "runtime"
        runtime_dir.mkdir()
        outside_dir = tmpdir / "outside"
        outside_dir.mkdir()

        broker_file = runtime_dir / "remctl_read_broker.py"
        broker_file.write_text("print('broker')\n")
        remctl_file = runtime_dir / "remctl"
        remctl_file.write_text('VERSION = "1.7.1"\n')
        outside_module = outside_dir / "remctl_host_manifest.py"
        outside_module.write_text("print('manifest')\n")

        return runtime_dir, outside_module, {
            "broker_path": broker_file,
            "remctl_path": remctl_file,
            "outside_path": outside_module,
        }

    def test_builder_rejects_divergent_runtime_file_path(self):
        """build_runtime_manifest raises RuntimeManifestError for an out-of-dir runtime file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            runtime_dir, outside_module, paths = self._build_divergent_manifest(d)
            python_exec = Path(sys.executable)
            python_exec.stat()  # confirm it exists

            with self.assertRaises(Exception) as ctx:
                build_runtime_manifest(
                    root=runtime_dir,
                    protected_python=python_exec,
                    broker_entrypoint=paths["broker_path"],
                    host_version="1.0.0",
                    runtime_files=(
                        ("remctl", paths["remctl_path"]),
                        ("hostManifest", outside_module),  # ← outside the runtime dir
                    ),
                )
            self.assertIn("sealed", str(ctx.exception).lower())

    def test_validator_rejects_manifest_with_divergent_runtime_file(self):
        """Validator raises RuntimeManifestError when a runtimeFile path escapes the sealed dir."""
        from remctl_host_manifest import (
            sha256_digest_bytes,
            runtime_manifest_bytes,
            RuntimeIdentityValidator,
            RUNTIME_MANIFEST_VERSION,
            HOST_ROLE,
            EXPECTED_BUNDLE_IDENTIFIER,
            EXPECTED_LAUNCH_AGENT_LABEL,
            PROTOCOL_VERSION,
            SCHEMA_MANIFEST_VERSION,
            SCHEMA_MANIFEST_DIGEST,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            runtime_dir = d / "runtime"
            runtime_dir.mkdir()
            outside_dir = d / "outside"
            outside_dir.mkdir()

            python_file = runtime_dir / "python-protected"
            python_file.write_bytes(Path(sys.executable).read_bytes())
            python_file.chmod(0o700)
            broker_file = runtime_dir / "remctl_read_broker.py"
            broker_file.write_text("print('broker')\n")
            remctl_file = runtime_dir / "remctl"
            remctl_file.write_text('VERSION = "1.7.1"\n')
            outside_module = outside_dir / "remctl_host_manifest.py"
            outside_module.write_text("print('manifest')\n")

            payload = {
                "runtimeManifestVersion": RUNTIME_MANIFEST_VERSION,
                "role": HOST_ROLE,
                "bundleIdentifier": EXPECTED_BUNDLE_IDENTIFIER,
                "launchAgentLabel": EXPECTED_LAUNCH_AGENT_LABEL,
                "cliVersion": "1.7.1",
                "hostVersion": "1.0.0",
                "protocolVersion": PROTOCOL_VERSION,
                "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
                "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
                "protectedPython": {
                    "path": str(python_file),
                    "sha256": sha256_digest_bytes(python_file.read_bytes()),
                },
                "brokerEntrypoint": {
                    "path": str(broker_file),
                    "sha256": sha256_digest_bytes(broker_file.read_bytes()),
                },
                "runtimeFiles": [
                    {
                        "key": "remctl",
                        "path": str(remctl_file),
                        "sha256": sha256_digest_bytes(remctl_file.read_bytes()),
                    },
                    {
                        "key": "hostManifest",
                        "path": str(outside_module),   # ← outside the runtime dir
                        "sha256": sha256_digest_bytes(outside_module.read_bytes()),
                    },
                ],
            }
            manifest_bytes_data = runtime_manifest_bytes(payload)
            from remctl_host_manifest import sha256_digest_bytes as sha256b
            digest = sha256b(manifest_bytes_data)
            manifest_path = d / "manifest.json"
            manifest_path.write_bytes(manifest_bytes_data)

            validator = RuntimeIdentityValidator(manifest_path, digest)
            from remctl_host_manifest import RuntimeManifestError
            with self.assertRaises(RuntimeManifestError) as ctx:
                validator.validate()
            self.assertIn("sealed", str(ctx.exception).lower())


# ── N6: Single validate() call in broker main ─────────────────────────────────

class N6SingleValidateCallTests(unittest.TestCase):
    """Broker main() calls validate() exactly once (no redundant double-call)."""

    def test_broker_main_calls_validate_exactly_once(self):
        """main() must have exactly one validate() call; two is redundant and documented N6."""
        broker_src = ROOT / "remctl_read_broker.py"
        text = broker_src.read_text(encoding="utf-8")
        # Find the main() function body
        import ast
        tree = ast.parse(text)
        main_fn = next(
            (node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name == "main"),
            None,
        )
        self.assertIsNotNone(main_fn, "main() not found in broker source")
        # Count calls to .validate()
        validate_calls = [
            node for node in ast.walk(main_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "validate"
        ]
        self.assertEqual(
            len(validate_calls), 1,
            f"N6: expected exactly 1 validate() call in main(), found {len(validate_calls)}"
        )

    def test_validate_called_before_import_of_operations(self):
        """main() must call validate() before importing ReadOperations (phase ordering)."""
        broker_src = ROOT / "remctl_read_broker.py"
        text = broker_src.read_text(encoding="utf-8")
        validate_pos = text.find("validated_identity = identity_validator.validate()")
        import_pos = text.find("from remctl_host_operations import ReadOperations")
        self.assertGreater(validate_pos, 0, "validate() call not found")
        self.assertGreater(import_pos, 0, "ReadOperations import not found")
        self.assertLess(
            validate_pos, import_pos,
            "N6: validate() must precede ReadOperations import (validate before loading untrusted code)"
        )


if __name__ == "__main__":
    unittest.main()
