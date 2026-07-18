# Command Guide

Run `remctl --help` for the full parser-generated reference and `remctl <command> --help` for command-specific options.

## Viewing

```bash
remctl today
remctl upcoming
remctl upcoming 14
remctl overdue
remctl flagged
remctl urgent
remctl lists
remctl groups
remctl group-info "Writing"
remctl group-create "Writing" --private --add-list Editorial
remctl group-edit "Writing" --private --new-name "Drafts" --add-list Ideas --remove-list Socials
remctl group-delete "Drafts" --private --force
remctl smart-lists
remctl templates
remctl template-info "Rome: Things To See"
remctl show Shopping
remctl show --list-id 153
remctl show Work --completed
remctl show Family -v
remctl show Work --via-eventkit
remctl search "milk"
remctl search "milk" --completed
remctl today --via-eventkit --json
remctl info 23880
remctl info 847 --images
remctl show Work --images --verbose
remctl subtasks 23880
remctl sections
remctl tags
remctl stats
```

## Flags, Priorities, Urgent, and Recurrence

```bash
remctl flagged
remctl urgent
remctl flag 23880
remctl unflag 23880
remctl add "Deploy" -d +3d -p high
remctl edit 23880 -p low
remctl edit 23880 --recurrence "weekly mon,wed"
```

RemCTL keeps these states distinct:

- priority is written with `-p high`, `-p medium`, or `-p low` and shown as `!!!`, `!!`, or `!`
- flagged reminders are shown with `⚑` and can be changed with `flag` / `unflag`
- macOS 26 urgent reminders are shown with `⏰` and listed with `urgent`
- urgent is stored in private ReminderKit metadata; power users can opt into unsupported writes with `add --private --urgent` or `edit --private --urgent`
- recurring reminders are shown with a `↻` badge and, in table output, a `Repeat` column

## Creating

```bash
remctl add "Buy milk"
remctl add "Review PR" -l Work
remctl add "Write column" -l "Weekly 513"   # safely resolves 🗓️ Weekly 513 when unambiguous
remctl add "Write column" --list-id 156
remctl add "Call dentist" -d tomorrow
remctl add "Team meeting" -d "next monday at 3pm"
remctl add "Deploy" -d +3d -p high
remctl add "Pay rent" -d "2026-06-01" -f
remctl add "Check app" --url "https://example.com"
```

Unsupported private metadata writes:

```bash
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --new-section "Research"
remctl add "Research" -l Projects --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8
remctl add "Prepare images" -l Projects --private --image ~/Desktop/mockup.png --subtask "Export final PNG"
remctl add "Leave now" -l Work --private --urgent
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}'
remctl edit 23880 --private --section "Research"
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8
remctl edit 23880 --private --set-tags remctl,work
remctl edit 23880 --private --remove-tag stale
remctl section-create "Research" -l Projects --private
remctl section-rename "Research" --new-name "Reading" -l Projects --private
remctl section-delete "Reading" -l Projects --private --force
remctl edit 23880 --private --subtask '{"title":"Follow up","notes":"Bring latest numbers","due":"next friday at 3pm","url":"https://example.com","tags":["work"]}'
remctl edit 23880 --private --flagged --urgent
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl add "Milk" -l Groceries --private --grocery
remctl smart-list-create "Flagged Review" --private --flagged
remctl smart-list-create "High Priority" --private --priority high
remctl smart-list-delete "Flagged Review" --private --force
remctl templates --json
remctl template-info "Rome: Things To See" --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-apply "Packing Template" --private --json
remctl template-delete "Packing Template" --private --force
```

`search` matches reminder titles and notes. By default it searches active reminders; add `--completed` to include completed reminders.

## CLI Syntax Rules

RemCTL uses nouns for read-only inspectors (`lists`, `groups`, `group-info`, `smart-lists`, `templates`, `today`, `stats`) and verb-style commands for writes (`add`, `edit`, `delete`, plus the `section-*`, `list-*`, `group-*`, `smart-list-*`, and `template-*` write commands). Section-management commands keep the `section-*` prefix; list-management commands keep the `list-*` prefix; group writes keep the `group-*` prefix; custom smart-list writes keep the `smart-list-*` prefix; template writes keep the `template-*` prefix.

Use `--json` on subcommands when scripting. For tabular read commands (`today`, `upcoming`, `overdue`, `flagged`, `urgent`, `lists`, `groups`, `show`, and `search`), `--format json|table|plain` can be passed globally before the command or directly on the read command, so both `remctl --format table show Work` and `remctl show Work --format table` are valid. Export keeps its own `--format json|csv` because that chooses a file format, not display style.

