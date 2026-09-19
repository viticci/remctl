---
name: remctl
description: Use when an agent needs to operate Apple Reminders through RemCTL or diagnose its installation, signed Capability Host, permissions, or agent-runner access on macOS.
---

# RemCTL

RemCTL is a power-user Apple Reminders CLI. In a normal installation, protected data commands run through the dedicated, signed `RemCTL Capability Host.app`, which a LaunchAgent keeps available. Terminal, Hermes, Codex, and other same-user callers connect through an owner-only Unix socket using protocol version 2. The host is the single macOS permission target for Full Disk Access, Reminders, and Automation. Public writes use EventKit, non-private flag operations use AppleScript, and explicit `--private` writes use unsupported ReminderKit APIs inside the host. Local administration commands such as `onboard`, `permissions`, `doctor`, `setup`, `completion`, and `list-symbols` stay in the caller.

The installed command can be invoked as `remctl`, `rctl`, or `reminders`; all three names behave identically and produce the same output.

## Default Workflow

- In Hermes, Codex, and other agent runners, resolve the installed command with `command -v remctl` and invoke that absolute path. If it is not on `PATH`, check the installer's default `~/bin/remctl` and any custom prefix chosen by the user. Examples below use `remctl` as a human-readable abbreviation.
- Leave `REMCTL_CAPABILITY_HOST` unset. In a normal installed setup, its default `auto` mode routes protected commands through the signed host. If no usable host is installed, `auto` falls back to direct caller execution, so the caller becomes responsible for its own permissions. `force` requires the host. `direct` always bypasses it; a custom `REMCTL_STORE_DIR` also uses direct execution.
- Invoke the installed CLI only. Do not call the host socket, EventKit bridge, or private helper directly for normal tasks.
- Prefer JSON for automation and verification: `remctl today --json`, `remctl show Work --json`, `remctl info <id> --json`.
- Never write directly to the Reminders SQLite database.
- Never use `--via-eventkit` merely because the caller lacks direct database access. Check effective host readiness first; EventKit mode is deliberately limited and cannot replace full RemCTL semantics.
- For private reminder metadata, use regular `add` or `edit` with `--private`; use `reminder-move --private` for display ordering; use `edit --private --set-tags`, `--clear-tags`, or `--remove-tag` for synced tag replacement/removal; use `section-create`, `section-rename`, or `section-delete` with `--private` for section management; for private list appearance, Groceries mode, regular-list pinning, or custom-smart-list pinning, use `list-create --private`, `list-edit --private`, `list-pin --private`, or `list-unpin --private`; built-in smart-list pinning is host-capability-dependent. For list groups, use `group-create`, `group-edit`, `list-create --private --group`, or `group-delete` with `--private`; for custom smart lists, use `smart-list-create`, `smart-list-edit`, or `smart-list-delete` with `--private`; for Reminders templates, use `template-create`, `template-apply`, or `template-delete` with `--private`. Do not use raw database mutation.
- For every destructive command, pass `--force` in agent or `--json` workflows. Without it, RemCTL returns `confirmation_required` on stderr and performs no write.
- Treat `import` as a limited ordinary-field importer, not a lossless restore. It accepts only `title`, `list`, `notes`, `due`/`dueDate`, `priority`, `url`, `recurrence`, `alarm`, and Boolean `flagged`. It validates the entire array before writing, but a runtime failure after writes begin returns a nonzero `partial` summary and keeps successful reminders. Use `createdIds` and failed indexes to build a retry subset; never rerun the whole file blindly.

## First Install or Upgrade

The CLI requires Python 3.10 or later. The signed-host installer also requires a canonical protected Python 3.13 or later; set `REMCTL_CAPABILITY_PYTHON` only when automatic selection cannot find one.

For a first install, bootstrap the signed host, then run onboarding:

```bash
cd /path/to/remctl
./install.sh --bootstrap
remctl onboard
```

`onboard` requests the host's Reminders and Automation grants and opens the Full Disk Access pane when that grant is missing. Pause for the manual Full Disk Access step: add only the exact `capabilityHost.app.path` reported by `remctl doctor --for-agent --json`. If the pane was closed or must be reopened, run `remctl permissions full-disk-access`; the helper is not a separate mandatory onboarding step. Restart the LaunchAgent only after a Full Disk Access change. Then, whether or not a change was needed, verify effective host readiness:

```bash
# Only after changing Full Disk Access:
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
# Always after onboarding:
remctl doctor --for-agent --json
```

For a reinstall or upgrade from an existing signed-host installation, use the normal installer. It preserves the existing signed host identity, so macOS can retain its grants:

```bash
cd /path/to/remctl
./install.sh
remctl doctor --for-agent --json
```

