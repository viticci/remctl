"""Shared-list moves copy and verify before deleting, with explicit MCP confirmation."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest
from unittest import mock

from helpers import load_module
import remctl_mcp
from test_mcp_server import FakeExecutor, make_server, modern_meta, request


remctl = load_module("remctl_reminder_moves_test", "remctl")


def reminder(pk=1, identifier="SOURCE", list_id=7, parent=None):
    return {
        "Z_PK": pk, "ZCKIDENTIFIER": identifier, "ZLIST": list_id,
        "ZPARENTREMINDER": parent, "ZTITLE": "Move me", "ZNOTES": "Keep these notes",
        "ZDUEDATE": 815000000.0, "ZDISPLAYDATEDATE": 815000000.0,
        "ZALLDAY": 0, "ZTIMEZONE": "America/New_York", "ZCOMPLETED": 0,
        "ZCOMPLETIONDATE": None, "ZPRIORITY": 5, "ZFLAGGED": 1,
        "ZISURGENTSTATEENABLEDFORCURRENTUSER": 0,
    }


def bridge_error(message="", **payload):
    return {"returncode": 1, "payload": {"status": "error", "message": message, **payload}}


def call(server, name, arguments):
    return request(server, "tools/call", {
        "_meta": modern_meta(), "name": name, "arguments": arguments,
    })["result"]


class MoveRouteTests(unittest.TestCase):
    @contextlib.contextmanager
    def route(self, sharing_status=0, bridge_result=None, other_args=()):
        parser, subparsers = remctl.build_parser()
        args = remctl.parse_cli_args(parser, subparsers, ["edit", "1", "--list-id", "9", "--json", *other_args])
        source = reminder()
        target = {"id": 9, "title": "Target", "objectUUID": "TARGET", "method": "id",
                  "sharingStatus": sharing_status}
        moved = {"status": "updated", "id": 42, "oldId": 1, "title": source["ZTITLE"],
                 "list": "Target", "objectUUID": "COPY", "subtasksMoved": 0,
                 "method": "clone-delete", "delete": {"status": "deleted"}}
        with (
            mock.patch.object(remctl, "open_db", return_value=mock.Mock()),
            mock.patch.object(remctl, "q_reminder", return_value=source),
            mock.patch.object(remctl, "resolve_required_list_target_or_die", return_value=target),
            mock.patch.object(remctl, "subtask_rows_for_parent_move", return_value=[]),
            mock.patch.object(remctl, "should_carry_absolute_alarm_to_new_due", return_value=False),
            mock.patch.object(remctl, "bridge_available", return_value=True),
            mock.patch.object(remctl, "bridge_call_result", return_value=bridge_result) as bridge,
            mock.patch.object(remctl, "clone_reminder_tree_to_list_or_die", return_value=moved) as clone,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            yield args, bridge, clone, stdout, stderr

    def test_resolved_list_keeps_sharing_status(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE ZREMCDBASELIST (Z_PK INTEGER, ZNAME TEXT, ZCKIDENTIFIER TEXT,
                    ZMARKEDFORDELETION INTEGER, Z_ENT INTEGER, ZSHARINGSTATUS INTEGER);
                INSERT INTO ZREMCDBASELIST VALUES (9, 'Target', 'TARGET', 0, 3, 1);
            """)
            target = remctl.list_ref_payload(remctl.q_lists(db)[0])
        self.assertEqual(target["sharingStatus"], 1)

    def test_shared_targets_bypass_eventkit(self):
        for status in (1, 2):
            with self.subTest(sharing_status=status), self.route(status) as (args, bridge, clone, stdout, _):
                remctl.cmd_edit(args)
                bridge.assert_not_called()
                clone.assert_called_once()
                self.assertEqual(json.loads(stdout.getvalue())["method"], "clone-delete")

    def test_subtask_query_failure_refuses_move_before_writing(self):
        original = remctl.subtask_rows_for_parent_move
        with self.route(1) as (args, bridge, clone, _, stderr), (
            mock.patch.object(remctl, "subtask_rows_for_parent_move", side_effect=original)
        ), mock.patch.object(remctl, "q_subtasks_for_parent", side_effect=sqlite3.OperationalError("read failed")):
            with self.assertRaises(SystemExit):
                remctl.cmd_edit(args)
            bridge.assert_not_called()
            clone.assert_not_called()
            self.assertEqual(json.loads(stderr.getvalue())["code"], "list_move_source_unverified")

    def test_unshared_target_uses_eventkit(self):
        result = {"returncode": 0, "payload": {"status": "updated", "id": "SOURCE"}}
        with self.route(bridge_result=result) as (args, bridge, clone, stdout, _):
            remctl.cmd_edit(args)
            bridge.assert_called_once()
            clone.assert_not_called()
            self.assertEqual(json.loads(stdout.getvalue())["id"], 1)

    def test_reminderkit_3002_falls_back_to_copy(self):
        failures = (
            bridge_error("Update failed: The operation couldn’t be completed. (com.apple.reminderkit error -3002.)"),
            bridge_error("Localized save failure", errorDomain="com.apple.reminderkit", errorCode=-3002),
        )
        for failure in failures:
            with self.subTest(failure=failure), self.route(bridge_result=failure) as (args, bridge, clone, stdout, _):
                remctl.cmd_edit(args)
                bridge.assert_called_once()
                clone.assert_called_once()
                self.assertEqual(json.loads(stdout.getvalue())["oldId"], 1)

    def test_unrelated_or_uncertain_errors_do_not_allow_copy(self):
        failures = (
            bridge_error("Update failed: com.apple.reminderkit error -30020"),
            bridge_error("Update failed: other.domain error -3002"),
            bridge_error("Update failed: generic failure", errorDomain="other.domain", errorCode=-3002),
            bridge_error("EventKit access error: com.apple.reminderkit error -3002"),
            bridge_error("com.apple.reminderkit error -3002", status="timeout"),
            bridge_error("com.apple.reminderkit error -3002", status="missing_bridge"),
            bridge_error("com.apple.reminderkit error -3002", status="exec_error"),
            {"returncode": 0, "payload": {"message": "com.apple.reminderkit error -3002"}},
        )
        for failure in failures:
            with self.subTest(failure=failure):
                self.assertFalse(remctl.bridge_failure_allows_clone_delete_fallback(failure))

    def test_combined_edits_return_typed_error_without_copying(self):
        cases = ((1, None), (0, bridge_error("com.apple.reminderkit error -3002")))
        for status, failure in cases:
            with self.subTest(sharing_status=status), self.route(status, failure, ("--title", "Changed")) as values:
                args, bridge, clone, _, stderr = values
                with self.assertRaises(SystemExit):
                    remctl.cmd_edit(args)
                clone.assert_not_called()
                if status:
                    bridge.assert_not_called()
                error = json.loads(stderr.getvalue())
                self.assertEqual(error["code"], "list_move_requires_separate_edit")
                self.assertIn("reminders_move_to_list", error["message"])
                self.assertNotIn("-3002", error["message"])