### Limited EventKit Read Fallback

`--via-eventkit` is a limited read-only fallback for hosts that cannot get Full Disk Access. It is never the default and is supported only by:

```bash
remctl show Work --via-eventkit
remctl search "milk" --via-eventkit
remctl today --via-eventkit --json
remctl upcoming 14 --via-eventkit --json
```

This mode uses EventKit through `remctl-bridge`, so it can return basic reminder fields without opening the Reminders SQLite database. It is not full RemCTL output. It does not support `--list-id`, table output, sections, synced tags, private rich links, urgent state, template internals, smart-list internals, or exact numeric ID compatibility. When no list target is given, these reads are scoped to iCloud reminders.

JSON output is a wrapper object, not the normal read-command array:

```json
{
  "source": "eventkit",
  "fidelity": "limited",
  "idWarning": "eventKitId is not a RemCTL numeric id and cannot be passed to info, edit, done, delete, link, open, subtasks, or any other numeric-id command.",
  "items": [
    {
      "eventKitId": "EVENTKIT-CALENDAR-ITEM-ID",
      "title": "Review PR",
      "list": "Work",
      "completed": false
    }
  ]
}
```

Treat `eventKitId` as display/readback data only. Never pass it to `info`, `edit`, `done`, `delete`, `link`, `open`, `subtasks`, or any command that expects a RemCTL numeric `id`. If an automation needs chainable IDs or private Reminders metadata, fix Full Disk Access and use the normal read path.

Date-only `add -d` inputs create all-day reminders instead of midnight timed reminders. This applies to forms such as `today`, `tomorrow`, `2026-06-01`, `+3d`, `in 2 weeks`, and `next friday`, and to natural-language date-only inputs parsed by `parsedatetime` such as `March 30` or `next week`; only inputs that resolve to a specific clock time remain timed reminders.

`upcoming DAYS` accepts 1 through 3650 days. Zero and negative ranges fail before RemCTL opens the Reminders database.

List targets are consistent across commands that can safely resolve them: pass a list name positionally or with `-l/--list`, or pass `--list-id` when an exact numeric target matters. If both a name and `--list-id` are provided, RemCTL fails before writing or exporting. This applies to `show`, `add`, `edit`, `link`, `export`, `section-create`, `section-rename`, `section-delete`, `list-edit`, `list-pin`, `list-unpin`, `list-rename`, `list-delete`, and the smart-list `--include-list-id` filter. For pinning, `list-pin` and `list-unpin` also accept smart-list names or `--smart-list-id`; if a name matches both a regular list and a smart list, RemCTL fails before writing and asks for an explicit ID.

Reminders list groups are containers for lists. Use `groups` to inspect them, or `lists --json` to get group rows with `children` plus child list rows with `group` metadata. `show <group>` reads across child lists. `group-create`, `group-edit`, and `group-delete` use private ReminderKit and require `--private`; they move list containers only, so reminders stay in their existing lists. Write commands that need a real list reject group targets before making changes and report the child lists you can target instead.

List names are resolved conservatively: exact match first, then case-insensitive match, then a normalized fallback that ignores decorative punctuation and emoji. If more than one list matches, RemCTL fails before writing and prints the candidate IDs; pass `--list-id` to target one explicitly.

`--section` resolves by name inside the target list. If duplicate section names exist, RemCTL uses the only non-empty matching section when there is exactly one. If the duplicate is still ambiguous, use `--section-id`.

`section-create`, `section-rename`, and `section-delete` manage list sections through private ReminderKit and require `--private`. Create and rename refuse duplicate names in the target list. Delete prompts unless `--force` is passed and leaves reminder movement to Reminders' normal section-delete behavior.

`edit --private -t/--tags` adds synced tags. `edit --private --set-tags`, `--clear-tags`, and repeatable `--remove-tag` rewrite the reminder's synced tag set, cannot be combined with `-t`, and cannot be combined with each other.

`--assign USER` assigns a reminder to a person in a shared list and requires `--private`. `USER` can be a unique name, email or phone address, numeric sharee ID, object UUID, or `me`; use `remctl sharees LIST --json` to inspect exact candidates. Names are fine for humans when unique, but agents should prefer email/address or IDs to avoid collisions. `--unassign` clears the current assignment.

