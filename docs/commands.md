# Command Guide

`remctl --help` prints the full reference. `remctl <command> --help` prints one command. Every command below is also available through the MCP server's `run` tool with the same arguments; see [mcp.md](mcp.md).

## Reading

```bash
remctl today                  # due today plus overdue
remctl today --no-overdue
remctl upcoming               # today plus 7 days
remctl upcoming 14
remctl overdue
remctl flagged
remctl urgent
remctl lists
remctl groups
remctl group-info "Writing"
remctl smart-lists
remctl templates
remctl template-info "Rome: Things To See"
remctl show Shopping          # one list, in Reminders' display order
remctl show --list-id 153
remctl show Work --completed
remctl show Family -v
remctl search "milk"          # titles and notes, active reminders
remctl search "milk" --completed
remctl info 23880             # everything about one reminder
remctl subtasks 23880
remctl sections
remctl tags
remctl sharees Shopping       # people you can assign to in a shared list
remctl stats
```

`show <list>` follows the manual order stored by Reminders. `show <group>` reads every child list and applies each list's own order. A reminder that has not entered the ordering record yet is shown after the ordered ones.

`upcoming DAYS` accepts 1 to 3650. The window starts today: `upcoming 1` is today and tomorrow.

## Output formats

```bash
remctl today                  # human output
remctl today --json           # machine output
remctl --format table upcoming 14
remctl show Work --format table
NO_COLOR=1 remctl today
```

Human output shows `#ID` for each reminder, colored with its list color when RemCTL can read it. Marks: `!!!`, `!!`, `!` for priority; `⚑` for flagged; `⏰` for urgent (macOS 26); `↻ weekly Mon, Wed` for recurrence; `🥕` for Groceries lists; `🔗` at the end of a line when the reminder has a rich link and `🌄` when it has an image attachment. Human output strips terminal control characters from Reminders text.

`--format table` works on `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `lists`, `groups`, `show`, and `search`, before or after the command name. `export --format json|csv` is different: it picks a file format.

JSON keeps the raw values and adds fields human output cannot show:

```json
{
  "id": 23880,
  "title": "Standup",
  "list": "Work",
  "completed": false,
  "flagged": false,
  "urgent": false,
  "priority": "none",
  "dueDate": "2026-05-05T09:00:00",
  "displayDate": "2026-05-05T08:45:00",
  "allDay": false,
  "alarms": [{"type": "relative", "relativeOffset": -900, "relativeOffsetMinutes": -15, "label": "15 minutes before due date"}],
  "earlyReminder": {"unit": "minutes", "count": -15, "value": 15, "direction": "before", "label": "15 minutes before"},
  "recurrence": {"frequency": "weekly", "interval": 1, "daysOfWeek": [2, 4, 6]},
  "attachments": [{"filename": "mockup.png", "type": "image", "path": "/Users/you/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/Account-ABCD/Attachments/<sha512>.png", "resolved": true, "uti": "public.png", "width": 1200, "height": 800}],
  "deepLink": "x-apple-reminderkit://REMCDReminder/…"
}
```

- `id` is RemCTL's numeric id. Pass it to `info`, `edit`, `done`, `undone`, `delete`, `link`, `open`, and `subtasks`.
- `dueDate` is the real due date. `displayDate` appears when Reminders stores a separate display or alert date, for example an alarm 15 minutes before. Do not treat `displayDate` as the due date.
- `alarms` lists EventKit alarms (`relative`, `absolute`) and location alarms (`type: "location"` with a `location` object).
- `earlyReminder` is Reminders' Early Reminder, a private field separate from alarms.
- `recurrence` decodes the stored rule. Weekdays pinned to a week of the month appear as `daysOfWeekDetailed` entries with `weekNumber`.
- `attachments` appears only when a reminder has attachments. `path` is the file inside Reminders' protected container as verified by the host, or `null` with `resolved: false` when the file was never downloaded to this Mac. The path is information, not a promise that your process can open the file.
- `info --json` adds notes, section, tags, subtasks with their own attachments, the shared-list `assignment`, and the private rich-link `url`.

### Limited reads through EventKit

`show`, `search`, `today`, and `upcoming` accept `--via-eventkit`. This reads through EventKit instead of the database and does not need Full Disk Access. RemCTL never chooses it on its own.

```bash
remctl today --via-eventkit --json
```

The JSON is a wrapper: `{"source": "eventkit", "fidelity": "limited", "idWarning": "…", "items": [...]}`. Items have `eventKitId`, which is an EventKit identifier, not a RemCTL numeric id. Never pass it to a numeric-id command. This mode has no sections, tags, rich links, urgent state, list ids, table output, smart-list or template internals. When no list is named, it reads iCloud reminders only.

## Creating

```bash
remctl add "Buy milk"
remctl add "Review PR" -l Work
remctl add "Write column" -l "Weekly 513"      # resolves "🗓️ Weekly 513" when unambiguous
remctl add "Write column" --list-id 156
remctl add "Call dentist" -d tomorrow           # all-day
remctl add "Team meeting" -d "next monday at 3pm"
remctl add "Deploy" -d +3d -p high
remctl add "Pay rent" -d 2026-06-01 -f          # flagged
remctl add "Check app" --url https://example.com
remctl add "Standup" -d "tomorrow 09:30" --recurrence "weekly mon,wed,fri" --alarm 15m
remctl add -- "-Title that starts with a dash"
```

Options: `-l/--list`, `--list-id`, `-n/--notes`, `-d/--due`, `-p/--priority high|medium|low`, `-f/--flag`, `-t/--tags`, `--url`, `--recurrence`, `--alarm`, and the `--private` options described under [Private metadata](#private-metadata).

Without `--private`, `--url` is appended to the notes and `-t/--tags` adds `#hashtags` to the title. With `--private` they become a synced rich link and synced tags.

