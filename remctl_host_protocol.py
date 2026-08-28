"""Closed protocol contract for the RemCTL read-only Capability Host."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

PROTOCOL_VERSION = 1
SCHEMA_MANIFEST_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_REQUEST_ID_LENGTH = 128
MAX_TEXT_LENGTH = 4096
MAX_LIMIT = 10000
# Positive signed 64-bit ceiling — CoreData Z_PK values fit in [1, 2**63-1].
MAX_REMINDER_ID = (1 << 63) - 1
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_payload(self) -> dict[str, str]:
        return {"status": "error", "code": self.code, "message": self.message}


@dataclass(frozen=True)
class Field:
    types: tuple[type, ...]
    required: bool = False
    # String-only: maximum length of a str value.  Never applied to int values.
    maximum: int | None = None
    # Integer-only: inclusive upper and lower bounds for int values.
    # These are intentionally distinct from *maximum* so that dual-type fields
    # (e.g. IDENTIFIER accepts str|int) can bound each type independently.
    int_maximum: int | None = None
    int_minimum: int | None = None
    choices: tuple[Any, ...] | None = None

    def manifest(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "types": [item.__name__ for item in self.types],
            "required": self.required,
        }
        if self.maximum is not None:
            payload["strMaximum"] = self.maximum
        if self.int_maximum is not None:
            payload["intMaximum"] = self.int_maximum
        if self.int_minimum is not None:
            payload["intMinimum"] = self.int_minimum
        if self.choices is not None:
            payload["choices"] = list(self.choices)
        return payload


@dataclass(frozen=True)
class Operation:
    fields: dict[str, Field]
    exactly_one_of: tuple[str, ...] = ()

    def manifest(self) -> dict[str, Any]:
        payload = {
            "fields": {
                name: field.manifest()
                for name, field in sorted(self.fields.items())
            }
        }
        if self.exactly_one_of:
            payload["exactlyOneOf"] = list(self.exactly_one_of)
        return payload


TEXT = Field((str,), maximum=MAX_TEXT_LENGTH)
REQUIRED_TEXT = Field((str,), required=True, maximum=MAX_TEXT_LENGTH)
# IDENTIFIER accepts a string CKID (bounded by text length) or a positive
# signed 64-bit CoreData Z_PK integer.  The two type-specific bounds are kept
# separate so neither leaks onto the other.
IDENTIFIER = Field(
    (str, int),
    required=True,
    maximum=MAX_TEXT_LENGTH,
    int_minimum=1,
    int_maximum=MAX_REMINDER_ID,
)
# CoreData row-ID fields: positive signed 64-bit integers only.
OPTIONAL_ID = Field((int,), int_minimum=1, int_maximum=MAX_REMINDER_ID)
LIMIT = Field((int,), int_maximum=MAX_LIMIT)
BOOL = Field((bool,))


OPERATIONS: dict[str, Operation] = {
    "health": Operation({}),
    "resolve.reminder": Operation({"identifier": IDENTIFIER}),
    "resolve.list": Operation(
        {"name": TEXT, "id": OPTIONAL_ID, "allowGroups": BOOL},
        exactly_one_of=("name", "id"),
    ),
    "resolve.group": Operation(
        {"name": TEXT, "id": OPTIONAL_ID},
        exactly_one_of=("name", "id"),
    ),
    "resolve.section": Operation(
        {
            "listId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID),
            "name": TEXT,
            "cloudId": TEXT,
        },
        exactly_one_of=("name", "cloudId"),
    ),
    "resolve.sharee": Operation(
        {
            "listId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID),
            "identifier": IDENTIFIER,
        }
    ),
    "resolve.smartList": Operation({"identifier": IDENTIFIER}),
    "resolve.template": Operation({"identifier": IDENTIFIER}),
    "snapshot.reminder": Operation({"identifier": IDENTIFIER}),
    "snapshot.reminderFull": Operation({"identifier": IDENTIFIER}),
    "snapshot.reminders": Operation(
        {
            "listId": Field((int,), int_minimum=1, int_maximum=MAX_REMINDER_ID),
            "completed": BOOL,
            "flagged": BOOL,
            "urgent": BOOL,
            "query": Field((str,), maximum=512),
            "daysAhead": Field((int,), int_minimum=1, int_maximum=3650),
            "includeOverdue": BOOL,
            "overdue": BOOL,
            "topLevel": BOOL,
            "parentPk": Field((int,), int_minimum=1, int_maximum=MAX_REMINDER_ID),
            "manualOrder": BOOL,
            "limit": Field((int,), int_minimum=1, int_maximum=10000),
        }
    ),
    "snapshot.reminderDetail": Operation({"identifier": IDENTIFIER}),
    "snapshot.stats": Operation({}),
    "snapshot.tags": Operation({}),
    "snapshot.allSections": Operation({}),
    "snapshot.allListSectionCounts": Operation({}),
    "snapshot.sharees": Operation(
        {"listId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID)}
    ),
    "snapshot.reminderOrder": Operation(
        {"listId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID)}
    ),
    "snapshot.manualSortHint": Operation(
        {
            "listType": Field((int,), required=True, int_minimum=0, int_maximum=1000000),
            "listUUID": REQUIRED_TEXT,
        }
    ),
    "snapshot.subtasksForMove": Operation({"identifier": IDENTIFIER}),
    "snapshot.allLists": Operation({}),
    "snapshot.allSmartLists": Operation({}),
    "snapshot.allTemplates": Operation({}),
    "snapshot.list": Operation({"identifier": IDENTIFIER}),
    # snapshot.locationAlarm: check whether a location alarm with matching
    # metadata exists on a reminder.  Returns only booleans; no alarm content,
    # coordinates, or address text is returned.  Coordinates are scaled by 1e7
    # (pass int(lat * 1e7)) so the protocol stays integer-only.
    "snapshot.locationAlarm": Operation(
        {
            "identifier": Field((str,), required=True, maximum=MAX_TEXT_LENGTH),
            "title": TEXT,
            "latitudeE7": Field((int,), int_minimum=-900_000_000, int_maximum=900_000_000),
            "longitudeE7": Field((int,), int_minimum=-1_800_000_000, int_maximum=1_800_000_000),
        }
    ),
    "snapshot.sections": Operation({"listId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID)}),
    "snapshot.smartList": Operation({"identifier": IDENTIFIER}),
    "snapshot.groupReminders": Operation(
        {
            "groupId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID),
            "completed": BOOL,
            "topLevel": BOOL,
            "limit": Field((int,), int_minimum=1, int_maximum=10000),
        }
    ),
    "snapshot.smartListSectionsByPk": Operation(
        {"listId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID)}
    ),
    "snapshot.template": Operation({"identifier": IDENTIFIER}),
    "snapshot.templateWithItems": Operation({"identifier": IDENTIFIER}),
    # snapshot.listReminderCount: bounded count of active (or all) reminders in a
    # list, used for template-create sync polling.  Returns only a single non-negative
    # integer (or null); never SQL, list content, or reminder data.
    "snapshot.listReminderCount": Operation(
        {
            "listId": Field((int,), required=True, int_minimum=1, int_maximum=MAX_REMINDER_ID),
            "includeCompleted": Field((bool,)),
        }
    ),
    # probe.store: read-only store capability probe.  No input fields — the host
    # finds the best DB automatically.  Returns only bounded booleans and a closed
    # error-category enum; never paths, counts, content, or raw exception text.
    "probe.store": Operation({}),
    "poll.condition": Operation(
        {
            "condition": Field(
                (str,),
                required=True,
                choices=(
                    "reminder_exists",
                    "reminder_absent",
                    "section_membership",
                    "subtask_count",
                    "list_state",
                    "smart_list_state",
                    "template_state",
                ),
            ),
            "identifier": IDENTIFIER,
            "expected": Field((str, int, bool, type(None))),
            "attempts": Field((int,), int_maximum=100),
            "delayMilliseconds": Field((int,), int_maximum=5000),
        }
    ),
}

IMPLEMENTED_OPERATIONS = (
    "health",
    "poll.condition",
    "probe.store",
    "resolve.group",
    "resolve.list",
    "resolve.reminder",
    "resolve.section",
    "resolve.sharee",
    "resolve.smartList",
    "resolve.template",
    "snapshot.allLists",
    "snapshot.allSmartLists",
    "snapshot.allSections",
    "snapshot.allListSectionCounts",
    "snapshot.allTemplates",
    "snapshot.list",
    "snapshot.listReminderCount",
    "snapshot.locationAlarm",
    "snapshot.manualSortHint",
    "snapshot.reminder",
    "snapshot.reminderDetail",
    "snapshot.reminderFull",
    "snapshot.reminderOrder",
    "snapshot.reminders",
    "snapshot.sections",
    "snapshot.sharees",
    "snapshot.smartList",
    "snapshot.groupReminders",
    "snapshot.smartListSectionsByPk",
    "snapshot.stats",
    "snapshot.subtasksForMove",
    "snapshot.tags",
    "snapshot.template",
    "snapshot.templateWithItems",
)


def schema_manifest() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
        "implementedOperations": list(IMPLEMENTED_OPERATIONS),
        "operations": {
            name: operation.manifest()
            for name, operation in sorted(OPERATIONS.items())
        },
    }


def schema_manifest_digest() -> str:
    encoded = json.dumps(
        schema_manifest(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


SCHEMA_MANIFEST_DIGEST = schema_manifest_digest()


def _validate_field(name: str, value: Any, field: Field) -> None:
    if not isinstance(value, field.types):
        expected = ", ".join(item.__name__ for item in field.types)
        raise ProtocolError(
            "invalid_request",
            f"{name} must have type {expected}",
        )
    if isinstance(value, bool) and bool not in field.types:
        raise ProtocolError("invalid_request", f"{name} must not be boolean")
    # String bounds: maximum applies only to str length.
    if isinstance(value, str) and field.maximum is not None and len(value) > field.maximum:
        raise ProtocolError("request_too_large", f"{name} is too long")
    # Integer bounds: int_minimum/int_maximum apply only to non-bool integers.
    if isinstance(value, int) and not isinstance(value, bool):
        if field.int_minimum is not None and value < field.int_minimum:
            raise ProtocolError(
                "invalid_request",
                f"{name} must be at least {field.int_minimum}",
            )
        if field.int_maximum is not None and value > field.int_maximum:
            raise ProtocolError(
                "invalid_request",
                f"{name} exceeds maximum {field.int_maximum}",
            )
    if field.choices is not None and value not in field.choices:
        raise ProtocolError("invalid_request", f"{name} has an unsupported value")


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "request must be a JSON object")

    common = {
        "protocolVersion",
        "schemaManifestVersion",
        "schemaManifestDigest",
        "requestId",
        "operation",
    }
    missing_common = common.difference(payload)
    if missing_common:
        raise ProtocolError(
            "invalid_request",
            f"missing required fields: {', '.join(sorted(missing_common))}",
        )
    if payload["protocolVersion"] != PROTOCOL_VERSION:
        raise ProtocolError("protocol_mismatch", "unsupported protocol version")
    if payload["schemaManifestVersion"] != SCHEMA_MANIFEST_VERSION:
        raise ProtocolError(
            "schema_mismatch",
            "unsupported schema manifest version",
        )
    if payload["schemaManifestDigest"] != SCHEMA_MANIFEST_DIGEST:
        raise ProtocolError("schema_mismatch", "schema manifest digest mismatch")

    request_id = payload["requestId"]
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > MAX_REQUEST_ID_LENGTH
        or not REQUEST_ID_RE.fullmatch(request_id)
    ):
        raise ProtocolError("invalid_request", "requestId is invalid")

    operation_name = payload["operation"]
    if (
        not isinstance(operation_name, str)
        or operation_name not in OPERATIONS
        or operation_name not in IMPLEMENTED_OPERATIONS
    ):
        raise ProtocolError("unsupported_operation", "operation is not hosted")
    operation = OPERATIONS[operation_name]

    allowed = common.union(operation.fields)
    extras = set(payload).difference(allowed)
    if extras:
        raise ProtocolError(
            "invalid_request",
            f"unknown fields: {', '.join(sorted(extras))}",
        )
    missing = [
        name
        for name, field in operation.fields.items()
        if field.required and name not in payload
    ]
    if missing:
        raise ProtocolError(
            "invalid_request",
            f"missing required fields: {', '.join(sorted(missing))}",
        )
    for name, field in operation.fields.items():
        if name in payload:
            _validate_field(name, payload[name], field)
    if operation.exactly_one_of:
        present = [
            name for name in operation.exactly_one_of
            if payload.get(name) is not None
        ]
        if len(present) != 1:
            joined = ", ".join(operation.exactly_one_of)
            raise ProtocolError(
                "invalid_request",
                f"exactly one of {joined} is required",
            )
    return payload


def decode_request(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_REQUEST_BYTES:
        raise ProtocolError("request_too_large", "request exceeds byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid_encoding", "request must be UTF-8") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid_json", "request must be valid JSON") from exc
    return validate_request(payload)
