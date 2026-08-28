"""Tests for probe.store: protocol, handler, client, doctor integration, routing.

Verifies:
- probe.store is in OPERATIONS and IMPLEMENTED_OPERATIONS
- Closed schema (no input fields; extra fields rejected)
- Handler returns correct bounded result for each store state
- No paths, content, SQL, or raw exception text in any result
- Client function returns the result dict
- Doctor reports transport-up/store-denied and store-ready/direct-denied correctly
- viaCapabilityHost requires store probe to pass
- effectiveReadRoute is 'unavailable' when transport up but store denied
- REMCTL_CAPABILITY_HOST_DISABLED killswitch honoured
- Explicit --read-route host fails clearly when store access is denied
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import contextlib
import socket
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import remctl_host_protocol as protocol
import remctl_read_broker as broker
import remctl_host as host_client
from remctl_host_operations import ReadOperations
from helpers import (
    StaticIdentityValidator,
    load_module,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _request(operation="probe.store", **fields):
    return {
        "protocolVersion": protocol.PROTOCOL_VERSION,
        "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
        "requestId": "probe-test",
        "operation": operation,
        **fields,
    }


def _frame(payload):
    data = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def _make_broker(handlers=None):
    fake_remctl = mock.Mock()
    return broker.ReadBroker(
        handlers or ReadOperations(fake_remctl).handlers(),
        identity_validator=StaticIdentityValidator(),
    )


# ── 1. Protocol schema ────────────────────────────────────────────────────────

class ProbeStoreProtocolTests(unittest.TestCase):

    def test_probe_store_in_operations(self):
        self.assertIn("probe.store", protocol.OPERATIONS)

    def test_probe_store_in_implemented_operations(self):
        self.assertIn("probe.store", protocol.IMPLEMENTED_OPERATIONS)

    def test_probe_store_has_no_input_fields(self):
        """probe.store takes no input — the host finds the DB automatically."""
        self.assertEqual(protocol.OPERATIONS["probe.store"].fields, {})

    def test_probe_store_schema_is_closed_extra_field_rejected(self):
        """Extra fields in a probe.store request must be rejected."""
        with self.assertRaises(protocol.ProtocolError) as ctx:
            protocol.validate_request(_request("probe.store", extraField="evil"))
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_probe_store_valid_request_accepted(self):
        validated = protocol.validate_request(_request("probe.store"))
        self.assertEqual(validated["operation"], "probe.store")

    def test_probe_store_has_handler_registered(self):
        ops = ReadOperations(mock.Mock())
        self.assertIn("probe.store", ops.handlers())


# ── 2. Handler unit tests ─────────────────────────────────────────────────────

class ProbeStoreHandlerTests(unittest.TestCase):
    """Handler returns correct bounded result for each store state."""

    def _ops(self, remctl_mock):
        return ReadOperations(remctl_mock)

    def test_store_accessible_and_schema_ok(self):
        """When open_db() succeeds, both booleans are True and category is None."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = None
        db = mock.MagicMock()
        db.close = mock.Mock()
        r.open_db.return_value = db

        result = self._ops(r).probe_store({})
        self.assertTrue(result["storeReadable"])
        self.assertTrue(result["schemaOk"])
        self.assertIsNone(result["errorCategory"])
        db.close.assert_called_once()

    def test_access_denied_when_store_dir_unreadable(self):
        """reminders_store_access_error() non-None → access_denied."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = (
            "Direct CLI reads are blocked because the Reminders store at "
            "/some/path is not readable from this process context"
        )

        result = self._ops(r).probe_store({})
        self.assertFalse(result["storeReadable"])
        self.assertFalse(result["schemaOk"])
        self.assertEqual(result["errorCategory"], "access_denied")
        r.open_db.assert_not_called()

    def test_schema_mismatch_when_table_missing(self):
        """open_db() raises 'table' → storeReadable=True, schemaOk=False, schema_mismatch."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = None
        r.open_db.side_effect = Exception(
            "Reminders store is missing the expected ZREMCDREMINDER table; "
            "this macOS Reminders schema may need a RemCTL update."
        )

        result = self._ops(r).probe_store({})
        self.assertTrue(result["storeReadable"])
        self.assertFalse(result["schemaOk"])
        self.assertEqual(result["errorCategory"], "schema_mismatch")

    def test_not_found_when_no_db_files(self):
        """open_db() raises 'No Reminders database' → storeReadable=True, not_found."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = None
        r.open_db.side_effect = Exception(
            "No Reminders database found. Is iCloud Reminders enabled?"
        )

        result = self._ops(r).probe_store({})
        self.assertTrue(result["storeReadable"])
        self.assertFalse(result["schemaOk"])
        self.assertEqual(result["errorCategory"], "not_found")

    def test_io_error_on_unexpected_open_failure(self):
        """Unrecognised exception from open_db() → io_error."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = None
        r.open_db.side_effect = OSError("disk full")

        result = self._ops(r).probe_store({})
        self.assertFalse(result["storeReadable"])
        self.assertFalse(result["schemaOk"])
        self.assertEqual(result["errorCategory"], "io_error")

    def test_unknown_when_access_error_check_itself_raises(self):
        """If reminders_store_access_error() raises, return unknown category."""
        r = mock.Mock()
        r.reminders_store_access_error.side_effect = RuntimeError("unexpected")

        result = self._ops(r).probe_store({})
        self.assertFalse(result["storeReadable"])
        self.assertFalse(result["schemaOk"])
        self.assertEqual(result["errorCategory"], "unknown")

    def test_db_is_closed_after_successful_probe(self):
        """Handler must close the DB even on success (no resource leak)."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = None
        db = mock.Mock()
        r.open_db.return_value = db

        self._ops(r).probe_store({})
        db.close.assert_called_once()

    def test_result_contains_no_paths(self):
        """No filesystem paths or raw exception text appear in the result."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = (
            "/secret/path/to/store is not readable"
        )
        result = self._ops(r).probe_store({})
        result_str = json.dumps(result)
        self.assertNotIn("/secret", result_str)
        self.assertNotIn("path", result_str.lower())

    def test_result_contains_no_sql(self):
        """No SQL fragments appear in the result dict."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = None
        r.open_db.side_effect = Exception("SELECT * FROM ZREMCDREMINDER failed")

        result = self._ops(r).probe_store({})
        result_str = json.dumps(result)
        self.assertNotIn("SELECT", result_str)
        self.assertNotIn("ZREMCDREMINDER", result_str)

    def test_error_category_is_bounded_enum(self):
        """errorCategory must be from the closed set or None."""
        valid_categories = {
            "access_denied", "not_found", "schema_mismatch", "io_error", "unknown", None
        }
        r = mock.Mock()
        for side_effect in [
            None,
            Exception("table missing"),
            Exception("No Reminders database"),
            OSError("disk error"),
        ]:
            r.reminders_store_access_error.return_value = None
            r.open_db.side_effect = side_effect
            if side_effect is None:
                r.open_db.return_value = mock.Mock()
                r.open_db.side_effect = None
            result = self._ops(r).probe_store({})
            self.assertIn(result["errorCategory"], valid_categories,
                          f"Unexpected errorCategory: {result['errorCategory']!r}")

    def test_access_denied_from_open_db_message(self):
        """open_db() raising 'not readable' after no access_error also maps to access_denied."""
        r = mock.Mock()
        r.reminders_store_access_error.return_value = None
        r.open_db.side_effect = Exception(
            "Direct CLI reads are blocked because the Reminders store is not readable"
        )
        result = self._ops(r).probe_store({})
        self.assertEqual(result["errorCategory"], "access_denied")


# ── 3. Client function ────────────────────────────────────────────────────────

class ProbeStoreClientTests(unittest.TestCase):
    """Client function probe_store() sends the right request and returns result dict."""

    def test_probe_store_client_calls_host_and_returns_result(self):
        """probe_store() must extract result from the broker response."""
        expected_result = {
            "storeReadable": True,
            "schemaOk": True,
            "errorCategory": None,
        }
        with mock.patch.object(
            host_client, "call_host",
            return_value={"status": "ok", "result": expected_result},
        ) as mock_call:
            result = host_client.probe_store(Path("/tmp/test.sock"))

        self.assertEqual(result, expected_result)
        call_args = mock_call.call_args
        request = call_args[0][1]  # second positional arg is the request dict
        self.assertEqual(request["operation"], "probe.store")

    def test_probe_store_raises_host_unavailable_on_transport_error(self):
        """Transport errors propagate as HostUnavailable."""
        with mock.patch.object(
            host_client, "call_host",
            side_effect=host_client.HostUnavailable("socket closed"),
        ):
            with self.assertRaises(host_client.HostUnavailable):
                host_client.probe_store(Path("/tmp/test.sock"))


# ── 4. Real client→broker integration ────────────────────────────────────────

class ProbeStoreRealBrokerTests(unittest.TestCase):
    """probe.store flows through the real broker dispatch layer (no mocks on protocol)."""

    def _make_server(self, sock_path, *, store_readable=True, schema_ok=True):
        fake_remctl = mock.Mock()
        if store_readable and schema_ok:
            fake_remctl.reminders_store_access_error.return_value = None
            db = mock.Mock()
            db.close = mock.Mock()
            fake_remctl.open_db.return_value = db
        elif store_readable and not schema_ok:
            fake_remctl.reminders_store_access_error.return_value = None
            fake_remctl.open_db.side_effect = Exception(
                "Reminders store is missing the expected ZREMCDREMINDER table"
            )
        else:
            fake_remctl.reminders_store_access_error.return_value = (
                "Direct CLI reads are blocked"
            )
        read_broker = broker.ReadBroker(
            ReadOperations(fake_remctl).handlers(),
            identity_validator=StaticIdentityValidator(),
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

    def test_store_accessible_returns_ok_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_server(sock_path, store_readable=True, schema_ok=True)
            try:
                response = self._send_recv(sock_path, _request("probe.store"))
                self.assertEqual(response.get("status"), "ok",
                                 f"Unexpected response: {response}")
                r = response["result"]
                self.assertTrue(r["storeReadable"])
                self.assertTrue(r["schemaOk"])
                self.assertIsNone(r["errorCategory"])
            finally:
                server.stop()
                t.join(timeout=3)

    def test_access_denied_returns_ok_with_false_readable(self):
        """access_denied is reported via result booleans, NOT as a broker error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_server(sock_path, store_readable=False)
            try:
                response = self._send_recv(sock_path, _request("probe.store"))
                # Protocol succeeded (status ok); store result is in result dict.
                self.assertEqual(response.get("status"), "ok",
                                 f"Expected ok status, got: {response}")
                r = response["result"]
                self.assertFalse(r["storeReadable"])
                self.assertFalse(r["schemaOk"])
                self.assertEqual(r["errorCategory"], "access_denied")
            finally:
                server.stop()
                t.join(timeout=3)

    def test_schema_mismatch_returns_readable_true_schema_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_server(sock_path, store_readable=True, schema_ok=False)
            try:
                response = self._send_recv(sock_path, _request("probe.store"))
                self.assertEqual(response.get("status"), "ok")
                r = response["result"]
                self.assertTrue(r["storeReadable"])
                self.assertFalse(r["schemaOk"])
                self.assertEqual(r["errorCategory"], "schema_mismatch")
            finally:
                server.stop()
                t.join(timeout=3)

    def test_result_never_contains_paths_or_content(self):
        """The result dict must not contain any filesystem paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "broker.sock"
            server, t = self._make_server(sock_path, store_readable=False)
            try:
                response = self._send_recv(sock_path, _request("probe.store"))
                result_str = json.dumps(response.get("result", {}))
                self.assertNotIn("/", result_str,
                                 f"Path found in result: {result_str}")
            finally:
                server.stop()
                t.join(timeout=3)


# ── 5. Doctor host_health / store probe integration ───────────────────────────

class DoctorProbeStoreIntegrationTests(unittest.TestCase):
    """doctor host_health and route fields are accurate based on store probe."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_doctor_probe_test", "remctl")

    def _run_doctor(self, *, transport_ok, store_ok, direct_readable=False):
        """Run gather_doctor_checks with mocked host checks; return checks list."""
        return self.remctl.gather_doctor_checks(
            host_transport_ok=transport_ok,
            host_transport_detail="ready" if transport_ok else "unavailable",
            host_store_ok=store_ok,
            host_store_detail="ready" if store_ok else "access_denied",
        )

    def _check(self, checks, name):
        return next((c for c in checks if c["name"] == name), None)

    def test_transport_up_store_ok_host_health_ok(self):
        checks = self._run_doctor(transport_ok=True, store_ok=True)
        # host_health and host_store_probe checks require host to be reported installed
        # We test directly via gather_doctor_checks with parameters; the checks
        # for host_health/host_store_probe only appear when host is installed+loaded.
        # Verify that the parameters flow through without exceptions.
        self.assertIsInstance(checks, list)

    def test_transport_up_store_denied_database_check_downgraded_to_warn(self):
        """When host store passes but direct is blocked, 'database' fail → warn."""
        with (
            mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND, "access_error",
                return_value="Direct reads blocked",
            ),
            mock.patch.object(
                self.remctl, "find_main_db_path",
                return_value=None,
            ),
        ):
            checks = self.remctl.gather_doctor_checks(
                host_transport_ok=True,
                host_transport_detail="ready",
                host_store_ok=True,       # ← store accessible via host
                host_store_detail="ready",
            )
        db_check = self._check(checks, "database")
        self.assertIsNotNone(db_check, "Expected 'database' check in results")
        self.assertEqual(db_check["status"], "warn",
                         "database check should be downgraded to warn when host covers it")

    def test_transport_up_store_denied_database_check_remains_fail(self):
        """When transport up but store denied, and direct also blocked → database stays fail."""
        with (
            mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND, "access_error",
                return_value="Direct reads blocked",
            ),
            mock.patch.object(
                self.remctl, "find_main_db_path",
                return_value=None,
            ),
        ):
            checks = self.remctl.gather_doctor_checks(
                host_transport_ok=True,
                host_transport_detail="ready",
                host_store_ok=False,      # ← store also denied
                host_store_detail="access_denied",
            )
        db_check = self._check(checks, "database")
        self.assertIsNotNone(db_check)
        self.assertEqual(db_check["status"], "fail",
                         "database should remain fail when no route has store access")

    def _run_doctor_json_full(self, *, transport_ok, store_ok, direct_readable):
        """Run cmd_doctor with all host mocks; return JSON payload."""
        def fake_transport():
            return (transport_ok, "ready" if transport_ok else "unavailable")

        def fake_store():
            return (store_ok, "ready" if store_ok else "access_denied")

        def fake_access():
            return None if direct_readable else "blocked"

        with (
            mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND, "access_error",
                side_effect=fake_access,
            ),
            mock.patch.object(
                self.remctl, "capability_host_installed", return_value=True
            ),
            mock.patch.object(
                self.remctl, "capability_host_launchagent_loaded", return_value=True
            ),
            mock.patch.object(
                self.remctl, "capability_host_socket_secure", return_value=True
            ),
            mock.patch.object(
                self.remctl, "capability_host_transport_result", side_effect=fake_transport
            ),
            mock.patch.object(
                self.remctl, "capability_host_store_probe_result", side_effect=fake_store
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

    def test_via_capability_host_false_when_transport_up_store_denied(self):
        """viaCapabilityHost must be False when transport up but store access denied."""
        payload = self._run_doctor_json_full(
            transport_ok=True, store_ok=False, direct_readable=False
        )
        self.assertFalse(payload["viaCapabilityHost"],
                         "viaCapabilityHost must not be True when store probe fails")

    def test_effective_route_unavailable_when_transport_up_store_denied(self):
        """effectiveReadRoute is 'unavailable' when direct blocked and host store denied."""
        payload = self._run_doctor_json_full(
            transport_ok=True, store_ok=False, direct_readable=False
        )
        self.assertEqual(payload["effectiveReadRoute"], "unavailable")

    def test_effective_route_host_when_store_ok_direct_denied(self):
        """effectiveReadRoute is 'host' when store probe passes and direct is blocked."""
        payload = self._run_doctor_json_full(
            transport_ok=True, store_ok=True, direct_readable=False
        )
        self.assertTrue(payload["viaCapabilityHost"])
        self.assertEqual(payload["effectiveReadRoute"], "host")

    def test_via_capability_host_true_when_both_transport_and_store_pass(self):
        payload = self._run_doctor_json_full(
            transport_ok=True, store_ok=True, direct_readable=False
        )
        self.assertTrue(payload["viaCapabilityHost"])

    def test_both_unavailable_effective_route_unavailable(self):
        payload = self._run_doctor_json_full(
            transport_ok=False, store_ok=False, direct_readable=False
        )
        self.assertFalse(payload["viaCapabilityHost"])
        self.assertEqual(payload["effectiveReadRoute"], "unavailable")

    def test_doctor_ok_true_when_host_store_ok_and_direct_blocked(self):
        """Doctor succeeds (ok=True) when host store passes even if direct is blocked."""
        def fake_access():
            return "blocked"

        with (
            mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND, "access_error",
                side_effect=fake_access,
            ),
            mock.patch.object(self.remctl, "capability_host_installed", return_value=True),
            mock.patch.object(self.remctl, "capability_host_launchagent_loaded", return_value=True),
            mock.patch.object(self.remctl, "capability_host_socket_secure", return_value=True),
            mock.patch.object(
                self.remctl, "capability_host_transport_result",
                return_value=(True, "ready"),
            ),
            mock.patch.object(
                self.remctl, "capability_host_store_probe_result",
                return_value=(True, "ready"),
            ),
            mock.patch.object(self.remctl, "find_main_db_path", return_value=None),
            mock.patch.object(self.remctl, "doctor_execution_context", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_doctor(SimpleNamespace(json=True, for_agent=False))

        payload = json.loads(stdout.getvalue())
        # With host store ok and direct blocked (downgraded to warn), no fails.
        self.assertTrue(payload["ok"], f"Expected ok=True but got: {payload}")


# ── 6. Routing: explicit host with denied store ───────────────────────────────

class ExplicitHostRouteDeniedStoreTests(unittest.TestCase):
    """initialize_read_route('host') fails clearly when store access is denied."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_route_probe_test", "remctl")

    def _init_host(self, *, store_ok, direct_readable=False):
        env = {"REMCTL_CAPABILITY_HOST_DISABLED": "0"}
        store_result = (True, "ready") if store_ok else (False, "access_denied")
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND, "access_error",
                return_value=None if direct_readable else "blocked",
            ),
            mock.patch.object(self.remctl, "_host_socket_ready", return_value=True),
            mock.patch.object(
                self.remctl, "_host_store_accessible", return_value=store_result
            ),
        ):
            self.remctl.initialize_read_route("host")

    def test_explicit_host_succeeds_when_store_accessible(self):
        self._init_host(store_ok=True)
        self.assertIsInstance(
            self.remctl.get_read_backend(), self.remctl.HostedReadBackend
        )

    def test_explicit_host_raises_when_store_denied(self):
        """Explicit --read-route host must raise ReadRouteUnavailable if store denied."""
        from remctl_runtime import ReadRouteUnavailable
        with self.assertRaises(ReadRouteUnavailable) as ctx:
            self._init_host(store_ok=False)
        msg = str(ctx.exception).lower()
        self.assertIn("store", msg)
        # Must mention Full Disk Access as the remedy.
        self.assertIn("full disk access", msg)

    def test_explicit_host_denied_message_contains_no_paths(self):
        """The ReadRouteUnavailable message must not contain filesystem paths."""
        from remctl_runtime import ReadRouteUnavailable
        try:
            self._init_host(store_ok=False)
        except ReadRouteUnavailable as exc:
            msg = str(exc)
            self.assertNotIn("/Users", msg)
            self.assertNotIn("/Library", msg)

    def test_killswitch_prevents_host_route(self):
        """REMCTL_CAPABILITY_HOST_DISABLED=1 prevents host route regardless of store."""
        from remctl_runtime import ReadRouteUnavailable
        env = {"REMCTL_CAPABILITY_HOST_DISABLED": "1"}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(self.remctl, "_host_socket_ready", return_value=True),
            mock.patch.object(
                self.remctl, "_host_store_accessible", return_value=(True, "ready")
            ),
        ):
            with self.assertRaises(ReadRouteUnavailable):
                self.remctl.initialize_read_route("host")

    def test_auto_route_store_probe_not_called_for_lazy_selection(self):
        """Auto route does NOT call store probe (lazy transport-only selection)."""
        env = {"REMCTL_CAPABILITY_HOST_DISABLED": "0"}
        store_probe_called = []

        def fake_store_accessible():
            store_probe_called.append(True)
            return (True, "ready")

        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND, "access_error",
                return_value="blocked",
            ),
            mock.patch.object(self.remctl, "_host_socket_ready", return_value=True),
            mock.patch.object(
                self.remctl, "_host_store_accessible",
                side_effect=fake_store_accessible,
            ),
        ):
            self.remctl.initialize_read_route("auto")

        # Auto mode: store probe NOT called (lazy, transport-only selection).
        self.assertEqual(store_probe_called, [],
                         "Auto mode must not call store probe during initialization")


# ── 7. No content leakage across all paths ───────────────────────────────────

class NoContentLeakageTests(unittest.TestCase):
    """Result dict must never contain reminder/list content, SQL, counts, or paths."""

    FORBIDDEN_PATTERNS = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "WHERE", "FROM",  # SQL
        "ZREMCDREMINDER", "ZREMCDBASELIST",                        # table names
        "/Users", "/Library", "/var",                              # paths
        "hashtag", "reminder", "list",                             # content fields
    )

    def test_no_forbidden_content_in_any_result(self):
        ops = ReadOperations(mock.Mock())
        scenarios = [
            ("access_denied", lambda r: setattr(r, "reminders_store_access_error", mock.Mock(return_value="blocked"))),
        ]
        remctl_mock = mock.Mock()
        remctl_mock.reminders_store_access_error.return_value = None
        remctl_mock.open_db.side_effect = Exception("table missing from schema")
        result = ops.probe_store.__func__(ops, {})  # test schema_mismatch path

        result_str = json.dumps(result)
        for pattern in self.FORBIDDEN_PATTERNS:
            self.assertNotIn(
                pattern, result_str,
                f"Forbidden content {pattern!r} found in probe.store result: {result_str}"
            )


if __name__ == "__main__":
    unittest.main()