The legacy migration recognizes only the exact public files from official RemCTL 1.7.1, its expected aliases, and expected native-helper types. Start with a normal upgrade. If generated helpers are present, manually review every reported path and rerun once with the same prefix plus `./install.sh --adopt-existing-install`. That release predates the signed host. After installing 1.8.0 over 1.7.1, run `remctl onboard`, complete any Full Disk Access step and restart, then verify `remctl doctor --for-agent --json`; there are no earlier host grants to preserve.

If an expected prerelease signed-host upgrade refuses an unmanifested installation, do not immediately bypass the refusal. Inspect every reported RemCTL-owned path and confirm the scripts, helpers, aliases, signed host app, and LaunchAgent belong to that reviewed prerelease installation. Move any foreign, unknown, or modified collision out of the install destinations. Only after that manual review, rerun once with the same prefix and `./install.sh --adopt-existing-install`, then verify `remctl --version` and `remctl doctor --for-agent --json`. Run onboarding if this exact host identity has not already received its grants. Never use adoption for routine upgrades or arbitrary older versions.

Do not run `onboard` during a routine reinstall or upgrade of an existing signed-host installation. Use it as a repair step only when `doctor --for-agent --json` reports a Reminders, Automation, or Full Disk Access problem. This does not apply to the official 1.7.1-to-1.8.0 first-host transition above. Reinstall through `install.sh`; do not copy, move, or re-sign the host app manually. Do not reset macOS permission records as a routine repair.

## Agent Routing

Start by deciding the write path. Public EventKit writes are stable and do not need `--private`. Private ReminderKit writes are unsupported, explicit, and required for Reminders-only metadata that EventKit cannot save.

| User intent | Command path | Private? | Verify with |
| --- | --- | --- | --- |
| Read due items, lists, groups, reminders, tags, sections, subtasks | `today`, `upcoming`, `overdue`, `lists`, `groups`, `group-info`, `show`, `search`, `info`, `tags`, `sections`, `subtasks` | No | same command with `--json` |
| Create/edit ordinary reminder fields | `add`, `edit`, `done`, `undone`, `delete` | No | `info <id> --json` or `show <list> --json` |
| Due date, priority, notes, recurrence, EventKit alarm | `add` or `edit` with `-d`, `-p`, `-n`, `--recurrence`, `--alarm` | No | `info <id> --json`; recurrence appears as `recurrence` |
| Move an existing reminder to another list | `edit <id> -l LIST` or `edit <id> --list-id ID` | No | Use the returned `id`; clone-delete fallback may return a new ID plus `oldId` |
| Reorder a reminder without changing its base list | `reminder-move <id> --before/--after <id>`; add `--smart-list NAME` for custom smart-list order | Yes | Command re-reads the stored identifier order and returns `verified: true`; for ordinary lists, `show <list> --json` reflects it |
| Set or clear the real flagged state | `flag <id>`, `unflag <id>`, `add -f/--flag` (AppleScript; needs Automation access) | No | `info <id> --json` under `flagged` |
| Synced rich URL, real tags, section assignment, shared-list assignment, subtask, image, real flag, urgent, Early Reminder, location alarm | `add --private` or `edit --private` | Yes | `info <id> --json`; UI/device check when sync matters |
| Replace/remove synced reminder tags | `edit --private --set-tags`, `edit --private --clear-tags`, `edit --private --remove-tag` | Yes | `info <id> --json` |
| Section create/rename/delete | `section-create`, `section-rename`, `section-delete` | Yes | `sections --json` or `show <list> --json` |
| List appearance, Groceries metadata, regular-list or custom-smart-list pin state | `list-create --private`, `list-edit --private`, `list-pin --private`, `list-unpin --private` | Yes | `lists --json` for list color/badge/Groceries/pin state, `smart-lists --json` for smart-list appearance/pin state |
| List group create/rename/membership/order/delete | `group-create`, `group-edit`, `list-create --private --group`, `group-delete` | Yes | `group-info <group> --json`, `groups --json`, `lists --json`, and `show <group-or-child-list> --json` |
| Custom smart list create/edit/delete | `smart-list-create`, `smart-list-edit`, `smart-list-delete` | Yes | `smart-lists --json` |
| Saved Reminders templates | `templates`, `template-info`, `template-create`, `template-apply`, `template-delete` | Reads no; writes yes | `templates --json`, `template-info`, then `show <new list> --json` after apply |

## Limited EventKit Read Mode

`--via-eventkit` explicitly requests a deliberately degraded read result; it does not change capability-host routing and RemCTL never selects it automatically. In normal installed `auto` mode, the limited read runs inside the signed host. In `direct` mode, in `auto` mode when no usable host is installed, or with a custom `REMCTL_STORE_DIR`, it runs in the caller. Use it only when the command is supported and the task explicitly tolerates missing numeric IDs and private metadata.

Supported commands:

```bash
remctl show Work --via-eventkit --json
remctl search "query" --via-eventkit --json
remctl today --via-eventkit --json
remctl upcoming 7 --via-eventkit --json
```

