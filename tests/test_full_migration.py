"""Tests for the full workflow migration of the RemCTL Capability Host.

Covers the operations added/implemented in the migration:
  - snapshot.reminderFull, snapshot.reminderOrder, snapshot.manualSortHint,
    snapshot.subtasksForMove, snapshot.allLists, snapshot.allSmartLists,
    snapshot.allTemplates
  - resolve.group, resolve.smartList, resolve.template
  - snapshot.list, snapshot.smartList, snapshot.template
  - poll.condition

Verifies:
  1. Every declared operation is implemented and closed/default-deny.
  2. Protocol bounds and rejection behaviour for the new operations.
  3. Direct-backend vs Capability-Host handler parity (both routes call the
     same remctl module-level helpers, so their outputs are identical).
  4. Hosted reminder-mutation commands read through the active ReadBackend.
  5. poll.condition bounded polling (met / not-met / met-after-N).
  6. Mutation requests for the new operations are rejected before dispatch.
"""
from __future__ import annotations

import io
import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import remctl_host_protocol as protocol
import remctl_read_broker as broker
import remctl_host_operations as host_ops
from remctl_host_operations import ReadOperations
from helpers import load_module, StaticIdentityValidator


NEW_SNAPSHOT_OPS = [
    "snapshot.reminderFull",
    "snapshot.reminderOrder",
    "snapshot.manualSortHint",
    "snapshot.subtasksForMove",
    "snapshot.allLists",
    "snapshot.allSmartLists",
    "snapshot.allTemplates",
]

PREVIOUSLY_UNIMPLEMENTED_OPS = [
    "resolve.group",
    "resolve.smartList",
    "resolve.template",
    "snapshot.list",
    "snapshot.smartList",
    "snapshot.template",
    "poll.condition",
]

ALL_MIGRATION_OPS = NEW_SNAPSHOT_OPS + PREVIOUSLY_UNIMPLEMENTED_OPS

# Minimal valid input fields (no extras) for each migration operation, used to
# assert closed-schema rejection of extra fields and required-field acceptance.
VALID_FIELDS = {
    "snapshot.reminderFull": {"identifier": 5},
    "snapshot.reminderOrder": {"listId": 7},
    "snapshot.manualSortHint": {"listType": 3, "listUUID": "LIST-UUID"},
    "snapshot.subtasksForMove": {"identifier": 5},
    "snapshot.allLists": {},
    "snapshot.allSmartLists": {},
    "snapshot.allTemplates": {},
    "resolve.group": {"name": "Work"},
    "resolve.smartList": {"identifier": "Flagged"},
    "resolve.template": {"identifier": "Weekly"},
    "snapshot.list": {"identifier": 7},
    "snapshot.smartList": {"identifier": 2},
    "snapshot.template": {"identifier": 3},
    "poll.condition": {"condition": "reminder_exists", "identifier": "CK-1"},
}


def request(operation, **fields):
    return {
        "protocolVersion": protocol.PROTOCOL_VERSION,
        "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
        "requestId": "migration-test",
        "operation": operation,
        **fields,
    }


# ── 1. Protocol: closed schema, implemented, bounded ──────────────────────────

