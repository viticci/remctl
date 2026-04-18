from __future__ import annotations

import io
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import load_module


class _DummyHandler:
    def __init__(self, headers=None, body=b"", api_token="secret"):
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.server = SimpleNamespace(
            api_token=api_token,
            allow_origin=None,
            enable_opengraph=False,
        )
        self.errors = []

    def _error(self, message, status=400):
        self.errors.append((message, status))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_module("remctl_server_test", "remctl-server")

    def _route_handler(self):
        handler = _DummyHandler()
        handler.ok_payload = None
        handler._ok = lambda data: setattr(handler, "ok_payload", data)
        handler._log_timing = mock.Mock()
        handler._match = self.server.RemctlHandler._match.__get__(handler, _DummyHandler)
        return handler

    def test_read_body_parses_json(self):
        handler = _DummyHandler(
            headers={"Content-Length": "16"},
            body=b'{"title":"Test"}',
        )
        body, error_status, error_message = self.server.RemctlHandler._read_body(handler)
        self.assertEqual(body, {"title": "Test"})
        self.assertIsNone(error_status)
        self.assertIsNone(error_message)

    def test_read_body_rejects_oversized_payloads(self):
        handler = _DummyHandler(headers={"Content-Length": str(self.server.MAX_REQUEST_BODY_BYTES + 1)})
        body, error_status, error_message = self.server.RemctlHandler._read_body(handler)
        self.assertIsNone(body)
        self.assertEqual(error_status, 413)
        self.assertIn("too large", error_message.lower())

    def test_check_auth_accepts_valid_bearer_token(self):
        handler = _DummyHandler(headers={"Authorization": "Bearer secret"})
        allowed = self.server.RemctlHandler._check_auth(handler)
        self.assertTrue(allowed)
        self.assertEqual(handler.errors, [])

    def test_check_auth_rejects_invalid_bearer_token(self):
        handler = _DummyHandler(headers={"Authorization": "Bearer wrong"})
        allowed = self.server.RemctlHandler._check_auth(handler)
        self.assertFalse(allowed)
        self.assertEqual(handler.errors, [("Invalid token", 401)])

    def test_internal_error_hides_exception_details(self):
        handler = _DummyHandler()
        handler._log_timing = mock.Mock()
        with mock.patch.object(self.server.sys, "stderr", io.StringIO()) as stderr:
            self.server.RemctlHandler._internal_error(
                handler,
                RuntimeError("database path leaked"),
                "GET",
                "/api/v1/test",
                0.0,
            )
        self.assertEqual(handler.errors, [("Internal server error", 500)])
        handler._log_timing.assert_called_once_with("GET", "/api/v1/test", 500, 0.0)
        self.assertIn("database path leaked", stderr.getvalue())

    def test_bridge_call_uses_cli_fallback_when_bridge_missing(self):
        action_data = {"action": "create", "title": "Test reminder"}
        with (
            mock.patch.object(self.server, "BRIDGE_PATH", Path("/definitely/not-there")),
            mock.patch.object(
                self.server,
                "remctl_cli_fallback",
                return_value={"ok": False, "error": "remctl not found"},
            ) as cli_fallback,
        ):
            result = self.server.bridge_call(action_data)

        cli_fallback.assert_called_once_with(action_data)
        self.assertEqual(result, {"ok": False, "error": "remctl not found"})

    def test_bridge_call_verifies_created_identifier_from_bridge_result(self):
        class _BridgePath:
            def exists(self):
                return True

            def __str__(self):
                return "/tmp/remctl-bridge"

        fake_db = mock.Mock()
        fake_db.execute.return_value.fetchone.return_value = [None]
        fake_proc = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "created", "id": "ABC-123", "title": "Test reminder"}),
            stderr="",
        )
        with (
            mock.patch.object(self.server, "BRIDGE_PATH", _BridgePath()),
            mock.patch.object(self.server.subprocess, "run", return_value=fake_proc),
            mock.patch.object(self.server, "open_db", return_value=fake_db),
            mock.patch.object(self.server.time, "sleep"),
        ):
            result = self.server.bridge_call({"action": "create", "title": "Test reminder"})
        self.assertEqual(result["status"], "created")
        fake_db.execute.assert_called_once_with(
            "SELECT ZACCOUNT FROM ZREMCDREMINDER WHERE ZCKIDENTIFIER = ?",
            ("ABC-123",),
        )
        fake_db.close.assert_called_once()

    def test_remctl_cli_fallback_maps_null_due_to_clear(self):
        captured = {}

        def fake_run(args, capture_output, text, timeout):
            captured["args"] = args
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(self.server.subprocess, "run", side_effect=fake_run):
            self.server.remctl_cli_fallback({"action": "update", "id": 42, "due": None})
        self.assertEqual(
            captured["args"],
            [str(self.server.REMCTL_PATH), "--format", "json", "edit", "42", "-d", "clear"],
        )

    def test_q_upcoming_uses_shared_day_window(self):
        fake = mock.Mock()
        fake.execute.return_value.fetchall.return_value = []
        start = datetime(2026, 4, 18, 0, 0, 0)
        end = datetime(2026, 4, 26, 0, 0, 0)
        with (
            mock.patch.object(self.server, "upcoming_window", return_value=(start, end)) as window,
            mock.patch.object(self.server, "to_ts", side_effect=[111, 222]),
        ):
            self.server.q_upcoming(fake, days=7)
        sql = fake.execute.call_args.args[0]
        self.assertIn("r.ZDUEDATE >= 111", sql)
        self.assertIn("r.ZDUEDATE < 222", sql)
        window.assert_called_once_with(7)

    def test_q_overdue_uses_start_of_day_cutoff(self):
        fake = mock.Mock()
        fake.execute.return_value.fetchall.return_value = []
        cutoff = datetime(2026, 4, 18, 0, 0, 0)
        with (
            mock.patch.object(self.server, "start_of_day", return_value=cutoff) as start_of_day,
            mock.patch.object(self.server, "to_ts", return_value=333),
        ):
            self.server.q_overdue(fake)
        sql = fake.execute.call_args.args[0]
        self.assertIn("r.ZDUEDATE < 333", sql)
        start_of_day.assert_called_once_with()

    def test_q_search_escapes_like_wildcards(self):
        """A search for `%` should match a literal `%`, not every row."""
        fake = mock.Mock()
        fake.execute.return_value.fetchall.return_value = []
        self.server.q_search(fake, "%")
        sql, params = fake.execute.call_args.args
        self.assertIn("ESCAPE '\\'", sql)
        # pattern should contain the ESCAPED `%`, not a raw `%` → no wildcard match
        self.assertEqual(params[0], "%\\%%")
        self.assertEqual(params[1], "%\\%%")

    def test_q_search_escapes_underscore(self):
        fake = mock.Mock()
        fake.execute.return_value.fetchall.return_value = []
        self.server.q_search(fake, "a_b")
        _, params = fake.execute.call_args.args
        self.assertEqual(params[0], "%a\\_b%")

    def test_route_get_list_supports_completed_query(self):
        handler = self._route_handler()
        fake_db = mock.Mock()
        with (
            mock.patch.object(self.server, "open_db", return_value=fake_db),
            mock.patch.object(self.server, "q_list_pk", return_value=7),
            mock.patch.object(self.server, "q_reminders", return_value=[]) as q_reminders,
            mock.patch.object(self.server, "reminders_to_list", return_value=[]),
        ):
            self.server.RemctlHandler._route_get(
                handler,
                "/api/v1/lists/Inbox",
                {"completed": ["1"]},
                0.0,
            )
        q_reminders.assert_called_once_with(fake_db, list_pk=7, completed=True, top_level=True)
        self.assertEqual(handler.ok_payload, [])
        fake_db.close.assert_called_once()

    def test_route_get_search_supports_completed_query(self):
        handler = self._route_handler()
        fake_db = mock.Mock()
        with (
            mock.patch.object(self.server, "open_db", return_value=fake_db),
            mock.patch.object(self.server, "q_search", return_value=[]) as q_search,
            mock.patch.object(self.server, "reminders_to_list", return_value=[]),
        ):
            self.server.RemctlHandler._route_get(
                handler,
                "/api/v1/search",
                {"q": ["ship"], "completed": ["true"]},
                0.0,
            )
        q_search.assert_called_once_with(fake_db, "ship", completed=True)
        self.assertEqual(handler.ok_payload, [])
        fake_db.close.assert_called_once()

    def test_route_get_reminder_detail_includes_attachments(self):
        handler = self._route_handler()
        fake_db = mock.Mock()
        reminder = {"ZLIST": 1, "ZCKIDENTIFIER": "ABC", "ZPARENTREMINDER": 0}
        with (
            mock.patch.object(self.server, "open_db", return_value=fake_db),
            mock.patch.object(self.server, "q_reminder", return_value=reminder),
            mock.patch.object(self.server, "q_section_memberships", return_value={}),
            mock.patch.object(self.server, "q_attachments", return_value=[
                {
                    "ZFILENAME": "spec.pdf",
                    "ZATTACHMENTTYPERAWVALUE": 4,
                    "ZUTI": "com.adobe.pdf",
                }
            ]),
            mock.patch.object(self.server, "q_reminders", return_value=[]),
            mock.patch.object(self.server, "reminder_to_dict", return_value={"id": 42, "title": "Ship remctl"}),
        ):
            self.server.RemctlHandler._route_get(handler, "/api/v1/reminders/42", {}, 0.0)
        self.assertEqual(
            handler.ok_payload["attachments"],
            [{"filename": "spec.pdf", "type": 4, "uti": "com.adobe.pdf"}],
        )
        fake_db.close.assert_called_once()

    def test_route_patch_forwards_recurrence_and_alarm(self):
        handler = self._route_handler()
        with mock.patch.object(self.server, "bridge_call", return_value={"status": "updated"}) as bridge_call:
            self.server.RemctlHandler._route_patch(
                handler,
                "/api/v1/reminders/42",
                {},
                {"recurrence": {"frequency": "weekly"}, "alarm": "-15m"},
                0.0,
            )
        self.assertEqual(
            bridge_call.call_args.args[0],
            {
                "action": "update",
                "id": 42,
                "recurrence": {"frequency": "weekly"},
                "alarm": "-15m",
            },
        )
        self.assertEqual(handler.ok_payload, {"status": "updated"})


if __name__ == "__main__":
    unittest.main()
