# Private Metadata Writes

RemCTL's normal write path is EventKit via `remctl-bridge`. Private metadata writes are different: they use Apple's private ReminderKit framework through `remctl-private`. Location alarms remain behind the private command guardrail because agents should treat them as Reminders-only metadata, but RemCTL saves them with EventKit structured-location alarms because that path materializes reliably on current macOS.

This mode is unsupported by Apple, optional, and explicit. Use `--private` on `add`, `edit`, private list appearance and pinning commands, custom smart-list creation/editing/deletion, or template creation/application/deletion.

RemCTL still does not write directly to SQLite.

## Safety Model

`remctl-private` receives a bounded JSON payload on stdin, looks up the target reminder through ReminderKit, applies one of a fixed set of actions, and saves through Apple's Reminders stack. It does not spawn a shell, does not accept arbitrary Objective-C selectors, and does not mutate the SQLite store.

The helper is still experimental. It links a private framework, so Apple can rename classes, change method signatures, reject behavior, or alter sync semantics in any macOS update. Public builds should treat this as an opt-in power-user feature, not a stability guarantee.

## Supported

Verified on macOS/iCloud sync:

- web rich URL attachments: `--private --url https://example.com`
- synced tags: `--private -t remctl,work`
- section assignment: `--private --section "Research"`
- section assignment by stable ID: `--private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8`
- section creation and assignment: `--private --new-section "Research"`
- subtasks: `--private --subtask "Follow up"` or rich JSON objects with child metadata
- image attachments: `--private --image ~/Desktop/mockup.png`
- real flag state: `edit ID --private --flagged` or `add ... --private -f`
- urgent state: `add "Leave now" --private --urgent` or `edit ID --private --urgent`
- Early Reminders: `add "Leave early" -d "today 14:00" --private --early-reminder 15m`, `edit ID --private --early-reminder 1h`, or `edit ID --private --early-reminder clear`
- location alarms: `edit ID --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090` (guarded by `--private`, saved through `remctl-bridge`)
- list appearance metadata: `list-create "Projects" --private --symbol education3`, `list-edit Projects --private --color '#FF8D28' --emoji 📌`
- list and smart-list pin state: `list-pin "Project X" --private`, `list-pin "Flagged" --private`, `list-unpin --smart-list-id 4 --private`
- custom smart lists with verified materializing Reminders filters: `smart-list-create "Flagged Review" --private --flagged`, `smart-list-create "Priority or Today" --private --match any --priority high,medium --date today`, `smart-list-create "Projects Today" --private --include-list Projects --date today --date-today-include-past-due`, and exact custom smart-list cleanup via `smart-list-delete "Flagged Review" --private --force`
- Reminders templates: `template-create "Packing Template" --from-list Packing --private`, `template-apply "Packing Template" --private`, and exact cleanup via `template-delete "Packing Template" --private --force`

Not exposed:

- generic file/PDF attachments. They are rejected because Reminders does not reliably display them.
- guessed or undocumented smart-list filter keys beyond the official Reminders.app samples decoded by `smart-lists`.
- iCloud template link creation. Existing template links can be read, but publishing/revoking links is not exposed.
- raw SQLite writes. Earlier experiments proved direct row insertion can stay local-only and fail to sync.

## Create Examples

```bash
remctl add "Research" -l Projects --private \
  --url "https://example.com" \
  -t remctl \
  --section "Research"

remctl add "Research" -l Projects --private \
  --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8

remctl add "Prepare screenshots" -l Projects --private \
  --image ~/Desktop/mockup.png \
  --subtask "Export final PNG"

remctl add "Launch assets" -l Projects --private \
  --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"],"urgent":true}'

remctl add "Flagged private task" -l Work --private -f
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m
```

With `--private`, `--url` creates a web rich link attachment and `-t/--tags` creates real synced Reminders tags. Without `--private`, `--url` is appended to notes and `-t/--tags` appends inline hashtags to the title.

## Edit Examples

