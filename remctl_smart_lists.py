"""Smart list filter decoding and safe encoding for RemCTL."""

from __future__ import annotations

import json
import plistlib
import xml.parsers.expat
from datetime import datetime


CUSTOM_SMART_LIST_TYPE = "com.apple.reminders.smartlist.custom"
SUPPORTED_PRIORITIES = {"low", "medium", "high"}
SUPPORTED_MATCH_OPERATIONS = {"all": "and", "any": "or", "and": "and", "or": "or"}
SUPPORTED_TIME_FILTERS = {"morning", "afternoon", "evening", "night", "no-time", "noTime"}
SUPPORTED_RELATIVE_DIRECTIONS = {
    "in-next": "inNext",
    "innext": "inNext",
    "inNext": "inNext",
    "next": "inNext",
    "in-past": "inPast",
    "inpast": "inPast",
    "inPast": "inPast",
    "past": "inPast",
}
SUPPORTED_RELATIVE_UNITS = {"minute", "hour", "day", "week", "month", "year"}
SUPPORTED_VEHICLES = {"connected", "disconnected"}
SUPPORTED_PROXIMITIES = {
    "enter": "enter",
    "arrive": "enter",
    "arriving": "enter",
    "leave": "leave",
    "leaving": "leave",
    "exit": "leave",
}


class SmartListFilterError(ValueError):
    """Raised when a requested smart list filter is not supported."""


def _uid_index(value):
    if isinstance(value, plistlib.UID):
        return value.data
    if isinstance(value, int):
        return value
    return None


def _loads_json_bytes(data: bytes):
    text = data.decode("utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Smart list filter JSON must be an object")
    return payload


def _extract_keyed_archive_json(data: bytes) -> bytes | None:
    archive = plistlib.loads(data)
    if not isinstance(archive, dict):
        return None
    objects = archive.get("$objects")
    top = archive.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict):
        return None
    root_index = _uid_index(top.get("root"))
    if root_index is None or root_index >= len(objects):
        return None
    root = objects[root_index]
    if not isinstance(root, dict):
        return None
    data_index = _uid_index(root.get("data"))
    if data_index is None or data_index >= len(objects):
        return None
    payload = objects[data_index]
    return payload if isinstance(payload, bytes) else None


def decode_smart_list_filter_blob(blob):
    """Decode known Reminders smart list filter blobs into JSON payloads.

    macOS 26 stores custom filters as raw UTF-8 JSON bytes. Older/research
    samples can wrap the same JSON in an NSKeyedArchiver object with a `data`
    field, so the decoder accepts both forms.
    """
    if blob in (None, b"", ""):
        return {
            "encoding": None,
            "payload": None,
            "summary": None,
            "error": None,
        }
    if isinstance(blob, str):
        raw = blob.encode("utf-8")
    else:
        raw = bytes(blob)

    stripped = raw.lstrip()
    try:
        if stripped.startswith(b"{"):
            payload = _loads_json_bytes(raw)
            return {
                "encoding": "json",
                "payload": payload,
                "summary": summarize_smart_list_filter(payload),
                "error": None,
            }
        archived = _extract_keyed_archive_json(raw)
        if archived is not None:
            payload = _loads_json_bytes(archived)
            return {
                "encoding": "keyed_archive_json",
                "payload": payload,
                "summary": summarize_smart_list_filter(payload),
                "error": None,
            }
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        plistlib.InvalidFileException,
        ValueError,
        xml.parsers.expat.ExpatError,
    ) as exc:
        return {
            "encoding": None,
            "payload": None,
            "summary": None,
            "error": str(exc),
        }

    return {
        "encoding": None,
        "payload": None,
        "summary": None,
        "error": "unsupported filter blob",
    }


