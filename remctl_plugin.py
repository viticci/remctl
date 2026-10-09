"""Desktop MCP extension contract and durable UI transactions for RemCTL."""
from __future__ import annotations

import base64
import contextlib
import time
import tempfile
import fcntl
import hashlib
import subprocess
import json
import os
import re
import threading
import uuid
import csv
from pathlib import Path
from urllib.parse import unquote, urlsplit

from remctl_runtime import resolve_config_dir, write_private_text_file

_UI_PATH = Path(__file__).with_name("remctl_workspace.html")
_UI_HTML = _UI_PATH.read_text() if _UI_PATH.exists() else ""
UI_URI = "ui://remctl/workspace-" + (hashlib.sha256(_UI_HTML.encode()).hexdigest()[:12] if _UI_HTML else "dev") + ".html"
MIME = "text/html;profile=mcp-app"
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
DEFAULTS = {"defaultList": "", "startView": "today", "layout": "list", "density": "comfortable",
            "weekStartsOn": "monday", "advancedFeatures": False, "showCompleted": False,
            "refreshSeconds": 30, "theme": "system", "loadLinkPreviews": True}
SETTINGS_SCHEMA = {"type": "object", "properties": {
    "defaultList": {"type": "string", "title": "Default list", "description": "List name or numeric ID"},
    "startView": {"type": "string", "title": "Open to", "enum": ["today", "scheduled", "flagged", "all", "assigned"]},
    "layout": {"type": "string", "title": "Layout", "enum": ["list", "columns", "calendar"]},
    "density": {"type": "string", "title": "Row spacing", "enum": ["comfortable", "compact"]},
    "weekStartsOn": {"type": "string", "title": "First day of week", "enum": ["monday", "sunday"]},
    "advancedFeatures": {"type": "boolean", "title": "Advanced Reminders features", "description": "Enable sections, tags, assignment, templates and other private ReminderKit features."},
    "showCompleted": {"type": "boolean", "title": "Show completed reminders"},
    "refreshSeconds": {"type": "integer", "title": "Refresh interval", "minimum": 15, "maximum": 300},
    "theme": {"type": "string", "title": "Appearance", "enum": ["system", "light", "dark"]},
    "loadLinkPreviews": {"type": "boolean", "title": "Load missing link previews", "description": "Fetch artwork from linked public websites when it is not saved on this Mac. Cached Reminders previews always stay available."},
}, "additionalProperties": False}
QUERY_SCHEMA = {"type": "object", "properties": {
    "view": {"type": "string", "enum": ["today", "scheduled", "flagged", "urgent", "overdue", "all", "completed", "deleted", "assigned", "list", "smart"]},
    "listId": {"type": "integer"}, "smartId": {"type": "integer"}, "query": {"type": "string", "maxLength": 512},
    "sort": {"type": "string", "enum": ["manual", "due", "title", "priority"]},
    "includeCompleted": {"type": "boolean"}, "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
}, "additionalProperties": False}


def validate(value, schema, path="arguments"):
    """Validate the bounded JSON shapes used by the plugin without a runtime dependency."""
    kind = schema.get("type")
    expected = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}.get(kind)
    if expected and (not isinstance(value, expected) or kind in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Invalid {path}")
    if kind == "object":
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ValueError(f"Unknown {path}: {', '.join(set(value) - set(props))}")
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], path + "." + key)
    if kind == "array":
        if len(value) > schema.get("maxItems", 500):
            raise ValueError(f"{path} is too long")
        for item in value:
            validate(item, schema.get("items", {}), path)
    if isinstance(value, str) and len(value) > schema.get("maxLength", 65536):
        raise ValueError(f"{path} is too long")
    if kind in {"integer", "number"} and not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
        raise ValueError(f"{path} is out of range")


def result(value, error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "structuredContent": value, "isError": error}


def descriptor(name, title, schema=EMPTY, read=True, entrypoints=None, model=False, extra=None):
    meta = {"ui": {"resourceUri": UI_URI, "visibility": ["app", "model"] if model else ["app"]}}
    if entrypoints:
        meta["openai/ui"] = {"entrypoints": entrypoints}
    if extra:
        meta.update(extra)
    icons = []
    if entrypoints:
        navigation = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.33" stroke-linecap="round"><circle cx="4" cy="3" r="1.4"/><circle cx="4" cy="7.7" r="1.4"/><path d="m2.6 12.4 1 1 1.9-2M9 3h8M9 7.7h8M9 12.4h8M9 17h8"/><circle cx="4" cy="17" r="1.4"/></svg>'
        icons = [{"src": "data:image/svg+xml;base64," + base64.b64encode(navigation.encode()).decode(), "mimeType": "image/svg+xml", "sizes": ["20x20"]}]
    return {"name": name, "title": title, "description": title + ". Powered by this Mac's Apple Reminders through RemCTL.",
            **({"icons": icons} if icons else {}),
            "inputSchema": schema, "annotations": {"readOnlyHint": read, "destructiveHint": not read,
            "idempotentHint": read, "openWorldHint": False}, "_meta": meta}