class CopyVerificationTests(unittest.TestCase):
    @contextlib.contextmanager
    def copy(self, parent_changes=None, child_changes=None, copied_children=None, copy_urls=None):
        source = reminder()
        child = reminder(2, "CHILD", parent=1)
        parent_copy = {**source, "Z_PK": 42, "ZCKIDENTIFIER": "COPY", "ZLIST": 9, **(parent_changes or {})}
        child_copy = {**child, "Z_PK": 43, "ZCKIDENTIFIER": "CHILD-COPY", "ZLIST": 9,
                      "ZPARENTREMINDER": 42, **(child_changes or {})}
        mapping = [{"sourceId": "CHILD", "id": "CHILD-COPY"}] if copied_children is None else copied_children
        urls = ["https://example.com/a", "https://example.com/b"]
        events = []

        def read_parent(*_):
            events.append("read-parent")
            return parent_copy

        def read_children(*_, **__):
            events.append("read-children")
            return [child_copy]

        def links(_, pk):
            values = copy_urls if copy_urls is not None and pk in (42, 43) else urls
            return [{"ZURL": url} for url in reversed(values)]

        def private_call(_):
            events.append("copy")
            return {"status": "cloned", "newId": "COPY", "children": mapping}

        def delete(_):
            events.append("delete")
            return {"status": "deleted"}

        with (
            mock.patch.object(remctl, "private_available", return_value=True),
            mock.patch.object(remctl, "private_call", side_effect=private_call),
            mock.patch.object(remctl, "open_db", side_effect=mock.Mock),
            mock.patch.object(remctl, "q_reminder_by_identifier", side_effect=read_parent),
            mock.patch.object(remctl, "q_subtasks_for_parent", side_effect=read_children),
            mock.patch.object(remctl, "q_rich_links", side_effect=links),
            mock.patch.object(remctl, "delete_reminder_by_identifier_or_die", side_effect=delete) as deletion,
            mock.patch.object(remctl.time, "sleep"),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            yield lambda: remctl.clone_reminder_tree_to_list_or_die(
                mock.Mock(), source, {"id": 9, "title": "Target", "objectUUID": "TARGET"}, [child],
            ), deletion, events, stderr

    def assert_copy_refused(self, **changes):
        with self.copy(**changes) as (run, deletion, events, stderr):
            with self.assertRaises(SystemExit):
                run()
            deletion.assert_not_called()
            error = json.loads(stderr.getvalue())
            self.assertEqual(error["code"], "list_move_copy_unverified")
            self.assertFalse(error["originalDeleted"])
            self.assertEqual(error["oldId"], 1)
            self.assertEqual(events[0], "copy")

    def test_parent_metadata_must_match_before_deleting_source(self):
        for field, changed in (
            ("ZTITLE", None), ("ZNOTES", "Lost notes"), ("ZDUEDATE", None),
            ("ZDISPLAYDATEDATE", 0), ("ZALLDAY", 1), ("ZTIMEZONE", "UTC"),
            ("ZCOMPLETED", 1), ("ZCOMPLETIONDATE", 815000001), ("ZPRIORITY", 0),
            ("ZFLAGGED", 0), ("ZISURGENTSTATEENABLEDFORCURRENTUSER", 1), ("ZLIST", 8),
        ):
            with self.subTest(field=field):
                self.assert_copy_refused(parent_changes={field: changed})

    def test_all_urls_must_survive_before_deleting_source(self):
        self.assert_copy_refused(copy_urls=["https://example.com/a"])

    def test_child_identity_parent_list_and_content_must_match(self):
        for field, changed in (
            ("ZCKIDENTIFIER", "UNEXPECTED"), ("ZPARENTREMINDER", 99),
            ("ZLIST", 8), ("ZNOTES", None), ("ZFLAGGED", 0),
        ):
            with self.subTest(field=field):
                self.assert_copy_refused(child_changes={field: changed})

    def test_missing_or_reused_child_mapping_keeps_source(self):
        for mapping in (
            [], [{"sourceId": "OTHER", "id": "CHILD-COPY"}],
            [{"sourceId": "CHILD", "id": "CHILD"}], [{"sourceId": "CHILD", "id": ""}],
        ):
            with self.subTest(mapping=mapping):
                self.assert_copy_refused(copied_children=mapping)

    def test_matching_copy_is_read_back_before_source_deletion(self):
        with self.copy() as (run, deletion, events, stderr):
            result = run()
            self.assertEqual(events, ["copy", "read-parent", "read-children", "delete"])
            deletion.assert_called_once()
            self.assertEqual((result["id"], result["oldId"], result["subtasksMoved"]), (42, 1, 1))
            self.assertEqual(result["method"], "clone-delete")
            self.assertEqual(stderr.getvalue(), "")


class MoveMCPTests(unittest.TestCase):
    def test_move_requires_confirmation_and_is_destructive(self):
        tool = remctl_mcp.TOOLS_BY_NAME["move_to_list"]
        self.assertIn("confirm", tool.input_schema()["required"])
        self.assertTrue(tool.annotations()["destructiveHint"])
        self.assertFalse(tool.annotations()["readOnlyHint"])
        self.assertFalse(tool.annotations()["idempotentHint"])
        for confirmation in ({}, {"confirm": False}):
            with self.subTest(confirmation=confirmation):
                server, executor = make_server()
                result = call(server, "move_to_list", {"reminder_id": 1, "list_id": 9, **confirmation})
                self.assertTrue(result["isError"])
                self.assertEqual(executor.calls, [])
                self.assertIn("confirm", result["structuredContent"]["error"]["message"])

    def test_confirmed_move_returns_new_identity(self):
        moved = {"status": "updated", "id": 42, "oldId": 1, "method": "clone-delete", "originalDeleted": True}
        server, executor = make_server(FakeExecutor(stdout=json.dumps(moved)))
        result = call(server, "move_to_list", {"reminder_id": 1, "list_id": 9, "confirm": True})
        self.assertFalse(result["isError"])
        self.assertEqual(executor.calls[0]["argv"], ["edit", "1", "--list-id", "9", "--json"])
        self.assertEqual(result["structuredContent"]["id"], 42)
        self.assertEqual(result["structuredContent"]["oldId"], 1)

    def test_update_with_list_is_redirected_before_execution(self):
        for selector in ({"list": "Target"}, {"list_id": 9}):
            with self.subTest(selector=selector):
                server, executor = make_server()
                result = call(server, "update_reminder", {"reminder_id": 1, "title": "Renamed", **selector})
                self.assertTrue(result["isError"])
                self.assertEqual(executor.calls, [])
                error = result["structuredContent"]["error"]
                self.assertEqual(error["code"], "move_required")
                self.assertIn("reminders_move_to_list", error["message"])

    def test_copy_verification_error_survives_mcp_mapping(self):
        error = {"status": "error", "code": "list_move_copy_unverified", "message": "The source was kept.",
                 "oldId": 1, "newObjectUUID": "COPY", "originalDeleted": False}
        server, _ = make_server(FakeExecutor(stderr=json.dumps(error), returncode=1))
        result = call(server, "move_to_list", {"reminder_id": 1, "list_id": 9, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "list_move_copy_unverified")
        self.assertFalse(result["structuredContent"]["error"]["originalDeleted"])


if __name__ == "__main__":
    unittest.main()
