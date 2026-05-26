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
remctl smart-lists
remctl templates
remctl template-info "Rome: Things To See"
remctl show Shopping
remctl show --list-id 153
remctl show Work --completed
remctl show Family -v
remctl search "milk"
remctl search "milk" --completed
remctl info 23880
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

RemCTL uses nouns for read-only inspectors (`lists`, `smart-lists`, `templates`, `today`, `stats`) and verb-style commands for writes (`add`, `edit`, `delete`, `list-create`, `smart-list-edit`). List-management commands keep the `list-*` prefix; custom smart-list writes keep the `smart-list-*` prefix; template writes keep the `template-*` prefix.

Use `--json` on subcommands when scripting. For tabular read commands (`today`, `upcoming`, `overdue`, `flagged`, `urgent`, `lists`, `show`, and `search`), `--format json|table|plain` can be passed globally before the command or directly on the read command, so both `remctl --format table show Work` and `remctl show Work --format table` are valid. Export keeps its own `--format json|csv` because that chooses a file format, not display style.

List targets are consistent across commands that can safely resolve them: pass a list name positionally or with `-l/--list`, or pass `--list-id` when an exact numeric target matters. If both a name and `--list-id` are provided, RemCTL fails before writing or exporting. This applies to `show`, `add`, `edit`, `link`, `export`, `list-edit`, `list-pin`, `list-unpin`, `list-rename`, `list-delete`, and the smart-list `--include-list-id` filter. For pinning, `list-pin` and `list-unpin` also accept smart-list names or `--smart-list-id`; if a name matches both a regular list and a smart list, RemCTL fails before writing and asks for an explicit ID.

List names are resolved conservatively: exact match first, then case-insensitive match, then a normalized fallback that ignores decorative punctuation and emoji. If more than one list matches, RemCTL fails before writing and prints the candidate IDs; pass `--list-id` to target one explicitly.

`--section` resolves by name inside the target list. If duplicate section names exist, RemCTL uses the only non-empty matching section when there is exactly one. If the duplicate is still ambiguous, use `--section-id`.

`--subtask` accepts either a plain child title or a JSON object with child metadata. Rich subtask fields include `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location alarm fields.

`--private` uses Apple's private ReminderKit framework through `remctl-private`. It does not write SQLite directly. Verified private writes include synced web rich links, tags, sections, rich subtasks, image attachments, real flag state, urgent state, Early Reminders, location alarms, list appearance metadata, list and smart-list pin state, Groceries list metadata and categorization verification, custom smart-list creation/editing/deletion for verified materializing Reminders filters, and Reminders template create/apply/delete. Location alarms are guarded by `--private` but saved through the EventKit bridge as structured-location alarms because the private ReminderKit alarm mutation does not materialize reliably on current macOS. Generic file/PDF attachments are intentionally rejected because Reminders does not reliably show them even when private rows sync.

`edit` covers these existing-reminder surfaces:

| Surface | Command | Private? | Notes |
| --- | --- | --- | --- |
| Title, notes, due date, priority | `edit --title`, `-n`, `-d`, `-p` | No | EventKit bridge with AppleScript fallback for ordinary fields |
| Move to another list | `edit -l LIST` or `edit --list-id ID` | No | EventKit bridge; use `--list-id` for exact agent targeting |
| Recurrence and normal alarms | `edit --recurrence`, `edit --alarm` | No | EventKit bridge only; verify in `info --json` |
| Notes URL fallback | `edit --url URL` | No | Appends the URL to notes, not a rich attachment |
| Rich web URL attachment and real tags | `edit --private --url URL -t tags` | Yes | Additive; does not remove or replace existing rich links |
| Section assignment or creation | `edit --private --section`, `--section-id`, `--new-section` | Yes | If combined with `-l/--list`, section resolution uses the destination list |
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

Recurring schedules and normal alarms use EventKit and work on both `add` and `edit`. `info --json`, `show --json`, and other read commands decode the stored recurrence rows back into a stable `recurrence` object. Normal alarms appear in `info --json` as `alarms` entries; relative alarms include `relativeOffset`, `relativeOffsetMinutes`, and a label such as `15 minutes before due date`. Use `edit ID --alarm clear --json` to remove normal alarms.

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

`smart-lists` is a read-only inspector. It reports built-in and custom smart lists with numeric ID, object UUID, smart-list type, pin state, pin date, filter byte length, and a decoded summary when RemCTL recognizes the filter payload. Smart-list pin verification should use `pinned`/`pinnedDate` from `smart-lists --json`; on macOS 26 smart-list pinning updates `ZPINNEDDATE` rather than the regular-list boolean.

`smart-list-create` and `smart-list-edit` are private ReminderKit support and always require `--private`. They support private appearance flags (`--color`, `--symbol`, and `--emoji`) plus the filters that currently materialize in Reminders.app through this write path: `--any-tag`, selected tags via `--tags` with optional `--tag-match all|any`, date filters (`--date any|today`, today+past-due, on/before/after/range), time filters (`morning`, `afternoon`, `evening`, `night`), priority filters including comma-separated Priority: Any, `--flagged`, `--vehicle connected`, specific `--location-title`/coordinates, one `--include-list` or one `--include-list-id`, and top-level `--match all|any`. Known zero-filter writes are rejected before saving: legacy short selected-tag JSON, untagged, no-date, relative date, no-time, vehicle disconnected, list exclusions, and more than one included list.

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
  }
}
```

`dueDate` is the actual Reminders due date. When Reminders stores a separate display/alert date, such as a normal alarm 15 minutes before the due date, JSON also includes `displayDate`; agents should not treat `displayDate` as the due date. For ordinary rescheduling with `edit -d`, RemCTL carries forward a single absolute alarm when that alarm matches the old due/display time, so Reminders.app's visible time moves with the due date instead of staying stale. `edit -d clear` removes a single matching absolute alarm/display time while preserving unrelated custom alarms.

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

Use [installation.md](installation.md) for the first-run visual permission flow. The manual fallback is `remctl doctor --for-agent`, then adding the printed target in System Settings. Run `doctor` from the same terminal, app, or agent runner that will run the RemCTL write.

## Agent Fast Path

For task creation, agents should avoid setup checks once the context is known-good:

```bash
remctl add "Title" -l Projects --private --section "Section" -d "2026-05-12 15:00" --url "https://example.com" --json
remctl info <numericId> --json
```

`add --json` returns `numericId` when the new reminder is immediately visible in the local database. Use that ID for `info`; fall back to resolving by title from `show <list> --json` only if `numericId` is absent. `info --json` includes private rich-link URLs, parent and subtask image attachments, EventKit alarms, location alarms, Early Reminders, and recurrence metadata, so raw SQLite verification should not be needed for normal reminder metadata tasks.

If an agent supplies an invalid due date, RemCTL creates nothing and exits with a structured `invalid_due_date` error on stderr. Retry the same `add` command with one of the provided examples or a normalized `YYYY-MM-DD HH:MM` value; do not create first and patch the due date afterward.

For Groceries automation, detect eligible lists with `remctl lists --json` and `listType == "groceries"`. After `add --private --grocery`, verify with `remctl show <list> --json` and check that the reminder has a non-empty `section` once categorization completes.

For live release verification of private surfaces, run `python3 scripts/live_private_matrix.py` from the repo after compiling the local helpers. It creates disposable Reminders lists, reminders, smart lists, and templates; verifies them through RemCTL JSON output; and cleans up unless `--keep` is passed.
