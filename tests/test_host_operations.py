from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import load_module
from remctl_host_operations import ReadOperations


class HostOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_host_operations_test", "remctl")

    def _database(self, path: Path, names: list[str]) -> None:
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE ZREMCDBASELIST (
                Z_PK INTEGER PRIMARY KEY,
                Z_ENT INTEGER NOT NULL DEFAULT 3,
                ZNAME TEXT,
                ZCKIDENTIFIER TEXT,
                ZMARKEDFORDELETION INTEGER NOT NULL DEFAULT 0,
                ZISGROUP INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE ZREMCDREMINDER (
                Z_PK INTEGER PRIMARY KEY,
                ZMARKEDFORDELETION INTEGER NOT NULL DEFAULT 0,
                ZCOMPLETED INTEGER NOT NULL DEFAULT 0,
                ZLIST INTEGER
            );
            """
        )
        for index, name in enumerate(names, start=1):
            db.execute(
                "INSERT INTO ZREMCDBASELIST "
                "(Z_PK, ZNAME, ZCKIDENTIFIER) VALUES (?, ?, ?)",
                (index, name, f"LIST-{index}"),
            )
        db.commit()
        db.close()

    def test_resolve_list_reuses_existing_exact_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "Data-test.sqlite"
            self._database(path, ["Work", "Home"])

            def open_database():
                db = sqlite3.connect(path)
                db.row_factory = sqlite3.Row
                return db

            operations = ReadOperations(self.remctl)
            with mock.patch.object(self.remctl, "open_db", side_effect=open_database):
                result = operations.resolve_list(
                    {"operation": "resolve.list", "name": "Work"}
                )
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["title"], "Work")
        self.assertEqual(result["objectUUID"], "LIST-1")
        self.assertEqual(result["method"], "exact")

    def test_resolve_list_preserves_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "Data-test.sqlite"
            self._database(path, ["Work", "WORK"])

            def open_database():
                db = sqlite3.connect(path)
                db.row_factory = sqlite3.Row
                return db

            operations = ReadOperations(self.remctl)
            with mock.patch.object(self.remctl, "open_db", side_effect=open_database):
                result = operations.resolve_list(
                    {"operation": "resolve.list", "name": "work"}
                )
        self.assertEqual(result["error"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)

    def _reminder_row(self, pk, ckid, title="Buy milk", list_pk=1):
        """Return a sqlite3.Row-like object for use in resolve_reminder tests."""
        import sqlite3

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE t (Z_PK, ZCKIDENTIFIER, ZTITLE, ZLIST, "
            "ZCOMPLETED, ZMARKEDFORDELETION)"
        )
        db.execute(
            "INSERT INTO t VALUES (?, ?, ?, ?, 0, 0)",
            (pk, ckid, title, list_pk),
        )
        db.commit()
        return db.execute("SELECT * FROM t").fetchone()

    def test_resolve_reminder_returns_identity_payload(self):
        row = self._reminder_row(42, "CKID-42")
        operations = ReadOperations(self.remctl)
        with mock.patch.object(
            self.remctl,
            "q_reminder_by_identifier",
            return_value=row,
        ):
            with mock.patch.object(
                self.remctl, "open_db", return_value=mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None, close=lambda: None)
            ):
                result = operations.resolve_reminder(
                    {"operation": "resolve.reminder", "identifier": "CKID-42"}
                )
        self.assertEqual(result["id"], 42)
        self.assertEqual(result["Z_PK"], 42)
        self.assertEqual(result["identifier"], "CKID-42")
        self.assertEqual(result["title"], "Buy milk")
        self.assertEqual(result["listId"], 1)
        self.assertFalse(result["completed"])
        self.assertFalse(result["deleted"])

    def test_resolve_reminder_not_found_returns_none(self):
        operations = ReadOperations(self.remctl)
        with mock.patch.object(
            self.remctl,
            "q_reminder_by_identifier",
            return_value=None,
        ):
            with mock.patch.object(
                self.remctl, "open_db", return_value=mock.MagicMock(close=lambda: None)
            ):
                result = operations.resolve_reminder(
                    {"operation": "resolve.reminder", "identifier": "NO-SUCH-ID"}
                )
        self.assertIsNone(result)

    def test_resolve_reminder_closes_db_on_success(self):
        row = self._reminder_row(7, "CKID-7")
        mock_db = mock.MagicMock()
        operations = ReadOperations(self.remctl)
        with mock.patch.object(
            self.remctl, "q_reminder_by_identifier", return_value=row
        ):
            with mock.patch.object(self.remctl, "open_db", return_value=mock_db):
                operations.resolve_reminder(
                    {"operation": "resolve.reminder", "identifier": "CKID-7"}
                )
        mock_db.close.assert_called_once()

    def test_resolve_reminder_closes_db_on_error(self):
        mock_db = mock.MagicMock()
        operations = ReadOperations(self.remctl)
        with mock.patch.object(
            self.remctl,
            "q_reminder_by_identifier",
            side_effect=RuntimeError("boom"),
        ):
            with mock.patch.object(self.remctl, "open_db", return_value=mock_db):
                with self.assertRaises(RuntimeError):
                    operations.resolve_reminder(
                        {"operation": "resolve.reminder", "identifier": "CKID-X"}
                    )
        mock_db.close.assert_called_once()

    def test_handlers_includes_resolve_reminder(self):
        operations = ReadOperations(self.remctl)
        handlers = operations.handlers()
        self.assertIn("resolve.reminder", handlers)
        self.assertTrue(callable(handlers["resolve.reminder"]))

    def test_snapshot_location_alarm_operation(self):
        db = mock.Mock()
        rows = [
            {
                "alarm_id": "ALARM-1",
                "latitude": 37.3349,
                "longitude": -122.009,
                "location_title": "Apple Park",
                "proximity": 1,
                "radius": 100,
                "address": None,
                "time_interval": None,
                "date_components": None,
            }
        ]
        operations = ReadOperations(self.remctl)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value={"Z_PK": 5}),
            mock.patch.object(self.remctl, "q_alarms", return_value=rows),
        ):
            result = operations.snapshot_location_alarm({
                "operation": "snapshot.locationAlarm",
                "identifier": "REM-1",
                "title": "Apple Park",
                "latitudeE7": 373349000,
                "longitudeE7": -1220090000,
            })
        self.assertEqual(result, {"found": True, "matches": True})
        db.close.assert_called_once()

    def test_snapshot_smart_list_sections_by_pk_operation(self):
        db = mock.Mock()
        rows = [{"Z_PK": 1, "ZDISPLAYNAME": "Produce", "ZCKIDENTIFIER": "SEC-1"}]
        operations = ReadOperations(self.remctl)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_smart_list_sections", return_value=rows),
        ):
            result = operations.snapshot_smart_list_sections_by_pk({
                "operation": "snapshot.smartListSectionsByPk",
                "listId": 7,
            })
        self.assertEqual(result, {
            "sections": [{"id": 1, "name": "Produce", "cloudId": "SEC-1"}],
            "count": 1,
        })
        db.close.assert_called_once()

    def test_snapshot_template_with_items_operation(self):
        db = mock.Mock()
        row = {"Z_PK": 5, "ZNAME": "Packing"}
        payload = {"id": 5, "name": "Packing", "itemCount": 3}
        operations = ReadOperations(self.remctl)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_template_matches", return_value=[row]),
            mock.patch.object(self.remctl, "template_to_dict", return_value=payload) as to_dict,
        ):
            result = operations.snapshot_template_with_items({
                "operation": "snapshot.templateWithItems",
                "identifier": 5,
            })
        to_dict.assert_called_once_with(row, db, include_items=True)
        self.assertTrue(result["found"])
        self.assertEqual(result["id"], 5)
        db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