def summarize_smart_list_filter(payload, *, strict=False):
    if payload is None:
        return None
    keys = sorted(str(key) for key in payload.keys())
    if payload == {}:
        return {
            "kind": "all",
            "description": "All reminders",
            "supported": True,
            "keys": [],
        }
    if payload == {"flagged": True}:
        return {
            "kind": "flagged",
            "description": "Flagged reminders",
            "supported": True,
            "keys": ["flagged"],
        }
    operation = payload.get("operation")
    payload_without_operation = {
        key: value for key, value in payload.items() if key != "operation"
    }
    priorities = payload_without_operation.get("priorities")
    if (
        set(payload_without_operation.keys()) == {"priorities"}
        and isinstance(priorities, list)
        and priorities
        and all(isinstance(item, str) and item in SUPPORTED_PRIORITIES for item in priorities)
    ):
        label = ", ".join(priorities)
        return {
            "kind": "priority",
            "description": f"Priority: {label}",
            "supported": True,
            "priorities": priorities,
            "keys": ["priorities"] + (["operation"] if operation else []),
        }
    parts = []
    unsupported = []
    for key, value in payload_without_operation.items():
        summary = _summarize_filter_family(key, value, strict=strict)
        if summary.get("supported"):
            parts.append(summary)
        else:
            unsupported.append(summary)
    if parts and not unsupported:
        match = {"and": "all", "or": "any"}.get(operation, "all")
        description = "; ".join(part["description"] for part in parts)
        if len(parts) > 1 or operation == "or":
            description = f"Match {match}: {description}"
        return {
            "kind": "compound" if len(parts) > 1 else parts[0]["kind"],
            "description": description,
            "supported": True,
            "match": match,
            "filters": parts,
            "keys": keys,
        }
    return {
        "kind": "unsupported",
        "description": "Unsupported custom filter",
        "supported": False,
        "materializes": False if any(part.get("materializes") is False for part in unsupported) else None,
        "filters": parts + unsupported,
        "keys": keys,
    }


def _summarize_filter_family(key, value, *, strict=False):
    if key == "flagged" and value is True:
        return {"kind": "flagged", "description": "Flagged reminders", "supported": True}
    if key == "priorities" and isinstance(value, list) and value:
        if all(isinstance(item, str) and item in SUPPORTED_PRIORITIES for item in value):
            return {
                "kind": "priority",
                "description": f"Priority: {', '.join(value)}",
                "supported": True,
                "priorities": value,
            }
    if key == "hashtags" and isinstance(value, dict):
        if value == {"any": ""}:
            return {"kind": "tags", "description": "Any tag", "supported": True}
        if value == {"untagged": ""}:
            return {"kind": "tags", "description": "Untagged only", "supported": True}
        hashtags = value.get("hashtags")
        if isinstance(hashtags, list) and all(isinstance(item, str) for item in hashtags):
            return {
                "kind": "tags",
                "description": f"Legacy selected tags: {', '.join(hashtags)}",
                "supported": False,
                "tags": hashtags,
                "tagMatch": "all",
                "materializes": False,
            }
        if isinstance(hashtags, dict):
            include = hashtags.get("include", [])
            exclude = hashtags.get("exclude", [])
            op = hashtags.get("operation")
            if (
                op in {"and", "or"}
                and isinstance(include, list)
                and isinstance(exclude, list)
                and all(isinstance(item, str) for item in include + exclude)
            ):
                match = "any" if op == "or" else "all"
                bits = []
                if include:
                    bits.append(f"include {', '.join(include)}")
                if exclude:
                    bits.append(f"exclude {', '.join(exclude)}")
                return {
                    "kind": "tags",
                    "description": f"Tags {match} selected: {', '.join(bits)}",
                    "supported": True,
                    "tags": include,
                    "excludeTags": exclude,
                    "tagMatch": match,
                }
    if key == "date" and isinstance(value, dict):
        return _summarize_date_filter(value, strict=strict)
    if key == "time" and isinstance(value, dict):
        for time_key, label in (
            ("morning", "Morning"),
            ("afternoon", "Afternoon"),
            ("evening", "Evening"),
            ("night", "Night"),
            ("noTime", "No time"),
        ):
            if value == {time_key: ""}:
                return {"kind": "time", "description": label, "supported": True, "time": time_key}
    if key == "location" and isinstance(value, dict):
        vehicle = value.get("vehicle")
        if vehicle in SUPPORTED_VEHICLES:
            return {
                "kind": "location",
                "description": "Getting in the car" if vehicle == "connected" else "Getting out of the car",
                "supported": True,
                "vehicle": vehicle,
            }
        location = value.get("location")
        if isinstance(location, dict):
            title = location.get("title") or "Specific location"
            proximity = location.get("proximity") or "enter"
            return {
                "kind": "location",
                "description": f"Location {proximity}: {title}",
                "supported": True,
                "location": location,
            }
    if key == "lists" and isinstance(value, dict):
        include = value.get("include", [])
        exclude = value.get("exclude", [])
        operation = value.get("operation")
        if (
            isinstance(include, list)
            and isinstance(exclude, list)
            and all(isinstance(item, str) for item in include + exclude)
            and (operation is None or operation in {"and", "or"})
        ):
            match = {"and": "all", "or": "any"}.get(operation, "all")
            bits = []
            if include:
                bits.append(f"include {len(include)} list(s)")
            if exclude:
                bits.append(f"exclude {len(exclude)} list(s)")
            return {
                "kind": "lists",
                "description": f"Lists {match}: " + ", ".join(bits),
                "supported": True,
                "include": include,
                "exclude": exclude,
                "listMatch": match,
            }
    return {
        "kind": str(key),
        "description": f"Unsupported {key} filter",
        "supported": False,
    }


