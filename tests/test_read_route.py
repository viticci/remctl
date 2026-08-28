"""Tests for HostedReadBackend, --read-route, killswitch, and routing parity.

Coverage:
- HostedReadBackend: resolve_list, resolve_reminder_by_ckid, open_db raises
- initialize_read_route: auto/direct/host selection and killswitch
- requested_read_route: CLI wins over env, invalid routes fail
- cmd_add list-resolution routing: direct vs hosted code paths
- Protocol mutation-negative: verify no mutation operations are exposed
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import load_module
from remctl_host_protocol import IMPLEMENTED_OPERATIONS, OPERATIONS
from remctl_runtime import (
    ReadRouteUnavailable,
    requested_read_route,
    select_read_route,
)


class RequestedReadRouteTests(unittest.TestCase):
    """requested_read_route: CLI value wins; env fallback; invalid routes raise."""

    def test_cli_value_wins_over_env(self):
        with mock.patch.dict(os.environ, {"REMCTL_READ_ROUTE": "host"}):
            self.assertEqual(requested_read_route("direct"), "direct")

    def test_env_fallback_when_cli_is_none(self):
        with mock.patch.dict(os.environ, {"REMCTL_READ_ROUTE": "direct"}):
            self.assertEqual(requested_read_route(None), "direct")

    def test_default_is_auto(self):
        env = {k: v for k, v in os.environ.items() if k != "REMCTL_READ_ROUTE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(requested_read_route(None), "auto")

    def test_invalid_route_raises(self):
        with self.assertRaises(ValueError):
            requested_read_route("garbage")

    def test_invalid_route_from_env_raises(self):
        with mock.patch.dict(os.environ, {"REMCTL_READ_ROUTE": "bogus"}):
            with self.assertRaises(ValueError):
                requested_read_route(None)

    def test_all_valid_routes_accepted(self):
        for route in ("auto", "direct", "host"):
            self.assertEqual(requested_read_route(route), route)


class SelectReadRouteTests(unittest.TestCase):
    """select_read_route: deterministic auto|direct|host selection and killswitch."""

    def test_auto_prefers_direct_when_readable(self):
        self.assertEqual(
            select_read_route("auto", direct_readable=True, host_ready=True),
            "direct",
        )

    def test_auto_falls_to_host_when_direct_unavailable(self):
        self.assertEqual(
            select_read_route("auto", direct_readable=False, host_ready=True),
            "host",
        )

    def test_auto_fails_when_both_unavailable(self):
        with self.assertRaises(ReadRouteUnavailable):
            select_read_route("auto", direct_readable=False, host_ready=False)

    def test_direct_fails_when_unreadable(self):
        with self.assertRaises(ReadRouteUnavailable):
            select_read_route("direct", direct_readable=False, host_ready=True)

    def test_host_fails_when_not_ready(self):
        with self.assertRaises(ReadRouteUnavailable):
            select_read_route("host", direct_readable=True, host_ready=False)

    def test_host_disabled_killswitch_blocks_explicit_host(self):
        with self.assertRaises(ReadRouteUnavailable):
            select_read_route(
                "host", direct_readable=True, host_ready=True, host_disabled=True
            )

    def test_host_disabled_killswitch_skips_host_in_auto(self):
        """auto must fall through to direct when host is disabled."""
        self.assertEqual(
            select_read_route(
                "auto", direct_readable=True, host_ready=True, host_disabled=True
            ),
            "direct",
        )

    def test_host_disabled_killswitch_fails_auto_when_both_unavailable(self):
        with self.assertRaises(ReadRouteUnavailable):
            select_read_route(
                "auto", direct_readable=False, host_ready=True, host_disabled=True
            )


class HostedReadBackendTests(unittest.TestCase):
    """HostedReadBackend: open_db raises; typed calls delegate to host client."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_backend_test", "remctl")

    def _backend(self, socket_path="/tmp/test.sock"):
        return self.remctl.HostedReadBackend(socket_path=socket_path)

    def test_route_name_is_host(self):
        self.assertEqual(self._backend().route_name, "host")

    def test_open_db_raises_reminders_db_unavailable(self):
        backend = self._backend()
        with self.assertRaises(self.remctl.RemindersDBUnavailable):
            backend.open_db()

    def test_open_db_error_message_names_hosted_mode(self):
        backend = self._backend()
        try:
            backend.open_db()
        except self.remctl.RemindersDBUnavailable as exc:
            self.assertIn("Capability Host", str(exc))

    def test_resolve_list_by_name_calls_host(self):
        backend = self._backend(socket_path="/sock/path")
        fake_result = {"id": 5, "title": "Shopping", "method": "exact"}
        with mock.patch(
            "remctl_host.resolve_list", return_value=fake_result
        ) as mock_call:
            result = backend.resolve_list(name="Shopping")
        mock_call.assert_called_once_with(
            Path("/sock/path"), name="Shopping", list_id=None, allow_groups=False
        )
        self.assertEqual(result["id"], 5)

    def test_resolve_list_host_unavailable_raises_db_unavailable(self):
        backend = self._backend()
        from remctl_host import HostUnavailable

        with mock.patch("remctl_host.resolve_list", side_effect=HostUnavailable("down")):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                backend.resolve_list(name="Groceries")

    def test_resolve_reminder_by_ckid_calls_host(self):
        backend = self._backend(socket_path="/sock/path")
        fake_result = {"id": 99, "Z_PK": 99, "identifier": "CKID-99"}
        with mock.patch(
            "remctl_host.resolve_reminder", return_value=fake_result
        ) as mock_call:
            result = backend.resolve_reminder_by_ckid("CKID-99")
        mock_call.assert_called_once_with(Path("/sock/path"), "CKID-99")
        self.assertEqual(result["id"], 99)

    def test_resolve_reminder_empty_identifier_returns_none(self):
        backend = self._backend()
        self.assertIsNone(backend.resolve_reminder_by_ckid(""))
        self.assertIsNone(backend.resolve_reminder_by_ckid(None))

    def test_resolve_reminder_host_unavailable_returns_none(self):
        backend = self._backend()
        from remctl_host import HostUnavailable

        with mock.patch(
            "remctl_host.resolve_reminder", side_effect=HostUnavailable("down")
        ):
            result = backend.resolve_reminder_by_ckid("CKID-X")
        self.assertIsNone(result)

    def test_backend_is_read_backend_subclass(self):
        backend = self._backend()
        self.assertIsInstance(backend, self.remctl.ReadBackend)