`--subtask` accepts either a plain child title or a JSON object with child metadata. Rich subtask fields include `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location alarm fields. A subtask `due` given as a date without a time creates an all-day subtask, matching the parent's date-only behavior.

`--private` uses Apple's private ReminderKit framework through `remctl-private`. It does not write SQLite directly. Verified private writes include synced web rich links, synced tag add/replace/remove, section assignment/create/rename/delete, shared-list assignments, rich subtasks, image attachments, real flag state, urgent state, Early Reminders, location alarms, list appearance metadata, list and smart-list pin state, list group create/edit/delete, Groceries list metadata and categorization verification, custom smart-list creation/editing/deletion for verified materializing Reminders filters, and Reminders template create/apply/delete. Location alarms are guarded by `--private` but saved through the EventKit bridge as structured-location alarms because the private ReminderKit alarm mutation does not materialize reliably on current macOS. Generic file/PDF attachments are intentionally rejected because Reminders does not reliably show them even when private rows sync.

Private rich URLs require public `http` or `https` hosts. RemCTL rejects loopback, `.local`, private, link-local, multicast, reserved, and unresolved hosts before creating or editing a reminder; rich subtask URLs follow the same rule. Non-private `--url` remains a notes fallback.

`edit` covers these existing-reminder surfaces:

| Surface | Command | Private? | Notes |
| --- | --- | --- | --- |
| Title, notes, due date, priority | `edit --title`, `-n`, `-d`, `-p` | No | EventKit bridge with AppleScript fallback for ordinary fields |
| Move to another list | `edit -l LIST` or `edit --list-id ID` | No | EventKit bridge first; if a pure move is rejected by a list/container boundary, RemCTL uses a verified ReminderKit clone-delete fallback and returns a new numeric `id` plus `oldId` |
| Recurrence and normal alarms | `edit --recurrence`, `edit --alarm` | No | EventKit bridge only; verify in `info --json` |
| Notes URL fallback | `edit --url URL` | No | Appends the URL to notes, not a rich attachment |
| Rich web URL attachment and additive real tags | `edit --private --url URL -t tags` | Yes | Additive; does not remove or replace existing rich links |
| Replace or remove synced tags | `edit --private --set-tags tags`, `--clear-tags`, `--remove-tag tag` | Yes | Rewrites the complete tag set through ReminderKit; mutually exclusive with additive `-t/--tags` |
| Section assignment or creation | `edit --private --section`, `--section-id`, `--new-section` | Yes | If combined with `-l/--list`, section resolution uses the destination list |
| Section create, rename, delete | `section-create`, `section-rename`, `section-delete` | Yes | Private ReminderKit list-section management; verify with `sections --json` or `show <list> --json` |
| Shared-list assignment | `add/edit --private --assign USER`, `edit --private --unassign` | Yes | `USER` resolves against `remctl sharees LIST`; accepts unique name, email/phone, numeric sharee ID, object UUID, or `me`; agents should prefer address or ID |
| Subtasks | `edit --private --subtask ...` | Yes | Additive; rich JSON subtasks can include public and private child metadata |
| Image attachments | `edit --private --image PATH` | Yes | Additive; generic files/PDFs are rejected, and existing images are not removed/replaced |
| Real flagged, urgent, Early Reminder, location alarm, Groceries categorization | `edit --private --flagged`, `--urgent`, `--early-reminder`, location fields, `--grocery` | Yes | Private ReminderKit metadata except location alarms, which use EventKit structured-location alarms; verify with `info --json` or `show <list> --json` for Groceries sectioning |

See [private-metadata.md](private-metadata.md) for risks, guardrails, and verification notes.

Recurring reminders:

```bash
remctl add "Daily journal" --recurrence daily
remctl add "Weekly report" --recurrence weekly
remctl add "Standup" --recurrence "weekly mon,wed,fri" --alarm 15m
remctl add "Pay rent" --recurrence monthly
remctl add "Annual review" --recurrence yearly
```

Recurring schedules and normal alarms use EventKit and work on both `add` and `edit`. Accepted recurrence forms are `daily`, `weekly`, `weekly mon,wed,fri`, `monthly`, `monthly 1,15`, and `yearly`; invalid recurrence, alarm, and priority values fail before writing. `info --json`, `show --json`, and other read commands decode the stored recurrence rows back into a stable `recurrence` object. Normal alarms appear in `info --json` as `alarms` entries; relative alarms include `relativeOffset`, `relativeOffsetMinutes`, and a label such as `15 minutes before due date`. Use `edit ID --alarm clear --json` to remove normal alarms.

Early Reminders:

```bash
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m
remctl edit 23880 --private --early-reminder 1h
remctl edit 23880 --private --early-reminder clear
```

Early Reminders are private ReminderKit due-date delta alerts, not EventKit alarms. They require `--private`; setting one requires an existing or newly supplied due date. Accepted values are `15m`, `1h`, `2d`, `1w`, `1mo`, and equivalent words such as `15 minutes`; `clear`, `none`, `off`, or `never` removes existing Early Reminders. JSON readback includes `earlyReminder` and `earlyReminders` with fields such as `unit`, `count`, `value`, `direction`, and `label`.

Location alarms are normal reminder alarms saved through EventKit structured-location alarms. RemCTL still requires `--private` for the location command surface so agents do not treat it as an ordinary alarm. `edit --private --location-title ... --latitude ... --longitude ...` verifies through `info --json` in the same `alarms` array with `type: "location"` and a `location` object containing title, coordinates, radius, and `proximity`. `--address` is not supported.

## Editing

```bash
remctl done 23880
remctl done 23880 --date 2026-05-27
remctl done 23880 --date "2026-05-27 09:30"
remctl undone 23880
remctl edit 23880 --title "New title"
remctl edit 23880 -d "next friday" -p medium
remctl edit 23880 -d clear
remctl edit 23880 -l Work
remctl edit 23880 --list-id 156
remctl edit 23880 --recurrence "weekly mon,wed"
remctl flag 23880
remctl unflag 23880
remctl delete 23880
remctl delete 23880 --force
```

`done --date WHEN` records an explicit completion date instead of "now". It also works on an already-completed reminder to correct the stored completion date. `WHEN` must be an absolute `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`; recurring reminders reject `--date` because plain completion advances the series and EventKit discards a manually supplied completion date.

For parent reminders with subtasks, Reminders rejects an in-place EventKit list move. Some ordinary reminders can also hit EventKit list/container boundaries when moving between private and shared CalDAV containers. For a pure list move, RemCTL handles those shapes by cloning the reminder into the destination list with ReminderKit, verifying the cloned reminder and subtask count, then deleting the original. JSON output includes `method: "clone-delete"`, `oldId`, the new `id`, and `subtasksMoved` (`0` for ordinary reminders). Move first, then apply unrelated title/notes/due/private edits to the returned ID.

## Lists

```bash
remctl list-symbols
remctl list-symbols --json
remctl list-symbols --preview
remctl list-symbols --html ~/Desktop/remctl-list-symbols.html
remctl list-create "Project X" --color blue
remctl list-create "Project X" --color orange --private --symbol education3
remctl list-create "Cold Ideas" --color cyan --private --emoji 🥶
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl list-create "Ideas" --private --group Writing
remctl list-create "References" --private --group-id 476
remctl list-edit "Shopping" --private --groceries --grocery-locale it_IT
remctl list-edit "Shopping" --private --standard
remctl add "Milk" -l Groceries --private --grocery
remctl edit 23880 --private --grocery
remctl list-edit "Project X" --private --color '#FF8D28' --symbol education3
remctl list-edit --list-id 144 --private --emoji 📌
remctl list-pin "Project X" --private
remctl list-pin "Flagged" --private
remctl list-unpin --list-id 144 --private
remctl list-unpin --smart-list-id 4 --private
remctl list-rename "Project X" "Project Y"
remctl list-rename --list-id 144 --new-name "Project Y"
remctl list-delete "Project Y" --force
remctl list-delete --list-id 144 --force
```

`list-create --color NAME` uses EventKit and supports Reminders color names such as `red`, `orange`, `yellow`, `green`, `blue`, `purple`, `brown`, `gray`, and `cyan`.

List symbols, emoji badges, Groceries mode, and pin state are private Reminders metadata and require `--private`. `list-edit` is the exact-target appearance and list-type editor; `list-pin` and `list-unpin` toggle the Reminders.app sidebar pin state for regular lists and smart lists. Use `--list-id` or `--smart-list-id` when duplicate or normalized names could match more than one target. With `--private`, `--color` also accepts `#RRGGBB`.

