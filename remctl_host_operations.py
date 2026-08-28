"""Typed read operations exposed by the RemCTL Capability Host."""

from __future__ import annotations

import contextlib
import io
import time
from typing import Any


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    """Convert a sqlite3.Row (or None) to a plain JSON-serialisable dict."""
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _broker_call(func, *args, **kwargs):
    """Call *func*, converting a ``sys.exit()`` inside it into an error tuple.

    Several existing RemCTL query helpers (``resolve_sharee_or_die`` and
    friends) print a user-facing message to stderr and call ``sys.exit(1)``
    on failure. That is fine for the direct CLI process, but it must never
    be allowed to kill the long-lived Capability Host broker process. This
    helper redirects stderr into a buffer and converts ``SystemExit`` into a
    ``(None, message)`` result instead of propagating it.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            result = func(*args, **kwargs)
        return result, None
    except SystemExit:
        return None, buf.getvalue().strip()

# Closed set of fields returned by resolve.reminder.  Only identity and
# list-membership fields needed for cmd_add post-create readback are exposed.
_REMINDER_IDENTITY_FIELDS = (
    "Z_PK",
    "ZCKIDENTIFIER",
    "ZTITLE",
    "ZLIST",
    "ZCOMPLETED",
    "ZMARKEDFORDELETION",
)


def _reminder_identity_payload(row: Any) -> dict[str, Any] | None:
    """Convert a DB row to a closed, JSON-serialisable reminder identity dict.

    ``Z_PK`` is included as a compatibility alias alongside the canonical
    ``id`` field so that existing ``cmd_add`` readback code that accesses
    ``created_row["Z_PK"]`` works identically for both the direct and hosted
    routes without further changes.
    """
    if row is None:
        return None
    pk = row["Z_PK"]
    return {
        "id": pk,
        "Z_PK": pk,
        "identifier": row["ZCKIDENTIFIER"],
        "title": row["ZTITLE"],
        "listId": row["ZLIST"],
        "completed": bool(row["ZCOMPLETED"]),
        "deleted": bool(row["ZMARKEDFORDELETION"]),
    }


class ReadOperations:
    """Adapts RemCTL's existing query functions to closed host operations."""

    def __init__(self, remctl_module: Any):
        self.remctl = remctl_module

    def resolve_list(self, request: dict[str, Any]) -> Any:
        db = self.remctl.open_db()
        try:
            return self.remctl.resolve_list_ref(
                db,
                name=request.get("name"),
                list_id=request.get("id"),
                allow_groups=request.get("allowGroups", False),
            )
        finally:
            db.close()

    def resolve_reminder(self, request: dict[str, Any]) -> dict[str, Any] | None:
        db = self.remctl.open_db()
        try:
            row = self.remctl.q_reminder_by_identifier(db, request["identifier"])
            return _reminder_identity_payload(row)
        finally:
            db.close()

    def resolve_section(self, request: dict[str, Any]) -> dict[str, Any]:
        """Resolve a section by name or cloudId within a list.

        Reimplements the matching/ambiguity logic of ``resolve_section_ckid``
        without ever calling ``sys.exit`` — errors are returned as closed
        dicts with an ``error`` code and a ready-to-print ``message`` so the
        CLI-side caller can reproduce the exact direct-path error text.
        """
        db = self.remctl.open_db()
        try:
            list_pk = request["listId"]
            name = request.get("name")
            cloud_id = request.get("cloudId")
            sections = self.remctl.q_sections(db, list_pk)

            if cloud_id:
                matches = [
                    section
                    for section in sections
                    if (section["ZCKIDENTIFIER"] or "").lower() == cloud_id.lower()
                ]
                if not matches:
                    return {
                        "error": "not_found",
                        "message": f"Error: section ID not found in target list: {cloud_id}",
                    }
                section = matches[0]
                return {
                    "cloudId": section["ZCKIDENTIFIER"],
                    "name": section["ZDISPLAYNAME"],
                    "id": section["Z_PK"],
                }

            if name:
                matches = [
                    section
                    for section in sections
                    if (section["ZDISPLAYNAME"] or "").lower() == name.lower()
                ]
                if not matches:
                    return {
                        "error": "not_found",
                        "message": f"Error: section not found in target list: {name}",
                    }
                if len(matches) > 1:
                    counts = self.remctl.q_section_member_counts(db, list_pk)
                    non_empty = [
                        section
                        for section in matches
                        if counts.get(section["ZCKIDENTIFIER"], 0) > 0
                    ]
                    if len(non_empty) == 1:
                        matches = non_empty
                    else:
                        options = ", ".join(
                            f"{section['ZCKIDENTIFIER']} "
                            f"({counts.get(section['ZCKIDENTIFIER'], 0)} "
                            f"reminder{'s' if counts.get(section['ZCKIDENTIFIER'], 0) != 1 else ''})"
                            for section in matches
                        )
                        return {
                            "error": "ambiguous",
                            "message": (
                                f"Error: multiple sections named {name!r} in target list. "
                                f"Use --section-id with one of: {options}"
                            ),
                        }
                section = matches[0]
                if not section["ZCKIDENTIFIER"]:
                    return {
                        "error": "not_found",
                        "message": f"Error: section has no stable CloudKit identifier: {name}",
                    }
                return {
                    "cloudId": section["ZCKIDENTIFIER"],
                    "name": section["ZDISPLAYNAME"],
                    "id": section["Z_PK"],
                }

            return {
                "error": "no_identifier",
                "message": "Error: section lookup requires a name or cloudId.",
            }
        finally:
            db.close()

    def resolve_sharee(self, request: dict[str, Any]) -> dict[str, Any]:
        """Resolve a sharee by identifier (name/email/phone/ID, or "me").

        Delegates to ``resolve_sharee_or_die`` via ``_broker_call`` so the
        broker process survives a not-found/ambiguous lookup; the captured
        stderr text becomes the returned error message.
        """
        db = self.remctl.open_db()
        try:
            list_pk = request["listId"]
            identifier = request["identifier"]
            row, error_message = _broker_call(
                self.remctl.resolve_sharee_or_die, db, list_pk, identifier
            )
            if error_message is not None:
                return {"error": "failed", "message": error_message}
            return {
                "id": row["Z_PK"],
                "ZCKIDENTIFIER": row["ZCKIDENTIFIER"],
                "cloudId": row["ZCKIDENTIFIER"],
                "name": self.remctl._sharee_display_name(row),
            }
        finally:
            db.close()

    def snapshot_sections(self, request: dict[str, Any]) -> dict[str, Any]:
        db = self.remctl.open_db()
        try:
            list_pk = request["listId"]
            sections = self.remctl.q_sections(db, list_pk)
            memberships = self.remctl.q_section_memberships(db, list_pk)
            return {
                "sections": [
                    {
                        "id": section["Z_PK"],
                        "name": section["ZDISPLAYNAME"],
                        "cloudId": section["ZCKIDENTIFIER"],
                    }
                    for section in sections
                ],
                "memberships": memberships,
            }
        finally:
            db.close()

    def snapshot_reminder(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return hashtags and earlyReminderIdentifiers for a reminder.

        Accepts either a CKID string or a numeric Z_PK integer via the
        ``identifier`` field.  Returns ``found: False`` with empty lists when
        no matching reminder exists so callers can distinguish not-found from
        an empty (but found) result.
        """
        identifier = request["identifier"]
        db = self.remctl.open_db()
        try:
            if isinstance(identifier, int):
                row = db.execute(
                    "SELECT Z_PK, ZCKIDENTIFIER FROM ZREMCDREMINDER "
                    "WHERE Z_PK = ? AND ZMARKEDFORDELETION = 0",
                    (identifier,),
                ).fetchone()
                if row is None:
                    return {"found": False, "hashtags": [], "earlyReminderIdentifiers": []}
                pk = row["Z_PK"]
                ckid = row["ZCKIDENTIFIER"] if row["ZCKIDENTIFIER"] else None
            else:
                row = self.remctl.q_reminder_by_identifier(db, identifier)
                if row is None:
                    return {"found": False, "hashtags": [], "earlyReminderIdentifiers": []}
                pk = row["Z_PK"]
                ckid = identifier

            hashtags = [r["ZNAME"] for r in self.remctl.q_hashtags(db, pk)]
            early_ids = (
                self.remctl.early_reminder_identifiers_for_reminder(db, ckid)
                if ckid
                else []
            )
            return {
                "found": True,
                "hashtags": hashtags,
                "earlyReminderIdentifiers": early_ids,
            }
        finally:
            db.close()

    def _compute_alarm_flags(self, db: Any, pk: int, due_ts: Any, display_ts: Any):
        """Return ``(matchesDue, matchesDueOrDisplay)`` for a reminder's alarms.

        Mirrors ``should_carry_absolute_alarm_to_new_due`` /
        ``should_clear_matching_absolute_alarm``: only a single absolute alarm
        that matches the reminder's due (or display) second counts.
        """
        m = self.remctl
        try:
            alarms = m.alarm_rows_to_json(m.q_alarms(db, pk))
        except Exception:
            return False, False
        absolute = [alarm for alarm in alarms if alarm.get("type") == "absolute"]
        if len(alarms) != 1 or len(absolute) != 1:
            return False, False
        alarm_dt = m._parse_alarm_iso_datetime(absolute[0].get("date"))
        old_due = m.ts(due_ts) if due_ts else None
        old_display = m.ts(display_ts) if display_ts else None
        matches_due = (
            m._same_datetime_second(alarm_dt, old_due)
            if (alarm_dt and old_due)
            else False
        )
        matches_any = matches_due or (
            m._same_datetime_second(alarm_dt, old_display)
            if (alarm_dt and old_display)
            else False
        )
        return matches_due, matches_any

    def snapshot_reminder_full(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the full reminder row needed for mutation preflight.

        Accepts an int ``Z_PK`` or a str CloudKit identifier.  Returns
        ``{"found": False}`` when the reminder does not exist, otherwise a
        closed dict carrying both canonical and ``Z*`` alias keys plus the two
        computed absolute-alarm carry/clear flags.
        """
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            if isinstance(identifier, int):
                row = m.q_reminder(db, identifier)
            else:
                row = m.q_reminder_by_identifier(db, identifier)
            return m.reminder_full_snapshot(db, row)
        finally:
            db.close()

    def snapshot_reminder_order(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the persisted reminder CKID ordering for a list."""
        m = self.remctl
        db = m.open_db()
        try:
            return {"order": m.q_list_reminder_order(db, request["listId"])}
        finally:
            db.close()

    def snapshot_smart_list_sections_by_pk(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return sections for a custom smart list, keyed by Z_PK.

        Returns only bounded id/name/cloudId fields per section; never arbitrary SQL.
        """
        m = self.remctl
        db = m.open_db()
        try:
            rows = m.q_smart_list_sections(db, request["listId"])
            sections = [
                {
                    "id": row["Z_PK"],
                    "name": row["ZDISPLAYNAME"],
                    "cloudId": row["ZCKIDENTIFIER"],
                }
                for row in rows
            ]
            return {"sections": sections, "count": len(sections)}
        finally:
            db.close()

    def snapshot_manual_sort_hint(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the manual-sort hint for a smart list, or ``{"found": False}``."""
        m = self.remctl
        db = m.open_db()
        try:
            hint = m.q_manual_sort_hint(db, request["listType"], request["listUUID"])
            if hint is None:
                return {"found": False}
            return hint
        finally:
            db.close()

    def snapshot_subtasks_for_move(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the subtask identity rows for a parent reminder move."""
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            if isinstance(identifier, int):
                parent = m.q_reminder(db, identifier)
            else:
                parent = m.q_reminder_by_identifier(db, identifier)
            if parent is None:
                return {"subtasks": [], "count": 0}
            subtasks = m.subtask_move_identities(db, parent)
            return {"subtasks": subtasks, "count": len(subtasks)}
        finally:
            db.close()

    def snapshot_all_lists(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return every list (including groups) as closed payload dicts."""
        m = self.remctl
        db = m.open_db()
        try:
            return {"lists": m.all_lists_snapshot(db)}
        finally:
            db.close()

    def snapshot_all_smart_lists(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return every smart list as a closed payload dict."""
        m = self.remctl
        db = m.open_db()
        try:
            return {
                "smartLists": [m.smart_list_to_dict(row) for row in m.q_smart_lists(db)]
            }
        finally:
            db.close()

    def snapshot_all_templates(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return every template as a closed payload dict."""
        m = self.remctl
        db = m.open_db()
        try:
            return {"templates": [m.template_to_dict(row) for row in m.q_templates(db)]}
        finally:
            db.close()

    def resolve_group(self, request: dict[str, Any]) -> Any:
        """Resolve a Reminders list *group* by name or id."""
        m = self.remctl
        db = m.open_db()
        try:
            return m.resolve_group_ref(
                db, name=request.get("name"), group_id=request.get("id")
            )
        finally:
            db.close()

    def resolve_smart_list(self, request: dict[str, Any]) -> Any:
        """Resolve a smart list by identifier (str name or int id)."""
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            if isinstance(identifier, int):
                return m.resolve_smart_list_ref(db, smart_list_id=identifier)
            return m.resolve_smart_list_ref(db, name=identifier)
        finally:
            db.close()

    def resolve_template(self, request: dict[str, Any]) -> Any:
        """Resolve a template by identifier (str name or int id)."""
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            if isinstance(identifier, int):
                return m.resolve_template_ref(db, template_id=identifier)
            return m.resolve_template_ref(db, name=identifier)
        finally:
            db.close()

    def snapshot_list(self, request: dict[str, Any]) -> Any:
        """Return the full list snapshot for a name/CKID/Z_PK identifier."""
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            row = None
            if isinstance(identifier, int):
                row = m.q_list_by_pk(db, identifier)
            else:
                ref = m.resolve_list_ref(db, name=identifier, allow_groups=True)
                if ref and not ref.get("error"):
                    row = m.q_list_by_pk(db, ref["id"])
                if row is None:
                    columns = m.list_select_columns(db)
                    row = db.execute(
                        f"SELECT {', '.join(columns)} FROM ZREMCDBASELIST "
                        "WHERE lower(ZCKIDENTIFIER) = lower(?) AND ZMARKEDFORDELETION = 0 "
                        "AND Z_ENT = 3 AND ZNAME IS NOT NULL AND ZNAME != '' LIMIT 1",
                        (identifier,),
                    ).fetchone()
            if row is None:
                return None
            return m.list_to_dict(row)
        finally:
            db.close()

    def snapshot_smart_list(self, request: dict[str, Any]) -> Any:
        """Return the smart-list snapshot for an id/CKID/name identifier."""
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            rows = m.q_smart_lists(db)
            match = None
            if isinstance(identifier, int):
                match = next((row for row in rows if row["Z_PK"] == identifier), None)
            else:
                folded = identifier.casefold()
                match = next(
                    (row for row in rows if (row["ZCKIDENTIFIER"] or "").casefold() == folded),
                    None,
                )
                if match is None:
                    match = next(
                        (
                            row
                            for row in rows
                            if m.smart_list_display_name(row).casefold() == folded
                        ),
                        None,
                    )
            if match is None:
                return None
            return m.smart_list_to_dict(match)
        finally:
            db.close()

    def snapshot_group_reminders(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return reminders across all child lists in a Reminders group."""
        m = self.remctl
        db = m.open_db()
        try:
            return m.group_reminders_snapshot(
                db,
                request["groupId"],
                completed=request.get("completed"),
                top_level=True if request.get("topLevel") is None else request.get("topLevel"),
                limit=request.get("limit"),
            )
        finally:
            db.close()

    def snapshot_template(self, request: dict[str, Any]) -> Any:
        """Return the template snapshot (with sections/items) for an identifier."""
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            if isinstance(identifier, int):
                rows = m.q_template_matches(db, template_id=identifier)
            else:
                rows = m.q_template_matches(db, name=identifier)
                if not rows:
                    folded = identifier.casefold()
                    rows = [
                        row
                        for row in m.q_templates(db)
                        if (row["ZCKIDENTIFIER"] or "").casefold() == folded
                    ]
            if not rows:
                return None
            return m.template_to_dict(rows[0], db, include_items=True)
        finally:
            db.close()

    def snapshot_template_with_items(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the full template payload with items and sections.

        Accepts either a string name or int Z_PK identifier.
        Returns ``{"found": False}`` when not found, otherwise full template_to_dict shape.
        Response is bounded: item/section strings capped by existing remctl limits.
        """
        m = self.remctl
        identifier = request["identifier"]
        db = m.open_db()
        try:
            if isinstance(identifier, int):
                matches = m.q_template_matches(db, template_id=identifier)
            else:
                matches = m.q_template_matches(db, name=identifier)
            if not matches:
                return {"found": False}
            result = m.template_to_dict(matches[0], db, include_items=True)
            result["found"] = True
            return result
        finally:
            db.close()

    def snapshot_location_alarm(self, request: dict[str, Any]) -> dict[str, Any]:
        """Check whether a location alarm matching given criteria exists on a reminder.

        Returns only ``{"found": bool, "matches": bool}``; never alarm content,
        addresses, or coordinates.
        """
        m = self.remctl
        reminder_ckid = request["identifier"]
        title = request.get("title")
        lat_e7 = request.get("latitudeE7")
        lon_e7 = request.get("longitudeE7")
        lat = lat_e7 / 1e7 if lat_e7 is not None else None
        lon = lon_e7 / 1e7 if lon_e7 is not None else None
        db = m.open_db()
        try:
            row = m.q_reminder_by_identifier(db, reminder_ckid)
            if not row:
                return {"found": False, "matches": False}
            alarms = m.alarm_rows_to_json(m.q_alarms(db, row["Z_PK"]))
            for alarm in alarms:
                if alarm.get("type") != "location":
                    continue
                location = alarm.get("location", {})
                if title and location.get("title") != title:
                    continue
                if lat is not None and abs(float(location.get("latitude", 0)) - lat) > 0.0001:
                    continue
                if lon is not None and abs(float(location.get("longitude", 0)) - lon) > 0.0001:
                    continue
                return {"found": True, "matches": True}
            return {"found": True, "matches": False}
        except Exception:
            return {"found": False, "matches": False}
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _resolve_list_state(self, db: Any, identifier: Any) -> bool:
        m = self.remctl
        if isinstance(identifier, int):
            ref = m.resolve_list_ref(db, list_id=identifier, allow_groups=True)
        else:
            ref = m.resolve_list_ref(db, name=identifier, allow_groups=True)
        return bool(ref and not ref.get("error"))

    def _resolve_smart_list_state(self, db: Any, identifier: Any) -> bool:
        m = self.remctl
        if isinstance(identifier, int):
            ref = m.resolve_smart_list_ref(db, smart_list_id=identifier)
        else:
            ref = m.resolve_smart_list_ref(db, name=identifier)
        return bool(ref and not ref.get("error"))

    def _resolve_template_state(self, db: Any, identifier: Any) -> bool:
        m = self.remctl
        if isinstance(identifier, int):
            ref = m.resolve_template_ref(db, template_id=identifier)
        else:
            ref = m.resolve_template_ref(db, name=identifier)
        return bool(ref and not ref.get("error"))

    def _evaluate_condition(self, db: Any, condition: str, identifier: Any, expected: Any):
        """Evaluate a single poll condition against a fresh DB snapshot.

        Returns ``(met: bool, value)`` where *value* is the observed state
        for diagnostics (bounded scalar; never reminder content beyond a
        section name/count already exposed by other operations).
        """
        m = self.remctl
        if condition == "reminder_exists":
            exists = m.q_reminder_by_identifier(db, identifier) is not None
            return exists, exists
        if condition == "reminder_absent":
            absent = m.q_reminder_by_identifier(db, identifier) is None
            return absent, absent
        if condition == "section_membership":
            row = m.q_reminder_by_identifier(db, identifier)
            if row is None:
                return False, None
            memberships = m.q_section_memberships(db, row["ZLIST"])
            current = memberships.get(row["ZCKIDENTIFIER"])
            return (current == expected), current
        if condition == "subtask_count":
            row = m.q_reminder_by_identifier(db, identifier)
            if row is None:
                return False, None
            count = m.q_subtask_count(db, row["Z_PK"])
            return (count == expected), count
        if condition == "list_state":
            exists = self._resolve_list_state(db, identifier)
            want = expected if expected in ("exists", "absent") else "exists"
            current = "exists" if exists else "absent"
            return (current == want), current
        if condition == "smart_list_state":
            exists = self._resolve_smart_list_state(db, identifier)
            want = expected if expected in ("exists", "absent") else "exists"
            current = "exists" if exists else "absent"
            return (current == want), current
        if condition == "template_state":
            exists = self._resolve_template_state(db, identifier)
            want = expected if expected in ("exists", "absent") else "exists"
            current = "exists" if exists else "absent"
            return (current == want), current
        return False, None

    def poll_condition(self, request: dict[str, Any]) -> dict[str, Any]:
        """Bounded polling of a read-only condition.

        Loops up to ``attempts`` times (default 12), re-reading a fresh DB
        snapshot each pass with ``delayMilliseconds`` (default 250) between
        attempts.  Returns ``{"met", "attempts", "value"}``.
        """
        m = self.remctl
        condition = request["condition"]
        identifier = request.get("identifier")
        expected = request.get("expected")
        attempts = request.get("attempts", 12)
        delay_seconds = request.get("delayMilliseconds", 250) / 1000.0
        met = False
        value = None
        used = 0
        for index in range(attempts):
            used = index + 1
            db = m.open_db()
            try:
                met, value = self._evaluate_condition(
                    db, condition, identifier, expected
                )
            finally:
                db.close()
            if met:
                break
            if index < attempts - 1:
                time.sleep(delay_seconds)
        return {"met": met, "attempts": used, "value": value}

    def probe_store(self, request: dict[str, Any]) -> dict[str, Any]:
        """Read-only Reminders store capability probe.

        Phase 1: checks whether the store directory is accessible (FDA gate).
        Phase 2: opens the best DB read-only and validates the expected schema.

        Returns only bounded booleans and a closed ``errorCategory`` enum value;
        never paths, SQL, reminder/list content, raw exception text, or counts.
        This operation is safe to call from any restricted context.
        """
        # Phase 1 – directory accessibility (FDA).
        try:
            access_err = self.remctl.reminders_store_access_error()
        except Exception:
            return {"storeReadable": False, "schemaOk": False, "errorCategory": "unknown"}

        if access_err is not None:
            return {"storeReadable": False, "schemaOk": False, "errorCategory": "access_denied"}

        # Phase 2 – DB open + schema validation.
        db = None
        try:
            db = self.remctl.open_db()
            return {"storeReadable": True, "schemaOk": True, "errorCategory": None}
        except Exception as exc:
            msg = str(exc).lower()
            if "table" in msg or "schema" in msg:
                # DB opened but expected table is absent → schema mismatch.
                return {
                    "storeReadable": True,
                    "schemaOk": False,
                    "errorCategory": "schema_mismatch",
                }
            if "no reminders database" in msg or "icloud reminders" in msg:
                return {
                    "storeReadable": True,
                    "schemaOk": False,
                    "errorCategory": "not_found",
                }
            if "not readable" in msg or "full disk access" in msg:
                # open_db() itself can raise this if access_error was not caught above.
                return {
                    "storeReadable": False,
                    "schemaOk": False,
                    "errorCategory": "access_denied",
                }
            return {"storeReadable": False, "schemaOk": False, "errorCategory": "io_error"}
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def snapshot_list_reminder_count(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the count of reminders in a list for template-create sync polling.

        Returns only ``{"count": <int>}`` (non-negative) or ``{"count": null}``
        on DB error.  Never returns list/reminder content.
        """
        list_pk = request["listId"]
        include_completed = bool(request.get("includeCompleted", False))
        try:
            db = self.remctl.open_db()
            count = self.remctl.q_list_reminder_count_for_template(
                db, list_pk, include_completed=include_completed
            )
            return {"count": count}
        except Exception:
            return {"count": None}

    def snapshot_reminders(self, request: dict[str, Any]) -> dict[str, Any]:
        """Universal filtered reminder query used by the read commands."""
        m = self.remctl
        db = m.open_db()
        try:
            return m.reminder_query_snapshot(
                db,
                list_pk=request.get("listId"),
                completed=request.get("completed"),
                flagged=bool(request.get("flagged")),
                urgent=bool(request.get("urgent")),
                query=request.get("query"),
                days_ahead=request.get("daysAhead"),
                include_overdue=bool(request.get("includeOverdue", True)),
                overdue=bool(request.get("overdue")),
                top_level=request.get("topLevel"),
                parent_pk=request.get("parentPk"),
                manual_order=request.get("manualOrder"),
                limit=request.get("limit"),
            )
        finally:
            db.close()

    def snapshot_reminder_detail(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return full reminder detail for cmd_info, or ``{"found": False}``."""
        m = self.remctl
        db = m.open_db()
        try:
            return m.reminder_detail_snapshot(db, request["identifier"])
        finally:
            db.close()

    def snapshot_stats(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return global reminder stats (same queries as cmd_stats)."""
        m = self.remctl
        db = m.open_db()
        try:
            due_column = m.due_filter_expr(db)
            sod = m.start_of_day()
            total = db.execute(
                "SELECT COUNT(*) FROM ZREMCDREMINDER WHERE ZMARKEDFORDELETION = 0 "
                "AND ZACCOUNT IS NOT NULL"
            ).fetchone()[0]
            active = db.execute(
                "SELECT COUNT(*) FROM ZREMCDREMINDER WHERE ZMARKEDFORDELETION = 0 "
                "AND ZCOMPLETED = 0 AND ZACCOUNT IS NOT NULL"
            ).fetchone()[0]
            flagged = db.execute(
                "SELECT COUNT(*) FROM ZREMCDREMINDER WHERE ZMARKEDFORDELETION = 0 "
                "AND ZCOMPLETED = 0 AND ZFLAGGED = 1 AND ZACCOUNT IS NOT NULL"
            ).fetchone()[0]
            urgent = db.execute(
                f"SELECT COUNT(*) FROM ZREMCDREMINDER r WHERE r.ZMARKEDFORDELETION = 0 "
                f"AND r.ZCOMPLETED = 0 AND r.ZACCOUNT IS NOT NULL AND {m.urgent_where_clause(db)}"
            ).fetchone()[0]
            overdue = db.execute(
                f"SELECT COUNT(*) FROM ZREMCDREMINDER r LEFT JOIN ZREMCDBASELIST l "
                f"ON r.ZLIST = l.Z_PK WHERE r.ZMARKEDFORDELETION = 0 AND r.ZCOMPLETED = 0 "
                f"AND r.ZACCOUNT IS NOT NULL AND l.Z_PK IS NOT NULL AND {due_column} IS NOT NULL "
                f"AND {due_column} < {m.to_ts(sod)}"
            ).fetchone()[0]
            return {
                "total": total,
                "active": active,
                "completed": total - active,
                "overdue": overdue,
                "flagged": flagged,
                "urgent": urgent,
                "lists": len(m.q_lists(db)),
                "sections": len(m.q_sections(db)),
            }
        finally:
            db.close()

    def snapshot_tags(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return all hashtag labels."""
        m = self.remctl
        db = m.open_db()
        try:
            rows = db.execute(
                "SELECT ZNAME FROM ZREMCDHASHTAGLABEL WHERE ZNAME IS NOT NULL ORDER BY ZNAME"
            ).fetchall()
            return {"tags": [{"name": r["ZNAME"]} for r in rows]}
        finally:
            db.close()

    def snapshot_all_sections(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return all sections across all lists."""
        m = self.remctl
        db = m.open_db()
        try:
            rows = m.q_sections(db)
            sections = [
                {
                    "id": r["Z_PK"],
                    "name": r["ZDISPLAYNAME"],
                    "listId": r["ZLIST"],
                    "listName": r["list_name"] if "list_name" in r.keys() else None,
                }
                for r in rows
            ]
            return {"sections": sections, "count": len(sections)}
        finally:
            db.close()

    def snapshot_all_list_section_counts(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return section counts for every list keyed by string list id."""
        m = self.remctl
        db = m.open_db()
        try:
            return m.all_list_section_counts_snapshot(db)
        finally:
            db.close()

    def snapshot_sharees(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return all sharees for a list plus the current-user sharee CKID."""
        m = self.remctl
        list_pk = request["listId"]
        db = m.open_db()
        try:
            current_user_ckid = m.q_list_shared_owner_ckid(db, list_pk)
            rows = m.q_sharees(db, list_pk)
            return {
                "sharees": [
                    m.sharee_to_dict(row, current_user_ckid=current_user_ckid)
                    for row in rows
                ],
                "currentUserSharee": current_user_ckid,
            }
        finally:
            db.close()

    def handlers(self) -> dict[str, Any]:
        return {
            "poll.condition": self.poll_condition,
            "probe.store": self.probe_store,
            "resolve.group": self.resolve_group,
            "resolve.list": self.resolve_list,
            "resolve.reminder": self.resolve_reminder,
            "resolve.section": self.resolve_section,
            "resolve.sharee": self.resolve_sharee,
            "resolve.smartList": self.resolve_smart_list,
            "resolve.template": self.resolve_template,
            "snapshot.allLists": self.snapshot_all_lists,
            "snapshot.allSections": self.snapshot_all_sections,
            "snapshot.allListSectionCounts": self.snapshot_all_list_section_counts,
            "snapshot.allSmartLists": self.snapshot_all_smart_lists,
            "snapshot.allTemplates": self.snapshot_all_templates,
            "snapshot.listReminderCount": self.snapshot_list_reminder_count,
            "snapshot.list": self.snapshot_list,
            "snapshot.locationAlarm": self.snapshot_location_alarm,
            "snapshot.manualSortHint": self.snapshot_manual_sort_hint,
            "snapshot.reminder": self.snapshot_reminder,
            "snapshot.reminderDetail": self.snapshot_reminder_detail,
            "snapshot.reminderFull": self.snapshot_reminder_full,
            "snapshot.reminderOrder": self.snapshot_reminder_order,
            "snapshot.reminders": self.snapshot_reminders,
            "snapshot.sections": self.snapshot_sections,
            "snapshot.sharees": self.snapshot_sharees,
            "snapshot.smartList": self.snapshot_smart_list,
            "snapshot.groupReminders": self.snapshot_group_reminders,
            "snapshot.smartListSectionsByPk": self.snapshot_smart_list_sections_by_pk,
            "snapshot.stats": self.snapshot_stats,
            "snapshot.subtasksForMove": self.snapshot_subtasks_for_move,
            "snapshot.tags": self.snapshot_tags,
            "snapshot.template": self.snapshot_template,
            "snapshot.templateWithItems": self.snapshot_template_with_items,
        }
