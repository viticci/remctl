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
- For private reminder metadata, use regular `add` or `edit` with `--private`; for private list appearance, use `list-create --private` or `list-edit --private`. Do not use raw database mutation.
- After changing repo code, reinstall before testing the user-facing command: `./install.sh && hash -r`.

## Common Commands

```bash
remctl today --json
remctl upcoming 7 --json
remctl overdue --json
remctl lists --json
remctl show Work --json
remctl search "query" --json
remctl search "query" --completed --json
remctl info 23880 --json
remctl add "Review PR" -l Work -d "tomorrow 10:00" -p high --json
remctl list-symbols --json
remctl edit 23880 -d clear --json
remctl done 23880 --json
remctl list-symbols --preview
```

## Private Metadata

Use `--private` only when the user explicitly asks for private Reminders metadata or when a command needs synced web rich links, real tags, sections, subtasks, image attachments, real flags, urgent state, location alarms, or list appearance metadata.

```bash
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --section "Research" --json
remctl add "Research" -l Projects --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8 --json
remctl add "Prepare screenshots" -l Projects --private --image ~/Desktop/mockup.png --subtask "Export PNG" --json
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}' --json
remctl edit 23880 --private --url "https://example.com" -t remctl --json
remctl edit 23880 --private --section "Research" --subtask "Follow up" --json
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8 --json
remctl edit 23880 --private --subtask '{"title":"Follow up","notes":"Bring latest numbers","due":"next friday at 3pm","url":"https://example.com","tags":["work"]}' --json
remctl edit 23880 --private --flagged --urgent --json
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200 --json
remctl list-create "Research" --color orange --private --symbol education3 --json
remctl list-create "Cold Ideas" --color cyan --private --emoji 🥶 --json
remctl list-edit Projects --private --color '#FF8D28' --symbol education3 --json
remctl list-edit --list-id 144 --private --emoji 📌 --json
```

Private metadata rules:

- `--private --url` creates a synced web rich link. Without `--private`, `--url` is appended to notes.
- `--private -t/--tags` creates real synced tags. On `add` without `--private`, tags are inline title hashtags. On `edit`, tags require `--private`.
- `--section` resolves by name; if duplicates exist in the same list, RemCTL uses the single non-empty match when possible. Use `--section-id` for exact assignment.
- `--subtask` accepts either a plain child title or a JSON object with child metadata: `title`, `notes`, `due`, `priority`, `alarm`, `recurrence`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location fields.
- `--section`, `--new-section`, `--subtask`, `--image`, `--flagged`, `--urgent`, and location alarm fields require `--private` and should fail before writing if omitted.
- `add --private -f` writes the real private flag instead of the EventKit priority proxy.
- `list-symbols` prints the 71 official Reminders emblem names; its terminal glyph column is only an approximation. Use `list-symbols --preview` to open a native-asset HTML contact sheet with interactive official color swatches, or `list-symbols --html PATH` to write one. `list-create --color NAME` uses public EventKit for normal colors. `list-create --private` and `list-edit --private` can write exact `#RRGGBB` colors, official list symbols, and emoji badges. Reminders' picker icons use private emblem names such as `education3`; `--symbol` only accepts official names because arbitrary SF Symbol strings render as the default icon in Reminders. Use `--emoji` for custom standard emoji badges.
- List names resolve exact first, then case-insensitive, then normalized names such as `Weekly 513` for `🗓️ Weekly 513`. If multiple lists match, RemCTL fails before writing; use `--list-id`.
- Generic file/PDF attachments are rejected because Reminders does not reliably show them.
- Verify private writes with `remctl info <numeric-id> --json`; if cross-device sync matters, ask the user to check iPhone/iPad.

## Verification Rules

- Treat `remctl doctor --json` as the first setup check.
- For agents, prefer `remctl doctor --for-agent --json`; `doctor` must pass in the same execution context that will run the write.
- Check `private_helper` in `remctl doctor --json` before using `--private`.
- Do not run `doctor` before every ordinary task once the current context is known-good; it is a setup/TCC diagnostic, not a per-write verification step.
- For writes, verify against live Reminders data after the command succeeds.
- `remctl search QUERY --completed --json` includes completed reminders and searches both titles and notes.
- `remctl add --json` returns `numericId` when direct DB reads can resolve the new reminder. Use that for `remctl info <numericId> --json`. If `numericId` is absent, resolve the UUID-like `id` with `remctl show <list> --json` by matching the created title.
- Prefer deterministic due-date strings. If the user says "today at 3pm", either pass `today at 3pm` or normalize it to `YYYY-MM-DD HH:MM` in the user's timezone before calling `remctl`; do not invent broader natural-language phrases.
- `add` and `edit` are atomic for due dates: if `-d/--due` is present and cannot be parsed, RemCTL exits before writing. With `--json`, parse failures are structured `invalid_due_date` errors on stderr with accepted examples. Retry with a corrected date instead of creating first and patching later.
- Accepted dependency-free due-date forms include `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, `today at 3pm`, `tomorrow 09:30`, `tonight at 11`, `Friday at 15:00`, `next friday at 3pm`, `+3d`, `eod`, and `eow`.
- Date output should match Reminders.app's displayed date. RemCTL reads `ZDISPLAYDATEDATE` first and falls back to `ZDUEDATE`.
- When debugging due-date mismatches, compare both fields in the Reminders database before assuming the CLI or UI is wrong.

Fast create path for agents:

```bash
remctl add "Title" -l Projects --private --section "Section" -d "YYYY-MM-DD HH:MM" --url "https://example.com" --json
remctl info <numericId> --json
```

`info --json` includes section, due date, tags, subtasks, attachments, deep link, and private rich-link `url` when present. Avoid raw SQLite checks unless the CLI output lacks a field you need.

## Permissions

First-run setup:

```bash
remctl onboard
remctl permissions full-disk-access
remctl doctor
```

RemCTL may need Reminders access for EventKit writes and private ReminderKit writes, Automation access for AppleScript fallback operations, and Full Disk Access for direct database reads. The guided permission helper only handles CLI targets; there is no service target. `remctl-private` does not have its own first-run flow; it depends on the same Reminders access and must be installed next to `remctl`.

macOS TCC permissions are scoped to the process context. Terminal can pass `remctl doctor` while Codex or another agent runner fails from its own context. If agent-side `doctor` fails but the user's Terminal passes, treat that as expected TCC scoping rather than a broken install. Ask the user to grant Full Disk Access to the target printed by `remctl doctor --for-agent`, or for a one-off unblock run the requested `remctl` command through Terminal via AppleScript and capture stdout/stderr in temp files.

## Development Checks

```bash
python3 -m py_compile remctl remctl_runtime.py remctl_serialization.py
swiftc -O -framework EventKit -framework Foundation -o /tmp/remctl-bridge-check remctl-bridge.swift
swiftc -O -framework AppKit -framework Foundation -o /tmp/remctl-permissions-check remctl-permissions.swift
clang -fobjc-arc -O -F/System/Library/PrivateFrameworks -framework Foundation -framework AppKit -framework ReminderKit -o /tmp/remctl-private-check remctl-private.m
./install.sh --bootstrap
remctl doctor --for-agent --json
```
