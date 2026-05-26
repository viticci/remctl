#!/usr/bin/env python3
"""Run live edit-mode regression checks against disposable Reminders.

The matrix focuses on states where EventKit due edits can diverge from the
time Reminders.app actually shows: dueDate, displayDate, and alarm rows.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class LiveEditMatrix:
    def __init__(self, remctl: str, prefix: str, keep: bool = False):
        self.remctl = remctl
        self.prefix = prefix
        self.keep = keep
        self.lists: list[str] = []
        self.reminders: list[int] = []
        self.results: list[dict[str, str]] = []
        self.day = date.today() + timedelta(days=1)
        self.day_after = self.day + timedelta(days=1)

    def stamp(self, hour: int, minute: int = 0, *, day: date | None = None) -> str:
        target = day or self.day
        return f"{target:%Y-%m-%d} {hour:02d}:{minute:02d}"

    def iso(self, hour: int, minute: int = 0, *, day: date | None = None) -> str:
        target = day or self.day
        return f"{target:%Y-%m-%d}T{hour:02d}:{minute:02d}:00"

    def command(self, args: list[str], *, expect: int = 0) -> CommandResult:
        proc = subprocess.run(
            [self.remctl, *args],
            text=True,
            capture_output=True,
            timeout=60,
        )
        result = CommandResult(args, proc.returncode, proc.stdout, proc.stderr)
        if proc.returncode != expect:
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

    def record(self, name: str, detail: str = ""):
        self.results.append({"name": name, "status": "passed", "detail": detail})

    def assert_true(self, condition: bool, message: str):
        if not condition:
            raise AssertionError(message)

    def retry(self, fn, *, attempts: int = 40, delay: float = 0.25):
        last = None
        for _ in range(attempts):
            last = fn()
            if last:
                return last
            time.sleep(delay)
        return last

    def create_list(self, name: str):
        self.json_command(["list-create", name, "--json"])
        self.lists.append(name)
        self.retry(lambda: self.list_exists(name))

    def list_exists(self, name: str) -> bool:
        lists = self.json_command(["lists", "--json"])
        return any(item.get("title") == name for item in lists)

    def add(self, title: str, *args: str) -> int:
        payload = self.json_command(["add", title, *args, "--json"])
        rid = payload.get("numericId")
        self.assert_true(rid is not None, f"add did not return numericId for {title}")
        self.reminders.append(int(rid))
        return int(rid)

    def edit(self, rid: int, *args: str):
        self.json_command(["edit", str(rid), *args, "--json"])

    def info(self, rid: int) -> dict:
        return self.json_command(["info", str(rid), "--json"])

    def wait_info(self, rid: int, predicate, message: str) -> dict:
        found = self.retry(lambda: (info if predicate(info := self.info(rid)) else None))
        self.assert_true(bool(found), message)
        return found

    def alarm_dates(self, info: dict) -> list[str]:
        return [
            alarm["date"]
            for alarm in info.get("alarms", [])
            if alarm.get("type") == "absolute" and alarm.get("date")
        ]

    def relative_alarms(self, info: dict) -> list[dict]:
        return [alarm for alarm in info.get("alarms", []) if alarm.get("type") == "relative"]

    def run(self):
        source = f"{self.prefix} Source"
        target = f"{self.prefix} Target"
        self.create_list(source)
        self.create_list(target)

        self.matching_alarm_moves_with_due(source)
        self.custom_alarm_survives_reschedule(source)
        self.noop_reschedule_preserves_custom_alarm(source)
        self.due_clear_removes_matching_alarm(source)
        self.alarm_clear_removes_alarm(source)
        self.relative_alarm_survives_reschedule(source)
        self.title_notes_priority_preserve_schedule(source)
        self.move_list_preserves_schedule(source, target)

    def matching_alarm_moves_with_due(self, list_name: str):
        rid = self.add(
            f"{self.prefix} matching alarm",
            "-l",
            list_name,
            "-d",
            self.stamp(15),
            "--alarm",
            self.stamp(15),
        )
        self.edit(rid, "-d", self.stamp(16))
        info = self.wait_info(
            rid,
            lambda p: p.get("dueDate") == self.iso(16) and self.alarm_dates(p) == [self.iso(16)],
            "matching absolute alarm did not move with due date",
        )
        self.assert_true(info.get("displayDate") in (None, self.iso(16)), "displayDate stayed stale after reschedule")
        self.record("matching absolute alarm moves with due date", str(rid))

    def custom_alarm_survives_reschedule(self, list_name: str):
        rid = self.add(
            f"{self.prefix} custom alarm",
            "-l",
            list_name,
            "-d",
            self.stamp(16),
            "--alarm",
            self.stamp(15),
        )
        self.edit(rid, "-d", self.stamp(17))
        self.wait_info(
            rid,
            lambda p: p.get("dueDate") == self.iso(17) and self.alarm_dates(p) == [self.iso(15)],
            "custom absolute alarm was rewritten during reschedule",
        )
        self.record("custom absolute alarm survives reschedule", str(rid))

    def noop_reschedule_preserves_custom_alarm(self, list_name: str):
        rid = self.add(
            f"{self.prefix} noop custom alarm",
            "-l",
            list_name,
            "-d",
            self.stamp(16),
            "--alarm",
            self.stamp(15),
        )
        self.edit(rid, "-d", self.stamp(16))
        self.wait_info(
            rid,
            lambda p: p.get("dueDate") == self.iso(16) and self.alarm_dates(p) == [self.iso(15)],
            "no-op reschedule rewrote custom absolute alarm",
        )
        self.record("no-op reschedule preserves custom alarm", str(rid))

    def due_clear_removes_matching_alarm(self, list_name: str):
        rid = self.add(
            f"{self.prefix} clear matching alarm",
            "-l",
            list_name,
            "-d",
            self.stamp(15),
            "--alarm",
            self.stamp(15),
        )
        self.edit(rid, "-d", "clear")
        self.wait_info(
            rid,
            lambda p: "dueDate" not in p and "displayDate" not in p and not p.get("alarms"),
            "clearing due date left a matching absolute alarm/display date behind",
        )
        self.record("due clear removes matching absolute alarm", str(rid))

    def alarm_clear_removes_alarm(self, list_name: str):
        rid = self.add(
            f"{self.prefix} alarm clear",
            "-l",
            list_name,
            "-d",
            self.stamp(15),
            "--alarm",
            self.stamp(15),
        )
        self.edit(rid, "--alarm", "clear")
        self.wait_info(
            rid,
            lambda p: p.get("dueDate") == self.iso(15) and not p.get("alarms") and "displayDate" not in p,
            "alarm clear left alarm/display date behind",
        )
        self.record("alarm clear removes absolute alarm", str(rid))

    def relative_alarm_survives_reschedule(self, list_name: str):
        rid = self.add(
            f"{self.prefix} relative alarm",
            "-l",
            list_name,
            "-d",
            self.stamp(15),
            "--alarm",
            "15m",
        )
        self.edit(rid, "-d", self.stamp(16))
        self.wait_info(
            rid,
            lambda p: p.get("dueDate") == self.iso(16)
            and len(self.relative_alarms(p)) == 1
            and self.relative_alarms(p)[0].get("relativeOffsetMinutes") == -15,
            "relative alarm did not survive due reschedule",
        )
        self.record("relative alarm survives due reschedule", str(rid))

    def title_notes_priority_preserve_schedule(self, list_name: str):
        rid = self.add(
            f"{self.prefix} metadata preserve",
            "-l",
            list_name,
            "-d",
            self.stamp(14),
            "--alarm",
            self.stamp(14),
        )
        self.edit(rid, "--title", f"{self.prefix} metadata renamed", "-n", "edited notes", "-p", "high")
        self.wait_info(
            rid,
            lambda p: p.get("title") == f"{self.prefix} metadata renamed"
            and p.get("notes") == "edited notes"
            and p.get("priority") == "high"
            and p.get("dueDate") == self.iso(14)
            and self.alarm_dates(p) == [self.iso(14)],
            "title/notes/priority edit changed schedule metadata",
        )
        self.record("title notes priority preserve schedule", str(rid))

    def move_list_preserves_schedule(self, source: str, target: str):
        rid = self.add(
            f"{self.prefix} move preserve",
            "-l",
            source,
            "-d",
            self.stamp(13),
            "--alarm",
            self.stamp(13),
        )
        self.edit(rid, "-l", target)
        self.wait_info(
            rid,
            lambda p: p.get("list") == target
            and p.get("dueDate") == self.iso(13)
            and self.alarm_dates(p) == [self.iso(13)],
            "list move changed schedule metadata",
        )
        self.record("list move preserves schedule", str(rid))

    def cleanup(self):
        if self.keep:
            return
        for rid in reversed(self.reminders):
            self.command(["delete", str(rid), "--force", "--json"], expect=0)
        for name in reversed(self.lists):
            self.command(["list-delete", name, "--force", "--json"], expect=0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remctl", default="remctl")
    parser.add_argument("--prefix", default=f"RemCTL Edit Matrix {datetime.now():%Y%m%d-%H%M%S}")
    parser.add_argument("--keep", action="store_true", help="Keep disposable lists and reminders for inspection")
    args = parser.parse_args()

    matrix = LiveEditMatrix(args.remctl, args.prefix, keep=args.keep)
    cleanup_error = None
    try:
        matrix.run()
    finally:
        try:
            matrix.cleanup()
        except Exception as exc:
            cleanup_error = exc
            print(f"cleanup failed: {exc}", file=sys.stderr)
    if cleanup_error:
        return 1

    print(json.dumps({"status": "passed", "results": matrix.results}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
