from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import remctl_mcp  # noqa: E402
from test_mcp_server import FakeExecutor, make_server, modern_meta, request  # noqa: E402


def rows(n, size=500):
    return [{"id": i, "title": "Erinnerung %d" % i, "notes": "ä" * size} for i in range(n)]


class BudgetTests(unittest.TestCase):
    def test_large_lists_are_cut_honestly(self):
        out = remctl_mcp._within_budget({"items": rows(300), "count": 300})
        self.assertTrue(out["truncated"])
        self.assertEqual(out["count"], 300)
        self.assertEqual(out["resultBudget"]["totalItems"], 300)
        self.assertEqual(out["resultBudget"]["returnedItems"], len(out["items"]))
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False).encode()), remctl_mcp.RESULT_BYTE_LIMIT)

    def test_small_results_pass_unchanged(self):
        small = {"items": [{"id": 1}], "count": 1}
        self.assertIs(remctl_mcp._within_budget(small), small)

    def test_a_cut_page_resumes_after_the_last_row_it_kept(self):
        page = {"items": rows(100), "count": 100, "total": 250, "offset": 100, "limit": 100,
                "hasMore": True, "nextOffset": 200}
        out = remctl_mcp._within_budget(page)
        kept = len(out["items"])
        self.assertLess(kept, 100)
        self.assertEqual((out["count"], out["nextOffset"], out["hasMore"]), (kept, 100 + kept, True))
        self.assertEqual(out["total"], 250)
        self.assertIn("nextOffset", out["resultBudget"]["hint"])

    def test_a_row_too_large_on_its_own_is_still_returned(self):
        page = {"items": rows(3, size=60_000), "count": 3, "total": 3, "offset": 0, "limit": 100,
                "hasMore": False, "nextOffset": None}
        out = remctl_mcp._within_budget(page)
        self.assertEqual([item["id"] for item in out["items"]], [0])
        self.assertEqual(out["nextOffset"], 1)

    def test_tool_text_matches_the_cut_structured_content(self):
        server, _ = make_server(FakeExecutor(stdout=json.dumps(rows(300))))
        result = request(server, "tools/call", {"_meta": modern_meta(), "name": "show_list",
                                                "arguments": {"list": "Work"}})["result"]
        self.assertTrue(result["structuredContent"]["truncated"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])


if __name__ == "__main__":
    unittest.main()