Groceries lists are detected from private list columns. Human `lists` and `show` output marks them with `🥕`, and `show` decorates known Groceries section headings with matching category emoji such as `🥛 Dairy, Eggs & Cheese`, `🥬 Produce`, and `🧻 Household Items`. `lists --json` includes `listType`, `isGroceries`, and `grocery` locale/categorization fields; `show --json` includes `sectionEmoji` when a reminder belongs to a known Groceries category. Use `list-create --private --groceries --grocery-locale en_US` for new Groceries lists, `list-edit --private --groceries` or `--standard` to convert existing lists, and `add/edit --private --grocery` to verify Reminders' automatic grocery sections, with an explicit ReminderKit categorizer fallback when needed.

`list-symbols` prints the 71 official Reminders emblem names bundled in RemindersUICore. The terminal preview column is an approximate Unicode fallback, not the native icon. Use `list-symbols --preview` to generate and open a standalone HTML contact sheet from the native badge assets with interactive official color swatches, or `list-symbols --html PATH` to write that contact sheet without opening it. Reminders stores picker icons as private emblem names, not public SF Symbol names. For example, Reminders stores the pencil/ruler picker icon as `education3`. `--symbol` is intentionally restricted to official names because arbitrary SF Symbol strings can be accepted by ReminderKit but render as the default list icon in Reminders. Use `--emoji` for custom standard emoji badges.