Fallback JSON is a wrapper object with `source: "eventkit"`, `fidelity: "limited"`, `idWarning`, and `items`. Each item has `eventKitId`, not RemCTL numeric `id`. Never pass `eventKitId` to `info`, `edit`, `done`, `delete`, `link`, `open`, `subtasks`, or any command that expects a numeric ID.

Unavailable in this mode: RemCTL numeric IDs, `--list-id`, table output, sections, synced tags, private rich links, urgent state, template internals, smart-list internals, and exact ID compatibility. If the task needs any of those, fix Full Disk Access for the host process instead of using the fallback.

High-value guardrails:

- Do not use `--private` for normal recurrence or normal `--alarm`; those are EventKit features.
- Do use `--private --early-reminder` for Reminders' Early Reminder menu values; this is separate from EventKit alarms.
- `edit --private -t/--tags` is additive. Use `--set-tags`, `--clear-tags`, or repeatable `--remove-tag` only when replacing/removing the synced tag set is intended.
- Location alarms use the `edit --private --location-*` guardrail but are saved through the EventKit bridge as structured-location alarms; verify them in `info --json` under `alarms`.
- Private rich URLs require public `http` or `https` hosts; loopback, `.local`, private, link-local, multicast, reserved, and unresolved hosts fail before writing. Non-private `--url` is only a notes fallback.
- Human output strips terminal control characters from Reminders text; use JSON when exact raw values matter.
- Plain human list output may end reminder lines with trailing `🔗` (rich link) and/or `🌄` (image attachment) badges. Those badges are never in JSON — read the `attachments` and `url` JSON fields instead.
- Invalid due dates, recurrence, normal alarms, priorities, and location payloads fail before writing. `upcoming DAYS` accepts 1 through 3650 days.
- Date-only `add -d` inputs such as `today`, `tomorrow`, `2026-06-01`, `+3d`, and `next friday` create all-day reminders; explicit times create timed reminders.
- Do not verify smart-list pinning with `lists --json`; use `smart-lists --json`. Built-in smart-list pinning is host-capability-dependent and must fail before saving when the generic ReminderKit fetch is absent.
- Do not promise template link creation or editing individual saved reminders inside a template.
- Do not create multi-list aggregate smart lists with list filters; Reminders.app materializes only one included list through this write path.

## Common Commands

```bash
remctl today --json
remctl upcoming 7 --json
remctl overdue --json
remctl lists --json
remctl groups --json
remctl group-info Writing --json
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
remctl reminder-move 23880 --before 23881 --private --json
remctl reminder-move 23880 --last --smart-list "Focus" --private --json
remctl edit 23880 --recurrence monthly --json
remctl done 23880 --json
remctl done 23880 --date 2026-05-27 --json
remctl delete 23880 --force --json
remctl flag 23880 --json
remctl unflag 23880 --json
remctl link --list-id 153 --json
remctl export --list-id 153 --format json
remctl list-symbols --preview
remctl list-rename --list-id 123 --new-name "Project Archive" --json
remctl list-delete --list-id 123 --force --json
remctl group-create "Writing" --private --add-list Editorial --json
remctl group-edit "Writing" --private --new-name "Drafts" --add-list Ideas --remove-list Socials --json
remctl list-create "Ideas" --private --group Writing --json
remctl group-edit "Writing" --private --move-list Ideas --before-list Editorial --json
remctl group-delete "Drafts" --private --force --json
remctl edit 23880 --private --set-tags remctl,work --json
remctl edit 23880 --private --remove-tag stale --json
remctl section-create "Research" -l Projects --private --json
remctl section-rename "Research" --new-name "Reading" -l Projects --private --json
remctl section-delete "Reading" -l Projects --private --force --json
```

## Syntax Rules

- Use nouns for read-only inspectors: `lists`, `groups`, `group-info`, `smart-lists`, `templates`, `today`, `stats`.
- `show <list>` follows Reminders' stored manual display order in JSON, plain, and table output. `show <group>` applies each child list's order separately; still-merging rows that are absent from the ordering record remain visible afterward.
- Use verb-style commands for writes: `add`, `edit`, `reminder-move`, `delete`, plus the `section-*`, `list-*`, `group-*`, `smart-list-*`, and `template-*` write commands.
- Section-management commands use the `section-*` prefix; list-management commands use the `list-*` prefix; group writes use the `group-*` prefix; custom smart-list writes use the `smart-list-*` prefix; template writes use the `template-*` prefix.
- Use `--json` on subcommands for automation. For tabular read commands (`today`, `upcoming`, `overdue`, `flagged`, `urgent`, `lists`, `show`, and `search`), `--format json|table|plain` can be passed globally before the command or directly on the read command; export's `--format json|csv` is separate and chooses a file format.
- `export --format json|csv` chooses an export file format, not the display style.
- List targets resolve exact name first, then case-insensitive, then normalized names such as `Weekly 513` for `🗓️ Weekly 513`. If multiple lists match, RemCTL fails before writing; use `--list-id`.
- Commands that target lists consistently support exact numeric targeting where the underlying write/read path is safe: `show --list-id`, `add --list-id`, `edit --list-id`, `link --list-id`, `export --list-id`, `section-create --list-id`, `section-rename --list-id`, `section-delete --list-id`, `list-edit --list-id`, `list-pin --list-id`, `list-unpin --list-id`, `list-rename --list-id --new-name`, `list-delete --list-id`, plus smart-list `--include-list-id`. `list-pin` and `list-unpin` also accept smart-list names or `--smart-list-id`.
- If a command accepts both a list name and `--list-id`, passing both is an error.

