"""Offline checks for batch reminder extras and their fallback behavior."""

import os
import plistlib
import sqlite3
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from remctl_serialization import decode_contact_handles, preload_extras, serialize_reminder, serialize_reminders

APPLE_EPOCH = 978307200


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


def apple_seconds(moment):
    return moment.timestamp() - APPLE_EPOCH


def local_ts(zone):
    """The CLI's ts() as it behaves on a Mac set to this time zone."""
    def ts(value):
        if not value:
            return None
        return datetime.fromtimestamp(value + APPLE_EPOCH, ZoneInfo(zone)).replace(tzinfo=None)
    return ts


class DueDateTests(unittest.TestCase):
    def serialize(self, zone, **fields):
        row = dict(reminder_rows(1)[0], **fields)
        return serialize_reminder(row, ts=local_ts(zone), priority_names={})

    def test_all_day_due_date_keeps_its_day_in_every_time_zone(self):
        # Reminders stores an all-day due date as midnight UTC and the display date
        # as local midnight. Read as local time, midnight UTC is the evening before
        # in New York.
        for zone in ("America/New_York", "Europe/Rome", "Asia/Tokyo"):
            with self.subTest(zone=zone):
                payload = self.serialize(
                    zone,
                    ZALLDAY=1,
                    ZDUEDATE=apple_seconds(datetime(2026, 9, 30, tzinfo=timezone.utc)),
                    ZDISPLAYDATEDATE=apple_seconds(datetime(2026, 9, 30, tzinfo=ZoneInfo(zone))),
                )
                self.assertEqual(payload["dueDate"], "2026-09-30T00:00:00")
                self.assertNotIn("displayDate", payload)
                self.assertTrue(payload["allDay"])

    def test_all_day_due_date_that_is_not_utc_midnight_keeps_its_local_day(self):
        payload = self.serialize(
            "America/New_York",
            ZALLDAY=1,
            ZDUEDATE=apple_seconds(datetime(2026, 9, 30, 8, 0, tzinfo=ZoneInfo("America/New_York"))),
        )
        self.assertEqual(payload["dueDate"], "2026-09-30T00:00:00")

    def test_timed_due_date_is_local_time_and_keeps_a_separate_display_date(self):
        due = datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc)
        payload = self.serialize(
            "America/New_York",
            ZALLDAY=0,
            ZDUEDATE=apple_seconds(due),
            ZDISPLAYDATEDATE=apple_seconds(due) - 15 * 60,
        )
        self.assertEqual(payload["dueDate"], "2026-09-30T09:30:00")
        self.assertEqual(payload["displayDate"], "2026-09-30T09:15:00")
        self.assertFalse(payload["allDay"])

    def serialize_in_local_zone(self, zone, *, db=None, **fields):
        """Serialize with the process time zone set, as the CLI's own ts() reads it."""
        previous = os.environ.get("TZ")
        os.environ["TZ"] = zone
        time.tzset()
        try:
            return self.serialize_local(db=db, **fields)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    def serialize_local(self, *, db=None, **fields):
        row = dict(reminder_rows(1)[0], ZALLDAY=0, **fields)
        ts = lambda value: datetime.fromtimestamp(value + APPLE_EPOCH) if value else None
        return serialize_reminder(row, ts=ts, priority_names={}, db=db)

    def test_floating_timed_due_date_reads_as_wall_clock_time(self):
        # Rows from issue #49, in New York on September 30 (UTC-4). Reminders.app
        # stores an instant with a time zone; GoodTask stores 5 PM as 17:00Z with
        # none; some rows carry a time zone but still hold wall-clock time.
        utc = lambda hour, minute=0: apple_seconds(datetime(2026, 9, 30, hour, minute, tzinfo=timezone.utc))
        cases = {
            "instant": dict(ZDUEDATE=utc(21), ZDISPLAYDATEDATE=utc(21), ZTIMEZONE="America/New_York"),
            "no time zone": dict(ZDUEDATE=utc(17), ZDISPLAYDATEDATE=utc(21), ZTIMEZONE=None),
            "wall clock with a time zone": dict(ZDUEDATE=utc(12), ZDISPLAYDATEDATE=utc(16), ZTIMEZONE="America/New_York"),
        }
        expected = {"instant": "2026-09-30T17:00:00", "no time zone": "2026-09-30T17:00:00",
                    "wall clock with a time zone": "2026-09-30T12:00:00"}
        for name, fields in cases.items():
            with self.subTest(name):
                payload = self.serialize_in_local_zone("America/New_York", **fields)
                self.assertEqual(payload["dueDate"], expected[name])
                self.assertNotIn("displayDate", payload)

    def test_floating_due_date_east_of_utc_and_alarm_near_an_instant(self):
        utc = lambda hour, minute=0: apple_seconds(datetime(2026, 9, 30, hour, minute, tzinfo=timezone.utc))
        payload = self.serialize_in_local_zone("Europe/Rome", ZDUEDATE=utc(17), ZTIMEZONE=None)
        self.assertEqual(payload["dueDate"], "2026-09-30T17:00:00")
        # An alarm 15 minutes early is a display date, not a floating due date.
        payload = self.serialize_in_local_zone(
            "America/New_York", ZDUEDATE=utc(21), ZDISPLAYDATEDATE=utc(20, 45), ZTIMEZONE="America/New_York",
        )
        self.assertEqual(payload["dueDate"], "2026-09-30T17:00:00")
        self.assertEqual(payload["displayDate"], "2026-09-30T16:45:00")

    def test_absolute_alarm_at_utc_offset_does_not_change_zoned_due_time(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(
            "CREATE TABLE ZREMCDOBJECT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, "
            "ZREMINDER INTEGER, ZTRIGGER INTEGER, ZMARKEDFORDELETION INTEGER, "
            "ZDATECOMPONENTSDATA BLOB);"
            "CREATE INDEX alarm_reminder ON ZREMCDOBJECT (ZREMINDER);"
            "INSERT INTO ZREMCDOBJECT VALUES (1,15,1,2,0,NULL), (2,17,NULL,NULL,0,'{}');"
        )
        utc = lambda hour: apple_seconds(datetime(2026, 9, 30, hour, tzinfo=timezone.utc))
        for zone, due, display, expected_due, expected_display in (
            ("Europe/Rome", utc(15), utc(13), "17:00:00", "15:00:00"),
            ("America/New_York", utc(17), utc(21), "13:00:00", "17:00:00"),
        ):
            with self.subTest(zone=zone):
                payload = self.serialize_in_local_zone(
                    zone, db=db, ZDUEDATE=due, ZDISPLAYDATEDATE=display, ZTIMEZONE=zone,
                )
                self.assertEqual(payload["dueDate"], f"2026-09-30T{expected_due}")
                self.assertEqual(payload["displayDate"], f"2026-09-30T{expected_display}")
        # A genuinely floating reminder stays floating even when it has an alarm.
        payload = self.serialize_in_local_zone(
            "America/New_York", db=db, ZDUEDATE=utc(17), ZDISPLAYDATEDATE=utc(21), ZTIMEZONE=None,
        )
        self.assertEqual(payload["dueDate"], "2026-09-30T17:00:00")
        self.assertNotIn("displayDate", payload)
        # Deleted alarms must not override the legacy wall-clock fallback.
        db.execute("UPDATE ZREMCDOBJECT SET ZMARKEDFORDELETION = 1 WHERE Z_PK = 1")
        payload = self.serialize_in_local_zone(
            "America/New_York", db=db, ZDUEDATE=utc(12), ZDISPLAYDATEDATE=utc(16), ZTIMEZONE="America/New_York",
        )
        self.assertEqual(payload["dueDate"], "2026-09-30T12:00:00")
        # At UTC the two interpretations agree, so no alarm lookup is needed.
        no_queries = mock.Mock()
        no_queries.execute.side_effect = AssertionError("Unexpected alarm query")
        payload = self.serialize_in_local_zone(
            "UTC", db=no_queries, ZDUEDATE=utc(17), ZDISPLAYDATEDATE=utc(17), ZTIMEZONE="UTC",
        )
        self.assertEqual(payload["dueDate"], "2026-09-30T17:00:00")


class ContactHandlesTests(unittest.TestCase):
    def test_decodes_the_keyed_archive_reminderkit_writes(self):
        # The layout REMContactRepresentation.archivedData produced on macOS 27.2.
        uid = plistlib.UID
        array = {"$classes": ["NSArray", "NSObject"], "$classname": "NSArray"}
        archive = plistlib.dumps({
            "$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": uid(1)},
            "$objects": [
                "$null",
                {"$class": uid(7), "phones": uid(2), "emails": uid(5)},
                {"$class": uid(4), "NS.objects": [uid(3)]},
                "+1 (555) 010-0199",
                array,
                {"$class": uid(4), "NS.objects": [uid(6)]},
                "sam@example.com",
                {"$classes": ["REMContactRepresentation", "NSObject"], "$classname": "REMContactRepresentation"},
            ],
        }, fmt=plistlib.FMT_BINARY)
        self.assertEqual(decode_contact_handles(archive), {"phones": ["+1 (555) 010-0199"], "emails": ["sam@example.com"]})
        self.assertIsNone(decode_contact_handles(None))
        self.assertIsNone(decode_contact_handles(b"not an archive"))