## List Groups

```bash
remctl groups
remctl groups --json
remctl groups --format table
remctl group-info Writing
remctl group-info --group-id 476 --json
remctl show Writing --json
remctl group-create "Writing" --private --add-list Editorial
remctl group-create "Writing" --private --add-list-id 137 --json
remctl list-create "Ideas" --private --group Writing
remctl group-edit "Writing" --private --new-name "Drafts"
remctl group-edit "Writing" --private --add-list Ideas --remove-list Socials
remctl group-edit "Writing" --private --move-list Ideas --before-list Editorial
remctl group-edit "Writing" --private --move-list-id 137 --last
remctl group-edit --group-id 476 --private --add-list-id 475 --json
remctl group-delete "Drafts" --private --force
remctl group-delete --group-id 476 --private --force --json
```

Groups are Reminders list containers, not reminder containers. `groups` reports active/completed/total reminder counts for each group and child list. `group-info` shows IDs, object UUIDs, child lists, counts, and suggested follow-up commands. `show <group> --format table` prints separate tables for child lists and sections. With `--completed`, table output uses a `Completed` column instead of due status. `group-create` creates the group and can move existing lists into it. `list-create --private --group` creates a new list directly inside a group. `group-edit` can rename the group, add or remove lists, and reorder a list with `--move-list` plus `--before-list`, `--after-list`, `--first`, or `--last`. `group-delete` first detaches every child list to the top level, then deletes the empty group. These operations never move reminders between lists; verify with `show <list> --json` or `show <group> --json`.

## Smart Lists

```bash
remctl smart-lists
remctl smart-lists --json
remctl smart-list-create "Flagged Review" --private --flagged
remctl smart-list-create "High Priority" --private --priority high
remctl smart-list-create "Any Tag" --private --any-tag
remctl smart-list-create "#remctl Today" --private --tags remctl --date today
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today
remctl smart-list-create "Projects Today" --private --include-list Projects --date today --date-today-include-past-due
remctl smart-list-create "Due Before June 1" --private --date-range 2026-05-16,2026-05-31 --color red --emoji 📆
remctl smart-list-edit "Priority or Today" --private --priority high --color red --emoji 📆
remctl smart-list-delete "Flagged Review" --private --force
```

`smart-lists` is a read-only inspector. It reports built-in and custom smart lists with numeric ID, object UUID, smart-list type, pin state, pin date, filter byte length, and a decoded summary when RemCTL recognizes the filter payload. Unrecognized or corrupt filter blobs surface as an `error` field in `--json` instead of failing the command. Smart-list pin verification should use `pinned`/`pinnedDate` from `smart-lists --json`; on macOS 26 smart-list pinning updates `ZPINNEDDATE` rather than the regular-list boolean.

`smart-list-create` and `smart-list-edit` are private ReminderKit support and always require `--private`. They support private appearance flags (`--color`, `--symbol`, and `--emoji`) plus the filters that currently materialize in Reminders.app through this write path: `--any-tag`, selected tags via `--tags` with optional `--tag-match all|any`, date filters (`--date any|today`, today+past-due, on/before/after/range), time filters (`morning`, `afternoon`, `evening`, `night`), priority filters including comma-separated Priority: Any, `--flagged`, `--vehicle connected`, specific `--location-title`/coordinates, one `--include-list` or one `--include-list-id`, and top-level `--match all|any`. Known zero-filter writes are rejected before saving: legacy short selected-tag JSON, untagged, no-date, relative date, no-time, vehicle disconnected, list exclusions, and more than one included list.