## Multi-Account

- The Mac may have several Reminders accounts (iCloud, Exchange, on-device local). By default every command operates on the primary account only; existing single-account behavior is unchanged.
- `remctl accounts` lists connected accounts with their type. Run it first when the user mentions work/Exchange reminders or multiple accounts.
- Read commands accept `--all-accounts` (every account, grouped/labeled) and `--account NAME` (one account). `--json` adds `account`/`accountType` fields; `--format table` adds an Account column.
- Write commands (`add`, `done`, `undone`, `edit`, `delete`, `flag`, `unflag`) accept `--account NAME`. Account flags may be placed before or after the command.
- Reminder IDs (`Z_PK`) are unique only within an account. When the scope spans multiple accounts, a bare ID that exists in more than one account is rejected with a disambiguation error — pass `--account` to resolve it.
- Prefer `--account NAME` over a bare ID when acting on a non-primary account, and use `remctl accounts` plus `show <list> --account NAME --json` to find the correct ID first.
- A user can set a default scope with `remctl config accountScope all` (or an account name); `REMCTL_ACCOUNT_SCOPE` is the environment-variable equivalent.
- Non-iCloud accounts (Exchange/CalDAV) support create/edit/complete/delete (changes sync to the server) but not `flag`/`unflag` (no flag attribute) or `link`/`open` deep links. Microsoft To Do "Important (starred)" tasks sync in as **priority**, not flags — surface them with priority, and use `edit <id> -p high` rather than `flag`.
- Exchange/CalDAV sync is not instant (unlike iCloud). A write applies locally immediately but can take a minute or two to reach the server; don't re-issue the command if the server hasn't caught up yet. The local `ZEXTERNALIDENTIFIER` field is not a reliable "synced" indicator — it populates only on the return sync.

## Recurring Schedules

Recurring schedules are normal EventKit writes and do not require `--private`.

```bash
remctl add "Daily journal" --recurrence daily --json
remctl add "Weekly report" --recurrence weekly --json
remctl add "Standup" --recurrence "weekly mon,wed,fri" --alarm 15m --json
remctl add "Pay rent" --recurrence monthly --json
remctl add "Annual review" --recurrence yearly --json
remctl add "Team sync" --recurrence "monthly 4th-fri" --json
remctl add "Deep clean" --recurrence "daily x2" --json
remctl edit 23880 --recurrence "weekly mon,wed" --json
remctl edit 23880 --recurrence "monthly x2 last-fri" --json
```

Use `info --json`, `show --json`, `today --json`, or `upcoming --json` to verify recurrence readback. Accepted recurrence forms are `daily`, `weekly`, `weekly mon,wed,fri`, `monthly`, `monthly 1,15`, and `yearly`, each with an optional interval token immediately after the frequency (`daily x2`, `weekly x2 thu`, `monthly x3 15`, `monthly x2 4th-fri`, `yearly x2`; N is 1–999), plus monthly Nth-weekday rules: `monthly 4th-fri`, `monthly 1st-mon,3rd-mon`, `monthly last-fri`, and negative `monthly -1-fri` through `-5-fri` (`last-fri` is an alias of `-1-fri`). Nth-weekday rules are monthly-only, validate their ordinal suffix (`4st-fri` is rejected), and cannot be mixed with plain day-of-month numbers in one rule. Prefer `last-fri` over `5th-fri`: EventKit silently skips months with no fifth Friday. Invalid recurrence, alarm, and priority values fail before writing. Recurring reminders include a stable `recurrence` object in JSON and a repeat badge in human/table output. Week-pinned rules read back from the database as `daysOfWeekDetailed` entries carrying `weekNumber` alongside a flat `daysOfWeek`; the parallel `weekNumbers` array is the bridge shape and appears in `--via-eventkit` payloads. Human output renders them as `monthly 4th Fri`, `monthly last Fri`, or `every 2 months 4th Tue`, and occurrence-limited series end with `, 5 times` (the old `x5` suffix now means an input interval). Relative alarms such as `--alarm 15m` are EventKit alarms and verify in `info --json` under `alarms`; use `edit ID --alarm clear --json` to remove normal alarms. Early Reminders are separate private due-date delta alerts and require `--private --early-reminder`.

