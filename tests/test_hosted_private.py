"""Tests for the private-metadata routing slice added on top of the
read-only Capability Host: ``resolve.section``, ``resolve.sharee``, and
``snapshot.sections`` host operations, the typed backend API on
``ReadBackend``/``DirectReadBackend``/``HostedReadBackend``, and the
``validate_private_add_ready``/``apply_private_changes`` refactor that
routes ``cmd_add``'s ``--private``/``--section``/``--assign``/``--grocery``
flow through that typed API instead of raw sqlite handles.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from helpers import load_module

import remctl_host
import remctl_host_protocol as protocol
from remctl_host_operations import ReadOperations


def request(operation, **fields):
    return {
        "protocolVersion": protocol.PROTOCOL_VERSION,
        "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
        "requestId": "test-request-1",
        "operation": operation,
        **fields,
    }


def _make_test_db():
    uri = f"file:remctl-hosted-private-{uuid.uuid4().hex}?mode=memory&cache=shared"
    keeper = sqlite3.connect(uri, uri=True)
    keeper.row_factory = sqlite3.Row
    keeper.executescript(
        """
        CREATE TABLE ZREMCDBASELIST (
            Z_PK INTEGER PRIMARY KEY,
            Z_ENT INTEGER NOT NULL DEFAULT 3,
            ZNAME TEXT,
            ZCKIDENTIFIER TEXT,
            ZMARKEDFORDELETION INTEGER NOT NULL DEFAULT 0,
            ZISGROUP INTEGER NOT NULL DEFAULT 0,
            ZPARENTLIST INTEGER,
            ZPARENTLIST1 INTEGER,
            Z_FOK_PARENTLIST INTEGER,
            Z_FOK_PARENTLIST1 INTEGER,
            ZDADISPLAYORDER INTEGER,
            ZISPINNEDBYCURRENTUSER INTEGER NOT NULL DEFAULT 0,
            ZPINNEDDATE REAL,
            ZSMARTLISTTYPE INTEGER,
            ZFILTERDATA BLOB,
            ZMINIMUMSUPPORTEDAPPVERSION INTEGER,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION INTEGER,
            ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA TEXT,
            ZREMINDERIDSMERGEABLEORDERING_V2_JSON TEXT,
            ZSHAREDOWNERIDENTIFIER BLOB,
            ZSHOULDCATEGORIZEGROCERYITEMS INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE ZREMCDBASESECTION (
            Z_PK INTEGER PRIMARY KEY,
            ZDISPLAYNAME TEXT,
            ZLIST INTEGER,
            ZSMARTLIST INTEGER,
            ZCKIDENTIFIER TEXT,
            ZMARKEDFORDELETION INTEGER NOT NULL DEFAULT 0,
            ZTEMPLATE INTEGER
        );
        CREATE TABLE ZREMCDREMINDER (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZNOTES TEXT,
            ZCOMPLETED INTEGER NOT NULL DEFAULT 0,
            ZFLAGGED INTEGER NOT NULL DEFAULT 0,
            ZPRIORITY INTEGER NOT NULL DEFAULT 0,
            ZISURGENTSTATEENABLEDFORCURRENTUSER INTEGER NOT NULL DEFAULT 0,
            ZDUEDATE REAL,
            ZDISPLAYDATEDATE REAL,
            ZALLDAY INTEGER NOT NULL DEFAULT 0,
            ZCOMPLETIONDATE REAL,
            ZCREATIONDATE REAL,
            ZPARENTREMINDER INTEGER,
            ZLIST INTEGER,
            ZICSURL TEXT,
            ZCKIDENTIFIER TEXT,
            ZACCOUNT INTEGER,
            ZMARKEDFORDELETION INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE ZREMCDHASHTAGLABEL (
            Z_PK INTEGER PRIMARY KEY,
            ZNAME TEXT
        );
        CREATE TABLE ZREMCDOBJECT (
            Z_PK INTEGER PRIMARY KEY,
            Z_ENT INTEGER,
            ZMARKEDFORDELETION INTEGER NOT NULL DEFAULT 0,
            ZLIST INTEGER,
            ZDISPLAYNAME TEXT,
            ZCKIDENTIFIER TEXT,
            ZFIRSTNAME TEXT,
            ZLASTNAME TEXT,
            ZADDRESS1 TEXT,
            ZSTATUS INTEGER,
            ZACCESSLEVEL INTEGER,
            ZREMINDER2 INTEGER,
            ZFILENAME TEXT,
            ZUTI TEXT,
            ZWIDTH INTEGER,
            ZHEIGHT INTEGER,
            ZSHA512SUM TEXT,
            ZMIMETYPE TEXT,
            ZFILESIZE INTEGER,
            ZREMINDER3 INTEGER,
            ZHASHTAGLABEL INTEGER,
            ZREMINDER INTEGER,
            ZTRIGGER INTEGER,
            ZTIMEINTERVAL REAL,
            ZDATECOMPONENTSDATA TEXT,
            ZTITLE TEXT,
            ZLATITUDE REAL,
            ZLONGITUDE REAL,
            ZRADIUS REAL,
            ZADDRESS TEXT,
            ZPROXIMITY INTEGER,
            ZURL TEXT,
            ZREMINDER4 INTEGER,
            ZFREQUENCY TEXT,
            ZINTERVAL INTEGER,
            ZOCCURRENCECOUNT INTEGER,
            ZENDDATE REAL,
            ZDAYSOFTHEWEEK TEXT,
            ZDAYSOFTHEMONTH TEXT,
            ZMONTHSOFTHEYEAR TEXT,
            ZDAYSOFTHEYEAR TEXT,
            ZWEEKSOFTHEYEAR TEXT,
            ZSETPOSITIONS TEXT
        );
        CREATE TABLE ZREMCDSAVEDATTACHMENT (
            ZFILENAME TEXT,
            ZUTI TEXT,
            ZATTACHMENTTYPERAWVALUE TEXT,
            ZSHA512SUM TEXT,
            ZMIMETYPE TEXT,
            ZFILESIZE INTEGER,
            ZREMINDER INTEGER,
            ZMARKEDFORDELETION INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    owner_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111").bytes
    today = load_module("remctl_hosted_private_dbseed", "remctl")
    keeper.executemany(
        "INSERT INTO ZREMCDBASELIST (Z_PK, Z_ENT, ZNAME, ZCKIDENTIFIER, ZISGROUP, ZPARENTLIST, Z_FOK_PARENTLIST, ZSMARTLISTTYPE, ZFILTERDATA, ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA, ZREMINDERIDSMERGEABLEORDERING_V2_JSON, ZSHAREDOWNERIDENTIFIER, ZSHOULDCATEGORIZEGROCERYITEMS) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 3, "Errands Group", "GROUP-1", 1, None, None, None, None, None, None, None, 0),
            (2, 3, "Groceries", "LIST-2", 0, 1, 20, None, None, json.dumps({"memberships": [{"memberID": "REM-101", "groupID": "SEC-201"}, {"memberID": "REM-105", "groupID": "SEC-202"}]}), json.dumps(["REM-105", "REM-101"]), owner_uuid, 1),
            (3, 3, "Hardware", "LIST-3", 0, 1, 10, None, None, None, json.dumps(["REM-102"]), None, 0),
            (4, 3, "Work", "LIST-4", 0, None, None, None, None, None, None, None, 0),
            (50, 4, "Focus", "SMART-50", 0, None, None, 2, None, None, None, None, 0),
        ],
    )
    keeper.executemany(
        "INSERT INTO ZREMCDBASESECTION (Z_PK, ZDISPLAYNAME, ZLIST, ZSMARTLIST, ZCKIDENTIFIER) VALUES (?, ?, ?, ?, ?)",
        [
            (201, "Produce", 2, None, "SEC-201"),
            (202, "Dairy", 2, None, "SEC-202"),
            (203, "Tools", 3, None, "SEC-203"),
            (250, "Pinned", None, 50, "SEC-250"),
        ],
    )
    to_ts = today.to_ts
    start = today.start_of_day()
    reminders = [
        (101, "Milk", "buy oat milk", 0, 1, 0, 0, to_ts(start.replace(hour=9)), to_ts(start), 1, None, to_ts(start), None, 2, None, "REM-101", 1, 0),
        (102, "Buy bolts", "hardware aisle", 0, 0, 0, 1, to_ts(start + today.timedelta(days=2, hours=10)), to_ts(start + today.timedelta(days=2)), 0, None, to_ts(start), None, 3, None, "REM-102", 1, 0),
        (103, "Past due", "late", 0, 0, 0, 0, to_ts(start - today.timedelta(days=1, hours=1)), to_ts(start - today.timedelta(days=1)), 0, None, to_ts(start), None, 4, None, "REM-103", 1, 0),
        (104, "Done child", "completed child", 1, 0, 0, 0, None, None, 0, to_ts(start), to_ts(start), 101, 2, None, "REM-104", 1, 0),
        (105, "Nested active", "subtask detail", 0, 0, 0, 0, None, None, 0, None, to_ts(start), 101, 2, None, "REM-105", 1, 0),
        (106, "Completed top", "done root", 1, 0, 0, 0, None, None, 0, to_ts(start), to_ts(start), None, 4, None, "REM-106", 1, 0),
    ]
    keeper.executemany(
        "INSERT INTO ZREMCDREMINDER (Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZCOMPLETIONDATE, ZCREATIONDATE, ZPARENTREMINDER, ZLIST, ZICSURL, ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        reminders,
    )
    keeper.executemany(
        "INSERT INTO ZREMCDHASHTAGLABEL (Z_PK, ZNAME) VALUES (?, ?)",
        [(1, "groceries"), (2, "urgent")],
    )
    keeper.execute(
        "INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZREMINDER3, ZHASHTAGLABEL) VALUES (?, ?, ?, ?)",
        (900, 0, 101, 1),
    )
    keeper.executemany(
        "INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZLIST, ZCKIDENTIFIER, ZDISPLAYNAME, ZADDRESS1, ZSTATUS, ZACCESSLEVEL) VALUES (?, 36, ?, ?, ?, ?, ?, ?)",
        [
            (910, 2, "OWNER-CKID", "Matt", "matt@example.com", 1, 2),
            (911, 2, "SHAREE-1", "Alex", "alex@example.com", 1, 1),
        ],
    )
    keeper.execute(
        "INSERT INTO ZREMCDSAVEDATTACHMENT (ZFILENAME, ZUTI, ZATTACHMENTTYPERAWVALUE, ZMIMETYPE, ZFILESIZE, ZREMINDER, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, ?, ?, 0)",
        ("child.png", "public.png", "image", "image/png", 1234, 105),
    )
    keeper.commit()

    def open_db():
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    return keeper, open_db


# ── 1. Protocol tests ────────────────────────────────────────────────────────

class ProtocolTests(unittest.TestCase):
    def test_new_operations_are_implemented(self):
        for name in (
            "resolve.section",
            "resolve.sharee",
            "snapshot.sections",
            "snapshot.smartListSectionsByPk",
            "snapshot.templateWithItems",
            "snapshot.locationAlarm",
        ):
            with self.subTest(operation=name):
                self.assertIn(name, protocol.IMPLEMENTED_OPERATIONS)
                self.assertIn(name, protocol.OPERATIONS)

    def test_resolve_section_schema_is_closed_and_exactly_one_of(self):
        operation = protocol.OPERATIONS["resolve.section"]
        self.assertEqual(set(operation.fields), {"listId", "name", "cloudId"})
        self.assertEqual(operation.exactly_one_of, ("name", "cloudId"))
        self.assertTrue(operation.fields["listId"].required)

    def test_resolve_sharee_schema_is_closed(self):
        operation = protocol.OPERATIONS["resolve.sharee"]
        self.assertEqual(set(operation.fields), {"listId", "identifier"})
        self.assertTrue(operation.fields["listId"].required)
        self.assertTrue(operation.fields["identifier"].required)

    def test_snapshot_sections_schema_is_closed(self):
        operation = protocol.OPERATIONS["snapshot.sections"]
        self.assertEqual(set(operation.fields), {"listId"})
        self.assertTrue(operation.fields["listId"].required)

    def test_accepts_closed_resolve_section_request_by_name(self):
        payload = request("resolve.section", listId=1, name="Produce")
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_accepts_closed_resolve_section_request_by_cloud_id(self):
        payload = request("resolve.section", listId=1, cloudId="SEC-1")
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_rejects_resolve_section_with_both_name_and_cloud_id(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(
                request("resolve.section", listId=1, name="Produce", cloudId="SEC-1")
            )
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_rejects_resolve_section_missing_list_id(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(request("resolve.section", name="Produce"))
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_accepts_closed_resolve_sharee_request(self):
        payload = request("resolve.sharee", listId=1, identifier="Alex")
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_accepts_resolve_sharee_me_identifier(self):
        payload = request("resolve.sharee", listId=1, identifier="me")
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_rejects_resolve_sharee_missing_identifier(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(request("resolve.sharee", listId=1))
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_accepts_closed_snapshot_sections_request(self):
        payload = request("snapshot.sections", listId=1)
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_rejects_snapshot_sections_unknown_field(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(request("snapshot.sections", listId=1, path="/tmp/x"))
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_manifest_digest_reflects_expanded_operations(self):
        self.assertEqual(
            protocol.SCHEMA_MANIFEST_DIGEST,
            protocol.schema_manifest_digest(),
        )
        self.assertEqual(
            protocol.schema_manifest()["implementedOperations"],
            list(protocol.IMPLEMENTED_OPERATIONS),
        )

    # ── Mutation-negative (see also MutationNegativeTests below) ────────────
    def test_new_operations_have_no_mutation_fields(self):
        mutation_keywords = (
            "title", "notes", "due", "priority", "tags", "url", "recurrence",
            "alarm", "delete", "complete",
        )
        for name in (
            "resolve.section",
            "resolve.sharee",
            "snapshot.sections",
            "snapshot.smartListSectionsByPk",
            "snapshot.templateWithItems",
        ):
            operation = protocol.OPERATIONS[name]
            for field_name in operation.fields:
                with self.subTest(operation=name, field=field_name):
                    self.assertNotIn(field_name.lower(), mutation_keywords)


# ── 8. Mutation-negative ─────────────────────────────────────────────────────

class MutationNegativeTests(unittest.TestCase):
    MUTATION_OPERATION_NAMES = (
        "add", "edit", "delete", "complete", "uncomplete", "move", "assign",
        "unassign", "categorize", "create", "update", "remove",
    )

    def test_implemented_operations_contain_no_mutation_verbs(self):
        for name in protocol.IMPLEMENTED_OPERATIONS:
            verb = name.split(".")[0]
            with self.subTest(operation=name):
                self.assertIn(verb, ("health", "probe", "resolve", "snapshot", "poll"))
                self.assertNotIn(name, self.MUTATION_OPERATION_NAMES)

    def test_implemented_operations_unchanged_for_pre_existing_ops(self):
        # The three new operations are additive; the pre-existing read
        # operations from the previous slice must still be present.
        for name in ("health", "resolve.list", "resolve.reminder"):
            self.assertIn(name, protocol.IMPLEMENTED_OPERATIONS)

    def test_new_snapshot_operations_are_read_only(self):
        for name in (
            "snapshot.smartListSectionsByPk",
            "snapshot.templateWithItems",
            "snapshot.locationAlarm",
        ):
            with self.subTest(operation=name):
                self.assertIn(name, protocol.IMPLEMENTED_OPERATIONS)
                self.assertNotIn(name, self.MUTATION_OPERATION_NAMES)


# ── 2. Host operations unit tests ────────────────────────────────────────────

class HostOperationsResolveSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_ops_test", "remctl")

    def _operations(self):
        return ReadOperations(self.remctl)

    def test_resolve_section_by_name_found(self):
        db = mock.Mock()
        sections = [{"ZCKIDENTIFIER": "SEC-1", "ZDISPLAYNAME": "Produce", "Z_PK": 5}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=sections),
        ):
            result = self._operations().resolve_section(
                {"operation": "resolve.section", "listId": 1, "name": "Produce"}
            )
        self.assertEqual(result, {"cloudId": "SEC-1", "name": "Produce", "id": 5})
        db.close.assert_called_once()

    def test_resolve_section_by_cloud_id_found(self):
        db = mock.Mock()
        sections = [{"ZCKIDENTIFIER": "SEC-1", "ZDISPLAYNAME": "Produce", "Z_PK": 5}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=sections),
        ):
            result = self._operations().resolve_section(
                {"operation": "resolve.section", "listId": 1, "cloudId": "sec-1"}
            )
        self.assertEqual(result, {"cloudId": "SEC-1", "name": "Produce", "id": 5})

    def test_resolve_section_by_name_not_found(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=[]),
        ):
            result = self._operations().resolve_section(
                {"operation": "resolve.section", "listId": 1, "name": "Bogus"}
            )
        self.assertEqual(result["error"], "not_found")
        self.assertEqual(result["message"], "Error: section not found in target list: Bogus")
        db.close.assert_called_once()

    def test_resolve_section_by_cloud_id_not_found(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=[]),
        ):
            result = self._operations().resolve_section(
                {"operation": "resolve.section", "listId": 1, "cloudId": "SEC-X"}
            )
        self.assertEqual(result["error"], "not_found")
        self.assertEqual(result["message"], "Error: section ID not found in target list: SEC-X")

    def test_resolve_section_by_name_ambiguous(self):
        db = mock.Mock()
        sections = [
            {"ZCKIDENTIFIER": "SEC-1", "ZDISPLAYNAME": "Produce", "Z_PK": 5},
            {"ZCKIDENTIFIER": "SEC-2", "ZDISPLAYNAME": "Produce", "Z_PK": 6},
        ]
        counts = {"SEC-1": 3, "SEC-2": 2}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=sections),
            mock.patch.object(self.remctl, "q_section_member_counts", return_value=counts),
        ):
            result = self._operations().resolve_section(
                {"operation": "resolve.section", "listId": 1, "name": "Produce"}
            )
        self.assertEqual(result["error"], "ambiguous")
        self.assertIn("multiple sections named 'Produce'", result["message"])
        self.assertIn("SEC-1", result["message"])
        self.assertIn("SEC-2", result["message"])

    def test_resolve_section_by_name_ambiguous_resolved_by_non_empty_count(self):
        # When exactly one of the matches has members, the direct-path logic
        # (and this reimplementation) silently prefers it instead of erroring.
        db = mock.Mock()
        sections = [
            {"ZCKIDENTIFIER": "SEC-1", "ZDISPLAYNAME": "Produce", "Z_PK": 5},
            {"ZCKIDENTIFIER": "SEC-2", "ZDISPLAYNAME": "Produce", "Z_PK": 6},
        ]
        counts = {"SEC-1": 0, "SEC-2": 4}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=sections),
            mock.patch.object(self.remctl, "q_section_member_counts", return_value=counts),
        ):
            result = self._operations().resolve_section(
                {"operation": "resolve.section", "listId": 1, "name": "Produce"}
            )
        self.assertEqual(result, {"cloudId": "SEC-2", "name": "Produce", "id": 6})

    def test_resolve_section_requires_a_name_or_cloud_id(self):
        db = mock.Mock()
        with mock.patch.object(self.remctl, "open_db", return_value=db):
            result = self._operations().resolve_section(
                {"operation": "resolve.section", "listId": 1}
            )
        self.assertEqual(result["error"], "no_identifier")
        db.close.assert_called_once()

    def test_resolve_section_closes_db_on_exception(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                self._operations().resolve_section(
                    {"operation": "resolve.section", "listId": 1, "name": "Produce"}
                )
        db.close.assert_called_once()


class HostOperationsResolveShareeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_ops_sharee_test", "remctl")

    def _operations(self):
        return ReadOperations(self.remctl)

    def test_resolve_sharee_found_by_identifier(self):
        db = mock.Mock()
        row = {"Z_PK": 10, "ZCKIDENTIFIER": "SHAREE-1"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "resolve_sharee_or_die", return_value=row) as resolver,
            mock.patch.object(self.remctl, "_sharee_display_name", return_value="Alex"),
        ):
            result = self._operations().resolve_sharee(
                {"operation": "resolve.sharee", "listId": 7, "identifier": "Alex"}
            )
        resolver.assert_called_once_with(db, 7, "Alex")
        self.assertEqual(result, {
            "id": 10,
            "ZCKIDENTIFIER": "SHAREE-1",
            "cloudId": "SHAREE-1",
            "name": "Alex",
        })
        db.close.assert_called_once()

    def test_resolve_sharee_me_special_case(self):
        db = mock.Mock()
        row = {"Z_PK": 11, "ZCKIDENTIFIER": "OWNER-CKID"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "resolve_sharee_or_die", return_value=row) as resolver,
            mock.patch.object(self.remctl, "_sharee_display_name", return_value="Current User"),
        ):
            result = self._operations().resolve_sharee(
                {"operation": "resolve.sharee", "listId": 7, "identifier": "me"}
            )
        resolver.assert_called_once_with(db, 7, "me")
        self.assertEqual(result["ZCKIDENTIFIER"], "OWNER-CKID")

    def test_resolve_sharee_not_found_does_not_kill_broker(self):
        db = mock.Mock()

        def fake_resolve_sharee_or_die(_db, _list_pk, identifier):
            print(
                f"Error: no sharee matching {identifier!r} in this list. Available: Alex",
                file=sys.stderr,
            )
            sys.exit(1)

        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl, "resolve_sharee_or_die", side_effect=fake_resolve_sharee_or_die
            ),
        ):
            # If SystemExit escaped, this call itself would raise and fail the test.
            result = self._operations().resolve_sharee(
                {"operation": "resolve.sharee", "listId": 7, "identifier": "Bogus"}
            )
        self.assertEqual(result["error"], "failed")
        self.assertEqual(
            result["message"],
            "Error: no sharee matching 'Bogus' in this list. Available: Alex",
        )
        db.close.assert_called_once()

    def test_resolve_sharee_closes_db_on_exception(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl, "resolve_sharee_or_die", side_effect=RuntimeError("boom")
            ),
        ):
            with self.assertRaises(RuntimeError):
                self._operations().resolve_sharee(
                    {"operation": "resolve.sharee", "listId": 7, "identifier": "Alex"}
                )
        db.close.assert_called_once()


class HostOperationsSnapshotSectionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_ops_snapshot_test", "remctl")

    def _operations(self):
        return ReadOperations(self.remctl)

    def test_snapshot_sections_returns_sections_and_memberships(self):
        db = mock.Mock()
        sections = [
            {"Z_PK": 1, "ZDISPLAYNAME": "Produce", "ZCKIDENTIFIER": "SEC-1"},
            {"Z_PK": 2, "ZDISPLAYNAME": "Dairy", "ZCKIDENTIFIER": "SEC-2"},
        ]
        memberships = {"REM-1": "Produce", "REM-2": "Dairy"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=sections),
            mock.patch.object(self.remctl, "q_section_memberships", return_value=memberships),
        ):
            result = self._operations().snapshot_sections(
                {"operation": "snapshot.sections", "listId": 1}
            )
        self.assertEqual(result["sections"], [
            {"id": 1, "name": "Produce", "cloudId": "SEC-1"},
            {"id": 2, "name": "Dairy", "cloudId": "SEC-2"},
        ])
        self.assertEqual(result["memberships"], memberships)
        db.close.assert_called_once()

    def test_snapshot_sections_closes_db_on_exception(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                self._operations().snapshot_sections(
                    {"operation": "snapshot.sections", "listId": 1}
                )
        db.close.assert_called_once()


class HandlersRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_handlers_test", "remctl")

    def test_handlers_include_new_operations(self):
        handlers = ReadOperations(self.remctl).handlers()
        for name in (
            "resolve.section",
            "resolve.sharee",
            "snapshot.sections",
            "snapshot.listReminderCount",
            "snapshot.locationAlarm",
            "snapshot.smartListSectionsByPk",
            "snapshot.templateWithItems",
        ):
            with self.subTest(operation=name):
                self.assertIn(name, handlers)
                self.assertTrue(callable(handlers[name]))


# ── 3. DirectReadBackend typed methods ───────────────────────────────────────

class DirectReadBackendTypedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_direct_test", "remctl")

    def _backend(self):
        return self.remctl.DirectReadBackend()

    def test_typed_resolve_section_delegates_to_resolve_section_ckid(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl, "resolve_section_ckid", return_value="SEC-CKID"
            ) as resolver,
        ):
            result = self._backend().typed_resolve_section(
                7, section_name="Produce", section_id=None
            )
        resolver.assert_called_once_with(db, 7, section_name="Produce", section_id=None)
        self.assertEqual(result, "SEC-CKID")
        db.close.assert_called_once()

    def test_typed_resolve_sharee_wraps_row_with_display_name(self):
        db = mock.Mock()
        row = {"Z_PK": 3, "ZCKIDENTIFIER": "SHAREE-1"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "resolve_sharee_or_die", return_value=row) as resolver,
            mock.patch.object(self.remctl, "_sharee_display_name", return_value="Alex"),
        ):
            result = self._backend().typed_resolve_sharee(7, "Alex")
        resolver.assert_called_once_with(db, 7, "Alex")
        self.assertEqual(result, {
            "id": 3,
            "ZCKIDENTIFIER": "SHAREE-1",
            "cloudId": "SHAREE-1",
            "name": "Alex",
        })
        db.close.assert_called_once()

    def test_typed_snapshot_sections_converts_rows(self):
        db = mock.Mock()
        rows = [{"Z_PK": 1, "ZDISPLAYNAME": "Produce", "ZCKIDENTIFIER": "SEC-1"}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_sections", return_value=rows) as q_sections,
        ):
            result = self._backend().typed_snapshot_sections(7)
        q_sections.assert_called_once_with(db, 7)
        self.assertEqual(result, [{"id": 1, "name": "Produce", "cloudId": "SEC-1"}])
        db.close.assert_called_once()

    def test_typed_section_memberships_delegates(self):
        db = mock.Mock()
        memberships = {"REM-1": "Produce"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl, "q_section_memberships", return_value=memberships
            ) as q_memberships,
        ):
            result = self._backend().typed_section_memberships(7)
        q_memberships.assert_called_once_with(db, 7)
        self.assertEqual(result, memberships)
        db.close.assert_called_once()

    def test_typed_list_ckid_delegates(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_list_ckid", return_value="LIST-CKID") as q_ckid,
        ):
            result = self._backend().typed_list_ckid(7)
        q_ckid.assert_called_once_with(db, 7)
        self.assertEqual(result, "LIST-CKID")
        db.close.assert_called_once()

    def test_typed_require_grocery_target_delegates(self):
        db = mock.Mock()
        grocery_row = {"ZNAME": "Groceries", "ZSHOULDCATEGORIZEGROCERYITEMS": 1}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_list_by_pk", return_value=grocery_row),
        ):
            # Should succeed without raising SystemExit for a grocery list.
            self._backend().typed_require_grocery_target(7)
        db.close.assert_called_once()

    def test_typed_hashtags_extracts_names(self):
        db = mock.Mock()
        rows = [{"ZNAME": "Work"}, {"ZNAME": "Home"}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_hashtags", return_value=rows) as q_hashtags,
        ):
            result = self._backend().typed_hashtags(42)
        q_hashtags.assert_called_once_with(db, 42)
        self.assertEqual(result, ["Work", "Home"])
        db.close.assert_called_once()

    def test_typed_resolve_reminder_by_ckid_converts_row(self):
        db = mock.Mock()
        row = {
            "Z_PK": 5,
            "ZCKIDENTIFIER": "REM-1",
            "ZTITLE": "Buy milk",
            "ZLIST": 7,
            "ZCOMPLETED": 0,
            "ZMARKEDFORDELETION": 0,
        }
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl, "q_reminder_by_identifier", return_value=row
            ) as q_reminder,
        ):
            result = self._backend().typed_resolve_reminder_by_ckid("REM-1")
        q_reminder.assert_called_once_with(db, "REM-1")
        self.assertEqual(result["id"], 5)
        self.assertEqual(result["listId"], 7)
        self.assertFalse(result["completed"])
        db.close.assert_called_once()

    def test_typed_resolve_reminder_by_ckid_returns_none_when_missing(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=None),
        ):
            result = self._backend().typed_resolve_reminder_by_ckid("MISSING")
        self.assertIsNone(result)
        db.close.assert_called_once()

    def test_typed_early_reminder_identifiers_delegates(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl,
                "early_reminder_identifiers_for_reminder",
                return_value=["DELTA-1"],
            ) as delegate,
        ):
            result = self._backend().typed_early_reminder_identifiers("REM-1")
        delegate.assert_called_once_with(db, "REM-1")
        self.assertEqual(result, ["DELTA-1"])
        db.close.assert_called_once()

    def test_typed_list_reminder_count_delegates_to_q_function(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl,
                "q_list_reminder_count_for_template",
                return_value=7,
            ) as delegate,
        ):
            result = self._backend().typed_list_reminder_count(42, include_completed=True)
        delegate.assert_called_once_with(db, 42, include_completed=True)
        self.assertEqual(result, 7)
        db.close.assert_called_once()

    def test_typed_list_reminder_count_default_include_completed_false(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(
                self.remctl, "q_list_reminder_count_for_template", return_value=3
            ) as delegate,
        ):
            self._backend().typed_list_reminder_count(10)
        delegate.assert_called_once_with(db, 10, include_completed=False)

    def test_typed_smart_list_sections_by_pk_delegates_to_q_function(self):
        db = mock.Mock()
        rows = [{"Z_PK": 1, "ZDISPLAYNAME": "Produce", "ZCKIDENTIFIER": "SEC-1"}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_smart_list_sections", return_value=rows) as query,
        ):
            result = self._backend().typed_smart_list_sections_by_pk(7)
        query.assert_called_once_with(db, 7)
        self.assertEqual(result, [{"id": 1, "name": "Produce", "cloudId": "SEC-1"}])
        db.close.assert_called_once()

    def test_typed_smart_list_sections_by_pk_returns_empty_for_no_sections(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_smart_list_sections", return_value=[]),
        ):
            result = self._backend().typed_smart_list_sections_by_pk(7)
        self.assertEqual(result, [])
        db.close.assert_called_once()

    def test_typed_template_with_items_delegates_to_template_to_dict(self):
        db = mock.Mock()
        row = {"Z_PK": 5, "ZNAME": "Packing"}
        payload = {"id": 5, "name": "Packing", "itemCount": 3}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_template_matches", return_value=[row]) as query,
            mock.patch.object(self.remctl, "template_to_dict", return_value=payload) as to_dict,
        ):
            result = self._backend().typed_template_with_items(5)
        query.assert_called_once_with(db, template_id=5)
        to_dict.assert_called_once_with(row, db, include_items=True)
        self.assertEqual(result, payload)
        db.close.assert_called_once()

    def test_typed_template_with_items_returns_none_when_not_found(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_template_matches", return_value=[]),
        ):
            result = self._backend().typed_template_with_items(5)
        self.assertIsNone(result)
        db.close.assert_called_once()


