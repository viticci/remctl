---
name: remctl
description: Read or change Apple Reminders through RemCTL 2.0's local MCP tools, or diagnose its signed Capability Host, permissions, and client connections on macOS.
---

# RemCTL

RemCTL is a Reminders CLI and MCP server for macOS. Data commands run inside a signed app, `RemCTL Capability Host.app`, which holds the macOS permissions. You never need Full Disk Access, Reminders, or Automation access for your own process. Reads come from the local Reminders database. Writes go through EventKit, or through Apple's private ReminderKit framework when the user opts in with `--private`. RemCTL never writes the database directly.

## Use the local MCP server

Use **`mcp__remctl__<tool>`**, the RemCTL 2.0 server running locally on this Mac, for all reminder reads and writes. Prefer it over the Mac Remote connector's RemCTL wrappers. Do not shell out to the CLI for reminder work or silently fall back to it when an MCP connection fails.

The dedicated tools are `today`, `upcoming`, `overdue`, `flagged`, `search`, `show_list`, `lists`, `get_list`, `get_reminder`, `resolve_location`, `create_reminder`, `update_reminder`, `set_completion`, `set_flagged`, `delete_reminder`, `recently_deleted`, `restore_reminder`, `create_list`, `update_list`, and `doctor`. They validate arguments and return `structuredContent`; list results wrap rows in `items` with a `count`. Check `isError` and `structuredContent.error` before treating a call as successful.

Use `run` only for operations without a dedicated tool, including private metadata. Pass an `args` array of exact argument items, include `--json`, and add `--force` for an authorized destructive operation. This still runs through MCP. `run` refuses `mcp`, `onboard`, `setup`, `permissions`, `completion`, and `open`. Shell commands are reserved for installation, connection repair, and permission setup, or an explicit user request for CLI usage. If tools are missing, inspect or repair the local registration and reconnect the client.

Examples of MCP tool arguments:

```text
today({"include_overdue":true})
show_list({"list":"Work"})
get_reminder({"reminder_id":23880})
create_reminder({"title":"Research","list":"Work","due":"2026-09-20 15:00","priority":"high"})
update_reminder({"reminder_id":23880,"due":"clear"})
search({"query":"macstories.net","list_id":153,"offset":100})
set_completion({"reminder_ids":[23880,23881],"completed":true})
get_list({"list":"Shopping"})
create_reminder({"title":"Research","list":"Projects","private":true,"tags":["remctl"],"url":"https://example.com","section":"Research","subtasks":["Outline"]})
resolve_location({"query":"Piazza Navona, Rome"})
update_reminder({"reminder_id":23880,"private":true,"location_address":"Piazza Navona, Rome","location_title":"Stamps","radius":150})
run({"args":["sections","--json"]})
run({"args":["edit","23880","--private","--set-tags","remctl,work","--json"]})
```

The command syntax below documents the underlying options; it is not an instruction to use the shell. Use the named dedicated MCP tool where available, otherwise translate that syntax into `run.args`, omitting the `remctl` executable. Read [docs/mcp.md](docs/mcp.md) for the current typed argument contract and connection details. Inspect the live tool schema before a call. `create_reminder` and `update_reminder` take `private: true` for synced tags, rich links, sections, subtasks, assignment, Early Reminders, urgent state, and location alarms; without it those fields are refused, `url` is appended to the notes, and `create_reminder`'s `tags` become `#hashtags`. Images, Groceries sorting, ordering, groups, smart lists, and templates still use `run`.

## Setup and diagnosis

Install and onboard once:

```bash
cd /path/to/remctl && ./install.sh --bootstrap
remctl onboard
```

`onboard` is a guided flow: macOS permissions, a health check, connecting AI apps on the Mac, and optionally serving the tools to the user's other devices over Tailscale. It asks before each change. `onboard --json` runs the checks and reports detected apps without asking. If it opens the Full Disk Access helper, pause: the user must add the exact host app (`capabilityHost.app.path` from `doctor`) in System Settings, then restart the host:

```bash
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
```

Check readiness with the `doctor` MCP tool. If MCP itself cannot start, use this setup diagnostic:

```bash
remctl doctor --for-agent --json
```

Read `access.effective`. Ready means `route: "capabilityHost"`, `ready: true`, `capabilityHost.fullReady: true`, `capabilityHost.protocolVersion: 2`, `capabilityHost.privateProtocol.compatible: true`, and `fullDiskAccess`, `reminders`, and `automation` all `authorized` under `capabilityHost.permissions`. `access.direct` describes only your own process and is normally blocked; that is fine. Do not run `doctor` before every task once readiness is known.

