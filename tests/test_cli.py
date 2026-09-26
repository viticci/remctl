from __future__ import annotations

import contextlib
import base64
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import remctl_runtime
from helpers import load_module


class CliTests(unittest.TestCase):
    _absent_host_status = {
        "status": "error",
        "installed": False,
        "available": False,
        "ready": False,
        "fullReady": False,
        "protocolVersion": None,
        "expectedProtocolVersion": 2,
        "scope": "complete-protected-cli",
        "socket": {"path": "/tmp/remctl-test.sock", "exists": False, "secure": False},
        "launchAgent": {"path": "/tmp/remctl-test.plist", "installed": False, "loaded": False},
        "app": {"path": "/tmp/RemCTL Capability Host.app", "installed": False},
        "permissions": None,
        "commandScope": None,
        "error": {"code": "host_unavailable", "message": "not installed"},
    }

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")
        cls._real_should_route = cls.remctl.remctl_broker.should_route
        cls._default_protocol_probe = mock.patch.object(
            cls.remctl,
            "_probe_private_protocol_version",
            return_value={"ok": True, "version": 2},
        )
        cls._default_protocol_probe.start()
        cls._default_host_route = mock.patch.object(
            cls.remctl.remctl_broker,
            "should_route",
            return_value=False,
        )
        cls._default_host_status = mock.patch.object(
            cls.remctl.remctl_broker,
            "status",
            return_value=cls._absent_host_status,
        )
        cls._default_host_route.start()
        cls._default_host_status.start()

    @classmethod
    def tearDownClass(cls):
        cls._default_host_status.stop()
        cls._default_host_route.stop()
        cls._default_protocol_probe.stop()

    def setUp(self):
        self._color_enabled_before_test = self.remctl.C.enabled

    def tearDown(self):
        self.remctl.C.enabled = self._color_enabled_before_test

    @staticmethod
    def _bridge_result(payload, returncode=0):
        return {
            "returncode": returncode,
            "stdout": json.dumps(payload),
            "stderr": "",
            "payload": payload,
        }

    def _valid_capability_host_status(self, app_path):
        return {
            **self._absent_host_status,
            "installed": True,
            "app": {
                "path": str(app_path),
                "installed": True,
                "bundleIdentifier": self.remctl.remctl_broker.BUNDLE_IDENTIFIER,
                "signature": {
                    "valid": True,
                    "identifier": self.remctl.remctl_broker.BUNDLE_IDENTIFIER,
                    "teamID": "TEAM123",
                    "cdhash": "0123456789abcdef0123456789abcdef01234567",
                    "designatedRequirement": (
                        "identifier net.macstories.remctl.capability-host and anchor apple generic"
                    ),
                    "error": None,
                },
            },
        }

    @staticmethod
    def _write_capability_host_marker(root, app_path):
        marker = Path(root) / ".remctl-capability-host-app"
        marker.write_text(f"{app_path}\n")
        marker.chmod(0o600)
        return marker

    def test_parse_alarm_normalizes_relative_and_absolute_values(self):
        self.assertEqual(self.remctl.parse_alarm("15m"), "-15m")
        self.assertEqual(self.remctl.parse_alarm("2h"), "-2h")
        self.assertEqual(
            self.remctl.parse_alarm("2026-04-15 14:00"),
            "2026-04-15T14:00:00",
        )

    def test_parse_early_reminder_normalizes_due_date_delta(self):
        self.assertEqual(
            self.remctl.parse_early_reminder("15 minutes"),
            {"unit": 0, "unitName": "minutes", "count": -15, "value": 15},
        )
        self.assertEqual(
            self.remctl.parse_early_reminder("1h before"),
            {"unit": 1, "unitName": "hour", "count": -1, "value": 1},
        )
        self.assertEqual(self.remctl.parse_early_reminder("clear"), {"clear": True})
        self.assertIsNone(self.remctl.parse_early_reminder("eventually"))

    def test_parse_due_accepts_today_and_tomorrow_with_times(self):
        today_due = self.remctl.parse_due("today at 3pm")
        tomorrow_due = self.remctl.parse_due("tomorrow 15:30")
        tonight_due = self.remctl.parse_due("tonight at 11")
        friday_due = self.remctl.parse_due("Friday at 15:00")

        self.assertIsNotNone(today_due)
        self.assertEqual((today_due.hour, today_due.minute), (15, 0))
        self.assertIsNotNone(tomorrow_due)
        self.assertEqual((tomorrow_due.hour, tomorrow_due.minute), (15, 30))
        self.assertEqual((tomorrow_due.date() - today_due.date()).days, 1)
        self.assertIsNotNone(tonight_due)
        self.assertEqual((tonight_due.hour, tonight_due.minute), (23, 0))
        self.assertIsNotNone(friday_due)
        self.assertEqual((friday_due.hour, friday_due.minute), (15, 0))

    def test_parse_due_rejects_invalid_clock_time(self):
        self.assertIsNone(self.remctl.parse_due("today at 25:00"))

    def test_parse_completion_date_accepts_only_absolute_values(self):
        self.assertEqual(
            self.remctl.parse_completion_date("2026-05-27"),
            self.remctl.datetime(2026, 5, 27, 0, 0),
        )
        self.assertEqual(
            self.remctl.parse_completion_date("2026-05-27 09:30"),
            self.remctl.datetime(2026, 5, 27, 9, 30),
        )
        self.assertEqual(
            self.remctl.parse_completion_date("2026-05-27T09:30:15"),
            self.remctl.datetime(2026, 5, 27, 9, 30, 15),
        )
        self.assertIsNone(self.remctl.parse_completion_date("today"))
        self.assertIsNone(self.remctl.parse_completion_date("+3d"))
        self.assertIsNone(self.remctl.parse_completion_date("2026-02-31"))

    def test_due_spec_is_all_day_for_date_only_inputs(self):
        self.assertTrue(self.remctl.due_spec_is_all_day("today"))
        self.assertTrue(self.remctl.due_spec_is_all_day("tomorrow"))
        self.assertTrue(self.remctl.due_spec_is_all_day("2026-06-01"))
        self.assertTrue(self.remctl.due_spec_is_all_day("+3d"))
        self.assertTrue(self.remctl.due_spec_is_all_day("in 2 weeks"))
        self.assertTrue(self.remctl.due_spec_is_all_day("next friday"))
        self.assertFalse(self.remctl.due_spec_is_all_day("today at 3pm"))
        self.assertFalse(self.remctl.due_spec_is_all_day("2026-06-01 09:30"))
        self.assertFalse(self.remctl.due_spec_is_all_day("+2h"))
        self.assertFalse(self.remctl.due_spec_is_all_day("eod"))

    def test_due_spec_is_all_day_uses_parsedatetime_status_fallback(self):
        fake_date = self.remctl.datetime(2026, 3, 30, 0, 0)
        fake_datetime = self.remctl.datetime(2026, 3, 30, 15, 0)

        class FakeCal:
            def __init__(self, status):
                self._status = status

            def parseDT(self, _s):
                if self._status == 1:
                    return fake_date, 1
                return fake_datetime, self._status

        with mock.patch.object(self.remctl, "_cal", FakeCal(1)):
            self.assertTrue(self.remctl.due_spec_is_all_day("March 30"))
        with mock.patch.object(self.remctl, "_cal", FakeCal(3)):
            self.assertFalse(self.remctl.due_spec_is_all_day("March 30 at 3pm"))
        with mock.patch.object(self.remctl, "_cal", None):
            self.assertFalse(self.remctl.due_spec_is_all_day("March 30"))

    def test_add_passes_all_day_to_bridge_for_date_only_due(self):
        args = SimpleNamespace(
            title="Test",
            list=None,
            list_id=None,
            notes=None,
            due="today",
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "REMINDER-1"}),
            ) as bridge_call_result,
            mock.patch.object(
                self.remctl,
                "open_db",
                side_effect=self.remctl.RemindersDBUnavailable("no db"),
            ),
            mock.patch.object(sys, "stdout", new_callable=io.StringIO),
        ):
            self.remctl.cmd_add(args)

        bridge_call_result.assert_called_once()
        payload = bridge_call_result.call_args.args[0]
        self.assertEqual(payload["action"], "create")
        self.assertEqual(payload["due"], self.remctl.parse_due("today").isoformat())
        self.assertTrue(payload["allDay"])

    def test_parse_recurrence_rejects_invalid_specs(self):
        self.assertEqual(
            self.remctl.parse_recurrence("weekly mon,wed"),
            {"frequency": "weekly", "interval": 1, "daysOfWeek": [2, 4]},
        )
        self.assertIsNone(self.remctl.parse_recurrence("fortnightly"))
        self.assertIsNone(self.remctl.parse_recurrence("weekly funday"))
        self.assertIsNone(self.remctl.parse_recurrence("monthly 0,32"))

    def test_parse_recurrence_accepts_interval_tokens(self):
        self.assertEqual(
            self.remctl.parse_recurrence("yearly x2"),
            {"frequency": "yearly", "interval": 2},
        )
        self.assertEqual(
            self.remctl.parse_recurrence("weekly x2 thu"),
            {"frequency": "weekly", "interval": 2, "daysOfWeek": [5]},
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly x6"),
            {"frequency": "monthly", "interval": 6},
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly x2 4th-fri"),
            {
                "frequency": "monthly",
                "interval": 2,
                "daysOfWeek": [6],
                "weekNumbers": [4],
            },
        )
        self.assertEqual(
            self.remctl.parse_recurrence("daily x999"),
            {"frequency": "daily", "interval": 999},
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly x3 15"),
            {"frequency": "monthly", "interval": 3, "daysOfMonth": [15]},
        )

    def test_parse_recurrence_rejects_bad_interval_tokens(self):
        for spec in (
            "yearly x0",
            "yearly x",
            "yearly x1000",
            "yearly x²",
            "daily x2 extra",
            "daily x2 mon",
            "weekly x2 thu extra",
        ):
            with self.subTest(spec=spec):
                self.assertIsNone(self.remctl.parse_recurrence(spec))
        # A huge digit run must return None, not trip CPython's int() length cap.
        self.assertIsNone(self.remctl.parse_recurrence("yearly x" + "9" * 5000))

    def test_parse_recurrence_accepts_nth_weekday_tokens(self):
        self.assertEqual(
            self.remctl.parse_recurrence("monthly 4th-fri"),
            {
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [6],
                "weekNumbers": [4],
            },
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly 1st-mon,3rd-mon"),
            {
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [2, 2],
                "weekNumbers": [1, 3],
            },
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly -1-fri"),
            {
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [6],
                "weekNumbers": [-1],
            },
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly last-fri"),
            self.remctl.parse_recurrence("monthly -1-fri"),
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly last-sun"),
            {
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [1],
                "weekNumbers": [-1],
            },
        )
        self.assertEqual(
            self.remctl.parse_recurrence("monthly 2nd-tue,-2-thu"),
            {
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [3, 5],
                "weekNumbers": [2, -2],
            },
        )

    def test_parse_recurrence_rejects_bad_nth_weekday_tokens(self):
        for spec in (
            "monthly 6th-fri",
            "monthly 0th-fri",
            "monthly -6-fri",
            "monthly 4st-fri",
            "monthly 1th-fri",
            "monthly 2rd-fri",
            "monthly 4th-funday",
            "monthly 4th-fri,15",
            "monthly 15,4th-fri",
            "monthly last-funday",
            "weekly 4th-fri",
            "yearly 4th-fri",
        ):
            with self.subTest(spec=spec):
                self.assertIsNone(self.remctl.parse_recurrence(spec))

    def test_parse_recurrence_rejects_non_decimal_day_of_month(self):
        self.assertIsNone(self.remctl.parse_recurrence("monthly ²"))
        self.assertIsNone(self.remctl.parse_recurrence("monthly " + "9" * 5000))

    def test_recurrence_summary_renders_week_pinned_days_and_counts(self):
        summary = self.remctl.recurrence_summary
        self.assertEqual(
            summary({
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [6],
                "weekNumbers": [4],
            }),
            "monthly 4th Fri",
        )
        self.assertEqual(
            summary({
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [6],
                "weekNumbers": [-1],
            }),
            "monthly last Fri",
        )
        self.assertEqual(
            summary({
                "frequency": "monthly",
                "interval": 1,
                "daysOfWeek": [6],
                "weekNumbers": [-2],
            }),
            "monthly 2nd-last Fri",
        )
        self.assertEqual(
            summary({
                "frequency": "monthly",
                "interval": 2,
                "daysOfWeek": [3],
                "weekNumbers": [4],
            }),
            "every 2 months 4th Tue",
        )
        # The stored-blob shape from recurrence_from_row renders identically.
        self.assertEqual(
            summary({
                "frequency": "monthly",
                "interval": 2,
                "daysOfWeekDetailed": [{"weekNumber": 4, "dayOfTheWeek": 3}],
                "daysOfWeek": [3],
            }),
            "every 2 months 4th Tue",
        )
        # weekNumber 0 in the blob means "any week".
        self.assertEqual(
            summary({
                "frequency": "weekly",
                "interval": 1,
                "daysOfWeekDetailed": [
                    {"weekNumber": 0, "dayOfTheWeek": 2},
                    {"weekNumber": 0, "dayOfTheWeek": 4},
                ],
                "daysOfWeek": [2, 4],
            }),
            "weekly Mon, Wed",
        )
        # Missing weekNumbers entries stay unpinned.
        self.assertEqual(
            summary({"frequency": "weekly", "interval": 1, "daysOfWeek": [2, 4]}),
            "weekly Mon, Wed",
        )
        self.assertEqual(
            summary({"frequency": "daily", "interval": 1, "count": 5}),
            "daily, 5 times",
        )
        self.assertEqual(
            summary({"frequency": "daily", "interval": 1, "count": 1}),
            "daily, 1 time",
        )

    def test_private_call_returns_structured_error_payload(self):
        with mock.patch.object(
            self.remctl,
            "private_call_result",
            return_value={
                "returncode": 1,
                "stdout": '{"status":"error","message":"ReminderKit unavailable"}',
                "stderr": "",
                "payload": {"status": "error", "message": "ReminderKit unavailable"},
            },
        ):
            self.assertEqual(
                self.remctl.private_call({"action": "create_list"}),
                {"status": "error", "message": "ReminderKit unavailable"},
            )

    def test_private_call_result_timeout_reports_structured_error(self):
        with (
            mock.patch.object(
                self.remctl.subprocess,
                "run",
                side_effect=self.remctl.subprocess.TimeoutExpired(["remctl-private"], 30),
            ),
            mock.patch.object(self.remctl, "remindd_running", return_value=False),
        ):
            result = self.remctl.private_call_result({"action": "set_flagged"}, timeout=30)
        self.assertEqual(result["payload"]["status"], "timeout")
        self.assertIn("30s", result["payload"]["message"])
        self.assertIn("remindd", result["payload"]["message"])

    def test_private_call_result_missing_helper_reports_structured_error(self):
        with mock.patch.object(self.remctl.subprocess, "run", side_effect=FileNotFoundError):
            result = self.remctl.private_call_result({"action": "set_flagged"})
        self.assertEqual(result["payload"]["status"], "missing_helper")
        self.assertIn("Reinstall", result["payload"]["message"])

    def test_private_call_result_non_json_output_reports_stderr(self):
        proc = SimpleNamespace(returncode=2, stdout="", stderr="ReminderKit unavailable")
        with mock.patch.object(self.remctl.subprocess, "run", return_value=proc):
            result = self.remctl.private_call_result({"action": "set_flagged"})
        self.assertEqual(result["payload"]["status"], "error")
        self.assertEqual(result["payload"]["message"], "ReminderKit unavailable")

    def test_private_create_list_retries_transient_helper_error(self):
        transient = {
            "status": "error",
            "message": "Couldn’t communicate with a helper application.",
        }
        created = {"status": "created", "name": "Project X"}
        with (
            mock.patch.object(self.remctl, "private_call", side_effect=[transient, created]) as private_call,
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=0),
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            result = self.remctl.private_create_list_call({"action": "create_list"}, "Project X")

        self.assertEqual(result, created)
        self.assertEqual(private_call.call_count, 2)

    def test_private_create_list_rechecks_before_retrying_transient_helper_error(self):
        transient = {
            "status": "error",
            "message": "Couldn’t communicate with a helper application.",
        }
        with (
            mock.patch.object(self.remctl, "private_call", return_value=transient) as private_call,
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=1),
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            result = self.remctl.private_create_list_call({"action": "create_list"}, "Project X")

        self.assertEqual(result["status"], "created")
        self.assertTrue(result["verifiedAfterTransientError"])
        self.assertEqual(private_call.call_count, 1)

    def test_private_action_retries_idempotent_transient_helper_error(self):
        transient = {
            "status": "error",
            "message": "Couldn’t communicate with a helper application.",
        }
        updated = {"status": "updated", "action": "set_flagged"}
        with (
            mock.patch.object(self.remctl, "private_call", side_effect=[transient, updated]) as private_call,
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            result = self.remctl.private_action({
                "action": "set_flagged",
                "id": "REMINDER-1",
                "flagged": True,
            })

        self.assertEqual(result, updated)
        self.assertEqual(private_call.call_count, 2)

    def test_private_action_does_not_retry_additive_private_metadata(self):
        transient = {
            "status": "error",
            "message": "Couldn’t communicate with a helper application.",
        }
        with (
            mock.patch.object(self.remctl, "private_call", return_value=transient) as private_call,
            mock.patch.object(sys, "stderr", new_callable=io.StringIO),
        ):
            with self.assertRaises(SystemExit):
                self.remctl.private_action({
                    "action": "add_private_metadata",
                    "id": "REMINDER-1",
                    "tags": ["remctl"],
                })

        self.assertEqual(private_call.call_count, 1)

    def test_private_location_action_prefers_eventkit_bridge(self):
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "updated"}) as bridge_call,
            mock.patch.object(self.remctl, "private_call") as private_call,
        ):
            result = self.remctl.private_location_action({
                "action": "add_location_alarm",
                "id": "REMINDER-1",
                "title": "Apple Park",
                "latitude": 37.3349,
                "longitude": -122.0090,
                "radius": 200,
                "proximity": 1,
            })

        bridge_call.assert_called_once()
        self.assertEqual(bridge_call.call_args.args[0]["locationTitle"], "Apple Park")
        private_call.assert_not_called()
        self.assertEqual(result["source"], "eventkit_bridge")

    def test_current_helper_paths_honor_environment_overrides(self):
        with mock.patch.dict(
            self.remctl.os.environ,
            {
                "REMCTL_BRIDGE_PATH": "/tmp/custom-bridge",
                "REMCTL_PRIVATE_PATH": "/tmp/custom-private",
                "REMCTL_PERMISSIONS_PATH": "/tmp/custom-permissions",
            },
            clear=False,
        ):
            self.assertEqual(self.remctl.current_bridge_path(), Path("/tmp/custom-bridge"))
            self.assertEqual(self.remctl.current_private_path(), Path("/tmp/custom-private"))
            self.assertEqual(self.remctl.current_permissions_path(), Path("/tmp/custom-permissions"))

    def _store_db(self, path, *, reminders=0, active=0, objects=0, lists=1, filler=False):
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE ZREMCDREMINDER (
                Z_PK INTEGER PRIMARY KEY,
                ZMARKEDFORDELETION INTEGER DEFAULT 0,
                ZCOMPLETED INTEGER DEFAULT 0
            );
            CREATE TABLE ZREMCDBASELIST (
                Z_PK INTEGER PRIMARY KEY,
                ZNAME TEXT,
                ZMARKEDFORDELETION INTEGER DEFAULT 0
            );
            CREATE TABLE ZREMCDOBJECT (
                Z_PK INTEGER PRIMARY KEY,
                ZMARKEDFORDELETION INTEGER DEFAULT 0,
                ZMODIFIEDDATE REAL
            );
        """)
        for i in range(lists):
            db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME) VALUES (?, ?)", (i + 1, f"List {i + 1}"))
        for i in range(reminders):
            completed = 0 if i < active else 1
            db.execute("INSERT INTO ZREMCDREMINDER (Z_PK, ZCOMPLETED) VALUES (?, ?)", (i + 1, completed))
        for i in range(objects):
            db.execute("INSERT INTO ZREMCDOBJECT (Z_PK, ZMODIFIEDDATE) VALUES (?, ?)", (i + 1, 100 + i))
        if filler:
            db.execute("CREATE TABLE FILLER (body TEXT)")
            db.execute("INSERT INTO FILLER VALUES (?)", ("x" * 20000,))
        db.commit()
        db.close()

    def test_find_main_db_path_prefers_content_over_main_file_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Path(tmpdir)
            inactive = store / "Data-inactive.sqlite"
            active = store / "Data-active.sqlite"
            self._store_db(inactive, reminders=0, objects=0, filler=True)
            self._store_db(active, reminders=2, active=1, objects=2)
            with (
                mock.patch.object(self.remctl, "STORE_DIR", store),
                mock.patch.object(self.remctl, "reminders_store_access_error", return_value=None),
            ):
                self.assertEqual(self.remctl.find_main_db_path(), active)

    def test_find_main_db_path_uses_sidecar_size_as_tie_breaker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Path(tmpdir)
            first = store / "Data-first.sqlite"
            second = store / "Data-second.sqlite"
            self._store_db(first, reminders=0, objects=0)
            self._store_db(second, reminders=0, objects=0)
            Path(f"{second}-wal").write_bytes(b"x" * 4096)
            with (
                mock.patch.object(self.remctl, "STORE_DIR", store),
                mock.patch.object(self.remctl, "reminders_store_access_error", return_value=None),
            ):
                self.assertEqual(self.remctl.find_main_db_path(), second)

    def test_store_access_error_names_host_only_inside_sealed_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Path(tmpdir)
            with (
                mock.patch.object(self.remctl, "STORE_DIR", store),
                mock.patch.object(self.remctl.os, "access", return_value=False),
                mock.patch.dict(
                    "os.environ",
                    {"REMCTL_CAPABILITY_HOST_ACTIVE": "1"},
                    clear=False,
                ),
            ):
                hosted = self.remctl.reminders_store_access_error()

            with (
                mock.patch.object(self.remctl, "STORE_DIR", store),
                mock.patch.object(self.remctl.os, "access", return_value=False),
                mock.patch.dict(
                    "os.environ",
                    {
                        "REMCTL_CAPABILITY_HOST": "direct",
                        "REMCTL_CAPABILITY_HOST_ACTIVE": "",
                    },
                    clear=False,
                ),
            ):
                direct = self.remctl.reminders_store_access_error()

        self.assertIn("signed RemCTL Capability Host", hosted)
        self.assertIn("Grant Full Disk Access only", hosted)
        self.assertNotIn("app or interpreter", hosted)
        self.assertIn("Direct access", direct)
        self.assertIn("blocked for this caller", direct)
        self.assertIn("Normal automatic mode uses the signed RemCTL Capability Host", direct)
        self.assertIn("remctl onboard", direct)
        self.assertIn("remctl doctor --for-agent", direct)
        for forbidden in ("Grant Full Disk Access", "Hermes", "Python", "Terminal"):
            self.assertNotIn(forbidden, direct)

    def test_eventkit_access_guidance_names_host_only_inside_sealed_child(self):
        with mock.patch.dict(
            "os.environ",
            {"REMCTL_CAPABILITY_HOST_ACTIVE": "1"},
            clear=False,
        ):
            hosted = self.remctl.eventkit_access_fix_text()

        with (
            mock.patch.dict(
                "os.environ",
                {"REMCTL_CAPABILITY_HOST_ACTIVE": ""},
                clear=False,
            ),
            mock.patch.object(
                self.remctl,
                "doctor_execution_context",
                return_value={
                    "effective_context": "Terminal",
                    "host_app": "Terminal.app",
                },
            ),
        ):
            direct = self.remctl.eventkit_access_fix_text()

        self.assertIn("signed RemCTL Capability Host", hosted)
        self.assertIn("request Reminders access for that host", hosted)
        self.assertNotIn("same context", hosted)
        self.assertIn("caller-scoped and self-managed", direct)
        self.assertNotIn("same context", direct)

    def test_open_db_rejects_unexpected_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "Data-bad.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE ZREMCDOBJECT (Z_PK INTEGER PRIMARY KEY)")
            db.commit()
            db.close()
            with mock.patch.object(self.remctl, "find_main_db", return_value=path):
                with self.assertRaises(self.remctl.RemindersDBUnavailable):
                    self.remctl.open_db()

    def _list_db(self, names, grocery_locales=None):
        grocery_locales = grocery_locales or {}
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE ZREMCDBASELIST ("
            "Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER, Z_ENT INTEGER, "
            "ZBADGEEMBLEM TEXT, ZCOLOR BLOB, "
            "ZISGROUP INTEGER, ZPARENTLIST INTEGER, ZPARENTLIST1 INTEGER, "
            "Z_FOK_PARENTLIST INTEGER, Z_FOK_PARENTLIST1 INTEGER, ZDADISPLAYORDER INTEGER, "
            "ZISPINNEDBYCURRENTUSER INTEGER, ZPINNEDDATE REAL, "
            "ZSHOULDCATEGORIZEGROCERYITEMS INTEGER, ZSHOULDAUTOCATEGORIZEITEMS INTEGER, "
            "ZSHOULDSUGGESTCONVERSIONTOGROCERYLIST INTEGER, ZGROCERYLOCALEID TEXT, "
            "ZAUTOCATEGORIZATIONLOCALCORRECTIONSCHECKSUM TEXT, ZAUTOCATEGORIZATIONLOCALCORRECTIONSASDATA BLOB, "
            "ZCACHEDGROCERYITEMSCOUNT INTEGER, "
            "ZMEMBERSHIPSOFREMINDERSINPREDEFINEDGROCERYSECTIONSCHECKSUM TEXT, "
            "ZMEMBERSHIPSOFREMINDERSINPREDEFINEDGROCERYSECTIONSASDATA BLOB)"
        )
        for idx, name in enumerate(names, start=1):
            grocery_locale = grocery_locales.get(name)
            db.execute(
                "INSERT INTO ZREMCDBASELIST "
                "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT, "
                "ZBADGEEMBLEM, ZCOLOR, "
                "ZISPINNEDBYCURRENTUSER, ZPINNEDDATE, ZSHOULDCATEGORIZEGROCERYITEMS, "
                "ZSHOULDAUTOCATEGORIZEITEMS, ZSHOULDSUGGESTCONVERSIONTOGROCERYLIST, ZGROCERYLOCALEID) "
                "VALUES (?, ?, ?, 0, 3, NULL, NULL, 0, NULL, ?, 0, 0, ?)",
                (idx, name, f"CK-{idx}", 1 if grocery_locale else 0, grocery_locale),
            )
        db.execute(
            "CREATE TABLE ZREMCDBASESECTION ("
            "Z_PK INTEGER PRIMARY KEY, ZDISPLAYNAME TEXT, ZLIST INTEGER, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER)"
        )
        return db

    def _list_group_db(self):
        db = self._list_db(["Writing", "Editorial", "iOS and iPadOS 27 Review", "Work"])
        db.execute("UPDATE ZREMCDBASELIST SET ZISGROUP = 1 WHERE ZNAME = 'Writing'")
        db.execute(
            "UPDATE ZREMCDBASELIST SET ZPARENTLIST = 1, Z_FOK_PARENTLIST = 2048 "
            "WHERE ZNAME = 'Editorial'"
        )
        db.execute(
            "UPDATE ZREMCDBASELIST SET ZPARENTLIST = 1, Z_FOK_PARENTLIST = 3072 "
            "WHERE ZNAME = 'iOS and iPadOS 27 Review'"
        )
        return db

    def _add_group_reminders(self, db):
        db.execute(
            "CREATE TABLE ZREMCDREMINDER ("
            "Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZCOMPLETED INTEGER, "
            "ZMARKEDFORDELETION INTEGER DEFAULT 0, ZACCOUNT INTEGER, "
            "ZPARENTREMINDER INTEGER, ZLIST INTEGER)"
        )
        db.executemany(
            "INSERT INTO ZREMCDREMINDER "
            "(Z_PK, ZTITLE, ZCOMPLETED, ZMARKEDFORDELETION, ZACCOUNT, ZPARENTREMINDER, ZLIST) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "Editorial Active", 0, 0, 1, None, 2),
                (2, "Editorial Completed", 1, 0, 1, None, 2),
                (3, "Review Active", 0, 0, 1, None, 3),
                (4, "Deleted", 0, 1, 1, None, 2),
                (5, "Subtask", 0, 0, 1, 1, 2),
                (6, "No Account", 0, 0, None, None, 3),
            ],
        )
        return db

    def _template_db(self):
        db = self._list_db(["Source"])
        db.execute(
            "CREATE TABLE ZREMCDTEMPLATE ("
            "Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER, "
            "ZCREATIONDATE REAL, ZLASTMODIFIEDDATE REAL, ZPUBLICLINKCREATIONDATE REAL, "
            "ZPUBLICLINKEXPIRATIONDATE REAL, ZPUBLICLINKLASTMODIFIEDDATE REAL, "
            "ZBADGEEMBLEM TEXT, ZCOLOR BLOB, ZPUBLICLINKURLUUID BLOB, ZPUBLICLINKCONFIGURATIONDATA BLOB)"
        )
        db.execute(
            "CREATE TABLE ZREMCDSAVEDREMINDER ("
            "Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZCKIDENTIFIER TEXT, ZPARENTSAVEDREMINDERIDENTIFIER BLOB, "
            "ZPRIORITY INTEGER, ZDISPLAYDATEISALLDAY INTEGER, ZDISPLAYDATEDATE REAL, ZCREATIONDATE REAL, "
            "ZMETADATA BLOB, ZMARKEDFORDELETION INTEGER, ZTEMPLATE INTEGER)"
        )
        db.execute("ALTER TABLE ZREMCDBASESECTION ADD COLUMN Z_ENT INTEGER")
        db.execute("ALTER TABLE ZREMCDBASESECTION ADD COLUMN ZCANONICALNAME TEXT")
        db.execute("ALTER TABLE ZREMCDBASESECTION ADD COLUMN ZTEMPLATE INTEGER")
        db.execute("ALTER TABLE ZREMCDBASESECTION ADD COLUMN ZCREATIONDATE REAL")
        db.execute(
            "INSERT INTO ZREMCDTEMPLATE "
            "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, ZCREATIONDATE, ZLASTMODIFIEDDATE, "
            "ZPUBLICLINKCREATIONDATE, ZPUBLICLINKLASTMODIFIEDDATE, ZBADGEEMBLEM, ZPUBLICLINKURLUUID, ZPUBLICLINKCONFIGURATIONDATA) "
            "VALUES (1, 'Rome: Things To See', 'TEMPLATE-1', 0, 100, 200, 300, 400, 'star', ?, X'0102')",
            (uuid.UUID("3A6B9DE5-80A4-4180-8AFC-1D261121E344").bytes,),
        )
        metadata = {
            "title": "Colosseum",
            "flagged": 1,
            "priority": 1,
            "hashtags": [{"name": "rome"}],
            "recurrenceRules": [{"frequency": 1, "interval": 1}],
        }
        db.execute(
            "INSERT INTO ZREMCDSAVEDREMINDER "
            "(Z_PK, ZTITLE, ZCKIDENTIFIER, ZPRIORITY, ZDISPLAYDATEISALLDAY, ZCREATIONDATE, ZMETADATA, ZMARKEDFORDELETION, ZTEMPLATE) "
            "VALUES (10, 'Colosseum', 'ITEM-1', 1, 0, 150, ?, 0, 1)",
            (b"\x01" + json.dumps(metadata).encode("utf-8"),),
        )
        db.execute(
            "INSERT INTO ZREMCDBASESECTION "
            "(Z_PK, Z_ENT, ZDISPLAYNAME, ZCANONICALNAME, ZTEMPLATE, ZCKIDENTIFIER, ZMARKEDFORDELETION, ZCREATIONDATE) "
            "VALUES (20, 8, 'Ancient Rome', 'Ancient Rome', 1, 'SECTION-1', 0, 125)"
        )
        return db

    def test_list_resolution_matches_single_normalized_name(self):
        db = self._list_db(["🗓️ Weekly 513", "Work"])
        try:
            result = self.remctl.resolve_list_ref(db, name="Weekly 513")

            self.assertEqual(result["id"], 1)
            self.assertEqual(result["title"], "🗓️ Weekly 513")
            self.assertEqual(result["method"], "normalized")
        finally:
            db.close()

    def test_list_resolution_rejects_ambiguous_normalized_name(self):
        db = self._list_db(["🗓️ Weekly 513", "Weekly-513"])
        try:
            result = self.remctl.resolve_list_ref(db, name="Weekly 513")

            self.assertEqual(result["error"], "ambiguous")
            self.assertEqual(sorted(candidate["title"] for candidate in result["candidates"]), ["Weekly-513", "🗓️ Weekly 513"])
        finally:
            db.close()

    def test_list_resolution_by_id_includes_object_uuid(self):
        db = self._list_db(["Projects"])
        try:
            result = self.remctl.resolve_list_ref(db, list_id=1)

            self.assertEqual(result["id"], 1)
            self.assertEqual(result["title"], "Projects")
            self.assertEqual(result["objectUUID"], "CK-1")
            self.assertEqual(result["method"], "id")
            self.assertFalse(result["isGroceries"])
        finally:
            db.close()

    def test_list_resolution_rejects_group_targets_by_default(self):
        db = self._list_group_db()
        try:
            result = self.remctl.resolve_list_ref(db, name="Writing")

            self.assertEqual(result["error"], "group")
            self.assertEqual(result["group"]["title"], "Writing")
            self.assertEqual(
                [child["title"] for child in result["group"]["children"]],
                ["iOS and iPadOS 27 Review", "Editorial"],
            )
            self.assertNotIn("Writing", [row["ZNAME"] for row in self.remctl.q_lists(db)])
        finally:
            db.close()

    def test_list_resolution_allows_group_targets_when_requested(self):
        db = self._list_group_db()
        try:
            result = self.remctl.resolve_list_ref(db, name="Writing", allow_groups=True)

            self.assertTrue(result["isGroup"])
            self.assertEqual(result["listType"], "group")
            self.assertEqual([child["id"] for child in result["children"]], [3, 2])
        finally:
            db.close()

    def test_group_resolution_requires_group_rows(self):
        db = self._list_group_db()
        try:
            result = self.remctl.resolve_group_ref(db, name="Writing")
            not_group = self.remctl.resolve_group_ref(db, group_id=4)

            self.assertTrue(result["isGroup"])
            self.assertEqual(result["title"], "Writing")
            self.assertEqual([child["title"] for child in result["children"]], ["iOS and iPadOS 27 Review", "Editorial"])
            self.assertEqual(not_group["error"], "not_group")
            self.assertEqual(not_group["list"]["title"], "Work")
        finally:
            db.close()

    def test_list_and_group_resolution_can_disambiguate_same_visible_name(self):
        db = self._list_db(["Shared", "Shared"])
        db.execute("UPDATE ZREMCDBASELIST SET ZISGROUP = 1 WHERE Z_PK = 1")
        try:
            list_result = self.remctl.resolve_list_ref(db, name="Shared")
            group_result = self.remctl.resolve_group_ref(db, name="Shared")

            self.assertEqual(list_result["id"], 2)
            self.assertFalse(list_result["isGroup"])
            self.assertEqual(group_result["id"], 1)
            self.assertTrue(group_result["isGroup"])
        finally:
            db.close()

    def test_group_resolution_rejects_duplicate_group_names(self):
        db = self._list_db(["Shared", "Shared"])
        db.execute("UPDATE ZREMCDBASELIST SET ZISGROUP = 1")
        try:
            result = self.remctl.resolve_group_ref(db, name="Shared")

            self.assertEqual(result["error"], "ambiguous")
            self.assertEqual([candidate["id"] for candidate in result["candidates"]], [1, 2])
        finally:
            db.close()

    def test_list_to_dict_includes_private_appearance_fields(self):
        row = {
            "Z_PK": 1,
            "ZNAME": "Projects",
            "ZCKIDENTIFIER": "CK-1",
            "ZBADGEEMBLEM": "{\"Emoji\" : \"\\ud83d\\udccc\"}",
            "ZCOLOR": b"not-a-color",
            "ZSHOULDCATEGORIZEGROCERYITEMS": 0,
            "ZCACHEDGROCERYITEMSCOUNT": 3,
            "ZMEMBERSHIPSOFREMINDERSINPREDEFINEDGROCERYSECTIONSASDATA_LENGTH": 128,
            "ZMEMBERSHIPSOFREMINDERSINPREDEFINEDGROCERYSECTIONSCHECKSUM": "checksum",
        }

        payload = self.remctl.list_to_dict(row)

        self.assertEqual(payload["badge"]["emoji"], "\U0001f4cc")
        self.assertEqual(payload["color"]["hex"], "#007AFF")
        self.assertEqual(payload["grocery"]["cachedItemsCount"], 3)
        self.assertEqual(payload["grocery"]["predefinedSectionsMembershipLength"], 128)
        self.assertEqual(payload["grocery"]["predefinedSectionsChecksum"], "checksum")

    def test_lists_json_reports_grocery_metadata(self):
        db = self._list_db(["Groceries", "Work"], grocery_locales={"Groceries": "en_US"})
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(SimpleNamespace(json=True))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        groceries = next(item for item in payload if item["title"] == "Groceries")
        work = next(item for item in payload if item["title"] == "Work")
        self.assertEqual(groceries["listType"], "groceries")
        self.assertTrue(groceries["isGroceries"])
        self.assertEqual(groceries["grocery"]["locale"], "en_US")
        self.assertTrue(groceries["grocery"]["shouldCategorizeItems"])
        self.assertFalse(groceries["grocery"]["shouldAutoCategorizeItems"])
        self.assertEqual(work["listType"], "standard")
        self.assertFalse(work["isGroceries"])
        self.assertNotIn("grocery", work)

    def test_lists_json_reports_group_hierarchy(self):
        db = self._list_group_db()
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(SimpleNamespace(json=True))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        group = next(item for item in payload if item["title"] == "Writing")
        child = next(item for item in payload if item["title"] == "Editorial")
        work = next(item for item in payload if item["title"] == "Work")
        self.assertEqual(group["listType"], "group")
        self.assertTrue(group["isGroup"])
        self.assertEqual([item["title"] for item in group["children"]], ["iOS and iPadOS 27 Review", "Editorial"])
        self.assertEqual(child["parentListId"], 1)
        self.assertEqual(child["group"]["title"], "Writing")
        self.assertNotIn("group", work)

    def test_groups_json_reports_child_and_group_counts(self):
        db = self._add_group_reminders(self._list_group_db())
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_groups(SimpleNamespace(json=True, format=None))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        group = payload[0]
        self.assertEqual(group["title"], "Writing")
        self.assertEqual(group["counts"], {"active": 2, "completed": 1, "total": 3})
        counts_by_title = {child["title"]: child["counts"] for child in group["children"]}
        self.assertEqual(counts_by_title["Editorial"], {"active": 1, "completed": 1, "total": 2})
        self.assertEqual(counts_by_title["iOS and iPadOS 27 Review"], {"active": 1, "completed": 0, "total": 1})

    def test_groups_table_includes_counts(self):
        db = self._add_group_reminders(self._list_group_db())
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_groups(SimpleNamespace(json=False, format="table"))
        finally:
            db.close()

        output = stdout.getvalue()
        self.assertIn("Active", output)
        self.assertIn("Done", output)
        self.assertIn("Writing", output)
        self.assertIn("Editorial", output)
        self.assertIn("Total", output)

    def test_group_info_reports_counts_and_commands(self):
        db = self._add_group_reminders(self._list_group_db())
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_group_info(SimpleNamespace(name="Writing", group_id=None, json=True))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["counts"], {"active": 2, "completed": 1, "total": 3})
        self.assertEqual([child["title"] for child in payload["children"]], ["iOS and iPadOS 27 Review", "Editorial"])
        self.assertIn("remctl show Writing --format table", payload["suggestedCommands"]["showTable"])
        self.assertIn("remctl list-create LIST --private --group Writing", payload["suggestedCommands"]["createList"])

    def test_lists_human_output_marks_groceries_with_carrot(self):
        db = self._list_db(["Groceries"], grocery_locales={"Groceries": "en_US"})
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(SimpleNamespace(json=False, format=None))
        finally:
            db.close()

        output = stdout.getvalue()
        self.assertIn("Groceries", output)
        self.assertIn("🥕", output)

    def test_group_create_uses_private_helper_and_can_add_lists(self):
        db = self._list_group_db()
        args = SimpleNamespace(
            name="New Group",
            add_list=["Editorial"],
            add_list_id=[4],
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_create_group_call", return_value={"status": "created", "id": "GROUP-CK"}) as create_group,
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_group_create(args)
        finally:
            db.close()

        create_group.assert_called_once_with({"action": "create_group", "name": "New Group"}, "New Group")
        self.assertEqual(
            [call.args[0] for call in private_call.call_args_list],
            [
                {"action": "set_list_parent_group", "listId": "CK-4", "groupId": "GROUP-CK"},
                {"action": "set_list_parent_group", "listId": "CK-2", "groupId": "GROUP-CK"},
            ],
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "created")
        self.assertEqual([item["title"] for item in payload["addedLists"]], ["Work", "Editorial"])

    def test_list_create_can_target_group(self):
        db = self._list_group_db()
        args = SimpleNamespace(
            name="Research",
            color=None,
            private=True,
            symbol=None,
            emoji=None,
            groceries=False,
            standard=False,
            grocery_locale=None,
            group="Writing",
            group_id=None,
            json=True,
        )

        def create_list_side_effect(request, name):
            db.execute(
                "INSERT INTO ZREMCDBASELIST "
                "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT) "
                "VALUES (5, ?, 'CK-5', 0, 3)",
                (name,),
            )
            return {"status": "created", "id": "CK-5"}

        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_create_list_call", side_effect=create_list_side_effect) as create_list,
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
                mock.patch.object(self.remctl.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_list_create(args)
        finally:
            db.close()

        create_list.assert_called_once_with({"action": "create_list", "name": "Research"}, "Research")
        private_call.assert_called_once_with({"action": "set_list_parent_group", "listId": "CK-5", "groupId": "CK-1"})
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["id"], 5)
        self.assertEqual(payload["objectUUID"], "CK-5")
        self.assertEqual(payload["group"]["title"], "Writing")
        self.assertEqual(payload["membership"], {"status": "updated"})

    def test_group_edit_renames_and_adds_or_removes_lists(self):
        db = self._list_group_db()
        args = SimpleNamespace(
            name="Writing",
            group_id=None,
            new_name="Drafts",
            add_list=["Work"],
            add_list_id=None,
            remove_list=["Editorial"],
            remove_list_id=None,
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_group_edit(args)
        finally:
            db.close()

        self.assertEqual(
            [call.args[0] for call in private_call.call_args_list],
            [
                {"action": "set_list_appearance", "listId": "CK-1", "name": "Drafts"},
                {"action": "set_list_parent_group", "listId": "CK-4", "groupId": "CK-1"},
                {"action": "set_list_parent_group", "listId": "CK-2"},
            ],
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["renamed"]["oldName"], "Writing")
        self.assertEqual(payload["renamed"]["newName"], "Drafts")
        self.assertEqual(payload["addedLists"][0]["title"], "Work")
        self.assertEqual(payload["removedLists"][0]["title"], "Editorial")

    def test_group_edit_can_move_list_before_sibling(self):
        db = self._list_group_db()
        args = SimpleNamespace(
            name="Writing",
            group_id=None,
            new_name=None,
            add_list=None,
            add_list_id=None,
            remove_list=None,
            remove_list_id=None,
            move_list="Work",
            move_list_id=None,
            before_list="Editorial",
            before_list_id=None,
            after_list=None,
            after_list_id=None,
            first=False,
            last=False,
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_group_edit(args)
        finally:
            db.close()

        self.assertEqual(
            [call.args[0] for call in private_call.call_args_list],
            [
                {"action": "set_list_parent_group", "listId": "CK-3"},
                {"action": "set_list_parent_group", "listId": "CK-2"},
                {"action": "set_list_parent_group", "listId": "CK-2", "groupId": "CK-1"},
                {"action": "set_list_parent_group", "listId": "CK-4", "groupId": "CK-1"},
                {"action": "set_list_parent_group", "listId": "CK-3", "groupId": "CK-1"},
            ],
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["movedList"]["title"], "Work")
        self.assertEqual(payload["movedList"]["position"], "before")
        self.assertEqual(payload["movedList"]["relativeTo"]["title"], "Editorial")
        self.assertEqual([item["title"] for item in payload["movedList"]["finalOrder"]], ["iOS and iPadOS 27 Review", "Work", "Editorial"])
        self.assertEqual(payload["movedList"]["private"]["method"], "detach_reattach")

    def test_group_edit_can_move_list_last(self):
        db = self._list_group_db()
        args = SimpleNamespace(
            name="Writing",
            group_id=None,
            new_name=None,
            add_list=None,
            add_list_id=None,
            remove_list=None,
            remove_list_id=None,
            move_list="iOS and iPadOS 27 Review",
            move_list_id=None,
            before_list=None,
            before_list_id=None,
            after_list=None,
            after_list_id=None,
            first=False,
            last=True,
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_group_edit(args)
        finally:
            db.close()

        self.assertEqual(
            [call.args[0] for call in private_call.call_args_list],
            [
                {"action": "set_list_parent_group", "listId": "CK-3"},
                {"action": "set_list_parent_group", "listId": "CK-2"},
                {"action": "set_list_parent_group", "listId": "CK-3", "groupId": "CK-1"},
                {"action": "set_list_parent_group", "listId": "CK-2", "groupId": "CK-1"},
            ],
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["movedList"]["position"], "last")
        self.assertEqual(payload["movedList"]["relativeTo"]["placement"], "after")
        self.assertEqual([item["title"] for item in payload["movedList"]["finalOrder"]], ["Editorial", "iOS and iPadOS 27 Review"])

    def test_group_edit_rejects_removing_list_that_is_not_a_child(self):
        db = self._list_group_db()
        args = SimpleNamespace(
            name="Writing",
            group_id=None,
            new_name=None,
            add_list=None,
            add_list_id=None,
            remove_list=["Work"],
            remove_list_id=None,
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call") as private_call,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaises(SystemExit):
                    self.remctl.cmd_group_edit(args)
        finally:
            db.close()

        private_call.assert_not_called()
        self.assertIn("is not currently in group", stderr.getvalue())

    def test_group_delete_detaches_child_lists_before_deleting_group(self):
        db = self._list_group_db()
        args = SimpleNamespace(name="Writing", group_id=None, private=True, force=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    side_effect=[
                        {"status": "updated"},
                        {"status": "updated"},
                        {"status": "deleted"},
                    ],
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_group_delete(args)
        finally:
            db.close()

        self.assertEqual(
            [call.args[0] for call in private_call.call_args_list],
            [
                {"action": "set_list_parent_group", "listId": "CK-3"},
                {"action": "set_list_parent_group", "listId": "CK-2"},
                {"action": "delete_group", "groupId": "CK-1"},
            ],
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "deleted")
        self.assertEqual([item["title"] for item in payload["detachedLists"]], ["iOS and iPadOS 27 Review", "Editorial"])

    def _show_row(self, pk, title, ckid):
        return {
            "Z_PK": pk,
            "ZTITLE": title,
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATE": None,
            "ZDISPLAYDATEDATE": None,
            "ZALLDAY": None,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": ckid,
            "list_name": "Groceries",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
            "ZDUEDATEDELTAALERTSDATA": None,
        }

    def test_show_requests_persisted_manual_order_for_list_reads(self):
        db = object()
        args = SimpleNamespace(
            list="Projects",
            list_id=None,
            completed=False,
            json=True,
            format=None,
            verbose=False,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 7, "title": "Projects"},
            ),
            mock.patch.object(self.remctl, "q_reminders", return_value=[]) as q_reminders,
            mock.patch.object(self.remctl, "preload_extras", return_value=({}, {})),
            mock.patch.object(self.remctl, "preload_attachments", return_value={}),
            mock.patch.object(self.remctl, "preload_indicators", return_value={}),
            mock.patch.object(self.remctl, "q_sections", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_show(args)

        q_reminders.assert_called_once_with(
            db,
            list_pk=7,
            completed=False,
            top_level=True,
            manual_order=True,
        )
        self.assertEqual(json.loads(stdout.getvalue()), [])

    def test_show_group_requests_persisted_manual_order_for_each_child(self):
        db = object()
        children = [
            {"Z_PK": 7, "ZNAME": "Editorial"},
            {"Z_PK": 8, "ZNAME": "Projects"},
        ]
        args = SimpleNamespace(completed=False, json=True, format=None, verbose=False)
        with (
            mock.patch.object(self.remctl, "q_child_lists", return_value=children),
            mock.patch.object(
                self.remctl,
                "q_reminders_for_lists",
                return_value=[],
            ) as q_reminders_for_lists,
            mock.patch.object(self.remctl, "preload_extras", return_value=({}, {})),
            mock.patch.object(self.remctl, "preload_attachments", return_value={}),
            mock.patch.object(self.remctl, "preload_indicators", return_value={}),
            mock.patch.object(self.remctl, "q_sections", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_show_group(
                args,
                db,
                {"id": 1, "title": "Writing", "isGroup": True},
            )

        q_reminders_for_lists.assert_called_once_with(
            db,
            [7, 8],
            completed=False,
            top_level=True,
            manual_order=True,
        )
        self.assertEqual(json.loads(stdout.getvalue()), [])

    def test_show_human_output_marks_grocery_sections_with_matching_emoji(self):
        rows = [
            self._show_row(1, "Milk", "REM-1"),
            self._show_row(2, "Trash bags", "REM-2"),
        ]

        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 1, "title": "Groceries", "isGroceries": True},
            ),
            mock.patch.object(self.remctl, "q_reminders", return_value=rows),
            mock.patch.object(
                self.remctl,
                "q_sections",
                return_value=[
                    {"ZDISPLAYNAME": "Dairy, Eggs &amp; Cheese"},
                    {"ZDISPLAYNAME": "Household Items"},
                ],
            ),
            mock.patch.object(
                self.remctl,
                "q_section_memberships",
                return_value={
                    "REM-1": "Dairy, Eggs &amp; Cheese",
                    "REM-2": "Household Items",
                },
            ),
            mock.patch.object(
                self.remctl,
                "preload_extras",
                return_value=({1: 0, 2: 0}, {1: [], 2: []}),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_show(
                SimpleNamespace(
                    list="Groceries",
                    list_id=None,
                    completed=False,
                    json=False,
                    format=None,
                    verbose=False,
                )
            )

        output = stdout.getvalue()
        self.assertIn("[🥛 Dairy, Eggs & Cheese]", output)
        self.assertIn("[🧻 Household Items]", output)

    def test_show_json_includes_grocery_section_emoji(self):
        rows = [self._show_row(1, "Milk", "REM-1")]

        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 1, "title": "Groceries", "isGroceries": True},
            ),
            mock.patch.object(self.remctl, "q_reminders", return_value=rows),
            mock.patch.object(
                self.remctl,
                "q_sections",
                return_value=[{"ZDISPLAYNAME": "Dairy, Eggs & Cheese"}],
            ),
            mock.patch.object(
                self.remctl,
                "q_section_memberships",
                return_value={"REM-1": "Dairy, Eggs & Cheese"},
            ),
            mock.patch.object(self.remctl, "q_rich_link", return_value=None),
            mock.patch.object(
                self.remctl,
                "preload_extras",
                return_value=({1: 0}, {1: []}),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_show(
                SimpleNamespace(
                    list="Groceries",
                    list_id=None,
                    completed=False,
                    json=True,
                    format=None,
                    verbose=False,
                )
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["section"], "Dairy, Eggs & Cheese")
        self.assertEqual(payload[0]["sectionEmoji"], "🥛")

    def test_show_via_eventkit_json_uses_limited_non_chainable_ids(self):
        bridge_payload = {
            "status": "ok",
            "items": [
                {
                    "id": 42,
                    "eventKitId": "EK-1",
                    "title": "Review issue",
                    "list": "Work",
                    "completed": False,
                    "priority": "none",
                    "section": "Research",
                    "tags": ["remctl"],
                }
            ],
        }
        args = SimpleNamespace(
            list="Work",
            list_id=None,
            completed=False,
            via_eventkit=True,
            json=True,
            format=None,
            verbose=False,
        )
        with (
            mock.patch.object(self.remctl, "open_db") as open_db,
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", return_value=bridge_payload) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_show(args)

        open_db.assert_not_called()
        bridge_call.assert_called_once_with({
            "action": "read",
            "readMode": "show",
            "limit": 500,
            "list": "Work",
            "completed": False,
        })
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["source"], "eventkit")
        self.assertEqual(payload["fidelity"], "limited")
        self.assertIn("cannot be passed", payload["idWarning"])
        self.assertEqual(payload["items"][0]["eventKitId"], "EK-1")
        self.assertNotIn("id", payload["items"][0])
        self.assertNotIn("section", payload["items"][0])
        self.assertNotIn("tags", payload["items"][0])

    def test_show_via_eventkit_rejects_numeric_list_id_before_bridge(self):
        args = SimpleNamespace(
            list=None,
            list_id=153,
            completed=False,
            via_eventkit=True,
            json=True,
            format=None,
            verbose=False,
        )
        with (
            mock.patch.object(self.remctl, "open_db") as open_db,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.cmd_show(args)

        self.assertEqual(raised.exception.code, 2)
        open_db.assert_not_called()
        bridge_call.assert_not_called()
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["code"], "eventkit_read_unsupported")
        self.assertIn("numeric list ids", payload["message"])

    def test_today_via_eventkit_rejects_table_output_before_bridge(self):
        args = SimpleNamespace(
            no_overdue=False,
            via_eventkit=True,
            json=False,
            format="table",
            verbose=False,
        )
        with (
            mock.patch.object(self.remctl, "open_db") as open_db,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.cmd_today(args)

        self.assertEqual(raised.exception.code, 2)
        open_db.assert_not_called()
        bridge_call.assert_not_called()
        self.assertIn("does not support table output", stderr.getvalue())

    def test_templates_json_reports_counts_and_existing_public_link(self):
        db = self._template_db()
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_templates(SimpleNamespace(json=True))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["name"], "Rome: Things To See")
        self.assertEqual(payload[0]["itemCount"], 1)
        self.assertEqual(payload[0]["sectionCount"], 1)
        self.assertEqual(payload[0]["publicLink"]["uuid"], "3A6B9DE5-80A4-4180-8AFC-1D261121E344")
        self.assertEqual(
            payload[0]["publicLink"]["url"],
            "https://www.icloud.com/reminders/template/3A6B9DE5-80A4-4180-8AFC-1D261121E344#Rome:_Things_To_See",
        )

    def test_template_info_json_includes_saved_items_and_sections(self):
        db = self._template_db()
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_template_info(SimpleNamespace(name="Rome: Things To See", template_id=None, json=True))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["sections"][0]["name"], "Ancient Rome")
        self.assertEqual(payload["items"][0]["title"], "Colosseum")
        self.assertTrue(payload["items"][0]["flagged"])
        self.assertEqual(payload["items"][0]["priority"], "high")
        self.assertEqual(payload["items"][0]["tags"], ["rome"])

    def test_template_create_requires_private_before_helper(self):
        args = SimpleNamespace(
            name="Packing Template",
            from_list="Source",
            from_list_id=None,
            include_completed=False,
            private=False,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "private_available") as private_available,
            mock.patch.object(self.remctl, "private_call") as private_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_template_create(args)

        private_available.assert_not_called()
        private_call.assert_not_called()
        self.assertIn("--private", stderr.getvalue())

    def test_template_create_uses_private_helper(self):
        db = self._template_db()
        args = SimpleNamespace(
            name="Packing Template",
            from_list="Source",
            from_list_id=None,
            include_completed=True,
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "created", "id": "TEMPLATE-2"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_template_create(args)
        finally:
            db.close()

        self.assertEqual(
            private_call.call_args.args[0],
            {
                "action": "create_template",
                "name": "Packing Template",
                "listId": "CK-1",
                "includeCompleted": True,
            },
        )

    def test_template_apply_uses_private_helper(self):
        db = self._template_db()
        args = SimpleNamespace(name="Rome: Things To See", template_id=None, private=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "created", "id": "LIST-2"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_template_apply(args)
        finally:
            db.close()

        self.assertEqual(
            private_call.call_args.args[0],
            {
                "action": "apply_template",
                "templateId": "TEMPLATE-1",
            },
        )

    def test_template_delete_uses_private_helper(self):
        db = self._template_db()
        args = SimpleNamespace(name=None, template_id=1, private=True, force=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "deleted"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_template_delete(args)
        finally:
            db.close()

        self.assertEqual(
            private_call.call_args.args[0],
            {
                "action": "delete_template",
                "templateId": "TEMPLATE-1",
            },
        )

    def test_list_symbols_reports_official_reminders_emblems(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.remctl.cmd_list_symbols(SimpleNamespace(json=True))

        payload = json.loads(stdout.getvalue())
        names = [symbol["name"] for symbol in payload["symbols"]]
        self.assertEqual(payload["count"], 71)
        self.assertIn("approximate Unicode text fallback", payload["note"])
        self.assertIn("education3", names)
        self.assertIn("fitness", names)

    def test_list_symbols_tui_labels_approximate_preview_column(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.remctl.cmd_list_symbols(SimpleNamespace(json=False, html=None, preview=False))

        output = stdout.getvalue()
        self.assertIn("approximate text fallback", output)
        self.assertIn("remctl list-symbols --preview", output)
        self.assertIn("approx", output)
        self.assertIn("education3", output)

    def test_capability_host_command_partition_covers_real_parser(self):
        _parser, subparsers = self.remctl.build_parser()
        parser_commands = set(subparsers.choices)
        self.assertFalse(remctl_runtime.LOCAL_COMMANDS & remctl_runtime.HOSTED_COMMANDS)
        self.assertEqual(
            parser_commands,
            remctl_runtime.LOCAL_COMMANDS | remctl_runtime.HOSTED_COMMANDS,
        )
        self.assertTrue({"deleted", "restore"} <= remctl_runtime.HOSTED_COMMANDS)

    def test_internal_host_tty_state_requires_active_marker(self):
        non_tty = SimpleNamespace(isatty=lambda: False)
        with (
            mock.patch.object(self.remctl.sys, "stdout", non_tty),
            mock.patch.dict(
                "os.environ",
                {"REMCTL_CAPABILITY_STDOUT_TTY": "1"},
                clear=False,
            ),
        ):
            self.assertFalse(self.remctl.stdout_is_interactive())

        with (
            mock.patch.object(self.remctl.sys, "stdout", non_tty),
            mock.patch.dict(
                "os.environ",
                {
                    "REMCTL_CAPABILITY_HOST_ACTIVE": "1",
                    "REMCTL_CAPABILITY_STDOUT_TTY": "1",
                },
                clear=False,
            ),
        ):
            self.assertTrue(self.remctl.stdout_is_interactive())

    def test_internal_host_columns_require_active_marker_and_bounds(self):
        with (
            mock.patch.dict(
                "os.environ",
                {"REMCTL_CAPABILITY_COLUMNS": "144"},
                clear=False,
            ),
            mock.patch.object(self.remctl.os, "get_terminal_size", side_effect=OSError),
        ):
            self.assertEqual(self.remctl.terminal_columns(80), 80)

        with mock.patch.dict(
            "os.environ",
            {
                "REMCTL_CAPABILITY_HOST_ACTIVE": "1",
                "REMCTL_CAPABILITY_COLUMNS": "144",
            },
            clear=False,
        ):
            self.assertEqual(self.remctl.terminal_columns(80), 144)

        with (
            mock.patch.dict(
                "os.environ",
                {
                    "REMCTL_CAPABILITY_HOST_ACTIVE": "1",
                    "REMCTL_CAPABILITY_COLUMNS": "1001",
                },
                clear=False,
            ),
            mock.patch.object(self.remctl.os, "get_terminal_size", side_effect=OSError),
        ):
            self.assertEqual(self.remctl.terminal_columns(80), 80)

    def test_main_routes_once_then_onboards_flushes_and_dispatches_once(self):
        events = []
        stdout = SimpleNamespace(
            isatty=lambda: True,
            flush=lambda: events.append("stdout_flush"),
        )
        stderr = SimpleNamespace(flush=lambda: events.append("stderr_flush"))

        def route(_args):
            events.append("route")
            return True

        def onboard(_args):
            events.append("onboard")
            return {"ok": True}

        def hosted_dispatch(*_args, **_kwargs):
            events.append("dispatch")
            return 0

        with (
            mock.patch.object(sys, "argv", ["remctl", "lists"]),
            mock.patch.dict("os.environ", {"NO_COLOR": ""}, clear=False),
            mock.patch.object(self.remctl.sys, "stdout", stdout),
            mock.patch.object(self.remctl.sys, "stderr", stderr),
            mock.patch.object(self.remctl.remctl_broker, "should_route", side_effect=route) as should_route,
            mock.patch.object(self.remctl.remctl_broker, "dispatch", side_effect=hosted_dispatch) as dispatch,
            mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding", side_effect=onboard) as onboarding,
            mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
        ):
            self.remctl.main()

        self.assertEqual(events, ["route", "onboard", "stdout_flush", "stderr_flush", "dispatch"])
        should_route.assert_called_once()
        onboarding.assert_called_once()
        local_handler.assert_not_called()
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0], ["lists"])
        self.assertEqual(dispatch.call_args.kwargs["parsed_args"].cmd, "lists")

    def test_main_onboards_before_one_direct_handler_without_host_dispatch(self):
        events = []
        stdout = SimpleNamespace(
            isatty=lambda: True,
            flush=lambda: events.append("stdout_flush"),
        )
        stderr = SimpleNamespace(flush=lambda: events.append("stderr_flush"))

        def route(_args):
            events.append("route")
            return False

        def onboard(_args):
            events.append("onboard")
            return {"ok": False}

        def direct_handler(_args, handler):
            events.append("handler")
            self.assertIs(handler, self.remctl.cmd_lists)

        with (
            mock.patch.object(sys, "argv", ["remctl", "lists"]),
            mock.patch.dict("os.environ", {"NO_COLOR": ""}, clear=False),
            mock.patch.object(self.remctl.sys, "stdout", stdout),
            mock.patch.object(self.remctl.sys, "stderr", stderr),
            mock.patch.object(self.remctl.remctl_broker, "should_route", side_effect=route) as should_route,
            mock.patch.object(self.remctl.remctl_broker, "dispatch") as dispatch,
            mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding", side_effect=onboard) as onboarding,
            mock.patch.object(self.remctl, "run_handler_with_fallback", side_effect=direct_handler) as local_handler,
        ):
            self.remctl.main()

        self.assertEqual(events, ["route", "onboard", "stdout_flush", "stderr_flush", "handler"])
        should_route.assert_called_once()
        onboarding.assert_called_once()
        dispatch.assert_not_called()
        local_handler.assert_called_once()

    def test_main_makes_sanitized_host_presentation_defaults_explicit(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "show", "Work"]),
            mock.patch.dict(
                "os.environ",
                {
                    "NO_COLOR": "1",
                    "REMCTL_IMAGES": "1",
                    "REMCTL_IMAGE_MODE": "kitty",
                    "REMCTL_IMAGE_WIDTH": "72",
                },
                clear=False,
            ),
            mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=True),
            mock.patch.object(self.remctl.remctl_broker, "dispatch", return_value=0) as dispatch,
        ):
            self.remctl.main()

        self.assertEqual(
            dispatch.call_args.args[0],
            [
                "--no-color",
                "--images",
                "--image-mode",
                "kitty",
                "--image-width",
                "72",
                "show",
                "Work",
            ],
        )

    def test_host_normalization_ignores_literal_no_color_after_terminator(self):
        args = SimpleNamespace(
            no_color=True,
            images=False,
            image_mode=None,
            image_width=None,
        )
        self.assertEqual(
            self.remctl._normalized_host_argv(
                ["show", "--", "--no-color"],
                args,
            ),
            ["--no-color", "show", "--", "--no-color"],
        )

    def test_host_normalization_ignores_literal_image_options_after_terminator(self):
        args = SimpleNamespace(
            no_color=False,
            images=True,
            image_mode="kitty",
            image_width=72,
        )
        with mock.patch.dict("os.environ", {"NO_COLOR": ""}, clear=False):
            normalized = self.remctl._normalized_host_argv(
                ["show", "--", "--images", "--image-mode=ascii", "--image-width"],
                args,
            )
        self.assertEqual(
            normalized,
            [
                "--images",
                "--image-mode",
                "kitty",
                "--image-width",
                "72",
                "show",
                "--",
                "--images",
                "--image-mode=ascii",
                "--image-width",
            ],
        )

    def test_main_keeps_local_command_out_of_capability_host(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "list-symbols", "--json"]),
            mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=False) as should_route,
            mock.patch.object(self.remctl.remctl_broker, "dispatch") as dispatch,
            mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding") as onboard,
            mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
        ):
            self.remctl.main()

        should_route.assert_called_once()
        self.assertEqual(should_route.call_args.args[0].cmd, "list-symbols")
        dispatch.assert_not_called()
        onboard.assert_called_once()
        local_handler.assert_called_once()
        self.assertIs(local_handler.call_args.args[1], self.remctl.cmd_list_symbols)

    def test_no_command_keeps_splash_local_and_hosts_normalized_today(self):
        color_enabled = self.remctl.C.enabled
        try:
            with (
                mock.patch.object(sys, "argv", ["remctl", "--image-width", "64"]),
                mock.patch.dict("os.environ", {"NO_COLOR": ""}, clear=False),
                mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=True),
                mock.patch.object(self.remctl.remctl_broker, "dispatch", return_value=0) as dispatch,
                mock.patch.object(self.remctl, "show_splash") as splash,
                mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding") as onboard,
                mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
            ):
                self.remctl.main()
        finally:
            self.remctl.C.enabled = color_enabled

        splash.assert_called_once_with()
        onboard.assert_called_once()
        local_handler.assert_not_called()
        dispatch.assert_called_once()
        self.assertEqual(
            dispatch.call_args.args[0],
            ["today", "--format", "plain", "--image-width", "64"],
        )

    def test_splash_names_signed_host_and_repair_commands(self):
        self.remctl.C.enabled = False
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.remctl.show_splash()

        output = stdout.getvalue()
        self.assertIn("signed RemCTL Capability Host", output)
        self.assertIn("First setup: remctl onboard", output)
        self.assertIn("Diagnose or repair: remctl doctor --for-agent", output)
        self.assertIn("Reopen FDA guide: remctl permissions full-disk-access", output)
        self.assertNotIn("Setup or repair: remctl onboard", output)

    def test_no_command_json_hosts_today_without_splash(self):
        color_enabled = self.remctl.C.enabled
        try:
            with (
                mock.patch.object(sys, "argv", ["remctl", "--format", "json"]),
                mock.patch.dict("os.environ", {"NO_COLOR": ""}, clear=False),
                mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=True),
                mock.patch.object(self.remctl.remctl_broker, "dispatch", return_value=0) as dispatch,
                mock.patch.object(self.remctl, "show_splash") as splash,
            ):
                self.remctl.main()
        finally:
            self.remctl.C.enabled = color_enabled

        splash.assert_not_called()
        self.assertEqual(dispatch.call_args.args[0], ["today", "--format", "json"])

    def test_existing_install_flushes_splash_before_hosted_dispatch(self):
        events = []

        class RecordingStdout:
            def isatty(self):
                return True

            def write(self, value):
                events.append("write")
                return len(value)

            def flush(self):
                events.append("flush")

        def hosted_dispatch(*_args, **_kwargs):
            events.append("dispatch")
            return 0

        with (
            mock.patch.object(sys, "argv", ["remctl"]),
            mock.patch.dict("os.environ", {"NO_COLOR": ""}, clear=False),
            mock.patch.object(self.remctl.sys, "stdout", RecordingStdout()),
            mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=True),
            mock.patch.object(
                self.remctl.remctl_broker,
                "dispatch",
                side_effect=hosted_dispatch,
            ) as dispatch,
            mock.patch.object(
                self.remctl,
                "maybe_run_first_launch_onboarding",
                return_value=None,
            ) as onboard,
        ):
            self.remctl.main()

        onboard.assert_called_once()
        dispatch.assert_called_once()
        self.assertEqual(events[-2:], ["flush", "dispatch"])

    def test_hosted_command_exit_code_propagates_without_direct_retry(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "delete", "42", "--force"]),
            mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=True),
            mock.patch.object(self.remctl.remctl_broker, "dispatch", return_value=7) as dispatch,
            mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 7)
        dispatch.assert_called_once()
        local_handler.assert_not_called()

    def test_host_indeterminate_error_never_retries_directly(self):
        error = self.remctl.remctl_broker.HostIndeterminate(
            "connection ended after dispatch",
            code="broker_command_timeout",
        )
        with (
            mock.patch.object(sys, "argv", ["remctl", "add", "Possibly created"]),
            mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=True),
            mock.patch.object(self.remctl.remctl_broker, "dispatch", side_effect=error),
            mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 1)
        local_handler.assert_not_called()
        output = stderr.getvalue()
        self.assertIn("broker_command_timeout", output)
        self.assertIn("may have run", output)
        self.assertIn("did not retry", output)

    def test_installed_host_unhealthy_error_fails_closed(self):
        error = self.remctl.remctl_broker.HostUnhealthy(
            "installed host has an unsafe socket"
        )
        with (
            mock.patch.object(sys, "argv", ["remctl", "lists", "--json"]),
            mock.patch.object(self.remctl.remctl_broker, "should_route", return_value=True),
            mock.patch.object(self.remctl.remctl_broker, "dispatch", side_effect=error),
            mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 1)
        local_handler.assert_not_called()
        self.assertIn("capability_host_unhealthy", stderr.getvalue())

    def test_invalid_host_mode_is_a_clean_cli_error(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "lists"]),
            mock.patch.object(
                self.remctl.remctl_broker,
                "should_route",
                side_effect=ValueError(
                    "invalid REMCTL_CAPABILITY_HOST value 'sometimes'"
                ),
            ),
            mock.patch.object(self.remctl.remctl_broker, "dispatch") as dispatch,
            mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding") as onboard,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 2)
        dispatch.assert_not_called()
        onboard.assert_not_called()
        self.assertIn("REMCTL_CAPABILITY_HOST", stderr.getvalue())

    def test_force_host_with_custom_store_fails_before_execution(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "lists", "--json"]),
            mock.patch.dict(
                "os.environ",
                {
                    "REMCTL_CAPABILITY_HOST": "force",
                    "REMCTL_CAPABILITY_HOST_ACTIVE": "",
                    "REMCTL_STORE_DIR": "/does/not/exist",
                },
                clear=False,
            ),
            mock.patch.object(
                self.remctl.remctl_broker,
                "should_route",
                side_effect=type(self)._real_should_route,
            ),
            mock.patch.object(self.remctl.remctl_broker, "dispatch") as dispatch,
            mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 2)
        dispatch.assert_not_called()
        local_handler.assert_not_called()
        self.assertIn("conflicts with REMCTL_STORE_DIR", stderr.getvalue())

    def test_custom_store_runs_direct_in_auto_and_direct_modes(self):
        for mode in ("auto", "direct"):
            with self.subTest(mode=mode):
                with (
                    mock.patch.object(sys, "argv", ["remctl", "lists", "--json"]),
                    mock.patch.dict(
                        "os.environ",
                        {
                            "REMCTL_CAPABILITY_HOST": mode,
                            "REMCTL_CAPABILITY_HOST_ACTIVE": "",
                            "REMCTL_STORE_DIR": "/tmp/custom-reminders-store",
                        },
                        clear=False,
                    ),
                    mock.patch.object(
                        self.remctl.remctl_broker,
                        "should_route",
                        side_effect=type(self)._real_should_route,
                    ),
                    mock.patch.object(self.remctl.remctl_broker, "dispatch") as dispatch,
                    mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding"),
                    mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
                ):
                    self.remctl.main()

                dispatch.assert_not_called()
                local_handler.assert_called_once()
                self.assertIs(local_handler.call_args.args[1], self.remctl.cmd_lists)

    def test_active_host_child_runs_direct_despite_force_and_custom_store(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "lists", "--json"]),
            mock.patch.dict(
                "os.environ",
                {
                    "REMCTL_CAPABILITY_HOST": "force",
                    "REMCTL_CAPABILITY_HOST_ACTIVE": "1",
                    "REMCTL_STORE_DIR": "/sealed/host/store",
                },
                clear=False,
            ),
            mock.patch.object(
                self.remctl.remctl_broker,
                "should_route",
                side_effect=type(self)._real_should_route,
            ),
            mock.patch.object(self.remctl.remctl_broker, "dispatch") as dispatch,
            mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding"),
            mock.patch.object(self.remctl, "run_handler_with_fallback") as local_handler,
        ):
            self.remctl.main()

        dispatch.assert_not_called()
        local_handler.assert_called_once()
        self.assertIs(local_handler.call_args.args[1], self.remctl.cmd_lists)

    def test_unknown_command_error_is_readable_and_suggests_list_symbols(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "symbols"]),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 2)
        output = stderr.getvalue()
        self.assertIn("unknown command: 'symbols'", output)
        self.assertIn("Did you mean: remctl list-symbols", output)
        self.assertIn("Available commands:", output)
        self.assertIn("  add,", output)
        self.assertNotIn("choose from", output)

    def test_read_command_accepts_format_after_subcommand(self):
        captured = {}

        def capture(args, handler):
            captured["args"] = args
            captured["handler"] = handler

        color_enabled = self.remctl.C.enabled
        try:
            with (
                mock.patch.object(sys, "argv", ["remctl", "show", "Work", "--format", "table"]),
                mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding"),
                mock.patch.object(self.remctl, "run_handler_with_fallback", side_effect=capture),
            ):
                self.remctl.main()
        finally:
            self.remctl.C.enabled = color_enabled

        args = captured["args"]
        self.assertIs(captured["handler"], self.remctl.cmd_show)
        self.assertEqual(args.list, "Work")
        self.assertEqual(args.format, "table")
        self.assertFalse(args.json)

    def test_done_command_accepts_date_after_id(self):
        captured = {}

        def capture(args, handler):
            captured["args"] = args
            captured["handler"] = handler

        color_enabled = self.remctl.C.enabled
        try:
            with (
                mock.patch.object(sys, "argv", ["remctl", "done", "23880", "--date", "2026-05-27", "--json"]),
                mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding"),
                mock.patch.object(self.remctl, "run_handler_with_fallback", side_effect=capture),
            ):
                self.remctl.main()
        finally:
            self.remctl.C.enabled = color_enabled

        args = captured["args"]
        self.assertIs(captured["handler"], self.remctl.cmd_done)
        self.assertEqual(args.id, 23880)
        self.assertEqual(args.date, "2026-05-27")
        self.assertTrue(args.json)

    def test_read_command_local_format_json_enables_json_output(self):
        captured = {}

        def capture(args, handler):
            captured["args"] = args
            captured["handler"] = handler

        color_enabled = self.remctl.C.enabled
        try:
            with (
                mock.patch.object(sys, "argv", ["remctl", "today", "--format", "json"]),
                mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding"),
                mock.patch.object(self.remctl, "run_handler_with_fallback", side_effect=capture),
            ):
                self.remctl.main()
        finally:
            self.remctl.C.enabled = color_enabled

        args = captured["args"]
        self.assertIs(captured["handler"], self.remctl.cmd_today)
        self.assertEqual(args.format, "json")
        self.assertTrue(args.json)

    def test_read_command_parses_eventkit_fallback_flag(self):
        captured = {}

        def capture(args, handler):
            captured["args"] = args
            captured["handler"] = handler

        color_enabled = self.remctl.C.enabled
        try:
            with (
                mock.patch.object(sys, "argv", ["remctl", "today", "--via-eventkit", "--json"]),
                mock.patch.object(self.remctl, "maybe_run_first_launch_onboarding"),
                mock.patch.object(self.remctl, "run_handler_with_fallback", side_effect=capture),
            ):
                self.remctl.main()
        finally:
            self.remctl.C.enabled = color_enabled

        args = captured["args"]
        self.assertIs(captured["handler"], self.remctl.cmd_today)
        self.assertTrue(args.via_eventkit)
        self.assertTrue(args.json)

    def test_show_help_labels_eventkit_mode_as_limited_and_non_default(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "show", "--help"]),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 0)
        output = " ".join(stdout.getvalue().split())
        self.assertIn("--via-eventkit", output)
        self.assertIn("never the default", output)
        self.assertIn("no RemCTL numeric ids", output)
        self.assertIn("stored manual display order", output)

    def test_list_pin_help_names_custom_readback_and_builtin_capability_gate(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "list-pin", "--help"]),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 0)
        output = " ".join(stdout.getvalue().split())
        self.assertIn("custom smart-list state", output)
        self.assertIn("pinnedDate", output)
        self.assertIn("generic fetch capability", output)

    def test_list_symbols_html_contact_sheet_embeds_badge_assets(self):
        rows = [
            {"name": "education3", "asset": "ListBadgeEducation3", "preview": "✎"},
            {"name": "fitness", "asset": "ListBadgeFitness", "preview": "🏋"},
        ]

        html = self.remctl.build_list_symbols_html(rows, {"ListBadgeEducation3": "ZmFrZQ=="})

        self.assertIn("Official Reminders List Symbols", html)
        self.assertIn("data:image/png;base64,ZmFrZQ==", html)
        self.assertIn("Preview official colors", html)
        self.assertIn('data-color="#FF8D28"', html)
        self.assertIn("aria-pressed", html)
        self.assertIn("education3", html)
        self.assertIn("ListBadgeFitness", html)

    def test_list_create_uses_bridge_contract_fields(self):
        args = SimpleNamespace(name="Project X", color="blue", private=False, symbol=None, emoji=None, json=True)
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created"}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_list_create(args)
        self.assertEqual(
            bridge_call_result.call_args.args[0],
            {"action": "create_list", "title": "Project X", "color": "blue"},
        )

    def test_list_create_groceries_uses_private_helper(self):
        db = self._list_db(["Groceries"], grocery_locales={})
        args = SimpleNamespace(
            name="Groceries",
            color=None,
            private=True,
            symbol=None,
            emoji=None,
            groceries=True,
            standard=False,
            grocery_locale="it_IT",
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=0),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "created"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_list_create(args)
        finally:
            db.close()

        self.assertEqual(
            private_call.call_args.args[0],
            {
                "action": "create_list",
                "name": "Groceries",
                "shouldCategorizeGroceryItems": True,
                "groceryLocaleID": "it_IT",
            },
        )
        self.assertEqual(json.loads(stdout.getvalue())["private"]["status"], "created")

    def test_list_create_groceries_surfaces_private_helper_error(self):
        db = self._list_db(["Work"])
        args = SimpleNamespace(
            name="Groceries",
            color=None,
            private=True,
            symbol=None,
            emoji=None,
            groceries=True,
            standard=False,
            grocery_locale="en_US",
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=0),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "error", "message": "ReminderKit unavailable"}),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_list_create(args)
        finally:
            db.close()

        self.assertIn("ReminderKit unavailable", stderr.getvalue())

    def test_list_create_rejects_symbol_without_private_before_bridge(self):
        args = SimpleNamespace(name="Project X", color=None, private=False, symbol="education3", emoji=None, json=True)
        with (
            mock.patch.object(self.remctl, "bridge_available") as bridge_available,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_list_create(args)
        bridge_available.assert_not_called()

    def test_list_create_rejects_unsupported_symbol_before_bridge(self):
        args = SimpleNamespace(name="Project X", color=None, private=True, symbol="sparkles", emoji=None, json=True)
        with (
            mock.patch.object(self.remctl, "bridge_available") as bridge_available,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_list_create(args)
        bridge_available.assert_not_called()
        self.assertIn("unsupported list symbol", stderr.getvalue())
        self.assertIn("list-symbols", stderr.getvalue())

    def test_list_edit_private_targets_resolved_list_id(self):
        db = self._list_db(["Projects"])
        args = SimpleNamespace(
            name=None,
            list_id=1,
            new_name=None,
            color="#123456",
            private=True,
            symbol="education3",
            emoji=None,
            groceries=False,
            standard=False,
            grocery_locale=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_edit(args)
        finally:
            db.close()
        self.assertEqual(
            private_call.call_args.args[0],
            {
                "action": "set_list_appearance",
                "listId": "CK-1",
                "color": "#123456",
                "symbol": "education3",
            },
        )

    def test_list_edit_can_convert_groceries_and_standard(self):
        db = self._list_db(["Groceries"], grocery_locales={"Groceries": "en_US"})
        groceries_args = SimpleNamespace(
            name="Groceries",
            list_id=None,
            new_name=None,
            color=None,
            private=True,
            symbol=None,
            emoji=None,
            groceries=True,
            standard=False,
            grocery_locale=None,
            json=True,
        )
        standard_args = SimpleNamespace(
            name=None,
            list_id=1,
            new_name=None,
            color=None,
            private=True,
            symbol=None,
            emoji=None,
            groceries=False,
            standard=True,
            grocery_locale=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as groceries_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_edit(groceries_args)
            self.assertEqual(
                groceries_call.call_args.args[0],
                {
                    "shouldCategorizeGroceryItems": True,
                    "groceryLocaleID": "en_US",
                    "action": "set_list_appearance",
                    "listId": "CK-1",
                },
            )

            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as standard_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_edit(standard_args)
            self.assertEqual(
                standard_call.call_args.args[0],
                {
                    "shouldCategorizeGroceryItems": False,
                    "action": "set_list_appearance",
                    "listId": "CK-1",
                },
            )
        finally:
            db.close()

    def test_list_pin_and_unpin_use_private_helper(self):
        db = self._list_db(["📌 Project X"])
        pin_args = SimpleNamespace(name="Project X", list_id=None, smart_list_id=None, private=True, json=True)
        unpin_args = SimpleNamespace(name=None, list_id=1, smart_list_id=None, private=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated", "pinned": True}) as pin_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_pin(pin_args)
            self.assertEqual(
                pin_call.call_args.args[0],
                {"action": "set_list_pinned", "listId": "CK-1", "pinned": True},
            )

            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated", "pinned": False}) as unpin_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_unpin(unpin_args)
            self.assertEqual(
                unpin_call.call_args.args[0],
                {"action": "set_list_pinned", "listId": "CK-1", "pinned": False},
            )
        finally:
            db.close()

    def test_list_pin_rejects_without_private_before_helper(self):
        args = SimpleNamespace(name="Project X", list_id=None, smart_list_id=None, private=False, json=True)
        with (
            mock.patch.object(self.remctl, "private_available") as private_available,
            mock.patch.object(self.remctl, "private_call") as private_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_list_pin(args)

        private_available.assert_not_called()
        private_call.assert_not_called()
        self.assertIn("--private", stderr.getvalue())

    def test_add_help_exposes_urgent_creation_flag(self):
        with (
            mock.patch.object(sys, "argv", ["remctl", "add", "--help"]),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--urgent", stdout.getvalue())
        self.assertIn("--early-reminder", stdout.getvalue())

    def test_add_urgent_rejects_without_private_before_bridge(self):
        args = SimpleNamespace(
            title="Leave now",
            list="Work",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            urgent=True,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(args)

        bridge_call.assert_not_called()
        self.assertIn("--private", stderr.getvalue())

    def test_add_early_reminder_rejects_without_private_before_bridge(self):
        args = SimpleNamespace(
            title="Leave early",
            list="Work",
            list_id=None,
            notes=None,
            due="2026-05-18 14:00",
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            urgent=None,
            early_reminder="15m",
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(args)

        bridge_call.assert_not_called()
        self.assertIn("--private", stderr.getvalue())

    def test_add_early_reminder_requires_due_date_before_bridge(self):
        args = SimpleNamespace(
            title="Leave early",
            list="Work",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=True,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            urgent=None,
            early_reminder="15m",
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            mock.patch.object(self.remctl, "private_available", return_value=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(args)

        bridge_call.assert_not_called()
        self.assertIn("early_reminder_requires_due_date", stderr.getvalue())

    def test_add_relative_alarm_requires_due_date_before_bridge(self):
        # A relative alarm counts back from the due date; saved without one, it never fires.
        args = SimpleNamespace(
            title="Stretch",
            list=None,
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm="15m",
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            mock.patch.object(self.remctl, "bridge_call_result") as bridge_call_result,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(args)

        bridge_call.assert_not_called()
        bridge_call_result.assert_not_called()
        self.assertEqual(json.loads(stderr.getvalue())["code"], "relative_alarm_requires_due_date")
        self.assertTrue(self.remctl.alarm_is_relative(self.remctl.parse_alarm("1h")))
        self.assertFalse(self.remctl.alarm_is_relative(self.remctl.parse_alarm("2026-09-30 09:00")))
        self.assertFalse(self.remctl.alarm_is_relative(None))

    def test_add_grocery_rejects_without_private_before_bridge(self):
        args = SimpleNamespace(
            title="Milk",
            list="Groceries",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=True,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flagged=None,
            urgent=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(args)

        bridge_call.assert_not_called()
        self.assertIn("--private", stderr.getvalue())

    def test_add_grocery_reports_reminders_auto_categorization(self):
        db = self._list_db(["Groceries"], grocery_locales={"Groceries": "en_US"})
        args = SimpleNamespace(
            title="Milk",
            list="Groceries",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=True,
            private_metadata=False,
            grocery=True,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flagged=None,
            urgent=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        bridge_result = self._bridge_result({"status": "created", "id": "REM-1"})
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call") as private_call,
                mock.patch.object(self.remctl, "wait_for_grocery_section", return_value="Dairy, Eggs & Cheese"),
                mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_add(args)
        finally:
            db.close()

        private_call.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["private"][0]["source"], "reminders_auto")
        self.assertEqual(payload["private"][0]["verifiedSections"][0]["section"], "Dairy, Eggs & Cheese")

    def test_add_grocery_falls_back_to_private_categorizer_when_needed(self):
        db = self._list_db(["Groceries"], grocery_locales={"Groceries": "en_US"})
        args = SimpleNamespace(
            title="Milk",
            list="Groceries",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=True,
            private_metadata=False,
            grocery=True,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flagged=None,
            urgent=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        bridge_result = self._bridge_result({"status": "created", "id": "REM-1"})
        section_results = iter([None, "Dairy, Eggs & Cheese"])
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
                mock.patch.object(self.remctl, "wait_for_grocery_section", side_effect=lambda *_args, **_kwargs: next(section_results)),
                mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_add(args)
        finally:
            db.close()

        self.assertEqual(
            private_call.call_args.args[0],
            {
                "action": "categorize_grocery_items",
                "listId": "CK-1",
                "reminderIds": ["REM-1"],
            },
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["private"][0]["verifiedSections"][0]["section"], "Dairy, Eggs & Cheese")

    def test_add_grocery_treats_helper_error_as_success_when_section_verified(self):
        db = self._list_db(["Groceries"], grocery_locales={"Groceries": "en_US"})
        section_results = iter([None, "Dairy, Eggs & Cheese"])
        try:
            result = None
            with (
                mock.patch.object(self.remctl, "private_call", return_value={"status": "error", "message": "Couldn’t communicate with a helper application."}),
                mock.patch.object(self.remctl, "wait_for_grocery_section", side_effect=lambda *_args, **_kwargs: next(section_results)),
            ):
                result = self.remctl.apply_private_grocery_categorization(db, 1, ["REM-1"])
        finally:
            db.close()

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["source"], "reminders_auto")
        self.assertIn("helper application", result["warning"])
        self.assertEqual(result["verifiedSections"][0]["section"], "Dairy, Eggs & Cheese")

    def test_add_grocery_rejects_standard_target_list(self):
        db = self._list_db(["Work"])
        args = SimpleNamespace(
            title="Milk",
            list="Work",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=True,
            private_metadata=False,
            grocery=True,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flagged=None,
            urgent=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call") as bridge_call,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_add(args)
        finally:
            db.close()

        bridge_call.assert_not_called()
        self.assertIn("not a Groceries list", stderr.getvalue())

    def test_list_rename_and_delete_use_resolved_bridge_contract_fields(self):
        db = self._list_db(["Old"])
        rename_args = SimpleNamespace(name="Old", list_id=None, new_name="New", new_name_option=None, json=True)
        delete_args = SimpleNamespace(name=None, list_id=1, force=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call", return_value={"status": "renamed"}) as rename_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_rename(rename_args)
            self.assertEqual(
                rename_call.call_args.args[0],
                {
                    "action": "rename_list",
                    "title": "Old",
                    "newTitle": "New",
                    "list": "Old",
                    "listId": "CK-1",
                },
            )

            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call", return_value={"status": "deleted"}) as delete_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_delete(delete_args)
            self.assertEqual(
                delete_call.call_args.args[0],
                {
                    "action": "delete_list",
                    "title": "Old",
                    "list": "Old",
                    "listId": "CK-1",
                },
            )
        finally:
            db.close()

    def test_list_delete_does_not_fallback_to_applescript_after_bridge_failure(self):
        db = self._list_db(["Old"])
        args = SimpleNamespace(name=None, list_id=1, force=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "bridge_call",
                    return_value={"status": "error", "message": "iCloud Reminders list not found for id: CK-1"},
                ),
                mock.patch.object(self.remctl.subprocess, "run") as subprocess_run,
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.remctl.cmd_list_delete(args)
        finally:
            db.close()

        subprocess_run.assert_not_called()
        self.assertIn("iCloud Reminders list not found", stderr.getvalue())

    def _smart_list_db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE ZREMCDBASELIST ("
            "Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER, "
            "Z_ENT INTEGER, ZSMARTLISTTYPE TEXT, ZFILTERDATA BLOB, "
            "ZMINIMUMSUPPORTEDAPPVERSION INTEGER, ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION INTEGER, "
            "ZISPINNEDBYCURRENTUSER INTEGER, ZPINNEDDATE REAL)"
        )
        db.execute(
            "INSERT INTO ZREMCDBASELIST "
            "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT, ZSMARTLISTTYPE, ZFILTERDATA, "
            "ZMINIMUMSUPPORTEDAPPVERSION, ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION, ZISPINNEDBYCURRENTUSER, ZPINNEDDATE) "
            "VALUES (?, ?, ?, 0, 4, ?, ?, ?, ?, ?, ?)",
            (1, None, "BUILTIN-1", "com.apple.reminders.smartlist.flagged", None, 0, 0, 1, 123.0),
        )
        db.execute(
            "INSERT INTO ZREMCDBASELIST "
            "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT, ZSMARTLISTTYPE, ZFILTERDATA, "
            "ZMINIMUMSUPPORTEDAPPVERSION, ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION, ZISPINNEDBYCURRENTUSER, ZPINNEDDATE) "
            "VALUES (?, ?, ?, 0, 4, ?, ?, ?, ?, ?, ?)",
            (2, "High Priority", "CUSTOM-1", self.remctl.CUSTOM_SMART_LIST_TYPE, b'{"priorities":["high"]}', 20220430, 20220430, None, 456.0),
        )
        db.execute(
            "INSERT INTO ZREMCDBASELIST "
            "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT, ZSMARTLISTTYPE, ZFILTERDATA, "
            "ZMINIMUMSUPPORTEDAPPVERSION, ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION, ZISPINNEDBYCURRENTUSER, ZPINNEDDATE) "
            "VALUES (?, ?, ?, 0, 3, NULL, NULL, NULL, NULL, 0, NULL)",
            (10, "Reminders", "LIST-1"),
        )
        return db

    def test_smart_lists_json_decodes_builtin_and_custom_rows(self):
        db = self._smart_list_db()
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_smart_lists(SimpleNamespace(json=True))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["kind"], "built-in")
        self.assertEqual(payload[0]["name"], "Flagged")
        self.assertEqual(payload[0]["filterLength"], 0)
        self.assertTrue(payload[0]["pinned"])
        self.assertEqual(payload[0]["pinnedDate"], 123.0)
        self.assertEqual(payload[1]["kind"], "custom")
        self.assertTrue(payload[1]["pinned"])
        self.assertEqual(payload[1]["pinnedDate"], 456.0)
        self.assertEqual(payload[1]["minimumSupportedVersion"], 20220430)
        self.assertEqual(payload[1]["effectiveMinimumSupportedVersion"], 20220430)
        self.assertEqual(payload[1]["filter"]["kind"], "priority")
        self.assertEqual(payload[1]["filterJSON"], {"priorities": ["high"]})

    def test_list_pin_supports_smart_list_by_name_and_id(self):
        db = self._smart_list_db()
        pin_args = SimpleNamespace(name="Flagged", list_id=None, smart_list_id=None, private=True, json=True)
        unpin_args = SimpleNamespace(name=None, list_id=None, smart_list_id=2, private=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated", "pinned": True}) as pin_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_list_pin(pin_args)
            self.assertEqual(
                pin_call.call_args.args[0],
                {
                    "action": "set_smart_list_pinned",
                    "smartListId": "BUILTIN-1",
                    "isCustom": False,
                    "pinned": True,
                },
            )
            self.assertEqual(json.loads(stdout.getvalue())["kind"], "smart-list")

            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "updated", "pinned": False}) as unpin_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_list_unpin(unpin_args)
            self.assertEqual(
                unpin_call.call_args.args[0],
                {
                    "action": "set_smart_list_pinned",
                    "smartListId": "CUSTOM-1",
                    "isCustom": True,
                    "pinned": False,
                },
            )
        finally:
            db.close()

    def test_custom_smart_list_pin_and_unpin_use_custom_fetch_contract(self):
        db = self._smart_list_db()
        pin_args = SimpleNamespace(
            name="High Priority",
            list_id=None,
            smart_list_id=None,
            private=True,
            json=True,
        )
        unpin_args = SimpleNamespace(
            name=None,
            list_id=None,
            smart_list_id=2,
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    side_effect=[
                        {"status": "updated", "pinned": True},
                        {"status": "updated", "pinned": False},
                    ],
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_list_pin(pin_args)
                pin_payload = json.loads(stdout.getvalue())
                stdout.seek(0)
                stdout.truncate(0)
                self.remctl.cmd_list_unpin(unpin_args)
                unpin_payload = json.loads(stdout.getvalue())
        finally:
            db.close()

        self.assertEqual(
            [call.args[0] for call in private_call.call_args_list],
            [
                {
                    "action": "set_smart_list_pinned",
                    "smartListId": "CUSTOM-1",
                    "isCustom": True,
                    "pinned": True,
                },
                {
                    "action": "set_smart_list_pinned",
                    "smartListId": "CUSTOM-1",
                    "isCustom": True,
                    "pinned": False,
                },
            ],
        )
        self.assertEqual(pin_payload["status"], "pinned")
        self.assertEqual(pin_payload["kind"], "smart-list")
        self.assertEqual(pin_payload["private"]["pinned"], True)
        self.assertEqual(unpin_payload["status"], "unpinned")
        self.assertEqual(unpin_payload["private"]["pinned"], False)

    def test_custom_smart_list_pin_surfaces_helper_failure_without_success_output(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="High Priority",
            list_id=None,
            smart_list_id=None,
            private=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={
                        "status": "error",
                        "message": "Custom smart-list pinning is unsupported on this macOS version",
                    },
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_list_pin(args)
        finally:
            db.close()

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Custom smart-list pinning is unsupported", stderr.getvalue())

    def test_smart_list_create_rejects_without_private_before_helper(self):
        args = SimpleNamespace(name="Nope", private=False, flagged=True, priority=None, json=True)
        with (
            mock.patch.object(self.remctl, "private_available") as private_available,
            mock.patch.object(self.remctl, "private_call") as private_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_smart_list_create(args)

        private_available.assert_not_called()
        private_call.assert_not_called()
        self.assertIn("requires --private", stderr.getvalue())

    def test_smart_list_create_rejects_unsupported_filter_before_helper(self):
        db = self._smart_list_db()
        args = SimpleNamespace(name="Nope", private=True, flagged=False, priority="none", json=True)
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_call") as private_call,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        private_call.assert_not_called()
        self.assertIn("Unsupported smart list priority", stderr.getvalue())

    def test_smart_list_create_calls_private_helper_with_encoded_filter(self):
        db = self._smart_list_db()
        args = SimpleNamespace(name="Flagged Review", private=True, flagged=True, priority=None, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "created", "id": "SMART-1"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        payload = private_call.call_args.args[0]
        self.assertEqual(payload["action"], "create_smart_list")
        self.assertEqual(payload["name"], "Flagged Review")
        self.assertEqual(payload["filterData"], "eyJmbGFnZ2VkIjp0cnVlfQ==")
        self.assertEqual(json.loads(stdout.getvalue())["filter"]["kind"], "flagged")

    def test_smart_list_create_passes_appearance_to_private_helper(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="Due Before June 1",
            private=True,
            flagged=True,
            priority=None,
            color="red",
            symbol=None,
            emoji="📆",
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "created", "id": "SMART-1", "color": "red", "emoji": "📆"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        payload = private_call.call_args.args[0]
        self.assertEqual(payload["action"], "create_smart_list")
        self.assertEqual(payload["color"], "red")
        self.assertEqual(payload["emoji"], "📆")

    def test_smart_list_create_builds_official_multi_filter_payload(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="Any Reminders Today",
            private=True,
            match="any",
            flagged=False,
            priority="high,medium",
            tags=None,
            tag_match="all",
            any_tag=False,
            untagged=False,
            date="today",
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=["Reminders"],
            exclude_list=None,
            include_list_id=None,
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "created", "id": "SMART-2"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        payload = private_call.call_args.args[0]
        encoded = payload["filterData"]
        decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        self.assertEqual(
            decoded,
            {
                "operation": "or",
                "priorities": ["high", "medium"],
                "date": {"today": False},
                "lists": {"include": ["LIST-1"], "exclude": []},
            },
        )
        self.assertEqual(json.loads(stdout.getvalue())["filter"]["match"], "any")

    def test_smart_list_create_builds_materializing_selected_tag_payload(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="#remctl Today",
            private=True,
            match="all",
            flagged=False,
            priority=None,
            tags="remctl",
            tag_match="any",
            any_tag=False,
            untagged=False,
            date="today",
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=None,
            exclude_list=None,
            include_list_id=None,
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            color=None,
            symbol=None,
            emoji=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "created", "id": "SMART-TAG"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        decoded = json.loads(base64.b64decode(private_call.call_args.args[0]["filterData"]).decode("utf-8"))
        self.assertEqual(
            decoded,
            {
                "operation": "and",
                "hashtags": {"hashtags": {"operation": "or", "include": ["remctl"], "exclude": []}},
                "date": {"today": False},
            },
        )
        self.assertEqual(json.loads(stdout.getvalue())["filter"]["filters"][0]["tagMatch"], "any")

    def test_smart_list_create_resolves_include_list_id_to_object_uuid(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="List ID Filter",
            private=True,
            match="all",
            flagged=False,
            priority=None,
            tags=None,
            tag_match="all",
            any_tag=False,
            untagged=False,
            date=None,
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=None,
            exclude_list=None,
            include_list_id=[10],
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "created", "id": "SMART-3"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        decoded = json.loads(base64.b64decode(private_call.call_args.args[0]["filterData"]).decode("utf-8"))
        self.assertEqual(decoded, {"lists": {"include": ["LIST-1"], "exclude": []}})

    def test_smart_list_create_rejects_multiple_include_lists_before_helper(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="List Union Filter",
            private=True,
            match="all",
            flagged=False,
            priority=None,
            tags=None,
            tag_match="all",
            any_tag=False,
            untagged=False,
            date=None,
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=None,
            exclude_list=None,
            include_list_id=[10, 11],
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            json=True,
        )
        db.execute(
            "INSERT INTO ZREMCDBASELIST "
            "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT, ZSMARTLISTTYPE, ZFILTERDATA) "
            "VALUES (?, ?, ?, 0, 3, NULL, NULL)",
            (11, "Work", "LIST-2"),
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_call") as private_call,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        private_call.assert_not_called()
        self.assertIn("only materializes one included-list", stderr.getvalue())

    def test_smart_list_edit_replaces_filter_for_custom_match(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="High Priority",
            smart_list_id=None,
            private=True,
            match="all",
            flagged=False,
            priority=None,
            tags=None,
            tag_match="all",
            any_tag=False,
            untagged=False,
            date="today",
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=None,
            exclude_list=None,
            include_list_id=None,
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "updated", "id": "CUSTOM-1"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_smart_list_edit(args)
        finally:
            db.close()

        payload = private_call.call_args.args[0]
        self.assertEqual(payload["action"], "update_smart_list")
        self.assertEqual(payload["smartListId"], "CUSTOM-1")
        self.assertEqual(json.loads(base64.b64decode(payload["filterData"]).decode("utf-8")), {"date": {"today": False}})
        self.assertEqual(json.loads(stdout.getvalue())["status"], "updated")

    def test_smart_list_edit_can_update_appearance_without_filter(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="High Priority",
            smart_list_id=None,
            private=True,
            color="#123456",
            symbol=None,
            emoji="📆",
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "updated", "id": "CUSTOM-1", "color": "#123456", "emoji": "📆"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_smart_list_edit(args)
        finally:
            db.close()

        payload = private_call.call_args.args[0]
        self.assertEqual(
            payload,
            {
                "action": "update_smart_list",
                "smartListId": "CUSTOM-1",
                "color": "#123456",
                "emoji": "📆",
            },
        )
        self.assertNotIn("filter", json.loads(stdout.getvalue()))

    def test_smart_list_delete_rejects_without_private_before_helper(self):
        args = SimpleNamespace(name="High Priority", smart_list_id=None, private=False, force=True, json=True)
        with (
            mock.patch.object(self.remctl, "private_available") as private_available,
            mock.patch.object(self.remctl, "private_call") as private_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_smart_list_delete(args)

        private_available.assert_not_called()
        private_call.assert_not_called()
        self.assertIn("requires --private", stderr.getvalue())

    def test_smart_list_delete_calls_private_helper_for_custom_match(self):
        db = self._smart_list_db()
        args = SimpleNamespace(name="High Priority", smart_list_id=None, private=True, force=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "deleted", "id": "CUSTOM-1"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_smart_list_delete(args)
        finally:
            db.close()

        self.assertEqual(
            private_call.call_args.args[0],
            {"action": "delete_smart_list", "smartListId": "CUSTOM-1"},
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "deleted")
        self.assertEqual(payload["name"], "High Priority")

    def test_smart_list_delete_does_not_match_builtin_smart_lists(self):
        db = self._smart_list_db()
        args = SimpleNamespace(name="Flagged", smart_list_id=None, private=True, force=True, json=True)
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_call") as private_call,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_smart_list_delete(args)
        finally:
            db.close()

        private_call.assert_not_called()
        self.assertIn("custom smart list not found", stderr.getvalue())

    def test_import_accepts_due_date_from_exported_json(self):
        created_args = []

        def fake_cmd_add(args):
            created_args.append(args)
            print(json.dumps({"status": "created", "id": "UUID-1", "numericId": 41}))

        payload = [{"title": "Buy milk", "dueDate": "2026-05-01T12:00:00"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "import.json"
            input_path.write_text(json.dumps(payload))
            args = SimpleNamespace(file=str(input_path), json=False)
            with (
                mock.patch.object(self.remctl, "cmd_add", side_effect=fake_cmd_add),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.remctl.cmd_import(args)

        self.assertEqual(len(created_args), 1)
        self.assertEqual(created_args[0].due, "2026-05-01T12:00:00")

    def test_import_dash_reads_stdin_and_emits_one_json_result(self):
        def fake_cmd_add(args):
            print(json.dumps({"status": "created", "id": "UUID-1", "numericId": 41}))

        args = SimpleNamespace(file="-", json=True)
        with (
            mock.patch.object(sys, "stdin", io.StringIO('[{"title":"Piped"}]')),
            mock.patch.object(self.remctl, "cmd_add", side_effect=fake_cmd_add),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.cmd_import(args)

        self.assertEqual(stderr.getvalue(), "imported index=0 id=41\n")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["createdIds"], [41])
        self.assertEqual(stdout.getvalue().count("\n"), 1)

    def test_import_preflights_entire_document_before_any_write(self):
        payload = [{"title": "Would write"}, 42, {"title": ""}]
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "import.json"
            input_path.write_text(json.dumps(payload))
            args = SimpleNamespace(file=str(input_path), json=True)
            with (
                mock.patch.object(self.remctl, "cmd_add") as cmd_add,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit) as raised,
            ):
                self.remctl.cmd_import(args)

        self.assertEqual(raised.exception.code, 1)
        cmd_add.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["code"], "invalid_import")
        self.assertEqual(len(error["errors"]), 2)

    def test_import_dash_rejects_tty_without_reading(self):
        class TTYInput:
            def isatty(self):
                return True

            def read(self, *_args):
                raise AssertionError("TTY stdin must not be read")

        args = SimpleNamespace(file="-", json=True)
        with (
            mock.patch.object(sys, "stdin", TTYInput()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.cmd_import(args)

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("requires piped standard input", stderr.getvalue())

    def test_import_rejects_non_boolean_flagged_before_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "import.json"
            input_path.write_text(json.dumps([{"title": "No write", "flagged": "false"}]))
            args = SimpleNamespace(file=str(input_path), json=True)
            with (
                mock.patch.object(self.remctl, "cmd_add") as cmd_add,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_import(args)

        cmd_add.assert_not_called()
        self.assertEqual(json.loads(stderr.getvalue())["errors"][0]["message"], "flagged must be a boolean")

    def test_import_partial_failure_reports_progress_before_next_add_and_exits_nonzero(self):
        observed_before_second = []
        progress = io.StringIO()

        def fake_cmd_add(args):
            if args.title == "First":
                print(json.dumps({"status": "created", "numericId": 51}))
                return
            observed_before_second.append(progress.getvalue())
            print(json.dumps({"status": "error", "message": "failed"}), file=sys.stderr)
            raise SystemExit(1)

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "import.json"
            input_path.write_text(json.dumps([{"title": "First"}, {"title": "Second"}]))
            args = SimpleNamespace(file=str(input_path), json=True)
            with (
                mock.patch.object(self.remctl, "cmd_add", side_effect=fake_cmd_add),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(progress),
                self.assertRaises(SystemExit) as raised,
            ):
                self.remctl.cmd_import(args)

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(observed_before_second, ["imported index=0 id=51\n"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["created"], 1)
        self.assertEqual(payload["createdIds"], [51])
        self.assertEqual(len(payload["errors"]), 1)

    def test_cmd_add_rejects_private_only_options_without_private_before_writing(self):
        args = SimpleNamespace(
            title="Should not be created",
            list="Projects",
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            section="Research",
            new_section=None,
            subtask=None,
            image=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "open_db") as open_db,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_add(args)
        bridge_call.assert_not_called()
        open_db.assert_not_called()
        self.assertIn("require --private", stderr.getvalue())

    def test_cmd_add_rejects_unparseable_due_before_writing(self):
        args = SimpleNamespace(
            title="Should not be created",
            list="Projects",
            notes=None,
            due="today at 25:00",
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            section=None,
            new_section=None,
            subtask=None,
            image=None,
            json=False,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available") as bridge_available,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as raised:
                self.remctl.cmd_add(args)
        self.assertEqual(raised.exception.code, 2)
        bridge_available.assert_not_called()
        bridge_call.assert_not_called()
        self.assertIn("No reminder was created", stderr.getvalue())
        self.assertIn("today at 3pm", stderr.getvalue())

    def test_cmd_add_rejects_invalid_recurrence_priority_and_alarm_before_writing(self):
        base = dict(
            title="Should not be created",
            list="Projects",
            list_id=None,
            notes=None,
            due=None,
            flag=False,
            tags=None,
            url=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            urgent=None,
            early_reminder=None,
            json=True,
        )
        cases = [
            ({"priority": "urgent", "recurrence": None, "alarm": None}, "priority"),
            ({"priority": None, "recurrence": "fortnightly", "alarm": None}, "recurrence"),
            ({"priority": None, "recurrence": None, "alarm": "eventually"}, "alarm"),
        ]
        for extra, needle in cases:
            args = SimpleNamespace(**base, **extra)
            with (
                mock.patch.object(self.remctl, "bridge_available") as bridge_available,
                mock.patch.object(self.remctl, "bridge_call") as bridge_call,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_add(args)
            bridge_available.assert_not_called()
            bridge_call.assert_not_called()
            self.assertIn(needle, stderr.getvalue())

    def test_cmd_add_json_reports_structured_unparseable_due_error(self):
        args = SimpleNamespace(
            title="Should not be created",
            list="Projects",
            notes=None,
            due="nonsense date",
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            section=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as raised:
                self.remctl.cmd_add(args)
        self.assertEqual(raised.exception.code, 2)
        bridge_call.assert_not_called()
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["code"], "invalid_due_date")
        self.assertEqual(payload["field"], "due")
        self.assertEqual(payload["input"], "nonsense date")
        self.assertIn("examples", payload)

    def test_cmd_edit_rejects_tags_without_private_before_reading_database(self):
        args = SimpleNamespace(
            id=1,
            json=True,
            title=None,
            notes=None,
            priority=None,
            due=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            tags="remctl",
            section=None,
            new_section=None,
            subtask=None,
            image=None,
            flagged=None,
            urgent=None,
            location_title=None,
            latitude=None,
            longitude=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db") as open_db,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_edit(args)
        bridge_call.assert_not_called()
        open_db.assert_not_called()
        self.assertIn("editing synced tags requires --private", stderr.getvalue())

    def test_cmd_add_private_url_tags_and_flag_stay_out_of_bridge_payload(self):
        args = SimpleNamespace(
            title="Research",
            list="Projects",
            notes=None,
            due=None,
            priority=None,
            flag=True,
            tags="remctl,work",
            url="https://example.com",
            recurrence=None,
            alarm=None,
            private=True,
            private_metadata=False,
            section=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
        )
        fake_db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "open_db", return_value=fake_db),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={"id": 7, "title": "Projects", "requested": "Projects", "method": "exact"},
            ),
            mock.patch.object(
                self.remctl,
                "bridge_call",
                return_value={"status": "created", "id": "ABC-123"},
            ),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "ABC-123"}),
            ) as bridge_call_result,
            mock.patch.object(
                self.remctl,
                "apply_private_changes",
                return_value=[{"status": "updated", "action": "add_private_metadata"}],
            ) as apply_private_changes,
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_add(args)

        bridge_payload = bridge_call_result.call_args.args[0]
        self.assertEqual(bridge_payload["title"], "Research")
        self.assertNotIn("url", bridge_payload)
        self.assertNotIn("flagged", bridge_payload)
        apply_private_changes.assert_called_once_with(
            "ABC-123",
            args,
            db=fake_db,
            list_pk=7,
            partial_context=mock.ANY,
        )
        self.assertEqual(json.loads(stdout.getvalue())["private"][0]["status"], "updated")

    def test_cmd_add_json_includes_numeric_id_when_database_can_resolve_identifier(self):
        args = SimpleNamespace(
            title="Fast create",
            list="Projects",
            notes=None,
            due="today at 3pm",
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            section=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "UUID-1"}),
            ),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={"id": 7, "title": "Projects", "requested": "Projects", "method": "exact"},
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value={"Z_PK": 17839}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_add(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["numericId"], 17839)

    def test_cmd_add_uses_resolved_list_name_for_bridge(self):
        args = SimpleNamespace(
            title="Weekly",
            list="Weekly 513",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            section=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "UUID-1"}),
            ) as bridge_call_result,
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={
                    "id": 156,
                    "title": "🗓️ Weekly 513",
                    "objectUUID": "LIST-UUID-156",
                    "requested": "Weekly 513",
                    "method": "normalized",
                },
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_add(args)

        self.assertEqual(bridge_call_result.call_args.args[0]["list"], "🗓️ Weekly 513")
        self.assertEqual(bridge_call_result.call_args.args[0]["listId"], "LIST-UUID-156")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["resolvedList"]["title"], "🗓️ Weekly 513")
        self.assertEqual(payload["resolvedList"]["method"], "normalized")

    def test_cmd_add_surfaces_eventkit_access_error_without_applescript_fallback(self):
        args = SimpleNamespace(
            title="Tarea de prueba",
            list=None,
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
        )
        bridge_result = self._bridge_result(
            {
                "status": "error",
                "message": "EventKit access error: The operation couldn’t be completed. (Mach error 4099 - unknown error code)",
            },
            returncode=1,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(
                self.remctl,
                "doctor_execution_context",
                return_value={"effective_context": "Codex.app", "host_app": "Codex.app"},
            ),
            mock.patch.object(self.remctl, "osa") as osa,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(args)

        osa.assert_not_called()
        output = stderr.getvalue()
        self.assertIn("remctl-bridge failed while trying to create the reminder", output)
        self.assertIn("EventKit access error", output)
        self.assertIn("remctl onboard", output)
        self.assertIn("remctl doctor --for-agent", output)

    def test_subtask_spec_date_only_due_sets_all_day(self):
        specs = self.remctl.parse_subtask_specs(['{"title":"X","due":"2026-07-03"}'])
        self.assertEqual(specs[0]["due"], "2026-07-03T00:00:00")
        self.assertTrue(specs[0]["allDay"])

    def test_subtask_spec_timed_due_omits_all_day(self):
        specs = self.remctl.parse_subtask_specs(['{"title":"X","due":"2026-07-03 15:00"}'])
        self.assertEqual(specs[0]["due"], "2026-07-03T15:00:00")
        self.assertNotIn("allDay", specs[0])

    def test_bridge_update_subtask_passes_all_day_for_date_only_due(self):
        with mock.patch.object(self.remctl, "bridge_call", return_value={"status": "updated"}) as bridge_call:
            self.remctl.bridge_update_subtask(
                "SUB-1",
                {"title": "X", "due": "2026-07-03T00:00:00", "allDay": True},
            )
        payload = bridge_call.call_args.args[0]
        self.assertEqual(payload["due"], "2026-07-03T00:00:00")
        self.assertTrue(payload["allDay"])

    def test_bridge_update_subtask_timed_due_omits_all_day(self):
        with mock.patch.object(self.remctl, "bridge_call", return_value={"status": "updated"}) as bridge_call:
            self.remctl.bridge_update_subtask(
                "SUB-1",
                {"title": "X", "due": "2026-07-03T15:00:00"},
            )
        payload = bridge_call.call_args.args[0]
        self.assertEqual(payload["due"], "2026-07-03T15:00:00")
        self.assertNotIn("allDay", payload)

    def test_apply_private_changes_strips_all_day_from_subtask_payload(self):
        args = SimpleNamespace(
            private=True,
            private_metadata=False,
            tags=None,
            url=None,
            section=None,
            section_id=None,
            new_section=None,
            subtask=[json.dumps({"title": "Child", "due": "2026-07-03"})],
            image=None,
            flag=False,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
        )
        private_result = {
            "status": "updated",
            "action": "add_subtasks",
            "subtasks": [{"id": "CHILD-ID", "title": "Child"}],
        }
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "private_action", return_value=private_result) as private_action,
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "updated"}),
        ):
            self.remctl.apply_private_changes("PARENT-ID", args)

        private_payload = private_action.call_args.args[0]
        self.assertNotIn("allDay", private_payload["subtasks"][0])

    def test_list_create_timeout_fails_without_applescript_fallback(self):
        args = SimpleNamespace(name="Project X", color=None, private=False, symbol=None, emoji=None, json=True)
        bridge_result = self._bridge_result({"status": "timeout", "message": "timed out"}, returncode=-1)
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(self.remctl, "create_list_with_applescript") as applescript,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_list_create(args)

        applescript.assert_not_called()
        self.assertIn("remctl-bridge failed while trying to create the list", stderr.getvalue())

    def test_list_create_recovers_when_list_present_after_generic_bridge_error(self):
        args = SimpleNamespace(name="Project X", color=None, private=False, symbol=None, emoji=None, json=True)
        bridge_result = self._bridge_result({"status": "error", "message": "Save failed"}, returncode=1)
        db = self._list_db(["Project X"])
        try:
            with (
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=1),
                mock.patch.object(self.remctl, "create_list_with_applescript") as applescript,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_list_create(args)
        finally:
            db.close()

        applescript.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "created")

    def test_list_create_falls_back_to_applescript_when_list_absent_after_generic_bridge_error(self):
        args = SimpleNamespace(name="Project X", color=None, private=False, symbol=None, emoji=None, json=True)
        bridge_result = self._bridge_result({"status": "error", "message": "Save failed"}, returncode=1)
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=0),
            mock.patch.object(
                self.remctl,
                "create_list_with_applescript",
                return_value={"status": "created", "id": "LIST-1", "fallback": "applescript"},
            ) as applescript,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.remctl.cmd_list_create(args)

        applescript.assert_called_once_with("Project X")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "created")

    def test_cmd_add_recovers_when_reminder_present_after_generic_bridge_error(self):
        db = self._due_window_db()
        db.execute("DELETE FROM ZREMCDBASELIST")
        db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME) VALUES (1, 'Work')")
        self._insert_lookup_reminder(db, 99, "Dup test", list_pk=1, ckid="RECOVERED-ID")
        args = SimpleNamespace(
            title="Dup test",
            list="Work",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
        )
        bridge_result = self._bridge_result({"status": "error", "message": "Save failed"}, returncode=1)
        try:
            with (
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "resolve_list_or_die",
                    return_value={"id": 1, "title": "Work", "requested": "Work", "method": "exact"},
                ),
                mock.patch.object(self.remctl, "osa") as osa,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_add(args)
        finally:
            db.close()

        osa.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["id"], "RECOVERED-ID")
        self.assertEqual(payload["numericId"], 99)

    def test_cmd_add_falls_back_to_applescript_when_reminder_absent_after_generic_bridge_error(self):
        db = self._due_window_db()
        args = SimpleNamespace(
            title="Fresh task",
            list=None,
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
        )
        bridge_result = self._bridge_result({"status": "error", "message": "Save failed"}, returncode=1)
        try:
            with (
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "osa", return_value="NEW-ID") as osa,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.remctl.cmd_add(args)
        finally:
            db.close()

        osa.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "created")

    def test_cmd_add_flag_skips_bridge_flagged_and_uses_applescript(self):
        db = self._due_window_db()
        db.execute("DELETE FROM ZREMCDBASELIST")
        db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME) VALUES (1, 'Work')")
        self._insert_lookup_reminder(db, 42, "Flagged task", list_pk=1, ckid="FLAG-ID")
        args = SimpleNamespace(
            title="Flagged task",
            list="Work",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=True,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
        )
        try:
            with (
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "bridge_call_result",
                    return_value=self._bridge_result({"status": "created", "id": "FLAG-ID"}),
                ) as bridge_call_result,
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "resolve_list_or_die",
                    return_value={"id": 1, "title": "Work", "requested": "Work", "method": "exact"},
                ),
                mock.patch.object(self.remctl, "osa_set_flagged_result", return_value=(True, "")) as flag_try,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_add(args)
        finally:
            db.close()

        bridge_payload = bridge_call_result.call_args.args[0]
        self.assertNotIn("flagged", bridge_payload)
        flag_try.assert_called_once_with("FLAG-ID", flagged=True)

    def test_cmd_add_flag_applescript_failure_warns_without_failing(self):
        args = SimpleNamespace(
            title="Flagged task",
            list="Work",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=True,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            json=False,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "FLAG-ID"}),
            ),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={"id": 1, "title": "Work", "requested": "Work", "method": "exact"},
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            mock.patch.object(
                self.remctl,
                "osa_set_flagged_result",
                return_value=(False, "Not authorized to send Apple events to Reminders. (-1743)"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.cmd_add(args)

        self.assertIn("Created:", stdout.getvalue())
        self.assertIn("failed to set the flag via AppleScript", stderr.getvalue())
        self.assertIn("-1743", stderr.getvalue())

    def test_cmd_add_flag_applescript_failure_surfaces_warning_in_json(self):
        args = SimpleNamespace(
            title="Flagged task",
            list="Work",
            list_id=None,
            notes=None,
            due=None,
            priority=None,
            flag=True,
            tags=None,
            url=None,
            recurrence=None,
            alarm=None,
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100,
            proximity="arriving",
            address=None,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "FLAG-ID"}),
            ),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={"id": 1, "title": "Work", "requested": "Work", "method": "exact"},
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            mock.patch.object(
                self.remctl,
                "osa_set_flagged_result",
                return_value=(False, "Not authorized to send Apple events to Reminders. (-1743)"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.remctl.cmd_add(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "created")
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("flag_not_set", payload["warnings"][0])
        self.assertIn("-1743", payload["warnings"][0])

    def test_q_recent_reminder_by_title_returns_sqlite_row_without_get(self):
        db = self._due_window_db()
        db.execute("DELETE FROM ZREMCDBASELIST")
        db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME) VALUES (1, 'Work')")
        self._insert_lookup_reminder(db, 99, "Dup test", list_pk=1, ckid="RECOVERED-ID")
        try:
            row = self.remctl.q_recent_reminder_by_title(db, "Dup test", list_pk=1)
            self.assertIsInstance(row, sqlite3.Row)
            self.assertFalse(hasattr(row, "get"))
            self.assertEqual(row["ZCKIDENTIFIER"], "RECOVERED-ID")
            self.assertEqual(row["list_name"], "Work")
        finally:
            db.close()

    def test_subtask_accepts_json_metadata(self):
        specs = self.remctl.parse_subtask_specs([
            json.dumps({
                "title": "Export PNG",
                "notes": "Use final crop",
                "due": "2026-04-15 14:00",
                "url": "https://example.com/export",
                "tags": ["media", "remctl"],
                "priority": "high",
                "alarm": "15m",
                "flagged": True,
                "urgent": True,
                "locationTitle": "Apple Park",
                "latitude": 37.3349,
                "longitude": -122.0090,
                "radius": 200,
                "proximity": "arriving",
            })
        ])

        self.assertEqual(specs[0]["title"], "Export PNG")
        self.assertEqual(specs[0]["notes"], "Use final crop")
        self.assertEqual(specs[0]["due"], "2026-04-15T14:00:00")
        self.assertEqual(specs[0]["urls"], ["https://example.com/export"])
        self.assertEqual(specs[0]["tags"], ["media", "remctl"])
        self.assertEqual(specs[0]["priority"], "high")
        self.assertEqual(specs[0]["alarm"], "-15m")
        self.assertTrue(specs[0]["flagged"])
        self.assertTrue(specs[0]["urgent"])
        self.assertEqual(specs[0]["proximity"], 1)

    def test_apply_private_changes_updates_rich_subtask_public_fields(self):
        args = SimpleNamespace(
            private=True,
            private_metadata=False,
            tags=None,
            url=None,
            section=None,
            new_section=None,
            subtask=[
                json.dumps({
                    "title": "Child",
                    "notes": "Child notes",
                    "due": "2026-04-15 14:00",
                    "url": "https://example.com/child",
                    "tags": ["childtag"],
                })
            ],
            image=None,
            flag=False,
            flagged=None,
            urgent=None,
            location_title=None,
            latitude=None,
            longitude=None,
        )
        private_result = {
            "status": "updated",
            "action": "add_subtasks",
            "subtasks": [{"id": "CHILD-ID", "title": "Child"}],
        }
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "private_action",
                side_effect=[
                    private_result,
                    {"status": "updated", "action": "add_private_metadata"},
                ],
            ) as private_action,
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "updated"}) as bridge_call,
        ):
            result = self.remctl.apply_private_changes("PARENT-ID", args)

        private_payload = private_action.call_args_list[0].args[0]
        self.assertEqual(private_payload["action"], "add_subtasks")
        self.assertEqual(private_payload["subtasks"][0]["urls"], ["https://example.com/child"])
        self.assertEqual(private_payload["subtasks"][0]["tags"], ["childtag"])
        child_private_payload = private_action.call_args_list[1].args[0]
        self.assertEqual(child_private_payload, {
            "action": "add_private_metadata",
            "id": "CHILD-ID",
            "urls": ["https://example.com/child"],
            "tags": ["childtag"],
        })
        self.assertEqual(bridge_call.call_args.args[0], {
            "action": "update",
            "id": "CHILD-ID",
            "notes": "Child notes",
            "due": "2026-04-15T14:00:00",
        })
        self.assertEqual(result[0]["bridgeUpdates"][0]["id"], "CHILD-ID")
        self.assertEqual(result[0]["childPrivateUpdates"][0]["id"], "CHILD-ID")

    def test_apply_private_changes_sets_early_reminder_delta(self):
        args = SimpleNamespace(
            private=True,
            private_metadata=False,
            tags=None,
            url=None,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flag=False,
            flagged=None,
            urgent=None,
            early_reminder="15m",
            location_title=None,
            latitude=None,
            longitude=None,
        )
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "private_action",
                return_value={"status": "updated", "action": "set_early_reminder"},
            ) as private_action,
        ):
            result = self.remctl.apply_private_changes("PARENT-ID", args)

        self.assertEqual(private_action.call_args.args[0], {
            "action": "set_early_reminder",
            "id": "PARENT-ID",
            "unit": 0,
            "count": -15,
        })
        self.assertEqual(result[0]["action"], "set_early_reminder")

    def test_apply_private_changes_removes_existing_early_reminder_identifiers(self):
        args = SimpleNamespace(
            private=True,
            private_metadata=False,
            tags=None,
            url=None,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flag=False,
            flagged=None,
            urgent=None,
            early_reminder="1h",
            location_title=None,
            latitude=None,
            longitude=None,
        )
        row = {
            "Z_PK": 1,
            "ZDUEDATEDELTAALERTSDATA": json.dumps({
                "dueDateDeltaAlerts": [{"dueDateDeltaUnit": 0, "dueDateDeltaCount": -15, "identifier": "DELTA-1"}]
            }).encode(),
        }
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=row),
            mock.patch.object(
                self.remctl,
                "private_action",
                return_value={"status": "updated", "action": "set_early_reminder"},
            ) as private_action,
        ):
            self.remctl.apply_private_changes("PARENT-ID", args, db=object())

        self.assertEqual(private_action.call_args.args[0], {
            "action": "set_early_reminder",
            "id": "PARENT-ID",
            "existingIdentifiers": ["DELTA-1"],
            "unit": 1,
            "count": -1,
        })

    def test_subtask_rejects_private_url_before_writing(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit):
                self.remctl.parse_subtask_specs([
                    json.dumps({"title": "Child", "url": "file:///tmp/not-rich"})
                ])
        self.assertIn("http or https", stderr.getvalue())

    def _section_db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE ZREMCDBASESECTION (
                Z_PK INTEGER PRIMARY KEY,
                ZDISPLAYNAME TEXT,
                ZLIST INTEGER,
                ZCKIDENTIFIER TEXT,
                ZMARKEDFORDELETION INTEGER DEFAULT 0
            );
            CREATE TABLE ZREMCDBASELIST (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA TEXT
            );
        """)
        return db

    def test_duplicate_section_name_resolves_single_non_empty_match(self):
        db = self._section_db()
        section_a = "11111111-1111-1111-1111-111111111111"
        section_b = "22222222-2222-2222-2222-222222222222"
        db.execute(
            "INSERT INTO ZREMCDBASELIST (Z_PK, ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA) VALUES (?, ?)",
            (7, json.dumps({"memberships": [{"groupID": section_b, "memberID": "REMINDER-1"}]})),
        )
        db.executemany(
            "INSERT INTO ZREMCDBASESECTION (Z_PK, ZDISPLAYNAME, ZLIST, ZCKIDENTIFIER, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, 0)",
            [(1, "RemCTL", 7, section_a), (2, "RemCTL", 7, section_b)],
        )

        self.assertEqual(
            self.remctl.resolve_section_ckid(db, 7, section_name="RemCTL"),
            section_b,
        )
        db.close()

    def test_duplicate_section_name_errors_when_ambiguous_and_lists_section_ids(self):
        db = self._section_db()
        section_a = "11111111-1111-1111-1111-111111111111"
        section_b = "22222222-2222-2222-2222-222222222222"
        db.execute(
            "INSERT INTO ZREMCDBASELIST (Z_PK, ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA) VALUES (?, ?)",
            (7, json.dumps({"memberships": []})),
        )
        db.executemany(
            "INSERT INTO ZREMCDBASESECTION (Z_PK, ZDISPLAYNAME, ZLIST, ZCKIDENTIFIER, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, 0)",
            [(1, "RemCTL", 7, section_a), (2, "RemCTL", 7, section_b)],
        )

        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                self.remctl.resolve_section_ckid(db, 7, section_name="RemCTL")
        self.assertIn("--section-id", stderr.getvalue())
        self.assertIn(section_a, stderr.getvalue())
        self.assertIn(section_b, stderr.getvalue())
        db.close()

    def test_section_id_resolves_exact_target_and_rejects_section_name_too(self):
        db = self._section_db()
        section_id = "11111111-1111-1111-1111-111111111111"
        db.execute(
            "INSERT INTO ZREMCDBASELIST (Z_PK, ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA) VALUES (?, ?)",
            (7, json.dumps({"memberships": []})),
        )
        db.execute(
            "INSERT INTO ZREMCDBASESECTION (Z_PK, ZDISPLAYNAME, ZLIST, ZCKIDENTIFIER, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, 0)",
            (1, "RemCTL", 7, section_id),
        )

        self.assertEqual(
            self.remctl.resolve_section_ckid(
                db,
                7,
                section_id=f"x-apple-reminderkit://REMCDListSection/{section_id}",
            ),
            section_id,
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                self.remctl.resolve_section_ckid(
                    db,
                    7,
                    section_name="RemCTL",
                    section_id=section_id,
                )
        self.assertIn("either --section or --section-id", stderr.getvalue())
        db.close()

    def _sharee_db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE ZREMCDBASELIST (
                Z_PK INTEGER PRIMARY KEY,
                ZSHAREDOWNERIDENTIFIER BLOB,
                ZMARKEDFORDELETION INTEGER DEFAULT 0,
                Z_ENT INTEGER DEFAULT 3
            );
            CREATE TABLE ZREMCDOBJECT (
                Z_PK INTEGER PRIMARY KEY,
                Z_ENT INTEGER,
                ZMARKEDFORDELETION INTEGER DEFAULT 0,
                ZLIST INTEGER,
                ZCKIDENTIFIER TEXT,
                ZDISPLAYNAME TEXT,
                ZFIRSTNAME TEXT,
                ZLASTNAME TEXT,
                ZADDRESS1 TEXT,
                ZSTATUS INTEGER,
                ZACCESSLEVEL INTEGER
            );
        """)
        owner = uuid.UUID("EBA4B6AE-6FA9-4361-BA3D-F548DE185CDA")
        db.execute(
            "INSERT INTO ZREMCDBASELIST (Z_PK, ZSHAREDOWNERIDENTIFIER) VALUES (?, ?)",
            (7, owner.bytes),
        )
        db.executemany(
            "INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZLIST, ZCKIDENTIFIER, ZFIRSTNAME, ZLASTNAME, ZADDRESS1, ZSTATUS, ZACCESSLEVEL) "
            "VALUES (?, 36, 7, ?, ?, ?, ?, 2, 2)",
            [
                (10, "AAF651E6-F8D7-44FA-A71F-8AB3D69C5C5A", "Alex", "Example", "mailto:alex@example.com"),
                (11, "EBA4B6AE-6FA9-4361-BA3D-F548DE185CDA", "Current", "User", "mailto:current@example.com"),
            ],
        )
        return db

    def test_resolve_sharee_matches_name_email_and_me(self):
        db = self._sharee_db()
        self.assertEqual(self.remctl.resolve_sharee_or_die(db, 7, "Alex")["ZCKIDENTIFIER"], "AAF651E6-F8D7-44FA-A71F-8AB3D69C5C5A")
        self.assertEqual(self.remctl.resolve_sharee_or_die(db, 7, "alex@example.com")["ZCKIDENTIFIER"], "AAF651E6-F8D7-44FA-A71F-8AB3D69C5C5A")
        self.assertEqual(self.remctl.resolve_sharee_or_die(db, 7, "me")["ZCKIDENTIFIER"], "EBA4B6AE-6FA9-4361-BA3D-F548DE185CDA")
        db.close()

    def test_resolve_sharee_me_matches_lowercase_current_user_ckid(self):
        db = self._sharee_db()
        db.execute("UPDATE ZREMCDOBJECT SET ZCKIDENTIFIER = lower(ZCKIDENTIFIER)")
        self.assertEqual(
            self.remctl.resolve_sharee_or_die(db, 7, "me")["ZCKIDENTIFIER"],
            "eba4b6ae-6fa9-4361-ba3d-f548de185cda",
        )
        self.assertEqual(
            self.remctl.resolve_assignment_originator_or_die(db, 7)["ZCKIDENTIFIER"],
            "eba4b6ae-6fa9-4361-ba3d-f548de185cda",
        )
        db.close()

    def test_resolve_sharee_rejects_ambiguous_names(self):
        db = self._sharee_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZLIST, ZCKIDENTIFIER, ZFIRSTNAME, ZLASTNAME, ZADDRESS1, ZSTATUS, ZACCESSLEVEL) "
            "VALUES (?, 36, 7, ?, ?, ?, ?, 2, 2)",
            (12, "6D6E1104-E989-45F7-A5D5-1E8711E0B71D", "Alex", "Second", "mailto:alex.second@example.com"),
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                self.remctl.resolve_sharee_or_die(db, 7, "Alex")
        self.assertIn("multiple sharees match", stderr.getvalue())
        self.assertIn("alex.second@example.com", stderr.getvalue())
        db.close()

    def test_validate_private_args_rejects_assign_and_unassign_together(self):
        args = SimpleNamespace(
            assign="Alex",
            unassign=True,
            url=None,
            early_reminder=None,
            due=None,
            subtask=None,
            latitude=None,
            longitude=None,
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                self.remctl.validate_private_args(args)
        self.assertIn("either --assign or --unassign", stderr.getvalue())

    def test_assignment_requires_private_opt_in(self):
        args = SimpleNamespace(
            private=False,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flagged=None,
            urgent=None,
            early_reminder=None,
            location_title=None,
            latitude=None,
            longitude=None,
            assign="Alex",
            unassign=False,
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                self.remctl.refuse_private_args_without_opt_in(args)
        self.assertIn("assignment", stderr.getvalue())
        self.assertIn("--private", stderr.getvalue())

    def test_cmd_sharees_json_reports_assignment_candidates(self):
        db = self._sharee_db()
        list_ref = {
            "id": 7,
            "title": "Shopping",
            "objectUUID": "LIST-UUID",
            "requested": "Shopping",
            "method": "exact",
            "isGroceries": False,
        }
        args = SimpleNamespace(list="Shopping", list_id=None, json=True)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "resolve_required_list_target_or_die", return_value=list_ref),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_sharees(args)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["list"]["title"], "Shopping")
        self.assertEqual(len(payload["sharees"]), 2)
        self.assertEqual(payload["sharees"][0]["address"], "mailto:alex@example.com")
        self.assertTrue(payload["sharees"][1]["currentUser"])
        db.close()

    def test_cmd_sharees_json_reports_current_user_for_lowercase_ckid(self):
        db = self._sharee_db()
        db.execute("UPDATE ZREMCDOBJECT SET ZCKIDENTIFIER = lower(ZCKIDENTIFIER)")
        list_ref = {
            "id": 7,
            "title": "Shopping",
            "objectUUID": "LIST-UUID",
            "requested": "Shopping",
            "method": "exact",
            "isGroceries": False,
        }
        args = SimpleNamespace(list="Shopping", list_id=None, json=True)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "resolve_required_list_target_or_die", return_value=list_ref),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_sharees(args)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["sharees"][1]["currentUser"])
        db.close()

    def test_apply_private_changes_assigns_sharee_with_originator(self):
        db = self._sharee_db()
        args = SimpleNamespace(
            private=True,
            private_metadata=False,
            tags=None,
            url=None,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            flagged=None,
            flag=False,
            urgent=None,
            early_reminder=None,
            latitude=None,
            longitude=None,
            grocery=False,
            assign="Alex",
            unassign=False,
        )
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "private_action",
                return_value={"status": "updated", "action": "assign_sharee"},
            ) as private_action,
        ):
            result = self.remctl.apply_private_changes("REMINDER-1", args, db=db, list_pk=7)

        self.assertEqual(result[0]["action"], "assign_sharee")
        payload = private_action.call_args.args[0]
        self.assertEqual(payload["action"], "assign_sharee")
        self.assertEqual(payload["id"], "REMINDER-1")
        self.assertEqual(payload["assigneeId"], "AAF651E6-F8D7-44FA-A71F-8AB3D69C5C5A")
        self.assertEqual(payload["originatorId"], "EBA4B6AE-6FA9-4361-BA3D-F548DE185CDA")
        db.close()

    def test_cmd_add_private_invalid_url_fails_before_creation(self):
        args = SimpleNamespace(
            title="Bad private URL",
            list="Projects",
            notes=None,
            due=None,
            priority=None,
            flag=False,
            tags=None,
            url="file://localhost/tmp/secret",
            recurrence=None,
            alarm=None,
            private=True,
            private_metadata=False,
            section=None,
            new_section=None,
            subtask=None,
            image=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_add(args)
        bridge_call.assert_not_called()
        self.assertIn("http or https", stderr.getvalue())

    def test_cmd_add_private_rejects_local_rich_url_before_creation(self):
        args = SimpleNamespace(
            title="Research",
            list="Projects",
            list_id=None,
            notes=None,
            due="today",
            priority=None,
            flag=False,
            tags=None,
            url="http://127.0.0.1:631/",
            recurrence=None,
            alarm=None,
            private=True,
            private_metadata=False,
            grocery=False,
            section=None,
            section_id=None,
            new_section=None,
            subtask=None,
            image=None,
            urgent=None,
            early_reminder=None,
            json=True,
        )
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(args)
        bridge_call.assert_not_called()
        self.assertIn("public http or https", stderr.getvalue())

    def _cmd_add_private_args(self, **overrides):
        args = {
            "title": "Research",
            "list": "Projects",
            "list_id": None,
            "notes": None,
            "due": None,
            "priority": None,
            "flag": False,
            "tags": "remctl",
            "url": None,
            "recurrence": None,
            "alarm": None,
            "private": True,
            "private_metadata": False,
            "grocery": False,
            "section": None,
            "section_id": None,
            "new_section": None,
            "subtask": None,
            "image": None,
            "flagged": None,
            "urgent": None,
            "early_reminder": None,
            "location_title": None,
            "latitude": None,
            "longitude": None,
            "radius": 100,
            "proximity": "arriving",
            "address": None,
            "assign": None,
            "unassign": False,
            "set_tags": None,
            "clear_tags": False,
            "remove_tag": None,
            "json": True,
        }
        args.update(overrides)
        return SimpleNamespace(**args)

    def test_cmd_add_private_partial_failure_json_reports_created_id(self):
        fake_row = {"Z_PK": 23880, "ZCKIDENTIFIER": "ABC-123", "list_name": "Projects"}
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={"id": 7, "title": "Projects", "requested": "Projects", "method": "exact"},
            ),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "ABC-123"}),
            ) as bridge_call_result,
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=fake_row),
            mock.patch.object(
                self.remctl,
                "private_call",
                return_value={"status": "error", "message": "private helper blew up"},
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(self._cmd_add_private_args())

        bridge_call_result.assert_called_once()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["id"], "ABC-123")
        self.assertEqual(payload["numericId"], 23880)
        self.assertEqual(payload["failed"], "add_private_metadata")
        self.assertEqual(payload["error"], "private helper blew up")

    def test_cmd_add_private_partial_failure_text_warns_against_duplicate_add(self):
        fake_row = {"Z_PK": 23880, "ZCKIDENTIFIER": "ABC-123", "list_name": "Projects"}
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={"id": 7, "title": "Projects", "requested": "Projects", "method": "exact"},
            ),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "created", "id": "ABC-123"}),
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=fake_row),
            mock.patch.object(
                self.remctl,
                "private_call",
                return_value={"status": "error", "message": "private helper blew up"},
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(self._cmd_add_private_args(json=False))

        message = stderr.getvalue()
        self.assertIn("#23880", message)
        self.assertIn("Do NOT re-run add", message)
        self.assertIn("add_private_metadata", message)

    def test_cmd_add_private_unsafe_url_skips_bridge_call_result(self):
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result") as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(self._cmd_add_private_args(url="http://127.0.0.1:631/"))

        bridge_call_result.assert_not_called()

    def test_resolve_setup_shell_auto_skips_unsupported_shells(self):
        with mock.patch.dict("os.environ", {"SHELL": "/bin/tcsh"}, clear=False):
            self.assertEqual(self.remctl.resolve_setup_shell("auto"), "skip")
        with mock.patch.dict("os.environ", {"SHELL": "/bin/zsh"}, clear=False):
            self.assertEqual(self.remctl.resolve_setup_shell("auto"), "zsh")

    def test_cmd_setup_reports_config_and_completion_without_service(self):
        args = SimpleNamespace(
            shell="skip",
            doctor=False,
            json=False,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(self.remctl, "CONFIG_DIR", Path(tmpdir) / "config"),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_setup(args)
        output = stdout.getvalue()
        self.assertIn("RemCTL setup", output)
        self.assertIn("Shell completion: skipped", output)
        first_install = output.split("First install:", 1)[1].split("Unchanged existing install:", 1)[0]
        self.assertLess(first_install.index("remctl onboard"), first_install.index("restart the signed host"))
        self.assertLess(first_install.index("restart the signed host"), first_install.index("remctl doctor --for-agent"))
        existing = output.split("Unchanged existing install:", 1)[1].split("Upgrade or reinstall:", 1)[0]
        self.assertLess(existing.index("remctl doctor --for-agent"), existing.index("remctl onboard only if doctor"))
        upgrade = output.split("Upgrade or reinstall:", 1)[1]
        self.assertIn("run ./install.sh first, then follow the checks above", upgrade)
        self.assertIn("Repair: remctl permissions full-disk-access", output)

    def test_cmd_setup_zsh_reports_fpath_snippet_without_changing_json(self):
        args = SimpleNamespace(shell="zsh", doctor=False, json=False)
        completion_path = Path("/tmp/remctl-zsh/completions/_remctl")
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(self.remctl, "CONFIG_DIR", Path(tmpdir) / "config"),
                mock.patch.object(self.remctl, "install_completion", return_value=completion_path),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_setup(args)
        output = stdout.getvalue()
        self.assertIn("Enable zsh completions", output)
        self.assertIn(f"fpath=({completion_path.parent} $fpath)", output)

        json_args = SimpleNamespace(shell="zsh", doctor=False, json=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(self.remctl, "CONFIG_DIR", Path(tmpdir) / "config"),
                mock.patch.object(self.remctl, "install_completion", return_value=completion_path),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_setup(json_args)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["completion_shell"], "zsh")
        self.assertEqual(payload["completion_path"], str(completion_path))

    def test_gather_doctor_checks_warns_when_zsh_completion_is_not_on_fpath(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            completion_path = Path(tmpdir) / ".zsh" / "completions" / "_remctl"
            completion_path.parent.mkdir(parents=True)
            completion_path.write_text("#compdef remctl\n")
            with (
                mock.patch.object(self.remctl, "detect_shell_name", return_value="zsh"),
                mock.patch.object(self.remctl, "completion_target_path", return_value=completion_path),
                mock.patch.object(self.remctl, "zsh_completion_loadable", return_value=False),
                mock.patch.object(
                    self.remctl,
                    "bridge_access_check_for_onboarding",
                    return_value={"name": "eventkit", "status": "ok", "detail": "authorized", "fix": None},
                ),
            ):
                checks = self.remctl.gather_doctor_checks()
        fpath_check = next(check for check in checks if check["name"] == "completion_fpath")
        self.assertEqual(fpath_check["status"], "warn")
        self.assertIn("fpath=", fpath_check["fix"])

    def test_gather_doctor_checks_includes_eventkit_write_access(self):
        eventkit_check = {
            "name": "eventkit",
            "status": "fail",
            "detail": "EventKit access error: The operation couldn’t be completed. (Mach error 4099 - unknown error code)",
            "fix": "Run remctl onboard from this same context.",
        }
        with (
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="direct"),
            mock.patch.object(self.remctl, "bridge_access_check_for_onboarding", return_value=eventkit_check),
        ):
            checks = self.remctl.gather_doctor_checks()

        self.assertIn(eventkit_check, checks)
        self.assertEqual(next(check for check in checks if check["name"] == "eventkit")["status"], "fail")

    def test_bridge_call_result_timeout_reports_structured_error(self):
        with mock.patch.object(
            self.remctl.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="remctl-bridge", timeout=3),
        ):
            result = self.remctl.bridge_call_result({"action": "authorize"}, timeout=3)

        self.assertEqual(result["returncode"], -1)
        self.assertEqual(result["payload"]["status"], "timeout")
        self.assertIn("3s", result["payload"]["message"])

    def test_cmd_upcoming_rejects_non_positive_days_before_database_read(self):
        for days in (0, -1):
            args = SimpleNamespace(days=days, json=True, verbose=False, format="json")
            with (
                mock.patch.object(self.remctl, "open_db") as open_db,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_upcoming(args)
            open_db.assert_not_called()
            self.assertIn("between 1 and 3650", stderr.getvalue())

    def test_bridge_access_check_for_onboarding_reports_authorized_bridge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "remctl-bridge"
            fake_path.write_text("#!/bin/sh\n")
            fake_path.chmod(0o755)
            with (
                mock.patch.object(self.remctl, "current_bridge_path", return_value=fake_path),
                mock.patch.object(self.remctl.os, "access", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "bridge_call_result",
                    return_value={
                        "returncode": 0,
                        "stdout": '{"status":"authorized"}',
                        "stderr": "",
                        "payload": {
                            "status": "authorized",
                            "calendarCount": 3,
                            "defaultList": "Reminders",
                        },
                    },
                ),
            ):
                check = self.remctl.bridge_access_check_for_onboarding()

        self.assertEqual(check["status"], "ok")
        self.assertIn("Reminders access granted", check["detail"])
        self.assertIn("default: Reminders", check["detail"])

    def test_database_access_check_for_onboarding_reports_full_disk_access_failure(self):
        with (
            mock.patch.object(self.remctl, "reminders_store_access_error", return_value="db blocked"),
            mock.patch.object(self.remctl, "find_main_db_path", return_value=None),
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
        ):
            check = self.remctl.database_access_check_for_onboarding()
        self.assertEqual(check["status"], "fail")
        self.assertIn("db blocked", check["detail"])
        self.assertIn("Reinstall RemCTL", check["fix"])
        self.assertIn("add only that host", check["fix"])
        for forbidden in ("Terminal.app", "/tmp/python3", "Hermes", "Codex"):
            self.assertNotIn(forbidden, check["fix"])

    def test_detect_terminal_app_name_prefers_term_program(self):
        with mock.patch.dict("os.environ", {"TERM_PROGRAM": "Apple_Terminal"}, clear=False):
            self.assertEqual(self.remctl.detect_terminal_app_name(), "Terminal.app")

    def test_doctor_execution_context_prefers_ghostty_embedder_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = Path(tmpdir) / "awesoMux.app"
            resources = app / "Contents" / "Resources" / "ghostty"
            resources.mkdir(parents=True)
            with (
                mock.patch.dict(
                    self.remctl.os.environ,
                    {
                        "TERM_PROGRAM": "ghostty",
                        "GHOSTTY_RESOURCES_DIR": str(resources),
                    },
                    clear=False,
                ),
                mock.patch.object(self.remctl, "process_ancestry", return_value=[]),
                mock.patch.object(self.remctl, "find_app_bundle_by_identifier", return_value=None),
            ):
                context = self.remctl.doctor_execution_context()
        self.assertEqual(context["terminal_app"], "Ghostty.app")
        self.assertEqual(context["host_app"], "awesoMux.app")
        self.assertEqual(context["effective_context"], "awesoMux")
        self.assertEqual(context["host_app_source"], "GHOSTTY_RESOURCES_DIR")

    def test_bundle_context_ignores_nonexistent_env_app_paths(self):
        with (
            mock.patch.dict(
                self.remctl.os.environ,
                {"GHOSTTY_RESOURCES_DIR": "/tmp/NotARealApp.app/Contents/Resources/ghostty"},
                clear=False,
            ),
            mock.patch.object(self.remctl, "find_app_bundle_by_identifier", return_value=None),
        ):
            self.assertIsNone(self.remctl.bundle_context_from_environment())

    def test_find_app_bundle_by_identifier_rejects_query_metacharacters(self):
        with mock.patch.object(self.remctl.subprocess, "run") as run:
            self.assertIsNone(self.remctl.find_app_bundle_by_identifier("com.example' || *"))
        run.assert_not_called()

    def test_full_disk_access_fix_text_never_targets_callers(self):
        with mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"):
            hosted = self.remctl.full_disk_access_fix_text(rerun_command="remctl doctor")
        self.assertIn("signed RemCTL Capability Host", hosted)
        self.assertIn("Reinstall RemCTL", hosted)
        self.assertIn("add only that host", hosted)
        self.assertLess(hosted.index("add only that host"), hosted.index("restart the signed host"))
        self.assertLess(hosted.index("restart the signed host"), hosted.index("remctl doctor"))

        with mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="direct"):
            direct = self.remctl.full_disk_access_fix_text(rerun_command="remctl doctor")
        self.assertIn("caller-scoped and self-managed", direct)
        self.assertIn("does not configure direct callers", direct)
        for text in (hosted, direct):
            for forbidden in ("Terminal.app", "/tmp/python3", "Hermes", "Codex"):
                self.assertNotIn(forbidden, text)

    def test_doctor_execution_context_reports_codex_ancestor(self):
        with mock.patch.object(
            self.remctl,
            "process_ancestry",
            return_value=[
                {"pid": 10, "ppid": 9, "name": "python3", "command": "python3 remctl"},
                {"pid": 9, "ppid": 1, "name": "Codex", "command": "/Applications/Codex.app/Contents/MacOS/Codex"},
            ],
        ), mock.patch.object(self.remctl, "detect_terminal_app_name", return_value=None):
            context = self.remctl.doctor_execution_context()

        self.assertEqual(context["effective_context"], "Codex")
        self.assertEqual(context["host_app"], "Codex.app")
        self.assertEqual(context["parent_process"]["name"], "python3")

    def test_cmd_doctor_json_includes_execution_context_and_agent_note(self):
        checks = [{"name": "platform", "status": "ok", "detail": "macOS", "fix": None}]
        context = {
            "python": "/tmp/python3",
            "pid": 123,
            "parent_process": {"pid": 122, "ppid": 1, "name": "Codex", "command": "Codex"},
            "terminal_app": None,
            "host_app": "Codex.app",
            "effective_context": "Codex",
        }
        with (
            mock.patch.object(self.remctl, "gather_doctor_checks", return_value=checks),
            mock.patch.object(self.remctl, "doctor_execution_context", return_value=context),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_doctor(SimpleNamespace(json=True, for_agent=True))

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["context"]["effective_context"], "Codex")
        self.assertIn("effective", payload["agent_note"])
        self.assertIn("capabilityHost", payload)

    def test_cmd_doctor_distinguishes_direct_and_effective_host_access(self):
        status = {
            **self._absent_host_status,
            "status": "ok",
            "installed": True,
            "available": True,
            "ready": True,
            "fullReady": True,
            "protocolVersion": 2,
            "permissions": {
                "fullDiskAccess": "authorized",
                "reminders": "authorized",
                "automation": "authorized",
            },
        }
        checks = [
            {"name": "database", "status": "warn", "detail": "direct blocked", "fix": None},
            {"name": "effective_access", "status": "ok", "detail": "route=capabilityHost; ready=yes", "fix": None},
        ]
        with (
            mock.patch.object(self.remctl, "capability_host_status_snapshot", return_value=status),
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
            mock.patch.object(self.remctl, "gather_doctor_checks", return_value=checks),
            mock.patch.object(self.remctl, "doctor_execution_context", return_value={"effective_context": "Hermes"}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_doctor(SimpleNamespace(json=True, for_agent=True))

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["access"]["direct"]["ready"])
        self.assertEqual(payload["access"]["effective"]["route"], "capabilityHost")
        self.assertTrue(payload["access"]["effective"]["ready"])
        self.assertTrue(payload["capabilityHost"]["fullReady"])

    def _warming_host_status(self, **permissions):
        return {
            **self._absent_host_status,
            "status": "ok",
            "installed": True,
            "available": True,
            "ready": True,
            "fullReady": False,
            "protocolVersion": 2,
            "permissions": {
                "fullDiskAccess": "authorized",
                "reminders": "unknown",
                "automation": "unknown",
                **permissions,
            },
            "error": None,
        }

    def test_host_permission_state_classifies_every_reported_value(self):
        self.assertEqual(self.remctl.host_permission_state("authorized"), "authorized")
        for value in ("denied", "restricted", "writeOnly", "notDetermined"):
            with self.subTest(value=value):
                self.assertEqual(self.remctl.host_permission_state(value), "blocked")
        for value in ("unknown", "targetNotRunning", None, ""):
            with self.subTest(value=value):
                self.assertEqual(self.remctl.host_permission_state(value), "unverified")

    def test_gather_doctor_checks_warns_while_host_permissions_are_unverified(self):
        # A host that has just started answers "unknown" until its first
        # permission refresh lands, and reads and writes work the whole time.
        with mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"):
            checks = self.remctl.gather_doctor_checks(self._warming_host_status())
        by_name = {check["name"]: check for check in checks}
        self.assertEqual(by_name["eventkit"]["status"], "warn")
        self.assertEqual(by_name["automation"]["status"], "warn")
        self.assertEqual(by_name["capability_host"]["status"], "warn")
        self.assertEqual(by_name["effective_access"]["status"], "ok")
        self.assertEqual([check["name"] for check in checks if check["status"] == "fail"], [])

    def test_gather_doctor_checks_warns_when_reminders_is_not_running(self):
        # macOS answers targetNotRunning when it cannot reach Reminders, which
        # says nothing about whether Automation was granted.
        status = self._warming_host_status(reminders="authorized", automation="targetNotRunning")
        with mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"):
            checks = self.remctl.gather_doctor_checks(status)
        by_name = {check["name"]: check for check in checks}
        self.assertEqual(by_name["eventkit"]["status"], "ok")
        self.assertEqual(by_name["automation"]["status"], "warn")
        self.assertIn("flag", by_name["automation"]["fix"])
        self.assertEqual(by_name["capability_host"]["status"], "warn")
        self.assertEqual(by_name["effective_access"]["detail"], "route=capabilityHost; ready=yes")

    def test_gather_doctor_checks_names_why_an_mcp_connection_is_stale(self):
        def doctor(clients, tailscale):
            with (
                mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
                mock.patch.object(self.remctl, "mcp_overview", return_value={"clients": clients, "tailscale": tailscale}),
            ):
                checks = self.remctl.gather_doctor_checks(self._warming_host_status())
            return {check["name"]: check for check in checks}

        serving = {"configured": True, "active": True, "url": "https://mac.example.ts.net/remctl"}
        current = {"name": "Codex", "installed": True, "configured": True, "current": True}
        by_name = doctor([current], serving)
        self.assertEqual(by_name["mcp_clients"]["status"], "ok")
        self.assertNotIn("mcp_tailscale", by_name)

        stale = {**current, "current": False, "staleReason": "interpreter_versioned"}
        by_name = doctor([stale], {**serving, "agentInterpreterProblem": "interpreter_versioned"})
        self.assertEqual(by_name["mcp_clients"]["status"], "warn")
        self.assertEqual(
            by_name["mcp_clients"]["detail"],
            "MCP connection starts a versioned Homebrew Python that `brew upgrade` deletes: Codex",
        )
        self.assertEqual(by_name["mcp_tailscale"]["status"], "warn")
        self.assertEqual(
            by_name["mcp_tailscale"]["detail"],
            "Tailnet endpoint service starts a versioned Homebrew Python that `brew upgrade` deletes",
        )
        self.assertIn("remctl mcp install --client tailscale", by_name["mcp_tailscale"]["fix"])

    def test_gather_doctor_checks_still_fails_when_a_permission_is_refused(self):
        status = self._warming_host_status(reminders="denied", automation="denied")
        with mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"):
            checks = self.remctl.gather_doctor_checks(status)
        by_name = {check["name"]: check for check in checks}
        self.assertEqual(by_name["eventkit"]["status"], "fail")
        self.assertEqual(by_name["automation"]["status"], "fail")
        self.assertEqual(by_name["capability_host"]["status"], "fail")
        self.assertEqual(by_name["effective_access"]["status"], "fail")
        self.assertIn("onboard", by_name["automation"]["fix"])

    def test_gather_doctor_checks_fails_when_the_host_reports_an_error(self):
        status = self._warming_host_status()
        status["error"] = {"code": "protocol_mismatch", "message": "Capability Host protocol mismatch"}
        with mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"):
            checks = self.remctl.gather_doctor_checks(status)
        by_name = {check["name"]: check for check in checks}
        self.assertEqual(by_name["capability_host"]["status"], "fail")
        self.assertIn("protocol mismatch", by_name["capability_host"]["detail"])
        self.assertEqual(by_name["effective_access"]["status"], "fail")

    def test_capability_host_status_settled_waits_for_a_verified_snapshot(self):
        warming = self._warming_host_status()
        verified = self._warming_host_status(reminders="authorized", automation="authorized")
        verified["fullReady"] = True
        with (
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
            mock.patch.object(
                self.remctl,
                "capability_host_status_snapshot",
                side_effect=[warming, warming, verified],
            ),
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            settled = self.remctl.capability_host_status_settled()
        self.assertTrue(settled["fullReady"])
        self.assertEqual(settled["permissions"]["automation"], "authorized")

    def test_capability_host_status_settled_gives_up_within_its_timeout(self):
        warming = self._warming_host_status()
        with (
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
            mock.patch.object(self.remctl, "capability_host_status_snapshot", return_value=warming),
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            settled = self.remctl.capability_host_status_settled(timeout=0)
        self.assertEqual(settled["permissions"]["reminders"], "unknown")

    def test_print_full_disk_access_guidance_copies_only_signed_host_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_path = Path(tmpdir).resolve() / "RemCTL Capability Host.app"
            app_path.mkdir()
            status = self._valid_capability_host_status(app_path)
            marker = self._write_capability_host_marker(tmpdir, app_path)
            with (
                mock.patch.object(self.remctl, "capability_host_install_marker_path", return_value=marker),
                mock.patch.object(self.remctl, "copy_to_clipboard", return_value=True) as copy_to_clipboard,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.print_capability_host_full_disk_access_guidance(status, settings_opened=True)
        expected_path = str(app_path)
        copy_to_clipboard.assert_called_once_with(expected_path)
        output = stdout.getvalue()
        self.assertIn(f"Copied path to clipboard: {expected_path}", output)
        self.assertIn("Do not add Hermes, Python, Codex, or Terminal", output)
        self.assertIn("If you changed Full Disk Access, restart the host", output)

    def test_open_full_disk_access_settings_tries_modern_url_before_legacy(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return SimpleNamespace(returncode=1 if len(calls) == 1 else 0)

        with mock.patch.object(self.remctl.subprocess, "run", side_effect=fake_run):
            self.assertTrue(self.remctl.open_full_disk_access_settings())
        self.assertIn("com.apple.settings.PrivacySecurity.extension", calls[0][1])
        self.assertIn("com.apple.preference.security", calls[1][1])

    def test_print_check_report_indents_multiline_fixes(self):
        checks = [
            {
                "name": "database",
                "status": "fail",
                "detail": "degraded",
                "fix": "First line\n  remctl permissions full-disk-access\n  remctl doctor",
            }
        ]
        with (
            mock.patch.object(self.remctl.C, "enabled", False),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.print_check_report(None, checks)
        output = stdout.getvalue()
        self.assertIn("      First line", output)
        self.assertIn("        remctl permissions full-disk-access", output)
        self.assertIn("        remctl doctor", output)

    def test_cmd_onboard_opens_full_disk_access_settings_when_database_not_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_path = Path(tmpdir).resolve() / "RemCTL Capability Host.app"
            app_path.mkdir()
            status = self._valid_capability_host_status(app_path)
            marker = self._write_capability_host_marker(tmpdir, app_path)
            result = {
                "ok": True,
                "warnings": 1,
                "failures": 0,
                "checks": [
                    {"name": "database", "status": "warn", "detail": "db blocked", "fix": "Full Disk Access is required"},
                ],
                "capabilityHost": status,
            }
            with (
                mock.patch.object(self.remctl, "capability_host_install_marker_path", return_value=marker),
                mock.patch.object(self.remctl, "run_onboarding", return_value=result),
                mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
                mock.patch.object(self.remctl, "print_check_report"),
                mock.patch.object(self.remctl, "launch_full_disk_access_helper", return_value=False),
                mock.patch.object(self.remctl, "open_full_disk_access_settings", return_value=True) as open_settings,
                mock.patch.object(self.remctl, "print_capability_host_full_disk_access_guidance") as guidance,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_onboard(SimpleNamespace(json=False))
        open_settings.assert_called_once_with()
        guidance.assert_called_once_with(
            status,
            settings_opened=True,
        )

    def test_cmd_onboard_json_runs_onboarding_but_suppresses_fda_ui(self):
        result = {
            "ok": True,
            "warnings": 1,
            "failures": 0,
            "checks": [{
                "name": "database",
                "status": "warn",
                "detail": "Full Disk Access is blocked",
                "fix": "Add Full Disk Access",
            }],
            "capabilityHost": self._absent_host_status,
        }
        with (
            mock.patch.object(self.remctl, "run_onboarding", return_value=result) as run_onboarding,
            mock.patch.object(self.remctl, "launch_full_disk_access_helper") as launch,
            mock.patch.object(self.remctl, "open_full_disk_access_settings") as open_settings,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_onboard(SimpleNamespace(json=True))

        self.assertEqual(json.loads(stdout.getvalue()), result)
        run_onboarding.assert_called_once_with(auto=False)
        launch.assert_not_called()
        open_settings.assert_not_called()

    def test_gather_onboarding_checks_includes_core_cli_checks(self):
        with (
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="direct"),
            mock.patch.object(
                self.remctl,
                "open_reminders_app_for_onboarding",
                return_value={"name": "open_reminders", "status": "ok", "detail": "opened", "fix": None},
            ),
            mock.patch.object(
                self.remctl,
                "bridge_access_check_for_onboarding",
                return_value={"name": "eventkit", "status": "ok", "detail": "authorized", "fix": None},
            ),
            mock.patch.object(
                self.remctl,
                "applescript_access_check_for_onboarding",
                return_value={"name": "automation", "status": "ok", "detail": "authorized", "fix": None},
            ),
            mock.patch.object(
                self.remctl,
                "database_access_check_for_onboarding",
                return_value={"name": "database", "status": "ok", "detail": "/tmp/db.sqlite", "fix": None},
            ),
        ):
            checks = self.remctl.gather_onboarding_checks()

        self.assertEqual([check["name"] for check in checks], ["open_reminders", "eventkit", "automation", "database"])

    def test_gather_onboarding_checks_auto_requires_host_without_direct_prompts(self):
        with (
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
            mock.patch.object(self.remctl, "bridge_access_check_for_onboarding") as direct_bridge,
            mock.patch.object(self.remctl, "applescript_access_check_for_onboarding") as direct_automation,
            mock.patch.object(self.remctl, "database_access_check_for_onboarding") as direct_database,
        ):
            checks = self.remctl.gather_onboarding_checks(self._absent_host_status)
        self.assertEqual([check["name"] for check in checks], ["capability_host"])
        self.assertEqual(checks[0]["status"], "fail")
        self.assertIn("Reinstall", checks[0]["fix"])
        direct_bridge.assert_not_called()
        direct_automation.assert_not_called()
        direct_database.assert_not_called()

    def test_gather_onboarding_checks_requests_permissions_from_installed_host(self):
        status = {
            **self._absent_host_status,
            "status": "ok",
            "installed": True,
            "available": True,
            "ready": True,
            "fullReady": False,
            "permissions": {
                "fullDiskAccess": "authorized",
                "reminders": "notDetermined",
                "automation": "notDetermined",
            },
        }
        refreshed = {
            **status,
            "fullReady": True,
            "permissions": {
                "fullDiskAccess": "authorized",
                "reminders": "authorized",
                "automation": "authorized",
            },
        }
        with (
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
            mock.patch.object(
                self.remctl,
                "open_reminders_app_for_onboarding",
                return_value={"name": "open_reminders", "status": "ok", "detail": "opened", "fix": None},
            ),
            mock.patch.object(self.remctl.remctl_broker, "request_permission", return_value={}) as request_permission,
            mock.patch.object(self.remctl, "capability_host_status_snapshot", return_value=refreshed),
            mock.patch.object(self.remctl, "bridge_access_check_for_onboarding") as direct_bridge,
            mock.patch.object(self.remctl, "applescript_access_check_for_onboarding") as direct_automation,
            mock.patch.object(self.remctl, "database_access_check_for_onboarding") as direct_database,
        ):
            checks = self.remctl.gather_onboarding_checks(status)

        self.assertEqual(
            request_permission.call_args_list,
            [mock.call("reminders", timeout=305), mock.call("automation", timeout=305)],
        )
        direct_bridge.assert_not_called()
        direct_automation.assert_not_called()
        direct_database.assert_not_called()
        self.assertEqual(
            [check["name"] for check in checks],
            ["open_reminders", "eventkit", "automation", "database"],
        )
        self.assertTrue(all(check["status"] == "ok" for check in checks))

    def test_gather_onboarding_checks_stops_after_terminal_reminders_prompt(self):
        status = {
            **self._absent_host_status,
            "status": "ok",
            "installed": True,
            "available": True,
            "ready": True,
            "fullReady": False,
            "permissions": {
                "fullDiskAccess": "authorized",
                "reminders": "notDetermined",
                "automation": "notDetermined",
            },
        }
        terminal_errors = {
            "permission_prompt_cancelled": "was cancelled",
            "permission_prompt_timed_out": "timed out",
            "permission_prompt_unavailable": "could not present",
        }
        for code, message in terminal_errors.items():
            with self.subTest(code=code):
                error = self.remctl.remctl_broker.HostUnhealthy(message, code=code)
                with (
                    mock.patch.object(
                        self.remctl,
                        "open_reminders_app_for_onboarding",
                        return_value={
                            "name": "open_reminders",
                            "status": "ok",
                            "detail": "opened",
                            "fix": None,
                        },
                    ),
                    mock.patch.object(
                        self.remctl.remctl_broker,
                        "request_permission",
                        side_effect=error,
                    ) as request_permission,
                    mock.patch.object(
                        self.remctl,
                        "capability_host_status_snapshot",
                        return_value=status,
                    ),
                ):
                    checks = self.remctl.gather_onboarding_checks(status)

                request_permission.assert_called_once_with("reminders", timeout=305)
                by_name = {check["name"]: check for check in checks}
                self.assertEqual(by_name["eventkit"]["status"], "fail")
                self.assertIn(message, by_name["eventkit"]["detail"])
                self.assertEqual(by_name["automation"]["status"], "warn")
                self.assertIn("did not request Automation", by_name["automation"]["detail"])
                self.assertIn(message, by_name["automation"]["detail"])

    def test_needs_full_disk_access_guidance_false_for_missing_database(self):
        check = {
            "name": "database",
            "status": "warn",
            "detail": "No local Reminders database found.",
            "fix": "Enable iCloud Reminders and open Reminders.app once.",
        }
        self.assertFalse(self.remctl.needs_full_disk_access_guidance(check))

    def test_maybe_run_first_launch_onboarding_runs_once_for_interactive_commands(self):
        args = SimpleNamespace(cmd="today", json=False, format="plain")
        with (
            mock.patch.object(self.remctl, "should_auto_onboard", return_value=True),
            mock.patch.object(
                self.remctl,
                "run_onboarding",
                return_value={"ok": True, "warnings": 0, "failures": 0, "checks": []},
            ) as run_onboarding,
            mock.patch.object(self.remctl, "print_check_report") as print_report,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = self.remctl.maybe_run_first_launch_onboarding(args)

        self.assertTrue(result["ok"])
        run_onboarding.assert_called_once_with(auto=True)
        print_report.assert_called_once_with("RemCTL onboard", [])
        output = stdout.getvalue()
        self.assertIn(
            "First launch detected. Running remctl onboard for the signed RemCTL Capability Host.",
            output,
        )
        self.assertIn(
            "Approve the Reminders and Automation prompts for the Capability Host. "
            "The original command will then continue.",
            output,
        )

    def test_first_launch_full_disk_helper_targets_only_installed_host(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_path = Path(tmpdir).resolve() / "RemCTL Capability Host.app"
            app_path.mkdir()
            status = self._valid_capability_host_status(app_path)
            marker = self._write_capability_host_marker(tmpdir, app_path)
            result = {
                "ok": False,
                "warnings": 0,
                "failures": 1,
                "checks": [{
                    "name": "database",
                    "status": "fail",
                    "detail": "Full Disk Access is blocked",
                    "fix": "Add Full Disk Access",
                }],
                "capabilityHost": status,
            }
            with (
                mock.patch.object(self.remctl, "capability_host_install_marker_path", return_value=marker),
                mock.patch.object(self.remctl, "should_auto_onboard", return_value=True),
                mock.patch.object(self.remctl, "run_onboarding", return_value=result),
                mock.patch.object(self.remctl, "print_check_report"),
                mock.patch.object(self.remctl, "launch_full_disk_access_helper", return_value=True) as launch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.maybe_run_first_launch_onboarding(SimpleNamespace(cmd="today", json=False, format="plain"))
            launch.assert_called_once()
            launched_targets = launch.call_args.kwargs["targets"]
            self.assertEqual(len(launched_targets), 1)
            self.assertEqual(launched_targets[0]["path"], str(app_path))

    def test_first_launch_direct_mode_never_launches_full_disk_helper(self):
        status = {
            **self._absent_host_status,
            "installed": True,
            "app": {"path": "/Applications/RemCTL Capability Host.app", "installed": True},
        }
        result = {
            "ok": False,
            "warnings": 0,
            "failures": 1,
            "checks": [{
                "name": "database",
                "status": "fail",
                "detail": "Full Disk Access is blocked",
                "fix": "Add Full Disk Access",
            }],
            "capabilityHost": status,
        }
        with (
            mock.patch.object(self.remctl, "should_auto_onboard", return_value=True),
            mock.patch.object(self.remctl, "run_onboarding", return_value=result),
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="direct"),
            mock.patch.object(self.remctl, "capability_host_target_specs") as host_targets,
            mock.patch.object(self.remctl, "launch_full_disk_access_helper") as launch,
            mock.patch.object(self.remctl, "print_check_report"),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.maybe_run_first_launch_onboarding(
                SimpleNamespace(cmd="today", json=False, format="plain")
            )
        host_targets.assert_not_called()
        launch.assert_not_called()
        self.assertIn("caller-scoped and self-managed", stdout.getvalue())

    def test_should_auto_onboard_requires_an_interactive_protected_caller(self):
        def evaluate(
            args,
            *,
            stdin_tty=True,
            stdout_tty=True,
            platform="darwin",
            environment=None,
        ):
            with (
                mock.patch.object(self.remctl, "load_onboard_state", return_value=None),
                mock.patch.object(self.remctl.sys, "platform", platform),
                mock.patch.dict("os.environ", environment or {}, clear=True),
                mock.patch.object(
                    self.remctl.sys,
                    "stdin",
                    SimpleNamespace(isatty=lambda: stdin_tty),
                ),
                mock.patch.object(
                    self.remctl.sys,
                    "stdout",
                    SimpleNamespace(isatty=lambda: stdout_tty),
                ),
            ):
                return self.remctl.should_auto_onboard(args)

        interactive = SimpleNamespace(cmd="today", json=False, format="plain")
        self.assertTrue(evaluate(interactive))
        self.assertFalse(evaluate(interactive, stdin_tty=False))
        self.assertFalse(evaluate(interactive, stdout_tty=False))
        self.assertFalse(evaluate(interactive, platform="linux"))
        self.assertFalse(
            evaluate(interactive, environment={"REMCTL_SKIP_ONBOARD": "1"})
        )
        self.assertFalse(
            evaluate(
                interactive,
                environment={"REMCTL_CAPABILITY_HOST_ACTIVE": "1"},
            )
        )
        self.assertFalse(
            evaluate(SimpleNamespace(cmd="today", json=True, format="plain"))
        )
        self.assertFalse(
            evaluate(SimpleNamespace(cmd="today", json=False, format="json"))
        )
        for command in sorted(self.remctl.AUTO_ONBOARD_SKIP_COMMANDS):
            with self.subTest(command=command):
                self.assertFalse(
                    evaluate(
                        SimpleNamespace(cmd=command, json=False, format="plain")
                    )
                )

    def test_cmd_permissions_missing_host_fails_without_caller_targets(self):
        args = SimpleNamespace(cmd="permissions", topic="full-disk-access", scope="cli", wait=False, json=True)
        with (
            mock.patch.object(self.remctl, "capability_host_status_snapshot", return_value=self._absent_host_status),
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
            mock.patch.object(self.remctl, "current_permissions_path", return_value=Path("/tmp/remctl-permissions")),
            mock.patch.object(self.remctl, "permission_helper_available", return_value=True),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_permissions(args)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["available"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["targets"], [])
        self.assertEqual(payload["error"]["code"], "capability_host_not_installed")

    def test_cmd_permissions_targets_only_installed_capability_host(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_path = Path(tmpdir).resolve() / "RemCTL Capability Host.app"
            app_path.mkdir()
            status = self._valid_capability_host_status(app_path)
            marker = self._write_capability_host_marker(tmpdir, app_path)
            args = SimpleNamespace(topic="full-disk-access", wait=False, json=True)
            with (
                mock.patch.object(self.remctl, "capability_host_install_marker_path", return_value=marker),
                mock.patch.object(self.remctl, "capability_host_status_snapshot", return_value=status),
                mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="auto"),
                mock.patch.object(self.remctl, "current_permissions_path", return_value=Path("/tmp/remctl-permissions")),
                mock.patch.object(self.remctl, "permission_helper_available", return_value=True),
                mock.patch.object(self.remctl, "launch_full_disk_access_helper") as launch,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_permissions(args)
            launch.assert_not_called()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload["targets"]), 1)
        self.assertEqual(
            payload["targets"][0]["path"],
            str(app_path),
        )
        self.assertNotIn("Python", payload["targets"][0]["title"])

    def test_capability_host_target_specs_rejects_untrusted_status_and_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            app_path = root / "RemCTL Capability Host.app"
            app_path.mkdir()
            valid_status = self._valid_capability_host_status(app_path)
            marker = self._write_capability_host_marker(root, app_path)
            marker_patch = mock.patch.object(
                self.remctl,
                "capability_host_install_marker_path",
                return_value=marker,
            )
            marker_patch.start()
            self.addCleanup(marker_patch.stop)
            self.assertEqual(
                self.remctl.capability_host_target_specs(valid_status)[0]["path"],
                str(app_path),
            )

            other_root = root / "other"
            other_root.mkdir()
            other_app = other_root / "RemCTL Capability Host.app"
            other_app.mkdir()
            marker.write_text(f"{other_app}\n")
            self.assertEqual(self.remctl.capability_host_target_specs(valid_status), [])
            marker.write_text(f"{app_path}\n")

            invalid_signature = self._valid_capability_host_status(app_path)
            invalid_signature["app"]["signature"] = {
                **invalid_signature["app"]["signature"],
                "valid": False,
            }
            self.assertEqual(self.remctl.capability_host_target_specs(invalid_signature), [])

            wrong_identifier = self._valid_capability_host_status(app_path)
            wrong_identifier["app"]["bundleIdentifier"] = "com.example.fake-host"
            self.assertEqual(self.remctl.capability_host_target_specs(wrong_identifier), [])

            nonexistent = root / "missing" / "RemCTL Capability Host.app"
            self.assertEqual(
                self.remctl.capability_host_target_specs(
                    self._valid_capability_host_status(nonexistent)
                ),
                [],
            )

            wrong_name = root / "Python.app"
            wrong_name.mkdir()
            self.assertEqual(
                self.remctl.capability_host_target_specs(
                    self._valid_capability_host_status(wrong_name)
                ),
                [],
            )

            (root / "segment").mkdir()
            noncanonical = root / "segment" / ".." / "RemCTL Capability Host.app"
            self.assertEqual(
                self.remctl.capability_host_target_specs(
                    self._valid_capability_host_status(noncanonical)
                ),
                [],
            )

            link_root = root / "linked"
            link_root.mkdir()
            symlink_path = link_root / "RemCTL Capability Host.app"
            symlink_path.symlink_to(app_path, target_is_directory=True)
            self.assertEqual(
                self.remctl.capability_host_target_specs(
                    self._valid_capability_host_status(symlink_path)
                ),
                [],
            )

    def test_cmd_permissions_direct_mode_is_self_managed(self):
        args = SimpleNamespace(topic="full-disk-access", wait=False, json=True)
        with (
            mock.patch.object(self.remctl, "capability_host_status_snapshot", return_value=self._absent_host_status),
            mock.patch.object(self.remctl, "capability_host_requested_mode", return_value="direct"),
            mock.patch.object(self.remctl, "launch_full_disk_access_helper") as launch,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_permissions(args)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "direct_mode_self_managed")
        self.assertEqual(payload["targets"], [])
        launch.assert_not_called()

    def test_launch_full_disk_access_helper_passes_targets_and_after_commands(self):
        class FakePath:
            def exists(self):
                return True

            def __str__(self):
                return "/tmp/remctl-permissions"

        targets = [
            {"title": "RemCTL Capability Host.app", "path": "/Applications/RemCTL Capability Host.app", "subtitle": "Signed host"},
        ]
        with (
            mock.patch.object(self.remctl, "current_permissions_path", return_value=FakePath()),
            mock.patch.object(self.remctl, "current_cli_path", return_value=Path("/tmp/RemCTL Tools/remctl")),
            mock.patch.object(self.remctl.os, "access", return_value=True),
            mock.patch.object(self.remctl.os, "getuid", return_value=501),
            mock.patch.object(self.remctl.subprocess, "Popen") as popen,
        ):
            launched = self.remctl.launch_full_disk_access_helper(targets=targets)
        self.assertTrue(launched)
        args = popen.call_args.args[0]
        self.assertEqual(args.count("--target"), 1)
        target_index = args.index("--target")
        self.assertEqual(
            args[target_index + 1 : target_index + 4],
            [
                "RemCTL Capability Host.app",
                "/Applications/RemCTL Capability Host.app",
                "Signed host",
            ],
        )
        after_index = args.index("--after")
        self.assertEqual(args[after_index + 1], "Restart Host + Run Doctor")
        self.assertEqual(
            args[after_index + 2],
            "/bin/launchctl kickstart -k "
            f"gui/501/{self.remctl.remctl_broker.LAUNCH_AGENT_LABEL} && "
            "'/tmp/RemCTL Tools/remctl' doctor --for-agent",
        )
        self.assertLess(
            args[after_index + 2].index("launchctl kickstart"),
            args[after_index + 2].index("doctor --for-agent"),
        )

    # --- Regression: mutating-op routing (bridge vs AppleScript) ---
    # 2026-04-17: we discovered EKEventStore.save() from a short-lived CLI
    # process was dropping dueDateComponents from the CKRecord push because
    # the bridge embedded .timeZone in the components set AND nil'd
    # startDateComponents. Later the same day we fixed the bridge source,
    # re-tested, and flipped done/undone/delete/edit BACK to bridge-first
    # for speed (70ms vs 30–90s via AppleScript). flag/unflag stay
    # AppleScript-first because EventKit has no public flagged API and
    # priority-as-proxy is lossy.
    _FAKE_REMINDER = {
        "ZCKIDENTIFIER": "DEAD-BEEF-0000-0000-0000-000000000000",
        "ZTITLE": "test",
        "list_name": "Emails",
        "ZLIST": 7,
        "ZDUEDATE": None,
    }

    def _assert_bridge_first(self, cmd_name, args, expected_action):
        reminder = self._FAKE_REMINDER
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({
                    "status": {
                        "complete": "completed", "uncomplete": "uncompleted",
                        "delete": "deleted", "update": "updated",
                    }.get(expected_action, expected_action),
                    "id": reminder["ZCKIDENTIFIER"],
                }),
            ) as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            getattr(self.remctl, cmd_name)(args)
        bridge_call.assert_called_once()
        osa_try.assert_not_called()
        payload = bridge_call.call_args.args[0]
        self.assertEqual(payload["action"], expected_action)

    def _assert_applescript_first(self, cmd_name, args, script_contains):
        reminder = self._FAKE_REMINDER
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            getattr(self.remctl, cmd_name)(args)
        osa_try.assert_called_once()
        action = osa_try.call_args.args[1]
        self.assertIn(script_contains, action)
        bridge_call.assert_not_called()

    def test_cmd_done_is_bridge_first(self):
        self._assert_bridge_first("cmd_done", SimpleNamespace(id=1, json=True), "complete")

    def _done_with_date(self, reminder, args, *, bridge_available=True, bridge_result=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=bridge_available) as bridge_available_mock,
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result(bridge_result) if bridge_result is not None else None,
            ) as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                self.remctl.cmd_done(args)
                exit_code = None
            except SystemExit as exc:
                exit_code = exc.code
        return SimpleNamespace(
            stdout=out.getvalue(),
            stderr=err.getvalue(),
            exit_code=exit_code,
            bridge_available=bridge_available_mock,
            bridge_call=bridge_call,
            osa_try=osa_try,
        )

    def test_cmd_done_date_forwards_completion_date_to_bridge(self):
        reminder = {**self._FAKE_REMINDER, "recurrence_frequency": None}
        result = self._done_with_date(
            reminder,
            SimpleNamespace(id=1, json=True, date="2026-05-27 09:30"),
            bridge_result={"status": "completed", "id": reminder["ZCKIDENTIFIER"]},
        )
        self.assertIsNone(result.exit_code)
        payload = result.bridge_call.call_args.args[0]
        self.assertEqual(payload["action"], "complete")
        self.assertEqual(payload["completionDate"], "2026-05-27T09:30:00")
        self.assertEqual(json.loads(result.stdout)["completionDate"], "2026-05-27T09:30:00")
        result.osa_try.assert_not_called()

    def test_cmd_done_date_via_applescript_fallback(self):
        reminder = {**self._FAKE_REMINDER, "recurrence_frequency": None}
        result = self._done_with_date(
            reminder,
            SimpleNamespace(id=1, json=True, date="2026-05-27"),
            bridge_available=False,
        )
        self.assertIsNone(result.exit_code)
        result.bridge_call.assert_not_called()
        script = result.osa_try.call_args.args[1]
        self.assertIn("set completed of r to true", script)
        self.assertIn("set completion date of r to _rdt", script)
        self.assertEqual(json.loads(result.stdout)["completionDate"], "2026-05-27T00:00:00")

    def test_cmd_done_date_rejects_recurring_reminder(self):
        reminder = {**self._FAKE_REMINDER, "recurrence_frequency": 2}
        result = self._done_with_date(
            reminder,
            SimpleNamespace(id=1, json=True, date="2026-05-27"),
        )
        self.assertEqual(result.exit_code, 1)
        result.bridge_call.assert_not_called()
        payload = json.loads(result.stderr)
        self.assertEqual(payload["code"], "completion_date_unsupported_for_recurring")

    def test_cmd_done_date_rejects_non_absolute_date_before_writing(self):
        reminder = {**self._FAKE_REMINDER, "recurrence_frequency": None}
        result = self._done_with_date(
            reminder,
            SimpleNamespace(id=1, json=True, date="today"),
        )
        self.assertEqual(result.exit_code, 2)
        result.bridge_available.assert_not_called()
        result.bridge_call.assert_not_called()
        self.assertEqual(json.loads(result.stderr)["code"], "invalid_completion_date")

    def test_cmd_undone_is_bridge_first(self):
        self._assert_bridge_first("cmd_undone", SimpleNamespace(id=1, json=True), "uncomplete")

    def test_cmd_delete_is_bridge_first(self):
        self._assert_bridge_first(
            "cmd_delete", SimpleNamespace(id=1, json=True, force=True), "delete"
        )

    def test_confirmation_required_is_structured_and_does_not_prompt_on_stdout(self):
        args = SimpleNamespace(force=False, json=True)
        with (
            mock.patch.object(sys, "stdin", io.StringIO("")),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.confirm_destructive_action(
                args,
                "Delete reminder? [y/N] ",
                identifier=967,
            )

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "confirmation_required")
        self.assertEqual(payload["id"], 967)

    def test_interactive_confirmation_prompt_uses_stderr(self):
        class TTYInput(io.StringIO):
            def isatty(self):
                return True

        with (
            mock.patch.object(sys, "stdin", TTYInput("n\n")),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            confirmed = self.remctl.confirm_destructive_action(
                SimpleNamespace(force=False, json=False),
                "Delete reminder? [y/N] ",
            )

        self.assertFalse(confirmed)
        self.assertEqual(stderr.getvalue(), "Delete reminder? [y/N] ")
        self.assertEqual(stdout.getvalue(), "Cancelled.\n")

    def test_all_delete_commands_share_noninteractive_confirmation_guard(self):
        reminder = {"ZTITLE": "Throwaway", "list_name": "Reminders", "ZCKIDENTIFIER": "REM-1"}
        list_ref = {"id": 10, "title": "Throwaway", "objectUUID": "LIST-1"}
        smart_row = {
            "Z_PK": 12,
            "ZNAME": "Focus",
            "ZCKIDENTIFIER": "SMART-1",
            "ZSMARTLISTTYPE": "custom",
        }
        template_ref = {"id": 13, "name": "Packing", "objectUUID": "TEMPLATE-1"}
        group_ref = {
            "id": 14,
            "title": "Writing",
            "objectUUID": "GROUP-1",
            "children": [{"id": 15, "title": "Editorial", "objectUUID": "LIST-3"}],
        }
        with (
            mock.patch.object(self.remctl, "confirm_destructive_action", return_value=False) as confirm,
            mock.patch.object(self.remctl, "require_private_metadata"),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "resolve_required_list_target_or_die", return_value=list_ref),
            mock.patch.object(self.remctl, "resolve_section_ckid", return_value="SECTION-1"),
            mock.patch.object(self.remctl, "q_custom_smart_list_delete_matches", return_value=[smart_row]),
            mock.patch.object(self.remctl, "resolve_required_template_target_or_die", return_value=template_ref),
            mock.patch.object(self.remctl, "resolve_required_group_target_or_die", return_value=group_ref),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            mock.patch.object(self.remctl, "private_call") as private_call,
        ):
            self.remctl.cmd_delete(SimpleNamespace(id=1, force=False, json=False))
            self.remctl.cmd_list_delete(SimpleNamespace(name="Throwaway", list_id=None, force=False, json=False))
            self.remctl.cmd_section_delete(SimpleNamespace(
                name="Current", section_id=None, list="Projects", list_id=None,
                private=True, private_metadata=False, force=False, json=False,
            ))
            self.remctl.cmd_smart_list_delete(SimpleNamespace(
                name="Focus", smart_list_id=None, private=True, force=False, json=False,
            ))
            self.remctl.cmd_template_delete(SimpleNamespace(
                name="Packing", template_id=None, private=True, force=False, json=False,
            ))
            self.remctl.cmd_group_delete(SimpleNamespace(
                name="Writing", group_id=None, private=True, force=False, json=False,
            ))

        self.assertEqual(confirm.call_count, 6)
        bridge_call.assert_not_called()
        private_call.assert_not_called()

    def _flag_cmd(self, cmd_name, *, set_result):
        reminder = self._FAKE_REMINDER
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl, "osa_set_flagged_result", return_value=set_result
            ) as set_flag,
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                getattr(self.remctl, cmd_name)(SimpleNamespace(id=1, json=True))
                exit_code = None
            except SystemExit as exc:
                exit_code = exc.code
        return SimpleNamespace(
            stdout=out.getvalue(),
            stderr=err.getvalue(),
            exit_code=exit_code,
            set_flag=set_flag,
            bridge_call=bridge_call,
        )

    def test_cmd_flag_writes_real_flag_via_applescript_only(self):
        result = self._flag_cmd("cmd_flag", set_result=(True, ""))
        self.assertIsNone(result.exit_code)
        result.set_flag.assert_called_once_with(
            self._FAKE_REMINDER["ZCKIDENTIFIER"], flagged=True
        )
        result.bridge_call.assert_not_called()
        self.assertEqual(json.loads(result.stdout)["status"], "flagged")

    def test_cmd_unflag_writes_real_flag_via_applescript_only(self):
        result = self._flag_cmd("cmd_unflag", set_result=(True, ""))
        self.assertIsNone(result.exit_code)
        result.set_flag.assert_called_once_with(
            self._FAKE_REMINDER["ZCKIDENTIFIER"], flagged=False
        )
        result.bridge_call.assert_not_called()
        self.assertEqual(json.loads(result.stdout)["status"], "unflagged")

    def test_cmd_flag_applescript_failure_is_an_error_not_bridge_fallback(self):
        # The old bridge fallback set priority=1 as a "flag proxy" and reported
        # success without ever touching ZFLAGGED; reminders in group-nested
        # lists hit it deterministically because their lists are invisible to
        # `tell list "<name>"`.
        result = self._flag_cmd(
            "cmd_flag",
            set_result=(False, 'Can\u2019t get list "Editorial". (-1728)'),
        )
        self.assertEqual(result.exit_code, 1)
        result.bridge_call.assert_not_called()
        payload = json.loads(result.stderr)
        self.assertEqual(payload["code"], "applescript_flag_failed")
        self.assertIn("-1728", payload["message"])

    def test_cmd_unflag_applescript_failure_is_an_error_not_bridge_fallback(self):
        result = self._flag_cmd("cmd_unflag", set_result=(False, "AppleScript timed out after 120s"))
        self.assertEqual(result.exit_code, 1)
        result.bridge_call.assert_not_called()
        payload = json.loads(result.stderr)
        self.assertEqual(payload["code"], "applescript_flag_failed")
        self.assertIn("timed out", payload["message"])

    def test_osa_set_flagged_result_addresses_reminder_at_app_level(self):
        # `tell list "<name>"` cannot see lists nested inside groups (-1728);
        # the app-level `reminder id` specifier reaches reminders in any list.
        with mock.patch.object(self.remctl.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            ok, err = self.remctl.osa_set_flagged_result("ABC-123", flagged=True)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        script = run.call_args.args[0][2]
        self.assertIn('reminder id "x-apple-reminder://ABC-123"', script)
        self.assertIn("to true", script)
        self.assertNotIn("tell list", script)

    def test_osa_by_id_try_addresses_reminder_at_app_level(self):
        # The AppleScript fallback for done, undone, delete, and edit must also
        # reach reminders in lists nested inside groups.
        with mock.patch.object(self.remctl.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertTrue(self.remctl.osa_by_id_try("x-apple-reminder://ABC-123", "delete r"))
        script = run.call_args.args[0][2]
        self.assertIn('set r to reminder id "x-apple-reminder://ABC-123"\ndelete r', script)
        self.assertNotIn("tell list", script)

    def test_osa_set_flagged_result_surfaces_osascript_stderr(self):
        with mock.patch.object(self.remctl.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr='36:40: execution error: Reminders got an error: Can\u2019t get list "Editorial". (-1728)\n',
            )
            ok, err = self.remctl.osa_set_flagged_result("ABC-123", flagged=False)
        self.assertFalse(ok)
        self.assertIn("-1728", err)
        script = run.call_args.args[0][2]
        self.assertIn("to false", script)

    def test_cmd_edit_due_date_is_bridge_first(self):
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due="2026-04-20 09:00", url=None, recurrence=None, alarm=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        bridge_call_result.assert_called_once()
        osa_try.assert_not_called()
        payload = bridge_call_result.call_args.args[0]
        self.assertEqual(payload["action"], "update")
        self.assertEqual(payload["due"], "2026-04-20T09:00:00")
        self.assertNotIn("allDay", payload)

    def test_cmd_edit_date_only_due_is_all_day_bridge_update(self):
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due="2026-06-21", url=None, recurrence=None, alarm=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)

        bridge_call_result.assert_called_once()
        osa_try.assert_not_called()
        payload = bridge_call_result.call_args.args[0]
        self.assertEqual(payload["action"], "update")
        self.assertEqual(payload["due"], "2026-06-21T00:00:00")
        self.assertTrue(payload["allDay"])

    def test_cmd_edit_date_only_due_requires_bridge(self):
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due="2026-06-21", url=None, recurrence=None, alarm=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_edit(args)

        bridge_call.assert_not_called()
        osa_try.assert_not_called()
        self.assertIn("all-day due date edits require remctl-bridge", stderr.getvalue())

    def test_cmd_edit_due_date_carries_matching_absolute_alarm(self):
        from datetime import datetime

        old_due = datetime(2026, 5, 26, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(old_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(old_due),
        })
        alarm_rows = [{
            "alarm_id": 7,
            "time_interval": None,
            "latitude": None,
            "longitude": None,
            "date_components": json.dumps({
                "year": 2026,
                "month": 5,
                "day": 26,
                "hour": 15,
                "minute": 0,
                "second": 0,
                "timeZone": {"identifier": "Europe/Rome"},
            }),
        }]

        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "q_alarms", return_value=alarm_rows),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1,
                json=True,
                title=None,
                notes=None,
                priority=None,
                due="2026-05-26 16:00",
                url=None,
                recurrence=None,
                alarm=None,
            ))

        payload = bridge_call_result.call_args.args[0]
        self.assertEqual(payload["due"], "2026-05-26T16:00:00")
        self.assertEqual(payload["alarm"], "2026-05-26T16:00:00")

    def test_cmd_edit_due_date_requires_bridge_when_matching_alarm_must_move(self):
        from datetime import datetime

        old_due = datetime(2026, 5, 26, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(old_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(old_due),
        })
        alarm_rows = [{
            "alarm_id": 7,
            "time_interval": None,
            "latitude": None,
            "longitude": None,
            "date_components": json.dumps({
                "year": 2026,
                "month": 5,
                "day": 26,
                "hour": 15,
                "minute": 0,
                "second": 0,
                "timeZone": {"identifier": "Europe/Rome"},
            }),
        }]

        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "q_alarms", return_value=alarm_rows),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_edit(SimpleNamespace(
                    id=1,
                    json=True,
                    title=None,
                    notes=None,
                    priority=None,
                    due="2026-05-26 16:00",
                    url=None,
                    recurrence=None,
                    alarm=None,
                ))

        bridge_call.assert_not_called()
        osa_try.assert_not_called()
        self.assertIn("requires remctl-bridge", stderr.getvalue())

    def test_cmd_edit_due_date_does_not_carry_custom_absolute_alarm(self):
        from datetime import datetime

        old_due = datetime(2026, 5, 26, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(old_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(datetime(2026, 5, 26, 9, 0, 0)),
        })
        alarm_rows = [{
            "alarm_id": 7,
            "time_interval": None,
            "latitude": None,
            "longitude": None,
            "date_components": json.dumps({
                "year": 2026,
                "month": 5,
                "day": 26,
                "hour": 9,
                "minute": 0,
                "second": 0,
                "timeZone": {"identifier": "Europe/Rome"},
            }),
        }]

        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "q_alarms", return_value=alarm_rows),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1,
                json=True,
                title=None,
                notes=None,
                priority=None,
                due="2026-05-26 16:00",
                url=None,
                recurrence=None,
                alarm=None,
            ))

        self.assertNotIn("alarm", bridge_call_result.call_args.args[0])

    def test_cmd_edit_due_date_noop_preserves_custom_absolute_alarm(self):
        from datetime import datetime

        current_due = datetime(2026, 5, 26, 16, 0, 0)
        custom_alarm = datetime(2026, 5, 26, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(current_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(custom_alarm),
        })
        alarm_rows = [{
            "alarm_id": 7,
            "time_interval": None,
            "latitude": None,
            "longitude": None,
            "date_components": json.dumps({
                "year": 2026,
                "month": 5,
                "day": 26,
                "hour": 15,
                "minute": 0,
                "second": 0,
                "timeZone": {"identifier": "Europe/Rome"},
            }),
        }]

        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "q_alarms", return_value=alarm_rows),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1,
                json=True,
                title=None,
                notes=None,
                priority=None,
                due="2026-05-26 16:00",
                url=None,
                recurrence=None,
                alarm=None,
            ))

        self.assertEqual(bridge_call_result.call_count, 2)
        final_payload = bridge_call_result.call_args_list[-1].args[0]
        self.assertEqual(final_payload["due"], "2026-05-26T16:00:00")
        self.assertNotIn("alarm", final_payload)

    def test_cmd_edit_due_clear_clears_matching_absolute_alarm(self):
        from datetime import datetime

        old_due = datetime(2026, 5, 26, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(old_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(old_due),
        })
        alarm_rows = [{
            "alarm_id": 7,
            "time_interval": None,
            "latitude": None,
            "longitude": None,
            "date_components": json.dumps({
                "year": 2026,
                "month": 5,
                "day": 26,
                "hour": 15,
                "minute": 0,
                "second": 0,
                "timeZone": {"identifier": "Europe/Rome"},
            }),
        }]

        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "q_alarms", return_value=alarm_rows),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1,
                json=True,
                title=None,
                notes=None,
                priority=None,
                due="clear",
                url=None,
                recurrence=None,
                alarm=None,
            ))

        payload = bridge_call_result.call_args.args[0]
        self.assertIsNone(payload["due"])
        self.assertTrue(payload["clearAlarms"])

    def _absolute_alarm_row(self, alarm_id, when):
        return {
            "alarm_id": alarm_id,
            "time_interval": None,
            "latitude": None,
            "longitude": None,
            "date_components": json.dumps({
                "year": when.year,
                "month": when.month,
                "day": when.day,
                "hour": when.hour,
                "minute": when.minute,
                "second": when.second,
                "timeZone": {"identifier": "Europe/Rome"},
            }),
        }

    def _bridge_payloads_for_due_edit(self, reminder, alarm_rows, due):
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "q_alarms", return_value=alarm_rows),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1,
                json=True,
                title=None,
                notes=None,
                priority=None,
                due=due,
                url=None,
                recurrence=None,
                alarm=None,
            ))
        return [call.args[0] for call in bridge_call_result.call_args_list]

    def test_cmd_edit_due_date_carries_every_copy_of_the_due_alarm(self):
        from datetime import datetime

        # Each device that handles the notification stores its own
        # acknowledged copy of the alarm. Moving only the due date left both
        # copies behind, and Reminders kept showing the old time in Today.
        old_due = datetime(2026, 9, 22, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(old_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(old_due),
        })
        alarm_rows = [self._absolute_alarm_row(7640, old_due), self._absolute_alarm_row(7655, old_due)]

        payload = self._bridge_payloads_for_due_edit(reminder, alarm_rows, "2026-09-24 15:00")[-1]

        self.assertEqual(payload["due"], "2026-09-24T15:00:00")
        self.assertEqual(payload.get("alarm"), "2026-09-24T15:00:00")

    def test_cmd_edit_due_date_keeps_alarms_when_one_is_custom(self):
        from datetime import datetime

        old_due = datetime(2026, 9, 22, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(old_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(datetime(2026, 9, 22, 9, 0, 0)),
        })
        alarm_rows = [
            self._absolute_alarm_row(7640, old_due),
            self._absolute_alarm_row(7641, datetime(2026, 9, 22, 9, 0, 0)),
        ]

        payload = self._bridge_payloads_for_due_edit(reminder, alarm_rows, "2026-09-24 15:00")[-1]

        self.assertEqual(payload["due"], "2026-09-24T15:00:00")
        self.assertNotIn("alarm", payload)

    def test_cmd_edit_due_clear_clears_every_copy_of_the_due_alarm(self):
        from datetime import datetime

        old_due = datetime(2026, 9, 22, 15, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder.update({
            "Z_PK": 1,
            "ZDUEDATE": self.remctl.to_ts(old_due),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(old_due),
        })
        alarm_rows = [self._absolute_alarm_row(7640, old_due), self._absolute_alarm_row(7655, old_due)]

        payload = self._bridge_payloads_for_due_edit(reminder, alarm_rows, "clear")[-1]

        self.assertIsNone(payload["due"])
        self.assertIs(payload.get("clearAlarms"), True)

    def test_cmd_edit_alarm_clear_routes_through_bridge(self):
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due=None, url=None, recurrence=None, alarm="clear",
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        payload = bridge_call_result.call_args.args[0]
        self.assertTrue(payload["clearAlarms"])
        self.assertNotIn("alarm", payload)

    @staticmethod
    def _edit_args(**overrides):
        base = dict(
            id=1, json=True, title=None, list=None, list_id=None,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_edit_url_preserves_notes_on_bridge_and_applescript_paths(self):
        cases = [
            (None, "Existing notes", "Existing notes\n\nhttps://example.com"),
            (None, None, "https://example.com"),
            ("Replacement", "Old notes", "Replacement\n\nhttps://example.com"),
            ("", "Old notes", "https://example.com"),
        ]
        for bridge in (True, False):
            for supplied, existing, expected in cases:
                with self.subTest(bridge=bridge, notes=supplied, existing=existing):
                    reminder = dict(self._FAKE_REMINDER, ZNOTES=existing)
                    args = self._edit_args(notes=supplied, url="https://example.com")
                    with (
                        mock.patch.object(self.remctl, "open_db", return_value=None),
                        mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
                        mock.patch.object(self.remctl, "bridge_available", return_value=bridge),
                        mock.patch.object(self.remctl, "bridge_call_result", return_value=self._bridge_result({"status": "updated"})) as call,
                        mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        self.remctl.cmd_edit(args)
                    if bridge:
                        self.assertEqual(call.call_args.args[0]["notes"], expected)
                        osa.assert_not_called()
                    else:
                        self.assertIn(f'set body of r to "{self.remctl.esc(expected)}"', osa.call_args.args[1])

    def test_edit_empty_notes_clears_applescript_body(self):
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=self._FAKE_REMINDER),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(self._edit_args(notes=""))
        self.assertIn('set body of r to ""', osa.call_args.args[1])

    def test_cmd_edit_json_echoes_the_new_title_after_a_rename(self):
        reminder = self._FAKE_REMINDER
        args = self._edit_args(title="Renamed")
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["title"], "Renamed")

    def test_cmd_edit_json_echoes_the_existing_title_when_the_edit_is_not_a_rename(self):
        reminder = self._FAKE_REMINDER
        args = self._edit_args(priority="high")
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        self.assertEqual(json.loads(stdout.getvalue())["title"], reminder["ZTITLE"])

    def test_cmd_edit_applescript_fallback_json_echoes_title(self):
        reminder = self._FAKE_REMINDER
        args = self._edit_args(title="Renamed")
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(payload["title"], "Renamed")

    def test_cmd_edit_private_only_json_echoes_title(self):
        reminder = self._FAKE_REMINDER
        args = self._edit_args(private=True, flagged=True)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result") as bridge_call_result,
            mock.patch.object(
                self.remctl,
                "apply_private_changes",
                return_value=[{"status": "updated", "action": "set_flagged"}],
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        bridge_call_result.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(payload["title"], reminder["ZTITLE"])
        self.assertEqual(len(payload["private"]), 1)

    def test_cmd_edit_nothing_to_update_emits_structured_json_and_exits_zero(self):
        reminder = self._FAKE_REMINDER
        args = self._edit_args()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_try,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        osa_try.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "unchanged")
        self.assertEqual(payload["code"], "nothing_to_update")
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["title"], reminder["ZTITLE"])
        self.assertEqual(payload["message"], "Nothing to update.")

    def test_cmd_edit_nothing_to_update_stays_plain_text_without_json(self):
        reminder = self._FAKE_REMINDER
        args = self._edit_args(json=False)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        self.assertEqual(stdout.getvalue().strip(), "Nothing to update.")

    def test_cmd_edit_moves_reminder_to_list_through_eventkit_bridge(self):
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, list="Projects", list_id=None,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 9, "title": "Projects", "requested": "Projects", "method": "exact"},
            ) as resolve_list,
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            mock.patch.object(self.remctl, "private_available") as private_available,
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_try,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        resolve_list.assert_called_once_with(mock.ANY, name="Projects", list_id=None)
        bridge_call_result.assert_called_once()
        self.assertEqual(bridge_call_result.call_args.args[0]["list"], "Projects")
        private_available.assert_not_called()
        osa_try.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())["list"], "Projects")

    def test_cmd_edit_moves_by_list_id_and_reports_resolved_list(self):
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, list=None, list_id=9,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 9, "title": "Projects", "requested": "9", "method": "id"},
            ),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        self.assertEqual(bridge_call_result.call_args.args[0]["list"], "Projects")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["resolvedList"]["id"], 9)
        self.assertEqual(payload["resolvedList"]["method"], "id")

    def test_cmd_edit_moves_parent_with_subtasks_by_verified_clone_delete(self):
        reminder = dict(self._FAKE_REMINDER)
        reminder["Z_PK"] = 1
        reminder["ZLIST"] = 7
        children = [
            {"Z_PK": 2, "ZCKIDENTIFIER": "CHILD-1"},
            {"Z_PK": 3, "ZCKIDENTIFIER": "CHILD-2"},
        ]
        target = {"id": 9, "title": "Projects", "requested": "9", "method": "id", "objectUUID": "LIST-UUID"}
        args = SimpleNamespace(
            id=1, json=True, title=None, list=None, list_id=9,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value=target,
            ),
            mock.patch.object(self.remctl, "subtask_rows_for_parent_move", return_value=children) as subtask_rows,
            mock.patch.object(
                self.remctl,
                "clone_reminder_tree_to_list_or_die",
                return_value={
                    "status": "updated",
                    "id": 42,
                    "oldId": 1,
                    "list": "Projects",
                    "objectUUID": "NEW-REMINDER",
                    "subtasksMoved": 2,
                    "method": "clone-delete",
                    "delete": {"status": "deleted"},
                },
            ) as clone_move,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        subtask_rows.assert_called_once_with(mock.ANY, reminder)
        clone_move.assert_called_once_with(mock.ANY, reminder, target, children)
        bridge_call.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["id"], 42)
        self.assertEqual(payload["oldId"], 1)
        self.assertEqual(payload["method"], "clone-delete")
        self.assertEqual(payload["subtasksMoved"], 2)
        self.assertTrue(payload["originalDeleted"])
        self.assertEqual(payload["resolvedList"]["id"], 9)

    def test_cmd_edit_rejects_parent_with_subtasks_move_plus_other_edits(self):
        reminder = dict(self._FAKE_REMINDER)
        reminder["Z_PK"] = 1
        reminder["ZLIST"] = 7
        args = SimpleNamespace(
            id=1, json=True, title="New title", list=None, list_id=9,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 9, "title": "Projects", "requested": "9", "method": "id", "objectUUID": "LIST-UUID"},
            ),
            mock.patch.object(self.remctl, "subtask_rows_for_parent_move", return_value=[{"Z_PK": 2, "ZCKIDENTIFIER": "CHILD-1"}]),
            mock.patch.object(self.remctl, "clone_reminder_tree_to_list_or_die") as clone_move,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_edit(args)

        clone_move.assert_not_called()
        bridge_call.assert_not_called()
        self.assertIn("cannot currently be combined with other edits", stderr.getvalue())

    def test_cmd_edit_clone_deletes_ordinary_list_move_after_container_rejection(self):
        reminder = dict(self._FAKE_REMINDER)
        reminder["Z_PK"] = 1
        reminder["ZLIST"] = 7
        target = {"id": 9, "title": "Shared Tasks", "requested": "Shared Tasks", "method": "exact", "objectUUID": "LIST-UUID"}
        args = SimpleNamespace(
            id=1, json=True, title=None, list="Shared Tasks", list_id=None,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        bridge_result = self._bridge_result(
            {"status": "error", "message": "Update failed: Cannot move reminder to a calendar in a different store"},
            returncode=1,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "resolve_required_list_target_or_die", return_value=target),
            mock.patch.object(self.remctl, "subtask_rows_for_parent_move", return_value=[]),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(
                self.remctl,
                "clone_reminder_tree_to_list_or_die",
                return_value={
                    "status": "updated",
                    "id": 42,
                    "oldId": 1,
                    "list": "Shared Tasks",
                    "objectUUID": "NEW-REMINDER",
                    "subtasksMoved": 0,
                    "method": "clone-delete",
                    "delete": {"status": "deleted"},
                },
            ) as clone_move,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        clone_move.assert_called_once_with(mock.ANY, reminder, target, [])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["id"], 42)
        self.assertEqual(payload["oldId"], 1)
        self.assertEqual(payload["method"], "clone-delete")
        self.assertEqual(payload["subtasksMoved"], 0)
        self.assertTrue(payload["originalDeleted"])

    def test_cmd_edit_does_not_clone_ordinary_move_when_eventkit_access_is_blocked(self):
        reminder = dict(self._FAKE_REMINDER)
        reminder["Z_PK"] = 1
        reminder["ZLIST"] = 7
        args = SimpleNamespace(
            id=1, json=True, title=None, list="Projects", list_id=None,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        bridge_result = self._bridge_result(
            {
                "status": "error",
                "message": "EventKit access error: The operation couldn’t be completed. (Mach error 4099 - unknown error code)",
            },
            returncode=1,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 9, "title": "Projects", "requested": "Projects", "method": "exact", "objectUUID": "LIST-UUID"},
            ),
            mock.patch.object(self.remctl, "subtask_rows_for_parent_move", return_value=[]),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(
                self.remctl,
                "doctor_execution_context",
                return_value={"effective_context": "Codex.app", "host_app": "Codex.app"},
            ),
            mock.patch.object(self.remctl, "clone_reminder_tree_to_list_or_die") as clone_move,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_edit(args)

        clone_move.assert_not_called()
        output = stderr.getvalue()
        self.assertIn("EventKit access error", output)
        self.assertIn("remctl onboard", output)

    def test_cmd_edit_does_not_clone_combined_move_and_title_after_bridge_rejection(self):
        reminder = dict(self._FAKE_REMINDER)
        reminder["Z_PK"] = 1
        reminder["ZLIST"] = 7
        args = SimpleNamespace(
            id=1, json=True, title="New title", list="Projects", list_id=None,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        bridge_result = self._bridge_result(
            {"status": "error", "message": "Update failed: Cannot move reminder to a calendar in a different store"},
            returncode=1,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 9, "title": "Projects", "requested": "Projects", "method": "exact", "objectUUID": "LIST-UUID"},
            ),
            mock.patch.object(self.remctl, "subtask_rows_for_parent_move", return_value=[]),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(self.remctl, "clone_reminder_tree_to_list_or_die") as clone_move,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_edit(args)

        clone_move.assert_not_called()
        self.assertIn("remctl-bridge failed while trying to edit the reminder", stderr.getvalue())

    def test_cmd_edit_does_not_clone_ordinary_move_for_generic_bridge_error(self):
        reminder = dict(self._FAKE_REMINDER)
        reminder["Z_PK"] = 1
        reminder["ZLIST"] = 7
        args = SimpleNamespace(
            id=1, json=True, title=None, list="Projects", list_id=None,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        bridge_result = self._bridge_result(
            {"status": "error", "message": "Reminder not found for id: DEAD-BEEF"},
            returncode=1,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 9, "title": "Projects", "requested": "Projects", "method": "exact", "objectUUID": "LIST-UUID"},
            ),
            mock.patch.object(self.remctl, "subtask_rows_for_parent_move", return_value=[]),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(self.remctl, "clone_reminder_tree_to_list_or_die") as clone_move,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_edit(args)

        clone_move.assert_not_called()
        self.assertIn("Reminder not found", stderr.getvalue())

    def test_clone_reminder_tree_to_list_or_die_allows_zero_child_rows(self):
        reminder = {
            "Z_PK": 1,
            "ZCKIDENTIFIER": "SOURCE-REMINDER",
            "ZTITLE": "Move me",
            "list_name": "Inbox",
        }
        target = {"id": 9, "title": "Projects", "objectUUID": "TARGET-LIST"}
        new_row = {"Z_PK": 42, "ZLIST": 9, "ZCKIDENTIFIER": "NEW-REMINDER"}
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "private_call",
                return_value={"status": "cloned", "newId": "NEW-REMINDER", "children": []},
            ) as private_call,
            mock.patch.object(self.remctl, "wait_for_cloned_reminder_tree", return_value=(new_row, [])),
            mock.patch.object(self.remctl, "delete_reminder_by_identifier_or_die", return_value={"status": "deleted"}),
        ):
            result = self.remctl.clone_reminder_tree_to_list_or_die(object(), reminder, target, [])

        private_call.assert_called_once_with({
            "action": "clone_reminder_tree_to_list",
            "id": "SOURCE-REMINDER",
            "listId": "TARGET-LIST",
            "childIds": [],
        })
        self.assertEqual(result["id"], 42)
        self.assertEqual(result["oldId"], 1)
        self.assertEqual(result["subtasksMoved"], 0)
        # The cloned row is read back before Reminders has filled in its title,
        # so the move result falls back to the source reminder's title.
        self.assertEqual(result["title"], "Move me")

    def test_emit_clone_delete_move_result_json_echoes_title(self):
        args = SimpleNamespace(json=True)
        move_result = {
            "status": "updated",
            "id": 42,
            "oldId": 1,
            "title": "Move me",
            "list": "Projects",
            "objectUUID": "NEW-REMINDER",
            "subtasksMoved": 0,
            "method": "clone-delete",
            "delete": {"status": "deleted"},
        }
        target = {"id": 9, "title": "Projects", "requested": "Projects", "method": "exact"}
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.remctl.emit_clone_delete_move_result(args, move_result, target)

        self.assertEqual(json.loads(stdout.getvalue())["title"], "Move me")

    def test_cmd_edit_resolves_private_section_against_destination_list(self):
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, list="Projects", list_id=None,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=True, private_metadata=False, tags=None,
            grocery=False, section="Launch", section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 9, "title": "Projects", "requested": "Projects", "method": "exact"},
            ),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ),
            mock.patch.object(
                self.remctl,
                "apply_private_changes",
                return_value=[{"status": "updated", "action": "assign_section"}],
            ) as apply_private_changes,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)

        apply_private_changes.assert_called_once_with(
            reminder["ZCKIDENTIFIER"],
            args,
            db=mock.ANY,
            list_pk=9,
        )

    def test_cmd_edit_rejects_list_name_and_list_id_together_before_bridge(self):
        args = SimpleNamespace(
            id=1, json=True, title=None, list="Projects", list_id=9,
            notes=None, priority=None, due=None, url=None, recurrence=None,
            alarm=None, private=False, private_metadata=False, tags=None,
            grocery=False, section=None, section_id=None, new_section=None,
            subtask=None, image=None, flagged=None, urgent=None,
            early_reminder=None, location_title=None, latitude=None,
            longitude=None, radius=100, proximity="arriving", address=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=self._FAKE_REMINDER),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_edit(args)

        bridge_call.assert_not_called()
        self.assertIn("pass either a list name or --list-id", stderr.getvalue())

    def test_cmd_edit_double_taps_when_due_equals_current(self):
        """Ghost-target defense: if CoreData already holds the target dueDate
        (from a pre-2026-04-17 bridge write that never reached iCloud), the
        bridge must fire TWO writes — a one-hour nudge, then the real target —
        so CloudKit actually serializes dueDateComponents in the push."""
        from datetime import datetime
        target = datetime(2026, 4, 20, 9, 0, 0)
        apple_epoch = 978307200
        reminder = dict(self._FAKE_REMINDER)
        reminder["ZDUEDATE"] = target.timestamp() - apple_epoch
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1, json=True, title=None, notes=None, priority=None,
                due="2026-04-20 09:00", url=None, recurrence=None, alarm=None,
            ))
        self.assertEqual(bridge_call_result.call_count, 2)
        nudge_payload, real_payload = (
            bridge_call_result.call_args_list[0].args[0],
            bridge_call_result.call_args_list[1].args[0],
        )
        self.assertEqual(nudge_payload["due"], "2026-04-20T10:00:00")  # +1h
        self.assertEqual(real_payload["due"], "2026-04-20T09:00:00")

    def test_cmd_edit_double_tap_keeps_date_only_due_all_day(self):
        from datetime import datetime

        target = datetime(2026, 6, 21, 0, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder["ZDUEDATE"] = self.remctl.to_ts(target)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1, json=True, title=None, notes=None, priority=None,
                due="2026-06-21", url=None, recurrence=None, alarm=None,
            ))

        self.assertEqual(bridge_call_result.call_count, 2)
        nudge_payload, real_payload = (
            bridge_call_result.call_args_list[0].args[0],
            bridge_call_result.call_args_list[1].args[0],
        )
        self.assertEqual(nudge_payload["due"], "2026-06-22T00:00:00")
        self.assertTrue(nudge_payload["allDay"])
        self.assertEqual(real_payload["due"], "2026-06-21T00:00:00")
        self.assertTrue(real_payload["allDay"])

    def _cmd_edit_private_args(self, **overrides):
        args = {
            "id": 1,
            "json": True,
            "title": None,
            "notes": None,
            "priority": None,
            "due": None,
            "url": None,
            "recurrence": None,
            "alarm": None,
            "private": True,
            "private_metadata": False,
            "tags": "remctl",
            "grocery": False,
            "section": None,
            "section_id": None,
            "new_section": None,
            "subtask": None,
            "image": None,
            "flagged": None,
            "urgent": None,
            "early_reminder": None,
            "location_title": None,
            "latitude": None,
            "longitude": None,
            "radius": 100,
            "proximity": "arriving",
            "address": None,
            "assign": None,
            "unassign": False,
            "list": None,
            "list_id": None,
            "set_tags": None,
            "clear_tags": False,
            "remove_tag": None,
        }
        args.update(overrides)
        return SimpleNamespace(**args)

    def _cmd_edit_reminder_row(self, *, due_ts, ckid="DEAD-BEEF-0000-0000-0000-000000000000", pk=1, list_pk=7, list_name="Emails"):
        db = self._due_window_db()
        try:
            db.execute("DELETE FROM ZREMCDBASELIST")
            db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME) VALUES (?, ?)", (list_pk, list_name))
            db.execute(
                "INSERT INTO ZREMCDREMINDER "
                "(Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
                "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATEDELTAALERTSDATA, "
                "ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZCOMPLETIONDATE, ZCREATIONDATE, "
                "ZPARENTREMINDER, ZLIST, ZICSURL, ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) "
                "VALUES (?, 'test', NULL, 0, 0, 0, 0, NULL, ?, ?, 0, NULL, NULL, NULL, ?, NULL, ?, 1, 0)",
                (pk, due_ts, due_ts, list_pk, ckid),
            )
            row = self.remctl.q_reminder(db, pk)
        finally:
            db.close()
        self.assertIsInstance(row, sqlite3.Row)
        self.assertFalse(hasattr(row, "get"))
        return row

    def test_cmd_edit_rolls_back_nudged_due_when_bridge_only_edit_fails(self):
        from datetime import datetime

        target = datetime(2026, 4, 20, 9, 0, 0)
        due_ts = target.timestamp() - 978307200
        reminder = self._cmd_edit_reminder_row(due_ts=due_ts)
        bridge_responses = [
            self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            self._bridge_result({"status": "error", "message": "Save failed"}, returncode=1),
            self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
        ]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result", side_effect=bridge_responses,
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_edit(self._cmd_edit_private_args(due="2026-04-20 09:00"))

        self.assertEqual(bridge_call_result.call_count, 3)
        nudge_payload, main_payload, rollback_payload = (
            bridge_call_result.call_args_list[i].args[0] for i in range(3)
        )
        self.assertEqual(nudge_payload["due"], "2026-04-20T10:00:00")
        self.assertEqual(main_payload["due"], "2026-04-20T09:00:00")
        self.assertEqual(rollback_payload["due"], "2026-04-20T09:00:00")

    def test_cmd_edit_nudge_rollback_failure_warns_about_wrong_due_date(self):
        from datetime import datetime

        target = datetime(2026, 4, 20, 9, 0, 0)
        due_ts = target.timestamp() - 978307200
        reminder = self._cmd_edit_reminder_row(due_ts=due_ts)
        bridge_responses = [
            self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            self._bridge_result({"status": "error", "message": "Save failed"}, returncode=1),
            self._bridge_result({"status": "error", "message": "Rollback failed"}, returncode=1),
        ]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result", side_effect=bridge_responses,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_edit(self._cmd_edit_private_args(due="2026-04-20 09:00"))

        warning = stderr.getvalue()
        self.assertIn("due date may be wrong", warning)
        self.assertIn(reminder["ZCKIDENTIFIER"], warning)

    def test_cmd_edit_nudge_failure_warns_and_still_attempts_main_update(self):
        from datetime import datetime

        target = datetime(2026, 4, 20, 9, 0, 0)
        reminder = dict(self._FAKE_REMINDER)
        reminder["ZDUEDATE"] = target.timestamp() - 978307200
        bridge_responses = [
            self._bridge_result({"status": "timeout", "message": "timed out"}, returncode=-1),
            self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
        ]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result", side_effect=bridge_responses,
            ) as bridge_call_result,
            mock.patch.object(
                self.remctl,
                "apply_private_changes",
                return_value=[{"status": "updated", "action": "add_private_metadata"}],
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.cmd_edit(self._cmd_edit_private_args(due="2026-04-20 09:00"))

        self.assertEqual(bridge_call_result.call_count, 2)
        nudge_payload, main_payload = (
            bridge_call_result.call_args_list[0].args[0],
            bridge_call_result.call_args_list[1].args[0],
        )
        self.assertEqual(nudge_payload["due"], "2026-04-20T10:00:00")
        self.assertEqual(main_payload["due"], "2026-04-20T09:00:00")
        self.assertIn("due-date sync workaround", stderr.getvalue())

    def test_cmd_edit_rejects_unparseable_due(self):
        """An unparseable due-date string must exit non-zero rather than
        silently dropping the field and letting the rest of the update proceed."""
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=self._FAKE_REMINDER),
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_edit(SimpleNamespace(
                    id=1, json=False, title=None, notes=None, priority=None,
                    due="not a real date", url=None, recurrence=None, alarm=None,
                ))
        bridge_call.assert_not_called()

    def test_cmd_edit_alarm_routes_through_bridge(self):
        """alarm is a bridge-only field; ensure it's routed correctly."""
        # A relative alarm counts back from the due date, so the reminder has one.
        reminder = {**self._FAKE_REMINDER, "ZDUEDATE": 798800400.0}
        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due=None, url=None, recurrence=None, alarm="15m",
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        bridge_call_result.assert_called_once()
        self.assertEqual(bridge_call_result.call_args.args[0]["alarm"], "-15m")

    def test_cmd_edit_falls_back_to_applescript_when_bridge_unavailable(self):
        """If bridge_available() is False, AppleScript path must run."""
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due="2026-04-20 09:00", url=None, recurrence=None, alarm=None,
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        bridge_call.assert_not_called()
        osa_try.assert_called_once()
        self.assertIn("set due date of r to _rdt", osa_try.call_args.args[1])

    def test_cmd_edit_applescript_fallback_changes_the_date_before_other_fields(self):
        # AppleScript has no transaction. The date is the change Reminders can
        # refuse, so it runs first: a refusal then leaves nothing half-applied.
        for due, date_statement in (
            ("2026-04-20 09:00", "set due date of r to _rdt"),
            ("clear", "set due date of r to missing value"),
        ):
            with self.subTest(due=due):
                args = SimpleNamespace(
                    id=1, json=True, title="Renamed", notes="Notes", priority="high",
                    due=due, url=None, recurrence=None, alarm=None,
                )
                with (
                    mock.patch.object(self.remctl, "open_db", return_value=None),
                    mock.patch.object(self.remctl, "q_reminder", return_value=self._FAKE_REMINDER),
                    mock.patch.object(self.remctl, "bridge_available", return_value=False),
                    mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.remctl.cmd_edit(args)
                script = osa_try.call_args.args[1]
                for statement in ('set name of r to "Renamed"', 'set body of r to "Notes"', "set priority of r to 1"):
                    self.assertLess(script.index(date_statement), script.index(statement), statement)

    def test_cmd_edit_refuses_to_clear_the_due_date_of_a_repeating_reminder(self):
        # Reminders will not save a repeating reminder without a due date. The
        # refusal comes before any write, so the title change is not applied alone.
        reminder = {**self._FAKE_REMINDER, "ZDUEDATE": 798800400.0, "recurrence_frequency": 1}
        args = SimpleNamespace(
            id=1, json=True, title="Renamed", notes=None, priority=None,
            due="clear", url=None, recurrence=None, alarm=None,
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result") as bridge_call_result,
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as raised:
                self.remctl.cmd_edit(args)
        self.assertEqual(raised.exception.code, 1)
        bridge_call_result.assert_not_called()
        osa_try.assert_not_called()
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["code"], "repeating_reminder_requires_due_date")
        self.assertEqual(payload["id"], 1)

        one_off = {**reminder, "recurrence_frequency": None}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=one_off),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": reminder["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        self.assertIsNone(bridge_call_result.call_args.args[0]["due"])

    def test_cmd_edit_failure_names_the_bridge_error(self):
        args = SimpleNamespace(
            id=1, json=True, title="Renamed", notes=None, priority=None,
            due=None, url=None, recurrence=None, alarm=None,
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=self._FAKE_REMINDER),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "error", "message": "The reminder could not be saved."}, returncode=1),
            ),
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_edit(args)
        self.assertIn(
            "Identifier-based writes failed (remctl-bridge: The reminder could not be saved). Refusing unsafe",
            stderr.getvalue(),
        )

    def test_cmd_edit_relative_alarm_requires_a_due_date(self):
        # A relative alarm counts back from the due date; saved without one, it never fires.
        for label, reminder, due in (
            ("no due date", self._FAKE_REMINDER, None),
            ("due date cleared", {**self._FAKE_REMINDER, "ZDUEDATE": 798800400.0}, "clear"),
        ):
            with self.subTest(label):
                args = SimpleNamespace(
                    id=1, json=True, title=None, notes=None, priority=None,
                    due=due, url=None, recurrence=None, alarm="15m",
                )
                stderr = io.StringIO()
                with (
                    mock.patch.object(self.remctl, "open_db", return_value=None),
                    mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
                    mock.patch.object(self.remctl, "bridge_available", return_value=True),
                    mock.patch.object(self.remctl, "bridge_call_result") as bridge_call_result,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(stderr),
                ):
                    with self.assertRaises(SystemExit):
                        self.remctl.cmd_edit(args)
                bridge_call_result.assert_not_called()
                self.assertEqual(json.loads(stderr.getvalue())["code"], "relative_alarm_requires_due_date")

        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due="2026-04-20 09:00", url=None, recurrence=None, alarm="15m",
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=self._FAKE_REMINDER),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call_result",
                return_value=self._bridge_result({"status": "updated", "id": self._FAKE_REMINDER["ZCKIDENTIFIER"]}),
            ) as bridge_call_result,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        payload = bridge_call_result.call_args.args[0]
        self.assertEqual((payload["due"], payload["alarm"]), ("2026-04-20T09:00:00", "-15m"))

    def _assert_refuses_unsafe_fallback(self, cmd_name, args):
        reminder = dict(self._FAKE_REMINDER)
        reminder["ZCKIDENTIFIER"] = None
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_try,
            mock.patch.object(self.remctl, "osa") as osa,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                getattr(self.remctl, cmd_name)(args)
        bridge_call.assert_not_called()
        osa_try.assert_not_called()
        osa.assert_not_called()

    def test_cmd_done_refuses_title_based_fallback_without_identifier(self):
        self._assert_refuses_unsafe_fallback("cmd_done", SimpleNamespace(id=1, json=True))

    def test_cmd_delete_refuses_title_based_fallback_without_identifier(self):
        self._assert_refuses_unsafe_fallback("cmd_delete", SimpleNamespace(id=1, json=True, force=True))

    def test_cmd_flag_refuses_title_based_fallback_without_identifier(self):
        self._assert_refuses_unsafe_fallback("cmd_flag", SimpleNamespace(id=1, json=True))

    def test_cmd_edit_refuses_title_based_fallback_without_identifier(self):
        self._assert_refuses_unsafe_fallback(
            "cmd_edit",
            SimpleNamespace(
                id=1,
                json=True,
                title="Retitle",
                notes=None,
                priority=None,
                due=None,
                url=None,
                recurrence=None,
                alarm=None,
            ),
        )

    def test_run_handler_with_fallback_exits_when_database_unavailable(self):
        args = SimpleNamespace(cmd="today", json=True, no_overdue=False, format="json")
        handler = mock.Mock(side_effect=self.remctl.RemindersDBUnavailable("db unavailable"))
        with (
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.run_handler_with_fallback(args, handler)
        self.assertIn("db unavailable", stderr.getvalue())

    def test_run_handler_with_fallback_suggests_eventkit_without_switching(self):
        args = SimpleNamespace(cmd="today", json=False, no_overdue=False, format="plain", via_eventkit=False)
        handler = mock.Mock(side_effect=self.remctl.RemindersDBUnavailable("db unavailable"))
        with (
            mock.patch.dict(
                "os.environ",
                {"REMCTL_CAPABILITY_HOST_ACTIVE": ""},
                clear=False,
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.run_handler_with_fallback(args, handler)

        self.assertEqual(raised.exception.code, 1)
        handler.assert_called_once_with(args)
        output = stderr.getvalue()
        self.assertIn("--via-eventkit", output)
        self.assertIn("when Full Disk Access is unavailable", output)
        self.assertIn("not full fidelity", output)
        self.assertIn("does not return RemCTL numeric ids", output)

    def test_run_handler_with_fallback_names_host_inside_sealed_child(self):
        args = SimpleNamespace(
            cmd="today",
            json=False,
            no_overdue=False,
            format="plain",
            via_eventkit=False,
        )
        handler = mock.Mock(
            side_effect=self.remctl.RemindersDBUnavailable("host database unavailable")
        )
        with (
            mock.patch.dict(
                "os.environ",
                {"REMCTL_CAPABILITY_HOST_ACTIVE": "1"},
                clear=False,
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.run_handler_with_fallback(args, handler)

        self.assertEqual(raised.exception.code, 1)
        output = stderr.getvalue()
        self.assertIn("signed Capability Host lacks Full Disk Access", output)
        self.assertIn("--via-eventkit", output)
        self.assertNotIn("when Full Disk Access is unavailable", output)

    def test_fmt_supports_sqlite_row_without_dict_get(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 42 AS Z_PK, 'Test reminder' AS ZTITLE, 0 AS ZCOMPLETED, "
            "0 AS ZFLAGGED, 5 AS ZPRIORITY, NULL AS ZDUEDATE, "
            "'Reminders' AS list_name, NULL AS ZNOTES, NULL AS ZICSURL"
        ).fetchone()
        formatted = self.remctl.fmt(row, db=None, verbose=False)
        table = self.remctl.reminders_to_table_data([row], db=None)
        conn.close()
        self.assertIn("Test reminder", formatted)
        self.assertEqual(self.remctl._strip_ansi(table[0]["id"]), "#42")
        self.assertEqual(table[0]["title"], "Test reminder")

    def test_completed_table_mode_uses_completion_date_column(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        due = self.remctl.to_ts(self.remctl.datetime(2026, 4, 1, 12, 0))
        completed = self.remctl.to_ts(self.remctl.datetime(2026, 5, 27, 9, 30))
        row = conn.execute(
            "SELECT 42 AS Z_PK, 'Completed item' AS ZTITLE, 1 AS ZCOMPLETED, "
            "0 AS ZFLAGGED, 0 AS ZPRIORITY, ? AS ZDUEDATE, NULL AS ZDISPLAYDATEDATE, "
            "0 AS ZALLDAY, ? AS ZCOMPLETIONDATE, 'Work' AS list_name, NULL AS ZNOTES, "
            "NULL AS ZICSURL",
            (due, completed),
        ).fetchone()
        table_data = self.remctl.reminders_to_table_data([row], db=None)
        rendered = self.remctl.fmt_table(table_data, date_mode="completed")
        conn.close()

        plain = self.remctl._strip_ansi(rendered)
        self.assertIn("Completed", plain)
        self.assertIn("2026-05-27 09:30", plain)
        self.assertNotIn("Overdue", plain)

    def test_table_width_accounts_for_emoji_cells(self):
        rows = [{
            "id": "#18396",
            "title": "📅 Post 1.0.5",
            "list": "Editorial",
            "due": "Overdue 10d",
            "repeat": "",
            "pri": "",
        }]
        rendered = self.remctl.fmt_table(rows, max_width=100)
        widths = {self.remctl._visible_len(line) for line in rendered.splitlines()}
        self.assertEqual(len(widths), 1)

    def test_visible_len_preserves_unicode_and_control_widths(self):
        cases = [
            ("", 0),
            ("ASCII title", 11),
            ("\033[31mRed\033[0m", 3),
            ("a\t\n\x00\x7fb", 2),
            ("cafe\u0301", 4),
            ("日本語", 6),
            ("👨\u200d👩\u200d👧", 6),
            ("❤\ufe0f", 1),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.remctl._visible_len(text), expected)

    def test_group_table_mode_prints_child_list_and_section_tables(self):
        children = [
            {"Z_PK": 2, "ZNAME": "Editorial", "ZCKIDENTIFIER": "LIST-2", "ZISGROUP": 0},
            {"Z_PK": 3, "ZNAME": "Podcasts", "ZCKIDENTIFIER": "LIST-3", "ZISGROUP": 0},
        ]
        completed = self.remctl.to_ts(self.remctl.datetime(2026, 5, 27, 9, 30))
        items = [
            {
                "Z_PK": 42,
                "ZTITLE": "Publish review",
                "ZCOMPLETED": 1,
                "ZFLAGGED": 0,
                "ZPRIORITY": 0,
                "ZDUEDATE": None,
                "ZDISPLAYDATEDATE": None,
                "ZALLDAY": 0,
                "ZCOMPLETIONDATE": completed,
                "ZLIST": 2,
                "ZCKIDENTIFIER": "REM-1",
                "list_name": "Editorial",
            },
            {
                "Z_PK": 43,
                "ZTITLE": "Record episode",
                "ZCOMPLETED": 1,
                "ZFLAGGED": 0,
                "ZPRIORITY": 0,
                "ZDUEDATE": None,
                "ZDISPLAYDATEDATE": None,
                "ZALLDAY": 0,
                "ZCOMPLETIONDATE": completed,
                "ZLIST": 3,
                "ZCKIDENTIFIER": "REM-2",
                "list_name": "Podcasts",
            },
        ]

        def sections_for(_db, list_id):
            return [{"ZDISPLAYNAME": "MacStories"}] if list_id == 2 else []

        with (
            mock.patch.object(self.remctl, "q_sections", side_effect=sections_for),
            mock.patch.object(self.remctl, "color_list_name", side_effect=lambda name, db=None: name),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.print_group_reminder_tables(
                {"id": 1, "title": "Writing"},
                children,
                items,
                db=object(),
                memberships_by_list={2: {"REM-1": "MacStories"}, 3: {}},
                args=SimpleNamespace(completed=True),
            )

        plain = self.remctl._strip_ansi(stdout.getvalue())
        self.assertIn("Writing (group):", plain)
        self.assertIn("Editorial:", plain)
        self.assertIn("[MacStories]", plain)
        self.assertIn("Podcasts:", plain)
        self.assertIn("Completed", plain)
        self.assertIn("2026-05-27 09:30", plain)
        self.assertNotIn("│ List ", plain)

    def test_human_reminder_output_sanitizes_terminal_controls(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 42 AS Z_PK, ? AS ZTITLE, 0 AS ZCOMPLETED, "
            "0 AS ZFLAGGED, 0 AS ZPRIORITY, NULL AS ZDUEDATE, "
            "? AS list_name, ? AS ZNOTES, ? AS ZICSURL",
            (
                "\033]52;c;cHduZWQ=\aClipboard",
                "Work\033[31m",
                "line one\nline two\033]8;;https://evil.example\a",
                "https://example.com/\033]52;c;cHduZWQ=\a",
            ),
        ).fetchone()
        formatted = self.remctl.fmt(row, db=None, verbose=True)
        table = self.remctl.reminders_to_table_data([row], db=None)
        conn.close()

        self.assertNotIn("\033]52", formatted)
        self.assertNotIn("\033]8", formatted)
        self.assertNotIn("\a", formatted)
        self.assertNotIn("\033]52", table[0]["title"])
        self.assertIn("Clipboard", formatted)

    def test_flagged_and_urgent_reminders_show_distinct_symbols_and_serialize(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 42 AS Z_PK, 'Urgent flagged' AS ZTITLE, NULL AS ZNOTES, "
            "0 AS ZCOMPLETED, 1 AS ZFLAGGED, 0 AS ZPRIORITY, "
            "1 AS ZISURGENTSTATEENABLEDFORCURRENTUSER, NULL AS ZDUEDATE, "
            "NULL AS ZALLDAY, NULL AS ZCOMPLETIONDATE, NULL AS ZCREATIONDATE, "
            "NULL AS ZPARENTREMINDER, 1 AS ZLIST, NULL AS ZICSURL, "
            "'ABC' AS ZCKIDENTIFIER, 'Work' AS list_name, "
            "NULL AS recurrence_frequency, NULL AS recurrence_interval, "
            "NULL AS recurrence_count, NULL AS recurrence_end_date, "
            "NULL AS recurrence_days_of_week, NULL AS recurrence_days_of_month, "
            "NULL AS recurrence_months_of_year, NULL AS recurrence_days_of_year, "
            "NULL AS recurrence_weeks_of_year, NULL AS recurrence_set_positions"
        ).fetchone()
        formatted = self.remctl._strip_ansi(self.remctl.fmt(row, db=None, verbose=True))
        table = self.remctl.reminders_to_table_data([row], db=None)
        payload = self.remctl.to_dict(row, db=None)
        conn.close()
        self.assertIn("⏰", formatted)
        self.assertIn("⚑", formatted)
        self.assertIn("⏰ ⚑ Urgent flagged", self.remctl._strip_ansi(table[0]["title"]))
        self.assertTrue(payload["urgent"])
        self.assertTrue(payload["flagged"])

    def _due_window_db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE ZREMCDREMINDER ("
            "Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZNOTES TEXT, ZCOMPLETED INTEGER, "
            "ZFLAGGED INTEGER, ZPRIORITY INTEGER, ZISURGENTSTATEENABLEDFORCURRENTUSER INTEGER, "
            "ZDUEDATEDELTAALERTSDATA TEXT, ZDUEDATE REAL, ZDISPLAYDATEDATE REAL, ZALLDAY INTEGER, "
            "ZCOMPLETIONDATE REAL, ZCREATIONDATE REAL, ZPARENTREMINDER INTEGER, ZLIST INTEGER, "
            "ZICSURL TEXT, ZCKIDENTIFIER TEXT, ZACCOUNT INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        db.execute("CREATE TABLE ZREMCDBASELIST (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT)")
        db.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, Z_ENT INTEGER, ZREMINDER4 INTEGER, ZMARKEDFORDELETION INTEGER, "
            "ZFREQUENCY INTEGER, ZINTERVAL INTEGER, ZOCCURRENCECOUNT INTEGER, ZENDDATE REAL, "
            "ZDAYSOFTHEWEEK BLOB, ZDAYSOFTHEMONTH BLOB, ZMONTHSOFTHEYEAR BLOB, "
            "ZDAYSOFTHEYEAR BLOB, ZWEEKSOFTHEYEAR BLOB, ZSETPOSITIONS BLOB)"
        )
        db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME) VALUES (1, 'Inbox')")
        return db

    def _insert_due_reminder(self, db, pk, title, *, due_ts, display_ts, all_day):
        db.execute(
            "INSERT INTO ZREMCDREMINDER "
            "(Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
            "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATEDELTAALERTSDATA, "
            "ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, ZCKIDENTIFIER, "
            "ZACCOUNT, ZMARKEDFORDELETION) "
            "VALUES (?, ?, NULL, 0, 0, 0, 0, NULL, ?, ?, ?, 1, ?, 1, 0)",
            (pk, title, due_ts, display_ts, 1 if all_day else 0, f"CK-{pk}"),
        )

    def _insert_lookup_reminder(self, db, pk, title, *, list_pk=1, ckid=None, creation_ts=None):
        if creation_ts is None:
            creation_ts = self.remctl.to_ts(self.remctl.datetime.now())
        db.execute(
            "INSERT INTO ZREMCDREMINDER "
            "(Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
            "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATEDELTAALERTSDATA, "
            "ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZCOMPLETIONDATE, ZCREATIONDATE, "
            "ZPARENTREMINDER, ZLIST, ZICSURL, ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) "
            "VALUES (?, ?, NULL, 0, 0, 0, 0, NULL, NULL, NULL, 0, NULL, ?, NULL, ?, NULL, ?, 1, 0)",
            (pk, title, creation_ts, list_pk, ckid or f"CK-{pk}"),
        )

    def test_all_day_reminders_bucket_and_render_by_display_date_west_of_utc(self):
        from datetime import datetime, timedelta, timezone

        prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        self.remctl._REMINDER_COLUMN_CACHE.clear()
        db = self._due_window_db()
        try:
            apple_epoch = self.remctl.APPLE_EPOCH

            def utc_midnight_ts(d):
                dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                return dt.timestamp() - apple_epoch

            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = today + timedelta(days=1)

            self._insert_due_reminder(
                db, 1, "AllDay Today",
                due_ts=utc_midnight_ts(today),
                display_ts=self.remctl.to_ts(today),
                all_day=True,
            )
            self._insert_due_reminder(
                db, 2, "AllDay Tomorrow",
                due_ts=utc_midnight_ts(tomorrow),
                display_ts=self.remctl.to_ts(tomorrow),
                all_day=True,
            )
            timed_due = self.remctl.to_ts(today.replace(hour=9))
            self._insert_due_reminder(
                db, 3, "Timed Today",
                due_ts=timed_due,
                display_ts=timed_due,
                all_day=False,
            )

            raw_tomorrow_due = db.execute(
                "SELECT ZDUEDATE FROM ZREMCDREMINDER WHERE Z_PK = 2"
            ).fetchone()["ZDUEDATE"]
            self.assertEqual(self.remctl.ts(raw_tomorrow_due).date(), today.date())

            due_today = self.remctl.q_due_today(db, include_overdue=True)
            self.assertEqual({row["ZTITLE"] for row in due_today}, {"AllDay Today", "Timed Today"})
            self.assertEqual(self.remctl.q_overdue(db), [])

            upcoming = {row["ZTITLE"]: row for row in self.remctl.q_upcoming(db, days=7)}
            tomorrow_item = upcoming["AllDay Tomorrow"]
            effective = self.remctl.row_effective_due(tomorrow_item)
            self.assertEqual(self.remctl.ts(effective).date(), tomorrow.date())

            rendered = self.remctl._strip_ansi(self.remctl.fmt(tomorrow_item))
            self.assertIn("📅 AllDay Tomorrow", rendered)
            self.assertIn("(tomorrow)", rendered)
            self.assertNotIn("20:00", rendered)

            table = self.remctl.reminders_to_table_data([tomorrow_item], db=None)[0]
            self.assertIn("📅 AllDay Tomorrow", self.remctl._strip_ansi(table["title"]))
            self.assertEqual(table["due"], "Tomorrow")
        finally:
            db.close()
            self.remctl._REMINDER_COLUMN_CACHE.clear()
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()

    def test_stats_overdue_count_matches_q_overdue(self):
        from datetime import datetime, timedelta

        db = self._due_window_db()
        try:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday = today - timedelta(days=1)

            timed_today = self.remctl.to_ts(today.replace(hour=9))
            self._insert_due_reminder(
                db, 1, "Timed Earlier Today",
                due_ts=timed_today,
                display_ts=timed_today,
                all_day=False,
            )
            self._insert_due_reminder(
                db, 2, "AllDay Today",
                due_ts=self.remctl.to_ts(today),
                display_ts=self.remctl.to_ts(today),
                all_day=True,
            )
            self._insert_due_reminder(
                db, 3, "Due Yesterday",
                due_ts=self.remctl.to_ts(yesterday.replace(hour=9)),
                display_ts=self.remctl.to_ts(yesterday.replace(hour=9)),
                all_day=False,
            )

            overdue_rows = self.remctl.q_overdue(db)
            self.assertEqual([row["ZTITLE"] for row in overdue_rows], ["Due Yesterday"])

            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "q_lists", return_value=[]),
                mock.patch.object(self.remctl, "q_sections", return_value=[]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_stats(SimpleNamespace(json=True))

            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["overdue"], len(overdue_rows))
        finally:
            db.close()

    def test_timed_reminders_bucket_by_the_date_reminders_shows(self):
        from datetime import datetime, timedelta

        db = self._due_window_db()
        try:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday = today - timedelta(days=1)
            # Due in two days, but its alarm stayed at yesterday 15:00, so
            # Reminders lists it in Today as overdue.
            self._insert_due_reminder(
                db, 1, "Moved Due Date",
                due_ts=self.remctl.to_ts((today + timedelta(days=2)).replace(hour=15)),
                display_ts=self.remctl.to_ts(yesterday.replace(hour=15)),
                all_day=False,
            )
            # Due yesterday, but its alarm is tomorrow at 09:00, which is
            # where Reminders lists it.
            self._insert_due_reminder(
                db, 2, "Alarm After Due",
                due_ts=self.remctl.to_ts(yesterday.replace(hour=9)),
                display_ts=self.remctl.to_ts((today + timedelta(days=1)).replace(hour=9)),
                all_day=False,
            )

            def titles(rows):
                return [row["ZTITLE"] for row in rows]

            self.assertEqual(titles(self.remctl.q_due_today(db, include_overdue=True)), ["Moved Due Date"])
            self.assertEqual(titles(self.remctl.q_due_today(db, include_overdue=False)), [])
            self.assertEqual(titles(self.remctl.q_overdue(db)), ["Moved Due Date"])
            self.assertEqual(titles(self.remctl.q_upcoming(db, days=7)), ["Alarm After Due"])

            moved = self.remctl.q_overdue(db)[0]
            self.assertIn("(overdue", self.remctl._strip_ansi(self.remctl.fmt(moved)))

            args = SimpleNamespace(json=False, no_overdue=False, format=None, via_eventkit=False, verbose=False)
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "q_hashtags", return_value=[]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_today(args)
            plain = self.remctl._strip_ansi(stdout.getvalue())
            self.assertIn("Overdue (1):", plain)
            self.assertIn("Moved Due Date", plain)
            self.assertNotIn("Due Today", plain)

            payload = self.remctl.to_dict(moved, db=None)
            self.assertEqual(payload["dueDate"][:10], (today + timedelta(days=2)).strftime("%Y-%m-%d"))
            self.assertEqual(payload["displayDate"], yesterday.replace(hour=15).isoformat())
        finally:
            db.close()

    def test_orphaned_reminder_excluded_from_read_queries(self):
        db = self._due_window_db()
        try:
            db.execute(
                "INSERT INTO ZREMCDREMINDER "
                "(Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
                "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATEDELTAALERTSDATA, "
                "ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, ZCKIDENTIFIER, "
                "ZACCOUNT, ZMARKEDFORDELETION) "
                "VALUES (99, 'Orphan', NULL, 0, 0, 0, 0, NULL, NULL, NULL, 0, 999, 'ORPHAN', 1, 0)"
            )

            self.assertEqual(self.remctl.q_reminders(db), [])
            self.assertIsNone(self.remctl.q_reminder(db, 99))
            self.assertIsNone(self.remctl.q_reminder_by_identifier(db, "ORPHAN"))
        finally:
            db.close()

    def test_cmd_info_json_includes_private_rich_link_url(self):
        reminder = {
            "Z_PK": 42,
            "ZTITLE": "Final README",
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATE": None,
            "ZALLDAY": None,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": "ABC",
            "list_name": "Projects",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
        }
        with (
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "q_reminders", return_value=[]),
            mock.patch.object(self.remctl, "q_attachments", return_value=[]),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
            mock.patch.object(self.remctl, "q_hashtags", return_value=[]),
            mock.patch.object(self.remctl, "q_section_memberships", return_value={"ABC": "Playground"}),
            mock.patch.object(self.remctl, "q_rich_link", return_value="https://example.com/shortcuts-playground-plugin"),
            mock.patch.object(self.remctl, "q_subtask_count", return_value=0),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_info(SimpleNamespace(id=42, json=True))

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["url"], "https://example.com/shortcuts-playground-plugin")
        self.assertEqual(payload["section"], "Playground")

    def test_info_json_keeps_due_date_separate_from_display_alarm_date(self):
        from datetime import datetime

        row = {
            "Z_PK": 42,
            "ZTITLE": "Review",
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None,
            "ZDUEDATE": self.remctl.to_ts(datetime(2026, 5, 23, 10, 0, 0)),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(datetime(2026, 5, 23, 9, 45, 0)),
            "ZALLDAY": 0,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": "ABC",
            "list_name": "Work",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
        }

        payload = self.remctl.to_dict(row, db=None)

        self.assertEqual(payload["dueDate"], "2026-05-23T10:00:00")
        self.assertEqual(payload["displayDate"], "2026-05-23T09:45:00")
        self.assertFalse(payload["allDay"])

    def test_alarm_rows_serialize_relative_absolute_and_location_triggers(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, Z_ENT INTEGER, ZREMINDER INTEGER, ZTRIGGER INTEGER, "
            "ZMARKEDFORDELETION INTEGER, ZTIMEINTERVAL REAL, ZDATECOMPONENTSDATA BLOB, "
            "ZTITLE TEXT, ZLATITUDE REAL, ZLONGITUDE REAL, ZRADIUS REAL, "
            "ZADDRESS TEXT, ZPROXIMITY INTEGER)"
        )
        date_components = json.dumps({
            "year": 2026,
            "month": 5,
            "day": 23,
            "hour": 10,
            "minute": 30,
            "second": 0,
            "timeZone": {"identifier": "Europe/Rome"},
        }).encode()
        conn.executemany(
            "INSERT INTO ZREMCDOBJECT VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 15, 42, 2, 0, None, None, None, None, None, None, None, None),
                (2, 19, None, None, 0, -900, None, None, None, None, None, None, None),
                (3, 15, 42, 4, 0, None, None, None, None, None, None, None, None),
                (4, 17, None, None, 0, None, date_components, None, None, None, None, None, None),
                (5, 15, 42, 6, 0, None, None, None, None, None, None, None, None),
                (6, 18, None, None, 0, None, None, "Apple Park", 37.3349, -122.009, 200, "One Apple Park Way", 1),
            ],
        )

        alarms = self.remctl.alarm_rows_to_json(self.remctl.q_alarms(conn, 42))
        conn.close()

        self.assertEqual(alarms[0]["type"], "relative")
        self.assertEqual(alarms[0]["relativeOffset"], -900)
        self.assertEqual(alarms[0]["relativeOffsetMinutes"], -15)
        self.assertEqual(alarms[0]["label"], "15 minutes before due date")
        self.assertEqual(alarms[1]["type"], "absolute")
        self.assertEqual(alarms[1]["date"], "2026-05-23T10:30:00")
        self.assertEqual(alarms[1]["timeZone"], "Europe/Rome")
        self.assertEqual(alarms[2]["type"], "location")
        self.assertEqual(alarms[2]["location"]["title"], "Apple Park")
        self.assertEqual(alarms[2]["location"]["proximity"], "arriving")

    def test_cmd_info_json_hydrates_subtask_attachments_and_alarms(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE ZREMCDREMINDER ("
            "Z_PK INTEGER, ZPARENTREMINDER INTEGER, ZMARKEDFORDELETION INTEGER, ZCOMPLETED INTEGER)"
        )
        db.execute("CREATE TABLE ZREMCDHASHTAGLABEL (Z_PK INTEGER, ZNAME TEXT)")
        db.execute(
            "CREATE TABLE ZREMCDSAVEDATTACHMENT ("
            "ZFILENAME TEXT, ZUTI TEXT, ZATTACHMENTTYPERAWVALUE TEXT, ZSHA512SUM TEXT, "
            "ZREMINDER INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        db.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, Z_ENT INTEGER, ZREMINDER INTEGER, ZTRIGGER INTEGER, "
            "ZREMINDER2 INTEGER, ZREMINDER3 INTEGER, ZHASHTAGLABEL INTEGER, ZMARKEDFORDELETION INTEGER, "
            "ZFILENAME TEXT, ZUTI TEXT, ZWIDTH INTEGER, ZHEIGHT INTEGER, ZSHA512SUM TEXT, ZURL TEXT, "
            "ZTIMEINTERVAL REAL, ZDATECOMPONENTSDATA BLOB, ZTITLE TEXT, ZLATITUDE REAL, "
            "ZLONGITUDE REAL, ZRADIUS REAL, ZADDRESS TEXT, ZPROXIMITY INTEGER)"
        )
        db.executemany(
            "INSERT INTO ZREMCDOBJECT VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 25, None, None, 43, None, None, 0, "child.png", "public.png", 100, 100, None, None, None, None, None, None, None, None, None, None),
                (2, 15, 43, 3, None, None, None, 0, None, None, None, None, None, None, None, None, None, None, None, None, None, None),
                (3, 19, None, None, None, None, None, 0, None, None, None, None, None, None, -600, None, None, None, None, None, None, None),
            ],
        )
        parent = {
            "Z_PK": 42,
            "ZTITLE": "Parent",
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None,
            "ZDUEDATE": None,
            "ZDISPLAYDATEDATE": None,
            "ZALLDAY": 0,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": "PARENT",
            "list_name": "Projects",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
        }
        child = dict(parent, Z_PK=43, ZTITLE="Child", ZPARENTREMINDER=42, ZCKIDENTIFIER="CHILD")
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "q_reminder", return_value=parent),
                mock.patch.object(self.remctl, "q_reminders", return_value=[child]),
                mock.patch.object(self.remctl, "q_section_memberships", return_value={}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_info(SimpleNamespace(id=42, json=True))
        finally:
            db.close()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["subtasks"][0]["attachments"][0]["filename"], "child.png")
        self.assertEqual(payload["subtasks"][0]["alarms"][0]["relativeOffset"], -600)

    def test_q_attachments_reads_current_image_attachment_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE ZREMCDSAVEDATTACHMENT ("
            "ZFILENAME TEXT, ZUTI TEXT, ZATTACHMENTTYPERAWVALUE TEXT, ZSHA512SUM TEXT, "
            "ZREMINDER INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        conn.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "ZFILENAME TEXT, ZUTI TEXT, ZWIDTH INTEGER, ZHEIGHT INTEGER, ZSHA512SUM TEXT, "
            "ZREMINDER2 INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        conn.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('first.png', 'public.png', 512, 512, NULL, 42, 0), "
            "('second.png', 'public.png', 512, 512, NULL, 42, 0), "
            "('deleted.png', 'public.png', 512, 512, NULL, 42, 1), "
            "(NULL, 'public.url', NULL, NULL, NULL, 42, 0)"
        )

        attachments = [dict(row) for row in self.remctl.q_attachments(conn, 42)]
        conn.close()

        self.assertEqual(
            attachments,
            [
                {"ZFILENAME": "first.png", "ZUTI": "public.png", "ZATTACHMENTTYPERAWVALUE": "image",
                 "ZSHA512SUM": None, "ZWIDTH": 512, "ZHEIGHT": 512},
                {"ZFILENAME": "second.png", "ZUTI": "public.png", "ZATTACHMENTTYPERAWVALUE": "image",
                 "ZSHA512SUM": None, "ZWIDTH": 512, "ZHEIGHT": 512},
            ],
        )

    def test_reminder_id_uses_list_color_when_database_colors_are_available(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 42 AS Z_PK, 'Test reminder' AS ZTITLE, 0 AS ZCOMPLETED, "
            "0 AS ZFLAGGED, 0 AS ZPRIORITY, NULL AS ZDUEDATE, "
            "'Work' AS list_name, NULL AS ZNOTES, NULL AS ZICSURL"
        ).fetchone()
        fake_db = object()
        with mock.patch.object(self.remctl, "get_list_colors", return_value={"Work": (1, 2, 3)}):
            formatted = self.remctl.fmt(row, db=fake_db, verbose=False, _sc={42: 0}, _ht={42: []})
            table = self.remctl.reminders_to_table_data([row], db=fake_db)
        conn.close()
        self.assertIn("\033[38;2;1;2;3m#42\033[0m", formatted)
        self.assertEqual(table[0]["id"], "\033[38;2;1;2;3m#42\033[0m")

    def test_recurring_reminders_show_badge_and_serialize_recurrence(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        days_blob = json.dumps([
            {"weekNumber": 0, "dayOfTheWeek": 2},
            {"weekNumber": 0, "dayOfTheWeek": 4},
        ]).encode()
        row = conn.execute(
            "SELECT 42 AS Z_PK, 'Standup' AS ZTITLE, NULL AS ZNOTES, "
            "0 AS ZCOMPLETED, 0 AS ZFLAGGED, 0 AS ZPRIORITY, "
            "NULL AS ZDUEDATE, NULL AS ZALLDAY, NULL AS ZCOMPLETIONDATE, "
            "NULL AS ZCREATIONDATE, NULL AS ZPARENTREMINDER, 1 AS ZLIST, "
            "NULL AS ZICSURL, 'ABC' AS ZCKIDENTIFIER, 'Work' AS list_name, "
            "1 AS recurrence_frequency, 1 AS recurrence_interval, "
            "0 AS recurrence_count, NULL AS recurrence_end_date, "
            "? AS recurrence_days_of_week, NULL AS recurrence_days_of_month, "
            "NULL AS recurrence_months_of_year, NULL AS recurrence_days_of_year, "
            "NULL AS recurrence_weeks_of_year, NULL AS recurrence_set_positions",
            (days_blob,),
        ).fetchone()
        formatted = self.remctl.fmt(row, db=None, verbose=True)
        table = self.remctl.reminders_to_table_data([row], db=None)
        payload = self.remctl.to_dict(row, db=None)
        conn.close()

        self.assertIn("weekly Mon, Wed", self.remctl._strip_ansi(formatted))
        self.assertEqual(self.remctl._strip_ansi(table[0]["repeat"]), "weekly Mon, Wed")
        self.assertEqual(
            payload["recurrence"],
            {
                "frequency": "weekly",
                "interval": 1,
                "daysOfWeekDetailed": [
                    {"weekNumber": 0, "dayOfTheWeek": 2},
                    {"weekNumber": 0, "dayOfTheWeek": 4},
                ],
                "daysOfWeek": [2, 4],
            },
        )

    def test_early_reminder_due_date_delta_serializes_from_private_blob(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        delta_blob = json.dumps({
            "dueDateDeltaAlerts": [{
                "dueDateDeltaUnit": 0,
                "dueDateDeltaCount": -15,
                "identifier": "DELTA-1",
                "creationDate": 800786817.653489,
                "minimumSupportedAppVersion": 0,
            }]
        }).encode()
        row = conn.execute(
            "SELECT 42 AS Z_PK, 'Taxes' AS ZTITLE, NULL AS ZNOTES, "
            "0 AS ZCOMPLETED, 0 AS ZFLAGGED, 0 AS ZPRIORITY, 0 AS ZISURGENTSTATEENABLEDFORCURRENTUSER, "
            "? AS ZDUEDATEDELTAALERTSDATA, 800798400 AS ZDUEDATE, NULL AS ZALLDAY, NULL AS ZCOMPLETIONDATE, "
            "NULL AS ZCREATIONDATE, NULL AS ZPARENTREMINDER, 1 AS ZLIST, "
            "NULL AS ZICSURL, 'ABC' AS ZCKIDENTIFIER, 'Work' AS list_name, "
            "NULL AS recurrence_frequency, NULL AS recurrence_interval, "
            "NULL AS recurrence_count, NULL AS recurrence_end_date, "
            "NULL AS recurrence_days_of_week, NULL AS recurrence_days_of_month, "
            "NULL AS recurrence_months_of_year, NULL AS recurrence_days_of_year, "
            "NULL AS recurrence_weeks_of_year, NULL AS recurrence_set_positions",
            (delta_blob,),
        ).fetchone()
        formatted = self.remctl.fmt(row, db=None, verbose=True)
        payload = self.remctl.to_dict(row, db=None)
        conn.close()

        self.assertIn("Early: 15 minutes before", self.remctl._strip_ansi(formatted))
        self.assertEqual(payload["earlyReminder"]["label"], "15 minutes before")
        self.assertEqual(payload["earlyReminder"]["unitCode"], 0)
        self.assertEqual(payload["earlyReminder"]["count"], -15)
        self.assertEqual(payload["earlyReminders"][0]["identifier"], "DELTA-1")

    def test_early_reminder_serializes_from_delta_alert_table_when_blob_missing(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        reminder_uuid = uuid.UUID("339DDE31-8F53-46BD-8469-CF42CA5C8CAB")
        alert_uuid = uuid.UUID("2C0EDC64-6294-488A-814F-46B44C8ABBD3")
        conn.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, ZURL TEXT, ZREMINDER2 INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        conn.execute(
            "CREATE TABLE ZREMCDDUEDATEDELTAALERT ("
            "Z_PK INTEGER, ZIDENTIFIER BLOB, ZREMINDERIDENTIFIER BLOB, "
            "ZDUEDATEDELTAUNIT INTEGER, ZDUEDATEDELTACOUNT INTEGER, "
            "ZMINIMUMSUPPORTEDAPPVERSION INTEGER, ZCREATIONDATE REAL, "
            "ZACKNOWLEDGEDDATE REAL, ZSORTORDER INTEGER)"
        )
        conn.execute(
            "INSERT INTO ZREMCDDUEDATEDELTAALERT VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, alert_uuid.bytes, reminder_uuid.bytes, 1, -1, 0, 800786817.653489, None, 1),
        )
        row = conn.execute(
            "SELECT 42 AS Z_PK, 'Taxes' AS ZTITLE, NULL AS ZNOTES, "
            "0 AS ZCOMPLETED, 0 AS ZFLAGGED, 0 AS ZPRIORITY, 0 AS ZISURGENTSTATEENABLEDFORCURRENTUSER, "
            "NULL AS ZDUEDATEDELTAALERTSDATA, 800798400 AS ZDUEDATE, NULL AS ZALLDAY, NULL AS ZCOMPLETIONDATE, "
            "NULL AS ZCREATIONDATE, NULL AS ZPARENTREMINDER, 1 AS ZLIST, "
            "NULL AS ZICSURL, ? AS ZCKIDENTIFIER, 'Work' AS list_name, "
            "NULL AS recurrence_frequency, NULL AS recurrence_interval, "
            "NULL AS recurrence_count, NULL AS recurrence_end_date, "
            "NULL AS recurrence_days_of_week, NULL AS recurrence_days_of_month, "
            "NULL AS recurrence_months_of_year, NULL AS recurrence_days_of_year, "
            "NULL AS recurrence_weeks_of_year, NULL AS recurrence_set_positions",
            (str(reminder_uuid),),
        ).fetchone()

        payload = self.remctl.to_dict(row, db=conn, _sc={42: 0}, _ht={42: []})
        formatted = self.remctl.fmt(row, db=conn, verbose=True, _sc={42: 0}, _ht={42: []})
        conn.close()

        self.assertEqual(payload["earlyReminder"]["label"], "1 hour before")
        self.assertEqual(payload["earlyReminder"]["identifier"], str(alert_uuid).upper())
        self.assertIn("Early: 1 hour before", self.remctl._strip_ansi(formatted))

    def test_cmd_info_text_shows_early_reminder_from_delta_alert_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        reminder_uuid = uuid.UUID("339DDE31-8F53-46BD-8469-CF42CA5C8CAB")
        alert_uuid = uuid.UUID("2C0EDC64-6294-488A-814F-46B44C8ABBD3")
        conn.execute(
            "CREATE TABLE ZREMCDDUEDATEDELTAALERT ("
            "Z_PK INTEGER, ZIDENTIFIER BLOB, ZREMINDERIDENTIFIER BLOB, "
            "ZDUEDATEDELTAUNIT INTEGER, ZDUEDATEDELTACOUNT INTEGER, "
            "ZMINIMUMSUPPORTEDAPPVERSION INTEGER, ZCREATIONDATE REAL, "
            "ZACKNOWLEDGEDDATE REAL, ZSORTORDER INTEGER)"
        )
        conn.execute(
            "INSERT INTO ZREMCDDUEDATEDELTAALERT VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, alert_uuid.bytes, reminder_uuid.bytes, 1, -1, 0, 800786817.653489, None, 1),
        )
        reminder = {
            "Z_PK": 42,
            "ZTITLE": "Taxes",
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None,
            "ZDUEDATE": 800798400,
            "ZDISPLAYDATEDATE": None,
            "ZALLDAY": 0,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": str(reminder_uuid),
            "list_name": "Work",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
        }
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=conn),
                mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
                mock.patch.object(self.remctl, "q_reminders", return_value=[]),
                mock.patch.object(self.remctl, "q_attachments", return_value=[]),
                mock.patch.object(self.remctl, "q_alarms", return_value=[]),
                mock.patch.object(self.remctl, "q_hashtags", return_value=[]),
                mock.patch.object(self.remctl, "q_section_memberships", return_value={}),
                mock.patch.object(self.remctl, "q_rich_link", return_value=None),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_info(SimpleNamespace(id=42, json=False))
        finally:
            conn.close()

        plain = self.remctl._strip_ansi(stdout.getvalue())
        self.assertIn("Early:", plain)
        self.assertIn("1 hour before", plain)

    def test_cmd_info_text_shows_when_reminders_lists_it_at_another_time(self):
        from datetime import datetime

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        reminder = {
            "Z_PK": 42,
            "ZTITLE": "Write about Deflector",
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None,
            "ZDUEDATE": self.remctl.to_ts(datetime(2026, 9, 24, 15, 0, 0)),
            "ZDISPLAYDATEDATE": self.remctl.to_ts(datetime(2026, 9, 22, 15, 0, 0)),
            "ZALLDAY": 0,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": None,
            "list_name": "Work",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
        }
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=conn),
                mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
                mock.patch.object(self.remctl, "q_reminders", return_value=[]),
                mock.patch.object(self.remctl, "q_attachments", return_value=[]),
                mock.patch.object(self.remctl, "q_alarms", return_value=[]),
                mock.patch.object(self.remctl, "q_hashtags", return_value=[]),
                mock.patch.object(self.remctl, "q_section_memberships", return_value={}),
                mock.patch.object(self.remctl, "q_rich_link", return_value=None),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_info(SimpleNamespace(id=42, json=False))
        finally:
            conn.close()

        plain = self.remctl._strip_ansi(stdout.getvalue())
        self.assertIn("Due:       Sep 24, 2026 at 03:00 PM", plain)
        self.assertIn("Shown at:  Sep 22, 2026 at 03:00 PM", plain)

    def test_existing_early_reminder_identifiers_fall_back_to_delta_alert_table(self):
        row = {"Z_PK": 1, "ZDUEDATEDELTAALERTSDATA": None}
        alert_row = {
            "ZIDENTIFIER": uuid.UUID("2C0EDC64-6294-488A-814F-46B44C8ABBD3").bytes,
            "ZDUEDATEDELTAUNIT": 0,
            "ZDUEDATEDELTACOUNT": -15,
            "ZMINIMUMSUPPORTEDAPPVERSION": 0,
            "ZCREATIONDATE": None,
            "ZACKNOWLEDGEDDATE": None,
        }
        with (
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=row),
            mock.patch.object(self.remctl, "q_due_date_delta_alerts", return_value=[alert_row]),
        ):
            identifiers = self.remctl.early_reminder_identifiers_for_reminder(object(), "REMINDER-ID")

        self.assertEqual(identifiers, ["2C0EDC64-6294-488A-814F-46B44C8ABBD3"])

    def _private_edit_args(self, **overrides):
        base = {
            "id": 42,
            "private": True,
            "private_metadata": False,
            "url": None,
            "tags": None,
            "set_tags": None,
            "clear_tags": False,
            "remove_tag": None,
            "grocery": False,
            "section": None,
            "section_id": None,
            "new_section": None,
            "subtask": None,
            "image": None,
            "flag": False,
            "flagged": None,
            "urgent": None,
            "early_reminder": None,
            "location_title": None,
            "latitude": None,
            "longitude": None,
            "radius": 100,
            "proximity": "arriving",
            "address": None,
            "assign": None,
            "unassign": False,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def _insert_section(self, db, pk, name, ckid, list_pk=1):
        db.execute(
            "INSERT INTO ZREMCDBASESECTION "
            "(Z_PK, ZDISPLAYNAME, ZLIST, ZCKIDENTIFIER, ZMARKEDFORDELETION) "
            "VALUES (?, ?, ?, ?, 0)",
            (pk, name, list_pk, ckid),
        )

    def test_apply_private_changes_replaces_synced_tags(self):
        args = self._private_edit_args(set_tags="work,#home,work")
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "private_action", return_value={"status": "updated"}) as private_action,
        ):
            result = self.remctl.apply_private_changes("REM-1", args)

        self.assertEqual(result, [{"status": "updated"}])
        private_action.assert_called_once_with({
            "action": "set_tags",
            "id": "REM-1",
            "tags": ["work", "home"],
        }, partial_context=None)

    def test_apply_private_changes_clears_synced_tags(self):
        args = self._private_edit_args(clear_tags=True)
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(self.remctl, "private_action", return_value={"status": "updated"}) as private_action,
        ):
            self.remctl.apply_private_changes("REM-1", args)

        private_action.assert_called_once_with({
            "action": "set_tags",
            "id": "REM-1",
            "tags": [],
        }, partial_context=None)

    def test_apply_private_changes_removes_selected_synced_tags(self):
        args = self._private_edit_args(remove_tag=["#work", "archive"])
        db = object()
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "q_hashtags",
                return_value=[{"ZNAME": "Work"}, {"ZNAME": "Home"}, {"ZNAME": "Archive"}],
            ) as q_hashtags,
            mock.patch.object(self.remctl, "private_action", return_value={"status": "updated"}) as private_action,
        ):
            self.remctl.apply_private_changes("REM-1", args, db=db)

        q_hashtags.assert_called_once_with(db, 42)
        private_action.assert_called_once_with({
            "action": "set_tags",
            "id": "REM-1",
            "tags": ["Home"],
        }, partial_context=None)

    def test_q_hashtags_excludes_soft_deleted_tag_links(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE ZREMCDHASHTAGLABEL (Z_PK INTEGER, ZNAME TEXT)")
        conn.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, ZREMINDER3 INTEGER, ZHASHTAGLABEL INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        conn.execute(
            "CREATE TABLE ZREMCDREMINDER ("
            "ZPARENTREMINDER INTEGER, ZMARKEDFORDELETION INTEGER, ZCOMPLETED INTEGER)"
        )
        conn.execute("INSERT INTO ZREMCDHASHTAGLABEL VALUES (1, 'Work'), (2, 'Deleted')")
        conn.execute(
            "INSERT INTO ZREMCDOBJECT VALUES (1, 42, 1, 0), (2, 42, 2, 1)"
        )

        tags = [row["ZNAME"] for row in self.remctl.q_hashtags(conn, 42)]
        _, hashtags = self.remctl.preload_extras(conn, [42])
        conn.close()

        self.assertEqual(tags, ["Work"])
        self.assertEqual(hashtags.get(42), ["Work"])

    def test_apply_private_changes_remove_tag_ignores_soft_deleted_tags(self):
        args = self._private_edit_args(remove_tag=["work"])
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE ZREMCDHASHTAGLABEL (Z_PK INTEGER, ZNAME TEXT)")
        db.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, ZREMINDER3 INTEGER, ZHASHTAGLABEL INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        db.execute("INSERT INTO ZREMCDHASHTAGLABEL VALUES (1, 'Work'), (2, 'Home'), (3, 'Archive')")
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(1, 42, 1, 0), (2, 42, 2, 0), (3, 42, 3, 1)"
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_action", return_value={"status": "updated"}) as private_action,
            ):
                self.remctl.apply_private_changes("REM-1", args, db=db)
        finally:
            db.close()

        private_action.assert_called_once_with({
            "action": "set_tags",
            "id": "REM-1",
            "tags": ["Home"],
        }, partial_context=None)

    def test_tag_replacement_rejects_additive_tags(self):
        args = self._private_edit_args(tags="new", set_tags="work")
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.validate_tag_replacement_args(args)

        self.assertIn("cannot be combined", stderr.getvalue())

    def test_tag_replacement_requires_private_opt_in(self):
        args = self._private_edit_args(private=False, set_tags="work")
        with (
            mock.patch.object(self.remctl, "private_available") as private_available,
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.refuse_private_args_without_opt_in(args)

        private_available.assert_not_called()
        self.assertIn("require --private", stderr.getvalue())

    def test_section_create_uses_private_helper_with_existing_order(self):
        db = self._list_db(["Projects"])
        self._insert_section(db, 10, "Inbox", "SECTION-1")
        args = SimpleNamespace(
            name="Research",
            list="Projects",
            list_id=None,
            private=True,
            private_metadata=False,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call", return_value={"status": "created", "id": "SECTION-2"}) as private_call,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_section_create(args)
        finally:
            db.close()

        private_call.assert_called_once_with({
            "action": "create_section",
            "listId": "CK-1",
            "name": "Research",
            "existingSectionIds": ["SECTION-1"],
        })
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["name"], "Research")

    def test_apply_private_changes_new_section_includes_existing_section_order(self):
        db = self._list_db(["Projects"])
        self._insert_section(db, 10, "Inbox", "SECTION-1")
        self._insert_section(db, 11, "Active", "SECTION-2")
        args = self._private_edit_args(new_section="Research")
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_action", return_value={"status": "updated"}) as private_action,
            ):
                self.remctl.apply_private_changes("REM-1", args, db=db, list_pk=1)
        finally:
            db.close()

        private_action.assert_called_once_with({
            "action": "add_section_and_assign",
            "id": "REM-1",
            "name": "Research",
            "existingSectionIds": ["SECTION-1", "SECTION-2"],
        }, partial_context=None)

    def test_apply_private_changes_new_section_passes_empty_existing_section_ids(self):
        db = self._list_db(["Projects"])
        args = self._private_edit_args(new_section="Research")
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_action", return_value={"status": "updated"}) as private_action,
            ):
                self.remctl.apply_private_changes("REM-1", args, db=db, list_pk=1)
        finally:
            db.close()

        private_action.assert_called_once_with({
            "action": "add_section_and_assign",
            "id": "REM-1",
            "name": "Research",
            "existingSectionIds": [],
        }, partial_context=None)

    def test_decode_manual_sort_hint_blob_accepts_version_prefix(self):
        payload = {
            "topLevelElementIDs": ["REM-1", "REM-2"],
            "secondaryLevelElementIDsByTopLevelElementID": {"REM-1": ["CHILD-1"]},
        }
        encoded = b"\x01" + json.dumps(payload).encode("utf-8")

        self.assertEqual(self.remctl.decode_manual_sort_hint_blob(encoded), payload)

    def test_sort_reminder_rows_follows_identifier_order_and_appends_unknown_rows(self):
        rows = [
            {"Z_PK": 1, "ZTITLE": "First", "ZCKIDENTIFIER": "REM-A"},
            {"Z_PK": 2, "ZTITLE": "Merging", "ZCKIDENTIFIER": "REM-NEW"},
            {"Z_PK": 3, "ZTITLE": "Second", "ZCKIDENTIFIER": "REM-B"},
            {"Z_PK": 4, "ZTITLE": "No identifier", "ZCKIDENTIFIER": None},
        ]

        ordered = self.remctl.sort_reminder_rows_by_identifier_order(
            rows,
            ["rem-b", "rem-a"],
        )

        self.assertEqual(
            [row["ZTITLE"] for row in ordered],
            ["Second", "First", "Merging", "No identifier"],
        )

    def test_q_reminders_manual_order_expands_query_and_applies_stored_order(self):
        class Result:
            def __init__(self, *, one=None, rows=None):
                self.one = one
                self.rows = rows or []

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.rows

        class FakeDB:
            def __init__(self):
                self.reminder_query_params = None
                self.reminder_query = None

            def execute(self, query, params=()):
                if "ZREMINDERIDSMERGEABLEORDERING_V2_JSON" in query:
                    return Result(one=(json.dumps(["REM-B", "REM-A", "REM-C"]),))
                self.reminder_query = query
                self.reminder_query_params = params
                return Result(rows=[
                    {"Z_PK": 1, "ZTITLE": "First", "ZCKIDENTIFIER": "REM-A"},
                    {"Z_PK": 2, "ZTITLE": "Second", "ZCKIDENTIFIER": "REM-B"},
                    {"Z_PK": 3, "ZTITLE": "Third", "ZCKIDENTIFIER": "REM-C"},
                ])

        db = FakeDB()
        with mock.patch.object(self.remctl, "rem_cols", return_value="r.Z_PK"):
            rows = self.remctl.q_reminders(
                db,
                list_pk=7,
                top_level=True,
                limit=2,
                manual_order=True,
            )

        self.assertEqual([row["ZTITLE"] for row in rows], ["Second", "First"])
        self.assertNotIn("LIMIT ?", db.reminder_query)
        self.assertEqual(db.reminder_query_params, [7])

    def test_q_reminders_sorts_before_limit_when_unknown_rows_precede_positioned_row(self):
        class Result:
            def __init__(self, *, one=None, rows=None):
                self.one = one
                self.rows = rows or []

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.rows

        class FakeDB:
            def execute(self, query, params=()):
                if "ZREMINDERIDSMERGEABLEORDERING_V2_JSON" in query:
                    return Result(one=(json.dumps(["REM-Z"]),))
                self.asserted_query = query
                return Result(rows=[
                    {"Z_PK": 1, "ZTITLE": "Unknown A", "ZCKIDENTIFIER": "UNKNOWN-A"},
                    {"Z_PK": 2, "ZTITLE": "Unknown B", "ZCKIDENTIFIER": "UNKNOWN-B"},
                    {"Z_PK": 3, "ZTITLE": "Positioned", "ZCKIDENTIFIER": "REM-Z"},
                ])

        db = FakeDB()
        with mock.patch.object(self.remctl, "rem_cols", return_value="r.Z_PK"):
            rows = self.remctl.q_reminders(
                db,
                list_pk=7,
                top_level=True,
                limit=2,
                manual_order=True,
            )

        self.assertNotIn("LIMIT ?", db.asserted_query)
        self.assertEqual([row["ZTITLE"] for row in rows], ["Positioned", "Unknown A"])

    def test_q_reminders_for_lists_applies_each_child_list_order(self):
        class Result:
            def __init__(self, *, one=None, rows=None):
                self.one = one
                self.rows = rows or []

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.rows

        class FakeDB:
            def execute(self, query, params=()):
                if "ZREMINDERIDSMERGEABLEORDERING_V2_JSON" in query:
                    order = {
                        7: ["EDITORIAL-B", "EDITORIAL-A"],
                        8: ["PROJECTS-A", "PROJECTS-B"],
                    }[params[0]]
                    return Result(one=(json.dumps(order),))
                return Result(rows=[
                    {"Z_PK": 1, "ZTITLE": "Editorial A", "ZCKIDENTIFIER": "EDITORIAL-A", "ZLIST": 7},
                    {"Z_PK": 2, "ZTITLE": "Editorial B", "ZCKIDENTIFIER": "EDITORIAL-B", "ZLIST": 7},
                    {"Z_PK": 3, "ZTITLE": "Projects A", "ZCKIDENTIFIER": "PROJECTS-A", "ZLIST": 8},
                    {"Z_PK": 4, "ZTITLE": "Projects B", "ZCKIDENTIFIER": "PROJECTS-B", "ZLIST": 8},
                ])

        with mock.patch.object(self.remctl, "rem_cols", return_value="r.Z_PK"):
            rows = self.remctl.q_reminders_for_lists(
                FakeDB(),
                [7, 8],
                top_level=True,
                manual_order=True,
            )

        self.assertEqual(
            [row["ZTITLE"] for row in rows],
            ["Editorial B", "Editorial A", "Projects A", "Projects B"],
        )

    def test_q_reminders_for_lists_sorts_all_children_before_global_limit(self):
        class Result:
            def __init__(self, *, one=None, rows=None):
                self.one = one
                self.rows = rows or []

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.rows

        class FakeDB:
            def execute(self, query, params=()):
                if "ZREMINDERIDSMERGEABLEORDERING_V2_JSON" in query:
                    order = {7: ["EDITORIAL-Z"], 8: ["PROJECTS-Z"]}[params[0]]
                    return Result(one=(json.dumps(order),))
                self.reminder_query = query
                return Result(rows=[
                    {"Z_PK": 1, "ZTITLE": "Editorial unknown", "ZCKIDENTIFIER": "EDITORIAL-A", "ZLIST": 7},
                    {"Z_PK": 2, "ZTITLE": "Editorial positioned", "ZCKIDENTIFIER": "EDITORIAL-Z", "ZLIST": 7},
                    {"Z_PK": 3, "ZTITLE": "Projects unknown", "ZCKIDENTIFIER": "PROJECTS-A", "ZLIST": 8},
                    {"Z_PK": 4, "ZTITLE": "Projects positioned", "ZCKIDENTIFIER": "PROJECTS-Z", "ZLIST": 8},
                ])

        db = FakeDB()
        with mock.patch.object(self.remctl, "rem_cols", return_value="r.Z_PK"):
            rows = self.remctl.q_reminders_for_lists(
                db,
                [7, 8],
                top_level=True,
                limit=3,
                manual_order=True,
            )

        self.assertNotIn("LIMIT ?", db.reminder_query)
        self.assertEqual(
            [row["ZTITLE"] for row in rows],
            ["Editorial positioned", "Editorial unknown", "Projects positioned"],
        )

    def test_reorder_identifier_list_supports_relative_and_edge_positions(self):
        ordering = ["A", "MOVE", "B", "C"]

        self.assertEqual(
            self.remctl.reorder_identifier_list(ordering, "MOVE", "after", "C"),
            ["A", "B", "C", "MOVE"],
        )
        self.assertEqual(
            self.remctl.reorder_identifier_list(ordering, "MOVE", "first"),
            ["MOVE", "A", "B", "C"],
        )
        self.assertEqual(
            self.remctl.reorder_identifier_list(ordering, "MOVE", "last"),
            ["A", "B", "C", "MOVE"],
        )

    def test_smart_list_section_query_uses_smart_owner_column(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        try:
            db.execute(
                "CREATE TABLE ZREMCDBASESECTION ("
                "Z_PK INTEGER PRIMARY KEY, ZDISPLAYNAME TEXT, ZSMARTLIST INTEGER, "
                "ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER)"
            )
            db.execute(
                "INSERT INTO ZREMCDBASESECTION VALUES (?, ?, ?, ?, 0)",
                (1, "Current", 7, "SECTION"),
            )

            sections = self.remctl.q_smart_list_sections(db, 7)
        finally:
            db.close()

        self.assertEqual([section["ZDISPLAYNAME"] for section in sections], ["Current"])

    def test_reminder_move_in_custom_smart_list_sends_manual_order(self):
        args = SimpleNamespace(
            id=101,
            before=202,
            after=None,
            first=False,
            last=False,
            smart_list="Focus",
            smart_list_id=None,
            private=True,
            private_metadata=False,
            json=True,
        )
        reminders = {
            101: {
                "ZCKIDENTIFIER": "MOVING",
                "ZLIST": 12,
                "ZTITLE": "Review proposal",
                "list_name": "Work",
            },
            202: {
                "ZCKIDENTIFIER": "ANCHOR",
                "ZLIST": 4,
                "ZTITLE": "Plan launch",
                "list_name": "Projects",
            },
        }
        hint = {
            "objectUUID": "HINT",
            "listType": 2,
            "listID": "SMART",
            "topLevelElementIDs": ["MOVING", "OTHER", "ANCHOR"],
            "secondaryLevelElementIDsByTopLevelElementID": {},
        }
        with (
            mock.patch.object(self.remctl, "require_private_metadata"),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", side_effect=lambda _db, pk: reminders.get(pk)),
            mock.patch.object(
                self.remctl,
                "resolve_smart_list_or_die",
                return_value={"id": 7, "title": "Focus", "objectUUID": "SMART", "kind": "custom"},
            ),
            mock.patch.object(self.remctl, "q_manual_sort_hint", return_value=hint),
            mock.patch.object(self.remctl, "q_smart_list_sections", return_value=[]),
            mock.patch.object(self.remctl, "private_call", return_value={"status": "updated"}) as private_call,
            mock.patch.object(self.remctl, "_wait_for_order", return_value=True),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_reminder_move(args)

        private_call.assert_called_once_with({
            "action": "set_smart_list_manual_order",
            "id": "MOVING",
            "smartListId": "SMART",
            "manualOrderingId": "HINT",
            "listType": 2,
            "topLevelElementIds": ["OTHER", "MOVING", "ANCHOR"],
            "secondaryLevelElementIdsByTopLevelElementId": {},
        })
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["verified"])
        self.assertEqual(result["smartList"], "Focus")
        self.assertNotIn("section", result)

    def test_reminder_move_rejects_sectioned_custom_smart_list_before_writing(self):
        args = SimpleNamespace(
            id=101,
            before=None,
            after=None,
            first=True,
            last=False,
            smart_list="Focus",
            smart_list_id=None,
            private=True,
            private_metadata=False,
            json=True,
        )
        reminder = {
            "ZCKIDENTIFIER": "MOVING",
            "ZLIST": 12,
            "ZTITLE": "Review proposal",
            "list_name": "Work",
        }
        hint = {
            "objectUUID": "HINT",
            "listType": 2,
            "listID": "SMART",
            "topLevelElementIDs": ["MOVING"],
            "secondaryLevelElementIDsByTopLevelElementID": {},
        }
        with (
            mock.patch.object(self.remctl, "require_private_metadata"),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(
                self.remctl,
                "resolve_smart_list_or_die",
                return_value={"id": 7, "title": "Focus", "objectUUID": "SMART", "kind": "custom"},
            ),
            mock.patch.object(self.remctl, "q_manual_sort_hint", return_value=hint),
            mock.patch.object(self.remctl, "q_smart_list_sections", return_value=[{"Z_PK": 1}]),
            mock.patch.object(self.remctl, "private_call") as private_call,
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.cmd_reminder_move(args)

        self.assertIn("sectioned custom smart lists is not supported", stderr.getvalue())
        private_call.assert_not_called()

    def test_reminder_move_rejects_cross_list_without_smart_list(self):
        args = SimpleNamespace(
            id=1,
            before=2,
            after=None,
            first=False,
            last=False,
            smart_list=None,
            smart_list_id=None,
            private=True,
            private_metadata=False,
            json=True,
        )
        reminders = {
            1: {"ZCKIDENTIFIER": "A", "ZLIST": 10, "ZTITLE": "A", "list_name": "One"},
            2: {"ZCKIDENTIFIER": "B", "ZLIST": 20, "ZTITLE": "B", "list_name": "Two"},
        }
        with (
            mock.patch.object(self.remctl, "require_private_metadata"),
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(self.remctl, "q_reminder", side_effect=lambda _db, pk: reminders.get(pk)),
            mock.patch.object(self.remctl, "private_call") as private_call,
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.cmd_reminder_move(args)

        self.assertIn("different lists require --smart-list", stderr.getvalue())
        private_call.assert_not_called()

    def test_require_private_metadata_accepts_protocol_version_two(self):
        self._default_protocol_probe.stop()
        self.remctl._private_protocol_probe = None
        try:
            with (
                mock.patch.object(self.remctl, "private_metadata_enabled", return_value=True),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "private_call_result",
                    return_value={
                        "returncode": 0,
                        "stdout": json.dumps({"status": "ok", "protocolVersion": 2}),
                        "stderr": "",
                        "payload": {"status": "ok", "protocolVersion": 2},
                    },
                ) as private_call_result,
            ):
                self.remctl.require_private_metadata(SimpleNamespace(private=True))
        finally:
            self.remctl._private_protocol_probe = None
            self._default_protocol_probe.start()

        private_call_result.assert_called_once_with({"action": "protocol_version"}, timeout=5)

    def test_require_private_metadata_rejects_protocol_version_one(self):
        with (
            mock.patch.object(self.remctl, "private_metadata_enabled", return_value=True),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl,
                "_probe_private_protocol_version",
                return_value={"ok": True, "version": 1},
            ),
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.require_private_metadata(SimpleNamespace(private=True))

        self.assertIn("protocol 1 < required 2", stderr.getvalue())

    def test_require_private_metadata_rejects_outdated_helper_unknown_action(self):
        self._default_protocol_probe.stop()
        self.remctl._private_protocol_probe = None
        try:
            with (
                mock.patch.object(self.remctl, "private_metadata_enabled", return_value=True),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "private_call_result",
                    return_value={
                        "returncode": 1,
                        "stdout": json.dumps({"status": "error", "message": "Unknown action"}),
                        "stderr": "",
                        "payload": {"status": "error", "message": "Unknown action"},
                    },
                ),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.remctl.require_private_metadata(SimpleNamespace(private=True))
        finally:
            self.remctl._private_protocol_probe = None
            self._default_protocol_probe.start()

        self.assertIn("remctl-private is outdated", stderr.getvalue())
        self.assertIn("protocol 0 < required 2", stderr.getvalue())

    def test_require_private_metadata_memoizes_protocol_probe(self):
        self._default_protocol_probe.stop()
        self.remctl._private_protocol_probe = None
        try:
            with (
                mock.patch.object(self.remctl, "private_metadata_enabled", return_value=True),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "private_call_result",
                    return_value={
                        "returncode": 0,
                        "stdout": json.dumps({"status": "ok", "protocolVersion": 2}),
                        "stderr": "",
                        "payload": {"status": "ok", "protocolVersion": 2},
                    },
                ) as private_call_result,
            ):
                self.remctl.require_private_metadata(SimpleNamespace(private=True))
                self.remctl.require_private_metadata(SimpleNamespace(private=True))
        finally:
            self.remctl._private_protocol_probe = None
            self._default_protocol_probe.start()

        private_call_result.assert_called_once_with({"action": "protocol_version"}, timeout=5)

    def test_smart_list_edit_match_with_tags_updates_filter_payload(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="High Priority",
            smart_list_id=None,
            private=True,
            match="any",
            flagged=False,
            priority=None,
            tags="work,home",
            tag_match="any",
            any_tag=False,
            untagged=False,
            date=None,
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=None,
            exclude_list=None,
            include_list_id=None,
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "updated", "id": "CUSTOM-1"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_smart_list_edit(args)
        finally:
            db.close()

        payload = private_call.call_args.args[0]
        decoded = json.loads(base64.b64decode(payload["filterData"]).decode("utf-8"))
        self.assertEqual(decoded["operation"], "or")
        self.assertEqual(
            decoded["hashtags"],
            {"hashtags": {"operation": "or", "include": ["work", "home"], "exclude": []}},
        )

    def test_smart_list_edit_match_only_reports_clear_error(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="High Priority",
            smart_list_id=None,
            private=True,
            match="any",
            flagged=False,
            priority=None,
            tags=None,
            tag_match=None,
            any_tag=False,
            untagged=False,
            date=None,
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=None,
            exclude_list=None,
            include_list_id=None,
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_call") as private_call,
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.remctl.cmd_smart_list_edit(args)
        finally:
            db.close()

        private_call.assert_not_called()
        self.assertIn("--match", stderr.getvalue())
        self.assertIn("filter option", stderr.getvalue())

    def test_smart_list_create_without_match_flags_builds_unchanged_payload(self):
        db = self._smart_list_db()
        args = SimpleNamespace(
            name="Flagged Review",
            private=True,
            flagged=True,
            priority=None,
            tags=None,
            tag_match="any",
            any_tag=False,
            untagged=False,
            date=None,
            date_today_include_past_due=False,
            date_on=None,
            date_before=None,
            date_after=None,
            date_range=None,
            date_relative=None,
            time=None,
            include_list=None,
            exclude_list=None,
            include_list_id=None,
            exclude_list_id=None,
            list_match=None,
            vehicle=None,
            location_title=None,
            latitude=None,
            longitude=None,
            radius=100.0,
            proximity="enter",
            filter_json=None,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    return_value={"status": "created", "id": "SMART-1"},
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_smart_list_create(args)
        finally:
            db.close()

        payload = private_call.call_args.args[0]
        self.assertEqual(payload["filterData"], "eyJmbGFnZ2VkIjp0cnVlfQ==")

    def test_section_create_refuses_duplicate_name_before_private_call(self):
        db = self._list_db(["Projects"])
        self._insert_section(db, 10, "Research", "SECTION-1")
        args = SimpleNamespace(
            name="research",
            list="Projects",
            list_id=None,
            private=True,
            private_metadata=False,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call") as private_call,
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.remctl.cmd_section_create(args)
        finally:
            db.close()

        private_call.assert_not_called()
        self.assertIn("section already exists", stderr.getvalue())

    def test_section_rename_and_delete_use_private_helper(self):
        db = self._list_db(["Projects"])
        self._insert_section(db, 10, "Research", "SECTION-1")
        rename_args = SimpleNamespace(
            name="Research",
            section_id=None,
            new_name="Reading",
            list="Projects",
            list_id=None,
            private=True,
            private_metadata=False,
            json=True,
        )
        delete_args = SimpleNamespace(
            name=None,
            section_id="SECTION-1",
            list="Projects",
            list_id=None,
            private=True,
            private_metadata=False,
            force=True,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(
                    self.remctl,
                    "private_call",
                    side_effect=[{"status": "renamed"}, {"status": "deleted"}],
                ) as private_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.remctl.cmd_section_rename(rename_args)
                self.remctl.cmd_section_delete(delete_args)
        finally:
            db.close()

        self.assertEqual(
            [call.args[0] for call in private_call.call_args_list],
            [
                {"action": "rename_section", "sectionId": "SECTION-1", "name": "Reading"},
                {"action": "delete_section", "sectionId": "SECTION-1"},
            ],
        )

    def test_section_rename_refuses_name_collision_before_private_call(self):
        db = self._list_db(["Projects"])
        self._insert_section(db, 10, "Research", "SECTION-1")
        self._insert_section(db, 11, "Reading", "SECTION-2")
        args = SimpleNamespace(
            name="Research",
            section_id=None,
            new_name="reading",
            list="Projects",
            list_id=None,
            private=True,
            private_metadata=False,
            json=True,
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "private_available", return_value=True),
                mock.patch.object(self.remctl, "private_call") as private_call,
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.remctl.cmd_section_rename(args)
        finally:
            db.close()

        private_call.assert_not_called()
        self.assertIn("already named", stderr.getvalue())

    def _run_uninstall(self, tmp, *args):
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.pop("PREFIX", None)
        env.update({
            "HOME": str(tmp / "home"),
            "PREFIX": str(tmp / "prefix"),
            "REMCTL_BIN_DIR": str(tmp / "bin"),
            "REMCTL_LAUNCH_AGENT_DIR": str(tmp / "prefix" / "Library" / "LaunchAgents"),
            "REMCTL_CONFIG_DIR": str(tmp / "home" / ".config" / "remctl"),
            "REMCTL_SKIP_LAUNCHSERVICES": "1",
        })
        return subprocess.run(
            ["bash", str(root / "uninstall.sh"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

    def test_uninstall_dry_run_without_ownership_marker_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            config_dir = tmp / "home" / ".config" / "remctl"
            (bin_dir / "completions").mkdir(parents=True)
            config_dir.mkdir(parents=True)
            for rel in ["remctl", "remctl-bridge", "completions/_remctl"]:
                (bin_dir / rel).write_text("installed")
            (config_dir / "settings.json").write_text("{}")

            result = self._run_uninstall(tmp, "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((bin_dir / "remctl").exists())
            self.assertTrue((bin_dir / "remctl-bridge").exists())
            self.assertTrue((bin_dir / "completions" / "_remctl").exists())
            self.assertTrue(config_dir.exists())
            self.assertIn("Keeping unowned config directory", result.stdout)

    def test_uninstall_without_ownership_marker_keeps_bin_and_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            config_dir = tmp / "home" / ".config" / "remctl"
            (bin_dir / "completions").mkdir(parents=True)
            config_dir.mkdir(parents=True)
            for rel in ["remctl", "remctl_runtime.py", "remctl-bridge", "completions/_remctl"]:
                (bin_dir / rel).write_text("installed")
            (bin_dir / "unrelated").write_text("keep")
            (config_dir / "settings.json").write_text("{}")

            result = self._run_uninstall(tmp)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((bin_dir / "remctl").exists())
            self.assertTrue((bin_dir / "remctl_runtime.py").exists())
            self.assertTrue((bin_dir / "remctl-bridge").exists())
            self.assertTrue((bin_dir / "completions" / "_remctl").exists())
            self.assertTrue((bin_dir / "unrelated").exists())
            self.assertTrue(config_dir.exists())
            self.assertIn("Keeping unowned config directory", result.stdout)
            self.assertIn("Nothing found to remove", result.stdout)

# ── Inline Images (feature/inline-images) ────────────────────────────────────

ATTACHMENT_JSON_KEYS = {
    "filename",
    "type",
    "path",
    "resolved",
    "uti",
    "width",
    "height",
}


def _tiny_png_bytes(width=4, height=2):
    """Deterministic truecolor PNG without Pillow: solid red / solid blue rows."""
    import zlib

    rows = [
        [(255, 0, 0)] * width,  # top row: red
        [(0, 0, 255)] * width,  # bottom row: blue
    ]
    raw = b""
    for y in range(height):
        raw += b"\x00" + b"".join(
            bytes((r, g, b)) for (r, g, b) in rows[y % len(rows)]
        )

    def chunk(tag, data):
        import binascii

        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _tiny_rgba_png_bytes(width=4, height=2):
    """Deterministic RGBA PNG: semi-transparent red over semi-transparent blue."""
    import zlib

    rows = [
        [(255, 0, 0, 128)] * width,
        [(0, 0, 255, 255)] * width,
    ]
    raw = b""
    for y in range(height):
        raw += b"\x00" + b"".join(
            bytes((r, g, b, a)) for (r, g, b, a) in rows[y % len(rows)]
        )

    def chunk(tag, data):
        import binascii

        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _tiny_jpeg_bytes():
    """Minimal valid baseline JPEG (4x2 white) used to exercise kitty f=106."""
    return base64.b64decode(
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
        "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
        "2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
        "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAQDASIAAhEBAxEB"
        "/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIE"
        "AwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2Jy"
        "ggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlq"
        "c3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
        "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEB"
        "AQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3"
        "AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcY"
        "GRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6"
        "goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK"
        "0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iii"
        "gD//2Q=="
    )


class InlineImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")
        import remctl_images
        import remctl_serialization

        cls.images = remctl_images
        cls.remctl_serialization = remctl_serialization
        cls._default_protocol_probe = mock.patch.object(
            cls.remctl,
            "_probe_private_protocol_version",
            return_value={"ok": True, "version": 2},
        )
        cls._default_protocol_probe.start()

    @classmethod
    def tearDownClass(cls):
        cls._default_protocol_probe.stop()

    def setUp(self):
        self._color_enabled = self.remctl.C.enabled
        # The attachment-dir lookup caches on Files/ mtime; isolate tests.
        self.images._attachment_dir_cache.clear()
        self.remctl._attachment_sha_capability_cache.clear()

    def tearDown(self):
        self.remctl.C.enabled = self._color_enabled
        self.images._attachment_dir_cache.clear()
        self.remctl._attachment_sha_capability_cache.clear()

    # ── Shared fixtures ─────────────────────────────────────────────────

    @staticmethod
    def _reminder_row(pk=42, title="Pick up prints", ckid="ATT-REM-1"):
        return {
            "Z_PK": pk,
            "ZTITLE": title,
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None,
            "ZDUEDATE": None,
            "ZDISPLAYDATEDATE": None,
            "ZALLDAY": 0,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": ckid,
            "list_name": "Projects",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
        }

    @staticmethod
    def _attachment_db(*, saved_sha=True, object_sha=True):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        # to_dict's subtask-count fallback needs a reminder table.
        db.execute(
            "CREATE TABLE ZREMCDREMINDER ("
            "Z_PK INTEGER, ZPARENTREMINDER INTEGER, "
            "ZMARKEDFORDELETION INTEGER, ZCOMPLETED INTEGER)"
        )
        saved_cols = (
            "ZFILENAME TEXT, ZUTI TEXT, ZATTACHMENTTYPERAWVALUE TEXT, "
            + ("ZSHA512SUM TEXT, " if saved_sha else "")
            + "ZREMINDER INTEGER, ZMARKEDFORDELETION INTEGER"
        )
        db.execute(f"CREATE TABLE ZREMCDSAVEDATTACHMENT ({saved_cols})")
        object_cols = (
            "ZFILENAME TEXT, ZUTI TEXT, ZWIDTH INTEGER, ZHEIGHT INTEGER, "
            + ("ZSHA512SUM TEXT, " if object_sha else "")
            + "ZREMINDER2 INTEGER, ZMARKEDFORDELETION INTEGER"
        )
        db.execute(f"CREATE TABLE ZREMCDOBJECT ({object_cols})")
        return db

    @staticmethod
    def _attachment_store(tmpdir, filename="photo.png", content=None):
        """Create a Stores/ + Files/Account-X/Attachments layout; return pieces."""
        content = content if content is not None else _tiny_png_bytes()
        sha = hashlib.sha512(content).hexdigest()
        store_dir = Path(tmpdir) / "Stores"
        store_dir.mkdir()
        attachments_dir = (
            Path(tmpdir) / "Files" / "Account-X" / "Attachments"
        )
        attachments_dir.mkdir(parents=True)
        (attachments_dir / f"{sha}{Path(filename).suffix}").write_bytes(content)
        return store_dir, sha, content

    def _run_cmd_info(self, row, db, args):
        patches = [
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_reminder", return_value=row),
            mock.patch.object(self.remctl, "q_reminders", return_value=[]),
            mock.patch.object(self.remctl, "q_section_memberships", return_value={}),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
            mock.patch.object(self.remctl, "q_hashtags", return_value=[]),
            mock.patch.object(self.remctl, "q_rich_link", return_value=None),
            mock.patch.object(self.remctl, "q_assignment", return_value=None),
        ]
        stdout = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with contextlib.redirect_stdout(stdout):
                self.remctl.cmd_info(args)
        return stdout.getvalue()

    def _info_args(self, **overrides):
        base = {
            "id": 42,
            "json": False,
            "images": False,
            "image_mode": None,
            "image_width": 32,
            "verbose": False,
            "format": None,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    # ── 1. JSON shape ───────────────────────────────────────────────────

    def test_info_json_attachment_entries_have_all_seven_keys(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 640, 480, NULL, 42, 0)"
        )
        try:
            out = self._run_cmd_info(
                self._reminder_row(), db, self._info_args(json=True)
            )
        finally:
            db.close()
        attachments = json.loads(out)["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(set(attachments[0].keys()), ATTACHMENT_JSON_KEYS)

    def test_show_json_includes_attachments_and_omits_key_when_absent(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 640, 480, NULL, 42, 0)"
        )
        rows = [
            self._reminder_row(pk=42, ckid="REM-A"),
            self._reminder_row(pk=43, title="No attachments", ckid="REM-B"),
        ]
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "resolve_required_list_target_or_die",
                    return_value={"id": 1, "title": "Projects"},
                ),
                mock.patch.object(self.remctl, "q_reminders", return_value=rows),
                mock.patch.object(self.remctl, "q_sections", return_value=[]),
                mock.patch.object(self.remctl, "q_rich_link", return_value=None),
                mock.patch.object(self.remctl, "q_assignment", return_value=None),
                mock.patch.object(
                    self.remctl,
                    "preload_extras",
                    return_value=({42: 0, 43: 0}, {42: [], 43: []}),
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_show(
                    SimpleNamespace(
                        list="Projects",
                        list_id=None,
                        completed=False,
                        json=True,
                        format=None,
                        verbose=False,
                        images=False,
                        image_mode=None,
                        image_width=32,
                    )
                )
        finally:
            db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        with_attachment = next(item for item in payload if item["id"] == 42)
        without_attachment = next(item for item in payload if item["id"] == 43)
        self.assertEqual(
            set(with_attachment["attachments"][0].keys()), ATTACHMENT_JSON_KEYS
        )
        self.assertEqual(with_attachment["attachments"][0]["filename"], "photo.png")
        self.assertNotIn("attachments", without_attachment)

    def test_search_json_includes_attachments(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 640, 480, NULL, 42, 0)"
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "q_search",
                    return_value=[self._reminder_row()],
                ),
                mock.patch.object(self.remctl, "q_search_count", return_value=1),
                mock.patch.object(self.remctl, "q_rich_link", return_value=None),
                mock.patch.object(self.remctl, "q_assignment", return_value=None),
                mock.patch.object(
                    self.remctl,
                    "preload_extras",
                    return_value=({42: 0}, {42: []}),
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_search(
                    SimpleNamespace(
                        query="prints",
                        completed=False,
                        json=True,
                        format=None,
                        verbose=False,
                        images=False,
                        image_mode=None,
                        image_width=32,
                    )
                )
        finally:
            db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(
            set(payload[0]["attachments"][0].keys()), ATTACHMENT_JSON_KEYS
        )
        self.assertEqual(payload[0]["attachments"][0]["uti"], "public.png")

    def test_info_json_legacy_null_sha_row_is_unresolved(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 640, 480, NULL, 42, 0)"
        )
        try:
            out = self._run_cmd_info(
                self._reminder_row(), db, self._info_args(json=True)
            )
        finally:
            db.close()
        entry = json.loads(out)["attachments"][0]
        self.assertIsNone(entry["path"])
        self.assertFalse(entry["resolved"])
        self.assertEqual(entry["width"], 640)
        self.assertEqual(entry["height"], 480)
        self.assertEqual(entry["type"], "image")

    def test_info_json_resolved_row_reports_path_and_dimensions(self):
        db = self._attachment_db()
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, content = self._attachment_store(tmp)
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('photo.png', 'public.png', 4, 2, ?, 42, 0)",
                (sha,),
            )
            try:
                with mock.patch.object(
                    self.remctl, "STORE_DIR", store_dir
                ):
                    out = self._run_cmd_info(
                        self._reminder_row(), db, self._info_args(json=True)
                    )
            finally:
                db.close()
        entry = json.loads(out)["attachments"][0]
        self.assertTrue(entry["resolved"])
        self.assertTrue(entry["path"].endswith(f"{sha}.png"))
        self.assertEqual(entry["uti"], "public.png")
        self.assertEqual(entry["width"], 4)
        self.assertEqual(entry["height"], 2)

    # ── 2. Resolution ───────────────────────────────────────────────────

    def test_resolve_attachment_file_verified_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, content = self._attachment_store(tmp)
            resolved = self.images.resolve_attachment_file(
                store_dir, sha, filename="photo.png", uti="public.png"
            )
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.endswith(f"{sha}.png"))

    def test_resolve_attachment_file_rejects_tampered_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            target = (
                Path(tmp) / "Files" / "Account-X" / "Attachments" / f"{sha}.png"
            )
            target.write_bytes(b"tampered bytes, not the original image")
            resolved = self.images.resolve_attachment_file(
                store_dir, sha, filename="photo.png", uti="public.png"
            )
        self.assertIsNone(resolved)

    def test_resolve_attachment_file_extension_fallback_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Row claims .png, file on disk is .jpg: UTI map resolves it.
            content = _tiny_jpeg_bytes()
            sha = hashlib.sha512(content).hexdigest()
            store_dir = Path(tmp) / "Stores"
            store_dir.mkdir()
            attachments_dir = Path(tmp) / "Files" / "Account-X" / "Attachments"
            attachments_dir.mkdir(parents=True)
            (attachments_dir / f"{sha}.jpg").write_bytes(content)
            resolved = self.images.resolve_attachment_file(
                store_dir, sha, filename="photo.png", uti="public.jpeg"
            )
            self.assertIsNotNone(resolved)
            self.assertTrue(resolved.endswith(f"{sha}.jpg"))
        with tempfile.TemporaryDirectory() as tmp:
            # Unknown filename suffix and unknown UTI: probing finds .heic.
            content = b"heic-ish bytes"
            sha = hashlib.sha512(content).hexdigest()
            store_dir = Path(tmp) / "Stores"
            store_dir.mkdir()
            attachments_dir = Path(tmp) / "Files" / "Account-X" / "Attachments"
            attachments_dir.mkdir(parents=True)
            (attachments_dir / f"{sha}.heic").write_bytes(content)
            resolved = self.images.resolve_attachment_file(
                store_dir, sha, filename="photo.bmp", uti="com.example.unknown"
            )
            self.assertIsNotNone(resolved)
            self.assertTrue(resolved.endswith(f"{sha}.heic"))
            self.assertIsNotNone(
                self.images.resolve_attachment_file(
                    store_dir, sha, filename=None, uti="com.example.unknown"
                )
            )

    def test_resolve_attachment_file_searches_multiple_account_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = _tiny_png_bytes()
            sha = hashlib.sha512(content).hexdigest()
            store_dir = Path(tmp) / "Stores"
            store_dir.mkdir()
            first = Path(tmp) / "Files" / "Account-A" / "Attachments"
            second = Path(tmp) / "Files" / "Account-B" / "Attachments"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (second / f"{sha}.png").write_bytes(content)
            resolved = self.images.resolve_attachment_file(
                store_dir, sha, filename="photo.png", uti="public.png"
            )
            self.assertIsNotNone(resolved)
            self.assertIn("Account-B", resolved)

    def test_resolve_attachment_file_rejects_malformed_or_missing_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            self.assertIsNone(
                self.images.resolve_attachment_file(
                    store_dir, None, filename="photo.png"
                )
            )
            self.assertIsNone(
                self.images.resolve_attachment_file(
                    store_dir, "not-a-sha", filename="photo.png"
                )
            )
            self.assertEqual(
                self.images.resolve_attachment_file(
                    store_dir, sha.upper(), filename="photo.png"
                ),
                str(
                    Path(tmp)
                    / "Files"
                    / "Account-X"
                    / "Attachments"
                    / f"{sha}.png"
                ),
            )
        with tempfile.TemporaryDirectory() as empty:
            store_dir = Path(empty) / "Stores"
            store_dir.mkdir()
            self.assertIsNone(
                self.images.resolve_attachment_file(
                    store_dir, sha, filename="photo.png"
                )
            )

    # ── 3. Schema drift ─────────────────────────────────────────────────

    def test_q_attachments_handles_tables_without_sha_columns(self):
        db = self._attachment_db(saved_sha=False, object_sha=False)
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('legacy.png', 'public.png', 'image', 42, 0)"
        )
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('object.png', 'public.png', 100, 50, 42, 0)"
        )
        try:
            rows = self.remctl.q_attachments(db, 42)
            payload = self.remctl.attachment_rows_to_json(rows)
        finally:
            db.close()
        self.assertEqual(len(payload), 2)
        for entry in payload:
            self.assertIsNone(entry["path"])
            self.assertFalse(entry["resolved"])

    def test_preload_attachments_handles_tables_without_sha_columns(self):
        db = self._attachment_db(saved_sha=False, object_sha=False)
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('legacy.png', 'public.png', 'image', 42, 0)"
        )
        try:
            grouped = self.remctl_serialization.preload_attachments(db, [42])
        finally:
            db.close()
        self.assertEqual(len(grouped[42]), 1)

    # ── 4. detect_image_mode ────────────────────────────────────────────

    _IMAGE_ENV_KEYS = (
        "REMCTL_IMAGE_MODE",
        "TERM_PROGRAM",
        "KONSOLE_VERSION",
        "KITTY_WINDOW_ID",
        "LC_TERMINAL",
        "COLORTERM",
        "TERM",
    )

    def _detect_mode(self, env):
        with mock.patch.dict(os.environ, clear=False):
            for key in self._IMAGE_ENV_KEYS:
                os.environ.pop(key, None)
            os.environ.update(env)
            with mock.patch.object(sys.stdout, "isatty", return_value=False):
                return self.images.detect_image_mode()

    def test_detect_image_mode_override_wins(self):
        self.assertEqual(
            self._detect_mode(
                {"REMCTL_IMAGE_MODE": "halfblock", "TERM_PROGRAM": "ghostty"}
            ),
            "halfblock",
        )
        self.assertIsNone(
            self._detect_mode({"REMCTL_IMAGE_MODE": "none", "TERM_PROGRAM": "kitty"})
        )

    def test_default_image_width_scales_with_terminal(self):
        cases = [
            # (columns, mode, expected)
            (200, "kitty", 80),      # 40% of 200
            (120, "kitty", 48),      # 40% of 120
            (80, "kitty", 32),       # 40% of 80
            (40, "kitty", 24),       # floor
            (1000, "kitty", 100),    # cap
            (1000, "halfblock", 64), # halfblock cap
            (200, "halfblock", 64),  # 40% = 80, clamped to 64
        ]
        for cols, mode, expected in cases:
            with self.subTest(cols=cols, mode=mode):
                size = os.terminal_size((cols, 24))
                with mock.patch.object(os, "get_terminal_size", return_value=size):
                    self.assertEqual(self.remctl._default_image_width(mode), expected)

    def test_default_image_width_fallback_when_size_unknown(self):
        with mock.patch.object(os, "get_terminal_size", side_effect=OSError):
            self.assertEqual(self.remctl._default_image_width("kitty"), 48)
            self.assertEqual(self.remctl._default_image_width(None), 48)

    def test_explicit_image_width_still_wins(self):
        size = os.terminal_size((300, 24))
        args = SimpleNamespace(images=True, image_mode="kitty", image_width=32,
                               json=False, format="plain", verbose=False)
        with mock.patch.object(os, "get_terminal_size", return_value=size), \
             mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch.object(self.remctl.C, "enabled", True):
            mode, width = self.remctl._image_render_config(args)
        self.assertEqual((mode, width), ("kitty", 32))

    def test_unset_image_width_uses_terminal_default(self):
        size = os.terminal_size((300, 24))  # 40% = 120 -> cap 100
        args = SimpleNamespace(images=True, image_mode="kitty", image_width=None,
                               json=False, format="plain", verbose=False)
        with mock.patch.object(os, "get_terminal_size", return_value=size), \
             mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch.object(self.remctl.C, "enabled", True):
            mode, width = self.remctl._image_render_config(args)
        self.assertEqual((mode, width), ("kitty", 100))

    def test_detect_image_mode_terminals(self):
        self.assertEqual(self._detect_mode({"TERM_PROGRAM": "ghostty"}), "kitty")
        self.assertEqual(self._detect_mode({"TERM_PROGRAM": "kitty"}), "kitty")
        self.assertEqual(self._detect_mode({"TERM_PROGRAM": "WezTerm"}), "kitty")
        self.assertEqual(self._detect_mode({"KONSOLE_VERSION": "230800"}), "kitty")
        self.assertEqual(self._detect_mode({"KITTY_WINDOW_ID": "1"}), "kitty")
        self.assertEqual(self._detect_mode({"TERM_PROGRAM": "iTerm.app"}), "iterm2")
        self.assertEqual(self._detect_mode({"LC_TERMINAL": "iTerm2"}), "iterm2")
        self.assertEqual(self._detect_mode({"LC_TERMINAL": "blink"}), "iterm2")

    def test_detect_image_mode_color_fallbacks(self):
        self.assertEqual(
            self._detect_mode({"COLORTERM": "truecolor"}), "halfblock"
        )
        self.assertEqual(self._detect_mode({"COLORTERM": "24bit"}), "halfblock")
        self.assertEqual(
            self._detect_mode({"TERM": "xterm-256color"}), "halfblock"
        )
        self.assertEqual(
            self._detect_mode({"TERM": "xterm-truecolor"}), "halfblock"
        )

    def test_detect_image_mode_unknown_terminal_returns_none(self):
        with mock.patch.dict(os.environ, clear=False):
            for key in self._IMAGE_ENV_KEYS:
                os.environ.pop(key, None)
            os.environ["TERM"] = "xterm"
            with mock.patch.object(sys.stdout, "isatty", return_value=True):
                self.assertIsNone(self.images.detect_image_mode())
            with mock.patch.object(sys.stdout, "isatty", return_value=False):
                self.assertIsNone(self.images.detect_image_mode())

    # ── 5. render_attachment per mode ───────────────────────────────────

    def test_render_kitty_round_trips_png_payload(self):
        payload = _tiny_png_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            path.write_bytes(payload)
            rendered = self.images.render_attachment(path, "kitty", 8)
        self.assertIsNotNone(rendered)
        self.assertIn("\x1b_G", rendered)
        self.assertIn("a=T,t=d,f=100,c=8", rendered)
        chunks = re.findall(r"\x1b_G[^;]*;([A-Za-z0-9+/=]+)\x1b\\", rendered)
        self.assertTrue(chunks)
        self.assertEqual(base64.b64decode("".join(chunks)), payload)

    def test_render_kitty_uses_jpeg_format_106(self):
        payload = _tiny_jpeg_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.jpg"
            path.write_bytes(payload)
            rendered = self.images.render_attachment(path, "kitty", 8)
        self.assertIsNotNone(rendered)
        self.assertIn("f=106", rendered)
        chunks = re.findall(r"\x1b_G[^;]*;([A-Za-z0-9+/=]+)\x1b\\", rendered)
        self.assertEqual(base64.b64decode("".join(chunks)), payload)

    def test_render_iterm2_inline_file_escape(self):
        payload = _tiny_png_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            path.write_bytes(payload)
            rendered = self.images.render_attachment(path, "iterm2", 12)
        self.assertIsNotNone(rendered)
        self.assertTrue(rendered.startswith("\x1b]1337;File=inline=1;width=12"))
        self.assertTrue(rendered.endswith("\x07"))
        self.assertIn(base64.b64encode(payload).decode("ascii"), rendered)

    def test_render_halfblock_emits_truecolor_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            path.write_bytes(_tiny_png_bytes())
            rendered = self.images.render_attachment(path, "halfblock", 4)
        self.assertIsNotNone(rendered)
        self.assertIn("\x1b[38;2;", rendered)
        self.assertIn("\x1b[48;2;", rendered)
        self.assertIn("▀", rendered)
        self.assertIn("\x1b[0m", rendered)

    def test_render_attachment_never_raises_on_unrenderable_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("plain text, not an image")
            self.assertIsNone(self.images.render_attachment(path, "halfblock", 8))
            missing = Path(tmp) / "missing.png"
            for mode in ("kitty", "iterm2", "halfblock"):
                self.assertIsNone(self.images.render_attachment(missing, mode, 8))
        self.assertIsNone(self.images.render_attachment("/tmp/x.png", "nope", 8))
        self.assertIsNone(self.images.render_attachment("/tmp/x.png", "kitty", 0))
        self.assertIsNone(
            self.images.render_attachment("/tmp/x.png", "kitty", "wide")
        )

    def test_render_attachment_rejects_removed_ascii_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            path.write_bytes(_tiny_png_bytes())
            self.assertIsNone(self.images.render_attachment(path, "ascii", 8))
        self.assertNotIn("ascii", self.images.IMAGE_MODES)

    # ── 6. sips/BMP stdlib path ─────────────────────────────────────────

    def test_halfblock_falls_back_to_sips_when_pillow_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            path.write_bytes(_tiny_png_bytes())
            with mock.patch.object(
                self.images,
                "_pixel_rows_via_pillow",
                side_effect=ImportError("Pillow not installed"),
            ):
                rendered = self.images.render_attachment(path, "halfblock", 4)
        self.assertIsNotNone(rendered)
        self.assertIn("\x1b[38;2;", rendered)
        self.assertIn("\x1b[48;2;", rendered)
        self.assertIn("▀", rendered)

    def test_sips_path_handles_alpha_png_via_32bit_bmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alpha.png"
            path.write_bytes(_tiny_rgba_png_bytes())
            with mock.patch.object(
                self.images,
                "_pixel_rows_via_pillow",
                side_effect=ImportError("Pillow not installed"),
            ):
                pixels = self.images._pixel_rows(path, 4)
        self.assertIsNotNone(pixels)
        width, height, rows = pixels
        self.assertEqual(width, 4)
        self.assertGreaterEqual(height, 1)
        for row in rows:
            for pixel in row:
                self.assertEqual(len(pixel), 3)  # alpha flattened over black

    # ── 7. CLI guards ───────────────────────────────────────────────────

    def test_cmd_info_images_flag_is_noop_when_not_forced_and_piped(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 4, 2, NULL, 42, 0)"
        )
        try:
            plain = self._run_cmd_info(
                self._reminder_row(), db, self._info_args()
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REMCTL_IMAGES_FORCE", None)
                with_images = self._run_cmd_info(
                    self._reminder_row(),
                    db,
                    self._info_args(images=True, image_mode="halfblock"),
                )
        finally:
            db.close()
        self.assertEqual(plain, with_images)

    def test_cmd_info_force_renders_halfblock_escapes(self):
        db = self._attachment_db()
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('photo.png', 'public.png', 4, 2, ?, 42, 0)",
                (sha,),
            )
            try:
                with (
                    mock.patch.object(self.remctl, "STORE_DIR", store_dir),
                    mock.patch.dict(
                        os.environ,
                        {
                            "REMCTL_IMAGES_FORCE": "1",
                            "REMCTL_IMAGE_MODE": "halfblock",
                        },
                        clear=False,
                    ),
                ):
                    out = self._run_cmd_info(
                        self._reminder_row(),
                        db,
                        self._info_args(images=True),
                    )
            finally:
                db.close()
        self.assertIn("\x1b[38;2;", out)
        self.assertIn("Attachments (1)", out)

    def test_cmd_info_json_with_images_force_has_no_escapes(self):
        db = self._attachment_db()
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('photo.png', 'public.png', 4, 2, ?, 42, 0)",
                (sha,),
            )
            try:
                with (
                    mock.patch.object(self.remctl, "STORE_DIR", store_dir),
                    mock.patch.dict(
                        os.environ,
                        {
                            "REMCTL_IMAGES_FORCE": "1",
                            "REMCTL_IMAGE_MODE": "halfblock",
                        },
                        clear=False,
                    ),
                ):
                    out = self._run_cmd_info(
                        self._reminder_row(),
                        db,
                        self._info_args(json=True, images=True),
                    )
            finally:
                db.close()
        self.assertNotIn("\x1b", out)
        self.assertEqual(
            json.loads(out)["attachments"][0]["filename"], "photo.png"
        )

    def test_cmd_show_table_format_with_images_force_has_no_escapes(self):
        db = self._attachment_db()
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('photo.png', 'public.png', 4, 2, ?, 42, 0)",
                (sha,),
            )
            try:
                with (
                    mock.patch.object(self.remctl, "STORE_DIR", store_dir),
                    mock.patch.object(self.remctl, "open_db", return_value=db),
                    mock.patch.object(
                        self.remctl,
                        "resolve_required_list_target_or_die",
                        return_value={"id": 1, "title": "Projects"},
                    ),
                    mock.patch.object(
                        self.remctl,
                        "q_reminders",
                        return_value=[self._reminder_row()],
                    ),
                    mock.patch.object(self.remctl, "q_sections", return_value=[]),
                    mock.patch.object(
                        self.remctl,
                        "preload_extras",
                        return_value=({42: 0}, {42: []}),
                    ),
                    mock.patch.dict(
                        os.environ,
                        {
                            "REMCTL_IMAGES_FORCE": "1",
                            "REMCTL_IMAGE_MODE": "halfblock",
                        },
                        clear=False,
                    ),
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                ):
                    self.remctl.cmd_show(
                        SimpleNamespace(
                            list="Projects",
                            list_id=None,
                            completed=False,
                            json=False,
                            format="table",
                            verbose=True,
                            images=True,
                            image_mode=None,
                            image_width=32,
                        )
                    )
            finally:
                db.close()
        self.assertNotIn("\x1b[38;2;", stdout.getvalue())

    def test_cmd_info_fallback_strings_for_unresolved_and_unrenderable(self):
        db = self._attachment_db()
        # Row 1: NULL sha -> not downloaded. Row 2: resolved but not an image.
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(
                tmp, filename="notes.txt", content=b"just text"
            )
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('legacy.png', 'public.png', NULL, NULL, NULL, 42, 0)"
            )
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('notes.txt', 'public.plain-text', NULL, NULL, ?, 42, 0)",
                (sha,),
            )
            try:
                with (
                    mock.patch.object(self.remctl, "STORE_DIR", store_dir),
                    mock.patch.dict(
                        os.environ,
                        {
                            "REMCTL_IMAGES_FORCE": "1",
                            "REMCTL_IMAGE_MODE": "halfblock",
                        },
                        clear=False,
                    ),
                ):
                    out = self._run_cmd_info(
                        self._reminder_row(),
                        db,
                        self._info_args(images=True),
                    )
            finally:
                db.close()
        self.assertIn("(file not downloaded on this Mac)", out)
        self.assertIn("(preview unavailable)", out)

    # ── 8. fmt verbose hook ─────────────────────────────────────────────

    def _cmd_show_human(self, db, *, verbose, extra_env=None):
        env = {
            "REMCTL_IMAGES_FORCE": "1",
            "REMCTL_IMAGE_MODE": "halfblock",
        }
        if extra_env:
            env.update(extra_env)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl,
                "resolve_required_list_target_or_die",
                return_value={"id": 1, "title": "Projects"},
            ),
            mock.patch.object(
                self.remctl,
                "q_reminders",
                return_value=[self._reminder_row()],
            ),
            mock.patch.object(self.remctl, "q_sections", return_value=[]),
            mock.patch.object(
                self.remctl, "preload_extras", return_value=({42: 0}, {42: []})
            ),
            mock.patch.dict(os.environ, env, clear=False),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_show(
                SimpleNamespace(
                    list="Projects",
                    list_id=None,
                    completed=False,
                    json=False,
                    format=None,
                    verbose=verbose,
                    images=True,
                    image_mode=None,
                    image_width=32,
                )
            )
        return stdout.getvalue()

    def test_cmd_show_images_verbose_renders_attachment_block(self):
        db = self._attachment_db()
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('photo.png', 'public.png', 4, 2, ?, 42, 0)",
                (sha,),
            )
            try:
                with mock.patch.object(self.remctl, "STORE_DIR", store_dir):
                    out = self._cmd_show_human(db, verbose=True)
            finally:
                db.close()
        self.assertIn("Attachment: photo.png (image)", out)
        self.assertIn("\x1b[38;2;", out)

    def test_cmd_show_images_without_verbose_stays_text_only(self):
        db = self._attachment_db()
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                "('photo.png', 'public.png', 4, 2, ?, 42, 0)",
                (sha,),
            )
            try:
                with mock.patch.object(self.remctl, "STORE_DIR", store_dir):
                    out = self._cmd_show_human(db, verbose=False)
            finally:
                db.close()
        self.assertNotIn("Attachment:", out)
        self.assertNotIn("\x1b[38;2;", out)

    # ── 9. Byte-compat ──────────────────────────────────────────────────

    def test_cmd_info_human_output_without_images_has_no_escapes_or_fallbacks(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 640, 480, NULL, 42, 0)"
        )
        try:
            self.remctl.C.enabled = False
            out = self._run_cmd_info(
                self._reminder_row(), db, self._info_args()
            )
        finally:
            db.close()
        self.assertNotIn("\x1b", out)
        self.assertNotIn("(file not downloaded on this Mac)", out)
        self.assertNotIn("(preview unavailable)", out)
        self.assertIn("  Attachments (1):\n    - photo.png (image)\n", out)

    def test_cmd_info_images_disabled_and_enabled_unforced_are_byte_identical(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('doc.pdf', 'com.adobe.pdf', 'file', NULL, 42, 0)"
        )
        try:
            self.remctl.C.enabled = False
            baseline = self._run_cmd_info(
                self._reminder_row(), db, self._info_args()
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REMCTL_IMAGES_FORCE", None)
                flagged = self._run_cmd_info(
                    self._reminder_row(),
                    db,
                    self._info_args(images=True, image_mode="halfblock"),
                )
        finally:
            db.close()
        self.assertEqual(baseline, flagged)

    # ── 8. Review fixes: kitty q=2, halfblock odd heights, memo, caps ───

    def test_kitty_escape_suppresses_terminal_response_on_first_chunk(self):
        payload = _tiny_png_bytes()
        rendered = self.images._kitty_escape(payload, 8, 100)
        self.assertIsNotNone(rendered)
        chunks = re.findall(r"\x1b_G([^;]*);[A-Za-z0-9+/=]*\x1b\\", rendered)
        self.assertTrue(chunks)
        self.assertIn("q=2", chunks[0])
        for continuation in chunks[1:]:
            self.assertNotIn("q=2", continuation)
        # Multi-chunk payloads (b64 > 4096) still only quiet the first.
        big = _tiny_png_bytes() * 4000
        rendered_big = self.images._kitty_escape(big, 8, 100)
        big_chunks = re.findall(r"\x1b_G([^;]*);[A-Za-z0-9+/=]*\x1b\\", rendered_big)
        self.assertGreater(len(big_chunks), 1)
        self.assertIn("q=2", big_chunks[0])
        for continuation in big_chunks[1:]:
            self.assertNotIn("q=2", continuation)

    def test_halfblock_renders_last_row_on_odd_heights(self):
        rows = [
            [(255, 0, 0)] * 2,
            [(0, 255, 0)] * 2,
            [(0, 0, 255)] * 2,
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                self.images, "_pixel_rows", return_value=(2, 3, rows)
            ),
        ):
            path = Path(tmp) / "odd.png"
            path.write_bytes(b"not-really-a-png")
            rendered = self.images.render_attachment(path, "halfblock", 2)
        self.assertIsNotNone(rendered)
        lines = rendered.split("\n")
        self.assertEqual(len(lines), 2)
        # The unpaired bottom row composites over black, not dropped.
        self.assertIn("38;2;0;0;255m", lines[1])
        self.assertIn("48;2;0;0;0m", lines[1])

    def test_sha512_memo_avoids_rehashing_same_file(self):
        content = _tiny_png_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memo.png"
            path.write_bytes(content)
            self.images._sha512_memo.clear()
            try:
                first = self.images._sha512_of(path)
                with mock.patch.object(
                    self.images.hashlib, "sha512", side_effect=AssertionError("rehashed")
                ):
                    second = self.images._sha512_of(path)
            finally:
                self.images._sha512_memo.clear()
        self.assertEqual(first, hashlib.sha512(content).hexdigest())
        self.assertEqual(second, first)
        # Changing the file (new mtime/size key) rehashes instead of serving stale.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memo.png"
            path.write_bytes(content)
            self.images._sha512_memo.clear()
            try:
                before = self.images._sha512_of(path)
                path.write_bytes(content + b"x")
                after = self.images._sha512_of(path)
            finally:
                self.images._sha512_memo.clear()
        self.assertNotEqual(before, after)

    def test_resolve_attachment_file_uses_memo_across_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir, sha, _ = self._attachment_store(tmp)
            self.images._sha512_memo.clear()
            try:
                first = self.images.resolve_attachment_file(store_dir, sha)
                with mock.patch.object(
                    self.images.hashlib, "sha512", side_effect=AssertionError("rehashed")
                ):
                    second = self.images.resolve_attachment_file(store_dir, sha)
            finally:
                self.images._sha512_memo.clear()
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_oversized_files_skip_render_but_stay_in_json(self):
        content = _tiny_png_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.png"
            path.write_bytes(content)
            real_stat = os.stat(path).st_size
            with mock.patch.object(self.images, "MAX_IMAGE_BYTES", real_stat - 1):
                for mode in ("kitty", "iterm2", "halfblock"):
                    self.assertIsNone(self.images.render_attachment(path, mode, 8))
                # Hashing is capped too: resolution fails, JSON keeps metadata.
                self.assertIsNone(self.images._sha512_of(path))

    def test_attachment_sha_capability_cache_hits_across_connections(self):
        db = self._attachment_db()
        counting = self._counting_db(db)
        try:
            self.remctl._attachment_sha_capability_cache.clear()
            try:
                first = self.remctl._attachment_sha_capability(counting)
                baseline = len(counting.queries)
                second = self.remctl._attachment_sha_capability(counting)
            finally:
                self.remctl._attachment_sha_capability_cache.clear()
        finally:
            db.close()
        self.assertEqual(first, second)
        # Second call is served from the module-level cache: zero queries.
        self.assertEqual(len(counting.queries), baseline)
        self.assertGreater(baseline, 0)

    # ── 9. Batch attachment loading (no N+1) ────────────────────────────

    def _counting_db(self, db):
        class CountingConnection:
            def __init__(self, inner):
                self._inner = inner
                self.queries = []

            def execute(self, sql, *args, **kwargs):
                self.queries.append(sql)
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        return CountingConnection(db)

    def test_show_json_uses_constant_attachment_queries(self):
        db = self._attachment_db()
        for pk in (42, 43, 44, 45):
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                f"('photo{pk}.png', 'public.png', 640, 480, NULL, {pk}, 0)"
            )
        rows = [self._reminder_row(pk=pk, ckid=f"REM-{pk}") for pk in (42, 43, 44, 45)]
        counting = self._counting_db(db)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=counting),
                mock.patch.object(
                    self.remctl,
                    "resolve_required_list_target_or_die",
                    return_value={"id": 1, "title": "Projects"},
                ),
                mock.patch.object(self.remctl, "q_reminders", return_value=rows),
                mock.patch.object(self.remctl, "q_sections", return_value=[]),
                mock.patch.object(self.remctl, "q_rich_link", return_value=None),
                mock.patch.object(self.remctl, "q_assignment", return_value=None),
                mock.patch.object(
                    self.remctl,
                    "preload_extras",
                    return_value=(
                        {pk: 0 for pk in (42, 43, 44, 45)},
                        {pk: [] for pk in (42, 43, 44, 45)},
                    ),
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_show(
                    SimpleNamespace(
                        list="Projects",
                        list_id=None,
                        completed=False,
                        json=True,
                        format=None,
                        verbose=False,
                        images=False,
                        image_mode=None,
                        image_width=32,
                    )
                )
        finally:
            db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 4)
        for item in payload:
            self.assertEqual(len(item["attachments"]), 1)
        attachment_queries = [
            sql
            for sql in counting.queries
            if sql.lstrip().upper().startswith("SELECT")
            and "ZFILENAME" in sql
            and "ZATTACHMENTTYPERAWVALUE" in sql
        ]
        # Batch path: one query per backing table regardless of page size.
        self.assertLessEqual(len(attachment_queries), 2)

    def test_to_dict_batch_map_matches_per_item_fallback(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 640, 480, NULL, 42, 0)"
        )
        row = self._reminder_row()
        try:
            with (
                mock.patch.object(self.remctl, "q_rich_link", return_value=None),
                mock.patch.object(self.remctl, "q_assignment", return_value=None),
            ):
                sc, ht = {42: 0}, {42: []}
                fallback = self.remctl.to_dict(row, db, _sc=sc, _ht=ht)
                batch = self.remctl.to_dict(
                    row, db, _sc=sc, _ht=ht,
                    _att=self.remctl.preload_attachments(db, [42]),
                )
        finally:
            db.close()
        self.assertEqual(fallback, batch)

    def test_hydrate_reminder_detail_skips_attachment_requery(self):
        db = self._attachment_db()
        try:
            payload = {"id": 42, "attachments": [{"filename": "photo.png"}]}
            with (
                mock.patch.object(
                    self.remctl, "q_attachments", side_effect=AssertionError("re-queried")
                ),
                mock.patch.object(self.remctl, "q_alarms", return_value=[]),
            ):
                result = self.remctl.hydrate_reminder_detail(db, payload, 42)
        finally:
            db.close()
        self.assertEqual(result["attachments"], [{"filename": "photo.png"}])

    def test_hydrate_reminder_detail_fills_attachments_when_absent(self):
        db = self._attachment_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('photo.png', 'public.png', 640, 480, NULL, 42, 0)"
        )
        try:
            with mock.patch.object(self.remctl, "q_alarms", return_value=[]):
                result = self.remctl.hydrate_reminder_detail(db, {"id": 42}, 42)
        finally:
            db.close()
        self.assertEqual(len(result["attachments"]), 1)
        self.assertEqual(result["attachments"][0]["filename"], "photo.png")


class CapabilityHostHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_help_test", "remctl")

    def test_root_and_setup_help_describe_one_signed_permission_host(self):
        parser, sub = self.remctl.build_parser()
        root_help = parser.format_help()
        self.assertIn("signed RemCTL Capability Host", root_help)
        self.assertIn("Python 3.10+ CLI", root_help)
        self.assertIn("access.effective and protocol v2", root_help)
        self.assertNotIn("writes via remctl-bridge", root_help)
        self.assertIn("add/edit/reminder-move", root_help)
        self.assertIn("section-create/section-rename/section-delete", root_help)
        self.assertIn("group-create/group-edit/group-delete", root_help)
        self.assertIn("template-create/template-apply/template-delete", root_help)
        self.assertNotIn("group-*", root_help)
        self.assertNotIn("template-*", root_help)
        self.assertNotIn("group-info", root_help.split("Private metadata writes", 1)[1])
        self.assertNotIn("template-info", root_help.split("Private metadata writes", 1)[1])
        self.assertIn(f"{self.remctl.CLI_NAME} setup --shell auto", root_help)
        self.assertNotIn(f"{self.remctl.CLI_NAME} setup --shell auto --doctor", root_help)

        setup_help = " ".join(sub.choices["setup"].format_help().split())
        self.assertIn("does not grant macOS permissions", setup_help)
        self.assertIn("remctl onboard", setup_help)
        self.assertIn("opens the host-only FDA guide if needed", setup_help)
        self.assertIn("restart the host before doctor", setup_help)
        self.assertIn("remctl doctor --for-agent", setup_help)
        self.assertIn("unchanged existing install", setup_help)
        self.assertIn("onboard only if doctor reports host permission trouble", setup_help)
        self.assertIn("upgrade or reinstall, run `./install.sh`, then doctor", setup_help)
        self.assertIn("only to reopen the guide for repair", setup_help)
        self.assertIn("remctl setup --shell auto", setup_help)
        self.assertNotIn("remctl setup --shell auto --doctor", setup_help)
        self.assertIn("unchanged installs or completed upgrades/reinstalls only, after any FDA restart", setup_help)

        cli_name = self.remctl.CLI_NAME
        first_install = root_help.split("First install:", 1)[1].split("Upgrade or reinstall:", 1)[0]
        self.assertLess(first_install.index(f"{cli_name} onboard"), first_install.index("restart the signed host"))
        self.assertLess(first_install.index("restart the signed host"), first_install.index(f"{cli_name} doctor --for-agent"))
        upgrade = root_help.split("Upgrade or reinstall:", 1)[1].split("Repair:", 1)[0]
        self.assertLess(upgrade.index(f"{cli_name} doctor --for-agent"), upgrade.index(f"{cli_name} onboard only if doctor"))
        self.assertIn(f"Repair: {cli_name} permissions full-disk-access", root_help)

    def test_permission_and_diagnostic_help_never_target_callers(self):
        _, sub = self.remctl.build_parser()
        onboard = sub.choices["onboard"].format_help()
        doctor = sub.choices["doctor"].format_help()
        permissions = sub.choices["permissions"].format_help()
        onboard_compact = " ".join(onboard.split())
        permissions_compact = " ".join(permissions.split())

        self.assertIn("cannot grant Full Disk Access", onboard)
        self.assertIn("request Reminders and Automation access", onboard)
        self.assertIn("still requests Reminders and Automation", onboard_compact)
        self.assertIn("does not open the FDA helper", onboard_compact)
        self.assertIn("`access.effective` is authoritative", doctor)
        self.assertIn("`capabilityHost.fullReady`", doctor)
        self.assertIn("remctl-private helper protocol version (required: v2)", doctor)
        self.assertIn("only the exact installed signed host", permissions)
        self.assertIn("JSON reports the validated target without opening UI", permissions_compact)
        self.assertIn("reports state and targets but does not open", permissions_compact)
        self.assertIn("Explicit direct mode is self-managed", permissions)
        self.assertNotIn("direct CLI reads", onboard)
        self.assertNotIn("Terminal can pass", doctor)

    def test_hosted_command_help_names_signed_execution_target(self):
        _, sub = self.remctl.build_parser()
        self.assertIn("Signed-host database reads", sub.choices["show"].format_help())
        self.assertIn("signed host's EventKit bridge", sub.choices["add"].format_help())
        self.assertIn("inside the signed host by default", sub.choices["edit"].format_help())
        self.assertIn("inside the signed host", sub.choices["flag"].description or sub.choices["flag"].format_help())

    def test_zsh_and_fish_completion_descriptions_match_host_contract(self):
        for shell in ("zsh", "fish"):
            script = self.remctl.get_completion_script(shell)
            self.assertIn("Request signed-host Reminders and Automation access", script)
            self.assertIn("Diagnose signed-host and effective access", script)
            self.assertIn("Reopen the signed host Full Disk Access guide for repair", script)
            self.assertIn("authoritative access.effective readiness", script)
            self.assertIn("signed-host or direct read-only fallback", script)
            self.assertNotIn("TCC guidance", script)


@unittest.skipUnless(sys.platform == "darwin", "AppKit helper requires macOS")
class PermissionHelperTests(unittest.TestCase):
    @staticmethod
    def _create_host_app(root, host_binary, *, name="RemCTL Capability Host.app", sign=False):
        app = Path(root) / name
        executable = app / "Contents" / "MacOS" / "RemCTL Capability Host"
        executable.parent.mkdir(parents=True)
        shutil.copy2(host_binary, executable)
        info = {
            "CFBundleIdentifier": "net.macstories.remctl.capability-host",
            "CFBundleExecutable": "RemCTL Capability Host",
            "CFBundleName": "RemCTL Capability Host",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
        }
        with (app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(info, handle)
        if sign:
            identity = "-" if sign is True else str(sign)
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--deep", "--sign", identity, str(app)],
                check=True,
                capture_output=True,
                text=True,
            )
        return app

    @classmethod
    def setUpClass(cls):
        swiftc = shutil.which("swiftc")
        if swiftc is None or not Path("/usr/bin/codesign").exists():
            raise unittest.SkipTest("swiftc or codesign is unavailable")
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp_dir.name)
        cls.source = Path(__file__).resolve().parents[1] / "remctl-permissions.swift"
        cls.binary = cls.root / "remctl-permissions"
        subprocess.run(
            [
                swiftc,
                "-framework",
                "AppKit",
                "-framework",
                "Foundation",
                "-o",
                str(cls.binary),
                str(cls.source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        host_source = cls.root / "signed-host.swift"
        host_source.write_text("import Foundation\n")
        host_binary = cls.root / "signed-host"
        cls.host_binary = host_binary
        subprocess.run(
            [swiftc, "-o", str(host_binary), str(host_source)],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.valid_app = cls._create_host_app(cls.root / "valid", host_binary, sign=True)
        cls.unsigned_app = cls._create_host_app(cls.root / "unsigned", host_binary)
        cls.invalid_signature_app = cls._create_host_app(
            cls.root / "invalid-signature", host_binary, sign=True
        )
        with (
            cls.invalid_signature_app / "Contents" / "MacOS" / "RemCTL Capability Host"
        ).open("ab") as executable:
            executable.write(b"tampered")
        cls.wrong_name_app = cls._create_host_app(
            cls.root / "wrong-name", host_binary, name="Python.app", sign=True
        )
        cls.marker = cls.binary.parent / ".remctl-capability-host-app"
        cls.identity_marker = cls.binary.parent / ".remctl-capability-host-signing-identity"

    def setUp(self):
        if self.marker.exists() or self.marker.is_symlink():
            self.marker.unlink()
        self.marker.write_text(f"{self.valid_app}\n")
        self.marker.chmod(0o600)
        self.identity_marker.write_text("0" * 40 + "\n")
        self.identity_marker.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls._temp_dir.cleanup()

    def _run_target(self, path, *, targets=1):
        args = [str(self.binary)]
        for _ in range(targets):
            args.extend([
                "--target",
                "RemCTL Capability Host.app",
                str(path),
                "Signed host",
            ])
        args.append("--validate-only")
        return subprocess.run(args, capture_output=True, text=True, timeout=5)

    def test_direct_launch_without_target_fails_closed(self):
        result = subprocess.run(
            [str(self.binary)],
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, "")
        self.assertIn("exactly one canonical", result.stderr)
        self.assertIn("remctl permissions full-disk-access", result.stderr)
        self.assertNotIn("/usr/bin/python3", result.stderr)
        self.assertNotIn("process", result.stderr.lower())

    def test_direct_launch_rejects_explicit_python_target(self):
        result = subprocess.run(
            [
                str(self.binary),
                "--target",
                "Current Python interpreter",
                "/usr/bin/python3",
                "Caller access",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, "")
        self.assertIn("RemCTL Capability Host.app", result.stderr)
        self.assertNotIn("/usr/bin/python3", result.stderr)

    def test_ad_hoc_signed_marker_target_fails_closed(self):
        result = self._run_target(self.valid_app)
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, "")
        self.assertIn("stable signing identity", result.stderr)

    def test_matching_preserved_development_identity_passes_preflight(self):
        identities = subprocess.run(
            ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        match = re.search(r'(?m)^\s*\d+\)\s+([0-9A-F]{40})\s+"Apple Development:', identities)
        if match is None:
            self.skipTest("Apple Development signing identity is unavailable")
        development_identity = match.group(1)
        stable_app = self._create_host_app(
            self.root / "stable", self.host_binary, sign=development_identity
        )
        self.marker.write_text(f"{stable_app}\n")
        self.identity_marker.write_text(f"{development_identity}\n")
        result = self._run_target(stable_app)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "valid\n")

    def test_missing_preserved_identity_fails_closed(self):
        self.identity_marker.unlink()
        result = self._run_target(self.valid_app)
        self.assertEqual(result.returncode, 64)
        self.assertIn("preserved signing identity is missing or unsafe", result.stderr)

    def test_unsafe_preserved_identity_marker_fails_closed(self):
        self.identity_marker.chmod(0o666)
        result = self._run_target(self.valid_app)
        self.assertEqual(result.returncode, 64)
        self.assertIn("preserved signing identity is missing or unsafe", result.stderr)

    def test_multiple_targets_fail_closed(self):
        result = self._run_target(self.valid_app, targets=2)
        self.assertEqual(result.returncode, 64)

    def test_wrong_named_signed_app_fails_closed(self):
        self.marker.write_text(f"{self.wrong_name_app}\n")
        result = self._run_target(self.wrong_name_app)
        self.assertEqual(result.returncode, 64)

    def test_same_basename_nonexistent_app_fails_closed(self):
        missing = self.root / "missing" / "RemCTL Capability Host.app"
        self.marker.write_text(f"{missing}\n")
        result = self._run_target(missing)
        self.assertEqual(result.returncode, 64)

    def test_same_basename_unsigned_app_fails_closed(self):
        self.marker.write_text(f"{self.unsigned_app}\n")
        result = self._run_target(self.unsigned_app)
        self.assertEqual(result.returncode, 64)

    def test_same_basename_invalid_signature_fails_closed(self):
        self.marker.write_text(f"{self.invalid_signature_app}\n")
        result = self._run_target(self.invalid_signature_app)
        self.assertEqual(result.returncode, 64)

    def test_noncanonical_target_and_marker_fail_closed(self):
        segment = self.valid_app.parent / "segment"
        segment.mkdir(exist_ok=True)
        noncanonical = segment / ".." / self.valid_app.name
        self.marker.write_text(f"{noncanonical}\n")
        result = self._run_target(noncanonical)
        self.assertEqual(result.returncode, 64)

    def test_symlink_target_fails_closed(self):
        link_root = self.root / "symlink"
        link_root.mkdir(exist_ok=True)
        link = link_root / "RemCTL Capability Host.app"
        if not link.exists():
            link.symlink_to(self.valid_app, target_is_directory=True)
        self.marker.write_text(f"{link}\n")
        result = self._run_target(link)
        self.assertEqual(result.returncode, 64)

    def test_unsafe_marker_permissions_fail_closed(self):
        self.marker.chmod(0o666)
        result = self._run_target(self.valid_app)
        self.assertEqual(result.returncode, 64)

    def test_helper_source_has_no_implicit_python_target(self):
        source = self.source.read_text()
        self.assertNotIn("/usr/bin/python3", source)
        self.assertNotIn("Grant Full Disk Access to the processes", source)
        self.assertNotIn("Store readable", source)
        self.assertNotIn("Access verified", source)
        self.assertIn("Verify after restarting the signed host", source)


class ImageFlagParsingTests(unittest.TestCase):
    """Global image flags must work before AND after the subcommand."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def _parse(self, argv):
        parser, sub = self.remctl.build_parser()
        return self.remctl.parse_cli_args(parser, sub, argv)

    def test_root_help_formats_literal_image_width_percentage(self):
        parser, _ = self.remctl.build_parser()

        help_text = parser.format_help()

        self.assertIn("default: ~40% of", help_text)
        self.assertIn("terminal width, capped 24-100", help_text)

    def test_images_before_subcommand_survives_redeclaration(self):
        for argv in (
            ["--images", "info", "847"],
            ["--images", "today"],
            ["--images", "show", "Work"],
            ["--images", "search", "prints"],
        ):
            a = self._parse(argv)
            self.assertTrue(a.images, argv)

    def test_images_after_subcommand(self):
        for argv in (
            ["info", "847", "--images"],
            ["today", "--images"],
            ["show", "Work", "--images"],
        ):
            a = self._parse(argv)
            self.assertTrue(a.images, argv)

    def test_image_width_before_subcommand_is_not_mistaken_for_command(self):
        a = self._parse(["--image-width", "64", "today"])
        self.assertEqual(a.image_width, 64)
        self.assertEqual(a.cmd, "today")

    def test_image_mode_before_subcommand(self):
        a = self._parse(["--image-mode", "kitty", "show", "Work"])
        self.assertEqual(a.image_mode, "kitty")
        self.assertEqual(a.cmd, "show")
        self.assertEqual(a.list, "Work")

    def test_image_flags_after_subcommand(self):
        a = self._parse(["today", "--image-mode", "halfblock", "--image-width", "48"])
        self.assertEqual(a.image_mode, "halfblock")
        self.assertEqual(a.image_width, 48)

    def test_image_flags_equals_form_before_subcommand(self):
        a = self._parse(["--image-mode=iterm2", "--image-width=24", "today"])
        self.assertEqual(a.image_mode, "iterm2")
        self.assertEqual(a.image_width, 24)
        self.assertEqual(a.cmd, "today")

    def test_before_and_after_combine_last_wins(self):
        a = self._parse(
            ["--images", "--image-mode", "kitty", "info", "847", "--image-mode", "halfblock"]
        )
        self.assertTrue(a.images)
        self.assertEqual(a.image_mode, "halfblock")
        self.assertEqual(a.cmd, "info")

    def test_removed_ascii_image_mode_is_rejected(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            self._parse(["--image-mode", "ascii", "today"])
        self.assertEqual(ctx.exception.code, 2)

    def test_defaults_unset_when_no_image_flags(self):
        a = self._parse(["today"])
        self.assertFalse(a.images)
        self.assertIsNone(a.image_mode)
        self.assertIsNone(a.image_width)

    def test_unknown_command_still_rejected(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            self._parse(["frobnicate"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unknown command", stderr.getvalue())


class TrailingBadgeTests(unittest.TestCase):
    """Trailing 🔗/🌄 one-liner badges: human output only, batch loaded."""

    IMAGE_EMOJI = "\U0001F304"
    LINK_EMOJI = "\U0001F517"

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_badge_test", "remctl")

    def setUp(self):
        self._color_enabled = self.remctl.C.enabled
        self.remctl.C.enabled = False
        self.remctl._attachment_sha_capability_cache.clear()

    def tearDown(self):
        self.remctl.C.enabled = self._color_enabled
        self.remctl._attachment_sha_capability_cache.clear()

    @staticmethod
    def _reminder_row(pk=42, title="Badge target"):
        return {
            "Z_PK": pk,
            "ZTITLE": title,
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None,
            "ZDUEDATE": None,
            "ZDISPLAYDATEDATE": None,
            "ZALLDAY": 0,
            "ZCOMPLETIONDATE": None,
            "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None,
            "ZLIST": 1,
            "ZICSURL": None,
            "ZCKIDENTIFIER": f"REM-{pk}",
            "list_name": "Projects",
            "recurrence_frequency": None,
            "recurrence_interval": None,
            "recurrence_count": None,
            "recurrence_end_date": None,
            "recurrence_days_of_week": None,
            "recurrence_days_of_month": None,
            "recurrence_months_of_year": None,
            "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None,
            "recurrence_set_positions": None,
        }

    @staticmethod
    def _badge_db(*, minimal=False):
        """In-memory fixture with attachment/url/subtask/hashtag tables."""
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE ZREMCDREMINDER ("
            "Z_PK INTEGER, ZPARENTREMINDER INTEGER, "
            "ZMARKEDFORDELETION INTEGER, ZCOMPLETED INTEGER)"
        )
        if minimal:
            return db
        db.execute(
            "CREATE TABLE ZREMCDSAVEDATTACHMENT ("
            "ZFILENAME TEXT, ZUTI TEXT, ZATTACHMENTTYPERAWVALUE TEXT, "
            "ZSHA512SUM TEXT, ZREMINDER INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        db.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, ZFILENAME TEXT, ZUTI TEXT, ZWIDTH INTEGER, ZHEIGHT INTEGER, "
            "ZSHA512SUM TEXT, ZURL TEXT, ZREMINDER2 INTEGER, ZREMINDER3 INTEGER, "
            "ZHASHTAGLABEL INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        db.execute(
            "CREATE TABLE ZREMCDHASHTAGLABEL (Z_PK INTEGER, ZNAME TEXT)"
        )
        return db

    def _fmt(self, row, db, pks=None):
        pks = pks if pks is not None else [row["Z_PK"]]
        sc, ht = self.remctl.preload_extras(db, pks)
        ind = self.remctl.preload_indicators(db, pks)
        # Fully populate the maps the way list call sites do: key-present
        # maps mean fmt never falls back to per-item queries.
        sc = {pk: sc.get(pk, 0) for pk in pks}
        ht = {pk: ht.get(pk, []) for pk in pks}
        return self.remctl._strip_ansi(
            self.remctl.fmt(row, db=db, verbose=False, _sc=sc, _ht=ht, _ind=ind)
        )

    def test_fmt_badge_for_image_attachment(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(1, 'photo.png', 'public.png', 640, 480, NULL, NULL, 42, NULL, NULL, 0)"
        )
        try:
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertIn(self.IMAGE_EMOJI, line)
        self.assertNotIn(self.LINK_EMOJI, line)
        self.assertTrue(line.endswith(f" {self.IMAGE_EMOJI}"))

    def test_fmt_badge_for_saved_image_attachment(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('scan.png', 'public.png', 'image', NULL, 42, 0)"
        )
        try:
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertIn(self.IMAGE_EMOJI, line)
        self.assertNotIn(self.LINK_EMOJI, line)

    def test_fmt_badge_for_rich_link_row(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(2, NULL, NULL, NULL, NULL, NULL, 'https://example.com', 42, NULL, NULL, 0)"
        )
        try:
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertIn(self.LINK_EMOJI, line)
        self.assertNotIn(self.IMAGE_EMOJI, line)

    def test_fmt_badge_for_legacy_url_saved_attachment(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "(NULL, NULL, 'url', NULL, 42, 0)"
        )
        try:
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertIn(self.LINK_EMOJI, line)
        self.assertNotIn(self.IMAGE_EMOJI, line)

    def test_fmt_badges_both_in_link_then_image_order(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(1, 'photo.png', 'public.png', 640, 480, NULL, NULL, 42, NULL, NULL, 0)"
        )
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(2, NULL, NULL, NULL, NULL, NULL, 'https://example.com', 42, NULL, NULL, 0)"
        )
        try:
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertTrue(
            line.endswith(f" {self.LINK_EMOJI} {self.IMAGE_EMOJI}"), line
        )

    def test_fmt_no_badges_without_attachments_or_links(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('doc.pdf', 'com.adobe.pdf', 'file', NULL, 42, 0)"
        )
        try:
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertNotIn(self.IMAGE_EMOJI, line)
        self.assertNotIn(self.LINK_EMOJI, line)

    def test_fmt_badges_come_after_subtask_count(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDREMINDER VALUES (100, 42, 0, 0)"
        )
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('scan.png', 'public.png', 'image', NULL, 42, 0)"
        )
        try:
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertIn("[1 subtask]", line)
        self.assertTrue(
            line.endswith(f"[1 subtask] {self.IMAGE_EMOJI}"), line
        )

    def test_fmt_badges_present_on_completed_reminder(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('scan.png', 'public.png', 'image', NULL, 42, 0)"
        )
        row = self._reminder_row()
        row["ZCOMPLETED"] = 1
        try:
            line = self._fmt(row, db)
        finally:
            db.close()
        self.assertIn("[x]", line)
        self.assertTrue(line.endswith(f" {self.IMAGE_EMOJI}"), line)

    def test_fmt_badge_fallback_queries_when_map_is_none(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(2, NULL, NULL, NULL, NULL, NULL, 'https://example.com', 42, NULL, NULL, 0)"
        )
        try:
            line = self.remctl._strip_ansi(
                self.remctl.fmt(
                    self._reminder_row(), db=db, verbose=False,
                    _sc={42: 0}, _ht={42: []},
                )
            )
        finally:
            db.close()
        self.assertIn(self.LINK_EMOJI, line)

    def test_preload_indicators_flags_and_soft_deletes(self):
        db = self._badge_db()
        # Image for 42, link for 43, soft-deleted rows for 44 ignored.
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(1, 'a.png', 'public.png', 10, 10, NULL, NULL, 42, NULL, NULL, 0)"
        )
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(2, NULL, NULL, NULL, NULL, NULL, 'https://x.example', 43, NULL, NULL, 0)"
        )
        db.execute(
            "INSERT INTO ZREMCDSAVEDATTACHMENT VALUES "
            "('b.png', 'public.png', 'image', NULL, 44, 1)"
        )
        try:
            ind = self.remctl.preload_indicators(db, [42, 43, 44])
        finally:
            db.close()
        self.assertEqual(ind.get(42), {"image": True, "link": False})
        self.assertEqual(ind.get(43), {"image": False, "link": True})
        self.assertNotIn(44, ind)

    def test_preload_crash_safety_minimal_fixture(self):
        # Missing attachment/url/hashtag tables: no badges, no exception,
        # subtask counts still work.
        db = self._badge_db(minimal=True)
        db.execute("INSERT INTO ZREMCDREMINDER VALUES (100, 42, 0, 0)")
        try:
            sc, ht = self.remctl.preload_extras(db, [42])
            ind = self.remctl.preload_indicators(db, [42])
            line = self._fmt(self._reminder_row(), db)
        finally:
            db.close()
        self.assertEqual(sc, {42: 1})
        self.assertEqual(ht, {})
        self.assertEqual(ind, {})
        self.assertIn("[1 subtask]", line)
        self.assertNotIn(self.IMAGE_EMOJI, line)
        self.assertNotIn(self.LINK_EMOJI, line)

    def test_show_json_payload_has_no_badge_emoji(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(1, 'photo.png', 'public.png', 640, 480, NULL, NULL, 42, NULL, NULL, 0)"
        )
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(2, NULL, NULL, NULL, NULL, NULL, 'https://example.com', 42, NULL, NULL, 0)"
        )
        rows = [self._reminder_row()]
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(
                    self.remctl,
                    "resolve_required_list_target_or_die",
                    return_value={"id": 1, "title": "Projects"},
                ),
                mock.patch.object(self.remctl, "q_reminders", return_value=rows),
                mock.patch.object(self.remctl, "q_sections", return_value=[]),
                mock.patch.object(self.remctl, "q_rich_link", return_value=None),
                mock.patch.object(self.remctl, "q_assignment", return_value=None),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_show(
                    SimpleNamespace(
                        list="Projects",
                        list_id=None,
                        completed=False,
                        json=True,
                        format=None,
                        verbose=False,
                        images=False,
                        image_mode=None,
                        image_width=32,
                    )
                )
        finally:
            db.close()
        payload = stdout.getvalue()
        json.loads(payload)
        self.assertNotIn(self.IMAGE_EMOJI, payload)
        self.assertNotIn(self.LINK_EMOJI, payload)

    def test_table_mode_has_no_badge_emoji(self):
        db = self._badge_db()
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(1, 'photo.png', 'public.png', 640, 480, NULL, NULL, 42, NULL, NULL, 0)"
        )
        db.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "(2, NULL, NULL, NULL, NULL, NULL, 'https://example.com', 42, NULL, NULL, 0)"
        )
        try:
            rows_data = self.remctl.reminders_to_table_data(
                [self._reminder_row()], db=db
            )
            table = self.remctl.fmt_table(rows_data)
        finally:
            db.close()
        self.assertNotIn(self.IMAGE_EMOJI, table)
        self.assertNotIn(self.LINK_EMOJI, table)

    def test_list_render_uses_constant_indicator_queries(self):
        db = self._badge_db()
        pks = (42, 43, 44, 45, 46)
        for pk in pks:
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                f"({pk}, 'photo{pk}.png', 'public.png', 640, 480, NULL, NULL, {pk}, NULL, NULL, 0)"
            )
            db.execute(
                "INSERT INTO ZREMCDOBJECT VALUES "
                f"({pk} + 1000, NULL, NULL, NULL, NULL, NULL, 'https://example.com/{pk}', {pk}, NULL, NULL, 0)"
            )
        rows = [self._reminder_row(pk=pk) for pk in pks]

        class CountingConnection:
            def __init__(self, inner):
                self._inner = inner
                self.queries = []

            def execute(self, sql, *args, **kwargs):
                self.queries.append(sql)
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        counting = CountingConnection(db)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=counting),
                mock.patch.object(
                    self.remctl,
                    "resolve_required_list_target_or_die",
                    return_value={"id": 1, "title": "Projects"},
                ),
                mock.patch.object(self.remctl, "q_reminders", return_value=rows),
                mock.patch.object(self.remctl, "q_sections", return_value=[]),
                mock.patch.object(self.remctl, "q_assignment", return_value=None),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_show(
                    SimpleNamespace(
                        list="Projects",
                        list_id=None,
                        completed=False,
                        json=False,
                        format=None,
                        verbose=False,
                        images=False,
                        image_mode=None,
                        image_width=32,
                    )
                )
        finally:
            db.close()
        out = self.remctl._strip_ansi(stdout.getvalue())
        # Every rendered row carries both badges (link first, then image).
        badge_lines = [
            line for line in out.splitlines()
            if self.LINK_EMOJI in line or self.IMAGE_EMOJI in line
        ]
        self.assertEqual(len(badge_lines), len(pks))
        for line in badge_lines:
            self.assertTrue(
                line.rstrip().endswith(f"{self.LINK_EMOJI} {self.IMAGE_EMOJI}"),
                line,
            )
        indicator_queries = [
            sql
            for sql in counting.queries
            if sql.lstrip().upper().startswith("SELECT")
            and ("ZATTACHMENTTYPERAWVALUE" not in sql)
            and (
                "FROM ZREMCDSAVEDATTACHMENT" in sql
                or ("FROM ZREMCDOBJECT" in sql and "ZURL" in sql)
            )
        ]
        # O(1): one query per backing table regardless of page size.
        self.assertLessEqual(len(indicator_queries), 2)


if __name__ == "__main__":
    unittest.main()