## Flagging

`flag` and `unflag` write the real flagged state through AppleScript (`set flagged of reminder id …`). EventKit has no public flagged API, so there is no bridge fallback and priority is never touched. The reminder is addressed at application level, so lists nested inside groups work.

```bash
remctl flag 23880 --json
remctl unflag 23880 --json
remctl add "Ship release" -l Work --flag --json
```

- Success under `--json` is `{"status": "flagged", "id": 23880, "title": "…"}` (or `"unflagged"`) on stdout, exit 0. Verify with `info <id> --json` under `flagged`.
- Failure is terminal: exit 1 with the underlying osascript error. Under `--json` the payload is `{"status": "error", "code": "applescript_flag_failed", "id": 23880, "message": "…"}` on **stderr**, stdout is empty, and the flag is unchanged. Never report a flag as set without a `flagged`/`unflagged` payload.
- Typical causes are missing Automation access for the signed RemCTL Capability Host (`-1743`) and an unresponsive Reminders.app (120s timeout). Run `remctl doctor --for-agent --json`; use `remctl onboard` to repair the host grant only when doctor reports permission trouble. When appropriate, `edit <id> --private --flagged` or `edit <id> --private --no-flagged` avoids AppleScript and writes the same state through ReminderKit.
- `add -f/--flag` never fails the create. On flag failure the reminder still exists and `--json` returns `status: "created"` plus `"warnings": ["flag_not_set: <error>"]`; check `warnings` before telling the user the new reminder is flagged. With `--private`, `add --flag` goes through ReminderKit instead and its failures surface on the `partial` path, not in `warnings`.

## Private Metadata

Use `--private` only when the user explicitly asks for private Reminders metadata or when a command needs synced web rich links, real tags, synced tag replacement/removal, sections, shared-list assignment, subtasks, image attachments, real flags, urgent state, Early Reminders, location alarms, reminder display ordering, private list appearance metadata, Groceries mode/categorization verification, regular-list or supported smart-list pinning, list group create/edit/delete, custom smart-list creation/editing/deletion, or Reminders template creation/application/deletion.