class MigrationProtocolTests(unittest.TestCase):
    def test_all_declared_operations_are_implemented(self):
        for name in protocol.OPERATIONS:
            self.assertIn(
                name,
                protocol.IMPLEMENTED_OPERATIONS,
                f"{name} declared but not marked implemented",
            )

    def test_migration_ops_present_and_implemented(self):
        for name in ALL_MIGRATION_OPS:
            self.assertIn(name, protocol.OPERATIONS)
            self.assertIn(name, protocol.IMPLEMENTED_OPERATIONS)

    def test_every_operation_has_a_handler(self):
        handlers = ReadOperations(mock.Mock()).handlers()
        for name in protocol.IMPLEMENTED_OPERATIONS:
            if name == "health":
                # health is answered directly by the broker dispatch loop, not
                # via the handler registry.
                continue
            self.assertIn(name, handlers, f"{name} has no registered handler")

    def test_valid_requests_accepted(self):
        for name, fields in VALID_FIELDS.items():
            validated = protocol.validate_request(request(name, **fields))
            self.assertEqual(validated["operation"], name)

    def test_closed_schema_rejects_extra_fields(self):
        for name, fields in VALID_FIELDS.items():
            with self.assertRaises(protocol.ProtocolError) as ctx:
                protocol.validate_request(request(name, evilField="x", **fields))
            self.assertEqual(ctx.exception.code, "invalid_request", name)

    def test_reminder_full_requires_identifier(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request("snapshot.reminderFull"))

    def test_identifier_int_lower_bound_enforced(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request("snapshot.reminderFull", identifier=0))

    def test_identifier_text_length_bounded(self):
        oversized = "x" * (protocol.MAX_TEXT_LENGTH + 1)
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                request("snapshot.reminderFull", identifier=oversized)
            )

    def test_reminder_order_listid_bounds(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request("snapshot.reminderOrder", listId=0))
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request("snapshot.reminderOrder"))

    def test_manual_sort_hint_requires_both_fields(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request("snapshot.manualSortHint", listType=1))
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                request("snapshot.manualSortHint", listUUID="U")
            )
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                request("snapshot.manualSortHint", listType=-1, listUUID="U")
            )

    def test_resolve_group_exactly_one_of(self):
        # Both -> reject.
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request("resolve.group", name="Work", id=3))
        # Neither -> reject.
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request("resolve.group"))
        # id alone -> accepted.
        validated = protocol.validate_request(request("resolve.group", id=3))
        self.assertEqual(validated["id"], 3)

    def test_poll_condition_bounds_and_choices(self):
        # Unknown condition -> reject.
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                request("poll.condition", condition="bogus", identifier="CK-1")
            )
        # attempts over cap -> reject.
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                request(
                    "poll.condition",
                    condition="reminder_exists",
                    identifier="CK-1",
                    attempts=101,
                )
            )
        # delay over cap -> reject.
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                request(
                    "poll.condition",
                    condition="reminder_exists",
                    identifier="CK-1",
                    delayMilliseconds=6000,
                )
            )
        # Bounded valid request -> accepted.
        validated = protocol.validate_request(
            request(
                "poll.condition",
                condition="reminder_absent",
                identifier="CK-1",
                expected="absent",
                attempts=3,
                delayMilliseconds=0,
            )
        )
        self.assertEqual(validated["condition"], "reminder_absent")

    def test_all_lists_takes_no_input(self):
        for name in ("snapshot.allLists", "snapshot.allSmartLists", "snapshot.allTemplates"):
            self.assertEqual(protocol.OPERATIONS[name].fields, {})


# ── 2. Direct backend vs Capability Host handler parity ───────────────────────

class DirectHostParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_migration_parity", "remctl")

    def setUp(self):
        self.ops = ReadOperations(self.remctl)
        self.backend = self.remctl.DIRECT_READ_BACKEND

    @staticmethod
    def _fake_db():
        db = mock.Mock()
        db.close.return_value = None
        return db

    def _reminder_row(self, pk=5):
        return {
            "Z_PK": pk,
            "ZCKIDENTIFIER": f"CK-{pk}",
            "ZTITLE": "Buy milk",
            "ZLIST": 7,
            "list_name": "Groceries",
            "ZDUEDATE": None,
            "ZDISPLAYDATEDATE": None,
            "ZALLDAY": 0,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZPRIORITY": 0,
            "ZNOTES": None,
            "ZPARENTREMINDER": None,
            "recurrence_frequency": None,
        }

    def test_reminder_full_parity_present(self):
        row = self._reminder_row(5)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_reminder", side_effect=lambda db, pk: row if pk == 5 else None),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
        ):
            direct = self.backend.typed_reminder_by_pk(5)
            hosted = self.ops.snapshot_reminder_full(request("snapshot.reminderFull", identifier=5))
        self.assertEqual(direct, hosted)
        self.assertTrue(hosted["found"])
        self.assertEqual(hosted["id"], 5)
        self.assertEqual(hosted["Z_PK"], 5)
        self.assertEqual(hosted["identifier"], "CK-5")
        self.assertEqual(hosted["ZCKIDENTIFIER"], "CK-5")
        self.assertEqual(hosted["listName"], "Groceries")
        self.assertFalse(hosted["absoluteAlarmMatchesDue"])
        self.assertFalse(hosted["absoluteAlarmMatchesDueOrDisplay"])

    def test_reminder_full_parity_absent(self):
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_reminder", return_value=None),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
        ):
            direct = self.backend.typed_reminder_by_pk(999)
            hosted = self.ops.snapshot_reminder_full(request("snapshot.reminderFull", identifier=999))
        self.assertIsNone(direct)
        self.assertEqual(hosted, {"found": False})

    def test_reminder_full_accepts_ckid_string(self):
        row = self._reminder_row(9)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_reminder_by_identifier", return_value=row),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
        ):
            hosted = self.ops.snapshot_reminder_full(
                request("snapshot.reminderFull", identifier="CK-9")
            )
        self.assertTrue(hosted["found"])
        self.assertEqual(hosted["Z_PK"], 9)

    def test_reminder_order_parity(self):
        order = ["CK-1", "CK-2", "CK-3"]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_list_reminder_order", return_value=order),
        ):
            direct = self.backend.typed_list_reminder_order(7)
            hosted = self.ops.snapshot_reminder_order(request("snapshot.reminderOrder", listId=7))
        self.assertEqual(direct, order)
        self.assertEqual(hosted, {"order": order})

    def test_subtasks_for_move_parity(self):
        children = [
            {"Z_PK": 11, "ZCKIDENTIFIER": "CK-11", "ZTITLE": "a", "ZCOMPLETED": 0},
            {"Z_PK": 12, "ZCKIDENTIFIER": "CK-12", "ZTITLE": "b", "ZCOMPLETED": 1},
        ]
        parent = self._reminder_row(5)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_reminder", return_value=parent),
            mock.patch.object(self.remctl, "q_subtasks_for_parent", return_value=children),
        ):
            direct = self.backend.typed_subtasks_for_move(5)
            hosted = self.ops.snapshot_subtasks_for_move(
                request("snapshot.subtasksForMove", identifier=5)
            )
        self.assertEqual(hosted["subtasks"], direct)
        self.assertEqual(hosted["count"], 2)
        self.assertEqual(direct[0], {"id": 11, "identifier": "CK-11", "title": "a", "completed": False})
        self.assertEqual(direct[1]["completed"], True)

    def test_manual_sort_hint_parity(self):
        hint = {"objectUUID": "S-1", "listType": 3, "topLevelElementIDs": ["a", "b"]}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_manual_sort_hint", return_value=hint),
        ):
            direct = self.backend.typed_manual_sort_hint(3, "LIST-UUID")
            hosted = self.ops.snapshot_manual_sort_hint(
                request("snapshot.manualSortHint", listType=3, listUUID="LIST-UUID")
            )
        self.assertEqual(direct, hint)
        self.assertEqual(hosted, hint)

    def test_manual_sort_hint_absent(self):
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_manual_sort_hint", return_value=None),
        ):
            direct = self.backend.typed_manual_sort_hint(3, "LIST-UUID")
            hosted = self.ops.snapshot_manual_sort_hint(
                request("snapshot.manualSortHint", listType=3, listUUID="LIST-UUID")
            )
        self.assertIsNone(direct)
        self.assertEqual(hosted, {"found": False})

    def test_all_lists_parity(self):
        lists = [{"id": 1, "title": "Work", "isGroup": False}]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "all_lists_snapshot", return_value=lists),
        ):
            direct = self.backend.typed_all_lists()
            hosted = self.ops.snapshot_all_lists(request("snapshot.allLists"))
        self.assertEqual(direct, lists)
        self.assertEqual(hosted, {"lists": lists})

    def test_all_smart_lists_parity(self):
        rows = ["row-a", "row-b"]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_smart_lists", return_value=rows),
            mock.patch.object(self.remctl, "smart_list_to_dict", side_effect=lambda r: {"name": r}),
        ):
            direct = self.backend.typed_all_smart_lists()
            hosted = self.ops.snapshot_all_smart_lists(request("snapshot.allSmartLists"))
        self.assertEqual(direct, [{"name": "row-a"}, {"name": "row-b"}])
        self.assertEqual(hosted, {"smartLists": direct})

    def test_all_templates_parity(self):
        rows = ["t-a"]
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_templates", return_value=rows),
            mock.patch.object(self.remctl, "template_to_dict", side_effect=lambda r: {"name": r}),
        ):
            direct = self.backend.typed_all_templates()
            hosted = self.ops.snapshot_all_templates(request("snapshot.allTemplates"))
        self.assertEqual(direct, [{"name": "t-a"}])
        self.assertEqual(hosted, {"templates": direct})

    def test_resolve_group_routing_and_parity(self):
        ref = {"id": 3, "title": "Work Group", "isGroup": True}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "resolve_group_ref", return_value=ref) as rg,
        ):
            direct = self.backend.typed_group_ref(group_id=3)
            hosted = self.ops.resolve_group(request("resolve.group", id=3))
        self.assertEqual(direct, ref)
        self.assertEqual(hosted, ref)
        self.assertEqual(rg.call_args.kwargs.get("group_id"), 3)

    def test_resolve_smart_list_routing_int_vs_str(self):
        ref = {"id": 2, "name": "Flagged"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "resolve_smart_list_ref", return_value=ref) as rs,
        ):
            self.ops.resolve_smart_list(request("resolve.smartList", identifier=2))
            self.assertEqual(rs.call_args.kwargs.get("smart_list_id"), 2)
            self.ops.resolve_smart_list(request("resolve.smartList", identifier="Flagged"))
            self.assertEqual(rs.call_args.kwargs.get("name"), "Flagged")

    def test_resolve_template_routing_int_vs_str(self):
        ref = {"id": 4, "name": "Weekly"}
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "resolve_template_ref", return_value=ref) as rt,
        ):
            self.ops.resolve_template(request("resolve.template", identifier=4))
            self.assertEqual(rt.call_args.kwargs.get("template_id"), 4)
            self.ops.resolve_template(request("resolve.template", identifier="Weekly"))
            self.assertEqual(rt.call_args.kwargs.get("name"), "Weekly")

    def test_alarm_carry_flags_helper(self):
        row = self._reminder_row(5)
        with (
            mock.patch.object(self.remctl, "open_db", return_value=self._fake_db()),
            mock.patch.object(self.remctl, "q_reminder", return_value=row),
            mock.patch.object(self.remctl, "q_alarms", return_value=[]),
        ):
            flags = self.backend.typed_alarm_carry_flags(5)
        self.assertEqual(flags, {"matchesDue": False, "matchesDueOrDisplay": False})