`-f/--flag` sets the flag through Reminders automation after the reminder is created. If that step fails, the reminder still exists; the command prints a warning and `--json` adds `"warnings": ["flag_not_set: …"]`.

With `--json`, `add` prints `{"status": "created", "id": "<uuid>", "numericId": 32308, "title": "…"}`. Use `numericId` for follow-up commands. `resolvedList` appears when the list name was matched loosely.

### Due dates

| Input | Meaning |
| --- | --- |
| `2026-04-15` | All-day |
| `2026-04-15 14:00` | Timed |
| `today`, `tomorrow`, `+3d`, `+1w`, `next friday`, `eow` | All-day |
| `+2h`, `eod`, `tonight at 11`, `Friday at 15:00`, `next monday at 3pm`, `tomorrow 09:30` | Timed |
| `clear` (edit only) | Remove the due date; `--alarm clear` removes alarms. A repeating reminder must keep its due date, so `edit` refuses with `repeating_reminder_requires_due_date` and changes nothing. |

Date-only input creates an all-day reminder; input with a time creates a timed reminder. If `-d` cannot be parsed, `add` and `edit` stop before writing. With `--json`, the error is `{"status": "error", "code": "invalid_due_date", …}` on stderr with accepted examples. Natural-language phrases beyond this table need the optional `parsedatetime` package; prefer `YYYY-MM-DD HH:MM` in scripts.

### Recurrence

```bash
remctl add "Daily journal" -d tomorrow --recurrence daily
remctl add "Weekly report" -d tomorrow --recurrence weekly
remctl add "Pay rent" -d tomorrow --recurrence monthly
remctl add "Annual review" -d tomorrow --recurrence yearly
remctl add "Standup" -d "tomorrow 09:30" --recurrence "weekly mon,wed,fri"
remctl add "Invoices" -d tomorrow --recurrence "monthly 1,15"
remctl add "Sprint review" -d tomorrow --recurrence "monthly 4th-fri"
remctl add "Payday check" -d tomorrow --recurrence "monthly last-fri"
remctl add "Deep clean" -d tomorrow --recurrence "daily x2"
remctl edit 23880 --recurrence "monthly x2 last-fri"
```