```bash
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --section "Research" --json
remctl add "Research" -l Projects --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8 --json
remctl sharees Shopping --json
remctl add "Pick up groceries" -l Shopping --private --assign Alex --json
remctl add "Prepare screenshots" -l Projects --private --image ~/Desktop/mockup.png --subtask "Export PNG" --json
remctl add "Leave now" -l Work --private --urgent --json
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m --json
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}' --json
remctl edit 23880 --private --url "https://example.com" -t remctl --json
remctl edit 23880 --private --set-tags remctl,work --json
remctl edit 23880 --private --clear-tags --json
remctl edit 23880 --private --remove-tag stale --json
remctl edit 23880 --private --section "Research" --subtask "Follow up" --json
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8 --json
remctl section-create "Research" -l Projects --private --json
remctl section-rename "Research" --new-name "Reading" -l Projects --private --json
remctl section-delete "Reading" -l Projects --private --force --json
remctl edit 23880 --private --assign Alex --json
remctl edit 23880 --private --unassign --json
remctl edit 23880 --private --subtask '{"title":"Follow up","notes":"Bring latest numbers","due":"next friday at 3pm","url":"https://example.com","tags":["work"]}' --json
remctl edit 23880 --private --flagged --urgent --json
remctl edit 23880 --private --early-reminder 1h --json
remctl edit 23880 --private --early-reminder clear --json
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200 --json
remctl list-create "Research" --color orange --private --symbol education3 --json
remctl list-create "Cold Ideas" --color cyan --private --emoji 🥶 --json
remctl list-create "Groceries" --private --groceries --grocery-locale en_US --json
remctl list-create "Ideas" --private --group Writing --json
remctl add "Milk" -l Groceries --private --grocery --json
remctl edit 23880 --private --grocery --json
remctl list-edit "Shopping" --private --standard --json
remctl list-edit Projects --private --color '#FF8D28' --symbol education3 --json
remctl list-edit --list-id 144 --private --emoji 📌 --json
remctl list-pin "Project X" --private --json
remctl list-pin "Flagged" --private --json
remctl list-unpin --list-id 144 --private --json
remctl list-unpin --smart-list-id 4 --private --json
remctl group-create "Writing" --private --add-list Editorial --json
remctl group-edit "Writing" --private --add-list Ideas --remove-list Socials --json
remctl group-edit "Writing" --private --move-list Ideas --last --json
remctl group-delete "Writing" --private --force --json
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

- `--private --url` creates a synced web rich link and must resolve to a public `http` or `https` host. Without `--private`, `--url` is appended to notes.
- `--private -t/--tags` creates real synced tags additively. On `add` without `--private`, tags are inline title hashtags. On `edit`, tags require `--private`.
- `edit --private --set-tags`, `--clear-tags`, and repeatable `--remove-tag` rewrite the synced tag set. They cannot be combined with additive `-t/--tags` or with each other.
- `edit -l/--list` and `edit --list-id` use the normal EventKit bridge first; they do not require `--private`. If a pure move is rejected by a list/container boundary, RemCTL can use a verified ReminderKit clone-delete fallback. In JSON, treat `id` as the current reminder ID; `oldId` means the original was intentionally deleted.
- `reminder-move ID --before/--after OTHER --private` reorders within one ordinary list. Add `--smart-list NAME` or `--smart-list-id ID` for an unsectioned, manually ordered custom smart list. Sectioned custom smart lists are refused before saving. Trust success only when JSON returns `verified: true`; for ordinary lists, verify the visible sequence with `show <list> --json`.
- `--section` resolves by name; if duplicates exist in the same list, RemCTL uses the single non-empty match when possible. Use `--section-id` for exact assignment.
- `--assign` resolves a shared-list user by display name, first/last name, email/phone address, numeric sharee ID, object UUID, or `me`. Names are acceptable when unique; agents should inspect `remctl sharees LIST --json` first and prefer the returned `address`, `id`, or `objectUUID` when ambiguity matters. Use `--unassign` to clear the current assignment and verify with `remctl info ID --json` under `assignment`.
- `--early-reminder` writes Reminders' private Early Reminder due-date delta alert. It accepts `15m`, `1h`, `2d`, `1w`, `1mo`, or `clear`; non-clear values require a due date and must be verified with `remctl info ID --json`.
- `--location-title` with `--latitude` and `--longitude` requires `--private` as a guardrail, but RemCTL persists it through EventKit structured-location alarms because the private ReminderKit alarm mutation does not materialize reliably on current macOS.
- `--subtask` accepts either a plain child title or a JSON object with child metadata: `title`, `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location fields. Rich subtask URLs follow the same public-host rule as parent private URLs.
- `--section`, `--new-section`, `--set-tags`, `--clear-tags`, `--remove-tag`, `--assign`, `--unassign`, `--subtask`, `--image`, `--flagged`, `--urgent`, `--early-reminder`, and location alarm fields require `--private` and should fail before writing if omitted.
- `section-create`, `section-rename`, and `section-delete` require `--private`, refuse duplicate section names on create/rename, and should be verified with `sections --json` or `show <list> --json`.
- Rich-link and image attachment edits are additive. RemCTL can add synced rich links and images; it does not remove or replace existing rich links/images.
- `add -f/--flag` sets the real flagged state via AppleScript after the create (no `--private` needed); if the flag-set fails the add still succeeds, with the osascript error in the stderr warning and a `warnings: ["flag_not_set: …"]` array in the JSON payload. `add --private` sets the real flag through the private path instead. See Flagging for `flag`/`unflag`.
- `list-symbols` prints the 71 official Reminders emblem names; its terminal glyph column is only an approximation. Use `list-symbols --preview` to open a native-asset HTML contact sheet with interactive official color swatches, or `list-symbols --html PATH` to write one. `list-create --color NAME` uses public EventKit for normal colors. `list-create --private`, `list-edit --private`, `smart-list-create --private`, and `smart-list-edit --private` can write exact `#RRGGBB` colors, official list symbols, and emoji badges; verify those via `color`, `badge`, and `badgeEmblem` in `lists --json` or `smart-lists --json`. `list-create --private --groceries`, `list-edit --private --groceries`, and `list-edit --private --standard` write Reminders' private Groceries list metadata and locale. `list-pin` and `list-unpin` require `--private`; regular lists and custom smart lists use stable fetch paths, while built-in smart lists require the host's generic ReminderKit fetch. Reminders' picker icons use private emblem names such as `education3`; `--symbol` only accepts official names because arbitrary SF Symbol strings render as the default icon in Reminders. Use `--emoji` for custom standard emoji badges.
- List groups are visible through `groups`, `group-info`, `lists --json`, and `show <group>`. `groups` and `group-info` include active/completed/total counts for child lists. `show <group> --format table` prints child-list and section tables; `show <group> --completed --format table` uses completion timestamps rather than due status. `group-create`, `list-create --private --group`, `group-edit`, and `group-delete` require `--private`; they move list containers only, not reminders. `group-edit --move-list LIST --before-list SIBLING`, `--after-list`, `--first`, or `--last` reorders child lists. `group-delete` detaches child lists to the top level before deleting the empty group.
- Groceries lists are visible in `lists --json` as `listType: "groceries"`, `isGroceries: true`, and `grocery.locale`; human list headings show `🥕`, and known Groceries section headings get matching category emoji. `show --json` includes `sectionEmoji` for known Groceries categories. Use `add --private --grocery` or `edit --private --grocery` only against detected Groceries lists. RemCTL first verifies Reminders' automatic grocery sorting and reports `source: "reminders_auto"` when the item is already sectioned; it falls back to the private categorizer only for unsectioned items.
- Grocery fallback dispatch is host-aware. Tahoe preserves `categorizeGroceryItemsWithReminderIDs:` on the grocery context with UUIDs; Golden Gate uses `autoCategorizeRemindersWithReminderIDs:` on the list change with `REMObjectID` values. Do not collapse these into a selector-name-only branch or pass `REMObjectID` values to Tahoe—the tested alternative tombstoned its disposable objects.
- `smart-lists` is read-only and safe. `smart-list-create`, `smart-list-edit`, and `smart-list-delete` use unsupported private ReminderKit APIs and require `--private`; filter writes support the Reminders.app filters that currently materialize through this write path.
- Verify smart-list pinning with `smart-lists --json`, not `lists --json`. For a custom target, capture `objectUUID` and `filterJSON`, pin it, require `pinned: true` with a positive `pinnedDate`, then unpin it and require `pinned: false` with no positive pin date while identity and filter stay unchanged. On macOS 26, successful smart-list pinning can leave `ZISPINNEDBYCURRENTUSER` empty while updating `ZPINNEDDATE`; RemCTL reports `pinned: true` from a positive smart-list `pinnedDate`. Tested Tahoe 26.2 and Golden Gate 27.0 builds do not expose the generic fetch for built-in smart lists, so that target returns an unsupported-capability error before saving; custom smart-list pinning remains supported.
- `templates` and `template-info` are read-only and safe. They report saved Reminders templates, saved reminders, sections, and any existing public template links. Existing iCloud links are read-only metadata; RemCTL does not create sharing links.
- `template-create`, `template-apply`, and `template-delete` use unsupported private ReminderKit APIs and require `--private`. They are whole-list operations only: RemCTL does not append individual reminders to existing templates, copy selected reminders into templates, or strip subtasks or due dates while saving. Verify template writes with `remctl templates --json` or `remctl template-info`; verify applied templates with `remctl show <new list> --json`.
- Generic file/PDF attachments are rejected because Reminders does not reliably show them.
- Verify private reminder writes with `remctl info <numeric-id> --json`.
- Verify list group writes with `remctl group-info <group> --json`, `remctl groups --json`, `remctl lists --json`, and `remctl show <group-or-child-list> --json`.
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

