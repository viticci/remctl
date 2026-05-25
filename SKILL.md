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
- For private reminder metadata, use regular `add` or `edit` with `--private`; for private list appearance, Groceries mode, or regular/smart-list pin state, use `list-create --private`, `list-edit --private`, `list-pin --private`, or `list-unpin --private`; for custom smart lists, use `smart-list-create`, `smart-list-edit`, or `smart-list-delete` with `--private`; for Reminders templates, use `template-create`, `template-apply`, or `template-delete` with `--private`. Do not use raw database mutation.
- After changing repo code, reinstall before testing the user-facing command: `./install.sh && hash -r`.

## Agent Routing

Start by deciding the write path. Public EventKit writes are stable and do not need `--private`. Private ReminderKit writes are unsupported, explicit, and required for Reminders-only metadata that EventKit cannot save.

| User intent | Command path | Private? | Verify with |
| --- | --- | --- | --- |
| Read due items, lists, reminders, tags, sections, subtasks | `today`, `upcoming`, `overdue`, `lists`, `show`, `search`, `info`, `tags`, `sections`, `subtasks` | No | same command with `--json` |
| Create/edit ordinary reminder fields | `add`, `edit`, `done`, `undone`, `delete` | No | `info <id> --json` or `show <list> --json` |
| Due date, priority, notes, recurrence, EventKit alarm | `add` or `edit` with `-d`, `-p`, `-n`, `--recurrence`, `--alarm` | No | `info <id> --json`; recurrence appears as `recurrence` |
| Move an existing reminder to another list | `edit <id> -l LIST` or `edit <id> --list-id ID` | No | `info <id> --json` or `show <destination> --json` |
| Synced rich URL, real tags, section, subtask, image, real flag, urgent, Early Reminder, location alarm | `add --private` or `edit --private` | Yes | `info <id> --json`; UI/device check when sync matters |
| List appearance, Groceries metadata, list or smart-list pin state | `list-create --private`, `list-edit --private`, `list-pin --private`, `list-unpin --private` | Yes | `lists --json` for list color/badge/Groceries/pin state, `smart-lists --json` for smart-list appearance/pin state |
| Custom smart list create/edit/delete | `smart-list-create`, `smart-list-edit`, `smart-list-delete` | Yes | `smart-lists --json` |
| Saved Reminders templates | `templates`, `template-info`, `template-create`, `template-apply`, `template-delete` | Reads no; writes yes | `templates --json`, `template-info`, then `show <new list> --json` after apply |

High-value guardrails:

- Do not use `--private` for normal recurrence or normal `--alarm`; those are EventKit features.
- Do use `--private --early-reminder` for Reminders' Early Reminder menu values; this is separate from EventKit alarms.
- Location alarms use the `edit --private --location-*` guardrail but are saved through the EventKit bridge as structured-location alarms; verify them in `info --json` under `alarms`.
- Do not verify smart-list pinning with `lists --json`; use `smart-lists --json`.
- Do not promise template link creation or editing individual saved reminders inside a template.
- Do not create multi-list aggregate smart lists with list filters; Reminders.app materializes only one included list through this write path.

## Common Commands

```bash
remctl today --json
remctl upcoming 7 --json
remctl overdue --json
remctl lists --json
remctl show Work --json
remctl show --list-id 153 --json
remctl search "query" --json
remctl search "query" --completed --json
remctl info 23880 --json
remctl add "Review PR" -l Work -d "tomorrow 10:00" -p high --json
remctl add "Write column" --list-id 156 -d "2026-05-20 15:00" --json
remctl add "Weekly report" -l Work --recurrence "weekly mon,wed,fri" --alarm 15m --json
remctl smart-lists --json
remctl templates --json
remctl template-info "Rome: Things To See" --json
remctl list-symbols --json
remctl edit 23880 -d clear --json
remctl edit 23880 -l Work --json
remctl edit 23880 --list-id 156 --json
remctl edit 23880 --recurrence monthly --json
remctl done 23880 --json
remctl link --list-id 153 --json
remctl export --list-id 153 --format json
remctl list-symbols --preview
remctl list-rename --list-id 123 --new-name "Project Archive" --json
remctl list-delete --list-id 123 --force --json
```

## Syntax Rules

