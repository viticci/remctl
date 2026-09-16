"""RemCTL's MCP (Model Context Protocol) server and client-connection helpers.

The server speaks the stateless 2026-07-28 MCP revision (``server/discover``,
per-request ``_meta`` version and capability fields, ``resultType`` on every
result) and stays a dual-era server for older ``initialize``-based clients
(2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05). It runs over stdio, uses only
the standard library, and executes every tool by spawning the installed
``remctl`` client with ``--json``, so the signed Capability Host keeps owning
every macOS permission exactly as it does for a terminal caller.

The module also contains the MCP Apps (``io.modelcontextprotocol/ui``) wiring
for the reminders widget, and the helpers ``remctl mcp install`` uses to
register the server with Claude Code, Codex, Claude Desktop/Cowork, and other
clients without hand-editing configuration files.
"""

from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# ── Protocol constants ───────────────────────────────────────────────────────

MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]
SUPPORTED_PROTOCOL_VERSIONS = MODERN_PROTOCOL_VERSIONS + LEGACY_PROTOCOL_VERSIONS

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_LEGACY_RESOURCE_NOT_FOUND = -32002
ERR_UNSUPPORTED_PROTOCOL_VERSION = -32022

SERVER_NAME = "remctl"
SERVER_TITLE = "RemCTL Reminders"
SERVER_WEBSITE = "https://github.com/viticci/remctl"

# MCP Apps extension (SEP-1865, spec 2026-01-26).
UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
UI_MIME_TYPE = "text/html;profile=mcp-app"
UI_RESOURCE_URI = "ui://remctl/reminders-v1.html"
UI_READABLE_RESOURCE_URIS = (UI_RESOURCE_URI,)
UI_RESULT_META_KEY = "net.macstories.remctl/ui"
UI_WIDGET_FILENAME = "remctl_mcp_widget.html"
UI_ACCENT = "#8b5cf6"

LIST_TTL_MS = 60 * 60 * 1000

SERVER_INSTRUCTIONS = (
    "RemCTL exposes the user's Apple Reminders on this Mac. Reads return JSON rows with a "
    "numeric `id`; pass that id to get_reminder, update_reminder, set_completion, "
    "set_flagged, and delete_reminder. Use deterministic due dates: YYYY-MM-DD for all-day "
    "reminders and 'YYYY-MM-DD HH:MM' for timed ones, or 'clear' to remove a due date. "
    "Lists are targeted by name or by numeric list_id; use lists when a name might be "
    "ambiguous. delete_reminder is permanent, so confirm with the user first. Use run only "
    "for commands the dedicated tools do not cover; pass exact argv items and include "
    "--json. Destructive run commands need --force. If a tool reports that the Capability "
    "Host is unavailable, call doctor and follow its fix text instead of retrying blindly."
)

TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
INTEGER_ID_RANGE = (1, 999_999_999)

_DEBUG = os.environ.get("REMCTL_MCP_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def _debug(message: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[remctl mcp] {message}\n")
        sys.stderr.flush()


# ── Media-type negotiation ───────────────────────────────────────────────────

def normalized_media_type(raw: str) -> str:
    """Canonical media type: lowercase essence, unquoted sorted parameters.

    RFC 9110 lets a host spell ``text/html; profile="mcp-app"`` or add
    ``;charset=utf-8``; a byte-exact comparison would refuse the widget to a
    client that clearly supports it.
    """

    pieces = str(raw).split(";")
    essence = pieces[0].strip().lower()
    parameters = []
    for piece in pieces[1:]:
        parameter = piece.strip()
        if not parameter:
            continue
        if "=" not in parameter:
            parameters.append(parameter.lower())
            continue
        name, value = parameter.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        parameters.append(f"{name.strip().lower()}={value}")
    return essence + "".join(";" + item for item in sorted(parameters))


def is_html_app_media_type(raw: str) -> bool:
    expected = normalized_media_type(UI_MIME_TYPE).split(";")
    actual = normalized_media_type(raw).split(";")
    if actual[0] != expected[0]:
        return False
    return set(expected[1:]).issubset(set(actual[1:]))


def client_supports_apps(capabilities: Any) -> bool:
    if not isinstance(capabilities, dict):
        return False
    extensions = capabilities.get("extensions")
    if not isinstance(extensions, dict):
        return False
    ui = extensions.get(UI_EXTENSION_ID)
    if not isinstance(ui, dict):
        return False
    mime_types = ui.get("mimeTypes")
    if not isinstance(mime_types, list):
        return False
    return any(isinstance(item, str) and is_html_app_media_type(item) for item in mime_types)


# ── Tool catalog ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Param:
    name: str
    type: str  # string | integer | boolean | array
    description: str
    required: bool = False
    enum: tuple[str, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    max_length: int | None = None
    default: Any = None
    items_type: str | None = None

    def schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            schema["enum"] = list(self.enum)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.default is not None:
            schema["default"] = self.default
        if self.type == "array":
            schema["items"] = {"type": self.items_type or "string"}
        return schema


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    params: tuple[Param, ...]
    build_argv: Callable[[dict[str, Any]], list[str]]
    profile: str  # reminders | reminder | change | lists | doctor | generic
    read_only: bool
    destructive: bool = False
    idempotent: bool = False
    timeout: float = 60.0
    require_one_of: tuple[str, ...] = ()
    mutually_exclusive: tuple[tuple[str, ...], ...] = ()
    output_schema: dict[str, Any] | None = None
    accepts_stdin: bool = False

    def input_schema(self) -> dict[str, Any]:
        properties = {param.name: param.schema() for param in self.params}
        schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        required = [param.name for param in self.params if param.required]
        if required:
            schema["required"] = required
        return schema

    def annotations(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": False,
        }


class ToolArgumentError(ValueError):
    """The arguments are well-formed JSON but not valid for the tool."""


REMINDER_ID = Param(
    "reminder_id",
    "integer",
    "Numeric reminder id from RemCTL results.",
    required=True,
    minimum=INTEGER_ID_RANGE[0],
    maximum=INTEGER_ID_RANGE[1],
)
LIST_NAME = Param("list", "string", "List name. RemCTL resolves exact, case-insensitive, then emoji-prefixed matches.", max_length=512)
LIST_ID = Param("list_id", "integer", "Stable numeric list id from lists.", minimum=INTEGER_ID_RANGE[0], maximum=INTEGER_ID_RANGE[1])
DUE_HELP = (
    "Due date: YYYY-MM-DD for all-day, 'YYYY-MM-DD HH:MM' for timed, relative forms such as "
    "tomorrow, 'tomorrow 09:30', +3d, or 'next friday'."
)
PRIORITY = Param("priority", "string", "Reminder priority.", enum=("high", "medium", "low", "none"))
RECURRENCE_HELP = (
    "Recurrence rule: daily, weekly, monthly, yearly; optional xN interval after the frequency "
    "(daily x2); weekday lists (weekly mon,wed,fri); month days (monthly 1,15); ordinal weekdays "
    "(monthly 4th-fri, monthly last-fri)."
)
ALARM_HELP = "Alarm relative to the due date (15m, 1h, 1d) or an ISO datetime."

ROWS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "count": {"type": "integer"},
    },
    "required": ["items", "count"],
}
OBJECT_OUTPUT_SCHEMA = {"type": "object"}


