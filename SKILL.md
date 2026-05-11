---
name: remctl
description: Use when an agent needs to read, create, edit, complete, inspect, or troubleshoot Apple Reminders through the RemCTL CLI on macOS.
---

# RemCTL

RemCTL is a power-user Apple Reminders CLI. It reads the local Reminders CoreData database for fast, detailed output and writes normally through `remctl-bridge` using EventKit. Unsupported private metadata writes are available only when explicitly requested with `--private`; those go through `remctl-private` and Apple's private ReminderKit APIs. It is CLI-only: there is no local API server, token, launch agent, or service command.

## Default Workflow

- Use the installed command for user tasks: `remctl ...`.
- Use the repo command while developing RemCTL itself: `./remctl ...` from the repo root.
- Prefer JSON for automation and verification: `remctl today --json`, `remctl show Work --json`, `remctl info <id> --json`.
- Never write directly to the Reminders SQLite database.
- For private metadata, use regular `add` or `edit` with `--private`; do not use a separate command or raw database mutation.
- After changing repo code, reinstall before testing the user-facing command: `./install.sh && hash -r`.

## Common Commands

```bash
remctl today --json
remctl upcoming 7 --json
remctl overdue --json
remctl lists --json
remctl show Work --json
remctl search "query" --json
remctl info 23880 --json
remctl add "Review PR" -l Work -d "tomorrow 10:00" -p high --json
remctl edit 23880 -d clear --json
remctl done 23880 --json
```

## Private Metadata

Use `--private` only when Federico explicitly asks for private Reminders metadata or when a command needs synced web rich links, real tags, sections, subtasks, image attachments, real flags, urgent state, or location alarms.

```bash
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --section "Research" --json
remctl add "Prepare screenshots" -l Projects --private --image ~/Desktop/mockup.png --subtask "Export PNG" --json
remctl edit 23880 --private --url "https://example.com" -t remctl --json
remctl edit 23880 --private --section "Research" --subtask "Follow up" --json
remctl edit 23880 --private --flagged --urgent --json
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200 --json
```

Private metadata rules:

- `--private --url` creates a synced web rich link. Without `--private`, `--url` is appended to notes.
- `--private -t/--tags` creates real synced tags. On `add` without `--private`, tags are inline title hashtags. On `edit`, tags require `--private`.
- `--section`, `--new-section`, `--subtask`, `--image`, `--flagged`, `--urgent`, and location alarm fields require `--private` and should fail before writing if omitted.
- `add --private -f` writes the real private flag instead of the EventKit priority proxy.
- Generic file/PDF attachments are rejected because Reminders does not reliably show them.
- Verify private writes with `remctl info <numeric-id> --json`; if cross-device sync matters, ask Federico to check iPhone/iPad.

## Verification Rules

- Treat `remctl doctor --json` as the first setup check.
- Check `private_helper` in `remctl doctor --json` before using `--private`.
- For writes, verify against live Reminders data after the command succeeds.
- `remctl add` can return a UUID-like object ID; `remctl info` expects the numeric `#ID`. Resolve it with `remctl show <list> --json` by matching the created title before calling `remctl info`.
- Date output should match Reminders.app's displayed date. RemCTL reads `ZDISPLAYDATEDATE` first and falls back to `ZDUEDATE`.
- When debugging due-date mismatches, compare both fields in the Reminders database before assuming the CLI or UI is wrong.

## Permissions

First-run setup:

```bash
remctl onboard
remctl permissions full-disk-access
remctl doctor
```

RemCTL may need Reminders access for EventKit writes and private ReminderKit writes, Automation access for AppleScript fallback operations, and Full Disk Access for direct database reads. The guided permission helper only handles CLI targets; there is no service target. `remctl-private` does not have its own first-run flow; it depends on the same Reminders access and must be installed next to `remctl`.

## Development Checks

```bash
python3 -m py_compile remctl remctl_runtime.py remctl_serialization.py
swiftc -O -framework EventKit -framework Foundation -o /tmp/remctl-bridge-check remctl-bridge.swift
swiftc -O -framework AppKit -framework Foundation -o /tmp/remctl-permissions-check remctl-permissions.swift
clang -fobjc-arc -O -F/System/Library/PrivateFrameworks -framework Foundation -framework AppKit -framework ReminderKit -o /tmp/remctl-private-check remctl-private.m
./install.sh --bootstrap
remctl doctor --json
```
