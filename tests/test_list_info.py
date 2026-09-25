"""list-info (section ids and sharees for typed writes) and list-create's numeric id."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import load_module

remctl = load_module("remctl_list_info_test", "remctl")


def store():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE ZREMCDBASELIST (
            Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZCKIDENTIFIER TEXT,
            ZMARKEDFORDELETION INTEGER DEFAULT 0, Z_ENT INTEGER DEFAULT 3, ZSHAREDOWNERIDENTIFIER BLOB);
        CREATE TABLE ZREMCDBASESECTION (
            Z_PK INTEGER PRIMARY KEY, ZDISPLAYNAME TEXT, ZLIST INTEGER, ZCKIDENTIFIER TEXT,
            ZMARKEDFORDELETION INTEGER DEFAULT 0);
        CREATE TABLE ZREMCDOBJECT (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZLIST INTEGER, ZCKIDENTIFIER TEXT, ZDISPLAYNAME TEXT,
            ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZADDRESS1 TEXT, ZSTATUS INTEGER, ZACCESSLEVEL INTEGER,
            ZMARKEDFORDELETION INTEGER DEFAULT 0);
        INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME, ZCKIDENTIFIER) VALUES (1, 'Shopping', 'L1'), (2, 'Work', 'L2');
        INSERT INTO ZREMCDBASESECTION (Z_PK, ZDISPLAYNAME, ZLIST, ZCKIDENTIFIER) VALUES
            (1, 'Produce', 1, 'SEC-A'), (2, 'Produce', 1, 'SEC-B'), (3, 'Drafts', 2, 'SEC-C');
        INSERT INTO ZREMCDOBJECT (Z_PK, Z_ENT, ZLIST, ZCKIDENTIFIER, ZDISPLAYNAME, ZADDRESS1) VALUES
            (40, 36, 1, 'SHAREE-1', 'Alex', 'alex@example.com');
    """)
    return db


class ListInfoTests(unittest.TestCase):
    def run_json(self, func, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            func(args)
        return json.loads(out.getvalue())

    def test_sections_with_duplicate_names_keep_distinct_ids_and_sharees_are_listed(self):
        with mock.patch.object(remctl, "open_db", return_value=store()):
            info = self.run_json(remctl.cmd_list_info, SimpleNamespace(name="Shopping", list_id=None, json=True))
        self.assertEqual((info["id"], info["title"], info["shared"]), (1, "Shopping", True))
        self.assertEqual(info["sections"], [{"id": "SEC-A", "name": "Produce"}, {"id": "SEC-B", "name": "Produce"}])
        self.assertEqual(info["sharees"][0]["address"], "alex@example.com")
        with mock.patch.object(remctl, "open_db", return_value=store()):
            work = self.run_json(remctl.cmd_list_info, SimpleNamespace(name=None, list_id=2, json=True))
        self.assertEqual((work["shared"], work["sharees"], len(work["sections"])), (False, [], 1))

    def test_new_list_id_ignores_an_older_list_with_the_same_name(self):
        db = store()
        before = remctl.q_list_ids_named(db, "Work")
        db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME, ZCKIDENTIFIER) VALUES (9, 'Work', 'L9')")
        with mock.patch.object(remctl, "open_db", return_value=db):
            self.assertEqual(remctl.wait_for_new_list_id("Work", before), 9)
        with mock.patch.object(remctl, "open_db", return_value=db), mock.patch.object(remctl.time, "sleep"):
            self.assertIsNone(remctl.wait_for_new_list_id("Work", {2, 9}))


if __name__ == "__main__":
    unittest.main()
