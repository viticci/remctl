# Architecture

RemCTL intentionally splits reads and writes.

## Components

```text
remctl (Python)
  ├─ reads Reminders CoreData SQLite database
  ├─ formats human and JSON output
  ├─ calls remctl-bridge for normal writes and limited EventKit reads
  └─ calls remctl-private for opt-in private metadata writes

remctl-bridge (Swift)
  └─ writes through EventKit and supports --via-eventkit read fallback

remctl-private (Objective-C)
  └─ writes selected private metadata through private ReminderKit APIs

remctl-permissions (Swift/AppKit)
  └─ guides Full Disk Access setup with draggable targets
```

There is no daemon, localhost API, launch agent, or token setup in RemCTL. The CLI is the only runtime surface.

## Reads

Direct reads use the iCloud Reminders CoreData store:

```text
~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/Data-*.sqlite
```

This exposes fields EventKit does not expose cleanly for fast list views:

- sections
- subtasks
- tags
- attachments
- deep links
- list colors and badge emblems
- recurrence rules
- macOS 26 urgent state
- Early Reminder due-date delta alerts

RemCTL opens the database read-only. It never writes to SQLite.

### Attachment Resolution and Rendering

Attachment rows live in the store's attachment tables (`ZREMCDATTACHMENT` on parent reminders, saved-attachment rows for subtasks). Each row records a filename, UTI, pixel dimensions, and a `ZSHA512SUM` digest of the file's contents. The files themselves live outside the database:

```text
~/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/Account-*/Attachments/<ZSHA512SUM><ext>
```

Resolution and rendering both live on the read side, in `remctl_images.py`. RemCTL maps a row to its on-disk file by trying candidate extensions (from the filename, the UTI, then a small fallback set) and trusting the file only when its sha512 matches `ZSHA512SUM`. The verified path is what JSON exposes as `attachments[].path` alongside `resolved`, `uti`, `width`, and `height`. Legacy rows have a NULL sha and no local file; they serialize as `path: null` with `resolved: false` instead of guessing a path that cannot be verified. Subtask attachments are resolved the same way but only surface through `info`.

Inline rendering (`--images`) is purely a read-side display concern: it never involves `remctl-bridge` or `remctl-private`, and it only runs on a real TTY. RemCTL auto-detects the best protocol — Kitty graphics on Ghostty/Kitty/WezTerm/Konsole, iTerm2's inline protocol on iTerm2 and Blink, truecolor half-blocks elsewhere — with `--image-mode`/`REMCTL_IMAGE_MODE` and `--image-width`/`REMCTL_IMAGE_WIDTH` as overrides. Terminals without a usable protocol simply skip inline rendering; there is no fallback renderer. The pipeline stays stdlib-only: Pillow is used when importable, otherwise macOS `sips` decodes pixels through a small stdlib BMP reader. Nothing about images adds a required dependency or touches the write path. Plain human list output also carries trailing `🔗`/`🌄` badges for reminders with rich links or image attachments, batch-loaded alongside the other one-line metadata and never present in JSON.

## Limited EventKit Read Fallback

`--via-eventkit` is a narrow fallback for automation hosts that cannot get Full Disk Access. It is supported only by `show`, `search`, `today`, and `upcoming`, and it is never selected automatically. Normal direct reads remain the primary read path because they expose Reminders metadata that EventKit does not.

The fallback runs through `remctl-bridge` and uses public EventKit reminder predicates. It returns a wrapper payload with `source: "eventkit"`, `fidelity: "limited"`, `idWarning`, and `items`. Items use `eventKitId` from `EKReminder.calendarItemIdentifier`; they do not include RemCTL numeric `id` values. `eventKitId` is not accepted by numeric-ID commands such as `info`, `edit`, `done`, `delete`, `link`, `open`, or `subtasks`.

The fallback intentionally does not support `--list-id`, table output, sections, synced tags, private rich links, urgent state, template internals, smart-list internals, or exact ID compatibility with direct database reads. If a workflow needs those fields or needs to chain IDs into later commands, the correct fix is Full Disk Access for the host process.

