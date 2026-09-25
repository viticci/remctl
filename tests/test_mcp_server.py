from __future__ import annotations

import contextlib
import html.parser
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import remctl_mcp
import remctl_runtime
from helpers import ROOT, load_module

MODERN = remctl_mcp.MODERN_PROTOCOL_VERSIONS[0]
PV = remctl_mcp.META_PROTOCOL_VERSION
CC = remctl_mcp.META_CLIENT_CAPABILITIES
APPS_CAPS = {"extensions": {remctl_mcp.UI_EXTENSION_ID: {"mimeTypes": [remctl_mcp.UI_MIME_TYPE]}}}


def modern_meta(capabilities=None):
    return {PV: MODERN, CC: capabilities if capabilities is not None else {}}


class FakeExecutor:
    """Stands in for the subprocess runner; records argv and returns canned output."""

    def __init__(self, stdout="[]", stderr="", returncode=0, timed_out=False):
        self.calls = []
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out
        self.cancelled = []

    def run(self, key, argv, *, timeout, stdin_text=None):
        self.calls.append({"key": key, "argv": list(argv), "timeout": timeout, "stdin": stdin_text})
        return remctl_mcp.CommandResult(list(argv), self.returncode, self.stdout, self.stderr, timed_out=self.timed_out)

    def cancel(self, key):
        self.cancelled.append(key)
        return True

    def reserve(self, key):
        return object()

    def release(self, key, ticket):
        pass


def make_server(executor=None, widget_path=None):
    executor = executor or FakeExecutor()
    config = remctl_mcp.ServerConfig(version="2.0.0-test", executor=executor, widget_path=widget_path)
    return remctl_mcp.MCPServer(config), executor


def request(server, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle_message(message)


class CatalogTests(unittest.TestCase):
    def test_tool_names_are_valid_unique_and_deterministic(self):
        names = [tool.name for tool in remctl_mcp.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, remctl_mcp.TOOL_NAME_PATTERN)
        self.assertEqual(names[0], "today")
        self.assertEqual(names[-1], "run")
        self.assertEqual(names, [tool.name for tool in remctl_mcp.TOOLS])

    def test_descriptors_carry_schemas_and_annotations(self):
        for tool in remctl_mcp.TOOLS:
            descriptor = remctl_mcp.tool_descriptor(tool, ui_meta=None)
            self.assertEqual(descriptor["inputSchema"]["type"], "object")
            self.assertFalse(descriptor["inputSchema"]["additionalProperties"])
            annotations = descriptor["annotations"]
            self.assertEqual(annotations["readOnlyHint"], tool.read_only)
            self.assertEqual(annotations["destructiveHint"], tool.destructive)
            # Only the geocoder lookup reaches a service outside this Mac.
            self.assertEqual(annotations["openWorldHint"], tool.name == "resolve_location")
            self.assertNotIn("_meta", descriptor)
        delete = remctl_mcp.TOOLS_BY_NAME["delete_reminder"]
        self.assertTrue(delete.destructive)
        self.assertFalse(delete.read_only)
        self.assertTrue(remctl_mcp.TOOLS_BY_NAME["today"].read_only)
        self.assertEqual(remctl_mcp.TOOLS_BY_NAME["today"].output_schema, remctl_mcp.ROWS_OUTPUT_SCHEMA)
        self.assertIsNone(remctl_mcp.TOOLS_BY_NAME["run"].output_schema)

    def test_argv_builders_match_the_cli_grammar(self):
        cases = [
            (("today", ()), ["today", "--json"]),
            (("today", (("include_overdue", False),)), ["today", "--no-overdue", "--json"]),
            (("upcoming", (("days", 3),)), ["upcoming", "3", "--json"]),
            (("upcoming", ()), ["upcoming", "7", "--json"]),
            (("overdue", ()), ["overdue", "--json"]),
            (("flagged", ()), ["flagged", "--json"]),
            (("search", (("query", "milk"), ("include_completed", True))), ["search", "--completed", "--limit", "100", "--offset", "0", "--json", "--", "milk"]),
            (("search", (("query", "-urgent"),)), ["search", "--limit", "100", "--offset", "0", "--json", "--", "-urgent"]),
            (("search", (("query", "invoice"), ("list", "-Work"), ("limit", 20), ("offset", 40))), ["search", "--list=-Work", "--limit", "20", "--offset", "40", "--json", "--", "invoice"]),
            (("search", (("query", "invoice"), ("list_id", 153))), ["search", "--list-id", "153", "--limit", "100", "--offset", "0", "--json", "--", "invoice"]),
            (("show_list", (("list", "Work"),)), ["show", "--json", "--", "Work"]),
            (("show_list", (("list_id", 153), ("include_completed", True))), ["show", "--list-id", "153", "--completed", "--json"]),
            (("lists", ()), ["lists", "--json"]),
            (("get_reminder", (("reminder_id", 42),)), ["info", "42", "--json"]),
            (("create_reminder", (("title", "-Leading dash"), ("list", "Work"), ("due", "tomorrow 09:30"), ("priority", "high"), ("flagged", True))), ["add", "--list", "Work", "--due", "tomorrow 09:30", "--priority", "high", "--flag", "--json", "--", "-Leading dash"]),
            (("create_reminder", (("title", "Call Bo"), ("notes", "-> ask about Friday"))), ["add", "--notes=-> ask about Friday", "--json", "--", "Call Bo"]),
            (("update_reminder", (("reminder_id", 7), ("due", "clear"), ("list_id", 9))), ["edit", "7", "--list-id", "9", "--due", "clear", "--json"]),
            (("update_reminder", (("reminder_id", 7), ("title", "-Renamed"), ("alarm", "-15m"))), ["edit", "7", "--title=-Renamed", "--alarm=-15m", "--json"]),
            (("set_completion", (("reminder_id", 7), ("completed", True), ("completion_date", "2026-09-01"))), ["done", "7", "--date", "2026-09-01", "--json"]),
            (("set_completion", (("reminder_id", 7), ("completed", False))), ["undone", "7", "--json"]),
            (("set_flagged", (("reminder_id", 7), ("flagged", False))), ["unflag", "7", "--json"]),
            (("delete_reminder", (("reminder_id", 7),)), ["delete", "7", "--force", "--json"]),
            (("delete_reminder", (("reminder_ids", [7, 8, 7]),)), ["delete", "7", "8", "7", "--batch", "--force", "--json"]),
            (("set_completion", (("reminder_ids", ["7", 8]), ("completed", True))), ["done", "7", "8", "--batch", "--json"]),
            (("set_completion", (("reminder_ids", [7]), ("completed", False))), ["undone", "7", "--batch", "--json"]),
            (("get_list", (("list", "Work"),)), ["list-info", "--json", "--", "Work"]),
            (("get_list", (("list_id", 153),)), ["list-info", "--list-id", "153", "--json"]),
            (("resolve_location", (("query", "Piazza Navona, Rome"),)), ["location-lookup", "--json", "--", "Piazza Navona, Rome"]),
            (("create_list", (("name", "-Errands"), ("color", "blue"))), ["list-create", "--color", "blue", "--json", "--", "-Errands"]),
            (("create_list", (("name", "Food"), ("private", True), ("groceries", True), ("group_id", 4))), ["list-create", "--private", "--groceries", "--group-id", "4", "--json", "--", "Food"]),
            (("update_list", (("list_id", 9), ("new_name", "Chores"))), ["list-rename", "--list-id", "9", "--new-name", "Chores", "--json"]),
            (("update_list", (("list", "Work"), ("emoji", "💼"), ("private", True))), ["list-edit", "--emoji", "💼", "--private", "--json", "--", "Work"]),
            (("doctor", ()), ["doctor", "--for-agent", "--json"]),
            (("run", (("args", ["groups", "--json"]),)), ["groups", "--json"]),
        ]
        for (name, pairs), expected in cases:
            with self.subTest(tool=name, args=pairs):
                tool = remctl_mcp.TOOLS_BY_NAME[name]
                arguments = remctl_mcp.validate_arguments(tool, dict(pairs))
                self.assertEqual(tool.build_argv(arguments), expected)

    def test_json_flag_stays_per_subcommand(self):
        # `--json` before the subcommand exits 2 in remctl's parser; every recipe must end with it.
        for tool in remctl_mcp.TOOLS:
            if tool.name == "run":
                continue
            sample = {param.name: {"integer": 5, "boolean": True, "string": "x", "array": ["a"]}[param.type] for param in tool.params if param.required}
            if tool.name in {"show_list", "get_list"}:
                sample = {"list": "Work"}
            if tool.name in {"set_completion", "delete_reminder"}:
                sample["reminder_id"] = 5
            if tool.name == "update_list":
                sample = {"list": "Work", "new_name": "Office"}
            if tool.name == "update_reminder":
                sample["title"] = "New"
            if tool.name == "create_reminder":
                sample = {"title": "Buy milk"}
            argv = tool.build_argv(remctl_mcp.validate_arguments(tool, sample))
            self.assertNotEqual(argv[0], "--json", argv)
            self.assertIn("--json", argv)

    def test_argument_validation_reports_actionable_errors(self):
        tool = remctl_mcp.TOOLS_BY_NAME["get_reminder"]
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "Missing required argument: reminder_id"):
            remctl_mcp.validate_arguments(tool, {})
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "must be an integer"):
            remctl_mcp.validate_arguments(tool, {"reminder_id": "abc"})
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "at least 1"):
            remctl_mcp.validate_arguments(tool, {"reminder_id": 0})
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "Unknown argument"):
            remctl_mcp.validate_arguments(tool, {"reminder_id": 1, "bogus": 2})
        self.assertEqual(remctl_mcp.validate_arguments(tool, {"reminder_id": "12"}), {"reminder_id": 12})
        show = remctl_mcp.TOOLS_BY_NAME["show_list"]
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "Pass only one of"):
            remctl_mcp.validate_arguments(show, {"list": "Work", "list_id": 1})
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "Pass at least one of"):
            remctl_mcp.validate_arguments(show, {})
        update = remctl_mcp.TOOLS_BY_NAME["update_reminder"]
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "Pass at least one of"):
            remctl_mcp.validate_arguments(update, {"reminder_id": 1})
        create = remctl_mcp.TOOLS_BY_NAME["create_reminder"]
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "must be one of"):
            remctl_mcp.validate_arguments(create, {"title": "x", "priority": "urgent"})
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "NUL"):
            remctl_mcp.validate_arguments(create, {"title": "bad\x00title"})
        completion = remctl_mcp.TOOLS_BY_NAME["set_completion"]
        self.assertEqual(remctl_mcp.validate_arguments(completion, {"reminder_id": 1, "completed": "true"})["completed"], True)
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "true or false"):
            remctl_mcp.validate_arguments(completion, {"reminder_id": 1, "completed": "maybe"})

    def test_run_refuses_interactive_and_recursive_commands(self):
        tool = remctl_mcp.TOOLS_BY_NAME["run"]
        for forbidden in ("mcp", "onboard", "setup", "completion", "permissions", "open"):
            with self.subTest(command=forbidden):
                with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "does not execute"):
                    tool.build_argv(remctl_mcp.validate_arguments(tool, {"args": [forbidden]}))
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "at least one"):
            tool.build_argv(remctl_mcp.validate_arguments(tool, {"args": []}))
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "array of strings"):
            remctl_mcp.validate_arguments(tool, {"args": [1]})

    def test_run_finds_the_command_behind_top_level_options(self):
        # `--format json mcp` runs `mcp`: the value of a top-level option is not the command.
        tool = remctl_mcp.TOOLS_BY_NAME["run"]
        refused = (
            ["--format", "json", "mcp"],
            ["--form", "json", "setup"],
            ["--format=json", "onboard"],
            ["--image-width", "40", "--no-color", "permissions"],
            ["--image-mode", "kitty", "completion", "zsh"],
            ["--", "mcp"],
        )
        for argv in refused:
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "does not execute"):
                    tool.build_argv(remctl_mcp.validate_arguments(tool, {"args": argv}))
        allowed = (
            ["--format", "json", "lists"],
            ["--no-color", "show", "--json", "--", "mcp"],
            ["search", "--json", "--", "setup"],
        )
        for argv in allowed:
            with self.subTest(argv=argv):
                self.assertEqual(tool.build_argv(remctl_mcp.validate_arguments(tool, {"args": argv})), argv)

    def test_set_completion_rejects_a_completion_date_when_reopening(self):
        tool = remctl_mcp.TOOLS_BY_NAME["set_completion"]
        arguments = remctl_mcp.validate_arguments(tool, {"reminder_id": 7, "completed": False, "completion_date": "2026-09-01"})
        with self.assertRaisesRegex(remctl_mcp.ToolArgumentError, "completion_date applies only when completed is true"):
            tool.build_argv(arguments)