```bash
remctl edit 23880 --private --url "https://example.com"
remctl edit 23880 --private -t remctl,work
remctl edit 23880 --private --section "Research"
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8
remctl edit 23880 --private --new-section "Inbox Zero"
remctl edit 23880 --private --subtask "Follow up"
remctl edit 23880 --private --subtask '{"title":"Follow up","notes":"Bring latest numbers","due":"next friday at 3pm","url":"https://example.com","tags":["work"],"flagged":true}'
remctl edit 23880 --private --image ~/Desktop/mockup.png
remctl edit 23880 --private --flagged --urgent
remctl edit 23880 --private --early-reminder 1h
remctl edit 23880 --private --early-reminder clear
remctl edit 23880 --private --no-flagged --no-urgent
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200 --proximity arriving
```

`--subtask` remains backwards compatible with a plain title string. To set metadata on the child reminder itself, pass a JSON object. Supported subtask fields are `title`, `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location fields (`locationTitle`, `latitude`, `longitude`, `radius`, `proximity`). `address` is not supported for location alarms. Public fields such as notes, due dates, and location alarms are applied through `remctl-bridge`; private fields such as rich links, tags, and Early Reminders are applied through `remctl-private`.

Subtask due dates use the same parser as parent reminders. Invalid parent or subtask due dates fail before RemCTL creates or edits anything, so private metadata is not silently dropped onto a partially-created reminder.

Early Reminders are stored by Reminders as private `REMDueDateDeltaAlert` metadata and mirrored in `ZDUEDATEDELTAALERTSDATA`. RemCTL writes them with `REMReminderChangeItem.dueDateDeltaAlertContext`, removes existing due-date delta identifiers before replacing the value, and verifies readback through `remctl info ID --json`. Non-clear values require a due date because Reminders anchors the delta to the reminder's due date. The unit mapping is `0 = minutes`, `1 = hours`, `2 = days`, `3 = weeks`, `4 = months`; RemCTL accepts friendly forms such as `15m`, `1h`, `2d`, `1w`, and `1mo`.

`--section` resolves by name inside the target list. If duplicate section names exist, RemCTL automatically uses the only non-empty matching section when there is exactly one. If the duplicate remains ambiguous, the command fails before writing and prints the available stable IDs; pass one with `--section-id`.

## List Appearance Examples

```bash
remctl list-symbols
remctl list-symbols --preview
remctl list-create "Research" --color orange --private --symbol education3
remctl list-create "Focus" --private --color '#34C759' --emoji 🎯
remctl list-edit Projects --private --color orange --symbol education3
remctl list-edit --list-id 144 --private --symbol education3
remctl list-edit Projects --private --emoji 📌
remctl list-rename --list-id 123 --new-name "Project Archive"
remctl list-pin "Project X" --private
remctl list-pin "Flagged" --private
remctl list-unpin --list-id 144 --private
remctl list-unpin --smart-list-id 4 --private
```

List colors and badge emblems were reverse-engineered from `ZREMCDBASELIST`. `ZCOLOR` stores a `REMColor` keyed archive. `ZBADGEEMBLEM` stores either an emoji JSON string or a private Reminders emblem name. `list-symbols` prints the 71 official emblem names bundled in RemindersUICore; the terminal glyph column is approximate. Use `list-symbols --preview` or `list-symbols --html PATH` for a native-asset HTML contact sheet with interactive official color swatches. RemCTL writes those values through ReminderKit change items, not by editing the database.

Important limits:

- `list-create --color NAME` works without `--private` through EventKit for normal color names.
- `list-create --private --color '#RRGGBB'` and `list-edit --private --color '#RRGGBB'` use private ReminderKit for exact custom colors.
- `--symbol` writes one of the official Reminders emblem names printed by `list-symbols`. Reminders' own picker uses private names such as `education3`; arbitrary SF Symbol strings are rejected because they fall back to the default icon in Reminders.
- `--emoji` writes a Reminders emoji badge for standard emoji such as `🥶` or `📌`.
- `list-edit` resolves by exact list name, then safe normalized matching; if a duplicate match is ambiguous, use `--list-id`.
- `list-pin` and `list-unpin` can target regular lists or smart lists by name. If a name matches both, use `--list-id` or `--smart-list-id`.
- Verify regular list pinning with `lists --json` and smart-list pinning with `smart-lists --json`. Smart-list rows can leave `ZISPINNEDBYCURRENTUSER` empty while updating `ZPINNEDDATE`; RemCTL reports `pinned: true` when the smart-list pin date is positive.

## Groceries List Examples

```bash
remctl lists --json
remctl show Groceries
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl list-edit "Shopping" --private --groceries --grocery-locale it_IT
remctl list-edit "Shopping" --private --standard
remctl add "Milk" -l Groceries --private --grocery --json
remctl edit 23880 --private --grocery --json
```

Reminders' Groceries type is stored as private list metadata, not as a separate public EventKit list class. RemCTL reads the grocery columns from `ZREMCDBASELIST`, marks detected Groceries lists with `🥕` in human output, and returns `listType`, `isGroceries`, and `grocery.locale` / categorization flags in `lists --json`.

Groceries writes use `REMListChangeItem.groceryContextChangeItem`: `list-create --private --groceries` creates a list with grocery metadata, `list-edit --private --groceries` converts an existing list, `list-edit --private --standard` clears the grocery flag, and `--grocery-locale` writes the locale identifier Reminders stores for grocery categorization.

`add --private --grocery` and `edit --private --grocery` verify automatic grocery sorting for the target reminder IDs. The target list must already be a detected Groceries list, and RemCTL fails before writing if it is not. RemCTL first polls the local section membership table because Reminders often sorts new items immediately; if the item is still unsectioned, RemCTL falls back to ReminderKit's explicit grocery categorizer. The JSON result includes `verifiedSections` and `source: "reminders_auto"` when the automatic sorter already handled it.

## Smart List Examples

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

`smart-lists` is read-only and safe. It reads `REMCDSmartList` rows from `ZREMCDBASELIST`, including built-in smart lists, and decodes known `ZFILTERDATA` payloads.

`smart-list-create` writes through `REMSaveRequest.addCustomSmartListWithName` and saves through ReminderKit. It requires `--private`, verifies that the active account supports custom smart lists, rejects duplicate exact custom names, and accepts private appearance flags plus the filters that currently materialize in Reminders.app through this write path: Any Tag, selected tags via the include/exclude tag payload, date any/today/on/before/after/range, time of day, priority single or Priority: Any, flagged, vehicle connected, specific location, one included list, and top-level all/any matching across those families. Known zero-filter writes are rejected before saving: legacy short selected-tag JSON, untagged, no-date, relative date, no-time, vehicle disconnected, list exclusions, and more than one included list. It explicitly sets the private account ownership and supported-version metadata Reminders.app expects; without those fields, the row can survive but the edit UI can show zero filters. It does not write SQLite.

`smart-list-edit` fetches an existing custom smart list by exact name or numeric `--smart-list-id`, replaces its `filterData` and/or private appearance metadata through ReminderKit, and requires `--private`. It never edits built-in smart lists.

`smart-list-delete` deletes through ReminderKit and requires `--private`. It only targets custom smart lists by exact name or numeric `--smart-list-id`; built-in smart lists are never matched.

Developer note: on the verified macOS 26 store, `ZFILTERDATA` for custom smart lists is UTF-8 JSON bytes. The decoder also accepts older research samples that wrap the same JSON in an `NSKeyedArchiver` object whose root class is `ReminderKitInternal.REMCustomSmartListFilterDescriptor`, keyed field is `data`, and payload is UTF-8 JSON. Known decoded keys are `operation`, `hashtags`, `date`, `time`, `priorities`, `flagged`, `location`, and `lists`, but not every decoded shape materializes when written back. For selected tags, Reminders.app materializes `{"hashtags":{"hashtags":{"operation":"or","include":["remctl"],"exclude":[]}}}` and can show zero filter rows for the legacy short form `{"hashtags":{"hashtags":["remctl"]}}`; RemCTL decodes the legacy form but does not write it. The `lists` key is a single filter descriptor; Reminders.app currently materializes only one included list through this write path.

## Template Examples

```bash
remctl templates
remctl templates --json
remctl template-info "Rome: Things To See" --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-create "Archive Template" --from-list-id 144 --include-completed --private
remctl template-apply "Packing Template" --private --json
remctl template-delete "Packing Template" --private --force
```

Templates are stored separately from lists in `ZREMCDTEMPLATE`; their saved reminders live in `ZREMCDSAVEDREMINDER`, and template sections use `ZREMCDBASESECTION` with `ZTEMPLATE`. `templates` and `template-info` are read-only direct database inspectors. `template-info` decodes the saved-reminder `ZMETADATA` JSON envelope enough to expose titles, tags, flags, priority, recurrence rules, alarm trigger dictionaries, and metadata keys without dumping large rich-text blobs.

`template-create` writes through `REMSaveRequest.addTemplateWithName:configuration:toAccountChangeItem:` after building a `REMTemplateConfiguration` from the source list object ID. It requires `--private`, rejects duplicate exact template names before saving, and never writes SQLite. `--include-completed` maps to ReminderKit's `shouldSaveCompleted` template configuration.

`template-apply` fetches the template by object ID and calls `REMSaveRequest.addListUsingTemplate:toAccountChangeItem:`. The new list name is controlled by Reminders' template behavior. Verify the created list with `lists --json` and `show <list> --json`.

`template-delete` fetches the template by object ID, updates it through ReminderKit, and removes it from the parent account. It deletes the saved template only; lists already created from that template are separate lists.

Template writes are list-level only. RemCTL does not append individual reminders to existing templates, copy selected reminders into a template, or offer flags to strip subtasks or due dates while saving. Those operations would need a separate native ReminderKit API before they are safe enough for this CLI.

Existing iCloud template links are read as `publicLink` when `ZPUBLICLINKURLUUID` exists. Creating or revoking iCloud sharing links is intentionally not implemented because the private `shareTemplate` operation did not reliably materialize a public link in the local store during testing.

## Guardrails

Private-only options fail before writes unless `--private` is set.

Examples of rejected commands:

```bash
remctl add "Research" -l Projects --section "Research"
remctl edit 23880 -t remctl
remctl edit 23880 --urgent
remctl edit 23880 --early-reminder 15m
remctl add "Milk" -l Groceries --grocery
remctl list-create "Groceries" --groceries
remctl template-create "Packing Template" --from-list Packing
remctl template-apply "Packing Template"
```

These fail because they would otherwise look successful while silently dropping private metadata.

Moving a reminder to another list is not private metadata: use `remctl edit ID -l LIST` or `remctl edit ID --list-id ID` through the normal EventKit bridge. If a move is combined with `--private --section` or `--private --grocery`, RemCTL validates the private metadata against the destination list.

`--private --url` and subtask `url`/`urls` accept `http` and `https` URLs. Image attachments must point to readable image files. Rich-link and image edit operations are additive: RemCTL can add synced rich links and images, but it does not remove or replace existing rich links or image attachments. Early Reminders validate their delta syntax and due-date anchor before saving. Location alarms validate latitude, longitude, radius, and proximity before saving, then write through the EventKit bridge as structured-location alarms. The previous private ReminderKit alarm mutation is intentionally not used because live testing returned a persistent helper communication failure without materializing an alarm.

## Installation and Doctor

`./install.sh` compiles `remctl-private` when `clang` is available:

```bash
clang -fobjc-arc -O -F/System/Library/PrivateFrameworks \
  -framework Foundation -framework AppKit -framework ReminderKit \
  -o ~/bin/remctl-private remctl-private.m