# ── 3. poll.condition bounded polling ─────────────────────────────────────────

class PollConditionTests(unittest.TestCase):
    def setUp(self):
        self.m = mock.Mock()
        self.m.open_db.return_value = mock.Mock()
        self.ops = ReadOperations(self.m)
        # Never actually sleep during tests.
        self._sleep = mock.patch.object(host_ops.time, "sleep", return_value=None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def test_reminder_exists_met_immediately(self):
        self.m.q_reminder_by_identifier.return_value = {"Z_PK": 1}
        result = self.ops.poll_condition(
            request("poll.condition", condition="reminder_exists", identifier="CK-1", attempts=5, delayMilliseconds=0)
        )
        self.assertTrue(result["met"])
        self.assertEqual(result["attempts"], 1)
        self.assertTrue(result["value"])

    def test_reminder_absent_times_out(self):
        # Always present -> reminder_absent never met.
        self.m.q_reminder_by_identifier.return_value = {"Z_PK": 1}
        result = self.ops.poll_condition(
            request("poll.condition", condition="reminder_absent", identifier="CK-1", attempts=3, delayMilliseconds=0)
        )
        self.assertFalse(result["met"])
        self.assertEqual(result["attempts"], 3)

    def test_reminder_exists_met_after_retries(self):
        # Absent for two passes, then present on the third.
        self.m.q_reminder_by_identifier.side_effect = [None, None, {"Z_PK": 1}]
        result = self.ops.poll_condition(
            request("poll.condition", condition="reminder_exists", identifier="CK-1", attempts=5, delayMilliseconds=0)
        )
        self.assertTrue(result["met"])
        self.assertEqual(result["attempts"], 3)

    def test_default_attempts_is_twelve(self):
        self.m.q_reminder_by_identifier.return_value = None
        result = self.ops.poll_condition(
            request("poll.condition", condition="reminder_exists", identifier="CK-1", delayMilliseconds=0)
        )
        self.assertFalse(result["met"])
        self.assertEqual(result["attempts"], 12)

    def test_subtask_count_condition(self):
        self.m.q_reminder_by_identifier.return_value = {"Z_PK": 5, "ZLIST": 7, "ZCKIDENTIFIER": "CK-5"}
        self.m.q_subtask_count.return_value = 2
        result = self.ops.poll_condition(
            request("poll.condition", condition="subtask_count", identifier="CK-5", expected=2, attempts=2, delayMilliseconds=0)
        )
        self.assertTrue(result["met"])
        self.assertEqual(result["value"], 2)


# ── 4. Hosted reminder-mutation commands read through the backend ─────────────

class HostedCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remctl = load_module("remctl_migration_hosted", "remctl")

    def setUp(self):
        self._saved = self.remctl.get_read_backend()

        reminder = {
            "Z_PK": 42,
            "ZCKIDENTIFIER": "CK-42",
            "ZTITLE": "Buy milk",
            "list_name": "Groceries",
            "ZLIST": 7,
            "ZDUEDATE": None,
            "recurrence_frequency": None,
        }
        self.reminder = reminder

        class FakeHostBackend(self.remctl.ReadBackend):
            route_name = "host"

            def typed_reminder_by_pk(self, pk):
                return reminder if pk == 42 else None

        self.remctl.set_read_backend(FakeHostBackend())

    def tearDown(self):
        self.remctl.set_read_backend(self._saved)

    def _run(self, fn, args):
        out = io.StringIO()
        with (
            mock.patch.object(self.remctl, "bridge_available", return_value=True),
            contextlib.redirect_stdout(out),
        ):
            fn(args)
        return out.getvalue()

    def test_hosted_done_uses_backend_reminder(self):
        with mock.patch.object(self.remctl, "bridge_call", return_value={"status": "completed"}) as bridge:
            out = self._run(self.remctl.cmd_done, SimpleNamespace(id=42, date=None, json=True))
        self.assertEqual(bridge.call_args.args[0]["id"], "CK-42")
        self.assertIn('"status": "completed"', out)

    def test_hosted_undone_uses_backend_reminder(self):
        with mock.patch.object(self.remctl, "bridge_call", return_value={"status": "uncompleted"}) as bridge:
            out = self._run(self.remctl.cmd_undone, SimpleNamespace(id=42, json=True))
        self.assertEqual(bridge.call_args.args[0]["id"], "CK-42")
        self.assertIn('"status": "uncompleted"', out)

    def test_hosted_flag_uses_backend_reminder(self):
        # flag/unflag are AppleScript-only (EventKit exposes no flagged API).
        with mock.patch.object(
            self.remctl, "osa_set_flagged_result", return_value=(True, None)
        ) as osa:
            out = self._run(self.remctl.cmd_flag, SimpleNamespace(id=42, json=True))
        self.assertEqual(osa.call_args.args[0], "CK-42")
        self.assertIn('"status": "flagged"', out)

    def test_hosted_unflag_uses_backend_reminder(self):
        with mock.patch.object(
            self.remctl, "osa_set_flagged_result", return_value=(True, None)
        ) as osa:
            out = self._run(self.remctl.cmd_unflag, SimpleNamespace(id=42, json=True))
        self.assertEqual(osa.call_args.args[0], "CK-42")
        self.assertIn('"status": "unflagged"', out)

    def test_hosted_delete_uses_backend_reminder(self):
        with (
            mock.patch.object(self.remctl, "confirm_destructive_action", return_value=True),
            mock.patch.object(self.remctl, "bridge_call", return_value={"status": "deleted"}) as bridge,
        ):
            out = self._run(
                self.remctl.cmd_delete, SimpleNamespace(id=42, json=True, force=True, yes=True)
            )
        self.assertEqual(bridge.call_args.args[0]["id"], "CK-42")
        self.assertIn('"status": "deleted"', out)

    def test_hosted_missing_reminder_errors(self):
        err = io.StringIO()
        with (
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as raised,
        ):
            self.remctl.cmd_done(SimpleNamespace(id=999, date=None, json=False))
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("not found", err.getvalue())


# ── 5. New operations refuse mutation requests (default-deny) ──────────────────

class MigrationMutationNegativeTests(unittest.TestCase):
    @staticmethod
    def _broker():
        fake_remctl = mock.Mock()
        return broker.ReadBroker(
            ReadOperations(fake_remctl).handlers(),
            identity_validator=StaticIdentityValidator(),
        )

    def test_mutation_command_names_are_never_hosted(self):
        # The host exposes only read operations: caller mutation verbs must not
        # appear in the protocol schema even after the migration expansion.
        for verb in ("add", "done", "delete", "edit", "flag", "list-create", "group-create"):
            self.assertNotIn(verb, protocol.OPERATIONS)
            self.assertNotIn(verb, protocol.IMPLEMENTED_OPERATIONS)

    def test_mutation_request_rejected_before_dispatch(self):
        import json as _json
        import struct as _struct

        raw = broker.encode_frame(request("add", title="Milk"))
        out = self._broker().handle_frame(raw)
        size = _struct.unpack(">I", out[:4])[0]
        decoded = _json.loads(out[4:4 + size])
        self.assertEqual(decoded["status"], "error")
        self.assertEqual(decoded["code"], "unsupported_operation")

    def test_broker_registers_every_migration_handler(self):
        handlers = self._broker()._handlers
        for name in ALL_MIGRATION_OPS:
            self.assertIn(name, handlers)


if __name__ == "__main__":
    unittest.main()