A repeating reminder needs a due date: Reminders will not save one without it. Rules: a frequency (`daily`, `weekly`, `monthly`, `yearly`); an optional interval `xN` (1 to 999) right after it; for weekly, a weekday list; for monthly, day numbers or ordinal weekdays (`1st-mon`, `3rd-mon`, `4th-fri`, `last-fri`, `-1-fri` down to `-5-fri`). Ordinal weekdays cannot be mixed with day numbers. The suffix must match the number (`4st-fri` is rejected). Prefer `last-fri` to `5th-fri`: EventKit skips months without a fifth Friday. Recurrence and normal alarms are EventKit features and never need `--private`.

Read-back: JSON has a `recurrence` object; human output shows `↻ monthly 4th Fri` or `↻ every 2 months 4th Tue`. A series limited to N occurrences shows `, 5 times`.

### Alarms

`--alarm 15m`, `1h`, `1d`, or an ISO date creates an EventKit alarm relative to or at a time. A relative alarm counts back from the due date, so it needs one: without a due date, `add` and `edit` stop with `relative_alarm_requires_due_date`. An ISO date works without a due date. `edit ID --alarm clear` removes normal alarms. Alarms appear in `info --json` under `alarms`.

Early Reminders are different: they are Reminders' private "early reminder" setting and require `--private --early-reminder 15m|1h|2d|1w|1mo|clear`. Setting one needs a due date.

Location alarms use `edit ID --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 [--radius 200] [--proximity arriving|leaving]`. The `--private` guard is deliberate, but the write itself goes through EventKit. One location alarm per reminder; a new one replaces the old one. `--address` is not supported.

## Editing

```bash
remctl edit 23880 --title "New title"
remctl edit 23880 -n "Notes"
remctl edit 23880 -d "next friday" -p medium
remctl edit 23880 -d clear
remctl edit 23880 -l Work                   # move to another list
remctl edit 23880 --list-id 156
remctl edit 23880 --recurrence "weekly mon,wed"
remctl edit 23880 --alarm 1h
remctl done 23880
remctl done 23880 --date 2026-05-27
remctl done 23880 --date "2026-05-27 09:30"
remctl undone 23880
remctl flag 23880
remctl unflag 23880
remctl delete 23880                         # asks first
remctl delete 23880 --force                 # required with --json or without a terminal
```

`edit` needs at least one change. With `--json` it prints `{"status": "updated", "id": 23880, "title": "…"}`.

Rescheduling: when a reminder has one absolute alarm at the old due time, `edit -d` moves that alarm too, so the time shown in Reminders follows the due date. `edit -d clear` removes such an alarm as well. Other alarms are left alone.

Moving between lists: `edit -l` and `edit --list-id` use EventKit. Some moves are rejected by EventKit, for example a parent reminder with subtasks or a move across a shared-list boundary. For a pure move, RemCTL then clones the reminder into the destination through ReminderKit, verifies the clone and its subtask count, and deletes the original. The JSON then has `"method": "clone-delete"`, `oldId`, the new `id`, and `subtasksMoved`. Continue with the new `id`. Move first; apply other edits afterwards.