# ── 4. HostedReadBackend typed methods ───────────────────────────────────────

class HostedReadBackendTypedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_hosted_test", "remctl")

    def _backend(self):
        return self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")

    def test_typed_resolve_section_success_returns_cloud_id(self):
        with mock.patch.object(
            remctl_host,
            "resolve_section",
            return_value={"cloudId": "SEC-1", "name": "Produce", "id": 5},
        ) as call:
            result = self._backend().typed_resolve_section(7, section_name="Produce")
        call.assert_called_once()
        self.assertEqual(result, "SEC-1")

    def test_typed_resolve_section_neither_given_returns_none_without_host_call(self):
        with mock.patch.object(remctl_host, "resolve_section") as call:
            result = self._backend().typed_resolve_section(7)
        call.assert_not_called()
        self.assertIsNone(result)

    def test_typed_resolve_section_both_given_exits_without_host_call(self):
        with mock.patch.object(remctl_host, "resolve_section") as call:
            with self.assertRaises(SystemExit):
                self._backend().typed_resolve_section(
                    7, section_name="Produce", section_id="SEC-1"
                )
        call.assert_not_called()

    def test_typed_resolve_section_missing_list_pk_exits_without_host_call(self):
        with mock.patch.object(remctl_host, "resolve_section") as call:
            with self.assertRaises(SystemExit):
                self._backend().typed_resolve_section(None, section_name="Produce")
        call.assert_not_called()

    def test_typed_resolve_section_error_dict_exits(self):
        with mock.patch.object(
            remctl_host,
            "resolve_section",
            return_value={"error": "not_found", "message": "Error: section not found in target list: Bogus"},
        ):
            with self.assertRaises(SystemExit):
                self._backend().typed_resolve_section(7, section_name="Bogus")

    def test_typed_resolve_section_host_unavailable_raises_reminders_db_unavailable(self):
        with mock.patch.object(
            remctl_host, "resolve_section", side_effect=remctl_host.HostUnavailable("down")
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_resolve_section(7, section_name="Produce")

    def test_typed_resolve_sharee_success_returns_dict(self):
        payload = {"id": 3, "ZCKIDENTIFIER": "SHAREE-1", "cloudId": "SHAREE-1", "name": "Alex"}
        with mock.patch.object(remctl_host, "resolve_sharee", return_value=payload):
            result = self._backend().typed_resolve_sharee(7, "Alex")
        self.assertEqual(result, payload)

    def test_typed_resolve_sharee_error_dict_exits(self):
        with mock.patch.object(
            remctl_host,
            "resolve_sharee",
            return_value={"error": "failed", "message": "Error: no sharee matching 'Bogus'."},
        ):
            with self.assertRaises(SystemExit):
                self._backend().typed_resolve_sharee(7, "Bogus")

    def test_typed_resolve_sharee_host_unavailable_raises_reminders_db_unavailable(self):
        with mock.patch.object(
            remctl_host, "resolve_sharee", side_effect=remctl_host.HostUnavailable("down")
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_resolve_sharee(7, "Alex")

    def test_typed_snapshot_sections_returns_sections(self):
        sections = [{"id": 1, "name": "Produce", "cloudId": "SEC-1"}]
        with mock.patch.object(
            remctl_host,
            "snapshot_sections",
            return_value={"sections": sections, "memberships": {}},
        ):
            result = self._backend().typed_snapshot_sections(7)
        self.assertEqual(result, sections)

    def test_typed_section_memberships_returns_memberships(self):
        memberships = {"REM-1": "Produce"}
        with mock.patch.object(
            remctl_host,
            "snapshot_sections",
            return_value={"sections": [], "memberships": memberships},
        ):
            result = self._backend().typed_section_memberships(7)
        self.assertEqual(result, memberships)

    def test_typed_snapshot_sections_host_unavailable_raises_reminders_db_unavailable(self):
        with mock.patch.object(
            remctl_host, "snapshot_sections", side_effect=remctl_host.HostUnavailable("down")
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_snapshot_sections(7)

    def test_typed_list_ckid_returns_object_uuid(self):
        with mock.patch.object(
            remctl_host, "resolve_list", return_value={"id": 7, "objectUUID": "LIST-CKID"}
        ):
            result = self._backend().typed_list_ckid(7)
        self.assertEqual(result, "LIST-CKID")

    def test_typed_list_ckid_returns_none_when_list_missing(self):
        with mock.patch.object(remctl_host, "resolve_list", return_value=None):
            result = self._backend().typed_list_ckid(7)
        self.assertIsNone(result)

    def test_typed_require_grocery_target_passes_for_grocery_list(self):
        # B2: grocery dict must have shouldCategorizeItems: True (parity with
        # direct mode ZSHOULDCATEGORIZEGROCERYITEMS == 1).
        with mock.patch.object(
            remctl_host,
            "resolve_list",
            return_value={"id": 7, "title": "Groceries", "grocery": {"shouldCategorizeItems": True}},
        ):
            self._backend().typed_require_grocery_target(7)  # should not raise

    def test_typed_require_grocery_target_exits_when_list_missing(self):
        with mock.patch.object(remctl_host, "resolve_list", return_value=None):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_typed_require_grocery_target_exits_when_not_grocery(self):
        # B2: grocery key present but shouldCategorizeItems is False must be rejected.
        with mock.patch.object(
            remctl_host,
            "resolve_list",
            return_value={"id": 7, "title": "Work", "grocery": {"shouldCategorizeItems": False}},
        ):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_typed_require_grocery_target_exits_when_grocery_key_missing(self):
        # B2 divergent-field parity: grocery dict exists but shouldCategorizeItems
        # is absent — must be treated the same as not a grocery list.
        with mock.patch.object(
            remctl_host,
            "resolve_list",
            return_value={"id": 7, "title": "Maybe", "grocery": {"locale": "en_US"}},
        ):
            with self.assertRaises(SystemExit):
                self._backend().typed_require_grocery_target(7)

    def test_typed_hashtags_uses_snapshot_reminder(self):
        # C1: real host call; returns the actual hashtags for existing reminders.
        with mock.patch.object(
            remctl_host,
            "snapshot_reminder",
            return_value={"found": True, "hashtags": ["groceries", "urgent"], "earlyReminderIdentifiers": []},
        ) as call:
            result = self._backend().typed_hashtags(42)
        self.assertEqual(result, ["groceries", "urgent"])
        call.assert_called_once()

    def test_typed_hashtags_returns_empty_for_not_found_reminder(self):
        # C1: not-found reminder (newly created or deleted) → safe empty list.
        with mock.patch.object(
            remctl_host,
            "snapshot_reminder",
            return_value={"found": False, "hashtags": [], "earlyReminderIdentifiers": []},
        ):
            result = self._backend().typed_hashtags(99)
        self.assertEqual(result, [])

    def test_typed_hashtags_raises_on_host_unavailable(self):
        # C1: loud failure if host is unreachable — no silent data loss.
        from remctl_host import HostUnavailable
        remctl_module = load_module("remctl_hosted_hashtag_fail", "remctl")
        backend = remctl_module.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with mock.patch.object(remctl_host, "snapshot_reminder", side_effect=HostUnavailable("gone")):
            with self.assertRaises(remctl_module.RemindersDBUnavailable):
                backend.typed_hashtags(42)

    def test_typed_resolve_reminder_by_ckid_delegates_to_resolve_reminder(self):
        payload = {"id": 5, "identifier": "REM-1", "listId": 7}
        with mock.patch.object(remctl_host, "resolve_reminder", return_value=payload):
            result = self._backend().typed_resolve_reminder_by_ckid("REM-1")
        self.assertEqual(result, payload)

    def test_typed_early_reminder_identifiers_uses_snapshot_reminder(self):
        # C1: real host call preserves existing early-reminder identifiers.
        with mock.patch.object(
            remctl_host,
            "snapshot_reminder",
            return_value={"found": True, "hashtags": [], "earlyReminderIdentifiers": ["ALERT-A", "ALERT-B"]},
        ) as call:
            result = self._backend().typed_early_reminder_identifiers("REM-1")
        self.assertEqual(result, ["ALERT-A", "ALERT-B"])
        call.assert_called_once()

    def test_typed_early_reminder_identifiers_returns_empty_for_not_found(self):
        # C1: not-found reminder → safe empty list.
        with mock.patch.object(
            remctl_host,
            "snapshot_reminder",
            return_value={"found": False, "hashtags": [], "earlyReminderIdentifiers": []},
        ):
            result = self._backend().typed_early_reminder_identifiers("NO-SUCH")
        self.assertEqual(result, [])

    def test_typed_early_reminder_identifiers_raises_on_host_unavailable(self):
        # C1: loud failure if host is unreachable — no silent data loss.
        from remctl_host import HostUnavailable
        remctl_module = load_module("remctl_hosted_early_fail", "remctl")
        backend = remctl_module.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with mock.patch.object(remctl_host, "snapshot_reminder", side_effect=HostUnavailable("gone")):
            with self.assertRaises(remctl_module.RemindersDBUnavailable):
                backend.typed_early_reminder_identifiers("REM-1")

    def test_typed_list_reminder_count_returns_count_from_host(self):
        backend = self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with mock.patch.object(
            remctl_host,
            "snapshot_list_reminder_count",
            return_value={"count": 5},
        ) as call:
            result = backend.typed_list_reminder_count(42, include_completed=True)
        call.assert_called_once()
        self.assertEqual(result, 5)

    def test_typed_list_reminder_count_returns_none_on_host_error(self):
        backend = self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with mock.patch.object(
            remctl_host,
            "snapshot_list_reminder_count",
            return_value={"error": "db_error"},
        ):
            result = backend.typed_list_reminder_count(42)
        self.assertIsNone(result)

    def test_typed_smart_list_sections_by_pk_returns_sections_from_host(self):
        sections = [{"id": 1, "name": "Produce", "cloudId": "SEC-1"}]
        with mock.patch.object(
            remctl_host,
            "snapshot_smart_list_sections_by_pk",
            return_value={"sections": sections, "count": 1},
        ) as call:
            result = self._backend().typed_smart_list_sections_by_pk(7)
        call.assert_called_once()
        self.assertEqual(result, sections)

    def test_typed_smart_list_sections_by_pk_no_sections_returns_empty_list(self):
        with mock.patch.object(
            remctl_host,
            "snapshot_smart_list_sections_by_pk",
            return_value={"sections": [], "count": 0},
        ):
            result = self._backend().typed_smart_list_sections_by_pk(7)
        self.assertEqual(result, [])

    def test_typed_smart_list_sections_by_pk_raises_on_host_unavailable(self):
        with mock.patch.object(
            remctl_host,
            "snapshot_smart_list_sections_by_pk",
            side_effect=remctl_host.HostUnavailable("down"),
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_smart_list_sections_by_pk(7)

    def test_typed_template_with_items_returns_payload_from_host(self):
        payload = {"found": True, "id": 5, "name": "Packing", "itemCount": 3}
        with mock.patch.object(
            remctl_host,
            "snapshot_template_with_items",
            return_value=payload,
        ) as call:
            result = self._backend().typed_template_with_items(5)
        call.assert_called_once()
        self.assertEqual(result, payload)

    def test_typed_template_with_items_returns_none_when_not_found(self):
        with mock.patch.object(
            remctl_host,
            "snapshot_template_with_items",
            return_value={"found": False},
        ):
            result = self._backend().typed_template_with_items(5)
        self.assertIsNone(result)

    def test_typed_template_with_items_raises_on_host_unavailable(self):
        with mock.patch.object(
            remctl_host,
            "snapshot_template_with_items",
            side_effect=remctl_host.HostUnavailable("down"),
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_template_with_items(5)

class TransientVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_transient_verify_test", "remctl")

    def test_typed_list_exists_by_name_direct_delegates_to_q_function(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=1) as q,
        ):
            result = self.remctl.DirectReadBackend().typed_list_exists_by_name("Groceries")
        q.assert_called_once_with(db, "Groceries")
        self.assertTrue(result)
        db.close.assert_called_once()

    def test_typed_list_exists_by_name_direct_returns_false_when_absent(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_list_exact_name_count", return_value=0),
        ):
            result = self.remctl.DirectReadBackend().typed_list_exists_by_name("NoSuchList")
        self.assertFalse(result)
        db.close.assert_called_once()

    def test_typed_group_exists_by_name_direct_delegates_to_q_function(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_group_exact_name_count", return_value=2) as q,
        ):
            result = self.remctl.DirectReadBackend().typed_group_exists_by_name("Work")
        q.assert_called_once_with(db, "Work")
        self.assertTrue(result)
        db.close.assert_called_once()

    def test_typed_location_alarm_exists_direct_delegates_to_inline_logic(self):
        db = mock.Mock()
        alarm_row = {
            "type": "location",
            "location": {"title": "Home", "latitude": 37.4, "longitude": -122.1},
        }
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value={"Z_PK": 5}),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
            mock.patch.object(self.remctl, "alarm_rows_to_json", return_value=[alarm_row]),
        ):
            result = self.remctl.DirectReadBackend().typed_location_alarm_exists(
                "REM-1", title="Home", latitude=37.4, longitude=-122.1
            )
        self.assertTrue(result)
        db.close.assert_called_once()

    def test_typed_location_alarm_exists_hosted_calls_host(self):
        backend = self.remctl.HostedReadBackend(socket_path="/Users/ninja-matt/Projects/remctl/fake.sock")
        with mock.patch.object(
            remctl_host,
            "snapshot_location_alarm",
            return_value={"found": True, "matches": True},
        ) as call:
            result = backend.typed_location_alarm_exists(
                "REM-1", title="Home", latitude=37.4, longitude=-122.1
            )
        call.assert_called_once()
        self.assertTrue(result)

    def test_typed_location_alarm_exists_hosted_returns_false_on_no_match(self):
        backend = self.remctl.HostedReadBackend(socket_path="/Users/ninja-matt/Projects/remctl/fake.sock")
        with mock.patch.object(
            remctl_host,
            "snapshot_location_alarm",
            return_value={"found": True, "matches": False},
        ):
            result = backend.typed_location_alarm_exists("REM-1")
        self.assertFalse(result)

    def test_typed_location_alarm_exists_hosted_returns_false_on_host_unavailable(self):
        from remctl_host import HostUnavailable
        backend = self.remctl.HostedReadBackend(socket_path="/Users/ninja-matt/Projects/remctl/fake.sock")
        with mock.patch.object(
            remctl_host,
            "snapshot_location_alarm",
            side_effect=HostUnavailable("down"),
        ):
            result = backend.typed_location_alarm_exists("REM-1")
        self.assertFalse(result)

    def test_typed_list_exists_by_name_hosted_uses_resolve_list(self):
        backend = self.remctl.HostedReadBackend(socket_path="/Users/ninja-matt/Projects/remctl/fake.sock")
        with mock.patch.object(backend, "resolve_list", return_value={"id": 7, "title": "Groceries"}) as resolve_list:
            result = backend.typed_list_exists_by_name("Groceries")
        resolve_list.assert_called_once_with(name="Groceries")
        self.assertTrue(result)

    def test_typed_group_exists_by_name_hosted_uses_group_resolution(self):
        backend = self.remctl.HostedReadBackend(socket_path="/Users/ninja-matt/Projects/remctl/fake.sock")
        with mock.patch.object(backend, "typed_group_ref", return_value={"id": 9, "title": "Work"}) as resolve_group:
            result = backend.typed_group_exists_by_name("Work")
        resolve_group.assert_called_once_with(name="Work")
        self.assertTrue(result)

    def test_location_alarm_exists_routes_through_active_backend(self):
        backend = mock.Mock()
        backend.typed_location_alarm_exists.return_value = True
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend):
            result = self.remctl.location_alarm_exists("REM-1", title="Home")
        backend.typed_location_alarm_exists.assert_called_once_with(
            "REM-1", title="Home", latitude=None, longitude=None
        )
        self.assertTrue(result)

    def test_private_create_list_call_verifies_via_backend_on_transient_error(self):
        transient_result = {"status": "error", "message": "communicate with a helper application"}
        backend = mock.Mock()
        backend.typed_list_exists_by_name.return_value = True
        with (
            mock.patch.object(self.remctl, "private_call", return_value=transient_result),
            mock.patch.object(self.remctl, "get_read_backend", return_value=backend),
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            result = self.remctl.private_create_list_call(
                {"action": "create_list", "name": "MyList"}, "MyList", attempts=1
            )
        self.assertEqual(result.get("status"), "created")
        self.assertTrue(result.get("verifiedAfterTransientError"))

    def test_private_create_list_call_does_not_recover_when_backend_says_absent(self):
        transient_result = {"status": "error", "message": "communicate with a helper application"}
        backend = mock.Mock()
        backend.typed_list_exists_by_name.return_value = False
        with (
            mock.patch.object(self.remctl, "private_call", return_value=transient_result),
            mock.patch.object(self.remctl, "get_read_backend", return_value=backend),
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            result = self.remctl.private_create_list_call(
                {"action": "create_list", "name": "MyList"}, "MyList", attempts=1
            )
        self.assertNotEqual(result.get("status"), "created")

    def test_private_create_group_call_verifies_via_backend_on_transient_error(self):
        transient_result = {"status": "error", "message": "communicate with a helper application"}
        backend = mock.Mock()
        backend.typed_group_exists_by_name.return_value = True
        with (
            mock.patch.object(self.remctl, "private_call", return_value=transient_result),
            mock.patch.object(self.remctl, "get_read_backend", return_value=backend),
            mock.patch.object(self.remctl.time, "sleep"),
        ):
            result = self.remctl.private_create_group_call(
                {"action": "create_group", "name": "Work"}, "Work", attempts=1
            )
        self.assertEqual(result.get("status"), "created")
        self.assertTrue(result.get("verifiedAfterTransientError"))

class HostedReadBackendErrorPropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_errors_test", "remctl")

    def _backend(self):
        return self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")

    def test_section_timeout_becomes_reminders_db_unavailable(self):
        with mock.patch.object(
            remctl_host,
            "resolve_section",
            side_effect=remctl_host.HostUnavailable("Capability Host is unavailable: timed out"),
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_resolve_section(7, section_name="Produce")

    def test_sharee_timeout_becomes_reminders_db_unavailable(self):
        with mock.patch.object(
            remctl_host,
            "resolve_sharee",
            side_effect=remctl_host.HostUnavailable("Capability Host is unavailable: timed out"),
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_resolve_sharee(7, "Alex")

    def test_snapshot_timeout_becomes_reminders_db_unavailable(self):
        with mock.patch.object(
            remctl_host,
            "snapshot_sections",
            side_effect=remctl_host.HostUnavailable("Capability Host is unavailable: timed out"),
        ):
            with self.assertRaises(self.remctl.RemindersDBUnavailable):
                self._backend().typed_snapshot_sections(7)

    def test_section_not_found_error_dict_exits_process(self):
        with mock.patch.object(
            remctl_host,
            "resolve_section",
            return_value={"error": "not_found", "message": "Error: section not found in target list: X"},
        ):
            with self.assertRaises(SystemExit) as caught:
                self._backend().typed_resolve_section(7, section_name="X")
        self.assertEqual(caught.exception.code, 1)

    def test_sharee_failed_error_dict_exits_process(self):
        with mock.patch.object(
            remctl_host,
            "resolve_sharee",
            return_value={"error": "failed", "message": "Error: no sharee matching 'X'."},
        ):
            with self.assertRaises(SystemExit) as caught:
                self._backend().typed_resolve_sharee(7, "X")
        self.assertEqual(caught.exception.code, 1)


# ── 5. Direct-vs-host parity ─────────────────────────────────────────────────

class DirectVsHostParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_parity_test", "remctl")

    def test_resolve_sharee_result_shapes_match(self):
        db = mock.Mock()
        row = {"Z_PK": 3, "ZCKIDENTIFIER": "SHAREE-1"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "resolve_sharee_or_die", return_value=row),
            mock.patch.object(self.remctl, "_sharee_display_name", return_value="Alex"),
        ):
            direct_result = self.remctl.DirectReadBackend().typed_resolve_sharee(7, "Alex")

        with mock.patch.object(
            remctl_host,
            "resolve_sharee",
            return_value={"id": 3, "ZCKIDENTIFIER": "SHAREE-1", "cloudId": "SHAREE-1", "name": "Alex"},
        ):
            hosted_result = self.remctl.HostedReadBackend(
                socket_path="/tmp/does-not-matter.sock"
            ).typed_resolve_sharee(7, "Alex")

        self.assertEqual(set(direct_result), set(hosted_result))
        self.assertEqual(direct_result["ZCKIDENTIFIER"], hosted_result["ZCKIDENTIFIER"])
        self.assertEqual(direct_result["cloudId"], hosted_result["cloudId"])

    def test_snapshot_sections_result_shapes_match(self):
        rows = [{"Z_PK": 1, "ZDISPLAYNAME": "Produce", "ZCKIDENTIFIER": "SEC-1"}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=mock.Mock()),
            mock.patch.object(self.remctl, "q_sections", return_value=rows),
        ):
            direct_result = self.remctl.DirectReadBackend().typed_snapshot_sections(7)

        with mock.patch.object(
            remctl_host,
            "snapshot_sections",
            return_value={
                "sections": [{"id": 1, "name": "Produce", "cloudId": "SEC-1"}],
                "memberships": {},
            },
        ):
            hosted_result = self.remctl.HostedReadBackend(
                socket_path="/tmp/does-not-matter.sock"
            ).typed_snapshot_sections(7)

        self.assertEqual(direct_result, hosted_result)
        for section in direct_result + hosted_result:
            self.assertEqual(set(section), {"id", "name", "cloudId"})

    def test_resolve_section_returns_same_cloud_id_type(self):
        db = mock.Mock()
        with (
            mock.patch.object(self.remctl, "open_db", return_value=db),
            mock.patch.object(self.remctl, "resolve_section_ckid", return_value="SEC-1"),
        ):
            direct_result = self.remctl.DirectReadBackend().typed_resolve_section(
                7, section_name="Produce"
            )

        with mock.patch.object(
            remctl_host,
            "resolve_section",
            return_value={"cloudId": "SEC-1", "name": "Produce", "id": 1},
        ):
            hosted_result = self.remctl.HostedReadBackend(
                socket_path="/tmp/does-not-matter.sock"
            ).typed_resolve_section(7, section_name="Produce")

        self.assertEqual(direct_result, hosted_result)
        self.assertIsInstance(direct_result, str)
        self.assertIsInstance(hosted_result, str)

    def test_smart_list_sections_by_pk_result_shapes_match(self):
        rows = [{"Z_PK": 1, "ZDISPLAYNAME": "Produce", "ZCKIDENTIFIER": "SEC-1"}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=mock.Mock()),
            mock.patch.object(self.remctl, "q_smart_list_sections", return_value=rows),
        ):
            direct_result = self.remctl.DirectReadBackend().typed_smart_list_sections_by_pk(7)

        with mock.patch.object(
            remctl_host,
            "snapshot_smart_list_sections_by_pk",
            return_value={"sections": [{"id": 1, "name": "Produce", "cloudId": "SEC-1"}], "count": 1},
        ):
            hosted_result = self.remctl.HostedReadBackend(
                socket_path="/tmp/does-not-matter.sock"
            ).typed_smart_list_sections_by_pk(7)

        self.assertEqual(direct_result, hosted_result)
        for section in direct_result + hosted_result:
            self.assertEqual(set(section), {"id", "name", "cloudId"})


# ── 6. Routing integration tests ─────────────────────────────────────────────

class RoutingIntegrationTests(unittest.TestCase):
    """Verify validate_private_add_ready/apply_private_changes call the
    typed backend API rather than any concrete backend's internals, so the
    same code path works for either DirectReadBackend or HostedReadBackend."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_routing_test", "remctl")
        cls._default_protocol_probe = mock.patch.object(
            cls.remctl,
            "_probe_private_protocol_version",
            return_value={"ok": True, "version": 2},
        )
        cls._default_protocol_probe.start()

    @classmethod
    def tearDownClass(cls):
        cls._default_protocol_probe.stop()

    def _args(self, **overrides):
        from types import SimpleNamespace
        base = dict(
            private=True,
            private_metadata=False,
            tags=None,
            url=None,
            section="Produce",
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
            assign=None,
            unassign=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_validate_private_add_ready_uses_typed_resolve_section(self):
        backend = mock.Mock(spec=self.remctl.ReadBackend)
        args = self._args(section="Produce", section_id=None, assign=None)
        self.remctl.validate_private_add_ready(args, backend, 7)
        backend.typed_resolve_section.assert_called_once_with(
            7, section_name="Produce", section_id=None
        )
        backend.typed_resolve_sharee.assert_not_called()

    def test_validate_private_add_ready_uses_typed_resolve_sharee_for_assign(self):
        backend = mock.Mock(spec=self.remctl.ReadBackend)
        args = self._args(section=None, assign="Alex")
        self.remctl.validate_private_add_ready(args, backend, 7)
        self.assertEqual(
            backend.typed_resolve_sharee.call_args_list,
            [mock.call(7, "Alex"), mock.call(7, "me")],
        )

    def test_apply_private_changes_section_routes_through_backend(self):
        backend = mock.Mock(spec=self.remctl.ReadBackend)
        backend.typed_resolve_section.return_value = "SEC-1"
        args = self._args(section="Produce", assign=None)
        with mock.patch.object(
            self.remctl, "private_action", return_value={"status": "updated"}
        ) as private_action:
            self.remctl.apply_private_changes("REM-1", args, backend=backend, list_pk=7)
        backend.typed_resolve_section.assert_called_once_with(
            7, section_name="Produce", section_id=None
        )
        private_action.assert_called_once_with({
            "action": "assign_section",
            "id": "REM-1",
            "sectionId": "SEC-1",
        }, partial_context=None)

    def test_apply_private_changes_assign_routes_through_backend(self):
        backend = mock.Mock(spec=self.remctl.ReadBackend)
        backend.typed_resolve_sharee.side_effect = [
            {"ZCKIDENTIFIER": "ASSIGNEE-1"},
            {"ZCKIDENTIFIER": "OWNER-1"},
        ]
        args = self._args(section=None, assign="Alex")
        with mock.patch.object(
            self.remctl, "private_action", return_value={"status": "updated"}
        ) as private_action:
            self.remctl.apply_private_changes("REM-1", args, backend=backend, list_pk=7)
        self.assertEqual(
            backend.typed_resolve_sharee.call_args_list,
            [mock.call(7, "Alex"), mock.call(7, "me")],
        )
        payload = private_action.call_args.args[0]
        self.assertEqual(payload["assigneeId"], "ASSIGNEE-1")
        self.assertEqual(payload["originatorId"], "OWNER-1")

    def test_apply_private_changes_grocery_routes_through_backend(self):
        backend = mock.Mock(spec=self.remctl.ReadBackend)
        backend.typed_list_ckid.return_value = "LIST-CKID"
        args = self._args(section=None, assign=None, grocery=True)
        with (
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl, "wait_for_grocery_section", return_value="Produce"
            ) as wait_for_section,
        ):
            results = self.remctl.apply_private_changes(
                "REM-1", args, backend=backend, list_pk=7
            )
        backend.typed_require_grocery_target.assert_called_once_with(7)
        backend.typed_list_ckid.assert_called_once_with(7)
        wait_for_section.assert_called_with(7, "REM-1", backend=backend)
        self.assertEqual(results[0]["status"], "updated")
        self.assertEqual(results[0]["source"], "reminders_auto")

    def test_apply_private_changes_defaults_backend_to_active_read_backend(self):
        fake_backend = mock.Mock(spec=self.remctl.ReadBackend)
        fake_backend.typed_resolve_section.return_value = "SEC-1"
        args = self._args(section="Produce", assign=None)
        with (
            mock.patch.object(self.remctl, "get_read_backend", return_value=fake_backend),
            mock.patch.object(
                self.remctl, "private_action", return_value={"status": "updated"}
            ),
        ):
            self.remctl.apply_private_changes("REM-1", args, list_pk=7)
        fake_backend.typed_resolve_section.assert_called_once_with(
            7, section_name="Produce", section_id=None
        )

    def test_cmd_add_dispatches_to_hosted_backend_for_private_section(self):
        # End-to-end: cmd_add's private/section path must call
        # HostedReadBackend.typed_resolve_section (via the host client),
        # never open_db, when the Capability Host route is active.
        from types import SimpleNamespace

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
            grocery=False,
            section="Produce",
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
            assign=None,
            unassign=False,
            json=True,
        )
        list_resolution = {
            "id": 7,
            "title": "Groceries",
            "objectUUID": "LIST-CKID",
            "method": "exact",
        }
        bridge_result = {
            "returncode": 0,
            "stdout": "{\"status\": \"created\", \"id\": \"REM-1\"}",
            "stderr": "",
            "payload": {"status": "created", "id": "REM-1"},
        }
        hosted_backend = self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with (
            mock.patch.object(self.remctl, "get_read_backend", return_value=hosted_backend),
            mock.patch.object(hosted_backend, "resolve_list", return_value=list_resolution),
            mock.patch.object(
                hosted_backend, "resolve_reminder_by_ckid", return_value=None
            ),
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            mock.patch.object(self.remctl, "bridge_call_result", return_value=bridge_result),
            mock.patch.object(self.remctl, "private_available", return_value=True),
            mock.patch.object(
                self.remctl, "private_action", return_value={"status": "updated"}
            ),
            mock.patch.object(
                remctl_host,
                "resolve_section",
                return_value={"cloudId": "SEC-1", "name": "Produce", "id": 5},
            ) as host_resolve_section,
            mock.patch.object(self.remctl, "open_db") as open_db,
            mock.patch("contextlib.redirect_stdout"),
        ):
            self.remctl.cmd_add(args)
        host_resolve_section.assert_called()
        open_db.assert_not_called()


class HostedCommandIntegrationTests(unittest.TestCase):
    """End-to-end command routing: when the active read backend is not a
    DirectReadBackend (e.g. a Mock standing in for HostedReadBackend), each
    migrated read command must render from the typed backend API and must not
    touch open_db()."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_command_integration_test", "remctl")

    def _capture(self, fn, *args, **kwargs):
        import io
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn(*args, **kwargs)
        return out.getvalue(), err.getvalue()

    def _backend(self):
        return mock.Mock(spec=self.remctl.ReadBackend)

    def _args(self, **overrides):
        from types import SimpleNamespace
        base = dict(json=False, format=None, verbose=False)
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_cmd_search_routes_through_typed_query(self):
        from types import SimpleNamespace
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [{"id": 1, "title": "Buy milk", "list": "Groceries",
                           "completed": False, "flagged": False, "priority": "none"}],
            "count": 1, "truncated": False,
        }
        args = self._args(query="milk", completed=None)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "eventkit_read_requested", return_value=False), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_search, args)
        backend.typed_reminders_query.assert_called_once_with(query="milk", completed=None)
        open_db.assert_not_called()
        self.assertIn("Buy milk", out)

    def test_cmd_flagged_routes_through_typed_query(self):
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [], "count": 0, "truncated": False}
        args = self._args()
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            self._capture(self.remctl.cmd_flagged, args)
        backend.typed_reminders_query.assert_called_once_with(flagged=True)
        open_db.assert_not_called()

    def test_cmd_urgent_routes_through_typed_query(self):
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [], "count": 0, "truncated": False}
        args = self._args()
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            self._capture(self.remctl.cmd_urgent, args)
        backend.typed_reminders_query.assert_called_once_with(urgent=True)
        open_db.assert_not_called()

    def test_cmd_today_routes_through_typed_query(self):
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [], "count": 0, "truncated": False}
        args = self._args(no_overdue=False)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "eventkit_read_requested", return_value=False), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            self._capture(self.remctl.cmd_today, args)
        backend.typed_reminders_query.assert_called_once_with(include_overdue=True)
        open_db.assert_not_called()

    def test_cmd_upcoming_routes_through_typed_query(self):
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [], "count": 0, "truncated": False}
        args = self._args(days=7)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "eventkit_read_requested", return_value=False), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            self._capture(self.remctl.cmd_upcoming, args)
        backend.typed_reminders_query.assert_called_once_with(days_ahead=7)
        open_db.assert_not_called()

    def test_cmd_overdue_routes_through_typed_query(self):
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [], "count": 0, "truncated": False}
        args = self._args()
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            self._capture(self.remctl.cmd_overdue, args)
        backend.typed_reminders_query.assert_called_once_with(overdue=True)
        open_db.assert_not_called()

    def test_cmd_tags_routes_through_typed_tags(self):
        backend = self._backend()
        backend.typed_tags.return_value = {"tags": [{"name": "home"}, {"name": "work"}]}
        args = self._args()
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_tags, args)
        backend.typed_tags.assert_called_once_with()
        open_db.assert_not_called()
        self.assertIn("home", out)

    def test_cmd_stats_routes_through_typed_stats(self):
        backend = self._backend()
        backend.typed_stats.return_value = {
            "total": 10, "active": 6, "completed": 4, "overdue": 1,
            "flagged": 2, "urgent": 0, "lists": 3, "sections": 1}
        args = self._args()
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_stats, args)
        backend.typed_stats.assert_called_once_with()
        open_db.assert_not_called()
        self.assertIn("Total", out)

    def test_cmd_sections_routes_through_typed_all_sections(self):
        backend = self._backend()
        backend.typed_all_sections.return_value = {
            "sections": [{"id": 1, "name": "Produce", "listId": 7, "listName": "Groceries"}],
            "count": 1}
        args = self._args()
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_sections, args)
        backend.typed_all_sections.assert_called_once_with()
        open_db.assert_not_called()
        self.assertIn("Produce", out)

    def test_cmd_info_found_routes_through_typed_detail(self):
        backend = self._backend()
        backend.typed_reminder_detail.return_value = {
            "id": 5, "title": "Pay rent", "list": "Bills", "completed": False,
            "flagged": False, "urgent": False, "priority": "none",
            "found": True,
        }
        args = self._args(id=5)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_info, args)
        backend.typed_reminder_detail.assert_called_once_with(5)
        open_db.assert_not_called()
        self.assertIn("Pay rent", out)

    def test_cmd_info_not_found_exits(self):
        backend = self._backend()
        backend.typed_reminder_detail.return_value = None
        args = self._args(id=999)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db"):
            with self.assertRaises(SystemExit):
                self._capture(self.remctl.cmd_info, args)
        backend.typed_reminder_detail.assert_called_once_with(999)

    def test_cmd_sharees_routes_through_typed_sharees(self):
        backend = self._backend()
        backend.resolve_list.return_value = {"id": 7, "title": "Groceries"}
        backend.typed_sharees.return_value = {
            "sharees": [{"id": 1, "name": "Alex", "address": "alex@example.com",
                         "currentUser": False}],
            "currentUserSharee": "OWNER-1",
        }
        args = self._args(list="Groceries", list_id=None)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_sharees, args)
        backend.typed_sharees.assert_called_once_with(7)
        open_db.assert_not_called()
        self.assertIn("Alex", out)

    def test_cmd_subtasks_routes_through_typed(self):
        backend = self._backend()
        backend.typed_reminder_by_pk.return_value = {"id": 3, "title": "Parent task"}
        backend.typed_reminders_query.return_value = {
            "reminders": [{"id": 4, "title": "Child", "list": "L", "completed": False,
                           "flagged": False, "priority": "none"}],
            "count": 1, "truncated": False,
        }
        args = self._args(id=3)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_subtasks, args)
        backend.typed_reminder_by_pk.assert_called_once_with(3)
        backend.typed_reminders_query.assert_called_once_with(parent_pk=3, completed=True)
        open_db.assert_not_called()
        self.assertIn("Child", out)

    def test_cmd_export_routes_through_typed_query(self):
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [{"id": 1, "title": "Done thing", "list": "L", "completed": True,
                           "flagged": False, "priority": "none"}],
            "count": 1, "truncated": False,
        }
        args = self._args(list=None, list_id=None, export_format="json")
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_export, args)
        backend.typed_reminders_query.assert_called_once_with(
            completed=True, top_level=False, limit=10000)
        open_db.assert_not_called()
        self.assertIn("Done thing", out)

    def test_cmd_show_routes_through_typed_query(self):
        backend = self._backend()
        backend.resolve_list.return_value = {"id": 7, "title": "Groceries"}
        backend.typed_reminders_query.return_value = {
            "reminders": [{"id": 1, "title": "Milk", "list": "Groceries", "completed": False,
                           "flagged": False, "priority": "none", "section": None}],
            "count": 1, "truncated": False,
        }
        backend.typed_snapshot_sections.return_value = []
        args = self._args(list="Groceries", list_id=None, completed=False)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "eventkit_read_requested", return_value=False), \
             mock.patch.object(self.remctl, "open_db") as open_db:
            out, _ = self._capture(self.remctl.cmd_show, args)
        backend.typed_reminders_query.assert_called_once_with(
            list_pk=7, completed=False, top_level=True)
        open_db.assert_not_called()
        self.assertIn("Milk", out)


class CapabilityHostReminderQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_query_tests", "remctl")

    def test_protocol_includes_new_snapshot_operations_and_fields(self):
        self.assertIn("snapshot.allListSectionCounts", protocol.OPERATIONS)
        self.assertIn("snapshot.groupReminders", protocol.OPERATIONS)
        self.assertIn("snapshot.allListSectionCounts", protocol.IMPLEMENTED_OPERATIONS)
        self.assertIn("snapshot.groupReminders", protocol.IMPLEMENTED_OPERATIONS)
        self.assertIn("manualOrder", protocol.OPERATIONS["snapshot.reminders"].fields)
        self.assertIn("limit", protocol.OPERATIONS["snapshot.groupReminders"].fields)

    def test_hosted_backend_sends_explicit_top_level_and_manual_order_for_list_queries(self):
        backend = self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with mock.patch.object(remctl_host, "snapshot_reminders", return_value={"reminders": [], "count": 0, "truncated": False}) as call:
            backend.typed_reminders_query(list_pk=2, completed=False)
        call.assert_called_once_with(
            self.remctl.Path("/tmp/does-not-matter.sock"),
            listId=2,
            completed=False,
            includeOverdue=True,
            topLevel=True,
            manualOrder=True,
        )

    def test_hosted_backend_sends_explicit_top_level_for_non_list_q_reminders_queries(self):
        backend = self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with mock.patch.object(remctl_host, "snapshot_reminders", return_value={"reminders": [], "count": 0, "truncated": False}) as call:
            backend.typed_reminders_query(completed=True)
        call.assert_called_once_with(
            self.remctl.Path("/tmp/does-not-matter.sock"),
            completed=True,
            includeOverdue=True,
            topLevel=True,
            manualOrder=False,
        )

    def test_hosted_backend_parent_query_forces_top_level_false(self):
        backend = self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        with mock.patch.object(remctl_host, "snapshot_reminders", return_value={"reminders": [], "count": 0, "truncated": False}) as call:
            backend.typed_reminders_query(parent_pk=101, completed=True)
        call.assert_called_once_with(
            self.remctl.Path("/tmp/does-not-matter.sock"),
            completed=True,
            includeOverdue=True,
            parentPk=101,
            topLevel=False,
            manualOrder=False,
        )

    def test_hosted_backend_forwards_group_reminder_limit(self):
        backend = self.remctl.HostedReadBackend(socket_path="/tmp/does-not-matter.sock")
        payload = {"lists": [], "reminders": [], "count": 0, "truncated": False}
        with mock.patch.object(remctl_host, "snapshot_group_reminders", return_value=payload) as call:
            result = backend.typed_group_reminders(1, completed=False, top_level=False, limit=25)
        self.assertEqual(result, payload)
        call.assert_called_once_with(
            self.remctl.Path("/tmp/does-not-matter.sock"),
            1,
            completed=False,
            top_level=False,
            limit=25,
        )


class RealSQLiteHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_real_sqlite", "remctl")

    def setUp(self):
        self._keeper, self._open_db = _make_test_db()
        self._open_patch = mock.patch.object(self.remctl, "open_db", side_effect=self._open_db)
        self._open_patch.start()
        self.handler = ReadOperations(self.remctl)

    def tearDown(self):
        self._open_patch.stop()
        self._keeper.close()

    def _ids(self, payload):
        return [row["id"] for row in payload["reminders"]]

    def test_snapshot_reminders_variants_use_real_sqlite(self):
        cases = [
            ({"query": "bolts"}, [102]),
            ({"flagged": True}, [101]),
            ({"urgent": True}, [102]),
            ({"overdue": True}, [103]),
            ({"daysAhead": 3}, [101, 102]),
            ({"listId": 2}, [101]),
        ]
        for fields, expected_ids in cases:
            with self.subTest(fields=fields):
                payload = self.handler.snapshot_reminders(request("snapshot.reminders", **fields))
                self.assertEqual(self._ids(payload), expected_ids)
                self.assertFalse(payload["truncated"])

    def test_snapshot_reminders_completed_top_level_false_matches_direct_and_skips_due_today(self):
        hosted = self.handler.snapshot_reminders(
            request("snapshot.reminders", completed=True, topLevel=False)
        )
        direct = self.remctl.DirectReadBackend().typed_reminders_query(
            completed=True,
            top_level=False,
        )
        self.assertEqual(self._ids(hosted), [101, 102, 103, 104, 105, 106])
        self.assertEqual(hosted, direct)

    def test_snapshot_reminders_default_top_level_matches_direct_for_list_and_non_list(self):
        hosted_list = self.handler.snapshot_reminders(request("snapshot.reminders", listId=2))
        direct_list = self.remctl.DirectReadBackend().typed_reminders_query(list_pk=2)
        self.assertEqual(self._ids(hosted_list), [101])
        self.assertEqual(hosted_list, direct_list)

        hosted_non_list = self.handler.snapshot_reminders(request("snapshot.reminders", completed=True))
        direct_non_list = self.remctl.DirectReadBackend().typed_reminders_query(completed=True)
        self.assertEqual(self._ids(hosted_non_list), [101, 102, 103, 106])
        self.assertEqual(hosted_non_list, direct_non_list)

    def test_snapshot_reminders_parent_query_returns_children(self):
        payload = self.handler.snapshot_reminders(
            request("snapshot.reminders", parentPk=101, completed=True, topLevel=False)
        )
        self.assertEqual(self._ids(payload), [104, 105])

    def test_snapshot_reminder_detail_includes_parent_title_and_attachment_metadata(self):
        payload = self.handler.snapshot_reminder_detail(
            request("snapshot.reminderDetail", identifier=105)
        )
        self.assertTrue(payload["found"])
        self.assertEqual(payload["parentTitle"], "Milk")
        self.assertEqual(payload["attachments"][0]["filename"], "child.png")
        self.assertEqual(payload["attachments"][0]["mimeType"], "image/png")
        self.assertEqual(payload["attachments"][0]["size"], 1234)
        self.assertFalse(payload["attachments"][0]["inlineData"])

    def test_snapshot_reminder_detail_not_found(self):
        self.assertEqual(
            self.handler.snapshot_reminder_detail(request("snapshot.reminderDetail", identifier=9999)),
            {"found": False},
        )

    def test_snapshot_stats_tags_sections_sharees_and_smart_lists(self):
        stats = self.handler.snapshot_stats(request("snapshot.stats"))
        self.assertEqual(stats["total"], 6)
        self.assertEqual(stats["active"], 4)
        self.assertEqual(stats["completed"], 2)
        self.assertEqual(stats["overdue"], 1)
        self.assertEqual(stats["flagged"], 1)
        self.assertEqual(stats["urgent"], 1)
        self.assertEqual(stats["lists"], 3)
        self.assertEqual(stats["sections"], 4)

        tags = self.handler.snapshot_tags(request("snapshot.tags"))
        self.assertEqual(tags["tags"], [{"name": "groceries"}, {"name": "urgent"}])

        all_sections = self.handler.snapshot_all_sections(request("snapshot.allSections"))
        self.assertEqual(all_sections["count"], 4)
        self.assertIn({"id": 201, "name": "Produce", "listId": 2, "listName": "Groceries"}, all_sections["sections"])

        sections = self.handler.snapshot_sections(request("snapshot.sections", listId=2))
        self.assertEqual([section["id"] for section in sections["sections"]], [201, 202])
        self.assertEqual(sections["memberships"]["REM-101"], "Produce")

        sharees = self.handler.snapshot_sharees(request("snapshot.sharees", listId=2))
        self.assertEqual(sharees["currentUserSharee"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual([sharee["name"] for sharee in sharees["sharees"]], ["Matt", "Alex"])

        smart_list = self.handler.snapshot_smart_list(request("snapshot.smartList", identifier=50))
        self.assertEqual(smart_list["id"], 50)
        self.assertEqual(smart_list["name"], "Focus")

    def test_snapshot_all_list_section_counts_and_group_reminders(self):
        counts = self.handler.snapshot_all_list_section_counts(
            request("snapshot.allListSectionCounts")
        )
        self.assertEqual(counts, {"sectionCounts": {"2": 2, "3": 1}})

        direct_counts = self.remctl.DirectReadBackend().typed_all_list_section_counts()
        self.assertEqual(direct_counts, counts)

        group_payload = self.handler.snapshot_group_reminders(
            request("snapshot.groupReminders", groupId=1)
        )
        self.assertEqual([row["id"] for row in group_payload["lists"]], [2, 3])
        self.assertEqual(self._ids(group_payload), [101, 102])
        self.assertFalse(group_payload["truncated"])

    def test_bounded_pagination_honors_limit_and_reports_truncation(self):
        self._keeper.execute(
            "INSERT INTO ZREMCDBASELIST (Z_PK, Z_ENT, ZNAME, ZCKIDENTIFIER, ZISGROUP) VALUES (9, 3, 'Bulk', 'LIST-9', 0)"
        )
        rows = [
            (2000 + idx, f"Bulk {idx:03d}", None, 0, 0, 0, 0, None, None, 0, None, idx, None, 9, None, f"BULK-{idx:03d}", 1, 0)
            for idx in range(501)
        ]
        self._keeper.executemany(
            "INSERT INTO ZREMCDREMINDER (Z_PK, ZTITLE, ZNOTES, ZCOMPLETED, ZFLAGGED, ZPRIORITY, ZISURGENTSTATEENABLEDFORCURRENTUSER, ZDUEDATE, ZDISPLAYDATEDATE, ZALLDAY, ZCOMPLETIONDATE, ZCREATIONDATE, ZPARENTREMINDER, ZLIST, ZICSURL, ZCKIDENTIFIER, ZACCOUNT, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._keeper.commit()

        default_payload = self.remctl.DirectReadBackend().typed_reminders_query(list_pk=9)
        self.assertEqual(default_payload["count"], 500)
        self.assertTrue(default_payload["truncated"])

        export_payload = self.remctl.DirectReadBackend().typed_reminders_query(
            list_pk=9,
            completed=True,
            top_level=False,
            limit=10000,
        )
        self.assertEqual(export_payload["count"], 501)
        self.assertFalse(export_payload["truncated"])


class HostedReminderCommandRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_hosted_private_command_regressions", "remctl")

    def _capture(self, fn, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn(*args, **kwargs)
        return out.getvalue(), err.getvalue()

    def _backend(self):
        return mock.Mock(spec=self.remctl.ReadBackend)

    def _args(self, **overrides):
        base = dict(
            json=False,
            format=None,
            verbose=False,
            list=None,
            list_id=None,
            completed=False,
            subtasks=False,
            sort=None,
            export_format="json",
            limit=None,
            no_overdue=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_cmd_lists_uses_single_all_list_section_count_call(self):
        backend = self._backend()
        backend.typed_all_lists.return_value = [
            {"id": 1, "title": "Errands Group", "isGroup": True},
            {"id": 2, "title": "Groceries", "parentListId": 1},
            {"id": 3, "title": "Hardware", "parentListId": 1},
            {"id": 4, "title": "Work"},
        ]
        backend.typed_group_ref.return_value = {
            "id": 1,
            "title": "Errands Group",
            "children": [
                {"id": 2, "title": "Groceries", "parentListId": 1},
                {"id": 3, "title": "Hardware", "parentListId": 1},
            ],
        }
        backend.typed_all_list_section_counts.return_value = {"sectionCounts": {"2": 2, "3": 1}}
        backend.typed_snapshot_sections.side_effect = AssertionError("should not fetch per-list sections")
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend):
            out, _ = self._capture(self.remctl.cmd_lists, self._args())
        backend.typed_all_list_section_counts.assert_called_once_with()
        self.assertIn("Groceries", out)
        self.assertIn("Hardware", out)

    def test_cmd_show_group_routes_through_typed_group_reminders(self):
        backend = self._backend()
        backend.resolve_list.return_value = {"id": 1, "title": "Errands Group", "isGroup": True}
        backend.typed_group_reminders.return_value = {
            "lists": [
                {"id": 2, "title": "Groceries"},
                {"id": 3, "title": "Hardware"},
            ],
            "reminders": [
                {"id": 101, "title": "Milk", "list": "Groceries", "listId": 2, "completed": False, "flagged": False, "priority": "none", "section": "Produce"},
                {"id": 102, "title": "Buy bolts", "list": "Hardware", "listId": 3, "completed": False, "flagged": False, "priority": "none", "section": None},
            ],
            "count": 2,
            "truncated": False,
        }
        backend.typed_snapshot_sections.side_effect = [
            [{"id": 201, "name": "Produce", "cloudId": "SEC-201"}],
            [],
        ]
        args = self._args(list="Errands Group", completed=False)
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend),              mock.patch.object(self.remctl, "eventkit_read_requested", return_value=False),              mock.patch.object(self.remctl, "open_db") as open_db:
            out, err = self._capture(self.remctl.cmd_show, args)
        open_db.assert_not_called()
        backend.typed_group_reminders.assert_called_once_with(
            group_id=1, completed=False, top_level=True
        )
        self.assertEqual(err, "")
        self.assertIn("Milk", out)
        self.assertIn("Buy bolts", out)

    def test_cmd_export_fails_loudly_when_results_are_truncated(self):
        backend = self._backend()
        backend.typed_reminders_query.return_value = {
            "reminders": [{"id": 1, "title": "Done thing", "list": "L", "completed": True, "flagged": False, "priority": "none"}],
            "count": 1,
            "truncated": True,
        }
        args = self._args(export_format="json")
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend),              mock.patch.object(self.remctl, "open_db"):
            with self.assertRaises(SystemExit):
                self._capture(self.remctl.cmd_export, args)


class N2ProtocolRejectionTests(unittest.TestCase):
    """N2: dueBefore/dueAfter removed from schema — must be rejected."""

    def _req(self, **kw):
        return request("snapshot.reminders", **kw)

    def test_dueBefore_rejected(self):
        with self.assertRaises(protocol.ProtocolError) as cm:
            protocol.validate_request(self._req(dueBefore="2026-01-01"))
        self.assertIn("dueBefore", str(cm.exception))

    def test_dueAfter_rejected(self):
        with self.assertRaises(protocol.ProtocolError) as cm:
            protocol.validate_request(self._req(dueAfter="2026-01-01"))
        self.assertIn("dueAfter", str(cm.exception))

    def test_includeOverdue_accepted(self):
        """includeOverdue is NOT removed — it is actually used by q_due_today."""
        # Should not raise
        protocol.validate_request(self._req(includeOverdue=True))

    def test_all_remaining_schema_fields_accepted(self):
        """Every field still in schema must be accepted without raising."""
        valid_fields = [
            {"listId": 1},
            {"completed": True},
            {"flagged": True},
            {"urgent": True},
            {"query": "milk"},
            {"daysAhead": 7},
            {"includeOverdue": False},
            {"overdue": True},
            {"topLevel": True},
            {"parentPk": 99},
            {"manualOrder": True},
            {"limit": 100},
        ]
        for f in valid_fields:
            with self.subTest(field=list(f.keys())[0]):
                # Should not raise
                protocol.validate_request(self._req(**f))

    def test_unknown_field_rejected(self):
        """Default-deny: unknown fields raise ProtocolError."""
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(self._req(unknownGarbage=True))


class N1TruncationWarningTests(unittest.TestCase):
    """N1: truncated flag must not be swallowed; JSON keeps bare array;
    stderr receives the warning; correct per-branch limit defaults."""

    remctl = load_module("remctl_n1_truncation", "remctl")

    def _backend(self, items=None, truncated=False, limit=500):
        # Use MagicMock (not spec'd to DirectReadBackend) so isinstance checks
        # naturally return False and commands take the hosted code path.
        backend = mock.MagicMock()
        items = items or []
        backend.typed_reminders_query.return_value = {
            "reminders": items,
            "count": len(items),
            "truncated": truncated,
            "limit": limit,
        }
        return backend

    def _args(self, **kw):
        ns = SimpleNamespace(
            json=False, verbose=False, format=None,
            no_overdue=False, days=7, completed=None,
            query="x",
        )
        ns.__dict__.update(kw)
        return ns

    def _capture_both(self, fn, args):
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                fn(args)
            except SystemExit:
                pass
        return out_buf.getvalue(), err_buf.getvalue()

    def _run_cmd(self, fn, args, backend):
        # MagicMock is not a DirectReadBackend, so isinstance check is False naturally.
        with mock.patch.object(self.remctl, "get_read_backend", return_value=backend), \
             mock.patch.object(self.remctl, "eventkit_read_requested", return_value=False):
            return self._capture_both(fn, args)

    def test_flagged_human_truncated_warns_stderr(self):
        items = [{"id": i, "title": f"R{i}", "list": "L", "completed": False,
                  "flagged": True, "priority": "none"} for i in range(3)]
        backend = self._backend(items=items, truncated=True, limit=10000)
        out, err = self._run_cmd(self.remctl.cmd_flagged, self._args(), backend)
        self.assertIn("truncat", err.lower())
        self.assertIn("R0", out)

    def test_flagged_json_bare_array_truncated_warns_stderr(self):
        items = [{"id": 1, "title": "R1", "list": "L", "completed": False,
                  "flagged": True, "priority": "none"}]
        backend = self._backend(items=items, truncated=True, limit=10000)
        out, err = self._run_cmd(self.remctl.cmd_flagged, self._args(json=True), backend)
        # JSON output must remain a bare array, not an object
        self.assertIsInstance(json.loads(out), list)
        self.assertIn("truncat", err.lower())

    def test_urgent_json_bare_array_truncated_warns_stderr(self):
        items = [{"id": 1, "title": "U1", "list": "L", "completed": False,
                  "flagged": False, "priority": "none"}]
        backend = self._backend(items=items, truncated=True, limit=10000)
        out, err = self._run_cmd(self.remctl.cmd_urgent, self._args(json=True), backend)
        self.assertIsInstance(json.loads(out), list)
        self.assertIn("truncat", err.lower())

    def test_overdue_json_bare_array_truncated_warns_stderr(self):
        items = [{"id": 1, "title": "O1", "list": "L", "completed": False,
                  "flagged": False, "priority": "none"}]
        backend = self._backend(items=items, truncated=True, limit=10000)
        out, err = self._run_cmd(self.remctl.cmd_overdue, self._args(json=True), backend)
        self.assertIsInstance(json.loads(out), list)
        self.assertIn("truncat", err.lower())

    def test_upcoming_json_bare_array_truncated_warns_stderr(self):
        items = [{"id": 1, "title": "U1", "list": "L", "completed": False,
                  "flagged": False, "priority": "none",
                  "dueDate": "2026-09-01T10:00:00"}]
        backend = self._backend(items=items, truncated=True, limit=10000)
        out, err = self._run_cmd(self.remctl.cmd_upcoming, self._args(json=True), backend)
        self.assertIsInstance(json.loads(out), list)
        self.assertIn("truncat", err.lower())

    def test_today_json_bare_array_truncated_warns_stderr(self):
        items = [{"id": 1, "title": "T1", "list": "L", "completed": False,
                  "flagged": False, "priority": "none"}]
        backend = self._backend(items=items, truncated=True, limit=10000)
        out, err = self._run_cmd(self.remctl.cmd_today, self._args(json=True), backend)
        self.assertIsInstance(json.loads(out), list)
        self.assertIn("truncat", err.lower())

    def test_search_json_bare_array_truncated_warns_stderr(self):
        items = [{"id": 1, "title": "milk", "list": "L", "completed": False,
                  "flagged": False, "priority": "none"}]
        backend = self._backend(items=items, truncated=True, limit=100)
        out, err = self._run_cmd(self.remctl.cmd_search, self._args(json=True), backend)
        self.assertIsInstance(json.loads(out), list)
        self.assertIn("truncat", err.lower())
        self.assertIn("100", err)  # warns with correct search limit

    def test_no_truncation_no_stderr_warning(self):
        items = [{"id": 1, "title": "R1", "list": "L", "completed": False,
                  "flagged": True, "priority": "none"}]
        backend = self._backend(items=items, truncated=False, limit=10000)
        out, err = self._run_cmd(self.remctl.cmd_flagged, self._args(json=True), backend)
        self.assertIsInstance(json.loads(out), list)
        self.assertEqual(err, "")


class N1LimitDefaultTests(unittest.TestCase):
    """N1: verify branch-specific limit defaults in reminder_query_snapshot.

    Uses mock.patch to avoid requiring a full-schema in-memory SQLite DB;
    each test verifies the limit argument forwarded to the appropriate q_*
    function and the ``limit`` key in the returned payload.
    """

    remctl = load_module("remctl_n1_limit_defaults", "remctl")

    def _ctx(self, patched_q, return_value=([], False)):
        """Return a context that patches ``patched_q`` on the module."""
        return mock.patch.object(self.remctl, patched_q, return_value=return_value)

    def _snap(self, db=None, **kwargs):
        db = db or mock.MagicMock()
        with mock.patch.object(self.remctl, "reminders_to_dicts", return_value=[]), \
             mock.patch.object(self.remctl, "q_section_memberships", return_value={}):
            return self.remctl.reminder_query_snapshot(db, **kwargs)

    def test_search_default_limit_is_100(self):
        with self._ctx("q_search") as m:
            result = self._snap(query="milk")
        m.assert_called_once()
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), 100)
        self.assertEqual(result["limit"], 100)

    def test_flagged_default_limit_is_max(self):
        with self._ctx("q_flagged") as m:
            result = self._snap(flagged=True)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), self.remctl.MAX_REMINDER_QUERY_LIMIT)
        self.assertEqual(result["limit"], self.remctl.MAX_REMINDER_QUERY_LIMIT)

    def test_urgent_default_limit_is_max(self):
        with self._ctx("q_urgent") as m:
            result = self._snap(urgent=True)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), self.remctl.MAX_REMINDER_QUERY_LIMIT)

    def test_overdue_default_limit_is_max(self):
        with self._ctx("q_overdue") as m:
            result = self._snap(overdue=True)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), self.remctl.MAX_REMINDER_QUERY_LIMIT)

    def test_upcoming_default_limit_is_max(self):
        with self._ctx("q_upcoming") as m:
            result = self._snap(days_ahead=7)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), self.remctl.MAX_REMINDER_QUERY_LIMIT)

    def test_today_default_limit_is_max(self):
        with self._ctx("q_due_today") as m:
            result = self._snap(include_overdue=True)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), self.remctl.MAX_REMINDER_QUERY_LIMIT)

    def test_list_query_default_limit_is_500(self):
        with self._ctx("q_reminders") as m:
            result = self._snap(list_pk=1)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), self.remctl.DEFAULT_REMINDER_QUERY_LIMIT)
        self.assertEqual(result["limit"], self.remctl.DEFAULT_REMINDER_QUERY_LIMIT)

    def test_over_500_list_query_truncated_flag_propagated(self):
        """Simulate q_reminders returning truncated=True at limit=500."""
        fake_rows = [{"id": i} for i in range(500)]
        with self._ctx("q_reminders", return_value=(fake_rows, True)):
            result = self._snap(list_pk=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["limit"], self.remctl.DEFAULT_REMINDER_QUERY_LIMIT)

    def test_over_100_search_truncated_flag_propagated(self):
        """Simulate q_search returning truncated=True at limit=100."""
        fake_rows = [{"id": i} for i in range(100)]
        with self._ctx("q_search", return_value=(fake_rows, True)):
            result = self._snap(query="x")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["limit"], 100)

    def test_explicit_limit_honored_for_flagged(self):
        with self._ctx("q_flagged") as m:
            result = self._snap(flagged=True, limit=50)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("limit"), 50)
        self.assertEqual(result["limit"], 50)

    def test_explicit_limit_honored_for_list(self):
        fake_rows = [{"id": i} for i in range(5)]
        with self._ctx("q_reminders", return_value=(fake_rows, True)):
            result = self._snap(list_pk=1, limit=5)
        self.assertEqual(result["limit"], 5)

    def test_truncated_and_limit_keys_always_present(self):
        with self._ctx("q_flagged"):
            result = self._snap(flagged=True)
        self.assertIn("truncated", result)
        self.assertIn("limit", result)

    def test_due_before_dueafter_not_in_reminder_query_snapshot_signature(self):
        """N2: reminder_query_snapshot must not accept due_before/due_after."""
        import inspect
        sig = inspect.signature(self.remctl.reminder_query_snapshot)
        self.assertNotIn("due_before", sig.parameters)
        self.assertNotIn("due_after", sig.parameters)






if __name__ == "__main__":
    unittest.main()
