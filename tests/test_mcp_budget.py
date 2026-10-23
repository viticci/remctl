from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import remctl_mcp  # noqa: E402


class BudgetTests(unittest.TestCase):
    def test_large_lists_are_cut_honestly(self):
        rows = [{"id": i, "title": "Erinnerung %d" % i, "notes": "ä" * 500} for i in range(300)]
        out = remctl_mcp._within_budget({"items": rows, "count": 300})
        self.assertTrue(out["truncated"])
        self.assertEqual(out["count"], 300)
        self.assertEqual(out["resultBudget"]["totalItems"], 300)
        self.assertEqual(out["resultBudget"]["returnedItems"], len(out["items"]))
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False).encode()), remctl_mcp.RESULT_BYTE_LIMIT)

    def test_small_results_pass_unchanged(self):
        small = {"items": [{"id": 1}], "count": 1}
        self.assertIs(remctl_mcp._within_budget(small), small)


if __name__ == "__main__":
    unittest.main()