def _flag(args: dict[str, Any], key: str, option: str) -> list[str]:
    return [option] if args.get(key) else []


def _option(args: dict[str, Any], key: str, option: str) -> list[str]:
    value = args.get(key)
    if value is None or value == "":
        return []
    return [option, str(value)]


def _argv_today(args):
    return ["today", *(["--no-overdue"] if args.get("include_overdue") is False else []), "--json"]


def _argv_upcoming(args):
    return ["upcoming", str(args.get("days", 7)), "--json"]


def _argv_overdue(args):
    return ["overdue", "--json"]


def _argv_flagged(args):
    return ["flagged", "--json"]


def _argv_search(args):
    return ["search", str(args["query"]), *_flag(args, "include_completed", "--completed"), "--json"]


def _argv_show_list(args):
    argv = ["show"]
    if args.get("list_id") is not None:
        argv += ["--list-id", str(args["list_id"])]
    argv += _flag(args, "include_completed", "--completed")
    argv.append("--json")
    if args.get("list") is not None:
        argv += ["--", str(args["list"])]
    return argv


def _argv_lists(args):
    return ["lists", "--json"]


def _argv_get_reminder(args):
    return ["info", str(args["reminder_id"]), "--json"]


def _argv_create_reminder(args):
    argv = ["add"]
    argv += _option(args, "list", "--list")
    argv += _option(args, "list_id", "--list-id")
    argv += _option(args, "notes", "--notes")
    argv += _option(args, "due", "--due")
    argv += _option(args, "priority", "--priority")
    argv += _option(args, "recurrence", "--recurrence")
    argv += _option(args, "alarm", "--alarm")
    argv += _option(args, "url", "--url")
    argv += _option(args, "tags", "--tags")
    argv += _flag(args, "flagged", "--flag")
    argv += ["--json", "--", str(args["title"])]
    return argv


def _argv_update_reminder(args):
    argv = ["edit", str(args["reminder_id"])]
    argv += _option(args, "title", "--title")
    argv += _option(args, "list", "--list")
    argv += _option(args, "list_id", "--list-id")
    argv += _option(args, "notes", "--notes")
    argv += _option(args, "due", "--due")
    argv += _option(args, "priority", "--priority")
    argv += _option(args, "recurrence", "--recurrence")
    argv += _option(args, "alarm", "--alarm")
    argv += _option(args, "url", "--url")
    argv.append("--json")
    return argv


def _argv_set_completion(args):
    if args["completed"]:
        return ["done", str(args["reminder_id"]), *_option(args, "completion_date", "--date"), "--json"]
    return ["undone", str(args["reminder_id"]), "--json"]


def _argv_set_flagged(args):
    return ["flag" if args["flagged"] else "unflag", str(args["reminder_id"]), "--json"]


def _argv_delete_reminder(args):
    return ["delete", str(args["reminder_id"]), "--force", "--json"]


def _argv_doctor(args):
    return ["doctor", "--for-agent", "--json"]


RUN_FORBIDDEN_COMMANDS = frozenset({"mcp", "completion", "setup", "onboard", "permissions", "open"})


def _argv_run(args):
    argv = [str(item) for item in args.get("args") or []]
    if not argv:
        raise ToolArgumentError("args must contain at least one RemCTL argument, for example [\"lists\", \"--json\"].")
    command = next((item for item in argv if not item.startswith("-")), None)
    if command in RUN_FORBIDDEN_COMMANDS:
        raise ToolArgumentError(
            f"run does not execute `remctl {command}`; it is an interactive or setup command with no MCP equivalent."
        )
    return argv


UPDATE_FIELDS = ("title", "list", "list_id", "notes", "due", "priority", "recurrence", "alarm", "url")

