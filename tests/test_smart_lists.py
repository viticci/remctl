from __future__ import annotations

import json
import plistlib
import unittest

from remctl_smart_lists import (
    SmartListFilterError,
    build_supported_filter_payload,
    decode_smart_list_filter_blob,
    encode_supported_filter_payload,
)


def archived_filter(payload: dict) -> bytes:
    objects = [
        "$null",
        {
            "$class": plistlib.UID(3),
            "data": plistlib.UID(2),
        },
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        {
            "$classes": [
                "ReminderKitInternal.REMCustomSmartListFilterDescriptor",
                "NSObject",
            ],
            "$classname": "ReminderKitInternal.REMCustomSmartListFilterDescriptor",
        },
    ]
    return plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$top": {"root": plistlib.UID(1)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
    )


class SmartListFilterTests(unittest.TestCase):
    def test_decode_null_filter_blob(self):
        decoded = decode_smart_list_filter_blob(None)

        self.assertIsNone(decoded["payload"])
        self.assertIsNone(decoded["summary"])
        self.assertIsNone(decoded["error"])

    def test_decode_empty_custom_filter_archive(self):
        decoded = decode_smart_list_filter_blob(archived_filter({}))

        self.assertEqual(decoded["encoding"], "keyed_archive_json")
        self.assertEqual(decoded["payload"], {})
        self.assertEqual(decoded["summary"]["kind"], "all")

    def test_decode_flagged_filter(self):
        decoded = decode_smart_list_filter_blob(b'{"flagged":true}')

        self.assertEqual(decoded["payload"], {"flagged": True})
        self.assertEqual(decoded["summary"]["kind"], "flagged")
        self.assertTrue(decoded["summary"]["supported"])

    def test_decode_priority_filters(self):
        single = decode_smart_list_filter_blob(b'{"priorities":["high"]}')
        multiple = decode_smart_list_filter_blob(b'{"priorities":["low","medium","high"]}')

        self.assertEqual(single["summary"]["priorities"], ["high"])
        self.assertEqual(multiple["summary"]["priorities"], ["low", "medium", "high"])

    def test_decode_reminders_app_official_filter_samples(self):
        samples = [
            {"hashtags": {"hashtags": {"operation": "or", "include": ["remctl", "codex"], "exclude": []}}},
            {"hashtags": {"any": ""}},
            {"operation": "and", "hashtags": {"untagged": ""}},
            {"operation": "and", "date": {"afterDate": "15-05-2026"}},
            {"date": {"dateRange": ["15-05-2026", "15-05-2026"]}},
            {"operation": "and", "date": {"relativeRange": {"direction": "inNext", "magnitude": "1", "includePastDue": True, "units": "hour"}}},
            {"time": {"afternoon": ""}},
            {"operation": "and", "time": {"noTime": ""}},
            {"location": {"vehicle": "connected"}},
            {"location": {"location": {"title": "Home", "latitude": 41.9, "radius": 100, "longitude": 12.5, "proximity": "enter"}}},
            {"operation": "and", "lists": {"include": ["46EBCB36-C7CB-4983-937A-A5137895473F"], "exclude": []}},
            {"operation": "and", "lists": {"operation": "or", "include": ["WORK", "PROJECTS"], "exclude": []}},
            {"operation": "or", "date": {"today": False}, "lists": {"include": ["46EBCB36-C7CB-4983-937A-A5137895473F"], "exclude": []}},
        ]

        for sample in samples:
            with self.subTest(sample=sample):
                decoded = decode_smart_list_filter_blob(json.dumps(sample).encode("utf-8"))
                self.assertTrue(decoded["summary"]["supported"])

    def test_decode_legacy_short_selected_tag_filter_marks_non_materializing(self):
        decoded = decode_smart_list_filter_blob(b'{"hashtags":{"hashtags":["remctl"]}}')

        self.assertFalse(decoded["summary"]["supported"])
        self.assertFalse(decoded["summary"]["materializes"])
        self.assertEqual(decoded["summary"]["kind"], "unsupported")

    def test_build_supported_filter_payload_encodes_official_filters(self):
        self.assertEqual(
            build_supported_filter_payload(tags=["remctl", "codex"]),
            {"hashtags": {"hashtags": {"operation": "or", "include": ["remctl", "codex"], "exclude": []}}},
        )
        self.assertEqual(
            build_supported_filter_payload(tags=["remctl"], tag_match="all"),
            {"hashtags": {"hashtags": {"operation": "and", "include": ["remctl"], "exclude": []}}},
        )
        self.assertEqual(
            build_supported_filter_payload(tags=["#remctl"], tag_match="any"),
            {"hashtags": {"hashtags": {"operation": "or", "include": ["remctl"], "exclude": []}}},
        )
        self.assertEqual(build_supported_filter_payload(any_tag=True), {"hashtags": {"any": ""}})
        self.assertEqual(build_supported_filter_payload(untagged=True), {"hashtags": {"untagged": ""}})
        self.assertEqual(
            build_supported_filter_payload(date_on="2026-05-15"),
            {"date": {"onDate": "15-05-2026"}},
        )
        self.assertEqual(
            build_supported_filter_payload(date_range="2026-05-15,2026-05-16"),
            {"date": {"dateRange": ["15-05-2026", "16-05-2026"]}},
        )
        self.assertEqual(
            build_supported_filter_payload(date_relative="in-next:1:hour:past-due"),
            {"date": {"relativeRange": {"direction": "inNext", "magnitude": "1", "includePastDue": True, "units": "hour"}}},
        )
        self.assertEqual(build_supported_filter_payload(time_filter="no-time"), {"time": {"noTime": ""}})
        self.assertEqual(build_supported_filter_payload(vehicle="disconnected"), {"location": {"vehicle": "disconnected"}})
        self.assertEqual(
            build_supported_filter_payload(
                match="any",
                tags=["remctl"],
                date_today=True,
                include_list_ids=["LIST-1"],
            ),
            {
                "operation": "or",
                "hashtags": {"hashtags": {"operation": "or", "include": ["remctl"], "exclude": []}},
                "date": {"today": False},
                "lists": {"include": ["LIST-1"], "exclude": []},
            },
        )
        self.assertEqual(
            build_supported_filter_payload(include_list_ids=["WORK", "PROJECTS"]),
            {"lists": {"include": ["WORK", "PROJECTS"], "exclude": [], "operation": "or"}},
        )
        self.assertEqual(
            build_supported_filter_payload(include_list_ids=["WORK", "PROJECTS"], list_match="all"),
            {"lists": {"include": ["WORK", "PROJECTS"], "exclude": [], "operation": "and"}},
        )

    def test_build_supported_filter_payload_rejects_unknown_filters(self):
        with self.assertRaises(SmartListFilterError):
            build_supported_filter_payload()
        with self.assertRaises(SmartListFilterError):
            build_supported_filter_payload(priorities=["none"])
        with self.assertRaises(SmartListFilterError):
            build_supported_filter_payload(date_any=True, date_on="2026-05-15")

    def test_encode_supported_filter_payload_outputs_raw_json_bytes(self):
        payload = build_supported_filter_payload(priorities=["high"])

        self.assertEqual(encode_supported_filter_payload(payload), b'{"priorities":["high"]}')

    def test_decode_malformed_xml_plist_returns_error_dict(self):
        blob = b'<?xml version="1.0"?><plist><dict><unclosed'

        decoded = decode_smart_list_filter_blob(blob)

        self.assertIsNone(decoded["payload"])
        self.assertIsNone(decoded["summary"])
        self.assertIsNotNone(decoded["error"])

    def test_encode_rejects_invalid_date_string_in_filter_json_shape(self):
        payload = {"date": {"onDate": "not-a-date"}}

        with self.assertRaises(SmartListFilterError):
            encode_supported_filter_payload(payload)

    def test_encode_accepts_valid_date_string_in_filter_json_shape(self):
        payload = {"date": {"onDate": "15-05-2026"}}

        self.assertEqual(
            encode_supported_filter_payload(payload),
            b'{"date":{"onDate":"15-05-2026"}}',
        )

    def test_decode_lenient_summary_for_invalid_date_string_in_existing_blob(self):
        decoded = decode_smart_list_filter_blob(b'{"date":{"onDate":"not-a-date"}}')

        self.assertEqual(decoded["payload"], {"date": {"onDate": "not-a-date"}})
        self.assertIsNotNone(decoded["summary"])
        self.assertIn("not-a-date", decoded["summary"]["description"])


if __name__ == "__main__":
    unittest.main()