For advanced use, `--filter-json` accepts a raw official Reminders filter payload (inline JSON or `@path`). It bypasses the guarded flag builders above but is still validated: payloads with malformed date strings are rejected before writing on both `smart-list-create` and `smart-list-edit`. `--match`, `--tag-match`, and `--list-match` only change how other filters combine, so `smart-list-edit` rejects a match-only edit and asks for at least one filter option (for example `--tags`, `--priority`, or `--date`) alongside them.

`smart-list-edit` replaces the filter for an existing custom smart list by exact name or `--smart-list-id`. `smart-list-delete` only matches custom smart lists by exact name or `--smart-list-id`, never built-in smart lists, and requires `--private`.

## Templates

```bash
remctl templates
remctl templates --json
remctl template-info "Rome: Things To See"
remctl template-info --template-id 2 --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-create "Archive Template" --from-list-id 144 --include-completed --private
remctl template-apply "Packing Template" --private --json
remctl template-apply --template-id 2 --private
remctl template-delete "Packing Template" --private --force
remctl template-delete --template-id 2 --private --force --json
```

`templates` is a read-only inspector for saved Reminders templates. It reports numeric ID, object UUID, deep link, item count, section count, dates, badge metadata, and any existing public template link. Existing iCloud links are read-only metadata; RemCTL does not create or revoke template sharing links.

`template-info` reads one template by exact name or `--template-id` and includes saved reminder rows, decoded template metadata keys, tags, recurrence rules, alarm trigger dictionaries, and template sections when present.

`template-create`, `template-apply`, and `template-delete` are private ReminderKit commands and always require `--private`. `template-create` saves an entire existing list as a template by `--from-list` or `--from-list-id`; pass `--include-completed` only when completed reminders should be part of the saved template. `template-apply` creates a new list from the selected template. `template-delete` deletes only the saved template, not lists previously created from it.

Template writes are list-level only. RemCTL does not append individual reminders to an existing template, copy selected reminders into a template, or offer flags to strip subtasks or due dates while saving a template.

Verify template writes with `remctl templates --json` or `remctl template-info`. Verify applied templates with `remctl lists --json` and `remctl show <new list> --json`.

## Import and Export

```bash
remctl export --list Shopping --format json > shopping.json
remctl export --list-id 153 --format json > shopping.json
remctl export --format csv > all-reminders.csv
remctl import shopping.json
```

## Links

```bash
remctl link 23880
remctl link -l Shopping
remctl link --list-id 153
remctl open 23880
remctl open
```

## Output Formats

```bash
remctl today
remctl --format table today
remctl today --json
remctl --format json today
NO_COLOR=1 remctl today
```

Human-readable output shows:

- `#ID` for each reminder
- the `#ID` colored with its list color when available
- priority markers: `!!!` for high, `!!` for medium, `!` for low
- `⚑` for flagged reminders
- `⏰` for macOS 26 urgent reminders
- repeat badges such as `↻ monthly` for recurring reminders
- section and subtask context where the command supports it

JSON output preserves machine-readable fields:

```json
{
  "id": 23880,
  "title": "Standup",
  "list": "Work",
  "flagged": false,
  "urgent": false,
  "dueDate": "2026-05-05T09:00:00",
  "displayDate": "2026-05-05T08:45:00",
  "alarms": [
    {
      "type": "relative",
      "relativeOffset": -900,
      "relativeOffsetMinutes": -15,
      "label": "15 minutes before due date"
    }
  ],
  "earlyReminder": {
    "unit": "minutes",
    "unitCode": 0,
    "count": -15,
    "value": 15,
    "direction": "before",
    "label": "15 minutes before"
  },
  "recurrence": {
    "frequency": "weekly",
    "interval": 1,
    "daysOfWeek": [2, 4, 6]
  },
  "attachments": [
    {
      "filename": "mockup.png",
      "type": 1,
      "path": "/Users/you/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/Account-ABCD/Attachments/<sha512>.png",
      "resolved": true,
      "uti": "public.png",
      "width": 1200,
      "height": 800
    }
  ]
}
```

`dueDate` is the actual Reminders due date. When Reminders stores a separate display/alert date, such as a normal alarm 15 minutes before the due date or an all-day display date, JSON also includes `displayDate`; agents should not treat `displayDate` as the due date. For ordinary rescheduling with `edit -d`, RemCTL carries forward a single absolute alarm when that alarm matches the old due/display time, so Reminders.app's visible time moves with the due date instead of staying stale. `edit -d clear` removes a single matching absolute alarm/display time while preserving unrelated custom alarms.