TOOLS: tuple[Tool, ...] = (
    Tool(
        "today",
        "Today's Reminders",
        "Return reminders due today plus overdue reminders. Set include_overdue to false for today only.",
        (Param("include_overdue", "boolean", "Include overdue reminders with today's reminders.", default=True),),
        _argv_today, "reminders", read_only=True, idempotent=True, timeout=45, output_schema=ROWS_OUTPUT_SCHEMA,
    ),
    Tool(
        "upcoming",
        "Upcoming Reminders",
        "Return reminders due from today through the next N days (default 7).",
        (Param("days", "integer", "Days to look ahead beyond today.", minimum=1, maximum=365, default=7),),
        _argv_upcoming, "reminders", read_only=True, idempotent=True, timeout=45, output_schema=ROWS_OUTPUT_SCHEMA,
    ),
    Tool(
        "overdue",
        "Overdue Reminders",
        "Return every overdue reminder.",
        (),
        _argv_overdue, "reminders", read_only=True, idempotent=True, timeout=45, output_schema=ROWS_OUTPUT_SCHEMA,
    ),
    Tool(
        "flagged",
        "Flagged Reminders",
        "Return every flagged reminder.",
        (),
        _argv_flagged, "reminders", read_only=True, idempotent=True, timeout=45, output_schema=ROWS_OUTPUT_SCHEMA,
    ),
    Tool(
        "search",
        "Find Reminders",
        "Search reminder titles and notes. Active reminders only unless include_completed is true.",
        (
            Param("query", "string", "Text to find in reminder titles or notes.", required=True, max_length=512),
            Param("include_completed", "boolean", "Include completed reminders.", default=False),
        ),
        _argv_search, "reminders", read_only=True, idempotent=True, timeout=45, output_schema=ROWS_OUTPUT_SCHEMA,
    ),
    Tool(
        "show_list",
        "Show a List",
        "Return the reminders in one list in Reminders' display order, with sections. Target by list name or list_id.",
        (
            LIST_NAME,
            LIST_ID,
            Param("include_completed", "boolean", "Include completed reminders.", default=False),
        ),
        _argv_show_list, "reminders", read_only=True, idempotent=True, timeout=45,
        require_one_of=("list", "list_id"), mutually_exclusive=(("list", "list_id"),),
        output_schema=ROWS_OUTPUT_SCHEMA,
    ),
    Tool(
        "lists",
        "Reminder Lists",
        "Return every Reminders list with its numeric id, color, badge, type, and pin state.",
        (),
        _argv_lists, "lists", read_only=True, idempotent=True, timeout=45, output_schema=ROWS_OUTPUT_SCHEMA,
    ),
    Tool(
        "get_reminder",
        "Get Reminder",
        "Return one reminder's complete record: notes, due date, recurrence, alarms, tags, section, subtasks, attachments, and deep link.",
        (REMINDER_ID,),
        _argv_get_reminder, "reminder", read_only=True, idempotent=True, timeout=45, output_schema=OBJECT_OUTPUT_SCHEMA,
    ),
    Tool(
        "create_reminder",
        "Create Reminder",
        "Create one reminder. Returns status, the new numeric id, and any warnings (a failed flag step keeps the reminder).",
        (
            Param("title", "string", "Reminder title.", required=True, max_length=1024),
            LIST_NAME,
            LIST_ID,
            Param("notes", "string", "Plain-text notes.", max_length=16 * 1024),
            Param("due", "string", DUE_HELP, max_length=128),
            PRIORITY,
            Param("recurrence", "string", RECURRENCE_HELP, max_length=128),
            Param("alarm", "string", ALARM_HELP, max_length=64),
            Param("url", "string", "URL appended to the notes.", max_length=2048),
            Param("tags", "string", "Comma-separated tags added as inline #hashtags in the title.", max_length=512),
            Param("flagged", "boolean", "Flag the reminder after creating it.", default=False),
        ),
        _argv_create_reminder, "change", read_only=False, timeout=150,
        mutually_exclusive=(("list", "list_id"),), output_schema=OBJECT_OUTPUT_SCHEMA,
    ),
    Tool(
        "update_reminder",
        "Update Reminder",
        "Change one or more fields of a reminder. Pass 'clear' as due, recurrence, or alarm to remove them. A list move can return a new id plus oldId.",
        (
            REMINDER_ID,
            Param("title", "string", "Replacement title.", max_length=1024),
            LIST_NAME,
            LIST_ID,
            Param("notes", "string", "Replacement notes.", max_length=16 * 1024),
            Param("due", "string", DUE_HELP + " Use clear to remove the due date.", max_length=128),
            PRIORITY,
            Param("recurrence", "string", RECURRENCE_HELP + " Use clear to remove recurrence.", max_length=128),
            Param("alarm", "string", ALARM_HELP + " Use clear to remove the alarm.", max_length=64),
            Param("url", "string", "URL appended to the notes.", max_length=2048),
        ),
        _argv_update_reminder, "change", read_only=False, idempotent=True, timeout=90,
        require_one_of=UPDATE_FIELDS, mutually_exclusive=(("list", "list_id"),), output_schema=OBJECT_OUTPUT_SCHEMA,
    ),
    Tool(
        "set_completion",
        "Set Completion",
        "Mark a reminder done or not done. An optional completion_date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM') records when it was done.",
        (
            REMINDER_ID,
            Param("completed", "boolean", "true marks the reminder done; false marks it not done.", required=True),
            Param("completion_date", "string", "Completion date for completed=true; not allowed for recurring reminders.", max_length=32),
        ),
        _argv_set_completion, "change", read_only=False, idempotent=True, timeout=150, output_schema=OBJECT_OUTPUT_SCHEMA,
    ),
    Tool(
        "set_flagged",
        "Set Flag",
        "Flag or unflag a reminder through Reminders automation inside the signed host.",
        (
            REMINDER_ID,
            Param("flagged", "boolean", "true flags the reminder; false removes the flag.", required=True),
        ),
        _argv_set_flagged, "change", read_only=False, idempotent=True, timeout=150, output_schema=OBJECT_OUTPUT_SCHEMA,
    ),
    Tool(
        "delete_reminder",
        "Delete Reminder",
        "Permanently delete one reminder by numeric id. Confirm with the user before calling.",
        (REMINDER_ID,),
        _argv_delete_reminder, "change", read_only=False, destructive=True, idempotent=True, timeout=150,
        output_schema=OBJECT_OUTPUT_SCHEMA,
    ),
    Tool(
        "doctor",
        "Diagnose RemCTL",
        "Report RemCTL's installation and permission state. access.effective is the authoritative readiness result.",
        (),
        _argv_doctor, "doctor", read_only=True, idempotent=True, timeout=120, output_schema=OBJECT_OUTPUT_SCHEMA,
    ),
    Tool(
        "run",
        "Run RemCTL Command",
        "Run any other RemCTL command with exact argv items (no shell). Covers list groups, sections, smart lists, templates, "
        "export/import, private metadata (--private), reminder ordering, and diagnostics. Always include --json; destructive "
        "commands need --force. Prefer the dedicated tools when one fits.",
        (
            Param("args", "array", "Exact RemCTL argv items, for example [\"groups\", \"--json\"].", required=True, items_type="string"),
            Param("stdin", "string", "Optional text passed to RemCTL standard input (for example import -).", max_length=1024 * 1024),
        ),
        _argv_run, "generic", read_only=False, destructive=True, timeout=600, accepts_stdin=True,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_descriptor(tool: Tool, *, ui_meta: dict[str, Any] | None) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "inputSchema": tool.input_schema(),
        "annotations": tool.annotations(),
    }
    if tool.output_schema is not None:
        descriptor["outputSchema"] = tool.output_schema
    if ui_meta:
        descriptor["_meta"] = ui_meta
    return descriptor


def _coerce_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,18}", value.strip()):
        return int(value.strip())
    return None


def _coerce_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    return None


def validate_arguments(tool: Tool, arguments: Any) -> dict[str, Any]:
    """Validate and normalize tool arguments; raise ToolArgumentError on misuse.

    Integer-like strings and boolean-like strings are accepted, because widget
    actions substitute row fields as text and models occasionally do the same.
    """

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolArgumentError("arguments must be a JSON object.")
    known = {param.name: param for param in tool.params}
    unknown = sorted(set(arguments) - set(known))
    if unknown:
        raise ToolArgumentError(f"Unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(known) or 'none'}.")
    values: dict[str, Any] = {}
    for param in tool.params:
        raw = arguments.get(param.name)
        if raw is None:
            if param.required:
                raise ToolArgumentError(f"Missing required argument: {param.name}.")
            continue
        if param.type == "integer":
            value = _coerce_integer(raw)
            if value is None:
                raise ToolArgumentError(f"{param.name} must be an integer.")
            if param.minimum is not None and value < param.minimum:
                raise ToolArgumentError(f"{param.name} must be at least {param.minimum}.")
            if param.maximum is not None and value > param.maximum:
                raise ToolArgumentError(f"{param.name} must be at most {param.maximum}.")
        elif param.type == "boolean":
            value = _coerce_boolean(raw)
            if value is None:
                raise ToolArgumentError(f"{param.name} must be true or false.")
        elif param.type == "string":
            if not isinstance(raw, str):
                raise ToolArgumentError(f"{param.name} must be a string.")
            value = raw
            if "\x00" in value:
                raise ToolArgumentError(f"{param.name} must not contain NUL bytes.")
            if param.max_length is not None and len(value) > param.max_length:
                raise ToolArgumentError(f"{param.name} is longer than {param.max_length} characters.")
            if param.enum and value not in param.enum:
                raise ToolArgumentError(f"{param.name} must be one of: {', '.join(param.enum)}.")
        elif param.type == "array":
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ToolArgumentError(f"{param.name} must be an array of strings.")
            if any("\x00" in item for item in raw):
                raise ToolArgumentError(f"{param.name} must not contain NUL bytes.")
            if len(raw) > 128 or sum(len(item.encode("utf-8")) for item in raw) > 48 * 1024:
                raise ToolArgumentError(f"{param.name} is too large (max 128 items, 48 KiB).")
            value = list(raw)
        else:  # pragma: no cover - catalog bug
            raise ToolArgumentError(f"Unsupported parameter type for {param.name}.")
        values[param.name] = value
    for group in tool.mutually_exclusive:
        present = [name for name in group if values.get(name) is not None]
        if len(present) > 1:
            raise ToolArgumentError(f"Pass only one of: {', '.join(group)}.")
    if tool.require_one_of and not any(values.get(name) is not None for name in tool.require_one_of):
        raise ToolArgumentError(f"Pass at least one of: {', '.join(tool.require_one_of)}.")
    return values