- Use nouns for read-only inspectors: `lists`, `smart-lists`, `templates`, `today`, `stats`.
- Use verb-style commands for writes: `add`, `edit`, `delete`, `list-create`, `smart-list-create`, `smart-list-edit`, `template-create`.
- List-management commands use the `list-*` prefix; custom smart-list writes use the `smart-list-*` prefix; template writes use the `template-*` prefix.
- Use `--json` on subcommands for automation. For tabular read commands (`today`, `upcoming`, `overdue`, `flagged`, `urgent`, `lists`, `show`, and `search`), `--format json|table|plain` can be passed globally before the command or directly on the read command; export's `--format json|csv` is separate and chooses a file format.
- `export --format json|csv` chooses an export file format, not the display style.
- List targets resolve exact name first, then case-insensitive, then normalized names such as `Weekly 513` for `🗓️ Weekly 513`. If multiple lists match, RemCTL fails before writing; use `--list-id`.
- Commands that target lists consistently support exact numeric targeting where the underlying write/read path is safe: `show --list-id`, `add --list-id`, `edit --list-id`, `link --list-id`, `export --list-id`, `list-edit --list-id`, `list-pin --list-id`, `list-unpin --list-id`, `list-rename --list-id --new-name`, `list-delete --list-id`, plus smart-list `--include-list-id`. `list-pin` and `list-unpin` also accept smart-list names or `--smart-list-id`.
- If a command accepts both a list name and `--list-id`, passing both is an error.

## Recurring Schedules

Recurring schedules are normal EventKit writes and do not require `--private`.

```bash
remctl add "Daily journal" --recurrence daily --json
remctl add "Weekly report" --recurrence weekly --json
remctl add "Standup" --recurrence "weekly mon,wed,fri" --alarm 15m --json
remctl add "Pay rent" --recurrence monthly --json
remctl add "Annual review" --recurrence yearly --json
remctl edit 23880 --recurrence "weekly mon,wed" --json
```

Use `info --json`, `show --json`, `today --json`, or `upcoming --json` to verify recurrence readback. Recurring reminders include a stable `recurrence` object in JSON and a repeat badge in human/table output. Relative alarms such as `--alarm 15m` are EventKit alarms and verify in `info --json` under `alarms`; Early Reminders are separate private due-date delta alerts and require `--private --early-reminder`.

## Private Metadata

Use `--private` only when the user explicitly asks for private Reminders metadata or when a command needs synced web rich links, real tags, sections, subtasks, image attachments, real flags, urgent state, Early Reminders, location alarms, private list appearance metadata, Groceries mode/categorization verification, regular/smart-list pinning, custom smart-list creation/editing/deletion, or Reminders template creation/application/deletion.

```bash
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --section "Research" --json
remctl add "Research" -l Projects --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8 --json
remctl add "Prepare screenshots" -l Projects --private --image ~/Desktop/mockup.png --subtask "Export PNG" --json
remctl add "Leave now" -l Work --private --urgent --json
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m --json
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}' --json
remctl edit 23880 --private --url "https://example.com" -t remctl --json
remctl edit 23880 --private --section "Research" --subtask "Follow up" --json
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8 --json
remctl edit 23880 --private --subtask '{"title":"Follow up","notes":"Bring latest numbers","due":"next friday at 3pm","url":"https://example.com","tags":["work"]}' --json
remctl edit 23880 --private --flagged --urgent --json
remctl edit 23880 --private --early-reminder 1h --json
remctl edit 23880 --private --early-reminder clear --json
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200 --json
remctl list-create "Research" --color orange --private --symbol education3 --json
remctl list-create "Cold Ideas" --color cyan --private --emoji 🥶 --json
remctl list-create "Groceries" --private --groceries --grocery-locale en_US --json
remctl add "Milk" -l Groceries --private --grocery --json
remctl edit 23880 --private --grocery --json
remctl list-edit "Shopping" --private --standard --json
remctl list-edit Projects --private --color '#FF8D28' --symbol education3 --json
remctl list-edit --list-id 144 --private --emoji 📌 --json
remctl list-pin "Project X" --private --json
remctl list-pin "Flagged" --private --json
remctl list-unpin --list-id 144 --private --json
remctl list-unpin --smart-list-id 4 --private --json
remctl smart-list-create "Flagged Review" --private --flagged --json
remctl smart-list-create "High Priority" --private --priority high --json
remctl smart-list-create "Any Tag" --private --any-tag --json
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today --json
remctl smart-list-create "Projects Today" --private --include-list Projects --date today --date-today-include-past-due --json
remctl smart-list-create "Due Before June 1" --private --date-range 2026-05-16,2026-05-31 --color red --emoji 📆 --json
remctl smart-list-edit "Priority or Today" --private --priority high --color red --emoji 📆 --json
remctl smart-list-edit --smart-list-id 170 --private --match any --priority high,medium --date today --json
remctl smart-list-delete "Flagged Review" --private --force --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-create "Archive Template" --from-list-id 144 --include-completed --private --json
remctl template-apply "Packing Template" --private --json
remctl template-delete "Packing Template" --private --force --json
```

Private metadata rules:

- `--private --url` creates a synced web rich link. Without `--private`, `--url` is appended to notes.
- `--private -t/--tags` creates real synced tags. On `add` without `--private`, tags are inline title hashtags. On `edit`, tags require `--private`.
- `edit -l/--list` and `edit --list-id` move reminders through the normal EventKit bridge; they do not require `--private`.
- `--section` resolves by name; if duplicates exist in the same list, RemCTL uses the single non-empty match when possible. Use `--section-id` for exact assignment.
- `--early-reminder` writes Reminders' private Early Reminder due-date delta alert. It accepts `15m`, `1h`, `2d`, `1w`, `1mo`, or `clear`; non-clear values require a due date and must be verified with `remctl info ID --json`.
- `--location-title` with `--latitude` and `--longitude` requires `--private` as a guardrail, but RemCTL persists it through EventKit structured-location alarms because the private ReminderKit alarm mutation does not materialize reliably on current macOS.
- `--subtask` accepts either a plain child title or a JSON object with child metadata: `title`, `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location fields.
- `--section`, `--new-section`, `--subtask`, `--image`, `--flagged`, `--urgent`, `--early-reminder`, and location alarm fields require `--private` and should fail before writing if omitted.
- Rich-link and image attachment edits are additive. RemCTL can add synced rich links and images; it does not remove or replace existing rich links/images.
- `add --private -f` writes the real private flag instead of the EventKit priority proxy.
- `list-symbols` prints the 71 official Reminders emblem names; its terminal glyph column is only an approximation. Use `list-symbols --preview` to open a native-asset HTML contact sheet with interactive official color swatches, or `list-symbols --html PATH` to write one. `list-create --color NAME` uses public EventKit for normal colors. `list-create --private`, `list-edit --private`, `smart-list-create --private`, and `smart-list-edit --private` can write exact `#RRGGBB` colors, official list symbols, and emoji badges; verify those via `color`, `badge`, and `badgeEmblem` in `lists --json` or `smart-lists --json`. `list-create --private --groceries`, `list-edit --private --groceries`, and `list-edit --private --standard` write Reminders' private Groceries list metadata and locale. `list-pin` and `list-unpin` require `--private` and save regular list or smart-list pin state through ReminderKit. Reminders' picker icons use private emblem names such as `education3`; `--symbol` only accepts official names because arbitrary SF Symbol strings render as the default icon in Reminders. Use `--emoji` for custom standard emoji badges.
- Groceries lists are visible in `lists --json` as `listType: "groceries"`, `isGroceries: true`, and `grocery.locale`; human list headings show `🥕`. Use `add --private --grocery` or `edit --private --grocery` only against detected Groceries lists. RemCTL first verifies Reminders' automatic grocery sorting and reports `source: "reminders_auto"` when the item is already sectioned; it falls back to the private categorizer only for unsectioned items.
- `smart-lists` is read-only and safe. `smart-list-create`, `smart-list-edit`, and `smart-list-delete` use unsupported private ReminderKit APIs and require `--private`; filter writes support the Reminders.app filters that currently materialize through this write path.
- Verify smart-list pinning with `smart-lists --json`, not `lists --json`. On macOS 26, smart-list pinning can leave `ZISPINNEDBYCURRENTUSER` empty while updating `ZPINNEDDATE`; RemCTL reports `pinned: true` from a positive smart-list `pinnedDate`.
- `templates` and `template-info` are read-only and safe. They report saved Reminders templates, saved reminders, sections, and any existing public template links. Existing iCloud links are read-only metadata; RemCTL does not create sharing links.
- `template-create`, `template-apply`, and `template-delete` use unsupported private ReminderKit APIs and require `--private`. They are whole-list operations only: RemCTL does not append individual reminders to existing templates, copy selected reminders into templates, or strip subtasks or due dates while saving. Verify template writes with `remctl templates --json` or `remctl template-info`; verify applied templates with `remctl show <new list> --json`.
- Generic file/PDF attachments are rejected because Reminders does not reliably show them.
- Verify private reminder writes with `remctl info <numeric-id> --json`.
- Verify custom smart-list writes with `remctl smart-lists --json` and check the target custom smart list, decoded filter summary, `filter.supported`, and `minimumSupportedVersion`/`effectiveMinimumSupportedVersion` `20220430`; Reminders.app can show zero filters when those private version fields are left at `0`.
- Verify template writes with `remctl templates --json` or `remctl template-info`. Verify `template-apply` with `remctl lists --json` and `remctl show <new list> --json`.
- If cross-device sync matters, ask the user to check iPhone/iPad after CLI verification.

## Smart List Filters

`smart-list-create` and `smart-list-edit` accept these Reminders filters that currently materialize in Reminders.app through this write path:

```bash
remctl smart-list-create "Any Tag" --private --any-tag --json
remctl smart-list-create "#remctl Today" --private --tags remctl --date today --json
remctl smart-list-create "Priority: Any" --private --priority high,medium --json
remctl smart-list-create "Morning" --private --time morning --json
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today --json
remctl smart-list-create "Projects Today" --private --include-list Projects --date today --date-today-include-past-due --json
remctl smart-list-create "Near Home" --private --location-title Home --latitude 41.9 --longitude 12.5 --radius 100 --proximity enter --json
remctl smart-list-create "Due Before June 1" --private --date-range 2026-05-16,2026-05-31 --color red --emoji 📆 --json
remctl smart-list-edit --smart-list-id 170 --private --filter-json @filter.json --color red --emoji 📆 --json
```

Supported materializing filter families are Any Tag (`--any-tag`), selected tags (`--tags remctl` with optional `--tag-match all|any`), date (`--date any|today`, `--date-today-include-past-due`, `--date-on`, `--date-before`, `--date-after`, `--date-range START,END`), time (`morning`, `afternoon`, `evening`, `night`), priority (`high`, `medium`, `low`; comma-separated values mean Priority: Any), flag (`--flagged`), vehicle connected (`--vehicle connected`), specific location (`--location-title`, `--latitude`, `--longitude`, `--radius`, `--proximity enter|leave|arriving|leaving`), one included list (`--include-list` or `--include-list-id`), and top-level matching (`--match all|any`). `smart-list-create` and `smart-list-edit` also accept appearance flags `--color`, `--symbol`, and `--emoji`.

Known non-materializing writes are rejected before saving: untagged, no-date, relative date, no-time, vehicle disconnected, list exclusions, and more than one included list. Reminders.app currently materializes only one included-list filter at a time, so never try to aggregate multiple lists with smart-list list filters. Do not use `--filter-json` to force the legacy short selected-tag shape (`{"hashtags":{"hashtags":["tag"]}}`); Reminders.app can persist it but show zero filter rows. Use `--tags tag --date today` instead.

`--filter-json` is an advanced escape hatch for raw official filter JSON or `@path`; unknown or unsupported smart-list filter shapes are rejected before writing. `smart-list-edit` and `smart-list-delete` target custom smart lists by exact name or `--smart-list-id` and never match built-in smart lists.

## Templates

Template commands:

```bash
remctl templates --json
remctl template-info "Rome: Things To See" --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-create "Archive Template" --from-list-id 144 --include-completed --private --json
remctl template-apply "Packing Template" --private --json
remctl template-delete "Packing Template" --private --force --json
```

`templates` and `template-info` inspect `ZREMCDTEMPLATE`, `ZREMCDSAVEDREMINDER`, and template sections. `template-create` saves an entire existing list as a template, `template-apply` creates a new list from a template, and `template-delete` deletes only the saved template. All template writes require `--private`. Template support is intentionally list-level: do not promise appending individual reminders to an existing template, copying selected reminders into a template, or stripping subtasks/due dates while saving. Do not promise iCloud template link creation; RemCTL only reports existing public links.

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
- `dueDate` in JSON is the actual Reminders due date from `ZDUEDATE`. If Reminders stores a separate UI/alert display date, RemCTL reports it separately as `displayDate`.
- When debugging due-date or alarm mismatches, compare `dueDate`, `displayDate`, and `alarms` before assuming the CLI or UI is wrong.

Fast create path for agents:

```bash
remctl add "Title" -l Projects --private --section "Section" -d "YYYY-MM-DD HH:MM" --url "https://example.com" --json
remctl info <numericId> --json
```

`info --json` includes section, actual due date, optional display/alert date, tags, subtasks, parent and subtask attachments, EventKit alarms, location alarms, Early Reminders, deep link, and private rich-link `url` when present. Avoid raw SQLite checks unless the CLI output lacks a field you need.

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
python3 -m py_compile remctl remctl_runtime.py remctl_serialization.py remctl_smart_lists.py
python3 -m unittest discover -s tests -q
swiftc -O -framework EventKit -framework Foundation -o /tmp/remctl-bridge-check remctl-bridge.swift
swiftc -O -framework AppKit -framework Foundation -o /tmp/remctl-permissions-check remctl-permissions.swift
clang -fobjc-arc -O -F/System/Library/PrivateFrameworks -framework Foundation -framework AppKit -framework ReminderKit -o /tmp/remctl-private-check remctl-private.m
git diff --check
swiftc -O -framework EventKit -framework Foundation -o remctl-bridge remctl-bridge.swift
python3 scripts/live_private_matrix.py
./install.sh --bootstrap
remctl doctor --for-agent --json
```