class Plugin:
    def __init__(self, server):
        self.server = server
        self.directory = resolve_config_dir() / "desktop"
        self.lock = threading.RLock()
        self.pending_forms = {}
        self.send_request = None

    @contextlib.contextmanager
    def state_lock(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock, (self.directory / "state.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read_state(self, name, default):
        path = self.directory / name
        return json.loads(path.read_text()) if path.exists() else default

    def settings(self):
        with self.state_lock():
            values = {**DEFAULTS, **self.read_state("settings.json", {})}
        return {"schema": SETTINGS_SCHEMA, "values": values,
                "layout": [{"kind": "group", "title": "Preferences", "items": [{"kind": "property", "property": key} for key in DEFAULTS]}]}

    def descriptors(self):
        operations = list(ADVANCED)
        tools = [
            descriptor("open_workspace", "Reminders", entrypoints=[{"type": "global"}], model=True),
            descriptor("open_selection", "Selected Reminders", entrypoints=[{"type": "thread"}]),
            descriptor("open_remctl_file", "RemCTL File", {"type": "object", "properties": {"file": {"type": "object", "properties": {"name": {"type": "string"}, "resourceUri": {"type": "string"}}, "required": ["name", "resourceUri"]}}, "required": ["file"]}, entrypoints=[{"type": "file", "extensions": [".remctl"]}]),
            descriptor("workspace_query", "Browse Reminders", QUERY_SCHEMA),
            descriptor("workspace_detail", "Reminder Details", {"type": "object", "properties": {"identifier": {}}, "required": ["identifier"], "additionalProperties": False}),
            descriptor("search_mentions", "Find a reminder or list", {"type": "object", "properties": {"query": {"type": "string", "maxLength": 512}}, "required": ["query"], "additionalProperties": False}, extra={"openai/extensions": {"mentions/search": {}}}),
            descriptor("read_settings", "RemCTL Settings"),
            descriptor("read_sidebar_order", "Sidebar Order"),
            descriptor("update_sidebar_order", "Save Sidebar Order", {"type": "object", "properties": {
                "scope": {"type": "string", "maxLength": 134},
                "order": {"type": "array", "maxItems": 500, "items": {"type": "string", "maxLength": 134}},
            }, "required": ["scope", "order"], "additionalProperties": False}, read=False),
            descriptor("update_settings", "Update RemCTL Settings", {"type": "object", "properties": {"set": SETTINGS_SCHEMA}, "required": ["set"], "additionalProperties": False}, read=False),
            descriptor("workspace_mutate", "Apply a Reminders change", {"type": "object", "properties": {
                "operationId": {"type": "string", "maxLength": 128}, "tool": {"type": "string"},
                "arguments": {"type": "object"}, "expectedRevision": {"type": "string"},
            }, "required": ["operationId", "tool", "arguments"], "additionalProperties": False}, read=False),
            descriptor("workspace_catalog", "Reminders Actions"),
            descriptor("export_remctl_file", "Export Reminders", {"type": "object", "properties": {"listId": {"type": "integer"}, "query": QUERY_SCHEMA, "format": {"type": "string", "enum": ["remctl", "json", "csv"]}}, "additionalProperties": False}, read=False),
            descriptor("review_import", "Review RemCTL Import", {"type": "object", "properties": {"document": {"type": "object"}}, "required": ["document"], "additionalProperties": False}),
            descriptor("import_remctl_file", "Import Reviewed Reminders", {"type": "object", "properties": {"reviewId": {"type": "string"}, "operationId": {"type": "string"}}, "required": ["reviewId", "operationId"], "additionalProperties": False}, read=False),
            descriptor("workspace_attach_image", "Attach an image", {"type": "object", "properties": {"operationId": {"type": "string"}, "reminderId": {"type": "integer"}, "mimeType": {"type": "string", "enum": ["image/png", "image/jpeg", "image/webp", "image/heic"]}, "data": {"type": "string", "maxLength": 12 * 1024 * 1024}}, "required": ["operationId", "reminderId", "mimeType", "data"], "additionalProperties": False}, read=False),
            descriptor("choose_reminder_details", "Choose Reminder Details", {"type": "object", "properties": {"listId": {"type": "integer"}, "reminderId": {"type": "integer"}}, "additionalProperties": False}),
            descriptor("event_activity", "Reminders Event Activity"),
            descriptor("preview_smart_filter", "Preview Smart List", {"type":"object","properties":{"filterJSON":{"type":"object"}},"required":["filterJSON"],"additionalProperties":False}),
            descriptor("export_attachment", "Save Attachment to Downloads", {"type":"object","properties":{"reminderId":{"type":"integer","minimum":1},"index":{"type":"integer","minimum":0,"maximum":500}},"required":["reminderId","index"],"additionalProperties":False}, read=False),
        ]
        tools[6]["outputSchema"] = {"type": "object", "properties": {"schema": {"type": "object"}, "values": SETTINGS_SCHEMA, "layout": {"type": "array"}}, "required": ["schema", "values", "layout"]}
        tools.extend(descriptor(name, spec["title"], spec["schema"], read=spec["read"]) for name, spec in ADVANCED.items())
        return tools

    def cli(self, argv, key, stdin=None):
        from remctl_mcp import Tool, tool_result_from_command
        command = self.server.config.executor.run(key, argv, timeout=120, stdin_text=stdin)
        synthetic = Tool("workspace", "Reminders", "", (), lambda _: [], "generic", read_only=True)
        return tool_result_from_command(synthetic, command)

    def query(self, args, key):
        # Artwork belongs to the app-only catalog. Keep model-visible reminder
        # reads compact rather than repeating base64 icons with every snapshot.
        return self.cli(["workspace", "--request", json.dumps(args), "--json"], key)

    def call(self, name, args, context, key, meta=None):
        try:
            # Current desktop hosts include a picker navigation path alongside
            # the spec's query. RemCTL searches a flat resource collection.
            if name == "search_mentions" and isinstance(args, dict):
                args = {field: value for field, value in args.items() if field != "path"}
            desc = next(t for t in self.descriptors() if t["name"] == name)
            validate(args, desc["inputSchema"])
            value = self._call(name, args, context, key, meta or {})
        except (ValueError, OSError, StopIteration, subprocess.TimeoutExpired) as exc:
            value = result({"status": "error", "message": str(exc) or "Unknown action"}, True)
        value.setdefault("_meta", {}).update({"ui": {"resourceUri": UI_URI}, "remctl/surface": name})
        return value

    def _call(self, name, args, context, key, meta):
        if name == "event_activity":
            from remctl_events import local_principal
            return result(self.server.events.status(context.principal or local_principal()))
        if name == "preview_smart_filter":
            return self.query({"operation":"preview_filter", "view":"all", "limit":20, **args}, key)
        if name == "export_attachment":
            response = self.query({"operation":"attachment", "identifier":args["reminderId"], "index":args["index"], "download":True}, key)
            if response.get("isError"): return response
            attachment = response["structuredContent"]
            name = re.sub(r"[^\w. -]", "_", Path(attachment["filename"]).name)[:180] or "Attachment"
            path = Path.home() / "Downloads" / (uuid.uuid4().hex[:8] + "-" + name)
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as handle:
                os.chmod(path, 0o600)
                handle.write(base64.b64decode(attachment["blob"], validate=True))
            return result({"path":str(path),"filename":name})
        if name == "read_settings":
            return result(self.settings())
        if name == "read_sidebar_order":
            with self.state_lock():
                return result({"orders": self.read_state("sidebar-order.json", {})})
        if name == "update_sidebar_order":
            scope, order = args["scope"], args["order"]
            if not re.fullmatch(r"top|pinned|group:[A-Za-z0-9-]{1,128}", scope):
                raise ValueError("Invalid sidebar scope")
            if len(set(order)) != len(order) or any(not re.fullmatch(r"(?:list|smart):[A-Za-z0-9-]{1,128}", item) for item in order):
                raise ValueError("Invalid sidebar order")
            with self.state_lock():
                orders = self.read_state("sidebar-order.json", {})
                if order:
                    orders[scope] = order
                else:
                    orders.pop(scope, None)
                write_private_text_file(self.directory / "sidebar-order.json", json.dumps(orders))
            return result({"orders": orders})
        if name == "update_settings":
            with self.state_lock():
                values = {**DEFAULTS, **self.read_state("settings.json", {}), **args["set"]}
                write_private_text_file(self.directory / "settings.json", json.dumps(values))
            return result(self.settings())
        if name in {"open_workspace", "workspace_query"}:
            values = self.settings()["values"]
            query = {"view": values["startView"], "includeCompleted": values["showCompleted"], **args}
            response = self.query(query, key)
            response.setdefault("_meta", {})["remctl/settings"] = values
            return response
        if name == "open_selection":
            response = self.query({"view": "all", "limit": 1}, key)
            if response.get("isError"):
                return response
            response["structuredContent"].update({"surface": "selection", "items": [], "nextOffset": None})
            response["content"] = result(response["structuredContent"])["content"]
            return response
        if name == "open_remctl_file":
            return result({"surface": "file", **args})
        if name == "workspace_detail":
            return self.query({"operation": "detail", **args}, key)
        if name == "search_mentions":
            return self.query({"operation": "mentions", "view": "all", "limit": 30, **args}, key)
        if name == "workspace_catalog":
            from remctl_mcp import TOOLS, tool_descriptor
            try:
                symbols = json.loads(Path(__file__).with_name("remctl-list-symbols.json").read_text())
            except (OSError, ValueError):
                symbols = {}
            return result({"symbols": symbols, "tools": [tool_descriptor(t, ui_meta=None) for t in TOOLS if t.name != "run"] +
                           [t for t in self.descriptors() if t["name"] in ADVANCED]})
        if name in ADVANCED:
            spec = ADVANCED[name]
            argv, positional = [spec["command"], "--json"], []
            for field, value in args.items():
                mapping = spec["fields"][field]
                if mapping["position"]:
                    positional.append((mapping["position"], str(value)))
                elif mapping["boolean"]:
                    if value:
                        argv.append(mapping["flag"])
                elif mapping["array"]:
                    for item in value:
                        argv += [mapping["flag"], str(item)]
                else:
                    argv += [mapping["flag"], str(value)]
            if positional:
                argv += ["--"] + [value for _, value in sorted(positional)]
            return self.cli(argv, key)
        if name == "workspace_mutate":
            return self.mutate(args, context, key)
        if name == "export_remctl_file":
            if "query" in args and "listId" in args:
                raise ValueError("Choose a query or listId for export, not both")
            scope = args.get("query", {"view": "all", "includeCompleted": True, **({"listId": args["listId"]} if "listId" in args else {})})
            query = {**scope, "offset": 0, "limit": 500}
            items = []
            snapshot = None
            while True:
                response = self.query(query, key)
                if response.get("isError"):
                    return response
                data = response["structuredContent"]
                if snapshot is not None and snapshot != data["snapshot"]:
                    raise ValueError("Reminders changed during export. Try again for a consistent document.")
                snapshot = data["snapshot"]
                items.extend(data["items"])
                if data["nextOffset"] is None:
                    break
                query["offset"] = data["nextOffset"]
            document = {"format": "net.macstories.remctl", "version": 1, "reminders": items}
            directory = Path.home() / "Downloads"
            format = args.get("format", "remctl")
            path = directory / ("Reminders-" + uuid.uuid4().hex[:8] + "." + format)
            # Do not apply the private config-directory permissions to Downloads.
            directory.mkdir(exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                if format == "csv":
                    writer = csv.DictWriter(handle, fieldnames=["id","title","list","notes","dueDate","completed","priority","flagged","tags","url"], extrasaction="ignore")
                    writer.writeheader()
                    for item in items:
                        # Spreadsheet programs otherwise execute user-authored values as formulas.
                        writer.writerow({k:("'" + v if isinstance(v,str) and v.startswith(("=","+","-","@","\t","\r")) else v) for k,v in {**item,"tags":", ".join(item.get("tags",[]))}.items()})
                else:
                    json.dump(document if format == "remctl" else items, handle, ensure_ascii=False, indent=2)
            return result({"path": str(path), "count": len(items)})
        if name == "review_import":
            document = args["document"]
            if document.get("format") != "net.macstories.remctl" or document.get("version") != 1:
                raise ValueError("Not a supported RemCTL file")
            reminders = document.get("reminders")
            if not isinstance(reminders, list) or len(reminders) > 1000:
                raise ValueError("Import must contain at most 1,000 reminders")
            fields = {"title", "notes", "due", "dueDate", "priority", "list", "url", "flagged", "recurrence", "alarm"}
            ordinary = []
            omitted = set()
            for item in reminders:
                if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
                    raise ValueError("Every reminder needs a title")
                supported = {k: v for k, v in item.items() if k in fields}
                if item.get("allDay") and supported.get("dueDate"):
                    supported["dueDate"] = supported["dueDate"][:10]
                ordinary.append(supported)
                omitted.update(set(item) - fields - {"id", "objectUUID", "resourceUri", "revision", "listId", "listUUID", "deepLink", "createdDate"})
            checked = self.cli(["import", "-", "--dry-run", "--json"], key, json.dumps(ordinary))
            if checked.get("isError"):
                return checked
            review_id = uuid.uuid4().hex
            with self.state_lock():
                write_private_text_file(self.directory / ("import-" + review_id + ".json"), json.dumps(ordinary))
            return result({"reviewId": review_id, "count": len(ordinary), "items": ordinary,
                           "omittedFields": sorted(omitted), "createsNewReminders": True})
        if name == "import_remctl_file":
            if not re.fullmatch(r"[a-f0-9]{32}", args["reviewId"]):
                raise ValueError("Invalid import review")
            return self.mutate({"operationId": args["operationId"], "tool": "_reviewed_import", "arguments": {"reviewId": args["reviewId"]}}, context, key)
        if name == "choose_reminder_details":
            return self.form(args, context, key, meta)
        if name == "workspace_attach_image":
            return self.mutate({"operationId": args["operationId"], "tool": "_attach_image", "arguments": {**{k:v for k,v in args.items() if k != "operationId"}, "private": True}}, context, key)
        raise ValueError("Unknown plugin action")

    def mutate(self, args, context, key):
        from remctl_mcp import TOOLS_BY_NAME
        operation_id = args["operationId"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{12,128}", operation_id):
            raise ValueError("Operation ID must be a unique, durable identifier")
        tool, values = args["tool"], dict(args["arguments"])
        allowed = {name for name, t in TOOLS_BY_NAME.items() if not t.read_only and name != "run"}
        allowed.update(name for name, spec in ADVANCED.items() if not spec["read"])
        allowed.update({"_reviewed_import", "_attach_image"})
        if tool not in allowed:
            raise ValueError("This action is not a workspace mutation")
        if tool in TOOLS_BY_NAME:
            from remctl_mcp import validate_arguments
            normalized = validate_arguments(TOOLS_BY_NAME[tool], values)
            TOOLS_BY_NAME[tool].build_argv(normalized)
        elif tool in ADVANCED:
            validate(values, ADVANCED[tool]["schema"])
        elif tool == "_attach_image":
            validate({k:v for k,v in values.items() if k != "private"}, {"type":"object", "properties":{"reminderId":{"type":"integer","minimum":1},"mimeType":{"type":"string","enum":["image/png","image/jpeg","image/webp","image/heic"]},"data":{"type":"string","maxLength":12*1024*1024}},"required":["reminderId","mimeType","data"],"additionalProperties":False})
            if not values.get("private"):
                raise ValueError("Attaching images requires Advanced Reminders features")
            try:
                decoded_image = base64.b64decode(values["data"], validate=True)
            except ValueError as exc:
                raise ValueError("Image data is not valid base64") from exc
            if not decoded_image or len(decoded_image) > 8 * 1024 * 1024:
                raise ValueError("Choose an image smaller than 8 MiB")
        digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        with self.state_lock():
            journal = self.read_state("operations.json", {})
            previous = journal.get(operation_id)
            if previous:
                if previous["digest"] != digest:
                    raise ValueError("Operation ID has already been used for a different change")
                return previous.get("result") or result({"status": "uncertain", "message": "This change was already started. Refresh and check its outcome before making another change."}, True)
            if values.get("private") and not self.read_state("settings.json", DEFAULTS).get("advancedFeatures", False):
                raise ValueError("Enable Advanced Reminders features in Settings first")
            expected = args.get("expectedRevision")
            target_id = values.get("reminder_id") or (values.get("id") if tool == "manage_reminder_move" else None)
            if expected and target_id:
                current = self.query({"operation": "detail", "identifier": target_id}, key)
                if current.get("isError"):
                    return current
                if current["structuredContent"]["revision"] != expected:
                    return result({"status": "conflict", "message": "This reminder changed in Reminders. Reload it before saving.", "current": current["structuredContent"]}, True)
            journal[operation_id] = {"digest": digest}
            write_private_text_file(self.directory / "operations.json", json.dumps(journal))
            if tool == "_attach_image":
                try:
                    raw = base64.b64decode(values["data"], validate=True)
                except ValueError as exc:
                    raise ValueError("Image data is not valid base64") from exc
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Image exceeds 8 MiB")
                suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/heic": ".heic"}[values["mimeType"]]
                with tempfile.NamedTemporaryFile(suffix=suffix, dir=self.directory) as handle:
                    handle.write(raw)
                    handle.flush()
                    response = self.cli(["edit", str(values["reminderId"]), "--private", "--image", handle.name, "--json"], key)
            elif tool == "_reviewed_import":
                path = self.directory / ("import-" + values["reviewId"] + ".json")
                response = self.cli(["import", "-", "--json"], key, path.read_text())
            elif tool in ADVANCED:
                response = self.call(tool, values, context, key)
            else:
                response = self.server._tools_call({"name": tool, "arguments": values}, context, key)
            journal[operation_id]["result"] = response
            write_private_text_file(self.directory / "operations.json", json.dumps(journal))
            return response

    def resource(self, uri, context):
        if uri == UI_URI:
            # A running worker keeps its matching UI when a newer build is installed.
            return {"contents": [{"uri": uri, "mimeType": MIME, "text": _UI_HTML, "_meta": {
                "ui": {"csp": {"connectDomains": [], "resourceDomains": []}, "permissions": {"clipboardWrite": {}}},
                "openai/ui": {"preferredDisplayMode": "fullscreen", "availableDisplayModes": ["inline", "fullscreen"]}}}]}
        match = re.fullmatch(r"remctl://reminder/([A-Za-z0-9-]+)(?:/(attachment|link)/(\d+))?", uri)
        if match:
            args = {"operation": {"attachment":"attachment", "link":"link_preview"}.get(match[2], "detail"), "identifier": match[1]}
            if match[2]:
                args["index"] = int(match[3])
            response = self.query(args, ("resource", uuid.uuid4().hex))
            if response.get("isError"):
                raise ValueError(response["content"][0]["text"])
            payload = response["structuredContent"]
            if match[2] == "attachment":
                return {"contents": [{"uri": uri, **payload}]}
            if match[2] == "link" and not payload.get("image") and self.read_state("settings.json", DEFAULTS).get("loadLinkPreviews", True):
                payload = self.enrich_link_preview(payload)
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload)}]}
        match = re.fullmatch(r"remctl://list/([A-Za-z0-9-]+)", uri)
        if match:
            response = self.query({"view": "all", "limit": 1}, ("resource", uuid.uuid4().hex))
            data = response.get("structuredContent", {})
            target = next((item for item in data.get("lists", []) if item.get("objectUUID", "").lower() == match[1].lower()), None)
            if not target:
                raise ValueError("List no longer exists")
            response = self.query({"view": "list", "listId": target["id"], "limit": 100}, ("resource", uuid.uuid4().hex))
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(response["structuredContent"])}]}
        raise ValueError("Resource not found")

    def enrich_link_preview(self, saved):
        """Cache missing public artwork outside the protected reminder store."""
        from remctl_workspace import fetch_public_preview
        url = saved.get("url", "")
        directory = self.directory / "link-previews"
        path = directory / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        try:
            cached = json.loads(path.read_text()) if path.exists() else {}
            if cached.get("expires", 0) > time.time():
                fetched = cached.get("preview", {})
            else:
                try:
                    fetched = fetch_public_preview(url)
                except (OSError, ValueError):
                    fetched = {}
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_private_text_file(path, json.dumps({"expires":time.time() + (7*86400 if fetched.get("image") else 900), "preview":fetched}))
                # Bound private cached artwork by age and total disk usage.
                files = sorted(directory.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)
                size = 0
                for entry in files:
                    size += entry.stat().st_size
                    if size > 64 * 1024 * 1024:
                        entry.unlink(missing_ok=True)
            return {**fetched, **{k:v for k,v in saved.items() if v and k != "source"}, "source":saved.get("source") if saved.get("image") else fetched.get("source", "reminders")}
        except (OSError, ValueError):
            return saved

    def accept_response(self, message):
        with self.lock:
            pending = self.pending_forms.get(message.get("id"))
            if pending:
                pending["response"] = message
                pending["event"].set()

    def form(self, args, context, key, request):
        caps = context.capabilities
        extended = caps.get("extensions", {}).get("openai/elicitation", caps.get("experimental", {}).get("openai/elicitation", {}))
        if "form" not in extended:
            raise ValueError("Native rich forms are unavailable in this host. Use the inspector fields.")
        if context.era == "modern" and request.get("inputResponses"):
            state = request.get("requestState", "")
            with self.state_lock():
                pending = self.read_state("forms.json", {})
                saved = pending.pop(state, None)
                if not saved or saved["args"] != args or saved["expires"] < time.time():
                    raise ValueError("This form expired. Open it again.")
                write_private_text_file(self.directory / "forms.json", json.dumps(pending))
            response = request["inputResponses"].get("details", {})
            return self.form_result(response)
        snapshot = self.query({"view": "all", "limit": 1}, key)
        if snapshot.get("isError"):
            return snapshot
        lists = snapshot["structuredContent"]["lists"]
        thumb_key = hashlib.sha256(json.dumps(lists,sort_keys=True).encode()).hexdigest()
        cached = self.read_state("thumbnails.json", {})
        thumbnails = cached.get("images", {}) if cached.get("key") == thumb_key else {}
        renderer = Path(__file__).with_name("remctl-list-artwork")
        if not thumbnails and renderer.exists():
            rendered = subprocess.run([str(renderer), "--thumbnails"], input=json.dumps(lists), text=True, capture_output=True, timeout=25)
            if rendered.returncode == 0:
                thumbnails = json.loads(rendered.stdout)
                write_private_text_file(self.directory / "thumbnails.json", json.dumps({"key":thumb_key,"images":thumbnails}))
        def thumbnail(item):
            if str(item["id"]) in thumbnails:
                return {"src":thumbnails[str(item["id"])],"mimeType":"image/png"}
            # Source-only development may not have the installed AppKit renderer.
            icon = Path(__file__).with_name("remctl-mcp-icon-512.png")
            if not icon.exists(): icon = Path(__file__).parent / "assets" / "remctl-mcp-icon-512.png"
            return {"src":"data:image/png;base64,"+base64.b64encode(icon.read_bytes()).decode(),"mimeType":"image/png"}
        props = {
            "list_id": {"type": "string", "title": "List", "oneOf": [{"const": str(item["id"]), "title": item["title"], "description": "Groceries" if item.get("isGroceries") else "Reminders list", "x-openai-thumbnail": thumbnail(item)} for item in lists if not item.get("isGroup")]},
            "tags": {"type": "array", "title": "Tags", "items": {"type": "string", "x-openai-suggestions": []}},
            "image": {"type": "string", "format": "uri", "title": "Image", "x-openai-input": {"type": "resource", "options": [], "userOptions": {"kind": "file", "accept": [".png", ".jpg", ".jpeg", ".webp", ".heic"]}}},
        }
        if args.get("listId"):
            props["list_id"]["default"] = str(args["listId"])
        tags = self.cli(["tags", "--json"], key).get("structuredContent", {}).get("items", [])
        props["tags"]["items"]["x-openai-suggestions"] = [{"const": item.get("name", item.get("title", "")), "title": item.get("name", item.get("title", ""))} for item in tags if isinstance(item, dict)][:40]
        if args.get("reminderId"):
            detail = self.query({"operation": "detail", "identifier": args["reminderId"]}, key).get("structuredContent", {})
            props["tags"]["default"] = detail.get("tags", [])
            if detail.get("sharees"):
                props["assign"] = {"type": "string", "title": "Assign to", "oneOf": [{"const": item["objectUUID"], "title": item["name"], "description": "You" if item.get("currentUser") else "List member"} for item in detail["sharees"]]}
            for attachment in detail.get("attachments", []):
                props["image"]["x-openai-input"]["options"].append({"uri": attachment["resourceUri"], "name": attachment.get("filename", "Image"), "_meta": {"openai/preview": {"target": {"type": "mcp_app_tool", "name": "workspace_detail", "arguments": {"identifier": detail["id"]}}}}})
        form = {"method": "openai/elicitation/create", "params": {"mode": "form", "message": "Reminder details", "requestedSchema": {"type": "object", "properties": props}}}
        if context.era == "modern":
            state = uuid.uuid4().hex
            with self.state_lock():
                pending = self.read_state("forms.json", {})
                pending = {k:v for k,v in pending.items() if v["expires"] > time.time()}
                pending[state] = {"args": args, "expires": time.time() + 900}
                write_private_text_file(self.directory / "forms.json", json.dumps(pending))
            return {"resultType": "input_required", "inputRequests": {"details": form}, "requestState": state}
        if self.send_request is None:
            raise ValueError("Native forms need the desktop stdio connection")
        identifier = "remctl-form-" + uuid.uuid4().hex
        pending = {"event": threading.Event()}
        with self.lock:
            self.pending_forms[identifier] = pending
        try:
            self.send_request({"jsonrpc": "2.0", "id": identifier, **form})
            if not pending["event"].wait(900):
                raise ValueError("Form timed out; no reminder was changed")
            response = pending["response"]
            if "error" in response:
                raise ValueError(response["error"].get("message", "Form could not open"))
            return self.form_result(response.get("result", {}))
        finally:
            with self.lock:
                self.pending_forms.pop(identifier, None)

    @staticmethod
    def form_result(response):
        if response.get("action") != "accept":
            return result({"action": "cancel", "content": {}})
        content = response.get("content", {})
        if not isinstance(content, dict):
            raise ValueError("Invalid form response")
        output = {}
        if content.get("list_id"):
            output["list_id"] = int(content["list_id"])
        if content.get("assign"):
            output["assign"] = str(content["assign"])
        if "tags" in content:
            if not isinstance(content["tags"], list) or any(not isinstance(t, str) for t in content["tags"]):
                raise ValueError("Invalid tags")
            output["set_tags"] = ",".join(content["tags"])
        if content.get("image"):
            image_uri = str(content["image"])
            selected = urlsplit(image_uri)
            if selected.scheme == "file":
                # Only the native form response grants this read. Never accept a
                # filesystem path as a workspace tool argument or send it to JS.
                if selected.netloc not in {"", "localhost"}:
                    raise ValueError("Choose an image on this Mac")
                path = Path(unquote(selected.path))
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".heic": "image/heic"}.get(path.suffix.lower())
                if not mime or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("Choose a PNG, JPEG, WebP or HEIC image smaller than 8 MB")
                with path.open("rb") as stream:
                    raw = stream.read(8 * 1024 * 1024 + 1)
                if not raw or len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Choose an image smaller than 8 MB")
                output["imagePayload"] = {"mimeType": mime, "data": base64.b64encode(raw).decode()}
            else:
                output["image"] = image_uri
        return result({"action": "accept", "content": output})


