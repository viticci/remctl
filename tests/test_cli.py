from __future__ import annotations

import contextlib
import io
import json
import plistlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import load_module


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def test_parse_alarm_normalizes_relative_and_absolute_values(self):
        self.assertEqual(self.remctl.parse_alarm("15m"), "-15m")
        self.assertEqual(self.remctl.parse_alarm("2h"), "-2h")
        self.assertEqual(
            self.remctl.parse_alarm("2026-04-15 14:00"),
            "2026-04-15T14:00:00",
        )

    def test_list_create_uses_bridge_contract_fields(self):
        args = SimpleNamespace(name="Project X", color="blue", json=True)
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "created"}) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_list_create(args)
        self.assertEqual(
            bridge_call.call_args.args[0],
            {"action": "create_list", "title": "Project X", "color": "blue"},
        )

    def test_list_rename_and_delete_use_bridge_contract_fields(self):
        rename_args = SimpleNamespace(name="Old", new_name="New", json=True)
        delete_args = SimpleNamespace(name="Old", force=True, json=True)
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "renamed"}) as rename_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_list_rename(rename_args)
        self.assertEqual(
            rename_call.call_args.args[0],
            {"action": "rename_list", "title": "Old", "newTitle": "New"},
        )

        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "deleted"}) as delete_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_list_delete(delete_args)
        self.assertEqual(
            delete_call.call_args.args[0],
            {"action": "delete_list", "title": "Old"},
        )

    def test_import_accepts_due_date_from_exported_json(self):
        created_args = []

        def fake_cmd_add(args):
            created_args.append(args)

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

    def test_launch_agent_program_args_include_requested_flags(self):
        args = self.remctl.launch_agent_program_args(
            Path("/tmp/remctl-server"),
            host="0.0.0.0",
            port=8123,
            allow_origin="https://example.com",
            enable_opengraph=True,
        )
        self.assertEqual(
            args,
            [
                "/tmp/remctl-server",
                "--port",
                "8123",
                "--host",
                "0.0.0.0",
                "--allow-origin",
                "https://example.com",
                "--enable-opengraph",
            ],
        )

    def test_parse_launch_agent_settings_supports_legacy_shell_wrapper(self):
        plist = {
            "ProgramArguments": [
                "/bin/zsh",
                "-l",
                "-c",
                "exec /Users/test/bin/remctl-server --port 19876 --host 0.0.0.0",
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_path = Path(tmpdir) / "com.remctl.server.plist"
            with plist_path.open("wb") as fh:
                plistlib.dump(plist, fh)
            with mock.patch.object(self.remctl, "launch_agent_path", return_value=plist_path):
                settings = self.remctl.parse_launch_agent_settings()

        self.assertEqual(settings["server_path"], "/Users/test/bin/remctl-server")
        self.assertEqual(settings["host"], "0.0.0.0")
        self.assertEqual(settings["port"], 19876)

    def test_parse_launch_agent_settings_supports_python_wrapper(self):
        plist = {
            "ProgramArguments": [
                "/usr/bin/python3",
                "/Users/test/bin/remctl-server",
                "--port",
                "19876",
                "--host",
                "0.0.0.0",
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_path = Path(tmpdir) / "com.remctl.server.plist"
            with plist_path.open("wb") as fh:
                plistlib.dump(plist, fh)
            with mock.patch.object(self.remctl, "launch_agent_path", return_value=plist_path):
                settings = self.remctl.parse_launch_agent_settings()

        self.assertEqual(settings["python_path"], "/usr/bin/python3")
        self.assertEqual(settings["server_path"], "/Users/test/bin/remctl-server")
        self.assertEqual(settings["host"], "0.0.0.0")
        self.assertEqual(settings["port"], 19876)

    def test_current_server_path_prefers_resolved_binary_over_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bundled = Path(tmpdir) / "remctl-server"
            bundled.write_text("#!/usr/bin/env python3\n")
            with (
                mock.patch.object(self.remctl, "SERVER_PATH", bundled),
                mock.patch.object(self.remctl.shutil, "which", return_value="/tmp/on-path/remctl-server"),
            ):
                resolved = self.remctl.current_server_path()
        self.assertEqual(resolved, bundled.resolve())

    def test_assess_local_api_probe_flags_degraded_database_as_failure(self):
        result = self.remctl.assess_local_api_probe(
            "127.0.0.1",
            19876,
            {
                "health": {
                    "ok": True,
                    "data": {"status": "degraded", "database": "not found"},
                },
                "stats": None,
                "error": None,
            },
            settings={"python_path": "/tmp/service-python", "server_path": "/tmp/remctl-server"},
        )
        self.assertEqual(result["status"], "fail")
        self.assertIn("database: not found", result["detail"])
        self.assertIn("separate launchd process", result["fix"])
        self.assertIn("/tmp/service-python", result["fix"])
        self.assertIn("remctl service restart", result["fix"])
        self.assertIn("remctl doctor", result["fix"])

    def test_stop_service_treats_no_such_process_as_already_stopped(self):
        proc = SimpleNamespace(
            returncode=3,
            stdout="",
            stderr="Boot-out failed: 3: No such process",
        )
        with (
            mock.patch.object(self.remctl.sys, "platform", "darwin"),
            mock.patch.object(self.remctl, "launchctl_run", return_value=proc),
        ):
            result = self.remctl.stop_service()
        self.assertTrue(result["ok"])
        self.assertEqual(result["details"], "Service was not loaded")

    def test_resolve_setup_shell_auto_skips_unsupported_shells(self):
        with mock.patch.dict("os.environ", {"SHELL": "/bin/tcsh"}, clear=False):
            self.assertEqual(self.remctl.resolve_setup_shell("auto"), "skip")
        with mock.patch.dict("os.environ", {"SHELL": "/bin/zsh"}, clear=False):
            self.assertEqual(self.remctl.resolve_setup_shell("auto"), "zsh")

    def test_cmd_setup_reports_existing_service_when_service_install_is_skipped(self):
        args = SimpleNamespace(
            shell="skip",
            service="skip",
            host="127.0.0.1",
            port=19876,
            allow_origin=None,
            enable_opengraph=False,
            doctor=False,
            json=False,
        )
        with (
            mock.patch.object(self.remctl, "ensure_api_token", return_value=("token", False)),
            mock.patch.object(
                self.remctl,
                "launch_agent_status",
                return_value={
                    "installed": True,
                    "loaded": True,
                    "running": True,
                    "path": "/tmp/com.remctl.server.plist",
                },
            ),
            mock.patch.object(
                self.remctl,
                "parse_launch_agent_settings",
                return_value={
                    "python_path": "/tmp/service-python",
                    "server_path": "/tmp/remctl-server",
                    "host": "127.0.0.1",
                    "port": 19876,
                },
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_setup(args)
        output = stdout.getvalue()
        self.assertIn("Local API service: already installed (running)", output)
        self.assertIn("/tmp/com.remctl.server.plist", output)
        self.assertIn("Full Disk Access target: /tmp/service-python", output)

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

    def test_database_access_check_for_onboarding_downgrades_when_local_api_is_healthy(self):
        with (
            mock.patch.object(self.remctl, "reminders_store_access_error", return_value="db blocked"),
            mock.patch.object(self.remctl, "find_main_db_path", return_value=None),
            mock.patch.object(self.remctl, "full_disk_access_targets", return_value=["Terminal.app", "/tmp/python3"]),
        ):
            check = self.remctl.database_access_check_for_onboarding(
                api_check={"status": "ok", "detail": "healthy", "fix": None}
            )
        self.assertEqual(check["status"], "warn")
        self.assertIn("local remctl service fallback is healthy", check["detail"])
        self.assertIn("Terminal.app", check["fix"])
        self.assertIn("ignore this warning", check["fix"])

    def test_detect_terminal_app_name_prefers_term_program(self):
        with mock.patch.dict("os.environ", {"TERM_PROGRAM": "Apple_Terminal"}, clear=False):
            self.assertEqual(self.remctl.detect_terminal_app_name(), "Terminal.app")

    def test_full_disk_access_fix_text_mentions_targets_and_fallback(self):
        with mock.patch.object(
            self.remctl,
            "full_disk_access_targets",
            return_value=["Terminal.app (recommended for CLI use)", "/tmp/python3"],
        ):
            text = self.remctl.full_disk_access_fix_text(
                api_healthy=True,
                rerun_command="remctl doctor",
                mention_onboard=True,
            )
        self.assertIn("Terminal.app", text)
        self.assertIn("/tmp/python3", text)
        self.assertIn("remctl onboard", text)
        self.assertIn("ignore this warning", text)
        self.assertIn("Command-Shift-G", text)

    def test_print_full_disk_access_guidance_copies_python_path(self):
        with (
            mock.patch.object(self.remctl.sys, "executable", "/tmp/python3"),
            mock.patch.object(self.remctl, "full_disk_access_targets", return_value=["Terminal.app", "/tmp/python3"]),
            mock.patch.object(self.remctl, "copy_to_clipboard", return_value=True) as copy_to_clipboard,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.print_full_disk_access_guidance(settings_opened=True)
        copy_to_clipboard.assert_called_once_with("/tmp/python3")
        output = stdout.getvalue()
        self.assertIn("Copied path to clipboard: /tmp/python3", output)
        self.assertIn("Command-Shift-G", output)

    def test_print_service_full_disk_access_guidance_copies_service_python_path(self):
        with (
            mock.patch.object(
                self.remctl,
                "parse_launch_agent_settings",
                return_value={"python_path": "/tmp/service-python", "server_path": "/tmp/remctl-server"},
            ),
            mock.patch.object(self.remctl, "copy_to_clipboard", return_value=True) as copy_to_clipboard,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.print_service_full_disk_access_guidance(settings_opened=True)
        copy_to_clipboard.assert_called_once_with("/tmp/service-python")
        output = stdout.getvalue()
        self.assertIn("Copied path to clipboard: /tmp/service-python", output)
        self.assertIn("Command-Shift-G", output)

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
                "name": "local_api",
                "status": "fail",
                "detail": "degraded",
                "fix": "First line\n  remctl service restart\n  remctl doctor",
            }
        ]
        with (
            mock.patch.object(self.remctl.C, "enabled", False),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.print_check_report(None, checks)
        output = stdout.getvalue()
        self.assertIn("      First line", output)
        self.assertIn("        remctl service restart", output)
        self.assertIn("        remctl doctor", output)

    def test_cmd_onboard_opens_full_disk_access_settings_when_database_not_ready(self):
        result = {
            "ok": True,
            "warnings": 1,
            "failures": 0,
            "checks": [
                {"name": "database", "status": "warn", "detail": "db blocked", "fix": "Grant Full Disk Access to Terminal.app"},
                {"name": "local_api", "status": "ok", "detail": "healthy", "fix": None},
            ],
        }
        with (
            mock.patch.object(self.remctl, "run_onboarding", return_value=result),
            mock.patch.object(self.remctl, "print_check_report"),
            mock.patch.object(self.remctl, "open_full_disk_access_settings", return_value=True) as open_settings,
            mock.patch.object(self.remctl, "print_full_disk_access_guidance") as guidance,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_onboard(SimpleNamespace(json=False))
        open_settings.assert_called_once_with()
        guidance.assert_called_once_with(
            api_healthy=True,
            settings_opened=True,
            rerun_command="remctl doctor",
        )

    def test_cmd_onboard_opens_full_disk_access_settings_when_service_not_ready(self):
        result = {
            "ok": False,
            "warnings": 0,
            "failures": 1,
            "checks": [
                {"name": "database", "status": "ok", "detail": "/tmp/db.sqlite", "fix": None},
                {
                    "name": "local_api",
                    "status": "fail",
                    "detail": "degraded",
                    "fix": "The service needs Full Disk Access.",
                },
            ],
        }
        with (
            mock.patch.object(self.remctl, "run_onboarding", return_value=result),
            mock.patch.object(self.remctl, "print_check_report"),
            mock.patch.object(self.remctl, "open_full_disk_access_settings", return_value=True) as open_settings,
            mock.patch.object(self.remctl, "print_service_full_disk_access_guidance") as guidance,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                self.remctl.cmd_onboard(SimpleNamespace(json=False))
        open_settings.assert_called_once_with()
        guidance.assert_called_once_with(
            settings_opened=True,
            rerun_command="remctl doctor",
        )

    def test_gather_onboarding_checks_includes_failing_installed_service_when_database_is_ready(self):
        with (
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
                "launch_agent_status",
                return_value={"installed": True, "running": True, "path": "/tmp/com.remctl.server.plist"},
            ),
            mock.patch.object(
                self.remctl,
                "local_api_check_for_onboarding",
                return_value={
                    "name": "local_api",
                    "status": "fail",
                    "detail": "degraded",
                    "fix": "Full Disk Access required",
                },
            ),
            mock.patch.object(
                self.remctl,
                "database_access_check_for_onboarding",
                return_value={"name": "database", "status": "ok", "detail": "/tmp/db.sqlite", "fix": None},
            ),
        ):
            checks = self.remctl.gather_onboarding_checks()

        self.assertEqual([check["name"] for check in checks], ["open_reminders", "eventkit", "automation", "database", "local_api"])

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
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = self.remctl.maybe_run_first_launch_onboarding(args)

        self.assertTrue(result["ok"])
        run_onboarding.assert_called_once_with(auto=True)
        print_report.assert_called_once_with("RemCTL onboard", [])

    def test_should_auto_onboard_skips_admin_commands(self):
        args = SimpleNamespace(cmd="setup", json=False, format="plain")
        with (
            mock.patch.object(self.remctl, "load_onboard_state", return_value=None),
            mock.patch.object(self.remctl.sys, "platform", "darwin"),
            mock.patch.dict("os.environ", {}, clear=False),
            mock.patch.object(self.remctl.sys, "stdout", SimpleNamespace(isatty=lambda: True)),
        ):
            result = self.remctl.should_auto_onboard(args)

        self.assertFalse(result)

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
        "ZDUEDATE": None,
    }

    def _assert_bridge_first(self, cmd_name, args, expected_action):
        reminder = self._FAKE_REMINDER
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call",
                return_value={
                    "status": {
                        "complete": "completed", "uncomplete": "uncompleted",
                        "delete": "deleted", "update": "updated",
                    }.get(expected_action, expected_action),
                    "id": reminder["ZCKIDENTIFIER"],
                },
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
        action = osa_try.call_args.args[2]
        self.assertIn(script_contains, action)
        bridge_call.assert_not_called()

    def test_cmd_done_is_bridge_first(self):
        self._assert_bridge_first("cmd_done", SimpleNamespace(id=1, json=True), "complete")

    def test_cmd_undone_is_bridge_first(self):
        self._assert_bridge_first("cmd_undone", SimpleNamespace(id=1, json=True), "uncomplete")

    def test_cmd_delete_is_bridge_first(self):
        self._assert_bridge_first(
            "cmd_delete", SimpleNamespace(id=1, json=True, force=True), "delete"
        )

    def test_cmd_flag_is_applescript_first(self):
        # Bridge has no flagged property — priority=1 as proxy is lossy.
        self._assert_applescript_first(
            "cmd_flag", SimpleNamespace(id=1, json=True), "set flagged of r to true"
        )

    def test_cmd_unflag_is_applescript_first(self):
        self._assert_applescript_first(
            "cmd_unflag", SimpleNamespace(id=1, json=True), "set flagged of r to false"
        )

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
                self.remctl, "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_try,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        bridge_call.assert_called_once()
        osa_try.assert_not_called()
        payload = bridge_call.call_args.args[0]
        self.assertEqual(payload["action"], "update")
        self.assertEqual(payload["due"], "2026-04-20T09:00:00")

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
                self.remctl, "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(SimpleNamespace(
                id=1, json=True, title=None, notes=None, priority=None,
                due="2026-04-20 09:00", url=None, recurrence=None, alarm=None,
            ))
        self.assertEqual(bridge_call.call_count, 2)
        nudge_payload, real_payload = (
            bridge_call.call_args_list[0].args[0],
            bridge_call.call_args_list[1].args[0],
        )
        self.assertEqual(nudge_payload["due"], "2026-04-20T10:00:00")  # +1h
        self.assertEqual(real_payload["due"], "2026-04-20T09:00:00")

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
        reminder = self._FAKE_REMINDER
        args = SimpleNamespace(
            id=1, json=True, title=None, notes=None, priority=None,
            due=None, url=None, recurrence=None, alarm="15m",
        )
        with (
            mock.patch.object(self.remctl, "open_db", return_value=None),
            mock.patch.object(self.remctl, "q_reminder", return_value=reminder),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        bridge_call.assert_called_once()
        self.assertEqual(bridge_call.call_args.args[0]["alarm"], "-15m")

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
        self.assertIn("set due date of r to _rdt", osa_try.call_args.args[2])

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

    def test_handle_local_api_fallback_today_json_works(self):
        items = [
            {
                "id": 42,
                "title": "Ship remctl",
                "list": "Work",
                "completed": False,
                "flagged": False,
                "priority": "medium",
                "subtaskCount": 0,
                "isSubtask": False,
                "dueDate": "2026-04-18T09:00:00",
            }
        ]
        args = SimpleNamespace(cmd="today", json=True, no_overdue=False, format="json")
        with (
            mock.patch.object(self.remctl, "local_api_request", return_value=items) as api_request,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            handled = self.remctl.handle_local_api_fallback(args)
        self.assertTrue(handled)
        api_request.assert_called_once_with("GET", "/api/v1/today")
        self.assertEqual(json.loads(stdout.getvalue()), items)

    def test_handle_local_api_fallback_done_posts_complete(self):
        args = SimpleNamespace(cmd="done", id=42, json=True)
        with (
            mock.patch.object(
                self.remctl,
                "local_api_request",
                return_value={"status": "completed", "title": "Ship remctl"},
            ) as api_request,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            handled = self.remctl.handle_local_api_fallback(args)
        self.assertTrue(handled)
        api_request.assert_called_once_with("POST", "/api/v1/reminders/42/complete", body={})
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"status": "completed", "id": 42, "title": "Ship remctl"},
        )

    def test_handle_local_api_fallback_edit_forwards_clear_due_and_bridge_fields(self):
        args = SimpleNamespace(
            cmd="edit",
            id=42,
            json=True,
            title="Renamed",
            notes="Updated notes",
            priority="high",
            due="clear",
            url="https://example.com",
            recurrence="weekly mon,wed",
            alarm="15m",
        )
        with (
            mock.patch.object(self.remctl, "local_api_request", return_value={"status": "updated"}) as api_request,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            handled = self.remctl.handle_local_api_fallback(args)
        self.assertTrue(handled)
        api_request.assert_called_once()
        self.assertEqual(api_request.call_args.args, ("PATCH", "/api/v1/reminders/42"))
        self.assertEqual(
            api_request.call_args.kwargs["body"],
            {
                "title": "Renamed",
                "notes": "Updated notes",
                "priority": 1,
                "due": None,
                "url": "https://example.com",
                "recurrence": {"frequency": "weekly", "interval": 1, "daysOfWeek": [2, 4]},
                "alarm": "-15m",
            },
        )
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "updated", "id": 42})

    def test_run_handler_with_fallback_uses_local_api_when_database_unavailable(self):
        args = SimpleNamespace(cmd="today", json=True, no_overdue=False, format="json")
        handler = mock.Mock(side_effect=self.remctl.RemindersDBUnavailable("db unavailable"))
        with mock.patch.object(self.remctl, "handle_local_api_fallback", return_value=True) as fallback:
            self.remctl.run_handler_with_fallback(args, handler)
        fallback.assert_called_once_with(args)

    def test_gather_doctor_checks_downgrades_database_failure_when_local_api_is_healthy(self):
        def fake_path(path):
            return SimpleNamespace(exists=lambda: True, __str__=lambda self: path)

        with (
            mock.patch.object(self.remctl, "reminders_store_access_error", return_value="db blocked"),
            mock.patch.object(self.remctl, "find_main_db_path", return_value=None),
            mock.patch.object(self.remctl, "STORE_DIR", fake_path("/tmp/store")),
            mock.patch.object(self.remctl, "CONFIG_DIR", fake_path("/tmp/config")),
            mock.patch.object(
                self.remctl,
                "TOKEN_FILE",
                SimpleNamespace(
                    exists=lambda: True,
                    read_text=lambda: "secret",
                    __str__=lambda self: "/tmp/token",
                ),
            ),
            mock.patch.object(self.remctl, "current_cli_path", return_value=fake_path("/tmp/remctl")),
            mock.patch.object(self.remctl, "current_bridge_path", return_value=fake_path("/tmp/remctl-bridge")),
            mock.patch.object(self.remctl, "current_server_path", return_value=fake_path("/tmp/remctl-server")),
            mock.patch.object(self.remctl.os, "access", return_value=True),
            mock.patch.object(self.remctl, "detect_shell_name", return_value="zsh"),
            mock.patch.object(self.remctl, "completion_target_path", return_value=fake_path("/tmp/_remctl")),
            mock.patch.object(
                self.remctl,
                "launch_agent_status",
                return_value={"installed": True, "running": True, "path": "/tmp/com.remctl.server.plist"},
            ),
            mock.patch.object(self.remctl, "parse_launch_agent_settings", return_value={"host": "127.0.0.1", "port": 19876}),
            mock.patch.object(self.remctl, "local_api_probe", return_value={}),
            mock.patch.object(
                self.remctl,
                "assess_local_api_probe",
                return_value={"status": "ok", "detail": "healthy", "fix": None},
            ),
            mock.patch.object(self.remctl.shutil, "which", return_value="/tmp/remctl"),
        ):
            checks = self.remctl.gather_doctor_checks()
        database = next(check for check in checks if check["name"] == "database")
        self.assertEqual(database["status"], "warn")
        self.assertIn("local remctl service fallback is healthy", database["detail"])

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
        self.assertEqual(table[0]["id"], 42)
        self.assertEqual(table[0]["title"], "Test reminder")


if __name__ == "__main__":
    unittest.main()