class MediaTypeTests(unittest.TestCase):
    def test_apps_media_type_is_compared_after_normalization(self):
        accepted = [
            "text/html;profile=mcp-app",
            'text/html; profile="mcp-app"',
            "TEXT/HTML;Profile=mcp-app",
            "text/html;profile=mcp-app;charset=utf-8",
            "text/html; charset=utf-8; profile=mcp-app",
        ]
        rejected = ["text/html", "text/html;charset=utf-8", "text/html;profile=other", "text/plain;profile=mcp-app", ""]
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(remctl_mcp.is_html_app_media_type(value))
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(remctl_mcp.is_html_app_media_type(value))

    def test_client_supports_apps_requires_the_extension_and_mime(self):
        self.assertTrue(remctl_mcp.client_supports_apps(APPS_CAPS))
        self.assertFalse(remctl_mcp.client_supports_apps({"extensions": {remctl_mcp.UI_EXTENSION_ID: {}}}))
        self.assertFalse(remctl_mcp.client_supports_apps({"extensions": {remctl_mcp.UI_EXTENSION_ID: {"mimeTypes": ["text/html"]}}}))
        self.assertFalse(remctl_mcp.client_supports_apps({}))
        self.assertFalse(remctl_mcp.client_supports_apps(None))


class ModernProtocolTests(unittest.TestCase):
    def test_discover_advertises_versions_capabilities_and_identity(self):
        server, _ = make_server()
        response = request(server, "server/discover", {"_meta": modern_meta()})
        result = response["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"], [MODERN])
        self.assertIn("tools", result["capabilities"])
        self.assertIn(remctl_mcp.UI_EXTENSION_ID, result["capabilities"]["extensions"])
        self.assertEqual(result["cacheScope"], "public")
        info = result["_meta"][remctl_mcp.META_SERVER_INFO]
        self.assertEqual(info["name"], "remctl")
        self.assertEqual(info["version"], "2.0.0-test")
        self.assertTrue(result["instructions"])

    def test_discover_without_meta_is_still_answered(self):
        server, _ = make_server()
        response = request(server, "server/discover", {})
        self.assertEqual(response["result"]["supportedVersions"], [MODERN])

    def test_modern_results_carry_result_type_server_info_and_cache_hints(self):
        server, _ = make_server()
        result = request(server, "tools/list", {"_meta": modern_meta()})["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["ttlMs"], remctl_mcp.LIST_TTL_MS)
        self.assertEqual(result["cacheScope"], "public")
        self.assertEqual(result["_meta"][remctl_mcp.META_SERVER_INFO]["name"], "remctl")
        self.assertNotIn("icons", result["_meta"][remctl_mcp.META_SERVER_INFO])
        self.assertEqual([tool["name"] for tool in result["tools"]], [tool.name for tool in remctl_mcp.TOOLS])
        ping = request(server, "ping", {"_meta": modern_meta()})["result"]
        self.assertEqual(ping["resultType"], "complete")

    def test_unsupported_version_returns_32022_with_supported_list(self):
        server, _ = make_server()
        response = request(server, "tools/list", {"_meta": {PV: "2025-11-25", CC: {}}})
        self.assertEqual(response["error"]["code"], remctl_mcp.ERR_UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(response["error"]["data"], {"supported": [MODERN], "requested": "2025-11-25"})

    def test_partial_modern_envelope_is_invalid_params_with_hint(self):
        server, _ = make_server()
        for meta in ({PV: MODERN}, {CC: {}}):
            with self.subTest(meta=meta):
                response = request(server, "tools/list", {"_meta": meta})
                self.assertEqual(response["error"]["code"], remctl_mcp.ERR_INVALID_PARAMS)
                self.assertEqual(response["error"]["data"], {"supported": [MODERN]})

    def test_modern_initialize_is_refused_with_supported_versions(self):
        server, _ = make_server()
        response = request(server, "initialize", {"_meta": modern_meta(), "protocolVersion": MODERN})
        self.assertEqual(response["error"]["code"], remctl_mcp.ERR_METHOD_NOT_FOUND)
        self.assertEqual(response["error"]["data"], {"supported": [MODERN]})

    def test_unknown_method_and_unsupported_methods(self):
        server, _ = make_server()
        for method in ("nope/method", "tasks/get", "completion/complete", "subscriptions/listen", "logging/setLevel"):
            with self.subTest(method=method):
                response = request(server, method, {"_meta": modern_meta()})
                self.assertEqual(response["error"]["code"], remctl_mcp.ERR_METHOD_NOT_FOUND)

    def test_apps_linkage_is_capability_scoped_for_modern_clients(self):
        server, _ = make_server()
        without = request(server, "tools/list", {"_meta": modern_meta()})["result"]["tools"][0]
        self.assertNotIn("_meta", without)
        with_apps = request(server, "tools/list", {"_meta": modern_meta(APPS_CAPS)})["result"]["tools"][0]
        self.assertEqual(with_apps["_meta"]["ui"], {"resourceUri": remctl_mcp.UI_RESOURCE_URI, "visibility": ["model", "app"]})
        self.assertEqual(with_apps["_meta"]["ui/resourceUri"], remctl_mcp.UI_RESOURCE_URI)
        self.assertEqual(with_apps["_meta"]["openai/outputTemplate"], remctl_mcp.UI_RESOURCE_URI)
        self.assertTrue(with_apps["_meta"]["openai/widgetAccessible"])
        self.assertEqual(request(server, "resources/list", {"_meta": modern_meta()})["result"]["resources"], [])
        listed = request(server, "resources/list", {"_meta": modern_meta(APPS_CAPS)})["result"]["resources"]
        self.assertEqual([item["uri"] for item in listed], [remctl_mcp.UI_RESOURCE_URI])
        self.assertEqual(listed[0]["mimeType"], remctl_mcp.UI_MIME_TYPE)
        self.assertEqual(listed[0]["_meta"]["ui"], remctl_mcp.UI_POLICY)

    def test_resource_read_serves_widget_with_policy_and_not_found_uses_32602(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget = Path(tmp) / "widget.html"
            widget.write_text("<!DOCTYPE html><html><body>widget</body></html>", encoding="utf-8")
            server, _ = make_server(widget_path=widget)
            result = request(server, "resources/read", {"_meta": modern_meta(), "uri": remctl_mcp.UI_RESOURCE_URI})["result"]
            content = result["contents"][0]
            self.assertEqual(content["mimeType"], remctl_mcp.UI_MIME_TYPE)
            self.assertIn("widget", content["text"])
            self.assertEqual(content["_meta"]["ui"], remctl_mcp.UI_POLICY)
            self.assertIn("openai/widgetCSP", content["_meta"])
            self.assertEqual(result["cacheScope"], "public")
            missing = request(server, "resources/read", {"_meta": modern_meta(), "uri": "ui://remctl/nope"})
            self.assertEqual(missing["error"]["code"], remctl_mcp.ERR_INVALID_PARAMS)
            self.assertEqual(missing["error"]["data"], {"uri": "ui://remctl/nope"})
            templates = request(server, "resources/templates/list", {"_meta": modern_meta()})["result"]
            self.assertEqual(templates["resourceTemplates"], [])

    def test_prompts(self):
        server, _ = make_server()
        listed = request(server, "prompts/list", {"_meta": modern_meta()})["result"]
        self.assertEqual([item["name"] for item in listed["prompts"]], ["daily_review", "plan_week"])
        got = request(server, "prompts/get", {"_meta": modern_meta(), "name": "plan_week", "arguments": {"days": "3"}})["result"]
        self.assertIn("days=3", got["messages"][0]["content"]["text"])
        self.assertEqual(got["messages"][0]["role"], "user")
        fallback = request(server, "prompts/get", {"_meta": modern_meta(), "name": "plan_week", "arguments": {"days": "900"}})["result"]
        self.assertIn("days=7", fallback["messages"][0]["content"]["text"])
        unknown = request(server, "prompts/get", {"_meta": modern_meta(), "name": "nope"})
        self.assertEqual(unknown["error"]["code"], remctl_mcp.ERR_INVALID_PARAMS)


class LegacyProtocolTests(unittest.TestCase):
    def test_initialize_negotiates_known_versions_and_falls_back_to_latest_legacy(self):
        server, _ = make_server()
        for requested, expected in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"), ("1999-01-01", "2025-11-25"), (None, "2025-11-25")):
            with self.subTest(requested=requested):
                params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
                if requested:
                    params["protocolVersion"] = requested
                result = request(server, "initialize", params)["result"]
                self.assertEqual(result["protocolVersion"], expected)
                self.assertEqual(result["serverInfo"]["name"], "remctl")
                self.assertIn(remctl_mcp.UI_EXTENSION_ID, result["capabilities"]["extensions"])
                self.assertNotIn("resultType", result)

    def test_legacy_results_omit_modern_fields(self):
        server, _ = make_server()
        request(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        result = request(server, "tools/list", {})["result"]
        for key in ("resultType", "ttlMs", "cacheScope", "_meta"):
            self.assertNotIn(key, result)
        self.assertEqual(request(server, "ping", {})["result"], {})
        self.assertIsNone(server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_legacy_client_without_apps_gets_aliases_only(self):
        server, _ = make_server()
        request(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        meta = request(server, "tools/list", {})["result"]["tools"][0]["_meta"]
        self.assertNotIn("ui", meta)
        self.assertEqual(meta["ui/resourceUri"], remctl_mcp.UI_RESOURCE_URI)
        self.assertEqual(meta["openai/outputTemplate"], remctl_mcp.UI_RESOURCE_URI)
        self.assertEqual(request(server, "resources/list", {})["result"]["resources"], [])

    def test_legacy_client_with_apps_gets_standard_linkage(self):
        server, _ = make_server()
        request(server, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {"extensions": {remctl_mcp.UI_EXTENSION_ID: {"mimeTypes": ['text/html; profile="mcp-app"']}}}})
        meta = request(server, "tools/list", {})["result"]["tools"][0]["_meta"]
        self.assertEqual(meta["ui"]["resourceUri"], remctl_mcp.UI_RESOURCE_URI)
        self.assertEqual([r["uri"] for r in request(server, "resources/list", {})["result"]["resources"]], [remctl_mcp.UI_RESOURCE_URI])

    def test_legacy_resource_not_found_keeps_32002(self):
        server, _ = make_server()
        request(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        response = request(server, "resources/read", {"uri": "ui://nope"})
        self.assertEqual(response["error"]["code"], remctl_mcp.ERR_LEGACY_RESOURCE_NOT_FOUND)

    def test_batches_parse_errors_and_invalid_requests(self):
        server, _ = make_server()
        batch = server.handle_message([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(batch, [{"jsonrpc": "2.0", "id": 1, "result": {}}])
        self.assertEqual(server.handle_message([])["error"]["code"], remctl_mcp.ERR_INVALID_REQUEST)
        self.assertEqual(server.handle_message({"id": 1, "method": "ping"})["error"]["code"], remctl_mcp.ERR_INVALID_REQUEST)
        self.assertEqual(server.handle_message({"jsonrpc": "2.0", "id": 1, "method": 5})["error"]["code"], remctl_mcp.ERR_INVALID_REQUEST)
        self.assertEqual(server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []})["error"]["code"], remctl_mcp.ERR_INVALID_PARAMS)
        self.assertIsNone(server.handle_message({"jsonrpc": "2.0", "id": 9, "result": {}}))
        self.assertEqual(server.handle_message({"jsonrpc": "2.0", "id": True, "method": "ping"})["error"]["code"], remctl_mcp.ERR_INVALID_REQUEST)


class ToolCallTests(unittest.TestCase):
    def test_array_output_is_wrapped_and_text_matches_structured(self):
        rows = [{"id": 1, "title": "Milk", "list": "Shopping", "completed": False}]
        server, executor = make_server(FakeExecutor(stdout=json.dumps(rows)))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "today", "arguments": {}}, request_id="call-1")["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {"items": rows, "count": 1})
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(result["resultType"], "complete")
        self.assertNotIn("ui", result["_meta"])
        self.assertEqual(executor.calls[0]["argv"], ["today", "--json"])
        self.assertEqual(executor.calls[0]["key"], (server.default_session.request_scope, "call-1"))
        self.assertEqual(executor.calls[0]["timeout"], remctl_mcp.TOOLS_BY_NAME["today"].timeout)

    def test_object_output_passes_through_and_stderr_is_attached(self):
        server, _ = make_server(FakeExecutor(stdout='{"status":"created","id":"abc","numericId":5}', stderr="Warning: something\n"))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "create_reminder", "arguments": {"title": "x"}})["result"]
        self.assertEqual(result["structuredContent"]["id"], 5)
        self.assertEqual(result["structuredContent"]["stderr"], "Warning: something")

    def test_create_reminder_reports_the_numeric_id_as_id(self):
        created = '{"status":"created","id":"3AE94447-1111-2222-3333-444444444444","title":"Milk","numericId":4774}'
        server, _ = make_server(FakeExecutor(stdout=created))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "create_reminder", "arguments": {"title": "Milk"}})["result"]
        structured = result["structuredContent"]
        self.assertEqual(structured["id"], 4774)
        self.assertEqual(structured["cloudKitId"], "3AE94447-1111-2222-3333-444444444444")
        self.assertNotIn("numericId", structured)
        self.assertEqual(list(structured), ["status", "id", "cloudKitId", "title"])
        # The id it hands back is the id every other tool accepts.
        follow_up = remctl_mcp.validate_arguments(
            remctl_mcp.TOOLS_BY_NAME["get_reminder"], {"reminder_id": structured["id"]}
        )
        self.assertEqual(follow_up["reminder_id"], 4774)

    def test_create_reminder_warns_when_the_numeric_id_is_missing(self):
        server, _ = make_server(FakeExecutor(stdout='{"status":"created","id":"3AE94447-1111","title":"Milk"}'))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "create_reminder", "arguments": {"title": "Milk"}})["result"]
        structured = result["structuredContent"]
        self.assertNotIn("id", structured)
        self.assertEqual(structured["cloudKitId"], "3AE94447-1111")
        self.assertIn("numeric_id_unavailable", structured["warnings"][0])

    def test_tools_that_already_report_a_numeric_id_are_untouched(self):
        server, _ = make_server(FakeExecutor(stdout='{"status":"updated","id":4774,"title":"Milk"}'))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "update_reminder", "arguments": {"reminder_id": 4774, "title": "Milk"}})["result"]
        self.assertEqual(result["structuredContent"], {"status": "updated", "id": 4774, "title": "Milk"})

    def test_priority_accepts_apple_numbers_and_still_rejects_nonsense(self):
        tool = remctl_mcp.TOOLS_BY_NAME["create_reminder"]
        for value, expected in ((0, "none"), (1, "high"), (4, "high"), (5, "medium"), (9, "low"), ("5", "medium"), ("high", "high")):
            with self.subTest(value=value):
                arguments = remctl_mcp.validate_arguments(tool, {"title": "x", "priority": value})
                self.assertEqual(arguments["priority"], expected)
        for value in (42, -1, 1.5, True, "urgent"):
            with self.subTest(value=value):
                with self.assertRaises(remctl_mcp.ToolArgumentError):
                    remctl_mcp.validate_arguments(tool, {"title": "x", "priority": value})

    def test_tags_accept_a_list_as_well_as_a_comma_separated_string(self):
        tool = remctl_mcp.TOOLS_BY_NAME["create_reminder"]
        self.assertEqual(remctl_mcp.validate_arguments(tool, {"title": "x", "tags": ["work", " home "]})["tags"], "work,home")
        self.assertEqual(remctl_mcp.validate_arguments(tool, {"title": "x", "tags": "work,home"})["tags"], "work,home")
        with self.assertRaises(remctl_mcp.ToolArgumentError):
            remctl_mcp.validate_arguments(tool, {"title": "x", "tags": [1, 2]})

    def test_apps_client_receives_result_hints_and_actions(self):
        server, _ = make_server(FakeExecutor(stdout="[]"))
        result = request(server, "tools/call", {"_meta": modern_meta(APPS_CAPS), "name": "search", "arguments": {"query": "x"}})["result"]
        hints = result["_meta"][remctl_mcp.UI_RESULT_META_KEY]
        self.assertEqual(hints["profile"], "reminders")
        self.assertEqual(hints["toolName"], "search")
        self.assertEqual({a["toolName"] for a in hints["actions"]}, {"set_completion", "update_reminder", "delete_reminder"})
        self.assertEqual(result["_meta"]["ui"]["resourceUri"], remctl_mcp.UI_RESOURCE_URI)
        change = request(server, "tools/call", {"_meta": modern_meta(APPS_CAPS), "name": "set_flagged", "arguments": {"reminder_id": 1, "flagged": True}})["result"]
        self.assertEqual(change["_meta"][remctl_mcp.UI_RESULT_META_KEY]["profile"], "change")
        self.assertNotIn("actions", change["_meta"][remctl_mcp.UI_RESULT_META_KEY])

    def test_widget_actions_target_real_tools_with_valid_arguments(self):
        for action in remctl_mcp.REMINDER_ROW_ACTIONS:
            with self.subTest(action=action["id"]):
                tool = remctl_mcp.TOOLS_BY_NAME[action["toolName"]]
                substituted = json.loads(json.dumps(action["arguments"]).replace("{id}", "42").replace("{input.due}", "tomorrow").replace("{input.title}", "New title"))
                arguments = remctl_mcp.validate_arguments(tool, substituted)
                argv = tool.build_argv(arguments)
                self.assertEqual(argv[-1], "--json")
                self.assertNotEqual(argv[0], "--json")

    def test_nonzero_exit_with_structured_stderr_becomes_tool_error(self):
        stderr = 'Some noise\n{"status": "error", "code": "invalid_due_date", "message": "bad date"}\n'
        server, _ = make_server(FakeExecutor(stdout="", stderr=stderr, returncode=1))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "create_reminder", "arguments": {"title": "x", "due": "never"}})["result"]
        self.assertTrue(result["isError"])
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "invalid_due_date")
        self.assertEqual(error["exitCode"], 1)
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

    def test_nonzero_exit_with_plain_stderr_and_timeouts(self):
        server, _ = make_server(FakeExecutor(stdout="", stderr="Error: #5 not found\n", returncode=1))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "get_reminder", "arguments": {"reminder_id": 5}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "nonzero_exit")
        self.assertIn("not found", result["structuredContent"]["error"]["message"])
        server, _ = make_server(FakeExecutor(stdout="", stderr="", returncode=-9, timed_out=True))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "set_flagged", "arguments": {"reminder_id": 5, "flagged": True}})["result"]
        self.assertEqual(result["structuredContent"]["error"]["code"], "timeout")

    def test_invalid_arguments_are_tool_errors_and_unknown_tools_are_protocol_errors(self):
        server, executor = make_server()
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "get_reminder", "arguments": {"reminder_id": "x"}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_argument")
        self.assertEqual(executor.calls, [])
        response = request(server, "tools/call", {"_meta": modern_meta(), "name": "nope"})
        self.assertEqual(response["error"]["code"], remctl_mcp.ERR_INVALID_PARAMS)
        self.assertIn("today", response["error"]["data"]["availableTools"])
        self.assertEqual(request(server, "tools/call", {"_meta": modern_meta()})["error"]["code"], remctl_mcp.ERR_INVALID_PARAMS)

    def test_run_passes_stdin_and_cancellation_reaches_the_executor(self):
        server, executor = make_server(FakeExecutor(stdout='{"status":"ok"}'))
        request(server, "tools/call", {"_meta": modern_meta(), "name": "run", "arguments": {"args": ["import", "-", "--json"], "stdin": "[]"}}, request_id=7)
        self.assertEqual(executor.calls[0]["argv"], ["import", "-", "--json"])
        self.assertEqual(executor.calls[0]["stdin"], "[]")
        self.assertEqual(executor.calls[0]["timeout"], 600)
        self.assertIsNone(server.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}))
        self.assertEqual(executor.cancelled, [executor.calls[0]["key"]])


