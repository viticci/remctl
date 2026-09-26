#!/usr/bin/env python3
"""Run an opt-in live matrix against Reminders private ReminderKit writes.

This creates disposable lists, reminders, smart lists, and templates in the
user's live Reminders store, verifies them through remctl JSON output, and
cleans up unless --keep is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PNG_1X1 = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
    "0000000A49444154789C63000100000500010D0A2DB40000000049454E44AE426082"
)


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class LiveMatrix:
    def __init__(
        self,
        remctl: str,
        prefix: str,
        keep: bool = False,
        *,
        standalone_helper: bool = False,
        monotonic=time.monotonic,
        sleep=time.sleep,
        cleanup_quiet_seconds: float = 120,
        cleanup_poll_seconds: float = 5,
        cleanup_max_seconds: float = 300,
    ):
        self.remctl = remctl
        self.prefix = prefix
        self.keep = keep
        self.standalone_helper = standalone_helper
        self.tmpdir = tempfile.TemporaryDirectory(prefix="remctl-private-matrix-")
        self.image_path = Path(self.tmpdir.name) / "pixel.png"
        self.image_path.write_bytes(PNG_1X1)
        self.results: list[dict] = []
        self.created_lists: set[str] = set()
        self.created_smart_lists: set[str] = set()
        self.created_smart_list_ids: dict[int, str] = {}
        self.created_templates: set[str] = set()
        self.created_reminders: set[int] = set()
        self.private_capabilities: dict = {}
        self.monotonic = monotonic
        self.sleep = sleep
        self.cleanup_quiet_seconds = cleanup_quiet_seconds
        self.cleanup_poll_seconds = cleanup_poll_seconds
        self.cleanup_max_seconds = cleanup_max_seconds

    def close(self):
        self.tmpdir.cleanup()

    def record(self, name: str, status: str, detail: str = ""):
        self.results.append({"name": name, "status": status, "detail": detail})

    def command(self, args: list[str], *, expect: int | None = 0, input_text: str | None = None) -> CommandResult:
        proc = subprocess.run(
            [self.remctl, *args],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=60,
        )
        result = CommandResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        if expect is not None and proc.returncode != expect:
            raise AssertionError(
                f"{self.remctl} {' '.join(args)} exited {proc.returncode}, expected {expect}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return result

    def json_command(self, args: list[str]) -> object:
        result = self.command(args)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"Expected JSON from {' '.join(args)}\n{result.stdout}") from exc

    def expect_fail(self, name: str, args: list[str], needle: str):
        result = self.command(args, expect=None)
        output = result.stdout + result.stderr
        if result.returncode == 0:
            raise AssertionError(f"{name}: command unexpectedly succeeded: {' '.join(args)}")
        if needle not in output:
            raise AssertionError(f"{name}: expected {needle!r} in output:\n{output}")
        self.record(name, "passed", needle)

    def retry(self, fn, *, attempts: int = 30, delay: float = 0.25):
        last = None
        for _ in range(attempts):
            last = fn()
            if last:
                return last
            self.sleep(delay)
        return last

    def retry_absent(self, fn, *, attempts: int = 30, delay: float = 0.25) -> bool:
        for _ in range(attempts):
            if not fn():
                return True
            self.sleep(delay)
        return False

    def lists(self) -> list[dict]:
        return self.json_command(["lists", "--json"])

    def list_named(self, name: str) -> dict | None:
        return next((item for item in self.lists() if item.get("title") == name), None)

    def smart_lists(self) -> list[dict]:
        return self.json_command(["smart-lists", "--json"])

    def smart_named(self, name: str) -> dict | None:
        return next((item for item in self.smart_lists() if item.get("name") == name), None)

    def prefixed_custom_smart_lists(self) -> list[dict]:
        return [
            item for item in self.smart_lists()
            if item.get("kind") == "custom"
            and item.get("id") is not None
            and isinstance(item.get("name"), str)
            and item["name"].startswith(self.prefix)
        ]

    def remember_smart_list_identity(self, item: dict) -> bool:
        smart_list_id = int(item["id"])
        current_uuid = str(item.get("objectUUID") or "")
        expected_uuid = self.created_smart_list_ids.get(smart_list_id)
        if expected_uuid and expected_uuid != current_uuid:
            return False
        if expected_uuid is None or (not expected_uuid and current_uuid):
            self.created_smart_list_ids[smart_list_id] = current_uuid
        return True

    def templates(self) -> list[dict]:
        return self.json_command(["templates", "--json"])

    def template_named(self, name: str) -> dict | None:
        return next((item for item in self.templates() if item.get("name") == name), None)

    def show_list(self, name: str) -> list[dict]:
        return self.json_command(["show", name, "--json"])

    def info(self, reminder_id: int) -> dict:
        return self.json_command(["info", str(reminder_id), "--json"])

    def private_helper_path(self) -> Path:
        override = os.environ.get("REMCTL_PRIVATE_PATH")
        if override:
            return Path(override).expanduser().resolve()
        sibling = Path(self.remctl).expanduser().resolve().with_name("remctl-private")
        if sibling.is_file():
            return sibling
        return Path.home() / "bin" / "remctl-private"

    def private_helper_json(self, payload: dict, *, expect: int = 0, retry_transient: bool = False) -> dict:
        if payload.get("action") not in {"capabilities", "protocol_version"} and not self.standalone_helper:
            raise AssertionError("Direct helper writes require --standalone-helper and caller Reminders access")
        helper = self.private_helper_path()
        attempts = 3 if retry_transient else 1
        for attempt in range(attempts):
            proc = subprocess.run(
                [str(helper)],
                input=json.dumps(payload, separators=(",", ":")),
                text=True,
                capture_output=True,
                timeout=60,
            )
            transient = "communicate with a helper application" in (proc.stdout + proc.stderr)
            if not transient or attempt == attempts - 1:
                break
            time.sleep(0.5)
        if proc.returncode != expect:
            raise AssertionError(
                f"{helper} exited {proc.returncode}, expected {expect}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"Expected JSON from {helper}\n{proc.stdout}") from exc

    def create_list(self, name: str, *args: str) -> dict:
        self.json_command(["list-create", name, *args, "--json"])
        self.created_lists.add(name)
        found = self.retry(lambda: self.list_named(name))
        if not found:
            raise AssertionError(f"List did not appear: {name}")
        return found

    def create_reminder(self, title: str, *args: str) -> dict:
        payload = self.json_command(["add", title, *args, "--json"])
        reminder_id = payload.get("numericId")
        if reminder_id:
            self.created_reminders.add(int(reminder_id))
        return payload

    def create_smart_list(self, name: str, *args: str) -> dict:
        self.json_command(["smart-list-create", name, "--private", *args, "--json"])
        self.created_smart_lists.add(name)
        found = self.retry(lambda: self.smart_named(name))
        if not found:
            raise AssertionError(f"Smart list did not appear: {name}")
        if found.get("id") is not None:
            self.created_smart_list_ids[int(found["id"])] = str(found.get("objectUUID") or "")
        return found

    def assert_true(self, condition: bool, message: str):
        if not condition:
            raise AssertionError(message)

    def run_guardrails(self):
        self.expect_fail(
            "guardrail add section without private",
            ["add", f"{self.prefix} Guard Section", "-l", "Work", "--section", "Research", "--json"],
            "require --private",
        )
        self.expect_fail(
            "guardrail edit tags without private",
            ["edit", "0", "-t", "remctl", "--json"],
            "editing synced tags requires --private",
        )
        self.expect_fail(
            "guardrail list symbol without private",
            ["list-create", f"{self.prefix} Bad Symbol", "--symbol", "education3", "--json"],
            "require --private",
        )
        self.expect_fail(
            "guardrail smart list without private",
            ["smart-list-create", f"{self.prefix} Bad Smart", "--flagged", "--json"],
            "requires --private",
        )
        self.expect_fail(
            "guardrail non-materializing untagged smart list",
            ["smart-list-create", f"{self.prefix} Bad Untagged", "--private", "--untagged", "--json"],
            "do not materialize reliably",
        )
        self.expect_fail(
            "guardrail legacy selected tag raw JSON",
            [
                "smart-list-create",
                f"{self.prefix} Bad Legacy Tags",
                "--private",
                "--filter-json",
                '{"hashtags":{"hashtags":["remctl"]}}',
                "--json",
            ],
            "Unsupported smart list filter shape",
        )

    def run_lists_and_reminders(self):
        standard = f"{self.prefix} Standard"
        grocery = f"{self.prefix} Groceries"
        renamed = f"{self.prefix} Renamed"

        created = self.create_list(standard, "--private", "--color", "orange", "--symbol", "education3")
        self.assert_true(created.get("objectUUID"), "private list-create did not produce an objectUUID")
        self.assert_true(created.get("color"), "lists --json did not expose persisted list color")
        self.assert_true(created.get("badge"), "lists --json did not expose persisted list badge")
        self.record("list-create private color and symbol", "passed", standard)

        edit_payload = self.json_command([
            "list-edit",
            standard,
            "--private",
            "--new-name",
            renamed,
            "--color",
            "#30B0C7",
            "--emoji",
            "\U0001f4cc",
            "--json",
        ])
        self.assert_true(edit_payload.get("status") == "updated", "list-edit did not report updated")
        self.created_lists.discard(standard)
        self.created_lists.add(renamed)
        edited = self.retry(lambda: self.list_named(renamed))
        self.assert_true(bool(edited), "renamed list did not appear")
        self.assert_true(edited.get("badge", {}).get("emoji") == "\U0001f4cc", "edited emoji badge did not persist")
        self.record("list-edit rename color and emoji", "passed", renamed)

        self.json_command(["list-pin", renamed, "--private", "--json"])
        pinned = self.retry(lambda: self.list_named(renamed) and self.list_named(renamed).get("pinned"))
        self.assert_true(bool(pinned), "list-pin did not persist")
        self.json_command(["list-unpin", renamed, "--private", "--json"])
        unpinned = self.retry(lambda: self.list_named(renamed) and not self.list_named(renamed).get("pinned"))
        self.assert_true(bool(unpinned), "list-unpin did not persist")
        self.record("list-pin/list-unpin regular list", "passed", renamed)

        order_first = self.create_reminder(f"{self.prefix} Order First", "-l", renamed)
        order_second = self.create_reminder(f"{self.prefix} Order Second", "-l", renamed)
        self.assert_true(order_first.get("numericId") is not None, "first ordering reminder has no numericId")
        self.assert_true(order_second.get("numericId") is not None, "second ordering reminder has no numericId")
        moved = self.json_command([
            "reminder-move",
            str(order_second["numericId"]),
            "--before",
            str(order_first["numericId"]),
            "--private",
            "--json",
        ])
        self.assert_true(moved.get("verified") is True, "reminder-move did not verify the stored order")
        self.assert_true(moved.get("anchorId") == order_first["numericId"], "reminder-move returned the wrong anchor")
        expected_titles = [
            f"{self.prefix} Order Second",
            f"{self.prefix} Order First",
        ]
        shown_order = self.retry(
            lambda: (
                titles
                if (titles := [item.get("title") for item in self.show_list(renamed)])[:2] == expected_titles
                else None
            )
        )
        self.assert_true(bool(shown_order), "show did not reflect the persisted reminder order")
        self.record("reminder-move ordinary list", "passed", "show order: Second, First")

        grocery_row = self.create_list(grocery, "--private", "--groceries", "--grocery-locale", "en_US")
        self.assert_true(grocery_row.get("isGroceries"), "Groceries metadata did not persist")
        self.assert_true(grocery_row.get("grocery", {}).get("locale") == "en_US", "Groceries locale did not persist")
        self.record("list-create groceries", "passed", grocery)

        milk = self.create_reminder(f"{self.prefix} Milk", "-l", grocery, "--private", "--grocery")
        self.assert_true(milk.get("numericId") is not None, "private grocery add did not return numericId")
        shown = self.retry(lambda: [item for item in self.show_list(grocery) if item.get("title") == f"{self.prefix} Milk"])
        self.assert_true(bool(shown), "grocery reminder did not appear in list")
        self.record("add --private --grocery", "passed", str(milk.get("numericId")))

        if self.standalone_helper:
            direct_title = f"{self.prefix} Bananas Direct"
            direct = self.create_reminder(direct_title, "-l", grocery)
            direct_id = int(direct["numericId"])
            direct_info = self.retry(lambda: self.info(direct_id) if self.info(direct_id).get("deepLink") else None)
            self.assert_true(bool(direct_info), "direct grocery test reminder has no stable deep link")
            direct_object_id = direct_info["deepLink"].rstrip("/").rsplit("/", 1)[-1]
            self.assert_true(bool(direct_object_id), "direct grocery test reminder has no object UUID")
            grocery_caps = self.private_capabilities.get("grocery", {})
            legacy_available = grocery_caps.get("categorizeGroceryItemsWithReminderIDs:", {}).get("available") is True
            current_available = grocery_caps.get("autoCategorizeRemindersWithReminderIDs:", {}).get("available") is True
            request = {
                "action": "categorize_grocery_items",
                "listId": grocery_row["objectUUID"],
                "reminderIds": [direct_object_id],
            }
            if legacy_available or current_available:
                expected_selector = (
                    "categorizeGroceryItemsWithReminderIDs:"
                    if legacy_available
                    else "autoCategorizeRemindersWithReminderIDs:"
                )
                categorized = self.private_helper_json(request, retry_transient=True)
                self.assert_true(categorized.get("status") == "updated", "grocery selector call did not update")
                self.assert_true(
                    categorized.get("selector") == expected_selector,
                    "grocery selector dispatch did not match capabilities",
                )
                categorized_row = self.retry(
                    lambda: next(
                        (
                            item
                            for item in self.show_list(grocery)
                            if item.get("title") == direct_title and item.get("section")
                        ),
                        None,
                    )
                )
                self.assert_true(bool(categorized_row), "direct grocery selector did not persist a section")
                self.record(
                    "standalone helper: direct grocery selector dispatch",
                    "passed",
                    f"{expected_selector} -> {categorized_row['section']}",
                )
            else:
                raise AssertionError("host exposes no known grocery categorization selector")
        else:
            self.record(
                "standalone helper: direct grocery selector dispatch",
                "skipped",
                "requires --standalone-helper and caller Reminders access; hosted grocery sorting was tested above",
            )

        child = {
            "title": f"{self.prefix} Child",
            "notes": "Use final crop",
            "due": "tomorrow",
            "url": "https://example.com/child",
            "tags": ["childtag"],
            "earlyReminder": "15m",
        }
        rich = self.create_reminder(
            f"{self.prefix} Rich",
            "-l",
            renamed,
            "-d",
            "tomorrow 10:00",
            "--private",
            "--url",
            "https://example.com",
            "-t",
            "remctl,media",
            "--new-section",
            "Research",
            "--subtask",
            json.dumps(child, separators=(",", ":")),
            "--image",
            str(self.image_path),
            "--urgent",
            "-f",
            "--early-reminder",
            "15m",
        )
        rid = int(rich["numericId"])
        def rich_metadata_ready():
            payload = self.info(rid)
            if (
                payload.get("url") == "https://example.com"
                and {"remctl", "media"}.issubset(set(payload.get("tags", [])))
                and payload.get("flagged") is True
                and payload.get("urgent") is True
                and payload.get("section") == "Research"
                and payload.get("earlyReminder")
                and payload.get("attachments")
                and payload.get("subtasks")
            ):
                return payload
            return None

        info = self.retry(rich_metadata_ready, attempts=40)
        self.assert_true(bool(info), "private rich metadata bundle did not persist")
        self.assert_true(info.get("url") == "https://example.com", "private rich URL did not persist")
        self.assert_true({"remctl", "media"}.issubset(set(info.get("tags", []))), "private tags did not persist")
        self.assert_true(info.get("flagged") is True, "private flag did not persist")
        self.assert_true(info.get("urgent") is True, "urgent state did not persist")
        self.assert_true(info.get("section") == "Research", "new section assignment did not persist")
        self.assert_true(info.get("earlyReminder"), "Early Reminder did not persist")
        self.assert_true(info.get("attachments"), "image attachment did not persist")
        self.assert_true(info.get("subtasks"), "subtask did not persist")
        self.record("add private rich metadata bundle", "passed", str(rid))

        self.json_command([
            "edit",
            str(rid),
            "--private",
            "--no-flagged",
            "--no-urgent",
            "--location-title",
            "Apple Park",
            "--latitude",
            "37.3349",
            "--longitude",
            "-122.0090",
            "--radius",
            "200",
            "--proximity",
            "arriving",
            "--json",
        ])
        def edited_metadata_ready():
            payload = self.info(rid)
            location_alarms = [
                alarm for alarm in payload.get("alarms", [])
                if alarm.get("type") == "location" and alarm.get("location", {}).get("title") == "Apple Park"
            ]
            if (
                payload.get("flagged") is False
                and payload.get("urgent") is False
                and location_alarms
            ):
                return payload
            return None

        edited_info = self.retry(edited_metadata_ready, attempts=40)
        self.assert_true(bool(edited_info), "private flag, urgent, and location edits did not persist")
        location_alarms = [
            alarm for alarm in edited_info.get("alarms", [])
            if alarm.get("type") == "location" and alarm.get("location", {}).get("title") == "Apple Park"
        ]
        self.assert_true(edited_info.get("flagged") is False, "private unflag did not persist")
        self.assert_true(edited_info.get("urgent") is False, "urgent clear did not persist")
        self.assert_true(bool(location_alarms), "location alarm did not persist")
        self.record("edit private flag urgent and location", "passed", str(rid))

        self.json_command(["edit", str(rid), "--private", "--early-reminder", "clear", "--json"])
        def early_reminder_cleared():
            payload = self.info(rid)
            return payload if not payload.get("earlyReminder") else None

        cleared = self.retry(early_reminder_cleared, attempts=40)
        self.assert_true(bool(cleared), "Early Reminder clear did not persist")
        self.assert_true(not cleared.get("earlyReminder"), "Early Reminder clear did not persist")
        self.record("edit private early reminder clear", "passed", str(rid))

        return renamed

    def run_smart_lists(self, include_list: str):
        cases = [
            ("Smart Flagged", ["--flagged"], "Flagged"),
            ("Smart Priority", ["--priority", "high,medium"], "Priority"),
            ("Smart Any Tag", ["--any-tag"], "Any tag"),
            ("Smart Tag Today", ["--tags", "remctl", "--date", "today"], "Tags any selected"),
            ("Smart Or", ["--match", "any", "--priority", "high", "--date", "today"], "Match any"),
            ("Smart Range", ["--date-range", "2026-05-16,2026-05-31", "--color", "red", "--symbol", "education3"], "Date range"),
            ("Smart Time", ["--time", "morning"], "Morning"),
            ("Smart List", ["--include-list", include_list, "--date", "today", "--date-today-include-past-due"], "Lists all"),
            ("Smart Vehicle", ["--vehicle", "connected"], "Getting in the car"),
            ("Smart Location", ["--location-title", "Home", "--latitude", "41.9", "--longitude", "12.5"], "Location"),
        ]
        for suffix, args, expected in cases:
            name = f"{self.prefix} {suffix}"
            row = self.create_smart_list(name, *args)
            summary = row.get("filter", {}).get("description", "")
            self.assert_true(row.get("minimumSupportedVersion") == 20220430, f"{name} missing minimum version")
            self.assert_true(row.get("effectiveMinimumSupportedVersion") == 20220430, f"{name} missing effective version")
            self.assert_true(row.get("filter", {}).get("supported") is True, f"{name} unsupported filter")
            self.assert_true(expected in summary, f"{name} summary {summary!r} missing {expected!r}")
            self.record(f"smart-list-create {suffix}", "passed", summary)

        editable = f"{self.prefix} Smart Editable"
        row = self.create_smart_list(editable, "--flagged")
        self.json_command([
            "smart-list-edit",
            editable,
            "--private",
            "--tags",
            "remctl",
            "--date",
            "today",
            "--color",
            "orange",
            "--emoji",
            "\U0001f3f7",
            "--json",
        ])
        edited = self.retry(lambda: self.smart_named(editable))
        summary = edited.get("filter", {}).get("description", "")
        self.assert_true("Tags any selected: include remctl" in summary, "smart-list-edit selected tag filter did not persist")
        self.assert_true(edited.get("badge", {}).get("emoji") == "\U0001f3f7", "smart-list-edit emoji did not persist")
        self.record("smart-list-edit filter and appearance", "passed", summary)

        baseline = self.smart_named(editable)
        self.assert_true(bool(baseline), "custom smart list disappeared before pin testing")
        smart_id = baseline["id"]
        smart_uuid = baseline["objectUUID"]
        baseline_filter = baseline.get("filterJSON")
        built_in_before = {
            item["id"]: (item.get("pinned"), item.get("pinnedDate"))
            for item in self.smart_lists()
            if item.get("kind") == "built-in"
        }

        def assert_pin_state(expected: bool, label: str) -> dict:
            current = self.retry(
                lambda: (
                    row
                    if (row := self.smart_named(editable))
                    and bool(row.get("pinned")) == expected
                    else None
                )
            )
            self.assert_true(bool(current), f"{label} did not persist")
            self.assert_true(current.get("objectUUID") == smart_uuid, f"{label} changed the smart-list identity")
            self.assert_true(current.get("filterJSON") == baseline_filter, f"{label} changed the smart-list filter")
            if expected:
                self.assert_true(
                    isinstance(current.get("pinnedDate"), (int, float)) and current["pinnedDate"] > 0,
                    f"{label} did not persist a positive pinnedDate",
                )
            else:
                pinned_date = current.get("pinnedDate")
                self.assert_true(
                    pinned_date is None or (isinstance(pinned_date, (int, float)) and pinned_date <= 0),
                    f"{label} left a positive pinnedDate",
                )
            return current

        pin_by_name = self.json_command(["list-pin", editable, "--private", "--json"])
        self.assert_true(pin_by_name.get("kind") == "smart-list", "name pin resolved to the wrong target kind")
        self.assert_true(pin_by_name.get("id") == smart_id, "name pin returned the wrong smart-list ID")
        self.assert_true(pin_by_name.get("private", {}).get("pinned") is True, "name pin helper result is wrong")
        assert_pin_state(True, "custom smart-list pin by name")

        pin_by_id = self.json_command([
            "list-pin",
            "--smart-list-id",
            str(smart_id),
            "--private",
            "--json",
        ])
        self.assert_true(pin_by_id.get("private", {}).get("pinned") is True, "idempotent ID pin helper result is wrong")
        assert_pin_state(True, "idempotent custom smart-list pin by ID")

        unpin_by_id = self.json_command([
            "list-unpin",
            "--smart-list-id",
            str(smart_id),
            "--private",
            "--json",
        ])
        self.assert_true(unpin_by_id.get("private", {}).get("pinned") is False, "ID unpin helper result is wrong")
        assert_pin_state(False, "custom smart-list unpin by ID")

        if self.standalone_helper:
            legacy_pin = self.private_helper_json({
                "action": "set_smart_list_pinned",
                "smartListId": smart_uuid,
                "pinned": True,
            })
            self.assert_true(legacy_pin.get("pinned") is True, "legacy custom smart-list pin result is wrong")
            assert_pin_state(True, "legacy payload custom smart-list pin")
            self.record("standalone helper: protocol-1 pin payload", "passed", str(smart_id))

            legacy_unpin = self.private_helper_json({
                "action": "set_smart_list_pinned",
                "smartListId": smart_uuid,
                "pinned": False,
            })
            self.assert_true(legacy_unpin.get("pinned") is False, "legacy custom smart-list unpin result is wrong")
            assert_pin_state(False, "legacy payload custom smart-list unpin")
            self.record("standalone helper: protocol-1 unpin payload", "passed", str(smart_id))
        else:
            self.record(
                "standalone helper: protocol-1 pin/unpin payloads",
                "skipped",
                "requires --standalone-helper and caller Reminders access; hosted pin/unpin was tested above",
            )

        built_in_after = {
            item["id"]: (item.get("pinned"), item.get("pinnedDate"))
            for item in self.smart_lists()
            if item.get("kind") == "built-in"
        }
        self.assert_true(built_in_after == built_in_before, "custom pin cycles changed a built-in smart list")
        self.record(
            "custom smart-list pinning",
            "passed",
            "name + ID + idempotent pin/unpin with pinnedDate/filter/identity readback",
        )

        built_in = next(
            (item for item in self.smart_lists() if item.get("kind") == "built-in" and item.get("objectUUID")),
            None,
        )
        self.assert_true(bool(built_in), "no built-in smart list is available for pin capability testing")
        generic_fetch = self.private_capabilities.get("store", {}).get("fetchSmartListWithObjectID:error:", {})
        if generic_fetch.get("available") is not True:
            self.expect_fail(
                "built-in smart-list pin unsupported gate",
                ["list-pin", "--smart-list-id", str(built_in["id"]), "--private", "--json"],
                "Built-in smart-list pinning is unsupported on this macOS version",
            )
        else:
            original = bool(built_in.get("pinned"))
            desired = not original
            action = "list-pin" if desired else "list-unpin"
            restore_action = "list-pin" if original else "list-unpin"
            try:
                self.json_command([action, "--smart-list-id", str(built_in["id"]), "--private", "--json"])
                changed = self.retry(
                    lambda: next(
                        (
                            item
                            for item in self.smart_lists()
                            if item.get("id") == built_in["id"] and bool(item.get("pinned")) == desired
                        ),
                        None,
                    )
                )
                self.assert_true(bool(changed), "built-in smart-list pin state did not change")
                self.record("built-in smart-list pin compatibility", "passed", action)
            finally:
                current = next((item for item in self.smart_lists() if item.get("id") == built_in["id"]), None)
                if current and bool(current.get("pinned")) != original:
                    self.json_command([restore_action, "--smart-list-id", str(built_in["id"]), "--private", "--json"])
                    restored = self.retry(
                        lambda: next(
                            (
                                item
                                for item in self.smart_lists()
                                if item.get("id") == built_in["id"] and bool(item.get("pinned")) == original
                            ),
                            None,
                        )
                    )
                    self.assert_true(bool(restored), "built-in smart-list pin state was not restored")

    def run_templates(self, source_list: str):
        name = f"{self.prefix} Template"
        self.json_command(["template-create", name, "--from-list", source_list, "--private", "--json"])
        self.created_templates.add(name)
        template = self.retry(lambda: self.template_named(name))
        self.assert_true(bool(template), "template-create did not persist")
        self.record("template-create", "passed", name)

        applied = self.json_command(["template-apply", name, "--private", "--json"])
        list_name = applied.get("list", {}).get("title") or applied.get("private", {}).get("name") or name
        if list_name:
            self.created_lists.add(list_name)
        found = self.retry(lambda: self.list_named(list_name))
        self.assert_true(bool(found), "template-apply did not create a list")
        items = self.retry(lambda: self.show_list(list_name))
        self.assert_true(items is not None, "template-applied list is not readable")
        self.record("template-apply", "passed", list_name)

    def cleanup(self):
        if self.keep:
            return
        issues: list[str] = []

        # Include prefix-matched rows so a partial create cannot escape tracking.
        try:
            self.created_templates.update(
                item["name"]
                for item in self.templates()
                if isinstance(item.get("name"), str) and item["name"].startswith(self.prefix)
            )
            self.created_smart_lists.update(
                item["name"]
                for item in self.smart_lists()
                if item.get("kind") == "custom"
                and isinstance(item.get("name"), str)
                and item["name"].startswith(self.prefix)
            )
            for item in self.prefixed_custom_smart_lists():
                if not self.remember_smart_list_identity(item):
                    issues.append(f"smart-list id {item['id']} changed UUID before cleanup")
            self.created_lists.update(
                item["title"]
                for item in self.lists()
                if isinstance(item.get("title"), str) and item["title"].startswith(self.prefix)
            )
            search = self.json_command(["search", self.prefix, "--completed", "--json"])
            self.created_reminders.update(
                int(item["numericId"])
                for item in search
                if item.get("numericId") is not None
                and isinstance(item.get("title"), str)
                and item["title"].startswith(self.prefix)
            )
        except Exception as exc:
            issues.append(f"cleanup inventory failed: {exc}")

        for name in sorted(self.created_templates, reverse=True):
            try:
                if self.template_named(name):
                    result = self.command(["template-delete", name, "--private", "--force", "--json"], expect=None)
                    if result.returncode != 0:
                        issues.append(f"template delete failed for {name}: {result.stderr or result.stdout}")
            except Exception as exc:
                issues.append(f"template cleanup failed for {name}: {exc}")
        for smart_list_id in sorted(self.created_smart_list_ids, reverse=True):
            try:
                current = next(
                    (item for item in self.prefixed_custom_smart_lists() if int(item["id"]) == smart_list_id),
                    None,
                )
                if current is None:
                    continue
                if not self.remember_smart_list_identity(current):
                    issues.append(f"smart-list id {smart_list_id} changed UUID before delete")
                    continue
                result = self.command([
                    "smart-list-delete", "--smart-list-id", str(smart_list_id),
                    "--private", "--force", "--json",
                ], expect=None)
                if result.returncode != 0 and "not found" not in (result.stderr + result.stdout).lower():
                    issues.append(f"smart-list delete failed for id {smart_list_id}: {result.stderr or result.stdout}")
            except Exception as exc:
                issues.append(f"smart-list cleanup failed for id {smart_list_id}: {exc}")
        for rid in sorted(self.created_reminders, reverse=True):
            try:
                result = self.command(["delete", str(rid), "--force", "--json"], expect=None)
                if result.returncode != 0:
                    issues.append(f"reminder delete failed for {rid}: {result.stderr or result.stdout}")
            except Exception as exc:
                issues.append(f"reminder cleanup failed for {rid}: {exc}")
        for name in sorted(self.created_lists, reverse=True):
            try:
                if self.list_named(name):
                    result = self.command(["list-delete", name, "--force", "--json"], expect=None)
                    if result.returncode != 0:
                        issues.append(f"list delete failed for {name}: {result.stderr or result.stdout}")
            except Exception as exc:
                issues.append(f"list cleanup failed for {name}: {exc}")

        try:
            clean = self.retry_absent(
                lambda: (
                    any(
                        isinstance(item.get("name"), str) and item["name"].startswith(self.prefix)
                        for item in self.templates()
                    )
                    or any(
                        isinstance(item.get("title"), str) and item["title"].startswith(self.prefix)
                        for item in self.lists()
                    )
                    or any(
                        isinstance(item.get("title"), str) and item["title"].startswith(self.prefix)
                        for item in self.json_command(["search", self.prefix, "--completed", "--json"])
                    )
                )
            )
            if not clean:
                issues.append(f"prefix-matched disposable data remains after cleanup: {self.prefix}")
        except Exception as exc:
            issues.append(f"cleanup readback failed: {exc}")

        # Cloud-backed creates can reappear after one empty local snapshot. Require
        # a sustained empty window and delete only exact numeric IDs discovered
        # under this run's unique prefix if they return.
        try:
            started = self.monotonic()
            empty_since = None
            while True:
                matching = self.prefixed_custom_smart_lists()
                now = self.monotonic()
                if matching:
                    empty_since = None
                    for item in matching:
                        smart_list_id = int(item["id"])
                        if not self.remember_smart_list_identity(item):
                            issues.append(f"smart-list id {smart_list_id} changed UUID after delete")
                            break
                        result = self.command([
                            "smart-list-delete", "--smart-list-id", str(smart_list_id),
                            "--private", "--force", "--json",
                        ], expect=None)
                        if result.returncode != 0 and "not found" not in (result.stderr + result.stdout).lower():
                            issues.append(
                                f"reappeared smart-list delete failed for id {smart_list_id}: "
                                f"{result.stderr or result.stdout}"
                            )
                            break
                elif empty_since is None:
                    empty_since = now
                elif now - empty_since >= self.cleanup_quiet_seconds:
                    break

                if issues or now - started >= self.cleanup_max_seconds:
                    if not issues:
                        issues.append(
                            "smart-list cleanup never reached a sustained empty local readback "
                            f"for {self.cleanup_quiet_seconds:g}s within {self.cleanup_max_seconds:g}s"
                        )
                    break
                self.sleep(self.cleanup_poll_seconds)
        except Exception as exc:
            issues.append(f"sustained smart-list cleanup readback failed: {exc}")

        try:
            final_leftovers = []
            final_leftovers.extend(
                f"template {item['name']}" for item in self.templates()
                if isinstance(item.get("name"), str) and item["name"].startswith(self.prefix)
            )
            final_leftovers.extend(
                f"smart-list {item['name']}" for item in self.prefixed_custom_smart_lists()
            )
            final_leftovers.extend(
                f"list {item['title']}" for item in self.lists()
                if isinstance(item.get("title"), str) and item["title"].startswith(self.prefix)
            )
            final_leftovers.extend(
                f"reminder {item['title']}"
                for item in self.json_command(["search", self.prefix, "--completed", "--json"])
                if isinstance(item.get("title"), str) and item["title"].startswith(self.prefix)
            )
            if final_leftovers:
                issues.append("final prefix inventory is not empty: " + ", ".join(final_leftovers))
        except Exception as exc:
            issues.append(f"final cleanup inventory failed: {exc}")

        if issues:
            raise AssertionError("; ".join(issues))
        self.record(
            "cleanup readback",
            "passed",
            f"smart lists remained absent for {self.cleanup_quiet_seconds:g}s locally; final prefix inventory was empty",
        )

    def run(self):
        doctor = self.json_command(["doctor", "--for-agent", "--json"])
        checks = {item["name"]: item["status"] for item in doctor.get("checks", [])}
        self.assert_true(checks.get("private_helper") == "ok", "private helper is not available")
        effective_route = doctor.get("access", {}).get("effective", {}).get("route", "unknown")
        requested_mode = os.environ.get("REMCTL_CAPABILITY_HOST", "auto")
        if requested_mode.strip().lower() == "force":
            self.assert_true(
                effective_route == "capabilityHost",
                f"force mode did not select the Capability Host (effective route: {effective_route})",
            )
        self.record(
            "CLI execution route",
            "passed",
            f"requested={requested_mode}; effective={effective_route}",
        )
        self.record("CLI doctor private helper", "passed", "ok")
        self.private_capabilities = self.private_helper_json({"action": "capabilities"})
        self.assert_true(self.private_capabilities.get("status") == "ok", "private capability probe failed")
        self.assert_true(self.private_capabilities.get("saveCalled") is False, "capability probe must not save")
        self.record(
            "standalone helper: read-only capability probe",
            "passed",
            self.private_capabilities.get("operatingSystemVersion", "unknown OS"),
        )
        self.run_guardrails()
        source_list = self.run_lists_and_reminders()
        self.run_smart_lists(source_list)
        self.run_templates(source_list)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live private RemCTL command matrix against disposable Reminders data.")
    parser.add_argument("--remctl", default=str(Path(__file__).resolve().parents[1] / "remctl"), help="remctl binary to test")
    parser.add_argument("--prefix", default=f"RemCTL Matrix {datetime.now().strftime('%Y%m%d-%H%M%S')}", help="Disposable item prefix")
    parser.add_argument("--keep", action="store_true", help="Keep disposable Reminders data for manual inspection")
    parser.add_argument("--standalone-helper", action="store_true", help="Also test direct helper writes; requires caller Reminders access, separate from the signed host")
    args = parser.parse_args()

    if args.keep and not args.prefix.strip():
        parser.error("--keep with an empty --prefix is unsafe")
    if not args.keep and len(args.prefix.strip()) < 8:
        parser.error("--prefix must be at least 8 non-whitespace characters when cleanup is enabled")

    matrix = LiveMatrix(args.remctl, args.prefix, keep=args.keep, standalone_helper=args.standalone_helper)
    failed = False
    try:
        matrix.run()
    except Exception as exc:
        failed = True
        matrix.record("matrix failed", "failed", str(exc))
    finally:
        try:
            matrix.cleanup()
        except Exception as exc:
            failed = True
            matrix.record("cleanup failed", "failed", str(exc))
        finally:
            matrix.close()

    summary = {
        "status": "failed" if failed else "passed",
        "prefix": args.prefix,
        "kept": args.keep,
        "results": matrix.results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
