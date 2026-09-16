from __future__ import annotations

import html.parser
import io
import json
import os
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
            self.assertFalse(annotations["openWorldHint"])
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
            (("search", (("query", "milk"), ("include_completed", True))), ["search", "milk", "--completed", "--json"]),
            (("show_list", (("list", "Work"),)), ["show", "--json", "--", "Work"]),
            (("show_list", (("list_id", 153), ("include_completed", True))), ["show", "--list-id", "153", "--completed", "--json"]),
            (("lists", ()), ["lists", "--json"]),
            (("get_reminder", (("reminder_id", 42),)), ["info", "42", "--json"]),
            (("create_reminder", (("title", "-Leading dash"), ("list", "Work"), ("due", "tomorrow 09:30"), ("priority", "high"), ("flagged", True))), ["add", "--list", "Work", "--due", "tomorrow 09:30", "--priority", "high", "--flag", "--json", "--", "-Leading dash"]),
            (("update_reminder", (("reminder_id", 7), ("due", "clear"), ("list_id", 9))), ["edit", "7", "--list-id", "9", "--due", "clear", "--json"]),
            (("set_completion", (("reminder_id", 7), ("completed", True), ("completion_date", "2026-09-01"))), ["done", "7", "--date", "2026-09-01", "--json"]),
            (("set_completion", (("reminder_id", 7), ("completed", False))), ["undone", "7", "--json"]),
            (("set_flagged", (("reminder_id", 7), ("flagged", False))), ["unflag", "7", "--json"]),
            (("delete_reminder", (("reminder_id", 7),)), ["delete", "7", "--force", "--json"]),
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
            if tool.name == "show_list":
                sample = {"list": "Work"}
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
        self.assertEqual(executor.calls[0]["key"], "call-1")
        self.assertEqual(executor.calls[0]["timeout"], remctl_mcp.TOOLS_BY_NAME["today"].timeout)

    def test_object_output_passes_through_and_stderr_is_attached(self):
        server, _ = make_server(FakeExecutor(stdout='{"status":"created","id":"abc","numericId":5}', stderr="Warning: something\n"))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "create_reminder", "arguments": {"title": "x"}})["result"]
        self.assertEqual(result["structuredContent"]["numericId"], 5)
        self.assertEqual(result["structuredContent"]["stderr"], "Warning: something")

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
        self.assertEqual(executor.cancelled, [7])


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
        self.assertEqual(args, ["/Users/x/bin/remctl", "mcp"])

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


if __name__ == "__main__":
    unittest.main()