## Writes

Writes go through Apple-supported APIs:

1. `remctl-bridge` writes via EventKit. This is the normal path for create, edit, moving reminders between lists, complete, delete, recurrence, alarms, URLs appended to notes, and list management. The Python CLI validates user input first, and the bridge also rejects malformed due dates, recurrence rules, alarms, priorities, and location payloads if called directly. Bridge failures are kept structured so the CLI can distinguish permission denials from move-specific container rejections.
2. AppleScript is a fallback for operations that still need Reminders.app automation behavior. It also sets the real flagged state after a non-private `add -f/--flag`, because EventKit has no public flag property. Before using the AppleScript fallback, RemCTL first checks whether the write already landed (recovery check) so retries do not create duplicates; `list-create` hard-fails on timeout or execution errors rather than falling back.

There is also an explicitly unsupported opt-in helper:

3. `remctl-private` writes selected private metadata through Apple's private ReminderKit framework. It is normally gated by `--private`, never writes SQLite directly, and is intentionally excluded from ordinary write behavior except for the verified clone-delete fallback used when EventKit cannot perform a pure list move, including parent reminders with subtasks and ordinary reminders blocked by list/container boundaries. The fallback clones the reminder into the destination list, verifies the clone and expected subtask count, then deletes the original; it is not used for EventKit access denials, missing helpers, timeouts, generic bridge errors, or list moves combined with unrelated edits. Verified private writes include web rich URL attachments, synced hashtag add/replace/remove, section assignment/creation/rename/delete, shared-list assignment, rich subtasks, image attachments, real flag state, urgent state, Early Reminders, location alarms, list appearance metadata such as exact colors, private emblem names, and emoji badges, list and smart-list pin state, list group create/rename/membership/delete, Groceries list metadata and categorization verification, experimental custom smart-list creation/editing/deletion for verified materializing Reminders filters, Reminders template create/apply/delete, and verified clone-delete list moves. Rich URL and image edit writes are additive; additive `-t/--tags` keeps existing synced tags, while `--set-tags`, `--clear-tags`, and `--remove-tag` rewrite the full tag set. RemCTL does not remove or replace existing rich links/images. For rich subtasks, `remctl-private` creates the child and applies private child metadata, then `remctl-bridge` applies public child fields such as notes and due dates. Generic file/PDF attachments are rejected because Reminders does not reliably show them even when private rows sync.

`remctl-private` answers a `protocol_version` handshake (currently protocol 1). RemCTL probes it before any `--private` write and rejects an outdated helper with a "re-run install.sh" message; `doctor` surfaces the reported protocol version. Rebuild `remctl-private` after updating RemCTL. Helper paths that previously no-op'd silently — section assignment or tag/URL writes with nil contexts — now return explicit errors instead of reporting false success, and the account fallback only accepts CloudKit accounts, failing with `No active iCloud Reminders account found` when none is available.

Shared-list assignment is resolved from local `REMCDSharee` rows before writing. `remctl sharees LIST --json` exposes those candidates for humans and agents; `--assign USER` accepts a unique name, email or phone address, numeric sharee ID, object UUID, or `me`, then `remctl-private` writes the assignment through ReminderKit's assignment context with the current-user sharee as originator.

The bridge is detected next to the installed CLI. Override it with:

```bash
REMCTL_BRIDGE_PATH=/path/to/remctl-bridge remctl add "Test"
```

`doctor` and `permissions` report the same helper path that the write path will use. Environment overrides are authoritative, even when the target is missing, so an agent can diagnose the exact failing helper path instead of silently falling back to another binary.

Override the private helper path with:

```bash
REMCTL_PRIVATE_PATH=/path/to/remctl-private remctl edit 123 --private --url https://example.com
```

See [private-metadata.md](private-metadata.md) for supported private fields, known limits, and verification rules.

## Output Safety

Human output neutralizes terminal control characters from Reminders-controlled text such as titles, notes, URLs, list names, section names, and tags. JSON output preserves the raw stored values for automation.