Connect AI apps:

```bash
remctl mcp install --client claude-code      # claude mcp add
remctl mcp install --client codex            # codex mcp add
remctl mcp install --client claude-desktop   # Claude Desktop and Cowork; the user must restart Claude
remctl mcp install --client tailscale        # HTTPS endpoint for the user's other tailnet devices
remctl mcp status
remctl mcp config --format tailscale         # commands and token for another device
```

Upgrades: `git pull && ./install.sh`, then `doctor`. Run `onboard` again only when `doctor` reports a permission problem. Do not copy or re-sign the host app by hand, and do not reset macOS privacy records as a routine fix. Never grant Full Disk Access, Reminders, or Automation to Terminal, Python, Hermes, Codex, or Claude; only the host needs them.

## Rules that always apply

- Use the numeric `id` from JSON for `info`, `edit`, `done`, `undone`, `delete`, `link`, `open`, and `subtasks`. Never pass a UUID from `deepLink`.
- Give deterministic due dates: `YYYY-MM-DD` for all-day reminders, `YYYY-MM-DD HH:MM` for timed ones, resolved in the user's time zone. `clear` removes a due date. An unparseable date stops the command before any write and returns `code: "invalid_due_date"` with examples; retry with a corrected value.
- Destructive commands (`delete`, `list-delete`, `section-delete`, `group-delete`, `smart-list-delete`, `template-delete`) need `--force` with `--json` or without a terminal. Without it, nothing is written and stderr carries `code: "confirmation_required"`. Delete only with user authorization; an explicit deletion request already provides it.
- Target lists by name or by `--list-id`, never both. Names resolve exact, then case-insensitive, then normalized (`Weekly 513` matches `🗓️ Weekly 513`). If several lists match, the command stops and lists candidate ids; use `--list-id`. Groups are not valid targets for reminder writes.
- Do not pass `--private` for recurrence, alarms, priority, notes, or ordinary list moves; those are EventKit features. Use `--private` only for the metadata in [Private metadata](#private-metadata), or when the user asks for it.
- Do not use `--via-eventkit` unless the task explicitly accepts limited data. It returns `eventKitId` values that no numeric-id command accepts, and no sections, tags, or private fields.
- `search` matches titles, notes, and saved rich links of active reminders, ignoring case and accents; add `--completed` to include completed ones. The MCP tool always returns a page: while `hasMore` is true, call again with `offset` set to `nextOffset`. Scope it with `list` or `list_id`.
- `done`, `undone`, and `delete` (MCP `reminder_ids`) take up to 50 ids. Read `succeeded`, `failed`, and `uncertain`. Never retry an `uncertain` id without `get_reminder` first: completing a repeating reminder advances it one occurrence each time.
- JSON preserves raw Reminders text. Human output strips control characters and adds marks (`⚑`, `!!!`, `🔗`, `🌄`) that are never in JSON.
- Never read or write the Reminders SQLite database yourself.

## Reading

| Task | CLI | MCP tool |
| --- | --- | --- |
| Due today plus overdue | `remctl today --json` | `today` |
| Next N days | `remctl upcoming 7 --json` | `upcoming` |
| Overdue only | `remctl overdue --json` | `overdue` |
| Flagged | `remctl flagged --json` | `flagged` |
| Search | `remctl search "query" [--completed] [--list NAME\|--list-id ID] [--limit N --offset N] --json` | `search` |
| One list, in Reminders' order | `remctl show Work --json`, `remctl show --list-id 153 --json` | `show_list` |
| All lists with ids | `remctl lists --json` | `lists` |
| One list with section ids and sharees | `remctl list-info Work --json` | `get_list` |
| Check an address for a location alarm | `remctl location-lookup "ADDRESS" --json` | `resolve_location` |
| One reminder in full | `remctl info 23880 --json` | `get_reminder` |
| Groups, sections, tags, subtasks, sharees, stats, smart lists, templates | `remctl groups --json`, `sections --json`, `tags --json`, `subtasks ID --json`, `sharees LIST --json`, `stats --json`, `smart-lists --json`, `templates --json`, `template-info NAME --json` | `run` |

Row fields: `id`, `title`, `list`, `completed`, `flagged`, `urgent`, `priority`, `subtaskCount`, `dueDate`, `allDay`, `deepLink`, plus `notes`, `section`, `recurrence`, and `attachments` when present. `info` adds `alarms`, `earlyReminder`, `tags`, `subtasks`, `assignment`, and the rich-link `url`. `dueDate` is the real due date. `displayDate` appears when Reminders shows the reminder at another time, for example because an absolute alarm still points at an old time. Reminders.app lists and labels the reminder at `displayDate`, and `today`, `overdue`, and `upcoming` place it there too. `attachments[].path` is the host-verified file location, or `null` with `resolved: false` for files not downloaded to this Mac; it does not mean your process can open the file.

## Writing

| Task | CLI | MCP tool |
| --- | --- | --- |
| Create | `remctl add "Title" -l Work -d "2026-05-20 15:00" -p high --json` | `create_reminder` |
| Edit fields | `remctl edit 23880 --title "New" -d clear -n "Notes" --json` | `update_reminder` |
| Move to a list | `remctl edit 23880 -l Work --json` | `update_reminder` with `list` or `list_id` |
| Complete, uncomplete | `remctl done 23880 [23881 …] [--date 2026-05-27] --json`, `remctl undone 23880 --json` | `set_completion` with `reminder_id` or `reminder_ids` |
| Flag, unflag | `remctl flag 23880 --json`, `remctl unflag 23880 --json` | `set_flagged` |
| Delete | `remctl delete 23880 [23881 …] --force --json` | `delete_reminder` with `reminder_id` or `reminder_ids` |
| Create or edit a list | `remctl list-create NAME --json`, `remctl list-rename`, `remctl list-edit … --private` | `create_list`, `update_list` |
| Recurrence, alarm | `remctl add "Standup" -d "tomorrow 09:30" --recurrence "weekly mon,wed,fri" --alarm 15m --json` | `create_reminder` with `due` / `update_reminder` |
| Reorder | `remctl reminder-move 23880 --before 23881 --private --json` | `run` |

- `create_reminder` returns `status: "created"`, the numeric `id` for the next call, and the CloudKit `cloudKitId`. The CLI's `add --json` names them differently: `id` is the UUID and `numericId` is the number. If the number could not be read back, `create_reminder` has no `id` and warns `numeric_id_unavailable`; find the reminder with `search` or `show_list` by title.
- `add -f/--flag` creates the reminder first and flags it through automation. If the flag step fails, the result is still `created` with `warnings: ["flag_not_set: …"]`. Do not run `add` again; flag the returned id instead.
- `edit -l` and `edit --list-id` normally keep the id. When EventKit refuses a pure move (parents with subtasks, shared-list boundaries), RemCTL clones and deletes through ReminderKit and returns `method: "clone-delete"`, `oldId`, and a new `id`. Continue with the new `id`. Move first, then apply other edits.
- `edit -d` moves the absolute alarms that matched the old due time, so the time shown in Reminders follows the due date. Reminders can keep one copy of an alarm for each device that handled it, and every copy moves. When the reminder has any other alarm, its alarms stay as they are. `edit -d clear` removes those alarms, but a repeating reminder must keep its due date: `edit` refuses with `code: "repeating_reminder_requires_due_date"` and changes nothing.
- `done --date` takes only `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` and is rejected for recurring reminders; plain `done` advances the series.
- `flag`/`unflag` succeed with `status: "flagged"` or `"unflagged"`, or fail with exit 1 and `code: "applescript_flag_failed"` on stderr, flag unchanged. Common causes: the host lacks Automation access, or Reminders did not answer within 120 seconds. `edit ID --private --flagged` or `--no-flagged` writes the flag through ReminderKit instead.
- `reminder-move` needs `--private`. Within one list, the anchor must be in the same list. `--smart-list NAME` or `--smart-list-id ID` reorders an unsectioned custom smart list; sectioned smart lists are refused. Success returns `verified: true`.

Recurrence grammar: `daily`, `weekly`, `monthly`, `yearly`; an interval right after the frequency (`daily x2`, N 1 to 999); weekdays for weekly (`weekly mon,wed,fri`); day numbers (`monthly 1,15`) or ordinal weekdays (`monthly 4th-fri`, `monthly 1st-mon,3rd-mon`, `monthly last-fri`) for monthly, never mixed. Prefer `last-fri` to `5th-fri`. Invalid recurrence, alarm, and priority values fail before writing. Recurrence and relative alarms (`15m`, `1h`, `1d`) need a due date: pass `-d` (MCP `due`) with `add`, while `edit` can also use the due date the reminder already has. Without one, a relative alarm stops with `code: "relative_alarm_requires_due_date"` and Reminders refuses to save a repeating reminder; nothing is written.

## Recently Deleted

Use `recently_deleted` to find recoverable reminders. Follow `nextOffset` while `hasMore` is true; pages count parents and nest subtasks. `get_reminder` with `include_deleted: true` can inspect a deleted ID. The original list may be unavailable, and deleted details do not include all private metadata.

To recover, call `restore_reminder` with the item's `restoreId`, a destination `list` or `list_id` in the same account, and `private: true`. This restores the parent and its subtasks with their original IDs. Do not recreate them with `create_reminder`. `verified: true` means the IDs and hierarchy were read back; `already_restored` is a no-op for an active ID already in that list. For `restore_unconfirmed`, inspect the ID and refresh Recently Deleted before retrying.

Apple normally keeps deleted reminders for 30 days. Do not promise a deadline or recoverability from a database deletion flag. RemCTL asks Apple's recovery view and does not expose permanent purging. See [recovery details](docs/commands.md#recently-deleted).

## Private metadata

`--private` writes Reminders-only fields through ReminderKit. Use it for: synced rich links (`--url`), synced tags (`-t` adds; `--set-tags` replaces; `--remove-tag` and `--clear-tags` remove), sections (`--section`, `--section-id`, `--new-section`, and the `section-*` commands), shared-list assignment (`--assign`, `--unassign`), subtasks (`--subtask TITLE` or a JSON object), image attachments (`--image PATH`), flag and urgent state (`--flagged`, `--urgent`), Early Reminders (`--early-reminder 15m|1h|2d|1w|1mo|clear`, needs a due date), location alarms (`--location-title` with `--latitude` and `--longitude`, or `--location-address`; saved through EventKit but guarded by `--private`), ordering (`reminder-move`), list appearance and pins (`list-create`, `list-edit`, `list-pin`, `list-unpin`), Groceries lists (`--groceries`, `--grocery`), groups (`group-*`), custom smart lists (`smart-list-*`), and templates (`template-*`).

```bash
remctl add "Research" -l Projects --private --url https://example.com -t remctl --section Research --json
remctl edit 23880 --private --set-tags remctl,work --json
remctl edit 23880 --private --subtask '{"title":"Follow up","due":"2026-06-01","tags":["work"]}' --json
remctl edit 23880 --private --assign alex@example.com --json
remctl section-create "Research" -l Projects --private --json
```

Rules:

- Rich URLs must be public `http` or `https` hosts. Without `--private`, `--url` appends to existing notes (or to an explicit replacement from `--notes`); `add -t` adds title `#hashtags`, while `edit -t` requires `--private`.
- Rich links and images are additive; RemCTL never removes or replaces existing ones. Generic files and PDFs are rejected.
- `--section` resolves by name; with duplicate names in one list, RemCTL uses the single non-empty one, otherwise use `--section-id`. Section create and rename refuse duplicate names.
- `--assign` accepts a unique name, an email or phone address, the numeric sharee `id`, the `objectUUID`, or `me`. Call `sharees LIST --json` first and prefer the address or an id.
- `--location-address` (MCP `location_address`) is geocoded before anything is written. RemCTL uses it only for a single street-sized match that names the street typed, in an address that also gives a town or postal code. `location_not_found`, `location_ambiguous` (read `candidates`), `location_imprecise` (a whole city or region), `location_unconfirmed` (a different street, or no town; read `reason`), and `location_label_not_address` (`Home`, `Work`) change nothing. Never turn a personal label into an address yourself; ask the user for the address, and pass the label as `--location-title`.
- Subtask JSON fields: `title`, `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location fields.
- If a private step fails after the reminder exists, `add --json` returns `status: "partial"` with `numericId`, `failed`, and `error`. Finish with `edit`; do not run `add` again.

### Lists, groups, Groceries

- `list-create --color NAME` is EventKit. Exact `#RRGGBB` colors, `--symbol` (official names from `list-symbols` only), `--emoji`, Groceries mode, and pins need `--private`.
- `list-pin`/`list-unpin` also target smart lists by name or `--smart-list-id`. Verify smart-list pins in `smart-lists --json` (`pinned`, positive `pinnedDate`), not in `lists --json`. Built-in smart-list pinning fails before saving on hosts without the generic ReminderKit fetch (Tahoe 26.2 and Golden Gate 27.0).
- Groups hold lists, not reminders. `group-create`, `group-edit` (`--new-name`, `--add-list`, `--remove-list`, `--move-list` with `--before-list`, `--after-list`, `--first`, `--last`), `list-create --private --group`, and `group-delete` move containers only. Verify with `group-info`, `groups --json`, `lists --json`, `show <group> --json`.
- Groceries lists show `listType: "groceries"` in `lists --json`. Use `add --private --grocery` and `edit --private --grocery` only there, then check `section` in `show <list> --json`.

### Smart lists

`smart-lists --json` is read-only. `smart-list-create`, `smart-list-edit`, and `smart-list-delete` need `--private` and target custom smart lists by exact name or `--smart-list-id`. Supported filters: `--any-tag`; `--tags a,b` with `--tag-match all|any`; `--date any|today` plus `--date-today-include-past-due`, `--date-on`, `--date-before`, `--date-after`, `--date-range START,END`; `--time morning|afternoon|evening|night`; `--priority high` or a comma list; `--flagged`; `--vehicle connected`; one `--include-list` or `--include-list-id`; `--match all|any`; appearance `--color`, `--symbol`, `--emoji`. Rejected: untagged, no date, relative date, no time, vehicle disconnected, list exclusions, more than one included list, and the legacy short tag JSON. Do not aggregate several lists in one smart list. `--filter-json` takes raw filter JSON or `@path` and is validated. An edit with only `--match` or `--tag-match` is an error. Verify in `smart-lists --json`: `objectUUID`, decoded filter, `filter.supported`, and `minimumSupportedVersion` `20220430`.

### Templates

`templates --json` and `template-info` are read-only. `template-create --from-list NAME|--from-list-id ID [--include-completed]`, `template-apply`, and `template-delete` need `--private` and work on whole lists only. Do not promise editing single reminders inside a template or creating iCloud template links. Verify with `templates --json`, `template-info`, and after apply with `lists --json` and `show <new list> --json`.

## Verification

After a write, read the data back through MCP:

- Reminder fields, tags, section, subtasks, alarms, Early Reminders, rich link, assignment: `get_reminder({"reminder_id":ID})`.
- List order: `show_list` with the exact `list` or `list_id`.
- List appearance and Groceries metadata: `lists`; groups: `run` with `group-info` and `--json`.
- Smart lists and pins: `run({"args":["smart-lists","--json"]})`.
- Templates: `run` with `templates` or `template-info` and `--json`.
- When cross-device sync matters, ask the user to check another device.

When debugging a date mismatch, compare `dueDate`, `displayDate`, and `alarms` before assuming a bug.

## Errors

| Signal | Meaning | Action |
| --- | --- | --- |
| `code: "invalid_due_date"` | Due date not parseable; nothing written | Retry with `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` |
| `code: "confirmation_required"` | Destructive command without `--force`; nothing written | Confirm authorization, then add `--force` |
| `status: "partial"` | Reminder created, a private step failed | `edit` the returned `numericId`; do not `add` again |
| Batch `uncertain` ids, `code: "completion_uncertain"` or `"write_uncertain"` | A write may have landed | `get_reminder` each id before retrying; never complete a repeating reminder again blindly |
| `code: "location_…"` | Address lookup refused; nothing written | Follow the message: fuller address, a candidate's coordinates, or ask the user |
| `warnings: ["flag_not_set: …"]` | Reminder created, flag not set | Flag the returned id |
| `code: "applescript_flag_failed"` | Flag unchanged | Check the host's Automation grant with `doctor`; or use `edit --private --flagged` |
| Several lists match | Ambiguous list name | Use `--list-id` |
| Capability Host unavailable or not ready | Host stopped or permissions missing | `remctl doctor --for-agent --json`, then the fix it prints; reinstall if the protocol is not 2 |
| `remctl-private is outdated` | Sealed helper older than the CLI | `./install.sh` |

## Permissions

The host is the only macOS privacy target. `onboard` requests Reminders and Automation only when they are missing and guides Full Disk Access, which macOS allows only by hand. After changing Full Disk Access, restart the host and run `doctor`. If `doctor` reports Reminders or Automation denied, enable `RemCTL Capability Host` in that System Settings pane and run `onboard` again. `remctl permissions full-disk-access` reopens the Full Disk Access helper. A freshly started host may report Automation as `targetNotRunning` or `unknown` until it verifies the state; Reminders does not need to stay open.

For private-API drift audits, send `{"action":"capabilities"}` to the installed `remctl-private`; it is read-only and reports `saveCalled: false`. For a live release check of the installed generation:

```bash
REMCTL_CAPABILITY_HOST=force python3 scripts/live_private_matrix.py --remctl "$HOME/bin/remctl"
```
