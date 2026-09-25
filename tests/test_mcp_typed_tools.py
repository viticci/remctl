"""Typed MCP tools for search paging, batches, lists, locations, and private metadata.

These tests exercise the real tool schemas and the real CLI parser, and, at the
end, the real stdio server with a stand-in remctl.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import remctl_mcp
from helpers import ROOT, load_module
from test_mcp_server import FakeExecutor, make_server, modern_meta, request

remctl = load_module("remctl_mcp_typed_tools_test", "remctl")
TOOLS = remctl_mcp.TOOLS_BY_NAME


def argv_for(name, arguments):
    tool = TOOLS[name]
    return tool.build_argv(remctl_mcp.validate_arguments(tool, arguments))


def parsed(name, arguments):
    """Build a tool's argv and parse it with the CLI's own parser."""
    parser, subparsers = remctl.build_parser()
    with contextlib.redirect_stderr(io.StringIO()):
        return remctl.parse_cli_args(parser, subparsers, argv_for(name, arguments))


def call(server, name, arguments):
    return request(server, "tools/call", {"_meta": modern_meta(), "name": name, "arguments": arguments})["result"]


class SchemaTests(unittest.TestCase):
    def test_search_schema_offers_scope_and_bounded_paging(self):
        schema = TOOLS["search"].input_schema()["properties"]
        self.assertEqual({"list", "list_id", "limit", "offset"} - set(schema), set())
        self.assertEqual((schema["limit"]["minimum"], schema["limit"]["maximum"]), (1, 500))
        self.assertIn("hasMore", TOOLS["search"].output_schema["required"])
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "Pass only one of"):
            remctl_mcp.validate_arguments(TOOLS["search"], {"query": "x", "list": "Work", "list_id": 3})

    def test_batch_ids_are_bounded_integers_and_exclusive_with_one_id(self):
        schema = TOOLS["set_completion"].input_schema()["properties"]["reminder_ids"]
        self.assertEqual((schema["items"]["type"], schema["maxItems"]), ("integer", 50))
        tool = TOOLS["delete_reminder"]
        for bad, message in (
            ({"reminder_ids": []}, "1 to 50"),
            ({"reminder_ids": list(range(1, 52))}, "1 to 50"),
            ({"reminder_ids": ["x"]}, "array of integers"),
            ({"reminder_ids": [0]}, "positive"),
            ({"reminder_id": 1, "reminder_ids": [2]}, "Pass only one of"),
            ({}, "Pass at least one of"),
        ):
            with self.subTest(arguments=bad):
                with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, message):
                    remctl_mcp.validate_arguments(tool, bad)

    def test_location_numbers_are_finite_and_in_range(self):
        tool = TOOLS["update_reminder"]
        for bad, message in (
            ({"latitude": 91}, "at most 90"),
            ({"radius": 0}, "at least 1"),
            ({"radius": 100_001}, "at most 100000"),
            ({"latitude": "nan"}, "finite number"),
            ({"location_address": "Via Roma 1", "latitude": 41.9}, "Pass only one of"),
        ):
            with self.subTest(arguments=bad):
                with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, message):
                    remctl_mcp.validate_arguments(tool, {"reminder_id": 1, "private": True, **bad})


class ReviewFixTests(unittest.TestCase):
    def test_completion_is_not_marked_idempotent(self):
        # Completing a repeating reminder twice skips an occurrence, so clients must not retry it freely.
        self.assertFalse(TOOLS["set_completion"].annotations()["idempotentHint"])
        self.assertGreaterEqual(TOOLS["set_completion"].timeout, 300)

    def test_false_flags_count_as_absent(self):
        tool = TOOLS["update_reminder"]
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "Pass at least one of"):
            remctl_mcp.validate_arguments(tool, {"reminder_id": 1, "clear_tags": False})
        values = remctl_mcp.validate_arguments(tool, {"reminder_id": 1, "private": True, "assign": "me", "unassign": False})
        self.assertEqual(argv_for("update_reminder", values), ["edit", "1", "--private", "--assign", "me", "--json"])
        # urgent: false is a real request, not an omission.
        self.assertIn("--no-urgent", argv_for("update_reminder", {"reminder_id": 1, "private": True, "urgent": False}))

    def test_calls_longer_than_the_host_accepts_are_refused_before_running(self):
        server, executor = make_server(FakeExecutor(stdout="{}"))
        result = call(server, "create_reminder", {
            "title": "x", "private": True, "subtasks": [f"s{n}" for n in range(20)],
            "notes": "n", "due": "2026-10-01", "priority": "high", "recurrence": "daily", "alarm": "15m",
            "url": "https://example.com", "tags": "a", "section": "S", "assign": "me", "early_reminder": "1h",
            "urgent": True, "location_address": "Via Roma 1, Roma", "location_title": "T", "radius": 10,
            "proximity": "leaving", "list": "Work",
        })
        self.assertFalse(result["isError"])  # 20 subtasks still fit
        self.assertLessEqual(len(executor.calls[0]["argv"]), remctl_mcp.HOST_ARGV_MAX)
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "too large"):
            remctl_mcp.validate_arguments(TOOLS["create_reminder"], {"title": "x", "subtasks": ["s"] * 21})
        with mock.patch.object(remctl_mcp, "HOST_ARGV_MAX", 5):
            server, executor = make_server(FakeExecutor(stdout="{}"))
            result = call(server, "create_reminder", {"title": "x", "list": "Work", "due": "2026-10-01"})
        self.assertTrue(result["isError"])
        self.assertIn("RemCTL accepts 5", result["structuredContent"]["error"]["message"])
        self.assertEqual(executor.calls, [])


class PrivateOptInTests(unittest.TestCase):
    def test_without_private_the_public_fallbacks_are_unchanged(self):
        args = parsed("create_reminder", {"title": "Read", "tags": ["a", "b"], "url": "https://example.com"})
        self.assertFalse(args.private)
        self.assertEqual((args.tags, args.url), ("a,b", "https://example.com"))

    def test_reminders_only_fields_refuse_to_run_without_private(self):
        for name, arguments in (
            ("create_reminder", {"title": "x", "section": "Research"}),
            ("create_reminder", {"title": "x", "location_address": "Via Roma 1"}),
            ("create_reminder", {"title": "x", "urgent": False}),
            ("update_reminder", {"reminder_id": 1, "tags": "work"}),
            ("update_reminder", {"reminder_id": 1, "clear_tags": True}),
            ("create_list", {"name": "Food", "groceries": True}),
            ("update_list", {"list": "Work", "emoji": "💼"}),
        ):
            with self.subTest(tool=name, arguments=arguments):
                with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "private: true"):
                    argv_for(name, arguments)

    def test_typed_metadata_reaches_the_cli_parser_intact(self):
        args = parsed("create_reminder", {
            "title": "Launch", "list": "Projects", "private": True, "due": "2026-10-01 09:00", "alarm": "15m",
            "tags": ["media", "launch"], "url": "https://example.com/a", "section": "-Research",
            "subtasks": ["Export PNG", '{"title":"Follow up","due":"2026-10-02"}'], "assign": "alex@example.com",
            "early_reminder": "1h", "urgent": False, "location_address": "Piazza Navona, Rome",
            "location_title": "Office", "radius": 150, "proximity": "leaving",
        })
        self.assertEqual(args.cmd, "add")
        self.assertTrue(args.private)
        self.assertEqual((args.due, args.alarm, args.tags, args.section), ("2026-10-01 09:00", "15m", "media,launch", "-Research"))
        self.assertEqual(args.subtask, ["Export PNG", '{"title":"Follow up","due":"2026-10-02"}'])
        self.assertEqual((args.assign, args.early_reminder, args.urgent), ("alex@example.com", "1h", False))
        self.assertEqual((args.location_address, args.location_title, args.radius, args.proximity),
                         ("Piazza Navona, Rome", "Office", 150.0, "leaving"))

        edit = parsed("update_reminder", {
            "reminder_id": 7, "private": True, "set_tags": ["a"], "remove_tags": ["-old"], "unassign": True,
            "latitude": 37.3349, "longitude": -122.009,
        })
        self.assertEqual((edit.id, edit.set_tags, edit.remove_tag, edit.unassign), (7, "a", ["-old"], True))
        self.assertEqual((edit.latitude, edit.longitude), (37.3349, -122.009))

    def test_location_extras_need_a_location(self):
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "need location_address"):
            argv_for("update_reminder", {"reminder_id": 1, "private": True, "radius": 50})
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "latitude and longitude together"):
            argv_for("update_reminder", {"reminder_id": 1, "private": True, "latitude": 41.9})

    def test_new_tools_parse_with_the_real_cli(self):
        cases = (
            ("set_completion", {"reminder_ids": [4, 5, 4], "completed": True}, "done", {"id": 4, "more_ids": [5, 4], "batch": True}),
            ("set_completion", {"reminder_ids": [4], "completed": True}, "done", {"id": 4, "more_ids": [], "batch": True}),
            ("delete_reminder", {"reminder_ids": [4, 5]}, "delete", {"id": 4, "more_ids": [5], "force": True, "batch": True}),
            ("delete_reminder", {"reminder_id": 4}, "delete", {"id": 4, "more_ids": [], "batch": False}),
            ("search", {"query": "-x", "list_id": 3, "offset": 100}, "search", {"query": "-x", "list_id": 3, "offset": 100, "limit": 100}),
            ("get_list", {"list": "-Work"}, "list-info", {"name": "-Work"}),
            ("resolve_location", {"query": "-Via Roma"}, "location-lookup", {"query": "-Via Roma"}),
            ("create_list", {"name": "Food", "private": True, "emoji": "🥕", "group": "Home"}, "list-create", {"name": "Food", "emoji": "🥕", "group": "Home"}),
            ("update_list", {"list_id": 9, "new_name": "Chores"}, "list-rename", {"list_id": 9, "new_name_option": "Chores"}),
            ("update_list", {"list": "Work", "new_name": "Office", "color": "#112233", "private": True}, "list-edit", {"name": "Work", "new_name": "Office", "color": "#112233"}),
        )
        for name, arguments, command, expected in cases:
            with self.subTest(tool=name):
                args = parsed(name, arguments)
                self.assertEqual(args.cmd, command)
                for key, value in expected.items():
                    self.assertEqual(getattr(args, key), value, key)


class ResultTests(unittest.TestCase):
    def test_batch_results_survive_a_nonzero_exit(self):
        batch = {
            "status": "failed", "operation": "done", "requested": [3, 1], "succeeded": [], "failed": [1],
            "uncertain": [3], "results": [
                {"id": 3, "status": "uncertain", "code": "completion_uncertain", "message": "check it"},
                {"id": 1, "status": "skipped", "code": "batch_stopped", "message": "not attempted"},
            ],
        }
        server, executor = make_server(FakeExecutor(stdout=json.dumps(batch), returncode=1))
        result = call(server, "set_completion", {"reminder_ids": [3, 1], "completed": True})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["uncertain"], [3])
        self.assertEqual(result["structuredContent"]["results"], batch["results"])
        self.assertEqual(executor.calls[0]["argv"], ["done", "3", "1", "--batch", "--json"])

    def test_search_page_passes_through_unwrapped(self):
        page = {"query": "x", "items": [{"id": 1, "title": "x", "list": "Work"}], "count": 1, "total": 120,
                "offset": 0, "limit": 1, "hasMore": True, "nextOffset": 1}
        server, _ = make_server(FakeExecutor(stdout=json.dumps(page)))
        result = call(server, "search", {"query": "x", "limit": 1})
        self.assertEqual(result["structuredContent"], page)

    def test_readback_keeps_tags_assignment_subtasks_dates_and_alarms(self):
        record = {
            "id": 77, "title": "Launch", "list": "Projects", "dueDate": "2026-10-01T09:00:00",
            "displayDate": "2026-10-01T08:45:00", "tags": ["media"],
            "assignment": {"assignee": {"name": "Alex", "address": "alex@example.com"}},
            "subtasks": [{"id": 78, "title": "Export PNG"}],
            "alarms": [{"type": "relative", "offset": -900}], "earlyReminder": {"unit": "hour", "count": 1},
        }
        server, _ = make_server(FakeExecutor(stdout=json.dumps(record)))
        self.assertEqual(call(server, "get_reminder", {"reminder_id": 77})["structuredContent"], record)

    def test_moved_reminder_reports_its_new_id(self):
        moved = {"status": "updated", "id": 90, "oldId": 77, "method": "clone-delete", "list": "Work"}
        server, _ = make_server(FakeExecutor(stdout=json.dumps(moved)))
        structured = call(server, "update_reminder", {"reminder_id": 77, "list": "Work"})["structuredContent"]
        self.assertEqual((structured["id"], structured["oldId"]), (90, 77))


class StdioEndToEndTests(unittest.TestCase):
    def test_real_server_lists_the_new_schemas_and_relays_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-remctl"
            fake.write_text(textwrap.dedent(f"""\
                #!{sys.executable}
                import json, sys
                argv = sys.argv[1:]
                if argv[0] == "search":
                    print(json.dumps({{"items": [], "count": 0, "total": 0, "offset": 0, "limit": 100,
                                      "hasMore": False, "nextOffset": None, "argv": argv}}))
                elif argv[0] == "delete":
                    print(json.dumps({{"status": "partial", "operation": "delete", "succeeded": [1], "failed": [2],
                                      "uncertain": [], "results": [{{"id": 1, "status": "deleted"}},
                                      {{"id": 2, "status": "not_found"}}]}}))
                    sys.exit(1)
                else:
                    sys.exit(2)
            """), encoding="utf-8")
            fake.chmod(0o755)
            env = dict(os.environ, REMCTL_MCP_CLI=str(fake), REMCTL_SKIP_ONBOARD="1", NO_COLOR="1")
            env.pop("GHOSTTY_BIN_DIR", None)
            env.pop("GHOSTTY_RESOURCES_DIR", None)
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "remctl"), "mcp"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
            )
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "invoice", "list_id": 4, "offset": 100}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "delete_reminder", "arguments": {"reminder_ids": [1, 2]}}},
            ]
            stdout, stderr = process.communicate("".join(json.dumps(m) + "\n" for m in messages), timeout=60)
        self.assertEqual(process.returncode, 0, stderr)
        responses = {r["id"]: r for r in (json.loads(line) for line in stdout.splitlines())}
        tools = {tool["name"]: tool for tool in responses[2]["result"]["tools"]}
        for name in ("get_list", "resolve_location", "create_list", "update_list"):
            self.assertIn(name, tools)
        self.assertIn("reminder_ids", tools["delete_reminder"]["inputSchema"]["properties"])
        self.assertIn("private", tools["create_reminder"]["inputSchema"]["properties"])
        self.assertTrue(tools["resolve_location"]["annotations"]["readOnlyHint"])
        search = responses[3]["result"]["structuredContent"]
        self.assertEqual(search["argv"], ["search", "--list-id", "4", "--limit", "100", "--offset", "100", "--json", "--", "invoice"])
        deleted = responses[4]["result"]
        self.assertTrue(deleted["isError"])
        self.assertEqual((deleted["structuredContent"]["succeeded"], deleted["structuredContent"]["failed"]), ([1], [2]))


if __name__ == "__main__":
    unittest.main()