Private rich URL attachments validate the target before writing. RemCTL accepts only public `http` and `https` hosts and rejects loopback, `.local`, private, link-local, multicast, reserved, and unresolved hosts. Non-private `--url` remains a notes fallback and does not create a rich attachment.

## List Appearance

Reminders stores list appearance on `ZREMCDBASELIST`:

- `ZCOLOR` is a keyed archive containing a `REMColor` object with symbolic color names, hex, and RGB values.
- `ZBADGEEMBLEM` is text. Emoji badges are stored as JSON such as `{"Emoji":"📌"}`; Reminders picker icons are stored as private emblem names such as `education3`.
- `ZISPINNEDBYCURRENTUSER` and `ZPINNEDDATE` track whether the current user has pinned a list or smart list in the Reminders sidebar. Regular lists use `ZISPINNEDBYCURRENTUSER`; smart-list pinning updates `ZPINNEDDATE`, so RemCTL treats a positive smart-list pin date as pinned.
- `ZISGROUP` marks Reminders list-group containers. Child lists point at the group through `ZPARENTLIST`/`ZPARENTLIST1`; `Z_FOK_PARENTLIST`/`Z_FOK_PARENTLIST1` hold sparse ordering keys.
- `ZSHOULDCATEGORIZEGROCERYITEMS`, `ZSHOULDAUTOCATEGORIZEITEMS`, `ZSHOULDSUGGESTCONVERSIONTOGROCERYLIST`, and `ZGROCERYLOCALEID` describe Reminders' special Groceries lists.

RemCTL reads these fields directly for display and verification. `groups`, `group-info`, and `lists --json` expose list-group hierarchy and child-list reminder counts, and `show <group>` reads across child lists. Private group writes use ReminderKit instead of SQLite: `group-create` calls `REMSaveRequest.addGroupWithName` on the account group context, `list-create --private --group` creates a list and then sets its parent group, `group-edit` renames the group through the normal list appearance path and moves child lists by setting `REMListChangeItem.parentSubContainerID`, and `group-delete` first detaches each child list to the top level before removing the empty group with `REMListChangeItem.removeFromParentWithAccountChangeItem`. Child-list ordering is implemented with verified ReminderKit parent changes: RemCTL computes the desired order, detaches the target group's current children, then reattaches them in reverse visible order because assigning a list to a group places it at the top. These commands move list containers only; reminders remain in their original lists. Section management uses `REMSaveRequest.updateList`, `addListSectionWithDisplayName`, and `REMSaveRequest.updateListSection` through `section-create`, `section-rename`, and `section-delete`; create and rename refuse duplicate names before saving. `list-symbols` exposes the 71 official bundled emblem names discovered from RemindersUICore's `ListBadge*` assets. Its terminal glyphs are approximate Unicode fallbacks; `list-symbols --preview` and `list-symbols --html PATH` load the native badge assets from RemindersUICore and write a standalone HTML contact sheet with interactive official color swatches. Normal `list-create --color NAME` still writes through EventKit. Private list appearance writes use `REMStore.fetchListWithObjectID`, `REMSaveRequest.updateList`, `REMListChangeItem.setColor`, and `REMListChangeItem.appearanceContext.setBadgeEmblem` / `setBadge`, then save through ReminderKit. `list-pin` and `list-unpin` use `REMListChangeItem.setIsPinned` for regular lists and `REMSmartListChangeItem.setIsPinned` for smart lists. Groceries list metadata writes use `REMListChangeItem.groceryContextChangeItem`, `setShouldCategorizeGroceryItems`, and `setGroceryLocaleID`. For item sorting, RemCTL first polls section membership rows because Reminders auto-sorts new Groceries items; only unsectioned items fall back to `categorizeGroceryItemsWithReminderIDs`. `--symbol` is restricted to official emblem names because arbitrary SF Symbol strings are accepted by the private API but render as the default icon in Reminders. Custom icons should use `--emoji`.

## Smart Lists

Smart lists are stored in `ZREMCDBASELIST` as `REMCDSmartList` rows (`Z_ENT = 4`) with `ZSMARTLISTTYPE` and optional `ZFILTERDATA`.