def _is_valid_filter_date_string(value):
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            pass
    return False


def _summarize_date_filter(value, *, strict=False):
    if value == {"any": ""}:
        return {"kind": "date", "description": "Any date", "supported": True, "date": "any"}
    if value == {"noDate": ""}:
        return {"kind": "date", "description": "No date", "supported": True, "date": "noDate"}
    if "today" in value and isinstance(value.get("today"), bool):
        label = "Today and include past due" if value["today"] else "Today"
        return {"kind": "date", "description": label, "supported": True, "date": "today", "includePastDue": value["today"]}
    for key, label in (
        ("onDate", "On date"),
        ("beforeDate", "Before date"),
        ("afterDate", "After date"),
    ):
        if set(value.keys()) == {key} and isinstance(value[key], str):
            if strict and not _is_valid_filter_date_string(value[key]):
                return {"kind": "date", "description": "Unsupported date filter", "supported": False}
            return {"kind": "date", "description": f"{label}: {value[key]}", "supported": True, "date": key, "value": value[key]}
    if set(value.keys()) == {"dateRange"}:
        date_range = value["dateRange"]
        if isinstance(date_range, list) and len(date_range) == 2 and all(isinstance(item, str) for item in date_range):
            if strict and not all(_is_valid_filter_date_string(item) for item in date_range):
                return {"kind": "date", "description": "Unsupported date filter", "supported": False}
            return {"kind": "date", "description": f"Date range: {date_range[0]} to {date_range[1]}", "supported": True, "date": "dateRange", "range": date_range}
    relative = value.get("relativeRange")
    if isinstance(relative, dict):
        required = {"direction", "magnitude", "units"}
        if required.issubset(relative.keys()):
            include_past_due = bool(relative.get("includePastDue", False))
            suffix = ", include past due" if include_past_due else ""
            return {
                "kind": "date",
                "description": f"Relative date: {relative['direction']} {relative['magnitude']} {relative['units']}{suffix}",
                "supported": True,
                "date": "relativeRange",
                "relativeRange": relative,
            }
    return {"kind": "date", "description": "Unsupported date filter", "supported": False}


def normalize_match_operation(match):
    value = str(match or "all").strip()
    if value not in SUPPORTED_MATCH_OPERATIONS:
        raise SmartListFilterError("Smart list match must be all or any.")
    return SUPPORTED_MATCH_OPERATIONS[value]


def normalize_priorities(priorities):
    priorities = priorities or []
    normalized = []
    for priority in priorities:
        value = str(priority).strip().lower()
        if value not in SUPPORTED_PRIORITIES:
            raise SmartListFilterError(
                "Unsupported smart list priority. Use low, medium, or high."
            )
        if value not in normalized:
            normalized.append(value)
    return normalized


