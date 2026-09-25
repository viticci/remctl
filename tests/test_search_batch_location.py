"""Search coverage, batch completion and deletion, and address-based location alarms."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import load_module

remctl = load_module("remctl_search_batch_location_test", "remctl")


def run(func, args):
    """Run a command handler; return (exit code or None, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            func(args)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def parse(argv):
    parser, subparsers = remctl.build_parser()
    with contextlib.redirect_stderr(io.StringIO()):
        return remctl.parse_cli_args(parser, subparsers, argv)


def bridge_result(payload, returncode=0, stdout=None):
    return {
        "returncode": returncode,
        "stdout": json.dumps(payload) if stdout is None else stdout,
        "stderr": "",
        "payload": payload,
    }


class SearchTests(unittest.TestCase):
    """Matching, list scope, and paging against a small real SQLite store."""

    def setUp(self):
        remctl._REMINDER_COLUMN_CACHE.clear()
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE ZREMCDBASELIST (
                Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT,
                ZMARKEDFORDELETION INTEGER DEFAULT 0, Z_ENT INTEGER DEFAULT 3);
            CREATE TABLE ZREMCDREMINDER (
                Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZNOTES TEXT, ZCOMPLETED INTEGER DEFAULT 0,
                ZFLAGGED INTEGER DEFAULT 0, ZPRIORITY INTEGER DEFAULT 0, ZDUEDATE REAL, ZALLDAY INTEGER,
                ZCOMPLETIONDATE REAL, ZCREATIONDATE REAL, ZPARENTREMINDER INTEGER, ZLIST INTEGER,
                ZICSURL TEXT, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER DEFAULT 0, ZACCOUNT INTEGER DEFAULT 1);
            CREATE TABLE ZREMCDOBJECT (
                Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZREMINDER2 INTEGER, ZREMINDER4 INTEGER, ZURL TEXT,
                ZMARKEDFORDELETION INTEGER DEFAULT 0, ZFREQUENCY INTEGER, ZINTERVAL INTEGER,
                ZOCCURRENCECOUNT INTEGER, ZENDDATE REAL, ZDAYSOFTHEWEEK BLOB, ZDAYSOFTHEMONTH BLOB,
                ZMONTHSOFTHEYEAR BLOB, ZDAYSOFTHEYEAR BLOB, ZWEEKSOFTHEYEAR BLOB, ZSETPOSITIONS BLOB);
            INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME, ZCKIDENTIFIER) VALUES
                (1, 'Work', 'L1'), (2, 'Errands', 'L2'), (3, 'Work', 'L3');
        """)
        rows = [
            (1, "Read the review", None, 1),
            (2, "Café meeting", None, 2),
            (3, r"100% done_ok\path", None, 2),
            (4, "Other story", None, 3),
            (5, "Straße sign", None, 2),
            (6, "Plain", "notes mention Macstories.net once", 3),
        ]
        rows += [(100 + n, f"Bulk item {n}", None, 2) for n in range(150)]
        db.executemany("INSERT INTO ZREMCDREMINDER (Z_PK, ZTITLE, ZNOTES, ZLIST) VALUES (?, ?, ?, ?)", rows)
        db.executemany(
            "INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZREMINDER2, ZURL, ZMARKEDFORDELETION) VALUES (?, 20, ?, ?, ?)",
            [
                (1, 1, "https://www.macstories.net/stories/remctl-review/", 0),
                (2, 4, "https://www.macstories.net/deleted-link/", 1),  # removed link: must not match
            ],
        )
        self.db = db

    def tearDown(self):
        self.db.close()
        remctl._REMINDER_COLUMN_CACHE.clear()

    def ids(self, query, **kwargs):
        return [row["Z_PK"] for row in remctl.q_search(self.db, query, **kwargs)]

    def test_rich_link_only_match_is_found_in_its_list(self):
        rows = remctl.q_search(self.db, "remctl-review")
        self.assertEqual([(row["Z_PK"], row["list_name"]) for row in rows], [(1, "Work")])
        # The same text in notes still matches; a deleted link does not.
        self.assertEqual(self.ids("macstories.net"), [6, 1])

    def test_matching_ignores_case_and_accents(self):
        self.assertEqual(self.ids("CAFE"), [2])
        self.assertEqual(self.ids("café"), [2])
        self.assertEqual(self.ids("strasse"), [5])

    def test_like_wildcards_and_backslashes_are_literal(self):
        self.assertEqual(self.ids("%"), [3])
        self.assertEqual(self.ids("_"), [3])
        self.assertEqual(self.ids("\\"), [3])
        self.assertEqual(self.ids("done_ok"), [3])
        self.assertEqual(self.ids("done%ok"), [])

    def test_list_scope_uses_exact_list_ids(self):
        self.assertEqual(self.ids("story", list_pk=3), [4])
        self.assertEqual(self.ids("story", list_pk=1), [])

    def test_more_than_one_page_is_reachable_with_honest_counts(self):
        self.assertEqual(remctl.q_search_count(self.db, "bulk"), 150)
        first = self.ids("bulk", limit=100, offset=0)
        second = self.ids("bulk", limit=100, offset=100)
        self.assertEqual((len(first), len(second)), (100, 50))
        self.assertEqual(sorted(first + second), list(range(100, 250)))
        self.assertEqual(first[0], 249)  # newest first

    def _search(self, argv):
        simple_row = lambda row, db=None, **_: {"id": row["Z_PK"], "title": row["ZTITLE"], "list": row["list_name"]}
        with (
            mock.patch.object(remctl, "open_db", return_value=self.db),
            mock.patch.object(remctl, "to_dict", side_effect=simple_row),
            mock.patch.object(remctl, "preload_extras", return_value=({}, {})),
            mock.patch.object(remctl, "preload_attachments", return_value={}),
            mock.patch.object(remctl, "preload_indicators", return_value={}),
        ):
            return run(remctl.cmd_search, parse(["search", *argv]))

    def test_paged_json_reports_total_and_next_offset(self):
        code, out, _ = self._search(["bulk", "--limit", "100", "--json"])
        self.assertIsNone(code)
        page = json.loads(out)
        self.assertEqual((page["count"], page["total"], page["hasMore"], page["nextOffset"]), (100, 150, True, 100))
        code, out, _ = self._search(["bulk", "--limit", "100", "--offset", "100", "--json"])
        page = json.loads(out)
        self.assertEqual((page["count"], page["hasMore"], page["nextOffset"]), (50, False, None))

    def test_unpaged_json_stays_an_array_and_warns_when_cut_short(self):
        code, out, err = self._search(["bulk", "--json"])
        self.assertIsNone(code)
        self.assertEqual(len(json.loads(out)), 100)
        self.assertIn("showing 100 of 150 matches", err)
        self.assertIn("--offset 100", err)

    def test_duplicate_list_names_require_the_numeric_id(self):
        code, _, err = self._search(["story", "--list", "Work", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("multiple lists match", err)
        self.assertIn("--list-id", err)
        code, out, _ = self._search(["story", "--list-id", "3", "--json"])
        self.assertEqual([item["id"] for item in json.loads(out)], [4])
        code, out, _ = self._search(["story", "--list-id", "3", "--offset", "0", "--json"])
        page = json.loads(out)
        self.assertEqual([item["id"] for item in page["items"]], [4])
        self.assertEqual(page["list"], {"id": 3, "title": "Work"})

    def test_leading_dash_query_and_bad_paging_values(self):
        self.assertEqual(parse(["search", "--", "-urgent"]).query, "-urgent")
        code, _, err = self._search(["bulk", "--limit", "0"])
        self.assertEqual(code, 1)
        self.assertIn("--limit", err)


class BatchStateTests(unittest.TestCase):
    """done, undone, and delete with several ids."""

    ROWS = {
        1: {"Z_PK": 1, "ZTITLE": "One", "list_name": "Work", "ZCKIDENTIFIER": "CK-1", "recurrence_frequency": None},
        2: {"Z_PK": 2, "ZTITLE": "Two", "list_name": "Work", "ZCKIDENTIFIER": "CK-2", "recurrence_frequency": None},
        3: {"Z_PK": 3, "ZTITLE": "Weekly review", "list_name": "Work", "ZCKIDENTIFIER": "CK-3", "recurrence_frequency": 2},
    }

    def _run(self, argv, *, bridge=None, osa=True, rows=None):
        """Run a parsed command with fake rows; bridge maps a reminder CK id to a bridge result."""
        bridge = bridge or {}
        success = {"complete": "completed", "uncomplete": "uncompleted", "delete": "deleted"}

        def call(data, timeout=30):
            return bridge.get(data["id"]) or bridge_result({"status": success[data["action"]], "id": data["id"]})

        with (
            mock.patch.object(remctl, "open_db", return_value=None),
            mock.patch.object(remctl, "q_reminder", side_effect=rows or (lambda db, pk: self.ROWS.get(pk))),
            mock.patch.object(remctl, "bridge_available", return_value=True),
            mock.patch.object(remctl, "bridge_call_result", side_effect=call) as bridge_call,
            mock.patch.object(remctl, "osa_by_id_try", return_value=osa) as osa_try,
        ):
            code, out, err = run(getattr(remctl, "cmd_" + argv[0]), parse(argv))
        return SimpleNamespace(code=code, out=out, err=err, bridge=bridge_call, osa=osa_try)

    def test_duplicates_missing_and_recurring_ids_get_accurate_results(self):
        result = self._run(["done", "1", "2", "1", "999", "3", "--json"])
        self.assertEqual(result.code, 1)
        payload = json.loads(result.out)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["duplicatesIgnored"], [1])
        self.assertEqual(payload["succeeded"], [1, 2, 3])
        self.assertEqual(payload["failed"], [999])
        by_id = {item["id"]: item for item in payload["results"]}
        self.assertEqual(by_id[999]["status"], "not_found")
        self.assertTrue(by_id[3]["recurring"])
        # One write per unique existing reminder, and nothing for the missing id.
        self.assertEqual([c.args[0]["id"] for c in result.bridge.call_args_list], ["CK-1", "CK-2", "CK-3"])
        result.osa.assert_not_called()

    def test_uncertain_repeating_completion_is_not_repeated_and_stops_the_batch(self):
        timeout = {"returncode": -1, "stdout": "", "stderr": "", "payload": {"status": "timeout", "message": "slow"}}
        result = self._run(["done", "3", "1", "--json"], bridge={"CK-3": timeout})
        payload = json.loads(result.out)
        self.assertEqual(payload["status"], "partial")  # "failed" would promise nothing changed
        self.assertEqual(payload["uncertain"], [3])
        by_id = {item["id"]: item for item in payload["results"]}
        self.assertEqual(by_id[3]["code"], "completion_uncertain")
        self.assertEqual(by_id[1]["status"], "skipped")
        result.osa.assert_not_called()  # no AppleScript second completion
        self.assertEqual(result.bridge.call_count, 1)  # #1 was never attempted

    def test_single_uncertain_repeating_completion_fails_without_fallback(self):
        crash = bridge_result({"status": "error", "message": "exited"}, returncode=-9, stdout="")
        result = self._run(["done", "3", "--json"], bridge={"CK-3": crash})
        self.assertEqual(result.code, 1)
        self.assertEqual(json.loads(result.err)["code"], "completion_uncertain")
        result.osa.assert_not_called()

    def test_a_reported_bridge_error_still_allows_the_applescript_fallback(self):
        refused = bridge_result({"status": "error", "message": "Complete failed: busy"}, returncode=1)
        result = self._run(["done", "3", "--json"], bridge={"CK-3": refused})
        self.assertIsNone(result.code)
        self.assertEqual(json.loads(result.out)["status"], "completed")
        result.osa.assert_called_once()

    def test_date_on_a_repeating_reminder_fails_only_that_item(self):
        result = self._run(["done", "1", "3", "--date", "2026-09-01", "--json"])
        payload = json.loads(result.out)
        self.assertEqual(payload["succeeded"], [1])
        self.assertEqual(payload["results"][1]["code"], "completion_date_unsupported_for_recurring")
        self.assertEqual(result.bridge.call_count, 1)

    def test_batch_delete_needs_force_and_lists_the_targets(self):
        result = self._run(["delete", "1", "2", "--json"])
        self.assertEqual(result.code, 1)
        error = json.loads(result.err)
        self.assertEqual((error["code"], error["ids"]), ("confirmation_required", [1, 2]))
        result.bridge.assert_not_called()
        result = self._run(["delete", "1", "2", "--force", "--json"])
        self.assertIsNone(result.code)
        self.assertEqual(json.loads(result.out)["status"], "deleted")

    def test_a_success_report_followed_by_a_crash_is_uncertain(self):
        # The bridge only prints its success after EventKit saved, so a crash then means "maybe saved".
        crashed = bridge_result({"status": "completed", "id": "CK-3"}, returncode=-11)
        garbled = bridge_result({"status": "error", "message": "no JSON"}, returncode=0, stdout="not json")
        for label, outcome in (("crash after success", crashed), ("exit 0 without status", garbled)):
            with self.subTest(label):
                result = self._run(["done", "3", "--json"], bridge={"CK-3": outcome})
                self.assertEqual(json.loads(result.err)["code"], "completion_uncertain")
                result.osa.assert_not_called()

    def test_a_stalled_bridge_stops_the_batch_even_when_the_fallback_works(self):
        timeout = {"returncode": -1, "stdout": "", "stderr": "", "payload": {"status": "timeout", "message": "slow"}}
        result = self._run(["done", "1", "2", "--json"], bridge={"CK-1": timeout})
        payload = json.loads(result.out)
        self.assertEqual((payload["succeeded"], payload["failed"]), ([1], [2]))
        self.assertEqual(payload["results"][1]["code"], "batch_stopped")
        self.assertEqual(result.bridge.call_count, 1)

    def test_the_batch_starts_no_write_after_its_time_budget(self):
        with mock.patch.object(remctl.time, "monotonic", side_effect=[0.0, 0.0, 61.0, 62.0]):
            result = self._run(["undone", "1", "2", "--json"])
        payload = json.loads(result.out)
        self.assertEqual(payload["succeeded"], [1])
        self.assertEqual(payload["results"][1]["code"], "batch_time_budget")

    def test_batch_flag_gives_one_id_the_batch_shape(self):
        result = self._run(["done", "1", "--batch", "--json"])
        payload = json.loads(result.out)
        self.assertEqual((payload["status"], payload["succeeded"], payload["results"][0]["id"]), ("completed", [1], 1))

    def test_a_delete_that_fails_because_the_reminder_is_already_gone_counts(self):
        refused = bridge_result({"status": "error", "message": "Reminder not found for id: CK-2"}, returncode=1)
        seen = []

        def rows(db, pk):
            seen.append(pk)
            return None if seen.count(pk) > 1 else self.ROWS.get(pk)  # gone when rechecked

        result = self._run(["delete", "1", "2", "--force", "--json"], bridge={"CK-2": refused}, osa=False, rows=rows)
        payload = json.loads(result.out)
        self.assertEqual((payload["status"], payload["succeeded"]), ("deleted", [1, 2]))

    def test_undone_batch_and_the_size_limit(self):
        result = self._run(["undone", "1", "2", "--json"])
        self.assertEqual(json.loads(result.out)["succeeded"], [1, 2])
        result = self._run(["done", *[str(n) for n in range(1, 52)], "--json"])
        self.assertEqual(json.loads(result.err)["code"], "batch_too_large")
        result.bridge.assert_not_called()


class LocationAddressTests(unittest.TestCase):
    """--location-address and location-lookup with a mocked geocoder."""

    STREET = {
        "name": "Piazza Navona", "address": "Piazza Navona, Rome, Lazio, 00186, Italy", "thoroughfare": "Piazza Navona",
        "latitude": 41.8978252, "longitude": 12.4732402, "regionRadius": 70.7,
    }
    CITY = {"name": "Rome", "address": "Rome, Lazio, Italy", "latitude": 41.8893, "longitude": 12.4935, "regionRadius": 38253.2}

    def _add(self, argv, geocode):
        """Run add with the private stack faked; geocode is the bridge's geocode result."""
        calls = []

        def call(data, timeout=30):
            calls.append(data)
            if data["action"] == "geocode":
                return geocode
            if data["action"] == "create":
                return bridge_result({"status": "created", "id": "CK-NEW", "title": data["title"]})
            raise AssertionError(f"unexpected bridge action {data['action']}")

        with (
            mock.patch.object(remctl, "bridge_available", return_value=True),
            mock.patch.object(remctl, "private_available", return_value=True),
            mock.patch.object(remctl, "open_db", return_value=mock.MagicMock()),
            mock.patch.object(remctl, "validate_private_add_ready"),
            mock.patch.object(remctl, "q_reminder_by_identifier", return_value={"Z_PK": 77}),
            mock.patch.object(remctl, "bridge_call_result", side_effect=call),
            mock.patch.object(remctl, "bridge_call", return_value={"status": "updated", "id": "CK-NEW"}) as update,
        ):
            code, out, err = run(remctl.cmd_add, parse(["add", "Buy stamps", "--private", *argv, "--json"]))
        return SimpleNamespace(code=code, out=out, err=err, calls=calls, update=update)

    def test_address_resolves_before_the_reminder_is_created(self):
        result = self._add(["--location-address", "Piazza Navona, Rome", "--radius", "150"],
                           bridge_result({"status": "ok", "candidates": [self.STREET]}))
        self.assertIsNone(result.code, result.err)
        self.assertEqual([call["action"] for call in result.calls], ["geocode", "create"])
        alarm = result.update.call_args.args[0]
        self.assertEqual((alarm["latitude"], alarm["longitude"], alarm["radius"]), (41.8978252, 12.4732402, 150.0))
        self.assertEqual(alarm["locationTitle"], "Piazza Navona")
        payload = json.loads(result.out)
        self.assertEqual(payload["numericId"], 77)
        resolved = payload["private"][0]["resolvedLocation"]
        self.assertEqual((resolved["address"], resolved["source"], resolved["precision"]),
                         (self.STREET["address"], "geocoded", "street"))

    def test_unusable_lookups_create_nothing(self):
        cases = {
            "location_ambiguous": bridge_result({"status": "ok", "candidates": [self.STREET, {**self.STREET, "latitude": 45.0}]}),
            "location_imprecise": bridge_result({"status": "ok", "candidates": [self.CITY]}),
            "location_not_found": bridge_result({"status": "ok", "candidates": []}),
            "location_lookup_timeout": bridge_result({"status": "error", "code": "timeout", "message": "slow"}, returncode=1),
            "location_lookup_denied": bridge_result({"status": "error", "code": "denied", "message": "no"}, returncode=1),
            # Apple's single best guess for a street it did not find, and for a street with no town.
            "location_unconfirmed": bridge_result({"status": "ok", "candidates": [
                {**self.STREET, "thoroughfare": "Rykneld Court", "name": "1 Rykneld Court, Main Street"}]}),
        }
        for code, geocode in cases.items():
            with self.subTest(code=code):
                result = self._add(["--location-address", "Somewhere"], geocode)
                self.assertEqual(result.code, 1)
                self.assertEqual(json.loads(result.err)["code"], code)
                self.assertEqual([call["action"] for call in result.calls], ["geocode"])
                result.update.assert_not_called()

    def test_invalid_input_stops_before_geocoding(self):
        cases = (
            (["--location-address", "Home"], "location_label_not_address"),
            (["--location-address", "my house!"], "location_label_not_address"),
            (["--location-address", "Via Roma 1", "--radius", "0"], "invalid_location_radius"),
            (["--location-address", "Via Roma 1", "--radius", "0.5"], "invalid_location_radius"),
            (["--location-address", "Via Roma 1", "--radius", "nan"], "invalid_location_radius"),
            (["--location-address", "Via Roma 1", "--radius", "200000"], "invalid_location_radius"),
            (["--location-address", "Via Roma 1", "--latitude", "41.9", "--longitude", "12.5"], "location_address_conflict"),
        )
        for argv, code in cases:
            with self.subTest(argv=argv):
                result = self._add(argv, None)
                self.assertEqual(result.code, 1)
                self.assertEqual(json.loads(result.err)["code"], code)
                self.assertEqual(result.calls, [])

    def test_single_matches_must_name_the_street_and_a_town(self):
        via_roma = {**self.STREET, "name": "Via Roma 1", "thoroughfare": "Via Roma", "postalCode": "00046"}
        cases = (
            ("Main Street 1", {**self.STREET, "thoroughfare": "Rykneld Court", "name": "1 Rykneld Court, Main Street"}, "unconfirmed", "street_mismatch"),
            ("Via Roma 1", via_roma, "unconfirmed", "no_town"),
            ("Via Roma 1, Roma", via_roma, "resolved", None),  # the town may share the street's name
            ("Via Roma 1 00046", via_roma, "resolved", None),  # a postal code confirms the town
            ("Main Street 1, Springfield", {**self.STREET, "name": "1 Main St", "thoroughfare": "Main St"}, "resolved", None),
        )
        for query, raw, status, reason in cases:
            with self.subTest(query=query):
                with (
                    mock.patch.object(remctl, "bridge_available", return_value=True),
                    mock.patch.object(remctl, "bridge_call_result", return_value=bridge_result({"status": "ok", "candidates": [raw]})),
                ):
                    lookup = remctl.lookup_location(query)
                self.assertEqual((lookup["status"], lookup.get("reason")), (status, reason))

    def test_address_needs_the_private_opt_in(self):
        with mock.patch.object(remctl, "bridge_call_result") as bridge:
            code, _, err = run(remctl.cmd_add, parse(["add", "Buy stamps", "--location-address", "Via Roma 1", "--json"]))
        self.assertEqual(code, 1)
        self.assertIn("require --private", err)
        bridge.assert_not_called()

    def test_lookup_reports_status_without_writing(self):
        with (
            mock.patch.object(remctl, "bridge_available", return_value=True),
            mock.patch.object(remctl, "bridge_call_result", return_value=bridge_result({"status": "ok", "candidates": [self.CITY]})) as bridge,
        ):
            code, out, _ = run(remctl.cmd_location_lookup, parse(["location-lookup", "Rome", "--json"]))
        self.assertIsNone(code)
        lookup = json.loads(out)
        self.assertEqual((lookup["status"], lookup["usable"]), ("imprecise", False))
        self.assertEqual(lookup["candidates"][0]["precision"], "area")
        self.assertEqual(bridge.call_args.args[0]["action"], "geocode")


if __name__ == "__main__":
    unittest.main()
