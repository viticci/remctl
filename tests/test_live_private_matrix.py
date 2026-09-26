from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest import mock
from pathlib import Path

matrix_path = Path(__file__).resolve().parents[1] / "scripts" / "live_private_matrix.py"
matrix_spec = importlib.util.spec_from_file_location("live_private_matrix_test", matrix_path)
assert matrix_spec is not None and matrix_spec.loader is not None
matrix_module = importlib.util.module_from_spec(matrix_spec)
sys.modules[matrix_spec.name] = matrix_module
matrix_spec.loader.exec_module(matrix_module)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class MatrixCleanupTests(unittest.TestCase):
    prefix = "RemCTL Test Unique Prefix"

    def matrix(self, clock, *, quiet=10, poll=5, maximum=40):
        return matrix_module.LiveMatrix(
            "remctl",
            self.prefix,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            cleanup_quiet_seconds=quiet,
            cleanup_poll_seconds=poll,
            cleanup_max_seconds=maximum,
        )

    def stub_non_smart_inventory(self, matrix):
        matrix.templates = lambda: []
        matrix.lists = lambda: []
        matrix.json_command = lambda _args: []

    def test_cleanup_redeletes_reappeared_exact_id_and_requires_quiet_window(self):
        clock = FakeClock()
        matrix = self.matrix(clock)
        self.stub_non_smart_inventory(matrix)
        deletes = []
        initial_delete_done = False
        reappearance_delete_done = False

        def smart_lists():
            if not initial_delete_done:
                return [{"id": 41, "objectUUID": "UUID-41", "name": self.prefix + " Smart", "kind": "custom"}]
            if clock.now >= 10 and not reappearance_delete_done:
                return [{"id": 41, "objectUUID": "UUID-41", "name": self.prefix + " Smart", "kind": "custom"}]
            return []

        def command(args, **_kwargs):
            nonlocal initial_delete_done, reappearance_delete_done
            if args[:2] == ["smart-list-delete", "--smart-list-id"]:
                deletes.append(int(args[2]))
                if not initial_delete_done:
                    initial_delete_done = True
                else:
                    reappearance_delete_done = True
            return matrix_module.CommandResult(args, 0, "{}", "")

        matrix.smart_lists = smart_lists
        matrix.command = command
        try:
            matrix.cleanup()
        finally:
            matrix.close()

        self.assertEqual(deletes, [41, 41])
        self.assertGreaterEqual(clock.now, 20)
        self.assertIn("smart lists remained absent for 10s locally", matrix.results[-1]["detail"])
        self.assertIn("final prefix inventory was empty", matrix.results[-1]["detail"])

    def test_cleanup_fails_when_reappearance_never_reaches_quiet_window(self):
        clock = FakeClock()
        matrix = self.matrix(clock, quiet=10, poll=5, maximum=20)
        self.stub_non_smart_inventory(matrix)
        matrix.smart_lists = lambda: [
            {"id": 52, "objectUUID": "UUID-52", "name": self.prefix + " Smart", "kind": "custom"}
        ]
        matrix.command = lambda args, **_kwargs: matrix_module.CommandResult(args, 0, "{}", "")
        try:
            with self.assertRaisesRegex(AssertionError, "never reached a sustained empty local readback"):
                matrix.cleanup()
        finally:
            matrix.close()

        self.assertEqual(clock.now, 20)

    def test_keep_skips_cleanup_inventory_and_waits(self):
        clock = FakeClock()
        matrix = matrix_module.LiveMatrix(
            "remctl",
            self.prefix,
            keep=True,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            cleanup_quiet_seconds=10,
            cleanup_poll_seconds=5,
            cleanup_max_seconds=20,
        )
        matrix.smart_lists = lambda: self.fail("--keep must not inventory smart lists")
        try:
            matrix.cleanup()
        finally:
            matrix.close()
        self.assertEqual(clock.sleeps, [])

    def test_cleanup_refuses_reused_tracked_id_with_different_uuid(self):
        clock = FakeClock()
        matrix = self.matrix(clock)
        self.stub_non_smart_inventory(matrix)
        matrix.created_smart_list_ids[61] = "ORIGINAL-UUID"
        matrix.smart_lists = lambda: [
            {
                "id": 61,
                "objectUUID": "REPLACEMENT-UUID",
                "name": self.prefix + " Replacement",
                "kind": "custom",
            }
        ]
        deletes = []

        def command(args, **_kwargs):
            if args and args[0] == "smart-list-delete":
                deletes.append(args)
            return matrix_module.CommandResult(args, 0, "{}", "")

        matrix.command = command
        try:
            with self.assertRaisesRegex(AssertionError, "changed UUID"):
                matrix.cleanup()
        finally:
            matrix.close()

        self.assertEqual(deletes, [])


class StandaloneHelperTests(unittest.TestCase):
    def test_default_matrix_refuses_direct_helper_writes(self):
        matrix = matrix_module.LiveMatrix("remctl", "RemCTL Test Prefix")
        try:
            with mock.patch.object(matrix_module.subprocess, "run") as run:
                with self.assertRaisesRegex(AssertionError, "--standalone-helper"):
                    matrix.private_helper_json({"action": "set_smart_list_pinned"})
                run.assert_not_called()
        finally:
            matrix.close()

    def test_explicit_standalone_mode_runs_helper_write(self):
        matrix = matrix_module.LiveMatrix("remctl", "RemCTL Test Prefix", standalone_helper=True)
        try:
            proc = mock.Mock(returncode=0, stdout='{"status":"updated"}', stderr="")
            with mock.patch.object(matrix_module.subprocess, "run", return_value=proc) as run:
                result = matrix.private_helper_json({"action": "set_smart_list_pinned"})
            self.assertEqual(result["status"], "updated")
            run.assert_called_once()
        finally:
            matrix.close()


if __name__ == "__main__":
    unittest.main()
