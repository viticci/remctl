from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import remctl_mcp as m
import remctl_plugin as p
from remctl_workspace import matches_smart, _read
from test_mcp_server import FakeExecutor, request, modern_meta, APPS_CAPS
from datetime import date

class DesktopPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"REMCTL_PLUGIN":"1", "REMCTL_CONFIG_DIR":self.temp.name})
        self.environment.start()
        self.executor = FakeExecutor(stdout='{"status":"ok","id":42}')
        self.server=m.MCPServer(m.ServerConfig(version="test",executor=self.executor))

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def call(self,name,args=None,**extra):
        return request(self.server,"tools/call",{"name":name,"arguments":args or {},**extra})["result"]

    def test_standard_tools_do_not_open_workspace(self):
        # Cover negotiated Apps and the legacy aliases used by desktop hosts.
        for meta in (None, modern_meta(APPS_CAPS), modern_meta()):
            params = {"_meta": meta} if meta else {}
            catalog = request(self.server, "tools/list", params)["result"]["tools"]
            for tool in catalog:
                if tool["name"] in m.TOOLS_BY_NAME:
                    self.assertNotIn("_meta", tool, tool["name"])
            for name, args in (("get_list", {"list": "Work"}),
                               ("create_reminder", {"title": "Demo"}),
                               ("update_reminder", {"reminder_id": 42, "title": "Edit"})):
                value = self.call(name, args, **params)
                self.assertFalse(value.get("isError"), value)
                expected = m.tool_result_from_command(m.TOOLS_BY_NAME[name],
                    m.CommandResult([], 0, self.executor.stdout, ""))
                self.assertEqual(value["structuredContent"], expected["structuredContent"])
                self.assertEqual(value["content"], expected["content"])
                result_meta = value.get("_meta", {})
                for key in ("ui", "ui/resourceUri", "openai/outputTemplate", "openai/widgetAccessible"):
                    self.assertNotIn(key, result_meta, (name, key))
            invalid = self.call("update_reminder", {"reminder_id": 0}, **params)
            self.assertTrue(invalid["isError"])
            self.assertNotIn("ui", invalid.get("_meta", {}))

    def test_workspace_entrypoint_keeps_its_ui_resource(self):
        catalog = {tool["name"]: tool for tool in request(self.server, "tools/list", {})["result"]["tools"]}
        entry = catalog["open_workspace"]
        self.assertEqual(entry["_meta"]["ui"]["resourceUri"], p.UI_URI)
        self.assertIn("model", entry["_meta"]["ui"]["visibility"])
        value = self.call("open_workspace")
        self.assertFalse(value.get("isError"), value)
        self.assertEqual(value["_meta"]["ui"]["resourceUri"], p.UI_URI)
        resource = self.server.plugin.resource(p.UI_URI, None)
        self.assertTrue(resource["contents"][0]["text"])

    def test_settings_merge_and_native_layout(self):
        self.call("update_settings",{"set":{"layout":"columns"}})
        self.call("update_settings",{"set":{"density":"compact"}})
        value=self.call("read_settings")["structuredContent"]
        self.assertEqual(value["values"]["layout"],"columns")
        self.assertEqual(value["values"]["density"],"compact")
        self.assertEqual(value["layout"][0]["kind"],"group")
        self.assertEqual(self.server.capabilities()["extensions"]["openai/settings"]["readTool"],"read_settings")

    def test_mutation_replay_cannot_create_twice_or_change_payload(self):
        args={"operationId":"unique-operation-001","tool":"create_reminder","arguments":{"title":"Demo"}}
        first=self.call("workspace_mutate",args)
        self.assertFalse(first.get("isError"),first)
        self.assertEqual(self.call("workspace_mutate",args)["structuredContent"], first["structuredContent"])
        self.assertEqual(len(self.executor.calls),1)
        args["arguments"]["title"]="Different"
        self.assertTrue(self.call("workspace_mutate",args)["isError"])
        self.assertEqual(len(self.executor.calls),1)

    def test_sidebar_order_persists_across_server_restarts_and_keeps_scopes_separate(self):
        top = ["list:HOME", "list:WORK", "list:PRIORITIES", "list:MISC"]
        self.call("update_sidebar_order", {"scope":"top", "order":top})
        group = ["smart:THIS-WEEK", "smart:NEXT-WEEK", "list:ERRANDS"]
        self.call("update_sidebar_order", {"scope":"group:PRIORITIES", "order":group})
        self.server = m.MCPServer(m.ServerConfig(version="next-build", executor=self.executor))
        value = self.call("read_sidebar_order")["structuredContent"]["orders"]
        self.assertEqual(value, {"top":top, "group:PRIORITIES":group})
        value = self.call("update_sidebar_order", {"scope":"group:PRIORITIES", "order":[]})
        self.assertEqual(value["structuredContent"]["orders"], {"top":top})
        self.assertEqual(self.executor.calls, [], "Display preferences must never write to Reminders")

    def test_sidebar_order_rejects_invalid_keys_and_duplicate_items(self):
        for args in ({"scope":"../file", "order":[]}, {"scope":"top", "order":["list:HOME", "list:HOME"]},
                     {"scope":"top", "order":["invalid"]}, {"scope":"top", "order":[1]}):
            with self.subTest(args=args):
                self.assertTrue(self.call("update_sidebar_order", args)["isError"])
        self.assertEqual(self.call("read_sidebar_order")["structuredContent"], {"orders":{}})

    def test_stale_edit_never_runs_write(self):
        self.executor.stdout='{"id":42,"revision":"new"}'
        value=self.call("workspace_mutate",{"operationId":"stale-operation-001","tool":"update_reminder","arguments":{"reminder_id":42,"title":"Edit"},"expectedRevision":"old"})
        self.assertEqual(value["structuredContent"]["status"],"conflict")
        self.assertEqual(self.executor.calls[0]["argv"][0],"workspace")
        self.assertEqual(len(self.executor.calls),1)

    def test_advanced_opt_in_is_required(self):
        value=self.call("workspace_mutate",{"operationId":"private-operation-001","tool":"manage_section_create","arguments":{"name":"Demo","list_id":9,"private":True}})
        self.assertTrue(value["isError"])
        self.assertEqual(self.executor.calls,[])

    def test_smart_compound_evaluates_the_inner_predicate(self):
        spec={"kind":"tags","filters":[{"kind":"tags","tags":["work"],"tagMatch":"all"}],"match":"all"}
        self.assertFalse(matches_smart({"tags":["home"]},spec,date.today()))
        self.assertTrue(matches_smart({"tags":["work","home"]},spec,date.today()))

    def test_export_formats_and_attachment_download_preserve_private_files(self):
        self.executor.stdout=json.dumps({"items":[{"id":42,"title":"=SUM(A1)","notes":"line one\nline two","tags":["demo"]}],"snapshot":"one","nextOffset":None})
        with patch.object(Path,"home",return_value=Path(self.temp.name)):
            csv_result=self.call("export_remctl_file",{"format":"csv"})
            self.assertFalse(csv_result.get("isError"),csv_result)
            csv_path=Path(csv_result["structuredContent"]["path"])
            self.assertIn("'=SUM(A1)",csv_path.read_text())
            self.assertEqual(csv_path.stat().st_mode&0o777,0o600)
            json_result=self.call("export_remctl_file",{"format":"json"})
            self.assertEqual(json.loads(Path(json_result["structuredContent"]["path"]).read_text())[0]["title"],"=SUM(A1)")
            scoped=self.call("export_remctl_file",{"format":"json","query":{"view":"smart","smartId":180,"query":"demo","includeCompleted":False,"offset":500}})
            self.assertFalse(scoped.get("isError"),scoped)
            exported_query=json.loads(self.executor.calls[-1]["argv"][2])
            self.assertEqual({key:exported_query[key] for key in ("view","smartId","query","includeCompleted","offset","limit")},
                             {"view":"smart","smartId":180,"query":"demo","includeCompleted":False,"offset":0,"limit":500})
            self.executor.stdout=json.dumps({"filename":"../../demo.pdf","blob":"JVBERg==","mimeType":"application/pdf"})
            result=self.call("export_attachment",{"reminderId":42,"index":0})
            path=Path(result["structuredContent"]["path"])
            self.assertEqual(path.parent,Path(self.temp.name)/"Downloads")
            self.assertEqual(path.read_bytes(),b"%PDF")
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_export_keeps_every_row_of_a_large_workspace_page(self):
        # The MCP result budget applies to model-facing tools, not to workspace pages.
        items=[{"id":i,"title":"Item %d"%i,"notes":"x"*200} for i in range(500)]
        self.executor.stdout=json.dumps({"items":items,"snapshot":"one","nextOffset":None})
        with patch.object(Path,"home",return_value=Path(self.temp.name)):
            result=self.call("export_remctl_file",{"format":"json"})
            self.assertEqual(len(json.loads(Path(result["structuredContent"]["path"]).read_text())),500)

    def test_smart_filter_exclusions_relative_ranges_and_empty_values(self):
        from remctl_smart_lists import summarize_smart_list_filter
        item={"tags":["demo","design"],"priority":"high","dueDate":"2026-10-02T09:00:00","allDay":False,"listUUID":"demo-list"}
        payload={"operation":"and","hashtags":{"hashtags":{"operation":"and","include":["demo"],"exclude":["blocked"]}},"date":{"relativeRange":{"direction":"inNext","magnitude":3,"units":"day"}},"lists":{"include":["demo-list"],"exclude":["other"]}}
        spec=summarize_smart_list_filter(payload,strict=True)
        self.assertTrue(matches_smart(item,spec,date(2026,9,30)))
        self.assertFalse(matches_smart({**item,"tags":["demo","blocked"]},spec,date(2026,9,30)))
        self.assertFalse(matches_smart({**item,"listUUID":"other"},spec,date(2026,9,30)))
        self.assertFalse(matches_smart({**item,"dueDate":None},spec,date(2026,9,30)))
        self.assertTrue(matches_smart({"tags":[]},summarize_smart_list_filter({"hashtags":{"untagged":""}},strict=True),date.today()))

    def test_smart_no_priority_matches_unprioritized_reminders(self):
        from remctl_smart_lists import summarize_smart_list_filter
        payload={"operation":"and","priorities":["none"],"lists":{"include":[],"exclude":["errands"],"operation":"and"}}
        spec=summarize_smart_list_filter(payload,strict=True)
        item={"priority":"none","listUUID":"work"}
        self.assertTrue(matches_smart(item,spec,date.today()))
        self.assertFalse(matches_smart({**item,"priority":"low"},spec,date.today()))
        self.assertFalse(matches_smart({**item,"listUUID":"errands"},spec,date.today()))

    def test_pinned_smart_count_excludes_completed_and_uses_tags_without_artwork(self):
        rows = [{"Z_PK": i, "ZLIST": 1, "ZCOMPLETED": completed, "ZFLAGGED": 1} for i, completed in [(1, False), (2, False), (3, True)]]
        api = {
            "q_all_lists": lambda db: [{"id": 1, "title": "Demo", "objectUUID": "list-id"}],
            "list_to_dict": lambda row: row, "q_sections": lambda db: [],
            "q_section_memberships": lambda db, identifier: {}, "search_fold": lambda value: value,
            "q_smart_lists": lambda db: [{"id": 9, "kind": "custom", "pinned": True, "filter": {"supported": True, "kind": "tags", "tags": ["demo"]}}],
            "smart_list_to_dict": lambda row: row, "q_reminders": lambda db, **kwargs: rows,
            "row_shown_date": lambda row: None, "ts": lambda value: None,
            "to_dict": lambda row: {"id": row["Z_PK"], "flagged": True},
            "q_hashtags": lambda db, identifier: [{"ZNAME": "demo"}] if identifier in {1, 3} else [],
            "deleted_reminders": lambda db, **kwargs: [],
        }
        value = _read(None, {"view": "deleted"}, api, "query")
        self.assertEqual(value["smartLists"][0]["count"], 1)
        self.assertEqual(value["counts"]["all"], 2)

    def test_smart_filters_load_details_only_for_the_page_and_reuse_filter_fields(self):
        from unittest.mock import Mock
        rows = [{"Z_PK": i, "ZCKIDENTIFIER": f"rem-{i}", "ZLIST": 1,
                 "ZCOMPLETED": i == 6, "ZFLAGGED": 0} for i in range(1, 7)]
        spec = {"supported": True, "kind": "tags", "tags": ["demo"]}
        tags = Mock(side_effect=lambda db, pk: [{"ZNAME": "demo"}] if pk % 2 == 0 else [])
        hydrate = Mock()
        serialize = Mock(side_effect=lambda rows, db, memberships: [
            {"id": r["Z_PK"], "notes": "Preserved", "tags": ["demo"] if r["Z_PK"] % 2 == 0 else [],
             "attachments": [{"filename": "photo.png", "path": "/private/photo.png"}]}
            for r in rows])
        api = {
            "q_all_lists": lambda db: [{"id": 1, "title": "Demo", "objectUUID": "list-id"}],
            "list_to_dict": lambda row: row, "q_sections": lambda db: [],
            "q_section_memberships": lambda db, identifier: {}, "search_fold": lambda value: value,
            "q_smart_lists": lambda db: [{"id": 9, "kind": "custom", "pinned": True, "filter": spec}],
            "smart_list_to_dict": lambda row: row, "q_reminders": lambda db, **kwargs: rows,
            "row_shown_date": lambda row: None, "ts": lambda value: None,
            "to_dict": lambda row: {"id": row["Z_PK"], "priority": "none"},
            "q_hashtags": tags, "reminders_to_dicts": serialize, "hydrate_reminder_detail": hydrate,
        }
        with patch("remctl_workspace.rich_link_rows", return_value=[]) as links:
            value = _read(None, {"view": "smart", "smartId": 9, "offset": 1, "limit": 1}, api, "query")
        self.assertEqual(value["total"], 2)
        self.assertEqual(value["smartLists"][0]["count"], 2)
        self.assertEqual([i["id"] for i in value["items"]], [4])
        self.assertEqual(value["items"][0]["notes"], "Preserved")
        self.assertEqual(value["items"][0]["attachments"], [{
            "filename": "photo.png", "resourceUri": "remctl://reminder/rem-4/attachment/0"}])
        self.assertEqual(tags.call_count, 5)
        self.assertEqual(serialize.call_count, 1)
        self.assertEqual(hydrate.call_count, 1)
        self.assertEqual(links.call_count, 1)

        # A priority-only preview needs neither tags nor location alarms.
        spec.clear()
        spec.update(supported=True, kind="priority", priorities=["none"])
        tags.reset_mock()
        with patch("remctl_workspace.rich_link_rows", return_value=[]):
            value = _read(None, {"view": "smart", "smartId": 9, "limit": 1}, api, "query")
        self.assertEqual(value["total"], 5)
        tags.assert_not_called()

        spec.clear()
        spec.update(supported=True, kind="location", location={"latitude": 42, "longitude": 12})
        alarms = Mock(side_effect=lambda db, pk: [{"location": {
            "latitude": 42, "longitude": 12, "proximity": "arriving"}}] if pk % 2 == 0 else [])
        api.update(q_alarms=alarms, alarm_rows_to_json=lambda rows: rows)
        with patch("remctl_workspace.rich_link_rows", return_value=[]):
            value = _read(None, {"view": "smart", "smartId": 9, "includeCompleted": True, "limit": 1}, api, "query")
        self.assertEqual(value["total"], 3)
        self.assertEqual(value["smartLists"][0]["count"], 2)
        self.assertEqual(alarms.call_count, 6)
        tags.assert_not_called()

    def test_worker_retains_ui_that_matches_its_contract_after_install(self):
        with patch.object(Path,"read_text",side_effect=AssertionError("Do not load another build's UI")):
            resource=self.server.plugin.resource(p.UI_URI,None)
        self.assertEqual(resource["contents"][0]["text"],p._UI_HTML)

    def test_workspace_today_uses_the_same_display_date_as_cli_reads(self):
        from datetime import datetime, timedelta
        today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        rows = [{"Z_PK": i, "ZCKIDENTIFIER": f"rem-{i}", "ZLIST": 1,
                 "ZCOMPLETED": False, "ZFLAGGED": 0, "due": today + timedelta(days=delta),
                 "display": today - timedelta(days=delta)} for i, delta in [(1, 1), (2, -1)]]
        api = {
            "q_all_lists": lambda db: [{"id": 1, "title": "Demo", "objectUUID": "list-id"}],
            "list_to_dict": lambda row: row, "q_sections": lambda db: [],
            "q_section_memberships": lambda db, identifier: {}, "search_fold": lambda value: value,
            "q_smart_lists": lambda db: [], "q_reminders": lambda db, **kwargs: rows,
            "row_effective_due": lambda row: row["due"], "row_shown_date": lambda row: row["display"],
            "ts": lambda value: value,
            "reminders_to_dicts": lambda rows, *args: [{"id": r["Z_PK"]} for r in rows],
            "hydrate_reminder_detail": lambda *args: None,
        }
        with patch("remctl_workspace.rich_link_rows", return_value=[]):
            value = _read(None, {"view": "today"}, api, "query")
        self.assertEqual([item["id"] for item in value["items"]], [1])
        self.assertEqual(value["counts"]["today"], 1)

    def test_saved_link_resource_resolves_only_the_selected_reminder_link(self):
        self.executor.stdout = json.dumps({"url":"https://example.com", "title":"Saved title", "image":{"mimeType":"image/png", "data":"eA=="}})
        response = self.server.plugin.resource("remctl://reminder/demo-uuid/link/2", None)
        self.assertEqual(json.loads(response["contents"][0]["text"])["title"], "Saved title")
        self.assertEqual(json.loads(self.executor.calls[-1]["argv"][2]), {"operation":"link_preview", "identifier":"demo-uuid", "index":2})

    def test_missing_preview_respects_setting_and_caches_public_artwork(self):
        self.executor.stdout = json.dumps({"url":"https://example.com", "source":"reminders"})
        self.call("update_settings", {"set":{"loadLinkPreviews":False}})
        with patch('remctl_workspace.fetch_public_preview') as fetch:
            self.server.plugin.resource("remctl://reminder/demo-uuid/link/0", None)
            fetch.assert_not_called()
        self.call("update_settings", {"set":{"loadLinkPreviews":True}})
        with patch('remctl_workspace.fetch_public_preview', return_value={"source":"website","image":{"mimeType":"image/png","data":"eA=="}}) as fetch:
            for _ in range(2):
                response=self.server.plugin.resource("remctl://reminder/demo-uuid/link/0", None)
                self.assertIn("image",json.loads(response["contents"][0]["text"]))
            self.assertEqual(fetch.call_count,1)
        cached = next((self.server.plugin.directory / 'link-previews').glob('*.json'))
        self.assertEqual(cached.stat().st_mode & 0o777,0o600)

    def test_extension_entrypoints_are_app_only_except_workspace(self):
        catalog={tool["name"]:tool for tool in self.server.plugin.descriptors()}
        self.assertEqual(catalog["open_workspace"]["inputSchema"],p.EMPTY)
        self.assertEqual(catalog["open_selection"]["_meta"]["ui"]["visibility"],["app"])
        self.assertEqual(catalog["open_remctl_file"]["_meta"]["openai/ui"]["entrypoints"][0]["extensions"],[".remctl"])
        self.assertEqual(catalog["search_mentions"]["_meta"]["openai/extensions"],{"mentions/search":{}})

    def test_unknown_or_invalid_settings_are_rejected(self):
        self.assertTrue(self.call("update_settings",{"set":{"refreshSeconds":True}})["isError"])
        self.assertTrue(self.call("update_settings",{"set":{"surprise":"value"}})["isError"])

    def test_mention_search_accepts_desktop_picker_navigation_context(self):
        link = {"type":"resource_link", "uri":"remctl://reminder/demo", "name":"Demo"}
        self.executor.stdout = json.dumps({"items":[link]})
        response = self.call("search_mentions", {"query":"Demo", "path":[]})
        self.assertFalse(response.get("isError"), response)
        self.assertEqual(response["structuredContent"]["items"], [link])
        request_value = json.loads(self.executor.calls[-1]["argv"][2])
        self.assertEqual(request_value["query"], "Demo")
        self.assertNotIn("path", request_value)
        self.assertTrue(self.call("search_mentions", {"query":"Demo", "unexpected":1})["isError"])

    def test_mention_typeahead_does_not_hydrate_reminder_details(self):
        api = {
            "q_all_lists": lambda db: [{"id":1, "title":"Demo list", "objectUUID":"list-id"}],
            "list_to_dict": lambda row: row,
            "search_fold": lambda value: value.casefold(),
            "q_reminders": lambda db, **kwargs: [
                {"ZLIST":1, "ZTITLE":"Verify capture", "ZCKIDENTIFIER":"reminder-id"},
                {"ZLIST":1, "ZTITLE":"Other", "ZCKIDENTIFIER":"other-id"}],
        }
        value = _read(None, {"query":"VERIFY"}, api, "mentions")
        self.assertEqual([item["uri"] for item in value["items"]], ["remctl://reminder/reminder-id"])
        self.assertEqual(_read(None, {"query":"demo"}, api, "mentions")["items"][0]["uri"], "remctl://list/list-id")

    def test_invalid_mutation_does_not_reserve_an_uncertain_operation(self):
        args={"operationId":"invalid-operation-001","tool":"update_reminder","arguments":{"reminder_id":42,"title":42}}
        self.assertTrue(self.call("workspace_mutate",args)["isError"])
        self.assertFalse((self.server.plugin.directory / "operations.json").exists())
        self.assertEqual(self.executor.calls,[])

    def test_modern_form_round_trip_and_replay(self):
        self.executor.stdout='{"lists":[{"id":9,"title":"Demo","objectUUID":"list-9"}]}'
        meta={m.META_PROTOCOL_VERSION:m.MODERN_PROTOCOL_VERSIONS[0],m.META_CLIENT_CAPABILITIES:{"extensions":{"openai/elicitation":{"form":{}}}}}
        first=self.call("choose_reminder_details",{},_meta=meta)
        self.assertEqual(first["resultType"],"input_required")
        self.assertEqual(first["inputRequests"]["details"]["method"],"openai/elicitation/create")
        choice=first["inputRequests"]["details"]["params"]["requestedSchema"]["properties"]["list_id"]["oneOf"][0]
        self.assertTrue(choice["x-openai-thumbnail"]["src"].startswith("data:image/png;base64,"))
        extra={"_meta":meta,"requestState":first["requestState"],"inputResponses":{"details":{"action":"accept","content":{"list_id":"9","tags":["work"]}}}}
        response=self.call("choose_reminder_details",{},**extra)
        self.assertEqual(response["structuredContent"]["content"],{"list_id":9,"set_tags":"work"})
        self.assertTrue(self.call("choose_reminder_details",{},**extra)["isError"])

    def test_native_form_reads_only_the_selected_local_image(self):
        image = Path(self.temp.name) / "chosen image.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        response = self.server.plugin.form_result({"action":"accept", "content":{"image":image.as_uri(),"tags":["demo"]}})
        value = response["structuredContent"]["content"]
        self.assertEqual(value["set_tags"], "demo")
        self.assertEqual(value["imagePayload"]["mimeType"], "image/png")
        self.assertNotIn(str(image), json.dumps(response))
        with self.assertRaises(ValueError):
            self.server.plugin.form_result({"action":"accept","content":{"image":"file://remote/image.png"}})
        self.assertEqual(self.server.plugin.form_result({"action":"cancel","content":{"image":image.as_uri()}})["structuredContent"]["content"], {})

if __name__ == '__main__':unittest.main()
