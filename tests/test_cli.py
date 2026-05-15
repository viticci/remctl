from __future__ import annotations

import contextlib
import io
import json
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

    def _list_db(self, names):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE ZREMCDBASELIST ("
            "Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER, Z_ENT INTEGER)"
        )
        for idx, name in enumerate(names, start=1):
            db.execute(
                "INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT) VALUES (?, ?, ?, 0, 3)",
                (idx, name, f"CK-{idx}"),
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

    def test_list_create_uses_bridge_contract_fields(self):
        args = SimpleNamespace(name="Project X", color="blue", private=False, symbol=None, emoji=None, json=True)
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

    def test_list_create_rejects_symbol_without_private_before_bridge(self):
        args = SimpleNamespace(name="Project X", color=None, private=False, symbol="education3", emoji=None, json=True)
        with (
            mock.patch.object(self.remctl, "bridge_available") as bridge_available,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_list_create(args)
        bridge_available.assert_not_called()

    def test_list_edit_private_targets_resolved_list_id(self):
        db = self._list_db(["Projects"])
        args = SimpleNamespace(
            name=None,
            list_id=1,
            new_name=None,
            color="#123456",
            private=True,
            symbol="pencil.and.ruler",
            emoji=None,
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
                "symbol": "pencil.and.ruler",
            },
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
            ) as bridge_call,
            mock.patch.object(
                self.remctl,
                "apply_private_changes",
                return_value=[{"status": "updated", "action": "add_private_metadata"}],
            ) as apply_private_changes,
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_add(args)

        bridge_payload = bridge_call.call_args.args[0]
        self.assertEqual(bridge_payload["title"], "Research")
        self.assertNotIn("url", bridge_payload)
        self.assertNotIn("flagged", bridge_payload)
        apply_private_changes.assert_called_once_with(
            "ABC-123",
            args,
            db=fake_db,
            list_pk=7,
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
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "created", "id": "UUID-1"}),
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
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "created", "id": "UUID-1"}) as bridge_call,
            mock.patch.object(self.remctl, "open_db", return_value=object()),
            mock.patch.object(
                self.remctl,
                "resolve_list_or_die",
                return_value={"id": 156, "title": "🗓️ Weekly 513", "requested": "Weekly 513", "method": "normalized"},
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_add(args)

        self.assertEqual(bridge_call.call_args.args[0]["list"], "🗓️ Weekly 513")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["resolvedList"]["title"], "🗓️ Weekly 513")
        self.assertEqual(payload["resolvedList"]["method"], "normalized")

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
        self.assertIn("remctl onboard", output)

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
            mock.patch.object(self.remctl, "full_disk_access_targets", return_value=["Terminal.app", "/tmp/python3"]),
        ):
            check = self.remctl.database_access_check_for_onboarding()
        self.assertEqual(check["status"], "fail")
        self.assertIn("db blocked", check["detail"])
        self.assertIn("Terminal.app", check["fix"])

    def test_detect_terminal_app_name_prefers_term_program(self):
        with mock.patch.dict("os.environ", {"TERM_PROGRAM": "Apple_Terminal"}, clear=False):
            self.assertEqual(self.remctl.detect_terminal_app_name(), "Terminal.app")

    def test_full_disk_access_fix_text_mentions_targets_and_fallback(self):
        with mock.patch.object(
            self.remctl,
            "full_disk_access_targets",
            return_value=["Terminal.app (recommended for CLI use)", "/tmp/python3"],
        ), mock.patch.object(
            self.remctl,
            "doctor_execution_context",
            return_value={"effective_context": "Codex"},
        ):
            text = self.remctl.full_disk_access_fix_text(
                rerun_command="remctl doctor",
                mention_onboard=True,
            )
        self.assertIn("Terminal.app", text)
        self.assertIn("/tmp/python3", text)
        self.assertIn("remctl onboard", text)
        self.assertIn("Current execution context: Codex", text)
        self.assertIn("Terminal does not grant access", text)
        self.assertIn("Command-Shift-G", text)

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
        self.assertIn("same context", payload["agent_note"])

    def test_print_full_disk_access_guidance_copies_python_path(self):
        with (
            mock.patch.object(self.remctl.sys, "executable", "/tmp/python3"),
            mock.patch.object(self.remctl, "full_disk_access_targets", return_value=["Terminal.app", "/tmp/python3"]),
            mock.patch.object(self.remctl, "copy_to_clipboard", return_value=True) as copy_to_clipboard,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.print_full_disk_access_guidance(settings_opened=True)
        expected_path = str(Path("/tmp/python3").resolve(strict=False))
        copy_to_clipboard.assert_called_once_with(expected_path)
        output = stdout.getvalue()
        self.assertIn(f"Copied path to clipboard: {expected_path}", output)
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
        result = {
            "ok": True,
            "warnings": 1,
            "failures": 0,
            "checks": [
                {"name": "database", "status": "warn", "detail": "db blocked", "fix": "Grant Full Disk Access to Terminal.app"},
            ],
        }
        with (
            mock.patch.object(self.remctl, "run_onboarding", return_value=result),
            mock.patch.object(self.remctl, "print_check_report"),
            mock.patch.object(self.remctl, "launch_full_disk_access_helper", return_value=False),
            mock.patch.object(self.remctl, "open_full_disk_access_settings", return_value=True) as open_settings,
            mock.patch.object(self.remctl, "print_full_disk_access_guidance") as guidance,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_onboard(SimpleNamespace(json=False))
        open_settings.assert_called_once_with()
        guidance.assert_called_once_with(
            settings_opened=True,
            rerun_command="remctl doctor",
        )

    def test_gather_onboarding_checks_includes_core_cli_checks(self):
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
                "database_access_check_for_onboarding",
                return_value={"name": "database", "status": "ok", "detail": "/tmp/db.sqlite", "fix": None},
            ),
        ):
            checks = self.remctl.gather_onboarding_checks()

        self.assertEqual([check["name"] for check in checks], ["open_reminders", "eventkit", "automation", "database"])

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

    def test_cmd_permissions_reports_helper_targets_in_json(self):
        args = SimpleNamespace(cmd="permissions", topic="full-disk-access", scope="cli", wait=False, json=True)
        target = [{"title": "CLI Python", "path": "/tmp/python3", "subtitle": "CLI target"}]
        with (
            mock.patch.object(self.remctl, "full_disk_access_target_specs", return_value=target),
            mock.patch.object(self.remctl, "current_permissions_path", return_value=Path("/tmp/remctl-permissions")),
            mock.patch.object(self.remctl, "permission_helper_available", return_value=True),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_permissions(args)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["available"])
        self.assertEqual(payload["targets"], target)

    def test_launch_full_disk_access_helper_passes_targets_and_after_commands(self):
        class FakePath:
            def exists(self):
                return True

            def __str__(self):
                return "/tmp/remctl-permissions"

        targets = [
            {"title": "CLI Python", "path": "/tmp/python3", "subtitle": "CLI target"},
        ]
        with (
            mock.patch.object(self.remctl, "current_permissions_path", return_value=FakePath()),
            mock.patch.object(self.remctl.os, "access", return_value=True),
            mock.patch.object(self.remctl, "full_disk_access_target_specs", return_value=targets),
            mock.patch.object(self.remctl.subprocess, "Popen") as popen,
        ):
            launched = self.remctl.launch_full_disk_access_helper(include_cli=True)
        self.assertTrue(launched)
        args = popen.call_args.args[0]
        self.assertIn("--target", args)
        self.assertIn("/tmp/python3", args)
        self.assertIn("Run Doctor", args)

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

    def test_run_handler_with_fallback_exits_when_database_unavailable(self):
        args = SimpleNamespace(cmd="today", json=True, no_overdue=False, format="json")
        handler = mock.Mock(side_effect=self.remctl.RemindersDBUnavailable("db unavailable"))
        with (
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.run_handler_with_fallback(args, handler)
        self.assertIn("db unavailable", stderr.getvalue())

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
            mock.patch.object(self.remctl, "q_hashtags", return_value=[]),
            mock.patch.object(self.remctl, "q_section_memberships", return_value={"ABC": "Playground"}),
            mock.patch.object(self.remctl, "q_rich_link", return_value="https://github.com/viticci/shortcuts-playground-plugin"),
            mock.patch.object(self.remctl, "q_subtask_count", return_value=0),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_info(SimpleNamespace(id=42, json=True))

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["url"], "https://github.com/viticci/shortcuts-playground-plugin")
        self.assertEqual(payload["section"], "Playground")

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


if __name__ == "__main__":
    unittest.main()
