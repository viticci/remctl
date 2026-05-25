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
    def __init__(self, remctl: str, prefix: str, keep: bool = False):
        self.remctl = remctl
        self.prefix = prefix
        self.keep = keep
        self.tmpdir = tempfile.TemporaryDirectory(prefix="remctl-private-matrix-")
        self.image_path = Path(self.tmpdir.name) / "pixel.png"
        self.image_path.write_bytes(PNG_1X1)
        self.results: list[dict] = []
        self.created_lists: set[str] = set()
        self.created_smart_lists: set[str] = set()
        self.created_templates: set[str] = set()
        self.created_reminders: set[int] = set()

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
            time.sleep(delay)
        return last

    def lists(self) -> list[dict]:
        return self.json_command(["lists", "--json"])

    def list_named(self, name: str) -> dict | None:
        return next((item for item in self.lists() if item.get("title") == name), None)

    def smart_lists(self) -> list[dict]:
        return self.json_command(["smart-lists", "--json"])

    def smart_named(self, name: str) -> dict | None:
        return next((item for item in self.smart_lists() if item.get("name") == name), None)

    def templates(self) -> list[dict]:
        return self.json_command(["templates", "--json"])

    def template_named(self, name: str) -> dict | None:
        return next((item for item in self.templates() if item.get("name") == name), None)

    def show_list(self, name: str) -> list[dict]:
        return self.json_command(["show", name, "--json"])

    def info(self, reminder_id: int) -> dict:
        return self.json_command(["info", str(reminder_id), "--json"])

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

        grocery_row = self.create_list(grocery, "--private", "--groceries", "--grocery-locale", "en_US")
        self.assert_true(grocery_row.get("isGroceries"), "Groceries metadata did not persist")
        self.assert_true(grocery_row.get("grocery", {}).get("locale") == "en_US", "Groceries locale did not persist")
        self.record("list-create groceries", "passed", grocery)

        milk = self.create_reminder(f"{self.prefix} Milk", "-l", grocery, "--private", "--grocery")
        self.assert_true(milk.get("numericId") is not None, "private grocery add did not return numericId")
        shown = self.retry(lambda: [item for item in self.show_list(grocery) if item.get("title") == f"{self.prefix} Milk"])
        self.assert_true(bool(shown), "grocery reminder did not appear in list")
        self.record("add --private --grocery", "passed", str(milk.get("numericId")))

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
        info = self.retry(lambda: self.info(rid) if self.info(rid).get("subtasks") else None)
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
        edited_info = self.retry(lambda: self.info(rid))
        location_alarms = [
            alarm for alarm in edited_info.get("alarms", [])
            if alarm.get("type") == "location" and alarm.get("location", {}).get("title") == "Apple Park"
        ]
        self.assert_true(edited_info.get("flagged") is False, "private unflag did not persist")
        self.assert_true(edited_info.get("urgent") is False, "urgent clear did not persist")
        self.assert_true(bool(location_alarms), "location alarm did not persist")
        self.record("edit private flag urgent and location", "passed", str(rid))

        self.json_command(["edit", str(rid), "--private", "--early-reminder", "clear", "--json"])
        cleared = self.retry(lambda: self.info(rid))
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

        self.json_command(["list-pin", editable, "--private", "--json"])
        pinned = self.retry(lambda: self.smart_named(editable) and self.smart_named(editable).get("pinned"))
        self.assert_true(bool(pinned), "smart-list pin did not persist")
        self.json_command(["list-unpin", editable, "--private", "--json"])
        unpinned = self.retry(lambda: self.smart_named(editable) and not self.smart_named(editable).get("pinned"))
        self.assert_true(bool(unpinned), "smart-list unpin did not persist")
        self.record("list-pin/list-unpin smart list", "passed", editable)

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
        for name in sorted(self.created_templates, reverse=True):
            try:
                if self.template_named(name):
                    self.command(["template-delete", name, "--private", "--force", "--json"], expect=None)
            except Exception:
                pass
        for name in sorted(self.created_smart_lists, reverse=True):
            try:
                if self.smart_named(name):
                    self.command(["smart-list-delete", name, "--private", "--force", "--json"], expect=None)
            except Exception:
                pass
        for rid in sorted(self.created_reminders, reverse=True):
            try:
                self.command(["delete", str(rid), "--force", "--json"], expect=None)
            except Exception:
                pass
        for name in sorted(self.created_lists, reverse=True):
            try:
                if self.list_named(name):
                    self.command(["list-delete", name, "--force", "--json"], expect=None)
            except Exception:
                pass

    def run(self):
        doctor = self.json_command(["doctor", "--for-agent", "--json"])
        checks = {item["name"]: item["status"] for item in doctor.get("checks", [])}
        self.assert_true(checks.get("private_helper") == "ok", "private helper is not available")
        self.record("doctor private helper", "passed", "ok")
        self.run_guardrails()
        source_list = self.run_lists_and_reminders()
        self.run_smart_lists(source_list)
        self.run_templates(source_list)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live private RemCTL command matrix against disposable Reminders data.")
    parser.add_argument("--remctl", default=str(Path(__file__).resolve().parents[1] / "remctl"), help="remctl binary to test")
    parser.add_argument("--prefix", default=f"RemCTL Matrix {datetime.now().strftime('%Y%m%d-%H%M%S')}", help="Disposable item prefix")
    parser.add_argument("--keep", action="store_true", help="Keep disposable Reminders data for manual inspection")
    args = parser.parse_args()

    matrix = LiveMatrix(args.remctl, args.prefix, keep=args.keep)
    failed = False
    try:
        matrix.run()
    except Exception as exc:
        failed = True
        matrix.record("matrix failed", "failed", str(exc))
    finally:
        matrix.cleanup()
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