`smart-lists` is read-only. It reports built-in and custom smart lists with numeric row ID, object UUID, smart-list type, pin state, pin date, filter length, and decoded filter summaries where possible. Current custom smart-list filters on macOS 26 store `ZFILTERDATA` as UTF-8 JSON bytes. The decoder also accepts keyed-archive research samples shaped as `ReminderKitInternal.REMCustomSmartListFilterDescriptor` with a `data` field containing the same JSON bytes. Blobs that fail to decode return an `error` entry instead of aborting the command.

`smart-list-create` is private and experimental. Python validates and encodes the Reminders filter payloads decoded from Reminders.app, but the user-facing write path only allows shapes verified to materialize in Reminders.app: Any Tag, date any/today/on/before/after/range, time of day, priority single or Priority: Any, flagged, vehicle connected, specific location, one included list, and top-level all/any matching across those families. Known decoded shapes that write as zero filters are rejected before saving: selected tags, untagged, no-date, relative date, no-time, vehicle disconnected, list exclusions, and more than one included list. RemCTL base64-encodes the raw JSON bytes and sends them to `remctl-private` alongside optional private appearance metadata. The helper resolves the active CloudKit account (falling back to the default account only when it is itself a CloudKit account, and failing with `No active iCloud Reminders account found` otherwise), verifies `supportsCustomSmartLists`, creates the smart list with `REMSaveRequest.addCustomSmartListWithName`, explicitly attaches the change item to the account, sets the custom smart-list supported-version fields to `20220430`, sets `smartListType`, `filterData`, color, and badge appearance when requested, and saves through ReminderKit. The account ownership fields keep the object durable; the supported-version fields are required for Reminders.app to materialize the filter controls instead of showing zero filters. `smart-list-edit` fetches a custom smart list by object ID and replaces `filterData` and/or private appearance metadata through `REMSaveRequest.updateSmartList`. `smart-list-delete` fetches a custom smart list by object ID, removes it from the parent account through ReminderKit, and never matches built-in smart lists.

## Templates

Templates are stored separately from ordinary lists:

- `ZREMCDTEMPLATE` stores saved template rows, object UUIDs, creation/modification dates, badge metadata, and optional public-link fields such as `ZPUBLICLINKURLUUID`.
- `ZREMCDSAVEDREMINDER` stores reminders saved inside each template. `ZMETADATA` is a one-byte-prefixed UTF-8 JSON payload containing fields such as title, tags, flags, priority, recurrence rules, and alarm triggers.
- `ZREMCDBASESECTION` can point at templates through `ZTEMPLATE` for saved template sections.

`templates` and `template-info` are read-only inspectors. They report numeric row IDs, object UUIDs, counts, existing public links, sections, and decoded saved-reminder metadata without mutating the store.

Template writes are private ReminderKit writes and currently stay at whole-list granularity. `template-create` resolves the source list to a ReminderKit object ID, builds `REMTemplateConfiguration` with `shouldSaveCompleted`, then calls `REMSaveRequest.addTemplateWithName:configuration:toAccountChangeItem:`. `template-apply` fetches a template by object ID and calls `REMSaveRequest.addListUsingTemplate:toAccountChangeItem:`. `template-delete` fetches the template and removes it from the parent account through `REMSaveRequest.updateTemplate`. RemCTL does not mutate individual saved reminders inside a template, append selected reminders to a template, or strip subtasks or due dates while saving a source list.

iCloud template link sharing is intentionally not implemented. RemCTL can read existing public-link UUIDs from the database, but local testing showed the private sharing operation can return success without materializing a link.

## Recurrence

EventKit writes recurrence rules. Direct reads resolve those rules from `ZREMCDOBJECT` rows linked to reminders and serialize them as:

```json
{
  "frequency": "weekly",
  "interval": 1,
  "daysOfWeek": [2, 4]
}
```

Human output summarizes the same data with badges such as `↻ weekly Mon, Wed`.

## Due Dates and Alarms