`done --date WHEN` records a specific completion time, also on an already completed reminder. `WHEN` must be `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. Recurring reminders reject `--date`; plain `done` advances the series.

Every destructive command (`delete`, `list-delete`, `section-delete`, `group-delete`, `smart-list-delete`, `template-delete`) asks for confirmation on a terminal. With `--json`, or when stdin is not a terminal, it requires `--force`. Without it, nothing is written, stdout stays empty, stderr gets `{"status": "error", "code": "confirmation_required", …}`, and the exit code is 1.

### Flags

`flag` and `unflag` write the real flag through Reminders automation (AppleScript), the only API that can set it. Priority is not touched. Reminders nested in groups work.

- Success under `--json`: `{"status": "flagged", "id": 23880, "title": "…"}` or `"unflagged"`.
- Failure: exit 1 and, under `--json`, `{"status": "error", "code": "applescript_flag_failed", "id": 23880, "message": "…"}` on stderr. The flag is unchanged. Usual causes: the host lacks Automation access (error -1743), or Reminders did not respond within 120 seconds.
- `edit ID --private --flagged` and `--no-flagged` write the same state through ReminderKit without automation.

## Reminder ordering

```bash
remctl reminder-move 23880 --before 23881 --private
remctl reminder-move 23880 --after 23881 --private
remctl reminder-move 23880 --first --private
remctl reminder-move 23880 --last --private --json
remctl reminder-move 23880 --before 23881 --smart-list "Focus" --private
remctl reminder-move 23880 --last --smart-list-id 170 --private
```

`reminder-move` changes the display position without changing the list. It is a private write and requires `--private`. Without a smart-list target, the reminder and the anchor must be in the same list. With `--smart-list` or `--smart-list-id`, RemCTL reorders an unsectioned custom smart list, whose reminders may come from different lists. Sectioned smart lists are refused. The command reads the order back and reports `verified: true`; `show <list> --json` shows the new order.

## Lists

```bash
remctl lists
remctl lists --json
remctl list-symbols                        # the 71 official Reminders icon names
remctl list-symbols --preview              # HTML contact sheet with the real icons
remctl list-create "Project X" --color blue
remctl list-create "Project X" --color orange --private --symbol education3
remctl list-create "Cold Ideas" --color cyan --private --emoji 🥶
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl list-create "Ideas" --private --group Writing
remctl list-edit "Project X" --private --color '#FF8D28' --symbol education3
remctl list-edit --list-id 144 --private --emoji 📌
remctl list-edit "Shopping" --private --groceries --grocery-locale it_IT
remctl list-edit "Shopping" --private --standard
remctl list-pin "Project X" --private
remctl list-unpin --list-id 144 --private
remctl list-rename "Project X" "Project Y"
remctl list-rename --list-id 144 --new-name "Project Y"
remctl list-delete "Project Y" --force
```

`list-create --color NAME` uses EventKit and accepts Reminders color names (`red`, `orange`, `yellow`, `green`, `blue`, `purple`, `brown`, `gray`, `cyan`). Exact `#RRGGBB` colors, official icon names, emoji badges, Groceries mode, and pin state are private metadata and need `--private`.

`--symbol` accepts only the official icon names from `list-symbols`, because Reminders draws unknown names as the default icon. Use `--emoji` for any emoji.

`list-pin` and `list-unpin` also accept smart lists by name or `--smart-list-id`. Pinning built-in smart lists works only on macOS versions whose ReminderKit exposes the generic smart-list fetch; other versions fail before saving.

`lists --json` includes `listType`, `isGroup`, `isGroceries`, `color`, `badge`, `pinned`, group `children`, and each child list's `group`.

### List names and ids

A list can be named positionally or with `-l/--list`, or targeted exactly with `--list-id`. Names resolve in three passes: exact, case-insensitive, then normalized (ignoring decorative punctuation and emoji, so `Weekly 513` matches `🗓️ Weekly 513`). If more than one list matches, the command stops and prints the candidate ids. Passing both a name and `--list-id` is an error. This applies to `show`, `add`, `edit`, `link`, `export`, the `section-*` commands, `list-edit`, `list-pin`, `list-unpin`, `list-rename`, `list-delete`, and the smart-list `--include-list-id` filter.

Write commands that need a real list reject a group and name the child lists you can target instead.

## Groceries lists

Reminders stores Groceries lists as ordinary lists with private grocery metadata. `lists --json` reports `listType: "groceries"`, `isGroceries`, and `grocery.locale`. Human output marks them with `🥕` and gives known sections their category emoji (`🥛 Dairy, Eggs & Cheese`, `🥬 Produce`). `show --json` includes `sectionEmoji`.

```bash
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl list-edit "Shopping" --private --groceries --grocery-locale it_IT
remctl list-edit "Shopping" --private --standard
remctl add "Milk" -l Groceries --private --grocery
remctl edit 23880 --private --grocery
```

`add --private --grocery` creates the reminder, waits for Reminders' own sorter, checks the section, and only uses the private categorizer when the item was not sorted. The result reports `source: "reminders_auto"` in the automatic case.

## Sections

```bash
remctl sections
remctl section-create "Research" -l Projects --private
remctl section-rename "Research" --new-name "Reading" -l Projects --private
remctl section-delete "Reading" -l Projects --private --force
remctl add "Paper" -l Projects --private --section Research
remctl add "Paper" -l Projects --private --new-section Reading
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8
```

