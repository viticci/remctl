"""Offline checks for batch reminder extras and their fallback behavior."""

import sqlite3
import unittest
from unittest import mock

from remctl_serialization import preload_extras, serialize_reminder, serialize_reminders


def fixture_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE ZREMCDREMINDER (ZPARENTREMINDER INTEGER, "
        "ZMARKEDFORDELETION INTEGER, ZCOMPLETED INTEGER);"
        "CREATE TABLE ZREMCDOBJECT (ZREMINDER3 INTEGER, ZHASHTAGLABEL INTEGER, "
        "ZMARKEDFORDELETION INTEGER);"
        "CREATE TABLE ZREMCDHASHTAGLABEL (Z_PK INTEGER, ZNAME TEXT);"
        "INSERT INTO ZREMCDREMINDER VALUES (1,0,0),(1,0,1),(1,1,0);"
        "INSERT INTO ZREMCDHASHTAGLABEL VALUES (1,'Work'),(2,'Deleted');"
        "INSERT INTO ZREMCDOBJECT VALUES (1,1,0),(1,2,1);"
    )
    return db


def reminder_rows(count):
    fields = dict.fromkeys((
        "ZNOTES", "ZICSURL", "ZDUEDATE", "ZCREATIONDATE", "ZCOMPLETIONDATE",
        "ZPARENTREMINDER", "ZCKIDENTIFIER", "ZPRIORITY", "ZCOMPLETED", "ZFLAGGED",
    ))
    return [dict(fields, Z_PK=pk, ZTITLE=f"Fixture {pk}", list_name="Fixture")
            for pk in range(1, count + 1)]


def subtask_count(db, pk):
    return db.execute(
        "SELECT COUNT(*) FROM ZREMCDREMINDER WHERE ZPARENTREMINDER = ? "
        "AND ZMARKEDFORDELETION = 0 AND ZCOMPLETED = 0", (pk,),
    ).fetchone()[0]


def hashtags(db, pk):
    return [row[0] for row in db.execute(
        "SELECT h.ZNAME FROM ZREMCDOBJECT o "
        "JOIN ZREMCDHASHTAGLABEL h ON o.ZHASHTAGLABEL = h.Z_PK "
        "WHERE o.ZREMINDER3 = ? AND o.ZMARKEDFORDELETION = 0", (pk,),
    )]


class BatchExtrasTests(unittest.TestCase):
    def test_batch_matches_individual_payloads_with_constant_query_count(self):
        for size in (1, 100):
            with self.subTest(size=size):
                db = fixture_db()
                self.addCleanup(db.close)
                rows = reminder_rows(size)
                kwargs = dict(ts=None, priority_names={}, db=db,
                              fallback_subtask_count=subtask_count, fallback_hashtags=hashtags)
                individual = [serialize_reminder(row, **kwargs) for row in rows]
                queries = []
                db.set_trace_callback(queries.append)
                batch = serialize_reminders(rows, **kwargs)
                db.set_trace_callback(None)
                self.assertEqual(batch, individual)
                self.assertEqual(len(queries), 2)
                self.assertEqual(batch[0]["subtaskCount"], 1)
                self.assertEqual(batch[0]["tags"], ["Work"])
                for item in batch[1:]:
                    self.assertEqual(item["subtaskCount"], 0)
                    self.assertNotIn("tags", item)

    def test_failed_preload_keeps_only_its_own_fallback(self):
        for missing in ("ZREMCDREMINDER", "ZREMCDHASHTAGLABEL"):
            with self.subTest(missing=missing):
                db = fixture_db()
                self.addCleanup(db.close)
                db.execute(f"DROP TABLE {missing}")
                counts, tags = preload_extras(db, [1, 2])
                count_fallback = mock.Mock(return_value=7)
                tag_fallback = mock.Mock(return_value=["Fallback"])
                payloads = [serialize_reminder(
                    row, ts=None, priority_names={}, db=db,
                    subtask_counts=counts, hashtags=tags,
                    fallback_subtask_count=count_fallback, fallback_hashtags=tag_fallback,
                ) for row in reminder_rows(2)]
                if missing == "ZREMCDREMINDER":
                    self.assertEqual(counts, {})
                    self.assertEqual(tags, {1: ["Work"], 2: []})
                    self.assertEqual(count_fallback.call_args_list,
                                     [mock.call(db, 1), mock.call(db, 2)])
                    tag_fallback.assert_not_called()
                    self.assertEqual([item["subtaskCount"] for item in payloads], [7, 7])
                else:
                    self.assertEqual(counts, {1: 1, 2: 0})
                    self.assertEqual(tags, {})
                    count_fallback.assert_not_called()
                    self.assertEqual(tag_fallback.call_args_list,
                                     [mock.call(db, 1), mock.call(db, 2)])
                    self.assertEqual([item["tags"] for item in payloads],
                                     [["Fallback"], ["Fallback"]])
