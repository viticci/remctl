"""Shared reminder serialization helpers for RemCTL."""

from __future__ import annotations

import json


RECURRENCE_FREQUENCIES = {
    0: "daily",
    1: "weekly",
    2: "monthly",
    3: "yearly",
}

DUE_DATE_DELTA_UNITS = {
    0: ("minute", "minutes"),
    1: ("hour", "hours"),
    2: ("day", "days"),
    3: ("week", "weeks"),
    4: ("month", "months"),
}


def _row_has(row, key):
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return key in keys()
        except Exception:
            return False
    return isinstance(row, dict) and key in row


def _row_get(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    if _row_has(row, key):
        return row[key]
    return default


def _json_blob(value):
    if value in (None, ""):
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def recurrence_from_row(row, *, ts=None):
    """Extract a stable recurrence object from aliased ZREMCDOBJECT fields."""
    frequency_raw = _row_get(row, "recurrence_frequency")
    if frequency_raw is None:
        return None
    try:
        frequency = RECURRENCE_FREQUENCIES[int(frequency_raw)]
    except (KeyError, TypeError, ValueError):
        return None

    interval = _row_get(row, "recurrence_interval") or 1
    recurrence = {"frequency": frequency, "interval": int(interval)}

    days_of_week = _json_blob(_row_get(row, "recurrence_days_of_week")) or []
    if days_of_week:
        recurrence["daysOfWeekDetailed"] = days_of_week
        recurrence["daysOfWeek"] = [
            int(item["dayOfTheWeek"])
            for item in days_of_week
            if isinstance(item, dict) and item.get("dayOfTheWeek")
        ]

    for alias, output_key in (
        ("recurrence_days_of_month", "daysOfMonth"),
        ("recurrence_months_of_year", "monthsOfYear"),
        ("recurrence_days_of_year", "daysOfYear"),
        ("recurrence_weeks_of_year", "weeksOfYear"),
        ("recurrence_set_positions", "setPositions"),
    ):
        value = _json_blob(_row_get(row, alias))
        if value:
            recurrence[output_key] = value

    count = _row_get(row, "recurrence_count") or 0
    if count:
        recurrence["count"] = int(count)

    end_date = _row_get(row, "recurrence_end_date")
    if end_date and ts is not None:
        recurrence["endDate"] = ts(end_date).isoformat()

    return recurrence


def due_date_delta_alerts_from_row(row, *, ts=None):
    """Extract Early Reminder due-date delta alerts from a reminder row."""
    payload = _json_blob(_row_get(row, "ZDUEDATEDELTAALERTSDATA"))
    if not isinstance(payload, dict):
        return []
    alerts = payload.get("dueDateDeltaAlerts")
    if not isinstance(alerts, list):
        return []

    result = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        try:
            unit_raw = int(alert.get("dueDateDeltaUnit"))
            count = int(alert.get("dueDateDeltaCount"))
        except (TypeError, ValueError):
            continue
        singular, plural = DUE_DATE_DELTA_UNITS.get(unit_raw, ("unknown", "unknown"))
        value = abs(count)
        unit_name = singular if value == 1 else plural
        direction = "before" if count < 0 else "after"
        item = {
            "unit": unit_name,
            "unitCode": unit_raw,
            "count": count,
            "value": value,
            "direction": direction,
            "label": f"{value} {unit_name} {direction}",
        }
        identifier = alert.get("identifier")
        if identifier:
            item["identifier"] = identifier
        creation_date = alert.get("creationDate")
        if creation_date and ts is not None:
            try:
                item["creationDate"] = ts(float(creation_date)).isoformat()
            except (TypeError, ValueError):
                pass
        min_version = alert.get("minimumSupportedAppVersion")
        if min_version is not None:
            item["minimumSupportedAppVersion"] = min_version
        result.append(item)
    return result


def preload_extras(db, pks):
    """Batch-load subtask counts and hashtags to avoid N+1 queries."""
    if not pks:
        return {}, {}
    placeholders = ",".join("?" * len(pks))
    try:
        subtask_rows = db.execute(
            f"SELECT ZPARENTREMINDER, COUNT(*) FROM ZREMCDREMINDER "
            f"WHERE ZPARENTREMINDER IN ({placeholders}) AND ZMARKEDFORDELETION = 0 "
            f"AND ZCOMPLETED = 0 GROUP BY ZPARENTREMINDER",
            pks,
        ).fetchall()
        hashtag_rows = db.execute(
            f"SELECT o.ZREMINDER3, h.ZNAME FROM ZREMCDOBJECT o "
            f"JOIN ZREMCDHASHTAGLABEL h ON o.ZHASHTAGLABEL = h.Z_PK "
            f"WHERE o.ZREMINDER3 IN ({placeholders}) AND o.ZMARKEDFORDELETION = 0",
            pks,
        ).fetchall()
    except Exception:
        # Minimal test fixtures omit these tables; extras are best-effort.
        return {}, {}
    subtask_counts = {row[0]: row[1] for row in subtask_rows}
    hashtags = {}
    for row in hashtag_rows:
        hashtags.setdefault(row[0], []).append(row[1])
    return subtask_counts, hashtags


def _table_columns(db, table):
    try:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def preload_attachments(db, pks):
    """Batch-load attachment rows grouped by parent reminder pk.

    One query per backing table over the page of reminders; mirrors
    preload_extras. Returns {reminder_pk: [row, ...]} with sha/width/height
    included when the schema has those columns ("" placeholders otherwise).
    """
    if not pks or db is None:
        return {}
    placeholders = ",".join("?" * len(pks))
    saved_cols = _table_columns(db, "ZREMCDSAVEDATTACHMENT")
    object_cols = _table_columns(db, "ZREMCDOBJECT")
    saved_sha = "ZSHA512SUM" if "ZSHA512SUM" in saved_cols else "NULL AS ZSHA512SUM"
    object_sha = "ZSHA512SUM" if "ZSHA512SUM" in object_cols else "NULL AS ZSHA512SUM"
    grouped = {}
    try:
        saved_rows = db.execute(
            f"SELECT ZREMINDER, ZFILENAME, ZUTI, ZATTACHMENTTYPERAWVALUE, {saved_sha}, "
            "NULL AS ZWIDTH, NULL AS ZHEIGHT "
            f"FROM ZREMCDSAVEDATTACHMENT "
            f"WHERE ZREMINDER IN ({placeholders}) AND ZMARKEDFORDELETION = 0",
            pks,
        ).fetchall()
        object_rows = db.execute(
            f"SELECT ZREMINDER2, ZFILENAME, ZUTI, "
            "CASE WHEN ZWIDTH IS NOT NULL OR ZHEIGHT IS NOT NULL THEN 'image' ELSE 'file' END AS ZATTACHMENTTYPERAWVALUE, "
            f"{object_sha}, ZWIDTH, ZHEIGHT "
            f"FROM ZREMCDOBJECT "
            f"WHERE ZREMINDER2 IN ({placeholders}) AND ZFILENAME IS NOT NULL AND ZFILENAME != '' "
            "AND ZMARKEDFORDELETION = 0",
            pks,
        ).fetchall()
    except Exception:
        return {}
    for row in saved_rows + object_rows:
        grouped.setdefault(row[0], []).append(row)
    for pk in grouped:
        grouped[pk].sort(key=lambda row: row[1] or "")
    return grouped


def serialize_reminder(
    row,
    *,
    ts,
    priority_names,
    db=None,
    section=None,
    subtask_counts=None,
    hashtags=None,
    attachments=None,
    rich_link_resolver=None,
    fallback_subtask_count=None,
    fallback_hashtags=None,
    attachment_rows_to_json=None,
):
    """Convert a reminder row to a JSON-serializable dict."""
    subtask_counts = subtask_counts or {}
    hashtags = hashtags or {}
    attachments = attachments or {}

    subtask_count = subtask_counts.get(row["Z_PK"])
    if subtask_count is None:
        if db is not None and fallback_subtask_count is not None:
            subtask_count = fallback_subtask_count(db, row["Z_PK"])
        else:
            subtask_count = 0

    tags = hashtags.get(row["Z_PK"])
    if tags is None:
        if db is not None and fallback_hashtags is not None:
            tags = fallback_hashtags(db, row["Z_PK"])
        else:
            tags = []

    reminder = {
        "id": row["Z_PK"],
        "title": row["ZTITLE"],
        "list": row["list_name"],
        "completed": bool(row["ZCOMPLETED"]),
        "flagged": bool(row["ZFLAGGED"]),
        "urgent": bool(_row_get(row, "ZISURGENTSTATEENABLEDFORCURRENTUSER", False)),
        "priority": priority_names.get(row["ZPRIORITY"] or 0, "none"),
        "subtaskCount": subtask_count,
        "isSubtask": bool(row["ZPARENTREMINDER"]),
    }
    if section:
        reminder["section"] = section
    if row["ZNOTES"]:
        reminder["notes"] = row["ZNOTES"]

    url = row["ZICSURL"]
    if not url and db is not None and rich_link_resolver is not None:
        url = rich_link_resolver(db, row["Z_PK"])
    if url:
        reminder["url"] = url

    if row["ZDUEDATE"]:
        reminder["dueDate"] = ts(row["ZDUEDATE"]).isoformat()
    display_date = _row_get(row, "ZDISPLAYDATEDATE")
    if display_date and display_date != row["ZDUEDATE"]:
        reminder["displayDate"] = ts(display_date).isoformat()
    if _row_get(row, "ZALLDAY") is not None:
        reminder["allDay"] = bool(_row_get(row, "ZALLDAY"))
    if row["ZCREATIONDATE"]:
        reminder["createdDate"] = ts(row["ZCREATIONDATE"]).isoformat()
    if row["ZCOMPLETIONDATE"]:
        reminder["completionDate"] = ts(row["ZCOMPLETIONDATE"]).isoformat()
    if row["ZPARENTREMINDER"]:
        reminder["parentID"] = row["ZPARENTREMINDER"]
    if tags:
        reminder["tags"] = tags
    if attachments and attachment_rows_to_json is not None:
        attachment_payload = attachment_rows_to_json(attachments.get(row["Z_PK"], []))
        if attachment_payload:
            reminder["attachments"] = attachment_payload
    recurrence = recurrence_from_row(row, ts=ts)
    if recurrence:
        reminder["recurrence"] = recurrence
    early_reminders = due_date_delta_alerts_from_row(row, ts=ts)
    if early_reminders:
        reminder["earlyReminder"] = early_reminders[0]
        reminder["earlyReminders"] = early_reminders
    if row["ZCKIDENTIFIER"]:
        reminder["deepLink"] = f"x-apple-reminderkit://REMCDReminder/{row['ZCKIDENTIFIER']}"
    return reminder


def serialize_reminders(
    rows,
    *,
    ts,
    priority_names,
    db=None,
    memberships=None,
    rich_link_resolver=None,
    fallback_subtask_count=None,
    fallback_hashtags=None,
    attachment_rows_to_json=None,
):
    """Convert reminder rows with shared preloaded metadata."""
    pks = [row["Z_PK"] for row in rows]
    subtask_counts, hashtags = preload_extras(db, pks) if db else ({}, {})
    attachments = preload_attachments(db, pks) if db and attachment_rows_to_json is not None else {}
    memberships = memberships or {}
    return [
        serialize_reminder(
            row,
            ts=ts,
            priority_names=priority_names,
            db=db,
            section=memberships.get(row["ZCKIDENTIFIER"]),
            subtask_counts=subtask_counts,
            hashtags=hashtags,
            attachments=attachments,
            rich_link_resolver=rich_link_resolver,
            fallback_subtask_count=fallback_subtask_count,
            fallback_hashtags=fallback_hashtags,
            attachment_rows_to_json=attachment_rows_to_json,
        )
        for row in rows
    ]