class CommandExecutorTests(unittest.TestCase):
    def test_runs_the_configured_command_with_hardened_environment(self):
        script = textwrap.dedent(
            """
            import json, os, sys
            print(json.dumps({"argv": sys.argv[1:], "skip": os.environ.get("REMCTL_SKIP_ONBOARD"), "color": os.environ.get("NO_COLOR"), "stdin": sys.stdin.read()}))
            """
        )
        executor = remctl_mcp.CommandExecutor([sys.executable, "-c", script])
        result = executor.run("k", ["today", "--json"], timeout=30, stdin_text="hello")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["argv"], ["today", "--json"])
        self.assertEqual(payload["skip"], "1")
        self.assertEqual(payload["color"], "1")
        self.assertEqual(payload["stdin"], "hello")

    def test_timeout_kills_the_child(self):
        executor = remctl_mcp.CommandExecutor([sys.executable, "-c", "import time; time.sleep(30)"])
        started = time.time()
        result = executor.run("k", [], timeout=0.5)
        self.assertTrue(result.timed_out)
        self.assertLess(time.time() - started, 10)

    def test_cancel_terminates_and_marks_the_result(self):
        executor = remctl_mcp.CommandExecutor([sys.executable, "-c", "import time; time.sleep(30)"])
        outcome = {}

        def run():
            outcome["result"] = executor.run("job", [], timeout=30)

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.time() + 5
        while time.time() < deadline and not executor.cancel("job"):
            time.sleep(0.05)
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(outcome["result"].cancelled)
        self.assertFalse(executor.cancel("job"))

    def test_spawn_failure_is_reported_not_raised(self):
        executor = remctl_mcp.CommandExecutor(["/nonexistent/remctl-binary"])
        result = executor.run("k", ["today"], timeout=5)
        self.assertIsNone(result.returncode)
        self.assertIn("could not start", result.stderr)
        tool = remctl_mcp.TOOLS_BY_NAME["today"]
        self.assertEqual(remctl_mcp.tool_result_from_command(tool, result)["structuredContent"]["error"]["code"], "spawn_failed")


