# Private Metadata Writes

Use `--private` for Reminders metadata that EventKit cannot write. RemCTL sends these changes through Apple's private ReminderKit framework using `remctl-private`. Location alarms also require this flag, but use EventKit for the actual write.

The helper accepts a fixed set of JSON actions, checks its protocol version, and saves through Reminders. It never writes SQLite directly, runs a shell, or accepts arbitrary Objective-C method names. Apple does not support these APIs and can change them in any macOS update.

## Supported fields

Private writes cover synced tags and rich links, sections, assignments, subtasks, images, flags, urgent state, Early Reminders, ordering, list appearance, pins, groups, Groceries, custom smart lists, and templates. Location alarms also require `--private` but are saved through EventKit.

The [command guide](commands.md#private-metadata) lists the flags. Generic files and PDFs are rejected because Reminders does not reliably display them. Template sharing links can be read but cannot be created or revoked.

## Create

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

remctl add "Pick up groceries" -l Shopping --private --assign Alex --json

remctl add "Flagged private task" -l Work --private -f
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m
```

With `--private`, `--url` creates a web rich link attachment and `-t/--tags` creates real synced Reminders tags. Private rich URLs must resolve to public `http` or `https` hosts; loopback, `.local`, private, link-local, multicast, reserved, and unresolved hosts fail before writing. Without `--private`, `--url` is appended to notes and `-t/--tags` appends inline hashtags to the title.

## Edit

```bash
remctl edit 23880 --private --url "https://example.com"
remctl edit 23880 --private -t remctl,work
remctl edit 23880 --private --set-tags remctl,work
remctl edit 23880 --private --remove-tag stale
remctl edit 23880 --private --clear-tags
remctl edit 23880 --private --section "Research"
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8
remctl edit 23880 --private --new-section "Inbox Zero"
remctl sharees Shopping --json
remctl edit 23880 --private --assign Alex --json
remctl edit 23880 --private --unassign --json
remctl edit 23880 --private --subtask "Follow up"
remctl edit 23880 --private --subtask '{"title":"Follow up","notes":"Bring latest numbers","due":"next friday at 3pm","url":"https://example.com","tags":["work"],"flagged":true}'
remctl edit 23880 --private --image ~/Desktop/mockup.png
remctl edit 23880 --private --flagged --urgent
remctl edit 23880 --private --early-reminder 1h
remctl edit 23880 --private --early-reminder clear
remctl edit 23880 --private --no-flagged --no-urgent
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200 --proximity arriving
remctl edit 23880 --private --location-title Office --location-address "1 Apple Park Way, Cupertino, CA"
```

## Assignment and reminder fields

Shared-list assignment uses ReminderKit assignment rows, not SQLite writes. The list must already be shared, and assignment writes require `--private`.

```bash
remctl sharees Shopping
remctl sharees Shopping --json
remctl add "Pick up groceries" -l Shopping --private --assign Alex --json
remctl edit 23880 --private --assign alex@example.com --json
remctl edit 23880 --private --assign me --json
remctl edit 23880 --private --unassign --json
```

`--assign USER` resolves `USER` against the target list's `REMCDSharee` rows. It accepts display name, first/last name, email or phone address, numeric sharee ID, object UUID, or `me`. You do not need an email address when a person's name is unique, but email/phone address or ID is better for automation because names can collide. Inspect the exact candidates with `remctl sharees LIST --json`; agents should prefer the returned `address`, `id`, or `objectUUID` when ambiguity matters. RemCTL uses the current-user share participant stored on the list as the originator and verifies readback through `info --json` under `assignment`.

`--subtask` remains backwards compatible with a plain title string. To set metadata on the child reminder itself, pass a JSON object. Supported subtask fields are `title`, `notes`, `due`, `priority`, `alarm`, `recurrence`, `earlyReminder`, `url`/`urls`, `tags`, `image`/`images`, `flagged`, `urgent`, and location fields (`locationTitle`, `latitude`, `longitude`, `radius`, `proximity`). Rich subtask URLs follow the same public-host rule as parent private URLs. `address` is not supported for location alarms. Public fields such as notes, due dates, and location alarms are applied through `remctl-bridge`; private fields such as rich links, tags, and Early Reminders are applied through `remctl-private`.

Subtask due dates, priority, recurrence, and alarms use the same validators as parent reminders. Invalid parent or subtask values fail before RemCTL creates or edits anything, so private metadata is not silently dropped onto a partially-created reminder.

Early Reminders are stored by Reminders as private `REMDueDateDeltaAlert` metadata and mirrored in `ZDUEDATEDELTAALERTSDATA`. RemCTL writes them with `REMReminderChangeItem.dueDateDeltaAlertContext`, removes existing due-date delta identifiers before replacing the value, and verifies readback through `remctl info ID --json`. Both `info` text output and `--json` show Early Reminders even when only the normalized `ZREMCDDUEDATEDELTAALERT` table is populated. Non-clear values require a due date because Reminders anchors the delta to the reminder's due date. The unit mapping is `0 = minutes`, `1 = hours`, `2 = days`, `3 = weeks`, `4 = months`; RemCTL accepts friendly forms such as `15m`, `1h`, `2d`, `1w`, and `1mo`.

`--section` resolves by name inside the target list. If duplicate section names exist, RemCTL automatically uses the only non-empty matching section when there is exactly one. If the duplicate remains ambiguous, the command fails before writing and prints the available stable IDs; pass one with `--section-id`.

`-t/--tags` is additive. `--set-tags`, `--clear-tags`, and repeatable `--remove-tag` rewrite the synced tag set, require `--private`, and cannot be combined with `-t` or each other. Tag reads and rewrites ignore soft-deleted tag links, so a recently-deleted tag is no longer returned and `--remove-tag` cannot resurrect it.

## Sections

```bash
remctl sections --json
remctl section-create "Research" -l Projects --private --json
remctl section-rename "Research" --new-name "Reading" -l Projects --private --json
remctl section-delete "Reading" -l Projects --private --force --json
```

Section management uses ReminderKit and requires `--private`. `section-create` refuses duplicate names in the target list, `section-rename` refuses collisions with another section in the same list, and `section-delete` moves reminders out of the section using Reminders' normal behavior. Verify with `sections --json` or `show <list> --json`.

## List appearance

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

Limits:

- `list-create --color NAME` works without `--private` through EventKit for normal color names.
- `list-create --private --color '#RRGGBB'` and `list-edit --private --color '#RRGGBB'` use private ReminderKit for exact custom colors.
- `--symbol` writes one of the official Reminders emblem names printed by `list-symbols`. Reminders' own picker uses private names such as `education3`; arbitrary SF Symbol strings are rejected because they fall back to the default icon in Reminders.
- `--emoji` writes a Reminders emoji badge for standard emoji such as `🥶` or `📌`.
- `list-edit` resolves by exact list name, then safe normalized matching; if a duplicate match is ambiguous, use `--list-id`.
- `list-pin` and `list-unpin` can target regular lists or smart lists by name. If a name matches both, use `--list-id` or `--smart-list-id`.
- Verify regular list pinning with `lists --json` and smart-list pinning with `smart-lists --json`. For a custom smart list, capture its `objectUUID` and filter, pin it, require `pinned: true` with a positive `pinnedDate`, then unpin it and require `pinned: false` with no positive pin date while identity and filter remain unchanged. Successful smart-list writes can leave `ZISPINNEDBYCURRENTUSER` empty while updating `ZPINNEDDATE`; RemCTL therefore derives custom pin state from a positive pin date when needed. Built-in smart-list pinning fails before saving on hosts without the generic ReminderKit fetch.

## List groups

```bash
remctl groups
remctl groups --json
remctl groups --format table
remctl group-info Writing
remctl group-info --group-id 476 --json
remctl show Writing --json
remctl group-create "Writing" --private --add-list Editorial --add-list Socials
remctl group-create "Writing" --private --add-list-id 137 --json
remctl list-create "Ideas" --private --group Writing
remctl list-create "References" --private --group-id 476
remctl group-edit "Writing" --private --new-name "Drafts"
remctl group-edit "Drafts" --private --add-list Ideas --remove-list Socials
remctl group-edit "Drafts" --private --move-list Ideas --before-list Editorial
remctl group-edit "Drafts" --private --move-list-id 137 --last
remctl group-edit --group-id 476 --private --add-list-id 475 --json
remctl group-delete "Drafts" --private --force
remctl group-delete --group-id 476 --private --force --json
```

List groups are containers for lists, not containers for reminders. `groups` and `group-info` report active/completed/total counts from child lists. `group-create` creates the group and can move existing lists under it. `list-create --private --group` creates a new list directly under a group. `group-edit` renames the group, adds or removes child lists, and can reorder a child list with `--move-list` plus `--before-list`, `--after-list`, `--first`, or `--last`. `group-delete` detaches every child list to the top level before deleting the empty group. These operations update list parentage only; reminders stay in the same child lists and should be verified with `show <list> --json`, `groups --json`, or `show <group> --json`.

## Groceries

```bash
remctl lists --json
remctl show Groceries
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl list-edit "Shopping" --private --groceries --grocery-locale it_IT
remctl list-edit "Shopping" --private --standard
remctl add "Milk" -l Groceries --private --grocery --json
remctl edit 23880 --private --grocery --json
```

Reminders' Groceries type is stored as private list metadata, not as a separate public EventKit list class. RemCTL reads the grocery columns from `ZREMCDBASELIST`, marks detected Groceries lists with `🥕` in human output, decorates known Groceries section headings with matching category emoji, and returns `listType`, `isGroceries`, and `grocery.locale` / categorization flags in `lists --json`. `show --json` includes `sectionEmoji` when a reminder belongs to a known Groceries category.

Groceries writes use `REMListChangeItem.groceryContextChangeItem`: `list-create --private --groceries` creates a list with grocery metadata, `list-edit --private --groceries` converts an existing list, `list-edit --private --standard` clears the grocery flag, and `--grocery-locale` writes the locale identifier Reminders stores for grocery categorization.

`add --private --grocery` and `edit --private --grocery` verify automatic grocery sorting for the target reminder IDs. The target list must already be a detected Groceries list, and RemCTL fails before writing if it is not. RemCTL first polls the local section membership table because Reminders often sorts new items immediately; if the item is still unsectioned, RemCTL falls back to ReminderKit's explicit grocery categorizer. The JSON result includes `verifiedSections` and `source: "reminders_auto"` when the automatic sorter already handled it.

The grocery fallback uses different method arguments by macOS version: Tahoe takes UUIDs on the grocery-context change; Golden Gate takes `REMObjectID` values on the list change. RemCTL checks available methods before calling them. See the [compatibility audit](private-api-audit-2026-08-12.md#cross-version-follow-up) for the tested contracts and rejected alternatives.

## Ordering

```bash
remctl reminder-move 23880 --before 23881 --private
remctl reminder-move 23880 --first --private
remctl reminder-move 23880 --after 23881 --smart-list "Focus" --private
remctl reminder-move 23880 --last --smart-list-id 170 --private --json
```

Ordinary-list moves use ReminderKit's list ordering changes and require both relative reminders to share a base list. Unsectioned custom-smart-list moves update the existing `REMManualOrdering` object through `REMSmartListChangeItem`; they do not mutate the local SQLite row. Cross-list anchors are supported only when targeting a custom smart list.

RemCTL intentionally refuses built-in smart lists, sectioned custom smart lists, missing manual-order records, and anchors without a persisted position. A custom smart list with no manual-order record must be manually reordered once in Reminders.app before RemCTL can preserve and update that ordering safely. Every successful command verifies the resulting identifier order from the local Reminders store before reporting success.

## Smart lists

### Pinning compatibility

RemCTL identifies the target as built-in or custom before it calls `remctl-private`. Custom smart lists always use `fetchCustomSmartListWithObjectID:error:`. Built-in smart lists use `fetchSmartListWithObjectID:error:` only when the host store exposes it. The tested Tahoe 26.2 and Golden Gate 27.0 builds both lack that generic selector, so built-in pin/unpin returns `Built-in smart-list pinning is unsupported on this macOS version` before RemCTL creates a save request. Other hosts that still expose the selector keep the existing behavior.

This split matters because a built-in object ID is not a custom smart-list object ID. Passing it to the custom fetch returns ReminderKit error `-3000`; it is not a safe fallback. Read-only inspection through `smart-lists` is unaffected.

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

`smart-lists` is read-only. It reads `REMCDSmartList` rows from `ZREMCDBASELIST`, including built-in smart lists, and decodes known `ZFILTERDATA` payloads.

`smart-list-create` writes through `REMSaveRequest.addCustomSmartListWithName` and saves through ReminderKit. It requires `--private`, requires an active iCloud (CloudKit) Reminders account and fails with `No active iCloud Reminders account found` when none is available, verifies that the active account supports custom smart lists, rejects duplicate exact custom names, and accepts private appearance flags plus the filters that appear in Reminders.app through this write path: Any Tag, selected tags via the include/exclude tag payload, date any/today/on/before/after/range, time of day, priority single or Priority: Any, flagged, vehicle connected, specific location, one included list, and top-level all/any matching across those families. Known zero-filter writes are rejected before saving: legacy short selected-tag JSON, untagged, no-date, relative date, no-time, vehicle disconnected, list exclusions, and more than one included list. It explicitly sets the private account ownership and supported-version metadata Reminders.app expects; without those fields, the row can survive but the edit UI can show zero filters. It does not write SQLite.

`smart-list-edit` fetches an existing custom smart list by exact name or numeric `--smart-list-id`, replaces its `filterData` and/or private appearance metadata through ReminderKit, and requires `--private`. It never edits built-in smart lists.

`smart-list-delete` deletes through ReminderKit and requires `--private`. It only targets custom smart lists by exact name or numeric `--smart-list-id`; built-in smart lists are never matched.

For shapes the guarded flags do not cover, `--filter-json` accepts a raw official filter payload (inline JSON or `@path`) on `smart-list-create` and `smart-list-edit`. RemCTL still rejects payloads containing malformed date strings before writing.

Developer note: on the verified macOS 26 store, `ZFILTERDATA` for custom smart lists is UTF-8 JSON bytes. The decoder also accepts older research samples that wrap the same JSON in an `NSKeyedArchiver` object whose root class is `ReminderKitInternal.REMCustomSmartListFilterDescriptor`, keyed field is `data`, and payload is UTF-8 JSON. Blobs that fail to decode return an `error` field in `smart-lists` output instead of aborting the command. Known decoded keys are `operation`, `hashtags`, `date`, `time`, `priorities`, `flagged`, `location`, and `lists`, but not every decoded shape works when written back. For selected tags, Reminders.app displays `{"hashtags":{"hashtags":{"operation":"or","include":["remctl"],"exclude":[]}}}` and can show zero filter rows for the legacy short form `{"hashtags":{"hashtags":["remctl"]}}`; RemCTL decodes the legacy form but does not write it. The `lists` key is a single filter descriptor; Reminders.app currently displays only one included list through this write path.

## Templates

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

Existing iCloud template links are read as `publicLink` when `ZPUBLICLINKURLUUID` exists. Creating or revoking iCloud sharing links is intentionally not implemented because the private `shareTemplate` operation did not reliably create a public link in the local store during testing.

## Validation and partial writes

Private-only commands and options fail before writing unless `--private` is set. If a later step fails after creation, `add --json` returns `status: "partial"`, `numericId`, `failed`, and `error`. Finish with `edit` using that id; do not create the reminder again.

Moving a reminder to another list is not private metadata: use `remctl edit ID -l LIST` or `remctl edit ID --list-id ID` through the normal EventKit bridge. If EventKit rejects a pure move across a list/container boundary, RemCTL uses a verified ReminderKit clone-delete fallback. Parent reminders with subtasks also use that fallback because EventKit rejects moving only the parent. RemCTL clones the reminder and any child reminders into the destination list, verifies the cloned reminder and subtask count, then deletes the original. The returned JSON includes the new numeric `id`, `oldId`, `method: "clone-delete"`, and `subtasksMoved` (`0` for ordinary reminders). Move first, then apply other edits to the returned ID. For ordinary reminders, if a move is combined with `--private --section` or `--private --grocery`, RemCTL validates the private metadata against the destination list but does not clone-delete combined edits.

`--private --url` and subtask `url`/`urls` accept only public `http` and `https` hosts. Image attachments must point to readable image files. Rich-link and image edit operations are additive: RemCTL can add synced rich links and images, but it does not remove or replace existing rich links or image attachments. Tag replacement/removal is available through `edit --private --set-tags`, `--clear-tags`, and `--remove-tag`. Early Reminders validate their delta syntax and due-date anchor before saving. Location alarms validate latitude, longitude, radius (1 to 100000 meters), and proximity before saving, then write through the EventKit bridge as structured-location alarms. `--location-address` is resolved first by the bridge's `geocode` action, which uses Apple's public geocoder (CoreLocation), needs no Reminders or Location Services permission, and never writes. RemCTL uses a match only when it is the only one, covers less than about a kilometer, names the street or place the user typed, and the address also gives a town or postal code; anything else, a personal label such as `Home`, a timeout, or a failure stops the command before the reminder is created or changed. [commands.md](commands.md#location-alarms-from-an-address) lists the error codes. Re-writing a location alarm replaces the existing structured-location alarm rather than adding a second one. The previous private ReminderKit alarm mutation is intentionally not used because live testing returned a persistent helper communication failure without saving an alarm.

## Installation and Doctor

`./install.sh` compiles `remctl-private` and publishes it as part of the signed Capability Host generation. Normal `auto` execution uses the exact helper path fixed by that sealed generation; a caller-side helper override cannot redirect hosted execution.

For an isolated direct helper build:

```bash
clang -fobjc-arc -O -F/System/Library/PrivateFrameworks \
  -framework Foundation -framework AppKit -framework ReminderKit \
  -o /tmp/remctl-private remctl-private.m
```

For normal hosted `auto` execution, `remctl doctor --for-agent --json` reports private-helper readiness under `capabilityHost.privateProtocol.compatible`; use it with `capabilityHost.fullReady`. The direct `private_helper` check and path apply only to explicit direct diagnostics. An incompatible hosted protocol or outdated direct helper blocks `--private` writes while normal non-private commands can keep working. Rebuild and republish the signed generation with `install.sh` after every RemCTL update.

Override the helper path for testing:

```bash
REMCTL_CAPABILITY_HOST=direct \
REMCTL_PRIVATE_PATH=/tmp/remctl-private \
remctl edit 23880 --private --url https://example.com
```

Helper overrides are supported only in direct mode for development and diagnostics.

## Agent Notes

After a write, verify the result through MCP, or through the CLI when testing it explicitly:

| Change | Readback |
| --- | --- |
| Reminder fields, tags, links, images, subtasks, assignment, alarms | `get_reminder` / `info ID --json` |
| List appearance and pins | `lists` / `lists --json` |
| Smart-list appearance, filters, and pins | `run` with `smart-lists --json` |
| Groups | `run` with `group-info GROUP --json` |
| Groceries sections and list order | `show_list` / `show LIST --json` |
| Templates | `run` with `template-info NAME --json`; after applying, read the new list |

Do not read SQLite directly for routine verification. A saved row alone does not prove that Reminders displays or syncs a feature. Ask the user to check another device when cross-device sync is part of the task.

## Live testing

The default matrix tests the CLI through its selected execution route and cleans up disposable data:

```bash
REMCTL_CAPABILITY_HOST=force python3 scripts/live_private_matrix.py --remctl "$HOME/bin/remctl"
```

Standalone helper writes bypass the signed host and need separate Reminders access for the caller. They are skipped by default and reported as `skipped`, not passed. In an already authorized development context, add `--standalone-helper` to test direct grocery categorization and legacy pin payloads. Do not grant an agent extra permissions just to run those checks.