```

`remctl doctor` reports `private_helper`. Missing `private_helper` is a warning, not a failure, because normal RemCTL usage still works. `--private` writes fail with a direct error when the helper is unavailable.

Override the helper path for testing:

```bash
REMCTL_PRIVATE_PATH=/tmp/remctl-private remctl edit 23880 --private --url https://example.com
```

## Agent Notes

Agents must verify private writes with `remctl info ID --json` and, when sync behavior matters, ask the user to check another device. `info --json` reports private rich-link URLs in `url`, parent and subtask image attachments in `attachments`, normal and location alarms in `alarms`, Early Reminders in `earlyReminder`/`earlyReminders`, and keeps actual `dueDate` separate from Reminders' optional `displayDate`. `lists --json` and `smart-lists --json` expose persisted `color`, `badge`, and `badgeEmblem` fields for appearance verification. Agents should not query SQLite directly for ordinary reminder metadata verification. For Groceries categorization, verify with `remctl show <list> --json` because the section membership lives on the list grouping rather than only in the reminder detail payload. For templates, verify with `remctl templates --json` or `remctl template-info`, then verify applied template lists with `remctl show <list> --json`. Do not ask RemCTL to mutate individual saved reminders inside a template; current support is whole-list template create/apply/delete. Do not assume a CloudKit-clean row means the Reminders UI displays it; generic files and PDFs were the counterexample and are intentionally rejected.