def normalize_filter_date(value):
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y")
    text = str(value).strip()
    if not text:
        raise SmartListFilterError("Smart list date cannot be empty.")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%d-%m-%Y")
        except ValueError:
            pass
    raise SmartListFilterError("Smart list dates must be YYYY-MM-DD or DD-MM-YYYY.")


def normalize_relative_range(value):
    if isinstance(value, dict):
        direction = value.get("direction")
        magnitude = value.get("magnitude")
        units = value.get("units")
        include_past_due = bool(value.get("includePastDue", False))
    else:
        parts = [part.strip() for part in str(value).split(":") if part.strip()]
        if len(parts) not in (3, 4):
            raise SmartListFilterError(
                "Relative date range must be direction:magnitude:unit[:past-due]."
            )
        direction, magnitude, units = parts[:3]
        include_past_due = len(parts) == 4 and parts[3] in {
            "past-due",
            "include-past-due",
            "includePastDue",
            "true",
        }
    direction = SUPPORTED_RELATIVE_DIRECTIONS.get(str(direction).strip())
    if not direction:
        raise SmartListFilterError("Relative date direction must be in-next or in-past.")
    magnitude = str(magnitude).strip()
    if not magnitude.isdigit() or int(magnitude) <= 0:
        raise SmartListFilterError("Relative date magnitude must be a positive integer.")
    units = str(units).strip().lower()
    if units.endswith("s"):
        units = units[:-1]
    if units not in SUPPORTED_RELATIVE_UNITS:
        raise SmartListFilterError(
            "Relative date unit must be minute, hour, day, week, month, or year."
        )
    payload = {"direction": direction, "magnitude": magnitude}
    if include_past_due:
        payload["includePastDue"] = True
    payload["units"] = units
    return payload


def build_supported_filter_payload(
    *,
    match="all",
    flagged=False,
    priorities=None,
    tags=None,
    tag_match="any",
    any_tag=False,
    untagged=False,
    date_any=False,
    date_today=False,
    date_today_include_past_due=False,
    date_no_date=False,
    date_on=None,
    date_before=None,
    date_after=None,
    date_range=None,
    date_relative=None,
    time_filter=None,
    include_list_ids=None,
    exclude_list_ids=None,
    list_match=None,
    vehicle=None,
    location=None,
):
    filters = []
    if flagged:
        filters.append(("flagged", True))

    normalized_priorities = normalize_priorities(priorities)
    if normalized_priorities:
        filters.append(("priorities", normalized_priorities))

    tag_payload = _build_tag_filter(
        tags=tags,
        tag_match=tag_match,
        any_tag=any_tag,
        untagged=untagged,
    )
    if tag_payload is not None:
        filters.append(("hashtags", tag_payload))

    date_payload = _build_date_filter(
        any_date=date_any,
        today=date_today,
        today_include_past_due=date_today_include_past_due,
        no_date=date_no_date,
        on=date_on,
        before=date_before,
        after=date_after,
        date_range=date_range,
        relative=date_relative,
    )
    if date_payload is not None:
        filters.append(("date", date_payload))

    time_payload = _build_time_filter(time_filter)
    if time_payload is not None:
        filters.append(("time", time_payload))

    list_payload = _build_lists_filter(include_list_ids, exclude_list_ids, list_match=list_match)
    if list_payload is not None:
        filters.append(("lists", list_payload))

    location_payload = _build_location_filter(vehicle=vehicle, location=location)
    if location_payload is not None:
        filters.append(("location", location_payload))

    if not filters:
        raise SmartListFilterError("Pass at least one smart list filter.")

    operation = normalize_match_operation(match)
    payload = {}
    if len(filters) > 1 or operation == "or":
        payload["operation"] = operation
    for key, value in filters:
        payload[key] = value
    return payload