`--filter-json` is an advanced escape hatch for raw official filter JSON or `@path`; unknown or unsupported filter shapes — and payloads with invalid date strings — are rejected before writing. Malformed stored blobs decode to an error entry rather than crashing `smart-lists`. `smart-list-edit` and `smart-list-delete` target custom smart lists by exact name or `--smart-list-id` and never match built-in smart lists. On `smart-list-edit`, `--match`/`--tag-match` count as changes only alongside filter options; a match-flag-only edit errors and explains they must accompany filter options.

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

- For agents, start with the resolved installed command: `remctl doctor --for-agent --json`.
- Treat `access.effective` as authoritative. Normal readiness requires `route: "capabilityHost"`, `ready: true`, `capabilityHost.fullReady: true`, `capabilityHost.protocolVersion: 2`, `capabilityHost.privateProtocol.compatible: true`, and `fullDiskAccess`, `reminders`, and `automation` all `authorized` under `capabilityHost.permissions`.
- `access.direct.ready` describes only the caller. It may be false while Hermes or another agent has full effective access through the host. `capabilityHost.ready` means the host has Full Disk Access; it is not the full-readiness result.
- For private-API drift audits, send `{"action":"capabilities"}` to the installed `remctl-private`. It is read-only: it reports the host OS, watched smart-list/grocery selectors and encodings, and `saveCalled: false`. Do not infer write safety from capability presence alone; run the disposable write/readback/cleanup matrix on each supported macOS version.
- Reinstall when the capability-host protocol is not version 2 or `capabilityHost.privateProtocol.compatible` is false. Do not infer installed-host compatibility from the direct `private_helper` check.
- Do not run `doctor` before every ordinary task once effective access is known-good; it is a setup and permission diagnostic, not a per-write verification step.
- For writes, verify against live Reminders data after the command succeeds.
- `remctl search QUERY --completed --json` includes completed reminders and searches both titles and notes.
- `remctl add --json` returns `numericId` when direct DB reads can resolve the new reminder. Use that for `remctl info <numericId> --json`. If `numericId` is absent, resolve the UUID-like `id` with `remctl show <list> --json` by matching the created title.
- `remctl edit ID -l LIST --json` can return the same `id` after an EventKit move or a new `id` plus `oldId` after a verified clone-delete fallback. Continue with the returned `id`, not the original, when `oldId` is present.
- Prefer deterministic due-date strings. If the user says "today at 3pm", either pass `today at 3pm` or normalize it to `YYYY-MM-DD HH:MM` in the user's timezone before calling `remctl`; do not invent broader natural-language phrases.
- `add` and `edit` are atomic for due dates: if `-d/--due` is present and cannot be parsed, RemCTL exits before writing. With `--json`, parse failures are structured `invalid_due_date` errors on stderr with accepted examples. Retry with a corrected date instead of creating first and patching later.
- Accepted dependency-free due-date forms include `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, `today at 3pm`, `tomorrow 09:30`, `tonight at 11`, `Friday at 15:00`, `next friday at 3pm`, `+3d`, `eod`, and `eow`.
- `remctl done ID --date WHEN --json` records an explicit completion date or corrects an already-completed reminder; `WHEN` must be absolute `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. Recurring reminders reject `--date`; use plain `done` to advance the series.
- `dueDate` in JSON is the actual Reminders due date from `ZDUEDATE`. If Reminders stores a separate UI/alert display date, including all-day display dates, RemCTL reports it separately as `displayDate`.
- For ordinary rescheduling, use `remctl edit ID -d "YYYY-MM-DD HH:MM" --json` first. When a reminder has a single absolute alarm/display time equal to the old due time, RemCTL carries that alarm forward so Reminders.app does not keep showing the old time. `edit ID -d clear --json` also removes a single matching absolute alarm/display time so the item does not stay visible under the old time.
- When debugging due-date or alarm mismatches, compare `dueDate`, `displayDate`, and `alarms` before assuming the CLI or UI is wrong.