class InitializeReadRouteTests(unittest.TestCase):
    """initialize_read_route activates the correct backend and honours killswitch."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_init_route_test", "remctl")

    def _init(self, cli_value, *, direct_readable, host_ready, host_disabled=False):
        # Always explicitly set the killswitch to avoid leakage from user env.
        env = {
            "REMCTL_CAPABILITY_HOST_DISABLED": "1" if host_disabled else "0",
        }
        store_result = (True, "ready") if host_ready else (False, "transport_unavailable")
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(
                self.remctl.DIRECT_READ_BACKEND,
                "access_error",
                return_value=None if direct_readable else "blocked",
            ):
                with mock.patch.object(
                    self.remctl,
                    "_host_socket_ready",
                    return_value=host_ready,
                ):
                    with mock.patch.object(
                        self.remctl,
                        "_host_store_accessible",
                        return_value=store_result,
                    ):
                        self.remctl.initialize_read_route(cli_value)

    def test_direct_route_sets_direct_backend(self):
        self._init("direct", direct_readable=True, host_ready=False)
        self.assertIsInstance(
            self.remctl.get_read_backend(), self.remctl.DirectReadBackend
        )

    def test_host_route_sets_hosted_backend(self):
        self._init("host", direct_readable=False, host_ready=True)
        self.assertIsInstance(
            self.remctl.get_read_backend(), self.remctl.HostedReadBackend
        )

    def test_auto_prefers_direct(self):
        self._init("auto", direct_readable=True, host_ready=True)
        self.assertIsInstance(
            self.remctl.get_read_backend(), self.remctl.DirectReadBackend
        )

    def test_auto_falls_to_host(self):
        self._init("auto", direct_readable=False, host_ready=True)
        self.assertIsInstance(
            self.remctl.get_read_backend(), self.remctl.HostedReadBackend
        )

    def test_killswitch_prevents_host_in_auto(self):
        self._init("auto", direct_readable=True, host_ready=True, host_disabled=True)
        self.assertIsInstance(
            self.remctl.get_read_backend(), self.remctl.DirectReadBackend
        )

    def test_none_cli_value_uses_env_or_auto(self):
        env = {k: v for k, v in os.environ.items() if k != "REMCTL_READ_ROUTE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self._init(None, direct_readable=True, host_ready=False)
        self.assertIsInstance(
            self.remctl.get_read_backend(), self.remctl.DirectReadBackend
        )


class ProtocolMutationNegativeTests(unittest.TestCase):
    """The implemented protocol must not expose any mutation or write operations."""

    MUTATION_KEYWORDS = (
        "create",
        "update",
        "delete",
        "write",
        "set",
        "add",
        "remove",
        "modify",
        "patch",
        "insert",
        "upsert",
    )

    def test_implemented_operations_contain_no_mutation_keywords(self):
        for op in IMPLEMENTED_OPERATIONS:
            for kw in self.MUTATION_KEYWORDS:
                self.assertNotIn(
                    kw,
                    op.lower(),
                    f"Implemented operation {op!r} contains mutation keyword {kw!r}",
                )

    def test_all_defined_operations_contain_no_mutation_keywords(self):
        for op in OPERATIONS:
            for kw in self.MUTATION_KEYWORDS:
                self.assertNotIn(
                    kw,
                    op.lower(),
                    f"Protocol operation {op!r} contains mutation keyword {kw!r}",
                )

    def test_resolve_reminder_is_implemented(self):
        self.assertIn("resolve.reminder", IMPLEMENTED_OPERATIONS)

    def test_resolve_list_is_implemented(self):
        self.assertIn("resolve.list", IMPLEMENTED_OPERATIONS)

    def test_health_is_implemented(self):
        self.assertIn("health", IMPLEMENTED_OPERATIONS)

    def test_implemented_operations_are_subset_of_operations(self):
        for op in IMPLEMENTED_OPERATIONS:
            self.assertIn(op, OPERATIONS)


class DirectVsHostParityTests(unittest.TestCase):
    """Parity: hosted and direct routes produce structurally compatible results."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_parity_test", "remctl")

    def test_hosted_backend_resolve_list_result_has_required_cmd_add_keys(self):
        """Hosted resolve.list result must have the keys cmd_add relies on."""
        backend = self.remctl.HostedReadBackend(socket_path="/sock")
        fake_result = {
            "id": 3,
            "title": "Groceries",
            "method": "exact",
            "objectUUID": "LIST-3",
            "grocery": {"sections": []},
        }
        with mock.patch("remctl_host.resolve_list", return_value=fake_result):
            result = backend.resolve_list(name="Groceries")
        # cmd_add relies on "id" and "title"
        self.assertIn("id", result)
        self.assertIn("title", result)
        self.assertEqual(result["id"], 3)
        self.assertEqual(result["title"], "Groceries")

    def test_hosted_backend_resolve_reminder_result_has_z_pk_alias(self):
        """Hosted reminder identity dict must expose Z_PK for existing cmd_add code."""
        backend = self.remctl.HostedReadBackend(socket_path="/sock")
        fake_result = {
            "id": 17,
            "Z_PK": 17,
            "identifier": "CKID-17",
            "title": "Call dentist",
            "listId": 1,
            "completed": False,
            "deleted": False,
        }
        with mock.patch("remctl_host.resolve_reminder", return_value=fake_result):
            result = backend.resolve_reminder_by_ckid("CKID-17")
        self.assertEqual(result["Z_PK"], 17)
        self.assertEqual(result["id"], 17)

    def test_hosted_backend_open_db_raises_reminders_db_unavailable(self):
        """open_db() must raise RemindersDBUnavailable (not a generic exception)."""
        backend = self.remctl.HostedReadBackend()
        with self.assertRaises(self.remctl.RemindersDBUnavailable):
            backend.open_db()

    def test_direct_backend_open_db_is_callable(self):
        """DirectReadBackend exposes an open_db() that is callable."""
        self.assertTrue(callable(self.remctl.DirectReadBackend().open_db))

    def test_hosted_backend_route_name_distinct_from_direct(self):
        direct = self.remctl.DirectReadBackend()
        hosted = self.remctl.HostedReadBackend()
        self.assertNotEqual(direct.route_name, hosted.route_name)
        self.assertEqual(direct.route_name, "direct")
        self.assertEqual(hosted.route_name, "host")


if __name__ == "__main__":
    unittest.main()