# ── MCP Apps metadata ────────────────────────────────────────────────────────

UI_POLICY: dict[str, Any] = {
    "prefersBorder": True,
    "csp": {"connectDomains": [], "resourceDomains": []},
    "permissions": {"clipboardWrite": {}},
}
UI_WIDGET_DESCRIPTION = (
    "Renders RemCTL results as reminder lists with check-off, reschedule, rename, and delete actions, "
    "single-reminder cards, change confirmations, list tables, and diagnostics."
)

REMINDER_ROW_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "complete",
        "title": "Mark Done",
        "kind": "checkOff",
        "toolName": "set_completion",
        "arguments": {"reminder_id": "{id}", "completed": True},
        "requiredRowFields": ["id"],
        "completedWhenField": "completed",
    },
    {
        "id": "reschedule",
        "title": "Reschedule",
        "kind": "rowButton",
        "toolName": "update_reminder",
        "arguments": {"reminder_id": "{id}", "due": "{input.due}"},
        "requiredRowFields": ["id"],
        "inputs": [{"name": "due", "title": "New due", "placeholder": "tomorrow 9:30 · +3d · 2026-10-01"}],
    },
    {
        "id": "rename",
        "title": "Edit Title",
        "kind": "rowButton",
        "toolName": "update_reminder",
        "arguments": {"reminder_id": "{id}", "title": "{input.title}"},
        "requiredRowFields": ["id"],
        "inputs": [{"name": "title", "title": "Title", "prefillField": "title"}],
    },
    {
        "id": "delete",
        "title": "Delete",
        "kind": "rowButton",
        "toolName": "delete_reminder",
        "arguments": {"reminder_id": "{id}"},
        "requiredRowFields": ["id"],
        "style": "destructive",
    },
]


def tool_ui_meta(*, apps: bool, legacy_aliases: bool) -> dict[str, Any]:
    """Tool descriptor `_meta` for the widget linkage.

    Standard nested `ui.resourceUri` is emitted only to clients that negotiated
    MCP Apps. Legacy clients that did not negotiate still receive the flat
    deprecated alias and ChatGPT's aliases; a modern client without Apps
    receives nothing.
    """

    meta: dict[str, Any] = {}
    if apps:
        meta["ui"] = {"resourceUri": UI_RESOURCE_URI, "visibility": ["model", "app"]}
    if apps or legacy_aliases:
        meta["ui/resourceUri"] = UI_RESOURCE_URI
        meta["openai/outputTemplate"] = UI_RESOURCE_URI
        meta["openai/widgetAccessible"] = True
    return meta


def result_ui_meta(tool: Tool, *, apps: bool, legacy_aliases: bool) -> dict[str, Any]:
    meta = tool_ui_meta(apps=apps, legacy_aliases=legacy_aliases)
    if not meta:
        return {}
    hints: dict[str, Any] = {
        "version": 1,
        "profile": tool.profile,
        "toolName": tool.name,
        "toolTitle": tool.title,
        "accent": UI_ACCENT,
    }
    if tool.profile in {"reminders", "reminder", "generic"}:
        hints["actions"] = REMINDER_ROW_ACTIONS
    meta[UI_RESULT_META_KEY] = hints
    return meta


def ui_resource_descriptor() -> dict[str, Any]:
    return {
        "uri": UI_RESOURCE_URI,
        "name": "remctl-reminders",
        "title": "RemCTL Reminders",
        "description": UI_WIDGET_DESCRIPTION,
        "mimeType": UI_MIME_TYPE,
        "_meta": {"ui": UI_POLICY},
    }


def ui_resource_content_meta() -> dict[str, Any]:
    return {
        "ui": UI_POLICY,
        "openai/widgetDescription": UI_WIDGET_DESCRIPTION,
        "openai/widgetPrefersBorder": True,
        "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
    }


def default_widget_path() -> Path:
    return Path(__file__).resolve().parent / UI_WIDGET_FILENAME


def load_widget_html(path: Path | None = None) -> str:
    return (path or default_widget_path()).read_text(encoding="utf-8")


# ── Prompts ──────────────────────────────────────────────────────────────────

PROMPTS: tuple[dict[str, Any], ...] = (
    {
        "name": "daily_review",
        "title": "Daily Review",
        "description": "Review today's and overdue reminders, then propose what to do, reschedule, or drop.",
        "arguments": [],
    },
    {
        "name": "plan_week",
        "title": "Plan the Week",
        "description": "Summarize the upcoming reminders and suggest a realistic plan.",
        "arguments": [
            {"name": "days", "description": "Days to look ahead (default 7).", "required": False},
        ],
    },
)