Fast create path for agents:

```bash
remctl add "Title" -l Projects --private --section "Section" -d "YYYY-MM-DD HH:MM" --url "https://example.com" --json
remctl info <numericId> --json
```

`add --private` validates section/assignee/URL inputs before creating the reminder. If a private step still fails after creation, JSON output is `{"status": "partial", "id", "numericId", "failed", "error"}` (text mode: `Created reminder #N but failed to apply <action>; re-run edit to finish. Do NOT re-run add (would duplicate).`). On `partial`, re-run `edit` to finish the metadata; never re-run `add`.

`info --json` includes section, actual due date, optional display/alert date, tags, subtasks, parent and subtask attachments, EventKit alarms, location alarms, Early Reminders, deep link, and private rich-link `url` when present. List-command JSON (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`) also includes `attachments` for parent reminders; the key is omitted when a reminder has none. Each attachment entry is `{filename, type, path, resolved, uti, width, height}`. In hosted output, `resolved: true` and `path` mean the signed host resolved and sha512-verified the file; they do not prove the caller can open that protected group-container path. Do not grant Hermes, Codex, Python, or another caller Full Disk Access to compensate. Treat the entry as verified metadata unless RemCTL delivers or renders its contents through a supported command. `path: null` with `resolved: false` means a legacy attachment that is not downloaded on this Mac; treat it as unavailable, not as an error. Avoid raw SQLite checks unless the CLI output lacks a field you need.

## Permissions

Run onboarding after a first install. For an existing installation, run it only when `doctor --for-agent --json` reports a host permission problem:

```bash
remctl onboard
```

The signed RemCTL Capability Host owns Reminders, Automation, and Full Disk Access for normal `auto` execution. `onboard` checks and requests the host's Reminders and Automation grants. macOS has no Full Disk Access prompt, so onboarding opens System Settings and presents the exact host app path when that grant is missing. If that pane must be reopened, run `remctl permissions full-disk-access`. Do not run the helper unconditionally.

Pause while the user adds only the exact `capabilityHost.app.path` reported by doctor in Full Disk Access. Do not add Hermes, Codex, Terminal, Python, or every caller. `flag`, `unflag`, and non-private `add --flag` require the host's Automation grant; they do not silently degrade.

Restart the LaunchAgent only after changing Full Disk Access. Whether or not a change was needed, run doctor after onboarding to read back effective readiness:

```bash
# Only after changing Full Disk Access:
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
# Always after onboarding:
remctl doctor --for-agent --json
```

If doctor reports that Reminders or Automation was denied, enable RemCTL Capability Host in the matching System Settings privacy pane and rerun `onboard`. After the running persistent host has observed a definitive Automation state (`authorized`, `denied`, or `notDetermined`), transient status outages retain that last state. A cold or restarted host without a definitive cached state may report `targetNotRunning` or `unknown`, with `fullReady: false`, until verification succeeds. Do not require Reminders.app to remain open between commands.

Per-caller macOS permission scope matters only during explicit `REMCTL_CAPABILITY_HOST=direct` diagnostics or custom-store work. A blocked direct result is not a reason to route through Terminal or AppleScript when effective host access is ready. `doctor` also reports `completion_fpath` when an installed zsh completion file does not appear in exported `FPATH` or the usual zsh startup files.