Section commands are private writes. Create and rename refuse duplicate names in the same list. `--section` resolves by name; if a list has two sections with the same name, RemCTL uses the one that is not empty when exactly one qualifies, otherwise ask for `--section-id`. Verify with `sections --json` or `show <list> --json`.

## List groups

Groups are containers for lists, not for reminders.

```bash
remctl groups
remctl group-info Writing --json
remctl show Writing                         # reads every child list
remctl group-create "Writing" --private --add-list Editorial --add-list-id 137
remctl list-create "Ideas" --private --group Writing
remctl group-edit "Writing" --private --new-name "Drafts"
remctl group-edit "Writing" --private --add-list Ideas --remove-list Socials
remctl group-edit "Writing" --private --move-list Ideas --before-list Editorial
remctl group-edit "Writing" --private --move-list-id 137 --last
remctl group-delete "Drafts" --private --force
```

`groups` and `group-info` report active, completed, and total reminder counts per child list. `show <group> --format table` prints one table per child list; with `--completed` the date column shows completion times. Group writes are private and move only list containers; reminders stay where they are. `group-delete` detaches the child lists first, then removes the empty group. Verify with `group-info --json`, `lists --json`, or `show <group> --json`.

## Smart lists

```bash
remctl smart-lists --json
remctl smart-list-create "Flagged Review" --private --flagged
remctl smart-list-create "High Priority" --private --priority high
remctl smart-list-create "Any Tag" --private --any-tag
remctl smart-list-create "#remctl Today" --private --tags remctl --date today
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today
remctl smart-list-create "Projects Today" --private --include-list Projects --date today --date-today-include-past-due
remctl smart-list-create "Near Home" --private --location-title Home --latitude 41.9 --longitude 12.5 --radius 100 --proximity enter
remctl smart-list-create "Due Before June 1" --private --date-range 2026-05-16,2026-05-31 --color red --emoji 📆
remctl smart-list-edit "Priority or Today" --private --priority high
remctl smart-list-edit --smart-list-id 170 --private --filter-json @filter.json
remctl smart-list-delete "Flagged Review" --private --force
```

`smart-lists` is read-only. It reports built-in and custom smart lists with numeric id, `objectUUID`, type, `pinned`, `pinnedDate`, and a decoded filter summary. A filter RemCTL cannot decode becomes an `error` field instead of failing the command.

The write commands are private. They support the filters that Reminders reliably shows after this write path:

| Filter | Options |
| --- | --- |
| Any tag | `--any-tag` |
| Selected tags | `--tags a,b`, optional `--tag-match all|any` |
| Date | `--date any|today`, `--date-today-include-past-due`, `--date-on`, `--date-before`, `--date-after`, `--date-range START,END` |
| Time of day | `--time morning|afternoon|evening|night` |
| Priority | `--priority high`, or a comma list for "any of" |
| Flag | `--flagged` |
| Vehicle | `--vehicle connected` |
| Location | `--location-title`, `--latitude`, `--longitude`, `--radius`, `--proximity enter|leave` |
| One list | `--include-list NAME` or `--include-list-id ID` |
| Combination | `--match all|any` |

Appearance: `--color`, `--symbol`, `--emoji`.

Rejected before saving, because Reminders shows zero filters for them: untagged, no date, relative date, no time, vehicle disconnected, list exclusions, and more than one included list. Do not build "all of these lists" smart lists this way.

`--filter-json` accepts raw Reminders filter JSON or `@path` for advanced cases. It is still validated; invalid date strings are rejected. `--match` and `--tag-match` only change how other filters combine, so an edit with only those flags is an error. `smart-list-edit` and `smart-list-delete` target custom smart lists by exact name or `--smart-list-id` and never touch built-in ones.

Verify with `smart-lists --json`: the target's `objectUUID`, the decoded filter, `filter.supported`, and `minimumSupportedVersion` `20220430`. Verify pin state with `pinned` and `pinnedDate` there, not with `lists --json`.

## Templates

```bash
remctl templates --json
remctl template-info "Rome: Things To See" --json
remctl template-info --template-id 2 --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-create "Archive Template" --from-list-id 144 --include-completed --private
remctl template-apply "Packing Template" --private --json
remctl template-apply --template-id 2 --private
remctl template-delete "Packing Template" --private --force
```