Reminder JSON keeps due dates and alarm/display dates separate for agents. `dueDate` is serialized from `ZREMCDREMINDER.ZDUEDATE`, the actual due date. When Reminders also stores `ZDISPLAYDATEDATE`, for example after adding an EventKit alarm 15 minutes before the due date or when storing an all-day reminder, RemCTL exposes that value as `displayDate` instead of overwriting `dueDate`. In create mode, date-only due inputs such as `today`, `tomorrow`, `2026-06-01`, `+3d`, and `next friday`, plus natural-language date-only inputs resolved by `parsedatetime`, are sent to the EventKit bridge as all-day reminders using date-only components. On `edit -d`, RemCTL carries a single absolute alarm forward only when it matches the old due time; this keeps Reminders.app's visible time aligned for ordinary reschedules without rewriting deliberately separate custom alarms. On `edit -d clear`, RemCTL clears a single absolute alarm only when it matches the old due/display time.

Normal alarms are `ZREMCDOBJECT` rows linked from the reminder through alarm rows (`Z_ENT = 15`) and trigger rows. Relative triggers serialize as `alarms` entries with `type: "relative"`, `relativeOffset`, `relativeOffsetMinutes`, and a human label. Absolute/date-component triggers serialize with `type: "absolute"`, `dateComponents`, and a best-effort local `date` string. Private ReminderKit location alarms use the same alarm relationship with location trigger rows and serialize as `type: "location"` plus a `location` object. Writing a location alarm replaces any existing structured-location alarm instead of stacking a second one.

## Flags, Urgent, and Early Reminders

Flags are read from `ZFLAGGED` and shown as `⚑`.

macOS 26 urgent reminders are read from `ZISURGENTSTATEENABLEDFORCURRENTUSER` and shown as `⏰`. Apple describes urgent reminders as reminders that schedule an alarm when due. Normal writes do not touch the private urgent fields; `add --private --urgent` and `edit --private --urgent` can write them through the unsupported private helper.

Early Reminders are private due-date delta alerts. Reminders stores them in `ZREMCDDUEDATEDELTAALERT` and mirrors a JSON envelope in `ZREMCDREMINDER.ZDUEDATEDELTAALERTSDATA`; the live iOS/macOS UI value “15 minutes” is `dueDateDeltaUnit = 0` and `dueDateDeltaCount = -15`. RemCTL reads that blob into `earlyReminder`/`earlyReminders` JSON fields and writes through `REMReminderChangeItem.dueDateDeltaAlertContext`, not EventKit. Replacement writes remove existing due-date delta alert identifiers before adding the new `REMDueDateDeltaInterval`, because simply adding a new alert can leave multiple early alerts attached to the same reminder.

## Permissions

The CLI process may need Full Disk Access for direct database reads and Reminders/EventKit authorization for writes.

TCC grants are context-specific. Terminal, Codex, a CI runner, and another host app can each have different access to the same Reminders database and EventKit write surface. A green report from Terminal does not prove that an agent context can read or write Reminders.

Check setup with:

```bash
remctl doctor --for-agent
```

Open the guided setup flow with:

```bash
remctl permissions full-disk-access
```

The helper opens the Full Disk Access pane, copies the first path to the clipboard, exposes each target as a draggable file row, and periodically checks whether each target can read the Reminders store. Verified targets get a green check. It does not edit macOS TCC data directly.

## Environment Overrides

```bash
REMCTL_BRIDGE_PATH=/path/to/remctl-bridge
REMCTL_PRIVATE_PATH=/path/to/remctl-private
REMCTL_PERMISSIONS_PATH=/path/to/remctl-permissions
REMCTL_PATH=/path/to/remctl
REMCTL_STORE_DIR=/path/to/reminders/store
REMCTL_CONFIG_DIR=/path/to/config
REMCTL_IMAGES=1
REMCTL_IMAGE_MODE=kitty|iterm2|halfblock|none
REMCTL_IMAGE_WIDTH=32
NO_COLOR=1
```

The `REMCTL_IMAGES*` variables mirror the `--images`, `--image-mode`, and `--image-width` flags; flags win over the environment. `REMCTL_IMAGES_FORCE=1` bypasses the TTY check for tests only and is not a supported output configuration.