class StdioTransportTests(unittest.TestCase):
    def test_loop_writes_one_json_line_per_response_and_exits_on_eof(self):
        server, _ = make_server(FakeExecutor(stdout="[]"))
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": modern_meta()}}),
            "",
            "not json",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"_meta": modern_meta(), "name": "lists", "arguments": {}}}),
        ]
        stdin = io.BufferedReader(io.BytesIO(("\n".join(lines) + "\n").encode("utf-8")))
        stdout = io.BytesIO()
        code = remctl_mcp.serve_stdio(server, stdin=stdin, stdout=stdout)
        self.assertEqual(code, 0)
        output = stdout.getvalue().decode("utf-8")
        self.assertTrue(output.endswith("\n"))
        responses = [json.loads(line) for line in output.splitlines()]
        by_id = {r.get("id"): r for r in responses}
        self.assertIn("supportedVersions", by_id[1]["result"])
        self.assertEqual(by_id[None]["error"]["code"], remctl_mcp.ERR_PARSE)
        self.assertEqual(by_id[2]["result"]["structuredContent"], {"items": [], "count": 0})
        self.assertEqual(len(responses), 3)
        for line in output.splitlines():
            self.assertNotIn("\n", line)

    def test_end_to_end_with_the_real_cli_and_a_fake_remctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-remctl"
            fake.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"today\" ]; then printf '%s' '[{\"id\":1,\"title\":\"E2E\",\"list\":\"Inbox\",\"completed\":false}]'; exit 0; fi\n"
                "echo 'Error: unsupported' >&2; exit 1\n",
                encoding="utf-8",
            )
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
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "today", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "overdue", "arguments": {}}},
            ]
            stdout, stderr = process.communicate("".join(json.dumps(m) + "\n" for m in messages), timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            responses = {r["id"]: r for r in (json.loads(line) for line in stdout.splitlines())}
            self.assertEqual(responses[1]["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(responses[2]["result"]["structuredContent"]["items"][0]["title"], "E2E")
            self.assertTrue(responses[3]["result"]["isError"])
            self.assertIn("unsupported", responses[3]["result"]["structuredContent"]["error"]["message"])


class RegistrationTests(unittest.TestCase):
    def test_server_command_uses_the_absolute_interpreter(self):
        command, args = remctl_mcp.server_command(Path("/Users/x/bin/remctl"))
        self.assertTrue(os.path.isabs(command))
        self.assertEqual(command, remctl_mcp.stable_interpreter())
        self.assertIsNone(remctl_mcp.interpreter_problem(command))
        self.assertEqual(args, ["/Users/x/bin/remctl", "mcp"])

    @staticmethod
    def _homebrew_python(root):
        """A fake Homebrew prefix: the interpreter lives in a versioned keg, reached through opt/ and bin/ links."""
        keg = root / "Cellar" / "python@3.14" / "3.14.7"
        (keg / "bin").mkdir(parents=True)
        real = keg / "bin" / "python3.14"
        real.write_text("#!/bin/sh\n", encoding="utf-8")
        real.chmod(0o755)
        (root / "opt").mkdir()
        (root / "opt" / "python@3.14").symlink_to(keg)
        (root / "bin").mkdir()
        (root / "bin" / "python3").symlink_to(real)
        return real

    def test_stable_interpreter_prefers_the_homebrew_opt_link_over_the_versioned_keg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            real = self._homebrew_python(root)
            opt = str(root / "opt" / "python@3.14" / "bin" / "python3.14")
            self.assertEqual(remctl_mcp.stable_interpreter(str(root / "bin" / "python3")), opt)
            self.assertEqual(remctl_mcp.stable_interpreter(str(real)), opt)
            # Without an opt link that reaches the same file, keep the resolved path.
            (root / "opt" / "python@3.14").unlink()
            self.assertEqual(remctl_mcp.stable_interpreter(str(real)), str(real))
            plain = root / "python3"
            plain.symlink_to(real)
            self.assertEqual(remctl_mcp.stable_interpreter(str(plain)), str(real))

    def test_interpreter_problem_names_interpreters_that_will_stop_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            real = self._homebrew_python(root)
            not_executable = root / "python-data"
            not_executable.write_text("", encoding="utf-8")
            self.assertIsNone(remctl_mcp.interpreter_problem(str(root / "opt" / "python@3.14" / "bin" / "python3.14")))
            self.assertIsNone(remctl_mcp.interpreter_problem(str(root / "bin" / "python3")))
            self.assertEqual(remctl_mcp.interpreter_problem(str(real)), "interpreter_versioned")
            for command in (None, "", 3, str(root / "missing" / "python3"), str(not_executable)):
                with self.subTest(command=command):
                    self.assertEqual(remctl_mcp.interpreter_problem(command), "interpreter_missing")
            with mock.patch.object(remctl_mcp.shutil, "which", side_effect=lambda name: str(root / "bin" / "python3") if name == "python3" else None):
                self.assertIsNone(remctl_mcp.interpreter_problem("python3"))
                self.assertEqual(remctl_mcp.interpreter_problem("python9"), "interpreter_missing")

    def test_registration_status_accepts_any_working_interpreter_and_names_stale_reasons(self):
        # An app registered from another Python is still current; one whose Python
        # is gone, or sits in a keg that `brew upgrade` deletes, is not.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            real = self._homebrew_python(root)
            cli = Path("/Users/x/bin/remctl")
            _, args = remctl_mcp.server_command(cli)
            claude = root / ".claude.json"
            codex = root / "config.toml"
            desktop = root / "desktop.json"
            claude.write_text(json.dumps({"mcpServers": {"remctl": {"command": str(root / "bin" / "python3"), "args": args}}}), encoding="utf-8")
            codex.write_text(f"[mcp_servers.remctl]\ncommand = {json.dumps(str(real))}\nargs = {json.dumps(args)}\n", encoding="utf-8")
            desktop.write_text(json.dumps({"mcpServers": {"remctl": {"command": str(root / "gone" / "python3"), "args": args}}}), encoding="utf-8")
            status = {entry["client"]: entry for entry in remctl_mcp.registration_status(cli, claude_config=claude, codex_config=codex, desktop_config=desktop)}
            self.assertTrue(status["claude-code"]["current"])
            self.assertNotIn("staleReason", status["claude-code"])
            self.assertFalse(status["codex"]["current"])
            self.assertEqual(status["codex"]["staleReason"], "interpreter_versioned")
            self.assertFalse(status["claude-desktop"]["current"])
            self.assertEqual(status["claude-desktop"]["staleReason"], "interpreter_missing")
            moved = {entry["client"]: entry for entry in remctl_mcp.registration_status(Path("/Users/y/bin/remctl"), claude_config=claude, codex_config=codex, desktop_config=desktop)}
            self.assertEqual({entry["staleReason"] for entry in moved.values()}, {"different_cli"})

    def test_claude_code_install_builds_the_documented_command_and_replaces_existing(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ["mcp", "add"] and len(calls) == 1:
                return subprocess.CompletedProcess(argv, 1, "", "MCP server remctl already exists in user config")
            return subprocess.CompletedProcess(argv, 0, "Added stdio MCP server remctl", "")

        with mock.patch.object(remctl_mcp.shutil, "which", side_effect=lambda name: "/opt/claude" if name == "claude" else None):
            result = remctl_mcp.install_claude_code(Path("/Users/x/bin/remctl"), runner=runner)
        self.assertTrue(result["ok"])
        command, args = remctl_mcp.server_command(Path("/Users/x/bin/remctl"))
        self.assertEqual(calls[0], ["/opt/claude", "mcp", "add", "--scope", "user", "--transport", "stdio", "remctl", "--", command, *args])
        self.assertEqual(calls[1], ["/opt/claude", "mcp", "remove", "--scope", "user", "remctl"])
        self.assertEqual(calls[2], calls[0])
        with mock.patch.object(remctl_mcp.shutil, "which", return_value=None):
            self.assertFalse(remctl_mcp.install_claude_code(Path("/x"), runner=runner)["ok"])
        with self.assertRaises(ValueError):
            remctl_mcp.install_claude_code(Path("/x"), scope="global", runner=runner)

    def test_codex_install_builds_the_documented_command(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(remctl_mcp.shutil, "which", side_effect=lambda name: "/opt/codex" if name == "codex" else None):
            result = remctl_mcp.install_codex(Path("/Users/x/bin/remctl"), runner=runner)
            removed = remctl_mcp.remove_codex(runner=runner)
        self.assertTrue(result["ok"])
        command, args = remctl_mcp.server_command(Path("/Users/x/bin/remctl"))
        self.assertEqual(calls[0], ["/opt/codex", "mcp", "add", "remctl", "--", command, *args])
        self.assertTrue(removed["ok"])
        self.assertEqual(calls[1], ["/opt/codex", "mcp", "remove", "remctl"])

    def test_claude_desktop_install_merges_config_preserves_other_keys_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "claude_desktop_config.json"
            config.write_text(json.dumps({"preferences": {"x": 1}, "mcpServers": {"other": {"command": "o"}}}), encoding="utf-8")
            config.chmod(0o600)
            result = remctl_mcp.install_claude_desktop(Path("/Users/x/bin/remctl"), config_path=config)
            self.assertTrue(result["ok"])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600, "the merge must keep the file mode")
            self.assertTrue(Path(result["backup"]).exists())
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(data["preferences"], {"x": 1})
            self.assertEqual(data["mcpServers"]["other"], {"command": "o"})
            command, args = remctl_mcp.server_command(Path("/Users/x/bin/remctl"))
            self.assertEqual(data["mcpServers"]["remctl"], {"command": command, "args": args})
            status = remctl_mcp.registration_status(Path("/Users/x/bin/remctl"), claude_config=Path(tmp) / "nope.json", codex_config=Path(tmp) / "nope.toml", desktop_config=config)
            desktop = next(entry for entry in status if entry["client"] == "claude-desktop")
            self.assertTrue(desktop["configured"])
            self.assertTrue(desktop["current"])
            stale = remctl_mcp.registration_status(Path("/Users/y/bin/remctl"), claude_config=Path(tmp) / "nope.json", codex_config=Path(tmp) / "nope.toml", desktop_config=config)
            self.assertFalse(next(entry for entry in stale if entry["client"] == "claude-desktop")["current"])
            removed = remctl_mcp.remove_claude_desktop(config_path=config)
            self.assertTrue(removed["ok"])
            self.assertNotIn("remctl", json.loads(config.read_text(encoding="utf-8"))["mcpServers"])
            missing = remctl_mcp.install_claude_desktop(Path("/Users/x/bin/remctl"), config_path=Path(tmp) / "fresh.json")
            self.assertTrue(missing["ok"])
            self.assertIsNone(missing["backup"])
            config.write_text("{not json", encoding="utf-8")
            self.assertFalse(remctl_mcp.install_claude_desktop(Path("/Users/x/bin/remctl"), config_path=config)["ok"])

    def test_registration_status_reads_claude_code_and_codex_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = Path(tmp) / ".claude.json"
            codex = Path(tmp) / "config.toml"
            desktop = Path(tmp) / "desktop.json"
            command, args = remctl_mcp.server_command(Path("/Users/x/bin/remctl"))
            claude.write_text(json.dumps({"mcpServers": {"remctl": {"type": "stdio", "command": command, "args": args}}}), encoding="utf-8")
            codex.write_text("[mcp_servers.other]\ncommand = \"x\"\n\n[mcp_servers.remctl]\ncommand = \"y\"\n", encoding="utf-8")
            status = {entry["client"]: entry for entry in remctl_mcp.registration_status(Path("/Users/x/bin/remctl"), claude_config=claude, codex_config=codex, desktop_config=desktop)}
            self.assertTrue(status["claude-code"]["configured"])
            self.assertEqual(status["claude-code"]["scope"], "user")
            self.assertTrue(status["claude-code"]["current"])
            self.assertTrue(status["codex"]["configured"])
            self.assertFalse(status["claude-desktop"]["configured"])
            claude.write_text(json.dumps({"projects": {"/p": {"mcpServers": {"remctl": {"command": "z"}}}}}), encoding="utf-8")
            local = {entry["client"]: entry for entry in remctl_mcp.registration_status(Path("/Users/x/bin/remctl"), claude_config=claude, codex_config=codex, desktop_config=desktop)}
            self.assertEqual(local["claude-code"]["scope"], "local")
            self.assertFalse(local["claude-code"]["current"])

    def test_config_snippets_are_consistent(self):
        snippets = remctl_mcp.config_snippets(Path("/Users/x/bin/remctl"))
        command, args = remctl_mcp.server_command(Path("/Users/x/bin/remctl"))
        self.assertEqual(json.loads(snippets["json"])["mcpServers"]["remctl"], {"command": command, "args": args})
        self.assertIn("[mcp_servers.remctl]", snippets["toml"])
        self.assertIn(command, snippets["command"])
        self.assertTrue(snippets["claude-code"].startswith("claude mcp add --scope user --transport stdio remctl -- "))
        self.assertTrue(snippets["codex"].startswith("codex mcp add remctl -- "))
        self.assertIn(f"  remctl:\n    command: {json.dumps(command)}\n    args: {json.dumps(args)}\n", snippets["hermes"])
        self.assertEqual(remctl_mcp._shell_quote("it's"), "'it'\\''s'")

    def test_bundle_contains_manifest_launcher_and_icon(self):
        with tempfile.TemporaryDirectory() as tmp:
            icon = Path(tmp) / "icon.png"
            icon.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            output = remctl_mcp.build_bundle(Path("/Users/x/bin/remctl"), "2.0.0", Path(tmp) / "out" / "RemCTL.mcpb", icon_path=icon)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertEqual(names, {"manifest.json", "server/remctl-mcp.py", "icon.png"})
                manifest = json.loads(archive.read("manifest.json"))
                launcher = archive.read("server/remctl-mcp.py").decode("utf-8")
                self.assertEqual(archive.getinfo("server/remctl-mcp.py").external_attr >> 16 & 0o777, 0o755)
            self.assertEqual(manifest["manifest_version"], "0.3")
            self.assertEqual(manifest["name"], "remctl")
            self.assertEqual(manifest["version"], "2.0.0")
            self.assertEqual(manifest["server"]["type"], "python")
            self.assertEqual(manifest["server"]["entry_point"], "server/remctl-mcp.py")
            self.assertEqual(manifest["server"]["mcp_config"]["args"], ["${__dirname}/server/remctl-mcp.py"])
            self.assertEqual(manifest["icon"], "icon.png")
            self.assertEqual([tool["name"] for tool in manifest["tools"]], [tool.name for tool in remctl_mcp.TOOLS])
            self.assertEqual(manifest["compatibility"], {"platforms": ["darwin"]})
            self.assertIn("/Users/x/bin/remctl", launcher)
            self.assertIn("os.execv", launcher)
            compile(launcher, "launcher", "exec")


class _AssetTagParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.external_scripts = []
        self.stylesheets = []
        self.iframes = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.external_scripts.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(values.get("href"))
        if tag == "iframe":
            self.iframes.append(values.get("src"))


class WidgetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.widget = remctl_mcp.load_widget_html()

    def test_widget_is_self_contained(self):
        parser = _AssetTagParser()
        parser.feed(self.widget)
        self.assertTrue(self.widget.startswith("<!DOCTYPE html>"))
        self.assertFalse(parser.external_scripts)
        self.assertFalse(parser.stylesheets)
        self.assertFalse(parser.iframes)
        self.assertIsNone(re.search(r"\bfetch\s*\(", self.widget))
        self.assertIsNone(re.search(r"\bXMLHttpRequest\s*\(", self.widget))
        self.assertIsNone(re.search(r"\bnew\s+WebSocket\s*\(", self.widget))
        self.assertNotIn("innerHTML = ", self.widget.replace("t.innerHTML = markup.trim()", ""))
        raw = [byte for byte in self.widget.encode("utf-8") if byte < 0x20 and byte not in (9, 10)]
        self.assertEqual(raw, [], "raw control bytes break the HTML parser (NUL becomes U+FFFD inside a regex)")

    def test_widget_speaks_the_apps_lifecycle_and_reads_result_hints(self):
        for token in (
            '"2026-01-26"', '"ui/initialize"', '"ui/notifications/initialized"', '"ui/notifications/tool-input"',
            '"ui/notifications/tool-result"', '"ui/notifications/tool-cancelled"', '"ui/notifications/host-context-changed"',
            '"ui/notifications/size-changed"', '"ui/resource-teardown"', '"ui/open-link"', '"tools/call"',
            "event.source !== window.parent", "HOST_STYLE_VARIABLES", "safeAreaInsets", "rejectPending(new Error",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.widget)
        self.assertIn(f'"{remctl_mcp.UI_RESULT_META_KEY}"', self.widget)
        for action in remctl_mcp.REMINDER_ROW_ACTIONS:
            self.assertNotIn(action["toolName"] + '"', self.widget.split("remctl-transport")[0], "renderer must not hardcode tool names")


class CliIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_mcp_cli_test", "remctl")

    def test_mcp_is_a_local_command_and_parses(self):
        self.assertIn("mcp", remctl_runtime.LOCAL_COMMANDS)
        parser, subparsers = self.remctl.build_parser()
        self.assertIn("mcp", subparsers.choices)
        args = self.remctl.parse_cli_args(parser, subparsers, ["mcp"])
        self.assertEqual(args.cmd, "mcp")
        self.assertIsNone(args.mcp_action)
        args = self.remctl.parse_cli_args(parser, subparsers, ["mcp", "install", "--client", "codex", "--client", "claude-code", "--json"])
        self.assertEqual(args.client, ["codex", "claude-code"])
        self.assertEqual(args.scope, "user")
        args = self.remctl.parse_cli_args(parser, subparsers, ["mcp", "config", "--format", "toml"])
        self.assertEqual(args.format_kind, "toml")
        args = self.remctl.parse_cli_args(parser, subparsers, ["onboard", "--no-mcp"])
        self.assertTrue(args.no_mcp)

    def test_dash_leading_values_reach_the_real_parser_as_values(self):
        # A title, note, query, or alarm that starts with "-" must not be read as an option.
        parser, subparsers = self.remctl.build_parser()
        cases = (
            ("search", {"query": "-urgent"}, "query", "-urgent"),
            ("create_reminder", {"title": "-Leading dash"}, "title", "-Leading dash"),
            ("create_reminder", {"title": "Call Bo", "notes": "--draft"}, "notes", "--draft"),
            ("update_reminder", {"reminder_id": 7, "title": "-Renamed"}, "title", "-Renamed"),
            ("update_reminder", {"reminder_id": 7, "notes": "-n"}, "notes", "-n"),
            ("update_reminder", {"reminder_id": 7, "alarm": "-15m"}, "alarm", "-15m"),
        )
        for name, arguments, dest, expected in cases:
            with self.subTest(tool=name, arguments=arguments):
                tool = remctl_mcp.TOOLS_BY_NAME[name]
                argv = tool.build_argv(remctl_mcp.validate_arguments(tool, arguments))
                with contextlib.redirect_stderr(io.StringIO()):
                    parsed = self.remctl.parse_cli_args(parser, subparsers, argv)
                self.assertEqual(getattr(parsed, dest), expected)

    def test_run_guard_knows_every_top_level_option(self):
        parser, subparsers = self.remctl.build_parser()
        options = {
            option: action.nargs != 0
            for action in parser._actions
            for option in action.option_strings
            if option.startswith("--")
        }
        self.assertEqual(remctl_mcp.RUN_GLOBAL_OPTIONS, options)
        # The value of a top-level option is not the command: this argv runs `mcp`.
        self.assertEqual(self.remctl.parse_cli_args(parser, subparsers, ["--format", "json", "mcp"]).cmd, "mcp")

    def test_stale_connections_are_grouped_by_reason(self):
        stale = [
            {"name": "Codex", "staleReason": "interpreter_versioned"},
            {"name": "Claude Desktop", "staleReason": "different_cli"},
            {"name": "Claude Code", "staleReason": "interpreter_versioned"},
            {"name": "Older client"},
        ]
        self.assertEqual(
            self.remctl.mcp_stale_clients_text(stale),
            "MCP connection starts a versioned Homebrew Python that `brew upgrade` deletes: Codex, Claude Code; "
            "MCP connection points at a different RemCTL path: Claude Desktop, Older client",
        )
        serving = {"installed": True, "configured": True, "active": True, "url": "https://mac.example.ts.net/remctl"}
        with mock.patch.object(self.remctl.C, "enabled", False):
            self.assertEqual(self.remctl.tailscale_state_text(serving), "serving at https://mac.example.ts.net/remctl")
            self.assertEqual(
                self.remctl.tailscale_state_text({**serving, "agentInterpreterProblem": "interpreter_versioned"}),
                "serving at https://mac.example.ts.net/remctl, but the service starts a versioned Homebrew Python "
                "that `brew upgrade` deletes; rerun `remctl mcp install --client tailscale`",
            )

    def test_status_reports_a_connected_app_whose_cli_is_not_on_path(self):
        # Claude Code and Codex install into ~/.local/bin, which GUI apps,
        # LaunchAgents, and remote runners such as MacRemote often lack on PATH.
        overview = {
            "server": {"command": ["/usr/bin/python3", "/Users/x/bin/remctl", "mcp"]},
            "clients": [
                {"id": "claude-code", "name": "Claude Code", "installed": False, "configured": True, "current": True},
                {"id": "codex", "name": "Codex", "installed": False, "configured": True, "current": False, "staleReason": "different_cli"},
                {"id": "claude-desktop", "name": "Claude Desktop and Cowork", "installed": False, "configured": False, "current": None},
            ],
            "tailscale": {"installed": False},
        }
        out = io.StringIO()
        with mock.patch.object(self.remctl, "mcp_overview", return_value=overview), \
             mock.patch.object(self.remctl.C, "enabled", False), \
             mock.patch.object(sys, "stdout", out):
            self.remctl.cmd_mcp(SimpleNamespace(mcp_action="status", json=False))
        output = out.getvalue()
        self.assertIn("Claude Code: connected\n", output)
        self.assertIn("Codex: connected, but it points at a different RemCTL path", output)
        self.assertIn("Claude Desktop and Cowork: not installed", output)

    def test_completion_scripts_mention_mcp(self):
        for shell in ("zsh", "bash", "fish"):
            with self.subTest(shell=shell):
                self.assertIn("mcp", self.remctl.get_completion_script(shell))

    def test_cmd_mcp_config_and_status_print_without_touching_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with mock.patch.object(remctl_mcp, "CLAUDE_CODE_CONFIG", missing / "claude.json"), \
                 mock.patch.object(remctl_mcp, "CODEX_CONFIG", missing / "config.toml"), \
                 mock.patch.object(remctl_mcp, "CLAUDE_DESKTOP_CONFIG", missing / "desktop.json"), \
                 mock.patch.object(remctl_mcp.shutil, "which", return_value=None):
                out = io.StringIO()
                with mock.patch.object(sys, "stdout", out):
                    self.remctl.cmd_mcp(SimpleNamespace(mcp_action="status", json=True))
                overview = json.loads(out.getvalue())
                self.assertEqual(overview["server"]["command"][1:], [str(self.remctl.mcp_cli_path()), "mcp"])
                self.assertFalse(any(client["configured"] for client in overview["clients"]))
                out = io.StringIO()
                with mock.patch.object(sys, "stdout", out):
                    self.remctl.cmd_mcp(SimpleNamespace(mcp_action="config", json=False, format_kind="json"))
                self.assertIn('"mcpServers"', out.getvalue())
                out = io.StringIO()
                with mock.patch.object(sys, "stdout", out):
                    self.remctl.cmd_mcp(SimpleNamespace(mcp_action="install", json=True, client=["other"], scope="user"))
                payload = json.loads(out.getvalue())
                self.assertTrue(payload["ok"])
                self.assertIn("toml", payload["results"][0]["snippets"])

    def test_cmd_mcp_bundle_writes_to_the_requested_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                self.remctl.cmd_mcp(SimpleNamespace(mcp_action="bundle", json=True, output=str(Path(tmp) / "RemCTL.mcpb"), open=False))
            payload = json.loads(out.getvalue())
            self.assertTrue(zipfile.is_zipfile(payload["path"]))

    def test_doctor_reports_mcp_client_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with mock.patch.object(remctl_mcp, "CLAUDE_CODE_CONFIG", missing / "claude.json"), \
                 mock.patch.object(remctl_mcp, "CODEX_CONFIG", missing / "config.toml"), \
                 mock.patch.object(remctl_mcp, "CLAUDE_DESKTOP_CONFIG", missing / "desktop.json"), \
                 mock.patch.object(remctl_mcp.shutil, "which", side_effect=lambda name: "/opt/codex" if name == "codex" else None):
                overview = self.remctl.mcp_overview()
            codex = next(client for client in overview["clients"] if client["id"] == "codex")
            self.assertTrue(codex["installed"])
            self.assertFalse(codex["configured"])



def http_call(port, method="POST", path="/mcp", body=None, headers=None):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    request_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    request_headers.update(headers or {})
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=request_headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    payload = json.loads(data) if data else None
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, payload


class HTTPTransportTests(unittest.TestCase):
    AUTH = {"Authorization": "Bearer test-token"}
    MODERN_HEADERS = {"Authorization": "Bearer test-token", "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}

    @classmethod
    def setUpClass(cls):
        cls.server, cls.executor = make_server(FakeExecutor(stdout="[]"))
        transport = remctl_mcp.HTTPTransportConfig(token="test-token", allowed_hosts=remctl_mcp.LOOPBACK_HOSTS | {"mac.example.ts.net"})
        cls.httpd = remctl_mcp.make_http_server(cls.server, transport, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_health_needs_no_token_and_works_under_a_mount_path(self):
        for path in ("/health", "/remctl/health"):
            status, _, payload = http_call(self.port, "GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertIn(MODERN, payload["protocolVersions"])

    def test_missing_or_wrong_token_is_401_and_bad_origin_is_403(self):
        status, headers, payload = http_call(self.port, body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 401)
        self.assertIn("bearer", headers["www-authenticate"].lower())
        status, _, _ = http_call(self.port, body={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)
        status, _, _ = http_call(self.port, body={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={**self.AUTH, "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = http_call(self.port, body={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={**self.AUTH, "Origin": "https://mac.example.ts.net"})
        self.assertEqual(status, 200)
        status, _, _ = http_call(self.port, body={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={**self.AUTH, "Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_modern_requests_validate_mirrored_headers(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": modern_meta()}}
        status, headers, payload = http_call(self.port, body=body, headers=self.MODERN_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(headers["mcp-protocol-version"], MODERN)
        self.assertEqual(payload["result"]["resultType"], "complete")
        for broken, fragment in (
            ({**self.MODERN_HEADERS, "MCP-Protocol-Version": None}, "MCP-Protocol-Version"),
            ({**self.MODERN_HEADERS, "Mcp-Method": "ping"}, "Mcp-Method"),
            ({**self.MODERN_HEADERS, "Mcp-Method": None}, "Mcp-Method"),
        ):
            with self.subTest(fragment=fragment):
                clean = {k: v for k, v in broken.items() if v is not None}
                status, _, payload = http_call(self.port, body=body, headers=clean)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], remctl_mcp.ERR_HEADER_MISMATCH)
                self.assertIn(fragment, payload["error"]["message"])
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"_meta": modern_meta(), "name": "lists", "arguments": {}}}
        status, _, payload = http_call(self.port, body=call, headers={**self.MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "lists"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["structuredContent"], {"items": [], "count": 0})
        status, _, payload = http_call(self.port, body=call, headers={**self.MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "today"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], remctl_mcp.ERR_HEADER_MISMATCH)
        encoded = "=?base64?" + __import__("base64").b64encode(b"lists").decode() + "?="
        status, _, _ = http_call(self.port, body=call, headers={**self.MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": encoded})
        self.assertEqual(status, 200)

    def test_modern_error_mapping_and_notifications(self):
        bad_version = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {PV: "2025-11-25", CC: {}}}}
        status, _, payload = http_call(self.port, body=bad_version, headers={**self.MODERN_HEADERS, "MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], remctl_mcp.ERR_UNSUPPORTED_PROTOCOL_VERSION)
        unknown = {"jsonrpc": "2.0", "id": 1, "method": "nope", "params": {"_meta": modern_meta()}}
        status, _, payload = http_call(self.port, body=unknown, headers={**self.MODERN_HEADERS, "Mcp-Method": "nope"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], remctl_mcp.ERR_METHOD_NOT_FOUND)
        notification = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"_meta": modern_meta(), "requestId": 1}}
        status, _, payload = http_call(self.port, body=notification, headers={**self.MODERN_HEADERS, "Mcp-Method": "notifications/cancelled"})
        self.assertEqual(status, 202)
        self.assertIsNone(payload)
        discover = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": modern_meta()}}
        status, _, payload = http_call(self.port, body=discover, headers={**self.MODERN_HEADERS, "Mcp-Method": "server/discover"})
        self.assertEqual(payload["result"]["supportedVersions"], [MODERN])

    def test_legacy_sessions_are_minted_validated_and_deletable(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": APPS_CAPS, "clientInfo": {"name": "t", "version": "1"}}}
        status, headers, payload = http_call(self.port, body=init, headers=self.AUTH)
        self.assertEqual(status, 200)
        session = headers["mcp-session-id"]
        self.assertTrue(session)
        self.assertEqual(payload["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(headers["mcp-protocol-version"], "2025-06-18")
        status, _, _ = http_call(self.port, body={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={**self.AUTH, "Mcp-Session-Id": session})
        self.assertEqual(status, 202)
        status, _, payload = http_call(self.port, body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers={**self.AUTH, "Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual(status, 200)
        self.assertNotIn("resultType", payload["result"])
        self.assertIn("ui", payload["result"]["tools"][0]["_meta"], "the session remembers the negotiated Apps capability")
        status, _, payload = http_call(self.port, body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers={**self.AUTH, "Mcp-Session-Id": "unknown"})
        self.assertEqual(status, 404)
        status, _, payload = http_call(self.port, body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=self.AUTH)
        self.assertEqual(status, 200, "a session-less legacy request is still served")
        self.assertNotIn("ui", payload["result"]["tools"][0]["_meta"])
        status, _, payload = http_call(self.port, body={"jsonrpc": "2.0", "id": 2, "method": "ping"}, headers={**self.AUTH, "MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], remctl_mcp.ERR_UNSUPPORTED_PROTOCOL_VERSION)
        status, _, payload = http_call(self.port, body={"jsonrpc": "2.0", "id": 3, "method": "ping"}, headers={**self.AUTH, "MCP-Protocol-Version": MODERN})
        self.assertEqual(status, 200, "a modern header on an envelope-less body is served as legacy")
        status, _, _ = http_call(self.port, "GET", headers=self.AUTH)
        self.assertEqual(status, 405)
        status, _, _ = http_call(self.port, "DELETE", headers={**self.AUTH, "Mcp-Session-Id": session})
        self.assertEqual(status, 200)
        status, _, _ = http_call(self.port, "DELETE", headers={**self.AUTH, "Mcp-Session-Id": session})
        self.assertEqual(status, 404)

    def test_rejected_request_does_not_poison_a_keep_alive_connection(self):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
        first = connection.getresponse()
        first.read()
        self.assertEqual(first.status, 401)
        connection.request("POST", "/mcp", body=body, headers={**self.AUTH, "Content-Type": "application/json"})
        second = connection.getresponse()
        payload = json.loads(second.read().decode("utf-8"))
        self.assertEqual(second.status, 200, "the rejected connection must close so the client reconnects cleanly")
        self.assertEqual(payload["result"], {})
        connection.close()

    def test_parse_errors_and_batches(self):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("POST", "/mcp", body="{not json", headers={**self.AUTH, "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"]["code"], remctl_mcp.ERR_PARSE)
        connection.close()
        status, _, payload = http_call(self.port, body=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}], headers=self.AUTH)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload], [1, 2])


class HTTPConfigAndTailscaleTests(unittest.TestCase):
    def test_config_is_created_private_and_token_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp-http.json"
            config = remctl_mcp.ensure_http_config(path=path)
            self.assertEqual(config["port"], remctl_mcp.HTTP_DEFAULT_PORT)
            self.assertGreaterEqual(len(config["token"]), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            again = remctl_mcp.ensure_http_config(path=path)
            self.assertEqual(again["token"], config["token"])
            moved = remctl_mcp.ensure_http_config(port=9999, path=path)
            self.assertEqual(moved["port"], 9999)
            rotated = remctl_mcp.rotate_http_token(path=path)
            self.assertNotEqual(rotated["token"], config["token"])
            self.assertEqual(remctl_mcp.load_http_config(path)["token"], rotated["token"])
            path.write_text("{}", encoding="utf-8")
            self.assertIsNone(remctl_mcp.load_http_config(path))

    def test_allowed_hosts_include_tailscale_identity(self):
        config = {"tailscale": {"hostname": "Mac.Example.ts.net", "ips": ["100.1.2.3"]}, "allowedHosts": ["Other.Host"]}
        hosts = remctl_mcp.allowed_hosts_for(config)
        for host in ("127.0.0.1", "localhost", "mac.example.ts.net", "100.1.2.3", "other.host"):
            self.assertIn(host, hosts)
        self.assertEqual(remctl_mcp._host_of("https://Mac.Example.ts.net:443/x"), "mac.example.ts.net")
        self.assertEqual(remctl_mcp._host_of("127.0.0.1:7362"), "127.0.0.1")
        self.assertEqual(remctl_mcp._host_of("[::1]:7362"), "::1")

    def test_tailscale_status_parses_the_cli_json(self):
        payload = {"BackendState": "Running", "Self": {"DNSName": "mac.example.ts.net.", "TailscaleIPs": ["100.1.2.3"]}, "CertDomains": ["mac.example.ts.net"], "CurrentTailnet": {"MagicDNSEnabled": True}}

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with mock.patch.object(remctl_mcp, "tailscale_binary", return_value="/usr/local/bin/tailscale"):
            status = remctl_mcp.tailscale_status(runner=runner)
        self.assertTrue(status["running"])
        self.assertEqual(status["hostname"], "mac.example.ts.net")
        self.assertTrue(status["https"])
        payload["CertDomains"] = []
        with mock.patch.object(remctl_mcp, "tailscale_binary", return_value="/usr/local/bin/tailscale"):
            self.assertFalse(remctl_mcp.tailscale_status(runner=runner)["https"])
        with mock.patch.object(remctl_mcp, "tailscale_binary", return_value=None):
            self.assertFalse(remctl_mcp.tailscale_status()["installed"])

    def test_tailscale_serve_commands_and_mount_parsing(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Web": {"mac.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}, "/remctl": {"Proxy": "http://127.0.0.1:7362"}}}}}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(remctl_mcp, "tailscale_binary", return_value="/opt/tailscale"):
            enabled = remctl_mcp.tailscale_serve_enable(7362, runner=runner)
            mount = remctl_mcp.tailscale_serve_mount(runner=runner)
            disabled = remctl_mcp.tailscale_serve_disable(runner=runner)
        self.assertTrue(enabled["ok"])
        self.assertEqual(calls[0], ["/opt/tailscale", "serve", "--bg", "--https=443", "--set-path=/remctl", "http://127.0.0.1:7362"])
        self.assertEqual(mount["proxy"], "http://127.0.0.1:7362")
        self.assertTrue(disabled["ok"])
        self.assertEqual(calls[-1], ["/opt/tailscale", "serve", "--https=443", "--set-path=/remctl", "off"])

        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "error: HTTPS certs are not enabled for this tailnet")

        with mock.patch.object(remctl_mcp, "tailscale_binary", return_value="/opt/tailscale"):
            failed = remctl_mcp.tailscale_serve_enable(7362, runner=failing)
        self.assertFalse(failed["ok"])
        self.assertIn("admin console", failed["error"])

    def test_tailscale_commands_run_as_the_cli_without_a_terminal(self):
        # The binary inside Tailscale.app acts as the CLI only when TERM or
        # TAILSCALE_BE_CLI is set; the tailnet service and GUI apps have no TERM.
        environments = []

        def runner(argv, **kwargs):
            environments.append(kwargs.get("env"))
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with mock.patch.dict(os.environ, {"REMCTL_TEST_MARKER": "kept"}), \
             mock.patch.object(remctl_mcp, "tailscale_binary", return_value="/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
            os.environ.pop("TERM", None)
            os.environ.pop("TAILSCALE_BE_CLI", None)
            remctl_mcp.tailscale_status(runner=runner)
            remctl_mcp.tailscale_serve_mount(runner=runner)
            remctl_mcp.tailscale_serve_enable(7362, runner=runner)
            remctl_mcp.tailscale_serve_disable(runner=runner)
        self.assertEqual(len(environments), 4)
        for env in environments:
            self.assertEqual(env["TAILSCALE_BE_CLI"], "1")
            self.assertEqual(env["REMCTL_TEST_MARKER"], "kept")

    def test_http_agent_status_reports_an_interpreter_that_will_stop_working(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 113, "", "Could not find service")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            keg = RegistrationTests._homebrew_python(root)
            plist = root / "agent.plist"
            with mock.patch.object(remctl_mcp, "http_agent_plist_path", return_value=plist):
                self.assertNotIn("interpreterProblem", remctl_mcp.http_agent_status(runner=runner))
                cases = (
                    (str(root / "opt" / "python@3.14" / "bin" / "python3.14"), None),
                    (str(keg), "interpreter_versioned"),
                    (str(root / "gone" / "python3"), "interpreter_missing"),
                )
                for command, expected in cases:
                    with self.subTest(command=command):
                        plist.write_bytes(plistlib.dumps({"Label": remctl_mcp.HTTP_AGENT_LABEL, "ProgramArguments": [command, "/Users/x/bin/remctl", "mcp", "serve", "--http"]}))
                        self.assertEqual(remctl_mcp.http_agent_status(runner=runner)["interpreterProblem"], expected)

    def test_reinstalling_a_running_http_agent_waits_for_the_old_job_to_exit(self):
        # bootout returns while launchd is still stopping the job, and a bootstrap
        # in that window fails with "Bootstrap failed: 5: Input/output error".
        calls = []
        stopping = {"checks": 2}

        def launchd(argv, **kwargs):
            action = argv[1]
            calls.append(action)
            if action == "print":
                if stopping["checks"]:
                    stopping["checks"] -= 1
                    return subprocess.CompletedProcess(argv, 0, "state = running", "")
                return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
            if action == "bootstrap" and stopping["checks"]:
                return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5: Input/output error")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(remctl_mcp, "http_agent_log_path", return_value=Path(tmp) / "Logs" / "remctl-mcp-http.log"):
            result = remctl_mcp.install_http_agent(Path("/Users/x/bin/remctl"), runner=launchd, plist_path=Path(tmp) / "agent.plist")
        self.assertTrue(result["ok"], result)
        self.assertEqual(calls, ["bootout", "print", "print", "print", "bootstrap"])

    def test_installing_the_http_agent_restarts_it_only_when_it_was_still_loaded(self):
        # A clean bootstrap starts the job (RunAtLoad). kickstart -k then killed
        # it, and launchd held the restart for its 10-second throttle.
        cases = (
            (0, "", ["bootout", "print", "bootstrap"]),
            (37, "Bootstrap failed: 37: Operation already in progress", ["bootout", "print", "bootstrap", "kickstart"]),
        )
        for code, message, expected in cases:
            calls = []

            def launchd(argv, **kwargs):
                calls.append(argv[1])
                if argv[1] == "print":
                    return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
                if argv[1] == "bootstrap":
                    return subprocess.CompletedProcess(argv, code, "", message)
                return subprocess.CompletedProcess(argv, 0, "", "")

            with self.subTest(bootstrap=code), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(remctl_mcp, "http_agent_log_path", return_value=Path(tmp) / "Logs" / "remctl-mcp-http.log"):
                result = remctl_mcp.install_http_agent(Path("/Users/x/bin/remctl"), runner=launchd, plist_path=Path(tmp) / "agent.plist")
                self.assertTrue(result["ok"], result)
                self.assertEqual(calls, expected)

    def test_http_agent_plist_and_remote_snippets(self):
        plist = remctl_mcp.http_agent_plist(Path("/Users/x/bin/remctl"))
        command, args = remctl_mcp.server_command(Path("/Users/x/bin/remctl"))
        self.assertEqual(plist["Label"], remctl_mcp.HTTP_AGENT_LABEL)
        self.assertEqual(plist["ProgramArguments"], [command, *args, "serve", "--http"])
        self.assertTrue(plist["KeepAlive"])
        config = {"port": 7362, "token": "tok", "tailscale": {"hostname": "mac.example.ts.net", "path": "/remctl"}}
        self.assertEqual(remctl_mcp.tailscale_url(config), "https://mac.example.ts.net/remctl")
        snippets = remctl_mcp.remote_snippets(config)
        self.assertIn("--transport http", snippets["claude-code"])
        self.assertIn("Bearer tok", snippets["claude-code"])
        self.assertIn("--bearer-token-env-var REMCTL_MCP_TOKEN", snippets["codex"])
        desktop = json.loads(snippets["claude-desktop"])
        self.assertEqual(desktop["mcpServers"]["remctl"]["args"][1], "mcp-remote")
        self.assertEqual(json.loads(snippets["json"])["mcpServers"]["remctl"]["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(remctl_mcp.mask_token("abcdefghijklmnop"), "abcd…op")

    def test_install_tailscale_fails_closed_without_prerequisites(self):
        with mock.patch.object(remctl_mcp, "tailscale_status", return_value={"installed": False, "running": False, "hostname": None, "ips": [], "https": False}):
            self.assertIn("not installed", remctl_mcp.install_tailscale(Path("/x"))["error"])
        with mock.patch.object(remctl_mcp, "tailscale_status", return_value={"installed": True, "running": False, "hostname": None, "ips": [], "https": False}):
            self.assertIn("not connected", remctl_mcp.install_tailscale(Path("/x"))["error"])
        with mock.patch.object(remctl_mcp, "tailscale_status", return_value={"installed": True, "running": True, "hostname": "m.ts.net", "ips": [], "https": False}):
            self.assertIn("HTTPS", remctl_mcp.install_tailscale(Path("/x"))["error"])


class OnboardingFlowTests(unittest.TestCase):
    """The guided `remctl onboard` flow, with the host, apps, and Tailscale mocked."""

    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_onboard_test", "remctl")

    def _run(self, *, answers, tailscale, clients, interactive=True, extra_args=()):
        remctl = self.remctl
        checks = [
            {"name": "open_reminders", "status": "ok", "detail": "Reminders is running", "fix": None},
            {"name": "eventkit", "status": "ok", "detail": "authorized", "fix": None},
            {"name": "automation", "status": "ok", "detail": "authorized", "fix": None},
            {"name": "database", "status": "ok", "detail": "authorized", "fix": None},
        ]
        result = {"ok": True, "warnings": 0, "failures": 0, "checks": checks, "capabilityHost": {"available": True, "ready": True, "fullReady": True, "protocolVersion": 2}}
        installs = []

        def fake_install(client, cli_path, **kwargs):
            installs.append(client)
            if client == "tailscale":
                return {"client": "tailscale", "ok": True, "url": "https://mac.example.ts.net/remctl", "port": 7362, "health": {"ok": True},
                        "snippets": {"claude-code": "claude mcp add ... TOKEN", "codex": "export X\ncodex mcp add ..."}}
            return {"client": client, "ok": True, "note": f"{client} note"}

        overview = {"server": {"command": ["python", "remctl", "mcp"]}, "clients": clients, "tailscale": tailscale}
        parser, subparsers = remctl.build_parser()
        args = remctl.parse_cli_args(parser, subparsers, ["onboard", *extra_args])
        out = io.StringIO()
        answer_iter = iter(answers)
        with mock.patch.object(remctl, "run_onboarding", return_value=result), \
             mock.patch.object(remctl, "mcp_overview", return_value=overview), \
             mock.patch.object(remctl, "remctl_tailscale_detect", return_value=tailscale), \
             mock.patch.object(remctl, "mcp_install_client", side_effect=fake_install), \
             mock.patch.object(remctl, "onboarding_today_count", return_value=3), \
             mock.patch.object(remctl, "capability_host_status_snapshot", return_value=result["capabilityHost"]), \
             mock.patch.object(remctl, "capability_host_is_effective", return_value=True), \
             mock.patch.object(remctl, "capability_host_requested_mode", return_value="auto"), \
             mock.patch.object(remctl, "needs_full_disk_access_guidance", return_value=False), \
             mock.patch("builtins.input", side_effect=lambda prompt: (print(prompt, end=""), next(answer_iter))[1]), \
             mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sys.stdin, "isatty", return_value=interactive), \
             mock.patch.object(out, "isatty", return_value=interactive, create=True):
            remctl.C.enabled = False
            remctl.cmd_onboard(args)
        return out.getvalue(), installs

    def test_interactive_flow_asks_per_app_and_offers_tailscale(self):
        clients = [
            {"id": "claude-code", "name": "Claude Code", "installed": True, "configured": True, "current": True},
            {"id": "codex", "name": "Codex", "installed": True, "configured": False, "current": None},
            {"id": "claude-desktop", "name": "Claude Desktop and Cowork", "installed": False, "configured": False, "current": None},
        ]
        tailscale = {"installed": True, "running": True, "hostname": "mac.example.ts.net", "https": True, "configured": False}
        output, installs = self._run(answers=["y", "y"], tailscale=tailscale, clients=clients)
        self.assertIn("Step 1 of 4", output)
        self.assertIn("Step 4 of 4", output)
        self.assertIn("✓ Reminders access", output)
        self.assertIn("✓ Capability Host ready (protocol 2)", output)
        self.assertIn("3 reminders due today or overdue", output)
        self.assertIn("✓ Claude Code: connected", output)
        self.assertIn("Connect Codex? [Y/n]", output)
        self.assertNotIn("Claude Desktop and Cowork:", output, "apps that are not installed are not listed")
        self.assertIn("Set this up now? [y/N]", output)
        self.assertIn("Serving at https://mac.example.ts.net/remctl", output)
        self.assertIn("claude mcp add ... TOKEN", output)
        self.assertEqual(installs, ["codex", "tailscale"])
        self.assertIn("Done.", output)

    def test_declining_everything_leaves_hints_and_skips_when_not_a_tty(self):
        clients = [{"id": "codex", "name": "Codex", "installed": True, "configured": False, "current": None}]
        tailscale = {"installed": True, "running": True, "hostname": "mac.example.ts.net", "https": True, "configured": False}
        output, installs = self._run(answers=["n", "n"], tailscale=tailscale, clients=clients)
        self.assertEqual(installs, [])
        self.assertIn("Skipped Codex. Later: remctl mcp install --client codex", output)
        self.assertIn("Skipped. Later: remctl mcp install --client tailscale", output)
        output, installs = self._run(answers=[], tailscale=tailscale, clients=clients, interactive=False)
        self.assertEqual(installs, [])
        self.assertIn("○ Codex: not connected", output)
        self.assertIn("○ Not set up", output)

    def test_tailscale_step_is_hidden_without_tailscale_and_explains_missing_https(self):
        clients = []
        output, _ = self._run(answers=[], tailscale={"installed": False}, clients=clients)
        self.assertIn("Step 3 of 3", output)
        self.assertNotIn("other devices", output)
        self.assertIn("No supported AI app found", output)
        output, _ = self._run(answers=[], tailscale={"installed": True, "running": True, "hostname": "m.ts.net", "https": False, "configured": False}, clients=clients)
        self.assertIn("no HTTPS certificate", output)
        output, _ = self._run(answers=[], tailscale={"installed": True, "running": True, "hostname": "m.ts.net", "https": True, "configured": True, "active": True, "url": "https://m.ts.net/remctl"}, clients=clients)
        self.assertIn("Already serving at https://m.ts.net/remctl", output)
        output, _ = self._run(answers=[], tailscale={"installed": True, "running": True, "hostname": "m.ts.net", "https": True, "configured": False}, clients=clients, extra_args=["--no-tailscale"])
        self.assertIn("Step 3 of 3", output)
        output, _ = self._run(answers=[], tailscale={"installed": True, "running": True, "hostname": "m.ts.net", "https": True, "configured": False}, clients=clients, extra_args=["--no-mcp"])
        self.assertIn("Step 2 of 2", output)

if __name__ == "__main__":
    unittest.main()