Templates are saved lists. `templates` and `template-info` are read-only and report ids, `objectUUID`, item and section counts, dates, badges, saved reminders, and any existing public link. The write commands are private and work on whole lists: `template-create` saves an entire list (add `--include-completed` to keep completed items), `template-apply` creates a new list from a template, and `template-delete` removes the saved template only. RemCTL does not edit single reminders inside a template and does not create or revoke iCloud template links.

Verify with `templates --json` or `template-info`; after `template-apply`, with `lists --json` and `show <new list> --json`.

## Assignment in shared lists

```bash
remctl sharees Shopping --json
remctl add "Pick up groceries" -l Shopping --private --assign Alex
remctl edit 23880 --private --assign alex@example.com
remctl edit 23880 --private --assign me
remctl edit 23880 --private --unassign
```

`--assign USER` accepts a unique name, an email or phone address, the numeric sharee `id`, the `objectUUID`, or `me`. Names work when unique; scripts should use the address or an id from `sharees --json`. Assignment requires `--private` and a shared list. Verify with `info ID --json` under `assignment`.

## Private metadata

`--private` unlocks writes that use Apple's private ReminderKit framework through the `remctl-private` helper. They never write the database directly. Apple can change them in any release; treat them as a power-user feature. [private-metadata.md](private-metadata.md) has the full list, the safety model, and verification steps.

```bash
remctl add "Research" -l Projects --private --url https://example.com -t remctl --section Research
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}'
remctl add "Leave now" -l Work --private --urgent
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m
remctl edit 23880 --private --url https://example.com -t remctl
remctl edit 23880 --private --set-tags remctl,work
remctl edit 23880 --private --clear-tags
remctl edit 23880 --private --remove-tag stale
remctl edit 23880 --private --subtask "Follow up"
remctl edit 23880 --private --image ~/Desktop/mockup.png
remctl edit 23880 --private --flagged --urgent
remctl edit 23880 --private --early-reminder clear
```