def _build_tag_filter(*, tags=None, tag_match="any", any_tag=False, untagged=False):
    tags = [str(tag).lstrip("#").strip() for tag in (tags or []) if str(tag).lstrip("#").strip()]
    requested = sum(1 for item in (bool(tags), any_tag, untagged) if item)
    if requested > 1:
        raise SmartListFilterError("Pass only one tag filter: --tags, --any-tag, or --untagged.")
    if any_tag:
        return {"any": ""}
    if untagged:
        return {"untagged": ""}
    if tags:
        operation = normalize_match_operation(tag_match)
        return {"hashtags": {"operation": operation, "include": tags, "exclude": []}}
    return None


def _build_date_filter(
    *,
    any_date=False,
    today=False,
    today_include_past_due=False,
    no_date=False,
    on=None,
    before=None,
    after=None,
    date_range=None,
    relative=None,
):
    requested = sum(
        1
        for item in (
            any_date,
            today or today_include_past_due,
            no_date,
            on is not None,
            before is not None,
            after is not None,
            date_range is not None,
            relative is not None,
        )
        if item
    )
    if requested > 1:
        raise SmartListFilterError("Pass only one date filter.")
    if any_date:
        return {"any": ""}
    if today or today_include_past_due:
        return {"today": bool(today_include_past_due)}
    if no_date:
        return {"noDate": ""}
    if on is not None:
        return {"onDate": normalize_filter_date(on)}
    if before is not None:
        return {"beforeDate": normalize_filter_date(before)}
    if after is not None:
        return {"afterDate": normalize_filter_date(after)}
    if date_range is not None:
        if isinstance(date_range, str):
            parts = [part.strip() for part in date_range.replace("..", ",").split(",")]
        else:
            parts = list(date_range)
        if len(parts) != 2:
            raise SmartListFilterError("Date range must be START,END.")
        return {"dateRange": [normalize_filter_date(parts[0]), normalize_filter_date(parts[1])]}
    if relative is not None:
        return {"relativeRange": normalize_relative_range(relative)}
    return None


def _build_time_filter(value):
    if value is None:
        return None
    normalized = str(value).strip()
    if normalized not in SUPPORTED_TIME_FILTERS:
        raise SmartListFilterError("Time filter must be morning, afternoon, evening, night, or no-time.")
    if normalized == "no-time":
        normalized = "noTime"
    return {normalized: ""}


def _build_lists_filter(include_list_ids=None, exclude_list_ids=None, *, list_match=None):
    include = [str(item).strip() for item in (include_list_ids or []) if str(item).strip()]
    exclude = [str(item).strip() for item in (exclude_list_ids or []) if str(item).strip()]
    if include or exclude:
        payload = {"include": include, "exclude": exclude}
        if list_match is None:
            operation = "or" if len(include) > 1 and not exclude else "and"
        else:
            operation = normalize_match_operation(list_match)
        if operation == "or" or len(include) > 1 or exclude:
            payload["operation"] = operation
        return payload
    return None


def _build_location_filter(*, vehicle=None, location=None):
    if vehicle and location:
        raise SmartListFilterError("Pass either --vehicle or a specific location, not both.")
    if vehicle:
        value = str(vehicle).strip()
        if value not in SUPPORTED_VEHICLES:
            raise SmartListFilterError("Vehicle filter must be connected or disconnected.")
        return {"vehicle": value}
    if location:
        required = {"title", "latitude", "longitude"}
        if not required.issubset(location.keys()):
            raise SmartListFilterError("Specific location requires title, latitude, and longitude.")
        try:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
            radius = float(location.get("radius", 100))
        except (TypeError, ValueError) as exc:
            raise SmartListFilterError("Specific location latitude, longitude, and radius must be numbers.") from exc
        proximity = SUPPORTED_PROXIMITIES.get(str(location.get("proximity", "enter")).strip())
        if not proximity:
            raise SmartListFilterError("Specific location proximity must be enter or leave.")
        return {
            "location": {
                "title": str(location["title"]),
                "latitude": latitude,
                "radius": radius,
                "longitude": longitude,
                "proximity": proximity,
            }
        }
    return None


def encode_supported_filter_payload(payload) -> bytes:
    summary = summarize_smart_list_filter(payload, strict=True)
    if not summary or not summary.get("supported") or summary.get("kind") == "all":
        raise SmartListFilterError("Unsupported smart list filter shape.")
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
