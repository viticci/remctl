---
name: remctl
description: Use when an agent needs to read or change Apple Reminders through RemCTL (its MCP tools or the remctl CLI), or diagnose its installation, signed Capability Host, permissions, or client connections on macOS.
---

# RemCTL

RemCTL is a Reminders CLI and MCP server for macOS. Data commands run inside a signed app, `RemCTL Capability Host.app`, which holds the macOS permissions. You never need Full Disk Access, Reminders, or Automation access for your own process. Reads come from the local Reminders database. Writes go through EventKit, or through Apple's private ReminderKit framework when the user opts in with `--private`. RemCTL never writes the database directly.

## Which surface to use

1. **MCP tools, when your host has them.** They are named `today`, `upcoming`, `overdue`, `flagged`, `search`, `show_list`, `lists`, `get_reminder`, `create_reminder`, `update_reminder`, `set_completion`, `set_flagged`, `delete_reminder`, `doctor`, and `run`. In Claude Code they appear as `mcp__remctl__<tool>`. They validate arguments, return `structuredContent`, and use the same numeric ids as the CLI. Use `run` for any CLI command that has no dedicated tool: pass exact argument items, include `--json`, and add `--force` to destructive commands.
2. **The CLI, otherwise.** Resolve the installed command with `command -v remctl`; if it is not on `PATH`, use `~/bin/remctl`. Invoke the absolute path. Use `--json` for every read and write.

Everything below applies to both. Where a rule names a CLI flag, the `run` tool takes the same flag.

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