| Field | Options | Notes |
| --- | --- | --- |
| Rich link | `--url` | Public `http` or `https` host only; loopback, `.local`, and private addresses are rejected. Additive. |
| Synced tags | `-t/--tags` (add), `--set-tags` (replace), `--clear-tags`, `--remove-tag` (repeatable) | The replace and remove options cannot be combined with each other or with `-t`. |
| Section | `--section`, `--section-id`, `--new-section` | See [Sections](#sections). |
| Assignment | `--assign`, `--unassign` | Shared lists only. |
| Subtasks | `--subtask TITLE` or `--subtask '{json}'` | JSON accepts `title`, `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location fields. Additive. |
| Images | `--image PATH` (repeatable) | Additive. Other file types are rejected because Reminders does not show them. |
| Flag, urgent | `--flagged`/`--no-flagged`, `--urgent`/`--no-urgent` | |
| Early Reminder | `--early-reminder 15m|1h|2d|1w|1mo|clear` | Needs a due date. |
| Location alarm | `--location-title`, `--latitude`, `--longitude`, `--radius`, `--proximity` | Saved through EventKit. |
| Groceries | `--grocery` | Only in a Groceries list. |

Rich links and images are additive: RemCTL adds them and does not remove or replace existing ones.

If a private step fails after the reminder was created, `add --json` prints `{"status": "partial", "id": …, "numericId": …, "failed": "…", "error": "…"}`. The reminder exists. Finish it with `edit`; do not run `add` again.

## Import and export

```bash
remctl export --list Shopping --format json > shopping.json
remctl export --list-id 153 --format json > shopping.json
remctl export --format csv > all-reminders.csv
remctl import shopping.json
remctl import - --json < shopping.json
```

`import` reads a JSON array from a file, or from standard input with `-` (up to 8 MiB, not from a terminal). Supported fields: `title` (required), `list`, `notes`, `due` or `dueDate`, `priority`, `url`, `recurrence`, `alarm`, and boolean `flagged`. Other keys are ignored. It creates ordinary reminders through `add`; it does not restore completion state, tags, sections, subtasks, attachments, assignments, or private metadata. An export is therefore not a full backup.

The whole array is validated before the first write. Invalid input creates nothing and exits 1; with `--json`, field errors come back as `{"status": "error", "code": "invalid_import", "errors": [...]}` on stderr. During the import, each success is reported on stderr as `imported index=N id=ID`. The final line on stdout is one JSON object with `status` (`completed` or `partial`), `created`, `createdIds`, `errors`, and `total`. A partial import exits 1 and keeps what was created; retry only the failed indexes.

## Links

```bash
remctl link 23880              # x-apple-reminderkit deep link
remctl link -l Shopping        # every active reminder in a list
remctl link --list-id 153
remctl open 23880              # open the reminder in Reminders
remctl open                    # open the app
```

## Inline images

```bash
remctl info 847 --images
remctl show Work --images --verbose
remctl today --images --verbose --image-mode halfblock --image-width 48
```

`--images` draws image attachments in the terminal. `info` draws them whenever `--images` is passed; list commands draw them only with `--verbose`. Drawing happens only on a real terminal, never in pipes, `--json`, or table output.

| Flag | Environment | Default |
| --- | --- | --- |
| `--images` | `REMCTL_IMAGES=1` | off |
| `--image-mode kitty|iterm2|halfblock|none` | `REMCTL_IMAGE_MODE` | detected from the terminal |
| `--image-width N` | `REMCTL_IMAGE_WIDTH` | about 40% of the terminal width, 24 to 100 cells |

Detection: Kitty graphics on Ghostty, Kitty, WezTerm, and Konsole; iTerm2's protocol on iTerm2 and Blink; half-block characters on other truecolor terminals. Terminals with no usable protocol skip the image and keep the filename line. Decoding uses Pillow when installed, otherwise macOS `sips`. Files over 16 MB are listed but not drawn. `(file not downloaded on this Mac)` and `(preview unavailable)` explain a missing render. `REMCTL_IMAGES_FORCE=1` skips the terminal check and exists for tests only.

## Setup commands

```bash
remctl onboard                         # guided setup; repeat any time
remctl onboard --json                  # checks only, no questions
remctl doctor                          # health report
remctl doctor --for-agent --json       # readiness for scripts and agents
remctl permissions full-disk-access    # reopen the Full Disk Access helper
remctl setup --shell auto              # shell completion
remctl completion zsh
remctl mcp install                     # connect AI apps
remctl mcp status
REMCTL_CAPABILITY_HOST=force remctl stats --json    # prove the host route works
REMCTL_CAPABILITY_HOST=direct remctl stats --json   # bypass the host (diagnostics only)
```

`onboard` is described in [installation.md](installation.md#onboarding). `doctor --for-agent --json` reports `access.direct` and `access.effective`; `access.effective.ready` plus `capabilityHost.fullReady` is the readiness gate. `capabilityHost.privateProtocol.compatible` reports whether the sealed private helper matches the CLI; if not, run `./install.sh` again. `doctor` also warns when the zsh completion directory is not on `fpath`.

The `mcp` commands are documented in [mcp.md](mcp.md).

## Environment variables

| Variable | Effect |
| --- | --- |
| `REMCTL_CAPABILITY_HOST=auto|force|direct` | `auto` (default) uses the host when installed; `force` requires it; `direct` bypasses it and needs the caller's own permissions |
| `REMCTL_STORE_DIR` | Custom Reminders store; implies `direct`; conflicts with `force` |
| `REMCTL_BRIDGE_PATH`, `REMCTL_PRIVATE_PATH`, `REMCTL_PERMISSIONS_PATH` | Helper overrides for `direct` mode only; the host uses its sealed helpers |
| `REMCTL_CONFIG_DIR` | Config directory (default `~/.config/remctl`) |
| `REMCTL_SKIP_ONBOARD=1` | Never run onboarding automatically |
| `REMCTL_IMAGES`, `REMCTL_IMAGE_MODE`, `REMCTL_IMAGE_WIDTH` | Inline image defaults; flags win |
| `REMCTL_MCP_DEBUG=1` | Per-request trace from the MCP server on stderr |
| `NO_COLOR=1` | Plain output |