ADVANCED = {'manage_groups': {'title': 'Groups',
                   'command': 'groups',
                   'schema': {'type': 'object',
                              'properties': {'command_format': {'type': 'string',
                                                                'description': 'Output format for this read '
                                                                               'command',
                                                                'enum': ['plain', 'table', 'json'],
                                                                'maxLength': 4096}},
                              'additionalProperties': False},
                   'fields': {'command_format': {'position': 0,
                                                 'boolean': False,
                                                 'array': False,
                                                 'flag': '--format'}},
                   'read': True},
 'manage_group_info': {'title': 'Group Info',
                       'command': 'group-info',
                       'schema': {'type': 'object',
                                  'properties': {'name': {'type': 'string',
                                                          'description': 'Group name',
                                                          'maxLength': 4096},
                                                 'group_id': {'type': 'integer',
                                                              'description': 'Read by numeric group ID from '
                                                                             'groups'}},
                                  'additionalProperties': False},
                       'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                  'group_id': {'position': 0,
                                               'boolean': False,
                                               'array': False,
                                               'flag': '--group-id'}},
                       'read': True},
 'manage_group_create': {'title': 'Group Create',
                         'command': 'group-create',
                         'schema': {'type': 'object',
                                    'properties': {'name': {'type': 'string',
                                                            'description': 'Group name',
                                                            'maxLength': 4096},
                                                   'add_list': {'type': 'array',
                                                                'items': {'type': 'string',
                                                                          'description': 'Move an existing list '
                                                                                         'into the new group by '
                                                                                         'name; repeatable'},
                                                                'maxItems': 100,
                                                                'maxLength': 4096},
                                                   'add_list_id': {'type': 'array',
                                                                   'items': {'type': 'integer',
                                                                             'description': 'Move an existing '
                                                                                            'list into the new '
                                                                                            'group by numeric ID; '
                                                                                            'repeatable'},
                                                                   'maxItems': 100},
                                                   'private': {'type': 'boolean',
                                                               'description': 'Required for private ReminderKit '
                                                                              'group creation'}},
                                    'additionalProperties': False,
                                    'required': ['name']},
                         'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                    'add_list': {'position': 0,
                                                 'boolean': False,
                                                 'array': True,
                                                 'flag': '--add-list'},
                                    'add_list_id': {'position': 0,
                                                    'boolean': False,
                                                    'array': True,
                                                    'flag': '--add-list-id'},
                                    'private': {'position': 0,
                                                'boolean': True,
                                                'array': False,
                                                'flag': '--private'}},
                         'read': False},
 'manage_group_edit': {'title': 'Group Edit',
                       'command': 'group-edit',
                       'schema': {'type': 'object',
                                  'properties': {'name': {'type': 'string',
                                                          'description': 'Group name',
                                                          'maxLength': 4096},
                                                 'group_id': {'type': 'integer',
                                                              'description': 'Edit by numeric group ID from '
                                                                             'groups'},
                                                 'new_name': {'type': 'string',
                                                              'description': 'Rename the group',
                                                              'maxLength': 4096},
                                                 'add_list': {'type': 'array',
                                                              'items': {'type': 'string',
                                                                        'description': 'Move an existing list '
                                                                                       'into the group by name; '
                                                                                       'repeatable'},
                                                              'maxItems': 100,
                                                              'maxLength': 4096},
                                                 'add_list_id': {'type': 'array',
                                                                 'items': {'type': 'integer',
                                                                           'description': 'Move an existing list '
                                                                                          'into the group by '
                                                                                          'numeric ID; '
                                                                                          'repeatable'},
                                                                 'maxItems': 100},
                                                 'remove_list': {'type': 'array',
                                                                 'items': {'type': 'string',
                                                                           'description': 'Move an existing child '
                                                                                          'list out of the group '
                                                                                          'by name; repeatable'},
                                                                 'maxItems': 100,
                                                                 'maxLength': 4096},
                                                 'remove_list_id': {'type': 'array',
                                                                    'items': {'type': 'integer',
                                                                              'description': 'Move an existing '
                                                                                             'child list out of '
                                                                                             'the group by '
                                                                                             'numeric ID; '
                                                                                             'repeatable'},
                                                                    'maxItems': 100},
                                                 'move_list': {'type': 'string',
                                                               'description': 'Move or reorder a list in this '
                                                                              'group by name',
                                                               'maxLength': 4096},
                                                 'move_list_id': {'type': 'integer',
                                                                  'description': 'Move or reorder a list in this '
                                                                                 'group by numeric ID'},
                                                 'before_list': {'type': 'string',
                                                                 'description': 'Place --move-list before this '
                                                                                'child list',
                                                                 'maxLength': 4096},
                                                 'before_list_id': {'type': 'integer',
                                                                    'description': 'Place --move-list before this '
                                                                                   'child list ID'},
                                                 'after_list': {'type': 'string',
                                                                'description': 'Place --move-list after this '
                                                                               'child list',
                                                                'maxLength': 4096},
                                                 'after_list_id': {'type': 'integer',
                                                                   'description': 'Place --move-list after this '
                                                                                  'child list ID'},
                                                 'first': {'type': 'boolean',
                                                           'description': 'Place --move-list first in the group'},
                                                 'last': {'type': 'boolean',
                                                          'description': 'Place --move-list last in the group'},
                                                 'private': {'type': 'boolean',
                                                             'description': 'Required for private ReminderKit '
                                                                            'group editing'}},
                                  'additionalProperties': False},
                       'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                  'group_id': {'position': 0,
                                               'boolean': False,
                                               'array': False,
                                               'flag': '--group-id'},
                                  'new_name': {'position': 0,
                                               'boolean': False,
                                               'array': False,
                                               'flag': '--new-name'},
                                  'add_list': {'position': 0,
                                               'boolean': False,
                                               'array': True,
                                               'flag': '--add-list'},
                                  'add_list_id': {'position': 0,
                                                  'boolean': False,
                                                  'array': True,
                                                  'flag': '--add-list-id'},
                                  'remove_list': {'position': 0,
                                                  'boolean': False,
                                                  'array': True,
                                                  'flag': '--remove-list'},
                                  'remove_list_id': {'position': 0,
                                                     'boolean': False,
                                                     'array': True,
                                                     'flag': '--remove-list-id'},
                                  'move_list': {'position': 0,
                                                'boolean': False,
                                                'array': False,
                                                'flag': '--move-list'},
                                  'move_list_id': {'position': 0,
                                                   'boolean': False,
                                                   'array': False,
                                                   'flag': '--move-list-id'},
                                  'before_list': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--before-list'},
                                  'before_list_id': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--before-list-id'},
                                  'after_list': {'position': 0,
                                                 'boolean': False,
                                                 'array': False,
                                                 'flag': '--after-list'},
                                  'after_list_id': {'position': 0,
                                                    'boolean': False,
                                                    'array': False,
                                                    'flag': '--after-list-id'},
                                  'first': {'position': 0, 'boolean': True, 'array': False, 'flag': '--first'},
                                  'last': {'position': 0, 'boolean': True, 'array': False, 'flag': '--last'},
                                  'private': {'position': 0,
                                              'boolean': True,
                                              'array': False,
                                              'flag': '--private'}},
                       'read': False},
 'manage_group_delete': {'title': 'Group Delete',
                         'command': 'group-delete',
                         'schema': {'type': 'object',
                                    'properties': {'name': {'type': 'string',
                                                            'description': 'Group name',
                                                            'maxLength': 4096},
                                                   'group_id': {'type': 'integer',
                                                                'description': 'Delete by numeric group ID from '
                                                                               'groups'},
                                                   'private': {'type': 'boolean',
                                                               'description': 'Required for private ReminderKit '
                                                                              'group deletion'},
                                                   'force': {'type': 'boolean',
                                                             'description': 'Required for --json/non-interactive '
                                                                            'use; skip confirmation'}},
                                    'additionalProperties': False},
                         'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                    'group_id': {'position': 0,
                                                 'boolean': False,
                                                 'array': False,
                                                 'flag': '--group-id'},
                                    'private': {'position': 0,
                                                'boolean': True,
                                                'array': False,
                                                'flag': '--private'},
                                    'force': {'position': 0, 'boolean': True, 'array': False, 'flag': '--force'}},
                         'read': False},
 'manage_sections': {'title': 'Sections',
                     'command': 'sections',
                     'schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                     'fields': {},
                     'read': True},
 'manage_section_create': {'title': 'Section Create',
                           'command': 'section-create',
                           'schema': {'type': 'object',
                                      'properties': {'name': {'type': 'string',
                                                              'description': 'New section name',
                                                              'maxLength': 4096},
                                                     'list': {'type': 'string',
                                                              'description': 'Target list name',
                                                              'maxLength': 4096},
                                                     'list_id': {'type': 'integer',
                                                                 'description': 'Target list by stable numeric '
                                                                                'ID'},
                                                     'private': {'type': 'boolean',
                                                                 'description': 'Required for private ReminderKit '
                                                                                'section creation'}},
                                      'additionalProperties': False,
                                      'required': ['name']},
                           'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                      'list': {'position': 0, 'boolean': False, 'array': False, 'flag': '--list'},
                                      'list_id': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--list-id'},
                                      'private': {'position': 0,
                                                  'boolean': True,
                                                  'array': False,
                                                  'flag': '--private'}},
                           'read': False},
 'manage_section_rename': {'title': 'Section Rename',
                           'command': 'section-rename',
                           'schema': {'type': 'object',
                                      'properties': {'name': {'type': 'string',
                                                              'description': 'Existing section name; omit when '
                                                                             'using --section-id',
                                                              'maxLength': 4096},
                                                     'new_name': {'type': 'string',
                                                                  'description': 'New section name',
                                                                  'maxLength': 4096},
                                                     'list': {'type': 'string',
                                                              'description': "The section's list name",
                                                              'maxLength': 4096},
                                                     'list_id': {'type': 'integer',
                                                                 'description': "The section's list by stable "
                                                                                'numeric ID'},
                                                     'section_id': {'type': 'string',
                                                                    'description': 'Target the section by stable '
                                                                                   'ID when names collide',
                                                                    'maxLength': 4096},
                                                     'private': {'type': 'boolean',
                                                                 'description': 'Required for private ReminderKit '
                                                                                'section renaming'}},
                                      'additionalProperties': False,
                                      'required': ['new_name']},
                           'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                      'new_name': {'position': 0,
                                                   'boolean': False,
                                                   'array': False,
                                                   'flag': '--new-name'},
                                      'list': {'position': 0, 'boolean': False, 'array': False, 'flag': '--list'},
                                      'list_id': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--list-id'},
                                      'section_id': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--section-id'},
                                      'private': {'position': 0,
                                                  'boolean': True,
                                                  'array': False,
                                                  'flag': '--private'}},
                           'read': False},
 'manage_section_delete': {'title': 'Section Delete',
                           'command': 'section-delete',
                           'schema': {'type': 'object',
                                      'properties': {'name': {'type': 'string',
                                                              'description': 'Existing section name; omit when '
                                                                             'using --section-id',
                                                              'maxLength': 4096},
                                                     'list': {'type': 'string',
                                                              'description': "The section's list name",
                                                              'maxLength': 4096},
                                                     'list_id': {'type': 'integer',
                                                                 'description': "The section's list by stable "
                                                                                'numeric ID'},
                                                     'section_id': {'type': 'string',
                                                                    'description': 'Target the section by stable '
                                                                                   'ID when names collide',
                                                                    'maxLength': 4096},
                                                     'force': {'type': 'boolean',
                                                               'description': 'Required for '
                                                                              '--json/non-interactive use; skip '
                                                                              'confirmation'},
                                                     'private': {'type': 'boolean',
                                                                 'description': 'Required for private ReminderKit '
                                                                                'section deletion'}},
                                      'additionalProperties': False},
                           'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                      'list': {'position': 0, 'boolean': False, 'array': False, 'flag': '--list'},
                                      'list_id': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--list-id'},
                                      'section_id': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--section-id'},
                                      'force': {'position': 0, 'boolean': True, 'array': False, 'flag': '--force'},
                                      'private': {'position': 0,
                                                  'boolean': True,
                                                  'array': False,
                                                  'flag': '--private'}},
                           'read': False},
 'manage_sharees': {'title': 'Sharees',
                    'command': 'sharees',
                    'schema': {'type': 'object',
                               'properties': {'list': {'type': 'string',
                                                       'description': 'Shared list name',
                                                       'maxLength': 4096},
                                              'list_id': {'type': 'integer',
                                                          'description': 'Shared list numeric ID'}},
                               'additionalProperties': False},
                    'fields': {'list': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                               'list_id': {'position': 0, 'boolean': False, 'array': False, 'flag': '--list-id'}},
                    'read': True},
 'manage_tags': {'title': 'Tags',
                 'command': 'tags',
                 'schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                 'fields': {},
                 'read': True},
 'manage_templates': {'title': 'Templates',
                      'command': 'templates',
                      'schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                      'fields': {},
                      'read': True},
 'manage_template_info': {'title': 'Template Info',
                          'command': 'template-info',
                          'schema': {'type': 'object',
                                     'properties': {'name': {'type': 'string',
                                                             'description': 'Template name',
                                                             'maxLength': 4096},
                                                    'template_id': {'type': 'integer',
                                                                    'description': 'Read by numeric template ID '
                                                                                   'from templates'}},
                                     'additionalProperties': False},
                          'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                     'template_id': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--template-id'}},
                          'read': True},
 'manage_template_create': {'title': 'Template Create',
                            'command': 'template-create',
                            'schema': {'type': 'object',
                                       'properties': {'name': {'type': 'string',
                                                               'description': 'Template name',
                                                               'maxLength': 4096},
                                                      'from_list': {'type': 'string',
                                                                    'description': 'Source list name',
                                                                    'maxLength': 4096},
                                                      'from_list_id': {'type': 'integer',
                                                                       'description': 'Source list numeric ID'},
                                                      'include_completed': {'type': 'boolean',
                                                                            'description': 'Include completed '
                                                                                           'reminders in the '
                                                                                           'saved template'},
                                                      'private': {'type': 'boolean',
                                                                  'description': 'Required for private '
                                                                                 'ReminderKit template creation'}},
                                       'additionalProperties': False,
                                       'required': ['name']},
                            'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                       'from_list': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--from-list'},
                                       'from_list_id': {'position': 0,
                                                        'boolean': False,
                                                        'array': False,
                                                        'flag': '--from-list-id'},
                                       'include_completed': {'position': 0,
                                                             'boolean': True,
                                                             'array': False,
                                                             'flag': '--include-completed'},
                                       'private': {'position': 0,
                                                   'boolean': True,
                                                   'array': False,
                                                   'flag': '--private'}},
                            'read': False},
 'manage_template_apply': {'title': 'Template Apply',
                           'command': 'template-apply',
                           'schema': {'type': 'object',
                                      'properties': {'name': {'type': 'string',
                                                              'description': 'Template name',
                                                              'maxLength': 4096},
                                                     'template_id': {'type': 'integer',
                                                                     'description': 'Apply by numeric template ID '
                                                                                    'from templates'},
                                                     'private': {'type': 'boolean',
                                                                 'description': 'Required for private ReminderKit '
                                                                                'template application'}},
                                      'additionalProperties': False},
                           'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                      'template_id': {'position': 0,
                                                      'boolean': False,
                                                      'array': False,
                                                      'flag': '--template-id'},
                                      'private': {'position': 0,
                                                  'boolean': True,
                                                  'array': False,
                                                  'flag': '--private'}},
                           'read': False},
 'manage_template_delete': {'title': 'Template Delete',
                            'command': 'template-delete',
                            'schema': {'type': 'object',
                                       'properties': {'name': {'type': 'string',
                                                               'description': 'Template name',
                                                               'maxLength': 4096},
                                                      'template_id': {'type': 'integer',
                                                                      'description': 'Delete by numeric template '
                                                                                     'ID from templates'},
                                                      'private': {'type': 'boolean',
                                                                  'description': 'Required for private '
                                                                                 'ReminderKit template deletion'},
                                                      'force': {'type': 'boolean',
                                                                'description': 'Required for '
                                                                               '--json/non-interactive use; skip '
                                                                               'confirmation'}},
                                       'additionalProperties': False},
                            'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                       'template_id': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--template-id'},
                                       'private': {'position': 0,
                                                   'boolean': True,
                                                   'array': False,
                                                   'flag': '--private'},
                                       'force': {'position': 0,
                                                 'boolean': True,
                                                 'array': False,
                                                 'flag': '--force'}},
                            'read': False},
 'manage_smart_lists': {'title': 'Smart Lists',
                        'command': 'smart-lists',
                        'schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                        'fields': {},
                        'read': True},
 'manage_smart_list_create': {'title': 'Smart List Create',
                              'command': 'smart-list-create',
                              'schema': {'type': 'object',
                                         'properties': {'name': {'type': 'string',
                                                                 'description': 'name',
                                                                 'maxLength': 4096},
                                                        'private': {'type': 'boolean',
                                                                    'description': 'Required for private '
                                                                                   'ReminderKit smart-list '
                                                                                   'creation'},
                                                        'color': {'type': 'string',
                                                                  'description': 'Smart-list color name or '
                                                                                 '#RRGGBB',
                                                                  'maxLength': 4096},
                                                        'symbol': {'type': 'string',
                                                                   'description': 'Official Reminders list symbol '
                                                                                  'name; run list-symbols',
                                                                   'maxLength': 4096},
                                                        'emoji': {'type': 'string',
                                                                  'description': 'Private Reminders list emoji '
                                                                                 'badge',
                                                                  'maxLength': 4096},
                                                        'match': {'type': 'string',
                                                                  'description': 'Match all or any supplied '
                                                                                 'filters',
                                                                  'enum': ['all', 'any'],
                                                                  'maxLength': 4096},
                                                        'filter_json': {'type': 'string',
                                                                        'description': 'Advanced: raw official '
                                                                                       'smart-list filter JSON or '
                                                                                       '@path',
                                                                        'maxLength': 4096},
                                                        'flagged': {'type': 'boolean',
                                                                    'description': 'Filter to flagged reminders'},
                                                        'priority': {'type': 'string',
                                                                     'description': 'Priority filter: high, '
                                                                                    'medium, low, none, or '
                                                                                    'comma-separated values',
                                                                     'maxLength': 4096},
                                                        'tags': {'type': 'string',
                                                                 'description': 'Selected tag filter, '
                                                                                'comma-separated; # prefix '
                                                                                'optional',
                                                                 'maxLength': 4096},
                                                        'tag_match': {'type': 'string',
                                                                      'description': 'Selected tag matching mode',
                                                                      'enum': ['all', 'any'],
                                                                      'maxLength': 4096},
                                                        'any_tag': {'type': 'boolean',
                                                                    'description': 'Filter to reminders with any '
                                                                                   'tag'},
                                                        'date': {'type': 'string',
                                                                 'description': 'Date filter',
                                                                 'enum': ['any', 'today'],
                                                                 'maxLength': 4096},
                                                        'date_today_include_past_due': {'type': 'boolean',
                                                                                        'description': 'Include '
                                                                                                       'past due '
                                                                                                       'reminders '
                                                                                                       'with '
                                                                                                       '--date '
                                                                                                       'today'},
                                                        'date_on': {'type': 'string',
                                                                    'description': 'Date filter: on YYYY-MM-DD',
                                                                    'maxLength': 4096},
                                                        'date_before': {'type': 'string',
                                                                        'description': 'Date filter: before '
                                                                                       'YYYY-MM-DD',
                                                                        'maxLength': 4096},
                                                        'date_after': {'type': 'string',
                                                                       'description': 'Date filter: after '
                                                                                      'YYYY-MM-DD',
                                                                       'maxLength': 4096},
                                                        'date_range': {'type': 'string',
                                                                       'description': 'Date filter range: '
                                                                                      'START,END',
                                                                       'maxLength': 4096},
                                                        'time': {'type': 'string',
                                                                 'description': 'Time-of-day filter',
                                                                 'enum': ['morning',
                                                                          'afternoon',
                                                                          'evening',
                                                                          'night'],
                                                                 'maxLength': 4096},
                                                        'include_list': {'type': 'array',
                                                                         'items': {'type': 'string',
                                                                                   'description': 'Include '
                                                                                                  'reminders from '
                                                                                                  'one list name'},
                                                                         'maxItems': 100,
                                                                         'maxLength': 4096},
                                                        'include_list_id': {'type': 'array',
                                                                            'items': {'type': 'integer',
                                                                                      'description': 'Include '
                                                                                                     'reminders '
                                                                                                     'from one '
                                                                                                     'numeric '
                                                                                                     'list ID'},
                                                                            'maxItems': 100},
                                                        'vehicle': {'type': 'string',
                                                                    'description': 'Location vehicle filter',
                                                                    'enum': ['connected'],
                                                                    'maxLength': 4096},
                                                        'location_title': {'type': 'string',
                                                                           'description': 'Specific location '
                                                                                          'title',
                                                                           'maxLength': 4096},
                                                        'latitude': {'type': 'number',
                                                                     'description': 'Specific location latitude'},
                                                        'longitude': {'type': 'number',
                                                                      'description': 'Specific location '
                                                                                     'longitude'},
                                                        'radius': {'type': 'number',
                                                                   'description': 'Specific location radius in '
                                                                                  'meters'},
                                                        'proximity': {'type': 'string',
                                                                      'description': 'Specific location proximity',
                                                                      'enum': ['enter',
                                                                               'leave',
                                                                               'arriving',
                                                                               'leaving'],
                                                                      'maxLength': 4096}},
                                         'additionalProperties': False,
                                         'required': ['name']},
                              'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                         'private': {'position': 0,
                                                     'boolean': True,
                                                     'array': False,
                                                     'flag': '--private'},
                                         'color': {'position': 0,
                                                   'boolean': False,
                                                   'array': False,
                                                   'flag': '--color'},
                                         'symbol': {'position': 0,
                                                    'boolean': False,
                                                    'array': False,
                                                    'flag': '--symbol'},
                                         'emoji': {'position': 0,
                                                   'boolean': False,
                                                   'array': False,
                                                   'flag': '--emoji'},
                                         'match': {'position': 0,
                                                   'boolean': False,
                                                   'array': False,
                                                   'flag': '--match'},
                                         'filter_json': {'position': 0,
                                                         'boolean': False,
                                                         'array': False,
                                                         'flag': '--filter-json'},
                                         'flagged': {'position': 0,
                                                     'boolean': True,
                                                     'array': False,
                                                     'flag': '--flagged'},
                                         'priority': {'position': 0,
                                                      'boolean': False,
                                                      'array': False,
                                                      'flag': '--priority'},
                                         'tags': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--tags'},
                                         'tag_match': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--tag-match'},
                                         'any_tag': {'position': 0,
                                                     'boolean': True,
                                                     'array': False,
                                                     'flag': '--any-tag'},
                                         'date': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--date'},
                                         'date_today_include_past_due': {'position': 0,
                                                                         'boolean': True,
                                                                         'array': False,
                                                                         'flag': '--date-today-include-past-due'},
                                         'date_on': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--date-on'},
                                         'date_before': {'position': 0,
                                                         'boolean': False,
                                                         'array': False,
                                                         'flag': '--date-before'},
                                         'date_after': {'position': 0,
                                                        'boolean': False,
                                                        'array': False,
                                                        'flag': '--date-after'},
                                         'date_range': {'position': 0,
                                                        'boolean': False,
                                                        'array': False,
                                                        'flag': '--date-range'},
                                         'time': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--time'},
                                         'include_list': {'position': 0,
                                                          'boolean': False,
                                                          'array': True,
                                                          'flag': '--include-list'},
                                         'include_list_id': {'position': 0,
                                                             'boolean': False,
                                                             'array': True,
                                                             'flag': '--include-list-id'},
                                         'vehicle': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--vehicle'},
                                         'location_title': {'position': 0,
                                                            'boolean': False,
                                                            'array': False,
                                                            'flag': '--location-title'},
                                         'latitude': {'position': 0,
                                                      'boolean': False,
                                                      'array': False,
                                                      'flag': '--latitude'},
                                         'longitude': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--longitude'},
                                         'radius': {'position': 0,
                                                    'boolean': False,
                                                    'array': False,
                                                    'flag': '--radius'},
                                         'proximity': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--proximity'}},
                              'read': False},
 'manage_smart_list_edit': {'title': 'Smart List Edit',
                            'command': 'smart-list-edit',
                            'schema': {'type': 'object',
                                       'properties': {'name': {'type': 'string',
                                                               'description': 'Custom smart list name',
                                                               'maxLength': 4096},
                                                      'smart_list_id': {'type': 'integer',
                                                                        'description': 'Edit by numeric '
                                                                                       'smart-list ID from '
                                                                                       'smart-lists'},
                                                      'private': {'type': 'boolean',
                                                                  'description': 'Required for private '
                                                                                 'ReminderKit smart-list editing'},
                                                      'color': {'type': 'string',
                                                                'description': 'Smart-list color name or #RRGGBB',
                                                                'maxLength': 4096},
                                                      'symbol': {'type': 'string',
                                                                 'description': 'Official Reminders list symbol '
                                                                                'name; run list-symbols',
                                                                 'maxLength': 4096},
                                                      'emoji': {'type': 'string',
                                                                'description': 'Private Reminders list emoji '
                                                                               'badge',
                                                                'maxLength': 4096},
                                                      'match': {'type': 'string',
                                                                'description': 'Match all or any supplied filters',
                                                                'enum': ['all', 'any'],
                                                                'maxLength': 4096},
                                                      'filter_json': {'type': 'string',
                                                                      'description': 'Advanced: raw official '
                                                                                     'smart-list filter JSON or '
                                                                                     '@path',
                                                                      'maxLength': 4096},
                                                      'flagged': {'type': 'boolean',
                                                                  'description': 'Filter to flagged reminders'},
                                                      'priority': {'type': 'string',
                                                                   'description': 'Priority filter: high, medium, '
                                                                                  'low, none, or comma-separated values',
                                                                   'maxLength': 4096},
                                                      'tags': {'type': 'string',
                                                               'description': 'Selected tag filter, '
                                                                              'comma-separated; # prefix optional',
                                                               'maxLength': 4096},
                                                      'tag_match': {'type': 'string',
                                                                    'description': 'Selected tag matching mode',
                                                                    'enum': ['all', 'any'],
                                                                    'maxLength': 4096},
                                                      'any_tag': {'type': 'boolean',
                                                                  'description': 'Filter to reminders with any '
                                                                                 'tag'},
                                                      'date': {'type': 'string',
                                                               'description': 'Date filter',
                                                               'enum': ['any', 'today'],
                                                               'maxLength': 4096},
                                                      'date_today_include_past_due': {'type': 'boolean',
                                                                                      'description': 'Include '
                                                                                                     'past due '
                                                                                                     'reminders '
                                                                                                     'with --date '
                                                                                                     'today'},
                                                      'date_on': {'type': 'string',
                                                                  'description': 'Date filter: on YYYY-MM-DD',
                                                                  'maxLength': 4096},
                                                      'date_before': {'type': 'string',
                                                                      'description': 'Date filter: before '
                                                                                     'YYYY-MM-DD',
                                                                      'maxLength': 4096},
                                                      'date_after': {'type': 'string',
                                                                     'description': 'Date filter: after '
                                                                                    'YYYY-MM-DD',
                                                                     'maxLength': 4096},
                                                      'date_range': {'type': 'string',
                                                                     'description': 'Date filter range: START,END',
                                                                     'maxLength': 4096},
                                                      'time': {'type': 'string',
                                                               'description': 'Time-of-day filter',
                                                               'enum': ['morning',
                                                                        'afternoon',
                                                                        'evening',
                                                                        'night'],
                                                               'maxLength': 4096},
                                                      'include_list': {'type': 'array',
                                                                       'items': {'type': 'string',
                                                                                 'description': 'Include '
                                                                                                'reminders from '
                                                                                                'one list name'},
                                                                       'maxItems': 100,
                                                                       'maxLength': 4096},
                                                      'include_list_id': {'type': 'array',
                                                                          'items': {'type': 'integer',
                                                                                    'description': 'Include '
                                                                                                   'reminders '
                                                                                                   'from one '
                                                                                                   'numeric list '
                                                                                                   'ID'},
                                                                          'maxItems': 100},
                                                      'vehicle': {'type': 'string',
                                                                  'description': 'Location vehicle filter',
                                                                  'enum': ['connected'],
                                                                  'maxLength': 4096},
                                                      'location_title': {'type': 'string',
                                                                         'description': 'Specific location title',
                                                                         'maxLength': 4096},
                                                      'latitude': {'type': 'number',
                                                                   'description': 'Specific location latitude'},
                                                      'longitude': {'type': 'number',
                                                                    'description': 'Specific location longitude'},
                                                      'radius': {'type': 'number',
                                                                 'description': 'Specific location radius in '
                                                                                'meters'},
                                                      'proximity': {'type': 'string',
                                                                    'description': 'Specific location proximity',
                                                                    'enum': ['enter',
                                                                             'leave',
                                                                             'arriving',
                                                                             'leaving'],
                                                                    'maxLength': 4096}},
                                       'additionalProperties': False},
                            'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                       'smart_list_id': {'position': 0,
                                                         'boolean': False,
                                                         'array': False,
                                                         'flag': '--smart-list-id'},
                                       'private': {'position': 0,
                                                   'boolean': True,
                                                   'array': False,
                                                   'flag': '--private'},
                                       'color': {'position': 0,
                                                 'boolean': False,
                                                 'array': False,
                                                 'flag': '--color'},
                                       'symbol': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--symbol'},
                                       'emoji': {'position': 0,
                                                 'boolean': False,
                                                 'array': False,
                                                 'flag': '--emoji'},
                                       'match': {'position': 0,
                                                 'boolean': False,
                                                 'array': False,
                                                 'flag': '--match'},
                                       'filter_json': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--filter-json'},
                                       'flagged': {'position': 0,
                                                   'boolean': True,
                                                   'array': False,
                                                   'flag': '--flagged'},
                                       'priority': {'position': 0,
                                                    'boolean': False,
                                                    'array': False,
                                                    'flag': '--priority'},
                                       'tags': {'position': 0, 'boolean': False, 'array': False, 'flag': '--tags'},
                                       'tag_match': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--tag-match'},
                                       'any_tag': {'position': 0,
                                                   'boolean': True,
                                                   'array': False,
                                                   'flag': '--any-tag'},
                                       'date': {'position': 0, 'boolean': False, 'array': False, 'flag': '--date'},
                                       'date_today_include_past_due': {'position': 0,
                                                                       'boolean': True,
                                                                       'array': False,
                                                                       'flag': '--date-today-include-past-due'},
                                       'date_on': {'position': 0,
                                                   'boolean': False,
                                                   'array': False,
                                                   'flag': '--date-on'},
                                       'date_before': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--date-before'},
                                       'date_after': {'position': 0,
                                                      'boolean': False,
                                                      'array': False,
                                                      'flag': '--date-after'},
                                       'date_range': {'position': 0,
                                                      'boolean': False,
                                                      'array': False,
                                                      'flag': '--date-range'},
                                       'time': {'position': 0, 'boolean': False, 'array': False, 'flag': '--time'},
                                       'include_list': {'position': 0,
                                                        'boolean': False,
                                                        'array': True,
                                                        'flag': '--include-list'},
                                       'include_list_id': {'position': 0,
                                                           'boolean': False,
                                                           'array': True,
                                                           'flag': '--include-list-id'},
                                       'vehicle': {'position': 0,
                                                   'boolean': False,
                                                   'array': False,
                                                   'flag': '--vehicle'},
                                       'location_title': {'position': 0,
                                                          'boolean': False,
                                                          'array': False,
                                                          'flag': '--location-title'},
                                       'latitude': {'position': 0,
                                                    'boolean': False,
                                                    'array': False,
                                                    'flag': '--latitude'},
                                       'longitude': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--longitude'},
                                       'radius': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--radius'},
                                       'proximity': {'position': 0,
                                                     'boolean': False,
                                                     'array': False,
                                                     'flag': '--proximity'}},
                            'read': False},
 'manage_smart_list_delete': {'title': 'Smart List Delete',
                              'command': 'smart-list-delete',
                              'schema': {'type': 'object',
                                         'properties': {'name': {'type': 'string',
                                                                 'description': 'Custom smart list name',
                                                                 'maxLength': 4096},
                                                        'smart_list_id': {'type': 'integer',
                                                                          'description': 'Delete by numeric '
                                                                                         'smart-list ID from '
                                                                                         'smart-lists'},
                                                        'private': {'type': 'boolean',
                                                                    'description': 'Required for private '
                                                                                   'ReminderKit smart-list '
                                                                                   'deletion'},
                                                        'force': {'type': 'boolean',
                                                                  'description': 'Required for '
                                                                                 '--json/non-interactive use; '
                                                                                 'skip confirmation'}},
                                         'additionalProperties': False},
                              'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                         'smart_list_id': {'position': 0,
                                                           'boolean': False,
                                                           'array': False,
                                                           'flag': '--smart-list-id'},
                                         'private': {'position': 0,
                                                     'boolean': True,
                                                     'array': False,
                                                     'flag': '--private'},
                                         'force': {'position': 0,
                                                   'boolean': True,
                                                   'array': False,
                                                   'flag': '--force'}},
                              'read': False},
 'manage_reminder_move': {'title': 'Reminder Move',
                          'command': 'reminder-move',
                          'schema': {'type': 'object',
                                     'properties': {'id': {'type': 'integer',
                                                           'description': 'Reminder numeric ID'},
                                                    'before': {'type': 'integer',
                                                               'description': 'Place immediately before reminder '
                                                                              'ID'},
                                                    'after': {'type': 'integer',
                                                              'description': 'Place immediately after reminder '
                                                                             'ID'},
                                                    'first': {'type': 'boolean',
                                                              'description': 'Place first in the current '
                                                                             'ordering'},
                                                    'last': {'type': 'boolean',
                                                             'description': 'Place last in the current ordering'},
                                                    'parent': {'type': 'integer',
                                                               'description': 'Make it a subtask of reminder ID; '
                                                                              'placed last unless a position is '
                                                                              'given'},
                                                    'top_level': {'type': 'boolean',
                                                                  'description': 'Make a subtask a top-level '
                                                                                 'reminder; placed after its parent '
                                                                                 'unless a position is given'},
                                                    'smart_list': {'type': 'string',
                                                                   'description': 'Custom smart list containing '
                                                                                  'the reminder',
                                                                   'maxLength': 4096},
                                                    'smart_list_id': {'type': 'integer',
                                                                      'description': 'Custom smart list numeric '
                                                                                     'ID'},
                                                    'private': {'type': 'boolean',
                                                                'description': 'Required for private ReminderKit '
                                                                               'ordering'}},
                                     'additionalProperties': False,
                                     'required': ['id']},
                          'fields': {'id': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                     'before': {'position': 0,
                                                'boolean': False,
                                                'array': False,
                                                'flag': '--before'},
                                     'after': {'position': 0, 'boolean': False, 'array': False, 'flag': '--after'},
                                     'first': {'position': 0, 'boolean': True, 'array': False, 'flag': '--first'},
                                     'last': {'position': 0, 'boolean': True, 'array': False, 'flag': '--last'},
                                     'parent': {'position': 0, 'boolean': False, 'array': False, 'flag': '--parent'},
                                     'top_level': {'position': 0,
                                                   'boolean': True,
                                                   'array': False,
                                                   'flag': '--top-level'},
                                     'smart_list': {'position': 0,
                                                    'boolean': False,
                                                    'array': False,
                                                    'flag': '--smart-list'},
                                     'smart_list_id': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--smart-list-id'},
                                     'private': {'position': 0,
                                                 'boolean': True,
                                                 'array': False,
                                                 'flag': '--private'}},
                          'read': False},
 'manage_list_pin': {'title': 'List Pin',
                     'command': 'list-pin',
                     'schema': {'type': 'object',
                                'properties': {'name': {'type': 'string',
                                                        'description': 'List or smart-list name to pin',
                                                        'maxLength': 4096},
                                               'list_id': {'type': 'integer',
                                                           'description': 'Pin a list by stable numeric ID'},
                                               'smart_list_id': {'type': 'integer',
                                                                 'description': 'Pin a smart list by stable '
                                                                                'numeric ID'},
                                               'private': {'type': 'boolean',
                                                           'description': 'Required for private ReminderKit list '
                                                                          'pinning'}},
                                'additionalProperties': False},
                     'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                'list_id': {'position': 0, 'boolean': False, 'array': False, 'flag': '--list-id'},
                                'smart_list_id': {'position': 0,
                                                  'boolean': False,
                                                  'array': False,
                                                  'flag': '--smart-list-id'},
                                'private': {'position': 0, 'boolean': True, 'array': False, 'flag': '--private'}},
                     'read': False},
 'manage_list_unpin': {'title': 'List Unpin',
                       'command': 'list-unpin',
                       'schema': {'type': 'object',
                                  'properties': {'name': {'type': 'string',
                                                          'description': 'List or smart-list name to unpin',
                                                          'maxLength': 4096},
                                                 'list_id': {'type': 'integer',
                                                             'description': 'Unpin a list by stable numeric ID'},
                                                 'smart_list_id': {'type': 'integer',
                                                                   'description': 'Unpin a smart list by stable '
                                                                                  'numeric ID'},
                                                 'private': {'type': 'boolean',
                                                             'description': 'Required for private ReminderKit '
                                                                            'list pinning'}},
                                  'additionalProperties': False},
                       'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                  'list_id': {'position': 0,
                                              'boolean': False,
                                              'array': False,
                                              'flag': '--list-id'},
                                  'smart_list_id': {'position': 0,
                                                    'boolean': False,
                                                    'array': False,
                                                    'flag': '--smart-list-id'},
                                  'private': {'position': 0,
                                              'boolean': True,
                                              'array': False,
                                              'flag': '--private'}},
                       'read': False},
 'manage_list_delete': {'title': 'List Delete',
                        'command': 'list-delete',
                        'schema': {'type': 'object',
                                   'properties': {'name': {'type': 'string',
                                                           'description': 'List name to delete',
                                                           'maxLength': 4096},
                                                  'list_id': {'type': 'integer',
                                                              'description': 'Delete a list by stable numeric ID'},
                                                  'force': {'type': 'boolean',
                                                            'description': 'Required for --json/non-interactive '
                                                                           'use; skip confirmation'}},
                                   'additionalProperties': False},
                        'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                   'list_id': {'position': 0,
                                               'boolean': False,
                                               'array': False,
                                               'flag': '--list-id'},
                                   'force': {'position': 0, 'boolean': True, 'array': False, 'flag': '--force'}},
                        'read': False},
 'manage_list_rename': {'title': 'List Rename',
                        'command': 'list-rename',
                        'schema': {'type': 'object',
                                   'properties': {'name': {'type': 'string',
                                                           'description': 'List name to rename',
                                                           'maxLength': 4096},
                                                  'new_name': {'type': 'string',
                                                               'description': 'New list name',
                                                               'maxLength': 4096},
                                                  'list_id': {'type': 'integer',
                                                              'description': 'Rename a list by stable numeric ID'},
                                                  'new_name_option': {'type': 'string',
                                                                      'description': 'New list name, useful with '
                                                                                     '--list-id',
                                                                      'maxLength': 4096}},
                                   'additionalProperties': False},
                        'fields': {'name': {'position': 1, 'boolean': False, 'array': False, 'flag': None},
                                   'new_name': {'position': 2, 'boolean': False, 'array': False, 'flag': None},
                                   'list_id': {'position': 0,
                                               'boolean': False,
                                               'array': False,
                                               'flag': '--list-id'},
                                   'new_name_option': {'position': 0,
                                                       'boolean': False,
                                                       'array': False,
                                                       'flag': '--new-name'}},
                        'read': False}}

# Read operations also belong in the command palette, alongside their direct UI.
for _name, _title, _command, _properties, _required in [
    ("manage_stats", "Reminder Statistics", "stats", {}, []),
    ("manage_subtasks", "Browse Subtasks", "subtasks", {"id":{"type":"integer","minimum":1}}, ["id"]),
    ("manage_links", "Reminder Links", "link", {"ids":{"type":"string","maxLength":1024}}, ["ids"]),
]:
    ADVANCED[_name] = {"title":_title,"command":_command,"read":True,
        "schema":{"type":"object","properties":_properties,"required":_required,"additionalProperties":False},
        "fields":{field:{"position":1,"boolean":False,"array":False,"flag":None} for field in _properties}}
