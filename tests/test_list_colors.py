"""Offline regressions for strings in archived list colors and lists JSON."""

import contextlib
import io
import json
import plistlib
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import load_module

remctl = load_module("remctl_list_colors_test", "remctl")


def color_archive(name="orange", hex_value="#FF9500", *, include_hex=True):
    color = {"ckSymbolicColorName": plistlib.UID(2)}
    if include_hex:
        color["daHexString"] = plistlib.UID(3)
    return plistlib.dumps({
        "$archiver": "NSKeyedArchiver",
        "$version": 100000,
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null", color, name, hex_value,
                     {"$classname": "NSString", "$classes": ["NSString", "NSObject"]},
                     "#FF9500"],
    }, fmt=plistlib.FMT_BINARY)


class ListColorTests(unittest.TestCase):
    def test_plain_archived_strings_keep_their_values(self):
        self.assertEqual(remctl.parse_list_color(color_archive()),
                         {"name": "orange", "hex": "#FF9500"})

    def test_archived_nsstrings_are_unwrapped_for_both_fields(self):
        blob = color_archive(
            {"NS.string": "orange", "$class": plistlib.UID(4)},
            {"NS.string": "#FF9500", "$class": plistlib.UID(4)},
        )
        self.assertEqual(remctl.parse_list_color(blob),
                         {"name": "orange", "hex": "#FF9500"})

    def test_nsstring_content_can_reference_a_string_object(self):
        blob = color_archive(hex_value={"NS.string": plistlib.UID(5), "$class": plistlib.UID(4)})
        self.assertEqual(remctl.parse_list_color(blob),
                         {"name": "orange", "hex": "#FF9500"})

    def test_missing_hex_keeps_the_existing_empty_string(self):
        self.assertEqual(remctl.parse_list_color(color_archive(include_hex=False)),
                         {"name": "orange", "hex": ""})

    def test_malformed_archives_keep_the_existing_default_and_serialize(self):
        default = {"name": "blue", "hex": "#007AFF"}
        malformed_values = (
            plistlib.UID(999),
            {"$class": plistlib.UID(4)},
            {"NS.string": 42, "$class": plistlib.UID(4)},
            {"NS.string": plistlib.UID(999), "$class": plistlib.UID(4)},
            ["orange"],
        )
        for field in ("name", "hex_value"):
            self_reference = {"NS.string": plistlib.UID(2 if field == "name" else 3),
                              "$class": plistlib.UID(4)}
            for value in (*malformed_values, self_reference):
                with self.subTest(field=field, value=value):
                    result = remctl.parse_list_color(color_archive(**{field: value}))
                    self.assertEqual(result, default)
                    self.assertEqual(json.loads(json.dumps(result)), default)
        for blob in (None, b"invalid plist"):
            with self.subTest(blob=blob):
                self.assertEqual(remctl.parse_list_color(blob), default)

    def test_lists_json_serializes_an_archived_nsstring_hex(self):
        row = {
            "Z_PK": 101,
            "ZNAME": "Synthetic list",
            "ZCKIDENTIFIER": "SYNTHETIC-LIST",
            "ZCOLOR": color_archive(hex_value={"NS.string": "#FF9500", "$class": plistlib.UID(4)}),
        }
        with (
            mock.patch.object(remctl, "open_db", return_value=None),
            mock.patch.object(remctl, "q_all_lists", return_value=[row]),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            remctl.cmd_lists(SimpleNamespace(json=True))
        payload = json.loads(out.getvalue())
        self.assertEqual(payload[0]["color"], {"name": "orange", "hex": "#FF9500"})


if __name__ == "__main__":
    unittest.main()