Check readiness:

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
- Destructive commands (`delete`, `list-delete`, `section-delete`, `group-delete`, `smart-list-delete`, `template-delete`) need `--force` with `--json` or without a terminal. Without it, nothing is written and stderr carries `code: "confirmation_required"`. Confirm with the user before deleting.
- Target lists by name or by `--list-id`, never both. Names resolve exact, then case-insensitive, then normalized (`Weekly 513` matches `🗓️ Weekly 513`). If several lists match, the command stops and lists candidate ids; use `--list-id`. Groups are not valid targets for reminder writes.
- Do not pass `--private` for recurrence, alarms, priority, notes, or ordinary list moves; those are EventKit features. Use `--private` only for the metadata in [Private metadata](#private-metadata), or when the user asks for it.
- Do not use `--via-eventkit` unless the task explicitly accepts limited data. It returns `eventKitId` values that no numeric-id command accepts, and no sections, tags, or private fields.
- `search` matches titles and notes of active reminders; add `--completed` to include completed ones.
- JSON preserves raw Reminders text. Human output strips control characters and adds marks (`⚑`, `!!!`, `🔗`, `🌄`) that are never in JSON.
- Never read or write the Reminders SQLite database yourself.

## Reading

| Task | CLI | MCP tool |
| --- | --- | --- |
| Due today plus overdue | `remctl today --json` | `today` |
| Next N days | `remctl upcoming 7 --json` | `upcoming` |
| Overdue only | `remctl overdue --json` | `overdue` |
| Flagged | `remctl flagged --json` | `flagged` |
| Search | `remctl search "query" [--completed] --json` | `search` |
| One list, in Reminders' order | `remctl show Work --json`, `remctl show --list-id 153 --json` | `show_list` |
| All lists with ids | `remctl lists --json` | `lists` |
| One reminder in full | `remctl info 23880 --json` | `get_reminder` |
| Groups, sections, tags, subtasks, sharees, stats, smart lists, templates | `remctl groups --json`, `sections --json`, `tags --json`, `subtasks ID --json`, `sharees LIST --json`, `stats --json`, `smart-lists --json`, `templates --json`, `template-info NAME --json` | `run` |

Row fields: `id`, `title`, `list`, `completed`, `flagged`, `urgent`, `priority`, `subtaskCount`, `dueDate`, `allDay`, `deepLink`, plus `notes`, `section`, `recurrence`, and `attachments` when present. `info` adds `alarms`, `earlyReminder`, `tags`, `subtasks`, `assignment`, and the rich-link `url`. `dueDate` is the real due date; `displayDate`, when present, is Reminders' separate display or alert time. `attachments[].path` is the host-verified file location, or `null` with `resolved: false` for files not downloaded to this Mac; it does not mean your process can open the file.

## Writing

| Task | CLI | MCP tool |
| --- | --- | --- |
| Create | `remctl add "Title" -l Work -d "2026-05-20 15:00" -p high --json` | `create_reminder` |
| Edit fields | `remctl edit 23880 --title "New" -d clear -n "Notes" --json` | `update_reminder` |
| Move to a list | `remctl edit 23880 -l Work --json` | `update_reminder` with `list` or `list_id` |
| Complete, uncomplete | `remctl done 23880 [--date 2026-05-27] --json`, `remctl undone 23880 --json` | `set_completion` |
| Flag, unflag | `remctl flag 23880 --json`, `remctl unflag 23880 --json` | `set_flagged` |
| Delete | `remctl delete 23880 --force --json` | `delete_reminder` |
| Recurrence, alarm | `remctl add … --recurrence "weekly mon,wed,fri" --alarm 15m --json` | `create_reminder` / `update_reminder` |
| Reorder | `remctl reminder-move 23880 --before 23881 --private --json` | `run` |

- `add --json` returns `status: "created"`, `id` (a UUID), and `numericId`. Use `numericId` for the next call. If `numericId` is missing, find the reminder with `show <list> --json` by title.
- `add -f/--flag` creates the reminder first and flags it through automation. If the flag step fails, the result is still `created` with `warnings: ["flag_not_set: …"]`. Do not run `add` again; flag the returned id instead.
- `edit -l` and `edit --list-id` normally keep the id. When EventKit refuses a pure move (parents with subtasks, shared-list boundaries), RemCTL clones and deletes through ReminderKit and returns `method: "clone-delete"`, `oldId`, and a new `id`. Continue with the new `id`. Move first, then apply other edits.
- `edit -d` moves a single absolute alarm that matched the old due time, so the time shown in Reminders follows the due date.
- `done --date` takes only `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` and is rejected for recurring reminders; plain `done` advances the series.
- `flag`/`unflag` succeed with `status: "flagged"` or `"unflagged"`, or fail with exit 1 and `code: "applescript_flag_failed"` on stderr, flag unchanged. Common causes: the host lacks Automation access, or Reminders did not answer within 120 seconds. `edit ID --private --flagged` or `--no-flagged` writes the flag through ReminderKit instead.
- `reminder-move` needs `--private`. Within one list, the anchor must be in the same list. `--smart-list NAME` or `--smart-list-id ID` reorders an unsectioned custom smart list; sectioned smart lists are refused. Success returns `verified: true`.

Recurrence grammar: `daily`, `weekly`, `monthly`, `yearly`; an interval right after the frequency (`daily x2`, N 1 to 999); weekdays for weekly (`weekly mon,wed,fri`); day numbers (`monthly 1,15`) or ordinal weekdays (`monthly 4th-fri`, `monthly 1st-mon,3rd-mon`, `monthly last-fri`) for monthly, never mixed. Prefer `last-fri` to `5th-fri`. Invalid recurrence, alarm, and priority values fail before writing.

## Private metadata

`--private` writes Reminders-only fields through ReminderKit. Use it for: synced rich links (`--url`), synced tags (`-t` adds; `--set-tags` replaces; `--remove-tag` and `--clear-tags` remove), sections (`--section`, `--section-id`, `--new-section`, and the `section-*` commands), shared-list assignment (`--assign`, `--unassign`), subtasks (`--subtask TITLE` or a JSON object), image attachments (`--image PATH`), flag and urgent state (`--flagged`, `--urgent`), Early Reminders (`--early-reminder 15m|1h|2d|1w|1mo|clear`, needs a due date), location alarms (`--location-title` with `--latitude` and `--longitude`; saved through EventKit but guarded by `--private`), ordering (`reminder-move`), list appearance and pins (`list-create`, `list-edit`, `list-pin`, `list-unpin`), Groceries lists (`--groceries`, `--grocery`), groups (`group-*`), custom smart lists (`smart-list-*`), and templates (`template-*`).

```bash
remctl add "Research" -l Projects --private --url https://example.com -t remctl --section Research --json
remctl edit 23880 --private --set-tags remctl,work --json
remctl edit 23880 --private --subtask '{"title":"Follow up","due":"2026-06-01","tags":["work"]}' --json
remctl edit 23880 --private --assign alex@example.com --json
remctl section-create "Research" -l Projects --private --json
```

Rules:

- Rich URLs must be public `http` or `https` hosts. Without `--private`, `--url` only appends to the notes and `-t` only adds `#hashtags` to the title.
- Rich links and images are additive; RemCTL never removes or replaces existing ones. Generic files and PDFs are rejected.
- `--section` resolves by name; with duplicate names in one list, RemCTL uses the single non-empty one, otherwise use `--section-id`. Section create and rename refuse duplicate names.
- `--assign` accepts a unique name, an email or phone address, the numeric sharee `id`, the `objectUUID`, or `me`. Call `sharees LIST --json` first and prefer the address or an id.
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

After a write, read the data back:

- Reminder fields, tags, section, subtasks, alarms, Early Reminders, rich link, assignment: `info <id> --json`.
- List order: `show <list> --json`.
- List appearance, Groceries metadata, groups: `lists --json`, `group-info --json`.
- Smart lists and pins: `smart-lists --json`.
- Templates: `templates --json`, `template-info`.
- When cross-device sync matters, ask the user to check another device.

When debugging a date mismatch, compare `dueDate`, `displayDate`, and `alarms` before assuming a bug.

## Errors

| Signal | Meaning | Action |
| --- | --- | --- |
| `code: "invalid_due_date"` | Due date not parseable; nothing written | Retry with `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` |
| `code: "confirmation_required"` | Destructive command without `--force`; nothing written | Confirm with the user, add `--force` |
| `status: "partial"` | Reminder created, a private step failed | `edit` the returned `numericId`; do not `add` again |
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
