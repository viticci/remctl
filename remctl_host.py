"""Client transport for the RemCTL read-only Capability Host."""

from __future__ import annotations

import json
import socket
import struct
import uuid
from pathlib import Path
from typing import Any

from remctl_host_protocol import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_MANIFEST_DIGEST,
    SCHEMA_MANIFEST_VERSION,
    ProtocolError,
    validate_request,
)

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class HostUnavailable(RuntimeError):
    pass


def encode_frame(
    payload: dict[str, Any],
    *,
    maximum: int = MAX_REQUEST_BYTES,
) -> bytes:
    body = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(body) > maximum:
        raise ProtocolError("request_too_large", "request exceeds byte limit")
    return struct.pack(">I", len(body)) + body


def build_request(operation: str, **fields: Any) -> dict[str, Any]:
    payload = {
        "protocolVersion": PROTOCOL_VERSION,
        "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
        "requestId": uuid.uuid4().hex,
        "operation": operation,
        **fields,
    }
    return validate_request(payload)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise HostUnavailable("Capability Host closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode_response(sock: socket.socket) -> dict[str, Any]:
    header = _recv_exact(sock, 4)
    size = struct.unpack(">I", header)[0]
    if size > MAX_RESPONSE_BYTES:
        raise HostUnavailable("Capability Host response exceeds byte limit")
    body = _recv_exact(sock, size)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostUnavailable("Capability Host returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise HostUnavailable("Capability Host response must be an object")
    if (
        payload.get("protocolVersion") != PROTOCOL_VERSION
        or payload.get("schemaManifestVersion") != SCHEMA_MANIFEST_VERSION
        or payload.get("schemaManifestDigest") != SCHEMA_MANIFEST_DIGEST
    ):
        raise HostUnavailable("Capability Host protocol or schema mismatch")
    return payload


def call_host(
    socket_path: Path,
    request: dict[str, Any],
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
) -> dict[str, Any]:
    validated = validate_request(request)
    frame = encode_frame(validated)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(connect_timeout)
        client.connect(str(socket_path))
        client.settimeout(read_timeout)
        client.sendall(frame)
        response = _decode_response(client)
    except (OSError, TimeoutError) as exc:
        raise HostUnavailable(f"Capability Host is unavailable: {exc}") from exc
    finally:
        client.close()
    if response.get("status") == "error" and response.get("requestId") is None:
        # Pre-decode server errors (server_busy, request_timeout) are sent
        # before the request is processed, so they carry requestId: null.
        # Raise HostUnavailable with the actual code rather than a misleading
        # "requestId mismatch" message.
        code = str(response.get("code") or "host_error")
        message = str(response.get("message") or "Capability Host request failed")
        raise HostUnavailable(f"Capability Host returned {code}: {message}")
    if response.get("requestId") != validated["requestId"]:
        raise HostUnavailable("Capability Host response requestId mismatch")
    if response.get("status") == "error":
        raise ProtocolError(
            str(response.get("code") or "host_error"),
            str(response.get("message") or "Capability Host request failed"),
        )
    if response.get("status") != "ok" or "result" not in response:
        raise HostUnavailable("Capability Host response is incomplete")
    return response


def health(socket_path: Path, **timeout_options: Any) -> dict[str, Any]:
    return call_host(
        socket_path,
        build_request("health"),
        **timeout_options,
    )["result"]


def resolve_list(
    socket_path: Path,
    *,
    name: str | None = None,
    list_id: int | None = None,
    allow_groups: bool = False,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Ask the Capability Host to resolve a Reminders list.

    Exactly one of *name* or *list_id* must be provided; mirrors the semantics
    of the monolithic CLI's ``resolve_list_ref`` function.  Returns the host
    result dict, or ``None`` if the list is not found.
    """
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if list_id is not None:
        fields["id"] = list_id
    if allow_groups:
        fields["allowGroups"] = True
    response = call_host(
        socket_path,
        build_request("resolve.list", **fields),
        **timeout_options,
    )
    return response["result"]


def resolve_reminder(
    socket_path: Path,
    identifier: str,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Ask the Capability Host to resolve a reminder by its CloudKit identifier.

    Returns a closed identity dict with *id* (numeric Z_PK), *identifier*
    (CKID string), *title*, *listId*, *completed*, and *deleted* — or
    ``None`` when the reminder is not found.
    """
    response = call_host(
        socket_path,
        build_request("resolve.reminder", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def resolve_section(
    socket_path: Path,
    list_pk: int,
    *,
    name: str | None = None,
    cloud_id: str | None = None,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Ask the Capability Host to resolve a section by name or cloudId.

    Exactly one of *name* or *cloud_id* must be provided. Returns a closed
    dict with either ``cloudId``/``name``/``id`` on success, or ``error``/
    ``message`` on failure (not found, ambiguous, or no identifier given).
    """
    fields: dict[str, Any] = {"listId": list_pk}
    if name is not None:
        fields["name"] = name
    if cloud_id is not None:
        fields["cloudId"] = cloud_id
    response = call_host(
        socket_path,
        build_request("resolve.section", **fields),
        **timeout_options,
    )
    return response["result"]


def resolve_sharee(
    socket_path: Path,
    list_pk: int,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Ask the Capability Host to resolve a sharee within a list.

    *identifier* may be a name, email, phone number, numeric sharee ID, or
    the special values ``"me"``/``"myself"``. Returns a closed dict with
    ``id``, ``ZCKIDENTIFIER``, ``cloudId``, and ``name`` on success, or
    ``error``/``message`` on failure.
    """
    response = call_host(
        socket_path,
        build_request("resolve.sharee", listId=list_pk, identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_sections(
    socket_path: Path,
    list_pk: int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Ask the Capability Host for all sections in a list plus membership.

    Returns a closed dict with ``sections`` (list of ``id``/``name``/
    ``cloudId`` dicts) and ``memberships`` (reminder CKID -> section name).
    """
    response = call_host(
        socket_path,
        build_request("snapshot.sections", listId=list_pk),
        **timeout_options,
    )
    return response["result"]


def snapshot_reminder(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Ask the Capability Host for hashtags and early-reminder identifiers.

    *identifier* may be a CKID string or a numeric Z_PK integer.  Returns a
    closed dict with ``found`` (bool), ``hashtags`` (list[str]), and
    ``earlyReminderIdentifiers`` (list[str]).  ``found: False`` means the
    reminder does not exist; ``found: True`` means the lists reflect the
    actual DB state (possibly empty for a reminder with no tags/alerts).
    """
    response = call_host(
        socket_path,
        build_request("snapshot.reminder", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def probe_store(
    socket_path: Path,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Run a non-mutating store capability probe inside the Capability Host.

    Returns a closed dict with:
      ``storeReadable`` (bool) — True when the Reminders store directory is
          accessible (Full Disk Access granted to the host).
      ``schemaOk`` (bool) — True when the expected table schema was found.
      ``errorCategory`` (str | None) — bounded enum on failure:
          ``"access_denied"`` | ``"not_found"`` | ``"schema_mismatch"`` |
          ``"io_error"`` | ``"unknown"``; None when both booleans are True.

    Never contains paths, reminder/list content, SQL, raw exceptions, or counts.
    Raises :class:`HostUnavailable` only on transport/protocol errors, not on
    store access failures (those are reported via the result dict).
    """
    response = call_host(
        socket_path,
        build_request("probe.store"),
        **timeout_options,
    )
    return response["result"]


def _identifier_field(identifier: str | int) -> dict[str, Any]:
    return {"identifier": identifier}


def resolve_group(
    socket_path: Path,
    *,
    name: str | None = None,
    group_id: int | None = None,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Resolve a Reminders list *group* by name or id via the Capability Host."""
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if group_id is not None:
        fields["id"] = group_id
    response = call_host(
        socket_path,
        build_request("resolve.group", **fields),
        **timeout_options,
    )
    return response["result"]


def resolve_smart_list(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Resolve a smart list by name (str) or id (int) via the Capability Host."""
    response = call_host(
        socket_path,
        build_request("resolve.smartList", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def resolve_template(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Resolve a template by name (str) or id (int) via the Capability Host."""
    response = call_host(
        socket_path,
        build_request("resolve.template", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_reminder_full(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return the full reminder preflight snapshot (found flag + fields)."""
    response = call_host(
        socket_path,
        build_request("snapshot.reminderFull", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_reminder_order(
    socket_path: Path,
    list_pk: int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return the persisted reminder CKID ordering for a list."""
    response = call_host(
        socket_path,
        build_request("snapshot.reminderOrder", listId=list_pk),
        **timeout_options,
    )
    return response["result"]


def snapshot_manual_sort_hint(
    socket_path: Path,
    list_type: int,
    list_uuid: str,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return the manual-sort hint dict, or ``{"found": False}`` if absent."""
    response = call_host(
        socket_path,
        build_request("snapshot.manualSortHint", listType=list_type, listUUID=list_uuid),
        **timeout_options,
    )
    return response["result"]


def snapshot_subtasks_for_move(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return subtask identity rows for a parent reminder move."""
    response = call_host(
        socket_path,
        build_request("snapshot.subtasksForMove", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_all_lists(
    socket_path: Path,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return every list (including groups) as closed payload dicts."""
    response = call_host(
        socket_path,
        build_request("snapshot.allLists"),
        **timeout_options,
    )
    return response["result"]


def snapshot_all_smart_lists(
    socket_path: Path,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return every smart list as closed payload dicts."""
    response = call_host(
        socket_path,
        build_request("snapshot.allSmartLists"),
        **timeout_options,
    )
    return response["result"]


def snapshot_all_templates(
    socket_path: Path,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return every template as closed payload dicts."""
    response = call_host(
        socket_path,
        build_request("snapshot.allTemplates"),
        **timeout_options,
    )
    return response["result"]


def snapshot_list(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Return the full list snapshot for a name/CKID/Z_PK identifier."""
    response = call_host(
        socket_path,
        build_request("snapshot.list", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_smart_list_sections_by_pk(
    socket_path: Path,
    list_id: int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return ``{"sections": [...], "count": int}`` for a custom smart list."""
    response = call_host(
        socket_path,
        build_request("snapshot.smartListSectionsByPk", listId=list_id),
        **timeout_options,
    )
    return response["result"]


def snapshot_location_alarm(
    socket_path: Path,
    reminder_ckid: str,
    *,
    title: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return ``{"found": bool, "matches": bool}`` — no alarm content returned."""
    fields: dict[str, Any] = {"identifier": reminder_ckid}
    if title is not None:
        fields["title"] = title
    if latitude is not None:
        fields["latitudeE7"] = int(round(latitude * 1e7))
    if longitude is not None:
        fields["longitudeE7"] = int(round(longitude * 1e7))
    response = call_host(
        socket_path,
        build_request("snapshot.locationAlarm", **fields),
        **timeout_options,
    )
    return response["result"]


def snapshot_smart_list(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Return the smart-list snapshot for an id/CKID/name identifier."""
    response = call_host(
        socket_path,
        build_request("snapshot.smartList", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_group_reminders(
    socket_path: Path,
    group_id: int,
    *,
    completed: bool | None = None,
    top_level: bool | None = None,
    limit: int | None = None,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return child-list reminders for a Reminders group."""
    fields: dict[str, Any] = {"groupId": group_id}
    if completed is not None:
        fields["completed"] = completed
    if top_level is not None:
        fields["topLevel"] = top_level
    if limit is not None:
        fields["limit"] = limit
    response = call_host(
        socket_path,
        build_request("snapshot.groupReminders", **fields),
        **timeout_options,
    )
    return response["result"]


def snapshot_template(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any] | None:
    """Return the template snapshot (with sections/items) for an identifier."""
    response = call_host(
        socket_path,
        build_request("snapshot.template", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_template_with_items(
    socket_path: Path,
    identifier: str | int,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return the full template payload with items/sections, or ``{"found": False}``."""
    response = call_host(
        socket_path,
        build_request("snapshot.templateWithItems", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_list_reminder_count(
    socket_path: Path,
    list_id: int,
    include_completed: bool = False,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Return ``{"count": int|None}`` — active (or all) reminder count in *list_id*.

    Used by template-create sync polling; never returns reminder/list content.
    """
    response = call_host(
        socket_path,
        build_request(
            "snapshot.listReminderCount",
            listId=list_id,
            includeCompleted=include_completed,
        ),
        **timeout_options,
    )
    return response["result"]


def snapshot_reminders(socket_path, **fields):
    """Universal filtered reminder query. Returns reminders/count/truncated."""
    response = call_host(socket_path, build_request("snapshot.reminders", **fields))
    return response["result"]


def snapshot_reminder_detail(socket_path, identifier, **timeout_options):
    """Return full reminder detail for cmd_info, or ``{"found": False}``."""
    response = call_host(
        socket_path,
        build_request("snapshot.reminderDetail", identifier=identifier),
        **timeout_options,
    )
    return response["result"]


def snapshot_stats(socket_path, **timeout_options):
    """Return global reminder stats."""
    response = call_host(
        socket_path, build_request("snapshot.stats"), **timeout_options
    )
    return response["result"]


def snapshot_tags(socket_path, **timeout_options):
    """Return all hashtag labels."""
    response = call_host(
        socket_path, build_request("snapshot.tags"), **timeout_options
    )
    return response["result"]


def snapshot_all_sections(socket_path, **timeout_options):
    """Return all sections across all lists."""
    response = call_host(
        socket_path, build_request("snapshot.allSections"), **timeout_options
    )
    return response["result"]


def snapshot_all_list_section_counts(socket_path, **timeout_options):
    """Return ``{"sectionCounts": {"<listId>": count, ...}}``."""
    response = call_host(
        socket_path,
        build_request("snapshot.allListSectionCounts"),
        **timeout_options,
    )
    return response["result"]


def snapshot_sharees(socket_path, list_id, **timeout_options):
    """Return all sharees for a list plus the current-user sharee CKID."""
    response = call_host(
        socket_path,
        build_request("snapshot.sharees", listId=list_id),
        **timeout_options,
    )
    return response["result"]


def poll_condition(
    socket_path: Path,
    condition: str,
    identifier: str | int,
    *,
    expected: Any = None,
    attempts: int | None = None,
    delay_milliseconds: int | None = None,
    **timeout_options: Any,
) -> dict[str, Any]:
    """Run a bounded read-only poll on the Capability Host."""
    fields: dict[str, Any] = {"condition": condition, "identifier": identifier}
    if expected is not None:
        fields["expected"] = expected
    _attempts = attempts if attempts is not None else 24
    _delay_ms = delay_milliseconds if delay_milliseconds is not None else 250
    if attempts is not None:
        fields["attempts"] = attempts
    if delay_milliseconds is not None:
        fields["delayMilliseconds"] = delay_milliseconds
    if timeout_options.get("read_timeout") is None:
        _server_wall_s = (_attempts * _delay_ms) / 1000.0
        timeout_options = dict(timeout_options)
        timeout_options["read_timeout"] = max(_server_wall_s + 10.0, DEFAULT_READ_TIMEOUT)
    response = call_host(
        socket_path,
        build_request("poll.condition", **fields),
        **timeout_options,
    )
    return response["result"]