def prompt_messages(name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    if name == "daily_review":
        text = (
            "Call the today tool (include overdue). Group the results into overdue, due today, and flagged. "
            "For each overdue reminder propose one action: do it today, reschedule to a specific date, or ask me "
            "whether to delete it. Do not change anything until I confirm."
        )
    elif name == "plan_week":
        days = _coerce_integer(arguments.get("days")) if arguments else None
        days = days if days and 1 <= days <= 365 else 7
        text = (
            f"Call the upcoming tool with days={days}. Summarize what is due per day, flag days that look "
            "overloaded, and suggest which reminders to move earlier or later. Ask before rescheduling anything."
        )
    else:
        raise KeyError(name)
    return [{"role": "user", "content": {"type": "text", "text": text}}]


# ── Command execution ────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


class CommandExecutor:
    """Runs the RemCTL client as a subprocess and tracks it for cancellation."""

    def __init__(self, command: list[str]) -> None:
        self.command = list(command)
        self._lock = threading.Lock()
        self._processes: dict[Any, subprocess.Popen[bytes]] = {}

    def run(self, key: Any, argv: list[str], *, timeout: float, stdin_text: str | None = None) -> CommandResult:
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        env["REMCTL_SKIP_ONBOARD"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("REMCTL_IMAGES", None)
        try:
            process = subprocess.Popen(
                [*self.command, *argv],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            return CommandResult(argv, None, "", f"could not start RemCTL: {exc}")
        with self._lock:
            self._processes[key] = process
        timed_out = False
        try:
            stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else b""
            try:
                stdout, stderr = process.communicate(stdin_bytes, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout, stderr = process.communicate()
        finally:
            with self._lock:
                cancelled = self._processes.pop(key, None) is None
        return CommandResult(
            argv,
            process.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def cancel(self, key: Any) -> bool:
        with self._lock:
            process = self._processes.pop(key, None)
        if process is None:
            return False
        try:
            process.terminate()
        except OSError:
            pass
        return True


def _parse_json_document(text: str) -> tuple[bool, Any]:
    stripped = text.strip()
    if not stripped:
        return False, None
    try:
        return True, json.loads(stripped)
    except ValueError:
        return False, None


def _structured_error_from_stderr(stderr: str) -> dict[str, Any] | None:
    for line in reversed(stderr.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            ok, value = _parse_json_document(line)
            if ok and isinstance(value, dict):
                return value
    return None


def tool_result_from_command(tool: Tool, result: CommandResult) -> dict[str, Any]:
    """Map one CLI run to a CallToolResult (without era-specific fields)."""

    if result.cancelled:
        return _error_result({"code": "cancelled", "message": f"{tool.name} was cancelled."})
    if result.timed_out:
        return _error_result({"code": "timeout", "message": f"{tool.name} timed out after {int(tool.timeout)} seconds."})
    if result.returncode is None:
        return _error_result({"code": "spawn_failed", "message": result.stderr.strip() or "RemCTL could not start."})
    if result.returncode != 0:
        error = _structured_error_from_stderr(result.stderr)
        if error is None:
            message = result.stderr.strip() or result.stdout.strip() or f"RemCTL exited with status {result.returncode}."
            error = {"code": "nonzero_exit", "message": _tail(message, 4000), "exitCode": result.returncode}
        else:
            error.setdefault("code", "error")
            error["exitCode"] = result.returncode
        return _error_result(error)
    ok, value = _parse_json_document(result.stdout)
    if ok:
        if isinstance(value, list):
            structured: Any = {"items": value, "count": len(value)}
        elif isinstance(value, dict):
            structured = value
        else:
            structured = {"value": value}
    else:
        structured = {"output": _tail(result.stdout, 100_000)}
    if result.stderr.strip():
        warnings = [line for line in result.stderr.strip().splitlines() if line.strip()]
        if isinstance(structured, dict) and "stderr" not in structured:
            structured = dict(structured)
            structured["stderr"] = _tail("\n".join(warnings), 4000)
    text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured, "isError": False}


def _error_result(error: dict[str, Any]) -> dict[str, Any]:
    structured = {"error": error}
    return {
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":"))}],
        "structuredContent": structured,
        "isError": True,
    }


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


# ── JSON-RPC server ──────────────────────────────────────────────────────────

class RPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class RequestContext:
    era: str  # modern | legacy
    version: str | None
    apps: bool
    legacy_aliases: bool


@dataclass
class ServerConfig:
    version: str
    executor: CommandExecutor
    widget_path: Path | None = None
    icons: list[dict[str, Any]] = field(default_factory=list)
    max_workers: int = 4


class MCPServer:
    """Dual-era stdio MCP server. `handle_message` is transport-agnostic."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._legacy_capabilities: dict[str, Any] = {}
        self._legacy_version: str | None = None
        self._widget_html: str | None = None

    # -- identity -------------------------------------------------------------

    def implementation(self, *, with_icons: bool) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": self.config.version,
            "websiteUrl": SERVER_WEBSITE,
        }
        if with_icons and self.config.icons:
            info["icons"] = list(self.config.icons)
        return info

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
            "prompts": {"listChanged": False},
            "extensions": {UI_EXTENSION_ID: {}},
        }

    def widget_html(self) -> str:
        if self._widget_html is None:
            self._widget_html = load_widget_html(self.config.widget_path)
        return self._widget_html

    # -- era classification ---------------------------------------------------

    def classify(self, method: str, params: Any) -> RequestContext:
        meta = params.get("_meta") if isinstance(params, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        has_version = META_PROTOCOL_VERSION in meta
        has_capabilities = META_CLIENT_CAPABILITIES in meta
        if method == "server/discover" or has_version or has_capabilities:
            version = meta.get(META_PROTOCOL_VERSION)
            if has_version and version not in MODERN_PROTOCOL_VERSIONS:
                raise RPCError(
                    ERR_UNSUPPORTED_PROTOCOL_VERSION,
                    "Unsupported protocol version",
                    {"supported": list(MODERN_PROTOCOL_VERSIONS), "requested": version},
                )
            if method != "server/discover" and not (has_version and has_capabilities):
                raise RPCError(
                    ERR_INVALID_PARAMS,
                    f"Modern MCP requests require {META_PROTOCOL_VERSION} and {META_CLIENT_CAPABILITIES} in params._meta.",
                    {"supported": list(MODERN_PROTOCOL_VERSIONS)},
                )
            capabilities = meta.get(META_CLIENT_CAPABILITIES)
            return RequestContext(
                "modern",
                version if has_version else MODERN_PROTOCOL_VERSIONS[0],
                apps=client_supports_apps(capabilities),
                legacy_aliases=False,
            )
        apps = client_supports_apps(self._legacy_capabilities)
        return RequestContext("legacy", self._legacy_version, apps=apps, legacy_aliases=not apps)

    # -- message handling -----------------------------------------------------

    def handle_message(self, message: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Handle one decoded JSON-RPC message (or legacy batch). Returns the response or None."""

        if isinstance(message, list):
            if not message:
                return self._error_response(None, ERR_INVALID_REQUEST, "Invalid Request")
            responses = [self._handle_single(item) for item in message]
            responses = [response for response in responses if response is not None]
            return responses or None
        return self._handle_single(message)

    def _handle_single(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error_response(None, ERR_INVALID_REQUEST, "Invalid Request")
        has_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        if "method" not in message:
            # A response from the client; this server never sends requests, so ignore it.
            return None
        if not isinstance(method, str):
            return self._error_response(request_id if has_id else None, ERR_INVALID_REQUEST, "Invalid Request")
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            if has_id:
                return self._error_response(request_id, ERR_INVALID_PARAMS, "params must be an object")
            return None
        if not has_id or request_id is None:
            self._handle_notification(method, params or {})
            return None
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return self._error_response(None, ERR_INVALID_REQUEST, "Invalid Request")
        _debug(f"request {request_id} {method}")
        try:
            context = self.classify(method, params or {})
            result = self._dispatch(method, params or {}, context, request_id)
        except RPCError as exc:
            return self._error_response(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive
            _debug(f"internal error: {exc!r}")
            return self._error_response(request_id, ERR_INTERNAL, "Internal error")
        return self._result_response(request_id, result, context)

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        _debug(f"notification {method}")
        if method == "notifications/cancelled":
            request_id = params.get("requestId")
            if request_id is not None:
                self.config.executor.cancel(request_id)
        # notifications/initialized and unknown notifications are ignored by contract.

    def _dispatch(self, method: str, params: dict[str, Any], context: RequestContext, request_id: Any) -> dict[str, Any]:
        if method == "server/discover":
            return {
                "supportedVersions": list(MODERN_PROTOCOL_VERSIONS),
                "capabilities": self.capabilities(),
                "instructions": SERVER_INSTRUCTIONS,
                "ttlMs": LIST_TTL_MS,
                "cacheScope": "public",
                "_meta": {META_SERVER_INFO: self.implementation(with_icons=True)},
            }
        if method == "initialize":
            if context.era == "modern":
                raise RPCError(
                    ERR_METHOD_NOT_FOUND,
                    "initialize is a legacy method; use per-request _meta with server/discover.",
                    {"supported": list(MODERN_PROTOCOL_VERSIONS)},
                )
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._tools_list(context)
        if method == "tools/call":
            return self._tools_call(params, context, request_id)
        if method == "resources/list":
            return self._resources_list(context)
        if method == "resources/templates/list":
            return self._cacheable({"resourceTemplates": []}, context)
        if method == "resources/read":
            return self._resources_read(params, context)
        if method == "prompts/list":
            return self._cacheable({"prompts": [dict(prompt) for prompt in PROMPTS]}, context)
        if method == "prompts/get":
            return self._prompts_get(params)
        if method in {"tasks/get", "tasks/update", "tasks/cancel", "completion/complete", "logging/setLevel",
                      "subscriptions/listen", "resources/subscribe", "resources/unsubscribe"}:
            raise RPCError(ERR_METHOD_NOT_FOUND, f"Method not supported: {method}")
        raise RPCError(ERR_METHOD_NOT_FOUND, f"Method not found: {method}")

    # -- responses ------------------------------------------------------------

    def _result_response(self, request_id: Any, result: dict[str, Any], context: RequestContext) -> dict[str, Any]:
        payload = dict(result)
        if context.era == "modern":
            payload.setdefault("resultType", "complete")
            meta = dict(payload.get("_meta") or {})
            meta.setdefault(META_SERVER_INFO, self.implementation(with_icons=False))
            payload["_meta"] = meta
        else:
            for key in ("resultType", "ttlMs", "cacheScope"):
                payload.pop(key, None)
            meta = payload.get("_meta")
            if isinstance(meta, dict):
                meta = {key: value for key, value in meta.items() if key != META_SERVER_INFO}
                if meta:
                    payload["_meta"] = meta
                else:
                    payload.pop("_meta", None)
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    @staticmethod
    def _error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _cacheable(result: dict[str, Any], context: RequestContext, *, scope: str = "public") -> dict[str, Any]:
        if context.era == "modern":
            result["ttlMs"] = LIST_TTL_MS
            result["cacheScope"] = scope
        return result

    # -- legacy initialize ----------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_PROTOCOL_VERSIONS else LATEST_LEGACY_PROTOCOL_VERSION
        capabilities = params.get("capabilities")
        self._legacy_capabilities = capabilities if isinstance(capabilities, dict) else {}
        self._legacy_version = version
        client = params.get("clientInfo")
        if isinstance(client, dict):
            _debug(f"legacy client {client.get('name')} {client.get('version')} negotiated {version}")
        return {
            "protocolVersion": version,
            "capabilities": self.capabilities(),
            "serverInfo": self.implementation(with_icons=True),
            "instructions": SERVER_INSTRUCTIONS,
        }

    # -- tools ----------------------------------------------------------------

    def _tools_list(self, context: RequestContext) -> dict[str, Any]:
        ui_meta = tool_ui_meta(apps=context.apps, legacy_aliases=context.legacy_aliases)
        tools = [tool_descriptor(tool, ui_meta=ui_meta) for tool in TOOLS]
        return self._cacheable({"tools": tools}, context)

    def _tools_call(self, params: dict[str, Any], context: RequestContext, request_id: Any) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RPCError(ERR_INVALID_PARAMS, "Missing tool name.")
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise RPCError(ERR_INVALID_PARAMS, f"Unknown tool: {name}", {"availableTools": [item.name for item in TOOLS]})
        try:
            arguments = validate_arguments(tool, params.get("arguments"))
            argv = tool.build_argv(arguments)
        except ToolArgumentError as exc:
            result = _error_result({"code": "invalid_argument", "message": str(exc)})
        else:
            stdin_text = arguments.get("stdin") if tool.accepts_stdin else None
            command = self.config.executor.run(request_id, argv, timeout=tool.timeout, stdin_text=stdin_text)
            result = tool_result_from_command(tool, command)
        meta = result_ui_meta(tool, apps=context.apps, legacy_aliases=context.legacy_aliases)
        if meta:
            result["_meta"] = meta
        return result

    # -- resources ------------------------------------------------------------

    def _resources_list(self, context: RequestContext) -> dict[str, Any]:
        resources = [ui_resource_descriptor()] if context.apps else []
        return self._cacheable({"resources": resources}, context)

    def _resources_read(self, params: dict[str, Any], context: RequestContext) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise RPCError(ERR_INVALID_PARAMS, "Missing resource uri.")
        if uri not in UI_READABLE_RESOURCE_URIS:
            code = ERR_INVALID_PARAMS if context.era == "modern" else ERR_LEGACY_RESOURCE_NOT_FOUND
            raise RPCError(code, "Resource not found", {"uri": uri})
        content = {
            "uri": UI_RESOURCE_URI,
            "mimeType": UI_MIME_TYPE,
            "text": self.widget_html(),
            "_meta": ui_resource_content_meta(),
        }
        return self._cacheable({"contents": [content]}, context)

    # -- prompts --------------------------------------------------------------

    @staticmethod
    def _prompts_get(params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        prompt = next((item for item in PROMPTS if item["name"] == name), None)
        if prompt is None:
            raise RPCError(ERR_INVALID_PARAMS, f"Unknown prompt: {name}")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        return {"description": prompt["description"], "messages": prompt_messages(prompt["name"], arguments)}


# ── stdio transport ──────────────────────────────────────────────────────────

def _encode(message: Any) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def serve_stdio(server: MCPServer, stdin: io.BufferedReader | None = None, stdout: io.BufferedWriter | None = None) -> int:
    """Run the newline-delimited JSON-RPC loop until stdin closes.

    Requests are answered concurrently by a small worker pool so a slow tool
    call (an AppleScript flag can take a minute) never blocks `ping` or
    `tools/list`. Responses are written whole, under one lock, one per line.
    """

    reader = stdin or sys.stdin.buffer
    writer = stdout or sys.stdout.buffer
    write_lock = threading.Lock()

    def send(payload: Any) -> None:
        if payload is None:
            return
        data = _encode(payload)
        with write_lock:
            writer.write(data)
            writer.flush()

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, server.config.max_workers))
    pending: set[concurrent.futures.Future[None]] = set()
    try:
        while True:
            line = reader.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                send(MCPServer._error_response(None, ERR_PARSE, "Parse error"))
                continue

            def work(payload: Any = message) -> None:
                send(server.handle_message(payload))

            is_notification = isinstance(message, dict) and ("id" not in message or message.get("id") is None)
            if is_notification:
                work()  # cancellations must not queue behind the call they cancel
            else:
                future = pool.submit(work)
                pending.add(future)
                future.add_done_callback(pending.discard)
    finally:
        pool.shutdown(wait=True)
    return 0


def build_executor_command(cli_path: Path) -> list[str]:
    override = os.environ.get("REMCTL_MCP_CLI")
    if override:
        return [override]
    return [sys.executable, str(cli_path)]


def serve(cli_path: Path, version: str, *, widget_path: Path | None = None, icons: list[dict[str, Any]] | None = None) -> int:
    executor = CommandExecutor(build_executor_command(cli_path))
    server = MCPServer(ServerConfig(version=version, executor=executor, widget_path=widget_path, icons=icons or []))
    _debug(f"serving {SERVER_NAME} {version} for {cli_path}")
    return serve_stdio(server)


def icon_data_uri(path: Path | None) -> list[dict[str, Any]]:
    """Small PNG icon as a data: URI for serverInfo; empty when unavailable or too big."""

    if path is None or not path.is_file():
        return []
    data = path.read_bytes()
    if len(data) > 48 * 1024:
        return []
    encoded = base64.b64encode(data).decode("ascii")
    return [{"src": f"data:image/png;base64,{encoded}", "mimeType": "image/png", "sizes": ["64x64"]}]


# ── Client registration ──────────────────────────────────────────────────────

CLIENT_IDS = ("claude-code", "codex", "claude-desktop", "other")
CLAUDE_DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
CLAUDE_CODE_CONFIG = Path.home() / ".claude.json"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"


def server_command(cli_path: Path) -> tuple[str, list[str]]:
    """The interpreter and arguments every client should launch.

    The explicit interpreter matters for GUI hosts such as Claude Desktop, which
    start servers with a minimal PATH that may not contain python3.
    """

    return os.path.realpath(sys.executable), [str(cli_path), "mcp"]


def _run(argv: list[str], *, timeout: float = 60.0, runner: Callable[..., Any] | None = None) -> subprocess.CompletedProcess[str]:
    run = runner or subprocess.run
    return run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def detect_clients() -> list[dict[str, Any]]:
    claude = shutil.which("claude")
    codex = shutil.which("codex")
    desktop_app = next(
        (path for path in (Path("/Applications/Claude.app"), Path.home() / "Applications" / "Claude.app") if path.is_dir()),
        None,
    )
    return [
        {"id": "claude-code", "name": "Claude Code", "installed": bool(claude), "detail": claude or "claude CLI not on PATH"},
        {"id": "codex", "name": "Codex", "installed": bool(codex), "detail": codex or "codex CLI not on PATH"},
        {
            "id": "claude-desktop",
            "name": "Claude Desktop and Cowork",
            "installed": desktop_app is not None,
            "detail": str(desktop_app) if desktop_app else "Claude.app not found",
        },
    ]


def install_claude_code(cli_path: Path, *, scope: str = "user", runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    if scope not in {"user", "local", "project"}:
        raise ValueError("scope must be user, local, or project")
    claude = shutil.which("claude")
    if not claude:
        return {"client": "claude-code", "ok": False, "error": "The claude CLI is not on PATH. Install Claude Code first."}
    command, args = server_command(cli_path)
    add = [claude, "mcp", "add", "--scope", scope, "--transport", "stdio", SERVER_NAME, "--", command, *args]
    result = _run(add, runner=runner)
    if result.returncode != 0 and "already exists" in (result.stdout + result.stderr):
        _run([claude, "mcp", "remove", "--scope", scope, SERVER_NAME], runner=runner)
        result = _run(add, runner=runner)
    if result.returncode != 0:
        return {"client": "claude-code", "ok": False, "error": (result.stderr or result.stdout).strip() or "claude mcp add failed"}
    return {
        "client": "claude-code",
        "ok": True,
        "scope": scope,
        "command": add,
        "note": "Available in new Claude Code sessions; run /mcp inside an open session to reconnect.",
    }


def remove_claude_code(*, scope: str = "user", runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    claude = shutil.which("claude")
    if not claude:
        return {"client": "claude-code", "ok": False, "error": "The claude CLI is not on PATH."}
    result = _run([claude, "mcp", "remove", "--scope", scope, SERVER_NAME], runner=runner)
    return {"client": "claude-code", "ok": result.returncode == 0, "error": None if result.returncode == 0 else (result.stderr or result.stdout).strip()}


def install_codex(cli_path: Path, *, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    codex = shutil.which("codex")
    if not codex:
        return {"client": "codex", "ok": False, "error": "The codex CLI is not on PATH. Install Codex first."}
    command, args = server_command(cli_path)
    add = [codex, "mcp", "add", SERVER_NAME, "--", command, *args]
    result = _run(add, runner=runner)
    if result.returncode != 0 and "already exists" in (result.stdout + result.stderr).lower():
        _run([codex, "mcp", "remove", SERVER_NAME], runner=runner)
        result = _run(add, runner=runner)
    if result.returncode != 0:
        return {"client": "codex", "ok": False, "error": (result.stderr or result.stdout).strip() or "codex mcp add failed"}
    return {
        "client": "codex",
        "ok": True,
        "command": add,
        "note": "Shared by Codex CLI, the ChatGPT desktop app, and the IDE extension; new sessions pick it up.",
    }


def remove_codex(*, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    codex = shutil.which("codex")
    if not codex:
        return {"client": "codex", "ok": False, "error": "The codex CLI is not on PATH."}
    result = _run([codex, "mcp", "remove", SERVER_NAME], runner=runner)
    return {"client": "codex", "ok": result.returncode == 0, "error": None if result.returncode == 0 else (result.stderr or result.stdout).strip()}


def _read_json_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json_config(path: Path, value: dict[str, Any]) -> Path | None:
    """Atomically rewrite a client config, keeping a timestamped backup and the file mode."""

    backup = None
    mode = 0o600
    if path.exists():
        mode = stat_module.S_IMODE(path.stat().st_mode)
        backup = path.with_name(path.name + f".remctl-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".remctl-tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)
    return backup


def install_claude_desktop(cli_path: Path, *, config_path: Path | None = None) -> dict[str, Any]:
    """Merge the server into Claude Desktop's config, which Cowork also bridges."""

    path = config_path or CLAUDE_DESKTOP_CONFIG
    try:
        config = _read_json_config(path)
    except ValueError as exc:
        return {"client": "claude-desktop", "ok": False, "error": str(exc)}
    command, args = server_command(cli_path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = {"command": command, "args": args}
    config["mcpServers"] = servers
    backup = _write_json_config(path, config)
    return {
        "client": "claude-desktop",
        "ok": True,
        "path": str(path),
        "backup": str(backup) if backup else None,
        "note": "Quit and reopen Claude Desktop. The server then appears in Claude chats and in Cowork on this Mac.",
    }


def remove_claude_desktop(*, config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CLAUDE_DESKTOP_CONFIG
    try:
        config = _read_json_config(path)
    except ValueError as exc:
        return {"client": "claude-desktop", "ok": False, "error": str(exc)}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return {"client": "claude-desktop", "ok": True, "path": str(path), "note": "RemCTL was not configured."}
    del servers[SERVER_NAME]
    backup = _write_json_config(path, config)
    return {"client": "claude-desktop", "ok": True, "path": str(path), "backup": str(backup) if backup else None}


def _toml_has_server(text: str) -> bool:
    return re.search(r"^\s*\[mcp_servers\." + re.escape(SERVER_NAME) + r"\]", text, re.MULTILINE) is not None


def registration_status(cli_path: Path | None = None, *, claude_config: Path | None = None,
                        codex_config: Path | None = None, desktop_config: Path | None = None) -> list[dict[str, Any]]:
    """Read each client's configuration directly; no client process is spawned."""

    entries: list[dict[str, Any]] = []
    claude_path = claude_config or CLAUDE_CODE_CONFIG
    claude_entry: dict[str, Any] = {"client": "claude-code", "configured": False, "path": str(claude_path)}
    try:
        config = _read_json_config(claude_path)
        servers = config.get("mcpServers") if isinstance(config.get("mcpServers"), dict) else {}
        if SERVER_NAME in servers:
            claude_entry.update(configured=True, scope="user", server=servers[SERVER_NAME])
        else:
            for project, value in (config.get("projects") or {}).items():
                project_servers = value.get("mcpServers") if isinstance(value, dict) else None
                if isinstance(project_servers, dict) and SERVER_NAME in project_servers:
                    claude_entry.update(configured=True, scope="local", project=project, server=project_servers[SERVER_NAME])
                    break
    except ValueError as exc:
        claude_entry["error"] = str(exc)
    entries.append(claude_entry)

    codex_path = codex_config or CODEX_CONFIG
    codex_entry: dict[str, Any] = {"client": "codex", "configured": False, "path": str(codex_path)}
    if codex_path.exists():
        try:
            codex_entry["configured"] = _toml_has_server(codex_path.read_text(encoding="utf-8"))
        except OSError as exc:
            codex_entry["error"] = str(exc)
    entries.append(codex_entry)

    desktop_path = desktop_config or CLAUDE_DESKTOP_CONFIG
    desktop_entry: dict[str, Any] = {"client": "claude-desktop", "configured": False, "path": str(desktop_path)}
    try:
        config = _read_json_config(desktop_path)
        servers = config.get("mcpServers") if isinstance(config.get("mcpServers"), dict) else {}
        if SERVER_NAME in servers:
            desktop_entry.update(configured=True, server=servers[SERVER_NAME])
    except ValueError as exc:
        desktop_entry["error"] = str(exc)
    entries.append(desktop_entry)

    if cli_path is not None:
        command, args = server_command(cli_path)
        expected = {"command": command, "args": args}
        for entry in entries:
            server = entry.get("server")
            if isinstance(server, dict):
                entry["current"] = server.get("command") == expected["command"] and list(server.get("args") or []) == expected["args"]
    return entries


def config_snippets(cli_path: Path) -> dict[str, str]:
    command, args = server_command(cli_path)
    json_snippet = json.dumps({"mcpServers": {SERVER_NAME: {"command": command, "args": args}}}, indent=2)
    toml_snippet = (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = {json.dumps(command)}\n"
        f"args = {json.dumps(args)}\n"
    )
    shell = " ".join(_shell_quote(item) for item in [command, *args])
    return {
        "command": shell,
        "json": json_snippet,
        "toml": toml_snippet,
        "claude-code": f"claude mcp add --scope user --transport stdio {SERVER_NAME} -- {shell}",
        "codex": f"codex mcp add {SERVER_NAME} -- {shell}",
    }


def _shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


# ── MCPB bundle ──────────────────────────────────────────────────────────────

BUNDLE_LAUNCHER = '''#!/usr/bin/env python3
"""Launcher for the RemCTL MCP bundle: starts the installed RemCTL MCP server."""
import os, sys
COMMAND = {command!r}
ARGS = {args!r}
os.execv(COMMAND, [COMMAND, *ARGS, *sys.argv[1:]])
'''


def bundle_manifest(cli_path: Path, version: str, *, icon: bool) -> dict[str, Any]:
    command, args = server_command(cli_path)
    manifest: dict[str, Any] = {
        "manifest_version": "0.3",
        "name": SERVER_NAME,
        "display_name": SERVER_TITLE,
        "version": version,
        "description": "Apple Reminders for Claude through the signed RemCTL Capability Host on this Mac.",
        "long_description": (
            "RemCTL is a power-user Reminders CLI. This bundle connects Claude Desktop and Cowork to the RemCTL "
            "installed on this Mac: read, create, update, complete, flag, and delete reminders, with an interactive "
            "reminders widget. Permissions stay on the signed RemCTL Capability Host."
        ),
        "author": {"name": "Federico Viticci", "url": SERVER_WEBSITE},
        "homepage": SERVER_WEBSITE,
        "documentation": f"{SERVER_WEBSITE}/blob/main/docs/mcp.md",
        "license": "MIT",
        "keywords": ["reminders", "apple", "macos", "tasks"],
        "server": {
            "type": "python",
            "entry_point": "server/remctl-mcp.py",
            "mcp_config": {"command": command, "args": ["${__dirname}/server/remctl-mcp.py"], "env": {}},
        },
        "tools": [{"name": tool.name, "description": tool.description} for tool in TOOLS],
        "tools_generated": False,
        "prompts": [
            {"name": prompt["name"], "description": prompt["description"], "arguments": [arg["name"] for arg in prompt["arguments"]]}
            for prompt in PROMPTS
        ],
        "prompts_generated": False,
        "compatibility": {"platforms": ["darwin"]},
    }
    if icon:
        manifest["icon"] = "icon.png"
    return manifest


def build_bundle(cli_path: Path, version: str, output: Path, *, icon_path: Path | None = None) -> Path:
    """Write a .mcpb desktop-extension bundle that launches the installed server."""

    command, args = server_command(cli_path)
    icon_bytes = icon_path.read_bytes() if icon_path and icon_path.is_file() else None
    manifest = bundle_manifest(cli_path, version, icon=icon_bytes is not None)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        launcher = zipfile.ZipInfo("server/remctl-mcp.py")
        launcher.external_attr = 0o755 << 16
        launcher.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(launcher, BUNDLE_LAUNCHER.format(command=command, args=args))
        if icon_bytes is not None:
            archive.writestr("icon.png", icon_bytes)
    os.replace(temp, output)
    return output


def bundle_default_output() -> Path:
    return Path.home() / "Downloads" / "RemCTL.mcpb"


__all__ = [name for name in globals() if not name.startswith("_")]