Normal read-command JSON includes numeric `id` values. `--via-eventkit` is the exception: it returns `source: "eventkit"`, `fidelity: "limited"`, and per-item `eventKitId` values instead of numeric IDs. Those identifiers are not accepted by RemCTL numeric-ID commands.

## Inline Images

```bash
remctl info 847 --images
remctl show Work --images --verbose
remctl today --images --verbose
remctl info 847 --images --image-mode halfblock --image-width 48
```

`--images` renders image attachments inline in human output. `info` always renders its attachments when `--images` is passed; list commands (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`) render attachments only together with `--verbose`, so ordinary list output stays compact. Rendering happens only on a real TTY — it is skipped in pipes, `--json`, and table mode, so automation never sees escape sequences.

Two flags control the render, with matching environment variables (flags win over env):

| Flag | Env | Default | Notes |
| --- | --- | --- | --- |
| `--images` | `REMCTL_IMAGES=1` | off | Render image attachments inline |
| `--image-mode MODE` | `REMCTL_IMAGE_MODE` | auto-detect | One of `kitty`, `iterm2`, `halfblock`, `none` |
| `--image-width N` | `REMCTL_IMAGE_WIDTH` | ~40% of terminal width | Render width in terminal cells |

Without `--image-mode`, RemCTL picks the best protocol for the current terminal:

| Mode | Used on | Fallback behavior |
| --- | --- | --- |
| `kitty` | Ghostty, Kitty, WezTerm, Konsole (Kitty graphics protocol) | — |
| `iterm2` | iTerm2; Blink on iOS over SSH (iTerm2 inline image protocol) | — |
| `halfblock` | Terminals advertising truecolor/256color | Used when no graphics protocol is detected |
| `none` | — | Disables rendering explicitly |

Terminals without a usable protocol skip inline rendering entirely — there is no ASCII fallback. The plain attachment filename lines always print regardless of mode.

Rendering has no required dependencies: Pillow is used when it is importable, otherwise macOS `sips` decodes pixels through a stdlib BMP path, so a stock macOS install works out of the box. Attachments larger than 16 MB are reported in JSON but skipped for inline rendering (`(preview unavailable)`), and `halfblock` rendering assumes a dark terminal background.

Two failure markers can appear in place of a render: `(file not downloaded on this Mac)` for legacy attachments whose file is not present locally, and `(preview unavailable)` when a resolved file cannot be rendered.

`REMCTL_IMAGES_FORCE=1` bypasses the TTY check. It exists for tests only; do not use it in scripts or normal output, since it can emit escape sequences into non-terminal consumers.

## List Badges

Plain human list output can end a reminder's one-line summary with up to two trailing emoji badges:

- `🔗` — the reminder has at least one rich link (a `ZREMCDOBJECT` URL row, or a legacy saved attachment typed `url`).
- `🌄` — the reminder has at least one image attachment.

When both apply they print in that order, space-separated, at the end of the line after tags and any `[N subtasks]` count:

```text
[ ] #30165 Try Halo app on iOS again 🌄
[ ] #30174 Read the new MacStories piece 🔗
[ ] #30180 Review layout mockups 🔗 🌄
```

The badges appear in `show`, `search`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `group show`, and on subtask lines in `subtasks` and `info`. Completed reminders keep their badges. They are loaded in batch alongside the other list metadata, so they add no per-reminder queries.

Badges are a plain human output affordance only: they are never present in `--json`, CSV export, `--format table`, or `--via-eventkit` payloads. Agents should read the `attachments` and `url` JSON fields instead.

## Attachments in JSON

`info --json` and list-command JSON (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`) include an `attachments` array on any reminder that has attachments; the key is omitted when a reminder has none. Subtask attachments remain visible only through `info --json`.

Each entry has this shape:

```json
{
  "filename": "mockup.png",
  "type": "image",
  "path": "/Users/you/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/Account-ABCD/Attachments/<sha512>.png",
  "resolved": true,
  "uti": "public.png",
  "width": 1200,
  "height": 800
}
```

`path` is the sha512-verified on-disk file inside Reminders' group container. Agents can read that file directly — vision-capable models can open the image. For legacy attachments that were never downloaded to this Mac, `path` is `null` and `resolved` is `false`; treat those as unavailable rather than an error.

