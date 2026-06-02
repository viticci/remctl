from __future__ import annotations

import contextlib
import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import load_module


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_cli_test", "remctl")

    def setUp(self):
        """Isolate every test from the developer's real config file, the
        REMCTL_ACCOUNT_SCOPE environment variable, and module-level caches so
        account-scope behavior is deterministic regardless of local state."""
        # Save module globals that scope/config logic mutates.
        self._saved_store_dir = self.remctl.STORE_DIR
        self._saved_db_override = self.remctl.DB_OVERRIDE
        self._saved_config_file = self.remctl.CONFIG_FILE
        self._saved_account_scope_env = os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
        self._saved_db_env = os.environ.pop("REMCTL_DB", None)
        self._saved_store_env = os.environ.pop("REMCTL_STORE_DIR", None)
        # Point config at a path that does not exist so load_config() returns {}.
        self.remctl.CONFIG_FILE = Path("/nonexistent/remctl-test-config.json")
        self.remctl._ACCOUNT_CACHE = None
        try:
            self.remctl._REMINDER_COLUMN_CACHE.clear()
        except Exception:
            pass

    def tearDown(self):
        self.remctl.STORE_DIR = self._saved_store_dir
        self.remctl.DB_OVERRIDE = self._saved_db_override
        self.remctl.CONFIG_FILE = self._saved_config_file
        self.remctl._ACCOUNT_CACHE = None
        for var, val in (
            ("REMCTL_ACCOUNT_SCOPE", self._saved_account_scope_env),
            ("REMCTL_DB", self._saved_db_env),
            ("REMCTL_STORE_DIR", self._saved_store_env),
        ):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

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
                "bridge_call",
                return_value={"status": "created", "id": "REMINDER-1"},
            ) as bridge_call,
            mock.patch.object(
                self.remctl,
                "open_db",
                side_effect=self.remctl.RemindersDBUnavailable("no db"),
            ),
            mock.patch.object(sys, "stdout", new_callable=io.StringIO),
        ):
            self.remctl.cmd_add(args)

        bridge_call.assert_called_once()
        payload = bridge_call.call_args.args[0]
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

    def _list_db(self, names, grocery_locales=None):
        grocery_locales = grocery_locales or {}
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE ZREMCDBASELIST ("
            "Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER, Z_ENT INTEGER, "
            "ZBADGEEMBLEM TEXT, ZCOLOR BLOB, "
            "ZISPINNEDBYCURRENTUSER INTEGER, ZPINNEDDATE REAL, "
            "ZSHOULDCATEGORIZEGROCERYITEMS INTEGER, ZSHOULDAUTOCATEGORIZEITEMS INTEGER, "
            "ZSHOULDSUGGESTCONVERSIONTOGROCERYLIST INTEGER, ZGROCERYLOCALEID TEXT, "
            "ZAUTOCATEGORIZATIONLOCALCORRECTIONSCHECKSUM TEXT, ZAUTOCATEGORIZATIONLOCALCORRECTIONSASDATA BLOB)"
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

    def test_list_to_dict_includes_private_appearance_fields(self):
        row = {
            "Z_PK": 1,
            "ZNAME": "Projects",
            "ZCKIDENTIFIER": "CK-1",
            "ZBADGEEMBLEM": "{\"Emoji\" : \"\\ud83d\\udccc\"}",
            "ZCOLOR": b"not-a-color",
            "ZSHOULDCATEGORIZEGROCERYITEMS": 0,
        }

        payload = self.remctl.list_to_dict(row)

        self.assertEqual(payload["badge"]["emoji"], "\U0001f4cc")
        self.assertEqual(payload["color"]["hex"], "#007AFF")

    def test_lists_json_reports_grocery_metadata(self):
        db = self._list_db(["Groceries", "Work"], grocery_locales={"Groceries": "en_US"})
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "load_config", return_value={}),
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

    def test_lists_human_output_marks_groceries_with_carrot(self):
        db = self._list_db(["Groceries"], grocery_locales={"Groceries": "en_US"})
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "load_config", return_value={}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(SimpleNamespace(json=False, format=None))
        finally:
            db.close()

        output = stdout.getvalue()
        self.assertIn("Groceries", output)
        self.assertIn("🥕", output)

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
        self.assertIn("add,", output)
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
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "created"}) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_list_create(args)
        self.assertEqual(
            bridge_call.call_args.args[0],
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
        bridge_result = {"status": "created", "id": "REM-1"}
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call", return_value=bridge_result),
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
        bridge_result = {"status": "created", "id": "REM-1"}
        section_results = iter([None, "Dairy, Eggs & Cheese"])
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call", return_value=bridge_result),
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
                {"action": "rename_list", "title": "Old", "newTitle": "New"},
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
                {"action": "delete_list", "title": "Old"},
            )
        finally:
            db.close()

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
                {"action": "set_smart_list_pinned", "smartListId": "BUILTIN-1", "pinned": True},
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
                {"action": "set_smart_list_pinned", "smartListId": "CUSTOM-1", "pinned": False},
            )
        finally:
            db.close()

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
        args = SimpleNamespace(name="Nope", private=True, flagged=False, priority="none", json=True)
        with (
            mock.patch.object(self.remctl, "private_available") as private_available,
            mock.patch.object(self.remctl, "private_call") as private_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_smart_list_create(args)

        private_available.assert_not_called()
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
                "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
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

        payload = bridge_call.call_args.args[0]
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
                "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
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

        self.assertNotIn("alarm", bridge_call.call_args.args[0])

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
                "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
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

        self.assertEqual(bridge_call.call_count, 2)
        final_payload = bridge_call.call_args_list[-1].args[0]
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
                "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
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

        payload = bridge_call.call_args.args[0]
        self.assertIsNone(payload["due"])
        self.assertTrue(payload["clearAlarms"])

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
                self.remctl, "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_edit(args)
        payload = bridge_call.call_args.args[0]
        self.assertTrue(payload["clearAlarms"])
        self.assertNotIn("alarm", payload)

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
                "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
            mock.patch.object(self.remctl, "private_available") as private_available,
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_try,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        resolve_list.assert_called_once_with(mock.ANY, name="Projects", list_id=None)
        bridge_call.assert_called_once()
        self.assertEqual(bridge_call.call_args.args[0]["list"], "Projects")
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
                "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
            ) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_edit(args)

        self.assertEqual(bridge_call.call_args.args[0]["list"], "Projects")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["resolvedList"]["id"], 9)
        self.assertEqual(payload["resolvedList"]["method"], "id")

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
                "bridge_call",
                return_value={"status": "updated", "id": reminder["ZCKIDENTIFIER"]},
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
        row = {
            "Z_PK": 42,
            "ZTITLE": "Review",
            "ZNOTES": None,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None,
            "ZDUEDATE": 801216000,
            "ZDISPLAYDATEDATE": 801215100,
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
            "ZFILENAME TEXT, ZUTI TEXT, ZATTACHMENTTYPERAWVALUE TEXT, "
            "ZREMINDER INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        db.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, Z_ENT INTEGER, ZREMINDER INTEGER, ZTRIGGER INTEGER, "
            "ZREMINDER2 INTEGER, ZREMINDER3 INTEGER, ZHASHTAGLABEL INTEGER, ZMARKEDFORDELETION INTEGER, "
            "ZFILENAME TEXT, ZUTI TEXT, ZWIDTH INTEGER, ZHEIGHT INTEGER, ZURL TEXT, "
            "ZTIMEINTERVAL REAL, ZDATECOMPONENTSDATA BLOB, ZTITLE TEXT, ZLATITUDE REAL, "
            "ZLONGITUDE REAL, ZRADIUS REAL, ZADDRESS TEXT, ZPROXIMITY INTEGER)"
        )
        db.executemany(
            "INSERT INTO ZREMCDOBJECT VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 25, None, None, 43, None, None, 0, "child.png", "public.png", 100, 100, None, None, None, None, None, None, None, None, None),
                (2, 15, 43, 3, None, None, None, 0, None, None, None, None, None, None, None, None, None, None, None, None, None),
                (3, 19, None, None, None, None, None, 0, None, None, None, None, None, -600, None, None, None, None, None, None, None),
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
            "ZFILENAME TEXT, ZUTI TEXT, ZATTACHMENTTYPERAWVALUE TEXT, "
            "ZREMINDER INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        conn.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "ZFILENAME TEXT, ZUTI TEXT, ZWIDTH INTEGER, ZHEIGHT INTEGER, "
            "ZREMINDER2 INTEGER, ZMARKEDFORDELETION INTEGER)"
        )
        conn.execute(
            "INSERT INTO ZREMCDOBJECT VALUES "
            "('first.png', 'public.png', 512, 512, 42, 0), "
            "('second.png', 'public.png', 512, 512, 42, 0), "
            "('deleted.png', 'public.png', 512, 512, 42, 1), "
            "(NULL, 'public.url', NULL, NULL, 42, 0)"
        )

        attachments = [dict(row) for row in self.remctl.q_attachments(conn, 42)]
        conn.close()

        self.assertEqual(
            attachments,
            [
                {"ZFILENAME": "first.png", "ZUTI": "public.png", "ZATTACHMENTTYPERAWVALUE": "image"},
                {"ZFILENAME": "second.png", "ZUTI": "public.png", "ZATTACHMENTTYPERAWVALUE": "image"},
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

    # ── Multi-Account Tests ──────────────────────────────────────────────────

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_account_db(self, name, account_type, lists, reminders=None):
        """Build a minimal in-memory SQLite db representing one Reminders account store."""
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row

        # ZREMCDBASELIST
        db.execute(
            "CREATE TABLE ZREMCDBASELIST ("
            "Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT, "
            "ZMARKEDFORDELETION INTEGER, Z_ENT INTEGER, "
            "ZBADGEEMBLEM TEXT, ZCOLOR BLOB, "
            "ZISPINNEDBYCURRENTUSER INTEGER, ZPINNEDDATE REAL, "
            "ZSHOULDCATEGORIZEGROCERYITEMS INTEGER, ZSHOULDAUTOCATEGORIZEITEMS INTEGER, "
            "ZSHOULDSUGGESTCONVERSIONTOGROCERYLIST INTEGER, ZGROCERYLOCALEID TEXT, "
            "ZAUTOCATEGORIZATIONLOCALCORRECTIONSASDATA_LENGTH INTEGER)"
        )
        for pk, lname, ckid in lists:
            db.execute(
                "INSERT INTO ZREMCDBASELIST "
                "(Z_PK, ZNAME, ZCKIDENTIFIER, ZMARKEDFORDELETION, Z_ENT, "
                "ZBADGEEMBLEM, ZCOLOR, ZISPINNEDBYCURRENTUSER, ZPINNEDDATE, "
                "ZSHOULDCATEGORIZEGROCERYITEMS, ZSHOULDAUTOCATEGORIZEITEMS, "
                "ZSHOULDSUGGESTCONVERSIONTOGROCERYLIST, ZGROCERYLOCALEID) "
                "VALUES (?, ?, ?, 0, 3, NULL, NULL, 0, NULL, 0, 0, 0, NULL)",
                (pk, lname, ckid),
            )

        # ZREMCDBASESECTION
        db.execute(
            "CREATE TABLE ZREMCDBASESECTION ("
            "Z_PK INTEGER PRIMARY KEY, ZDISPLAYNAME TEXT, ZLIST INTEGER, "
            "ZTEMPLATE INTEGER, "
            "ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER)"
        )

        # ZREMCDTEMPLATE / ZREMCDSAVEDREMINDER — empty, so template lookups
        # resolve to "not found" rather than raising on a missing table.
        db.execute(
            "CREATE TABLE ZREMCDTEMPLATE ("
            "Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT, "
            "ZCREATIONDATE REAL, ZLASTMODIFIEDDATE REAL, "
            "ZPUBLICLINKCREATIONDATE REAL, ZPUBLICLINKEXPIRATIONDATE REAL, "
            "ZPUBLICLINKLASTMODIFIEDDATE REAL, ZMARKEDFORDELETION INTEGER)"
        )
        db.execute(
            "CREATE TABLE ZREMCDSAVEDREMINDER ("
            "Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZTEMPLATE INTEGER, "
            "ZMARKEDFORDELETION INTEGER)"
        )

        # ZREMCDREMINDER
        db.execute(
            "CREATE TABLE ZREMCDREMINDER ("
            "Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZNOTES TEXT, ZCOMPLETED INTEGER, "
            "ZFLAGGED INTEGER, ZPRIORITY INTEGER, ZISURGENTSTATEENABLEDFORCURRENTUSER INTEGER, "
            "ZDUEDATEDELTAALERTSDATA TEXT, ZDUEDATE REAL, ZDISPLAYDATEDATE REAL, ZALLDAY INTEGER, "
            "ZCOMPLETIONDATE REAL, ZCREATIONDATE REAL, ZPARENTREMINDER INTEGER, ZLIST INTEGER, "
            "ZICSURL TEXT, ZCKIDENTIFIER TEXT, ZACCOUNT INTEGER, ZMARKEDFORDELETION INTEGER)"
        )

        # ZREMCDOBJECT — used for account entity row, recurrence objects, hashtag joins,
        # rich link lookups, alarm lookups, attachment lookups, assignment lookups
        db.execute(
            "CREATE TABLE ZREMCDOBJECT ("
            "Z_PK INTEGER, Z_ENT INTEGER, ZNAME TEXT, "
            "ZREMINDER INTEGER, ZREMINDER1 INTEGER, ZREMINDER2 INTEGER, "
            "ZREMINDER3 INTEGER, ZREMINDER4 INTEGER, "
            "ZHASHTAGLABEL INTEGER, ZURL TEXT, ZFILENAME TEXT, "
            "ZTRIGGER INTEGER, ZTIMEINTERVAL REAL, ZDATECOMPONENTSDATA BLOB, "
            "ZTITLE TEXT, ZLATITUDE REAL, ZLONGITUDE REAL, ZRADIUS REAL, "
            "ZADDRESS TEXT, ZPROXIMITY INTEGER, "
            "ZCKIDENTIFIER TEXT, ZDISPLAYNAME TEXT, ZFIRSTNAME TEXT, ZLASTNAME TEXT, "
            "ZADDRESS1 TEXT, ZASSIGNEDDATE REAL, ZLIST INTEGER, "
            "ZCKASSIGNEEIDENTIFIER TEXT, ZCKORIGINATORIDENTIFIER TEXT, "
            "ZASSIGNEE INTEGER, ZORIGINATOR INTEGER, ZSTATUS INTEGER, ZACCESSLEVEL INTEGER, "
            "ZMARKEDFORDELETION INTEGER, ZFREQUENCY INTEGER, ZINTERVAL INTEGER, "
            "ZOCCURRENCECOUNT INTEGER, ZENDDATE REAL, ZDAYSOFTHEWEEK BLOB, "
            "ZDAYSOFTHEMONTH BLOB, ZMONTHSOFTHEYEAR BLOB, ZDAYSOFTHEYEAR BLOB, "
            "ZWEEKSOFTHEYEAR BLOB, ZSETPOSITIONS BLOB)"
        )

        # Z_PRIMARYKEY — needed for _store_account_info
        db.execute(
            "CREATE TABLE Z_PRIMARYKEY (Z_NAME TEXT, Z_ENT INTEGER, Z_MAX INTEGER)"
        )
        db.execute(
            "INSERT INTO Z_PRIMARYKEY (Z_NAME, Z_ENT, Z_MAX) VALUES ('REMCDAccount', 14, 1)"
        )

        # Insert account object row
        db.execute(
            "INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZNAME, ZMARKEDFORDELETION) VALUES (1, 14, ?, 0)",
            (name,),
        )

        # ZREMCDREPLICAMANAGER — determines account type
        db.execute(
            "CREATE TABLE ZREMCDREPLICAMANAGER (Z_PK INTEGER, Z_ENT INTEGER, ZIDENTIFIER TEXT)"
        )
        if account_type == "Exchange":
            identifier = "exchange-uuid/com.apple.exchangesync.exchangesyncd"
        else:
            identifier = "icloud-uuid/com.apple.reminders"
        db.execute(
            "INSERT INTO ZREMCDREPLICAMANAGER (Z_PK, Z_ENT, ZIDENTIFIER) VALUES (1, 15, ?)",
            (identifier,),
        )

        # ZREMCDHASHTAGLABEL — needed by q_hashtags
        db.execute(
            "CREATE TABLE ZREMCDHASHTAGLABEL (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZREMINDER3 INTEGER)"
        )

        # Insert reminders if provided
        if reminders:
            for rem in reminders:
                db.execute(
                    "INSERT INTO ZREMCDREMINDER "
                    "(Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
                    "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATEDELTAALERTSDATA, "
                    "ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, ZCKIDENTIFIER, "
                    "ZACCOUNT, ZMARKEDFORDELETION) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, 0)",
                    (
                        rem["Z_PK"],
                        rem["ZTITLE"],
                        rem.get("ZCOMPLETED", 0),
                        rem.get("ZFLAGGED", 0),
                        rem.get("ZPRIORITY", 0),
                        rem.get("ZISURGENTSTATEENABLEDFORCURRENTUSER", 0),
                        rem.get("ZDUEDATE"),
                        rem.get("ZDUEDATE"),
                        rem["ZLIST"],
                        rem.get("ZCKIDENTIFIER", f"CK-{rem['Z_PK']}"),
                        rem.get("ZACCOUNT", 1),
                    ),
                )

        return db

    @contextlib.contextmanager
    def _multi_account_patch(self, accounts):
        """Context manager patching discover_accounts and iter_account_dbs.

        accounts: list of (Account_namedtuple, sqlite3.Connection) pairs.
        """
        account_list = [acct for acct, _db in accounts]
        first_db = accounts[0][1] if accounts else None

        @contextlib.contextmanager
        def _fake_iter_account_dbs(scope):
            scope_names = {a.name.lower() for a in scope}
            yield [(acct, db) for acct, db in accounts if acct.name.lower() in scope_names]

        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=account_list),
            mock.patch.object(self.remctl, "iter_account_dbs", side_effect=_fake_iter_account_dbs),
            mock.patch.object(self.remctl, "open_db", return_value=first_db),
        ):
            yield

    # ── Group 1: discover_accounts / _store_account_info ─────────────────────

    def test_discover_accounts_excludes_hidden_and_empty_stores(self):
        """discover_accounts filters out LocalInternal and empty stores."""
        import pathlib

        fake_paths = [
            pathlib.Path(f"/tmp/fake-Data-{i}.sqlite") for i in range(3)
        ]

        info_map = {
            fake_paths[0]: ("iCloud", "iCloud"),
            fake_paths[1]: ("Work Exchange", "Exchange"),
            fake_paths[2]: ("LocalInternal", "Local"),
        }

        def fake_store_account_info(path):
            return info_map.get(path)

        def fake_reminder_count(path):
            return 5  # all non-empty

        # Build a fake STORE_DIR object that returns our paths from glob()
        class FakeStoreDir:
            def glob(self, pattern):
                return list(fake_paths)

        with (
            mock.patch.object(
                self.remctl, "reminders_store_access_error", return_value=None
            ),
            mock.patch.object(self.remctl, "DB_OVERRIDE", None),
            mock.patch.object(self.remctl, "STORE_DIR", FakeStoreDir()),
            mock.patch.object(
                self.remctl, "_store_account_info", side_effect=fake_store_account_info
            ),
            mock.patch.object(
                self.remctl, "_store_reminder_count", side_effect=fake_reminder_count
            ),
            mock.patch.object(
                self.remctl, "_store_file_group_mtime", return_value=0.0
            ),
        ):
            self.remctl._ACCOUNT_CACHE = None
            accounts = self.remctl.discover_accounts(force_refresh=True)

        names = [a.name for a in accounts]
        self.assertIn("iCloud", names)
        self.assertIn("Work Exchange", names)
        self.assertNotIn("LocalInternal", names)
        self.assertEqual(len(accounts), 2)

    def test_store_account_info_reads_name_and_type_from_db(self):
        """_store_account_info correctly extracts name and type for iCloud and Exchange.

        _store_account_info opens with immutable=1, so the db file must have no
        WAL/journal.  We create each db directly on disk (not via backup from
        :memory:) and use PRAGMA journal_mode=DELETE to ensure no WAL exists.
        """
        import tempfile, os

        def _make_disk_account_db(path, name, account_type):
            """Write a minimal account db directly to *path* on disk."""
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute(
                "CREATE TABLE Z_PRIMARYKEY (Z_NAME TEXT, Z_ENT INTEGER, Z_MAX INTEGER)"
            )
            conn.execute(
                "INSERT INTO Z_PRIMARYKEY (Z_NAME, Z_ENT, Z_MAX) VALUES ('REMCDAccount', 14, 1)"
            )
            conn.execute(
                "CREATE TABLE ZREMCDOBJECT (Z_PK INTEGER, Z_ENT INTEGER, "
                "ZNAME TEXT, ZMARKEDFORDELETION INTEGER)"
            )
            conn.execute(
                "INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZNAME, ZMARKEDFORDELETION) "
                "VALUES (1, 14, ?, 0)",
                (name,),
            )
            conn.execute(
                "CREATE TABLE ZREMCDREPLICAMANAGER "
                "(Z_PK INTEGER, Z_ENT INTEGER, ZIDENTIFIER TEXT)"
            )
            if account_type == "Exchange":
                ident = "exchange-uuid/com.apple.exchangesync.exchangesyncd"
            else:
                ident = "icloud-uuid/com.apple.reminders"
            conn.execute(
                "INSERT INTO ZREMCDREPLICAMANAGER (Z_PK, Z_ENT, ZIDENTIFIER) VALUES (1, 15, ?)",
                (ident,),
            )
            conn.commit()
            conn.close()

        # iCloud variant
        fd, icloud_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        _make_disk_account_db(icloud_path, "iCloud", "iCloud")
        try:
            result = self.remctl._store_account_info(icloud_path)
            self.assertIsNotNone(result)
            self.assertEqual(result[0], "iCloud")
            self.assertEqual(result[1], "iCloud")
        finally:
            os.unlink(icloud_path)

        # Exchange variant
        fd, exchange_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        _make_disk_account_db(exchange_path, "Work Exchange", "Exchange")
        try:
            result_ex = self.remctl._store_account_info(exchange_path)
            self.assertIsNotNone(result_ex)
            self.assertEqual(result_ex[0], "Work Exchange")
            self.assertEqual(result_ex[1], "Exchange")
        finally:
            os.unlink(exchange_path)

    # ── Group 2: resolve_account_scope ───────────────────────────────────────

    def test_resolve_account_scope_default_returns_first_account(self):
        """Default scope (no flags, no env) returns only the first account."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        self.remctl._ACCOUNT_CACHE = None
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(self.remctl, "load_config", return_value={}),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            result = self.remctl.resolve_account_scope(
                SimpleNamespace(account=None, all_accounts=False)
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "iCloud")

    def test_resolve_account_scope_all_accounts_flag(self):
        """--all-accounts returns all discovered accounts."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(self.remctl, "load_config", return_value={}),
        ):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            result = self.remctl.resolve_account_scope(
                SimpleNamespace(account=None, all_accounts=True)
            )
        self.assertEqual(len(result), 2)
        self.assertEqual({a.name for a in result}, {"iCloud", "Exchange"})

    def test_resolve_account_scope_named_account(self):
        """--account Exchange returns only the Exchange account."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(self.remctl, "load_config", return_value={}),
        ):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            result = self.remctl.resolve_account_scope(
                SimpleNamespace(account="Exchange", all_accounts=False)
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Exchange")

    def test_resolve_account_scope_unknown_account_exits(self):
        """Unknown --account name triggers sys.exit."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud]),
            mock.patch.object(self.remctl, "load_config", return_value={}),
        ):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            with self.assertRaises(SystemExit):
                self.remctl.resolve_account_scope(
                    SimpleNamespace(account="NoSuchAccount", all_accounts=False)
                )

    def test_resolve_account_scope_env_override(self):
        """REMCTL_ACCOUNT_SCOPE=all env var returns all accounts."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(self.remctl, "load_config", return_value={}),
            mock.patch.dict(os.environ, {"REMCTL_ACCOUNT_SCOPE": "all"}),
        ):
            result = self.remctl.resolve_account_scope(
                SimpleNamespace(account=None, all_accounts=False)
            )
        self.assertEqual(len(result), 2)

    def test_resolve_account_scope_config_override(self):
        """config.json accountScope=all returns all accounts."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(self.remctl, "load_config", return_value={"accountScope": "all"}),
        ):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            result = self.remctl.resolve_account_scope(
                SimpleNamespace(account=None, all_accounts=False)
            )
        self.assertEqual(len(result), 2)

    # ── Group 3: READ commands — single-account back-compat ──────────────────

    def test_cmd_lists_single_account_no_account_field_in_json(self):
        """Single-account cmd_lists JSON output has no 'account' key on any item."""
        db = self._list_db(["Inbox", "Work"])
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "load_config", return_value={}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(SimpleNamespace(json=True))
        finally:
            db.close()
        payload = json.loads(stdout.getvalue())
        self.assertGreater(len(payload), 0)
        for item in payload:
            self.assertNotIn("account", item)

    def _extend_due_window_db_for_serialization(self, db):
        """Add columns needed by to_dict/serialize_reminder to a _due_window_db instance."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS ZREMCDHASHTAGLABEL "
            "(Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZREMINDER3 INTEGER)"
        )
        for col in ("ZREMINDER", "ZREMINDER2", "ZREMINDER3", "ZHASHTAGLABEL",
                    "ZURL", "ZFILENAME", "ZTRIGGER", "ZTIMEINTERVAL",
                    "ZDATECOMPONENTSDATA", "ZTITLE", "ZLATITUDE", "ZLONGITUDE",
                    "ZRADIUS", "ZADDRESS", "ZPROXIMITY",
                    "ZASSIGNEE", "ZORIGINATOR", "ZSTATUS", "ZACCESSLEVEL"):
            try:
                db.execute(f"ALTER TABLE ZREMCDOBJECT ADD COLUMN {col}")
            except Exception:
                pass  # column may already exist

    def test_cmd_today_single_account_output_unchanged(self):
        """Single-account cmd_today JSON output has no 'account' key."""
        from datetime import datetime

        db = self._due_window_db()
        self._extend_due_window_db_for_serialization(db)
        now = datetime.now()
        due_ts = self.remctl.to_ts(now)
        self._insert_due_reminder(db, 1, "Today Task", due_ts=due_ts, display_ts=due_ts, all_day=False)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "load_config", return_value={}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_today(SimpleNamespace(json=True, no_overdue=False))
        finally:
            db.close()
        payload = json.loads(stdout.getvalue())
        self.assertGreater(len(payload), 0)
        for item in payload:
            self.assertNotIn("account", item)

    def test_cmd_search_single_account_output_unchanged(self):
        """Single-account cmd_search JSON output has no 'account' key."""
        db = self._due_window_db()
        self._extend_due_window_db_for_serialization(db)
        self._insert_due_reminder(db, 1, "Find me", due_ts=None, display_ts=None, all_day=False)
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "load_config", return_value={}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_search(
                    SimpleNamespace(json=True, query="Find me", completed=False,
                                    verbose=False, format=None)
                )
        finally:
            db.close()
        payload = json.loads(stdout.getvalue())
        self.assertGreater(len(payload), 0)
        for item in payload:
            self.assertNotIn("account", item)

    def test_cmd_flagged_single_account_output_unchanged(self):
        """Single-account cmd_flagged JSON output has no 'account' key."""
        db = self._due_window_db()
        self._extend_due_window_db_for_serialization(db)
        db.execute(
            "INSERT INTO ZREMCDREMINDER "
            "(Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
            "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATEDELTAALERTSDATA, "
            "ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, ZCKIDENTIFIER, "
            "ZACCOUNT, ZMARKEDFORDELETION) "
            "VALUES (1, 'Flagged Task', NULL, 0, 1, 0, 0, NULL, NULL, NULL, 0, 1, 'CK-1', 1, 0)"
        )
        try:
            with (
                mock.patch.object(self.remctl, "open_db", return_value=db),
                mock.patch.object(self.remctl, "load_config", return_value={}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_flagged(
                    SimpleNamespace(json=True, verbose=False, format=None)
                )
        finally:
            db.close()
        payload = json.loads(stdout.getvalue())
        self.assertGreater(len(payload), 0)
        for item in payload:
            self.assertNotIn("account", item)

    # ── Group 4: READ commands — multi-account (--all-accounts) ──────────────

    def test_cmd_lists_all_accounts_json_includes_account_field(self):
        """Multi-account cmd_lists JSON includes 'account' and 'accountType' on each item."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Inbox", "CK-1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Tasks", "CK-2")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(
                    SimpleNamespace(json=True, all_accounts=True, account=None, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        titles = [item["title"] for item in payload]
        self.assertIn("Inbox", titles)
        self.assertIn("Tasks", titles)
        for item in payload:
            self.assertIn("account", item)
            self.assertIn("accountType", item)

    def test_cmd_lists_all_accounts_human_shows_account_headers(self):
        """Multi-account cmd_lists human output shows [iCloud] and [Exchange] headers."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Inbox", "CK-1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Tasks", "CK-2")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(
                    SimpleNamespace(json=False, all_accounts=True, account=None, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        output = stdout.getvalue()
        self.assertIn("iCloud", output)
        self.assertIn("Exchange", output)

    def test_cmd_today_all_accounts_json_includes_account_field(self):
        """Multi-account cmd_today JSON includes 'account' on each item."""
        from datetime import datetime

        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")

        now = datetime.now()
        due_ts = self.remctl.to_ts(now)
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud Task", "ZLIST": 1, "ZDUEDATE": due_ts}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 2, "ZTITLE": "Exchange Task", "ZLIST": 1, "ZDUEDATE": due_ts}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_today(
                    SimpleNamespace(json=True, all_accounts=True, account=None,
                                    no_overdue=False, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertGreater(len(payload), 0)
        for item in payload:
            self.assertIn("account", item)
        names = {item["account"] for item in payload}
        self.assertIn("iCloud", names)
        self.assertIn("Exchange", names)

    def test_cmd_today_all_accounts_human_output_has_full_formatting(self):
        """Multi-account cmd_today human output contains '[ ]' checkbox format (regression guard for closed-db bug)."""
        from datetime import datetime

        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")

        now = datetime.now()
        due_ts = self.remctl.to_ts(now)
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "Task Alpha", "ZLIST": 1, "ZDUEDATE": due_ts}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 2, "ZTITLE": "Task Beta", "ZLIST": 1, "ZDUEDATE": due_ts}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_today(
                    SimpleNamespace(json=False, all_accounts=True, account=None,
                                    no_overdue=False, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        output = self.remctl._strip_ansi(stdout.getvalue())
        # The checkbox format from fmt() must be present — this is the regression guard
        self.assertIn("[ ]", output)
        self.assertIn("Task Alpha", output)
        self.assertIn("Task Beta", output)

    def test_cmd_search_all_accounts_merges_results(self):
        """Multi-account cmd_search returns results from both accounts with account labels."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "Meeting notes iCloud", "ZLIST": 1}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 2, "ZTITLE": "Meeting notes Exchange", "ZLIST": 1}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_search(
                    SimpleNamespace(json=True, query="Meeting notes", all_accounts=True,
                                    account=None, completed=False, verbose=False, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        for item in payload:
            self.assertIn("account", item)

    def test_cmd_flagged_all_accounts_merges_results(self):
        """Multi-account cmd_flagged returns flagged reminders from both accounts."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud Flagged", "ZLIST": 1, "ZFLAGGED": 1}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 2, "ZTITLE": "Exchange Flagged", "ZLIST": 1, "ZFLAGGED": 1}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_flagged(
                    SimpleNamespace(json=True, all_accounts=True, account=None,
                                    verbose=False, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        for item in payload:
            self.assertIn("account", item)
        titles = {item["title"] for item in payload}
        self.assertIn("iCloud Flagged", titles)
        self.assertIn("Exchange Flagged", titles)

    def test_cmd_urgent_all_accounts_merges_results(self):
        """Multi-account cmd_urgent returns urgent reminders from both accounts."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud Urgent",
                        "ZLIST": 1, "ZISURGENTSTATEENABLEDFORCURRENTUSER": 1}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 2, "ZTITLE": "Exchange Urgent",
                        "ZLIST": 1, "ZISURGENTSTATEENABLEDFORCURRENTUSER": 1}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_urgent(
                    SimpleNamespace(json=True, all_accounts=True, account=None,
                                    verbose=False, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        for item in payload:
            self.assertIn("account", item)

    def test_cmd_overdue_all_accounts_merges_results(self):
        """Multi-account cmd_overdue returns overdue reminders from both accounts."""
        from datetime import datetime, timedelta

        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")

        yesterday = datetime.now() - timedelta(days=2)
        past_ts = self.remctl.to_ts(yesterday)
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud Overdue", "ZLIST": 1, "ZDUEDATE": past_ts}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 2, "ZTITLE": "Exchange Overdue", "ZLIST": 1, "ZDUEDATE": past_ts}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_overdue(
                    SimpleNamespace(json=True, all_accounts=True, account=None,
                                    verbose=False, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        for item in payload:
            self.assertIn("account", item)

    # ── Group 5: READ commands — --account filter ─────────────────────────────

    def test_cmd_lists_account_filter_returns_only_that_account(self):
        """--account iCloud returns only iCloud lists, not Exchange."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Inbox", "CK-1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Tasks", "CK-2")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_lists(
                    SimpleNamespace(json=True, all_accounts=False,
                                    account="iCloud", format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        titles = [item["title"] for item in payload]
        self.assertIn("Inbox", titles)
        self.assertNotIn("Tasks", titles)

    def test_cmd_show_account_flag_queries_correct_store(self):
        """--account Exchange for show uses Exchange's list, not iCloud's."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        # Both accounts have list Z_PK=1 but different names
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "iCloud-List", "CK-icloud-1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud reminder", "ZLIST": 1}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Exchange-List", "CK-exch-1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "Exchange reminder", "ZLIST": 1}],
        )
        connected_paths = []

        # Wrap sqlite3.connect in the remctl module so exception types remain intact
        original_sqlite3_connect = self.remctl.sqlite3.connect

        def fake_connect(path, **kwargs):
            connected_paths.append(str(path))
            # Strip the URI parameters and return the correct in-memory db
            if "fake-exchange" in str(path):
                exchange_db.row_factory = sqlite3.Row
                return exchange_db
            return original_sqlite3_connect(path, **kwargs)

        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                mock.patch.object(self.remctl.sqlite3, "connect", side_effect=fake_connect),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_show(
                    SimpleNamespace(
                        json=True, account="Exchange",
                        list="Exchange-List", list_id=None,
                        completed=False, verbose=False, format=None,
                    )
                )
        finally:
            icloud_db.close()
            # exchange_db is returned directly; don't double-close via fake_connect
        output = stdout.getvalue()
        payload = json.loads(output)
        titles = [item["title"] for item in payload]
        self.assertIn("Exchange reminder", titles)
        self.assertTrue(
            any("fake-exchange" in p for p in connected_paths),
            f"Expected Exchange store path in connect calls; got: {connected_paths}",
        )

    def test_cmd_today_account_filter(self):
        """--account iCloud for today returns only iCloud's due reminders."""
        from datetime import datetime

        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")

        now = datetime.now()
        due_ts = self.remctl.to_ts(now)
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud Today", "ZLIST": 1, "ZDUEDATE": due_ts}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 2, "ZTITLE": "Exchange Today", "ZLIST": 1, "ZDUEDATE": due_ts}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_today(
                    SimpleNamespace(json=True, all_accounts=False, account="iCloud",
                                    no_overdue=False, format=None)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        titles = [item["title"] for item in payload]
        self.assertIn("iCloud Today", titles)
        self.assertNotIn("Exchange Today", titles)

    # ── Group 6: Ambiguity / error cases ─────────────────────────────────────

    def test_resolve_list_ref_across_ambiguous_same_name(self):
        """Same list name in two accounts returns error='ambiguous' with candidates."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Work", "CK-i1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Work", "CK-e1")])
        try:
            result = self.remctl.resolve_list_ref_across(
                [(icloud_acct, icloud_db), (exchange_acct, exchange_db)],
                name="Work",
                account_name=None,
            )
        finally:
            icloud_db.close()
            exchange_db.close()
        self.assertIsNotNone(result)
        self.assertEqual(result.get("error"), "ambiguous")
        candidates = result.get("candidates", [])
        self.assertEqual(len(candidates), 2)
        candidate_accounts = {c.get("account") for c in candidates}
        self.assertIn("iCloud", candidate_accounts)
        self.assertIn("Exchange", candidate_accounts)

    def test_resolve_list_ref_across_resolved_by_account_name(self):
        """account_name disambiguates a list that exists in both accounts."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Work", "CK-i1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Work", "CK-e2")])
        try:
            result = self.remctl.resolve_list_ref_across(
                [(icloud_acct, icloud_db), (exchange_acct, exchange_db)],
                name="Work",
                account_name="iCloud",
            )
        finally:
            icloud_db.close()
            exchange_db.close()
        self.assertIsNotNone(result)
        self.assertNotEqual(result.get("error"), "ambiguous")
        self.assertEqual(result.get("account"), "iCloud")
        self.assertEqual(result.get("title"), "Work")

    def test_resolve_reminder_across_ambiguous_same_pk(self):
        """Same Z_PK in two accounts without account_name causes sys.exit."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud rem", "ZLIST": 1}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 1, "ZTITLE": "Exchange rem", "ZLIST": 1}],
        )
        try:
            with self.assertRaises(SystemExit):
                self.remctl.resolve_reminder_across(
                    [(icloud_acct, icloud_db), (exchange_acct, exchange_db)],
                    1,
                    account_name=None,
                )
        finally:
            icloud_db.close()
            exchange_db.close()

    def test_resolve_reminder_across_resolved_by_account(self):
        """account_name disambiguates when same Z_PK exists in two accounts."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud rem", "ZLIST": 1}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 1, "ZTITLE": "Exchange rem", "ZLIST": 1}],
        )
        try:
            result = self.remctl.resolve_reminder_across(
                [(icloud_acct, icloud_db), (exchange_acct, exchange_db)],
                1,
                account_name="Exchange",
            )
        finally:
            icloud_db.close()
            exchange_db.close()
        self.assertIsNotNone(result)
        acct, db, row = result
        self.assertEqual(acct.name, "Exchange")
        self.assertEqual(row["ZTITLE"], "Exchange rem")

    # ── Group 6b: sharees / template-info / link cross-account scanning ──────

    def test_cmd_sharees_single_account_json_omits_internal_fields(self):
        """`sharees --account NAME` (single scope) must not leak _store_path/
        account/accountType into the JSON 'list' payload — byte-compat with the
        original single-account output."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(2, "Reminders", "CK-r2")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_sharees(
                    SimpleNamespace(list="Reminders", list_id=None,
                                    json=True, account="iCloud", all_accounts=False)
                )
        finally:
            icloud_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["list"]["title"], "Reminders")
        # internal/multi-only keys must be absent for single-account scope
        self.assertNotIn("_store_path", payload["list"])
        self.assertNotIn("account", payload["list"])
        self.assertNotIn("accountType", payload["list"])
        self.assertEqual(payload["sharees"], [])

    def test_cmd_sharees_all_accounts_ambiguous_lists_account_qualified(self):
        """`sharees NAME --all-accounts` on a name present in two accounts errors
        with account-qualified candidates (cross-account scan)."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Work", "CK-i1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Work", "CK-e1")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_sharees(
                    SimpleNamespace(list="Work", list_id=None,
                                    json=False, account=None, all_accounts=True)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        msg = stderr.getvalue()
        self.assertIn("iCloud", msg)
        self.assertIn("Exchange", msg)
        self.assertIn("--account", msg)

    def test_cmd_sharees_account_filter_resolves_in_target_store(self):
        """`sharees NAME --account X` resolves the list in the named account even
        when the same name exists elsewhere (no ambiguity error)."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Work", "CK-i1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Work", "CK-e1")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_sharees(
                    SimpleNamespace(list="Work", list_id=None,
                                    json=True, account="Exchange", all_accounts=False)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["list"]["title"], "Work")
        # single-account scope: no leaked label fields
        self.assertNotIn("_store_path", payload["list"])
        self.assertNotIn("account", payload["list"])

    def test_cmd_link_all_accounts_scans_for_reminder_id(self):
        """`link <id> --all-accounts` finds the reminder in whichever store holds
        it and emits its deep link."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-L1")],
            reminders=[{"Z_PK": 1, "ZTITLE": "iCloud rem", "ZLIST": 1, "ZCKIDENTIFIER": "ICK-1"}],
        )
        exchange_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L2")],
            reminders=[{"Z_PK": 9, "ZTITLE": "Exchange rem", "ZLIST": 1, "ZCKIDENTIFIER": "ECK-9"}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_link(
                    SimpleNamespace(ids=[9], list=None, list_id=None,
                                    completed=False, json=True,
                                    account=None, all_accounts=True)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["id"], 9)
        self.assertEqual(payload[0]["title"], "Exchange rem")
        self.assertIn("ECK-9", payload[0]["link"])
        # output is the original 3-field shape — no internal keys
        self.assertEqual(set(payload[0].keys()), {"id", "title", "link"})

    def test_cmd_template_info_all_accounts_not_found(self):
        """`template-info NAME --all-accounts` reports not-found cleanly when no
        store has the template."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Inbox", "CK-L1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(1, "Tasks", "CK-L2")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_template_info(
                    SimpleNamespace(name="Nonexistent", template_id=None,
                                    json=False, account=None, all_accounts=True)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        self.assertIn("not found", stderr.getvalue())

    def test_cmd_export_refuses_multi_account_scope(self):
        """export must refuse an 'all-accounts' scope (rc=2) rather than silently
        exporting only the default account — guards against partial backups."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange_acct = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        icloud_db = self._make_account_db("iCloud", "iCloud", [(1, "Inbox", "CK-1")])
        exchange_db = self._make_account_db("Exchange", "Exchange", [(2, "Tasks", "CK-2")])
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db), (exchange_acct, exchange_db)]),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit) as cm,
            ):
                self.remctl.cmd_export(
                    SimpleNamespace(account=None, all_accounts=True,
                                    list=None, list_id=None, export_format="json", json=False)
                )
        finally:
            icloud_db.close()
            exchange_db.close()
        self.assertEqual(cm.exception.code, 2)
        msg = stderr.getvalue()
        self.assertIn("single account", msg)
        self.assertIn("--account", msg)

    def test_cmd_export_single_account_scope_proceeds(self):
        """export with a single-account scope opens that account and dumps it
        (no multi-scope error)."""
        icloud_acct = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        icloud_db = self._make_account_db(
            "iCloud", "iCloud", [(1, "Inbox", "CK-1")],
            reminders=[{"Z_PK": 5, "ZTITLE": "Solo", "ZLIST": 1, "ZCKIDENTIFIER": "CK-5"}],
        )
        try:
            with (
                self._multi_account_patch([(icloud_acct, icloud_db)]),
                mock.patch.object(self.remctl, "_open_db_for_account", return_value=icloud_db),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_export(
                    SimpleNamespace(account=None, all_accounts=False,
                                    list=None, list_id=None, export_format="json", json=False)
                )
        finally:
            icloud_db.close()
        payload = json.loads(stdout.getvalue())
        self.assertTrue(any(r.get("title") == "Solo" for r in payload))

    # ── Group 7: WRITE commands — _resolve_reminder_for_write ────────────────

    def test_resolve_reminder_for_write_default_uses_open_db(self):
        """Without --account, _resolve_reminder_for_write calls open_db()."""
        fake_row = {"ZTITLE": "My Task", "ZCKIDENTIFIER": "ABC", "ZLIST": 1}
        fake_db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=fake_db) as open_db_mock,
            mock.patch.object(self.remctl, "q_reminder", return_value=fake_row),
        ):
            db, row = self.remctl._resolve_reminder_for_write(SimpleNamespace(id=1, account=None))
        open_db_mock.assert_called_once()
        self.assertIs(db, fake_db)
        self.assertEqual(row["ZTITLE"], "My Task")

    def test_resolve_reminder_for_write_account_flag_opens_correct_store(self):
        """--account Exchange opens Exchange's store_path via sqlite3.connect."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        fake_row = {"ZTITLE": "Exchange Task", "ZCKIDENTIFIER": "XYZ", "ZLIST": 1}
        fake_db = mock.Mock()
        fake_db.row_factory = None
        connected_paths = []

        def fake_connect(path, **kwargs):
            connected_paths.append(path)
            return fake_db

        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(sqlite3, "connect", side_effect=fake_connect),
            mock.patch.object(self.remctl, "q_reminder", return_value=fake_row),
        ):
            db, row = self.remctl._resolve_reminder_for_write(
                SimpleNamespace(id=1, account="Exchange")
            )
        # The connect path should contain the Exchange store path
        self.assertTrue(
            any("fake-exchange.sqlite" in str(p) for p in connected_paths),
            f"Expected Exchange store path in connect calls; got: {connected_paths}",
        )
        self.assertEqual(row["ZTITLE"], "Exchange Task")

    def test_resolve_reminder_for_write_not_found_exits(self):
        """Reminder not found triggers sys.exit."""
        fake_db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=fake_db),
            mock.patch.object(self.remctl, "q_reminder", return_value=None),
            self.assertRaises(SystemExit),
        ):
            self.remctl._resolve_reminder_for_write(SimpleNamespace(id=99, account=None))

    def test_resolve_reminder_for_write_unknown_account_exits(self):
        """Unknown --account name triggers sys.exit with useful message."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud]),
        ):
            with self.assertRaises(SystemExit):
                self.remctl._resolve_reminder_for_write(
                    SimpleNamespace(id=1, account="NoSuchAccount")
                )

    # ── Group 8: WRITE commands — done/undone/delete/flag/unflag with --account ──

    def _make_write_reminder(self, title="Task", ckid="CK-WRITE-001"):
        return {
            "ZCKIDENTIFIER": ckid,
            "ZTITLE": title,
            "list_name": "Inbox",
            "ZLIST": 1,
            "ZDUEDATE": None,
        }

    def test_cmd_done_with_account_flag_looks_up_correct_store(self):
        """cmd_done with --account Exchange calls bridge_call with reminder's ZCKIDENTIFIER."""
        fake_row = self._make_write_reminder("Exchange Done Task", "EXCH-DONE-001")
        fake_db = mock.Mock()
        with (
            mock.patch.object(
                self.remctl, "_resolve_reminder_for_write", return_value=(fake_db, fake_row)
            ),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call",
                return_value={"status": "completed", "id": fake_row["ZCKIDENTIFIER"]},
            ) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_done(SimpleNamespace(id=1, json=True, account="Exchange"))
        bridge_call.assert_called_once()
        payload = bridge_call.call_args.args[0]
        self.assertEqual(payload["id"], "EXCH-DONE-001")
        self.assertEqual(payload["action"], "complete")

    def test_cmd_undone_with_account_flag(self):
        """cmd_undone with --account Exchange calls bridge_call with the correct identifier."""
        fake_row = self._make_write_reminder("Exchange Undone Task", "EXCH-UNDONE-001")
        fake_db = mock.Mock()
        with (
            mock.patch.object(
                self.remctl, "_resolve_reminder_for_write", return_value=(fake_db, fake_row)
            ),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call",
                return_value={"status": "uncompleted", "id": fake_row["ZCKIDENTIFIER"]},
            ) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_undone(SimpleNamespace(id=1, json=True, account="Exchange"))
        bridge_call.assert_called_once()
        payload = bridge_call.call_args.args[0]
        self.assertEqual(payload["id"], "EXCH-UNDONE-001")
        self.assertEqual(payload["action"], "uncomplete")

    def test_cmd_delete_with_account_flag(self):
        """cmd_delete with --account Exchange and force=True calls bridge_call delete."""
        fake_row = self._make_write_reminder("Exchange Delete Task", "EXCH-DEL-001")
        fake_db = mock.Mock()
        with (
            mock.patch.object(
                self.remctl, "_resolve_reminder_for_write", return_value=(fake_db, fake_row)
            ),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call",
                return_value={"status": "deleted", "id": fake_row["ZCKIDENTIFIER"]},
            ) as bridge_call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_delete(
                SimpleNamespace(id=1, json=True, account="Exchange", force=True)
            )
        bridge_call.assert_called_once()
        payload = bridge_call.call_args.args[0]
        self.assertEqual(payload["id"], "EXCH-DEL-001")
        self.assertEqual(payload["action"], "delete")

    def test_cmd_flag_with_account_flag(self):
        """cmd_flag with --account Exchange uses AppleScript (flagged-first path)."""
        fake_row = self._make_write_reminder("Exchange Flag Task", "EXCH-FLAG-001")
        fake_db = mock.Mock()
        with (
            mock.patch.object(
                self.remctl, "_resolve_reminder_for_write", return_value=(fake_db, fake_row)
            ),
            mock.patch.object(
                self.remctl, "osa_by_id_try", return_value=True
            ) as osa_mock,
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_flag(SimpleNamespace(id=1, json=True, account="Exchange"))
        osa_mock.assert_called_once()
        script = osa_mock.call_args.args[2]
        self.assertIn("set flagged of r to true", script)

    def test_cmd_unflag_with_account_flag(self):
        """cmd_unflag with --account Exchange uses AppleScript (unflag-first path)."""
        fake_row = self._make_write_reminder("Exchange Unflag Task", "EXCH-UNFLAG-001")
        fake_db = mock.Mock()
        with (
            mock.patch.object(
                self.remctl, "_resolve_reminder_for_write", return_value=(fake_db, fake_row)
            ),
            mock.patch.object(
                self.remctl, "osa_by_id_try", return_value=True
            ) as osa_mock,
            mock.patch.object(self.remctl, "bridge_available", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_unflag(SimpleNamespace(id=1, json=True, account="Exchange"))
        osa_mock.assert_called_once()
        script = osa_mock.call_args.args[2]
        self.assertIn("set flagged of r to false", script)

    # ── Group 9: WRITE — cmd_add with --account ───────────────────────────────

    def test_cmd_add_with_account_opens_correct_store_for_list_lookup(self):
        """cmd_add with account=["Exchange"] connects to Exchange's store_path for list lookup."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        connected_paths = []

        def fake_connect(path, **kwargs):
            connected_paths.append(str(path))
            fake_db = mock.Mock()
            fake_db.row_factory = None
            return fake_db

        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(sqlite3, "connect", side_effect=fake_connect),
            mock.patch.object(
                self.remctl, "resolve_list_or_die",
                return_value={"id": 1, "title": "Tasks", "requested": "Tasks", "method": "exact"},
            ),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call",
                return_value={"status": "created", "id": "NEW-001"},
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            mock.patch.object(self.remctl, "private_metadata_enabled", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_add(
                SimpleNamespace(
                    title="New Task",
                    list="Tasks",
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
                    account=["Exchange"],
                )
            )
        self.assertTrue(
            any("fake-exchange.sqlite" in p for p in connected_paths),
            f"Expected Exchange store path in connect calls; got: {connected_paths}",
        )

    def test_cmd_add_without_account_uses_open_db(self):
        """cmd_add without --account calls open_db() for list lookup."""
        fake_db = mock.Mock()
        fake_db.row_factory = None
        with (
            mock.patch.object(self.remctl, "open_db", return_value=fake_db) as open_db_mock,
            mock.patch.object(
                self.remctl, "resolve_list_or_die",
                return_value={"id": 1, "title": "Inbox", "requested": "Inbox", "method": "exact"},
            ),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(
                self.remctl, "bridge_call",
                return_value={"status": "created", "id": "NEW-002"},
            ),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            mock.patch.object(self.remctl, "private_metadata_enabled", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_add(
                SimpleNamespace(
                    title="No Account Task",
                    list="Inbox",
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
                    account=None,
                )
            )
        open_db_mock.assert_called()

    # ── Group 10: config / accounts command ──────────────────────────────────

    def test_cmd_accounts_json_lists_all_discovered_accounts(self):
        """cmd_accounts --json lists all discovered accounts with name, type, storePath."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        with (
            mock.patch.object(
                self.remctl, "discover_accounts", return_value=[icloud, exchange]
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_accounts(SimpleNamespace(json=True))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        names = {item["name"] for item in payload}
        self.assertIn("iCloud", names)
        self.assertIn("Exchange", names)
        for item in payload:
            self.assertIn("name", item)
            self.assertIn("type", item)
            self.assertIn("storePath", item)

    def test_cmd_accounts_human_shows_default_label(self):
        """cmd_accounts human output marks the first account as (default)."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        with (
            mock.patch.object(
                self.remctl, "discover_accounts", return_value=[icloud, exchange]
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_accounts(SimpleNamespace(json=False))
        output = self.remctl._strip_ansi(stdout.getvalue())
        self.assertIn("(default)", output)
        # The first account (iCloud) should be the default
        lines = output.splitlines()
        default_line = next((l for l in lines if "(default)" in l), "")
        self.assertIn("iCloud", default_line)

    def test_cmd_config_set_and_get(self):
        """cmd_config set accountScope all; then get returns 'all'."""
        config_store = {}

        def fake_load_config():
            return dict(config_store)

        def fake_save_config(data):
            config_store.clear()
            config_store.update(data)

        with (
            mock.patch.object(self.remctl, "load_config", side_effect=fake_load_config),
            mock.patch.object(self.remctl, "save_config", side_effect=fake_save_config),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_config(
                SimpleNamespace(key="accountScope", value="all", json=False)
            )

        self.assertEqual(config_store.get("accountScope"), "all")

        with (
            mock.patch.object(self.remctl, "load_config", side_effect=fake_load_config),
            mock.patch.object(self.remctl, "save_config", side_effect=fake_save_config),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_config(
                SimpleNamespace(key="accountScope", value=None, json=False)
            )
        self.assertIn("all", stdout.getvalue())

    def test_is_multi_account_mode_false_by_default(self):
        """Default args with no flags, no env, no config returns False."""
        with (
            mock.patch.object(self.remctl, "load_config", return_value={}),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            result = self.remctl._is_multi_account_mode(
                SimpleNamespace(all_accounts=False, account=None)
            )
        self.assertFalse(result)

    def test_is_multi_account_mode_true_with_all_accounts_flag(self):
        """all_accounts=True returns True from _is_multi_account_mode."""
        with mock.patch.object(self.remctl, "load_config", return_value={}):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            result = self.remctl._is_multi_account_mode(
                SimpleNamespace(all_accounts=True, account=None)
            )
        self.assertTrue(result)

    def test_is_multi_account_mode_true_with_account_flag(self):
        """account='iCloud' returns True from _is_multi_account_mode."""
        with mock.patch.object(self.remctl, "load_config", return_value={}):
            os.environ.pop("REMCTL_ACCOUNT_SCOPE", None)
            result = self.remctl._is_multi_account_mode(
                SimpleNamespace(all_accounts=False, account="iCloud")
            )
        self.assertTrue(result)

    def test_is_multi_account_mode_true_with_env(self):
        """REMCTL_ACCOUNT_SCOPE=all env var returns True from _is_multi_account_mode."""
        with (
            mock.patch.object(self.remctl, "load_config", return_value={}),
            mock.patch.dict(os.environ, {"REMCTL_ACCOUNT_SCOPE": "all"}),
        ):
            result = self.remctl._is_multi_account_mode(
                SimpleNamespace(all_accounts=False, account=None)
            )
        self.assertTrue(result)

    def test_is_multi_account_mode_true_with_config_scope(self):
        """accountScope in config makes _is_multi_account_mode True."""
        with mock.patch.object(self.remctl, "load_config", return_value={"accountScope": "all"}):
            result = self.remctl._is_multi_account_mode(
                SimpleNamespace(all_accounts=False, account=None)
            )
        self.assertTrue(result)

    # ── Group 11: _open_db_for_account ───────────────────────────────────────

    def test_open_db_for_account_none_uses_open_db(self):
        """_open_db_for_account(None) falls back to open_db()."""
        sentinel = mock.Mock()
        with mock.patch.object(self.remctl, "open_db", return_value=sentinel) as open_db_mock:
            result = self.remctl._open_db_for_account(None)
        open_db_mock.assert_called_once()
        self.assertIs(result, sentinel)

    def test_open_db_for_account_named_opens_correct_store(self):
        """_open_db_for_account('Exchange') connects to Exchange's store_path."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        connected = []

        def fake_connect(conn_str, *args, **kwargs):
            connected.append(str(conn_str))
            return mock.Mock()

        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(sqlite3, "connect", side_effect=fake_connect),
        ):
            self.remctl._open_db_for_account("Exchange")
        self.assertTrue(any("fake-exchange.sqlite" in p for p in connected),
                        f"got: {connected}")

    def test_open_db_for_account_case_insensitive(self):
        """Account name matching is case-insensitive."""
        exchange = self.remctl.Account("/tmp/fake-exchange.sqlite", "Exchange", "Exchange")
        connected = []
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[exchange]),
            mock.patch.object(sqlite3, "connect", side_effect=lambda conn_str, *a, **k: connected.append(str(conn_str)) or mock.Mock()),
        ):
            self.remctl._open_db_for_account("EXCHANGE")
        self.assertTrue(any("fake-exchange.sqlite" in p for p in connected))

    def test_open_db_for_account_unknown_exits(self):
        """Unknown account name exits with an error."""
        icloud = self.remctl.Account("/tmp/fake-icloud.sqlite", "iCloud", "iCloud")
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud]),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl._open_db_for_account("NoSuchAccount")
        self.assertIn("NoSuchAccount", stderr.getvalue())

    # ── Group 12: _ek_identifier (Exchange identifier resolution) ────────────

    def test_ek_identifier_returns_ckidentifier_for_icloud(self):
        """When ZCKIDENTIFIER is present it is returned directly, no bridge call."""
        r = {"ZCKIDENTIFIER": "CK-ABC", "list_name": "Reminders", "ZTITLE": "Task"}
        with mock.patch.object(self.remctl, "bridge_call") as bridge_call:
            result = self.remctl._ek_identifier(r)
        self.assertEqual(result, "CK-ABC")
        bridge_call.assert_not_called()

    def test_ek_identifier_falls_back_to_find_reminder_for_exchange(self):
        """Exchange reminder (no ZCKIDENTIFIER) resolves via list_calendars + find_reminder."""
        r = {"ZCKIDENTIFIER": None, "list_name": "Tasks", "ZTITLE": "Exch Task"}

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Tasks", "calendarIdentifier": "CAL-EX",
                         "sourceTitle": "Exchange", "sourceType": "Exchange"}]
            if data["action"] == "find_reminder":
                self.assertEqual(data["calendarIdentifier"], "CAL-EX")
                self.assertEqual(data["title"], "Exch Task")
                return {"calendarItemIdentifier": "EK-ITEM-99"}
            return None

        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
        ):
            result = self.remctl._ek_identifier(r, account_name="Exchange")
        self.assertEqual(result, "EK-ITEM-99")

    def test_ek_identifier_narrows_by_account_when_list_name_collides(self):
        """When the same list name exists in two accounts, the account hint selects the right calendar."""
        r = {"ZCKIDENTIFIER": None, "list_name": "Tasks", "ZTITLE": "T"}

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [
                    {"title": "Tasks", "calendarIdentifier": "CAL-A", "sourceTitle": "AcctA"},
                    {"title": "Tasks", "calendarIdentifier": "CAL-B", "sourceTitle": "AcctB"},
                ]
            if data["action"] == "find_reminder":
                self.assertEqual(data["calendarIdentifier"], "CAL-B")
                return {"calendarItemIdentifier": "EK-B"}
            return None

        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
        ):
            result = self.remctl._ek_identifier(r, account_name="AcctB")
        self.assertEqual(result, "EK-B")

    def test_ek_identifier_none_when_bridge_unavailable(self):
        """No ZCKIDENTIFIER and no bridge → None."""
        r = {"ZCKIDENTIFIER": None, "list_name": "Tasks", "ZTITLE": "T"}
        with mock.patch.object(self.remctl, "bridge_available", return_value=False):
            self.assertIsNone(self.remctl._ek_identifier(r))

    def test_ek_identifier_none_when_no_list_or_title(self):
        """No ZCKIDENTIFIER and missing list_name/title → None."""
        r = {"ZCKIDENTIFIER": None, "list_name": None, "ZTITLE": None}
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
        ):
            self.assertIsNone(self.remctl._ek_identifier(r))
        bridge_call.assert_not_called()

    # ── Group 13: _resolve_reminder_for_write cross-account scan ─────────────

    def _connect_by_path(self, mapping):
        """Return a sqlite3.connect replacement mapping store paths to in-memory dbs."""
        def fake_connect(conn_str, *args, **kwargs):
            for path, db in mapping.items():
                if path in str(conn_str):
                    return db
            raise sqlite3.OperationalError(f"unmapped uri: {conn_str}")
        return fake_connect

    def test_resolve_reminder_for_write_multi_scope_unique_match(self):
        """Multi-account scope: a unique id match resolves and stamps a.account."""
        icloud = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 50, "ZTITLE": "iC", "ZLIST": 1}])
        ex_db = self._make_account_db("Exchange", "Exchange", [(1, "Tasks", "CK-2")],
                                      reminders=[{"Z_PK": 99, "ZTITLE": "ExOnly", "ZLIST": 1}])
        a = SimpleNamespace(id=99, account=None)
        try:
            with (
                mock.patch.object(self.remctl, "_is_multi_account_mode", return_value=True),
                mock.patch.object(self.remctl, "resolve_account_scope", return_value=[icloud, exchange]),
                mock.patch.object(sqlite3, "connect",
                                  side_effect=self._connect_by_path({"/tmp/ic.sqlite": ic_db,
                                                                     "/tmp/ex.sqlite": ex_db})),
            ):
                db, row = self.remctl._resolve_reminder_for_write(a)
            self.assertEqual(row["ZTITLE"], "ExOnly")
            self.assertEqual(a.account, "Exchange")  # stamped for downstream write path
        finally:
            ic_db.close()
            ex_db.close()

    def test_resolve_reminder_for_write_multi_scope_ambiguous_exits(self):
        """Same id in two active accounts raises an ambiguity error."""
        a1 = self.remctl.Account("/tmp/a1.sqlite", "AcctA", "Exchange")
        a2 = self.remctl.Account("/tmp/a2.sqlite", "AcctB", "Exchange")
        db1 = self._make_account_db("AcctA", "Exchange", [(1, "Tasks", "CK-1")],
                                    reminders=[{"Z_PK": 5, "ZTITLE": "A5", "ZLIST": 1}])
        db2 = self._make_account_db("AcctB", "Exchange", [(1, "Tasks", "CK-2")],
                                    reminders=[{"Z_PK": 5, "ZTITLE": "B5", "ZLIST": 1}])
        try:
            with (
                mock.patch.object(self.remctl, "_is_multi_account_mode", return_value=True),
                mock.patch.object(self.remctl, "resolve_account_scope", return_value=[a1, a2]),
                mock.patch.object(sqlite3, "connect",
                                  side_effect=self._connect_by_path({"/tmp/a1.sqlite": db1,
                                                                     "/tmp/a2.sqlite": db2})),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl._resolve_reminder_for_write(SimpleNamespace(id=5, account=None))
            err = stderr.getvalue()
            self.assertIn("multiple accounts", err)
            self.assertIn("AcctA", err)
            self.assertIn("AcctB", err)
        finally:
            db1.close()
            db2.close()

    def test_resolve_reminder_for_write_multi_scope_not_found_exits(self):
        """An id absent from every active account exits cleanly."""
        a1 = self.remctl.Account("/tmp/a1.sqlite", "AcctA", "Exchange")
        db1 = self._make_account_db("AcctA", "Exchange", [(1, "Tasks", "CK-1")],
                                    reminders=[{"Z_PK": 1, "ZTITLE": "A1", "ZLIST": 1}])
        try:
            with (
                mock.patch.object(self.remctl, "_is_multi_account_mode", return_value=True),
                mock.patch.object(self.remctl, "resolve_account_scope", return_value=[a1]),
                mock.patch.object(sqlite3, "connect",
                                  side_effect=self._connect_by_path({"/tmp/a1.sqlite": db1})),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self.remctl._resolve_reminder_for_write(SimpleNamespace(id=777, account=None))
        finally:
            db1.close()

    # ── Group 14: _gather_stats / cmd_stats (incl. overdue-definition fix) ───

    def _stats_db(self):
        """A single-account db with a controlled mix for stats assertions."""
        from datetime import timedelta
        db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-L1")])
        sod = self.remctl.start_of_day()
        yesterday = self.remctl.to_ts(sod - timedelta(days=1))
        today_midnight = self.remctl.to_ts(sod)
        tomorrow = self.remctl.to_ts(sod + timedelta(days=1))
        # overdue: due yesterday, timed
        db.execute("INSERT INTO ZREMCDREMINDER (Z_PK, ZTITLE, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
                   "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, "
                   "ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) "
                   "VALUES (1,'Overdue',0,0,0,0,?,?,0,1,'CK-1',1,0)", (yesterday, yesterday))
        # all-day due today → NOT overdue (display date == start of day)
        db.execute("INSERT INTO ZREMCDREMINDER (Z_PK, ZTITLE, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
                   "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, "
                   "ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) "
                   "VALUES (2,'AllDayToday',0,0,0,0,?,?,1,1,'CK-2',1,0)", (today_midnight, today_midnight))
        # flagged + future
        db.execute("INSERT INTO ZREMCDREMINDER (Z_PK, ZTITLE, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
                   "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, "
                   "ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) "
                   "VALUES (3,'Flagged',0,1,0,0,?,?,0,1,'CK-3',1,0)", (tomorrow, tomorrow))
        # completed
        db.execute("INSERT INTO ZREMCDREMINDER (Z_PK, ZTITLE, ZCOMPLETED, ZFLAGGED, ZPRIORITY, "
                   "ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZLIST, "
                   "ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) "
                   "VALUES (4,'Done',1,0,0,0,NULL,NULL,0,1,'CK-4',1,0)")
        return db

    def test_gather_stats_counts(self):
        """_gather_stats returns correct totals/active/completed/flagged."""
        db = self._stats_db()
        try:
            with mock.patch.object(self.remctl, "_REMINDER_COLUMN_CACHE", {}):
                s = self.remctl._gather_stats(db)
        finally:
            db.close()
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["active"], 3)
        self.assertEqual(s["completed"], 1)
        self.assertEqual(s["flagged"], 1)

    def test_gather_stats_overdue_matches_q_overdue(self):
        """stats overdue count equals what q_overdue lists (canonical definition)."""
        db = self._stats_db()
        try:
            with mock.patch.object(self.remctl, "_REMINDER_COLUMN_CACHE", {}):
                s = self.remctl._gather_stats(db)
                overdue_rows = self.remctl.q_overdue(db)
        finally:
            db.close()
        # Only the due-yesterday reminder is overdue; all-day-today is NOT.
        self.assertEqual(s["overdue"], 1)
        self.assertEqual(s["overdue"], len(overdue_rows))

    def test_gather_stats_all_day_today_not_overdue(self):
        """An all-day reminder due today must not be counted overdue."""
        db = self._stats_db()
        try:
            with mock.patch.object(self.remctl, "_REMINDER_COLUMN_CACHE", {}):
                overdue_rows = self.remctl.q_overdue(db)
        finally:
            db.close()
        titles = {r["ZTITLE"] for r in overdue_rows}
        self.assertNotIn("AllDayToday", titles)
        self.assertIn("Overdue", titles)

    def test_cmd_stats_multi_account_totals(self):
        """cmd_stats --all-accounts JSON has per-account stats and a summed total."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 1, "ZTITLE": "A", "ZLIST": 1}])
        ex_db = self._make_account_db("Exchange", "Exchange", [(1, "Tasks", "CK-2")],
                                      reminders=[{"Z_PK": 1, "ZTITLE": "B", "ZLIST": 1},
                                                 {"Z_PK": 2, "ZTITLE": "C", "ZLIST": 1}])
        try:
            with (
                self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                mock.patch.object(self.remctl, "_REMINDER_COLUMN_CACHE", {}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_stats(SimpleNamespace(json=True, all_accounts=True, account=None))
            payload = json.loads(stdout.getvalue())
        finally:
            ic_db.close()
            ex_db.close()
        self.assertEqual(payload["total"]["total"], 3)
        self.assertIn("iCloud", payload["accounts"])
        self.assertIn("Exchange", payload["accounts"])
        self.assertEqual(payload["accounts"]["Exchange"]["total"], 2)

    # ── Group 15: multi-account tags (merge + dedup) ─────────────────────────

    def test_cmd_tags_all_accounts_merges_and_dedups(self):
        """Tags from all accounts are merged, deduplicated, and sorted."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "R", "CK-1")])
        ex_db = self._make_account_db("Exchange", "Exchange", [(1, "T", "CK-2")])
        ic_db.execute("INSERT INTO ZREMCDHASHTAGLABEL (Z_PK, ZNAME) VALUES (1,'work'),(2,'home')")
        ex_db.execute("INSERT INTO ZREMCDHASHTAGLABEL (Z_PK, ZNAME) VALUES (1,'work'),(2,'urgent')")
        try:
            with (
                self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_tags(SimpleNamespace(json=True, all_accounts=True, account=None))
            payload = json.loads(stdout.getvalue())
        finally:
            ic_db.close()
            ex_db.close()
        names = [t["name"] for t in payload]
        self.assertEqual(names, ["home", "urgent", "work"])  # deduped + sorted

    # ── Group 16: multi-account sections (label consistency) ─────────────────

    def _sections_account_db(self, account_name, account_type, list_name, sections):
        db = self._make_account_db(account_name, account_type, [(1, list_name, "CK-L1")])
        for i, sec in enumerate(sections, start=1):
            db.execute("INSERT INTO ZREMCDBASESECTION (Z_PK, ZDISPLAYNAME, ZLIST, ZCKIDENTIFIER, ZMARKEDFORDELETION) "
                       "VALUES (?,?,1,?,0)", (i, sec, f"SEC-{i}"))
        return db

    def test_cmd_sections_single_account_no_label(self):
        """sections --account X shows no redundant account label (single scope)."""
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ex_db = self._sections_account_db("Exchange", "Exchange", "Tasks", ["Phase 1"])
        try:
            with (
                self._multi_account_patch([(ex, ex_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_sections(SimpleNamespace(json=False, all_accounts=False, account="Exchange"))
            out = self.remctl._strip_ansi(stdout.getvalue())
        finally:
            ex_db.close()
        self.assertIn("Tasks:", out)
        self.assertNotIn("(Exchange)", out)

    def test_cmd_sections_multi_account_shows_label(self):
        """sections --all-accounts labels each list with its account."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ic_db = self._sections_account_db("iCloud", "iCloud", "Groceries", ["Produce"])
        ex_db = self._sections_account_db("Exchange", "Exchange", "Tasks", ["Phase 1"])
        try:
            with (
                self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_sections(SimpleNamespace(json=False, all_accounts=True, account=None))
            out = self.remctl._strip_ansi(stdout.getvalue())
        finally:
            ic_db.close()
            ex_db.close()
        self.assertIn("(iCloud)", out)
        self.assertIn("(Exchange)", out)

    # ── Group 17: config storeDir / dbPath + _apply_config_path_overrides ────

    def test_cmd_config_set_and_clear_store_dir(self):
        store = {}
        with (
            mock.patch.object(self.remctl, "load_config", side_effect=lambda: dict(store)),
            mock.patch.object(self.remctl, "save_config", side_effect=lambda d: (store.clear(), store.update(d))),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_config(SimpleNamespace(key="storeDir", value="/tmp/x", json=False))
            self.assertEqual(store.get("storeDir"), "/tmp/x")
            # clearing with empty string removes the key
            self.remctl.cmd_config(SimpleNamespace(key="storeDir", value="", json=False))
            self.assertNotIn("storeDir", store)

    def test_cmd_config_unknown_key_warns(self):
        store = {}
        with (
            mock.patch.object(self.remctl, "load_config", side_effect=lambda: dict(store)),
            mock.patch.object(self.remctl, "save_config", side_effect=lambda d: (store.clear(), store.update(d))),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.remctl.cmd_config(SimpleNamespace(key="bogusKey", value="x", json=False))
        self.assertIn("not a recognised config key", stderr.getvalue())

    def test_cmd_config_empty_lists_supported_keys(self):
        with (
            mock.patch.object(self.remctl, "load_config", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_config(SimpleNamespace(key=None, value=None, json=False))
        out = stdout.getvalue()
        self.assertIn("accountScope", out)
        self.assertIn("storeDir", out)
        self.assertIn("dbPath", out)

    def test_apply_config_path_overrides_sets_store_dir(self):
        """storeDir from config sets STORE_DIR when REMCTL_STORE_DIR is unset."""
        import tempfile, json as _json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            _json.dump({"storeDir": "/tmp/custom-stores"}, f)
            cfg_path = f.name
        try:
            self.remctl.CONFIG_FILE = Path(cfg_path)
            self.remctl._apply_config_path_overrides()
            self.assertEqual(str(self.remctl.STORE_DIR), "/tmp/custom-stores")
        finally:
            os.unlink(cfg_path)

    def test_apply_config_path_overrides_sets_db_path(self):
        import tempfile, json as _json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            _json.dump({"dbPath": "/tmp/pinned.sqlite"}, f)
            cfg_path = f.name
        try:
            self.remctl.CONFIG_FILE = Path(cfg_path)
            self.remctl._apply_config_path_overrides()
            self.assertEqual(str(self.remctl.DB_OVERRIDE), "/tmp/pinned.sqlite")
        finally:
            os.unlink(cfg_path)

    def test_apply_config_path_overrides_env_wins_over_config(self):
        """REMCTL_STORE_DIR env var takes precedence over config storeDir."""
        import tempfile, json as _json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            _json.dump({"storeDir": "/tmp/from-config"}, f)
            cfg_path = f.name
        try:
            self.remctl.CONFIG_FILE = Path(cfg_path)
            with mock.patch.dict(os.environ, {"REMCTL_STORE_DIR": "/tmp/from-env"}):
                self.remctl.STORE_DIR = Path("/tmp/from-env")  # as resolve_store_dir would have set
                self.remctl._apply_config_path_overrides()
                # config must not override the env-derived value
                self.assertEqual(str(self.remctl.STORE_DIR), "/tmp/from-env")
        finally:
            os.unlink(cfg_path)

    # ── Group 18: first_command_token (global flag positioning) ──────────────

    def test_first_command_token_account_before_command(self):
        """--account NAME before the subcommand: NAME is not mistaken for the command."""
        self.assertEqual(
            self.remctl.first_command_token(["--account", "Work Exchange", "lists"]),
            "lists",
        )

    def test_first_command_token_account_equals_form(self):
        self.assertEqual(
            self.remctl.first_command_token(["--account=Exchange", "today"]),
            "today",
        )

    def test_first_command_token_all_accounts_before_command(self):
        self.assertEqual(
            self.remctl.first_command_token(["--all-accounts", "stats"]),
            "stats",
        )

    def test_first_command_token_format_value_still_skipped(self):
        """Regression: --format value is not mistaken for the command."""
        self.assertEqual(
            self.remctl.first_command_token(["--format", "table", "lists"]),
            "lists",
        )

    def test_first_command_token_combined_global_flags(self):
        self.assertEqual(
            self.remctl.first_command_token(["--no-color", "--account", "iCloud", "show", "Work"]),
            "show",
        )

    # ── Group 19: bridge_call returns a list (list_calendars) ────────────────

    def test_bridge_call_returns_list_payload(self):
        """bridge_call returns list payloads (e.g. list_calendars), not just dicts."""
        fake_result = {"returncode": 0, "stdout": "[]", "stderr": "",
                       "payload": [{"title": "Tasks", "calendarIdentifier": "CAL-1"}]}
        with mock.patch.object(self.remctl, "bridge_call_result", return_value=fake_result):
            result = self.remctl.bridge_call({"action": "list_calendars"})
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["calendarIdentifier"], "CAL-1")

    def test_bridge_call_returns_dict_payload(self):
        """bridge_call still returns dict payloads for normal actions."""
        fake_result = {"returncode": 0, "stdout": "{}", "stderr": "",
                       "payload": {"status": "created", "id": "X"}}
        with mock.patch.object(self.remctl, "bridge_call_result", return_value=fake_result):
            result = self.remctl.bridge_call({"action": "create"})
        self.assertEqual(result["status"], "created")

    # ── Group 20: serialization account fields & table column ────────────────

    def test_list_to_dict_account_kwarg(self):
        row = {"Z_PK": 1, "ZNAME": "Work", "ZCKIDENTIFIER": "CK-1"}
        acct = self.remctl.Account("/tmp/x", "Exchange", "Exchange")
        with_acct = self.remctl.list_to_dict(row, account=acct)
        without = self.remctl.list_to_dict(row)
        self.assertEqual(with_acct["account"], "Exchange")
        self.assertEqual(with_acct["accountType"], "Exchange")
        self.assertNotIn("account", without)

    def test_list_ref_payload_account_kwarg(self):
        row = {"Z_PK": 2, "ZNAME": "Tasks", "ZCKIDENTIFIER": "CK-2"}
        acct = self.remctl.Account("/tmp/x", "iCloud", "iCloud")
        with_acct = self.remctl.list_ref_payload(row, account=acct)
        without = self.remctl.list_ref_payload(row)
        self.assertEqual(with_acct["account"], "iCloud")
        self.assertNotIn("account", without)

    def test_fmt_table_account_column_present_when_tagged(self):
        rows = [{"id": 1, "title": "A", "list": "", "due": "", "pri": "", "account": "iCloud"}]
        out = self.remctl.fmt_table(rows)
        self.assertIn("Account", out)
        self.assertIn("iCloud", out)

    def test_fmt_table_account_column_absent_when_untagged(self):
        rows = [{"id": 1, "title": "A", "list": "", "due": "", "pri": ""}]
        out = self.remctl.fmt_table(rows)
        self.assertNotIn("Account", out)

    def test_tag_account_helper(self):
        acct = self.remctl.Account("/tmp/x", "Exchange", "Exchange")
        multi = self.remctl._tag_account({"id": 1}, acct, True)
        single = self.remctl._tag_account({"id": 2}, acct, False)
        self.assertEqual(multi["account"], "Exchange")
        self.assertEqual(multi["accountType"], "Exchange")
        self.assertNotIn("account", single)

    # ── Group 21: discovery edge cases ───────────────────────────────────────

    def test_store_account_info_fallback_name_for_unsynced_account(self):
        """A store whose account row has no ZNAME yet derives a fallback name."""
        import tempfile
        path = Path(tempfile.mkdtemp()) / "Data-ABCD1234-5678.sqlite"
        db = sqlite3.connect(str(path))
        db.execute("CREATE TABLE Z_PRIMARYKEY (Z_NAME TEXT, Z_ENT INTEGER, Z_MAX INTEGER)")
        db.execute("INSERT INTO Z_PRIMARYKEY VALUES ('REMCDAccount', 14, 1)")
        db.execute("CREATE TABLE ZREMCDOBJECT (Z_PK INTEGER, Z_ENT INTEGER, ZNAME TEXT)")
        db.execute("INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZNAME) VALUES (1, 14, NULL)")
        db.execute("CREATE TABLE ZREMCDREPLICAMANAGER (Z_PK INTEGER, Z_ENT INTEGER, ZIDENTIFIER TEXT)")
        db.execute("INSERT INTO ZREMCDREPLICAMANAGER VALUES (1, 15, 'EE402AA3-1111/com.apple.exchangesync.exchangesyncd')")
        db.commit()
        db.close()
        try:
            info = self.remctl._store_account_info(path)
        finally:
            os.unlink(path)
            os.rmdir(path.parent)
        self.assertIsNotNone(info)
        name, acct_type = info
        self.assertEqual(acct_type, "Exchange")
        self.assertTrue(name)  # a non-empty fallback name was derived

    def test_discover_accounts_includes_empty_store(self):
        """A real account with zero reminders is still discovered (not filtered out)."""
        import tempfile
        store_dir = Path(tempfile.mkdtemp())
        # one store with a valid account name but no reminders
        path = store_dir / "Data-EMPTY1234-0000.sqlite"
        db = sqlite3.connect(str(path))
        db.execute("CREATE TABLE Z_PRIMARYKEY (Z_NAME TEXT, Z_ENT INTEGER, Z_MAX INTEGER)")
        db.execute("INSERT INTO Z_PRIMARYKEY VALUES ('REMCDAccount', 14, 1)")
        db.execute("CREATE TABLE ZREMCDOBJECT (Z_PK INTEGER, Z_ENT INTEGER, ZNAME TEXT)")
        db.execute("INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZNAME) VALUES (1, 14, 'NewAccount')")
        db.execute("CREATE TABLE ZREMCDREPLICAMANAGER (Z_PK INTEGER, Z_ENT INTEGER, ZIDENTIFIER TEXT)")
        db.execute("INSERT INTO ZREMCDREPLICAMANAGER VALUES (1, 15, 'uuid/com.apple.exchangesync.exchangesyncd')")
        db.commit()
        db.close()
        try:
            self.remctl._ACCOUNT_CACHE = None
            with (
                mock.patch.object(self.remctl, "STORE_DIR", store_dir),
                mock.patch.object(self.remctl, "DB_OVERRIDE", None),
                mock.patch.object(self.remctl, "reminders_store_access_error", return_value=None),
            ):
                accounts = self.remctl.discover_accounts(force_refresh=True)
        finally:
            os.unlink(path)
            os.rmdir(store_dir)
            self.remctl._ACCOUNT_CACHE = None
        names = [a.name for a in accounts]
        self.assertIn("NewAccount", names)

    # ── Group 22: cmd_show / cmd_info cross-account via config ───────────────

    def test_cmd_show_ambiguous_list_via_config_all(self):
        """With config=all, show <name> present in two accounts reports ambiguity."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Tasks", "CK-1")])
        ex_db = self._make_account_db("Exchange", "Exchange", [(1, "Tasks", "CK-2")])
        try:
            with (
                self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                mock.patch.object(self.remctl, "load_config", return_value={"accountScope": "all"}),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit),
            ):
                self.remctl.cmd_show(SimpleNamespace(list="Tasks", list_id=None, account=None,
                                                     completed=False, json=False, format=None,
                                                     verbose=False))
            self.assertIn("multiple lists match", stderr.getvalue())
        finally:
            ic_db.close()
            ex_db.close()

    def test_cmd_info_with_account_routes_through_resolver(self):
        """info --account Exchange resolves via the --account-aware resolver."""
        fake_db = mock.Mock()
        fake_row = {"Z_PK": 7, "ZTITLE": "ExInfo", "ZLIST": None,
                    "ZCKIDENTIFIER": "X", "list_name": "Tasks"}
        captured = {}

        def fake_resolve(a):
            captured["account"] = getattr(a, "account", None)
            return fake_db, fake_row

        with (
            mock.patch.object(self.remctl, "_resolve_reminder_for_write", side_effect=fake_resolve),
            mock.patch.object(self.remctl, "q_reminders", return_value=[]),
            mock.patch.object(self.remctl, "q_attachments", return_value=[]),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
            mock.patch.object(self.remctl, "q_hashtags", return_value=[]),
            mock.patch.object(self.remctl, "q_rich_link", return_value=None),
            mock.patch.object(self.remctl, "to_dict", return_value={"id": 7, "title": "ExInfo"}),
            mock.patch.object(self.remctl, "hydrate_reminder_detail", side_effect=lambda db, d, i: d),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_info(SimpleNamespace(id=7, account="Exchange", json=True))
        self.assertEqual(captured["account"], "Exchange")
        self.assertEqual(json.loads(stdout.getvalue())["title"], "ExInfo")

    # ── Group 23: cmd_add account-aware write targeting ──────────────────────

    def _add_args(self, **overrides):
        base = dict(
            title="X", list=None, list_id=None, notes=None, due=None, priority=None,
            flag=False, tags=None, url=None, recurrence=None, alarm=None,
            private=False, private_metadata=False, section=None, new_section=None,
            subtask=None, image=None, grocery=False, json=False, account=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_cmd_add_account_with_list_passes_calendar_identifier(self):
        """add -l Tasks --account Exchange resolves a stable calendarIdentifier for create."""
        icloud = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        exchange = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        create_payloads = []

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Tasks", "calendarIdentifier": "CAL-EX", "sourceTitle": "Exchange"},
                        {"title": "Tasks", "calendarIdentifier": "CAL-IC", "sourceTitle": "iCloud"}]
            if data["action"] == "create":
                create_payloads.append(data)
                return {"status": "created", "id": "NEW-1"}
            return None

        fake_db = mock.Mock()
        fake_db.row_factory = None
        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[icloud, exchange]),
            mock.patch.object(sqlite3, "connect", side_effect=lambda *a, **k: fake_db),
            mock.patch.object(self.remctl, "resolve_list_or_die",
                              return_value={"id": 1, "title": "Tasks", "requested": "Tasks", "method": "exact"}),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            mock.patch.object(self.remctl, "private_metadata_enabled", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_add(self._add_args(title="T", list="Tasks", account="Exchange"))
        self.assertEqual(len(create_payloads), 1)
        self.assertEqual(create_payloads[0].get("calendarIdentifier"), "CAL-EX")
        self.assertEqual(create_payloads[0].get("account"), "Exchange")
        self.assertNotIn("list", create_payloads[0])  # calendarIdentifier supersedes title

    def test_cmd_add_account_no_list_picks_first_calendar_in_account(self):
        """add --account Exchange with no list targets the first calendar in that account."""
        exchange = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        create_payloads = []

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Other", "calendarIdentifier": "CAL-IC", "sourceTitle": "iCloud"},
                        {"title": "Tasks", "calendarIdentifier": "CAL-EX", "sourceTitle": "Exchange"}]
            if data["action"] == "create":
                create_payloads.append(data)
                return {"status": "created", "id": "NEW-2"}
            return None

        with (
            mock.patch.object(self.remctl, "discover_accounts", return_value=[exchange]),
            mock.patch.object(sqlite3, "connect", side_effect=lambda *a, **k: mock.Mock()),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
            mock.patch.object(self.remctl, "private_metadata_enabled", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.remctl.cmd_add(self._add_args(title="T", account="Exchange"))
        self.assertEqual(len(create_payloads), 1)
        self.assertEqual(create_payloads[0].get("calendarIdentifier"), "CAL-EX")

    def test_cmd_add_ambiguous_list_without_account_exits(self):
        """add -l Tasks with no --account when Tasks exists in two accounts errors."""
        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Tasks", "calendarIdentifier": "CAL-A", "sourceTitle": "AcctA"},
                        {"title": "Tasks", "calendarIdentifier": "CAL-B", "sourceTitle": "AcctB"}]
            return {"status": "created", "id": "X"}

        with (
            mock.patch.object(self.remctl, "open_db", return_value=mock.Mock()),
            mock.patch.object(self.remctl, "resolve_list_or_die",
                              return_value={"id": 1, "title": "Tasks", "requested": "Tasks", "method": "exact"}),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
            mock.patch.object(self.remctl, "private_metadata_enabled", return_value=False),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_add(self._add_args(title="T", list="Tasks", account=None))
        self.assertIn("multiple accounts", stderr.getvalue())

    def test_cmd_add_exchange_id_fallback_by_title(self):
        """For Exchange (no ZCKIDENTIFIER), the numeric id is recovered by title after create."""
        exchange = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ex_db = self._make_account_db(
            "Exchange", "Exchange", [(1, "Tasks", "CK-L1")],
            reminders=[{"Z_PK": 42, "ZTITLE": "NewTask", "ZLIST": 1, "ZCKIDENTIFIER": None}],
        )

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Tasks", "calendarIdentifier": "CAL-EX", "sourceTitle": "Exchange"}]
            if data["action"] == "create":
                return {"status": "created", "id": "EK-NEW"}
            return None

        try:
            with (
                mock.patch.object(self.remctl, "discover_accounts", return_value=[exchange]),
                mock.patch.object(sqlite3, "connect",
                                  side_effect=self._connect_by_path({"/tmp/ex.sqlite": ex_db})),
                mock.patch.object(self.remctl, "resolve_list_or_die",
                                  return_value={"id": 1, "title": "Tasks", "requested": "Tasks", "method": "exact"}),
                mock.patch.object(self.remctl, "bridge_available", return_value=True),
                mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
                mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
                mock.patch.object(self.remctl, "private_metadata_enabled", return_value=False),
                mock.patch.object(self.remctl, "_REMINDER_COLUMN_CACHE", {}),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_add(self._add_args(title="NewTask", list="Tasks", account="Exchange", json=True))
            payload = json.loads(stdout.getvalue())
        finally:
            ex_db.close()
        self.assertEqual(payload.get("numericId"), 42)

    # ── Group 24: multi-account human output shows account group headers ─────

    def test_cmd_today_all_accounts_shows_account_group_headers(self):
        """today --all-accounts human output groups items under bold account headers."""
        from datetime import datetime
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        due_ts = self.remctl.to_ts(datetime.now())
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 1, "ZTITLE": "iThing", "ZLIST": 1, "ZDUEDATE": due_ts}])
        ex_db = self._make_account_db("Exchange", "Exchange", [(1, "Tasks", "CK-2")],
                                      reminders=[{"Z_PK": 1, "ZTITLE": "xThing", "ZLIST": 1, "ZDUEDATE": due_ts}])
        try:
            with (
                self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_today(SimpleNamespace(json=False, all_accounts=True, account=None,
                                                      no_overdue=False, format=None))
            out = self.remctl._strip_ansi(stdout.getvalue())
        finally:
            ic_db.close()
            ex_db.close()
        # Both account headers and both items (same Z_PK=1 in each, no collision corruption)
        self.assertIn("iCloud", out)
        self.assertIn("Exchange", out)
        self.assertIn("iThing", out)
        self.assertIn("xThing", out)

    # ── Group 25: priority bucketing (Exchange/CalDAV full 1-9 range) ────────

    def test_priority_bucket_ranges(self):
        """priority_bucket maps the full iCalendar 1-9 range to canonical buckets."""
        b = self.remctl.priority_bucket
        self.assertEqual(b(0), "none")
        self.assertEqual(b(None), "none")
        for v in (1, 2, 3, 4):
            self.assertEqual(b(v), "high", f"priority {v} should be high")
        self.assertEqual(b(5), "medium")
        for v in (6, 7, 8, 9):
            self.assertEqual(b(v), "low", f"priority {v} should be low")

    def test_pri_marker_apple_native_values_unchanged(self):
        """Apple's native 1/5/9 still map to !!!/!!/! (backward compatible)."""
        self.assertEqual(self.remctl.PRI[0], "")
        self.assertEqual(self.remctl.PRI[1], "!!!")
        self.assertEqual(self.remctl.PRI[5], "!!")
        self.assertEqual(self.remctl.PRI[9], "!")

    def test_pri_marker_exchange_intermediate_values(self):
        """Exchange/CalDAV intermediate values resolve via range buckets."""
        # high range 1-4
        for v in (2, 3, 4):
            self.assertEqual(self.remctl.PRI[v], "!!!", f"priority {v}")
        # low range 6-8
        for v in (6, 7, 8):
            self.assertEqual(self.remctl.PRI[v], "!", f"priority {v}")

    def test_pri_name_map_ranges(self):
        """PRI_NAME buckets values for the JSON 'priority' field."""
        self.assertEqual(self.remctl.PRI_NAME[3], "high")
        self.assertEqual(self.remctl.PRI_NAME[5], "medium")
        self.assertEqual(self.remctl.PRI_NAME[7], "low")
        self.assertEqual(self.remctl.PRI_NAME[0], "none")

    def test_pri_get_with_default_buckets(self):
        """.get() (used by serialize_reminder) buckets too, honoring its default for none."""
        self.assertEqual(self.remctl.PRI_NAME.get(3, "none"), "high")
        self.assertEqual(self.remctl.PRI.get(3, ""), "!!!")
        self.assertEqual(self.remctl.PRI_NAME.get(0, "none"), "none")

    def test_color_priority_marks_exchange_high(self):
        """_color_priority returns a non-empty marker for an Exchange high value (3)."""
        prev = self.remctl.C.enabled
        self.remctl.C.enabled = False  # no ANSI, so we compare plain text
        try:
            self.assertEqual(self.remctl._color_priority(3), "!!!")
            self.assertEqual(self.remctl._color_priority(5), "!!")
            self.assertEqual(self.remctl._color_priority(7), "!")
            self.assertEqual(self.remctl._color_priority(0), "")
        finally:
            self.remctl.C.enabled = prev

    def test_serialize_reminder_exchange_priority_is_high(self):
        """A reminder stored with priority 3 (Exchange/To Do starred) serializes as 'high'."""
        row = {
            "Z_PK": 7, "ZTITLE": "Starred Task", "ZNOTES": None, "ZCOMPLETED": 0,
            "ZFLAGGED": 0, "ZPRIORITY": 3, "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None, "ZDUEDATE": None, "ZDISPLAYDATEDATE": None,
            "ZALLDAY": 0, "ZCOMPLETIONDATE": None, "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None, "ZLIST": 1, "ZICSURL": None, "ZCKIDENTIFIER": None,
            "list_name": "Tasks",
            "recurrence_frequency": None, "recurrence_interval": None,
            "recurrence_count": None, "recurrence_end_date": None,
            "recurrence_days_of_week": None, "recurrence_days_of_month": None,
            "recurrence_months_of_year": None, "recurrence_days_of_year": None,
            "recurrence_weeks_of_year": None, "recurrence_set_positions": None,
        }
        payload = self.remctl.to_dict(row, db=None)
        self.assertEqual(payload["priority"], "high")
        # And not flagged — confirms starred maps to priority, not the flag
        self.assertFalse(payload["flagged"])

    def test_fmt_shows_exchange_high_priority_marker(self):
        """fmt() renders the !!! marker for an Exchange high-priority reminder."""
        row = {
            "Z_PK": 7, "ZTITLE": "Starred", "ZNOTES": None, "ZCOMPLETED": 0,
            "ZFLAGGED": 0, "ZPRIORITY": 3, "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
            "ZDUEDATEDELTAALERTSDATA": None, "ZDUEDATE": None, "ZDISPLAYDATEDATE": None,
            "ZALLDAY": 0, "ZCOMPLETIONDATE": None, "ZCREATIONDATE": None,
            "ZPARENTREMINDER": None, "ZLIST": 1, "ZICSURL": None, "ZCKIDENTIFIER": None,
            "list_name": "Tasks",
        }
        prev = self.remctl.C.enabled
        self.remctl.C.enabled = False
        try:
            line = self.remctl.fmt(row, db=None)
        finally:
            self.remctl.C.enabled = prev
        self.assertIn("!!!", line)

    # ── Group 26: flag/unflag on non-iCloud accounts + _ek_identifier case ───

    def test_cmd_flag_exchange_reminder_refuses_with_honest_error(self):
        """flag on a reminder with no ZCKIDENTIFIER (Exchange) errors honestly, no fake success."""
        fake_db = mock.Mock()
        fake_row = {"ZCKIDENTIFIER": None, "ZTITLE": "Test_CAT1",
                    "list_name": "Tasks", "ZLIST": 1, "ZDUEDATE": None}
        with (
            mock.patch.object(self.remctl, "_resolve_reminder_for_write",
                              return_value=(fake_db, fake_row)),
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_mock,
            mock.patch.object(self.remctl, "bridge_call") as bridge_call,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_flag(SimpleNamespace(id=2, json=False, account="Work Exchange"))
        err = stderr.getvalue()
        self.assertIn("no flag attribute", err)
        self.assertIn("priority", err)
        # Must NOT have attempted AppleScript or the misleading bridge proxy
        osa_mock.assert_not_called()
        bridge_call.assert_not_called()

    def test_cmd_unflag_exchange_reminder_refuses_with_honest_error(self):
        """unflag on a no-ZCKIDENTIFIER reminder errors honestly."""
        fake_db = mock.Mock()
        fake_row = {"ZCKIDENTIFIER": None, "ZTITLE": "Test_CAT1",
                    "list_name": "Tasks", "ZLIST": 1, "ZDUEDATE": None}
        with (
            mock.patch.object(self.remctl, "_resolve_reminder_for_write",
                              return_value=(fake_db, fake_row)),
            mock.patch.object(self.remctl, "osa_by_id_try") as osa_mock,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.remctl.cmd_unflag(SimpleNamespace(id=2, json=False, account="Exchange"))
        self.assertIn("no flag attribute", stderr.getvalue())
        osa_mock.assert_not_called()

    def test_cmd_flag_icloud_reminder_uses_applescript(self):
        """flag on an iCloud reminder (ZCKIDENTIFIER present) sets the real flag via AppleScript."""
        fake_db = mock.Mock()
        fake_row = {"ZCKIDENTIFIER": "CK-IC-1", "ZTITLE": "iThing",
                    "list_name": "Reminders", "ZLIST": 1, "ZDUEDATE": None}
        with (
            mock.patch.object(self.remctl, "_resolve_reminder_for_write",
                              return_value=(fake_db, fake_row)),
            mock.patch.object(self.remctl, "osa_by_id_try", return_value=True) as osa_mock,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.remctl.cmd_flag(SimpleNamespace(id=1, json=False, account=None))
        osa_mock.assert_called_once()
        self.assertIn("set flagged of r to true", osa_mock.call_args.args[2])
        self.assertIn("Flagged", stdout.getvalue())

    def test_ek_identifier_account_match_is_case_insensitive(self):
        """_ek_identifier matches the account hint case-insensitively."""
        r = {"ZCKIDENTIFIER": None, "list_name": "Tasks", "ZTITLE": "T"}

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Tasks", "calendarIdentifier": "CAL-X",
                         "sourceTitle": "Work Exchange"}]
            if data["action"] == "find_reminder":
                self.assertEqual(data["calendarIdentifier"], "CAL-X")
                return {"calendarItemIdentifier": "EK-1"}
            return None

        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
        ):
            # lowercase account hint must still match the canonical-cased sourceTitle
            result = self.remctl._ek_identifier(r, account_name="work exchange")
        self.assertEqual(result, "EK-1")

    def test_ek_identifier_refuses_wrong_account_when_name_collides(self):
        """When the account hint matches no calendar of that name, return None (no wrong-account guess)."""
        r = {"ZCKIDENTIFIER": None, "list_name": "Tasks", "ZTITLE": "T"}

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Tasks", "calendarIdentifier": "CAL-A", "sourceTitle": "AcctA"},
                        {"title": "Tasks", "calendarIdentifier": "CAL-B", "sourceTitle": "AcctB"}]
            # find_reminder must never be called with a wrong calendar
            raise AssertionError("find_reminder should not be called when account does not match")

        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
        ):
            result = self.remctl._ek_identifier(r, account_name="AcctC")  # not present
        self.assertIsNone(result)

    def test_ek_identifier_ambiguous_without_account_returns_none(self):
        """Same list name in two accounts and no account hint → None (don't guess)."""
        r = {"ZCKIDENTIFIER": None, "list_name": "Tasks", "ZTITLE": "T"}

        def fake_bridge_call(data):
            if data["action"] == "list_calendars":
                return [{"title": "Tasks", "calendarIdentifier": "CAL-A", "sourceTitle": "AcctA"},
                        {"title": "Tasks", "calendarIdentifier": "CAL-B", "sourceTitle": "AcctB"}]
            raise AssertionError("find_reminder should not be called when ambiguous")

        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", side_effect=fake_bridge_call),
        ):
            result = self.remctl._ek_identifier(r, account_name=None)
        self.assertIsNone(result)

    # ── Group 27: multi-account --format table preserves rich rows ───────────

    def test_gather_table_rows_multi_has_rich_fields_and_account(self):
        """Multi-account table rows keep id/due/repeat formatting AND add an account column."""
        from datetime import datetime
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        due_ts = self.remctl.to_ts(datetime.now())
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 5, "ZTITLE": "iTask", "ZLIST": 1, "ZDUEDATE": due_ts}])
        ex_db = self._make_account_db("Exchange", "Exchange", [(1, "Tasks", "CK-2")],
                                      reminders=[{"Z_PK": 9, "ZTITLE": "xTask", "ZLIST": 1, "ZDUEDATE": due_ts}])
        try:
            with self._multi_account_patch([(ic, ic_db), (ex, ex_db)]):
                rows = self.remctl.gather_table_rows(
                    [ic, ex], lambda db: self.remctl.q_due_today(db, include_overdue=True)
                )
        finally:
            ic_db.close()
            ex_db.close()
        self.assertEqual(len(rows), 2)
        for row in rows:
            # rich table shape: id carries the '#', due is populated, account present
            self.assertIn("#", self.remctl._strip_ansi(str(row["id"])))
            self.assertIn("due", row)
            self.assertIn(row["account"], {"iCloud", "Exchange"})

    def test_gather_table_rows_single_account_has_no_account_key(self):
        """Single-account table rows must not carry an account key (byte-compat)."""
        from datetime import datetime
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        due_ts = self.remctl.to_ts(datetime.now())
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 5, "ZTITLE": "iTask", "ZLIST": 1, "ZDUEDATE": due_ts}])
        try:
            with self._multi_account_patch([(ic, ic_db)]):
                rows = self.remctl.gather_table_rows(
                    [ic], lambda db: self.remctl.q_due_today(db, include_overdue=True)
                )
        finally:
            ic_db.close()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("account", rows[0])

    def test_cmd_today_all_accounts_table_populates_due_column(self):
        """Regression: today --all-accounts --format table must populate Due (not blank)."""
        from datetime import datetime
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        due_ts = self.remctl.to_ts(datetime.now())
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 5, "ZTITLE": "iTask", "ZLIST": 1, "ZDUEDATE": due_ts}])
        ex_db = self._make_account_db("Exchange", "Exchange", [(1, "Tasks", "CK-2")],
                                      reminders=[{"Z_PK": 9, "ZTITLE": "xTask", "ZLIST": 1, "ZDUEDATE": due_ts}])
        try:
            with (
                self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.remctl.cmd_today(SimpleNamespace(json=False, all_accounts=True, account=None,
                                                      no_overdue=False, format="table"))
            out = self.remctl._strip_ansi(stdout.getvalue())
        finally:
            ic_db.close()
            ex_db.close()
        self.assertIn("Today", out)       # Due column populated, not blank
        self.assertIn("Account", out)     # account column present
        self.assertIn("#5", out)          # id keeps the # prefix
        self.assertIn("#9", out)

    # ── Group 28: single-account scope (--account) must not add labels ───────

    def test_cmd_lists_single_account_scope_no_labels(self):
        """lists --account X (one account in scope) emits no account field/column — byte-compat."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")])
        try:
            # JSON: no account field
            with (self._multi_account_patch([(ic, ic_db)]),
                  contextlib.redirect_stdout(io.StringIO()) as out):
                self.remctl.cmd_lists(SimpleNamespace(json=True, all_accounts=False, account="iCloud"))
            payload = json.loads(out.getvalue())
            self.assertTrue(payload)
            for item in payload:
                self.assertNotIn("account", item)
            # table: no Account column
            with (self._multi_account_patch([(ic, ic_db)]),
                  contextlib.redirect_stdout(io.StringIO()) as out2):
                self.remctl.cmd_lists(SimpleNamespace(json=False, all_accounts=False,
                                                      account="iCloud", format="table"))
            self.assertNotIn("Account", out2.getvalue())
        finally:
            ic_db.close()

    def test_cmd_stats_single_account_scope_flat_json_and_heading(self):
        """stats --account X (one account) uses the flat JSON + 'Reminders Stats' heading (byte-compat)."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 1, "ZTITLE": "x", "ZLIST": 1}])
        try:
            with (self._multi_account_patch([(ic, ic_db)]),
                  mock.patch.object(self.remctl, "_REMINDER_COLUMN_CACHE", {}),
                  contextlib.redirect_stdout(io.StringIO()) as out):
                self.remctl.cmd_stats(SimpleNamespace(json=True, all_accounts=False, account="iCloud"))
            payload = json.loads(out.getvalue())
            # flat structure (original), NOT nested {"total": {...}, "accounts": {...}}
            self.assertIsInstance(payload["total"], int)
            self.assertNotIn("accounts", payload)
            with (self._multi_account_patch([(ic, ic_db)]),
                  mock.patch.object(self.remctl, "_REMINDER_COLUMN_CACHE", {}),
                  contextlib.redirect_stdout(io.StringIO()) as out2):
                self.remctl.cmd_stats(SimpleNamespace(json=False, all_accounts=False, account="iCloud"))
            self.assertIn("Reminders Stats", out2.getvalue())
            self.assertNotIn("Stats: iCloud", out2.getvalue())
        finally:
            ic_db.close()

    def test_cmd_search_single_account_scope_no_indent(self):
        """search --account X (one account) does not indent results — matches original flat format."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ic_db = self._make_account_db("iCloud", "iCloud", [(1, "Reminders", "CK-1")],
                                      reminders=[{"Z_PK": 1, "ZTITLE": "findme", "ZLIST": 1}])
        try:
            with (self._multi_account_patch([(ic, ic_db)]),
                  contextlib.redirect_stdout(io.StringIO()) as out):
                self.remctl.cmd_search(SimpleNamespace(json=False, all_accounts=False, account="iCloud",
                                                       query="findme", completed=False, verbose=False,
                                                       format=None))
            lines = [l for l in self.remctl._strip_ansi(out.getvalue()).splitlines()
                     if "findme" in l]
            self.assertTrue(lines)
            for l in lines:
                self.assertFalse(l.startswith("  "), f"single-account search must not indent: {l!r}")
        finally:
            ic_db.close()


    # ── Group 29: global account-flag strictness for non-account commands ────

    def test_global_account_flag_rejected_for_non_account_command(self):
        """--account before a non-account command (doctor) errors strictly (rc 2)."""
        with (
            mock.patch.object(sys, "argv", ["remctl", "--account", "iCloud", "doctor"]),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("does not support", stderr.getvalue())

    def test_global_all_accounts_flag_rejected_for_non_account_command(self):
        """--all-accounts before a non-account command (accounts) errors strictly."""
        with (
            mock.patch.object(sys, "argv", ["remctl", "--all-accounts", "accounts"]),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("does not support", stderr.getvalue())

    def test_all_accounts_rejected_for_single_account_command(self):
        """A command that supports --account but does not aggregate (export) rejects
        --all-accounts rather than silently ignoring it (per-flag strictness)."""
        with (
            mock.patch.object(sys, "argv", ["remctl", "--all-accounts", "export"]),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("does not support --all-accounts", stderr.getvalue())

    # ── Group 30: smart-lists / templates multi-account aggregation ──────────

    def test_cmd_smart_lists_single_account_no_account_field(self):
        """smart-lists --account X (single) emits no account field — byte-compat with original."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ic_db = self._make_account_db("iCloud", "iCloud", [])
        try:
            with (self._multi_account_patch([(ic, ic_db)]),
                  contextlib.redirect_stdout(io.StringIO()) as out):
                self.remctl.cmd_smart_lists(SimpleNamespace(json=True, all_accounts=False, account="iCloud"))
            payload = json.loads(out.getvalue())
            for item in payload:
                self.assertNotIn("account", item)
        finally:
            ic_db.close()

    def test_cmd_smart_lists_all_accounts_runs_and_tags_when_multi(self):
        """smart-lists --all-accounts aggregates across accounts (valid JSON, account-tagged)."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ic_db = self._make_account_db("iCloud", "iCloud", [])
        ex_db = self._make_account_db("Exchange", "Exchange", [])
        try:
            with (self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                  contextlib.redirect_stdout(io.StringIO()) as out):
                self.remctl.cmd_smart_lists(SimpleNamespace(json=True, all_accounts=True, account=None))
            payload = json.loads(out.getvalue())  # must be valid JSON (list), no crash
            self.assertIsInstance(payload, list)
        finally:
            ic_db.close()
            ex_db.close()

    def test_cmd_templates_all_accounts_aggregates_and_tags(self):
        """templates --all-accounts aggregates per account and tags each with its account."""
        ic = self.remctl.Account("/tmp/ic.sqlite", "iCloud", "iCloud")
        ex = self.remctl.Account("/tmp/ex.sqlite", "Exchange", "Exchange")
        ic_db = self._make_account_db("iCloud", "iCloud", [])
        ex_db = self._make_account_db("Exchange", "Exchange", [])
        # q_templates is unchanged code needing extra schema; mock it to focus on
        # cmd_templates' multi-account aggregation wiring. Return one row for iCloud.
        def fake_q_templates(db):
            return [{"_ck": "T1"}] if db is ic_db else []
        try:
            with (
                self._multi_account_patch([(ic, ic_db), (ex, ex_db)]),
                mock.patch.object(self.remctl, "q_templates", side_effect=fake_q_templates),
                mock.patch.object(self.remctl, "template_to_dict",
                                  side_effect=lambda row: {"id": 1, "name": "Tmpl", "itemCount": 0}),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.remctl.cmd_templates(SimpleNamespace(json=True, all_accounts=True, account=None))
            payload = json.loads(out.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["account"], "iCloud")  # tagged with its account
        finally:
            ic_db.close()
            ex_db.close()


if __name__ == "__main__":
    unittest.main()