## Due Date Formats

| Format | Example |
| --- | --- |
| `today` | `-d today` |
| `tomorrow` | `-d tomorrow` |
| `+Nd` | `-d +3d` |
| `+Nw` | `-d +1w` |
| `+Nh` | `-d +2h` |
| `eod` | `-d eod` |
| `eow` | `-d eow` |
| `tonight at <time>` | `-d "tonight at 11"` |
| `<day> at <time>` | `-d "Friday at 15:00"` |
| `next <day>` | `-d "next friday"` |
| `next <day> at <time>` | `-d "next monday at 3pm"` |
| ISO date | `-d 2026-04-15` |
| ISO date and time | `-d "2026-04-15 14:00"` |

If `-d/--due` is present and RemCTL cannot parse it, `add` and `edit` fail before writing. In `--json` mode, the error has `code: "invalid_due_date"` plus accepted examples so agents can retry with a deterministic value. Natural-language parsing uses `parsedatetime` when it is installed, but the core CLI has no required Python dependencies; agents should prefer `YYYY-MM-DD HH:MM` when they can resolve the date themselves.

## Setup Commands

```bash
remctl onboard
remctl permissions full-disk-access
remctl doctor
remctl doctor --for-agent --json
remctl setup --shell auto --doctor
remctl completion zsh
```

Use [installation.md](installation.md) for the first-run visual permission flow. The manual fallback is `remctl doctor --for-agent`, then adding the printed target in System Settings. Run `doctor` from the same terminal, app, or agent runner that will run the RemCTL write; `doctor` checks both direct database access and EventKit Reminders write authorization for that context.

`doctor` reports the `remctl-private` protocol version alongside its path. After updating RemCTL you must rebuild the private helper; an outdated helper is flagged with a "re-run install.sh" message and `--private` writes fail until it is rebuilt.

`doctor` also checks whether an installed zsh completion file appears in exported `FPATH` or the usual zsh startup files. If it warns, add the printed `fpath=(... $fpath)` and `compinit` lines to `~/.zshrc`, then open a new terminal.

## Agent Fast Path

For task creation, agents should avoid setup checks once the context is known-good:

```bash
remctl add "Title" -l Projects --private --section "Section" -d "2026-05-12 15:00" --url "https://example.com" --json
remctl info <numericId> --json
```

`add --json` returns `numericId` when the new reminder is immediately visible in the local database. Use that ID for `info`; fall back to resolving by title from `show <list> --json` only if `numericId` is absent. `info --json` includes private rich-link URLs, parent and subtask image attachments, EventKit alarms, location alarms, Early Reminders, and recurrence metadata, so raw SQLite verification should not be needed for normal reminder metadata tasks. Attachment entries include a sha512-verified `path` to the local file (or `path: null` with `resolved: false` for legacy attachments not downloaded on this Mac); list-command JSON carries the same `attachments` array for parent reminders. See [Inline Images](#inline-images) and [Attachments in JSON](#attachments-in-json).

If a `--private` write partially fails after the reminder already exists, `add --json` returns `{"status":"partial", ...}` (text mode prints an explicit "Do NOT re-run add" line) carrying the created `numericId`. Re-run `edit` on that ID to finish the remaining metadata; do not re-run `add`, which would duplicate the reminder.

Agents must not use `--via-eventkit` by default. It is a limited read-only fallback only for `show`, `search`, `today`, and `upcoming` when Full Disk Access blocks a basic read and the task does not need chainable IDs or private metadata. Its `eventKitId` values are not RemCTL numeric IDs and must not be used with `info`, `edit`, `done`, `delete`, `link`, `open`, or `subtasks`.

If an agent supplies an invalid due date, RemCTL creates nothing and exits with a structured `invalid_due_date` error on stderr. Retry the same `add` command with one of the provided examples, using `YYYY-MM-DD` for all-day reminders or `YYYY-MM-DD HH:MM` for timed reminders; do not create first and patch the due date afterward.

For Groceries automation, detect eligible lists with `remctl lists --json` and `listType == "groceries"`. After `add --private --grocery`, verify with `remctl show <list> --json` and check that the reminder has a non-empty `section` once categorization completes.

For live release verification of private surfaces, run `python3 scripts/live_private_matrix.py` from the repo after compiling the local helpers. It creates disposable Reminders lists, reminders, smart lists, and templates; verifies them through RemCTL JSON output; and cleans up unless `--keep` is passed.
